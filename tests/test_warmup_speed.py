"""Faster lane warmup: bars-based lookback (A), candle cache + gap-fill (B),
and an immediate 'warming up' placeholder (C). B is a strict speedup — any cache
miss/shortfall/error falls back to a full fetch, so correctness never depends on
the cache."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from vnedge.runtime.multi_lane import (
    LaneSpec,
    MultiLaneProvider,
    _load_candle_cache,
    _closed_warmup_candles,
    _timeframe_ms,
    _warmup_bars,
    _warmup_candles,
)
from vnedge.runtime.runner_config import RunnerMode

_TF = 300_000  # 5m in ms


class _FakeRest:
    """Records fetch ranges and emits candles across each requested window."""

    def __init__(self):
        self.calls: list[tuple[int, int]] = []

    async def fetch_candles(self, symbol, timeframe, since, until):
        self.calls.append((since, until))
        return [[t, 100.0, 101.0, 99.0, 100.5, 10.0] for t in range(since, until, _TF)]


def _spec() -> LaneSpec:
    return LaneSpec(lane_id="lane-a", exchange="binanceusdm",
                    symbol="BTC/USDT:USDT", timeframe="5m", mode=RunnerMode.SHADOW)


def test_bars_based_lookback_is_per_timeframe():
    assert _timeframe_ms("5m") == 300_000
    assert _timeframe_ms("4h") == 14_400_000
    assert _timeframe_ms("nonsense") == 3_600_000  # safe 1h default
    assert _warmup_bars({"MULTI_LANE_WARMUP_BARS": "600"}) == 600
    assert _warmup_bars({}) == 500
    assert _warmup_bars({"MULTI_LANE_WARMUP_BARS": "x"}) == 500  # bad value -> default


def test_first_run_full_fetches_and_writes_cache(tmp_path):
    cache = tmp_path / "lane-a.candles.parquet"
    rest = _FakeRest()
    frame = asyncio.run(_warmup_candles(rest, _spec(), cache, 0, 500 * _TF))
    assert len(frame) >= 499
    assert cache.exists()
    assert len(rest.calls) == 1  # one full fetch


def test_warmup_excludes_current_forming_candle():
    import pandas as pd

    frame = pd.DataFrame({
        "timestamp": pd.to_datetime([0, _TF, 2 * _TF], unit="ms", utc=True),
        "open": [100.0] * 3,
        "high": [101.0] * 3,
        "low": [99.0] * 3,
        "close": [100.5] * 3,
        "volume": [10.0] * 3,
    })

    # At 2.5 bars, the candle opened at 2 bars is still forming.
    closed = _closed_warmup_candles(
        frame, timeframe="5m", until_ms=int(2.5 * _TF)
    )

    assert closed["timestamp"].tolist() == frame["timestamp"].iloc[:2].tolist()


def test_warmup_keeps_candle_closing_exactly_at_cutoff():
    import pandas as pd

    frame = pd.DataFrame({
        "timestamp": pd.to_datetime([0, _TF], unit="ms", utc=True),
        "open": [100.0] * 2,
        "high": [101.0] * 2,
        "low": [99.0] * 2,
        "close": [100.5] * 2,
        "volume": [10.0] * 2,
    })

    closed = _closed_warmup_candles(frame, timeframe="5m", until_ms=2 * _TF)

    assert len(closed) == 2


def test_second_run_fetches_only_the_gap(tmp_path):
    cache = tmp_path / "lane-a.candles.parquet"
    asyncio.run(_warmup_candles(_FakeRest(), _spec(), cache, 0, 500 * _TF))  # seed cache
    # window shifts forward 10 bars -> only the delta should be fetched
    rest = _FakeRest()
    frame = asyncio.run(_warmup_candles(rest, _spec(), cache, 10 * _TF, 510 * _TF))
    gap_since = rest.calls[0][0]
    assert gap_since > 10 * _TF  # started AFTER the cached window, not at `since`
    assert len(frame) >= 499


def test_corrupt_cache_falls_back_to_full_fetch(tmp_path):
    cache = tmp_path / "lane-a.candles.parquet"
    cache.write_text("not a parquet file")
    assert _load_candle_cache(cache) is None
    rest = _FakeRest()
    frame = asyncio.run(_warmup_candles(rest, _spec(), cache, 0, 500 * _TF))
    assert len(frame) >= 499  # recovered via full fetch
    assert len(rest.calls) == 1


def test_warming_placeholder_shows_the_fleet_immediately():
    provider = MultiLaneProvider("lane-a")
    provider.publish_warming("lane-a", "binanceusdm", "BTC/USDT:USDT")
    snap = provider.latest()
    assert snap is not None
    lane = snap["lanes"][0]
    assert lane["risk_status"] == "warming"
    assert snap["mode"] == "warming up"


# --- warmup-burst resilience: bounded concurrency + transient retry --------------

import pytest
from ccxt.base.errors import ExchangeError, NetworkError

from vnedge.runtime.multi_lane import _lane_build_concurrency, _retry_transient


def test_lane_build_concurrency_reads_env_with_safe_default():
    assert _lane_build_concurrency({}) == 6
    assert _lane_build_concurrency({"MULTI_LANE_BUILD_CONCURRENCY": "10"}) == 10
    assert _lane_build_concurrency({"MULTI_LANE_BUILD_CONCURRENCY": "x"}) == 6
    assert _lane_build_concurrency({"MULTI_LANE_BUILD_CONCURRENCY": "0"}) == 1  # floor


def test_retry_transient_returns_on_first_success():
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        return "ok"

    out = asyncio.run(_retry_transient(factory, retries=3, backoff_s=0, label="lane"))
    assert out == "ok" and calls["n"] == 1


def test_retry_transient_recovers_after_transient_network_errors():
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        if calls["n"] < 3:
            raise NetworkError("rate limited")
        return "recovered"

    out = asyncio.run(_retry_transient(factory, retries=3, backoff_s=0, label="lane"))
    assert out == "recovered" and calls["n"] == 3


def test_retry_transient_raises_after_exhausting_retries():
    async def factory():
        raise NetworkError("still down")

    with pytest.raises(NetworkError):
        asyncio.run(_retry_transient(factory, retries=3, backoff_s=0, label="lane"))


def test_retry_transient_does_not_retry_non_network_errors():
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        raise ExchangeError("bad symbol")  # a real error, not transient

    with pytest.raises(ExchangeError):
        asyncio.run(_retry_transient(factory, retries=3, backoff_s=0, label="lane"))
    assert calls["n"] == 1  # raised immediately, no retry
