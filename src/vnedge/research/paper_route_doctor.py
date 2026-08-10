"""Paper route and journal doctor.

The paper activation board says whether a lane is approved and routed. This
module answers the next operator question: if a routed paper lane is not
showing trades, is the runner alive, which journal should exist, and what is
the next safe repair action?

Read-only by design. It cannot start containers, edit manifests, or trade.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from collections import Counter
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_RESEARCH_DIR = Path("research/live_research")
DEFAULT_ACTIVATION = DEFAULT_RESEARCH_DIR / "paper_lane_activation_latest.json"
DEFAULT_FLEET = Path("logs/fleet.json")
DEFAULT_JOURNAL_DIR = Path("logs/paper_trials")
DEFAULT_OUT = DEFAULT_RESEARCH_DIR / "paper_route_doctor_latest.json"
DEFAULT_FEED = DEFAULT_RESEARCH_DIR / "paper_route_doctor_feed.jsonl"

STATE_JOURNAL_ACTIVE = "JOURNAL_ACTIVE"
STATE_JOURNAL_STALE = "JOURNAL_STALE"
STATE_ROUTE_READY_JOURNAL_MISSING = "ROUTE_READY_JOURNAL_MISSING"
STATE_RUNNER_SERVICE_DOWN = "RUNNER_SERVICE_DOWN"
STATE_ROUTE_NOT_WIRED = "ROUTE_NOT_WIRED"
STATE_APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
STATE_MANIFEST_UNSAFE = "MANIFEST_UNSAFE"
STATE_BLOCKED_NEGATIVE = "BLOCKED_NEGATIVE"
STATE_OBSERVE_ONLY = "OBSERVE_ONLY"
STATE_UNKNOWN = "UNKNOWN"

_RUNNER_SERVICES = ("multi-lane-shadow",)
_ROUTE_STATES = {
    "PAPER_RUNNING",
    "PAPER_ONLINE_WAITING",
    "PAPER_ROUTE_READY_NO_JOURNAL",
}


@dataclass(frozen=True)
class PaperRouteDoctorConfig:
    stale_after_hours: float = 3.0
    max_rows: int = 180
    runner_services: tuple[str, ...] = _RUNNER_SERVICES

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["runner_services"] = list(self.runner_services)
        return data


def build_paper_route_doctor(
    *,
    activation: Mapping[str, Any] | None = None,
    fleet: Mapping[str, Any] | None = None,
    activation_path: Path | str = DEFAULT_ACTIVATION,
    fleet_path: Path | str = DEFAULT_FLEET,
    journal_dir: Path | str = DEFAULT_JOURNAL_DIR,
    config: PaperRouteDoctorConfig = PaperRouteDoctorConfig(),
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the read-only route/journal doctor report."""
    now = now or datetime.now(UTC)
    activation_path = Path(activation_path)
    fleet_path = Path(fleet_path)
    journal_dir = Path(journal_dir)
    activation_payload = (
        dict(activation)
        if isinstance(activation, Mapping)
        else _read_json_payload(activation_path, {"rows": [], "summary": {}})
    )
    fleet_payload = (
        dict(fleet)
        if isinstance(fleet, Mapping)
        else _read_json_payload(fleet_path, {"services": []})
    )
    service_state = _service_state(fleet_payload, config.runner_services)
    rows = [
        _doctor_row(row, journal_dir=journal_dir, service_state=service_state, now=now, config=config)
        for row in activation_payload.get("rows", []) or []
        if isinstance(row, Mapping)
    ]
    rows.sort(key=_row_sort_key)
    rows = rows[: max(1, int(config.max_rows))]
    summary = _summary(rows, service_state)
    return {
        "generated_at": now.isoformat(),
        "report_id": "paper_route_doctor_v1",
        "mode": "read_only_paper_route_doctor",
        "source_report_id": activation_payload.get("report_id"),
        "source_generated_at": activation_payload.get("generated_at"),
        "inputs": {
            "activation_path": str(activation_path),
            "fleet_path": str(fleet_path),
            "journal_dir": str(journal_dir),
        },
        "config": config.to_dict(),
        "runner_service": service_state,
        "summary": summary,
        "rows": rows,
        "operator_answer": _operator_answer(summary),
        "policy": {
            "read_only": True,
            "can_trade": False,
            "can_promote": False,
            "can_restart_runner": False,
        },
        "can_trade": False,
        "can_promote": False,
    }


