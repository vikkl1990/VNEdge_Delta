"""Candle ingestion: fetch -> normalize -> quality gate -> Parquet.

The gate is a hard barrier: rejected data is never written to the store, only
its quality report is persisted. There is no force flag by design — if a
dataset needs gaps accepted, that is the caller's explicit ``allow_gaps``.
"""

from __future__ import annotations

import logging
from pathlib import Path

from vnedge.data.ccxt_client import CcxtPublicClient
from vnedge.data.data_quality_gate import validate_candles, write_report
from vnedge.data.ingest_result import IngestResult
from vnedge.data.parquet_store import ParquetStore, sanitize_symbol
from vnedge.data.schemas import normalize_candles

logger = logging.getLogger(__name__)


async def ingest_candles(
    client: CcxtPublicClient,
    store: ParquetStore,
    *,
    symbol: str,
    timeframe: str,
    since_ms: int,
    until_ms: int,
    allow_gaps: bool = False,
    reports_dir: Path | None = None,
) -> IngestResult:
    dataset = f"candles/{client.exchange_id}/{sanitize_symbol(symbol)}/{timeframe}"
    raw = await client.fetch_candles(symbol, timeframe, since_ms, until_ms)
    df = normalize_candles(raw)

    report = validate_candles(df, timeframe, allow_gaps=allow_gaps, dataset=dataset)
    if reports_dir is not None:
        write_report(report, reports_dir)

    if not report.passed:
        logger.error("quality gate rejected %s: %s", dataset, report.summary)
        return IngestResult(dataset, len(df), persisted=False, report=report)

    upsert = store.upsert_candles(client.exchange_id, symbol, timeframe, df)
    logger.info("%s: wrote %d new rows (total %d)", dataset, upsert.rows_added, upsert.rows_total)
    return IngestResult(
        dataset, len(df), persisted=True, report=report,
        path=upsert.path, rows_added=upsert.rows_added,
    )
