"""Human fingerprint / stealth-trail BBP scanner."""

import pandas as pd
import pytest

from vnedge.data.schemas import normalize_candles
from vnedge.runtime.multi_lane_shadow import (
    desired_lane_specs,
    stealth_trail_bbp_delta_lanes,
)
from vnedge.runtime.runner_config import RunnerMode
from vnedge.strategy.stealth_trail_bbp import (
    HUMAN_TRADE_FINGERPRINT_ID,
    STEALTH_TRAIL_BBP_ID,
    STEALTH_TRAIL_BBP_PROMOTION_GATE,
    HumanTradeFingerprintScanner,
    StealthTrailBBPParams,
    StealthTrailBBPScanner,
    add_stealth_trail_bbp_columns,
)
from vnedge.strategy.strategy_registry import STRATEGIES, get_strategy_class

BASE = 1_750_000_000_000
FIVE_MIN = 300_000


def params() -> StealthTrailBBPParams:
    return StealthTrailBBPParams(
        ema_window=5,
        atr_window=5,
        bbp_z_window=8,
        bbp_slope_window=1,
        volume_z_window=8,
        displacement_pct_window=12,
        structure_window=10,
        stealth_trail_atr_mult=1.6,
        confirm_ema_fast=3,
        confirm_ema_slow=5,
        confirm_er_window=3,
        confirm_adx_window=3,
        min_15m_er=0.0,
        bias_ema_fast=3,
        bias_ema_slow=5,
        bias_er_window=3,
        bias_adx_window=3,
        min_1h_er=0.0,
        min_1h_adx=0.0,
        min_bbp_z=-0.25,
        min_bbp_slope=-0.25,
        min_volume_z=0.0,
        min_body_atr=0.35,
        min_body_percentile=0.50,
        min_expected_net_edge_bps=25.0,
        stop_atr_mult=0.75,
        min_stop_bps=5.0,
        take_profit_r=3.0,
        taker_entry_bps=5.0,
        taker_exit_bps=5.0,
        slippage_bps=1.0,
        safety_buffer_bps=4.0,
    )


def make_candles(rows):
    return normalize_candles(
        [
            [BASE + i * FIVE_MIN, open_, high, low, close, volume]
            for i, (open_, high, low, close, volume) in enumerate(rows)
        ]
    )


def mtf_uptrend_rows(n=120, start=100.0):
    rows = []
    prev = start
    for i in range(n):
        if i < 35:
            close = start + 0.02 * ((i % 5) - 2)
        elif i < n - 1:
            close = prev + 0.08 + 0.01 * (i % 3)
        else:
            close = prev + 0.65
        high = max(prev, close) + (0.10 if i < n - 1 else 0.32)
        low = min(prev, close) - (0.08 if i < n - 1 else 0.10)
        volume = 100.0 + (2.0 * i if i >= 35 else 0.0)
        if i == n - 1:
            volume *= 3.0
        rows.append((prev, high, low, close, volume))
        prev = close
    return rows


def mtf_downtrend_rows(n=120, start=100.0):
    rows = []
    prev = start
    for i in range(n):
        if i < 35:
            close = start + 0.02 * ((i % 5) - 2)
        elif i < n - 1:
            close = prev - 0.08 - 0.01 * (i % 3)
        else:
            close = prev - 0.65
        high = max(prev, close) + (0.08 if i < n - 1 else 0.10)
        low = min(prev, close) - (0.10 if i < n - 1 else 0.32)
        volume = 100.0 + (2.0 * i if i >= 35 else 0.0)
        if i == n - 1:
            volume *= 3.0
        rows.append((prev, high, low, close, volume))
        prev = close
    return rows


def test_stealth_trail_bbp_fires_on_mtf_aligned_long():
    candles = make_candles(mtf_uptrend_rows())
    strategy = StealthTrailBBPScanner(params=params())
    df = strategy.prepare(candles)

    intent = strategy.signal(df, len(df) - 1)

    assert intent is not None
    assert intent.side == "long"
    assert "mtf=5m_trigger/15m_confirm/1h_bias" in intent.reason
    assert "BBPz=" in intent.reason
    assert "expectedNet=" in intent.reason
    assert "takerFallback=allowed" in intent.reason
    assert "BE_after_TP1" in intent.reason
    assert intent.stop_price < float(df["close"].iloc[-1]) < intent.take_profit_price
    assert float(df["expected_net_edge_bps_long"].iloc[-1]) >= 25.0


def test_stealth_trail_bbp_fires_on_mtf_aligned_short():
    candles = make_candles(mtf_downtrend_rows())
    strategy = StealthTrailBBPScanner(params=params())
    df = strategy.prepare(candles)

    intent = strategy.signal(df, len(df) - 1)

    assert intent is not None
    assert intent.side == "short"
    assert intent.stop_price > float(df["close"].iloc[-1]) > intent.take_profit_price
    assert float(df["expected_net_edge_bps_short"].iloc[-1]) >= 25.0


