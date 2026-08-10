"""Benchmark the research-only Polars candle pipeline against Pandas."""

from __future__ import annotations

import argparse
import json
import math
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import numpy as np
import pandas as pd

from vnedge.research.polars_candles import (
    add_candle_features,
    clean_candle_lazy,
    detect_gaps,
    scan_candle_parquet,
)

DEFAULT_CACHE = Path("data/delta_scalper_cache")
DEFAULT_OUTPUT = Path("research/live_research/polars_candle_benchmark_latest.json")


def _timed(call: Callable[[], Any], repeats: int) -> tuple[Any, list[float]]:
    durations: list[float] = []
    result: Any = None
    for _ in range(repeats):
        started = time.perf_counter()
        result = call()
        durations.append(time.perf_counter() - started)
    return result, durations


def _pandas_pipeline(paths: list[Path], symbol: str, timeframe: str, atr_window: int):
    frames = []
    for path in paths:
        frame = pd.read_parquet(path).rename(columns={"timestamp": "ts"})
        frame["source_path"] = str(path)
        frames.append(frame)
    frame = pd.concat(frames, ignore_index=True)
    frame["symbol"] = symbol.upper()
    frame["timeframe"] = timeframe
    valid = (
        frame["volume"].ge(0)
        & frame[["open", "high", "low", "close"]].min(axis=1).gt(0)
        & frame["high"].ge(frame[["open", "close"]].max(axis=1))
        & frame["low"].le(frame[["open", "close"]].min(axis=1))
        & frame["high"].ge(frame["low"])
    )
    frame = (
        frame.loc[valid]
        .sort_values(["symbol", "timeframe", "ts", "source_path"])
        .drop_duplicates(["symbol", "timeframe", "ts"], keep="last")
        .sort_values(["symbol", "timeframe", "ts"])
        .reset_index(drop=True)
    )
    groups = frame.groupby(["symbol", "timeframe"], sort=False)
    frame["previous_close"] = groups["close"].shift(1)
    frame["log_return"] = np.log(frame["close"]) - np.log(frame["previous_close"])
    frame["true_range"] = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - frame["previous_close"]).abs(),
            (frame["low"] - frame["previous_close"]).abs(),
        ],
        axis=1,
    ).max(axis=1)
    frame["atr"] = groups["true_range"].transform(
        lambda values: values.rolling(atr_window, min_periods=atr_window).mean()
    )
    return frame


def benchmark(
    paths: list[Path],
    *,
    symbol: str,
    timeframe: str,
    expected_minutes: int,
    atr_window: int,
    repeats: int,
) -> dict[str, Any]:
    if not paths:
        raise ValueError("no candle shards matched the benchmark")
    polars_plan = add_candle_features(
        clean_candle_lazy(scan_candle_parquet(paths, symbol=symbol, timeframe=timeframe)),
        atr_window=atr_window,
    )
    polars_plan.collect()  # warm metadata and filesystem caches consistently
    _pandas_pipeline(paths, symbol, timeframe, atr_window)
    polars_frame, polars_times = _timed(polars_plan.collect, repeats)
    pandas_frame, pandas_times = _timed(
        lambda: _pandas_pipeline(paths, symbol, timeframe, atr_window), repeats
    )
    gaps = detect_gaps(polars_frame, expected_minutes=expected_minutes)
    polars_seconds = min(polars_times)
    pandas_seconds = min(pandas_times)
    close_parity = math.isclose(
        float(polars_frame["close"].sum()),
        float(pandas_frame["close"].sum()),
        rel_tol=1e-12,
    )
    return {
        "report_id": "polars_candle_benchmark_v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "research_only": True,
        "trading_path_changed": False,
        "inputs": {
            "symbol": symbol.upper(),
            "timeframe": timeframe,
            "shards": len(paths),
            "atr_window": atr_window,
            "repeats": repeats,
        },
        "parity": {
            "rows_equal": len(polars_frame) == len(pandas_frame),
            "close_sum_equal": close_parity,
            "polars_rows": len(polars_frame),
            "pandas_rows": len(pandas_frame),
        },
        "data_quality": {
            "gap_rows": len(gaps),
            "missing_bars": int(gaps["missing_bars"].sum()) if len(gaps) else 0,
        },
        "performance": {
            "polars_seconds_best": polars_seconds,
            "pandas_seconds_best": pandas_seconds,
            "speedup_x": pandas_seconds / polars_seconds if polars_seconds else None,
            "polars_estimated_bytes": polars_frame.estimated_size(),
            "pandas_deep_bytes": int(pandas_frame.memory_usage(index=True, deep=True).sum()),
            "polars_runs_seconds": polars_times,
            "pandas_runs_seconds": pandas_times,
        },
    }


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--symbol", default="BTCUSD")
    parser.add_argument("--timeframe", default="1m")
    parser.add_argument("--expected-minutes", type=int, default=1)
    parser.add_argument("--atr-window", type=int, default=14)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    paths = sorted(args.cache_dir.glob(f"{args.symbol.upper()}_{args.timeframe}_*.parquet"))
    payload = benchmark(
        paths,
        symbol=args.symbol,
        timeframe=args.timeframe,
        expected_minutes=args.expected_minutes,
        atr_window=args.atr_window,
        repeats=args.repeats,
    )
    _atomic_json(args.output, payload)
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
