"""Candidate 2: funding-skew mean reversion.

Hypothesis: when funding is at a trailing extreme AND price is stretched
from its mean in the same direction, positioning is crowded and short-
horizon reversion toward the mean has positive after-cost expectancy —
UNLESS the market is in a clean trend (crowded can stay crowded in trends,
which is exactly when fading gets run over).

Entry (short; long mirrored):
- funding-rate percentile at a trailing extreme (longs paying richly)
- price z-score stretched above its rolling mean
- trend regime NOT up (do not fade clean trends)

Risk: ATR-multiple stop beyond the extension; take profit at the rolling
mean (the reversion target itself, not a fixed R).

Known failure modes: strong trends that keep paying funding for weeks,
short squeezes through the stop, regime filter lagging a fresh trend.
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

_REQUIRED = ("atr", "er", "funding_pct", "close_z", "close_mean")


class FundingMeanReversion(BaseStrategy):
    strategy_id = "funding_mean_reversion_v1"

    def __init__(
        self,
        funding: pd.DataFrame,
        *,
        funding_pct_window: int = 240,
        extreme_pct: float = 0.90,
        z_window: int = 48,
        z_entry: float = 2.0,
        stop_atr_mult: float = 1.5,
        allowed_sides: tuple[str, ...] | list[str] = ("long", "short"),
        regime: RegimeParams = RegimeParams(),
    ) -> None:
        if funding is None or funding.empty:
            raise ValueError(
                "FundingMeanReversion requires a funding-rate series — "
                "the funding skew IS the hypothesis"
            )
        self.funding = funding
        self.funding_pct_window = funding_pct_window
        self.extreme_pct = extreme_pct
        self.z_window = z_window
        self.z_entry = z_entry
        self.stop_atr_mult = stop_atr_mult
        # Side restriction (e.g. short_only) — the attribution finding on BTC
        # showed the edge is short-side-carried. A restricted variant is a
        # pre-registered candidate, never an in-place tweak to the trial.
        self.allowed_sides = tuple(allowed_sides)
        self.regime = regime
        self.warmup_bars = max(
            funding_pct_window, z_window, regime_warmup_bars(regime)
        )

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        df = add_regime_columns(candles, self.regime)
        df = merge_funding(df, self.funding)
        df["funding_pct"] = rolling_percentile(df["funding_rate"], self.funding_pct_window)
        df["close_z"] = zscore(df["close"], self.z_window)
        df["close_mean"] = sma(df["close"], self.z_window)
        return df

    def synthesize_exit_plan(
        self, df: pd.DataFrame, index: int, side: str, entry_price: float
    ) -> SignalIntent | None:
        """Same exit geometry as signal(): stop = entry ± stop_atr_mult*ATR,
        target = the rolling close mean (the reversion target refreshes with
        the data, exactly as a fresh signal would compute it)."""
        row = df.iloc[index]
        atr = float(row["atr"])
        mean = float(row["close_mean"])
        if not (atr > 0) or not (mean > 0):
            return None
        stop_dist = self.stop_atr_mult * atr
        if side == "short":
            return SignalIntent(
                "short", stop_price=entry_price + stop_dist,
                take_profit_price=mean,
                reason=f"exit plan rebuilt on resume: stop {entry_price + stop_dist:.2f}, target mean {mean:.2f}",
            )
        return SignalIntent(
            "long", stop_price=max(entry_price - stop_dist, 1e-9),
            take_profit_price=mean,
            reason=f"exit plan rebuilt on resume: stop {entry_price - stop_dist:.2f}, target mean {mean:.2f}",
        )

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        row = df.iloc[index]
        if any(math.isnan(float(row[c])) for c in _REQUIRED):
            return None

        close = float(row["close"])
        mean = float(row["close_mean"])
        stop_dist = self.stop_atr_mult * float(row["atr"])
        if stop_dist <= 0:
            return None

        # Fade crowded longs: rich funding + stretched price, no up-trend.
        if (
            "short" in self.allowed_sides
            and float(row["funding_pct"]) >= self.extreme_pct
            and float(row["close_z"]) >= self.z_entry
            and not row["regime_trend_up"]
            and mean < close
        ):
            return SignalIntent(
                side="short",
                stop_price=close + stop_dist,
                take_profit_price=mean,
                reason=(
                    f"crowded longs: funding_pct={float(row['funding_pct']):.2f}, "
                    f"z={float(row['close_z']):+.2f}, target mean {mean:.2f}"
                ),
            )
        # Fade crowded shorts: deeply negative funding + stretched down.
        if (
            "long" in self.allowed_sides
            and float(row["funding_pct"]) <= 1.0 - self.extreme_pct
            and float(row["close_z"]) <= -self.z_entry
            and not row["regime_trend_down"]
            and mean > close
        ):
            return SignalIntent(
                side="long",
                stop_price=close - stop_dist,
                take_profit_price=mean,
                reason=(
                    f"crowded shorts: funding_pct={float(row['funding_pct']):.2f}, "
                    f"z={float(row['close_z']):+.2f}, target mean {mean:.2f}"
                ),
            )
        return None
