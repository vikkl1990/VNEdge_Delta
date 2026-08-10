"""Paper roster drift reconciler.

The paper lane governor proposes a bounded paper roster. The runtime can still
show additional paper lanes when observation probes, old manifests, or demotion
queues stay online. This module makes that drift explicit.

Read-only by design: it cannot start, stop, promote, demote, or trade a lane.
"""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_RESEARCH_DIR = Path("research/live_research")
DEFAULT_GOVERNOR = DEFAULT_RESEARCH_DIR / "paper_lane_governor_latest.json"
DEFAULT_SCANNER = DEFAULT_RESEARCH_DIR / "realtime_scanner_latest.json"
DEFAULT_ACTIVATION = DEFAULT_RESEARCH_DIR / "paper_lane_activation_latest.json"
DEFAULT_SHADOW_MANIFEST = DEFAULT_RESEARCH_DIR / "shadow_lanes.json"
DEFAULT_OUT = DEFAULT_RESEARCH_DIR / "paper_roster_drift_latest.json"
DEFAULT_FEED = DEFAULT_RESEARCH_DIR / "paper_roster_drift_feed.jsonl"

STATE_EXPECTED_RUNNING = "EXPECTED_RUNNING"
STATE_EXPECTED_MISSING = "EXPECTED_MISSING"
STATE_EXTRA_RUNNING_PAPER = "EXTRA_RUNNING_PAPER"
STATE_DEMOTION_STILL_RUNNING = "DEMOTION_STILL_RUNNING"
STATE_PROBATION_STILL_RUNNING = "PROBATION_STILL_RUNNING"
STATE_REPAIR_STILL_RUNNING = "REPAIR_STILL_RUNNING"


