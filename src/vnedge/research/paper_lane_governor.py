"""Paper lane governor.

The lane survival engine says how each paper lane is behaving. This module
turns that into a proposed operating roster:

* which lanes stay in paper observation,
* which lanes move into a survivor tournament,
* which lanes should be demoted back to shadow/research,
* which lanes must be repaired before they can be judged.

It is deliberately read-only. It writes recommendations and autopsies only;
it cannot edit manifests, stop services, promote lanes, or submit orders.
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_RESEARCH_DIR = Path("research/live_research")
DEFAULT_SURVIVAL = DEFAULT_RESEARCH_DIR / "lane_survival_latest.json"
DEFAULT_OUT = DEFAULT_RESEARCH_DIR / "paper_lane_governor_latest.json"
DEFAULT_FEED = DEFAULT_RESEARCH_DIR / "paper_lane_governor_feed.jsonl"

ACTION_KEEP_PAPER_SURVIVOR = "KEEP_PAPER_SURVIVOR"
ACTION_EXTEND_PAPER_SAMPLE = "EXTEND_PAPER_SAMPLE"
ACTION_DEMOTE_TO_SHADOW_RECOMMENDED = "DEMOTE_TO_SHADOW_RECOMMENDED"
ACTION_HOLD_PAPER_PROBATION = "HOLD_PAPER_PROBATION"
ACTION_REPAIR_ROUTE_OR_CADENCE = "REPAIR_ROUTE_OR_CADENCE"
ACTION_REPAIR_LEDGER = "REPAIR_LEDGER"
ACTION_KEEP_RESEARCH_ONLY = "KEEP_RESEARCH_ONLY"
ACTION_WAIT_FOR_TRADE_EVIDENCE = "WAIT_FOR_TRADE_EVIDENCE"

BUCKET_PAPER_ROSTER = "PAPER_ROSTER"
BUCKET_SURVIVOR_TOURNAMENT = "SURVIVOR_TOURNAMENT"
BUCKET_DEMOTION_QUEUE = "DEMOTION_QUEUE"
BUCKET_PROBATION_QUEUE = "PROBATION_QUEUE"
BUCKET_REPAIR_QUEUE = "REPAIR_QUEUE"
BUCKET_RESEARCH_ONLY = "RESEARCH_ONLY"
BUCKET_NO_EVIDENCE = "NO_EVIDENCE"


@dataclass(frozen=True)
class PaperLaneGovernorConfig:
    min_closed_trades: int = 20
    min_profit_factor: float = 1.5
    min_avg_net_bps: float = 25.0
    demote_after_negative_closed: int = 5
    max_paper_roster: int = 18
    max_tournament_lanes: int = 12
    max_rows: int = 180

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_paper_lane_governor(
    *,
    survival: Mapping[str, Any] | None = None,
    survival_path: Path | str = DEFAULT_SURVIVAL,
    config: PaperLaneGovernorConfig = PaperLaneGovernorConfig(),
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the proposed paper operating roster from lane-survival evidence."""
    now = now or datetime.now(UTC)
    survival_path = Path(survival_path)
    survival_payload = (
        dict(survival)
        if isinstance(survival, Mapping)
        else _read_json_payload(survival_path, {"rows": [], "summary": {}})
    )
    rows = [
        _governor_row(row, config=config)
        for row in survival_payload.get("rows", []) or []
        if isinstance(row, Mapping)
    ]
    rows.sort(key=_row_sort_key)
    rows = rows[: max(1, int(config.max_rows))]
    summary = _summary(rows, config=config)
    return {
        "generated_at": now.isoformat(),
        "report_id": "paper_lane_governor_v1",
        "mode": "read_only_paper_lane_governor",
        "source_report_id": survival_payload.get("report_id"),
        "source_generated_at": survival_payload.get("generated_at"),
        "inputs": {"survival_path": str(survival_path)},
        "config": config.to_dict(),
        "summary": summary,
        "proposed_roster": _proposed_roster(rows, config=config),
        "boards": _boards(rows),
        "rows": rows,
        "operator_answer": _operator_answer(summary),
        "policy": {
            "read_only": True,
            "can_trade": False,
            "can_promote": False,
            "can_demote": False,
            "requires_human_approval_for_roster_changes": True,
        },
        "can_trade": False,
        "can_promote": False,
    }


