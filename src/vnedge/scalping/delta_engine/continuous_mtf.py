"""Continuous causal state machine for multi-timeframe alignment research.

Every proven candle close updates one shared symbol state. Higher timeframes
can immediately invalidate lower-timeframe setup, confirmation, and trigger
state. The module owns no order route and uses no L2 or future information.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from statistics import fmean, pstdev
from types import MappingProxyType
from typing import Any

import yaml

from vnedge.scalping.delta_engine.candle_store import MultiTimeframeCandleStore
from vnedge.scalping.delta_engine.fee_model import DeltaFeeModel
from vnedge.scalping.delta_engine.regime import _adx, _ema, _true_ranges
from vnedge.scalping.delta_engine.types import Candle, Side, SignalCandidate


@dataclass(frozen=True)
class ContinuousMTFConfig:
    ema_4h_fast: int
    ema_4h_slow: int
    adx_4h_window: int
    adx_4h_minimum: float
    bias_stable_closes: int
    range_adx_maximum: float
    swing_left: int
    swing_right: int
    key_level_lookback: int
    ema_1h_window: int
    ema_1h_slope_bars: int
    atr_1h_window: int
    pullback_1h_tolerance_atr: float
    bos_1h_lookback: int
    target_1h_lookback: int
    ema_15m_window: int
    atr_15m_window: int
    pullback_15m_tolerance_atr: float
    pullback_15m_min_body: float
    breakout_15m_lookback: int
    breakout_15m_min_body: float
    breakout_15m_min_volume_z: float
    setup_expiry_minutes: int
    confirmation_5m_min_body: float
    confirmation_5m_min_volume_z: float
    confirmation_expiry_minutes: int
    trigger_1m_lookback: int
    trigger_1m_min_body: float
    trigger_1m_min_volume_z: float
    trigger_expiry_minutes: int
    stop_buffer_bps: float
    minimum_reward_risk: float
    minimum_target_cost_multiple: float
    expected_hold_seconds: int
    time_stop_seconds: int
    prefer_maker: bool
    cooldown_minutes: int


def load_continuous_mtf_config(
    path: str | Path = "configs/research/continuous_mtf_alignment_v1.yaml",
) -> tuple[ContinuousMTFConfig, dict[str, Any]]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError("continuous MTF contract must be a mapping")
    if raw.get("can_trade") or raw.get("can_promote") or not raw.get("research_only"):
        raise ValueError("continuous MTF v1 must remain research-only")
    state = raw["state"]
    bias = state["bias_4h"]
    structure = state["structure_1h"]
    setup = state["setup_15m"]
    confirmation = state["confirmation_5m"]
    trigger = state["trigger_1m"]
    geometry = raw["geometry"]
    config = ContinuousMTFConfig(
        ema_4h_fast=int(bias["ema_fast"]),
        ema_4h_slow=int(bias["ema_slow"]),
        adx_4h_window=int(bias["adx_window"]),
        adx_4h_minimum=float(bias["minimum_adx"]),
        bias_stable_closes=int(bias["stable_closes"]),
        range_adx_maximum=float(bias["range_adx_maximum"]),
        swing_left=int(bias["confirmed_swing_left_bars"]),
        swing_right=int(bias["confirmed_swing_right_bars"]),
        key_level_lookback=int(bias["key_level_lookback_bars"]),
        ema_1h_window=int(structure["ema_window"]),
        ema_1h_slope_bars=int(structure["ema_slope_bars"]),
        atr_1h_window=int(structure["atr_window"]),
        pullback_1h_tolerance_atr=float(structure["pullback_tolerance_atr"]),
        bos_1h_lookback=int(structure["bos_lookback_bars"]),
        target_1h_lookback=int(structure["target_lookback_bars"]),
        ema_15m_window=int(setup["ema_window"]),
        atr_15m_window=int(setup["atr_window"]),
        pullback_15m_tolerance_atr=float(setup["pullback_tolerance_atr"]),
        pullback_15m_min_body=float(setup["pullback_minimum_body_ratio"]),
        breakout_15m_lookback=int(setup["breakout_lookback_bars"]),
        breakout_15m_min_body=float(setup["breakout_minimum_body_ratio"]),
        breakout_15m_min_volume_z=float(setup["breakout_minimum_volume_z"]),
        setup_expiry_minutes=int(setup["expiry_minutes"]),
        confirmation_5m_min_body=float(confirmation["minimum_body_ratio"]),
        confirmation_5m_min_volume_z=float(confirmation["minimum_volume_z"]),
        confirmation_expiry_minutes=int(confirmation["expiry_minutes_after_setup"]),
        trigger_1m_lookback=int(trigger["break_lookback_bars"]),
        trigger_1m_min_body=float(trigger["minimum_body_ratio"]),
        trigger_1m_min_volume_z=float(trigger["minimum_volume_z"]),
        trigger_expiry_minutes=int(trigger["expiry_minutes_after_confirmation"]),
        stop_buffer_bps=float(geometry["stop_buffer_bps"]),
        minimum_reward_risk=float(geometry["minimum_reward_risk"]),
        minimum_target_cost_multiple=float(geometry["minimum_target_cost_multiple"]),
        expected_hold_seconds=int(geometry["expected_hold_seconds"]),
        time_stop_seconds=int(geometry["time_stop_seconds"]),
        prefer_maker=bool(geometry["prefer_maker"]),
        cooldown_minutes=int(raw["frequency"]["cooldown_minutes_after_emission"]),
    )
    if config.ema_4h_slow <= config.ema_4h_fast:
        raise ValueError("4h slow EMA must exceed fast EMA")
    if config.expected_hold_seconds > config.time_stop_seconds:
        raise ValueError("expected hold cannot exceed time stop")
    return config, raw


@dataclass(frozen=True)
class MechanicalSetupZone:
    setup_id: str
    setup_type: str
    side: Side
    born_at: datetime
    expires_at: datetime
    lower: float
    upper: float
    structural_target: float
    target_source: str

    def to_dict(self) -> dict[str, object]:
        return {
            **self.__dict__,
            "side": self.side.value,
            "born_at": self.born_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
        }


@dataclass(frozen=True)
class MultiTFState:
    symbol: str
    bias_4h: int = 0
    bias_stable_closes: int = 0
    structure_4h: str = "unavailable"
    key_highs_4h: tuple[float, ...] = ()
    key_lows_4h: tuple[float, ...] = ()
    structure_1h: str = "unavailable"
    last_bos_1h: Mapping[str, object] | None = None
    last_choch_1h: Mapping[str, object] | None = None
    setup_15m: str = "none"
    zone_15m: MechanicalSetupZone | None = None
    confirmation_5m: bool = False
    momentum_quality_5m: float = 0.0
    confirmation_ts: datetime | None = None
    trigger_1m: bool = False
    micro_structure_1m: str = "none"
    stack_aligned: bool = False
    conflict_flags: tuple[str, ...] = ()
    last_update_ts: datetime | None = None
    update_sequence: int = 0

    def __post_init__(self) -> None:
        if self.last_bos_1h is not None:
            object.__setattr__(self, "last_bos_1h", MappingProxyType(dict(self.last_bos_1h)))
        if self.last_choch_1h is not None:
            object.__setattr__(self, "last_choch_1h", MappingProxyType(dict(self.last_choch_1h)))

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "bias_4h": self.bias_4h,
            "bias_stable_closes": self.bias_stable_closes,
            "structure_4h": self.structure_4h,
            "key_highs_4h": list(self.key_highs_4h),
            "key_lows_4h": list(self.key_lows_4h),
            "structure_1h": self.structure_1h,
            "last_bos_1h": dict(self.last_bos_1h) if self.last_bos_1h else None,
            "last_choch_1h": dict(self.last_choch_1h) if self.last_choch_1h else None,
            "setup_15m": self.setup_15m,
            "zone_15m": self.zone_15m.to_dict() if self.zone_15m else None,
            "confirmation_5m": self.confirmation_5m,
            "momentum_quality_5m": self.momentum_quality_5m,
            "confirmation_ts": self.confirmation_ts.isoformat() if self.confirmation_ts else None,
            "trigger_1m": self.trigger_1m,
            "micro_structure_1m": self.micro_structure_1m,
            "stack_aligned": self.stack_aligned,
            "conflict_flags": list(self.conflict_flags),
            "last_update_ts": self.last_update_ts.isoformat() if self.last_update_ts else None,
            "update_sequence": self.update_sequence,
        }


@dataclass(frozen=True)
class ContinuousSetupIntent:
    scanner_id: str
    symbol: str
    side: Side
    decision_ts: datetime
    decision_price: float
    setup: MechanicalSetupZone
    state: MultiTFState

    @property
    def intent_id(self) -> str:
        return f"{self.scanner_id}:{self.setup.setup_id}:{self.decision_ts.isoformat()}"


@dataclass(frozen=True)
class EntryGeometry:
    status: str
    reason: str
    entry_price: float
    stop_price: float | None
    target_price: float | None
    stop_distance_bps: float
    target_distance_bps: float
    reward_risk: float
    cost_bps: float
    cost_multiple: float
    candidate: SignalCandidate | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            key: value.to_dict() if key == "candidate" and value is not None else value
            for key, value in self.__dict__.items()
        }


@dataclass
class _StateMemory:
    symbol: str
    bias_candidate: int = 0
    bias_stable_closes: int = 0
    bias_4h: int = 0
    structure_4h: str = "unavailable"
    key_highs_4h: tuple[float, ...] = ()
    key_lows_4h: tuple[float, ...] = ()
    structure_1h: str = "unavailable"
    one_hour_agrees: bool = False
    last_bos_1h: dict[str, object] | None = None
    last_choch_1h: dict[str, object] | None = None
    zone: MechanicalSetupZone | None = None
    confirmation_ts: datetime | None = None
    momentum_quality_5m: float = 0.0
    trigger_1m: bool = False
    micro_structure_1m: str = "none"
    conflicts: list[str] = field(default_factory=list)
    last_update_ts: datetime | None = None
    update_sequence: int = 0
    last_emitted_setup: str | None = None
    last_fire_ts: datetime | None = None
    counters: Counter = field(default_factory=Counter)


def _body_ratio(row: Candle) -> float:
    span = max(row.high - row.low, row.close * 1e-12)
    return abs(row.close - row.open) / span


def _volume_z(rows: tuple[Candle, ...], window: int = 20) -> float:
    if len(rows) < window + 1:
        return 0.0
    history = [float(row.volume) for row in rows[-(window + 1) : -1]]
    deviation = pstdev(history)
    return (float(rows[-1].volume) - fmean(history)) / deviation if deviation else 0.0


class ContinuousMultiTFStateMachine:
    """One persistent, causally updated state per symbol."""

    scanner_id = "continuous_mtf_alignment_v1"

    def __init__(
        self,
        store: MultiTimeframeCandleStore,
        config: ContinuousMTFConfig,
        *,
        subscribe: bool = True,
    ) -> None:
        self.store = store
        self.config = config
        self._states: dict[str, _StateMemory] = {}
        if subscribe:
            store.on_closed_candle(self.on_closed_candle)

    def _memory(self, symbol: str) -> _StateMemory:
        native = symbol.upper()
        return self._states.setdefault(native, _StateMemory(native))

    def reset_symbol(self, symbol: str) -> None:
        native = symbol.upper()
        previous = self._states.get(native)
        resets = previous.counters["source_gap_reset"] + 1 if previous else 1
        memory = _StateMemory(native)
        memory.counters["source_gap_reset"] = resets
        self._states[native] = memory

    def on_closed_candle(self, symbol: str, candle: Candle) -> None:
        memory = self._memory(symbol)
        memory.last_update_ts = candle.ts
        memory.update_sequence += 1
        self._expire(memory, candle.ts)
        if candle.tf == "4h":
            self._update_4h(memory)
        elif candle.tf == "1h":
            self._update_1h(memory)
        elif candle.tf == "15m":
            self._update_15m(memory)
        elif candle.tf == "5m":
            self._update_5m(memory)
        elif candle.tf == "1m":
            self._update_1m(memory)

    def _invalidate_lower(self, memory: _StateMemory, reason: str) -> None:
        if memory.zone is not None or memory.confirmation_ts is not None or memory.trigger_1m:
            memory.counters[f"invalidated:{reason}"] += 1
        memory.zone = None
        memory.confirmation_ts = None
        memory.momentum_quality_5m = 0.0
        memory.trigger_1m = False
        memory.micro_structure_1m = "none"
        memory.conflicts = [reason]

    def _expire(self, memory: _StateMemory, now: datetime) -> None:
        if memory.zone is not None and now > memory.zone.expires_at:
            self._invalidate_lower(memory, "setup_expired")
        elif memory.confirmation_ts is not None and now - memory.confirmation_ts > timedelta(
            minutes=self.config.trigger_expiry_minutes
        ):
            memory.confirmation_ts = None
            memory.momentum_quality_5m = 0.0
            memory.trigger_1m = False
            memory.conflicts = ["confirmation_expired"]
            memory.counters["invalidated:confirmation_expired"] += 1

    def _confirmed_swings(
        self, rows: tuple[Candle, ...]
    ) -> tuple[tuple[float, ...], tuple[float, ...]]:
        left, right = self.config.swing_left, self.config.swing_right
        bounded = rows[-self.config.key_level_lookback :]
        highs: list[float] = []
        lows: list[float] = []
        for index in range(left, len(bounded) - right):
            row = bounded[index]
            neighbours = bounded[index - left : index] + bounded[index + 1 : index + right + 1]
            if all(row.high > other.high for other in neighbours):
                highs.append(row.high)
            if all(row.low < other.low for other in neighbours):
                lows.append(row.low)
        return tuple(highs[-8:]), tuple(lows[-8:])

    def _update_4h(self, memory: _StateMemory) -> None:
        rows = self.store.recent(memory.symbol, "4h")
        if len(rows) < max(self.config.ema_4h_slow + 1, self.config.adx_4h_window + 2):
            memory.structure_4h = "unavailable"
            return
        closes = [row.close for row in rows]
        fast = _ema(closes, self.config.ema_4h_fast)
        slow = _ema(closes, self.config.ema_4h_slow)
        adx = _adx(rows, self.config.adx_4h_window)
        candidate = 0
        if adx >= self.config.adx_4h_minimum:
            if fast > slow and rows[-1].close > fast:
                candidate = 1
            elif fast < slow and rows[-1].close < fast:
                candidate = -1
        if candidate == memory.bias_candidate:
            memory.bias_stable_closes += 1
        else:
            memory.bias_candidate = candidate
            memory.bias_stable_closes = 1
        new_bias = (
            candidate
            if candidate != 0 and memory.bias_stable_closes >= self.config.bias_stable_closes
            else 0
        )
        old_bias = memory.bias_4h
        memory.bias_4h = new_bias
        memory.structure_4h = (
            "trend"
            if new_bias
            else "range"
            if adx <= self.config.range_adx_maximum
            else "transition"
        )
        memory.key_highs_4h, memory.key_lows_4h = self._confirmed_swings(rows)
        if new_bias != old_bias:
            self._invalidate_lower(memory, "4h_bias_flip_or_neutral")
            memory.counters["4h_bias_changes"] += 1
        elif new_bias:
            memory.conflicts = []

    def _update_1h(self, memory: _StateMemory) -> None:
        rows = self.store.recent(memory.symbol, "1h")
        required = max(
            self.config.ema_1h_window + self.config.ema_1h_slope_bars,
            self.config.atr_1h_window + 1,
            self.config.bos_1h_lookback + 1,
        )
        if len(rows) < required or memory.bias_4h == 0:
            memory.structure_1h = "unavailable" if len(rows) < required else "conflict"
            memory.one_hour_agrees = False
            self._invalidate_lower(memory, "1h_unavailable_or_4h_neutral")
            return
        latest = rows[-1]
        closes = [row.close for row in rows]
        ema_now = _ema(closes, self.config.ema_1h_window)
        ema_then = _ema(closes[: -self.config.ema_1h_slope_bars], self.config.ema_1h_window)
        atr = fmean(_true_ranges(rows)[-self.config.atr_1h_window :])
        prior = rows[-(self.config.bos_1h_lookback + 1) : -1]
        broke_up = latest.close > max(row.high for row in prior)
        broke_down = latest.close < min(row.low for row in prior)
        if broke_up:
            event = {
                "side": "up",
                "ts": latest.ts.isoformat(),
                "level": max(row.high for row in prior),
            }
            if memory.bias_4h > 0:
                memory.last_bos_1h = event
            else:
                memory.last_choch_1h = event
        elif broke_down:
            event = {
                "side": "down",
                "ts": latest.ts.isoformat(),
                "level": min(row.low for row in prior),
            }
            if memory.bias_4h < 0:
                memory.last_bos_1h = event
            else:
                memory.last_choch_1h = event
        tolerance = atr * self.config.pullback_1h_tolerance_atr
        if memory.bias_4h > 0:
            agrees = ema_now > ema_then and latest.close >= ema_now and not broke_down
            pullback = latest.low <= ema_now + tolerance
        else:
            agrees = ema_now < ema_then and latest.close <= ema_now and not broke_up
            pullback = latest.high >= ema_now - tolerance
        memory.one_hour_agrees = agrees
        memory.structure_1h = (
            "aligned_pullback"
            if agrees and pullback
            else "aligned_impulse"
            if agrees
            else "conflict"
        )
        if not agrees:
            self._invalidate_lower(memory, "1h_structure_conflict_or_choch")
        else:
            memory.conflicts = []

    def _structural_target(self, memory: _StateMemory, price: float) -> tuple[float, str] | None:
        one_hour = self.store.recent(memory.symbol, "1h")
        candidates: list[tuple[float, str]] = []
        prior = one_hour[-self.config.target_1h_lookback :]
        if prior:
            level = (
                max(row.high for row in prior)
                if memory.bias_4h > 0
                else min(row.low for row in prior)
            )
            if (memory.bias_4h > 0 and level > price) or (memory.bias_4h < 0 and level < price):
                candidates.append((level, "1h_prior_24_bar_extreme"))
        levels = memory.key_highs_4h if memory.bias_4h > 0 else memory.key_lows_4h
        for level in levels:
            if (memory.bias_4h > 0 and level > price) or (memory.bias_4h < 0 and level < price):
                candidates.append((level, "confirmed_4h_swing"))
        if not candidates:
            return None
        return (
            min(candidates, key=lambda item: item[0])
            if memory.bias_4h > 0
            else max(candidates, key=lambda item: item[0])
        )

    def _update_15m(self, memory: _StateMemory) -> None:
        rows = self.store.recent(memory.symbol, "15m")
        if memory.zone is not None:
            latest = rows[-1]
            breached = (
                latest.close < memory.zone.lower
                if memory.zone.side is Side.LONG
                else latest.close > memory.zone.upper
            )
            if breached:
                self._invalidate_lower(memory, "15m_zone_breached")
            return  # one active setup lifecycle; never replace it in place
        if memory.bias_4h == 0 or not memory.one_hour_agrees:
            return
        required = max(
            self.config.ema_15m_window + 1,
            self.config.atr_15m_window + 1,
            self.config.breakout_15m_lookback + 1,
            21,
        )
        if len(rows) < required:
            return
        latest = rows[-1]
        prior_breakout = rows[-(self.config.breakout_15m_lookback + 1) : -1]
        ema = _ema([row.close for row in rows], self.config.ema_15m_window)
        atr = fmean(_true_ranges(rows)[-self.config.atr_15m_window :])
        tolerance = atr * self.config.pullback_15m_tolerance_atr
        body = _body_ratio(latest)
        volume_z = _volume_z(rows)
        if memory.bias_4h > 0:
            breakout = (
                latest.close > max(row.high for row in prior_breakout)
                and latest.close > latest.open
                and body >= self.config.breakout_15m_min_body
                and volume_z >= self.config.breakout_15m_min_volume_z
            )
            pullback = (
                latest.low <= ema + tolerance
                and latest.close >= ema
                and latest.close > latest.open
                and body >= self.config.pullback_15m_min_body
            )
            side = Side.LONG
        else:
            breakout = (
                latest.close < min(row.low for row in prior_breakout)
                and latest.close < latest.open
                and body >= self.config.breakout_15m_min_body
                and volume_z >= self.config.breakout_15m_min_volume_z
            )
            pullback = (
                latest.high >= ema - tolerance
                and latest.close <= ema
                and latest.close < latest.open
                and body >= self.config.pullback_15m_min_body
            )
            side = Side.SHORT
        setup_type = "breakout" if breakout else "pullback" if pullback else None
        if setup_type is None:
            return
        target = self._structural_target(memory, latest.close)
        if target is None:
            memory.counters["setup_rejected:no_structural_target"] += 1
            return
        target_price, target_source = target
        memory.zone = MechanicalSetupZone(
            setup_id=f"{memory.symbol}:{side.value}:{setup_type}:{latest.ts.isoformat()}",
            setup_type=setup_type,
            side=side,
            born_at=latest.ts,
            expires_at=latest.ts + timedelta(minutes=self.config.setup_expiry_minutes),
            lower=latest.low,
            upper=latest.high,
            structural_target=target_price,
            target_source=target_source,
        )
        memory.confirmation_ts = None
        memory.trigger_1m = False
        memory.micro_structure_1m = "none"
        memory.conflicts = []
        memory.counters[f"setup_born:{setup_type}"] += 1

    def _update_5m(self, memory: _StateMemory) -> None:
        rows = self.store.recent(memory.symbol, "5m")
        zone = memory.zone
        if zone is None or len(rows) < 21 or rows[-1].ts <= zone.born_at:
            return
        latest, previous = rows[-1], rows[-2]
        if latest.ts - zone.born_at > timedelta(minutes=self.config.confirmation_expiry_minutes):
            self._invalidate_lower(memory, "5m_confirmation_window_expired")
            return
        body = _body_ratio(latest)
        volume_z = _volume_z(rows)
        aligned = (
            latest.close > latest.open and latest.close > previous.high
            if zone.side is Side.LONG
            else latest.close < latest.open and latest.close < previous.low
        )
        if (
            aligned
            and body >= self.config.confirmation_5m_min_body
            and volume_z >= self.config.confirmation_5m_min_volume_z
        ):
            memory.confirmation_ts = latest.ts
            memory.momentum_quality_5m = body * max(0.0, 1.0 + volume_z)
            memory.counters["5m_confirmations"] += 1

    def _update_1m(self, memory: _StateMemory) -> None:
        memory.trigger_1m = False
        memory.micro_structure_1m = "none"
        rows = self.store.recent(memory.symbol, "1m")
        zone = memory.zone
        confirmed = memory.confirmation_ts
        if zone is None or confirmed is None or len(rows) < 21 or rows[-1].ts <= confirmed:
            return
        latest = rows[-1]
        if latest.ts - confirmed > timedelta(minutes=self.config.trigger_expiry_minutes):
            return
        prior = rows[-(self.config.trigger_1m_lookback + 1) : -1]
        body = _body_ratio(latest)
        volume_z = _volume_z(rows)
        aligned = (
            latest.close > latest.open and latest.close > max(row.high for row in prior)
            if zone.side is Side.LONG
            else latest.close < latest.open and latest.close < min(row.low for row in prior)
        )
        if (
            aligned
            and body >= self.config.trigger_1m_min_body
            and volume_z >= self.config.trigger_1m_min_volume_z
        ):
            memory.trigger_1m = True
            memory.micro_structure_1m = (
                "break_3bar_high" if zone.side is Side.LONG else "break_3bar_low"
            )
            memory.counters["1m_triggers"] += 1

    def snapshot(self, symbol: str) -> MultiTFState:
        memory = self._memory(symbol)
        aligned = bool(
            memory.bias_4h
            and memory.bias_stable_closes >= self.config.bias_stable_closes
            and memory.one_hour_agrees
            and memory.zone is not None
            and memory.confirmation_ts is not None
            and memory.trigger_1m
            and not memory.conflicts
        )
        return MultiTFState(
            symbol=memory.symbol,
            bias_4h=memory.bias_4h,
            bias_stable_closes=memory.bias_stable_closes,
            structure_4h=memory.structure_4h,
            key_highs_4h=memory.key_highs_4h,
            key_lows_4h=memory.key_lows_4h,
            structure_1h=memory.structure_1h,
            last_bos_1h=memory.last_bos_1h,
            last_choch_1h=memory.last_choch_1h,
            setup_15m=memory.zone.setup_type if memory.zone else "none",
            zone_15m=memory.zone,
            confirmation_5m=memory.confirmation_ts is not None,
            momentum_quality_5m=memory.momentum_quality_5m,
            confirmation_ts=memory.confirmation_ts,
            trigger_1m=memory.trigger_1m,
            micro_structure_1m=memory.micro_structure_1m,
            stack_aligned=aligned,
            conflict_flags=tuple(memory.conflicts),
            last_update_ts=memory.last_update_ts,
            update_sequence=memory.update_sequence,
        )

    def claim_intent(self, symbol: str, *, decision_price: float) -> ContinuousSetupIntent | None:
        memory = self._memory(symbol)
        state = self.snapshot(symbol)
        zone = memory.zone
        if not state.stack_aligned or zone is None or state.last_update_ts is None:
            return None
        if memory.last_emitted_setup == zone.setup_id:
            return None
        if (
            memory.last_fire_ts is not None
            and state.last_update_ts - memory.last_fire_ts
            < timedelta(minutes=self.config.cooldown_minutes)
        ):
            return None
        memory.last_emitted_setup = zone.setup_id
        memory.last_fire_ts = state.last_update_ts
        memory.trigger_1m = False
        memory.counters["intents_emitted"] += 1
        return ContinuousSetupIntent(
            scanner_id=self.scanner_id,
            symbol=memory.symbol,
            side=zone.side,
            decision_ts=state.last_update_ts,
            decision_price=decision_price,
            setup=zone,
            state=state,
        )

    def counters(self, symbol: str) -> dict[str, int]:
        return dict(self._memory(symbol).counters)


def finalize_continuous_entry(
    intent: ContinuousSetupIntent,
    entry_bar: Candle,
    config: ContinuousMTFConfig,
    fee_model: DeltaFeeModel,
) -> EntryGeometry:
    """Apply structural geometry at the next open; never manufacture a target."""
    entry = float(entry_bar.open)
    if intent.side is Side.LONG:
        stop = intent.setup.lower * (1.0 - config.stop_buffer_bps / 10_000.0)
        target = intent.setup.structural_target
        stop_bps = (1.0 - stop / entry) * 10_000
        target_bps = (target / entry - 1.0) * 10_000
    else:
        stop = intent.setup.upper * (1.0 + config.stop_buffer_bps / 10_000.0)
        target = intent.setup.structural_target
        stop_bps = (stop / entry - 1.0) * 10_000
        target_bps = (1.0 - target / entry) * 10_000
    costs = fee_model.breakdown(
        intent.symbol,
        entry_is_maker=config.prefer_maker,
        hold_seconds=config.expected_hold_seconds,
    )
    reward_risk = target_bps / stop_bps if stop_bps > 0 else 0.0
    cost_multiple = target_bps / costs.total_bps if costs.total_bps > 0 else 0.0
    reason = "accepted"
    if stop_bps <= 0 or target_bps <= 0:
        reason = "invalid_next_open_structural_geometry"
    elif reward_risk < config.minimum_reward_risk:
        reason = "reward_risk_below_minimum"
    elif cost_multiple < config.minimum_target_cost_multiple:
        reason = "structural_target_below_cost_multiple"
    if reason != "accepted":
        return EntryGeometry(
            "rejected",
            reason,
            entry,
            stop if stop > 0 else None,
            target if target > 0 else None,
            stop_bps,
            target_bps,
            reward_risk,
            costs.total_bps,
            cost_multiple,
        )
    metadata = {
        "signal_type": f"continuous_mtf_{intent.setup.setup_type}",
        "setup_id": intent.setup.setup_id,
        "continuous_state": intent.state.to_dict(),
        "target_source": intent.setup.target_source,
        "target_floor_policy": "reject_never_expand_structural_target",
        "calibration": {
            "status": "standalone_unmodeled_primary_hypothesis",
            "probability_used_for_gate": False,
            "confidence_used_for_gate": False,
            "promotion_eligible": False,
        },
        "l2_confirmation": {
            "status": "unavailable_in_replay",
            "context_only": True,
            "used_for_signal": False,
            "used_for_execution": False,
        },
        "regime": "not_used",
        "regime_profile": {},
        "fee_breakdown": costs.to_dict(),
    }
    candidate = SignalCandidate(
        scanner_id=intent.scanner_id,
        symbol=intent.symbol,
        side=intent.side,
        decision_ts=intent.decision_ts,
        entry_price=entry,
        stop_loss=stop,
        take_profits=(target,),
        time_stop_seconds=config.time_stop_seconds,
        expected_hold_seconds=config.expected_hold_seconds,
        expected_move_bps=target_bps,
        raw_expectancy_bps=0.0,
        modeled_cost_bps=costs.total_bps,
        fee_adjusted_expectancy_bps=-costs.total_bps,
        scalper_probability=0.5,
        confidence=0.0,
        entry_is_maker=config.prefer_maker,
        metadata=metadata,
    )
    return EntryGeometry(
        "accepted",
        reason,
        entry,
        stop,
        target,
        stop_bps,
        target_bps,
        reward_risk,
        costs.total_bps,
        cost_multiple,
        candidate,
    )
