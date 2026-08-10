import json
from pathlib import Path

from vnedge.data.context_backfill import (
    BackfillChunk,
    build_chunks,
    chunk_is_covered,
    chunk_ranges,
    parse_args,
    parse_day_map,
    resolve_manifest_path,
    run_backfill,
)
from vnedge.data.parquet_store import ParquetStore
from vnedge.data.schemas import normalize_candles
from vnedge.research.universe import ResearchTarget, discover_exchange_targets


BASE = 1_735_689_600_000  # 2025-01-01T00:00:00Z
DAY = 86_400_000
FIFTEEN_MINUTES = 900_000


class FakeClient:
    def __init__(self, exchange_id: str, candles: list[list]) -> None:
        self.exchange_id = exchange_id
        self.candles = candles
        self.calls: list[tuple[str, str, int, int]] = []

    async def __aenter__(self) -> "FakeClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def fetch_candles(
        self,
        symbol: str,
        timeframe: str,
        since_ms: int,
        until_ms: int,
    ) -> list[list]:
        self.calls.append((symbol, timeframe, since_ms, until_ms))
        return [row for row in self.candles if since_ms <= row[0] < until_ms]


def clean_15m_candles(*, days: int = 1) -> list[list]:
    rows = int(days * DAY / FIFTEEN_MINUTES)
    return [
        [BASE + i * FIFTEEN_MINUTES, 100.0, 101.0, 99.0, 100.5, 10.0]
        for i in range(rows)
    ]


def test_day_map_requires_every_selected_timeframe():
    assert parse_day_map("1m=3,15m=14", allowed=("1m", "15m")) == {"1m": 3, "15m": 14}

    try:
        parse_day_map("1m=3", allowed=("1m", "15m"))
    except ValueError as exc:
        assert "missing day map entries" in str(exc)
    else:
        raise AssertionError("missing timeframe was accepted")


def test_chunk_ranges_and_target_expansion_are_deterministic():
    ranges = chunk_ranges(BASE, BASE + 5 * DAY, chunk_days=2)
    assert ranges == (
        (BASE, BASE + 2 * DAY),
        (BASE + 2 * DAY, BASE + 4 * DAY),
        (BASE + 4 * DAY, BASE + 5 * DAY),
    )

    chunks = build_chunks(
        (ResearchTarget("binanceusdm", "BTC/USDT:USDT"),),
        timeframes=("4h", "15m"),
        timeframe_days={"4h": 10, "15m": 5},
        chunk_days={"4h": 5, "15m": 2},
        until_ms=BASE + 10 * DAY,
    )
    assert len(chunks) == 5
    assert chunks[0].dataset_id == "binanceusdm|BTC/USDT:USDT|4h"
    assert chunks[-1].dataset_id == "binanceusdm|BTC/USDT:USDT|15m"


def test_manifest_path_defaults_under_data_root(tmp_path):
    data_root = tmp_path / "data"
    assert resolve_manifest_path("manifest.json", data_root) == (
        data_root / "reports" / "context_backfill" / "manifest.json"
    )
    assert resolve_manifest_path("data/reports/context_backfill/manifest.json", data_root) == (
        data_root / "reports" / "context_backfill" / "manifest.json"
    )
    assert resolve_manifest_path("custom/context.json", data_root) == data_root / "custom/context.json"


async def test_existing_parquet_coverage_is_manifested_without_refetch(tmp_path):
    store = ParquetStore(tmp_path)
    store.upsert_candles(
        "binanceusdm",
        "BTC/USDT:USDT",
        "15m",
        normalize_candles(clean_15m_candles(days=1)),
    )
    client = FakeClient("binanceusdm", candles=[])

    args = parse_args(
        [
            "--data-root",
            str(tmp_path),
            "--exchanges",
            "binanceusdm",
            "--symbols",
            "BTC/USDT:USDT",
            "--timeframes",
            "15m",
            "--timeframe-days",
            "15m=1",
            "--chunk-days",
            "15m=1",
            "--since",
            "2025-01-01T00:00:00+00:00",
            "--until",
            "2025-01-02T00:00:00+00:00",
        ]
    )

    summary = await run_backfill(args, client_factory=lambda _: client)

    assert summary.ok
    assert summary.chunks_fetched == 0
    assert summary.chunks_skipped_covered == 1
    assert client.calls == []

    manifest = json.loads(Path(summary.manifest_path).read_text())
    completed = list(manifest["completed_chunks"].values())
    assert completed[0]["status"] == "covered_existing"
    assert manifest["datasets"]["binanceusdm|BTC/USDT:USDT|15m"]["rows"] == 96


def test_coverage_detection_tolerates_mid_candle_rolling_boundaries(tmp_path):
    store = ParquetStore(tmp_path)
    store.upsert_candles(
        "binanceusdm",
        "BTC/USDT:USDT",
        "15m",
        normalize_candles(clean_15m_candles(days=1)),
    )

    shifted = BackfillChunk(
        "binanceusdm",
        "BTC/USDT:USDT",
        "15m",
        BASE + 13_000,
        BASE + DAY + 13_000,
    )

    assert chunk_is_covered(store, shifted)


async def test_fetched_chunks_are_checkpointed_and_skipped_on_rerun(tmp_path):
    client = FakeClient("binanceusdm", candles=clean_15m_candles(days=1))
    args = parse_args(
        [
            "--data-root",
            str(tmp_path),
            "--exchanges",
            "binanceusdm",
            "--symbols",
            "BTC/USDT:USDT",
            "--timeframes",
            "15m",
            "--timeframe-days",
            "15m=1",
            "--chunk-days",
            "15m=1",
            "--since",
            "2025-01-01T00:00:00+00:00",
            "--until",
            "2025-01-02T00:00:00+00:00",
        ]
    )

    first = await run_backfill(args, client_factory=lambda _: client)
    second_client = FakeClient("binanceusdm", candles=clean_15m_candles(days=1))
    second = await run_backfill(args, client_factory=lambda _: second_client)

    assert first.ok
    assert first.chunks_fetched == 1
    assert first.rows_added == 96
    assert len(client.calls) == 1

    assert second.ok
    assert second.chunks_fetched == 0
    assert second.chunks_skipped_completed == 1
    assert second_client.calls == []


async def test_delta_india_discovery_uses_ccxt_alias_without_losing_label(monkeypatch):
    class FakeExchange:
        def __init__(self, exchange_id: str) -> None:
            self.exchange_id = exchange_id

        async def load_markets(self) -> dict:
            return {
                "BTC/USD:USD": {
                    "symbol": "BTC/USD:USD",
                    "active": True,
                    "swap": True,
                    "linear": True,
                    "quote": "USD",
                    "settle": "USD",
                }
            }

        async def close(self) -> None:
            return None

    created: list[str] = []

    def fake_create(exchange_id: str) -> FakeExchange:
        created.append(exchange_id)
        return FakeExchange(exchange_id)

    monkeypatch.setattr("vnedge.data.ccxt_client.create_ccxt_async_exchange", fake_create)

    targets = await discover_exchange_targets("delta_india", timeframe="15m")

    assert created == ["delta_india"]
    assert targets == (ResearchTarget("delta_india", "BTC/USD:USD", "15m"),)
