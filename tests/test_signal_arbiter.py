"""Execution signal arbitration — rank edge, then ask risk about one winner."""

from vnedge.execution.signal_arbiter import (
    ArbiterConfig,
    SignalArbiter,
    SignalCandidate,
)
from vnedge.strategy.base_strategy import SignalIntent


def candidate(
    source_id: str,
    *,
    side: str = "long",
    symbol: str = "BTC/USDT:USDT",
    edge: float = 5.0,
    cost: float = 1.0,
    pf: float | None = 1.2,
    route: str = "MAKER_ONLY",
    notional: float | None = None,
) -> SignalCandidate:
    stop = 99.0 if side == "long" else 101.0
    return SignalCandidate(
        source_id=source_id,
        strategy_id=source_id.split("#")[0],
        symbol=symbol,
        signal=SignalIntent(side, stop_price=stop, reason=f"{source_id} fired"),
        expected_edge_bps=edge,
        expected_cost_bps=cost,
        profit_factor=pf,
        route=route,
        planned_notional_usd=notional,
    )


def test_rejects_blocked_and_sub_breakeven_candidates():
    arbiter = SignalArbiter(ArbiterConfig(min_net_edge_bps=0.0))
    decision = arbiter.arbitrate([
        candidate("blocked", edge=10.0, cost=1.0, route="BLOCKED"),
        candidate("fee_wall", edge=1.0, cost=2.0),
    ])

    assert decision.selected == ()
    assert [r.reason for r in decision.rejected] == [
        "route_blocked",
        "below_breakeven_net_edge",
    ]


def test_unknown_route_uses_profit_factor_to_allow_taker():
    arbiter = SignalArbiter(
        ArbiterConfig(
            max_selected=2,
            taker_min_profit_factor=1.30,
            taker_min_net_edge_bps=2.0,
        )
    )

    strong = candidate("strong_pf", edge=6.0, cost=2.0, pf=1.6, route="UNKNOWN")
    weak = candidate("weak_pf", symbol="ETH/USDT:USDT", edge=6.0, cost=2.0,
                     pf=1.1, route="UNKNOWN")
    decision = arbiter.arbitrate([weak, strong])

    by_source = {c.source_id: c for c in decision.selected}
    assert by_source["strong_pf"].route == "TAKER_ALLOWED"
    assert by_source["weak_pf"].route == "MAKER_ONLY"


def test_selects_highest_score_and_rejects_opposite_side_conflict():
    arbiter = SignalArbiter(ArbiterConfig(max_selected=2, max_per_symbol=2))

    long = candidate("long_edge", edge=8.0, cost=1.0, side="long")
    short = candidate("short_edge", edge=7.0, cost=1.0, side="short")
    eth = candidate("eth_edge", symbol="ETH/USDT:USDT", edge=5.0, cost=1.0)
    decision = arbiter.arbitrate([short, eth, long])

    assert [c.source_id for c in decision.selected] == ["long_edge", "eth_edge"]
    assert any(
        r.candidate.source_id == "short_edge"
        and r.reason == "opposite_side_conflict_with=long_edge"
        for r in decision.rejected
    )


def test_respects_notional_budget_after_ranking():
    arbiter = SignalArbiter(
        ArbiterConfig(max_selected=3, max_total_planned_notional_usd=100.0)
    )

    first = candidate("first", edge=8.0, cost=1.0, notional=80.0)
    too_big = candidate("too_big", symbol="ETH/USDT:USDT", edge=7.0, cost=1.0,
                        notional=30.0)
    small = candidate("small", symbol="SOL/USDT:USDT", edge=6.0, cost=1.0,
                      notional=20.0)
    decision = arbiter.arbitrate([too_big, small, first])

    assert [c.source_id for c in decision.selected] == ["first", "small"]
    assert any(
        r.candidate.source_id == "too_big" and r.reason == "notional_budget_exceeded"
        for r in decision.rejected
    )


def test_decision_to_signal_preserves_intent_and_adds_audit_reason():
    decision = SignalArbiter().arbitrate([candidate("alpha", side="short")])

    signal = decision.to_signal()

    assert signal is not None
    assert signal.side == "short"
    assert "alpha fired" in signal.reason
    assert "arbiter_selected source=alpha" in signal.reason
    assert "net_edge_bps=4.00" in signal.reason
