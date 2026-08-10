"""Offensive lane C: funding squeeze continuation.

The aggressive cousin of funding mean-reversion — same feature, opposite
action, disambiguated by regime: extreme funding inside a STRONG TREND with
volume expansion is evidence of a squeeze underway, not a fade setup. MR
fades crowding in chop; this joins it in trend. The regime filter is the
entire difference between the two hypotheses, which is why both can coexist
in the registry without contradiction.

Known failure modes: late entries at squeeze exhaustion, funding flipping
mid-hold, regime filter classifying a blowoff top as a clean trend.
"""

from __future__ import annotations

import math

import pandas as pd

from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent
from vnedge.strategy.indicators import rolling_percentile, zscore
from vnedge.strategy.regime import (
    RegimeParams,
    add_regime_columns,
    merge_funding,
    regime_warmup_bars,
)

_REQUIRED = ("atr", "er", "funding_pct", "volume_z")


class FundingSqueezeContinuation(BaseStrategy):
    strategy_id = "funding_squeeze_continuation_v1"

    def __init__(
        self,
        funding: pd.DataFrame,
        *,
        extreme_pct: float = 0.90,
        min_volume_z: float = 0.0,
        stop_atr_mult: float = 2.0,
        take_profit_r: float = 2.5,
        funding_pct_window: int = 240,
        volume_z_window: int = 48,
        regime: RegimeParams = RegimeParams(),
    ) -> None:
        if funding is None or funding.empty:
            raise ValueError(
                "FundingSqueezeContinuation requires a funding series — "
                "the squeeze IS the hypothesis"
            )
        self.funding = funding
        self.extreme_pct = extreme_pct
        self.min_volume_z = min_volume_z
        self.stop_atr_mult = stop_atr_mult
        self.take_profit_r = take_profit_r
        self.funding_pct_window = funding_pct_window
        self.volume_z_window = volume_z_window
        self.regime = regime
        self.warmup_bars = max(
            funding_pct_window, volume_z_window + 1, regime_warmup_bars(regime)
        )

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        df = add_regime_columns(candles, self.regime)
        df = merge_funding(df, self.funding)
        df["funding_pct"] = rolling_percentile(df["funding_rate"], self.funding_pct_window)
        df["volume_z"] = zscore(df["volume"], self.volume_z_window)
        return df

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        row = df.iloc[index]
        if any(math.isnan(float(row[c])) for c in _REQUIRED):
            return None
        if float(row["volume_z"]) < self.min_volume_z:
            return None
        close = float(row["close"])
        stop_dist = self.stop_atr_mult * float(row["atr"])
        if stop_dist <= 0:
            return None
        fpct = float(row["funding_pct"])
        common = (
            f"funding squeeze: fundingPct={fpct:.2f}, "
            f"ER={float(row['er']):.2f}, volZ={float(row['volume_z']):+.2f} "
            "(continuation, not fade — trend regime active)"
        )
        # Extreme positive funding + strong uptrend: shorts are being
        # squeezed; JOIN the crowding instead of fading it.
        if fpct >= self.extreme_pct and row["regime_trend_up"]:
            return SignalIntent(
                "long", stop_price=close - stop_dist,
                take_profit_price=close + self.take_profit_r * stop_dist,
                reason=common,
            )
        if fpct <= 1.0 - self.extreme_pct and row["regime_trend_down"]:
            return SignalIntent(
                "short", stop_price=close + stop_dist,
                take_profit_price=close - self.take_profit_r * stop_dist,
                reason=common,
            )
        return None
