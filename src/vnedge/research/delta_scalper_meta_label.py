"""Chronological meta-label experiment for the research-only Delta scalper.

The primary scanners create the shared causal candidate ledger. A regularized
logistic secondary model estimates whether a candidate will clear a positive
after-cost label. Probability thresholds are evaluated on a later selection
slice with independent next-open simulations. The frozen tail is opened once
only if a preregistered threshold first clears every selection gate.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from vnedge.research.delta_scalper_backtest import _load_candles
from vnedge.research.delta_scalper_threshold_sweep import (
    CandidateLedger,
    CandidateVariant,
    GateVariant,
    VariantSimulation,
    _frozen_boundary,
    build_candidate_ledger,
    simulate_variant,
    summarize_simulations,
)
from vnedge.scalping.delta_engine.config import load_delta_scalper_config
from vnedge.scalping.delta_engine.types import Side, SignalCandidate

DEFAULT_CONFIG = Path("configs/delta_scalper.yaml")
DEFAULT_BACKTEST = Path("research/live_research/delta_scalper_backtest_latest.json")
DEFAULT_CACHE = Path("data/delta_scalper_cache")
DEFAULT_OUTPUT = Path("research/live_research/delta_scalper_meta_label_latest.json")

CATEGORICAL_FEATURES = (
    "scanner_id",
    "symbol",
    "side",
    "regime",
    "trend_regime",
    "trend_direction",
    "volatility_regime",
    "session_regime",
    "change_point_window",
)
NUMERIC_FEATURES = (
    "expected_move_bps",
    "expected_net_bps",
    "scalper_probability",
    "confidence",
    "planned_stop_bps",
    "planned_target_bps",
    "change_point_return_score",
    "change_point_volatility_score",
    "decision_hour_sin",
    "decision_hour_cos",
)
FEATURE_COLUMNS = (*CATEGORICAL_FEATURES, *NUMERIC_FEATURES)
PREREGISTERED_THRESHOLDS = (0.50, 0.55, 0.60, 0.65, 0.70, 0.75)


def _stamp(value: object) -> datetime:
    stamp = datetime.fromisoformat(str(value))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.astimezone(UTC)


def _cyclical_hour(ts: datetime) -> tuple[float, float]:
    angle = 2.0 * math.pi * (ts.hour * 60 + ts.minute) / (24 * 60)
    return math.sin(angle), math.cos(angle)


def trade_features(row: dict) -> dict[str, object]:
    decision = _stamp(row["decision_ts"])
    hour_sin, hour_cos = _cyclical_hour(decision)
    return {
        "scanner_id": str(row.get("scanner_id") or "unknown"),
        "symbol": str(row.get("symbol") or "unknown").upper(),
        "side": str(row.get("side") or "unknown").lower(),
        "regime": str(row.get("regime_at_entry") or row.get("regime") or "unknown"),
        "trend_regime": str(
            row.get("trend_regime_at_entry")
            or row.get("trend_regime")
            or "unknown"
        ),
        "trend_direction": str(
            row.get("trend_direction_at_entry")
            or row.get("trend_direction")
            or "unknown"
        ),
        "volatility_regime": str(
            row.get("volatility_regime_at_entry")
            or row.get("volatility_regime")
            or "unknown"
        ),
        "session_regime": str(
            row.get("session_regime_at_entry")
            or row.get("session_regime")
            or "unknown"
        ),
        "change_point_window": str(
            row.get("change_point_window_at_entry")
            or row.get("change_point_window")
            or "unavailable"
        ),
        "expected_move_bps": float(row.get("expected_move_bps") or 0.0),
        "expected_net_bps": float(row.get("expected_net_bps") or 0.0),
        "scalper_probability": float(row.get("scalper_probability") or 0.0),
        "confidence": float(row.get("confidence") or 0.0),
        "planned_stop_bps": float(row.get("planned_stop_bps") or 0.0),
        "planned_target_bps": float(row.get("planned_target_bps") or 0.0),
        "change_point_return_score": float(
            row.get("change_point_return_score") or 0.0
        ),
        "change_point_volatility_score": float(
            row.get("change_point_volatility_score") or 0.0
        ),
        "decision_hour_sin": hour_sin,
        "decision_hour_cos": hour_cos,
    }


def candidate_features(candidate: SignalCandidate) -> dict[str, object]:
    metadata = candidate.metadata
    raw_profile = metadata.get("regime_profile")
    profile = raw_profile if isinstance(raw_profile, dict) else {}
    raw_change_point = profile.get("change_point")
    change_point = raw_change_point if isinstance(raw_change_point, dict) else {}
    if candidate.side is Side.LONG:
        stop_bps = (1 - candidate.stop_loss / candidate.entry_price) * 10_000
        target_bps = (candidate.take_profits[0] / candidate.entry_price - 1) * 10_000
    else:
        stop_bps = (candidate.stop_loss / candidate.entry_price - 1) * 10_000
        target_bps = (1 - candidate.take_profits[0] / candidate.entry_price) * 10_000
    hour_sin, hour_cos = _cyclical_hour(candidate.decision_ts)
    return {
        "scanner_id": candidate.scanner_id,
        "symbol": candidate.symbol.upper(),
        "side": candidate.side.value,
        "regime": str(metadata.get("regime") or "unknown"),
        "trend_regime": str(profile.get("trend") or "unknown"),
        "trend_direction": str(profile.get("trend_direction") or "unknown"),
        "volatility_regime": str(profile.get("volatility") or "unknown"),
        "session_regime": str(profile.get("session") or "unknown"),
        "change_point_window": str(
            change_point.get("shift_window") or "unavailable"
        ),
        "expected_move_bps": candidate.expected_move_bps,
        "expected_net_bps": candidate.fee_adjusted_expectancy_bps,
        "scalper_probability": candidate.scalper_probability,
        "confidence": candidate.confidence,
        "planned_stop_bps": stop_bps,
        "planned_target_bps": target_bps,
        "change_point_return_score": float(
            change_point.get("return_score") or 0.0
        ),
        "change_point_volatility_score": float(
            change_point.get("volatility_score") or 0.0
        ),
        "decision_hour_sin": hour_sin,
        "decision_hour_cos": hour_cos,
    }


def build_model() -> Pipeline:
    preprocessor = ColumnTransformer(
        (
            (
                "numeric",
                Pipeline(
                    (
                        ("imputer", SimpleImputer(strategy="median")),
                        ("scale", StandardScaler()),
                    )
                ),
                list(NUMERIC_FEATURES),
            ),
            (
                "categorical",
                OneHotEncoder(handle_unknown="ignore"),
                list(CATEGORICAL_FEATURES),
            ),
        )
    )
    return Pipeline(
        (
            ("features", preprocessor),
            (
                "classifier",
                LogisticRegression(
                    C=0.5,
                    class_weight="balanced",
                    max_iter=2_000,
                    random_state=17,
                ),
            ),
        )
    )


def fit_model(rows: list[dict], *, label_net_bps: float) -> Pipeline:
    labels = [float(row.get("net_bps") or 0.0) > label_net_bps for row in rows]
    if len(set(labels)) < 2:
        raise ValueError("meta-label training requires both positive and negative outcomes")
    model = build_model()
    model.fit(pd.DataFrame([trade_features(row) for row in rows]), labels)
    return model


def score_candidates(
    model: Pipeline,
    ledgers: dict[str, CandidateLedger],
    baseline_gate: GateVariant,
    *,
    start: datetime,
    end: datetime,
) -> dict[str, float]:
    candidates = [
        candidate
        for ledger in ledgers.values()
        for close_ts, rows in ledger.candidates_by_close.items()
        if start <= close_ts < end
        for candidate in rows
        if baseline_gate.accepts(candidate)
    ]
    if not candidates:
        return {}
    frame = pd.DataFrame([candidate_features(candidate) for candidate in candidates])
    probabilities = model.predict_proba(frame)[:, 1]
    return {
        candidate.dedup_key: float(probability)
        for candidate, probability in zip(candidates, probabilities)
    }


@dataclass(frozen=True)
class MetaLabelVariant(CandidateVariant):
    threshold: float
    scores: dict[str, float]
    baseline_gate: GateVariant

    @property
    def config_id(self) -> str:
        return f"meta_probability_{self.threshold:.2f}"

    def accepts(self, candidate: SignalCandidate) -> bool:
        return self.baseline_gate.accepts(candidate) and (
            self.scores.get(candidate.dedup_key, -1.0) >= self.threshold
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "config_id": self.config_id,
            "threshold": self.threshold,
            "model": "regularized_logistic_regression",
        }


def _selection_gate(summary: dict, baseline: dict) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if not bool(summary.get("data_quality_pass")):
        reasons.append("validation_data_quality_failed")
    if int(summary["trades"]) < 300:
        reasons.append("fewer_than_300_validation_trades")
    reduction = 1 - int(summary["trades"]) / max(1, int(baseline["trades"]))
    if not 0.20 <= reduction <= 0.90:
        reasons.append("frequency_reduction_outside_20_90_pct")
    if summary["profit_factor"] is None or float(summary["profit_factor"]) < 1.10:
        reasons.append("validation_profit_factor_below_1_10")
    if float(summary["average_net_bps"]) < 1.0:
        reasons.append("validation_average_net_below_1_bps")
    if int(summary["positive_markets"]) < 1:
        reasons.append("no_positive_validation_market")
    return not reasons, reasons


def _rank(row: dict) -> tuple[float, float, float, float, int]:
    summary = row["validation"]
    reduction = float(summary.get("frequency_reduction_vs_baseline") or 0.0)
    adequate_sample = int(summary["trades"]) >= 300 and 0.20 <= reduction <= 0.90
    return (
        float(row["selection_gate_pass"]),
        float(adequate_sample),
        float(summary["profit_factor"] or 0.0),
        float(summary["average_net_bps"]),
        int(summary["trades"]),
    )


def _classifier_diagnostics(
    model: Pipeline,
    rows: list[dict],
    *,
    label_net_bps: float,
) -> dict:
    if not rows:
        return {"observations": 0}
    labels = [float(row.get("net_bps") or 0.0) > label_net_bps for row in rows]
    probabilities = model.predict_proba(
        pd.DataFrame([trade_features(row) for row in rows])
    )[:, 1]
    transformed_names = model.named_steps["features"].get_feature_names_out()
    coefficients = model.named_steps["classifier"].coef_[0]
    importance = sorted(
        (
            {
                "feature": str(name).replace("numeric__", "").replace(
                    "categorical__", ""
                ),
                "coefficient": float(coefficient),
                "absolute_coefficient": abs(float(coefficient)),
            }
            for name, coefficient in zip(transformed_names, coefficients)
        ),
        key=lambda row: row["absolute_coefficient"],
        reverse=True,
    )[:20]
    return {
        "observations": len(rows),
        "positive_label_rate": sum(labels) / len(labels),
        "roc_auc": roc_auc_score(labels, probabilities) if len(set(labels)) > 1 else None,
        "brier_score": brier_score_loss(labels, probabilities),
        "average_predicted_probability": float(probabilities.mean()),
        "top_absolute_coefficients": importance,
    }


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


async def run(args: argparse.Namespace) -> dict:
    config = load_delta_scalper_config(args.config)
    backtest = json.loads(args.backtest.read_text(encoding="utf-8"))
    window = backtest.get("window") or {}
    start = _stamp(args.start or window["start"])
    end = _stamp(args.end or window["end"])
    frozen_boundary = _frozen_boundary(backtest)
    train_end = start + (frozen_boundary - start) * args.train_fraction
    all_historical = sorted(
        [
            trade
            for market in (backtest.get("markets") or {}).values()
            for trade in market.get("trades", [])
        ],
        key=lambda row: str(row.get("exit_ts") or ""),
    )
    train_rows = [row for row in all_historical if _stamp(row["exit_ts"]) < train_end]
    validation_rows = [
        row
        for row in all_historical
        if train_end <= _stamp(row["decision_ts"])
        and _stamp(row["exit_ts"]) < frozen_boundary
    ]
    model = fit_model(train_rows, label_net_bps=args.label_net_bps)
    symbols = tuple(args.symbols.split(",")) if args.symbols else config.engine.symbols
    rows_by_symbol = {}
    ledgers = {}
    resolvers = {}
    for raw_symbol in symbols:
        symbol = raw_symbol.strip().upper()
        candles = await _load_candles(
            symbol,
            start,
            end,
            cache_dir=args.cache_dir,
            refresh=args.refresh,
        )
        ledger, resolver = build_candidate_ledger(
            symbol,
            candles,
            config,
            scalper_opted_in=args.scalper_opted_in,
            deto_enabled=args.deto,
        )
        rows_by_symbol[symbol] = candles
        ledgers[symbol] = ledger
        resolvers[symbol] = resolver
    baseline_gate = GateVariant(
        "baseline",
        "baseline",
        0.0,
        config.engine.min_probability,
        config.engine.min_confidence,
        config.engine.min_expectancy_bps,
    )
    validation_scores = score_candidates(
        model,
        ledgers,
        baseline_gate,
        start=train_end,
        end=frozen_boundary,
    )
    baseline_simulations = [
        simulate_variant(
            [bar for bar in candles if train_end <= bar.ts < frozen_boundary],
            ledgers[symbol],
            resolvers[symbol],
            baseline_gate,
        )
        for symbol, candles in rows_by_symbol.items()
    ]
    baseline_summary = summarize_simulations(baseline_simulations)
    evaluated = []
    for threshold in PREREGISTERED_THRESHOLDS:
        variant = MetaLabelVariant(threshold, validation_scores, baseline_gate)
        simulations = [
            simulate_variant(
                [bar for bar in candles if train_end <= bar.ts < frozen_boundary],
                ledgers[symbol],
                resolvers[symbol],
                variant,
            )
            for symbol, candles in rows_by_symbol.items()
        ]
        summary = summarize_simulations(simulations)
        summary["frequency_reduction_vs_baseline"] = 1 - int(summary["trades"]) / max(
            1, int(baseline_summary["trades"])
        )
        passed, reasons = _selection_gate(summary, baseline_summary)
        evaluated.append(
            {
                "config": variant.to_dict(),
                "validation": summary,
                "selection_gate_pass": passed,
                "selection_gate_reasons": reasons,
            }
        )
    best = max(evaluated, key=_rank)
    selected = best if best["selection_gate_pass"] else None
    if selected is None:
        frozen = {
            "status": "not_run_selection_gate_failed",
            "window_consumed": False,
            "reason": "no preregistered meta-label threshold cleared validation gates",
        }
    else:
        final_rows = [
            row for row in all_historical if _stamp(row["exit_ts"]) < frozen_boundary
        ]
        final_model = fit_model(final_rows, label_net_bps=args.label_net_bps)
        frozen_scores = score_candidates(
            final_model,
            ledgers,
            baseline_gate,
            start=frozen_boundary,
            end=end,
        )
        final_variant = MetaLabelVariant(
            float(selected["config"]["threshold"]),
            frozen_scores,
            baseline_gate,
        )
        frozen_simulations: list[VariantSimulation] = [
            simulate_variant(
                [bar for bar in candles if frozen_boundary <= bar.ts < end],
                ledgers[symbol],
                resolvers[symbol],
                final_variant,
            )
            for symbol, candles in rows_by_symbol.items()
        ]
        frozen_summary = summarize_simulations(frozen_simulations)
        frozen = {
            "status": "evaluated_once",
            "window_consumed": True,
            "config_id": final_variant.config_id,
            "summary": frozen_summary,
            "success_gates": {
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
            },
        }
    payload = {
        "report_id": "delta_scalper_meta_label_v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "preregistered_before_results": True,
        "preregistered_thresholds": list(PREREGISTERED_THRESHOLDS),
        "label": f"realized_net_bps_above_{args.label_net_bps:g}",
        "model": {
            "type": "regularized_logistic_regression",
            "C": 0.5,
            "class_weight": "balanced",
            "random_state": 17,
            "categorical_features": list(CATEGORICAL_FEATURES),
            "numeric_features": list(NUMERIC_FEATURES),
            "historical_l2_included": False,
            "historical_funding_included": False,
        },
        "windows": {
            "train": {"start": start.isoformat(), "end_exclusive": train_end.isoformat()},
            "validation": {
                "start": train_end.isoformat(),
                "end_exclusive": frozen_boundary.isoformat(),
            },
            "frozen": {"start": frozen_boundary.isoformat(), "end": end.isoformat()},
        },
        "train_fraction_of_selection_window": args.train_fraction,
        "training": {
            "outcomes": len(train_rows),
            "label_positive_rate": (
                sum(float(row.get("net_bps") or 0.0) > args.label_net_bps for row in train_rows)
                / len(train_rows)
                if train_rows
                else 0.0
            ),
        },
        "validation_classifier_diagnostics": _classifier_diagnostics(
            model,
            validation_rows,
            label_net_bps=args.label_net_bps,
        ),
        "baseline_validation": baseline_summary,
        "evaluated_thresholds": sorted(evaluated, key=_rank, reverse=True),
        "best_diagnostic_threshold": best,
        "selected_threshold": selected,
        "frozen_window": frozen,
        "policy": {
            "research_only": True,
            "chronological_training": True,
            "candidate_features_available_at_decision": True,
            "shared_candidate_ledger": True,
            "next_open_entries": True,
            "stop_first_ambiguity": True,
            "historical_l2_not_fabricated": True,
            "historical_funding_not_fabricated": True,
            "meta_label_used_for_live_signal": False,
            "model_artifact_promoted": False,
            "can_trade": False,
            "can_promote": False,
        },
        "can_trade": False,
        "can_promote": False,
    }
    _atomic_json(args.output, payload)
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--backtest", type=Path, default=DEFAULT_BACKTEST)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--symbols")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--train-fraction", type=float, default=0.65)
    parser.add_argument("--label-net-bps", type=float, default=3.0)
    parser.add_argument("--scalper-opted-in", action="store_true")
    parser.add_argument("--deto", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    return parser


def main() -> None:
    args = _parser().parse_args()
    if not 0.50 <= args.train_fraction <= 0.80:
        raise ValueError("train fraction must be between 0.50 and 0.80")
    payload = asyncio.run(run(args))
    print(
        json.dumps(
            {
                "best": payload["best_diagnostic_threshold"],
                "frozen": payload["frozen_window"],
            },
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
