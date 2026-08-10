"""Paper lane survival engine.

This report reconciles paper activation, route health, cadence, and corrected
closed-trade performance into one operator answer:

    "Which lanes should stay in paper, which need more evidence, and which
    should be demoted back to shadow/research before they keep bleeding fees?"

Read-only by design. It cannot start, stop, promote, demote, or trade a lane.
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
DEFAULT_CADENCE = DEFAULT_RESEARCH_DIR / "paper_lane_cadence_latest.json"
DEFAULT_PERFORMANCE = DEFAULT_RESEARCH_DIR / "paper_lane_performance_latest.json"
DEFAULT_ROUTE_DOCTOR = DEFAULT_RESEARCH_DIR / "paper_route_doctor_latest.json"
DEFAULT_OUT = DEFAULT_RESEARCH_DIR / "lane_survival_latest.json"
DEFAULT_FEED = DEFAULT_RESEARCH_DIR / "lane_survival_feed.jsonl"

STATE_PAPER_SURVIVOR_CANDIDATE = "PAPER_SURVIVOR_CANDIDATE"
STATE_PAPER_OBSERVE_MORE = "PAPER_OBSERVE_MORE"
STATE_PAPER_PROBATION = "PAPER_PROBATION"
STATE_DEMOTE_TO_SHADOW = "DEMOTE_TO_SHADOW"
STATE_RESEARCH_ONLY = "RESEARCH_ONLY"
STATE_STALE_NO_JUDGMENT = "STALE_NO_JUDGMENT"
STATE_ROUTE_BLOCKED = "ROUTE_BLOCKED"
STATE_NO_TRADE_EVIDENCE = "NO_TRADE_EVIDENCE"
STATE_LEDGER_REPAIR_REQUIRED = "LEDGER_REPAIR_REQUIRED"

DECISION_KEEP_PAPER = "KEEP_PAPER"
DECISION_OBSERVE_MORE = "OBSERVE_MORE"
DECISION_DEMOTE_TO_SHADOW = "DEMOTE_TO_SHADOW"
DECISION_KEEP_RESEARCH_ONLY = "KEEP_RESEARCH_ONLY"
DECISION_REPAIR_ROUTE_OR_CADENCE = "REPAIR_ROUTE_OR_CADENCE"
DECISION_REPAIR_LEDGER = "REPAIR_LEDGER"

_FRESH_ACTIVATION_STATES = {
    "PAPER_RUNNING",
    "PAPER_ONLINE_WAITING",
    "PAPER_ROUTE_READY_NO_JOURNAL",
}
_ROUTE_BLOCKED_STATES = {
    "ROUTE_BLOCKED",
    "NEEDS_RUNTIME_ADAPTER",
    "MANIFEST_UNSAFE",
}
_DOCTOR_BLOCKED_STATES = {
    "ROUTE_NOT_WIRED",
    "MANIFEST_UNSAFE",
    "RUNNER_SERVICE_DOWN",
}
_STALE_CADENCE_STATES = {
    "EVAL_STALE",
    "HEARTBEAT_STALE",
    "JOURNAL_MISSING",
}
_STALE_DOCTOR_STATES = {
    "JOURNAL_STALE",
    "ROUTE_READY_JOURNAL_MISSING",
}


@dataclass(frozen=True)
class LaneSurvivalConfig:
    min_closed_trades: int = 20
    min_profit_factor: float = 1.5
    min_avg_net_bps: float = 25.0
    probation_closed_trades: int = 5
    demote_negative_bps: float = -10.0
    demote_profit_factor: float = 0.8
    max_rows: int = 180

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_lane_survival(
    *,
    activation: Mapping[str, Any] | None = None,
    cadence: Mapping[str, Any] | None = None,
    performance: Mapping[str, Any] | None = None,
    route_doctor: Mapping[str, Any] | None = None,
    activation_path: Path | str = DEFAULT_ACTIVATION,
    cadence_path: Path | str = DEFAULT_CADENCE,
    performance_path: Path | str = DEFAULT_PERFORMANCE,
    route_doctor_path: Path | str = DEFAULT_ROUTE_DOCTOR,
    config: LaneSurvivalConfig = LaneSurvivalConfig(),
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the read-only survival report."""
    now = now or datetime.now(UTC)
    activation_path = Path(activation_path)
    cadence_path = Path(cadence_path)
    performance_path = Path(performance_path)
    route_doctor_path = Path(route_doctor_path)

    activation_payload = _payload_or_file(activation, activation_path)
    cadence_payload = _payload_or_file(cadence, cadence_path)
    performance_payload = _payload_or_file(performance, performance_path)
    route_payload = _payload_or_file(route_doctor, route_doctor_path)

    activation_index = _index_rows(activation_payload.get("rows", []))
    cadence_index = _index_rows(cadence_payload.get("rows", []))
    performance_index = _index_rows(performance_payload.get("rows", []))
    route_index = _index_rows(route_payload.get("rows", []))

    lane_ids = sorted(
        set(activation_index) | set(cadence_index) | set(performance_index) | set(route_index)
    )
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for lane_id in lane_ids:
        if not lane_id or lane_id in seen:
            continue
        row = _survival_row(
            lane_id,
            activation=activation_index.get(lane_id, {}),
            cadence=cadence_index.get(lane_id, {}),
            performance=performance_index.get(lane_id, {}),
            route=route_index.get(lane_id, {}),
            config=config,
        )
        canonical = str(row.get("lane_id") or lane_id)
        if canonical in seen:
            continue
        seen.add(canonical)
        rows.append(row)

    rows.sort(key=_row_sort_key)
    rows = rows[: max(1, int(config.max_rows))]
    summary = _summary(rows)
    return {
        "generated_at": now.isoformat(),
        "report_id": "lane_survival_v1",
        "mode": "read_only_lane_survival",
        "source_reports": {
            "activation": activation_payload.get("report_id"),
            "cadence": cadence_payload.get("report_id"),
            "performance": performance_payload.get("report_id"),
            "route_doctor": route_payload.get("report_id"),
        },
        "source_generated_at": {
            "activation": activation_payload.get("generated_at"),
            "cadence": cadence_payload.get("generated_at"),
            "performance": performance_payload.get("generated_at"),
            "route_doctor": route_payload.get("generated_at"),
        },
        "inputs": {
            "activation_path": str(activation_path),
            "cadence_path": str(cadence_path),
            "performance_path": str(performance_path),
            "route_doctor_path": str(route_doctor_path),
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
            "survivor_candidate_requires_human_review": True,
            "demotion_is_recommendation_only": True,
        },
        "can_trade": False,
        "can_promote": False,
    }


