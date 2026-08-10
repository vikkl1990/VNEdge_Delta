"""Live execution adapter — the last link in the execution chain.

Implements the same ExecutionAdapter protocol the paper broker does, against
a real venue via CCXT. Safety posture:

- **Production-data only.** Testnet/sandbox execution is refused because fake
  liquidity and matching behavior are not valid scalper evidence. Real orders
  require ``live_confirmed=True`` — wired only from the live trader or bounded
  execution drill after the three-gate settings check. There is no code path
  that reaches mainnet by accident.
- **Idempotent by client_order_id**: the venue receives our journaled id as
  the client order id. A duplicate-id rejection is resolved by fetching the
  existing order — never by minting a new id.
- **Timeout discipline** (docs/DESIGN.md §2): on a network timeout the
  adapter VERIFIES against the venue by client id. Found -> acknowledged.
  Not found -> bounded resubmit with the SAME id. Still ambiguous ->
  AdapterTimeout, which parks the order in TIMEOUT_UNKNOWN for
  reconciliation. Never a blind retry, never a fresh id.
"""

from __future__ import annotations

import asyncio
import logging

from vnedge.execution.order_manager import AdapterRejection, AdapterTimeout
from vnedge.execution.order_state import ManagedOrder

logger = logging.getLogger(__name__)

_NATIVE_EXECUTION_ONLY_IDS = frozenset({"delta", "delta_india", "deltaindia"})


