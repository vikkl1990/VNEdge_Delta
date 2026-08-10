"""Operator-action audit log — durable, append-only, hash-chained.

Every OPERATOR action that could touch capital or override a safety gate — kill
switch trip/reset, a live-gate flip, a strategy promotion, a config override —
gets one immutable record here. Each line embeds a SHA-256 of the previous
record, so any edit, deletion, or reorder breaks the chain and is caught by
``verify_chain`` (same discipline as the fill ledger and decision journal).

This is the "who did what, when, and what changed" trail that must exist before
a system trades real money. The kill switch already kept an in-memory history
and left a comment that the persistent ledger arrives "in a later milestone" —
this is that ledger. It is write-only from the app's side; nothing here can
enable trading or bypass a gate — it only records that a human did.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_GENESIS = "0" * 64


def _record_hash(payload: dict, prev_hash: str) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{prev_hash}|{canonical}".encode()).hexdigest()


@dataclass(frozen=True)
class AuditChainReport:
    ok: bool
    lines: int
    first_bad_line: int | None = None


class OperatorAuditLog:
    """Append-only, fsync'd, hash-chained log of operator actions."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._prev_hash = _GENESIS
        self._resume()

    def _resume(self) -> None:
        """Continue an existing chain; refuse to append onto a broken one."""
        if not self.path.exists():
            return
        report = self.verify_chain()
        if not report.ok:
            raise RuntimeError(
                f"operator audit log {self.path} fails chain verification at line "
                f"{report.first_bad_line} — refusing to append to a broken chain"
            )
        last = None
        for line in self.path.read_text().splitlines():
            if line.strip():
                last = line
        if last:
            self._prev_hash = json.loads(last)["hash"]

    def record(
        self,
        *,
        actor: str,
        action: str,
        detail: str = "",
        source: str = "",
        before: object = None,
        after: object = None,
        now: datetime | None = None,
    ) -> str:
        """Append one operator-action record; returns its chain hash.

        actor  — who/what took the action (e.g. "operator", "kill_switch").
        action — the verb (e.g. "kill_switch_activate", "live_gate_flip",
                 "strategy_promote").
        before/after — optional state snapshots for a change.
        """
        payload = {
            "ts": (now or datetime.now(UTC)).isoformat(),
            "actor": actor,
            "action": action,
            "detail": detail,
            "source": source,
        }
        if before is not None:
            payload["before"] = before
        if after is not None:
            payload["after"] = after
        h = _record_hash(payload, self._prev_hash)
        line = json.dumps({**payload, "prev_hash": self._prev_hash, "hash": h})
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        self._prev_hash = h
        return h

    def verify_chain(self) -> AuditChainReport:
        """Recompute every record's hash; the first mismatch is the tamper point."""
        if not self.path.exists():
            return AuditChainReport(ok=True, lines=0)
        prev = _GENESIS
        n = 0
        for i, line in enumerate(self.path.read_text().splitlines(), start=1):
            if not line.strip():
                continue
            n += 1
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                return AuditChainReport(ok=False, lines=n, first_bad_line=i)
            stored = rec.pop("hash", None)
            claimed_prev = rec.pop("prev_hash", None)
            if claimed_prev != prev or _record_hash(rec, prev) != stored:
                return AuditChainReport(ok=False, lines=n, first_bad_line=i)
            prev = stored
        return AuditChainReport(ok=True, lines=n)
