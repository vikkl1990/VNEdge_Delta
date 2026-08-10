"""Luxara Live Plan QTM scanner adaptation tests."""

import pandas as pd

from vnedge.data.schemas import normalize_candles
from vnedge.research.execution_edge_labeler import strategy_signal_events
from vnedge.strategy.luxara_live_plan_qtm import (
    LUXARA_LIVE_PLAN_QTM_ID,
    LuxaraLivePlanQTMParams,
    LuxaraLivePlanQTMScanner,
    add_luxara_live_plan_qtm_columns,
)
from vnedge.strategy.strategy_registry import STRATEGIES, get_strategy_class


BASE = 1_750_000_000_000
FIVE_MIN = 300_000


def params() -> LuxaraLivePlanQTMParams:
    return LuxaraLivePlanQTMParams(
        atr_period=4,
        atr_multiplier=0.7,
        signal_mode="trend_flip",
        cooldown_bars=0,
        ema_length=5,
        use_ema_filter=False,
        rsi_length=3,
        rsi_buy_level=50.0,
        rsi_bear_level=50.0,
        structure_lookback=8,
        min_grade_score=2,
        min_volume_ratio=0.1,
        volume_sma_window=5,
        min_expected_net_edge_bps=0.0,
        min_room_to_liquidity_bps=0.0,
        min_fill_probability=0.0,
        stop_atr_mult=0.8,
        min_stop_bps=5.0,
        take_profit_r=2.0,
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


def flip_rows(direction: str, n: int = 180, start: float = 100.0):
    rows = []
    prev = start
    first_drift = -0.05 if direction == "up" else 0.05
    second_drift = 0.20 if direction == "up" else -0.20
    for i in range(n):
        drift = first_drift if i < n - 20 else second_drift
        close = prev + drift
        if i == n - 1:
            close = prev + (1.2 if direction == "up" else -1.2)
        high = max(prev, close) + (0.12 if i < n - 1 else 0.45)
        low = min(prev, close) - (0.12 if i < n - 1 else 0.45)
        volume = 100.0 + i
        if i >= n - 6:
            volume *= 2.8
        rows.append((prev, high, low, close, volume))
        prev = close
    return rows


def test_luxara_live_plan_qtm_fires_on_buy_plan():
    candles = make_candles(flip_rows("up"))
    strategy = LuxaraLivePlanQTMScanner(params=params())

    events = strategy_signal_events(candles, strategy)

    assert events
    assert any(event.side == "long" for event in events)
    event = next(event for event in reversed(events) if event.side == "long")
    assert event.stop_price < float(candles["close"].iloc[-2])
    assert event.take_profit_price is not None
    assert "luxara_live_plan_qtm long" in event.metadata["reason"]
    assert event.expected_edge_bps is not None
    assert event.fill_probability is not None


def test_luxara_live_plan_qtm_fires_on_sell_plan():
    candles = make_candles(flip_rows("down"))
    strategy = LuxaraLivePlanQTMScanner(params=params())

    events = strategy_signal_events(candles, strategy)

    assert events
    assert any(event.side == "short" for event in events)
    event = next(event for event in reversed(events) if event.side == "short")
    assert event.stop_price > float(candles["close"].iloc[-2])
    assert event.take_profit_price is not None


def test_luxara_live_plan_qtm_columns_are_causal_when_future_changes():
    candles = make_candles(flip_rows("up", n=220))
    mutated = candles.copy()
    mutated.loc[141:, ["open", "high", "low", "close"]] *= 1.25
    mutated.loc[141:, "volume"] *= 4.0
    p = params()

    a = add_luxara_live_plan_qtm_columns(candles, p)
    b = add_luxara_live_plan_qtm_columns(mutated, p)

    cols = [
        "qtm_atr",
        "qtm_ema",
        "qtm_rsi",
        "qtm_resistance",
        "qtm_support",
        "qtm_midline",
        "qtm_trail",
        "qtm_trend",
        "qtm_volume_ratio",
        "qtm_grade_score_long",
        "qtm_grade_score_short",
        "expected_net_edge_bps_long",
        "expected_net_edge_bps_short",
    ]
    pd.testing.assert_frame_equal(a.loc[:140, cols], b.loc[:140, cols])


def test_luxara_live_plan_qtm_reason_feeds_router_metadata():
    candles = make_candles(flip_rows("up"))
    strategy = LuxaraLivePlanQTMScanner(params=params())

    events = strategy_signal_events(candles, strategy)

    assert events
    assert events[-1].expected_edge_bps is not None
    assert events[-1].fill_probability is not None
    assert 0.0 <= events[-1].fill_probability <= 1.0


def test_luxara_live_plan_qtm_is_registered():
    assert get_strategy_class(LUXARA_LIVE_PLAN_QTM_ID) is LuxaraLivePlanQTMScanner
    assert LUXARA_LIVE_PLAN_QTM_ID in STRATEGIES
