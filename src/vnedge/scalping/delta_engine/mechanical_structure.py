"""Causal, non-repainting swing structure for BOS/CHOCH research.

Swings become visible only after their right-hand confirmation bars close.
Break events require a close beyond the latest confirmed swing of the required
type, and each confirmed swing can produce at most one break event.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

from vnedge.scalping.delta_engine.types import Candle


@dataclass(frozen=True)
class MechanicalStructureConfig:
    swing_left: int = 3
    swing_right: int = 3
    minimum_swing_bps: float = 8.0
    trend_lookback: int = 5
    event_freshness_hours: int = 24


def load_mechanical_structure_config(
    path: str | Path,
) -> MechanicalStructureConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError("continuous MTF contract must be a mapping")
    if raw.get("contract_id") != "continuous_mtf_alignment_v2":
        raise ValueError("mechanical structure requires continuous MTF v2")
    section = raw["state"]["mechanical_structure_1h"]
    config = MechanicalStructureConfig(
        swing_left=int(section["swing_left_bars"]),
        swing_right=int(section["swing_right_bars"]),
        minimum_swing_bps=float(section["minimum_swing_bps"]),
        trend_lookback=int(section["trend_lookback_swings"]),
        event_freshness_hours=int(section["event_freshness_hours"]),
    )
    if config.swing_left < 1 or config.swing_right < 1:
        raise ValueError("swing confirmation windows must be positive")
    if config.minimum_swing_bps < 0 or config.trend_lookback < 4:
        raise ValueError("invalid mechanical structure thresholds")
    return config


@dataclass(frozen=True)
class ConfirmedSwing:
    swing_id: str
    timeframe: str
    kind: str
    ts: datetime
    confirmed_at: datetime
    price: float
    candle_index: int

    def to_dict(self) -> dict[str, object]:
        return {
            "swing_id": self.swing_id,
            "timeframe": self.timeframe,
            "kind": self.kind,
            "ts": self.ts.isoformat(),
            "confirmed_at": self.confirmed_at.isoformat(),
            "price": self.price,
            "candle_index": self.candle_index,
        }


@dataclass(frozen=True)
class StructureEvent:
    event_id: str
    event_type: str
    direction: int
    ts: datetime
    candle_index: int
    close: float
    trend_before: int
    broken_swing: ConfirmedSwing

    def to_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "type": self.event_type,
            "direction": self.direction,
            "side": "up" if self.direction > 0 else "down",
            "ts": self.ts.isoformat(),
            "candle_index": self.candle_index,
            "close": self.close,
            "trend_before": self.trend_before,
            "level": self.broken_swing.price,
            "broken_swing_id": self.broken_swing.swing_id,
            "broken_swing_ts": self.broken_swing.ts.isoformat(),
            "broken_swing_confirmed_at": self.broken_swing.confirmed_at.isoformat(),
        }


@dataclass(frozen=True)
class StructureUpdate:
    swings: tuple[ConfirmedSwing, ...]
    trend: int
    event: StructureEvent | None


def _is_swing_high(rows: tuple[Candle, ...], index: int, left: int, right: int) -> bool:
    if index < left or index + right >= len(rows):
        return False
    price = rows[index].high
    return all(
        rows[other].high < price
        for other in range(index - left, index + right + 1)
        if other != index
    )


def _is_swing_low(rows: tuple[Candle, ...], index: int, left: int, right: int) -> bool:
    if index < left or index + right >= len(rows):
        return False
    price = rows[index].low
    return all(
        rows[other].low > price
        for other in range(index - left, index + right + 1)
        if other != index
    )


def confirmed_swings(
    candles: Collection[Candle],
    config: MechanicalStructureConfig,
) -> tuple[ConfirmedSwing, ...]:
    """Return append-stable swings visible at the latest candle close.

    ``minimum_swing_bps`` is measured from the most recent accepted swing of
    the opposite type. This makes the supplied "ignore tiny swings" rule fully
    deterministic without revising an already confirmed swing later.
    """
    rows = tuple(candles)
    if not rows:
        return ()
    timeframe = rows[-1].tf
    accepted: list[ConfirmedSwing] = []
    latest_by_kind: dict[str, ConfirmedSwing] = {}
    for index in range(config.swing_left, len(rows) - config.swing_right):
        candidates: list[tuple[str, float]] = []
        if _is_swing_high(rows, index, config.swing_left, config.swing_right):
            candidates.append(("high", float(rows[index].high)))
        if _is_swing_low(rows, index, config.swing_left, config.swing_right):
            candidates.append(("low", float(rows[index].low)))
        for kind, price in candidates:
            opposite = latest_by_kind.get("low" if kind == "high" else "high")
            if opposite is not None:
                excursion_bps = abs(price / opposite.price - 1.0) * 10_000.0
                if excursion_bps < config.minimum_swing_bps:
                    continue
            row = rows[index]
            confirmed_at = rows[index + config.swing_right].ts
            swing = ConfirmedSwing(
                swing_id=f"{timeframe}:{kind}:{row.ts.isoformat()}:{price:.12g}",
                timeframe=timeframe,
                kind=kind,
                ts=row.ts,
                confirmed_at=confirmed_at,
                price=price,
                candle_index=index,
            )
            accepted.append(swing)
            latest_by_kind[kind] = swing
    return tuple(accepted)


def current_swing_trend(
    swings: Collection[ConfirmedSwing],
    lookback: int = 5,
) -> int:
    recent = tuple(swings)[-lookback:]
    highs = [swing for swing in recent if swing.kind == "high"]
    lows = [swing for swing in recent if swing.kind == "low"]
    if len(highs) < 2 or len(lows) < 2:
        return 0
    if highs[-1].price > highs[-2].price and lows[-1].price > lows[-2].price:
        return 1
    if highs[-1].price < highs[-2].price and lows[-1].price < lows[-2].price:
        return -1
    return 0


def latest_swing(
    swings: Collection[ConfirmedSwing], kind: str
) -> ConfirmedSwing | None:
    return next((swing for swing in reversed(tuple(swings)) if swing.kind == kind), None)


def detect_structure_event(
    candles: Collection[Candle],
    swings: Collection[ConfirmedSwing],
    trend: int,
    *,
    already_broken_swing_ids: Collection[str] = (),
) -> StructureEvent | None:
    """Detect one close-only BOS or CHOCH against type-correct swing levels."""
    rows = tuple(candles)
    if not rows or trend == 0:
        return None
    latest = rows[-1]
    if trend > 0:
        bos_swing = latest_swing(swings, "high")
        choch_swing = latest_swing(swings, "low")
        tests = ((bos_swing, "bos", 1, latest.close > (bos_swing.price if bos_swing else 0)),
                 (choch_swing, "choch", -1, latest.close < (choch_swing.price if choch_swing else 0)))
    else:
        bos_swing = latest_swing(swings, "low")
        choch_swing = latest_swing(swings, "high")
        tests = ((bos_swing, "bos", -1, latest.close < (bos_swing.price if bos_swing else 0)),
                 (choch_swing, "choch", 1, latest.close > (choch_swing.price if choch_swing else 0)))
    broken = set(already_broken_swing_ids)
    for swing, event_type, direction, crossed in tests:
        if swing is None or not crossed or swing.swing_id in broken:
            continue
        return StructureEvent(
            event_id=f"{event_type}:{swing.swing_id}:{latest.ts.isoformat()}",
            event_type=event_type,
            direction=direction,
            ts=latest.ts,
            candle_index=len(rows) - 1,
            close=float(latest.close),
            trend_before=trend,
            broken_swing=swing,
        )
    return None


class MechanicalStructureTracker:
    """Exactly-once state around the pure swing and event functions."""

    def __init__(self, config: MechanicalStructureConfig) -> None:
        self.config = config
        self._broken: dict[tuple[str, str], set[str]] = {}
        self._latest: dict[tuple[str, str], StructureUpdate] = {}

    def reset(self, symbol: str, timeframe: str | None = None) -> None:
        native = symbol.upper()
        keys = [key for key in self._broken if key[0] == native]
        for key in keys:
            if timeframe is None or key[1] == timeframe:
                self._broken.pop(key, None)
                self._latest.pop(key, None)

    def update(
        self, symbol: str, timeframe: str, candles: Collection[Candle]
    ) -> StructureUpdate:
        key = (symbol.upper(), timeframe)
        swings = confirmed_swings(candles, self.config)
        trend = current_swing_trend(swings, self.config.trend_lookback)
        broken = self._broken.setdefault(key, set())
        event = detect_structure_event(
            candles,
            swings,
            trend,
            already_broken_swing_ids=broken,
        )
        if event is not None:
            broken.add(event.broken_swing.swing_id)
        result = StructureUpdate(swings, trend, event)
        self._latest[key] = result
        return result

    def latest(self, symbol: str, timeframe: str) -> StructureUpdate | None:
        return self._latest.get((symbol.upper(), timeframe))
