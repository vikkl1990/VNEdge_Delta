from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from vnedge.data.parquet_store import ParquetStore
from vnedge.data.schemas import normalize_candles
from vnedge.research.factor_ranker import (
    FactorRankerConfig,
    build_factor_ranker_payload,
    write_factor_ranker_payload,
)
from vnedge.research.universe import ResearchTarget


NOW = datetime(2026, 7, 16, 12, tzinfo=UTC)


def _candles(
    *,
    rows: int = 160,
    start: float = 100.0,
    step: float = 0.1,
    volume: float = 1000.0,
    range_bps: float = 80.0,
    end_age_minutes: int = 60,
) -> list[list]:
    start_ts = NOW - timedelta(minutes=end_age_minutes, hours=rows - 1)
    out: list[list] = []
    close = start
    half_range = range_bps / 20_000.0
    for i in range(rows):
        close += step
        timestamp = int((start_ts + timedelta(hours=i)).timestamp() * 1000)
        open_ = close - step
        high = max(open_, close) * (1.0 + half_range)
        low = min(open_, close) * (1.0 - half_range)
        out.append([timestamp, open_, high, low, close, volume])
    return out


def test_factor_ranker_scores_ready_lane_above_blocked_lanes(tmp_path):
    store = ParquetStore(tmp_path)
    store.upsert_candles(
        "binanceusdm",
        "BTC/USDT:USDT",
        "1h",
        normalize_candles(_candles(start=100.0, step=0.25, volume=5000.0)),
    )
    store.upsert_candles(
        "bybit",
        "DOGE/USDT:USDT",
        "1h",
        normalize_candles(
            _candles(start=0.12, step=0.0, volume=20.0, range_bps=2.0)
        ),
    )

    payload = build_factor_ranker_payload(
        store,
        (
            ResearchTarget("binanceusdm", "BTC/USDT:USDT", "1h"),
            ResearchTarget("bybit", "DOGE/USDT:USDT", "1h"),
            ResearchTarget("delta_india", "ETH/USD:USD", "1h"),
        ),
        config=FactorRankerConfig(max_rows=10, max_data_age_minutes=180),
        now=NOW,
    )

    assert payload["policy"]["can_trade"] is False
    assert payload["policy"]["can_promote"] is False
    assert payload["summary"]["targets"] == 3
    assert payload["summary"]["ready"] == 1
    assert payload["summary"]["missing"] == 1
    assert payload["rows"][0]["lane_key"] == "binanceusdm|BTC/USDT:USDT|1h"
    assert payload["rows"][0]["state"] == "READY"
    assert payload["rows"][0]["recommended_action"] == "scan_now"
    assert payload["blockers_by_state"]["MISSING"] == ["delta_india|ETH/USD:USD|1h"]
    assert "LOW_RANGE" in payload["blockers_by_state"]


def test_factor_ranker_marks_stale_and_under_sampled(tmp_path):
    store = ParquetStore(tmp_path)
    store.upsert_candles(
        "binanceusdm",
        "SOL/USDT:USDT",
        "1h",
        normalize_candles(_candles(rows=30, start=10.0, step=0.05)),
    )
    store.upsert_candles(
        "bybit",
        "ETH/USDT:USDT",
        "1h",
        normalize_candles(_candles(end_age_minutes=600)),
    )

    payload = build_factor_ranker_payload(
        store,
        (
            ResearchTarget("binanceusdm", "SOL/USDT:USDT", "1h"),
            ResearchTarget("bybit", "ETH/USDT:USDT", "1h"),
        ),
        config=FactorRankerConfig(min_rows=80, max_data_age_minutes=180),
        now=NOW,
    )

    by_key = {row["lane_key"]: row for row in payload["rows"]}
    assert by_key["binanceusdm|SOL/USDT:USDT|1h"]["state"] == "UNDER_SAMPLED"
    assert by_key["bybit|ETH/USDT:USDT|1h"]["state"] == "STALE"
    assert payload["summary"]["under_sampled"] == 1
    assert payload["summary"]["stale"] == 1


def test_factor_ranker_writes_atomic_artifact(tmp_path):
    payload = {
        "policy": {"research_only": True, "can_trade": False},
        "rows": [{"lane_key": "binanceusdm|BTC/USDT:USDT|1h"}],
    }

    path = write_factor_ranker_payload(payload, tmp_path)

    assert path.name == "factor_ranker.json"
    assert json.loads(path.read_text()) == payload
    assert not path.with_suffix(".json.tmp").exists()
