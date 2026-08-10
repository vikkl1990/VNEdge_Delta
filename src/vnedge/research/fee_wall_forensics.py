"""Batch fee-wall forensics for scanner opportunity families.

The execution router answers one target/strategy at a time. This module is the
operator-facing batch layer: sweep selected venues, symbols, timeframes, and
scanner families; persist every compact lane report; and optionally write every
opportunity route row to JSONL for later model training.

Research-only. It never places orders, never promotes lanes, and never weakens
the runtime risk gateway.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from typing import Callable, Iterable, Literal

import pandas as pd

from vnedge.data.parquet_store import ParquetStore
from vnedge.research.execution_edge_router import (
    DEFAULT_SCALPER_STRATEGIES,
    OpportunityRoute,
    OpportunityRouterConfig,
    build_router_report,
    label_strategy_opportunities,
)
from vnedge.research.universe import ResearchTarget, load_research_targets
from vnedge.strategy.strategy_registry import get_strategy_class

ProgressStatus = Literal["running", "completed", "failed"]

DEFAULT_FORENSICS_TIMEFRAMES = ("5m", "15m", "1h", "4h")
DEFAULT_HORIZON_BY_TIMEFRAME = {
    "1m": 24,
    "5m": 12,
    "15m": 8,
    "1h": 6,
    "4h": 3,
}
DEFAULT_SELECTED_STRATEGIES = (
    "vnedge_algo_ml_pro_v1",
    "luxara_live_plan_qtm_v1",
    "luxara_break_bounce_v27_v1",
    "sats_5m_scalper_v1",
    "stealth_trail_bbp_v1",
    "fvg_liquidity_breakout_v1",
    "luxy_ut_bot_forecast_v1",
    "momentum_cascade_lyro_v1",
    "context_scalper_v2",
    "quantified_fee_wall_sniper_v1",
)


def run_fee_wall_forensics(
    *,
    data_root: Path | str,
    targets: Iterable[ResearchTarget],
    strategy_ids: Iterable[str] = DEFAULT_SELECTED_STRATEGIES,
    lookback_days: int = 30,
    horizon_by_timeframe: dict[str, int] | None = None,
    min_samples: int = 10,
    min_edge_bps: float = 8.0,
    min_profit_factor: float = 1.15,
    maker_fill_probability: float = 0.60,
    paper_margin_usd: float = 100.0,
    paper_leverage: float = 25.0,
    include_opportunities: bool = False,
    progress_callback: Callable[[dict], None] | None = None,
    route_sink: Callable[[OpportunityRoute], None] | None = None,
) -> dict:
    """Run batch forensics and return a compact report.

    The latest JSON intentionally stores lane summaries, not every opportunity,
    unless `include_opportunities` is set. Use `route_sink` to stream full route
    rows to JSONL without building a giant in-memory payload.
    """

    targets_tuple = tuple(targets)
    strategy_tuple = tuple(strategy_ids)
    horizons = dict(DEFAULT_HORIZON_BY_TIMEFRAME)
    horizons.update(horizon_by_timeframe or {})
    store = ParquetStore(data_root)
    reports: list[dict] = []
    errors: list[dict] = []
    total_work_units = len(targets_tuple) * len(strategy_tuple)
    completed_work_units = 0

    for target in targets_tuple:
        try:
            candles = _window(
                store.read_candles(target.exchange, target.symbol, target.timeframe),
                lookback_days,
            )
        except FileNotFoundError as exc:
            for strategy_id in strategy_tuple:
                errors.append(_error_record(target, strategy_id, str(exc), "missing_candles"))
                completed_work_units += 1
                _publish_progress(
                    progress_callback,
                    phase="missing_candles",
                    target=target,
                    strategy_id=strategy_id,
                    total_work_units=total_work_units,
                    completed_work_units=completed_work_units,
                    rows=0,
                    routes=0,
                    last_error="missing candle parquet",
                )
            continue

        for strategy_id in strategy_tuple:
            _publish_progress(
                progress_callback,
                phase="labeling_opportunities",
                target=target,
                strategy_id=strategy_id,
                total_work_units=total_work_units,
                completed_work_units=completed_work_units,
                rows=len(candles),
                routes=0,
                last_error=None,
            )
            config = _config_for_timeframe(
                target.timeframe,
                horizons,
                min_samples=min_samples,
                min_edge_bps=min_edge_bps,
                min_profit_factor=min_profit_factor,
                maker_fill_probability=maker_fill_probability,
                lookback_days=lookback_days,
                paper_margin_usd=paper_margin_usd,
                paper_leverage=paper_leverage,
            )
            last_error: str | None = None
            try:
                strategy = _instantiate_strategy(
                    get_strategy_class(strategy_id),
                    store,
                    target.exchange,
                    target.symbol,
                )
                routes = tuple(
                    _attach_target_metadata(route, target)
                    for route in label_strategy_opportunities(
                        candles,
                        strategy,
                        exchange=target.exchange,
                        config=config,
                    )
                )
            except Exception as exc:
                last_error = str(exc)
                errors.append(_error_record(target, strategy_id, last_error, "strategy_error"))
                routes = ()

            if route_sink is not None:
                for route in routes:
                    route_sink(route)
            report = build_router_report(
                exchange=target.exchange,
                symbol=target.symbol,
                timeframe=target.timeframe,
                strategy_id=strategy_id,
                opportunities=routes,
                config=config,
            )
            reports.append(_compact_router_report(report, include_opportunities))
            completed_work_units += 1
            _publish_progress(
                progress_callback,
                phase="labeling_opportunities",
                target=target,
                strategy_id=strategy_id,
                total_work_units=total_work_units,
                completed_work_units=completed_work_units,
                rows=len(candles),
                routes=len(routes),
                last_error=last_error,
            )

    return build_fee_wall_forensics_report(
        reports,
        errors=errors,
        targets=targets_tuple,
        strategy_ids=strategy_tuple,
        lookback_days=lookback_days,
        horizon_by_timeframe=horizons,
        min_samples=min_samples,
        min_edge_bps=min_edge_bps,
        min_profit_factor=min_profit_factor,
        include_opportunities=include_opportunities,
        paper_margin_usd=paper_margin_usd,
        paper_leverage=paper_leverage,
    )


def build_fee_wall_forensics_report(
    reports: Iterable[dict],
    *,
    errors: Iterable[dict] = (),
    targets: Iterable[ResearchTarget] = (),
    strategy_ids: Iterable[str] = (),
    lookback_days: int = 30,
    horizon_by_timeframe: dict[str, int] | None = None,
    min_samples: int = 10,
    min_edge_bps: float = 8.0,
    min_profit_factor: float = 1.15,
    include_opportunities: bool = False,
    paper_margin_usd: float = 100.0,
    paper_leverage: float = 25.0,
    max_top: int = 25,
) -> dict:
    report_rows = tuple(reports)
    error_rows = tuple(errors)
    targets_tuple = tuple(targets)
    strategies_tuple = tuple(strategy_ids)
    top = _top_reports(report_rows, max_top=max_top)
    sparse = _sparse_positive_reports(report_rows)
    exit_salvage = _exit_salvage_reports(report_rows)
    strict_candidates = _strict_candidate_reports(report_rows)
    summary = _summary(
        report_rows,
        error_rows,
        strict_candidates=strict_candidates,
        sparse=sparse,
        exit_salvage=exit_salvage,
    )
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "truth_layer": "fee_wall_forensics_v1",
        "policy": {
            "research_only": True,
            "can_trade": False,
            "can_promote": False,
            "requires_untouched_judgment": True,
            "decision_uses_forward_truth": False,
            "live_governance_unchanged": True,
            "operator_note": (
                "This artifact identifies fee-wall-breaking movement, sparse "
                "sample pockets, and exit failures. It is evidence for the next "
                "research experiment only, not a trading permission."
            ),
        },
        "config": {
            "lookback_days": lookback_days,
            "horizon_by_timeframe": horizon_by_timeframe or DEFAULT_HORIZON_BY_TIMEFRAME,
            "min_samples": min_samples,
            "min_edge_bps": min_edge_bps,
            "min_profit_factor": min_profit_factor,
            "include_opportunities": include_opportunities,
            "paper_margin_usd": paper_margin_usd,
            "paper_leverage": paper_leverage,
            "paper_notional_usd": paper_margin_usd * paper_leverage,
        },
        "scope": {
            "target_count": len(targets_tuple),
            "strategy_count": len(strategies_tuple),
            "targets": [asdict(target) for target in targets_tuple],
            "strategies": list(strategies_tuple),
        },
        "summary": summary,
        "top": top,
        "strict_fee_wall_candidates": strict_candidates[:25],
        "sample_expansion_candidates": sparse[:25],
        "exit_salvage_candidates": exit_salvage[:25],
        "reports": list(report_rows),
        "errors": list(error_rows),
    }


def build_fee_wall_forensics_progress(
    *,
    status: ProgressStatus,
    phase: str,
    started_at: str,
    targets: Iterable[ResearchTarget],
    strategy_ids: Iterable[str],
    lookback_days: int,
    completed_work_units: int = 0,
    total_work_units: int | None = None,
    current_target: dict | ResearchTarget | None = None,
    current_strategy: str | None = None,
    rows: int | None = None,
    routes: int | None = None,
    output_path: Path | str | None = None,
    routes_output_path: Path | str | None = None,
    last_error: str | None = None,
) -> dict:
    targets_tuple = tuple(targets)
    strategies_tuple = tuple(strategy_ids)
    total = total_work_units
    if total is None:
        total = len(targets_tuple) * len(strategies_tuple)
    pct = (completed_work_units / total * 100.0) if total else 0.0
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "truth_layer": "fee_wall_forensics_progress_v1",
        "status": status,
        "phase": phase,
        "started_at": started_at,
        "progress_pct": round(min(max(pct, 0.0), 100.0), 2),
        "completed_work_units": completed_work_units,
        "total_work_units": total,
        "current_target": _target_payload(current_target),
        "current_strategy": current_strategy,
        "current_rows": rows,
        "current_routes": routes,
        "lookback_days": lookback_days,
        "output_path": str(output_path) if output_path is not None else None,
        "routes_output_path": (
            str(routes_output_path) if routes_output_path is not None else None
        ),
        "last_error": last_error,
        "can_trade": False,
        "can_promote": False,
        "policy": {
            "research_only": True,
            "can_trade": False,
            "can_promote": False,
            "live_governance_unchanged": True,
        },
    }


def publish_json(payload: dict, output: Path | str) -> None:
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    tmp_path.chmod(0o644)
    tmp_path.replace(output_path)
    output_path.chmod(0o644)


def append_feed(payload: dict, feed: Path | str) -> None:
    feed_path = Path(feed)
    feed_path.parent.mkdir(parents=True, exist_ok=True)
    with feed_path.open("a") as fh:
        fh.write(json.dumps(_feed_record(payload), sort_keys=True) + "\n")
    feed_path.chmod(0o644)


class JsonlRouteSink:
    """Small append-only route writer used by the CLI."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("w")

    def __call__(self, route: OpportunityRoute) -> None:
        self._fh.write(json.dumps(route.to_dict(), sort_keys=True) + "\n")

    def close(self) -> None:
        self._fh.close()
        self.path.chmod(0o644)


