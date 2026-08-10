"""Research-only alpha factory.

The factory is the layer that hunts for structural edge families before a
strategy exists. It does not place orders, promote lanes, or bless a signal.
It ranks hypotheses by conditional forward expectancy after realistic maker
costs, then queues the best ones for conservative tick replay.
"""

from __future__ import annotations

import argparse
import bisect
import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import mean
from typing import Any, Iterable, Literal, Mapping

import pandas as pd

from vnedge.research.scalper_context import (
    ScalperContextStack,
    build_context_stack,
    load_context_candles,
)
from vnedge.research.scalper_replay_diagnostics import ScalperReplayRow
from vnedge.research.scalper_scanners import (
    ExecutionRouteDecision,
    ScalperScannerConfig,
    decide_execution_route,
)
from vnedge.research.universe import ResearchTarget, load_research_targets
from vnedge.scalping.features import IncrementalFeatureEngine, ScalperFeatures
from vnedge.scalping.microstructure import TopOfBook
from vnedge.scalping.parameter_registry import DEFAULT_SCALPER_PARAMETER_REGISTRY
from vnedge.scalping.replay_backtester import load_tick_events


ALPHA_FACTORY_ID = "structural_alpha_factory_v1"
ALPHA_TOURNAMENT_ID = "event_scalper_alpha_tournament_v1"
ALPHA_FACTORY_FLOW = (
    "record_tick_l2",
    "mine_structural_hypotheses",
    "score_after_cost",
    "event_family_tournament",
    "route_policy",
    "conservative_replay_required",
    "untouched_judgment",
    "paper_shadow_after_human_approval",
)
_ALPHA_DEFAULTS = DEFAULT_SCALPER_PARAMETER_REGISTRY.alpha_factory_kwargs()


@dataclass(frozen=True)
class AlphaFactoryConfig:
    scanner_config: ScalperScannerConfig = field(default_factory=ScalperScannerConfig)
    horizons_ms: tuple[int, ...] = _ALPHA_DEFAULTS["horizons_ms"]
    context_timeframes: tuple[str, ...] = _ALPHA_DEFAULTS["context_timeframes"]
    context_enabled: bool = True
    sample_every_ms: int = _ALPHA_DEFAULTS["sample_every_ms"]
    min_samples: int = _ALPHA_DEFAULTS["min_samples"]
    max_spread_bps: float = _ALPHA_DEFAULTS["max_spread_bps"]
    min_trade_count: int = _ALPHA_DEFAULTS["min_trade_count"]
    min_abs_imbalance: float = _ALPHA_DEFAULTS["min_abs_imbalance"]
    flow_agreement: float = _ALPHA_DEFAULTS["flow_agreement"]
    min_pressure_notional_usd: float = _ALPHA_DEFAULTS["min_pressure_notional_usd"]
    microprice_dislocation_bps: float = _ALPHA_DEFAULTS["microprice_dislocation_bps"]
    liquidity_vacuum_depth_usd: float = _ALPHA_DEFAULTS["liquidity_vacuum_depth_usd"]
    min_realized_vol_bps: float = _ALPHA_DEFAULTS["min_realized_vol_bps"]
    maker_bps: float = _ALPHA_DEFAULTS["maker_bps"]
    taker_bps: float = _ALPHA_DEFAULTS["taker_bps"]
    slippage_bps: float = _ALPHA_DEFAULTS["slippage_bps"]
    safety_buffer_bps: float = _ALPHA_DEFAULTS["safety_buffer_bps"]
    notional_usd: float = 100.0


@dataclass(frozen=True)
class AlphaObservation:
    ts_ms: int
    side: Literal["buy", "sell"]
    forward_bps: float
    net_bps: float
    context_tag: str = "ungated"


@dataclass(frozen=True)
class AlphaHypothesisResult:
    alpha_factory_id: str
    exchange: str
    symbol: str
    day: str
    hypothesis_id: str
    family: str
    context_tag: str
    side: str
    horizon_ms: int
    samples: int
    avg_forward_bps: float | None
    avg_net_bps: float | None
    win_rate_pct: float
    profit_factor: float | None
    route_decision: ExecutionRouteDecision
    state: str
    cost_bps: float
    replay_priority: float
    rationale: str
    evidence: dict
    context_score: float | None = None
    context_summary: dict | None = None
    can_trade: bool = False
    can_promote: bool = False
    execution_evidence: str = "hypothesis_only"
    fill_assumption: str = "synthetic_observation_fill_not_replay"
    requires_conservative_replay: bool = True
    requires_untouched_judgment: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class _Point:
    ts_ms: int
    top: TopOfBook
    features: ScalperFeatures