def publish_lane_survival(
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
        "=== Paper lane survival ===",
        f"generated: {payload.get('generated_at')}",
        str(payload.get("operator_answer") or ""),
        (
            "summary: "
            f"{summary.get('total_lanes', 0)} lanes, "
            f"{summary.get('survivor_candidates', 0)} survivor candidates, "
            f"{summary.get('observe_more', 0)} observe-more, "
            f"{summary.get('demote_to_shadow', 0)} demote recommendations"
        ),
    ]
    for row in list(payload.get("rows", []))[:limit]:
        lines.append(
            f"  {row.get('survival_state', ''):<28} "
            f"{row.get('lane_id', ''):<42} "
            f"score {row.get('survival_score', 0):>5.1f} "
            f"{row.get('closed_trades', 0):>3} closed "
            f"net ${row.get('closed_net_pnl_usd', 0.0):>8.2f} "
            f"PF {row.get('profit_factor', 0.0):>5.2f} "
            f"{row.get('decision', '')}"
        )
    lines.append("read-only: can_trade=false can_promote=false")
    return "\n".join(lines)


def _survival_row(
    lane_id: str,
    *,
    activation: Mapping[str, Any],
    cadence: Mapping[str, Any],
    performance: Mapping[str, Any],
    route: Mapping[str, Any],
    config: LaneSurvivalConfig,
) -> dict[str, Any]:
    canonical = _canonical_lane_id(lane_id, activation, cadence, performance, route)
    activation_state = str(activation.get("activation_state") or "")
    cadence_state = str(cadence.get("cadence_state") or "")
    doctor_state = str(route.get("doctor_state") or "")
    perf_state = str(performance.get("state") or "")
    exchange = _first_text(
        performance.get("exchange"),
        activation.get("exchange"),
        route.get("exchange"),
        cadence.get("exchange"),
    )
    symbol = _first_text(
        performance.get("symbol"),
        activation.get("symbol"),
        route.get("symbol"),
        cadence.get("symbol"),
    )
    timeframe = _first_text(
        performance.get("timeframe"),
        activation.get("timeframe"),
        route.get("timeframe"),
        cadence.get("timeframe"),
    )
    strategy_id = _first_text(
        performance.get("strategy_id"),
        activation.get("strategy_id"),
        route.get("strategy_id"),
        cadence.get("strategy_id"),
        canonical,
    )

    closed = _int(performance.get("closed_trades"))
    closed_net = _float(
        performance.get(
            "closed_net_pnl_usd",
            performance.get("net_pnl_usd", 0.0),
        )
    )
    total_net = _float(performance.get("net_pnl_usd"))
    fees = _float(performance.get("fees_usd"))
    avg_bps = _maybe_float(performance.get("avg_closed_trade_net_bps"))
    pf = _float(performance.get("profit_factor"))
    win_rate = _maybe_float(performance.get("win_rate"))
    open_fill_count = _int(performance.get("open_fill_count"))
    open_fee_drag = _float(performance.get("open_position_entry_fees_usd"))
    unpaired_closing_fills = _int(performance.get("unpaired_closing_fills"))
    ledger_ok = bool(performance.get("ledger_ok", True))
    drift_flags = [
        str(x)
        for x in performance.get("journal_drift_flags", []) or []
        if str(x).strip()
    ]

    facts = {
        "route_blocked": _route_blocked(activation_state, doctor_state),
        "blocked_negative": activation_state == "BLOCKED_NEGATIVE_EDGE"
        or doctor_state == "BLOCKED_NEGATIVE",
        "stale": _stale(perf_state, cadence_state, doctor_state),
        "fresh_route": activation_state in _FRESH_ACTIVATION_STATES
        and cadence_state not in _STALE_CADENCE_STATES
        and doctor_state not in _STALE_DOCTOR_STATES,
        # Ledger drift = ACTUAL corruption only: an orphan closing fill, or the
        # performance layer's own integrity flag. A benign open position (an
        # entry fill awaiting its close, and its entry-fee drag) is NOT
        # corruption — yet those show up in journal_drift_flags too, so keying
        # off `bool(drift_flags)` false-flagged every lane holding a position as
        # LEDGER_REPAIR_REQUIRED (overriding even PAPER_ACTIVE_PROFITABLE, and
        # docking the darwinian score −10). Real corruption is unpaired closes.
        "ledger_drift": (not ledger_ok) or unpaired_closing_fills > 0,
    }
    blockers = _blockers(
        closed=closed,
        closed_net=closed_net,
        pf=pf,
        avg_bps=avg_bps,
        activation_state=activation_state,
        cadence_state=cadence_state,
        doctor_state=doctor_state,
        perf_state=perf_state,
        facts=facts,
        drift_flags=drift_flags,
        config=config,
    )
    state, decision, action = _classify(
        closed=closed,
        closed_net=closed_net,
        pf=pf,
        avg_bps=avg_bps,
        facts=facts,
        config=config,
    )
    components = _score_components(
        closed=closed,
        closed_net=closed_net,
        pf=pf,
        avg_bps=avg_bps,
        win_rate=win_rate,
        facts=facts,
        open_fill_count=open_fill_count,
        unpaired_closing_fills=unpaired_closing_fills,
        config=config,
    )
    score = round(sum(components.values()), 2)
    return {
        "lane_id": canonical,
        "exchange": exchange,
        "symbol": symbol,
        "timeframe": timeframe,
        "strategy_id": strategy_id,
        "survival_state": state,
        "survival_score": score,
        "decision": decision,
        "next_action": action,
        "blockers": blockers,
        "score_components": components,
        "activation_state": activation_state or None,
        "cadence_state": cadence_state or None,
        "doctor_state": doctor_state or None,
        "performance_state": perf_state or None,
        "closed_trades": closed,
        "wins": _int(performance.get("wins")),
        "losses": _int(performance.get("losses")),
        "win_rate": round(win_rate, 6) if win_rate is not None else None,
        "profit_factor": pf,
        "fees_usd": round(fees, 6),
        "net_pnl_usd": round(total_net, 6),
        "closed_net_pnl_usd": round(closed_net, 6),
        "avg_closed_trade_net_bps": round(avg_bps, 4) if avg_bps is not None else None,
        "live_signals": _int(performance.get("live_signals")),
        "paper_order_intents": _int(performance.get("paper_order_intents")),
        "fills": _int(performance.get("fills")),
        "open_fill_count": open_fill_count,
        "open_position_entry_fees_usd": round(open_fee_drag, 6),
        "unpaired_closing_fills": unpaired_closing_fills,
        "journal_drift_flags": drift_flags,
        "exit_quality": _exit_quality(
            closed=closed,
            closed_net=closed_net,
            pf=pf,
            avg_bps=avg_bps,
            win_rate=win_rate,
            fees=fees,
            open_fill_count=open_fill_count,
            open_fee_drag=open_fee_drag,
            unpaired_closing_fills=unpaired_closing_fills,
            config=config,
        ),
        "latest_why_no_trade": performance.get("latest_why_no_trade")
        or cadence.get("latest_why")
        or route.get("next_action")
        or activation.get("next_action"),
        "evidence": {
            "latest_performance_ts": performance.get("latest_ts"),
            "latest_cadence_age_hours": cadence.get("age_hours"),
            "latest_route_age_hours": route.get("age_hours"),
        },
        "can_trade": False,
        "can_promote": False,
    }


