"""Alpha Arena Lite for scanner sample-expansion candidates.

The scanner uplift report is intentionally conservative: it can say a row is
interesting, sparse, near the fee wall, or rejected, but it does not own the
follow-through. This module turns those research-only experiments into durable
Quant OS tasks and scorecards so the operator can see the next proof step
without mistaking sparse positives for paper-ready lanes.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import hashlib
import json
import math
from pathlib import Path
from tempfile import NamedTemporaryFile
import time
from typing import Any, Iterable

from vnedge.agent_gateway.task_registry import (
    QuantOSAgentGateway,
    env_quant_os_agent_gateway_dir,
)

ALPHA_ARENA_LITE_ID = "alpha_arena_lite_v1"
DEFAULT_UPLIFT = Path("research/live_research/scanner_backtest_uplift_latest.json")
DEFAULT_SCANNER = Path("research/live_research/scanner_tournament_latest.json")
DEFAULT_FEE_WALL = Path("research/live_research/fee_wall_forensics_latest.json")
DEFAULT_OUT = Path("research/live_research/alpha_arena_lite_latest.json")
DEFAULT_FEED = Path("research/live_research/alpha_arena_lite_feed.jsonl")


@dataclass(frozen=True)
class AlphaArenaGateConfig:
    min_net_bps: float = 25.0
    min_profit_factor: float = 1.50
    min_trades: int = 20
    max_scorecards: int = 12

    def __post_init__(self) -> None:
        if self.min_net_bps <= 0:
            raise ValueError("min_net_bps must be positive")
        if self.min_profit_factor < 1.0:
            raise ValueError("min_profit_factor must be >= 1")
        if self.min_trades < 1:
            raise ValueError("min_trades must be >= 1")
        if self.max_scorecards < 1:
            raise ValueError("max_scorecards must be >= 1")


def run_alpha_arena_lite(
    *,
    uplift_payload: dict[str, Any],
    scanner_payload: dict[str, Any] | None = None,
    fee_wall_payload: dict[str, Any] | None = None,
    config: AlphaArenaGateConfig = AlphaArenaGateConfig(),
    gateway_dir: Path | str | None = None,
    sync_gateway: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    generated = now or datetime.now(UTC)
    scanner_index = _scanner_candidate_index(scanner_payload or {})
    top_rows = _top_rows(uplift_payload)
    strict_scorecards = _scorecards_from_strict_candidates(
        uplift_payload=uplift_payload,
        fee_wall_payload=fee_wall_payload or {},
        scanner_index=scanner_index,
        config=config,
    )
    experiment_scorecards = _scorecards_from_experiments(
        uplift_payload.get("experiments") or [],
        top_rows=top_rows,
        scanner_index=scanner_index,
        config=config,
    )
    scorecards = _dedupe_scorecards(
        [*strict_scorecards, *experiment_scorecards],
        max_scorecards=config.max_scorecards,
    )
    gateway_summary = (
        _sync_gateway(scorecards, gateway_dir=gateway_dir)
        if sync_gateway
        else _gateway_summary_disabled(gateway_dir)
    )
    payload = {
        "arena_id": ALPHA_ARENA_LITE_ID,
        "generated_at": generated.isoformat(),
        "summary": _summary(scorecards, config, gateway_summary),
        "policy": _policy(config),
        "scorecards": scorecards,
        "gateway": gateway_summary,
        "operator_answer": _operator_answer(scorecards),
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
    }
    return payload


def publish_alpha_arena_lite(
    payload: dict[str, Any],
    *,
    out: Path | str = DEFAULT_OUT,
    feed: Path | str | None = DEFAULT_FEED,
) -> Path:
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    with NamedTemporaryFile(
        "w",
        dir=out_path.parent,
        prefix=out_path.name,
        suffix=".tmp",
        delete=False,
        encoding="utf-8",
    ) as tmp:
        tmp.write(encoded)
        tmp_path = Path(tmp.name)
    tmp_path.chmod(0o644)
    tmp_path.replace(out_path)
    out_path.chmod(0o644)
    if feed is not None:
        feed_path = Path(feed)
        feed_path.parent.mkdir(parents=True, exist_ok=True)
        with feed_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_feed_record(payload), sort_keys=True) + "\n")
        feed_path.chmod(0o644)
    return out_path


def _scorecards_from_experiments(
    experiments: Iterable[Any],
    *,
    top_rows: dict[str, dict[str, Any]],
    scanner_index: dict[tuple[str, str, str, str], dict[str, Any]],
    config: AlphaArenaGateConfig,
) -> list[dict[str, Any]]:
    scorecards: list[dict[str, Any]] = []
    for ordinal, experiment in enumerate(experiments, start=1):
        if not isinstance(experiment, dict):
            continue
        rows = _rows_for_experiment(experiment, top_rows)
        scorecards.append(
            _scorecard(
                experiment,
                rows=rows,
                scanner_index=scanner_index,
                ordinal=ordinal,
                config=config,
            )
        )
    return sorted(
        scorecards,
        key=lambda row: (float(row.get("arena_score") or 0.0), -int(row.get("priority") or 0)),
        reverse=True,
    )


def _scorecards_from_strict_candidates(
    *,
    uplift_payload: dict[str, Any],
    fee_wall_payload: dict[str, Any],
    scanner_index: dict[tuple[str, str, str, str], dict[str, Any]],
    config: AlphaArenaGateConfig,
) -> list[dict[str, Any]]:
    rows_by_key: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for row in _strict_rows_from_uplift(uplift_payload, config=config):
        rows_by_key.setdefault(_strict_row_key(row), []).append(row)
    for row in _strict_rows_from_fee_wall(fee_wall_payload, config=config):
        rows_by_key.setdefault(_strict_row_key(row), []).append(row)

    scorecards: list[dict[str, Any]] = []
    for ordinal, ((strategy_id, exchange, symbol, timeframe), rows) in enumerate(
        sorted(rows_by_key.items(), key=lambda item: _strict_group_rank(item[1]), reverse=True),
        start=1,
    ):
        best = max(rows, key=_strict_row_rank)
        target_rows = tuple(
            str(row.get("row_id") or row.get("candidate_id") or row.get("source_ref") or "")
            for row in rows
            if str(row.get("row_id") or row.get("candidate_id") or row.get("source_ref") or "")
        )
        experiment = {
            "experiment_id": f"untouched_judgment|{exchange}|{symbol}|{strategy_id}|{timeframe}",
            "priority": ordinal,
            "experiment_type": "untouched_judgment",
            "target_rows": target_rows,
            "exchange": exchange,
            "symbol": symbol,
            "timeframes": [timeframe],
            "strategy_id": strategy_id,
            "hypothesis": (
                f"{strategy_id} on {exchange} {symbol} {timeframe} cleared the "
                "strict fee-wall screen across canonical evidence sources."
            ),
            "required_change": "Freeze the causal port and pre-register a one-shot untouched-window judgment.",
            "expected_effect": "Separate real edge from already-seen scanner/replay evidence.",
            "guardrails": [
                "research-only output; no paper/live promotion",
                "do not tune on the seen strict-candidate slice",
                "use burn registry before the judgment run",
                "paper manifest only after untouched judgment passes",
            ],
        }
        card = _scorecard(
            experiment,
            rows=tuple(rows),
            scanner_index=scanner_index,
            ordinal=ordinal,
            config=config,
        )
        card["candidate_id"] = _strict_candidate_id(
            strategy_id=strategy_id,
            exchange=exchange,
            symbol=symbol,
            timeframe=timeframe,
        )
        card["experiment_id"] = str(experiment["experiment_id"])
        card["experiment_type"] = "untouched_judgment"
        card["task_kind"] = "alpha_arena_lite.untouched_judgment"
        card["next_action"] = "ASK_OPERATOR_TO_APPROVE_ONE_SHOT_UNTOUCHED_JUDGMENT"
        card["source_evidence"] = [
            {
                "source_kind": row.get("source_kind"),
                "source_artifact": row.get("source_artifact"),
                "row_id": row.get("row_id") or row.get("candidate_id"),
                "samples": row.get("samples"),
                "avg_net_bps": row.get("avg_net_bps"),
                "profit_factor": row.get("profit_factor"),
            }
            for row in sorted(rows, key=lambda item: str(item.get("source_kind") or ""))
        ]
        card["metrics"]["source_evidence_count"] = len(rows)
        card["metrics"]["source_kinds"] = sorted(
            {str(row.get("source_kind") or "") for row in rows if row.get("source_kind")}
        )
        card["metrics"]["top_avg_net_bps"] = _float(best.get("avg_net_bps"))
        card["metrics"]["best_profit_factor"] = _float(best.get("profit_factor"))
        card["metrics"]["max_samples"] = _int(best.get("samples"))
        card["metrics"]["sample_gap"] = 0
        card["untouched_window_plan"]["status"] = "READY_FOR_OPERATOR_PRE_REGISTRATION"
        card["untouched_window_plan"]["required_trade_gap"] = 0
        card["operator_note"] = (
            "Canonical evidence clears the arena screen; request a one-shot untouched judgment."
        )
        scorecards.append(card)
    return scorecards


def _scorecard(
    experiment: dict[str, Any],
    *,
    rows: tuple[dict[str, Any], ...],
    scanner_index: dict[tuple[str, str, str, str], dict[str, Any]],
    ordinal: int,
    config: AlphaArenaGateConfig,
) -> dict[str, Any]:
    best = _best_row(rows)
    strategy_id = str(experiment.get("strategy_id") or best.get("strategy_id") or "unknown")
    exchange = str(experiment.get("exchange") or best.get("exchange") or "unknown")
    symbol = str(experiment.get("symbol") or best.get("symbol") or "unknown")
    timeframes = tuple(
        str(item) for item in (experiment.get("timeframes") or []) if str(item).strip()
    ) or tuple(dict.fromkeys(str(row.get("timeframe") or "unknown") for row in rows))
    if not timeframes:
        timeframes = ("unknown",)
    samples = tuple(_int(row.get("samples")) for row in rows)
    max_samples = max(samples or (0,))
    aggregate_samples = sum(samples)
    avg_net_bps = _float(best.get("avg_net_bps"))
    profit_factor = _float(best.get("profit_factor"))
    failure_modes = Counter(str(row.get("failure_mode") or "unknown") for row in rows)
    sparse = "SPARSE_POSITIVE" in failure_modes
    fee_wall_near = "FEE_WALL_NEAR_MISS" in failure_modes
    promotable_mode = "PROMOTABLE_PROOF_CANDIDATE" in failure_modes
    sample_valid = max_samples >= config.min_trades
    fee_wall_valid = avg_net_bps is not None and avg_net_bps >= config.min_net_bps
    pf_is_synthetic = (
        profit_factor is not None
        and profit_factor >= 999.0
        and not sample_valid
    )
    pf_valid = (
        profit_factor is not None
        and profit_factor >= config.min_profit_factor
        and not pf_is_synthetic
    )
    candidate_id = _candidate_id(experiment)
    scanner_metrics = _merge_scanner_metrics(
        strategy_id=strategy_id,
        exchange=exchange,
        symbol=symbol,
        timeframes=timeframes,
        scanner_index=scanner_index,
    )
    verdict = _arena_verdict(
        sparse=sparse,
        fee_wall_near=fee_wall_near,
        promotable_mode=promotable_mode,
        sample_valid=sample_valid,
        fee_wall_valid=fee_wall_valid,
        pf_valid=pf_valid,
        avg_net_bps=avg_net_bps,
    )
    score = _arena_score(
        avg_net_bps=avg_net_bps,
        profit_factor=profit_factor,
        max_samples=max_samples,
        sample_valid=sample_valid,
        fee_wall_valid=fee_wall_valid,
        pf_valid=pf_valid,
        sparse=sparse,
        fee_wall_near=fee_wall_near,
    )
    return {
        "candidate_id": candidate_id,
        "experiment_id": str(experiment.get("experiment_id") or candidate_id),
        "priority": _int(experiment.get("priority")) or ordinal,
        "experiment_type": str(experiment.get("experiment_type") or "unknown"),
        "task_kind": "alpha_arena_lite.sample_expansion",
        "strategy_id": strategy_id,
        "exchange": exchange,
        "symbol": symbol,
        "timeframes": list(timeframes),
        "target_rows": list(experiment.get("target_rows") or []),
        "failure_modes": dict(failure_modes),
        "arena_verdict": verdict,
        "arena_score": score,
        "next_action": _next_action(verdict),
        "hypothesis": str(experiment.get("hypothesis") or ""),
        "required_change": str(experiment.get("required_change") or ""),
        "expected_effect": str(experiment.get("expected_effect") or ""),
        "guardrails": list(experiment.get("guardrails") or []),
        "metrics": {
            "top_avg_net_bps": avg_net_bps,
            "top_visual_avg_bps": _float(best.get("visual_avg_bps")),
            "best_profit_factor": profit_factor,
            "pf_is_sparse_synthetic": pf_is_synthetic,
            "win_rate_pct": _float(best.get("win_rate_pct")),
            "max_samples": max_samples,
            "aggregate_samples": aggregate_samples,
            "sample_required": config.min_trades,
            "sample_gap": max(0, config.min_trades - max_samples),
            "required_uplift_bps": _float(best.get("required_uplift_bps")),
            "fee_drag_bps": _float(best.get("fee_drag_bps")),
            "dominant_mode": str(best.get("mode") or "unknown"),
            "scanner_avg_mfe_bps": scanner_metrics.get("avg_mfe_bps"),
            "scanner_avg_mae_bps": scanner_metrics.get("avg_mae_bps"),
            "scanner_opportunities": scanner_metrics.get("opportunities"),
            "scanner_routed": scanner_metrics.get("routed"),
        },
        "gate_checks": {
            "sample_valid": sample_valid,
            "fee_wall_valid": fee_wall_valid,
            "profit_factor_valid": pf_valid,
            "min_net_bps": config.min_net_bps,
            "min_profit_factor": config.min_profit_factor,
            "min_trades": config.min_trades,
        },
        "untouched_window_plan": {
            "required": True,
            "status": "NEXT_UNTOUCHED_EXTENSION_REQUIRED",
            "recommended_lookback_days": _recommended_lookback_days(timeframes),
            "required_trade_gap": max(0, config.min_trades - max_samples),
            "freeze_strategy_params": True,
            "use_burn_registry": True,
            "do_not_tune_on_seen_slice": True,
        },
        "execution_plan": {
            "paper_ready": False,
            "maker_first": True,
            "taker_fallback": "blocked_until_expected_net_clears_fee_slippage_buffer",
            "smart_capture": "evaluate_tp1_be_trail_before_tp3_only_replay",
            "live_orders_enabled": False,
        },
        "operator_note": _operator_note(verdict, max_samples, config),
        "can_trade": False,
        "can_promote": False,
        "requires_untouched_judgment": True,
    }


def _sync_gateway(
    scorecards: list[dict[str, Any]],
    *,
    gateway_dir: Path | str | None,
) -> dict[str, Any]:
    gateway = QuantOSAgentGateway(
        Path(gateway_dir) if gateway_dir is not None else env_quant_os_agent_gateway_dir()
    )
    snapshot = gateway.snapshot(limit=100_000)
    tasks_by_candidate = _tasks_by_candidate(snapshot)
    artifact_keys = _artifact_keys(snapshot)
    created = 0
    reused = 0
    artifacts_registered = 0
    artifacts_skipped = 0
    task_refs: list[dict[str, str]] = []
    for scorecard in scorecards:
        candidate_id = str(scorecard["candidate_id"])
        task = tasks_by_candidate.get(candidate_id)
        if task is None:
            task = gateway.create_task(
                kind=str(scorecard["task_kind"]),
                objective=_task_objective(scorecard),
                requested_by=ALPHA_ARENA_LITE_ID,
                priority=int(scorecard.get("priority") or 50),
                target={
                    "exchange": scorecard["exchange"],
                    "symbol": scorecard["symbol"],
                    "timeframes": scorecard["timeframes"],
                    "strategy_id": scorecard["strategy_id"],
                },
                payload={
                    "alpha_arena_lite": {
                        "candidate_id": candidate_id,
                        "experiment_id": scorecard["experiment_id"],
                        "arena_verdict": scorecard["arena_verdict"],
                        "research_only": True,
                    }
                },
            )
            created += 1
        else:
            reused += 1
        task_id = str(task["task_id"])
        scorecard["task_id"] = task_id
        artifact_key = _artifact_key(scorecard)
        if (task_id, artifact_key) in artifact_keys:
            artifacts_skipped += 1
        else:
            gateway.register_content_artifact(
                task_id,
                artifact_type="alpha_arena_scorecard",
                summary=(
                    f"{scorecard['arena_verdict']} "
                    f"{scorecard['strategy_id']} {scorecard['exchange']} {scorecard['symbol']}"
                ),
                content=scorecard,
                metadata={
                    "artifact_key": artifact_key,
                    "candidate_id": candidate_id,
                    "experiment_id": scorecard["experiment_id"],
                    "arena_verdict": scorecard["arena_verdict"],
                    "research_only": True,
                },
            )
            artifacts_registered += 1
        task_refs.append({"candidate_id": candidate_id, "task_id": task_id})
    gateway.write_snapshot()
    return {
        "root": str(gateway.root),
        "tasks_created": created,
        "tasks_reused": reused,
        "artifacts_registered": artifacts_registered,
        "artifacts_skipped": artifacts_skipped,
        "task_refs": task_refs,
        "can_trade": False,
        "can_promote": False,
    }


def _top_rows(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for row in payload.get("top_uplifts") or []:
        if isinstance(row, dict):
            row_id = str(row.get("row_id") or "")
            if row_id:
                rows[row_id] = row
    return rows


def _rows_for_experiment(
    experiment: dict[str, Any],
    top_rows: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], ...]:
    rows = tuple(
        top_rows[row_id]
        for row_id in (str(item) for item in (experiment.get("target_rows") or []))
        if row_id in top_rows
    )
    if rows:
        return rows
    return ({
        "row_id": _candidate_id(experiment),
        "failure_mode": "UNKNOWN",
        "strategy_id": experiment.get("strategy_id"),
        "exchange": experiment.get("exchange"),
        "symbol": experiment.get("symbol"),
        "timeframe": (experiment.get("timeframes") or ["unknown"])[0],
        "samples": 0,
    },)


def _strict_rows_from_uplift(
    payload: dict[str, Any],
    *,
    config: AlphaArenaGateConfig,
) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    for row in payload.get("top_uplifts") or []:
        if not isinstance(row, dict):
            continue
        normalized = {
            "source_kind": "scanner_uplift",
            "source_artifact": "scanner_backtest_uplift_latest.json",
            "row_id": row.get("row_id"),
            "failure_mode": str(row.get("failure_mode") or ""),
            "strategy_id": str(row.get("strategy_id") or ""),
            "exchange": str(row.get("exchange") or ""),
            "symbol": str(row.get("symbol") or ""),
            "timeframe": str(row.get("timeframe") or ""),
            "mode": str(row.get("mode") or ""),
            "samples": _int(row.get("samples")),
            "avg_net_bps": _float(row.get("avg_net_bps")),
            "visual_avg_bps": _float(row.get("visual_avg_bps")),
            "profit_factor": _float(row.get("profit_factor")),
            "win_rate_pct": _float(row.get("win_rate_pct")),
            "required_uplift_bps": _float(row.get("required_uplift_bps")),
            "fee_drag_bps": _float(row.get("fee_drag_bps")),
        }
        if _passes_strict_gate(normalized, config=config):
            normalized["failure_mode"] = "PROMOTABLE_PROOF_CANDIDATE"
            rows.append(normalized)
    return tuple(rows)


def _strict_rows_from_fee_wall(
    payload: dict[str, Any],
    *,
    config: AlphaArenaGateConfig,
) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    for row in payload.get("strict_fee_wall_candidates") or []:
        if not isinstance(row, dict):
            continue
        normalized = {
            "source_kind": "fee_wall_forensics",
            "source_artifact": "fee_wall_forensics_latest.json",
            "row_id": row.get("candidate_id") or row.get("row_id"),
            "failure_mode": "PROMOTABLE_PROOF_CANDIDATE",
            "strategy_id": str(row.get("strategy") or row.get("strategy_id") or ""),
            "exchange": str(row.get("exchange") or ""),
            "symbol": str(row.get("symbol") or ""),
            "timeframe": str(row.get("timeframe") or ""),
            "mode": str(row.get("mode") or row.get("route") or ""),
            "samples": _int(row.get("routed") or row.get("opportunities")),
            "avg_net_bps": _float(row.get("avg_selected_net_bps")),
            "visual_avg_bps": _float(row.get("avg_selected_gross_bps")),
            "profit_factor": _float(row.get("profit_factor")),
            "win_rate_pct": _float(row.get("win_rate_pct")),
            "fee_drag_bps": None,
        }
        if _passes_strict_gate(normalized, config=config):
            rows.append(normalized)
    for report in payload.get("reports") or []:
        if not isinstance(report, dict):
            continue
        summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
        normalized = {
            "source_kind": "fee_wall_forensics",
            "source_artifact": "fee_wall_forensics_latest.json",
            "row_id": report.get("candidate_id") or report.get("row_id"),
            "failure_mode": "PROMOTABLE_PROOF_CANDIDATE",
            "strategy_id": str(report.get("strategy") or report.get("strategy_id") or ""),
            "exchange": str(report.get("exchange") or ""),
            "symbol": str(report.get("symbol") or ""),
            "timeframe": str(report.get("timeframe") or ""),
            "mode": str(summary.get("dominant_route") or report.get("route") or ""),
            "samples": _int(summary.get("routed") or report.get("opportunity_count")),
            "avg_net_bps": _float(summary.get("avg_selected_net_bps")),
            "visual_avg_bps": _float(summary.get("avg_selected_gross_bps")),
            "profit_factor": _float(summary.get("profit_factor")),
            "win_rate_pct": _float(summary.get("win_rate_pct")),
            "fee_drag_bps": None,
        }
        if (
            str(summary.get("verdict") or "") in {"MAKER_EDGE", "TAKER_EDGE", "MIXED_ROUTE_EDGE"}
            and _passes_strict_gate(normalized, config=config)
        ):
            rows.append(normalized)
    return tuple(rows)


def _passes_strict_gate(row: dict[str, Any], *, config: AlphaArenaGateConfig) -> bool:
    avg = _float(row.get("avg_net_bps"))
    pf = _float(row.get("profit_factor"))
    return (
        bool(row.get("strategy_id"))
        and bool(row.get("exchange"))
        and bool(row.get("symbol"))
        and bool(row.get("timeframe"))
        and _int(row.get("samples")) >= config.min_trades
        and avg is not None
        and avg >= config.min_net_bps
        and pf is not None
        and pf >= config.min_profit_factor
    )


def _strict_row_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row.get("strategy_id") or ""),
        str(row.get("exchange") or ""),
        str(row.get("symbol") or ""),
        str(row.get("timeframe") or ""),
    )


def _strict_row_rank(row: dict[str, Any]) -> tuple[float, float, int]:
    return (
        _float(row.get("avg_net_bps")) or -999999.0,
        min(_float(row.get("profit_factor")) or -1.0, 999.0),
        _int(row.get("samples")),
    )


def _strict_group_rank(rows: list[dict[str, Any]]) -> tuple[float, float, int, int]:
    best = max(rows, key=_strict_row_rank)
    avg, pf, samples = _strict_row_rank(best)
    return avg, pf, samples, len(rows)


def _dedupe_scorecards(
    scorecards: list[dict[str, Any]],
    *,
    max_scorecards: int,
) -> list[dict[str, Any]]:
    by_lane: dict[tuple[str, str, str, tuple[str, ...]], dict[str, Any]] = {}
    for scorecard in scorecards:
        key = (
            str(scorecard.get("strategy_id") or ""),
            str(scorecard.get("exchange") or ""),
            str(scorecard.get("symbol") or ""),
            tuple(str(tf) for tf in (scorecard.get("timeframes") or [])),
        )
        existing = by_lane.get(key)
        if existing is None or _scorecard_rank(scorecard) > _scorecard_rank(existing):
            by_lane[key] = scorecard
    return sorted(by_lane.values(), key=_scorecard_rank, reverse=True)[:max_scorecards]


def _scorecard_rank(scorecard: dict[str, Any]) -> tuple[int, float, int]:
    verdict_rank = {
        "PRE_REGISTER_UNTOUCHED_JUDGMENT": 4,
        "EXPAND_UNTOUCHED_SAMPLE": 3,
        "EXECUTION_SALVAGE_REQUIRED": 2,
        "SELECTIVITY_OR_EXIT_UPLIFT": 1,
        "FEATURE_BANK_OR_REJECT": 0,
    }.get(str(scorecard.get("arena_verdict") or ""), 0)
    metrics = scorecard.get("metrics") if isinstance(scorecard.get("metrics"), dict) else {}
    return (
        verdict_rank,
        float(scorecard.get("arena_score") or 0.0),
        _int(metrics.get("max_samples")),
    )


def _best_row(rows: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    return max(
        rows,
        key=lambda row: (
            _float(row.get("avg_net_bps")) if _float(row.get("avg_net_bps")) is not None else -1e9,
            _int(row.get("samples")),
        ),
    )


def _scanner_candidate_index(payload: dict[str, Any]) -> dict[tuple[str, str, str, str], dict[str, Any]]:
    index: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in payload.get("candidates") or []:
        if not isinstance(row, dict):
            continue
        key = (
            str(row.get("strategy_id") or ""),
            str(row.get("exchange") or ""),
            str(row.get("symbol") or ""),
            str(row.get("timeframe") or ""),
        )
        if all(key):
            index[key] = row
    return index


def _merge_scanner_metrics(
    *,
    strategy_id: str,
    exchange: str,
    symbol: str,
    timeframes: tuple[str, ...],
    scanner_index: dict[tuple[str, str, str, str], dict[str, Any]],
) -> dict[str, Any]:
    matches = tuple(
        scanner_index[key]
        for timeframe in timeframes
        for key in ((strategy_id, exchange, symbol, timeframe),)
        if key in scanner_index
    )
    if not matches:
        return {}
    return {
        "avg_mfe_bps": _avg(row.get("avg_mfe_bps") for row in matches),
        "avg_mae_bps": _avg(row.get("avg_mae_bps") for row in matches),
        "opportunities": sum(_int(row.get("opportunities")) for row in matches),
        "routed": sum(_int(row.get("routed")) for row in matches),
    }


def _arena_verdict(
    *,
    sparse: bool,
    fee_wall_near: bool,
    promotable_mode: bool,
    sample_valid: bool,
    fee_wall_valid: bool,
    pf_valid: bool,
    avg_net_bps: float | None,
) -> str:
    if promotable_mode and sample_valid and fee_wall_valid and pf_valid:
        return "PRE_REGISTER_UNTOUCHED_JUDGMENT"
    if sparse:
        return "EXPAND_UNTOUCHED_SAMPLE"
    if fee_wall_near:
        return "EXECUTION_SALVAGE_REQUIRED"
    if avg_net_bps is not None and avg_net_bps > 0:
        return "SELECTIVITY_OR_EXIT_UPLIFT"
    return "FEATURE_BANK_OR_REJECT"


def _next_action(verdict: str) -> str:
    actions = {
        "PRE_REGISTER_UNTOUCHED_JUDGMENT": "ASK_OPERATOR_TO_APPROVE_ONE_SHOT_UNTOUCHED_JUDGMENT",
        "EXPAND_UNTOUCHED_SAMPLE": "RUN_FROZEN_SETUP_ON_NEXT_UNTOUCHED_WINDOW",
        "EXECUTION_SALVAGE_REQUIRED": "TEST_MAKER_FIRST_AND_TAKER_FALLBACK_ROUTE_WITH_FEE_BUFFER",
        "SELECTIVITY_OR_EXIT_UPLIFT": "MINE_CAUSAL_CONTEXT_FILTERS_AND_SMART_CAPTURE_EXITS",
        "FEATURE_BANK_OR_REJECT": "RECYCLE_AS_EDGE_MODEL_FEATURE_OR_REJECT_STANDALONE_ENTRY",
    }
    return actions.get(verdict, "REVIEW_RESEARCH_EVIDENCE")


def _arena_score(
    *,
    avg_net_bps: float | None,
    profit_factor: float | None,
    max_samples: int,
    sample_valid: bool,
    fee_wall_valid: bool,
    pf_valid: bool,
    sparse: bool,
    fee_wall_near: bool,
) -> float:
    avg = avg_net_bps if avg_net_bps is not None else -50.0
    bounded_pf = min(profit_factor or 0.0, 5.0)
    score = avg + bounded_pf * 8.0 + min(math.sqrt(max_samples), 10.0)
    if sample_valid:
        score += 25.0
    if fee_wall_valid:
        score += 20.0
    if pf_valid:
        score += 20.0
    if sparse:
        score -= 15.0
    if fee_wall_near:
        score += 5.0
    return round(score, 4)


def _summary(
    scorecards: list[dict[str, Any]],
    config: AlphaArenaGateConfig,
    gateway_summary: dict[str, Any],
) -> dict[str, Any]:
    verdict_counts = Counter(str(row.get("arena_verdict") or "unknown") for row in scorecards)
    sample_valid = [row for row in scorecards if row["gate_checks"]["sample_valid"]]
    fee_wall_valid = [row for row in scorecards if row["gate_checks"]["fee_wall_valid"]]
    pf_valid = [row for row in scorecards if row["gate_checks"]["profit_factor_valid"]]
    pre_judgment = [
        row for row in scorecards
        if row.get("arena_verdict") == "PRE_REGISTER_UNTOUCHED_JUDGMENT"
    ]
    sparse = [row for row in scorecards if row.get("arena_verdict") == "EXPAND_UNTOUCHED_SAMPLE"]
    best = scorecards[0] if scorecards else None
    return {
        "candidate_count": len(scorecards),
        "task_count": len(gateway_summary.get("task_refs") or []),
        "scorecards": len(scorecards),
        "sample_valid": len(sample_valid),
        "fee_wall_valid": len(fee_wall_valid),
        "profit_factor_valid": len(pf_valid),
        "sparse_positive": len(sparse),
        "ready_for_untouched_judgment": len(pre_judgment),
        "verdict_counts": dict(verdict_counts),
        "best_candidate_id": best.get("candidate_id") if best is not None else None,
        "best_strategy_id": best.get("strategy_id") if best is not None else None,
        "best_avg_net_bps": (
            (best.get("metrics") or {}).get("top_avg_net_bps")
            if best is not None
            else None
        ),
        "best_samples": (
            (best.get("metrics") or {}).get("max_samples")
            if best is not None
            else None
        ),
        "best_verdict": best.get("arena_verdict") if best is not None else None,
        "gate": asdict(config),
        "research_only": True,
        "can_trade": False,
        "can_promote": False,
    }


def _policy(config: AlphaArenaGateConfig) -> dict[str, Any]:
    return {
        "research_only": True,
        "can_trade": False,
        "can_promote": False,
        "min_net_bps": config.min_net_bps,
        "min_profit_factor": config.min_profit_factor,
        "min_trades": config.min_trades,
        "agent_role": (
            "Convert scanner uplift rows into durable research tasks and scorecards; "
            "do not lower promotion gates or start paper/live lanes."
        ),
    }


def _operator_answer(scorecards: list[dict[str, Any]]) -> str:
    if not scorecards:
        return "Alpha Arena Lite has no scanner uplift experiments to supervise yet."
    best = scorecards[0]
    if best["arena_verdict"] == "PRE_REGISTER_UNTOUCHED_JUDGMENT":
        return (
            f"Best arena candidate is {best['candidate_id']} and has enough sample on "
            "this evidence. Next step is operator-approved untouched judgment, not paper."
        )
    if best["arena_verdict"] == "EXPAND_UNTOUCHED_SAMPLE":
        gap = (best.get("metrics") or {}).get("sample_gap")
        return (
            f"Best arena candidate is {best['candidate_id']} but it is sparse. "
            f"Need at least {gap} more closed examples on a frozen untouched window "
            "before any paper promotion discussion."
        )
    return (
        f"Best arena candidate is {best['candidate_id']} with verdict "
        f"{best['arena_verdict']}. Use it for execution/context uplift, not promotion."
    )


def _gateway_summary_disabled(gateway_dir: Path | str | None) -> dict[str, Any]:
    return {
        "root": str(gateway_dir or env_quant_os_agent_gateway_dir()),
        "tasks_created": 0,
        "tasks_reused": 0,
        "artifacts_registered": 0,
        "artifacts_skipped": 0,
        "task_refs": [],
        "sync_disabled": True,
        "can_trade": False,
        "can_promote": False,
    }


def _tasks_by_candidate(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for task in snapshot.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
        arena = payload.get("alpha_arena_lite") if isinstance(payload.get("alpha_arena_lite"), dict) else {}
        candidate_id = str(arena.get("candidate_id") or "")
        if candidate_id:
            out[candidate_id] = task
    return out


def _artifact_keys(snapshot: dict[str, Any]) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    artifacts = snapshot.get("artifacts") if isinstance(snapshot.get("artifacts"), dict) else {}
    for artifact in artifacts.get("recent") or []:
        if not isinstance(artifact, dict):
            continue
        metadata = artifact.get("metadata") if isinstance(artifact.get("metadata"), dict) else {}
        artifact_key = str(metadata.get("artifact_key") or "")
        task_id = str(artifact.get("task_id") or "")
        if artifact_key and task_id:
            keys.add((task_id, artifact_key))
    return keys


def _artifact_key(scorecard: dict[str, Any]) -> str:
    stable = {
        key: scorecard.get(key)
        for key in (
            "candidate_id",
            "arena_verdict",
            "metrics",
            "gate_checks",
            "untouched_window_plan",
            "execution_plan",
        )
    }
    encoded = json.dumps(stable, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


def _candidate_id(experiment: dict[str, Any]) -> str:
    return "|".join(
        (
            str(experiment.get("experiment_type") or "unknown"),
            str(experiment.get("exchange") or "unknown"),
            _compact_symbol(str(experiment.get("symbol") or "unknown")),
            str(experiment.get("strategy_id") or "unknown"),
        )
    )


def _strict_candidate_id(
    *,
    strategy_id: str,
    exchange: str,
    symbol: str,
    timeframe: str,
) -> str:
    return "|".join(
        (
            "strict_judgment",
            exchange,
            _compact_symbol(symbol),
            timeframe,
            strategy_id,
        )
    )


def _task_objective(scorecard: dict[str, Any]) -> str:
    return (
        f"{scorecard['arena_verdict']}: {scorecard['strategy_id']} "
        f"{scorecard['exchange']} {scorecard['symbol']} "
        f"{','.join(scorecard['timeframes'])}"
    )


def _operator_note(verdict: str, max_samples: int, config: AlphaArenaGateConfig) -> str:
    if verdict == "EXPAND_UNTOUCHED_SAMPLE":
        return (
            f"Only {max_samples}/{config.min_trades} samples are available. "
            "Freeze params and extend the next untouched sample before paper."
        )
    if verdict == "EXECUTION_SALVAGE_REQUIRED":
        return (
            "The idea is near the fee wall. Test route selection and smart captures "
            "before adding entries."
        )
    if verdict == "PRE_REGISTER_UNTOUCHED_JUDGMENT":
        return "Evidence clears the arena screen; request a one-shot untouched judgment."
    return "Treat as feature-bank input unless a causal uplift replay improves it."


def _recommended_lookback_days(timeframes: tuple[str, ...]) -> int:
    fast = {"1m", "5m", "15m"}
    return 180 if any(tf in fast for tf in timeframes) else 365


def _compact_symbol(symbol: str) -> str:
    return symbol.replace("/", "").replace(":", "").replace("-", "")


def _feed_record(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "arena_id": payload.get("arena_id"),
        "generated_at": payload.get("generated_at"),
        "summary": payload.get("summary", {}),
        "operator_answer": payload.get("operator_answer"),
        "can_trade": False,
        "can_promote": False,
    }


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload if isinstance(payload, dict) else {}


def _float(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _avg(values: Iterable[object]) -> float | None:
    parsed = [_float(value) for value in values]
    kept = [value for value in parsed if value is not None]
    if not kept:
        return None
    return round(sum(kept) / len(kept), 4)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="publish research-only Alpha Arena Lite scorecards"
    )
    parser.add_argument("--uplift", default=str(DEFAULT_UPLIFT))
    parser.add_argument("--scanner", default=str(DEFAULT_SCANNER))
    parser.add_argument("--fee-wall", default=str(DEFAULT_FEE_WALL))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--feed", default=str(DEFAULT_FEED))
    parser.add_argument("--gateway-dir", default=str(env_quant_os_agent_gateway_dir()))
    parser.add_argument("--max-scorecards", type=int, default=12)
    parser.add_argument("--min-net-bps", type=float, default=25.0)
    parser.add_argument("--min-profit-factor", type=float, default=1.50)
    parser.add_argument("--min-trades", type=int, default=20)
    parser.add_argument("--no-gateway-sync", action="store_true")
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=0,
        help="repeat forever at this cadence; 0 runs once",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    while True:
        payload = run_alpha_arena_lite(
            uplift_payload=_read_json(Path(args.uplift)),
            scanner_payload=_read_json(Path(args.scanner)),
            fee_wall_payload=_read_json(Path(args.fee_wall)),
            config=AlphaArenaGateConfig(
                min_net_bps=args.min_net_bps,
                min_profit_factor=args.min_profit_factor,
                min_trades=args.min_trades,
                max_scorecards=args.max_scorecards,
            ),
            gateway_dir=Path(args.gateway_dir),
            sync_gateway=not args.no_gateway_sync,
        )
        path = publish_alpha_arena_lite(
            payload,
            out=args.out,
            feed=None if args.feed == "" else args.feed,
        )
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True), flush=True)
        else:
            print(f"alpha arena lite wrote {path}", flush=True)
            print(payload["operator_answer"], flush=True)
        if args.interval_seconds <= 0:
            break
        time.sleep(max(1, args.interval_seconds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
