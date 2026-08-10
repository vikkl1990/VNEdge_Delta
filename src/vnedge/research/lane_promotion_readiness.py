"""Lane promotion readiness: one operator-facing truth table.

This module answers the question the raw dashboard counters cannot answer by
themselves: which lanes are merely configured, which are actually firing, and
which have enough live shadow evidence to deserve a paper-trial review?

It is read-only and conservative. A "paper review" label is not promotion, and
live readiness is always false here unless a future paper/live evidence reader
is explicitly added. The live ladder remains the authority.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from vnedge.research.shadow_manifest import load_shadow_manifest
from vnedge.research.shadow_perf_reader import (
    DEFAULT_JOURNAL_DIR,
    index_shadow_perf,
    read_shadow_perf,
    shadow_perf_key,
)

DEFAULT_RESEARCH_DIR = Path("research/live_research")
DEFAULT_OUT = DEFAULT_RESEARCH_DIR / "lane_promotion_readiness_latest.json"
DEFAULT_FEED = DEFAULT_RESEARCH_DIR / "lane_promotion_readiness_feed.jsonl"

STATUS_PAPER_REVIEW_READY = "PAPER_REVIEW_READY"
STATUS_SHADOW_COLLECTING = "SHADOW_COLLECTING"
STATUS_SHADOW_NOT_FIRING = "SHADOW_NOT_FIRING"
STATUS_SHADOW_NEGATIVE = "SHADOW_NEGATIVE"
STATUS_SHADOW_PF_TOO_LOW = "SHADOW_PF_TOO_LOW"
STATUS_REPLAY_NEEDS_ADAPTER = "REPLAY_POSITIVE_NEEDS_SHADOW_ADAPTER"
STATUS_BLOCKED = "BLOCKED"
STATUS_PAPER_ACTIVE = "PAPER_ACTIVE"
STATUS_PAPER_WAITING = "PAPER_WAITING_FOR_SIGNAL"

FUNNEL_RESEARCH = "RESEARCH"
FUNNEL_REPLAY = "REPLAY"
FUNNEL_UNTOUCHED = "UNTOUCHED_JUDGMENT"
FUNNEL_SHADOW = "SHADOW"
FUNNEL_PAPER = "PAPER"
FUNNEL_LIVE_SMALL = "LIVE_SMALL"

FUNNEL_STAGE_ORDER = (
    FUNNEL_RESEARCH,
    FUNNEL_REPLAY,
    FUNNEL_UNTOUCHED,
    FUNNEL_SHADOW,
    FUNNEL_PAPER,
    FUNNEL_LIVE_SMALL,
)

_FUNNEL_LABELS = {
    FUNNEL_RESEARCH: "Research",
    FUNNEL_REPLAY: "Fee-aware replay",
    FUNNEL_UNTOUCHED: "Untouched judgment",
    FUNNEL_SHADOW: "Shadow evidence",
    FUNNEL_PAPER: "Paper trial",
    FUNNEL_LIVE_SMALL: "Live-small",
}


@dataclass(frozen=True)
class ReadinessConfig:
    min_shadow_trades: int = 10
    min_shadow_span_days: float = 7.0
    min_shadow_profit_factor: float = 1.25
    min_shadow_net_usd: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_lane_promotion_readiness(
    *,
    research_dir: Path | str = DEFAULT_RESEARCH_DIR,
    journal_dir: Path | str = DEFAULT_JOURNAL_DIR,
    config: ReadinessConfig = ReadinessConfig(),
) -> dict[str, Any]:
    """Build a JSON-serializable readiness report from persisted artifacts."""
    research_dir = Path(research_dir)
    manifest = load_shadow_manifest(research_dir)
    shadow_perf = read_shadow_perf(journal_dir)
    shadow_index = index_shadow_perf(shadow_perf)

    rows: list[dict[str, Any]] = []
    rows.extend(
        _manifest_lane_row(lane, shadow_index, config)
        for lane in manifest.get("lanes", []) or []
        if isinstance(lane, Mapping)
    )
    rows.extend(
        _shadow_trial_row(trial)
        for trial in manifest.get("shadow_trials", []) or []
        if isinstance(trial, Mapping)
    )
    rows.extend(
        _blocked_row(blocked)
        for blocked in manifest.get("blocked", []) or []
        if isinstance(blocked, Mapping)
    )
    rows.extend(_paper_trial_rows(Path(journal_dir)))
    rows = [_attach_canonical_funnel(row) for row in rows]
    rows.sort(key=_row_sort_key)

    summary = _summary(rows)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "policy": {
            "status": "read_only_readiness",
            "can_trade": False,
            "can_promote": False,
            "paper_review_is_not_promotion": True,
            "live_ready_requires_completed_paper_trial_and_live_safety_gates": True,
        },
        "config": config.to_dict(),
        "inputs": {
            "research_dir": str(research_dir),
            "journal_dir": str(journal_dir),
            "shadow_manifest": str(research_dir / "shadow_lanes.json"),
        },
        "summary": summary,
        "canonical_funnel": _canonical_funnel(rows),
        "rows": rows,
        "shadow_perf": {
            "available": bool(shadow_perf.get("available")),
            "journals_read": int(shadow_perf.get("journals_read") or 0),
        },
        "operator_answer": _operator_answer(summary),
        "can_trade": False,
        "can_promote": False,
    }


def publish_readiness(payload: Mapping[str, Any], out: Path, feed: Path | None = None) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    tmp.replace(out)
    if feed is not None:
        feed.parent.mkdir(parents=True, exist_ok=True)
        with open(feed, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, default=str) + "\n")


def render_report(payload: Mapping[str, Any], *, limit: int = 30) -> str:
    summary = payload.get("summary", {})
    lines = [
        "=== Lane promotion readiness ===",
        f"generated: {payload.get('generated_at')}",
        str(payload.get("operator_answer") or ""),
        (
            "summary: "
            f"{summary.get('total_rows', 0)} rows, "
            f"{summary.get('paper_active', 0)} paper-active, "
            f"{summary.get('shadow_firing', 0)} firing, "
            f"{summary.get('paper_review_ready', 0)} paper-review candidates, "
            f"{summary.get('live_ready', 0)} live-ready"
        ),
    ]
    funnel = payload.get("canonical_funnel") or []
    if funnel:
        lines.append("funnel: " + " → ".join(
            f"{stage.get('stage')}={stage.get('count', 0)}"
            for stage in funnel
        ))
    for row in list(payload.get("rows", []))[:limit]:
        evidence = row.get("evidence", {})
        funnel_state = row.get("funnel", {}).get("state", row.get("status", "UNKNOWN"))
        trades = evidence.get("virtual_trades", evidence.get("paper_order_intents", 0))
        net = evidence.get("net_usd", evidence.get("realized_pnl_usd"))
        lines.append(
            f"  {funnel_state:<38} "
            f"{row.get('exchange', '')} {row.get('symbol', '')} "
            f"{row.get('strategy_id') or row.get('family') or row.get('source', '')} "
            f"trades={trades} "
            f"net={net}"
        )
    lines.append("read-only: can_trade=false can_promote=false")
    return "\n".join(lines)


def _manifest_lane_row(
    lane: Mapping[str, Any],
    shadow_index: Mapping[str, dict],
    config: ReadinessConfig,
) -> dict[str, Any]:
    strategy = str(lane.get("strategy_id") or "")
    exchange = str(lane.get("exchange") or "")
    symbol = str(lane.get("symbol") or "")
    perf = shadow_index.get(shadow_perf_key(strategy, exchange, symbol))
    status, blockers = _shadow_status(perf, config)
    evidence = _shadow_evidence(perf)
    triage = _shadow_triage(status, evidence, blockers, config)
    paper_review_ready = status == STATUS_PAPER_REVIEW_READY
    return {
        "row_type": "runtime_shadow_lane",
        "lane_id": lane.get("lane_id"),
        "exchange": exchange,
        "symbol": symbol,
        "timeframe": str(lane.get("timeframe") or "1h"),
        "strategy_id": strategy,
        "mode": str(lane.get("mode") or "shadow"),
        "status": status,
        "paper_review_ready": paper_review_ready,
        "live_ready": False,
        "can_trade": False,
        "can_promote": False,
        "evidence": evidence,
        "blockers": blockers,
        "triage": triage,
        "next_action": (
            "open human paper-trial approval review"
            if paper_review_ready
            else triage["action"]
        ),
        "live_blockers": [
            "paper trial not completed",
            "pre-live checklist not cleared",
            "three live gates not open",
            "real execution adapter not mounted by this report",
        ],
    }


def _shadow_trial_row(trial: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "row_type": "filtered_replay_shadow_trial",
        "trial_id": trial.get("trial_id"),
        "candidate_id": trial.get("candidate_id"),
        "source": trial.get("source"),
        "family": trial.get("family"),
        "exchange": trial.get("exchange"),
        "symbol": trial.get("symbol"),
        "timeframe": trial.get("timeframe"),
        "mode": "shadow_trial",
        "status": STATUS_REPLAY_NEEDS_ADAPTER,
        "paper_review_ready": False,
        "live_ready": False,
        "can_trade": False,
        "can_promote": False,
        "evidence": {"filtered_replay": trial.get("replay", {})},
        "blockers": [
            "replay-positive event has no runtime shadow adapter",
            "no live shadow outcomes yet",
            "paper trial not approved",
        ],
        "triage": {
            "bucket": "REPLAY_POSITIVE_NEEDS_ADAPTER",
            "priority": "build",
            "action": "build runtime shadow adapter, then collect live outcomes",
            "rationale": "execution replay is promising but not connected to live shadow evidence",
        },
        "next_action": trial.get("next_action")
        or "build runtime shadow adapter, then collect live shadow outcomes",
    }


def _blocked_row(blocked: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "row_type": "blocked_manifest_candidate",
        "exchange": blocked.get("exchange"),
        "symbol": blocked.get("symbol"),
        "strategy_id": blocked.get("strategy_id"),
        "mode": "blocked",
        "status": STATUS_BLOCKED,
        "paper_review_ready": False,
        "live_ready": False,
        "can_trade": False,
        "can_promote": False,
        "evidence": {"latest_judgment": blocked.get("latest_judgment")},
        "blockers": [str(blocked.get("reason") or "blocked by manifest")],
        "triage": {
            "bucket": "MANIFEST_BLOCKED",
            "priority": "blocked",
            "action": "resolve blocker through a fresh approved judgment",
            "rationale": "blocked manifest candidates are not eligible for elevation",
        },
        "next_action": "resolve blocker with a fresh approved judgment or locked runtime params",
    }


def _paper_trial_rows(journal_dir: Path) -> list[dict[str, Any]]:
    """Summarize approved live-paper lanes from runtime journals."""
    if not journal_dir.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(journal_dir.glob("*.journal.jsonl")):
        if path.name.endswith("_shadow.journal.jsonl"):
            continue
        evidence = _paper_evidence(path)
        if evidence is None:
            continue
        status = (
            STATUS_PAPER_ACTIVE
            if int(evidence.get("paper_order_intents") or 0) > 0
            else STATUS_PAPER_WAITING
        )
        triage = _paper_triage(status, evidence)
        rows.append({
            "row_type": "paper_trial_lane",
            "lane_id": path.name.removesuffix(".journal.jsonl"),
            "trial_id": path.name.removesuffix(".journal.jsonl"),
            "journal": str(path),
            "exchange": evidence.get("exchange") or "",
            "symbol": evidence.get("symbol") or "",
            "timeframe": evidence.get("timeframe") or "1h",
            "strategy_id": evidence.get("strategy_id") or "",
            "mode": "paper",
            "status": status,
            "paper_active": status == STATUS_PAPER_ACTIVE,
            "paper_review_ready": False,
            "live_ready": False,
            "can_trade": False,
            "can_promote": False,
            "evidence": evidence,
            "blockers": _paper_blockers(evidence),
            "triage": triage,
            "next_action": (
                triage["action"]
                if status == STATUS_PAPER_ACTIVE
                else triage["action"]
            ),
            "live_blockers": [
                "paper trial verdict not complete",
                "pre-live checklist not cleared",
                "three live gates not open",
            ],
        })
    return rows


def _paper_evidence(path: Path) -> dict[str, Any] | None:
    counters = {
        "evals": 0,
        "live_signals": 0,
        "risk_decisions": 0,
        "paper_order_intents": 0,
        "paper_order_acknowledged": 0,
        "paper_exits": 0,
        "paper_reports": 0,
    }
    active_since_ts: str | None = None
    latest_eval_ts: str | None = None
    latest_bar_ts: str | None = None
    latest_signal_reason: str | None = None
    latest_order: dict[str, Any] | None = None
    latest_exit: dict[str, Any] | None = None
    latest_report: dict[str, Any] | None = None
    symbol = ""
    strategy_id = ""
    exchange = ""

    for record in _iter_jsonl(path):
        ts = str(record.get("ts") or "")
        if ts and active_since_ts is None:
            active_since_ts = ts
        kind = str(record.get("kind") or "")
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            payload = {}
        if kind == "lane_eval":
            counters["evals"] += 1
            latest_eval_ts = ts or latest_eval_ts
            latest_bar_ts = str(payload.get("bar_ts") or latest_bar_ts or "")
            symbol = str(payload.get("symbol") or symbol)
            strategy_id = str(payload.get("strategy_id") or strategy_id)
            exchange = str(payload.get("exchange") or exchange)
            if bool(payload.get("fired")) and not bool(payload.get("backfill")):
                counters["live_signals"] += 1
                latest_signal_reason = str(payload.get("signal_reason") or "")
        elif kind == "risk_decision":
            counters["risk_decisions"] += 1
        elif kind == "order_intent":
            counters["paper_order_intents"] += 1
            latest_order = dict(payload)
            intent = payload.get("intent")
            if isinstance(intent, Mapping):
                symbol = str(intent.get("symbol") or symbol)
                strategy_id = str(intent.get("strategy_id") or strategy_id)
        elif kind == "order_acknowledged":
            counters["paper_order_acknowledged"] += 1
        elif kind == "live_paper_exit":
            counters["paper_exits"] += 1
            latest_exit = dict(payload)
        elif kind == "live_paper_report":
            counters["paper_reports"] += 1
            latest_report = dict(payload.get("report") or payload)
            symbol = str(latest_report.get("symbol") or symbol)
            strategy_id = str(latest_report.get("strategy_id") or strategy_id)

    if not any(counters.values()):
        return None
    if counters["evals"] == 0 and counters["paper_order_intents"] == 0 and latest_report is None:
        return None

    report = latest_report or {}
    return {
        **counters,
        "active_since_ts": active_since_ts,
        "latest_eval_ts": latest_eval_ts,
        "latest_bar_ts": latest_bar_ts or None,
        "latest_signal_reason": latest_signal_reason,
        "latest_order": _compact_paper_order(latest_order),
        "latest_exit": latest_exit,
        "latest_report": report,
        "symbol": symbol or report.get("symbol") or "",
        "strategy_id": strategy_id or report.get("strategy_id") or "",
        "exchange": exchange,
        "timeframe": "1h",
        "orders_submitted": report.get("orders_submitted"),
        "fills": report.get("fills"),
        "realized_pnl_usd": report.get("realized_pnl_usd"),
        "unrealized_pnl_usd": report.get("unrealized_pnl_usd"),
        "final_equity_usd": report.get("final_equity_usd"),
        "risk_rejects": report.get("risk_rejects"),
    }


def _compact_paper_order(payload: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(payload, Mapping):
        return None
    intent = payload.get("intent")
    if not isinstance(intent, Mapping):
        return {"intent_key": payload.get("intent_key")}
    return {
        "intent_key": payload.get("intent_key"),
        "client_order_id": payload.get("client_order_id"),
        "symbol": intent.get("symbol"),
        "side": intent.get("side"),
        "quantity": intent.get("quantity"),
        "reduce_only": intent.get("reduce_only"),
        "strategy_id": intent.get("strategy_id"),
        "order_type": intent.get("order_type"),
    }


def _paper_blockers(evidence: Mapping[str, Any]) -> list[str]:
    blockers = [
        "paper status is visibility only, not promotion",
        "live promotion still requires completed paper verdict and live ladder gates",
    ]
    if int(evidence.get("paper_order_intents") or 0) == 0:
        blockers.append("no paper order intents yet; waiting for the next approved signal")
    return blockers


def _shadow_status(
    perf: Mapping[str, Any] | None,
    config: ReadinessConfig,
) -> tuple[str, list[str]]:
    if not perf:
        return STATUS_SHADOW_NOT_FIRING, [
            "no resolved shadow_outcome records for this lane",
            "cannot discuss paper/live until the lane fires and outcomes settle",
        ]
    trades = int(perf.get("virtual_trades") or 0)
    span_days = float(perf.get("span_days") or 0.0)
    net = float(perf.get("net_usd") or 0.0)
    profit_factor = _optional_float(perf.get("profit_factor"))
    blockers: list[str] = []
    if trades < config.min_shadow_trades:
        blockers.append(
            f"shadow trades too few: {trades} < {config.min_shadow_trades}"
        )
    if span_days < config.min_shadow_span_days:
        blockers.append(
            f"shadow span too short: {span_days:g}d < {config.min_shadow_span_days:g}d"
        )
    if net <= config.min_shadow_net_usd:
        blockers.append(f"shadow net not positive after costs: ${net:.2f}")
    if profit_factor is None:
        blockers.append("shadow profit factor missing/undefined")
    elif profit_factor < config.min_shadow_profit_factor:
        blockers.append(
            f"shadow profit factor too low: {profit_factor:.2f} "
            f"< {config.min_shadow_profit_factor:.2f}"
        )
    if not blockers:
        return STATUS_PAPER_REVIEW_READY, []
    if trades == 0:
        return STATUS_SHADOW_NOT_FIRING, blockers
    if net <= config.min_shadow_net_usd:
        return STATUS_SHADOW_NEGATIVE, blockers
    if profit_factor is not None and profit_factor < config.min_shadow_profit_factor:
        return STATUS_SHADOW_PF_TOO_LOW, blockers
    return STATUS_SHADOW_COLLECTING, blockers


def _shadow_evidence(perf: Mapping[str, Any] | None) -> dict[str, Any]:
    if not perf:
        return {
            "virtual_trades": 0,
            "wins": 0,
            "net_usd": 0.0,
            "profit_factor": None,
            "span_days": 0.0,
        }
    return {
        "virtual_trades": int(perf.get("virtual_trades") or 0),
        "wins": int(perf.get("wins") or 0),
        "win_rate_pct": perf.get("win_rate_pct"),
        "net_usd": perf.get("net_usd"),
        "profit_factor": perf.get("profit_factor"),
        "span_days": perf.get("span_days"),
        "last_resolution_ts": perf.get("last_resolution_ts"),
        "resolutions": perf.get("resolutions", {}),
        "source_journals": perf.get("source_journals", []),
    }


def _shadow_triage(
    status: str,
    evidence: Mapping[str, Any],
    blockers: list[str],
    config: ReadinessConfig,
) -> dict[str, str]:
    trades = int(evidence.get("virtual_trades") or 0)
    span_days = float(evidence.get("span_days") or 0.0)
    net = float(evidence.get("net_usd") or 0.0)
    profit_factor = _optional_float(evidence.get("profit_factor"))
    resolutions = evidence.get("resolutions") or {}
    stops = int(resolutions.get("stop") or 0) if isinstance(resolutions, Mapping) else 0
    targets = (
        int(resolutions.get("target") or 0)
        if isinstance(resolutions, Mapping)
        else 0
    )
    timeouts = (
        int(resolutions.get("timeout") or 0)
        if isinstance(resolutions, Mapping)
        else 0
    )

    if status == STATUS_PAPER_REVIEW_READY:
        return {
            "bucket": "PAPER_REVIEW_CANDIDATE",
            "priority": "review",
            "action": "open human paper-trial approval review",
            "rationale": "shadow evidence clears sample, span, net, and profit-factor gates",
        }
    if trades == 0:
        return {
            "bucket": "NO_LIVE_OUTCOMES",
            "priority": "observe",
            "action": "keep shadow lane running; inspect realtime scanner if it remains silent",
            "rationale": "no resolved shadow outcomes exist yet",
        }
    if net <= config.min_shadow_net_usd and (
        trades >= config.min_shadow_trades or span_days >= config.min_shadow_span_days
    ):
        return {
            "bucket": "MATURE_NEGATIVE_EDGE",
            "priority": "refactor_or_disable",
            "action": (
                "disable paper promotion and refactor entry/exit before more "
                "runtime exposure"
            ),
            "rationale": "negative after costs despite enough shadow sample or time span",
        }
    if net <= config.min_shadow_net_usd and stops >= max(1, targets + timeouts):
        return {
            "bucket": "EARLY_STOP_DOMINATED",
            "priority": "refactor",
            "action": "tighten candidate quality or exit geometry before promotion discussion",
            "rationale": (
                "early outcomes are stop-dominated: "
                f"stops={stops}, targets={targets}, timeouts={timeouts}"
            ),
        }
    if net <= config.min_shadow_net_usd:
        return {
            "bucket": "EARLY_NEGATIVE_SAMPLE",
            "priority": "observe",
            "action": "collect the minimum shadow sample, but keep paper/live blocked",
            "rationale": "negative so far, but sample/span gates are still open",
        }
    if profit_factor is not None and profit_factor < config.min_shadow_profit_factor:
        return {
            "bucket": "POSITIVE_LOW_PROFIT_FACTOR",
            "priority": "refactor",
            "action": "raise trade quality or improve payoff before paper review",
            "rationale": "net is positive but gross losses are too large for the PF gate",
        }
    if blockers:
        return {
            "bucket": "COLLECT_MORE_EVIDENCE",
            "priority": "observe",
            "action": "keep collecting shadow outcomes until sample/span gates are satisfied",
            "rationale": "; ".join(blockers[:2]),
        }
    return {
        "bucket": "UNKNOWN_REVIEW",
        "priority": "inspect",
        "action": "inspect shadow evidence manually",
        "rationale": "status/blocker combination was not recognized",
    }


def _paper_triage(status: str, evidence: Mapping[str, Any]) -> dict[str, str]:
    if status == STATUS_PAPER_ACTIVE:
        return {
            "bucket": "APPROVED_PAPER_RUNNING",
            "priority": "observe",
            "action": (
                "keep approved paper trial running and judge only against its "
                "locked manifest"
            ),
            "rationale": "paper order activity exists, but paper status is visibility only",
        }
    return {
        "bucket": "APPROVED_PAPER_WAITING",
        "priority": "observe",
        "action": "keep approved paper trial online until the next valid signal",
        "rationale": "approved paper lane has not submitted an order intent yet",
    }


def _attach_canonical_funnel(row: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(row)
    stage, state = _canonical_stage_state(out)
    primary_blocker = _primary_blocker(out)
    next_stage = _next_funnel_stage(stage, state)
    out["canonical_status"] = f"{stage}:{state}"
    out["primary_blocker"] = primary_blocker
    out["funnel"] = {
        "stage": stage,
        "stage_label": _FUNNEL_LABELS.get(stage, stage.replace("_", " ").title()),
        "state": state,
        "rank": _funnel_rank(stage, state),
        "primary_blocker": primary_blocker,
        "next_stage": next_stage,
        "next_stage_label": _FUNNEL_LABELS.get(next_stage, ""),
        "promotion_path": list(FUNNEL_STAGE_ORDER),
        "trade_blocked": not bool(out.get("can_trade")),
        "promotion_blocked": not bool(out.get("can_promote")),
    }
    return out


def _canonical_stage_state(row: Mapping[str, Any]) -> tuple[str, str]:
    status = str(row.get("status") or "")
    row_type = str(row.get("row_type") or "")
    if bool(row.get("live_ready")):
        return FUNNEL_LIVE_SMALL, "READY_FOR_LIVE_SMALL"
    if row_type == "paper_trial_lane":
        if status == STATUS_PAPER_ACTIVE:
            return FUNNEL_PAPER, "PAPER_RUNNING"
        return FUNNEL_PAPER, "PAPER_WAITING_FOR_SIGNAL"
    if status == STATUS_PAPER_REVIEW_READY:
        return FUNNEL_SHADOW, "READY_FOR_PAPER_REVIEW"
    if row_type == "runtime_shadow_lane":
        if status == STATUS_SHADOW_NOT_FIRING:
            return FUNNEL_SHADOW, "WAITING_FOR_OUTCOMES"
        if status == STATUS_SHADOW_COLLECTING:
            return FUNNEL_SHADOW, "COLLECTING_EVIDENCE"
        if status == STATUS_SHADOW_PF_TOO_LOW:
            return FUNNEL_SHADOW, "QUALITY_BLOCKED"
        if status == STATUS_SHADOW_NEGATIVE:
            return FUNNEL_SHADOW, "NEGATIVE_EDGE_BLOCKED"
        return FUNNEL_SHADOW, status or "UNKNOWN_SHADOW"
    if row_type == "filtered_replay_shadow_trial":
        return FUNNEL_REPLAY, "NEEDS_SHADOW_ADAPTER"
    if row_type == "blocked_manifest_candidate":
        return FUNNEL_RESEARCH, "BLOCKED"
    return FUNNEL_RESEARCH, status or "UNKNOWN"


def _primary_blocker(row: Mapping[str, Any]) -> str:
    blockers = row.get("blockers")
    if isinstance(blockers, list):
        for blocker in blockers:
            if blocker:
                return str(blocker)
    triage = row.get("triage")
    if isinstance(triage, Mapping) and triage.get("rationale"):
        return str(triage["rationale"])
    if row.get("next_action"):
        return str(row["next_action"])
    return "no blocker recorded"


def _next_funnel_stage(stage: str, state: str) -> str | None:
    if stage == FUNNEL_RESEARCH:
        return FUNNEL_REPLAY
    if stage == FUNNEL_REPLAY:
        return FUNNEL_SHADOW if state == "NEEDS_SHADOW_ADAPTER" else FUNNEL_UNTOUCHED
    if stage == FUNNEL_UNTOUCHED:
        return FUNNEL_SHADOW
    if stage == FUNNEL_SHADOW:
        return FUNNEL_PAPER if state == "READY_FOR_PAPER_REVIEW" else FUNNEL_SHADOW
    if stage == FUNNEL_PAPER:
        return FUNNEL_LIVE_SMALL
    return None


def _funnel_rank(stage: str, state: str) -> int:
    stage_base = {
        FUNNEL_PAPER: 0,
        FUNNEL_SHADOW: 10,
        FUNNEL_REPLAY: 20,
        FUNNEL_UNTOUCHED: 30,
        FUNNEL_RESEARCH: 40,
        FUNNEL_LIVE_SMALL: 50,
    }.get(stage, 90)
    state_rank = {
        "PAPER_RUNNING": 0,
        "PAPER_WAITING_FOR_SIGNAL": 1,
        "READY_FOR_PAPER_REVIEW": 2,
        "COLLECTING_EVIDENCE": 3,
        "QUALITY_BLOCKED": 4,
        "NEGATIVE_EDGE_BLOCKED": 5,
        "WAITING_FOR_OUTCOMES": 6,
        "NEEDS_SHADOW_ADAPTER": 7,
        "BLOCKED": 8,
    }.get(state, 9)
    return stage_base + state_rank


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    statuses: dict[str, int] = {}
    funnel_stages: dict[str, int] = {}
    funnel_states: dict[str, int] = {}
    for row in rows:
        status = str(row.get("status") or "UNKNOWN")
        statuses[status] = statuses.get(status, 0) + 1
        funnel = row.get("funnel") or {}
        stage = str(funnel.get("stage") or "UNKNOWN")
        state = str(funnel.get("state") or "UNKNOWN")
        funnel_stages[stage] = funnel_stages.get(stage, 0) + 1
        funnel_states[state] = funnel_states.get(state, 0) + 1
    return {
        "total_rows": len(rows),
        "runtime_shadow_lanes": sum(
            1 for row in rows if row.get("row_type") == "runtime_shadow_lane"
        ),
        "filtered_replay_shadow_trials": sum(
            1 for row in rows if row.get("row_type") == "filtered_replay_shadow_trial"
        ),
        "blocked": statuses.get(STATUS_BLOCKED, 0),
        "paper_runtime_lanes": sum(
            1 for row in rows if row.get("row_type") == "paper_trial_lane"
        ),
        "paper_active": sum(1 for row in rows if row.get("paper_active")),
        "paper_order_intents": sum(
            int(row.get("evidence", {}).get("paper_order_intents") or 0)
            for row in rows
            if row.get("row_type") == "paper_trial_lane"
        ),
        "paper_exits": sum(
            int(row.get("evidence", {}).get("paper_exits") or 0)
            for row in rows
            if row.get("row_type") == "paper_trial_lane"
        ),
        "shadow_firing": sum(
            1
            for row in rows
            if row.get("row_type") == "runtime_shadow_lane"
            and int(row.get("evidence", {}).get("virtual_trades") or 0) > 0
        ),
        "shadow_not_firing": statuses.get(STATUS_SHADOW_NOT_FIRING, 0),
        "shadow_negative": statuses.get(STATUS_SHADOW_NEGATIVE, 0),
        "triage_counts": _triage_counts(rows),
        "paper_review_ready": sum(1 for row in rows if row.get("paper_review_ready")),
        "live_ready": sum(1 for row in rows if row.get("live_ready")),
        "status_counts": statuses,
        "funnel_stage_counts": {
            stage: funnel_stages.get(stage, 0) for stage in FUNNEL_STAGE_ORDER
        },
        "funnel_state_counts": dict(
            sorted(funnel_states.items(), key=lambda kv: (-kv[1], kv[0]))
        ),
        "primary_blocker_counts": _primary_blocker_counts(rows),
    }


def _operator_answer(summary: Mapping[str, Any]) -> str:
    paper = int(summary.get("paper_review_ready") or 0)
    live = int(summary.get("live_ready") or 0)
    firing = int(summary.get("shadow_firing") or 0)
    not_firing = int(summary.get("shadow_not_firing") or 0)
    trials = int(summary.get("filtered_replay_shadow_trials") or 0)
    paper_active = int(summary.get("paper_active") or 0)
    paper_orders = int(summary.get("paper_order_intents") or 0)
    paper_exits = int(summary.get("paper_exits") or 0)
    return (
        f"{paper_active} approved paper lane(s) active "
        f"({paper_orders} order intents, {paper_exits} exits), "
        f"{paper} lane(s) are paper-review ready, {live} lane(s) are live-ready, "
        f"{firing} runtime shadow lane(s) are firing, {not_firing} are not firing, "
        f"and {trials} replay-positive trial(s) still need runtime adapters."
    )


def _triage_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        triage = row.get("triage")
        if not isinstance(triage, Mapping):
            continue
        bucket = str(triage.get("bucket") or "")
        if not bucket:
            continue
        counts[bucket] = counts.get(bucket, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def _primary_blocker_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        blocker = str(row.get("primary_blocker") or "")
        if not blocker or blocker == "no blocker recorded":
            continue
        counts[blocker] = counts.get(blocker, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:12])


def _canonical_funnel(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for stage in FUNNEL_STAGE_ORDER:
        stage_rows = [
            row
            for row in rows
            if isinstance(row.get("funnel"), Mapping)
            and row["funnel"].get("stage") == stage
        ]
        if not stage_rows:
            out.append({
                "stage": stage,
                "label": _FUNNEL_LABELS.get(stage, stage),
                "count": 0,
                "state_counts": {},
                "top_blockers": [],
                "next_action": "no rows currently parked here",
            })
            continue
        state_counts: dict[str, int] = {}
        blocker_counts: dict[str, int] = {}
        for row in stage_rows:
            state = str(row.get("funnel", {}).get("state") or "UNKNOWN")
            state_counts[state] = state_counts.get(state, 0) + 1
            blocker = str(row.get("primary_blocker") or "")
            if blocker and blocker != "no blocker recorded":
                blocker_counts[blocker] = blocker_counts.get(blocker, 0) + 1
        top_blockers = [
            {"blocker": blocker, "count": count}
            for blocker, count in sorted(
                blocker_counts.items(), key=lambda kv: (-kv[1], kv[0])
            )[:3]
        ]
        out.append({
            "stage": stage,
            "label": _FUNNEL_LABELS.get(stage, stage),
            "count": len(stage_rows),
            "state_counts": dict(sorted(state_counts.items())),
            "top_blockers": top_blockers,
            "next_action": _stage_next_action(stage, stage_rows),
        })
    return out


def _stage_next_action(stage: str, rows: list[dict[str, Any]]) -> str:
    if stage == FUNNEL_PAPER:
        return "observe approved paper trials; judge only against locked manifests"
    if stage == FUNNEL_SHADOW:
        ready = sum(1 for row in rows if row.get("paper_review_ready"))
        if ready:
            return f"review {ready} paper candidate(s) for human-approved trial"
        return "collect/refactor shadow outcomes before paper promotion"
    if stage == FUNNEL_REPLAY:
        return "build runtime shadow adapters for replay-positive ideas"
    if stage == FUNNEL_UNTOUCHED:
        return "run only pre-registered untouched-window judgments"
    if stage == FUNNEL_RESEARCH:
        return "resolve blockers or archive rejected candidates"
    return "keep live locked until paper verdict and live gates clear"


def _row_sort_key(row: Mapping[str, Any]) -> tuple[int, str, str, str]:
    rank = int(row.get("funnel", {}).get("rank") or {
        STATUS_PAPER_ACTIVE: 0,
        STATUS_PAPER_WAITING: 1,
        STATUS_PAPER_REVIEW_READY: 2,
        STATUS_SHADOW_COLLECTING: 3,
        STATUS_SHADOW_PF_TOO_LOW: 4,
        STATUS_SHADOW_NEGATIVE: 5,
        STATUS_SHADOW_NOT_FIRING: 6,
        STATUS_REPLAY_NEEDS_ADAPTER: 7,
        STATUS_BLOCKED: 8,
    }.get(str(row.get("status") or ""), 9))
    return (
        rank,
        str(row.get("exchange") or ""),
        str(row.get("symbol") or ""),
        str(row.get("lane_id") or row.get("trial_id") or row.get("strategy_id") or ""),
    )


def _optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _iter_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open() as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    rows.append(record)
    except OSError:
        return []
    return rows


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish lane promotion readiness")
    parser.add_argument("--research-dir", default=str(DEFAULT_RESEARCH_DIR))
    parser.add_argument("--journal-dir", default=str(DEFAULT_JOURNAL_DIR))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--feed", default=str(DEFAULT_FEED))
    parser.add_argument("--min-shadow-trades", type=int, default=10)
    parser.add_argument("--min-shadow-span-days", type=float, default=7.0)
    parser.add_argument("--min-shadow-profit-factor", type=float, default=1.25)
    parser.add_argument("--interval-seconds", type=int, default=0)
    parser.add_argument("--no-publish", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--limit", type=int, default=30)
    return parser.parse_args(argv)


def _run_once(args: argparse.Namespace) -> dict[str, Any]:
    payload = build_lane_promotion_readiness(
        research_dir=args.research_dir,
        journal_dir=args.journal_dir,
        config=ReadinessConfig(
            min_shadow_trades=args.min_shadow_trades,
            min_shadow_span_days=args.min_shadow_span_days,
            min_shadow_profit_factor=args.min_shadow_profit_factor,
        ),
    )
    if not args.no_publish:
        publish_readiness(payload, Path(args.out), Path(args.feed) if args.feed else None)
    if args.json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(render_report(payload, limit=args.limit))
    return payload


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    while True:
        _run_once(args)
        if args.interval_seconds <= 0:
            break
        time.sleep(args.interval_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
