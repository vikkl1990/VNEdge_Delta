"""Strategy candidates — signal construction, filters both ways, warmup."""

import pandas as pd
import pytest

from vnedge.data.schemas import normalize_candles
from vnedge.strategy.crypto_trend_atr_margin import CryptoTrendAtrMargin
from vnedge.strategy.funding_mean_reversion import FundingMeanReversion
from vnedge.strategy.regime import RegimeParams
from vnedge.strategy.strategy_registry import STRATEGIES, get_strategy_class
from vnedge.strategy.trend_continuation import TrendContinuation

BASE = 1_750_000_000_000
HOUR = 3_600_000

SMALL_REGIME = RegimeParams(
    ema_fast=6, ema_slow=24, er_window=12, atr_window=6, atr_pct_window=48
)


def candles_from_closes(closes: list[float]) -> pd.DataFrame:
    raw, prev = [], closes[0]
    for i, c in enumerate(closes):
        raw.append([BASE + i * HOUR, prev, max(prev, c) * 1.002, min(prev, c) * 0.998, c, 10.0])
        prev = c
    return normalize_candles(raw)


def funding_series(n_hours: int, rate: float, late_rate: float | None = None,
                   switch_at: int = 0) -> pd.DataFrame:
    rows = []
    for i in range(0, n_hours, 8):
        r = late_rate if (late_rate is not None and i >= switch_at) else rate
        rows.append({"timestamp": pd.Timestamp(BASE + i * HOUR, unit="ms", tz="UTC"),
                     "funding_rate": r})
    return pd.DataFrame(rows)


def first_signal(strategy, candles):
    df = strategy.prepare(candles)
    for i in range(strategy.warmup_bars, len(df)):
        intent = strategy.signal(df, i)
        if intent is not None:
            return i, intent
    return None, None


# --- Trend continuation -------------------------------------------------------

def uptrend_after_chop(chop_bars: int = 90, ramp_bars: int = 40) -> pd.DataFrame:
    closes = [100.0 + (0.1 if i % 2 else -0.1) for i in range(chop_bars)]
    for i in range(ramp_bars):
        closes.append(closes[-1] * 1.006)
    return candles_from_closes(closes)


def trend_strategy(**overrides) -> TrendContinuation:
    params = dict(
        breakout_bars=24, max_atr_pct=1.0, regime=SMALL_REGIME, funding=None
    )
    params.update(overrides)
    return TrendContinuation(**params)


def test_breakout_in_uptrend_emits_long_with_stop_and_tp():
    strategy = trend_strategy()
    idx, intent = first_signal(strategy, uptrend_after_chop())
    assert intent is not None
    assert intent.side == "long"
    assert intent.stop_price < intent.take_profit_price
    assert "broke" in intent.reason and "ER=" in intent.reason


def test_no_signal_during_warmup():
    strategy = trend_strategy()
    df = strategy.prepare(uptrend_after_chop())
    for i in range(1, strategy.warmup_bars):
        assert strategy.signal(df, i) is None


def test_chop_never_signals():
    closes = [100.0 + (0.5 if i % 2 else -0.5) for i in range(160)]
    idx, intent = first_signal(trend_strategy(), candles_from_closes(closes))
    assert intent is None


def test_expensive_funding_blocks_long():
    candles = uptrend_after_chop()
    rich = funding_series(len(candles), rate=0.002)  # longs paying 0.2%/8h
    idx, intent = first_signal(trend_strategy(funding=rich), candles)
    assert intent is None


def test_cheap_funding_does_not_block_long():
    candles = uptrend_after_chop()
    cheap = funding_series(len(candles), rate=0.0001)
    idx, intent = first_signal(trend_strategy(funding=cheap), candles)
    assert intent is not None and intent.side == "long"


def test_volatility_ceiling_blocks_entries():
    idx, intent = first_signal(trend_strategy(max_atr_pct=0.0), uptrend_after_chop())
    assert intent is None


def test_downtrend_emits_short():
    closes = [100.0 + (0.1 if i % 2 else -0.1) for i in range(90)]
    for _ in range(40):
        closes.append(closes[-1] * 0.994)
    idx, intent = first_signal(trend_strategy(), candles_from_closes(closes))
    assert intent is not None and intent.side == "short"
    assert intent.stop_price > intent.take_profit_price


# --- Funding mean reversion ----------------------------------------------------

def pumped_market(flat_bars: int = 100, pump_bars: int = 8) -> pd.DataFrame:
    closes = [100.0 + (0.1 if i % 2 else -0.1) for i in range(flat_bars)]
    for _ in range(pump_bars):
        closes.append(closes[-1] * 1.015)
    return candles_from_closes(closes)


def mr_strategy(candles_len: int, *, funding_rate: float = 0.0001,
                late_rate: float | None = 0.003, no_trend_filter: bool = True,
                **overrides) -> FundingMeanReversion:
    # funding turns extreme only near the end: a FRESH extreme. (A rate that
    # has been extreme for most of the window is, by midrank percentile, the
    # new normal — that's intended behavior.)
    funding = funding_series(candles_len, funding_rate, late_rate,
                             switch_at=candles_len - 8)
    regime = (
        RegimeParams(ema_fast=6, ema_slow=24, er_window=12, atr_window=6,
                     atr_pct_window=48, er_trend_min=0.99)  # trend filter ~off
        if no_trend_filter else SMALL_REGIME
    )
    params = dict(funding_pct_window=48, z_window=24, z_entry=1.5, regime=regime)
    params.update(overrides)
    return FundingMeanReversion(funding, **params)