@dataclass(frozen=True)
class _Signal:
    family: str
    side: Literal["buy", "sell"]
    rationale: str
    evidence: dict

    @property
    def label(self) -> str:
        return self.family


def alpha_factory_policy() -> dict:
    return {
        "status": "research_only",
        "can_trade": False,
        "can_promote": False,
        "requires_conservative_replay": True,
        "requires_untouched_judgment": True,
        "alpha_factory_id": ALPHA_FACTORY_ID,
        "principle": (
            "raw structural hypotheses are not signals; they must survive "
            "conservative tick replay, untouched judgment, and human approval"
        ),
        "families": [
            "forced_flow_continuation",
            "absorption_reversal",
            "microprice_dislocation",
            "liquidity_vacuum_continuation",
            "volatility_impulse",
        ],
        "context_timeframes": list(DEFAULT_SCALPER_PARAMETER_REGISTRY.context_timeframes),
        "context_principle": (
            "4h/1h/15m/1m are mined as context tags, not as direct scalp entries"
        ),
    }


def run_alpha_factory(
    data_root: Path | str,
    targets: tuple[ResearchTarget, ...],
    *,
    days: tuple[str, ...],
    config: AlphaFactoryConfig = AlphaFactoryConfig(),
    max_rows: int = 50,
) -> dict:
    """Run structural alpha discovery over already recorded tick/L2 tape."""
    root = Path(data_root)
    hypotheses = mine_recorded_alpha_days(root, targets, days, config)
    tournament = build_alpha_tournament(hypotheses, max_rows=max_rows)
    replay_queue = [
        {**h.to_dict(), "source": "alpha_factory_structural_hypothesis"}
        for h in hypotheses
        if h.state in {"REPLAY_REQUIRED_MAKER", "REPLAY_REQUIRED_TAKER"}
    ][:max_rows]
    payload = {
        "policy": alpha_factory_policy(),
        "flow": list(ALPHA_FACTORY_FLOW),
        "flow_guards": {
            "raw_hypothesis_is_not_signal": True,
            "conservative_replay_required": True,
            "untouched_judgment_required": True,
            "human_approval_required": True,
            "can_trade": False,
            "can_promote": False,
        },
        "targets": [asdict(t) for t in targets],
        "days": list(days),
        "context_mining": {
            "enabled": config.context_enabled,
            "timeframes": list(config.context_timeframes),
            "purpose": "test whether candle context improves L2 hypothesis expectancy",
        },
        "hypotheses": [h.to_dict() for h in hypotheses[:max_rows]],
        "tournament": tournament,
        "replay_queue": replay_queue,
        "recorder_directives": _recorder_directives(targets, days, hypotheses),
    }
    if not days:
        payload["note"] = "no tick/L2 days supplied; record public tape before alpha mining"
    return payload


def mine_recorded_alpha_days(
    data_root: Path | str,
    targets: Iterable[ResearchTarget],
    days: Iterable[str],
    config: AlphaFactoryConfig = AlphaFactoryConfig(),
) -> tuple[AlphaHypothesisResult, ...]:
    out: list[AlphaHypothesisResult] = []
    for target in targets:
        context_candles = (
            load_context_candles(
                data_root,
                target.exchange,
                target.symbol,
                config.context_timeframes,
            )
            if config.context_enabled else {}
        )
        for day in days:
            events = load_tick_events(data_root, target.exchange, target.symbol, day)
            out.extend(
                mine_structural_alpha_events(
                    events,
                    exchange=target.exchange,
                    symbol=target.symbol,
                    day=day,
                    config=config,
                    context_candles=context_candles,
                )
            )
    return tuple(sorted(out, key=_result_sort_key))