def publish_paper_route_doctor(
    payload: Mapping[str, Any], out: Path, feed: Path | None = None
) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    tmp.replace(out)
    if feed is not None:
        feed.parent.mkdir(parents=True, exist_ok=True)
        with open(feed, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, default=str) + "\n")


def render_report(payload: Mapping[str, Any], *, limit: int = 40) -> str:
    summary = payload.get("summary", {})
    lines = [
        "=== Paper route doctor ===",
        f"generated: {payload.get('generated_at')}",
        str(payload.get("operator_answer") or ""),
        (
            "summary: "
            f"{summary.get('total_rows', 0)} rows, "
            f"{summary.get('journal_active', 0)} active, "
            f"{summary.get('journal_stale', 0)} stale, "
            f"{summary.get('journal_missing', 0)} missing, "
            f"runner={summary.get('runner_state', 'unknown')}"
        ),
    ]
    for row in list(payload.get("rows", []))[:limit]:
        lines.append(
            f"  {row.get('doctor_state', ''):<30} "
            f"{row.get('exchange', ''):<14} {row.get('symbol', ''):<14} "
            f"{row.get('timeframe', ''):<3} {row.get('strategy_id', ''):<28} "
            f"{row.get('next_action', '')}"
        )
    lines.append("read-only: can_trade=false can_promote=false")
    return "\n".join(lines)


def _doctor_row(
    source: Mapping[str, Any],
    *,
    journal_dir: Path,
    service_state: Mapping[str, Any],
    now: datetime,
    config: PaperRouteDoctorConfig,
) -> dict[str, Any]:
    trial_id = str(source.get("trial_id") or "")
    route_ids = _route_ids(source)
    expected_id = route_ids[0] if route_ids else trial_id
    expected_journal = journal_dir / f"{expected_id}.journal.jsonl" if expected_id else None
    expected_equity = journal_dir / f"{expected_id}.equity.jsonl" if expected_id else None
    expected_account = journal_dir / f"{expected_id}.account.json" if expected_id else None
    expected_fills = journal_dir / f"{expected_id}.fills.jsonl" if expected_id else None

    journal = _paper_journal(source)
    actual_journal = Path(str(journal.get("journal"))) if journal.get("journal") else expected_journal
    journal_seen = bool(journal) or bool(actual_journal and actual_journal.exists())
    latest_ts = str(journal.get("last_ts") or "")
    if not latest_ts and actual_journal is not None:
        latest_ts = _last_jsonl_ts(actual_journal)
    latest_dt = _parse_dt(latest_ts) or _mtime_dt(actual_journal)
    age_hours = (now - latest_dt).total_seconds() / 3600 if latest_dt else None
    doctor_state, next_action = _doctor_state(
        source,
        journal_seen=journal_seen,
        age_hours=age_hours,
        service_state=service_state,
        config=config,
    )
    return {
        "lane_key": source.get("lane_key"),
        "trial_id": trial_id or None,
        "route_lane_ids": route_ids,
        "expected_lane_id": expected_id or None,
        "exchange": source.get("exchange"),
        "symbol": source.get("symbol"),
        "timeframe": source.get("timeframe"),
        "strategy_id": source.get("strategy_id"),
        "activation_state": source.get("activation_state"),
        "route_status": source.get("route_status"),
        "doctor_state": doctor_state,
        "journal_seen": journal_seen,
        "journal_path": str(actual_journal) if actual_journal is not None else None,
        "expected_paths": {
            "journal": str(expected_journal) if expected_journal is not None else None,
            "equity": str(expected_equity) if expected_equity is not None else None,
            "account": str(expected_account) if expected_account is not None else None,
            "fills": str(expected_fills) if expected_fills is not None else None,
        },
        "latest_ts": latest_ts or None,
        "age_hours": round(age_hours, 4) if age_hours is not None else None,
        "journal_counts": {
            "heartbeats": int(journal.get("paper_lane_heartbeats") or 0),
            "evals": int(journal.get("evals") or 0),
            "order_intents": int(journal.get("paper_order_intents") or 0),
            "reports": int(journal.get("paper_reports") or 0),
        },
        "latest_why": journal.get("latest_why"),
        "runner_up": service_state.get("up"),
        "runner_status": service_state.get("status"),
        "next_action": next_action,
        "can_trade": False,
        "can_promote": False,
    }


