"""Market feed factory — websocket where possible, REST fallback where needed."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from vnedge.data.ccxt_client import create_ccxt_async_exchange, resolve_ccxt_exchange_id
from vnedge.exchange.live_feed import (
    DeltaWsFeed,
    LiveMarketFeed,
    RestPollingMarketFeed,
    create_market_feed,
    supports_ccxt_pro_feed,
)


@pytest.mark.network
async def test_delta_india_uses_india_rest_host():
    assert resolve_ccxt_exchange_id("delta_india") == "delta"
    exchange = create_ccxt_async_exchange("delta_india")
    try:
        assert exchange.urls["api"]["public"] == "https://api.india.delta.exchange"
        assert exchange.urls["api"]["private"] == "https://api.india.delta.exchange"
    finally:
        await exchange.close()


@pytest.mark.network
async def test_feed_factory_routes_validated_websocket_venues():
    assert supports_ccxt_pro_feed("binanceusdm") is True
    assert supports_ccxt_pro_feed("bybit") is True

    binance = create_market_feed("binanceusdm", symbol="BTC/USDT:USDT")
    bybit = create_market_feed("bybit", symbol="BTC/USDT:USDT")

    try:
        assert isinstance(binance, LiveMarketFeed)
        assert isinstance(bybit, LiveMarketFeed)
    finally:
        await binance.stop()
        await bybit.stop()


@pytest.mark.network
async def test_feed_factory_routes_delta_to_native_websocket():
    # Delta has no CCXT Pro class, but it does have a native public websocket.
    assert supports_ccxt_pro_feed("delta_india") is False

    feed = create_market_feed("delta_india", symbol="BTC/USD:USD")
    try:
        assert isinstance(feed, DeltaWsFeed)
        # DeltaWsFeed keeps the RestPollingMarketFeed surface (candles via REST).
        assert isinstance(feed, RestPollingMarketFeed)
        assert feed.exchange_id == "delta_india"
        assert feed._native_symbol == "BTCUSD"
        assert "native ws" in feed.feed_mode
    finally:
        await feed.stop()


@pytest.mark.network
async def test_delta_ws_candles_emit_closed_bars_with_monotonic_dedup():
    feed = create_market_feed("delta_india", symbol="BTC/USD:USD", timeframe="1h")
    try:
        assert isinstance(feed, DeltaWsFeed)
        # the native ws client subscribes the candle channel for OUR timeframe
        assert "candlestick_1h" in feed._ws.channels
        assert feed._ws_candles_fresh() is False  # nothing from ws yet

        row = [1_000, 1.0, 2.0, 0.5, 1.5, 10.0]
        feed._on_ws_candle("BTCUSD", "1h", row)
        assert feed.closed_candles.get_nowait() == row
        assert feed.candles_closed == 1
        assert feed._ws_candles_fresh() is True

        # same bar again (e.g. the REST fallback catching up) is deduplicated
        assert feed._emit_closed(row) is False
        # other symbols/timeframes never leak into this feed's queue
        feed._on_ws_candle("ETHUSD", "1h", [3_601_000, 1, 1, 1, 1, 1])
        feed._on_ws_candle("BTCUSD", "1m", [3_601_000, 1, 1, 1, 1, 1])
        assert feed.closed_candles.empty()

        # the next interval's close flows through
        nxt = [3_601_000, 1.5, 2.5, 1.0, 2.0, 11.0]
        feed._on_ws_candle("BTCUSD", "1h", nxt)
        assert feed.closed_candles.get_nowait() == nxt
        assert feed.candles_closed == 2
    finally:
        await feed.stop()


class _FakeRestExchange:
    has: dict = {}

    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    async def fetch_ohlcv(self, symbol, timeframe, since=None, limit=4):
        self.calls += 1
        return self.rows

    async def close(self):
        pass


@pytest.mark.network
async def test_delta_rest_candle_fallback_only_when_ws_is_stale():
    feed = create_market_feed("delta_india", symbol="BTC/USD:USD", timeframe="1m")
    step_ms = 60_000
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    closed_ts = (now_ms // step_ms - 1) * step_ms  # latest fully closed minute
    fake = _FakeRestExchange([
        [closed_ts, 1.0, 2.0, 0.5, 1.5, 10.0],
        [closed_ts + step_ms, 1.5, 1.6, 1.4, 1.5, 2.0],  # forming, never emitted
    ])
    await feed._ex.close()
    feed._ex = fake
    feed.candle_poll_seconds = 0.01
    try:
        # ws candles fresh -> the fallback loop never touches REST
        feed._last_ws_candle_at = datetime.now(UTC)
        task = asyncio.create_task(feed._poll_candles())
        await asyncio.sleep(0.08)
        assert fake.calls == 0
        assert feed.closed_candles.empty()

        # no ws candle for 2x the timeframe -> REST emits the closed bar
        feed._last_ws_candle_at = datetime.now(UTC) - timedelta(seconds=121)
        for _ in range(100):
            if fake.calls:
                break
            await asyncio.sleep(0.01)
        assert fake.calls >= 1
        emitted = await asyncio.wait_for(feed.closed_candles.get(), timeout=1.0)
        assert emitted[0] == closed_ts

        # repeat polls do not re-emit the same bar (monotonic guard)
        calls_before = fake.calls
        for _ in range(100):
            if fake.calls > calls_before:
                break
            await asyncio.sleep(0.01)
        assert feed.closed_candles.empty()

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    finally:
        await feed.stop()


def test_rest_polling_latest_closed_row_uses_only_closed_candles():
    now_ms = 10_000
    step_ms = 1_000
    rows = [
        [7_000, 1.0, 1.0, 1.0, 1.0, 1.0],
        [8_000, 2.0, 2.0, 2.0, 2.0, 2.0],
        [9_500, 3.0, 3.0, 3.0, 3.0, 3.0],
    ]

    assert RestPollingMarketFeed._latest_closed_row(rows, now_ms, step_ms) == rows[1]


@pytest.mark.network
async def test_live_feed_uses_one_book_update_for_quote_metrics_and_health():
    feed = LiveMarketFeed("binanceusdm", symbol="BTC/USDT:USDT")
    try:
        assert feed._apply_order_book({
            "bids": [[100.0, 2.0], [99.5, 3.0]],
            "asks": [[100.5, 2.5], [101.0, 4.0]],
        }) is True
        assert feed.quote == (100.0, 100.5)
        assert feed.book_metrics is not None
        assert feed.book_metrics["spread_bps"] > 0
        assert feed.healthy is True
        assert feed.last_event_at is not None
    finally:
        await feed.stop()


@pytest.mark.network
async def test_live_feed_rejects_invalid_book_without_overwriting_quote():
    feed = LiveMarketFeed("binanceusdm", symbol="BTC/USDT:USDT")
    try:
        feed.quote = (100.0, 100.5)
        assert feed._apply_order_book({"bids": [], "asks": []}) is False
        assert feed._apply_order_book({
            "bids": [[101.0, 1.0]], "asks": [[100.0, 1.0]],
        }) is False
        assert feed.quote == (100.0, 100.5)
    finally:
        await feed.stop()


class _WsFallbackExchange:
    def __init__(self, rows, book):
        self.rows = rows
        self.book = book
        self.ohlcv_calls = 0
        self.book_calls = 0

    async def fetch_ohlcv(self, *args, **kwargs):
        self.ohlcv_calls += 1
        return self.rows

    async def fetch_order_book(self, *args, **kwargs):
        self.book_calls += 1
        return self.book

    async def close(self):
        pass


@pytest.mark.network
async def test_live_feed_rest_watchdog_recovers_quote_and_closed_candle():
    feed = LiveMarketFeed("binanceusdm", symbol="BTC/USDT:USDT", timeframe="1m")
    step_ms = 60_000
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    closed_ts = (now_ms // step_ms - 1) * step_ms
    fake = _WsFallbackExchange(
        [
            [closed_ts, 100.0, 101.0, 99.0, 100.5, 10.0],
            [closed_ts + step_ms, 100.5, 102.0, 100.0, 101.0, 2.0],
        ],
        {"bids": [[100.0, 2.0]], "asks": [[100.5, 3.0]]},
    )
    await feed._ex.close()
    feed._ex = fake
    try:
        await feed._poll_rest_fallback_once()
        emitted = feed.closed_candles.get_nowait()
        assert emitted[0] == closed_ts
        assert feed.quote == (100.0, 100.5)
        assert feed.healthy is True
        assert fake.ohlcv_calls == fake.book_calls == 1

        # A repeat fallback poll cannot duplicate the same candle.
        await feed._poll_rest_fallback_once()
        assert feed.closed_candles.empty()
    finally:
        await feed.stop()


class _HistExchange:
    """Fake ccxt exchange: settled funding history support."""
    has = {"fetchFundingRateHistory": True}

    async def fetch_funding_rate_history(self, symbol, limit=10):
        return [
            {"timestamp": 2_000, "fundingRate": "0.0002"},
            {"timestamp": 1_000, "fundingRate": "0.0001"},
            {"timestamp": None, "fundingRate": "0.9"},      # dropped
            {"timestamp": 3_000, "fundingRate": None},       # dropped
        ]


class _NoHistExchange:
    has = {"fetchFundingRateHistory": False}


async def test_refresh_funding_events_populates_sorted_settled_prints():
    from vnedge.exchange.live_feed import _refresh_funding_events

    class Feed:
        _ex = _HistExchange()
        symbol = "BTC/USDT:USDT"
        funding_events: list = []

    feed = Feed()
    await _refresh_funding_events(feed)
    assert feed.funding_events == [(1_000, 0.0001), (2_000, 0.0002)]


async def test_refresh_funding_events_noop_without_venue_support():
    from vnedge.exchange.live_feed import _refresh_funding_events

    class Feed:
        _ex = _NoHistExchange()
        symbol = "BTC/USD:USD"
        funding_events: list = []

    feed = Feed()
    await _refresh_funding_events(feed)
    assert feed.funding_events == []
