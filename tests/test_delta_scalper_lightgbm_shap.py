from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("shap")

from vnedge.research.delta_scalper_lightgbm_shap import (
    aggregate_base_shap,
    base_feature_name,
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
