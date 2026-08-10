"""Backtest performance metrics.

Judged per the project rules: risk-adjusted stability over raw return. A
strategy is never approved on total return alone — drawdown, profit factor,
trade count, and cost drag all get a vote.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

from vnedge.backtest.backtester import BacktestResult
from vnedge.data.schemas import TIMEFRAME_MS

_MS_PER_YEAR = 365 * 86_400_000


@dataclass(frozen=True)
class BacktestMetrics:
    num_trades: int
    skipped_by_sizing: int
    net_profit_usd: float
    return_pct: float
    max_drawdown_pct: float
    sharpe: float
    sortino: float
    profit_factor: float
    win_rate_pct: float
    avg_win_usd: float
    avg_loss_usd: float
    total_fees_usd: float
    total_funding_usd: float
    exit_reasons: dict[str, int]
    payoff_ratio: float = 0.0  # avg win / |avg loss|
    avg_mae_usd: float = 0.0
    avg_mfe_usd: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def summary(self) -> str:
        return (
            f"{self.num_trades} trades | net ${self.net_profit_usd:+.2f} "
            f"({self.return_pct:+.2f}%) | maxDD {self.max_drawdown_pct:.2f}% | "
            f"Sharpe {self.sharpe:.2f} | PF {self.profit_factor:.2f} | "
            f"win {self.win_rate_pct:.1f}% | fees ${self.total_fees_usd:.2f} | "
            f"funding ${self.total_funding_usd:+.2f}"
        )


def _annualized_ratio(returns, bars_per_year: float, downside_only: bool) -> float:
    if len(returns) < 2:
        return 0.0
    mean = returns.mean()
    std = returns[returns < 0].std() if downside_only else returns.std()
    if std is None or std == 0 or math.isnan(std):
        return 0.0
    return float(mean / std * math.sqrt(bars_per_year))


def compute_metrics(result: BacktestResult) -> BacktestMetrics:
    curve = result.equity_curve
    initial = result.config.initial_equity_usd
    trades = result.trades

    returns = curve.pct_change().dropna()
    bars_per_year = _MS_PER_YEAR / TIMEFRAME_MS[result.timeframe]

    drawdown = (curve / curve.cummax() - 1.0) * 100.0
    max_dd = float(-drawdown.min()) if len(drawdown) else 0.0

    pnls = [t.net_pnl_usd for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_wins = sum(wins)
    gross_losses = -sum(losses)
    if gross_losses > 0:
        profit_factor = gross_wins / gross_losses
    else:
        profit_factor = math.inf if gross_wins > 0 else 0.0

    exit_reasons: dict[str, int] = {}
    for t in trades:
        exit_reasons[t.exit_reason] = exit_reasons.get(t.exit_reason, 0) + 1

    return BacktestMetrics(
        num_trades=len(trades),
        skipped_by_sizing=result.skipped_by_sizing,
        net_profit_usd=result.final_equity_usd - initial,
        return_pct=(result.final_equity_usd / initial - 1.0) * 100.0,
        max_drawdown_pct=max_dd,
        sharpe=_annualized_ratio(returns, bars_per_year, downside_only=False),
        sortino=_annualized_ratio(returns, bars_per_year, downside_only=True),
        profit_factor=profit_factor,
        win_rate_pct=(len(wins) / len(trades) * 100.0) if trades else 0.0,
        avg_win_usd=(gross_wins / len(wins)) if wins else 0.0,
        avg_loss_usd=(-gross_losses / len(losses)) if losses else 0.0,
        total_fees_usd=sum(t.fees_usd for t in trades),
        total_funding_usd=sum(t.funding_usd for t in trades),
        exit_reasons=exit_reasons,
        payoff_ratio=(
            (gross_wins / len(wins)) / (gross_losses / len(losses))
            if wins and losses and gross_losses > 0 else 0.0
        ),
        avg_mae_usd=sum(t.mae_usd for t in trades) / len(trades) if trades else 0.0,
        avg_mfe_usd=sum(t.mfe_usd for t in trades) / len(trades) if trades else 0.0,
    )
