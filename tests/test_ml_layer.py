"""ML layer — causality, labels, embargo, trainer guards, registry, wrapper."""

import numpy as np
import pandas as pd
import pytest

from vnedge.data.schemas import normalize_candles
from vnedge.ml.feature_matrix import FEATURE_COLUMNS, FeatureParams, build_feature_matrix
from vnedge.ml.labels import triple_barrier_labels
from vnedge.ml.ml_strategy import MLStrategy
from vnedge.ml.model_registry import ModelRegistry
from vnedge.ml.trainer import TrainedModel, train_classifier
from vnedge.strategy.regime import RegimeParams

BASE = 1_750_000_000_000
HOUR = 3_600_000

SMALL = FeatureParams(
    regime=RegimeParams(ema_fast=6, ema_slow=24, er_window=12,
                        atr_window=6, atr_pct_window=48),
    funding_pct_window=48, z_window=24,
)


def wavy_candles(n: int) -> pd.DataFrame:
    raw, price = [], 100.0
    for i in range(n):
        drift = 0.004 if (i // 30) % 2 == 0 else -0.003
        new = price * (1 + drift + 0.001 * ((i * 7919) % 7 - 3) / 3)
        raw.append([BASE + i * HOUR, price, max(price, new) * 1.004,
                    min(price, new) * 0.996, new, 10.0 + (i % 5)])
        price = new
    return normalize_candles(raw)


def test_features_are_causal():
    """Mutating FUTURE bars must not change PAST feature rows."""
    candles = wavy_candles(400)
    before = build_feature_matrix(candles, None, SMALL)

    tampered = candles.copy()
    tampered.loc[tampered.index > 300, ["open", "high", "low", "close", "volume"]] *= 7.0
    after = build_feature_matrix(tampered, None, SMALL)

    pd.testing.assert_series_equal(
        before.loc[300, FEATURE_COLUMNS].astype(float),
        after.loc[300, FEATURE_COLUMNS].astype(float),
        check_names=False,
    )


def test_labels_target_hit_first():
    # flat, then a clean rally: long barrier trade wins
    rows = [[BASE + i * HOUR, 100.0, 100.6, 99.4, 100.0, 5.0] for i in range(30)]
    for i in range(30, 48):
        price = 100.0 + (i - 29) * 2.0
        rows.append([BASE + i * HOUR, price - 2.0, price + 0.5, price - 2.5, price, 5.0])
    candles = normalize_candles(rows)
    labels = triple_barrier_labels(candles, stop_atr_mult=2.0, target_r=2.0,
                                   horizon_bars=12, atr_window=6)
    assert labels.iloc[29] == 1.0  # rally begins right after bar 29


def test_labels_stop_hit_is_zero_and_stop_wins_ties():
    rows = [[BASE + i * HOUR, 100.0, 100.6, 99.4, 100.0, 5.0] for i in range(30)]
    # one violent bar that spans BOTH barriers -> stop-first rule -> 0
    rows.append([BASE + 30 * HOUR, 100.0, 130.0, 70.0, 100.0, 5.0])
    rows += [[BASE + i * HOUR, 100.0, 100.6, 99.4, 100.0, 5.0] for i in range(31, 45)]
    candles = normalize_candles(rows)
    labels = triple_barrier_labels(candles, horizon_bars=12, atr_window=6)
    assert labels.iloc[29] == 0.0


def test_labels_tail_is_nan():
    candles = wavy_candles(60)
    labels = triple_barrier_labels(candles, horizon_bars=12, atr_window=6)
    assert labels.iloc[-12:].isna().all()


def test_trainer_learns_separable_pattern():
    rng = np.random.default_rng(7)
    X = pd.DataFrame({"a": rng.normal(size=1000), "b": rng.normal(size=1000)})
    y = (X["a"] > 0).astype(float)
    trained = train_classifier(X, y)
    proba = trained.predict_proba_up(X)
    accuracy = ((proba > 0.5).astype(float) == y).mean()
    assert accuracy > 0.95
    assert trained.importances[0][0] == "a"  # explainability names the true driver


def test_trainer_refuses_bad_data():
    X = pd.DataFrame({"a": [1.0, np.nan] * 200})
    y = pd.Series([0.0, 1.0] * 200)
    with pytest.raises(ValueError, match="NaN"):
        train_classifier(X, y)
    with pytest.raises(ValueError, match="refusing to fit noise"):
        train_classifier(pd.DataFrame({"a": [1.0] * 50}), pd.Series([0.0, 1.0] * 25))
    with pytest.raises(ValueError, match="single-class"):
        train_classifier(pd.DataFrame({"a": [1.0] * 300}), pd.Series([1.0] * 300))


def test_registry_roundtrip(tmp_path):
    rng = np.random.default_rng(7)
    X = pd.DataFrame({"a": rng.normal(size=500), "b": rng.normal(size=500)})
    y = (X["a"] > 0).astype(float)
    trained = train_classifier(X, y, compute_importances=False)

    registry = ModelRegistry(tmp_path)
    version = registry.save(trained, {"note": "test"})
    assert version in registry.list_versions()

    loaded, meta = registry.load(version)
    assert meta["feature_names"] == ["a", "b"]
    np.testing.assert_allclose(
        loaded.predict_proba_up(X[:10]), trained.predict_proba_up(X[:10])
    )


class StubModel:
    """predict_proba controlled by the 'close_z' feature sign."""

    def predict_proba(self, X):
        p = np.where(X["close_z"].to_numpy() > 0, 0.9, 0.1)
        return np.column_stack([1 - p, p])


def stub_trained() -> TrainedModel:
    return TrainedModel(
        model=StubModel(), feature_names=tuple(FEATURE_COLUMNS),
        params={}, train_rows=1000, positive_rate=0.4,
        importances=(("close_z", 0.5), ("er", 0.1)),
    )


def test_ml_strategy_threshold_and_stop():
    candles = wavy_candles(300)
    strategy = MLStrategy(stub_trained(), None, model_version="stub",
                          threshold=0.6, features=SMALL)
    df = strategy.prepare(candles)
    fired = rejected = 0
    for i in range(strategy.warmup_bars, len(df)):
        intent = strategy.signal(df, i)
        p = df["p_up"].iloc[i]
        if intent is not None:
            fired += 1
            assert p >= 0.6
            assert intent.side == "long"
            assert intent.stop_price < float(df["close"].iloc[i])
            assert "p_up=" in intent.reason and "stub" in intent.reason
        elif not np.isnan(p):
            rejected += 1
            assert p < 0.6
    assert fired > 0 and rejected > 0  # both branches exercised


def test_ml_strategy_rejects_coinflip_threshold():
    with pytest.raises(ValueError, match="coin-flip"):
        MLStrategy(stub_trained(), None, threshold=0.4)


def test_walk_forward_ml_runs_with_embargo():
    from vnedge.backtest.backtester import BacktestConfig
    from vnedge.ml.walk_forward_ml import MLRoundConfig, walk_forward_ml

    candles = wavy_candles(900)
    # tight barriers (1 ATR each way) so the synthetic market produces BOTH
    # label classes — wide barriers on gentle waves are all horizon-expiry
    cfg = MLRoundConfig(
        horizon_bars=8, threshold=0.55, train_bars=400, test_bars=150,
        stop_atr_mult=1.0, target_r=1.0,
        features=SMALL, model_params={"max_iter": 30, "min_samples_leaf": 20},
    )
    result = walk_forward_ml(candles, None, cfg, BacktestConfig())
    assert len(result.windows) == 3  # (900-550)/150 -> starts 0,150,300
    for w in result.windows:
        assert w.test_start > w.train_start
        assert "positive_rate" in w.chosen_params
