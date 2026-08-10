from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("lightgbm")

from vnedge.research.delta_scalper_categorical_encoding import (
    UNKNOWN_CATEGORY,
    NativeCategoricalPreprocessor,
    run,
)
from vnedge.research.delta_scalper_lightgbm_meta import (
    CATEGORICAL_FEATURES,
    FEATURES,
    NUMERIC_FEATURES,
)


def _frame(rows: int = 1_000) -> pd.DataFrame:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    payload = []
    for index in range(rows):
        decision = start + timedelta(hours=index)
        label = int(index % 3 == 0)
        row = {
            "decision_ts": decision.isoformat(),
            "exit_ts": (decision + timedelta(minutes=20)).isoformat(),
            "tb_label": label,
            "net_bps_resolved": 8.0 if label else -10.0,
        }
        row.update({feature: float(index % 17) for feature in NUMERIC_FEATURES})
        row.update(
            {
                feature: f"bucket_{index % 3}"
                for feature in CATEGORICAL_FEATURES
                if feature != "hour_utc"
            }
        )
        payload.append(row)
    frame = pd.DataFrame(payload)
    frame["hour_utc"] = pd.to_datetime(frame["decision_ts"], utc=True).dt.strftime("%H")
    return frame


def test_native_preprocessor_freezes_categories_and_maps_unknowns():
    fit = _frame(100)
    later = fit.iloc[:3].copy()
    later.loc[later.index[0], "scanner_id"] = "never_seen"
    later.loc[later.index[1], "expected_move_bps"] = np.inf

    preprocessor = NativeCategoricalPreprocessor().fit(fit.loc[:, FEATURES])
    transformed = preprocessor.transform(later.loc[:, FEATURES])

    assert transformed.columns.tolist() == list(FEATURES)
    assert transformed["scanner_id"].dtype.name == "category"
    assert UNKNOWN_CATEGORY in transformed["scanner_id"].cat.categories
    assert transformed.loc[later.index[0], "scanner_id"] == UNKNOWN_CATEGORY
    assert np.isfinite(transformed.loc[:, NUMERIC_FEATURES].to_numpy()).all()


def test_encoding_comparison_never_scores_frozen_window(tmp_path):
    data = tmp_path / "labels.parquet"
    backtest = tmp_path / "backtest.json"
    output = tmp_path / "encoding.json"
    _frame().to_parquet(data, index=False)
    backtest.write_text(json.dumps({"summary": {"data_quality_pass": True}}))

    report = run(data, backtest, output)

    assert report["policy"]["selection_only_encoding_comparison"] is True
    assert report["policy"]["frozen_window_scored"] is False
    assert report["policy"]["deployable_artifacts_written"] is False
    assert report["can_trade"] is False
    assert report["can_promote"] is False
    assert set(report["encodings"]) == {"one_hot", "native"}
    assert report["encodings"]["native"]["transformed_features"] == len(FEATURES)
    assert output.exists()
