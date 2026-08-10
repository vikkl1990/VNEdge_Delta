"""Liquidation event recorder — zero-risk data collection.

    python -m vnedge.exchange.liquidation_recorder --exchange binanceusdm \
        --symbols BTC/USDT:USDT,ETH/USDT:USDT

Streams forced-liquidation events from public websockets and appends them to
the same atomic Parquet tick lake the tick/L2 recorder writes
(``ticks/exchange=<ex>/symbol=<sym>/stream=liquidations/<day>/``). NO
execution, NO credentials, NO order code — public streams only. Liquidation
prints are the event type the event-scalper families key on most (cascades,
stop-hunts, capitulation wicks), and none of the existing streams carry them.

Row schema (fixed):
    ts_ms         int    event time (venue timestamp; wall clock if absent)
    price         float  fill price of the forced order
    amount        float  base quantity liquidated
    side          str    side of the LIQUIDATED position's FORCED ORDER:
                         "sell" = a long was liquidated, "buy" = a short was.
    notional_usd  float  price * amount (quote value)

Side normalisation is deliberate and venue-aware: Binance's forceOrder ``S``
is already the forced order side, but Bybit's allLiquidation ``S`` is the
POSITION side ("Buy" = a long was liquidated), so Bybit sides are flipped on
ingest. One convention on disk, no consumer-side guesswork.

Path selection: CCXT Pro ``watch_liquidations`` where the installed ccxt
supports it for the venue (binanceusdm and bybit both do as of ccxt 4.5.x);
if a future downgrade removes support, Binance falls back to the native
``<symbol>@forceOrder`` combined stream (``BinanceForceOrderRecorder``,
following the DeltaPublicWsClient pattern). Unsupported venue + no native
fallback fails loudly at startup.

Writes reuse the tick recorder's ``_Buffer``: atomic per-flush shards via
temp + rename, batch grouped by UTC day. Liquidations are SPARSE, so flushing
runs on a dedicated 1s cadence loop — a lone event never sits buffered
waiting for the next message to trigger the flush check.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path

from vnedge.exchange.tick_recorder import _Buffer

logger = logging.getLogger(__name__)

STREAM = "liquidations"
_BACKOFF = 2.0
_FLUSH_POLL_SECONDS = 1.0

BINANCE_FORCE_ORDER_WS_URL = "wss://fstream.binance.com/stream"

# Venues whose liquidation feed reports the POSITION side, not the forced
# order side (Bybit allLiquidation: "Buy" == a long position was liquidated).
_POSITION_SIDE_VENUES = {"bybit"}

# Venues with a native websocket fallback when CCXT Pro lacks
# watch_liquidations for them.
_NATIVE_FALLBACKS = {"binanceusdm": "native_binance"}

_FLIP = {"buy": "sell", "sell": "buy"}


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def _forced_order_side(raw_side: str | None, exchange_id: str) -> str:
    """Normalise a venue-reported side to the FORCED ORDER side convention.

    Binance reports the forced order side already; Bybit reports the side of
    the liquidated POSITION, which is the opposite of its forced order.
    Unknown/missing sides become "" — never guessed.
    """
    side = (raw_side or "").lower()
    if side not in _FLIP:
        return ""
    if exchange_id in _POSITION_SIDE_VENUES:
        return _FLIP[side]
    return side


def _liq_row(liq: dict, exchange_id: str, *, fallback_ts_ms: int | None = None) -> dict | None:
    """Map a CCXT liquidation structure to the on-disk row, or None if the
    event lacks a usable price/amount (dropped, counted by the caller's log).

    amount prefers ``baseValue`` (contracts * contractSize, computed by
    ccxt's safe_liquidation) and falls back to ``contracts`` — identical for
    linear USDT perps where contractSize == 1.
    """
    price = liq.get("price")
    amount = liq.get("baseValue")
    if amount is None:
        amount = liq.get("contracts")
    if price is None or amount is None:
        return None
    price = float(price)
    amount = float(amount)
    notional = liq.get("quoteValue")
    notional = float(notional) if notional is not None else price * amount
    ts = liq.get("timestamp")
    if ts is None:
        ts = fallback_ts_ms if fallback_ts_ms is not None else _now_ms()
    info = liq.get("info") or {}
    raw_side = liq.get("side") or info.get("side") or info.get("S")
    return {
        "ts_ms": int(ts),
        "price": price,
        "amount": amount,
        "side": _forced_order_side(raw_side, exchange_id),
        "notional_usd": notional,
    }


def select_path(exchange_id: str, ccxt_supports_liquidations: bool) -> str:
    """Pick the ingest path: "ccxt" when CCXT Pro can watch liquidations for
    the venue, else the venue's registered native fallback. No silent
    degradation — an unservable venue raises with both reasons."""
    if ccxt_supports_liquidations:
        return "ccxt"
    native = _NATIVE_FALLBACKS.get(exchange_id)
    if native is not None:
        return native
    raise ValueError(
        f"no liquidation stream available for {exchange_id!r}: installed ccxt "
        "lacks watch_liquidations for it and no native fallback is implemented"
    )


class LiquidationRecorder:
    """CCXT Pro path: one watch_liquidations task per symbol + a flush loop."""

    def __init__(self, exchange_id: str, symbols: list[str], root: Path,
                 *, exchange=None) -> None:
        if exchange is None:
            import ccxt.pro as ccxtpro

            if not hasattr(ccxtpro, exchange_id):
                raise ValueError(f"unknown CCXT Pro exchange id: {exchange_id}")
            exchange = getattr(ccxtpro, exchange_id)({"enableRateLimit": True})
        if not exchange.has.get("watchLiquidations"):
            raise ValueError(
                f"{exchange_id} does not support watch_liquidations in the "
                "installed ccxt; use build_recorder() for fallback selection"
            )
        self._ex = exchange
        self.exchange_id = exchange_id
        self.symbols = list(symbols)
        self.root = Path(root)
        self.liquidation_count = 0
        self._bufs = {s: _Buffer(self.root, exchange_id, s, STREAM) for s in self.symbols}

    async def _watch(self, symbol: str) -> None:
        buf = self._bufs[symbol]
        while True:
            try:
                liqs = await self._ex.watch_liquidations(symbol)
                for liq in liqs:
                    row = _liq_row(liq, self.exchange_id)
                    if row is None:
                        logger.warning("%s liquidation dropped (no price/amount)", symbol)
                        continue
                    buf.add(row)
                    self.liquidation_count += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("%s liquidations error: %s", symbol, exc)
                await asyncio.sleep(_BACKOFF)

    async def _flush_loop(self, clock) -> None:
        # dedicated cadence: liquidations are sparse, so a message-driven
        # flush check would leave lone events buffered indefinitely.
        try:
            while True:
                now = clock()
                for buf in self._bufs.values():
                    if buf.should_flush(now):
                        buf.flush(now)
                await asyncio.sleep(_FLUSH_POLL_SECONDS)
        except asyncio.CancelledError:
            now = clock()
            for buf in self._bufs.values():
                buf.flush(now)
            raise

    async def run(self, clock=None) -> None:
        import time as _t

        clock = clock or _t.monotonic
        tasks = [asyncio.create_task(self._watch(s)) for s in self.symbols]
        tasks.append(asyncio.create_task(self._flush_loop(clock)))
        logger.info(
            "liquidation recorder (ccxt): %s %s -> %s",
            self.exchange_id, self.symbols, self.root,
        )
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._ex.close()


def binance_stream_name(symbol: str) -> str:
    """CCXT-style symbol -> Binance stream id: ``BTC/USDT:USDT`` -> ``btcusdt``."""
    return symbol.split(":", 1)[0].replace("/", "").lower()


def _binance_force_order_row(o: dict) -> dict | None:
    """Map a raw Binance forceOrder payload (the ``o`` object) to the row.

    ``S`` is the forced order side (recorded verbatim, lowercased), ``ap`` the
    average fill price (falls back to ``p``), ``l`` the last-filled quantity
    (falls back to ``q``). Unparseable events are dropped, never guessed.
    """
    try:
        price = float(o.get("ap") or o["p"])
        amount = float(o.get("l") or o["q"])
        ts = int(o["T"])
    except (KeyError, TypeError, ValueError):
        return None
    return {
        "ts_ms": ts,
        "price": price,
        "amount": amount,
        "side": str(o.get("S", "")).lower(),
        "notional_usd": price * amount,
    }


class BinanceForceOrderRecorder:
    """Native Binance USDT-M ``@forceOrder`` fallback (DeltaPublicWsClient
    pattern): one combined-stream connection for all symbols, a reader task
    that fills per-symbol buffers, and the same sparse-safe flush loop.
    Public stream, no credentials, bounded-backoff reconnection."""

    exchange_id = "binanceusdm"

    def __init__(self, symbols: list[str], root: Path, *,
                 url: str = BINANCE_FORCE_ORDER_WS_URL, connect=None) -> None:
        self.symbols = list(symbols)
        self.root = Path(root)
        self._connect = connect  # injectable for tests; defaults to websockets.connect
        self.liquidation_count = 0
        # buffers keyed by the venue's raw symbol id ("BTCUSDT") so incoming
        # events route without re-parsing; paths use the CCXT-style symbol.
        self._bufs = {
            binance_stream_name(s).upper(): _Buffer(self.root, self.exchange_id, s, STREAM)
            for s in self.symbols
        }
        streams = "/".join(f"{binance_stream_name(s)}@forceOrder" for s in self.symbols)
        self.url = f"{url}?streams={streams}"

    def _handle_raw(self, raw: str | bytes) -> None:
        import json

        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return
        if not isinstance(msg, dict):
            return
        # combined-stream envelope {"stream": ..., "data": {...}} or bare event
        data = msg.get("data", msg)
        if not isinstance(data, dict) or data.get("e") != "forceOrder":
            return
        o = data.get("o")
        if not isinstance(o, dict):
            return
        buf = self._bufs.get(str(o.get("s", "")).upper())
        if buf is None:
            return
        row = _binance_force_order_row(o)
        if row is None:
            logger.warning("forceOrder event dropped (unparseable): %s", o)
            return
        buf.add(row)
        self.liquidation_count += 1

    async def _read_loop(self) -> None:
        connect = self._connect or _default_connect
        while True:
            try:
                async with connect(self.url) as ws:
                    async for raw in ws:
                        self._handle_raw(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("forceOrder stream error: %s", exc)
            # clean stream end (Binance drops sockets ~24h) or error:
            # reconnect with bounded backoff, never hot-loop.
            await asyncio.sleep(_BACKOFF)

    async def run(self, clock=None) -> None:
        import time as _t

        clock = clock or _t.monotonic
        reader = asyncio.create_task(self._read_loop())
        logger.info(
            "liquidation recorder (native forceOrder): %s -> %s", self.symbols, self.root
        )
        try:
            while True:
                now = clock()
                for buf in self._bufs.values():
                    if buf.should_flush(now):
                        buf.flush(now)
                await asyncio.sleep(_FLUSH_POLL_SECONDS)
        except asyncio.CancelledError:
            now = clock()
            for buf in self._bufs.values():
                buf.flush(now)
            raise
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)


def _default_connect(url: str):
    """Real websocket connect, imported lazily so tests need no network dep."""
    import websockets  # local import: heavy, optional at import time

    return websockets.connect(url, ping_interval=20, ping_timeout=20, max_size=2**22)


def build_recorder(exchange_id: str, symbols: list[str], root: Path, *, ccxtpro=None):
    """Probe the installed ccxt and build the right recorder for the venue."""
    if ccxtpro is None:
        import ccxt.pro as ccxtpro  # heavy import, kept off the module import path
    probe = None
    supported = False
    if hasattr(ccxtpro, exchange_id):
        probe = getattr(ccxtpro, exchange_id)({"enableRateLimit": True})
        supported = bool(probe.has.get("watchLiquidations"))
    path = select_path(exchange_id, supported)
    if path == "ccxt":
        return LiquidationRecorder(exchange_id, symbols, root, exchange=probe)
    logger.info("ccxt lacks watch_liquidations for %s; using native fallback", exchange_id)
    return BinanceForceOrderRecorder(symbols, root)


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="zero-risk liquidation event recorder")
    p.add_argument("--exchange", default="binanceusdm")
    p.add_argument("--symbols", default="BTC/USDT:USDT")
    p.add_argument("--data-root", default="data")
    return p.parse_args(argv)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args(argv)
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    recorder = build_recorder(args.exchange, symbols, Path(args.data_root))
    asyncio.run(recorder.run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
