"""Maker quote lifecycle auditor.

This read-only report bridges the gap between "scanner fired" and "the lane has
an execution path that can survive fees."  It reads the append-only paper/shadow
journals emitted by ``MakerTakerExecutor`` plus the existing paper performance
and exit-autopsy artifacts, then classifies each lane by maker-fill proof,
taker-fallback discipline, and execution-quality blockers.

It cannot trade, promote, restart, or mutate runtime configuration.
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_RESEARCH_DIR = Path("research/live_research")
DEFAULT_JOURNAL_DIR = Path("logs/paper_trials")
DEFAULT_PERFORMANCE = DEFAULT_RESEARCH_DIR / "paper_lane_performance_latest.json"
DEFAULT_EXIT_AUTOPSY = DEFAULT_RESEARCH_DIR / "paper_trade_exit_autopsy_latest.json"
DEFAULT_OUT = DEFAULT_RESEARCH_DIR / "maker_quote_lifecycle_latest.json"
DEFAULT_FEED = DEFAULT_RESEARCH_DIR / "maker_quote_lifecycle_feed.jsonl"

STATE_NO_QUOTE_LIFECYCLE_WIRING = "NO_QUOTE_LIFECYCLE_WIRING"
STATE_COLLECT_MAKER_QUOTE_SAMPLE = "COLLECT_MAKER_QUOTE_SAMPLE"
STATE_MAKER_ROUTE_BLOCKED = "MAKER_ROUTE_BLOCKED"
STATE_MAKER_FILL_UNPROVEN = "MAKER_FILL_UNPROVEN"
STATE_MAKER_OBSERVED_SHADOW_READY = "MAKER_OBSERVED_SHADOW_READY"
STATE_TAKER_FALLBACK_FORBIDDEN = "TAKER_FALLBACK_FORBIDDEN"
STATE_TAKER_FALLBACK_NEEDS_AUTOPSY = "TAKER_FALLBACK_NEEDS_AUTOPSY"
STATE_NEGATIVE_AFTER_EXECUTION = "NEGATIVE_AFTER_EXECUTION"
STATE_TIMEOUT_UNKNOWN_FAIL_CLOSED = "TIMEOUT_UNKNOWN_FAIL_CLOSED"
STATE_QUOTE_LIFECYCLE_PAPER_REVIEW = "QUOTE_LIFECYCLE_PAPER_REVIEW"

ACTION_WIRE_EXECUTOR = "WIRE_SIGNALS_TO_MAKER_TAKER_EXECUTOR"
ACTION_COLLECT_SAMPLE = "COLLECT_MAKER_QUOTE_SAMPLE"
ACTION_REPAIR_ROUTE = "REPAIR_POST_ONLY_ROUTE_OR_EDGE_MODEL"
ACTION_KEEP_MAKER_ONLY = "KEEP_MAKER_ONLY_DO_NOT_CHASE_TAKER"
ACTION_REPAIR_FILL_MODEL = "COLLECT_OR_REPAIR_MAKER_FILL_TELEMETRY"
ACTION_REPLAY_AUTOPSY = "RUN_EXECUTION_AUTOPSY_BEFORE_PROMOTION"
ACTION_RETURN_RESEARCH = "RETURN_LANE_TO_RESEARCH"
ACTION_FAIL_CLOSED = "FAIL_CLOSED_RECONCILE_EXECUTOR"
ACTION_HUMAN_REVIEW = "HUMAN_REVIEW_BEFORE_ANY_PROMOTION"


@dataclass(frozen=True)
class MakerQuoteLifecycleConfig:
    tail_bytes: int = 8_000_000
    max_rows: int = 180
    min_maker_attempts: int = 5
    min_maker_fill_rate_pct: float = 5.0
    min_taker_net_edge_bps: float = 25.0
    min_taker_cost_coverage: float = 1.5
    min_closed_trades: int = 20
    min_profit_factor: float = 1.5
    min_avg_net_bps: float = 25.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_maker_quote_lifecycle(
    *,
    journal_dir: Path | str = DEFAULT_JOURNAL_DIR,
    performance: Mapping[str, Any] | None = None,
    exit_autopsy: Mapping[str, Any] | None = None,
    performance_path: Path | str = DEFAULT_PERFORMANCE,
    exit_autopsy_path: Path | str = DEFAULT_EXIT_AUTOPSY,
    config: MakerQuoteLifecycleConfig = MakerQuoteLifecycleConfig(),
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the read-only maker quote lifecycle report."""

    now = now or datetime.now(UTC)
    root = Path(journal_dir)
    performance_path = Path(performance_path)
    exit_autopsy_path = Path(exit_autopsy_path)
    performance_payload = _payload_or_file(performance, performance_path)
    exit_payload = _payload_or_file(exit_autopsy, exit_autopsy_path)

    journal_rows = _journal_index(root, config=config)
    performance_index = _index_rows(performance_payload.get("rows", []))
    exit_index = _index_rows(exit_payload.get("rows", []))
    lane_ids = sorted(set(journal_rows) | set(performance_index) | set(exit_index))

    rows = [
        _lane_lifecycle_row(
            lane_id,
            journal_rows=journal_rows.get(lane_id, []),
            performance=performance_index.get(lane_id, {}),
            exit_autopsy=exit_index.get(lane_id, {}),
            config=config,
        )
        for lane_id in lane_ids
    ]
    rows.sort(key=_row_sort_key)
    rows = rows[: max(1, int(config.max_rows))]
    summary = _summary(rows)
    return {
        "generated_at": now.isoformat(),
        "report_id": "maker_quote_lifecycle_v1",
        "mode": "read_only_maker_quote_lifecycle",
        "source_reports": {
            "performance": performance_payload.get("report_id"),
            "exit_autopsy": exit_payload.get("report_id"),
        },
        "source_generated_at": {
            "performance": performance_payload.get("generated_at"),
            "exit_autopsy": exit_payload.get("generated_at"),
        },
        "inputs": {
            "journal_dir": str(root),
            "performance_path": str(performance_path),
            "exit_autopsy_path": str(exit_autopsy_path),
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
            "scope": "execution evidence only; promotion still requires normal gates",
            "taker_fallback_rule": (
                "taker fallback is allowed only when expected net edge and cost "
                "coverage clear the configured hurdle"
            ),
        },
        "can_trade": False,
        "can_promote": False,
    }


