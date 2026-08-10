"""Pre-trade risk gateway behavior."""

from datetime import UTC, datetime, timedelta

import pytest

from vnedge.config.risk_config import RiskConfig
from vnedge.risk.kill_switch import KillSwitch
from vnedge.risk.risk_manager import (
    AccountState,
    MarketState,
    OrderIntent,
    PreTradeRiskGateway,
)

NOW = datetime(2026, 7, 2, 12, 0, tzinfo=UTC)


@pytest.fixture
def kill_switch(tmp_path):
    return KillSwitch(kill_file=tmp_path / "KILL")


@pytest.fixture
def gateway(kill_switch):
    return PreTradeRiskGateway(RiskConfig(), kill_switch)


def healthy_market(**overrides) -> MarketState:
    defaults = dict(
        symbol="BTC/USDT:USDT",
        last_update=NOW - timedelta(seconds=1),
        spread_bps=1.0,
        estimated_slippage_bps=2.0,
        funding_rate=0.0001,
        exchange_healthy=True,
    )
    defaults.update(overrides)
    return MarketState(**defaults)


def healthy_account(**overrides) -> AccountState:
    defaults = dict(
        equity_usd=800.0,
        daily_pnl_usd=-5.0,
        peak_equity_usd=850.0,
        open_positions=0,
        exposure_by_symbol_usd={},
        total_exposure_usd=0.0,
    )
    defaults.update(overrides)
    return AccountState(**defaults)


def entry_intent(**overrides) -> OrderIntent:
    defaults = dict(
        symbol="BTC/USDT:USDT",
        side="long",
        quantity=0.001,
        notional_usd=110.0,
        leverage=3.0,
        reduce_only=False,
        strategy_id="test",
    )
    defaults.update(overrides)
    return OrderIntent(**defaults)


def test_clean_entry_is_approved(gateway):
    decision = gateway.evaluate(entry_intent(), healthy_account(), healthy_market(), now=NOW)
    assert decision.approved, decision.explanation


def test_kill_switch_blocks_entries(gateway, kill_switch):
    kill_switch.activate("test trip")
    decision = gateway.evaluate(entry_intent(), healthy_account(), healthy_market(), now=NOW)
    assert not decision.approved
    assert any("kill_switch" in f for f in decision.failed_checks)


def test_kill_switch_never_blocks_reduce_only_exits(gateway, kill_switch):
    """Kill switch = exits only. Flattening must flow while it is tripped."""
    kill_switch.activate("test trip")
    decision = gateway.evaluate(
        entry_intent(reduce_only=True), healthy_account(open_positions=1),
        healthy_market(), now=NOW,
    )
    assert decision.approved, decision.explanation


def test_kill_file_trips_switch(gateway, kill_switch):
    kill_switch.kill_file.touch()
    decision = gateway.evaluate(entry_intent(), healthy_account(), healthy_market(), now=NOW)
    assert not decision.approved
    assert kill_switch.is_active


def test_stale_data_rejected(gateway):
    market = healthy_market(last_update=NOW - timedelta(seconds=30))
    decision = gateway.evaluate(entry_intent(), healthy_account(), market, now=NOW)
    assert not decision.approved
    assert any("data_freshness" in f for f in decision.failed_checks)


def test_stale_data_warns_but_does_not_block_reduce_only_exit(gateway):
    market = healthy_market(last_update=NOW - timedelta(seconds=30))
    decision = gateway.evaluate(
        entry_intent(reduce_only=True),
        healthy_account(open_positions=1),
        market,
        now=NOW,
    )
    assert decision.approved, decision.explanation
    assert not decision.failed_checks
    assert any("data_freshness" in f for f in decision.warning_checks)


def test_exchange_health_warns_but_does_not_block_reduce_only_exit(gateway):
    market = healthy_market(exchange_healthy=False)
    decision = gateway.evaluate(
        entry_intent(reduce_only=True),
        healthy_account(open_positions=1),
        market,
        now=NOW,
    )
    assert decision.approved, decision.explanation
    assert not decision.failed_checks
    assert any("exchange_health" in f for f in decision.warning_checks)


