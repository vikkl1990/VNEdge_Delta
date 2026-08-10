"""Operator action queue.

The paper/scanner ladder now emits several truthful but separate reports:
activation, route doctor, cadence, trade profiles, performance, exit autopsy,
and lane causality. This module joins those read-only facts into one ranked
"what next" queue for the operator. It never starts runners, edits manifests,
promotes lanes, or trades.
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
DEFAULT_CONTRACT_RECONCILER = (
    DEFAULT_RESEARCH_DIR / "paper_trade_contract_reconciler_latest.json"
)
DEFAULT_CAUSALITY = DEFAULT_RESEARCH_DIR / "lane_firing_causality_latest.json"
DEFAULT_OUT = DEFAULT_RESEARCH_DIR / "operator_actions_latest.json"
DEFAULT_FEED = DEFAULT_RESEARCH_DIR / "operator_actions_feed.jsonl"

ACTION_REPAIR_ROUTE = "REPAIR_ROUTE"
ACTION_RESTORE_CADENCE = "RESTORE_CADENCE"
ACTION_FIX_SIZE_PROFILE = "FIX_SIZE_PROFILE"
ACTION_REPAIR_CONTRACT = "REPAIR_PAPER_CONTRACT"
ACTION_FIX_EXIT_QUALITY = "FIX_EXIT_QUALITY"
ACTION_REVIEW_PAPER_CANDIDATE = "REVIEW_PAPER_CANDIDATE"
ACTION_MINE_CLEAN_ALPHA = "MINE_CLEAN_ALPHA"
ACTION_COLLECT_OUTCOMES = "COLLECT_OUTCOMES"
ACTION_WAIT_FOR_SIGNAL = "WAIT_FOR_SIGNAL"
ACTION_OBSERVE = "OBSERVE"

_ACTION_PRIORITY = {
    ACTION_REPAIR_ROUTE: 0,
    ACTION_RESTORE_CADENCE: 1,
    ACTION_FIX_SIZE_PROFILE: 2,
    ACTION_REPAIR_CONTRACT: 3,
    ACTION_FIX_EXIT_QUALITY: 4,
    ACTION_REVIEW_PAPER_CANDIDATE: 5,
    ACTION_MINE_CLEAN_ALPHA: 6,
    ACTION_COLLECT_OUTCOMES: 7,
    ACTION_WAIT_FOR_SIGNAL: 8,
    ACTION_OBSERVE: 9,
}

_ROUTE_REPAIR_STATES = {
    "RUNNER_SERVICE_DOWN",
    "ROUTE_READY_JOURNAL_MISSING",
    "ROUTE_NOT_WIRED",
    "MANIFEST_UNSAFE",
    "BLOCKED_NEGATIVE",
    "ROUTE_BLOCKED",
}
_ACTIVATION_REPAIR_STATES = {
    "ROUTE_BLOCKED",
    "MANIFEST_UNSAFE",
    "BLOCKED_NEGATIVE_EDGE",
    "NEEDS_RUNTIME_ADAPTER",
}
_CADENCE_REPAIR_STATES = {
    "EVAL_STALE",
    "HEARTBEAT_STALE",
    "JOURNAL_MISSING",
    "JOURNAL_STALE",
}
_PROFILE_REPAIR_STATES = {
    "PAPER_PROFILE_BLOCKED_BY_RISK",
    "LIVE_PROFILE_BLOCKED_BY_RISK",
    "PROFILE_MISSING",
}
_WAITING_SCANNER_STATES = {"FIRING", "NEAR_TRIGGER"}
_PAPER_WAITING_STATES = {
    "PAPER_RUNNING",
    "PAPER_ONLINE_WAITING",
    "PAPER_ROUTE_READY_NO_JOURNAL",
}
_EXIT_QUALITY_STATES = {
    "STOP_DOMINATED",
    "FEE_WALL_DOMINATED",
    "TIMEOUT_DOMINATED",
    "TP_CAPTURE_WEAK",
    "NEGATIVE_EDGE",
    "LEDGER_OR_EXIT_METADATA_GAP",
}
_CONTRACT_REPAIR_STATES = {
    "CONTRACT_BROKEN",
    "FEE_WALL_BREACH",
}
_CLEAN_ALPHA_STATES = {
    "CONTRACT_OK_NEGATIVE_ALPHA",
    "CONTRACT_OK_EDGE_DEFICIT",
}


@dataclass(frozen=True)
class OperatorActionConfig:
    max_rows: int = 180

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_operator_actions(
    *,
    activation: Mapping[str, Any] | None = None,
    route: Mapping[str, Any] | None = None,
    cadence: Mapping[str, Any] | None = None,
    performance: Mapping[str, Any] | None = None,
    exit_autopsy: Mapping[str, Any] | None = None,
    contract_reconciler: Mapping[str, Any] | None = None,
    profile: Mapping[str, Any] | None = None,
    causality: Mapping[str, Any] | None = None,
    activation_path: Path | str = DEFAULT_ACTIVATION,
    route_path: Path | str = DEFAULT_ROUTE,
    cadence_path: Path | str = DEFAULT_CADENCE,
    performance_path: Path | str = DEFAULT_PERFORMANCE,
    exit_autopsy_path: Path | str = DEFAULT_EXIT_AUTOPSY,
    contract_reconciler_path: Path | str = DEFAULT_CONTRACT_RECONCILER,
    causality_path: Path | str = DEFAULT_CAUSALITY,
    config: OperatorActionConfig = OperatorActionConfig(),
    now: datetime | None = None,
) -> dict[str, Any]:
    """Join read-only lane artifacts into one ranked action queue."""
    now = now or datetime.now(UTC)
    activation_payload = _payload(activation, activation_path)
    route_payload = _payload(route, route_path)
    cadence_payload = _payload(cadence, cadence_path)
    performance_payload = _payload(performance, performance_path)
    exit_autopsy_payload = _payload(exit_autopsy, exit_autopsy_path)
    contract_payload = _payload(contract_reconciler, contract_reconciler_path)
    profile_payload = profile if isinstance(profile, Mapping) else {"rows": []}
    causality_payload = _payload(causality, causality_path)

    slots: dict[str, dict[str, Any]] = {}
    _add_rows(slots, "activation", activation_payload.get("rows", []))
    _add_rows(slots, "route", route_payload.get("rows", []))
    _add_rows(slots, "cadence", cadence_payload.get("rows", []))
    _add_rows(slots, "performance", performance_payload.get("rows", []))
    _add_rows(slots, "exit_autopsy", exit_autopsy_payload.get("rows", []))
    _add_rows(slots, "contract_reconciler", contract_payload.get("rows", []))
    _add_rows(slots, "profile", profile_payload.get("rows", []))
    _add_rows(slots, "causality", causality_payload.get("rows", []))

    rows = [_action_row(slot) for slot in slots.values()]
    rows.sort(key=_row_sort_key)
    rows = rows[: max(1, int(config.max_rows))]
    summary = _summary(rows)
    return {
        "generated_at": now.isoformat(),
        "report_id": "operator_actions_v1",
        "mode": "read_only_operator_action_queue",
        "source_report_ids": {
            "activation": activation_payload.get("report_id"),
            "route": route_payload.get("report_id"),
            "cadence": cadence_payload.get("report_id"),
            "performance": performance_payload.get("report_id"),
            "exit_autopsy": exit_autopsy_payload.get("report_id"),
            "contract_reconciler": contract_payload.get("report_id"),
            "profile": profile_payload.get("report_id"),
            "causality": causality_payload.get("report_id"),
        },
        "inputs": {
            "activation_path": str(activation_path),
            "route_path": str(route_path),
            "cadence_path": str(cadence_path),
            "performance_path": str(performance_path),
            "exit_autopsy_path": str(exit_autopsy_path),
            "contract_reconciler_path": str(contract_reconciler_path),
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
            "can_restart_runner": False,
            "can_apply_profile": False,
            "human_paper_review_is_not_promotion": True,
        },
        "can_trade": False,
        "can_promote": False,
    }


def publish_operator_actions(
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


def render_report(payload: Mapping[str, Any], *, limit: int = 40) -> str:
    summary = payload.get("summary", {})
    lines = [
        "=== Operator action queue ===",
        f"generated: {payload.get('generated_at')}",
        str(payload.get("operator_answer") or ""),
        (
            "summary: "
            f"{summary.get('total_rows', 0)} lanes, "
            f"{summary.get('repair_first', 0)} repair-first, "
            f"{summary.get('paper_review', 0)} review, "
            f"{summary.get('collect_outcomes', 0)} collect, "
            f"{summary.get('wait_or_observe', 0)} wait/observe"
        ),
    ]
    for row in list(payload.get("rows", []))[:limit]:
        lines.append(
            f"  {row.get('bucket', ''):<24} {row.get('owner', ''):<8} "
            f"{row.get('exchange', ''):<14} {row.get('symbol', ''):<14} "
            f"{row.get('timeframe', ''):<3} {row.get('strategy_id', ''):<28} "
            f"{row.get('action', '')}"
        )
    lines.append("read-only: can_trade=false can_promote=false")
    return "\n".join(lines)


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
            profile_name = str(row.get("profile") or "unknown")
            slot["profiles"][profile_name] = row
        else:
            slot[kind] = row
        slot.setdefault("seed", row)
        for field in ("strategy_id", "exchange", "symbol", "timeframe", "trial_id"):
            if slot.get(field) in (None, "") and row.get(field) not in (None, ""):
                slot[field] = row.get(field)


def _row_key(row: Mapping[str, Any]) -> str:
    lane_key = str(row.get("lane_key") or "").strip()
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


def _action_row(slot: Mapping[str, Any]) -> dict[str, Any]:
    activation = _map(slot.get("activation"))
    route = _map(slot.get("route"))
    cadence = _map(slot.get("cadence"))
    performance = _map(slot.get("performance"))
    exit_autopsy = _map(slot.get("exit_autopsy"))
    contract = _map(slot.get("contract_reconciler"))
    causality = _map(slot.get("causality"))
    profiles = _map(slot.get("profiles"))
    paper_profile = _map(profiles.get("paper"))
    live_profile = _map(profiles.get("live"))

    strategy_id = _first(
        slot.get("strategy_id"),
        activation.get("strategy_id"),
        route.get("strategy_id"),
        cadence.get("strategy_id"),
        performance.get("strategy_id"),
        exit_autopsy.get("strategy_id"),
        contract.get("strategy_id"),
        causality.get("strategy_id"),
    )
    exchange = _first(
        slot.get("exchange"),
        activation.get("exchange"),
        route.get("exchange"),
        cadence.get("exchange"),
        performance.get("exchange"),
        exit_autopsy.get("exchange"),
        contract.get("exchange"),
        causality.get("exchange"),
    )
    symbol = _first(
        slot.get("symbol"),
        activation.get("symbol"),
        route.get("symbol"),
        cadence.get("symbol"),
        performance.get("symbol"),
        exit_autopsy.get("symbol"),
        contract.get("symbol"),
        causality.get("symbol"),
    )
    timeframe = _first(
        slot.get("timeframe"),
        activation.get("timeframe"),
        route.get("timeframe"),
        cadence.get("timeframe"),
        performance.get("timeframe"),
        exit_autopsy.get("timeframe"),
        contract.get("timeframe"),
        causality.get("timeframe"),
    )

    activation_state = _text(activation.get("activation_state"))
    route_state = _text(route.get("doctor_state"))
    cadence_state = _text(cadence.get("cadence_state"))
    performance_state = _text(performance.get("state"))
    exit_driver = _text(exit_autopsy.get("loss_driver"))
    contract_verdict = _text(contract.get("verdict"))
    scanner_state = _text(causality.get("scanner_state"))
    paper_state = _text(paper_profile.get("profile_state"))
    live_state = _text(live_profile.get("profile_state"))
    paper_decision = _map(causality.get("paper_decision"))
    paper_decision_state = _text(paper_decision.get("state"))

    bucket = ACTION_OBSERVE
    owner = "system"
    severity = "P3"
    action = _first(
        activation.get("next_action"),
        route.get("next_action"),
        cadence.get("next_action"),
        performance.get("next_action"),
        _map(causality.get("primary_blocker")).get("action"),
        "observe lane",
    )
    reason = action

    if route_state in _ROUTE_REPAIR_STATES or activation_state in _ACTIVATION_REPAIR_STATES:
        bucket = ACTION_REPAIR_ROUTE
        owner = "system"
        severity = "P1"
        action = _first(
            route.get("next_action"),
            activation.get("next_action"),
            "repair paper route or manifest before judging this lane",
        )
        reason = _first(route_state, activation_state, action)
    elif cadence_state in _CADENCE_REPAIR_STATES or route_state in _CADENCE_REPAIR_STATES:
        bucket = ACTION_RESTORE_CADENCE
        owner = "system"
        severity = "P1"
        action = _first(
            cadence.get("next_action"),
            route.get("next_action"),
            "restore fresh lane evaluations before trusting signal frequency",
        )
        reason = _first(cadence_state, route_state)
    elif paper_state in _PROFILE_REPAIR_STATES or live_state in _PROFILE_REPAIR_STATES:
        bucket = ACTION_FIX_SIZE_PROFILE
        owner = "operator"
        severity = "P2"
        action = _first(
            paper_profile.get("next_action"),
            live_profile.get("next_action"),
            "adjust margin/leverage profile before route review",
        )
        reason = _first(paper_state, live_state, action)
    elif contract_verdict in _CONTRACT_REPAIR_STATES:
        bucket = ACTION_REPAIR_CONTRACT
        owner = "system"
        severity = "P1" if contract_verdict == "CONTRACT_BROKEN" else "P2"
        action = _first(
            contract.get("next_action"),
            "repair paper trade contract before alpha review",
        )
        reason = _first(contract_verdict, action)
    elif exit_driver in _EXIT_QUALITY_STATES:
        bucket = ACTION_FIX_EXIT_QUALITY
        owner = "system"
        severity = "P1" if exit_driver == "LEDGER_OR_EXIT_METADATA_GAP" else "P2"
        action = _first(
            exit_autopsy.get("next_action"),
            "repair paper exit quality before promotion review",
        )
        reason = _first(exit_driver, action)
    elif contract_verdict in _CLEAN_ALPHA_STATES:
        bucket = ACTION_MINE_CLEAN_ALPHA
        owner = "research"
        severity = "P2"
        action = _first(
            contract.get("next_action"),
            "contract is clean; mine entry/exit alpha instead of repairing runtime",
        )
        reason = _first(contract_verdict, action)
    elif (
        performance_state == "PAPER_PROMOTION_CANDIDATE"
        or paper_decision_state == "READY_FOR_PAPER_REVIEW"
    ):
        bucket = ACTION_REVIEW_PAPER_CANDIDATE
        owner = "human"
        severity = "P2"
        action = _first(
            performance.get("next_action"),
            paper_decision.get("action"),
            "review paper evidence for the next promotion step",
        )
        reason = _first(performance_state, paper_decision_state, action)
    elif performance_state in {
        "PAPER_ACTIVE_PROFITABLE",
        "PAPER_ACTIVE_NEGATIVE",
        "PAPER_ACTIVE_FLAT",
        "PAPER_ONLINE_NO_TRADES",
    } or _num(performance.get("closed_trades")) > 0:
        bucket = ACTION_COLLECT_OUTCOMES
        owner = "system"
        severity = "P2" if performance_state == "PAPER_ACTIVE_NEGATIVE" else "P3"
        action = _first(
            performance.get("next_action"),
            "collect paper outcomes until the lane clears promotion gates",
        )
        reason = _first(performance_state, action)
    elif (
        scanner_state in _WAITING_SCANNER_STATES
        or activation_state in _PAPER_WAITING_STATES
        or paper_decision_state == "PAPER_WAITING_FOR_SIGNAL"
    ):
        bucket = ACTION_WAIT_FOR_SIGNAL
        owner = "market"
        severity = "P3"
        action = _first(
            causality.get("why_no_trade_minute"),
            activation.get("next_action"),
            paper_decision.get("action"),
            "wait for a qualified signal and journal proof",
        )
        reason = _first(scanner_state, activation_state, paper_decision_state, action)

    closed = int(_num(performance.get("closed_trades")))
    net = _num(performance.get("net_pnl_usd"))
    return {
        "lane_key": _text(slot.get("lane_key")) or _row_key(slot),
        "bucket": bucket,
        "priority": _ACTION_PRIORITY.get(bucket, 9),
        "severity": severity,
        "owner": owner,
        "action": str(action),
        "reason": str(reason),
        "exchange": exchange,
        "symbol": symbol,
        "timeframe": timeframe,
        "strategy_id": strategy_id,
        "trial_id": _first(
            slot.get("trial_id"),
            activation.get("trial_id"),
            route.get("trial_id"),
            cadence.get("trial_id"),
        ),
        "evidence": {
            "activation_state": activation_state or None,
            "route_state": route_state or None,
            "cadence_state": cadence_state or None,
            "paper_profile_state": paper_state or None,
            "live_profile_state": live_state or None,
            "performance_state": performance_state or None,
            "exit_driver": exit_driver or None,
            "contract_verdict": contract_verdict or None,
            "contract_violations": _map(contract.get("top_violations")),
            "contract_warnings": _map(contract.get("top_warnings")),
            "scanner_state": scanner_state or None,
            "paper_decision": paper_decision_state or None,
            "primary_blocker": _map(causality.get("primary_blocker")),
            "latest_why_no_trade": _first(
                performance.get("latest_why_no_trade"),
                cadence.get("latest_why"),
                causality.get("why_no_trade_minute"),
            ),
        },
        "metrics": {
            "closed_trades": closed,
            "net_pnl_usd": round(net, 6),
            "profit_factor": _num(performance.get("profit_factor")),
            "live_evals": int(
                _num(performance.get("live_evals"))
                or _num(_map(cadence.get("counts")).get("live_evals"))
            ),
            "live_signals": int(
                _num(performance.get("live_signals"))
                or _num(_map(cadence.get("counts")).get("signals"))
            ),
            "paper_order_intents": int(_num(performance.get("paper_order_intents"))),
            "avg_net_bps": _num(exit_autopsy.get("avg_net_bps")),
            "stop_rate": _num(exit_autopsy.get("stop_rate")),
            "take_profit_rate": _num(exit_autopsy.get("take_profit_rate")),
            "contract_avg_net_bps": _num(contract.get("avg_net_bps")),
            "contract_avg_fee_bps": _num(contract.get("avg_fee_bps")),
            "critical_contract_violations": int(_num(contract.get("critical_violations"))),
            "contract_fee_wall_breaches": int(_num(contract.get("fee_wall_breaches"))),
        },
        "can_trade": False,
        "can_promote": False,
    }


def _row_sort_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        int(row.get("priority") or 9),
        str(row.get("severity") or "P9"),
        0 if _num(_map(row.get("metrics")).get("net_pnl_usd")) < 0 else 1,
        str(row.get("strategy_id") or ""),
        str(row.get("exchange") or ""),
        str(row.get("symbol") or ""),
    )


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    buckets = Counter(str(r.get("bucket") or "") for r in rows)
    owners = Counter(str(r.get("owner") or "") for r in rows)
    negative = sum(
        1
        for r in rows
        if _map(r.get("evidence")).get("performance_state") == "PAPER_ACTIVE_NEGATIVE"
    )
    repair_first = sum(
        buckets[b]
        for b in (
            ACTION_REPAIR_ROUTE,
            ACTION_RESTORE_CADENCE,
            ACTION_FIX_SIZE_PROFILE,
            ACTION_REPAIR_CONTRACT,
        )
    )
    return {
        "total_rows": len(rows),
        "repair_first": repair_first,
        "route_repairs": buckets[ACTION_REPAIR_ROUTE],
        "cadence_repairs": buckets[ACTION_RESTORE_CADENCE],
        "profile_fixes": buckets[ACTION_FIX_SIZE_PROFILE],
        "contract_repairs": buckets[ACTION_REPAIR_CONTRACT],
        "exit_quality_fixes": buckets[ACTION_FIX_EXIT_QUALITY],
        "paper_review": buckets[ACTION_REVIEW_PAPER_CANDIDATE],
        "clean_alpha_mining": buckets[ACTION_MINE_CLEAN_ALPHA],
        "collect_outcomes": buckets[ACTION_COLLECT_OUTCOMES],
        "wait_or_observe": buckets[ACTION_WAIT_FOR_SIGNAL] + buckets[ACTION_OBSERVE],
        "wait_for_signal": buckets[ACTION_WAIT_FOR_SIGNAL],
        "observe": buckets[ACTION_OBSERVE],
        "negative_paper_lanes": negative,
        "bucket_counts": dict(sorted(buckets.items())),
        "owner_counts": dict(sorted(owners.items())),
        "can_trade": False,
        "can_promote": False,
    }


def _boards(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def slim(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "lane_key": row.get("lane_key"),
            "bucket": row.get("bucket"),
            "severity": row.get("severity"),
            "owner": row.get("owner"),
            "exchange": row.get("exchange"),
            "symbol": row.get("symbol"),
            "timeframe": row.get("timeframe"),
            "strategy_id": row.get("strategy_id"),
            "action": row.get("action"),
            "metrics": row.get("metrics"),
            "evidence": row.get("evidence"),
        }

    return {
        "top_actions": [slim(r) for r in rows[:12]],
        "repair_first": [
            slim(r)
            for r in rows
            if r.get("bucket")
            in {
                ACTION_REPAIR_ROUTE,
                ACTION_RESTORE_CADENCE,
                ACTION_FIX_SIZE_PROFILE,
                ACTION_REPAIR_CONTRACT,
            }
        ][:12],
        "contract_repairs": [
            slim(r) for r in rows if r.get("bucket") == ACTION_REPAIR_CONTRACT
        ][:12],
        "clean_alpha_mining": [
            slim(r) for r in rows if r.get("bucket") == ACTION_MINE_CLEAN_ALPHA
        ][:12],
        "paper_review": [
            slim(r) for r in rows if r.get("bucket") == ACTION_REVIEW_PAPER_CANDIDATE
        ][:12],
        "negative_outcomes": [
            slim(r)
            for r in rows
            if _map(r.get("evidence")).get("performance_state") == "PAPER_ACTIVE_NEGATIVE"
        ][:12],
    }


def _operator_answer(summary: Mapping[str, Any]) -> str:
    if int(summary.get("repair_first") or 0) > 0:
        return (
            f"{summary.get('repair_first')} lane action(s) must be repaired before "
            "paper/live judgment is trustworthy."
        )
    if int(summary.get("clean_alpha_mining") or 0) > 0:
        return (
            f"{summary.get('clean_alpha_mining')} paper lane(s) have clean contracts but "
            "negative/weak edge; mine entry and exit alpha before promotion."
        )
    if int(summary.get("exit_quality_fixes") or 0) > 0:
        return (
            f"{summary.get('exit_quality_fixes')} paper lane(s) need exit-quality fixes "
            "before promotion review."
        )
    if int(summary.get("paper_review") or 0) > 0:
        return (
            f"{summary.get('paper_review')} paper candidate(s) are ready for human review; "
            "this feed cannot approve or promote them."
        )
    if int(summary.get("collect_outcomes") or 0) > 0:
        return (
            f"{summary.get('collect_outcomes')} paper lane(s) need more outcome evidence; "
            "negative lanes must be mined, not promoted."
        )
    if int(summary.get("wait_or_observe") or 0) > 0:
        return "Paper lanes are online or observable; wait for qualified signals and journal proof."
    return "No joined operator action evidence is available yet."


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


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--activation", type=Path, default=DEFAULT_ACTIVATION)
    parser.add_argument("--route", type=Path, default=DEFAULT_ROUTE)
    parser.add_argument("--cadence", type=Path, default=DEFAULT_CADENCE)
    parser.add_argument("--performance", type=Path, default=DEFAULT_PERFORMANCE)
    parser.add_argument("--exit-autopsy", type=Path, default=DEFAULT_EXIT_AUTOPSY)
    parser.add_argument("--contract-reconciler", type=Path, default=DEFAULT_CONTRACT_RECONCILER)
    parser.add_argument("--causality", type=Path, default=DEFAULT_CAUSALITY)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--feed", type=Path, default=DEFAULT_FEED)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval-seconds", type=float, default=60.0)
    parser.add_argument("--max-rows", type=int, default=180)
    parser.add_argument("--print", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    from vnedge.research.trade_profile_matrix import build_trade_profile_matrix

    config = OperatorActionConfig(max_rows=args.max_rows)
    while True:
        activation = _read_json(args.activation, {"rows": [], "summary": {}})
        payload = build_operator_actions(
            activation=activation,
            route_path=args.route,
            cadence_path=args.cadence,
            performance_path=args.performance,
            exit_autopsy_path=args.exit_autopsy,
            contract_reconciler_path=args.contract_reconciler,
            profile=build_trade_profile_matrix(activation),
            causality_path=args.causality,
            config=config,
        )
        publish_operator_actions(payload, args.out, args.feed)
        if args.print:
            print(render_report(payload), flush=True)
        if args.once:
            return 0
        time.sleep(max(1.0, float(args.interval_seconds)))


if __name__ == "__main__":
    raise SystemExit(main())