@dataclass(frozen=True)
class PaperRosterDriftConfig:
    max_rows: int = 180
    max_runtime_age_seconds: float = 3 * 60 * 60

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_paper_roster_drift(
    *,
    governor: Mapping[str, Any] | None = None,
    scanner: Mapping[str, Any] | None = None,
    activation: Mapping[str, Any] | None = None,
    shadow_manifest: Mapping[str, Any] | None = None,
    governor_path: Path | str = DEFAULT_GOVERNOR,
    scanner_path: Path | str = DEFAULT_SCANNER,
    activation_path: Path | str = DEFAULT_ACTIVATION,
    shadow_manifest_path: Path | str = DEFAULT_SHADOW_MANIFEST,
    config: PaperRosterDriftConfig = PaperRosterDriftConfig(),
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the read-only roster drift payload."""

    now = now or datetime.now(UTC)
    governor_path = Path(governor_path)
    scanner_path = Path(scanner_path)
    activation_path = Path(activation_path)
    shadow_manifest_path = Path(shadow_manifest_path)
    governor_payload = (
        dict(governor)
        if isinstance(governor, Mapping)
        else _read_json_payload(governor_path, {"proposed_roster": {}, "rows": []})
    )
    scanner_payload = (
        dict(scanner)
        if isinstance(scanner, Mapping)
        else _read_json_payload(scanner_path, {"rows": [], "summary": {}})
    )
    activation_payload = (
        dict(activation)
        if isinstance(activation, Mapping)
        else _read_json_payload(activation_path, {"rows": [], "summary": {}})
    )
    shadow_payload = (
        dict(shadow_manifest)
        if isinstance(shadow_manifest, Mapping)
        else _read_json_payload(
            shadow_manifest_path, {"lanes": [], "blocked": [], "shadow_trials": []}
        )
    )

    proposed = (
        governor_payload.get("proposed_roster")
        if isinstance(governor_payload.get("proposed_roster"), Mapping)
        else {}
    )
    expected = _refs(proposed.get("paper_lanes"), source="governor_paper_roster")
    demotion = _refs(proposed.get("demote_to_shadow"), source="governor_demotion")
    probation = _refs(proposed.get("probation_shadow_watch"), source="governor_probation")
    repair = _refs(proposed.get("repair_first"), source="governor_repair")
    actual, stale_actual = _actual_paper_refs(
        scanner_payload,
        activation_payload,
        config=config,
        now=now,
    )

    actual_index = _index_refs(actual)
    expected_index = _index_refs(expected)
    demotion_index = _index_refs(demotion)
    probation_index = _index_refs(probation)
    repair_index = _index_refs(repair)

    rows: list[dict[str, Any]] = []
    matched_actual_keys: set[str] = set()
    for ref in expected:
        match = _find_match(ref, actual_index)
        if match is not None:
            matched_actual_keys.update(match["_keys"])
            rows.append(_row(STATE_EXPECTED_RUNNING, ref, actual=match))
        else:
            rows.append(_row(STATE_EXPECTED_MISSING, ref))

    for ref in actual:
        if ref["_keys"] & matched_actual_keys or _find_match(ref, expected_index):
            continue
        status = STATE_EXTRA_RUNNING_PAPER
        if _find_match(ref, demotion_index):
            status = STATE_DEMOTION_STILL_RUNNING
        elif _find_match(ref, probation_index):
            status = STATE_PROBATION_STILL_RUNNING
        elif _find_match(ref, repair_index):
            status = STATE_REPAIR_STILL_RUNNING
        rows.append(_row(status, ref))

    rows.sort(key=_row_sort_key)
    rows = rows[: max(1, int(config.max_rows))]
    shadow_rows = _shadow_rows(shadow_payload, limit=max(1, int(config.max_rows)))
    lane_rows = _lane_rows(rows, shadow_rows, limit=max(1, int(config.max_rows)))
    summary = _summary(
        rows,
        expected=expected,
        actual=actual,
        stale_actual=stale_actual,
        shadow_rows=shadow_rows,
    )
    return {
        "generated_at": now.isoformat(),
        "report_id": "paper_roster_drift_v1",
        "mode": "read_only_unified_lane_roster",
        "inputs": {
            "governor_path": str(governor_path),
            "scanner_path": str(scanner_path),
            "activation_path": str(activation_path),
            "shadow_manifest_path": str(shadow_manifest_path),
        },
        "config": config.to_dict(),
        "summary": summary,
        "rows": rows,
        "shadow_rows": shadow_rows,
        "lane_rows": lane_rows,
        "operator_answer": _operator_answer(summary),
        "shadow_operator_answer": _shadow_operator_answer(summary),
        "roster_operator_answer": _roster_operator_answer(summary),
        "policy": {
            "read_only": True,
            "can_trade": False,
            "can_promote": False,
            "can_demote": False,
            "purpose": (
                "present paper and shadow lanes as one operator roster while "
                "preserving their different execution policies"
            ),
        },
        "can_trade": False,
        "can_promote": False,
    }


def publish_paper_roster_drift(
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
        "=== Unified lane roster ===",
        f"generated: {payload.get('generated_at')}",
        str(payload.get("roster_operator_answer") or payload.get("operator_answer") or ""),
        (
            "summary: "
            f"{summary.get('expected_paper_lanes', 0)} expected, "
            f"{summary.get('actual_paper_lanes', 0)} actual, "
            f"{summary.get('extra_paper_lanes', 0)} extra, "
            f"{summary.get('missing_paper_lanes', 0)} missing, "
            f"{summary.get('demotion_queue_running', 0)} demotion-running, "
            f"{summary.get('stale_paper_evidence_lanes', 0)} stale historical, "
            f"{summary.get('shadow_observation_lanes', 0)} shadow-observing, "
            f"{summary.get('shadow_blocked_lanes', 0)} shadow-blocked"
        ),
    ]
    lane_rows = payload.get("lane_rows") or []
    if not isinstance(lane_rows, list):
        lane_rows = []
    for row in list(lane_rows)[:limit]:
        lines.append(
            f"  {row.get('roster_mode', ''):<6} {row.get('roster_state', ''):<26} "
            f"{row.get('lane_id', ''):<42} "
            f"{row.get('exchange', ''):<12} {row.get('symbol', ''):<14} "
            f"{row.get('timeframe', ''):<4} {row.get('strategy_id', ''):<28} "
            f"{row.get('next_action', '')}"
        )
    lines.append("read-only: can_trade=false can_promote=false can_demote=false")
    return "\n".join(lines)


def _actual_paper_refs(
    scanner_payload: Mapping[str, Any],
    activation_payload: Mapping[str, Any],
    *,
    config: PaperRosterDriftConfig,
    now: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    out: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in scanner_payload.get("rows", []) or []:
        if not isinstance(row, Mapping) or not _is_paper_runtime(row):
            continue
        if not _has_recent_runtime_evidence(row, config=config, now=now):
            stale.append(_ref(row, source="stale_realtime_scanner"))
            continue
        ref = _ref(row, source="realtime_scanner")
        if not ref["_primary_key"] or ref["_primary_key"] in seen:
            continue
        out.append(ref)
        seen.add(ref["_primary_key"])
    for row in activation_payload.get("rows", []) or []:
        if not isinstance(row, Mapping) or not _is_activation_running(row):
            continue
        if not _has_recent_runtime_evidence(row, config=config, now=now):
            stale.append(_ref(row, source="stale_paper_activation"))
            continue
        runtime = row.get("runtime") if isinstance(row.get("runtime"), Mapping) else {}
        lane_ids = [
            str(item)
            for item in runtime.get("desired_lane_ids", []) or []
            if str(item).strip()
        ] or [str(row.get("trial_id") or row.get("lane_id") or "")]
        for lane_id in lane_ids:
            ref = _ref({**dict(row), "lane_id": lane_id}, source="paper_activation")
            if not ref["_primary_key"] or ref["_primary_key"] in seen:
                continue
            out.append(ref)
            seen.add(ref["_primary_key"])
    return out, [ref for ref in stale if ref.get("_primary_key")]


def _refs(rows: Any, *, source: str) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows:
        if isinstance(row, Mapping):
            ref = _ref(row, source=source)
            if ref["_primary_key"]:
                out.append(ref)
    return out


def _ref(row: Mapping[str, Any], *, source: str) -> dict[str, Any]:
    lane_id = str(row.get("lane_id") or row.get("trial_id") or "").strip()
    strategy_id = str(row.get("strategy_id") or row.get("family") or "").strip()
    exchange = str(row.get("exchange") or row.get("venue") or "").strip()
    symbol = str(row.get("symbol") or "").strip()
    timeframe = str(row.get("timeframe") or "").strip()
    lane_key = str(row.get("lane_key") or "").strip().lower()
    signature = _signature(strategy_id, exchange, symbol, timeframe)
    keys = {key for key in (lane_id, lane_key, signature) if key}
    return {
        "lane_id": lane_id,
        "lane_key": lane_key,
        "signature": signature,
        "exchange": exchange,
        "symbol": symbol,
        "timeframe": timeframe,
        "strategy_id": strategy_id,
        "mode": row.get("mode"),
        "state": row.get("state") or row.get("activation_state") or row.get("governor_bucket"),
        "source": source,
        "_primary_key": lane_id or lane_key or signature,
        "_keys": keys,
    }


def _index_refs(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        for key in row["_keys"]:
            out.setdefault(key, []).append(row)
    return out


def _find_match(
    ref: Mapping[str, Any], index: Mapping[str, list[dict[str, Any]]]
) -> dict[str, Any] | None:
    for key in ref.get("_keys", set()):
        matches = index.get(str(key), [])
        if matches:
            return matches[0]
    return None


def _row(
    state: str,
    ref: Mapping[str, Any],
    *,
    actual: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    source = actual or ref
    return {
        "drift_state": state,
        "lane_id": source.get("lane_id") or ref.get("lane_id"),
        "lane_key": source.get("lane_key") or ref.get("lane_key"),
        "signature": source.get("signature") or ref.get("signature"),
        "exchange": source.get("exchange") or ref.get("exchange"),
        "symbol": source.get("symbol") or ref.get("symbol"),
        "timeframe": source.get("timeframe") or ref.get("timeframe"),
        "strategy_id": source.get("strategy_id") or ref.get("strategy_id"),
        "expected_source": ref.get("source"),
        "actual_source": actual.get("source") if actual else None,
        "runtime_state": actual.get("state") if actual else None,
        "next_action": _next_action(state),
        "can_trade": False,
        "can_promote": False,
    }


def _summary(
    rows: list[dict[str, Any]],
    *,
    expected: list[dict[str, Any]],
    actual: list[dict[str, Any]],
    stale_actual: list[dict[str, Any]],
    shadow_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    missing = sum(1 for row in rows if row.get("drift_state") == STATE_EXPECTED_MISSING)
    non_expected = sum(
        1
        for row in rows
        if row.get("drift_state")
        in {
            STATE_EXTRA_RUNNING_PAPER,
            STATE_DEMOTION_STILL_RUNNING,
            STATE_PROBATION_STILL_RUNNING,
            STATE_REPAIR_STILL_RUNNING,
        }
    )
    demotion = sum(1 for row in rows if row.get("drift_state") == STATE_DEMOTION_STILL_RUNNING)
    probation = sum(1 for row in rows if row.get("drift_state") == STATE_PROBATION_STILL_RUNNING)
    repair = sum(1 for row in rows if row.get("drift_state") == STATE_REPAIR_STILL_RUNNING)
    expected_running = sum(1 for row in rows if row.get("drift_state") == STATE_EXPECTED_RUNNING)
    shadow_observing = sum(
        1 for row in shadow_rows if row.get("shadow_state") == "SHADOW_OBSERVING"
    )
    shadow_blocked = sum(
        1 for row in shadow_rows if row.get("shadow_state") == "SHADOW_BLOCKED"
    )
    shadow_trials = sum(
        1
        for row in shadow_rows
        if row.get("shadow_state") == "SHADOW_TRIAL_WAITING_ADAPTER"
    )
    shadow_pass = sum(
        1
        for row in shadow_rows
        if row.get("latest_judgment_verdict") == "PASS"
    )
    return {
        "expected_paper_lanes": len(expected),
        "actual_paper_lanes": len(actual),
        "stale_paper_evidence_lanes": len(stale_actual),
        "expected_running": expected_running,
        "missing_paper_lanes": missing,
        "extra_paper_lanes": non_expected,
        "demotion_queue_running": demotion,
        "probation_queue_running": probation,
        "repair_queue_running": repair,
        "drift_lanes": missing + non_expected,
        "drift_detected": bool(missing or non_expected),
        "shadow_observation_lanes": shadow_observing,
        "shadow_blocked_lanes": shadow_blocked,
        "shadow_trials_waiting_adapter": shadow_trials,
        "shadow_pass_lanes": shadow_pass,
        "can_trade": False,
        "can_promote": False,
    }


def _operator_answer(summary: Mapping[str, Any]) -> str:
    extra = int(summary.get("extra_paper_lanes") or 0)
    missing = int(summary.get("missing_paper_lanes") or 0)
    demotion = int(summary.get("demotion_queue_running") or 0)
    stale = int(summary.get("stale_paper_evidence_lanes") or 0)
    if demotion:
        return (
            f"{demotion} demotion-queue lane(s) still appear as paper; move them "
            "back to shadow before trusting paper PnL."
        )
    if extra:
        return f"{extra} paper lane(s) are outside the governor roster; reconcile runtime roster."
    if missing:
        return f"{missing} governor paper lane(s) are missing from runtime evidence."
    if stale:
        return (
            "Paper runtime matches the governor proposal; "
            f"{stale} stale paper evidence lane(s) were ignored as historical."
        )
    return "Paper runtime matches the governor proposal."


def _shadow_operator_answer(summary: Mapping[str, Any]) -> str:
    observing = int(summary.get("shadow_observation_lanes") or 0)
    blocked = int(summary.get("shadow_blocked_lanes") or 0)
    trials = int(summary.get("shadow_trials_waiting_adapter") or 0)
    if blocked:
        return (
            f"{blocked} shadow candidate(s) are blocked by judgment/lock policy; "
            "keep them out of paper until a fresh approved proof exists."
        )
    if trials:
        return (
            f"{trials} replay-positive shadow trial(s) still need a runtime "
            "adapter before they can produce live observation evidence."
        )
    if observing:
        return f"{observing} shadow lane(s) are observation-only and cannot trade."
    return "No shadow observation lanes are published."


def _roster_operator_answer(summary: Mapping[str, Any]) -> str:
    paper = int(summary.get("actual_paper_lanes") or 0)
    expected = int(summary.get("expected_paper_lanes") or 0)
    shadow = int(summary.get("shadow_observation_lanes") or 0)
    blocked = int(summary.get("shadow_blocked_lanes") or 0)
    drift = int(summary.get("drift_lanes") or 0)
    stale = int(summary.get("stale_paper_evidence_lanes") or 0)
    parts = [f"{paper}/{expected} paper lanes recent"]
    if shadow:
        parts.append(f"{shadow} shadow observing")
    if blocked:
        parts.append(f"{blocked} shadow blocked")
    if drift:
        parts.append(f"{drift} roster drift")
    if stale:
        parts.append(f"{stale} stale paper journals ignored")
    return "; ".join(parts) + "."


def _next_action(state: str) -> str:
    if state == STATE_EXPECTED_RUNNING:
        return "keep observing paper lane"
    if state == STATE_EXPECTED_MISSING:
        return "start or repair the approved paper route"
    if state == STATE_DEMOTION_STILL_RUNNING:
        return "demote this paper lane back to shadow"
    if state == STATE_PROBATION_STILL_RUNNING:
        return "hold out of active paper roster until probation clears"
    if state == STATE_REPAIR_STILL_RUNNING:
        return "repair route/cadence/ledger before spending paper cycles"
    return "review why this lane is paper outside the governor roster"


def _lane_rows(
    paper_rows: list[dict[str, Any]],
    shadow_rows: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in paper_rows:
        rows.append({
            "roster_mode": "paper",
            "roster_state": row.get("drift_state"),
            "lane_id": row.get("lane_id"),
            "lane_key": row.get("lane_key"),
            "signature": row.get("signature"),
            "exchange": row.get("exchange"),
            "symbol": row.get("symbol"),
            "timeframe": row.get("timeframe"),
            "strategy_id": row.get("strategy_id"),
            "source": row.get("actual_source") or row.get("expected_source"),
            "runtime_state": row.get("runtime_state"),
            "latest_judgment_verdict": "",
            "next_action": row.get("next_action"),
            "paper_trading_enabled": row.get("drift_state") == STATE_EXPECTED_RUNNING,
            "shadow_observation_only": False,
            "live_trade_enabled": False,
            "can_trade": False,
            "can_promote": False,
        })
    for row in shadow_rows:
        rows.append({
            "roster_mode": "shadow",
            "roster_state": row.get("shadow_state"),
            "lane_id": row.get("lane_id"),
            "lane_key": "",
            "signature": "",
            "exchange": row.get("exchange"),
            "symbol": row.get("symbol"),
            "timeframe": row.get("timeframe"),
            "strategy_id": row.get("strategy_id"),
            "source": "shadow_manifest",
            "runtime_state": row.get("reason") or "",
            "latest_judgment_verdict": row.get("latest_judgment_verdict"),
            "source_verdict": row.get("source_verdict"),
            "next_action": row.get("next_action"),
            "paper_trading_enabled": False,
            "shadow_observation_only": True,
            "live_trade_enabled": False,
            "can_trade": False,
            "can_promote": False,
        })
    rows.sort(key=_lane_row_sort_key)
    return rows[:limit]


def _lane_row_sort_key(row: Mapping[str, Any]) -> tuple[int, str]:
    state = str(row.get("roster_state") or "")
    mode = str(row.get("roster_mode") or "")
    priority = {
        STATE_DEMOTION_STILL_RUNNING: 0,
        STATE_EXTRA_RUNNING_PAPER: 1,
        STATE_EXPECTED_MISSING: 2,
        STATE_PROBATION_STILL_RUNNING: 3,
        STATE_REPAIR_STILL_RUNNING: 4,
        "SHADOW_BLOCKED": 5,
        "SHADOW_TRIAL_WAITING_ADAPTER": 6,
        STATE_EXPECTED_RUNNING: 7,
        "SHADOW_OBSERVING": 8,
    }.get(state, 9)
    return (
        priority,
        mode,
        str(row.get("lane_id") or row.get("signature") or row.get("strategy_id") or ""),
    )


def _shadow_rows(payload: Mapping[str, Any], *, limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for lane in payload.get("lanes", []) or []:
        if not isinstance(lane, Mapping):
            continue
        latest = lane.get("latest_judgment")
        latest = latest if isinstance(latest, Mapping) else {}
        rows.append({
            "shadow_state": "SHADOW_OBSERVING",
            "lane_id": str(lane.get("lane_id") or ""),
            "exchange": str(lane.get("exchange") or ""),
            "symbol": str(lane.get("symbol") or ""),
            "timeframe": str(lane.get("timeframe") or ""),
            "strategy_id": str(lane.get("strategy_id") or lane.get("strategy") or ""),
            "latest_judgment_verdict": str(latest.get("verdict") or ""),
            "source_verdict": lane.get("source_verdict"),
            "next_action": "observe live intents; paper requires governor approval",
            "can_trade": False,
            "can_promote": False,
        })
    for blocked in payload.get("blocked", []) or []:
        if not isinstance(blocked, Mapping):
            continue
        latest = blocked.get("latest_judgment")
        latest = latest if isinstance(latest, Mapping) else {}
        rows.append({
            "shadow_state": "SHADOW_BLOCKED",
            "lane_id": str(blocked.get("lane_id") or ""),
            "exchange": str(blocked.get("exchange") or ""),
            "symbol": str(blocked.get("symbol") or ""),
            "timeframe": str(blocked.get("timeframe") or ""),
            "strategy_id": str(blocked.get("strategy_id") or blocked.get("strategy") or ""),
            "latest_judgment_verdict": str(latest.get("verdict") or ""),
            "reason": str(blocked.get("reason") or ""),
            "next_action": "do not run; needs fresh approved proof or locked params",
            "can_trade": False,
            "can_promote": False,
        })
    for trial in payload.get("shadow_trials", []) or []:
        if not isinstance(trial, Mapping):
            continue
        rows.append({
            "shadow_state": "SHADOW_TRIAL_WAITING_ADAPTER",
            "lane_id": str(trial.get("trial_id") or trial.get("candidate_id") or ""),
            "exchange": str(trial.get("exchange") or ""),
            "symbol": str(trial.get("symbol") or ""),
            "timeframe": str(trial.get("timeframe") or ""),
            "strategy_id": str(trial.get("family") or trial.get("runtime_strategy_id") or ""),
            "latest_judgment_verdict": "",
            "reason": str(trial.get("status") or ""),
            "next_action": "build runtime shadow adapter before paper review",
            "can_trade": False,
            "can_promote": False,
        })
    rows.sort(key=_shadow_row_sort_key)
    return rows[:limit]


def _shadow_row_sort_key(row: Mapping[str, Any]) -> tuple[int, str]:
    priority = {
        "SHADOW_BLOCKED": 0,
        "SHADOW_TRIAL_WAITING_ADAPTER": 1,
        "SHADOW_OBSERVING": 2,
    }.get(str(row.get("shadow_state") or ""), 9)
    return (priority, str(row.get("lane_id") or row.get("strategy_id") or ""))


def _row_sort_key(row: Mapping[str, Any]) -> tuple[int, str]:
    priority = {
        STATE_DEMOTION_STILL_RUNNING: 0,
        STATE_EXTRA_RUNNING_PAPER: 1,
        STATE_EXPECTED_MISSING: 2,
        STATE_PROBATION_STILL_RUNNING: 3,
        STATE_REPAIR_STILL_RUNNING: 4,
        STATE_EXPECTED_RUNNING: 5,
    }.get(str(row.get("drift_state") or ""), 9)
    return (priority, str(row.get("lane_id") or row.get("signature") or ""))


def _is_paper_runtime(row: Mapping[str, Any]) -> bool:
    mode = str(row.get("mode") or "").lower()
    lane = str(row.get("lane_id") or "").lower()
    if "paper" in mode or "_paper" in lane or lane.startswith("papertrial_"):
        return True
    funnel = row.get("funnel") if isinstance(row.get("funnel"), Mapping) else {}
    return int(funnel.get("paper_order_intents") or 0) > 0


def _is_activation_running(row: Mapping[str, Any]) -> bool:
    return str(row.get("activation_state") or "") in {
        "PAPER_RUNNING",
        "PAPER_ONLINE_WAITING",
    }


def _has_recent_runtime_evidence(
    row: Mapping[str, Any],
    *,
    config: PaperRosterDriftConfig,
    now: datetime,
) -> bool:
    state = str(row.get("state") or row.get("activation_state") or "").upper()
    lifecycle = (
        row.get("trade_lifecycle")
        if isinstance(row.get("trade_lifecycle"), Mapping)
        else {}
    )
    if state == "STALE" or str(lifecycle.get("stage") or "").upper() == "STALE":
        return False

    age = _optional_float(row.get("age_seconds"))
    stale_after = _optional_float(row.get("stale_after_seconds"))
    if age is not None:
        limit = (
            stale_after
            if stale_after is not None and stale_after > 0
            else config.max_runtime_age_seconds
        )
        return age <= limit

    last_ts = _latest_runtime_ts(row)
    if last_ts is None:
        return False
    age_seconds = (now - last_ts).total_seconds()
    return age_seconds <= config.max_runtime_age_seconds


def _latest_runtime_ts(row: Mapping[str, Any]) -> datetime | None:
    candidates: list[Any] = [
        row.get("latest_heartbeat"),
        row.get("latest_eval_ts"),
        row.get("latest_ts"),
    ]
    runtime = row.get("runtime") if isinstance(row.get("runtime"), Mapping) else {}
    journal = runtime.get("journal") if isinstance(runtime.get("journal"), Mapping) else {}
    evidence = row.get("evidence") if isinstance(row.get("evidence"), Mapping) else {}
    paper_journal = (
        evidence.get("paper_journal")
        if isinstance(evidence.get("paper_journal"), Mapping)
        else {}
    )
    candidates.extend([
        journal.get("last_ts"),
        paper_journal.get("last_ts"),
        paper_journal.get("latest_ts"),
    ])
    for value in candidates:
        parsed = _parse_dt(value)
        if parsed is not None:
            return parsed
    return None


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _signature(strategy_id: str, exchange: str, symbol: str, timeframe: str) -> str:
    parts = [
        strategy_id.strip().lower(),
        exchange.strip().lower(),
        _norm_symbol(symbol),
        timeframe.strip().lower(),
    ]
    return "|".join(parts) if any(parts) else ""


def _norm_symbol(symbol: str) -> str:
    return str(symbol or "").split(":", 1)[0].strip().lower()


def _read_json_payload(path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback
    return payload if isinstance(payload, dict) else fallback


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--governor", type=Path, default=DEFAULT_GOVERNOR)
    parser.add_argument("--scanner", type=Path, default=DEFAULT_SCANNER)
    parser.add_argument("--activation", type=Path, default=DEFAULT_ACTIVATION)
    parser.add_argument("--shadow-manifest", type=Path, default=DEFAULT_SHADOW_MANIFEST)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--feed", type=Path, default=DEFAULT_FEED)
    parser.add_argument("--interval-seconds", type=_positive_float, default=60.0)
    parser.add_argument("--max-rows", type=_positive_int, default=180)
    parser.add_argument(
        "--max-runtime-age-seconds",
        type=_positive_float,
        default=PaperRosterDriftConfig.max_runtime_age_seconds,
        help=(
            "Maximum age for scanner/activation evidence to count as active "
            "paper runtime; older rows are kept as historical evidence."
        ),
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--print", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = PaperRosterDriftConfig(
        max_rows=args.max_rows,
        max_runtime_age_seconds=args.max_runtime_age_seconds,
    )
    while True:
        payload = build_paper_roster_drift(
            governor_path=args.governor,
            scanner_path=args.scanner,
            activation_path=args.activation,
            shadow_manifest_path=args.shadow_manifest,
            config=config,
        )
        publish_paper_roster_drift(payload, args.out, args.feed)
        if args.print:
            print(render_report(payload))
        if args.once:
            return 0
        time.sleep(args.interval_seconds)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
