"""Chronological LightGBM meta-label research on Route A outcomes.

This is a diagnostic trade filter, not an execution backtest. The model-fit,
early-stopping, and threshold-selection windows are disjoint. The frozen final
20 percent is opened only when every preregistered selection gate passes.
Deployable artifacts are written only after frozen-window success.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import joblib
import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier, early_stopping
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.metrics import brier_score_loss, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, RobustScaler

DEFAULT_DATA = Path("research/live_research/delta_scalper_with_tb_labels.parquet")
DEFAULT_BACKTEST = Path("research/live_research/delta_scalper_backtest_latest.json")
DEFAULT_OUTPUT = Path(
    "research/live_research/delta_scalper_lightgbm_meta_latest.json"
)
DEFAULT_ARTIFACT_DIR = Path("research/meta_labeling_lightgbm")

NUMERIC_FEATURES = (
    "expected_move_bps",
    "expected_net_bps",
    "scalper_probability",
    "confidence",
    "expected_fee_multiple",
    "atr_percentile_at_entry",
    "atr_bps_at_entry",
    "bb_width_percentile_at_entry",
    "planned_stop_bps",
    "planned_target_bps",
    "change_point_bars_since_shift",
    "change_point_return_score",
    "change_point_volatility_score",
    "cusum_alarm_recent_at_entry",
)
CATEGORICAL_FEATURES = (
    "scanner_id",
    "symbol",
    "side",
    "regime_at_entry",
    "trend_regime_at_entry",
    "trend_direction_at_entry",
    "volatility_regime_at_entry",
    "session_regime_at_entry",
    "change_point_window_at_entry",
    "hour_utc",
)
FEATURES = (*NUMERIC_FEATURES, *CATEGORICAL_FEATURES)
PREREGISTERED_THRESHOLDS = tuple(round(0.45 + 0.025 * index, 3) for index in range(18))


@dataclass(frozen=True)
class Windows:
    fit_end: datetime
    early_start: datetime
    early_end: datetime
    selection_start: datetime
    selection_end: datetime
    frozen_start: datetime
    end: datetime

    def to_dict(self) -> dict[str, str]:
        return {key: value.isoformat() for key, value in self.__dict__.items()}


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_backtest_quality(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    summary = payload.get("summary") or {}
    window = payload.get("window") or {}
    return {
        "data_quality_pass": bool(summary.get("data_quality_pass")),
        "window": window,
        "baseline_trades": int(summary.get("trades") or 0),
    }


def load_dataset(path: Path) -> pd.DataFrame:
    frame = pd.read_parquet(path)
    required = {
        "decision_ts",
        "exit_ts",
        "tb_label",
        "net_bps_resolved",
        *NUMERIC_FEATURES,
        *(feature for feature in CATEGORICAL_FEATURES if feature != "hour_utc"),
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"LightGBM dataset missing causal features: {missing}")
    frame = frame.copy()
    frame["decision_ts"] = pd.to_datetime(frame["decision_ts"], utc=True)
    frame["exit_ts"] = pd.to_datetime(frame["exit_ts"], utc=True)
    frame["hour_utc"] = frame["decision_ts"].dt.strftime("%H")
    return frame.sort_values(["decision_ts", "exit_ts"]).reset_index(drop=True)


def chronological_windows(frame: pd.DataFrame, embargo_minutes: int) -> Windows:
    start = frame["decision_ts"].min().to_pydatetime()
    end = frame["decision_ts"].max().to_pydatetime() + timedelta(microseconds=1)
    span = end - start
    first = start + span * 0.60
    second = start + span * 0.70
    third = start + span * 0.80
    embargo = timedelta(minutes=embargo_minutes)
    return Windows(
        fit_end=first - embargo,
        early_start=first + embargo,
        early_end=second - embargo,
        selection_start=second + embargo,
        selection_end=third - embargo,
        frozen_start=third + embargo,
        end=end,
    )


def split_dataset(
    frame: pd.DataFrame,
    windows: Windows,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    fit = frame.loc[frame["exit_ts"] < windows.fit_end].copy()
    early = frame.loc[
        (frame["decision_ts"] >= windows.early_start)
        & (frame["exit_ts"] < windows.early_end)
    ].copy()
    selection = frame.loc[
        (frame["decision_ts"] >= windows.selection_start)
        & (frame["exit_ts"] < windows.selection_end)
    ].copy()
    frozen = frame.loc[frame["decision_ts"] >= windows.frozen_start].copy()
    if min(map(len, (fit, early, selection, frozen))) == 0:
        raise ValueError("chronological split produced an empty window")
    return fit, early, selection, frozen


def build_preprocessor() -> ColumnTransformer:
    return ColumnTransformer(
        (
            (
                "numeric",
                Pipeline(
                    (
                        ("impute", SimpleImputer(strategy="median")),
                        ("scale", RobustScaler()),
                    )
                ),
                list(NUMERIC_FEATURES),
            ),
            (
                "categorical",
                Pipeline(
                    (
                        ("impute", SimpleImputer(strategy="most_frequent")),
                        ("encode", OneHotEncoder(handle_unknown="ignore")),
                    )
                ),
                list(CATEGORICAL_FEATURES),
            ),
        )
    )


def build_model() -> LGBMClassifier:
    return LGBMClassifier(
        n_estimators=500,
        learning_rate=0.05,
        max_depth=4,
        num_leaves=16,
        min_child_samples=40,
        subsample=0.8,
        colsample_bytree=0.8,
        reg_lambda=1.0,
        random_state=42,
        n_jobs=-1,
        verbosity=-1,
    )


def profit_factor(net: pd.Series) -> float | None:
    gains = float(net.loc[net > 0].sum())
    losses = float(-net.loc[net < 0].sum())
    return gains / losses if losses else None


def summarize(frame: pd.DataFrame, mask: np.ndarray | pd.Series | None = None) -> dict:
    selected = frame if mask is None else frame.loc[np.asarray(mask)].copy()
    net = selected["net_bps_resolved"].astype(float)
    market_breakdown = {}
    for symbol in sorted(frame["symbol"].unique()):
        market = selected.loc[selected["symbol"] == symbol]
        values = market["net_bps_resolved"].astype(float)
        market_breakdown[str(symbol)] = {
            "trades": len(market),
            "net_bps": float(values.sum()),
            "average_net_bps": float(values.mean()) if len(values) else 0.0,
            "profit_factor": profit_factor(values),
        }
    duration_days = max(
        1 / 1_440,
        (frame["decision_ts"].max() - frame["decision_ts"].min()).total_seconds()
        / 86_400,
    )
    return {
        "trades": len(selected),
        "net_bps": float(net.sum()),
        "average_net_bps": float(net.mean()) if len(net) else 0.0,
        "profit_factor": profit_factor(net),
        "false_signal_rate": float((net <= 0).mean()) if len(net) else 0.0,
        "trades_per_day": len(selected) / duration_days,
        "positive_markets": sum(
            row["net_bps"] > 0 for row in market_breakdown.values()
        ),
        "market_breakdown": market_breakdown,
    }


def selection_gate(
    summary: dict,
    baseline: dict,
    *,
    data_quality_pass: bool,
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if not data_quality_pass:
        reasons.append("source_data_quality_failed")
    if int(summary["trades"]) < 300:
        reasons.append("fewer_than_300_selection_trades")
    reduction = 1 - int(summary["trades"]) / max(1, int(baseline["trades"]))
    if not 0.20 <= reduction <= 0.90:
        reasons.append("frequency_reduction_outside_20_90_pct")
    if summary["profit_factor"] is None or float(summary["profit_factor"]) < 1.10:
        reasons.append("selection_profit_factor_below_1_10")
    if float(summary["average_net_bps"]) < 1.0:
        reasons.append("selection_average_net_below_1_bps")
    if int(summary["positive_markets"]) < 1:
        reasons.append("no_positive_selection_market")
    return not reasons, reasons


def _rank(row: dict) -> tuple[float, float, float, float, int]:
    summary = row["selection"]
    reduction = float(summary.get("frequency_reduction_vs_baseline") or 0.0)
    adequate_sample = int(summary["trades"]) >= 300 and 0.20 <= reduction <= 0.90
    return (
        float(row["selection_gate_pass"]),
        float(adequate_sample),
        float(summary["profit_factor"] or 0.0),
        float(summary["average_net_bps"]),
        int(summary["trades"]),
    )


def run(args: argparse.Namespace) -> dict[str, Any]:
    frame = load_dataset(args.data)
    quality = _load_backtest_quality(args.backtest)
    windows = chronological_windows(frame, args.embargo_minutes)
    fit, early, selection, frozen_rows = split_dataset(frame, windows)
    preprocessor = build_preprocessor()
    x_fit = preprocessor.fit_transform(fit.loc[:, FEATURES])
    x_early = preprocessor.transform(early.loc[:, FEATURES])
    model = build_model()
    model.fit(
        x_fit,
        fit[args.label_col].astype(int),
        eval_X=x_early,
        eval_y=early[args.label_col].astype(int),
        eval_metric="binary_logloss",
        callbacks=[early_stopping(stopping_rounds=40, verbose=False)],
    )
    x_selection = preprocessor.transform(selection.loc[:, FEATURES])
    selection_probability = model.predict_proba(x_selection)[:, 1]
    baseline = summarize(selection)
    evaluated = []
    for threshold in PREREGISTERED_THRESHOLDS:
        summary = summarize(selection, selection_probability >= threshold)
        summary["frequency_reduction_vs_baseline"] = 1 - int(summary["trades"]) / max(
            1, int(baseline["trades"])
        )
        passed, reasons = selection_gate(
            summary,
            baseline,
            data_quality_pass=quality["data_quality_pass"],
        )
        evaluated.append(
            {
                "threshold": threshold,
                "selection": summary,
                "selection_gate_pass": passed,
                "selection_gate_reasons": reasons,
            }
        )
    best = max(evaluated, key=_rank)
    statistically_adequate = [
        row
        for row in evaluated
        if int(row["selection"]["trades"]) >= 300
        and 0.20
        <= float(row["selection"]["frequency_reduction_vs_baseline"])
        <= 0.90
    ]
    best_statistically_adequate = (
        max(statistically_adequate, key=_rank) if statistically_adequate else None
    )
    selected = best if best["selection_gate_pass"] else None
    frozen_model_bundle: dict[str, Any] | None = None
    if selected is None:
        frozen = {
            "status": "not_run_selection_gate_failed",
            "window_consumed": False,
            "reason": "no preregistered LightGBM threshold cleared selection gates",
        }
    else:
        x_frozen = preprocessor.transform(frozen_rows.loc[:, FEATURES])
        probability = model.predict_proba(x_frozen)[:, 1]
        frozen_summary = summarize(
            frozen_rows,
            probability >= float(selected["threshold"]),
        )
        gates = {
            "profit_factor_above_1_30": (
                frozen_summary["profit_factor"] is not None
                and float(frozen_summary["profit_factor"]) > 1.30
            ),
            "average_net_above_3_bps": (
                float(frozen_summary["average_net_bps"]) > 3.0
            ),
            "positive_markets_at_least_one": (
                int(frozen_summary["positive_markets"]) >= 1
            ),
        }
        frozen = {
            "status": "evaluated_once",
            "window_consumed": True,
            "summary": frozen_summary,
            "success_gates": gates,
        }
        if all(gates.values()):
            frozen_model_bundle = {
                "preprocessor": preprocessor,
                "model": model,
                "threshold": float(selected["threshold"]),
                "features": FEATURES,
            }

    selection_labels = selection[args.label_col].astype(int)
    feature_names = preprocessor.get_feature_names_out()
    importance = pd.DataFrame(
        {
            "feature": [
                str(name).replace("numeric__", "").replace("categorical__", "")
                for name in feature_names
            ],
            "importance": model.feature_importances_,
        }
    ).sort_values(["importance", "feature"], ascending=[False, True])
    payload: dict[str, Any] = {
        "report_id": "delta_scalper_lightgbm_meta_v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "label": args.label_col,
        "preregistered_before_results": True,
        "preregistered_thresholds": list(PREREGISTERED_THRESHOLDS),
        "windows": windows.to_dict(),
        "embargo_minutes_each_boundary_side": args.embargo_minutes,
        "window_counts": {
            "fit": len(fit),
            "early_stopping": len(early),
            "selection": len(selection),
            "frozen_protected": len(frozen_rows),
        },
        "model": {
            "type": "LightGBM LGBMClassifier",
            "best_iteration": int(model.best_iteration_ or model.n_estimators),
            "parameters": model.get_params(),
            "numeric_scaler": "RobustScaler fit on model-fit window only",
            "categorical_encoder": "OneHotEncoder fit on model-fit window only",
            "numeric_features": list(NUMERIC_FEATURES),
            "categorical_features": list(CATEGORICAL_FEATURES),
            "historical_l2_included": False,
            "historical_cvd_included": False,
            "historical_funding_included": False,
        },
        "source_data_quality": quality,
        "selection_classifier_diagnostics": {
            "observations": len(selection),
            "positive_label_rate": float(selection_labels.mean()),
            "roc_auc": (
                roc_auc_score(selection_labels, selection_probability)
                if selection_labels.nunique() > 1
                else None
            ),
            "brier_score": brier_score_loss(selection_labels, selection_probability),
        },
        "baseline_selection": baseline,
        "evaluated_thresholds": sorted(evaluated, key=_rank, reverse=True),
        "best_diagnostic_threshold": best,
        "best_statistically_adequate_threshold": best_statistically_adequate,
        "selected_threshold": selected,
        "frozen_window": frozen,
        "policy": {
            "research_only": True,
            "direct_trade_filter_not_independent_execution_replay": True,
            "fit_early_stop_and_selection_are_disjoint": True,
            "untouched_requires_selection_gate": True,
            "missing_features_are_not_fabricated": True,
            "deployable_artifacts_require_untouched_success": True,
            "can_trade": False,
            "can_promote": False,
        },
        "can_trade": False,
        "can_promote": False,
    }
    args.artifact_dir.mkdir(parents=True, exist_ok=True)
    _atomic_json(args.artifact_dir / "feature_list.json", payload["model"])
    importance.to_csv(args.artifact_dir / "feature_importance.csv", index=False)
    payload["research_artifacts"] = {
        "directory": str(args.artifact_dir),
        "support_files_written": True,
        "deployable_model_bundle_written": False,
    }
    if frozen_model_bundle is not None:
        temporary = args.artifact_dir / "meta_model_lgbm.joblib.tmp"
        joblib.dump(frozen_model_bundle, temporary)
        temporary.replace(args.artifact_dir / "meta_model_lgbm.joblib")
        booster_temporary = args.artifact_dir / "meta_model_lgbm.txt.tmp"
        model.booster_.save_model(str(booster_temporary))
        booster_temporary.replace(args.artifact_dir / "meta_model_lgbm.txt")
        preprocessor_temporary = args.artifact_dir / "meta_preprocessor.joblib.tmp"
        joblib.dump(preprocessor, preprocessor_temporary)
        preprocessor_temporary.replace(args.artifact_dir / "meta_preprocessor.joblib")
        _atomic_json(
            args.artifact_dir / "meta_threshold.json",
            {
                "threshold": float(selected["threshold"]),
                "frozen_after_untouched_success": True,
                "live_integration_enabled": False,
            },
        )
        _atomic_json(
            args.artifact_dir / "meta_config.json",
            {
                "features": list(FEATURES),
                "numeric_features": list(NUMERIC_FEATURES),
                "categorical_features": list(CATEGORICAL_FEATURES),
                "threshold": float(selected["threshold"]),
                "native_booster": "meta_model_lgbm.txt",
                "preprocessor": "meta_preprocessor.joblib",
                "sha256": {
                    "native_booster": _sha256_file(
                        args.artifact_dir / "meta_model_lgbm.txt"
                    ),
                    "preprocessor": _sha256_file(
                        args.artifact_dir / "meta_preprocessor.joblib"
                    ),
                },
                "frozen_after_untouched_success": True,
                "approved_for_shap": True,
                "live_integration_enabled": False,
            },
        )
        payload["research_artifacts"]["deployable_model_bundle_written"] = True
    _atomic_json(args.output, payload)
    _atomic_json(args.artifact_dir / "train_validation_untouched_metrics.json", payload)
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--backtest", type=Path, default=DEFAULT_BACKTEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--artifact-dir", type=Path, default=DEFAULT_ARTIFACT_DIR)
    parser.add_argument(
        "--label-col",
        choices=("tb_label", "net_gt_4bps", "net_positive"),
        default="tb_label",
    )
    parser.add_argument("--embargo-minutes", type=int, default=30)
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.embargo_minutes < 0:
        raise ValueError("embargo minutes cannot be negative")
    payload = run(args)
    print(
        json.dumps(
            {
                "best": payload["best_diagnostic_threshold"],
                "diagnostics": payload["selection_classifier_diagnostics"],
                "frozen": payload["frozen_window"],
                "artifacts": payload["research_artifacts"],
            },
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
