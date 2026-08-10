"""Agentic Research OS supervisor.

This is the second research-only Quant OS layer. It does not execute research
jobs itself. It reads existing artifacts and turns them into a single operator
queue: what agents should keep working, what needs verifier proof, what should
decay or retire, and which tasks are stale.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

AGENTIC_RESEARCH_OS_ID = "agentic_research_os_v2"
DEFAULT_VIBE = Path("research/live_research/vibe_intelligence_latest.json")
DEFAULT_ALPHA_ARENA = Path("research/live_research/alpha_arena_lite_latest.json")
DEFAULT_GATEWAY = Path("logs/agent_gateway/quant_os/snapshot.json")
DEFAULT_QUANT_LOOP = Path("research/live_research/quant_loop_governance_latest.json")
DEFAULT_PAPER_PERFORMANCE = Path("research/live_research/paper_lane_performance_latest.json")
DEFAULT_OUT = Path("research/live_research/agentic_research_os_latest.json")
DEFAULT_FEED = Path("research/live_research/agentic_research_os_feed.jsonl")

TERMINAL_TASK_STATES = {
    "COMPLETED_RESEARCH_ONLY",
    "FAILED_RESEARCH_ONLY",
    "CANCELLED_RESEARCH_ONLY",
}


@dataclass(frozen=True)
class AgenticResearchOSConfig:
    stale_task_minutes: float = 90.0
    stale_artifact_minutes: float = 240.0
    repeated_decay_disable_after: int = 3
    min_verifier_artifacts_for_ready: int = 1
    max_actions: int = 25
    max_scorecards: int = 16

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_agentic_research_os(
    *,
    vibe_payload: Mapping[str, Any] | None = None,
    alpha_arena_payload: Mapping[str, Any] | None = None,
    gateway_snapshot: Mapping[str, Any] | None = None,
    quant_loop_payload: Mapping[str, Any] | None = None,
    paper_performance_payload: Mapping[str, Any] | None = None,
    config: AgenticResearchOSConfig = AgenticResearchOSConfig(),
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the operator-facing Agentic Research OS v2 payload."""

    generated = now or datetime.now(UTC)
    vibe = dict(vibe_payload or {})
    arena = dict(alpha_arena_payload or {})
    gateway = dict(gateway_snapshot or {})
    quant_loop = dict(quant_loop_payload or {})
    paper = dict(paper_performance_payload or {})

    hypothesis_actions = _hypothesis_actions(vibe, config=config)
    task_actions = _task_actions(gateway, generated, config=config)
    arena_actions = _arena_actions(arena, config=config)
    paper_actions = _paper_actions(paper, config=config)
    loop_actions = _loop_actions(quant_loop, config=config)

    actions = _rank_actions(
        [*hypothesis_actions, *task_actions, *arena_actions, *paper_actions, *loop_actions],
        limit=config.max_actions,
    )
    scorecards = _agent_scorecards(
        vibe=vibe,
        arena=arena,
        gateway=gateway,
        quant_loop=quant_loop,
        paper=paper,
        actions=actions,
        now=generated,
        config=config,
    )
    summary = _summary(
        vibe=vibe,
        arena=arena,
        gateway=gateway,
        quant_loop=quant_loop,
        paper=paper,
        actions=actions,
        scorecards=scorecards,
    )
    return {
        "os_id": AGENTIC_RESEARCH_OS_ID,
        "generated_at": generated.isoformat(),
        "mode": "research_only_agent_supervisor",
        "summary": summary,
        "policy": _policy(config),
        "agent_scorecards": scorecards[: config.max_scorecards],
        "operator_queue": actions,
        "source_status": _source_status(
            vibe=vibe,
            arena=arena,
            gateway=gateway,
            quant_loop=quant_loop,
            paper=paper,
            now=generated,
            config=config,
        ),
        "operator_answer": _operator_answer(summary, actions),
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
    }


