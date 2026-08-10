from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from vnedge.research.htf_structure_break_backtest import (
    HTFReplayResult,
    selection_gate,
    simulate_selection,
)
from vnedge.scalping.delta_engine.htf_structure_break import (
    HTFStructureBreakScanner,
    HTFStructureContext,
    htf_structure_fee_model,
    load_htf_structure_break_config,
)
from vnedge.scalping.delta_engine.mechanical_structure import ConfirmedSwing, StructureEvent, StructureUpdate
from vnedge.scalping.delta_engine.types import Candle, Side

CONFIG = "configs/research/htf_structure_break_v1.yaml"
NOW = datetime(2026, 3, 1, 12, tzinfo=UTC)


def _swing(kind: str, price: float, tf: str, hours: int) -> ConfirmedSwing:
    ts = NOW - timedelta(hours=hours)
    return ConfirmedSwing(f"{tf}:{kind}:{price}", tf, kind, ts, ts + timedelta(hours=1), price, hours)


class _FixedTracker:
    def __init__(self, update: StructureUpdate) -> None:
        self.value = update

    def update(self, symbol: str, timeframe: str, candles: tuple[Candle, ...]) -> StructureUpdate:
        return self.value


def test_contract_is_locked_selection_only_and_costed():
    config = load_htf_structure_break_config(CONFIG)
    fee = htf_structure_fee_model(config)
    assert config.can_trade is False and config.can_promote is False
    assert config.exit.minimum_target_cost_multiple == 5.0
    assert fee.breakdown("ETHUSD", entry_is_maker=False, hold_seconds=86400).total_bps == pytest.approx(14.8)


def test_scanner_emits_aligned_structural_candidate():
    config = load_htf_structure_break_config(CONFIG)
    scanner = HTFStructureBreakScanner(config, htf_structure_fee_model(config))
    four_swings = (_swing("low", 90, "4h", 20), _swing("high", 104, "4h", 16), _swing("low", 92, "4h", 12), _swing("high", 108, "4h", 8))
    scanner._four_hour["BTCUSD"] = StructureUpdate(four_swings, 1, None)
    one_swings = (_swing("low", 98, "1h", 8), _swing("high", 100, "1h", 6), _swing("low", 101, "1h", 4), _swing("high", 101.5, "1h", 2))
    event = StructureEvent("event", "bos", 1, NOW, 10, 102, 1, one_swings[-1])
    scanner.one_hour_tracker = _FixedTracker(StructureUpdate(one_swings, 1, event))  # type: ignore[assignment]
    row = Candle(NOW, 101, 103, 100, 102, 100, "1h")
    candidate = scanner.evaluate(HTFStructureContext("BTCUSD", NOW, (row,), (Candle(NOW, 100, 110, 90, 102, 1, "4h"),)))
    assert candidate is not None
    assert candidate.side is Side.LONG
    assert candidate.metadata["target_source_timeframe"] == "4h"
    assert candidate.metadata["target_cost_multiple"] >= 5.0


def test_gap_resets_and_selection_gate_fails_closed():
    config = load_htf_structure_break_config(CONFIG)
    rows = [
        Candle(NOW + timedelta(minutes=i + int(i >= 3)), 100, 101, 99, 100, 1, "1m")
        for i in range(8)
    ]
    result = simulate_selection("BTCUSD", rows, config, decision_end_exclusive=NOW + timedelta(days=1))
    assert result.missing_minutes == 1
    empty = (
        HTFReplayResult("BTCUSD", (), 0, 0, 10, 1, NOW, NOW + timedelta(days=1), {}),
        HTFReplayResult("ETHUSD", (), 0, 0, 10, 1, NOW, NOW + timedelta(days=1), {}),
    )
    gate = selection_gate(empty, config)
    assert gate["passed"] is False
    assert gate["checks"]["minimum_trades"] is False
