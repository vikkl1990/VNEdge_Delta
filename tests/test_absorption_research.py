"""Event-time absorption outcomes: next-trade entry and causal path labels."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from vnedge.scalping.delta_engine.absorption import (
    AbsorptionInstrumentConfig,
    AbsorptionObservation,
)
from vnedge.scalping.delta_engine.absorption_research import (
    AbsorptionResearchConfig,
    AbsorptionResearchTracker,
    summarize_absorption_outcomes,
)
from vnedge.scalping.delta_engine.fee_model import DeltaFeeModel

NOW = datetime(2026, 8, 9, 8, 0, tzinfo=UTC)
BASE_NS = 2_000_000_000


def observation(*, detected_ns: int = BASE_NS, direction: int = 1):
    return AbsorptionObservation(
        symbol="BTCUSD",
        price=100.0,
        absorbed_side=(
            "buy_limits_absorbing_sells"
            if direction > 0
            else "sell_limits_absorbing_buys"
        ),
        reversal_direction=direction,
        aggressive_buy_volume=0.0 if direction > 0 else 10.0,
        aggressive_sell_volume=10.0 if direction > 0 else 0.0,
        aggressive_notional_usd=1_000.0,
        dynamic_minimum_notional_usd=500.0,
        duration_ms=200.0,
        price_range_ticks=1.0,
        absorption_ratio=0.9,
        resting_hold_ratio=0.8,
        replenishment_ratio=0.5,
        refresh_count=2,
        strength=0.8,
        detected_monotonic_ns=detected_ns,
        is_stacked=True,
        stacked_levels=(99.5, 100.0),
        liquidation_distance_ticks=3.0,
        liquidation_cluster_side="long",
        liquidation_cluster_size=20.0,
        liquidation_relation="nearby_long_liquidations",
    )


def tracker() -> AbsorptionResearchTracker:
    instrument = AbsorptionInstrumentConfig(
        symbol="BTCUSD",
        tick_size=0.5,
        minimum_aggressive_notional_usd=500.0,
    )
    fee = DeltaFeeModel(
        maker_fee_bps_pre_tax=0,
        taker_fee_bps_pre_tax=0,
        default_slippage_bps_per_leg=0,
    )
    return AbsorptionResearchTracker(
        fee,
        (instrument,),
        config=AbsorptionResearchConfig(
            target_1_ticks=2,
            target_2_ticks=4,
            stop_ticks=2,
            horizon_ms=1_000,
            entry_timeout_ms=500,
        ),
    )


def test_next_trade_entry_tracks_mfe_mae_and_both_targets_to_horizon() -> None:
    engine = tracker()
    event = observation()
    assert engine.register(event, decision_ts=NOW)
    assert not engine.register(event, decision_ts=NOW)
    assert engine.on_trade(
        "BTCUSD",
        price=100.0,
        received_at=NOW + timedelta(milliseconds=100),
        monotonic_ns=BASE_NS + 100_000_000,
    ) == ()
    assert engine.on_trade(
        "BTCUSD",
        price=101.0,
        received_at=NOW + timedelta(milliseconds=200),
        monotonic_ns=BASE_NS + 200_000_000,
    ) == ()
    assert engine.on_trade(
        "BTCUSD",
        price=102.0,
        received_at=NOW + timedelta(milliseconds=300),
        monotonic_ns=BASE_NS + 300_000_000,
    ) == ()
    outcomes = engine.on_trade(
        "BTCUSD",
        price=101.5,
        received_at=NOW + timedelta(milliseconds=1_100),
        monotonic_ns=BASE_NS + 1_100_000_000,
    )
    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.entry_price == pytest.approx(100.0)
    assert outcome.realized_exit_reason == "target_1"
    assert outcome.realized_exit_ts == (NOW + timedelta(milliseconds=200)).isoformat()
    assert outcome.realized_gross_ticks == pytest.approx(2.0)
    assert outcome.realized_net_ticks == pytest.approx(2.0)
    assert outcome.mfe_ticks == pytest.approx(4.0)
    assert outcome.mae_ticks == pytest.approx(0.0)
    assert outcome.time_to_mfe_ms == pytest.approx(200.0)
    assert outcome.hit_target_1 and outcome.hit_target_2
    assert outcome.was_stacked and outcome.had_liquidation_confluence
    assert outcome.volume_percentile == pytest.approx(1.0)
    assert outcome.to_dict()["order_route"] == "absent"


def test_stop_is_locked_as_realized_exit_while_path_stays_observed() -> None:
    engine = tracker()
    event = observation()
    assert engine.register(event, decision_ts=NOW)
    engine.on_trade(
        "BTCUSD",
        price=100.0,
        received_at=NOW + timedelta(milliseconds=100),
        monotonic_ns=BASE_NS + 100_000_000,
    )
    engine.on_trade(
        "BTCUSD",
        price=99.0,
        received_at=NOW + timedelta(milliseconds=200),
        monotonic_ns=BASE_NS + 200_000_000,
    )
    outcomes = engine.on_trade(
        "BTCUSD",
        price=102.0,
        received_at=NOW + timedelta(milliseconds=1_100),
        monotonic_ns=BASE_NS + 1_100_000_000,
    )
    outcome = outcomes[0]
    assert outcome.realized_exit_reason == "stop"
    assert outcome.realized_gross_ticks == pytest.approx(-2.0)
    assert outcome.stopped_out
    assert outcome.mfe_ticks == pytest.approx(4.0)
    assert outcome.mae_ticks == pytest.approx(2.0)


def test_missing_next_trade_is_recorded_not_silently_dropped() -> None:
    engine = tracker()
    assert engine.register(observation(), decision_ts=NOW)
    outcomes = engine.on_trade(
        "BTCUSD",
        price=100.0,
        received_at=NOW + timedelta(milliseconds=600),
        monotonic_ns=BASE_NS + 600_000_000,
    )
    assert len(outcomes) == 1
    assert outcomes[0].realized_exit_reason == "missed_entry"
    assert outcomes[0].entry_price is None
    assert outcomes[0].realized_exit_ts is None


def test_weekly_summary_separates_stacked_and_liquidation_cohorts() -> None:
    engine = tracker()
    assert engine.register(observation(), decision_ts=NOW)
    engine.on_trade(
        "BTCUSD",
        price=100.0,
        received_at=NOW + timedelta(milliseconds=100),
        monotonic_ns=BASE_NS + 100_000_000,
    )
    engine.on_trade(
        "BTCUSD",
        price=101.0,
        received_at=NOW + timedelta(milliseconds=200),
        monotonic_ns=BASE_NS + 200_000_000,
    )
    win = engine.on_trade(
        "BTCUSD",
        price=101.0,
        received_at=NOW + timedelta(milliseconds=1_100),
        monotonic_ns=BASE_NS + 1_100_000_000,
    )[0]
    loss = replace(
        win,
        key="absorption:synthetic-loss",
        realized_exit_reason="stop",
        realized_gross_ticks=-2.0,
        realized_net_ticks=-2.0,
        mfe_ticks=1.0,
        mae_ticks=2.0,
        hit_target_1=False,
        hit_target_2=False,
        stopped_out=True,
        was_stacked=False,
        had_liquidation_confluence=False,
        time_to_mfe_ms=400.0,
    )

    summary = summarize_absorption_outcomes([win, loss])

    assert summary.observations == 2
    assert summary.completed == 2
    assert summary.target_1_win_rate == pytest.approx(0.5)
    assert summary.expectancy_net_ticks == pytest.approx(0.0)
    assert summary.profit_factor == pytest.approx(1.0)
    assert summary.stacked_win_rate == pytest.approx(1.0)
    assert summary.single_win_rate == pytest.approx(0.0)
    assert summary.liquidation_win_rate == pytest.approx(1.0)
    assert summary.no_liquidation_win_rate == pytest.approx(0.0)
    assert summary.false_absorption_rate == pytest.approx(0.5)
    assert summary.median_time_to_mfe_ms == pytest.approx(250.0)