def _config_for_timeframe(
    timeframe: str,
    horizon_by_timeframe: dict[str, int],
    *,
    min_samples: int,
    min_edge_bps: float,
    min_profit_factor: float,
    maker_fill_probability: float,
    lookback_days: int,
    paper_margin_usd: float,
    paper_leverage: float,
) -> OpportunityRouterConfig:
    return OpportunityRouterConfig(
        horizon_bars=int(horizon_by_timeframe.get(timeframe, 12)),
        min_samples=min_samples,
        min_expected_net_edge_bps=min_edge_bps,
        min_profit_factor=min_profit_factor,
        maker_fill_probability=maker_fill_probability,
        default_lookback_days=lookback_days,
        paper_margin_usd=paper_margin_usd,
        paper_leverage=paper_leverage,
    )


def _compact_router_report(report: dict, include_opportunities: bool) -> dict:
    item = dict(report)
    opportunities = item.get("opportunities") or []
    item["opportunity_count"] = len(opportunities)
    if not include_opportunities:
        item["opportunities_omitted"] = len(opportunities)
        item["opportunities"] = []
    return item


def _attach_target_metadata(route: OpportunityRoute, target: ResearchTarget) -> OpportunityRoute:
    from dataclasses import replace

    meta = dict(route.metadata)
    meta.update(
        {
            "exchange": target.exchange,
            "symbol": target.symbol,
            "timeframe": target.timeframe,
        }
    )
    return replace(route, metadata=meta)