def mine_structural_alpha_events(
    events: list[tuple[int, str, object]],
    *,
    exchange: str,
    symbol: str,
    day: str,
    config: AlphaFactoryConfig = AlphaFactoryConfig(),
    context_candles: Mapping[str, pd.DataFrame] | None = None,
) -> tuple[AlphaHypothesisResult, ...]:
    points = _feature_points(events)
    if len(points) < 2:
        return ()
    timestamps = [p.ts_ms for p in points]
    mids = [p.top.mid_price for p in points]
    observations: dict[tuple[str, str, int, str], list[AlphaObservation]] = {}
    evidence: dict[tuple[str, str, int, str], dict] = {}
    last_sample: dict[tuple[str, str, int, str], int] = {}
    context_cache: dict[int, ScalperContextStack] = {}

    for i, point in enumerate(points):
        context_stack = None
        if config.context_enabled and context_candles:
            context_bucket = point.ts_ms // 60_000
            context_stack = context_cache.get(context_bucket)
            if context_stack is None:
                context_stack = build_context_stack(
                    context_candles,
                    at_ms=point.ts_ms,
                    timeframes=config.context_timeframes,
                )
                context_cache[context_bucket] = context_stack
        for signal in _signals(point, config):
            context_tag = "ungated"
            context_summary = None
            if context_stack is not None:
                context_tag = context_stack.tag_for_side(signal.side)
                context_summary = context_stack.summary_for_side(signal.side)
            for horizon_ms in config.horizons_ms:
                key = (signal.label, signal.side, horizon_ms, context_tag)
                if point.ts_ms - last_sample.get(key, -10**15) < config.sample_every_ms:
                    continue
                j = bisect.bisect_left(timestamps, point.ts_ms + horizon_ms, lo=i + 1)
                if j >= len(points):
                    continue
                forward = (mids[j] - point.top.mid_price) / point.top.mid_price * 10_000.0
                signed = forward if signal.side == "buy" else -forward
                net = signed - _maker_replay_cost_bps(config)
                observations.setdefault(key, []).append(
                    AlphaObservation(point.ts_ms, signal.side, signed, net, context_tag)
                )
                row_evidence = dict(signal.evidence)
                if context_summary is not None:
                    row_evidence["context"] = context_summary
                evidence.setdefault(key, row_evidence)
                last_sample[key] = point.ts_ms

    results = [
        _result(exchange, symbol, day, key, obs, evidence.get(key, {}), config)
        for key, obs in observations.items()
    ]
    return tuple(sorted(results, key=_result_sort_key))


def build_alpha_tournament(
    hypotheses: Iterable[AlphaHypothesisResult | Mapping[str, Any] | Any],
    *,
    max_rows: int = 50,
) -> dict:
    """Rank event-driven scalp families by replay readiness after costs.

    The tournament deliberately consumes already-mined hypotheses. It does not
    create new signals; it decides which family/lane/context deserves scarce
    conservative replay next.
    """
    registry = DEFAULT_SCALPER_PARAMETER_REGISTRY
    active_ids = {family.family_id for family in registry.active_research_families()}
    rows = [
        row for row in (_hypothesis_payload(h) for h in hypotheses)
        if row.get("family") in active_ids
    ]
    groups: dict[tuple[str, str, str, str], list[dict]] = {}
    for row in rows:
        key = (
            str(row.get("family", "")),
            str(row.get("exchange", "")),
            str(row.get("symbol", "")),
            str(row.get("context_tag", "ungated")),
        )
        groups.setdefault(key, []).append(row)

    standings = [
        _tournament_entry(key, group)
        for key, group in groups.items()
        if group
    ]
    standings.sort(key=_tournament_sort_key)
    ranked_standings = [
        {**entry, "rank": rank}
        for rank, entry in enumerate(standings[:max_rows], start=1)
    ]
    replay_queue = [
        entry for entry in ranked_standings
        if entry["decision"] in {"REPLAY_TAKER_CANDIDATE", "REPLAY_MAKER_CANDIDATE"}
    ]
    return {
        "tournament_id": ALPHA_TOURNAMENT_ID,
        "policy": _tournament_policy(),
        "summary": _tournament_summary(standings, replay_queue),
        "family_standings": _family_standings(standings, max_rows=max_rows),
        "standings": ranked_standings,
        "replay_queue": replay_queue[:max_rows],
        "can_trade": False,
        "can_promote": False,
    }


