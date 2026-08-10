"""Level-aware absorption detection stays causal, bounded, and fail-closed."""

from __future__ import annotations

import pytest

from vnedge.exchange.delta_contracts import DeltaContractSpec
from vnedge.scalping.delta_engine.absorption import (
    AbsorptionDetector,
    AbsorptionDetectorConfig,
    AbsorptionInstrumentConfig,
)

BASE_NS = 1_000_000_000


def detector(**overrides) -> AbsorptionDetector:
    instrument = AbsorptionInstrumentConfig(
        symbol="BTCUSD",
        tick_size=0.5,
        minimum_aggressive_notional_usd=500.0,
    )
    values = {"instruments": (instrument,), "relative_trade_multiple": 3.0}
    values.update(overrides)
    config = AbsorptionDetectorConfig(**values)
    return AbsorptionDetector(instrument, config)


def buy_hit(engine: AbsorptionDetector, offset_ms: int, *, mid: float = 100.5):
    return engine.on_trade(
        price=101.0,
        size=2.0,
        notional_usd=202.0,
        side="buy",
        current_resting_size=20.0,
        mid=mid,
        now_ns=BASE_NS + offset_ms * 1_000_000,
    )


def prime_buy_absorption(engine: AbsorptionDetector) -> None:
    assert buy_hit(engine, 100) is None
    assert buy_hit(engine, 150) is None
    assert buy_hit(engine, 200) is None


def test_classic_buy_absorption_requires_exhaustion_and_resting_capacity() -> None:
    engine = detector()
    prime_buy_absorption(engine)
    assert engine.on_book_delta(
        side="ask",
        price=101.0,
        previous_size=20.0,
        current_size=10.0,
        mid=100.5,
        now_ns=BASE_NS + 250_000_000,
    ) is None
    observation = engine.on_book_delta(
        side="ask",
        price=101.0,
        previous_size=10.0,
        current_size=18.0,
        mid=100.5,
        now_ns=BASE_NS + 400_000_000,
    )
    assert observation is not None
    assert observation.absorbed_side == "sell_limits_absorbing_buys"
    assert observation.reversal_direction == -1
    assert observation.aggressive_notional_usd == pytest.approx(606.0)
    assert observation.dynamic_minimum_notional_usd == pytest.approx(606.0)
    assert observation.duration_ms == pytest.approx(100.0)
    assert observation.price_range_ticks == pytest.approx(0.0)
    assert observation.absorption_ratio == pytest.approx(1.0)
    assert observation.resting_hold_ratio == pytest.approx(0.9)
    assert observation.replenishment_ratio == pytest.approx(1.0)
    assert observation.refresh_count == 1
    assert observation.strength > 0.7
    assert observation.research_only and not observation.used_for_execution


def test_price_break_beyond_tick_tolerance_rejects_absorption() -> None:
    engine = detector()
    prime_buy_absorption(engine)
    assert engine.on_time(mid=102.0, now_ns=BASE_NS + 400_000_000) is None
    assert engine.latest(BASE_NS + 400_000_000) is None


def test_opposing_flow_can_confirm_before_exhaustion_timeout() -> None:
    engine = detector(exhaustion_ms=1_000, opposing_flow_ratio=0.15)
    prime_buy_absorption(engine)
    observation = engine.on_trade(
        price=100.0,
        size=1.0,
        notional_usd=100.0,
        side="sell",
        current_resting_size=20.0,
        mid=100.5,
        now_ns=BASE_NS + 250_000_000,
    )
    assert observation is not None
    assert observation.absorbed_side == "sell_limits_absorbing_buys"
    assert observation.reversal_direction == -1


def test_sell_absorption_at_bid_mirrors_to_long_reversal() -> None:
    engine = detector()
    for offset in (100, 150, 200):
        assert engine.on_trade(
            price=100.0,
            size=2.0,
            notional_usd=200.0,
            side="sell",
            current_resting_size=20.0,
            mid=100.5,
            now_ns=BASE_NS + offset * 1_000_000,
        ) is None
    observation = engine.on_time(mid=100.5, now_ns=BASE_NS + 400_000_000)
    assert observation is not None
    assert observation.absorbed_side == "buy_limits_absorbing_sells"
    assert observation.reversal_direction == 1


def test_tiny_volume_never_becomes_absorption() -> None:
    engine = detector(relative_trade_multiple=0.0)
    assert engine.on_trade(
        price=101.0,
        size=0.1,
        notional_usd=10.1,
        side="buy",
        current_resting_size=20.0,
        mid=100.5,
        now_ns=BASE_NS + 100_000_000,
    ) is None
    assert engine.on_trade(
        price=101.0,
        size=0.1,
        notional_usd=10.1,
        side="buy",
        current_resting_size=20.0,
        mid=100.5,
        now_ns=BASE_NS + 200_000_000,
    ) is None
    assert engine.on_time(mid=100.5, now_ns=BASE_NS + 500_000_000) is None


