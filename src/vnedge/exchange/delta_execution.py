"""Delta India execution adapter — native, maker-first, gated.

CCXT remains useful for VNEDGE's public/research plumbing, but Delta India
live execution is deliberately native: CCXT has no Delta Pro websocket, no
Delta funding-history surface, and its unified execution/sandbox abstraction
is not the contract we want for India-domiciled real orders. This adapter uses
the official ``delta-rest-client`` call shape with explicit
``post_only``/``reduce_only`` on the official India production environment:
``https://api.india.delta.exchange``.

Same ExecutionAdapter protocol + safety posture as ``CcxtExecutionAdapter``:

- **Production-data, dry-run by default.** Testnet/sandbox execution is
  refused because its liquidity, queues, and matching behavior are not valid
  scalper evidence. Real orders require BOTH real credentials AND
  ``dry_run=False`` AND ``live_confirmed=True`` — set only by the live trader
  after the three-gate settings check. No path reaches mainnet by accident.
- **Idempotent by client_order_id** — the journaled id is the venue client id;
  a duplicate rejection is resolved by lookup, never by minting a new id.
- **Timeout discipline** — a network failure VERIFIES against the venue by
  client id before any bounded resubmit (same id). Still ambiguous ->
  AdapterTimeout -> TIMEOUT_UNKNOWN for reconciliation.
- Sizing/precision rounds DOWN to contract steps; the gateway upstream already
  rejected too-small results (never inflated to a minimum).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from vnedge.exchange.delta_contracts import (
    DeltaContractSpec,
    contracts_from_base_quantity,
)
from vnedge.execution.order_manager import AdapterRejection, AdapterTimeout
from vnedge.execution.order_state import ManagedOrder
from vnedge.risk.risk_manager import ServerSideBracket

if TYPE_CHECKING:
    from vnedge.exchange.delta_execution_safety import DeltaDeadmanConfig

logger = logging.getLogger(__name__)

_INDIA_BASE = "https://api.india.delta.exchange"


@dataclass(frozen=True)
class _DeltaEnumValue:
    """Tiny enum-compatible shim for the official delta-rest-client.

    The client expects ``order_type.value`` and ``time_in_force.value`` but we
    keep this adapter import-light so dry-run/test paths do not need to import
    the network client eagerly.
    """

    value: str


class DeltaRestExecutionAdapter:
    def __init__(
        self,
        *,
        api_key: str = "",
        api_secret: str = "",
        testnet: bool = False,
        live_confirmed: bool = False,
        dry_run: bool | None = None,
        base_url: str | None = None,
        product_ids: dict[str, int] | None = None,
        contract_specs: dict[str, DeltaContractSpec] | None = None,
        max_submit_attempts: int = 2,
        client: Any | None = None,  # injectable for tests
        safety_client: Any | None = None,  # heartbeat + atomic brackets
    ) -> None:
        # dry_run defaults ON unless the caller explicitly opts into real orders
        self.dry_run = True if dry_run is None else bool(dry_run)
        candidate_base_url = base_url or _INDIA_BASE
        if testnet or "testnet" in candidate_base_url.lower():
            raise ValueError(
                "Delta testnet execution is disabled: use production market data "
                "with dry_run/shadow, then live_confirmed mainnet only after gates"
            )
        if not self.dry_run:
            if not api_key or not api_secret:
                raise ValueError(
                    "real Delta orders require trade-only credentials (or dry_run=True)"
                )
            if not live_confirmed:
                raise ValueError(
                    "mainnet execution requires live_confirmed=True — only the live "
                    "trader sets this, after the three-gate settings check"
                )
        self.testnet = testnet
        self.max_submit_attempts = max_submit_attempts
        self._product_ids = dict(product_ids or {})
        self._contract_specs = dict(contract_specs or {})
        self._client = client
        self._safety_client = safety_client
        self._base_url = candidate_base_url
        self._creds = (api_key, api_secret)
        self._deadman_armed_until = 0.0

    # --- client (lazy; real construction only when not dry-run) ---------------
    def _ensure_client(self):
        if self._client is not None:
            return self._client
        if self.dry_run:  # pragma: no cover - dry-run never builds a live client
            return None
        import delta_rest_client  # type: ignore[import-untyped]  # pragma: no cover

        self._client = delta_rest_client.DeltaRestClient(
            base_url=self._base_url, api_key=self._creds[0], api_secret=self._creds[1]
        )  # pragma: no cover
        return self._client

    def _product_id(self, symbol: str) -> int:
        pid = self._product_ids.get(symbol)
        if pid is None:
            raise AdapterRejection(f"no product_id mapping for {symbol} — load products first")
        return pid

    def _ensure_safety_client(self):
        if self._safety_client is not None:
            return self._safety_client
        if self.dry_run:  # pragma: no cover - dry-run never builds an authenticated client
            return None
        from vnedge.exchange.delta_execution_safety import (  # pragma: no cover
            DeltaAuthenticatedSafetyClient,
        )

        self._safety_client = DeltaAuthenticatedSafetyClient(
            api_key=self._creds[0],
            api_secret=self._creds[1],
            base_url=self._base_url,
        )
        return self._safety_client

    def _order_contracts(self, intent) -> int:
        spec = self._contract_specs.get(intent.symbol)
        if spec is None:
            # Legacy compatibility for already-contract-shaped tests/configs.
            contracts = int(intent.quantity)
        else:
            if intent.limit_price is not None:
                price = float(intent.limit_price)
            elif intent.quantity > 0 and intent.notional_usd > 0:
                price = float(intent.notional_usd) / float(intent.quantity)
            else:
                raise AdapterRejection(
                    f"cannot convert {intent.symbol} quantity to Delta contracts without "
                    "limit_price or notional/quantity reference price"
                )
            contracts = contracts_from_base_quantity(
                base_quantity=float(intent.quantity),
                entry_price=price,
                spec=spec,
            )
        if contracts <= 0:
            raise AdapterRejection(
                f"Delta size rounds to {contracts} contracts for {intent.symbol}; "
                "quantity is below one contract after rounding down"
            )
        return contracts

    # --- ExecutionAdapter protocol -------------------------------------------
    async def submit_order(self, order: ManagedOrder) -> str:
        args = self._submission_args(order)
        if self.dry_run:
            logger.info(
                "DRY-RUN Delta %s %s size=%s post_only=%s reduce_only=%s coid=%s",
                args["side"],
                order.intent.symbol,
                args["size"],
                args["post_only"],
                args["reduce_only"],
                order.client_order_id,
            )
            return f"dryrun-{order.client_order_id}"
        if not order.intent.reduce_only:
            raise AdapterRejection(
                "live Delta entries require DeltaExecutionSafetyWrapper with an armed "
                "dead-man heartbeat and atomic server-side bracket"
            )

        client = self._ensure_client()
        return await self._submit_with_reconciliation(
            order,
            lambda: client.place_order(**args),
        )

    async def submit_protected_order(
        self,
        order: ManagedOrder,
        protection: ServerSideBracket,
    ) -> str:
        """Attach TP/SL to the entry in the same authenticated order request."""

        if order.intent.reduce_only:
            raise AdapterRejection("protected entry path cannot submit a reduce-only order")
        if not self.dry_run and time.monotonic() >= self._deadman_armed_until:
            raise AdapterRejection("protected Delta entry refused: dead-man heartbeat is not armed")
        protection.validate_for(order.intent.side, order.intent.limit_price)
        args = self._submission_args(order)
        payload = {
            "product_id": args["product_id"],
            "size": args["size"],
            "side": args["side"],
            "order_type": args["order_type"].value,
            "post_only": args["post_only"] == "true",
            "reduce_only": False,
            "client_order_id": args["client_order_id"],
            "bracket_stop_trigger_method": protection.trigger_method,
            "bracket_stop_loss_price": str(protection.stop_loss_trigger_price),
            "bracket_take_profit_price": str(protection.take_profit_trigger_price),
        }
        if args["limit_price"] is not None:
            payload["limit_price"] = str(args["limit_price"])
        if args["time_in_force"] is not None:
            payload["time_in_force"] = args["time_in_force"].value
        if protection.stop_loss_limit_price is not None:
            payload["bracket_stop_loss_limit_price"] = str(protection.stop_loss_limit_price)
        if protection.take_profit_limit_price is not None:
            payload["bracket_take_profit_limit_price"] = str(protection.take_profit_limit_price)
        if self.dry_run:
            logger.info(
                "DRY-RUN protected Delta entry %s coid=%s stop=%s target=%s",
                order.intent.symbol,
                order.client_order_id,
                protection.stop_loss_trigger_price,
                protection.take_profit_trigger_price,
            )
            return f"dryrun-{order.client_order_id}"
        safety_client = self._ensure_safety_client()
        return await self._submit_with_reconciliation(
            order,
            lambda: safety_client.place_protected_order(payload),
        )

    async def create_deadman(self, config: DeltaDeadmanConfig) -> dict[str, Any]:
        if self.dry_run:
            return {"heartbeat_id": config.heartbeat_id, "status": "dry_run"}
        client = self._ensure_safety_client()
        result = await asyncio.to_thread(client.create_heartbeat, config)
        return dict(result)

    async def acknowledge_deadman(self, heartbeat_id: str, ttl_ms: int) -> dict[str, Any]:
        if self.dry_run:
            return {"heartbeat_timestamp": "dry_run", "process_enabled": ttl_ms > 0}
        client = self._ensure_safety_client()
        result = await asyncio.to_thread(client.acknowledge_heartbeat, heartbeat_id, ttl_ms)
        process_enabled = result.get("process_enabled", ttl_ms > 0)
        if ttl_ms > 0 and str(process_enabled).lower() not in {"true", "1"}:
            self._deadman_armed_until = 0.0
            raise AdapterRejection(f"Delta heartbeat acknowledgment is disabled: {result}")
        self._deadman_armed_until = time.monotonic() + ttl_ms / 1000.0 if ttl_ms > 0 else 0.0
        return dict(result)

    async def fetch_exchange_truth(self) -> dict[str, Any]:
        """Read direct venue truth for the reconciliation owner.

        This deliberately does not mutate or clear local reconciliation state.
        """

        if self.dry_run:
            return {"open_orders": [], "positions": [], "wallet_balances": []}
        client = self._ensure_safety_client()
        product_ids = tuple(sorted(set(self._product_ids.values())))
        if not product_ids:
            raise AdapterRejection("Delta truth fetch requires configured product IDs")
        open_orders = await asyncio.to_thread(client.get_open_orders, product_ids)
        positions = [
            await asyncio.to_thread(client.get_position, product_id)
            for product_id in product_ids
        ]
        wallets = await asyncio.to_thread(client.get_wallet_balances)
        return {
            "open_orders": list(open_orders),
            "positions": positions,
            "wallet_balances": list(wallets),
        }

    async def cancel_risk_increasing_orders(
        self,
        product_id: int | None = None,
    ) -> dict[str, Any]:
        """Pull entry limits while leaving stops/reduce-only exits working."""

        if self.dry_run:
            return {"status": "dry_run", "protective_orders_preserved": True}
        client = self._ensure_safety_client()
        result = await asyncio.to_thread(client.cancel_risk_increasing_orders, product_id)
        return dict(result)

    def _submission_args(self, order: ManagedOrder) -> dict[str, Any]:
        intent = order.intent
        if not order.client_order_id or len(order.client_order_id) > 32:
            raise AdapterRejection("Delta client_order_id must contain 1-32 characters")
        side = "buy" if intent.side == "long" else "sell"
        order_type = _order_type(intent.order_type)
        post_only = "true" if intent.time_in_force == "PO" else "false"
        time_in_force = _time_in_force(intent.time_in_force)
        if order_type.value == "market_order" and post_only == "true":
            raise AdapterRejection("Delta market orders cannot be post_only")
        return {
            "product_id": self._product_id(intent.symbol),
            "size": self._order_contracts(intent),
            "side": side,
            "limit_price": intent.limit_price,
            "order_type": order_type,
            "time_in_force": time_in_force,
            "post_only": post_only,
            "reduce_only": "true" if intent.reduce_only else "false",
            "client_order_id": order.client_order_id,
        }

    async def _submit_with_reconciliation(
        self,
        order: ManagedOrder,
        submit: Callable[[], Any],
    ) -> str:
        for attempt in range(1, self.max_submit_attempts + 1):  # pragma: no cover - network
            try:
                result = await asyncio.to_thread(submit)
                oid = _order_id(result)
                if oid is None:
                    raise AdapterRejection(f"venue accepted but returned no id: {result}")
                return str(oid)
            except AdapterRejection:
                raise
            except Exception as exc:
                msg = str(exc).lower()
                if any(k in msg for k in ("duplicate", "client_order_id")):
                    existing = await self._verify_by_client_id(order)
                    if existing is not None:
                        return existing
                    raise AdapterTimeout(
                        "duplicate client id but order not found — reconcile"
                    ) from exc
                if any(k in msg for k in ("insufficient", "invalid", "reduce_only", "rejected")):
                    raise AdapterRejection(f"venue rejected: {exc}") from exc
                # treat as network-ambiguous: verify before any resubmit
                logger.warning(
                    "Delta submit %s ambiguous (attempt %d/%d): %s",
                    order.client_order_id,
                    attempt,
                    self.max_submit_attempts,
                    exc,
                )
                existing = await self._verify_by_client_id(order)
                if existing is not None:
                    return existing
                if attempt == self.max_submit_attempts:
                    raise AdapterTimeout(f"submission ambiguous after {attempt} attempts") from exc
        raise AdapterTimeout("unreachable")  # pragma: no cover

    async def _verify_by_client_id(
        self, order: ManagedOrder
    ) -> str | None:  # pragma: no cover - network
        if self.dry_run or self._client is None:
            return None
        await asyncio.sleep(0.5)
        try:
            res = await asyncio.to_thread(
                self._client.get_order_by_client_id, order.client_order_id
            )
            oid = _order_id(res)
            return str(oid) if oid else None
        except Exception as exc:  # noqa: BLE001
            logger.warning("post-timeout verification failed: %s", exc)
            return None

    async def cancel_order(self, order: ManagedOrder) -> str:
        """Cancel a working Delta order and return the venue's terminal-ish state."""

        if self.dry_run:
            return "cancelled"
        client = self._ensure_client()
        order_id = order.exchange_order_id or await self._verify_by_client_id(order)
        if order_id is None:
            return "cancelled"
        try:  # pragma: no cover - network
            result = await asyncio.to_thread(
                client.cancel_order,
                self._product_id(order.intent.symbol),
                order_id,
            )
            return _normalise_delta_status(result, default="cancelled")
        except Exception as exc:
            if _looks_not_found(exc):
                status = await self.fetch_order_status(order)
                if status is None:
                    return "cancelled"
                return _normalise_delta_status(status)
            raise AdapterRejection(f"Delta cancel rejected: {exc}") from exc

    async def fetch_order_status(self, order: ManagedOrder) -> dict | None:
        """Fetch venue truth by idempotent client id for reconciliation."""

        if self.dry_run:
            return None
        client = self._ensure_client()
        try:  # pragma: no cover - network
            result = await asyncio.to_thread(
                client.get_order_by_client_id,
                order.client_order_id,
            )
        except Exception as exc:
            if _looks_not_found(exc):
                return None
            raise
        payload = _unwrap_result(result)
        if not isinstance(payload, dict) or not payload.get("id"):
            return None
        return payload