def publish_maker_quote_lifecycle(
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
        "=== Maker quote lifecycle ===",
        f"generated: {payload.get('generated_at')}",
        str(payload.get("operator_answer") or ""),
        (
            "summary: "
            f"{summary.get('total_lanes', 0)} lanes, "
            f"{summary.get('lanes_with_executor_events', 0)} with executor events, "
            f"{summary.get('quote_lifecycle_review_ready', 0)} review-ready, "
            f"{summary.get('maker_fill_unproven', 0)} fill-unproven, "
            f"{summary.get('taker_fallback_forbidden', 0)} taker-forbidden"
        ),
    ]
    for row in list(payload.get("rows", []))[:limit]:
        lines.append(
            f"  {row.get('lifecycle_state', ''):<32} "
            f"{row.get('lane_id', ''):<42} "
            f"maker {row.get('maker_attempts', 0):>3}/"
            f"{row.get('maker_fill_rate_pct', 0.0):>5.1f}% "
            f"taker {row.get('taker_fallback_submitted', 0):>3} "
            f"score {row.get('quote_lifecycle_score', 0):>5.1f} "
            f"{row.get('next_action', '')}"
        )
    lines.append("read-only: can_trade=false can_promote=false")
    return "\n".join(lines)


def _lane_lifecycle_row(
    lane_id: str,
    *,
    journal_rows: list[dict[str, Any]],
    performance: Mapping[str, Any],
    exit_autopsy: Mapping[str, Any],
    config: MakerQuoteLifecycleConfig,
) -> dict[str, Any]:
    counters: Counter[str] = Counter()
    state_counts: Counter[str] = Counter()
    maker_checks: list[dict[str, Any]] = []
    taker_checks: list[dict[str, Any]] = []
    finished: list[dict[str, Any]] = []
    latest_reason = ""
    latest_ts = None
    exchange = _text(performance.get("exchange") or exit_autopsy.get("exchange"))
    symbol = _text(performance.get("symbol") or exit_autopsy.get("symbol"))
    timeframe = _text(performance.get("timeframe") or exit_autopsy.get("timeframe"))
    strategy_id = _text(
        performance.get("strategy_id") or exit_autopsy.get("strategy_id") or lane_id
    )
    mode = _text(performance.get("mode") or exit_autopsy.get("mode"))

    for record in journal_rows:
        latest_ts = _record_ts(record) or latest_ts
        kind = _text(record.get("kind"))
        counters[kind] += 1
        payload = record.get("payload") if isinstance(record.get("payload"), Mapping) else {}
        intent = payload.get("intent") if isinstance(payload.get("intent"), Mapping) else {}
        for src in ("exchange", "symbol", "timeframe", "strategy_id", "mode"):
            value = _text(payload.get(src) or intent.get(src))
            if value:
                if src == "exchange":
                    exchange = value
                elif src == "symbol":
                    symbol = value
                elif src == "timeframe":
                    timeframe = value
                elif src == "strategy_id":
                    strategy_id = value
                elif src == "mode":
                    mode = value
        if kind == "executor_route_check":
            route = _text(payload.get("route"))
            if route == "maker":
                maker_checks.append(dict(payload))
            elif route == "taker_fallback":
                taker_checks.append(dict(payload))
        elif kind == "executor_finished":
            row = dict(payload)
            finished.append(row)
            state_counts[_text(row.get("state"))] += 1
            latest_reason = _text(row.get("reason") or latest_reason)
        elif kind == "executor_started":
            exchange = _text(payload.get("exchange") or exchange)
            symbol = _text(payload.get("symbol") or symbol)
            strategy_id = _text(payload.get("strategy_id") or strategy_id)

    maker_attempts = int(counters.get("executor_maker_submitted") or 0)
    maker_full_fills = int(state_counts.get("maker_filled") or 0)
    maker_partial_fills = sum(
        1
        for row in finished
        if _num(row.get("maker_filled_quantity")) > 0
        and _text(row.get("state")) != "maker_filled"
    )
    maker_fill_events = maker_full_fills + maker_partial_fills
    maker_fill_rate = (
        maker_fill_events / maker_attempts * 100.0 if maker_attempts else 0.0
    )
    taker_submitted = int(counters.get("executor_taker_submitted") or 0)
    taker_blocked = int(state_counts.get("taker_blocked") or 0)
    timeout_unknown = int(state_counts.get("timeout_unknown") or 0)
    maker_cancelled = sum(
        1
        for row in finished
        if _text(row.get("state")) in {"taker_submitted", "taker_blocked"}
    )
    latest_maker = maker_checks[-1] if maker_checks else {}
    latest_taker = taker_checks[-1] if taker_checks else {}
    closed_trades = int(_num(performance.get("closed_trades")))
    profit_factor = _num(performance.get("profit_factor"))
    net_pnl_usd = _num(performance.get("net_pnl_usd"))
    avg_perf_bps = _maybe_num(performance.get("avg_closed_trade_net_bps"))
    avg_exit_bps = _maybe_num(exit_autopsy.get("avg_net_bps"))
    avg_net_bps = avg_exit_bps if avg_exit_bps is not None else avg_perf_bps
    avg_fee_bps = _maybe_num(exit_autopsy.get("avg_fee_bps"))
    loss_driver = _text(exit_autopsy.get("loss_driver"))
    lifecycle_state, next_action, blockers = _diagnose(
        maker_attempts=maker_attempts,
        maker_fill_rate_pct=maker_fill_rate,
        maker_route_blocked=_route_blocked(latest_maker),
        taker_submitted=taker_submitted,
        taker_blocked=taker_blocked,
        latest_taker=latest_taker,
        timeout_unknown=timeout_unknown,
        closed_trades=closed_trades,
        net_pnl_usd=net_pnl_usd,
        profit_factor=profit_factor,
        avg_net_bps=avg_net_bps,
        loss_driver=loss_driver,
        config=config,
    )
    row = {
        "lane_id": lane_id,
        "exchange": exchange or _exchange_hint(lane_id),
        "symbol": symbol,
        "timeframe": timeframe or _timeframe_hint(lane_id),
        "strategy_id": strategy_id,
        "mode": mode,
        "latest_ts": latest_ts,
        "latest_executor_reason": latest_reason or None,
        "lifecycle_state": lifecycle_state,
        "next_action": next_action,
        "blockers": blockers,
        "executor_events": sum(
            count for kind, count in counters.items() if kind.startswith("executor_")
        ),
        "executor_finished": len(finished),
        "maker_attempts": maker_attempts,
        "maker_full_fills": maker_full_fills,
        "maker_partial_fills": maker_partial_fills,
        "maker_fill_events": maker_fill_events,
        "maker_fill_rate_pct": round(maker_fill_rate, 4),
        "maker_cancelled_after_ttl": int(maker_cancelled),
        "taker_fallback_submitted": taker_submitted,
        "taker_fallback_blocked": taker_blocked,
        "timeout_unknown": timeout_unknown,
        "scalper_risk_rejections": sum(
            1
            for record in journal_rows
            if _text(record.get("kind")) == "executor_scalper_risk_decision"
            and not bool(
                (record.get("payload") if isinstance(record.get("payload"), Mapping) else {}).get(
                    "approved"
                )
            )
        ),
        "latest_maker_check": _slim_route_check(latest_maker),
        "latest_taker_check": _slim_route_check(latest_taker),
        "taker_fallback_math": _taker_math(latest_taker, config),
        "queue_proof_state": _queue_proof_state(maker_attempts, maker_fill_rate, closed_trades),
        "adverse_selection_state": _adverse_state(
            maker_attempts=maker_attempts,
            closed_trades=closed_trades,
            avg_net_bps=avg_net_bps,
            avg_fee_bps=avg_fee_bps,
        ),
        "performance_state": performance.get("state"),
        "closed_trades": closed_trades,
        "profit_factor": round(profit_factor, 4),
        "net_pnl_usd": round(net_pnl_usd, 6),
        "avg_net_bps": round(avg_net_bps, 4) if avg_net_bps is not None else None,
        "avg_fee_bps": round(avg_fee_bps, 4) if avg_fee_bps is not None else None,
        "exit_loss_driver": loss_driver or None,
        "quote_lifecycle_score": 0.0,
        "can_trade": False,
        "can_promote": False,
    }
    row["quote_lifecycle_score"] = round(_score(row, config), 2)
    return row


