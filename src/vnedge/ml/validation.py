"""Robust validation — the anti-overfit toolkit for ML promotion.

For a trading model, "high quality" is not a bigger network; it is a HONEST
out-of-sample claim. The last ML attempt overfit (IS +$1,343 vs OOS -$18.50)
and was correctly rejected by the IS/OOS collapse gate. This module makes that
rejection quantitative and systematic, so a model is promoted only when the
edge is measurably unlikely to be a selection artifact.

Every ML role (meta-labeling, regime, standalone direction) validates through
here BEFORE any pre-registered untouched-window judgment. Nothing here trades;
it only measures. References: Bailey & Lopez de Prado, "The Deflated Sharpe
Ratio" (2014); Bailey et al., "The Probability of Backtest Overfitting" (2015);
Lopez de Prado, "Advances in Financial Machine Learning" (2018).

Two independent guards, because they catch different failures:
  * DSR  — is a SINGLE strategy's Sharpe real, given how many configs we tried?
  * PBO  — across a FAMILY of configs, how often does the in-sample winner turn
           out below-median out-of-sample? (the definition of overfitting)
And CPCV gives many purged, embargoed OOS paths instead of one lucky split.
"""

from __future__ import annotations

import math
from itertools import combinations

import numpy as np
from scipy.stats import kurtosis, norm, skew

_EULER_MASCHERONI = 0.5772156649015329


def _sample_sharpe(returns: np.ndarray) -> float:
    """Per-observation Sharpe (NOT annualized). Std uses ddof=1."""
    sd = returns.std(ddof=1)
    if sd == 0.0:
        return 0.0
    return float(returns.mean() / sd)


def probabilistic_sharpe_ratio(returns, benchmark_sr: float = 0.0) -> float:
    """PSR: probability the true (per-observation) Sharpe exceeds `benchmark_sr`.

    Accounts for track-record length, skew and (non-excess) kurtosis — fat left
    tails and negative skew inflate a naive Sharpe, and PSR discounts them.
    Returns a probability in [0, 1]; ~0.5 when the observed Sharpe equals the
    benchmark. `benchmark_sr` is a per-observation Sharpe (0 for "better than
    noise"; the deflated benchmark for DSR).
    """
    r = np.asarray(returns, dtype=float)
    r = r[~np.isnan(r)]
    n = r.size
    if n < 3:
        raise ValueError("need >= 3 returns for PSR")
    sr = _sample_sharpe(r)
    g3 = float(skew(r))                    # sample skewness
    g4 = float(kurtosis(r, fisher=False))  # non-excess kurtosis (normal == 3)
    var_term = 1.0 - g3 * sr + ((g4 - 1.0) / 4.0) * sr * sr
    if var_term <= 0.0:
        # Degenerate SR-estimator variance — treat as no discriminating power.
        return 0.5
    z = (sr - benchmark_sr) * math.sqrt(n - 1) / math.sqrt(var_term)
    return float(norm.cdf(z))


def expected_max_sharpe(sr_variance: float, n_trials: int) -> float:
    """E[max Sharpe] under the null of ZERO true Sharpe across `n_trials`.

    The more independent configs you tried, the higher a Sharpe you expect from
    luck alone — this is that expected best-of-N, in per-observation units.
    `sr_variance` is the variance of the Sharpe estimates across the trials.
    """
    if n_trials < 1:
        raise ValueError("n_trials must be >= 1")
    if n_trials == 1 or sr_variance <= 0.0:
        return 0.0
    z1 = norm.ppf(1.0 - 1.0 / n_trials)
    z2 = norm.ppf(1.0 - 1.0 / (n_trials * math.e))
    return float(
        math.sqrt(sr_variance)
        * ((1.0 - _EULER_MASCHERONI) * z1 + _EULER_MASCHERONI * z2)
    )


def deflated_sharpe_ratio(
    returns,
    n_trials: int,
    *,
    sr_variance: float | None = None,
    trial_sharpes=None,
) -> float:
    """DSR: PSR against the multiple-testing–deflated benchmark.

    Provide the breadth of the search either as `sr_variance` (variance of the
    Sharpe estimates across all configs tried) or as `trial_sharpes` (the list
    of per-observation Sharpes, from which the variance is computed). A DSR
    below ~0.95 means the Sharpe is not convincingly real once the number of
    trials is accounted for.
    """
    if sr_variance is None:
        if trial_sharpes is None:
            raise ValueError("supply sr_variance or trial_sharpes")
        arr = np.asarray(trial_sharpes, dtype=float)
        if arr.size < 2:
            raise ValueError("need >= 2 trial_sharpes to estimate variance")
        sr_variance = float(arr.var(ddof=1))
    sr0 = expected_max_sharpe(sr_variance, n_trials)
    return probabilistic_sharpe_ratio(returns, benchmark_sr=sr0)