def render_alpha_report(results: Iterable[AlphaHypothesisResult], *, limit: int = 40) -> str:
    lines = [
        "alpha factory",
        "policy=research_only can_trade=false can_promote=false",
        "",
        "state                 route         side context  pf    net_bps fwd_bps win% "
        "samples priority exchange    symbol          horizon family",
    ]
    for r in tuple(results)[:limit]:
        lines.append(
            f"{r.state:<21} {r.route_decision.route:<13} {r.side:<4} "
            f"{r.context_tag:<8} {_fmt(r.profit_factor):>5} "
            f"{_fmt(r.avg_net_bps):>7} {_fmt(r.avg_forward_bps):>7} "
            f"{r.win_rate_pct:>4.0f} {r.samples:>7} "
            f"{r.replay_priority:>8.1f} {r.exchange:<11} "
            f"{r.symbol:<15} {r.horizon_ms:>7} {r.family}"
        )
    if len(lines) == 4:
        lines.append("no structural hypotheses found; record more tick/L2 data")
    return "\n".join(lines)


def _tournament_entry(
    key: tuple[str, str, str, str],
    group: list[dict],
) -> dict:
    family, exchange, symbol, context_tag = key
    group = sorted(group, key=_payload_sort_key)
    best = group[0]
    state_counts = Counter(str(row.get("state", "UNKNOWN")) for row in group)
    route_counts = Counter(_payload_route(row) for row in group)
    decision = _tournament_decision(best, state_counts)
    return {
        "family": family,
        "exchange": exchange,
        "symbol": symbol,
        "context_tag": context_tag,
        "decision": decision,
        "next_step": _tournament_next_step(decision),
        "score": _tournament_score(best, state_counts, route_counts),
        "best_hypothesis_id": best.get("hypothesis_id"),
        "best_state": best.get("state"),
        "route_decision": _payload_route(best),
        "best_side": best.get("side"),
        "best_horizon_ms": best.get("horizon_ms"),
        "best_avg_net_bps": _round_or_none(best.get("avg_net_bps")),
        "best_avg_forward_bps": _round_or_none(best.get("avg_forward_bps")),
        "best_profit_factor": _round_or_none(best.get("profit_factor")),
        "best_replay_priority": _round_or_none(best.get("replay_priority")),
        "best_samples": int(best.get("samples", 0) or 0),
        "total_samples": sum(int(row.get("samples", 0) or 0) for row in group),
        "hypotheses": len(group),
        "state_counts": dict(state_counts),
        "route_counts": dict(route_counts),
        "route_gap": _route_gap(best),
        "cost_bps": _round_or_none(best.get("cost_bps")),
        "evidence": best.get("evidence") or {},
        "can_trade": False,
        "can_promote": False,
        "requires_conservative_replay": True,
        "requires_untouched_judgment": True,
        "requires_human_approval": True,
    }


def _tournament_policy() -> dict:
    registry = DEFAULT_SCALPER_PARAMETER_REGISTRY
    return {
        "status": "research_only",
        "can_trade": False,
        "can_promote": False,
        "tournament_id": ALPHA_TOURNAMENT_ID,
        "active_research_families": [
            family.family_id for family in registry.active_research_families()
        ],
        "tombstoned_families": [
            {
                "family_id": family.family_id,
                "evidence": family.evidence,
            }
            for family in registry.tombstoned_families()
        ],
        "decision_rule": (
            "rank active event families by post-cost net bps, PF, route decision, "
            "and sample count; only replay-required rows enter the replay queue"
        ),
        "hard_guards": [
            "no raw hypothesis is a signal",
            "blocked fee-wall rows do not trade",
            "replay queue still requires conservative replay",
            "untouched judgment and human approval remain mandatory",
        ],
    }


def _tournament_summary(standings: list[dict], replay_queue: list[dict]) -> dict:
    decisions = Counter(row["decision"] for row in standings)
    routes = Counter(row["route_decision"] for row in standings)
    return {
        "lanes": len(standings),
        "replay_queue": len(replay_queue),
        "families": len({row["family"] for row in standings}),
        "replay_taker_candidates": decisions.get("REPLAY_TAKER_CANDIDATE", 0),
        "replay_maker_candidates": decisions.get("REPLAY_MAKER_CANDIDATE", 0),
        "record_more": decisions.get("RECORD_MORE", 0),
        "blocked_fee_wall": decisions.get("BLOCKED_FEE_WALL", 0),
        "maker_only": routes.get("MAKER_ONLY", 0),
        "taker_allowed": routes.get("TAKER_ALLOWED", 0),
        "blocked": routes.get("BLOCKED", 0),
        "can_trade": False,
        "can_promote": False,
    }


