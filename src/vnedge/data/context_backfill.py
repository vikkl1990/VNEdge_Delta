"""Chunked, idempotent context-lane backfill.

This is the data builder for research/scalper context lanes. It fills and
keeps 4h/1h/15m/1m candle Parquet datasets across the configured research
universe, checkpointing every chunk so reruns skip already-covered data.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Iterable

import pandas as pd
from ccxt.base.errors import ExchangeError, NotSupported

from vnedge.data.candle_ingestor import ingest_candles
from vnedge.data.ccxt_client import CcxtPublicClient
from vnedge.data.parquet_store import ParquetStore
from vnedge.data.schemas import TIMEFRAME_MS
from vnedge.research.universe import (
    DEFAULT_DERIVATIVE_QUOTES,
    DEFAULT_EXCHANGES,
    DEFAULT_SYMBOLS,
    ResearchTarget,
    discover_research_targets,
    load_research_targets,
)


logger = logging.getLogger(__name__)

REQUIRED_CONTEXT_TIMEFRAMES: tuple[str, ...] = ("4h", "1h", "15m", "1m")
DEFAULT_TIMEFRAME_DAYS: dict[str, int] = {
    "4h": 365,
    "1h": 365,
    "15m": 180,
    "1m": 60,
}
DEFAULT_CHUNK_DAYS: dict[str, int] = {
    "4h": 90,
    "1h": 30,
    "15m": 14,
    "1m": 3,
}
DEFAULT_MANIFEST = "data/reports/context_backfill/manifest.json"


@dataclass(frozen=True)
class BackfillChunk:
    exchange: str
    symbol: str
    timeframe: str
    since_ms: int
    until_ms: int

    @property
    def dataset_id(self) -> str:
        return f"{self.exchange}|{self.symbol}|{self.timeframe}"

    @property
    def chunk_id(self) -> str:
        return (
            f"{self.exchange}|{self.symbol}|{self.timeframe}|"
            f"{self.since_ms}-{self.until_ms}"
        )


@dataclass(frozen=True)
class BackfillDatasetSummary:
    exchange: str
    symbol: str
    timeframe: str
    chunks_total: int = 0
    chunks_fetched: int = 0
    chunks_skipped_completed: int = 0
    chunks_skipped_covered: int = 0
    chunks_failed: int = 0
    rows_added: int = 0
    first_timestamp: str | None = None
    last_timestamp: str | None = None
    path: str | None = None

    @property
    def dataset_id(self) -> str:
        return f"{self.exchange}|{self.symbol}|{self.timeframe}"


@dataclass(frozen=True)
class BackfillRunSummary:
    generated_at: str
    data_root: str
    manifest_path: str
    targets: int
    datasets: int
    chunks_total: int
    chunks_fetched: int
    chunks_skipped_completed: int
    chunks_skipped_covered: int
    chunks_failed: int
    rows_added: int
    dataset_summaries: tuple[BackfillDatasetSummary, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.chunks_failed == 0

    def to_dict(self) -> dict:
        return asdict(self)


ClientFactory = Callable[[str], CcxtPublicClient]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build research context candle lanes in chunks")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST)
    parser.add_argument("--exchanges", default=",".join(DEFAULT_EXCHANGES))
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    parser.add_argument("--timeframes", default=",".join(REQUIRED_CONTEXT_TIMEFRAMES))
    parser.add_argument(
        "--discover-active",
        action="store_true",
        help="discover active derivative markets instead of using --symbols",
    )
    parser.add_argument("--quote-assets", default=",".join(DEFAULT_DERIVATIVE_QUOTES))
    parser.add_argument("--max-symbols-per-exchange", type=int)
    parser.add_argument("--include-inactive", action="store_true")
    parser.add_argument(
        "--timeframe-days",
        default=_mapping_to_cli(DEFAULT_TIMEFRAME_DAYS),
        help="comma map, e.g. 1m=60,15m=180,1h=365,4h=365",
    )
    parser.add_argument(
        "--chunk-days",
        default=_mapping_to_cli(DEFAULT_CHUNK_DAYS),
        help="comma map, e.g. 1m=3,15m=14,1h=30,4h=90",
    )
    parser.add_argument("--since", help="UTC ISO start applied to all timeframes")
    parser.add_argument("--until", help="UTC ISO end; default now")
    parser.add_argument("--allow-gaps", action="store_true")
    parser.add_argument("--max-datasets", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def _mapping_to_cli(values: dict[str, int]) -> str:
    return ",".join(f"{key}={value}" for key, value in values.items())


def parse_csv(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def parse_day_map(raw: str, *, allowed: Iterable[str]) -> dict[str, int]:
    allowed_set = set(allowed)
    out: dict[str, int] = {}
    for item in parse_csv(raw):
        if "=" not in item:
            raise ValueError(f"expected key=value in day map entry: {item!r}")
        key, value = (part.strip() for part in item.split("=", maxsplit=1))
        if key not in allowed_set:
            raise ValueError(f"unknown timeframe in day map: {key!r}")
        days = int(value)
        if days <= 0:
            raise ValueError(f"days for {key} must be positive")
        out[key] = days
    missing = allowed_set - set(out)
    if missing:
        raise ValueError(f"missing day map entries for: {sorted(missing)}")
    return out


def parse_utc_ms(value: str | None) -> int:
    if value is None:
        return int(time.time() * 1000)
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def chunk_ranges(since_ms: int, until_ms: int, *, chunk_days: int) -> tuple[tuple[int, int], ...]:
    if since_ms >= until_ms:
        raise ValueError("since must be before until")
    step_ms = chunk_days * 86_400_000
    ranges: list[tuple[int, int]] = []
    start = since_ms
    while start < until_ms:
        end = min(start + step_ms, until_ms)
        ranges.append((start, end))
        start = end
    return tuple(ranges)


def _ceil_to_step(value_ms: int, step_ms: int) -> int:
    return ((value_ms + step_ms - 1) // step_ms) * step_ms


def _floor_to_step(value_ms: int, step_ms: int) -> int:
    return (value_ms // step_ms) * step_ms


def build_chunks(
    targets: Iterable[ResearchTarget],
    *,
    timeframes: tuple[str, ...],
    timeframe_days: dict[str, int],
    chunk_days: dict[str, int],
    until_ms: int,
    since_ms: int | None = None,
) -> tuple[BackfillChunk, ...]:
    chunks: list[BackfillChunk] = []
    for target in targets:
        for timeframe in timeframes:
            if timeframe not in TIMEFRAME_MS:
                raise ValueError(f"unknown timeframe: {timeframe}")
            start_ms = since_ms if since_ms is not None else until_ms - timeframe_days[timeframe] * 86_400_000
            for start, end in chunk_ranges(start_ms, until_ms, chunk_days=chunk_days[timeframe]):
                chunks.append(BackfillChunk(target.exchange, target.symbol, timeframe, start, end))
    return tuple(chunks)


def load_manifest(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "completed_chunks": {}, "datasets": {}}
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError:
        logger.warning("ignoring corrupt context backfill manifest: %s", path)
        return {"version": 1, "completed_chunks": {}, "datasets": {}}
    payload.setdefault("version", 1)
    payload.setdefault("completed_chunks", {})
    payload.setdefault("datasets", {})
    return payload


def resolve_manifest_path(raw: str, data_root: Path) -> Path:
    path = Path(raw)
    if path.is_absolute():
        return path
    if path.parts[:3] == ("data", "reports", "context_backfill"):
        return data_root / "reports" / "context_backfill" / path.name
    if len(path.parts) > 1:
        return data_root / path
    return data_root / "reports" / "context_backfill" / path


def write_manifest(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload["updated_at"] = datetime.now(UTC).isoformat()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    tmp.replace(path)


def dataset_coverage(
    store: ParquetStore,
    exchange: str,
    symbol: str,
    timeframe: str,
) -> tuple[int, int, int, Path] | None:
    path = store.candles_path(exchange, symbol, timeframe)
    if not path.exists():
        return None
    frame = pd.read_parquet(path, columns=["timestamp"])
    if frame.empty:
        return None
    first_ms = int(frame["timestamp"].min().timestamp() * 1000)
    last_ms = int(frame["timestamp"].max().timestamp() * 1000)
    return first_ms, last_ms, len(frame), path


def chunk_is_covered(
    store: ParquetStore,
    chunk: BackfillChunk,
    *,
    allow_gaps: bool = False,
) -> bool:
    path = store.candles_path(chunk.exchange, chunk.symbol, chunk.timeframe)
    if not path.exists():
        return False
    frame = pd.read_parquet(path, columns=["timestamp"])
    if frame.empty:
        return False
    timestamps = frame["timestamp"].drop_duplicates().sort_values()
    first_ms = int(timestamps.min().timestamp() * 1000)
    last_ms = int(timestamps.max().timestamp() * 1000)
    step_ms = TIMEFRAME_MS[chunk.timeframe]
    first_expected_open = _ceil_to_step(chunk.since_ms, step_ms)
    last_expected_open = _floor_to_step(chunk.until_ms - step_ms, step_ms)
    if last_expected_open < first_expected_open:
        return False
    if first_ms > first_expected_open or last_ms < last_expected_open:
        return False
    if allow_gaps:
        return True
    start = pd.to_datetime(first_expected_open, unit="ms", utc=True)
    end = pd.to_datetime(last_expected_open, unit="ms", utc=True)
    subset = timestamps[(timestamps >= start) & (timestamps <= end)]
    expected_rows = ((last_expected_open - first_expected_open) // step_ms) + 1
    return len(subset) >= expected_rows


async def run_backfill(
    args: argparse.Namespace,
    *,
    client_factory: ClientFactory = CcxtPublicClient,
) -> BackfillRunSummary:
    data_root = Path(args.data_root)
    store = ParquetStore(data_root)
    manifest_path = resolve_manifest_path(args.manifest, data_root)
    reports_dir = data_root / "reports" / "data_quality"

    timeframes = parse_csv(args.timeframes)
    timeframe_days = parse_day_map(args.timeframe_days, allowed=timeframes)
    chunk_days = parse_day_map(args.chunk_days, allowed=timeframes)
    until_ms = parse_utc_ms(args.until)
    since_ms = parse_utc_ms(args.since) if args.since else None
    exchanges = parse_csv(args.exchanges)
    if args.discover_active:
        targets = await discover_research_targets(
            exchanges,
            timeframe="1h",
            quote_assets=parse_csv(args.quote_assets),
            active_only=not args.include_inactive,
            max_symbols_per_exchange=args.max_symbols_per_exchange,
        )
    else:
        targets = load_research_targets(
            exchanges=exchanges,
            symbols=parse_csv(args.symbols),
            timeframe="1h",
        )
    if args.max_datasets is not None:
        wanted = args.max_datasets
        filtered: list[ResearchTarget] = []
        seen = 0
        for target in targets:
            if seen >= wanted:
                break
            filtered.append(target)
            seen += len(timeframes)
        targets = tuple(filtered)

    chunks = build_chunks(
        targets,
        timeframes=timeframes,
        timeframe_days=timeframe_days,
        chunk_days=chunk_days,
        until_ms=until_ms,
        since_ms=since_ms,
    )
    manifest = load_manifest(manifest_path)
    completed = manifest.setdefault("completed_chunks", {})
    dataset_acc: dict[str, dict] = {}

    if args.dry_run:
        return _summary(data_root, manifest_path, targets, chunks, dataset_acc)

    by_exchange: dict[str, list[BackfillChunk]] = {}
    for chunk in chunks:
        by_exchange.setdefault(chunk.exchange, []).append(chunk)

    for exchange, exchange_chunks in by_exchange.items():
        async with client_factory(exchange) as client:
            for chunk in exchange_chunks:
                acc = dataset_acc.setdefault(chunk.dataset_id, _blank_dataset_acc(chunk))
                acc["chunks_total"] += 1
                if chunk.chunk_id in completed:
                    acc["chunks_skipped_completed"] += 1
                    continue
                if chunk_is_covered(store, chunk, allow_gaps=args.allow_gaps):
                    completed[chunk.chunk_id] = _completed_payload(chunk, status="covered_existing")
                    acc["chunks_skipped_covered"] += 1
                    _refresh_dataset_manifest(store, manifest, chunk)
                    write_manifest(manifest_path, manifest)
                    continue
                try:
                    result = await ingest_candles(
                        client,
                        store,
                        symbol=chunk.symbol,
                        timeframe=chunk.timeframe,
                        since_ms=chunk.since_ms,
                        until_ms=chunk.until_ms,
                        allow_gaps=args.allow_gaps,
                        reports_dir=reports_dir,
                    )
                except (ExchangeError, NotSupported) as exc:
                    logger.error("%s failed at venue: %s", chunk.chunk_id, exc)
                    acc["chunks_failed"] += 1
                    continue
                if not result.persisted:
                    acc["chunks_failed"] += 1
                    continue
                acc["chunks_fetched"] += 1
                acc["rows_added"] += result.rows_added or 0
                completed[chunk.chunk_id] = _completed_payload(
                    chunk,
                    status="fetched",
                    rows_added=result.rows_added or 0,
                )
                _refresh_dataset_manifest(store, manifest, chunk)
                write_manifest(manifest_path, manifest)

    return _summary(data_root, manifest_path, targets, chunks, dataset_acc)


def _blank_dataset_acc(chunk: BackfillChunk) -> dict:
    return {
        "exchange": chunk.exchange,
        "symbol": chunk.symbol,
        "timeframe": chunk.timeframe,
        "chunks_total": 0,
        "chunks_fetched": 0,
        "chunks_skipped_completed": 0,
        "chunks_skipped_covered": 0,
        "chunks_failed": 0,
        "rows_added": 0,
    }


def _completed_payload(chunk: BackfillChunk, *, status: str, rows_added: int = 0) -> dict:
    return {
        "dataset_id": chunk.dataset_id,
        "exchange": chunk.exchange,
        "symbol": chunk.symbol,
        "timeframe": chunk.timeframe,
        "since_ms": chunk.since_ms,
        "until_ms": chunk.until_ms,
        "status": status,
        "rows_added": rows_added,
        "completed_at": datetime.now(UTC).isoformat(),
    }


def _refresh_dataset_manifest(store: ParquetStore, manifest: dict, chunk: BackfillChunk) -> None:
    coverage = dataset_coverage(store, chunk.exchange, chunk.symbol, chunk.timeframe)
    if coverage is None:
        return
    first_ms, last_ms, rows, path = coverage
    manifest.setdefault("datasets", {})[chunk.dataset_id] = {
        "exchange": chunk.exchange,
        "symbol": chunk.symbol,
        "timeframe": chunk.timeframe,
        "rows": rows,
        "first_timestamp": datetime.fromtimestamp(first_ms / 1000, UTC).isoformat(),
        "last_timestamp": datetime.fromtimestamp(last_ms / 1000, UTC).isoformat(),
        "path": str(path),
    }


def _summary(
    data_root: Path,
    manifest_path: Path,
    targets: tuple[ResearchTarget, ...],
    chunks: tuple[BackfillChunk, ...],
    dataset_acc: dict[str, dict],
) -> BackfillRunSummary:
    summaries = tuple(
        BackfillDatasetSummary(**payload)
        for payload in sorted(dataset_acc.values(), key=lambda p: (p["exchange"], p["symbol"], p["timeframe"]))
    )
    return BackfillRunSummary(
        generated_at=datetime.now(UTC).isoformat(),
        data_root=str(data_root),
        manifest_path=str(manifest_path),
        targets=len(targets),
        datasets=len({c.dataset_id for c in chunks}),
        chunks_total=sum(s.chunks_total for s in summaries) or len(chunks),
        chunks_fetched=sum(s.chunks_fetched for s in summaries),
        chunks_skipped_completed=sum(s.chunks_skipped_completed for s in summaries),
        chunks_skipped_covered=sum(s.chunks_skipped_covered for s in summaries),
        chunks_failed=sum(s.chunks_failed for s in summaries),
        rows_added=sum(s.rows_added for s in summaries),
        dataset_summaries=summaries,
    )


def render_summary(summary: BackfillRunSummary) -> str:
    lines = [
        "=== Context data backfill ===",
        f"generated: {summary.generated_at}",
        f"targets={summary.targets} datasets={summary.datasets} chunks={summary.chunks_total}",
        (
            "chunks: "
            f"fetched={summary.chunks_fetched}, "
            f"covered={summary.chunks_skipped_covered}, "
            f"completed={summary.chunks_skipped_completed}, "
            f"failed={summary.chunks_failed}"
        ),
        f"rows_added={summary.rows_added}",
        f"manifest={summary.manifest_path}",
    ]
    for item in summary.dataset_summaries[:40]:
        lines.append(
            f"  {item.exchange} {item.symbol} {item.timeframe}: "
            f"fetched={item.chunks_fetched} covered={item.chunks_skipped_covered} "
            f"completed={item.chunks_skipped_completed} failed={item.chunks_failed} "
            f"rows_added={item.rows_added}"
        )
    if len(summary.dataset_summaries) > 40:
        lines.append(f"  ... {len(summary.dataset_summaries) - 40} more datasets")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args(argv)
    try:
        summary = asyncio.run(run_backfill(args))
    except Exception as exc:  # noqa: BLE001
        logger.exception("context backfill failed: %s", exc)
        return 1
    if args.json:
        print(json.dumps(summary.to_dict(), indent=2, default=str))
    else:
        print(render_summary(summary))
    return 0 if summary.ok else 1


if __name__ == "__main__":
    sys.exit(main())
