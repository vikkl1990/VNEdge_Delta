"""Metrics math on constructed results with known values."""

import math

import pandas as pd
import pytest

from vnedge.backtest.backtester import BacktestConfig, BacktestResult, Trade
from vnedge.backtest.metrics import compute_metrics

BASE = 1_750_000_000_000


def ts(i: int) -> pd.Timestamp:
    return pd.Timestamp(BASE + i * 3_600_000, unit="ms", tz="UTC")


def make_trade(net: float, reason: str = "stop") -> Trade:
    """A trade whose net pnl is exactly `net` (fees/funding folded in)."""
    return Trade(
        side="long", quantity=1.0, entry_ts=ts(0), entry_price=100.0,
        exit_ts=ts(1), exit_price=100.0 + net, exit_reason=reason,
        gross_pnl_usd=net, fees_usd=0.0, funding_usd=0.0, entry_reason="test",
    )


def make_result(curve_values: list[float], trades: list[Trade]) -> BacktestResult:
    curve = pd.Series(curve_values, index=[ts(i) for i in range(len(curve_values))])
    return BacktestResult(
        symbol="BTC/USDT:USDT", timeframe="1h", trades=tuple(trades),
        equity_curve=curve, skipped_by_sizing=0,
        final_equity_usd=curve_values[-1], config=BacktestConfig(),
    )


def test_basic_metrics():
    result = make_result(
        [500.0, 520.0, 510.0, 530.0],
        [make_trade(20.0, "take_profit"), make_trade(-10.0), make_trade(20.0, "take_profit")],
    )
    m = compute_metrics(result)
    assert m.num_trades == 3
    assert m.net_profit_usd == pytest.approx(30.0)
    assert m.return_pct == pytest.approx(6.0)
    assert m.win_rate_pct == pytest.approx(2 / 3 * 100)
    assert m.profit_factor == pytest.approx(4.0)  # 40 won / 10 lost
    assert m.exit_reasons == {"take_profit": 2, "stop": 1}


def test_max_drawdown():
    # peak 600 -> trough 480 = 20% drawdown
    result = make_result([500.0, 600.0, 480.0, 550.0], [])
    m = compute_metrics(result)
    assert m.max_drawdown_pct == pytest.approx(20.0)


def test_no_losses_gives_infinite_profit_factor():
    result = make_result([500.0, 520.0], [make_trade(20.0)])
    assert math.isinf(compute_metrics(result).profit_factor)


def test_no_trades_is_all_zeros():
    result = make_result([500.0, 500.0], [])
    m = compute_metrics(result)
    assert m.num_trades == 0
    assert m.win_rate_pct == 0.0
    assert m.profit_factor == 0.0
    assert m.sharpe == 0.0


def test_flat_curve_has_zero_sharpe():
    result = make_result([500.0] * 10, [])
    assert compute_metrics(result).sharpe == 0.0
