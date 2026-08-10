"""Live market data feeds.

Public data only — no credentials, no orders, no risk decisions. This module
produces exactly two things for the trading loop:

- a queue of CLOSED candles (a forming candle is emitted only when the next
  interval's first update proves it closed — the live equivalent of the
  backtester's bar-close discipline)
- a fresh MarketState (quote-derived spread, last-known funding via periodic
  REST refresh, and honest staleness: `last_update` is the wall-clock time
  of the last websocket event, so the gateway's data-freshness check fails
  naturally when the stream stalls)

Failure posture: errors mark the feed unhealthy and retry with bounded
backoff. An unhealthy or stale feed doesn't need to block anything itself —
the risk gateway already rejects on `exchange_healthy`/`data_freshness`,
which is where that decision belongs.

CCXT Pro is preferred for low-latency websocket venues. Some venues in the
architecture (notably Delta in current CCXT) expose public REST data but no
CCXT Pro websocket class; those use ``RestPollingMarketFeed`` so the lane can
still be observed in paper/shadow without pretending to be a fast path.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from vnedge.data.ccxt_client import create_ccxt_async_exchange
from vnedge.data.schemas import TIMEFRAME_MS
from vnedge.risk.risk_manager import MarketState

logger = logging.getLogger(__name__)

_MAX_CONSECUTIVE_ERRORS = 5
_BACKOFF_SECONDS = 2.0
_DEFAULT_REST_CANDLE_POLL_SECONDS = 10.0
_DEFAULT_REST_QUOTE_POLL_SECONDS = 2.0
_DEFAULT_WS_FALLBACK_POLL_SECONDS = 10.0
_DEFAULT_WS_STALE_SECONDS = 30.0
_VALIDATED_CCXT_PRO_FEEDS = {"binanceusdm", "bybit"}


class LiveMarketFeed:
    def __init__(
        self,
        exchange_id: str = "binanceusdm",
        *,
        symbol: str,
        timeframe: str = "1m",
        slippage_est_bps: float = 2.0,
        funding_refresh_seconds: float = 900.0,
        fallback_poll_seconds: float = _DEFAULT_WS_FALLBACK_POLL_SECONDS,
        ws_stale_seconds: float = _DEFAULT_WS_STALE_SECONDS,
    ) -> None:
        import ccxt.pro as ccxtpro  # heavy import kept local

        if not hasattr(ccxtpro, exchange_id):
            raise ValueError(f"unknown CCXT Pro exchange id: {exchange_id}")
        self._ex = getattr(ccxtpro, exchange_id)({"enableRateLimit": True})
        self.exchange_id = exchange_id
        self.feed_mode = "live ws (rest fallback)"
        self.symbol = symbol
        self.timeframe = timeframe
        self.slippage_est_bps = slippage_est_bps
        self.funding_refresh_seconds = funding_refresh_seconds
        self.fallback_poll_seconds = fallback_poll_seconds
        self.ws_stale_seconds = ws_stale_seconds

        self.closed_candles: asyncio.Queue[list] = asyncio.Queue()
        self.quote: tuple[float, float] | None = None  # (bid, ask)
        self.funding_rate: float = 0.0
        # SETTLED funding prints [(ts_ms, rate), ...] refreshed with the rate.
        # Strategies validated on settled-print series (funding-MR) must read
        # THIS, not funding_rate: the predicted rate is a different series
        # than research used, and mixing them silently shifts percentiles.
        self.funding_events: list[tuple[int, float]] = []
        self.book_metrics: dict | None = None  # live L2 metrics (fast loop)
        self.last_event_at: datetime | None = None
        self.healthy: bool = False
        self.candles_closed = 0
        self._consecutive_errors = 0
        self._forming: list | None = None
        self._last_emitted_candle_ts: int | None = None
        self._tasks: list[asyncio.Task] = []

    # --- Lifecycle ----------------------------------------------------------------
    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._watch_candles(), name="feed-candles"),
            asyncio.create_task(self._refresh_funding(), name="feed-funding"),
            asyncio.create_task(self._watch_book(), name="feed-book"),
            asyncio.create_task(self._rest_watchdog(), name="feed-rest-watchdog"),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._ex.close()

    # --- Health ---------------------------------------------------------------------
    def _mark_ok(self) -> None:
        self.last_event_at = datetime.now(UTC)
        self._consecutive_errors = 0
        self.healthy = True

    def _mark_error(self, where: str, exc: Exception) -> None:
        self._consecutive_errors += 1
        if self._consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
            self.healthy = False
        logger.warning("live feed %s error (%d consecutive): %s",
                       where, self._consecutive_errors, exc)

    def staleness_seconds(self, now: datetime | None = None) -> float:
        if self.last_event_at is None:
            return float("inf")
        return ((now or datetime.now(UTC)) - self.last_event_at).total_seconds()

    def market_state(self) -> MarketState:
        if self.quote is not None:
            bid, ask = self.quote
            spread_bps = (ask - bid) / ((ask + bid) / 2.0) * 10_000.0
        else:
            spread_bps = float("inf")  # no quote yet -> gateway rejects on spread
        return MarketState(
            symbol=self.symbol,
            last_update=self.last_event_at or datetime(1970, 1, 1, tzinfo=UTC),
            spread_bps=spread_bps,
            estimated_slippage_bps=self.slippage_est_bps,
            funding_rate=self.funding_rate,
            exchange_healthy=self.healthy,
        )

    # --- Stream loops -----------------------------------------------------------------
    async def _watch_candles(self) -> None:
        while True:
            try:
                rows = await self._ex.watch_ohlcv(self.symbol, self.timeframe)
                self._mark_ok()
                for row in rows:
                    if self._forming is None:
                        self._forming = row
                    elif row[0] > self._forming[0]:
                        # a newer interval started: the forming candle is closed
                        self._emit_closed(self._forming)
                        self._forming = row
                    else:
                        self._forming = row  # same interval, updated values
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — reconnect with backoff
                self._mark_error("candles", exc)
                await asyncio.sleep(_BACKOFF_SECONDS)

    async def _watch_quotes(self) -> None:
        # Top-of-book via watch_order_book: some venues' ticker streams
        # (e.g. Binance USDT-M 24h ticker) carry no bid/ask at all.
        # limit=50 is the common depth both Binance and Bybit accept for swaps
        # (Bybit rejects 5: only {1,50,200,1000}). We only read level 0.
        while True:
            try:
                book = await self._ex.watch_order_book(self.symbol, limit=50)
                if book["bids"] and book["asks"]:
                    bid = float(book["bids"][0][0])
                    ask = float(book["asks"][0][0])
                    if 0 < bid <= ask:
                        self.quote = (bid, ask)
                        self._mark_ok()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._mark_error("quotes", exc)
                await asyncio.sleep(_BACKOFF_SECONDS)

    async def _refresh_funding(self) -> None:
        while True:
            try:
                data = await self._ex.fetch_funding_rate(self.symbol)
                rate = data.get("fundingRate")
                if rate is not None:
                    self.funding_rate = float(rate)
                await _refresh_funding_events(self)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._mark_error("funding", exc)
            await asyncio.sleep(self.funding_refresh_seconds)

    async def _watch_book(self) -> None:
        """Single L2 subscription for quotes, health and fast-loop metrics.

        Uses the venue-safe depth limit (Bybit rejects anything but
        {1,50,200,1000}).  Top-of-book previously had a second concurrent
        ``watch_order_book`` consumer on the same CCXT Pro client.  That
        duplicate subscription caused intermittent ping timeouts and stale
        quotes.  One stream now owns both outputs.
        """
        while True:
            try:
                ob = await self._ex.watch_order_book(self.symbol, limit=50)
                self._apply_order_book(ob)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._mark_error("book", exc)
                await asyncio.sleep(_BACKOFF_SECONDS)

    def _apply_order_book(self, ob: dict) -> bool:
        """Apply one valid book snapshot to quote, metrics and feed health."""
        bids, asks = ob.get("bids"), ob.get("asks")
        if not bids or not asks:
            return False
        bid, ask = float(bids[0][0]), float(asks[0][0])
        if bid <= 0 or ask < bid:
            return False
        self.quote = (bid, ask)
        self.book_metrics = compute_book_metrics(self.symbol, bids, asks)
        self._mark_ok()
        return True

    def _emit_closed(self, row: list) -> bool:
        """Emit a closed candle once across websocket and REST fallback."""
        ts = int(row[0])
        if (
            self._last_emitted_candle_ts is not None
            and ts <= self._last_emitted_candle_ts
        ):
            return False
        self.closed_candles.put_nowait(list(row))
        self._last_emitted_candle_ts = ts
        self.candles_closed += 1
        return True

    async def _rest_watchdog(self) -> None:
        """Keep public paper data moving while a websocket is stale.

        This is intentionally a safety/continuity fallback, not a latency path:
        it activates only after the websocket has been stale for 30 seconds and
        emits only a fully closed candle through the same monotonic dedup gate.
        """
        while True:
            if self.staleness_seconds() >= self.ws_stale_seconds:
                await self._poll_rest_fallback_once()
            await asyncio.sleep(self.fallback_poll_seconds)

    async def _poll_rest_fallback_once(self) -> None:
        step_ms = TIMEFRAME_MS[self.timeframe]
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        try:
            rows = await self._ex.fetch_ohlcv(
                self.symbol,
                self.timeframe,
                since=now_ms - 4 * step_ms,
                limit=4,
            )
            closed = RestPollingMarketFeed._latest_closed_row(rows, now_ms, step_ms)
            if closed is not None:
                self._emit_closed(closed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._mark_error("rest-candles-fallback", exc)
        try:
            book = await self._ex.fetch_order_book(self.symbol, limit=50)
            self._apply_order_book(book)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._mark_error("rest-book-fallback", exc)


def compute_book_metrics(symbol: str, bids: list, asks: list) -> dict | None:
    """Flatten a bids/asks ladder into the fast-loop L2 metrics dict."""
    from vnedge.scalping.depth import OrderBookL2

    try:
        book = OrderBookL2(
            symbol=symbol,
            bids=tuple((float(p), float(q)) for p, q, *_ in bids[:10]),
            asks=tuple((float(p), float(q)) for p, q, *_ in asks[:10]),
            event_time=datetime.now(UTC),
        )
    except (ValueError, TypeError):
        return None  # crossed/empty snapshot — keep the last good metrics
    return {
        "spread_bps": round(book.spread_bps, 4),
        "imbalance": round(book.depth_imbalance(), 4),
        "liq_usd_5bps": round(book.liquidity_usd_within_bps(5.0), 0),
        "ts": datetime.now(UTC).isoformat(),
    }


async def _refresh_funding_events(feed) -> None:
    """Refresh a feed's SETTLED funding prints (``funding_events``).

    Best effort: venues without funding history simply keep an empty list
    (their strategies use live accumulation instead). Only recent prints are
    needed — the seed history covers the deep past; this keeps the tail fresh
    so the live series matches the research construction print-for-print.
    """
    ex = feed._ex
    if not ex.has.get("fetchFundingRateHistory"):
        return
    rows = await ex.fetch_funding_rate_history(feed.symbol, limit=10)
    events: list[tuple[int, float]] = []
    for row in rows or []:
        ts, rate = row.get("timestamp"), row.get("fundingRate")
        if ts is not None and rate is not None:
            events.append((int(ts), float(rate)))
    if events:
        feed.funding_events = sorted(events)


class RestPollingMarketFeed:
    """Public REST fallback feed for venues without CCXT Pro websocket support.

    This is an observability/shadow bridge, not a scalping feed. It polls
    top-of-book and OHLCV, emits only closed candles, and keeps the same
    surface as ``LiveMarketFeed`` so the runner remains single-path.
    """

    def __init__(
        self,
        exchange_id: str,
        *,
        symbol: str,
        timeframe: str = "1m",
        slippage_est_bps: float = 3.0,
        candle_poll_seconds: float = _DEFAULT_REST_CANDLE_POLL_SECONDS,
        quote_poll_seconds: float = _DEFAULT_REST_QUOTE_POLL_SECONDS,
        funding_refresh_seconds: float = 900.0,
    ) -> None:
        if timeframe not in TIMEFRAME_MS:
            raise ValueError(f"unsupported timeframe for REST polling feed: {timeframe}")
        self._ex = create_ccxt_async_exchange(exchange_id)
        self.exchange_id = exchange_id
        self.feed_mode = "rest polling"
        self.symbol = symbol
        self.timeframe = timeframe
        self.slippage_est_bps = slippage_est_bps
        self.candle_poll_seconds = candle_poll_seconds
        self.quote_poll_seconds = quote_poll_seconds
        self.funding_refresh_seconds = funding_refresh_seconds

        self.closed_candles: asyncio.Queue[list] = asyncio.Queue()
        self.quote: tuple[float, float] | None = None
        self.funding_rate: float = 0.0
        self.funding_events: list[tuple[int, float]] = []  # settled prints (ts_ms, rate)
        self.book_metrics: dict | None = None  # L2 metrics (native-ws subclasses)
        self.last_event_at: datetime | None = None
        self.healthy: bool = False
        self.candles_closed = 0
        self._consecutive_errors = 0
        self._last_emitted_candle_ts: int | None = None
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._poll_candles(), name="rest-feed-candles"),
            asyncio.create_task(self._poll_quotes(), name="rest-feed-quotes"),
            asyncio.create_task(self._refresh_funding(), name="rest-feed-funding"),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._ex.close()

    def _mark_ok(self) -> None:
        self.last_event_at = datetime.now(UTC)
        self._consecutive_errors = 0
        self.healthy = True

    def _mark_error(self, where: str, exc: Exception) -> None:
        self._consecutive_errors += 1
        if self._consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
            self.healthy = False
        logger.warning("REST feed %s %s error (%d consecutive): %s",
                       self.exchange_id, where, self._consecutive_errors, exc)

    def staleness_seconds(self, now: datetime | None = None) -> float:
        if self.last_event_at is None:
            return float("inf")
        return ((now or datetime.now(UTC)) - self.last_event_at).total_seconds()

    def market_state(self) -> MarketState:
        if self.quote is not None:
            bid, ask = self.quote
            spread_bps = (ask - bid) / ((ask + bid) / 2.0) * 10_000.0
        else:
            spread_bps = float("inf")
        return MarketState(
            symbol=self.symbol,
            last_update=self.last_event_at or datetime(1970, 1, 1, tzinfo=UTC),
            spread_bps=spread_bps,
            estimated_slippage_bps=self.slippage_est_bps,
            funding_rate=self.funding_rate,
            exchange_healthy=self.healthy,
        )

    async def _poll_candles(self) -> None:
        while True:
            await self._poll_candles_once()
            await asyncio.sleep(self.candle_poll_seconds)

    async def _poll_candles_once(self) -> None:
        """One REST poll: fetch recent bars, emit the latest CLOSED one (if new)."""
        step_ms = TIMEFRAME_MS[self.timeframe]
        try:
            now_ms = int(datetime.now(UTC).timestamp() * 1000)
            since = now_ms - 4 * step_ms
            rows = await self._ex.fetch_ohlcv(
                self.symbol, self.timeframe, since=since, limit=4
            )
            closed = self._latest_closed_row(rows, now_ms, step_ms)
            if closed is not None:
                self._emit_closed(closed)
            if rows:
                self._mark_ok()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._mark_error("candles", exc)
            await asyncio.sleep(_BACKOFF_SECONDS)

    def _emit_closed(self, row: list) -> bool:
        """Queue a closed candle if it is newer than the last emitted one.

        The monotonic guard deduplicates across sources (e.g. a websocket
        candle stream and its REST fallback emitting the same bar).
        """
        ts = int(row[0])
        if self._last_emitted_candle_ts is not None and ts <= self._last_emitted_candle_ts:
            return False
        self.closed_candles.put_nowait(list(row))
        self._last_emitted_candle_ts = ts
        self.candles_closed += 1
        return True

    @staticmethod
    def _latest_closed_row(rows: list[list], now_ms: int, step_ms: int) -> list | None:
        closed = [row for row in rows if int(row[0]) + step_ms <= now_ms]
        if not closed:
            return None
        return closed[-1]

    async def _poll_quotes(self) -> None:
        while True:
            try:
                book = await self._ex.fetch_order_book(self.symbol, limit=5)
                if book.get("bids") and book.get("asks"):
                    bid = float(book["bids"][0][0])
                    ask = float(book["asks"][0][0])
                    if 0 < bid <= ask:
                        self.quote = (bid, ask)
                        self._mark_ok()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._mark_error("quotes", exc)
                await asyncio.sleep(_BACKOFF_SECONDS)
            await asyncio.sleep(self.quote_poll_seconds)

    async def _refresh_funding(self) -> None:
        while True:
            try:
                if self._ex.has.get("fetchFundingRate"):
                    data = await self._ex.fetch_funding_rate(self.symbol)
                    rate = data.get("fundingRate")
                    if rate is not None:
                        self.funding_rate = float(rate)
                await _refresh_funding_events(self)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._mark_error("funding", exc)
            await asyncio.sleep(self.funding_refresh_seconds)


class DeltaWsFeed(RestPollingMarketFeed):
    """Delta India feed: NATIVE websocket candles/quotes/funding, REST fallback.

    Delta has no CCXT Pro class, but its native public websocket
    (``DeltaPublicWsClient``) pushes everything the lane needs: top-of-book
    from ``l2_orderbook``, funding from the ``funding_rate`` channel, and
    closed candles from the ``candlestick_<timeframe>`` channel (verified
    live 2026-07-08 — the channel streams the forming candle; the client
    emits it as closed when a newer ``candle_start_time`` appears, the same
    bar-close discipline as ``LiveMarketFeed``).

    REST candle polling remains as a FALLBACK only: it emits a closed bar
    when the websocket has not delivered one for 2x the timeframe (or has
    not delivered any yet, e.g. right after startup mid-interval). The
    monotonic ``_emit_closed`` guard deduplicates across the two sources.
    Staleness mirrors the last websocket event, so the gateway's freshness
    check reflects the real stream.
    """

    def __init__(
        self,
        exchange_id: str,
        *,
        symbol: str,
        timeframe: str = "1m",
        slippage_est_bps: float = 3.0,
        candle_poll_seconds: float = _DEFAULT_REST_CANDLE_POLL_SECONDS,
    ) -> None:
        super().__init__(
            exchange_id,
            symbol=symbol,
            timeframe=timeframe,
            slippage_est_bps=slippage_est_bps,
            candle_poll_seconds=candle_poll_seconds,
        )
        from vnedge.exchange.delta_ws import DeltaPublicWsClient, delta_native_symbol

        self.feed_mode = "delta native ws candles (rest fallback)"
        self._native_symbol = delta_native_symbol(symbol)
        self._last_ws_candle_at: datetime | None = None
        self._ws = DeltaPublicWsClient(
            [self._native_symbol],
            candle_timeframes=(timeframe,),
            on_candle=self._on_ws_candle,
        )

    async def start(self) -> None:
        await self._ws.start()
        self._tasks = [
            asyncio.create_task(self._poll_candles(), name="delta-feed-candles"),
            asyncio.create_task(self._sync_ws_state(), name="delta-feed-ws-sync"),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._ws.stop()
        await self._ex.close()

    # --- websocket candles with REST fallback ------------------------------------
    def _on_ws_candle(self, sym: str, timeframe: str, row: list) -> None:
        """A candle CLOSED on the native websocket stream."""
        if sym != self._native_symbol or timeframe != self.timeframe:
            return
        self._last_ws_candle_at = datetime.now(UTC)
        self._emit_closed(row)

    def _ws_candles_fresh(self, now: datetime | None = None) -> bool:
        """Websocket candle stream considered alive: a close within 2x timeframe."""
        if self._last_ws_candle_at is None:
            return False
        age = ((now or datetime.now(UTC)) - self._last_ws_candle_at).total_seconds()
        return age < 2.0 * (TIMEFRAME_MS[self.timeframe] / 1000.0)

    async def _poll_candles(self) -> None:
        # FALLBACK-only loop: while websocket candles flow, REST stays quiet.
        while True:
            if not self._ws_candles_fresh():
                await self._poll_candles_once()
            await asyncio.sleep(self.candle_poll_seconds)

    async def _sync_ws_state(self) -> None:
        """Mirror native websocket state into the polling-feed surface."""
        while True:
            try:
                quote = self._ws.quote(self._native_symbol)
                if quote is not None:
                    self.quote = quote
                fr = self._ws.funding_rate.get(self._native_symbol)
                if fr is not None:
                    self.funding_rate = fr
                book = self._ws.books.get(self._native_symbol)
                if book:
                    buy, sell = book
                    metrics = compute_book_metrics(
                        self.symbol,
                        [[e["limit_price"], e["size"]] for e in buy[:10]],
                        [[e["limit_price"], e["size"]] for e in sell[:10]],
                    )
                    if metrics is not None:
                        self.book_metrics = metrics
                # honest staleness/health: track the real stream, not this loop
                if self._ws.last_event_at is not None:
                    self.last_event_at = self._ws.last_event_at
                    self.healthy = self._ws.healthy
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._mark_error("ws-sync", exc)
            await asyncio.sleep(0.5)


def supports_ccxt_pro_feed(exchange_id: str) -> bool:
    """Whether this CCXT id has the websocket methods our live feed needs."""
    try:
        import ccxt.pro as ccxtpro  # heavy import kept local
    except Exception:  # pragma: no cover - import failure is environment-specific
        return False
    return exchange_id in _VALIDATED_CCXT_PRO_FEEDS and hasattr(ccxtpro, exchange_id)


_DELTA_NATIVE_WS_IDS = {"delta_india", "delta", "deltaindia"}


def create_market_feed(
    exchange_id: str,
    *,
    symbol: str,
    timeframe: str = "1m",
) -> LiveMarketFeed | RestPollingMarketFeed:
    if supports_ccxt_pro_feed(exchange_id):
        return LiveMarketFeed(exchange_id, symbol=symbol, timeframe=timeframe)
    if exchange_id in _DELTA_NATIVE_WS_IDS:
        # Delta has no CCXT Pro class but does have a native public websocket.
        return DeltaWsFeed(exchange_id, symbol=symbol, timeframe=timeframe)
    return RestPollingMarketFeed(exchange_id, symbol=symbol, timeframe=timeframe)
