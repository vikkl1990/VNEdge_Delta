"""Portfolio backtester — shared equity, slots, exposure, ordering, parity."""

from dataclasses import replace

import pandas as pd
import pytest

from vnedge.backtest.backtester import BacktestConfig, run_backtest
from vnedge.backtest.portfolio_backtester import (
    PortfolioBacktestConfig,
    run_portfolio_backtest,
)
from vnedge.data.schemas import normalize_candles, normalize_funding
from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent

BASE = 1_750_000_000_000
HOUR = 3_600_000
SLIP = 2 / 10_000  # default slippage bps

FLAT = (100.0, 101.0, 99.0, 100.0)
STOP_BAR = (100.0, 100.5, 96.0, 98.0)  # takes out a stop at 97
TP_BAR = (100.0, 107.0, 99.5, 106.5)  # takes out a target at 106

LONG_INTENT = SignalIntent(side="long", stop_price=97.0, take_profit_price=106.0)


def make_candles(bars: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    raw = [
        [BASE + i * HOUR, o, h, low, c, 10.0] for i, (o, h, low, c) in enumerate(bars)
    ]
    return normalize_candles(raw)


class StubStrategy(BaseStrategy):
    """Emits a fixed intent at chosen bar indices."""

    strategy_id = "stub"

    def __init__(self, at_indices: list[int], intent: SignalIntent = LONG_INTENT):
        self.at_indices = at_indices
        self.intent = intent
        self.signal_calls: list[int] = []

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        return candles.copy()

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        self.signal_calls.append(index)
        return self.intent if index in self.at_indices else None


def two_symbol_setup():
    """AAA stops out at bar 6; BBB takes profit at bar 10."""
    candles_a = make_candles([FLAT] * 6 + [STOP_BAR] + [FLAT] * 8)
    candles_b = make_candles([FLAT] * 10 + [TP_BAR] + [FLAT] * 4)
    datasets = {"AAA": (candles_a, None), "BBB": (candles_b, None)}
    strategies = {"AAA": StubStrategy([4]), "BBB": StubStrategy([8])}
    return datasets, strategies


def test_two_symbol_determinism():
    config = PortfolioBacktestConfig()
    results = [
        run_portfolio_backtest(*two_symbol_setup(), config) for _ in range(2)
    ]
    assert results[0].trades == results[1].trades
    assert results[0].equity_curve == results[1].equity_curve
    assert results[0].final_equity_usd == results[1].final_equity_usd


def test_trades_carry_their_symbol():
    result = run_portfolio_backtest(*two_symbol_setup(), PortfolioBacktestConfig())
    assert [t.symbol for t in result.trades] == ["AAA", "BBB"]
    assert result.trades[0].exit_reason == "stop"
    assert result.trades[1].exit_reason == "take_profit"
    assert result.per_symbol["AAA"].num_trades == 1
    assert result.per_symbol["AAA"].net_pnl_usd < 0 < result.per_symbol["BBB"].net_pnl_usd


def test_shared_equity_loss_reduces_later_sizing():
    datasets, strategies = two_symbol_setup()
    config = PortfolioBacktestConfig()
    portfolio = run_portfolio_backtest(datasets, strategies, config)
    solo_b = run_portfolio_backtest(
        {"BBB": datasets["BBB"]}, {"BBB": StubStrategy([8])}, config
    )
    trade_a = portfolio.trades[0]
    trade_b = portfolio.trades[1]
    assert trade_a.net_pnl_usd < 0
    # BBB entered after AAA's loss: sized from the reduced shared equity.
    assert trade_b.quantity < solo_b.trades[0].quantity
    # Exact: 1% of post-loss equity / stop distance, floored to 0.0001 step.
    equity_at_b_entry = config.initial_equity_usd + trade_a.net_pnl_usd
    risk_usd = equity_at_b_entry * config.risk.risk_per_trade_pct / 100.0
    raw_qty = risk_usd / (trade_b.entry_price - 97.0)
    assert trade_b.quantity == pytest.approx(int(raw_qty / 0.0001) * 0.0001)


def test_max_concurrent_positions_enforced_alphabetical_tie_break():
    candles = make_candles([FLAT] * 12)
    datasets = {s: (candles.copy(), None) for s in ("AAA", "BBB", "CCC")}
    strategies = {s: StubStrategy([4]) for s in ("AAA", "BBB", "CCC")}
    config = PortfolioBacktestConfig(max_concurrent_positions=2, max_holding_bars=3)
    result = run_portfolio_backtest(datasets, strategies, config)
    # All three signal at bar 4; only AAA and BBB get slots (alphabetical).
    assert sorted(t.symbol for t in result.trades) == ["AAA", "BBB"]
    assert result.skipped_by_slots == 1
    assert result.per_symbol["CCC"].num_trades == 0
    # CCC's strategy WAS consulted — the slot cap dropped it, not the engine.
    assert 4 in strategies["CCC"].signal_calls


def test_max_total_exposure_enforced():
    candles = make_candles([FLAT] * 12)
    datasets = {s: (candles.copy(), None) for s in ("AAA", "BBB")}
    strategies = {s: StubStrategy([4]) for s in ("AAA", "BBB")}
    # One position is ~$167 notional on $500 equity — cap total at 40% ($200)
    # so the second same-bar fill must be rejected at fill time.
    config = PortfolioBacktestConfig(max_total_exposure_pct=40.0, max_holding_bars=3)
    result = run_portfolio_backtest(datasets, strategies, config)
    assert [t.symbol for t in result.trades] == ["AAA"]
    assert result.skipped_by_exposure == 1


def test_exits_process_before_entries_on_same_timestamp():
    # AAA holds the only slot and stops out at bar 6 — the same bar BBB
    # signals on. The freed slot must be available to BBB.
    candles_a = make_candles([FLAT] * 6 + [STOP_BAR] + [FLAT] * 8)
    candles_b = make_candles([FLAT] * 15)
    datasets = {"AAA": (candles_a, None), "BBB": (candles_b, None)}
    strategies = {"AAA": StubStrategy([4]), "BBB": StubStrategy([6])}
    config = PortfolioBacktestConfig(max_concurrent_positions=1, max_holding_bars=3)
    result = run_portfolio_backtest(datasets, strategies, config)
    assert [t.symbol for t in result.trades] == ["AAA", "BBB"]
    assert result.skipped_by_slots == 0
    assert result.trades[1].entry_ts == candles_b["timestamp"].iloc[7]


def test_no_lookahead_truncating_future_keeps_past_trades():
    datasets, strategies = two_symbol_setup()
    config = PortfolioBacktestConfig()
    full = run_portfolio_backtest(datasets, strategies, config)
    truncated_sets = {
        s: (candles.iloc[:8].reset_index(drop=True), None)
        for s, (candles, _) in datasets.items()
    }
    truncated = run_portfolio_backtest(
        truncated_sets, {"AAA": StubStrategy([4]), "BBB": StubStrategy([8])}, config
    )
    # AAA's trade completed before the cut — it must be bit-identical.
    assert truncated.trades[0] == full.trades[0]
    assert truncated.equity_curve == full.equity_curve[: len(truncated.equity_curve)]


def test_one_position_per_symbol():
    # Strategy signals every bar; while a position is open the engine must
    # not even ask, so at most one position per symbol exists at a time.
    candles = make_candles([FLAT] * 12)
    strategy = StubStrategy(list(range(12)))
    config = PortfolioBacktestConfig(max_holding_bars=4)
    result = run_portfolio_backtest({"AAA": (candles, None)}, {"AAA": strategy}, config)
    for prev, cur in zip(result.trades, result.trades[1:]):
        assert cur.entry_ts > prev.exit_ts


def test_single_symbol_parity_with_run_backtest():
    # The strongest test: one symbol through the portfolio engine must
    # reproduce run_backtest EXACTLY — same fills, trades, equity path.
    bars = (
        [FLAT] * 6 + [STOP_BAR] + [FLAT] * 3 + [TP_BAR] + [FLAT] * 6
        + [(100.0, 102.0, 99.5, 101.5)] * 4 + [FLAT] * 4
    )
    candles = make_candles(bars)
    funding = normalize_funding(
        [
            {"timestamp": BASE + 5 * HOUR, "fundingRate": 0.001},
            {"timestamp": BASE + 13 * HOUR, "fundingRate": -0.0005},
        ]
    )
    signal_bars = list(range(len(bars)))  # signal whenever flat
    single_cfg = BacktestConfig(max_holding_bars=5)
    single = run_backtest(
        candles, funding, StubStrategy(signal_bars), single_cfg, timeframe="1h"
    )
    portfolio = run_portfolio_backtest(
        {"BTC/USDT:USDT": (candles, funding)},
        {"BTC/USDT:USDT": StubStrategy(signal_bars)},
        PortfolioBacktestConfig(max_holding_bars=5, max_total_exposure_pct=500.0),
        timeframe="1h",
    )
    assert len(single.trades) >= 3  # the scenario actually exercises the engine
    assert [replace(t, symbol="") for t in portfolio.trades] == list(single.trades)
    assert portfolio.final_equity_usd == single.final_equity_usd
    assert [ts for ts, _ in portfolio.equity_curve] == list(single.equity_curve.index)
    assert [eq for _, eq in portfolio.equity_curve] == list(single.equity_curve)
    assert portfolio.skipped_by_sizing == single.skipped_by_sizing


def test_missing_bars_fill_at_symbols_next_open():
    # BBB has no bar at the fill timestamp; its intent must wait for BBB's
    # next bar, not fill on another symbol's bar.
    candles_a = make_candles([FLAT] * 12)
    full_b = make_candles([FLAT] * 12)
    candles_b = full_b.drop(index=5).reset_index(drop=True)  # gap at bar 5
    datasets = {"AAA": (candles_a, None), "BBB": (candles_b, None)}
    strategies = {"AAA": StubStrategy([], LONG_INTENT), "BBB": StubStrategy([4])}
    config = PortfolioBacktestConfig(max_holding_bars=3)
    result = run_portfolio_backtest(datasets, strategies, config)
    assert [t.symbol for t in result.trades] == ["BBB"]
    assert result.trades[0].entry_ts == full_b["timestamp"].iloc[6]


def test_mismatched_symbol_sets_rejected():
    candles = make_candles([FLAT] * 8)
    with pytest.raises(ValueError, match="same symbols"):
        run_portfolio_backtest(
            {"AAA": (candles, None)},
            {"BBB": StubStrategy([4])},
            PortfolioBacktestConfig(),
        )