def _classify(
    *,
    closed: int,
    closed_net: float,
    pf: float,
    avg_bps: float | None,
    facts: Mapping[str, bool],
    config: LaneSurvivalConfig,
) -> tuple[str, str, str]:
    if facts["ledger_drift"]:
        return (
            STATE_LEDGER_REPAIR_REQUIRED,
            DECISION_REPAIR_LEDGER,
            "repair fill/close pairing before judging this lane",
        )
    if facts["route_blocked"]:
        return (
            STATE_ROUTE_BLOCKED,
            DECISION_REPAIR_ROUTE_OR_CADENCE,
            "repair runtime route/cadence before judging this lane",
        )
    if facts["blocked_negative"]:
        return (
            STATE_RESEARCH_ONLY,
            DECISION_KEEP_RESEARCH_ONLY,
            "keep this lane out of paper until a new research proof exists",
        )
    if closed == 0:
        if facts["stale"]:
            return (
                STATE_STALE_NO_JUDGMENT,
                DECISION_REPAIR_ROUTE_OR_CADENCE,
                "restore fresh eval/heartbeat proof before judging the lane",
            )
        return (
            STATE_NO_TRADE_EVIDENCE,
            DECISION_OBSERVE_MORE,
            "keep in paper only if the scanner explains why no trade every cycle",
        )
    if facts["stale"]:
        return (
            STATE_STALE_NO_JUDGMENT,
            DECISION_REPAIR_ROUTE_OR_CADENCE,
            "performance exists but is stale; repair route/cadence before judging",
        )
    if (
        closed >= config.min_closed_trades
        and closed_net > 0
        and pf >= config.min_profit_factor
        and (avg_bps or 0.0) >= config.min_avg_net_bps
    ):
        return (
            STATE_PAPER_SURVIVOR_CANDIDATE,
            DECISION_KEEP_PAPER,
            "prepare human review; still needs untouched judgment/live checklist",
        )
    if closed_net < 0 and (
        closed >= config.probation_closed_trades
        or (avg_bps is not None and avg_bps <= config.demote_negative_bps)
        or pf <= config.demote_profit_factor
    ):
        return (
            STATE_DEMOTE_TO_SHADOW,
            DECISION_DEMOTE_TO_SHADOW,
            "demote to shadow and mine entry/exit failures before more paper exposure",
        )
    if closed_net < 0:
        return (
            STATE_PAPER_PROBATION,
            DECISION_OBSERVE_MORE,
            "probation: one more negative close should trigger shadow demotion",
        )
    return (
        STATE_PAPER_OBSERVE_MORE,
        DECISION_OBSERVE_MORE,
        "positive or flat but under-gated; keep observing until sample/PF/bps clear",
    )


