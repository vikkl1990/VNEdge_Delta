"""Model robustness + stability — calibration, drift, and a fail-safe guard.

A model that was accurate at training time degrades silently as the market
drifts. For a trading system that is a capital risk, so this module makes
degradation observable and turns it into a SAFE action: fall back to the
rule-based signal. Three pieces, all pure measurement plus one policy:

  * Calibration — make a predicted probability MEAN what it says (0.7 => wins
    ~70% of the time), which is what makes probability-proportional sizing sane.
    Measured by Expected Calibration Error (ECE).
  * Drift — Population Stability Index (PSI) between the training feature
    distribution and live, per feature. The early-warning that the world the
    model learned has moved.
  * Health guard — combine staleness + drift + recent performance into a single
    explainable use_model / fall-back decision. Fails SAFE: any doubt => the
    rule-based signal, which the caller must never let block reduce-only exits.

Nothing here trades or sizes; it measures and decides eligibility. Sizing and
order placement stay in the gateway/position sizer.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# PSI interpretation thresholds (industry-standard credit-risk convention).
PSI_STABLE = 0.10        # < 0.10  : no meaningful shift
PSI_SIGNIFICANT = 0.25   # >= 0.25 : significant shift — investigate / retrain


def population_stability_index(
    reference, current, *, bins: int = 10, epsilon: float = 1e-6
) -> float:
    """PSI between a reference (training) and current sample of one feature.

    Quantile-bins the reference, applies the SAME edges to the current sample,
    and sums (cur% - ref%) * ln(cur% / ref%) over bins. ~0 means the
    distribution is unchanged; >= 0.25 is a significant shift. Robust to
    constant reference (returns 0.0 — nothing to drift from).
    """
    ref = np.asarray(reference, dtype=float)
    cur = np.asarray(current, dtype=float)
    ref = ref[~np.isnan(ref)]
    cur = cur[~np.isnan(cur)]
    if ref.size == 0 or cur.size == 0:
        raise ValueError("reference and current must be non-empty")

    quantiles = np.linspace(0.0, 1.0, bins + 1)
    edges = np.quantile(ref, quantiles)
    edges = np.unique(edges)
    if edges.size < 2:
        return 0.0  # constant reference — no distribution to drift from
    edges[0], edges[-1] = -np.inf, np.inf

    ref_counts = np.histogram(ref, bins=edges)[0].astype(float)
    cur_counts = np.histogram(cur, bins=edges)[0].astype(float)
    ref_pct = ref_counts / ref_counts.sum()
    cur_pct = cur_counts / cur_counts.sum()
    ref_pct = np.clip(ref_pct, epsilon, None)
    cur_pct = np.clip(cur_pct, epsilon, None)
    return float(np.sum((cur_pct - ref_pct) * np.log(cur_pct / ref_pct)))


def feature_drift_report(reference, current, *, bins: int = 10) -> dict:
    """Per-feature PSI + a worst-feature summary for two aligned feature frames.

    `reference` and `current` are dict-like or DataFrame-like column->array.
    Returns {feature: psi, ...} plus 'max_psi', 'worst_feature', and a
    'verdict' (STABLE / MODERATE / SIGNIFICANT).
    """
    ref_cols = _columns(reference)
    cur_cols = _columns(current)
    shared = [c for c in ref_cols if c in cur_cols]
    if not shared:
        raise ValueError("reference and current share no columns")
    psi = {c: population_stability_index(ref_cols[c], cur_cols[c], bins=bins) for c in shared}
    worst = max(psi, key=psi.get)
    max_psi = psi[worst]
    verdict = (
        "SIGNIFICANT" if max_psi >= PSI_SIGNIFICANT
        else "MODERATE" if max_psi >= PSI_STABLE
        else "STABLE"
    )
    return {"psi": psi, "max_psi": max_psi, "worst_feature": worst, "verdict": verdict}


def expected_calibration_error(y_true, p_pred, *, bins: int = 10) -> float:
    """ECE: mean gap between predicted probability and realized frequency.

    Bins predictions into `bins` equal-width probability buckets and averages
    |mean(y_true) - mean(p_pred)| weighted by bucket size. 0 = perfectly
    calibrated. A well-calibrated model is what lets you size by probability.
    """
    y = np.asarray(y_true, dtype=float)
    p = np.asarray(p_pred, dtype=float)
    if y.shape != p.shape or y.size == 0:
        raise ValueError("y_true and p_pred must be same non-empty shape")
    edges = np.linspace(0.0, 1.0, bins + 1)
    idx = np.clip(np.digitize(p, edges[1:-1]), 0, bins - 1)
    ece = 0.0
    n = y.size
    for b in range(bins):
        mask = idx == b
        if not mask.any():
            continue
        conf = p[mask].mean()
        acc = y[mask].mean()
        ece += (mask.sum() / n) * abs(acc - conf)
    return float(ece)


class ProbabilityCalibrator:
    """Post-hoc probability calibration (isotonic by default; Platt available).

    Fit on a held-out (raw_prob, outcome) set — NEVER the training fold, or the
    calibration itself leaks. transform() maps raw model probabilities to
    calibrated ones. Kept as a tiny wrapper so it can be versioned in the
    model registry alongside the model it calibrates.
    """

    def __init__(self, method: str = "isotonic") -> None:
        if method not in ("isotonic", "platt"):
            raise ValueError("method must be 'isotonic' or 'platt'")
        self.method = method
        self._model = None

    def fit(self, p_raw, y_true) -> "ProbabilityCalibrator":
        p = np.asarray(p_raw, dtype=float)
        y = np.asarray(y_true, dtype=float)
        if self.method == "isotonic":
            from sklearn.isotonic import IsotonicRegression

            self._model = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
            self._model.fit(p, y)
        else:
            from sklearn.linear_model import LogisticRegression

            self._model = LogisticRegression(C=1e6, solver="lbfgs")
            self._model.fit(p.reshape(-1, 1), y.astype(int))
        return self

    def transform(self, p_raw) -> np.ndarray:
        if self._model is None:
            raise RuntimeError("calibrator is not fitted")
        p = np.asarray(p_raw, dtype=float)
        if self.method == "isotonic":
            return np.clip(self._model.predict(p), 0.0, 1.0)
        return self._model.predict_proba(p.reshape(-1, 1))[:, 1]


@dataclass(frozen=True)
class ModelHealth:
    """Explainable model-eligibility verdict. use_model=False => fall back."""

    fresh: bool
    drift_ok: bool
    performance_ok: bool
    reasons: tuple[str, ...]

    @property
    def use_model(self) -> bool:
        return self.fresh and self.drift_ok and self.performance_ok


def assess_model_health(
    *,
    model_age_seconds: float,
    max_age_seconds: float,
    drift_psi: float,
    max_drift_psi: float = PSI_SIGNIFICANT,
    recent_profit_factor: float | None,
    min_profit_factor: float = 1.0,
    min_recent_trades: int = 0,
    recent_trades: int = 0,
) -> ModelHealth:
    """Fail-safe eligibility: any doubt => don't use the model.

    A model is eligible only if it is fresh, its live features have not
    significantly drifted, and its recent realized performance holds up. On
    ``use_model=False`` the caller uses the rule-based signal instead — and must
    NEVER let this gate block a reduce-only exit (exits ignore model health).
    """
    reasons: list[str] = []
    fresh = model_age_seconds <= max_age_seconds
    if not fresh:
        reasons.append(f"stale: age {model_age_seconds:.0f}s > {max_age_seconds:.0f}s")

    drift_ok = drift_psi < max_drift_psi
    if not drift_ok:
        reasons.append(f"drift: PSI {drift_psi:.3f} >= {max_drift_psi:.3f}")

    # Not enough recent evidence is treated as "healthy" ONLY if a floor was not
    # requested; if a floor is set and unmet, we cannot confirm health => block.
    if recent_trades < min_recent_trades:
        performance_ok = False
        reasons.append(f"insufficient recent trades: {recent_trades} < {min_recent_trades}")
    elif recent_profit_factor is None:
        performance_ok = True  # no PF signal yet and no floor breached
    else:
        performance_ok = recent_profit_factor >= min_profit_factor
        if not performance_ok:
            reasons.append(
                f"decay: PF {recent_profit_factor:.2f} < {min_profit_factor:.2f}"
            )

    if not reasons:
        reasons.append("healthy")
    return ModelHealth(fresh, drift_ok, performance_ok, tuple(reasons))


def _columns(frame) -> dict:
    """Accept a DataFrame-like or a plain dict of column -> array."""
    if hasattr(frame, "columns") and hasattr(frame, "__getitem__"):
        return {c: np.asarray(frame[c], dtype=float) for c in frame.columns}
    if isinstance(frame, dict):
        return {c: np.asarray(v, dtype=float) for c, v in frame.items()}
    raise TypeError("expected a DataFrame-like or dict of columns")
