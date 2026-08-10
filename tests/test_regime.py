"""Regime classifier on constructed series."""

import pandas as pd

from vnedge.data.schemas import normalize_candles
from vnedge.strategy.regime import RegimeParams, add_regime_columns, merge_funding, regime_warmup_bars

BASE = 1_750_000_000_000
HOUR = 3_600_000

# Small windows so tests stay fast and readable.
P = RegimeParams(ema_fast=6, ema_slow=24, er_window=12, atr_window=6, atr_pct_window=48)


def candles_from_closes(closes: list[float]) -> pd.DataFrame:
    raw, prev = [], closes[0]
    for i, c in enumerate(closes):
        raw.append([BASE + i * HOUR, prev, max(prev, c) * 1.002, min(prev, c) * 0.998, c, 10.0])
        prev = c
    return normalize_candles(raw)


def test_uptrend_classified_trend_up():
    closes = [100 * 1.005**i for i in range(120)]
    df = add_regime_columns(candles_from_closes(closes), P)
    tail = df.iloc[regime_warmup_bars(P):]
    assert tail["regime_trend_up"].all()
    assert not tail["regime_trend_down"].any()


def test_downtrend_classified_trend_down():
    closes = [100 * 0.995**i for i in range(120)]
    df = add_regime_columns(candles_from_closes(closes), P)
    tail = df.iloc[regime_warmup_bars(P):]
    assert tail["regime_trend_down"].all()


def test_chop_is_neither_regime():
    closes = [100.0 + (0.5 if i % 2 else -0.5) for i in range(120)]
    df = add_regime_columns(candles_from_closes(closes), P)
    tail = df.iloc[regime_warmup_bars(P):]
    assert not tail["regime_trend_up"].any()
    assert not tail["regime_trend_down"].any()


def test_merge_funding_is_backward_only():
    candles = candles_from_closes([100.0] * 10)
    funding = pd.DataFrame(
        {
            "timestamp": pd.to_datetime([BASE + 5 * HOUR], unit="ms", utc=True),
            "funding_rate": [0.01],
        }
    )
    merged = merge_funding(candles, funding)
    assert (merged["funding_rate"].iloc[:5] == 0.0).all()   # before event: nothing known
    assert (merged["funding_rate"].iloc[5:] == 0.01).all()  # after: last known value


def test_merge_funding_none_defaults_to_zero():
    merged = merge_funding(candles_from_closes([100.0] * 5), None)
    assert (merged["funding_rate"] == 0.0).all()
