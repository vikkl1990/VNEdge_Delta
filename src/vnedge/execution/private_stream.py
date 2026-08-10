"""Private order/fill stream for live execution.

This module consumes CCXT-Pro user streams (`watch_orders` and
`watch_my_trades`) and applies venue truth through OrderManager. It never
submits orders and never bypasses the gateway; it is reconciliation input.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Iterable, Literal

from vnedge.execution.order_manager import OrderManager
from vnedge.execution.order_state import OrderState


_CCXT_ORDER_STATUS_MAP = {
    "open": OrderState.ACKNOWLEDGED,
    "closed": OrderState.FILLED,
    "canceled": OrderState.CANCELLED,
    "cancelled": OrderState.CANCELLED,
    "expired": OrderState.CANCELLED,
    "rejected": OrderState.REJECTED,
}


@dataclass(frozen=True)
class PrivateOrderUpdate:
    client_order_id: str | None
    exchange_order_id: str | None
    symbol: str | None
    status: str
    state: OrderState
    filled_quantity: float
    raw: dict[str, Any]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["state"] = self.state.value
        return d


@dataclass(frozen=True)
class PrivateFillUpdate:
    client_order_id: str | None
    exchange_order_id: str | None
    trade_id: str
    symbol: str | None
    side: str | None
    price: float | None
    quantity: float
    fee_cost: float
    fee_currency: str | None
    raw: dict[str, Any]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PrivateStreamHealth:
    connected: bool = False
    last_event_at: datetime | None = None
    last_error: str | None = None
    orders_seen: int = 0
    fills_seen: int = 0

    def mark_event(self, kind: Literal["order", "fill"]) -> None:
        self.connected = True
        self.last_event_at = datetime.now(UTC)
        self.last_error = None
        if kind == "order":
            self.orders_seen += 1
        else:
            self.fills_seen += 1

    def mark_error(self, exc: BaseException) -> None:
        self.connected = False
        self.last_error = f"{type(exc).__name__}: {exc}"

    def age_seconds(self, now: datetime | None = None) -> float:
        if self.last_event_at is None:
            return float("inf")
        return ((now or datetime.now(UTC)) - self.last_event_at).total_seconds()

    def snapshot(self, now: datetime | None = None) -> dict:
        return {
            "connected": self.connected,
            "age_seconds": self.age_seconds(now),
            "last_event_at": self.last_event_at.isoformat() if self.last_event_at else None,
            "last_error": self.last_error,
            "orders_seen": self.orders_seen,
            "fills_seen": self.fills_seen,
        }


class PrivateStreamEventApplier:
    """Apply normalized private stream events to the order manager."""

    def __init__(self, order_manager: OrderManager) -> None:
        self._om = order_manager
        self._seen_trade_ids: set[str] = set()

    def apply_order(self, update: PrivateOrderUpdate) -> bool:
        client_id = self._resolve_client_id(update.client_order_id, update.exchange_order_id)
        if client_id is None:
            return self._om.apply_venue_order_update(
                client_order_id="",
                exchange_order_id=update.exchange_order_id,
                state=update.state,
                filled_quantity=update.filled_quantity,
                note=f"private order update without client id: {update.status}",
            )
        target = update.state
        if update.state is OrderState.ACKNOWLEDGED and update.filled_quantity > 0:
            target = OrderState.PARTIALLY_FILLED
        return self._om.apply_venue_order_update(
            client_order_id=client_id,
            exchange_order_id=update.exchange_order_id,
            state=target,
            filled_quantity=update.filled_quantity,
            note=f"private order stream status={update.status}",
        )

    def apply_fill(self, update: PrivateFillUpdate) -> bool:
        if update.trade_id in self._seen_trade_ids:
            return True
        client_id = self._resolve_client_id(update.client_order_id, update.exchange_order_id)
        if client_id is None:
            applied = self._om.apply_venue_fill_update(
                client_order_id="",
                exchange_order_id=update.exchange_order_id,
                trade_id=update.trade_id,
                fill_quantity=update.quantity,
                fill_price=update.price,
                fee_cost=update.fee_cost,
            )
        else:
            applied = self._om.apply_venue_fill_update(
                client_order_id=client_id,
                exchange_order_id=update.exchange_order_id,
                trade_id=update.trade_id,
                fill_quantity=update.quantity,
                fill_price=update.price,
                fee_cost=update.fee_cost,
            )
        if applied:
            self._seen_trade_ids.add(update.trade_id)
        return applied

    def _resolve_client_id(
        self, client_order_id: str | None, exchange_order_id: str | None
    ) -> str | None:
        if client_order_id:
            return client_order_id
        if exchange_order_id:
            return self._om.client_id_for_exchange_order(exchange_order_id)
        return None


class CcxtPrivateStream:
    """Thin CCXT-Pro private stream wrapper.

    A fake `client` can be injected for tests. Without a client this constructs
    a real `ccxt.pro` exchange on production endpoints only; testnet/sandbox
    private streams are refused because they are not valid execution evidence.
    """

    def __init__(
        self,
        exchange_id: str = "binanceusdm",
        *,
        api_key: str,
        api_secret: str,
        applier: PrivateStreamEventApplier,
        testnet: bool = False,
        live_confirmed: bool = False,
        client: object | None = None,
        health: PrivateStreamHealth | None = None,
    ) -> None:
        if not api_key or not api_secret:
            raise ValueError("private stream requires API credentials (trade-only keys)")
        if testnet:
            raise ValueError(
                "testnet private streams are disabled: use production private "
                "truth behind live_confirmed gates"
            )
        if not live_confirmed:
            raise ValueError("mainnet private stream requires live_confirmed=True")
        self.exchange_id = exchange_id
        self.testnet = testnet
        self.applier = applier
        self.health = health or PrivateStreamHealth()
        if client is not None:
            self._ex = client
        else:  # pragma: no cover - network client construction
            import ccxt.pro as ccxt_pro

            self._ex = getattr(ccxt_pro, exchange_id)(
                {"apiKey": api_key, "secret": api_secret, "enableRateLimit": True}
            )

    async def close(self) -> None:
        close = getattr(self._ex, "close", None)
        if close is not None:
            await close()

    async def watch_orders_once(self) -> tuple[PrivateOrderUpdate, ...]:
        try:
            raw = await self._ex.watch_orders()
            updates = tuple(normalize_order_update(item) for item in _as_items(raw))
            for update in updates:
                self.applier.apply_order(update)
                self.health.mark_event("order")
            return updates
        except Exception as exc:
            self.health.mark_error(exc)
            raise

    async def watch_fills_once(self, symbol: str | None = None) -> tuple[PrivateFillUpdate, ...]:
        try:
            if symbol is None:
                raw = await self._ex.watch_my_trades()
            else:
                raw = await self._ex.watch_my_trades(symbol)
            updates = tuple(normalize_fill_update(item) for item in _as_items(raw))
            for update in updates:
                self.applier.apply_fill(update)
                self.health.mark_event("fill")
            return updates
        except Exception as exc:
            self.health.mark_error(exc)
            raise

    async def run_forever(
        self,
        *,
        symbol: str | None = None,
        stop_event: asyncio.Event | None = None,
        retry_delay_seconds: float = 1.0,
    ) -> None:
        while stop_event is None or not stop_event.is_set():
            try:
                await asyncio.gather(
                    self.watch_orders_once(),
                    self.watch_fills_once(symbol),
                )
            except Exception:
                await asyncio.sleep(retry_delay_seconds)


def normalize_order_update(raw: dict[str, Any]) -> PrivateOrderUpdate:
    status = str(_get(raw, ("status", "X", "x"), default="")).lower()
    state = _CCXT_ORDER_STATUS_MAP.get(status)
    if state is None:
        raise ValueError(f"unmapped private order status: {status}")
    return PrivateOrderUpdate(
        client_order_id=_str_or_none(_get(raw, ("clientOrderId", "client_order_id", "c"))),
        exchange_order_id=_str_or_none(_get(raw, ("id", "orderId", "order", "i"))),
        symbol=_str_or_none(_get(raw, ("symbol", "s"))),
        status=status,
        state=state,
        filled_quantity=float(_get(raw, ("filled", "filledAmount", "z"), default=0.0) or 0.0),
        raw=raw,
    )


def normalize_fill_update(raw: dict[str, Any]) -> PrivateFillUpdate:
    fee = raw.get("fee") if isinstance(raw.get("fee"), dict) else {}
    exchange_order_id = _str_or_none(_get(raw, ("order", "orderId", "i")))
    trade_id = _str_or_none(_get(raw, ("id", "tradeId", "t")))
    if not trade_id:
        trade_id = "|".join(
            str(x) for x in (
                exchange_order_id or "",
                _get(raw, ("timestamp", "T"), default=""),
                _get(raw, ("amount", "qty", "q"), default=""),
            )
        )
    return PrivateFillUpdate(
        client_order_id=_str_or_none(_get(raw, ("clientOrderId", "client_order_id", "c"))),
        exchange_order_id=exchange_order_id,
        trade_id=trade_id,
        symbol=_str_or_none(_get(raw, ("symbol", "s"))),
        side=_str_or_none(_get(raw, ("side", "S"))),
        price=_float_or_none(_get(raw, ("price", "p"))),
        quantity=float(_get(raw, ("amount", "qty", "q"), default=0.0) or 0.0),
        fee_cost=float(fee.get("cost") or _get(raw, ("feeCost", "n"), default=0.0) or 0.0),
        fee_currency=_str_or_none(fee.get("currency") or _get(raw, ("feeCurrency", "N"))),
        raw=raw,
    )


def _as_items(raw: Any) -> Iterable[dict[str, Any]]:
    if raw is None:
        return ()
    if isinstance(raw, dict):
        return (raw,)
    return tuple(item for item in raw if isinstance(item, dict))


def _get(raw: dict[str, Any], keys: tuple[str, ...], default: Any = None) -> Any:
    info = raw.get("info") if isinstance(raw.get("info"), dict) else {}
    for key in keys:
        if raw.get(key) is not None:
            return raw[key]
        if info.get(key) is not None:
            return info[key]
    return default


def _str_or_none(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)
