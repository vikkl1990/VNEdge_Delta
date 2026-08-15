"""Correctness checks for the StealthTrail default parity audit."""

import numpy as np
import pandas as pd
import pytest

from vnedge.research.stealthtrail_supertrend_parity import (
    TrailSignal,
    compute_default_state,
    simulate_causal_30m,
    simulate_indicator_native,
)


def _state(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    index = pd.date_range("2026-01-01", periods=len(rows), freq="5min", tz="UTC")
    frame = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=index)
    frame["volume"] = 100.0
    frame["band"] = 99.0
    return frame


def _signal(frame: pd.DataFrame) -> TrailSignal:
    return TrailSignal(
        index=0,
        timestamp=frame.index[0],
        side="long",
        signal_close=100.0,
        atr=1.0,
        band=99.0,
        rsi=60.0,
        effective_multiplier=2.0,
    )


def test_native_close_stop_and_causal_wick_stop_diverge():
    state = _state([(99.8, 100.1, 99.7, 100.0), (100.0, 100.5, 98.5, 100.2)])
    signal = _signal(state)

    native = simulate_indicator_native(state, [signal], symbol="ETHUSD", timeframe="5m")
    causal = simulate_causal_30m(state, [signal], symbol="ETHUSD", timeframe="5m", cost_bps=14.8)

    assert native == []  # close stayed above the visual stop
    assert causal[0].exit_reason == "stop"
    assert causal[0].gross_bps == pytest.approx(-100.0)
    assert causal[0].net_bps == pytest.approx(-114.8)


def test_causal_path_enters_next_open_and_caps_at_30_minutes():
    state = _state([(99.8, 100.1, 99.7, 100.0)] + [(101.0, 101.2, 100.8, 101.1)] * 6)
    state["band"] = 98.0
    signal = _signal(state)
    signal = TrailSignal(**{**signal.__dict__, "band": 98.0})

    outcome = simulate_causal_30m(state, [signal], symbol="BTCUSD", timeframe="5m", cost_bps=14.8)[
        0
    ]

    assert outcome.entry_price == 101.0
    assert outcome.entry_ts == "2026-01-01T00:05:00+00:00"
    assert outcome.exit_reason == "time_stop"
    assert outcome.minutes_held == 30.0


def test_adaptive_signal_state_is_prefix_stable():
    rng = np.random.default_rng(7)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.006, 600)))
    open_ = np.r_[close[0], close[:-1]]
    spread = np.maximum(0.05, np.abs(rng.normal(0.25, 0.08, len(close))))
    frame = pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) + spread,
            "low": np.minimum(open_, close) - spread,
            "close": close,
            "volume": 100.0,
        },
        index=pd.date_range("2026-01-01", periods=len(close), freq="5min", tz="UTC"),
    )

    prefix = compute_default_state(frame.iloc[:500])
    full = compute_default_state(frame)

    assert prefix["trend"].tolist() == full.iloc[:500]["trend"].tolist()
    assert prefix["signal"].tolist() == full.iloc[:500]["signal"].tolist()
