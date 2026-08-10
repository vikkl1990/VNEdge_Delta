"""Native Delta public websocket client — protocol parsing and stream loop.

No network: a fake connect factory replays canned Delta messages, so these
tests pin the exact message schemas confirmed against the live venue.
"""

import asyncio
import json

from vnedge.exchange.delta_ws import DeltaPublicWsClient, delta_native_symbol


def test_native_symbol_conversion():
    assert delta_native_symbol("BTC/USD:USD") == "BTCUSD"
    assert delta_native_symbol("ETH/USD:USD") == "ETHUSD"
    assert delta_native_symbol("BTCUSD") == "BTCUSD"


def test_handle_l2_orderbook_sets_top_of_book():
    client = DeltaPublicWsClient(["BTC/USD:USD"])
    client._handle(
        {
            "type": "l2_orderbook",
            "symbol": "BTCUSD",
            "buy": [
                {"limit_price": "62697.5", "size": 1762, "depth": "1762"},
                {"limit_price": "62697.0", "size": 10},
            ],
            "sell": [
                {"limit_price": "62698.0", "size": 5},
                {"limit_price": "62698.5", "size": 20},
            ],
        }
    )
    assert client.best_bid["BTCUSD"] == 62697.5
    assert client.best_ask["BTCUSD"] == 62698.0
    assert client.quote("BTC/USD:USD") == (62697.5, 62698.0)
    assert client.last_event_at is not None
    assert client.healthy is True


def test_handle_all_trades_taker_side():
    client = DeltaPublicWsClient(["BTCUSD"])
    seen = []
    client.on_trade = lambda sym, t: seen.append(t)

    client._handle(
        {
            "type": "all_trades",
            "symbol": "BTCUSD",
            "size": 3,
            "price": "62644.5",
            "buyer_role": "maker",
            "seller_role": "taker",
            "timestamp": 1_720_000_000_000_000,  # microseconds
        }
    )
    trade = client.last_trade["BTCUSD"]
    assert trade["price"] == 62644.5
    assert trade["size"] == 3.0
    assert trade["side"] == "sell"  # seller is the taker/aggressor
    assert trade["ts_ms"] == 1_720_000_000_000  # us -> ms
    assert seen == [trade]

    # buyer as taker => buy print
    client._handle(
        {
            "type": "all_trades",
            "symbol": "BTCUSD",
            "size": 1,
            "price": "62645.0",
            "buyer_role": "taker",
            "seller_role": "maker",
            "timestamp": 1_720_000_001_000_000,
        }
    )
    assert client.last_trade["BTCUSD"]["side"] == "buy"


def test_handle_funding_rate_normalises_percent_to_fraction():
    client = DeltaPublicWsClient(["BTCUSD"])
    client._handle(
        {
            "type": "funding_rate",
            "symbol": "BTCUSD",
            "funding_rate": 0.01,  # percent
            "funding_interval": 28800,
            "funding_rate_8h": 0.01,
        }
    )
    # 0.01% -> 0.0001 fraction, matching CCXT's fundingRate convention
    assert client.funding_rate["BTCUSD"] == 0.0001


def test_handle_ticker_captures_mark_price():
    client = DeltaPublicWsClient(["BTCUSD"])
    client._handle(
        {"type": "v2/ticker", "symbol": "BTCUSD", "mark_price": "62637.53", "close": 62642.5}
    )
    assert client.mark_price["BTCUSD"] == 62637.53


def test_handle_ignores_unknown_and_malformed():
    client = DeltaPublicWsClient(["BTCUSD"])
    client._handle({"type": "subscriptions", "channels": []})
    client._handle({"type": "heartbeat"})
    client._handle_raw("not json")
    client._handle_raw(b'{"type":"l2_orderbook","symbol":"BTCUSD","buy":[],"sell":[]}')
    # empty book: no top-of-book, but the event still counts as liveness
    assert "BTCUSD" not in client.best_bid
    assert client.quote("BTCUSD") is None


def _candle_msg(start_us, close, *, volume=10.0, resolution="1h"):
    return {
        "type": f"candlestick_{resolution}",
        "symbol": "BTCUSD",
        "resolution": resolution,
        "open": 100.0,
        "high": 110.0,
        "low": 90.0,
        "close": close,
        "volume": volume,
        "candle_start_time": start_us,  # microseconds (confirmed live)
        "timestamp": start_us + 1,
        "last_updated": start_us + 1,
    }


