"""Persistent hypothesis lifecycle memory for VNEDGE research.

This is the VNEDGE-safe adaptation of "vibe" style trading agents: memory,
agent debate, and lifecycle management without granting agents any trading
authority. It sits above alpha_council and alpha_workbench, turning debates and
proof tasks into durable hypothesis cards with active/monitoring/decayed/
disabled states.
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
from typing import Any, Iterable, Literal, Mapping

from vnedge.research.alpha_council import run_alpha_council
from vnedge.research.alpha_workbench import run_alpha_workbench

VIBE_INTELLIGENCE_ID = "vibe_intelligence_v1"
DEFAULT_RESEARCH_DIR = Path("research/live_research")
DEFAULT_STORE_DIR = DEFAULT_RESEARCH_DIR / "vibe_intelligence"
DEFAULT_LATEST = DEFAULT_RESEARCH_DIR / "vibe_intelligence_latest.json"
DEFAULT_FEED = DEFAULT_RESEARCH_DIR / "vibe_intelligence_feed.jsonl"

LifecycleState = Literal["INCUBATING", "ACTIVE", "MONITORING", "DECAYED", "DISABLED"]

ACTIVE_ACTIONS = {
    "RUN_CONSERVATIVE_L2_REPLAY",
    "RUN_FILTERED_REPLAY_FROM_EXECUTION_CONDITIONS",
    "PRE_REGISTER_UNTOUCHED_JUDGMENT",
    "PRE_REGISTER_NEAR_PASS_JUDGMENT",
    "RECORD_MORE_TICKS",
    "REPAIR_EXIT_PAYOFF",
    "CHECK_ZERO_WINDOW_STABILITY",
    "SPLIT_REPLAY_BY_BTC_REGIME",
}
MONITORING_ACTIONS = {"QUEUE_SHADOW_TRIAL_AFTER_REPLAY"}
INCUBATING_ACTIONS = {
    "REFRESH_STALE_ARTIFACT",
    "REFRESH_BITCOIN_NODE_HEALTH",
    "DIAGNOSE_CLOSE_REJECT",
}
DECAY_ACTIONS = {"MINE_PRE_EVENT_EXECUTION_CONDITIONS"}
DECAY_VETOES = {
    "execution_replay_failed",
    "maker_fill_failed",
    "replay_negative_edge",
    "negative_edge_after_cost",
    "fee_wall_failed",
    "adverse_selection_failed",
    "candidate_replay_failure",
    "shadow_trial_negative",
    "paper_trial_negative",
    "insufficient_edge_after_cost",
}
PROOF_VETOES = {
    "requires_conservative_l2_replay",
    "requires_shadow_trial_after_replay",
    "requires_untouched_judgment",
    "maker_fill_unproven",
    "needs_more_samples",
}


@dataclass(frozen=True)
class HypothesisCard:
    hypothesis_id: str
    candidate_id: str
    source: str
    family: str
    exchange: str
    symbol: str
    timeframe: str
    lifecycle_state: LifecycleState
    priority_score: float
    health_score: float
    confidence_score: float
    decay_score: float
    latest_verdict: str
    next_action: str
    proof_step: str
    workbench_task_id: str | None
    task_type: str | None
    blocked_by: tuple[str, ...]
    vetoes: tuple[str, ...]
    evidence_digest: str
    evidence_count: int
    times_seen: int
    previous_lifecycle_state: str | None
    rationale: str
    candidate: dict[str, Any]
    can_trade: bool = False
    can_promote: bool = False
    live_orders_enabled: bool = False
    requires_human_approval: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_vibe_intelligence(
    research_dir: Path | str = DEFAULT_RESEARCH_DIR,
    *,
    store_dir: Path | str | None = DEFAULT_STORE_DIR,
    max_cards: int = 50,
    council_payload: dict[str, Any] | None = None,
    workbench_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build and optionally persist the current hypothesis lifecycle board."""
    research = Path(research_dir)
    council = council_payload or _load_council_or_run(research, max_cards=max_cards)
    workbench = workbench_payload or run_alpha_workbench(
        research,
        store_dir=None,
        max_tasks=max_cards,
        council_payload=council,
    )
    previous = _read_previous_records(Path(store_dir) if store_dir is not None else None)
    cards = build_hypothesis_cards(
        council,
        workbench,
        previous_records=previous,
        max_cards=max_cards,
    )
    persisted = (
        persist_hypothesis_cards(cards, Path(store_dir))
        if store_dir is not None else _memory_manifest(cards)
    )
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "intelligence_id": VIBE_INTELLIGENCE_ID,
        "mode": "research_only_hypothesis_lifecycle_memory",
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
        "policy": vibe_intelligence_policy(),
        "source": {
            "council_id": council.get("council_id"),
            "council_generated_at": council.get("generated_at"),
            "workbench_id": workbench.get("workbench_id"),
            "workbench_generated_at": workbench.get("generated_at"),
            "research_dir": str(research),
        },
        "summary": _summary(cards, persisted),
        "persistence": persisted,
        "cards": [card.to_dict() for card in cards],
    }
    return payload