def _instantiate_strategy(strategy_cls, store: ParquetStore, exchange: str, symbol: str):
    import inspect

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


def _window(df: pd.DataFrame, lookback_days: int | None) -> pd.DataFrame:
    if lookback_days is None or lookback_days <= 0 or df.empty:
        return df.reset_index(drop=True)
    ts = pd.to_datetime(df["timestamp"], utc=True)
    cutoff = pd.Timestamp(ts.iloc[-1]) - pd.Timedelta(days=lookback_days)
    return df[ts >= cutoff].reset_index(drop=True)


def _publish_progress(callback: Callable[[dict], None] | None, **payload: object) -> None:
    if callback is None:
        return
    callback(dict(payload))


def _error_record(
    target: ResearchTarget,
    strategy_id: str,
    message: str,
    category: str,
) -> dict:
    return {
        "category": category,
        "exchange": target.exchange,
        "symbol": target.symbol,
        "timeframe": target.timeframe,
        "strategy_id": strategy_id,
        "message": message,
    }


def _summary(
    reports: tuple[dict, ...],
    errors: tuple[dict, ...],
    *,
    strict_candidates: list[dict],
    sparse: list[dict],
    exit_salvage: list[dict],
) -> dict:
    summaries = [report.get("summary", {}) for report in reports]
    routed_reports = [summary for summary in summaries if int(summary.get("routed") or 0) > 0]
    positive_avg = [
        summary
        for summary in routed_reports
        if _float_or_none(summary.get("avg_selected_net_bps")) is not None
        and float(summary["avg_selected_net_bps"]) > 0.0
    ]
    fee_breakers = [
        summary
        for summary in routed_reports
        if _float_or_none(summary.get("fee_wall_break_rate_pct")) is not None
        and float(summary["fee_wall_break_rate_pct"]) > 0.0
    ]
    route_counts = Counter()
    verdict_counts = Counter()
    diagnosis_counts = Counter()
    for summary in summaries:
        route_counts.update(summary.get("action_counts") or {})
        verdict_counts.update([str(summary.get("verdict") or "UNKNOWN")])
        diagnosis_counts.update(summary.get("exit_diagnosis_counts") or {})
    return {
        "reports": len(reports),
        "errors": len(errors),
        "route_rows": sum(int(report.get("opportunity_count") or 0) for report in reports),
        "routed_reports": len(routed_reports),
        "positive_avg_net_reports": len(positive_avg),
        "fee_wall_breaker_reports": len(fee_breakers),
        "strict_fee_wall_candidates": len(strict_candidates),
        "sample_expansion_candidates": len(sparse),
        "exit_salvage_candidates": len(exit_salvage),
        "route_counts": dict(route_counts),
        "verdict_counts": dict(verdict_counts),
        "exit_diagnosis_counts": dict(diagnosis_counts),
        "best_positive_avg_net_bps": _best_metric(positive_avg, "avg_selected_net_bps"),
        "best_positive_profit_factor": _best_metric(positive_avg, "profit_factor"),
        "can_trade": False,
        "can_promote": False,
    }


