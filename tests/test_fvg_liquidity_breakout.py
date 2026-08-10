"""FVG/liquidity breakout scanner tests."""

import pandas as pd

from vnedge.data.schemas import normalize_candles
from vnedge.research.execution_edge_labeler import strategy_signal_events
from vnedge.runtime.multi_lane_shadow import (
    desired_lane_specs,
    fvg_liquidity_breakout_delta_lanes,
)
from vnedge.runtime.runner_config import RunnerMode
from vnedge.strategy.fvg_liquidity_breakout import (
    FVG_LIQUIDITY_BREAKOUT_ID,
    FvgLiquidityBreakoutParams,
    FvgLiquidityBreakoutScanner,
    add_fvg_liquidity_breakout_columns,
)
from vnedge.strategy.strategy_registry import STRATEGIES, get_strategy_class


BASE = 1_750_000_000_000
FIVE_MIN = 300_000


def relaxed_params() -> FvgLiquidityBreakoutParams:
    return FvgLiquidityBreakoutParams(
        atr_window=4,
        ema_window=4,
        fvg_displacement_atr=0.15,
        fvg_ttl_bars=12,
        volume_z_window=5,
        body_percentile_window=5,
        structure_window=8,
        min_body_atr=0.10,
        min_body_percentile=0.0,
        min_volume_z=-5.0,
        min_room_to_liquidity_bps=-999.0,
        confirm_ema_fast=2,
        confirm_ema_slow=4,
        confirm_er_window=2,
        min_15m_er=0.0,
        bias_ema_fast=2,
        bias_ema_slow=4,
        bias_er_window=2,
        min_1h_er=0.0,
        stop_atr_mult=0.7,
        min_stop_bps=5.0,
        take_profit_r=2.5,
        min_expected_net_edge_bps=0.0,
        slippage_bps=1.0,
        safety_buffer_bps=1.0,
        allowed_sides=(),
    )


def make_candles(rows):
    return normalize_candles(
        [
            [BASE + i * FIVE_MIN, open_, high, low, close, volume]
            for i, (open_, high, low, close, volume) in enumerate(rows)
        ]
    )


def fvg_rows(direction: str, n: int = 96, start: float = 100.0):
    rows = []
    prev = start
    drift = 0.08 if direction == "long" else -0.08
    for i in range(n - 7):
        close = prev + drift
        rows.append((prev, max(prev, close) + 0.12, min(prev, close) - 0.12, close, 100.0))
        prev = close

    if direction == "long":
        rows.extend(
            [
                (prev, prev + 0.25, prev - 0.25, prev + 0.05, 105.0),
                (prev + 0.05, prev + 3.20, prev - 0.05, prev + 2.70, 650.0),
                (prev + 2.75, prev + 3.35, prev + 0.75, prev + 2.95, 520.0),
                (prev + 1.05, prev + 2.85, prev + 0.55, prev + 2.65, 820.0),
                (prev + 2.65, prev + 3.05, prev + 2.20, prev + 2.90, 260.0),
                (prev + 2.90, prev + 3.10, prev + 2.50, prev + 2.80, 230.0),
                (prev + 2.80, prev + 3.00, prev + 2.55, prev + 2.95, 220.0),
            ]
        )
    else:
        rows.extend(
            [
                (prev, prev + 0.25, prev - 0.25, prev - 0.05, 105.0),
                (prev - 0.05, prev + 0.05, prev - 3.20, prev - 2.70, 650.0),
                (prev - 2.75, prev - 0.75, prev - 3.35, prev - 2.95, 520.0),
                (prev - 1.05, prev - 0.55, prev - 2.85, prev - 2.65, 820.0),
                (prev - 2.65, prev - 2.20, prev - 3.05, prev - 2.90, 260.0),
                (prev - 2.90, prev - 2.50, prev - 3.10, prev - 2.80, 230.0),
                (prev - 2.80, prev - 2.55, prev - 3.00, prev - 2.95, 220.0),
            ]
        )
    return rows


def test_fvg_liquidity_breakout_fires_on_bullish_retest_plan():
    candles = make_candles(fvg_rows("long"))
    strategy = FvgLiquidityBreakoutScanner(params=relaxed_params())

    events = strategy_signal_events(candles, strategy)

    assert events
    event = next(
        event for event in events
        if event.side == "long" and "fvg_retest" in event.metadata["reason"]
    )
    assert event.stop_price < float(candles["close"].iloc[-5])
    assert event.take_profit_price is not None
    assert event.expected_edge_bps is not None
    assert event.fill_probability is not None
    assert "fvg_retest" in event.metadata["reason"]
    assert "BE_after_TP1" in event.metadata["reason"]