def vibe_intelligence_policy() -> dict[str, Any]:
    return {
        "status": "research_only",
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
        "orders_allowed": False,
        "auto_promotion_allowed": False,
        "agent_generated_code_allowed": False,
        "principle": (
            "hypothesis memory can rank, decay, and disable research ideas, but "
            "cannot create orders or promote lanes; replay, untouched judgment, "
            "human approval, shadow, paper, and the risk gateway remain mandatory"
        ),
        "lifecycle": ["INCUBATING", "ACTIVE", "MONITORING", "DECAYED", "DISABLED"],
    }


def build_hypothesis_cards(
    council_payload: Mapping[str, Any],
    workbench_payload: Mapping[str, Any],
    *,
    previous_records: Mapping[str, Mapping[str, Any]] | None = None,
    max_cards: int = 50,
) -> tuple[HypothesisCard, ...]:
    """Join alpha-council debates with workbench tasks into lifecycle cards."""
    previous = previous_records or {}
    tasks = _tasks_by_candidate(workbench_payload)
    cards: list[HypothesisCard] = []
    debates = council_payload.get("debates", [])
    if not isinstance(debates, list):
        return ()
    for row in debates:
        if not isinstance(row, Mapping):
            continue
        candidate = row.get("candidate")
        if not isinstance(candidate, Mapping):
            continue
        card = _card_from_debate(row, candidate, tasks, previous)
        cards.append(card)
    cards.sort(key=_card_sort_key)
    return tuple(cards[:max_cards])


def persist_hypothesis_cards(cards: Iterable[HypothesisCard], store_dir: Path) -> dict[str, Any]:
    """Checkpoint each hypothesis as a durable chunk and update manifest memory."""
    cards = tuple(cards)
    now = datetime.now(UTC).isoformat()
    store_dir.mkdir(parents=True, exist_ok=True)
    chunks_dir = store_dir / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = store_dir / "manifest.json"
    manifest = _read_manifest(manifest_path)
    records = manifest.setdefault("hypotheses", {})
    new_count = 0
    updated_count = 0
    unchanged_count = 0

    for card in cards:
        payload = card.to_dict()
        content_hash = _stable_digest(_card_content_payload(card), length=32)
        record = records.get(card.hypothesis_id)
        chunk_name = f"{_slug(card.family)}__{_stable_digest(card.hypothesis_id, length=16)}.json"
        chunk_path = chunks_dir / chunk_name
        if not isinstance(record, dict):
            first_seen = now
            times_seen = 0
            previous_hash = None
            decay_observations = 0
            monitoring_observations = 0
            new_count += 1
        else:
            first_seen = str(record.get("first_seen_at") or now)
            times_seen = int(record.get("times_seen", 0) or 0)
            previous_hash = record.get("content_hash")
            decay_observations = int(record.get("decay_observations", 0) or 0)
            monitoring_observations = int(record.get("monitoring_observations", 0) or 0)

        if previous_hash == content_hash and chunk_path.exists():
            unchanged_count += 1
        else:
            if record is not None:
                updated_count += 1
            _write_json_atomic(
                {
                    "intelligence_id": VIBE_INTELLIGENCE_ID,
                    "card": payload,
                    "content_hash": content_hash,
                    "written_at": now,
                },
                chunk_path,
            )

        if card.lifecycle_state in {"DECAYED", "DISABLED"}:
            decay_observations += 1
        if card.lifecycle_state == "MONITORING":
            monitoring_observations += 1

        records[card.hypothesis_id] = {
            "hypothesis_id": card.hypothesis_id,
            "candidate_id": card.candidate_id,
            "source": card.source,
            "family": card.family,
            "exchange": card.exchange,
            "symbol": card.symbol,
            "timeframe": card.timeframe,
            "lifecycle_state": card.lifecycle_state,
            "next_action": card.next_action,
            "content_hash": content_hash,
            "chunk": str(chunk_path.relative_to(store_dir)),
            "health_score": card.health_score,
            "priority_score": card.priority_score,
            "first_seen_at": first_seen,
            "last_seen_at": now,
            "times_seen": times_seen + 1,
            "decay_observations": decay_observations,
            "monitoring_observations": monitoring_observations,
            "can_trade": False,
            "can_promote": False,
        }

    manifest.update({
        "intelligence_id": VIBE_INTELLIGENCE_ID,
        "updated_at": now,
        "total_hypotheses_seen": len(records),
    })
    _write_json_atomic(manifest, manifest_path)
    return {
        "store_dir": str(store_dir),
        "manifest_path": str(manifest_path),
        "stored_cards": len(cards),
        "known_hypotheses": len(records),
        "new_hypotheses": new_count,
        "updated_hypotheses": updated_count,
        "unchanged_hypotheses": unchanged_count,
    }


