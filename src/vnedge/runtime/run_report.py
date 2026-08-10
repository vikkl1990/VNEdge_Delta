"""Machine-readable run report for paper/shadow runs."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from vnedge.backtest.metrics import BacktestMetrics


@dataclass(frozen=True)
class RunReport:
    mode: str
    symbol: str
    strategy_id: str
    bars_processed: int
    signals_generated: int
    orders_submitted: int
    fills: int
    fees_usd: float
    realized_pnl_usd: float
    unrealized_pnl_usd: float
    max_drawdown_pct: float
    risk_rejects: int
    sizing_skips: int
    shadow_approved: int
    shadow_rejected: int
    reconciliation_mismatches: int
    final_equity_usd: float

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def summary(self) -> str:
        return (
            f"[{self.mode}] {self.strategy_id} on {self.symbol}: "
            f"{self.bars_processed} bars, {self.signals_generated} signals, "
            f"{self.orders_submitted} orders, {self.fills} fills, "
            f"net ${self.realized_pnl_usd + self.unrealized_pnl_usd:+.2f}, "
            f"fees ${self.fees_usd:.2f}, maxDD {self.max_drawdown_pct:.2f}%, "
            f"risk rejects {self.risk_rejects}, "
            f"recon mismatches {self.reconciliation_mismatches}, "
            f"final equity ${self.final_equity_usd:.2f}"
        )

    def compare_to_backtest(self, expected: BacktestMetrics) -> dict:
        """Paper-vs-backtest drift on the same data. Large deltas mean the
        execution model and the research model disagree — investigate before
        trusting either."""
        paper_net = self.realized_pnl_usd + self.unrealized_pnl_usd
        return {
            "net_profit_delta_usd": paper_net - expected.net_profit_usd,
            "trade_count_delta": self.fills // 2 - expected.num_trades,
            "fees_delta_usd": self.fees_usd - expected.total_fees_usd,
            "max_drawdown_delta_pct": self.max_drawdown_pct - expected.max_drawdown_pct,
        }