def test_candlestick_close_discipline_emits_prior_candle_once():
    closed = []
    client = DeltaPublicWsClient(
        ["BTC/USD:USD"], candle_timeframes=("1h",),
        on_candle=lambda sym, tf, row: closed.append((sym, tf, row)),
    )
    # candle channels are appended to the subscription list
    assert "candlestick_1h" in client.channels

    hour_us = 3_600_000_000
    t0 = 1_783_530_000_000_000
    # forming updates for the same interval: nothing closes yet
    client._handle(_candle_msg(t0, 101.0, volume=5.0))
    client._handle(_candle_msg(t0, 102.5, volume=8.0))
    assert closed == []
    assert client.last_closed_candle == {}

    # a newer candle_start_time proves the prior candle closed
    client._handle(_candle_msg(t0 + hour_us, 103.0, volume=1.0))
    assert len(closed) == 1
    sym, tf, row = closed[0]
    assert (sym, tf) == ("BTCUSD", "1h")
    # LAST forming values win, start time is microseconds -> ms
    assert row == [t0 // 1000, 100.0, 110.0, 90.0, 102.5, 8.0]
    assert client.last_closed_candle[("BTCUSD", "1h")] == row
    assert client.healthy is True

    # replayed/out-of-order message for the OLD interval never re-closes it
    client._handle(_candle_msg(t0, 999.0))
    client._handle(_candle_msg(t0 + hour_us, 104.0, volume=2.0))
    assert len(closed) == 1  # same interval update, no new close
    assert client._forming_candles[("BTCUSD", "1h")][4] == 104.0


def test_candlestick_tracks_resolutions_independently():
    closed = []
    client = DeltaPublicWsClient(
        ["BTCUSD"], candle_timeframes=("1m", "1h"),
        on_candle=lambda sym, tf, row: closed.append(tf),
    )
    minute_us = 60_000_000
    t0 = 1_783_532_640_000_000
    client._handle(_candle_msg(t0, 101.0, resolution="1m"))
    client._handle(_candle_msg(t0, 101.0, resolution="1h"))
    # the 1m candle rolls over; the 1h candle is still forming
    client._handle(_candle_msg(t0 + minute_us, 102.0, resolution="1m"))
    assert closed == ["1m"]


def test_candlestick_malformed_messages_are_ignored():
    closed = []
    client = DeltaPublicWsClient(
        ["BTCUSD"], candle_timeframes=("1h",),
        on_candle=lambda sym, tf, row: closed.append(row),
    )
    client._handle({"type": "candlestick_1h", "symbol": "BTCUSD"})  # no fields
    client._handle(_candle_msg(1_000_000_000, None))  # close=None
    client._handle({"type": "candlestick_1h", "resolution": "1h"})  # no symbol
    assert closed == []
    assert client._forming_candles == {}


class _FakeWs:
    """Minimal async websocket: records sends, replays canned frames, then ends."""

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


async def test_reader_loop_consumes_stream_and_subscribes():
    frames = [
        json.dumps({"type": "subscriptions", "channels": []}),
        json.dumps(
            {
                "type": "l2_orderbook",
                "symbol": "BTCUSD",
                "buy": [{"limit_price": "100.0", "size": 1}],
                "sell": [{"limit_price": "101.0", "size": 1}],
            }
        ),
        json.dumps(
            {
                "type": "funding_rate",
                "symbol": "BTCUSD",
                "funding_rate": 0.02,
            }
        ),
    ]
    fake = _FakeWs(frames)
    client = DeltaPublicWsClient(["BTC/USD:USD"], connect=lambda url: fake)

    await client.start()
    # let the reader task drain the fake stream
    for _ in range(50):
        if client.best_bid.get("BTCUSD") and client.funding_rate.get("BTCUSD"):
            break
        await asyncio.sleep(0.01)
    await client.stop()

    # subscribe message was sent with all default channels
    sub = fake.sent[0]
    assert sub["type"] == "subscribe"
    names = {c["name"] for c in sub["payload"]["channels"]}
    assert {"l2_orderbook", "all_trades", "funding_rate", "v2/ticker"} <= names
    assert all(c["symbols"] == ["BTCUSD"] for c in sub["payload"]["channels"])

    assert client.quote("BTCUSD") == (100.0, 101.0)
    assert client.funding_rate["BTCUSD"] == 0.0002
