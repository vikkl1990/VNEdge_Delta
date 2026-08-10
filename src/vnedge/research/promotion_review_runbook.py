"""Promotion review runbook: turns red-team charges into operator steps.

The promotion red-team argues against PASSED candidates. This module turns that
prosecution into a compact operator packet: what is blocked, what needs more
evidence, and what a human may review next. It is intentionally powerless. It
does not promote, trade, edit manifests, or relax gates.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from vnedge.research.promotion_red_team import (
    CRITICAL,
    DEFAULT_FEED as DEFAULT_EXPERIMENT_FEED,
    DEFAULT_OUT as DEFAULT_RED_TEAM_OUT,
    WARN,
    red_team_candidates,
)

RUNBOOK_ID = "promotion_review_runbook_v1"
DEFAULT_OUT = Path("research/live_research/promotion_review_runbook_latest.json")
DEFAULT_FEED = Path("research/live_research/promotion_review_runbook_feed.jsonl")

STATE_BLOCKED = "BLOCKED_BY_RED_TEAM"
STATE_NEEDS_ANSWERS = "NEEDS_OPERATOR_ANSWERS"
STATE_HUMAN_REVIEW_READY = "HUMAN_REVIEW_READY"

ACTION_BLOCK = "DO_NOT_PROMOTE_REPAIR_EVIDENCE_FIRST"
ACTION_EXPAND = "EXTEND_UNTOUCHED_SAMPLE_OR_CROSS_SYMBOL_PROOF"
ACTION_REVIEW = "OPEN_HUMAN_PAPER_REVIEW_PACKET"

_CHARGE_STEP_HINTS = {
    "thin_edge": "Replay the candidate with wider target/stop economics or a fresh untouched span; edge must clear the noise floor per trade.",
    "fee_drag": "Run maker-route and venue-fee sensitivity before paper; taker fallback is forbidden unless net edge still pays fees plus slippage.",
    "single_symbol": "Seek a second-symbol or portfolio corroboration before treating this as a durable alpha.",
    "sparse_sample": "Extend the sample on the next untouched window until tail count is robust.",
    "barely_passed_profit_factor": "Require another untouched proof where PF stays comfortably above break-even.",
    "thin_payoff": "Inspect exit geometry; widen payoff or prove win-rate stability before paper review.",
    "window_fragility": "Check fold distribution and zero-trade windows; do not promote a result carried by one lucky slice.",
}


def build_promotion_review_runbook(
    red_team_payload: Mapping[str, Any],
    *,
    source: str = "",
) -> dict[str, Any]:
    """Build a dashboard-safe promotion review packet from red-team output."""
    briefs = [
        b for b in red_team_payload.get("briefs", []) or []
        if isinstance(b, Mapping)
    ]
    rows = [_row_from_brief(brief) for brief in briefs]
    rows.sort(key=_row_sort_key)
    summary = _summary(rows)
    return {
        "runbook_id": RUNBOOK_ID,
        "generated_at": datetime.now(UTC).isoformat(),
        "source": source,
        "summary": summary,
        "rows": rows,
        "operator_answer": _operator_answer(summary),
        "policy": {
            "role": "review packet only; never an authority to promote or trade",
            "input": "promotion_red_team_v1 briefs from PASSED walk-forward candidates",
            "can_trade": False,
            "can_promote": False,
            "human_review_required": True,
            "paper_review_is_not_live_approval": True,
        },
        "can_trade": False,
        "can_promote": False,
    }


def publish_runbook(payload: Mapping[str, Any], out: Path, feed: Path | None = None) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))
    tmp.replace(out)
    out.chmod(0o644)
    if feed is not None:
        feed.parent.mkdir(parents=True, exist_ok=True)
        with open(feed, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True, default=str) + "\n")


def run_once(
    *,
    feed_path: Path | str,
    burn_registry_path: Path | str,
    paper_trials_dir: Path | str,
    red_team_out: Path,
    out: Path,
    feed: Path | None = None,
) -> dict[str, Any]:
    """Refresh red-team evidence and publish the derived review runbook."""
    red_team_payload = red_team_candidates(
        feed_path=feed_path,
        burn_registry_path=burn_registry_path,
        paper_trials_dir=paper_trials_dir,
    )
    _write_json(red_team_out, red_team_payload)
    payload = build_promotion_review_runbook(
        red_team_payload,
        source=str(red_team_out),
    )
    publish_runbook(payload, out, feed)
    return payload


def render_report(payload: Mapping[str, Any], *, limit: int = 20) -> str:
    summary = payload.get("summary", {})
    lines = [
        "=== Promotion review runbook ===",
        f"generated: {payload.get('generated_at')}",
        str(payload.get("operator_answer") or ""),
        (
            "summary: "
            f"{summary.get('total_candidates', 0)} candidates, "
            f"{summary.get('blocked_by_red_team', 0)} blocked, "
            f"{summary.get('needs_answers', 0)} need answers, "
            f"{summary.get('human_review_ready', 0)} human-review ready"
        ),
    ]
    for row in list(payload.get("rows", []))[:limit]:
        lines.append(
            f"  {row.get('review_state', 'UNKNOWN'):<26} "
            f"{row.get('exchange', '')} {row.get('symbol', '')} "
            f"{row.get('strategy_id', '')} :: {row.get('primary_charge', 'none')} "
            f"=> {row.get('next_action', '')}"
        )
    lines.append("read-only: can_trade=false can_promote=false")
    return "\n".join(lines)


def _row_from_brief(brief: Mapping[str, Any]) -> dict[str, Any]:
    charges = [
        c for c in brief.get("charges", []) or []
        if isinstance(c, Mapping)
    ]
    critical_count = int(brief.get("critical_count") or 0)
    warn_count = int(brief.get("warn_count") or 0)
    review_state, action = _state_and_action(
        str(brief.get("recommendation") or ""),
        critical_count,
        warn_count,
    )
    primary = charges[0] if charges else {}
    return {
        "strategy_id": str(brief.get("strategy_id") or ""),
        "exchange": str(brief.get("exchange") or ""),
        "symbol": str(brief.get("symbol") or ""),
        "input_verdict": str(brief.get("input_verdict") or ""),
        "red_team_recommendation": str(brief.get("recommendation") or ""),
        "review_state": review_state,
        "next_action": action,
        "primary_charge": str(primary.get("name") or "none"),
        "primary_severity": str(primary.get("severity") or "info"),
        "charge_summary": [str(c.get("claim") or c.get("name") or "") for c in charges],
        "runbook_steps": _runbook_steps(charges, review_state),
        "evidence": _evidence_summary(charges),
        "critical_count": critical_count,
        "warn_count": warn_count,
        "info_count": max(0, len(charges) - critical_count - warn_count),
        "can_trade": False,
        "can_promote": False,
    }


def _state_and_action(
    recommendation: str,
    critical_count: int,
    warn_count: int,
) -> tuple[str, str]:
    if recommendation == "DO_NOT_PROMOTE_YET" or critical_count:
        return STATE_BLOCKED, ACTION_BLOCK
    if recommendation == "NEEDS_ANSWERS" or warn_count >= 2:
        return STATE_NEEDS_ANSWERS, ACTION_EXPAND
    return STATE_HUMAN_REVIEW_READY, ACTION_REVIEW


def _runbook_steps(charges: list[Mapping[str, Any]], review_state: str) -> list[str]:
    steps: list[str] = []
    for charge in charges:
        name = str(charge.get("name") or "")
        answer = str(charge.get("what_would_answer_it") or "")
        hint = _CHARGE_STEP_HINTS.get(name)
        if hint:
            steps.append(hint)
        if answer:
            steps.append(f"Answer required: {answer}.")
    if review_state == STATE_HUMAN_REVIEW_READY:
        steps.append(
            "Human review may open a paper-trial packet, but live-capital gates remain closed."
        )
    return _dedupe(steps)


def _evidence_summary(charges: list[Mapping[str, Any]]) -> dict[str, Any]:
    evidence: dict[str, Any] = {}
    for charge in charges:
        name = str(charge.get("name") or "")
        ev = charge.get("evidence")
        if name and isinstance(ev, Mapping):
            evidence[name] = dict(ev)
    return evidence


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    blocked = sum(1 for r in rows if r["review_state"] == STATE_BLOCKED)
    needs = sum(1 for r in rows if r["review_state"] == STATE_NEEDS_ANSWERS)
    ready = sum(1 for r in rows if r["review_state"] == STATE_HUMAN_REVIEW_READY)
    return {
        "total_candidates": len(rows),
        "blocked_by_red_team": blocked,
        "needs_answers": needs,
        "human_review_ready": ready,
        "critical_charges": sum(int(r.get("critical_count") or 0) for r in rows),
        "warn_charges": sum(int(r.get("warn_count") or 0) for r in rows),
        "next_action": (
            ACTION_BLOCK if blocked
            else ACTION_EXPAND if needs
            else ACTION_REVIEW if ready
            else "WAIT_FOR_PASSED_WALK_FORWARD_CANDIDATE"
        ),
    }


def _operator_answer(summary: Mapping[str, Any]) -> str:
    total = int(summary.get("total_candidates") or 0)
    if total == 0:
        return "No passed walk-forward candidates are available for promotion review yet."
    blocked = int(summary.get("blocked_by_red_team") or 0)
    needs = int(summary.get("needs_answers") or 0)
    ready = int(summary.get("human_review_ready") or 0)
    if blocked:
        return (
            f"{blocked}/{total} passed candidate(s) are blocked by critical red-team charges; "
            "repair evidence before any paper-review packet."
        )
    if needs:
        return (
            f"{needs}/{total} passed candidate(s) need operator answers before paper review; "
            "extend untouched proof or cross-symbol evidence."
        )
    return (
        f"{ready}/{total} passed candidate(s) are defensible for human paper review; "
        "this is not live approval."
    )


def _row_sort_key(row: Mapping[str, Any]) -> tuple[int, int, str, str, str]:
    state_rank = {
        STATE_BLOCKED: 0,
        STATE_NEEDS_ANSWERS: 1,
        STATE_HUMAN_REVIEW_READY: 2,
    }.get(str(row.get("review_state") or ""), 9)
    severity_rank = {
        CRITICAL: 0,
        WARN: 1,
        "info": 2,
    }.get(str(row.get("primary_severity") or ""), 9)
    return (
        state_rank,
        severity_rank,
        str(row.get("exchange") or ""),
        str(row.get("symbol") or ""),
        str(row.get("strategy_id") or ""),
    )


def _dedupe(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))
    tmp.replace(path)
    path.chmod(0o644)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Publish a read-only operator runbook from promotion red-team evidence."
    )
    parser.add_argument("--feed", default=str(DEFAULT_EXPERIMENT_FEED))
    parser.add_argument("--burn-registry", default="research/live_research/data_burn_registry.jsonl")
    parser.add_argument("--paper-trials", default="research/paper_trials")
    parser.add_argument("--red-team-out", default=str(DEFAULT_RED_TEAM_OUT))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--runbook-feed", default=str(DEFAULT_FEED))
    parser.add_argument("--interval-seconds", type=float, default=0.0)
    parser.add_argument("--print", action="store_true")
    args = parser.parse_args(argv)

    while True:
        payload = run_once(
            feed_path=args.feed,
            burn_registry_path=args.burn_registry,
            paper_trials_dir=args.paper_trials,
            red_team_out=Path(args.red_team_out),
            out=Path(args.out),
            feed=Path(args.runbook_feed) if args.runbook_feed else None,
        )
        if args.print:
            print(render_report(payload))
        if args.interval_seconds <= 0:
            return 0
        time.sleep(args.interval_seconds)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
