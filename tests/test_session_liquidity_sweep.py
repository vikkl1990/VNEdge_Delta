from __future__ import annotations

from collections import deque
from datetime import UTC, datetime, timedelta

import pytest

from vnedge.research.session_liquidity_sweep_backtest import (
    SweepReplayResult,
    selection_gate,
    simulate_symbol_window,
)
from vnedge.scalping.delta_engine.session_sweep import (
    SessionLiquiditySweepScanner,
    SessionSweepContext,
    finalize_next_open,
    load_session_sweep_config,
    session_sweep_fee_model,
)
from vnedge.scalping.delta_engine.types import Candle, Side

CONFIG_PATH = "configs/research/session_liquidity_sweep_v1.yaml"
DAY = datetime(2026, 7, 25, tzinfo=UTC)


def _bar(ts: datetime, *, volume: float = 100.0) -> Candle:
    return Candle(ts, 100.0, 100.15, 99.85, 100.0, volume, "1m")


def _upside_sweep_setup():
    config = load_session_sweep_config(CONFIG_PATH)
    scanner = SessionLiquiditySweepScanner(config)
    history: deque[Candle] = deque(maxlen=160)
    start = DAY - timedelta(hours=2)
    total = int((DAY.replace(hour=8, minute=0) - start).total_seconds() // 60) + 1
    for index in range(total):
        bar = _bar(start + timedelta(minutes=index))
        history.append(bar)
        assert (
            scanner.evaluate(SessionSweepContext("BTCUSD", bar.ts, tuple(history)))
            is None
        )
    signal_ts = DAY.replace(hour=8, minute=1)
    signal = Candle(
        signal_ts,
        100.08,
        100.15 * 1.001,
        100.04,
        100.05,
        200.0,
        "1m",
    )
    history.append(signal)
    setup = scanner.evaluate(SessionSweepContext("BTCUSD", signal.ts, tuple(history)))
    return config, scanner, setup


def test_contract_is_frozen_causal_and_research_only():
    config = load_session_sweep_config(CONFIG_PATH)
    costs = session_sweep_fee_model(config).breakdown(
        "BTCUSD", entry_is_maker=False, hold_seconds=1200
    )

    assert config.contract_id == "session_liquidity_sweep_v1"
    assert config.sessions.asian_expected_bars == 480
    assert config.sessions.london_first_close_utc.isoformat() == "08:01:00"
    assert config.can_trade is False and config.can_promote is False
    assert costs.total_bps == pytest.approx(14.8)


def test_scanner_emits_one_london_upside_sweep_with_full_attribution():
    _, scanner, setup = _upside_sweep_setup()

    assert setup is not None
    assert setup.side is Side.SHORT
    assert setup.session == "london"
    assert setup.wick_ratio >= 0.55
    assert setup.volume_ratio == pytest.approx(2.0)
    assert "close_back_inside" in setup.hierarchy_reason
    assert setup.setup_id.endswith(":london:short")
    assert scanner.evaluate(
        SessionSweepContext(
            "BTCUSD",
            setup.decision_ts,
            (_bar(setup.decision_ts),),
        )
    ) is None


def test_next_open_geometry_rejects_small_target_and_accepts_large_distance():
    config, _, setup = _upside_sweep_setup()
    assert setup is not None
    fee = session_sweep_fee_model(config)
    small = Candle(
        DAY.replace(hour=8, minute=2),
        100.05,
        100.1,
        100.0,
        100.02,
        100.0,
        "1m",
    )
    large = Candle(
        DAY.replace(hour=8, minute=2),
        99.60,
        100.0,
        99.5,
        99.8,
        100.0,
        "1m",
    )

    rejected = finalize_next_open(setup, small, config, fee)
    accepted = finalize_next_open(setup, large, config, fee)

    assert rejected.candidate is None
    assert rejected.reason == "structural_target_below_cost_multiple"
    assert accepted.candidate is not None
    assert accepted.cost_multiple >= 3.5
    assert accepted.candidate.take_profits[0] < large.open


def test_replay_gap_resets_and_fails_data_quality():
    config = load_session_sweep_config(CONFIG_PATH)
    rows = [
        _bar(DAY + timedelta(minutes=index + int(index >= 5)))
        for index in range(10)
    ]

    result = simulate_symbol_window("BTCUSD", rows, config)

    assert result.missing_minutes == 1
    assert result.trades == ()


def test_selection_gate_fails_closed_without_completed_trades():
    config = load_session_sweep_config(CONFIG_PATH)
    start = DAY - timedelta(days=2)
    results = tuple(
        SweepReplayResult(symbol, (), (), 0, 0, 100, start, DAY)
        for symbol in config.data.symbols
    )

    gate = selection_gate(results, config)

    assert gate["passed"] is False
    assert gate["checks"]["minimum_trades"] is False
    assert gate["checks"]["positive_net"] is False
