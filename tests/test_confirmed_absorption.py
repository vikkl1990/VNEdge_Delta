"""Confirmed absorption V3 waits for proof and remains research-only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from vnedge.scalping.delta_engine.absorption import AbsorptionObservation
from vnedge.scalping.delta_engine.confirmed_absorption import (
    ConfirmedAbsorptionConfig,
    ConfirmedAbsorptionReversalScanner,
)
from vnedge.scalping.delta_engine.event_trigger import EventMarketSnapshot
from vnedge.scalping.delta_engine.fee_model import DeltaFeeModel
from vnedge.scalping.delta_engine.types import Side

NOW = datetime(2026, 8, 13, 9, 0, tzinfo=UTC)


def observation(*, direction: int = -1, detected_ns: int = 1_000_000_000):
    return AbsorptionObservation(
        symbol="BTCUSD",
        price=101.0 if direction < 0 else 100.0,
        absorbed_side=(
            "sell_limits_absorbing_buys"
            if direction < 0
            else "buy_limits_absorbing_sells"
        ),
        reversal_direction=direction,
        aggressive_buy_volume=10.0 if direction < 0 else 0.0,
        aggressive_sell_volume=0.0 if direction < 0 else 10.0,
        aggressive_notional_usd=1_000.0,
        dynamic_minimum_notional_usd=500.0,
        duration_ms=500.0,
        price_range_ticks=0.0,
        absorption_ratio=1.0,
        resting_hold_ratio=0.8,
        replenishment_ratio=0.5,
        refresh_count=2,
        strength=0.85,
        detected_monotonic_ns=detected_ns,
    )


def snapshot(
    offset_ms: int,
    *,
    mid: float,
    direction: int,
    source: str,
    absorption: AbsorptionObservation | None = None,
    flow: float | None = None,
    book: float | None = None,
    trend_5m: float = -10.0,
    trend_15m: float = -15.0,
    coverage: float = 900.0,
) -> EventMarketSnapshot:
    signed_flow = flow if flow is not None else direction * 0.8
    signed_book = book if book is not None else direction * 0.6
    return EventMarketSnapshot(
        symbol="BTCUSD",
        decision_ts=NOW + timedelta(milliseconds=offset_ms),
        available_at=NOW + timedelta(milliseconds=offset_ms),
        best_bid=mid - 0.5,
        best_ask=mid + 0.5,
        mid=mid,
        spread_bps=1.0 / mid * 10_000.0,
        book_imbalance=signed_book,
        flow_imbalance=signed_flow,
        cvd_usd=1_000.0 * direction,
        aggressive_buy_usd=1_000.0 if direction > 0 else 0.0,
        aggressive_sell_usd=1_000.0 if direction < 0 else 0.0,
        absorption_score=absorption.strength if absorption else 0.0,
        absorption=absorption,
        bid_wall_distance_bps=1.0,
        ask_wall_distance_bps=1.0,
        funding_rate=0.0,
        open_interest=1_000.0,
        open_interest_delta=0.0,
        liquidation_distance_bps=None,
        liquidation_side=None,
        htf_bias=0,
        regime="unknown",
        vwap_distance_bps=0.0,
        cusum_state="unavailable",
        confirmed_direction=direction,
        confirmation_source=source,
        confirmation_age_ms=500.0,
        confirmation_samples=4,
        book_sequence=1,
        book_healthy=True,
        event_kind="trade",
        source_exchange_ts=NOW + timedelta(milliseconds=offset_ms - 20),
        source_received_at=NOW + timedelta(milliseconds=offset_ms),
        features={
            "last_trade_price": mid,
            "trend_coverage_seconds": coverage,
            "trend_5m_bps": trend_5m,
            "trend_15m_bps": trend_15m,
        },
    )


def scanner(**overrides: object) -> ConfirmedAbsorptionReversalScanner:
    config = ConfirmedAbsorptionConfig(
        tick_sizes={"BTCUSD": 0.5},
        **overrides,
    )
    return ConfirmedAbsorptionReversalScanner(
        DeltaFeeModel(default_slippage_bps_per_leg=1.5),
        config,
    )


def test_absorption_is_pending_until_opposing_flow_and_price_reclaim() -> None:
    engine = scanner()
    setup = observation(direction=-1)
    assert (
        engine.evaluate(
            snapshot(0, mid=100.5, direction=-1, source="absorption", absorption=setup)
        )
        is None
    )
    assert (
        engine.evaluate(
            snapshot(200, mid=100.5, direction=0, source="flow_imbalance", flow=0, book=0)
        )
        is None
    )
    assert (
        engine.evaluate(snapshot(300, mid=99.5, direction=-1, source="flow_imbalance"))
        is None
    )
    assert (
        engine.evaluate(snapshot(500, mid=99.5, direction=-1, source="flow_imbalance"))
        is None
    )
    candidate = engine.evaluate(
        snapshot(700, mid=99.5, direction=-1, source="flow_imbalance")
    )
    assert candidate is not None
    assert candidate.side is Side.SHORT
    assert candidate.time_stop_seconds == 900
    assert candidate.metadata["hypothesis"] == "confirmed_absorption_reversal"
    assert candidate.metadata["post_setup_confirmation_samples"] == 3
    assert candidate.metadata["post_setup_confirmation_ms"] == pytest.approx(400.0)
    assert candidate.metadata["research_only"] is True
    assert candidate.to_dict()["trade_horizon"] == "scalp"


def test_adverse_break_invalidates_setup_before_confirmation() -> None:
    engine = scanner()
    setup = observation(direction=-1)
    engine.evaluate(
        snapshot(0, mid=100.5, direction=-1, source="absorption", absorption=setup)
    )
    assert (
        engine.evaluate(
            snapshot(300, mid=102.5, direction=0, source="flow_imbalance", flow=0, book=0)
        )
        is None
    )
    assert (
        engine.evaluate(snapshot(700, mid=99.5, direction=-1, source="flow_imbalance"))
        is None
    )
    assert engine.telemetry()["counts"]["adverse_invalidations"] == 1


def test_trend_veto_blocks_countertrend_reversal() -> None:
    engine = scanner()
    setup = observation(direction=1)
    engine.evaluate(
        snapshot(
            0,
            mid=100.5,
            direction=1,
            source="absorption",
            absorption=setup,
            trend_5m=-20,
            trend_15m=-30,
        )
    )
    for offset in (100, 300):
        assert (
            engine.evaluate(
                snapshot(
                    offset,
                    mid=101.5,
                    direction=1,
                    source="flow_imbalance",
                    trend_5m=-20,
                    trend_15m=-30,
                )
            )
            is None
        )
    candidate = engine.evaluate(
        snapshot(
            500,
            mid=101.5,
            direction=1,
            source="flow_imbalance",
            trend_5m=-20,
            trend_15m=-30,
        )
    )
    assert candidate is None
    assert engine.telemetry()["counts"]["trend_vetoes"] == 1


def test_scanner_leaves_active_observation_lock_to_shared_engine() -> None:
    engine = scanner()
    setup = observation(direction=-1)
    engine.evaluate(
        snapshot(0, mid=100.5, direction=-1, source="absorption", absorption=setup)
    )
    for offset in (100, 300):
        assert (
            engine.evaluate(snapshot(offset, mid=99.5, direction=-1, source="flow_imbalance"))
            is None
        )
    assert engine.evaluate(snapshot(500, mid=99.5, direction=-1, source="flow_imbalance"))
    second = observation(direction=-1, detected_ns=2_000_000_000)
    assert (
        engine.evaluate(
            snapshot(
                600,
                mid=100.5,
                direction=-1,
                source="absorption",
                absorption=second,
            )
        )
        is None
    )
    telemetry = engine.telemetry()
    assert telemetry["counts"]["setups"] == 2
    assert telemetry["pending_symbols"] == ["BTCUSD"]
    assert telemetry["observation_lock_owner"] == "event_trigger_layer_after_shared_gates"


def test_contract_rejects_non_positive_tick_size() -> None:
    with pytest.raises(ValueError, match="positive symbol tick"):
        ConfirmedAbsorptionConfig(tick_sizes={"BTCUSD": 0.0})


def test_contract_rejects_target_that_cannot_clear_cost_multiple() -> None:
    with pytest.raises(ValueError, match="does not clear the frozen cost multiple"):
        ConfirmedAbsorptionReversalScanner(
            DeltaFeeModel(default_slippage_bps_per_leg=1.5),
            ConfirmedAbsorptionConfig(
                tick_sizes={"BTCUSD": 0.5},
                target_bps=30.0,
            ),
        )