def _family_standings(standings: list[dict], *, max_rows: int) -> list[dict]:
    by_family: dict[str, list[dict]] = {}
    for row in standings:
        by_family.setdefault(row["family"], []).append(row)
    out = []
    for family, rows in by_family.items():
        rows = sorted(rows, key=_tournament_sort_key)
        best = rows[0]
        decisions = Counter(row["decision"] for row in rows)
        out.append({
            "family": family,
            "lanes": len(rows),
            "best_decision": best["decision"],
            "best_exchange": best["exchange"],
            "best_symbol": best["symbol"],
            "best_context_tag": best["context_tag"],
            "best_score": best["score"],
            "best_avg_net_bps": best["best_avg_net_bps"],
            "best_profit_factor": best["best_profit_factor"],
            "replay_candidates": (
                decisions.get("REPLAY_TAKER_CANDIDATE", 0)
                + decisions.get("REPLAY_MAKER_CANDIDATE", 0)
            ),
            "record_more": decisions.get("RECORD_MORE", 0),
            "blocked_fee_wall": decisions.get("BLOCKED_FEE_WALL", 0),
            "can_trade": False,
        })
    out.sort(key=lambda row: (
        _decision_rank(row["best_decision"]),
        -float(row["best_score"] or 0.0),
        row["family"],
    ))
    return out[:max_rows]


def _tournament_decision(best: dict, state_counts: Counter) -> str:
    state = str(best.get("state", ""))
    if state == "REPLAY_REQUIRED_TAKER":
        return "REPLAY_TAKER_CANDIDATE"
    if state == "REPLAY_REQUIRED_MAKER":
        return "REPLAY_MAKER_CANDIDATE"
    if state_counts.get("UNDER_SAMPLED", 0) > 0:
        return "RECORD_MORE"
    return "BLOCKED_FEE_WALL"


def _tournament_next_step(decision: str) -> str:
    return {
        "REPLAY_TAKER_CANDIDATE": "run_conservative_replay_with_taker_route_check",
        "REPLAY_MAKER_CANDIDATE": "run_conservative_replay_maker_first",
        "RECORD_MORE": "extend_tick_l2_recording_before_judgment",
        "BLOCKED_FEE_WALL": "do_not_trade_or_replay_until_new_premise",
    }.get(decision, "hold_research_only")


def _tournament_score(
    best: dict,
    state_counts: Counter,
    route_counts: Counter,
) -> float:
    avg_net = _maybe_float(best.get("avg_net_bps")) or 0.0
    pf = _maybe_float(best.get("profit_factor")) or 0.0
    priority = _maybe_float(best.get("replay_priority")) or 0.0
    samples = int(best.get("samples", 0) or 0)
    score = 0.0
    score += min(max(priority, 0.0), 100.0) * 0.45
    score += min(max(avg_net, 0.0), 20.0) * 2.0
    score += min(max(pf - 1.0, 0.0), 4.0) * 7.0
    score += min(samples, 500) / 500.0 * 10.0
    score += route_counts.get("TAKER_ALLOWED", 0) * 2.0
    score += route_counts.get("MAKER_ONLY", 0) * 1.0
    score += state_counts.get("UNDER_SAMPLED", 0) * 0.1
    return round(score, 2)


def _route_gap(row: dict) -> dict:
    route = row.get("route_decision") or {}
    if not isinstance(route, Mapping):
        route = {}
    avg = _maybe_float(row.get("avg_net_bps"))
    pf = _maybe_float(row.get("profit_factor")) or _maybe_float(
        route.get("observed_profit_factor")
    )
    maker_floor = _maybe_float(route.get("maker_breakeven_bps"))
    taker_floor = _maybe_float(route.get("taker_breakeven_bps"))
    maker_pf = _maybe_float(route.get("maker_min_profit_factor"))
    taker_pf = _maybe_float(route.get("taker_min_profit_factor"))
    return {
        "avg_net_bps": _round_or_none(avg),
        "profit_factor": _round_or_none(pf),
        "maker_net_gap_bps": _round_or_none(
            None if avg is None or maker_floor is None else avg - maker_floor
        ),
        "taker_net_gap_bps": _round_or_none(
            None if avg is None or taker_floor is None else avg - taker_floor
        ),
        "maker_pf_gap": _round_or_none(
            None if pf is None or maker_pf is None else pf - maker_pf
        ),
        "taker_pf_gap": _round_or_none(
            None if pf is None or taker_pf is None else pf - taker_pf
        ),
    }


