"""Lux/UT forecast scanner adaptation tests."""

import pandas as pd

from vnedge.data.schemas import normalize_candles
from vnedge.research.execution_edge_labeler import strategy_signal_events
from vnedge.strategy.luxy_ut_bot_forecast import (
    LUXY_UT_BOT_FORECAST_ID,
    LuxyUTBotForecastParams,
    LuxyUTBotForecastScanner,
    add_luxy_ut_bot_forecast_columns,
)
from vnedge.strategy.strategy_registry import STRATEGIES, get_strategy_class


BASE = 1_750_000_000_000
FIFTEEN_MIN = 900_000


def params() -> LuxyUTBotForecastParams:
    return LuxyUTBotForecastParams(
        ut_key=0.7,
        ut_atr_window=5,
        crypto_asset_multiplier=1.0,
        volatility_ratio_window=12,
        efficiency_window=4,
        chop_strength=0.1,
        volume_sma_window=8,
        min_volume_ratio=0.2,
        supertrend_atr_window=5,
        supertrend_multiplier=1.7,
        adx_window=5,
        adx_threshold=0.0,
        mtf_fast_ema=2,
        mtf_slow_ema=4,
        mtf_er_window=3,
        mtf_adx_window=3,
        min_1h_er=0.0,
        min_4h_er=0.0,
        min_1h_adx=0.0,
        min_4h_adx=0.0,
        rsi_window=5,
        divergence_pivot_window=2,
        divergence_recent_window=8,
        structure_window=6,
        zone_window=20,
        body_percentile_window=10,
        displacement_atr_floor=0.05,
        displacement_percentile_floor=0.35,
        min_confidence=45.0,
        min_expected_net_edge_bps=0.0,
        cooldown_bars=0,
        allow_continuations=True,
        stop_atr_window=5,
        stop_atr_multiplier=0.8,
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


def trend_rows(direction: str, n: int = 220, start: float = 100.0):
    rows = []
    prev = start
    drift = 0.05 if direction == "up" else -0.05
    for i in range(n):
        if i < 45:
            close = prev - drift * 0.8
        elif i < n - 1:
            close = prev + drift + (0.01 if direction == "up" else -0.01) * (i % 3)
        else:
            close = prev + (0.95 if direction == "up" else -0.95)
        high = max(prev, close) + (0.10 if i < n - 1 else 0.42)
        low = min(prev, close) - (0.10 if i < n - 1 else 0.42)
        volume = 100.0 + i * 1.5
        if i == n - 1:
            volume *= 4.0
        rows.append((prev, high, low, close, volume))
        prev = close
    return rows


def test_luxy_ut_bot_forecast_fires_on_day_trading_long_setup():
    candles = make_candles(trend_rows("up"))
    strategy = LuxyUTBotForecastScanner(params=params())
    df = strategy.prepare(candles)

    intent = strategy.signal(df, len(df) - 1)

    assert intent is not None
    assert intent.side == "long"
    assert intent.stop_price < float(df["close"].iloc[-1]) < intent.take_profit_price
    assert "luxy_ut_bot_forecast long" in intent.reason
    assert "confidence=" in intent.reason
    assert "expectedEdge=" in intent.reason
    assert "fillProbability=" in intent.reason
    assert "tp_ladder=" in intent.reason


def test_luxy_ut_bot_forecast_fires_on_day_trading_short_setup():
    candles = make_candles(trend_rows("down"))
    strategy = LuxyUTBotForecastScanner(params=params())
    df = strategy.prepare(candles)

    intent = strategy.signal(df, len(df) - 1)

    assert intent is not None
    assert intent.side == "short"
    assert intent.stop_price > float(df["close"].iloc[-1]) > intent.take_profit_price


def test_luxy_ut_bot_forecast_columns_are_causal_when_future_changes():
    candles = make_candles(trend_rows("up", n=240))
    mutated = candles.copy()
    mutated.loc[141:, ["open", "high", "low", "close"]] *= 1.35
    mutated.loc[141:, "volume"] *= 5.0
    p = params()

    a = add_luxy_ut_bot_forecast_columns(candles, p)
    b = add_luxy_ut_bot_forecast_columns(mutated, p)

    cols = [
        "atr_ut",
        "ema_fast",
        "ema_slow",
        "volume_ratio",
        "efficiency_ratio",
        "ut_trail",
        "ut_trend",
        "supertrend",
        "supertrend_trend",
        "adx",
        "rsi",
        "body_atr",
        "body_percentile",
        "prior_high",
        "prior_low",
        "context_1h_ema_fast",
        "context_1h_ema_slow",
        "context_4h_ema_fast",
        "context_4h_ema_slow",
        "confidence_long",
        "expected_net_edge_bps_long",
    ]
    pd.testing.assert_frame_equal(a.loc[:140, cols], b.loc[:140, cols])


def test_luxy_ut_bot_forecast_reason_feeds_router_metadata():
    candles = make_candles(trend_rows("up"))
    strategy = LuxyUTBotForecastScanner(params=params())

    events = strategy_signal_events(candles, strategy)

    assert events
    assert events[-1].expected_edge_bps is not None
    assert events[-1].fill_probability is not None
    assert 0.0 <= events[-1].fill_probability <= 1.0


def test_luxy_ut_bot_forecast_is_registered():
    assert get_strategy_class(LUXY_UT_BOT_FORECAST_ID) is LuxyUTBotForecastScanner
    assert LUXY_UT_BOT_FORECAST_ID in STRATEGIES
