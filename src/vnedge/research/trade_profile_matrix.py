"""Read-only paper/live trade profile matrix.

The activation board answers whether a lane is wired for paper. This module
turns that same report into an operator profile matrix: margin, leverage,
notional, venue limits, and the blocker that prevents paper/live use.

It is deliberately a planner. It cannot write manifests, update runtime
configuration, promote, or trade.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

PAPER_PROFILE_READY = "PAPER_PROFILE_READY"
PAPER_PROFILE_NEEDS_ROUTE = "PAPER_PROFILE_NEEDS_ROUTE"
PAPER_PROFILE_BLOCKED_BY_RISK = "PAPER_PROFILE_BLOCKED_BY_RISK"
LIVE_PROFILE_RISK_OK_PRELIVE_REQUIRED = "LIVE_PROFILE_RISK_OK_PRELIVE_REQUIRED"
LIVE_PROFILE_BLOCKED_BY_RISK = "LIVE_PROFILE_BLOCKED_BY_RISK"
PROFILE_MISSING = "PROFILE_MISSING"

_PAPER_ROUTE_STATES = {
    "PAPER_RUNNING",
    "PAPER_ONLINE_WAITING",
    "PAPER_ROUTE_READY_NO_JOURNAL",
}


def build_trade_profile_matrix(
    activation_payload: Mapping[str, Any] | None,
    *,
    now: datetime | None = None,
    max_rows: int = 240,
) -> dict[str, Any]:
    """Build a profile matrix from ``paper_lane_activation_latest.json``."""
    now = now or datetime.now(UTC)
    activation_payload = activation_payload if isinstance(activation_payload, Mapping) else {}
    rows: list[dict[str, Any]] = []
    for lane in activation_payload.get("rows", []) or []:
        if not isinstance(lane, Mapping):
            continue
        profiles = lane.get("sizing_profiles")
        if not isinstance(profiles, Mapping):
            rows.append(_missing_row(lane))
            continue
        for profile_name in ("paper", "live"):
            profile = profiles.get(profile_name)
            if isinstance(profile, Mapping):
                rows.append(_profile_row(lane, profile_name, profile))

    rows.sort(key=_row_sort_key)
    rows = rows[: max(1, int(max_rows))]
    summary = _summary(rows)
    return {
        "generated_at": now.isoformat(),
        "report_id": "trade_profile_matrix_v1",
        "mode": "read_only_trade_profile_planner",
        "source_report_id": activation_payload.get("report_id"),
        "source_generated_at": activation_payload.get("generated_at"),
        "summary": summary,
        "rows": rows,
        "operator_answer": _operator_answer(summary),
        "policy": {
            "read_only": True,
            "can_trade": False,
            "can_promote": False,
            "can_apply_from_dashboard": False,
            "dashboard_inputs_are_plan_only": True,
        },
        "can_trade": False,
        "can_promote": False,
    }


def _profile_row(
    lane: Mapping[str, Any],
    profile_name: str,
    profile: Mapping[str, Any],
) -> dict[str, Any]:
    risk_ok = bool(profile.get("risk_compatible"))
    activation_state = str(lane.get("activation_state") or "")
    state, next_action = _profile_state(profile_name, risk_ok, activation_state, profile)
    return {
        "lane_key": lane.get("lane_key"),
        "trial_id": lane.get("trial_id"),
        "exchange": lane.get("exchange"),
        "symbol": lane.get("symbol"),
        "timeframe": lane.get("timeframe"),
        "strategy_id": lane.get("strategy_id"),
        "activation_state": activation_state,
        "route_status": lane.get("route_status"),
        "profile": profile_name,
        "profile_state": state,
        "requested_margin_usd": _round(profile.get("requested_margin_usd")),
        "requested_leverage": _round(profile.get("requested_leverage")),
        "requested_notional_usd": _round(profile.get("requested_notional_usd")),
        "manifest_max_leverage": _round(profile.get("manifest_max_leverage")),
        "venue_min_notional_usd": _round(profile.get("venue_min_notional_usd")),
        "venue_min_qty": _round(profile.get("venue_min_qty")),
        "venue_qty_step": _round(profile.get("venue_qty_step")),
        "venue_spec_source": profile.get("venue_spec_source"),
        "risk_compatible": risk_ok,
        "blockers": [str(x) for x in profile.get("blockers") or [] if x],
        "control_blockers": [str(x) for x in profile.get("control_blockers") or [] if x],
        "execution_permission": profile.get("execution_permission"),
        "next_action": next_action,
        "can_trade": False,
        "can_promote": False,
    }


def _missing_row(lane: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "lane_key": lane.get("lane_key"),
        "trial_id": lane.get("trial_id"),
        "exchange": lane.get("exchange"),
        "symbol": lane.get("symbol"),
        "timeframe": lane.get("timeframe"),
        "strategy_id": lane.get("strategy_id"),
        "activation_state": lane.get("activation_state"),
        "route_status": lane.get("route_status"),
        "profile": "unknown",
        "profile_state": PROFILE_MISSING,
        "risk_compatible": False,
        "blockers": ["activation row has no sizing profile"],
        "next_action": "repair paper activation publisher before sizing review",
        "can_trade": False,
        "can_promote": False,
    }


def _profile_state(
    profile_name: str,
    risk_ok: bool,
    activation_state: str,
    profile: Mapping[str, Any],
) -> tuple[str, str]:
    blockers = [str(x) for x in profile.get("blockers") or [] if x]
    if profile_name == "live":
        if not risk_ok:
            return (
                LIVE_PROFILE_BLOCKED_BY_RISK,
                blockers[0] if blockers else "fix live sizing before any pre-live review",
            )
        return (
            LIVE_PROFILE_RISK_OK_PRELIVE_REQUIRED,
            "risk-compatible live plan only; live still needs ladder approval and pre-live checklist",
        )
    if not risk_ok:
        return (
            PAPER_PROFILE_BLOCKED_BY_RISK,
            blockers[0] if blockers else "fix paper sizing before running this profile",
        )
    if activation_state not in _PAPER_ROUTE_STATES:
        return (
            PAPER_PROFILE_NEEDS_ROUTE,
            "paper sizing is compatible, but the lane still needs approval/route/journal work",
        )
    return (
        PAPER_PROFILE_READY,
        "paper sizing is compatible with the current route state",
    )


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    states = Counter(str(r.get("profile_state") or "") for r in rows)
    profiles = Counter(str(r.get("profile") or "") for r in rows)
    return {
        "total_rows": len(rows),
        "paper_rows": profiles["paper"],
        "live_rows": profiles["live"],
        "paper_profile_ready": states[PAPER_PROFILE_READY],
        "paper_profile_needs_route": states[PAPER_PROFILE_NEEDS_ROUTE],
        "paper_profile_blocked_by_risk": states[PAPER_PROFILE_BLOCKED_BY_RISK],
        "live_profile_risk_ok_prelive_required": states[LIVE_PROFILE_RISK_OK_PRELIVE_REQUIRED],
        "live_profile_blocked_by_risk": states[LIVE_PROFILE_BLOCKED_BY_RISK],
        "missing_profiles": states[PROFILE_MISSING],
        "state_counts": dict(sorted(states.items())),
        "can_trade": False,
        "can_promote": False,
    }


def _operator_answer(summary: Mapping[str, Any]) -> str:
    paper_ready = int(summary.get("paper_profile_ready") or 0)
    live_risk_ok = int(summary.get("live_profile_risk_ok_prelive_required") or 0)
    paper_blocked = int(summary.get("paper_profile_blocked_by_risk") or 0)
    live_blocked = int(summary.get("live_profile_blocked_by_risk") or 0)
    if paper_ready:
        return (
            f"{paper_ready} paper profile(s) are sizing-compatible with current route state; "
            f"{live_risk_ok} live profile(s) are risk-compatible but still pre-live gated."
        )
    if paper_blocked or live_blocked:
        if live_risk_ok:
            return (
                f"{paper_blocked} paper and {live_blocked} live profile(s) are blocked by sizing risk; "
                f"{live_risk_ok} live profile(s) are risk-compatible but still pre-live gated."
            )
        return (
            f"{paper_blocked} paper and {live_blocked} live profile(s) are blocked by sizing risk."
        )
    return "No trade profile rows are ready yet; wait for paper activation data."


def _row_sort_key(row: Mapping[str, Any]) -> tuple[int, str, str, str, str]:
    priority = {
        PAPER_PROFILE_READY: 0,
        LIVE_PROFILE_RISK_OK_PRELIVE_REQUIRED: 1,
        PAPER_PROFILE_NEEDS_ROUTE: 2,
        PAPER_PROFILE_BLOCKED_BY_RISK: 3,
        LIVE_PROFILE_BLOCKED_BY_RISK: 4,
        PROFILE_MISSING: 5,
    }.get(str(row.get("profile_state") or ""), 9)
    return (
        priority,
        str(row.get("exchange") or ""),
        str(row.get("symbol") or ""),
        str(row.get("strategy_id") or ""),
        str(row.get("profile") or ""),
    )


def _round(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed:
        return None
    return round(parsed, 6)