def build_agentic_research_os_from_files(
    *,
    vibe_path: Path | str = DEFAULT_VIBE,
    alpha_arena_path: Path | str = DEFAULT_ALPHA_ARENA,
    gateway_snapshot_path: Path | str = DEFAULT_GATEWAY,
    quant_loop_path: Path | str = DEFAULT_QUANT_LOOP,
    paper_performance_path: Path | str = DEFAULT_PAPER_PERFORMANCE,
    config: AgenticResearchOSConfig = AgenticResearchOSConfig(),
    now: datetime | None = None,
) -> dict[str, Any]:
    return run_agentic_research_os(
        vibe_payload=_read_json(Path(vibe_path)),
        alpha_arena_payload=_read_json(Path(alpha_arena_path)),
        gateway_snapshot=_read_json(Path(gateway_snapshot_path)),
        quant_loop_payload=_read_json(Path(quant_loop_path)),
        paper_performance_payload=_read_json(Path(paper_performance_path)),
        config=config,
        now=now,
    )


def publish_agentic_research_os(
    payload: Mapping[str, Any],
    *,
    out: Path | str = DEFAULT_OUT,
    feed: Path | str | None = DEFAULT_FEED,
) -> Path:
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n"
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
            handle.write(json.dumps(_feed_record(payload), sort_keys=True, default=str) + "\n")
        feed_path.chmod(0o644)
    return out_path


