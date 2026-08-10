from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

pl = pytest.importorskip("polars")

from vnedge.research.polars_candle_benchmark import benchmark
from vnedge.research.polars_candles import (
    add_candle_features,
    candles_to_polars,
    clean_candle_lazy,
    detect_gaps,
    polars_to_candles,
    precompute_confirmed_swings,
    scan_candle_parquet,
)
from vnedge.scalping.delta_engine.mechanical_structure import (
    MechanicalStructureConfig,
    confirmed_swings,
)
from vnedge.scalping.delta_engine.types import Candle

START = datetime(2025, 1, 1, tzinfo=UTC)


def _bar(index: int, high: float, low: float, *, tf: str = "1h") -> Candle:
    mid = (high + low) / 2.0
    return Candle(START + timedelta(hours=index + 1), mid, high, low, mid, 1.0, tf)


def test_candle_polars_round_trip_preserves_contract():
    rows = [_bar(0, 101.0, 99.0), _bar(1, 102.0, 100.0)]

    frame = candles_to_polars(rows, symbol="btcusd")
    restored = polars_to_candles(frame)

    assert frame.schema["ts"] == pl.Datetime("us", "UTC")
    assert frame["symbol"].to_list() == ["BTCUSD", "BTCUSD"]
    assert restored == rows


def test_lazy_hygiene_filters_invalid_rows_and_deduplicates_deterministically():
    raw = pl.DataFrame(
        {
            "ts": [START, START, START + timedelta(minutes=1)],
            "symbol": ["BTCUSD"] * 3,
            "timeframe": ["1m"] * 3,
            "open": [100.0, 100.0, 100.0],
            "high": [101.0, 102.0, 99.0],
            "low": [99.0, 99.0, 98.0],
            "close": [100.0, 101.0, 100.0],
            "volume": [1.0, 2.0, 1.0],
            "source_path": ["a.parquet", "b.parquet", "a.parquet"],
        }
    )

    cleaned = clean_candle_lazy(raw.lazy()).collect()

    assert len(cleaned) == 1
    assert cleaned["close"].item() == 101.0
    assert cleaned["source_path"].item() == "b.parquet"


def test_gap_detection_is_group_safe_and_reports_missing_bars():
    rows = [
        Candle(START, 100, 101, 99, 100, 1, "1m"),
        Candle(START + timedelta(minutes=1), 100, 101, 99, 100, 1, "1m"),
        Candle(START + timedelta(minutes=4), 100, 101, 99, 100, 1, "1m"),
    ]
    frame = candles_to_polars(rows, symbol="BTCUSD")

    gaps = detect_gaps(frame, expected_minutes=1)

    assert len(gaps) == 1
    assert gaps["delta_minutes"].item() == 3
    assert gaps["missing_bars"].item() == 2


def test_feature_calculation_does_not_cross_symbol_boundaries():
    first = candles_to_polars(
        [
            Candle(START, 100, 101, 99, 100, 1, "1m"),
            Candle(START + timedelta(minutes=1), 100, 103, 99, 102, 1, "1m"),
        ],
        symbol="BTCUSD",
    )
    second = candles_to_polars(
        [
            Candle(START, 200, 201, 199, 200, 1, "1m"),
            Candle(START + timedelta(minutes=1), 200, 201, 197, 198, 1, "1m"),
        ],
        symbol="ETHUSD",
    )

    featured = add_candle_features(pl.concat([first, second]).lazy(), atr_window=2).collect()
    first_per_symbol = featured.group_by("symbol", maintain_order=True).first()

    assert first_per_symbol["previous_close"].null_count() == 2
    assert first_per_symbol["log_return"].null_count() == 2


def test_polars_swing_precompute_matches_frozen_mechanical_batch_logic():
    highs = (100.0, 102.0, 101.0, 110.0, 103.0, 101.0, 100.0, 99.0, 100.0, 98.0)
    lows = (98.0, 99.0, 98.5, 101.0, 99.0, 97.0, 96.0, 90.0, 95.0, 94.0)
    rows = tuple(_bar(index, high, low) for index, (high, low) in enumerate(zip(highs, lows)))
    config = MechanicalStructureConfig(2, 2, 8.0, 5, 24)
    expected = confirmed_swings(rows, config)

    actual = precompute_confirmed_swings(
        candles_to_polars(rows, symbol="BTCUSD"),
        left=config.swing_left,
        right=config.swing_right,
        minimum_swing_bps=config.minimum_swing_bps,
    )

    assert actual["swing_id"].to_list() == [swing.swing_id for swing in expected]
    assert actual["confirmed_at"].to_list() == [swing.confirmed_at for swing in expected]
    assert all(
        confirmed_at > pivot_ts
        for pivot_ts, confirmed_at in zip(actual["ts"], actual["confirmed_at"])
    )


def test_native_parquet_scan_attaches_identity_and_renames_timestamp(tmp_path):
    path = tmp_path / "BTCUSD_1m_test.parquet"
    pl.DataFrame(
        {
            "timestamp": [START],
            "open": [100.0],
            "high": [101.0],
            "low": [99.0],
            "close": [100.0],
            "volume": [1.0],
        }
    ).write_parquet(path)

    scanned = clean_candle_lazy(
        scan_candle_parquet([path], symbol="BTCUSD", timeframe="1m")
    ).collect()

    assert scanned["symbol"].item() == "BTCUSD"
    assert scanned["timeframe"].item() == "1m"
    assert scanned["ts"].item() == START


def test_benchmark_requires_polars_pandas_parity(tmp_path):
    paths = []
    for shard in range(2):
        path = tmp_path / f"BTCUSD_1m_{shard}.parquet"
        pl.DataFrame(
            {
                "timestamp": [START + timedelta(minutes=index) for index in range(4)],
                "open": [100.0] * 4,
                "high": [101.0] * 4,
                "low": [99.0] * 4,
                "close": [100.0 + shard] * 4,
                "volume": [1.0] * 4,
            }
        ).write_parquet(path)
        paths.append(path)

    result = benchmark(
        paths,
        symbol="BTCUSD",
        timeframe="1m",
        expected_minutes=1,
        atr_window=2,
        repeats=1,
    )

    assert result["parity"]["rows_equal"] is True
    assert result["parity"]["close_sum_equal"] is True
    assert result["parity"]["polars_rows"] == 4
    assert result["trading_path_changed"] is False
