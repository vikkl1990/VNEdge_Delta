"""Deterministic v1 move/expectancy estimator.

This is deliberately a calibrated heuristic, not an ML claim.  Its inputs and
weights are stable in live and replay.  Walk-forward evidence may replace the
calibration later without changing scanner contracts.
"""

from __future__ import annotations

from dataclasses import dataclass

from vnedge.scalping.delta_engine.types import MarketContext, Regime, Side


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass(frozen=True)
class MoveEstimate:
    expected_move_bps: float
    probability: float
    confidence: float
    expected_hold_seconds: int


class MovePredictor:
    """Small monotonic model whose output never depends on future data."""

    def estimate(self, ctx: MarketContext, side: Side, *, setup_strength: float) -> MoveEstimate:
        features = ctx.features
        atr = max(1.0, float(features.get("atr_bps", 0.0)))
        volume_z = float(features.get("volume_z", 0.0))
        body = float(features.get("body_ratio", 0.0))
        aligned = (
            side is Side.LONG and ctx.regime is Regime.TRENDING_UP
        ) or (side is Side.SHORT and ctx.regime is Regime.TRENDING_DOWN)
        regime_score = 0.08 if aligned else 0.04 if ctx.regime is Regime.EXPANDING else 0.0
        probability = _clip(
            0.52
            + 0.14 * _clip(setup_strength, 0.0, 1.5)
            + 0.035 * _clip(volume_z, 0.0, 3.0)
            + 0.05 * body
            + regime_score,
            0.50,
            0.88,
        )
        confidence = _clip(0.45 + 0.28 * setup_strength + regime_score, 0.0, 0.95)
        expected_move = _clip(atr * (0.75 + 0.35 * setup_strength), 6.0, 45.0)
        hold = 8 * 60 if ctx.regime is Regime.EXPANDING else 14 * 60
        return MoveEstimate(expected_move, probability, confidence, hold)

