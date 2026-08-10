"""Paper lane root-cause matrix.

The paper operator surface has many honest feeds: activation, route doctor,
cadence, profile, performance, exit autopsy, survival, governor, and firing
causality. This module joins them into one lane-level answer:

    "What is the primary reason this lane is silent or losing?"

Read-only by design. It cannot start runners, edit manifests, promote, demote,
or submit orders.
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_RESEARCH_DIR = Path("research/live_research")
DEFAULT_ACTIVATION = DEFAULT_RESEARCH_DIR / "paper_lane_activation_latest.json"
DEFAULT_ROUTE = DEFAULT_RESEARCH_DIR / "paper_route_doctor_latest.json"
DEFAULT_CADENCE = DEFAULT_RESEARCH_DIR / "paper_lane_cadence_latest.json"
DEFAULT_PERFORMANCE = DEFAULT_RESEARCH_DIR / "paper_lane_performance_latest.json"
DEFAULT_EXIT_AUTOPSY = DEFAULT_RESEARCH_DIR / "paper_trade_exit_autopsy_latest.json"
DEFAULT_ENTRY_AUTOPSY = DEFAULT_RESEARCH_DIR / "paper_trade_entry_autopsy_latest.json"
DEFAULT_SURVIVAL = DEFAULT_RESEARCH_DIR / "lane_survival_latest.json"
DEFAULT_GOVERNOR = DEFAULT_RESEARCH_DIR / "paper_lane_governor_latest.json"
DEFAULT_CAUSALITY = DEFAULT_RESEARCH_DIR / "lane_firing_causality_latest.json"
DEFAULT_OUT = DEFAULT_RESEARCH_DIR / "paper_lane_root_cause_latest.json"
DEFAULT_FEED = DEFAULT_RESEARCH_DIR / "paper_lane_root_cause_feed.jsonl"

ROOT_ROUTE_OR_JOURNAL_BROKEN = "ROUTE_OR_JOURNAL_BROKEN"
ROOT_EVALUATION_CADENCE_BROKEN = "EVALUATION_CADENCE_BROKEN"
ROOT_SIZING_PROFILE_BLOCKED = "SIZING_PROFILE_BLOCKED"
ROOT_ENTRY_QUALITY_BLOCKED = "ENTRY_QUALITY_BLOCKED"
ROOT_EXIT_OR_CAPTURE_BLOCKED = "EXIT_OR_CAPTURE_BLOCKED"
ROOT_NEGATIVE_AFTER_COST = "NEGATIVE_AFTER_COST"
ROOT_NO_CLOSED_PAPER_TRADES = "NO_CLOSED_PAPER_TRADES"
ROOT_GOVERNOR_DEMOTION_QUEUE = "GOVERNOR_DEMOTION_QUEUE"
ROOT_READY_FOR_HUMAN_REVIEW = "READY_FOR_HUMAN_REVIEW"
ROOT_MARKET_NOT_TRIGGERING = "MARKET_NOT_TRIGGERING"
ROOT_EVIDENCE_INCOMPLETE = "EVIDENCE_INCOMPLETE"

STAGE_ROUTE = "route"
STAGE_CADENCE = "cadence"
STAGE_SIZING = "sizing"
STAGE_ENTRY = "entry"
STAGE_EXIT = "exit"
STAGE_PERFORMANCE = "performance"
STAGE_GOVERNANCE = "governance"
STAGE_MARKET = "market"
STAGE_EVIDENCE = "evidence"

_STAGE_PRIORITY = {
    STAGE_ROUTE: 0,
    STAGE_CADENCE: 1,
    STAGE_SIZING: 2,
    STAGE_ENTRY: 3,
    STAGE_EXIT: 4,
    STAGE_PERFORMANCE: 5,
    STAGE_GOVERNANCE: 6,
    STAGE_MARKET: 7,
    STAGE_EVIDENCE: 8,
}

_ROUTE_BLOCKERS = {
    "RUNNER_SERVICE_DOWN",
    "ROUTE_READY_JOURNAL_MISSING",
    "ROUTE_NOT_WIRED",
    "MANIFEST_UNSAFE",
    "BLOCKED_NEGATIVE",
    "ROUTE_BLOCKED",
}
_ACTIVATION_BLOCKERS = {
    "ROUTE_BLOCKED",
    "MANIFEST_UNSAFE",
    "BLOCKED_NEGATIVE_EDGE",
    "NEEDS_RUNTIME_ADAPTER",
}
_CADENCE_BLOCKERS = {
    "EVAL_STALE",
    "HEARTBEAT_STALE",
    "JOURNAL_MISSING",
    "JOURNAL_STALE",
}
_PROFILE_BLOCKERS = {
    "PAPER_PROFILE_BLOCKED_BY_RISK",
    "LIVE_PROFILE_BLOCKED_BY_RISK",
    "PROFILE_MISSING",
}
_ENTRY_BLOCKERS = {
    "ENTRY_CONTEXT_MISSING",
    "ENTRY_SIGNAL_STALE",
    "ENTRY_DIRECTION_DRIFT",
    "ENTRY_FEE_WALL_TOO_SMALL",
    "ENTRY_NEGATIVE_AFTER_COST",
    "ENTRY_QUALITY_BLOCKED",
    "NO_ENTRY_CONTEXT",
    "STALE_ENTRY_CONTEXT",
}
_EXIT_BLOCKERS = {
    "STOP_DOMINATED",
    "FEE_WALL_DOMINATED",
    "TIMEOUT_DOMINATED",
    "TP_CAPTURE_WEAK",
    "NEGATIVE_EDGE",
    "LEDGER_OR_EXIT_METADATA_GAP",
}
_REVIEW_STATES = {
    "PAPER_PROMOTION_CANDIDATE",
    "PAPER_SURVIVOR_CANDIDATE",
    "READY_FOR_PAPER_REVIEW",
    "PAPER_ROSTER",
    "SURVIVOR_TOURNAMENT",
}
_DEMOTION_STATES = {
    "DEMOTE_TO_SHADOW",
    "DEMOTION_QUEUE",
    "RESEARCH_ONLY",
    "PROBATION_QUEUE",
}
_WAITING_STATES = {
    "PAPER_RUNNING",
    "PAPER_ONLINE_WAITING",
    "PAPER_ROUTE_READY_NO_JOURNAL",
    "FIRING",
    "NEAR_TRIGGER",
    "PAPER_WAITING_FOR_SIGNAL",
    "EVALUATING_NO_SIGNAL",
    "EVALUATING_SIGNAL_SEEN",
}


@dataclass(frozen=True)
class PaperLaneRootCauseConfig:
    max_rows: int = 240

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_paper_lane_root_cause(
    *,
    activation: Mapping[str, Any] | None = None,
    route: Mapping[str, Any] | None = None,
    cadence: Mapping[str, Any] | None = None,
    performance: Mapping[str, Any] | None = None,
    exit_autopsy: Mapping[str, Any] | None = None,
    entry_autopsy: Mapping[str, Any] | None = None,
    survival: Mapping[str, Any] | None = None,
    governor: Mapping[str, Any] | None = None,
    profile: Mapping[str, Any] | None = None,
    causality: Mapping[str, Any] | None = None,
    activation_path: Path | str = DEFAULT_ACTIVATION,
    route_path: Path | str = DEFAULT_ROUTE,
    cadence_path: Path | str = DEFAULT_CADENCE,
    performance_path: Path | str = DEFAULT_PERFORMANCE,
    exit_autopsy_path: Path | str = DEFAULT_EXIT_AUTOPSY,
    entry_autopsy_path: Path | str = DEFAULT_ENTRY_AUTOPSY,
    survival_path: Path | str = DEFAULT_SURVIVAL,
    governor_path: Path | str = DEFAULT_GOVERNOR,
    causality_path: Path | str = DEFAULT_CAUSALITY,
    config: PaperLaneRootCauseConfig = PaperLaneRootCauseConfig(),
    now: datetime | None = None,
) -> dict[str, Any]:
    """Join paper evidence into one deterministic root-cause row per lane."""

    now = now or datetime.now(UTC)
    activation_payload = _payload(activation, activation_path)
    route_payload = _payload(route, route_path)
    cadence_payload = _payload(cadence, cadence_path)
    performance_payload = _payload(performance, performance_path)
    exit_autopsy_payload = _payload(exit_autopsy, exit_autopsy_path)
    entry_autopsy_payload = _payload(entry_autopsy, entry_autopsy_path)
    survival_payload = _payload(survival, survival_path)
    governor_payload = _payload(governor, governor_path)
    profile_payload = profile if isinstance(profile, Mapping) else {"rows": []}
    causality_payload = _payload(causality, causality_path)

    slots: dict[str, dict[str, Any]] = {}
    _add_rows(slots, "activation", activation_payload.get("rows", []))
    _add_rows(slots, "route", route_payload.get("rows", []))
    _add_rows(slots, "cadence", cadence_payload.get("rows", []))
    _add_rows(slots, "performance", performance_payload.get("rows", []))
    _add_rows(slots, "exit_autopsy", exit_autopsy_payload.get("rows", []))
    _add_rows(slots, "entry_autopsy", entry_autopsy_payload.get("rows", []))
    _add_rows(slots, "survival", survival_payload.get("rows", []))
    _add_rows(slots, "governor", governor_payload.get("rows", []))
    _add_rows(slots, "profile", profile_payload.get("rows", []))
    _add_rows(slots, "causality", causality_payload.get("rows", []))

    rows = [_root_cause_row(slot) for slot in slots.values()]
    rows.sort(key=_row_sort_key)
    rows = rows[: max(1, int(config.max_rows))]
    summary = _summary(rows)

    return {
        "generated_at": now.isoformat(),
        "report_id": "paper_lane_root_cause_v1",
        "mode": "read_only_paper_lane_root_cause",
        "source_report_ids": {
            "activation": activation_payload.get("report_id"),
            "route": route_payload.get("report_id"),
            "cadence": cadence_payload.get("report_id"),
            "performance": performance_payload.get("report_id"),
            "exit_autopsy": exit_autopsy_payload.get("report_id"),
            "entry_autopsy": entry_autopsy_payload.get("report_id"),
            "survival": survival_payload.get("report_id"),
            "governor": governor_payload.get("report_id"),
            "profile": profile_payload.get("report_id"),
            "causality": causality_payload.get("report_id"),
        },
        "inputs": {
            "activation_path": str(activation_path),
            "route_path": str(route_path),
            "cadence_path": str(cadence_path),
            "performance_path": str(performance_path),
            "exit_autopsy_path": str(exit_autopsy_path),
            "entry_autopsy_path": str(entry_autopsy_path),
            "survival_path": str(survival_path),
            "governor_path": str(governor_path),
            "causality_path": str(causality_path),
        },
        "config": config.to_dict(),
        "summary": summary,
        "boards": _boards(rows),
        "rows": rows,
        "operator_answer": _operator_answer(summary),
        "policy": {
            "read_only": True,
            "can_trade": False,
            "can_promote": False,
            "can_demote": False,
            "single_root_cause_is_diagnostic_not_approval": True,
        },
        "can_trade": False,
        "can_promote": False,
    }


def publish_paper_lane_root_cause(
    payload: Mapping[str, Any], out: Path, feed: Path | None = None
) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    tmp.replace(out)
    if feed is not None:
        feed.parent.mkdir(parents=True, exist_ok=True)
        with open(feed, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, default=str) + "\n")


def render_report(payload: Mapping[str, Any], *, limit: int = 50) -> str:
    summary = _map(payload.get("summary"))
    lines = [
        "=== Paper lane root-cause matrix ===",
        f"generated: {payload.get('generated_at')}",
        str(payload.get("operator_answer") or ""),
        (
            "summary: "
            f"{summary.get('total_lanes', 0)} lanes, "
            f"{summary.get('route_blocked', 0)} route, "
            f"{summary.get('cadence_blocked', 0)} cadence, "
            f"{summary.get('entry_blocked', 0)} entry, "
            f"{summary.get('exit_blocked', 0)} exit, "
            f"{summary.get('negative_after_cost', 0)} negative"
        ),
    ]
    for row in list(payload.get("rows", []))[:limit]:
        lines.append(
            f"  {row.get('severity', ''):<2} {row.get('stage', ''):<11} "
            f"{row.get('root_cause', ''):<30} "
            f"{row.get('exchange', ''):<13} {row.get('symbol', ''):<12} "
            f"{row.get('timeframe', ''):<3} {row.get('strategy_id', ''):<28} "
            f"{row.get('action', '')}"
        )
    lines.append("read-only: can_trade=false can_promote=false")
    return "\n".join(lines)


def _root_cause_row(slot: Mapping[str, Any]) -> dict[str, Any]:
    activation = _map(slot.get("activation"))
    route = _map(slot.get("route"))
    cadence = _map(slot.get("cadence"))
    performance = _map(slot.get("performance"))
    exit_autopsy = _map(slot.get("exit_autopsy"))
    entry_autopsy = _map(slot.get("entry_autopsy"))
    survival = _map(slot.get("survival"))
    governor = _map(slot.get("governor"))
    causality = _map(slot.get("causality"))
    profiles = _map(slot.get("profiles"))
    paper_profile = _map(profiles.get("paper"))
    live_profile = _map(profiles.get("live"))

    identity = _identity(slot, activation, route, cadence, performance, exit_autopsy,
                         entry_autopsy, survival, governor, causality)
    evidence = _evidence_snapshot(
        activation=activation,
        route=route,
        cadence=cadence,
        performance=performance,
        exit_autopsy=exit_autopsy,
        entry_autopsy=entry_autopsy,
        survival=survival,
        governor=governor,
        paper_profile=paper_profile,
        live_profile=live_profile,
        causality=causality,
    )
    stage, root, severity, owner, action, confidence, blockers = _classify(
        evidence=evidence,
        activation=activation,
        route=route,
        cadence=cadence,
        performance=performance,
        exit_autopsy=exit_autopsy,
        entry_autopsy=entry_autopsy,
        survival=survival,
        governor=governor,
        paper_profile=paper_profile,
        live_profile=live_profile,
        causality=causality,
    )
    metrics = _metrics(performance, exit_autopsy, cadence, causality)
    return {
        "lane_key": _text(slot.get("lane_key")) or _row_key(slot),
        "root_cause": root,
        "stage": stage,
        "stage_priority": _STAGE_PRIORITY.get(stage, 99),
        "severity": severity,
        "owner": owner,
        "confidence": confidence,
        "action": action,
        "blockers": blockers,
        "evidence_sources": _present_sources(slot),
        "exchange": identity["exchange"],
        "symbol": identity["symbol"],
        "timeframe": identity["timeframe"],
        "strategy_id": identity["strategy_id"],
        "trial_id": identity["trial_id"],
        "evidence": evidence,
        "metrics": metrics,
        "can_trade": False,
        "can_promote": False,
    }


def _classify(
    *,
    evidence: Mapping[str, Any],
    activation: Mapping[str, Any],
    route: Mapping[str, Any],
    cadence: Mapping[str, Any],
    performance: Mapping[str, Any],
    exit_autopsy: Mapping[str, Any],
    entry_autopsy: Mapping[str, Any],
    survival: Mapping[str, Any],
    governor: Mapping[str, Any],
    paper_profile: Mapping[str, Any],
    live_profile: Mapping[str, Any],
    causality: Mapping[str, Any],
) -> tuple[str, str, str, str, str, float, list[str]]:
    activation_state = _text(evidence.get("activation_state"))
    route_state = _text(evidence.get("route_state"))
    cadence_state = _text(evidence.get("cadence_state"))
    paper_state = _text(evidence.get("paper_profile_state"))
    live_state = _text(evidence.get("live_profile_state"))
    entry_state = _text(evidence.get("entry_state"))
    exit_driver = _text(evidence.get("exit_driver"))
    performance_state = _text(evidence.get("performance_state"))
    survival_state = _text(evidence.get("survival_state"))
    governor_bucket = _text(evidence.get("governor_bucket"))
    scanner_state = _text(evidence.get("scanner_state"))
    paper_decision = _text(evidence.get("paper_decision"))
    closed = int(_num(performance.get("closed_trades")))
    net = _num(performance.get("net_pnl_usd"))

    if route_state in _ROUTE_BLOCKERS or activation_state in _ACTIVATION_BLOCKERS:
        return (
            STAGE_ROUTE,
            ROOT_ROUTE_OR_JOURNAL_BROKEN,
            "P1",
            "system",
            _first(
                route.get("next_action"),
                activation.get("next_action"),
                "repair route/journal proof before judging strategy edge",
            ),
            0.95,
            _compact([route_state, activation_state]),
        )
    if cadence_state in _CADENCE_BLOCKERS:
        return (
            STAGE_CADENCE,
            ROOT_EVALUATION_CADENCE_BROKEN,
            "P1",
            "system",
            _first(
                cadence.get("next_action"),
                "restore fresh lane_eval cadence before trusting no-signal counts",
            ),
            0.92,
            _compact([cadence_state, route_state]),
        )
    if paper_state in _PROFILE_BLOCKERS or live_state in _PROFILE_BLOCKERS:
        return (
            STAGE_SIZING,
            ROOT_SIZING_PROFILE_BLOCKED,
            "P2",
            "operator",
            _first(
                paper_profile.get("next_action"),
                live_profile.get("next_action"),
                "fix margin/leverage/venue sizing profile before paper review",
            ),
            0.9,
            _compact(
                [paper_state, live_state]
                + list(paper_profile.get("blockers") or [])
                + list(live_profile.get("blockers") or [])
            ),
        )
    if entry_state in _ENTRY_BLOCKERS:
        return (
            STAGE_ENTRY,
            ROOT_ENTRY_QUALITY_BLOCKED,
            "P2",
            "strategy",
            _first(
                entry_autopsy.get("next_action"),
                "repair entry timing/context so signal clears fee wall before routing",
            ),
            0.88,
            _compact([entry_state, entry_autopsy.get("primary_failure")]),
        )
    if exit_driver in _EXIT_BLOCKERS:
        return (
            STAGE_EXIT,
            ROOT_EXIT_OR_CAPTURE_BLOCKED,
            "P2" if exit_driver != "LEDGER_OR_EXIT_METADATA_GAP" else "P1",
            "strategy",
            _first(
                exit_autopsy.get("next_action"),
                "repair stop/TP/trailing capture before promotion review",
            ),
            0.86,
            _compact([exit_driver] + list(exit_autopsy.get("blockers") or [])),
        )
    if performance_state == "PAPER_ACTIVE_NEGATIVE" or (closed > 0 and net < 0):
        return (
            STAGE_PERFORMANCE,
            ROOT_NEGATIVE_AFTER_COST,
            "P2",
            "strategy",
            _first(
                performance.get("next_action"),
                "mine entry/exit failure distribution before adding paper size",
            ),
            0.82,
            _compact([performance_state, f"net_pnl_usd={net:.4f}"]),
        )
    if governor_bucket in _DEMOTION_STATES or survival_state in _DEMOTION_STATES:
        return (
            STAGE_GOVERNANCE,
            ROOT_GOVERNOR_DEMOTION_QUEUE,
            "P2",
            "operator",
            _first(
                governor.get("action"),
                survival.get("next_action"),
                "remove or repair this paper lane before more exposure",
            ),
            0.84,
            _compact([governor_bucket, survival_state]),
        )
    if (
        performance_state in _REVIEW_STATES
        or survival_state in _REVIEW_STATES
        or governor_bucket in _REVIEW_STATES
        or paper_decision in _REVIEW_STATES
    ):
        return (
            STAGE_GOVERNANCE,
            ROOT_READY_FOR_HUMAN_REVIEW,
            "P3",
            "human",
            _first(
                governor.get("action"),
                survival.get("next_action"),
                performance.get("next_action"),
                "review evidence; this report cannot approve or promote",
            ),
            0.8,
            _compact([governor_bucket, survival_state, performance_state, paper_decision]),
        )
    if closed <= 0 and (
        performance_state == "PAPER_ONLINE_NO_TRADES"
        or exit_driver == "NO_CLOSED_TRADES"
        or activation_state in _WAITING_STATES
    ):
        return (
            STAGE_MARKET,
            ROOT_NO_CLOSED_PAPER_TRADES,
            "P3",
            "market",
            _first(
                performance.get("next_action"),
                causality.get("why_no_trade_minute"),
                "keep paper lane online until it records closed outcome proof",
            ),
            0.76,
            _compact([performance_state, exit_driver, activation_state]),
        )
    if scanner_state in _WAITING_STATES or paper_decision in _WAITING_STATES:
        return (
            STAGE_MARKET,
            ROOT_MARKET_NOT_TRIGGERING,
            "P3",
            "market",
            _first(
                causality.get("why_no_trade_minute"),
                activation.get("next_action"),
                "wait for a qualified market setup; do not loosen gates mid-trial",
            ),
            0.72,
            _compact([scanner_state, paper_decision]),
        )
    return (
        STAGE_EVIDENCE,
        ROOT_EVIDENCE_INCOMPLETE,
        "P3",
        "system",
        "publish activation, cadence, performance, and autopsy evidence for this lane",
        0.55,
        _compact([activation_state, route_state, cadence_state, performance_state]),
    )


def _evidence_snapshot(**sources: Mapping[str, Any]) -> dict[str, Any]:
    activation = sources["activation"]
    route = sources["route"]
    cadence = sources["cadence"]
    performance = sources["performance"]
    exit_autopsy = sources["exit_autopsy"]
    entry_autopsy = sources["entry_autopsy"]
    survival = sources["survival"]
    governor = sources["governor"]
    paper_profile = sources["paper_profile"]
    live_profile = sources["live_profile"]
    causality = sources["causality"]
    paper_decision = _map(causality.get("paper_decision"))

    return {
        "activation_state": _text(activation.get("activation_state")) or None,
        "route_state": _text(route.get("doctor_state")) or None,
        "cadence_state": _text(cadence.get("cadence_state")) or None,
        "paper_profile_state": _text(paper_profile.get("profile_state")) or None,
        "live_profile_state": _text(live_profile.get("profile_state")) or None,
        "entry_state": _entry_state(entry_autopsy),
        "performance_state": _text(performance.get("state")) or None,
        "exit_driver": _text(exit_autopsy.get("loss_driver")) or None,
        "survival_state": _text(survival.get("survival_state")) or None,
        "governor_bucket": _text(governor.get("governor_bucket")) or None,
        "scanner_state": _text(causality.get("scanner_state")) or None,
        "paper_decision": _text(paper_decision.get("state")) or None,
        "primary_blocker": _map(causality.get("primary_blocker")),
        "latest_why_no_trade": _first(
            performance.get("latest_why_no_trade"),
            cadence.get("latest_why"),
            causality.get("why_no_trade_minute"),
        )
        or None,
    }


def _entry_state(row: Mapping[str, Any]) -> str | None:
    for key in (
        "entry_state",
        "autopsy_state",
        "root_cause",
        "primary_failure",
        "state",
        "loss_driver",
        "decision",
    ):
        value = _text(row.get(key))
        if value:
            return value
    return None


def _metrics(
    performance: Mapping[str, Any],
    exit_autopsy: Mapping[str, Any],
    cadence: Mapping[str, Any],
    causality: Mapping[str, Any],
) -> dict[str, Any]:
    counts = _map(cadence.get("counts"))
    return {
        "closed_trades": int(_num(performance.get("closed_trades"))),
        "net_pnl_usd": round(_num(performance.get("net_pnl_usd")), 6),
        "fees_usd": round(_num(performance.get("fees_usd")), 6),
        "profit_factor": _num(performance.get("profit_factor")),
        "avg_net_bps": _num(exit_autopsy.get("avg_net_bps")),
        "avg_fee_bps": _num(exit_autopsy.get("avg_fee_bps")),
        "stop_rate": _num(exit_autopsy.get("stop_rate")),
        "take_profit_rate": _num(exit_autopsy.get("take_profit_rate")),
        "live_evals": int(
            _num(performance.get("live_evals")) or _num(counts.get("live_evals"))
        ),
        "live_signals": int(
            _num(performance.get("live_signals")) or _num(counts.get("signals"))
        ),
        "paper_order_intents": int(_num(performance.get("paper_order_intents"))),
        "readiness_score": _num(_map(causality.get("gate_diagnostics")).get("readiness_score")),
    }


def _identity(slot: Mapping[str, Any], *rows: Mapping[str, Any]) -> dict[str, str]:
    return {
        "strategy_id": _first(slot.get("strategy_id"), *(r.get("strategy_id") for r in rows)),
        "exchange": _first(slot.get("exchange"), *(r.get("exchange") for r in rows)),
        "symbol": _first(slot.get("symbol"), *(r.get("symbol") for r in rows)),
        "timeframe": _first(slot.get("timeframe"), *(r.get("timeframe") for r in rows)),
        "trial_id": _first(slot.get("trial_id"), *(r.get("trial_id") for r in rows)),
    }


def _add_rows(slots: dict[str, dict[str, Any]], kind: str, rows: Any) -> None:
    for raw in rows or []:
        if not isinstance(raw, Mapping):
            continue
        row = dict(raw)
        key = _row_key(row)
        if not key:
            continue
        slot = slots.setdefault(key, {"lane_key": key, "profiles": {}})
        if kind == "profile":
            profile_name = _text(row.get("profile")) or "unknown"
            slot["profiles"][profile_name] = row
        else:
            slot[kind] = row
        slot.setdefault("seed", row)
        for field in ("strategy_id", "exchange", "symbol", "timeframe", "trial_id"):
            if slot.get(field) in (None, "") and row.get(field) not in (None, ""):
                slot[field] = row.get(field)


def _row_key(row: Mapping[str, Any]) -> str:
    lane_key = _text(row.get("lane_key"))
    if lane_key:
        return lane_key.lower()
    strategy = _text(row.get("strategy_id"))
    exchange = _text(row.get("exchange"))
    symbol = _text(row.get("symbol")).upper()
    timeframe = _text(row.get("timeframe")).lower()
    if strategy and exchange and symbol and timeframe:
        return "|".join((strategy.lower(), exchange.lower(), symbol, timeframe))
    for fallback in ("expected_lane_id", "lane_id", "trial_id", "id"):
        value = _text(row.get(fallback))
        if value:
            return value.lower()
    return ""


def _present_sources(slot: Mapping[str, Any]) -> list[str]:
    ordered = [
        "activation",
        "route",
        "cadence",
        "profile",
        "entry_autopsy",
        "performance",
        "exit_autopsy",
        "survival",
        "governor",
        "causality",
    ]
    return [name for name in ordered if slot.get(name) or (name == "profile" and slot.get("profiles"))]


def _row_sort_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    severity_rank = {"P1": 0, "P2": 1, "P3": 2}.get(_text(row.get("severity")), 9)
    metrics = _map(row.get("metrics"))
    return (
        severity_rank,
        int(row.get("stage_priority") or 99),
        0 if _num(metrics.get("net_pnl_usd")) < 0 else 1,
        -int(_num(metrics.get("closed_trades"))),
        _text(row.get("strategy_id")),
        _text(row.get("exchange")),
        _text(row.get("symbol")),
    )


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    roots = Counter(_text(r.get("root_cause")) for r in rows)
    stages = Counter(_text(r.get("stage")) for r in rows)
    severities = Counter(_text(r.get("severity")) for r in rows)
    owners = Counter(_text(r.get("owner")) for r in rows)
    closed = sum(int(_num(_map(r.get("metrics")).get("closed_trades"))) for r in rows)
    net = sum(_num(_map(r.get("metrics")).get("net_pnl_usd")) for r in rows)
    return {
        "total_lanes": len(rows),
        "p1": severities["P1"],
        "p2": severities["P2"],
        "p3": severities["P3"],
        "route_blocked": roots[ROOT_ROUTE_OR_JOURNAL_BROKEN],
        "cadence_blocked": roots[ROOT_EVALUATION_CADENCE_BROKEN],
        "sizing_blocked": roots[ROOT_SIZING_PROFILE_BLOCKED],
        "entry_blocked": roots[ROOT_ENTRY_QUALITY_BLOCKED],
        "exit_blocked": roots[ROOT_EXIT_OR_CAPTURE_BLOCKED],
        "negative_after_cost": roots[ROOT_NEGATIVE_AFTER_COST],
        "no_closed_paper_trades": roots[ROOT_NO_CLOSED_PAPER_TRADES],
        "demotion_queue": roots[ROOT_GOVERNOR_DEMOTION_QUEUE],
        "review_candidates": roots[ROOT_READY_FOR_HUMAN_REVIEW],
        "market_not_triggering": roots[ROOT_MARKET_NOT_TRIGGERING],
        "evidence_incomplete": roots[ROOT_EVIDENCE_INCOMPLETE],
        "closed_trades": closed,
        "net_pnl_usd": round(net, 6),
        "root_cause_counts": dict(sorted(roots.items())),
        "stage_counts": dict(sorted(stages.items())),
        "severity_counts": dict(sorted(severities.items())),
        "owner_counts": dict(sorted(owners.items())),
        "can_trade": False,
        "can_promote": False,
    }


def _boards(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def slim(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "lane_key": row.get("lane_key"),
            "root_cause": row.get("root_cause"),
            "stage": row.get("stage"),
            "severity": row.get("severity"),
            "owner": row.get("owner"),
            "confidence": row.get("confidence"),
            "exchange": row.get("exchange"),
            "symbol": row.get("symbol"),
            "timeframe": row.get("timeframe"),
            "strategy_id": row.get("strategy_id"),
            "action": row.get("action"),
            "blockers": row.get("blockers"),
            "metrics": row.get("metrics"),
        }

    return {
        "top_blockers": [slim(r) for r in rows[:15]],
        "repair_first": [
            slim(r)
            for r in rows
            if r.get("stage") in {STAGE_ROUTE, STAGE_CADENCE, STAGE_SIZING}
        ][:15],
        "entry_exit_work": [
            slim(r) for r in rows if r.get("stage") in {STAGE_ENTRY, STAGE_EXIT}
        ][:15],
        "negative_after_cost": [
            slim(r) for r in rows if r.get("root_cause") == ROOT_NEGATIVE_AFTER_COST
        ][:15],
        "paper_review": [
            slim(r) for r in rows if r.get("root_cause") == ROOT_READY_FOR_HUMAN_REVIEW
        ][:15],
    }


def _operator_answer(summary: Mapping[str, Any]) -> str:
    if int(summary.get("route_blocked") or 0):
        return (
            f"{summary.get('route_blocked')} lane(s) have route/journal failures; "
            "repair those before judging alpha."
        )
    if int(summary.get("cadence_blocked") or 0):
        return (
            f"{summary.get('cadence_blocked')} lane(s) are stale; signal frequency "
            "is not trustworthy until cadence is restored."
        )
    if int(summary.get("entry_blocked") or 0) or int(summary.get("exit_blocked") or 0):
        return (
            f"{summary.get('entry_blocked')} entry and {summary.get('exit_blocked')} "
            "exit/capture issue(s) explain the next strategy fixes."
        )
    if int(summary.get("negative_after_cost") or 0):
        return (
            f"{summary.get('negative_after_cost')} lane(s) are negative after costs; "
            "mine entry/exit distributions before adding exposure."
        )
    if int(summary.get("review_candidates") or 0):
        return (
            f"{summary.get('review_candidates')} lane(s) are ready for human paper review; "
            "this report cannot approve or promote them."
        )
    if int(summary.get("no_closed_paper_trades") or 0):
        return (
            f"{summary.get('no_closed_paper_trades')} lane(s) are online but need closed "
            "paper outcomes before judgment."
        )
    return "No decisive lane blocker is published yet; wait for the upstream truth boards."


def _payload(value: Mapping[str, Any] | None, path: Path | str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return _read_json(Path(path), {"rows": [], "summary": {}})


def _read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        if not path.exists():
            return dict(default)
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else dict(default)
    except Exception:
        return dict(default)


def _map(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _first(*values: Any) -> str:
    for value in values:
        text = _text(value)
        if text:
            return text
    return ""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _num(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _compact(values: list[Any]) -> list[str]:
    out: list[str] = []
    for value in values:
        text = _text(value)
        if text and text not in out:
            out.append(text)
    return out


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--activation", type=Path, default=DEFAULT_ACTIVATION)
    parser.add_argument("--route", type=Path, default=DEFAULT_ROUTE)
    parser.add_argument("--cadence", type=Path, default=DEFAULT_CADENCE)
    parser.add_argument("--performance", type=Path, default=DEFAULT_PERFORMANCE)
    parser.add_argument("--exit-autopsy", type=Path, default=DEFAULT_EXIT_AUTOPSY)
    parser.add_argument("--entry-autopsy", type=Path, default=DEFAULT_ENTRY_AUTOPSY)
    parser.add_argument("--survival", type=Path, default=DEFAULT_SURVIVAL)
    parser.add_argument("--governor", type=Path, default=DEFAULT_GOVERNOR)
    parser.add_argument("--causality", type=Path, default=DEFAULT_CAUSALITY)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--feed", type=Path, default=DEFAULT_FEED)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval-seconds", type=float, default=60.0)
    parser.add_argument("--max-rows", type=int, default=240)
    parser.add_argument("--print", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    from vnedge.research.trade_profile_matrix import build_trade_profile_matrix

    config = PaperLaneRootCauseConfig(max_rows=args.max_rows)
    while True:
        activation = _read_json(args.activation, {"rows": [], "summary": {}})
        payload = build_paper_lane_root_cause(
            activation=activation,
            route_path=args.route,
            cadence_path=args.cadence,
            performance_path=args.performance,
            exit_autopsy_path=args.exit_autopsy,
            entry_autopsy_path=args.entry_autopsy,
            survival_path=args.survival,
            governor_path=args.governor,
            profile=build_trade_profile_matrix(activation),
            causality_path=args.causality,
            config=config,
        )
        publish_paper_lane_root_cause(payload, args.out, args.feed)
        if args.print:
            print(render_report(payload), flush=True)
        if args.once:
            return 0
        time.sleep(max(1.0, float(args.interval_seconds)))


if __name__ == "__main__":
    raise SystemExit(main())
