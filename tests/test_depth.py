"""L2 depth primitive — validation, depth features, book-walk, loader."""

import pandas as pd
import pytest

from vnedge.exchange.tick_recorder import _book_row
from vnedge.scalping.depth import OrderBookL2, load_l2_books

SYM = "BTC/USDT:USDT"
T0 = 1_751_000_000_000


def book(bids, asks, ts_ms=T0):
    from datetime import UTC, datetime
    return OrderBookL2(SYM, tuple(bids), tuple(asks),
                       datetime.fromtimestamp(ts_ms / 1000, tz=UTC))


def test_basics_and_l1_view():
    b = book([(100.0, 2.0), (99.9, 5.0)], [(100.2, 1.0), (100.3, 4.0)])
    assert b.best_bid == 100.0 and b.best_ask == 100.2
    assert b.mid_price == pytest.approx(100.1)
    assert b.spread_bps == pytest.approx((0.2 / 100.1) * 10_000)
    top = b.top_of_book()               # level-0 L1 view for the v1 engine
    assert top.bid == 100.0 and top.bid_size == 2.0 and top.ask_size == 1.0


def test_rejects_crossed_and_unordered_books():
    from datetime import UTC, datetime
    now = datetime.now(UTC)
    with pytest.raises(ValueError):     # crossed
        OrderBookL2(SYM, ((100.2, 1.0),), ((100.0, 1.0),), now)
    with pytest.raises(ValueError):     # bids not descending
        OrderBookL2(SYM, ((100.0, 1.0), (100.1, 1.0)), ((100.2, 1.0),), now)


def test_depth_imbalance_multi_level():
    # level 0 balanced, but deeper bids dominate -> positive cumulative imbalance
    b = book([(100.0, 1.0), (99.9, 9.0)], [(100.2, 1.0), (100.3, 1.0)])
    assert b.depth_imbalance(levels=1) == pytest.approx(0.0)
    assert b.depth_imbalance() == pytest.approx((10.0 - 2.0) / 12.0)


def test_liquidity_within_bps():
    b = book([(100.0, 1.0), (95.0, 100.0)], [(100.2, 2.0), (105.0, 100.0)])
    # within 50 bps of mid (~100.1) only the touch levels qualify
    usd = b.liquidity_usd_within_bps(50)
    assert usd == pytest.approx(100.0 * 1.0 + 100.2 * 2.0)


def test_fill_walk_consumes_multiple_levels_with_slippage():
    b = book([(100.0, 10.0)], [(100.2, 1.0), (100.4, 100.0)])
    # buy $200: $100.2 clears the first level, remainder walks to 100.4
    w = b.fill_walk(200.0, "buy")
    assert w.fully_filled
    assert w.avg_price > 100.2 and w.avg_price < 100.4
    assert w.slippage_bps > 0                       # paid up vs mid
    assert w.filled_notional == pytest.approx(200.0)


def test_fill_walk_reports_partial_when_book_exhausted():
    b = book([(100.0, 1.0)], [(100.2, 1.0)])        # only ~$100 on the ask
    w = b.fill_walk(1_000.0, "buy")
    assert not w.fully_filled
    assert w.filled_notional == pytest.approx(100.2)


def test_from_row_parses_recorded_l2_and_drops_nan_levels():
    ob = {"bids": [[100.0 - i * 0.1, 1.0 + i] for i in range(4)],
          "asks": [[100.2 + i * 0.1, 2.0 + i] for i in range(4)]}
    row = _book_row(ob, levels=10, ts_ms=T0)        # levels 4..9 are NaN-padded
    b = OrderBookL2.from_row(row, SYM, levels=10)
    assert len(b.bids) == 4 and len(b.asks) == 4    # padded levels dropped
    assert b.best_bid == 100.0


def test_load_l2_books_from_recorded_shards(tmp_path):
    book_dir = (tmp_path / "ticks" / "exchange=binanceusdm"
                / "symbol=BTCUSDT" / "stream=book" / "20260704")
    book_dir.mkdir(parents=True)
    ob = {"bids": [[100.0 - i * 0.1, 5.0] for i in range(10)],
          "asks": [[100.2 + i * 0.1, 5.0] for i in range(10)]}
    pd.DataFrame([_book_row(ob, 10, T0), _book_row(ob, 10, T0 + 500)]
                 ).to_parquet(book_dir / "s.parquet")
    got = load_l2_books(tmp_path, "binanceusdm", SYM, "20260704")
    assert [ts for ts, _ in got] == [T0, T0 + 500]
    assert isinstance(got[0][1], OrderBookL2)
    assert len(got[0][1].bids) == 10


def test_load_l2_books_ignores_legacy_l1_only_day(tmp_path):
    base = (tmp_path / "ticks" / "exchange=binanceusdm"
            / "symbol=BTCUSDT" / "stream=book")
    base.mkdir(parents=True)
    # legacy L1 file has no bid_px_* columns -> no L2 books
    pd.DataFrame([{"ts_ms": T0, "bid": 100.0, "bid_qty": 5.0, "ask": 100.2, "ask_qty": 5.0}]
                 ).to_parquet(base / "20260704.parquet")
    assert load_l2_books(tmp_path, "binanceusdm", SYM, "20260704") == []


def test_fill_walk_validates_inputs():
    b = book([(100.0, 1.0)], [(100.2, 1.0)])
    with pytest.raises(ValueError):
        b.fill_walk(0.0, "buy")
    with pytest.raises(ValueError):
        b.fill_walk(100.0, "sideways")
