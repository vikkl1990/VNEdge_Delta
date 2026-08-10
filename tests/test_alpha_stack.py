"""AlphaStack confluence lane — causal structure signals, not chart art."""

import pandas as pd

from vnedge.data.schemas import normalize_candles
from vnedge.strategy.alpha_stack import (
    AlphaStackConfluence,
    AlphaStackParams,
    add_alpha_stack_columns,
)

BASE = 1_750_000_000_000
HOUR = 3_600_000


def params() -> AlphaStackParams:
    return AlphaStackParams(
        structure_window=24,
        atr_window=8,
        atr_pct_window=40,
        ema_fast=8,
        ema_slow=21,
        vwap_window=30,
        er_window=16,
        momentum_window=3,
        volume_z_window=30,
        max_atr_pct=1.0,
        min_volume_z=0.25,
        fvg_min_atr=0.10,
        displacement_atr=0.40,
    )


def candles_from(closes, *, volumes=None, force_last_gap: str | None = None):
    raw = []
    prev = closes[0]
    for i, close in enumerate(closes):
        vol = volumes[i] if volumes else 100.0
        high = max(prev, close) * 1.002
        low = min(prev, close) * 0.998
        if force_last_gap == "bull" and i == len(closes) - 1:
            high = close * 1.001
            low = max(raw[-2][2] * 1.003, min(prev, close) * 1.001)
        if force_last_gap == "bear" and i == len(closes) - 1:
            low = close * 0.999
            high = min(raw[-2][3] * 0.997, max(prev, close) * 0.999)
        raw.append([BASE + i * HOUR, prev, high, low, close, vol])
        prev = close
    return normalize_candles(raw)


def first_signal(strategy, candles):
    df = strategy.prepare(candles)
    for i in range(strategy.warmup_bars, len(df)):
        intent = strategy.signal(df, i)
        if intent is not None:
            return i, intent, df
    return None, None, df


def test_alpha_stack_fires_on_bullish_structure_liquidity_confluence():
    closes = [100.0 + i * 0.06 for i in range(90)]
    closes += [closes[-1] - 0.8, closes[-1] + 2.0]
    volumes = [100.0] * 90 + [95.0, 250.0]
    candles = candles_from(closes, volumes=volumes, force_last_gap="bull")
    strategy = AlphaStackConfluence(params=params(), structure_window=24)

    index, intent, df = first_signal(strategy, candles)

    assert intent is not None
    assert intent.side == "long"
    assert intent.stop_price < float(df["close"].iloc[index])
    assert intent.take_profit_price > float(df["close"].iloc[index])
    assert "alpha_stack long" in intent.reason
    assert float(df["long_score"].iloc[index]) >= strategy.params.min_score


def test_alpha_stack_fires_on_bearish_structure_liquidity_confluence():
    closes = [120.0 - i * 0.06 for i in range(90)]
    closes += [closes[-1] + 0.8, closes[-1] - 2.0]
    volumes = [100.0] * 90 + [95.0, 250.0]
    candles = candles_from(closes, volumes=volumes, force_last_gap="bear")
    strategy = AlphaStackConfluence(params=params(), structure_window=24)

    index, intent, df = first_signal(strategy, candles)

    assert intent is not None
    assert intent.side == "short"
    assert intent.stop_price > float(df["close"].iloc[index])
    assert intent.take_profit_price < float(df["close"].iloc[index])
    assert "alpha_stack short" in intent.reason
    assert float(df["short_score"].iloc[index]) >= strategy.params.min_score


def test_alpha_stack_blocks_low_confluence_chop():
    closes = [100.0 + (0.15 if i % 2 else -0.15) for i in range(120)]
    candles = candles_from(closes)
    strategy = AlphaStackConfluence(params=params(), structure_window=24)

    _, intent, _ = first_signal(strategy, candles)

    assert intent is None


def test_alpha_stack_features_are_causal_when_future_changes():
    closes = [100.0 + i * 0.04 for i in range(120)]
    original = candles_from(closes)
    mutated = original.copy()
    mutated.loc[91:, ["open", "high", "low", "close"]] *= 1.25
    p = params()

    a = add_alpha_stack_columns(original, p)
    b = add_alpha_stack_columns(mutated, p)

    cols = [
        "prior_high", "prior_low", "rolling_vwap", "trend_up",
        "bos_up", "sweep_low", "bullish_fvg", "long_score",
        "short_score",
    ]
    pd.testing.assert_frame_equal(a.loc[:90, cols], b.loc[:90, cols])
