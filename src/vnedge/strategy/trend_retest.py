"""Trend retest v1 — graded pullback-reclaim entries inside a trend.

Concept extracted from WillyAlgoTrader's Liquidity Trail Matrix (public
TradingView description, 2026). Where trend_continuation_v1 buys BREAKOUTS,
this enters on RETESTS: price pulls back into a stack of ratcheting ATR bands
below/above the trend anchor, then reclaims the first band — and the entry
only fires when a 5-factor quality score clears a threshold:

    pullback depth   (25) — mid-depth sweeps score highest (band2 > band3 > 1 > 4)
    reclaim candle   (20) — close-location-value of the reclaim bar
    volume           (20) — reclaim volume vs its 20-SMA
    HTF bias         (20) — slow-EMA alignment (single-frame proxy for HTF EMA-50)
    trend age        (15) — 10-150 bars scores full; young/stale trends score low

Optional 6th factor (off by default, catalog-enabled): a bonus when the
pullback swept a STRONG liquidity pool (liquidity_pools.strength >= 60) and
reclaimed — the "grab + reclaim" print.

Volume-profile features (POC distance, LVN proximity) are computed on the
current trend segment at retest bars only and exposed as columns for research
and lane_eval telemetry; v1 does not gate on them.

Deviations from the source, recorded honestly: HTF bias uses the same-frame
slow EMA (we run single-timeframe frames in research); the 5-bar signal
cooldown is omitted (in-position suppression covers it in both backtest and
live paths). Everything is causal: bands ratchet forward-only, pivots honour
their confirmation lag, and the causality unit test pins it.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent
from vnedge.strategy.indicators import atr, ema, sma
from vnedge.strategy.liquidity_pools import detect_pools, update_mitigation
from vnedge.strategy.regime import (
    RegimeParams,
    add_regime_columns,
    merge_funding,
    regime_warmup_bars,
)
from vnedge.strategy.volume_profile import profile_levels, range_distributed_profile

_REQUIRED = ("atr", "er", "atr_pct")
_BAND_MULTS = (4.0, 5.0, 6.0, 7.0)          # Balanced preset spacing
_DEPTH_POINTS = {1: 15.0, 2: 25.0, 3: 18.0, 4: 10.0}


class TrendRetest(BaseStrategy):
    strategy_id = "trend_retest_v1"

    def __init__(
        self,
        funding: pd.DataFrame | None = None,
        *,
        min_score: float = 80.0,
        retest_window: int = 8,
        atr_window: int = 13,
        take_profit_r: float = 2.0,
        stop_mode: str = "wick",          # "wick" | "atr"
        stop_atr_mult: float = 1.5,       # used by stop_mode="atr" + wick floor
        max_funding_against: float = 0.0005,
        min_er: float = 0.25,             # SATS-style efficiency gate: no chop
        use_pool_bonus: bool = False,
        pool_min_strength: float = 60.0,
        regime: RegimeParams = RegimeParams(),
    ) -> None:
        if stop_mode not in ("wick", "atr"):
            raise ValueError(f"unknown stop_mode: {stop_mode!r}")
        self.funding = funding
        self.min_score = min_score
        self.retest_window = retest_window
        self.atr_window = atr_window
        self.take_profit_r = take_profit_r
        self.stop_mode = stop_mode
        self.stop_atr_mult = stop_atr_mult
        self.max_funding_against = max_funding_against
        self.min_er = min_er
        self.use_pool_bonus = use_pool_bonus
        self.pool_min_strength = pool_min_strength
        self.regime = regime
        self.warmup_bars = max(60, regime_warmup_bars(regime))

    # -- prepare -------------------------------------------------------------
    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        df = add_regime_columns(candles, self.regime)
        df["atr13"] = atr(df, self.atr_window)
        df["ema_slow_bias"] = ema(df["close"], 200)
        df["vol_sma20"] = sma(df["volume"], 20)

        n = len(df)
        close = df["close"].to_numpy(dtype=float)
        high = df["high"].to_numpy(dtype=float)
        low = df["low"].to_numpy(dtype=float)
        atr13 = df["atr13"].to_numpy(dtype=float)
        ema50 = ema(df["close"], 50).to_numpy(dtype=float)

        # Self-referential trend state machine (supertrend semantics, faithful
        # to the source): the band stack DEFINES the trend. Bands ratchet
        # forward-only; the trend flips only when close crosses the OUTERMOST
        # band, so pullbacks can touch inner bands without ending the trend —
        # deriving direction from the EMA regime instead kills nearly every
        # retest (the regime flips before price reaches the stack).
        direction = np.zeros(n, dtype=int)         # +1 up, -1 down, 0 warmup
        trend_start = np.full(n, -1, dtype=int)
        bands = np.full((n, len(_BAND_MULTS)), np.nan)
        cur_dir, cur_start = 0, -1
        levels = [math.nan] * len(_BAND_MULTS)

        def _reset(i: int, d: int) -> None:
            for k, mult in enumerate(_BAND_MULTS):
                levels[k] = close[i] - d * mult * atr13[i]

        for i in range(n):
            if not np.isfinite(atr13[i]) or not np.isfinite(ema50[i]):
                bands[i] = levels
                continue
            if cur_dir == 0:                        # seed from EMA-50 side
                cur_dir = 1 if close[i] >= ema50[i] else -1
                cur_start = i
                _reset(i, cur_dir)
            elif not math.isnan(levels[-1]) and (
                (cur_dir > 0 and close[i] < levels[-1])
                or (cur_dir < 0 and close[i] > levels[-1])
            ):
                cur_dir = -cur_dir                  # crossed the outermost band
                cur_start = i
                _reset(i, cur_dir)
            else:
                for k, mult in enumerate(_BAND_MULTS):
                    raw = close[i] - cur_dir * mult * atr13[i]
                    if math.isnan(levels[k]):
                        levels[k] = raw
                    elif cur_dir > 0:
                        levels[k] = max(levels[k], raw)   # ratchet up
                    else:
                        levels[k] = min(levels[k], raw)   # ratchet down
            direction[i] = cur_dir
            trend_start[i] = cur_start
            bands[i] = levels

        df["trend_dir"] = direction
        df["trend_age"] = np.where(trend_start >= 0, np.arange(n) - trend_start, 0)
        for k in range(len(_BAND_MULTS)):
            df[f"band_{k + 1}"] = bands[:, k]

        # deepest band touched inside the trailing retest window (prior bars)
        deepest = np.zeros(n, dtype=int)
        for i in range(n):
            if direction[i] == 0:
                continue
            d = 0
            lo_w = max(trend_start[i], i - self.retest_window)
            for j in range(lo_w, i + 1):
                for k in range(len(_BAND_MULTS) - 1, -1, -1):
                    b = bands[j, k]
                    if math.isnan(b):
                        continue
                    touched = low[j] <= b if direction[i] > 0 else high[j] >= b
                    if touched:
                        d = max(d, k + 1)
                        break
            deepest[i] = d
        df["deepest_band_touched"] = deepest

        # liquidity pools (optional bonus factor), causal via confirmed_at
        self._pools = []
        if self.use_pool_bonus:
            self._pools = detect_pools(df, atr_col="atr13")
            update_mitigation(self._pools, df)

        # volume-profile features at retest-capable bars only (sparse, cheap)
        df["dist_to_poc_bps"] = np.nan
        df["near_lvn"] = np.nan
        for i in range(n):
            if deepest[i] == 0 or direction[i] == 0 or trend_start[i] < 0:
                continue
            seg_start = max(trend_start[i], i - 360)
            seg = df.iloc[seg_start:i + 1]
            edges, vols = range_distributed_profile(seg)
            lv = profile_levels(edges, vols)
            if lv is None:
                continue
            df.iat[i, df.columns.get_loc("dist_to_poc_bps")] = (
                (close[i] - lv.poc) / lv.poc * 10_000.0
            )
            near = any(abs(close[i] - x) <= lv.bin_width for x in lv.lvns)
            df.iat[i, df.columns.get_loc("near_lvn")] = float(near)

        return merge_funding(df, self.funding)

    # -- scoring ------------------------------------------------------------
    def _score(self, df: pd.DataFrame, index: int) -> tuple[float, str]:
        row = df.iloc[index]
        d = int(row["trend_dir"])
        depth = _DEPTH_POINTS.get(int(row["deepest_band_touched"]), 0.0)

        hi, lo, cl = float(row["high"]), float(row["low"]), float(row["close"])
        clv = (cl - lo) / (hi - lo) if hi > lo else 0.5
        clv = clv if d > 0 else 1.0 - clv          # symmetric for shorts
        reclaim = 20.0 if clv > 0.7 else (12.0 if clv > 0.5 else 5.0)

        vol_ratio = (float(row["volume"]) / float(row["vol_sma20"])
                     if float(row["vol_sma20"]) > 0 else 0.0)
        volume = 20.0 if vol_ratio > 1.2 else (12.0 if vol_ratio > 1.0 else 5.0)

        bias_ok = (cl > float(row["ema_slow_bias"])) if d > 0 else (cl < float(row["ema_slow_bias"]))
        htf = 20.0 if bias_ok else 0.0

        age = int(row["trend_age"])
        age_pts = 15.0 if 10 <= age <= 150 else (8.0 if age < 10 else 5.0)

        score = depth + reclaim + volume + htf + age_pts
        parts = (f"depth={depth:.0f} reclaim={reclaim:.0f} vol={volume:.0f} "
                 f"bias={htf:.0f} age={age_pts:.0f}")

        if self.use_pool_bonus and self._pools:
            for pool in self._pools:
                if pool.confirmed_at > index:
                    continue  # not knowable yet — causality
                want = "low" if d > 0 else "high"
                swept = (pool.side == want and pool.mitigated_at is not None
                         and index - self.retest_window <= pool.mitigated_at <= index)
                if swept and pool.strength(index) >= self.pool_min_strength:
                    score = min(100.0, score + 10.0)
                    parts += " pool+10"
                    break
        return score, parts

    # -- signal ---------------------------------------------------------------
    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        row = df.iloc[index]
        if any(math.isnan(float(row[c])) for c in _REQUIRED):
            return None
        d = int(row["trend_dir"])
        if d == 0 or int(row["deepest_band_touched"]) == 0:
            return None
        if float(row["er"]) < self.min_er:
            return None  # efficiency gate: retests only count inside real trends
        band1 = float(row["band_1"])
        close = float(row["close"])
        if math.isnan(band1):
            return None
        reclaimed = close > band1 if d > 0 else close < band1
        if not reclaimed:
            return None
        fr = float(row["funding_rate"])
        if d > 0 and fr > self.max_funding_against:
            return None
        if d < 0 and fr < -self.max_funding_against:
            return None

        score, parts = self._score(df, index)
        if score < self.min_score:
            return None

        atr13 = float(row["atr13"])
        if self.stop_mode == "wick":
            raw = float(row["low"]) - 0.25 * atr13 if d > 0 else float(row["high"]) + 0.25 * atr13
            floor = 0.5 * self.stop_atr_mult * atr13
            dist = max(abs(close - raw), floor)
        else:
            dist = self.stop_atr_mult * atr13
        if dist <= 0 or close - d * dist <= 0:
            return None

        side = "long" if d > 0 else "short"
        return SignalIntent(
            side=side,
            stop_price=close - d * dist,
            take_profit_price=close + d * self.take_profit_r * dist,
            reason=(
                f"retest reclaim score {score:.0f}/{self.min_score:.0f} "
                f"({parts}); band1 {band1:.2f}, trend age {int(row['trend_age'])}"
            ),
        )