def test_symbol_mismatch_still_blocks_reduce_only_exit(gateway):
    market = healthy_market(symbol="ETH/USDT:USDT")
    decision = gateway.evaluate(
        entry_intent(reduce_only=True),
        healthy_account(open_positions=1),
        market,
        now=NOW,
    )
    assert not decision.approved
    assert any("symbol_match" in f for f in decision.failed_checks)


def test_daily_loss_limit_blocks_new_entries(gateway):
    account = healthy_account(daily_pnl_usd=-25.0)  # limit is $20
    decision = gateway.evaluate(entry_intent(), account, healthy_market(), now=NOW)
    assert not decision.approved
    assert any("daily_loss_limit" in f for f in decision.failed_checks)


def test_daily_loss_limit_does_not_block_exits(gateway):
    """Reduce-only orders must go through even after the loss limit is hit."""
    account = healthy_account(daily_pnl_usd=-25.0, open_positions=1)
    decision = gateway.evaluate(
        entry_intent(reduce_only=True), account, healthy_market(), now=NOW
    )
    assert decision.approved, decision.explanation


def test_slippage_does_not_block_reduce_only_exits(gateway):
    """Bad slippage is an entry-quality problem; exits still need to flow."""
    account = healthy_account(open_positions=1)
    market = healthy_market(estimated_slippage_bps=99.0)
    decision = gateway.evaluate(
        entry_intent(reduce_only=True), account, market, now=NOW
    )
    assert decision.approved, decision.explanation


def test_leverage_cap_enforced(gateway):
    decision = gateway.evaluate(
        entry_intent(leverage=12.0), healthy_account(), healthy_market(), now=NOW
    )
    assert not decision.approved
    assert any("leverage_cap" in f for f in decision.failed_checks)


def test_exposure_limits(gateway):
    account = healthy_account(
        exposure_by_symbol_usd={"BTC/USDT:USDT": 450.0}, total_exposure_usd=450.0
    )
    decision = gateway.evaluate(
        entry_intent(notional_usd=100.0), account, healthy_market(), now=NOW
    )
    assert not decision.approved
    assert any("symbol_exposure" in f for f in decision.failed_checks)


def test_funding_against_position_rejected(gateway):
    market = healthy_market(funding_rate=0.005)  # longs pay 0.5%/interval
    decision = gateway.evaluate(entry_intent(side="long"), healthy_account(), market, now=NOW)
    assert not decision.approved
    assert any("funding" in f for f in decision.failed_checks)


def test_consecutive_loss_streak_blocks_entries(gateway):
    account = healthy_account(consecutive_losses=4)  # limit is 4
    decision = gateway.evaluate(entry_intent(), account, healthy_market(), now=NOW)
    assert not decision.approved
    assert any("consecutive_losses" in f for f in decision.failed_checks)


def test_consecutive_loss_streak_does_not_block_exits(gateway):
    account = healthy_account(consecutive_losses=10, open_positions=1)
    decision = gateway.evaluate(
        entry_intent(reduce_only=True), account, healthy_market(), now=NOW
    )
    assert decision.approved, decision.explanation


def test_funding_in_our_favor_accepted(gateway):
    market = healthy_market(funding_rate=0.005)  # shorts EARN 0.5%
    decision = gateway.evaluate(entry_intent(side="short"), healthy_account(), market, now=NOW)
    assert decision.approved, decision.explanation


def test_all_failures_reported_not_just_first(gateway, kill_switch):
    kill_switch.activate("test")
    market = healthy_market(exchange_healthy=False, spread_bps=50.0)
    decision = gateway.evaluate(entry_intent(), healthy_account(), market, now=NOW)
    assert len(decision.failed_checks) >= 3