class CcxtExecutionAdapter:
    def __init__(
        self,
        exchange_id: str = "binanceusdm",
        *,
        api_key: str,
        api_secret: str,
        testnet: bool = False,
        live_confirmed: bool = False,
        max_submit_attempts: int = 2,
        client: object | None = None,  # injectable for tests
    ) -> None:
        if exchange_id in _NATIVE_EXECUTION_ONLY_IDS:
            raise ValueError(
                "Delta India execution requires DeltaRestExecutionAdapter; "
                "the CCXT adapter is public/research-only for Delta in VNEDGE"
            )
        if testnet:
            raise ValueError(
                "testnet execution is disabled: use production market data with "
                "dry-run/shadow, then gated mainnet drill/live_small only"
            )
        if not api_key or not api_secret:
            raise ValueError("execution adapter requires API credentials (trade-only keys)")
        if not live_confirmed:
            raise ValueError(
                "mainnet execution requires live_confirmed=True — only the live "
                "trader sets this, after the three-gate settings check"
            )
        self.exchange_id = exchange_id
        self.testnet = testnet
        self.max_submit_attempts = max_submit_attempts
        if client is not None:
            self._ex = client
        else:  # pragma: no cover - network client construction
            import ccxt.async_support as ccxt_async

            from vnedge.data.ccxt_client import (
                _API_URL_OVERRIDES,
                resolve_ccxt_exchange_id,
            )

            resolved = resolve_ccxt_exchange_id(exchange_id)
            self._ex = getattr(ccxt_async, resolved)(
                {"apiKey": api_key, "secret": api_secret, "enableRateLimit": True}
            )
            overrides = _API_URL_OVERRIDES.get(exchange_id)
            if overrides:
                self._ex.urls["api"] = {**self._ex.urls.get("api", {}), **overrides}

    # --- read-only helpers (drill + reconciliation surface) -------------------
    async def fetch_balance(self) -> dict:
        bal = await self._ex.fetch_balance()
        total = bal.get("total", {}) or {}
        usd = sum(float(v or 0.0) for k, v in total.items()
                  if k in ("USDT", "USD", "USDC"))
        return {"total_usd": usd, **{k: float(v or 0.0) for k, v in total.items()}}

    async def fetch_positions(self, symbol: str) -> list:
        try:
            positions = await self._ex.fetch_positions([symbol])
        except Exception:  # noqa: BLE001 - venues vary; empty is the safe read
            return []
        return [p for p in positions or []
                if abs(float(p.get("contracts") or p.get("contractSize") or 0.0)) > 0]

    async def fetch_open_orders(self, symbol: str) -> list:
        return await self._ex.fetch_open_orders(symbol) or []

    async def fetch_mid_price(self, symbol: str) -> float:
        book = await self._ex.fetch_order_book(symbol, limit=50)
        bid, ask = float(book["bids"][0][0]), float(book["asks"][0][0])
        return (bid + ask) / 2.0

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        """Round DOWN to venue amount steps (never inflate to meet minimums)."""
        try:
            self._ex.options["truncate"] = True  # ccxt truncates by default
            return float(self._ex.amount_to_precision(symbol, amount))
        except Exception:  # noqa: BLE001 - markets not loaded yet
            return amount

    async def close(self) -> None:
        await self._ex.close()

    # --- ExecutionAdapter protocol --------------------------------------------------
    async def submit_order(self, order: ManagedOrder) -> str:
        from ccxt.base.errors import (
            DuplicateOrderId,
            ExchangeError,
            InsufficientFunds,
            InvalidOrder,
            NetworkError,
        )

        intent = order.intent
        side = "buy" if intent.side == "long" else "sell"
        params = {
            "newClientOrderId": order.client_order_id,
            "reduceOnly": intent.reduce_only,
        }
        # Time-in-force pass-through (ccxt unified params). Values are
        # validated at OrderIntent construction; None = venue default, so
        # neither key appears. "PO" (post-only) uses ccxt's unified
        # ``postOnly`` flag, which each venue translates itself
        # (binanceusdm -> timeInForce GTX, bybit -> PostOnly); the explicit
        # TIFs go through ``timeInForce`` verbatim.
        if intent.time_in_force == "PO":
            params["postOnly"] = True
        elif intent.time_in_force is not None:
            params["timeInForce"] = intent.time_in_force

        for attempt in range(1, self.max_submit_attempts + 1):
            try:
                result = await self._ex.create_order(
                    intent.symbol, intent.order_type, side, intent.quantity,
                    intent.limit_price, params,
                )
                return str(result["id"])
            except DuplicateOrderId:
                # Our id already exists at the venue — idempotency working.
                existing = await self._fetch_by_client_id(order)
                if existing is not None:
                    return existing
                raise AdapterTimeout(
                    "venue reports duplicate client id but order not found — reconcile"
                )
            except (InsufficientFunds, InvalidOrder) as exc:
                raise AdapterRejection(f"{type(exc).__name__}: {exc}") from exc
            except NetworkError as exc:
                logger.warning(
                    "submit %s network error (attempt %d/%d): %s",
                    order.client_order_id, attempt, self.max_submit_attempts, exc,
                )
                # MAY have reached the venue: verify before any resubmit.
                existing = await self._verify_after_timeout(order)
                if existing is not None:
                    return existing
                if attempt == self.max_submit_attempts:
                    raise AdapterTimeout(
                        f"submission ambiguous after {attempt} attempts"
                    ) from exc
                # Not found at venue: safe to resubmit with the SAME id.
            except ExchangeError as exc:
                raise AdapterRejection(f"venue error: {exc}") from exc
        raise AdapterTimeout("unreachable")  # pragma: no cover

    async def cancel_order(self, order: ManagedOrder) -> str:
        """Returns the venue's resulting state ('cancelled'/'filled'/...);
        a cancel losing the race to a fill is an answer, not an error."""
        from ccxt.base.errors import OrderNotFound

        try:
            await self._ex.cancel_order(
                order.exchange_order_id, order.intent.symbol,
                {"origClientOrderId": order.client_order_id},
            )
            return "cancelled"
        except OrderNotFound:
            status = await self.fetch_order_status(order)
            if status is None:
                return "cancelled"  # never existed venue-side; nothing working
            s = status.get("status", "")
            if s == "closed":
                return "filled"
            if s == "open" and (status.get("filled") or 0) > 0:
                return "partially_filled"
            return "cancelled" if s in ("canceled", "cancelled", "expired") else s

    # --- Venue truth (reconciliation surface, same shape as the paper venue) --------
    async def fetch_order_status(self, order: ManagedOrder) -> dict | None:
        from ccxt.base.errors import OrderNotFound

        try:
            return await self._ex.fetch_order(
                None, order.intent.symbol,
                {"origClientOrderId": order.client_order_id},
            )
        except OrderNotFound:
            return None

    async def _fetch_by_client_id(self, order: ManagedOrder) -> str | None:
        result = await self.fetch_order_status(order)
        return str(result["id"]) if result else None

    async def _verify_after_timeout(self, order: ManagedOrder) -> str | None:
        await asyncio.sleep(0.5)  # give the venue a beat to register it
        try:
            return await self._fetch_by_client_id(order)
        except Exception as exc:  # noqa: BLE001 — verification itself failed
            logger.warning("post-timeout verification failed: %s", exc)
            return None
