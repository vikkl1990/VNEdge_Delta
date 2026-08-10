"""Range-distributed volume profile over a bar slice.

Concept extracted from WillyAlgoTrader's Liquidity Trail Matrix (public
TradingView description, 2026): rebuild a volume profile over the CURRENT
trend segment, distributing each bar's volume across price bins by overlap
fraction, then derive POC / value area / high- and low-volume nodes. LVNs are
"volume vacuums" — the candle-scale cousin of the L2 liquidity-vacuum
hypothesis already in the alpha factory.

Pure functions over an explicit slice — causal by construction: callers pass
only bars they are allowed to see.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class ProfileLevels:
    poc: float                    # highest-volume bin center
    vah: float                    # value-area high
    val: float                    # value-area low
    hvns: tuple[float, ...]       # local acceptance shelves
    lvns: tuple[float, ...]       # local volume vacuums
    bin_width: float


def range_distributed_profile(
    bars: pd.DataFrame, *, bins: int = 24
) -> tuple[np.ndarray, np.ndarray]:
    """Distribute each bar's volume across ``bins`` by price-range overlap.

    bin_volume += bar_volume * overlap(bin, bar_range) / (high - low).
    Zero-range bars drop their volume entirely into the containing bin.
    Zero-volume data falls back to a range-weighted proxy (1.0 per bar) so
    the profile still describes where price SPENT TIME.
    """
    if bars.empty or bins < 2:
        return np.array([]), np.array([])
    lo = float(bars["low"].min())
    hi = float(bars["high"].max())
    if not (hi > lo):
        return np.array([]), np.array([])
    edges = np.linspace(lo, hi, bins + 1)
    volumes = np.zeros(bins)
    vol_col = bars["volume"].to_numpy(dtype=float)
    if not np.isfinite(vol_col).any() or vol_col.sum() <= 0:
        vol_col = np.ones(len(bars))  # range-weighted proxy
    lows = bars["low"].to_numpy(dtype=float)
    highs = bars["high"].to_numpy(dtype=float)
    for bar_lo, bar_hi, bar_vol in zip(lows, highs, vol_col):
        if not np.isfinite(bar_vol) or bar_vol <= 0:
            continue
        if bar_hi <= bar_lo:
            idx = min(int((bar_lo - lo) / (hi - lo) * bins), bins - 1)
            volumes[idx] += bar_vol
            continue
        overlap_lo = np.maximum(edges[:-1], bar_lo)
        overlap_hi = np.minimum(edges[1:], bar_hi)
        overlap = np.clip(overlap_hi - overlap_lo, 0.0, None)
        volumes += bar_vol * overlap / (bar_hi - bar_lo)
    return edges, volumes


def profile_levels(
    edges: np.ndarray, volumes: np.ndarray,
    *, value_area_pct: float = 0.70,
    hvn_ratio: float = 0.55, lvn_ratio: float = 0.30,
) -> ProfileLevels | None:
    """Derive POC / value area / HVN / LVN from a computed profile."""
    if len(volumes) < 3 or volumes.sum() <= 0:
        return None
    centers = (edges[:-1] + edges[1:]) / 2.0
    poc_idx = int(np.argmax(volumes))
    total = volumes.sum()

    # symmetric expansion from POC until value_area_pct of volume enclosed
    lo_i = hi_i = poc_idx
    enclosed = volumes[poc_idx]
    while enclosed < value_area_pct * total and (lo_i > 0 or hi_i < len(volumes) - 1):
        next_lo = volumes[lo_i - 1] if lo_i > 0 else -1.0
        next_hi = volumes[hi_i + 1] if hi_i < len(volumes) - 1 else -1.0
        if next_hi >= next_lo:
            hi_i += 1
            enclosed += max(next_hi, 0.0)
        else:
            lo_i -= 1
            enclosed += max(next_lo, 0.0)

    poc_vol = volumes[poc_idx]
    hvns, lvns = [], []
    for i in range(1, len(volumes) - 1):
        if i == poc_idx:
            continue
        local_peak = volumes[i] >= volumes[i - 1] and volumes[i] >= volumes[i + 1]
        local_trough = volumes[i] <= volumes[i - 1] and volumes[i] <= volumes[i + 1]
        if local_peak and volumes[i] >= hvn_ratio * poc_vol:
            hvns.append(float(centers[i]))
        elif local_trough and volumes[i] <= lvn_ratio * poc_vol and lo_i <= i <= hi_i:
            lvns.append(float(centers[i]))

    return ProfileLevels(
        poc=float(centers[poc_idx]),
        vah=float(edges[hi_i + 1]),
        val=float(edges[lo_i]),
        hvns=tuple(hvns),
        lvns=tuple(lvns),
        bin_width=float(edges[1] - edges[0]),
    )
