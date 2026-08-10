"""Order manager — the only path from an approved intent to a venue.

Submission pipeline (every step journaled before the next runs):

    intent -> duplicate check -> journal availability -> unresolved-order
    check -> pre-trade risk gateway -> mint client_order_id -> journal the
    intent -> submit via adapter -> ack / reject / TIMEOUT_UNKNOWN

Hard rules enforced here:
- No adapter call ever happens for an intent the risk gateway rejected.
- The client_order_id is journaled BEFORE the venue can possibly know it —
  crash recovery can always tell which orders might exist.
- While ANY order is TIMEOUT_UNKNOWN/RECONCILING, risk-increasing orders are
  rejected; reduce-only exits still flow (getting out is never blocked).
- Journal unavailable => same policy: exits only.
- A timed-out submission is never retried blindly; it parks in
  TIMEOUT_UNKNOWN until reconciliation resolves it via `resolve_order`.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Iterable, Protocol

from vnedge.execution.idempotency import IntentRegistry, mint_client_order_id
from vnedge.execution.journal import DecisionJournal
from vnedge.execution.order_state import ManagedOrder, OrderState
from vnedge.execution.order_state import IllegalTransition
from vnedge.risk.risk_manager import (
    AccountState,
    MarketState,
    OrderIntent,
    PreTradeRiskGateway,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FlattenTarget:
    """A position to close, as reported by exchange truth."""

    symbol: str
    side: str  # "long" | "short" — the position's direction
    quantity: float


class AdapterRejection(Exception):
    """Venue definitively rejected the order (insufficient margin, bad
    symbol...). Terminal for this order."""


class AdapterTimeout(Exception):
    """Submission MAY have reached the venue — final state unknown."""


class ExecutionAdapter(Protocol):
    async def submit_order(self, order: ManagedOrder) -> str:
        """Submit; returns the exchange order id. Raises AdapterRejection or
        AdapterTimeout. Retries (with the SAME client_order_id) live inside
        adapter implementations, never here."""
        ...


class OrderManager:
    def __init__(
        self,
        gateway: PreTradeRiskGateway,
        journal: DecisionJournal,
        adapter: ExecutionAdapter,
    ) -> None:
        self._gateway = gateway
        self._journal = journal
        self._adapter = adapter
        self._registry = IntentRegistry()
        self.orders: dict[str, ManagedOrder] = {}  # by client_order_id

    @property
    def has_unresolved_orders(self) -> bool:
        return any(o.is_unresolved for o in self.orders.values())

    async def submit(
        self,
        intent: OrderIntent,
        account: AccountState,
        market: MarketState,
        intent_key: str,
        now: datetime | None = None,
        *,
        replaces: str | None = None,
    ) -> ManagedOrder:
        """Run one intent through the full pipeline. Always returns the
        ManagedOrder — inspect .state and .history for the outcome.

        ``replaces`` records cancel/replace lineage (the client_order_id of
        the order this one replaces) in the journaled intent."""
        order = ManagedOrder(
            intent_key=intent_key,
            client_order_id=mint_client_order_id(),
            intent=intent,
            replaces=replaces,
        )

        # --- Duplicate decision guard ---------------------------------------
        if not self._registry.register(intent_key, order.client_order_id):
            existing = self._registry.existing_order_id(intent_key)
            order.transition(OrderState.RISK_REQUESTED, "duplicate check")
            order.transition(
                OrderState.RISK_REJECTED,
                f"duplicate intent — already handled as {existing}",
            )
            self._journal.append("duplicate_intent_dropped", {
                "intent_key": intent_key, "existing_order": existing,
            })
            logger.warning("duplicate intent dropped: %s", intent_key)
            self.orders[order.client_order_id] = order
            return order

        # --- Exits-only guards (journal health, unresolved orders) ----------
        if not intent.reduce_only:
            if not self._journal.available:
                return self._refuse(order, "decision journal unavailable — exits only")
            if self.has_unresolved_orders:
                return self._refuse(
                    order,
                    "order(s) in TIMEOUT_UNKNOWN/RECONCILING — no new risk "
                    "until reconciliation resolves them",
                )

        # --- Pre-trade risk gateway ------------------------------------------
        order.transition(OrderState.RISK_REQUESTED)
        # `now` lets replay/paper evaluate at bar time; live passes exchange-
        # synced time. Defaults to wall clock.
        decision = self._gateway.evaluate(intent, account, market, now=now)
        self._journal.append("risk_decision", {
            "intent_key": intent_key,
            "client_order_id": order.client_order_id,
            "approved": decision.approved,
            "failed_checks": list(decision.failed_checks),
            "warning_checks": list(decision.warning_checks),
            "intent": asdict(intent),
        })
        if not decision.approved:
            order.transition(OrderState.RISK_REJECTED, decision.explanation)
            self.orders[order.client_order_id] = order
            return order
        order.transition(OrderState.RISK_APPROVED)

        # --- Journal the intent BEFORE the venue can know about it ----------
        order.transition(OrderState.ORDER_INTENT_CREATED)
        intent_record = {
            "intent_key": intent_key,
            "client_order_id": order.client_order_id,
            "intent": asdict(intent),
        }
        if replaces is not None:
            intent_record["replaces"] = replaces
        journaled = self._journal.append("order_intent", intent_record)
        if not journaled and not intent.reduce_only:
            # Journal died mid-pipeline: refuse to create unrecorded risk.
            order.transition(OrderState.SUBMITTING, "aborting — journal write failed")
            order.transition(OrderState.REJECTED, "journal write failed before submit")
            self.orders[order.client_order_id] = order
            return order

        # --- Submit ------------------------------------------------------------
        order.transition(OrderState.SUBMITTING)
        self.orders[order.client_order_id] = order
        try:
            exchange_id = await self._adapter.submit_order(order)
        except AdapterRejection as exc:
            order.transition(OrderState.REJECTED, f"venue rejected: {exc}")
            self._journal.append("order_rejected", {
                "client_order_id": order.client_order_id, "reason": str(exc),
            })
            return order
        except AdapterTimeout as exc:
            order.transition(OrderState.TIMEOUT_UNKNOWN, str(exc))
            self._journal.append("order_timeout_unknown", {
                "client_order_id": order.client_order_id, "detail": str(exc),
            })
            logger.critical(
                "ORDER %s TIMEOUT_UNKNOWN — blocking new risk until reconciled",
                order.client_order_id,
            )
            return order

        if order.exchange_order_id is None:
            order.exchange_order_id = exchange_id
        if order.state is OrderState.SUBMITTING:
            order.transition(OrderState.ACKNOWLEDGED, f"exchange id {exchange_id}")
        else:
            self._journal.append("order_ack_race_resolved", {
                "client_order_id": order.client_order_id,
                "exchange_order_id": exchange_id,
                "state": order.state.value,
            })
        self._journal.append("order_acknowledged", {
            "client_order_id": order.client_order_id,
            "exchange_order_id": exchange_id,
        })
        return order

    def _refuse(self, order: ManagedOrder, reason: str) -> ManagedOrder:
        order.transition(OrderState.RISK_REQUESTED, "pre-gateway guard")
        order.transition(OrderState.RISK_REJECTED, reason)
        self._journal.append("order_refused", {
            "client_order_id": order.client_order_id, "reason": reason,
        })
        logger.warning("order refused: %s", reason)
        self.orders[order.client_order_id] = order
        return order

    async def emergency_flatten(
        self,
        targets: Iterable[FlattenTarget],
        account: AccountState,
        markets: dict[str, MarketState],
        flatten_id: str,
        now: datetime | None = None,
    ) -> list[ManagedOrder]:
        """Close every position with reduce-only market orders — through the
        normal pipeline (gateway included; kill switch permits reduce-only by
        design). ``flatten_id`` makes the operation idempotent: re-invoking
        with the same id cannot double-close."""
        self._journal.append("emergency_flatten_started", {"flatten_id": flatten_id})
        logger.critical("EMERGENCY FLATTEN %s initiated", flatten_id)
        orders = []
        for pos in targets:
            intent = OrderIntent(
                symbol=pos.symbol,
                side="short" if pos.side == "long" else "long",
                quantity=pos.quantity,
                notional_usd=0.0,
                leverage=1.0,
                reduce_only=True,
                strategy_id="emergency_flatten",
            )
            orders.append(
                await self.submit(
                    intent, account, markets[pos.symbol],
                    intent_key=f"flatten|{flatten_id}|{pos.symbol}",
                    now=now,
                )
            )
        self._journal.append("emergency_flatten_finished", {
            "flatten_id": flatten_id,
            "results": {o.client_order_id: o.state.value for o in orders},
        })
        return orders

    async def cancel_order(self, client_order_id: str, reason: str = "") -> ManagedOrder:
        """Cancel a working order. The venue's answer wins: if the order
        filled before the cancel arrived, the state becomes FILLED — a cancel
        is a request, not a fact."""
        order = self.orders[client_order_id]
        order.transition(OrderState.CANCEL_REQUESTED, reason)
        venue_state = await self._adapter.cancel_order(order)
        target = {
            "cancelled": OrderState.CANCELLED,
            "filled": OrderState.FILLED,
            "partially_filled": OrderState.PARTIALLY_FILLED,
        }.get(venue_state)
        if target is None:
            order.transition(OrderState.TIMEOUT_UNKNOWN,
                             f"cancel returned unknown venue state '{venue_state}'")
        else:
            order.transition(target, f"venue: {venue_state}")
        # A cancel can land after partial (or full) fills — pull the venue's
        # fill totals so remaining-quantity math downstream is honest.
        await self._refresh_fill_state(order)
        self._journal.append("order_cancel", {
            "client_order_id": client_order_id, "venue_state": venue_state,
            "filled_quantity": order.filled_quantity,
            "reason": reason,
        })
        return order

    async def _refresh_fill_state(self, order: ManagedOrder) -> None:
        """Refresh filled_quantity/fees from venue status when the adapter
        exposes ``fetch_order_status``. Monotonic: a stale poll can never
        regress a fresher private-stream update. Failure is non-fatal."""
        fetch = getattr(self._adapter, "fetch_order_status", None)
        if fetch is None:
            return
        try:
            status = await fetch(order)
        except Exception as exc:  # noqa: BLE001 — refresh is best-effort
            logger.warning(
                "fill-state refresh failed for %s: %s", order.client_order_id, exc
            )
            return
        if not status:
            return
        filled = float(status.get("filled") or 0.0)
        order.filled_quantity = max(order.filled_quantity, filled)
        fee = status.get("fee") or {}
        if isinstance(fee, dict):
            fee_total = float(fee.get("cost") or 0.0)
            order.fees_paid = max(order.fees_paid, fee_total)

    def sync_fill_state(
        self,
        client_order_id: str,
        *,
        venue_state: str,
        filled_quantity: float,
        fees_total: float = 0.0,
    ) -> bool:
        """Update a working order's fill accounting from polled venue status
        (reconciliation / REST status polling). Inputs are CUMULATIVE venue
        totals; updates are monotonic. Returns True if anything changed."""
        order = self.orders.get(client_order_id)
        if order is None:
            return False
        changed = False
        if filled_quantity > order.filled_quantity + 1e-12:
            order.filled_quantity = float(filled_quantity)
            changed = True
        if fees_total > order.fees_paid + 1e-12:
            order.fees_paid = float(fees_total)
            changed = True
        target: OrderState | None = None
        if venue_state == "partially_filled" and order.state is OrderState.ACKNOWLEDGED:
            target = OrderState.PARTIALLY_FILLED
        elif venue_state == "filled" and order.state in (
            OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED
        ):
            target = OrderState.FILLED
        if target is not None:
            order.transition(target, f"status poll: venue reports {venue_state}")
            changed = True
        if changed:
            self._journal.append("order_fill_sync", {
                "client_order_id": client_order_id,
                "state": order.state.value,
                "filled_quantity": order.filled_quantity,
                "fees_paid": order.fees_paid,
                "venue_state": venue_state,
            })
        return changed

    async def cancel_replace(
        self,
        client_order_id: str,
        *,
        account: AccountState,
        market: MarketState,
        new_limit_price: float | None = None,
        new_quantity: float | None = None,
        now: datetime | None = None,
    ) -> tuple[ManagedOrder, ManagedOrder | None]:
        """Cancel a working order and submit a replacement (new price and/or
        quantity) with journaled lineage to the original.

        Gateway rule (documented deliberately): EVERY replacement re-runs
        the pre-trade risk gateway via the normal submit pipeline — including
        reduce-only replaces and pure price improvements that a minimal rule
        would allow to skip re-evaluation. This repo admits NO gateway bypass
        path, so a risk-increasing replace always re-evaluates by
        construction, and reduce-only replacements still flow because the
        gateway itself permits reduce-only exits even under halts.

        The replacement is a NEW order: a fresh client_order_id minted once,
        an intent key derived deterministically from the original (never from
        timestamps), and ``replaces=<old client_order_id>`` journaled with
        the intent BEFORE the venue can know about it.

        Raced fills: if the venue answers anything but 'cancelled' (filled,
        still-working partial, unknown), NOTHING is resubmitted — the fill
        already changed the position; replacing on top would double up.

        Partial fills: the replacement covers ONLY the unfilled remainder;
        a ``new_quantity`` above the remainder raises (the old order is
        already cancelled at that point — fail closed, nothing at the venue).
        """
        old = self.orders[client_order_id]
        self._journal.append("cancel_replace_requested", {
            "client_order_id": client_order_id,
            "intent_key": old.intent_key,
            "new_limit_price": new_limit_price,
            "new_quantity": new_quantity,
        })

        old = await self.cancel_order(client_order_id, reason="cancel/replace")
        if old.state is not OrderState.CANCELLED:
            self._journal.append("cancel_replace_outcome", {
                "client_order_id": client_order_id,
                "outcome": "aborted_no_resubmit",
                "old_state": old.state.value,
                "filled_quantity": old.filled_quantity,
            })
            return old, None

        remaining = old.intent.quantity - old.filled_quantity
        if remaining <= 0:
            self._journal.append("cancel_replace_outcome", {
                "client_order_id": client_order_id,
                "outcome": "nothing_remaining",
                "filled_quantity": old.filled_quantity,
            })
            return old, None
        quantity = remaining if new_quantity is None else float(new_quantity)
        if quantity > remaining + 1e-12:
            self._journal.append("cancel_replace_outcome", {
                "client_order_id": client_order_id,
                "outcome": "rejected_quantity_exceeds_remaining",
                "requested_quantity": quantity,
                "remaining_quantity": remaining,
            })
            raise ValueError(
                f"replacement quantity {quantity} exceeds unfilled remainder "
                f"{remaining} of {client_order_id}"
            )

        prior = old.intent
        limit_price = new_limit_price if new_limit_price is not None else prior.limit_price
        if limit_price is not None:
            notional_usd = quantity * limit_price
        else:
            notional_usd = prior.notional_usd * (quantity / prior.quantity)
        replacement_intent = OrderIntent(
            symbol=prior.symbol,
            side=prior.side,
            quantity=quantity,
            notional_usd=notional_usd,
            leverage=prior.leverage,
            reduce_only=prior.reduce_only,
            strategy_id=prior.strategy_id,
            order_type=prior.order_type,
            limit_price=limit_price,
        )
        # Minted once per (original intent, replaced order id) pair —
        # deterministic lineage, never derived from timestamps.
        replacement_key = f"{old.intent_key}|replace|{old.client_order_id}"
        replacement = await self.submit(
            replacement_intent, account, market, replacement_key,
            now=now, replaces=old.client_order_id,
        )
        self._journal.append("cancel_replace_outcome", {
            "client_order_id": client_order_id,
            "outcome": "replaced",
            "replaces": old.client_order_id,
            "replacement_client_order_id": replacement.client_order_id,
            "replacement_state": replacement.state.value,
            "replacement_quantity": quantity,
            "replacement_limit_price": limit_price,
        })
        return old, replacement

    # --- Reconciliation hooks (driven by the reconciliation engine, m6) ------
    def begin_reconciliation(self, client_order_id: str) -> None:
        order = self.orders[client_order_id]
        order.transition(OrderState.RECONCILING, "reconciliation started")
        self._journal.append("reconciling", {"client_order_id": client_order_id})

    def resolve_order(
        self, client_order_id: str, resolved_state: OrderState, note: str
    ) -> None:
        """Resolve an unknown order to what the EXCHANGE says it is. This is
        the only exit from TIMEOUT_UNKNOWN — never assumption."""
        order = self.orders[client_order_id]
        order.transition(resolved_state, f"reconciled: {note}")
        self._journal.append("order_resolved", {
            "client_order_id": client_order_id,
            "state": resolved_state.value,
            "note": note,
        })

    def apply_venue_order_update(
        self,
        *,
        client_order_id: str,
        state: OrderState,
        note: str,
        exchange_order_id: str | None = None,
        filled_quantity: float | None = None,
        fees_paid: float | None = None,
    ) -> bool:
        """Apply a private stream order update through the state machine.

        Private WS is venue truth, but it still must respect our state model
        and WAL. This method handles expected races: a private fill can arrive
        before the REST submit returns, and a venue-side cancel can arrive
        before we requested one locally. Conflicting terminal updates are
        logged and ignored instead of guessing.
        """
        order = self.orders.get(client_order_id)
        if order is None:
            self._journal.append("private_order_unmatched", {
                "client_order_id": client_order_id,
                "exchange_order_id": exchange_order_id,
                "state": state.value,
                "note": note,
            })
            logger.error("private stream update for unknown order %s", client_order_id)
            return False

        if exchange_order_id and order.exchange_order_id is None:
            order.exchange_order_id = exchange_order_id
        if filled_quantity is not None:
            order.filled_quantity = max(order.filled_quantity, float(filled_quantity))
        if fees_paid is not None:
            order.fees_paid += max(float(fees_paid), 0.0)

        if order.state is state:
            self._journal.append("private_order_update", {
                "client_order_id": client_order_id,
                "state": state.value,
                "note": note,
                "no_state_change": True,
            })
            return True

        if order.is_terminal:
            self._journal.append("private_order_terminal_conflict", {
                "client_order_id": client_order_id,
                "current_state": order.state.value,
                "venue_state": state.value,
                "note": note,
            })
            logger.error(
                "private stream conflicts with terminal order %s: %s -> %s",
                client_order_id, order.state.value, state.value,
            )
            return False

        try:
            self._transition_from_private_stream(order, state, note)
        except IllegalTransition as exc:
            self._journal.append("private_order_transition_error", {
                "client_order_id": client_order_id,
                "current_state": order.state.value,
                "venue_state": state.value,
                "note": note,
                "error": str(exc),
            })
            logger.error("private stream transition failed: %s", exc)
            return False

        self._journal.append("private_order_update", {
            "client_order_id": client_order_id,
            "exchange_order_id": order.exchange_order_id,
            "state": order.state.value,
            "filled_quantity": order.filled_quantity,
            "fees_paid": order.fees_paid,
            "note": note,
        })
        return True

    def apply_venue_fill_update(
        self,
        *,
        client_order_id: str,
        exchange_order_id: str | None,
        trade_id: str,
        fill_quantity: float,
        fill_price: float | None = None,
        fee_cost: float = 0.0,
    ) -> bool:
        """Apply one private trade/fill event idempotently."""
        order = self.orders.get(client_order_id)
        if order is None:
            self._journal.append("private_fill_unmatched", {
                "client_order_id": client_order_id,
                "exchange_order_id": exchange_order_id,
                "trade_id": trade_id,
                "fill_quantity": fill_quantity,
            })
            logger.error("private stream fill for unknown order %s", client_order_id)
            return False

        order.filled_quantity += max(float(fill_quantity), 0.0)
        order.fees_paid += max(float(fee_cost), 0.0)
        target = (
            OrderState.FILLED
            if order.filled_quantity + 1e-12 >= order.intent.quantity
            else OrderState.PARTIALLY_FILLED
        )
        return self.apply_venue_order_update(
            client_order_id=client_order_id,
            exchange_order_id=exchange_order_id,
            state=target,
            note=(
                f"private fill {trade_id}: qty={fill_quantity}, "
                f"price={fill_price}, fee={fee_cost}"
            ),
        )

    def client_id_for_exchange_order(self, exchange_order_id: str) -> str | None:
        for order in self.orders.values():
            if order.exchange_order_id == exchange_order_id:
                return order.client_order_id
        return None

    @staticmethod
    def _transition_from_private_stream(
        order: ManagedOrder, state: OrderState, note: str
    ) -> None:
        if order.state is OrderState.TIMEOUT_UNKNOWN:
            order.transition(OrderState.RECONCILING, "private stream resolved timeout")

        if state is OrderState.ACKNOWLEDGED:
            if order.state is OrderState.PARTIALLY_FILLED:
                return  # never downgrade a partial fill back to open
            order.transition(OrderState.ACKNOWLEDGED, note)
            return

        if state is OrderState.PARTIALLY_FILLED:
            if order.state is OrderState.SUBMITTING:
                order.transition(OrderState.ACKNOWLEDGED, "private fill before submit ack")
            order.transition(OrderState.PARTIALLY_FILLED, note)
            return

        if state is OrderState.FILLED:
            if order.state is OrderState.SUBMITTING:
                order.transition(OrderState.ACKNOWLEDGED, "private fill before submit ack")
            order.transition(OrderState.FILLED, note)
            return

        if state is OrderState.CANCELLED:
            if order.state is OrderState.SUBMITTING:
                order.transition(OrderState.ACKNOWLEDGED, "venue cancel before submit ack")
            if order.state in (OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED):
                order.transition(OrderState.CANCEL_REQUESTED, "venue-side cancel event")
            order.transition(OrderState.CANCELLED, note)
            return

        if state is OrderState.REJECTED:
            if order.state is OrderState.SUBMITTING:
                order.transition(OrderState.ACKNOWLEDGED, "venue reject after submit ack")
            order.transition(OrderState.REJECTED, note)
            return

        order.transition(state, note)
