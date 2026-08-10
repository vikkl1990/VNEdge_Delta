"""Fail-closed Delta India execution safety interlock.

The exchange heartbeat (dead-man switch) cancels resting orders when this
process stops acknowledging it. New risk is refused unless that heartbeat is
currently armed, journaled, and has enough TTL remaining. Entry orders must
also carry an exchange-resident stop/target contract which the native adapter
attaches to the entry request atomically.

Reduce-only exits deliberately remain available when the heartbeat or journal
is unhealthy: an entry interlock must never prevent risk reduction.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import httpx

from vnedge.execution.order_manager import AdapterRejection
from vnedge.execution.order_state import ManagedOrder
from vnedge.risk.risk_manager import ServerSideBracket

logger = logging.getLogger(__name__)

DELTA_INDIA_API = "https://api.india.delta.exchange"


class DeltaSafetyError(RuntimeError):
    """The exchange safety contract could not be established or maintained."""


class DeltaSafetyTransportError(DeltaSafetyError):
    """Authenticated Delta safety request failed or returned an invalid response."""


@dataclass(frozen=True)
class DeltaDeadmanConfig:
    heartbeat_id: str
    product_symbols: tuple[str, ...]
    ttl_ms: int = 15_000
    acknowledge_interval_ms: int = 5_000
    entry_guard_ms: int = 5_000
    unhealthy_count: int = 1
    impact: str = "low"

    def __post_init__(self) -> None:
        if not self.heartbeat_id.strip():
            raise ValueError("heartbeat_id cannot be empty")
        if not self.product_symbols or any(not symbol.strip() for symbol in self.product_symbols):
            raise ValueError("product_symbols cannot be empty")
        if len(set(self.product_symbols)) != len(self.product_symbols):
            raise ValueError("product_symbols cannot contain duplicates")
        if self.ttl_ms < 5_000:
            raise ValueError("dead-man TTL must be at least 5000ms")
        if not 250 <= self.acknowledge_interval_ms < self.ttl_ms:
            raise ValueError("acknowledge interval must be in [250ms, ttl_ms)")
        if not 0 < self.entry_guard_ms < self.ttl_ms:
            raise ValueError("entry_guard_ms must be in (0, ttl_ms)")
        if self.acknowledge_interval_ms + self.entry_guard_ms > self.ttl_ms:
            raise ValueError("heartbeat interval plus entry guard cannot exceed TTL")
        if self.unhealthy_count < 1:
            raise ValueError("unhealthy_count must be positive")
        if self.impact not in {"low", "medium", "high"}:
            raise ValueError("heartbeat impact must be low, medium, or high")


@dataclass(frozen=True)
class DeltaDeadmanSnapshot:
    heartbeat_id: str
    created: bool = False
    armed: bool = False
    last_acknowledged_at: datetime | None = None
    expires_at: datetime | None = None
    consecutive_failures: int = 0
    reason: str = "not_started"


@dataclass(frozen=True)
class DeltaExchangeTruthSnapshot:
    """Hashed, immutable summary of account truth observed directly at Delta.

    This is evidence for reconciliation, not a declaration that reconciliation
    passed.  Only the reconciliation owner may compare it with local state and
    clear the fail-closed entry block.
    """

    observed_at: datetime
    open_order_count: int
    non_reduce_open_order_count: int
    nonzero_position_count: int
    wallet_balance_count: int
    payload_sha256: str


class SafetyJournal(Protocol):
    @property
    def available(self) -> bool: ...

    def append(self, kind: str, payload: dict[str, Any]) -> bool: ...


class ProtectedDeltaAdapter(Protocol):
    async def submit_order(self, order: ManagedOrder) -> str: ...

    async def submit_protected_order(
        self,
        order: ManagedOrder,
        protection: ServerSideBracket,
    ) -> str: ...

    async def create_deadman(self, config: DeltaDeadmanConfig) -> Mapping[str, Any]: ...

    async def acknowledge_deadman(
        self,
        heartbeat_id: str,
        ttl_ms: int,
    ) -> Mapping[str, Any]: ...

    async def cancel_order(self, order: ManagedOrder) -> str: ...

    async def fetch_order_status(self, order: ManagedOrder) -> dict[str, Any] | None: ...

    async def fetch_exchange_truth(self) -> Mapping[str, Any]: ...

    async def cancel_risk_increasing_orders(self, product_id: int | None = None) -> Mapping[str, Any]: ...


class DeltaAuthenticatedSafetyClient:
    """Minimal HMAC REST client for heartbeat and atomic protected entries."""

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        base_url: str = DELTA_INDIA_API,
        timeout_seconds: float = 10.0,
        http: Any | None = None,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        if not api_key or not api_secret:
            raise ValueError("Delta safety client requires API credentials")
        if base_url.rstrip("/") != DELTA_INDIA_API:
            raise ValueError("Delta safety client is pinned to the official India production API")
        self._api_key = api_key
        self._secret = api_secret.encode()
        self._base_url = base_url.rstrip("/")
        self._clock = wall_clock
        self._owns_http = http is None
        self._http = http or httpx.Client(timeout=timeout_seconds)

    def create_heartbeat(self, config: DeltaDeadmanConfig) -> Mapping[str, Any]:
        return self._request(
            "POST",
            "/v2/heartbeat/create",
            {
                "heartbeat_id": config.heartbeat_id,
                "impact": config.impact,
                "product_symbols": list(config.product_symbols),
                "config": [
                    {
                        "action": "cancel_orders",
                        "unhealthy_count": config.unhealthy_count,
                    }
                ],
            },
        )

    def acknowledge_heartbeat(self, heartbeat_id: str, ttl_ms: int) -> Mapping[str, Any]:
        if not heartbeat_id.strip():
            raise ValueError("heartbeat_id cannot be empty")
        if ttl_ms < 0:
            raise ValueError("heartbeat TTL cannot be negative")
        return self._request(
            "POST",
            "/v2/heartbeat",
            {"heartbeat_id": heartbeat_id, "ttl": ttl_ms},
        )

    def place_protected_order(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        required = {
            "product_id",
            "size",
            "side",
            "order_type",
            "client_order_id",
            "bracket_stop_loss_price",
            "bracket_take_profit_price",
        }
        missing = sorted(required - payload.keys())
        if missing:
            raise ValueError(f"protected Delta order missing: {', '.join(missing)}")
        return self._request("POST", "/v2/orders", payload)

    def get_open_orders(self, product_ids: Sequence[int]) -> tuple[Mapping[str, Any], ...]:
        if not product_ids:
            raise ValueError("at least one product_id is required for open-order truth")
        result = self._request(
            "GET",
            "/v2/orders",
            query={
                "product_ids": ",".join(str(value) for value in product_ids),
                "states": "open,pending",
            },
        )
        return self._mapping_rows("/v2/orders", result)

    def get_position(self, product_id: int) -> Mapping[str, Any]:
        if product_id <= 0:
            raise ValueError("product_id must be positive")
        result = self._request(
            "GET",
            "/v2/positions",
            query={"product_id": str(product_id)},
        )
        return self._mapping_result("/v2/positions", result)

    def get_wallet_balances(self) -> tuple[Mapping[str, Any], ...]:
        result = self._request("GET", "/v2/wallet/balances")
        return self._mapping_rows("/v2/wallet/balances", result)

    def get_rate_limit_quota(self) -> Mapping[str, Any]:
        result = self._request("GET", "/v2/rate_limits/quota")
        return self._mapping_result("/v2/rate_limits/quota", result)

    def cancel_risk_increasing_orders(
        self,
        product_id: int | None = None,
    ) -> Mapping[str, Any]:
        """Pull entry limits while preserving stops and reduce-only exits.

        A panic path must not cancel the exchange-resident protection before
        reduce-only flatten orders have actually closed the position.
        """

        if product_id is not None and product_id <= 0:
            raise ValueError("product_id must be positive")
        payload: dict[str, Any] = {
            "cancel_limit_orders": True,
            "cancel_stop_orders": False,
            "cancel_reduce_only_orders": False,
        }
        if product_id is not None:
            payload["product_id"] = product_id
        return self._mapping_result(
            "/v2/orders/all",
            self._request("DELETE", "/v2/orders/all", payload),
        )

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def _request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
        *,
        query: Mapping[str, str] | None = None,
    ) -> Any:
        body = (
            ""
            if payload is None
            else json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        )
        query_string = _query_string(query)
        timestamp = str(int(self._clock()))
        prehash = f"{method}{timestamp}{path}{query_string}{body}".encode()
        signature = hmac.new(self._secret, prehash, hashlib.sha256).hexdigest()
        headers = {
            "api-key": self._api_key,
            "timestamp": timestamp,
            "signature": signature,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "vnedge-delta-safety/1",
        }
        try:
            response = self._http.request(
                method,
                f"{self._base_url}{path}{query_string}",
                content=body,
                headers=headers,
            )
            response.raise_for_status()
            decoded = response.json()
        except Exception as exc:
            raise DeltaSafetyTransportError(f"Delta {path} request failed: {exc}") from exc
        if not isinstance(decoded, dict):
            raise DeltaSafetyTransportError(f"Delta {path} returned invalid payload: {decoded}")
        if "success" in decoded and decoded.get("success") is not True:
            raise DeltaSafetyTransportError(
                f"Delta {path} returned unsuccessful payload: {decoded}"
            )
        return decoded.get("result", decoded)

    @staticmethod
    def _mapping_result(path: str, result: Any) -> Mapping[str, Any]:
        if not isinstance(result, dict):
            raise DeltaSafetyTransportError(f"Delta {path} returned invalid result: {result}")
        return result

    @staticmethod
    def _mapping_rows(path: str, result: Any) -> tuple[Mapping[str, Any], ...]:
        if not isinstance(result, list) or any(not isinstance(row, dict) for row in result):
            raise DeltaSafetyTransportError(f"Delta {path} returned invalid rows: {result}")
        return tuple(result)


class DeltaExecutionSafetyWrapper:
    """ExecutionAdapter interlock requiring dead-man + server-side protection."""

    def __init__(
        self,
        adapter: ProtectedDeltaAdapter,
        *,
        config: DeltaDeadmanConfig,
        journal: SafetyJournal,
        monotonic_clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._adapter = adapter
        self._config = config
        self._journal = journal
        self._monotonic = monotonic_clock
        self._wall_clock = wall_clock
        self._expires_monotonic = 0.0
        self._lock = asyncio.Lock()
        self._snapshot = DeltaDeadmanSnapshot(heartbeat_id=config.heartbeat_id)

    @property
    def snapshot(self) -> DeltaDeadmanSnapshot:
        if self._snapshot.armed and self._remaining_ms() <= 0:
            return replace(self._snapshot, armed=False, reason="local_ttl_expired")
        return self._snapshot

    @property
    def entry_ready(self) -> bool:
        state = self.snapshot
        return (
            state.created
            and state.armed
            and self._journal.available
            and self._remaining_ms() >= self._config.entry_guard_ms
        )

    async def start(self) -> DeltaDeadmanSnapshot:
        async with self._lock:
            try:
                await self._adapter.create_deadman(self._config)
                self._snapshot = replace(
                    self._snapshot,
                    created=True,
                    reason="created",
                )
                if not self._record(
                    "delta_deadman_created",
                    {
                        "heartbeat_id": self._config.heartbeat_id,
                        "product_symbols": list(self._config.product_symbols),
                        "ttl_ms": self._config.ttl_ms,
                    },
                ):
                    raise DeltaSafetyError("journal unavailable after heartbeat creation")
                await self._acknowledge_locked(self._config.ttl_ms)
            except Exception as exc:
                self._mark_failed(f"start_failed: {exc}")
                if isinstance(exc, DeltaSafetyError):
                    raise
                raise DeltaSafetyError(f"failed to arm Delta dead-man switch: {exc}") from exc
            return self.snapshot

    async def acknowledge(self) -> DeltaDeadmanSnapshot:
        async with self._lock:
            if not self._snapshot.created:
                raise DeltaSafetyError("dead-man heartbeat has not been created")
            try:
                await self._acknowledge_locked(self._config.ttl_ms)
            except Exception as exc:
                self._mark_failed(f"ack_failed: {exc}")
                if isinstance(exc, DeltaSafetyError):
                    raise
                raise DeltaSafetyError(f"Delta heartbeat acknowledgment failed: {exc}") from exc
            return self.snapshot

    async def run(self, stop: asyncio.Event) -> None:
        if not self._snapshot.created:
            await self.start()
        interval = self._config.acknowledge_interval_ms / 1000.0
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                try:
                    await self.acknowledge()
                except DeltaSafetyError:
                    logger.exception("Delta dead-man acknowledgment failed; entries blocked")
        await self.disable()

    async def disable(self) -> DeltaDeadmanSnapshot:
        async with self._lock:
            if not self._snapshot.created:
                return self.snapshot
            try:
                await self._adapter.acknowledge_deadman(self._config.heartbeat_id, 0)
                self._snapshot = replace(
                    self._snapshot,
                    armed=False,
                    expires_at=None,
                    reason="disabled",
                )
                self._expires_monotonic = 0.0
                self._record(
                    "delta_deadman_disabled",
                    {"heartbeat_id": self._config.heartbeat_id},
                )
            except Exception as exc:
                self._mark_failed(f"disable_failed: {exc}")
                raise DeltaSafetyError(f"failed to disable Delta heartbeat: {exc}") from exc
            return self.snapshot

    async def submit_order(self, order: ManagedOrder) -> str:
        if order.intent.reduce_only:
            return await self._adapter.submit_order(order)
        protection = order.intent.server_side_bracket
        if protection is None:
            raise AdapterRejection(
                "Delta entry refused: server-side stop/take-profit bracket is mandatory"
            )
        protection.validate_for(order.intent.side, order.intent.limit_price)
        async with self._lock:
            if not self.entry_ready:
                state = self.snapshot
                raise AdapterRejection(
                    "Delta entry refused: dead-man safety interlock is not armed "
                    f"(reason={state.reason}, remaining_ms={self._remaining_ms():.0f})"
                )
            if not self._record(
                "delta_execution_safety_authorized",
                {
                    "client_order_id": order.client_order_id,
                    "heartbeat_id": self._config.heartbeat_id,
                    "remaining_ttl_ms": round(self._remaining_ms()),
                    "server_side_bracket": asdict(protection),
                },
            ):
                raise AdapterRejection("Delta entry refused: safety journal unavailable")
            return await self._adapter.submit_protected_order(order, protection)

    async def cancel_order(self, order: ManagedOrder) -> str:
        return await self._adapter.cancel_order(order)

    async def fetch_order_status(self, order: ManagedOrder) -> dict[str, Any] | None:
        return await self._adapter.fetch_order_status(order)

    async def observe_exchange_truth(self) -> DeltaExchangeTruthSnapshot:
        """Journal a direct venue snapshot without declaring it reconciled."""

        if not self._journal.available:
            raise DeltaSafetyError("cannot observe Delta truth: safety journal unavailable")
        payload = dict(await self._adapter.fetch_exchange_truth())
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=str,
        )
        open_orders = _mapping_sequence(payload.get("open_orders"), "open_orders")
        positions = _mapping_sequence(payload.get("positions"), "positions")
        wallets = _mapping_sequence(payload.get("wallet_balances"), "wallet_balances")
        snapshot = DeltaExchangeTruthSnapshot(
            observed_at=self._wall_clock(),
            open_order_count=len(open_orders),
            non_reduce_open_order_count=sum(
                not _delta_truthy(row.get("reduce_only")) for row in open_orders
            ),
            nonzero_position_count=sum(_position_is_nonzero(row) for row in positions),
            wallet_balance_count=len(wallets),
            payload_sha256=hashlib.sha256(canonical.encode()).hexdigest(),
        )
        if not self._record(
            "delta_exchange_truth_observed",
            {"summary": asdict(snapshot), "exchange_truth": payload},
        ):
            raise DeltaSafetyError("failed to journal Delta exchange truth")
        return snapshot

    async def cancel_risk_increasing_orders(
        self,
        product_id: int | None = None,
    ) -> Mapping[str, Any]:
        """Cancel entry limits but retain all risk-reducing venue protection."""

        if not self._record(
            "delta_risk_increasing_cancel_requested",
            {"product_id": product_id, "protective_orders_preserved": True},
        ):
            raise DeltaSafetyError("cancel refused: safety journal unavailable")
        result = await self._adapter.cancel_risk_increasing_orders(product_id)
        if not self._record(
            "delta_risk_increasing_cancel_completed",
            {"product_id": product_id, "result": dict(result)},
        ):
            raise DeltaSafetyError("Delta cancel completed but could not be journaled")
        return result

    async def _acknowledge_locked(self, ttl_ms: int) -> None:
        result = await self._adapter.acknowledge_deadman(
            self._config.heartbeat_id,
            ttl_ms,
        )
        process_enabled = result.get("process_enabled", True)
        if str(process_enabled).lower() not in {"true", "1"}:
            raise DeltaSafetyError(f"exchange reports heartbeat disabled: {result}")
        now = self._wall_clock()
        expires_at = now + _milliseconds(ttl_ms)
        self._expires_monotonic = self._monotonic() + ttl_ms / 1000.0
        self._snapshot = replace(
            self._snapshot,
            armed=True,
            last_acknowledged_at=now,
            expires_at=expires_at,
            consecutive_failures=0,
            reason="armed",
        )
        if not self._record(
            "delta_deadman_armed",
            {
                "heartbeat_id": self._config.heartbeat_id,
                "ttl_ms": ttl_ms,
                "expires_at": expires_at.isoformat(),
            },
        ):
            raise DeltaSafetyError("journal unavailable after heartbeat acknowledgment")

    def _remaining_ms(self) -> float:
        return max(0.0, (self._expires_monotonic - self._monotonic()) * 1000.0)

    def _mark_failed(self, reason: str) -> None:
        self._expires_monotonic = 0.0
        self._snapshot = replace(
            self._snapshot,
            armed=False,
            expires_at=None,
            consecutive_failures=self._snapshot.consecutive_failures + 1,
            reason=reason,
        )
        self._record(
            "delta_deadman_failed",
            {
                "heartbeat_id": self._config.heartbeat_id,
                "reason": reason,
                "consecutive_failures": self._snapshot.consecutive_failures,
            },
        )

    def _record(self, kind: str, payload: dict[str, Any]) -> bool:
        if not self._journal.available:
            return False
        return bool(self._journal.append(kind, payload))


def _milliseconds(value: int) -> timedelta:
    return timedelta(milliseconds=value)


def _query_string(query: Mapping[str, str] | None) -> str:
    if not query:
        return ""
    from urllib.parse import urlencode

    return "?" + urlencode(sorted(query.items()), safe=",")


def _mapping_sequence(value: Any, field: str) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, (list, tuple)) or any(not isinstance(row, dict) for row in value):
        raise DeltaSafetyError(f"Delta exchange truth has invalid {field}")
    return tuple(value)


def _delta_truthy(value: Any) -> bool:
    return value is True or str(value).lower() in {"true", "1"}


def _position_is_nonzero(row: Mapping[str, Any]) -> bool:
    raw = row.get("size", row.get("position_size", 0))
    try:
        return float(raw or 0) != 0.0
    except (TypeError, ValueError):
        # Unknown position shape is unsafe and therefore counted as non-zero.
        return True
