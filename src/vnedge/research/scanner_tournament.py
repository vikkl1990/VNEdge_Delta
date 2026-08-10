"""Research-only scanner tournament for Pine-inspired alpha families.

The normal VNEDGE promotion gates are intentionally hard. This module lowers
only the discovery friction: scanners may fire with relaxed research thresholds
so the edge model and operators can see more examples. The output is never a
trade permission, never a promotion, and every report carries that contract.
"""

from __future__ import annotations

import argparse
import json
import math
import signal
import threading
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Iterable, Literal

from vnedge.data.parquet_store import ParquetStore
from vnedge.research.edge_model_v1 import (
    DEFAULT_EDGE_MODEL_TIMEFRAMES,
    EdgeModelConfig,
    backtest_edge_model_timeframe_matrix,
    load_strategy_opportunities,
)
from vnedge.research.execution_edge_router import (
    DEFAULT_SCALPER_STRATEGIES,
    OpportunityRoute,
    OpportunityRouterConfig,
    summarize_routes,
)
from vnedge.research.universe import ResearchTarget, load_research_targets

TournamentVerdict = Literal[
    "STRICT_PROOF_WATCHLIST",
    "DISCOVERY_WATCHLIST",
    "NEEDS_MORE_SAMPLES",
    "WATCH_PF_WEAK",
    "REJECT_NEGATIVE_AFTER_COST",
    "NO_ROUTED_TRADES",
]
ProgressStatus = Literal["running", "completed", "failed"]
DEFAULT_PROGRESS_HEARTBEAT_SECONDS = 60
DEFAULT_PROGRESS_STALE_AFTER_SECONDS = 300


@dataclass(frozen=True)
class ScannerTournamentProfile:
    name: str
    description: str
    router_config: OpportunityRouterConfig
    model_config: EdgeModelConfig
    can_trade: bool = False
    can_promote: bool = False
    live_governance_unchanged: bool = True
    lowered_governance_scope: str = "research_discovery_only"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "router_config": asdict(self.router_config),
            "model_config": asdict(self.model_config),
            "can_trade": self.can_trade,
            "can_promote": self.can_promote,
            "live_governance_unchanged": self.live_governance_unchanged,
            "lowered_governance_scope": self.lowered_governance_scope,
        }


@dataclass(frozen=True)
class ScannerTournamentCandidate:
    rank: int
    candidate_id: str
    verdict: TournamentVerdict
    recommended_action: str
    score: float
    exchange: str
    symbol: str
    timeframe: str
    strategy_id: str
    opportunities: int
    routed: int
    skipped: int
    avg_selected_net_bps: float | None
    avg_selected_gross_bps: float | None
    profit_factor: float | None
    win_rate_pct: float
    avg_mfe_bps: float | None
    avg_mae_bps: float | None
    primary_blocker: str
    action_counts: dict[str, int]
    dominant_route: str | None
    timeframe_model_verdict: str | None
    timeframe_model_avg_net_bps: float | None
    timeframe_model_profit_factor: float | None
    timeframe_model_improvement_bps: float | None
    strict_watchlist: bool
    can_trade: bool = False
    can_promote: bool = False
    requires_untouched_judgment: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


def strict_proof_profile() -> ScannerTournamentProfile:
    return ScannerTournamentProfile(
        name="strict_proof",
        description=(
            "Current proof-grade scanner routing thresholds. Use this to confirm "
            "that a candidate already clears the normal fee/PF evidence bar."
        ),
        router_config=OpportunityRouterConfig(
            horizon_bars=12,
            min_samples=20,
            min_expected_net_edge_bps=25.0,
            min_profit_factor=1.50,
            maker_fill_probability=0.60,
            maker_fill_floor=0.50,
            maker_fallback_fill_floor=0.25,
            taker_extra_buffer_bps=5.0,
        ),
        model_config=EdgeModelConfig(
            min_train_samples=100,
            min_test_samples=50,
            min_model_trades=20,
            min_predicted_net_bps=25.0,
            min_profit_factor=1.50,
            min_improvement_bps=1.0,
        ),
    )