def probability_of_backtest_overfitting(perf_matrix, n_blocks: int = 16) -> float:
    """PBO via Combinatorially-Symmetric Cross-Validation (CSCV).

    `perf_matrix` is (T observations, N configs) of per-period performance (e.g.
    per-bar strategy returns for each config). Rows are split into `n_blocks`
    contiguous blocks; for every way to choose half the blocks as in-sample, the
    best-IS config's out-of-sample RANK is measured. PBO is the fraction of
    partitions where that in-sample winner lands below the OOS median — i.e. how
    often "best backtest" predicts "worse than average live". PBO near 0 is
    good; near 0.5 means the selection is noise.
    """
    m = np.asarray(perf_matrix, dtype=float)
    if m.ndim != 2:
        raise ValueError("perf_matrix must be 2-D (T, N)")
    t, n = m.shape
    if n < 2:
        raise ValueError("need >= 2 configs")
    if n_blocks < 2 or n_blocks % 2 != 0:
        raise ValueError("n_blocks must be even and >= 2")
    if t < n_blocks:
        raise ValueError("need at least n_blocks observations")

    blocks = np.array_split(np.arange(t), n_blocks)

    def _sharpe(cols: np.ndarray) -> np.ndarray:
        mean = cols.mean(axis=0)
        std = cols.std(axis=0, ddof=1)
        out = np.zeros_like(mean)
        nz = std > 0
        out[nz] = mean[nz] / std[nz]
        return out

    logits = []
    for is_combo in combinations(range(n_blocks), n_blocks // 2):
        is_set = set(is_combo)
        is_rows = np.concatenate([blocks[b] for b in range(n_blocks) if b in is_set])
        oos_rows = np.concatenate([blocks[b] for b in range(n_blocks) if b not in is_set])
        is_perf = _sharpe(m[is_rows])
        oos_perf = _sharpe(m[oos_rows])
        best = int(np.argmax(is_perf))
        # Rank of the IS winner among OOS configs (1 = worst, N = best).
        rank = int(np.argsort(np.argsort(oos_perf))[best]) + 1
        omega = rank / (n + 1.0)
        omega = min(max(omega, 1e-12), 1.0 - 1e-12)
        logits.append(math.log(omega / (1.0 - omega)))

    logits = np.asarray(logits, dtype=float)
    return float(np.mean(logits < 0.0))


def combinatorial_purged_splits(
    n_samples: int,
    *,
    n_groups: int = 6,
    n_test_groups: int = 2,
    embargo_pct: float = 0.01,
    label_horizon: int = 0,
):
    """Combinatorial Purged CV splits (train_idx, test_idx), leakage-safe.

    Time is cut into `n_groups` contiguous groups; every combination of
    `n_test_groups` of them forms a test fold (C(n_groups, n_test_groups) folds
    — far more OOS paths than a single split). For each fold the train set is
    PURGED of samples whose label window (`label_horizon` bars forward) would
    overlap a test group, and EMBARGOED for `embargo_pct` of the sample after
    each test group to kill serial-correlation leakage. Features must be causal;
    only forward-looking labels can leak, which is exactly what is purged.
    """
    if n_test_groups >= n_groups:
        raise ValueError("n_test_groups must be < n_groups")
    if label_horizon < 0 or embargo_pct < 0:
        raise ValueError("label_horizon and embargo_pct must be non-negative")

    idx = np.arange(n_samples)
    groups = np.array_split(idx, n_groups)
    embargo = int(round(n_samples * embargo_pct))

    splits = []
    for test_combo in combinations(range(n_groups), n_test_groups):
        test_idx = np.concatenate([groups[g] for g in test_combo])
        forbidden = set(int(i) for i in test_idx)
        for g in test_combo:
            gstart = int(groups[g][0])
            gend = int(groups[g][-1])
            # Purge train samples whose forward label window reaches the test group.
            for j in range(max(0, gstart - label_horizon), gstart):
                forbidden.add(j)
            # Embargo the samples immediately after the test group.
            for j in range(gend + 1, min(n_samples, gend + 1 + embargo)):
                forbidden.add(j)
        train_idx = np.fromiter(
            (i for i in range(n_samples) if i not in forbidden), dtype=int
        )
        splits.append((train_idx, np.sort(test_idx)))
    return splits