def _hypothesis_actions(
    vibe: Mapping[str, Any], *, config: AgenticResearchOSConfig
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for card in _list(vibe.get("cards")):
        state = str(card.get("lifecycle_state") or "INCUBATING")
        decay_count = _int(card.get("decay_score"))
        times_seen = _int(card.get("times_seen"))
        vetoes = tuple(str(v) for v in _list(card.get("vetoes")))
        blocked = tuple(str(v) for v in _list(card.get("blocked_by")))
        base = _base_from_card(card)
        if state == "DISABLED" or (
            state == "DECAYED"
            and times_seen >= config.repeated_decay_disable_after
            and decay_count >= 60
        ):
            actions.append(
                _action(
                    **base,
                    action="RETIRE_HYPOTHESIS",
                    bucket="RETIRE",
                    severity="critical",
                    priority=96,
                    reason="repeated decay or disabled state; stop cycling agent effort",
                    evidence={
                        "times_seen": times_seen,
                        "decay_score": decay_count,
                        "vetoes": vetoes,
                    },
                )
            )
        elif state == "DECAYED":
            actions.append(
                _action(
                    **base,
                    action="DECAY_AND_REFRAME",
                    bucket="DECAY",
                    severity="warning",
                    priority=78,
                    reason="evidence says this hypothesis is unhealthy as currently framed",
                    evidence={"vetoes": vetoes, "blocked_by": blocked},
                )
            )
        elif "requires_untouched_judgment" in vetoes or any(
            "untouched" in item for item in blocked
        ):
            actions.append(
                _action(
                    **base,
                    action="REQUEST_UNTOUCHED_VERIFIER",
                    bucket="VERIFY",
                    severity="info",
                    priority=88,
                    reason=(
                        "candidate needs one-shot untouched-window verification before promotion"
                    ),
                    evidence={"blocked_by": blocked, "next_action": card.get("next_action")},
                )
            )
        elif state == "MONITORING":
            actions.append(
                _action(
                    **base,
                    action="COLLECT_SHADOW_OR_PAPER_OUTCOMES",
                    bucket="MONITOR",
                    severity="info",
                    priority=70,
                    reason="candidate is in monitoring; collect outcomes, do not tune mid-trial",
                    evidence={"latest_verdict": card.get("latest_verdict")},
                )
            )
        elif state == "ACTIVE":
            actions.append(
                _action(
                    **base,
                    action="KEEP_NEXT_PROOF_STEP",
                    bucket="KEEP",
                    severity="info",
                    priority=62,
                    reason="active hypothesis still has a falsifiable proof step",
                    evidence={
                        "next_action": card.get("next_action"),
                        "proof_step": card.get("proof_step"),
                    },
                )
            )
    return actions


def _task_actions(
    gateway: Mapping[str, Any],
    now: datetime,
    *,
    config: AgenticResearchOSConfig,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    artifacts = gateway.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, Mapping) else {}
    artifact_count_by_task = Counter(
        str(row.get("task_id") or "") for row in _list(artifacts.get("recent"))
    )
    for task in _list(gateway.get("tasks")):
        status = str(task.get("status") or "UNKNOWN")
        task_id = str(task.get("task_id") or "")
        age_min = _age_minutes(_parse_dt(task.get("updated_at") or task.get("created_at")), now)
        target = task.get("target") if isinstance(task.get("target"), Mapping) else {}
        payload = task.get("payload") if isinstance(task.get("payload"), Mapping) else {}
        base = {
            "entity_id": task_id or str(task.get("kind") or "task"),
            "source": "quant_os_agent_gateway_v2",
            "strategy_id": _safe(
                payload.get("strategy_id") or payload.get("family") or task.get("kind")
            ),
            "exchange": _safe(target.get("exchange")),
            "symbol": _safe(target.get("symbol")),
            "timeframe": _safe(target.get("timeframe")),
        }
        if status not in TERMINAL_TASK_STATES and age_min > config.stale_task_minutes:
            actions.append(
                _action(
                    **base,
                    action="RECLAIM_OR_FAIL_STALE_TASK",
                    bucket="REPAIR",
                    severity="warning",
                    priority=92,
                    reason=f"task has been {status.lower()} for {age_min:.1f} minutes",
                    evidence={"status": status, "age_minutes": round(age_min, 2)},
                )
            )
        elif status == "FAILED_RESEARCH_ONLY":
            actions.append(
                _action(
                    **base,
                    action="REVIEW_FAILED_AGENT_TASK",
                    bucket="REPAIR",
                    severity="warning",
                    priority=82,
                    reason=(
                        "agent task failed; capture failure as evidence and decide retry or retire"
                    ),
                    evidence={"last_event": task.get("last_event")},
                )
            )
        elif (
            status == "COMPLETED_RESEARCH_ONLY"
            and artifact_count_by_task[task_id] < config.min_verifier_artifacts_for_ready
        ):
            actions.append(
                _action(
                    **base,
                    action="ATTACH_VERIFIER_ARTIFACT",
                    bucket="VERIFY",
                    severity="warning",
                    priority=74,
                    reason="completed task lacks enough registered artifacts for operator audit",
                    evidence={"artifact_count": artifact_count_by_task[task_id]},
                )
            )
    return actions


def _arena_actions(
    arena: Mapping[str, Any], *, config: AgenticResearchOSConfig
) -> list[dict[str, Any]]:
    del config
    actions: list[dict[str, Any]] = []
    for card in _list(arena.get("scorecards")):
        verdict = str(card.get("arena_verdict") or "")
        base = {
            "entity_id": str(card.get("candidate_id") or card.get("experiment_id") or "arena"),
            "source": "alpha_arena_lite_v1",
            "strategy_id": _safe(card.get("strategy_id")),
            "exchange": _safe(card.get("exchange")),
            "symbol": _safe(card.get("symbol")),
            "timeframe": ",".join(str(v) for v in _list(card.get("timeframes")))
            or _safe(card.get("timeframe")),
        }
        if verdict == "PRE_REGISTER_UNTOUCHED_JUDGMENT":
            actions.append(
                _action(
                    **base,
                    action="ASK_OPERATOR_FOR_ONE_SHOT_JUDGMENT",
                    bucket="VERIFY",
                    severity="info",
                    priority=94,
                    reason="arena says the next legitimate step is untouched-window judgment",
                    evidence={"verdict": verdict, "metrics": card.get("metrics") or {}},
                )
            )
        elif verdict == "EXPAND_UNTOUCHED_SAMPLE":
            actions.append(
                _action(
                    **base,
                    action="EXPAND_SAMPLE_ON_NEXT_UNTOUCHED_WINDOW",
                    bucket="EXPAND",
                    severity="info",
                    priority=86,
                    reason="sparse positive evidence needs more untouched outcomes",
                    evidence={"verdict": verdict, "metrics": card.get("metrics") or {}},
                )
            )
        elif verdict == "EXECUTION_SALVAGE_REQUIRED":
            actions.append(
                _action(
                    **base,
                    action="RUN_EXECUTION_SALVAGE_BEFORE_MORE_ENTRIES",
                    bucket="REPAIR",
                    severity="warning",
                    priority=84,
                    reason="edge is near the fee wall; route/capture must improve first",
                    evidence={"verdict": verdict, "metrics": card.get("metrics") or {}},
                )
            )
    return actions


def _paper_actions(
    paper: Mapping[str, Any], *, config: AgenticResearchOSConfig
) -> list[dict[str, Any]]:
    del config
    actions: list[dict[str, Any]] = []
    for row in _list(paper.get("rows")):
        state = str(row.get("state") or "")
        base = {
            "entity_id": str(row.get("lane_id") or row.get("strategy_id") or "paper_lane"),
            "source": "paper_lane_performance_v1",
            "strategy_id": _safe(row.get("strategy_id") or row.get("lane_id")),
            "exchange": _safe(row.get("exchange")),
            "symbol": _safe(row.get("symbol")),
            "timeframe": _safe(row.get("timeframe")),
        }
        if state == "PAPER_PROMOTION_CANDIDATE":
            actions.append(
                _action(
                    **base,
                    action="REQUEST_HUMAN_PROMOTION_REVIEW",
                    bucket="VERIFY",
                    severity="info",
                    priority=98,
                    reason=(
                        "paper performance claims a promotion candidate; verify untouched proof "
                        "and route truth"
                    ),
                    evidence={
                        "net_pnl_usd": row.get("net_pnl_usd"),
                        "profit_factor": row.get("profit_factor"),
                    },
                )
            )
        elif state == "PAPER_ACTIVE_NEGATIVE":
            actions.append(
                _action(
                    **base,
                    action="DECAY_OR_REPAIR_PAPER_LANE",
                    bucket="DECAY",
                    severity="warning",
                    priority=90,
                    reason="paper lane is negative; stop promoting and run exit/route autopsy",
                    evidence={
                        "net_pnl_usd": row.get("net_pnl_usd"),
                        "closed_trades": row.get("closed_trades"),
                    },
                )
            )
    return actions


def _loop_actions(
    quant_loop: Mapping[str, Any], *, config: AgenticResearchOSConfig
) -> list[dict[str, Any]]:
    del config
    actions: list[dict[str, Any]] = []
    for gate in _list(quant_loop.get("gate_checks")):
        status = str(gate.get("status") or "")
        if status not in {"BLOCKED", "WARN"}:
            continue
        actions.append(
            _action(
                entity_id=str(gate.get("gate_id") or "quant_loop_gate"),
                source="quant_loop_governance_v1",
                strategy_id="quant_loop",
                exchange="",
                symbol="",
                timeframe="",
                action="FIX_QUANT_LOOP_GATE",
                bucket="REPAIR",
                severity="critical" if status == "BLOCKED" else "warning",
                priority=95 if status == "BLOCKED" else 80,
                reason=str(
                    gate.get("detail") or gate.get("message") or "quant loop gate needs attention"
                ),
                evidence={"status": status},
            )
        )
    return actions


def _agent_scorecards(
    *,
    vibe: Mapping[str, Any],
    arena: Mapping[str, Any],
    gateway: Mapping[str, Any],
    quant_loop: Mapping[str, Any],
    paper: Mapping[str, Any],
    actions: list[dict[str, Any]],
    now: datetime,
    config: AgenticResearchOSConfig,
) -> list[dict[str, Any]]:
    del config
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for action in actions:
        grouped[str(action.get("source") or "unknown")].append(action)

    scorecards = [
        _scorecard(
            agent_id="hypothesis_memory",
            role="tracks keep/decay/retire lifecycle",
            source="vibe_intelligence_v1",
            freshness_minutes=_age_minutes(_parse_dt(vibe.get("generated_at")), now),
            raw_summary=vibe.get("summary") or {},
            actions=grouped.get("vibe_intelligence_v1", []),
        ),
        _scorecard(
            agent_id="arena_verifier",
            role="turns sparse positives into next proof tasks",
            source="alpha_arena_lite_v1",
            freshness_minutes=_age_minutes(_parse_dt(arena.get("generated_at")), now),
            raw_summary=arena.get("summary") or {},
            actions=grouped.get("alpha_arena_lite_v1", []),
        ),
        _scorecard(
            agent_id="task_ledger",
            role="durable Quant OS task/event/artifact registry",
            source="quant_os_agent_gateway_v2",
            freshness_minutes=_age_minutes(_parse_dt(gateway.get("generated_at")), now),
            raw_summary=gateway.get("summary") or {},
            actions=grouped.get("quant_os_agent_gateway_v2", []),
        ),
        _scorecard(
            agent_id="loop_governor",
            role="checks collisions, stale loops, and research budget",
            source="quant_loop_governance_v1",
            freshness_minutes=_age_minutes(_parse_dt(quant_loop.get("generated_at")), now),
            raw_summary=quant_loop.get("summary") or {},
            actions=grouped.get("quant_loop_governance_v1", []),
        ),
        _scorecard(
            agent_id="paper_outcome_watcher",
            role="feeds paper results back into keep/decay decisions",
            source="paper_lane_performance_v1",
            freshness_minutes=_age_minutes(_parse_dt(paper.get("generated_at")), now),
            raw_summary=paper.get("summary") or {},
            actions=grouped.get("paper_lane_performance_v1", []),
        ),
    ]
    return sorted(
        scorecards,
        key=lambda row: (-_float(row.get("health_score")), row["agent_id"]),
    )


def _scorecard(
    *,
    agent_id: str,
    role: str,
    source: str,
    freshness_minutes: float,
    raw_summary: Mapping[str, Any],
    actions: list[dict[str, Any]],
) -> dict[str, Any]:
    critical = sum(1 for row in actions if row.get("severity") == "critical")
    warnings = sum(1 for row in actions if row.get("severity") == "warning")
    health = 100.0
    if freshness_minutes > 240:
        health -= 25.0
    if freshness_minutes > 720:
        health -= 35.0
    health -= critical * 18.0 + warnings * 9.0
    return {
        "agent_id": agent_id,
        "source": source,
        "role": role,
        "health_score": round(_clamp(health, 0.0, 100.0), 2),
        "freshness_minutes": round(freshness_minutes, 2),
        "critical_actions": critical,
        "warning_actions": warnings,
        "action_count": len(actions),
        "summary": _dict(raw_summary),
        "status": "HEALTHY" if health >= 75 and not critical else "NEEDS_OPERATOR_REVIEW",
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
    }


def _summary(
    *,
    vibe: Mapping[str, Any],
    arena: Mapping[str, Any],
    gateway: Mapping[str, Any],
    quant_loop: Mapping[str, Any],
    paper: Mapping[str, Any],
    actions: list[dict[str, Any]],
    scorecards: list[dict[str, Any]],
) -> dict[str, Any]:
    vibe_summary = _dict(vibe.get("summary"))
    arena_summary = _dict(arena.get("summary"))
    gateway_summary = _dict(gateway.get("summary"))
    quant_loop_summary = _dict(quant_loop.get("summary"))
    paper_summary = _dict(paper.get("summary"))
    buckets = Counter(str(row.get("bucket") or "UNKNOWN") for row in actions)
    severities = Counter(str(row.get("severity") or "info") for row in actions)
    return {
        "hypotheses": _int(vibe_summary.get("hypotheses")),
        "active_hypotheses": _int(vibe_summary.get("active")),
        "monitoring_hypotheses": _int(vibe_summary.get("monitoring")),
        "decayed_hypotheses": _int(vibe_summary.get("decayed")),
        "disabled_hypotheses": _int(vibe_summary.get("disabled")),
        "arena_candidates": _int(arena_summary.get("candidate_count")),
        "gateway_tasks": _int(gateway_summary.get("total_tasks")),
        "gateway_active_tasks": _int(gateway_summary.get("active")),
        "paper_negative_lanes": _int(paper_summary.get("active_negative")),
        "quant_loop_readiness": quant_loop_summary.get("readiness_level", "UNKNOWN"),
        "operator_actions": len(actions),
        "critical_actions": severities.get("critical", 0),
        "warning_actions": severities.get("warning", 0),
        "buckets": dict(sorted(buckets.items())),
        "agent_health_min": min(
            (_float(row.get("health_score")) for row in scorecards),
            default=0.0,
        ),
        "research_only": True,
        "can_trade": False,
        "can_promote": False,
    }


def _source_status(
    *,
    vibe: Mapping[str, Any],
    arena: Mapping[str, Any],
    gateway: Mapping[str, Any],
    quant_loop: Mapping[str, Any],
    paper: Mapping[str, Any],
    now: datetime,
    config: AgenticResearchOSConfig,
) -> list[dict[str, Any]]:
    return [
        _source_row("vibe_intelligence", vibe.get("generated_at"), bool(vibe), now, config),
        _source_row("alpha_arena_lite", arena.get("generated_at"), bool(arena), now, config),
        _source_row(
            "quant_os_agent_gateway",
            gateway.get("generated_at"),
            bool(gateway),
            now,
            config,
        ),
        _source_row(
            "quant_loop_governance",
            quant_loop.get("generated_at"),
            bool(quant_loop),
            now,
            config,
        ),
        _source_row("paper_lane_performance", paper.get("generated_at"), bool(paper), now, config),
    ]


def _source_row(
    source: str,
    generated_at: Any,
    present: bool,
    now: datetime,
    config: AgenticResearchOSConfig,
) -> dict[str, Any]:
    age = _age_minutes(_parse_dt(generated_at), now)
    if not present:
        state = "MISSING"
    elif age > config.stale_artifact_minutes:
        state = "STALE"
    else:
        state = "OK"
    return {
        "source": source,
        "state": state,
        "age_minutes": round(age, 2),
        "generated_at": generated_at,
    }


def _policy(config: AgenticResearchOSConfig) -> dict[str, Any]:
    return {
        "surface": "research_only_agent_supervisor",
        "config": config.to_dict(),
        "orders_allowed": False,
        "promotion_allowed": False,
        "strategy_mutation_allowed": False,
        "agent_live_trading_scope_available": False,
        "requires_human_promotion": True,
        "requires_untouched_judgment": True,
        "principle": (
            "agents can keep, decay, retire, and request proof work; they cannot "
            "place orders, approve promotions, or change live configuration"
        ),
    }


def _operator_answer(summary: Mapping[str, Any], actions: list[dict[str, Any]]) -> str:
    if _int(summary.get("critical_actions")):
        return (
            "Research OS needs operator attention: fix critical stale/retire actions "
            "before trusting new agent work."
        )
    if any(row.get("bucket") == "VERIFY" for row in actions):
        return (
            "Research OS is ready for verifier work: review the queued untouched/paper "
            "proof requests before promotion."
        )
    if actions:
        return (
            "Research OS is healthy enough to continue research; work the queue top-down "
            "and keep it research-only."
        )
    return (
        "Research OS has no active actions; wait for new council, arena, gateway, "
        "or paper evidence."
    )


def _rank_actions(actions: Iterable[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    order = {"critical": 0, "warning": 1, "info": 2}
    unique: dict[tuple[str, str, str], dict[str, Any]] = {}
    for action in actions:
        key = (
            str(action.get("source")),
            str(action.get("entity_id")),
            str(action.get("action")),
        )
        existing = unique.get(key)
        if existing is None or _float(action.get("priority")) > _float(existing.get("priority")):
            unique[key] = action
    ranked = sorted(
        unique.values(),
        key=lambda row: (
            order.get(str(row.get("severity")), 9),
            -_float(row.get("priority")),
            str(row.get("source")),
            str(row.get("entity_id")),
        ),
    )
    return ranked[: max(1, int(limit))]


def _action(
    *,
    entity_id: str,
    source: str,
    strategy_id: str,
    exchange: str,
    symbol: str,
    timeframe: str,
    action: str,
    bucket: str,
    severity: str,
    priority: int,
    reason: str,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "entity_id": entity_id,
        "source": source,
        "strategy_id": strategy_id,
        "exchange": exchange,
        "symbol": symbol,
        "timeframe": timeframe,
        "action": action,
        "bucket": bucket,
        "severity": severity,
        "priority": int(priority),
        "reason": reason,
        "evidence": dict(evidence or {}),
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
    }


def _base_from_card(card: Mapping[str, Any]) -> dict[str, str]:
    return {
        "entity_id": str(card.get("hypothesis_id") or card.get("candidate_id") or "hypothesis"),
        "source": "vibe_intelligence_v1",
        "strategy_id": _safe(card.get("family") or card.get("candidate_id")),
        "exchange": _safe(card.get("exchange")),
        "symbol": _safe(card.get("symbol")),
        "timeframe": _safe(card.get("timeframe")),
    }


def _feed_record(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "os_id": payload.get("os_id"),
        "generated_at": payload.get("generated_at"),
        "summary": payload.get("summary", {}),
        "operator_answer": payload.get("operator_answer"),
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _age_minutes(value: datetime | None, now: datetime) -> float:
    if value is None:
        return 9_999.0
    return max(0.0, (now - value).total_seconds() / 60.0)


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _safe(value: Any) -> str:
    return "" if value is None else str(value)


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return parsed if math.isfinite(parsed) else 0.0


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="publish Agentic Research OS v2")
    parser.add_argument("--vibe", default=str(DEFAULT_VIBE))
    parser.add_argument("--alpha-arena", default=str(DEFAULT_ALPHA_ARENA))
    parser.add_argument("--gateway-snapshot", default=str(DEFAULT_GATEWAY))
    parser.add_argument("--quant-loop", default=str(DEFAULT_QUANT_LOOP))
    parser.add_argument("--paper-performance", default=str(DEFAULT_PAPER_PERFORMANCE))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--feed", default=str(DEFAULT_FEED))
    parser.add_argument("--interval-seconds", type=int, default=0)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    while True:
        payload = build_agentic_research_os_from_files(
            vibe_path=Path(args.vibe),
            alpha_arena_path=Path(args.alpha_arena),
            gateway_snapshot_path=Path(args.gateway_snapshot),
            quant_loop_path=Path(args.quant_loop),
            paper_performance_path=Path(args.paper_performance),
        )
        out = publish_agentic_research_os(
            payload,
            out=Path(args.out),
            feed=None if args.feed == "" else Path(args.feed),
        )
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True, default=str), flush=True)
        else:
            print(f"agentic research os wrote {out}", flush=True)
            print(payload["operator_answer"], flush=True)
        if args.interval_seconds <= 0:
            break
        time.sleep(max(1, args.interval_seconds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
