"""Ex-ante edge model for scanner opportunity routing.

`execution_edge_router_v1` proved the raw scanner entries are not profitable
policies by themselves. This module is the first learning layer above those
opportunity rows:

- train on chronological opportunity labels,
- predict maker/taker net edge from ex-ante fields only,
- route the untouched OOS tail using those predictions,
- compare model-routed performance against the raw all-scanner baseline.

Research-only. It never submits orders, never promotes lanes, and never writes
runtime manifests.
"""

from __future__ import annotations

import argparse
import inspect
import json
import math
import re
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from typing import Callable, Iterable, Literal

import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.metrics import mean_absolute_error
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder

from vnedge.data.parquet_store import ParquetStore
from vnedge.research.execution_edge_router import (
    DEFAULT_SCALPER_STRATEGIES,
    OpportunityRoute,
    OpportunityRouterConfig,
    label_strategy_opportunities,
    _window,
)
from vnedge.research.universe import ResearchTarget, load_research_targets
from vnedge.strategy.base_strategy import BaseStrategy
from vnedge.strategy.strategy_registry import get_strategy_class

ModelAction = Literal["SKIP", "MAKER", "TAKER_NOW"]
SelectorDirection = Literal["ge", "le"]
ModelVerdict = Literal[
    "NO_OPPORTUNITIES",
    "UNDER_SAMPLED",
    "NO_MODEL_SELECTION",
    "MODEL_NO_IMPROVEMENT",
    "MODEL_IMPROVED",
    "MODEL_PAPER_CANDIDATE",
]

DEFAULT_EDGE_MODEL_TIMEFRAMES = ("1m", "5m", "15m", "1h", "4h")


@dataclass(frozen=True)
class EdgeModelConfig:
    train_fraction: float = 0.70
    min_train_samples: int = 100
    min_test_samples: int = 50
    min_model_trades: int = 20
    min_predicted_net_bps: float = 25.0
    taker_extra_buffer_bps: float = 5.0
    min_profit_factor: float = 1.50
    min_improvement_bps: float = 1.0
    random_state: int = 42

    def __post_init__(self) -> None:
        if not 0.1 <= self.train_fraction <= 0.9:
            raise ValueError("train_fraction must be in [0.1, 0.9]")
        if self.min_train_samples < 10 or self.min_test_samples < 1:
            raise ValueError("sample floors are too small")
        if self.min_model_trades < 1:
            raise ValueError("min_model_trades must be positive")
        if self.min_predicted_net_bps < 0 or self.taker_extra_buffer_bps < 0:
            raise ValueError("edge thresholds cannot be negative")
        if self.min_profit_factor < 1.0:
            raise ValueError("min_profit_factor must be >= 1")


@dataclass(frozen=True)
class ModelRoutedOpportunity:
    event_id: str
    ts: str
    exchange: str
    symbol: str
    timeframe: str
    strategy_id: str
    side: str
    action: ModelAction
    reason: str
    predicted_maker_net_bps: float
    predicted_taker_net_bps: float
    selected_net_bps: float | None
    raw_maker_net_bps: float | None
    raw_taker_net_bps: float | None
    can_trade: bool = False
    can_promote: bool = False
    requires_untouched_judgment: bool = True

    @property
    def routed(self) -> bool:
        return self.action != "SKIP" and self.selected_net_bps is not None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ModelSelectorRule:
    route: Literal["MAKER", "TAKER_NOW"]
    direction: SelectorDirection
    threshold: float
    train_selected: int
    train_avg_net_bps: float
    train_profit_factor: float | None
    train_improvement_bps: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class EdgeModelSummary:
    verdict: ModelVerdict
    opportunities: int
    train_samples: int
    test_samples: int
    model_trades: int
    raw_avg_net_bps: float | None
    model_avg_net_bps: float | None
    improvement_bps: float | None
    raw_profit_factor: float | None
    model_profit_factor: float | None
    raw_win_rate_pct: float
    model_win_rate_pct: float
    selection_rate_pct: float
    maker_mae_bps: float | None
    taker_mae_bps: float | None
    action_counts: dict[str, int]
    primary_blocker: str
    paper_candidate: bool
    selector: dict | None
    can_trade: bool = False
    can_promote: bool = False
    requires_untouched_judgment: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


