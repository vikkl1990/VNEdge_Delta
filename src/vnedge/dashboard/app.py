"""Read-only dashboard server (docs/DESIGN.md §6).

Hard invariants, enforced structurally:
- No token, no dashboard: `create_app` refuses to start without at least one
  authorized user (legacy shared token or per-user store — see auth.py and
  docs/DASHBOARD_AUTH.md).
- Zero control actions: the only routes are the static page, GET /state,
  and the snapshot WebSocket. There is nothing to POST to.
- Cannot slow the bot: the server only reads whatever snapshot the bot last
  published; a dead or slow browser drops its own socket and nothing else.
"""

from __future__ import annotations

import asyncio
import csv
import html
import io
import json
import logging
import math
import os
import re
import shutil
import socket
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

from vnedge.agent_gateway.app import (
    AgentGatewayArtifacts,
    env_agent_audit_path,
    env_agent_jobs_dir,
    mount_agent_gateway,
)
from vnedge.agent_gateway.audit import AgentAuditLogger
from vnedge.agent_gateway.auth import AgentTokenStore
from vnedge.agent_gateway.jobs import (
    BLOCKED_STATUS,
    DONE_STATUS,
    FAILED_STATUS,
    PENDING_STATUS,
    RUNNING_STATUS,
    TERMINAL_STATUSES,
    list_jobs,
)
from vnedge.agent_gateway.task_registry import (
    QuantOSAgentGateway,
    env_quant_os_agent_gateway_dir,
    quant_os_event_stream,
)
from vnedge.dashboard.auth import (
    AuthResult,
    DashboardUser,
    TokenStore,
    has_permission,
    permissions_for,
)
from vnedge.dashboard.scanner_bridge import dashboard_scanner_payload
from vnedge.dashboard.session import SessionIssuer
from vnedge.dashboard.session_regime import build_session_regime
from vnedge.dashboard.sse_health import health_event_generator
from vnedge.dashboard.trade_journal import build_trade_journal
from vnedge.research.external_repo_synthesis import build_external_repo_synthesis
from vnedge.research.pine_script_research import load_pine_research_payload
from vnedge.research.quantified_blueprint_proof import (
    load_quantified_blueprint_proof_payload,
)
from vnedge.research.quantified_port_factory import load_quantified_port_factory_payload
from vnedge.research.quantified_proof_result_arbiter import (
    load_quantified_proof_result_arbiter_payload,
)
from vnedge.research.quantified_pullback_reversion_proof import (
    load_quantified_pullback_reversion_proof_payload,
)
from vnedge.research.quantified_strategy_lab import load_quantified_strategy_lab_payload

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"
_REPO_ROOT = Path(__file__).resolve().parents[3]
_APP_START = time.time()


def _build_sha() -> str:
    for p in (Path("/app/BUILD_SHA"), _REPO_ROOT / "BUILD_SHA"):
        try:
            sha = p.read_text().strip()
            if sha:
                return sha
        except OSError:
            continue
    return "dev"


# --- incident timeline --------------------------------------------------------
# Journal kinds that are operator incidents (not routine order flow), mapped to
# a severity and a runbook anchor in docs/RUNBOOKS.md.
_INCIDENT_JOURNAL_KINDS: dict[str, tuple[str, str]] = {
    "reconciliation_fail_closed": ("critical", "reconciliation-fail-closed"),
    "orphaned_paper_position": ("warning", "orphaned-paper-position"),
    "plan_restore_rejected": ("warning", "plan-restore-rejected"),
    "emergency_flatten_started": ("critical", "kill-switch-and-flatten"),
    "emergency_flatten_finished": ("info", "kill-switch-and-flatten"),
}

# Alert rule_ids -> runbook anchors. Anything unmapped gets general triage.
_ALERT_RUNBOOKS: dict[str, str] = {
    "feed_stale": "feed-stale",
    "kill_switch": "kill-switch-and-flatten",
    "journal_unhealthy": "journal-unavailable",
    "risk_status": "risk-status-degraded",
    "daily_loss": "daily-loss-stop",
    "loss_streak": "loss-streak",
    "drawdown": "drawdown",
}
_GENERAL_RUNBOOK = "general-triage"

# Alert rule_ids that are trade notifications, not incidents.
_NON_INCIDENT_ALERTS = frozenset({"new_fill"})

_LANE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

# Human-facing venue names for the fee/PnL calculator (keys are the registry's
# canonical exchange ids).
_EXCHANGE_LABELS: dict[str, str] = {
    "binanceusdm": "Binance USDⓈ-M",
    "bybit": "Bybit V5",
    "delta_india": "Delta India",
}


