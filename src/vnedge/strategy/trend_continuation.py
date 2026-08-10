"""Candidate 1: regime-filtered trend continuation.

Hypothesis: breakouts have positive after-cost expectancy ONLY when the
market is already trending cleanly, volatility is not blowing out, and we
are not paying rich funding to join a crowded side.

Entry (long; short mirrored):
- close breaks above the prior N-bar close high (current bar excluded)
- trend regime up (efficiency ratio + EMA alignment)
- ATR percentile below a ceiling (no entries into volatility explosions)
- funding not expensive in our direction

Risk: ATR-multiple stop, R-multiple take profit. Max holding and cooldown
live in BacktestConfig / the risk layer, not here.

Known failure modes: range-bound chop (filtered but never perfectly),
V-shaped reversals right after breakout, low-liquidity fakeouts.
"""

from __future__ import annotations

import math

import pandas as pd

from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent
from vnedge.strategy.indicators import prior_high, prior_low
from vnedge.strategy.regime import (
    RegimeParams,
    add_regime_columns,
    merge_funding,
    regime_warmup_bars,
)

_REQUIRED = ("atr", "atr_pct", "er", "prior_high", "prior_low")


class TrendContinuation(BaseStrategy):
    strategy_id = "trend_continuation_v1"

    def __init__(
        self,
        funding: pd.DataFrame | None = None,
        *,
        breakout_bars: int = 48,
        stop_atr_mult: float = 2.0,
        take_profit_r: float = 2.0,
        max_funding_against: float = 0.0005,
        max_atr_pct: float = 0.90,
        regime: RegimeParams = RegimeParams(),
    ) -> None:
        self.funding = funding
        self.breakout_bars = breakout_bars
        self.stop_atr_mult = stop_atr_mult
        self.take_profit_r = take_profit_r
        self.max_funding_against = max_funding_against
        self.max_atr_pct = max_atr_pct
        self.regime = regime
        self.warmup_bars = max(breakout_bars + 1, regime_warmup_bars(regime))

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        df = add_regime_columns(candles, self.regime)
        df["prior_high"] = prior_high(df["close"], self.breakout_bars)
        df["prior_low"] = prior_low(df["close"], self.breakout_bars)
        return merge_funding(df, self.funding)

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        row = df.iloc[index]
        if any(math.isnan(float(row[c])) for c in _REQUIRED):
            return None  # still in indicator warmup
        if row["atr_pct"] > self.max_atr_pct:
            return None

        close = float(row["close"])
        stop_dist = self.stop_atr_mult * float(row["atr"])
        if stop_dist <= 0:
            return None

        if (
            row["regime_trend_up"]
            and close > float(row["prior_high"])
            and float(row["funding_rate"]) <= self.max_funding_against
        ):
            return SignalIntent(
                side="long",
                stop_price=close - stop_dist,
                take_profit_price=close + self.take_profit_r * stop_dist,
                reason=(
                    f"close {close:.2f} broke {self.breakout_bars}-bar high "
                    f"{float(row['prior_high']):.2f}; ER={float(row['er']):.2f}, "
                    f"ATRpct={float(row['atr_pct']):.2f}, "
                    f"funding={float(row['funding_rate']):+.4%}"
                ),
            )
        if (
            row["regime_trend_down"]
            and close < float(row["prior_low"])
            and float(row["funding_rate"]) >= -self.max_funding_against
        ):
            stop = close + stop_dist
            return SignalIntent(
                side="short",
                stop_price=stop,
                take_profit_price=close - self.take_profit_r * stop_dist,
                reason=(
                    f"close {close:.2f} broke {self.breakout_bars}-bar low "
                    f"{float(row['prior_low']):.2f}; ER={float(row['er']):.2f}, "
                    f"ATRpct={float(row['atr_pct']):.2f}, "
                    f"funding={float(row['funding_rate']):+.4%}"
                ),
            )
        return None