def _blockers(
    *,
    closed: int,
    closed_net: float,
    pf: float,
    avg_bps: float | None,
    activation_state: str,
    cadence_state: str,
    doctor_state: str,
    perf_state: str,
    facts: Mapping[str, bool],
    drift_flags: list[str],
    config: LaneSurvivalConfig,
) -> list[str]:
    blockers: list[str] = []
    if facts["route_blocked"]:
        blockers.append(f"route blocked: {activation_state or doctor_state or 'unknown'}")
    if facts["blocked_negative"]:
        blockers.append("negative-edge block is already active")
    if facts["stale"]:
        stale = cadence_state or doctor_state or perf_state or "stale proof"
        blockers.append(f"stale/no current judgment: {stale}")
    if drift_flags:
        blockers.extend(drift_flags)
    if closed < config.min_closed_trades:
        blockers.append(f"needs {config.min_closed_trades - closed} more closed trade(s)")
    if closed > 0 and closed_net <= 0:
        blockers.append("closed trades are negative after fees")
    if closed > 0 and pf < config.min_profit_factor:
        blockers.append(f"PF {pf:.2f} below {config.min_profit_factor:.2f}")
    if closed > 0 and (avg_bps is None or avg_bps < config.min_avg_net_bps):
        value = "--" if avg_bps is None else f"{avg_bps:.2f}"
        blockers.append(f"avg net {value}bps below {config.min_avg_net_bps:.2f}bps")
    return blockers or ["survival gates clear; human review still required"]


