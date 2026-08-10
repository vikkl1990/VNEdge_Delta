"""Causal indicator utilities.

Every function here is lookahead-safe by construction: only rolling windows
over past data and backward shifts are allowed. The critical convention:
``prior_high``/``prior_low`` EXCLUDE the current bar, so "close breaks the
N-bar high" compares against highs the market had already printed.

NaN is the warmup marker — strategies must treat any NaN input as "no
signal", and tests verify these functions emit NaN until their window fills.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window).mean()


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    return pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)


def atr(df: pd.DataFrame, window: int) -> pd.Series:
    # First bar's TR needs prev_close; keep it NaN rather than assuming.
    tr = true_range(df)
    tr.iloc[0] = np.nan
    return tr.rolling(window).mean()


def prior_high(series: pd.Series, window: int) -> pd.Series:
    """Max of the PREVIOUS `window` bars — current bar excluded (no lookahead
    into the very bar being evaluated)."""
    return series.rolling(window).max().shift(1)


def prior_low(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window).min().shift(1)


def zscore(series: pd.Series, window: int) -> pd.Series:
    mean = series.rolling(window).mean()
    std = series.rolling(window).std()
    return (series - mean) / std


def rolling_percentile(series: pd.Series, window: int) -> pd.Series:
    """Midrank percentile of the CURRENT value within its trailing window,
    in (0, 1). Ties count half — so a perfectly flat series reads 0.5
    (neutral), never 1.0 (extreme). A constant funding rate must not look
    like a crowded extreme."""
    return series.rolling(window).apply(
        lambda w: (w < w[-1]).mean() + 0.5 * (w == w[-1]).mean(), raw=True
    )


def efficiency_ratio(close: pd.Series, window: int) -> pd.Series:
    """Kaufman efficiency ratio: |net move| / path length over `window`.
    ~1.0 = clean trend, ~0.0 = chop. NaN when the path is zero (dead flat)."""
    net = (close - close.shift(window)).abs()
    path = close.diff().abs().rolling(window).sum()
    return net / path.replace(0.0, np.nan)
