"""Open-interest history ingestion: fetch -> normalize -> gate -> Parquet.

Venue caveat: Binance only serves ~30 days of OI history; longer ranges
return what exists. OI series are patchy across venues, so gaps are accepted
by default here (unlike candles) — the gate still enforces structure and
value sanity.
"""

from __future__ import annotations

import logging
from pathlib import Path

from vnedge.data.ccxt_client import CcxtPublicClient
from vnedge.data.data_quality_gate import validate_open_interest, write_report
from vnedge.data.ingest_result import IngestResult
from vnedge.data.parquet_store import ParquetStore, sanitize_symbol
from vnedge.data.schemas import normalize_open_interest

logger = logging.getLogger(__name__)


async def ingest_open_interest(
    client: CcxtPublicClient,
    store: ParquetStore,
    *,
    symbol: str,
    timeframe: str,
    since_ms: int,
    until_ms: int,
    reports_dir: Path | None = None,
) -> IngestResult:
    dataset = f"open_interest/{client.exchange_id}/{sanitize_symbol(symbol)}/{timeframe}"
    raw = await client.fetch_open_interest_history(symbol, timeframe, since_ms, until_ms)
    df = normalize_open_interest(raw)

    report = validate_open_interest(df, dataset=dataset)
    if reports_dir is not None:
        write_report(report, reports_dir)

    if not report.passed:
        logger.error("quality gate rejected %s: %s", dataset, report.summary)
        return IngestResult(dataset, len(df), persisted=False, report=report)

    upsert = store.upsert_open_interest(client.exchange_id, symbol, timeframe, df)
    logger.info("%s: wrote %d new rows (total %d)", dataset, upsert.rows_added, upsert.rows_total)
    return IngestResult(
        dataset, len(df), persisted=True, report=report,
        path=upsert.path, rows_added=upsert.rows_added,
    )
