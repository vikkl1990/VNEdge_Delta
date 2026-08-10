"""Meta-labeling harness: gated verdict over the existing validation primitives.

The harness must (a) refuse to fit below the label floor, (b) degrade honestly
between 'can fit' and 'can validate OOS', (c) PASS a genuinely predictive signal
through the locked gates, (d) FAIL pure noise, and (e) never emit inf/nan."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd

from vnedge.ml.feature_matrix import FEATURE_COLUMNS
from vnedge.ml.meta_labeler import MetaLabelReport, evaluate_meta_labeler


def _dataset(n: int, *, predictive: bool, seed: int = 11) -> pd.DataFrame:
    rng = np.random.RandomState(seed)
    df = pd.DataFrame({c: rng.normal(size=n) for c in FEATURE_COLUMNS})
    if predictive:
        z = df[FEATURE_COLUMNS[0]] * 2.6 + df[FEATURE_COLUMNS[1]] * 1.3 + df[FEATURE_COLUMNS[2]] * 0.9
        p = 1.0 / (1.0 + np.exp(-z))
        label = (rng.uniform(size=n) < p).astype(float)
    else:
        label = (rng.uniform(size=n) < 0.5).astype(float)
    df["meta_label"] = label
    df["net_usd"] = np.where(label > 0, rng.uniform(0.6, 1.4, n), -rng.uniform(0.6, 1.4, n))
    for col in ("strategy", "symbol", "side", "lane"):
        df[col] = "x"
    df["entry_ts"] = np.arange(n)  # time-ordered so PBO's in/out split is temporal
    return df


def _assert_json_safe(report: MetaLabelReport) -> None:
    json.dumps(report.to_dict(), allow_nan=False)  # raises on inf/nan
    assert report.can_trade is False and report.can_promote is False
    assert report.requires_untouched_judgment is True


def test_collecting_labels_below_floor():
    r = evaluate_meta_labeler(_dataset(120, predictive=True))
    assert r.status == "COLLECTING_LABELS" and not r.trainable and not r.passed
    assert r.metrics["progress_pct"] == 60.0
    _assert_json_safe(r)


def test_trainable_but_insufficient_for_cpcv():
    r = evaluate_meta_labeler(_dataset(250, predictive=True))
    assert r.status == "TRAINABLE_INSUFFICIENT_FOR_CPCV"
    assert r.trainable and not r.passed
    _assert_json_safe(r)


def test_single_class_is_not_trainable():
    df = _dataset(400, predictive=True)
    df["meta_label"] = 1.0  # all wins
    r = evaluate_meta_labeler(df)
    assert r.status == "SINGLE_CLASS" and not r.trainable and not r.passed
    _assert_json_safe(r)


def test_predictive_signal_passes_all_gates():
    r = evaluate_meta_labeler(_dataset(800, predictive=True))
    assert r.status == "VALIDATED_PASS" and r.passed is True
    assert r.beats_baseline is True
    assert {g.name for g in r.gates} == {
        "cpcv_median_pf", "deflated_sharpe", "pbo", "beats_baseline"
    }
    assert all(g.ok for g in r.gates)
    assert r.metrics["cpcv_median_pf"] >= 1.3
    assert r.metrics["pbo"] <= 0.20
    _assert_json_safe(r)


def test_pure_noise_fails_the_gates():
    r = evaluate_meta_labeler(_dataset(800, predictive=False))
    assert r.status == "VALIDATED_FAIL" and r.passed is False
    _assert_json_safe(r)


def test_pbo_discriminates_signal_from_noise():
    good = evaluate_meta_labeler(_dataset(800, predictive=True)).metrics["pbo"]
    bad = evaluate_meta_labeler(_dataset(800, predictive=False)).metrics["pbo"]
    # a real edge overfits far less than noise
    assert good < bad
