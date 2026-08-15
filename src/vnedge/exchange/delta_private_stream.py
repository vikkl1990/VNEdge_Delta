"""Authenticated Delta private order/fill stream with sequence integrity.

The stream is reconciliation input only.  It never submits orders.  Missing
sequence numbers or stale data make health false and therefore block entries;
REST reconciliation remains the source for rebuilding truth after a gap.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime
from typing import Any

from vnedge.execution.order_manager import OrderManager
from vnedge.execution.order_state import OrderState
from vnedge.execution.private_stream import (
    PrivateFillUpdate,
    PrivateOrderUpdate,
    PrivateStreamEventApplier,
    PrivateStreamHealth,
)

DELTA_PRIVATE_WS = "wss://socket.india.delta.exchange"
_STATE_MAP = {
    "open": OrderState.ACKNOWLEDGED,
    "pending": OrderState.ACKNOWLEDGED,
    "closed": OrderState.FILLED,
    "filled": OrderState.FILLED,
    "cancelled": OrderState.CANCELLED,
    "canceled": OrderState.CANCELLED,
    "rejected": OrderState.REJECTED,
}


class DeltaPrivateStream:
    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        symbols: tuple[str, ...],
        order_manager: OrderManager,
        url: str = DELTA_PRIVATE_WS,
        health: PrivateStreamHealth | None = None,
        websocket_factory=None,
    ) -> None:
        if not api_key or not api_secret:
            raise ValueError("Delta private stream requires trade-only credentials")
        if not symbols:
            raise ValueError("Delta private stream requires product symbols")
        if url != DELTA_PRIVATE_WS:
            raise ValueError("Delta private stream is pinned to the production India endpoint")
        self._api_key = api_key
        self._api_secret = api_secret.encode("utf-8")
        self.symbols = tuple(symbols)
        self.url = url
        self.health = health or PrivateStreamHealth()
        self._applier = PrivateStreamEventApplier(order_manager)
        self._websocket_factory = websocket_factory
        self._socket = None
        self._sequences: dict[tuple[str, str], int] = {}

    async def run_forever(
        self,
        *,
        stop_event: asyncio.Event,
        retry_delay_seconds: float = 1.0,
    ) -> None:
        while not stop_event.is_set():
            try:
                await self._run_connection(stop_event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.health.mark_error(exc)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=retry_delay_seconds)
                except TimeoutError:
                    pass

    async def close(self) -> None:
        socket = self._socket
        self._socket = None
        if socket is not None:
            await socket.close()
        self.health.connected = False

    async def _run_connection(self, stop_event: asyncio.Event) -> None:
        factory = self._websocket_factory
        if factory is None:  # pragma: no cover - live network construction
            from websockets.asyncio.client import connect

            factory = connect
        async with factory(self.url, ping_interval=20, ping_timeout=20) as socket:
            self._socket = socket
            await socket.send(json.dumps(self._auth_message()))
            authenticated = False
            while not stop_event.is_set():
                try:
                    raw = await asyncio.wait_for(socket.recv(), timeout=2.0)
                except TimeoutError:
                    # Quiet accounts produce no order frames.  Ping/pong proves
                    # transport health without pretending an order event exists.
                    pong = await asyncio.wait_for(socket.ping(), timeout=2.0)
                    # websockets versions differ: some await the pong inside
                    # ping(), while others return a second pong waiter.
                    if hasattr(pong, "__await__"):
                        await asyncio.wait_for(pong, timeout=2.0)
                    self.health.connected = authenticated
                    self.health.last_event_at = datetime.now(UTC)
                    continue
                message = json.loads(raw)
                self.health.connected = authenticated
                self.health.last_event_at = datetime.now(UTC)
                if message.get("type") == "key-auth":
                    if message.get("success") is not True:
                        raise RuntimeError(
                            f"Delta private authentication failed: {message.get('status')}"
                        )
                    authenticated = True
                    await socket.send(json.dumps(self._subscription()))
                    self.health.connected = True
                    self.health.last_event_at = datetime.now(UTC)
                    continue
                if not authenticated:
                    raise RuntimeError("Delta private event arrived before authentication")
                self.apply_message(message)

    def _auth_message(self) -> dict[str, Any]:
        timestamp = int(time.time())
        signature = hmac.new(
            self._api_secret,
            f"GET{timestamp}/live".encode(),
            hashlib.sha256,
        ).hexdigest()
        return {
            "type": "key-auth",
            "payload": {
                "api-key": self._api_key,
                "timestamp": timestamp,
                "signature": signature,
            },
        }

    def _subscription(self) -> dict[str, Any]:
        return {
            "type": "subscribe",
            "payload": {
                "channels": [
                    {"name": "orders", "symbols": list(self.symbols)},
                    {"name": "v2/user_trades", "symbols": list(self.symbols)},
                    {"name": "positions", "symbols": list(self.symbols)},
                ]
            },
        }

    def apply_message(self, message: dict[str, Any]) -> None:
        kind = str(message.get("type") or "")
        symbol = str(message.get("symbol") or message.get("sy") or "")
        self._check_sequence(kind, symbol, message)
        if kind == "orders":
            rows = message.get("result") if message.get("action") == "snapshot" else [message]
            if not isinstance(rows, list):
                raise ValueError("Delta order snapshot has invalid result")
            for row in rows:
                update = _normalize_order(dict(row), symbol)
                self._applier.apply_order(update)
                self.health.mark_event("order")
        elif kind in {"v2/user_trades", "user_trades"}:
            update = _normalize_fill(message)
            self._applier.apply_fill(update)
            self.health.mark_event("fill")
        elif kind == "positions":
            # Positions keep the stream fresh; the account provider and REST
            # reconciliation own actual position truth.
            self.health.connected = True
            self.health.last_event_at = datetime.now(UTC)

    def _check_sequence(self, kind: str, symbol: str, message: dict[str, Any]) -> None:
        raw = message.get("sequence_id", message.get("seq_no"))
        meta = message.get("meta")
        if raw is None and isinstance(meta, dict):
            raw = meta.get("seq_no")
        if raw is None or not kind or not symbol:
            return
        sequence = int(raw)
        key = (kind, symbol)
        previous = self._sequences.get(key)
        if previous is not None and sequence != previous + 1 and sequence != 1:
            self.health.connected = False
            raise RuntimeError(
                f"Delta private sequence gap {kind}:{symbol} {previous}->{sequence}"
            )
        self._sequences[key] = sequence


def _normalize_order(row: dict[str, Any], fallback_symbol: str) -> PrivateOrderUpdate:
    state_text = str(row.get("state") or row.get("status") or "").lower()
    state = _STATE_MAP.get(state_text)
    if state is None:
        raise ValueError(f"unmapped Delta private order state: {state_text}")
    size = float(row.get("size") or 0)
    unfilled = float(row.get("unfilled_size") or 0)
    return PrivateOrderUpdate(
        client_order_id=_optional_string(row.get("client_order_id")),
        exchange_order_id=_optional_string(row.get("id") or row.get("order_id")),
        symbol=str(row.get("product_symbol") or row.get("symbol") or fallback_symbol),
        status=state_text,
        state=state,
        filled_quantity=max(0.0, size - unfilled),
        raw=row,
    )


def _normalize_fill(row: dict[str, Any]) -> PrivateFillUpdate:
    return PrivateFillUpdate(
        client_order_id=_optional_string(row.get("client_order_id") or row.get("c")),
        exchange_order_id=_optional_string(row.get("order_id") or row.get("o")),
        trade_id=str(row.get("fill_id") or row.get("f") or ""),
        symbol=str(row.get("symbol") or row.get("sy") or ""),
        side=str(row.get("side") or row.get("S") or "").lower() or None,
        price=float(row.get("price") or row.get("p") or 0) or None,
        quantity=float(row.get("size") or row.get("s") or 0),
        fee_cost=float(row.get("commission") or 0),
        fee_currency=_optional_string(row.get("commission_currency")),
        raw=row,
    )


def _optional_string(value: Any) -> str | None:
    return None if value in (None, "") else str(value)
