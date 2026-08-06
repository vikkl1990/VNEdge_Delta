from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd

from vnedge.research.delta_scalper_meta_label import (
    PREREGISTERED_THRESHOLDS,
    MetaLabelVariant,
    candidate_features,
    fit_model,
    label_comparison,
    outcome_label,
    trade_features,
    triple_barrier_label,
)
from vnedge.research.delta_scalper_threshold_sweep import GateVariant
from vnedge.scalping.delta_engine.types import Side, SignalCandidate

NOW = datetime(2025, 1, 1, 10, 30, tzinfo=UTC)


def _candidate() -> SignalCandidate:
    return SignalCandidate(
        scanner_id="delta_momentum_burst_v1",
        symbol="BTCUSD",
        side=Side.LONG,
        decision_ts=NOW,
        entry_price=100.0,
        stop_loss=99.8,
        take_profits=(100.3,),
        time_stop_seconds=600,
        expected_hold_seconds=300,
        expected_move_bps=30.0,
        raw_expectancy_bps=20.0,
        modeled_cost_bps=5.0,
        fee_adjusted_expectancy_bps=15.0,
        scalper_probability=0.80,
        confidence=0.75,
        metadata={
            "regime": "expanding",
            "regime_profile": {
                "trend": "strong_trend",
                "trend_direction": "up",
                "volatility": "high",
                "session": "europe",
                "change_point": {
                    "shift_window": "00-30m",
                    "bars_since_shift": 2,
                    "return_score": 4.0,
                    "volatility_score": 6.0,
                },
                "metrics": {
                    "atr_percentile": 0.8,
                    "bb_width_percentile": 0.7,
                },
            },
        },
    )


def _trade(index: int, *, winner: bool) -> dict:
    candidate = _candidate()
    features = candidate_features(candidate)
    expected_move = 35.0 + index * 0.1 if winner else 12.0 + index * 0.05
    return {
        "scanner_id": features["scanner_id"],
        "symbol": features["symbol"],
        "side": features["side"],
        "decision_ts": (NOW + timedelta(minutes=index)).isoformat(),
        "exit_ts": (NOW + timedelta(minutes=index + 1)).isoformat(),
        "regime_at_entry": features["regime"],
        "trend_regime_at_entry": features["trend_regime"],
        "trend_direction_at_entry": features["trend_direction"],
        "volatility_regime_at_entry": features["volatility_regime"],
        "session_regime_at_entry": features["session_regime"],
        "change_point_window_at_entry": features["change_point_window"],
        "expected_move_bps": expected_move,
        "expected_net_bps": 15.0 if winner else 8.0,
        "scalper_probability": 0.85 if winner else 0.72,
        "confidence": 0.82 if winner else 0.64,
        "expected_fee_multiple": features["expected_fee_multiple"],
        "atr_percentile_at_entry": features["atr_percentile"],
        "bb_width_percentile_at_entry": features["bb_width_percentile"],
        "planned_stop_bps": features["planned_stop_bps"],
        "planned_target_bps": features["planned_target_bps"],
        "change_point_return_score": features["change_point_return_score"],
        "change_point_volatility_score": features[
            "change_point_volatility_score"
        ],
        "change_point_bars_since_shift": features["bars_since_regime_change"],
        "cusum_alarm_recent_at_entry": bool(features["cusum_alarm_recent"]),
        "net_bps": 8.0 if winner else -10.0,
        "exit_reason": "target_1" if winner else "stop",
        "triple_barrier_label": int(winner),
        "triple_barrier_outcome": "upper" if winner else "lower",
    }


def test_candidate_and_trade_feature_contracts_match():
    candidate = _candidate()
    candidate_row = candidate_features(candidate)
    trade_row = trade_features(_trade(0, winner=True))

    for key in (
        "scanner_id",
        "symbol",
        "side",
        "regime",
        "trend_regime",
        "trend_direction",
        "volatility_regime",
        "session_regime",
        "change_point_window",
        "planned_stop_bps",
        "planned_target_bps",
        "expected_fee_multiple",
        "atr_percentile",
        "bb_width_percentile",
        "bars_since_regime_change",
        "cusum_alarm_recent",
    ):
        assert trade_row[key] == candidate_row[key]


def test_meta_label_model_and_threshold_are_deterministic():
    training = [
        _trade(index, winner=index % 2 == 0)
        for index in range(80)
    ]
    model = fit_model(training, label_net_bps=3.0)
    probability = float(
        model.predict_proba(pd.DataFrame([candidate_features(_candidate())]))[0, 1]
    )
    baseline = GateVariant("baseline", "baseline", 0.0, 0.70, 0.60, 8.0)
    accepted = MetaLabelVariant(
        threshold=max(0.0, probability - 0.01),
        scores={_candidate().dedup_key: probability},
        baseline_gate=baseline,
    )
    rejected = MetaLabelVariant(
        threshold=min(1.0, probability + 0.01),
        scores={_candidate().dedup_key: probability},
        baseline_gate=baseline,
    )

    assert accepted.accepts(_candidate()) is True
    assert rejected.accepts(_candidate()) is False


def test_meta_label_threshold_grid_matches_preregistered_design():
    assert PREREGISTERED_THRESHOLDS[0] == 0.50
    assert PREREGISTERED_THRESHOLDS[-1] == 0.94
    assert len(PREREGISTERED_THRESHOLDS) == 23


def test_triple_barrier_label_is_target_before_stop_or_time():
    assert triple_barrier_label({"exit_reason": "target_1"}) is True
    assert triple_barrier_label({"exit_reason": "stop"}) is False
    assert triple_barrier_label({"exit_reason": "time_stop"}) is False
    assert triple_barrier_label(
        {"exit_reason": "stop", "triple_barrier_label": 1}
    ) is True

    comparison = label_comparison(
        [
            {"exit_reason": "target_1", "net_bps": 8.0},
            {"exit_reason": "time_stop", "net_bps": 6.0},
            {"exit_reason": "stop", "net_bps": -10.0},
        ],
        label_net_bps=4.0,
    )
    assert comparison["triple_barrier_positive"] == 1
    assert comparison["fixed_net_positive"] == 2
    assert comparison["label_disagreements"] == 1
    assert outcome_label(
        {"exit_reason": "target_1"},
        label_mode="triple_barrier",
        label_net_bps=999,
    ) is True


def test_triple_barrier_meta_model_is_deterministic():
    training = [_trade(index, winner=index % 2 == 0) for index in range(80)]
    first = fit_model(
        training,
        label_net_bps=4.0,
        label_mode="triple_barrier",
    )
    second = fit_model(
        training,
        label_net_bps=4.0,
        label_mode="triple_barrier",
    )
    frame = pd.DataFrame([candidate_features(_candidate())])
    assert first.predict_proba(frame).tolist() == second.predict_proba(frame).tolist()
