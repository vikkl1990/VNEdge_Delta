"""Shared market-feed registry: one real feed per (exchange, symbol, timeframe).

Lanes watching the same market used to each build their own feed — duplicate
websocket connections to the same venue for identical data (e.g. the governed
Binance BTC paper lane and its shadow twin). The registry deduplicates:
``acquire()`` creates the real feed on first use and hands every caller a
lightweight :class:`SharedFeedView`.

Each view owns its OWN closed-candle queue. A fan-out task drains the real
feed's queue and copies every closed candle into ALL registered view queues,
so every lane sees every candle — lanes never compete for items on a single
queue. Everything else (quote, funding, book metrics, staleness, market
state) is read-only shared state and proxies straight through to the real
feed; that is safe in the single-process asyncio design.

Lifecycle is refcounted: ``view.start()`` starts the real feed exactly once;
``view.stop()`` releases the view, and the LAST release stops the real feed
(and forgets the entry, so a later acquire builds a fresh one). A stopped
view stops receiving candles immediately, without disturbing its siblings.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime

from vnedge.exchange.live_feed import (
    LiveMarketFeed,
    RestPollingMarketFeed,
    create_market_feed,
)
from vnedge.risk.risk_manager import MarketState

logger = logging.getLogger(__name__)

FeedKey = tuple[str, str, str]  # (exchange_id, symbol, timeframe)
_Feed = LiveMarketFeed | RestPollingMarketFeed
FeedFactory = Callable[..., _Feed]


class SharedFeedView:
    """One lane's handle on a shared feed.

    Keeps the ``LiveMarketFeed`` surface the runners consume: an exclusive
    ``closed_candles`` queue (fed by the registry fan-out) plus read-through
    proxies for the shared market state.
    """

    def __init__(self, registry: "SharedFeedRegistry", entry: "_FeedEntry") -> None:
        self._registry = registry
        self._entry = entry
        self.closed_candles: asyncio.Queue[list] = asyncio.Queue()
        self.candles_closed = 0  # candles delivered to THIS view
        self._stopped = False

    # --- lifecycle (refcounted through the registry) -------------------------------
    async def start(self) -> None:
        if self._stopped:
            raise RuntimeError("cannot start a released SharedFeedView")
        await self._registry._start_entry(self._entry)

    async def stop(self) -> None:
        if self._stopped:
            return  # idempotent: runners stop defensively
        self._stopped = True
        await self._registry._release(self._entry, self)

    def _deliver(self, row: list) -> None:
        self.closed_candles.put_nowait(row)
        self.candles_closed += 1

    # --- shared read-through state --------------------------------------------------
    @property
    def _feed(self) -> _Feed:
        return self._entry.feed

    @property
    def exchange_id(self) -> str:
        return self._feed.exchange_id

    @property
    def symbol(self) -> str:
        return self._feed.symbol

    @property
    def timeframe(self) -> str:
        return self._feed.timeframe

    @property
    def feed_mode(self) -> str:
        mode = self._feed.feed_mode
        n = len(self._entry.views)
        return mode if n <= 1 else f"{mode}, shared x{n}"

    @property
    def slippage_est_bps(self) -> float:
        return self._feed.slippage_est_bps

    @property
    def quote(self) -> tuple[float, float] | None:
        return self._feed.quote

    @property
    def funding_rate(self) -> float:
        return self._feed.funding_rate

    @property
    def funding_events(self) -> list[tuple[int, float]]:
        return self._feed.funding_events

    @property
    def book_metrics(self) -> dict | None:
        return self._feed.book_metrics

    @property
    def healthy(self) -> bool:
        return self._feed.healthy

    @property
    def last_event_at(self) -> datetime | None:
        return self._feed.last_event_at

    def staleness_seconds(self, now: datetime | None = None) -> float:
        return self._feed.staleness_seconds(now)

    def market_state(self) -> MarketState:
        return self._feed.market_state()


class _FeedEntry:
    """Registry bookkeeping for one real feed: views, fan-out task, start state."""

    def __init__(self, key: FeedKey, feed: _Feed) -> None:
        self.key = key
        self.feed = feed
        self.views: list[SharedFeedView] = []
        self.fanout_task: asyncio.Task | None = None
        self.started = False
        self.start_lock = asyncio.Lock()


class SharedFeedRegistry:
    """Refcounted registry of market feeds keyed by (exchange, symbol, timeframe)."""

    def __init__(self, feed_factory: FeedFactory | None = None) -> None:
        self._factory: FeedFactory = feed_factory or create_market_feed
        self._entries: dict[FeedKey, _FeedEntry] = {}

    def acquire(
        self, exchange_id: str, *, symbol: str, timeframe: str = "1m"
    ) -> SharedFeedView:
        """Get a view of the shared feed for this market, creating it if needed."""
        key: FeedKey = (exchange_id, symbol, timeframe)
        entry = self._entries.get(key)
        if entry is None:
            feed = self._factory(exchange_id, symbol=symbol, timeframe=timeframe)
            entry = _FeedEntry(key, feed)
            self._entries[key] = entry
            logger.info("feed registry: created shared feed for %s", key)
        else:
            logger.info(
                "feed registry: reusing shared feed for %s (now %d views)",
                key, len(entry.views) + 1,
            )
        view = SharedFeedView(self, entry)
        entry.views.append(view)
        return view

    def active_feeds(self) -> dict[FeedKey, int]:
        """Live view counts per feed key (observability/tests)."""
        return {key: len(entry.views) for key, entry in self._entries.items()}

    # --- internal lifecycle ----------------------------------------------------------
    async def _start_entry(self, entry: _FeedEntry) -> None:
        async with entry.start_lock:
            if entry.started:
                return
            await entry.feed.start()
            entry.fanout_task = asyncio.create_task(
                self._fan_out(entry), name=f"feed-fanout-{'-'.join(entry.key)}"
            )
            entry.started = True

    async def _fan_out(self, entry: _FeedEntry) -> None:
        """Copy every closed candle from the real feed to EVERY view queue."""
        while True:
            row = await entry.feed.closed_candles.get()
            for view in list(entry.views):
                view._deliver(row)

    async def _release(self, entry: _FeedEntry, view: SharedFeedView) -> None:
        if view in entry.views:
            entry.views.remove(view)
        if entry.views:
            return  # other lanes still consume this feed
        # last view released: tear the real feed down and forget the entry
        self._entries.pop(entry.key, None)
        if entry.fanout_task is not None:
            entry.fanout_task.cancel()
            await asyncio.gather(entry.fanout_task, return_exceptions=True)
            entry.fanout_task = None
        entry.started = False
        await entry.feed.stop()
        logger.info("feed registry: stopped shared feed for %s (last view released)",
                    entry.key)


# Default process-wide registry: lanes built by different call sites still share
# feeds because the v1 architecture is a single asyncio process.
_DEFAULT_REGISTRY = SharedFeedRegistry()


def shared_feed_registry() -> SharedFeedRegistry:
    return _DEFAULT_REGISTRY


def acquire_market_feed(
    exchange_id: str, *, symbol: str, timeframe: str = "1m"
) -> SharedFeedView:
    """Acquire a shared-feed view from the default process-wide registry."""
    return _DEFAULT_REGISTRY.acquire(exchange_id, symbol=symbol, timeframe=timeframe)
