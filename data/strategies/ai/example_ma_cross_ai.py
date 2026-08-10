"""Example AI-authored strategy: a causal moving-average crossover.

This file is deliberately minimal and PASSES the sandbox validator: it imports
only whitelisted modules, reads rows <= index only, uses NaN as the warmup
marker, and carries a stop on every intent. It is a *candidate* — loading it
does not register it for trading; it enters the research sweep and must clear
walk-forward gates, the causality analyzer, a pre-registered untouched-data
judgment, and human approval like any other strategy.

Hypothesis (unproven, illustrative): when the fast SMA crosses above the slow
SMA the short-term trend has turned up; enter long with an ATR stop and an
R-multiple target. Mirror image for shorts.
"""

from __future__ import annotations

import math

import pandas as pd

from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent
from vnedge.strategy.indicators import atr, sma

_REQUIRED = ("sma_fast", "sma_slow", "sma_fast_prev", "sma_slow_prev", "atr")


class ExampleMaCrossAI(BaseStrategy):
    strategy_id = "example_ma_cross"

    def __init__(
        self,
        fast: int = 12,
        slow: int = 48,
        stop_atr_mult: float = 2.0,
        take_profit_r: float = 2.0,
        atr_window: int = 14,
    ) -> None:
        if fast >= slow:
            raise ValueError("fast window must be shorter than slow window")
        self.fast = fast
        self.slow = slow
        self.stop_atr_mult = stop_atr_mult
        self.take_profit_r = take_profit_r
        self.atr_window = atr_window
        # slow SMA needs `slow` bars; the prior-bar shift needs one more.
        self.warmup_bars = slow + 1

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        df = candles.copy()
        df["sma_fast"] = sma(df["close"], self.fast)
        df["sma_slow"] = sma(df["close"], self.slow)
        # Prior-bar values via a BACKWARD shift — the causal way to detect a
        # crossover between the previous close and this one.
        df["sma_fast_prev"] = df["sma_fast"].shift(1)
        df["sma_slow_prev"] = df["sma_slow"].shift(1)
        df["atr"] = atr(df, self.atr_window)
        return df

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        row = df.iloc[index]
        if any(math.isnan(float(row[col])) for col in _REQUIRED):
            return None  # still in indicator warmup

        close = float(row["close"])
        stop_dist = self.stop_atr_mult * float(row["atr"])
        if stop_dist <= 0:
            return None

        fast_now = float(row["sma_fast"])
        slow_now = float(row["sma_slow"])
        fast_prev = float(row["sma_fast_prev"])
        slow_prev = float(row["sma_slow_prev"])

        crossed_up = fast_prev <= slow_prev and fast_now > slow_now
        crossed_down = fast_prev >= slow_prev and fast_now < slow_now

        if crossed_up:
            return SignalIntent(
                side="long",
                stop_price=close - stop_dist,
                take_profit_price=close + self.take_profit_r * stop_dist,
                reason=f"fast SMA({self.fast}) crossed above slow SMA({self.slow}) at {close:.2f}",
            )
        if crossed_down:
            return SignalIntent(
                side="short",
                stop_price=close + stop_dist,
                take_profit_price=close - self.take_profit_r * stop_dist,
                reason=f"fast SMA({self.fast}) crossed below slow SMA({self.slow}) at {close:.2f}",
            )
        return None
