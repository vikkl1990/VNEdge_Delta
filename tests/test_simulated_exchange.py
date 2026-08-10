"""Simulated exchange mechanics — fills, accounting, reduce-only, limits."""

import pytest

from vnedge.paper.fill_model import FillModel
from vnedge.paper.simulated_exchange import PaperOrderRequest, SimulatedExchange

SLIP = 2 / 10_000
FEE = 5 / 10_000
SYM = "BTC/USDT:USDT"


def make_exchange(**fill_overrides) -> SimulatedExchange:
    ex = SimulatedExchange(FillModel(**fill_overrides), starting_balance_usd=1_000.0)
    ex.set_quote(SYM, bid=99.9, ask=100.1)
    return ex


def req(coid: str, buy: bool = True, qty: float = 1.0, **kw) -> PaperOrderRequest:
    return PaperOrderRequest(client_order_id=coid, symbol=SYM, buy=buy, quantity=qty, **kw)


def test_market_buy_fills_pessimistically():
    ex = make_exchange()
    status = ex.submit_order(req("o1"))
    assert status.state == "filled"
    assert status.avg_fill_price == pytest.approx(100.1 * (1 + SLIP))  # ask + slip
    fill = ex.get_fills()[0]
    assert fill.fee_usd == pytest.approx(status.avg_fill_price * FEE)
    assert ex.get_balances()["USDT"] == pytest.approx(1_000.0 - fill.fee_usd)
    pos = ex.get_positions()[0]
    assert pos.quantity == pytest.approx(1.0) and pos.side == "long"


def test_market_sell_fills_at_bid_minus_slip():
    ex = make_exchange()
    status = ex.submit_order(req("o1", buy=False))
    assert status.avg_fill_price == pytest.approx(99.9 * (1 - SLIP))
    assert ex.get_positions()[0].side == "short"


def test_close_realizes_pnl_to_balance():
    ex = make_exchange()
    ex.submit_order(req("o1", buy=True, qty=2.0))
    entry = ex.get_positions()[0].entry_price
    ex.set_quote(SYM, bid=109.9, ask=110.1)
    ex.submit_order(req("o2", buy=False, qty=2.0, reduce_only=True))
    assert ex.get_positions() == []  # flat
    exit_price = ex.get_fills()[-1].price
    expected_pnl = 2.0 * (exit_price - entry)
    fees = sum(f.fee_usd for f in ex.get_fills())
    assert ex.get_balances()["USDT"] == pytest.approx(1_000.0 + expected_pnl - fees)


def test_reduce_only_clamps_to_position():
    ex = make_exchange()
    ex.submit_order(req("o1", buy=True, qty=1.0))
    status = ex.submit_order(req("o2", buy=False, qty=5.0, reduce_only=True))
    assert status.filled_qty == pytest.approx(1.0)  # clamped, never over-closes
    assert ex.get_positions() == []


def test_reduce_only_rejected_when_flat():
    ex = make_exchange()
    status = ex.submit_order(req("o1", buy=False, reduce_only=True))
    assert status.state == "rejected"
    assert "no opposing position" in status.reason


def test_deterministic_partial_fill():
    ex = make_exchange(partial_fill_fraction=0.5)
    status = ex.submit_order(req("o1", qty=2.0))
    assert status.state == "partially_filled"
    assert status.filled_qty == pytest.approx(1.0)
    assert ex.get_positions()[0].quantity == pytest.approx(1.0)


def test_duplicate_client_order_id_is_idempotent():
    ex = make_exchange()
    first = ex.submit_order(req("o1"))
    second = ex.submit_order(req("o1"))
    assert second is first
    assert len(ex.get_fills()) == 1  # no double booking


def test_limit_buy_rests_until_price_crosses():
    ex = make_exchange()
    status = ex.submit_order(req("o1", order_type="limit", limit_price=95.0))
    assert status.state == "open"
    assert len(ex.get_open_orders()) == 1
    ex.set_quote(SYM, bid=94.8, ask=94.9)  # ask crossed below limit
    assert status.state == "filled"
    assert status.avg_fill_price == pytest.approx(95.0)  # at limit, no improvement


def test_limit_sell_fills_when_bid_crosses_up():
    ex = make_exchange()
    status = ex.submit_order(req("o1", buy=False, order_type="limit", limit_price=105.0))
    ex.set_quote(SYM, bid=105.2, ask=105.4)
    assert status.state == "filled"
    assert status.avg_fill_price == pytest.approx(105.0)


def test_cancel_resting_limit():
    ex = make_exchange()
    ex.submit_order(req("o1", order_type="limit", limit_price=95.0))
    status = ex.cancel_order("o1")
    assert status.state == "cancelled"
    ex.set_quote(SYM, bid=94.0, ask=94.1)
    assert status.state == "cancelled"  # cancelled orders never fill
    assert ex.get_fills() == []


def test_no_market_data_rejected():
    ex = SimulatedExchange(FillModel())
    status = ex.submit_order(req("o1"))
    assert status.state == "rejected"
    assert "no market data" in status.reason


def test_position_flip_through_zero():
    ex = make_exchange()
    ex.submit_order(req("o1", buy=True, qty=1.0))
    ex.submit_order(req("o2", buy=False, qty=3.0))  # close 1, open 2 short
    pos = ex.get_positions()[0]
    assert pos.side == "short"
    assert pos.quantity == pytest.approx(-2.0)
    assert pos.entry_price == pytest.approx(99.9 * (1 - SLIP))  # fresh entry


# --- maker vs taker fee routing (fill-type aware) --------------------------------

MAKER_FEE = 2 / 10_000


def test_resting_limit_filled_by_later_quote_pays_maker_fee():
    # A buy limit BELOW the ask rests (provides liquidity); when a later quote
    # drops to cross it, that is a MAKER fill -> maker fee, not taker.
    ex = make_exchange()
    status = ex.submit_order(req("m1", buy=True, order_type="limit", limit_price=99.5))
    assert status.state == "open"  # rested, not marketable on arrival
    ex.set_quote(SYM, bid=99.3, ask=99.4)  # ask 99.4 <= 99.5 -> crosses, maker fill
    filled = ex.get_order_status("m1")
    assert filled.state == "filled" and filled.avg_fill_price == pytest.approx(99.5)
    fill = ex.get_fills()[0]
    assert fill.fee_usd == pytest.approx(99.5 * MAKER_FEE)  # maker, not 99.5 * FEE


def test_marketable_limit_on_arrival_pays_taker_fee():
    # A buy limit AT/ABOVE the ask crosses on arrival (removes liquidity): taker.
    # It fills at its own limit price (no imaginary improvement).
    ex = make_exchange()
    status = ex.submit_order(req("t1", buy=True, order_type="limit", limit_price=100.5))
    assert status.state == "filled"
    fill = ex.get_fills()[0]
    assert fill.price == pytest.approx(100.5)
    assert fill.fee_usd == pytest.approx(100.5 * FEE)  # taker (marketable on arrival)


def test_market_order_is_taker():
    ex = make_exchange()
    ex.submit_order(req("k1", buy=True))
    fill = ex.get_fills()[0]
    assert fill.fee_usd == pytest.approx(fill.price * FEE)  # taker
