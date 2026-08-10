"""Model training + explainability.

scikit-learn HistGradientBoostingClassifier instead of XGBoost — a deliberate
deviation: XGBoost requires a libomp runtime on macOS, and install fragility
in a trading system is a real cost. The interface below is model-agnostic;
any classifier with fit/predict_proba drops in (including XGBoost later).

Explainability via permutation importance: which features, when shuffled,
actually hurt the model. Slower than tree importances but model-agnostic and
harder to fool.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

DEFAULT_MODEL_PARAMS: dict = {
    "max_depth": 4,
    "learning_rate": 0.08,
    "max_iter": 200,
    "min_samples_leaf": 50,
    "l2_regularization": 1.0,
    "random_state": 7,  # determinism is a project rule, not an option
}


@dataclass(frozen=True)
class TrainedModel:
    model: object
    feature_names: tuple[str, ...]
    params: dict
    train_rows: int
    positive_rate: float
    importances: tuple[tuple[str, float], ...] = field(default_factory=tuple)

    def predict_proba_up(self, X: pd.DataFrame) -> np.ndarray:
        return self.model.predict_proba(X[list(self.feature_names)])[:, 1]


def train_classifier(
    X: pd.DataFrame,
    y: pd.Series,
    params: dict | None = None,
    *,
    compute_importances: bool = True,
) -> TrainedModel:
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.inspection import permutation_importance

    if len(X) != len(y):
        raise ValueError("X and y length mismatch")
    if y.isna().any() or X.isna().any().any():
        raise ValueError("training data contains NaN — purge warmup/label rows first")
    if len(X) < 200:
        raise ValueError(f"only {len(X)} training rows — refusing to fit noise")
    if y.nunique() < 2:
        raise ValueError("labels are single-class — nothing to learn")

    merged = dict(DEFAULT_MODEL_PARAMS)
    merged.update(params or {})
    model = HistGradientBoostingClassifier(**merged)
    model.fit(X, y)

    importances: tuple[tuple[str, float], ...] = ()
    if compute_importances:
        # tail subsample keeps this cheap inside walk-forward loops
        tail = min(len(X), 1500)
        result = permutation_importance(
            model, X.iloc[-tail:], y.iloc[-tail:], n_repeats=3, random_state=7
        )
        ranked = sorted(
            zip(X.columns, result.importances_mean), key=lambda kv: -kv[1]
        )
        importances = tuple((name, float(score)) for name, score in ranked)

    return TrainedModel(
        model=model,
        feature_names=tuple(X.columns),
        params=merged,
        train_rows=len(X),
        positive_rate=float(y.mean()),
        importances=importances,
    )
