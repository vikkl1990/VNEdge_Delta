"""Market-data provider seam: deny-by-default, capability-gated, extensible.

Pins the two guarantees (unknown provider refused; undeclared capability refused
before any call) and proves a non-CCXT source plugs in with zero CCXT changes.
"""

import asyncio

import pytest

from vnedge.data.ccxt_provider import CcxtProvider, ccxt_manifest, default_registry
from vnedge.data.provider import (
    CAP_CANDLES,
    CAP_FUNDING,
    CAP_OPEN_INTEREST,
    MarketDataProvider,
    ProviderError,
    ProviderManifest,
    ProviderRegistry,
)


# ------------------------------------------------------------------ manifest
def test_manifest_rejects_unknown_capability():
    with pytest.raises(ValueError, match="unknown capabilities"):
        ProviderManifest("x", "custom", ("v",), frozenset({"teleport"}))


def test_manifest_declares_and_serializes():
    m = ProviderManifest("p", "custom", ("v",), frozenset({CAP_CANDLES}), notes="n")
    assert m.declares(CAP_CANDLES) and not m.declares(CAP_FUNDING)
    assert m.to_dict()["capabilities"] == [CAP_CANDLES]


# ------------------------------------------------------------------ registry
def test_registry_is_deny_by_default():
    reg = ProviderRegistry()
    with pytest.raises(ProviderError, match="unknown provider"):
        reg.create("ccxt:binanceusdm")
    with pytest.raises(ProviderError, match="unknown provider"):
        reg.manifest("nope")


def test_register_and_capability_query():
    reg = ProviderRegistry()
    reg.register(
        ProviderManifest("a", "custom", ("v",), frozenset({CAP_CANDLES})),
        lambda: _FakeProvider("a"),
    )
    reg.register(
        ProviderManifest("b", "custom", ("v",), frozenset({CAP_CANDLES, CAP_FUNDING})),
        lambda: _FakeProvider("b"),
    )
    assert reg.names() == ["a", "b"]
    assert reg.with_capability(CAP_FUNDING) == ["b"]
    reg.require_capability("b", CAP_FUNDING)  # declared → ok
    with pytest.raises(ProviderError, match="does not declare capability"):
        reg.require_capability("a", CAP_FUNDING)  # undeclared → refused


def test_double_register_is_refused():
    reg = ProviderRegistry()
    m = ProviderManifest("a", "custom", ("v",), frozenset({CAP_CANDLES}))
    reg.register(m, lambda: _FakeProvider("a"))
    with pytest.raises(ProviderError, match="already registered"):
        reg.register(m, lambda: _FakeProvider("a"))


# ------------------------------------------------------------- CCXT reference
def test_default_registry_has_builtin_ccxt_providers():
    reg = default_registry()
    assert reg.names() == ["ccxt:binanceusdm", "ccxt:bybit"]
    assert reg.manifest("ccxt:binanceusdm").kind == "ccxt"
    assert CAP_OPEN_INTEREST in reg.manifest("ccxt:bybit").capabilities
    assert reg.manifest("ccxt:binanceusdm").requires_credentials is False


def test_ccxt_delta_manifest_is_candles_only():
    # CLAUDE.md: Delta has no CCXT funding history.
    m = ccxt_manifest("delta")
    assert m.declares(CAP_CANDLES) and not m.declares(CAP_FUNDING)


def test_ccxt_provider_delegates_to_its_client():
    provider = CcxtProvider("binanceusdm", client=_RecordingClient())
    rows = asyncio.run(provider.fetch_candles("BTC/USDT", "1h", 0, 100))
    assert rows == [[1, 2, 3]]
    assert isinstance(provider, MarketDataProvider)  # structural conformance


# --------------------------------------------------- a non-CCXT source plugs in
def test_non_ccxt_provider_registers_with_zero_ccxt_changes():
    reg = default_registry()
    reg.register(
        ProviderManifest("vendor:x", "custom", ("x",), frozenset({CAP_CANDLES})),
        lambda: _FakeProvider("vendor:x"),
    )
    assert "vendor:x" in reg.names()
    provider = reg.create("vendor:x")
    assert asyncio.run(provider.fetch_candles("BTC/USDT", "1h", 0, 9)) == [["fake"]]


class _FakeProvider:
    def __init__(self, name):
        self.name = name

    async def fetch_candles(self, *a):
        return [["fake"]]

    async def fetch_funding_history(self, *a):
        return []

    async def fetch_open_interest_history(self, *a):
        return []

    async def close(self):
        return None


class _RecordingClient:
    async def fetch_candles(self, *a):
        return [[1, 2, 3]]

    async def fetch_funding_history(self, *a):
        return []

    async def fetch_open_interest_history(self, *a):
        return []

    async def close(self):
        return None


# ------------------------------------------------------- Fetcher (three-stage)
def test_apply_aliases_renames_and_passes_through():
    from vnedge.data.provider import apply_aliases

    out = apply_aliases({"t": 1, "o": 2, "keep": 3}, {"t": "timestamp", "o": "open"})
    assert out == {"timestamp": 1, "open": 2, "keep": 3}


def test_fetcher_runs_the_three_stages_in_order():
    import asyncio

    from vnedge.data.provider import Fetcher, apply_aliases

    calls = []

    class DemoFetcher(Fetcher):
        @staticmethod
        def transform_query(params):
            calls.append("q")
            return {"symbol": params["symbol"].upper()}

        @staticmethod
        async def extract(query):
            calls.append("e")
            return [{"t": 1, "px": 100}]  # raw source shape

        @staticmethod
        def transform_data(query, raw):
            calls.append("d")
            return [apply_aliases(r, {"px": "price"}) | {"symbol": query["symbol"]} for r in raw]

    out = asyncio.run(DemoFetcher.fetch({"symbol": "btc"}))
    assert calls == ["q", "e", "d"]
    assert out == [{"t": 1, "price": 100, "symbol": "BTC"}]
