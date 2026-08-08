"""Research-only Polars adapters for large causal candle datasets.

The live and shadow decision paths intentionally remain on ``Candle`` and the
existing event-driven store. Batch frames produced here carry an explicit
``confirmed_at`` timestamp for future-dependent research annotations such as
right-confirmed swings.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from vnedge.scalping.delta_engine.types import Candle

if TYPE_CHECKING:
    import polars as pl


def _polars() -> Any:
    try:
        import polars as pl
    except ModuleNotFoundError as exc:  # pragma: no cover - exercised without extra
        raise RuntimeError(
            "Polars research support is optional; install with "
            "`pip install -e '.[quant-research]'`"
        ) from exc
    return pl


def candle_schema() -> dict[str, Any]:
    pl = _polars()
    return {
        "ts": pl.Datetime("us", "UTC"),
        "symbol": pl.String,
        "timeframe": pl.String,
        "open": pl.Float64,
        "high": pl.Float64,
        "low": pl.Float64,
        "close": pl.Float64,
        "volume": pl.Float64,
    }


def candles_to_polars(candles: Iterable[Candle], *, symbol: str) -> pl.DataFrame:
    pl = _polars()
    rows = tuple(candles)
    native = symbol.upper()
    frame = pl.DataFrame(
        {
            "ts": [row.ts for row in rows],
            "symbol": [native] * len(rows),
            "timeframe": [row.tf for row in rows],
            "open": [float(row.open) for row in rows],
            "high": [float(row.high) for row in rows],
            "low": [float(row.low) for row in rows],
            "close": [float(row.close) for row in rows],
            "volume": [float(row.volume) for row in rows],
        },
        schema=candle_schema(),
    )
    return frame.sort(["symbol", "timeframe", "ts"])


def polars_to_candles(frame: pl.DataFrame) -> list[Candle]:
    required = {"ts", "open", "high", "low", "close", "volume", "timeframe"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"Polars candle frame is missing columns: {sorted(missing)}")
    ordered = frame.sort([column for column in ("symbol", "timeframe", "ts") if column in frame])
    return [
        Candle(
            ts=row["ts"],
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row["volume"]),
            tf=str(row["timeframe"]),
        )
        for row in ordered.iter_rows(named=True)
    ]


def scan_candle_parquet(
    paths: Sequence[str | Path],
    *,
    symbol: str,
    timeframe: str,
) -> pl.LazyFrame:
    """Lazily scan native Delta cache shards and attach identity columns."""
    pl = _polars()
    sources = [str(Path(path)) for path in paths]
    if not sources:
        raise ValueError("at least one candle parquet path is required")
    lazy = pl.scan_parquet(sources, include_file_paths="source_path")
    columns = set(lazy.collect_schema().names())
    if "ts" not in columns and "timestamp" in columns:
        lazy = lazy.rename({"timestamp": "ts"})
    return lazy.with_columns(
        pl.lit(symbol.upper()).alias("symbol"),
        pl.lit(timeframe).alias("timeframe"),
    )


def clean_candle_lazy(lazy: pl.LazyFrame) -> pl.LazyFrame:
    """Apply deterministic OHLCV hygiene, deduplication, and ordering."""
    pl = _polars()
    required = set(candle_schema())
    columns = set(lazy.collect_schema().names())
    missing = required.difference(columns)
    if missing:
        raise ValueError(f"lazy candle frame is missing columns: {sorted(missing)}")
    source_sort = ["source_path"] if "source_path" in columns else []
    return (
        lazy.with_columns(
            pl.col("ts").cast(pl.Datetime("us", "UTC")),
            pl.col("symbol").cast(pl.String),
            pl.col("timeframe").cast(pl.String),
            *[pl.col(column).cast(pl.Float64) for column in ("open", "high", "low", "close", "volume")],
        )
        .filter(
            (pl.col("volume") >= 0)
            & (pl.min_horizontal("open", "high", "low", "close") > 0)
            & (pl.col("high") >= pl.max_horizontal("open", "close"))
            & (pl.col("low") <= pl.min_horizontal("open", "close"))
            & (pl.col("high") >= pl.col("low"))
        )
        .sort(["symbol", "timeframe", "ts", *source_sort])
        .unique(subset=["symbol", "timeframe", "ts"], keep="last", maintain_order=True)
        .sort(["symbol", "timeframe", "ts"])
    )


def add_candle_features(lazy: pl.LazyFrame, *, atr_window: int = 14) -> pl.LazyFrame:
    """Add group-safe log return, true range, and causal rolling ATR."""
    pl = _polars()
    if atr_window < 1:
        raise ValueError("ATR window must be positive")
    groups = ["symbol", "timeframe"]
    staged = lazy.sort([*groups, "ts"]).with_columns(
        pl.col("close").shift(1).over(groups).alias("previous_close"),
        (pl.col("close").log() - pl.col("close").shift(1).over(groups).log()).alias(
            "log_return"
        ),
    )
    staged = staged.with_columns(
        pl.max_horizontal(
            pl.col("high") - pl.col("low"),
            (pl.col("high") - pl.col("previous_close")).abs(),
            (pl.col("low") - pl.col("previous_close")).abs(),
        ).alias("true_range")
    )
    return staged.with_columns(
        pl.col("true_range")
        .rolling_mean(window_size=atr_window, min_samples=atr_window)
        .over(groups)
        .alias("atr")
    )


def detect_gaps(frame: pl.DataFrame, *, expected_minutes: int) -> pl.DataFrame:
    """Return only chronological discontinuities within each symbol/timeframe."""
    pl = _polars()
    if expected_minutes < 1:
        raise ValueError("expected timeframe minutes must be positive")
    groups = ["symbol", "timeframe"]
    return (
        frame.sort([*groups, "ts"])
        .with_columns(pl.col("ts").shift(1).over(groups).alias("previous_ts"))
        .with_columns(
            (pl.col("ts") - pl.col("previous_ts"))
            .dt.total_minutes()
            .alias("delta_minutes")
        )
        .filter(pl.col("delta_minutes") > expected_minutes)
        .with_columns(
            ((pl.col("delta_minutes") // expected_minutes) - 1)
            .cast(pl.Int64)
            .alias("missing_bars")
        )
    )


def _raw_swing_masks(
    high: np.ndarray,
    low: np.ndarray,
    left: int,
    right: int,
) -> tuple[np.ndarray, np.ndarray]:
    size = len(high)
    high_mask = np.zeros(size, dtype=bool)
    low_mask = np.zeros(size, dtype=bool)
    if size < left + right + 1:
        return high_mask, low_mask
    centers = slice(left, size - right)
    center_high = high[centers]
    center_low = low[centers]
    highs = np.ones(len(center_high), dtype=bool)
    lows = np.ones(len(center_low), dtype=bool)
    for offset in range(1, left + 1):
        highs &= center_high > high[left - offset : size - right - offset]
        lows &= center_low < low[left - offset : size - right - offset]
    for offset in range(1, right + 1):
        highs &= center_high > high[left + offset : size - right + offset]
        lows &= center_low < low[left + offset : size - right + offset]
    high_mask[centers] = highs
    low_mask[centers] = lows
    return high_mask, low_mask


def precompute_confirmed_swings(
    frame: pl.DataFrame,
    *,
    left: int = 3,
    right: int = 3,
    minimum_swing_bps: float = 8.0,
) -> pl.DataFrame:
    """Precompute v2-compatible swings with explicit causal availability.

    Vectorized NumPy masks locate raw pivots. The minimum excursion filter then
    mirrors the frozen mechanical v2 rule: compare against the most recent
    accepted swing of the opposite type. The output timestamp is the pivot time;
    ``confirmed_at`` is the only timestamp at which a live/replay consumer may
    use the level.
    """
    pl = _polars()
    if left < 1 or right < 1 or minimum_swing_bps < 0:
        raise ValueError("invalid swing precomputation parameters")
    required = {"ts", "symbol", "timeframe", "high", "low"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"swing frame is missing columns: {sorted(missing)}")
    records: list[dict[str, object]] = []
    working = frame.sort(["symbol", "timeframe", "ts"])
    for key, group in working.partition_by(["symbol", "timeframe"], as_dict=True).items():
        symbol, timeframe = (str(value) for value in key)
        high = group["high"].to_numpy()
        low = group["low"].to_numpy()
        timestamps = group["ts"].to_list()
        high_mask, low_mask = _raw_swing_masks(high, low, left, right)
        latest: dict[str, float] = {}
        for index in range(left, len(group) - right):
            candidates: list[tuple[str, float]] = []
            if high_mask[index]:
                candidates.append(("high", float(high[index])))
            if low_mask[index]:
                candidates.append(("low", float(low[index])))
            for kind, price in candidates:
                opposite = latest.get("low" if kind == "high" else "high")
                strength = abs(price / opposite - 1.0) * 10_000.0 if opposite else None
                if strength is not None and strength < minimum_swing_bps:
                    continue
                ts = timestamps[index]
                records.append(
                    {
                        "swing_id": f"{timeframe}:{kind}:{ts.isoformat()}:{price:.12g}",
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "kind": kind,
                        "ts": ts,
                        "confirmed_at": timestamps[index + right],
                        "price": price,
                        "pivot_index": index,
                        "strength_bps": strength,
                    }
                )
                latest[kind] = price
    schema = {
        "swing_id": pl.String,
        "symbol": pl.String,
        "timeframe": pl.String,
        "kind": pl.String,
        "ts": pl.Datetime("us", "UTC"),
        "confirmed_at": pl.Datetime("us", "UTC"),
        "price": pl.Float64,
        "pivot_index": pl.Int64,
        "strength_bps": pl.Float64,
    }
    return pl.DataFrame(records, schema=schema).sort(["symbol", "timeframe", "ts", "kind"])

