"""ML pipeline status — honest, data-gated readiness for the ML view."""

import json

from vnedge.ml.feature_matrix import FEATURE_COLUMNS
from vnedge.research.ml_pipeline_status import (
    MIN_LABELS_TO_TRAIN,
    build_ml_pipeline_status,
    main,
)


def test_empty_status_is_honest_and_collecting(tmp_path):
    status = build_ml_pipeline_status(lane_dir=tmp_path, data_root=tmp_path)
    assert status["stage"] == "COLLECTING_LABELS"
    assert status["dataset"]["samples"] == 0
    assert status["dataset"]["min_to_train"] == MIN_LABELS_TO_TRAIN
    assert status["can_trade"] is False and status["can_promote"] is False
    # the locked gates and the 6-stage pipeline are always present for the view
    assert status["gates"]["deflated_sharpe_min"] == 0.95
    assert status["gates"]["pbo_max"] == 0.20
    assert len(status["stages"]) == 6
    assert status["stages"][0]["key"] == "FOUNDATION" and status["stages"][0]["done"] is True
    # foundation reports the real (enriched) feature count
    assert status["foundation"]["feature_count"] == len(FEATURE_COLUMNS)
    assert status["active_role"] == "meta_labeling"


def test_main_writes_the_artifact(tmp_path):
    out = tmp_path / "ml_pipeline_status.json"
    rc = main([
        "--lane-dir", str(tmp_path),
        "--data-root", str(tmp_path),
        "--output", str(out),
        "--interval-seconds", "0",
    ])
    assert rc == 0
    payload = json.loads(out.read_text())
    assert payload["stage"] in {"COLLECTING_LABELS", "READY_TO_TRAIN"}
    assert "dataset" in payload and "gates" in payload and "stages" in payload