def publish_paper_lane_governor(
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
        "=== Paper lane governor ===",
        f"generated: {payload.get('generated_at')}",
        str(payload.get("operator_answer") or ""),
        (
            "summary: "
            f"{summary.get('total_lanes', 0)} lanes, "
            f"{summary.get('paper_roster', 0)} paper roster, "
            f"{summary.get('survivor_tournament', 0)} tournament, "
            f"{summary.get('probation_queue', 0)} probation, "
            f"{summary.get('demotion_queue', 0)} demote, "
            f"{summary.get('repair_queue', 0)} repair"
        ),
    ]
    for row in list(payload.get("rows", []))[:limit]:
        lines.append(
            f"  {row.get('governor_bucket', ''):<20} "
            f"{row.get('lane_id', ''):<42} "
            f"score {row.get('governor_score', 0):>5.1f} "
            f"{row.get('action', ''):<32} "
            f"{row.get('why', '')}"
        )
    lines.append("read-only: can_trade=false can_promote=false")
    return "\n".join(lines)


def _governor_row(
    source: Mapping[str, Any], *, config: PaperLaneGovernorConfig
) -> dict[str, Any]:
    survival_state = str(source.get("survival_state") or "")
    decision = str(source.get("decision") or "")
    closed = _int(source.get("closed_trades"))
    avg_bps = _maybe_float(source.get("avg_closed_trade_net_bps"))
    pf = _float(source.get("profit_factor"))
    score = _float(source.get("survival_score"))
    closed_net = _float(source.get("closed_net_pnl_usd"))
    action, bucket, why = _action_bucket(
        survival_state=survival_state,
        decision=decision,
        closed=closed,
        avg_bps=avg_bps,
        pf=pf,
        closed_net=closed_net,
        config=config,
    )
    autopsy = _autopsy(source, action=action, config=config)
    tournament = _tournament_status(source, bucket=bucket, config=config)
    return {
        "lane_id": source.get("lane_id"),
        "exchange": source.get("exchange"),
        "symbol": source.get("symbol"),
        "timeframe": source.get("timeframe"),
        "strategy_id": source.get("strategy_id"),
        "governor_bucket": bucket,
        "action": action,
        "why": why,
        "governor_score": round(_governor_score(source, action=action), 2),
        "survival_state": survival_state,
        "survival_score": round(score, 2),
        "survival_decision": decision,
        "closed_trades": closed,
        "closed_trades_needed": max(0, config.min_closed_trades - closed),
        "profit_factor": pf,
        "avg_closed_trade_net_bps": round(avg_bps, 4) if avg_bps is not None else None,
        "closed_net_pnl_usd": round(closed_net, 6),
        "fees_usd": round(_float(source.get("fees_usd")), 6),
        "live_signals": _int(source.get("live_signals")),
        "paper_order_intents": _int(source.get("paper_order_intents")),
        "exit_quality": source.get("exit_quality") if isinstance(source.get("exit_quality"), Mapping) else {},
        "blockers": list(source.get("blockers") or []),
        "autopsy": autopsy,
        "tournament": tournament,
        "latest_why_no_trade": source.get("latest_why_no_trade"),
        "can_trade": False,
        "can_promote": False,
    }


