"""Promotion gates — every rejection rule, plus the passing case."""

import pandas as pd

from vnedge.backtest.metrics import BacktestMetrics
from vnedge.backtest.walk_forward import (
    PromotionGates,
    WalkForwardResult,
    WindowResult,
    evaluate_promotion,
)
from vnedge.governance.promotion_policy import DEFAULT_PROMOTION_POLICY

BASE = 1_750_000_000_000


def ts(i: int) -> pd.Timestamp:
    return pd.Timestamp(BASE + i * 3_600_000, unit="ms", tz="UTC")


def metrics(
    num_trades: int = 10,
    net: float = 20.0,
    max_dd: float = 3.0,
    win_rate: float = 60.0,
    avg_win: float = 6.0,
    avg_loss: float = -4.0,
) -> BacktestMetrics:
    return BacktestMetrics(
        num_trades=num_trades, skipped_by_sizing=0, net_profit_usd=net,
        return_pct=net / 5.0, max_drawdown_pct=max_dd, sharpe=1.0, sortino=1.2,
        profit_factor=1.5, win_rate_pct=win_rate, avg_win_usd=avg_win,
        avg_loss_usd=avg_loss, total_fees_usd=1.0, total_funding_usd=0.0,
        exit_reasons={},
    )


def window(i: int, train: BacktestMetrics, test: BacktestMetrics) -> WindowResult:
    return WindowResult(
        window_index=i, train_start=ts(i * 100), test_start=ts(i * 100 + 45),
        test_end=ts(i * 100 + 60), chosen_params={"p": 1},
        train_metrics=train, test_metrics=test,
    )


def result_with(tests: list[BacktestMetrics], trains: list[BacktestMetrics] | None = None):
    trains = trains or [metrics() for _ in tests]
    return WalkForwardResult(
        windows=tuple(window(i, tr, te) for i, (tr, te) in enumerate(zip(trains, tests)))
    )


def test_good_result_passes():
    decision = evaluate_promotion(result_with([metrics(), metrics(), metrics()]))
    assert decision.passed, decision.reject_reasons
    assert decision.policy_version == DEFAULT_PROMOTION_POLICY.policy_version
    assert "eligible for paper trading" in decision.summary


def test_too_few_splits_rejected():
    decision = evaluate_promotion(result_with([metrics(), metrics()]))
    assert not decision.passed
    assert any("OOS splits" in r for r in decision.reject_reasons)


def test_zero_trade_oos_split_rejected():
    tests = [metrics(), metrics(num_trades=0, net=0.0, win_rate=0.0), metrics()]
    decision = evaluate_promotion(result_with(tests))
    assert not decision.passed
    assert any("zero OOS trades" in r for r in decision.reject_reasons)


def test_negative_aggregate_net_rejected():
    tests = [metrics(net=5.0), metrics(net=-20.0), metrics(net=5.0)]
    decision = evaluate_promotion(result_with(tests))
    assert not decision.passed
    assert any("not positive" in r for r in decision.reject_reasons)


def test_low_profit_factor_rejected():
    # 50% win rate, avg win 4, avg loss -4 -> PF 1.0 < 1.1, but keep net
    # positive via one window so only the PF gate fires alongside retention.
    weak = metrics(num_trades=10, net=0.5, win_rate=50.0, avg_win=4.0, avg_loss=-4.0)
    decision = evaluate_promotion(
        result_with([weak, weak, weak], trains=[metrics(net=1.0)] * 3)
    )
    assert not decision.passed
    assert any("profit factor" in r for r in decision.reject_reasons)


def test_drawdown_gate():
    tests = [metrics(), metrics(max_dd=22.0), metrics()]
    decision = evaluate_promotion(result_with(tests))
    assert not decision.passed
    assert any("drawdown" in r for r in decision.reject_reasons)


def test_min_total_trades_gate():
    tests = [metrics(num_trades=2), metrics(num_trades=2), metrics(num_trades=2)]
    decision = evaluate_promotion(result_with(tests))
    assert not decision.passed
    assert any("total OOS trades" in r for r in decision.reject_reasons)


def test_is_oos_collapse_rejected():
    # IS made $100/window; OOS scrapes $2/window -> 2% retention < 25%
    trains = [metrics(net=100.0)] * 3
    tests = [metrics(net=2.0)] * 3
    decision = evaluate_promotion(result_with(tests, trains))
    assert not decision.passed
    assert any("collapse" in r for r in decision.reject_reasons)