def build_opportunity_dataset(routes: Iterable[OpportunityRoute]) -> pd.DataFrame:
    """Convert opportunity rows into an ex-ante feature/target table."""

    records: list[dict] = []
    for row in routes:
        maker_net = _finite_or_none(row.maker_net_bps)
        taker_net = _finite_or_none(row.taker_net_bps)
        if maker_net is None and taker_net is None:
            continue
        ts = pd.Timestamp(row.ts)
        meta = dict(row.metadata or {})
        reason_features = _parse_reason_features(str(meta.get("reason", "")))
        exchange = str(meta.get("exchange", "unknown"))
        symbol = str(meta.get("symbol", "unknown"))
        timeframe = str(meta.get("timeframe", "unknown"))
        risk_bps = _finite_or_none(row.risk_bps)
        expected_edge_bps = _finite_or_none(row.expected_edge_bps)
        maker_cost_bps = _finite_or_none(row.maker_cost_bps)
        taker_cost_bps = _finite_or_none(row.taker_cost_bps)
        timeframe_seconds = _timeframe_seconds(timeframe)
        record = {
            "event_id": row.event_id,
            "ts": ts,
            "exchange": exchange,
            "symbol": symbol,
            "timeframe": timeframe,
            "strategy_id": row.strategy_id,
            "source_id": row.source_id,
            "side": row.side,
            "hour": ts.hour,
            "day_of_week": ts.dayofweek,
            "hour_sin": math.sin(2.0 * math.pi * ts.hour / 24.0),
            "hour_cos": math.cos(2.0 * math.pi * ts.hour / 24.0),
            "day_of_week_sin": math.sin(2.0 * math.pi * ts.dayofweek / 7.0),
            "day_of_week_cos": math.cos(2.0 * math.pi * ts.dayofweek / 7.0),
            "timeframe_seconds": timeframe_seconds,
            "log_timeframe_seconds": (
                math.log(float(timeframe_seconds)) if timeframe_seconds else None
            ),
            "risk_bps": risk_bps,
            "maker_cost_bps": maker_cost_bps,
            "taker_cost_bps": taker_cost_bps,
            "maker_taker_cost_gap_bps": _subtract(taker_cost_bps, maker_cost_bps),
            "maker_fill_probability": row.maker_fill_probability,
            "expected_edge_bps": expected_edge_bps,
            "expected_minus_maker_cost_bps": _subtract(
                expected_edge_bps,
                maker_cost_bps,
            ),
            "expected_minus_taker_cost_bps": _subtract(
                expected_edge_bps,
                taker_cost_bps,
            ),
            "expected_edge_to_risk": _safe_div(expected_edge_bps, risk_bps),
            "risk_to_maker_cost": _safe_div(risk_bps, maker_cost_bps),
            "risk_to_taker_cost": _safe_div(risk_bps, taker_cost_bps),
            "maker_fill_edge_bps": (
                (float(row.maker_fill_probability) * expected_edge_bps) - maker_cost_bps
                if expected_edge_bps is not None and maker_cost_bps is not None
                else None
            ),
            "maker_net_bps": maker_net,
            "taker_net_bps": taker_net,
        }
        for key, value in reason_features.items():
            record[f"reason_{key}"] = value
        for key, value in meta.items():
            if key in {"reason", "bar_index", "exchange", "symbol", "timeframe"}:
                continue
            numeric = _finite_or_none(value)
            if numeric is not None:
                record[f"meta_{_clean_name(key)}"] = numeric
        records.append(record)
    if not records:
        return pd.DataFrame()
    return pd.DataFrame(records).sort_values("ts").reset_index(drop=True)


def backtest_edge_model(
    routes: Iterable[OpportunityRoute],
    *,
    config: EdgeModelConfig = EdgeModelConfig(),
) -> dict:
    dataset = build_opportunity_dataset(routes)
    if dataset.empty:
        return build_edge_model_report(
            config=config,
            dataset=dataset,
            routed=(),
            maker_mae=None,
            taker_mae=None,
            blocker="no opportunity rows with route labels",
        )
    split = int(len(dataset) * config.train_fraction)
    train = dataset.iloc[:split].reset_index(drop=True)
    test = dataset.iloc[split:].reset_index(drop=True)
    if len(train) < config.min_train_samples or len(test) < config.min_test_samples:
        return build_edge_model_report(
            config=config,
            dataset=dataset,
            routed=(),
            maker_mae=None,
            taker_mae=None,
            train_samples=len(train),
            test_samples=len(test),
            blocker=(
                f"need >= {config.min_train_samples} train and >= "
                f"{config.min_test_samples} test rows"
            ),
        )

    feature_columns = _feature_columns(dataset)
    maker_model = _fit_regressor(train, feature_columns, "maker_net_bps", config)
    taker_model = _fit_regressor(train, feature_columns, "taker_net_bps", config)
    train_pred_maker = maker_model.predict(train[list(feature_columns)])
    train_pred_taker = taker_model.predict(train[list(feature_columns)])
    pred_maker = maker_model.predict(test[list(feature_columns)])
    pred_taker = taker_model.predict(test[list(feature_columns)])
    maker_mae = mean_absolute_error(test["maker_net_bps"], pred_maker)
    taker_mae = mean_absolute_error(test["taker_net_bps"], pred_taker)
    selector = _select_rule(
        train,
        train_pred_maker,
        train_pred_taker,
        config=config,
    )
    routed = tuple(
        _route_prediction(
            test.iloc[i],
            float(pred_maker[i]),
            float(pred_taker[i]),
            config,
            selector,
        )
        for i in range(len(test))
    )
    return build_edge_model_report(
        config=config,
        dataset=dataset,
        routed=routed,
        maker_mae=float(maker_mae),
        taker_mae=float(taker_mae),
        selector=selector,
        train_samples=len(train),
        test_samples=len(test),
    )


