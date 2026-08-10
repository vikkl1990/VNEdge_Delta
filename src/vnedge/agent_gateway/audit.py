"""Append-only audit log for Agent Gateway calls."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any


def _hash_payload(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(encoded).hexdigest()


@dataclass(frozen=True)
class AgentAuditEvent:
    agent: str | None
    token_prefix: str | None
    method: str
    path: str
    action: str
    outcome: str
    scope: str | None = None
    reason: str | None = None
    job_id: str | None = None
    paper_only: bool | None = None


class AgentAuditLogger:
    """Tiny hash-chained JSONL audit log.

    The gateway is a separate surface from the order decision journal, so it
    gets its own append-only trail. The hash chain is intentionally simple: each
    record includes the previous record's hash and the current record hash.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._last_hash = self._load_last_hash(path)

    def write(self, event: AgentAuditEvent) -> dict[str, Any]:
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "agent": event.agent,
            "token_prefix": event.token_prefix,
            "method": event.method,
            "path": event.path,
            "action": event.action,
            "scope": event.scope,
            "outcome": event.outcome,
            "reason": event.reason,
            "job_id": event.job_id,
            "paper_only": event.paper_only,
        }
        with self._lock:
            record["prev_hash"] = self._last_hash
            record["hash"] = _hash_payload(record)
            self._last_hash = str(record["hash"])
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(record, sort_keys=True) + "\n")
        return record

    @staticmethod
    def _load_last_hash(path: Path | None) -> str:
        if path is None or not path.exists():
            return "0" * 64
        last = ""
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    last = line
        except OSError:
            return "0" * 64
        if not last:
            return "0" * 64
        try:
            payload = json.loads(last)
        except json.JSONDecodeError:
            return "0" * 64
        value = payload.get("hash")
        return value if isinstance(value, str) and len(value) == 64 else "0" * 64

