"""Research-only orderflow footprint miner.

This module turns recorded public trades plus top-of-book context into compact
footprint bars: buy/sell volume, delta, CVD, and a conservative stacked
imbalance proxy. It does not produce trade signals. Its only job is to nominate
orderflow anomalies for conservative tick/L2 replay.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Literal

from vnedge.research.continuous_research import _scalper_research_days
from vnedge.research.universe import ResearchTarget, load_research_targets
from vnedge.scalping.microstructure import TopOfBook, TradeTick
from vnedge.scalping.replay_backtester import load_tick_events

logger = logging.getLogger(__name__)

ORDERFLOW_FOOTPRINT_ID = "orderflow_footprint_v1"
DEFAULT_LATEST = Path("research/live_research/orderflow_footprint_latest.json")
DEFAULT_FEED = Path("research/live_research/orderflow_footprint_feed.jsonl")


@dataclass(frozen=True)
class FootprintConfig:
    bar_seconds: int = 60
    stacked_window: int = 3
    min_delta_ratio: float = 0.60
    min_price_move_bps: float = 0.50
    min_bars: int = 10
    min_trades_per_bar: int = 5
    min_bar_notional_usd: float = 5_000.0
    min_lane_notional_usd: float = 25_000.0

    @property
    def bar_ms(self) -> int:
        return self.bar_seconds * 1000

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FootprintBar:
    start_ts_ms: int
    end_ts_ms: int
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    buy_volume: float
    sell_volume: float
    buy_notional_usd: float
    sell_notional_usd: float
    trade_count: int
    buy_trade_count: int
    sell_trade_count: int
    delta_volume: float
    delta_notional_usd: float
    total_volume: float
    total_notional_usd: float
    cvd_volume: float
    cvd_notional_usd: float
    delta_ratio: float
    price_change_bps: float
    avg_spread_bps: float | None = None
    avg_book_imbalance: float | None = None
    avg_top_depth_usd: float | None = None
    book_events: int = 0
    stacked_buy_imbalance: bool = False
    stacked_sell_imbalance: bool = False
    stacked_run_length: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FootprintCandidate:
    candidate_id: str
    exchange: str
    symbol: str
    day: str
    family: str
    side: Literal["buy", "sell"]
    timeframe: str
    state: str
    route_decision: str
    samples: int
    stacked_run_length: int
    score: float
    start_ts_ms: int
    end_ts_ms: int
    delta_ratio: float
    price_change_bps: float
    cvd_notional_usd: float
    total_notional_usd: float
    trade_count: int
    avg_spread_bps: float | None
    can_trade: bool = False
    can_promote: bool = False
    requires_conservative_replay: bool = True
    requires_untouched_judgment: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FootprintLaneReport:
    exchange: str
    symbol: str
    day: str
    timeframe: str
    state: str
    bars: int
    trade_count: int
    total_notional_usd: float
    stacked_buy_bars: int
    stacked_sell_bars: int
    max_abs_delta_ratio: float
    cvd_notional_change: float
    candidates: tuple[FootprintCandidate, ...] = field(default_factory=tuple)
    reason: str | None = None
    can_trade: bool = False
    can_promote: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["candidates"] = [candidate.to_dict() for candidate in self.candidates]
        return payload


@dataclass
class _BarAccumulator:
    start_ts_ms: int
    end_ts_ms: int
    open_price: float | None = None
    high_price: float | None = None
    low_price: float | None = None
    close_price: float | None = None
    buy_volume: float = 0.0
    sell_volume: float = 0.0
    buy_notional_usd: float = 0.0
    sell_notional_usd: float = 0.0
    trade_count: int = 0
    buy_trade_count: int = 0
    sell_trade_count: int = 0
    spreads: list[float] = field(default_factory=list)
    imbalances: list[float] = field(default_factory=list)
    depths: list[float] = field(default_factory=list)

    def on_trade(self, tick: TradeTick) -> None:
        price = float(tick.price)
        qty = float(tick.quantity)
        notional = price * qty
        if self.open_price is None:
            self.open_price = price
            self.high_price = price
            self.low_price = price
        self.high_price = max(float(self.high_price), price)
        self.low_price = min(float(self.low_price), price)
        self.close_price = price
        self.trade_count += 1
        if tick.taker_side == "buy":
            self.buy_volume += qty
            self.buy_notional_usd += notional
            self.buy_trade_count += 1
        else:
            self.sell_volume += qty
            self.sell_notional_usd += notional
            self.sell_trade_count += 1

    def on_book(self, top: TopOfBook) -> None:
        self.spreads.append(top.spread_bps)
        self.imbalances.append(top.book_imbalance)
        self.depths.append(top.top_depth_usd)


def orderflow_policy(config: FootprintConfig = FootprintConfig()) -> dict[str, Any]:
    return {
        "status": "research_only",
        "miner_id": ORDERFLOW_FOOTPRINT_ID,
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
        "requires_conservative_replay": True,
        "requires_untouched_judgment": True,
        "bars": "public trade footprint bars with top-of-book context",
        "proxy_limit": (
            "stacked imbalance is inferred from consecutive signed public-flow "
            "bars; it is not proprietary footprint-by-price reconstruction"
        ),
        "route_policy": (
            "orderflow anomalies only queue conservative replay; maker/taker "
            "choice remains a replay/paper-trial decision"
        ),
        "config": config.to_dict(),
    }


def build_footprint_bars(
    events: Iterable[tuple[int, str, object]],
    *,
    config: FootprintConfig = FootprintConfig(),
) -> tuple[FootprintBar, ...]:
    if config.bar_seconds <= 0:
        raise ValueError("bar_seconds must be positive")
    if config.stacked_window <= 0:
        raise ValueError("stacked_window must be positive")

    accs: dict[int, _BarAccumulator] = {}
    for ts_ms, kind, obj in sorted(events, key=lambda row: (row[0], row[1])):
        bucket = _bucket_start(int(ts_ms), config.bar_ms)
        acc = accs.setdefault(
            bucket,
            _BarAccumulator(bucket, bucket + config.bar_ms),
        )
        if kind == "trade" and isinstance(obj, TradeTick):
            acc.on_trade(obj)
        elif kind == "book" and isinstance(obj, TopOfBook):
            acc.on_book(obj)

    rows: list[dict[str, Any]] = []
    cvd_volume = 0.0
    cvd_notional = 0.0
    for acc in (accs[start] for start in sorted(accs)):
        if acc.trade_count <= 0 or acc.open_price is None or acc.close_price is None:
            continue
        delta_volume = acc.buy_volume - acc.sell_volume
        delta_notional = acc.buy_notional_usd - acc.sell_notional_usd
        total_volume = acc.buy_volume + acc.sell_volume
        total_notional = acc.buy_notional_usd + acc.sell_notional_usd
        cvd_volume += delta_volume
        cvd_notional += delta_notional
        rows.append({
            "start_ts_ms": acc.start_ts_ms,
            "end_ts_ms": acc.end_ts_ms,
            "open_price": round(float(acc.open_price), 12),
            "high_price": round(float(acc.high_price), 12),
            "low_price": round(float(acc.low_price), 12),
            "close_price": round(float(acc.close_price), 12),
            "buy_volume": round(acc.buy_volume, 12),
            "sell_volume": round(acc.sell_volume, 12),
            "buy_notional_usd": round(acc.buy_notional_usd, 6),
            "sell_notional_usd": round(acc.sell_notional_usd, 6),
            "trade_count": acc.trade_count,
            "buy_trade_count": acc.buy_trade_count,
            "sell_trade_count": acc.sell_trade_count,
            "delta_volume": round(delta_volume, 12),
            "delta_notional_usd": round(delta_notional, 6),
            "total_volume": round(total_volume, 12),
            "total_notional_usd": round(total_notional, 6),
            "cvd_volume": round(cvd_volume, 12),
            "cvd_notional_usd": round(cvd_notional, 6),
            "delta_ratio": round(_safe_ratio(delta_notional, total_notional), 6),
            "price_change_bps": round(
                _price_change_bps(float(acc.open_price), float(acc.close_price)), 6
            ),
            "avg_spread_bps": _round_or_none(_mean(acc.spreads), 6),
            "avg_book_imbalance": _round_or_none(_mean(acc.imbalances), 6),
            "avg_top_depth_usd": _round_or_none(_mean(acc.depths), 6),
            "book_events": len(acc.spreads),
        })

    out: list[FootprintBar] = []
    run_side: Literal["buy", "sell"] | None = None
    run_length = 0
    previous_start: int | None = None
    for row in rows:
        side = _stack_side(row, config)
        consecutive = (
            previous_start is None
            or int(row["start_ts_ms"]) == previous_start + config.bar_ms
        )
        if side is None or not consecutive:
            run_side = None
            run_length = 0
        if side is not None:
            if side == run_side:
                run_length += 1
            else:
                run_side = side
                run_length = 1
        row["stacked_buy_imbalance"] = side == "buy" and run_length >= config.stacked_window
        row["stacked_sell_imbalance"] = side == "sell" and run_length >= config.stacked_window
        row["stacked_run_length"] = run_length if side is not None else 0
        out.append(FootprintBar(**row))
        previous_start = int(row["start_ts_ms"])
    return tuple(out)


def mine_orderflow_footprints(
    data_root: Path | str,
    targets: Iterable[ResearchTarget],
    days: Iterable[str],
    *,
    config: FootprintConfig = FootprintConfig(),
    max_candidates_per_lane: int = 20,
) -> tuple[FootprintLaneReport, ...]:
    root = Path(data_root)
    reports: list[FootprintLaneReport] = []
    for target in targets:
        for day in days:
            try:
                events = load_tick_events(root, target.exchange, target.symbol, day)
            except (OSError, ValueError) as exc:
                reports.append(_missing_lane(target, day, f"failed to load tick data: {exc}"))
                continue
            reports.append(
                _lane_report(
                    target,
                    day,
                    build_footprint_bars(events, config=config),
                    config,
                    max_candidates=max_candidates_per_lane,
                    missing_stream=not events,
                )
            )
    return tuple(reports)


def run_orderflow_footprint(
    data_root: Path | str = "data",
    *,
    targets: tuple[ResearchTarget, ...] | None = None,
    days: tuple[str, ...] | None = None,
    config: FootprintConfig = FootprintConfig(),
    max_candidates: int = 100,
) -> dict[str, Any]:
    root = Path(data_root)
    targets = targets or load_research_targets()
    days = days if days is not None else _scalper_research_days(root, targets)
    reports = mine_orderflow_footprints(root, targets, days, config=config)
    candidates = sorted(
        (candidate for report in reports for candidate in report.candidates),
        key=_candidate_sort_key,
    )[:max_candidates]
    lanes = [report.to_dict() for report in reports]
    top_candidates = [candidate.to_dict() for candidate in candidates]
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "miner_id": ORDERFLOW_FOOTPRINT_ID,
        "policy": orderflow_policy(config),
        "targets": [asdict(target) for target in targets],
        "days": list(days),
        "summary": _summary(lanes, top_candidates),
        "lanes": lanes,
        "candidates": top_candidates,
        "top_candidates": top_candidates,
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
    }


def publish_orderflow_footprint(
    payload: dict[str, Any],
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


def _lane_report(
    target: ResearchTarget,
    day: str,
    bars: tuple[FootprintBar, ...],
    config: FootprintConfig,
    *,
    max_candidates: int,
    missing_stream: bool,
) -> FootprintLaneReport:
    if missing_stream:
        return _missing_lane(target, day, "missing recorded book/trade stream")
    total_notional = sum(bar.total_notional_usd for bar in bars)
    trade_count = sum(bar.trade_count for bar in bars)
    candidates = sorted(
        (
            _candidate_from_bar(target, day, bar, config)
            for bar in bars
            if bar.stacked_buy_imbalance or bar.stacked_sell_imbalance
        ),
        key=_candidate_sort_key,
    )[:max_candidates]
    if len(bars) < config.min_bars:
        state = "UNDER_SAMPLED_ORDERFLOW"
        reason = f"only {len(bars)} footprint bars; need >= {config.min_bars}"
    elif total_notional < config.min_lane_notional_usd:
        state = "UNDER_SAMPLED_ORDERFLOW"
        reason = (
            f"only ${total_notional:.0f} lane notional; "
            f"need >= ${config.min_lane_notional_usd:.0f}"
        )
    elif candidates:
        state = "ORDERFLOW_CANDIDATE"
        reason = None
    else:
        state = "NO_ORDERFLOW_CANDIDATE"
        reason = "no consecutive signed-flow stack cleared proxy thresholds"
    return FootprintLaneReport(
        exchange=target.exchange,
        symbol=target.symbol,
        day=day,
        timeframe=f"{config.bar_seconds}s",
        state=state,
        bars=len(bars),
        trade_count=trade_count,
        total_notional_usd=round(total_notional, 6),
        stacked_buy_bars=sum(1 for bar in bars if bar.stacked_buy_imbalance),
        stacked_sell_bars=sum(1 for bar in bars if bar.stacked_sell_imbalance),
        max_abs_delta_ratio=round(max((abs(bar.delta_ratio) for bar in bars), default=0.0), 6),
        cvd_notional_change=round(
            bars[-1].cvd_notional_usd - bars[0].cvd_notional_usd
            if len(bars) >= 2 else (bars[0].cvd_notional_usd if bars else 0.0),
            6,
        ),
        candidates=tuple(candidates),
        reason=reason,
    )


def _missing_lane(target: ResearchTarget, day: str, reason: str) -> FootprintLaneReport:
    return FootprintLaneReport(
        exchange=target.exchange,
        symbol=target.symbol,
        day=day,
        timeframe="60s",
        state="MISSING_TICK_DATA",
        bars=0,
        trade_count=0,
        total_notional_usd=0.0,
        stacked_buy_bars=0,
        stacked_sell_bars=0,
        max_abs_delta_ratio=0.0,
        cvd_notional_change=0.0,
        reason=reason,
    )


def _candidate_from_bar(
    target: ResearchTarget,
    day: str,
    bar: FootprintBar,
    config: FootprintConfig,
) -> FootprintCandidate:
    side: Literal["buy", "sell"] = "buy" if bar.stacked_buy_imbalance else "sell"
    enough = (
        bar.trade_count >= config.min_trades_per_bar
        and bar.total_notional_usd >= config.min_bar_notional_usd
    )
    state = "ORDERFLOW_CANDIDATE" if enough else "UNDER_SAMPLED_ORDERFLOW"
    score = _candidate_score(bar)
    return FootprintCandidate(
        candidate_id=(
            f"orderflow_footprint|{target.exchange}|{target.symbol}|"
            f"{day}|{bar.start_ts_ms}|{side}"
        ),
        exchange=target.exchange,
        symbol=target.symbol,
        day=day,
        family=ORDERFLOW_FOOTPRINT_ID,
        side=side,
        timeframe=f"{config.bar_seconds}s",
        state=state,
        route_decision="REPLAY_REQUIRED",
        samples=bar.trade_count,
        stacked_run_length=bar.stacked_run_length,
        score=score,
        start_ts_ms=bar.start_ts_ms,
        end_ts_ms=bar.end_ts_ms,
        delta_ratio=bar.delta_ratio,
        price_change_bps=bar.price_change_bps,
        cvd_notional_usd=bar.cvd_notional_usd,
        total_notional_usd=bar.total_notional_usd,
        trade_count=bar.trade_count,
        avg_spread_bps=bar.avg_spread_bps,
    )


def _summary(lanes: list[dict[str, Any]], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    states = Counter(str(row.get("state", "UNKNOWN")) for row in lanes)
    candidate_states = Counter(str(row.get("state", "UNKNOWN")) for row in candidates)
    return {
        "lanes": len(lanes),
        "lanes_with_bars": sum(1 for row in lanes if int(row.get("bars", 0) or 0) > 0),
        "bars": sum(int(row.get("bars", 0) or 0) for row in lanes),
        "trade_count": sum(int(row.get("trade_count", 0) or 0) for row in lanes),
        "candidates": len(candidates),
        "states": dict(states),
        "candidate_states": dict(candidate_states),
        "can_trade": False,
        "can_promote": False,
        "top_candidate": candidates[0]["candidate_id"] if candidates else None,
    }


def _candidate_sort_key(
    candidate: FootprintCandidate | dict[str, Any],
) -> tuple[int, float, int, str]:
    if isinstance(candidate, FootprintCandidate):
        state = candidate.state
        score = candidate.score
        samples = candidate.samples
        candidate_id = candidate.candidate_id
    else:
        state = str(candidate.get("state", "UNKNOWN"))
        score = _num(candidate.get("score"))
        samples = int(_num(candidate.get("samples")))
        candidate_id = str(candidate.get("candidate_id", ""))
    state_rank = {
        "ORDERFLOW_CANDIDATE": 0,
        "UNDER_SAMPLED_ORDERFLOW": 1,
    }.get(state, 2)
    return (state_rank, -score, -samples, candidate_id)


def _candidate_score(bar: FootprintBar) -> float:
    score = (
        abs(bar.delta_ratio) * 45.0
        + min(abs(bar.price_change_bps), 50.0) * 0.75
        + min(math.log10(max(bar.total_notional_usd, 1.0)), 7.0) * 4.0
        + bar.stacked_run_length * 7.5
    )
    if bar.avg_spread_bps is not None:
        score -= min(max(bar.avg_spread_bps, 0.0), 20.0)
    return round(score, 4)


def _stack_side(row: dict[str, Any], config: FootprintConfig) -> Literal["buy", "sell"] | None:
    delta_ratio = float(row["delta_ratio"])
    price_change = float(row["price_change_bps"])
    if (
        delta_ratio >= config.min_delta_ratio
        and price_change >= config.min_price_move_bps
    ):
        return "buy"
    if (
        delta_ratio <= -config.min_delta_ratio
        and price_change <= -config.min_price_move_bps
    ):
        return "sell"
    return None


def _bucket_start(ts_ms: int, bar_ms: int) -> int:
    return ts_ms - (ts_ms % bar_ms)


def _safe_ratio(num: float, den: float) -> float:
    return num / den if den else 0.0


def _price_change_bps(open_price: float, close_price: float) -> float:
    return (close_price - open_price) / open_price * 10_000.0 if open_price > 0 else 0.0


def _mean(values: Iterable[float]) -> float | None:
    clean = [float(v) for v in values if math.isfinite(float(v))]
    return sum(clean) / len(clean) if clean else None


def _round_or_none(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


def _num(value: Any) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    return out if math.isfinite(out) else 0.0


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _split_csv(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _parse_days(
    raw: str | None,
    data_root: Path,
    targets: tuple[ResearchTarget, ...],
) -> tuple[str, ...]:
    if not raw or raw.lower() == "latest":
        return _scalper_research_days(data_root, targets)
    return _split_csv(raw)


def _config_from_args(args: argparse.Namespace) -> FootprintConfig:
    return FootprintConfig(
        bar_seconds=args.bar_seconds,
        stacked_window=args.stacked_window,
        min_delta_ratio=args.min_delta_ratio,
        min_price_move_bps=args.min_price_move_bps,
        min_bars=args.min_bars,
        min_trades_per_bar=args.min_trades_per_bar,
        min_bar_notional_usd=args.min_bar_notional_usd,
        min_lane_notional_usd=args.min_lane_notional_usd,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="mine public-trade orderflow footprints")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--out", default=str(DEFAULT_LATEST))
    parser.add_argument("--feed", default=str(DEFAULT_FEED))
    parser.add_argument("--days", default="latest")
    parser.add_argument("--exchanges")
    parser.add_argument("--symbols")
    parser.add_argument("--bar-seconds", type=int,
                        default=_env_int("ORDERFLOW_FOOTPRINT_BAR_SECONDS", 60))
    parser.add_argument("--stacked-window", type=int,
                        default=_env_int("ORDERFLOW_FOOTPRINT_STACKED_WINDOW", 3))
    parser.add_argument("--min-delta-ratio", type=float,
                        default=_env_float("ORDERFLOW_FOOTPRINT_MIN_DELTA_RATIO", 0.60))
    parser.add_argument("--min-price-move-bps", type=float,
                        default=_env_float("ORDERFLOW_FOOTPRINT_MIN_PRICE_MOVE_BPS", 0.50))
    parser.add_argument("--min-bars", type=int,
                        default=_env_int("ORDERFLOW_FOOTPRINT_MIN_BARS", 10))
    parser.add_argument("--min-trades-per-bar", type=int,
                        default=_env_int("ORDERFLOW_FOOTPRINT_MIN_TRADES_PER_BAR", 5))
    parser.add_argument("--min-bar-notional-usd", type=float,
                        default=_env_float("ORDERFLOW_FOOTPRINT_MIN_BAR_NOTIONAL_USD", 5_000.0))
    parser.add_argument("--min-lane-notional-usd", type=float,
                        default=_env_float("ORDERFLOW_FOOTPRINT_MIN_LANE_NOTIONAL_USD", 25_000.0))
    parser.add_argument("--max-candidates", type=int,
                        default=_env_int("ORDERFLOW_FOOTPRINT_MAX_CANDIDATES", 100))
    parser.add_argument("--interval-seconds", type=int,
                        default=_env_int("ORDERFLOW_FOOTPRINT_INTERVAL_SECONDS", 0))
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    root = Path(args.data_root)
    targets = load_research_targets(
        exchanges=_split_csv(args.exchanges) or None,
        symbols=_split_csv(args.symbols) or None,
    )
    config = _config_from_args(args)
    out = Path(args.out)
    feed = Path(args.feed) if args.feed else None

    while True:
        started = time.time()
        days = _parse_days(args.days, root, targets)
        payload = run_orderflow_footprint(
            root,
            targets=targets,
            days=days,
            config=config,
            max_candidates=args.max_candidates,
        )
        publish_orderflow_footprint(payload, out, feed)
        logger.info(
            "orderflow footprint: %d lanes, %d bars, %d candidates, %.1fs -> %s",
            payload["summary"]["lanes"],
            payload["summary"]["bars"],
            payload["summary"]["candidates"],
            time.time() - started,
            out,
        )
        if args.json:
            print(json.dumps(payload, indent=2, default=str))
        if args.interval_seconds <= 0:
            break
        time.sleep(args.interval_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
