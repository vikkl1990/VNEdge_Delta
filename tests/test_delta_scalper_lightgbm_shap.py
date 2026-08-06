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
    ApprovedBoosterArtifactError,
    _flat_grouped_attribution,
    aggregate_base_shap,
    base_feature_name,
    load_approved_booster_artifacts,
)


def test_shap_transformed_features_map_to_causal_base_features():
    assert base_feature_name("numeric__expected_move_bps") == "expected_move_bps"
    assert base_feature_name("categorical__scanner_id_delta_imbalance_fade_v1") == (
        "scanner_id"
    )
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
