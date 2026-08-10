"""Offensive lane A: volatility-expansion breakout.

Hypothesis: breakouts clear the fee wall only when volatility is EXPANDING
and volume confirms — most breakouts fail, so the payoff must come from a
2.5R target on the ones that run. Judged under OFFENSIVE_GATES: low win
rate is acceptable; poor payoff ratio or one-lucky-trade results are not.

Known failure modes: false breakouts in high-vol chop, slippage on entry
during the expansion itself, regime filter lag at trend starts.
"""

from __future__ import annotations

import math

import pandas as pd

from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent
from vnedge.strategy.indicators import prior_high, prior_low, zscore
from vnedge.strategy.regime import (
    RegimeParams,
    add_regime_columns,
    merge_funding,
    regime_warmup_bars,
)

_REQUIRED = ("atr", "atr_pct", "er", "prior_high", "prior_low", "volume_z")


class VolatilityExpansionBreakout(BaseStrategy):
    strategy_id = "volatility_expansion_breakout_v1"

    def __init__(
        self,
        funding: pd.DataFrame | None = None,
        *,
        breakout_bars: int = 48,
        stop_atr_mult: float = 2.0,
        take_profit_r: float = 2.5,
        min_atr_pct: float = 0.55,
        max_atr_pct: float = 0.97,
        min_volume_z: float = 0.5,
        max_funding_against: float = 0.0008,
        volume_z_window: int = 48,
        regime: RegimeParams = RegimeParams(),
    ) -> None:
        self.funding = funding
        self.breakout_bars = breakout_bars
        self.stop_atr_mult = stop_atr_mult
        self.take_profit_r = take_profit_r
        self.min_atr_pct = min_atr_pct
        self.max_atr_pct = max_atr_pct
        self.min_volume_z = min_volume_z
        self.max_funding_against = max_funding_against
        self.volume_z_window = volume_z_window
        self.regime = regime
        self.warmup_bars = max(breakout_bars + 1, regime_warmup_bars(regime),
                               volume_z_window + 1)

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        df = add_regime_columns(candles, self.regime)
        df["prior_high"] = prior_high(df["close"], self.breakout_bars)
        df["prior_low"] = prior_low(df["close"], self.breakout_bars)
        df["volume_z"] = zscore(df["volume"], self.volume_z_window)
        return merge_funding(df, self.funding)

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        row = df.iloc[index]
        if any(math.isnan(float(row[c])) for c in _REQUIRED):
            return None
        atr_pct = float(row["atr_pct"])
        if not (self.min_atr_pct <= atr_pct <= self.max_atr_pct):
            return None  # expansion required; explosions excluded
        if float(row["volume_z"]) < self.min_volume_z:
            return None
        close = float(row["close"])
        stop_dist = self.stop_atr_mult * float(row["atr"])
        if stop_dist <= 0:
            return None
        funding = float(row["funding_rate"])
        common = (
            f"vol-expansion breakout: ATRpct={atr_pct:.2f}, "
            f"volZ={float(row['volume_z']):+.2f}, ER={float(row['er']):.2f}"
        )
        if (row["regime_trend_up"] and close > float(row["prior_high"])
                and funding <= self.max_funding_against):
            return SignalIntent(
                "long", stop_price=close - stop_dist,
                take_profit_price=close + self.take_profit_r * stop_dist,
                reason=common,
            )
        if (row["regime_trend_down"] and close < float(row["prior_low"])
                and funding >= -self.max_funding_against):
            return SignalIntent(
                "short", stop_price=close + stop_dist,
                take_profit_price=close - self.take_profit_r * stop_dist,
                reason=common,
            )
        return None