def test_fvg_liquidity_breakout_fires_on_bearish_retest_plan():
    candles = make_candles(fvg_rows("short"))
    strategy = FvgLiquidityBreakoutScanner(params=relaxed_params())

    events = strategy_signal_events(candles, strategy)

    assert events
    event = next(event for event in events if event.side == "short")
    assert event.stop_price > float(candles["close"].iloc[-5])
    assert event.take_profit_price is not None


def test_fvg_liquidity_breakout_columns_are_causal_when_future_changes():
    candles = make_candles(fvg_rows("long", n=120))
    mutated = candles.copy()
    mutated.loc[86:, ["open", "high", "low", "close"]] *= 1.4
    mutated.loc[86:, "volume"] *= 4.0
    params = relaxed_params()

    original = add_fvg_liquidity_breakout_columns(candles, params)
    changed = add_fvg_liquidity_breakout_columns(mutated, params)

    cols = [
        "fvg_atr",
        "fvg_ema",
        "fvg_body_atr",
        "fvg_body_percentile",
        "fvg_volume_z",
        "active_bull_fvg_low",
        "active_bull_fvg_high",
        "confirm_15m_close",
        "bias_1h_close",
        "expected_net_edge_bps_long",
        "expected_net_edge_bps_short",
    ]
    pd.testing.assert_frame_equal(original.loc[:85, cols], changed.loc[:85, cols])


def test_fvg_liquidity_breakout_rebuilds_smart_capture_exit_plan():
    candles = make_candles(fvg_rows("long"))
    strategy = FvgLiquidityBreakoutScanner(params=relaxed_params())
    df = strategy.prepare(candles)

    plan = strategy.synthesize_exit_plan(df, len(df) - 3, "long", float(df["close"].iloc[-3]))

    assert plan is not None
    assert plan.stop_price < float(df["close"].iloc[-3])
    assert plan.take_profit_price is not None
    assert "smart_capture=TP1_or_trail" in plan.reason


def test_fvg_liquidity_breakout_is_registered():
    assert get_strategy_class(FVG_LIQUIDITY_BREAKOUT_ID) is FvgLiquidityBreakoutScanner
    assert FVG_LIQUIDITY_BREAKOUT_ID in STRATEGIES


def test_fvg_liquidity_breakout_delta_lanes_are_mainline_shadow_5m():
    lanes = fvg_liquidity_breakout_delta_lanes({})

    assert {lane.symbol for lane in lanes} == {
        "ETH/USD:USD",
        "BTC/USD:USD",
        "SOL/USD:USD",
        "XRP/USD:USD",
    }
    assert all(lane.exchange == "delta_india" for lane in lanes)
    assert all(lane.timeframe == "5m" for lane in lanes)
    assert all(lane.strategy_id == FVG_LIQUIDITY_BREAKOUT_ID for lane in lanes)
    assert all(lane.mode is RunnerMode.SHADOW for lane in lanes)
    assert all(
        lane.strategy_params["min_expected_net_edge_bps"] == 25.0
        for lane in lanes
    )


def test_fvg_liquidity_breakout_lanes_can_be_paper_observed_without_promotion():
    # fvg is pruned from the default roster (2026-08-03 hard-cut); prune-off
    # verifies the generator + paper-observation wiring still work.
    specs = desired_lane_specs(
        {"MULTI_LANE_PAPER_OBSERVE_ALL": "1", "MULTI_LANE_PRUNE_DEAD": "0"}
    )
    ids = {spec.lane_id for spec in specs}

    assert "fvg_liquidity_breakout_delta_india_eth_usd_usd_shadow" in ids
    assert "fvg_liquidity_breakout_delta_india_eth_usd_usd_paper_observation" in ids
    observed = next(
        spec
        for spec in specs
        if spec.lane_id
        == "fvg_liquidity_breakout_delta_india_eth_usd_usd_paper_observation"
    )
    assert observed.mode is RunnerMode.PAPER
    assert observed.timeframe == "5m"
    assert observed.is_primary is False


def test_fvg_liquidity_breakout_mainline_lanes_can_be_disabled():
    assert (
        fvg_liquidity_breakout_delta_lanes(
            {"MULTI_LANE_FVG_LIQUIDITY_DELTA": "0"}
        )
        == []
    )