def backtest_edge_model_timeframe_matrix(
    routes: Iterable[OpportunityRoute],
    *,
    config: EdgeModelConfig = EdgeModelConfig(),
    include_aggregate: bool = True,
) -> dict:
    """Run edge_model_v1 once per timeframe plus an optional aggregate view."""

    rows = tuple(routes)
    reports: list[dict] = []
    if include_aggregate:
        aggregate = backtest_edge_model(rows, config=config)
        aggregate["scope"] = _report_scope("ALL_TIMEFRAMES", rows)
        reports.append(aggregate)

    by_timeframe: dict[str, list[OpportunityRoute]] = {}
    for row in rows:
        timeframe = str((row.metadata or {}).get("timeframe") or "unknown")
        by_timeframe.setdefault(timeframe, []).append(row)

    for timeframe in sorted(by_timeframe, key=_timeframe_sort_key):
        tf_rows = tuple(by_timeframe[timeframe])
        report = backtest_edge_model(tf_rows, config=config)
        report["scope"] = _report_scope(timeframe, tf_rows)
        reports.append(report)

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "truth_layer": "edge_model_v1_timeframe_matrix",
        "policy": {
            "research_only": True,
            "can_trade": False,
            "can_promote": False,
            "requires_untouched_judgment": True,
            "decision_uses_forward_truth": False,
            "operator_note": (
                "Timeframe matrix verifies the learner per slice. Aggregate "
                "improvement cannot promote a lane if the timeframe slice fails."
            ),
        },
        "config": asdict(config),
        "summary": _matrix_summary(reports),
        "reports": reports,
    }


def _report_scope(scope_id: str, rows: tuple[OpportunityRoute, ...]) -> dict:
    timeframes = sorted(
        {str((row.metadata or {}).get("timeframe") or "unknown") for row in rows},
        key=_timeframe_sort_key,
    )
    targets = sorted(
        {
            (
                str((row.metadata or {}).get("exchange") or "unknown"),
                str((row.metadata or {}).get("symbol") or "unknown"),
                str((row.metadata or {}).get("timeframe") or "unknown"),
            )
            for row in rows
        }
    )
    return {
        "id": scope_id,
        "timeframe": None if scope_id == "ALL_TIMEFRAMES" else scope_id,
        "opportunities": len(rows),
        "timeframes": timeframes,
        "targets": [
            {"exchange": exchange, "symbol": symbol, "timeframe": timeframe}
            for exchange, symbol, timeframe in targets
        ],
        "strategies": sorted({row.strategy_id for row in rows}),
    }


def _matrix_summary(reports: Iterable[dict]) -> dict:
    reports = tuple(reports)
    timeframe_reports = [
        report for report in reports
        if report.get("scope", {}).get("id") != "ALL_TIMEFRAMES"
    ]
    improved = [
        report for report in timeframe_reports
        if report["summary"]["verdict"] in {"MODEL_IMPROVED", "MODEL_PAPER_CANDIDATE"}
    ]
    candidates = [
        report for report in timeframe_reports
        if report["summary"]["verdict"] == "MODEL_PAPER_CANDIDATE"
    ]
    best = _best_report(improved or timeframe_reports)
    return {
        "timeframe_count": len(timeframe_reports),
        "timeframes": [
            report["scope"]["id"]
            for report in sorted(
                timeframe_reports,
                key=lambda report: _timeframe_sort_key(str(report["scope"]["id"])),
            )
        ],
        "total_opportunities": sum(
            int(report["scope"]["opportunities"]) for report in timeframe_reports
        ),
        "improved_timeframes": [report["scope"]["id"] for report in improved],
        "paper_candidate_timeframes": [report["scope"]["id"] for report in candidates],
        "best_timeframe": best["scope"]["id"] if best is not None else None,
        "best_verdict": best["summary"]["verdict"] if best is not None else None,
        "best_model_avg_net_bps": (
            best["summary"]["model_avg_net_bps"] if best is not None else None
        ),
        "best_profit_factor": (
            best["summary"]["model_profit_factor"] if best is not None else None
        ),
    }


def _best_report(reports: Iterable[dict]) -> dict | None:
    best: dict | None = None
    best_key: tuple[float, float, int] | None = None
    for report in reports:
        summary = report["summary"]
        avg = summary["model_avg_net_bps"]
        improvement = summary["improvement_bps"]
        trades = int(summary["model_trades"])
        key = (
            float(avg) if avg is not None else -10**9,
            float(improvement) if improvement is not None else -10**9,
            trades,
        )
        if best_key is None or key > best_key:
            best = report
            best_key = key
    return best