def _top_reports(reports: tuple[dict, ...], *, max_top: int) -> list[dict]:
    ranked = sorted(reports, key=_report_rank_key, reverse=True)
    return [_summary_card(report) for report in ranked[:max_top]]


def _strict_candidate_reports(reports: tuple[dict, ...]) -> list[dict]:
    cards: list[dict] = []
    for report in reports:
        summary = report.get("summary", {})
        if summary.get("verdict") in {"MAKER_EDGE", "TAKER_EDGE", "MIXED_ROUTE_EDGE"}:
            card = _summary_card(report)
            card["recommended_action"] = "PRE_REGISTER_UNTOUCHED_JUDGMENT_WINDOW"
            cards.append(card)
    return sorted(cards, key=_card_rank_key, reverse=True)


def _sparse_positive_reports(reports: tuple[dict, ...]) -> list[dict]:
    cards: list[dict] = []
    for report in reports:
        summary = report.get("summary", {})
        avg = _float_or_none(summary.get("avg_selected_net_bps"))
        routed = int(summary.get("routed") or 0)
        min_samples = int(report.get("config", {}).get("min_samples") or 0)
        if avg is not None and avg > 0.0 and 0 < routed < min_samples:
            card = _summary_card(report)
            card["recommended_action"] = "EXPAND_SAMPLE_OR_LOWER_TIMEFRAME_TRIGGER"
            cards.append(card)
    return sorted(cards, key=_card_rank_key, reverse=True)


