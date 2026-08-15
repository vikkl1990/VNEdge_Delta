from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from vnedge.runtime.delta_scalper_shadow import DeltaScalperShadowService
from vnedge.scalping.delta_engine import (
    Candle,
    MarketContextBuilder,
    MultiTimeframeCandleEngine,
    MultiTimeframeCandleStore,
    Side,
    SignalCandidate,
)

BASE = datetime(2026, 8, 14, tzinfo=UTC)


def minute(index: int, *, close: float | None = None) -> Candle:
    price = 100.0 + index
    settled = price + 0.5 if close is None else close
    return Candle(
        ts=BASE + timedelta(minutes=index + 1),
        open=price,
        high=max(price, settled) + 0.25,
        low=min(price, settled) - 0.25,
        close=settled,
        volume=10.0 + index,
        tf="1m",
    )


def test_one_minute_mode_exposes_partial_higher_timeframes_without_promoting_them():
    engine = MultiTimeframeCandleEngine(
        timeframes=("1m", "3m", "5m", "15m"), input_mode="one_minute"
    )

    engine.on_closed_one_minute("BTCUSD", minute(0), observed_at=BASE + timedelta(minutes=1))
    engine.on_closed_one_minute("BTCUSD", minute(1), observed_at=BASE + timedelta(minutes=2))
    snapshot = engine.snapshot("BTCUSD")

    assert len(snapshot.states["1m"].completed) == 2
    assert snapshot.states["1m"].forming is None
    five = snapshot.states["5m"].forming
    assert five is not None
    assert five.start_ts == BASE
    assert five.end_ts == BASE + timedelta(minutes=5)
    assert five.open == 100.0
    assert five.close == 101.5
    assert five.volume == 21.0
    assert five.progress == pytest.approx(0.4)
    assert five.remaining_seconds == pytest.approx(180.0)
    assert five.complete is False
    assert engine.completed("BTCUSD", "5m") == ()


def test_complete_minute_bucket_is_promoted_exactly_at_boundary():
    engine = MultiTimeframeCandleEngine(timeframes=("1m", "5m"), input_mode="one_minute")
    for index in range(5):
        candle = minute(index)
        assert engine.on_closed_one_minute("BTCUSD", candle, observed_at=candle.ts)

    snapshot = engine.snapshot("BTCUSD")
    assert snapshot.states["5m"].forming is None
    assert len(snapshot.states["5m"].completed) == 1
    completed = snapshot.states["5m"].completed[0]
    assert completed.ts == BASE + timedelta(minutes=5)
    assert completed.open == 100.0
    assert completed.close == 104.5
    assert completed.volume == sum(10.0 + index for index in range(5))


def test_partial_first_bucket_is_context_only_and_never_promoted():
    engine = MultiTimeframeCandleEngine(timeframes=("1m", "5m"), input_mode="one_minute")
    for index in range(2, 5):
        candle = minute(index)
        engine.on_closed_one_minute("ETHUSD", candle, observed_at=candle.ts)

    state = engine.snapshot("ETHUSD").states["5m"]
    assert state.completed == ()
    assert state.forming is None
    assert state.incomplete_buckets_dropped == 1


def test_gap_invalidates_forming_buckets_and_is_visible_in_snapshot():
    engine = MultiTimeframeCandleEngine(timeframes=("1m", "5m"), input_mode="one_minute")
    first = minute(0)
    third = minute(2)
    engine.on_closed_one_minute("BTCUSD", first, observed_at=first.ts)
    engine.on_closed_one_minute("BTCUSD", third, observed_at=third.ts)

    snapshot = engine.snapshot("BTCUSD")
    assert snapshot.continuity_ok is False
    assert snapshot.last_gap_reason is not None
    assert snapshot.last_gap_reason.startswith("1m_gap:")
    assert snapshot.states["5m"].forming is not None
    assert snapshot.states["5m"].forming.continuity_ok is False
    assert snapshot.states["5m"].incomplete_buckets_dropped == 1


def test_tick_mode_builds_forming_bars_and_closes_on_first_tick_of_next_bucket():
    engine = MultiTimeframeCandleEngine(timeframes=("1m", "5m"), input_mode="tick")
    engine.on_tick(
        "BTCUSD",
        exchange_ts=BASE + timedelta(seconds=10),
        price=100.0,
        volume=2.0,
        event_id="a",
    )
    engine.on_tick(
        "BTCUSD",
        exchange_ts=BASE + timedelta(seconds=40),
        price=102.0,
        volume=3.0,
        event_id="b",
    )
    before = engine.snapshot("BTCUSD")
    assert before.states["1m"].forming.progress == pytest.approx(2 / 3)
    assert before.states["1m"].forming.high == 102.0

    engine.on_tick(
        "BTCUSD",
        exchange_ts=BASE + timedelta(minutes=1),
        price=101.0,
        volume=1.0,
        event_id="c",
    )
    after = engine.snapshot("BTCUSD")
    assert len(after.states["1m"].completed) == 1
    assert after.states["1m"].completed[0].close == 102.0
    assert after.states["1m"].completed[0].volume == 5.0
    assert after.states["1m"].forming.open == 101.0
    assert after.states["5m"].forming.close == 101.0


