from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from vnedge.research.continuous_mtf_backtest import simulate_symbol_selection
from vnedge.scalping.delta_engine.mechanical_structure import (
    ConfirmedSwing,
    MechanicalStructureConfig,
    confirmed_swings,
    current_swing_trend,
    detect_structure_event,
    load_mechanical_structure_config,
)
from vnedge.scalping.delta_engine.types import Candle

START = datetime(2025, 1, 1, tzinfo=UTC)
CONFIG = Path("configs/research/continuous_mtf_alignment_v2.yaml")


def _candle(index: int, high: float, low: float, close: float | None = None) -> Candle:
    resolved_close = (high + low) / 2 if close is None else close
    return Candle(
        START + timedelta(hours=index + 1),
        resolved_close,
        high,
        low,
        resolved_close,
        1.0,
        "1h",
    )


def _swing(index: int, kind: str, price: float) -> ConfirmedSwing:
    ts = START + timedelta(hours=index)
    return ConfirmedSwing(
        f"1h:{kind}:{index}",
        "1h",
        kind,
        ts,
        ts + timedelta(hours=3),
        price,
        index,
    )


def test_swing_is_invisible_until_all_right_confirmation_bars_close():
    config = MechanicalStructureConfig(3, 3, 0.0, 5, 24)
    rows = tuple(
        _candle(index, high, high - 2.0)
        for index, high in enumerate((10.0, 11.0, 12.0, 20.0, 12.0, 11.0, 10.0))
    )

    before_confirmation = confirmed_swings(rows[:-1], config)
    after_confirmation = confirmed_swings(rows, config)
    after_more_data = confirmed_swings(rows + (_candle(7, 25.0, 23.0),), config)

    assert before_confirmation == ()
    assert len(after_confirmation) == 1
    assert after_confirmation[0].kind == "high"
    assert after_confirmation[0].ts == rows[3].ts
    assert after_confirmation[0].confirmed_at == rows[6].ts
    assert after_more_data[0] == after_confirmation[0]


def test_trend_uses_higher_highs_and_higher_lows_from_recent_swings():
    bullish = (
        _swing(1, "high", 100.0),
        _swing(2, "low", 90.0),
        _swing(3, "high", 105.0),
        _swing(4, "low", 95.0),
    )
    bearish = (
        _swing(1, "high", 110.0),
        _swing(2, "low", 100.0),
        _swing(3, "high", 105.0),
        _swing(4, "low", 95.0),
    )

    assert current_swing_trend(bullish, 5) == 1
    assert current_swing_trend(bearish, 5) == -1


def test_minimum_swing_filter_uses_the_last_accepted_opposite_swing():
    rows = tuple(
        _candle(index, high, low)
        for index, (high, low) in enumerate(
            ((100.09, 100.06), (100.10, 100.07), (100.09, 100.05), (100.08, 100.06))
        )
    )

    unfiltered = confirmed_swings(rows, MechanicalStructureConfig(1, 1, 0.0, 4, 24))
    filtered = confirmed_swings(rows, MechanicalStructureConfig(1, 1, 8.0, 4, 24))

    assert [swing.kind for swing in unfiltered] == ["high", "low"]
    assert [swing.kind for swing in filtered] == ["high"]


def test_bos_uses_latest_high_even_when_latest_overall_swing_is_a_low():
    swings = (
        _swing(1, "high", 100.0),
        _swing(2, "low", 90.0),
        _swing(3, "high", 105.0),
        _swing(4, "low", 95.0),
    )
    bar = _candle(10, 107.0, 104.0, 106.0)

    event = detect_structure_event((bar,), swings, 1)

    assert event is not None
    assert event.event_type == "bos"
    assert event.direction == 1
    assert event.broken_swing == swings[2]


def test_choch_uses_latest_low_even_when_latest_overall_swing_is_a_high():
    swings = (
        _swing(1, "low", 90.0),
        _swing(2, "high", 100.0),
        _swing(3, "low", 95.0),
        _swing(4, "high", 105.0),
    )
    bar = _candle(10, 95.0, 92.0, 94.0)

    event = detect_structure_event((bar,), swings, 1)

    assert event is not None
    assert event.event_type == "choch"
    assert event.direction == -1
    assert event.broken_swing == swings[2]


def test_confirmed_swing_can_emit_only_one_break_event():
    swings = (
        _swing(1, "high", 100.0),
        _swing(2, "low", 90.0),
        _swing(3, "high", 105.0),
        _swing(4, "low", 95.0),
    )
    first = detect_structure_event((_candle(10, 107.0, 104.0, 106.0),), swings, 1)
    assert first is not None

    duplicate = detect_structure_event(
        (_candle(11, 108.0, 105.0, 107.0),),
        swings,
        1,
        already_broken_swing_ids={first.broken_swing.swing_id},
    )

    assert duplicate is None


def test_v2_contract_loads_and_replay_path_selects_v2_machine():
    config = load_mechanical_structure_config(CONFIG)
    rows = [
        Candle(START + timedelta(minutes=index + 1), 100, 101, 99, 100, 1, "1m")
        for index in range(10)
    ]

    result = simulate_symbol_selection(
        "BTCUSD",
        rows,
        CONFIG,
        score_end=rows[-1].ts,
        process_end=rows[-1].ts,
    )

    assert config.swing_left == 3 and config.swing_right == 3
    assert config.minimum_swing_bps == 8.0
    assert result.trade_records == ()
