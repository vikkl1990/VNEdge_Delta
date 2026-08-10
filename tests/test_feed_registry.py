"""Shared feed registry — fan-out, refcounted lifecycle, view isolation.

No network: a fake feed with the LiveMarketFeed surface stands in for the
real thing, so these tests pin the multiplexing contract: one real feed per
(exchange, symbol, timeframe), every closed candle delivered to EVERY view,
and the last view release stopping the real feed.
"""

import asyncio
from datetime import UTC, datetime

import pytest

from vnedge.exchange.feed_registry import SharedFeedRegistry
from vnedge.risk.risk_manager import MarketState


class FakeFeed:
    """Stands in for LiveMarketFeed/RestPollingMarketFeed in the registry."""

    def __init__(self, exchange_id: str, *, symbol: str, timeframe: str = "1m") -> None:
        self.exchange_id = exchange_id
        self.symbol = symbol
        self.timeframe = timeframe
        self.feed_mode = "fake ws"
        self.slippage_est_bps = 2.0
        self.closed_candles: asyncio.Queue[list] = asyncio.Queue()
        self.quote: tuple[float, float] | None = (100.0, 101.0)
        self.funding_rate = 0.0001
        self.funding_events = [(1_000, 0.0001)]
        self.book_metrics = {"spread_bps": 1.0}
        self.healthy = True
        self.last_event_at = datetime.now(UTC)
        self.candles_closed = 0
        self.start_calls = 0
        self.stop_calls = 0

    async def start(self) -> None:
        self.start_calls += 1

    async def stop(self) -> None:
        self.stop_calls += 1

    def staleness_seconds(self, now=None) -> float:
        return 1.5

    def market_state(self) -> MarketState:
        return MarketState(
            symbol=self.symbol,
            last_update=self.last_event_at,
            spread_bps=1.0,
            estimated_slippage_bps=self.slippage_est_bps,
            funding_rate=self.funding_rate,
            exchange_healthy=self.healthy,
        )


def make_registry():
    created: list[FakeFeed] = []

    def factory(exchange_id, *, symbol, timeframe="1m"):
        feed = FakeFeed(exchange_id, symbol=symbol, timeframe=timeframe)
        created.append(feed)
        return feed

    return SharedFeedRegistry(feed_factory=factory), created


async def drain(view, n, timeout=1.0):
    return [
        await asyncio.wait_for(view.closed_candles.get(), timeout=timeout)
        for _ in range(n)
    ]


async def test_fan_out_delivers_every_candle_to_all_views():
    registry, created = make_registry()
    a = registry.acquire("binanceusdm", symbol="BTC/USDT:USDT", timeframe="1h")
    b = registry.acquire("binanceusdm", symbol="BTC/USDT:USDT", timeframe="1h")

    # same key -> ONE real feed, created once, started once
    assert len(created) == 1
    await a.start()
    await b.start()
    assert created[0].start_calls == 1

    candles = [[1_000, 1, 2, 0.5, 1.5, 10], [2_000, 1.5, 3, 1, 2, 11], [3_000, 2, 4, 2, 3, 12]]
    for row in candles:
        created[0].closed_candles.put_nowait(row)

    # BOTH views receive EVERY candle, in order — no competition on one queue
    assert await drain(a, 3) == candles
    assert await drain(b, 3) == candles
    assert a.candles_closed == 3
    assert b.candles_closed == 3

    await a.stop()
    await b.stop()


async def test_refcounted_stop_only_last_release_stops_the_feed():
    registry, created = make_registry()
    a = registry.acquire("bybit", symbol="BTC/USDT:USDT", timeframe="1h")
    b = registry.acquire("bybit", symbol="BTC/USDT:USDT", timeframe="1h")
    await a.start()
    await b.start()

    await a.stop()
    assert created[0].stop_calls == 0  # b still consumes the shared feed
    assert registry.active_feeds() == {("bybit", "BTC/USDT:USDT", "1h"): 1}

    await b.stop()
    assert created[0].stop_calls == 1  # last release stops the real feed
    assert registry.active_feeds() == {}

    # next acquire builds a FRESH feed, not the stopped one
    c = registry.acquire("bybit", symbol="BTC/USDT:USDT", timeframe="1h")
    assert len(created) == 2
    await c.stop()
    assert created[1].stop_calls == 1


