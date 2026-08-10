"""VNEDGE Algo ML Pro source-backed scanner tests."""

import pandas as pd

from vnedge.data.schemas import normalize_candles
from vnedge.research.execution_edge_labeler import strategy_signal_events
from vnedge.strategy.strategy_registry import STRATEGIES, get_strategy_class
from vnedge.strategy.vnedge_algo_ml_pro import (
    VNEDGE_ALGO_ML_PRO_ID,
    VNEDGEAlgoMLProParams,
    VNEDGEAlgoMLProScanner,
    add_vnedge_algo_ml_pro_columns,
)


BASE = 1_750_000_000_000
FIVE_MIN = 300_000


def params() -> VNEDGEAlgoMLProParams:
    return VNEDGEAlgoMLProParams(
        auto_tune=False,
        atr_length=4,
        base_multiplier=1.0,
        profile_lookback=30,
        min_multiplier=0.7,
        max_multiplier=2.0,
        cushion_atr=0.0,
        cooldown_bars=0,
        use_mtf=False,
        htf_ema_fast=2,
        htf_ema_slow=3,
        use_ml_filter=False,
        use_momentum=False,
        use_volume_filter=False,
        bbp_length=4,
        bbp_norm_lookback=12,
        min_expected_net_edge_bps=-100.0,
        min_fill_probability=0.0,
        min_stop_bps=4.0,
    )


def make_candles(rows):
    return normalize_candles(
        [
            [BASE + i * FIVE_MIN, open_, high, low, close, volume]
            for i, (open_, high, low, close, volume) in enumerate(rows)
        ]
    )


def flip_rows(direction: str, n: int = 140, start: float = 100.0):
    rows = []
    prev = start
    for i in range(n):
        if direction == "up":
            drift = -0.12 if i < 80 else 0.28
            shock = 1.4 if i == 84 else 0.0
        else:
            drift = 0.12 if i < 80 else -0.28
            shock = -1.4 if i == 84 else 0.0
        close = prev + drift + shock
        high = max(prev, close) + 0.22
        low = min(prev, close) - 0.22
        volume = 100.0 + i
        if i >= 80:
            volume *= 2.0
        rows.append((prev, high, low, close, volume))
        prev = close
    return rows


def test_vnedge_algo_ml_pro_fires_long_flip_with_route_metadata():
    candles = make_candles(flip_rows("up"))
    strategy = VNEDGEAlgoMLProScanner(params=params())

    events = strategy_signal_events(candles, strategy)

    assert events
    assert any(event.side == "long" for event in events)
    event = next(event for event in events if event.side == "long")
    assert event.stop_price < float(candles["close"].iloc[-2])
    assert event.take_profit_price is not None
    assert event.expected_edge_bps is not None
    assert event.fill_probability is not None
    assert "paperNotional=2500" in str(event.metadata["reason"])


def test_vnedge_algo_ml_pro_fires_short_flip():
    candles = make_candles(flip_rows("down"))
    strategy = VNEDGEAlgoMLProScanner(params=params())

    events = strategy_signal_events(candles, strategy)

    assert events
    assert any(event.side == "short" for event in events)
    event = next(event for event in events if event.side == "short")
    assert event.stop_price > float(candles["close"].iloc[-2])


def test_vnedge_algo_ml_pro_columns_are_causal_when_future_changes():
    candles = make_candles(flip_rows("up", n=180))
    mutated = candles.copy()
    mutated.loc[121:, ["open", "high", "low", "close"]] *= 1.35
    mutated.loc[121:, "volume"] *= 5.0
    p = params()

    a = add_vnedge_algo_ml_pro_columns(candles, p)
    b = add_vnedge_algo_ml_pro_columns(mutated, p)

    cols = [
        "atr_base",
        "st_band",
        "trend_dir",
        "raw_flip",
        "rsi",
        "adx",
        "bbp",
        "bbp_strength",
        "ml_score",
        "expected_net_edge_bps_long",
        "expected_net_edge_bps_short",
    ]
    pd.testing.assert_frame_equal(a.loc[:120, cols], b.loc[:120, cols])


def test_vnedge_algo_ml_pro_is_registered():
    assert get_strategy_class(VNEDGE_ALGO_ML_PRO_ID) is VNEDGEAlgoMLProScanner
    assert VNEDGE_ALGO_ML_PRO_ID in STRATEGIES