def build_edge_model_report(
    *,
    config: EdgeModelConfig,
    dataset: pd.DataFrame,
    routed: Iterable[ModelRoutedOpportunity],
    maker_mae: float | None,
    taker_mae: float | None,
    selector: ModelSelectorRule | None = None,
    train_samples: int | None = None,
    test_samples: int | None = None,
    blocker: str | None = None,
) -> dict:
    routed_rows = tuple(routed)
    test_n = test_samples if test_samples is not None else len(routed_rows)
    train_n = train_samples if train_samples is not None else 0
    raw_net = [
        row.raw_maker_net_bps
        for row in routed_rows
        if row.raw_maker_net_bps is not None
    ]
    model_net = [
        row.selected_net_bps
        for row in routed_rows
        if row.routed and row.selected_net_bps is not None
    ]
    raw_avg = mean(raw_net) if raw_net else None
    model_avg = mean(model_net) if model_net else None
    improvement = (
        (model_avg - raw_avg)
        if model_avg is not None and raw_avg is not None
        else None
    )
    raw_pf = _profit_factor(raw_net)
    model_pf = _profit_factor(model_net)
    raw_win = _win_rate(raw_net)
    model_win = _win_rate(model_net)
    action_counts = dict(Counter(row.action for row in routed_rows))
    verdict, primary_blocker = _verdict(
        dataset=dataset,
        routed=routed_rows,
        raw_avg=raw_avg,
        model_avg=model_avg,
        improvement=improvement,
        model_pf=model_pf,
        blocker=blocker,
        config=config,
        train_samples=train_n,
        test_samples=test_n,
    )
    paper_candidate = verdict == "MODEL_PAPER_CANDIDATE"
    summary = EdgeModelSummary(
        verdict=verdict,
        opportunities=len(dataset),
        train_samples=train_n,
        test_samples=test_n,
        model_trades=len(model_net),
        raw_avg_net_bps=_round_or_none(raw_avg),
        model_avg_net_bps=_round_or_none(model_avg),
        improvement_bps=_round_or_none(improvement),
        raw_profit_factor=_round_or_none(raw_pf),
        model_profit_factor=_round_or_none(model_pf),
        raw_win_rate_pct=raw_win,
        model_win_rate_pct=model_win,
        selection_rate_pct=round(len(model_net) / test_n * 100.0, 2) if test_n else 0.0,
        maker_mae_bps=_round_or_none(maker_mae),
        taker_mae_bps=_round_or_none(taker_mae),
        action_counts=action_counts,
        primary_blocker=primary_blocker,
        paper_candidate=paper_candidate,
        selector=selector.to_dict() if selector is not None else None,
    )
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "truth_layer": "edge_model_v1",
        "policy": {
            "research_only": True,
            "can_trade": False,
            "can_promote": False,
            "requires_untouched_judgment": True,
            "decision_uses_forward_truth": False,
            "operator_note": (
                "Model is trained on chronological opportunity labels and "
                "evaluated on the held-out OOS tail. A positive result is still "
                "research evidence, not paper or live approval."
            ),
        },
        "config": asdict(config),
        "summary": summary.to_dict(),
        "routes": [row.to_dict() for row in routed_rows],
    }


def load_strategy_opportunities(
    *,
    data_root: Path | str,
    targets: Iterable[ResearchTarget],
    strategy_ids: Iterable[str] = DEFAULT_SCALPER_STRATEGIES,
    lookback_days: int = 30,
    router_config: OpportunityRouterConfig = OpportunityRouterConfig(),
    progress_callback: Callable[[dict], None] | None = None,
) -> tuple[OpportunityRoute, ...]:
    store = ParquetStore(data_root)
    out: list[OpportunityRoute] = []
    targets_tuple = tuple(targets)
    strategy_tuple = tuple(strategy_ids)
    total_work_units = len(targets_tuple) * len(strategy_tuple)
    completed_work_units = 0
    for target in targets_tuple:
        try:
            candles = _window(
                store.read_candles(target.exchange, target.symbol, target.timeframe),
                lookback_days,
            )
        except FileNotFoundError:
            for strategy_id in strategy_tuple:
                if progress_callback is not None:
                    progress_callback(
                        {
                            "phase": "missing_candles",
                            "target": asdict(target),
                            "strategy_id": strategy_id,
                            "total_work_units": total_work_units,
                            "completed_work_units": completed_work_units,
                            "rows": 0,
                            "routes": 0,
                            "last_error": "missing candle parquet",
                        }
                    )
                completed_work_units += 1
                if progress_callback is not None:
                    progress_callback(
                        {
                            "phase": "missing_candles",
                            "target": asdict(target),
                            "strategy_id": strategy_id,
                            "total_work_units": total_work_units,
                            "completed_work_units": completed_work_units,
                            "rows": 0,
                            "routes": 0,
                            "last_error": "missing candle parquet",
                        }
                    )
            continue
        for strategy_id in strategy_tuple:
            if progress_callback is not None:
                progress_callback(
                    {
                        "phase": "labeling_opportunities",
                        "target": asdict(target),
                        "strategy_id": strategy_id,
                        "total_work_units": total_work_units,
                        "completed_work_units": completed_work_units,
                        "rows": len(candles),
                        "routes": 0,
                        "last_error": None,
                    }
                )
            strategy = _instantiate_strategy(
                get_strategy_class(strategy_id),
                store,
                target.exchange,
                target.symbol,
            )
            route_count = 0
            for route in label_strategy_opportunities(
                candles,
                strategy,
                exchange=target.exchange,
                config=router_config,
            ):
                route_count += 1
                meta = dict(route.metadata)
                meta.update(
                    {
                        "exchange": target.exchange,
                        "symbol": target.symbol,
                        "timeframe": target.timeframe,
                    }
                )
                out.append(replace(route, metadata=meta))
            completed_work_units += 1
            if progress_callback is not None:
                progress_callback(
                    {
                        "phase": "labeling_opportunities",
                        "target": asdict(target),
                        "strategy_id": strategy_id,
                        "total_work_units": total_work_units,
                        "completed_work_units": completed_work_units,
                        "rows": len(candles),
                        "routes": route_count,
                        "last_error": None,
                    }
                )
    return tuple(out)


