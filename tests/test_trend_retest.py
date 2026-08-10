"""Trend retest v1 + volume profile + liquidity pools — causality first."""

import numpy as np
import pandas as pd
import pytest

from vnedge.data.schemas import normalize_candles
from vnedge.strategy.liquidity_pools import detect_pools, update_mitigation
from vnedge.strategy.trend_retest import TrendRetest
from vnedge.strategy.volume_profile import profile_levels, range_distributed_profile

BASE = 1_750_000_000_000
HOUR = 3_600_000


def _candles(rows):
    return normalize_candles([[BASE + i * HOUR, *r] for i, r in enumerate(rows)])


# --- volume profile ---------------------------------------------------------

def test_profile_distributes_volume_by_overlap():
    # one bar spanning exactly two of four bins splits volume 50/50
    bars = pd.DataFrame({
        "low": [100.0, 100.0], "high": [104.0, 102.0],
        "volume": [0.0, 8.0],
    })
    edges, vols = range_distributed_profile(bars, bins=4)
    assert len(vols) == 4
    # bar 2 spans 100-102 = bins 1+2 equally (bar 1 sets range 100-104, no volume)
    assert vols[0] == pytest.approx(4.0)
    assert vols[1] == pytest.approx(4.0)
    assert vols[2] == vols[3] == pytest.approx(0.0)


def test_profile_levels_poc_and_value_area():
    edges = np.linspace(100, 110, 11)
    vols = np.array([1, 1, 2, 8, 20, 9, 3, 2, 1, 1], dtype=float)
    lv = profile_levels(edges, vols)
    assert lv is not None
    assert lv.poc == pytest.approx(104.5)          # center of the 20-volume bin
    assert lv.val <= lv.poc <= lv.vah
    assert lv.vah - lv.val < 110 - 100              # VA is a subset of the range


def test_profile_zero_volume_uses_range_proxy():
    bars = pd.DataFrame({
        "low": [100.0, 101.0], "high": [101.0, 102.0], "volume": [0.0, 0.0],
    })
    edges, vols = range_distributed_profile(bars, bins=4)
    assert vols.sum() > 0  # proxy kicked in


# --- liquidity pools ---------------------------------------------------------

def _pool_frame():
    # two equal highs at 110 (bars 10 and 30), noise elsewhere
    n = 50
    high = np.full(n, 105.0) + np.random.default_rng(7).normal(0, 0.2, n)
    high[10] = high[30] = 110.0
    low = high - 2.0
    close = (high + low) / 2
    df = pd.DataFrame({
        "high": high, "low": low, "close": close,
        "open": close, "volume": np.full(n, 10.0),
        "atr": np.full(n, 1.0),
    })
    return df


def test_pools_cluster_equal_highs_and_score():
    df = _pool_frame()
    pools = detect_pools(df, pivot_length=5, tolerance_atr=0.25)
    highs = [p for p in pools if p.side == "high" and abs(p.price - 110.0) < 0.5]
    assert len(highs) == 1                       # clustered, not duplicated
    pool = highs[0]
    assert len(pool.touch_indices) == 2
    assert pool.confirmed_at == 35               # last pivot 30 + right lookback 5
    s = pool.strength(40)
    assert 0 < s <= 100
    assert pool.strength(40) > pool.strength(400)  # recency decays


def test_pool_mitigation_requires_pierce_and_close_back():
    df = _pool_frame()
    # bar 40 wicks through 110 but closes back below -> MITIGATED
    df.loc[40, "high"] = 111.0
    df.loc[40, "close"] = 109.0
    pools = detect_pools(df, pivot_length=5)
    update_mitigation(pools, df)
    pool = next(p for p in pools if p.side == "high" and abs(p.price - 110.0) < 0.5)
    assert pool.mitigated_at == 40


# --- trend retest strategy ----------------------------------------------------

