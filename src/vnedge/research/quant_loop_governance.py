"""Research-loop governance for VNEDGE AI Quant OS.

The agent loops are useful only when their state is legible: which evidence is
fresh, which candidates share a lock, which budgets are close to exhaustion,
and whether the proof chain is still research-only. This module publishes that
control-plane view. It never creates orders, paper lanes, or promotions.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import yaml

from vnedge.governance.promotion_policy import DEFAULT_PROMOTION_POLICY

GOVERNANCE_ID = "quant_loop_governance_v1"
DEFAULT_GATES = Path("governance/loop_gates.yaml")
DEFAULT_STATE = Path("research/quant_loop_state.json")
DEFAULT_ALPHA_ARENA = Path("research/live_research/alpha_arena_lite_latest.json")
DEFAULT_SCANNER_UPLIFT = Path("research/live_research/scanner_backtest_uplift_latest.json")
DEFAULT_SCANNER_PROGRESS = Path("research/live_research/scanner_tournament_progress.json")
DEFAULT_GATEWAY_SNAPSHOT = Path("logs/agent_gateway/quant_os/snapshot.json")
DEFAULT_OUT = Path("research/live_research/quant_loop_governance_latest.json")
DEFAULT_FEED = Path("research/live_research/quant_loop_governance_feed.jsonl")
DEFAULT_RUN_LOG = Path("research/live_research/quant_loop_run_log.jsonl")


@dataclass(frozen=True)
class QuantLoopGovernanceConfig:
    max_alpha_age_minutes: int = 360
    max_scanner_uplift_age_minutes: int = 360
    max_progress_age_minutes: int = 90
    max_gateway_age_minutes: int = 360

    def __post_init__(self) -> None:
        for name, value in (
            ("max_alpha_age_minutes", self.max_alpha_age_minutes),
            ("max_scanner_uplift_age_minutes", self.max_scanner_uplift_age_minutes),
            ("max_progress_age_minutes", self.max_progress_age_minutes),
            ("max_gateway_age_minutes", self.max_gateway_age_minutes),
        ):
            if value < 1:
                raise ValueError(f"{name} must be >= 1")


def run_quant_loop_audit(
    *,
    gates_path: Path | str = DEFAULT_GATES,
    state_path: Path | str = DEFAULT_STATE,
    alpha_arena_path: Path | str = DEFAULT_ALPHA_ARENA,
    scanner_uplift_path: Path | str = DEFAULT_SCANNER_UPLIFT,
    scanner_progress_path: Path | str = DEFAULT_SCANNER_PROGRESS,
    gateway_snapshot_path: Path | str = DEFAULT_GATEWAY_SNAPSHOT,
    run_log_path: Path | str = DEFAULT_RUN_LOG,
    config: QuantLoopGovernanceConfig | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    config = config or QuantLoopGovernanceConfig()
    generated = now or datetime.now(UTC)
    gates_artifact = _read_yaml_artifact(Path(gates_path))
    state_artifact = _read_json_artifact(Path(state_path))
    alpha_artifact = _read_json_artifact(Path(alpha_arena_path))
    scanner_artifact = _read_json_artifact(Path(scanner_uplift_path))
    progress_artifact = _read_json_artifact(Path(scanner_progress_path))
    gateway_artifact = _read_json_artifact(Path(gateway_snapshot_path))

    gates = gates_artifact.payload
    loop_cards = _loop_cards(
        gates=gates,
        state=state_artifact,
        alpha=alpha_artifact,
        scanner=scanner_artifact,
        progress=progress_artifact,
        gateway=gateway_artifact,
        config=config,
        now=generated,
    )
    candidate_locks = _candidate_locks(alpha_artifact.payload)
    collisions = _collisions(candidate_locks)
    budget_alerts = _budget_alerts(
        run_log_path=Path(run_log_path),
        gates=gates,
        now=generated,
    )
    gate_checks = _gate_checks(
        gates_artifact=gates_artifact,
        gates=gates,
        collisions=collisions,
        budget_alerts=budget_alerts,
    )
    score = _readiness_score(
        loop_cards=loop_cards,
        gate_checks=gate_checks,
        collisions=collisions,
        budget_alerts=budget_alerts,
    )
    level = _readiness_level(score)
    summary = {
        "readiness_score": score,
        "readiness_level": level,
        "loops_total": len(loop_cards),
        "loops_ok": sum(1 for row in loop_cards if row["status"] == "OK"),
        "loops_waiting": sum(1 for row in loop_cards if row["status"] == "WAITING"),
        "loops_stale": sum(1 for row in loop_cards if row["status"] == "STALE"),
        "loops_missing": sum(1 for row in loop_cards if row["status"] == "MISSING"),
        "loops_blocked": sum(1 for row in loop_cards if row["status"] == "BLOCKED"),
        "candidate_locks": len(candidate_locks),
        "collisions": len(collisions),
        "budget_alerts": len(budget_alerts),
        "gate_blocks": sum(1 for row in gate_checks if row["status"] == "BLOCKED"),
        "alpha_candidates": _int((alpha_artifact.payload.get("summary") or {}).get("candidate_count")),
        "alpha_sample_valid": _int((alpha_artifact.payload.get("summary") or {}).get("sample_valid")),
        "alpha_judgment_ready": _int(
            (alpha_artifact.payload.get("summary") or {}).get("ready_for_untouched_judgment")
        ),
        "scanner_evidence_rows": _int(
            (scanner_artifact.payload.get("summary") or {}).get("evidence_rows")
        ),
        "progress_status": str(progress_artifact.payload.get("status") or "unknown"),
        "research_only": True,
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
    }
    payload = {
        "governance_id": GOVERNANCE_ID,
        "generated_at": generated.isoformat(),
        "summary": summary,
        "gate_checks": gate_checks,
        "loop_cards": loop_cards,
        "candidate_locks": candidate_locks,
        "collisions": collisions,
        "budget_alerts": budget_alerts,
        "run_log_record": _run_log_record(generated=generated, summary=summary),
        "policy": _policy(gates),
        "operator_answer": _operator_answer(summary, collisions, budget_alerts, loop_cards),
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
    }
    return payload


def publish_quant_loop_audit(
    payload: dict[str, Any],
    *,
    out: Path | str = DEFAULT_OUT,
    feed: Path | str | None = DEFAULT_FEED,
    run_log: Path | str | None = DEFAULT_RUN_LOG,
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
    ) as handle:
        handle.write(encoded)
        tmp_path = Path(handle.name)
    tmp_path.chmod(0o644)
    tmp_path.replace(out_path)
    out_path.chmod(0o644)

    if feed is not None:
        _append_jsonl(Path(feed), _feed_record(payload))
    if run_log is not None:
        _append_jsonl(Path(run_log), payload["run_log_record"])
    return out_path


@dataclass(frozen=True)
class _Artifact:
    path: Path
    payload: dict[str, Any]
    exists: bool
    error: str | None = None


def _read_json_artifact(path: Path) -> _Artifact:
    if not path.exists():
        return _Artifact(path=path, payload={}, exists=False)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _Artifact(path=path, payload={}, exists=True, error=str(exc))
    return _Artifact(path=path, payload=payload if isinstance(payload, dict) else {}, exists=True)


def _read_yaml_artifact(path: Path) -> _Artifact:
    if not path.exists():
        return _Artifact(path=path, payload={}, exists=False)
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        return _Artifact(path=path, payload={}, exists=True, error=str(exc))
    return _Artifact(path=path, payload=payload if isinstance(payload, dict) else {}, exists=True)


def _loop_cards(
    *,
    gates: dict[str, Any],
    state: _Artifact,
    alpha: _Artifact,
    scanner: _Artifact,
    progress: _Artifact,
    gateway: _Artifact,
    config: QuantLoopGovernanceConfig,
    now: datetime,
) -> list[dict[str, Any]]:
    return [
        _artifact_card(
            "machine_readable_gates",
            gates,
            _Artifact(Path("governance"), gates, bool(gates)),
            max_age_minutes=None,
            now=now,
            ok_when=bool(gates),
            action="MAINTAIN_GOVERNANCE_GATES",
        ),
        _artifact_card(
            "quant_loop_state",
            gates,
            state,
            max_age_minutes=None,
            now=now,
            ok_when=state.exists and not state.error,
            action="KEEP_STATE_SEED_VERSIONED",
        ),
        _artifact_card(
            "alpha_arena_lite",
            gates,
            alpha,
            max_age_minutes=config.max_alpha_age_minutes,
            now=now,
            ok_when=alpha.exists and not alpha.error and bool(alpha.payload.get("scorecards")),
            action="EXPAND_SAMPLES_OR_PRE_REGISTER_JUDGMENT",
        ),
        _artifact_card(
            "scanner_backtest_uplift",
            gates,
            scanner,
            max_age_minutes=config.max_scanner_uplift_age_minutes,
            now=now,
            ok_when=scanner.exists
            and not scanner.error
            and _int((scanner.payload.get("summary") or {}).get("evidence_rows")) > 0,
            action="MINE_FAILED_AND_NEAR_MISS_ROWS",
        ),
        _artifact_card(
            "scanner_tournament_progress",
            gates,
            progress,
            max_age_minutes=config.max_progress_age_minutes,
            now=now,
            ok_when=progress.exists
            and not progress.error
            and str(progress.payload.get("status") or "").lower() in {"running", "completed"},
            action="KEEP_BACKTEST_PROGRESS_HEARTBEAT_FRESH",
        ),
        _artifact_card(
            "quant_os_agent_gateway",
            gates,
            gateway,
            max_age_minutes=config.max_gateway_age_minutes,
            now=now,
            ok_when=gateway.exists and not gateway.error,
            action="SYNC_AGENT_TASKS_AND_ARTIFACTS",
        ),
    ]


def _artifact_card(
    loop_id: str,
    gates: dict[str, Any],
    artifact: _Artifact,
    *,
    max_age_minutes: int | None,
    now: datetime,
    ok_when: bool,
    action: str,
) -> dict[str, Any]:
    generated = _artifact_time(artifact.payload, artifact.path)
    age_minutes = _age_minutes(generated, now)
    stale = max_age_minutes is not None and age_minutes is not None and age_minutes > max_age_minutes
    if artifact.error:
        status = "BLOCKED"
        reason = artifact.error
    elif not artifact.exists:
        status = "MISSING"
        reason = "artifact missing"
    elif stale:
        status = "STALE"
        reason = f"artifact age {age_minutes:.1f}m exceeds {max_age_minutes}m"
    elif ok_when:
        status = "OK"
        reason = "artifact is usable"
    else:
        status = "WAITING"
        reason = "artifact present but not enough evidence yet"
    budget = (gates.get("loop_budgets") or {}).get(loop_id) or {}
    return {
        "loop_id": loop_id,
        "status": status,
        "reason": reason,
        "path": str(artifact.path),
        "generated_at": generated.isoformat() if generated is not None else None,
        "age_minutes": round(age_minutes, 2) if age_minutes is not None else None,
        "max_age_minutes": max_age_minutes,
        "budget": budget,
        "action": action,
        "can_trade": False,
        "can_promote": False,
    }


def _candidate_locks(alpha_payload: dict[str, Any]) -> list[dict[str, Any]]:
    locks: list[dict[str, Any]] = []
    for card in alpha_payload.get("scorecards") or []:
        if not isinstance(card, dict):
            continue
        timeframes = tuple(str(item) for item in (card.get("timeframes") or [])) or ("unknown",)
        for timeframe in timeframes:
            lock = {
                "candidate_id": str(card.get("candidate_id") or ""),
                "strategy_id": str(card.get("strategy_id") or "unknown"),
                "exchange": str(card.get("exchange") or "unknown"),
                "symbol": str(card.get("symbol") or "unknown"),
                "timeframe": timeframe,
                "data_window": str(
                    ((card.get("untouched_window_plan") or {}).get("status"))
                    or "NEXT_UNTOUCHED_EXTENSION_REQUIRED"
                ),
            }
            lock["lock_key"] = "|".join(
                str(lock[key])
                for key in ("strategy_id", "exchange", "symbol", "timeframe", "data_window")
            )
            locks.append(lock)
    return locks


def _collisions(locks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[str, list[dict[str, Any]]] = {}
    for lock in locks:
        by_key.setdefault(str(lock["lock_key"]), []).append(lock)
    collisions: list[dict[str, Any]] = []
    for key, rows in sorted(by_key.items()):
        candidates = sorted({str(row.get("candidate_id") or "") for row in rows if row.get("candidate_id")})
        if len(rows) > 1 and len(candidates) > 1:
            collisions.append(
                {
                    "lock_key": key,
                    "candidate_count": len(candidates),
                    "candidate_ids": candidates,
                    "action": "SERIALIZE_OR_MERGE_DUPLICATE_AGENT_TASKS",
                }
            )
    return collisions


def _budget_alerts(
    *,
    run_log_path: Path,
    gates: dict[str, Any],
    now: datetime,
) -> list[dict[str, Any]]:
    budgets = gates.get("loop_budgets") if isinstance(gates.get("loop_budgets"), dict) else {}
    current_date = now.date().isoformat()
    counts = Counter[str]()
    if run_log_path.exists():
        for row in _iter_jsonl(run_log_path):
            generated = str(row.get("generated_at") or row.get("ts") or "")
            if generated.startswith(current_date):
                counts[str(row.get("pattern") or row.get("loop_id") or "unknown")] += 1
    alerts: list[dict[str, Any]] = []
    for loop_id, budget in budgets.items():
        if not isinstance(budget, dict):
            continue
        max_runs = _int(budget.get("max_runs_per_day"))
        if max_runs <= 0:
            continue
        used = counts[str(loop_id)]
        if used >= max_runs:
            alerts.append(
                {
                    "loop_id": str(loop_id),
                    "used_runs_today": used,
                    "max_runs_per_day": max_runs,
                    "action": "PAUSE_OR_RAISE_INTERVAL_BEFORE_NEXT_RUN",
                }
            )
    return alerts


def _gate_checks(
    *,
    gates_artifact: _Artifact,
    gates: dict[str, Any],
    collisions: list[dict[str, Any]],
    budget_alerts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    promotion = gates.get("promotion") if isinstance(gates.get("promotion"), dict) else {}
    denylist = gates.get("denylist_paths") if isinstance(gates.get("denylist_paths"), list) else []
    min_net = _float(promotion.get("min_net_bps")) or 0.0
    min_pf = _float(promotion.get("min_profit_factor")) or 0.0
    min_trades = _int(promotion.get("sample_min_trades"))
    policy_version = str(promotion.get("policy_version") or "")
    requirements = DEFAULT_PROMOTION_POLICY.governance
    checks = [
        {
            "gate_id": "machine_readable_gates",
            "status": "PASS" if gates_artifact.exists and not gates_artifact.error else "BLOCKED",
            "detail": str(gates_artifact.path),
        },
        {
            "gate_id": "research_only_scope",
            "status": "PASS",
            "detail": "governance output can_trade=false can_promote=false live_orders_enabled=false",
        },
        {
            "gate_id": "denylist_live_paths",
            "status": "PASS" if len(denylist) >= 3 else "WAITING",
            "detail": f"{len(denylist)} denylist patterns",
        },
        {
            "gate_id": "promotion_proof_thresholds",
            "status": (
                "PASS"
                if policy_version == DEFAULT_PROMOTION_POLICY.policy_version
                and min_net >= requirements.minimum_average_net_bps
                and min_pf >= requirements.minimum_profit_factor
                and min_trades >= requirements.minimum_trades
                else "BLOCKED"
            ),
            "detail": (
                f"policy={policy_version or 'missing'} "
                f"net>={promotion.get('min_net_bps')}bps "
                f"PF>={promotion.get('min_profit_factor')} "
                f"trades>={promotion.get('sample_min_trades')}"
            ),
        },
        {
            "gate_id": "verifier_before_paper",
            "status": "PASS" if promotion.get("verifier_required_before_paper") is True else "BLOCKED",
            "detail": "independent verifier required before promotion",
        },
        {
            "gate_id": "candidate_collision_control",
            "status": "PASS" if not collisions else "BLOCKED",
            "detail": f"{len(collisions)} duplicate locks",
        },
        {
            "gate_id": "loop_budget_control",
            "status": "PASS" if not budget_alerts else "BLOCKED",
            "detail": f"{len(budget_alerts)} budget alerts",
        },
    ]
    return checks


def _readiness_score(
    *,
    loop_cards: list[dict[str, Any]],
    gate_checks: list[dict[str, Any]],
    collisions: list[dict[str, Any]],
    budget_alerts: list[dict[str, Any]],
) -> int:
    score = 0
    status_points = {"OK": 12, "WAITING": 6, "STALE": 2, "MISSING": 0, "BLOCKED": 0}
    for card in loop_cards:
        score += status_points.get(str(card.get("status")), 0)
    score += sum(4 for check in gate_checks if check["status"] == "PASS")
    score -= 12 * len(collisions)
    score -= 10 * len(budget_alerts)
    return max(0, min(100, int(score)))


def _readiness_level(score: int) -> str:
    if score >= 80:
        return "L3_GOVERNED_RESEARCH_READY"
    if score >= 60:
        return "L2_LOOP_HEALTHY_WAITING_EVIDENCE"
    if score >= 35:
        return "L1_BOOTSTRAPPING"
    return "L0_BLOCKED"


def _policy(gates: dict[str, Any]) -> dict[str, Any]:
    promotion = gates.get("promotion") if isinstance(gates.get("promotion"), dict) else {}
    return {
        "research_only": True,
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
        "policy_version": promotion.get("policy_version"),
        "policy_sha256": DEFAULT_PROMOTION_POLICY.sha256,
        "min_net_bps": _float(promotion.get("min_net_bps")),
        "min_profit_factor": _float(promotion.get("min_profit_factor")),
        "sample_min_trades": _int(promotion.get("sample_min_trades")),
        "burn_registry_required": promotion.get("burn_registry_required") is True,
        "verifier_required_before_paper": promotion.get("verifier_required_before_paper") is True,
        "agent_role": (
            "Coordinate research loops, surface collisions/staleness/budgets, "
            "and require verifier + untouched judgment before any paper lane."
        ),
    }


def _operator_answer(
    summary: dict[str, Any],
    collisions: list[dict[str, Any]],
    budget_alerts: list[dict[str, Any]],
    loop_cards: list[dict[str, Any]],
) -> str:
    if collisions:
        return (
            f"Quant Loop Governance is blocked by {len(collisions)} candidate lock "
            "collision(s). Serialize those agent tasks before expanding samples."
        )
    if budget_alerts:
        return (
            f"Quant Loop Governance is blocked by {len(budget_alerts)} loop budget "
            "alert(s). Raise intervals or pause duplicate loops before continuing."
        )
    stale_or_missing = [
        row for row in loop_cards if row.get("status") in {"STALE", "MISSING", "BLOCKED"}
    ]
    if stale_or_missing:
        worst = stale_or_missing[0]
        return (
            f"Quant loops are not fully healthy: {worst['loop_id']} is "
            f"{worst['status']} ({worst['reason']}). Fix that artifact before trusting "
            "the arena queue."
        )
    if summary.get("alpha_judgment_ready"):
        return (
            "Governance is healthy and at least one arena candidate is ready for "
            "operator-approved untouched judgment. This is still not paper promotion."
        )
    if summary.get("alpha_candidates"):
        return (
            "Governance is healthy enough for research. Alpha Arena candidates exist, "
            "but they need sample expansion, verifier review, or untouched judgment "
            "before paper."
        )
    return "Governance is running, but no Alpha Arena candidates are available yet."


def _run_log_record(*, generated: datetime, summary: dict[str, Any]) -> dict[str, Any]:
    outcome = "blocked" if summary.get("gate_blocks") else "ok"
    if not summary.get("alpha_candidates"):
        outcome = "waiting"
    return {
        "run_id": f"{GOVERNANCE_ID}-{generated.strftime('%Y%m%dT%H%M%S')}",
        "pattern": "quant_loop_governance",
        "generated_at": generated.isoformat(),
        "outcome": outcome,
        "readiness_score": summary.get("readiness_score"),
        "readiness_level": summary.get("readiness_level"),
        "items_found": summary.get("alpha_candidates"),
        "gate_blocks": summary.get("gate_blocks"),
        "collisions": summary.get("collisions"),
        "budget_alerts": summary.get("budget_alerts"),
        "can_trade": False,
        "can_promote": False,
    }


def _feed_record(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "governance_id": payload.get("governance_id"),
        "generated_at": payload.get("generated_at"),
        "summary": payload.get("summary", {}),
        "operator_answer": payload.get("operator_answer"),
        "can_trade": False,
        "can_promote": False,
    }


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
    path.chmod(0o644)


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    yield row
    except OSError:
        return


def _artifact_time(payload: dict[str, Any], path: Path) -> datetime | None:
    for key in ("generated_at", "heartbeat_at", "completed_at", "started_at", "updated_at"):
        parsed = _parse_dt(payload.get(key))
        if parsed is not None:
            return parsed
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        return None


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _age_minutes(generated: datetime | None, now: datetime) -> float | None:
    if generated is None:
        return None
    return max(0.0, (now - generated).total_seconds() / 60.0)


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="publish research-only Quant Loop Governance readiness"
    )
    parser.add_argument("--gates", default=str(DEFAULT_GATES))
    parser.add_argument("--state", default=str(DEFAULT_STATE))
    parser.add_argument("--alpha-arena", default=str(DEFAULT_ALPHA_ARENA))
    parser.add_argument("--scanner-uplift", default=str(DEFAULT_SCANNER_UPLIFT))
    parser.add_argument("--scanner-progress", default=str(DEFAULT_SCANNER_PROGRESS))
    parser.add_argument("--gateway-snapshot", default=str(DEFAULT_GATEWAY_SNAPSHOT))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--feed", default=str(DEFAULT_FEED))
    parser.add_argument("--run-log", default=str(DEFAULT_RUN_LOG))
    parser.add_argument("--interval-seconds", type=int, default=0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    while True:
        payload = run_quant_loop_audit(
            gates_path=Path(args.gates),
            state_path=Path(args.state),
            alpha_arena_path=Path(args.alpha_arena),
            scanner_uplift_path=Path(args.scanner_uplift),
            scanner_progress_path=Path(args.scanner_progress),
            gateway_snapshot_path=Path(args.gateway_snapshot),
            run_log_path=Path(args.run_log),
        )
        path = publish_quant_loop_audit(
            payload,
            out=Path(args.out),
            feed=None if args.feed == "" else Path(args.feed),
            run_log=None if args.run_log == "" else Path(args.run_log),
        )
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True), flush=True)
        else:
            print(f"quant loop governance wrote {path}", flush=True)
            print(payload["operator_answer"], flush=True)
        if args.interval_seconds <= 0:
            break
        time.sleep(max(1, args.interval_seconds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
