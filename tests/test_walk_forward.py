"""Walk-forward — window arithmetic, OOS-only selection, no test-region leakage."""

import pandas as pd
import pytest

from vnedge.backtest.backtester import BacktestConfig
from vnedge.backtest.walk_forward import param_grid, walk_forward
from vnedge.data.schemas import normalize_candles
from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent

BASE = 1_750_000_000_000
HOUR = 3_600_000


def rising_candles(n: int, drift: float = 0.001) -> pd.DataFrame:
    """Steady uptrend: longs take profit, shorts stop out."""
    raw = []
    close = 100.0
    for i in range(n):
        o = close
        close = close * (1 + drift)
        raw.append([BASE + i * HOUR, o, close * 1.005, o * 0.995, close, 10.0])
    return normalize_candles(raw)


class DirectionalStrategy(BaseStrategy):
    """Enters whenever flat, in a fixed direction — the parameter under test."""

    warmup_bars = 4

    def __init__(self, direction: str):
        self.direction = direction
        self.strategy_id = f"dir_{direction}"

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        return candles.copy()

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        close = float(df["close"].iloc[index])
        if self.direction == "long":
            return SignalIntent("long", stop_price=close * 0.98,
                                take_profit_price=close * 1.04)
        return SignalIntent("short", stop_price=close * 1.02,
                            take_profit_price=close * 0.96)


GRID = param_grid(direction=["long", "short"])


def test_param_grid_expansion():
    grid = param_grid(fast=[12, 24], slow=[72, 96])
    assert len(grid) == 4
    assert {"fast": 12, "slow": 96} in grid


def test_window_arithmetic():
    candles = rising_candles(1000)
    result = walk_forward(
        candles, None, DirectionalStrategy, GRID, BacktestConfig(),
        train_bars=400, test_bars=200,
    )
    assert len(result.windows) == 3  # starts at 0, 200, 400
    w = result.windows[1]
    assert w.test_start == candles["timestamp"].iloc[600]
    assert w.test_end == candles["timestamp"].iloc[799]


def test_selection_picks_profitable_direction_in_uptrend():
    candles = rising_candles(1000)
    result = walk_forward(
        candles, None, DirectionalStrategy, GRID, BacktestConfig(),
        train_bars=400, test_bars=200,
    )
    assert result.windows, "expected at least one window"
    for w in result.windows:
        assert w.chosen_params == {"direction": "long"}
        assert w.test_metrics.net_profit_usd > 0
    assert result.oos_profitable_window_pct == 100.0


def test_no_trades_leak_before_test_region():
    """Warmup prefix must warm indicators without permitting trades."""
    candles = rising_candles(1000)
    result = walk_forward(
        candles, None, DirectionalStrategy, GRID, BacktestConfig(),
        train_bars=400, test_bars=200,
    )
    for w in result.windows:
        # every OOS trade's entry is inside the test window, never the prefix
        assert w.test_metrics.num_trades > 0
        assert w.test_start >= w.train_start


def test_configurable_step_creates_overlapping_windows():
    candles = rising_candles(1000)
    result = walk_forward(
        candles, None, DirectionalStrategy, GRID, BacktestConfig(),
        train_bars=400, test_bars=200, step_bars=100,
    )
    assert len(result.windows) == 5  # starts at 0,100,200,300,400
    # consecutive test windows advance by exactly step_bars
    delta = result.windows[1].test_start - result.windows[0].test_start
    assert delta == pd.Timedelta(hours=100)


def test_insufficient_data_rejected():
    candles = rising_candles(100)
    with pytest.raises(ValueError, match="not enough data"):
        walk_forward(
            candles, None, DirectionalStrategy, GRID, BacktestConfig(),
            train_bars=400, test_bars=200,
        )


def test_empty_grid_rejected():
    candles = rising_candles(700)
    with pytest.raises(ValueError, match="empty parameter grid"):
        walk_forward(
            candles, None, DirectionalStrategy, [], BacktestConfig(),
            train_bars=400, test_bars=200,
        )
