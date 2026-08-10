"""Live reconciliation — TIMEOUT_UNKNOWN resolution against real venue truth.

Same contract as the paper reconciler (docs/DESIGN.md §3), driven through the
live adapter's fetch_order_status. Absence at the venue is evidence: a
submission that never arrived resolves to REJECTED. An unmappable venue
status leaves the order in RECONCILING — still unresolved, still blocking
new risk — and logs loudly; we never guess.
"""

from __future__ import annotations

import logging

from vnedge.execution.order_manager import OrderManager
from vnedge.execution.order_state import OrderState, UNRESOLVED_STATES

logger = logging.getLogger(__name__)

_CCXT_STATUS_MAP = {
    "closed": OrderState.FILLED,
    "canceled": OrderState.CANCELLED,
    "cancelled": OrderState.CANCELLED,
    "expired": OrderState.CANCELLED,
    "rejected": OrderState.REJECTED,
}


class LiveReconciler:
    def __init__(self, order_manager: OrderManager, adapter) -> None:
        self._om = order_manager
        self._adapter = adapter

    async def resolve_unknown_orders(self) -> list[str]:
        resolved: list[str] = []
        for order in list(self._om.orders.values()):
            if order.state not in UNRESOLVED_STATES:
                continue
            if order.state is not OrderState.RECONCILING:
                self._om.begin_reconciliation(order.client_order_id)
            status = await self._adapter.fetch_order_status(order)
            if status is None:
                self._om.resolve_order(
                    order.client_order_id, OrderState.REJECTED,
                    "not found at venue — submission never arrived",
                )
                resolved.append(order.client_order_id)
                continue
            s = str(status.get("status", ""))
            filled = float(status.get("filled") or 0.0)
            if s == "open":
                target = (
                    OrderState.PARTIALLY_FILLED if filled > 0 else OrderState.ACKNOWLEDGED
                )
            else:
                target = _CCXT_STATUS_MAP.get(s)
            if target is None:
                logger.error(
                    "order %s: unmappable venue status '%s' — staying in "
                    "RECONCILING, new risk remains blocked",
                    order.client_order_id, s,
                )
                continue
            self._om.resolve_order(
                order.client_order_id, target,
                f"venue reports {s} (filled {filled})",
            )
            resolved.append(order.client_order_id)
        return resolved