async def test_stopped_view_is_isolated_from_the_shared_stream():
    registry, created = make_registry()
    a = registry.acquire("binanceusdm", symbol="ETH/USDT:USDT", timeframe="1h")
    b = registry.acquire("binanceusdm", symbol="ETH/USDT:USDT", timeframe="1h")
    await a.start()
    await b.start()

    await a.stop()
    created[0].closed_candles.put_nowait([1_000, 1, 2, 0.5, 1.5, 10])

    assert await drain(b, 1) == [[1_000, 1, 2, 0.5, 1.5, 10]]
    assert a.closed_candles.empty()  # released view no longer receives
    assert a.candles_closed == 0

    # stop is idempotent and never double-releases the refcount
    await a.stop()
    assert created[0].stop_calls == 0
    with pytest.raises(RuntimeError, match="released"):
        await a.start()

    await b.stop()
    assert created[0].stop_calls == 1


async def test_views_proxy_shared_state_and_report_sharing():
    registry, created = make_registry()
    a = registry.acquire("binanceusdm", symbol="BTC/USDT:USDT", timeframe="1h")
    assert a.feed_mode == "fake ws"  # single view: plain mode
    b = registry.acquire("binanceusdm", symbol="BTC/USDT:USDT", timeframe="1h")

    feed = created[0]
    assert a.exchange_id == "binanceusdm"
    assert a.symbol == "BTC/USDT:USDT"
    assert a.timeframe == "1h"
    assert a.quote == feed.quote
    assert a.funding_rate == feed.funding_rate
    assert a.funding_events == feed.funding_events
    assert a.book_metrics == feed.book_metrics
    assert a.healthy is True
    assert a.last_event_at == feed.last_event_at
    assert a.staleness_seconds() == 1.5
    assert a.market_state() == feed.market_state()
    assert b.feed_mode == "fake ws, shared x2"

    # shared state mutates in ONE place and every view sees it
    feed.quote = (200.0, 201.0)
    assert a.quote == b.quote == (200.0, 201.0)

    await a.stop()
    await b.stop()


async def test_different_keys_build_independent_feeds():
    registry, created = make_registry()
    a = registry.acquire("binanceusdm", symbol="BTC/USDT:USDT", timeframe="1h")
    b = registry.acquire("binanceusdm", symbol="ETH/USDT:USDT", timeframe="1h")
    c = registry.acquire("binanceusdm", symbol="BTC/USDT:USDT", timeframe="1m")
    d = registry.acquire("bybit", symbol="BTC/USDT:USDT", timeframe="1h")

    assert len(created) == 4  # symbol, timeframe and exchange all key the feed
    assert len(registry.active_feeds()) == 4
    for view in (a, b, c, d):
        await view.stop()
    assert registry.active_feeds() == {}
    assert all(feed.stop_calls == 1 for feed in created)


async def test_build_lane_uses_the_shared_registry(monkeypatch, tmp_path):
    """multi_lane.build_lane must acquire feeds through the registry."""
    import vnedge.runtime.multi_lane as ml

    seen = []

    def fake_acquire(exchange_id, *, symbol, timeframe="1m"):
        seen.append((exchange_id, symbol, timeframe))
        raise RuntimeError("stop here — wiring verified")

    monkeypatch.setattr(ml, "acquire_market_feed", fake_acquire)

    # skip the network warmup by faking the REST client context
    class FakeRest:
        def __init__(self, exchange):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def fetch_candles(self, *a, **k):
            return []

        async def fetch_funding_history(self, *a, **k):
            return []

    monkeypatch.setattr(ml, "CcxtPublicClient", FakeRest)

    spec = ml.LaneSpec(lane_id="x", exchange="bybit", symbol="BTC/USDT:USDT",
                       timeframe="1h")
    provider = ml.MultiLaneProvider("x")

    with pytest.raises(RuntimeError, match="wiring verified"):
        await ml.build_lane(spec, provider, journal_dir=tmp_path)
    assert seen == [("bybit", "BTC/USDT:USDT", "1h")]
