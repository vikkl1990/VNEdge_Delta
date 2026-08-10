"""Robust validation helpers for Delta scalper research evidence."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from vnedge.ml.validation import (
    combinatorial_purged_splits,
    deflated_sharpe_ratio,
    probability_of_backtest_overfitting,
)
from vnedge.scalping.delta_engine.fee_model import DeltaFeeModel


@dataclass(frozen=True)
class RobustValidationReport:
    configs: int
    observations: int
    deflated_sharpe: float | None
    pbo: float | None
    cpcv_paths: int
    status: str

    def to_dict(self) -> dict[str, object]:
        return self.__dict__.copy()


def robust_validation_report(
    performance_matrix: np.ndarray,
    *,
    selected_config: int,
    label_horizon: int = 28,
    embargo_pct: float = 0.01,
) -> RobustValidationReport:
    """Calculate DSR/PBO and purged CPCV path count for a config family."""
    matrix = np.asarray(performance_matrix, dtype=float)
    if matrix.ndim != 2:
        raise ValueError("performance_matrix must be 2-D")
    observations, configs = matrix.shape
    if not 0 <= selected_config < configs:
        raise ValueError("selected_config is out of range")
    if observations < 6:
        return RobustValidationReport(
            configs, observations, None, None, 0, "insufficient_observations"
        )
    splits = combinatorial_purged_splits(
        observations,
        n_groups=6,
        n_test_groups=2,
        embargo_pct=embargo_pct,
        label_horizon=label_horizon,
    )
    if configs < 2:
        return RobustValidationReport(
            configs,
            observations,
            None,
            None,
            len(splits),
            "requires_multiple_preregistered_configs",
        )
    sharpes = []
    for column in range(configs):
        values = matrix[:, column]
        deviation = values.std(ddof=1)
        sharpes.append(float(values.mean() / deviation) if deviation else 0.0)
    selected = matrix[:, selected_config]
    dsr = deflated_sharpe_ratio(
        selected,
        n_trials=configs,
        trial_sharpes=sharpes,
    )
    even_blocks = min(16, observations if observations % 2 == 0 else observations - 1)
    pbo = probability_of_backtest_overfitting(matrix, n_blocks=max(2, even_blocks))
    return RobustValidationReport(configs, observations, dsr, pbo, len(splits), "complete")


def fee_sensitivity(trades: list[dict], *, slippage_bps_per_leg: float = 1.5) -> list[dict]:
    """Reprice one fixed trade set across DETO and Scalper Offer states.

    This is a cost sensitivity, not a substitute for rerunning signal gates;
    that limitation is recorded in every returned row.
    """
    rows: list[dict] = []
    for scalper in (False, True):
        for deto in (False, True):
            model = DeltaFeeModel(
                deto_enabled=deto,
                scalper_opted_in=scalper,
                default_slippage_bps_per_leg=slippage_bps_per_leg,
            )
            net = []
            compliant = 0
            for trade in trades:
                costs = model.breakdown(
                    str(trade["symbol"]),
                    entry_is_maker=bool(trade.get("entry_is_maker", True)),
                    hold_seconds=float(trade["hold_seconds"]),
                )
                net.append(float(trade["gross_bps"]) - costs.total_bps)
                compliant += int(costs.scalper_eligible)
            rows.append(
                {
                    "scalper_opted_in": scalper,
                    "deto_enabled": deto,
                    "trades": len(trades),
                    "net_bps": sum(net),
                    "average_net_bps": sum(net) / len(net) if net else 0.0,
                    "scalper_compliance_rate": compliant / len(trades) if trades else 0.0,
                    "fixed_trade_set_only": True,
                }
            )
    return rows


def untouched_window_summary(trades: list[dict], *, fraction: float = 0.20) -> dict:
    if not 0 < fraction < 1:
        raise ValueError("untouched fraction must be between 0 and 1")
    ordered = sorted(trades, key=lambda row: str(row["exit_ts"]))
    split = max(1, int(len(ordered) * (1 - fraction))) if ordered else 0

    def summarize(rows: list[dict]) -> dict:
        wins = [float(row["net_bps"]) for row in rows if float(row["net_bps"]) > 0]
        losses = [-float(row["net_bps"]) for row in rows if float(row["net_bps"]) < 0]
        return {
            "trades": len(rows),
            "net_bps": sum(float(row["net_bps"]) for row in rows),
            "average_net_bps": (
                sum(float(row["net_bps"]) for row in rows) / len(rows) if rows else 0.0
            ),
            "profit_factor": sum(wins) / sum(losses) if losses else None,
        }

    return {
        "selection_window": summarize(ordered[:split]),
        "second_untouched_window": summarize(ordered[split:]),
        "untouched_fraction": fraction,
        "chronological": True,
    }