def publish_vibe_intelligence(
    payload: Mapping[str, Any],
    out: Path,
    feed: Path | None = None,
) -> None:
    _write_json_atomic(payload, out)
    if feed is not None:
        feed.parent.mkdir(parents=True, exist_ok=True)
        with open(feed, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, default=str) + "\n")


def _card_from_debate(
    row: Mapping[str, Any],
    candidate: Mapping[str, Any],
    tasks: Mapping[str, Mapping[str, Any]],
    previous: Mapping[str, Mapping[str, Any]],
) -> HypothesisCard:
    candidate_id = str(candidate.get("candidate_id") or "unknown_candidate")
    task = tasks.get(candidate_id, {})
    vetoes = tuple(str(v) for v in row.get("vetoes", []) if isinstance(v, str))
    blocked_by = _blocked_by(task, vetoes)
    next_action = str(row.get("next_action") or task.get("next_action") or "HOLD_RESEARCH_ONLY")
    priority = _float(row.get("priority_score"))
    latest_verdict = str(row.get("council_verdict") or "UNKNOWN")
    evidence = candidate.get("evidence", {})
    hypothesis_id = _hypothesis_id(candidate)
    previous_record = previous.get(hypothesis_id, {})
    previous_state = (
        str(previous_record.get("lifecycle_state"))
        if isinstance(previous_record, Mapping) and previous_record.get("lifecycle_state")
        else None
    )
    previous_seen = (
        int(previous_record.get("times_seen", 0) or 0)
        if isinstance(previous_record, Mapping) else 0
    )
    previous_decay = (
        int(previous_record.get("decay_observations", 0) or 0)
        if isinstance(previous_record, Mapping) else 0
    )
    decay_score = _decay_score(next_action, latest_verdict, vetoes, blocked_by)
    confidence_score = _confidence_score(priority, latest_verdict, evidence, vetoes)
    lifecycle = _lifecycle_state(
        next_action,
        latest_verdict,
        priority,
        decay_score,
        previous_decay,
    )
    health_score = _health_score(priority, confidence_score, decay_score, lifecycle, blocked_by)
    return HypothesisCard(
        hypothesis_id=hypothesis_id,
        candidate_id=candidate_id,
        source=str(candidate.get("source", "unknown")),
        family=str(candidate.get("family", "unknown")),
        exchange=str(candidate.get("exchange", "unknown")),
        symbol=str(candidate.get("symbol", "unknown")),
        timeframe=str(candidate.get("timeframe", "unknown")),
        lifecycle_state=lifecycle,
        priority_score=round(priority, 2),
        health_score=round(health_score, 2),
        confidence_score=round(confidence_score, 2),
        decay_score=round(decay_score, 2),
        latest_verdict=latest_verdict,
        next_action=next_action,
        proof_step=str(task.get("proof_step") or _fallback_proof_step(next_action)),
        workbench_task_id=str(task.get("task_id")) if task.get("task_id") else None,
        task_type=str(task.get("task_type")) if task.get("task_type") else None,
        blocked_by=blocked_by,
        vetoes=vetoes,
        evidence_digest=_stable_digest(evidence, length=16),
        evidence_count=_evidence_count(evidence),
        times_seen=previous_seen + 1,
        previous_lifecycle_state=previous_state,
        rationale=_rationale(row),
        candidate=dict(candidate),
    )


