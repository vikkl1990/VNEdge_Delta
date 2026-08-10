"""Market regime classification.

Adds boolean regime columns used as strategy filters:

- ``regime_trend_up`` / ``regime_trend_down``: efficiency ratio above a
  threshold (price moving cleanly, not chopping) AND fast EMA on the
  corresponding side of the slow EMA.
- ``atr_pct``: percentile of current ATR within its trailing window — the
  volatility regime dial (strategies bound it from either side).

Neither column is a signal by itself; per the strategy design rules, regimes
gate signals, they don't generate them.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from vnedge.strategy.indicators import atr, efficiency_ratio, ema, rolling_percentile


@dataclass(frozen=True)
class RegimeParams:
    ema_fast: int = 24
    ema_slow: int = 96
    er_window: int = 48
    er_trend_min: float = 0.30
    atr_window: int = 24
    atr_pct_window: int = 240


def regime_warmup_bars(p: RegimeParams) -> int:
    """Bars needed before every regime column is NaN-free."""
    return max(p.ema_slow, p.er_window + 1, p.atr_window + p.atr_pct_window)


def add_regime_columns(candles: pd.DataFrame, p: RegimeParams) -> pd.DataFrame:
    df = candles.copy()
    df["atr"] = atr(df, p.atr_window)
    df["atr_pct"] = rolling_percentile(df["atr"], p.atr_pct_window)
    df["er"] = efficiency_ratio(df["close"], p.er_window)
    fast = ema(df["close"], p.ema_fast)
    slow = ema(df["close"], p.ema_slow)
    trending = df["er"] >= p.er_trend_min  # NaN compares False -> not trending
    df["regime_trend_up"] = trending & (fast > slow)
    df["regime_trend_down"] = trending & (fast < slow)
    return df


def merge_funding(candles: pd.DataFrame, funding: pd.DataFrame | None) -> pd.DataFrame:
    """Attach the last-known funding rate to each bar (backward as-of join —
    strictly causal: a bar only ever sees funding already printed). Bars
    before the first funding event get 0.0."""
    df = candles.copy()
    if funding is None or funding.empty:
        df["funding_rate"] = 0.0
        return df
    merged = pd.merge_asof(
        df, funding[["timestamp", "funding_rate"]], on="timestamp", direction="backward"
    )
    merged["funding_rate"] = merged["funding_rate"].fillna(0.0)
    return merged
