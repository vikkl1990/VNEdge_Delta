"""Persistent alpha workbench for research proof tasks.

The alpha council decides what each hypothesis needs next. The workbench turns
those council decisions into durable, restart-safe proof tasks so the research
loop does not rediscover the same work every cycle.

It is intentionally research-only: no orders, no promotion, no parameter
mutation, and no execution mounts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from vnedge.research.alpha_council import run_alpha_council

WORKBENCH_ID = "alpha_workbench_v1"
DEFAULT_RESEARCH_DIR = Path("research/live_research")
DEFAULT_STORE_DIR = DEFAULT_RESEARCH_DIR / "alpha_workbench"
DEFAULT_LATEST = DEFAULT_RESEARCH_DIR / "alpha_workbench_latest.json"
DEFAULT_FEED = DEFAULT_RESEARCH_DIR / "alpha_workbench_feed.jsonl"


@dataclass(frozen=True)
class ProofTask:
    task_id: str
    task_type: str
    next_action: str
    proof_step: str
    candidate_id: str
    source: str
    family: str
    exchange: str
    symbol: str
    timeframe: str
    candidate_state: str
    route_decision: str
    priority_score: float
    council_verdict: str
    vetoes: tuple[str, ...]
    blocked_by: tuple[str, ...]
    rationale: str
    evidence_digest: str
    candidate: dict[str, Any]
    can_trade: bool = False
    can_promote: bool = False
    live_orders_enabled: bool = False
    requires_human_approval: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_alpha_workbench(
    research_dir: Path | str = DEFAULT_RESEARCH_DIR,
    *,
    store_dir: Path | str | None = DEFAULT_STORE_DIR,
    max_tasks: int = 50,
    council_payload: dict | None = None,
) -> dict[str, Any]:
    """Build and optionally persist the current alpha proof backlog."""
    research = Path(research_dir)
    council = council_payload or _load_council_or_run(research, max_tasks=max_tasks)
    tasks = build_proof_tasks(council, max_tasks=max_tasks)
    persisted = (
        persist_proof_tasks(tasks, Path(store_dir))
        if store_dir is not None else _memory_manifest(tasks)
    )
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "workbench_id": WORKBENCH_ID,
        "mode": "research_only_persistent_proof_queue",
        "policy": alpha_workbench_policy(),
        "source": {
            "council_id": council.get("council_id"),
            "council_generated_at": council.get("generated_at"),
            "research_dir": str(research),
        },
        "summary": _summary(tasks, persisted),
        "persistence": persisted,
        "tasks": [task.to_dict() for task in tasks],
    }
    return payload


def alpha_workbench_policy() -> dict[str, Any]:
    return {
        "status": "research_only",
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
        "orders_allowed": False,
        "auto_promotion_allowed": False,
        "principle": (
            "proof tasks are durable research instructions only; every lane "
            "still requires replay, untouched judgment, human approval, and "
            "normal shadow/paper gates before trading discussion"
        ),
        "supported_actions": sorted(_ACTION_CONTRACTS),
    }


def build_proof_tasks(
    council_payload: Mapping[str, Any],
    *,
    max_tasks: int = 50,
) -> tuple[ProofTask, ...]:
    tasks: list[ProofTask] = []
    debates = council_payload.get("debates", [])
    if not isinstance(debates, list):
        return ()
    for row in debates:
        if not isinstance(row, Mapping):
            continue
        action = str(row.get("next_action") or "")
        contract = _ACTION_CONTRACTS.get(action)
        if contract is None:
            continue
        candidate = row.get("candidate")
        if not isinstance(candidate, Mapping):
            continue
        task = _task_from_debate(row, candidate, action, contract)
        tasks.append(task)
    tasks.sort(key=lambda task: (-task.priority_score, task.task_id))
    return tuple(tasks[:max_tasks])


def persist_proof_tasks(tasks: Iterable[ProofTask], store_dir: Path) -> dict[str, Any]:
    """Checkpoint each task as a content-addressed chunk and update a manifest."""
    tasks = tuple(tasks)
    now = datetime.now(UTC).isoformat()
    store_dir.mkdir(parents=True, exist_ok=True)
    chunks_dir = store_dir / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = store_dir / "manifest.json"
    manifest = _read_manifest(manifest_path)
    records = manifest.setdefault("tasks", {})
    new_count = 0
    updated_count = 0
    unchanged_count = 0

    for task in tasks:
        task_payload = task.to_dict()
        content_hash = _stable_digest(task_payload, length=32)
        record = records.get(task.task_id)
        chunk_name = (
            f"{_slug(task.task_type)}__{_stable_digest(task.task_id, length=16)}.json"
        )
        chunk_path = chunks_dir / chunk_name
        if not isinstance(record, dict):
            new_count += 1
            first_seen = now
            times_seen = 0
            previous_hash = None
        else:
            first_seen = str(record.get("first_seen_at") or now)
            times_seen = int(record.get("times_seen", 0) or 0)
            previous_hash = record.get("content_hash")

        if previous_hash == content_hash and chunk_path.exists():
            unchanged_count += 1
        else:
            if record is not None:
                updated_count += 1
            _write_json_atomic(
                {
                    "workbench_id": WORKBENCH_ID,
                    "task": task_payload,
                    "content_hash": content_hash,
                    "written_at": now,
                },
                chunk_path,
            )

        records[task.task_id] = {
            "task_id": task.task_id,
            "task_type": task.task_type,
            "next_action": task.next_action,
            "source": task.source,
            "candidate_id": task.candidate_id,
            "content_hash": content_hash,
            "chunk": str(chunk_path.relative_to(store_dir)),
            "status": "OPEN",
            "priority_score": task.priority_score,
            "first_seen_at": first_seen,
            "last_seen_at": now,
            "times_seen": times_seen + 1,
            "can_trade": False,
            "can_promote": False,
        }

    manifest.update({
        "workbench_id": WORKBENCH_ID,
        "updated_at": now,
        "total_tasks_seen": len(records),
    })
    _write_json_atomic(manifest, manifest_path)
    return {
        "store_dir": str(store_dir),
        "manifest_path": str(manifest_path),
        "stored_tasks": len(tasks),
        "known_tasks": len(records),
        "new_tasks": new_count,
        "updated_tasks": updated_count,
        "unchanged_tasks": unchanged_count,
    }


def publish_alpha_workbench(payload: dict[str, Any], out: Path, feed: Path | None = None) -> None:
    _write_json_atomic(payload, out)
    if feed is not None:
        feed.parent.mkdir(parents=True, exist_ok=True)
        with open(feed, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, default=str) + "\n")


_ACTION_CONTRACTS: dict[str, dict[str, str]] = {
    "RUN_CONSERVATIVE_L2_REPLAY": {
        "task_type": "conservative_replay",
        "proof_step": "run conservative tick/L2 replay with maker/taker route audit",
    },
    "QUEUE_SHADOW_TRIAL_AFTER_REPLAY": {
        "task_type": "shadow_trial_after_replay",
        "proof_step": (
            "open a governed shadow-trial manifest only for replay-passed "
            "candidates; every order intent still traverses the risk gateway"
        ),
    },
    "MINE_PRE_EVENT_EXECUTION_CONDITIONS": {
        "task_type": "execution_condition_mining",
        "proof_step": (
            "mine pre-event quoteability, fillability, adverse-selection, and "
            "fee-wall filters for replay-failed candidates without promoting "
            "from the seen replay window"
        ),
    },
    "RUN_FILTERED_REPLAY_FROM_EXECUTION_CONDITIONS": {
        "task_type": "filtered_conservative_replay",
        "proof_step": (
            "freeze the execution-condition filter and run conservative replay "
            "on a fresh or explicitly governed slice before any shadow trial"
        ),
    },
    "PRE_REGISTER_UNTOUCHED_JUDGMENT": {
        "task_type": "untouched_judgment",
        "proof_step": "write fixed manifest for an untouched out-of-sample judgment window",
    },
    "RECORD_MORE_TICKS": {
        "task_type": "data_collection",
        "proof_step": "continue public tick/L2 recording until sample and coverage gates are met",
    },
    "REPAIR_EXIT_PAYOFF": {
        "task_type": "exit_repair",
        "proof_step": (
            "test exit-only variants against the same entry evidence; no entry "
            "threshold changes and no promotion from seen data"
        ),
    },
    "CHECK_ZERO_WINDOW_STABILITY": {
        "task_type": "stability_check",
        "proof_step": (
            "pre-register a stability check with wider windows or longer horizon "
            "to determine whether the positive reject is sparse or dead"
        ),
    },
    "PRE_REGISTER_NEAR_PASS_JUDGMENT": {
        "task_type": "near_pass_judgment",
        "proof_step": "write fixed untouched-window manifest for a positive rejected lane",
    },
    "DIAGNOSE_CLOSE_REJECT": {
        "task_type": "close_reject_diagnosis",
        "proof_step": "classify the unresolved close-reject blocker before any tuning",
    },
    "REFRESH_STALE_ARTIFACT": {
        "task_type": "artifact_refresh",
        "proof_step": "refresh the missing or stale research artifact from its producer service",
    },
    "SPLIT_REPLAY_BY_BTC_REGIME": {
        "task_type": "regime_replay_split",
        "proof_step": (
            "split replay and strategy research by Bitcoin network/mempool "
            "regime tags before judging whether an edge is conditional"
        ),
    },
    "REFRESH_BITCOIN_NODE_HEALTH": {
        "task_type": "bitcoin_node_health",
        "proof_step": (
            "refresh the Bitcoin regime artifact from read-only node and "
            "mempool telemetry before using network context"
        ),
    },
}


def _load_council_or_run(research_dir: Path, *, max_tasks: int) -> dict:
    latest = research_dir / "alpha_council_latest.json"
    payload = _read_json(latest)
    if payload:
        return payload
    return run_alpha_council(research_dir, max_candidates=max_tasks)


def _task_from_debate(
    row: Mapping[str, Any],
    candidate: Mapping[str, Any],
    action: str,
    contract: Mapping[str, str],
) -> ProofTask:
    candidate_id = str(candidate.get("candidate_id") or "unknown_candidate")
    task_type = contract["task_type"]
    task_id = _task_id(action, candidate_id)
    vetoes = tuple(str(v) for v in row.get("vetoes", []) if isinstance(v, str))
    blocked_by = _blocked_by(action, vetoes)
    return ProofTask(
        task_id=task_id,
        task_type=task_type,
        next_action=action,
        proof_step=contract["proof_step"],
        candidate_id=candidate_id,
        source=str(candidate.get("source", "unknown")),
        family=str(candidate.get("family", "unknown")),
        exchange=str(candidate.get("exchange", "unknown")),
        symbol=str(candidate.get("symbol", "unknown")),
        timeframe=str(candidate.get("timeframe", "unknown")),
        candidate_state=str(candidate.get("state", "UNKNOWN")),
        route_decision=str(candidate.get("route_decision", "UNKNOWN")),
        priority_score=_float(row.get("priority_score")),
        council_verdict=str(row.get("council_verdict", "UNKNOWN")),
        vetoes=vetoes,
        blocked_by=blocked_by,
        rationale=_rationale(row),
        evidence_digest=_stable_digest(candidate.get("evidence", {}), length=16),
        candidate=dict(candidate),
    )


def _task_id(action: str, candidate_id: str) -> str:
    digest = _stable_digest({"action": action, "candidate_id": candidate_id}, length=16)
    return f"{_slug(action)}|{digest}"


def _blocked_by(action: str, vetoes: tuple[str, ...]) -> tuple[str, ...]:
    required = {
        "RUN_CONSERVATIVE_L2_REPLAY": ("conservative_replay_result",),
        "QUEUE_SHADOW_TRIAL_AFTER_REPLAY": (
            "human_approved_shadow_manifest",
            "risk_gateway_path",
            "shadow_intent_journal",
        ),
        "MINE_PRE_EVENT_EXECUTION_CONDITIONS": (
            "candidate_replay_failure",
            "pre_event_feature_report",
            "no_seen_window_promotion",
        ),
        "RUN_FILTERED_REPLAY_FROM_EXECUTION_CONDITIONS": (
            "frozen_execution_condition_filter",
            "fresh_replay_slice",
            "conservative_replay_result",
            "no_seen_window_promotion",
        ),
        "PRE_REGISTER_UNTOUCHED_JUDGMENT": ("human_approved_manifest",),
        "RECORD_MORE_TICKS": ("sample_size_and_coverage",),
        "REPAIR_EXIT_PAYOFF": ("exit_repair_backtest", "unchanged_entry_manifest"),
        "CHECK_ZERO_WINDOW_STABILITY": ("stability_resample_result", "pre_registered_window"),
        "PRE_REGISTER_NEAR_PASS_JUDGMENT": ("human_approved_manifest",),
        "DIAGNOSE_CLOSE_REJECT": ("diagnosis_report",),
        "REFRESH_STALE_ARTIFACT": ("fresh_artifact_publish",),
        "SPLIT_REPLAY_BY_BTC_REGIME": (
            "bitcoin_regime_artifact",
            "per_regime_replay_report",
        ),
        "REFRESH_BITCOIN_NODE_HEALTH": ("fresh_bitcoin_regime_artifact",),
    }.get(action, ())
    return tuple(dict.fromkeys((*vetoes, *required)))


def _rationale(row: Mapping[str, Any]) -> str:
    debate = row.get("debate")
    if isinstance(debate, list):
        for opinion in debate:
            if isinstance(opinion, Mapping) and opinion.get("agent_id") == "research_director":
                argument = str(opinion.get("argument") or "").strip()
                if argument:
                    return argument
    return str(row.get("next_action") or "research task")


def _summary(tasks: tuple[ProofTask, ...], persisted: Mapping[str, Any]) -> dict[str, Any]:
    by_type = Counter(task.task_type for task in tasks)
    by_action = Counter(task.next_action for task in tasks)
    by_source = Counter(task.source for task in tasks)
    top = tasks[0].task_id if tasks else None
    return {
        "open_tasks": len(tasks),
        "top_task": top,
        "high_priority": sum(1 for task in tasks if task.priority_score >= 75),
        "by_type": dict(by_type),
        "by_action": dict(by_action),
        "by_source": dict(by_source),
        "new_tasks": int(persisted.get("new_tasks", 0) or 0),
        "unchanged_tasks": int(persisted.get("unchanged_tasks", 0) or 0),
        "can_trade": False,
        "can_promote": False,
    }


def _memory_manifest(tasks: tuple[ProofTask, ...]) -> dict[str, Any]:
    return {
        "store_dir": None,
        "manifest_path": None,
        "stored_tasks": len(tasks),
        "known_tasks": len(tasks),
        "new_tasks": len(tasks),
        "updated_tasks": 0,
        "unchanged_tasks": 0,
    }


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _read_manifest(path: Path) -> dict:
    manifest = _read_json(path)
    if not isinstance(manifest.get("tasks"), dict):
        manifest["tasks"] = {}
    return manifest


def _write_json_atomic(payload: Mapping[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    tmp.replace(path)


def _stable_digest(value: Any, *, length: int) -> str:
    raw = json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:length]


def _slug(value: str) -> str:
    out = []
    for char in value.lower():
        out.append(char if char.isalnum() else "_")
    return "_".join(part for part in "".join(out).split("_") if part)


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the VNEDGE Alpha Workbench")
    parser.add_argument("--research-dir", default=str(DEFAULT_RESEARCH_DIR))
    parser.add_argument("--store-dir", default=str(DEFAULT_STORE_DIR))
    parser.add_argument("--out", default=str(DEFAULT_LATEST))
    parser.add_argument("--feed", default=str(DEFAULT_FEED))
    parser.add_argument("--max-tasks", type=int, default=50)
    parser.add_argument("--interval-seconds", type=int, default=900)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-persist", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    while True:
        payload = run_alpha_workbench(
            args.research_dir,
            store_dir=None if args.no_persist else args.store_dir,
            max_tasks=args.max_tasks,
        )
        publish_alpha_workbench(payload, Path(args.out), Path(args.feed) if args.feed else None)
        if args.json:
            print(json.dumps(payload, indent=2, default=str))
        else:
            summary = payload["summary"]
            print(
                f"{payload['generated_at']} {WORKBENCH_ID}: "
                f"{summary['open_tasks']} tasks, top={summary['top_task']}"
            )
        if args.once:
            return 0
        time.sleep(max(args.interval_seconds, 1))


if __name__ == "__main__":
    raise SystemExit(main())