def _trending_with_pullback(n=420, pullback_at=400, reclaim_at=402):
    """Steady uptrend, one sharp pullback into the band stack, strong reclaim."""
    rows = []
    price = 100.0
    for i in range(n):
        if i == pullback_at:
            rows.append([price, price + 0.2, price - 6.0, price - 5.0, 30.0])
            price -= 5.0
        elif i == pullback_at + 1:
            rows.append([price, price + 1.0, price - 0.5, price + 0.8, 35.0])
            price += 0.8
        elif i == reclaim_at:
            rows.append([price, price + 5.4, price - 0.2, price + 5.2, 60.0])
            price += 5.2
        else:
            rows.append([price, price + 0.6, price - 0.3, price + 0.35, 10.0])
            price += 0.35
    return _candles(rows)


def test_retest_fires_on_quality_reclaim():
    candles = _trending_with_pullback()
    strat = TrendRetest(min_score=60.0)
    df = strat.prepare(candles)
    hits = [(i, strat.signal(df, i)) for i in range(len(df))]
    fired = [(i, s) for i, s in hits if s is not None]
    assert fired, "expected at least one retest signal"
    idx, sig = fired[-1]
    assert sig.side == "long"
    assert sig.stop_price < float(df["close"].iloc[idx])
    assert sig.take_profit_price > float(df["close"].iloc[idx])
    assert "retest reclaim score" in sig.reason


def test_min_score_gates_entries():
    candles = _trending_with_pullback()
    loose = TrendRetest(min_score=60.0)
    strict = TrendRetest(min_score=99.0)
    df_l = loose.prepare(candles)
    df_s = strict.prepare(candles)
    n_loose = sum(loose.signal(df_l, i) is not None for i in range(len(df_l)))
    n_strict = sum(strict.signal(df_s, i) is not None for i in range(len(df_s)))
    assert n_loose > n_strict
    assert n_strict == 0


def test_features_are_causal_when_future_changes():
    candles = _trending_with_pullback()
    strat = TrendRetest(min_score=60.0)
    df_full = strat.prepare(candles)

    cut = 405
    strat2 = TrendRetest(min_score=60.0)
    df_cut = strat2.prepare(candles.iloc[:cut].reset_index(drop=True))
    for i in range(strat.warmup_bars, cut):
        a = strat.signal(df_full, i)
        b = strat2.signal(df_cut, i)
        assert (a is None) == (b is None), f"signal at {i} depends on the future"
        if a is not None:
            assert a.side == b.side
            assert a.stop_price == pytest.approx(b.stop_price)


def test_no_signals_in_chop():
    rng = np.random.default_rng(3)
    rows = []
    price = 100.0
    for _ in range(400):
        delta = rng.normal(0, 0.4)
        rows.append([price, price + abs(delta) + 0.2, price - abs(delta) - 0.2,
                     price + delta, 10.0])
        price += delta
    candles = _candles(rows)
    strat = TrendRetest(min_score=60.0)
    df = strat.prepare(candles)
    fired = sum(strat.signal(df, i) is not None for i in range(len(df)))
    assert fired == 0


def test_wick_stop_respects_floor():
    candles = _trending_with_pullback()
    strat = TrendRetest(min_score=60.0, stop_mode="wick", stop_atr_mult=1.5)
    df = strat.prepare(candles)
    for i in range(len(df)):
        sig = strat.signal(df, i)
        if sig is not None:
            dist = abs(float(df["close"].iloc[i]) - sig.stop_price)
            assert dist >= 0.5 * 1.5 * float(df["atr13"].iloc[i]) - 1e-9


def test_registry_and_lane_dispatch():
    from vnedge.runtime.multi_lane import _build_single_strategy
    from vnedge.strategy.strategy_registry import STRATEGIES

    assert "trend_retest_v1" in STRATEGIES
    strat = _build_single_strategy(
        "trend_retest_v1", {},
        pd.DataFrame(columns=["timestamp", "funding_rate"]), None,
    )
    assert isinstance(strat, TrendRetest)
