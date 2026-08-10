"""Microstructure edge miner for scalper research.

This is the layer that looks for edge before we even talk about a strategy.
It mines recorded tick/L2 days for conditional forward expectancy using
non-candle microstructure hypotheses:

- pressure continuation: book imbalance and taker flow agree
- absorption reversal: book liquidity absorbs opposite taker flow
- microprice continuation: microprice is displaced from mid

Output remains research-only. A candidate is not a signal, not paper approval,
and not a promotion; it is a hypothesis to pre-register for untouched replay.
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import mean
from typing import Iterable, Literal

from vnedge.research.scalper_replay_diagnostics import ScalperReplayRow
from vnedge.research.scalper_scanners import (
    ExecutionRouteDecision,
    ScalperScannerConfig,
    decide_execution_route,
    scanner_policy,
)
from vnedge.research.universe import (
    DEFAULT_DERIVATIVE_QUOTES,
    DEFAULT_EXCHANGES,
    ResearchTarget,
    discover_research_targets,
    load_research_targets,
)
from vnedge.scalping.features import IncrementalFeatureEngine, ScalperFeatures
from vnedge.scalping.microstructure import TopOfBook
from vnedge.scalping.replay_backtester import load_tick_events


@dataclass(frozen=True)
class EdgeMinerConfig:
    scanner_config: ScalperScannerConfig = field(default_factory=ScalperScannerConfig)
    horizons_ms: tuple[int, ...] = (1_000, 3_000, 5_000)
    imbalance_thresholds: tuple[float, ...] = (0.35, 0.55, 0.70)
    flow_thresholds: tuple[float, ...] = (0.58, 0.66)
    microprice_threshold_bps: tuple[float, ...] = (0.10, 0.25, 0.50)
    max_spread_bps: float = 2.0
    min_trade_count: int = 5
    sample_every_ms: int = 250
    min_samples: int = 30
    max_trade_window: int = 200
    notional_usd: float = 100.0


@dataclass(frozen=True)
class EdgeObservation:
    ts_ms: int
    side: Literal["buy", "sell"]
    forward_bps: float
    net_bps: float


@dataclass(frozen=True)
class EdgeHypothesisResult:
    exchange: str
    symbol: str
    day: str
    hypothesis_id: str
    family: str
    side: str
    horizon_ms: int
    samples: int
    avg_forward_bps: float | None
    avg_net_bps: float | None
    win_rate_pct: float
    profit_factor: float | None
    route_decision: ExecutionRouteDecision
    state: str
    can_trade: bool = False
    can_promote: bool = False
    execution_evidence: str = "hypothesis_only"
    fill_assumption: str = "synthetic_observation_fill_not_replay"
    requires_untouched_judgment: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class _Hypothesis:
    family: str
    imbalance: float = 0.0
    flow: float = 0.0
    microprice_bps: float = 0.0

    @property
    def label(self) -> str:
        if self.family == "microprice_continuation":
            return f"{self.family}|micro>={self.microprice_bps:.2f}|imb>={self.imbalance:.2f}"
        return f"{self.family}|imb>={self.imbalance:.2f}|flow>={self.flow:.2f}"


def mine_events(
    events: list[tuple[int, str, object]],
    *,
    exchange: str,
    symbol: str,
    day: str,
    config: EdgeMinerConfig = EdgeMinerConfig(),
) -> tuple[EdgeHypothesisResult, ...]:
    points = _feature_points(events, config)
    if len(points) < 2:
        return ()
    book_ts = [p[0] for p in points]
    mids = [p[1].mid_price for p in points]
    observations: dict[tuple[str, str, int], list[EdgeObservation]] = {}
    last_sample: dict[tuple[str, str, int], int] = {}

    for i, (ts_ms, top, features) in enumerate(points):
        for hypothesis in _hypotheses(config):
            side = _direction(hypothesis, features, top, config)
            if side is None:
                continue
            for horizon_ms in config.horizons_ms:
                key = (hypothesis.label, side, horizon_ms)
                if ts_ms - last_sample.get(key, -10**15) < config.sample_every_ms:
                    continue
                j = bisect.bisect_left(book_ts, ts_ms + horizon_ms, lo=i + 1)
                if j >= len(points):
                    continue
                forward = (mids[j] - top.mid_price) / top.mid_price * 10_000.0
                signed = forward if side == "buy" else -forward
                net = signed - _maker_round_trip_cost_bps(config)
                observations.setdefault(key, []).append(EdgeObservation(ts_ms, side, signed, net))
                last_sample[key] = ts_ms

    results = [
        _result(exchange, symbol, day, key, obs, config)
        for key, obs in observations.items()
    ]
    return tuple(sorted(results, key=_result_sort_key))


def mine_recorded_days(
    data_root: Path | str,
    targets: Iterable[ResearchTarget],
    days: Iterable[str],
    config: EdgeMinerConfig = EdgeMinerConfig(),
) -> tuple[EdgeHypothesisResult, ...]:
    out: list[EdgeHypothesisResult] = []
    for target in targets:
        for day in days:
            events = load_tick_events(data_root, target.exchange, target.symbol, day)
            out.extend(
                mine_events(
                    events,
                    exchange=target.exchange,
                    symbol=target.symbol,
                    day=day,
                    config=config,
                )
            )
    return tuple(sorted(out, key=_result_sort_key))


def render_edge_report(results: Iterable[EdgeHypothesisResult], *, limit: int = 40) -> str:
    results = tuple(results)
    lines = [
        "scalper edge miner",
        "policy=research_only can_trade=false can_promote=false",
        "",
        "state                  route         side pf    net_bps fwd_bps win% samples "
        "exchange    symbol          horizon hypothesis",
    ]
    for r in results[:limit]:
        lines.append(
            f"{r.state:<22} {r.route_decision.route:<13} {r.side:<4} "
            f"{_fmt(r.profit_factor):>5} "
            f"{_fmt(r.avg_net_bps):>7} {_fmt(r.avg_forward_bps):>7} "
            f"{r.win_rate_pct:>4.0f} {r.samples:>7} {r.exchange:<11} "
            f"{r.symbol:<15} {r.horizon_ms:>7} {r.hypothesis_id}"
        )
    if not results:
        lines.append("no edge observations found; record more tick/L2 data")
    return "\n".join(lines)


def _feature_points(
    events: list[tuple[int, str, object]],
    config: EdgeMinerConfig,
) -> list[tuple[int, TopOfBook, ScalperFeatures]]:
    engine = IncrementalFeatureEngine(max_trades=config.max_trade_window)
    points: list[tuple[int, TopOfBook, ScalperFeatures]] = []
    for ts_ms, kind, obj in events:
        if kind == "trade":
            engine.on_trade(obj)
            continue
        if kind == "book" and isinstance(obj, TopOfBook):
            features = engine.on_book(obj)
            points.append((ts_ms, obj, features))
    return points


def _hypotheses(config: EdgeMinerConfig) -> tuple[_Hypothesis, ...]:
    out: list[_Hypothesis] = []
    for imb in config.imbalance_thresholds:
        for flow in config.flow_thresholds:
            out.append(_Hypothesis("pressure_continuation", imbalance=imb, flow=flow))
            out.append(_Hypothesis("absorption_reversal", imbalance=imb, flow=flow))
        for micro in config.microprice_threshold_bps:
            out.append(_Hypothesis("microprice_continuation", imbalance=imb, microprice_bps=micro))
    return tuple(out)


def _direction(
    hypothesis: _Hypothesis,
    features: ScalperFeatures,
    top: TopOfBook,
    config: EdgeMinerConfig,
) -> Literal["buy", "sell"] | None:
    if top.spread_bps > config.max_spread_bps or features.trade_count < config.min_trade_count:
        return None
    imb = features.book_imbalance
    buy_ratio = features.taker_buy_ratio
    signed = features.signed_trade_notional_usd
    if hypothesis.family == "pressure_continuation":
        if imb >= hypothesis.imbalance and buy_ratio >= hypothesis.flow and signed > 0:
            return "buy"
        if imb <= -hypothesis.imbalance and buy_ratio <= 1 - hypothesis.flow and signed < 0:
            return "sell"
    elif hypothesis.family == "absorption_reversal":
        if imb >= hypothesis.imbalance and buy_ratio <= 1 - hypothesis.flow and signed < 0:
            return "buy"
        if imb <= -hypothesis.imbalance and buy_ratio >= hypothesis.flow and signed > 0:
            return "sell"
    elif hypothesis.family == "microprice_continuation":
        micro_bps = (features.microprice - features.mid_price) / features.mid_price * 10_000.0
        if micro_bps >= hypothesis.microprice_bps and imb >= hypothesis.imbalance:
            return "buy"
        if micro_bps <= -hypothesis.microprice_bps and imb <= -hypothesis.imbalance:
            return "sell"
    return None


def _result(
    exchange: str,
    symbol: str,
    day: str,
    key: tuple[str, str, int],
    observations: list[EdgeObservation],
    config: EdgeMinerConfig,
) -> EdgeHypothesisResult:
    hypothesis_id, side, horizon_ms = key
    net = [o.net_bps for o in observations]
    forward = [o.forward_bps for o in observations]
    wins = [v for v in net if v > 0]
    losses = [-v for v in net if v < 0]
    pf = (sum(wins) / sum(losses)) if wins and losses else (999.0 if wins else None)
    avg_net = mean(net) if net else None
    avg_forward = mean(forward) if forward else None
    win_rate = len(wins) / len(net) * 100.0 if net else 0.0
    family = hypothesis_id.split("|", 1)[0]
    hypothesis_row = ScalperReplayRow(
        min_imbalance=0.0,
        max_spread_bps=config.max_spread_bps,
        quotes=len(observations),
        filled=len(observations),
        missed=0,
        open_at_end=0,
        fill_rate_pct=100.0 if observations else 0.0,
        net_usd=sum(net) / 10_000.0 * config.notional_usd,
        avg_net_bps=avg_net,
        avg_adverse_bps=None,
        verdict="HYPOTHESIS_ONLY" if avg_net and avg_net > 0 else "NEGATIVE_EDGE",
        profit_factor=pf,
        breakeven_bps=_maker_round_trip_cost_bps(config),
        family_id=family,
    )
    route = decide_execution_route(hypothesis_row, config.scanner_config)
    state = _edge_state(len(observations), route, config)
    return EdgeHypothesisResult(
        exchange=exchange,
        symbol=symbol,
        day=day,
        hypothesis_id=hypothesis_id,
        family=family,
        side=side,
        horizon_ms=horizon_ms,
        samples=len(observations),
        avg_forward_bps=avg_forward,
        avg_net_bps=avg_net,
        win_rate_pct=win_rate,
        profit_factor=pf,
        route_decision=route,
        state=state,
    )


def _edge_state(
    samples: int,
    route: ExecutionRouteDecision,
    config: EdgeMinerConfig,
) -> str:
    if samples < config.min_samples:
        return "UNDER_SAMPLED"
    if route.route == "TAKER_ALLOWED":
        return "EDGE_CANDIDATE_TAKER"
    if route.route == "MAKER_ONLY":
        return "EDGE_CANDIDATE_MAKER"
    return "BELOW_BREAKEVEN"


def _maker_round_trip_cost_bps(config: EdgeMinerConfig) -> float:
    fees = config.scanner_config.replay_config
    return fees.maker_bps + fees.taker_bps + fees.slippage_bps


def _result_sort_key(r: EdgeHypothesisResult) -> tuple[int, float, float, int, str]:
    state_rank = {
        "EDGE_CANDIDATE_TAKER": 0,
        "EDGE_CANDIDATE_MAKER": 1,
        "UNDER_SAMPLED": 2,
        "BELOW_BREAKEVEN": 3,
    }.get(r.state, 4)
    return (
        state_rank,
        -(r.profit_factor or 0.0),
        -(r.avg_net_bps or -999.0),
        -r.samples,
        r.hypothesis_id,
    )


def _split_csv(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _fmt(v: float | None, digits: int = 2) -> str:
    return "--" if v is None else f"{v:.{digits}f}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="mine tick/L2 microstructure edge hypotheses")
    p.add_argument("--data-root", default="data")
    p.add_argument("--days", required=True, help="comma-separated UTC days, YYYYMMDD")
    p.add_argument("--exchanges", help="comma-separated exchange ids")
    p.add_argument("--symbols", help="comma-separated perp symbols")
    p.add_argument("--all-markets", action="store_true",
                   help="discover active linear derivative markets via CCXT")
    p.add_argument("--quote-assets", default=",".join(DEFAULT_DERIVATIVE_QUOTES))
    p.add_argument("--max-symbols-per-exchange", type=int)
    p.add_argument("--limit", type=int, default=40)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    exchanges = _split_csv(args.exchanges) or None
    if args.all_markets:
        targets = asyncio.run(discover_research_targets(
            exchanges or DEFAULT_EXCHANGES,
            quote_assets=_split_csv(args.quote_assets),
            max_symbols_per_exchange=args.max_symbols_per_exchange,
        ))
    else:
        targets = load_research_targets(
            exchanges=exchanges,
            symbols=_split_csv(args.symbols) or None,
        )
    results = mine_recorded_days(Path(args.data_root), targets, _split_csv(args.days))
    if args.json:
        payload = {
            "policy": scanner_policy(),
            "results": [r.to_dict() for r in results],
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(render_edge_report(results, limit=args.limit))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
