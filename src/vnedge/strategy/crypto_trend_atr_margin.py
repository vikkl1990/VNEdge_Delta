"""Source-backed crypto trend/ATR-margin scanner.

Research origin: the accessible TradingView "Crypto Trend Indicator" batch
translated in ``research/pine/top100_crypto_backtest.py``. The only source
batch lane that survived a simple chronological OOS sanity check was
DOGEUSDT 1h with an EMA30/EMA60 trend spread that had to clear an ATR60
margin.

This module ports that entry trigger into the normal VNEDGE strategy contract.
It is shadow-only until a full untouched-window judgment and paper exit-parity
check are complete.
"""

from __future__ import annotations

import math

import pandas as pd

from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent, StrategyExitIntent
from vnedge.strategy.indicators import ema, true_range

_REQUIRED = ("ema_fast", "ema_slow", "atr_margin", "trend_spread")


def _wilder_atr(candles: pd.DataFrame, length: int) -> pd.Series:
    """Causal ATR variant used by the source-batch research proxy."""

    tr = true_range(candles)
    return tr.ewm(alpha=1 / length, adjust=False).mean()


class CryptoTrendAtrMargin(BaseStrategy):
    """EMA trend flip that must clear an ATR-distance margin."""

    strategy_id = "crypto_trend_atr_margin_v1"

    def __init__(
        self,
        funding: pd.DataFrame | None = None,
        *,
        fast_ema: int = 30,
        slow_ema: int = 60,
        atr_window: int = 60,
        atr_margin_mult: float = 0.30,
        stop_atr_mult: float = 1.60,
        min_stop_bps: float = 15.0,
        take_profit_r: float | None = None,
        exit_on_neutral: bool = True,
        exit_on_opposite: bool = True,
    ) -> None:
        if fast_ema <= 0 or slow_ema <= 0 or atr_window <= 0:
            raise ValueError("EMA and ATR windows must be positive")
        if fast_ema >= slow_ema:
            raise ValueError("fast_ema must be lower than slow_ema")
        if atr_margin_mult <= 0 or stop_atr_mult <= 0 or min_stop_bps <= 0:
            raise ValueError("margin/stop settings must be positive")
        if take_profit_r is not None and take_profit_r <= 0:
            raise ValueError("take_profit_r must be positive when configured")
        self.funding = funding
        self.fast_ema = fast_ema
        self.slow_ema = slow_ema
        self.atr_window = atr_window
        self.atr_margin_mult = atr_margin_mult
        self.stop_atr_mult = stop_atr_mult
        self.min_stop_bps = min_stop_bps
        self.take_profit_r = take_profit_r
        self.exit_on_neutral = exit_on_neutral
        self.exit_on_opposite = exit_on_opposite
        self.warmup_bars = max(250, slow_ema + atr_window + 1)

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        df = candles.copy()
        df["ema_fast"] = ema(df["close"], self.fast_ema)
        df["ema_slow"] = ema(df["close"], self.slow_ema)
        df["atr_margin"] = _wilder_atr(df, self.atr_window)
        df["trend_spread"] = df["ema_fast"] - df["ema_slow"]
        df["trend_bull"] = df["trend_spread"] > self.atr_margin_mult * df["atr_margin"]
        df["trend_bear"] = df["trend_spread"] < -self.atr_margin_mult * df["atr_margin"]
        return df

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        if index <= 0 or index < self.warmup_bars:
            return None
        row = df.iloc[index]
        prev = df.iloc[index - 1]
        if any(math.isnan(float(row[c])) for c in _REQUIRED):
            return None

        close = float(row["close"])
        stop_dist = max(
            self.stop_atr_mult * float(row["atr_margin"]),
            close * self.min_stop_bps / 10_000.0,
        )
        if stop_dist <= 0:
            return None

        bull_flip = bool(row["trend_bull"]) and not bool(prev["trend_bull"])
        bear_flip = bool(row["trend_bear"]) and not bool(prev["trend_bear"])
        spread_bps = float(row["trend_spread"]) / close * 10_000.0
        margin_bps = self.atr_margin_mult * float(row["atr_margin"]) / close * 10_000.0

        if bull_flip:
            tp = close + self.take_profit_r * stop_dist if self.take_profit_r else None
            return SignalIntent(
                side="long",
                stop_price=close - stop_dist,
                take_profit_price=tp,
                reason=(
                    "crypto trend ATR-margin bull flip: "
                    f"ema{self.fast_ema}>ema{self.slow_ema}, "
                    f"spread={spread_bps:+.1f}bps > margin={margin_bps:.1f}bps"
                ),
            )
        if bear_flip:
            tp = close - self.take_profit_r * stop_dist if self.take_profit_r else None
            return SignalIntent(
                side="short",
                stop_price=close + stop_dist,
                take_profit_price=tp,
                reason=(
                    "crypto trend ATR-margin bear flip: "
                    f"ema{self.fast_ema}<ema{self.slow_ema}, "
                    f"spread={spread_bps:+.1f}bps < -margin={margin_bps:.1f}bps"
                ),
            )
        return None

    def exit_signal(
        self,
        df: pd.DataFrame,
        index: int,
        side: str,
        entry_price: float,
    ) -> StrategyExitIntent | None:
        if index <= 0 or index < self.warmup_bars:
            return None
        row = df.iloc[index]
        if any(math.isnan(float(row[c])) for c in _REQUIRED):
            return None
        close = float(row["close"])
        if side == "long":
            if self.exit_on_opposite and bool(row["trend_bear"]):
                return StrategyExitIntent(
                    reason="strategy_reversal_long",
                    exit_price=close,
                )
            if self.exit_on_neutral and not bool(row["trend_bull"]):
                return StrategyExitIntent(
                    reason="strategy_neutral_long",
                    exit_price=close,
                )
        elif side == "short":
            if self.exit_on_opposite and bool(row["trend_bull"]):
                return StrategyExitIntent(
                    reason="strategy_reversal_short",
                    exit_price=close,
                )
            if self.exit_on_neutral and not bool(row["trend_bear"]):
                return StrategyExitIntent(
                    reason="strategy_neutral_short",
                    exit_price=close,
                )
        return None
