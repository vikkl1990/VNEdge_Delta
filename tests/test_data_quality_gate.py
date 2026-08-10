"""Quality gate — every rejection class the gate must catch."""

import pandas as pd

from vnedge.data.data_quality_gate import (
    validate_candles,
    validate_funding,
    validate_open_interest,
)
from vnedge.data.schemas import normalize_candles, normalize_funding


def make_candles(n: int = 10, step_ms: int = 3_600_000) -> pd.DataFrame:
    base = 1_750_000_000_000
    raw = [
        [base + i * step_ms, 100.0 + i, 101.0 + i, 99.0 + i, 100.5 + i, 10.0]
        for i in range(n)
    ]
    return normalize_candles(raw)


def test_clean_candles_pass():
    report = validate_candles(make_candles(), "1h")
    assert report.passed, report.summary
    assert report.gap_count == 0


def test_empty_rejected():
    report = validate_candles(make_candles(0), "1h")
    assert not report.passed
    assert any(i.check == "empty" for i in report.issues)


def test_missing_column_rejected():
    df = make_candles().drop(columns=["volume"])
    report = validate_candles(df, "1h")
    assert not report.passed
    assert any(i.check == "schema" for i in report.issues)


def test_null_values_rejected():
    df = make_candles()
    df.loc[3, "close"] = float("nan")
    report = validate_candles(df, "1h")
    assert not report.passed
    assert any(i.check == "nulls" for i in report.issues)


def test_duplicate_timestamps_rejected():
    df = make_candles()
    df.loc[5, "timestamp"] = df.loc[4, "timestamp"]
    report = validate_candles(df, "1h")
    assert not report.passed
    assert any(i.check == "duplicates" for i in report.issues)


def test_non_monotonic_rejected():
    df = make_candles().iloc[::-1].reset_index(drop=True)
    report = validate_candles(df, "1h")
    assert not report.passed
    assert any(i.check == "monotonic" for i in report.issues)


def test_non_positive_price_rejected():
    df = make_candles()
    df.loc[2, "low"] = -1.0
    report = validate_candles(df, "1h")
    assert not report.passed
    assert any(i.check == "prices" for i in report.issues)


def test_negative_volume_rejected():
    df = make_candles()
    df.loc[2, "volume"] = -5.0
    report = validate_candles(df, "1h")
    assert not report.passed
    assert any(i.check == "volume" for i in report.issues)


def test_high_below_low_rejected():
    df = make_candles()
    df.loc[2, "high"] = df.loc[2, "low"] - 1.0
    report = validate_candles(df, "1h")
    assert not report.passed
    assert any(i.check == "ohlc_range" for i in report.issues)


def test_gap_rejected_by_default_but_allowed_explicitly():
    df = make_candles(10).drop(index=5).reset_index(drop=True)
    strict = validate_candles(df, "1h")
    assert not strict.passed
    assert any(i.check == "gaps" for i in strict.issues)

    lenient = validate_candles(df, "1h", allow_gaps=True)
    assert lenient.passed
    assert lenient.gap_count == 1  # gap still recorded, never hidden


def test_unknown_timeframe_rejected():
    report = validate_candles(make_candles(), "7m")
    assert not report.passed
    assert any(i.check == "timeframe" for i in report.issues)


def test_funding_sanity_bounds():
    base = 1_750_000_000_000
    good = normalize_funding(
        [{"timestamp": base + i * 28_800_000, "fundingRate": 0.0001} for i in range(5)]
    )
    assert validate_funding(good).passed

    absurd = normalize_funding(
        [{"timestamp": base, "fundingRate": 0.5}]  # 50% per interval = corrupt
    )
    report = validate_funding(absurd)
    assert not report.passed
    assert any(i.check == "funding_sanity" for i in report.issues)


def test_open_interest_negative_rejected():
    df = pd.DataFrame(
        {
            "timestamp": pd.to_datetime([1_750_000_000_000], unit="ms", utc=True),
            "open_interest": [-10.0],
            "open_interest_value": [float("nan")],  # NaN value column is allowed
        }
    )
    report = validate_open_interest(df)
    assert not report.passed
    assert any(i.check == "open_interest" for i in report.issues)
