"""Cross-venue event lead-lag miner."""

import pandas as pd

from vnedge.data.parquet_store import ParquetStore
from vnedge.research.event_leadlag_alpha import (
    LeadLagFilter,
    LeadLagMinerConfig,
    parse_args,
    run_event_leadlag_alpha,
)
from vnedge.research.universe import ResearchTarget

SYM = "DOGE/USDT:USDT"


def candles(prices, volumes):
    timestamps = pd.date_range("2026-07-01T00:00:00Z", periods=len(prices), freq="1min")
    opens = [prices[0], *prices[:-1]]
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": opens,
            "high": [max(o, c) * 1.0001 for o, c in zip(opens, prices)],
            "low": [min(o, c) * 0.9999 for o, c in zip(opens, prices)],
            "close": prices,
            "volume": volumes,
        }
    )


def lead_lag_frames(*, follower_moves: bool = True):
    n = 320
    leader = [100.0] * n
    follower = [100.0] * n
    leader_volume = [100.0] * n
    follower_volume = [100.0] * n
    events = (130, 160, 190, 220, 250, 280)

    for i in range(1, n):
        leader[i] = leader[i - 1]
        follower[i] = follower[i - 1]
        if i in events:
            leader[i] = leader[i - 1] * 1.010
            leader_volume[i] = 900.0
        if follower_moves and (i - 3) in events:
            follower[i] = follower[i - 1] * 1.006
            follower_volume[i] = 400.0
    return candles(leader, leader_volume), candles(follower, follower_volume)


def config() -> LeadLagMinerConfig:
    return LeadLagMinerConfig(
        rolling_window=30,
        horizons_min=(3,),
        filters=(LeadLagFilter(20.0, 1.0, -1.0, 0.50, 5.0),),
        min_samples=5,
        max_single_win_share=0.50,
    )


def test_event_leadlag_finds_delayed_follower_edge(tmp_path):
    store = ParquetStore(tmp_path)
    leader, follower = lead_lag_frames()
    store.upsert_candles("binanceusdm", SYM, "1m", leader)
    store.upsert_candles("bybit", SYM, "1m", follower)

    payload = run_event_leadlag_alpha(
        tmp_path,
        targets=(
            ResearchTarget("binanceusdm", SYM, "1m"),
            ResearchTarget("bybit", SYM, "1m"),
        ),
        config=config(),
    )

    assert payload["policy"]["can_trade"] is False
    assert payload["summary"]["edge_candidates"] >= 1
    best = payload["hypotheses"][0]
    assert best["leader_exchange"] == "binanceusdm"
    assert best["follower_exchange"] == "bybit"
    assert best["state"] in {"EDGE_CANDIDATE_MAKER", "EDGE_CANDIDATE_TAKER"}
    assert best["maker_avg_net_bps"] > 0
    assert best["samples"] >= 5
    assert best["requires_conservative_replay"] is True


def test_event_leadlag_blocks_when_follower_does_not_reprice(tmp_path):
    store = ParquetStore(tmp_path)
    leader, follower = lead_lag_frames(follower_moves=False)
    store.upsert_candles("binanceusdm", SYM, "1m", leader)
    store.upsert_candles("bybit", SYM, "1m", follower)

    payload = run_event_leadlag_alpha(
        tmp_path,
        targets=(
            ResearchTarget("binanceusdm", SYM, "1m"),
            ResearchTarget("bybit", SYM, "1m"),
        ),
        config=config(),
    )

    assert payload["summary"]["edge_candidates"] == 0
    assert payload["hypotheses"][0]["state"] == "BELOW_COST"
    assert payload["hypotheses"][0]["route_decision"] == "BLOCKED"
    assert payload["hypotheses"][0]["can_trade"] is False


def test_event_leadlag_reports_missing_data_lanes(tmp_path):
    payload = run_event_leadlag_alpha(
        tmp_path,
        targets=(ResearchTarget("binanceusdm", SYM, "1m"),),
        config=config(),
    )

    assert payload["summary"]["loaded_lanes"] == 0
    assert payload["summary"]["missing_lanes"] == 1
    assert "missing" in payload["data_lanes"]
    assert payload["can_trade"] is False


def test_event_leadlag_cli_keeps_one_shot_default_but_supports_loop_mode():
    default = parse_args([])
    loop = parse_args(["--interval-seconds", "3600", "--once"])

    assert default.interval_seconds == 0
    assert default.once is False
    assert loop.interval_seconds == 3600
    assert loop.once is True
