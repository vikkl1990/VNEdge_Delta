from __future__ import annotations

import hashlib
import json

import joblib
import numpy as np
import pandas as pd
import pytest
from lightgbm import LGBMClassifier
from sklearn.preprocessing import StandardScaler

pytest.importorskip("shap")

from vnedge.research.delta_scalper_lightgbm_shap import (
    FEATURES,
    ApprovedBoosterArtifactError,
    _dependence_plots,
    _flat_grouped_attribution,
    _interaction_pair_plots,
    _interaction_summaries,
    aggregate_base_interactions,
    aggregate_base_shap,
    base_feature_name,
    load_approved_booster_artifacts,
    normalize_interaction_values,
    normalize_shap_values,
    strongest_interaction_partner,
)


def test_shap_transformed_features_map_to_causal_base_features():
    assert base_feature_name("numeric__expected_move_bps") == "expected_move_bps"
    assert base_feature_name("categorical__scanner_id_delta_imbalance_fade_v1") == ("scanner_id")
    assert base_feature_name("categorical__hour_utc_14") == "hour_utc"
    with pytest.raises(ValueError, match="cannot map"):
        base_feature_name("categorical__fabricated_feature_value")


def test_shap_one_hot_contributions_are_additively_grouped():
    values = np.asarray(
        [
            [0.2, 0.3, -0.1],
            [-0.2, 0.1, 0.4],
        ]
    )
    names = [
        "numeric__expected_move_bps",
        "categorical__scanner_id_delta_imbalance_fade_v1",
        "categorical__scanner_id_delta_momentum_burst_v1",
    ]

    grouped = aggregate_base_shap(values, names)

    assert grouped["expected_move_bps"].tolist() == [0.2, -0.2]
    assert grouped["scanner_id"].tolist() == pytest.approx([0.2, 0.5])
    assert grouped.sum(axis=1).tolist() == pytest.approx(values.sum(axis=1).tolist())


def test_binary_shap_values_are_normalized_across_library_shapes():
    positive = np.arange(6, dtype=float).reshape(2, 3)
    assert np.array_equal(
        normalize_shap_values([np.zeros_like(positive), positive], samples=2, features=3),
        positive,
    )
    sample_feature_class = np.stack((np.zeros_like(positive), positive), axis=-1)
    assert np.array_equal(
        normalize_shap_values(sample_feature_class, samples=2, features=3),
        positive,
    )
    class_sample_feature = np.stack((np.zeros_like(positive), positive), axis=0)
    assert np.array_equal(
        normalize_shap_values(class_sample_feature, samples=2, features=3),
        positive,
    )
    with pytest.raises(ValueError, match="unexpected SHAP value shape"):
        normalize_shap_values(np.zeros((4, 4)), samples=2, features=3)


def test_shap_interactions_are_normalized_and_additively_grouped():
    names = [
        "numeric__expected_move_bps",
        "categorical__scanner_id_delta_imbalance_fade_v1",
        "categorical__scanner_id_delta_momentum_burst_v1",
    ]
    raw = np.arange(18, dtype=float).reshape(2, 3, 3) / 100
    normalized = normalize_interaction_values(
        [np.zeros_like(raw), raw],
        samples=2,
        features=3,
    )
    grouped = aggregate_base_interactions(normalized, names)

    assert normalized.shape == (2, 3, 3)
    assert grouped.shape[0] == 2
    expected_move = list(FEATURES).index("expected_move_bps")
    scanner = list(FEATURES).index("scanner_id")
    assert grouped[:, expected_move, scanner].tolist() == pytest.approx(
        raw[:, 0, 1:].sum(axis=1).tolist()
    )
    assert grouped.sum(axis=(1, 2)).tolist() == pytest.approx(raw.sum(axis=(1, 2)).tolist())


