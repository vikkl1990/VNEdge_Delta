"""Event-driven Delta trigger: causality, gating, telemetry, and safety locks."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from vnedge.execution.journal import DecisionJournal
from vnedge.scalping.delta_engine.absorption import (
    AbsorptionDetectorConfig,
    AbsorptionInstrumentConfig,
)
from vnedge.scalping.delta_engine.event_trigger import (
    AbsorptionReversalScanner,
    BookLevel,
    DeltaVerifiedEventBridge,
    EventDrivenTriggerLayer,
    EventTriggerConfig,
    FundingOpenInterestEvent,
    HigherTimeframeContext,
    L2Event,
    SustainedFlowImbalanceScanner,
    TradeEvent,
)
from vnedge.scalping.delta_engine.fee_model import DeltaFeeModel
from vnedge.scalping.delta_engine.signal_generator import SignalGateConfig

NOW = datetime(2026, 8, 9, 6, 0, tzinfo=UTC)
BASE_NS = 10_000_000_000


def config(**overrides) -> EventTriggerConfig:
    return EventTriggerConfig(
        confirmation_ms=400,
        min_eval_interval_ms=50,
        minimum_confirmation_samples=3,
        max_book_age_ms=1_000,
        **overrides,
    )


def l2(
    offset_ms: int,
    *,
    sequence: int = 1,
    snapshot: bool = True,
    bid_size: float = 10.0,
    ask_size: float = 1.0,
    checksum_healthy: bool = True,
) -> L2Event:
    received = NOW + timedelta(milliseconds=offset_ms)
    return L2Event(
        symbol="BTCUSD",
        bids=(BookLevel(100.0, bid_size), BookLevel(99.0, bid_size / 2)),
        asks=(BookLevel(101.0, ask_size), BookLevel(102.0, ask_size / 2)),
        sequence=sequence,
        is_snapshot=snapshot,
        exchange_ts=received - timedelta(milliseconds=20),
        received_at=received,
        received_monotonic_ns=BASE_NS + offset_ms * 1_000_000,
        checksum_healthy=checksum_healthy,
    )


def trade(offset_ms: int, *, received_at: datetime | None = None) -> TradeEvent:
    received = received_at or NOW + timedelta(milliseconds=offset_ms)
    return TradeEvent(
        symbol="BTCUSD",
        price=100.5,
        size=1.0,
        side="buy",
        exchange_ts=received - timedelta(milliseconds=20),
        received_at=received,
        received_monotonic_ns=BASE_NS + offset_ms * 1_000_000,
    )


def layer(*, probability: float = 0.5, journal=None) -> EventDrivenTriggerLayer:
    fee = DeltaFeeModel(default_slippage_bps_per_leg=1.5)
    scanner = SustainedFlowImbalanceScanner(fee, probability_prior=probability)
    return EventDrivenTriggerLayer(
        (scanner,),
        config=config(),
        gates=SignalGateConfig(
            min_expectancy_bps=8.0,
            min_probability=0.70,
            min_confidence=0.60,
            allowed_symbols=("BTCUSD", "ETHUSD"),
        ),
        journal=journal,
    )


class BrokenIndicatorScorer:
    def score_event_candidate(self, context, candidate):
        raise RuntimeError("diagnostic scorer unavailable")


def prime_sustained_flow(engine: EventDrivenTriggerLayer, *, final_at: datetime | None = None):
    assert engine.on_l2(l2(0)) is None
    assert engine.on_trade(trade(100)) is None
    assert engine.on_trade(trade(300)) is None
    return engine.on_trade(trade(500, received_at=final_at))


def test_config_cannot_enable_trading_or_promotion() -> None:
    with pytest.raises(ValueError, match="research-only"):
        EventTriggerConfig(can_trade=True)
    with pytest.raises(ValueError, match="research-only"):
        EventTriggerConfig(can_promote=True)


def test_sustained_confirmation_builds_immutable_event_snapshot() -> None:
    decision = prime_sustained_flow(layer())
    assert decision is not None
    assert decision.context is not None
    assert decision.context.confirmed_direction == 1
    assert decision.context.confirmation_age_ms == pytest.approx(400.0)
    assert decision.context.book_imbalance > 0.7
    assert decision.context.flow_imbalance == pytest.approx(1.0)
    assert decision.context.available_at <= decision.decision_ts
    assert decision.feed_delay_us == 20_000
    assert decision.evaluated[0].side.value == "long"
    assert decision.selected is None
    assert decision.rejection_reasons == (
        "event_sustained_flow_imbalance_v1:fee_adjusted_expectancy_below_gate",
        "event_sustained_flow_imbalance_v1:probability_below_gate",
    )
    assert decision.research_only and not decision.can_trade and not decision.can_promote
    assert decision.to_dict()["order_route"] == "absent"
    score = decision.evaluated[0].metadata["indicator_family_score"]
    assert score["policy_version"] == "indicator_family_v1.0.0"
    assert score["research_only"] is True
    assert score["can_trade"] is False
    assert score["used_for_signal"] is False


def test_calibrated_plugin_still_uses_shared_gates_and_journal(tmp_path: Path) -> None:
    journal = DecisionJournal(tmp_path / "event-decisions.jsonl")
    decision = prime_sustained_flow(layer(probability=0.9, journal=journal))
    assert decision is not None and decision.selected is not None
    assert decision.journal_write_success is True
    rows = journal.read_all()
    assert len(rows) == 1
    assert rows[0]["kind"] == "delta_event_research_decision"
    payload = rows[0]["payload"]
    assert payload["selected"]["scanner_id"] == "event_sustained_flow_imbalance_v1"
    assert payload["can_trade"] is False
    assert payload["order_route"] == "absent"


def test_advisory_score_failure_does_not_change_candidate_gates() -> None:
    fee = DeltaFeeModel(default_slippage_bps_per_leg=1.5)
    engine = EventDrivenTriggerLayer(
        (SustainedFlowImbalanceScanner(fee, probability_prior=0.9),),
        config=config(),
        gates=SignalGateConfig(
            min_expectancy_bps=8.0,
            min_probability=0.70,
            min_confidence=0.60,
            allowed_symbols=("BTCUSD", "ETHUSD"),
        ),
        indicator_scorer=BrokenIndicatorScorer(),  # type: ignore[arg-type]
    )
    decision = prime_sustained_flow(engine)
    assert decision is not None and decision.selected is not None
    assert decision.selected.metadata["indicator_family_score_error"] == "RuntimeError"
    assert engine.telemetry()["counts"]["indicator_scoring_errors"] == 1


def test_sequence_gap_invalidates_book_and_requires_new_snapshot() -> None:
    engine = layer(probability=0.9)
    assert engine.on_l2(l2(0)) is None
    gap = l2(20, sequence=3, snapshot=False)
    assert engine.on_l2(gap) is None
    assert engine.on_trade(trade(500)) is None
    telemetry = engine.telemetry()
    assert telemetry["counts"]["invalid_book"] >= 1
    assert telemetry["counts"]["selected"] == 0


def test_stale_book_blocks_evaluation() -> None:
    engine = layer(probability=0.9)
    assert engine.on_l2(l2(0)) is None
    assert engine.on_trade(trade(100)) is None
    assert engine.on_trade(trade(300)) is None
    assert engine.on_trade(trade(1_500)) is None
    assert engine.telemetry()["counts"]["invalid_book"] >= 1


def test_rate_limit_and_exactly_once_candidate_dedup() -> None:
    engine = layer(probability=0.9)
    first = prime_sustained_flow(engine)
    assert first is not None and first.selected is not None
    same_wall_time = NOW + timedelta(milliseconds=500)
    assert engine.on_trade(trade(520, received_at=same_wall_time)) is None
    duplicate = engine.on_trade(trade(560, received_at=same_wall_time))
    assert duplicate is not None
    assert duplicate.selected is None and duplicate.duplicate
    assert engine.telemetry()["counts"]["rate_limited"] >= 1


def test_candidate_cooldown_prevents_event_spam() -> None:
    engine = layer(probability=0.9)
    first = prime_sustained_flow(engine)
    assert first is not None and first.selected is not None
    later = engine.on_trade(trade(1_000))
    assert later is not None and later.selected is None
    assert later.rejection_reasons == (
        "event_sustained_flow_imbalance_v1:candidate_cooldown",
    )
    assert engine.telemetry()["counts"]["cooldown_blocked"] == 1


def test_future_higher_timeframe_context_fails_closed() -> None:
    engine = layer(probability=0.9)
    engine.update_higher_timeframe_context(
        HigherTimeframeContext(
            "BTCUSD",
            available_at=NOW + timedelta(seconds=10),
            bias=1,
            regime="trending_up",
        )
    )
    decision = prime_sustained_flow(engine)
    assert decision is not None
    assert decision.selected is None and decision.context is None
    assert decision.rejection_reasons == ("context_error:ValueError",)


def test_funding_oi_state_is_point_in_time_metadata_not_a_trigger() -> None:
    engine = layer()
    assert engine.on_l2(l2(0)) is None
    event = FundingOpenInterestEvent(
        symbol="BTCUSD",
        funding_rate=0.0001,
        open_interest=1_000.0,
        exchange_ts=NOW,
        received_at=NOW + timedelta(milliseconds=10),
        received_monotonic_ns=BASE_NS + 10_000_000,
    )
    assert engine.on_funding_or_oi(event) is None
    assert engine.telemetry()["counts"]["evaluations"] == 0


def test_telemetry_exposes_bounded_latency_percentiles() -> None:
    engine = layer()
    decision = prime_sustained_flow(engine)
    assert decision is not None
    telemetry = engine.telemetry()
    assert telemetry["feed_delay"] == {
        "count": 1,
        "p50_us": 20_000,
        "p95_us": 20_000,
        "p99_us": 20_000,
    }
    assert telemetry["receive_to_decision"]["count"] == 1
    assert telemetry["research_only"] is True
    assert telemetry["order_route"] == "absent"
    assert telemetry["funnel"]["events"] == 4
    assert telemetry["rejection_reasons"][
        "event_sustained_flow_imbalance_v1:probability_below_gate"
    ] == 1
    market = telemetry["market_states"]["BTCUSD"]
    assert market["price"] == pytest.approx(100.5)
    assert market["best_bid"] == pytest.approx(100.0)
    assert market["best_ask"] == pytest.approx(101.0)
    assert market["htf"]["available"] is False


def test_verified_recorder_bridge_parses_book_and_aggressor_trade() -> None:
    engine = layer()
    bridge = DeltaVerifiedEventBridge(engine)
    snapshot_message = {
        "type": "ob_updates",
        "action": "snapshot",
        "sy": "BTCUSD",
        "seq": 1,
        "a": [["101", "1"]],
        "b": [["100", "10"]],
    }
    envelope = {
        "record_kind": "exchange",
        "channel": "ob_updates",
        "symbol": "BTCUSD",
        "exchange_timestamp_us": int(NOW.timestamp() * 1_000_000),
        "local_recv_ns": int((NOW + timedelta(milliseconds=20)).timestamp() * 1e9),
        "local_monotonic_ns": BASE_NS,
        "raw_text": json.dumps(snapshot_message),
    }
    assert bridge.consume(envelope, integrity_verified=True) is None

    trade_message = {
        "type": "trades",
        "sy": "BTCUSD",
        "p": "100.5",
        "s": "2",
        "r": "t",
        "t": int(NOW.timestamp() * 1_000_000),
        "ts": int(NOW.timestamp() * 1_000_000),
    }
    trade_envelope = {
        **envelope,
        "channel": "trades",
        "local_monotonic_ns": BASE_NS + 100_000_000,
        "raw_text": json.dumps(trade_message),
    }
    assert bridge.consume(trade_envelope, integrity_verified=True) is None


def test_verified_recorder_bridge_rejects_unverified_tape() -> None:
    bridge = DeltaVerifiedEventBridge(layer())
    with pytest.raises(ValueError, match="unverified"):
        bridge.consume({}, integrity_verified=False)


def test_absorption_is_a_separate_reversal_confirmation_source() -> None:
    fee = DeltaFeeModel(default_slippage_bps_per_leg=1.5)
    absorption = AbsorptionDetectorConfig(
        instruments=(
            AbsorptionInstrumentConfig(
                symbol="BTCUSD",
                tick_size=0.5,
                minimum_aggressive_notional_usd=500.0,
            ),
        ),
        relative_trade_multiple=3.0,
    )
    engine = EventDrivenTriggerLayer(
        (AbsorptionReversalScanner(fee), SustainedFlowImbalanceScanner(fee)),
        config=config(absorption=absorption),
        gates=SignalGateConfig(
            min_expectancy_bps=8.0,
            min_probability=0.70,
            min_confidence=0.60,
            allowed_symbols=("BTCUSD", "ETHUSD"),
        ),
    )
    snapshot = L2Event(
        symbol="BTCUSD",
        bids=(BookLevel(100.0, 10.0),),
        asks=(BookLevel(101.0, 20.0),),
        sequence=1,
        is_snapshot=True,
        exchange_ts=NOW,
        received_at=NOW,
        received_monotonic_ns=BASE_NS,
    )
    assert engine.on_l2(snapshot) is None
    for offset in (100, 150, 200):
        event = TradeEvent(
            symbol="BTCUSD",
            price=101.0,
            size=2.0,
            side="buy",
            exchange_ts=NOW + timedelta(milliseconds=offset - 20),
            received_at=NOW + timedelta(milliseconds=offset),
            received_monotonic_ns=BASE_NS + offset * 1_000_000,
        )
        assert engine.on_trade(event) is None
    book_refresh = L2Event(
        symbol="BTCUSD",
        bids=(),
        asks=(BookLevel(101.0, 18.0),),
        sequence=2,
        is_snapshot=False,
        exchange_ts=NOW + timedelta(milliseconds=380),
        received_at=NOW + timedelta(milliseconds=400),
        received_monotonic_ns=BASE_NS + 400_000_000,
    )
    decision = engine.on_l2(book_refresh)
    assert decision is not None and decision.context is not None
    assert decision.context.confirmation_source == "absorption"
    assert decision.context.confirmed_direction == -1
    assert decision.context.absorption is not None
    assert decision.context.absorption_score > 0.7
    assert len(decision.evaluated) == 1
    assert decision.evaluated[0].scanner_id == "event_absorption_reversal_v1"
    assert decision.evaluated[0].side.value == "short"
    assert decision.selected is None
    assert decision.rejection_reasons == (
        "event_absorption_reversal_v1:fee_adjusted_expectancy_below_gate",
        "event_absorption_reversal_v1:probability_below_gate",
    )
    assert engine.telemetry()["counts"]["absorption_observations"] == 1
    dashboard = engine.absorption_dashboard(
        "BTCUSD",
        now_ns=BASE_NS + 450_000_000,
    )
    assert dashboard["status"] == "active"
    assert dashboard["latest"]["absorbed_side"] == "sell_limits_absorbing_buys"
    assert dashboard["timeline"]
    assert dashboard["footprint"]
    assert dashboard["can_trade"] is False
