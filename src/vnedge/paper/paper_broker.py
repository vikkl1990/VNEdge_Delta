"""Paper broker — the first real ExecutionAdapter.

Maps OrderManager's ManagedOrder onto the simulated exchange and injects
scripted failures deterministically, including the two distinct timeout
flavors that reconciliation must treat differently:

- ``timeout_reached``: the order DID land at the venue before the ack was
  lost — reconciliation will find it (the dangerous real-world case).
- ``timeout_lost``: the order never arrived — reconciliation finds nothing.

Intent-side mapping: intent.side is the order's market impact
("long" = buy, "short" = sell), consistent with the backtester.
"""

from __future__ import annotations

from collections import deque

from vnedge.execution.order_manager import AdapterRejection, AdapterTimeout
from vnedge.execution.order_state import ManagedOrder
from vnedge.paper.simulated_exchange import PaperOrderRequest, SimulatedExchange

#: scripted behaviors: "ok", "reject:<reason>", "timeout_reached", "timeout_lost"
Script = list[str]


class PaperBroker:
    def __init__(self, exchange: SimulatedExchange, script: Script | None = None) -> None:
        self.exchange = exchange
        self._script: deque[str] = deque(script or [])

    def _next_behavior(self) -> str:
        return self._script.popleft() if self._script else "ok"

    async def submit_order(self, order: ManagedOrder) -> str:
        behavior = self._next_behavior()

        if behavior.startswith("reject:"):
            raise AdapterRejection(behavior.split(":", 1)[1])
        if behavior == "timeout_lost":
            # Never reached the venue; nothing exists there.
            raise AdapterTimeout("submit timed out (never reached venue)")

        request = PaperOrderRequest(
            client_order_id=order.client_order_id,
            symbol=order.intent.symbol,
            buy=order.intent.side == "long",
            quantity=order.intent.quantity,
            reduce_only=order.intent.reduce_only,
            order_type=order.intent.order_type,
            limit_price=order.intent.limit_price,
        )
        status = self.exchange.submit_order(request)

        if behavior == "timeout_reached":
            # Order landed, ack lost — the case reconciliation exists for.
            raise AdapterTimeout("ack lost after submission reached venue")
        if status.state == "rejected":
            raise AdapterRejection(status.reason)
        return status.exchange_order_id

    async def cancel_order(self, order: ManagedOrder) -> str:
        """Cancel at the venue; returns the venue's resulting state
        ('cancelled', or 'filled'/'partially_filled' if it beat the cancel)."""
        return self.exchange.cancel_order(order.client_order_id).state

    async def fetch_order_status(self, order: ManagedOrder) -> dict | None:
        """Venue truth in the same dict shape the live adapter returns
        (ccxt-style status / filled / fee)."""
        status = self.exchange.get_order_status(order.client_order_id)
        if status is None:
            return None
        ccxt_state = {
            "open": "open",
            "partially_filled": "open",
            "filled": "closed",
            "cancelled": "canceled",
            "rejected": "rejected",
        }.get(status.state, status.state)
        return {
            "status": ccxt_state,
            "filled": status.filled_qty,
            "fee": {"cost": status.fee_usd},
        }
