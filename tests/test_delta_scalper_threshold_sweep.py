from __future__ import annotations

from datetime import UTC, datetime, timedelta

from vnedge.research.delta_scalper_threshold_sweep import (
    CandidateLedger,
    GateVariant,
    preregistered_variants,
    simulate_variant,
    summarize_simulations,
)
from vnedge.scalping.delta_engine.backtester import CausalScalperBacktester
from vnedge.scalping.delta_engine.candle_store import MultiTimeframeCandleStore
from vnedge.scalping.delta_engine.config import DeltaScalperConfig
from vnedge.scalping.delta_engine.factory import build_delta_scalper_assembly
from vnedge.scalping.delta_engine.types import Candle, Side, SignalCandidate

NOW = datetime(2025, 1, 1, tzinfo=UTC)


def _candidate() -> SignalCandidate:
    return SignalCandidate(
        scanner_id="test_scanner",
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
    )


def _rows() -> list[Candle]:
    return [
        Candle(NOW, 99.9, 100.1, 99.8, 100.0, 1, "1m"),
        Candle(
            NOW + timedelta(minutes=1),
            100.0,
            100.3,
            100.0,
            100.2,
            1,
            "1m",
        ),
        Candle(
            NOW + timedelta(minutes=2),
            100.2,
            100.3,
            100.1,
            100.2,
            1,
            "1m",
        ),
    ]


def _resolver() -> CausalScalperBacktester:
    store = MultiTimeframeCandleStore()
    assembly = build_delta_scalper_assembly(
        store,
        DeltaScalperConfig(),
        scalper_opted_in=True,
    )
    return CausalScalperBacktester(assembly.generator, assembly.fee_model, store)


def test_gate_sweep_reuses_canonical_next_open_exit_path():
    rows = _rows()
    ledger = CandidateLedger("BTCUSD", {NOW: (_candidate(),)}, 0)
    accepted = GateVariant("base", "baseline", 0, 0.70, 0.60, 8.0)
    rejected = GateVariant("prob", "probability", 0.80, 0.80, 0.60, 8.0)

    accepted_run = simulate_variant(rows, ledger, _resolver(), accepted)
    rejected_run = simulate_variant(rows, ledger, _resolver(), rejected)

    assert len(accepted_run.trades) == 1
    assert accepted_run.trades[0].entry_ts == NOW + timedelta(minutes=1)
    assert accepted_run.trades[0].exit_reason == "target_1"
    assert len(rejected_run.trades) == 0
    assert summarize_simulations([accepted_run])["profit_factor"] is None


def test_preregistered_variants_change_only_one_gate_family():
    config = DeltaScalperConfig()
    variants = preregistered_variants(config)
    assert len(variants) == 20
    baseline = variants[0]
    assert baseline.config_id == "baseline"
    for variant in variants[1:]:
        changed = sum(
            left != right
            for left, right in (
                (variant.min_probability, baseline.min_probability),
                (variant.min_confidence, baseline.min_confidence),
                (variant.min_expectancy_bps, baseline.min_expectancy_bps),
                (variant.min_expected_move_bps, baseline.min_expected_move_bps),
                (variant.min_net_fee_multiple, baseline.min_net_fee_multiple),
            )
        )
        assert changed == 1
