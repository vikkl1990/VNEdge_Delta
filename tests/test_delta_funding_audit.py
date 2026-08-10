from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from vnedge.research.delta_funding_audit import (
    RawFetch,
    audit_raw_funding,
    detect_extreme_events,
    evaluate_event_paths,
)
from vnedge.scalping.delta_engine.types import Candle

SETTINGS = {
    "expected_cadence_seconds": 3600,
    "raw_to_canonical_divisor": 100.0,
    "maximum_abs_raw_percent_sanity": 5.0,
    "maximum_abs_canonical_fraction_sanity": 0.05,
    "maximum_missing_fraction": 0.0,
}


def _raw(time_s: int, close: float) -> dict:
    return {
        "time": time_s,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": None,
    }


def test_audit_shifts_availability_dedupes_and_excludes_forming_row():
    start = datetime(1970, 1, 1, tzinfo=UTC)
    end = start + timedelta(hours=3)
    raw = RawFetch(
        "BTCUSD",
        (_raw(0, 0.01), _raw(3600, 0.02), _raw(3600, 0.02), _raw(7200, 0.03)),
        1,
    )

    frame, report = audit_raw_funding(raw, start, end, SETTINGS)

    assert list(frame["available_at"]) == [
        pd.Timestamp(start + timedelta(hours=1)),
        pd.Timestamp(start + timedelta(hours=2)),
        pd.Timestamp(start + timedelta(hours=3)),
    ]
    assert report["duplicate_timestamps"] == 1
    assert report["conflicting_duplicate_timestamps"] == 0
    assert report["passed"] is True


def test_audit_fails_on_gap_and_conflicting_duplicate():
    start = datetime(1970, 1, 1, tzinfo=UTC)
    end = start + timedelta(hours=3)
    raw = RawFetch(
        "ETHUSD",
        (_raw(0, 0.01), _raw(0, 0.02), _raw(7200, 0.03)),
        1,
    )

    _, report = audit_raw_funding(raw, start, end, SETTINGS)

    assert report["missing_rows"] == 1
    assert report["conflicting_duplicate_timestamps"] == 1
    assert report["passed"] is False


def test_extreme_detection_uses_prior_window_and_crossing_only():
    start = pd.Timestamp("2026-01-01T00:00:00Z")
    rates = [0.0, 0.1, -0.1, 1.0, 1.1, 0.0, -1.0]
    frame = pd.DataFrame(
        {
            "symbol": "BTCUSD",
            "raw_start": [start + pd.Timedelta(hours=i) for i in range(len(rates))],
            "available_at": [start + pd.Timedelta(hours=i + 1) for i in range(len(rates))],
            "funding_rate": rates,
        }
    )

    events = detect_extreme_events(frame, history=3, threshold=2.0, cooldown_hours=1)

    assert len(events) == 2
    assert events.iloc[0]["funding_rate"] == 1.0
    assert events.iloc[1]["funding_rate"] == -1.0


def test_path_evaluation_enters_after_availability_and_is_directional():
    available = pd.Timestamp("2026-01-01T01:00:00Z")
    events = pd.DataFrame(
        [
            {
                "raw_start": available - pd.Timedelta(hours=1),
                "available_at": available,
                "funding_rate": 0.001,
                "zscore": 3.0,
            }
        ]
    )
    candles = [
        Candle(
            available.to_pydatetime() + timedelta(minutes=i),
            100.0,
            101.0,
            99.0,
            101.0 if i == 60 else 100.0,
            1.0,
            "1m",
        )
        for i in range(1, 61)
    ]

    rows = evaluate_event_paths("BTCUSD", events, candles, horizons=[1], cost_bps=10.0)

    continuation = next(row for row in rows if row["orientation"] == "continuation")
    reversal = next(row for row in rows if row["orientation"] == "reversal")
    assert continuation["entry_bar_close"] == available.to_pydatetime() + timedelta(minutes=1)
    assert continuation["terminal_gross_bps"] == pytest.approx(100.0)
    assert reversal["terminal_gross_bps"] == pytest.approx(-100.0)
