"""Causal parity checks for the manually ported STRAT Trap defaults."""

import numpy as np
import pandas as pd
import pytest

from vnedge.research.strat_trap_vwap_parity import (
    TrapSignal,
    detect_default_traps,
    simulate_causal_30m,
    simulate_tv_native,
)


def _frame(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    index = pd.date_range("2026-01-01", periods=len(rows), freq="5min", tz="UTC")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=index).assign(
        volume=100.0
    )


def _signal(frame: pd.DataFrame, *, atr: float = 1.0) -> TrapSignal:
    return TrapSignal(
        index=0,
        timestamp=frame.index[0],
        side="long",
        signal_close=100.0,
        sweep_extreme=99.75,
        atr=atr,
        pre_entry_reversal_bps=25.0,
    )


def test_tv_target_priority_and_causal_stop_first_diverge_on_ambiguous_bar():
    candles = _frame([(99.8, 100.1, 99.7, 100.0), (100.0, 102.0, 99.0, 101.0)])
    signal = _signal(candles)

    tv = simulate_tv_native(candles, [signal], symbol="ETHUSD", timeframe="5m")
    causal = simulate_causal_30m(candles, [signal], symbol="ETHUSD", timeframe="5m", cost_bps=14.8)

    assert tv[0].exit_reason == "tp3"
    assert tv[0].gross_bps == pytest.approx(150.0)
    assert causal[0].exit_reason == "stop"
    assert causal[0].gross_bps == pytest.approx(-50.0)
    assert causal[0].net_bps == pytest.approx(-64.8)


def test_causal_30m_exits_at_close_of_first_30m_entry_bar():
    candles = _frame([(99.8, 100.1, 99.7, 100.0), (100.0, 100.2, 99.8, 100.1)])
    candles.index = pd.date_range("2026-01-01", periods=2, freq="30min", tz="UTC")
    signal = _signal(candles, atr=2.0)

    outcome = simulate_causal_30m(
        candles, [signal], symbol="ETHUSD", timeframe="30m", cost_bps=14.8
    )[0]

    assert outcome.exit_reason == "time_stop"
    assert outcome.minutes_held == 30.0
    assert outcome.entry_ts == "2026-01-01T00:30:00+00:00"
    assert outcome.exit_ts == "2026-01-01T01:00:00+00:00"


def test_default_trap_detection_is_prefix_stable_and_closed_candle_causal():
    rng = np.random.default_rng(42)
    returns = rng.normal(0.0, 0.004, 500)
    close = 100.0 * np.exp(np.cumsum(returns))
    open_ = np.r_[close[0], close[:-1]]
    spread = np.maximum(0.1, np.abs(rng.normal(0.4, 0.15, len(close))))
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

    prefix = detect_default_traps(frame.iloc[:400])
    full_past = [row for row in detect_default_traps(frame) if row.index < 400]

    assert [(row.index, row.side) for row in prefix] == [(row.index, row.side) for row in full_past]