def test_crowded_longs_faded_short():
    candles = pumped_market()
    strategy = mr_strategy(len(candles))
    idx, intent = first_signal(strategy, candles)
    assert intent is not None
    assert intent.side == "short"
    assert intent.stop_price > float(candles["close"].iloc[idx])   # stop above
    assert intent.take_profit_price < float(candles["close"].iloc[idx])  # target mean below
    assert "crowded longs" in intent.reason


def test_normal_funding_never_fades():
    candles = pumped_market()
    strategy = mr_strategy(len(candles), late_rate=None)  # funding stays normal
    idx, intent = first_signal(strategy, candles)
    assert intent is None


def test_strong_trend_blocks_fade():
    candles = pumped_market(pump_bars=20)  # long clean pump = trending regime
    strategy = mr_strategy(len(candles), no_trend_filter=False)
    idx, intent = first_signal(strategy, candles)
    assert intent is None


def test_funding_series_is_mandatory():
    with pytest.raises(ValueError, match="requires a funding-rate series"):
        FundingMeanReversion(pd.DataFrame())


# --- Crypto trend ATR-margin ----------------------------------------------------

def test_crypto_trend_atr_margin_emits_bull_flip():
    closes = [100.0 + (0.05 if i % 2 else -0.05) for i in range(270)]
    for _ in range(70):
        closes.append(closes[-1] * 1.006)
    candles = candles_from_closes(closes)
    strategy = CryptoTrendAtrMargin(fast_ema=6, slow_ema=18, atr_window=12)
    idx, intent = first_signal(strategy, candles)
    assert intent is not None
    assert intent.side == "long"
    assert intent.stop_price < float(candles["close"].iloc[idx])
    assert intent.take_profit_price is None
    assert "ATR-margin bull flip" in intent.reason


def test_crypto_trend_atr_margin_emits_bear_flip():
    closes = [100.0 + (0.05 if i % 2 else -0.05) for i in range(270)]
    for _ in range(70):
        closes.append(closes[-1] * 0.994)
    candles = candles_from_closes(closes)
    strategy = CryptoTrendAtrMargin(fast_ema=6, slow_ema=18, atr_window=12)
    idx, intent = first_signal(strategy, candles)
    assert intent is not None
    assert intent.side == "short"
    assert intent.stop_price > float(candles["close"].iloc[idx])
    assert "ATR-margin bear flip" in intent.reason


def test_crypto_trend_atr_margin_warmup_blocks_early_rows():
    strategy = CryptoTrendAtrMargin(fast_ema=6, slow_ema=18, atr_window=12)
    df = strategy.prepare(candles_from_closes([100.0 + i * 0.1 for i in range(280)]))
    for i in range(1, strategy.warmup_bars):
        assert strategy.signal(df, i) is None


def test_crypto_trend_atr_margin_exits_long_when_margin_goes_neutral():
    strategy = CryptoTrendAtrMargin()
    df = pd.DataFrame({
        "close": [100.0] * 251,
        "ema_fast": [101.0] * 251,
        "ema_slow": [100.0] * 251,
        "atr_margin": [2.0] * 251,
        "trend_spread": [0.1] * 251,
        "trend_bull": [False] * 251,
        "trend_bear": [False] * 251,
    })

    exit_sig = strategy.exit_signal(df, 250, "long", 100.0)

    assert exit_sig is not None
    assert exit_sig.reason == "strategy_neutral_long"
    assert exit_sig.exit_price == 100.0


def test_crypto_trend_atr_margin_exits_short_on_opposite_bull_regime():
    strategy = CryptoTrendAtrMargin()
    df = pd.DataFrame({
        "close": [100.0] * 251,
        "ema_fast": [101.0] * 251,
        "ema_slow": [100.0] * 251,
        "atr_margin": [2.0] * 251,
        "trend_spread": [2.0] * 251,
        "trend_bull": [True] * 251,
        "trend_bear": [False] * 251,
    })

    exit_sig = strategy.exit_signal(df, 250, "short", 100.0)

    assert exit_sig is not None
    assert exit_sig.reason == "strategy_reversal_short"
    assert exit_sig.exit_price == 100.0


# --- Registry -------------------------------------------------------------------

def test_registry_contains_core_candidates():
    assert {
        "crypto_trend_atr_margin_v1",
        "trend_continuation_v1",
        "funding_mean_reversion_v1",
    } <= set(STRATEGIES)
    assert get_strategy_class("crypto_trend_atr_margin_v1") is CryptoTrendAtrMargin
    assert get_strategy_class("trend_continuation_v1") is TrendContinuation


def test_registry_unknown_name():
    with pytest.raises(KeyError, match="registered"):
        get_strategy_class("secret_profit_machine")
