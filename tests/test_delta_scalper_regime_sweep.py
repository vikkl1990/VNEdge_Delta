from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from vnedge.research.delta_scalper_regime_sweep import (
    FADE,
    MOMENTUM,
    preregistered_regime_variants,
)
from vnedge.scalping.delta_engine.config import DeltaScalperConfig
from vnedge.scalping.delta_engine.types import Regime, Side, SignalCandidate

NOW = datetime(2025, 1, 1, tzinfo=UTC)


def _candidate(scanner_id: str, regime: Regime) -> SignalCandidate:
    return SignalCandidate(
        scanner_id=scanner_id,
        symbol="BTCUSD",
        side=Side.LONG,
        decision_ts=NOW,
        entry_price=100.0,
        stop_loss=99.9,
        take_profits=(100.2,),
        time_stop_seconds=600,
        expected_hold_seconds=300,
        expected_move_bps=20.0,
        raw_expectancy_bps=16.0,
        modeled_cost_bps=5.0,
        fee_adjusted_expectancy_bps=11.0,
        scalper_probability=0.75,
        confidence=0.72,
        metadata={"regime": regime.value},
    )


def test_regime_variants_change_regimes_without_changing_score_gates():
    config = DeltaScalperConfig()
    variants = preregistered_regime_variants(config)

    assert len(variants) == 11
    assert variants[0].config_id == "baseline"
    for variant in variants:
        assert variant.min_probability == config.engine.min_probability
        assert variant.min_confidence == config.engine.min_confidence
        assert variant.min_expectancy_bps == config.engine.min_expectancy_bps


def test_hard_regime_variants_filter_scanners_independently():
    variants = {
        variant.config_id: variant
        for variant in preregistered_regime_variants(DeltaScalperConfig())
    }
    momentum_trend = _candidate(MOMENTUM, Regime.TRENDING_UP)
    fade_quiet = _candidate(FADE, Regime.QUIET)

    assert variants["baseline"].accepts(momentum_trend)
    assert not variants["momentum_no_directional_trends"].accepts(momentum_trend)
    assert variants["fade_only"].accepts(fade_quiet)
    assert not variants["momentum_only"].accepts(fade_quiet)
    assert not variants["baseline"].accepts(
        replace(momentum_trend, scalper_probability=0.1)
    )
