"""CLI for historical data downloads.

    python -m vnedge.data.download --symbols "BTC/USDT:USDT,ETH/USDT:USDT" \
        --timeframe 1h --days 90

Exit code is non-zero if any dataset failed its quality gate, so this can run
in cron/CI and fail loudly.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from ccxt.base.errors import ExchangeError, NotSupported

from vnedge.data.ccxt_client import CcxtPublicClient
from vnedge.data.candle_ingestor import ingest_candles
from vnedge.data.funding_ingestor import ingest_funding
from vnedge.data.open_interest_ingestor import ingest_open_interest
from vnedge.data.ingest_result import IngestResult
from vnedge.data.parquet_store import ParquetStore


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="VNEDGE historical data downloader")
    p.add_argument("--exchange", default="binanceusdm", help="CCXT exchange id")
    p.add_argument(
        "--symbols", default="BTC/USDT:USDT,ETH/USDT:USDT",
        help="comma-separated CCXT symbols",
    )
    p.add_argument("--timeframe", default="1h")
    p.add_argument("--days", type=int, default=90, help="lookback window in days")
    p.add_argument(
        "--since", default=None,
        help="ISO date (UTC) for range start, e.g. 2024-07-03; overrides --days",
    )
    p.add_argument(
        "--until", default=None,
        help="ISO date (UTC) for range end; default now. Historical ranges keep "
        "research data uncontaminated by hypothesis-selection data.",
    )
    p.add_argument("--data-root", default="data", help="store root directory")
    p.add_argument(
        "--allow-gaps", action="store_true",
        help="accept candle series with missing intervals (explicit opt-in)",
    )
    p.add_argument("--skip-funding", action="store_true")
    p.add_argument("--skip-oi", action="store_true")
    return p.parse_args(argv)


def _parse_utc_date_ms(value: str) -> int:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def resolve_range(args: argparse.Namespace) -> tuple[int, int]:
    """(since_ms, until_ms) from --since/--until/--days. --since wins over --days."""
    until_ms = _parse_utc_date_ms(args.until) if args.until else int(time.time() * 1000)
    if args.since:
        since_ms = _parse_utc_date_ms(args.since)
    else:
        since_ms = until_ms - args.days * 86_400_000
    if since_ms >= until_ms:
        raise ValueError(f"empty range: since {args.since} >= until {args.until}")
    return since_ms, until_ms


async def run(args: argparse.Namespace) -> tuple[list[IngestResult], list[str]]:
    """Returns (ingest results, per-dataset error strings). One dataset
    failing at the venue must not abort the rest of the run."""
    store = ParquetStore(Path(args.data_root))
    reports_dir = Path(args.data_root) / "reports" / "data_quality"
    since_ms, until_ms = resolve_range(args)
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    results: list[IngestResult] = []
    errors: list[str] = []

    async def attempt(label: str, coro) -> None:
        try:
            results.append(await coro)
        except NotSupported as exc:
            logging.warning("%s skipped (not supported): %s", label, exc)
        except ExchangeError as exc:
            logging.error("%s failed at venue: %s", label, exc)
            errors.append(f"{label}: {exc}")

    async with CcxtPublicClient(args.exchange) as client:
        for symbol in symbols:
            await attempt(
                f"candles {symbol}",
                ingest_candles(
                    client, store, symbol=symbol, timeframe=args.timeframe,
                    since_ms=since_ms, until_ms=until_ms,
                    allow_gaps=args.allow_gaps, reports_dir=reports_dir,
                ),
            )
            if not args.skip_funding:
                await attempt(
                    f"funding {symbol}",
                    ingest_funding(
                        client, store, symbol=symbol,
                        since_ms=since_ms, until_ms=until_ms, reports_dir=reports_dir,
                    ),
                )
            if not args.skip_oi:
                await attempt(
                    f"open_interest {symbol}",
                    ingest_open_interest(
                        client, store, symbol=symbol, timeframe=args.timeframe,
                        since_ms=since_ms, until_ms=until_ms, reports_dir=reports_dir,
                    ),
                )
    return results, errors


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args(argv)
    results, errors = asyncio.run(run(args))
    print("\n=== Download summary ===")
    for r in results:
        print(("  OK   " if r.persisted else "  FAIL ") + r.summary)
    for e in errors:
        print("  ERROR " + e)
    failed = [r for r in results if not r.persisted]
    if failed or errors:
        print(f"\n{len(failed)} gate rejection(s), {len(errors)} venue error(s).")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