def test_interaction_summaries_rank_pairs_and_preserve_manual_hypotheses():
    values = np.zeros((3, len(FEATURES), len(FEATURES)))
    scanner = list(FEATURES).index("scanner_id")
    cusum = list(FEATURES).index("change_point_window_at_entry")
    values[:, scanner, cusum] = [0.3, 0.2, 0.1]
    values[:, cusum, scanner] = [0.3, 0.2, 0.1]

    pairs, shares, matrix, hypotheses = _interaction_summaries(
        values,
        ["scanner_id", "change_point_window_at_entry", "expected_net_bps"],
    )

    assert pairs.iloc[0]["feature_a"] == "scanner_id"
    assert pairs.iloc[0]["feature_b"] == "change_point_window_at_entry"
    assert matrix.loc["scanner_id", "change_point_window_at_entry"] == pytest.approx(0.2)
    assert shares.loc[shares["feature"] == "scanner_id", "interaction_share"].iloc[
        0
    ] == pytest.approx(1.0)
    assert hypotheses[-1]["available"] is False


def test_dependence_plots_use_base_features_and_interaction_colours(tmp_path):
    rows = 20
    selection = pd.DataFrame(
        {
            "scanner_id": ["fade", "momentum"] * (rows // 2),
            "expected_net_bps": np.linspace(-4.0, 8.0, rows),
        }
    )
    base_shap = pd.DataFrame(0.0, index=range(rows), columns=FEATURES)
    base_shap["scanner_id"] = np.linspace(-0.2, 0.2, rows)
    base_shap["expected_net_bps"] = np.linspace(-0.3, 0.4, rows)
    importance = pd.DataFrame(
        {
            "feature": ["scanner_id", "expected_net_bps"],
            "mean_abs_shap": [0.2, 0.1],
        }
    )
    matrix = pd.DataFrame(0.0, index=FEATURES, columns=FEATURES)
    matrix.loc["scanner_id", "expected_net_bps"] = 0.2
    matrix.loc["expected_net_bps", "scanner_id"] = 0.2

    manifest = _dependence_plots(
        selection,
        base_shap,
        importance,
        matrix,
        tmp_path,
        top_count=2,
        sample_size=rows,
    )

    assert strongest_interaction_partner(matrix, "scanner_id") == "expected_net_bps"
    assert [row["feature_type"] for row in manifest] == ["categorical", "numeric"]
    assert [row["interaction_feature"] for row in manifest] == [
        "expected_net_bps",
        "scanner_id",
    ]
    assert all((tmp_path / row["path"]).is_file() for row in manifest)


def test_interaction_pair_plots_use_signed_base_pair_effects(tmp_path):
    rows = 18
    selection = pd.DataFrame(
        {
            "scanner_id": ["fade", "momentum"] * (rows // 2),
            "expected_net_bps": np.linspace(-5.0, 7.0, rows),
            "confidence": np.linspace(0.4, 0.9, rows),
        }
    )
    interactions = np.zeros((rows, len(FEATURES), len(FEATURES)))
    scanner = list(FEATURES).index("scanner_id")
    expected_net = list(FEATURES).index("expected_net_bps")
    confidence = list(FEATURES).index("confidence")
    interactions[:, scanner, expected_net] = np.linspace(-0.2, 0.3, rows)
    interactions[:, expected_net, scanner] = interactions[:, scanner, expected_net]
    interactions[:, expected_net, confidence] = np.linspace(-0.1, 0.1, rows)
    interactions[:, confidence, expected_net] = interactions[:, expected_net, confidence]
    pairs = pd.DataFrame(
        [
            {
                "feature_a": "scanner_id",
                "feature_b": "expected_net_bps",
                "mean_abs_interaction": 0.17,
                "mean_signed_interaction": 0.05,
            },
            {
                "feature_a": "expected_net_bps",
                "feature_b": "confidence",
                "mean_abs_interaction": 0.08,
                "mean_signed_interaction": 0.0,
            },
        ]
    )

    manifest = _interaction_pair_plots(
        selection,
        interactions,
        pairs,
        tmp_path,
        top_count=2,
    )

    assert [row["rank"] for row in manifest] == [1, 2]
    assert manifest[0]["x_feature"] == "expected_net_bps"
    assert manifest[0]["colour_feature"] == "scanner_id"
    assert manifest[0]["observations"] == rows
    assert all((tmp_path / row["path"]).is_file() for row in manifest)


def test_flat_grouped_shap_includes_realized_economics_and_sample_control():
    selection = pd.DataFrame(
        {
            "scanner_id": ["fade", "fade", "momentum"],
            "volatility_regime_at_entry": ["high", "high", "low"],
            "net_bps_resolved": [4.0, -2.0, -8.0],
        }
    )
    base_shap = pd.DataFrame(
        {
            "scanner_id": [0.2, 0.4, -0.1],
            "expected_net_bps": [0.1, -0.1, -0.2],
        }
    )

    grouped = _flat_grouped_attribution(
        selection,
        base_shap,
        np.asarray([0.7, 0.5, 0.2]),
        ("scanner_id", "volatility_regime_at_entry"),
        minimum_trades=2,
    )

    assert grouped["key"].tolist() == ["fade | high"]
    assert grouped.loc[0, "trades"] == 2
    assert grouped.loc[0, "average_net_bps"] == pytest.approx(1.0)
    assert grouped.loc[0, "profit_factor"] == pytest.approx(2.0)
    assert grouped.loc[0, "average_prediction_probability"] == pytest.approx(0.6)
    assert grouped.loc[0, "shap_scanner_id"] == pytest.approx(0.3)


def test_saved_booster_loader_requires_approved_complete_bundle(tmp_path):
    (tmp_path / "meta_config.json").write_text(
        json.dumps(
            {
                "approved_for_shap": False,
                "frozen_after_untouched_success": True,
                "live_integration_enabled": False,
            }
        )
    )
    with pytest.raises(ApprovedBoosterArtifactError, match="not approved"):
        load_approved_booster_artifacts(tmp_path)

    model = LGBMClassifier(n_estimators=2, verbosity=-1, random_state=42)
    x = np.asarray([[0.0], [1.0], [2.0], [3.0]])
    preprocessor = StandardScaler().fit(x)
    model.fit(preprocessor.transform(x), np.asarray([0, 0, 1, 1]))
    model.booster_.save_model(str(tmp_path / "meta_model_lgbm.txt"))
    joblib.dump(preprocessor, tmp_path / "meta_preprocessor.joblib")
    hashes = {}
    for key, name in (
        ("native_booster", "meta_model_lgbm.txt"),
        ("preprocessor", "meta_preprocessor.joblib"),
    ):
        hashes[key] = hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()
    (tmp_path / "meta_config.json").write_text(
        json.dumps(
            {
                "features": ["x"],
                "numeric_features": ["x"],
                "categorical_features": ["none"],
                "native_booster": "meta_model_lgbm.txt",
                "preprocessor": "meta_preprocessor.joblib",
                "sha256": hashes,
                "approved_for_shap": True,
                "frozen_after_untouched_success": True,
                "live_integration_enabled": False,
            }
        )
    )

    booster, preprocessor, config = load_approved_booster_artifacts(tmp_path)

    assert booster.num_trees() >= 1
    assert booster.predict(x).shape == (4,)
    assert preprocessor.transform(x).shape == (4, 1)
    assert config["approved_for_shap"] is True


def test_saved_booster_loader_reports_missing_corrupt_and_escaping_files(tmp_path):
    with pytest.raises(ApprovedBoosterArtifactError, match="missing approved"):
        load_approved_booster_artifacts(tmp_path)

    (tmp_path / "meta_config.json").write_text("{broken")
    with pytest.raises(ApprovedBoosterArtifactError, match="invalid JSON"):
        load_approved_booster_artifacts(tmp_path)

    (tmp_path / "meta_config.json").write_text(
        json.dumps(
            {
                "features": ["x"],
                "numeric_features": ["x"],
                "categorical_features": ["none"],
                "native_booster": "../outside.txt",
                "preprocessor": "meta_preprocessor.joblib",
                "sha256": {},
                "approved_for_shap": True,
                "frozen_after_untouched_success": True,
                "live_integration_enabled": False,
            }
        )
    )
    with pytest.raises(ApprovedBoosterArtifactError, match="escapes"):
        load_approved_booster_artifacts(tmp_path)
