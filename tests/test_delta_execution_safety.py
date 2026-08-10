from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import pytest

from vnedge.exchange.delta_execution import DeltaRestExecutionAdapter
from vnedge.exchange.delta_execution_safety import (
    DeltaAuthenticatedSafetyClient,
    DeltaDeadmanConfig,
    DeltaExecutionSafetyWrapper,
    DeltaSafetyError,
    DeltaSafetyTransportError,
)
from vnedge.execution.order_manager import AdapterRejection
from vnedge.execution.order_state import ManagedOrder
from vnedge.risk.risk_manager import OrderIntent, ServerSideBracket


class Journal:
    def __init__(self) -> None:
        self.available = True
        self.rows: list[tuple[str, dict]] = []

    def append(self, kind: str, payload: dict) -> bool:
        if not self.available:
            return False
        self.rows.append((kind, payload))
        return True


class Clock:
    def __init__(self) -> None:
        self.seconds = 100.0
        self.now = datetime(2026, 8, 8, tzinfo=UTC)

    def monotonic(self) -> float:
        return self.seconds

    def wall(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.seconds += seconds
        self.now += timedelta(seconds=seconds)


class FakeAdapter:
    def __init__(self) -> None:
        self.created = 0
        self.acks: list[tuple[str, int]] = []
        self.protected: list[tuple[ManagedOrder, ServerSideBracket]] = []
        self.plain: list[ManagedOrder] = []
        self.fail_ack = False
        self.truth = {
            "open_orders": [
                {"id": 1, "reduce_only": False},
                {"id": 2, "reduce_only": True},
            ],
            "positions": [
                {"product_id": 27, "size": 2},
                {"product_id": 3136, "size": 0},
            ],
            "wallet_balances": [{"asset_symbol": "USD", "available_balance": "100"}],
        }

    async def create_deadman(self, config: DeltaDeadmanConfig) -> dict:
        self.created += 1
        return {"heartbeat_id": config.heartbeat_id}

    async def acknowledge_deadman(self, heartbeat_id: str, ttl_ms: int) -> dict:
        self.acks.append((heartbeat_id, ttl_ms))
        if self.fail_ack:
            raise RuntimeError("network down")
        return {"process_enabled": ttl_ms > 0, "heartbeat_timestamp": "exchange-ts"}

    async def submit_protected_order(
        self,
        order: ManagedOrder,
        protection: ServerSideBracket,
    ) -> str:
        self.protected.append((order, protection))
        return "protected-1"

    async def submit_order(self, order: ManagedOrder) -> str:
        self.plain.append(order)
        return "plain-1"

    async def cancel_order(self, order: ManagedOrder) -> str:
        return "cancelled"

    async def fetch_order_status(self, order: ManagedOrder) -> dict | None:
        return {"id": "venue-1", "client_order_id": order.client_order_id}

    async def fetch_exchange_truth(self) -> dict:
        return self.truth

    async def cancel_risk_increasing_orders(self, product_id: int | None = None) -> dict:
        return {"status": "accepted", "product_id": product_id}


def config() -> DeltaDeadmanConfig:
    return DeltaDeadmanConfig(
        heartbeat_id="vnedge-live-small",
        product_symbols=("BTCUSD", "ETHUSD"),
    )


def bracket(*, side: str = "long") -> ServerSideBracket:
    if side == "long":
        return ServerSideBracket(
            stop_loss_trigger_price=95.0,
            take_profit_trigger_price=110.0,
            stop_loss_limit_price=94.0,
            take_profit_limit_price=109.0,
        )
    return ServerSideBracket(
        stop_loss_trigger_price=105.0,
        take_profit_trigger_price=90.0,
        stop_loss_limit_price=106.0,
        take_profit_limit_price=91.0,
    )


def order(*, reduce_only: bool = False, protected: bool = True) -> ManagedOrder:
    intent = OrderIntent(
        symbol="BTCUSD",
        side="long",
        quantity=1.0,
        notional_usd=100.0,
        leverage=1.0,
        reduce_only=reduce_only,
        order_type="limit",
        limit_price=100.0,
        time_in_force=None if reduce_only else "PO",
        server_side_bracket=bracket() if protected else None,
    )
    return ManagedOrder(intent_key="intent-1", client_order_id="vne_protected_1", intent=intent)


def wrapper(
    adapter: FakeAdapter | None = None,
    journal: Journal | None = None,
    clock: Clock | None = None,
) -> tuple[DeltaExecutionSafetyWrapper, FakeAdapter, Journal, Clock]:
    venue = adapter or FakeAdapter()
    audit = journal or Journal()
    timer = clock or Clock()
    safety = DeltaExecutionSafetyWrapper(
        venue,
        config=config(),
        journal=audit,
        monotonic_clock=timer.monotonic,
        wall_clock=timer.wall,
    )
    return safety, venue, audit, timer


async def test_entry_refused_until_deadman_is_armed() -> None:
    safety, venue, _, _ = wrapper()
    with pytest.raises(AdapterRejection, match="not armed"):
        await safety.submit_order(order())
    assert venue.protected == []


async def test_start_arms_deadman_and_allows_atomic_protected_entry() -> None:
    safety, venue, audit, _ = wrapper()
    snapshot = await safety.start()
    assert snapshot.created and snapshot.armed
    assert safety.entry_ready
    assert await safety.submit_order(order()) == "protected-1"
    assert venue.created == 1
    assert venue.acks == [("vnedge-live-small", 15_000)]
    assert len(venue.protected) == 1
    assert any(kind == "delta_execution_safety_authorized" for kind, _ in audit.rows)


async def test_entry_without_server_side_bracket_is_always_refused() -> None:
    safety, venue, _, _ = wrapper()
    await safety.start()
    with pytest.raises(AdapterRejection, match="bracket is mandatory"):
        await safety.submit_order(order(protected=False))
    assert venue.protected == []


async def test_local_ttl_expiry_blocks_new_risk() -> None:
    safety, venue, _, timer = wrapper()
    await safety.start()
    timer.advance(16)
    assert not safety.entry_ready
    assert safety.snapshot.reason == "local_ttl_expired"
    with pytest.raises(AdapterRejection, match="not armed"):
        await safety.submit_order(order())
    assert venue.protected == []


async def test_ack_failure_disarms_but_reduce_only_exit_still_flows() -> None:
    safety, venue, _, _ = wrapper()
    await safety.start()
    venue.fail_ack = True
    with pytest.raises(DeltaSafetyError, match="acknowledgment failed"):
        await safety.acknowledge()
    assert not safety.entry_ready
    assert await safety.submit_order(order(reduce_only=True, protected=False)) == "plain-1"
    assert venue.plain[0].intent.reduce_only


async def test_journal_failure_blocks_entries_even_with_exchange_heartbeat() -> None:
    safety, venue, audit, _ = wrapper()
    await safety.start()
    audit.available = False
    with pytest.raises(AdapterRejection, match="not armed"):
        await safety.submit_order(order())
    assert venue.protected == []


async def test_disable_sends_zero_ttl_and_blocks_entries() -> None:
    safety, venue, _, _ = wrapper()
    await safety.start()
    snapshot = await safety.disable()
    assert not snapshot.armed and snapshot.reason == "disabled"
    assert venue.acks[-1] == ("vnedge-live-small", 0)


async def test_exchange_truth_is_hashed_and_journaled_without_claiming_reconciliation() -> None:
    safety, _, audit, _ = wrapper()
    snapshot = await safety.observe_exchange_truth()
    assert snapshot.open_order_count == 2
    assert snapshot.non_reduce_open_order_count == 1
    assert snapshot.nonzero_position_count == 1
    assert snapshot.wallet_balance_count == 1
    assert len(snapshot.payload_sha256) == 64
    rows = [payload for kind, payload in audit.rows if kind == "delta_exchange_truth_observed"]
    assert len(rows) == 1
    assert rows[0]["summary"]["payload_sha256"] == snapshot.payload_sha256


async def test_panic_cancel_preserves_stop_and_reduce_only_orders() -> None:
    safety, _, audit, _ = wrapper()
    result = await safety.cancel_risk_increasing_orders(product_id=27)
    assert result == {"status": "accepted", "product_id": 27}
    assert [kind for kind, _ in audit.rows] == [
        "delta_risk_increasing_cancel_requested",
        "delta_risk_increasing_cancel_completed",
    ]


def test_deadman_config_rejects_unsafe_timing() -> None:
    with pytest.raises(ValueError, match="plus entry guard"):
        DeltaDeadmanConfig(
            heartbeat_id="x",
            product_symbols=("BTCUSD",),
            ttl_ms=10_000,
            acknowledge_interval_ms=6_000,
            entry_guard_ms=5_000,
        )


def test_server_side_bracket_rejects_wrong_direction_geometry() -> None:
    with pytest.raises(ValueError, match="invalid long bracket geometry"):
        OrderIntent(
            symbol="BTCUSD",
            side="long",
            quantity=1.0,
            notional_usd=100.0,
            leverage=1.0,
            order_type="limit",
            limit_price=100.0,
            server_side_bracket=bracket(side="short"),
        )


class FakeResponse:
    def __init__(self, payload: dict, *, fail: bool = False) -> None:
        self.payload = payload
        self.fail = fail

    def raise_for_status(self) -> None:
        if self.fail:
            raise RuntimeError("HTTP 500")

    def json(self) -> dict:
        return self.payload


class FakeHttp:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.calls: list[dict] = []

    def request(self, method: str, url: str, **kwargs) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.response


def test_authenticated_client_signs_exact_heartbeat_body() -> None:
    http = FakeHttp(FakeResponse({"success": True, "result": {"process_enabled": True}}))
    client = DeltaAuthenticatedSafetyClient(
        api_key="key",
        api_secret="secret",
        http=http,
        wall_clock=lambda: 1_700_000_000.9,
    )
    result = client.acknowledge_heartbeat("hb-1", 15_000)
    assert result == {"process_enabled": True}
    call = http.calls[0]
    assert call["url"] == "https://api.india.delta.exchange/v2/heartbeat"
    body = json.dumps(
        {"heartbeat_id": "hb-1", "ttl": 15_000},
        sort_keys=True,
        separators=(",", ":"),
    )
    expected = hmac.new(
        b"secret",
        f"POST1700000000/v2/heartbeat{body}".encode(),
        hashlib.sha256,
    ).hexdigest()
    assert call["content"] == body
    assert call["headers"]["signature"] == expected
    assert call["headers"]["timestamp"] == "1700000000"


def test_authenticated_client_rejects_unsuccessful_payload() -> None:
    http = FakeHttp(FakeResponse({"success": False, "error": "bad heartbeat"}))
    client = DeltaAuthenticatedSafetyClient(
        api_key="key",
        api_secret="secret",
        http=http,
    )
    with pytest.raises(DeltaSafetyTransportError, match="unsuccessful"):
        client.acknowledge_heartbeat("hb-1", 15_000)


def test_authenticated_client_signs_exact_sorted_query_for_open_order_truth() -> None:
    http = FakeHttp(FakeResponse({"success": True, "result": []}))
    client = DeltaAuthenticatedSafetyClient(
        api_key="key",
        api_secret="secret",
        http=http,
        wall_clock=lambda: 1_700_000_000.9,
    )
    assert client.get_open_orders((27, 3136)) == ()
    call = http.calls[0]
    query = "?product_ids=27,3136&states=open,pending"
    assert call["url"] == f"https://api.india.delta.exchange/v2/orders{query}"
    expected = hmac.new(
        b"secret",
        f"GET1700000000/v2/orders{query}".encode(),
        hashlib.sha256,
    ).hexdigest()
    assert call["content"] == ""
    assert call["headers"]["signature"] == expected


def test_cancel_all_filter_keeps_protective_orders_alive() -> None:
    http = FakeHttp(FakeResponse({"success": True, "result": {"status": "accepted"}}))
    client = DeltaAuthenticatedSafetyClient(
        api_key="key",
        api_secret="secret",
        http=http,
        wall_clock=lambda: 1_700_000_000.0,
    )
    assert client.cancel_risk_increasing_orders(27) == {"status": "accepted"}
    call = http.calls[0]
    assert call["method"] == "DELETE"
    assert call["url"].endswith("/v2/orders/all")
    assert json.loads(call["content"]) == {
        "cancel_limit_orders": True,
        "cancel_reduce_only_orders": False,
        "cancel_stop_orders": False,
        "product_id": 27,
    }


def test_current_account_truth_endpoints_match_delta_v2_contract() -> None:
    wallet_http = FakeHttp(FakeResponse({"success": True, "result": []}))
    wallet_client = DeltaAuthenticatedSafetyClient(
        api_key="key", api_secret="secret", http=wallet_http
    )
    assert wallet_client.get_wallet_balances() == ()
    assert wallet_http.calls[0]["url"].endswith("/v2/wallet/balances")

    quota_http = FakeHttp(
        FakeResponse({"current_quota": 10_000, "remaining_time_in_milliseconds": 2_000})
    )
    quota_client = DeltaAuthenticatedSafetyClient(
        api_key="key", api_secret="secret", http=quota_http
    )
    assert quota_client.get_rate_limit_quota()["current_quota"] == 10_000
    assert quota_http.calls[0]["url"].endswith("/v2/rate_limits/quota")


class ExistingClient:
    def get_order_by_client_id(self, client_order_id: str) -> dict:
        return {"id": 11, "client_order_id": client_order_id}


class AtomicSafetyClient:
    def __init__(self) -> None:
        self.payloads: list[dict] = []

    def place_protected_order(self, payload: dict) -> dict:
        self.payloads.append(payload)
        return {"id": 22}

    def acknowledge_heartbeat(self, heartbeat_id: str, ttl_ms: int) -> dict:
        return {"heartbeat_id": heartbeat_id, "process_enabled": ttl_ms > 0}

    def get_open_orders(self, product_ids: tuple[int, ...]) -> tuple[dict, ...]:
        return ({"id": 1, "reduce_only": False, "product_ids": product_ids},)

    def get_position(self, product_id: int) -> dict:
        return {"product_id": product_id, "size": 0}

    def get_wallet_balances(self) -> tuple[dict, ...]:
        return ({"asset_symbol": "USD", "available_balance": "100"},)

    def cancel_risk_increasing_orders(self, product_id: int | None = None) -> dict:
        return {"status": "accepted", "product_id": product_id}


def test_native_adapter_sends_atomic_bracket_and_idempotency_fields() -> None:
    atomic = AtomicSafetyClient()
    adapter = DeltaRestExecutionAdapter(
        dry_run=False,
        api_key="key",
        api_secret="secret",
        live_confirmed=True,
        product_ids={"BTCUSD": 27},
        client=ExistingClient(),
        safety_client=atomic,
    )
    asyncio.run(adapter.acknowledge_deadman("hb-1", 15_000))
    result = asyncio.run(adapter.submit_protected_order(order(), bracket()))
    assert result == "22"
    payload = atomic.payloads[0]
    assert payload["client_order_id"] == "vne_protected_1"
    assert payload["post_only"] is True
    assert payload["reduce_only"] is False
    assert payload["bracket_stop_loss_price"] == "95.0"
    assert payload["bracket_take_profit_price"] == "110.0"


def test_native_adapter_rejects_protected_entry_when_deadman_is_unarmed() -> None:
    adapter = DeltaRestExecutionAdapter(
        dry_run=False,
        api_key="key",
        api_secret="secret",
        live_confirmed=True,
        product_ids={"BTCUSD": 27},
        client=ExistingClient(),
        safety_client=AtomicSafetyClient(),
    )
    with pytest.raises(AdapterRejection, match="dead-man heartbeat is not armed"):
        asyncio.run(adapter.submit_protected_order(order(), bracket()))


def test_native_adapter_fetches_truth_for_every_configured_product() -> None:
    atomic = AtomicSafetyClient()
    adapter = DeltaRestExecutionAdapter(
        dry_run=False,
        api_key="key",
        api_secret="secret",
        live_confirmed=True,
        product_ids={"BTCUSD": 27, "ETHUSD": 3136},
        client=ExistingClient(),
        safety_client=atomic,
    )
    truth = asyncio.run(adapter.fetch_exchange_truth())
    assert [position["product_id"] for position in truth["positions"]] == [27, 3136]
    assert truth["open_orders"][0]["product_ids"] == (27, 3136)
    assert truth["wallet_balances"][0]["asset_symbol"] == "USD"
