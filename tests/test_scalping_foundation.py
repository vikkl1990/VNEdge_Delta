"""Scalper foundation: microstate, incremental features, stops, hot risk gates."""

from datetime import UTC, datetime, timedelta

import pytest

from vnedge.config.risk_config import RiskConfig
from vnedge.risk.kill_switch import KillSwitch
from vnedge.risk.risk_manager import AccountState, OrderIntent, PreTradeRiskGateway
from vnedge.scalping.features import IncrementalFeatureEngine
from vnedge.scalping.microstructure import MarketMicroState, PrivateStreamState, TopOfBook, TradeTick
from vnedge.scalping.risk import ScalperRiskConfig, ScalperRiskGateway, ScalperRiskLimits
from vnedge.scalping.strategy import QuoteIntent
from vnedge.scalping.tick_stop import StopRegistration, TickStopEngine

NOW = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
SYM = "BTC/USDT:USDT"


def top(**overrides) -> TopOfBook:
    defaults = dict(
        symbol=SYM,
        bid=99.99,
        bid_size=40.0,
        ask=100.01,
        ask_size=20.0,
        event_time=NOW - timedelta(milliseconds=100),
    )
    defaults.update(overrides)
    return TopOfBook(**defaults)


def private(**overrides) -> PrivateStreamState:
    defaults = dict(
        last_event_at=NOW - timedelta(milliseconds=200),
        connected=True,
        open_order_count=0,
        position_qty_by_symbol={},
    )
    defaults.update(overrides)
    return PrivateStreamState(**defaults)


def micro(**overrides) -> MarketMicroState:
    defaults = dict(
        top=top(),
        private=private(),
        funding_rate=0.0001,
        estimated_slippage_bps=1.0,
    )
    defaults.update(overrides)
    return MarketMicroState(**defaults)


def account() -> AccountState:
    return AccountState(
        equity_usd=800.0,
        daily_pnl_usd=0.0,
        peak_equity_usd=800.0,
        open_positions=0,
        exposure_by_symbol_usd={},
        total_exposure_usd=0.0,
    )


def intent(**overrides) -> OrderIntent:
    defaults = dict(
        symbol=SYM,
        side="long",
        quantity=0.5,
        notional_usd=50.0,
        leverage=1.0,
        reduce_only=False,
        strategy_id="scalper_test",
        order_type="limit",
        limit_price=99.99,
    )
    defaults.update(overrides)
    return OrderIntent(**defaults)


def scalper_gateway(tmp_path, cfg: ScalperRiskConfig | None = None,
                    limits: ScalperRiskLimits | None = None) -> ScalperRiskGateway:
    base = PreTradeRiskGateway(
        RiskConfig(max_spread_bps=10.0, max_slippage_bps=10.0),
        KillSwitch(kill_file=tmp_path / "KILL"),
    )
    return ScalperRiskGateway(base, cfg or ScalperRiskConfig(), limits)


def test_top_of_book_microprice_and_validation():
    book = top()
    assert book.mid_price == pytest.approx(100.0)
    assert book.spread_bps == pytest.approx(2.0)
    assert book.book_imbalance == pytest.approx(1 / 3)
    assert book.microprice > book.mid_price  # more bid size pulls fair value upward
    with pytest.raises(ValueError, match="crossed"):
        top(bid=101.0, ask=100.0)


def test_incremental_features_from_book_and_trades():
    engine = IncrementalFeatureEngine(max_midpoints=4, max_trades=4)
    f0 = engine.on_book(top(bid=99.0, ask=101.0, bid_size=1.0, ask_size=1.0))
    assert f0.trade_count == 0
    engine.on_trade(TradeTick(SYM, 100.5, 2.0, "buy", NOW))
    engine.on_trade(TradeTick(SYM, 99.5, 1.0, "sell", NOW))
    engine.on_book(top(bid=100.0, ask=100.2, bid_size=3.0, ask_size=1.0))
    snap = engine.snapshot(NOW)
    assert snap.trade_count == 2
    assert snap.taker_buy_ratio == pytest.approx(2 / 3)
    assert snap.signed_trade_notional_usd > 0
    assert snap.realized_vol_bps >= 0
    assert snap.book_imbalance == pytest.approx(0.5)