def paper_probe_profile() -> ScannerTournamentProfile:
    return ScannerTournamentProfile(
        name="paper_probe_candidate",
        description=(
            "Intermediate research scan. It highlights candidates worth a fresh "
            "untouched replay or paper-probe design, but still cannot trade."
        ),
        router_config=OpportunityRouterConfig(
            horizon_bars=10,
            min_samples=10,
            min_expected_net_edge_bps=12.0,
            min_profit_factor=1.20,
            maker_fill_probability=0.58,
            maker_fill_floor=0.35,
            maker_fallback_fill_floor=0.18,
            taker_extra_buffer_bps=3.0,
        ),
        model_config=EdgeModelConfig(
            min_train_samples=60,
            min_test_samples=25,
            min_model_trades=10,
            min_predicted_net_bps=12.0,
            min_profit_factor=1.20,
            min_improvement_bps=0.75,
        ),
    )


def discovery_relaxed_profile() -> ScannerTournamentProfile:
    return ScannerTournamentProfile(
        name="discovery_relaxed",
        description=(
            "Lowered research-only governance. It makes scanners fire more often "
            "for diagnosis, AI ranking, and feature learning; paper/live gates "
            "remain unchanged."
        ),
        router_config=OpportunityRouterConfig(
            horizon_bars=8,
            min_samples=5,
            min_expected_net_edge_bps=5.0,
            min_profit_factor=1.00,
            maker_fill_probability=0.55,
            maker_fill_floor=0.20,
            maker_fallback_fill_floor=0.10,
            taker_extra_buffer_bps=2.0,
        ),
        model_config=EdgeModelConfig(
            min_train_samples=30,
            min_test_samples=10,
            min_model_trades=5,
            min_predicted_net_bps=5.0,
            min_profit_factor=1.00,
            min_improvement_bps=0.25,
        ),
    )


def scanner_tournament_profile(name: str) -> ScannerTournamentProfile:
    profiles = {
        "strict_proof": strict_proof_profile,
        "paper_probe_candidate": paper_probe_profile,
        "discovery_relaxed": discovery_relaxed_profile,
    }
    try:
        return profiles[name]()
    except KeyError:
        raise ValueError(f"unknown scanner tournament profile: {name}") from None


def build_scanner_tournament_report(
    routes: Iterable[OpportunityRoute],
    *,
    profile: ScannerTournamentProfile,
    targets: Iterable[ResearchTarget],
    strategy_ids: Iterable[str],
    lookback_days: int,
    data_coverage: dict | None = None,
    max_candidates: int = 50,
) -> dict:
    rows = tuple(routes)
    targets_tuple = tuple(targets)
    strategies_tuple = tuple(strategy_ids)
    matrix = backtest_edge_model_timeframe_matrix(rows, config=profile.model_config)
    candidates = _rank_candidates(
        rows,
        profile=profile,
        model_matrix=matrix,
        max_candidates=max_candidates,
    )
    summary = _build_summary(rows, candidates, profile=profile)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "truth_layer": "scanner_tournament_v1",
        "policy": _research_policy(),
        "profile": profile.to_dict(),
        "scope": {
            "lookback_days": lookback_days,
            "targets": [asdict(target) for target in targets_tuple],
            "target_count": len(targets_tuple),
            "strategies": list(strategies_tuple),
            "strategy_count": len(strategies_tuple),
            "data_coverage": data_coverage or {},
        },
        "summary": summary,
        "candidates": [candidate.to_dict() for candidate in candidates],
        "edge_model_matrix": _compact_matrix(matrix),
    }


def build_scanner_tournament_progress(
    *,
    status: ProgressStatus,
    phase: str,
    started_at: str,
    profile: ScannerTournamentProfile,
    targets: Iterable[ResearchTarget],
    strategy_ids: Iterable[str],
    lookback_days: int,
    completed_work_units: int = 0,
    total_work_units: int | None = None,
    current_target: dict | ResearchTarget | None = None,
    current_strategy: str | None = None,
    rows: int | None = None,
    routes: int | None = None,
    output_path: Path | str | None = None,
    last_error: str | None = None,
    stale_after_seconds: int = DEFAULT_PROGRESS_STALE_AFTER_SECONDS,
) -> dict:
    targets_tuple = tuple(targets)
    strategies_tuple = tuple(strategy_ids)
    computed_total = len(targets_tuple) * len(strategies_tuple)
    total = computed_total if total_work_units is None else max(0, int(total_work_units))
    completed = max(0, int(completed_work_units))
    if total:
        completed = min(completed, total)
    progress_pct = round((completed / total) * 100.0, 2) if total else 100.0
    now = datetime.now(UTC).isoformat()
    return {
        "generated_at": now,
        "truth_layer": "scanner_tournament_progress_v1",
        "status": status,
        "phase": phase,
        "started_at": started_at,
        "heartbeat_at": now,
        "stale_after_seconds": max(1, int(stale_after_seconds)),
        "completed_at": now if status in {"completed", "failed"} else None,
        "profile": profile.name,
        "lookback_days": lookback_days,
        "target_count": len(targets_tuple),
        "strategy_count": len(strategies_tuple),
        "total_work_units": total,
        "completed_work_units": completed,
        "progress_pct": progress_pct,
        "current_target": _target_payload(current_target),
        "current_strategy": current_strategy,
        "current_rows": rows,
        "current_routes": routes,
        "output_path": str(output_path) if output_path is not None else None,
        "last_error": last_error,
        "policy": _research_policy(),
        "can_trade": False,
        "can_promote": False,
    }