def _action_bucket(
    *,
    survival_state: str,
    decision: str,
    closed: int,
    avg_bps: float | None,
    pf: float,
    closed_net: float,
    config: PaperLaneGovernorConfig,
) -> tuple[str, str, str]:
    if decision == "REPAIR_LEDGER" or survival_state == "LEDGER_REPAIR_REQUIRED":
        return (
            ACTION_REPAIR_LEDGER,
            BUCKET_REPAIR_QUEUE,
            "ledger/fill pairing must be repaired before the lane can be judged",
        )
    if decision == "REPAIR_ROUTE_OR_CADENCE" or survival_state in {
        "STALE_NO_JUDGMENT",
        "ROUTE_BLOCKED",
    }:
        return (
            ACTION_REPAIR_ROUTE_OR_CADENCE,
            BUCKET_REPAIR_QUEUE,
            "route, heartbeat, or evaluation cadence is not trustworthy",
        )
    if decision == "DEMOTE_TO_SHADOW" or survival_state == "DEMOTE_TO_SHADOW":
        return (
            ACTION_DEMOTE_TO_SHADOW_RECOMMENDED,
            BUCKET_DEMOTION_QUEUE,
            "paper evidence is negative after costs; stop spending paper cycles here",
        )
    if survival_state == "RESEARCH_ONLY" or decision == "KEEP_RESEARCH_ONLY":
        return (
            ACTION_KEEP_RESEARCH_ONLY,
            BUCKET_RESEARCH_ONLY,
            "lane is blocked by prior negative evidence and needs fresh research proof",
        )
    if survival_state == "PAPER_PROBATION":
        return (
            ACTION_HOLD_PAPER_PROBATION,
            BUCKET_PROBATION_QUEUE,
            "negative under-sampled lane is held out of the active paper roster",
        )
    if survival_state == "NO_TRADE_EVIDENCE" or closed == 0:
        return (
            ACTION_WAIT_FOR_TRADE_EVIDENCE,
            BUCKET_NO_EVIDENCE,
            "lane has no closed paper trade sample yet",
        )
    if survival_state == "PAPER_SURVIVOR_CANDIDATE" or (
        closed >= config.min_closed_trades
        and closed_net > 0
        and pf >= config.min_profit_factor
        and (avg_bps or 0.0) >= config.min_avg_net_bps
    ):
        return (
            ACTION_KEEP_PAPER_SURVIVOR,
            BUCKET_SURVIVOR_TOURNAMENT,
            "lane clears sample, PF, and fee-wall survival gates; queue human review",
        )
    if closed_net >= 0:
        return (
            ACTION_EXTEND_PAPER_SAMPLE,
            BUCKET_PAPER_ROSTER,
            "lane is not dead, but still needs more sample or stronger capture",
        )
    if closed >= config.demote_after_negative_closed:
        return (
            ACTION_DEMOTE_TO_SHADOW_RECOMMENDED,
            BUCKET_DEMOTION_QUEUE,
            "negative lane crossed the governor demotion sample threshold",
        )
    return (
        ACTION_HOLD_PAPER_PROBATION,
        BUCKET_PROBATION_QUEUE,
        "negative but under-sampled; keep in shadow-observe until exit autopsy improves",
    )


def _autopsy(
    source: Mapping[str, Any],
    *,
    action: str,
    config: PaperLaneGovernorConfig,
) -> dict[str, Any]:
    avg_bps = _maybe_float(source.get("avg_closed_trade_net_bps"))
    pf = _float(source.get("profit_factor"))
    closed = _int(source.get("closed_trades"))
    closed_net = _float(source.get("closed_net_pnl_usd"))
    exit_quality = source.get("exit_quality") if isinstance(source.get("exit_quality"), Mapping) else {}
    blockers = [str(x) for x in source.get("blockers") or [] if str(x)]
    fee_wall_gap = (
        round(config.min_avg_net_bps - avg_bps, 4)
        if avg_bps is not None
        else None
    )
    entry_failure = (
        _first_matching(blockers, ("route", "stale", "no closed", "needs "))
        or str(source.get("latest_why_no_trade") or "")
        or "entry sample not sufficient"
    )
    exit_label = str(exit_quality.get("label") or "UNKNOWN_EXIT_QUALITY")
    if exit_label == "FEE_WALL_DRAG":
        exit_failure = "exits did not capture enough movement to pay fees/slippage"
    elif exit_label == "NO_EXIT_SAMPLE":
        exit_failure = "no closed exit sample exists yet"
    elif exit_label == "LEDGER_DRIFT":
        exit_failure = "fill ledger drift prevents exit judgment"
    elif exit_label == "UNDER_SAMPLED_CAPTURE":
        exit_failure = "capture exists but sample/PF/bps gates are not clear"
    else:
        exit_failure = "exit capture currently acceptable"
    return {
        "entry_failure_reason": entry_failure,
        "exit_failure_reason": exit_failure,
        "fee_wall_gap_bps": fee_wall_gap,
        "sample_gap": max(0, config.min_closed_trades - closed),
        "pf_gap": round(max(0.0, config.min_profit_factor - pf), 4),
        "avg_net_bps_gap": (
            round(max(0.0, config.min_avg_net_bps - avg_bps), 4)
            if avg_bps is not None
            else None
        ),
        "closed_net_pnl_usd": round(closed_net, 6),
        "primary_failure": _primary_failure(action, exit_label, blockers),
    }


