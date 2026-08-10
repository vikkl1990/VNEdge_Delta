"""Full pipeline: OrderManager + PaperBroker + SimulatedExchange + reconciliation.

This is the milestone 6 integration surface: both timeout flavors resolved
correctly, unresolved-order blocking, kill-switch-then-flatten, and clean
reconciliation reports.
"""

from datetime import UTC, datetime, timedelta

import pytest

from vnedge.config.risk_config import RiskConfig
from vnedge.execution.journal import DecisionJournal
from vnedge.execution.order_manager import FlattenTarget, OrderManager
from vnedge.execution.order_state import OrderState as S
from vnedge.paper.fill_model import FillModel
from vnedge.paper.paper_broker import PaperBroker
from vnedge.paper.paper_reconciliation import PaperReconciler
from vnedge.paper.simulated_exchange import SimulatedExchange
from vnedge.risk.kill_switch import KillSwitch
from vnedge.risk.risk_manager import AccountState, MarketState, OrderIntent, PreTradeRiskGateway

SYM = "BTC/USDT:USDT"


@pytest.fixture
def world(tmp_path):
    """(order_manager, exchange, kill_switch, journal) wired together."""
    exchange = SimulatedExchange(FillModel(), starting_balance_usd=1_000.0)
    exchange.set_quote(SYM, bid=99.9, ask=100.1)
    journal = DecisionJournal(tmp_path / "journal.jsonl")
    kill = KillSwitch(kill_file=tmp_path / "KILL")
    gateway = PreTradeRiskGateway(RiskConfig(), kill)
    om = OrderManager(gateway, journal, PaperBroker(exchange))
    return om, exchange, kill, journal


def intent(**overrides) -> OrderIntent:
    defaults = dict(
        symbol=SYM, side="long", quantity=0.5, notional_usd=50.0,
        leverage=1.0, reduce_only=False, strategy_id="test",
    )
    defaults.update(overrides)
    return OrderIntent(**defaults)


def account(**overrides) -> AccountState:
    defaults = dict(
        equity_usd=800.0, daily_pnl_usd=0.0, peak_equity_usd=800.0,
        open_positions=0, exposure_by_symbol_usd={}, total_exposure_usd=0.0,
    )
    defaults.update(overrides)
    return AccountState(**defaults)


def market() -> MarketState:
    return MarketState(
        symbol=SYM, last_update=datetime.now(UTC) - timedelta(seconds=1),
        spread_bps=1.0, estimated_slippage_bps=2.0,
        funding_rate=0.0001, exchange_healthy=True,
    )


def key(i: int) -> str:
    return f"test|{SYM}|k{i}"


async def test_order_reaches_venue_and_fills(world):
    om, exchange, _, _ = world
    order = await om.submit(intent(), account(), market(), key(0))
    assert order.state is S.ACKNOWLEDGED
    status = exchange.get_order_status(order.client_order_id)
    assert status.state == "filled"
    assert exchange.get_positions()[0].quantity == pytest.approx(0.5)
    assert exchange.get_fills()[0].fee_usd > 0  # fees always charged


async def test_time_in_force_is_ignored_harmlessly_by_paper_venue(world):
    """time_in_force is live-adapter prep: the paper broker maps intent
    fields explicitly and must fill exactly as if it were unset."""
    om, exchange, _, _ = world
    order = await om.submit(
        intent(time_in_force="PO"), account(), market(), key(0)
    )
    assert order.state is S.ACKNOWLEDGED
    assert exchange.get_order_status(order.client_order_id).state == "filled"


async def test_timeout_reached_recovers_to_filled(tmp_path, world):
    om, exchange, _, _ = world
    om._adapter = PaperBroker(exchange, script=["timeout_reached"])

    stuck = await om.submit(intent(), account(), market(), key(0))
    assert stuck.state is S.TIMEOUT_UNKNOWN
    # the venue REALLY has the order and the position — the dangerous case
    assert exchange.get_order_status(stuck.client_order_id).state == "filled"

    blocked = await om.submit(intent(), account(), market(), key(1))
    assert blocked.state is S.RISK_REJECTED  # no new risk while unknown

    report = PaperReconciler(om, exchange).run()
    assert stuck.client_order_id in report.resolved_orders
    assert stuck.state is S.FILLED
    assert report.clean

    unblocked = await om.submit(intent(), account(), market(), key(2))
    assert unblocked.state is S.ACKNOWLEDGED


