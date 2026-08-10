"""Durable research task/event/artifact registry for Quant OS agents.

This is the Agent Gateway v2 control-plane ledger. It is deliberately
research-only: tasks may coordinate analysis work, stream progress events, and
publish hashed artifacts, but they cannot trade, promote, or mutate live
runtime state.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TASK_QUEUED = "QUEUED_RESEARCH_ONLY"
TASK_RUNNING = "RUNNING_RESEARCH_ONLY"
TASK_COMPLETED = "COMPLETED_RESEARCH_ONLY"
TASK_FAILED = "FAILED_RESEARCH_ONLY"
TASK_CANCELLED = "CANCELLED_RESEARCH_ONLY"
TASK_TERMINAL_STATUSES = frozenset({TASK_COMPLETED, TASK_FAILED, TASK_CANCELLED})

DEFAULT_AGENT_TASK_DIR = Path("logs/agent_gateway/quant_os")


def env_quant_os_agent_gateway_dir(env: dict[str, str] | None = None) -> Path:
    source = os.environ if env is None else env
    return Path(source.get("QUANT_OS_AGENT_GATEWAY_DIR", str(DEFAULT_AGENT_TASK_DIR)))


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def _research_only(payload: dict[str, Any]) -> dict[str, Any]:
    payload["can_trade"] = False
    payload["can_promote"] = False
    payload["live_orders_enabled"] = False
    return payload


def _json_default(value: Any) -> str:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _coerce_json_object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _clamp_priority(value: int | float) -> int:
    return max(0, min(100, int(value)))


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True, default=_json_default) + "\n")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=_json_default),
        encoding="utf-8",
    )
    tmp.replace(path)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class QuantAgentTask:
    task_id: str
    kind: str
    objective: str
    status: str = TASK_QUEUED
    priority: int = 50
    requested_by: str = "operator"
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    target: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)
    lease_owner: str | None = None
    artifact_ids: tuple[str, ...] = ()
    last_event: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _research_only(asdict(self))


@dataclass(frozen=True)
class QuantAgentEvent:
    event_id: str
    task_id: str
    event_type: str
    message: str
    level: str = "info"
    created_at: str = field(default_factory=_now_iso)
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _research_only(asdict(self))


@dataclass(frozen=True)
class QuantAgentArtifact:
    artifact_id: str
    task_id: str
    artifact_type: str
    summary: str
    path: str
    sha256: str
    bytes: int
    created_at: str = field(default_factory=_now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _research_only(asdict(self))


class QuantOSAgentGateway:
    """Append-only Quant OS task registry with replayable snapshots."""

    def __init__(self, root: Path | str = DEFAULT_AGENT_TASK_DIR) -> None:
        self.root = Path(root)
        self.tasks_path = self.root / "tasks.jsonl"
        self.events_path = self.root / "events.jsonl"
        self.artifacts_path = self.root / "artifacts.jsonl"
        self.snapshot_path = self.root / "snapshot.json"
        self.artifact_root = self.root / "artifacts"

    def create_task(
        self,
        *,
        kind: str,
        objective: str,
        requested_by: str,
        priority: int = 50,
        target: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = _now_iso()
        task = QuantAgentTask(
            task_id=_new_id("qtask"),
            kind=kind,
            objective=objective,
            priority=_clamp_priority(priority),
            requested_by=requested_by,
            created_at=now,
            updated_at=now,
            target=_coerce_json_object(target),
            payload=_coerce_json_object(payload),
        )
        task_doc = task.to_dict()
        _append_jsonl(self.tasks_path, task_doc)
        self.emit_event(
            task.task_id,
            event_type="TASK_CREATED",
            message=f"{kind}: {objective}",
            level="info",
            payload={"priority": task.priority, "requested_by": requested_by},
            update_task=False,
        )
        self.write_snapshot()
        return self.read_task(task.task_id) or task_doc

    def read_task(self, task_id: str) -> dict[str, Any] | None:
        return self._replay()[0].get(task_id)

    def start_task(self, task_id: str, *, lease_owner: str) -> dict[str, Any]:
        return self.update_task(
            task_id,
            status=TASK_RUNNING,
            lease_owner=lease_owner,
            event_type="TASK_STARTED",
            message=f"task claimed by {lease_owner}",
        )

    def complete_task(
        self,
        task_id: str,
        *,
        message: str = "task completed",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.update_task(
            task_id,
            status=TASK_COMPLETED,
            event_type="TASK_COMPLETED",
            message=message,
            event_payload=payload,
        )

    def fail_task(
        self,
        task_id: str,
        *,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.update_task(
            task_id,
            status=TASK_FAILED,
            event_type="TASK_FAILED",
            message=message,
            level="error",
            event_payload=payload,
        )

    def update_task(
        self,
        task_id: str,
        *,
        status: str | None = None,
        lease_owner: str | None = None,
        event_type: str | None = None,
        message: str | None = None,
        level: str = "info",
        event_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = self.read_task(task_id)
        if current is None:
            raise KeyError(f"unknown Quant OS task: {task_id}")
        updated = dict(current)
        if status is not None:
            updated["status"] = status
        if lease_owner is not None:
            updated["lease_owner"] = lease_owner
        updated["updated_at"] = _now_iso()
        _append_jsonl(self.tasks_path, _research_only(updated))
        if event_type is not None:
            self.emit_event(
                task_id,
                event_type=event_type,
                message=message or event_type,
                level=level,
                payload=event_payload,
                update_task=False,
            )
        self.write_snapshot()
        return self.read_task(task_id) or updated

    def emit_event(
        self,
        task_id: str,
        *,
        event_type: str,
        message: str,
        level: str = "info",
        payload: dict[str, Any] | None = None,
        update_task: bool = True,
    ) -> dict[str, Any]:
        if update_task and self.read_task(task_id) is None:
            raise KeyError(f"unknown Quant OS task: {task_id}")
        event = QuantAgentEvent(
            event_id=_new_id("qevt"),
            task_id=task_id,
            event_type=event_type,
            message=message,
            level=level,
            payload=_coerce_json_object(payload),
        ).to_dict()
        _append_jsonl(self.events_path, event)
        if update_task:
            current = self.read_task(task_id)
            if current is not None:
                updated = {**current, "last_event": message, "updated_at": event["created_at"]}
                _append_jsonl(self.tasks_path, _research_only(updated))
        self.write_snapshot()
        return event

    def register_content_artifact(
        self,
        task_id: str,
        *,
        artifact_type: str,
        summary: str,
        content: str | dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.read_task(task_id) is None:
            raise KeyError(f"unknown Quant OS task: {task_id}")
        artifact_id = _new_id("qart")
        artifact_dir = self.artifact_root / task_id
        suffix = ".json" if isinstance(content, dict) else ".txt"
        artifact_path = artifact_dir / f"{artifact_id}{suffix}"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        if isinstance(content, dict):
            artifact_path.write_text(
                json.dumps(content, indent=2, sort_keys=True, default=_json_default),
                encoding="utf-8",
            )
        else:
            artifact_path.write_text(str(content), encoding="utf-8")
        return self.register_artifact(
            task_id,
            artifact_path=artifact_path,
            artifact_type=artifact_type,
            summary=summary,
            metadata=metadata,
            artifact_id=artifact_id,
        )

    def register_artifact(
        self,
        task_id: str,
        *,
        artifact_path: Path | str,
        artifact_type: str,
        summary: str,
        metadata: dict[str, Any] | None = None,
        artifact_id: str | None = None,
    ) -> dict[str, Any]:
        current = self.read_task(task_id)
        if current is None:
            raise KeyError(f"unknown Quant OS task: {task_id}")
        path = Path(artifact_path)
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(str(path))
        resolved = path.resolve()
        root = self.root.resolve()
        if not _is_relative_to(resolved, root):
            raise ValueError("artifact path must be inside the Quant OS gateway directory")
        data = resolved.read_bytes()
        artifact = QuantAgentArtifact(
            artifact_id=artifact_id or _new_id("qart"),
            task_id=task_id,
            artifact_type=artifact_type,
            summary=summary,
            path=str(resolved),
            sha256=hashlib.sha256(data).hexdigest(),
            bytes=len(data),
            metadata=_coerce_json_object(metadata),
        ).to_dict()
        _append_jsonl(self.artifacts_path, artifact)
        artifact_ids = tuple(current.get("artifact_ids") or ()) + (artifact["artifact_id"],)
        updated = {
            **current,
            "artifact_ids": artifact_ids,
            "updated_at": artifact["created_at"],
            "last_event": f"artifact {artifact_type}: {summary}",
        }
        _append_jsonl(self.tasks_path, _research_only(updated))
        self.emit_event(
            task_id,
            event_type="ARTIFACT_REGISTERED",
            message=f"{artifact_type}: {summary}",
            level="info",
            payload={
                "artifact_id": artifact["artifact_id"],
                "sha256": artifact["sha256"],
                "bytes": artifact["bytes"],
            },
            update_task=False,
        )
        self.write_snapshot()
        return artifact

    def snapshot(self, *, limit: int = 100) -> dict[str, Any]:
        tasks, events, artifacts = self._replay()
        ordered_tasks = sorted(
            tasks.values(),
            key=lambda row: str(row.get("updated_at") or row.get("created_at") or ""),
            reverse=True,
        )
        ordered_artifacts = sorted(
            artifacts,
            key=lambda row: str(row.get("created_at") or ""),
            reverse=True,
        )
        recent_events = events[-limit:]
        status_counts = Counter(str(task.get("status") or "UNKNOWN") for task in tasks.values())
        artifact_kind_counts = Counter(
            str(artifact.get("artifact_type") or "unknown") for artifact in artifacts
        )
        queued = status_counts.get(TASK_QUEUED, 0)
        running = status_counts.get(TASK_RUNNING, 0)
        completed = status_counts.get(TASK_COMPLETED, 0)
        failed = status_counts.get(TASK_FAILED, 0)
        cancelled = status_counts.get(TASK_CANCELLED, 0)
        active = queued + running
        payload = {
            "gateway_id": "quant_os_agent_gateway_v2",
            "version": 2,
            "generated_at": _now_iso(),
            "root": str(self.root),
            "summary": {
                "total_tasks": len(tasks),
                "queued": queued,
                "running": running,
                "completed": completed,
                "failed": failed,
                "cancelled": cancelled,
                "terminal": completed + failed + cancelled,
                "active": active,
                "events": len(events),
                "artifacts": len(artifacts),
                "artifact_kinds": dict(sorted(artifact_kind_counts.items())),
            },
            "tasks": ordered_tasks[:limit],
            "events": {
                "count": len(events),
                "recent": recent_events,
            },
            "artifacts": {
                "count": len(artifacts),
                "recent": ordered_artifacts[:limit],
            },
            "policy": {
                "surface": "research_only_control_plane",
                "orders_allowed": False,
                "promotion_allowed": False,
                "requires_human_promotion": True,
                "requires_untouched_judgment": True,
            },
            "alpha_arena_lite": {
                "foundation_ready": True,
                "inputs": [
                    "durable research tasks",
                    "append-only event feed",
                    "sha256 artifact registry",
                ],
                "next_use": (
                    "score experiments by evidence quality and queue only causal "
                    "ports/replays into the existing promotion ladder"
                ),
            },
            "can_trade": False,
            "can_promote": False,
            "live_orders_enabled": False,
        }
        return payload

    def write_snapshot(self) -> dict[str, Any]:
        payload = self.snapshot()
        _atomic_json(self.snapshot_path, payload)
        return payload

    def _replay(
        self,
    ) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        tasks: dict[str, dict[str, Any]] = {}
        for row in _read_jsonl(self.tasks_path):
            task_id = str(row.get("task_id") or "")
            if task_id:
                tasks[task_id] = _research_only(dict(row))
        events = [_research_only(dict(row)) for row in _read_jsonl(self.events_path)]
        artifacts = [_research_only(dict(row)) for row in _read_jsonl(self.artifacts_path)]
        return tasks, events, artifacts


def quant_os_event_stream(snapshot: dict[str, Any]) -> list[str]:
    """Render recent gateway events as finite Server-Sent Event frames."""
    events = (snapshot.get("events") or {}).get("recent") or []
    frames: list[str] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        event_id = str(event.get("event_id") or "")
        event_type = str(event.get("event_type") or "message")
        data = json.dumps(event, sort_keys=True, default=_json_default)
        frames.append(f"id: {event_id}\nevent: {event_type}\ndata: {data}\n\n")
    heartbeat = {
        "gateway_id": snapshot.get("gateway_id"),
        "generated_at": snapshot.get("generated_at"),
        "summary": snapshot.get("summary"),
        "can_trade": False,
        "can_promote": False,
    }
    frames.append(f"event: heartbeat\ndata: {json.dumps(heartbeat, sort_keys=True)}\n\n")
    return frames