def run_scanner_tournament(
    *,
    data_root: Path | str,
    targets: Iterable[ResearchTarget],
    strategy_ids: Iterable[str] = DEFAULT_SCALPER_STRATEGIES,
    lookback_days: int = 30,
    profile: ScannerTournamentProfile = discovery_relaxed_profile(),
    max_candidates: int = 50,
    progress_callback: Callable[[dict], None] | None = None,
) -> dict:
    targets_tuple = tuple(targets)
    strategies_tuple = tuple(strategy_ids)
    routes = load_strategy_opportunities(
        data_root=data_root,
        targets=targets_tuple,
        strategy_ids=strategies_tuple,
        lookback_days=lookback_days,
        router_config=profile.router_config,
        progress_callback=progress_callback,
    )
    if progress_callback is not None:
        progress_callback(
            {
                "phase": "building_report",
                "target": None,
                "strategy_id": None,
                "total_work_units": len(targets_tuple) * len(strategies_tuple),
                "completed_work_units": len(targets_tuple) * len(strategies_tuple),
                "rows": None,
                "routes": len(routes),
                "last_error": None,
            }
        )
    return build_scanner_tournament_report(
        routes,
        profile=profile,
        targets=targets_tuple,
        strategy_ids=strategies_tuple,
        lookback_days=lookback_days,
        data_coverage=_target_data_coverage(data_root, targets_tuple),
        max_candidates=max_candidates,
    )


def publish_report(report: dict, output: Path | str, feed: Path | str | None = None) -> None:
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(report, indent=2, sort_keys=True))
    tmp_path.chmod(0o644)
    tmp_path.replace(output_path)
    output_path.chmod(0o644)
    if feed is not None:
        feed_path = Path(feed)
        feed_path.parent.mkdir(parents=True, exist_ok=True)
        with feed_path.open("a") as fh:
            fh.write(json.dumps(_feed_record(report), sort_keys=True) + "\n")
        feed_path.chmod(0o644)


def publish_progress(progress: dict, output: Path | str) -> None:
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(progress, indent=2, sort_keys=True))
    tmp_path.chmod(0o644)
    tmp_path.replace(output_path)
    output_path.chmod(0o644)


