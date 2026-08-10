from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from vnedge.research.htf_structure_break_v2_backtest import (
    funding_settlements_from_hourly,
)
from vnedge.scalping.delta_engine.htf_structure_break_v2 import (
    HTFStructureBreakV2Scanner,
    HTFStructureV2Context,
    geometry_valid_at_entry,
    htf_structure_v2_fee_model,
    load_htf_structure_break_v2_config,
)
from vnedge.scalping.delta_engine.types import Candle, Side

CONFIG = "configs/research/htf_structure_break_v2.yaml"
START = datetime(2026, 1, 1, tzinfo=UTC)


def _bar(index: int, close: float, *, high: float | None = None, tf: str = "1h") -> Candle:
    ts = START + timedelta(hours=index if tf == "1h" else index * 4)
    return Candle(
        ts=ts,
        open=close - 0.2,
        high=high if high is not None else close + 0.8,
        low=close - 1.0,
        close=close,
        volume=100.0,
        tf=tf,
    )


def _context() -> HTFStructureV2Context:
    closes = [100.0] * 25
    closes[6] = 108.0
    closes[14] = 103.5
    closes[-2] = 104.0
    closes[-1] = 106.0
    one = []
    for index, close in enumerate(closes):
        high = 115.0 if index == 6 else 105.0 if index == 14 else close + 0.8
        one.append(_bar(index, close, high=high))
    four = tuple(
        Candle(
            ts=one[-1].ts - timedelta(hours=4 * (60 - index)),
            open=99.8 + index,
            high=100.8 + index,
            low=99.0 + index,
            close=100.0 + index,
            volume=100.0,
            tf="4h",
        )
        for index in range(60)
    )
    return HTFStructureV2Context("BTCUSD", one[-1].ts, tuple(one), four)


def test_v2_contract_is_frozen_and_cost_arithmetic_is_consistent():
    config = load_htf_structure_break_v2_config(CONFIG)
    fee = htf_structure_v2_fee_model(config)
    assert config.can_trade is False and config.can_promote is False
    assert config.structure.swing_right_bars == 5
    assert config.structure.event_type == "bos"
    assert config.exit.time_stop_seconds == 12 * 3600
    assert fee.breakdown(
        "ETHUSD", entry_is_maker=False, exit_is_maker=False, hold_seconds=3600
    ).total_bps == pytest.approx(14.8)


def test_v2_emits_only_bias_aligned_closed_candle_bos():
    config = load_htf_structure_break_v2_config(CONFIG)
    fee = htf_structure_v2_fee_model(config)
    candidate = HTFStructureBreakV2Scanner(config, fee).evaluate(_context())
    assert candidate is not None
    assert candidate.side is Side.LONG
    assert candidate.decision_ts == _context().ts
    assert candidate.metadata["event_1h"] == "bos"
    assert candidate.metadata["target_cost_multiple_at_decision"] >= 5.0
    assert candidate.metadata["funding_included_at_decision"] is False


def test_v2_revalidates_geometry_and_cost_at_actual_entry_open():
    config = load_htf_structure_break_v2_config(CONFIG)
    fee = htf_structure_v2_fee_model(config)
    candidate = HTFStructureBreakV2Scanner(config, fee).evaluate(_context())
    assert candidate is not None
    valid, reason, _, _ = geometry_valid_at_entry(candidate, 106.0, fee, 5.0)
    assert valid and reason == "accepted"
    valid, reason, _, _ = geometry_valid_at_entry(candidate, 114.5, fee, 5.0)
    assert not valid and reason == "target_below_5x_cost_at_entry"


def test_only_actual_eight_hour_funding_events_are_costed():
    frame = pd.DataFrame(
        {
            "available_at": pd.date_range("2026-01-01", periods=12, freq="1h", tz="UTC"),
            "funding_rate": [0.0001 + index * 0.000001 for index in range(12)],
        }
    )
    settlements = funding_settlements_from_hourly(frame, interval_seconds=8 * 3600)
    assert [row[0].hour for row in settlements] == [0, 8]
    assert len(settlements) == 2
