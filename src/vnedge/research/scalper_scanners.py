"""Scalper scanners.

This module answers the practical research question: which exchange/symbol
lanes deserve scarce tick-recorder and replay attention? It does not place
orders, promote strategies, or loosen the scalper to manufacture activity.

The scanner consumes the conservative replay diagnostics and ranks lanes by:
sample sufficiency, spread/liquidity, observed imbalance pressure, passive
fill evidence, adverse selection, and net edge after maker/taker/slippage
costs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

from vnedge.research.scalper_replay_diagnostics import (
    ReplaySweepConfig,
    ScalperReplayDiagnostics,
    ScalperReplayRow,
    TickSampleStats,
    diagnose_recorded_day,
)
from vnedge.research.universe import (
    DEFAULT_DERIVATIVE_QUOTES,
    DEFAULT_EXCHANGES,
    ResearchTarget,
    discover_research_targets,
    load_research_targets,
)
from vnedge.scalping.parameter_registry import DEFAULT_SCALPER_PARAMETER_REGISTRY


SCANNER_ID = "scalper_microstructure_scanner_v1"
_SCANNER_DEFAULTS = DEFAULT_SCALPER_PARAMETER_REGISTRY.scanner_gate_kwargs()


@dataclass(frozen=True)
class ScalperScannerConfig:
    replay_config: ReplaySweepConfig = field(default_factory=ReplaySweepConfig)
    min_sample_seconds: float = 6 * 3_600.0
    min_book_events: int = 5_000
    min_trade_events: int = 5_000
    max_spread_p95_bps: float = 2.0
    min_abs_imbalance_p90: float = 0.35
    min_fills: int = 20
    min_fill_rate_pct: float = _SCANNER_DEFAULTS["min_fill_rate_pct"]
    maker_min_profit_factor: float = _SCANNER_DEFAULTS["maker_min_profit_factor"]
    taker_min_profit_factor: float = _SCANNER_DEFAULTS["taker_min_profit_factor"]
    min_avg_net_bps: float = _SCANNER_DEFAULTS["min_avg_net_bps"]
    max_avg_adverse_bps: float = _SCANNER_DEFAULTS["max_avg_adverse_bps"]
    min_candidate_score: float = 85.0


@dataclass(frozen=True)
class ExecutionRouteDecision:
    route: str
    reason: str
    observed_profit_factor: float | None
    avg_net_bps: float | None
    maker_min_profit_factor: float
    taker_min_profit_factor: float
    maker_breakeven_bps: float
    taker_breakeven_bps: float


@dataclass(frozen=True)
class ScalperLaneScan:
    scanner_id: str
    exchange: str
    symbol: str
    day: str
    state: str
    primary_blocker: str
    recorder_priority: float
    edge_score: float
    gates: dict[str, bool]
    stats: TickSampleStats
    best_row: ScalperReplayRow | None
    route_decision: ExecutionRouteDecision
    next_action: str
    can_trade: bool = False
    can_promote: bool = False
    requires_untouched_judgment: bool = True

    def to_dict(self) -> dict:
        d = asdict(self)
        d["best_row"] = asdict(self.best_row) if self.best_row else None
        return d


def scan_diagnostics(
    report: ScalperReplayDiagnostics,
    config: ScalperScannerConfig = ScalperScannerConfig(),
) -> ScalperLaneScan:
    best = report.best_row
    gates = _gates(report.stats, best, config)
    route = decide_execution_route(best, config)
    edge_score = _edge_score(report.stats, best, gates, config)
    state = _state(report.primary_blocker, gates, edge_score, route, config)
    priority = _recorder_priority(report.stats, best, state, config)
    return ScalperLaneScan(
        scanner_id=SCANNER_ID,
        exchange=report.exchange,
        symbol=report.symbol,
        day=report.day,
        state=state,
        primary_blocker=report.primary_blocker,
        recorder_priority=priority,
        edge_score=edge_score,
        gates=gates,
        stats=report.stats,
        best_row=best,
        route_decision=route,
        next_action=_action(state, report.stats, best, route, config),
    )


def decide_execution_route(
    best: ScalperReplayRow | None,
    config: ScalperScannerConfig = ScalperScannerConfig(),
) -> ExecutionRouteDecision:
    maker_floor = config.min_avg_net_bps
    taker_floor = (
        config.min_avg_net_bps
        + max(config.replay_config.taker_bps - config.replay_config.maker_bps, 0.0)
    )
    if best is None or best.filled == 0:
        return ExecutionRouteDecision(
            route="BLOCKED",
            reason="no completed replay fills",
            observed_profit_factor=None,
            avg_net_bps=None,
            maker_min_profit_factor=config.maker_min_profit_factor,
            taker_min_profit_factor=config.taker_min_profit_factor,
            maker_breakeven_bps=maker_floor,
            taker_breakeven_bps=taker_floor,
        )
    pf = best.profit_factor or 0.0
    avg = best.avg_net_bps
    if avg is None or avg < maker_floor or pf < config.maker_min_profit_factor:
        return ExecutionRouteDecision(
            route="BLOCKED",
            reason=(
                "below maker breakeven/PF floor: "
                f"avg_net_bps={_fmt(avg)}, pf={_fmt(best.profit_factor)}"
            ),
            observed_profit_factor=best.profit_factor,
            avg_net_bps=avg,
            maker_min_profit_factor=config.maker_min_profit_factor,
            taker_min_profit_factor=config.taker_min_profit_factor,
            maker_breakeven_bps=maker_floor,
            taker_breakeven_bps=taker_floor,
        )
    if avg >= taker_floor and pf >= config.taker_min_profit_factor:
        return ExecutionRouteDecision(
            route="TAKER_ALLOWED",
            reason="PF and net bps clear the extra taker-entry cost",
            observed_profit_factor=best.profit_factor,
            avg_net_bps=avg,
            maker_min_profit_factor=config.maker_min_profit_factor,
            taker_min_profit_factor=config.taker_min_profit_factor,
            maker_breakeven_bps=maker_floor,
            taker_breakeven_bps=taker_floor,
        )
    return ExecutionRouteDecision(
        route="MAKER_ONLY",
        reason="edge clears maker floor but not taker PF/cost floor",
        observed_profit_factor=best.profit_factor,
        avg_net_bps=avg,
        maker_min_profit_factor=config.maker_min_profit_factor,
        taker_min_profit_factor=config.taker_min_profit_factor,
        maker_breakeven_bps=maker_floor,
        taker_breakeven_bps=taker_floor,
    )


def scan_recorded_days(
    data_root: Path | str,
    targets: Iterable[ResearchTarget],
    days: Iterable[str],
    config: ScalperScannerConfig = ScalperScannerConfig(),
) -> tuple[ScalperLaneScan, ...]:
    scans: list[ScalperLaneScan] = []
    for target in targets:
        for day in days:
            report = diagnose_recorded_day(
                data_root,
                target.exchange,
                target.symbol,
                day,
                config.replay_config,
            )
            scans.append(scan_diagnostics(report, config))
    return tuple(sorted(scans, key=_scan_sort_key))


def select_recorder_targets(
    scans: Iterable[ScalperLaneScan],
    *,
    limit: int = 12,
) -> tuple[ScalperLaneScan, ...]:
    """Return the unique exchange/symbol lanes worth recording next."""
    best: dict[tuple[str, str], ScalperLaneScan] = {}
    for scan in scans:
        if scan.state in {"REJECTED_COST_WALL", "REJECTED_LIQUIDITY"}:
            continue
        key = (scan.exchange, scan.symbol)
        prev = best.get(key)
        if prev is None or _scan_sort_key(scan) < _scan_sort_key(prev):
            best[key] = scan
    ranked = sorted(best.values(), key=_scan_sort_key)
    return tuple(ranked[:limit])


def render_scanner_report(scans: Iterable[ScalperLaneScan], *, limit: int = 50) -> str:
    scans = tuple(scans)
    lines = [
        "scalper scanner report",
        "policy=research_only can_trade=false can_promote=false",
        "",
        "prio edge state                  exchange    symbol          day      "
        "spread95 fills fill% pf  route         avg_net action",
    ]
    for scan in scans[:limit]:
        stats = scan.stats
        best = scan.best_row
        lines.append(
            f"{scan.recorder_priority:>5.1f} {scan.edge_score:>4.0f} "
            f"{scan.state:<22} {scan.exchange:<11} {scan.symbol:<15} {scan.day:<8} "
            f"{_fmt(stats.spread_bps_p95, 3):>8} "
            f"{(best.filled if best else 0):>5} "
            f"{_fmt(best.fill_rate_pct if best else None, 1):>5} "
            f"{_fmt(best.profit_factor if best else None, 2):>4} "
            f"{scan.route_decision.route:<13} "
            f"{_fmt(best.avg_net_bps if best else None, 2):>7} "
            f"{scan.next_action}"
        )
    targets = select_recorder_targets(scans, limit=10)
    if targets:
        lines.append("")
        lines.append("recorder_targets=" + ",".join(
            f"{s.exchange}:{s.symbol}" for s in targets
        ))
    return "\n".join(lines)


def scanner_policy() -> dict:
    registry = DEFAULT_SCALPER_PARAMETER_REGISTRY
    return {
        "status": "research_only",
        "can_trade": False,
        "can_promote": False,
        "requires_untouched_judgment": True,
        "scanner_id": SCANNER_ID,
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
        "profitability_rule": (
            "candidate only when sample, liquidity, flow, fill, edge, and adverse "
            "selection gates pass on recorded tick/L2 replay; maker/taker route is "
            "blocked unless PF and avg net bps clear breakeven"
        ),
    }


def _gates(
    stats: TickSampleStats,
    best: ScalperReplayRow | None,
    config: ScalperScannerConfig,
) -> dict[str, bool]:
    sample = (
        stats.span_seconds >= config.min_sample_seconds
        and stats.book_events >= config.min_book_events
        and stats.trade_events >= config.min_trade_events
    )
    liquidity = (
        stats.spread_bps_p95 is not None
        and stats.spread_bps_p95 <= config.max_spread_p95_bps
    )
    flow = (
        stats.abs_imbalance_p90 is not None
        and stats.abs_imbalance_p90 >= config.min_abs_imbalance_p90
    )
    quote = best is not None and best.quotes > 0
    fill = (
        best is not None
        and best.filled >= config.min_fills
        and best.fill_rate_pct >= config.min_fill_rate_pct
    )
    edge = (
        best is not None
        and best.net_usd > 0
        and best.avg_net_bps is not None
        and best.avg_net_bps >= config.min_avg_net_bps
    )
    profit_factor = (
        best is not None
        and best.profit_factor is not None
        and best.profit_factor >= config.maker_min_profit_factor
    )
    adverse = (
        best is not None
        and best.avg_adverse_bps is not None
        and best.avg_adverse_bps >= -config.max_avg_adverse_bps
    )
    return {
        "sample": sample,
        "liquidity": liquidity,
        "flow": flow,
        "quote": quote,
        "fill": fill,
        "profit_factor": profit_factor,
        "edge_after_cost": edge,
        "adverse_selection": adverse,
    }


def _state(
    primary_blocker: str,
    gates: dict[str, bool],
    edge_score: float,
    route: ExecutionRouteDecision,
    config: ScalperScannerConfig,
) -> str:
    if primary_blocker == "NO_TICK_DATA":
        return "MISSING_TICK_DATA"
    if all(gates.values()) and edge_score >= config.min_candidate_score:
        return "REPLAY_CANDIDATE"
    if not gates["sample"]:
        return "RECORD_MORE"
    if not gates["liquidity"]:
        return "REJECTED_LIQUIDITY"
    if not gates["quote"]:
        return "REJECTED_NO_QUOTES"
    if not gates["fill"]:
        return "REJECTED_NO_FILLS"
    if route.route == "BLOCKED" or not gates["profit_factor"] or not gates["edge_after_cost"]:
        return "REJECTED_COST_WALL"
    return "REJECTED_MICROSTRUCTURE"


def _edge_score(
    stats: TickSampleStats,
    best: ScalperReplayRow | None,
    gates: dict[str, bool],
    config: ScalperScannerConfig,
) -> float:
    score = 0.0
    score += 15.0 * _sample_progress(stats, config)
    score += 15.0 * _liquidity_score(stats.spread_bps_p95, config.max_spread_p95_bps)
    score += 15.0 * _ratio(stats.abs_imbalance_p90, config.min_abs_imbalance_p90)
    if best is not None:
        score += 15.0 * min(best.fill_rate_pct / config.min_fill_rate_pct, 1.0)
        if best.avg_net_bps is not None:
            score += 20.0 * _ratio(best.avg_net_bps, config.min_avg_net_bps)
        if best.profit_factor is not None:
            score += 10.0 * _ratio(best.profit_factor, config.maker_min_profit_factor)
        if best.avg_adverse_bps is not None:
            adverse_ratio = (best.avg_adverse_bps + config.max_avg_adverse_bps)
            adverse_ratio /= config.max_avg_adverse_bps
            score += 10.0 * _clamp(adverse_ratio, 0.0, 1.0)
    if not gates["edge_after_cost"] or not gates["profit_factor"]:
        score = min(score, 79.0)
    return round(_clamp(score, 0.0, 100.0), 1)


def _recorder_priority(
    stats: TickSampleStats,
    best: ScalperReplayRow | None,
    state: str,
    config: ScalperScannerConfig,
) -> float:
    if state == "REPLAY_CANDIDATE":
        return 100.0
    if state == "MISSING_TICK_DATA":
        return 55.0
    liquidity = _liquidity_score(stats.spread_bps_p95, config.max_spread_p95_bps)
    flow = _ratio(stats.abs_imbalance_p90, config.min_abs_imbalance_p90)
    quote = 1.0 if best is not None and best.quotes > 0 else 0.0
    fill = (
        min(best.fill_rate_pct / config.min_fill_rate_pct, 1.0)
        if best is not None else 0.0
    )
    sample = _sample_progress(stats, config)
    priority = 10.0 + 30.0 * liquidity + 25.0 * flow + 15.0 * quote
    priority += 10.0 * fill + 10.0 * sample
    if state.startswith("REJECTED_"):
        priority = min(priority, 35.0)
    if state == "REJECTED_COST_WALL":
        priority = min(priority, 20.0)
    return round(_clamp(priority, 0.0, 100.0), 1)


def _action(
    state: str,
    stats: TickSampleStats,
    best: ScalperReplayRow | None,
    route: ExecutionRouteDecision,
    config: ScalperScannerConfig,
) -> str:
    if state == "MISSING_TICK_DATA":
        return "start tick/L2 recorder for this lane"
    if state == "RECORD_MORE":
        needed = max(config.min_sample_seconds - stats.span_seconds, 0.0) / 3_600.0
        return f"record {needed:.1f}h more before judging"
    if state == "REPLAY_CANDIDATE":
        return "pre-register untouched replay; no auto-promotion"
    if state == "REJECTED_LIQUIDITY":
        return "deprioritize; spread/liquidity fails scanner"
    if state == "REJECTED_NO_QUOTES":
        return "no quote-worthy imbalance; do not loosen blindly"
    if state == "REJECTED_NO_FILLS":
        return "passive quotes do not fill under conservative model"
    if state == "REJECTED_COST_WALL":
        avg = _fmt(best.avg_net_bps if best else None, 2)
        return f"cost wall; avg_net_bps={avg}, route={route.route}, do not trade"
    return "archive until a new pre-registered scanner hypothesis exists"


def _scan_sort_key(scan: ScalperLaneScan) -> tuple[float, float, str, str, str]:
    return (-scan.recorder_priority, -scan.edge_score, scan.exchange, scan.symbol, scan.day)


def _sample_progress(stats: TickSampleStats, config: ScalperScannerConfig) -> float:
    span = stats.span_seconds / config.min_sample_seconds if config.min_sample_seconds else 1.0
    books = stats.book_events / config.min_book_events if config.min_book_events else 1.0
    trades = stats.trade_events / config.min_trade_events if config.min_trade_events else 1.0
    return _clamp(min(span, books, trades), 0.0, 1.0)


def _liquidity_score(spread_p95_bps: float | None, max_spread_p95_bps: float) -> float:
    if spread_p95_bps is None or max_spread_p95_bps <= 0:
        return 0.0
    if spread_p95_bps <= max_spread_p95_bps:
        return 1.0
    return _clamp(1.0 - ((spread_p95_bps - max_spread_p95_bps) / max_spread_p95_bps), 0.0, 1.0)


def _ratio(value: float | None, target: float) -> float:
    if value is None or target <= 0:
        return 0.0
    return _clamp(value / target, 0.0, 1.0)


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _fmt(v: float | None, digits: int = 2) -> str:
    return "--" if v is None else f"{v:.{digits}f}"


def _split_csv(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="rank tick/L2 scalper scanner lanes")
    p.add_argument("--data-root", default="data")
    p.add_argument("--days", required=True, help="comma-separated UTC days, YYYYMMDD")
    p.add_argument("--exchanges", help="comma-separated exchange ids")
    p.add_argument("--symbols", help="comma-separated perp symbols")
    p.add_argument("--all-markets", action="store_true",
                   help="discover active linear derivative markets via CCXT")
    p.add_argument("--quote-assets", default=",".join(DEFAULT_DERIVATIVE_QUOTES),
                   help="comma-separated quote/settle assets for --all-markets")
    p.add_argument("--max-symbols-per-exchange", type=int,
                   help="optional cap for first-pass all-market scans")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--json", action="store_true", help="emit machine-readable JSON")
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
    scans = scan_recorded_days(
        Path(args.data_root),
        targets,
        _split_csv(args.days),
    )
    if args.json:
        payload = {
            "policy": scanner_policy(),
            "scans": [s.to_dict() for s in scans],
            "recorder_targets": [s.to_dict() for s in select_recorder_targets(scans)],
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(render_scanner_report(scans, limit=args.limit))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
