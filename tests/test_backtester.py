"""Backtest engine — fill timing, exits, funding, fees, sizing integration."""

import pandas as pd
import pytest

from vnedge.backtest.backtester import BacktestConfig, run_backtest
from vnedge.runtime.daily_factory import DailySignalFactoryConfig
from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent, StrategyExitIntent
from vnedge.data.schemas import normalize_candles, normalize_funding

BASE = 1_750_000_000_000
HOUR = 3_600_000
SLIP = 2 / 10_000  # default slippage bps
FEE = 5 / 10_000   # default taker bps


def make_candles(bars: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    """bars: list of (open, high, low, close); hourly, volume 10."""
    raw = [
        [BASE + i * HOUR, o, h, low, c, 10.0] for i, (o, h, low, c) in enumerate(bars)
    ]
    return normalize_candles(raw)


def make_minute_candles(
    start: str, bars: list[tuple[float, float, float, float]], *, step_minutes: int = 5
) -> pd.DataFrame:
    base = pd.Timestamp(start, tz="UTC")
    raw = [
        [
            int((base + pd.Timedelta(minutes=i * step_minutes)).timestamp() * 1000),
            o,
            h,
            low,
            c,
            10.0,
        ]
        for i, (o, h, low, c) in enumerate(bars)
    ]
    return normalize_candles(raw)


FLAT = (100.0, 101.0, 99.0, 100.0)


class StubStrategy(BaseStrategy):
    """Emits a fixed intent at a chosen bar index."""

    strategy_id = "stub"

    def __init__(self, at_index: int, intent: SignalIntent):
        self.at_index = at_index
        self.intent = intent
        self.signal_calls: list[int] = []

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        return candles.copy()

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        self.signal_calls.append(index)
        return self.intent if index == self.at_index else None


class StrategyExitAfterEntry(StubStrategy):
    def __init__(
        self,
        at_index: int,
        intent: SignalIntent,
        *,
        exit_at: int,
        reason: str = "strategy_neutral",
    ) -> None:
        super().__init__(at_index, intent)
        self.exit_at = exit_at
        self.reason = reason

    def exit_signal(
        self,
        df: pd.DataFrame,
        index: int,
        side: str,
        entry_price: float,
    ) -> StrategyExitIntent | None:
        if index == self.exit_at:
            return StrategyExitIntent(self.reason, exit_price=float(df["close"].iloc[index]))
        return None


LONG_INTENT = SignalIntent(side="long", stop_price=97.0, take_profit_price=106.0)


def test_intent_fills_at_next_bar_open():
    candles = make_candles([FLAT] * 10)
    strategy = StubStrategy(at_index=4, intent=LONG_INTENT)
    result = run_backtest(candles, None, strategy, BacktestConfig())
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.entry_ts == candles["timestamp"].iloc[5]  # next bar, not signal bar
    assert trade.entry_price == pytest.approx(100.0 * (1 + SLIP))


def test_signal_on_last_bar_never_fills():
    candles = make_candles([FLAT] * 10)
    strategy = StubStrategy(at_index=9, intent=LONG_INTENT)
    result = run_backtest(candles, None, strategy, BacktestConfig())
    assert result.trades == ()
    assert 9 not in strategy.signal_calls  # engine never asks on the final bar


def test_stop_loss_exits_with_slippage():
    bars = [FLAT] * 6 + [(100.0, 100.5, 96.0, 98.0)] + [FLAT] * 3
    candles = make_candles(bars)
    strategy = StubStrategy(at_index=4, intent=LONG_INTENT)
    result = run_backtest(candles, None, strategy, BacktestConfig())
    trade = result.trades[0]
    assert trade.exit_reason == "stop"
    assert trade.exit_ts == candles["timestamp"].iloc[6]
    assert trade.exit_price == pytest.approx(97.0 * (1 - SLIP))
    assert trade.net_pnl_usd < 0


def test_take_profit_exit():
    bars = [FLAT] * 6 + [(100.0, 107.0, 99.5, 106.5)] + [FLAT] * 3
    candles = make_candles(bars)
    strategy = StubStrategy(at_index=4, intent=LONG_INTENT)
    result = run_backtest(candles, None, strategy, BacktestConfig())
    trade = result.trades[0]
    assert trade.exit_reason == "take_profit"
    assert trade.exit_price == pytest.approx(106.0 * (1 - SLIP))
    assert trade.net_pnl_usd > 0


def test_stop_wins_when_both_hit_in_one_bar():
    bars = [FLAT] * 6 + [(100.0, 107.0, 96.0, 100.0)] + [FLAT] * 3
    candles = make_candles(bars)
    strategy = StubStrategy(at_index=4, intent=LONG_INTENT)
    result = run_backtest(candles, None, strategy, BacktestConfig())
    assert result.trades[0].exit_reason == "stop"  # conservative


def test_max_holding_exit():
    candles = make_candles([FLAT] * 12)
    strategy = StubStrategy(at_index=4, intent=LONG_INTENT)
    config = BacktestConfig(max_holding_bars=3)
    result = run_backtest(candles, None, strategy, config)
    trade = result.trades[0]
    assert trade.exit_reason == "max_holding"
    assert trade.exit_ts == candles["timestamp"].iloc[8]  # entry bar 5 + 3


def test_strategy_managed_exit_closes_before_max_holding():
    bars = [FLAT] * 6 + [(100.0, 101.0, 99.0, 101.0)] + [FLAT] * 4
    candles = make_candles(bars)
    strategy = StrategyExitAfterEntry(4, LONG_INTENT, exit_at=6)
    result = run_backtest(candles, None, strategy, BacktestConfig(max_holding_bars=20))
    trade = result.trades[0]
    assert trade.exit_reason == "strategy_neutral"
    assert trade.exit_ts == candles["timestamp"].iloc[6]
    assert trade.exit_price == pytest.approx(101.0 * (1 - SLIP))


def test_stop_still_wins_before_strategy_managed_exit():
    bars = [FLAT] * 6 + [(100.0, 101.0, 96.0, 101.0)] + [FLAT] * 4
    candles = make_candles(bars)
    strategy = StrategyExitAfterEntry(4, LONG_INTENT, exit_at=6)
    result = run_backtest(candles, None, strategy, BacktestConfig(max_holding_bars=20))
    assert result.trades[0].exit_reason == "stop"


def test_long_pays_positive_funding():
    candles = make_candles([FLAT] * 12)
    funding = normalize_funding(
        [{"timestamp": BASE + 7 * HOUR, "fundingRate": 0.001}]
    )
    strategy = StubStrategy(at_index=4, intent=LONG_INTENT)
    result = run_backtest(candles, funding, strategy, BacktestConfig())
    trade = result.trades[0]
    expected = -0.001 * trade.quantity * 100.0  # longs PAY positive rates
    assert trade.funding_usd == pytest.approx(expected)


def test_short_receives_positive_funding():
    candles = make_candles([FLAT] * 12)
    funding = normalize_funding(
        [{"timestamp": BASE + 7 * HOUR, "fundingRate": 0.001}]
    )
    intent = SignalIntent(side="short", stop_price=103.0)
    strategy = StubStrategy(at_index=4, intent=intent)
    result = run_backtest(candles, funding, strategy, BacktestConfig())
    assert result.trades[0].funding_usd > 0


def test_funding_while_flat_is_ignored():
    candles = make_candles([FLAT] * 12)
    funding = normalize_funding(
        [{"timestamp": BASE + 2 * HOUR, "fundingRate": 0.001}]  # before entry
    )
    strategy = StubStrategy(at_index=4, intent=LONG_INTENT)
    result = run_backtest(candles, funding, strategy, BacktestConfig())
    assert result.trades[0].funding_usd == 0.0


def test_equity_accounting_is_exact():
    bars = [FLAT] * 6 + [(100.0, 107.0, 99.5, 106.5)] + [FLAT] * 3
    candles = make_candles(bars)
    strategy = StubStrategy(at_index=4, intent=LONG_INTENT)
    config = BacktestConfig()
    result = run_backtest(candles, None, strategy, config)
    total_net = sum(t.net_pnl_usd for t in result.trades)
    assert result.final_equity_usd == pytest.approx(
        config.initial_equity_usd + total_net
    )
    # fees actually charged: taker both sides
    trade = result.trades[0]
    expected_fees = (
        trade.quantity * trade.entry_price * FEE + trade.quantity * trade.exit_price * FEE
    )
    assert trade.fees_usd == pytest.approx(expected_fees)


def test_sizing_rejection_skips_trade():
    candles = make_candles([FLAT] * 10)
    # 0.1% stop -> implied leverage ~10x > 5x default cap -> sizer rejects
    tight = SignalIntent(side="long", stop_price=99.9)
    strategy = StubStrategy(at_index=4, intent=tight)
    result = run_backtest(candles, None, strategy, BacktestConfig())
    assert result.trades == ()
    assert result.skipped_by_sizing == 1


def test_open_position_closed_at_end_of_data():
    candles = make_candles([FLAT] * 8)
    strategy = StubStrategy(at_index=4, intent=LONG_INTENT)
    result = run_backtest(candles, None, strategy, BacktestConfig(max_holding_bars=100))
    assert result.trades[0].exit_reason == "end_of_data"


def test_unvalidated_candles_rejected():
    candles = make_candles([FLAT] * 10)
    shuffled = candles.iloc[::-1].reset_index(drop=True)
    strategy = StubStrategy(at_index=4, intent=LONG_INTENT)
    with pytest.raises(ValueError, match="gate-validated"):
        run_backtest(shuffled, None, strategy, BacktestConfig())


def test_stopless_intent_is_unrepresentable():
    with pytest.raises(ValueError, match="stop-less"):
        SignalIntent(side="long", stop_price=0.0)


def test_daily_factory_blocks_entries_after_cutoff():
    candles = make_candles([FLAT] * 10)
    strategy = StubStrategy(at_index=4, intent=LONG_INTENT)
    config = BacktestConfig(
        daily_factory=DailySignalFactoryConfig(
            enabled=True,
            entry_cutoff_minute=0,
            force_flatten_minute=1439,
        )
    )

    result = run_backtest(candles, None, strategy, config)

    assert result.trades == ()
    assert result.factory_blocked
    assert result.factory_blocked[0][1].startswith("daily_factory_entry_cutoff")


def test_daily_factory_force_closes_open_position_before_session_end():
    candles = make_minute_candles(
        "2026-08-02T20:00:00Z",
        [FLAT] * 8,
        step_minutes=5,
    )
    strategy = StubStrategy(at_index=4, intent=LONG_INTENT)
    config = BacktestConfig(
        max_holding_bars=100,
        daily_factory=DailySignalFactoryConfig(
            enabled=True,
            entry_cutoff_minute=20 * 60 + 29,
            force_flatten_minute=20 * 60 + 30,
        ),
    )

    result = run_backtest(candles, None, strategy, config)

    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == "daily_factory_close"
    assert result.trades[0].entry_ts.date() == result.trades[0].exit_ts.date()