def _tournament_status(
    source: Mapping[str, Any],
    *,
    bucket: str,
    config: PaperLaneGovernorConfig,
) -> dict[str, Any]:
    closed = _int(source.get("closed_trades"))
    avg_bps = _maybe_float(source.get("avg_closed_trade_net_bps"))
    pf = _float(source.get("profit_factor"))
    if bucket == BUCKET_SURVIVOR_TOURNAMENT:
        tier = "FINAL_REVIEW"
    elif bucket == BUCKET_PAPER_ROSTER and closed > 0:
        tier = "EXTEND_SAMPLE"
    elif bucket == BUCKET_NO_EVIDENCE:
        tier = "WAIT_FOR_FIRST_CLOSE"
    else:
        tier = "OUT_OF_TOURNAMENT"
    return {
        "tier": tier,
        "needs_closed_trades": max(0, config.min_closed_trades - closed),
        "needs_pf": round(max(0.0, config.min_profit_factor - pf), 4),
        "needs_avg_net_bps": (
            round(max(0.0, config.min_avg_net_bps - avg_bps), 4)
            if avg_bps is not None
            else config.min_avg_net_bps
        ),
    }


def _governor_score(source: Mapping[str, Any], *, action: str) -> float:
    base = _float(source.get("survival_score"))
    if action == ACTION_KEEP_PAPER_SURVIVOR:
        return min(100.0, base + 5.0)
    if action == ACTION_EXTEND_PAPER_SAMPLE:
        return min(90.0, base)
    if action == ACTION_WAIT_FOR_TRADE_EVIDENCE:
        return min(55.0, base)
    if action == ACTION_HOLD_PAPER_PROBATION:
        return min(38.0, base)
    if action in {ACTION_REPAIR_LEDGER, ACTION_REPAIR_ROUTE_OR_CADENCE}:
        return min(45.0, base)
    if action == ACTION_DEMOTE_TO_SHADOW_RECOMMENDED:
        return min(35.0, base)
    return min(40.0, base)


