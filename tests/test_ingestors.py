"""Ingestor pipeline — gate blocks persistence, reports always written.

Uses a fake client so no test touches the network.
"""

import json

from vnedge.data.candle_ingestor import ingest_candles
from vnedge.data.funding_ingestor import ingest_funding
from vnedge.data.parquet_store import ParquetStore

BASE = 1_750_000_000_000
HOUR = 3_600_000


class FakeClient:
    """Stands in for CcxtPublicClient; returns canned raw payloads."""

    exchange_id = "binanceusdm"

    def __init__(self, candles=None, funding=None):
        self._candles = candles or []
        self._funding = funding or []

    async def fetch_candles(self, symbol, timeframe, since_ms, until_ms):
        return self._candles

    async def fetch_funding_history(self, symbol, since_ms, until_ms):
        return self._funding


def clean_raw_candles(n=24):
    return [
        [BASE + i * HOUR, 100.0, 101.0, 99.0, 100.5, 10.0] for i in range(n)
    ]


async def test_clean_candles_are_persisted(tmp_path):
    store = ParquetStore(tmp_path)
    reports = tmp_path / "reports"
    result = await ingest_candles(
        FakeClient(candles=clean_raw_candles()), store,
        symbol="BTC/USDT:USDT", timeframe="1h",
        since_ms=BASE, until_ms=BASE + 24 * HOUR, reports_dir=reports,
    )
    assert result.persisted
    assert result.rows_added == 24
    assert store.read_candles("binanceusdm", "BTC/USDT:USDT", "1h").shape[0] == 24
    assert len(list(reports.glob("*.json"))) == 1


async def test_gapped_candles_are_rejected_and_not_persisted(tmp_path):
    raw = clean_raw_candles(24)
    del raw[10]  # introduce a gap
    store = ParquetStore(tmp_path)
    reports = tmp_path / "reports"
    result = await ingest_candles(
        FakeClient(candles=raw), store,
        symbol="BTC/USDT:USDT", timeframe="1h",
        since_ms=BASE, until_ms=BASE + 24 * HOUR, reports_dir=reports,
    )
    assert not result.persisted
    assert not store.candles_path("binanceusdm", "BTC/USDT:USDT", "1h").exists()
    # rejection is documented, not silent
    report_files = list(reports.glob("*.json"))
    assert len(report_files) == 1
    payload = json.loads(report_files[0].read_text())
    assert payload["passed"] is False
    assert any(i["check"] == "gaps" for i in payload["issues"])


async def test_gapped_candles_persist_with_explicit_allow_gaps(tmp_path):
    raw = clean_raw_candles(24)
    del raw[10]
    store = ParquetStore(tmp_path)
    result = await ingest_candles(
        FakeClient(candles=raw), store,
        symbol="BTC/USDT:USDT", timeframe="1h",
        since_ms=BASE, until_ms=BASE + 24 * HOUR, allow_gaps=True,
    )
    assert result.persisted
    assert result.report.gap_count == 1


async def test_pagination_overlap_deduped_before_gate(tmp_path):
    """Overlapping pages from the venue must not trip the duplicate check."""
    raw = clean_raw_candles(24) + clean_raw_candles(24)[-3:]
    store = ParquetStore(tmp_path)
    result = await ingest_candles(
        FakeClient(candles=raw), store,
        symbol="BTC/USDT:USDT", timeframe="1h",
        since_ms=BASE, until_ms=BASE + 24 * HOUR,
    )
    assert result.persisted
    assert result.rows_added == 24


async def test_funding_pipeline(tmp_path):
    raw = [
        {"timestamp": BASE + i * 8 * HOUR, "fundingRate": 0.0001} for i in range(9)
    ]
    store = ParquetStore(tmp_path)
    result = await ingest_funding(
        FakeClient(funding=raw), store,
        symbol="BTC/USDT:USDT", since_ms=BASE, until_ms=BASE + 72 * HOUR,
    )
    assert result.persisted
    loaded = store.read_funding("binanceusdm", "BTC/USDT:USDT")
    assert len(loaded) == 9
    assert (loaded["funding_rate"] == 0.0001).all()


async def test_corrupt_funding_rejected(tmp_path):
    raw = [{"timestamp": BASE, "fundingRate": 0.9}]  # 90% per interval
    store = ParquetStore(tmp_path)
    result = await ingest_funding(
        FakeClient(funding=raw), store,
        symbol="BTC/USDT:USDT", since_ms=BASE, until_ms=BASE + HOUR,
    )
    assert not result.persisted
    assert not store.funding_path("binanceusdm", "BTC/USDT:USDT").exists()