def _score_components(
    *,
    closed: int,
    closed_net: float,
    pf: float,
    avg_bps: float | None,
    win_rate: float | None,
    facts: Mapping[str, bool],
    open_fill_count: int,
    unpaired_closing_fills: int,
    config: LaneSurvivalConfig,
) -> dict[str, float]:
    evidence = min(20.0, (closed / max(1, config.min_closed_trades)) * 20.0)
    expectancy = 0.0
    if avg_bps is not None and avg_bps > 0:
        expectancy = min(25.0, (avg_bps / max(0.0001, config.min_avg_net_bps)) * 25.0)
    if closed_net < 0:
        expectancy = 0.0
    pf_score = min(20.0, (pf / max(0.0001, config.min_profit_factor)) * 20.0)
    if closed == 0:
        pf_score = 0.0
    freshness = (
        15.0
        if not facts["stale"] and not facts["route_blocked"] and not facts["ledger_drift"]
        else 0.0
    )
    execution = max(
        0.0,
        10.0
        - (10.0 if facts["ledger_drift"] else 0.0)
        - min(4.0, open_fill_count * 2.0)
        - min(4.0, unpaired_closing_fills * 2.0),
    )
    exit_quality = 0.0
    if closed > 0:
        exit_quality = min(10.0, ((win_rate or 0.0) * 10.0) + (2.0 if pf >= 1.0 else 0.0))
    return {
        "evidence": round(evidence, 2),
        "expectancy": round(expectancy, 2),
        "profit_factor": round(pf_score, 2),
        "freshness": round(freshness, 2),
        "execution_hygiene": round(execution, 2),
        "exit_quality": round(exit_quality, 2),
    }


def _exit_quality(
    *,
    closed: int,
    closed_net: float,
    pf: float,
    avg_bps: float | None,
    win_rate: float | None,
    fees: float,
    open_fill_count: int,
    open_fee_drag: float,
    unpaired_closing_fills: int,
    config: LaneSurvivalConfig,
) -> dict[str, Any]:
    if closed == 0:
        label = "NO_EXIT_SAMPLE"
    elif unpaired_closing_fills:
        label = "LEDGER_DRIFT"
    elif closed_net < 0 or (avg_bps is not None and avg_bps < 0):
        label = "FEE_WALL_DRAG"
    elif (avg_bps or 0.0) >= config.min_avg_net_bps and pf >= config.min_profit_factor:
        label = "CAPTURE_OK"
    else:
        label = "UNDER_SAMPLED_CAPTURE"
    return {
        "label": label,
        "closed_trades": closed,
        "win_rate": round(win_rate, 6) if win_rate is not None else None,
        "profit_factor": pf,
        "avg_net_bps": round(avg_bps, 4) if avg_bps is not None else None,
        "closed_net_pnl_usd": round(closed_net, 6),
        "fee_drag_usd": round(fees, 6),
        "open_fill_count": open_fill_count,
        "open_fee_drag_usd": round(open_fee_drag, 6),
        "unpaired_closing_fills": unpaired_closing_fills,
    }


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    states = Counter(str(r.get("survival_state") or "") for r in rows)
    decisions = Counter(str(r.get("decision") or "") for r in rows)
    scores = [_float(r.get("survival_score")) for r in rows]
    closed_rows = [r for r in rows if _int(r.get("closed_trades")) > 0]
    return {
        "total_lanes": len(rows),
        "survivor_candidates": states[STATE_PAPER_SURVIVOR_CANDIDATE],
        "observe_more": states[STATE_PAPER_OBSERVE_MORE],
        "probation": states[STATE_PAPER_PROBATION],
        "demote_to_shadow": states[STATE_DEMOTE_TO_SHADOW],
        "research_only": states[STATE_RESEARCH_ONLY],
        "stale_no_judgment": states[STATE_STALE_NO_JUDGMENT],
        "route_blocked": states[STATE_ROUTE_BLOCKED],
        "no_trade_evidence": states[STATE_NO_TRADE_EVIDENCE],
        "ledger_repair_required": states[STATE_LEDGER_REPAIR_REQUIRED],
        "closed_trade_lanes": len(closed_rows),
        "closed_trades": sum(_int(r.get("closed_trades")) for r in rows),
        "net_pnl_usd": round(sum(_float(r.get("net_pnl_usd")) for r in rows), 6),
        "closed_net_pnl_usd": round(
            sum(_float(r.get("closed_net_pnl_usd")) for r in rows), 6
        ),
        "fees_usd": round(sum(_float(r.get("fees_usd")) for r in rows), 6),
        "best_survival_score": round(max(scores), 2) if scores else 0.0,
        "avg_survival_score": round(sum(scores) / len(scores), 2) if scores else 0.0,
        "state_counts": dict(sorted(states.items())),
        "decision_counts": dict(sorted(decisions.items())),
        "can_trade": False,
        "can_promote": False,
    }


