"""Robust-validation toolkit tests: PSR, Deflated Sharpe, PBO, purged CPCV."""

import numpy as np
import pytest

from vnedge.ml.validation import (
    combinatorial_purged_splits,
    deflated_sharpe_ratio,
    expected_max_sharpe,
    probabilistic_sharpe_ratio,
    probability_of_backtest_overfitting,
)


# ---- PSR -------------------------------------------------------------------

def test_psr_is_a_probability_and_rises_with_sharpe():
    rng = np.random.default_rng(0)
    weak = rng.normal(0.0002, 0.01, 2000)
    strong = rng.normal(0.0020, 0.01, 2000)
    p_weak = probabilistic_sharpe_ratio(weak)
    p_strong = probabilistic_sharpe_ratio(strong)
    assert 0.0 <= p_weak <= 1.0 and 0.0 <= p_strong <= 1.0
    assert p_strong > p_weak
    # A clearly profitable, low-vol, long track record is convincingly > noise.
    assert p_strong > 0.99


def test_psr_penalises_negative_skew():
    # Same mean and std, but a fat/heavy left tail must lower the PSR.
    rng = np.random.default_rng(1)
    symmetric = rng.normal(0.001, 0.01, 4000)
    skewed = symmetric.copy()
    skewed[:: 50] -= 0.06  # inject occasional large losses (negative skew)
    # renormalise to the same mean/std so only the shape differs
    skewed = (skewed - skewed.mean()) / skewed.std() * symmetric.std() + symmetric.mean()
    assert probabilistic_sharpe_ratio(skewed) < probabilistic_sharpe_ratio(symmetric)


def test_psr_requires_minimum_length():
    with pytest.raises(ValueError):
        probabilistic_sharpe_ratio([0.1, 0.2])


# ---- Deflated Sharpe -------------------------------------------------------

def test_expected_max_sharpe_grows_with_trials():
    v = 0.01
    assert expected_max_sharpe(v, 1) == 0.0
    assert expected_max_sharpe(v, 100) > expected_max_sharpe(v, 10) > 0.0


def test_deflation_lowers_the_sharpe_verdict():
    rng = np.random.default_rng(2)
    returns = rng.normal(0.0015, 0.01, 3000)
    undeflated = probabilistic_sharpe_ratio(returns, benchmark_sr=0.0)
    # Searching over many configs deflates the benchmark => a lower verdict.
    deflated = deflated_sharpe_ratio(returns, n_trials=500, sr_variance=0.02)
    assert deflated < undeflated
    assert 0.0 <= deflated <= 1.0


def test_deflated_sharpe_from_trial_sharpes():
    rng = np.random.default_rng(3)
    returns = rng.normal(0.0015, 0.01, 3000)
    trial_sharpes = rng.normal(0.0, 0.1, 200)  # the family of configs searched
    dsr = deflated_sharpe_ratio(returns, n_trials=trial_sharpes.size, trial_sharpes=trial_sharpes)
    assert 0.0 <= dsr <= 1.0


# ---- PBO (CSCV) ------------------------------------------------------------

def test_pbo_zero_for_a_genuinely_dominant_config():
    # Config 0 beats the rest in every block => the IS winner is also OOS-best,
    # so it is never below the OOS median. PBO must be 0.
    rng = np.random.default_rng(4)
    t, n = 400, 8
    m = rng.normal(0.0, 0.01, (t, n))
    m[:, 0] += 0.02  # a persistent real edge
    assert probability_of_backtest_overfitting(m, n_blocks=8) == 0.0


def test_pbo_high_for_a_classic_overfit_generator():
    # Each config has a real edge in exactly ONE block and only noise elsewhere.
    # Whichever config wins in-sample spiked inside the IS blocks, so it has no
    # edge out-of-sample and lands below the OOS median => PBO near 1. This is
    # the textbook "looks great in the backtest, dead live" pattern.
    rng = np.random.default_rng(7)
    n_blocks, block = 8, 100
    n = n_blocks
    m = rng.normal(0.0, 0.01, (n_blocks * block, n))
    for c in range(n):
        m[c * block : (c + 1) * block, c] += 0.05 + 0.002 * c  # distinct spikes
    pbo = probability_of_backtest_overfitting(m, n_blocks=n_blocks)
    assert pbo > 0.5


def test_pbo_validates_shape_and_blocks():
    with pytest.raises(ValueError):
        probability_of_backtest_overfitting(np.zeros((100, 1)))  # need >=2 configs
    with pytest.raises(ValueError):
        probability_of_backtest_overfitting(np.zeros((100, 3)), n_blocks=7)  # odd


# ---- Combinatorial Purged CV ----------------------------------------------

def test_cpcv_split_count_and_no_overlap():
    splits = combinatorial_purged_splits(120, n_groups=6, n_test_groups=2)
    assert len(splits) == 15  # C(6, 2)
    for train, test in splits:
        assert set(train.tolist()).isdisjoint(set(test.tolist()))
        assert len(test) > 0 and len(train) > 0


def test_cpcv_purges_label_horizon_before_test():
    horizon = 5
    splits = combinatorial_purged_splits(
        120, n_groups=6, n_test_groups=1, embargo_pct=0.0, label_horizon=horizon
    )
    for train, test in splits:
        tstart = int(test.min())
        train_set = set(train.tolist())
        # The `horizon` samples immediately before the test block must be purged.
        for j in range(max(0, tstart - horizon), tstart):
            assert j not in train_set


def test_cpcv_embargoes_after_test():
    splits = combinatorial_purged_splits(
        200, n_groups=5, n_test_groups=1, embargo_pct=0.05, label_horizon=0
    )
    embargo = int(round(200 * 0.05))
    for train, test in splits:
        tend = int(test.max())
        train_set = set(train.tolist())
        for j in range(tend + 1, min(200, tend + 1 + embargo)):
            assert j not in train_set