def _diagnose(
    *,
    maker_attempts: int,
    maker_fill_rate_pct: float,
    maker_route_blocked: bool,
    taker_submitted: int,
    taker_blocked: int,
    latest_taker: Mapping[str, Any],
    timeout_unknown: int,
    closed_trades: int,
    net_pnl_usd: float,
    profit_factor: float,
    avg_net_bps: float | None,
    loss_driver: str,
    config: MakerQuoteLifecycleConfig,
) -> tuple[str, str, list[str]]:
    blockers: list[str] = []
    if timeout_unknown:
        return (
            STATE_TIMEOUT_UNKNOWN_FAIL_CLOSED,
            ACTION_FAIL_CLOSED,
            [f"{timeout_unknown} unresolved executor timeout(s)"],
        )
    if maker_attempts <= 0:
        if maker_route_blocked:
            return (
                STATE_MAKER_ROUTE_BLOCKED,
                ACTION_REPAIR_ROUTE,
                ["maker route check blocked before quote submit"],
            )
        return (
            STATE_NO_QUOTE_LIFECYCLE_WIRING,
            ACTION_WIRE_EXECUTOR,
            ["no executor_maker_submitted journal proof"],
        )
    if maker_attempts < config.min_maker_attempts:
        blockers.append(f"maker attempts {maker_attempts} < {config.min_maker_attempts}")
        return (STATE_COLLECT_MAKER_QUOTE_SAMPLE, ACTION_COLLECT_SAMPLE, blockers)
    if taker_blocked and _taker_forbidden(latest_taker, config):
        blockers.extend(_route_failures(latest_taker))
        if not blockers:
            blockers.append("taker fallback does not clear net edge/cost coverage")
        return (STATE_TAKER_FALLBACK_FORBIDDEN, ACTION_KEEP_MAKER_ONLY, blockers)
    if maker_fill_rate_pct < config.min_maker_fill_rate_pct:
        blockers.append(
            f"maker fill rate {maker_fill_rate_pct:.1f}% < "
            f"{config.min_maker_fill_rate_pct:.1f}%"
        )
        return (STATE_MAKER_FILL_UNPROVEN, ACTION_REPAIR_FILL_MODEL, blockers)
    if closed_trades > 0 and net_pnl_usd < 0:
        blockers.append("closed paper net is negative after execution costs")
        if loss_driver:
            blockers.append(f"exit autopsy: {loss_driver}")
        return (STATE_NEGATIVE_AFTER_EXECUTION, ACTION_RETURN_RESEARCH, blockers)
    if taker_submitted and closed_trades < config.min_closed_trades:
        blockers.append(
            f"taker fallback used but closed trades {closed_trades} < "
            f"{config.min_closed_trades}"
        )
        return (STATE_TAKER_FALLBACK_NEEDS_AUTOPSY, ACTION_REPLAY_AUTOPSY, blockers)
    if (
        closed_trades >= config.min_closed_trades
        and profit_factor >= config.min_profit_factor
        and avg_net_bps is not None
        and avg_net_bps >= config.min_avg_net_bps
        and net_pnl_usd > 0
    ):
        return (STATE_QUOTE_LIFECYCLE_PAPER_REVIEW, ACTION_HUMAN_REVIEW, blockers)
    blockers.append("maker quote lifecycle exists but paper proof is not mature")
    return (STATE_MAKER_OBSERVED_SHADOW_READY, ACTION_REPLAY_AUTOPSY, blockers)


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    states = Counter(str(row.get("lifecycle_state") or "") for row in rows)
    maker_attempts = sum(int(row.get("maker_attempts") or 0) for row in rows)
    maker_fills = sum(int(row.get("maker_fill_events") or 0) for row in rows)
    taker_submitted = sum(int(row.get("taker_fallback_submitted") or 0) for row in rows)
    return {
        "total_lanes": len(rows),
        "lanes_with_executor_events": sum(
            1 for row in rows if int(row.get("executor_events") or 0) > 0
        ),
        "quote_lifecycle_review_ready": states[STATE_QUOTE_LIFECYCLE_PAPER_REVIEW],
        "maker_observed_shadow_ready": states[STATE_MAKER_OBSERVED_SHADOW_READY],
        "maker_fill_unproven": states[STATE_MAKER_FILL_UNPROVEN],
        "taker_fallback_forbidden": states[STATE_TAKER_FALLBACK_FORBIDDEN],
        "negative_after_execution": states[STATE_NEGATIVE_AFTER_EXECUTION],
        "no_quote_lifecycle_wiring": states[STATE_NO_QUOTE_LIFECYCLE_WIRING],
        "timeout_unknown_fail_closed": states[STATE_TIMEOUT_UNKNOWN_FAIL_CLOSED],
        "maker_attempts": maker_attempts,
        "maker_fill_events": maker_fills,
        "maker_fill_rate_pct": round(
            maker_fills / maker_attempts * 100.0 if maker_attempts else 0.0, 4
        ),
        "taker_fallback_submitted": taker_submitted,
        "taker_fallback_blocked": sum(
            int(row.get("taker_fallback_blocked") or 0) for row in rows
        ),
        "closed_trades": sum(int(row.get("closed_trades") or 0) for row in rows),
        "state_counts": dict(sorted(states.items())),
        "can_trade": False,
        "can_promote": False,
    }