def _summary(rows: list[dict[str, Any]], *, config: PaperLaneGovernorConfig) -> dict[str, Any]:
    buckets = Counter(str(r.get("governor_bucket") or "") for r in rows)
    actions = Counter(str(r.get("action") or "") for r in rows)
    paper_roster = [
        r
        for r in rows
        if r.get("governor_bucket") in {BUCKET_PAPER_ROSTER, BUCKET_SURVIVOR_TOURNAMENT}
    ]
    return {
        "total_lanes": len(rows),
        "paper_roster": len(paper_roster),
        "survivor_tournament": buckets[BUCKET_SURVIVOR_TOURNAMENT],
        "demotion_queue": buckets[BUCKET_DEMOTION_QUEUE],
        "probation_queue": buckets[BUCKET_PROBATION_QUEUE],
        "repair_queue": buckets[BUCKET_REPAIR_QUEUE],
        "research_only": buckets[BUCKET_RESEARCH_ONLY],
        "no_evidence": buckets[BUCKET_NO_EVIDENCE],
        "proposed_paper_lanes": min(len(paper_roster), config.max_paper_roster),
        "excluded_from_active_roster": max(
            0, len(rows) - min(len(paper_roster), config.max_paper_roster)
        ),
        "closed_trades": sum(_int(r.get("closed_trades")) for r in rows),
        "closed_net_pnl_usd": round(
            sum(_float(r.get("closed_net_pnl_usd")) for r in rows), 6
        ),
        "fees_usd": round(sum(_float(r.get("fees_usd")) for r in rows), 6),
        "bucket_counts": dict(sorted(buckets.items())),
        "action_counts": dict(sorted(actions.items())),
        "can_trade": False,
        "can_promote": False,
    }


def _proposed_roster(
    rows: list[dict[str, Any]], *, config: PaperLaneGovernorConfig
) -> dict[str, Any]:
    paper = [
        _slim(r)
        for r in rows
        if r.get("governor_bucket") in {BUCKET_PAPER_ROSTER, BUCKET_SURVIVOR_TOURNAMENT}
    ][: max(1, int(config.max_paper_roster))]
    tournament = [
        _slim(r)
        for r in rows
        if r.get("governor_bucket") == BUCKET_SURVIVOR_TOURNAMENT
    ][: max(1, int(config.max_tournament_lanes))]
    return {
        "paper_lanes": paper,
        "survivor_tournament": tournament,
        "demote_to_shadow": [
            _slim(r) for r in rows if r.get("governor_bucket") == BUCKET_DEMOTION_QUEUE
        ],
        "probation_shadow_watch": [
            _slim(r) for r in rows if r.get("governor_bucket") == BUCKET_PROBATION_QUEUE
        ],
        "repair_first": [
            _slim(r) for r in rows if r.get("governor_bucket") == BUCKET_REPAIR_QUEUE
        ],
        "policy": "proposal_only_human_review_required",
    }


