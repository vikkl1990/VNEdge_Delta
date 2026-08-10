"""Conservative replay executor for mined scalper candidates.

The alpha miners nominate *events*:

* cross-venue lead/lag says "after this leader candle, quote the follower"
* orderflow footprint says "after this signed-flow bar, quote this lane"

Those rows are not signals. This module is the missing proof step between a
research candidate and any shadow/paper proposal: it turns each event into a
single passive quote and runs the existing tick/L2 replay fill model.

Research-only invariants:

* event is known only after its bar closes
* quote is passive maker-first, never a touch fill
* fill requires the existing conservative trade-through model
* exit is taker with fee + slippage + safety buffer
* result never trades, promotes, or changes runtime lane state
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

import pandas as pd

from vnedge.data.parquet_store import ParquetStore
from vnedge.research.event_leadlag_alpha import (
    LeadLagFilter,
    LeadLagMinerConfig,
    prepare_lane,
)
from vnedge.scalping.microstructure import TopOfBook
from vnedge.scalping.parameter_registry import DEFAULT_SCALPER_PARAMETER_REGISTRY, RouteGate
from vnedge.scalping.replay_backtester import (
    ReplayFees,
    ReplayQuote,
    ReplayResult,
    TickReplayBacktester,
    load_tick_events,
)

logger = logging.getLogger(__name__)

EXECUTOR_ID = "candidate_replay_executor_v1"
DEFAULT_EVENT_LEADLAG = Path("research/live_research/event_leadlag_latest.json")
DEFAULT_ORDERFLOW = Path("research/live_research/orderflow_footprint_latest.json")
DEFAULT_OUT = Path("research/live_research/candidate_replay_latest.json")
DEFAULT_FEED = Path("research/live_research/candidate_replay_feed.jsonl")


def replay_policy() -> dict[str, Any]:
    return {
        "status": "research_only",
        "executor_id": EXECUTOR_ID,
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
        "requires_conservative_replay": True,
        "requires_untouched_judgment": True,
        "requires_human_approval": True,
        "fill_model": "passive maker quote; conservative trade-through fill",
        "entry_timing": "event is known only after the source bar closes",
        "promotion_rule": (
            "positive rows are proof tasks only; they need untouched judgment, "
            "paper/shadow trial, and explicit human approval before promotion"
        ),
    }


@dataclass(frozen=True)
class EventReplaySpec:
    candidate_id: str
    source: str
    family: str
    exchange: str
    symbol: str
    day: str
    side: str  # "buy" | "sell"
    trigger_ts_ms: int
    horizon_ms: int
    event_count: int = 1
    evidence: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.side not in ("buy", "sell"):
            raise ValueError(f"invalid replay side: {self.side}")
        if self.trigger_ts_ms <= 0 or self.horizon_ms <= 0:
            raise ValueError("trigger_ts_ms and horizon_ms must be positive")

    @property
    def end_ts_ms(self) -> int:
        return self.trigger_ts_ms + self.horizon_ms

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CandidateReplayConfig:
    warmup_ms: int = 5 * 60_000
    quote_deadline_ms: int = 2_000
    ttl_ms: int = 3_000
    stop_bps: float = 6.0
    target_bps: float = 8.0
    max_spread_bps: float = 3.0
    notional_usd: float = 100.0
    max_event_leadlag_specs: int = 3
    max_orderflow_specs: int = 20
    min_replay_fills: int = 5
    queue_aware: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TimedEventScalper:
    """One passive quote after a known event, then never again."""

    def __init__(self, spec: EventReplaySpec, config: CandidateReplayConfig) -> None:
        self.spec = spec
        self.config = config
        self._done = False
        self.expired_without_quote = False

    def quote(self, _features, top: TopOfBook) -> ReplayQuote | None:
        if self._done:
            return None
        ts_ms = _ts_ms(top.event_time)
        if ts_ms < self.spec.trigger_ts_ms:
            return None
        if ts_ms > self.spec.trigger_ts_ms + self.config.quote_deadline_ms:
            self._done = True
            self.expired_without_quote = True
            return None
        if top.spread_bps > self.config.max_spread_bps:
            return None
        self._done = True
        return ReplayQuote(
            self.spec.side,
            self.config.ttl_ms,
            self.config.stop_bps,
            self.config.target_bps,
        )


@dataclass(frozen=True)
class CandidateReplayRow:
    candidate_id: str
    source: str
    family: str
    exchange: str
    symbol: str
    day: str
    side: str
    trigger_ts: str
    horizon_ms: int
    source_event_count: int
    quotes: int
    fills: int
    missed_fills: int
    open_quotes_at_end: int
    fill_rate_pct: float
    net_usd: float
    avg_net_bps: float | None
    profit_factor: float | None
    win_rate_pct: float
    avg_adverse_bps: float | None
    exit_reason_counts: dict[str, int]
    verdict: str
    can_trade: bool = False
    can_promote: bool = False
    requires_untouched_judgment: bool = True
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_candidate_replay(
    data_root: Path | str,
    *,
    event_leadlag_path: Path | str = DEFAULT_EVENT_LEADLAG,
    orderflow_path: Path | str = DEFAULT_ORDERFLOW,
    config: CandidateReplayConfig = CandidateReplayConfig(),
) -> dict[str, Any]:
    leadlag_payload = _read_json(Path(event_leadlag_path))
    orderflow_payload = _read_json(Path(orderflow_path))
    specs: list[EventReplaySpec] = []
    specs.extend(_leadlag_specs(data_root, leadlag_payload, config=config))
    specs.extend(_orderflow_specs(orderflow_payload, config=config))

    rows = [row.to_dict() for row in replay_specs(data_root, specs, config=config)]
    rows.sort(key=_row_sort_key)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "executor_id": EXECUTOR_ID,
        "policy": replay_policy(),
        "config": config.to_dict(),
        "inputs": {
            "event_leadlag_path": str(event_leadlag_path),
            "orderflow_path": str(orderflow_path),
        },
        "summary": _summary(rows),
        "rows": rows,
        "can_trade": False,
        "can_promote": False,
    }


def replay_spec(
    data_root: Path | str,
    spec: EventReplaySpec,
    *,
    config: CandidateReplayConfig = CandidateReplayConfig(),
    events: list[tuple[int, str, object]] | None = None,
) -> CandidateReplayRow:
    if events is None:
        events = load_tick_events(data_root, spec.exchange, spec.symbol, spec.day)
    window_start = spec.trigger_ts_ms - config.warmup_ms
    window_end = spec.end_ts_ms
    window = [event for event in events if window_start <= event[0] <= window_end]
    fees = _fees_for_exchange(spec.exchange)
    result = TickReplayBacktester(
        fees,
        notional_usd=config.notional_usd,
        queue_aware=config.queue_aware,
    ).run(window, TimedEventScalper(spec, config))
    return _row_from_result(spec, result, config=config, event_count=spec.event_count)


def replay_specs(
    data_root: Path | str,
    specs: Sequence[EventReplaySpec],
    *,
    config: CandidateReplayConfig = CandidateReplayConfig(),
) -> list[CandidateReplayRow]:
    """Replay specs while loading each tick lane once.

    A single mined batch commonly contains many events for the same
    exchange/symbol/day. Full-day tick shards are the expensive part, so this
    cache keeps the executor usable for lead-lag hypotheses with dozens of
    reconstructed events.
    """
    # Process specs grouped by tick-lane key so each full-day tick set (millions
    # of rows, ~hundreds of MB) is loaded once AND held only while its group is
    # active — a single-entry cache, not an unbounded one. The prior dict cached
    # EVERY (exchange,symbol,day) with no eviction and grew to multi-GB across a
    # batch spanning many days (2026-07-13 OOM). Original spec order preserved.
    indexed: list[tuple[int, CandidateReplayRow]] = []
    cur_key: tuple[str, str, str] | None = None
    cur_events: list[tuple[int, str, object]] | None = None
    order = sorted(range(len(specs)), key=lambda i: (
        specs[i].exchange, specs[i].symbol, specs[i].day))
    for i in order:
        spec = specs[i]
        key = (spec.exchange, spec.symbol, spec.day)
        if key != cur_key:
            cur_events = load_tick_events(data_root, spec.exchange, spec.symbol, spec.day)
            cur_key = key
        indexed.append((i, replay_spec(data_root, spec, config=config, events=cur_events)))
    return [row for _, row in sorted(indexed)]


def _row_from_result(
    spec: EventReplaySpec,
    result: ReplayResult,
    *,
    config: CandidateReplayConfig,
    event_count: int,
) -> CandidateReplayRow:
    nets = [trade.net_bps for trade in result.trades]
    wins = [value for value in nets if value > 0.0]
    losses = [-value for value in nets if value < 0.0]
    profit_factor = (
        sum(wins) / sum(losses)
        if wins and losses
        else (999.0 if wins else None)
    )
    avg_net = sum(nets) / len(nets) if nets else None
    avg_adverse = (
        sum(trade.adverse_bps for trade in result.trades) / len(result.trades)
        if result.trades else None
    )
    exits: dict[str, int] = {}
    for trade in result.trades:
        exits[trade.exit_reason] = exits.get(trade.exit_reason, 0) + 1
    fill_rate = result.fill_rate * 100.0
    return CandidateReplayRow(
        candidate_id=spec.candidate_id,
        source=spec.source,
        family=spec.family,
        exchange=spec.exchange,
        symbol=spec.symbol,
        day=spec.day,
        side=spec.side,
        trigger_ts=datetime.fromtimestamp(spec.trigger_ts_ms / 1000, tz=UTC).isoformat(),
        horizon_ms=spec.horizon_ms,
        source_event_count=event_count,
        quotes=result.quotes_placed,
        fills=result.filled,
        missed_fills=result.missed_fills,
        open_quotes_at_end=result.open_quotes_at_end,
        fill_rate_pct=round(fill_rate, 3),
        net_usd=round(result.net_usd, 6),
        avg_net_bps=_round(avg_net, 6),
        profit_factor=_round(profit_factor, 6),
        win_rate_pct=round(len(wins) / len(nets) * 100.0, 3) if nets else 0.0,
        avg_adverse_bps=_round(avg_adverse, 6),
        exit_reason_counts=exits,
        verdict=_verdict(result, avg_net, profit_factor, fill_rate, config),
        evidence=spec.evidence,
    )


def _verdict(
    result: ReplayResult,
    avg_net_bps: float | None,
    profit_factor: float | None,
    fill_rate_pct: float,
    config: CandidateReplayConfig,
) -> str:
    if result.quotes_placed == 0:
        return "NO_QUOTE"
    if result.filled == 0:
        return "NO_FILLS"
    if result.net_usd <= 0.0:
        return "NEGATIVE_EDGE_AFTER_REPLAY"
    gate = RouteGate()
    if result.filled < config.min_replay_fills:
        return "UNDER_SAMPLED_POSITIVE_REPLAY"
    if fill_rate_pct < gate.min_fill_rate_pct:
        return "REJECT_LOW_FILL_RATE"
    if avg_net_bps is None or avg_net_bps < gate.min_avg_net_bps:
        return "REJECT_BELOW_NET_BPS"
    if profit_factor is None or profit_factor < gate.maker_min_profit_factor:
        return "REJECT_BELOW_PF"
    avg_adverse = (
        sum(trade.adverse_bps for trade in result.trades) / len(result.trades)
        if result.trades else 0.0
    )
    if abs(avg_adverse) > gate.max_avg_adverse_bps:
        return "REJECT_ADVERSE_SELECTION"
    return "REPLAY_CANDIDATE"


def _leadlag_specs(
    data_root: Path | str,
    payload: Mapping[str, Any] | None,
    *,
    config: CandidateReplayConfig,
) -> list[EventReplaySpec]:
    if not payload:
        return []
    specs: list[EventReplaySpec] = []
    for row in payload.get("replay_queue", [])[: config.max_event_leadlag_specs]:
        specs.extend(_leadlag_row_specs(data_root, row))
    return specs


def _leadlag_row_specs(data_root: Path | str, row: Mapping[str, Any]) -> list[EventReplaySpec]:
    follower_exchange = str(row.get("follower_exchange") or "")
    follower_symbol = str(row.get("follower_symbol") or "")
    leader_exchange = str(row.get("leader_exchange") or "")
    leader_symbol = str(row.get("leader_symbol") or "")
    if not all((follower_exchange, follower_symbol, leader_exchange, leader_symbol)):
        return []

    horizon_min = int(float(row.get("horizon_min") or 1))
    side = "buy" if str(row.get("side")) == "long" else "sell"
    event_filter = _leadlag_filter(row.get("filter") or {})
    cfg = LeadLagMinerConfig(
        horizons_min=(horizon_min,),
        filters=(event_filter,),
        lookback_days=int(_num(row.get("lookback_days"), 60)),
    )
    store = ParquetStore(data_root)
    try:
        leader = prepare_lane(
            store.read_candles(leader_exchange, leader_symbol, cfg.timeframe),
            cfg,
        )
        follower = prepare_lane(
            store.read_candles(follower_exchange, follower_symbol, cfg.timeframe), cfg
        )
    except FileNotFoundError:
        return []
    cutoff = min(leader["timestamp"].iloc[-1], follower["timestamp"].iloc[-1]) - pd.Timedelta(
        days=cfg.lookback_days
    )
    leader = leader[leader["timestamp"] >= cutoff].reset_index(drop=True)
    follower = follower[follower["timestamp"] >= cutoff].reset_index(drop=True)
    merged = pd.merge(
        leader,
        follower,
        on="timestamp",
        how="inner",
        suffixes=("_leader", "_follower"),
    ).sort_values("timestamp").reset_index(drop=True)
    if merged.empty:
        return []
    sign = 1.0 if side == "buy" else -1.0
    signed_leader = sign * merged["ret_bps_leader"]
    signed_follower_same = sign * merged["ret_bps_follower"]
    max_same = pd.concat(
        [
            merged["abs_ret_bps_leader"] * event_filter.max_follower_same_min_ratio,
            pd.Series(event_filter.max_follower_same_min_bps, index=merged.index),
        ],
        axis=1,
    ).min(axis=1)
    mask = (
        (signed_leader >= event_filter.min_abs_leader_bps)
        & ((sign * merged["ret_z_leader"]) >= event_filter.min_abs_leader_z)
        & (merged["volume_z_leader"] >= event_filter.min_volume_z)
        & (signed_follower_same <= max_same)
        & (signed_follower_same >= -event_filter.max_follower_same_min_bps)
    )
    indices = [int(i) for i in merged.index[mask] if i + 1 < len(merged)]
    out: list[EventReplaySpec] = []
    for idx in indices:
        trigger_ts = _ts_ms(merged["timestamp"].iloc[idx + 1])
        out.append(
            EventReplaySpec(
                candidate_id=str(row.get("hypothesis_id") or row.get("candidate_id")),
                source="event_leadlag_alpha",
                family=str(row.get("family") or "cross_venue_event_leadlag"),
                exchange=follower_exchange,
                symbol=follower_symbol,
                day=datetime.fromtimestamp(trigger_ts / 1000, tz=UTC).strftime("%Y%m%d"),
                side=side,
                trigger_ts_ms=trigger_ts,
                horizon_ms=horizon_min * 60_000,
                event_count=len(indices),
                evidence={
                    "leader_exchange": leader_exchange,
                    "leader_symbol": leader_symbol,
                    "event_filter": row.get("filter") or {},
                    "hypothesis_samples": row.get("samples"),
                },
            )
        )
    return out


def _orderflow_specs(
    payload: Mapping[str, Any] | None,
    *,
    config: CandidateReplayConfig,
) -> list[EventReplaySpec]:
    if not payload:
        return []
    specs: list[EventReplaySpec] = []
    rows = payload.get("top_candidates") or payload.get("candidates") or []
    for row in rows[: config.max_orderflow_specs]:
        if str(row.get("state")) != "ORDERFLOW_CANDIDATE":
            continue
        trigger = int(float(row.get("end_ts_ms") or 0))
        if trigger <= 0:
            continue
        specs.append(
            EventReplaySpec(
                candidate_id=str(row.get("candidate_id")),
                source="orderflow_footprint",
                family=str(row.get("family") or "orderflow_footprint_v1"),
                exchange=str(row.get("exchange")),
                symbol=str(row.get("symbol")),
                day=str(
                    row.get("day")
                    or datetime.fromtimestamp(trigger / 1000, tz=UTC).strftime("%Y%m%d")
                ),
                side=str(row.get("side")),
                trigger_ts_ms=trigger,
                horizon_ms=_timeframe_ms(str(row.get("timeframe") or "60s")),
                event_count=1,
                evidence={
                    key: row.get(key)
                    for key in (
                        "score",
                        "samples",
                        "stacked_run_length",
                        "delta_ratio",
                        "price_change_bps",
                        "total_notional_usd",
                        "avg_spread_bps",
                    )
                },
            )
        )
    return specs


def publish_candidate_replay(
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
        "=== Candidate conservative replay ===",
        f"generated: {payload.get('generated_at')}",
        (
            "summary: "
            f"{summary.get('rows', 0)} rows, "
            f"{summary.get('fills', 0)} fills, "
            f"net ${summary.get('net_usd', 0.0):+.4f}, "
            f"{summary.get('replay_candidates', 0)} replay candidates"
        ),
    ]
    for row in list(payload.get("rows", []))[:limit]:
        lines.append(
            f"  {row['verdict']:<30} {row['source']:<20} "
            f"{row['exchange']} {row['symbol']} {row['side']:<4} "
            f"fills={row['fills']}/{row['quotes']} "
            f"net=${row['net_usd']:+.4f} "
            f"avg={_fmt(row['avg_net_bps'])}bps pf={_fmt(row['profit_factor'])}"
        )
    lines.append("research-only: can_trade=false can_promote=false")
    return "\n".join(lines)


def _summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    verdicts = Counter(str(row.get("verdict", "UNKNOWN")) for row in rows)
    sources = Counter(str(row.get("source", "unknown")) for row in rows)
    return {
        "rows": len(rows),
        "sources": dict(sources),
        "verdicts": dict(verdicts),
        "quotes": sum(int(row.get("quotes", 0) or 0) for row in rows),
        "fills": sum(int(row.get("fills", 0) or 0) for row in rows),
        "net_usd": round(sum(float(row.get("net_usd", 0.0) or 0.0) for row in rows), 6),
        "replay_candidates": verdicts.get("REPLAY_CANDIDATE", 0),
        "positive_under_sampled": verdicts.get("UNDER_SAMPLED_POSITIVE_REPLAY", 0),
        "can_trade": False,
        "can_promote": False,
    }


def _row_sort_key(row: Mapping[str, Any]) -> tuple[int, float, int, str]:
    rank = {
        "REPLAY_CANDIDATE": 0,
        "UNDER_SAMPLED_POSITIVE_REPLAY": 1,
        "REJECT_ADVERSE_SELECTION": 2,
        "REJECT_LOW_FILL_RATE": 3,
        "REJECT_BELOW_PF": 4,
        "REJECT_BELOW_NET_BPS": 5,
        "NEGATIVE_EDGE_AFTER_REPLAY": 6,
        "NO_FILLS": 7,
        "NO_QUOTE": 8,
    }.get(str(row.get("verdict")), 9)
    return (
        rank,
        -float(row.get("avg_net_bps") or -999.0),
        -int(row.get("fills") or 0),
        str(row.get("candidate_id") or ""),
    )


def _leadlag_filter(raw: Mapping[str, Any]) -> LeadLagFilter:
    return LeadLagFilter(
        min_abs_leader_bps=float(raw.get("min_abs_leader_bps", 4.0)),
        min_abs_leader_z=float(raw.get("min_abs_leader_z", 1.8)),
        min_volume_z=float(raw.get("min_volume_z", -0.25)),
        max_follower_same_min_ratio=float(raw.get("max_follower_same_min_ratio", 0.5)),
        max_follower_same_min_bps=float(raw.get("max_follower_same_min_bps", 6.0)),
    )


def _fees_for_exchange(exchange: str) -> ReplayFees:
    fee = DEFAULT_SCALPER_PARAMETER_REGISTRY.fee_profile(exchange)
    return ReplayFees(
        maker_bps=fee.maker_bps,
        taker_bps=fee.taker_bps,
        slippage_bps=fee.slippage_bps + fee.safety_buffer_bps,
    )


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        logger.warning("failed to parse replay input %s", path)
        return None


def _timeframe_ms(label: str) -> int:
    text = label.strip().lower()
    if text.endswith("ms"):
        return max(1, int(float(text[:-2])))
    if text.endswith("s"):
        return max(1, int(float(text[:-1]) * 1000))
    if text.endswith("m"):
        return max(1, int(float(text[:-1]) * 60_000))
    return 60_000


def _ts_ms(value: Any) -> int:
    if isinstance(value, pd.Timestamp):
        ts = value.tz_convert(UTC) if value.tzinfo else value.tz_localize(UTC)
        return int(ts.timestamp() * 1000)
    if isinstance(value, datetime):
        dt = value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
        return int(dt.timestamp() * 1000)
    return int(pd.Timestamp(value, tz=UTC).timestamp() * 1000)


def _num(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _round(value: float | None, digits: int = 3) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, digits)


def _fmt(value: Any) -> str:
    return "--" if value is None else f"{float(value):.2f}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay mined scalper candidates over tick/L2")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--event-leadlag", default=str(DEFAULT_EVENT_LEADLAG))
    parser.add_argument("--orderflow", default=str(DEFAULT_ORDERFLOW))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--feed", default=str(DEFAULT_FEED))
    parser.add_argument(
        "--max-event-leadlag",
        type=int,
        default=CandidateReplayConfig().max_event_leadlag_specs,
    )
    parser.add_argument(
        "--max-orderflow",
        type=int,
        default=CandidateReplayConfig().max_orderflow_specs,
    )
    parser.add_argument("--queue-aware", action="store_true")
    parser.add_argument("--no-publish", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--interval-seconds", type=int, default=0)
    return parser.parse_args(argv)


def _run_once(args: argparse.Namespace) -> None:
    config = CandidateReplayConfig(
        max_event_leadlag_specs=args.max_event_leadlag,
        max_orderflow_specs=args.max_orderflow,
        queue_aware=bool(args.queue_aware),
    )
    payload = run_candidate_replay(
        args.data_root,
        event_leadlag_path=args.event_leadlag,
        orderflow_path=args.orderflow,
        config=config,
    )
    if not args.no_publish:
        publish_candidate_replay(payload, Path(args.out), Path(args.feed) if args.feed else None)
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