def _boards(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    return {
        "survivor_candidates": [
            _slim(r) for r in rows if r.get("survival_state") == STATE_PAPER_SURVIVOR_CANDIDATE
        ],
        "observe_more": [
            _slim(r) for r in rows if r.get("survival_state") == STATE_PAPER_OBSERVE_MORE
        ],
        "probation": [
            _slim(r) for r in rows if r.get("survival_state") == STATE_PAPER_PROBATION
        ],
        "demote_to_shadow": [
            _slim(r) for r in rows if r.get("survival_state") == STATE_DEMOTE_TO_SHADOW
        ],
        "fix_first": [
            _slim(r)
            for r in rows
            if r.get("survival_state")
            in {STATE_STALE_NO_JUDGMENT, STATE_ROUTE_BLOCKED, STATE_LEDGER_REPAIR_REQUIRED}
        ],
    }


def _slim(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "lane_id": row.get("lane_id"),
        "exchange": row.get("exchange"),
        "symbol": row.get("symbol"),
        "timeframe": row.get("timeframe"),
        "strategy_id": row.get("strategy_id"),
        "survival_state": row.get("survival_state"),
        "survival_score": row.get("survival_score"),
        "decision": row.get("decision"),
        "closed_trades": row.get("closed_trades"),
        "closed_net_pnl_usd": row.get("closed_net_pnl_usd"),
        "profit_factor": row.get("profit_factor"),
        "avg_closed_trade_net_bps": row.get("avg_closed_trade_net_bps"),
        "next_action": row.get("next_action"),
    }


def _operator_answer(summary: Mapping[str, Any]) -> str:
    candidates = _int(summary.get("survivor_candidates"))
    demote = _int(summary.get("demote_to_shadow"))
    probation = _int(summary.get("probation"))
    stale = _int(summary.get("stale_no_judgment")) + _int(summary.get("route_blocked"))
    observe = _int(summary.get("observe_more"))
    no_trades = _int(summary.get("no_trade_evidence"))
    if candidates:
        return (
            f"{candidates} lane(s) are paper survivor candidates. "
            "They still need human review and untouched/live checklist gates."
        )
    if demote:
        return (
            f"{demote} lane(s) should be demoted to shadow before more paper fee bleed; "
            f"{probation} are on probation."
        )
    if stale:
        return f"{stale} lane(s) need route/cadence repair before performance can be trusted."
    if observe:
        return f"{observe} lane(s) are positive/flat but still under-sampled or under-gated."
    if no_trades:
        return f"{no_trades} lane(s) have no closed trade evidence yet."
    return "No paper lane survival evidence is available yet."


def _payload_or_file(payload: Mapping[str, Any] | None, path: Path) -> dict[str, Any]:
    if isinstance(payload, Mapping):
        return dict(payload)
    return _read_json_payload(path, {"rows": [], "summary": {}})


def _read_json_payload(path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback
    return payload if isinstance(payload, dict) else fallback


def _index_rows(rows: Any) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    if not isinstance(rows, list):
        return index
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        row = dict(raw)
        for key in _lane_keys(row):
            index.setdefault(key, row)
    return index


def _lane_keys(row: Mapping[str, Any]) -> list[str]:
    keys: list[str] = []
    for field in ("lane_id", "expected_lane_id", "trial_id"):
        raw = str(row.get(field) or "").strip()
        if raw:
            keys.append(raw)
    runtime = row.get("runtime")
    if isinstance(runtime, Mapping):
        for raw in runtime.get("desired_lane_ids") or []:
            text = str(raw or "").strip()
            if text:
                keys.append(text)
    route_ids = row.get("route_lane_ids")
    if isinstance(route_ids, list):
        for raw in route_ids:
            text = str(raw or "").strip()
            if text:
                keys.append(text)
    return list(dict.fromkeys(keys))


def _canonical_lane_id(
    lane_id: str,
    activation: Mapping[str, Any],
    cadence: Mapping[str, Any],
    performance: Mapping[str, Any],
    route: Mapping[str, Any],
) -> str:
    for row in (performance, cadence, route):
        for field in ("lane_id", "expected_lane_id", "trial_id"):
            raw = str(row.get(field) or "").strip()
            if raw:
                return raw
    runtime = activation.get("runtime")
    if isinstance(runtime, Mapping):
        for raw in runtime.get("desired_lane_ids") or []:
            text = str(raw or "").strip()
            if text:
                return text
    route_ids = activation.get("route_lane_ids")
    if isinstance(route_ids, list):
        for raw in route_ids:
            text = str(raw or "").strip()
            if text:
                return text
    for field in ("lane_id", "expected_lane_id", "trial_id"):
        raw = str(activation.get(field) or "").strip()
        if raw:
            return raw
    return lane_id


def _route_blocked(activation_state: str, doctor_state: str) -> bool:
    return activation_state in _ROUTE_BLOCKED_STATES or doctor_state in _DOCTOR_BLOCKED_STATES


def _stale(perf_state: str, cadence_state: str, doctor_state: str) -> bool:
    return (
        perf_state == "NO_RECENT_PROOF"
        or cadence_state in _STALE_CADENCE_STATES
        or doctor_state in _STALE_DOCTOR_STATES
    )


def _row_sort_key(row: Mapping[str, Any]) -> tuple[int, float, float, str]:
    priority = {
        STATE_PAPER_SURVIVOR_CANDIDATE: 0,
        STATE_DEMOTE_TO_SHADOW: 1,
        STATE_PAPER_PROBATION: 2,
        STATE_PAPER_OBSERVE_MORE: 3,
        STATE_LEDGER_REPAIR_REQUIRED: 4,
        STATE_STALE_NO_JUDGMENT: 5,
        STATE_ROUTE_BLOCKED: 6,
        STATE_NO_TRADE_EVIDENCE: 7,
        STATE_RESEARCH_ONLY: 8,
    }.get(str(row.get("survival_state") or ""), 9)
    return (
        priority,
        -_float(row.get("survival_score")),
        -_float(row.get("closed_net_pnl_usd")),
        str(row.get("lane_id") or ""),
    )


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _maybe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--activation", type=Path, default=DEFAULT_ACTIVATION)
    parser.add_argument("--cadence", type=Path, default=DEFAULT_CADENCE)
    parser.add_argument("--performance", type=Path, default=DEFAULT_PERFORMANCE)
    parser.add_argument("--route-doctor", type=Path, default=DEFAULT_ROUTE_DOCTOR)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--feed", type=Path, default=DEFAULT_FEED)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval-seconds", type=float, default=60.0)
    parser.add_argument("--min-closed-trades", type=int, default=20)
    parser.add_argument("--min-profit-factor", type=float, default=1.5)
    parser.add_argument("--min-avg-net-bps", type=float, default=25.0)
    parser.add_argument("--max-rows", type=int, default=180)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    config = LaneSurvivalConfig(
        min_closed_trades=args.min_closed_trades,
        min_profit_factor=args.min_profit_factor,
        min_avg_net_bps=args.min_avg_net_bps,
        max_rows=args.max_rows,
    )
    while True:
        payload = build_lane_survival(
            activation_path=args.activation,
            cadence_path=args.cadence,
            performance_path=args.performance,
            route_doctor_path=args.route_doctor,
            config=config,
        )
        publish_lane_survival(payload, args.out, args.feed)
        print(render_report(payload), flush=True)
        if args.once:
            return 0
        time.sleep(max(1.0, float(args.interval_seconds)))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