def test_scalper_risk_wraps_base_gateway_and_approves_clean_entry(tmp_path):
    gw = scalper_gateway(tmp_path)
    decision = gw.evaluate(intent(), account(), micro(), expected_edge_bps=12.0, now=NOW)
    assert decision.approved, decision.explanation
    assert decision.base_decision.approved
    assert "scalper_edge_after_cost" in decision.passed_checks


def test_scalper_risk_rejects_stale_private_stream_for_entries(tmp_path):
    gw = scalper_gateway(tmp_path)
    stale = micro(private=private(last_event_at=NOW - timedelta(seconds=4)))
    decision = gw.evaluate(intent(), account(), stale, expected_edge_bps=20.0, now=NOW)
    assert not decision.approved
    assert any("scalper_private_stream" in f for f in decision.failed_checks)


def test_scalper_risk_rejects_edge_that_does_not_clear_costs(tmp_path):
    gw = scalper_gateway(tmp_path)
    decision = gw.evaluate(intent(), account(), micro(), expected_edge_bps=3.0, now=NOW)
    assert not decision.approved
    assert any("scalper_edge_after_cost" in f for f in decision.failed_checks)


def test_scalper_risk_rate_budget_blocks_new_entries(tmp_path):
    limits = ScalperRiskLimits(max_orders_per_minute=2, max_cancels_per_minute=10)
    limits.record_order(NOW - timedelta(seconds=5))
    limits.record_order(NOW - timedelta(seconds=1))
    gw = scalper_gateway(
        tmp_path,
        ScalperRiskConfig(max_orders_per_minute=2, max_cancels_per_minute=10),
        limits,
    )
    decision = gw.evaluate(intent(), account(), micro(), expected_edge_bps=20.0, now=NOW)
    assert not decision.approved
    assert any("scalper_order_rate" in f for f in decision.failed_checks)


def test_scalper_risk_cancel_budget_blocks_new_entries(tmp_path):
    limits = ScalperRiskLimits(max_orders_per_minute=10, max_cancels_per_minute=1)
    limits.record_cancel(NOW - timedelta(seconds=1))
    gw = scalper_gateway(
        tmp_path,
        ScalperRiskConfig(max_orders_per_minute=10, max_cancels_per_minute=1),
        limits,
    )
    decision = gw.evaluate(intent(), account(), micro(), expected_edge_bps=20.0, now=NOW)
    assert not decision.approved
    assert any("scalper_cancel_rate" in f for f in decision.failed_checks)


def test_reduce_only_exit_skips_private_stream_edge_and_rate_checks(tmp_path):
    limits = ScalperRiskLimits(max_orders_per_minute=1, max_cancels_per_minute=1)
    limits.record_order(NOW)
    gw = scalper_gateway(
        tmp_path,
        ScalperRiskConfig(max_orders_per_minute=1, max_cancels_per_minute=1),
        limits,
    )
    exit_intent = intent(side="short", reduce_only=True, notional_usd=0.0)
    stale_private = micro(private=private(last_event_at=NOW - timedelta(seconds=10)))
    decision = gw.evaluate(exit_intent, account(), stale_private, expected_edge_bps=0.0, now=NOW)
    assert decision.approved, decision.explanation


def test_tick_stop_engine_generates_one_reduce_only_exit():
    stops = TickStopEngine()
    stops.register(
        StopRegistration("pos1", SYM, "long", quantity=0.25, stop_price=99.0)
    )
    assert stops.evaluate(top(bid=99.5, ask=99.6)) == ()
    triggered = stops.evaluate(top(bid=98.9, ask=99.0))
    assert len(triggered) == 1
    assert triggered[0].side == "short"
    assert triggered[0].reduce_only
    assert stops.evaluate(top(bid=98.0, ask=98.1)) == ()  # no duplicate exit


def test_quote_intent_requires_positive_ttl():
    with pytest.raises(ValueError, match="ttl"):
        QuoteIntent(intent(), expected_edge_bps=12.0, ttl_ms=0)
