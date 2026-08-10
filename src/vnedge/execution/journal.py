"""Decision journal — the append-only WAL (docs/DESIGN.md §4).

Every signal, risk decision, intent, submission, ack, and error is written
here BEFORE the next step happens. After a crash, this file is the
deterministic baseline for reconstructing what the bot was doing.

The journal-unavailable rule: if a record cannot be written, the journal
marks itself unavailable and the order manager rejects all risk-increasing
orders (reduce-only exits remain allowed). If we can't record what we're
doing, we don't create new risk.

Writes are line-buffered JSONL with flush+fsync per record — at our order
frequency, durability beats throughput by an enormous margin.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class DecisionJournal:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._available = True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Probe writability at startup, not at first order.
            with open(self.path, "a", encoding="utf-8"):
                pass
        except OSError as exc:
            self._mark_unavailable(f"journal probe failed: {exc}")

    @property
    def available(self) -> bool:
        return self._available

    def _mark_unavailable(self, reason: str) -> None:
        if self._available:
            self._available = False
            logger.critical(
                "DECISION JOURNAL UNAVAILABLE (%s) — new risk-increasing "
                "orders will be rejected until this is resolved", reason,
            )

    def append(self, kind: str, payload: dict[str, Any]) -> bool:
        """Write one record. Returns False (and flips unavailable) on failure —
        never raises into the order path."""
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "kind": kind,
            "payload": payload,
        }
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")
                f.flush()
                os.fsync(f.fileno())
            return True
        except OSError as exc:
            self._mark_unavailable(str(exc))
            return False

    def read_all(self) -> list[dict]:
        """Full journal replay (recovery / tests / audit export)."""
        if not self.path.exists():
            return []
        records = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        return records
