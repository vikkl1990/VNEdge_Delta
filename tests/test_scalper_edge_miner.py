"""Scalper edge miner — microstructure hypotheses must clear PF/breakeven."""

from datetime import UTC, datetime

from vnedge.research.scalper_edge_miner import EdgeMinerConfig, mine_events
from vnedge.scalping.microstructure import TopOfBook, TradeTick

SYM = "BTC/USDT:USDT"
T0 = 1_750_000_000_000


def book(ts_ms, mid, bid_sz=9.0, ask_sz=1.0):
    spread = 0.01
    return (ts_ms, "book", TopOfBook(
        symbol=SYM,
        bid=mid - spread / 2,
        bid_size=bid_sz,
        ask=mid + spread / 2,
        ask_size=ask_sz,
        event_time=datetime.fromtimestamp(ts_ms / 1000, tz=UTC),
    ))


def trade(ts_ms, price, qty=1.0, side="buy"):
    return (ts_ms, "trade", TradeTick(
        symbol=SYM,
        price=price,
        quantity=qty,
        taker_side=side,
        event_time=datetime.fromtimestamp(ts_ms / 1000, tz=UTC),
    ))


def cfg(**kw):
    params = dict(
        horizons_ms=(1_000,),
        imbalance_thresholds=(0.35,),
        flow_thresholds=(0.58,),
        microprice_threshold_bps=(0.10,),
        min_samples=10,
        min_trade_count=3,
        sample_every_ms=0,
    )
    params.update(kw)
    return EdgeMinerConfig(**params)


def rising_pressure_events(n=30):
    events = []
    for i in range(n):
        ts = T0 + i * 100
        mid = 100.0 + i * 0.04
        events.append(trade(ts - 2, mid, side="buy"))
        events.append(book(ts, mid, bid_sz=9.0, ask_sz=1.0))
    return sorted(events, key=lambda e: (e[0], 0 if e[1] == "book" else 1))


def flat_pressure_events(n=30):
    events = []
    for i in range(n):
        ts = T0 + i * 100
        mid = 100.0
        events.append(trade(ts - 2, mid, side="buy"))
        events.append(book(ts, mid, bid_sz=9.0, ask_sz=1.0))
    return sorted(events, key=lambda e: (e[0], 0 if e[1] == "book" else 1))


def test_pressure_continuation_candidate_clears_route_gate():
    results = mine_events(
        rising_pressure_events(),
        exchange="test",
        symbol=SYM,
        day="20260704",
        config=cfg(),
    )

    best = results[0]
    assert best.state in {"EDGE_CANDIDATE_MAKER", "EDGE_CANDIDATE_TAKER"}
    assert best.route_decision.route in {"MAKER_ONLY", "TAKER_ALLOWED"}
    assert best.profit_factor is not None and best.profit_factor > 1.15
    assert best.avg_net_bps is not None and best.avg_net_bps > 0.5
    assert best.can_trade is False
    assert best.execution_evidence == "hypothesis_only"
    assert best.fill_assumption == "synthetic_observation_fill_not_replay"


def test_flat_pressure_is_below_breakeven_not_signal():
    results = mine_events(
        flat_pressure_events(),
        exchange="test",
        symbol=SYM,
        day="20260704",
        config=cfg(),
    )

    assert results
    assert all(r.state == "BELOW_BREAKEVEN" for r in results)
    assert all(r.route_decision.route == "BLOCKED" for r in results)