def _rank_candidates(
    rows: tuple[OpportunityRoute, ...],
    *,
    profile: ScannerTournamentProfile,
    model_matrix: dict,
    max_candidates: int,
) -> tuple[ScannerTournamentCandidate, ...]:
    model_by_timeframe = _model_reports_by_timeframe(model_matrix)
    grouped: dict[tuple[str, str, str, str], list[OpportunityRoute]] = defaultdict(list)
    for row in rows:
        meta = row.metadata or {}
        key = (
            str(meta.get("exchange") or "unknown"),
            str(meta.get("symbol") or "unknown"),
            str(meta.get("timeframe") or "unknown"),
            row.strategy_id,
        )
        grouped[key].append(row)

    pending: list[ScannerTournamentCandidate] = []
    for (exchange, symbol, timeframe, strategy_id), group in grouped.items():
        summary = summarize_routes(group, config=profile.router_config)
        model_summary = model_by_timeframe.get(timeframe, {})
        strict_watchlist = _clears_strict_watchlist(summary)
        verdict = _candidate_verdict(summary, strict_watchlist, profile)
        candidate = ScannerTournamentCandidate(
            rank=0,
            candidate_id=_candidate_id(exchange, symbol, timeframe, strategy_id),
            verdict=verdict,
            recommended_action=_recommended_action(verdict),
            score=_score_summary(summary, strict_watchlist),
            exchange=exchange,
            symbol=symbol,
            timeframe=timeframe,
            strategy_id=strategy_id,
            opportunities=summary.opportunities,
            routed=summary.routed,
            skipped=summary.skipped,
            avg_selected_net_bps=summary.avg_selected_net_bps,
            avg_selected_gross_bps=summary.avg_selected_gross_bps,
            profit_factor=summary.profit_factor,
            win_rate_pct=summary.win_rate_pct,
            avg_mfe_bps=summary.avg_mfe_bps,
            avg_mae_bps=summary.avg_mae_bps,
            primary_blocker=summary.primary_blocker,
            action_counts=summary.action_counts,
            dominant_route=_dominant_route(summary.action_counts),
            timeframe_model_verdict=model_summary.get("verdict"),
            timeframe_model_avg_net_bps=model_summary.get("model_avg_net_bps"),
            timeframe_model_profit_factor=model_summary.get("model_profit_factor"),
            timeframe_model_improvement_bps=model_summary.get("improvement_bps"),
            strict_watchlist=strict_watchlist,
        )
        pending.append(candidate)

    ranked = sorted(
        pending,
        key=lambda row: (
            row.score,
            row.routed,
            row.avg_selected_net_bps if row.avg_selected_net_bps is not None else -10**9,
        ),
        reverse=True,
    )
    return tuple(
        ScannerTournamentCandidate(**{**asdict(row), "rank": idx})
        for idx, row in enumerate(ranked[:max_candidates], start=1)
    )


def _build_summary(
    rows: tuple[OpportunityRoute, ...],
    candidates: tuple[ScannerTournamentCandidate, ...],
    *,
    profile: ScannerTournamentProfile,
) -> dict:
    verdict_counts = dict(Counter(candidate.verdict for candidate in candidates))
    route_counts = dict(Counter(row.action for row in rows))
    positive = [
        candidate for candidate in candidates
        if candidate.verdict in {"STRICT_PROOF_WATCHLIST", "DISCOVERY_WATCHLIST"}
    ]
    best = candidates[0] if candidates else None
    return {
        "profile": profile.name,
        "opportunities": len(rows),
        "routed": sum(1 for row in rows if row.routed),
        "skipped": sum(1 for row in rows if not row.routed),
        "candidate_count": len(candidates),
        "positive_watchlists": len(positive),
        "strict_watchlists": sum(1 for row in candidates if row.strict_watchlist),
        "verdict_counts": verdict_counts,
        "route_counts": route_counts,
        "best_candidate_id": best.candidate_id if best is not None else None,
        "best_score": best.score if best is not None else None,
        "best_avg_net_bps": best.avg_selected_net_bps if best is not None else None,
        "best_profit_factor": best.profit_factor if best is not None else None,
        "research_governance_lowered": profile.name != "strict_proof",
        "live_governance_unchanged": profile.live_governance_unchanged,
        "can_trade": False,
        "can_promote": False,
    }


def _candidate_verdict(
    summary,
    strict_watchlist: bool,
    profile: ScannerTournamentProfile,
) -> TournamentVerdict:
    if summary.routed <= 0:
        return "NO_ROUTED_TRADES"
    if strict_watchlist:
        return "STRICT_PROOF_WATCHLIST"
    if summary.avg_selected_net_bps is None or summary.avg_selected_net_bps <= 0.0:
        return "REJECT_NEGATIVE_AFTER_COST"
    if summary.routed < profile.router_config.min_samples:
        return "NEEDS_MORE_SAMPLES"
    if (summary.profit_factor or 0.0) < profile.router_config.min_profit_factor:
        return "WATCH_PF_WEAK"
    return "DISCOVERY_WATCHLIST"


def _clears_strict_watchlist(summary) -> bool:
    return (
        summary.routed >= 20
        and summary.avg_selected_net_bps is not None
        and summary.avg_selected_net_bps >= 25.0
        and (summary.profit_factor or 0.0) >= 1.50
    )


