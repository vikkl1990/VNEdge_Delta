"""Order state machine (docs/DESIGN.md §2).

Every order lives in exactly one state; transitions are whitelisted and
anything else raises. The state nobody else models properly is the one that
matters most:

    TIMEOUT_UNKNOWN — the order MAY have reached the exchange; we cannot
    confirm. It is never resolved by assumption, only by reconciliation
    (TIMEOUT_UNKNOWN -> RECONCILING -> a confirmed state). While any order
    is unresolved, the order manager blocks all risk-increasing orders.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum

from vnedge.risk.risk_manager import OrderIntent


class OrderState(str, Enum):
    SIGNAL_CREATED = "signal_created"
    RISK_REQUESTED = "risk_requested"
    RISK_APPROVED = "risk_approved"
    RISK_REJECTED = "risk_rejected"
    ORDER_INTENT_CREATED = "order_intent_created"
    SUBMITTING = "submitting"
    ACKNOWLEDGED = "acknowledged"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    TIMEOUT_UNKNOWN = "timeout_unknown"
    RECONCILING = "reconciling"


S = OrderState

LEGAL_TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    S.SIGNAL_CREATED: frozenset({S.RISK_REQUESTED}),
    S.RISK_REQUESTED: frozenset({S.RISK_APPROVED, S.RISK_REJECTED}),
    S.RISK_APPROVED: frozenset({S.ORDER_INTENT_CREATED}),
    S.RISK_REJECTED: frozenset(),  # terminal
    S.ORDER_INTENT_CREATED: frozenset({S.SUBMITTING}),
    S.SUBMITTING: frozenset({S.ACKNOWLEDGED, S.REJECTED, S.TIMEOUT_UNKNOWN}),
    S.ACKNOWLEDGED: frozenset(
        {S.PARTIALLY_FILLED, S.FILLED, S.CANCEL_REQUESTED, S.REJECTED}
    ),
    S.PARTIALLY_FILLED: frozenset(
        {S.PARTIALLY_FILLED, S.FILLED, S.CANCEL_REQUESTED}
    ),
    S.FILLED: frozenset(),  # terminal
    S.CANCEL_REQUESTED: frozenset(
        {S.CANCELLED, S.FILLED, S.PARTIALLY_FILLED, S.TIMEOUT_UNKNOWN}
    ),
    S.CANCELLED: frozenset(),  # terminal
    S.REJECTED: frozenset(),  # terminal
    # Unknown state is NEVER resolved by assumption — only via reconciliation.
    S.TIMEOUT_UNKNOWN: frozenset({S.RECONCILING}),
    S.RECONCILING: frozenset(
        {S.ACKNOWLEDGED, S.PARTIALLY_FILLED, S.FILLED, S.CANCELLED, S.REJECTED}
    ),
}

TERMINAL_STATES = frozenset(
    {S.RISK_REJECTED, S.FILLED, S.CANCELLED, S.REJECTED}
)
UNRESOLVED_STATES = frozenset({S.TIMEOUT_UNKNOWN, S.RECONCILING})


class IllegalTransition(RuntimeError):
    pass


@dataclass(frozen=True)
class StateEvent:
    timestamp: datetime
    state: OrderState
    note: str


@dataclass
class ManagedOrder:
    intent_key: str  # deterministic decision identity — minted once, journaled
    client_order_id: str  # idempotency key sent to the venue — never regenerated
    intent: OrderIntent
    state: OrderState = OrderState.SIGNAL_CREATED
    exchange_order_id: str | None = None
    filled_quantity: float = 0.0
    fees_paid: float = 0.0
    # Lineage: client_order_id of the order this one replaced (cancel/replace).
    replaces: str | None = None
    history: list[StateEvent] = field(default_factory=list)

    def transition(self, new_state: OrderState, note: str = "") -> None:
        allowed = LEGAL_TRANSITIONS[self.state]
        if new_state not in allowed:
            raise IllegalTransition(
                f"{self.client_order_id}: {self.state.value} -> {new_state.value} "
                f"is not a legal transition (allowed: {sorted(s.value for s in allowed)})"
            )
        self.state = new_state
        self.history.append(StateEvent(datetime.now(UTC), new_state, note))

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def is_unresolved(self) -> bool:
        return self.state in UNRESOLVED_STATES
