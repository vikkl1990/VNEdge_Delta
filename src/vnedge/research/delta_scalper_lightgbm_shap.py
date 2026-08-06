"""Selection-only SHAP attribution for the guarded LightGBM meta-label model."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import shap
from lightgbm import early_stopping

from vnedge.research.delta_scalper_lightgbm_meta import (
    CATEGORICAL_FEATURES,
    FEATURES,
    NUMERIC_FEATURES,
    build_model,
    build_preprocessor,
    chronological_windows,
    load_dataset,
    profit_factor,
    split_dataset,
)

DEFAULT_DATA = Path("research/live_research/delta_scalper_with_tb_labels.parquet")
DEFAULT_INTERACTIONS = Path(
    "research/live_research/delta_scalper_cusum_interactions_latest.json"
)
DEFAULT_OUTPUT = Path("research/live_research/delta_scalper_lightgbm_shap_latest.json")
DEFAULT_ARTIFACT_DIR = Path("research/meta_labeling_shap")
GROUP_FIELDS = (
    "scanner_id",
    "symbol",
    "trend_regime_at_entry",
    "volatility_regime_at_entry",
    "change_point_window_at_entry",
)
CUSUM_FEATURES = (
    "change_point_bars_since_shift",
    "change_point_return_score",
    "change_point_volatility_score",
    "cusum_alarm_recent_at_entry",
)
PRIMARY_SIGNAL_FEATURES = (
    "expected_move_bps",
    "expected_net_bps",
    "expected_fee_multiple",
    "scalper_probability",
    "confidence",
)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def base_feature_name(transformed_name: str) -> str:
    clean = transformed_name.split("__", 1)[-1]
    if clean in NUMERIC_FEATURES:
        return clean
    for feature in sorted(CATEGORICAL_FEATURES, key=len, reverse=True):
        if clean == feature or clean.startswith(f"{feature}_"):
            return feature
    raise ValueError(f"cannot map transformed feature to causal input: {transformed_name}")


def aggregate_base_shap(
    shap_values: np.ndarray,
    transformed_names: list[str] | np.ndarray,
) -> pd.DataFrame:
    output = pd.DataFrame(0.0, index=range(len(shap_values)), columns=list(FEATURES))
    for index, name in enumerate(transformed_names):
        output[base_feature_name(str(name))] += shap_values[:, index]
    return output


def _global_importance(base_shap: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "feature": base_shap.columns,
            "mean_abs_shap": base_shap.abs().mean().values,
            "mean_shap": base_shap.mean().values,
            "median_shap": base_shap.median().values,
            "positive_contribution_rate": (base_shap > 0).mean().values,
        }
    ).sort_values(["mean_abs_shap", "feature"], ascending=[False, True])


def _encoded_importance(
    shap_values: np.ndarray,
    transformed_names: list[str] | np.ndarray,
) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "feature": [str(name).split("__", 1)[-1] for name in transformed_names],
            "base_feature": [base_feature_name(str(name)) for name in transformed_names],
            "mean_abs_shap": np.abs(shap_values).mean(axis=0),
            "mean_shap": shap_values.mean(axis=0),
            "positive_contribution_rate": (shap_values > 0).mean(axis=0),
        }
    ).sort_values(["mean_abs_shap", "feature"], ascending=[False, True])


def _economic_metrics(frame: pd.DataFrame) -> dict[str, Any]:
    net = frame["net_bps_resolved"].astype(float)
    return {
        "trades": len(frame),
        "average_net_bps": float(net.mean()) if len(net) else 0.0,
        "net_bps": float(net.sum()),
        "profit_factor": profit_factor(net),
        "win_rate": float((net > 0).mean()) if len(net) else 0.0,
    }


def _grouped_attribution(
    selection: pd.DataFrame,
    base_shap: pd.DataFrame,
    probabilities: np.ndarray,
    *,
    minimum_trades: int,
) -> list[dict[str, Any]]:
    working = selection.reset_index(drop=True).copy()
    working["prediction_probability"] = probabilities
    groups = working.groupby(list(GROUP_FIELDS), observed=True, sort=False).indices
    output = []
    for values, raw_indices in groups.items():
        indices = np.asarray(raw_indices, dtype=int)
        if len(indices) < minimum_trades:
            continue
        contributions = base_shap.iloc[indices].mean().sort_values(ascending=False)
        members = working.iloc[indices]
        output.append(
            {
                "key": " | ".join(map(str, values)),
                **dict(zip(GROUP_FIELDS, values)),
                **_economic_metrics(members),
                "average_prediction_probability": float(
                    members["prediction_probability"].mean()
                ),
                "average_total_shap_log_odds": float(
                    base_shap.iloc[indices].sum(axis=1).mean()
                ),
                "top_positive_contributions": [
                    {"feature": str(feature), "mean_shap": float(value)}
                    for feature, value in contributions.head(5).items()
                ],
                "top_negative_contributions": [
                    {"feature": str(feature), "mean_shap": float(value)}
                    for feature, value in contributions.tail(5).sort_values().items()
                ],
            }
        )
    return sorted(
        output,
        key=lambda row: -float(row["average_prediction_probability"]),
    )


def _cusum_by_window(
    selection: pd.DataFrame,
    base_shap: pd.DataFrame,
    probabilities: np.ndarray,
) -> list[dict[str, Any]]:
    working = selection.reset_index(drop=True).copy()
    working["prediction_probability"] = probabilities
    output = []
    for window, indices in working.groupby(
        "change_point_window_at_entry", observed=True
    ).indices.items():
        positions = np.asarray(indices, dtype=int)
        contributions = base_shap.iloc[positions]
        output.append(
            {
                "cusum_window": str(window),
                **_economic_metrics(working.iloc[positions]),
                "average_prediction_probability": float(
                    working.iloc[positions]["prediction_probability"].mean()
                ),
                "feature_contributions": {
                    feature: {
                        "mean_shap": float(contributions[feature].mean()),
                        "mean_abs_shap": float(contributions[feature].abs().mean()),
                    }
                    for feature in CUSUM_FEATURES
                },
            }
        )
    return sorted(output, key=lambda row: str(row["cusum_window"]))


def _local_attributions(
    selection: pd.DataFrame,
    base_shap: pd.DataFrame,
    probabilities: np.ndarray,
    base_value: float,
    *,
    count: int = 10,
) -> list[dict[str, Any]]:
    order = np.argsort(probabilities)[::-1][:count]
    output = []
    for index in order:
        row = selection.iloc[int(index)]
        contributions = base_shap.iloc[int(index)].sort_values(ascending=False)
        output.append(
            {
                "decision_ts": str(row["decision_ts"]),
                "scanner_id": str(row["scanner_id"]),
                "symbol": str(row["symbol"]),
                "probability": float(probabilities[index]),
                "label": int(row["tb_label"]),
                "realized_net_bps": float(row["net_bps_resolved"]),
                "base_value_log_odds": base_value,
                "prediction_log_odds": base_value + float(base_shap.iloc[index].sum()),
                "top_positive_contributions": [
                    {"feature": str(feature), "shap": float(value)}
                    for feature, value in contributions.head(8).items()
                ],
                "top_negative_contributions": [
                    {"feature": str(feature), "shap": float(value)}
                    for feature, value in contributions.tail(8).sort_values().items()
                ],
            }
        )
    return output


def _manual_best_cell_comparison(
    interaction_path: Path,
    selection: pd.DataFrame,
    base_shap: pd.DataFrame,
    probabilities: np.ndarray,
) -> dict[str, Any] | None:
    if not interaction_path.exists():
        return None
    payload = json.loads(interaction_path.read_text(encoding="utf-8"))
    best = ((payload.get("findings") or {}).get("best_eligible_cells") or [])
    if not best:
        return None
    cell = best[0]
    mask = (
        selection["change_point_window_at_entry"].astype(str).eq(
            str(cell["change_point_window"])
        )
        & selection["scanner_id"].astype(str).eq(str(cell["scanner_id"]))
        & selection["symbol"].astype(str).eq(str(cell["symbol"]))
        & selection["trend_regime_at_entry"].astype(str).eq(
            str(cell["trend_regime"])
        )
        & selection["volatility_regime_at_entry"].astype(str).eq(
            str(cell["volatility_regime"])
        )
    ).to_numpy()
    positions = np.flatnonzero(mask)
    if not len(positions):
        return None
    contributions = base_shap.iloc[positions].mean().sort_values(ascending=False)
    return {
        "manual_cell": cell["key"],
        "trades": len(positions),
        "manual_average_net_bps": float(cell["average_net_bps"]),
        "manual_profit_factor": cell["profit_factor"],
        "model_selection_slice_economics": _economic_metrics(
            selection.iloc[positions]
        ),
        "average_model_probability": float(probabilities[positions].mean()),
        "selection_average_model_probability": float(probabilities.mean()),
        "model_upweights_cell": bool(
            probabilities[positions].mean() > probabilities.mean()
        ),
        "top_positive_contributions": [
            {"feature": str(feature), "mean_shap": float(value)}
            for feature, value in contributions.head(8).items()
        ],
        "top_negative_contributions": [
            {"feature": str(feature), "mean_shap": float(value)}
            for feature, value in contributions.tail(8).sort_values().items()
        ],
    }


def _share(global_importance: pd.DataFrame, features: tuple[str, ...]) -> float:
    total = float(global_importance["mean_abs_shap"].sum())
    selected = float(
        global_importance.loc[
            global_importance["feature"].isin(features), "mean_abs_shap"
        ].sum()
    )
    return selected / total if total else 0.0


def _plots(
    shap_values: np.ndarray,
    transformed: np.ndarray,
    transformed_names: list[str],
    base_value: float,
    probabilities: np.ndarray,
    artifact_dir: Path,
) -> None:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    shap.summary_plot(
        shap_values,
        transformed,
        feature_names=transformed_names,
        max_display=20,
        show=False,
    )
    plt.tight_layout()
    plt.savefig(artifact_dir / "shap_beeswarm.png", dpi=160, bbox_inches="tight")
    plt.close()
    shap.summary_plot(
        shap_values,
        transformed,
        feature_names=transformed_names,
        plot_type="bar",
        max_display=20,
        show=False,
    )
    plt.tight_layout()
    plt.savefig(artifact_dir / "shap_global_bar.png", dpi=160, bbox_inches="tight")
    plt.close()
    index = int(np.argmax(probabilities))
    explanation = shap.Explanation(
        values=shap_values[index],
        base_values=base_value,
        data=transformed[index],
        feature_names=transformed_names,
    )
    shap.plots.waterfall(explanation, max_display=15, show=False)
    plt.tight_layout()
    plt.savefig(artifact_dir / "shap_highest_probability_waterfall.png", dpi=160, bbox_inches="tight")
    plt.close()


def run(args: argparse.Namespace) -> dict[str, Any]:
    frame = load_dataset(args.data)
    windows = chronological_windows(frame, args.embargo_minutes)
    fit, early, selection, frozen = split_dataset(frame, windows)
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
    probabilities = model.predict_proba(x_selection)[:, 1]
    explainer = shap.TreeExplainer(model)
    shap_values = np.asarray(explainer.shap_values(x_selection), dtype=float)
    if shap_values.ndim == 3:
        shap_values = shap_values[:, :, -1]
    transformed_names = [str(name) for name in preprocessor.get_feature_names_out()]
    base_value = float(np.asarray(explainer.expected_value).reshape(-1)[-1])
    base_shap = aggregate_base_shap(shap_values, transformed_names)
    global_importance = _global_importance(base_shap)
    encoded_importance = _encoded_importance(shap_values, transformed_names)
    grouped = _grouped_attribution(
        selection,
        base_shap,
        probabilities,
        minimum_trades=args.minimum_group_trades,
    )
    cusum = _cusum_by_window(selection, base_shap, probabilities)
    local = _local_attributions(
        selection,
        base_shap,
        probabilities,
        base_value,
    )
    manual_comparison = _manual_best_cell_comparison(
        args.interaction_report,
        selection,
        base_shap,
        probabilities,
    )
    transformed_dense = (
        x_selection.toarray() if hasattr(x_selection, "toarray") else np.asarray(x_selection)
    )
    _plots(
        shap_values,
        transformed_dense,
        [name.split("__", 1)[-1] for name in transformed_names],
        base_value,
        probabilities,
        args.artifact_dir,
    )
    red_flags = {
        "time_feature_share": _share(global_importance, ("hour_utc",)),
        "symbol_feature_share": _share(global_importance, ("symbol",)),
        "scanner_feature_share": _share(global_importance, ("scanner_id",)),
        "time_and_symbol_share": _share(global_importance, ("hour_utc", "symbol")),
        "primary_signal_feature_share": _share(
            global_importance, PRIMARY_SIGNAL_FEATURES
        ),
        "cusum_feature_share": _share(global_importance, CUSUM_FEATURES),
        "historical_microstructure_available": False,
        "top_feature_is_time_or_symbol": bool(
            str(global_importance.iloc[0]["feature"]) in {"hour_utc", "symbol"}
        ),
        "top_feature_is_scanner_identity": bool(
            str(global_importance.iloc[0]["feature"]) == "scanner_id"
        ),
        "model_upweights_manual_least_bad_cell_while_still_negative": bool(
            manual_comparison
            and manual_comparison["model_upweights_cell"]
            and float(
                manual_comparison["model_selection_slice_economics"][
                    "average_net_bps"
                ]
            )
            < 0
        ),
    }
    payload: dict[str, Any] = {
        "report_id": "delta_scalper_lightgbm_shap_v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": "threshold_selection_window_only",
        "model_best_iteration": int(model.best_iteration_ or model.n_estimators),
        "observations_explained": len(selection),
        "transformed_features": len(transformed_names),
        "base_features": len(FEATURES),
        "base_value_log_odds": base_value,
        "average_prediction_probability": float(probabilities.mean()),
        "global_importance": global_importance.to_dict(orient="records"),
        "encoded_importance": encoded_importance.to_dict(orient="records"),
        "grouped_attribution": grouped,
        "cusum_attribution_by_window": cusum,
        "local_high_probability_attribution": local,
        "manual_interaction_comparison": manual_comparison,
        "red_flags": red_flags,
        "frozen_untouched_window": {
            "trades": len(frozen),
            "shap_computed": False,
            "predictions_computed": False,
            "protected": True,
        },
        "policy": {
            "research_only": True,
            "shap_used_for_live_prediction": False,
            "shap_used_for_scanner_gate": False,
            "shap_used_for_execution": False,
            "historical_l2_cvd_not_fabricated": True,
            "can_trade": False,
            "can_promote": False,
        },
        "can_trade": False,
        "can_promote": False,
    }
    args.artifact_dir.mkdir(parents=True, exist_ok=True)
    global_importance.to_csv(args.artifact_dir / "global_base_shap.csv", index=False)
    encoded_importance.to_csv(args.artifact_dir / "global_encoded_shap.csv", index=False)
    pd.DataFrame(grouped).to_json(
        args.artifact_dir / "grouped_shap.json", orient="records", indent=2
    )
    pd.DataFrame(cusum).to_json(
        args.artifact_dir / "cusum_window_shap.json", orient="records", indent=2
    )
    _atomic_json(args.artifact_dir / "local_high_probability_shap.json", {"rows": local})
    _atomic_json(args.artifact_dir / "shap_report.json", payload)
    _atomic_json(args.output, payload)
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--interaction-report", type=Path, default=DEFAULT_INTERACTIONS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--artifact-dir", type=Path, default=DEFAULT_ARTIFACT_DIR)
    parser.add_argument(
        "--label-col",
        choices=("tb_label", "net_gt_4bps", "net_positive"),
        default="tb_label",
    )
    parser.add_argument("--embargo-minutes", type=int, default=30)
    parser.add_argument("--minimum-group-trades", type=int, default=40)
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.minimum_group_trades <= 0:
        raise ValueError("minimum group trades must be positive")
    payload = run(args)
    print(
        json.dumps(
            {
                "observations": payload["observations_explained"],
                "top_global_features": payload["global_importance"][:10],
                "manual_interaction_comparison": payload[
                    "manual_interaction_comparison"
                ],
                "red_flags": payload["red_flags"],
                "frozen": payload["frozen_untouched_window"],
            },
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