def test_tick_snapshot_preserves_exchange_and_local_receive_timestamps():
    engine = MultiTimeframeCandleEngine(timeframes=("1m", "5m"), input_mode="tick")
    exchange_ts = BASE + timedelta(seconds=10)
    local_recv_ts = exchange_ts + timedelta(milliseconds=37.5)

    engine.on_tick(
        "BTCUSD",
        exchange_ts=exchange_ts,
        local_recv_ts=local_recv_ts,
        price=100.0,
        volume=2.0,
        event_id="latency-sample",
    )
    snapshot = engine.snapshot("BTCUSD")
    forming = snapshot.states["1m"].forming

    assert snapshot.last_exchange_ts == exchange_ts
    assert snapshot.last_local_receive_ts == local_recv_ts
    assert snapshot.feed_delay_ms == pytest.approx(37.5)
    assert forming is not None
    assert forming.last_update_ts == exchange_ts
    assert forming.local_received_at == local_recv_ts
    assert forming.feed_delay_ms == pytest.approx(37.5)
    assert snapshot.to_dict()["feed_delay_ms"] == pytest.approx(37.5)
    assert snapshot.to_dict()["clock_skew_suspected"] is False


def test_negative_receive_delta_is_preserved_and_labelled_as_clock_skew():
    engine = MultiTimeframeCandleEngine(timeframes=("1m",), input_mode="tick")
    exchange_ts = BASE + timedelta(seconds=10)
    engine.on_tick(
        "BTCUSD",
        exchange_ts=exchange_ts,
        local_recv_ts=exchange_ts - timedelta(milliseconds=12),
        price=100.0,
    )

    payload = engine.snapshot("BTCUSD").to_dict()
    assert payload["feed_delay_ms"] == pytest.approx(-12.0)
    assert payload["clock_skew_suspected"] is True
    assert payload["timeframes"]["1m"]["forming"]["clock_skew_suspected"] is True


def test_closed_minute_snapshot_records_observation_delay_without_changing_causality():
    engine = MultiTimeframeCandleEngine(timeframes=("1m", "5m"), input_mode="one_minute")
    candle = minute(0)
    observed = candle.ts + timedelta(milliseconds=80)

    engine.on_closed_one_minute("ETHUSD", candle, observed_at=observed)
    snapshot = engine.snapshot("ETHUSD")
    forming = snapshot.states["5m"].forming

    assert snapshot.available_at == observed
    assert snapshot.last_exchange_ts == candle.ts
    assert snapshot.last_local_receive_ts == observed
    assert snapshot.feed_delay_ms == pytest.approx(80.0)
    assert forming is not None
    assert forming.last_update_ts == candle.ts
    assert forming.local_received_at == observed
    assert forming.feed_delay_ms == pytest.approx(80.0)


def test_tick_event_ids_are_exactly_once():
    engine = MultiTimeframeCandleEngine(timeframes=("1m",), input_mode="tick")
    assert engine.on_tick(
        "BTCUSD", exchange_ts=BASE, price=100.0, volume=2.0, event_id="same"
    )
    assert not engine.on_tick(
        "BTCUSD", exchange_ts=BASE, price=100.0, volume=2.0, event_id="same"
    )
    # A delayed duplicate remains harmless even after event time advances.
    engine.on_tick(
        "BTCUSD",
        exchange_ts=BASE + timedelta(seconds=2),
        price=101.0,
        volume=1.0,
        event_id="new",
    )
    assert not engine.on_tick(
        "BTCUSD", exchange_ts=BASE, price=100.0, volume=2.0, event_id="same"
    )
    assert engine.snapshot("BTCUSD").states["1m"].forming.volume == 3.0


def test_future_or_regressing_inputs_are_rejected():
    minute_engine = MultiTimeframeCandleEngine(timeframes=("1m", "5m"))
    with pytest.raises(ValueError, match="before it closes"):
        minute_engine.on_closed_one_minute(
            "BTCUSD", minute(0), observed_at=BASE + timedelta(seconds=30)
        )

    tick_engine = MultiTimeframeCandleEngine(timeframes=("1m",), input_mode="tick")
    tick_engine.on_tick("BTCUSD", exchange_ts=BASE + timedelta(seconds=2), price=100.0)
    with pytest.raises(ValueError, match="regression"):
        tick_engine.on_tick("BTCUSD", exchange_ts=BASE + timedelta(seconds=1), price=100.0)


