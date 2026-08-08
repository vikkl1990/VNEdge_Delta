from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from vnedge.research.continuous_mtf_backtest import (
    ContinuousReplayResult,
    selection_gate,
    simulate_symbol_selection,
)
from vnedge.scalping.delta_engine.candle_store import MultiTimeframeCandleStore
from vnedge.scalping.delta_engine.continuous_mtf import (
    ContinuousMultiTFStateMachine,
    ContinuousSetupIntent,
    MechanicalSetupZone,
    MultiTFState,
    finalize_continuous_entry,
    load_continuous_mtf_config,
)
from vnedge.scalping.delta_engine.fee_model import DeltaFeeModel
from vnedge.scalping.delta_engine.types import Candle, Side

CONFIG_PATH = "configs/research/continuous_mtf_alignment_v1.yaml"
START = datetime(2025, 1, 1, tzinfo=UTC)


def _append(store, symbol, candle):
    store.append_closed(symbol, candle, observed_at=candle.ts)


def _trend_bar(ts, price, tf, *, volume=100.0, high_extra=2.0):
    return Candle(ts, price, price + high_extra, price - 1.0, price + 1.0, volume, tf)


def _aligned_machine():
    config, _ = load_continuous_mtf_config(CONFIG_PATH)
    store = MultiTimeframeCandleStore(max_bars_per_timeframe=700)
    symbol = "BTCUSD"
    four_hour = []
    for index in range(54):
        bar = _trend_bar(START + timedelta(hours=4 * (index + 1)), 100 + index * 2, "4h")
        _append(store, symbol, bar)
        four_hour.append(bar)
    # Histories for lower timeframes are already closed before the staged updates.
    base = four_hour[-1].ts
    for index in range(30):
        high_extra = 20.0 if index == 20 else 0.4
        bar = Candle(
            base - timedelta(hours=29 - index),
            207 + index * 0.1,
            207 + index * 0.1 + high_extra,
            206.8 + index * 0.1,
            207.1 + index * 0.1,
            100.0,
            "1h",
        )
        _append(store, symbol, bar)
    for index in range(23):
        price = 211.0 + index * 0.05
        _append(
            store,
            symbol,
            Candle(
                base - timedelta(minutes=15 * (22 - index)),
                price,
                price + 0.25,
                price - 0.25,
                price + 0.05,
                100.0,
                "15m",
            ),
        )
    for index in range(20):
        price = 212.0 + index * 0.02
        _append(
            store,
            symbol,
            Candle(
                base - timedelta(minutes=5 * (19 - index)),
                price,
                price + 0.15,
                price - 0.15,
                price + 0.02,
                90.0 if index % 2 == 0 else 110.0,
                "5m",
            ),
        )
    for index in range(20):
        price = 213.0 + index * 0.01
        _append(
            store,
            symbol,
            Candle(
                base - timedelta(minutes=19 - index),
                price,
                price + 0.08,
                price - 0.08,
                price + 0.01,
                90.0 if index % 2 == 0 else 110.0,
                "1m",
            ),
        )
    machine = ContinuousMultiTFStateMachine(store, config, subscribe=False)
    machine.on_closed_candle(symbol, four_hour[-1])
    next_4h = _trend_bar(base + timedelta(hours=4), 208.0, "4h")
    _append(store, symbol, next_4h)
    machine.on_closed_candle(symbol, next_4h)
    return config, store, machine, symbol, next_4h.ts


def test_contract_is_research_only_and_never_expands_target_floor():
    config, raw = load_continuous_mtf_config(CONFIG_PATH)

    assert raw["contract_id"] == "continuous_mtf_alignment_v1"
    assert raw["can_trade"] is False and raw["can_promote"] is False
    assert config.minimum_target_cost_multiple == 4.0
    assert raw["geometry"]["target_floor_policy"] == "reject_never_expand_a_structural_target"