def _hypothesis_payload(value: AlphaHypothesisResult | Mapping[str, Any] | Any) -> dict:
    if isinstance(value, AlphaHypothesisResult):
        return value.to_dict()
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _payload_route(row: Mapping[str, Any]) -> str:
    route = row.get("route_decision") or {}
    if isinstance(route, Mapping):
        return str(route.get("route", "BLOCKED"))
    return "BLOCKED"


def _payload_sort_key(row: Mapping[str, Any]) -> tuple:
    return (
        _state_rank(str(row.get("state", ""))),
        _route_rank(_payload_route(row)),
        -(_maybe_float(row.get("replay_priority")) or 0.0),
        -(_maybe_float(row.get("avg_net_bps")) or -999.0),
        -int(row.get("samples", 0) or 0),
        str(row.get("hypothesis_id", "")),
    )


def _tournament_sort_key(row: Mapping[str, Any]) -> tuple:
    return (
        _decision_rank(str(row.get("decision", ""))),
        _route_rank(str(row.get("route_decision", "BLOCKED"))),
        -(_maybe_float(row.get("score")) or 0.0),
        -(_maybe_float(row.get("best_avg_net_bps")) or -999.0),
        -int(row.get("best_samples", 0) or 0),
        str(row.get("family", "")),
        str(row.get("exchange", "")),
        str(row.get("symbol", "")),
    )


def _state_rank(state: str) -> int:
    return {
        "REPLAY_REQUIRED_TAKER": 0,
        "REPLAY_REQUIRED_MAKER": 1,
        "UNDER_SAMPLED": 2,
        "BELOW_COST": 3,
    }.get(state, 9)


def _decision_rank(decision: str) -> int:
    return {
        "REPLAY_TAKER_CANDIDATE": 0,
        "REPLAY_MAKER_CANDIDATE": 1,
        "RECORD_MORE": 2,
        "BLOCKED_FEE_WALL": 3,
    }.get(decision, 9)


def _route_rank(route: str) -> int:
    return {"TAKER_ALLOWED": 0, "MAKER_ONLY": 1, "BLOCKED": 2}.get(route, 9)


def _feature_points(events: list[tuple[int, str, object]]) -> list[_Point]:
    engine = IncrementalFeatureEngine(max_midpoints=240, max_trades=300)
    points: list[_Point] = []
    for ts_ms, kind, obj in events:
        if kind == "trade":
            engine.on_trade(obj)
        elif kind == "book" and isinstance(obj, TopOfBook):
            features = engine.on_book(obj)
            points.append(_Point(ts_ms, obj, features))
    return points


