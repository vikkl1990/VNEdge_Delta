"""DATrend / NomadaScalper protected-source proxy tests."""

import pandas as pd

from vnedge.data.schemas import normalize_candles
from vnedge.research.execution_edge_labeler import strategy_signal_events
from vnedge.strategy.datrend_nomada_scalper import (
    DATREND_NOMADA_SCALPER_ID,
    DATrendNomadaScalper,
    DATrendNomadaScalperParams,
    add_datrend_nomada_columns,
)
from vnedge.strategy.strategy_registry import STRATEGIES, get_strategy_class


BASE = 1_750_000_000_000
FIVE_MIN = 300_000


def params() -> DATrendNomadaScalperParams:
    return DATrendNomadaScalperParams(
        short_cycle=3,
        medium_cycle=6,
        long_cycle_mult=2,
        cycle_band_atr_mult=0.55,
        wma_fast=5,
        wma_slow=10,
        hold_window=4,
        hold_pct=0.5,
        slope_lookback=2,
        er_window=4,
        er_memory_window=5,
        min_er_memory=0.0,
        atr_percentile_window=12,
        max_atr_percentile=1.0,
        use_daily_cloud=False,
        cloud_length=2,
        ribbon_fast=3,
        ribbon_mid=5,
        ribbon_slow=8,
        bias_ema=8,
        rsi_window=4,
        rsi_smooth=3,
        min_panel_dots=1,
        arm_window=8,
        extreme_memory_window=8,
        pivot_window=8,
        stop_atr_mult=0.8,
        min_stop_bps=5.0,
        take_profit_r=2.0,
        min_expected_net_edge_bps=0.0,
    )


def make_candles(rows):
    return normalize_candles(
        [
            [BASE + i * FIVE_MIN, open_, high, low, close, volume]
            for i, (open_, high, low, close, volume) in enumerate(rows)
        ]
    )


def datrend_rows(direction: str, n: int = 130, start: float = 100.0):
    rows = []
    prev = start
    drift = 0.28 if direction == "long" else -0.28
    for i in range(n - 18):
        close = prev + drift + (0.04 if i % 3 == 0 else -0.01)
        rows.append((prev, max(prev, close) + 0.20, min(prev, close) - 0.20, close, 120 + i))
        prev = close

    if direction == "long":
        moves = [-3.7, -2.3, -1.2, -0.4, 0.7, 1.6, -0.8, 0.9, 1.6, 2.2]
    else:
        moves = [3.7, 2.3, 1.2, 0.4, -0.7, -1.6, 0.8, -0.9, -1.6, -2.2]
    for move in moves:
        close = prev + move
        high = max(prev, close) + abs(move) * 0.35 + 0.25
        low = min(prev, close) - abs(move) * 0.35 - 0.25
        rows.append((prev, high, low, close, 320.0))
        prev = close
    while len(rows) < n:
        close = prev + (0.45 if direction == "long" else -0.45)
        rows.append((prev, max(prev, close) + 0.20, min(prev, close) - 0.20, close, 180.0))
        prev = close
    return rows


def test_datrend_nomada_fires_on_long_golden_marker_proxy():
    candles = make_candles(datrend_rows("long"))
    strategy = DATrendNomadaScalper(params=params())

    events = strategy_signal_events(candles, strategy)

    assert any(event.side == "long" for event in events)
    event = next(event for event in events if event.side == "long")
    assert event.stop_price < float(candles["close"].iloc[-1])
    assert event.take_profit_price is not None
    assert event.expected_edge_bps is not None
    assert event.fill_probability is not None
    assert "protected_source_proxy" in event.metadata["reason"]
    assert "trigger=golden_marker" in event.metadata["reason"]


def test_datrend_nomada_fires_on_short_golden_marker_proxy():
    candles = make_candles(datrend_rows("short"))
    strategy = DATrendNomadaScalper(params=params())

    events = strategy_signal_events(candles, strategy)

    assert any(event.side == "short" for event in events)
    event = next(event for event in events if event.side == "short")
    assert event.stop_price > float(candles["close"].iloc[-1])
    assert event.take_profit_price is not None


def test_datrend_nomada_columns_are_causal_when_future_changes():
    candles = make_candles(datrend_rows("long", n=150))
    mutated = candles.copy()
    mutated.loc[101:, ["open", "high", "low", "close"]] *= 1.25
    mutated.loc[101:, "volume"] *= 4.0

    original = add_datrend_nomada_columns(candles, params())
    changed = add_datrend_nomada_columns(mutated, params())

    cols = [
        "datrend_fast",
        "datrend_slow",
        "datrend_macro",
        "datrend_er_memory",
        "trend_gate_long",
        "cloud_gate_long",
        "panel_score_long",
        "datrend_golden_long",
        "expected_net_edge_bps_long",
    ]
    pd.testing.assert_frame_equal(original.loc[:100, cols], changed.loc[:100, cols])


def test_datrend_nomada_is_registered():
    assert get_strategy_class(DATREND_NOMADA_SCALPER_ID) is DATrendNomadaScalper
    assert DATREND_NOMADA_SCALPER_ID in STRATEGIES
