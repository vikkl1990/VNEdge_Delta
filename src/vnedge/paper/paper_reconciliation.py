"""Reconciliation against simulated-exchange truth.

Two jobs, mirroring what the live reconciliation engine (same shape, live
adapter) will do:

1. **Resolve unknown orders**: every TIMEOUT_UNKNOWN order is looked up at
   the venue and resolved to what the venue says — found-filled, found-open,
   found-cancelled, or not-found (= never reached the venue, resolved
   REJECTED). Assumption is not an option; absence of evidence at the venue
   IS evidence for a lost submission.
2. **Compare order states**: OrderManager's view vs venue truth, producing
   an explicit mismatch list. Fail-closed policy on mismatch is enforced by
   the caller (stop entries, reduce-only) per docs/DESIGN.md §3.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from vnedge.execution.order_manager import OrderManager
from vnedge.execution.order_state import OrderState, UNRESOLVED_STATES
from vnedge.paper.simulated_exchange import SimulatedExchange

logger = logging.getLogger(__name__)

_VENUE_STATE_TO_ORDER_STATE = {
    "filled": OrderState.FILLED,
    "partially_filled": OrderState.PARTIALLY_FILLED,
    "open": OrderState.ACKNOWLEDGED,
    "cancelled": OrderState.CANCELLED,
    "rejected": OrderState.REJECTED,
}

# What the venue may legitimately say for each internal state.
_CONSISTENT = {
    OrderState.ACKNOWLEDGED: {"open", "filled", "partially_filled", "cancelled"},
    OrderState.PARTIALLY_FILLED: {"partially_filled", "filled", "cancelled"},
    OrderState.FILLED: {"filled"},
    OrderState.CANCELLED: {"cancelled"},
    OrderState.REJECTED: {"rejected"},
}


@dataclass(frozen=True)
class ReconciliationReport:
    resolved_orders: tuple[str, ...]
    mismatches: tuple[str, ...]

    @property
    def clean(self) -> bool:
        return not self.mismatches


class PaperReconciler:
    def __init__(self, order_manager: OrderManager, exchange: SimulatedExchange) -> None:
        self._om = order_manager
        self._exchange = exchange

    def run(self) -> ReconciliationReport:
        resolved = self._resolve_unknown_orders()
        self._sync_fills()
        mismatches = self._compare_orders()
        report = ReconciliationReport(tuple(resolved), tuple(mismatches))
        if not report.clean:
            logger.error("reconciliation mismatches: %s", report.mismatches)
        return report

    def _sync_fills(self) -> list[str]:
        """Push venue fill truth (filled qty, fees, partial/full fill state)
        into working ManagedOrders. Polling counterpart of the private
        stream: without it a partial fill on a resting limit order would
        never reach filled_quantity/fees_paid in paper mode."""
        synced = []
        for order in list(self._om.orders.values()):
            if order.state not in (
                OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED
            ):
                continue
            status = self._exchange.get_order_status(order.client_order_id)
            if status is None:
                continue
            if self._om.sync_fill_state(
                order.client_order_id,
                venue_state=status.state,
                filled_quantity=status.filled_qty,
                fees_total=status.fee_usd,
            ):
                synced.append(order.client_order_id)
        return synced

    def _resolve_unknown_orders(self) -> list[str]:
        resolved = []
        for order in list(self._om.orders.values()):
            if order.state not in UNRESOLVED_STATES:
                continue
            if order.state is not OrderState.RECONCILING:
                self._om.begin_reconciliation(order.client_order_id)
            status = self._exchange.get_order_status(order.client_order_id)
            if status is None:
                self._om.resolve_order(
                    order.client_order_id, OrderState.REJECTED,
                    "not found at venue — submission never arrived",
                )
            else:
                self._om.resolve_order(
                    order.client_order_id,
                    _VENUE_STATE_TO_ORDER_STATE[status.state],
                    f"venue reports {status.state} "
                    f"(filled {status.filled_qty} @ {status.avg_fill_price})",
                )
            resolved.append(order.client_order_id)
        return resolved

    def _compare_orders(self) -> list[str]:
        mismatches = []
        for order in self._om.orders.values():
            expected = _CONSISTENT.get(order.state)
            if expected is None:
                continue  # pre-submission states have no venue counterpart
            status = self._exchange.get_order_status(order.client_order_id)
            if order.state is OrderState.REJECTED and status is None:
                continue  # rejected before/without reaching the venue — consistent
            if status is None:
                mismatches.append(
                    f"{order.client_order_id}: internal {order.state.value} "
                    "but unknown at venue"
                )
            elif status.state not in expected:
                mismatches.append(
                    f"{order.client_order_id}: internal {order.state.value} "
                    f"vs venue {status.state}"
                )
        return mismatches
