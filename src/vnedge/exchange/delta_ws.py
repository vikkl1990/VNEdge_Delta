"""Native Delta Exchange India public websocket client.

CCXT (and CCXT Pro) has no websocket support for Delta Exchange, so the Delta
lane has run on a REST-polling feed. Delta's *native* public websocket, however,
streams exactly what we want in real time: full L2 order books, prints, current
funding, and mark price. This client speaks that native protocol.

Public data only. No authentication, no orders, no account channels — the same
posture as ``LiveMarketFeed``. It maintains live state (top-of-book, funding,
last trade, L2 book, mark price) that a feed wrapper reads, and exposes optional
``on_book`` / ``on_trade`` callbacks so a tick recorder can archive the raw
stream without a second connection.

Protocol (confirmed by live probing against wss://socket.india.delta.exchange):

- subscribe:   {"type":"subscribe","payload":{"channels":[
                   {"name":"l2_orderbook","symbols":["BTCUSD"]}, ...]}}
- l2_orderbook (full snapshot per message):
      {"type":"l2_orderbook","symbol":"BTCUSD",
       "buy":[{"limit_price":"62697.5","size":1762,"depth":"1762"}, ...],
       "sell":[{"limit_price":"62698.0","size":10,...}, ...]}
      buy is bids (descending), sell is asks (ascending). Prices are strings.
- all_trades:
      {"type":"all_trades","symbol":"BTCUSD","size":1,"price":"62644.5",
       "buyer_role":"maker","seller_role":"taker","timestamp":<microseconds>}
      taker side = whichever role == "taker".
- funding_rate (8h interval):
      {"type":"funding_rate","symbol":"BTCUSD","funding_rate":0.01,
       "funding_interval":28800,"funding_rate_8h":0.01,...}
      funding_rate is a PERCENT (0.01 == 0.01%); we normalise to a fraction
      (/100) so it matches CCXT's fundingRate convention used everywhere else.
- v2/ticker:
      {"type":"v2/ticker","symbol":"BTCUSD","mark_price":"62637.53",
       "close":62642.5,"oi":"1026.9130",...}
- candlestick_<tf> (tf in 1m/5m/15m/30m/1h/... — confirmed live 2026-07-08):
      {"type":"candlestick_1h","symbol":"BTCUSD","resolution":"1h",
       "open":62092,"high":62379.5,"low":62010,"close":62240.5,
       "volume":836519.0,"candle_start_time":1783530000000000,
       "timestamp":1783532686647151,"last_updated":1783532686647151,...}
      candle_start_time/timestamp are MICROSECONDS. The channel streams the
      FORMING candle repeatedly (sub-second cadence); there is no explicit
      "closed" flag. Closed-candle discipline therefore mirrors
      ``LiveMarketFeed._watch_candles``: when a message arrives with a newer
      ``candle_start_time``, the previously forming candle is proven closed
      and is emitted via ``on_candle``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

DELTA_INDIA_WS_URL = "wss://socket.india.delta.exchange"

DEFAULT_CHANNELS = ("l2_orderbook", "all_trades", "funding_rate", "v2/ticker")

_MAX_CONSECUTIVE_ERRORS = 5
_BACKOFF_SECONDS = 2.0


def delta_native_symbol(symbol: str) -> str:
    """Convert a CCXT-style Delta symbol to Delta's native ticker.

    ``BTC/USD:USD`` -> ``BTCUSD``. Already-native symbols pass through.
    """
    base = symbol.split(":", 1)[0]  # drop settlement suffix
    return base.replace("/", "").replace("-", "").upper()


class DeltaPublicWsClient:
    """Native public websocket client for Delta Exchange India.

    Reader task connects, subscribes to the requested public channels, and
    updates live per-symbol state. Reconnects with bounded backoff. All state
    is best-effort and last-known-value; freshness is exposed via
    ``last_event_at`` so callers can compute honest staleness.
    """

    def __init__(
        self,
        symbols: list[str],
        *,
        channels: tuple[str, ...] = DEFAULT_CHANNELS,
        candle_timeframes: tuple[str, ...] = (),
        url: str = DELTA_INDIA_WS_URL,
        connect: Callable[..., object] | None = None,
        on_book: Callable[[str, list, list, dict], None] | None = None,
        on_trade: Callable[[str, dict], None] | None = None,
        on_candle: Callable[[str, str, list], None] | None = None,
    ) -> None:
        self.symbols = [delta_native_symbol(s) for s in symbols]
        self.channels = tuple(channels) + tuple(
            f"candlestick_{tf}" for tf in candle_timeframes
        )
        self.url = url
        self._connect = connect  # injectable for tests; defaults to websockets.connect
        self.on_book = on_book
        self.on_trade = on_trade
        # on_candle(symbol, timeframe, [ts_ms, o, h, l, c, v]) — CLOSED candles only
        self.on_candle = on_candle

        # live per-symbol state (native symbol -> value)
        self.best_bid: dict[str, float] = {}
        self.best_ask: dict[str, float] = {}
        self.funding_rate: dict[str, float] = {}
        self.mark_price: dict[str, float] = {}
        self.books: dict[str, tuple[list, list]] = {}
        self.last_trade: dict[str, dict] = {}
        # candle state per (symbol, timeframe): forming candle + last closed
        self._forming_candles: dict[tuple[str, str], list] = {}
        self.last_closed_candle: dict[tuple[str, str], list] = {}

        self.last_event_at: datetime | None = None
        self.healthy: bool = False
        self._consecutive_errors = 0
        self._closed = False
        self._task: asyncio.Task | None = None

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        self._closed = False
        self._task = asyncio.create_task(self._run(), name="delta-ws-reader")

    async def stop(self) -> None:
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    def _subscribe_msg(self) -> dict:
        return {
            "type": "subscribe",
            "payload": {
                "channels": [
                    {"name": name, "symbols": list(self.symbols)}
                    for name in self.channels
                ]
            },
        }

    async def _run(self) -> None:
        connect = self._connect or _default_connect
        while not self._closed:
            try:
                async with connect(self.url) as ws:
                    await ws.send(json.dumps(self._subscribe_msg()))
                    # server drops idle sockets after ~60s; heartbeat keeps it up
                    try:
                        await ws.send(json.dumps({"type": "enable_heartbeat"}))
                    except Exception:  # noqa: BLE001 - heartbeat is best effort
                        pass
                    async for raw in ws:
                        self._handle_raw(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect on any stream error
                self._mark_error(exc)
            # normal stream-end or error: reconnect with bounded backoff so we
            # never hot-loop when the socket closes cleanly.
            if self._closed:
                break
            await asyncio.sleep(_BACKOFF_SECONDS)

    # -- message handling --------------------------------------------------
    def _handle_raw(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return
        if isinstance(msg, dict):
            self._handle(msg)

    def _handle(self, msg: dict) -> None:
        mtype = msg.get("type")
        sym = msg.get("symbol")
        if mtype == "l2_orderbook":
            self._handle_book(sym, msg)
        elif mtype == "all_trades":
            self._handle_trade(sym, msg)
        elif mtype == "funding_rate":
            self._handle_funding(sym, msg)
        elif mtype == "v2/ticker":
            self._handle_ticker(sym, msg)
        elif isinstance(mtype, str) and mtype.startswith("candlestick_"):
            self._handle_candle(sym, mtype, msg)
        # subscriptions / heartbeat / errors and unknown types are ignored

    def _handle_book(self, sym: str | None, msg: dict) -> None:
        if not sym:
            return
        buy = msg.get("buy") or []
        sell = msg.get("sell") or []
        if buy:
            self.best_bid[sym] = float(buy[0]["limit_price"])
        if sell:
            self.best_ask[sym] = float(sell[0]["limit_price"])
        self.books[sym] = (buy, sell)
        self._touch()
        if self.on_book is not None:
            self.on_book(sym, buy, sell, msg)

    def _handle_trade(self, sym: str | None, msg: dict) -> None:
        if not sym:
            return
        try:
            price = float(msg["price"])
            size = float(msg["size"])
        except (KeyError, TypeError, ValueError):
            return
        # taker side drives aggressor; buyer taker => buy print, seller taker => sell
        taker = "sell" if msg.get("seller_role") == "taker" else "buy"
        ts_raw = msg.get("timestamp")
        ts_ms = int(ts_raw) // 1000 if ts_raw is not None else self._now_ms()
        trade = {
            "symbol": sym,
            "price": price,
            "size": size,
            "side": taker,
            "ts_ms": ts_ms,
            "sequence": msg.get("sequence") or msg.get("sequence_number"),
        }
        self.last_trade[sym] = trade
        self._touch()
        if self.on_trade is not None:
            self.on_trade(sym, trade)

    def _handle_funding(self, sym: str | None, msg: dict) -> None:
        if not sym:
            return
        raw = msg.get("funding_rate")
        if raw is None:
            return
        try:
            # Delta reports funding as a percent; normalise to a fraction.
            self.funding_rate[sym] = float(raw) / 100.0
        except (TypeError, ValueError):
            return
        self._touch()

    def _handle_candle(self, sym: str | None, mtype: str, msg: dict) -> None:
        """Track the forming candle; emit it as CLOSED when the next one starts.

        Same bar-close discipline as ``LiveMarketFeed._watch_candles``: Delta
        streams only the forming candle, so a candle is proven closed exactly
        when a message carries a newer ``candle_start_time``.
        """
        if not sym:
            return
        timeframe = str(msg.get("resolution") or mtype.removeprefix("candlestick_"))
        try:
            start_ms = int(msg["candle_start_time"]) // 1000  # microseconds -> ms
            row = [
                start_ms,
                float(msg["open"]),
                float(msg["high"]),
                float(msg["low"]),
                float(msg["close"]),
                float(msg.get("volume") or 0.0),
            ]
        except (KeyError, TypeError, ValueError):
            return
        key = (sym, timeframe)
        forming = self._forming_candles.get(key)
        if forming is not None and row[0] > forming[0]:
            # a newer interval started: the forming candle is closed
            self.last_closed_candle[key] = forming
            if self.on_candle is not None:
                self.on_candle(sym, timeframe, forming)
        if forming is None or row[0] >= forming[0]:
            self._forming_candles[key] = row
        # older-start messages (out-of-order replays) never regress the forming
        # candle and never re-close an interval; they only count as liveness.
        self._touch()

    def _handle_ticker(self, sym: str | None, msg: dict) -> None:
        if not sym:
            return
        mp = msg.get("mark_price")
        if mp is not None:
            try:
                self.mark_price[sym] = float(mp)
            except (TypeError, ValueError):
                pass
        # ticker sometimes carries funding_rate too; prefer the dedicated channel
        self._touch()

    # -- helpers -----------------------------------------------------------
    def quote(self, symbol: str) -> tuple[float, float] | None:
        sym = delta_native_symbol(symbol)
        bid = self.best_bid.get(sym)
        ask = self.best_ask.get(sym)
        if bid is None or ask is None:
            return None
        if 0 < bid <= ask:
            return (bid, ask)
        return None

    def _touch(self) -> None:
        self.last_event_at = self._now()
        self._consecutive_errors = 0
        self.healthy = True

    def _mark_error(self, exc: Exception) -> None:
        self._consecutive_errors += 1
        if self._consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
            self.healthy = False
        logger.warning(
            "delta ws error (%d consecutive): %s", self._consecutive_errors, exc
        )

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def _now_ms() -> int:
        return int(datetime.now(UTC).timestamp() * 1000)


def _default_connect(url: str):
    """Real websocket connect, imported lazily so tests need no network dep."""
    import websockets  # local import: heavy, optional at import time

    # ping_interval keeps the protocol-level connection alive; Delta also has an
    # app-level heartbeat we enable after subscribe.
    return websockets.connect(url, ping_interval=20, ping_timeout=20, max_size=2**22)


ConnectFactory = Callable[[str], Awaitable[object]]
