"""Optimizer-style fitness scorecards for research rows.

OctoBot's useful pattern here is not its live trading stack; it is the split
between hard exclusion filters and weighted fitness parameters.  VNEDGE keeps
its existing promotion gates as the authority and uses this only to explain
which lanes are close, which filters failed, and which metric is dragging.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class OptimizerScorecardConfig:
    min_trades: int = 10
    min_profit_factor: float = 1.25
    min_net_usd: float = 0.0
    max_trade_sample: int = 60
    max_profit_factor: float = 3.0
    max_payoff_ratio: float = 3.0
    max_net_usd: float = 100.0
    max_fee_multiple: float = 3.0
    near_miss_score: float = 60.0


def build_optimizer_scorecard(
    *,
    net_usd: float,
    trades: int,
    fees_usd: float,
    profit_factor: float,
    payoff_ratio: float,
    profitable_windows_pct: float,
    config: OptimizerScorecardConfig = OptimizerScorecardConfig(),
) -> dict:
    """Build a research-only scorecard for one lane/variant row."""

    fee_multiple = _fee_multiple(net_usd, fees_usd)
    hard_filters = [
        _filter(
            "min_trades",
            actual=float(trades),
            threshold=float(config.min_trades),
            operator=">=",
            passed=trades >= config.min_trades,
            reason=f"needs at least {config.min_trades} OOS trades",
        ),
        _filter(
            "positive_net_after_fees",
            actual=net_usd,
            threshold=config.min_net_usd,
            operator=">",
            passed=net_usd > config.min_net_usd,
            reason="net must be positive after fees",
        ),
        _filter(
            "min_profit_factor",
            actual=profit_factor,
            threshold=config.min_profit_factor,
            operator=">=",
            passed=profit_factor >= config.min_profit_factor,
            reason=f"PF must clear {config.min_profit_factor:.2f}",
        ),
    ]
    components = [
        _component(
            "profit_factor",
            raw=profit_factor,
            normalized=_scale(profit_factor, 1.0, config.max_profit_factor),
            weight=30.0,
            target=config.min_profit_factor,
        ),
        _component(
            "trade_sample",
            raw=float(trades),
            normalized=_ratio(trades, config.max_trade_sample),
            weight=20.0,
            target=float(config.min_trades),
        ),
        _component(
            "net_after_fees",
            raw=net_usd,
            normalized=_ratio(max(net_usd, 0.0), config.max_net_usd),
            weight=20.0,
            target=0.0,
        ),
        _component(
            "payoff_ratio",
            raw=payoff_ratio,
            normalized=_ratio(payoff_ratio, config.max_payoff_ratio),
            weight=15.0,
            target=1.8,
        ),
        _component(
            "fee_multiple",
            raw=fee_multiple,
            normalized=_ratio(max(fee_multiple, 0.0), config.max_fee_multiple),
            weight=10.0,
            target=0.5,
        ),
        _component(
            "window_consistency",
            raw=profitable_windows_pct,
            normalized=_ratio(profitable_windows_pct, 100.0),
            weight=5.0,
            target=60.0,
        ),
    ]
    total_weight = sum(component["weight"] for component in components)
    score = (
        sum(component["contribution"] for component in components) / total_weight * 100.0
        if total_weight
        else 0.0
    )
    hard_filters_passed = all(item["passed"] for item in hard_filters)
    return {
        "source": "octobot_optimizer_pattern",
        "score": round(_clamp(score, 0.0, 100.0), 2),
        "hard_filters_passed": hard_filters_passed,
        "near_miss": (not hard_filters_passed) and score >= config.near_miss_score,
        "hard_filters": hard_filters,
        "components": components,
        "total_weight": total_weight,
        "can_trade": False,
        "can_promote": False,
    }


def optimizer_scorecard_policy(
    config: OptimizerScorecardConfig = OptimizerScorecardConfig(),
) -> dict:
    return {
        "source": "OctoBot optimizer fitness/filter pattern adapted for VNEDGE",
        "research_only": True,
        "can_trade": False,
        "can_promote": False,
        "hard_filters_do_not_replace_promotion_gates": True,
        "score_is_explanatory_only": True,
        "config": asdict(config),
        "hard_filters": [
            "min_trades",
            "positive_net_after_fees",
            "min_profit_factor",
        ],
        "fitness_components": [
            "profit_factor",
            "trade_sample",
            "net_after_fees",
            "payoff_ratio",
            "fee_multiple",
            "window_consistency",
        ],
    }


def _filter(
    name: str,
    *,
    actual: float,
    threshold: float,
    operator: str,
    passed: bool,
    reason: str,
) -> dict:
    return {
        "name": name,
        "actual": round(_finite(actual), 4),
        "threshold": round(_finite(threshold), 4),
        "operator": operator,
        "passed": bool(passed),
        "reason": "" if passed else reason,
    }


def _component(name: str, *, raw: float, normalized: float, weight: float, target: float) -> dict:
    norm = _clamp(_finite(normalized), 0.0, 1.0)
    return {
        "name": name,
        "raw": round(_finite(raw), 4),
        "normalized": round(norm, 4),
        "weight": weight,
        "contribution": round(norm * weight, 4),
        "target": round(_finite(target), 4),
    }


def _fee_multiple(net_usd: float, fees_usd: float) -> float:
    if fees_usd <= 0:
        return 0.0
    return _finite(net_usd / fees_usd)


def _scale(value: float, low: float, high: float) -> float:
    if math.isclose(high, low):
        return 0.0
    return (value - low) / (high - low)


def _ratio(value: float, denom: float) -> float:
    if denom <= 0:
        return 0.0
    return value / denom


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(value, high))


def _finite(value: object, default: float = 0.0) -> float:
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if not math.isfinite(out):
        return default
    return out
