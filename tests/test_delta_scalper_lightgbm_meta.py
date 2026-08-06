from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

pytest.importorskip("lightgbm")

from vnedge.research.delta_scalper_lightgbm_meta import (
    CATEGORICAL_FEATURES,
    NUMERIC_FEATURES,
    PREREGISTERED_THRESHOLDS,
    chronological_windows,
    load_dataset,
    selection_gate,
    split_dataset,
)


def _frame(rows: int = 1_000) -> pd.DataFrame:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    payload = []
    for index in range(rows):
        decision = start + timedelta(hours=index)
        row = {
            "decision_ts": decision.isoformat(),
            "exit_ts": (decision + timedelta(minutes=20)).isoformat(),
            "tb_label": index % 3 == 0,
            "net_bps_resolved": 8.0 if index % 3 == 0 else -10.0,
        }
        row.update({feature: float(index % 17) for feature in NUMERIC_FEATURES})
        row.update(
            {
                feature: "bucket"
                for feature in CATEGORICAL_FEATURES
                if feature != "hour_utc"
            }
        )
        payload.append(row)
    return pd.DataFrame(payload)


def test_lightgbm_dataset_requires_real_causal_features(tmp_path):
    path = tmp_path / "dataset.parquet"
    _frame().to_parquet(path, index=False)

    loaded = load_dataset(path)

    assert loaded["decision_ts"].is_monotonic_increasing
    assert loaded.loc[0, "hour_utc"] == "00"
    broken = _frame().drop(columns="expected_move_bps")
    broken.to_parquet(path, index=False)
    with pytest.raises(ValueError, match="expected_move_bps"):
        load_dataset(path)


def test_lightgbm_windows_are_disjoint_and_frozen_is_last_twenty_percent():
    frame = _frame()
    frame["decision_ts"] = pd.to_datetime(frame["decision_ts"], utc=True)
    frame["exit_ts"] = pd.to_datetime(frame["exit_ts"], utc=True)
    windows = chronological_windows(frame, embargo_minutes=30)
    fit, early, selection, frozen = split_dataset(frame, windows)

    assert fit["exit_ts"].max().to_pydatetime() < windows.fit_end
    assert early["decision_ts"].min().to_pydatetime() >= windows.early_start
    assert selection["decision_ts"].min().to_pydatetime() >= windows.selection_start
    assert frozen["decision_ts"].min().to_pydatetime() >= windows.frozen_start
    assert 0.18 <= len(frozen) / len(frame) <= 0.21


def test_lightgbm_thresholds_and_data_quality_gate_are_preregistered():
    assert PREREGISTERED_THRESHOLDS[0] == 0.45
    assert PREREGISTERED_THRESHOLDS[-1] == 0.875
    assert len(PREREGISTERED_THRESHOLDS) == 18
    summary = {
        "trades": 400,
        "profit_factor": 1.5,
        "average_net_bps": 3.0,
        "positive_markets": 2,
    }
    baseline = {"trades": 1_000}
    passed, reasons = selection_gate(summary, baseline, data_quality_pass=False)
    assert passed is False
    assert reasons == ["source_data_quality_failed"]