def _exit_salvage_reports(reports: tuple[dict, ...]) -> list[dict]:
    cards: list[dict] = []
    for report in reports:
        summary = report.get("summary", {})
        diagnosis = summary.get("exit_diagnosis_counts") or {}
        gave_back = int(diagnosis.get("GAVE_BACK_FEE_WALL_MOVE") or 0)
        failed_capture = int(diagnosis.get("HORIZON_EXIT_FAILED_CAPTURE") or 0)
        mfe_after_cost = _float_or_none(summary.get("avg_mfe_after_cost_bps"))
        avg = _float_or_none(summary.get("avg_selected_net_bps"))
        if (
            (gave_back > 0 or failed_capture > 0)
            and mfe_after_cost is not None
            and mfe_after_cost > 0.0
            and (avg is None or avg <= 0.0)
        ):
            card = _summary_card(report)
            card["recommended_action"] = "REBUILD_EXIT_TRAIL_OR_EARLIER_TARGET_CAPTURE"
            cards.append(card)
    return sorted(cards, key=_card_rank_key, reverse=True)


def _summary_card(report: dict) -> dict:
    summary = report.get("summary", {})
    return {
        "exchange": report.get("exchange"),
        "symbol": report.get("symbol"),
        "timeframe": report.get("timeframe"),
        "strategy": report.get("strategy"),
        "verdict": summary.get("verdict"),
        "routed": summary.get("routed"),
        "opportunities": summary.get("opportunities"),
        "avg_selected_net_bps": summary.get("avg_selected_net_bps"),
        "profit_factor": summary.get("profit_factor"),
        "win_rate_pct": summary.get("win_rate_pct"),
        "fee_wall_break_rate_pct": summary.get("fee_wall_break_rate_pct"),
        "avg_mfe_after_cost_bps": summary.get("avg_mfe_after_cost_bps"),
        "avg_hold_bars": summary.get("avg_hold_bars"),
        "avg_time_to_mfe_bars": summary.get("avg_time_to_mfe_bars"),
        "avg_capture_ratio": summary.get("avg_capture_ratio"),
        "paper_margin_usd": summary.get("paper_margin_usd"),
        "paper_leverage": summary.get("paper_leverage"),
        "paper_notional_usd": summary.get("paper_notional_usd"),
        "selected_net_usd": summary.get("selected_net_usd"),
        "selected_gross_usd": summary.get("selected_gross_usd"),
        "exit_diagnosis_counts": summary.get("exit_diagnosis_counts"),
        "primary_blocker": summary.get("primary_blocker"),
        "can_trade": False,
        "can_promote": False,
    }