def test_gates_are_configurable():
    tests = [metrics(num_trades=2), metrics(num_trades=2), metrics(num_trades=2)]
    lenient = PromotionGates(min_total_oos_trades=5)
    decision = evaluate_promotion(result_with(tests), lenient)
    assert decision.passed, decision.reject_reasons


def test_sparse_gates_tolerate_eventless_windows():
    """Pre-registered round-3 variant: zero-trade windows allowed as long as
    coverage stays above the floor and aggregate gates hold."""
    from vnedge.backtest.walk_forward import SPARSE_STRATEGY_GATES

    quiet = metrics(num_trades=0, net=0.0, win_rate=0.0)
    tests = [metrics(num_trades=6), metrics(num_trades=6), quiet,
             metrics(num_trades=6), metrics(num_trades=6)]  # 80% traded
    decision = evaluate_promotion(result_with(tests), SPARSE_STRATEGY_GATES)
    assert decision.passed, decision.reject_reasons


def test_sparse_gates_still_reject_poor_coverage():
    from vnedge.backtest.walk_forward import SPARSE_STRATEGY_GATES

    quiet = metrics(num_trades=0, net=0.0, win_rate=0.0)
    tests = [metrics(num_trades=12), quiet, quiet, quiet, quiet]  # 20% traded
    decision = evaluate_promotion(result_with(tests), SPARSE_STRATEGY_GATES)
    assert not decision.passed
    assert any("windows traded" in r for r in decision.reject_reasons)


def _trade(net: float):
    from vnedge.backtest.backtester import Trade

    return Trade(side="long", quantity=1.0, entry_ts=ts(0), entry_price=100.0,
                 exit_ts=ts(1), exit_price=100.0 + net, exit_reason="stop",
                 gross_pnl_usd=net, fees_usd=0.0, funding_usd=0.0, entry_reason="t")


def offensive_window(i, trades, m=None):
    return WindowResult(
        window_index=i, train_start=ts(i * 100), test_start=ts(i * 100 + 45),
        test_end=ts(i * 100 + 60), chosen_params={"p": 1},
        train_metrics=metrics(), test_metrics=m or metrics(),
        test_trades=tuple(trades),
    )


def test_offensive_gates_pass_with_asymmetric_payoff():
    from vnedge.backtest.walk_forward import OFFENSIVE_GATES

    m = metrics(num_trades=6, net=15.0, win_rate=50.0, avg_win=9.0, avg_loss=-4.0)
    trades = [_trade(9.0)] * 3 + [_trade(-4.0)] * 3  # no concentration
    result = WalkForwardResult(windows=tuple(
        offensive_window(i, trades, m) for i in range(3)
    ))
    decision = evaluate_promotion(result, OFFENSIVE_GATES)
    assert decision.passed, decision.reject_reasons


def test_offensive_gates_reject_weak_payoff_ratio():
    from vnedge.backtest.walk_forward import OFFENSIVE_GATES

    # PF fine (1.5) but payoff 6/4 = 1.5 < 1.8 floor
    m = metrics(num_trades=6, net=6.0, win_rate=50.0, avg_win=6.0, avg_loss=-4.0)
    trades = [_trade(6.0)] * 3 + [_trade(-4.0)] * 3
    result = WalkForwardResult(windows=tuple(
        offensive_window(i, trades, m) for i in range(3)
    ))
    decision = evaluate_promotion(result, OFFENSIVE_GATES)
    assert not decision.passed
    assert any("payoff ratio" in r for r in decision.reject_reasons)


def test_offensive_gates_reject_one_lucky_trade():
    from vnedge.backtest.walk_forward import OFFENSIVE_GATES

    m = metrics(num_trades=6, net=20.0, win_rate=50.0, avg_win=12.0, avg_loss=-4.0)
    # one monster trade in ONE window carries 82% of total gross profit
    per_window = [
        [_trade(54.0), _trade(-4.0)],
        [_trade(6.0), _trade(-4.0)],
        [_trade(6.0), _trade(-4.0)],
    ]
    result = WalkForwardResult(windows=tuple(
        offensive_window(i, per_window[i], m) for i in range(3)
    ))
    decision = evaluate_promotion(result, OFFENSIVE_GATES)
    assert not decision.passed
    assert any("lucky trade" in r for r in decision.reject_reasons)


def test_all_reasons_reported_together():
    tests = [metrics(num_trades=0, net=-10.0, win_rate=0.0, max_dd=30.0)] * 2
    decision = evaluate_promotion(result_with(tests))
    assert not decision.passed
    assert len(decision.reject_reasons) >= 4  # splits, zero-trade, net, dd...
