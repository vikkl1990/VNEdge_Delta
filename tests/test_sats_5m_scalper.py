"""5m SATS-style scalper: causal quality trend lane."""

import pandas as pd
import pytest

from vnedge.data.schemas import normalize_candles
from vnedge.runtime.multi_lane_shadow import desired_lane_specs, sats_5m_delta_lanes
from vnedge.runtime.runner_config import RunnerMode
from vnedge.strategy.sats_5m_scalper import (
    Sats5mScalper,
    Sats5mScalperParams,
    add_sats_5m_columns,
)

BASE = 1_750_000_000_000
FIVE_MIN = 300_000


def params() -> Sats5mScalperParams:
    return Sats5mScalperParams(
        ema_fast=5,
        ema_slow=12,
        bbp_window=5,
        bbp_slope_window=1,
        er_window=8,
        rsi_window=6,
        atr_window=6,
        atr_pct_window=24,
        structure_window=12,
        volume_z_window=12,
        momentum_persistence_window=4,
        max_long_rsi=100.0,
        min_short_rsi=0.0,
    )


def make_candles(rows):
    return normalize_candles(
        [
            [BASE + i * FIVE_MIN, open_, high, low, close, volume]
            for i, (open_, high, low, close, volume) in enumerate(rows)
        ]
    )


def trend_quality_rows(n=90, start=100.0):
    rows = []
    prev = start
    for i in range(n):
        if i < 35:
            close = start + 0.03 * ((i % 4) - 1.5)
        elif i < n - 1:
            close = prev + 0.12 + 0.02 * (i % 3)
        else:
            close = prev + 0.75
        high = max(prev, close) + (0.12 if i < n - 1 else 0.30)
        low = min(prev, close) - 0.10
        volume = 100.0 + (8.0 * i if i >= 35 else 0.0)
        rows.append((prev, high, low, close, volume))
        prev = close
    return rows


def chop_rows(n=90):
    rows = []
    prev = 100.0
    for i in range(n):
        close = 100.0 + (0.10 if i % 2 else -0.10)
        high = max(prev, close) + 0.08
        low = min(prev, close) - 0.08
        rows.append((prev, high, low, close, 100.0))
        prev = close
    return rows


def test_sats_5m_fires_on_quality_trend_continuation():
    candles = make_candles(trend_quality_rows())
    strategy = Sats5mScalper(
        params=params(),
        min_tqi=0.52,
        min_quality_strength=0.04,
        min_momentum_persistence=0.50,
        min_bbp_atr=0.05,
        min_bbp_slope=-0.20,
        min_volume_z=-0.75,
    )
    df = strategy.prepare(candles)

    intent = strategy.signal(df, len(df) - 1)

    assert intent is not None
    assert intent.side == "long"
    assert "sats_5m_scalper long" in intent.reason
    assert "BullP=" in intent.reason
    assert "BearP=" in intent.reason
    assert "trail=" in intent.reason
    assert "tp_ladder=" in intent.reason
    assert "TQI=" in intent.reason
    assert intent.stop_price < float(df["close"].iloc[-1])
    rr = (intent.take_profit_price - float(df["close"].iloc[-1])) / (
        float(df["close"].iloc[-1]) - intent.stop_price
    )
    assert rr == pytest.approx(3.0)
    assert float(df["tqi_long"].iloc[-1]) > float(df["tqi_short"].iloc[-1])


def test_sats_5m_blocks_low_quality_chop():
    candles = make_candles(chop_rows())
    strategy = Sats5mScalper(params=params(), min_tqi=0.52)
    df = strategy.prepare(candles)

    assert strategy.signal(df, len(df) - 1) is None


def test_sats_5m_computes_classic_bull_bear_power_and_stealth_trail():
    candles = make_candles(trend_quality_rows())
    p = params()

    df = add_sats_5m_columns(candles, p)
    last = df.iloc[-1]

    assert last["bull_power"] > 0
    assert last["bear_power"] > -1.0
    assert last["bbp"] == pytest.approx(last["bull_power"] + last["bear_power"])
    assert last["stealth_trend"] in {-1, 1}
    assert not pd.isna(last["stealth_trail"])
    if last["stealth_trend"] > 0:
        assert last["stealth_trail"] < last["close"]
    else:
        assert last["stealth_trail"] > last["close"]


def test_sats_5m_rejects_unknown_side_filter():
    with pytest.raises(ValueError, match="allowed_sides"):
        Sats5mScalper(allowed_sides=["flat"])


def test_sats_5m_features_are_causal_when_future_changes():
    candles = make_candles(trend_quality_rows(120))
    mutated = candles.copy()
    mutated.loc[81:, ["open", "high", "low", "close"]] *= 1.2
    p = params()

    a = add_sats_5m_columns(candles, p)
    b = add_sats_5m_columns(mutated, p)

    cols = [
        "ema_fast",
        "ema_slow",
        "bull_power",
        "bear_power",
        "bbp",
        "stealth_trail",
        "stealth_trend",
        "stealth_distance_atr",
        "rsi",
        "er",
        "prior_high",
        "prior_low",
        "tqi_long",
        "tqi_short",
        "sats_event_long",
    ]
    pd.testing.assert_frame_equal(a.loc[:80, cols], b.loc[:80, cols])


def test_sats_5m_delta_lanes_are_shadow_first_and_5m():
    lanes = sats_5m_delta_lanes({})

    assert {lane.symbol for lane in lanes} == {
        "ETH/USD:USD",
        "BTC/USD:USD",
        "SOL/USD:USD",
        "XRP/USD:USD",
    }
    assert all(lane.exchange == "delta_india" for lane in lanes)
    assert all(lane.timeframe == "5m" for lane in lanes)
    assert all(lane.strategy_id == "sats_5m_scalper_v1" for lane in lanes)
    assert all(lane.mode is RunnerMode.SHADOW for lane in lanes)


def test_sats_5m_lanes_can_be_paper_observed_without_promotion():
    # tests the paper-observation mirroring of sats_5m lanes; sats_5m_scalper_v1
    # was evidence-pruned 2026-08-02 (-$681 ledger), so run the mirror logic with
    # the prune off.
    specs = desired_lane_specs({"MULTI_LANE_PAPER_OBSERVE_ALL": "1", "MULTI_LANE_PRUNE_DEAD": "0"})
    ids = {spec.lane_id for spec in specs}

    assert "sats_5m_scalper_delta_india_eth_usd_usd_shadow" in ids
    assert "sats_5m_scalper_delta_india_eth_usd_usd_paper_observation" in ids
    observed = next(
        spec for spec in specs
        if spec.lane_id == "sats_5m_scalper_delta_india_eth_usd_usd_paper_observation"
    )
    assert observed.mode is RunnerMode.PAPER
    assert observed.timeframe == "5m"
    assert observed.is_primary is False