async def test_timeout_lost_resolves_to_rejected(world):
    om, exchange, _, _ = world
    om._adapter = PaperBroker(exchange, script=["timeout_lost"])

    stuck = await om.submit(intent(), account(), market(), key(0))
    assert stuck.state is S.TIMEOUT_UNKNOWN
    assert exchange.get_order_status(stuck.client_order_id) is None  # never arrived

    report = PaperReconciler(om, exchange).run()
    assert stuck.state is S.REJECTED
    assert "never arrived" in stuck.history[-1].note
    assert report.clean
    assert exchange.get_positions() == []  # and no phantom position


async def test_scripted_venue_rejection(world):
    om, exchange, _, _ = world
    om._adapter = PaperBroker(exchange, script=["reject:margin check failed"])
    order = await om.submit(intent(), account(), market(), key(0))
    assert order.state is S.REJECTED
    assert "margin check failed" in order.history[-1].note


async def test_kill_switch_then_emergency_flatten(world):
    om, exchange, kill, journal = world
    # open two positions
    await om.submit(intent(), account(), market(), key(0))
    await om.submit(
        intent(side="short", symbol=SYM, quantity=0.2, notional_usd=20.0),
        account(open_positions=1, exposure_by_symbol_usd={SYM: 50.0},
                total_exposure_usd=50.0),
        market(), key(1),
    )
    # net position after both: 0.3 long
    net = exchange.get_positions()[0]
    assert net.quantity == pytest.approx(0.3)

    kill.activate("test emergency")
    # entries are now blocked by the gateway...
    blocked = await om.submit(intent(), account(), market(), key(2))
    assert blocked.state is S.RISK_REJECTED

    # ...but flatten flows through the SAME pipeline, reduce-only
    targets = [FlattenTarget(p.symbol, p.side, abs(p.quantity))
               for p in exchange.get_positions()]
    results = await om.emergency_flatten(
        targets, account(open_positions=1), {SYM: market()}, flatten_id="f1"
    )
    assert all(o.state is S.ACKNOWLEDGED for o in results)
    assert all(o.intent.reduce_only for o in results)
    assert exchange.get_positions() == []  # flat

    kinds = [r["kind"] for r in journal.read_all()]
    assert "emergency_flatten_started" in kinds
    assert "emergency_flatten_finished" in kinds


async def test_emergency_flatten_is_idempotent(world):
    om, exchange, kill, _ = world
    await om.submit(intent(), account(), market(), key(0))
    kill.activate("test")
    targets = [FlattenTarget(p.symbol, p.side, abs(p.quantity))
               for p in exchange.get_positions()]
    await om.emergency_flatten(targets, account(open_positions=1), {SYM: market()}, "f1")
    # same flatten_id again: duplicate intents dropped, nothing double-closed
    results = await om.emergency_flatten(targets, account(open_positions=1), {SYM: market()}, "f1")
    assert all(o.state is S.RISK_REJECTED for o in results)
    assert all("duplicate" in o.history[-1].note for o in results)


async def test_reconciliation_flags_state_divergence(world):
    om, exchange, _, _ = world
    order = await om.submit(intent(), account(), market(), key(0))
    # sabotage venue truth: pretend the venue thinks it was cancelled
    exchange.get_order_status(order.client_order_id).state = "cancelled"
    # internal ACKNOWLEDGED vs venue "cancelled" is tolerated (ack precedes
    # fill knowledge) — but internal FILLED vs venue cancelled is a mismatch
    order.transition(S.FILLED, "test forces divergence")
    report = PaperReconciler(om, exchange).run()
    assert not report.clean
    assert any("filled" in m and "cancelled" in m for m in report.mismatches)
