"""Frozen causal change-point detection for closed-candle regime research."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from statistics import fmean, pstdev

from vnedge.scalping.delta_engine.types import Candle, ChangePointProfile


@dataclass(frozen=True)
class CausalCusumConfig:
    """Parameters are configuration-owned and identical in live and replay."""

    source_timeframe: str = "5m"
    minimum_history_bars: int = 50
    baseline_window_bars: int = 200
    drift_z: float = 0.50
    threshold_z: float = 8.0
    cooldown_bars: int = 6

    def __post_init__(self) -> None:
        if self.source_timeframe not in {"1m", "5m"}:
            raise ValueError("CUSUM timeframe must be 1m or 5m")
        if self.minimum_history_bars < 20:
            raise ValueError("CUSUM minimum history must be at least 20 bars")
        if self.baseline_window_bars < self.minimum_history_bars:
            raise ValueError("CUSUM baseline window must cover minimum history")
        if self.drift_z <= 0 or self.threshold_z <= self.drift_z:
            raise ValueError("CUSUM threshold must exceed positive drift")
        if self.cooldown_bars < 0:
            raise ValueError("CUSUM cooldown cannot be negative")


@dataclass
class _CusumState:
    positive: float = 0.0
    negative: float = 0.0

    def update(self, z_score: float, drift: float) -> float:
        self.positive = max(0.0, self.positive + z_score - drift)
        self.negative = min(0.0, self.negative + z_score + drift)
        return max(self.positive, -self.negative)

    def reset(self) -> None:
        self.positive = 0.0
        self.negative = 0.0


class CausalCusumDetector:
    """Sequential two-sided CUSUM using only observations before each bar.

    The baseline mean and scale are calculated before the current observation
    is appended. Repeated context builds for the same closed candle are
    idempotent, which preserves live/replay parity.
    """

    def __init__(self, config: CausalCusumConfig | None = None) -> None:
        self.config = config or CausalCusumConfig()
        self._return_history: deque[float] = deque(
            maxlen=self.config.baseline_window_bars
        )
        self._volatility_history: deque[float] = deque(
            maxlen=self.config.baseline_window_bars
        )
        self._return_state = _CusumState()
        self._volatility_state = _CusumState()
        self._last_ts = None
        self._previous_close: float | None = None
        self._cooldown = 0
        self._bars_since_shift: int | None = None
        self._profile = ChangePointProfile(
            source_timeframe=self.config.source_timeframe
        )

    @staticmethod
    def _z_score(value: float, history: deque[float]) -> float:
        baseline = list(history)
        mean = fmean(baseline)
        scale = pstdev(baseline)
        if scale <= 1e-12:
            return 0.0
        return max(-12.0, min(12.0, (value - mean) / scale))

    def update(self, rows: tuple[Candle, ...]) -> ChangePointProfile:
        if not rows or (
            self._last_ts is not None and rows[-1].ts <= self._last_ts
        ):
            return self._profile
        start = 0
        if self._last_ts is not None:
            for index in range(len(rows) - 1, -1, -1):
                if rows[index].ts <= self._last_ts:
                    start = index + 1
                    break
        for row in rows[start:]:
            if row.tf != self.config.source_timeframe:
                raise ValueError("CUSUM candle timeframe does not match configuration")
            self._process(row)
        return self._profile

    def _process(self, row: Candle) -> None:
        previous = self._previous_close if self._previous_close is not None else row.open
        return_bps = math.log(row.close / previous) * 10_000.0
        true_range = max(
            row.high - row.low,
            abs(row.high - previous),
            abs(row.low - previous),
        )
        range_bps = true_range / row.close * 10_000.0
        log_range_bps = math.log(max(range_bps, 1e-9))
        ready = (
            len(self._return_history) >= self.config.minimum_history_bars
            and len(self._volatility_history) >= self.config.minimum_history_bars
        )
        return_score = 0.0
        volatility_score = 0.0
        return_shift = False
        volatility_shift = False
        if ready and self._cooldown == 0:
            return_score = self._return_state.update(
                self._z_score(return_bps, self._return_history),
                self.config.drift_z,
            )
            volatility_score = self._volatility_state.update(
                self._z_score(log_range_bps, self._volatility_history),
                self.config.drift_z,
            )
            return_shift = return_score >= self.config.threshold_z
            volatility_shift = volatility_score >= self.config.threshold_z
        regime_shift = return_shift or volatility_shift
        if regime_shift:
            self._return_state.reset()
            self._volatility_state.reset()
            self._cooldown = self.config.cooldown_bars
            self._bars_since_shift = 0
        elif self._cooldown > 0:
            self._cooldown -= 1
            self._return_state.reset()
            self._volatility_state.reset()
            if self._bars_since_shift is not None:
                self._bars_since_shift += 1
        elif self._bars_since_shift is not None:
            self._bars_since_shift += 1
        self._return_history.append(return_bps)
        self._volatility_history.append(log_range_bps)
        self._previous_close = row.close
        self._last_ts = row.ts
        minutes_per_bar = 1 if self.config.source_timeframe == "1m" else 5
        minutes_since_shift = (
            self._bars_since_shift * minutes_per_bar
            if self._bars_since_shift is not None
            else None
        )
        self._profile = ChangePointProfile(
            source_timeframe=self.config.source_timeframe,
            detector_ready=ready,
            regime_shift=regime_shift,
            return_shift=return_shift,
            volatility_shift=volatility_shift,
            return_score=return_score,
            volatility_score=volatility_score,
            bars_since_shift=self._bars_since_shift,
            minutes_since_shift=minutes_since_shift,
        )