def _tasks_by_candidate(workbench_payload: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    out: dict[str, Mapping[str, Any]] = {}
    tasks = workbench_payload.get("tasks", [])
    if not isinstance(tasks, list):
        return out
    for task in tasks:
        if not isinstance(task, Mapping):
            continue
        candidate_id = str(task.get("candidate_id") or "")
        if not candidate_id:
            continue
        current = out.get(candidate_id)
        if current is None or _float(task.get("priority_score")) > _float(current.get("priority_score")):
            out[candidate_id] = task
    return out


def _hypothesis_id(candidate: Mapping[str, Any]) -> str:
    evidence = candidate.get("evidence", {})
    if isinstance(evidence, Mapping):
        direct = evidence.get("hypothesis_id")
        if direct:
            return str(direct)
    key = {
        "source": candidate.get("source"),
        "family": candidate.get("family"),
        "exchange": candidate.get("exchange"),
        "symbol": candidate.get("symbol"),
        "timeframe": candidate.get("timeframe"),
        "candidate_id": candidate.get("candidate_id"),
    }
    return f"hyp|{_slug(str(candidate.get('source', 'unknown')))}|{_stable_digest(key, length=20)}"


def _blocked_by(
    task: Mapping[str, Any],
    vetoes: tuple[str, ...],
) -> tuple[str, ...]:
    blocked = task.get("blocked_by", ())
    if not isinstance(blocked, (list, tuple)):
        blocked = ()
    return tuple(dict.fromkeys((*vetoes, *(str(item) for item in blocked))))


def _decay_score(
    next_action: str,
    latest_verdict: str,
    vetoes: tuple[str, ...],
    blocked_by: tuple[str, ...],
) -> float:
    evidence = set(vetoes) | set(blocked_by)
    score = 0.0
    score += 18.0 * len(evidence & DECAY_VETOES)
    if next_action in DECAY_ACTIONS:
        score += 25.0
    if "FAILED" in latest_verdict or "NEGATIVE" in latest_verdict:
        score += 18.0
    if "REJECT" in latest_verdict:
        score += 8.0
    return _clamp(score, 0.0, 100.0)


def _confidence_score(
    priority: float,
    latest_verdict: str,
    evidence: Any,
    vetoes: tuple[str, ...],
) -> float:
    score = priority
    if "HIGH_PRIORITY" in latest_verdict or "PASS" in latest_verdict:
        score += 12.0
    if isinstance(evidence, Mapping):
        samples = _float(
            evidence.get("samples")
            or evidence.get("fill_count")
            or evidence.get("trades")
            or evidence.get("oos_trades")
        )
        if samples >= 50:
            score += 12.0
        elif samples >= 15:
            score += 7.0
        elif samples > 0:
            score += 3.0
    proof_debt = len(set(vetoes) & PROOF_VETOES)
    score -= 5.0 * proof_debt
    return _clamp(score, 0.0, 100.0)


def _lifecycle_state(
    next_action: str,
    latest_verdict: str,
    priority: float,
    decay_score: float,
    previous_decay: int,
) -> LifecycleState:
    if previous_decay >= 2 and decay_score >= 35.0:
        return "DISABLED"
    if next_action in MONITORING_ACTIONS:
        return "MONITORING"
    if decay_score >= 45.0 or "NEGATIVE" in latest_verdict or "FAILED" in latest_verdict:
        return "DECAYED"
    if next_action in ACTIVE_ACTIONS:
        return "ACTIVE"
    if next_action in INCUBATING_ACTIONS:
        return "INCUBATING"
    if priority < 35.0:
        return "DISABLED"
    return "INCUBATING"


def _health_score(
    priority: float,
    confidence_score: float,
    decay_score: float,
    lifecycle: LifecycleState,
    blocked_by: tuple[str, ...],
) -> float:
    score = (priority * 0.5) + (confidence_score * 0.5) - (decay_score * 0.35)
    score -= min(len(blocked_by), 8) * 2.0
    if lifecycle == "MONITORING":
        score += 10.0
    elif lifecycle == "DECAYED":
        score -= 10.0
    elif lifecycle == "DISABLED":
        score = min(score, 15.0)
    return _clamp(score, 0.0, 100.0)


def _fallback_proof_step(next_action: str) -> str:
    if next_action in ACTIVE_ACTIONS:
        return "complete the governed proof step before any shadow or paper discussion"
    if next_action in MONITORING_ACTIONS:
        return "observe governed shadow evidence and paper readiness; no live authority"
    if next_action in DECAY_ACTIONS:
        return "mine why the hypothesis decayed without promoting from seen data"
    return "keep as research-only context until a falsifiable proof step exists"


def _rationale(row: Mapping[str, Any]) -> str:
    debate = row.get("debate")
    if isinstance(debate, list):
        for opinion in debate:
            if isinstance(opinion, Mapping) and opinion.get("agent_id") == "research_director":
                argument = str(opinion.get("argument") or "").strip()
                if argument:
                    return argument
    return str(row.get("next_action") or "research-only hypothesis")


def _evidence_count(evidence: Any) -> int:
    if not isinstance(evidence, Mapping):
        return 0
    for key in ("samples", "fill_count", "trades", "oos_trades", "count"):
        if key in evidence:
            return int(max(_float(evidence.get(key)), 0.0))
    return len(evidence)


def _summary(
    cards: tuple[HypothesisCard, ...],
    persisted: Mapping[str, Any],
) -> dict[str, Any]:
    by_lifecycle = Counter(card.lifecycle_state for card in cards)
    by_source = Counter(card.source for card in cards)
    by_action = Counter(card.next_action for card in cards)
    top_active = next((card.hypothesis_id for card in cards if card.lifecycle_state == "ACTIVE"), None)
    top_monitoring = next(
        (card.hypothesis_id for card in cards if card.lifecycle_state == "MONITORING"),
        None,
    )
    return {
        "hypotheses": len(cards),
        "active": by_lifecycle.get("ACTIVE", 0),
        "monitoring": by_lifecycle.get("MONITORING", 0),
        "decayed": by_lifecycle.get("DECAYED", 0),
        "disabled": by_lifecycle.get("DISABLED", 0),
        "incubating": by_lifecycle.get("INCUBATING", 0),
        "by_lifecycle": dict(by_lifecycle),
        "by_source": dict(by_source),
        "by_action": dict(by_action),
        "top_active": top_active,
        "top_monitoring": top_monitoring,
        "new_hypotheses": int(persisted.get("new_hypotheses", 0) or 0),
        "unchanged_hypotheses": int(persisted.get("unchanged_hypotheses", 0) or 0),
        "can_trade": False,
        "can_promote": False,
    }


def _memory_manifest(cards: tuple[HypothesisCard, ...]) -> dict[str, Any]:
    return {
        "store_dir": None,
        "manifest_path": None,
        "stored_cards": len(cards),
        "known_hypotheses": len(cards),
        "new_hypotheses": len(cards),
        "updated_hypotheses": 0,
        "unchanged_hypotheses": 0,
    }


def _card_content_payload(card: HypothesisCard) -> dict[str, Any]:
    payload = card.to_dict()
    payload.pop("times_seen", None)
    payload.pop("previous_lifecycle_state", None)
    return payload


def _read_previous_records(store_dir: Path | None) -> dict[str, Mapping[str, Any]]:
    if store_dir is None:
        return {}
    manifest = _read_manifest(store_dir / "manifest.json")
    records = manifest.get("hypotheses", {})
    return records if isinstance(records, dict) else {}


def _load_council_or_run(research_dir: Path, *, max_cards: int) -> dict[str, Any]:
    latest = research_dir / "alpha_council_latest.json"
    payload = _read_json(latest)
    if payload:
        return payload
    return run_alpha_council(research_dir, max_candidates=max_cards)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_manifest(path: Path) -> dict[str, Any]:
    manifest = _read_json(path)
    if not isinstance(manifest.get("hypotheses"), dict):
        manifest["hypotheses"] = {}
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


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _card_sort_key(card: HypothesisCard) -> tuple[int, float, float, str]:
    order = {
        "MONITORING": 0,
        "ACTIVE": 1,
        "INCUBATING": 2,
        "DECAYED": 3,
        "DISABLED": 4,
    }
    return (
        order.get(card.lifecycle_state, 9),
        -card.health_score,
        -card.priority_score,
        card.hypothesis_id,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run VNEDGE Vibe Intelligence")
    parser.add_argument("--research-dir", default=str(DEFAULT_RESEARCH_DIR))
    parser.add_argument("--store-dir", default=str(DEFAULT_STORE_DIR))
    parser.add_argument("--out", default=str(DEFAULT_LATEST))
    parser.add_argument("--feed", default=str(DEFAULT_FEED))
    parser.add_argument("--max-cards", type=int, default=50)
    parser.add_argument("--interval-seconds", type=int, default=900)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-persist", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    while True:
        payload = run_vibe_intelligence(
            args.research_dir,
            store_dir=None if args.no_persist else args.store_dir,
            max_cards=args.max_cards,
        )
        publish_vibe_intelligence(payload, Path(args.out), Path(args.feed) if args.feed else None)
        if args.json:
            print(json.dumps(payload, indent=2, default=str))
        else:
            summary = payload["summary"]
            print(
                f"{payload['generated_at']} {VIBE_INTELLIGENCE_ID}: "
                f"{summary['hypotheses']} hypotheses, "
                f"active={summary['active']}, monitoring={summary['monitoring']}, "
                f"decayed={summary['decayed']}"
            )
        if args.once:
            return 0
        time.sleep(max(args.interval_seconds, 1))


if __name__ == "__main__":
    raise SystemExit(main())
