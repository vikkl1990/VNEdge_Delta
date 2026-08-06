from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("shap")

from vnedge.research.delta_scalper_lightgbm_shap import (
    _flat_grouped_attribution,
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
