"""Lane firing causality: why a lane did or did not reach paper/live.

This joins the two operator truth layers that already exist:

- realtime_scanner_latest.json says what the lane is seeing right now;
- lane_promotion_readiness_latest.json says where the lane sits in the
  governed promotion ladder.

The output is read-only. It never trades, promotes, or mutates lane state.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from vnedge.research.lane_promotion_readiness import (
    FUNNEL_PAPER,
    FUNNEL_REPLAY,
    FUNNEL_RESEARCH,
    FUNNEL_SHADOW,
    FUNNEL_UNTOUCHED,
)
from vnedge.research.realtime_scanner import (
    STATE_FIRING,
    STATE_NEAR_TRIGGER,
    STATE_NO_EVAL,
    STATE_STALE,
    STATE_WAITING,
    STATE_WARMING,
)

DEFAULT_RESEARCH_DIR = Path("research/live_research")
DEFAULT_READINESS = DEFAULT_RESEARCH_DIR / "lane_promotion_readiness_latest.json"
DEFAULT_SCANNER = DEFAULT_RESEARCH_DIR / "realtime_scanner_latest.json"
DEFAULT_OUT = DEFAULT_RESEARCH_DIR / "lane_firing_causality_latest.json"
DEFAULT_FEED = DEFAULT_RESEARCH_DIR / "lane_firing_causality_feed.jsonl"

FLOW_DATA = "data"
FLOW_SETUP = "setup"
FLOW_TRIGGER = "trigger"
FLOW_RISK = "risk"
FLOW_EXECUTION = "execution"
FLOW_PROMOTION = "promotion"
FLOW_ORDER = (
    FLOW_DATA,
    FLOW_SETUP,
    FLOW_TRIGGER,
    FLOW_RISK,
    FLOW_EXECUTION,
    FLOW_PROMOTION,
)

FLOW_PASS = "PASS"
FLOW_WAIT = "WAIT"
FLOW_BLOCK = "BLOCK"
FLOW_REVIEW = "REVIEW"
FLOW_UNKNOWN = "UNKNOWN"
FLOW_NOT_REACHED = "NOT_REACHED"


@dataclass(frozen=True)
class LaneFiringCausalityConfig:
    max_rows: int = 120

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_lane_firing_causality(
    *,
    readiness: Mapping[str, Any] | None = None,
    scanner: Mapping[str, Any] | None = None,
    readiness_path: Path | str = DEFAULT_READINESS,
    scanner_path: Path | str = DEFAULT_SCANNER,
    config: LaneFiringCausalityConfig = LaneFiringCausalityConfig(),
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the operator-facing lane causality report."""
    now = now or datetime.now(UTC)
    readiness = readiness if isinstance(readiness, Mapping) else _read_json(Path(readiness_path))
    scanner = scanner if isinstance(scanner, Mapping) else _read_json(Path(scanner_path))

    readiness_rows = [
        row for row in readiness.get("rows", []) or [] if isinstance(row, Mapping)
    ]
    scanner_rows = [
        row for row in scanner.get("rows", []) or [] if isinstance(row, Mapping)
    ]

    exact_scanner, weak_scanner = _index(scanner_rows)
    consumed_scanner_keys: set[str] = set()
    rows: list[dict[str, Any]] = []
    for ready_row in readiness_rows:
        scanner_row = _match_scanner(ready_row, exact_scanner, weak_scanner)
        if scanner_row is not None:
            consumed_scanner_keys.add(_row_identity(scanner_row))
        rows.append(_causality_row(ready_row, scanner_row))

    for scanner_row in scanner_rows:
        if _row_identity(scanner_row) in consumed_scanner_keys:
            continue
        rows.append(_causality_row(None, scanner_row))

    rows.sort(key=_row_sort_key)
    rows = rows[: max(1, int(config.max_rows))]
    summary = _summary(rows, readiness_rows=readiness_rows, scanner_rows=scanner_rows)
    return {
        "generated_at": now.isoformat(),
        "report_id": "lane_firing_causality_v1",
        "mode": "read_only_operator_truth",
        "policy": {
            "can_trade": False,
            "can_promote": False,
            "uses_live_scanner": True,
            "uses_readiness_funnel": True,
            "paper_review_is_not_promotion": True,
        },
        "config": config.to_dict(),
        "inputs": {
            "readiness_path": str(readiness_path),
            "scanner_path": str(scanner_path),
        },
        "summary": summary,
        "promotion_board": _promotion_board(rows),
        "rows": rows,
        "operator_answer": _operator_answer(summary),
        "can_trade": False,
        "can_promote": False,
    }


