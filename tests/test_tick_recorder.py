"""Tick/L2 recorder — L2 schema, NaN padding, atomic sharded writes."""

import math

import pandas as pd
import pytest

from vnedge.exchange.tick_recorder import TickRecorder, _book_row, _Buffer

DAY_TS = 1_751_000_000_000  # fixed ms timestamp


def _ob(n_bid, n_ask):
    return {
        "bids": [[100.0 - i * 0.1, 1.0 + i] for i in range(n_bid)],
        "asks": [[101.0 + i * 0.1, 2.0 + i] for i in range(n_ask)],
    }


def test_book_row_captures_full_ladder_with_l1_aliases():
    row = _book_row(_ob(12, 12), levels=10, ts_ms=DAY_TS)
    # level-0 L1 aliases equal ladder level 0 (backward compat)
    assert row["bid"] == row["bid_px_0"] == 100.0
    assert row["ask"] == row["ask_px_0"] == 101.0
    assert row["bid_qty"] == row["bid_qty_0"] == 1.0
    # captured 10 levels; deepest is index 9; sliced there (no level 10)
    assert row["bid_px_9"] == pytest.approx(100.0 - 9 * 0.1)
    assert row["ask_px_9"] == pytest.approx(101.0 + 9 * 0.1)
    assert "bid_px_10" not in row


def test_book_row_pads_missing_levels_with_nan():
    row = _book_row(_ob(3, 3), levels=10, ts_ms=DAY_TS)
    assert row["bid_px_2"] == pytest.approx(100.0 - 2 * 0.1)
    assert math.isnan(row["bid_px_3"])   # only 3 levels available
    assert row["bid_qty_3"] == 0.0


def test_levels_must_be_positive(tmp_path):
    with pytest.raises(ValueError):
        TickRecorder("binanceusdm", ["BTC/USDT:USDT"], tmp_path, levels=0)


@pytest.mark.network
def test_book_limit_is_venue_safe():
    # 50 is the smallest depth Bybit swaps accept AND Binance USDT-M accepts;
    # limit=5 (the old value) errored on Bybit.
    rec = TickRecorder("binanceusdm", ["BTC/USDT:USDT"], "/tmp", levels=10)
    assert rec._book_limit == 50


def _book_dir(tmp_path, day):
    return (tmp_path / "ticks" / "exchange=binanceusdm"
            / "symbol=BTCUSDT" / "stream=book" / day)


def test_buffer_writes_atomic_shards_never_rewrites(tmp_path):
    buf = _Buffer(tmp_path, "binanceusdm", "BTC/USDT:USDT", "book")
    buf.add({"ts_ms": DAY_TS, "bid": 100.0})
    buf.flush(0.0)
    buf.add({"ts_ms": DAY_TS + 1000, "bid": 100.1})
    buf.flush(1.0)

    day = pd.to_datetime(DAY_TS, unit="ms", utc=True).strftime("%Y%m%d")
    shard_dir = _book_dir(tmp_path, day)
    shards = sorted(shard_dir.glob("*.parquet"))
    assert len(shards) == 2                         # two flushes -> two shards
    assert not list(shard_dir.glob(".*.tmp"))       # no leftover temp files
    # legacy single rewritten file is never produced
    assert not (shard_dir.parent / f"{day}.parquet").exists()
    df = pd.concat([pd.read_parquet(s) for s in shards], ignore_index=True)
    assert list(df["bid"]) == [100.0, 100.1]        # data intact, ordered


def test_empty_flush_is_a_noop(tmp_path):
    buf = _Buffer(tmp_path, "binanceusdm", "BTC/USDT:USDT", "book")
    assert buf.flush(0.0) == 0


# -- Delta native recorder -------------------------------------------------

import asyncio  # noqa: E402
import json  # noqa: E402

from vnedge.exchange.tick_recorder import DeltaTickRecorder, _delta_ob  # noqa: E402


def test_delta_ob_to_ccxt_book_shape():
    ob = _delta_ob(
        buy=[{"limit_price": "100.0", "size": 3}, {"limit_price": "99.5", "size": 5}],
        sell=[{"limit_price": "100.5", "size": 2}, {"limit_price": "101.0", "size": 8}],
    )
    assert ob["bids"] == [[100.0, 3.0], [99.5, 5.0]]  # descending
    assert ob["asks"] == [[100.5, 2.0], [101.0, 8.0]]  # ascending
    row = _book_row(ob, levels=2, ts_ms=DAY_TS)
    assert row["bid"] == 100.0 and row["ask"] == 100.5
    assert row["bid_px_1"] == 99.5 and row["ask_qty_1"] == 8.0


class _FakeWs:
    def __init__(self, frames):
        self._frames = frames
        self.sent = []

    async def send(self, data):
        self.sent.append(json.loads(data))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for f in self._frames:
            yield f
        # keep the socket "open" so the recorder's flush loop can run; the test
        # cancels run() to finish.
        await asyncio.Event().wait()


async def test_delta_recorder_writes_book_and_trade_shards(tmp_path):
    frames = [
        json.dumps({
            "type": "l2_orderbook", "symbol": "BTCUSD", "timestamp": DAY_TS * 1000,
            "buy": [{"limit_price": "62697.5", "size": 1762}, {"limit_price": "62697.0", "size": 10}],
            "sell": [{"limit_price": "62698.0", "size": 5}, {"limit_price": "62698.5", "size": 20}],
        }),
        json.dumps({
            "type": "all_trades", "symbol": "BTCUSD", "size": 3, "price": "62698.0",
            "buyer_role": "taker", "seller_role": "maker", "timestamp": DAY_TS * 1000,
        }),
    ]
    fake = _FakeWs(frames)
    rec = DeltaTickRecorder(
        ["BTC/USD:USD"], tmp_path, levels=2,
        connect=lambda url: fake, clock=lambda: 0.0,
    )
    task = asyncio.create_task(rec.run())
    for _ in range(100):
        if rec.book_count and rec.trade_count:
            break
        await asyncio.sleep(0.01)
    task.cancel()  # triggers final flush
    await asyncio.gather(task, return_exceptions=True)

    assert rec.book_count == 1 and rec.trade_count == 1
    day = pd.to_datetime(DAY_TS, unit="ms", utc=True).strftime("%Y%m%d")
    base = tmp_path / "ticks" / "exchange=delta_india" / "symbol=BTCUSD"
    book = pd.concat(
        [pd.read_parquet(s) for s in (base / "stream=book" / day).glob("*.parquet")],
        ignore_index=True,
    )
    assert book.loc[0, "bid"] == 62697.5 and book.loc[0, "ask"] == 62698.0
    assert book.loc[0, "bid_px_1"] == 62697.0  # full L2 ladder captured
    trades = pd.concat(
        [pd.read_parquet(s) for s in (base / "stream=trades" / day).glob("*.parquet")],
        ignore_index=True,
    )
    assert trades.loc[0, "price"] == 62698.0
    assert trades.loc[0, "amount"] == 3.0
    assert trades.loc[0, "side"] == "buy"  # buyer is the taker/aggressor