def _order_type(raw: str) -> _DeltaEnumValue:
    value = str(raw or "").lower()
    if value in {"limit", "limit_order"}:
        return _DeltaEnumValue("limit_order")
    if value in {"market", "market_order"}:
        return _DeltaEnumValue("market_order")
    raise AdapterRejection(f"unsupported Delta order_type: {raw}")


def _time_in_force(raw: str | None) -> _DeltaEnumValue | None:
    if raw is None or raw == "" or raw == "PO":
        return None
    value = str(raw).lower()
    if value in {"gtc", "ioc", "fok"}:
        return _DeltaEnumValue(value)
    raise AdapterRejection(f"unsupported Delta time_in_force: {raw}")


def _unwrap_result(result: object) -> object:
    if isinstance(result, dict) and isinstance(result.get("result"), dict):
        return result["result"]
    return result


def _order_id(result: object) -> str | None:
    payload = _unwrap_result(result)
    if isinstance(payload, dict):
        oid = payload.get("id")
        return str(oid) if oid is not None else None
    return None


def _normalise_delta_status(result: object, *, default: str = "open") -> str:
    payload = _unwrap_result(result)
    state = ""
    if isinstance(payload, dict):
        state = str(payload.get("state") or payload.get("status") or "").lower()
    if state in {"cancelled", "canceled"}:
        return "cancelled"
    if state in {"closed", "filled"}:
        return "filled"
    if state == "open":
        return "open"
    return default


def _looks_not_found(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(token in msg for token in ("not found", "404", "does not exist", "no order"))
