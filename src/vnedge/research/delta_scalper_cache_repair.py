"""Audit and safely repair missing 1m rows in Delta scalper cache shards.

The command is intentionally narrow: it only fills timestamps absent from an
existing cache shard, validates the complete expected minute index, and then
atomically replaces the parquet file. Existing rows always win on overlap.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

import pandas as pd

from vnedge.data.delta_native_history import fetch_delta_candle_history

_SHARD_RE = re.compile(
    r"^(?P<symbol>[A-Z0-9]+)_1m_"
    r"(?P<start>\d{8}T\d{4})_(?P<end>\d{8}T\d{4})\.parquet$"
)
_COLUMNS = ("timestamp", "open", "high", "low", "close", "volume")

Fetch = Callable[..., Awaitable[pd.DataFrame]]


@dataclass(frozen=True)
class CacheRepairResult:
    path: str
    symbol: str
    start: str
    end: str
    rows_before: int
    rows_after: int
    missing_before: int
    missing_after: int
    fetched_rows: int
    repaired: bool


def parse_shard_path(path: Path) -> tuple[str, pd.Timestamp, pd.Timestamp]:
    match = _SHARD_RE.match(path.name)
    if match is None:
        raise ValueError(f"unrecognised 1m cache shard name: {path.name}")
    return (
        match.group("symbol"),
        pd.to_datetime(match.group("start"), format="%Y%m%dT%H%M", utc=True),
        pd.to_datetime(match.group("end"), format="%Y%m%dT%H%M", utc=True),
    )


def normalize_frame(frame: pd.DataFrame) -> pd.DataFrame:
    missing_columns = set(_COLUMNS) - set(frame.columns)
    if missing_columns:
        raise ValueError(f"cache shard missing columns: {sorted(missing_columns)}")
    out = frame.loc[:, _COLUMNS].copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True)
    for column in _COLUMNS[1:]:
        out[column] = pd.to_numeric(out[column], errors="coerce")
    if out[list(_COLUMNS)].isna().any(axis=None):
        raise ValueError("cache shard contains null or non-numeric OHLCV data")
    return out.drop_duplicates("timestamp", keep="last").sort_values("timestamp").reset_index(drop=True)


def missing_minutes(
    frame: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp
) -> pd.DatetimeIndex:
    expected = pd.date_range(start=start, end=end, freq="1min", inclusive="left")
    observed = pd.DatetimeIndex(frame["timestamp"])
    return expected.difference(observed)


def contiguous_ranges(missing: Iterable[pd.Timestamp]) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    values = list(missing)
    if not values:
        return []
    groups: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    start = previous = values[0]
    for current in values[1:]:
        if current - previous != pd.Timedelta(minutes=1):
            groups.append((start, previous + pd.Timedelta(minutes=1)))
            start = current
        previous = current
    groups.append((start, previous + pd.Timedelta(minutes=1)))
    return groups


def _atomic_parquet(path: Path, frame: pd.DataFrame) -> None:
    with NamedTemporaryFile(dir=path.parent, suffix=".parquet", delete=False) as handle:
        temporary = Path(handle.name)
    try:
        frame.to_parquet(temporary, index=False)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


async def repair_cache_file(
    path: Path,
    *,
    fetch: Fetch = fetch_delta_candle_history,
) -> CacheRepairResult:
    symbol, start, end = parse_shard_path(path)
    original = normalize_frame(pd.read_parquet(path))
    before = missing_minutes(original, start, end)
    fetched: list[pd.DataFrame] = []
    for gap_start, gap_end in contiguous_ranges(before):
        # Delta can return an empty result when a query starts and ends exactly
        # on an exchange outage/recovery boundary. One known-good neighbour on
        # each side makes that same public range retrievable; overlap is safe
        # because existing cached rows win during the merge below.
        fetch_start = max(start, gap_start - pd.Timedelta(minutes=1))
        fetch_end = min(end, gap_end + pd.Timedelta(minutes=1))
        fetched.append(
            await fetch(
                symbol,
                resolution="1m",
                start_s=int(fetch_start.timestamp()),
                end_s=int(fetch_end.timestamp()),
            )
        )
    additions = normalize_frame(pd.concat(fetched, ignore_index=True)) if fetched else original.iloc[0:0]
    # Existing cached observations are authoritative if the API overlaps them.
    merged = normalize_frame(pd.concat([additions, original], ignore_index=True))
    merged = merged.loc[(merged["timestamp"] >= start) & (merged["timestamp"] < end)].reset_index(drop=True)
    after = missing_minutes(merged, start, end)
    if len(after):
        raise RuntimeError(
            f"repair incomplete for {path.name}: {len(after)} minute(s) still missing"
        )
    repaired = bool(len(before))
    if repaired:
        _atomic_parquet(path, merged)
    return CacheRepairResult(
        path=str(path),
        symbol=symbol,
        start=start.isoformat(),
        end=end.isoformat(),
        rows_before=len(original),
        rows_after=len(merged),
        missing_before=len(before),
        missing_after=len(after),
        fetched_rows=len(additions),
        repaired=repaired,
    )


async def run(paths: list[Path], manifest: Path) -> list[CacheRepairResult]:
    results = [await repair_cache_file(path) for path in paths]
    manifest.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "results": [asdict(result) for result in results],
    }
    with NamedTemporaryFile("w", dir=manifest.parent, delete=False, encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(manifest)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("research/live_research/delta_scalper_cache_repair_latest.json"),
    )
    args = parser.parse_args()
    results = asyncio.run(run(args.paths, args.manifest))
    print(json.dumps([asdict(result) for result in results], indent=2))


if __name__ == "__main__":
    main()
