"""Paper lane evaluation cadence monitor.

Activation answers whether a lane is approved and routed. Performance answers
whether it made money. This module answers the operational question between
those two:

    "Is each paper lane actually evaluating often enough for its timeframe?"

Read-only by design. It inspects activation rows and paper decision journals;
it never starts a runner, edits manifests, or submits orders.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from collections import Counter
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_RESEARCH_DIR = Path("research/live_research")
DEFAULT_ACTIVATION = DEFAULT_RESEARCH_DIR / "paper_lane_activation_latest.json"
DEFAULT_JOURNAL_DIR = Path("logs/paper_trials")
DEFAULT_OUT = DEFAULT_RESEARCH_DIR / "paper_lane_cadence_latest.json"
DEFAULT_FEED = DEFAULT_RESEARCH_DIR / "paper_lane_cadence_feed.jsonl"

STATE_EVALUATING_SIGNAL_SEEN = "EVALUATING_SIGNAL_SEEN"
STATE_EVALUATING_NO_SIGNAL = "EVALUATING_NO_SIGNAL"
STATE_HEARTBEAT_ONLY_NO_EVAL = "HEARTBEAT_ONLY_NO_EVAL"
STATE_EVAL_STALE = "EVAL_STALE"
STATE_HEARTBEAT_STALE = "HEARTBEAT_STALE"
STATE_JOURNAL_MISSING = "JOURNAL_MISSING"
STATE_ROUTE_NOT_ACTIVE = "ROUTE_NOT_ACTIVE"
STATE_UNKNOWN = "UNKNOWN"

_ROUTE_STATES = {
    "PAPER_RUNNING",
    "PAPER_ONLINE_WAITING",
    "PAPER_ROUTE_READY_NO_JOURNAL",
}


@dataclass(frozen=True)
class PaperLaneCadenceConfig:
    grace_multiplier: float = 2.5
    min_eval_sla_seconds: int = 180
    max_rows: int = 180
    tail_bytes: int = 3_000_000

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_paper_lane_cadence(
    *,
    activation: Mapping[str, Any] | None = None,
    activation_path: Path | str = DEFAULT_ACTIVATION,
    journal_dir: Path | str = DEFAULT_JOURNAL_DIR,
    config: PaperLaneCadenceConfig = PaperLaneCadenceConfig(),
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    activation_path = Path(activation_path)
    journal_dir = Path(journal_dir)
    activation_payload = (
        dict(activation)
        if isinstance(activation, Mapping)
        else _read_json_payload(activation_path, {"rows": [], "summary": {}})
    )
    rows = [
        _cadence_row(row, journal_dir=journal_dir, config=config, now=now)
        for row in activation_payload.get("rows", []) or []
        if isinstance(row, Mapping)
    ]
    rows.sort(key=_row_sort_key)
    rows = rows[: max(1, int(config.max_rows))]
    summary = _summary(rows)
    return {
        "generated_at": now.isoformat(),
        "report_id": "paper_lane_cadence_v1",
        "mode": "read_only_paper_lane_cadence",
        "source_report_id": activation_payload.get("report_id"),
        "source_generated_at": activation_payload.get("generated_at"),
        "inputs": {
            "activation_path": str(activation_path),
            "journal_dir": str(journal_dir),
        },
        "config": config.to_dict(),
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


def publish_paper_lane_cadence(
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
        "=== Paper lane cadence ===",
        f"generated: {payload.get('generated_at')}",
        str(payload.get("operator_answer") or ""),
        (
            "summary: "
            f"{summary.get('total_rows', 0)} rows, "
            f"{summary.get('cadence_ok', 0)} ok, "
            f"{summary.get('stale', 0)} stale, "
            f"{summary.get('journal_missing', 0)} missing, "
            f"{summary.get('heartbeat_only', 0)} heartbeat-only"
        ),
    ]
    for row in list(payload.get("rows", []))[:limit]:
        lines.append(
            f"  {row.get('cadence_state', ''):<26} "
            f"{row.get('exchange', ''):<14} {row.get('symbol', ''):<14} "
            f"{row.get('timeframe', ''):<3} {row.get('strategy_id', ''):<28} "
            f"{row.get('next_action', '')}"
        )
    lines.append("read-only: can_trade=false can_promote=false")
    return "\n".join(lines)


def _cadence_row(
    source: Mapping[str, Any],
    *,
    journal_dir: Path,
    config: PaperLaneCadenceConfig,
    now: datetime,
) -> dict[str, Any]:
    trial_id = str(source.get("trial_id") or "")
    lane_ids = _route_ids(source)
    expected_lane_id = lane_ids[0] if lane_ids else trial_id
    journal_path = journal_dir / f"{expected_lane_id}.journal.jsonl" if expected_lane_id else None
    journal = _journal_cadence(journal_path, config=config)
    evidence = _paper_journal(source)
    if not journal["journal_seen"] and evidence:
        journal = _journal_from_evidence(evidence, journal_path)

    timeframe = str(source.get("timeframe") or journal.get("timeframe") or "")
    expected_seconds = _expected_seconds(timeframe, config)
    eval_age = _age_seconds(journal.get("latest_eval_ts"), now)
    heartbeat_age = _age_seconds(journal.get("latest_heartbeat_ts"), now)
    event_age = _age_seconds(journal.get("latest_event_ts"), now)
    state, next_action = _cadence_state(
        source,
        journal=journal,
        expected_seconds=expected_seconds,
        eval_age_seconds=eval_age,
        heartbeat_age_seconds=heartbeat_age,
    )
    return {
        "lane_key": source.get("lane_key"),
        "trial_id": trial_id or None,
        "expected_lane_id": expected_lane_id or None,
        "journal_path": str(journal_path) if journal_path is not None else None,
        "exchange": source.get("exchange") or journal.get("exchange"),
        "symbol": source.get("symbol") or journal.get("symbol"),
        "timeframe": timeframe or None,
        "strategy_id": source.get("strategy_id") or journal.get("strategy_id"),
        "activation_state": source.get("activation_state"),
        "route_status": source.get("route_status"),
        "cadence_state": state,
        "expected_eval_seconds": expected_seconds,
        "eval_age_seconds": round(eval_age, 3) if eval_age is not None else None,
        "heartbeat_age_seconds": round(heartbeat_age, 3) if heartbeat_age is not None else None,
        "event_age_seconds": round(event_age, 3) if event_age is not None else None,
        "latest_eval_ts": journal.get("latest_eval_ts"),
        "latest_heartbeat_ts": journal.get("latest_heartbeat_ts"),
        "latest_event_ts": journal.get("latest_event_ts"),
        "last_bar_ts": journal.get("last_bar_ts"),
        "latest_why": journal.get("latest_why"),
        "latest_signal_reason": journal.get("latest_signal_reason"),
        "latest_eval_fired": bool(journal.get("latest_eval_fired")),
        "counts": {
            "evals": int(journal.get("evals") or 0),
            "live_evals": int(journal.get("live_evals") or 0),
            "backfill_evals": int(journal.get("backfill_evals") or 0),
            "heartbeats": int(journal.get("heartbeats") or 0),
            "signals": int(journal.get("signals") or 0),
            "order_intents": int(journal.get("order_intents") or 0),
        },
        "next_action": next_action,
        "can_trade": False,
        "can_promote": False,
    }


def _cadence_state(
    source: Mapping[str, Any],
    *,
    journal: Mapping[str, Any],
    expected_seconds: int,
    eval_age_seconds: float | None,
    heartbeat_age_seconds: float | None,
) -> tuple[str, str]:
    activation_state = str(source.get("activation_state") or "")
    if activation_state not in _ROUTE_STATES:
        return STATE_ROUTE_NOT_ACTIVE, "not an active paper route; use activation board first"
    if not bool(journal.get("journal_seen")):
        return STATE_JOURNAL_MISSING, "route is active but no paper journal proof exists"
    if int(journal.get("live_evals") or 0) <= 0:
        if heartbeat_age_seconds is not None and heartbeat_age_seconds <= expected_seconds:
            return STATE_HEARTBEAT_ONLY_NO_EVAL, "runner pulses but no live evals; inspect lane adapter wiring"
        return STATE_HEARTBEAT_STALE, "no live evals and heartbeat is stale or absent"
    if eval_age_seconds is None:
        return STATE_UNKNOWN, "journal exists but no evaluable lane_eval timestamp was found"
    if eval_age_seconds > expected_seconds:
        return STATE_EVAL_STALE, "lane_eval cadence is stale for this timeframe"
    if int(journal.get("signals") or 0) > 0 or bool(journal.get("latest_eval_fired")):
        return STATE_EVALUATING_SIGNAL_SEEN, "lane is evaluating and has fired at least once"
    return STATE_EVALUATING_NO_SIGNAL, "lane is evaluating on schedule but setup thresholds are not firing"


def _journal_cadence(path: Path | None, *, config: PaperLaneCadenceConfig) -> dict[str, Any]:
    if path is None or not path.exists():
        return {"journal_seen": False}
    counters: Counter[str] = Counter()
    latest_event_ts = ""
    latest_eval_ts = ""
    latest_heartbeat_ts = ""
    last_bar_ts = ""
    latest_why = ""
    latest_signal_reason = ""
    latest_eval_fired = False
    exchange = ""
    symbol = ""
    timeframe = ""
    strategy_id = ""
    for record in _iter_jsonl(path, max_bytes=config.tail_bytes):
        ts = str(record.get("ts") or "")
        latest_event_ts = ts or latest_event_ts
        kind = str(record.get("kind") or "")
        counters[kind] += 1
        payload = record.get("payload") if isinstance(record.get("payload"), Mapping) else {}
        if kind == "lane_eval":
            exchange = str(payload.get("exchange") or exchange)
            symbol = str(payload.get("symbol") or symbol)
            timeframe = str(payload.get("timeframe") or timeframe)
            strategy_id = str(payload.get("strategy_id") or strategy_id)
            last_bar_ts = str(payload.get("bar_ts") or payload.get("last_bar_ts") or last_bar_ts or "")
            latest_why = str(
                payload.get("skip_reason")
                or payload.get("signal_reason")
                or latest_why
                or ""
            )
            fired = bool(payload.get("fired"))
            if bool(payload.get("backfill")):
                counters["backfill_evals"] += 1
            else:
                counters["live_evals"] += 1
                latest_eval_ts = ts or latest_eval_ts
                latest_eval_fired = fired
            if fired and not bool(payload.get("backfill")):
                counters["signals"] += 1
                latest_signal_reason = str(payload.get("signal_reason") or latest_signal_reason)
        elif kind == "paper_lane_heartbeat":
            latest_heartbeat_ts = ts or latest_heartbeat_ts
            exchange = str(payload.get("exchange") or exchange)
            symbol = str(payload.get("symbol") or symbol)
            timeframe = str(payload.get("timeframe") or timeframe)
            strategy_id = str(payload.get("strategy_id") or strategy_id)
            latest_why = str(payload.get("why_no_trade") or payload.get("reason") or latest_why)
        elif kind == "order_intent":
            counters["order_intents"] += 1
    return {
        "journal_seen": True,
        "latest_event_ts": latest_event_ts or None,
        "latest_eval_ts": latest_eval_ts or None,
        "latest_heartbeat_ts": latest_heartbeat_ts or None,
        "last_bar_ts": last_bar_ts or None,
        "latest_why": latest_why or None,
        "latest_signal_reason": latest_signal_reason or None,
        "latest_eval_fired": latest_eval_fired,
        "evals": int(counters.get("lane_eval") or 0),
        "live_evals": int(counters.get("live_evals") or 0),
        "backfill_evals": int(counters.get("backfill_evals") or 0),
        "heartbeats": int(counters.get("paper_lane_heartbeat") or 0),
        "signals": int(counters.get("signals") or 0),
        "order_intents": int(counters.get("order_intents") or 0),
        "exchange": exchange,
        "symbol": symbol,
        "timeframe": timeframe,
        "strategy_id": strategy_id,
    }


def _journal_from_evidence(evidence: Mapping[str, Any], path: Path | None) -> dict[str, Any]:
    latest_heartbeat = (
        evidence.get("latest_heartbeat")
        if isinstance(evidence.get("latest_heartbeat"), Mapping)
        else {}
    )
    return {
        "journal_seen": True,
        "latest_event_ts": evidence.get("last_ts"),
        "latest_eval_ts": None,
        "latest_heartbeat_ts": evidence.get("last_ts"),
        "last_bar_ts": latest_heartbeat.get("last_bar_ts") or latest_heartbeat.get("bar_ts"),
        "latest_why": evidence.get("latest_why"),
        "latest_signal_reason": None,
        "latest_eval_fired": False,
        "evals": int(evidence.get("evals") or 0),
        "live_evals": int(evidence.get("evals") or 0),
        "backfill_evals": 0,
        "heartbeats": int(evidence.get("paper_lane_heartbeats") or 0),
        "signals": 0,
        "order_intents": int(evidence.get("paper_order_intents") or 0),
        "exchange": evidence.get("exchange"),
        "symbol": evidence.get("symbol"),
        "timeframe": evidence.get("timeframe"),
        "strategy_id": evidence.get("strategy_id"),
        "journal": str(path) if path is not None else None,
    }


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    states = Counter(str(r.get("cadence_state") or "") for r in rows)
    return {
        "total_rows": len(rows),
        "cadence_ok": states[STATE_EVALUATING_NO_SIGNAL] + states[STATE_EVALUATING_SIGNAL_SEEN],
        "signals_seen": states[STATE_EVALUATING_SIGNAL_SEEN],
        "evaluating_no_signal": states[STATE_EVALUATING_NO_SIGNAL],
        "heartbeat_only": states[STATE_HEARTBEAT_ONLY_NO_EVAL],
        "stale": states[STATE_EVAL_STALE] + states[STATE_HEARTBEAT_STALE],
        "journal_missing": states[STATE_JOURNAL_MISSING],
        "route_not_active": states[STATE_ROUTE_NOT_ACTIVE],
        "evals": sum(int(r.get("counts", {}).get("evals") or 0) for r in rows),
        "live_evals": sum(int(r.get("counts", {}).get("live_evals") or 0) for r in rows),
        "heartbeats": sum(int(r.get("counts", {}).get("heartbeats") or 0) for r in rows),
        "signals": sum(int(r.get("counts", {}).get("signals") or 0) for r in rows),
        "state_counts": dict(sorted(states.items())),
        "can_trade": False,
        "can_promote": False,
    }


def _operator_answer(summary: Mapping[str, Any]) -> str:
    missing = int(summary.get("journal_missing") or 0)
    stale = int(summary.get("stale") or 0)
    heartbeat_only = int(summary.get("heartbeat_only") or 0)
    ok = int(summary.get("cadence_ok") or 0)
    signals = int(summary.get("signals_seen") or 0)
    if missing:
        return f"{missing} active paper route(s) are missing decision-journal proof."
    if stale:
        return f"{stale} paper lane(s) have stale eval/heartbeat cadence."
    if heartbeat_only:
        return f"{heartbeat_only} paper lane(s) pulse but have no live lane_eval records."
    if signals:
        return f"{signals} paper lane(s) are evaluating and have fired at least once."
    if ok:
        return f"{ok} paper lane(s) are evaluating on schedule but not firing."
    return "No active paper cadence proof is available yet."


def _route_ids(row: Mapping[str, Any]) -> list[str]:
    runtime = row.get("runtime") if isinstance(row.get("runtime"), Mapping) else {}
    return [str(x) for x in runtime.get("desired_lane_ids") or [] if x]


def _paper_journal(row: Mapping[str, Any]) -> dict[str, Any]:
    evidence = row.get("evidence") if isinstance(row.get("evidence"), Mapping) else {}
    journal = evidence.get("paper_journal") if isinstance(evidence.get("paper_journal"), Mapping) else {}
    return dict(journal)


def _expected_seconds(timeframe: str, config: PaperLaneCadenceConfig) -> int:
    seconds = _timeframe_seconds(timeframe)
    return max(int(config.min_eval_sla_seconds), int(seconds * config.grace_multiplier))


def _timeframe_seconds(timeframe: str) -> int:
    text = timeframe.strip().lower()
    if not text:
        return 3600
    unit = text[-1]
    try:
        value = int(text[:-1] or "1")
    except ValueError:
        return 3600
    if unit == "s":
        return value
    if unit == "m":
        return value * 60
    if unit == "h":
        return value * 3600
    if unit == "d":
        return value * 86400
    return 3600


def _age_seconds(value: object, now: datetime) -> float | None:
    parsed = _parse_dt(value)
    if parsed is None:
        return None
    return max(0.0, (now - parsed).total_seconds())


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


def _iter_jsonl(path: Path, *, max_bytes: int) -> Iterator[dict[str, Any]]:
    for line in _tail_lines(path, max_bytes):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            yield record


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


def _read_json_payload(path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback
    return payload if isinstance(payload, dict) else fallback


def _row_sort_key(row: Mapping[str, Any]) -> tuple[int, str, str, str]:
    priority = {
        STATE_JOURNAL_MISSING: 0,
        STATE_EVAL_STALE: 1,
        STATE_HEARTBEAT_STALE: 2,
        STATE_HEARTBEAT_ONLY_NO_EVAL: 3,
        STATE_EVALUATING_SIGNAL_SEEN: 4,
        STATE_EVALUATING_NO_SIGNAL: 5,
        STATE_ROUTE_NOT_ACTIVE: 6,
    }.get(str(row.get("cadence_state") or ""), 9)
    return (
        priority,
        str(row.get("exchange") or ""),
        str(row.get("symbol") or ""),
        str(row.get("strategy_id") or ""),
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--activation", type=Path, default=DEFAULT_ACTIVATION)
    parser.add_argument("--journal-dir", type=Path, default=DEFAULT_JOURNAL_DIR)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--feed", type=Path, default=DEFAULT_FEED)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval-seconds", type=float, default=60.0)
    parser.add_argument("--grace-multiplier", type=float, default=2.5)
    parser.add_argument("--min-eval-sla-seconds", type=int, default=180)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    config = PaperLaneCadenceConfig(
        grace_multiplier=args.grace_multiplier,
        min_eval_sla_seconds=args.min_eval_sla_seconds,
    )
    while True:
        payload = build_paper_lane_cadence(
            activation_path=args.activation,
            journal_dir=args.journal_dir,
            config=config,
        )
        publish_paper_lane_cadence(payload, args.out, args.feed)
        print(render_report(payload), flush=True)
        if args.once:
            return 0
        time.sleep(max(1.0, float(args.interval_seconds)))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
