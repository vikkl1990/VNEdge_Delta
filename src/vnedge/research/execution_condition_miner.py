"""Mine pre-event execution conditions from conservative replay failures.

Candidate replay answers the capital-critical question: did this mined event
survive an honest passive quote, conservative fill, and fee/slippage model?
This module answers the follow-up question without changing that verdict:
*why* did the event fail, using only market information available at or before
the quote decision plus mechanical fill-path diagnostics.

Research-only invariants:

* never promotes or trades
* never treats a filtered seen-window result as proof
* proposes filter hypotheses that must be replayed on a fresh/frozen slice
* records data gaps separately from genuine negative edge
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from vnedge.research.candidate_replay_executor import CandidateReplayConfig
from vnedge.scalping.replay_backtester import load_tick_events

logger = logging.getLogger(__name__)

MINER_ID = "execution_condition_miner_v1"
DEFAULT_REPLAY = Path("research/live_research/candidate_replay_latest.json")
DEFAULT_OUT = Path("research/live_research/execution_condition_latest.json")
DEFAULT_FEED = Path("research/live_research/execution_condition_feed.jsonl")

BAD_REPLAY_VERDICTS = {
    "NO_QUOTE",
    "NO_FILLS",
    "NEGATIVE_EDGE_AFTER_REPLAY",
    "REJECT_LOW_FILL_RATE",
    "REJECT_BELOW_NET_BPS",
    "REJECT_BELOW_PF",
    "REJECT_ADVERSE_SELECTION",
}

FILTERABLE_BUCKETS = {
    "SPREAD_TOO_WIDE",
    "NO_TRADE_THROUGH",
    "TOUCH_ONLY_QUEUE_RISK",
    "LOW_FILL_RATE",
    "ADVERSE_SELECTION",
    "WRONG_WAY_DRIFT",
    "FEE_WALL_FAIL",
}


@dataclass(frozen=True)
class ExecutionConditionConfig:
    pre_window_ms: int = 60_000
    quote_deadline_ms: int = CandidateReplayConfig().quote_deadline_ms
    ttl_ms: int = CandidateReplayConfig().ttl_ms
    max_spread_bps: float = CandidateReplayConfig().max_spread_bps
    min_through_notional_usd: float = 50.0
    adverse_drift_bps: float = -2.0
    fee_wall_bps: float = 8.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ExecutionConditionRow:
    candidate_id: str
    source: str
    family: str
    exchange: str
    symbol: str
    day: str
    side: str
    trigger_ts: str
    replay_verdict: str
    reason_bucket: str
    recommended_action: str
    confidence: float
    quoteable: bool
    first_book_delay_ms: int | None
    first_spread_bps: float | None
    min_quote_window_spread_bps: float | None
    quote_price: float | None
    pre_book_count: int
    pre_trade_count: int
    pre_signed_notional_usd: float
    quote_window_book_count: int
    ttl_trade_count: int
    touch_trade_count: int
    through_trade_count: int
    through_notional_usd: float
    signed_mid_move_bps: float | None
    replay_avg_net_bps: float | None
    replay_avg_adverse_bps: float | None
    proposal: dict[str, Any] = field(default_factory=dict)
    can_trade: bool = False
    can_promote: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_execution_condition_miner(
    data_root: Path | str,
    *,
    replay_path: Path | str = DEFAULT_REPLAY,
    config: ExecutionConditionConfig = ExecutionConditionConfig(),
) -> dict[str, Any]:
    replay_payload = _read_json(Path(replay_path))
    replay_rows = [
        row for row in (replay_payload or {}).get("rows", [])
        if isinstance(row, Mapping)
    ]
    # Single-entry tick cache grouped by event key (see candidate_replay_executor):
    # load each full-day tick set once, hold only the active group — the prior
    # unbounded dict grew to multi-GB (2026-07-13 OOM). Original row order kept.
    indexed: list[tuple[int, dict]] = []
    cur_key: tuple[str, str, str] | None = None
    cur_events: list[tuple[int, str, object]] | None = None
    order = sorted(range(len(replay_rows)), key=lambda i: _row_event_key(replay_rows[i]))
    for i in order:
        row = replay_rows[i]
        key = _row_event_key(row)
        if key != cur_key:
            cur_events = _safe_load_events(data_root, *key)
            cur_key = key
        indexed.append((i, analyze_replay_row(
            data_root, row, config=config, events=cur_events,
        ).to_dict()))
    rows = [r for _, r in sorted(indexed)]
    candidate_conditions = _candidate_conditions(rows)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "miner_id": MINER_ID,
        "policy": miner_policy(),
        "config": config.to_dict(),
        "inputs": {"candidate_replay_path": str(replay_path)},
        "summary": _summary(rows, candidate_conditions),
        "candidate_conditions": candidate_conditions,
        "rows": rows,
        "can_trade": False,
        "can_promote": False,
    }


def miner_policy() -> dict[str, Any]:
    return {
        "status": "research_only",
        "miner_id": MINER_ID,
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
        "uses_seen_replay_window_for_promotion": False,
        "proposal_rule": (
            "filter proposals explain failed replay rows only; any candidate "
            "must survive a fresh conservative replay before shadow discussion"
        ),
    }


def analyze_replay_row(
    data_root: Path | str,
    replay_row: Mapping[str, Any],
    *,
    config: ExecutionConditionConfig = ExecutionConditionConfig(),
    events: list[tuple[int, str, object]] | None = None,
) -> ExecutionConditionRow:
    exchange = str(replay_row.get("exchange") or "")
    symbol = str(replay_row.get("symbol") or "")
    day = str(replay_row.get("day") or "")
    side = str(replay_row.get("side") or "")
    trigger_ts = _ts_ms(replay_row.get("trigger_ts"))
    if events is None:
        events = _safe_load_events(data_root, exchange, symbol, day)

    features = _event_features(events, trigger_ts, side, config)
    replay_verdict = str(replay_row.get("verdict") or "UNKNOWN")
    bucket = _reason_bucket(replay_verdict, replay_row, features, config)
    action = _recommended_action(bucket, replay_verdict)
    confidence = _confidence(bucket, replay_verdict, features)
    proposal = _proposal(bucket, replay_row, features, config)

    return ExecutionConditionRow(
        candidate_id=str(replay_row.get("candidate_id") or ""),
        source=str(replay_row.get("source") or ""),
        family=str(replay_row.get("family") or ""),
        exchange=exchange,
        symbol=symbol,
        day=day,
        side=side,
        trigger_ts=datetime.fromtimestamp(trigger_ts / 1000, tz=UTC).isoformat(),
        replay_verdict=replay_verdict,
        reason_bucket=bucket,
        recommended_action=action,
        confidence=round(confidence, 3),
        quoteable=bool(features["quoteable"]),
        first_book_delay_ms=features["first_book_delay_ms"],
        first_spread_bps=_round(features["first_spread_bps"]),
        min_quote_window_spread_bps=_round(features["min_quote_window_spread_bps"]),
        quote_price=_round(features["quote_price"], 8),
        pre_book_count=int(features["pre_book_count"]),
        pre_trade_count=int(features["pre_trade_count"]),
        pre_signed_notional_usd=round(float(features["pre_signed_notional_usd"]), 6),
        quote_window_book_count=int(features["quote_window_book_count"]),
        ttl_trade_count=int(features["ttl_trade_count"]),
        touch_trade_count=int(features["touch_trade_count"]),
        through_trade_count=int(features["through_trade_count"]),
        through_notional_usd=round(float(features["through_notional_usd"]), 6),
        signed_mid_move_bps=_round(features["signed_mid_move_bps"]),
        replay_avg_net_bps=_round(_num(replay_row.get("avg_net_bps"), None)),
        replay_avg_adverse_bps=_round(_num(replay_row.get("avg_adverse_bps"), None)),
        proposal=proposal,
    )


def publish_execution_conditions(
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
        "=== Execution condition miner ===",
        f"generated: {payload.get('generated_at')}",
        (
            "summary: "
            f"{summary.get('rows', 0)} rows, "
            f"{summary.get('filterable_rows', 0)} filterable, "
            f"{summary.get('data_gap_rows', 0)} data gaps"
        ),
    ]
    for row in list(payload.get("rows", []))[:limit]:
        lines.append(
            f"  {row['reason_bucket']:<24} {row['exchange']} {row['symbol']} "
            f"{row['side']:<4} replay={row['replay_verdict']:<28} "
            f"action={row['recommended_action']}"
        )
    lines.append("research-only: can_trade=false can_promote=false")
    return "\n".join(lines)


def _event_features(
    events: list[tuple[int, str, object]],
    trigger_ts: int,
    side: str,
    config: ExecutionConditionConfig,
) -> dict[str, Any]:
    pre_start = trigger_ts - config.pre_window_ms
    quote_end = trigger_ts + config.quote_deadline_ms
    pre_events = [event for event in events if pre_start <= event[0] < trigger_ts]
    quote_books = [
        (ts, obj) for ts, kind, obj in events
        if kind == "book" and trigger_ts <= ts <= quote_end
    ]
    pre_books = [event for event in pre_events if event[1] == "book"]
    pre_trades = [event for event in pre_events if event[1] == "trade"]

    first_book = quote_books[0] if quote_books else None
    acceptable_books = [
        (ts, top) for ts, top in quote_books
        if float(getattr(top, "spread_bps", math.inf)) <= config.max_spread_bps
    ]
    quote_book = acceptable_books[0] if acceptable_books else None
    quote_ts = int(quote_book[0]) if quote_book else None
    quote_top = quote_book[1] if quote_book else None
    quote_price = _quote_price(quote_top, side) if quote_top is not None else None

    ttl_trades: list[tuple[int, object]] = []
    if quote_ts is not None:
        ttl_end = quote_ts + config.ttl_ms
        ttl_trades = [
            (ts, obj) for ts, kind, obj in events
            if kind == "trade" and quote_ts <= ts <= ttl_end
        ]
    through_trades = [
        (ts, trade) for ts, trade in ttl_trades
        if quote_price is not None and _is_through_trade(trade, quote_price, side)
    ]
    touch_trades = [
        (ts, trade) for ts, trade in ttl_trades
        if quote_price is not None and _is_touch_trade(trade, quote_price, side)
    ]

    sign = 1.0 if side == "buy" else -1.0
    pre_signed_notional = sum(
        sign * float(getattr(trade, "signed_notional_usd", 0.0))
        for _, _, trade in pre_trades
    )
    through_notional = sum(
        float(getattr(trade, "price", 0.0)) * float(getattr(trade, "quantity", 0.0))
        for _, trade in through_trades
    )
    signed_mid_move = _signed_mid_move_bps(
        events,
        quote_ts,
        trigger_ts + max(config.ttl_ms, config.quote_deadline_ms),
        quote_top,
        sign,
    )

    min_spread = (
        min(float(getattr(top, "spread_bps", math.inf)) for _, top in quote_books)
        if quote_books else None
    )
    return {
        "quoteable": quote_book is not None,
        "first_book_delay_ms": (
            int(first_book[0] - trigger_ts) if first_book is not None else None
        ),
        "first_spread_bps": (
            float(getattr(first_book[1], "spread_bps", math.inf))
            if first_book is not None else None
        ),
        "min_quote_window_spread_bps": min_spread,
        "quote_price": quote_price,
        "pre_book_count": len(pre_books),
        "pre_trade_count": len(pre_trades),
        "pre_signed_notional_usd": pre_signed_notional,
        "quote_window_book_count": len(quote_books),
        "ttl_trade_count": len(ttl_trades),
        "touch_trade_count": len(touch_trades),
        "through_trade_count": len(through_trades),
        "through_notional_usd": through_notional,
        "signed_mid_move_bps": signed_mid_move,
    }


def _reason_bucket(
    verdict: str,
    row: Mapping[str, Any],
    features: Mapping[str, Any],
    config: ExecutionConditionConfig,
) -> str:
    if features["pre_book_count"] == 0 and features["quote_window_book_count"] == 0:
        return "DATA_GAP"
    if verdict == "REPLAY_CANDIDATE":
        return "REPLAY_PASSED"
    if verdict == "UNDER_SAMPLED_POSITIVE_REPLAY":
        return "UNDER_SAMPLED_POSITIVE"
    if verdict == "NO_QUOTE":
        if features["quote_window_book_count"] == 0:
            return "STALE_OR_MISSING_BOOK"
        min_spread = features["min_quote_window_spread_bps"]
        if min_spread is not None and float(min_spread) > config.max_spread_bps:
            return "SPREAD_TOO_WIDE"
        return "QUOTE_WINDOW_MISALIGNMENT"
    if verdict == "NO_FILLS":
        if not features["quoteable"]:
            return "NO_QUOTEABLE_BOOK"
        if features["through_trade_count"] == 0 and features["touch_trade_count"] > 0:
            return "TOUCH_ONLY_QUEUE_RISK"
        if features["through_trade_count"] == 0:
            return "NO_TRADE_THROUGH"
        return "LOW_FILL_RATE"
    if verdict in {"NEGATIVE_EDGE_AFTER_REPLAY", "REJECT_BELOW_NET_BPS", "REJECT_BELOW_PF"}:
        adverse = _num(row.get("avg_adverse_bps"), 0.0) or 0.0
        signed_move = features["signed_mid_move_bps"]
        avg_net = _num(row.get("avg_net_bps"), None)
        if adverse <= config.adverse_drift_bps:
            return "ADVERSE_SELECTION"
        if signed_move is not None and signed_move < config.adverse_drift_bps:
            return "WRONG_WAY_DRIFT"
        if avg_net is not None and avg_net < config.fee_wall_bps:
            return "FEE_WALL_FAIL"
        return "NEGATIVE_AFTER_COST"
    if verdict == "REJECT_LOW_FILL_RATE":
        return "LOW_FILL_RATE"
    if verdict == "REJECT_ADVERSE_SELECTION":
        return "ADVERSE_SELECTION"
    return "UNCLASSIFIED"


def _recommended_action(bucket: str, verdict: str) -> str:
    if bucket == "REPLAY_PASSED":
        return "KEEP_SHADOW_QUEUE"
    if bucket in {"DATA_GAP", "STALE_OR_MISSING_BOOK", "QUOTE_WINDOW_MISALIGNMENT"}:
        return "RECORD_MORE_TICKS"
    if bucket in FILTERABLE_BUCKETS or verdict in BAD_REPLAY_VERDICTS:
        return "RUN_FILTERED_REPLAY_FROM_EXECUTION_CONDITIONS"
    return "MINE_PRE_EVENT_EXECUTION_CONDITIONS"


def _confidence(bucket: str, verdict: str, features: Mapping[str, Any]) -> float:
    if bucket in {"DATA_GAP", "SPREAD_TOO_WIDE", "TOUCH_ONLY_QUEUE_RISK"}:
        return 0.95
    if bucket in {"NO_TRADE_THROUGH", "STALE_OR_MISSING_BOOK"}:
        return 0.9
    if bucket in {"ADVERSE_SELECTION", "FEE_WALL_FAIL", "LOW_FILL_RATE"}:
        return 0.82
    if verdict == "REPLAY_CANDIDATE":
        return 0.8
    if features["quote_window_book_count"] == 0:
        return 0.75
    return 0.65


def _proposal(
    bucket: str,
    row: Mapping[str, Any],
    features: Mapping[str, Any],
    config: ExecutionConditionConfig,
) -> dict[str, Any]:
    base = {
        "bucket": bucket,
        "must_replay_fresh_window": True,
        "does_not_promote_seen_window": True,
    }
    if bucket == "SPREAD_TOO_WIDE":
        return {
            **base,
            "filter": "require_quote_window_spread_bps_lte",
            "threshold": config.max_spread_bps,
            "observed_min_spread_bps": _round(features["min_quote_window_spread_bps"]),
            "rationale": "candidate only became quoteable when spread exceeded the maker gate",
        }
    if bucket in {"NO_TRADE_THROUGH", "TOUCH_ONLY_QUEUE_RISK", "LOW_FILL_RATE"}:
        return {
            **base,
            "filter": "require_pre_event_trade_through_proxy",
            "min_pre_signed_notional_usd": config.min_through_notional_usd,
            "observed_through_notional_usd": round(
                float(features["through_notional_usd"]), 6
            ),
            "observed_pre_signed_notional_usd": round(
                float(features["pre_signed_notional_usd"]), 6
            ),
            "rationale": "maker entry needs evidence that touch liquidity can clear",
        }
    if bucket in {"ADVERSE_SELECTION", "WRONG_WAY_DRIFT"}:
        return {
            **base,
            "filter": "block_adverse_selection_context",
            "max_adverse_drift_bps": abs(config.adverse_drift_bps),
            "observed_signed_mid_move_bps": _round(features["signed_mid_move_bps"]),
            "rationale": "fills occur when the market is moving against the quote",
        }
    if bucket == "FEE_WALL_FAIL":
        return {
            **base,
            "filter": "require_net_bps_buffer_above_fee_wall",
            "min_avg_net_bps": config.fee_wall_bps,
            "observed_avg_net_bps": _round(_num(row.get("avg_net_bps"), None)),
            "rationale": "gross move is not clearing maker+taker+slippage cost",
        }
    if bucket in {"DATA_GAP", "STALE_OR_MISSING_BOOK", "QUOTE_WINDOW_MISALIGNMENT"}:
        return {
            **base,
            "filter": "do_not_filter_yet",
            "required_data": "fresh book and trade coverage through quote window",
            "rationale": "missing market data is not evidence of negative edge",
        }
    return {
        **base,
        "filter": "manual_review",
        "rationale": "condition was not confidently classified",
    }


def _candidate_conditions(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        candidate_id = str(row.get("candidate_id") or "")
        if candidate_id:
            groups[candidate_id].append(row)
    out: list[dict[str, Any]] = []
    for candidate_id, group in groups.items():
        buckets = Counter(str(row.get("reason_bucket")) for row in group)
        actions = Counter(str(row.get("recommended_action")) for row in group)
        primary_bucket, primary_count = buckets.most_common(1)[0]
        action = _candidate_action(actions)
        primary = next(row for row in group if row.get("reason_bucket") == primary_bucket)
        out.append({
            "candidate_id": candidate_id,
            "source": str(primary.get("source") or ""),
            "family": str(primary.get("family") or ""),
            "exchange": str(primary.get("exchange") or ""),
            "symbol": str(primary.get("symbol") or ""),
            "rows": len(group),
            "primary_bucket": primary_bucket,
            "bucket_counts": dict(buckets),
            "recommended_action": action,
            "confidence": round(primary_count / max(1, len(group)), 3),
            "filter_proposal": primary.get("proposal") or {},
            "can_trade": False,
            "can_promote": False,
        })
    out.sort(key=lambda row: (
        0 if row["recommended_action"] == "RUN_FILTERED_REPLAY_FROM_EXECUTION_CONDITIONS" else 1,
        -float(row["confidence"]),
        row["candidate_id"],
    ))
    return out


def _candidate_action(actions: Counter[str]) -> str:
    if actions.get("RUN_FILTERED_REPLAY_FROM_EXECUTION_CONDITIONS"):
        return "RUN_FILTERED_REPLAY_FROM_EXECUTION_CONDITIONS"
    if actions.get("RECORD_MORE_TICKS"):
        return "RECORD_MORE_TICKS"
    if actions.get("KEEP_SHADOW_QUEUE"):
        return "KEEP_SHADOW_QUEUE"
    return actions.most_common(1)[0][0] if actions else "MINE_PRE_EVENT_EXECUTION_CONDITIONS"


def _summary(
    rows: list[Mapping[str, Any]],
    candidate_conditions: list[Mapping[str, Any]],
) -> dict[str, Any]:
    buckets = Counter(str(row.get("reason_bucket")) for row in rows)
    actions = Counter(str(row.get("recommended_action")) for row in rows)
    return {
        "rows": len(rows),
        "candidates": len(candidate_conditions),
        "buckets": dict(buckets),
        "actions": dict(actions),
        "filterable_rows": sum(buckets.get(bucket, 0) for bucket in FILTERABLE_BUCKETS),
        "data_gap_rows": (
            buckets.get("DATA_GAP", 0)
            + buckets.get("STALE_OR_MISSING_BOOK", 0)
            + buckets.get("QUOTE_WINDOW_MISALIGNMENT", 0)
        ),
        "can_trade": False,
        "can_promote": False,
    }


def _safe_load_events(
    data_root: Path | str,
    exchange: str,
    symbol: str,
    day: str,
) -> list[tuple[int, str, object]]:
    try:
        return load_tick_events(data_root, exchange, symbol, day)
    except (FileNotFoundError, ValueError, KeyError) as exc:
        logger.warning("failed to load tick events for %s %s %s: %s", exchange, symbol, day, exc)
        return []


def _row_event_key(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(row.get("exchange") or ""),
        str(row.get("symbol") or ""),
        str(row.get("day") or ""),
    )


def _quote_price(top: object | None, side: str) -> float | None:
    if top is None:
        return None
    return float(getattr(top, "bid" if side == "buy" else "ask"))


def _is_through_trade(trade: object, quote_price: float, side: str) -> bool:
    price = float(getattr(trade, "price", 0.0))
    taker_side = str(getattr(trade, "taker_side", ""))
    if side == "buy":
        return taker_side == "sell" and price < quote_price
    return taker_side == "buy" and price > quote_price


def _is_touch_trade(trade: object, quote_price: float, side: str) -> bool:
    price = float(getattr(trade, "price", 0.0))
    taker_side = str(getattr(trade, "taker_side", ""))
    if side == "buy":
        return taker_side == "sell" and price <= quote_price
    return taker_side == "buy" and price >= quote_price


def _signed_mid_move_bps(
    events: list[tuple[int, str, object]],
    quote_ts: int | None,
    end_ts: int,
    quote_top: object | None,
    sign: float,
) -> float | None:
    if quote_ts is None or quote_top is None:
        return None
    start_mid = float(getattr(quote_top, "mid_price", 0.0))
    if start_mid <= 0:
        return None
    last_top = quote_top
    for ts, kind, obj in events:
        if kind == "book" and quote_ts <= ts <= end_ts:
            last_top = obj
    end_mid = float(getattr(last_top, "mid_price", start_mid))
    return (end_mid - start_mid) / start_mid * 10_000.0 * sign


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        logger.warning("failed to parse execution-condition input %s", path)
        return None


def _ts_ms(value: Any) -> int:
    if isinstance(value, datetime):
        dt = value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
        return int(dt.timestamp() * 1000)
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value or "").replace("Z", "+00:00")
    return int(datetime.fromisoformat(text).timestamp() * 1000)


def _num(value: Any, default: float | None = 0.0) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _round(value: float | None, digits: int = 3) -> float | None:
    if value is None or not math.isfinite(float(value)):
        return None
    return round(float(value), digits)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Mine pre-event filters from conservative replay failures"
    )
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--candidate-replay", default=str(DEFAULT_REPLAY))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--feed", default=str(DEFAULT_FEED))
    parser.add_argument("--no-publish", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--interval-seconds", type=int, default=0)
    return parser.parse_args(argv)


def _run_once(args: argparse.Namespace) -> None:
    payload = run_execution_condition_miner(
        args.data_root,
        replay_path=args.candidate_replay,
    )
    if not args.no_publish:
        publish_execution_conditions(payload, Path(args.out), Path(args.feed) if args.feed else None)
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
