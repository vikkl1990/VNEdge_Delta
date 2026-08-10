"""Scalper focus report.

The scanner and edge miner already tell us whether a lane passes. This module
answers the next operator question when the answer is "no": what exactly is
blocking scalping, which lanes deserve recorder time, and which hypotheses are
closest to becoming replay candidates?

It is intentionally research-only. A focus row is not a signal, not a
promotion, and not paper/shadow approval.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from typing import Iterable, Mapping, Any

from vnedge.scalping.parameter_registry import DEFAULT_SCALPER_PARAMETER_REGISTRY


FOCUS_ID = "scalper_focus_v1"


def build_scalper_focus(
    scans: Iterable[Any],
    edge_hypotheses: Iterable[Any],
    *,
    recorder_targets: Iterable[Any] = (),
    days: Iterable[str] = (),
    max_lanes: int = 12,
    max_hypotheses: int = 12,
) -> dict:
    scan_rows = [_to_dict(s) for s in scans]
    hypothesis_rows = [_to_dict(h) for h in edge_hypotheses]
    recorder_rows = [_to_dict(r) for r in recorder_targets]
    replay_candidates = [s for s in scan_rows if s.get("state") == "REPLAY_CANDIDATE"]
    edge_candidates = [
        h for h in hypothesis_rows
        if str(h.get("state", "")).startswith("EDGE_CANDIDATE")
    ]
    status = (
        "REPLAY_CANDIDATE_READY"
        if replay_candidates
        else "EDGE_HYPOTHESIS_READY"
        if edge_candidates
        else "SCALPER_DATA_COLLECTION"
        if any(s.get("state") in {"MISSING_TICK_DATA", "RECORD_MORE"} for s in scan_rows)
        else "SCALPER_COST_WALL"
        if scan_rows or hypothesis_rows
        else "SCALPER_NO_DATA"
    )
    blocker_counts = Counter(s.get("state", "UNKNOWN") for s in scan_rows)
    primary_counts = Counter(s.get("primary_blocker", "UNKNOWN") for s in scan_rows)
    lifecycle = _family_lifecycle()
    return {
        "focus_id": FOCUS_ID,
        "status": status,
        "can_trade": False,
        "can_promote": False,
        "requires_untouched_judgment": True,
        "days": list(days),
        "summary": {
            "scanner_lanes": len(scan_rows),
            "edge_hypotheses": len(hypothesis_rows),
            "replay_candidates": len(replay_candidates),
            "edge_candidates": len(edge_candidates),
            "recorder_targets": len(recorder_rows),
            "missing_tick_data": blocker_counts.get("MISSING_TICK_DATA", 0),
            "record_more": blocker_counts.get("RECORD_MORE", 0),
            "cost_wall": blocker_counts.get("REJECTED_COST_WALL", 0),
        },
        "blockers": {
            "by_state": dict(blocker_counts),
            "by_primary_blocker": dict(primary_counts),
        },
        "family_lifecycle": lifecycle,
        "next_family_priority": lifecycle["active_research"],
        "lane_focus": _lane_focus(scan_rows, max_rows=max_lanes),
        "hypothesis_focus": _hypothesis_focus(hypothesis_rows, max_rows=max_hypotheses),
        "recorder_campaign": _recorder_campaign(
            recorder_rows or scan_rows,
            max_rows=max_lanes,
        ),
        "cost_wall": _cost_wall(scan_rows, hypothesis_rows),
        "next_actions": _next_actions(
            status,
            scan_rows,
            hypothesis_rows,
            recorder_rows,
            lifecycle,
        ),
    }


def _lane_focus(rows: list[dict], *, max_rows: int) -> list[dict]:
    ranked = sorted(rows, key=_scan_key)
    return [_lane_item(row) for row in ranked[:max_rows]]


def _lane_item(row: dict) -> dict:
    best = row.get("best_row") or {}
    route = row.get("route_decision") or {}
    gates = row.get("gates") or {}
    failed = sorted(k for k, ok in gates.items() if not ok)
    return {
        "exchange": row.get("exchange"),
        "symbol": row.get("symbol"),
        "day": row.get("day"),
        "state": row.get("state"),
        "primary_blocker": row.get("primary_blocker"),
        "edge_score": _round(row.get("edge_score")),
        "recorder_priority": _round(row.get("recorder_priority")),
        "route": route.get("route", "BLOCKED"),
        "failed_gates": failed,
        "next_action": row.get("next_action"),
        "best_replay": {
            "filled": int(best.get("filled", 0) or 0),
            "quotes": int(best.get("quotes", 0) or 0),
            "fill_rate_pct": _round(best.get("fill_rate_pct")),
            "profit_factor": _round(best.get("profit_factor")),
            "avg_net_bps": _round(best.get("avg_net_bps")),
            "net_usd": _round(best.get("net_usd")),
            "exit_policy_id": best.get("exit_policy_id"),
        },
        "maker_gap": _maker_gap(route, best),
    }


def _hypothesis_focus(rows: list[dict], *, max_rows: int) -> list[dict]:
    ranked = sorted(rows, key=_hypothesis_key)
    return [_hypothesis_item(row) for row in ranked[:max_rows]]


def _hypothesis_item(row: dict) -> dict:
    route = row.get("route_decision") or {}
    return {
        "exchange": row.get("exchange"),
        "symbol": row.get("symbol"),
        "day": row.get("day"),
        "family": row.get("family"),
        "side": row.get("side"),
        "horizon_ms": row.get("horizon_ms"),
        "state": row.get("state"),
        "route": route.get("route", "BLOCKED"),
        "samples": int(row.get("samples", 0) or 0),
        "profit_factor": _round(row.get("profit_factor")),
        "avg_net_bps": _round(row.get("avg_net_bps")),
        "avg_forward_bps": _round(row.get("avg_forward_bps")),
        "win_rate_pct": _round(row.get("win_rate_pct")),
        "maker_gap": _maker_gap(route, {"avg_net_bps": row.get("avg_net_bps")}),
        "hypothesis_id": row.get("hypothesis_id"),
    }


def _recorder_campaign(rows: list[dict], *, max_rows: int) -> list[dict]:
    best: dict[tuple[str, str], dict] = {}
    for row in rows:
        if row.get("state") in {"REJECTED_COST_WALL", "REJECTED_LIQUIDITY"}:
            continue
        key = (str(row.get("exchange", "")), str(row.get("symbol", "")))
        prev = best.get(key)
        if prev is None or _scan_key(row) < _scan_key(prev):
            best[key] = row
    return [
        {
            "exchange": row.get("exchange"),
            "symbol": row.get("symbol"),
            "state": row.get("state"),
            "recorder_priority": _round(row.get("recorder_priority")),
            "edge_score": _round(row.get("edge_score")),
            "reason": _recorder_reason(row),
            "can_trade": False,
        }
        for row in sorted(best.values(), key=_scan_key)[:max_rows]
    ]


def _cost_wall(scan_rows: list[dict], hypothesis_rows: list[dict]) -> dict:
    scan_cost = [
        _lane_item(row) for row in scan_rows
        if (row.get("route_decision") or {}).get("route") == "BLOCKED"
        and (row.get("best_row") or {}).get("avg_net_bps") is not None
    ]
    hypo_cost = [
        _hypothesis_item(row) for row in hypothesis_rows
        if (row.get("route_decision") or {}).get("route") == "BLOCKED"
        and row.get("avg_net_bps") is not None
    ]
    scan_cost.sort(key=lambda r: _gap_sort_value(r.get("maker_gap")))
    hypo_cost.sort(key=lambda r: _gap_sort_value(r.get("maker_gap")))
    return {
        "scanner_blocked": len(scan_cost),
        "hypotheses_blocked": len(hypo_cost),
        "closest_scanner_lanes": scan_cost[:8],
        "closest_hypotheses": hypo_cost[:8],
    }


def _next_actions(
    status: str,
    scan_rows: list[dict],
    hypothesis_rows: list[dict],
    recorder_rows: list[dict],
    lifecycle: dict,
) -> list[str]:
    active = ", ".join(
        item["family_id"] for item in lifecycle["active_research"][:5]
    )
    tombstoned = ", ".join(
        item["family_id"] for item in lifecycle["tombstoned"]
    ) or "none"
    if status == "REPLAY_CANDIDATE_READY":
        return [
            "pre-register untouched replay for the replay candidate; do not auto-promote",
            "keep candidate in shadow/paper discussion only after human approval",
        ]
    if status == "EDGE_HYPOTHESIS_READY":
        return [
            "run conservative replay on EDGE_CANDIDATE hypotheses before any signal work",
            "prioritize the same exchange/symbol in the recorder campaign",
        ]
    missing = sum(1 for row in scan_rows if row.get("state") == "MISSING_TICK_DATA")
    record_more = sum(1 for row in scan_rows if row.get("state") == "RECORD_MORE")
    if missing or record_more:
        return [
            f"record tick/L2 for {missing} missing lane(s) and extend {record_more} under-sampled lane(s)",
            "do not tune thresholds while sample gates are open",
            f"prioritize active event families next: {active}",
            "rerun scanner after the next L2 research pass",
        ]
    if hypothesis_rows or scan_rows:
        return [
            "current scalp shapes are below fee wall; do not trade",
            f"do not spend replay priority on tombstoned families: {tombstoned}",
            f"mine active event families with stronger structural premises: {active}",
            "keep maker-first as the default route until PF materially improves",
        ]
    if recorder_rows:
        return ["recorder campaign exists, but no scans are complete yet"]
    return ["start tick/L2 recorder; scalper cannot be proven from candles"]


def _family_lifecycle() -> dict:
    registry = DEFAULT_SCALPER_PARAMETER_REGISTRY
    return {
        "active_research": [
            {
                "family_id": family.family_id,
                "description": family.description,
                "exit_policy_id": family.exit_policy_id,
                "horizons_ms": list(family.horizons_ms),
            }
            for family in registry.active_research_families()
        ],
        "tombstoned": [
            {
                "family_id": family.family_id,
                "description": family.description,
                "evidence": family.evidence,
                "can_trade": False,
                "can_promote": False,
            }
            for family in registry.tombstoned_families()
        ],
    }


def _recorder_reason(row: dict) -> str:
    state = row.get("state")
    if state == "MISSING_TICK_DATA":
        return "missing tick/book data"
    if state == "RECORD_MORE":
        return row.get("next_action") or "under-sampled replay window"
    if state == "REPLAY_CANDIDATE":
        return "candidate evidence; record untouched continuation"
    return row.get("primary_blocker") or "scanner priority"


def _maker_gap(route: Mapping[str, Any], row: Mapping[str, Any]) -> dict:
    avg = _float(row.get("avg_net_bps"))
    pf = _float(row.get("profit_factor") or route.get("observed_profit_factor"))
    net_floor = _float(route.get("maker_breakeven_bps"))
    pf_floor = _float(route.get("maker_min_profit_factor"))
    return {
        "avg_net_bps": _round(avg),
        "maker_net_floor_bps": _round(net_floor),
        "net_gap_bps": _round(None if avg is None or net_floor is None else avg - net_floor),
        "profit_factor": _round(pf),
        "maker_pf_floor": _round(pf_floor),
        "pf_gap": _round(None if pf is None or pf_floor is None else pf - pf_floor),
    }


def _scan_key(row: dict) -> tuple:
    state_rank = {
        "REPLAY_CANDIDATE": 0,
        "RECORD_MORE": 1,
        "MISSING_TICK_DATA": 2,
        "REJECTED_NO_FILLS": 3,
        "REJECTED_COST_WALL": 4,
        "REJECTED_MICROSTRUCTURE": 5,
        "REJECTED_LIQUIDITY": 6,
    }
    return (
        state_rank.get(str(row.get("state")), 9),
        -_float(row.get("recorder_priority"), 0.0),
        -_float(row.get("edge_score"), 0.0),
        str(row.get("exchange", "")),
        str(row.get("symbol", "")),
    )


def _hypothesis_key(row: dict) -> tuple:
    state_rank = {
        "EDGE_CANDIDATE_TAKER": 0,
        "EDGE_CANDIDATE_MAKER": 1,
        "UNDER_SAMPLED": 2,
        "BELOW_BREAKEVEN": 3,
    }
    return (
        state_rank.get(str(row.get("state")), 9),
        -_float(row.get("profit_factor"), 0.0),
        -_float(row.get("avg_net_bps"), -999.0),
        -int(row.get("samples", 0) or 0),
        str(row.get("exchange", "")),
        str(row.get("symbol", "")),
    )


def _gap_sort_value(gap: Mapping[str, Any] | None) -> tuple[float, float]:
    if not gap:
        return (999.0, 999.0)
    net_gap = _float(gap.get("net_gap_bps"), -999.0)
    pf_gap = _float(gap.get("pf_gap"), -999.0)
    return (abs(min(net_gap, 0.0)), abs(min(pf_gap, 0.0)))


def _to_dict(value: Any) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "to_dict"):
        out = value.to_dict()
        if hasattr(value, "state") and "state" not in out:
            out["state"] = value.state
        return out
    if not hasattr(value, "__dataclass_fields__"):
        return {"value": value}
    return asdict(value)


def _float(value: Any, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _round(value: Any, digits: int = 4):
    out = _float(value)
    if out is None:
        return None
    return round(out, digits)
