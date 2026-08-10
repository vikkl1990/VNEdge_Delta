"""Agentic edge-uplift planner for source-backed Pine research evidence.

This module is the bridge between "the scanner failed" and "what should the
research system learn from that failure?"  It consumes the Pine Research KB
after backtest evidence has been overlaid, plus the source-backed alpha
distiller output, then emits deterministic research-only uplift experiments.

It never emits Pine source, never grants paper/live permission, and never
changes promotion gates.  Its job is to recycle failed evidence into safer
feature banks, confluence tests, execution-filter experiments, or untouched
judgment requests when a cell already clears proof-grade thresholds.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
import math
from pathlib import Path
from tempfile import NamedTemporaryFile
import time
from typing import Iterable, Literal

from vnedge.research.pine_alpha_distiller import DEFAULT_OUT as DEFAULT_DISTILLER_PATH
from vnedge.research.pine_script_research import (
    DEFAULT_PINE_KB_PATH,
    load_pine_research_payload,
)


PINE_EDGE_UPLIFT_AGENT_ID = "pine_edge_uplift_agent_v1"
DEFAULT_OUT = Path("research/live_research/pine_edge_uplift_agent_latest.json")
DEFAULT_FEED = Path("research/live_research/pine_edge_uplift_agent_feed.jsonl")

AgentVerdict = Literal[
    "PROMOTABLE_PROOF_REQUIRES_UNTOUCHED_JUDGMENT",
    "POSITIVE_BUT_UNDER_SAMPLED",
    "NEAR_MISS_AFTER_COST",
    "CONVERT_TO_CONTEXT_FEATURE",
    "FEATURE_BANK_ONLY",
    "BLOCKED_SOURCE_OR_CAUSALITY",
    "AWAITING_REPLAY",
]


@dataclass(frozen=True)
class ScriptUplift:
    script_id: str
    title: str
    recommended_port: str
    primitives: tuple[str, ...]
    completed_cells: int
    failed_cells: int
    positive_cells: int
    blocked_cells: int
    queued_cells: int
    best_timeframe: str
    best_status: str
    best_avg_net_bps: float | None
    best_profit_factor: float | None
    best_samples: int
    best_blocker: str
    failure_mode: str
    agent_verdict: AgentVerdict
    salvage_score: int
    uplift_action: str
    use_as: str
    rationale: str
    can_trade: bool = False
    can_promote: bool = False
    requires_untouched_judgment: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class UpliftExperiment:
    experiment_id: str
    agent: str
    experiment_type: str
    recommended_port: str
    primitive_stack: tuple[str, ...]
    source_script_ids: tuple[str, ...]
    source_titles: tuple[str, ...]
    failed_cells: int
    positive_cells: int
    best_avg_net_bps: float | None
    best_profit_factor: float | None
    salvage_score: int
    next_action: str
    hypothesis: str
    required_data: tuple[str, ...]
    guardrails: tuple[str, ...]
    can_trade: bool = False
    can_promote: bool = False
    requires_untouched_judgment: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


def run_pine_edge_uplift_agent(
    *,
    kb_path: Path | str | None = DEFAULT_PINE_KB_PATH,
    distiller_path: Path | str | None = DEFAULT_DISTILLER_PATH,
    max_scripts: int = 80,
    max_experiments: int = 12,
    now: datetime | None = None,
) -> dict:
    generated = now or datetime.now(UTC)
    kb = load_pine_research_payload(Path(kb_path) if kb_path is not None else None)
    distiller = _read_json(Path(distiller_path)) if distiller_path is not None else {}
    distillations = {
        str(row.get("script_id") or ""): dict(row)
        for row in distiller.get("script_distillations", [])
        if isinstance(row, dict)
    }
    records = [row for row in kb.get("records", []) if isinstance(row, dict)]
    uplifts = [
        _script_uplift(row, distillations.get(str(row.get("script_id") or "")))
        for row in records
    ]
    uplifts = sorted(uplifts, key=_uplift_rank, reverse=True)[:max_scripts]
    experiments = _build_experiments(uplifts, max_experiments=max_experiments)
    return {
        "agent_id": PINE_EDGE_UPLIFT_AGENT_ID,
        "generated_at": generated.isoformat(),
        "source": {
            "kb": str(kb_path or "default_seed"),
            "distiller": str(distiller_path or "none"),
        },
        "summary": _summary(uplifts, experiments),
        "policy": _policy(),
        "failure_clusters": _failure_clusters(uplifts),
        "top_uplifts": [row.to_dict() for row in uplifts[:max_scripts]],
        "experiments": [row.to_dict() for row in experiments],
        "operator_answer": _operator_answer(uplifts, experiments),
        "can_trade": False,
        "can_promote": False,
    }


def publish_pine_edge_uplift_agent(
    payload: dict,
    *,
    out: Path | str = DEFAULT_OUT,
    feed: Path | str = DEFAULT_FEED,
) -> Path:
    out_path = Path(out)
    feed_path = Path(feed)
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
    feed_path.parent.mkdir(parents=True, exist_ok=True)
    with feed_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
    feed_path.chmod(0o644)
    return out_path


def _script_uplift(record: dict, distillation: dict | None) -> ScriptUplift:
    distillation = distillation or {}
    cells = [cell for cell in record.get("backtests", []) if isinstance(cell, dict)]
    completed = [
        cell for cell in cells
        if str(cell.get("status") or "").lower() in {"passed", "failed"}
    ]
    failed = [cell for cell in cells if str(cell.get("status") or "").lower() == "failed"]
    positive = [cell for cell in completed if (_float(cell.get("avg_net_bps")) or 0.0) > 0.0]
    blocked = [cell for cell in cells if str(cell.get("status") or "").lower() == "blocked"]
    queued = [
        cell for cell in cells
        if str(cell.get("status") or "").lower() in {"queued", "running"}
    ]
    best = max(cells, key=_cell_rank, default={})
    primitives = tuple(distillation.get("primitives") or record.get("features") or ())
    port = str(distillation.get("recommended_port") or record.get("recommended_port") or "")
    best_avg = _float(best.get("avg_net_bps"))
    best_pf = _float(best.get("profit_factor"))
    best_samples = _int(best.get("samples"))
    failure_mode = _failure_mode(cells, best, completed, failed, positive, blocked)
    verdict = _agent_verdict(failure_mode, best_avg, best_pf, best_samples, blocked, queued)
    salvage_score = _salvage_score(
        record=record,
        primitives=primitives,
        best_avg=best_avg,
        best_pf=best_pf,
        samples=best_samples,
        verdict=verdict,
    )
    action, use_as = _uplift_action(verdict, primitives, port)
    return ScriptUplift(
        script_id=str(record.get("script_id") or ""),
        title=str(record.get("title") or record.get("script_id") or ""),
        recommended_port=port or "source_feature_library_review_v1",
        primitives=primitives,
        completed_cells=len(completed),
        failed_cells=len(failed),
        positive_cells=len(positive),
        blocked_cells=len(blocked),
        queued_cells=len(queued),
        best_timeframe=str(best.get("timeframe") or ""),
        best_status=str(best.get("status") or ""),
        best_avg_net_bps=best_avg,
        best_profit_factor=best_pf,
        best_samples=best_samples,
        best_blocker=str(best.get("blocker") or ""),
        failure_mode=failure_mode,
        agent_verdict=verdict,
        salvage_score=salvage_score,
        uplift_action=action,
        use_as=use_as,
        rationale=_rationale(verdict, best_avg, best_pf, best_samples, primitives),
    )


def _build_experiments(
    uplifts: Iterable[ScriptUplift],
    *,
    max_experiments: int,
) -> tuple[UpliftExperiment, ...]:
    by_port: dict[str, list[ScriptUplift]] = defaultdict(list)
    for row in uplifts:
        if row.agent_verdict == "BLOCKED_SOURCE_OR_CAUSALITY":
            continue
        by_port[row.recommended_port].append(row)

    experiments: list[UpliftExperiment] = []
    for port, rows in by_port.items():
        rows = sorted(rows, key=_uplift_rank, reverse=True)
        if not rows:
            continue
        primitives = _primitive_stack(rows)
        experiment_type = _experiment_type(port, rows)
        experiments.append(
            UpliftExperiment(
                experiment_id=f"{experiment_type}|{port}",
                agent="pine_edge_uplift_agent",
                experiment_type=experiment_type,
                recommended_port=port,
                primitive_stack=primitives,
                source_script_ids=tuple(row.script_id for row in rows[:8]),
                source_titles=tuple(row.title for row in rows[:5]),
                failed_cells=sum(row.failed_cells for row in rows),
                positive_cells=sum(row.positive_cells for row in rows),
                best_avg_net_bps=_best_float(row.best_avg_net_bps for row in rows),
                best_profit_factor=_best_float(row.best_profit_factor for row in rows),
                salvage_score=min(
                    100,
                    round(sum(row.salvage_score for row in rows[:8]) / min(len(rows), 8)),
                ),
                next_action=_experiment_action(experiment_type),
                hypothesis=_experiment_hypothesis(port, primitives, rows),
                required_data=_required_data(primitives),
                guardrails=_guardrails(experiment_type),
            )
        )

    feature_bank_rows = [
        row for row in uplifts if row.agent_verdict != "BLOCKED_SOURCE_OR_CAUSALITY"
    ]
    if feature_bank_rows:
        experiments.append(_feature_bank_experiment(feature_bank_rows))
    experiments = sorted(experiments, key=lambda row: row.salvage_score, reverse=True)
    return tuple(experiments[:max_experiments])


def _feature_bank_experiment(uplifts: Iterable[ScriptUplift]) -> UpliftExperiment:
    rows = sorted(
        [row for row in uplifts if row.agent_verdict != "BLOCKED_SOURCE_OR_CAUSALITY"],
        key=_uplift_rank,
        reverse=True,
    )
    primitives = _primitive_stack(rows)
    return UpliftExperiment(
        experiment_id="feature_bank|pine_failed_cells_to_edge_model",
        agent="pine_edge_uplift_agent",
        experiment_type="edge_model_feature_bank",
        recommended_port="edge_model_feature_bank_v1",
        primitive_stack=primitives,
        source_script_ids=tuple(row.script_id for row in rows[:12]),
        source_titles=tuple(row.title for row in rows[:6]),
        failed_cells=sum(row.failed_cells for row in rows),
        positive_cells=sum(row.positive_cells for row in rows),
        best_avg_net_bps=_best_float(row.best_avg_net_bps for row in rows),
        best_profit_factor=_best_float(row.best_profit_factor for row in rows),
        salvage_score=min(100, 50 + len(primitives) * 5 + sum(row.positive_cells for row in rows)),
        next_action="TRAIN_EDGE_ROUTER_FEATURES_ON_FAILURE_LABELS",
        hypothesis=(
            "Failed indicators may still contain useful context.  Feed their "
            "causal primitives into the edge model as features, not standalone "
            "entry signals, then prove OOS lift against raw scanner baseline."
        ),
        required_data=("scanner opportunity rows", "fee-aware labels", "chronological OOS split"),
        guardrails=_guardrails("edge_model_feature_bank"),
    )


def _summary(uplifts: Iterable[ScriptUplift], experiments: Iterable[UpliftExperiment]) -> dict:
    rows = tuple(uplifts)
    experiments = tuple(experiments)
    return {
        "scripts_reviewed": len(rows),
        "promotable_proofs": sum(
            1 for row in rows
            if row.agent_verdict == "PROMOTABLE_PROOF_REQUIRES_UNTOUCHED_JUDGMENT"
        ),
        "positive_under_sampled": sum(
            1 for row in rows if row.agent_verdict == "POSITIVE_BUT_UNDER_SAMPLED"
        ),
        "near_miss_after_cost": sum(
            1 for row in rows if row.agent_verdict == "NEAR_MISS_AFTER_COST"
        ),
        "feature_bank_only": sum(1 for row in rows if row.agent_verdict == "FEATURE_BANK_ONLY"),
        "failed_cells": sum(row.failed_cells for row in rows),
        "positive_cells": sum(row.positive_cells for row in rows),
        "blocked_cells": sum(row.blocked_cells for row in rows),
        "experiments": len(experiments),
        "top_experiment": experiments[0].experiment_id if experiments else "",
        "can_trade": False,
        "can_promote": False,
    }


def _failure_clusters(uplifts: Iterable[ScriptUplift]) -> list[dict]:
    by_mode: dict[str, list[ScriptUplift]] = defaultdict(list)
    for row in uplifts:
        by_mode[row.failure_mode].append(row)
    clusters = []
    for mode, rows in by_mode.items():
        primitive_counts = Counter(p for row in rows for p in row.primitives)
        clusters.append(
            {
                "failure_mode": mode,
                "scripts": len(rows),
                "failed_cells": sum(row.failed_cells for row in rows),
                "positive_cells": sum(row.positive_cells for row in rows),
                "top_primitives": [p for p, _ in primitive_counts.most_common(5)],
                "recommended_use": _cluster_use(mode),
                "can_trade": False,
                "can_promote": False,
            }
        )
    return sorted(clusters, key=lambda row: (row["positive_cells"], row["scripts"]), reverse=True)


def _cell_rank(cell: dict) -> tuple[float, float, int, int]:
    status = str(cell.get("status") or "").lower()
    status_rank = {"passed": 4, "failed": 3, "running": 2, "queued": 1}.get(status, 0)
    avg = _float(cell.get("avg_net_bps"))
    pf = _float(cell.get("profit_factor"))
    return (
        float(status_rank),
        avg if avg is not None else -999.0,
        int((pf if pf is not None else 0.0) * 100),
        _int(cell.get("samples")),
    )


def _failure_mode(
    cells: list[dict],
    best: dict,
    completed: list[dict],
    failed: list[dict],
    positive: list[dict],
    blocked: list[dict],
) -> str:
    if blocked and not completed:
        return "blocked_source_or_causality"
    if not cells or not completed:
        return "awaiting_replay"
    best_avg = _float(best.get("avg_net_bps"))
    best_pf = _float(best.get("profit_factor"))
    samples = _int(best.get("samples"))
    if (
        str(best.get("status") or "").lower() == "passed"
        and positive
        and best_avg is not None
        and best_avg >= 25.0
        and (best_pf or 0.0) >= 1.5
        and samples >= 20
    ):
        return "proof_grade_positive"
    if positive:
        return "positive_but_under_sampled_or_gate_weak"
    if failed and (best_avg is not None and best_avg > -15.0 or (best_pf or 0.0) >= 0.8):
        return "near_miss_after_cost"
    return "negative_after_cost"


def _agent_verdict(
    failure_mode: str,
    best_avg: float | None,
    best_pf: float | None,
    samples: int,
    blocked: list[dict],
    queued: list[dict],
) -> AgentVerdict:
    if failure_mode == "proof_grade_positive":
        return "PROMOTABLE_PROOF_REQUIRES_UNTOUCHED_JUDGMENT"
    if failure_mode == "positive_but_under_sampled_or_gate_weak":
        return "POSITIVE_BUT_UNDER_SAMPLED"
    if failure_mode == "near_miss_after_cost":
        return "NEAR_MISS_AFTER_COST"
    if failure_mode == "blocked_source_or_causality" or (blocked and not queued and samples == 0):
        return "BLOCKED_SOURCE_OR_CAUSALITY"
    if failure_mode == "awaiting_replay":
        return "AWAITING_REPLAY"
    if best_avg is not None and best_avg > -25.0 or (best_pf or 0.0) >= 0.5:
        return "CONVERT_TO_CONTEXT_FEATURE"
    return "FEATURE_BANK_ONLY"


def _salvage_score(
    *,
    record: dict,
    primitives: tuple[str, ...],
    best_avg: float | None,
    best_pf: float | None,
    samples: int,
    verdict: AgentVerdict,
) -> int:
    score = int(record.get("crypto_fit_score") or 0)
    score += min(20, len(primitives) * 4)
    score += min(15, samples // 4)
    if best_avg is not None:
        score += 25 if best_avg > 0 else max(-30, int(best_avg))
    if best_pf is not None:
        score += min(20, int(best_pf * 8))
    if verdict == "PROMOTABLE_PROOF_REQUIRES_UNTOUCHED_JUDGMENT":
        score += 30
    if verdict == "BLOCKED_SOURCE_OR_CAUSALITY":
        score -= 40
    return max(0, min(100, score))


def _uplift_action(
    verdict: AgentVerdict,
    primitives: tuple[str, ...],
    port: str,
) -> tuple[str, str]:
    if verdict == "PROMOTABLE_PROOF_REQUIRES_UNTOUCHED_JUDGMENT":
        return "PRE_REGISTER_UNTOUCHED_JUDGMENT", "candidate_lane"
    if verdict == "POSITIVE_BUT_UNDER_SAMPLED":
        return "EXPAND_REPLAY_WINDOW_AND_CROSS_VENUE", "sparse_candidate"
    if verdict == "NEAR_MISS_AFTER_COST":
        return "ADD_EXECUTION_FILTER_OR_MAKER_TAKER_ROUTER", "entry_filter_candidate"
    if verdict == "CONVERT_TO_CONTEXT_FEATURE":
        return "DISTILL_AS_EDGE_MODEL_FEATURE", "context_feature"
    if verdict == "AWAITING_REPLAY":
        return "PORT_CAUSAL_FEATURES_AND_REPLAY", "queued_candidate"
    if "risk_plan" in primitives or port == "trail_exit_lab_v1":
        return "TEST_AS_EXIT_OVERLAY", "exit_overlay"
    if verdict == "BLOCKED_SOURCE_OR_CAUSALITY":
        return "FIX_SOURCE_OR_CAUSALITY_BEFORE_ANY_REPLAY", "blocked"
    return "FEATURE_BANK_ONLY", "feature_bank"


def _primitive_stack(rows: Iterable[ScriptUplift]) -> tuple[str, ...]:
    counts = Counter(p for row in rows for p in row.primitives)
    ordered = [p for p, _ in counts.most_common()]
    if "risk_plan" not in ordered:
        ordered.append("risk_plan")
    return tuple(ordered[:8])


def _experiment_type(port: str, rows: list[ScriptUplift]) -> str:
    if any(row.agent_verdict == "PROMOTABLE_PROOF_REQUIRES_UNTOUCHED_JUDGMENT" for row in rows):
        return "untouched_judgment_candidate"
    if any(row.agent_verdict == "NEAR_MISS_AFTER_COST" for row in rows):
        return "execution_filtered_replay"
    if port in {"trend_momentum_context_v1", "edge_model_feature_bank_v1"}:
        return "context_feature_bank"
    return "confluence_stack_replay"


def _experiment_action(experiment_type: str) -> str:
    return {
        "untouched_judgment_candidate": "ASK_OPERATOR_TO_APPROVE_PRE_REGISTERED_WINDOW",
        "execution_filtered_replay": "REPLAY_WITH_MAKER_FIRST_AND_TAKER_FALLBACK_LABELS",
        "context_feature_bank": "TRAIN_EDGE_MODEL_WITH_PRIMITIVE_FEATURES",
        "edge_model_feature_bank": "TRAIN_EDGE_ROUTER_FEATURES_ON_FAILURE_LABELS",
    }.get(experiment_type, "PORT_CAUSAL_STACK_AND_REPLAY")


def _experiment_hypothesis(
    port: str,
    primitives: tuple[str, ...],
    rows: list[ScriptUplift],
) -> str:
    best = rows[0] if rows else None
    primitive_text = ", ".join(primitives[:5]) or "source primitives"
    if best and best.agent_verdict == "NEAR_MISS_AFTER_COST":
        return (
            f"{port} is not standalone edge yet, but {primitive_text} may clear "
            "costs when gated by participation, room-to-liquidity, and execution route."
        )
    if best and best.agent_verdict == "PROMOTABLE_PROOF_REQUIRES_UNTOUCHED_JUDGMENT":
        return (
            f"{port} already has proof-grade cells; only untouched judgment can decide "
            "promotion."
        )
    return (
        f"{port} should be tested as a causal confluence stack using {primitive_text}, "
        "then compared against the raw scanner baseline after fees."
    )


def _required_data(primitives: tuple[str, ...]) -> tuple[str, ...]:
    required = ["closed OHLCV candles", "fee/slippage model"]
    if "volume_participation" in primitives:
        required.append("venue-normalized volume z-score")
    if "liquidity_zone" in primitives or "sweep_reclaim" in primitives:
        required.append("swing/liquidity-zone state")
    if "mtf_bias" in primitives:
        required.append("closed-bar HTF alignment")
    return tuple(required)


def _guardrails(experiment_type: str) -> tuple[str, ...]:
    base = [
        "research-only; can_trade=false and can_promote=false",
        "closed-bar causality audit before replay",
        "fee-aware replay with maker/taker cost wall",
        "pre-registered untouched judgment before paper or shadow",
    ]
    if experiment_type == "execution_filtered_replay":
        base.append("taker allowed only when predicted edge pays fees plus buffer")
    if experiment_type == "edge_model_feature_bank":
        base.append("prove OOS uplift against raw scanner baseline")
    return tuple(base)


def _rationale(
    verdict: AgentVerdict,
    best_avg: float | None,
    best_pf: float | None,
    samples: int,
    primitives: tuple[str, ...],
) -> str:
    metric = (
        f"best avg={_fmt(best_avg)} bps, PF={_fmt(best_pf)}, samples={samples}; "
        f"primitives={', '.join(primitives[:4]) or 'none'}"
    )
    return {
        "PROMOTABLE_PROOF_REQUIRES_UNTOUCHED_JUDGMENT": (
            "Proof-like cell found; do not tune it, judge untouched data. "
        ),
        "POSITIVE_BUT_UNDER_SAMPLED": "Positive evidence exists but sample/gate strength is weak. ",
        "NEAR_MISS_AFTER_COST": (
            "The family is close enough to salvage with execution/context filters. "
        ),
        "CONVERT_TO_CONTEXT_FEATURE": (
            "Standalone edge failed, but causal atoms may help the edge model. "
        ),
        "FEATURE_BANK_ONLY": (
            "Standalone edge is negative after cost; reuse only as feature memory. "
        ),
        "BLOCKED_SOURCE_OR_CAUSALITY": "Source/causality is unresolved; do not replay yet. ",
        "AWAITING_REPLAY": "No completed evidence yet; port causal primitives first. ",
    }[verdict] + metric


def _cluster_use(mode: str) -> str:
    return {
        "proof_grade_positive": "freeze config; approve only untouched judgment",
        "positive_but_under_sampled_or_gate_weak": "extend data and validate cross-venue",
        "near_miss_after_cost": "add execution route and participation filters",
        "negative_after_cost": "feature bank only; never promote standalone",
        "blocked_source_or_causality": "source/causality cleanup before replay",
        "awaiting_replay": "port causal Python features and run fee-aware replay",
    }.get(mode, "review")


def _operator_answer(
    uplifts: Iterable[ScriptUplift],
    experiments: Iterable[UpliftExperiment],
) -> str:
    summary = _summary(tuple(uplifts), tuple(experiments))
    if summary["promotable_proofs"]:
        return (
            f"{summary['promotable_proofs']} proof-grade Pine-derived cells exist. "
            "Do not tune them; freeze and request untouched-window judgment. "
            f"{summary['near_miss_after_cost']} near misses should become "
            "execution-filtered replays."
        )
    if summary["near_miss_after_cost"] or summary["positive_under_sampled"]:
        return (
            "Failures are useful: route near-miss and sparse-positive families into "
            "confluence/feature-bank experiments, then prove OOS lift after costs."
        )
    return (
        "No promotable Pine-derived edge is visible yet. Preserve failures as "
        "negative labels and mine only causal primitive stacks, not looser signals."
    )


def _policy() -> dict:
    return {
        "research_only": True,
        "can_trade": False,
        "can_promote": False,
        "agent_role": "failure_salvage_and_experiment_planning",
        "allowed_actions": [
            "feature_bank",
            "confluence_stack_replay",
            "execution_filtered_replay",
            "untouched_judgment_request",
        ],
        "blocked_actions": [
            "auto_promote",
            "paper_trade_from_failed_cell",
            "copy_protected_source",
            "relax_live_risk_gates",
        ],
    }


def _uplift_rank(row: ScriptUplift) -> tuple[int, int, int, int]:
    verdict_rank = {
        "PROMOTABLE_PROOF_REQUIRES_UNTOUCHED_JUDGMENT": 5,
        "POSITIVE_BUT_UNDER_SAMPLED": 4,
        "NEAR_MISS_AFTER_COST": 3,
        "CONVERT_TO_CONTEXT_FEATURE": 2,
        "AWAITING_REPLAY": 1,
        "FEATURE_BANK_ONLY": 0,
        "BLOCKED_SOURCE_OR_CAUSALITY": -1,
    }[row.agent_verdict]
    return (row.salvage_score, verdict_rank, row.positive_cells, row.completed_cells)


def _best_float(values: Iterable[float | None]) -> float | None:
    vals = [float(value) for value in values if value is not None]
    return round(max(vals), 4) if vals else None


def _float(value: object) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _fmt(value: float | None) -> str:
    return "--" if value is None else f"{float(value):.2f}"


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="run Pine edge-uplift agent")
    parser.add_argument("--kb", default=str(DEFAULT_PINE_KB_PATH))
    parser.add_argument("--distiller", default=str(DEFAULT_DISTILLER_PATH))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--feed", default=str(DEFAULT_FEED))
    parser.add_argument("--max-scripts", type=int, default=80)
    parser.add_argument("--max-experiments", type=int, default=12)
    parser.add_argument("--interval-seconds", type=int, default=0)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    while True:
        payload = run_pine_edge_uplift_agent(
            kb_path=args.kb,
            distiller_path=args.distiller,
            max_scripts=args.max_scripts,
            max_experiments=args.max_experiments,
        )
        publish_pine_edge_uplift_agent(payload, out=args.out, feed=args.feed)
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            s = payload["summary"]
            print(
                "pine edge uplift agent "
                f"scripts={s['scripts_reviewed']} experiments={s['experiments']} "
                f"promotable={s['promotable_proofs']} near_miss={s['near_miss_after_cost']}",
                flush=True,
            )
        if args.interval_seconds <= 0:
            return 0
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
