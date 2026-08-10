"""Paper promotion bridge.

This read-only artifact gives the operator one conservative answer for each
paper/shadow lane: is it ready for human paper/live review, or which proof is
missing first?

It deliberately does not promote anything. It joins the existing truth boards
that were previously inspected separately: lane readiness, paper performance,
paper trade contract reconciliation, maker quote lifecycle, and operator
actions.
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
DEFAULT_READINESS = DEFAULT_RESEARCH_DIR / "lane_promotion_readiness_latest.json"
DEFAULT_PERFORMANCE = DEFAULT_RESEARCH_DIR / "paper_lane_performance_latest.json"
DEFAULT_CONTRACT = DEFAULT_RESEARCH_DIR / "paper_trade_contract_reconciler_latest.json"
DEFAULT_MAKER_QUOTE = DEFAULT_RESEARCH_DIR / "maker_quote_lifecycle_latest.json"
DEFAULT_ACTIONS = DEFAULT_RESEARCH_DIR / "operator_actions_latest.json"
DEFAULT_OUT = DEFAULT_RESEARCH_DIR / "paper_promotion_bridge_latest.json"
DEFAULT_FEED = DEFAULT_RESEARCH_DIR / "paper_promotion_bridge_feed.jsonl"

DECISION_REPAIR_FIRST = "REPAIR_FIRST"
DECISION_EXECUTION_PROOF_MISSING = "EXECUTION_PROOF_MISSING"
DECISION_CONTRACT_PROOF_MISSING = "CONTRACT_PROOF_MISSING"
DECISION_MINE_ALPHA = "MINE_CLEAN_ALPHA"
DECISION_COLLECT_SAMPLE = "COLLECT_PAPER_SAMPLE"
DECISION_WAIT_SIGNAL = "WAIT_FOR_SIGNAL"
DECISION_SHADOW_READY = "SHADOW_READY_NEEDS_PAPER_APPROVAL"
DECISION_PAPER_REVIEW_READY = "PAPER_REVIEW_READY"
DECISION_OBSERVE = "OBSERVE"

_REPAIR_ACTIONS = {
    "REPAIR_ROUTE",
    "RESTORE_CADENCE",
    "FIX_SIZE_PROFILE",
    "REPAIR_PAPER_CONTRACT",
    "FIX_EXIT_QUALITY",
}
_CONTRACT_GOOD = {"CONTRACT_OK_PROFITABLE"}
_CONTRACT_BAD = {"CONTRACT_BROKEN", "FEE_WALL_BREACH"}
_CONTRACT_ALPHA = {"CONTRACT_OK_NEGATIVE_ALPHA", "CONTRACT_OK_EDGE_DEFICIT"}
_QUOTE_GOOD = {"QUOTE_LIFECYCLE_PAPER_REVIEW", "MAKER_OBSERVED_SHADOW_READY"}
_QUOTE_BAD = {
    "NO_QUOTE_LIFECYCLE_WIRING",
    "MAKER_ROUTE_BLOCKED",
    "MAKER_FILL_UNPROVEN",
    "TAKER_FALLBACK_FORBIDDEN",
    "NEGATIVE_AFTER_EXECUTION",
    "TIMEOUT_UNKNOWN_FAIL_CLOSED",
}


@dataclass(frozen=True)
class PaperPromotionBridgeConfig:
    min_closed_trades: int = 20
    min_profit_factor: float = 1.5
    min_avg_net_bps: float = 25.0
    max_rows: int = 220

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_paper_promotion_bridge(
    *,
    readiness: Mapping[str, Any] | None = None,
    performance: Mapping[str, Any] | None = None,
    contract: Mapping[str, Any] | None = None,
    maker_quote: Mapping[str, Any] | None = None,
    actions: Mapping[str, Any] | None = None,
    readiness_path: Path | str = DEFAULT_READINESS,
    performance_path: Path | str = DEFAULT_PERFORMANCE,
    contract_path: Path | str = DEFAULT_CONTRACT,
    maker_quote_path: Path | str = DEFAULT_MAKER_QUOTE,
    actions_path: Path | str = DEFAULT_ACTIONS,
    config: PaperPromotionBridgeConfig = PaperPromotionBridgeConfig(),
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the joined paper-promotion bridge payload."""

    now = now or datetime.now(UTC)
    readiness_payload = _payload(readiness, readiness_path)
    performance_payload = _payload(performance, performance_path)
    contract_payload = _payload(contract, contract_path)
    quote_payload = _payload(maker_quote, maker_quote_path)
    actions_payload = _payload(actions, actions_path)

    slots: dict[str, dict[str, Any]] = {}
    _add_rows(slots, "readiness", readiness_payload.get("rows", []))
    _add_rows(slots, "performance", performance_payload.get("rows", []))
    _add_rows(slots, "contract", contract_payload.get("rows", []))
    _add_rows(slots, "maker_quote", quote_payload.get("rows", []))
    _add_rows(slots, "actions", actions_payload.get("rows", []))

    rows = [_bridge_row(slot, config) for slot in slots.values()]
    rows.sort(key=_row_sort_key)
    rows = rows[: max(1, int(config.max_rows))]
    summary = _summary(rows)
    return {
        "generated_at": now.isoformat(),
        "report_id": "paper_promotion_bridge_v1",
        "mode": "read_only_paper_promotion_bridge",
        "source_report_ids": {
            "readiness": readiness_payload.get("report_id"),
            "performance": performance_payload.get("report_id"),
            "contract": contract_payload.get("report_id"),
            "maker_quote": quote_payload.get("report_id"),
            "operator_actions": actions_payload.get("report_id"),
        },
        "inputs": {
            "readiness_path": str(readiness_path),
            "performance_path": str(performance_path),
            "contract_path": str(contract_path),
            "maker_quote_path": str(maker_quote_path),
            "actions_path": str(actions_path),
        },
        "config": config.to_dict(),
        "summary": summary,
        "boards": _boards(rows),
        "rows": rows,
        "operator_answer": _operator_answer(summary),
        "policy": {
            "read_only": True,
            "can_trade": False,
            "can_promote": False,
            "human_review_is_not_promotion": True,
            "live_small_requires_pre_live_checklist_and_live_ladder": True,
        },
        "can_trade": False,
        "can_promote": False,
    }