def _doctor_state(
    source: Mapping[str, Any],
    *,
    journal_seen: bool,
    age_hours: float | None,
    service_state: Mapping[str, Any],
    config: PaperRouteDoctorConfig,
) -> tuple[str, str]:
    activation_state = str(source.get("activation_state") or "")
    route_status = str(source.get("route_status") or "")
    runner_up = service_state.get("up")
    if activation_state == "MANIFEST_UNSAFE":
        return STATE_MANIFEST_UNSAFE, "fix manifest: live_orders_enabled must stay false"
    if activation_state == "NEEDS_HUMAN_PAPER_APPROVAL":
        return STATE_APPROVAL_REQUIRED, "create/approve a locked paper-trial manifest"
    if activation_state == "BLOCKED_NEGATIVE_EDGE":
        return STATE_BLOCKED_NEGATIVE, "do not run paper; mine rejection reasons first"
    if route_status == "ROUTE_BLOCKED" or activation_state == "ROUTE_BLOCKED":
        return STATE_ROUTE_NOT_WIRED, "wire this manifest into the multi-lane paper roster"
    if activation_state in _ROUTE_STATES and runner_up is False:
        return STATE_RUNNER_SERVICE_DOWN, "restart/inspect multi-lane-shadow before judging the lane"
    if journal_seen:
        if age_hours is not None and age_hours > config.stale_after_hours:
            return STATE_JOURNAL_STALE, "paper journal is stale; inspect runner/feed before judging signals"
        return STATE_JOURNAL_ACTIVE, "journal proof exists; use performance ledger for trade quality"
    if activation_state in _ROUTE_STATES:
        return STATE_ROUTE_READY_JOURNAL_MISSING, "route exists but expected journal is absent; inspect runner write path"
    if activation_state == "OBSERVE_ONLY":
        return STATE_OBSERVE_ONLY, "observation-only lane; attach a locked manifest before paper"
    return STATE_UNKNOWN, "inspect activation row; doctor could not classify it"


def _summary(rows: list[dict[str, Any]], service_state: Mapping[str, Any]) -> dict[str, Any]:
    states = Counter(str(r.get("doctor_state") or "") for r in rows)
    return {
        "total_rows": len(rows),
        "journal_active": states[STATE_JOURNAL_ACTIVE],
        "journal_stale": states[STATE_JOURNAL_STALE],
        "journal_missing": states[STATE_ROUTE_READY_JOURNAL_MISSING],
        "runner_down": states[STATE_RUNNER_SERVICE_DOWN],
        "route_not_wired": states[STATE_ROUTE_NOT_WIRED],
        "approval_required": states[STATE_APPROVAL_REQUIRED],
        "manifest_unsafe": states[STATE_MANIFEST_UNSAFE],
        "blocked_negative": states[STATE_BLOCKED_NEGATIVE],
        "runner_state": service_state.get("state"),
        "runner_status": service_state.get("status"),
        "state_counts": dict(sorted(states.items())),
        "can_trade": False,
        "can_promote": False,
    }


def _operator_answer(summary: Mapping[str, Any]) -> str:
    active = int(summary.get("journal_active") or 0)
    stale = int(summary.get("journal_stale") or 0)
    missing = int(summary.get("journal_missing") or 0)
    runner_down = int(summary.get("runner_down") or 0)
    route_not_wired = int(summary.get("route_not_wired") or 0)
    if runner_down:
        return f"{runner_down} paper route(s) cannot be judged because the paper runner is down."
    if missing:
        return f"{missing} paper route(s) are wired but missing journal proof; inspect runner write path."
    if stale:
        return f"{stale} paper journal(s) are stale; inspect runner/feed freshness before judging signals."
    if route_not_wired:
        return f"{route_not_wired} approved manifest(s) are not wired into the paper route roster."
    if active:
        return f"{active} paper journal(s) are active; use the performance ledger for trade quality."
    return "No paper route/journal evidence is ready to judge yet."