def _boards(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    return {
        "review_ready": [
            _slim(row)
            for row in rows
            if row.get("lifecycle_state") == STATE_QUOTE_LIFECYCLE_PAPER_REVIEW
        ],
        "maker_observed": [
            _slim(row)
            for row in rows
            if row.get("lifecycle_state") == STATE_MAKER_OBSERVED_SHADOW_READY
        ],
        "fill_unproven": [
            _slim(row)
            for row in rows
            if row.get("lifecycle_state") == STATE_MAKER_FILL_UNPROVEN
        ],
        "taker_forbidden": [
            _slim(row)
            for row in rows
            if row.get("lifecycle_state") == STATE_TAKER_FALLBACK_FORBIDDEN
        ],
        "repair": [
            _slim(row)
            for row in rows
            if row.get("lifecycle_state")
            in {
                STATE_NO_QUOTE_LIFECYCLE_WIRING,
                STATE_MAKER_ROUTE_BLOCKED,
                STATE_TIMEOUT_UNKNOWN_FAIL_CLOSED,
                STATE_NEGATIVE_AFTER_EXECUTION,
            }
        ],
    }


def _operator_answer(summary: Mapping[str, Any]) -> str:
    if int(summary.get("timeout_unknown_fail_closed") or 0):
        return "Executor timeout_unknown exists; fail closed and reconcile before more quote attempts."
    if int(summary.get("quote_lifecycle_review_ready") or 0):
        return "At least one lane has maker lifecycle plus positive paper proof; human review is still required."
    if int(summary.get("maker_fill_unproven") or 0):
        return "Maker quotes are being attempted, but fill proof is weak; do not promote until queue/fill evidence improves."
    if int(summary.get("taker_fallback_forbidden") or 0):
        return "Some lanes correctly refused taker fallback because edge does not pay fees; keep maker-only or improve signal expectancy."
    if int(summary.get("negative_after_execution") or 0):
        return "Closed paper execution is negative after costs; return those lanes to research."
    if int(summary.get("no_quote_lifecycle_wiring") or 0):
        return "Signals exist without quote lifecycle proof; wire them through maker/taker execution before judging live readiness."
    return "Maker quote lifecycle evidence is collecting; no lane is promotion-ready from execution proof alone."


def _score(row: Mapping[str, Any], config: MakerQuoteLifecycleConfig) -> float:
    score = 0.0
    if int(row.get("executor_events") or 0) > 0:
        score += 15.0
    maker_attempts = int(row.get("maker_attempts") or 0)
    score += 20.0 * min(maker_attempts / max(1, config.min_maker_attempts), 1.0)
    fill_rate = _num(row.get("maker_fill_rate_pct"))
    score += 25.0 * min(fill_rate / max(1.0, config.min_maker_fill_rate_pct), 1.0)
    taker = row.get("latest_taker_check") if isinstance(row.get("latest_taker_check"), Mapping) else {}
    if taker:
        net = _num(taker.get("net_edge_bps"))
        cov = _num(taker.get("cost_coverage"))
        if net >= config.min_taker_net_edge_bps and cov >= config.min_taker_cost_coverage:
            score += 15.0
    closed = int(row.get("closed_trades") or 0)
    score += 15.0 * min(closed / max(1, config.min_closed_trades), 1.0)
    if _num(row.get("net_pnl_usd")) > 0 and _num(row.get("profit_factor")) >= config.min_profit_factor:
        score += 10.0
    if row.get("lifecycle_state") in {
        STATE_TIMEOUT_UNKNOWN_FAIL_CLOSED,
        STATE_NEGATIVE_AFTER_EXECUTION,
    }:
        score -= 30.0
    return max(0.0, min(100.0, score))


def _slim(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "lane_id": row.get("lane_id"),
        "exchange": row.get("exchange"),
        "symbol": row.get("symbol"),
        "timeframe": row.get("timeframe"),
        "strategy_id": row.get("strategy_id"),
        "lifecycle_state": row.get("lifecycle_state"),
        "quote_lifecycle_score": row.get("quote_lifecycle_score"),
        "maker_attempts": row.get("maker_attempts"),
        "maker_fill_rate_pct": row.get("maker_fill_rate_pct"),
        "taker_fallback_submitted": row.get("taker_fallback_submitted"),
        "closed_trades": row.get("closed_trades"),
        "avg_net_bps": row.get("avg_net_bps"),
        "profit_factor": row.get("profit_factor"),
        "next_action": row.get("next_action"),
    }


def _journal_index(
    root: Path, *, config: MakerQuoteLifecycleConfig
) -> dict[str, list[dict[str, Any]]]:
    rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if not root.is_dir():
        return rows
    for path in sorted(root.glob("*.journal.jsonl")):
        lane = path.name.removesuffix(".journal.jsonl")
        for record in _iter_jsonl(path, max_bytes=config.tail_bytes):
            if isinstance(record, Mapping):
                rows[lane].append(dict(record))
    return rows


def _iter_jsonl(path: Path, *, max_bytes: int) -> Iterable[dict[str, Any]]:
    try:
        size = path.stat().st_size
        with open(path, "rb") as handle:
            if size > max_bytes:
                handle.seek(max(0, size - max_bytes))
                handle.readline()
            for raw in handle:
                try:
                    parsed = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if isinstance(parsed, dict):
                    yield parsed
    except FileNotFoundError:
        return


def _payload_or_file(payload: Mapping[str, Any] | None, path: Path) -> dict[str, Any]:
    if payload is not None:
        return dict(payload)
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _index_rows(rows: Any) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    if not isinstance(rows, Iterable) or isinstance(rows, (str, bytes)):
        return index
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        lane = _row_lane_id(row)
        if lane:
            index.setdefault(lane, dict(row))
    return index


def _row_lane_id(row: Mapping[str, Any]) -> str:
    for key in ("lane_id", "lane_key", "id"):
        value = _text(row.get(key))
        if value:
            return value
    parts = [
        _text(row.get("strategy_id")),
        _text(row.get("exchange")),
        _text(row.get("symbol")).lower(),
        _text(row.get("timeframe")),
    ]
    return "|".join(part for part in parts if part)


def _slim_route_check(row: Mapping[str, Any]) -> dict[str, Any] | None:
    if not row:
        return None
    return {
        "route": row.get("route"),
        "allowed": bool(row.get("allowed")),
        "expected_edge_bps": round(_num(row.get("expected_edge_bps")), 4),
        "cost_bps": round(_num(row.get("cost_bps")), 4),
        "net_edge_bps": round(_num(row.get("net_edge_bps")), 4),
        "cost_coverage": round(_num(row.get("cost_coverage")), 4),
        "failed_checks": list(row.get("failed_checks") or []),
    }


def _taker_math(row: Mapping[str, Any], config: MakerQuoteLifecycleConfig) -> dict[str, Any]:
    if not row:
        return {
            "observed": False,
            "fallback_allowed_by_math": False,
            "reason": "no taker fallback check observed",
        }
    net = _num(row.get("net_edge_bps"))
    coverage = _num(row.get("cost_coverage"))
    allowed = (
        bool(row.get("allowed"))
        and net >= config.min_taker_net_edge_bps
        and coverage >= config.min_taker_cost_coverage
    )
    return {
        "observed": True,
        "fallback_allowed_by_math": allowed,
        "net_edge_bps": round(net, 4),
        "required_net_edge_bps": config.min_taker_net_edge_bps,
        "cost_coverage": round(coverage, 4),
        "required_cost_coverage": config.min_taker_cost_coverage,
    }


def _route_blocked(row: Mapping[str, Any]) -> bool:
    return bool(row) and not bool(row.get("allowed"))


def _taker_forbidden(row: Mapping[str, Any], config: MakerQuoteLifecycleConfig) -> bool:
    if not row:
        return False
    return (
        not bool(row.get("allowed"))
        or _num(row.get("net_edge_bps")) < config.min_taker_net_edge_bps
        or _num(row.get("cost_coverage")) < config.min_taker_cost_coverage
    )


def _route_failures(row: Mapping[str, Any]) -> list[str]:
    failures = row.get("failed_checks") if isinstance(row, Mapping) else None
    if not isinstance(failures, list):
        return []
    return [str(item) for item in failures]


def _queue_proof_state(maker_attempts: int, fill_rate_pct: float, closed_trades: int) -> str:
    if maker_attempts <= 0:
        return "UNWIRED"
    if fill_rate_pct <= 0:
        return "QUOTED_NO_FILL_PROOF"
    if closed_trades <= 0:
        return "MAKER_FILL_OBSERVED_NO_CLOSED_OUTCOME"
    return "MAKER_FILL_AND_OUTCOME_OBSERVED"


def _adverse_state(
    *,
    maker_attempts: int,
    closed_trades: int,
    avg_net_bps: float | None,
    avg_fee_bps: float | None,
) -> str:
    if maker_attempts <= 0:
        return "UNMEASURED_NO_QUOTE_PATH"
    if closed_trades <= 0:
        return "UNMEASURED_NO_CLOSED_OUTCOME"
    if avg_net_bps is None:
        return "UNKNOWN_MISSING_NET_BPS"
    if avg_fee_bps is not None and avg_net_bps < avg_fee_bps:
        return "LIKELY_ADVERSE_OR_FEE_DOMINATED"
    if avg_net_bps < 0:
        return "NEGATIVE_AFTER_SELECTION"
    return "OUTCOME_OBSERVED"


def _row_sort_key(row: Mapping[str, Any]) -> tuple[int, float, str]:
    order = {
        STATE_TIMEOUT_UNKNOWN_FAIL_CLOSED: 0,
        STATE_NEGATIVE_AFTER_EXECUTION: 1,
        STATE_TAKER_FALLBACK_FORBIDDEN: 2,
        STATE_MAKER_FILL_UNPROVEN: 3,
        STATE_MAKER_ROUTE_BLOCKED: 4,
        STATE_NO_QUOTE_LIFECYCLE_WIRING: 5,
        STATE_COLLECT_MAKER_QUOTE_SAMPLE: 6,
        STATE_TAKER_FALLBACK_NEEDS_AUTOPSY: 7,
        STATE_MAKER_OBSERVED_SHADOW_READY: 8,
        STATE_QUOTE_LIFECYCLE_PAPER_REVIEW: 9,
    }
    return (
        order.get(_text(row.get("lifecycle_state")), 99),
        -_num(row.get("quote_lifecycle_score")),
        _text(row.get("lane_id")),
    )


def _record_ts(record: Mapping[str, Any]) -> str | None:
    for key in ("ts", "timestamp", "generated_at"):
        value = record.get(key)
        if value:
            return str(value)
    payload = record.get("payload") if isinstance(record.get("payload"), Mapping) else {}
    for key in ("ts", "timestamp", "bar_ts"):
        value = payload.get(key)
        if value:
            return str(value)
    return None


def _exchange_hint(lane_id: str) -> str:
    lowered = lane_id.lower()
    for exchange in ("binance", "bybit", "delta_india", "delta"):
        if exchange in lowered:
            return exchange
    return ""


def _timeframe_hint(lane_id: str) -> str:
    for part in lane_id.replace("|", "_").split("_"):
        if part in {"1m", "5m", "15m", "1h", "4h"}:
            return part
    return ""


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _num(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    if parsed == float("inf"):
        return 999.0
    if parsed == float("-inf"):
        return -999.0
    return parsed


def _maybe_num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal-dir", type=Path, default=DEFAULT_JOURNAL_DIR)
    parser.add_argument("--performance", type=Path, default=DEFAULT_PERFORMANCE)
    parser.add_argument("--exit-autopsy", type=Path, default=DEFAULT_EXIT_AUTOPSY)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--feed", type=Path, default=DEFAULT_FEED)
    parser.add_argument("--interval-seconds", type=_positive_float, default=0.0)
    parser.add_argument("--tail-bytes", type=_positive_int, default=8_000_000)
    parser.add_argument("--max-rows", type=_positive_int, default=180)
    parser.add_argument("--min-maker-attempts", type=_positive_int, default=5)
    parser.add_argument("--min-maker-fill-rate-pct", type=_positive_float, default=5.0)
    parser.add_argument("--min-taker-net-edge-bps", type=_positive_float, default=25.0)
    parser.add_argument("--min-taker-cost-coverage", type=_positive_float, default=1.5)
    parser.add_argument("--min-closed-trades", type=_positive_int, default=20)
    parser.add_argument("--min-profit-factor", type=_positive_float, default=1.5)
    parser.add_argument("--min-avg-net-bps", type=_positive_float, default=25.0)
    parser.add_argument("--print", action="store_true")
    return parser


def _build_from_args(args: argparse.Namespace) -> dict[str, Any]:
    return build_maker_quote_lifecycle(
        journal_dir=args.journal_dir,
        performance_path=args.performance,
        exit_autopsy_path=args.exit_autopsy,
        config=MakerQuoteLifecycleConfig(
            tail_bytes=args.tail_bytes,
            max_rows=args.max_rows,
            min_maker_attempts=args.min_maker_attempts,
            min_maker_fill_rate_pct=args.min_maker_fill_rate_pct,
            min_taker_net_edge_bps=args.min_taker_net_edge_bps,
            min_taker_cost_coverage=args.min_taker_cost_coverage,
            min_closed_trades=args.min_closed_trades,
            min_profit_factor=args.min_profit_factor,
            min_avg_net_bps=args.min_avg_net_bps,
        ),
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    while True:
        payload = _build_from_args(args)
        publish_maker_quote_lifecycle(payload, args.out, args.feed)
        if args.print:
            print(render_report(payload))
        if args.interval_seconds <= 0:
            break
        time.sleep(args.interval_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
