"""Liquidation recorder — row schema, side normalisation, path selection,
sparse-safe buffer flush, native forceOrder fallback, CLI parsing."""

import asyncio
import json

import pandas as pd
import pytest

from vnedge.exchange.liquidation_recorder import (
    BinanceForceOrderRecorder,
    LiquidationRecorder,
    _binance_force_order_row,
    _forced_order_side,
    _liq_row,
    binance_stream_name,
    build_recorder,
    parse_args,
    select_path,
)

DAY_TS = 1_751_000_000_000  # fixed ms timestamp

ROW_COLUMNS = {"ts_ms", "price", "amount", "side", "notional_usd"}


def _ccxt_liq(**over):
    """A ccxt-shaped liquidation structure (binanceusdm ws parse output)."""
    liq = {
        "info": {"s": "BTCUSDT", "S": "SELL", "ap": "9910", "l": "0.014", "T": DAY_TS},
        "symbol": "BTC/USDT:USDT",
        "contracts": 0.014,
        "contractSize": 1.0,
        "price": 9910.0,
        "side": "sell",
        "baseValue": 0.014,
        "quoteValue": 138.74,
        "timestamp": DAY_TS,
    }
    liq.update(over)
    return liq


# -- row schema --------------------------------------------------------------

def test_liq_row_schema_binance():
    row = _liq_row(_ccxt_liq(), "binanceusdm")
    assert set(row) == ROW_COLUMNS
    assert row["ts_ms"] == DAY_TS
    assert row["price"] == 9910.0
    assert row["amount"] == 0.014
    assert row["side"] == "sell"  # binance S is already the forced order side
    assert row["notional_usd"] == pytest.approx(138.74)


def test_liq_row_bybit_flips_position_side_to_forced_order_side():
    # Bybit allLiquidation reports the POSITION side: "buy" == a long was
    # liquidated, whose forced order is a SELL. One convention on disk.
    liq = _ccxt_liq(side="buy", info={"S": "Buy"})
    assert _liq_row(liq, "bybit")["side"] == "sell"
    liq = _ccxt_liq(side="sell", info={"S": "Sell"})
    assert _liq_row(liq, "bybit")["side"] == "buy"


def test_forced_order_side_unknown_is_empty_never_guessed():
    assert _forced_order_side(None, "binanceusdm") == ""
    assert _forced_order_side("weird", "bybit") == ""


def test_liq_row_side_falls_back_to_info():
    row = _liq_row(_ccxt_liq(side=None), "binanceusdm")
    assert row["side"] == "sell"  # from info["S"]


def test_liq_row_amount_falls_back_to_contracts_and_notional_computed():
    liq = _ccxt_liq(baseValue=None, quoteValue=None)
    row = _liq_row(liq, "binanceusdm")
    assert row["amount"] == 0.014
    assert row["notional_usd"] == pytest.approx(9910.0 * 0.014)


def test_liq_row_without_price_or_amount_is_dropped():
    assert _liq_row(_ccxt_liq(price=None), "binanceusdm") is None
    assert _liq_row(_ccxt_liq(baseValue=None, contracts=None), "binanceusdm") is None


def test_liq_row_missing_timestamp_uses_fallback():
    row = _liq_row(_ccxt_liq(timestamp=None), "binanceusdm", fallback_ts_ms=DAY_TS + 5)
    assert row["ts_ms"] == DAY_TS + 5


# -- ccxt-vs-native path selection -------------------------------------------

def test_select_path_prefers_ccxt_when_supported():
    assert select_path("binanceusdm", True) == "ccxt"
    assert select_path("bybit", True) == "ccxt"


def test_select_path_binance_native_fallback():
    assert select_path("binanceusdm", False) == "native_binance"


def test_select_path_unservable_venue_fails_loudly():
    with pytest.raises(ValueError, match="bybit"):
        select_path("bybit", False)


class _FakeCcxtExchange:
    """Offline stand-in for a CCXT Pro exchange class/instance."""

    def __init__(self, options=None, *, supported=True, events=()):
        self.has = {"watchLiquidations": supported}
        self._events = list(events)
        self.closed = False

    async def watch_liquidations(self, symbol):
        if self._events:
            return [self._events.pop(0)]
        await asyncio.Event().wait()  # stream stays "open"; tests cancel run()

    async def close(self):
        self.closed = True