def _route_ids(row: Mapping[str, Any]) -> list[str]:
    runtime = row.get("runtime") if isinstance(row.get("runtime"), Mapping) else {}
    return [str(x) for x in runtime.get("desired_lane_ids") or [] if x]


def _paper_journal(row: Mapping[str, Any]) -> dict[str, Any]:
    evidence = row.get("evidence") if isinstance(row.get("evidence"), Mapping) else {}
    journal = evidence.get("paper_journal") if isinstance(evidence.get("paper_journal"), Mapping) else {}
    return dict(journal)


def _service_state(fleet: Mapping[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
    services = fleet.get("services") if isinstance(fleet.get("services"), list) else []
    wanted = set(names)
    matches = [
        s for s in services
        if isinstance(s, Mapping) and str(s.get("name") or "") in wanted
    ]
    if not services:
        return {
            "state": "unknown",
            "up": None,
            "status": "fleet status unavailable",
            "services": list(names),
        }
    if not matches:
        return {
            "state": "missing",
            "up": False,
            "status": "runner service absent from fleet report",
            "services": list(names),
        }
    up = any(bool(s.get("up")) for s in matches)
    return {
        "state": "up" if up else "down",
        "up": up,
        "status": "; ".join(str(s.get("status") or "") for s in matches),
        "services": [str(s.get("name") or "") for s in matches],
    }


def _last_jsonl_ts(path: Path | None) -> str:
    if path is None or not path.exists():
        return ""
    for line in reversed(_tail_lines(path, 200_000)):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, Mapping):
            continue
        ts = record.get("ts")
        if ts:
            return str(ts)
    return ""


def _tail_lines(path: Path, max_bytes: int) -> list[str]:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            data = handle.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]
    return [line for line in lines if line.strip()]


def _parse_dt(value: object) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _mtime_dt(path: Path | None) -> datetime | None:
    if path is None:
        return None
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        return None


def _read_json_payload(path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback
    return payload if isinstance(payload, dict) else fallback


def _row_sort_key(row: Mapping[str, Any]) -> tuple[int, str, str, str]:
    priority = {
        STATE_RUNNER_SERVICE_DOWN: 0,
        STATE_ROUTE_READY_JOURNAL_MISSING: 1,
        STATE_JOURNAL_STALE: 2,
        STATE_ROUTE_NOT_WIRED: 3,
        STATE_MANIFEST_UNSAFE: 4,
        STATE_APPROVAL_REQUIRED: 5,
        STATE_BLOCKED_NEGATIVE: 6,
        STATE_JOURNAL_ACTIVE: 7,
        STATE_OBSERVE_ONLY: 8,
    }.get(str(row.get("doctor_state") or ""), 9)
    return (
        priority,
        str(row.get("exchange") or ""),
        str(row.get("symbol") or ""),
        str(row.get("strategy_id") or ""),
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--activation", type=Path, default=DEFAULT_ACTIVATION)
    parser.add_argument("--fleet", type=Path, default=DEFAULT_FLEET)
    parser.add_argument("--journal-dir", type=Path, default=DEFAULT_JOURNAL_DIR)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--feed", type=Path, default=DEFAULT_FEED)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval-seconds", type=float, default=60.0)
    parser.add_argument("--stale-after-hours", type=float, default=3.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    config = PaperRouteDoctorConfig(stale_after_hours=args.stale_after_hours)
    while True:
        payload = build_paper_route_doctor(
            activation_path=args.activation,
            fleet_path=args.fleet,
            journal_dir=args.journal_dir,
            config=config,
        )
        publish_paper_route_doctor(payload, args.out, args.feed)
        print(render_report(payload), flush=True)
        if args.once:
            return 0
        time.sleep(max(1.0, float(args.interval_seconds)))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
