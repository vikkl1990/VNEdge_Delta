"""1-minute scalper — candle approximation of the microstructure scalper.

The real scalper (src/vnedge/scalping/) trades on book_imbalance + taker-buy
ratio (order-flow). Candles have no book, so this approximates flow with:

- flow proxy: 2*(close-low)/(high-low) - 1 in [-1, +1] — closed-near-high =
  buyers were aggressive; closed-near-low = sellers were.
- conviction: volume z-score.
- momentum: short-horizon return.

This is a FAVORABLE approximation (it assumes the candle's close position
cleanly reflects flow, which real microstructure rarely does). Its purpose
in the gauntlet is a lower bound on the fee problem: if a favorable-case
candle scalper cannot clear costs, the true tick version faces at least the
same wall. Scalper-shaped: tight ATR stop, small R target, short holding,
many trades — exactly the profile that stresses the fee wall.

Not a candidate for anything until it clears cost in walk-forward. It exists
to be measured, most likely to fail, and to show WHY.
"""

from __future__ import annotations

import math

import pandas as pd

from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent
from vnedge.strategy.indicators import atr as atr_indicator
from vnedge.strategy.indicators import zscore


class Scalper1m(BaseStrategy):
    strategy_id = "scalper_1m_v1"

    def __init__(
        self,
        funding: pd.DataFrame | None = None,
        *,
        flow_window: int = 3,
        flow_threshold: float = 0.5,
        volume_z_window: int = 60,
        min_volume_z: float = 0.5,
        momentum_bars: int = 3,
        atr_window: int = 30,
        stop_atr_mult: float = 0.75,
        take_profit_r: float = 1.0,
    ) -> None:
        self.funding = funding  # accepted for factory uniformity; unused
        self.flow_window = flow_window
        self.flow_threshold = flow_threshold
        self.volume_z_window = volume_z_window
        self.min_volume_z = min_volume_z
        self.momentum_bars = momentum_bars
        self.atr_window = atr_window
        self.stop_atr_mult = stop_atr_mult
        self.take_profit_r = take_profit_r
        self.warmup_bars = max(atr_window + 1, volume_z_window + 1,
                               flow_window, momentum_bars + 1)

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        df = candles.copy()
        rng = (df["high"] - df["low"]).replace(0.0, float("nan"))
        bar_flow = 2.0 * (df["close"] - df["low"]) / rng - 1.0
        df["flow"] = bar_flow.rolling(self.flow_window).mean()
        df["volume_z"] = zscore(df["volume"], self.volume_z_window)
        df["momentum"] = df["close"].pct_change(self.momentum_bars)
        df["atr"] = atr_indicator(df, self.atr_window)
        # sanity floor: ignore microscopic ATR that makes stops sub-tick
        df["atr"] = df["atr"].where(df["atr"] > 0)
        return df

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        row = df.iloc[index]
        for col in ("flow", "volume_z", "momentum", "atr"):
            if math.isnan(float(row[col])):
                return None
        if float(row["volume_z"]) < self.min_volume_z:
            return None
        close = float(row["close"])
        stop_dist = self.stop_atr_mult * float(row["atr"])
        if stop_dist <= 0:
            return None
        flow = float(row["flow"])
        mom = float(row["momentum"])

        if flow >= self.flow_threshold and mom > 0:
            return SignalIntent(
                "long", stop_price=close - stop_dist,
                take_profit_price=close + self.take_profit_r * stop_dist,
                reason=f"scalp long flow={flow:+.2f} mom={mom:+.4f} volZ={float(row['volume_z']):+.2f}",
            )
        if flow <= -self.flow_threshold and mom < 0:
            return SignalIntent(
                "short", stop_price=close + stop_dist,
                take_profit_price=close - self.take_profit_r * stop_dist,
                reason=f"scalp short flow={flow:+.2f} mom={mom:+.4f} volZ={float(row['volume_z']):+.2f}",
            )
        return None