def test_price_level_cooldown_blocks_repeat_observation() -> None:
    engine = detector(price_level_cooldown_ms=20_000)
    prime_buy_absorption(engine)
    first = engine.on_time(mid=100.5, now_ns=BASE_NS + 400_000_000)
    assert first is not None
    prime_start = BASE_NS + 1_500_000_000
    for offset in (0, 50, 100):
        assert engine.on_trade(
            price=101.0,
            size=2.0,
            notional_usd=202.0,
            side="buy",
            current_resting_size=20.0,
            mid=100.5,
            now_ns=prime_start + offset * 1_000_000,
        ) is None
    assert engine.on_time(mid=100.5, now_ns=prime_start + 400_000_000) is None


def test_detector_config_requires_explicit_unique_instrument_contracts() -> None:
    assert AbsorptionDetectorConfig().instrument("BTCUSD") is None
    row = AbsorptionInstrumentConfig(
        symbol="btcusd",
        tick_size=0.5,
        minimum_aggressive_notional_usd=500,
    )
    with pytest.raises(ValueError, match="unique"):
        AbsorptionDetectorConfig(instruments=(row, row))


def test_instrument_contract_uses_exchange_tick_metadata_without_guessing() -> None:
    row = AbsorptionInstrumentConfig.from_delta_contract(
        DeltaContractSpec(symbol="BTCUSD", tick_size=0.5),
        minimum_aggressive_notional_usd=500,
    )
    assert row.tick_size == pytest.approx(0.5)
    with pytest.raises(ValueError, match="tick_size"):
        AbsorptionInstrumentConfig.from_delta_contract(
            DeltaContractSpec(symbol="ETHUSD", tick_size=None),
            minimum_aggressive_notional_usd=500,
        )


def test_stacked_absorption_combines_subthreshold_levels_in_tight_band() -> None:
    engine = detector(relative_trade_multiple=0.0)
    events = (
        (100, 101.0),
        (110, 101.5),
        (200, 101.0),
        (210, 101.5),
    )
    for offset, price in events:
        assert engine.on_trade(
            price=price,
            size=2.0,
            notional_usd=price * 2.0,
            side="buy",
            current_resting_size=20.0,
            mid=100.5,
            now_ns=BASE_NS + offset * 1_000_000,
        ) is None
    observation = engine.on_time(mid=100.5, now_ns=BASE_NS + 450_000_000)
    assert observation is not None
    assert observation.is_stacked
    assert observation.stacked_levels == (101.0, 101.5)
    assert observation.price == pytest.approx(101.25)
    assert observation.reversal_direction == -1
    assert observation.aggressive_notional_usd == pytest.approx(810.0)


def test_liquidation_proximity_is_metadata_and_never_boosts_strength() -> None:
    plain = detector()
    enriched = detector()
    enriched.on_liquidation(
        price=102.0,
        size=50.0,
        liquidated_side="short",
        now_ns=BASE_NS + 50_000_000,
    )
    prime_buy_absorption(plain)
    prime_buy_absorption(enriched)
    plain_event = plain.on_time(mid=100.5, now_ns=BASE_NS + 400_000_000)
    enriched_event = enriched.on_time(mid=100.5, now_ns=BASE_NS + 400_000_000)
    assert plain_event is not None and enriched_event is not None
    assert enriched_event.liquidation_cluster_side == "short"
    assert enriched_event.liquidation_distance_ticks == pytest.approx(2.0)
    assert enriched_event.liquidation_relation == "nearby_short_liquidations"
    assert enriched_event.strength == pytest.approx(plain_event.strength)


def test_dashboard_snapshot_contains_footprint_timeline_and_session_map() -> None:
    engine = detector(relative_trade_multiple=0.0)
    for offset, price in ((100, 101.0), (110, 101.5), (200, 101.0), (210, 101.5)):
        engine.on_trade(
            price=price,
            size=2.0,
            notional_usd=price * 2.0,
            side="buy",
            current_resting_size=20.0,
            mid=100.5,
            now_ns=BASE_NS + offset * 1_000_000,
        )
    assert engine.on_time(mid=100.5, now_ns=BASE_NS + 450_000_000) is not None
    payload = engine.dashboard_snapshot(
        bids=((100.0, 10.0),),
        asks=((101.0, 20.0), (101.5, 20.0)),
        now_ns=BASE_NS + 500_000_000,
    )
    assert payload["latest"]["is_stacked"] is True
    assert payload["timeline"]
    assert payload["session_map"]
    assert payload["strength_gauge"] > 0
    assert payload["liquidation_strength_applied_to_signal"] is False
    markers = {row["price"]: row["absorption_marker"] for row in payload["footprint"]}
    assert markers[101.0] == "stacked"
    assert markers[101.5] == "stacked"
