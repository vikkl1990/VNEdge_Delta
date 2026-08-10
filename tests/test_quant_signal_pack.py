"""Quant Signal Pack — causal commercial-style confluence lane."""

import pandas as pd
import pytest

from vnedge.data.schemas import normalize_candles
from vnedge.strategy.quant_signal_pack import (
    QuantSignalPack,
    QuantSignalPackParams,
    add_quant_signal_pack_columns,
)

BASE = 1_750_000_000_000
HOUR = 3_600_000


def params() -> QuantSignalPackParams:
    return QuantSignalPackParams(
        structure_window=14,
        liquidity_window=14,
        atr_window=6,
        atr_pct_window=24,
        ema_fast=5,
        ema_mid=10,
        ema_slow=18,
        er_window=8,
        vwap_window=14,
        volume_z_window=14,
        squeeze_window=10,
        squeeze_pct_window=24,
        squeeze_lookback=5,
        min_er=0.05,
        min_volume_z=0.20,
        min_atr_pct=0.0,
        max_atr_pct=1.0,
        fvg_min_atr=0.08,
        displacement_atr=0.35,
        squeeze_max_pct=0.50,
        vwap_extreme_atr=0.70,
    )


def make_candles(rows):
    return normalize_candles([
        [BASE + i * HOUR, open_, high, low, close, volume]
        for i, (open_, high, low, close, volume) in enumerate(rows)
    ])


def trend_rows(n=70, start=100.0, step=0.08, volume=100.0):
    rows = []
    prev = start
    for i in range(n):
        close = start + i * step
        high = max(prev, close) + 0.10
        low = min(prev, close) - 0.10
        rows.append((prev, high, low, close, volume))
        prev = close
    return rows


def test_quant_pack_fires_on_liquidity_sweep_reclaim():
    rows = trend_rows()
    prior_pool = min(row[2] for row in rows[-20:-1])
    prev_close = rows[-1][3]
    rows.append((
        prev_close - 1.2,
        prev_close + 1.6,
        prior_pool - 0.8,
        prev_close + 1.3,
        320.0,
    ))
    candles = make_candles(rows)
    strategy = QuantSignalPack(params=params(), min_score=4.5, structure_window=14)
    df = strategy.prepare(candles)

    intent = strategy.signal(df, len(df) - 1)

    assert intent is not None
    assert intent.side == "long"
    assert "liquidity_sweep" in intent.reason
    assert bool(df["sweep_low"].iloc[-1])
    assert intent.stop_price < float(df["close"].iloc[-1])
    assert intent.take_profit_price > float(df["close"].iloc[-1])


def test_quant_pack_family_filter_blocks_mismatched_signal():
    rows = trend_rows()
    prior_pool = min(row[2] for row in rows[-20:-1])
    prev_close = rows[-1][3]
    rows.append((
        prev_close - 1.2,
        prev_close + 1.6,
        prior_pool - 0.8,
        prev_close + 1.3,
        320.0,
    ))
    candles = make_candles(rows)
    strategy = QuantSignalPack(
        params=params(),
        min_score=4.5,
        structure_window=14,
        allowed_families=["fvg_retest"],
    )
    df = strategy.prepare(candles)

    assert strategy.signal(df, len(df) - 1) is None


def test_quant_pack_side_filter_blocks_mismatched_signal():
    rows = trend_rows()
    prior_pool = min(row[2] for row in rows[-20:-1])
    prev_close = rows[-1][3]
    rows.append((
        prev_close - 1.2,
        prev_close + 1.6,
        prior_pool - 0.8,
        prev_close + 1.3,
        320.0,
    ))
    candles = make_candles(rows)
    strategy = QuantSignalPack(
        params=params(),
        min_score=4.5,
        structure_window=14,
        allowed_sides=["short"],
    )
    df = strategy.prepare(candles)

    assert strategy.signal(df, len(df) - 1) is None


def test_quant_pack_rejects_unknown_filters():
    with pytest.raises(ValueError, match="allowed_families"):
        QuantSignalPack(params=params(), allowed_families=["moon_phase"])
    with pytest.raises(ValueError, match="allowed_sides"):
        QuantSignalPack(params=params(), allowed_sides=["flat"])


def test_quant_pack_fires_on_bullish_fvg_retest():
    rows = trend_rows()
    prev = rows[-1][3]
    rows.append((prev, prev + 0.10, prev - 0.70, prev - 0.45, 90.0))
    gap_floor = rows[-2][1]
    gap_low = gap_floor + 0.55
    rows.append((prev - 0.45, gap_low + 1.4, gap_low, gap_low + 1.25, 340.0))
    rows.append((gap_low + 0.55, gap_low + 1.25, gap_low - 0.08, gap_low + 0.95, 260.0))
    candles = make_candles(rows)
    strategy = QuantSignalPack(params=params(), min_score=4.0, structure_window=14)
    df = strategy.prepare(candles)

    intent = strategy.signal(df, len(df) - 1)

    assert intent is not None
    assert intent.side == "long"
    assert "fvg_retest" in intent.reason
    assert bool(df["bullish_fvg_retest"].iloc[-1])


def test_quant_pack_fires_on_squeeze_release_short():
    rows = []
    prev = 100.0
    for i in range(80):
        close = 100.0 + (0.04 if i % 2 else -0.04)
        rows.append((prev, max(prev, close) + 0.04, min(prev, close) - 0.04, close, 100.0))
        prev = close
    rows.append((prev, prev + 0.05, prev - 2.4, prev - 2.1, 330.0))
    candles = make_candles(rows)
    strategy = QuantSignalPack(params=params(), min_score=3.5, structure_window=14)
    df = strategy.prepare(candles)

    intent = strategy.signal(df, len(df) - 1)

    assert intent is not None
    assert intent.side == "short"
    assert "squeeze_release" in intent.reason
    assert bool(df["squeeze_release_down"].iloc[-1])


def test_quant_pack_blocks_low_confluence_chop():
    rows = []
    prev = 100.0
    for i in range(90):
        close = 100.0 + (0.15 if i % 2 else -0.15)
        rows.append((prev, max(prev, close) + 0.08, min(prev, close) - 0.08, close, 100.0))
        prev = close
    candles = make_candles(rows)
    strategy = QuantSignalPack(params=params(), min_score=5.0, structure_window=14)
    df = strategy.prepare(candles)

    assert strategy.signal(df, len(df) - 1) is None


def test_quant_pack_features_are_causal_when_future_changes():
    candles = make_candles(trend_rows(100))
    mutated = candles.copy()
    mutated.loc[71:, ["open", "high", "low", "close"]] *= 1.4
    p = params()

    a = add_quant_signal_pack_columns(candles, p)
    b = add_quant_signal_pack_columns(mutated, p)

    cols = [
        "prior_high",
        "prior_low",
        "liquidity_high",
        "liquidity_low",
        "rolling_vwap",
        "bias_long",
        "sweep_low",
        "bullish_fvg_retest",
        "squeeze_release_up",
        "long_score",
        "short_score",
    ]
    pd.testing.assert_frame_equal(a.loc[:70, cols], b.loc[:70, cols])