def publish_lane_firing_causality(
    payload: Mapping[str, Any], out: Path, feed: Path | None = None
) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    tmp.replace(out)
    if feed is not None:
        feed.parent.mkdir(parents=True, exist_ok=True)
        with open(feed, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, default=str) + "\n")


def render_report(payload: Mapping[str, Any], *, limit: int = 24) -> str:
    summary = payload.get("summary", {})
    lines = [
        "=== Lane firing causality ===",
        f"generated: {payload.get('generated_at')}",
        str(payload.get("operator_answer") or ""),
        (
            "summary: "
            f"{summary.get('total_rows', 0)} lanes, "
            f"{summary.get('firing_now', 0)} firing, "
            f"{summary.get('near_trigger', 0)} near, "
            f"{summary.get('paper_review_ready', 0)} paper-review, "
            f"{summary.get('paper_running', 0)} paper-running"
        ),
    ]
    for row in list(payload.get("rows", []))[:limit]:
        blocker = row.get("primary_blocker") or {}
        lines.append(
            f"  {row.get('scanner_state', 'NO_SCANNER'):<13} "
            f"{row.get('promotion_stage', ''):<8} "
            f"{row.get('exchange', ''):<13} {row.get('symbol', ''):<15} "
            f"{row.get('strategy_id', ''):<28} "
            f"{blocker.get('stage', 'ok')}:{blocker.get('action', 'none')}"
        )
    lines.append("read-only: can_trade=false can_promote=false")
    return "\n".join(lines)


def _causality_row(
    readiness_row: Mapping[str, Any] | None,
    scanner_row: Mapping[str, Any] | None,
) -> dict[str, Any]:
    base = scanner_row or readiness_row or {}
    strategy_id = _first_text(
        base.get("strategy_id"),
        base.get("family"),
        base.get("source"),
        (readiness_row or {}).get("strategy_id"),
        (scanner_row or {}).get("strategy_id"),
    )
    exchange = _first_text(
        base.get("exchange"),
        (readiness_row or {}).get("exchange"),
        (scanner_row or {}).get("exchange"),
    )
    symbol = _first_text(
        base.get("symbol"),
        (readiness_row or {}).get("symbol"),
        (scanner_row or {}).get("symbol"),
    )
    timeframe = _first_text(
        base.get("timeframe"),
        (readiness_row or {}).get("timeframe"),
        (scanner_row or {}).get("timeframe"),
    ) or "unknown"
    scanner_state = str((scanner_row or {}).get("state") or "NO_SCANNER")
    canonical = (readiness_row or {}).get("funnel") or {}
    promotion_stage = str(canonical.get("stage") or "UNTRACKED")
    promotion_state = str(canonical.get("state") or "UNTRACKED")
    counters = _counters(scanner_row, readiness_row)
    flow = _flow(readiness_row, scanner_row, counters)
    decision = _paper_decision(readiness_row, flow)
    blocker = _primary_blocker(flow, decision)
    return {
        "lane_key": _lane_key_parts(strategy_id, exchange, symbol, timeframe),
        "exchange": exchange,
        "symbol": symbol,
        "timeframe": timeframe,
        "strategy_id": strategy_id,
        "mode": _first_text(
            (scanner_row or {}).get("mode"),
            (readiness_row or {}).get("mode"),
        ),
        "scanner_state": scanner_state,
        "scanner_why": str((scanner_row or {}).get("why") or ""),
        "promotion_stage": promotion_stage,
        "promotion_state": promotion_state,
        "canonical_status": str((readiness_row or {}).get("canonical_status") or "UNTRACKED"),
        "flow": flow,
        "flow_summary": " -> ".join(
            f"{stage}:{flow[stage]['state']}" for stage in FLOW_ORDER
        ),
        "primary_blocker": blocker,
        "paper_decision": decision,
        "why_no_trade_minute": _why_no_trade(scanner_row, flow, decision, blocker),
        "counters": counters,
        "recent": (scanner_row or {}).get("recent") or {},
        "latest_eval": (scanner_row or {}).get("latest_eval") or {},
        "latest_shadow_intent": (scanner_row or {}).get("latest_shadow_intent"),
        "latest_shadow_outcome": (scanner_row or {}).get("latest_shadow_outcome"),
        "latest_paper_order": (scanner_row or {}).get("latest_paper_order"),
        "latest_paper_exit": (scanner_row or {}).get("latest_paper_exit"),
        "readiness": {
            "status": (readiness_row or {}).get("status"),
            "row_type": (readiness_row or {}).get("row_type"),
            "paper_review_ready": bool((readiness_row or {}).get("paper_review_ready")),
            "paper_active": bool((readiness_row or {}).get("paper_active")),
            "live_ready": bool((readiness_row or {}).get("live_ready")),
            "primary_blocker": (readiness_row or {}).get("primary_blocker"),
            "triage": (readiness_row or {}).get("triage") or {},
            "next_action": (readiness_row or {}).get("next_action"),
        },
        "can_trade": False,
        "can_promote": False,
    }


