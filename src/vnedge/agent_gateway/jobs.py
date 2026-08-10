"""Research-only job ledger for Agent Gateway requests."""

from __future__ import annotations

import json
import re
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

JOB_ID_RE = re.compile(r"^agj_[a-f0-9]{16}$")
PENDING_STATUS = "PENDING_RESEARCH_ONLY"
RUNNING_STATUS = "RUNNING_RESEARCH_ONLY"
DONE_STATUS = "DONE_RESEARCH_ONLY"
BLOCKED_STATUS = "BLOCKED_RESEARCH_ONLY"
FAILED_STATUS = "FAILED_RESEARCH_ONLY"
TERMINAL_STATUSES = frozenset({DONE_STATUS, BLOCKED_STATUS, FAILED_STATUS})


def new_job_id() -> str:
    return f"agj_{secrets.token_hex(8)}"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _force_research_only(job: dict[str, Any]) -> dict[str, Any]:
    """Stamp the hard gateway guarantees onto every persisted job version."""
    job["can_trade"] = False
    job["can_promote"] = False
    job["live_orders_enabled"] = False
    return job


def _write_job(jobs_dir: Path, job: dict[str, Any]) -> dict[str, Any]:
    jobs_dir.mkdir(parents=True, exist_ok=True)
    _force_research_only(job)
    path = jobs_dir / f"{job['job_id']}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(job, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    return job


def _append_job_event(jobs_dir: Path, job: dict[str, Any]) -> None:
    jobs_dir.mkdir(parents=True, exist_ok=True)
    with (jobs_dir / "jobs.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(job, sort_keys=True) + "\n")


def create_backtest_job(
    *,
    jobs_dir: Path,
    agent: str,
    request: dict[str, Any],
) -> dict[str, Any]:
    jobs_dir.mkdir(parents=True, exist_ok=True)
    job = {
        "job_id": new_job_id(),
        "kind": "backtest_request",
        "status": PENDING_STATUS,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "created_by": agent,
        "request": request,
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
    }
    _write_job(jobs_dir, job)
    _append_job_event(jobs_dir, job)
    return job


def read_job(jobs_dir: Path, job_id: str) -> dict[str, Any] | None:
    if not JOB_ID_RE.match(job_id):
        return None
    path = jobs_dir / f"{job_id}.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def list_jobs(jobs_dir: Path, *, limit: int = 100) -> list[dict[str, Any]]:
    if not jobs_dir.exists():
        return []
    jobs: list[dict[str, Any]] = []
    for path in sorted(jobs_dir.glob("agj_*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            jobs.append(payload)
    jobs.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
    return jobs[: max(1, min(limit, 500))]


def pending_jobs(jobs_dir: Path, *, limit: int = 100) -> list[dict[str, Any]]:
    """Oldest-first pending jobs, for a single research worker."""
    jobs = [
        job
        for job in list_jobs(jobs_dir, limit=500)
        if job.get("status") == PENDING_STATUS
    ]
    jobs.sort(key=lambda item: str(item.get("created_at", "")))
    return jobs[: max(1, min(limit, 500))]


def update_job(
    jobs_dir: Path,
    job_id: str,
    *,
    status: str,
    result: dict[str, Any] | None = None,
    error: str | None = None,
    blocked_reason: str | None = None,
) -> dict[str, Any] | None:
    job = read_job(jobs_dir, job_id)
    if job is None:
        return None
    job["status"] = status
    job["updated_at"] = _now_iso()
    if result is not None:
        job["result"] = result
    if error is not None:
        job["error"] = error
    if blocked_reason is not None:
        job["blocked_reason"] = blocked_reason
    _write_job(jobs_dir, job)
    _append_job_event(jobs_dir, job)
    return job


def claim_job(jobs_dir: Path, job_id: str) -> dict[str, Any] | None:
    """Move one pending job into the running state.

    The compose deployment runs a single worker. This transition still keeps a
    stale pending file from being executed twice inside one worker pass.
    """
    job = read_job(jobs_dir, job_id)
    if job is None or job.get("status") != PENDING_STATUS:
        return None
    return update_job(jobs_dir, job_id, status=RUNNING_STATUS)
