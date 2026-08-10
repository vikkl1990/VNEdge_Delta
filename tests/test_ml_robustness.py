"""Model robustness tests: PSI drift, ECE calibration, fail-safe health guard."""

import numpy as np
import pytest

from vnedge.ml.robustness import (
    ModelHealth,
    ProbabilityCalibrator,
    assess_model_health,
    expected_calibration_error,
    feature_drift_report,
    population_stability_index,
)


# ---- drift (PSI) -----------------------------------------------------------

def test_psi_near_zero_for_same_distribution():
    rng = np.random.default_rng(0)
    ref = rng.normal(0, 1, 5000)
    cur = rng.normal(0, 1, 5000)
    assert population_stability_index(ref, cur) < 0.10


def test_psi_flags_a_shifted_distribution():
    rng = np.random.default_rng(1)
    ref = rng.normal(0, 1, 5000)
    cur = rng.normal(1.5, 1, 5000)  # mean shift
    assert population_stability_index(ref, cur) >= 0.25


def test_psi_constant_reference_is_zero():
    assert population_stability_index(np.ones(100), np.linspace(0, 1, 100)) == 0.0


def test_feature_drift_report_finds_worst_feature():
    rng = np.random.default_rng(2)
    ref = {"stable": rng.normal(0, 1, 4000), "drifted": rng.normal(0, 1, 4000)}
    cur = {"stable": rng.normal(0, 1, 4000), "drifted": rng.normal(2.0, 1, 4000)}
    rep = feature_drift_report(ref, cur)
    assert rep["worst_feature"] == "drifted"
    assert rep["verdict"] == "SIGNIFICANT"
    assert rep["psi"]["drifted"] > rep["psi"]["stable"]


# ---- calibration (ECE) -----------------------------------------------------

def test_ece_small_for_calibrated_predictor():
    rng = np.random.default_rng(3)
    p_true = rng.uniform(0, 1, 8000)
    y = (rng.random(8000) < p_true).astype(float)
    assert expected_calibration_error(y, p_true) < 0.05  # p_pred == true prob


def test_isotonic_calibration_reduces_ece():
    rng = np.random.default_rng(4)
    n = 12000
    p_true = rng.uniform(0, 1, n)
    y = (rng.random(n) < p_true).astype(float)
    p_raw = np.clip(p_true ** 2, 0, 1)  # systematically miscalibrated (monotone)
    tr, te = slice(0, n // 2), slice(n // 2, n)
    before = expected_calibration_error(y[te], p_raw[te])
    cal = ProbabilityCalibrator("isotonic").fit(p_raw[tr], y[tr])
    after = expected_calibration_error(y[te], cal.transform(p_raw[te]))
    assert before > 0.05
    assert after < before


def test_calibrator_requires_fit_and_stays_in_unit_range():
    cal = ProbabilityCalibrator("isotonic")
    with pytest.raises(RuntimeError):
        cal.transform([0.3, 0.6])
    rng = np.random.default_rng(5)
    p = rng.uniform(0, 1, 500)
    y = (rng.random(500) < p).astype(float)
    out = cal.fit(p, y).transform(p)
    assert out.min() >= 0.0 and out.max() <= 1.0


# ---- fail-safe health guard ------------------------------------------------

def _healthy(**over):
    args = dict(
        model_age_seconds=100, max_age_seconds=3600,
        drift_psi=0.02, recent_profit_factor=1.4, min_profit_factor=1.0,
    )
    args.update(over)
    return assess_model_health(**args)


def test_health_uses_model_when_all_green():
    h = _healthy()
    assert isinstance(h, ModelHealth) and h.use_model is True
    assert h.reasons == ("healthy",)


def test_stale_model_falls_back():
    h = _healthy(model_age_seconds=99999)
    assert h.use_model is False and any("stale" in r for r in h.reasons)


def test_drift_falls_back():
    h = _healthy(drift_psi=0.4)
    assert h.use_model is False and any("drift" in r for r in h.reasons)


def test_performance_decay_falls_back():
    h = _healthy(recent_profit_factor=0.7)
    assert h.use_model is False and any("decay" in r for r in h.reasons)


def test_insufficient_recent_trades_blocks():
    h = _healthy(recent_trades=2, min_recent_trades=10)
    assert h.use_model is False and any("insufficient" in r for r in h.reasons)


def test_no_pf_signal_without_floor_is_healthy():
    h = _healthy(recent_profit_factor=None)
    assert h.use_model is True