def _score_summary(summary, strict_watchlist: bool) -> float:
    avg = (
        summary.avg_selected_net_bps
        if summary.avg_selected_net_bps is not None
        else -50.0
    )
    pf = summary.profit_factor if summary.profit_factor is not None else 0.0
    bounded_pf = min(float(pf), 5.0)
    sample_bonus = min(math.sqrt(max(summary.routed, 0)), 10.0)
    win_bonus = summary.win_rate_pct / 20.0
    mae_penalty = 0.0
    if summary.avg_mae_bps is not None:
        mae_penalty = min(abs(float(summary.avg_mae_bps)) * 0.05, 10.0)
    strict_bonus = 25.0 if strict_watchlist else 0.0
    score = (
        float(avg)
        + (bounded_pf * 4.0)
        + sample_bonus
        + win_bonus
        - mae_penalty
        + strict_bonus
    )
    return round(score, 4)


def _recommended_action(verdict: TournamentVerdict) -> str:
    actions = {
        "STRICT_PROOF_WATCHLIST": "PRE_REGISTER_UNTOUCHED_JUDGMENT_WINDOW",
        "DISCOVERY_WATCHLIST": "KEEP_RELAXED_RESEARCH_ON_AND_TRAIN_EDGE_MODEL",
        "NEEDS_MORE_SAMPLES": "COLLECT_MORE_CANDLES_OR_WIDEN_UNIVERSE",
        "WATCH_PF_WEAK": "REVIEW_EXIT_AND_FEE_FILTER_BEFORE_REPLAY",
        "REJECT_NEGATIVE_AFTER_COST": "DO_NOT_PROMOTE; MINE_FAILURE_CONTEXT",
        "NO_ROUTED_TRADES": "INSPECT_SCANNER_THRESHOLDS_OR_DATA_COVERAGE",
    }
    return actions[verdict]


