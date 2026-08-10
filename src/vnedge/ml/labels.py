"""Triple-barrier labels for the first ML target.

Label at bar i answers: "if we go long at the NEXT bar's open with an
ATR-multiple stop and an R-multiple target, does the target get hit before
the stop within `horizon_bars`?"

- 1.0  target touched first (stop-first tie-breaking within a bar, same
       conservative rule the backtester uses)
- 0.0  stop touched first, or horizon expires
- NaN  label not computable (warmup ATR, or the horizon extends past the end
       of data — the caller must PURGE these; they are also why walk-forward
       training must embargo the last `horizon_bars` of every train window)

Labels intentionally look into the future — that is their job. Leakage
discipline lives in the split, not the label.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from vnedge.strategy.indicators import atr as atr_indicator


def triple_barrier_labels(
    candles: pd.DataFrame,
    *,
    stop_atr_mult: float = 2.0,
    target_r: float = 2.0,
    horizon_bars: int = 24,
    atr_window: int = 24,
) -> pd.Series:
    n = len(candles)
    atr = atr_indicator(candles, atr_window).to_numpy()
    opens = candles["open"].to_numpy()
    highs = candles["high"].to_numpy()
    lows = candles["low"].to_numpy()
    labels = np.full(n, np.nan)

    for i in range(n):
        if i + 1 >= n or i + horizon_bars >= n or np.isnan(atr[i]) or atr[i] <= 0:
            continue
        entry = opens[i + 1]
        stop = entry - stop_atr_mult * atr[i]
        target = entry + target_r * stop_atr_mult * atr[i]
        label = 0.0
        for j in range(i + 1, i + 1 + horizon_bars):
            if lows[j] <= stop:  # stop first — conservative tie-break
                label = 0.0
                break
            if highs[j] >= target:
                label = 1.0
                break
        labels[i] = label
    return pd.Series(labels, index=candles.index, name="label")