def _flow(
    readiness_row: Mapping[str, Any] | None,
    scanner_row: Mapping[str, Any] | None,
    counters: Mapping[str, int],
) -> dict[str, dict[str, str]]:
    return {
        FLOW_DATA: _data_gate(scanner_row),
        FLOW_SETUP: _setup_gate(scanner_row),
        FLOW_TRIGGER: _trigger_gate(scanner_row),
        FLOW_RISK: _risk_gate(scanner_row, counters),
        FLOW_EXECUTION: _execution_gate(readiness_row, scanner_row, counters),
        FLOW_PROMOTION: _promotion_gate(readiness_row),
    }


def _data_gate(scanner_row: Mapping[str, Any] | None) -> dict[str, str]:
    if scanner_row is None:
        return _gate(FLOW_UNKNOWN, "data_freshness", "no realtime scanner row matched")
    state = str(scanner_row.get("state") or "")
    if state in {STATE_STALE, STATE_NO_EVAL}:
        return _gate(FLOW_BLOCK, "data_freshness", str(scanner_row.get("why") or state))
    return _gate(FLOW_PASS, "data_freshness", "latest scanner evaluation is available")


def _setup_gate(scanner_row: Mapping[str, Any] | None) -> dict[str, str]:
    if scanner_row is None:
        return _gate(FLOW_UNKNOWN, "setup_context", "no setup telemetry matched")
    state = str(scanner_row.get("state") or "")
    if state == STATE_WARMING:
        return _gate(FLOW_WAIT, "setup_context", "feature warmup incomplete")
    latest = scanner_row.get("latest_eval") or {}
    features = latest.get("features") if isinstance(latest, Mapping) else {}
    diagnostics = scanner_row.get("gate_diagnostics") or {}
    if not isinstance(features, Mapping) or not features:
        return _gate(FLOW_UNKNOWN, "setup_context", "lane does not publish feature context")
    score = diagnostics.get("readiness_score") if isinstance(diagnostics, Mapping) else None
    detail = (
        f"feature context published; readiness score {float(score):.2f}"
        if isinstance(score, (int, float))
        else "feature context published"
    )
    return _gate(FLOW_PASS, "setup_context", detail)