def _route_prediction(
    row: pd.Series,
    predicted_maker_net: float,
    predicted_taker_net: float,
    config: EdgeModelConfig,
    selector: ModelSelectorRule | None,
) -> ModelRoutedOpportunity:
    action: ModelAction = "SKIP"
    selected_net: float | None = None
    reason = "no train-calibrated selector accepted this opportunity"
    if selector is not None:
        score = predicted_maker_net if selector.route == "MAKER" else predicted_taker_net
        if _passes_selector(score, selector):
            action = selector.route
            selected_net = _finite_or_none(
                row.get("maker_net_bps" if selector.route == "MAKER" else "taker_net_bps")
            )
            reason = (
                f"train-calibrated {selector.route} selector "
                f"{selector.direction} {selector.threshold:.4f}"
            )
    return ModelRoutedOpportunity(
        event_id=str(row["event_id"]),
        ts=pd.Timestamp(row["ts"]).isoformat(),
        exchange=str(row["exchange"]),
        symbol=str(row["symbol"]),
        timeframe=str(row["timeframe"]),
        strategy_id=str(row["strategy_id"]),
        side=str(row["side"]),
        action=action,
        reason=reason,
        predicted_maker_net_bps=round(predicted_maker_net, 4),
        predicted_taker_net_bps=round(predicted_taker_net, 4),
        selected_net_bps=_round_or_none(selected_net),
        raw_maker_net_bps=_round_or_none(_finite_or_none(row.get("maker_net_bps"))),
        raw_taker_net_bps=_round_or_none(_finite_or_none(row.get("taker_net_bps"))),
    )


def _select_rule(
    train: pd.DataFrame,
    predicted_maker: Iterable[float],
    predicted_taker: Iterable[float],
    *,
    config: EdgeModelConfig,
) -> ModelSelectorRule | None:
    raw = [float(v) for v in train["maker_net_bps"].dropna()]
    if not raw:
        return None
    raw_avg = mean(raw)
    candidates: list[ModelSelectorRule] = []
    for route, predictions, target in (
        ("MAKER", tuple(float(v) for v in predicted_maker), "maker_net_bps"),
        ("TAKER_NOW", tuple(float(v) for v in predicted_taker), "taker_net_bps"),
    ):
        if not predictions:
            continue
        thresholds = _candidate_thresholds(predictions)
        for direction in ("ge", "le"):
            for threshold in thresholds:
                selected = [
                    float(train[target].iloc[i])
                    for i, score in enumerate(predictions)
                    if _passes(score, direction, threshold)
                    and _finite_or_none(train[target].iloc[i]) is not None
                ]
                if len(selected) < config.min_model_trades:
                    continue
                avg = mean(selected)
                improvement = avg - raw_avg
                pf = _profit_factor(selected)
                if improvement < config.min_improvement_bps:
                    continue
                if (pf or 0.0) < 1.0:
                    continue
                candidates.append(
                    ModelSelectorRule(
                        route=route,
                        direction=direction,  # type: ignore[arg-type]
                        threshold=float(threshold),
                        train_selected=len(selected),
                        train_avg_net_bps=round(avg, 4),
                        train_profit_factor=_round_or_none(pf),
                        train_improvement_bps=round(improvement, 4),
                    )
                )
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda rule: (
            rule.train_avg_net_bps,
            rule.train_profit_factor or 0.0,
            rule.train_selected,
        ),
    )


