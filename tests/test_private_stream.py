"""Private order/fill stream — venue truth through OrderManager."""

from datetime import UTC, datetime, timedelta

import pytest

from vnedge.config.risk_config import RiskConfig
from vnedge.execution.journal import DecisionJournal
from vnedge.execution.order_manager import OrderManager
from vnedge.execution.order_state import OrderState
from vnedge.execution.private_stream import (
    CcxtPrivateStream,
    PrivateFillUpdate,
    PrivateOrderUpdate,
    PrivateStreamEventApplier,
    normalize_fill_update,
    normalize_order_update,
)
from vnedge.risk.kill_switch import KillSwitch
from vnedge.risk.risk_manager import (
    AccountState,
    MarketState,
    OrderIntent,
    PreTradeRiskGateway,
)

SYM = "BTC/USDT:USDT"


class AckAdapter:
    async def submit_order(self, order):
        return "ex_1"

    async def cancel_order(self, order):
        return "cancelled"


def intent(**overrides) -> OrderIntent:
    defaults = dict(
        symbol=SYM,
        side="long",
        quantity=0.5,
        notional_usd=50.0,
        leverage=1.0,
        strategy_id="test",
    )
    defaults.update(overrides)
    return OrderIntent(**defaults)


def account() -> AccountState:
    return AccountState(
        equity_usd=800.0,
        daily_pnl_usd=0.0,
        peak_equity_usd=800.0,
        open_positions=0,
    )


def market() -> MarketState:
    return MarketState(
        symbol=SYM,
        last_update=datetime.now(UTC) - timedelta(milliseconds=100),
        spread_bps=1.0,
        estimated_slippage_bps=2.0,
        funding_rate=0.0001,
        exchange_healthy=True,
    )


def manager(tmp_path) -> OrderManager:
    gateway = PreTradeRiskGateway(
        RiskConfig(), KillSwitch(kill_file=tmp_path / "KILL")
    )
    return OrderManager(gateway, DecisionJournal(tmp_path / "j.jsonl"), AckAdapter())


async def submitted_order(tmp_path):
    om = manager(tmp_path)
    order = await om.submit(intent(), account(), market(), "k1")
    assert order.state is OrderState.ACKNOWLEDGED
    return om, order


def test_order_update_normalizes_nested_client_id():
    update = normalize_order_update({
        "status": "open",
        "filled": 0.2,
        "info": {"clientOrderId": "vne_1", "orderId": "123", "s": SYM},
    })

    assert update.client_order_id == "vne_1"
    assert update.exchange_order_id == "123"
    assert update.state is OrderState.ACKNOWLEDGED
    assert update.filled_quantity == 0.2


def test_fill_update_normalizes_fee_and_trade_id():
    update = normalize_fill_update({
        "id": "t1",
        "order": "ex_1",
        "amount": 0.1,
        "price": 100.0,
        "fee": {"cost": 0.01, "currency": "USDT"},
    })

    assert update.trade_id == "t1"
    assert update.exchange_order_id == "ex_1"
    assert update.quantity == 0.1
    assert update.fee_cost == 0.01


async def test_private_order_stream_drives_partial_then_filled(tmp_path):
    om, order = await submitted_order(tmp_path)
    applier = PrivateStreamEventApplier(om)

    applier.apply_order(PrivateOrderUpdate(
        client_order_id=order.client_order_id,
        exchange_order_id="ex_1",
        symbol=SYM,
        status="open",
        state=OrderState.ACKNOWLEDGED,
        filled_quantity=0.2,
        raw={},
    ))
    assert order.state is OrderState.PARTIALLY_FILLED
    assert order.filled_quantity == 0.2

    applier.apply_order(PrivateOrderUpdate(
        client_order_id=order.client_order_id,
        exchange_order_id="ex_1",
        symbol=SYM,
        status="closed",
        state=OrderState.FILLED,
        filled_quantity=0.5,
        raw={},
    ))
    assert order.state is OrderState.FILLED
    assert order.filled_quantity == 0.5


