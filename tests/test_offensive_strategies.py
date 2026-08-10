"""Offensive lanes — each fires on its constructed setup, blocks otherwise."""

import pandas as pd
import pytest

from vnedge.data.schemas import normalize_candles, normalize_funding
from vnedge.strategy.funding_squeeze_continuation import FundingSqueezeContinuation
from vnedge.strategy.panic_reversal import PanicReversal
from vnedge.strategy.regime import RegimeParams
from vnedge.strategy.strategy_registry import STRATEGIES
from vnedge.strategy.vol_expansion_breakout import VolatilityExpansionBreakout

BASE = 1_750_000_000_000
HOUR = 3_600_000

SMALL = RegimeParams(ema_fast=6, ema_slow=24, er_window=12,
                     atr_window=6, atr_pct_window=48)


def candles_from(closes, volumes=None):
    raw, prev = [], closes[0]
    for i, c in enumerate(closes):
        vol = volumes[i] if volumes else 10.0
        raw.append([BASE + i * HOUR, prev, max(prev, c) * 1.002,
                    min(prev, c) * 0.998, c, vol])
        prev = c
    return normalize_candles(raw)


def funding_series(n_hours, rate=0.0001, late_rate=None, switch_at=0):
    rows = []
    for i in range(0, n_hours, 8):
        r = late_rate if (late_rate is not None and i >= switch_at) else rate
        rows.append({"timestamp": BASE + i * HOUR, "fundingRate": r})
    return normalize_funding(rows)


def first_signal(strategy, candles):
    df = strategy.prepare(candles)
    for i in range(strategy.warmup_bars, len(df)):
        intent = strategy.signal(df, i)
        if intent is not None:
            return i, intent
    return None, None


# --- Lane A: volatility expansion breakout -------------------------------------

def breakout_market():
    closes = [100.0 + (0.1 if i % 2 else -0.1) for i in range(90)]
    vols = [10.0] * 90
    for i in range(40):
        closes.append(closes[-1] * 1.006)
        vols.append(30.0)  # volume expands with the move
    return candles_from(closes, vols)


def test_breakout_fires_with_volume_and_expansion():
    strategy = VolatilityExpansionBreakout(
        regime=SMALL, min_atr_pct=0.0, max_atr_pct=1.0, min_volume_z=0.5,
        breakout_bars=24,
    )
    idx, intent = first_signal(strategy, breakout_market())
    assert intent is not None and intent.side == "long"
    # 2.5R asymmetry: reward distance = 2.5x risk distance
    df = strategy.prepare(breakout_market())
    close = float(df["close"].iloc[idx])
    assert (intent.take_profit_price - close) == pytest.approx(
        2.5 * (close - intent.stop_price), rel=1e-6)


def test_breakout_blocked_without_volume():
    closes = [100.0 + (0.1 if i % 2 else -0.1) for i in range(90)]
    for i in range(40):
        closes.append(closes[-1] * 1.006)
    flat_vol = candles_from(closes)  # volume never expands
    strategy = VolatilityExpansionBreakout(
        regime=SMALL, min_atr_pct=0.0, max_atr_pct=1.0, min_volume_z=0.5,
        breakout_bars=24,
    )
    idx, intent = first_signal(strategy, flat_vol)
    assert intent is None


def test_breakout_blocked_by_volatility_ceiling():
    strategy = VolatilityExpansionBreakout(
        regime=SMALL, min_atr_pct=0.0, max_atr_pct=0.05, min_volume_z=0.5,
        breakout_bars=24,
    )
    idx, intent = first_signal(strategy, breakout_market())
    assert intent is None


# --- Lane B: panic reversal -------------------------------------------------------

def panic_market(stabilize: bool = True):
    closes = [100.0 + (0.1 if i % 2 else -0.1) for i in range(100)]
    for i in range(8):
        closes.append(closes[-1] * 0.97)  # -3% panic candles
    if stabilize:
        closes.append(closes[-1] * 1.012)  # first green bar (low still equals panic low)
        closes.append(closes[-1] * 1.005)  # second green bar: genuinely higher low
    else:
        closes.append(closes[-1] * 0.985)  # still falling
        closes.append(closes[-1] * 0.99)
    return candles_from(closes)


