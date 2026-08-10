"""Scalper replay diagnostics — explain signal silence without forcing trades."""

from datetime import UTC, datetime

from vnedge.scalping.microstructure import TopOfBook, TradeTick
from vnedge.research.scalper_replay_diagnostics import (
    ReplaySweepConfig,
    diagnose_events,
    render_text_report,
)

SYM = "BTC/USDT:USDT"
T0 = 1_750_000_000_000


def book(ts_ms, bid, bid_sz, ask, ask_sz):
    return (ts_ms, "book", TopOfBook(
        symbol=SYM, bid=bid, bid_size=bid_sz, ask=ask, ask_size=ask_sz,
        event_time=datetime.fromtimestamp(ts_ms / 1000, tz=UTC)))


def trade(ts_ms, price, qty, side):
    return (ts_ms, "trade", TradeTick(
        symbol=SYM, price=price, quantity=qty, taker_side=side,
        event_time=datetime.fromtimestamp(ts_ms / 1000, tz=UTC)))


def cfg(**overrides):
    params = dict(
        min_imbalances=(0.5,),
        max_spread_bps=(3.0,),
        min_span_seconds=0.0,
        min_fills_for_candidate=2,
    )
    params.update(overrides)
    return ReplaySweepConfig(**params)


def diagnose(events, **overrides):
    return diagnose_events(
        events,
        exchange="test",
        symbol=SYM,
        day="20260704",
        config=cfg(**overrides),
    )


def test_missing_tick_data_is_primary_blocker():
    report = diagnose([])
    assert report.primary_blocker == "NO_TICK_DATA"
    assert "tick recorder" in report.action


def test_no_quotes_when_book_filters_never_qualify():
    events = [
        book(T0, 100.0, 5.0, 100.1, 5.0),       # zero imbalance
        trade(T0 + 10, 100.1, 1.0, "buy"),
        book(T0 + 2000, 100.0, 5.0, 100.1, 5.0),
    ]
    report = diagnose(events)
    assert report.primary_blocker == "NO_QUOTES"
    assert report.rows[0].quotes == 0


def test_no_fills_when_passive_quotes_do_not_get_through_trades():
    events = [
        book(T0, 100.0, 9.0, 100.01, 1.0),      # quote buy
        trade(T0 + 10, 100.0, 1.0, "sell"),     # touch only, not through
        book(T0 + 2000, 100.0, 9.0, 100.01, 1.0),
    ]
    report = diagnose(events)
    assert report.primary_blocker == "NO_FILLS"
    assert report.rows[0].quotes == 1
    assert report.rows[0].filled == 0


def test_negative_edge_after_cost_is_reported():
    events = [
        book(T0, 100.0, 9.0, 100.01, 1.0),       # quote buy
        trade(T0 + 10, 99.99, 1.0, "sell"),      # fill through bid
        book(T0 + 20, 99.9, 9.0, 99.91, 1.0),    # stop
        book(T0 + 30, 99.8, 9.0, 99.81, 1.0),
    ]
    report = diagnose(events, stop_bps=5.0, target_bps=30.0)
    assert report.primary_blocker == "NEGATIVE_EDGE_AFTER_COST"
    assert report.rows[0].verdict == "NEGATIVE_EDGE"
    assert report.rows[0].net_usd < 0
    assert "do not force signals" in report.action


def test_adaptive_exit_policy_is_wired_into_diagnostics():
    events = [
        book(T0, 100.0, 9.0, 100.1, 1.0),
        trade(T0 + 10, 99.99, 1.0, "sell"),
        book(T0 + 20, 100.5, 9.0, 100.6, 1.0),
        book(T0 + 30, 100.25, 9.0, 100.35, 1.0),
    ]
    report = diagnose(
        events,
        family_id="liquidity_vacuum_continuation",
        exit_policy_id="adaptive_trail",
        min_imbalances=(0.5,),
        max_spread_bps=(12.0,),
        stop_bps=60.0,
        target_bps=100.0,
        maker_bps=0.0,
        taker_bps=0.0,
        slippage_bps=0.0,
    )

    row = report.rows[0]
    assert row.exit_policy_id == "adaptive_trail"
    assert row.exit_reason_counts == {"trail": 1}
    assert row.net_usd > 0


def test_text_report_names_blocker_and_best_row():
    events = [
        book(T0, 100.0, 9.0, 100.01, 1.0),
        trade(T0 + 10, 99.99, 1.0, "sell"),
        book(T0 + 20, 99.9, 9.0, 99.91, 1.0),
    ]
    text = render_text_report(diagnose(events, stop_bps=5.0, target_bps=30.0))
    assert "primary_blocker=NEGATIVE_EDGE_AFTER_COST" in text
    assert "best=imb>=0.50" in text
