"""Data quality gate — nothing reaches storage or strategies without passing.

The gate is strict by design (see docs/DESIGN.md §1): a dataset either passes
every check or it is rejected with an explicit issue list. Gaps are the one
negotiable check — historical altcoin data legitimately has holes — and even
then only via an explicit ``allow_gaps=True`` from the caller, never silently.

Every validation produces a QualityReport that is persisted as JSON under
data/reports/data_quality/, so "why was this dataset rejected" is always
answerable after the fact.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from vnedge.data.schemas import (
    CANDLE_COLUMNS,
    FUNDING_COLUMNS,
    TIMEFRAME_MS,
)

# Funding beyond ±5% per interval is data corruption, not a market condition
# (venue caps are well inside this: Binance ±0.75% for majors, ±2-3% tails).
MAX_ABS_FUNDING_RATE_SANITY = 0.05


@dataclass(frozen=True)
class QualityIssue:
    check: str
    detail: str


@dataclass(frozen=True)
class QualityReport:
    dataset: str
    passed: bool
    row_count: int
    gap_count: int = 0
    issues: tuple[QualityIssue, ...] = field(default_factory=tuple)

    @property
    def summary(self) -> str:
        if self.passed:
            return f"{self.dataset}: PASSED ({self.row_count} rows, {self.gap_count} gaps)"
        return f"{self.dataset}: REJECTED — " + "; ".join(
            f"{i.check}: {i.detail}" for i in self.issues
        )

    def to_dict(self) -> dict:
        return asdict(self)


def _base_checks(
    df: pd.DataFrame, required_columns: list[str], issues: list[QualityIssue]
) -> bool:
    """Structural checks. Returns False when further checks are meaningless."""
    if df.empty:
        issues.append(QualityIssue("empty", "dataset has no rows"))
        return False
    missing = [c for c in required_columns if c not in df.columns]
    if missing:
        issues.append(QualityIssue("schema", f"missing columns: {missing}"))
        return False

    null_counts = df[required_columns].isna().sum()
    for col, count in null_counts.items():
        if count:
            issues.append(QualityIssue("nulls", f"{count} null values in '{col}'"))

    dup_count = int(df["timestamp"].duplicated().sum())
    if dup_count:
        issues.append(QualityIssue("duplicates", f"{dup_count} duplicate timestamps"))
    if not df["timestamp"].is_monotonic_increasing:
        issues.append(QualityIssue("monotonic", "timestamps are not sorted ascending"))
    return True


def _count_gaps(df: pd.DataFrame, timeframe: str) -> int:
    step_ms = TIMEFRAME_MS[timeframe]
    diffs_ms = df["timestamp"].diff().dropna().dt.total_seconds() * 1000.0
    return int((diffs_ms != step_ms).sum())


def validate_candles(
    df: pd.DataFrame,
    timeframe: str,
    *,
    allow_gaps: bool = False,
    dataset: str = "candles",
) -> QualityReport:
    if timeframe not in TIMEFRAME_MS:
        return QualityReport(
            dataset, False, len(df),
            issues=(QualityIssue("timeframe", f"unknown timeframe '{timeframe}'"),),
        )
    issues: list[QualityIssue] = []
    gap_count = 0
    if _base_checks(df, CANDLE_COLUMNS, issues):
        price_cols = ["open", "high", "low", "close"]
        if (df[price_cols] <= 0).any().any():
            issues.append(QualityIssue("prices", "non-positive price values"))
        if (df["volume"] < 0).any():
            issues.append(QualityIssue("volume", "negative volume values"))
        bad_range = (
            (df["high"] < df["low"])
            | (df["high"] < df[["open", "close"]].max(axis=1))
            | (df["low"] > df[["open", "close"]].min(axis=1))
        )
        if bad_range.any():
            issues.append(
                QualityIssue("ohlc_range", f"{int(bad_range.sum())} rows violate high>=o,c>=low")
            )
        gap_count = _count_gaps(df, timeframe)
        if gap_count and not allow_gaps:
            issues.append(
                QualityIssue("gaps", f"{gap_count} irregular intervals (allow_gaps=False)")
            )
    return QualityReport(dataset, not issues, len(df), gap_count, tuple(issues))


def validate_funding(df: pd.DataFrame, *, dataset: str = "funding") -> QualityReport:
    """Funding intervals vary by venue/symbol (8h, 4h, 1h), so no gap check —
    only structure and value sanity."""
    issues: list[QualityIssue] = []
    if _base_checks(df, FUNDING_COLUMNS, issues):
        absurd = df["funding_rate"].abs() > MAX_ABS_FUNDING_RATE_SANITY
        if absurd.any():
            issues.append(
                QualityIssue(
                    "funding_sanity",
                    f"{int(absurd.sum())} rows with |rate| > "
                    f"{MAX_ABS_FUNDING_RATE_SANITY:.0%} per interval",
                )
            )
    return QualityReport(dataset, not issues, len(df), issues=tuple(issues))


def validate_open_interest(df: pd.DataFrame, *, dataset: str = "open_interest") -> QualityReport:
    """open_interest_value is optional (NaN allowed); open_interest is not."""
    issues: list[QualityIssue] = []
    if _base_checks(df, ["timestamp", "open_interest"], issues):
        if (df["open_interest"] < 0).any():
            issues.append(QualityIssue("open_interest", "negative open interest values"))
    return QualityReport(dataset, not issues, len(df), issues=tuple(issues))


def write_report(report: QualityReport, reports_dir: Path) -> Path:
    """Persist the report as JSON for the data-quality audit trail."""
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", report.dataset)
    path = reports_dir / f"{slug}_{stamp}.json"
    path.write_text(json.dumps(report.to_dict(), indent=2, default=str))
    return path
