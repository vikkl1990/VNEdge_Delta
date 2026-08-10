"""Fast L2 scout — recent-slice mining without promotion side effects."""

from datetime import UTC
from pathlib import Path

import pandas as pd

from vnedge.research.fast_l2_scout import load_recent_tick_events, run_fast_l2_scout
from vnedge.research.universe import ResearchTarget


DAY = "20260706"
SYM = "BTC/USDT:USDT"


def _base(tmp_path: Path) -> Path:
    return tmp_path / "ticks" / "exchange=binanceusdm" / "symbol=BTCUSDT"


def _write_shard(tmp_path: Path, stream: str, name: str, rows: list[dict]) -> None:
    d = _base(tmp_path) / f"stream={stream}" / DAY
    d.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(d / name, index=False)


def _book(ts_ms: int, mid: float) -> dict:
    return {
        "ts_ms": ts_ms,
        "bid": mid - 0.01,
        "bid_qty": 10.0,
        "ask": mid + 0.01,
        "ask_qty": 1.0,
    }


def _thin_book(ts_ms: int, mid: float) -> dict:
    return {
        "ts_ms": ts_ms,
        "bid": mid - 0.10,
        "bid_qty": 0.01,
        "ask": mid + 0.10,
        "ask_qty": 0.01,
    }


def _trade(ts_ms: int, price: float, side: str = "buy") -> dict:
    return {"ts_ms": ts_ms, "price": price, "amount": 1.0, "side": side}


def _rising_recent_lake(tmp_path: Path) -> None:
    old = 1_783_000_000_000
    recent = 1_783_003_600_000
    _write_shard(tmp_path, "book", f"{old}-000000.parquet", [_book(old, 100.0)])
    _write_shard(tmp_path, "trades", f"{old}-000000.parquet", [_trade(old - 2, 100.0)])

    books: list[dict] = []
    trades: list[dict] = []
    for i in range(40):
        ts = recent + i * 100
        mid = 100.0 + i * 0.04
        trades.append(_trade(ts - 2, mid + 0.01, "buy"))
        books.append(_book(ts, mid))
    _write_shard(tmp_path, "book", f"{recent}-000001.parquet", books)
    _write_shard(tmp_path, "trades", f"{recent}-000001.parquet", trades)


def _thin_recent_lake(tmp_path: Path) -> None:
    recent = 1_783_003_600_000
    books: list[dict] = []
    trades: list[dict] = []
    for i in range(40):
        ts = recent + i * 100
        mid = 100.0 + i * 0.04
        trades.append(_trade(ts - 2, mid + 0.01, "buy"))
        books.append(_thin_book(ts, mid))
    _write_shard(tmp_path, "book", f"{recent}-000001.parquet", books)
    _write_shard(tmp_path, "trades", f"{recent}-000001.parquet", trades)


def test_load_recent_tick_events_uses_latest_window(tmp_path):
    _rising_recent_lake(tmp_path)

    events, stats = load_recent_tick_events(
        tmp_path,
        "binanceusdm",
        SYM,
        DAY,
        lookback_minutes=1,
        max_shards=2,
    )

    assert stats["missing_stream"] is False
    assert stats["book_rows"] == 40
    assert stats["trade_rows"] == 40
    assert events[0][0] > 1_783_003_000_000
    assert all(e[2].event_time.tzinfo == UTC for e in events)


def test_fast_l2_scout_is_research_only(tmp_path):
    _rising_recent_lake(tmp_path)
    payload = run_fast_l2_scout(
        tmp_path,
        targets=(ResearchTarget("binanceusdm", SYM),),
        days=(DAY,),
        lookback_minutes=1,
        max_shards=2,
        max_results=10,
    )

    assert payload["scout_id"] == "fast_l2_scout_v1"
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False
    assert payload["policy"]["can_trade"] is False
    assert payload["policy"]["lane_filters"]["can_trade"] is False
    assert payload["summary"]["lanes"] == 1
    assert payload["summary"]["filtered_lanes"] == 0
    assert payload["lanes"][0]["filter_decision"]["passed"] is True
    assert payload["summary"]["results"] > 0
    assert payload["summary"]["best"]["avg_forward_bps"] is not None
    assert payload["top_results"][0]["can_trade"] is False
    assert payload["top_results"][0]["requires_untouched_judgment"] is True


def test_fast_l2_scout_filters_bad_lanes_before_mining(tmp_path):
    _thin_recent_lake(tmp_path)
    payload = run_fast_l2_scout(
        tmp_path,
        targets=(ResearchTarget("binanceusdm", SYM),),
        days=(DAY,),
        lookback_minutes=1,
        max_shards=2,
        max_results=10,
    )

    lane = payload["lanes"][0]
    assert payload["summary"]["filtered_lanes"] == 1
    assert payload["summary"]["results"] == 0
    assert payload["top_results"] == []
    assert lane["state"] == "FILTERED_LANE"
    assert lane["filter_decision"]["passed"] is False
    assert lane["filter_decision"]["primary_blocker"] == "spread"
    assert lane["can_trade"] is False
