"""Order state machine — legal paths, illegal transitions, terminal states."""

import pytest

from vnedge.execution.order_state import (
    IllegalTransition,
    LEGAL_TRANSITIONS,
    ManagedOrder,
    OrderState as S,
    TERMINAL_STATES,
)
from vnedge.risk.risk_manager import OrderIntent


def order() -> ManagedOrder:
    return ManagedOrder(
        intent_key="k", client_order_id="vne_test",
        intent=OrderIntent("BTC/USDT:USDT", "long", 0.01, 100.0, 3.0),
    )


def test_happy_path_walks_the_machine():
    o = order()
    for state in (
        S.RISK_REQUESTED, S.RISK_APPROVED, S.ORDER_INTENT_CREATED,
        S.SUBMITTING, S.ACKNOWLEDGED, S.PARTIALLY_FILLED, S.FILLED,
    ):
        o.transition(state)
    assert o.is_terminal
    assert len(o.history) == 7


def test_illegal_transition_raises():
    o = order()
    with pytest.raises(IllegalTransition, match="not a legal transition"):
        o.transition(S.FILLED)  # signal_created -> filled is nonsense


def test_terminal_states_are_dead_ends():
    for terminal in TERMINAL_STATES:
        assert LEGAL_TRANSITIONS[terminal] == frozenset()


def test_timeout_unknown_only_exits_via_reconciliation():
    assert LEGAL_TRANSITIONS[S.TIMEOUT_UNKNOWN] == frozenset({S.RECONCILING})


def test_reconciling_resolves_to_confirmed_states_only():
    # never back to SUBMITTING, never to TIMEOUT_UNKNOWN
    assert S.SUBMITTING not in LEGAL_TRANSITIONS[S.RECONCILING]
    assert S.TIMEOUT_UNKNOWN not in LEGAL_TRANSITIONS[S.RECONCILING]


def test_every_state_has_a_transition_entry():
    assert set(LEGAL_TRANSITIONS) == set(S)