def _signals(point: _Point, config: AlphaFactoryConfig) -> tuple[_Signal, ...]:
    top = point.top
    features = point.features
    if (
        top.spread_bps > config.max_spread_bps
        or features.trade_count < config.min_trade_count
    ):
        return ()

    out: list[_Signal] = []
    imb = features.book_imbalance
    buy_ratio = features.taker_buy_ratio
    pressure = features.signed_trade_notional_usd
    micro_bps = (features.microprice - features.mid_price) / features.mid_price * 10_000.0
    evidence = {
        "spread_bps": round(top.spread_bps, 4),
        "imbalance": round(imb, 4),
        "taker_buy_ratio": round(buy_ratio, 4),
        "signed_trade_notional_usd": round(pressure, 2),
        "microprice_bps": round(micro_bps, 4),
        "top_depth_usd": round(top.top_depth_usd, 2),
        "realized_vol_bps": round(features.realized_vol_bps, 4),
    }

    if (
        pressure >= config.min_pressure_notional_usd
        and buy_ratio >= config.flow_agreement
        and imb >= config.min_abs_imbalance
    ):
        out.append(_Signal("forced_flow_continuation", "buy",
                           "aggressive buy flow agrees with bid-side pressure", evidence))
    if (
        pressure <= -config.min_pressure_notional_usd
        and buy_ratio <= 1.0 - config.flow_agreement
        and imb <= -config.min_abs_imbalance
    ):
        out.append(_Signal("forced_flow_continuation", "sell",
                           "aggressive sell flow agrees with ask-side pressure", evidence))

    if (
        pressure <= -config.min_pressure_notional_usd
        and buy_ratio <= 1.0 - config.flow_agreement
        and imb >= config.min_abs_imbalance
    ):
        out.append(_Signal("absorption_reversal", "buy",
                           "bid-side liquidity absorbs sell pressure", evidence))
    if (
        pressure >= config.min_pressure_notional_usd
        and buy_ratio >= config.flow_agreement
        and imb <= -config.min_abs_imbalance
    ):
        out.append(_Signal("absorption_reversal", "sell",
                           "ask-side liquidity absorbs buy pressure", evidence))

    if micro_bps >= config.microprice_dislocation_bps and imb > 0:
        out.append(_Signal("microprice_dislocation", "buy",
                           "microprice displaced above mid with supportive book", evidence))
    if micro_bps <= -config.microprice_dislocation_bps and imb < 0:
        out.append(_Signal("microprice_dislocation", "sell",
                           "microprice displaced below mid with supportive book", evidence))

    if (
        top.top_depth_usd <= config.liquidity_vacuum_depth_usd
        and features.realized_vol_bps >= config.min_realized_vol_bps
    ):
        if pressure >= config.min_pressure_notional_usd and buy_ratio >= config.flow_agreement:
            out.append(_Signal("liquidity_vacuum_continuation", "buy",
                               "thin touch plus aggressive buy impulse", evidence))
        if pressure <= -config.min_pressure_notional_usd and buy_ratio <= 1 - config.flow_agreement:
            out.append(_Signal("liquidity_vacuum_continuation", "sell",
                               "thin touch plus aggressive sell impulse", evidence))

    if features.realized_vol_bps >= config.min_realized_vol_bps * 2:
        if pressure >= config.min_pressure_notional_usd and buy_ratio >= config.flow_agreement:
            out.append(_Signal("volatility_impulse", "buy",
                               "volatility expansion with one-sided buy flow", evidence))
        if pressure <= -config.min_pressure_notional_usd and buy_ratio <= 1 - config.flow_agreement:
            out.append(_Signal("volatility_impulse", "sell",
                               "volatility expansion with one-sided sell flow", evidence))

    return tuple(out)


def _result(
    exchange: str,
    symbol: str,
    day: str,
    key: tuple[str, str, int, str],
    observations: list[AlphaObservation],
    evidence: dict,
    config: AlphaFactoryConfig,
) -> AlphaHypothesisResult:
    family, side, horizon_ms, context_tag = key
    net = [o.net_bps for o in observations]
    forward = [o.forward_bps for o in observations]
    wins = [v for v in net if v > 0]
    losses = [-v for v in net if v < 0]
    pf = (sum(wins) / sum(losses)) if wins and losses else (999.0 if wins else None)
    avg_net = mean(net) if net else None
    avg_forward = mean(forward) if forward else None
    win_rate = len(wins) / len(net) * 100.0 if net else 0.0
    exit_policy_id = DEFAULT_SCALPER_PARAMETER_REGISTRY.family(family).exit_policy_id
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
        avg_adverse_bps=0.0 if observations else None,
        verdict="HYPOTHESIS_ONLY" if avg_net and avg_net > 0 else "NEGATIVE_EDGE",
        profit_factor=pf,
        breakeven_bps=_maker_replay_cost_bps(config),
        family_id=family,
        exit_policy_id=exit_policy_id,
    )
    route = decide_execution_route(hypothesis_row, config.scanner_config)
    state = _alpha_state(len(observations), route, config)
    context_summary = evidence.get("context")
    context_score = None
    if isinstance(context_summary, dict):
        raw_score = context_summary.get("score")
        context_score = float(raw_score) if raw_score is not None else None
    return AlphaHypothesisResult(
        alpha_factory_id=ALPHA_FACTORY_ID,
        exchange=exchange,
        symbol=symbol,
        day=day,
        hypothesis_id=f"{family}|side={side}|h={horizon_ms}|context={context_tag}",
        family=family,
        context_tag=context_tag,
        side=side,
        horizon_ms=horizon_ms,
        samples=len(observations),
        avg_forward_bps=avg_forward,
        avg_net_bps=avg_net,
        win_rate_pct=win_rate,
        profit_factor=pf,
        route_decision=route,
        state=state,
        cost_bps=_maker_replay_cost_bps(config),
        replay_priority=_replay_priority(len(observations), avg_net, pf, route, config),
        rationale=_rationale(family, route, avg_net),
        evidence=evidence,
        context_score=context_score,
        context_summary=context_summary,
    )