def _model_reports_by_timeframe(matrix: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for report in matrix.get("reports", []):
        scope = report.get("scope", {})
        timeframe = scope.get("timeframe")
        if timeframe:
            out[str(timeframe)] = dict(report.get("summary", {}))
    return out


def _compact_matrix(matrix: dict) -> dict:
    compact = dict(matrix)
    reports = []
    for report in compact.get("reports", []):
        item = dict(report)
        routes = item.get("routes") or []
        item["routes_omitted"] = len(routes)
        item["routes"] = []
        reports.append(item)
    compact["reports"] = reports
    return compact


def _dominant_route(action_counts: dict[str, int]) -> str | None:
    counts = {key: value for key, value in action_counts.items() if key != "SKIP"}
    if not counts:
        return None
    return max(counts.items(), key=lambda item: item[1])[0]


def _candidate_id(exchange: str, symbol: str, timeframe: str, strategy_id: str) -> str:
    safe_symbol = (
        symbol.replace("/", "")
        .replace(":", "")
        .replace("-", "")
        .replace("_", "")
        .upper()
    )
    return f"{strategy_id}__{exchange}__{safe_symbol}__{timeframe}"


def _research_policy() -> dict:
    return {
        "research_only": True,
        "can_trade": False,
        "can_promote": False,
        "requires_untouched_judgment": True,
        "decision_uses_forward_truth": False,
        "lowered_governance_scope": "research_discovery_only",
        "live_governance_unchanged": True,
        "risk_gateway_required_for_any_future_order": True,
        "live_order_gates_unchanged": True,
        "operator_note": (
            "Governance is lowered only inside the scanner discovery layer. "
            "Any candidate must pass normal untouched-data judgment before "
            "paper/shadow/live promotion is discussed."
        ),
    }


def _target_data_coverage(data_root: Path | str, targets: Iterable[ResearchTarget]) -> dict:
    store = ParquetStore(data_root)
    available: list[dict] = []
    missing: list[dict] = []
    for target in targets:
        item = asdict(target)
        if store.candles_path(target.exchange, target.symbol, target.timeframe).exists():
            available.append(item)
        else:
            missing.append(item)
    return {
        "attempted": len(available) + len(missing),
        "available": len(available),
        "missing": len(missing),
        "available_targets": available,
        "missing_targets": missing,
    }


def _feed_record(report: dict) -> dict:
    summary = report.get("summary", {})
    best = (report.get("candidates") or [{}])[0]
    return {
        "generated_at": report.get("generated_at"),
        "truth_layer": report.get("truth_layer"),
        "profile": summary.get("profile"),
        "opportunities": summary.get("opportunities"),
        "routed": summary.get("routed"),
        "positive_watchlists": summary.get("positive_watchlists"),
        "strict_watchlists": summary.get("strict_watchlists"),
        "best_candidate_id": summary.get("best_candidate_id"),
        "best_verdict": best.get("verdict"),
        "best_score": summary.get("best_score"),
        "can_trade": False,
        "can_promote": False,
    }


def _split_csv(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _resolve_timeframes(raw: str | None) -> tuple[str, ...]:
    values = _split_csv(raw) or DEFAULT_EDGE_MODEL_TIMEFRAMES
    if any(item.lower() == "all" for item in values):
        return DEFAULT_EDGE_MODEL_TIMEFRAMES
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value not in seen:
            out.append(value)
            seen.add(value)
    return tuple(out)


def _expand_targets(
    base_targets: Iterable[ResearchTarget],
    timeframes: Iterable[str],
) -> tuple[ResearchTarget, ...]:
    out: list[ResearchTarget] = []
    seen: set[str] = set()
    for target in base_targets:
        for timeframe in timeframes:
            expanded = ResearchTarget(target.exchange, target.symbol, timeframe)
            if expanded.key not in seen:
                out.append(expanded)
                seen.add(expanded.key)
    return tuple(out)


def _load_targets(
    args: argparse.Namespace,
    timeframes: tuple[str, ...],
) -> tuple[ResearchTarget, ...]:
    if args.exchange and args.symbol:
        base_targets = (ResearchTarget(args.exchange, args.symbol, timeframes[0]),)
    else:
        base_targets = load_research_targets(
            exchanges=_split_csv(args.exchanges) or None,
            symbols=_split_csv(args.symbols) or None,
            timeframe=timeframes[0],
        )
    if args.max_targets is not None:
        base_targets = tuple(base_targets)[: args.max_targets]
    return _expand_targets(base_targets, timeframes)


def _render_report(report: dict) -> str:
    summary = report["summary"]
    lines = [
        "scanner tournament v1",
        "policy=research_only lowered_governance=research_discovery_only live_governance=unchanged",
        "",
        (
            f"profile={summary['profile']} opportunities={summary['opportunities']} "
            f"routed={summary['routed']} positive_watchlists={summary['positive_watchlists']} "
            f"strict_watchlists={summary['strict_watchlists']}"
        ),
        "",
        "rank verdict                    score routed avg_bps pf     win%  scanner/venue/symbol/timeframe",
    ]
    for row in report["candidates"][:20]:
        lines.append(
            f"{row['rank']:>4} {row['verdict']:<26} {row['score']:>6.2f} "
            f"{row['routed']:>6} {_fmt(row['avg_selected_net_bps']):>7} "
            f"{_fmt(row['profit_factor']):>6} {row['win_rate_pct']:>5.1f} "
            f"{row['strategy_id']} {row['exchange']} {row['symbol']} {row['timeframe']}"
        )
    if not report["candidates"]:
        lines.append("no candidates produced; check candle coverage and strategy ids")
    return "\n".join(lines)


def _fmt(value: float | None) -> str:
    return "--" if value is None else f"{float(value):.2f}"


def _target_payload(target: dict | ResearchTarget | None) -> dict | None:
    if target is None:
        return None
    if isinstance(target, ResearchTarget):
        return asdict(target)
    return dict(target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="run a research-only relaxed scanner tournament"
    )
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--exchange", help="single exchange id")
    parser.add_argument("--symbol", help="single symbol")
    parser.add_argument("--exchanges", help="comma-separated exchange ids")
    parser.add_argument("--symbols", help="comma-separated symbols")
    parser.add_argument(
        "--timeframes",
        default="1m,5m,15m,1h,4h",
        help="comma-separated timeframes or 'all'",
    )
    parser.add_argument("--strategies", default=",".join(DEFAULT_SCALPER_STRATEGIES))
    parser.add_argument("--lookback-days", type=int, default=30)
    parser.add_argument("--max-targets", type=int)
    parser.add_argument("--max-candidates", type=int, default=50)
    parser.add_argument(
        "--profile",
        choices=("strict_proof", "paper_probe_candidate", "discovery_relaxed"),
        default="discovery_relaxed",
    )
    parser.add_argument("--interval-seconds", type=int, default=0)
    parser.add_argument(
        "--output",
        default="research/live_research/scanner_tournament_latest.json",
    )
    parser.add_argument(
        "--feed",
        default="research/live_research/scanner_tournament_feed.jsonl",
    )
    parser.add_argument(
        "--progress",
        default="research/live_research/scanner_tournament_progress.json",
    )
    parser.add_argument(
        "--heartbeat-seconds",
        type=int,
        default=DEFAULT_PROGRESS_HEARTBEAT_SECONDS,
        help="refresh running progress this often during long per-lane replays",
    )
    parser.add_argument(
        "--stale-after-seconds",
        type=int,
        default=DEFAULT_PROGRESS_STALE_AFTER_SECONDS,
        help="UI stale threshold for a missing scanner tournament heartbeat",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    while True:
        started_at = datetime.now(UTC).isoformat()
        timeframes = _resolve_timeframes(args.timeframes)
        targets = _load_targets(args, timeframes)
        strategies = _split_csv(args.strategies)
        profile = scanner_tournament_profile(args.profile)
        total_work_units = len(targets) * len(strategies)
        last_event: dict = {
            "phase": "initializing",
            "completed_work_units": 0,
            "total_work_units": total_work_units,
            "target": None,
            "strategy_id": None,
            "rows": None,
            "routes": None,
            "last_error": None,
        }
        publish_lock = threading.RLock()
        stop_heartbeat = threading.Event()

        def publish_state(
            status: ProgressStatus,
            phase: str,
            event: dict | None = None,
            last_error: str | None = None,
        ) -> None:
            payload = dict(event or last_event)
            with publish_lock:
                publish_progress(
                    build_scanner_tournament_progress(
                        status=status,
                        phase=phase,
                        started_at=started_at,
                        profile=profile,
                        targets=targets,
                        strategy_ids=strategies,
                        lookback_days=args.lookback_days,
                        completed_work_units=int(payload.get("completed_work_units") or 0),
                        total_work_units=int(payload.get("total_work_units") or total_work_units),
                        current_target=payload.get("target"),
                        current_strategy=payload.get("strategy_id"),
                        rows=payload.get("rows"),
                        routes=payload.get("routes"),
                        output_path=args.output,
                        last_error=last_error or payload.get("last_error"),
                        stale_after_seconds=args.stale_after_seconds,
                    ),
                    args.progress,
                )

        def progress_callback(event: dict) -> None:
            nonlocal last_event
            last_event = dict(event)
            publish_state(
                "running",
                str(event.get("phase") or "running"),
                event=last_event,
            )

        def heartbeat_loop() -> None:
            interval = max(1, int(args.heartbeat_seconds))
            while not stop_heartbeat.wait(interval):
                publish_state(
                    "running",
                    str(last_event.get("phase") or "running"),
                )

        heartbeat_thread: threading.Thread | None = None
        if args.heartbeat_seconds > 0:
            heartbeat_thread = threading.Thread(
                target=heartbeat_loop,
                name="scanner-tournament-progress-heartbeat",
                daemon=True,
            )
            heartbeat_thread.start()

        previous_sigterm = signal.getsignal(signal.SIGTERM)
        previous_sigint = signal.getsignal(signal.SIGINT)

        def handle_shutdown(signum: int, _frame: object) -> None:
            publish_state(
                "failed",
                "interrupted",
                last_error=(
                    f"received signal {signum}; scanner tournament stopped "
                    "before publishing a complete report"
                ),
            )
            raise SystemExit(128 + signum)

        signal.signal(signal.SIGTERM, handle_shutdown)
        signal.signal(signal.SIGINT, handle_shutdown)
        try:
            publish_state("running", "initializing")
            report = run_scanner_tournament(
                data_root=args.data_root,
                targets=targets,
                strategy_ids=strategies,
                lookback_days=args.lookback_days,
                profile=profile,
                max_candidates=args.max_candidates,
                progress_callback=progress_callback,
            )
            publish_report(report, args.output, args.feed)
            last_event = {
                **last_event,
                "phase": "published_report",
                "completed_work_units": total_work_units,
                "total_work_units": total_work_units,
                "target": None,
                "strategy_id": None,
                "routes": report.get("summary", {}).get("opportunities"),
                "last_error": None,
            }
            publish_state("completed", "published_report")
        except Exception as exc:
            publish_state("failed", "failed", last_error=str(exc))
            raise
        finally:
            stop_heartbeat.set()
            if heartbeat_thread is not None:
                heartbeat_thread.join(timeout=1)
            signal.signal(signal.SIGTERM, previous_sigterm)
            signal.signal(signal.SIGINT, previous_sigint)
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(_render_report(report), flush=True)
        if args.interval_seconds <= 0:
            return 0
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
