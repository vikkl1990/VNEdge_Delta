"""Offensive lane B: panic reversal.

Hypothesis: after an extreme downside extension with a volatility spike,
when funding does NOT support further downside and price prints a first
stabilization bar, the snap-back is sharp and asymmetric.

The falling-knife defense is structural: entry requires a stabilization
candle (green close, higher low), the stop sits below the panic low, and
the trade is REFUSED unless the mean-reversion target clears a minimum
R-multiple — a panic without asymmetric payoff is not a setup.

Long-only by design: panic-buy blowoffs are a separate hypothesis with
different microstructure; pre-register it separately if wanted.
"""

from __future__ import annotations

import math

import pandas as pd

from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent
from vnedge.strategy.indicators import rolling_percentile, sma, zscore
from vnedge.strategy.regime import (
    RegimeParams,
    add_regime_columns,
    merge_funding,
    regime_warmup_bars,
)

_REQUIRED = ("atr", "atr_pct", "drop_z", "target_mean", "funding_pct", "prev_low")


class PanicReversal(BaseStrategy):
    strategy_id = "panic_reversal_v1"

    def __init__(
        self,
        funding: pd.DataFrame | None = None,
        *,
        drop_z_window: int = 72,
        drop_z_entry: float = -2.5,
        min_atr_pct: float = 0.85,
        max_funding_pct: float = 0.40,
        stop_atr_pad: float = 0.25,
        target_window: int = 48,
        min_rr: float = 1.8,
        funding_pct_window: int = 240,
        regime: RegimeParams = RegimeParams(),
    ) -> None:
        self.funding = funding
        self.drop_z_window = drop_z_window
        self.drop_z_entry = drop_z_entry
        self.min_atr_pct = min_atr_pct
        self.max_funding_pct = max_funding_pct
        self.stop_atr_pad = stop_atr_pad
        self.target_window = target_window
        self.min_rr = min_rr
        self.funding_pct_window = funding_pct_window
        self.regime = regime
        self.warmup_bars = max(
            drop_z_window + 1, funding_pct_window, regime_warmup_bars(regime)
        )

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        df = add_regime_columns(candles, self.regime)
        df = merge_funding(df, self.funding)
        df["drop_z"] = zscore(df["close"], self.drop_z_window)
        df["target_mean"] = sma(df["close"], self.target_window)
        df["funding_pct"] = rolling_percentile(df["funding_rate"], self.funding_pct_window)
        df["prev_low"] = df["low"].shift(1)
        return df

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        row = df.iloc[index]
        if any(math.isnan(float(row[c])) for c in _REQUIRED):
            return None
        if float(row["drop_z"]) > self.drop_z_entry:
            return None  # not extended enough to call it panic
        if float(row["atr_pct"]) < self.min_atr_pct:
            return None  # extension without a vol spike is a trend, not a panic
        if float(row["funding_pct"]) > self.max_funding_pct:
            return None  # longs still paying up: crowd not flushed
        close, low = float(row["close"]), float(row["low"])
        if close <= float(row["open"]) or low <= float(row["prev_low"]):
            return None  # no stabilization bar yet — do not catch the knife
        target = float(row["target_mean"])
        stop = min(low, float(row["prev_low"])) - self.stop_atr_pad * float(row["atr"])
        if stop <= 0 or target <= close:
            return None
        rr = (target - close) / (close - stop)
        if rr < self.min_rr:
            return None  # panic without asymmetric payoff is not a setup
        return SignalIntent(
            "long", stop_price=stop, take_profit_price=target,
            reason=(
                f"panic reversal: z={float(row['drop_z']):+.2f}, "
                f"ATRpct={float(row['atr_pct']):.2f}, "
                f"fundingPct={float(row['funding_pct']):.2f}, RR={rr:.1f}"
            ),
        )
