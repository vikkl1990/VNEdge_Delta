"""Live execution adapter — idempotency, timeout verification, error mapping."""

import pytest
from ccxt.base.errors import (
    DuplicateOrderId,
    InsufficientFunds,
    NetworkError,
    OrderNotFound,
)

from vnedge.exchange.live_execution import CcxtExecutionAdapter
from vnedge.execution.order_manager import AdapterRejection, AdapterTimeout
from vnedge.execution.order_state import ManagedOrder
from vnedge.risk.risk_manager import OrderIntent


def order() -> ManagedOrder:
    return ManagedOrder(
        intent_key="k", client_order_id="vne_test123",
        intent=OrderIntent("BTC/USDT:USDT", "long", 0.01, 1000.0, 2.0),
    )


class FakeCcxt:
    """Scripted venue: list of behaviors consumed per create_order call."""

    def __init__(self, script, fetch_result=None):
        self.script = list(script)
        self.fetch_result = fetch_result
        self.create_calls = []
        self.create_params = []
        self.fetch_calls = 0

    async def create_order(self, symbol, type_, side, amount, price, params):
        self.create_calls.append(params["newClientOrderId"])
        self.create_params.append(dict(params))
        behavior = self.script.pop(0)
        if isinstance(behavior, Exception):
            raise behavior
        return {"id": behavior}

    async def fetch_order(self, id_, symbol, params):
        self.fetch_calls += 1
        if self.fetch_result is None:
            raise OrderNotFound("not found")
        return self.fetch_result

    async def close(self):
        pass


def adapter(client, **kw) -> CcxtExecutionAdapter:
    return CcxtExecutionAdapter(
        api_key="k", api_secret="s", live_confirmed=True, client=client, **kw
    )


def test_mainnet_requires_explicit_confirmation():
    with pytest.raises(ValueError, match="live_confirmed"):
        CcxtExecutionAdapter(api_key="k", api_secret="s", testnet=False,
                             client=FakeCcxt([]))


def test_missing_keys_refused():
    with pytest.raises(ValueError, match="credentials"):
        CcxtExecutionAdapter(api_key="", api_secret="", client=FakeCcxt([]))


def test_delta_india_requires_native_execution_adapter():
    with pytest.raises(ValueError, match="DeltaRestExecutionAdapter"):
        CcxtExecutionAdapter(
            "delta_india", api_key="k", api_secret="s", live_confirmed=True,
            client=FakeCcxt([]),
        )


def test_testnet_execution_is_disabled():
    with pytest.raises(ValueError, match="testnet"):
        CcxtExecutionAdapter(api_key="k", api_secret="s", testnet=True,
                             client=FakeCcxt([]))


async def test_happy_path_uses_journaled_client_id():
    fake = FakeCcxt(["ex_1"])
    result = await adapter(fake).submit_order(order())
    assert result == "ex_1"
    assert fake.create_calls == ["vne_test123"]  # OUR id, never regenerated


async def test_insufficient_funds_maps_to_rejection():
    fake = FakeCcxt([InsufficientFunds("margin")])
    with pytest.raises(AdapterRejection, match="InsufficientFunds"):
        await adapter(fake).submit_order(order())


async def test_duplicate_id_resolves_to_existing_order():
    fake = FakeCcxt([DuplicateOrderId("dup")], fetch_result={"id": "ex_prior"})
    assert await adapter(fake).submit_order(order()) == "ex_prior"


async def test_timeout_then_found_at_venue():
    """The dangerous case: order landed, ack lost. Verification finds it —
    no resubmit, no duplicate position."""
    fake = FakeCcxt([NetworkError("timeout")], fetch_result={"id": "ex_landed"})
    assert await adapter(fake).submit_order(order()) == "ex_landed"
    assert len(fake.create_calls) == 1  # never resubmitted


async def test_timeout_not_found_resubmits_same_id():
    fake = FakeCcxt([NetworkError("timeout"), "ex_2"])  # lost, then succeeds
    assert await adapter(fake).submit_order(order()) == "ex_2"
    assert fake.create_calls == ["vne_test123", "vne_test123"]  # SAME id twice


async def test_exhausted_ambiguity_is_timeout_unknown():
    fake = FakeCcxt([NetworkError("t1"), NetworkError("t2")])
    with pytest.raises(AdapterTimeout, match="ambiguous"):
        await adapter(fake).submit_order(order())


# --- time-in-force pass-through (live-phase prep; nothing sets it yet) ------

def tif_order(tif: str | None) -> ManagedOrder:
    return ManagedOrder(
        intent_key="k", client_order_id="vne_test123",
        intent=OrderIntent("BTC/USDT:USDT", "long", 0.01, 1000.0, 2.0,
                           time_in_force=tif),
    )


async def test_time_in_force_passes_through_unified_params():
    fake = FakeCcxt(["ex_1"])
    assert await adapter(fake).submit_order(tif_order("IOC")) == "ex_1"
    assert fake.create_params[0]["timeInForce"] == "IOC"
    assert "postOnly" not in fake.create_params[0]


async def test_post_only_maps_to_unified_postonly_flag():
    fake = FakeCcxt(["ex_1"])
    assert await adapter(fake).submit_order(tif_order("PO")) == "ex_1"
    assert fake.create_params[0]["postOnly"] is True
    assert "timeInForce" not in fake.create_params[0]


async def test_default_tif_none_means_absent_from_params():
    fake = FakeCcxt(["ex_1"])
    assert await adapter(fake).submit_order(order()) == "ex_1"
    assert "timeInForce" not in fake.create_params[0]
    assert "postOnly" not in fake.create_params[0]


def test_invalid_time_in_force_rejected_at_intent_construction():
    with pytest.raises(ValueError, match="time_in_force"):
        OrderIntent("BTC/USDT:USDT", "long", 0.01, 1000.0, 2.0,
                    time_in_force="DAY")