def test_input_modes_cannot_be_mixed():
    tick_engine = MultiTimeframeCandleEngine(input_mode="tick")
    with pytest.raises(RuntimeError, match="closed 1m input is disabled"):
        tick_engine.on_closed_one_minute("BTCUSD", minute(0), observed_at=minute(0).ts)
    minute_engine = MultiTimeframeCandleEngine(input_mode="one_minute")
    with pytest.raises(RuntimeError, match="tick input is disabled"):
        minute_engine.on_tick("BTCUSD", exchange_ts=BASE, price=100.0)


def test_replay_is_deterministic_for_identical_prefixes():
    left = MultiTimeframeCandleEngine(input_mode="one_minute")
    right = MultiTimeframeCandleEngine(input_mode="one_minute")
    for index in range(17):
        candle = minute(index)
        left.on_closed_one_minute("ETHUSD", candle, observed_at=candle.ts)
        right.on_closed_one_minute("ETHUSD", candle, observed_at=candle.ts)

    assert left.snapshot("ETHUSD").to_dict() == right.snapshot("ETHUSD").to_dict()


def test_snapshot_progress_uses_explicit_as_of_and_never_wall_clock():
    engine = MultiTimeframeCandleEngine(timeframes=("5m",), input_mode="tick")
    engine.on_tick("BTCUSD", exchange_ts=BASE + timedelta(seconds=30), price=100.0)

    at_event = engine.snapshot("BTCUSD")
    later = engine.snapshot("BTCUSD", as_of=BASE + timedelta(minutes=2))
    assert at_event.states["5m"].forming.progress == pytest.approx(0.1)
    assert later.states["5m"].forming.progress == pytest.approx(0.4)
    assert later.states["5m"].forming.last_update_ts == BASE + timedelta(seconds=30)
    assert later.states["5m"].forming.close == 100.0


def test_market_context_keeps_forming_state_separate_from_closed_features():
    store = MultiTimeframeCandleStore()
    closed = Candle(BASE, 99.0, 101.0, 98.0, 100.0, 20.0, "1m")
    store.append_closed("BTCUSD", closed, observed_at=BASE)
    forming = MultiTimeframeCandleEngine(timeframes=("1m", "5m"), input_mode="tick")
    forming.on_tick(
        "BTCUSD",
        exchange_ts=BASE + timedelta(seconds=20),
        price=105.0,
        volume=1.0,
    )

    context = MarketContextBuilder(store, forming_candle_engine=forming).build(
        "BTCUSD", now=BASE + timedelta(seconds=20)
    )

    assert context.candles["1m"] == (closed,)
    assert context.ts == BASE
    assert context.available_at == BASE + timedelta(seconds=20)
    assert context.forming_candles["1m"].close == 105.0
    assert context.forming_candles["1m"].complete is False
    assert context.features.get("close_1m") != 105.0


def test_selected_candidate_journals_exact_forming_snapshot_as_context_only():
    engine = MultiTimeframeCandleEngine(timeframes=("1m", "5m"), input_mode="tick")
    engine.on_tick(
        "BTCUSD",
        exchange_ts=BASE + timedelta(seconds=20),
        local_recv_ts=BASE + timedelta(seconds=20, milliseconds=25),
        price=100.0,
        volume=1.0,
    )
    candidate = SignalCandidate(
        scanner_id="test_scanner",
        symbol="BTCUSD",
        side=Side.LONG,
        decision_ts=BASE + timedelta(seconds=30),
        entry_price=100.0,
        stop_loss=99.0,
        take_profits=(102.0,),
        time_stop_seconds=300,
        expected_hold_seconds=120,
        expected_move_bps=200.0,
        raw_expectancy_bps=30.0,
        modeled_cost_bps=14.8,
        fee_adjusted_expectancy_bps=15.2,
        scalper_probability=0.7,
        confidence=0.8,
    )

    class Journal:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict]] = []

        def append(self, event_type: str, payload: dict) -> None:
            self.events.append((event_type, payload))

    service = object.__new__(DeltaScalperShadowService)
    service.forming_candles = engine
    service.journal = Journal()
    service._journal_candidate_forming_context("BTCUSD", candidate)

    assert len(service.journal.events) == 1
    event_type, payload = service.journal.events[0]
    assert event_type == "delta_scalper_candidate_forming_context"
    assert payload["candidate_key"] == candidate.dedup_key
    assert payload["forming_candles"]["available_at"] == candidate.decision_ts.isoformat()
    assert payload["forming_candles"]["timeframes"]["1m"]["forming"]["progress"] == 0.5
    assert payload["forming_candles_context_only"] is True
    assert payload["used_for_signal"] is False
    assert payload["used_for_execution"] is False
