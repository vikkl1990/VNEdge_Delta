"""Funding-rate history ingestion: fetch -> normalize -> gate -> Parquet."""

from __future__ import annotations

import logging
from pathlib import Path

from vnedge.data.ccxt_client import CcxtPublicClient
from vnedge.data.data_quality_gate import validate_funding, write_report
from vnedge.data.ingest_result import IngestResult
from vnedge.data.parquet_store import ParquetStore, sanitize_symbol
from vnedge.data.schemas import normalize_funding

logger = logging.getLogger(__name__)


async def ingest_funding(
    client: CcxtPublicClient,
    store: ParquetStore,
    *,
    symbol: str,
    since_ms: int,
    until_ms: int,
    reports_dir: Path | None = None,
) -> IngestResult:
    dataset = f"funding/{client.exchange_id}/{sanitize_symbol(symbol)}"
    raw = await client.fetch_funding_history(symbol, since_ms, until_ms)
    df = normalize_funding(raw)

    report = validate_funding(df, dataset=dataset)
    if reports_dir is not None:
        write_report(report, reports_dir)

    if not report.passed:
        logger.error("quality gate rejected %s: %s", dataset, report.summary)
        return IngestResult(dataset, len(df), persisted=False, report=report)

    upsert = store.upsert_funding(client.exchange_id, symbol, df)
    logger.info("%s: wrote %d new rows (total %d)", dataset, upsert.rows_added, upsert.rows_total)
    return IngestResult(
        dataset, len(df), persisted=True, report=report,
        path=upsert.path, rows_added=upsert.rows_added,
    )