def _boards(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    return {
        "paper_roster": [
            _slim(r)
            for r in rows
            if r.get("governor_bucket") in {BUCKET_PAPER_ROSTER, BUCKET_SURVIVOR_TOURNAMENT}
        ],
        "survivor_tournament": [
            _slim(r) for r in rows if r.get("governor_bucket") == BUCKET_SURVIVOR_TOURNAMENT
        ],
        "demotion_queue": [
            _slim(r) for r in rows if r.get("governor_bucket") == BUCKET_DEMOTION_QUEUE
        ],
        "probation_queue": [
            _slim(r) for r in rows if r.get("governor_bucket") == BUCKET_PROBATION_QUEUE
        ],
        "repair_queue": [
            _slim(r) for r in rows if r.get("governor_bucket") == BUCKET_REPAIR_QUEUE
        ],
        "research_only": [
            _slim(r) for r in rows if r.get("governor_bucket") == BUCKET_RESEARCH_ONLY
        ],
    }


def _slim(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "lane_id": row.get("lane_id"),
        "exchange": row.get("exchange"),
        "symbol": row.get("symbol"),
        "timeframe": row.get("timeframe"),
        "strategy_id": row.get("strategy_id"),
        "governor_bucket": row.get("governor_bucket"),
        "action": row.get("action"),
        "governor_score": row.get("governor_score"),
        "closed_trades": row.get("closed_trades"),
        "closed_trades_needed": row.get("closed_trades_needed"),
        "closed_net_pnl_usd": row.get("closed_net_pnl_usd"),
        "profit_factor": row.get("profit_factor"),
        "avg_closed_trade_net_bps": row.get("avg_closed_trade_net_bps"),
        "why": row.get("why"),
    }


def _operator_answer(summary: Mapping[str, Any]) -> str:
    demote = _int(summary.get("demotion_queue"))
    probation = _int(summary.get("probation_queue"))
    repair = _int(summary.get("repair_queue"))
    tournament = _int(summary.get("survivor_tournament"))
    paper = _int(summary.get("paper_roster"))
    no_evidence = _int(summary.get("no_evidence"))
    if demote:
        return f"{demote} lane(s) are recommended for shadow demotion; stop expanding paper exposure there."
    if probation:
        return f"{probation} negative under-sampled lane(s) are held out of the active paper roster."
    if repair:
        return f"{repair} lane(s) need route/cadence/ledger repair before the paper roster can be trusted."
    if tournament:
        return f"{tournament} lane(s) are in survivor tournament for human review."
    if paper:
        return f"{paper} lane(s) stay in paper observation; none are promotable yet."
    if no_evidence:
        return f"{no_evidence} lane(s) need first closed trade evidence before judgment."
    return "No lane-governor evidence is available yet."


def _primary_failure(action: str, exit_label: str, blockers: list[str]) -> str:
    if action == ACTION_DEMOTE_TO_SHADOW_RECOMMENDED:
        return "negative_after_fee_wall"
    if action == ACTION_HOLD_PAPER_PROBATION:
        return "negative_paper_probation"
    if action == ACTION_REPAIR_LEDGER:
        return "ledger_drift"
    if action == ACTION_REPAIR_ROUTE_OR_CADENCE:
        return "route_or_cadence"
    if exit_label == "NO_EXIT_SAMPLE":
        return "no_exit_sample"
    if blockers:
        return blockers[0]
    return "none"


def _first_matching(items: list[str], needles: tuple[str, ...]) -> str:
    for item in items:
        lowered = item.lower()
        if any(n in lowered for n in needles):
            return item
    return ""


def _row_sort_key(row: Mapping[str, Any]) -> tuple[int, float, float, str]:
    priority = {
        BUCKET_DEMOTION_QUEUE: 0,
        BUCKET_PROBATION_QUEUE: 1,
        BUCKET_REPAIR_QUEUE: 2,
        BUCKET_SURVIVOR_TOURNAMENT: 3,
        BUCKET_PAPER_ROSTER: 4,
        BUCKET_NO_EVIDENCE: 5,
        BUCKET_RESEARCH_ONLY: 6,
    }.get(str(row.get("governor_bucket") or ""), 9)
    return (
        priority,
        -_float(row.get("governor_score")),
        -_float(row.get("closed_net_pnl_usd")),
        str(row.get("lane_id") or ""),
    )


def _read_json_payload(path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback
    return payload if isinstance(payload, dict) else fallback


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _maybe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--survival", type=Path, default=DEFAULT_SURVIVAL)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--feed", type=Path, default=DEFAULT_FEED)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval-seconds", type=float, default=60.0)
    parser.add_argument("--min-closed-trades", type=int, default=20)
    parser.add_argument("--min-profit-factor", type=float, default=1.5)
    parser.add_argument("--min-avg-net-bps", type=float, default=25.0)
    parser.add_argument("--demote-after-negative-closed", type=int, default=5)
    parser.add_argument("--max-paper-roster", type=int, default=18)
    parser.add_argument("--max-tournament-lanes", type=int, default=12)
    parser.add_argument("--max-rows", type=int, default=180)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    config = PaperLaneGovernorConfig(
        min_closed_trades=args.min_closed_trades,
        min_profit_factor=args.min_profit_factor,
        min_avg_net_bps=args.min_avg_net_bps,
        demote_after_negative_closed=args.demote_after_negative_closed,
        max_paper_roster=args.max_paper_roster,
        max_tournament_lanes=args.max_tournament_lanes,
        max_rows=args.max_rows,
    )
    while True:
        payload = build_paper_lane_governor(
            survival_path=args.survival,
            config=config,
        )
        publish_paper_lane_governor(payload, args.out, args.feed)
        print(render_report(payload), flush=True)
        if args.once:
            return 0
        time.sleep(max(1.0, float(args.interval_seconds)))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
