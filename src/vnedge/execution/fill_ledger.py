"""Immutable fill ledger — hash-chained, append-only trade records.

The charter requires complete immutable fill/fee/funding data (tax/compliance
needs it later; Section 194S/TDS logic itself stays out pending CA sign-off —
this records FACTS, not tax interpretations). Each record embeds the previous
record's hash, so any edit, deletion, or reorder breaks the chain and is
detectable by ``verify_chain``. Paper fills flow through the same ledger as
live fills will — one code path, exercised long before real money touches it.

Not a journal replacement: the decision journal is the WAL of *decisions*;
this is the tamper-evident record of *executions*.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

_GENESIS = "0" * 64


def _record_hash(payload: dict, prev_hash: str) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{prev_hash}|{canonical}".encode()).hexdigest()


@dataclass(frozen=True)
class ChainReport:
    ok: bool
    records: int
    first_bad_line: int | None = None  # 1-indexed, None when ok


class FillLedger:
    """Append-only, fsync'd, hash-chained ledger of fills for one lane."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.records = 0
        self._prev_hash = _GENESIS
        self._resume()

    def _resume(self) -> None:
        """Continue an existing chain; refuse silently corrupt tails."""
        if not self.path.exists():
            return
        report = verify_chain(self.path)
        if not report.ok:
            raise ValueError(
                f"fill ledger {self.path} fails chain verification at line "
                f"{report.first_bad_line} — refusing to append to a broken chain"
            )
        self.records = report.records
        if report.records:
            with self.path.open() as fh:
                last = None
                for line in fh:
                    if line.strip():
                        last = line
            self._prev_hash = json.loads(last)["hash"]

    def append(self, fill: dict) -> str:
        """Append one fill record; returns its chain hash."""
        payload = dict(fill)
        payload["seq"] = self.records
        h = _record_hash(payload, self._prev_hash)
        record = {**payload, "prev_hash": self._prev_hash, "hash": h}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        self._prev_hash = h
        self.records += 1
        return h


def verify_chain(path: str | Path) -> ChainReport:
    """Walk the ledger and verify every link. Any tamper -> first bad line."""
    path = Path(path)
    if not path.exists():
        return ChainReport(ok=True, records=0)
    prev = _GENESIS
    n = 0
    with path.open() as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                claimed = record.pop("hash")
                claimed_prev = record.pop("prev_hash")
            except (ValueError, KeyError):
                return ChainReport(ok=False, records=n, first_bad_line=lineno)
            if claimed_prev != prev or _record_hash(record, prev) != claimed:
                return ChainReport(ok=False, records=n, first_bad_line=lineno)
            prev = claimed
            n += 1
    return ChainReport(ok=True, records=n)
