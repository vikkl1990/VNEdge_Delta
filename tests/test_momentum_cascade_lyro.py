"""Momentum Cascade scanner adaptation tests."""

import pandas as pd

from vnedge.data.schemas import normalize_candles
from vnedge.research.execution_edge_labeler import strategy_signal_events
from vnedge.strategy.momentum_cascade_lyro import (
    MOMENTUM_CASCADE_LYRO_ID,
    MomentumCascadeLyroParams,
    MomentumCascadeLyroScanner,
    add_momentum_cascade_lyro_columns,
)
from vnedge.strategy.strategy_registry import STRATEGIES, get_strategy_class


BASE = 1_750_000_000_000
FIFTEEN_MIN = 900_000


def params() -> MomentumCascadeLyroParams:
    return MomentumCascadeLyroParams(
        momentum_length=3,
        stage_smoothing=2,
        atr_window=5,
        volume_sma_window=6,
        body_percentile_window=8,
        structure_window=6,
        htf_ema_fast=2,
        htf_ema_slow=4,
        htf_er_window=2,
        htf_adx_window=2,
        min_1h_er=0.0,
        min_1h_adx=0.0,
        min_m3_abs=0.0,
        min_m3_slope=-99.0,
        min_coherence=0.0,
        min_volume_ratio=0.1,
        min_body_atr=0.0,
        min_body_percentile=0.0,
        min_confidence=20.0,
        min_expected_net_edge_bps=0.0,
        cooldown_bars=0,
        allow_continuations=True,
        max_continuation_trend_bars=24,
        stop_atr_mult=0.7,
        min_stop_bps=5.0,
        take_profit_r=2.0,
        slippage_bps=1.0,
        safety_buffer_bps=1.0,
    )


def make_candles(rows):
    return normalize_candles(
        [
            [BASE + i * FIFTEEN_MIN, open_, high, low, close, volume]
            for i, (open_, high, low, close, volume) in enumerate(rows)
        ]
    )


def cascade_rows(direction: str, n: int = 220, start: float = 100.0):
    rows = []
    prev = start
    first_drift = -0.05 if direction == "up" else 0.05
    second_drift = 0.16 if direction == "up" else -0.16
    final_push = 1.15 if direction == "up" else -1.15
    for i in range(n):
        drift = first_drift if i < n - 28 else second_drift
        close = prev + drift + (0.02 if direction == "up" else -0.02) * (i % 2)
        if i == n - 1:
            close = prev + final_push
        high = max(prev, close) + (0.14 if i < n - 1 else 0.44)
        low = min(prev, close) - (0.14 if i < n - 1 else 0.44)
        volume = 100.0 + i * 1.2
        if i >= n - 6:
            volume *= 2.5
        rows.append((prev, high, low, close, volume))
        prev = close
    return rows


def test_momentum_cascade_fires_on_long_flip():
    candles = make_candles(cascade_rows("up"))
    strategy = MomentumCascadeLyroScanner(params=params())

    events = strategy_signal_events(candles, strategy)

    assert events
    assert any(event.side == "long" for event in events)
    event = next(event for event in reversed(events) if event.side == "long")
    assert event.stop_price < float(candles["close"].iloc[-2])
    assert event.take_profit_price is not None
    assert "momentum_cascade_lyro long" in event.metadata["reason"]
    assert event.expected_edge_bps is not None
    assert event.fill_probability is not None


def test_momentum_cascade_fires_on_short_flip():
    candles = make_candles(cascade_rows("down"))
    strategy = MomentumCascadeLyroScanner(params=params())

    events = strategy_signal_events(candles, strategy)

    assert events
    assert any(event.side == "short" for event in events)
    event = next(event for event in reversed(events) if event.side == "short")
    assert event.stop_price > float(candles["close"].iloc[-2])
    assert event.take_profit_price is not None


def test_momentum_cascade_columns_are_causal_when_future_changes():
    candles = make_candles(cascade_rows("up", n=240))
    mutated = candles.copy()
    mutated.loc[141:, ["open", "high", "low", "close"]] *= 1.40
    mutated.loc[141:, "volume"] *= 4.0
    p = params()

    a = add_momentum_cascade_lyro_columns(candles, p)
    b = add_momentum_cascade_lyro_columns(mutated, p)

    cols = [
        "roc_pct",
        "cascade_m1",
        "cascade_m2",
        "cascade_m3",
        "cascade_score",
        "cascade_trend",
        "cascade_m3_slope",
        "cascade_coherence",
        "volume_ratio",
        "body_atr",
        "body_percentile",
        "prior_high",
        "prior_low",
        "context_1h_ema_fast",
        "context_1h_ema_slow",
        "context_1h_er",
        "context_1h_adx",
        "confidence_long",
        "expected_net_edge_bps_long",
    ]
    pd.testing.assert_frame_equal(a.loc[:140, cols], b.loc[:140, cols])


def test_momentum_cascade_reason_feeds_router_metadata():
    candles = make_candles(cascade_rows("up"))
    strategy = MomentumCascadeLyroScanner(params=params())

    events = strategy_signal_events(candles, strategy)

    assert events
    assert events[-1].expected_edge_bps is not None
    assert events[-1].fill_probability is not None
    assert 0.0 <= events[-1].fill_probability <= 1.0


def test_momentum_cascade_is_registered():
    assert get_strategy_class(MOMENTUM_CASCADE_LYRO_ID) is MomentumCascadeLyroScanner
    assert MOMENTUM_CASCADE_LYRO_ID in STRATEGIES
