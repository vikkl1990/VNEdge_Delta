"""Continuous MTF v2 using mechanical 1h BOS/CHOCH structure."""

from __future__ import annotations

from datetime import datetime, timedelta

from vnedge.scalping.delta_engine.continuous_mtf import (
    ContinuousMTFConfig,
    ContinuousMultiTFStateMachine,
    _StateMemory,
)
from vnedge.scalping.delta_engine.mechanical_structure import (
    MechanicalStructureConfig,
    MechanicalStructureTracker,
)


def _event_ts(event: dict[str, object]) -> datetime:
    value = event["ts"]
    if not isinstance(value, str):
        raise TypeError("mechanical structure event timestamp must be an ISO string")
    return datetime.fromisoformat(value)


class MechanicalStructureMultiTFStateMachine(ContinuousMultiTFStateMachine):
    """V1 state lifecycle with the preregistered v2 1h structure rules."""

    scanner_id = "continuous_mtf_alignment_v2"

    def __init__(
        self,
        store,
        config: ContinuousMTFConfig,
        structure_config: MechanicalStructureConfig,
        *,
        subscribe: bool = True,
    ) -> None:
        super().__init__(store, config, subscribe=subscribe)
        self.structure_config = structure_config
        self.structure_tracker = MechanicalStructureTracker(structure_config)

    def reset_symbol(self, symbol: str) -> None:
        super().reset_symbol(symbol)
        self.structure_tracker.reset(symbol)

    def _update_1h(self, memory: _StateMemory) -> None:
        rows = self.store.recent(memory.symbol, "1h")
        minimum_rows = (
            self.structure_config.swing_left
            + self.structure_config.swing_right
            + self.structure_config.trend_lookback
        )
        if len(rows) < minimum_rows or memory.bias_4h == 0:
            memory.structure_1h = "unavailable" if len(rows) < minimum_rows else "conflict"
            memory.one_hour_agrees = False
            self._invalidate_lower(memory, "1h_unavailable_or_4h_neutral")
            return

        update = self.structure_tracker.update(memory.symbol, "1h", rows)
        event = update.event
        if event is not None:
            payload = event.to_dict()
            if event.event_type == "bos":
                memory.last_bos_1h = payload
            else:
                memory.last_choch_1h = payload
            memory.counters[f"1h_structure_event:{event.event_type}"] += 1
            memory.counters[f"1h_structure_event_direction:{event.direction}"] += 1
            if event.direction != memory.bias_4h:
                memory.one_hour_agrees = False
                memory.structure_1h = f"conflict_{event.event_type}"
                self._invalidate_lower(memory, "1h_opposing_structure_event")
                return

        now = rows[-1].ts
        freshness = timedelta(hours=self.structure_config.event_freshness_hours)
        aligned_events: list[dict[str, object]] = []
        for candidate in (memory.last_bos_1h, memory.last_choch_1h):
            if candidate is None or int(candidate["direction"]) != memory.bias_4h:
                continue
            if now - _event_ts(candidate) <= freshness:
                aligned_events.append(candidate)
        latest = max(aligned_events, key=_event_ts) if aligned_events else None
        agrees = latest is not None and (
            latest["type"] == "choch" or update.trend == memory.bias_4h
        )
        memory.one_hour_agrees = agrees
        memory.structure_1h = f"aligned_{latest['type']}" if agrees else "conflict"
        if not agrees:
            self._invalidate_lower(memory, "1h_missing_fresh_aligned_structure_event")
        else:
            memory.conflicts = []