def _candidate_thresholds(predictions: Iterable[float]) -> tuple[float, ...]:
    s = pd.Series([p for p in predictions if math.isfinite(p)])
    if s.empty:
        return ()
    quantiles = [0.05, 0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 0.95]
    values = {round(float(s.quantile(q)), 8) for q in quantiles}
    unique = sorted({round(float(v), 8) for v in s.unique()})
    if 1 < len(unique) <= 50:
        values.update((unique[i - 1] + unique[i]) / 2.0 for i in range(1, len(unique)))
    return tuple(sorted(values))


def _passes_selector(score: float, selector: ModelSelectorRule) -> bool:
    return _passes(score, selector.direction, selector.threshold)


def _passes(score: float, direction: SelectorDirection | str, threshold: float) -> bool:
    if not math.isfinite(score):
        return False
    return score >= threshold if direction == "ge" else score <= threshold


def _fit_regressor(
    train: pd.DataFrame,
    feature_columns: tuple[str, ...],
    target_column: str,
    config: EdgeModelConfig,
) -> Pipeline:
    numeric_columns = tuple(
        col for col in feature_columns
        if col not in _categorical_columns()
    )
    categorical_columns = tuple(
        col for col in _categorical_columns()
        if col in feature_columns
    )
    preprocessor = ColumnTransformer(
        transformers=[
            ("num", SimpleImputer(strategy="median"), list(numeric_columns)),
            (
                "cat",
                OneHotEncoder(handle_unknown="ignore", sparse_output=False),
                list(categorical_columns),
            ),
        ],
        remainder="drop",
    )
    model = HistGradientBoostingRegressor(
        learning_rate=0.05,
        max_iter=150,
        max_leaf_nodes=15,
        l2_regularization=0.05,
        random_state=config.random_state,
    )
    pipe = Pipeline([("features", preprocessor), ("model", model)])
    pipe.fit(train[list(feature_columns)], train[target_column])
    return pipe


def _feature_columns(dataset: pd.DataFrame) -> tuple[str, ...]:
    blocked = {
        "event_id",
        "ts",
        "maker_net_bps",
        "taker_net_bps",
    }
    columns = [
        col for col in dataset.columns
        if col not in blocked and dataset[col].notna().any()
    ]
    return tuple(columns)


def _categorical_columns() -> tuple[str, ...]:
    return ("exchange", "symbol", "timeframe", "strategy_id", "source_id", "side")


def _parse_reason_features(reason: str) -> dict[str, float]:
    out: dict[str, float] = {}
    if not reason:
        return out
    ls = re.search(r"\bL/S=([+-]?\d+(?:\.\d+)?)/([+-]?\d+(?:\.\d+)?)", reason)
    if ls:
        out["long_score"] = float(ls.group(1))
        out["short_score"] = float(ls.group(2))
    for key, raw in re.findall(
        r"\b([A-Za-z][A-Za-z0-9_./-]*)=([+-]?\d+(?:\.\d+)?)%?",
        reason,
    ):
        if "/" in key:
            continue
        out[_clean_name(key)] = float(raw)
    feature_match = re.search(r"\bfeatures=([^;]+)", reason)
    if feature_match:
        for token in feature_match.group(1).split(","):
            token = _clean_name(token.strip())
            if token and token != "none":
                out[f"feature_{token}"] = 1.0
    return out


def _verdict(
    *,
    dataset: pd.DataFrame,
    routed: tuple[ModelRoutedOpportunity, ...],
    raw_avg: float | None,
    model_avg: float | None,
    improvement: float | None,
    model_pf: float | None,
    blocker: str | None,
    config: EdgeModelConfig,
    train_samples: int,
    test_samples: int,
) -> tuple[ModelVerdict, str]:
    if dataset.empty:
        return "NO_OPPORTUNITIES", blocker or "no opportunities"
    if blocker is not None:
        return "UNDER_SAMPLED", blocker
    if train_samples < config.min_train_samples or test_samples < config.min_test_samples:
        return "UNDER_SAMPLED", "not enough chronological samples"
    model_trades = tuple(row for row in routed if row.routed)
    if not model_trades:
        return "NO_MODEL_SELECTION", "model skipped every OOS opportunity"
    if len(model_trades) < config.min_model_trades:
        return "UNDER_SAMPLED", (
            f"only {len(model_trades)} model trades; need >= {config.min_model_trades}"
        )
    if improvement is None or improvement < config.min_improvement_bps:
        return "MODEL_NO_IMPROVEMENT", "model did not improve OOS average net"
    if model_avg is not None and model_avg >= config.min_predicted_net_bps and (
        model_pf or 0.0
    ) >= config.min_profit_factor:
        return "MODEL_PAPER_CANDIDATE", "model clears net/PF paper-candidate gate"
    return "MODEL_IMPROVED", "model improves OOS but does not clear paper gate"


