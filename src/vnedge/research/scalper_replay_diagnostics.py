"""Scalper replay diagnostics.

This is the honest answer to "why are no scalp signals firing?"  It does not
promote, trade, or tune live parameters.  It replays recorded tick/book data
through the conservative maker-in/taker-out replay engine and classifies the
blocker: no data, no quotes, no fills, negative edge after cost, under-sampled,
or candidate evidence.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from vnedge.scalping.microstructure import TopOfBook, TradeTick
from vnedge.scalping.replay_backtester import (
    ImbalanceScalper,
    ReplayFees,
    ReplayResult,
    TickReplayBacktester,
    load_tick_events,
)
from vnedge.scalping.parameter_registry import (
    DEFAULT_SCALPER_PARAMETER_REGISTRY,
    REFERENCE_REPLAY_FAMILY_ID,
)


_REPLAY_DEFAULTS = DEFAULT_SCALPER_PARAMETER_REGISTRY.replay_sweep_kwargs(
    family_id=REFERENCE_REPLAY_FAMILY_ID
)


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    vals = sorted(values)
    pos = (len(vals) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(vals) - 1)
    frac = pos - lo
    return vals[lo] * (1 - frac) + vals[hi] * frac


@dataclass(frozen=True)
class ReplaySweepConfig:
    family_id: str = _REPLAY_DEFAULTS["family_id"]
    exit_policy_id: str = _REPLAY_DEFAULTS["exit_policy_id"]
    min_imbalances: tuple[float, ...] = _REPLAY_DEFAULTS["min_imbalances"]
    max_spread_bps: tuple[float, ...] = _REPLAY_DEFAULTS["max_spread_bps"]
    ttl_ms: int = _REPLAY_DEFAULTS["ttl_ms"]
    stop_bps: float = _REPLAY_DEFAULTS["stop_bps"]
    target_bps: float = _REPLAY_DEFAULTS["target_bps"]
    notional_usd: float = 100.0
    maker_bps: float = _REPLAY_DEFAULTS["maker_bps"]
    taker_bps: float = _REPLAY_DEFAULTS["taker_bps"]
    slippage_bps: float = _REPLAY_DEFAULTS["slippage_bps"]
    min_fills_for_candidate: int = 20
    min_span_seconds: float = 3_600.0

    @classmethod
    def from_registry(
        cls,
        *,
        exchange: str = "binanceusdm",
        family_id: str = REFERENCE_REPLAY_FAMILY_ID,
    ) -> "ReplaySweepConfig":
        defaults = DEFAULT_SCALPER_PARAMETER_REGISTRY.replay_sweep_kwargs(
            exchange=exchange,
            family_id=family_id,
        )
        return cls(
            family_id=defaults["family_id"],
            exit_policy_id=defaults["exit_policy_id"],
            min_imbalances=defaults["min_imbalances"],
            max_spread_bps=defaults["max_spread_bps"],
            ttl_ms=defaults["ttl_ms"],
            stop_bps=defaults["stop_bps"],
            target_bps=defaults["target_bps"],
            maker_bps=defaults["maker_bps"],
            taker_bps=defaults["taker_bps"],
            slippage_bps=defaults["slippage_bps"],
        )


@dataclass(frozen=True)
class TickSampleStats:
    events: int
    book_events: int
    trade_events: int
    span_seconds: float
    spread_bps_p50: float | None
    spread_bps_p95: float | None
    abs_imbalance_p90: float | None
    taker_buy_ratio: float | None


@dataclass(frozen=True)
class ScalperReplayRow:
    min_imbalance: float
    max_spread_bps: float
    quotes: int
    filled: int
    missed: int
    open_at_end: int
    fill_rate_pct: float
    net_usd: float
    avg_net_bps: float | None
    avg_adverse_bps: float | None
    verdict: str
    profit_factor: float | None = None
    breakeven_bps: float = 0.0
    family_id: str = REFERENCE_REPLAY_FAMILY_ID
    exit_policy_id: str = "static_fast"
    exit_reason_counts: dict[str, int] | None = None


@dataclass(frozen=True)
class ScalperReplayDiagnostics:
    exchange: str
    symbol: str
    day: str
    stats: TickSampleStats
    rows: tuple[ScalperReplayRow, ...]
    primary_blocker: str
    action: str

    @property
    def best_row(self) -> ScalperReplayRow | None:
        if not self.rows:
            return None
        return max(self.rows, key=lambda r: (r.net_usd, r.filled, r.quotes))

    def to_dict(self) -> dict:
        d = asdict(self)
        d["best_row"] = asdict(self.best_row) if self.best_row else None
        return d


def _stats(events: list[tuple[int, str, object]]) -> TickSampleStats:
    books = [obj for _ts, kind, obj in events if kind == "book" and isinstance(obj, TopOfBook)]
    trades = [
        obj for _ts, kind, obj in events
        if kind == "trade" and isinstance(obj, TradeTick)
    ]
    timestamps = [ts for ts, _kind, _obj in events]
    span = (max(timestamps) - min(timestamps)) / 1000.0 if len(timestamps) >= 2 else 0.0
    total_qty = sum(t.quantity for t in trades)
    buy_qty = sum(t.quantity for t in trades if t.taker_side == "buy")
    spreads = [b.spread_bps for b in books]
    imbalances = [abs(b.book_imbalance) for b in books]
    return TickSampleStats(
        events=len(events),
        book_events=len(books),
        trade_events=len(trades),
        span_seconds=span,
        spread_bps_p50=_quantile(spreads, 0.50),
        spread_bps_p95=_quantile(spreads, 0.95),
        abs_imbalance_p90=_quantile(imbalances, 0.90),
        taker_buy_ratio=(buy_qty / total_qty) if total_qty > 0 else None,
    )


def _row_verdict(result: ReplayResult, config: ReplaySweepConfig) -> str:
    if result.quotes_placed == 0:
        return "NO_QUOTES"
    if result.filled == 0:
        return "NO_FILLS"
    if result.net_usd <= 0:
        return "NEGATIVE_EDGE"
    if result.filled < config.min_fills_for_candidate:
        return "UNDER_SAMPLED_POSITIVE"
    return "CANDIDATE"


def _row(
    min_imbalance: float,
    max_spread_bps: float,
    result: ReplayResult,
    config: ReplaySweepConfig,
) -> ScalperReplayRow:
    avg_net = (
        sum(t.net_bps for t in result.trades) / len(result.trades)
        if result.trades else None
    )
    avg_adverse = (
        sum(t.adverse_bps for t in result.trades) / len(result.trades)
        if result.trades else None
    )
    wins = [t.net_bps for t in result.trades if t.net_bps > 0]
    losses = [-t.net_bps for t in result.trades if t.net_bps < 0]
    if wins and losses:
        profit_factor = sum(wins) / sum(losses)
    elif wins:
        profit_factor = 999.0
    else:
        profit_factor = None
    exit_reasons: dict[str, int] = {}
    for trade in result.trades:
        exit_reasons[trade.exit_reason] = exit_reasons.get(trade.exit_reason, 0) + 1
    return ScalperReplayRow(
        family_id=config.family_id,
        exit_policy_id=config.exit_policy_id,
        min_imbalance=min_imbalance,
        max_spread_bps=max_spread_bps,
        quotes=result.quotes_placed,
        filled=result.filled,
        missed=result.missed_fills,
        open_at_end=result.open_quotes_at_end,
        fill_rate_pct=result.fill_rate * 100.0,
        net_usd=result.net_usd,
        avg_net_bps=avg_net,
        avg_adverse_bps=avg_adverse,
        verdict=_row_verdict(result, config),
        profit_factor=profit_factor,
        breakeven_bps=config.maker_bps + config.taker_bps + config.slippage_bps,
        exit_reason_counts=exit_reasons,
    )


def diagnose_events(
    events: list[tuple[int, str, object]],
    *,
    exchange: str,
    symbol: str,
    day: str,
    config: ReplaySweepConfig = ReplaySweepConfig(),
) -> ScalperReplayDiagnostics:
    stats = _stats(events)
    if stats.book_events == 0 or stats.trade_events == 0:
        return ScalperReplayDiagnostics(
            exchange=exchange,
            symbol=symbol,
            day=day,
            stats=stats,
            rows=(),
            primary_blocker="NO_TICK_DATA",
            action="start/repair the tick recorder; scalper research cannot run on candles",
        )

    fees = ReplayFees(
        maker_bps=config.maker_bps,
        taker_bps=config.taker_bps,
        slippage_bps=config.slippage_bps,
    )
    try:
        exit_policy = DEFAULT_SCALPER_PARAMETER_REGISTRY.exit_policy(
            config.exit_policy_id
        )
    except KeyError as exc:
        raise ValueError(f"unknown exit_policy_id: {config.exit_policy_id}") from exc
    runner = TickReplayBacktester(
        fees,
        notional_usd=config.notional_usd,
        exit_policy=exit_policy,
    )
    rows: list[ScalperReplayRow] = []
    for imb in config.min_imbalances:
        for spread in config.max_spread_bps:
            scalper = ImbalanceScalper(
                min_imbalance=imb,
                max_spread_bps=spread,
                ttl_ms=config.ttl_ms,
                stop_bps=config.stop_bps,
                target_bps=config.target_bps,
            )
            result = runner.run(events, scalper)
            rows.append(_row(imb, spread, result, config))

    if stats.span_seconds < config.min_span_seconds:
        blocker = "UNDER_SAMPLED_TICKS"
        action = "keep recording; do not infer edge from a short tick window"
    elif all(r.quotes == 0 for r in rows):
        blocker = "NO_QUOTES"
        action = "book/spread filters never qualified; inspect data quality before loosening"
    elif all(r.filled == 0 for r in rows):
        blocker = "NO_FILLS"
        action = "passive quotes are not getting conservative through-fills; do not trade"
    elif any(r.verdict == "CANDIDATE" for r in rows):
        blocker = "CANDIDATE_FOUND"
        action = "pre-register an untouched replay window before any shadow/paper exposure"
    elif any(r.verdict == "UNDER_SAMPLED_POSITIVE" for r in rows):
        blocker = "UNDER_SAMPLED_POSITIVE"
        action = "keep recording; positive sample is too thin for promotion"
    else:
        blocker = "NEGATIVE_EDGE_AFTER_COST"
        action = "do not force signals; current scalp shape does not clear maker/taker costs"

    return ScalperReplayDiagnostics(
        exchange=exchange,
        symbol=symbol,
        day=day,
        stats=stats,
        rows=tuple(rows),
        primary_blocker=blocker,
        action=action,
    )


def diagnose_recorded_day(
    data_root: Path | str,
    exchange: str,
    symbol: str,
    day: str,
    config: ReplaySweepConfig = ReplaySweepConfig(),
) -> ScalperReplayDiagnostics:
    events = load_tick_events(Path(data_root), exchange, symbol, day)
    return diagnose_events(events, exchange=exchange, symbol=symbol, day=day, config=config)


def _fmt(v: float | None, digits: int = 2) -> str:
    return "--" if v is None else f"{v:.{digits}f}"


def render_text_report(report: ScalperReplayDiagnostics) -> str:
    s = report.stats
    lines = [
        f"scalper replay diagnostics: {report.exchange} {report.symbol} {report.day}",
        (
            f"events={s.events} book={s.book_events} trades={s.trade_events} "
            f"span={s.span_seconds / 60:.1f}m spread_p50={_fmt(s.spread_bps_p50, 3)}bps "
            f"spread_p95={_fmt(s.spread_bps_p95, 3)}bps "
            f"|imb|_p90={_fmt(s.abs_imbalance_p90, 3)} "
            f"taker_buy={_fmt(s.taker_buy_ratio, 3)}"
        ),
        f"primary_blocker={report.primary_blocker}",
        f"action={report.action}",
    ]
    if report.rows:
        lines.append("")
        lines.append(
            "imb  spread  quotes fills fill%    net$ avg_net_bps "
            "pf avg_adv_bps policy exits verdict"
        )
        for r in sorted(report.rows, key=lambda x: (x.min_imbalance, x.max_spread_bps)):
            exits = (
                ",".join(
                    f"{reason}:{count}"
                    for reason, count in sorted((r.exit_reason_counts or {}).items())
                )
                or "--"
            )
            lines.append(
                f"{r.min_imbalance:>3.2f} {r.max_spread_bps:>6.1f} "
                f"{r.quotes:>7} {r.filled:>5} {r.fill_rate_pct:>5.1f} "
                f"{r.net_usd:>7.3f} {_fmt(r.avg_net_bps):>11} "
                f"{_fmt(r.profit_factor):>4} "
                f"{_fmt(r.avg_adverse_bps):>11} {r.exit_policy_id} "
                f"{exits} {r.verdict}"
            )
    best = report.best_row
    if best is not None:
        lines.append("")
        lines.append(
            "best="
            f"imb>={best.min_imbalance:.2f} spread<={best.max_spread_bps:.1f} "
            f"quotes={best.quotes} fills={best.filled} net=${best.net_usd:+.3f} "
            f"policy={best.exit_policy_id} verdict={best.verdict}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="diagnose recorded tick replay scalper blockers")
    p.add_argument("--data-root", default="data")
    p.add_argument("--exchange", default="binanceusdm")
    p.add_argument("--symbol", default="BTC/USDT:USDT")
    p.add_argument("--day", required=True, help="UTC day in YYYYMMDD")
    p.add_argument(
        "--family-id",
        default=REFERENCE_REPLAY_FAMILY_ID,
        choices=sorted(DEFAULT_SCALPER_PARAMETER_REGISTRY.families),
    )
    p.add_argument(
        "--exit-policy",
        default=None,
        choices=sorted(DEFAULT_SCALPER_PARAMETER_REGISTRY.exit_policies),
        help="override the family exit policy for replay comparison",
    )
    p.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = p.parse_args(argv)
    config = ReplaySweepConfig.from_registry(
        exchange=args.exchange,
        family_id=args.family_id,
    )
    if args.exit_policy:
        policy = DEFAULT_SCALPER_PARAMETER_REGISTRY.exit_policy(args.exit_policy)
        config = replace(
            config,
            exit_policy_id=policy.policy_id,
            ttl_ms=policy.ttl_ms,
            stop_bps=policy.stop_bps,
            target_bps=policy.target_bps,
        )
    report = diagnose_recorded_day(
        args.data_root,
        args.exchange,
        args.symbol,
        args.day,
        config=config,
    )
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        print(render_text_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