def _trigger_gate(scanner_row: Mapping[str, Any] | None) -> dict[str, str]:
    if scanner_row is None:
        return _gate(FLOW_NOT_REACHED, "trigger", "no trigger evaluation matched")
    state = str(scanner_row.get("state") or STATE_WAITING)
    why = str(scanner_row.get("why") or state)
    if state == STATE_FIRING:
        return _gate(FLOW_PASS, "trigger", why)
    if state == STATE_NEAR_TRIGGER:
        return _gate(FLOW_WAIT, "trigger", why)
    if state in {STATE_STALE, STATE_NO_EVAL}:
        return _gate(FLOW_BLOCK, "trigger", why)
    if state == STATE_WARMING:
        return _gate(FLOW_WAIT, "trigger", why)
    return _gate(FLOW_BLOCK, "trigger", why)


def _risk_gate(
    scanner_row: Mapping[str, Any] | None,
    counters: Mapping[str, int],
) -> dict[str, str]:
    if counters.get("risk_decisions", 0) > 0:
        return _gate(FLOW_PASS, "risk_gateway", "risk decision recorded")
    if counters.get("paper_order_intents", 0) > 0:
        return _gate(FLOW_PASS, "risk_gateway", "paper order passed the risk route")
    if counters.get("rejected_shadow_intents", 0) > 0:
        return _gate(FLOW_BLOCK, "risk_gateway", "latest shadow intent was rejected")
    if scanner_row is not None and scanner_row.get("state") == STATE_FIRING:
        return _gate(FLOW_WAIT, "risk_gateway", "signal fired; waiting for risk decision")
    return _gate(FLOW_NOT_REACHED, "risk_gateway", "no trigger has reached risk yet")


def _execution_gate(
    readiness_row: Mapping[str, Any] | None,
    scanner_row: Mapping[str, Any] | None,
    counters: Mapping[str, int],
) -> dict[str, str]:
    if counters.get("paper_order_intents", 0) > 0:
        return _gate(FLOW_PASS, "execution_route", "paper order intent recorded")
    if counters.get("approved_shadow_intents", 0) > 0 or counters.get("shadow_outcomes", 0) > 0:
        return _gate(FLOW_PASS, "execution_route", "shadow intent/outcome recorded")
    if counters.get("rejected_shadow_intents", 0) > 0:
        return _gate(FLOW_BLOCK, "execution_route", "shadow intent rejected before execution")
    stage = str(((readiness_row or {}).get("funnel") or {}).get("stage") or "")
    state = str(((readiness_row or {}).get("funnel") or {}).get("state") or "")
    if stage == FUNNEL_REPLAY and state == "NEEDS_SHADOW_ADAPTER":
        return _gate(FLOW_BLOCK, "execution_route", "replay-positive idea lacks runtime adapter")
    if scanner_row is not None and scanner_row.get("state") == STATE_FIRING:
        return _gate(FLOW_WAIT, "execution_route", "signal fired; no intent/order recorded yet")
    return _gate(FLOW_NOT_REACHED, "execution_route", "execution route not reached")


def _promotion_gate(readiness_row: Mapping[str, Any] | None) -> dict[str, str]:
    if readiness_row is None:
        return _gate(FLOW_UNKNOWN, "promotion", "not present in promotion readiness")
    funnel = readiness_row.get("funnel") or {}
    stage = str(funnel.get("stage") or "")
    state = str(funnel.get("state") or "")
    if stage == FUNNEL_PAPER and state == "PAPER_RUNNING":
        return _gate(FLOW_PASS, "promotion", "approved paper trial is running")
    if stage == FUNNEL_SHADOW and state == "READY_FOR_PAPER_REVIEW":
        return _gate(FLOW_REVIEW, "promotion", "ready for human paper-trial review")
    if stage == FUNNEL_SHADOW and state in {"NEGATIVE_EDGE_BLOCKED", "QUALITY_BLOCKED"}:
        return _gate(FLOW_BLOCK, "promotion", str(readiness_row.get("primary_blocker") or state))
    if stage == FUNNEL_REPLAY:
        return _gate(FLOW_BLOCK, "promotion", "needs runtime shadow adapter before promotion")
    if stage == FUNNEL_UNTOUCHED:
        return _gate(FLOW_WAIT, "promotion", "awaiting untouched-window judgment")
    if stage == FUNNEL_RESEARCH:
        return _gate(FLOW_BLOCK, "promotion", str(readiness_row.get("primary_blocker") or state))
    return _gate(FLOW_WAIT, "promotion", str(readiness_row.get("primary_blocker") or state))


