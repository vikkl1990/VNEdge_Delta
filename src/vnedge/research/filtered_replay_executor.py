"""Filtered conservative replay for execution-condition hypotheses.

The execution-condition miner can explain why a candidate failed replay and
propose a filter. This module is the next proof step: freeze that filter, apply
it only to information available at or before quote placement, then rerun the
same conservative event replay on a fresh slice.

It is deliberately research-only. A pass here is not promotion; it only allows
the Alpha Council to discuss a governed shadow manifest.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from vnedge.research.candidate_replay_executor import (
    CandidateReplayConfig,
    CandidateReplayRow,
    EventReplaySpec,
    _leadlag_specs,
    _orderflow_specs,
    replay_specs,
)
from vnedge.research.execution_condition_miner import (
    ExecutionConditionConfig,
    analyze_replay_row,
)
from vnedge.scalping.replay_backtester import load_tick_events

logger = logging.getLogger(__name__)

EXECUTOR_ID = "filtered_replay_executor_v1"
DEFAULT_EVENT_LEADLAG = Path("research/live_research/event_leadlag_latest.json")
DEFAULT_ORDERFLOW = Path("research/live_research/orderflow_footprint_latest.json")
DEFAULT_CANDIDATE_REPLAY = Path("research/live_research/candidate_replay_latest.json")
DEFAULT_CONDITIONS = Path("research/live_research/execution_condition_latest.json")
DEFAULT_OUT = Path("research/live_research/filtered_replay_latest.json")
DEFAULT_FEED = Path("research/live_research/filtered_replay_feed.jsonl")


@dataclass(frozen=True)
class FilteredReplayConfig:
    allow_seen_window: bool = False
    max_event_leadlag_specs: int = 12
    max_orderflow_specs: int = 60
    min_pre_trade_count: int = 1
    min_pre_signed_notional_usd: float = 50.0
    replay: CandidateReplayConfig = field(default_factory=CandidateReplayConfig)
    conditions: ExecutionConditionConfig = field(default_factory=ExecutionConditionConfig)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["replay"] = self.replay.to_dict()
        payload["conditions"] = self.conditions.to_dict()
        return payload


@dataclass(frozen=True)
class FilterDecision:
    candidate_id: str
    exchange: str
    symbol: str
    day: str
    side: str
    trigger_ts: str
    accepted: bool
    reason: str
    filter_name: str
    bucket: str
    feature_snapshot: dict[str, Any]
    can_trade: bool = False
    can_promote: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_filtered_replay(
    data_root: Path | str,
    *,
    event_leadlag_path: Path | str = DEFAULT_EVENT_LEADLAG,
    orderflow_path: Path | str = DEFAULT_ORDERFLOW,
    candidate_replay_path: Path | str = DEFAULT_CANDIDATE_REPLAY,
    condition_path: Path | str = DEFAULT_CONDITIONS,
    config: FilteredReplayConfig = FilteredReplayConfig(),
) -> dict[str, Any]:
    condition_payload = _read_json(Path(condition_path))
    candidate_replay_payload = _read_json(Path(candidate_replay_path))
    conditions = _condition_index(condition_payload)
    seen_days = _seen_replay_days(candidate_replay_payload)
    specs = _input_specs(
        data_root,
        _read_json(Path(event_leadlag_path)),
        _read_json(Path(orderflow_path)),
        config=config,
    )
    decisions: list[FilterDecision] = []
    accepted_specs: list[EventReplaySpec] = []
    cache: dict[tuple[str, str, str], list[tuple[int, str, object]]] = {}
    for spec in specs:
        condition = conditions.get(spec.candidate_id)
        if condition is None:
            continue
        decision, events = _filter_decision(
            data_root,
            spec,
            condition,
            seen_days.get(spec.candidate_id, set()),
            cache,
            config,
        )
        decisions.append(decision)
        if decision.accepted:
            accepted_specs.append(spec)
            cache[(spec.exchange, spec.symbol, spec.day)] = events
    rows = [
        _augment_row(row, conditions.get(row.candidate_id, {}))
        for row in replay_specs(data_root, accepted_specs, config=config.replay)
    ]
    rows.sort(key=_row_sort_key)
    decision_rows = [row.to_dict() for row in decisions]
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "executor_id": EXECUTOR_ID,
        "policy": filtered_replay_policy(),
        "config": config.to_dict(),
        "inputs": {
            "event_leadlag_path": str(event_leadlag_path),
            "orderflow_path": str(orderflow_path),
            "candidate_replay_path": str(candidate_replay_path),
            "condition_path": str(condition_path),
        },
        "summary": _summary(rows, decision_rows),
        "rows": rows,
        "filter_decisions": decision_rows,
        "can_trade": False,
        "can_promote": False,
    }


def filtered_replay_policy() -> dict[str, Any]:
    return {
        "status": "research_only",
        "executor_id": EXECUTOR_ID,
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
        "default_seen_window_policy": "exclude prior candidate_replay days",
        "filter_timing": "pre-trigger and quote-window-only; no post-fill selection",
        "promotion_rule": (
            "filtered replay pass only queues governed shadow-manifest review; "
            "it cannot promote or trade by itself"
        ),
    }


def publish_filtered_replay(
    payload: Mapping[str, Any],
    out: Path,
    feed: Path | None = None,
) -> None:
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
        "=== Filtered conservative replay ===",
        f"generated: {payload.get('generated_at')}",
        (
            "summary: "
            f"{summary.get('accepted_specs', 0)} accepted / "
            f"{summary.get('decisions', 0)} decisions, "
            f"{summary.get('replay_candidates', 0)} replay candidates"
        ),
    ]
    for row in list(payload.get("rows", []))[:limit]:
        lines.append(
            f"  {row['verdict']:<30} {row['exchange']} {row['symbol']} "
            f"{row['side']:<4} filter={row.get('filter_name', '--')} "
            f"fills={row['fills']}/{row['quotes']} net=${row['net_usd']:+.4f}"
        )
    lines.append("research-only: can_trade=false can_promote=false")
    return "\n".join(lines)


def _input_specs(
    data_root: Path | str,
    leadlag_payload: Mapping[str, Any] | None,
    orderflow_payload: Mapping[str, Any] | None,
    *,
    config: FilteredReplayConfig,
) -> list[EventReplaySpec]:
    replay_config = CandidateReplayConfig(
        max_event_leadlag_specs=config.max_event_leadlag_specs,
        max_orderflow_specs=config.max_orderflow_specs,
        warmup_ms=config.replay.warmup_ms,
        quote_deadline_ms=config.replay.quote_deadline_ms,
        ttl_ms=config.replay.ttl_ms,
        stop_bps=config.replay.stop_bps,
        target_bps=config.replay.target_bps,
        max_spread_bps=config.replay.max_spread_bps,
        notional_usd=config.replay.notional_usd,
        min_replay_fills=config.replay.min_replay_fills,
        queue_aware=config.replay.queue_aware,
    )
    specs: list[EventReplaySpec] = []
    specs.extend(_leadlag_specs(data_root, leadlag_payload, config=replay_config))
    specs.extend(_orderflow_specs(orderflow_payload, config=replay_config))
    return specs


def _filter_decision(
    data_root: Path | str,
    spec: EventReplaySpec,
    condition: Mapping[str, Any],
    seen_days: set[str],
    cache: dict[tuple[str, str, str], list[tuple[int, str, object]]],
    config: FilteredReplayConfig,
) -> tuple[FilterDecision, list[tuple[int, str, object]]]:
    events = _events_for_spec(data_root, spec, cache)
    proposal = condition.get("filter_proposal")
    proposal = proposal if isinstance(proposal, Mapping) else {}
    filter_name = str(proposal.get("filter") or "")
    bucket = str(condition.get("primary_bucket") or proposal.get("bucket") or "UNKNOWN")
    if spec.day in seen_days and not config.allow_seen_window:
        return (
            _decision(spec, False, "SEEN_REPLAY_WINDOW_EXCLUDED", filter_name, bucket, {}),
            events,
        )
    feature_row = analyze_replay_row(
        data_root,
        _spec_as_replay_row(spec),
        config=config.conditions,
        events=events,
    )
    features = _feature_snapshot(feature_row)
    accepted, reason = _passes_filter(filter_name, proposal, features, config)
    return (
        _decision(spec, accepted, reason, filter_name, bucket, features),
        events,
    )


def _passes_filter(
    filter_name: str,
    proposal: Mapping[str, Any],
    features: Mapping[str, Any],
    config: FilteredReplayConfig,
) -> tuple[bool, str]:
    if filter_name == "require_quote_window_spread_bps_lte":
        threshold = _num(proposal.get("threshold"), config.replay.max_spread_bps)
        spread = _num(features.get("min_quote_window_spread_bps"), math.inf)
        if bool(features.get("quoteable")) and spread <= threshold:
            return True, "FILTER_ACCEPTED"
        return False, "FILTER_REJECTED_SPREAD"
    if filter_name == "require_pre_event_trade_through_proxy":
        min_count = int(_num(proposal.get("min_pre_trade_count"), config.min_pre_trade_count))
        min_notional = _num(
            proposal.get("min_pre_signed_notional_usd"),
            config.min_pre_signed_notional_usd,
        )
        pre_count = int(_num(features.get("pre_trade_count"), 0.0))
        pre_notional = _num(features.get("pre_signed_notional_usd"), 0.0)
        if pre_count >= min_count and pre_notional >= min_notional:
            return True, "FILTER_ACCEPTED"
        return False, "FILTER_REJECTED_PRE_TAPE"
    if filter_name == "block_adverse_selection_context":
        pre_notional = _num(features.get("pre_signed_notional_usd"), 0.0)
        if pre_notional >= 0.0:
            return True, "FILTER_ACCEPTED"
        return False, "FILTER_REJECTED_ADVERSE_PRE_TAPE"
    if filter_name == "require_net_bps_buffer_above_fee_wall":
        spread = _num(features.get("min_quote_window_spread_bps"), math.inf)
        if bool(features.get("quoteable")) and spread <= config.replay.max_spread_bps:
            return True, "FILTER_ACCEPTED"
        return False, "FILTER_REJECTED_FEE_WALL_CONTEXT"
    return False, "UNSUPPORTED_FILTER"


def _augment_row(row: CandidateReplayRow, condition: Mapping[str, Any]) -> dict[str, Any]:
    evidence = dict(row.evidence)
    proposal = condition.get("filter_proposal")
    proposal = proposal if isinstance(proposal, Mapping) else {}
    evidence["filtered_replay"] = {
        "executor_id": EXECUTOR_ID,
        "condition_bucket": condition.get("primary_bucket"),
        "filter_proposal": dict(proposal),
        "fresh_replay_required": True,
    }
    payload = row.to_dict()
    payload["evidence"] = evidence
    payload["filtered_replay"] = True
    payload["filter_name"] = str(proposal.get("filter") or "")
    payload["condition_bucket"] = str(condition.get("primary_bucket") or "")
    return payload


def _condition_index(payload: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    rows = (payload or {}).get("candidate_conditions") or []
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("recommended_action")) != "RUN_FILTERED_REPLAY_FROM_EXECUTION_CONDITIONS":
            continue
        candidate_id = str(row.get("candidate_id") or "")
        if candidate_id:
            out[candidate_id] = row
    return out


def _seen_replay_days(payload: Mapping[str, Any] | None) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for row in (payload or {}).get("rows", []) or []:
        if not isinstance(row, Mapping):
            continue
        candidate_id = str(row.get("candidate_id") or "")
        day = str(row.get("day") or "")
        if candidate_id and day:
            out.setdefault(candidate_id, set()).add(day)
    return out


def _events_for_spec(
    data_root: Path | str,
    spec: EventReplaySpec,
    cache: dict[tuple[str, str, str], list[tuple[int, str, object]]],
) -> list[tuple[int, str, object]]:
    key = (spec.exchange, spec.symbol, spec.day)
    if key not in cache:
        try:
            cache[key] = load_tick_events(data_root, spec.exchange, spec.symbol, spec.day)
        except (FileNotFoundError, ValueError, KeyError) as exc:
            logger.warning(
                "failed to load tick events for filtered replay %s %s %s: %s",
                spec.exchange,
                spec.symbol,
                spec.day,
                exc,
            )
            cache[key] = []
    return cache[key]


def _spec_as_replay_row(spec: EventReplaySpec) -> dict[str, Any]:
    return {
        "candidate_id": spec.candidate_id,
        "source": spec.source,
        "family": spec.family,
        "exchange": spec.exchange,
        "symbol": spec.symbol,
        "day": spec.day,
        "side": spec.side,
        "trigger_ts": datetime.fromtimestamp(spec.trigger_ts_ms / 1000, tz=UTC).isoformat(),
        "verdict": "NO_FILLS",
    }


def _feature_snapshot(row: Any) -> dict[str, Any]:
    return {
        "quoteable": bool(row.quoteable),
        "first_book_delay_ms": row.first_book_delay_ms,
        "first_spread_bps": row.first_spread_bps,
        "min_quote_window_spread_bps": row.min_quote_window_spread_bps,
        "pre_book_count": row.pre_book_count,
        "pre_trade_count": row.pre_trade_count,
        "pre_signed_notional_usd": row.pre_signed_notional_usd,
        "quote_window_book_count": row.quote_window_book_count,
        "touch_trade_count": row.touch_trade_count,
        "through_trade_count": row.through_trade_count,
    }


def _decision(
    spec: EventReplaySpec,
    accepted: bool,
    reason: str,
    filter_name: str,
    bucket: str,
    features: Mapping[str, Any],
) -> FilterDecision:
    return FilterDecision(
        candidate_id=spec.candidate_id,
        exchange=spec.exchange,
        symbol=spec.symbol,
        day=spec.day,
        side=spec.side,
        trigger_ts=datetime.fromtimestamp(spec.trigger_ts_ms / 1000, tz=UTC).isoformat(),
        accepted=accepted,
        reason=reason,
        filter_name=filter_name,
        bucket=bucket,
        feature_snapshot=dict(features),
    )


def _summary(
    rows: Sequence[Mapping[str, Any]],
    decisions: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    verdicts = Counter(str(row.get("verdict", "UNKNOWN")) for row in rows)
    reasons = Counter(str(row.get("reason", "UNKNOWN")) for row in decisions)
    accepted = sum(1 for row in decisions if row.get("accepted"))
    return {
        "rows": len(rows),
        "decisions": len(decisions),
        "accepted_specs": accepted,
        "rejected_by_filter": len(decisions) - accepted,
        "decision_reasons": dict(reasons),
        "verdicts": dict(verdicts),
        "quotes": sum(int(row.get("quotes", 0) or 0) for row in rows),
        "fills": sum(int(row.get("fills", 0) or 0) for row in rows),
        "net_usd": round(sum(float(row.get("net_usd", 0.0) or 0.0) for row in rows), 6),
        "replay_candidates": verdicts.get("REPLAY_CANDIDATE", 0),
        "can_trade": False,
        "can_promote": False,
    }


def _row_sort_key(row: Mapping[str, Any]) -> tuple[int, float, str]:
    rank = {
        "REPLAY_CANDIDATE": 0,
        "UNDER_SAMPLED_POSITIVE_REPLAY": 1,
        "NEGATIVE_EDGE_AFTER_REPLAY": 5,
        "NO_FILLS": 6,
        "NO_QUOTE": 7,
    }.get(str(row.get("verdict")), 8)
    return (rank, -float(row.get("avg_net_bps") or -999.0), str(row.get("candidate_id") or ""))


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        logger.warning("failed to parse filtered replay input %s", path)
        return None


def _num(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run filtered conservative replay")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--event-leadlag", default=str(DEFAULT_EVENT_LEADLAG))
    parser.add_argument("--orderflow", default=str(DEFAULT_ORDERFLOW))
    parser.add_argument("--candidate-replay", default=str(DEFAULT_CANDIDATE_REPLAY))
    parser.add_argument("--conditions", default=str(DEFAULT_CONDITIONS))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--feed", default=str(DEFAULT_FEED))
    parser.add_argument("--max-event-leadlag", type=int, default=12)
    parser.add_argument("--max-orderflow", type=int, default=60)
    parser.add_argument("--allow-seen-window", action="store_true")
    parser.add_argument("--interval-seconds", type=int, default=0)
    parser.add_argument("--no-publish", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--limit", type=int, default=30)
    return parser.parse_args(argv)


def _run_once(args: argparse.Namespace) -> None:
    config = FilteredReplayConfig(
        allow_seen_window=bool(args.allow_seen_window),
        max_event_leadlag_specs=args.max_event_leadlag,
        max_orderflow_specs=args.max_orderflow,
    )
    payload = run_filtered_replay(
        args.data_root,
        event_leadlag_path=args.event_leadlag,
        orderflow_path=args.orderflow,
        candidate_replay_path=args.candidate_replay,
        condition_path=args.conditions,
        config=config,
    )
    if not args.no_publish:
        publish_filtered_replay(payload, Path(args.out), Path(args.feed) if args.feed else None)
    if args.json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(render_report(payload, limit=args.limit))


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args(argv)
    while True:
        _run_once(args)
        if args.interval_seconds <= 0:
            break
        time.sleep(args.interval_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