def panic_strategy(candles_len, funding_flushed=True):
    funding = funding_series(
        candles_len, rate=0.0001,
        late_rate=-0.0006 if funding_flushed else 0.0008,
        switch_at=candles_len - 24,
    )
    return PanicReversal(
        funding, regime=SMALL, drop_z_window=48, funding_pct_window=48,
        min_atr_pct=0.5, target_window=48, min_rr=1.5, drop_z_entry=-2.0,
    )


def test_panic_reversal_fires_after_stabilization():
    candles = panic_market(stabilize=True)
    idx, intent = first_signal(panic_strategy(len(candles)), candles)
    assert intent is not None and intent.side == "long"
    close = float(candles["close"].iloc[idx])
    assert intent.stop_price < float(candles["low"].iloc[idx])  # below panic low
    rr = (intent.take_profit_price - close) / (close - intent.stop_price)
    assert rr >= 1.5  # asymmetry enforced, not hoped for


def test_panic_reversal_never_catches_falling_knife():
    candles = panic_market(stabilize=False)
    idx, intent = first_signal(panic_strategy(len(candles)), candles)
    assert intent is None


def test_panic_reversal_blocked_when_longs_still_crowded():
    candles = panic_market(stabilize=True)
    idx, intent = first_signal(
        panic_strategy(len(candles), funding_flushed=False), candles)
    assert intent is None


# --- Lane C: funding squeeze continuation ----------------------------------------

def squeeze_market():
    closes = [100.0 + (0.1 if i % 2 else -0.1) for i in range(90)]
    vols = [10.0] * 90
    for i in range(40):
        closes.append(closes[-1] * 1.006)
        vols.append(25.0)
    return candles_from(closes, vols)


def test_squeeze_joins_crowding_in_trend():
    candles = squeeze_market()
    funding = funding_series(len(candles), rate=0.0001, late_rate=0.003,
                             switch_at=len(candles) - 10)
    strategy = FundingSqueezeContinuation(
        funding, regime=SMALL, funding_pct_window=48, extreme_pct=0.85,
    )
    idx, intent = first_signal(strategy, candles)
    assert intent is not None
    assert intent.side == "long"  # SAME feature as MR, OPPOSITE action in trend
    assert "continuation, not fade" in intent.reason


def test_squeeze_blocked_without_trend():
    # extreme funding in CHOP: that's MR territory, not squeeze territory
    closes = [100.0 + (0.5 if i % 2 else -0.5) for i in range(140)]
    candles = candles_from(closes)
    funding = funding_series(len(candles), rate=0.0001, late_rate=0.003,
                             switch_at=len(candles) - 10)
    strategy = FundingSqueezeContinuation(
        funding, regime=SMALL, funding_pct_window=48, extreme_pct=0.85,
    )
    idx, intent = first_signal(strategy, candles)
    assert intent is None


def test_squeeze_requires_funding_series():
    with pytest.raises(ValueError, match="squeeze IS the hypothesis"):
        FundingSqueezeContinuation(pd.DataFrame())


def test_registry_has_research_lanes():
    assert set(STRATEGIES) == {
        "crypto_trend_atr_margin_v1",
        "trend_continuation_v1", "funding_mean_reversion_v1",
        "volatility_expansion_breakout_v1", "panic_reversal_v1",
        "funding_squeeze_continuation_v1", "datrend_nomada_scalper_v1",
        "fvg_liquidity_breakout_v1", "alpha_stack_confluence_v1",
        "quant_signal_pack_v1", "alpha_distillation_pack_v1",
        "trend_retest_v1", "sats_5m_scalper_v1", "smc_playbook_scalper_v1",
        "stealth_trail_bbp_v1", "human_trade_fingerprint_v1",
        "luxy_ut_bot_forecast_v1", "momentum_cascade_lyro_v1",
        "luxara_live_plan_qtm_v1", "luxara_break_bounce_v27_v1",
        "vnedge_algo_ml_pro_v1", "context_scalper_v2",
        "quantified_fee_wall_sniper_v1",
    }