def _gate(state: str, category: str, detail: str) -> dict[str, str]:
    return {"state": state, "category": category, "detail": detail}


def _paper_decision(
    readiness_row: Mapping[str, Any] | None,
    flow: Mapping[str, Mapping[str, str]],
) -> dict[str, str]:
    if readiness_row is None:
        return {
            "state": "UNTRACKED_SCANNER",
            "action": "add to manifest or archive scanner-only row",
            "reason": "scanner row is not present in lane promotion readiness",
        }
    funnel = readiness_row.get("funnel") or {}
    stage = str(funnel.get("stage") or "")
    state = str(funnel.get("state") or "")
    if stage == FUNNEL_PAPER and state == "PAPER_RUNNING":
        return {
            "state": "PAPER_RUNNING",
            "action": "observe locked paper trial; do not retune mid-trial",
            "reason": "paper order activity is already recorded",
        }
    if stage == FUNNEL_PAPER:
        return {
            "state": "PAPER_WAITING_FOR_SIGNAL",
            "action": "keep paper lane online until next valid signal",
            "reason": "approved paper trial exists but has not produced an order",
        }
    if stage == FUNNEL_SHADOW and state == "READY_FOR_PAPER_REVIEW":
        return {
            "state": "READY_FOR_PAPER_REVIEW",
            "action": "open human paper-trial approval packet",
            "reason": "shadow evidence passed sample, span, net, and PF gates",
        }
    if stage == FUNNEL_SHADOW and state == "WAITING_FOR_OUTCOMES":
        return {
            "state": "NEEDS_SHADOW_OUTCOMES",
            "action": "keep shadow lane online; inspect trigger blockers",
            "reason": "no resolved shadow outcomes exist yet",
        }
    if stage == FUNNEL_SHADOW:
        return {
            "state": "SHADOW_BLOCKED",
            "action": "refactor entry/exit before paper review",
            "reason": flow[FLOW_PROMOTION]["detail"],
        }
    if stage == FUNNEL_REPLAY:
        return {
            "state": "NEEDS_SHADOW_ADAPTER",
            "action": "build runtime adapter for replay-positive idea",
            "reason": "replay evidence is not live shadow evidence",
        }
    return {
        "state": "PROMOTION_BLOCKED",
        "action": "resolve readiness blocker",
        "reason": flow[FLOW_PROMOTION]["detail"],
    }


def _primary_blocker(
    flow: Mapping[str, Mapping[str, str]],
    decision: Mapping[str, str],
) -> dict[str, str]:
    if decision.get("state") == "READY_FOR_PAPER_REVIEW":
        return {
            "stage": FLOW_PROMOTION,
            "category": "paper_review",
            "detail": str(decision.get("reason") or ""),
            "action": str(decision.get("action") or ""),
        }
    if decision.get("state") == "NEEDS_SHADOW_ADAPTER":
        detail = str(decision.get("reason") or flow[FLOW_EXECUTION]["detail"])
        return {
            "stage": FLOW_EXECUTION,
            "category": "execution_route",
            "detail": detail,
            "action": "build runtime shadow adapter",
        }
    if decision.get("state") == "UNTRACKED_SCANNER":
        return {
            "stage": FLOW_PROMOTION,
            "category": "manifest_tracking",
            "detail": str(decision.get("reason") or "scanner row is not tracked"),
            "action": str(decision.get("action") or "add to manifest or archive scanner-only row"),
        }
    for stage in FLOW_ORDER:
        gate = flow[stage]
        if gate["state"] in {FLOW_BLOCK, FLOW_UNKNOWN}:
            return {
                "stage": stage,
                "category": gate["category"],
                "detail": gate["detail"],
                "action": _action_for(stage, gate["category"]),
            }
        if gate["state"] == FLOW_WAIT and stage in {
            FLOW_TRIGGER,
            FLOW_RISK,
            FLOW_EXECUTION,
            FLOW_PROMOTION,
        }:
            return {
                "stage": stage,
                "category": gate["category"],
                "detail": gate["detail"],
                "action": _action_for(stage, gate["category"]),
            }
    return {
        "stage": "none",
        "category": "clear",
        "detail": "no blocker in current causality flow",
        "action": "observe",
    }