def publish_paper_promotion_bridge(
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
        "=== Paper promotion bridge ===",
        f"generated: {payload.get('generated_at')}",
        str(payload.get("operator_answer") or ""),
        (
            "summary: "
            f"{summary.get('total_lanes', 0)} lanes, "
            f"{summary.get('paper_review_ready', 0)} review-ready, "
            f"{summary.get('repair_first', 0)} repair-first, "
            f"{summary.get('mine_alpha', 0)} mine-alpha, "
            f"{summary.get('collect_sample', 0)} collect"
        ),
    ]
    for row in list(payload.get("rows", []))[:limit]:
        lines.append(
            f"  {row.get('decision', ''):<32} "
            f"{row.get('exchange', ''):<14} {row.get('symbol', ''):<14} "
            f"{row.get('timeframe', ''):<3} {row.get('strategy_id', ''):<30} "
            f"{row.get('next_action', '')}"
        )
    lines.append("read-only: can_trade=false can_promote=false")
    return "\n".join(lines)


def _payload(value: Mapping[str, Any] | None, path: Path | str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return _read_json(Path(path), {"rows": [], "summary": {}})


def _read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        if not path.exists():
            return dict(default)
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else dict(default)
    except Exception:
        return dict(default)


def _add_rows(slots: dict[str, dict[str, Any]], kind: str, rows: Any) -> None:
    for raw in rows or []:
        if not isinstance(raw, Mapping):
            continue
        row = dict(raw)
        key = _row_key(row).lower()
        if not key:
            continue
        slot = slots.setdefault(key, {"lane_key": key})
        slot[kind] = row
        slot.setdefault("seed", row)
        for field in ("strategy_id", "exchange", "symbol", "timeframe", "lane_id", "trial_id"):
            if slot.get(field) in (None, "") and row.get(field) not in (None, ""):
                slot[field] = row.get(field)


def _row_key(row: Mapping[str, Any]) -> str:
    lane_key = _text(row.get("lane_key"))
    if lane_key:
        return lane_key.lower()
    lane_id = _text(row.get("lane_id") or row.get("trial_id"))
    strategy = _text(row.get("strategy_id"))
    exchange = _text(row.get("exchange"))
    symbol = _text(row.get("symbol")).upper()
    timeframe = _text(row.get("timeframe")).lower()
    if strategy and exchange and symbol and timeframe:
        return "|".join((strategy.lower(), exchange.lower(), symbol, timeframe))
    if lane_id:
        return lane_id.lower()
    return ""


def _bridge_row(slot: Mapping[str, Any], config: PaperPromotionBridgeConfig) -> dict[str, Any]:
    readiness = _map(slot.get("readiness"))
    performance = _map(slot.get("performance"))
    contract = _map(slot.get("contract"))
    quote = _map(slot.get("maker_quote"))
    action_row = _map(slot.get("actions"))

    strategy_id = _first(
        slot.get("strategy_id"),
        readiness.get("strategy_id"),
        performance.get("strategy_id"),
        contract.get("strategy_id"),
        quote.get("strategy_id"),
        action_row.get("strategy_id"),
    )
    exchange = _first(
        slot.get("exchange"),
        readiness.get("exchange"),
        performance.get("exchange"),
        contract.get("exchange"),
        quote.get("exchange"),
        action_row.get("exchange"),
    )
    symbol = _first(
        slot.get("symbol"),
        readiness.get("symbol"),
        performance.get("symbol"),
        contract.get("symbol"),
        quote.get("symbol"),
        action_row.get("symbol"),
    )
    timeframe = _first(
        slot.get("timeframe"),
        readiness.get("timeframe"),
        performance.get("timeframe"),
        contract.get("timeframe"),
        quote.get("timeframe"),
        action_row.get("timeframe"),
    )

    action_bucket = _text(action_row.get("bucket"))
    performance_state = _text(performance.get("state"))
    readiness_status = _text(readiness.get("status"))
    contract_verdict = _text(contract.get("verdict"))
    quote_state = _text(quote.get("lifecycle_state"))
    closed = int(_num(performance.get("closed_trades") or contract.get("closed_trades")))
    profit_factor = _optional_num(performance.get("profit_factor"))
    avg_net_bps = _optional_num(
        contract.get("avg_net_bps")
        if contract.get("avg_net_bps") is not None
        else quote.get("avg_net_bps")
    )
    net = _num(performance.get("net_pnl_usd") or contract.get("net_pnl_usd"))

    decision, stage, owner, next_action, blockers = _decision(
        action_bucket=action_bucket,
        performance_state=performance_state,
        readiness_status=readiness_status,
        contract_verdict=contract_verdict,
        quote_state=quote_state,
        closed_trades=closed,
        profit_factor=profit_factor,
        avg_net_bps=avg_net_bps,
        config=config,
        action_row=action_row,
        performance=performance,
        contract=contract,
        quote=quote,
        readiness=readiness,
    )
    return {
        "lane_key": _text(slot.get("lane_key")) or _row_key(slot),
        "lane_id": _first(slot.get("lane_id"), performance.get("lane_id"), contract.get("lane_id")),
        "exchange": exchange,
        "symbol": symbol,
        "timeframe": timeframe,
        "strategy_id": strategy_id,
        "stage": stage,
        "decision": decision,
        "owner": owner,
        "next_action": next_action,
        "blockers": blockers,
        "evidence": {
            "readiness_status": readiness_status or None,
            "paper_status": performance_state or None,
            "contract_verdict": contract_verdict or None,
            "quote_lifecycle_state": quote_state or None,
            "operator_action_bucket": action_bucket or None,
            "operator_action": action_row.get("action"),
        },
        "metrics": {
            "closed_trades": closed,
            "net_pnl_usd": round(net, 6),
            "profit_factor": profit_factor,
            "avg_net_bps": avg_net_bps,
            "maker_attempts": int(_num(quote.get("maker_attempts"))),
            "maker_fill_rate_pct": _num(quote.get("maker_fill_rate_pct")),
            "critical_contract_violations": int(_num(contract.get("critical_violations"))),
            "fee_wall_breaches": int(_num(contract.get("fee_wall_breaches"))),
        },
        "paper_review_ready": decision == DECISION_PAPER_REVIEW_READY,
        "live_ready": False,
        "can_trade": False,
        "can_promote": False,
    }


def _decision(
    *,
    action_bucket: str,
    performance_state: str,
    readiness_status: str,
    contract_verdict: str,
    quote_state: str,
    closed_trades: int,
    profit_factor: float | None,
    avg_net_bps: float | None,
    config: PaperPromotionBridgeConfig,
    action_row: Mapping[str, Any],
    performance: Mapping[str, Any],
    contract: Mapping[str, Any],
    quote: Mapping[str, Any],
    readiness: Mapping[str, Any],
) -> tuple[str, str, str, str, list[str]]:
    blockers: list[str] = []
    if action_bucket in _REPAIR_ACTIONS:
        blockers.append(f"operator action requires repair first: {action_bucket}")
        return (
            DECISION_REPAIR_FIRST,
            "REPAIR",
            _text(action_row.get("owner")) or "system",
            _text(action_row.get("action")) or "repair lane before promotion review",
            blockers,
        )
    if contract_verdict in _CONTRACT_BAD:
        blockers.append(f"paper contract verdict is {contract_verdict}")
        return (
            DECISION_REPAIR_FIRST,
            "CONTRACT",
            "system",
            _text(contract.get("next_action")) or "repair paper contract first",
            blockers,
        )
    if quote_state in _QUOTE_BAD:
        blockers.append(f"maker/taker lifecycle state is {quote_state}")
        return (
            DECISION_EXECUTION_PROOF_MISSING,
            "EXECUTION",
            "system",
            _text(quote.get("next_action")) or "prove maker/taker lifecycle before review",
            blockers,
        )
    if contract_verdict in _CONTRACT_ALPHA or action_bucket == "MINE_CLEAN_ALPHA":
        blockers.append(f"contract clean but alpha insufficient: {contract_verdict or action_bucket}")
        return (
            DECISION_MINE_ALPHA,
            "ALPHA",
            "research",
            _text(contract.get("next_action"))
            or _text(action_row.get("action"))
            or "mine cleaner entry/exit alpha",
            blockers,
        )
    if performance_state == "PAPER_PROMOTION_CANDIDATE":
        if contract_verdict and contract_verdict not in _CONTRACT_GOOD:
            blockers.append(f"contract proof is not clean-profitable: {contract_verdict}")
        if quote_state and quote_state not in _QUOTE_GOOD:
            blockers.append(f"execution lifecycle is not review-ready: {quote_state}")
        if not contract_verdict:
            blockers.append("paper contract reconciliation missing")
        if not quote_state:
            blockers.append("maker/taker quote lifecycle proof missing")
        if closed_trades < config.min_closed_trades:
            blockers.append(f"closed trades too few: {closed_trades} < {config.min_closed_trades}")
        if profit_factor is not None and profit_factor < config.min_profit_factor:
            blockers.append(
                f"profit factor too low: {profit_factor:.2f} < {config.min_profit_factor:.2f}"
            )
        if avg_net_bps is not None and avg_net_bps < config.min_avg_net_bps:
            blockers.append(f"avg net too small: {avg_net_bps:.1f}bps < {config.min_avg_net_bps:.1f}bps")
        if not blockers:
            return (
                DECISION_PAPER_REVIEW_READY,
                "HUMAN_REVIEW",
                "human",
                "open human paper-to-live review; still no automatic promotion",
                [],
            )
        if not contract_verdict:
            return (
                DECISION_CONTRACT_PROOF_MISSING,
                "CONTRACT",
                "system",
                "publish paper contract reconciliation before review",
                blockers,
            )
        return (
            DECISION_COLLECT_SAMPLE,
            "PAPER",
            "system",
            _text(performance.get("next_action")) or "collect more paper proof before review",
            blockers,
        )
    if closed_trades > 0:
        blockers.append("paper lane has closed trades but no promotion-quality verdict")
        return (
            DECISION_COLLECT_SAMPLE,
            "PAPER",
            "system",
            _text(performance.get("next_action")) or "collect or refactor paper outcomes",
            blockers,
        )
    if readiness_status == "PAPER_REVIEW_READY":
        blockers.append("shadow proof is ready; paper trial approval still required")
        return (
            DECISION_SHADOW_READY,
            "SHADOW",
            "human",
            _text(readiness.get("next_action")) or "open paper-trial approval review",
            blockers,
        )
    if action_bucket == "WAIT_FOR_SIGNAL" or performance_state == "PAPER_ONLINE_NO_TRADES":
        blockers.append("no closed paper trades yet")
        return (
            DECISION_WAIT_SIGNAL,
            "MARKET",
            "market",
            _text(action_row.get("action")) or "wait for qualified signal and journal proof",
            blockers,
        )
    return (
        DECISION_OBSERVE,
        "OBSERVE",
        "system",
        _text(action_row.get("action")) or "observe lane evidence",
        blockers,
    )


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    decisions = Counter(str(row.get("decision") or "") for row in rows)
    stages = Counter(str(row.get("stage") or "") for row in rows)
    return {
        "total_lanes": len(rows),
        "paper_review_ready": decisions[DECISION_PAPER_REVIEW_READY],
        "repair_first": decisions[DECISION_REPAIR_FIRST],
        "execution_proof_missing": decisions[DECISION_EXECUTION_PROOF_MISSING],
        "contract_proof_missing": decisions[DECISION_CONTRACT_PROOF_MISSING],
        "mine_alpha": decisions[DECISION_MINE_ALPHA],
        "collect_sample": decisions[DECISION_COLLECT_SAMPLE],
        "wait_for_signal": decisions[DECISION_WAIT_SIGNAL],
        "shadow_ready": decisions[DECISION_SHADOW_READY],
        "decision_counts": dict(sorted(decisions.items())),
        "stage_counts": dict(sorted(stages.items())),
        "can_trade": False,
        "can_promote": False,
    }


def _boards(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    def slim(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "lane_key": row.get("lane_key"),
            "decision": row.get("decision"),
            "stage": row.get("stage"),
            "owner": row.get("owner"),
            "exchange": row.get("exchange"),
            "symbol": row.get("symbol"),
            "timeframe": row.get("timeframe"),
            "strategy_id": row.get("strategy_id"),
            "next_action": row.get("next_action"),
            "metrics": row.get("metrics"),
            "evidence": row.get("evidence"),
            "blockers": row.get("blockers"),
        }

    return {
        "paper_review_ready": [
            slim(r) for r in rows if r.get("decision") == DECISION_PAPER_REVIEW_READY
        ][:12],
        "repair_first": [
            slim(r) for r in rows if r.get("decision") == DECISION_REPAIR_FIRST
        ][:12],
        "mine_alpha": [
            slim(r) for r in rows if r.get("decision") == DECISION_MINE_ALPHA
        ][:12],
        "collect_or_wait": [
            slim(r)
            for r in rows
            if r.get("decision") in {DECISION_COLLECT_SAMPLE, DECISION_WAIT_SIGNAL}
        ][:12],
    }


def _operator_answer(summary: Mapping[str, Any]) -> str:
    ready = int(summary.get("paper_review_ready") or 0)
    repair = int(summary.get("repair_first") or 0)
    mine = int(summary.get("mine_alpha") or 0)
    collect = int(summary.get("collect_sample") or 0)
    if ready:
        return f"{ready} lane(s) are ready for human review; live promotion is still locked."
    if repair:
        return f"{repair} lane(s) need runtime/contract repair before more promotion talk."
    if mine:
        return f"{mine} lane(s) are contract-clean but need better alpha or exits."
    if collect:
        return f"{collect} lane(s) need more paper outcomes before review."
    return "No lane is ready for paper/live review yet; keep collecting truthful evidence."


def _row_sort_key(row: Mapping[str, Any]) -> tuple[int, float, str, str, str]:
    rank = {
        DECISION_REPAIR_FIRST: 0,
        DECISION_EXECUTION_PROOF_MISSING: 1,
        DECISION_CONTRACT_PROOF_MISSING: 2,
        DECISION_MINE_ALPHA: 3,
        DECISION_PAPER_REVIEW_READY: 4,
        DECISION_COLLECT_SAMPLE: 5,
        DECISION_SHADOW_READY: 6,
        DECISION_WAIT_SIGNAL: 7,
        DECISION_OBSERVE: 8,
    }.get(str(row.get("decision") or ""), 9)
    metrics = _map(row.get("metrics"))
    return (
        rank,
        _num(metrics.get("net_pnl_usd")),
        str(row.get("strategy_id") or ""),
        str(row.get("exchange") or ""),
        str(row.get("symbol") or ""),
    )


def _map(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _first(*values: Any) -> str:
    for value in values:
        text = _text(value)
        if text:
            return text
    return ""


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _num(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if parsed != parsed else parsed


def _optional_num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return None if parsed != parsed else parsed


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--readiness", type=Path, default=DEFAULT_READINESS)
    parser.add_argument("--performance", type=Path, default=DEFAULT_PERFORMANCE)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--maker-quote", type=Path, default=DEFAULT_MAKER_QUOTE)
    parser.add_argument("--actions", type=Path, default=DEFAULT_ACTIONS)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--feed", type=Path, default=DEFAULT_FEED)
    parser.add_argument("--interval-seconds", type=float, default=0.0)
    parser.add_argument("--min-closed-trades", type=int, default=PaperPromotionBridgeConfig.min_closed_trades)
    parser.add_argument("--min-profit-factor", type=float, default=PaperPromotionBridgeConfig.min_profit_factor)
    parser.add_argument("--min-avg-net-bps", type=float, default=PaperPromotionBridgeConfig.min_avg_net_bps)
    parser.add_argument("--max-rows", type=int, default=PaperPromotionBridgeConfig.max_rows)
    parser.add_argument("--print", action="store_true", dest="print_report")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    config = PaperPromotionBridgeConfig(
        min_closed_trades=max(1, int(args.min_closed_trades)),
        min_profit_factor=float(args.min_profit_factor),
        min_avg_net_bps=float(args.min_avg_net_bps),
        max_rows=max(1, int(args.max_rows)),
    )
    while True:
        payload = build_paper_promotion_bridge(
            readiness_path=args.readiness,
            performance_path=args.performance,
            contract_path=args.contract,
            maker_quote_path=args.maker_quote,
            actions_path=args.actions,
            config=config,
        )
        publish_paper_promotion_bridge(payload, args.out, args.feed)
        if args.print_report:
            print(render_report(payload), flush=True)
        if args.interval_seconds <= 0:
            return
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    main()
