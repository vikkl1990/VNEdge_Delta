"""Quantified fee-wall sniper scanner tests."""

from __future__ import annotations

import pandas as pd

from vnedge.data.schemas import normalize_candles
from vnedge.runtime.multi_lane import _build_single_strategy
from vnedge.strategy.quantified_fee_wall_sniper import (
    QUANTIFIED_FEE_WALL_SNIPER_ID,
    QuantifiedFeeWallSniper,
    QuantifiedFeeWallSniperParams,
    add_quantified_fee_wall_sniper_columns,
)
from vnedge.strategy.strategy_registry import STRATEGIES, get_strategy_class


BASE = 1_750_000_000_000
FIVE_MIN = 300_000


def params(**overrides) -> QuantifiedFeeWallSniperParams:
    base = {
        "fast_ema": 4,
        "slow_ema": 8,
        "bias_ema": 12,
        "atr_window": 4,
        "er_window": 5,
        "range_lookback": 8,
        "compression_lookback": 6,
        "structure_window": 8,
        "volume_z_window": 8,
        "bbp_window": 4,
        "pullback_memory_bars": 3,
        "min_er": 0.0,
        "compression_ratio": 10.0,
        "min_body_atr": 0.30,
        "min_volume_z": 0.10,
        "min_bbp_slope_bps": -20.0,
        "min_room_to_liquidity_bps": 10.0,
        "min_expected_net_edge_bps": 25.0,
        "min_quality_score": 0.50,
        "safety_buffer_bps": 8.0,
    }
    base.update(overrides)
    return QuantifiedFeeWallSniperParams(**base)


def make_candles(rows):
    return normalize_candles(
        [
            [BASE + i * FIVE_MIN, open_, high, low, close, volume]
            for i, (open_, high, low, close, volume) in enumerate(rows)
        ]
    )


def breakout_rows(n: int = 48, start: float = 100.0):
    rows = []
    prev = start
    for i in range(n):
        if i < n - 1:
            close = prev + 0.025 + (0.01 if i % 5 == 0 else 0.0)
            high = max(prev, close) + 0.08
            low = min(prev, close) - 0.08
            volume = 100.0 + (i % 4)
        else:
            close = prev + 2.4
            high = close + 0.34
            low = prev - 0.05
            volume = 520.0
        rows.append((prev, high, low, close, volume))
        prev = close
    return rows


def pullback_rows(n: int = 56, start: float = 100.0):
    rows = []
    prev = start
    for i in range(n):
        if i < n - 5:
            close = prev + 0.10
            high = close + 0.10
            low = prev - 0.06
            volume = 100.0 + i
        elif i < n - 1:
            close = prev - 0.22
            high = prev + 0.08
            low = close - 0.28
            volume = 125.0 + i
        else:
            close = prev + 1.55
            high = close + 0.25
            low = prev - 0.10
            volume = 500.0
        rows.append((prev, high, low, close, volume))
        prev = close
    return rows


def test_quantified_fee_wall_sniper_breakout_fires_only_after_fee_wall():
    candles = make_candles(breakout_rows())
    strategy = QuantifiedFeeWallSniper(params=params())
    df = strategy.prepare(candles)

    intent = strategy.signal(df, len(df) - 1)

    assert intent is not None
    assert intent.side == "long"
    assert intent.stop_price < float(df["close"].iloc[-1]) < intent.take_profit_price
    assert len(intent.take_profit_levels) == 3
    assert (
        intent.take_profit_levels[0]
        < intent.take_profit_levels[1]
        < intent.take_profit_levels[2]
    )
    assert float(df["expected_net_edge_bps_long"].iloc[-1]) >= 25.0
    assert "feeWall=20.0" in intent.reason
    assert "profitFloor=25.0" in intent.reason
    assert "takerFallback=" in intent.reason
    assert "TP1_partial" in intent.reason


def test_quantified_fee_wall_sniper_blocks_when_required_net_is_impossible():
    candles = make_candles(breakout_rows())
    strategy = QuantifiedFeeWallSniper(
        params=params(min_expected_net_edge_bps=10_000.0)
    )
    df = strategy.prepare(candles)

    assert strategy.signal(df, len(df) - 1) is None


def test_quantified_fee_wall_sniper_pullback_emits_active_exit_ladder():
    candles = make_candles(pullback_rows())
    strategy = QuantifiedFeeWallSniper(params=params(compression_ratio=0.1))
    df = strategy.prepare(candles)

    intent = strategy.signal(df, len(df) - 1)

    assert intent is not None
    assert intent.side == "long"
    assert "setup=pullback_continuation" in intent.reason
    assert intent.take_profit_levels[-1] == intent.take_profit_price


def test_quantified_fee_wall_sniper_can_run_pullback_only_for_quantified_proof():
    breakout = QuantifiedFeeWallSniper(params=params(enabled_setups=("pullback",)))
    breakout_df = breakout.prepare(make_candles(breakout_rows()))

    assert breakout_df["breakout_long"].iloc[-1]
    assert breakout.signal(breakout_df, len(breakout_df) - 1) is None

    pullback = QuantifiedFeeWallSniper(
        params=params(compression_ratio=0.1, enabled_setups=("pullback",))
    )
    pullback_df = pullback.prepare(make_candles(pullback_rows()))

    intent = pullback.signal(pullback_df, len(pullback_df) - 1)

    assert intent is not None
    assert "setup=pullback_continuation" in intent.reason


def test_quantified_fee_wall_sniper_rejects_unknown_setup_mode():
    try:
        QuantifiedFeeWallSniper(params=params(enabled_setups=("future_leak",)))
    except ValueError as exc:
        assert "unsupported setup" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("unknown setup mode should be rejected")


def test_quantified_fee_wall_sniper_columns_are_causal_when_future_changes():
    candles = make_candles(breakout_rows(70))
    mutated = candles.copy()
    mutated.loc[46:, ["open", "high", "low", "close"]] *= 1.35
    mutated.loc[46:, "volume"] *= 5.0
    p = params()

    a = add_quantified_fee_wall_sniper_columns(candles, p)
    b = add_quantified_fee_wall_sniper_columns(mutated, p)

    cols = [
        "ema_fast",
        "ema_slow",
        "atr_value",
        "range_high",
        "compression_ready",
        "bbp_bps",
        "expected_net_edge_bps_long",
        "expected_net_edge_bps_short",
    ]
    pd.testing.assert_frame_equal(a.loc[:45, cols], b.loc[:45, cols])


def test_quantified_fee_wall_sniper_registered_and_runtime_constructible():
    assert get_strategy_class(QUANTIFIED_FEE_WALL_SNIPER_ID) is QuantifiedFeeWallSniper
    assert QUANTIFIED_FEE_WALL_SNIPER_ID in STRATEGIES

    built = _build_single_strategy(
        QUANTIFIED_FEE_WALL_SNIPER_ID,
        {"min_expected_net_edge_bps": 25.0},
        pd.DataFrame(),
        None,
    )

    assert isinstance(built, QuantifiedFeeWallSniper)