def _alpha_state(
    samples: int,
    route: ExecutionRouteDecision,
    config: AlphaFactoryConfig,
) -> str:
    if samples < config.min_samples:
        return "UNDER_SAMPLED"
    if route.route == "TAKER_ALLOWED":
        return "REPLAY_REQUIRED_TAKER"
    if route.route == "MAKER_ONLY":
        return "REPLAY_REQUIRED_MAKER"
    return "BELOW_COST"


def _replay_priority(
    samples: int,
    avg_net: float | None,
    pf: float | None,
    route: ExecutionRouteDecision,
    config: AlphaFactoryConfig,
) -> float:
    sample_score = min(samples / max(config.min_samples, 1), 3.0) * 15.0
    net_score = max(avg_net or 0.0, 0.0) * 6.0
    pf_score = min(pf or 0.0, 5.0) * 8.0
    route_bonus = 0.0
    if route.route == "TAKER_ALLOWED":
        route_bonus = 20.0
    elif route.route == "MAKER_ONLY":
        route_bonus = 10.0
    return round(sample_score + net_score + pf_score + route_bonus, 2)


def _rationale(family: str, route: ExecutionRouteDecision, avg_net: float | None) -> str:
    if route.route == "BLOCKED":
        return f"{family} does not clear maker cost/PF floor yet"
    return (
        f"{family} has positive conditional expectancy after maker-first costs "
        f"({_fmt(avg_net)}bps avg net); route={route.route}; replay before promotion"
    )


def _maker_replay_cost_bps(config: AlphaFactoryConfig) -> float:
    return config.maker_bps + config.taker_bps + config.slippage_bps + config.safety_buffer_bps


def _recorder_directives(
    targets: tuple[ResearchTarget, ...],
    days: tuple[str, ...],
    hypotheses: tuple[AlphaHypothesisResult, ...],
) -> list[dict]:
    if not days:
        return [
            {
                "exchange": t.exchange,
                "symbol": t.symbol,
                "reason": "no recorded tick/L2 day available",
                "priority": 100.0,
            }
            for t in targets
        ]
    if not hypotheses:
        return [
            {
                "exchange": t.exchange,
                "symbol": t.symbol,
                "reason": "record more tape; no structural hypothesis fired",
                "priority": 60.0,
            }
            for t in targets
        ]
    under_sampled = [h for h in hypotheses if h.state == "UNDER_SAMPLED"]
    return [
        {
            "exchange": h.exchange,
            "symbol": h.symbol,
            "reason": f"{h.family} fired but sample count is thin",
            "priority": min(100.0, 50.0 + h.samples),
        }
        for h in under_sampled[:12]
    ]


def _result_sort_key(r: AlphaHypothesisResult) -> tuple[int, float, float, int, str]:
    state_rank = {
        "REPLAY_REQUIRED_TAKER": 0,
        "REPLAY_REQUIRED_MAKER": 1,
        "UNDER_SAMPLED": 2,
        "BELOW_COST": 3,
    }.get(r.state, 4)
    return (
        state_rank,
        -r.replay_priority,
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


def _maybe_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(out):
        return None
    return out


def _round_or_none(value: Any, digits: int = 4) -> float | None:
    out = _maybe_float(value)
    return None if out is None else round(out, digits)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="mine structural alpha hypotheses")
    p.add_argument("--data-root", default="data")
    p.add_argument("--days", required=True, help="comma-separated UTC days, YYYYMMDD")
    p.add_argument("--limit", type=int, default=40)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    targets = load_research_targets()
    results = mine_recorded_alpha_days(
        args.data_root,
        targets,
        _split_csv(args.days),
    )
    if args.json:
        print(json.dumps([r.to_dict() for r in results[:args.limit]], indent=2))
    else:
        print(render_alpha_report(results, limit=args.limit))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