def _action_for(stage: str, category: str) -> str:
    if category == "data_freshness":
        return "repair live data lane"
    if category == "setup_context":
        return "publish complete setup telemetry"
    if stage == FLOW_TRIGGER:
        return "wait for trigger or retune only through backtest"
    if category == "risk_gateway":
        return "inspect risk decision journal"
    if category == "execution_route":
        return "wire runtime adapter or execution journal"
    if category == "promotion":
        return "resolve promotion blocker"
    return "inspect lane"


def _why_no_trade(
    scanner_row: Mapping[str, Any] | None,
    flow: Mapping[str, Mapping[str, str]],
    decision: Mapping[str, str],
    blocker: Mapping[str, str],
) -> str:
    if scanner_row is not None and scanner_row.get("state") == STATE_FIRING:
        risk = flow[FLOW_RISK]
        execution = flow[FLOW_EXECUTION]
        if risk["state"] != FLOW_PASS:
            return f"signal fired, but risk is not complete: {risk['detail']}"
        if execution["state"] != FLOW_PASS:
            return f"signal fired, but execution is not complete: {execution['detail']}"
        return "signal fired and reached the recorded shadow/paper route"
    if decision.get("state") == "READY_FOR_PAPER_REVIEW":
        return "not trading live; lane is waiting for human paper-trial approval"
    detail = str(blocker.get("detail") or "")
    action = str(blocker.get("action") or "")
    return f"no trade: {detail}; next: {action}".strip()


def _counters(
    scanner_row: Mapping[str, Any] | None,
    readiness_row: Mapping[str, Any] | None,
) -> dict[str, int]:
    funnel = (scanner_row or {}).get("funnel") or {}
    evidence = (readiness_row or {}).get("evidence") or {}
    return {
        "evals": _int(funnel.get("evals")),
        "live_evals": _int(funnel.get("live_evals")),
        "live_signals": _int(funnel.get("live_signals") or funnel.get("signals")),
        "shadow_intents": _int(funnel.get("shadow_intents")),
        "approved_shadow_intents": _int(funnel.get("approved_shadow_intents")),
        "rejected_shadow_intents": _int(funnel.get("rejected_shadow_intents")),
        "shadow_outcomes": max(
            _int(funnel.get("shadow_outcomes")),
            _int(evidence.get("virtual_trades")),
        ),
        "risk_decisions": _int(funnel.get("risk_decisions")),
        "paper_order_intents": max(
            _int(funnel.get("paper_order_intents")),
            _int(evidence.get("paper_order_intents")),
        ),
        "paper_exits": max(
            _int(funnel.get("paper_exits")),
            _int(evidence.get("paper_exits")),
        ),
    }


def _summary(
    rows: list[dict[str, Any]],
    *,
    readiness_rows: list[Mapping[str, Any]],
    scanner_rows: list[Mapping[str, Any]],
) -> dict[str, Any]:
    blockers: dict[str, int] = {}
    actions: dict[str, int] = {}
    decisions: dict[str, int] = {}
    for row in rows:
        blocker = row.get("primary_blocker") or {}
        category = str(blocker.get("category") or "unknown")
        action = str(blocker.get("action") or "unknown")
        blockers[category] = blockers.get(category, 0) + 1
        actions[action] = actions.get(action, 0) + 1
        decision = str(row.get("paper_decision", {}).get("state") or "unknown")
        decisions[decision] = decisions.get(decision, 0) + 1
    return {
        "total_rows": len(rows),
        "readiness_rows": len(readiness_rows),
        "scanner_rows": len(scanner_rows),
        "firing_now": sum(1 for row in rows if row.get("scanner_state") == STATE_FIRING),
        "near_trigger": sum(1 for row in rows if row.get("scanner_state") == STATE_NEAR_TRIGGER),
        "stale": sum(1 for row in rows if row.get("scanner_state") == STATE_STALE),
        "no_live_scanner": sum(1 for row in rows if row.get("scanner_state") == "NO_SCANNER"),
        "paper_review_ready": decisions.get("READY_FOR_PAPER_REVIEW", 0),
        "paper_running": decisions.get("PAPER_RUNNING", 0),
        "paper_waiting": decisions.get("PAPER_WAITING_FOR_SIGNAL", 0),
        "needs_shadow_outcomes": decisions.get("NEEDS_SHADOW_OUTCOMES", 0),
        "needs_shadow_adapter": decisions.get("NEEDS_SHADOW_ADAPTER", 0),
        "shadow_blocked": decisions.get("SHADOW_BLOCKED", 0),
        "untracked_scanner": decisions.get("UNTRACKED_SCANNER", 0),
        "blocker_categories": _sorted_counts(blockers),
        "action_counts": _sorted_counts(actions),
        "paper_decision_counts": _sorted_counts(decisions),
        "can_trade": False,
        "can_promote": False,
    }


