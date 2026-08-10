"""CCXT reference provider — the first implementation of the provider seam.

Adapts the existing ``CcxtPublicClient`` to :class:`MarketDataProvider`. It adds
no behavior; it exists to prove the seam is real and to give a working default
registry. When a non-CCXT source appears, it implements the same protocol and
registers alongside these — this file is the template.

Capability declarations follow the venues VNEDGE actually uses (see CLAUDE.md:
Delta has no CCXT funding history, so a CCXT Delta provider is candles-only and
funding comes from the native client). The client's own ``_require`` still gates
each call against the venue's live ``.has`` — the manifest is the *declared*
contract, ``_require`` is the runtime truth.
"""

from __future__ import annotations

from vnedge.data.ccxt_client import CcxtPublicClient
from vnedge.data.provider import (
    CAP_CANDLES,
    CAP_FUNDING,
    CAP_OPEN_INTEREST,
    MarketDataProvider,
    ProviderManifest,
    ProviderRegistry,
)

# Declared capabilities per venue. Public market data → no credentials.
_CCXT_VENUE_CAPS: dict[str, frozenset[str]] = {
    "binanceusdm": frozenset({CAP_CANDLES, CAP_FUNDING, CAP_OPEN_INTEREST}),
    "bybit": frozenset({CAP_CANDLES, CAP_FUNDING, CAP_OPEN_INTEREST}),
    # Delta via CCXT is candles-only; funding comes from the native client.
    "delta": frozenset({CAP_CANDLES}),
}
DEFAULT_CCXT_VENUES: tuple[str, ...] = ("binanceusdm", "bybit")


class CcxtProvider:
    """:class:`MarketDataProvider` backed by :class:`CcxtPublicClient`."""

    def __init__(self, exchange_id: str = "binanceusdm", *, client: object | None = None) -> None:
        self.exchange_id = exchange_id
        self.name = f"ccxt:{exchange_id}"
        # Injectable client keeps this unit-testable without a live exchange.
        self._client = client if client is not None else CcxtPublicClient(exchange_id)

    async def fetch_candles(
        self, symbol: str, timeframe: str, since_ms: int, until_ms: int
    ) -> list[list]:
        return await self._client.fetch_candles(symbol, timeframe, since_ms, until_ms)

    async def fetch_funding_history(
        self, symbol: str, since_ms: int, until_ms: int
    ) -> list[dict]:
        return await self._client.fetch_funding_history(symbol, since_ms, until_ms)

    async def fetch_open_interest_history(
        self, symbol: str, timeframe: str, since_ms: int, until_ms: int
    ) -> list[dict]:
        return await self._client.fetch_open_interest_history(symbol, timeframe, since_ms, until_ms)

    async def close(self) -> None:
        await self._client.close()


def ccxt_manifest(exchange_id: str) -> ProviderManifest:
    caps = _CCXT_VENUE_CAPS.get(exchange_id, frozenset({CAP_CANDLES}))
    return ProviderManifest(
        name=f"ccxt:{exchange_id}",
        kind="ccxt",
        venues=(exchange_id,),
        capabilities=caps,
        requires_credentials=False,  # public market data only
        notes="wraps CcxtPublicClient; venue .has still gates each call at runtime",
    )


def register_ccxt_providers(
    registry: ProviderRegistry, venues: tuple[str, ...] = DEFAULT_CCXT_VENUES
) -> None:
    for venue in venues:
        registry.register(
            ccxt_manifest(venue),
            # default-arg binds the loop variable per iteration
            lambda v=venue: CcxtProvider(v),
        )


def default_registry(venues: tuple[str, ...] = DEFAULT_CCXT_VENUES) -> ProviderRegistry:
    """A registry preloaded with the built-in CCXT providers."""
    registry = ProviderRegistry()
    register_ccxt_providers(registry, venues)
    return registry


# CcxtProvider must structurally satisfy the provider protocol.
_provider_check: type[MarketDataProvider] = CcxtProvider
