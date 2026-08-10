"""Shared result type for all ingestors."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from vnedge.data.data_quality_gate import QualityReport


@dataclass(frozen=True)
class IngestResult:
    dataset: str
    rows_fetched: int
    persisted: bool
    report: QualityReport
    path: Path | None = None
    rows_added: int = 0

    @property
    def summary(self) -> str:
        if self.persisted:
            return (
                f"{self.dataset}: OK — {self.rows_fetched} fetched, "
                f"{self.rows_added} new rows written to {self.path}"
            )
        return f"{self.dataset}: NOT PERSISTED — {self.report.summary}"
