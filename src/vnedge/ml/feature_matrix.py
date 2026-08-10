"""Feature matrix builder — strictly causal features for ML models.

Every feature at bar i is computable from bars 0..i only (rolling windows and
backward shifts, reusing the same tested indicator utilities the rule-based
strategies use). NaN marks warmup, exactly as everywhere else in the system.
The causality property has a dedicated test: mutating future bars must not
change past feature rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from vnedge.strategy.indicators import (
    ema,
    rolling_percentile,
    sma,
    zscore,
)
from vnedge.strategy.regime import RegimeParams, add_regime_columns, merge_funding


@dataclass(frozen=True)
class FeatureParams:
    regime: RegimeParams = field(default_factory=RegimeParams)
    funding_pct_window: int = 240
    vol_window: int = 24
    z_window: int = 48
    vol_ratio_window: int = 96  # trailing baseline for the vol-expansion ratio

    @property
    def warmup_bars(self) -> int:
        from vnedge.strategy.regime import regime_warmup_bars

        return max(
            regime_warmup_bars(self.regime),
            self.funding_pct_window,
            self.z_window + 1,
            self.vol_window + self.vol_ratio_window,
        )


#: model input columns, in fixed order (order is part of the model contract)
FEATURE_COLUMNS = [
    "ret_1", "ret_6", "ret_24",
    "vol_24",
    "atr_pct", "er",
    "trend_atr", "dist_sma_atr",
    "funding_rate", "funding_pct",
    "volume_z", "range_atr", "close_z",
    "regime_up", "regime_down",
    # --- fee-wall / microstructure / session (appended; order-stable) ---
    "atr_bps", "range_bps",              # volatility in bps — the fee-wall yardstick
    "body_atr", "upper_wick_atr", "lower_wick_atr",  # displacement vs rejection
    "ret_accel", "vol_ratio",            # momentum acceleration, vol expansion
    "funding_z",                         # funding extremity
    "hour_sin", "hour_cos",              # session (cyclical hour-of-day)
]


def build_feature_matrix(
    candles: pd.DataFrame,
    funding: pd.DataFrame | None,
    params: FeatureParams = FeatureParams(),
) -> pd.DataFrame:
    """Returns candles + regime columns + FEATURE_COLUMNS."""
    df = add_regime_columns(candles, params.regime)
    df = merge_funding(df, funding)
    close = df["close"]

    df["ret_1"] = close.pct_change(1)
    df["ret_6"] = close.pct_change(6)
    df["ret_24"] = close.pct_change(24)
    df["vol_24"] = df["ret_1"].rolling(params.vol_window).std()

    atr = df["atr"]
    fast = ema(close, params.regime.ema_fast)
    slow = ema(close, params.regime.ema_slow)
    df["trend_atr"] = (fast - slow) / atr
    df["dist_sma_atr"] = (close - sma(close, params.z_window)) / atr

    df["funding_pct"] = rolling_percentile(df["funding_rate"], params.funding_pct_window)
    df["volume_z"] = zscore(df["volume"], params.z_window)
    df["range_atr"] = (df["high"] - df["low"]) / atr
    df["close_z"] = zscore(close, params.z_window)
    df["regime_up"] = df["regime_trend_up"].astype(float)
    df["regime_down"] = df["regime_trend_down"].astype(float)

    # --- fee-wall / microstructure / session features (all causal: bar i uses
    # only bars 0..i; the causality test iterates FEATURE_COLUMNS at row 300) ---
    open_, high, low = df["open"], df["high"], df["low"]
    # Volatility in basis points — directly comparable to the taker fee wall
    # (~5 bps): a move that is only a few bps cannot pay costs.
    df["atr_bps"] = atr / close * 1e4
    df["range_bps"] = (high - low) / close * 1e4
    # Candle anatomy: body = displacement/conviction, wicks = rejection.
    body_top = np.maximum(open_, close)
    body_bot = np.minimum(open_, close)
    df["body_atr"] = (close - open_).abs() / atr
    df["upper_wick_atr"] = (high - body_top) / atr
    df["lower_wick_atr"] = (body_bot - low) / atr
    # Momentum acceleration (backward shift) and volatility expansion.
    df["ret_accel"] = df["ret_6"] - df["ret_6"].shift(6)
    df["vol_ratio"] = df["vol_24"] / df["vol_24"].rolling(params.vol_ratio_window).mean()
    # Funding extremity (complements the percentile). A constant/absent funding
    # series has zero dispersion => 0 (no extremity); warmup stays NaN (not
    # computable), matching every other feature. Without this guard a funding-
    # free venue would NaN every row and purge the whole matrix.
    _froll = df["funding_rate"].rolling(params.z_window)
    _fstd = _froll.std()
    df["funding_z"] = ((df["funding_rate"] - _froll.mean()) / _fstd).mask(_fstd == 0.0, 0.0)
    # Session: cyclical hour-of-day (depends only on the bar's own timestamp).
    hour = pd.to_datetime(df["timestamp"], utc=True).dt.hour.to_numpy()
    df["hour_sin"] = np.sin(2.0 * np.pi * hour / 24.0)
    df["hour_cos"] = np.cos(2.0 * np.pi * hour / 24.0)
    return df