def _report_rank_key(report: dict) -> tuple[float, float, float, float]:
    summary = report.get("summary", {})
    avg = _float_or_none(summary.get("avg_selected_net_bps"))
    pf = _float_or_none(summary.get("profit_factor"))
    fee_break = _float_or_none(summary.get("fee_wall_break_rate_pct"))
    routed = int(summary.get("routed") or 0)
    return (
        avg if avg is not None else -10**9,
        min(pf if pf is not None else 0.0, 999.0),
        fee_break if fee_break is not None else 0.0,
        float(routed),
    )


def _card_rank_key(card: dict) -> tuple[float, float, float]:
    avg = _float_or_none(card.get("avg_selected_net_bps"))
    fee_break = _float_or_none(card.get("fee_wall_break_rate_pct"))
    routed = int(card.get("routed") or 0)
    return (
        avg if avg is not None else -10**9,
        fee_break if fee_break is not None else 0.0,
        float(routed),
    )


def _best_metric(rows: Iterable[dict], key: str) -> float | None:
    vals = [
        float(row[key])
        for row in rows
        if _float_or_none(row.get(key)) is not None
    ]
    if not vals:
        return None
    return round(max(vals), 4)


def _float_or_none(value: object) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _target_payload(target: dict | ResearchTarget | None) -> dict | None:
    if target is None:
        return None
    if isinstance(target, ResearchTarget):
        return asdict(target)
    return dict(target)


def _feed_record(payload: dict) -> dict:
    summary = payload.get("summary", {})
    top = (payload.get("top") or [{}])[0]
    return {
        "generated_at": payload.get("generated_at"),
        "truth_layer": payload.get("truth_layer"),
        "reports": summary.get("reports"),
        "routed_reports": summary.get("routed_reports"),
        "positive_avg_net_reports": summary.get("positive_avg_net_reports"),
        "fee_wall_breaker_reports": summary.get("fee_wall_breaker_reports"),
        "sample_expansion_candidates": summary.get("sample_expansion_candidates"),
        "exit_salvage_candidates": summary.get("exit_salvage_candidates"),
        "best_strategy": top.get("strategy"),
        "best_exchange": top.get("exchange"),
        "best_symbol": top.get("symbol"),
        "best_timeframe": top.get("timeframe"),
        "best_avg_selected_net_bps": top.get("avg_selected_net_bps"),
        "best_profit_factor": top.get("profit_factor"),
        "can_trade": False,
        "can_promote": False,
    }


