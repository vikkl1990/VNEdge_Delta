"""Incremental, causal swing confirmation for event-driven research.

This is an additive streaming adapter. It is deliberately not wired into the
frozen ``continuous_mtf_alignment_v2`` experiment, whose minimum-swing rule is
based on excursion from an opposite swing rather than local candle range.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from vnedge.scalping.delta_engine.mechanical_structure import ConfirmedSwing
from vnedge.scalping.delta_engine.types import Candle

SwingType = Literal["high", "low"]


@dataclass(frozen=True)
class IncrementalSwing:
    swing_id: str
    timeframe: str
    kind: SwingType
    price: float
    ts: datetime
    confirmed_at: datetime
    pivot_index: int
    confirmed_index: int
    bar_high: float
    bar_low: float
    strength_bps: float

    @property
    def type(self) -> SwingType:
        """Compatibility alias for the submitted ``Swing.type`` interface."""
        return self.kind

    def to_confirmed_swing(self) -> ConfirmedSwing:
        return ConfirmedSwing(
            swing_id=self.swing_id,
            timeframe=self.timeframe,
            kind=self.kind,
            ts=self.ts,
            confirmed_at=self.confirmed_at,
            price=self.price,
            candle_index=self.pivot_index,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "swing_id": self.swing_id,
            "timeframe": self.timeframe,
            "type": self.kind,
            "price": self.price,
            "ts": self.ts.isoformat(),
            "confirmed_at": self.confirmed_at.isoformat(),
            "pivot_index": self.pivot_index,
            "confirmed_index": self.confirmed_index,
            "bar_high": self.bar_high,
            "bar_low": self.bar_low,
            "strength_bps": self.strength_bps,
        }


@dataclass
class IncrementalSwingTracker:
    """Confirm pivots once their right-hand closed bars are available.

    ``swings`` is the active, same-type-deduplicated structural sequence.
    ``confirmed_archive`` retains every accepted confirmation, including a
    swing later superseded by a more extreme same-type swing. This preserves
    an audit trail while keeping the active sequence compact.
    """

    timeframe: str
    left: int = 3
    right: int = 3
    min_swing_bps: float = 8.0
    max_swings: int = 30
    max_confirmations: int = 120
    _window: deque[tuple[int, Candle]] = field(init=False, repr=False)
    _swings: list[IncrementalSwing] = field(init=False, default_factory=list, repr=False)
    _archive: deque[IncrementalSwing] = field(init=False, repr=False)
    _next_index: int = field(init=False, default=0, repr=False)
    _last_ts: datetime | None = field(init=False, default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.timeframe:
            raise ValueError("timeframe is required")
        if self.left < 1 or self.right < 1:
            raise ValueError("left and right confirmation windows must be positive")
        if self.min_swing_bps < 0:
            raise ValueError("minimum swing strength cannot be negative")
        if self.max_swings < 1 or self.max_confirmations < self.max_swings:
            raise ValueError("invalid swing history limits")
        self._window = deque(maxlen=self.left + self.right + 1)
        self._archive = deque(maxlen=self.max_confirmations)

    @property
    def swings(self) -> tuple[IncrementalSwing, ...]:
        return tuple(self._swings)

    @property
    def confirmed_archive(self) -> tuple[IncrementalSwing, ...]:
        return tuple(self._archive)

    def reset(self) -> None:
        self._window.clear()
        self._swings.clear()
        self._archive.clear()
        self._next_index = 0
        self._last_ts = None

    def update(self, candle: Candle) -> tuple[IncrementalSwing, ...]:
        """Ingest exactly one closed candle and return accepted confirmations."""
        if candle.tf != self.timeframe:
            raise ValueError(
                f"tracker timeframe {self.timeframe!r} cannot ingest {candle.tf!r} candle"
            )
        if self._last_ts is not None and candle.ts <= self._last_ts:
            raise ValueError("swing tracker requires unique ascending candle timestamps")
        absolute_index = self._next_index
        self._next_index += 1
        self._last_ts = candle.ts
        self._window.append((absolute_index, candle))
        if len(self._window) < self.left + self.right + 1:
            return ()

        window = tuple(self._window)
        pivot_position = len(window) - self.right - 1
        pivot_index, pivot = window[pivot_position]
        candidates: list[tuple[SwingType, float]] = []
        if self._is_pivot_high(window, pivot_position):
            candidates.append(("high", float(pivot.high)))
        if self._is_pivot_low(window, pivot_position):
            candidates.append(("low", float(pivot.low)))

        confirmed: list[IncrementalSwing] = []
        strength = self._local_range_strength_bps(pivot)
        if strength < self.min_swing_bps:
            return ()
        for kind, price in candidates:
            swing = IncrementalSwing(
                swing_id=(
                    f"{self.timeframe}:{kind}:{pivot.ts.isoformat()}:{price:.12g}"
                ),
                timeframe=self.timeframe,
                kind=kind,
                price=price,
                ts=pivot.ts,
                confirmed_at=candle.ts,
                pivot_index=pivot_index,
                confirmed_index=absolute_index,
                bar_high=float(pivot.high),
                bar_low=float(pivot.low),
                strength_bps=strength,
            )
            if self._accept(swing):
                confirmed.append(swing)
        return tuple(confirmed)

    def last_swing(self, kind: SwingType | None = None) -> IncrementalSwing | None:
        if kind is None:
            return self._swings[-1] if self._swings else None
        return next((swing for swing in reversed(self._swings) if swing.kind == kind), None)

    def recent_swings(self, count: int = 5) -> tuple[IncrementalSwing, ...]:
        if count < 0:
            raise ValueError("recent swing count cannot be negative")
        return tuple(self._swings[-count:]) if count else ()

    def confirmed_for_structure(self) -> tuple[ConfirmedSwing, ...]:
        """Adapt the active sequence for the existing BOS/CHOCH functions."""
        return tuple(swing.to_confirmed_swing() for swing in self._swings)

    def _accept(self, swing: IncrementalSwing) -> bool:
        if self._swings and self._swings[-1].kind == swing.kind:
            previous = self._swings[-1]
            is_more_extreme = (
                swing.price > previous.price
                if swing.kind == "high"
                else swing.price < previous.price
            )
            if not is_more_extreme:
                return False
            self._swings[-1] = swing
        else:
            self._swings.append(swing)
            if len(self._swings) > self.max_swings:
                self._swings.pop(0)
        self._archive.append(swing)
        return True

    def _is_pivot_high(
        self, window: Sequence[tuple[int, Candle]], pivot_position: int
    ) -> bool:
        price = window[pivot_position][1].high
        return all(
            row.high < price
            for position, (_, row) in enumerate(window)
            if position != pivot_position
        )

    def _is_pivot_low(
        self, window: Sequence[tuple[int, Candle]], pivot_position: int
    ) -> bool:
        price = window[pivot_position][1].low
        return all(
            row.low > price
            for position, (_, row) in enumerate(window)
            if position != pivot_position
        )

    @staticmethod
    def _local_range_strength_bps(candle: Candle) -> float:
        mid = (float(candle.high) + float(candle.low)) / 2.0
        return (float(candle.high) - float(candle.low)) / mid * 10_000.0 if mid > 0 else 0.0

