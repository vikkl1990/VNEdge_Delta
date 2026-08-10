"""Selection-only one-hot versus native LightGBM categorical comparison.

The experiment reuses the guarded meta-label dataset and chronological windows.
It compares encodings on the already-designated selection window and never
scores the protected final 20 percent. Results are diagnostic only: no model,
threshold, preprocessor, or promotion artifact is written.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import numpy as np
import pandas as pd
from lightgbm import early_stopping
from sklearn.metrics import brier_score_loss, roc_auc_score
from sklearn.preprocessing import RobustScaler

from vnedge.research.delta_scalper_lightgbm_meta import (
    CATEGORICAL_FEATURES,
    FEATURES,
    NUMERIC_FEATURES,
    PREREGISTERED_THRESHOLDS,
    build_model,
    build_preprocessor,
    chronological_windows,
    load_dataset,
    selection_gate,
    split_dataset,
    summarize,
)

DEFAULT_DATA = Path("research/live_research/delta_scalper_with_tb_labels.parquet")
DEFAULT_BACKTEST = Path("research/live_research/delta_scalper_backtest_latest.json")
DEFAULT_OUTPUT = Path("research/live_research/delta_scalper_categorical_encoding_latest.json")
MISSING_CATEGORY = "__MISSING__"
UNKNOWN_CATEGORY = "__UNKNOWN__"


@dataclass
class NativeCategoricalPreprocessor:
    """Fit-only numeric scaling and stable pandas categorical vocabularies."""

    scaler: RobustScaler = field(default_factory=RobustScaler)
    numeric_medians: pd.Series | None = None
    categories: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def fit(self, frame: pd.DataFrame) -> NativeCategoricalPreprocessor:
        numeric = self._numeric(frame)
        self.numeric_medians = numeric.median().fillna(0.0)
        self.scaler.fit(numeric.fillna(self.numeric_medians))
        self.categories = {}
        for name in CATEGORICAL_FEATURES:
            values = self._category_values(frame[name])
            learned = sorted(set(values.tolist()) - {UNKNOWN_CATEGORY})
            if MISSING_CATEGORY not in learned:
                learned.append(MISSING_CATEGORY)
            learned.append(UNKNOWN_CATEGORY)
            self.categories[name] = tuple(dict.fromkeys(learned))
        return self

    def transform(self, frame: pd.DataFrame) -> pd.DataFrame:
        if self.numeric_medians is None or not self.categories:
            raise RuntimeError("native categorical preprocessor is not fitted")
        numeric = self._numeric(frame).fillna(self.numeric_medians)
        scaled = pd.DataFrame(
            self.scaler.transform(numeric),
            columns=NUMERIC_FEATURES,
            index=frame.index,
        )
        output = scaled
        for name in CATEGORICAL_FEATURES:
            vocabulary = self.categories[name]
            known = set(vocabulary) - {UNKNOWN_CATEGORY}
            values = self._category_values(frame[name])
            values = values.where(values.isin(known), UNKNOWN_CATEGORY)
            output[name] = pd.Categorical(values, categories=vocabulary)
        return output.loc[:, FEATURES]

    def fit_transform(self, frame: pd.DataFrame) -> pd.DataFrame:
        return self.fit(frame).transform(frame)

    @staticmethod
    def _numeric(frame: pd.DataFrame) -> pd.DataFrame:
        return (
            frame.loc[:, NUMERIC_FEATURES]
            .apply(pd.to_numeric, errors="coerce")
            .replace([np.inf, -np.inf], np.nan)
        )

    @staticmethod
    def _category_values(series: pd.Series) -> pd.Series:
        return series.astype("string").fillna(MISSING_CATEGORY).astype(str)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def _source_quality(path: Path) -> bool:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return bool((payload.get("summary") or {}).get("data_quality_pass"))


def _classifier_metrics(labels: pd.Series, probability: np.ndarray) -> dict[str, Any]:
    target = labels.astype(int)
    return {
        "observations": len(target),
        "positive_label_rate": float(target.mean()),
        "roc_auc": (float(roc_auc_score(target, probability)) if target.nunique() > 1 else None),
        "brier_score": float(brier_score_loss(target, probability)),
        "average_probability": float(np.mean(probability)),
    }


def _rank(row: dict[str, Any]) -> tuple[float, float, float, float, int]:
    summary = row["selection"]
    reduction = float(summary.get("frequency_reduction_vs_baseline") or 0.0)
    adequate = int(summary["trades"]) >= 300 and 0.20 <= reduction <= 0.90
    return (
        float(row["selection_gate_pass"]),
        float(adequate),
        float(summary["profit_factor"] or 0.0),
        float(summary["average_net_bps"]),
        int(summary["trades"]),
    )


def _threshold_report(
    selection: pd.DataFrame,
    probability: np.ndarray,
    *,
    data_quality_pass: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    baseline = summarize(selection)
    evaluated: list[dict[str, Any]] = []
    for threshold in PREREGISTERED_THRESHOLDS:
        summary = summarize(selection, probability >= threshold)
        summary["frequency_reduction_vs_baseline"] = 1 - int(summary["trades"]) / max(
            1, int(baseline["trades"])
        )
        passed, reasons = selection_gate(
            summary,
            baseline,
            data_quality_pass=data_quality_pass,
        )
        evaluated.append(
            {
                "threshold": threshold,
                "selection": summary,
                "selection_gate_pass": passed,
                "selection_gate_reasons": reasons,
            }
        )
    return max(evaluated, key=_rank), sorted(evaluated, key=_rank, reverse=True)


def _category_diagnostics(
    selection: pd.DataFrame,
    probability: np.ndarray,
) -> dict[str, list[dict[str, Any]]]:
    working = selection.copy()
    working["_probability"] = probability
    output: dict[str, list[dict[str, Any]]] = {}
    for feature in CATEGORICAL_FEATURES:
        rows = []
        for value, group in working.groupby(feature, observed=True, dropna=False):
            net = group["net_bps_resolved"].astype(float)
            rows.append(
                {
                    "category": str(value),
                    "trades": len(group),
                    "average_probability": float(group["_probability"].mean()),
                    "positive_label_rate": float(group["tb_label"].astype(int).mean()),
                    "average_net_bps": float(net.mean()),
                }
            )
        output[feature] = sorted(
            rows,
            key=lambda row: (-int(row["trades"]), str(row["category"])),
        )
    return output


def _fit_encoding(
    name: str,
    fit: pd.DataFrame,
    early: pd.DataFrame,
    selection: pd.DataFrame,
    *,
    data_quality_pass: bool,
) -> dict[str, Any]:
    if name == "one_hot":
        preprocessor = build_preprocessor()
        x_fit = preprocessor.fit_transform(fit.loc[:, FEATURES])
        x_early = preprocessor.transform(early.loc[:, FEATURES])
        x_selection = preprocessor.transform(selection.loc[:, FEATURES])
        categorical_feature: list[str] | str = "auto"
        transformed_features = int(x_fit.shape[1])
        detail = "fit-only median and RobustScaler plus one-hot encoding"
    elif name == "native":
        preprocessor = NativeCategoricalPreprocessor()
        x_fit = preprocessor.fit_transform(fit.loc[:, FEATURES])
        x_early = preprocessor.transform(early.loc[:, FEATURES])
        x_selection = preprocessor.transform(selection.loc[:, FEATURES])
        categorical_feature = list(CATEGORICAL_FEATURES)
        transformed_features = len(FEATURES)
        detail = "fit-only median and RobustScaler plus stable pandas categories"
    else:
        raise ValueError(f"unsupported categorical encoding: {name}")

    model = build_model()
    model.fit(
        x_fit,
        fit["tb_label"].astype(int),
        eval_X=x_early,
        eval_y=early["tb_label"].astype(int),
        eval_metric="binary_logloss",
        categorical_feature=categorical_feature,
        callbacks=[early_stopping(stopping_rounds=40, verbose=False)],
    )
    fit_probability = model.predict_proba(x_fit)[:, 1]
    early_probability = model.predict_proba(x_early)[:, 1]
    selection_probability = model.predict_proba(x_selection)[:, 1]
    best, evaluated = _threshold_report(
        selection,
        selection_probability,
        data_quality_pass=data_quality_pass,
    )
    return {
        "encoding": name,
        "preprocessing": detail,
        "base_features": len(FEATURES),
        "transformed_features": transformed_features,
        "best_iteration": int(model.best_iteration_ or model.n_estimators),
        "fit_classifier": _classifier_metrics(fit["tb_label"], fit_probability),
        "early_stopping_classifier": _classifier_metrics(early["tb_label"], early_probability),
        "selection_classifier": _classifier_metrics(selection["tb_label"], selection_probability),
        "best_diagnostic_threshold": best,
        "evaluated_thresholds": evaluated,
        "per_category_selection": _category_diagnostics(selection, selection_probability),
    }


def run(
    data_path: Path = DEFAULT_DATA,
    backtest_path: Path = DEFAULT_BACKTEST,
    output_path: Path = DEFAULT_OUTPUT,
    *,
    embargo_minutes: int = 30,
) -> dict[str, Any]:
    frame = load_dataset(data_path)
    windows = chronological_windows(frame, embargo_minutes)
    fit, early, selection, frozen = split_dataset(frame, windows)
    quality = _source_quality(backtest_path)
    encodings = {
        name: _fit_encoding(
            name,
            fit,
            early,
            selection,
            data_quality_pass=quality,
        )
        for name in ("one_hot", "native")
    }
    one_hot = encodings["one_hot"]["selection_classifier"]
    native = encodings["native"]["selection_classifier"]
    payload: dict[str, Any] = {
        "report_id": "delta_scalper_categorical_encoding_v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "windows": windows.to_dict(),
        "embargo_minutes_each_boundary_side": embargo_minutes,
        "window_counts": {
            "fit": len(fit),
            "early_stopping": len(early),
            "selection": len(selection),
            "frozen_protected": len(frozen),
        },
        "source_data_quality_pass": quality,
        "encodings": encodings,
        "native_minus_one_hot": {
            "selection_roc_auc": (
                float(native["roc_auc"] - one_hot["roc_auc"])
                if native["roc_auc"] is not None and one_hot["roc_auc"] is not None
                else None
            ),
            "selection_brier_score": float(native["brier_score"] - one_hot["brier_score"]),
        },
        "policy": {
            "selection_only_encoding_comparison": True,
            "frozen_window_scored": False,
            "frozen_window_trades": len(frozen),
            "winner_not_promoted_from_same_selection_window": True,
            "target_encoding_used": False,
            "deployable_artifacts_written": False,
            "live_integration_enabled": False,
            "can_trade": False,
            "can_promote": False,
        },
        "can_trade": False,
        "can_promote": False,
    }
    _atomic_json(output_path, payload)
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--backtest", type=Path, default=DEFAULT_BACKTEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--embargo-minutes", type=int, default=30)
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.embargo_minutes < 0:
        raise ValueError("embargo minutes cannot be negative")
    report = run(
        args.data,
        args.backtest,
        args.output,
        embargo_minutes=args.embargo_minutes,
    )
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
