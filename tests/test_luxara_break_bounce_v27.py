"""Luxara Break & Bounce V27 scanner adaptation tests."""

import pandas as pd

from vnedge.data.schemas import normalize_candles
from vnedge.research.execution_edge_labeler import strategy_signal_events
from vnedge.strategy.luxara_break_bounce_v27 import (
    LUXARA_BREAK_BOUNCE_V27_ID,
    LuxaraBreakBounceV27Params,
    LuxaraBreakBounceV27Scanner,
    add_luxara_break_bounce_v27_columns,
)
from vnedge.strategy.strategy_registry import STRATEGIES, get_strategy_class


BASE = 1_750_000_000_000
FIFTEEN_MIN = 900_000


def params() -> LuxaraBreakBounceV27Params:
    return LuxaraBreakBounceV27Params(
        setup_lookback=8,
        signal_mode="close_outside_box",
        cooldown_bars=0,
        ema_fast=5,
        ema_slow=12,
        atr_window=5,
        volume_sma_window=5,
        min_volume_ratio=0.1,
        liquidity_lookback=16,
        measured_move_fraction=0.75,
        min_grade_score=2,
        min_box_width_atr=0.1,
        max_box_width_atr=20.0,
        min_breakout_bps=0.0,
        min_room_to_liquidity_bps=0.0,
        min_expected_net_edge_bps=0.0,
        min_fill_probability=0.0,
        stop_atr_mult=0.7,
        stop_buffer_atr=0.05,
        min_stop_bps=5.0,
        take_profit_r=2.0,
        slippage_bps=1.0,
        safety_buffer_bps=1.0,
        allowed_sides=(),
    )


def make_candles(rows):
    return normalize_candles(
        [
            [BASE + i * FIFTEEN_MIN, open_, high, low, close, volume]
            for i, (open_, high, low, close, volume) in enumerate(rows)
        ]
    )


def breakout_rows(direction: str, n: int = 180, start: float = 100.0):
    rows = []
    prev = start
    for i in range(n):
        phase = i % 6
        drift = 0.015 if direction == "up" else -0.015
        close = prev + drift + (phase - 2.5) * 0.012
        if n - 18 <= i < n - 3:
            box_mid = prev + (0.02 if direction == "up" else -0.02)
            close = box_mid + (0.05 if i % 2 else -0.04)
        if i == n - 3:
            close = prev + (1.30 if direction == "up" else -1.30)
        high = max(prev, close) + (0.16 if i != n - 3 else 0.38)
        low = min(prev, close) - (0.16 if i != n - 3 else 0.38)
        volume = 100.0 + i
        if i >= n - 8:
            volume *= 2.4
        rows.append((prev, high, low, close, volume))
        prev = close
    return rows


def test_luxara_break_bounce_fires_on_long_box_breakout():
    candles = make_candles(breakout_rows("up"))
    strategy = LuxaraBreakBounceV27Scanner(params=params())

    events = strategy_signal_events(candles, strategy)

    assert events
    assert any(event.side == "long" for event in events)
    event = next(event for event in reversed(events) if event.side == "long")
    assert event.stop_price < float(candles["close"].iloc[-3])
    assert event.take_profit_price is not None
    assert "luxara_break_bounce_v27 long" in event.metadata["reason"]
    assert "trigger=close_breakout" in event.metadata["reason"]
    assert event.expected_edge_bps is not None
    assert event.fill_probability is not None


def test_luxara_break_bounce_fires_on_short_box_breakout():
    candles = make_candles(breakout_rows("down"))
    strategy = LuxaraBreakBounceV27Scanner(params=params())

    events = strategy_signal_events(candles, strategy)

    assert events
    assert any(event.side == "short" for event in events)
    event = next(event for event in reversed(events) if event.side == "short")
    assert event.stop_price > float(candles["close"].iloc[-3])
    assert event.take_profit_price is not None


def test_luxara_break_bounce_columns_are_causal_when_future_changes():
    candles = make_candles(breakout_rows("up", n=220))
    mutated = candles.copy()
    mutated.loc[141:, ["open", "high", "low", "close"]] *= 1.25
    mutated.loc[141:, "volume"] *= 3.0
    p = params()

    a = add_luxara_break_bounce_v27_columns(candles, p)
    b = add_luxara_break_bounce_v27_columns(mutated, p)

    cols = [
        "bb_atr",
        "bb_ema_fast",
        "bb_ema_slow",
        "bb_box_high",
        "bb_box_low",
        "bb_box_mid",
        "bb_box_width_atr",
        "bb_volume_ratio",
        "bb_signal_long_raw",
        "bb_signal_short_raw",
        "bb_grade_score_long",
        "bb_grade_score_short",
        "bb_room_to_liquidity_long",
        "expected_net_edge_bps_long",
    ]
    pd.testing.assert_frame_equal(a.loc[:140, cols], b.loc[:140, cols])


def test_luxara_break_bounce_reason_feeds_router_metadata():
    candles = make_candles(breakout_rows("up"))
    strategy = LuxaraBreakBounceV27Scanner(params=params())

    events = strategy_signal_events(candles, strategy)

    assert events
    assert events[-1].expected_edge_bps is not None
    assert events[-1].fill_probability is not None
    assert 0.0 <= events[-1].fill_probability <= 1.0


def test_luxara_break_bounce_is_registered():
    assert get_strategy_class(LUXARA_BREAK_BOUNCE_V27_ID) is LuxaraBreakBounceV27Scanner
    assert LUXARA_BREAK_BOUNCE_V27_ID in STRATEGIES
