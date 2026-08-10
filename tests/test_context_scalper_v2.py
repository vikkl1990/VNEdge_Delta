"""Context scalper v2 tests."""

import pandas as pd

from vnedge.data.schemas import normalize_candles
from vnedge.runtime.multi_lane import _build_single_strategy
from vnedge.runtime.multi_lane_shadow import context_scalper_v2_delta_lanes
from vnedge.runtime.runner_config import RunnerMode
from vnedge.strategy.context_scalper_v2 import (
    CONTEXT_SCALPER_V2_ID,
    ContextScalperV2,
)
from vnedge.strategy.stealth_trail_bbp import StealthTrailBBPParams
from vnedge.strategy.strategy_registry import STRATEGIES, get_strategy_class
from vnedge.strategy.vnedge_algo_ml_pro import VNEDGEAlgoMLProParams


BASE = 1_750_000_000_000
FIVE_MIN = 300_000


def stealth_params() -> StealthTrailBBPParams:
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
    )


def algo_params() -> VNEDGEAlgoMLProParams:
    return VNEDGEAlgoMLProParams(
        auto_tune=False,
        atr_length=4,
        base_multiplier=1.0,
        profile_lookback=30,
        min_multiplier=0.7,
        max_multiplier=2.0,
        cushion_atr=0.0,
        cooldown_bars=0,
        use_mtf=False,
        use_ml_filter=False,
        use_momentum=False,
        use_volume_filter=False,
        bbp_length=4,
        bbp_norm_lookback=12,
        min_expected_net_edge_bps=-100.0,
        min_fill_probability=0.0,
        min_stop_bps=4.0,
    )


def make_candles(rows):
    return normalize_candles(
        [
            [BASE + i * FIVE_MIN, open_, high, low, close, volume]
            for i, (open_, high, low, close, volume) in enumerate(rows)
        ]
    )


def mtf_uptrend_rows(n=140, start=100.0):
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


def test_context_scalper_uses_stealth_with_context_and_route_metadata():
    candles = make_candles(mtf_uptrend_rows())
    strategy = ContextScalperV2(
        engine="stealth",
        stealth_params=stealth_params(),
        algo_params=algo_params(),
    )
    df = strategy.prepare(candles)

    intent = strategy.signal(df, len(df) - 1)

    assert intent is not None
    assert intent.side == "long"
    assert intent.stop_price < float(df["close"].iloc[-1]) < intent.take_profit_price
    assert "context_scalper_v2 long" in intent.reason
    assert "source=stealth" in intent.reason
    assert "mtf=5m_trigger/15m_confirm/1h_bias" in intent.reason
    assert "route=maker_first" in intent.reason
    assert "makerFillProbability=0.60" in intent.reason
    assert "TP1/TP2/TP3 optional" in intent.reason


def test_context_scalper_columns_are_causal_when_future_changes():
    candles = make_candles(mtf_uptrend_rows(170))
    mutated = candles.copy()
    mutated.loc[111:, ["open", "high", "low", "close"]] *= 1.35
    mutated.loc[111:, "volume"] *= 4.0
    strategy = ContextScalperV2(
        engine="auto",
        stealth_params=stealth_params(),
        algo_params=algo_params(),
        min_expected_net_edge_bps=-100.0,
        min_fill_probability=0.0,
    )

    a = strategy.prepare(candles)
    b = strategy.prepare(mutated)

    cols = [
        "bias_1h_close",
        "confirm_15m_close",
        "expected_net_edge_bps_long",
        "algo_st_band",
        "algo_trend_dir",
        "algo_expected_net_edge_bps_long",
    ]
    pd.testing.assert_frame_equal(a.loc[:110, cols], b.loc[:110, cols])


def test_context_scalper_registered_and_runtime_constructible():
    assert get_strategy_class(CONTEXT_SCALPER_V2_ID) is ContextScalperV2
    assert CONTEXT_SCALPER_V2_ID in STRATEGIES
    built = _build_single_strategy(
        CONTEXT_SCALPER_V2_ID,
        {"engine": "stealth", "stealth_params": stealth_params()},
        pd.DataFrame(),
        None,
    )
    assert isinstance(built, ContextScalperV2)


def test_context_scalper_delta_lanes_curate_eth_algo_and_xrp_stealth():
    specs = context_scalper_v2_delta_lanes(
        {
            "MULTI_LANE_EXCHANGES": "delta_india",
            "MULTI_LANE_CONTEXT_SCALPER_V2_DELTA": "1",
            "MULTI_LANE_CONTEXT_SCALPER_V2_SYMBOLS": "ETH/USDT:USDT,XRP/USDT:USDT",
        }
    )

    assert [spec.symbol for spec in specs] == ["ETH/USD:USD", "XRP/USD:USD"]
    assert all(spec.mode is RunnerMode.SHADOW for spec in specs)
    assert all(spec.timeframe == "5m" for spec in specs)
    assert all(spec.strategy_id == CONTEXT_SCALPER_V2_ID for spec in specs)
    assert specs[0].strategy_params["engine"] == "algo_ml"
    assert specs[1].strategy_params["engine"] == "stealth"
