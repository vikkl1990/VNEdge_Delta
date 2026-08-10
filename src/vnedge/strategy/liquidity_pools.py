"""Liquidity pool detection + 4-factor strength scoring.

Concept extracted from WillyAlgoTrader's Liquidity Pools Pro (public
TradingView description, 2026), filling the documented gap #3 in
docs/LUXALGO_EDGE_EXTRACTION.md ("liquidity-pool pressure"): not all
liquidity is equal — pools are graded by touches, recency, volume at level
and HTF confluence, and carry a mitigation lifecycle.

CAUSALITY: a pivot with right-lookback R is only KNOWN R bars after it
prints. Every pool records ``confirmed_at`` (the bar index where the pivot
became knowable) and detection honours it — features derived from these pools
must only consult pools with confirmed_at <= current index.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd


@dataclass
class LiquidityPool:
    price: float                  # volume-weighted level of clustered pivots
    side: str                     # "high" (resting sell-side stops above) | "low"
    touch_indices: list[int] = field(default_factory=list)
    touch_volume: float = 0.0
    confirmed_at: int = 0         # bar index when the LAST pivot became knowable
    mitigated_at: int | None = None  # wick pierced AND closed back inside

    def strength(self, now_index: int, *, half_life_bars: int = 150,
                 htf_aligned: bool = False) -> float:
        """0-100 pool strength (WillyAlgoTrader formula, verbatim weights):
        touches up to 35 (9 pts each), recency up to 35 (exponential decay),
        volume up to 20 (logarithmic), HTF confluence +20 bonus; capped 100."""
        touches = min(35.0, 9.0 * len(self.touch_indices))
        age = max(0, now_index - (self.touch_indices[-1] if self.touch_indices else now_index))
        recency = 35.0 * math.exp(-math.log(2) * age / max(half_life_bars, 1))
        volume = min(20.0, 4.0 * math.log1p(self.touch_volume)) if self.touch_volume > 0 else 0.0
        bonus = 20.0 if htf_aligned else 0.0
        return min(100.0, touches + recency + volume + bonus)


def _pivots(df: pd.DataFrame, col: str, left: int, right: int, highest: bool) -> list[int]:
    """Indices of confirmed symmetric pivots. Availability lag = ``right``."""
    vals = df[col].to_numpy(dtype=float)
    out = []
    for i in range(left, len(vals) - right):
        window = vals[i - left:i + right + 1]
        if highest and vals[i] == window.max() and (window == vals[i]).sum() == 1:
            out.append(i)
        elif not highest and vals[i] == window.min() and (window == vals[i]).sum() == 1:
            out.append(i)
    return out


def detect_pools(
    df: pd.DataFrame, *, pivot_length: int = 10,
    atr_col: str = "atr", tolerance_atr: float = 0.25,
) -> list[LiquidityPool]:
    """Cluster confirmed pivots into pools: two pivots are the same pool when
    their distance <= ATR * tolerance (scale-invariant equality)."""
    pools: list[LiquidityPool] = []
    for side, col, highest in (("high", "high", True), ("low", "low", False)):
        for idx in _pivots(df, col, pivot_length, pivot_length, highest):
            price = float(df[col].iloc[idx])
            tol = tolerance_atr * float(df[atr_col].iloc[idx]) if atr_col in df else 0.0
            vol = float(df["volume"].iloc[idx]) if "volume" in df else 0.0
            confirmed = idx + pivot_length
            for pool in pools:
                if pool.side == side and abs(pool.price - price) <= max(tol, 1e-12):
                    n = len(pool.touch_indices)
                    pool.price = (pool.price * n + price) / (n + 1)
                    pool.touch_indices.append(idx)
                    pool.touch_volume += vol
                    pool.confirmed_at = max(pool.confirmed_at, confirmed)
                    break
            else:
                pools.append(LiquidityPool(
                    price=price, side=side, touch_indices=[idx],
                    touch_volume=vol, confirmed_at=confirmed,
                ))
    return pools


def update_mitigation(pools: list[LiquidityPool], df: pd.DataFrame) -> None:
    """Mark pools MITIGATED where a bar's wick pierced the level and the bar
    closed back inside — the classic failed-breakout / liquidity-grab print."""
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    closes = df["close"].to_numpy(dtype=float)
    for pool in pools:
        if pool.mitigated_at is not None:
            continue
        start = pool.confirmed_at
        for i in range(start, len(df)):
            if pool.side == "high" and highs[i] > pool.price and closes[i] < pool.price:
                pool.mitigated_at = i
                break
            if pool.side == "low" and lows[i] < pool.price and closes[i] > pool.price:
                pool.mitigated_at = i
                break