def _split_csv(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _parse_horizon_map(raw: str | None) -> dict[str, int]:
    if not raw:
        return dict(DEFAULT_HORIZON_BY_TIMEFRAME)
    out = dict(DEFAULT_HORIZON_BY_TIMEFRAME)
    for item in _split_csv(raw):
        if "=" not in item:
            raise ValueError(f"invalid horizon item {item!r}; expected timeframe=bars")
        timeframe, bars = item.split("=", maxsplit=1)
        out[timeframe.strip()] = int(bars)
    return out


def _resolve_timeframes(raw: str | None) -> tuple[str, ...]:
    values = _split_csv(raw) or DEFAULT_FORENSICS_TIMEFRAMES
    if any(value.lower() == "all" for value in values):
        values = ("1m", "5m", "15m", "1h", "4h")
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value not in seen:
            out.append(value)
            seen.add(value)
    return tuple(out)


def _expand_targets(
    base_targets: Iterable[ResearchTarget],
    timeframes: Iterable[str],
) -> tuple[ResearchTarget, ...]:
    out: list[ResearchTarget] = []
    seen: set[str] = set()
    for target in base_targets:
        for timeframe in timeframes:
            expanded = ResearchTarget(target.exchange, target.symbol, timeframe)
            if expanded.key not in seen:
                out.append(expanded)
                seen.add(expanded.key)
    return tuple(out)


def _load_targets(args: argparse.Namespace, timeframes: tuple[str, ...]) -> tuple[ResearchTarget, ...]:
    if args.exchange and args.symbol:
        base = (ResearchTarget(args.exchange, args.symbol, timeframes[0]),)
    else:
        base = load_research_targets(
            exchanges=_split_csv(args.exchanges) or None,
            symbols=_split_csv(args.symbols) or None,
            timeframe=timeframes[0],
        )
    if args.max_targets is not None:
        base = tuple(base)[: args.max_targets]
    return _expand_targets(base, timeframes)


def _render_report(payload: dict) -> str:
    summary = payload["summary"]
    lines = [
        "fee-wall forensics v1",
        "policy=research_only can_trade=false can_promote=false",
        "",
        (
            f"reports={summary['reports']} errors={summary['errors']} "
            f"route_rows={summary['route_rows']} "
            f"positive_avg={summary['positive_avg_net_reports']} "
            f"fee_breakers={summary['fee_wall_breaker_reports']} "
            f"sparse={summary['sample_expansion_candidates']}"
        ),
        "",
        "rank avg_bps pf     routed fee% hold tMFE cap   scanner/venue/symbol/timeframe",
    ]
    for idx, row in enumerate(payload.get("top", [])[:20], start=1):
        lines.append(
            f"{idx:>4} {_fmt(row.get('avg_selected_net_bps')):>7} "
            f"{_fmt(row.get('profit_factor')):>6} "
            f"{int(row.get('routed') or 0):>6} "
            f"{_fmt(row.get('fee_wall_break_rate_pct')):>5} "
            f"{_fmt(row.get('avg_hold_bars')):>4} "
            f"{_fmt(row.get('avg_time_to_mfe_bars')):>4} "
            f"{_fmt_ratio(row.get('avg_capture_ratio')):>5} "
            f"{row.get('strategy')} {row.get('exchange')} "
            f"{row.get('symbol')} {row.get('timeframe')}"
        )
    if not payload.get("top"):
        lines.append("no reports produced; check candle coverage")
    return "\n".join(lines)


def _fmt(value: object) -> str:
    f = _float_or_none(value)
    return "--" if f is None else f"{f:.2f}"


def _fmt_ratio(value: object) -> str:
    f = _float_or_none(value)
    return "--" if f is None else f"{f:.2f}x"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="batch fee-wall forensics across scanner families"
    )
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--exchange", help="single exchange id")
    parser.add_argument("--symbol", help="single symbol")
    parser.add_argument("--exchanges", help="comma-separated exchange ids")
    parser.add_argument("--symbols", help="comma-separated symbols")
    parser.add_argument(
        "--timeframes",
        default=",".join(DEFAULT_FORENSICS_TIMEFRAMES),
        help="comma-separated timeframes or 'all'",
    )
    parser.add_argument("--strategies", default=",".join(DEFAULT_SELECTED_STRATEGIES))
    parser.add_argument("--lookback-days", type=int, default=30)
    parser.add_argument(
        "--horizon-by-timeframe",
        default=",".join(
            f"{tf}={bars}" for tf, bars in DEFAULT_HORIZON_BY_TIMEFRAME.items()
        ),
    )
    parser.add_argument("--min-samples", type=int, default=10)
    parser.add_argument("--min-edge-bps", type=float, default=8.0)
    parser.add_argument("--min-profit-factor", type=float, default=1.15)
    parser.add_argument("--maker-fill-probability", type=float, default=0.60)
    parser.add_argument("--paper-margin-usd", type=float, default=100.0)
    parser.add_argument("--paper-leverage", type=float, default=25.0)
    parser.add_argument("--max-targets", type=int)
    parser.add_argument("--include-opportunities", action="store_true")
    parser.add_argument(
        "--output",
        default="research/live_research/fee_wall_forensics_latest.json",
    )
    parser.add_argument(
        "--feed",
        default="research/live_research/fee_wall_forensics_feed.jsonl",
    )
    parser.add_argument(
        "--progress",
        default="research/live_research/fee_wall_forensics_progress.json",
    )
    parser.add_argument(
        "--routes-output",
        default="research/live_research/fee_wall_forensics_routes_latest.jsonl",
    )
    parser.add_argument("--interval-seconds", type=int, default=0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    while True:
        started_at = datetime.now(UTC).isoformat()
        timeframes = _resolve_timeframes(args.timeframes)
        targets = _load_targets(args, timeframes)
        strategies = _split_csv(args.strategies)
        horizons = _parse_horizon_map(args.horizon_by_timeframe)
        total_work_units = len(targets) * len(strategies)
        last_event: dict = {
            "phase": "initializing",
            "completed_work_units": 0,
            "total_work_units": total_work_units,
            "target": None,
            "strategy_id": None,
            "rows": None,
            "routes": None,
            "last_error": None,
        }

        def publish_state(
            status: ProgressStatus,
            phase: str,
            event: dict | None = None,
            last_error: str | None = None,
        ) -> None:
            payload = event or last_event
            publish_json(
                build_fee_wall_forensics_progress(
                    status=status,
                    phase=phase,
                    started_at=started_at,
                    targets=targets,
                    strategy_ids=strategies,
                    lookback_days=args.lookback_days,
                    completed_work_units=int(payload.get("completed_work_units") or 0),
                    total_work_units=int(payload.get("total_work_units") or total_work_units),
                    current_target=payload.get("target"),
                    current_strategy=payload.get("strategy_id"),
                    rows=payload.get("rows"),
                    routes=payload.get("routes"),
                    output_path=args.output,
                    routes_output_path=args.routes_output,
                    last_error=last_error or payload.get("last_error"),
                ),
                args.progress,
            )

        def progress_callback(event: dict) -> None:
            nonlocal last_event
            last_event = dict(event)
            publish_state(
                "running",
                str(event.get("phase") or "running"),
                event=last_event,
            )

        route_sink = JsonlRouteSink(args.routes_output)
        publish_state("running", "initializing")
        try:
            report = run_fee_wall_forensics(
                data_root=args.data_root,
                targets=targets,
                strategy_ids=strategies,
                lookback_days=args.lookback_days,
                horizon_by_timeframe=horizons,
                min_samples=args.min_samples,
                min_edge_bps=args.min_edge_bps,
                min_profit_factor=args.min_profit_factor,
                maker_fill_probability=args.maker_fill_probability,
                paper_margin_usd=args.paper_margin_usd,
                paper_leverage=args.paper_leverage,
                include_opportunities=args.include_opportunities,
                progress_callback=progress_callback,
                route_sink=route_sink,
            )
            route_sink.close()
            publish_json(report, args.output)
            append_feed(report, args.feed)
            last_event = {
                **last_event,
                "phase": "published_report",
                "completed_work_units": total_work_units,
                "total_work_units": total_work_units,
                "target": None,
                "strategy_id": None,
                "routes": report.get("summary", {}).get("route_rows"),
                "last_error": None,
            }
            publish_state("completed", "published_report")
        except Exception as exc:
            route_sink.close()
            publish_state("failed", "failed", last_error=str(exc))
            raise

        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(_render_report(report), flush=True)
        if args.interval_seconds <= 0:
            return 0
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
