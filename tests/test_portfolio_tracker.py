"""Portfolio tracker — equity math, daily reset, consecutive-loss counting."""

from datetime import UTC, datetime

import pytest

from vnedge.paper.fill_model import FillModel
from vnedge.paper.simulated_exchange import PaperOrderRequest, SimulatedExchange
from vnedge.runtime.portfolio_tracker import PortfolioTracker

SYM = "BTC/USDT:USDT"


def make_world(balance: float = 500.0):
    # zero slippage/fees where noted to make hand math exact
    ex = SimulatedExchange(FillModel(slippage_bps=0, taker_fee_bps=0), balance)
    ex.set_quote(SYM, bid=100.0, ask=100.0)
    return ex, PortfolioTracker(ex, balance)


def ts(day: int, hour: int = 0) -> datetime:
    return datetime(2026, 7, day, hour, tzinfo=UTC)


def buy(ex, coid, qty=1.0, reduce_only=False):
    return ex.submit_order(PaperOrderRequest(coid, SYM, True, qty, reduce_only))


def sell(ex, coid, qty=1.0, reduce_only=False):
    return ex.submit_order(PaperOrderRequest(coid, SYM, False, qty, reduce_only))


def test_equity_is_balance_plus_unrealized():
    ex, tracker = make_world()
    buy(ex, "o1", qty=2.0)  # 2 @ 100
    ex.set_quote(SYM, bid=105.0, ask=105.0)
    assert tracker.unrealized_pnl_usd() == pytest.approx(10.0)
    assert tracker.equity_usd() == pytest.approx(510.0)
    state = tracker.account_state()
    assert state.exposure_by_symbol_usd[SYM] == pytest.approx(210.0)
    assert state.open_positions == 1


def test_daily_pnl_resets_at_utc_midnight():
    ex, tracker = make_world()
    tracker.on_bar(ts(1, 10))
    buy(ex, "o1", qty=1.0)
    ex.set_quote(SYM, bid=110.0, ask=110.0)
    tracker.on_bar(ts(1, 11))
    assert tracker.account_state().daily_pnl_usd == pytest.approx(10.0)

    tracker.on_bar(ts(2, 0))  # new UTC day: baseline moves to current equity
    assert tracker.account_state().daily_pnl_usd == pytest.approx(0.0)
    ex.set_quote(SYM, bid=104.0, ask=104.0)
    assert tracker.account_state().daily_pnl_usd == pytest.approx(-6.0)


def test_peak_equity_tracks_high_water_mark():
    ex, tracker = make_world()
    buy(ex, "o1", qty=1.0)
    ex.set_quote(SYM, bid=120.0, ask=120.0)
    tracker.on_bar(ts(1, 1))
    ex.set_quote(SYM, bid=90.0, ask=90.0)
    tracker.on_bar(ts(1, 2))
    assert tracker.account_state().peak_equity_usd == pytest.approx(520.0)


def test_consecutive_losses_count_round_trips_net_of_fees():
    ex, tracker = make_world()

    # losing round trip 1: buy 100, sell 95
    buy(ex, "o1")
    ex.set_quote(SYM, bid=95.0, ask=95.0)
    sell(ex, "o2", reduce_only=True)
    tracker.on_bar(ts(1, 1))
    assert tracker.consecutive_losses == 1

    # losing round trip 2
    ex.set_quote(SYM, bid=100.0, ask=100.0)
    buy(ex, "o3")
    ex.set_quote(SYM, bid=96.0, ask=96.0)
    sell(ex, "o4", reduce_only=True)
    tracker.on_bar(ts(1, 2))
    assert tracker.consecutive_losses == 2

    # winner resets
    ex.set_quote(SYM, bid=100.0, ask=100.0)
    buy(ex, "o5")
    ex.set_quote(SYM, bid=108.0, ask=108.0)
    sell(ex, "o6", reduce_only=True)
    tracker.on_bar(ts(1, 3))
    assert tracker.consecutive_losses == 0


def test_fee_only_win_is_still_a_loss():
    """Gross +$0 after $0.50 of fees = a losing round trip."""
    ex = SimulatedExchange(FillModel(slippage_bps=0, taker_fee_bps=25), 500.0)
    ex.set_quote(SYM, bid=100.0, ask=100.0)
    tracker = PortfolioTracker(ex, 500.0)
    buy(ex, "o1")
    sell(ex, "o2", reduce_only=True)  # flat at same price, fees paid both ways
    tracker.on_bar(ts(1, 1))
    assert tracker.consecutive_losses == 1