def _fake_ccxtpro(supported: bool):
    class _Pro:
        pass

    def _factory(options=None):
        return _FakeCcxtExchange(options, supported=supported)

    _Pro.binanceusdm = staticmethod(_factory)
    return _Pro


def test_build_recorder_uses_ccxt_path_when_supported(tmp_path):
    rec = build_recorder("binanceusdm", ["BTC/USDT:USDT"], tmp_path,
                         ccxtpro=_fake_ccxtpro(supported=True))
    assert isinstance(rec, LiquidationRecorder)


def test_build_recorder_falls_back_to_native_binance(tmp_path):
    rec = build_recorder("binanceusdm", ["BTC/USDT:USDT"], tmp_path,
                         ccxtpro=_fake_ccxtpro(supported=False))
    assert isinstance(rec, BinanceForceOrderRecorder)


def test_build_recorder_unknown_exchange_raises(tmp_path):
    with pytest.raises(ValueError, match="bybit"):
        build_recorder("bybit", ["BTC/USDT:USDT"], tmp_path,
                       ccxtpro=_fake_ccxtpro(supported=False))


def test_recorder_rejects_exchange_without_watch_liquidations(tmp_path):
    with pytest.raises(ValueError, match="watch_liquidations"):
        LiquidationRecorder("binanceusdm", ["BTC/USDT:USDT"], tmp_path,
                            exchange=_FakeCcxtExchange(supported=False))


# -- buffer flush with fake events (ccxt path) --------------------------------

def _shard_dir(tmp_path, exchange, day):
    return (tmp_path / "ticks" / f"exchange={exchange}"
            / "symbol=BTCUSDT" / "stream=liquidations" / day)


async def test_ccxt_recorder_flushes_liquidation_shards(tmp_path):
    events = [_ccxt_liq(), _ccxt_liq(timestamp=DAY_TS + 1000, price=9900.0)]
    fake = _FakeCcxtExchange(supported=True, events=events)
    rec = LiquidationRecorder("binanceusdm", ["BTC/USDT:USDT"], tmp_path, exchange=fake)

    clock_now = 0.0
    task = asyncio.create_task(rec.run(clock=lambda: clock_now))
    for _ in range(100):
        if rec.liquidation_count >= 2:
            break
        await asyncio.sleep(0.01)
    clock_now = 100.0  # exceed FLUSH_SECONDS so the cadence loop flushes
    for _ in range(100):
        day_dir = _shard_dir(tmp_path, "binanceusdm",
                             pd.to_datetime(DAY_TS, unit="ms", utc=True).strftime("%Y%m%d"))
        if day_dir.exists() and list(day_dir.glob("*.parquet")):
            break
        await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert rec.liquidation_count == 2
    assert fake.closed  # exchange connection released on shutdown
    day = pd.to_datetime(DAY_TS, unit="ms", utc=True).strftime("%Y%m%d")
    shard_dir = _shard_dir(tmp_path, "binanceusdm", day)
    shards = sorted(shard_dir.glob("*.parquet"))
    assert shards, "no liquidation shards written"
    assert not list(shard_dir.glob(".*.tmp"))  # atomic publish, no temp leftovers
    df = pd.concat([pd.read_parquet(s) for s in shards], ignore_index=True)
    assert set(df.columns) == ROW_COLUMNS
    assert list(df["price"]) == [9910.0, 9900.0]
    assert list(df["side"]) == ["sell", "sell"]


async def test_ccxt_recorder_final_flush_on_cancel(tmp_path):
    # a lone sparse event must survive shutdown even if no cadence flush ran
    fake = _FakeCcxtExchange(supported=True, events=[_ccxt_liq()])
    rec = LiquidationRecorder("binanceusdm", ["BTC/USDT:USDT"], tmp_path, exchange=fake)
    task = asyncio.create_task(rec.run(clock=lambda: 0.0))
    for _ in range(100):
        if rec.liquidation_count:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    day = pd.to_datetime(DAY_TS, unit="ms", utc=True).strftime("%Y%m%d")
    shards = list(_shard_dir(tmp_path, "binanceusdm", day).glob("*.parquet"))
    assert len(shards) == 1
    assert pd.read_parquet(shards[0]).loc[0, "notional_usd"] == pytest.approx(138.74)