def _profit_factor(values: Iterable[float | None]) -> float | None:
    vals = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    wins = [v for v in vals if v > 0]
    losses = [-v for v in vals if v < 0]
    if wins and losses:
        return sum(wins) / sum(losses)
    if wins:
        return 999.0
    return None


def _win_rate(values: Iterable[float | None]) -> float:
    vals = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    if not vals:
        return 0.0
    return round(sum(1 for value in vals if value > 0) / len(vals) * 100.0, 2)


def _finite_or_none(value: object) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _subtract(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    return left - right


def _safe_div(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def _round_or_none(value: float | None) -> float | None:
    if value is None or not math.isfinite(float(value)):
        return None
    return round(float(value), 4)


def _timeframe_seconds(timeframe: str) -> int | None:
    match = re.fullmatch(r"(\d+)([mhdw])", str(timeframe).strip().lower())
    if not match:
        return None
    value = int(match.group(1))
    unit = match.group(2)
    multiplier = {"m": 60, "h": 3_600, "d": 86_400, "w": 604_800}[unit]
    return value * multiplier


def _timeframe_sort_key(timeframe: str) -> tuple[int, str]:
    seconds = _timeframe_seconds(timeframe)
    return (seconds if seconds is not None else 10**12, timeframe)


def _clean_name(value: object) -> str:
    return re.sub(r"[^a-zA-Z0-9_]+", "_", str(value).strip()).strip("_").lower()


def _split_csv(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _instantiate_strategy(
    strategy_cls: type[BaseStrategy],
    store: ParquetStore,
    exchange: str,
    symbol: str,
) -> BaseStrategy:
    params = inspect.signature(strategy_cls).parameters
    if "funding" not in params:
        return strategy_cls()
    try:
        funding = store.read_funding(exchange, symbol)
    except FileNotFoundError:
        funding = None
    if funding is None and params["funding"].default is inspect.Signature.empty:
        raise FileNotFoundError(
            f"{strategy_cls.strategy_id} requires funding data for {exchange}:{symbol}"
        )
    return strategy_cls(funding=funding)


def _render_report(report: dict) -> str:
    s = report["summary"]
    return "\n".join(
        [
            "edge model v1 backtest",
            "policy=research_only can_trade=false can_promote=false",
            "",
            f"verdict={s['verdict']} blocker={s['primary_blocker']}",
            (
                f"opportunities={s['opportunities']} train={s['train_samples']} "
                f"test={s['test_samples']} model_trades={s['model_trades']} "
                f"selection={s['selection_rate_pct']}%"
            ),
            (
                f"raw_avg_net_bps={s['raw_avg_net_bps']} raw_pf={s['raw_profit_factor']} "
                f"raw_win={s['raw_win_rate_pct']}%"
            ),
            (
                f"model_avg_net_bps={s['model_avg_net_bps']} "
                f"model_pf={s['model_profit_factor']} "
                f"model_win={s['model_win_rate_pct']}% "
                f"improvement_bps={s['improvement_bps']}"
            ),
            f"mae maker/taker={s['maker_mae_bps']}/{s['taker_mae_bps']}",
            f"actions={s['action_counts']}",
        ]
    )


def _render_matrix_report(payload: dict) -> str:
    summary = payload["summary"]
    lines = [
        "edge model v1 timeframe matrix",
        "policy=research_only can_trade=false can_promote=false",
        "",
        (
            f"timeframes={summary['timeframe_count']} "
            f"opportunities={summary['total_opportunities']} "
            f"best={summary['best_timeframe']} verdict={summary['best_verdict']}"
        ),
        "",
        "scope          verdict                 opp train/test trades raw_bps model_bps pf     imp_bps blocker",
    ]
    for report in payload["reports"]:
        scope = report.get("scope", {}).get("id", "unknown")
        s = report["summary"]
        lines.append(
            f"{scope:<14} {s['verdict']:<23} {s['opportunities']:>5} "
            f"{s['train_samples']:>5}/{s['test_samples']:<5} "
            f"{s['model_trades']:>6} {_fmt(s['raw_avg_net_bps']):>7} "
            f"{_fmt(s['model_avg_net_bps']):>9} {_fmt(s['model_profit_factor']):>6} "
            f"{_fmt(s['improvement_bps']):>7} {s['primary_blocker']}"
        )
    if not payload["reports"]:
        lines.append("no reports produced; check candles, symbols, and strategy ids")
    return "\n".join(lines)


def _fmt(value: float | None) -> str:
    return "--" if value is None else f"{float(value):.2f}"


def _compact_routes(report: dict) -> dict:
    compact = dict(report)
    routes = tuple(compact.get("routes") or ())
    compact["routes_omitted"] = len(routes)
    compact["routes"] = []
    return compact


def _compact_matrix(payload: dict) -> dict:
    compact = dict(payload)
    compact["reports"] = [_compact_routes(report) for report in payload["reports"]]
    return compact


def _resolve_timeframes(timeframe: str, timeframes: str | None) -> tuple[str, ...]:
    raw = _split_csv(timeframes) if timeframes else (timeframe,)
    if any(item.lower() == "all" for item in raw):
        return DEFAULT_EDGE_MODEL_TIMEFRAMES
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        if item not in seen:
            out.append(item)
            seen.add(item)
    return tuple(out)


def _expand_targets(
    targets: Iterable[ResearchTarget],
    timeframes: Iterable[str],
) -> tuple[ResearchTarget, ...]:
    out: list[ResearchTarget] = []
    seen: set[str] = set()
    for target in targets:
        for timeframe in timeframes:
            expanded = ResearchTarget(target.exchange, target.symbol, timeframe)
            if expanded.key not in seen:
                out.append(expanded)
                seen.add(expanded.key)
    return tuple(out)


def _target_data_coverage(data_root: Path | str, targets: Iterable[ResearchTarget]) -> dict:
    store = ParquetStore(data_root)
    available: list[dict] = []
    missing: list[dict] = []
    for target in targets:
        item = asdict(target)
        if store.candles_path(target.exchange, target.symbol, target.timeframe).exists():
            available.append(item)
        else:
            missing.append(item)
    return {
        "attempted": len(available) + len(missing),
        "available": len(available),
        "missing": len(missing),
        "available_targets": available,
        "missing_targets": missing,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="train/test edge_model_v1 on scanner opportunity rows"
    )
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--exchange", help="single exchange id")
    parser.add_argument("--symbol", help="single symbol")
    parser.add_argument("--exchanges", help="comma-separated exchange ids")
    parser.add_argument("--symbols", help="comma-separated symbols")
    parser.add_argument("--timeframe", default="15m")
    parser.add_argument(
        "--timeframes",
        help="comma-separated timeframes or 'all' for 1m,5m,15m,1h,4h",
    )
    parser.add_argument("--strategies", default=",".join(DEFAULT_SCALPER_STRATEGIES))
    parser.add_argument("--lookback-days", type=int, default=30)
    parser.add_argument("--horizon-bars", type=int, default=8)
    parser.add_argument("--train-fraction", type=float, default=0.70)
    parser.add_argument("--min-train-samples", type=int, default=100)
    parser.add_argument("--min-test-samples", type=int, default=50)
    parser.add_argument("--min-model-trades", type=int, default=20)
    parser.add_argument("--min-edge-bps", type=float, default=25.0)
    parser.add_argument("--min-profit-factor", type=float, default=1.5)
    parser.add_argument(
        "--matrix",
        action="store_true",
        help="emit aggregate plus per-timeframe reports",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="omit per-opportunity OOS routes from JSON output",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--output", help="optional JSON report path")
    args = parser.parse_args(argv)

    timeframes = _resolve_timeframes(args.timeframe, args.timeframes)
    if args.exchange and args.symbol:
        targets = tuple(
            ResearchTarget(args.exchange, args.symbol, timeframe)
            for timeframe in timeframes
        )
    else:
        base_targets = load_research_targets(
            exchanges=_split_csv(args.exchanges) or None,
            symbols=_split_csv(args.symbols) or None,
            timeframe=timeframes[0],
        )
        targets = _expand_targets(base_targets, timeframes)
    router_config = OpportunityRouterConfig(
        horizon_bars=args.horizon_bars,
        min_samples=args.min_model_trades,
        min_expected_net_edge_bps=args.min_edge_bps,
        min_profit_factor=args.min_profit_factor,
        default_lookback_days=args.lookback_days,
    )
    model_config = EdgeModelConfig(
        train_fraction=args.train_fraction,
        min_train_samples=args.min_train_samples,
        min_test_samples=args.min_test_samples,
        min_model_trades=args.min_model_trades,
        min_predicted_net_bps=args.min_edge_bps,
        min_profit_factor=args.min_profit_factor,
    )
    opportunities = load_strategy_opportunities(
        data_root=args.data_root,
        targets=targets,
        strategy_ids=_split_csv(args.strategies),
        lookback_days=args.lookback_days,
        router_config=router_config,
    )
    use_matrix = args.matrix or len(timeframes) > 1
    if use_matrix:
        report = backtest_edge_model_timeframe_matrix(opportunities, config=model_config)
    else:
        report = backtest_edge_model(opportunities, config=model_config)
    report["scope"] = {
        "targets": [asdict(target) for target in targets],
        "target_coverage": _target_data_coverage(args.data_root, targets),
        "strategies": list(_split_csv(args.strategies)),
        "lookback_days": args.lookback_days,
        "horizon_bars": args.horizon_bars,
        "timeframes": list(timeframes),
    }
    if args.compact:
        report = _compact_matrix(report) if use_matrix else _compact_routes(report)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(report, indent=2, sort_keys=True))
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    elif use_matrix:
        print(_render_matrix_report(report))
    else:
        print(_render_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
