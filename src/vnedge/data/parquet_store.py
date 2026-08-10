"""Parquet historical store.

Layout (Hive-style partitioning, one file per series):

    <root>/normalized/exchange=<ex>/symbol=<sym>/timeframe=<tf>/candles.parquet
    <root>/normalized/exchange=<ex>/symbol=<sym>/funding.parquet
    <root>/normalized/exchange=<ex>/symbol=<sym>/timeframe=<tf>/open_interest.parquet

Writes are idempotent upserts: existing rows are merged with new ones,
deduplicated on timestamp (newest write wins), sorted, and written atomically
(temp file + rename) so a crash mid-write can never corrupt a dataset.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pandas as pd


def sanitize_symbol(symbol: str) -> str:
    """'BTC/USDT:USDT' -> 'BTCUSDT'. Keeps directory names filesystem-safe."""
    base = symbol.split(":")[0]
    return re.sub(r"[^A-Za-z0-9]+", "", base).upper()


@dataclass(frozen=True)
class UpsertResult:
    path: Path
    rows_added: int
    rows_total: int


class ParquetStore:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    # --- Paths ---------------------------------------------------------------
    def _series_dir(self, exchange: str, symbol: str, timeframe: str | None) -> Path:
        parts = [self.root, "normalized", f"exchange={exchange}", f"symbol={sanitize_symbol(symbol)}"]
        if timeframe is not None:
            parts.append(f"timeframe={timeframe}")
        return Path(*[str(p) for p in parts])

    def candles_path(self, exchange: str, symbol: str, timeframe: str) -> Path:
        return self._series_dir(exchange, symbol, timeframe) / "candles.parquet"

    def funding_path(self, exchange: str, symbol: str) -> Path:
        return self._series_dir(exchange, symbol, None) / "funding.parquet"

    def open_interest_path(self, exchange: str, symbol: str, timeframe: str) -> Path:
        return self._series_dir(exchange, symbol, timeframe) / "open_interest.parquet"

    # --- Upserts ---------------------------------------------------------------
    def _upsert(self, path: Path, df: pd.DataFrame) -> UpsertResult:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            existing = pd.read_parquet(path)
            before = len(existing)
            merged = pd.concat([existing, df], ignore_index=True)
            # keep="last": a re-download corrects previously stored rows
            merged = merged.drop_duplicates(subset="timestamp", keep="last")
            merged = merged.sort_values("timestamp").reset_index(drop=True)
        else:
            before = 0
            merged = df.drop_duplicates(subset="timestamp", keep="last")
            merged = merged.sort_values("timestamp").reset_index(drop=True)

        tmp = path.with_suffix(".parquet.tmp")
        merged.to_parquet(tmp, index=False)
        tmp.replace(path)  # atomic on POSIX
        return UpsertResult(path=path, rows_added=len(merged) - before, rows_total=len(merged))

    def upsert_candles(
        self, exchange: str, symbol: str, timeframe: str, df: pd.DataFrame
    ) -> UpsertResult:
        return self._upsert(self.candles_path(exchange, symbol, timeframe), df)

    def upsert_funding(self, exchange: str, symbol: str, df: pd.DataFrame) -> UpsertResult:
        return self._upsert(self.funding_path(exchange, symbol), df)

    def upsert_open_interest(
        self, exchange: str, symbol: str, timeframe: str, df: pd.DataFrame
    ) -> UpsertResult:
        return self._upsert(self.open_interest_path(exchange, symbol, timeframe), df)

    # --- Reads -----------------------------------------------------------------
    @staticmethod
    def _read(path: Path) -> pd.DataFrame:
        if not path.exists():
            raise FileNotFoundError(f"no dataset at {path}")
        return pd.read_parquet(path)

    def read_candles(self, exchange: str, symbol: str, timeframe: str) -> pd.DataFrame:
        return self._read(self.candles_path(exchange, symbol, timeframe))

    def read_funding(self, exchange: str, symbol: str) -> pd.DataFrame:
        return self._read(self.funding_path(exchange, symbol))

    def read_open_interest(self, exchange: str, symbol: str, timeframe: str) -> pd.DataFrame:
        return self._read(self.open_interest_path(exchange, symbol, timeframe))