def _promotion_board(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def compact(row: Mapping[str, Any]) -> dict[str, Any]:
        blocker = row.get("primary_blocker") or {}
        counters = row.get("counters") or {}
        return {
            "lane_key": row.get("lane_key"),
            "exchange": row.get("exchange"),
            "symbol": row.get("symbol"),
            "timeframe": row.get("timeframe"),
            "strategy_id": row.get("strategy_id"),
            "scanner_state": row.get("scanner_state"),
            "paper_decision": row.get("paper_decision"),
            "blocker": blocker,
            "live_signals": counters.get("live_signals", 0),
            "paper_order_intents": counters.get("paper_order_intents", 0),
            "shadow_outcomes": counters.get("shadow_outcomes", 0),
            "why_no_trade_minute": row.get("why_no_trade_minute"),
        }

    ready = [
        compact(row)
        for row in rows
        if row.get("paper_decision", {}).get("state") == "READY_FOR_PAPER_REVIEW"
    ]
    running = [
        compact(row)
        for row in rows
        if row.get("paper_decision", {}).get("state") == "PAPER_RUNNING"
    ]
    waiting = [
        compact(row)
        for row in rows
        if row.get("paper_decision", {}).get("state") == "PAPER_WAITING_FOR_SIGNAL"
    ]
    fix_first = [
        compact(row)
        for row in rows
        if row.get("paper_decision", {}).get("state")
        in {"SHADOW_BLOCKED", "NEEDS_SHADOW_ADAPTER", "NEEDS_SHADOW_OUTCOMES"}
    ]
    return {
        "ready_for_review": ready[:12],
        "paper_running": running[:12],
        "paper_waiting": waiting[:12],
        "fix_first": sorted(
            fix_first,
            key=lambda row: (
                _fix_priority(str(row.get("paper_decision", {}).get("state") or "")),
                str(row.get("exchange") or ""),
                str(row.get("symbol") or ""),
            ),
        )[:12],
        "can_trade": False,
        "can_promote": False,
    }


def _fix_priority(state: str) -> int:
    return {
        "NEEDS_SHADOW_ADAPTER": 0,
        "SHADOW_BLOCKED": 1,
        "NEEDS_SHADOW_OUTCOMES": 2,
    }.get(state, 9)


def _operator_answer(summary: Mapping[str, Any]) -> str:
    firing = int(summary.get("firing_now") or 0)
    near = int(summary.get("near_trigger") or 0)
    ready = int(summary.get("paper_review_ready") or 0)
    running = int(summary.get("paper_running") or 0)
    no_scanner = int(summary.get("no_live_scanner") or 0)
    if ready:
        return (
            f"{ready} lane(s) are ready for human paper review, {running} paper "
            f"lane(s) are already running, {firing} are firing now, and {near} are near trigger."
        )
    if firing:
        return (
            f"{firing} lane(s) are firing now, but none are paper-review ready yet. "
            "Inspect risk/execution and shadow-outcome gates before promotion."
        )
    if near:
        return (
            f"No lane is firing now; {near} lane(s) are near trigger. "
            "This is the watchlist for the next live bar."
        )
    if no_scanner:
        return (
            f"No current firing; {no_scanner} lane(s) have readiness rows but no "
            "matching live scanner telemetry."
        )
    return "No current firing; lane blockers are published in the causality board."


def _index(
    rows: list[Mapping[str, Any]],
) -> tuple[dict[str, Mapping[str, Any]], dict[str, Mapping[str, Any]]]:
    exact: dict[str, Mapping[str, Any]] = {}
    weak: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        exact.setdefault(_lane_key(row), row)
        weak.setdefault(_weak_lane_key(row), row)
    return exact, weak


def _match_scanner(
    readiness_row: Mapping[str, Any],
    exact_scanner: Mapping[str, Mapping[str, Any]],
    weak_scanner: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    return exact_scanner.get(_lane_key(readiness_row)) or weak_scanner.get(
        _weak_lane_key(readiness_row)
    )


def _row_identity(row: Mapping[str, Any]) -> str:
    return str(row.get("lane_id") or row.get("journal") or _lane_key(row))


def _lane_key(row: Mapping[str, Any]) -> str:
    return _lane_key_parts(
        _first_text(row.get("strategy_id"), row.get("family"), row.get("source")),
        _first_text(row.get("exchange")),
        _first_text(row.get("symbol")),
        _first_text(row.get("timeframe")) or "unknown",
    )


def _weak_lane_key(row: Mapping[str, Any]) -> str:
    return "|".join(
        (
            _norm(_first_text(row.get("strategy_id"), row.get("family"), row.get("source"))),
            _norm(_first_text(row.get("exchange"))),
            _norm(_first_text(row.get("symbol"))),
        )
    )


def _lane_key_parts(strategy: str, exchange: str, symbol: str, timeframe: str) -> str:
    return "|".join((_norm(strategy), _norm(exchange), _norm(symbol), _norm(timeframe)))


def _row_sort_key(row: Mapping[str, Any]) -> tuple[int, int, str, str, str]:
    decision = str(row.get("paper_decision", {}).get("state") or "")
    state = str(row.get("scanner_state") or "")
    return (
        {
            "READY_FOR_PAPER_REVIEW": 0,
            "PAPER_RUNNING": 1,
            "PAPER_WAITING_FOR_SIGNAL": 2,
            "NEEDS_SHADOW_ADAPTER": 3,
            "SHADOW_BLOCKED": 4,
            "NEEDS_SHADOW_OUTCOMES": 5,
            "UNTRACKED_SCANNER": 6,
        }.get(decision, 8),
        {
            STATE_FIRING: 0,
            STATE_NEAR_TRIGGER: 1,
            STATE_WAITING: 2,
            STATE_WARMING: 3,
            STATE_STALE: 4,
        }.get(state, 5),
        str(row.get("exchange") or ""),
        str(row.get("symbol") or ""),
        str(row.get("strategy_id") or ""),
    )


def _sorted_counts(counts: Mapping[str, int]) -> dict[str, int]:
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def _first_text(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value)
        if text:
            return text
    return ""


def _norm(value: str) -> str:
    return str(value or "").strip().lower()


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish lane firing causality")
    parser.add_argument("--readiness", default=str(DEFAULT_READINESS))
    parser.add_argument("--scanner", default=str(DEFAULT_SCANNER))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--feed", default=str(DEFAULT_FEED))
    parser.add_argument("--interval-seconds", type=int, default=60)
    parser.add_argument("--max-rows", type=int, default=120)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-publish", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = LaneFiringCausalityConfig(max_rows=args.max_rows)
    while True:
        payload = build_lane_firing_causality(
            readiness_path=Path(args.readiness),
            scanner_path=Path(args.scanner),
            config=config,
        )
        if not args.no_publish:
            publish_lane_firing_causality(
                payload,
                Path(args.out),
                None if args.feed == "" else Path(args.feed),
            )
        print(json.dumps(payload, indent=2, default=str) if args.json else render_report(payload))
        if args.once:
            return 0
        time.sleep(max(1, int(args.interval_seconds)))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