def test_stealth_trail_bbp_blocks_when_taker_edge_cannot_pay_fee_wall():
    candles = make_candles(mtf_uptrend_rows())
    strategy = StealthTrailBBPScanner(
        params=params(),
        min_expected_net_edge_bps=10_000.0,
    )
    df = strategy.prepare(candles)

    assert strategy.signal(df, len(df) - 1) is None


def test_stealth_trail_bbp_features_are_causal_when_future_changes():
    candles = make_candles(mtf_uptrend_rows(140))
    mutated = candles.copy()
    mutated.loc[91:, ["open", "high", "low", "close"]] *= 1.25
    mutated.loc[91:, "volume"] *= 4.0
    p = params()

    a = add_stealth_trail_bbp_columns(candles, p)
    b = add_stealth_trail_bbp_columns(mutated, p)

    cols = [
        "ema13",
        "bull_power",
        "bear_power",
        "bbp_hist_atr",
        "bbp_hist_slope",
        "bbp_hist_z",
        "stealth_trail",
        "stealth_trend",
        "body_atr",
        "body_percentile",
        "volume_z",
        "prior_high",
        "prior_low",
        "confirm_15m_close",
        "confirm_15m_ema_fast",
        "confirm_15m_ema_slow",
        "bias_1h_close",
        "bias_1h_ema_fast",
        "bias_1h_ema_slow",
        "expected_net_edge_bps_long",
    ]
    pd.testing.assert_frame_equal(a.loc[:90, cols], b.loc[:90, cols])


def test_stealth_trail_bbp_exit_columns_fallback_without_valid_structural_stop():
    candles = make_candles([(100.0, 100.0, 100.0, 100.0, 100.0)] * 120)
    df = add_stealth_trail_bbp_columns(candles, params())
    warmed = df.dropna(subset=["atr_5m", "stop_long", "stop_short"])

    assert not warmed.empty
    assert (warmed["stop_long"] < warmed["close"]).all()
    assert (warmed["stop_short"] > warmed["close"]).all()


def test_stealth_trail_bbp_promotion_gate_matches_requested_floor():
    ok, reasons = STEALTH_TRAIL_BBP_PROMOTION_GATE.evaluate(
        avg_net_edge_bps=25.1,
        profit_factor=1.51,
        num_trades=20,
    )
    assert ok
    assert reasons == ()

    ok, reasons = STEALTH_TRAIL_BBP_PROMOTION_GATE.evaluate(
        avg_net_edge_bps=25.0,
        profit_factor=1.50,
        num_trades=19,
    )
    assert not ok
    assert reasons == (
        "expected_net_edge_bps<=25",
        "profit_factor<=1.5",
        "historical_trades<20",
    )


def test_stealth_trail_bbp_registry_aliases():
    assert get_strategy_class(STEALTH_TRAIL_BBP_ID) is StealthTrailBBPScanner
    assert get_strategy_class(HUMAN_TRADE_FINGERPRINT_ID) is HumanTradeFingerprintScanner
    assert {STEALTH_TRAIL_BBP_ID, HUMAN_TRADE_FINGERPRINT_ID} <= set(STRATEGIES)


def test_stealth_trail_bbp_delta_lanes_are_shadow_first_and_5m():
    lanes = stealth_trail_bbp_delta_lanes({})

    assert {lane.symbol for lane in lanes} == {
        "ETH/USD:USD",
        "BTC/USD:USD",
        "SOL/USD:USD",
        "XRP/USD:USD",
    }
    assert all(lane.exchange == "delta_india" for lane in lanes)
    assert all(lane.timeframe == "5m" for lane in lanes)
    assert all(lane.strategy_id == STEALTH_TRAIL_BBP_ID for lane in lanes)
    assert all(lane.mode is RunnerMode.SHADOW for lane in lanes)


def test_stealth_trail_bbp_lanes_can_be_paper_observed_without_promotion():
    # stealth_trail is pruned from the default roster (2026-08-03 hard-cut);
    # prune-off verifies the generator + paper-observation wiring still work.
    specs = desired_lane_specs(
        {"MULTI_LANE_PAPER_OBSERVE_ALL": "1", "MULTI_LANE_PRUNE_DEAD": "0"}
    )
    ids = {spec.lane_id for spec in specs}

    assert "stealth_trail_bbp_delta_india_eth_usd_usd_shadow" in ids
    assert "stealth_trail_bbp_delta_india_eth_usd_usd_paper_observation" in ids
    observed = next(
        spec
        for spec in specs
        if spec.lane_id == "stealth_trail_bbp_delta_india_eth_usd_usd_paper_observation"
    )
    assert observed.mode is RunnerMode.PAPER
    assert observed.timeframe == "5m"
    assert observed.is_primary is False