def test_continuous_stack_only_triggers_after_every_layer_updates():
    _, store, machine, symbol, ts = _aligned_machine()
    assert machine.snapshot(symbol).bias_4h == 1

    one_hour = Candle(ts, 212.0, 214.5, 211.8, 214.0, 120.0, "1h")
    _append(store, symbol, one_hour)
    machine.on_closed_candle(symbol, one_hour)
    assert machine.snapshot(symbol).structure_1h.startswith("aligned")

    setup_bar = Candle(ts + timedelta(minutes=15), 212.8, 214.2, 211.9, 213.8, 125.0, "15m")
    _append(store, symbol, setup_bar)
    machine.on_closed_candle(symbol, setup_bar)
    assert machine.snapshot(symbol).setup_15m == "pullback"
    assert machine.claim_intent(symbol, decision_price=setup_bar.close) is None

    confirmation = Candle(ts + timedelta(minutes=20), 213.7, 215.2, 213.5, 215.0, 120.0, "5m")
    _append(store, symbol, confirmation)
    machine.on_closed_candle(symbol, confirmation)
    assert machine.snapshot(symbol).confirmation_5m is True
    assert machine.claim_intent(symbol, decision_price=confirmation.close) is None

    trigger = Candle(ts + timedelta(minutes=21), 214.8, 216.2, 214.7, 216.0, 140.0, "1m")
    _append(store, symbol, trigger)
    machine.on_closed_candle(symbol, trigger)
    state = machine.snapshot(symbol)
    assert state.stack_aligned is True
    intent = machine.claim_intent(symbol, decision_price=trigger.close)
    assert intent is not None
    assert intent.setup.structural_target > trigger.close
    assert machine.claim_intent(symbol, decision_price=trigger.close) is None


def test_higher_timeframe_neutralization_cancels_lower_state():
    _, store, machine, symbol, ts = _aligned_machine()
    memory = machine._memory(symbol)  # explicit state-invalidation unit seam
    memory.zone = MechanicalSetupZone(
        "setup", "pullback", Side.LONG, ts, ts + timedelta(minutes=90), 200, 220, 240, "test"
    )
    memory.confirmation_ts = ts
    memory.trigger_1m = True
    memory.bias_4h = 1
    memory.bias_candidate = 1
    memory.bias_stable_closes = 2

    flat = Candle(ts + timedelta(hours=4), 210.0, 211.0, 49.0, 50.0, 100.0, "4h")
    # A single conflicting 4h candidate neutralizes the public bias immediately.
    _append(store, symbol, flat)
    machine.on_closed_candle(symbol, flat)

    state = machine.snapshot(symbol)
    assert state.bias_4h == 0
    assert state.zone_15m is None
    assert state.confirmation_5m is False
    assert "4h_bias_flip_or_neutral" in state.conflict_flags


def test_next_open_geometry_rejects_small_target_and_accepts_real_structure():
    config, _ = load_continuous_mtf_config(CONFIG_PATH)
    state = MultiTFState("BTCUSD", bias_4h=1, stack_aligned=True, last_update_ts=START)
    fee = DeltaFeeModel()
    entry_bar = Candle(START + timedelta(minutes=1), 100, 101, 99, 100, 100, "1m")

    def geometry(target):
        zone = MechanicalSetupZone(
            "setup",
            "pullback",
            Side.LONG,
            START,
            START + timedelta(minutes=90),
            99.95,
            101,
            target,
            "test",
        )
        intent = ContinuousSetupIntent(
            "continuous_mtf_alignment_v1", "BTCUSD", Side.LONG, START, 100, zone, state
        )
        return finalize_continuous_entry(intent, entry_bar, config, fee)

    rejected = geometry(100.40)
    accepted = geometry(102.00)

    assert rejected.status == "rejected"
    assert rejected.reason == "structural_target_below_cost_multiple"
    assert accepted.status == "accepted"
    assert accepted.cost_bps == pytest.approx(14.8)
    assert accepted.candidate is not None
    assert accepted.candidate.metadata["target_floor_policy"] == (
        "reject_never_expand_structural_target"
    )


def test_selection_replay_resets_the_entire_state_on_a_source_gap():
    config_path = Path(CONFIG_PATH)
    rows = [
        Candle(
            START + timedelta(minutes=index + 1 + int(index >= 5)),
            100,
            101,
            99,
            100,
            1,
            "1m",
        )
        for index in range(10)
    ]

    result = simulate_symbol_selection(
        "BTCUSD",
        rows,
        config_path,
        score_end=rows[-1].ts,
        process_end=rows[-1].ts,
    )

    assert result.missing_minutes == 1
    assert result.trade_records == ()
    assert result.state_counters["source_gap_reset"] == 1


def test_selection_gate_fails_closed_without_completed_trades():
    _, raw = load_continuous_mtf_config(CONFIG_PATH)
    result = ContinuousReplayResult(
        "BTCUSD",
        (),
        (),
        {},
        100,
        0,
        0,
        START,
        START + timedelta(days=1),
    )

    gate = selection_gate((result,), raw)

    assert gate["passed"] is False
    assert gate["checks"]["minimum_trades"] is False
    assert gate["checks"]["positive_net"] is False
