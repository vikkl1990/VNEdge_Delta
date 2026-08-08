from pathlib import Path

import pandas as pd
import pytest

from vnedge.research.delta_scalper_cache_repair import (
    contiguous_ranges,
    missing_minutes,
    parse_shard_path,
    repair_cache_file,
)


def _frame(index: pd.DatetimeIndex) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": index,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 10.0,
        }
    )


def test_parse_and_group_cache_gaps():
    path = Path("BTCUSD_1m_20250101T0000_20250101T0010.parquet")
    symbol, start, end = parse_shard_path(path)
    observed = _frame(pd.date_range(start, end, freq="1min", inclusive="left").delete([2, 3, 7]))
    missing = missing_minutes(observed, start, end)

    assert symbol == "BTCUSD"
    assert [len(pd.date_range(left, right, freq="1min", inclusive="left")) for left, right in contiguous_ranges(missing)] == [2, 1]


@pytest.mark.asyncio
async def test_repair_only_fills_missing_rows_and_reaudits(tmp_path):
    path = tmp_path / "ETHUSD_1m_20250101T0000_20250101T0010.parquet"
    _, start, end = parse_shard_path(path)
    complete_index = pd.date_range(start, end, freq="1min", inclusive="left")
    original = _frame(complete_index.delete([4, 5]))
    original.to_parquet(path, index=False)
    calls = []

    async def fake_fetch(symbol, *, resolution, start_s, end_s):
        calls.append((symbol, resolution, start_s, end_s))
        index = pd.date_range(
            pd.Timestamp(start_s, unit="s", tz="UTC"),
            pd.Timestamp(end_s, unit="s", tz="UTC"),
            freq="1min",
            inclusive="left",
        )
        return _frame(index)

    result = await repair_cache_file(path, fetch=fake_fetch)
    repaired = pd.read_parquet(path)

    assert result.missing_before == 2
    assert result.missing_after == 0
    assert result.rows_after == 10
    assert len(calls) == 1
    assert repaired.loc[0, "close"] == 100.5