# -- native Binance forceOrder fallback ---------------------------------------

def test_binance_stream_name():
    assert binance_stream_name("BTC/USDT:USDT") == "btcusdt"
    assert binance_stream_name("DOGE/USDT:USDT") == "dogeusdt"


def test_native_recorder_builds_combined_stream_url(tmp_path):
    rec = BinanceForceOrderRecorder(["BTC/USDT:USDT", "ETH/USDT:USDT"], tmp_path)
    assert rec.url.endswith("?streams=btcusdt@forceOrder/ethusdt@forceOrder")


def test_binance_force_order_row_maps_raw_payload():
    row = _binance_force_order_row(
        {"s": "BTCUSDT", "S": "SELL", "q": "0.014", "p": "9910",
         "ap": "9910", "X": "FILLED", "l": "0.014", "z": "0.014", "T": DAY_TS}
    )
    assert set(row) == ROW_COLUMNS
    assert row == {
        "ts_ms": DAY_TS, "price": 9910.0, "amount": 0.014,
        "side": "sell", "notional_usd": pytest.approx(9910.0 * 0.014),
    }


def test_binance_force_order_row_unparseable_is_dropped():
    assert _binance_force_order_row({"S": "SELL"}) is None


class _FakeWs:
    def __init__(self, frames):
        self._frames = frames

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for f in self._frames:
            yield f
        await asyncio.Event().wait()  # keep the socket "open"; tests cancel run()


async def test_native_recorder_records_force_order_events(tmp_path):
    frames = [
        json.dumps({  # combined-stream envelope
            "stream": "btcusdt@forceOrder",
            "data": {"e": "forceOrder", "E": DAY_TS,
                     "o": {"s": "BTCUSDT", "S": "BUY", "q": "2", "p": "100",
                           "ap": "101", "l": "2", "T": DAY_TS}},
        }),
        json.dumps({"stream": "btcusdt@aggTrade", "data": {"e": "aggTrade"}}),  # ignored
        "not json",  # ignored
        json.dumps({  # bare (single-stream) format also accepted
            "e": "forceOrder", "E": DAY_TS + 1000,
            "o": {"s": "BTCUSDT", "S": "SELL", "q": "1", "p": "99",
                  "ap": "98", "l": "1", "T": DAY_TS + 1000},
        }),
        json.dumps({  # unsubscribed symbol: ignored
            "e": "forceOrder", "E": DAY_TS,
            "o": {"s": "XRPUSDT", "S": "SELL", "q": "1", "p": "1", "ap": "1",
                  "l": "1", "T": DAY_TS},
        }),
    ]
    rec = BinanceForceOrderRecorder(
        ["BTC/USDT:USDT"], tmp_path, connect=lambda url: _FakeWs(frames)
    )
    task = asyncio.create_task(rec.run(clock=lambda: 0.0))
    for _ in range(100):
        if rec.liquidation_count >= 2:
            break
        await asyncio.sleep(0.01)
    task.cancel()  # triggers final flush
    await asyncio.gather(task, return_exceptions=True)

    assert rec.liquidation_count == 2
    day = pd.to_datetime(DAY_TS, unit="ms", utc=True).strftime("%Y%m%d")
    shards = list(_shard_dir(tmp_path, "binanceusdm", day).glob("*.parquet"))
    df = pd.concat([pd.read_parquet(s) for s in shards], ignore_index=True)
    assert set(df.columns) == ROW_COLUMNS
    assert list(df["side"]) == ["buy", "sell"]  # forced order side, verbatim
    assert list(df["price"]) == [101.0, 98.0]   # avg fill price preferred
    assert list(df["notional_usd"]) == [pytest.approx(202.0), pytest.approx(98.0)]


# -- CLI ----------------------------------------------------------------------

def test_parse_args_defaults():
    args = parse_args([])
    assert args.exchange == "binanceusdm"
    assert args.symbols == "BTC/USDT:USDT"
    assert args.data_root == "data"


def test_parse_args_explicit():
    args = parse_args([
        "--exchange", "bybit",
        "--symbols", "BTC/USDT:USDT, ETH/USDT:USDT,",
        "--data-root", "/app/data",
    ])
    assert args.exchange == "bybit"
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    assert symbols == ["BTC/USDT:USDT", "ETH/USDT:USDT"]
    assert args.data_root == "/app/data"
