"""Async CCXT client for public historical market data.

Public endpoints only — no credentials, no orders. Handles pagination,
bounded retries with exponential backoff on network errors, and venue
capability checks. Rate limiting is delegated to CCXT's built-in throttler
(``enableRateLimit``), which knows each venue's weight rules.

Venue notes:
- binanceusdm: OHLCV limit 1500/page; open-interest history only covers the
  most recent ~30 days and requires a period (we pass the timeframe).
- bybit: linear perps via unified v5 API; funding history supported.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import ccxt.async_support as ccxt_async
from ccxt.base.errors import NetworkError, NotSupported

logger = logging.getLogger(__name__)

_PAGE_LIMIT = 1000
_MAX_RETRIES = 3
_BACKOFF_BASE_SECONDS = 1.0

# Venues reject OI history requests older than their retention window
# (Binance: BadRequest -1130 beyond ~30 days). Clamp conservatively and log —
# never silently, so callers know their requested range was cut.
_OI_LOOKBACK_LIMIT_MS: dict[str, int] = {
    "binanceusdm": 29 * 86_400_000,
}
_CCXT_EXCHANGE_ALIASES: dict[str, str] = {
    "delta_india": "delta",
}
_API_URL_OVERRIDES: dict[str, dict[str, str]] = {
    "delta_india": {
        "public": "https://api.india.delta.exchange",
        "private": "https://api.india.delta.exchange",
    },
}


def resolve_ccxt_exchange_id(exchange_id: str) -> str:
    return _CCXT_EXCHANGE_ALIASES.get(exchange_id, exchange_id)


def create_ccxt_async_exchange(exchange_id: str):
    ccxt_exchange_id = resolve_ccxt_exchange_id(exchange_id)
    if not hasattr(ccxt_async, ccxt_exchange_id):
        raise ValueError(f"unknown CCXT exchange id: {exchange_id}")
    exchange = getattr(ccxt_async, ccxt_exchange_id)({"enableRateLimit": True})
    if exchange_id in _API_URL_OVERRIDES:
        exchange.urls["api"] = dict(_API_URL_OVERRIDES[exchange_id])
    return exchange


class CcxtPublicClient:
    def __init__(self, exchange_id: str = "binanceusdm") -> None:
        self.exchange_id = exchange_id
        self.ccxt_exchange_id = resolve_ccxt_exchange_id(exchange_id)
        self._exchange = create_ccxt_async_exchange(exchange_id)

    async def close(self) -> None:
        await self._exchange.close()

    async def __aenter__(self) -> "CcxtPublicClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # --- Internals -------------------------------------------------------------
    async def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """One venue call with bounded retries on transient network errors.
        Non-network exchange errors propagate immediately — retrying a
        malformed request only burns rate limit."""
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                return await getattr(self._exchange, method)(*args, **kwargs)
            except NetworkError as exc:
                if attempt == _MAX_RETRIES:
                    raise
                delay = _BACKOFF_BASE_SECONDS * 2 ** (attempt - 1)
                logger.warning(
                    "%s %s network error (attempt %d/%d), retrying in %.1fs: %s",
                    self.exchange_id, method, attempt, _MAX_RETRIES, delay, exc,
                )
                await asyncio.sleep(delay)
        raise RuntimeError("unreachable")  # pragma: no cover

    def _require(self, capability: str) -> None:
        if not self._exchange.has.get(capability):
            raise NotSupported(f"{self.exchange_id} does not support {capability}")

    @staticmethod
    def _advance(since: int, last_ts: int) -> int | None:
        """Next page cursor, or None when the venue stopped advancing
        (guards against infinite pagination loops)."""
        next_since = last_ts + 1
        return next_since if next_since > since else None

    # --- Public fetchers ---------------------------------------------------------
    async def fetch_candles(
        self, symbol: str, timeframe: str, since_ms: int, until_ms: int
    ) -> list[list]:
        """Paginated OHLCV in [since_ms, until_ms)."""
        self._require("fetchOHLCV")
        out: list[list] = []
        since = since_ms
        while since < until_ms:
            page = await self._call(
                "fetch_ohlcv", symbol, timeframe, since=since, limit=_PAGE_LIMIT
            )
            if not page:
                break
            out.extend(row for row in page if row[0] < until_ms)
            cursor = self._advance(since, int(page[-1][0]))
            if cursor is None or len(page) < 2:
                break
            since = cursor
        logger.info(
            "%s fetched %d candles %s %s", self.exchange_id, len(out), symbol, timeframe
        )
        return out

    async def fetch_funding_history(
        self, symbol: str, since_ms: int, until_ms: int
    ) -> list[dict]:
        self._require("fetchFundingRateHistory")
        out: list[dict] = []
        since = since_ms
        while since < until_ms:
            page = await self._call(
                "fetch_funding_rate_history", symbol, since=since, limit=_PAGE_LIMIT
            )
            if not page:
                break
            out.extend(item for item in page if item["timestamp"] < until_ms)
            cursor = self._advance(since, int(page[-1]["timestamp"]))
            if cursor is None or len(page) < 2:
                break
            since = cursor
        logger.info("%s fetched %d funding rows %s", self.exchange_id, len(out), symbol)
        return out

    async def fetch_open_interest_history(
        self, symbol: str, timeframe: str, since_ms: int, until_ms: int
    ) -> list[dict]:
        self._require("fetchOpenInterestHistory")
        limit_ms = _OI_LOOKBACK_LIMIT_MS.get(self.exchange_id)
        if limit_ms is not None:
            earliest = until_ms - limit_ms
            if since_ms < earliest:
                logger.warning(
                    "%s OI history capped at ~%dd lookback — clamping %s start "
                    "(requested range loses %.1f days)",
                    self.exchange_id, limit_ms // 86_400_000, symbol,
                    (earliest - since_ms) / 86_400_000,
                )
                since_ms = earliest
        out: list[dict] = []
        since = since_ms
        while since < until_ms:
            page = await self._call(
                "fetch_open_interest_history",
                symbol,
                timeframe,
                since=since,
                limit=500,
            )
            if not page:
                break
            out.extend(item for item in page if item["timestamp"] < until_ms)
            cursor = self._advance(since, int(page[-1]["timestamp"]))
            if cursor is None or len(page) < 2:
                break
            since = cursor
        logger.info("%s fetched %d OI rows %s %s", self.exchange_id, len(out), symbol, timeframe)
        return out