async def test_private_fill_stream_is_idempotent_and_can_map_exchange_order(tmp_path):
    om, order = await submitted_order(tmp_path)
    order.exchange_order_id = "ex_1"
    applier = PrivateStreamEventApplier(om)

    fill = PrivateFillUpdate(
        client_order_id=None,
        exchange_order_id="ex_1",
        trade_id="t1",
        symbol=SYM,
        side="buy",
        price=100.0,
        quantity=0.2,
        fee_cost=0.01,
        fee_currency="USDT",
        raw={},
    )
    assert applier.apply_fill(fill)
    assert applier.apply_fill(fill)  # duplicate trade id: no double count
    assert order.state is OrderState.PARTIALLY_FILLED
    assert order.filled_quantity == 0.2
    assert order.fees_paid == 0.01

    assert applier.apply_fill(PrivateFillUpdate(
        client_order_id=None,
        exchange_order_id="ex_1",
        trade_id="t2",
        symbol=SYM,
        side="buy",
        price=100.1,
        quantity=0.3,
        fee_cost=0.02,
        fee_currency="USDT",
        raw={},
    ))
    assert order.state is OrderState.FILLED
    assert order.filled_quantity == 0.5
    assert order.fees_paid == 0.03


async def test_unmapped_fill_can_be_retried_after_order_mapping(tmp_path):
    om, order = await submitted_order(tmp_path)
    applier = PrivateStreamEventApplier(om)
    fill = PrivateFillUpdate(
        client_order_id=None,
        exchange_order_id="ex_late",
        trade_id="late_fill",
        symbol=SYM,
        side="buy",
        price=100.0,
        quantity=0.5,
        fee_cost=0.01,
        fee_currency="USDT",
        raw={},
    )

    assert not applier.apply_fill(fill)
    assert order.filled_quantity == 0.0

    order.exchange_order_id = "ex_late"
    assert applier.apply_fill(fill)
    assert order.state is OrderState.FILLED
    assert order.filled_quantity == 0.5


class FakePrivateClient:
    def __init__(self, client_order_id):
        self.client_order_id = client_order_id
        self.closed = False

    async def watch_orders(self):
        return [{
            "clientOrderId": self.client_order_id,
            "id": "ex_1",
            "symbol": SYM,
            "status": "open",
            "filled": 0.0,
        }]

    async def watch_my_trades(self, symbol=None):
        return [{
            "clientOrderId": self.client_order_id,
            "order": "ex_1",
            "id": "t1",
            "symbol": symbol or SYM,
            "amount": 0.5,
            "price": 100.0,
            "fee": {"cost": 0.01, "currency": "USDT"},
        }]

    async def close(self):
        self.closed = True


async def test_ccxt_private_stream_applies_orders_and_fills(tmp_path):
    om, order = await submitted_order(tmp_path)
    applier = PrivateStreamEventApplier(om)
    client = FakePrivateClient(order.client_order_id)
    stream = CcxtPrivateStream(
        api_key="k",
        api_secret="s",
        live_confirmed=True,
        client=client,
        applier=applier,
    )

    orders = await stream.watch_orders_once()
    assert len(orders) == 1
    assert stream.health.connected
    assert stream.health.orders_seen == 1

    fills = await stream.watch_fills_once(SYM)
    assert len(fills) == 1
    assert order.state is OrderState.FILLED
    assert order.fees_paid == 0.01
    assert stream.health.fills_seen == 1

    await stream.close()
    assert client.closed


def test_ccxt_private_stream_refuses_testnet(tmp_path):
    om = manager(tmp_path)
    applier = PrivateStreamEventApplier(om)
    with pytest.raises(ValueError, match="testnet"):
        CcxtPrivateStream(
            api_key="k",
            api_secret="s",
            testnet=True,
            client=FakePrivateClient("coid"),
            applier=applier,
        )
