"""Parquet store — roundtrip, idempotent upsert, correction semantics."""

import pandas as pd
import pytest

from vnedge.data.parquet_store import ParquetStore, sanitize_symbol
from vnedge.data.schemas import normalize_candles

BASE = 1_750_000_000_000
HOUR = 3_600_000


def candles(start_idx: int, n: int, close: float = 100.0) -> pd.DataFrame:
    raw = [
        [BASE + (start_idx + i) * HOUR, 100.0, 101.0, 99.0, close, 10.0]
        for i in range(n)
    ]
    return normalize_candles(raw)


def test_sanitize_symbol():
    assert sanitize_symbol("BTC/USDT:USDT") == "BTCUSDT"
    assert sanitize_symbol("eth/usdt") == "ETHUSDT"


def test_roundtrip(tmp_path):
    store = ParquetStore(tmp_path)
    df = candles(0, 24)
    result = store.upsert_candles("binanceusdm", "BTC/USDT:USDT", "1h", df)
    assert result.rows_added == 24
    assert "exchange=binanceusdm" in str(result.path)
    assert "symbol=BTCUSDT" in str(result.path)

    loaded = store.read_candles("binanceusdm", "BTC/USDT:USDT", "1h")
    pd.testing.assert_frame_equal(loaded, df)


def test_upsert_is_idempotent(tmp_path):
    store = ParquetStore(tmp_path)
    store.upsert_candles("binanceusdm", "BTC/USDT:USDT", "1h", candles(0, 24))
    again = store.upsert_candles("binanceusdm", "BTC/USDT:USDT", "1h", candles(0, 24))
    assert again.rows_added == 0
    assert again.rows_total == 24


def test_upsert_merges_overlapping_ranges(tmp_path):
    store = ParquetStore(tmp_path)
    store.upsert_candles("binanceusdm", "BTC/USDT:USDT", "1h", candles(0, 24))
    result = store.upsert_candles("binanceusdm", "BTC/USDT:USDT", "1h", candles(12, 24))
    assert result.rows_total == 36
    loaded = store.read_candles("binanceusdm", "BTC/USDT:USDT", "1h")
    assert loaded["timestamp"].is_monotonic_increasing
    assert not loaded["timestamp"].duplicated().any()


def test_redownload_corrects_stored_rows(tmp_path):
    """keep='last': a fresh download of the same range overwrites old values."""
    store = ParquetStore(tmp_path)
    store.upsert_candles("binanceusdm", "BTC/USDT:USDT", "1h", candles(0, 5, close=100.0))
    store.upsert_candles("binanceusdm", "BTC/USDT:USDT", "1h", candles(0, 5, close=200.0))
    loaded = store.read_candles("binanceusdm", "BTC/USDT:USDT", "1h")
    assert (loaded["close"] == 200.0).all()
    assert len(loaded) == 5


def test_read_missing_dataset_raises(tmp_path):
    store = ParquetStore(tmp_path)
    with pytest.raises(FileNotFoundError):
        store.read_candles("binanceusdm", "XRP/USDT:USDT", "1h")
