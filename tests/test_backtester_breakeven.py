"""Breakeven / profit-lock stop management in the backtester.

The 2026-08-02 ledger study: 63% of losers ran into profit then gave it back.
This exit rescues those give-backs. Pins the mechanism + default-off parity.
"""

import numpy as np
import pytest
import pandas as pd

from vnedge.backtest.backtester import (
    BacktestConfig,
    _OpenPosition,
    _check_intrabar_exit,
    run_backtest,
)
from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent


def _pos(side="long", entry=100.0, stop=98.0, tp=110.0):
    p = _OpenPosition(
        intent=SignalIntent(side=side, stop_price=stop, take_profit_price=tp,
                            take_profit_levels=(), reason="t"),
        quantity=1.0, entry_price=entry, entry_ts=pd.Timestamp("2026-01-01"),
        entry_bar=0, entry_fee_usd=0.0,
    )
    return p


def test_giveback_is_rescued_at_breakeven():
    p = _pos()  # long 100, stop 98
    # ran to 105 (+500 bps), then a later bar dips to 97 (below the 98 stop)
    p.track_excursion(high=105.0, low=100.0)
    # WITHOUT arming: dip to 97 hits the -98 stop → a LOSS
    assert _check_intrabar_exit(p, high=99.0, low=97.0) == ("stop", 98.0)
    # arm breakeven at +200 bps → stop ratchets to entry (100)
    p.arm_breakeven(arm_bps=200.0, lock_bps=0.0)
    assert p.stop_armed and p.managed_stop_price == 100.0
    # now the same dip exits at BREAKEVEN (100), not the -98 loss
    assert _check_intrabar_exit(p, high=99.0, low=97.0) == ("breakeven", 100.0)


def test_profit_lock_exits_in_the_green():
    p = _pos()
    p.track_excursion(high=106.0, low=100.0)  # +600 bps
    p.arm_breakeven(arm_bps=200.0, lock_bps=50.0)  # lock +50 bps
    assert p.managed_stop_price == pytest.approx(100.5)  # exits in the green, not scratch


def test_short_side_and_tighten_only():
    p = _pos(side="short", entry=100.0, stop=102.0)
    p.track_excursion(high=100.0, low=95.0)  # short favorable: price fell to 95 (+500bps)
    p.arm_breakeven(arm_bps=200.0, lock_bps=0.0)
    assert p.managed_stop_price == 100.0  # moved DOWN toward entry (tighter)
    # a weak later excursion must NOT loosen it back
    p.arm_breakeven(arm_bps=200.0, lock_bps=0.0)
    assert p.managed_stop_price == 100.0


class _AlwaysLong(BaseStrategy):
    strategy_id = "always_long_test"
    warmup_bars = 1

    def prepare(self, candles):
        return candles

    def signal(self, df, i):
        px = float(df.iloc[i]["close"])
        return SignalIntent(side="long", stop_price=px * 0.98,
                            take_profit_price=px * 1.10, take_profit_levels=(), reason="x")


def _frame():
    n = 60
    rng = np.random.default_rng(7)
    close = 100 + np.cumsum(rng.normal(0, 0.5, n))
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC"),
        "open": close, "high": close * 1.003, "low": close * 0.997, "close": close,
        "volume": 1.0,
    })


def test_default_off_is_identical_to_baseline():
    df = _frame()
    base = run_backtest(df.copy(), None, _AlwaysLong(), BacktestConfig(),
                        symbol="BTC/USDT:USDT", timeframe="1h")
    off = run_backtest(df.copy(), None, _AlwaysLong(),
                       BacktestConfig(breakeven_arm_bps=None),
                       symbol="BTC/USDT:USDT", timeframe="1h")
    assert [t.net_pnl_usd for t in base.trades] == [t.net_pnl_usd for t in off.trades]


class _LadderOnce(BaseStrategy):
    """Longs once with a 3-rung TP ladder + stop; then stays flat so we watch
    one position's full active-exit lifecycle."""
    strategy_id = "ladder_once_test"
    warmup_bars = 1
    def __init__(self): self._fired = False
    def prepare(self, candles):
        c = candles.copy(); c["atr"] = 2.0; return c
    def signal(self, df, i):
        if self._fired: return None
        self._fired = True
        e = float(df.iloc[i]["close"])
        return SignalIntent(side="long", stop_price=e*0.98,
                            take_profit_price=None,
                            take_profit_levels=(e*1.02, e*1.04, e*1.06), reason="ladder")
    def signal_reset(self): self._fired = False


def _rise_then_fall():
    # rises 100 -> 107 (through all 3 TP rungs) then falls back to 101
    highs = [100,101,102.5,104.5,106.5,107.5,106,104,102]
    lows  = [ 99,100,101.5,103.5,105.5,106.5,104,102,100.5]
    close = [ 99.5,100.5,102,104,106,107,105,103,101]
    n=len(highs)
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC"),
        "open":close, "high":highs, "low":lows, "close":close, "volume":1.0,
    })


def test_active_exit_takes_ladder_partials_and_trails():
    df=_rise_then_fall()
    cfg=BacktestConfig(use_active_exit=True, trail_atr_mult=2.0, max_holding_bars=50)
    r=run_backtest(df, None, _LadderOnce(), cfg, symbol="BTC/USDT:USDT", timeframe="1h")
    # one entry -> multiple exit legs (TP1/TP2 partials + a final)
    assert len(r.trades) >= 2, [t.exit_reason for t in r.trades]
    reasons=[t.exit_reason for t in r.trades]
    assert any(rr.startswith("tp1") for rr in reasons), reasons
    # partial quantities sum back to (approximately) one full position
    total=sum(t.quantity for t in r.trades)
    assert total > 0
    # the ladder booked real profit (rose through 3 rungs)
    assert sum(t.net_pnl_usd for t in r.trades) > 0


def test_active_exit_default_off_matches_legacy():
    df=_rise_then_fall()
    a=run_backtest(df.copy(), None, _LadderOnce(), BacktestConfig(), symbol="X", timeframe="1h")
    b=run_backtest(df.copy(), None, _LadderOnce(), BacktestConfig(use_active_exit=False), symbol="X", timeframe="1h")
    assert [t.net_pnl_usd for t in a.trades]==[t.net_pnl_usd for t in b.trades]
