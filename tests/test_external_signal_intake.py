"""External TradingView/Willy-style signal intake."""

import json

from vnedge.execution.external_signal_intake import (
    ExternalSignalPolicy,
    external_signal_policy,
    ingest_external_signal,
)
from vnedge.execution.signal_arbiter import SignalArbiter


def payload(**overrides):
    base = {
        "event": "trade_opened",
        "ticker": "BTCUSDT",
        "tf": "15",
        "direction": "LONG",
        "stage": 3,
        "score": 72,
        "confluence": "4/5",
        "entry": 66900.0,
        "sl": 65850.0,
        "tp1": 67850.0,
        "tp2": 68250.0,
        "tp3": 68700.0,
    }
    base.update(overrides)
    return base


def test_valid_tradingview_json_becomes_blocked_candidate_until_source_verified():
    decision = ingest_external_signal(json.dumps(payload()))

    assert decision.accepted is True
    assert decision.plan is not None
    assert decision.plan.symbol == "BTC/USDT:USDT"
    assert decision.plan.reward_r and decision.plan.reward_r > 1.0
    assert decision.candidate is not None
    assert decision.candidate.route == "BLOCKED"
    assert decision.candidate.signal.side == "long"
    assert decision.candidate.signal.stop_price == 65850.0
    assert decision.candidate.signal.take_profit_price == 68700.0
    assert decision.candidate.metadata["exit_plan"]["tp1"]["fraction"] == 0.60
    assert decision.candidate.metadata["exit_plan"]["runner"]["fraction"] == 0.10
    assert decision.candidate.metadata["source_verified"] is False
    assert decision.to_dict()["can_trade"] is False

    arb = SignalArbiter().arbitrate([decision.candidate])
    assert arb.selected == ()
    assert arb.rejected[0].reason == "route_blocked"


def test_verified_source_still_goes_through_arbiter_not_gateway_bypass():
    policy = ExternalSignalPolicy(
        source_id="tap_shadow_verified",
        source_verified=True,
        verified_expected_edge_bps=14.0,
        verified_profit_factor=1.9,
        verified_route="MAKER_ONLY",
    )
    decision = ingest_external_signal(payload(), policy=policy)

    assert decision.accepted is True
    assert decision.candidate is not None
    assert decision.candidate.route == "MAKER_ONLY"
    assert decision.candidate.net_edge_bps == 5.0

    arb = SignalArbiter().arbitrate([decision.candidate])
    assert arb.selected[0].strategy_id == "external_tradingview_signal_v1"
    assert "arbiter_selected" in arb.to_signal().reason


def test_short_payload_uses_short_geometry_and_tp3_target():
    decision = ingest_external_signal(payload(
        ticker="ETHUSDT",
        direction="SHORT",
        entry=3500.0,
        sl=3560.0,
        tp1=3450.0,
        tp2=3410.0,
        tp3=3380.0,
    ))

    assert decision.accepted is True
    assert decision.plan is not None
    assert decision.plan.side == "short"
    assert decision.plan.symbol == "ETH/USDT:USDT"
    assert decision.candidate.signal.stop_price == 3560.0
    assert decision.candidate.signal.take_profit_price == 3380.0


def test_rejects_weak_or_unconfirmed_alerts_with_all_failed_checks():
    decision = ingest_external_signal(payload(
        event="pivot_label",
        stage=2,
        score=42,
        confluence=1,
    ))

    assert decision.accepted is False
    assert any("event_not_trade_opened" in c for c in decision.failed_checks)
    assert any("stage_below_floor" in c for c in decision.failed_checks)
    assert any("score_below_floor" in c for c in decision.failed_checks)
    assert any("confluence_below_floor" in c for c in decision.failed_checks)
    assert decision.candidate is None


def test_rejects_price_chase_when_current_price_is_too_far_from_entry():
    decision = ingest_external_signal(payload(), current_price=68100.0)

    assert decision.accepted is False
    assert any("entry_slippage_too_high" in c for c in decision.failed_checks)


def test_external_signal_policy_is_explicitly_non_trading():
    policy = external_signal_policy()

    assert policy["can_trade"] is False
    assert policy["can_promote"] is False
    assert "gateway" in policy["principle"]
