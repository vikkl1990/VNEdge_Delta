"""Indicator correctness and the no-lookahead property."""

import numpy as np
import pandas as pd
import pytest

from vnedge.strategy.indicators import (
    atr,
    efficiency_ratio,
    prior_high,
    prior_low,
    rolling_percentile,
    sma,
    true_range,
    zscore,
)


def s(values) -> pd.Series:
    return pd.Series(values, dtype="float64")


def test_sma_known_values():
    result = sma(s([1, 2, 3, 4, 5]), 3)
    assert np.isnan(result.iloc[1])  # warmup is NaN, never a guess
    assert result.iloc[2] == pytest.approx(2.0)
    assert result.iloc[4] == pytest.approx(4.0)


def test_prior_high_excludes_current_bar():
    """The no-lookahead property: a new all-time high must NOT see itself."""
    series = s([10, 12, 11, 15, 13])
    result = prior_high(series, 3)
    # at index 3 (value 15): prior 3 bars are [10,12,11] -> 12, not 15
    assert result.iloc[3] == pytest.approx(12.0)
    # at index 4: prior 3 bars [12,11,15] -> 15
    assert result.iloc[4] == pytest.approx(15.0)


def test_prior_low_excludes_current_bar():
    series = s([10, 8, 9, 5, 7])
    result = prior_low(series, 3)
    assert result.iloc[3] == pytest.approx(8.0)  # not the new low 5 itself


def test_true_range_uses_prev_close_gaps():
    df = pd.DataFrame(
        {"high": [105.0, 112.0], "low": [95.0, 108.0], "close": [100.0, 110.0]}
    )
    tr = true_range(df)
    # bar 1: max(112-108, |112-100|, |108-100|) = 12 (gap dominates range)
    assert tr.iloc[1] == pytest.approx(12.0)


def test_atr_first_bar_is_nan():
    df = pd.DataFrame(
        {"high": [105.0] * 5, "low": [95.0] * 5, "close": [100.0] * 5}
    )
    result = atr(df, 3)
    assert np.isnan(result.iloc[2])  # window includes bar 0's NaN TR
    assert result.iloc[3] == pytest.approx(10.0)


def test_zscore_known_value():
    result = zscore(s([1, 2, 3, 4, 10]), 5)
    window = np.array([1, 2, 3, 4, 10], dtype=float)
    expected = (10 - window.mean()) / window.std(ddof=1)
    assert result.iloc[4] == pytest.approx(expected)


def test_rolling_percentile_midrank():
    result = rolling_percentile(s([1, 2, 3, 4, 5, 0]), 5)
    assert result.iloc[4] == pytest.approx(0.9)   # max of window: 4/5 + 0.5/5
    assert result.iloc[5] == pytest.approx(0.1)   # min of [2,3,4,5,0]: 0 + 0.5/5


def test_rolling_percentile_flat_series_is_neutral():
    """A constant series must read 0.5, not 1.0 — flat funding is not
    'crowded positioning'. This was a real bug caught by strategy tests."""
    result = rolling_percentile(s([0.0001] * 10), 5)
    assert result.iloc[-1] == pytest.approx(0.5)


def test_efficiency_ratio_extremes():
    trend = s(list(range(100)))
    assert efficiency_ratio(trend, 10).iloc[-1] == pytest.approx(1.0)
    chop = s([100, 101] * 50)
    assert efficiency_ratio(chop, 10).iloc[-1] < 0.2
    flat = s([100.0] * 50)
    assert np.isnan(efficiency_ratio(flat, 10).iloc[-1])  # 0/0 -> NaN, not signal