def _safe_float(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(parsed):
        return None
    return parsed


def _read_json_dict(path: Path | None) -> dict[str, object]:
    """Read one research artifact without ever turning a bad file into truth."""

    if path is None or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _file_age_seconds(path: Path | None) -> float | None:
    if path is None or not path.is_file():
        return None
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return None


def _iso_age_seconds(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return max(0.0, (datetime.now(UTC) - moment.astimezone(UTC)).total_seconds())


def event_research_infrastructure_payload(
    *,
    event_root: Path,
    event_trigger_telemetry_path: Path,
    absorption_dashboard_path: Path,
    event_replay_dir: Path,
    replay_determinism_proof_path: Path | None = None,
    event_continuity_path: Path | None = None,
    kronos_matrix_path: Path | None = None,
    kronos_confirmation_path: Path | None = None,
    forced_flow_dir: Path | None = None,
    tv_rule_adapter_path: Path | None = None,
    htf_structure_path: Path | None = None,
    htf_structure_v2_path: Path | None = None,
    failed_auction_readiness_path: Path | None = None,
) -> dict[str, object]:
    """Truthful, read-only inventory of the new event-research stack.

    Presence of code is deliberately separated from runtime evidence. A missing
    artifact means *implemented but not running*, never "healthy" or "live".
    """

    shards = sorted(event_root.rglob("*.jsonl.gz")) if event_root.is_dir() else []
    shards += sorted(event_root.rglob("*.jsonl.zst")) if event_root.is_dir() else []
    partials = sorted(event_root.rglob("*.partial")) if event_root.is_dir() else []
    manifests = sorted(event_root.rglob("*.manifest.json")) if event_root.is_dir() else []
    recorder_runtime = _read_json_dict(event_root / "_recorder_status.json")
    recorder_runtime_state = str(recorder_runtime.get("state") or "UNKNOWN").upper()
    recorder_status_age_seconds: float | None = None
    updated_at = recorder_runtime.get("updated_at")
    if isinstance(updated_at, str):
        try:
            updated = datetime.fromisoformat(updated_at)
            recorder_status_age_seconds = max(
                0.0,
                (datetime.now(UTC) - updated.astimezone(UTC)).total_seconds(),
            )
        except ValueError:
            recorder_status_age_seconds = None
    stats_seconds = _safe_float(recorder_runtime.get("stats_seconds")) or 30.0
    recorder_is_fresh = (
        recorder_status_age_seconds is not None
        and recorder_status_age_seconds <= max(90.0, stats_seconds * 3.0)
    )
    recorder_is_running = recorder_runtime_state == "RECORDING" and recorder_is_fresh
    active_session_id = str(recorder_runtime.get("session_id") or "")
    active_partials = [
        path for path in partials if recorder_is_running and active_session_id in path.name
    ]
    orphan_partials = [path for path in partials if path not in active_partials]
    recorded_events = 0
    unreadable_manifests = 0
    for path in manifests:
        manifest = _read_json_dict(path)
        records = manifest.get("records")
        if isinstance(records, int) and not isinstance(records, bool) and records >= 0:
            recorded_events += records
        else:
            unreadable_manifests += 1

    if recorder_is_running:
        recorder_status = "RECORDING"
    elif recorder_runtime_state == "RECORDING" and not recorder_is_fresh:
        recorder_status = "STALE_RECORDER_STATUS"
    elif not shards:
        recorder_status = "WAITING_FOR_TAPE"
    elif partials or unreadable_manifests or len(manifests) != len(shards):
        recorder_status = "ATTENTION"
    else:
        recorder_status = "RECORDED"

    trigger = _read_json_dict(event_trigger_telemetry_path)
    trigger_age_seconds = _file_age_seconds(event_trigger_telemetry_path)
    trigger_counts = trigger.get("counts") if isinstance(trigger.get("counts"), dict) else {}
    trigger_status = "OBSERVING" if trigger else "IMPLEMENTED_NOT_RUNNING"
    trigger_funnel = trigger.get("funnel") if isinstance(trigger.get("funnel"), dict) else {}
    trigger_rejections = (
        trigger.get("rejection_reasons")
        if isinstance(trigger.get("rejection_reasons"), dict)
        else {}
    )
    counterfactual = (
        trigger.get("counterfactual_absorption")
        if isinstance(trigger.get("counterfactual_absorption"), dict)
        else {}
    )

    absorption = _read_json_dict(absorption_dashboard_path)
    absorption_timeline = (
        absorption.get("timeline") if isinstance(absorption.get("timeline"), list) else []
    )
    absorption_status = (
        str(absorption.get("status") or "OBSERVING").upper()
        if absorption
        else "AWAITING_EVENT_TAPE"
    )

    result_files = (
        sorted(
            event_replay_dir.rglob("*.result.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if event_replay_dir.is_dir()
        else []
    )
    replay = _read_json_dict(result_files[0]) if result_files else {}
    determinism_proof = _read_json_dict(replay_determinism_proof_path)
    proof_config = (
        determinism_proof.get("config") if isinstance(determinism_proof.get("config"), dict) else {}
    )
    determinism_passed = (
        determinism_proof.get("passed") is True
        and determinism_proof.get("hash_match") is True
        and determinism_proof.get("events_match") is True
        and determinism_proof.get("feature_snapshots_match") is True
        and determinism_proof.get("validation_match") is True
        and determinism_proof.get("first_hash") == determinism_proof.get("second_hash")
        and determinism_proof.get("first_validation_hash")
        == determinism_proof.get("second_validation_hash")
        and determinism_proof.get("first_events") == determinism_proof.get("second_events")
        and determinism_proof.get("first_feature_snapshots")
        == determinism_proof.get("second_feature_snapshots")
        and determinism_proof.get("code_version") == proof_config.get("code_version")
        and int(determinism_proof.get("first_events") or 0) > 0
        and int(determinism_proof.get("first_feature_snapshots") or 0) > 0
    )
    continuity = _read_json_dict(event_continuity_path)
    continuity_qualification = (
        continuity.get("qualification") if isinstance(continuity.get("qualification"), dict) else {}
    )
    continuity_epochs = (
        continuity.get("epochs") if isinstance(continuity.get("epochs"), dict) else {}
    )
    continuity_audit = continuity.get("audit") if isinstance(continuity.get("audit"), dict) else {}
    validation = replay.get("validation") if isinstance(replay.get("validation"), dict) else {}
    replay_status = (
        "DETERMINISM_PROVEN"
        if replay and validation.get("passed") is True and determinism_passed
        else "VALIDATED_RESULT"
        if replay and validation.get("passed") is True
        else "ATTENTION"
        if replay
        else "RECORDED_NOT_VALIDATED"
        if shards
        else "WAITING_FOR_TAPE"
    )

    kronos_matrix = _read_json_dict(kronos_matrix_path)
    kronos_confirmation = _read_json_dict(kronos_confirmation_path)
    kronos_completion = (
        kronos_matrix.get("completion") if isinstance(kronos_matrix.get("completion"), dict) else {}
    )
    confirmation_completion = (
        kronos_confirmation.get("completion")
        if isinstance(kronos_confirmation.get("completion"), dict)
        else {}
    )
    eligible = kronos_confirmation.get("eligible_selection_candidates")
    eligible_count = len(eligible) if isinstance(eligible, list) else 0
    forced_flow_manifests = (
        sorted(forced_flow_dir.glob("*_manifest.json"))
        if forced_flow_dir is not None and forced_flow_dir.is_dir()
        else []
    )
    forced_flow_symbols: list[str] = []
    forced_flow_quality_passed = 0
    for path in forced_flow_manifests:
        panel = _read_json_dict(path)
        if panel.get("symbol"):
            forced_flow_symbols.append(str(panel["symbol"]))
        quality = panel.get("quality") if isinstance(panel.get("quality"), dict) else {}
        if quality.get("passed") is True:
            forced_flow_quality_passed += 1
    tv_rule_adapter = _read_json_dict(tv_rule_adapter_path)
    htf_structure = _read_json_dict(htf_structure_path)
    htf_selection = (
        htf_structure.get("selection") if isinstance(htf_structure.get("selection"), dict) else {}
    )
    htf_metrics = (
        htf_selection.get("metrics") if isinstance(htf_selection.get("metrics"), dict) else {}
    )
    htf_gate = htf_selection.get("gate") if isinstance(htf_selection.get("gate"), dict) else {}
    htf_structure_v2 = _read_json_dict(htf_structure_v2_path)
    htf_v2_selection = (
        htf_structure_v2.get("selection")
        if isinstance(htf_structure_v2.get("selection"), dict)
        else {}
    )
    htf_v2_metrics = (
        htf_v2_selection.get("metrics") if isinstance(htf_v2_selection.get("metrics"), dict) else {}
    )
    htf_v2_gate = (
        htf_v2_selection.get("gate") if isinstance(htf_v2_selection.get("gate"), dict) else {}
    )

    readiness = _read_json_dict(failed_auction_readiness_path)
    readiness_coverage = (
        readiness.get("coverage") if isinstance(readiness.get("coverage"), dict) else {}
    )
    readiness_tree = (
        readiness.get("tree_verification")
        if isinstance(readiness.get("tree_verification"), dict)
        else {}
    )
    readiness_semantic = (
        readiness.get("semantic_validation")
        if isinstance(readiness.get("semantic_validation"), dict)
        else {}
    )
    readiness_blockers = (
        readiness.get("blockers") if isinstance(readiness.get("blockers"), list) else []
    )
    continuity_blockers = (
        continuity_qualification.get("blockers")
        if isinstance(continuity_qualification.get("blockers"), list)
        else []
    )
    effective_readiness_blockers = continuity_blockers if continuity else readiness_blockers
    continuity_validation = (
        continuity_qualification.get("semantic_validation")
        if isinstance(continuity_qualification.get("semantic_validation"), dict)
        else {}
    )
    effective_data_ready = (
        continuity_qualification.get("data_ready") is True
        if continuity
        else readiness.get("data_ready") is True
    )

    connection = (
        recorder_runtime.get("connection")
        if isinstance(recorder_runtime.get("connection"), dict)
        else {}
    )
    session_elapsed_seconds = _iso_age_seconds(recorder_runtime.get("started_at"))
    connected_seconds = _iso_age_seconds(connection.get("connected_since"))
    reconnects = int(connection.get("reconnects") or 0)
    reconnect_rate_per_hour = (
        reconnects / max(session_elapsed_seconds / 3600.0, 1 / 60)
        if session_elapsed_seconds is not None
        else None
    )
    delay_by_channel = (
        recorder_runtime.get("feed_delay_by_channel")
        if isinstance(recorder_runtime.get("feed_delay_by_channel"), dict)
        else {}
    )
    critical_latency: dict[str, object] = {}
    latency_attention = False
    for channel in ("ob_updates", "trades"):
        metrics = delay_by_channel.get(channel)
        if not isinstance(metrics, dict):
            critical_latency[channel] = {"status": "UNAVAILABLE"}
            latency_attention = True
            continue
        p95_us = _safe_float(metrics.get("p95_us"))
        p99_us = _safe_float(metrics.get("p99_us"))
        status = "HEALTHY" if p95_us is not None and p95_us <= 500_000 else "ATTENTION"
        latency_attention = latency_attention or status != "HEALTHY"
        critical_latency[channel] = {
            "status": status,
            "p50_us": _safe_float(metrics.get("p50_us")),
            "p95_us": p95_us,
            "p99_us": p99_us,
            "negative_samples": int(metrics.get("negative_samples") or 0),
            "sample_count": int(metrics.get("count") or 0),
            "p95_sla_us": 500_000,
        }

    try:
        storage_root = event_root if event_root.exists() else event_root.parent
        disk = shutil.disk_usage(storage_root)
        stored_bytes = sum(path.stat().st_size for path in (*shards, *partials) if path.is_file())
        bytes_per_day = (
            stored_bytes / max(session_elapsed_seconds / 86_400.0, 1 / 24)
            if session_elapsed_seconds is not None and stored_bytes > 0
            else None
        )
        retention_days = disk.free / bytes_per_day if bytes_per_day else None
        storage = {
            "status": "HEALTHY" if disk.free / disk.total >= 0.10 else "ATTENTION",
            "stored_bytes": stored_bytes,
            "disk_free_bytes": disk.free,
            "disk_total_bytes": disk.total,
            "disk_free_pct": disk.free / disk.total * 100.0,
            "estimated_days_remaining": retention_days,
            "writer_queue_depth": recorder_runtime.get("writer_queue_depth"),
            "writer_queue_high_water": recorder_runtime.get("writer_queue_high_water"),
            "note": "Retention estimate uses this recorder session's observed byte rate.",
        }
    except OSError:
        storage = {"status": "UNAVAILABLE"}

    gap_guard = (
        recorder_runtime.get("gap_guard")
        if isinstance(recorder_runtime.get("gap_guard"), dict)
        else {}
    )
    incident_reasons: list[str] = []
    if not recorder_is_running or connection.get("connected") is not True:
        incident_reasons.append("event recorder is not freshly connected")
    if gap_guard.get("healthy") is False:
        incident_reasons.append("gap guard reports an integrity fault")
    if orphan_partials:
        incident_reasons.append(f"{len(orphan_partials)} orphan partial shard(s)")
    if latency_attention:
        incident_reasons.append("critical event channel exceeds latency SLA or is unavailable")
    if storage.get("status") == "ATTENTION":
        incident_reasons.append("event storage has less than 10% free space")
    incident_status = "ATTENTION" if incident_reasons else "CLEAR"

    counter_open = int(counterfactual.get("open") or 0)
    counter_pending = int(counterfactual.get("pending") or 0)
    counter_outcomes = int(trigger_funnel.get("counterfactual_outcomes") or 0)
    observation_state = {
        "tradeable": {
            "active": 0,
            "pending": 0,
            "status": "DISABLED",
            "reason": "No primary scanner is enabled and no order route exists.",
        },
        "event_research": {
            "active": counter_open,
            "pending": counter_pending,
            "resolved": counter_outcomes,
            "status": "COUNTERFACTUAL_ONLY" if trigger else "NOT_RUNNING",
            "profit_factor": _safe_float(counterfactual.get("profit_factor")),
            "average_net_ticks": _safe_float(counterfactual.get("average_net_ticks")),
        },
    }

    readiness_target_events = 5_000_000
    readiness_target_days = 14.0
    failed_auction = {
        "implementation": "continuity_qualification_active",
        "artifact_present": bool(continuity or readiness),
        "contract_id": (
            continuity.get("contract_id")
            if continuity
            else readiness.get("contract_id")
            if readiness
            else None
        ),
        "data_ready": effective_data_ready,
        "scanner_implementation_authorized": False,
        "selection_authorized": False,
        "events": int(readiness_coverage.get("total_events") or 0),
        "target_events": readiness_target_events,
        "requested_days": _safe_float(readiness_coverage.get("requested_days")) or 0.0,
        "target_days": readiness_target_days,
        "blocker_count": len(effective_readiness_blockers),
        "blockers": effective_readiness_blockers,
        "tree_passed": (
            not continuity_audit.get("failed_shards")
            and int(continuity_audit.get("orphan_partial_files") or 0) == 0
            if continuity
            else readiness_tree.get("passed") is True
        ),
        "semantic_passed": (
            continuity_validation.get("passed") is True
            if continuity
            else readiness_semantic.get("passed") is True
        ),
        "replay_determinism_passed": determinism_passed,
        "replay_determinism_events": int(determinism_proof.get("first_events") or 0),
        "replay_feature_snapshots": int(determinism_proof.get("first_feature_snapshots") or 0),
        "continuity": {
            "artifact_present": bool(continuity),
            "qualified_events": int(continuity_qualification.get("qualified_events") or 0),
            "target_events": int(
                continuity_qualification.get("target_events") or readiness_target_events
            ),
            "qualified_days": _safe_float(continuity_qualification.get("qualified_days")) or 0.0,
            "target_days": _safe_float(continuity_qualification.get("target_days"))
            or readiness_target_days,
            "events_remaining": int(
                continuity_qualification.get("events_remaining") or readiness_target_events
            ),
            "days_remaining": _safe_float(continuity_qualification.get("days_remaining"))
            or readiness_target_days,
            "estimated_ready_at": continuity_qualification.get("estimated_ready_at"),
            "estimate_status": continuity_qualification.get("estimate_status"),
            "epoch_count": int(continuity_epochs.get("count") or 0),
            "latest_epoch": continuity_epochs.get("latest"),
            "longest_epoch": continuity_epochs.get("longest"),
            "last_reset": continuity_epochs.get("last_reset"),
            "verified_shards": int(continuity_audit.get("verified_shards") or 0),
            "failed_shards": len(continuity_audit.get("failed_shards") or []),
            "active_partial_files": int(continuity_audit.get("active_partial_files") or 0),
            "orphan_partial_files": int(continuity_audit.get("orphan_partial_files") or 0),
            "can_trade": False,
            "can_promote": False,
        },
        "raw_absorption_observations": int(trigger_funnel.get("absorption_observations") or 0),
        "counterfactual_outcomes": counter_outcomes,
        "stages": [
            {
                "id": "record",
                "label": "Record integrity epoch",
                "value": int(continuity_qualification.get("qualified_events") or 0),
                "target": readiness_target_events,
                "state": "IN_PROGRESS" if recorder_is_running else "BLOCKED",
            },
            {
                "id": "validate",
                "label": "Validate causal tape",
                "value": len(effective_readiness_blockers),
                "target": 0,
                "state": "PASSED" if effective_data_ready else "BLOCKED",
            },
            {
                "id": "determinism",
                "label": "Prove deterministic replay",
                "value": int(determinism_proof.get("first_feature_snapshots") or 0),
                "target": 1,
                "state": "PASSED" if determinism_passed else "BLOCKED",
            },
            {
                "id": "implement",
                "label": "Implement failed-auction scanner",
                "value": 0,
                "target": 1,
                "state": "NOT_AUTHORIZED",
            },
            {
                "id": "selection",
                "label": "Run chronological selection",
                "value": 0,
                "target": 1,
                "state": "NOT_REACHED",
            },
            {
                "id": "holdout",
                "label": "Open sealed holdout once",
                "value": 0,
                "target": 1,
                "state": "SEALED",
            },
            {
                "id": "paper",
                "label": "Paper eligibility proof",
                "value": 0,
                "target": 1,
                "state": "LOCKED",
            },
        ],
        "can_trade": False,
        "can_promote": False,
    }

    return {
        "schema_version": "vnedge.event_research_infrastructure.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "recorder": {
            "implementation": "available",
            "status": recorder_status,
            "event_root": str(event_root),
            "finalized_shards": len(shards),
            "manifest_files": len(manifests),
            "partial_files": len(partials),
            "active_partial_files": len(active_partials),
            "orphan_partial_files": len(orphan_partials),
            "recorded_events_from_manifests": recorded_events,
            "live_events": int(recorder_runtime.get("events") or 0),
            "active_session": (recorder_runtime.get("session_id") if recorder_is_running else None),
            "runtime_state": recorder_runtime_state,
            "runtime_status_age_seconds": recorder_status_age_seconds,
            "runtime": recorder_runtime,
            "unreadable_manifests": unreadable_manifests,
            "integrity_note": "inventory only; full sequence/checksum validation runs before replay",
            "connection": recorder_runtime.get("connection")
            or {
                "connected": None,
                "attempts": int(recorder_runtime.get("connections") or 0),
                "reconnects": None,
                "disconnects": None,
            },
            "feed_delay_by_channel": recorder_runtime.get("feed_delay_by_channel") or {},
            "feed_delay_corrected_by_channel": (
                recorder_runtime.get("feed_delay_corrected_by_channel") or {}
            ),
            "timestamp_quality": recorder_runtime.get("feed_timestamp_quality") or {},
            "operations": {
                "session_elapsed_seconds": session_elapsed_seconds,
                "current_connection_seconds": connected_seconds,
                "reconnect_rate_per_hour": reconnect_rate_per_hour,
                "last_disconnect": connection.get("last_disconnect"),
                "critical_latency": critical_latency,
                "trigger_telemetry_age_seconds": trigger_age_seconds,
            },
            "storage": storage,
        },
        "event_trigger": {
            "implementation": "available",
            "status": trigger_status,
            "telemetry_present": bool(trigger),
            "counts": trigger_counts,
            "feed_delay": trigger.get("feed_delay") if trigger else None,
            "receive_to_decision": trigger.get("receive_to_decision") if trigger else None,
            "funnel": trigger_funnel,
            "rejection_reasons": trigger_rejections,
            "market_states": trigger.get("market_states") or {},
            "counterfactual_absorption": counterfactual,
        },
        "absorption": {
            "implementation": "available",
            "status": absorption_status,
            "telemetry_present": bool(absorption),
            "symbol": absorption.get("symbol") if absorption else None,
            "strength_gauge": absorption.get("strength_gauge") if absorption else None,
            "observations": int(
                trigger_counts.get("absorption_observations") or len(absorption_timeline)
            ),
            "counterfactual_observations": int(
                trigger_counts.get("counterfactual_observations") or 0
            ),
            "counterfactual_outcomes": int(trigger_counts.get("counterfactual_outcomes") or 0),
            "latest": absorption.get("latest") if absorption else None,
            "liquidation_strength_applied_to_signal": False,
        },
        "observation_state": observation_state,
        "failed_auction_readiness": failed_auction,
        "operations": {
            "incident_status": incident_status,
            "incident_reasons": incident_reasons,
            "generated_at": datetime.now(UTC).isoformat(),
        },
        "replay": {
            "implementation": "available",
            "status": replay_status,
            "results_present": bool(replay),
            "latest_result": str(result_files[0]) if result_files else None,
            "events_processed": replay.get("events_processed") if replay else 0,
            "candidates_emitted": replay.get("candidates_emitted") if replay else 0,
            "deterministic_hash": replay.get("deterministic_hash") if replay else None,
            "validation": validation,
            "summary_metrics": replay.get("summary_metrics") if replay else {},
            "deterministic_hash_scope": (
                replay.get("summary_metrics", {}).get("deterministic_hash_scope")
                if isinstance(replay.get("summary_metrics"), dict)
                else None
            ),
            "determinism_proof": {
                **determinism_proof,
                "passed": determinism_passed,
                "can_trade": False,
                "can_promote": False,
            }
            if determinism_proof
            else {},
        },
        "research_modules": {
            "htf_structure_break": {
                "implementation": "available",
                "status": (
                    "SELECTION_PASS_TAIL_SEALED"
                    if htf_gate.get("passed") is True
                    else "SELECTION_REJECTED"
                    if htf_structure
                    else "READY_NO_ARTIFACT"
                ),
                "selection_trades": int(htf_metrics.get("trades") or 0),
                "selection_net_bps": float(htf_metrics.get("net_bps") or 0.0),
                "profit_factor": float(htf_metrics.get("profit_factor") or 0.0),
                "scanner_funnel": htf_metrics.get("scanner_funnel") or {},
                "untouched": htf_structure.get("untouched") or {},
                "can_trade": False,
                "can_promote": False,
            },
            "htf_structure_break_v2": {
                "implementation": "available",
                "status": (
                    "SELECTION_PASS_TAIL_SEALED"
                    if htf_v2_gate.get("passed") is True
                    else "SELECTION_REJECTED"
                    if htf_structure_v2
                    else "READY_NO_ARTIFACT"
                ),
                "selection_trades": int(htf_v2_metrics.get("trades") or 0),
                "selection_net_bps": float(htf_v2_metrics.get("net_bps") or 0.0),
                "average_gross_bps": float(htf_v2_metrics.get("average_gross_bps") or 0.0),
                "average_cost_bps": float(htf_v2_metrics.get("average_total_cost_bps") or 0.0),
                "average_net_bps": float(htf_v2_metrics.get("average_net_bps") or 0.0),
                "profit_factor": float(htf_v2_metrics.get("profit_factor") or 0.0),
                "markets": htf_v2_metrics.get("markets") or {},
                "untouched": htf_structure_v2.get("untouched") or {},
                "funding_used": htf_v2_metrics.get("funding_used") is True,
                "can_trade": False,
                "can_promote": False,
            },
            "kronos": {
                "implementation": "available",
                "status": "EXPLORATORY_ONLY" if kronos_matrix else "READY_NO_ARTIFACT",
                "matrix_present": bool(kronos_matrix),
                "matrix_complete": kronos_completion.get("complete") is True,
                "base_runs": kronos_completion.get("completed_base_runs", 0),
                "permutations": kronos_completion.get("scored_permutations", 0),
                "confirmation_base_runs": confirmation_completion.get("completed_base_runs", 0),
                "eligible_selection_candidates": eligible_count,
                "holdback_evaluated": False,
                "operator_answer": (
                    kronos_confirmation.get("operator_answer")
                    or kronos_matrix.get("operator_answer")
                    or "Kronos artifacts not published"
                ),
            },
            "forced_flow_panel": {
                "implementation": "available",
                "status": (
                    "PANEL_READY_PROXY_ONLY" if forced_flow_manifests else "READY_NO_ARTIFACT"
                ),
                "symbols": sorted(set(forced_flow_symbols)),
                "manifests": len(forced_flow_manifests),
                "quality_passed": forced_flow_quality_passed,
                "claims_actual_liquidations": False,
                "can_trade": False,
            },
            "governance": {
                "implementation": "available",
                "status": "SIGNED_PROOF_MIGRATION_PENDING",
                "sha256_proof_chain": "implemented",
                "ed25519_keyring_primitives": "implemented",
                "paper_manifest_signature_required": False,
                "note": "Ed25519 primitives are tested; current proof envelopes remain SHA-256 integrity proofs, not signed identity proofs.",
            },
            "delta_execution_safety": {
                "implementation": "available",
                "status": "IMPLEMENTED_NOT_CONNECTED",
                "deadman_interlock": "implemented",
                "server_side_protection": "implemented",
                "dashboard_runtime_connected": False,
                "note": "Execution safety code is isolated from this research dashboard and has no loaded credentials or route.",
            },
            "tv_rule_adapter": {
                "implementation": "available",
                "status": (
                    "RESEARCH_ARTIFACT_PRESENT"
                    if tv_rule_adapter
                    else "IMPLEMENTED_NO_PUBLISHED_ARTIFACT"
                ),
                "artifact_present": bool(tv_rule_adapter),
                "research_only": True,
                "can_trade": False,
                "note": "TradingView rules are normalized for causal research only and cannot create execution authority.",
            },
        },
        "safety": {
            "research_only": True,
            "can_trade": False,
            "can_promote": False,
            "order_route": "absent",
            "live_state_shared": False,
        },
        "operator_answer": (
            "The event recorder, incremental trigger, absorption detector, and deterministic "
            "replay are installed as research-only components. Runtime evidence is shown "
            "separately; missing tape or telemetry is not reported as live."
        ),
        "can_trade": False,
        "can_promote": False,
    }


def _agent_job_adapter(job: dict) -> str:
    request = job.get("request") if isinstance(job.get("request"), dict) else {}
    params = request.get("parameters") if isinstance(request.get("parameters"), dict) else {}
    strategy_id = str(request.get("strategy_id") or "")
    adapter = str(params.get("adapter") or params.get("job_adapter") or "")
    if strategy_id.startswith("ai_"):
        return "ai_candidate"
    if (
        "candidate_replay" in {strategy_id, adapter}
        or strategy_id == "candidate_replay_executor_v1"
    ):
        return "candidate_replay"
    return "registered_backtest"


def _agent_job_result_summary(job: dict) -> str:
    if job.get("blocked_reason"):
        return str(job["blocked_reason"])
    if job.get("error"):
        return str(job["error"])
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
    if metrics:
        net = _safe_float(metrics.get("net_profit_usd"))
        trades = int(_safe_float(metrics.get("num_trades")) or 0)
        if net is not None:
            return f"net {net:+.2f} USD / trades {trades}"
        return f"trades {trades}"
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    if summary:
        candidates = int(_safe_float(summary.get("replay_candidates")) or 0)
        fills = int(_safe_float(summary.get("fills")) or 0)
        rows = int(_safe_float(summary.get("rows")) or 0)
        return f"replay candidates {candidates} / fills {fills} / rows {rows}"
    matched = result.get("matched_candidate")
    if isinstance(matched, dict):
        verdict = str(matched.get("verdict") or "candidate")
        net = _safe_float(matched.get("oos_net_usd"))
        return f"{verdict} {net:+.2f} USD" if net is not None else verdict
    if job.get("status") == PENDING_STATUS:
        return "waiting for research runner"
    if job.get("status") == RUNNING_STATUS:
        return "running now"
    return "no terminal result yet"


def _agent_jobs_payload(
    jobs_dir: Path | None,
    *,
    limit: int,
    gateway_http_mounted: bool,
) -> dict:
    rows = list_jobs(jobs_dir, limit=limit) if jobs_dir is not None else []
    status_counts = Counter(str(job.get("status") or "UNKNOWN") for job in rows)
    pending = status_counts.get(PENDING_STATUS, 0)
    running = status_counts.get(RUNNING_STATUS, 0)
    done = status_counts.get(DONE_STATUS, 0)
    blocked = status_counts.get(BLOCKED_STATUS, 0)
    failed = status_counts.get(FAILED_STATUS, 0)
    recent: list[dict] = []
    for job in rows:
        request = job.get("request") if isinstance(job.get("request"), dict) else {}
        recent.append(
            {
                "job_id": job.get("job_id"),
                "status": job.get("status"),
                "adapter": _agent_job_adapter(job),
                "created_by": job.get("created_by"),
                "hypothesis_id": request.get("hypothesis_id"),
                "strategy_id": request.get("strategy_id"),
                "exchange": request.get("exchange"),
                "symbol": request.get("symbol"),
                "timeframe": request.get("timeframe"),
                "updated_at": job.get("updated_at") or job.get("created_at"),
                "result_summary": _agent_job_result_summary(job),
                "can_trade": False,
                "can_promote": False,
                "live_orders_enabled": False,
            }
        )
    return {
        "summary": {
            "total": len(rows),
            "pending": pending,
            "running": running,
            "done": done,
            "blocked": blocked,
            "failed": failed,
            "terminal": sum(status_counts.get(status, 0) for status in TERMINAL_STATUSES),
            "gateway_http_mounted": gateway_http_mounted,
        },
        "jobs": recent,
        "jobs_dir": str(jobs_dir) if jobs_dir is not None else None,
        "policy": "dashboard-read-only; agent jobs cannot trade or promote",
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
    }


def _tail_lines(path: Path, max_bytes: int = 512_000) -> list[str]:
    """Bounded tail read: journals grow unbounded; never load them whole."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]  # first line is almost certainly partial
    return [line for line in lines if line.strip()]


def _iter_jsonl(path: Path, max_bytes: int = 512_000):
    for line in _tail_lines(path, max_bytes=max_bytes):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            yield record


def _summarize_payload(payload: dict) -> str:
    return ", ".join(f"{key}={value}" for key, value in list(payload.items())[:6])


def _alert_incidents(paths: list[Path]) -> list[dict]:
    out: list[dict] = []
    for path in paths:
        if not path.exists():
            continue
        for record in _iter_jsonl(path):
            rule_id = str(record.get("rule_id", ""))
            if rule_id in _NON_INCIDENT_ALERTS:
                continue
            anchor = _ALERT_RUNBOOKS.get(rule_id, _GENERAL_RUNBOOK)
            out.append(
                {
                    "ts": str(record.get("ts", "")),
                    "severity": str(record.get("severity", "info")),
                    "source": f"alert:{rule_id or 'unknown'}",
                    "message": str(record.get("message", "")),
                    "runbook": f"/runbooks#{anchor}",
                }
            )
    return out


def _journal_incidents(journal_dir: Path | None) -> list[dict]:
    out: list[dict] = []
    if journal_dir is None or not journal_dir.is_dir():
        return out
    for path in sorted(journal_dir.glob("*.journal.jsonl")):
        lane = path.name.removesuffix(".journal.jsonl")
        for record in _iter_jsonl(path):
            kind = str(record.get("kind", ""))
            mapped = _INCIDENT_JOURNAL_KINDS.get(kind)
            if mapped is None:
                continue
            severity, anchor = mapped
            payload = record.get("payload")
            summary = _summarize_payload(payload) if isinstance(payload, dict) else ""
            out.append(
                {
                    "ts": str(record.get("ts", "")),
                    "severity": severity,
                    "source": f"journal:{lane}",
                    "message": kind + (f" — {summary}" if summary else ""),
                    "runbook": f"/runbooks#{anchor}",
                }
            )
    return out


def _snapshot_trade_log(snapshot: dict | None, lane: str) -> list[dict]:
    """The trade log lives in the coalesced snapshot (multi-lane snapshots
    carry a per-lane tail; the primary lane's session carries the full one)."""
    if not isinstance(snapshot, dict):
        return []
    if lane:
        for entry in snapshot.get("lanes") or []:
            if isinstance(entry, dict) and entry.get("lane_id") == lane:
                return [e for e in entry.get("trade_log") or [] if isinstance(e, dict)]
        if snapshot.get("lane_id") != lane:
            return []
    session = snapshot.get("session")
    log = session.get("trade_log") if isinstance(session, dict) else None
    return [e for e in log or [] if isinstance(e, dict)]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _render_runbooks_html(markdown: str) -> str:
    """Minimal, dependency-free markdown: headings become anchored <h1..h3>,
    everything else is escaped verbatim inside <pre> blocks."""
    parts: list[str] = [
        "<!doctype html><meta charset='utf-8'><title>VNEDGE runbooks</title>",
        (
            "<style>body{background:#05070a;color:#e8eef6;font:14px/1.55 ui-monospace,"
            "SFMono-Regular,Menlo,Consolas,monospace;max-width:860px;margin:24px auto;"
            "padding:0 16px}h1,h2,h3{color:#4cb7ff;scroll-margin-top:12px}"
            "h2{border-top:1px solid #263241;padding-top:18px}"
            "pre{white-space:pre-wrap;margin:4px 0}:target{color:#f7bd54}</style>"
        ),
    ]
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            parts.append("<pre>" + html.escape("\n".join(buffer)) + "</pre>")
            buffer.clear()

    for line in markdown.splitlines():
        heading = re.match(r"^(#{1,3})\s+(.*)$", line)
        if heading:
            flush()
            level = len(heading.group(1))
            title = heading.group(2).strip()
            parts.append(f"<h{level} id='{_slug(title)}'>{html.escape(title)}</h{level}>")
        else:
            buffer.append(line)
    flush()
    return "".join(parts)


def _cost_model_payload() -> dict:
    """The REAL round-trip cost model, read from the same constants the
    research and paper engines use — never hardcoded in the UI.

    Two honest cost models the operator must reconcile:
    - maker-first: maker entry + taker exit + slippage (the ~8 bps wall the
      scalper replay diagnostics use as breakeven).
    - taker round-trip: both legs taker + slippage (the ~11 bps wall).
    The paper broker's pessimistic fill model is reported alongside so the
    "8 vs 10 bps" disconnect is visible instead of buried in one number.
    """
    from vnedge.paper.fill_model import FillModel
    from vnedge.scalping.parameter_registry import (
        DEFAULT_SCALPER_PARAMETER_REGISTRY as _registry,
    )

    fee = _registry.fee_profile("binanceusdm")
    paper = FillModel()
    maker_first_rt = fee.maker_bps + fee.taker_bps + fee.slippage_bps
    taker_rt = 2 * fee.taker_bps + fee.slippage_bps
    paper_taker_rt = 2 * (paper.taker_fee_bps + paper.slippage_bps)

    # Every venue's real fee schedule, so the leverage/PnL calculator can model
    # each exchange from the SAME constants the research and paper engines use —
    # never a number hardcoded in the UI.
    exchanges = []
    for name, prof in sorted(_registry.exchange_fees.items()):
        exchanges.append(
            {
                "exchange": prof.exchange,
                "label": _EXCHANGE_LABELS.get(prof.exchange, prof.exchange),
                "maker_bps": prof.maker_bps,
                "taker_bps": prof.taker_bps,
                "slippage_bps": prof.slippage_bps,
                "safety_buffer_bps": prof.safety_buffer_bps,
                "maker_first_cost_bps": round(prof.maker_first_cost_bps, 2),
                "taker_round_trip_cost_bps": round(prof.taker_round_trip_cost_bps, 2),
            }
        )
    return {
        "exchange": fee.exchange,
        "source": "scalper_replay_diagnostics + paper.fill_model constants",
        "maker_bps": fee.maker_bps,
        "taker_bps": fee.taker_bps,
        "slippage_bps": fee.slippage_bps,
        "safety_buffer_bps": fee.safety_buffer_bps,
        # Two labelled round-trip cost models (no safety buffer — the raw wall).
        "maker_first_rt_bps": round(maker_first_rt, 2),
        "taker_rt_bps": round(taker_rt, 2),
        # With the research safety buffer applied (what the gates actually use).
        "maker_first_cost_bps": round(fee.maker_first_cost_bps, 2),
        "taker_round_trip_cost_bps": round(fee.taker_round_trip_cost_bps, 2),
        # Per-exchange schedules for the calculator (Binance / Bybit / Delta).
        "exchanges": exchanges,
        "paper_fill_model": {
            "taker_fee_bps": paper.taker_fee_bps,
            "slippage_bps": paper.slippage_bps,
            "taker_rt_bps": round(paper_taker_rt, 2),
        },
    }


class SnapshotProvider:
    """Holds the latest coalesced snapshot. The bot publishes; the UI reads.
    That is the entire coupling between them."""

    def __init__(self) -> None:
        self._latest: dict | None = None

    def publish(self, snapshot: dict) -> None:
        self._latest = snapshot

    def latest(self) -> dict | None:
        return self._latest


def create_app(
    provider: SnapshotProvider,
    token: str | None = None,
    snapshot_hz: float = 1.0,
    history_path: Path | None = None,
    research_path: Path | None = None,
    alpha_council_path: Path | None = None,
    alpha_workbench_path: Path | None = None,
    vibe_intelligence_path: Path | None = None,
    agentic_research_os_path: Path | None = None,
    alerts_path: Path | None = None,
    journal_dir: Path | None = None,
    runbooks_path: Path | None = None,
    lane_readiness_path: Path | None = None,
    promotion_review_runbook_path: Path | None = None,
    realtime_scanner_path: Path | None = None,
    delta_scalper_path: Path | None = None,
    delta_active_cost_evidence_path: Path | None = None,
    indicator_score_calibration_path: Path | None = None,
    revived_scanner_evidence_path: Path | None = None,
    revival_experiment_matrix_path: Path | None = None,
    scanner_forward_evidence_path: Path | None = None,
    lane_firing_causality_path: Path | None = None,
    paper_lane_activation_path: Path | None = None,
    paper_route_doctor_path: Path | None = None,
    paper_lane_cadence_path: Path | None = None,
    paper_lane_performance_path: Path | None = None,
    paper_trade_entry_autopsy_path: Path | None = None,
    paper_trade_exit_autopsy_path: Path | None = None,
    trade_analyzer_os_path: Path | None = None,
    paper_lane_root_cause_path: Path | None = None,
    maker_quote_lifecycle_path: Path | None = None,
    paper_trade_contract_reconciler_path: Path | None = None,
    paper_promotion_bridge_path: Path | None = None,
    lane_survival_path: Path | None = None,
    paper_lane_governor_path: Path | None = None,
    paper_roster_drift_path: Path | None = None,
    darwinian_agent_survival_path: Path | None = None,
    ml_pipeline_status_path: Path | None = None,
    pine_research_path: Path | None = None,
    quantified_strategy_lab_path: Path | None = None,
    quantified_port_factory_path: Path | None = None,
    quantified_blueprint_proof_path: Path | None = None,
    quantified_proof_arbiter_path: Path | None = None,
    quantified_pullback_proof_path: Path | None = None,
    pine_alpha_distiller_path: Path | None = None,
    tv_rule_spec_path: Path | None = None,
    backtest_progress_path: Path | None = None,
    pine_edge_uplift_path: Path | None = None,
    edge_uplift_executor_path: Path | None = None,
    scanner_backtest_uplift_path: Path | None = None,
    delta_5m_event_clock_path: Path | None = None,
    alpha_arena_lite_path: Path | None = None,
    quant_loop_governance_path: Path | None = None,
    evidence_index_path: Path | None = None,
    execution_replay_profile_path: Path | None = None,
    delta_event_root: Path | None = None,
    event_trigger_telemetry_path: Path | None = None,
    absorption_dashboard_path: Path | None = None,
    event_replay_dir: Path | None = None,
    replay_determinism_proof_path: Path | None = None,
    event_continuity_path: Path | None = None,
    kronos_matrix_path: Path | None = None,
    kronos_confirmation_path: Path | None = None,
    forced_flow_dir: Path | None = None,
    htf_structure_path: Path | None = None,
    htf_structure_v2_path: Path | None = None,
    failed_auction_readiness_path: Path | None = None,
    token_store: TokenStore | None = None,
    agent_token_store: AgentTokenStore | None = None,
    agent_audit_path: Path | None = None,
    agent_jobs_dir: Path | None = None,
    v2_dist_path: Path | None = None,
    session_issuer: SessionIssuer | None = None,
    quant_os_agent_gateway_dir: Path | None = None,
) -> FastAPI:
    """Build the read-only dashboard app.

    Auth accepts either a per-user ``token_store`` (DASHBOARD_USERS), the
    legacy shared ``token`` (DASHBOARD_TOKEN — becomes the ``operator``
    user with no expiry), or both. Zero users refuses to start.
    """
    users: list[DashboardUser] = list(token_store.users) if token_store is not None else []
    if token is not None and token.strip():
        users.append(DashboardUser(name="operator", token=token.strip(), role="operator"))
    if not users:
        raise ValueError(
            "DASHBOARD_TOKEN or DASHBOARD_USERS must supply at least one user "
            "— no token, no dashboard"
        )
    store = TokenStore(users)
    # Short-lived session tokens: present the root token once to POST /auth/session
    # to mint a JWT, then the root secret stops travelling on every request.
    issuer = session_issuer if session_issuer is not None else SessionIssuer.from_env()

    app = FastAPI(title="VNEDGE dashboard", docs_url=None, redoc_url=None)
    ws_connections: dict[str, int] = {}  # user name -> live socket count (never tokens)

    @app.middleware("http")
    async def dashboard_security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; connect-src 'self'; "
            "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'",
        )
        return response

    @app.get("/health")
    async def health() -> JSONResponse:
        """Unauthenticated liveness probe for container healthchecks + the TLS
        proxy. Returns 200 as soon as the app is serving; deliberately requires
        NO token and reveals no state — its only job is "is the process up".
        This is what lets compose gate dependents on `service_healthy` and stop
        the --force-recreate race that took the fleet down twice."""
        return JSONResponse({"status": "ok"})

    @app.get("/ready")
    async def ready() -> JSONResponse:
        """Unauthenticated READINESS probe — liveness says "process up", this
        says "up AND has data to serve". 200 once a snapshot has been published,
        503 while still warming. Reveals only that boolean, never any state, so
        it needs no token. Distinct from /health so an orchestrator can wait for
        data readiness before routing traffic without treating a warming
        process as dead."""
        if provider.latest() is None:
            return JSONResponse({"status": "starting"}, status_code=503)
        return JSONResponse({"status": "ready"})

    # Per-lane files (equity/fills/journals/alerts) live next to the primary
    # equity history unless a journal dir is given explicitly.
    lane_dir = journal_dir or (history_path.parent if history_path is not None else None)
    # Resolve the runbooks doc across both layouts: dev (repo checkout, where
    # _REPO_ROOT/docs works) and the container (pip-installed package, where
    # __file__ points into site-packages but docs/ is COPYed to the WORKDIR).
    runbooks_file = runbooks_path or next(
        (
            c
            for c in (_REPO_ROOT / "docs" / "RUNBOOKS.md", Path.cwd() / "docs" / "RUNBOOKS.md")
            if c.exists()
        ),
        _REPO_ROOT / "docs" / "RUNBOOKS.md",
    )

    agent_jobs_path = agent_jobs_dir or env_agent_jobs_dir()
    quant_os_gateway = QuantOSAgentGateway(
        quant_os_agent_gateway_dir or env_quant_os_agent_gateway_dir()
    )
    resolved_agent_store = (
        agent_token_store if agent_token_store is not None else AgentTokenStore.from_env()
    )
    agent_gateway_http_mounted = bool(len(resolved_agent_store))
    if len(resolved_agent_store):
        mount_agent_gateway(
            app,
            provider=provider,
            token_store=resolved_agent_store,
            audit_logger=AgentAuditLogger(agent_audit_path or env_agent_audit_path()),
            jobs_dir=agent_jobs_path,
            quant_os_gateway_dir=quant_os_gateway.root,
            artifacts=AgentGatewayArtifacts(
                research_path=research_path,
                alpha_council_path=alpha_council_path,
                alpha_workbench_path=alpha_workbench_path,
                vibe_intelligence_path=vibe_intelligence_path,
                lane_readiness_path=lane_readiness_path,
                realtime_scanner_path=realtime_scanner_path,
            ),
        )

    def _authorized(request: Request) -> AuthResult:
        """Authenticate the request; raise 401 (with the store's reason —
        e.g. expiry) on failure. Never returns an unauthorized result."""
        header = request.headers.get("authorization", "")
        candidate = header.removeprefix("Bearer ").strip()
        if not candidate:
            candidate = request.query_params.get("token", "")
        if not candidate:
            candidate = request.cookies.get("vnedge_session", "")
        # A short-lived session JWT is honored first; anything that isn't one of
        # ours (verify -> None) falls through to the long-lived token store, so
        # existing tokens keep working unchanged.
        session = issuer.verify(candidate)
        if session is not None:
            if not session.authorized:
                raise HTTPException(status_code=401, detail=session.reason or "invalid session")
            return session
        result = store.authenticate(candidate)
        if not result.authorized:
            raise HTTPException(status_code=401, detail=result.reason or "missing or invalid token")
        return result

    def _identity(user: AuthResult) -> dict[str, str]:
        # Role travels back with every authenticated response so a future
        # frontend can hide controls the caller can't use (defense in depth —
        # the server still enforces via _require_permission on control routes).
        return {"X-Dashboard-User": user.name or "", "X-Dashboard-Role": user.role or ""}

    def _require_permission(permission: str):
        """Dependency factory: authenticate, then 403 unless the caller's role
        grants ``permission``. Read routes need no gate today (every role has
        ``view``); this is the primitive that control routes (live-gate flip,
        promotion, kill-switch) attach to when they land — no second auth
        migration. Enforcement is server-side and cannot be spoofed by a header.
        """

        def _dep(request: Request) -> AuthResult:
            user = _authorized(request)
            if not has_permission(user.role, permission):
                raise HTTPException(
                    status_code=403,
                    detail=f"role {user.role!r} lacks permission {permission!r}",
                )
            return user

        return _dep

    def _read_json_payload(path: Path | None, fallback: dict) -> dict:
        if path is None or not path.exists():
            return fallback
        try:
            payload = json.loads(path.read_text())
        except json.JSONDecodeError:
            return fallback  # mid-write race: serve a safe empty payload
        return payload if isinstance(payload, dict) else fallback

    pine_alpha_distiller_file = pine_alpha_distiller_path or Path(
        "research/live_research/pine_alpha_distiller_latest.json"
    )
    tv_rule_spec_file = tv_rule_spec_path or Path(
        "research/live_research/tv_rule_adapter_latest.json"
    )
    quantified_strategy_lab_file = quantified_strategy_lab_path or Path(
        "research/live_research/quantified_strategy_lab_latest.json"
    )
    quantified_port_factory_file = quantified_port_factory_path or Path(
        "research/live_research/quantified_port_factory_latest.json"
    )
    quantified_blueprint_proof_file = quantified_blueprint_proof_path or Path(
        "research/live_research/quantified_blueprint_proof_latest.json"
    )
    quantified_proof_arbiter_file = quantified_proof_arbiter_path or Path(
        "research/live_research/quantified_proof_result_arbiter_latest.json"
    )
    quantified_pullback_proof_file = quantified_pullback_proof_path or Path(
        "research/live_research/quantified_pullback_reversion_proof_latest.json"
    )
    pine_backtest_progress_file = backtest_progress_path or Path(
        "research/live_research/scanner_tournament_progress.json"
    )
    pine_edge_uplift_file = pine_edge_uplift_path or Path(
        "research/live_research/pine_edge_uplift_agent_latest.json"
    )
    edge_uplift_executor_file = edge_uplift_executor_path or Path(
        "research/live_research/edge_uplift_experiments_latest.json"
    )
    scanner_backtest_uplift_file = scanner_backtest_uplift_path or Path(
        "research/live_research/scanner_backtest_uplift_latest.json"
    )
    delta_5m_event_clock_file = delta_5m_event_clock_path or Path(
        "research/live_research/delta_5m_event_clock_latest.json"
    )
    lane_firing_causality_file = lane_firing_causality_path or Path(
        "research/live_research/lane_firing_causality_latest.json"
    )
    alpha_arena_lite_file = alpha_arena_lite_path or Path(
        "research/live_research/alpha_arena_lite_latest.json"
    )
    quant_loop_governance_file = quant_loop_governance_path or Path(
        "research/live_research/quant_loop_governance_latest.json"
    )
    agentic_research_os_file = agentic_research_os_path or Path(
        "research/live_research/agentic_research_os_latest.json"
    )
    scanner_forward_evidence_file = scanner_forward_evidence_path or Path(
        "research/live_research/mtf_amf_forward_evidence_latest.json"
    )
    fee_wall_forensics_file = Path("research/live_research/fee_wall_forensics_latest.json")
    fee_wall_probes_file = Path("research/live_research/fee_wall_paper_probes.json")
    fee_wall_probe_actuals_file = Path("research/live_research/fee_wall_probe_actuals_latest.json")
    evidence_index_file = evidence_index_path or Path(
        "research/live_research/evidence_index_latest.json"
    )
    execution_replay_profile_file = execution_replay_profile_path or Path(
        "research/live_research/execution_replay_profile_latest.json"
    )
    # The Delta product homepage has its own source of truth.  Keep the
    # realtime-scanner fallback for callers that still publish the historical
    # combined payload, but allow production/local runtimes to point directly
    # at the dedicated Delta sidecar snapshot.
    delta_scalper_file = (
        delta_scalper_path
        or realtime_scanner_path
        or Path("research/live_research/delta_scalper_engine_latest.json")
    )
    delta_active_cost_evidence_file = delta_active_cost_evidence_path
    indicator_score_calibration_file = indicator_score_calibration_path or Path(
        "research/live_research/indicator_score_calibration_latest.json"
    )
    revived_scanner_evidence_file = revived_scanner_evidence_path or Path(
        "research/live_research/mtf_amf_confirmed_rejection_v2_latest.json"
    )
    revival_experiment_matrix_file = revival_experiment_matrix_path or Path(
        "research/live_research/mtf_amf_revival_matrix_latest.json"
    )
    delta_event_root_dir = delta_event_root or Path("data/delta_events")
    event_trigger_telemetry_file = event_trigger_telemetry_path or Path(
        "research/live_research/delta_event_trigger_telemetry_latest.json"
    )
    absorption_dashboard_file = absorption_dashboard_path or Path(
        "research/live_research/delta_absorption_dashboard_latest.json"
    )
    event_replay_output_dir = event_replay_dir or Path("research/event_replay")
    replay_determinism_proof_file = replay_determinism_proof_path or Path(
        "research/event_replay/replay_determinism_latest.json"
    )
    event_continuity_file = event_continuity_path or Path(
        "research/live_research/delta_event_continuity_latest.json"
    )
    kronos_matrix_file = kronos_matrix_path or Path(
        "research/live_research/kronos_permutation_matrix_latest.json"
    )
    kronos_confirmation_file = kronos_confirmation_path or Path(
        "research/live_research/kronos_permutation_confirmation_latest.json"
    )
    forced_flow_output_dir = forced_flow_dir or Path(
        "research/live_research/delta_forced_flow_panel"
    )
    htf_structure_file = htf_structure_path or Path(
        "research/live_research/htf_structure_break_v1_latest.json"
    )
    htf_structure_v2_file = htf_structure_v2_path or Path(
        "research/live_research/htf_structure_break_v2_latest.json"
    )
    failed_auction_readiness_file = failed_auction_readiness_path or Path(
        "research/live_research/failed_auction_response_v1_readiness_latest.json"
    )
    paper_lane_activation_file = paper_lane_activation_path or Path(
        "research/live_research/paper_lane_activation_latest.json"
    )
    promotion_review_runbook_file = promotion_review_runbook_path or Path(
        "research/live_research/promotion_review_runbook_latest.json"
    )
    paper_route_doctor_file = paper_route_doctor_path or Path(
        "research/live_research/paper_route_doctor_latest.json"
    )
    paper_lane_cadence_file = paper_lane_cadence_path or Path(
        "research/live_research/paper_lane_cadence_latest.json"
    )
    paper_lane_performance_file = paper_lane_performance_path or Path(
        "research/live_research/paper_lane_performance_latest.json"
    )
    paper_trade_entry_autopsy_file = paper_trade_entry_autopsy_path or Path(
        "research/live_research/paper_trade_entry_autopsy_latest.json"
    )
    paper_trade_exit_autopsy_file = paper_trade_exit_autopsy_path or Path(
        "research/live_research/paper_trade_exit_autopsy_latest.json"
    )
    trade_analyzer_os_file = trade_analyzer_os_path or Path(
        "research/live_research/trade_analyzer_os_latest.json"
    )
    paper_lane_root_cause_file = paper_lane_root_cause_path or Path(
        "research/live_research/paper_lane_root_cause_latest.json"
    )
    maker_quote_lifecycle_file = maker_quote_lifecycle_path or Path(
        "research/live_research/maker_quote_lifecycle_latest.json"
    )
    paper_trade_contract_reconciler_file = paper_trade_contract_reconciler_path or Path(
        "research/live_research/paper_trade_contract_reconciler_latest.json"
    )
    paper_promotion_bridge_file = paper_promotion_bridge_path or Path(
        "research/live_research/paper_promotion_bridge_latest.json"
    )
    lane_survival_file = lane_survival_path or Path(
        "research/live_research/lane_survival_latest.json"
    )
    paper_lane_governor_file = paper_lane_governor_path or Path(
        "research/live_research/paper_lane_governor_latest.json"
    )
    paper_roster_drift_file = paper_roster_drift_path or Path(
        "research/live_research/paper_roster_drift_latest.json"
    )
    darwinian_agent_survival_file = darwinian_agent_survival_path or Path(
        "research/live_research/darwinian_agent_survival_latest.json"
    )
    ml_pipeline_status_file = ml_pipeline_status_path or Path(
        "research/live_research/ml_pipeline_status.json"
    )

    @app.get("/")
    async def index() -> FileResponse:
        # Delta India is the product identity. The multi-venue desk remains
        # available below as a secondary research surface.
        return FileResponse(
            _STATIC_DIR / "delta_home.html",
            headers={"Cache-Control": "no-store, must-revalidate"},
        )

    @app.get("/research-lab")
    async def research_lab_page() -> FileResponse:
        """Secondary, read-only multi-venue research and archival desk."""

        return FileResponse(
            _STATIC_DIR / "index.html",
            headers={"Cache-Control": "no-store, must-revalidate"},
        )

    @app.get("/delta-research")
    async def delta_research_page() -> FileResponse:
        """Delta-only evidence, integrity, scanner, and latency detail."""

        return FileResponse(
            _STATIC_DIR / "delta_research.html",
            headers={"Cache-Control": "no-store, must-revalidate"},
        )

    @app.get("/pine-research")
    async def pine_research_page() -> FileResponse:
        # Separate static research page. Data remains token-gated below.
        return FileResponse(
            _STATIC_DIR / "pine_research.html",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/quantified-strategy-lab")
    async def quantified_strategy_lab_page() -> FileResponse:
        return FileResponse(
            _STATIC_DIR / "quantified_strategy_lab.html",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/state")
    async def state(request: Request) -> JSONResponse:
        user = _authorized(request)
        snapshot = provider.latest()
        if snapshot is None:
            return JSONResponse(
                {"status": "no snapshot yet"}, status_code=503, headers=_identity(user)
            )
        return JSONResponse(snapshot, headers=_identity(user))

    @app.get("/whoami")
    async def whoami(request: Request) -> JSONResponse:
        """The authenticated caller's identity, role, and permission set.

        Read-only and reveals only the caller's OWN identity (never the token,
        never other users). A frontend uses `permissions` to show/hide controls;
        the server still enforces every control server-side via
        `_require_permission`."""
        user = _authorized(request)
        return JSONResponse(
            {
                "name": user.name,
                "role": user.role,
                "permissions": permissions_for(user.role),
                "expires_at": user.expires_at.isoformat() if user.expires_at else None,
            },
            headers=_identity(user),
        )

    @app.post("/auth/session")
    async def auth_session(request: Request) -> JSONResponse:
        """Exchange the (long-lived) root token for a short-lived session JWT.

        Authenticates the presented token exactly like any data route, then mints
        a JWT carrying the same identity/role. The browser uses the JWT after
        this, so the root secret stops travelling on every request. Read-only:
        this grants no new capability — the session's role equals the token's."""
        user = _authorized(request)
        session = issuer.issue(user.name or "", user.role or "viewer")
        response = JSONResponse(
            {
                "token": session.token,
                "expires_at": session.expires_at.isoformat(),
                "name": user.name,
                "role": user.role,
            },
            headers=_identity(user),
        )
        response.set_cookie(
            key="vnedge_session",
            value=session.token,
            max_age=issuer.ttl_seconds,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="strict",
            path="/",
        )
        return response

    def _query_lane(request: Request) -> str:
        lane = request.query_params.get("lane", "").strip()
        if lane and not _LANE_ID_RE.match(lane):
            raise HTTPException(status_code=400, detail="invalid lane id")
        return lane

    def _query_days(request: Request) -> float | None:
        raw = request.query_params.get("days", "").strip()
        if not raw:
            return None
        try:
            days = float(raw)
        except ValueError:
            raise HTTPException(status_code=400, detail="days must be a number")
        if days <= 0:
            raise HTTPException(status_code=400, detail="days must be positive")
        return days

    def _since_iso(days: float | None) -> str | None:
        if days is None:
            return None
        from datetime import UTC, datetime, timedelta

        return (datetime.now(UTC) - timedelta(days=days)).isoformat()

    def _lane_file(lane: str, suffix: str) -> Path | None:
        """Resolve a per-lane data file; empty lane means the primary lane."""
        if lane and lane_dir is not None:
            return lane_dir / f"{lane}{suffix}"
        if suffix == ".equity.jsonl":
            return history_path
        if history_path is not None and history_path.name.endswith(".equity.jsonl"):
            primary = history_path.name.removesuffix(".equity.jsonl")
            return history_path.parent / f"{primary}{suffix}"
        return None

    def _equity_points(lane: str, since: str | None) -> list[dict]:
        path = _lane_file(lane, ".equity.jsonl")
        points: list[dict] = []
        if path is not None and path.exists():
            for record in _iter_jsonl(path, max_bytes=4_000_000):
                if since is not None and str(record.get("ts", "")) < since:
                    continue
                points.append(record)
        return points[-2000:]

    @app.get("/history")
    async def history(request: Request) -> JSONResponse:
        """Persisted equity curve (survives restarts and page reloads).

        Optional filters: ?days=N (recent window) and ?lane=<id> (any lane's
        equity file next to the primary one)."""
        user = _authorized(request)
        lane = _query_lane(request)
        since = _since_iso(_query_days(request))
        return JSONResponse(_equity_points(lane, since), headers=_identity(user))

    @app.get("/export.csv")
    async def export_csv(request: Request) -> Response:
        """Per-lane CSV export: equity curve + trade log + fills, one flat
        table keyed by record_type. Same filters as /history."""
        user = _authorized(request)
        lane = _query_lane(request)
        since = _since_iso(_query_days(request))
        lane_label = lane
        if not lane_label and history_path is not None:
            lane_label = history_path.name.removesuffix(".equity.jsonl")
        lane_label = lane_label or "primary"

        fields = [
            "record_type",
            "ts",
            "lane",
            "equity",
            "event",
            "detail",
            "symbol",
            "side",
            "quantity",
            "price",
            "fee_usd",
            "realized_pnl_usd",
            "client_order_id",
        ]

        def rows():
            for point in _equity_points(lane, since):
                yield {
                    "record_type": "equity",
                    "ts": point.get("ts", ""),
                    "equity": point.get("equity", ""),
                }
            for event in _snapshot_trade_log(provider.latest(), lane):
                ts = str(event.get("ts", ""))
                if since is not None and ts < since:
                    continue
                yield {
                    "record_type": "trade_log",
                    "ts": ts,
                    "event": event.get("event", ""),
                    "detail": event.get("detail", ""),
                }
            fills_path = _lane_file(lane, ".fills.jsonl")
            if fills_path is not None and fills_path.exists():
                for fill in _iter_jsonl(fills_path, max_bytes=4_000_000):
                    ts = str(fill.get("ts", ""))
                    if since is not None and ts < since:
                        continue
                    yield {
                        "record_type": "fill",
                        "ts": ts,
                        "symbol": fill.get("symbol", ""),
                        "side": fill.get("side", ""),
                        "quantity": fill.get("quantity", ""),
                        "price": fill.get("price", ""),
                        "fee_usd": fill.get("fee_usd", ""),
                        "realized_pnl_usd": fill.get("realized_pnl_usd", ""),
                        "client_order_id": fill.get("client_order_id", ""),
                    }

        def stream():
            buffer = io.StringIO()
            writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for row in rows():
                writer.writerow({"lane": lane_label, **row})
                if buffer.tell() > 64_000:
                    yield buffer.getvalue()
                    buffer.seek(0)
                    buffer.truncate()
            yield buffer.getvalue()

        return StreamingResponse(
            stream(),
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="vnedge_{lane_label}.csv"',
                **_identity(user),
            },
        )

    @app.get("/trade-journal")
    async def trade_journal(request: Request, limit: str = "200") -> JSONResponse:
        """Read-only trade journal projection.

        Combines current snapshot positions/orders with per-lane decision
        journals and hash-chained fill ledgers. No controls, no mutations.
        """
        user = _authorized(request)
        lane = _query_lane(request)
        since = _since_iso(_query_days(request))
        try:
            limit = max(1, min(int(limit), 500))
        except ValueError:
            raise HTTPException(status_code=400, detail="limit must be an integer")
        return JSONResponse(
            build_trade_journal(
                snapshot=provider.latest(),
                journal_dir=lane_dir,
                history_path=history_path,
                lane=lane,
                since=since,
                limit=limit,
            ),
            headers=_identity(user),
        )

    @app.get("/session-regime")
    async def session_regime(request: Request, limit: str = "4000") -> JSONResponse:
        """Session-regime rollup: closed trades bucketed by UTC entry session.

        Answers *when* each strategy earns (asia/europe/us/late) — trades, win
        rate, net $, worst stretch, break-even cushion per (strategy x session).
        Recent-window view over the same active-lane-filtered ledgers as
        /trade-journal. Read-only, no controls.
        """
        user = _authorized(request)
        lane = _query_lane(request)
        since = _since_iso(_query_days(request))
        try:
            limit = max(1, min(int(limit), 20000))
        except ValueError:
            raise HTTPException(status_code=400, detail="limit must be an integer")
        return JSONResponse(
            build_session_regime(
                snapshot=provider.latest(),
                journal_dir=lane_dir,
                lane=lane,
                since=since,
                limit=limit,
            ),
            headers=_identity(user),
        )

    @app.get("/incidents")
    async def incidents(request: Request) -> JSONResponse:
        """Merged reverse-chronological incident timeline: fired alerts plus
        incident-class decision-journal records, each with a runbook link."""
        user = _authorized(request)
        try:
            limit = int(request.query_params.get("limit", "100"))
        except ValueError:
            raise HTTPException(status_code=400, detail="limit must be an integer")
        limit = max(1, min(limit, 500))
        alert_files: list[Path] = []
        if alerts_path is not None:
            alert_files.append(alerts_path)
        if lane_dir is not None and lane_dir.is_dir():
            alert_files.extend(
                p for p in sorted(lane_dir.glob("*.alerts.jsonl")) if p != alerts_path
            )
        merged = _alert_incidents(alert_files) + _journal_incidents(lane_dir)
        merged.sort(key=lambda record: record["ts"], reverse=True)
        return JSONResponse(merged[:limit], headers=_identity(user))

    @app.get("/runbooks")
    async def runbooks(request: Request) -> HTMLResponse:
        """docs/RUNBOOKS.md rendered minimally so incident links can anchor
        into it. Read-only, token-gated like every data route."""
        user = _authorized(request)
        try:
            markdown = runbooks_file.read_text(encoding="utf-8")
        except OSError:
            raise HTTPException(status_code=404, detail="runbooks document not found")
        return HTMLResponse(_render_runbooks_html(markdown), headers=_identity(user))

    @app.get("/research")
    async def research(request: Request) -> JSONResponse:
        """Latest rolling walk-forward verdicts from the research loop."""
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(research_path, {"results": []}), headers=_identity(user)
        )

    @app.get("/cost-model")
    async def cost_model(request: Request) -> JSONResponse:
        """The real maker-first (~8bps) and taker (~11bps) round-trip cost
        models, read from the research/paper constants — not hardcoded in the
        UI. Auth-gated like every data route; read-only."""
        user = _authorized(request)
        return JSONResponse(_cost_model_payload(), headers=_identity(user))

    @app.get("/alpha-council")
    async def alpha_council(request: Request) -> JSONResponse:
        """Latest deterministic agent debate over research candidates."""
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                alpha_council_path,
                {"summary": {}, "debates": [], "can_trade": False, "can_promote": False},
            ),
            headers=_identity(user),
        )

    @app.get("/alpha-workbench")
    async def alpha_workbench(request: Request) -> JSONResponse:
        """Latest persistent proof-task backlog generated from the council."""
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                alpha_workbench_path,
                {"summary": {}, "tasks": [], "can_trade": False, "can_promote": False},
            ),
            headers=_identity(user),
        )

    @app.get("/vibe-intelligence")
    async def vibe_intelligence(request: Request) -> JSONResponse:
        """Latest persistent hypothesis lifecycle memory."""
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                vibe_intelligence_path,
                {"summary": {}, "cards": [], "can_trade": False, "can_promote": False},
            ),
            headers=_identity(user),
        )

    @app.get("/external-repo-synthesis")
    async def external_repo_synthesis(request: Request) -> JSONResponse:
        """Research-only synthesis of public repo review patterns.

        This is a source-attributed build queue, not a code import surface and
        not a trading/promotion route.
        """
        user = _authorized(request)
        return JSONResponse(build_external_repo_synthesis(), headers=_identity(user))

    @app.get("/agentic-research-os")
    async def agentic_research_os(request: Request) -> JSONResponse:
        """Latest Agentic Research OS supervisor report.

        This is dashboard-token gated and research-only. It ranks agent work,
        verifier gaps, stale tasks, and keep/decay/retire actions without
        granting trade or promotion authority.
        """
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                agentic_research_os_file,
                {
                    "os_id": "agentic_research_os_v2",
                    "summary": {},
                    "agent_scorecards": [],
                    "operator_queue": [],
                    "source_status": [],
                    "operator_answer": "agentic research os artifact unavailable",
                    "can_trade": False,
                    "can_promote": False,
                    "live_orders_enabled": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/agent-jobs")
    async def agent_jobs(request: Request, limit: int = 100) -> JSONResponse:
        """Operator-facing Agent Gateway job ledger.

        This is dashboard-token gated and read-only. It works even when the
        agent HTTP API is intentionally unmounted because no agent tokens are
        configured.
        """
        user = _authorized(request)
        limit = max(1, min(int(limit), 200))
        return JSONResponse(
            _agent_jobs_payload(
                agent_jobs_path,
                limit=limit,
                gateway_http_mounted=agent_gateway_http_mounted,
            ),
            headers=_identity(user),
        )

    @app.get("/quant-os/agent-gateway")
    async def quant_os_agent_gateway(request: Request, limit: int = 100) -> JSONResponse:
        """Operator-facing Quant OS Agent Gateway v2 ledger.

        This is dashboard-token gated and read-only. Agent-token write routes
        live under /api/agent/v2 and still cannot trade or promote.
        """
        user = _authorized(request)
        return JSONResponse(
            quant_os_gateway.snapshot(limit=max(1, min(int(limit), 250))),
            headers=_identity(user),
        )

    @app.get("/quant-os/agent-gateway/events")
    async def quant_os_agent_gateway_events(request: Request, limit: int = 100) -> Response:
        """Recent Agent Gateway v2 events as JSON or finite SSE frames."""
        user = _authorized(request)
        snapshot = quant_os_gateway.snapshot(limit=max(1, min(int(limit), 250)))
        if "text/event-stream" in request.headers.get("accept", ""):
            return StreamingResponse(
                iter(quant_os_event_stream(snapshot)),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-store", **_identity(user)},
            )
        return JSONResponse(
            {
                "gateway_id": snapshot["gateway_id"],
                "events": snapshot["events"],
                "can_trade": False,
                "can_promote": False,
            },
            headers=_identity(user),
        )

    @app.get("/lane-readiness")
    async def lane_readiness(request: Request) -> JSONResponse:
        """Latest lane firing/promotability report."""
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                lane_readiness_path,
                {
                    "summary": {},
                    "rows": [],
                    "operator_answer": "lane readiness report unavailable",
                    "can_trade": False,
                    "can_promote": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/promotion-review-runbook")
    async def promotion_review_runbook(request: Request) -> JSONResponse:
        """Latest promotion review runbook.

        This is the operator packet derived from red-team prosecution of PASSED
        walk-forward candidates. It never promotes or trades.
        """
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                promotion_review_runbook_file,
                {
                    "runbook_id": "promotion_review_runbook_v1",
                    "summary": {},
                    "rows": [],
                    "operator_answer": "promotion review runbook unavailable",
                    "can_trade": False,
                    "can_promote": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/ml-status")
    async def ml_status(request: Request) -> JSONResponse:
        """ML pipeline status — the meta-labeling training set accumulating from
        research journals, the pipeline stage, and the locked promotion gates.
        Read-only; no model or endpoint receives trade authority here."""
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                ml_pipeline_status_file,
                {
                    "stage": "COLLECTING_LABELS",
                    "stages": [],
                    "dataset": {
                        "samples": 0,
                        "min_to_train": 200,
                        "progress_pct": 0.0,
                        "by_strategy": {},
                    },
                    "foundation": {},
                    "gates": {
                        "deflated_sharpe_min": 0.95,
                        "pbo_max": 0.20,
                        "cpcv_median_profit_factor_min": 1.30,
                        "must_beat_rule_based_baseline": True,
                    },
                    "model": None,
                    "can_trade": False,
                    "can_promote": False,
                    "note": "ml pipeline status unavailable",
                },
            ),
            headers=_identity(user),
        )

    @app.get("/pre-live-checklist")
    async def pre_live_checklist(request: Request) -> JSONResponse:
        """The gates to a first live order + who must act on each red (deliberate
        / operator / system) + the ordered path to live. Computed on demand,
        read-only; booleans only — it never reads a secret value and cannot
        enable live trading."""
        user = _authorized(request)
        from vnedge.research.pre_live_status import build_pre_live_status

        ladder = _REPO_ROOT / "research" / "live_research" / "live_ladder_latest.json"
        return JSONResponse(
            build_pre_live_status(
                journal_dir=lane_dir or Path("logs/paper_trials"),
                ladder_path=ladder if ladder.exists() else None,
            ),
            headers=_identity(user),
        )

    @app.get("/paper-lane-activation")
    async def paper_lane_activation(request: Request) -> JSONResponse:
        """Latest paper activation truth board.

        This reconciles paper manifests, runtime paper routes, scanner pressure,
        and paper journals. It is read-only and cannot start or promote a lane.
        """
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                paper_lane_activation_file,
                {
                    "summary": {},
                    "boards": {},
                    "rows": [],
                    "operator_answer": "paper lane activation report unavailable",
                    "mode": "read_only_activation_truth",
                    "can_trade": False,
                    "can_promote": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/paper-lane-performance")
    async def paper_lane_performance(request: Request) -> JSONResponse:
        """Latest paper performance ledger.

        This summarizes paper journals and hash-chained fill ledgers into
        per-lane PnL/PF/sample status. It is read-only and cannot promote.
        """
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                paper_lane_performance_file,
                {
                    "summary": {},
                    "boards": {},
                    "rows": [],
                    "operator_answer": "paper performance report unavailable",
                    "mode": "read_only_paper_performance",
                    "can_trade": False,
                    "can_promote": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/paper-trade-exit-autopsy")
    async def paper_trade_exit_autopsy(request: Request) -> JSONResponse:
        """Latest paper trade exit autopsy.

        This explains closed paper-trade loss drivers from fills + exit journal
        metadata. It is read-only and cannot promote, demote, or trade.
        """
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                paper_trade_exit_autopsy_file,
                {
                    "summary": {},
                    "rows": [],
                    "operator_answer": "paper trade exit autopsy unavailable",
                    "mode": "read_only_paper_trade_exit_autopsy",
                    "can_trade": False,
                    "can_promote": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/paper-trade-entry-autopsy")
    async def paper_trade_entry_autopsy(request: Request) -> JSONResponse:
        """Latest paper trade entry autopsy.

        This joins closed paper entries to prior fired lane_eval context so
        operators can see stale entries, missing signal linkage, direction
        drift, and fee-wall-short expected edge. It is read-only.
        """
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                paper_trade_entry_autopsy_file,
                {
                    "summary": {},
                    "rows": [],
                    "operator_answer": "paper trade entry autopsy unavailable",
                    "mode": "read_only_paper_trade_entry_autopsy",
                    "can_trade": False,
                    "can_promote": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/trade-analyzer-os")
    async def trade_analyzer_os(request: Request) -> JSONResponse:
        """Latest joined trade analyzer verdict.

        This joins paper trade journal, entry autopsy, and exit autopsy into one
        read-only operator answer. It cannot promote, demote, or trade.
        """
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                trade_analyzer_os_file,
                {
                    "summary": {},
                    "rows": [],
                    "recent_trades": [],
                    "operator_answer": "trade analyzer OS unavailable",
                    "mode": "read_only_trade_analyzer_os",
                    "can_trade": False,
                    "can_promote": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/maker-quote-lifecycle")
    async def maker_quote_lifecycle(request: Request) -> JSONResponse:
        """Latest maker quote lifecycle report.

        This explains whether a lane has actual post-only maker quote, fill,
        cancel, and fee-aware taker fallback proof. It is read-only and cannot
        trade, promote, demote, or restart routes.
        """
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                maker_quote_lifecycle_file,
                {
                    "summary": {},
                    "boards": {},
                    "rows": [],
                    "operator_answer": "maker quote lifecycle report unavailable",
                    "mode": "read_only_maker_quote_lifecycle",
                    "can_trade": False,
                    "can_promote": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/paper-trade-contract-reconciler")
    async def paper_trade_contract_reconciler(request: Request) -> JSONResponse:
        """Latest paper trade contract reconciliation.

        This distinguishes execution/journal contract drift from contract-clean
        alpha/exit failure. It is read-only and cannot promote, demote, or trade.
        """
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                paper_trade_contract_reconciler_file,
                {
                    "summary": {},
                    "boards": {},
                    "rows": [],
                    "trade_samples": [],
                    "operator_answer": "paper trade contract reconciler unavailable",
                    "mode": "read_only_paper_contract_reconciliation",
                    "can_trade": False,
                    "can_promote": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/paper-promotion-bridge")
    async def paper_promotion_bridge(request: Request) -> JSONResponse:
        """Latest joined paper/live-review bridge.

        This joins lane readiness, paper performance, contract truth,
        maker/taker lifecycle, and operator actions into a single conservative
        review answer. It is read-only and cannot promote or trade.
        """
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                paper_promotion_bridge_file,
                {
                    "summary": {},
                    "boards": {},
                    "rows": [],
                    "operator_answer": "paper promotion bridge unavailable",
                    "mode": "read_only_paper_promotion_bridge",
                    "can_trade": False,
                    "can_promote": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/lane-survival")
    async def lane_survival(request: Request) -> JSONResponse:
        """Latest lane survival engine report.

        This reconciles activation, route, cadence, and corrected performance
        into keep/observe/demote/repair recommendations. It is read-only and
        cannot mutate routes or promote a lane.
        """
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                lane_survival_file,
                {
                    "summary": {},
                    "boards": {},
                    "rows": [],
                    "operator_answer": "lane survival report unavailable",
                    "mode": "read_only_lane_survival",
                    "can_trade": False,
                    "can_promote": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/paper-lane-governor")
    async def paper_lane_governor(request: Request) -> JSONResponse:
        """Latest paper lane governor report.

        This turns lane-survival evidence into a proposed paper roster,
        survivor tournament, demotion queue, and repair queue. It is read-only
        and cannot mutate runtime lanes.
        """
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                paper_lane_governor_file,
                {
                    "summary": {},
                    "proposed_roster": {},
                    "boards": {},
                    "rows": [],
                    "operator_answer": "paper lane governor report unavailable",
                    "mode": "read_only_paper_lane_governor",
                    "can_trade": False,
                    "can_promote": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/darwinian-agent-survival")
    async def darwinian_agent_survival(request: Request) -> JSONResponse:
        """Latest Atlas-inspired agent/cohort survival report.

        This computes advisory Darwinian weights and JANUS cohort weights from
        existing evidence. It is read-only and cannot mutate runtime lanes.
        """
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                darwinian_agent_survival_file,
                {
                    "summary": {},
                    "cohorts": [],
                    "agents": [],
                    "operator_answer": "darwinian agent survival report unavailable",
                    "mode": "atlas_inspired_read_only_agent_survival",
                    "can_trade": False,
                    "can_promote": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/paper-roster-drift")
    async def paper_roster_drift(request: Request) -> JSONResponse:
        """Latest paper roster drift report.

        This compares the governor's proposed paper roster to runtime scanner
        and activation evidence, naming extra/missing paper lanes. It is
        read-only and cannot mutate runtime lanes.
        """
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                paper_roster_drift_file,
                {
                    "summary": {},
                    "rows": [],
                    "operator_answer": "paper roster drift report unavailable",
                    "mode": "read_only_unified_lane_roster",
                    "can_trade": False,
                    "can_promote": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/paper-route-doctor")
    async def paper_route_doctor(request: Request) -> JSONResponse:
        """Latest paper route/journal doctor.

        It explains whether approved paper routes have fresh journal proof and
        whether the runner service is visible. Read-only; no restarts/trades.
        """
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                paper_route_doctor_file,
                {
                    "summary": {},
                    "rows": [],
                    "runner_service": {"state": "unknown", "up": None},
                    "operator_answer": "paper route doctor report unavailable",
                    "mode": "read_only_paper_route_doctor",
                    "can_trade": False,
                    "can_promote": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/trade-profile-matrix")
    async def trade_profile_matrix(request: Request) -> JSONResponse:
        """Read-only paper/live sizing profile matrix.

        It is derived from the paper activation artifact. Dashboard inputs are
        planner-only; this endpoint cannot apply margin/leverage changes.
        """
        user = _authorized(request)
        from vnedge.research.trade_profile_matrix import build_trade_profile_matrix

        activation = _read_json_payload(
            paper_lane_activation_file,
            {
                "summary": {},
                "boards": {},
                "rows": [],
                "operator_answer": "paper lane activation report unavailable",
                "mode": "read_only_activation_truth",
                "can_trade": False,
                "can_promote": False,
            },
        )
        return JSONResponse(
            build_trade_profile_matrix(activation),
            headers=_identity(user),
        )

    @app.get("/paper-lane-cadence")
    async def paper_lane_cadence(request: Request) -> JSONResponse:
        """Latest paper lane evaluation cadence report.

        It tells whether routed paper lanes are emitting live lane_eval events
        frequently enough for their timeframe. Read-only; no restarts/trades.
        """
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                paper_lane_cadence_file,
                {
                    "summary": {},
                    "rows": [],
                    "operator_answer": "paper lane cadence report unavailable",
                    "mode": "read_only_paper_lane_cadence",
                    "can_trade": False,
                    "can_promote": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/operator-actions")
    async def operator_actions(request: Request) -> JSONResponse:
        """Read-only ranked action queue for paper/scanner operations.

        Joins activation, route doctor, cadence, profile, performance, and
        causality evidence into one operator answer. It cannot trade, promote,
        restart runners, or apply profile changes.
        """
        user = _authorized(request)
        from vnedge.research.operator_actions import build_operator_actions
        from vnedge.research.trade_profile_matrix import build_trade_profile_matrix

        activation = _read_json_payload(
            paper_lane_activation_file,
            {
                "summary": {},
                "boards": {},
                "rows": [],
                "operator_answer": "paper lane activation report unavailable",
                "mode": "read_only_activation_truth",
                "can_trade": False,
                "can_promote": False,
            },
        )
        route = _read_json_payload(
            paper_route_doctor_file,
            {
                "summary": {},
                "rows": [],
                "runner_service": {"state": "unknown", "up": None},
                "operator_answer": "paper route doctor report unavailable",
                "mode": "read_only_paper_route_doctor",
                "can_trade": False,
                "can_promote": False,
            },
        )
        cadence = _read_json_payload(
            paper_lane_cadence_file,
            {
                "summary": {},
                "rows": [],
                "operator_answer": "paper lane cadence report unavailable",
                "mode": "read_only_paper_lane_cadence",
                "can_trade": False,
                "can_promote": False,
            },
        )
        performance = _read_json_payload(
            paper_lane_performance_file,
            {
                "summary": {},
                "boards": {},
                "rows": [],
                "operator_answer": "paper performance report unavailable",
                "mode": "read_only_paper_performance",
                "can_trade": False,
                "can_promote": False,
            },
        )
        exit_autopsy = _read_json_payload(
            paper_trade_exit_autopsy_file,
            {
                "summary": {},
                "rows": [],
                "operator_answer": "paper trade exit autopsy unavailable",
                "mode": "read_only_paper_trade_exit_autopsy",
                "can_trade": False,
                "can_promote": False,
            },
        )
        contract_reconciler = _read_json_payload(
            paper_trade_contract_reconciler_file,
            {
                "summary": {},
                "boards": {},
                "rows": [],
                "trade_samples": [],
                "operator_answer": "paper trade contract reconciler unavailable",
                "mode": "read_only_paper_contract_reconciliation",
                "can_trade": False,
                "can_promote": False,
            },
        )
        causality = _read_json_payload(
            lane_firing_causality_file,
            {
                "summary": {},
                "promotion_board": {},
                "rows": [],
                "operator_answer": "lane firing causality report unavailable",
                "mode": "read_only_operator_truth",
                "can_trade": False,
                "can_promote": False,
            },
        )
        return JSONResponse(
            build_operator_actions(
                activation=activation,
                route=route,
                cadence=cadence,
                performance=performance,
                exit_autopsy=exit_autopsy,
                contract_reconciler=contract_reconciler,
                profile=build_trade_profile_matrix(activation),
                causality=causality,
            ),
            headers=_identity(user),
        )

    @app.get("/paper-lane-root-cause")
    async def paper_lane_root_cause(request: Request) -> JSONResponse:
        """Read-only root-cause matrix for paper lanes.

        Joins activation, route, cadence, sizing profile, entry/exit autopsy,
        performance, survival, governor, and causality into one primary
        blocker per lane. It cannot trade, promote, demote, or apply fixes.
        """
        user = _authorized(request)
        cached = _read_json_payload(
            paper_lane_root_cause_file,
            {
                "summary": {},
                "boards": {},
                "rows": [],
                "operator_answer": "paper lane root-cause report unavailable",
                "mode": "read_only_paper_lane_root_cause",
                "can_trade": False,
                "can_promote": False,
            },
        )
        if cached.get("rows") or cached.get("summary"):
            return JSONResponse(cached, headers=_identity(user))

        from vnedge.research.paper_lane_root_cause import build_paper_lane_root_cause
        from vnedge.research.trade_profile_matrix import build_trade_profile_matrix

        activation = _read_json_payload(
            paper_lane_activation_file,
            {
                "summary": {},
                "boards": {},
                "rows": [],
                "operator_answer": "paper lane activation report unavailable",
                "mode": "read_only_activation_truth",
                "can_trade": False,
                "can_promote": False,
            },
        )
        route = _read_json_payload(
            paper_route_doctor_file,
            {
                "summary": {},
                "rows": [],
                "runner_service": {"state": "unknown", "up": None},
                "operator_answer": "paper route doctor report unavailable",
                "mode": "read_only_paper_route_doctor",
                "can_trade": False,
                "can_promote": False,
            },
        )
        cadence = _read_json_payload(
            paper_lane_cadence_file,
            {
                "summary": {},
                "rows": [],
                "operator_answer": "paper lane cadence report unavailable",
                "mode": "read_only_paper_lane_cadence",
                "can_trade": False,
                "can_promote": False,
            },
        )
        performance = _read_json_payload(
            paper_lane_performance_file,
            {
                "summary": {},
                "boards": {},
                "rows": [],
                "operator_answer": "paper performance report unavailable",
                "mode": "read_only_paper_performance",
                "can_trade": False,
                "can_promote": False,
            },
        )
        exit_autopsy = _read_json_payload(
            paper_trade_exit_autopsy_file,
            {
                "summary": {},
                "rows": [],
                "operator_answer": "paper trade exit autopsy unavailable",
                "mode": "read_only_paper_trade_exit_autopsy",
                "can_trade": False,
                "can_promote": False,
            },
        )
        survival = _read_json_payload(
            lane_survival_file,
            {
                "summary": {},
                "boards": {},
                "rows": [],
                "operator_answer": "lane survival report unavailable",
                "mode": "read_only_lane_survival",
                "can_trade": False,
                "can_promote": False,
            },
        )
        governor = _read_json_payload(
            paper_lane_governor_file,
            {
                "summary": {},
                "proposed_roster": {},
                "boards": {},
                "rows": [],
                "operator_answer": "paper lane governor report unavailable",
                "mode": "read_only_paper_lane_governor",
                "can_trade": False,
                "can_promote": False,
            },
        )
        causality = _read_json_payload(
            lane_firing_causality_file,
            {
                "summary": {},
                "promotion_board": {},
                "rows": [],
                "operator_answer": "lane firing causality report unavailable",
                "mode": "read_only_operator_truth",
                "can_trade": False,
                "can_promote": False,
            },
        )
        return JSONResponse(
            build_paper_lane_root_cause(
                activation=activation,
                route=route,
                cadence=cadence,
                performance=performance,
                exit_autopsy=exit_autopsy,
                survival=survival,
                governor=governor,
                profile=build_trade_profile_matrix(activation),
                causality=causality,
            ),
            headers=_identity(user),
        )

    fleet_status_file = Path("logs/fleet.json")

    @app.get("/meta")
    async def meta(request: Request) -> JSONResponse:
        """Build provenance: deployed git sha (baked at image build), host, and
        dashboard-process uptime. Read-only."""
        _authorized(request)
        return JSONResponse(
            {
                "build_sha": _build_sha(),
                "host": os.environ.get("VNEDGE_HOST") or socket.gethostname(),
                "uptime_seconds": int(max(0.0, time.time() - _APP_START)),
            }
        )

    @app.get("/fleet")
    async def fleet(request: Request) -> JSONResponse:
        """Container fleet status, written host-side by scripts/fleet_status.sh
        (the dashboard container has no docker access). Empty until the host
        timer runs. Read-only."""
        _authorized(request)
        payload = _read_json_payload(fleet_status_file, {"services": [], "written_at": None})
        return JSONResponse(payload)

    @app.get("/scorecard")
    async def scorecard(request: Request) -> JSONResponse:
        """Per-strategy scanner scorecard: best net edge (bps), fee-wall verdict,
        profit factor and break rate from the fee-wall forensics artifact, plus
        the approved paper-probe promotion queue. Read-only research surface —
        cannot trade or promote."""
        _authorized(request)
        forensics = _read_json_payload(fee_wall_forensics_file, {"reports": []})
        probes = _read_json_payload(fee_wall_probes_file, {"paper_probes": []})
        probe_actuals = _read_json_payload(fee_wall_probe_actuals_file, {"rows": [], "summary": {}})
        by: dict = {}
        for r in forensics.get("reports", []):
            strat = r.get("strategy")
            summ = r.get("summary") or {}
            net = summ.get("avg_selected_net_bps")
            if not strat or net is None:
                continue
            g = by.setdefault(
                strat,
                {
                    "strategy": strat,
                    "best_net_bps": None,
                    "verdict": None,
                    "profit_factor": None,
                    "break_rate_pct": None,
                    "samples": 0,
                    "venues": set(),
                },
            )
            if r.get("exchange"):
                g["venues"].add(r["exchange"])
            g["samples"] += int(summ.get("opportunities") or 0)
            if g["best_net_bps"] is None or net > g["best_net_bps"]:
                g["best_net_bps"] = net
                g["verdict"] = summ.get("verdict")
                g["profit_factor"] = summ.get("profit_factor")
                g["break_rate_pct"] = summ.get("fee_wall_break_rate_pct")
        rows = []
        for g in by.values():
            g = dict(g)
            g["venues"] = sorted(v for v in g["venues"] if v)
            rows.append(g)
        rows.sort(key=lambda r: (r["best_net_bps"] is None, -(r["best_net_bps"] or -1e9)))
        return JSONResponse(
            {
                "generated_at": forensics.get("generated_at"),
                "strategies": rows,
                "probes": probes.get("paper_probes", []),
                "probe_actuals": probe_actuals.get("rows", []),
                "probe_actuals_summary": probe_actuals.get("summary", {}),
                "can_trade": False,
                "can_promote": False,
            }
        )

    @app.get("/realtime-scanner")
    async def realtime_scanner(request: Request) -> JSONResponse:
        """Latest live scanner pressure report.

        This is intentionally separate from replay/candidate-replay reports:
        it reads current runtime journals only and cannot trade or promote.
        """
        user = _authorized(request)
        payload = _read_json_payload(
            realtime_scanner_path,
            {
                "summary": {},
                "rows": [],
                "operator_answer": "real-time scanner report unavailable",
                "mode": "live_observation_not_replay",
                "can_trade": False,
                "can_promote": False,
            },
        )
        return JSONResponse(
            dashboard_scanner_payload(payload),
            headers=_identity(user),
        )

    @app.get("/scanner-evidence")
    async def scanner_evidence(request: Request) -> JSONResponse:
        """Forward outcomes, expanded backtest, and locked promotion gates.

        The artifact is read-only and cannot place orders or promote a lane.
        """

        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                scanner_forward_evidence_file,
                {
                    "report_id": "mtf_amf_forward_evidence_v1",
                    "summary": {"journaled_alerts": 0, "resolved_outcomes": 0},
                    "horizons": {},
                    "market_breakdown": {},
                    "promotion": {
                        "verdict": "INSUFFICIENT_UNTOUCHED_BACKTEST",
                        "eligible_for_paper_review": False,
                        "paper_trading_enabled": False,
                        "gates": [],
                    },
                    "policy": {
                        "l2_is_confirmation_only": True,
                        "can_trade": False,
                        "can_promote": False,
                    },
                    "can_trade": False,
                    "can_promote": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/delta-scalper")
    async def delta_scalper(request: Request) -> JSONResponse:
        """Research-only Delta scalper regimes, flow, costs, and evidence."""

        user = _authorized(request)
        payload = _read_json_payload(
            delta_scalper_file,
            {"rows": [], "can_trade": False, "can_promote": False},
        )

        rows = [
            row
            for row in payload.get("rows", [])
            if isinstance(row, dict) and row.get("strategy_id") == "delta_scalper_engine_v1"
        ]
        embedded_panels = payload.get("delta_scalper")
        if not isinstance(embedded_panels, dict):
            embedded_panels = {
                "summary": payload.get("summary"),
                "architecture": payload.get("architecture"),
                "fee_model": payload.get("fee_model"),
                "backtest_summary": payload.get("backtest_summary"),
                "fee_effectiveness": payload.get("fee_effectiveness"),
                "robust_validation": payload.get("robust_validation"),
                "untouched_window": payload.get("untouched_window"),
            }
        else:
            embedded_panels = dict(embedded_panels)

        indicator_calibration = _read_json_payload(
            indicator_score_calibration_file,
            {
                "verdict": "NOT_RUN",
                "source": {"trades": 0},
                "deciles": [],
                "policy": {},
            },
        )
        indicator_policy = (
            indicator_calibration.get("policy")
            if isinstance(indicator_calibration.get("policy"), dict)
            else {}
        )
        indicator_calibration = {
            **indicator_calibration,
            "policy": {
                **indicator_policy,
                "advisory_only": True,
                "used_for_signal": False,
                "used_for_execution": False,
                "can_trade": False,
                "can_promote": False,
            },
            "can_trade": False,
            "can_promote": False,
        }
        embedded_panels["indicator_score_calibration"] = indicator_calibration
        revived_evidence = _read_json_payload(
            revived_scanner_evidence_file,
            {
                "schema_version": "vnedge.mtf_amf_confirmed_rejection.v2",
                "selection": {"metrics": {}, "gate": {"passed": False}},
                "untouched": {"status": "sealed", "eligible_to_open": False},
                "policy": {},
            },
        )
        revived_policy = (
            revived_evidence.get("policy")
            if isinstance(revived_evidence.get("policy"), dict)
            else {}
        )
        embedded_panels["revived_scanner_evidence"] = {
            **revived_evidence,
            "policy": {
                **revived_policy,
                "research_only": True,
                "registered_strategy": False,
                "paper_route": "absent",
                "order_route": "absent",
                "can_trade": False,
                "can_promote": False,
            },
            "can_trade": False,
            "can_promote": False,
        }
        revival_matrix = _read_json_payload(
            revival_experiment_matrix_file,
            {
                "schema_version": "vnedge.mtf_amf_revival_matrix.v1",
                "experiments": {},
                "policy": {},
            },
        )
        raw_experiments = revival_matrix.get("experiments")
        safe_experiments: dict[str, object] = {}
        if isinstance(raw_experiments, dict):
            for experiment_id, raw_experiment in raw_experiments.items():
                if not isinstance(raw_experiment, dict):
                    continue
                experiment_policy = raw_experiment.get("policy")
                safe_experiments[str(experiment_id)] = {
                    **raw_experiment,
                    "policy": {
                        **(
                            experiment_policy
                            if isinstance(experiment_policy, dict)
                            else {}
                        ),
                        "research_only": True,
                        "registered_strategy": False,
                        "paper_route": "absent",
                        "order_route": "absent",
                        "can_trade": False,
                        "can_promote": False,
                    },
                    "can_trade": False,
                    "can_promote": False,
                }
        matrix_policy = revival_matrix.get("policy")
        embedded_panels["revival_experiment_matrix"] = {
            **revival_matrix,
            "experiments": safe_experiments,
            "policy": {
                **(matrix_policy if isinstance(matrix_policy, dict) else {}),
                "research_only": True,
                "registered_strategies": [],
                "paper_route": "absent",
                "order_route": "absent",
                "can_trade": False,
                "can_promote": False,
            },
            "can_trade": False,
            "can_promote": False,
        }

        active_cost_evidence = embedded_panels.get("active_cost_evidence")
        if (
            not isinstance(active_cost_evidence, dict)
            and delta_active_cost_evidence_file is not None
        ):
            active_cost_evidence = _read_json_payload(delta_active_cost_evidence_file, {})
        active_cost_metrics = (
            active_cost_evidence.get("metrics") if isinstance(active_cost_evidence, dict) else None
        )
        if isinstance(active_cost_metrics, dict):
            source_backtest = embedded_panels.get("backtest_summary")
            if not isinstance(source_backtest, dict):
                source_backtest = {}
            active_backtest = dict(source_backtest)
            active_backtest.update(active_cost_metrics)
            active_backtest["positive_markets"] = active_cost_evidence.get("positive_markets", 0)
            active_backtest["markets"] = active_cost_evidence.get("markets", {})
            active_backtest["profit_factor_note"] = (
                "recomputed from per-trade gross returns under the active fee model"
            )
            active_backtest["active_cost_scenario"] = active_cost_evidence.get("fee_model", {})
            active_backtest["source_data_quality_pass"] = bool(
                source_backtest.get("data_quality_pass")
            )
            embedded_panels["source_backtest_summary"] = source_backtest
            embedded_panels["backtest_summary"] = active_backtest

        # The archived headline backtest was produced with the scalper fee
        # discount, while the current local config is not opted in. Present the
        # matching fixed-trade-set scenario as the active-cost headline and do
        # not reuse a profit factor computed under a different cost model.
        fee_model_view = embedded_panels.get("fee_model")
        fee_rows = embedded_panels.get("fee_effectiveness")
        backtest_view = embedded_panels.get("backtest_summary")
        if (
            not isinstance(active_cost_metrics, dict)
            and isinstance(fee_model_view, dict)
            and isinstance(fee_rows, list)
            and isinstance(backtest_view, dict)
        ):
            active_fee_row = next(
                (
                    row
                    for row in fee_rows
                    if isinstance(row, dict)
                    and bool(row.get("deto_enabled")) == bool(fee_model_view.get("deto_enabled"))
                    and bool(row.get("scalper_opted_in"))
                    == bool(fee_model_view.get("scalper_opted_in"))
                ),
                None,
            )
            if active_fee_row is not None:
                source_backtest = dict(backtest_view)
                active_backtest = dict(source_backtest)
                active_backtest["average_net_bps"] = active_fee_row.get("average_net_bps")
                active_backtest["net_bps"] = active_fee_row.get("net_bps")
                source_average = float(source_backtest.get("net_bps") or 0.0) / int(
                    source_backtest.get("trades") or 1
                )
                cost_mismatch = (
                    abs(float(active_fee_row.get("average_net_bps") or 0.0) - source_average)
                    > 1e-12
                )
                active_backtest["source_profit_factor"] = source_backtest.get("profit_factor")
                active_backtest["profit_factor"] = (
                    None if cost_mismatch else source_backtest.get("profit_factor")
                )
                active_backtest["profit_factor_note"] = (
                    "not recomputed for active fee scenario"
                    if cost_mismatch
                    else "matches active fee scenario"
                )
                active_backtest["active_cost_scenario"] = {
                    "deto_enabled": bool(fee_model_view.get("deto_enabled")),
                    "scalper_opted_in": bool(fee_model_view.get("scalper_opted_in")),
                }
                active_backtest["source_data_quality_pass"] = bool(
                    source_backtest.get("data_quality_pass")
                )
                embedded_panels["source_backtest_summary"] = source_backtest
                embedded_panels["backtest_summary"] = active_backtest

        enabled_scanners = []
        architecture = embedded_panels.get("architecture")
        components = architecture.get("components") if isinstance(architecture, dict) else None
        scanner_state = (
            str(components.get("scanner_engine") or "unknown")
            if isinstance(components, dict)
            else "unknown"
        )
        if scanner_state not in {
            "available_all_hypotheses_rejected_and_disabled",
            "disabled",
            "none",
            "unknown",
        }:
            enabled_scanners.append(scanner_state)

        lanes: list[dict] = []
        for row in rows:
            latest_eval = row.get("latest_eval")
            if not isinstance(latest_eval, dict):
                latest_eval = {}
            l2 = latest_eval.get("l2_confirmation")
            if not isinstance(l2, dict):
                l2 = {}
            pipeline_trace = latest_eval.get("pipeline_trace")
            if not isinstance(pipeline_trace, list):
                pipeline_trace = []

            latest_candidates = 0
            latest_accepted = 0
            for stage in pipeline_trace:
                if not isinstance(stage, dict):
                    continue
                if stage.get("name") != "fee_probability_confidence_gates":
                    continue
                match = re.search(
                    r"(\d+)\s*/\s*(\d+)\s+accepted",
                    str(stage.get("detail") or ""),
                )
                if match:
                    latest_accepted = int(match.group(1))
                    latest_candidates = int(match.group(2))
                break

            forward = row.get("forward_evidence")
            if not isinstance(forward, dict):
                forward = latest_eval.get("forward_evidence")
            if not isinstance(forward, dict):
                forward = {}

            if not enabled_scanners:
                no_signal_reason = (
                    "All primary scanner hypotheses are rejected and disabled; "
                    "this lane is monitoring context only."
                )
            else:
                no_signal_reason = str(
                    row.get("why") or "No candidate cleared the latest scanner and gate evaluation."
                )

            symbol = str(row.get("symbol") or "unknown")
            lanes.append(
                {
                    "lane_id": f"delta_scalper_{symbol.lower()}",
                    "strategy_id": row.get("strategy_id"),
                    "symbol": symbol,
                    "exchange": row.get("exchange") or "delta_india",
                    "timeframe": row.get("timeframe"),
                    "state": row.get("state") or "WAITING",
                    "active_regime": latest_eval.get("active_regime"),
                    "l2_status": l2.get("status") or "unavailable",
                    "evaluations": int(row.get("evaluations") or 0),
                    "latest_candidates": latest_candidates,
                    "latest_accepted": latest_accepted,
                    "alerts_journaled": int(row.get("alerts") or 0),
                    "forward_outcomes": int(forward.get("completed_alerts") or 0),
                    "last_eval_ts": row.get("latest_eval_ts"),
                    "pipeline_duration_us": latest_eval.get("pipeline_duration_us"),
                    "pipeline_trace": pipeline_trace,
                    "why_no_signal": no_signal_reason,
                    "can_trade": False,
                }
            )

        total_evaluations = sum(lane["evaluations"] for lane in lanes)
        latest_candidates = sum(lane["latest_candidates"] for lane in lanes)
        latest_accepted = sum(lane["latest_accepted"] for lane in lanes)
        alerts_journaled = sum(lane["alerts_journaled"] for lane in lanes)
        forward_outcomes = sum(lane["forward_outcomes"] for lane in lanes)
        scanner_count = len(enabled_scanners)
        funnel_blocker = (
            "No primary scanners enabled; gates received zero candidates. "
            "This is an intentional safety state, not a processing failure."
            if scanner_count == 0
            else "No candidate cleared the latest scanner and gate evaluation."
        )
        signal_funnel = {
            "scope": "latest evaluation for candidate and gate counts; cumulative for all other counts",
            "blocker": funnel_blocker,
            "stages": [
                {
                    "id": "evaluations",
                    "label": "Context evaluations",
                    "count": total_evaluations,
                    "state": "ACTIVE" if total_evaluations else "WAITING",
                },
                {
                    "id": "scanners",
                    "label": "Enabled scanners",
                    "count": scanner_count,
                    "state": "ACTIVE" if scanner_count else "BLOCKED",
                },
                {
                    "id": "candidates",
                    "label": "Latest candidates",
                    "count": latest_candidates,
                    "state": "ACTIVE" if latest_candidates else "NOT_REACHED",
                },
                {
                    "id": "gates",
                    "label": "Gate accepted",
                    "count": latest_accepted,
                    "state": "ACTIVE" if latest_accepted else "NOT_REACHED",
                },
                {
                    "id": "journal",
                    "label": "Journaled alerts",
                    "count": alerts_journaled,
                    "state": "ACTIVE" if alerts_journaled else "WAITING",
                },
                {
                    "id": "outcomes",
                    "label": "Forward outcomes",
                    "count": forward_outcomes,
                    "state": "ACTIVE" if forward_outcomes else "WAITING",
                },
            ],
        }

        latest_eval_times = [
            str(lane["last_eval_ts"]) for lane in lanes if lane.get("last_eval_ts")
        ]
        latest_eval_ts = max(latest_eval_times, default=None)
        journal_states = [
            row.get("latest_eval", {}).get("journal_write_success")
            for row in rows
            if isinstance(row.get("latest_eval"), dict)
        ]
        if journal_states and all(value is True for value in journal_states):
            journal_status = "healthy"
        elif any(value is False for value in journal_states):
            journal_status = "attention"
        else:
            journal_status = "unavailable"

        # The current candle snapshot intentionally does not publish prices or
        # the mechanical MultiTFState object. Expose that absence explicitly so
        # the dashboard cannot substitute placeholders that look like live
        # structure. These fields become populated only when the publisher
        # gains a point-in-time state contract.
        multi_tf_state = {
            str(row.get("symbol") or "unknown"): {
                "available": False,
                "snapshot_ts": row.get("latest_eval_ts"),
                "timeframes": {
                    timeframe: {"status": "not_published"}
                    for timeframe in ("4h", "1h", "15m", "5m", "1m")
                },
                "stack_aligned": None,
                "reason": (
                    "The live sidecar publishes closed-candle regime context, "
                    "not a mechanical MultiTFState snapshot."
                ),
            }
            for row in rows
        }
        recent_decisions = [
            {
                "timestamp": lane.get("last_eval_ts"),
                "symbol": lane.get("symbol"),
                "decision": "MONITOR_ONLY" if scanner_count == 0 else "NO_SELECTION",
                "candidates": lane.get("latest_candidates", 0),
                "accepted": lane.get("latest_accepted", 0),
                "journal_status": journal_status,
                "reason": lane.get("why_no_signal"),
            }
            for lane in lanes
        ]
        observations = {
            "active": [],
            "pending": [],
            "count": 0,
            "status": "none",
            "reason": (
                "No primary scanner is enabled, so no tradeable scanner observation "
                "can be opened. Event research observations are reported separately."
                if scanner_count == 0
                else "No active or pending observation is present in the snapshot."
            ),
        }
        system_health = {
            "snapshot_available": bool(payload.get("generated_at") and rows),
            "snapshot_generated_at": payload.get("generated_at"),
            "connected_symbols": len(rows),
            "expected_symbols": ["BTCUSD", "ETHUSD"],
            "l2_fresh": sum(lane.get("l2_status") == "fresh" for lane in lanes),
            "l2_total": len(lanes),
            "l2_age_ms": None,
            "feed_lag_ms": {timeframe: None for timeframe in ("1m", "5m", "1h")},
            "feed_lag_note": "per-timeframe exchange-to-receive lag is not published",
            "gap_guard": "not_published_in_candle_snapshot",
            "journal": journal_status,
            "last_evaluation_ts": latest_eval_ts,
        }

        return JSONResponse(
            {
                "generated_at": payload.get("generated_at"),
                "rows": rows,
                "lanes": lanes,
                "signal_funnel": signal_funnel,
                "system_health": system_health,
                "multi_tf_state": multi_tf_state,
                "observations": observations,
                "recent_decisions": recent_decisions,
                "panels": embedded_panels,
                "identity": {
                    "product": "VNEDGE Delta India Research Laboratory",
                    "runtime": payload.get("mode") or "delta_scalper_research_shadow",
                    "primary_symbols": ["BTCUSD", "ETHUSD"],
                    "validated_after_cost_edge": False,
                    "active_research_direction": "event_time_data_integrity_and_replay",
                },
                "scanner_status": {
                    "enabled": enabled_scanners,
                    "enabled_count": len(enabled_scanners),
                    "state": scanner_state,
                },
                "policy": {
                    "research_only": True,
                    "l2_is_confirmation_only": True,
                    "paper_trading": False,
                    "live_trading": False,
                    "validated_edge": False,
                    "order_route": "absent",
                    "broker": "absent",
                    "can_trade": False,
                    "can_promote": False,
                },
                "can_trade": False,
                "can_promote": False,
            },
            headers=_identity(user),
        )

    @app.get("/indicator-score-calibration")
    async def indicator_score_calibration(request: Request) -> JSONResponse:
        """Read-only score/outcome attribution; never an execution gate."""

        user = _authorized(request)
        payload = _read_json_payload(
            indicator_score_calibration_file,
            {
                "schema_version": "vnedge.indicator_score_calibration.v1",
                "verdict": "NOT_RUN",
                "source": {"trades": 0},
                "deciles": [],
                "policy": {},
            },
        )
        policy = payload.get("policy") if isinstance(payload.get("policy"), dict) else {}
        return JSONResponse(
            {
                **payload,
                "policy": {
                    **policy,
                    "advisory_only": True,
                    "used_for_signal": False,
                    "used_for_execution": False,
                    "can_trade": False,
                    "can_promote": False,
                },
                "can_trade": False,
                "can_promote": False,
            },
            headers=_identity(user),
        )

    @app.get("/event-research-infrastructure")
    async def event_research_infrastructure(request: Request) -> JSONResponse:
        """Installed-vs-running truth for event recorder, trigger, absorption and replay."""

        user = _authorized(request)
        return JSONResponse(
            event_research_infrastructure_payload(
                event_root=delta_event_root_dir,
                event_trigger_telemetry_path=event_trigger_telemetry_file,
                absorption_dashboard_path=absorption_dashboard_file,
                event_replay_dir=event_replay_output_dir,
                replay_determinism_proof_path=replay_determinism_proof_file,
                event_continuity_path=event_continuity_file,
                kronos_matrix_path=kronos_matrix_file,
                kronos_confirmation_path=kronos_confirmation_file,
                forced_flow_dir=forced_flow_output_dir,
                tv_rule_adapter_path=tv_rule_spec_file,
                htf_structure_path=htf_structure_file,
                htf_structure_v2_path=htf_structure_v2_file,
                failed_auction_readiness_path=failed_auction_readiness_file,
            ),
            headers=_identity(user),
        )

    @app.get("/api/delta/health/stream")
    async def delta_health_stream(request: Request) -> StreamingResponse:
        """Compact, authenticated SSE projection of Delta research health."""

        user = _authorized(request)

        def snapshot_supplier() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
            return (
                _read_json_payload(delta_scalper_file, {}),
                _read_json_payload(delta_event_root_dir / "_recorder_status.json", {}),
                _read_json_payload(event_trigger_telemetry_file, {}),
            )

        return StreamingResponse(
            health_event_generator(request, snapshot_supplier),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-store",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
                **_identity(user),
            },
        )

    @app.get("/delta-5m-event-clock")
    async def delta_5m_event_clock(request: Request) -> JSONResponse:
        """Delta India 5-minute UP/DOWN prep clock.

        Research-only and closed-candle-only: it tells the operator when the
        next Delta 5m decision window opens and whether a paper perp route
        clears the fee wall. It has no live-order authority.
        """
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                delta_5m_event_clock_file,
                {
                    "summary": {},
                    "rows": [],
                    "operator_answer": "Delta 5m event clock unavailable",
                    "mode": "read_only_delta_5m_up_down_perp_proxy",
                    "can_trade": False,
                    "can_promote": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/lane-firing-causality")
    async def lane_firing_causality(request: Request) -> JSONResponse:
        """Joined lane truth: live scanner cause, risk/execution route, and
        paper promotion state. Read-only and non-promoting."""
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                lane_firing_causality_file,
                {
                    "summary": {},
                    "promotion_board": {},
                    "rows": [],
                    "operator_answer": "lane firing causality report unavailable",
                    "can_trade": False,
                    "can_promote": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/pine-research/kb")
    async def pine_research_kb(request: Request) -> JSONResponse:
        """Public-script review KB.

        Read-only, dashboard-token gated, and explicitly research-only. This
        endpoint can be backed by a generated artifact once the crawler/review
        pipeline publishes it; until then it serves a conservative seed.
        """
        user = _authorized(request)
        return JSONResponse(
            load_pine_research_payload(pine_research_path),
            headers=_identity(user),
        )

    @app.get("/quantified-strategy-lab/kb")
    async def quantified_strategy_lab_kb(request: Request) -> JSONResponse:
        """Title-only 95-strategy inventory triage.

        This endpoint deliberately carries no executable strategy rules. It
        groups the 95 titles into VNEDGE-owned research hypotheses and replay
        queues while preserving the no-copy/no-promotion boundary.
        """
        user = _authorized(request)
        return JSONResponse(
            load_quantified_strategy_lab_payload(quantified_strategy_lab_file),
            headers=_identity(user),
        )

    @app.get("/quantified-strategy-lab/port-factory")
    async def quantified_strategy_lab_port_factory(request: Request) -> JSONResponse:
        """Agent-ready VNEDGE port tasks derived from the title inventory."""
        user = _authorized(request)
        return JSONResponse(
            load_quantified_port_factory_payload(quantified_port_factory_file),
            headers=_identity(user),
        )

    @app.get("/quantified-strategy-lab/blueprint-proof")
    async def quantified_strategy_lab_blueprint_proof(request: Request) -> JSONResponse:
        """Research-only proof matrix for every Quantified blueprint."""
        user = _authorized(request)
        return JSONResponse(
            load_quantified_blueprint_proof_payload(quantified_blueprint_proof_file),
            headers=_identity(user),
        )

    @app.get("/quantified-strategy-lab/proof-arbiter")
    async def quantified_strategy_lab_proof_arbiter(request: Request) -> JSONResponse:
        """Research-only next-action arbiter for Quantified proof cells."""
        user = _authorized(request)
        return JSONResponse(
            load_quantified_proof_result_arbiter_payload(quantified_proof_arbiter_file),
            headers=_identity(user),
        )

    @app.get("/quantified-strategy-lab/pullback-proof")
    async def quantified_strategy_lab_pullback_proof(request: Request) -> JSONResponse:
        """Research-only proof queue for the first Quantified pullback port."""
        user = _authorized(request)
        return JSONResponse(
            load_quantified_pullback_reversion_proof_payload(quantified_pullback_proof_file),
            headers=_identity(user),
        )

    @app.get("/pine-research/distiller")
    async def pine_alpha_distiller(request: Request) -> JSONResponse:
        """Source-backed Pine primitive/task distillation artifact."""
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                pine_alpha_distiller_file,
                {
                    "distiller_id": "pine_alpha_distiller_v1",
                    "summary": {},
                    "primitive_families": [],
                    "port_tasks": [],
                    "script_distillations": [],
                    "operator_answer": "pine alpha distiller artifact unavailable",
                    "can_trade": False,
                    "can_promote": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/pine-research/rule-spec")
    async def tv_rule_spec(request: Request) -> JSONResponse:
        """Local Pine RuleSpec compiler/evaluator status.

        The artifact is source-hash-bound, research-only, and built exclusively from
        operator-supplied/open-source Pine plus local closed-candle data. It
        never proxies TradingView or the unofficial tvscreener endpoints.
        """
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                tv_rule_spec_file,
                {
                    "adapter_id": "tv_rule_adapter_v1",
                    "status": "READY_NO_RULE_COMPILED",
                    "summary": {},
                    "rule_spec": None,
                    "evaluation": None,
                    "policy": {
                        "network_access": False,
                        "tradingview_data_used": False,
                        "unofficial_tvscreener_dependency": False,
                        "local_closed_candles_only": True,
                        "raw_source_emitted": False,
                        "normalized_rule_expressions_emitted": True,
                        "research_only": True,
                    },
                    "operator_answer": (
                        "Local RuleSpec adapter is available; compile a lawful Pine source "
                        "artifact before replay."
                    ),
                    "can_trade": False,
                    "can_promote": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/pine-research/progress")
    async def pine_backtest_progress(request: Request) -> JSONResponse:
        """Live scanner tournament/backtest progress.

        This is operational visibility only: it reports the in-flight research
        worker heartbeat and never grants trade or promotion permission.
        """
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                pine_backtest_progress_file,
                {
                    "truth_layer": "scanner_tournament_progress_v1",
                    "status": "idle",
                    "phase": "no_progress_artifact",
                    "started_at": None,
                    "heartbeat_at": None,
                    "completed_at": None,
                    "profile": None,
                    "lookback_days": None,
                    "target_count": 0,
                    "strategy_count": 0,
                    "total_work_units": 0,
                    "completed_work_units": 0,
                    "progress_pct": 0.0,
                    "current_target": None,
                    "current_strategy": None,
                    "current_rows": None,
                    "current_routes": None,
                    "output_path": None,
                    "last_error": None,
                    "can_trade": False,
                    "can_promote": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/pine-research/uplift-agent")
    async def pine_edge_uplift_agent(request: Request) -> JSONResponse:
        """Agentic failure-salvage and edge-uplift artifact."""
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                pine_edge_uplift_file,
                {
                    "agent_id": "pine_edge_uplift_agent_v1",
                    "summary": {},
                    "failure_clusters": [],
                    "top_uplifts": [],
                    "experiments": [],
                    "operator_answer": "pine edge uplift agent artifact unavailable",
                    "can_trade": False,
                    "can_promote": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/pine-research/uplift-executor")
    async def edge_uplift_executor(request: Request) -> JSONResponse:
        """Replay/port task queue produced from the edge-uplift agent."""
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                edge_uplift_executor_file,
                {
                    "executor_id": "edge_uplift_executor_v1",
                    "summary": {},
                    "port_pack": [],
                    "tasks": [],
                    "operator_answer": "edge uplift executor artifact unavailable",
                    "can_trade": False,
                    "can_promote": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/pine-research/scanner-uplift")
    async def scanner_backtest_uplift(request: Request) -> JSONResponse:
        """Backtest-failure classifications and scanner uplift experiments."""
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                scanner_backtest_uplift_file,
                {
                    "agent_id": "scanner_backtest_uplift_v1",
                    "summary": {},
                    "top_uplifts": [],
                    "experiments": [],
                    "operator_answer": "scanner backtest uplift artifact unavailable",
                    "can_trade": False,
                    "can_promote": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/pine-research/alpha-arena-lite")
    async def alpha_arena_lite(request: Request) -> JSONResponse:
        """Durable Arena task/scorecard layer for scanner uplift candidates."""
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                alpha_arena_lite_file,
                {
                    "arena_id": "alpha_arena_lite_v1",
                    "summary": {},
                    "scorecards": [],
                    "gateway": {},
                    "operator_answer": "alpha arena lite artifact unavailable",
                    "can_trade": False,
                    "can_promote": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/pine-research/quant-loop-governance")
    async def quant_loop_governance(request: Request) -> JSONResponse:
        """Research-loop readiness, collision, and budget governance."""
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                quant_loop_governance_file,
                {
                    "governance_id": "quant_loop_governance_v1",
                    "summary": {},
                    "gate_checks": [],
                    "loop_cards": [],
                    "candidate_locks": [],
                    "collisions": [],
                    "budget_alerts": [],
                    "operator_answer": "quant loop governance artifact unavailable",
                    "can_trade": False,
                    "can_promote": False,
                    "live_orders_enabled": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/pine-research/evidence-index")
    async def pine_research_evidence_index(request: Request) -> JSONResponse:
        """Unified research evidence index across Pine/scanner/arena artifacts."""
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                evidence_index_file,
                {
                    "evidence_store_id": "research_evidence_index_v1",
                    "summary": {},
                    "records": [],
                    "top_positive": [],
                    "fee_wall_breakers": [],
                    "sparse_positives": [],
                    "failure_clusters": [],
                    "operator_answer": "research evidence index artifact unavailable",
                    "can_trade": False,
                    "can_promote": False,
                    "live_orders_enabled": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/pine-research/execution-profile")
    async def pine_research_execution_profile(request: Request) -> JSONResponse:
        """Execution-realistic replay profile for research evidence rows."""
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                execution_replay_profile_file,
                {
                    "execution_profile_id": "execution_realistic_replay_profile_v1",
                    "summary": {},
                    "profiles": [],
                    "settlement_logic_evaluation": {"components": []},
                    "rows": [],
                    "execution_ready_rows": [],
                    "paper_blocked_rows": [],
                    "operator_answer": "execution replay profile artifact unavailable",
                    "can_trade": False,
                    "can_promote": False,
                    "live_orders_enabled": False,
                },
            ),
            headers=_identity(user),
        )

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        result = store.authenticate(websocket.query_params.get("token", ""))
        if not result.authorized:
            await websocket.close(
                code=4401, reason=(result.reason or "missing or invalid token")[:120]
            )
            return
        name = result.name or "?"
        await websocket.accept()
        ws_connections[name] = ws_connections.get(name, 0) + 1
        logger.info("dashboard ws connected: user=%s role=%s", name, result.role)
        try:
            while True:
                if result.expires_at is not None and (datetime.now(UTC) >= result.expires_at):
                    # A token that expires mid-session loses the stream too.
                    await websocket.close(code=4401, reason="token expired")
                    return
                snapshot = provider.latest()
                if snapshot is not None:
                    await websocket.send_json(
                        # Who's connected: count only — names and tokens are
                        # never serialized into the snapshot.
                        {**snapshot, "dashboard_connections": sum(ws_connections.values())}
                    )
                await asyncio.sleep(1.0 / snapshot_hz)
        except (WebSocketDisconnect, ConnectionError):
            return  # dropped client: deregistered by scope exit, bot unaffected
        except Exception as exc:  # noqa: BLE001 — UI must never propagate upward
            logger.warning("dashboard websocket dropped: %s", exc)
            return
        finally:
            remaining = ws_connections.get(name, 1) - 1
            if remaining <= 0:
                ws_connections.pop(name, None)
            else:
                ws_connections[name] = remaining
            logger.info("dashboard ws disconnected: user=%s", name)

    # v2 React frontend (frontend/dist), served at /app as a static SPA. Mounted
    # ONLY when a build exists — so a production image without the build simply
    # has no /app route (never a 500), and the classic dashboard at / is
    # unaffected. The SPA shell is public like the classic shell; its data calls
    # (/state, /journal, /whoami) stay token-gated. Build: `npm --prefix
    # frontend install && npm --prefix frontend run build`.
    # Resolve the built SPA across both layouts, like the runbooks doc above:
    # dev (repo checkout → _REPO_ROOT/frontend/dist) and the container (vnedge is
    # pip-installed into site-packages, so _REPO_ROOT points there; the build is
    # COPYed to /app/frontend/dist == cwd/frontend/dist).
    if v2_dist_path is not None:
        v2_candidates = [Path(v2_dist_path)]
    else:
        v2_candidates = [Path.cwd() / "frontend" / "dist", _REPO_ROOT / "frontend" / "dist"]
    v2_dist = next((c for c in v2_candidates if c.is_dir()), None)
    if v2_dist is not None:
        from starlette.staticfiles import StaticFiles

        app.mount("/app", StaticFiles(directory=str(v2_dist), html=True), name="v2")
        logger.info("v2 frontend mounted at /app from %s", v2_dist)

    return app
