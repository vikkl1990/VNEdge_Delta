"""Fast L2 scout.

Research-only microstructure scout for interactive edge hunting. The full
``l2_research_loop`` runs conservative replay and context mining over whole
recorded days; that is intentionally slow. This scout reads only the newest
tick/L2 shards per lane, mines the same fee-aware edge hypotheses, and writes
a compact report for operators.

It never trades, never promotes, and never changes lane state. A positive row
here is only a pointer for the heavy replay/tournament path.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from vnedge.research.continuous_research import _scalper_research_days
from vnedge.research.scalper_edge_miner import (
    EdgeHypothesisResult,
    EdgeMinerConfig,
    mine_events,
)
from vnedge.research.scalper_lane_filters import (
    LaneFilterConfig,
    LaneFilterDecision,
    LaneFilterEvidence,
    evaluate_lane_filters,
    lane_filter_policy,
    summarize_filter_decisions,
)
from vnedge.research.universe import ResearchTarget, load_research_targets
from vnedge.scalping.microstructure import TopOfBook, TradeTick
from vnedge.scalping.parameter_registry import DEFAULT_SCALPER_PARAMETER_REGISTRY

logger = logging.getLogger(__name__)

L2_SCOUT_LATEST = "l2_scout_latest.json"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _split_csv(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _safe_symbol(symbol: str) -> str:
    return symbol.split(":")[0].replace("/", "")


def _stream_files(stream_base: Path, day: str, *, max_shards: int) -> list[Path]:
    shard_dir = stream_base / day
    if shard_dir.is_dir():
        return sorted(shard_dir.glob("*.parquet"))[-max_shards:]
    single = stream_base / f"{day}.parquet"
    return [single] if single.exists() else []


def _read_stream_tail(
    stream_base: Path,
    day: str,
    *,
    max_shards: int,
) -> pd.DataFrame | None:
    frames: list[pd.DataFrame] = []
    for path in _stream_files(stream_base, day, max_shards=max_shards):
        try:
            frames.append(pd.read_parquet(path))
        except (OSError, ValueError):
            logger.warning("failed reading scout shard: %s", path)
    if not frames:
        return None
    return pd.concat(frames, ignore_index=True).sort_values("ts_ms")


def load_recent_tick_events(
    data_root: Path | str,
    exchange: str,
    symbol: str,
    day: str,
    *,
    lookback_minutes: int = 30,
    max_shards: int = 40,
) -> tuple[list[tuple[int, str, object]], dict]:
    """Load only the newest shard slice for a recorded exchange/symbol/day."""
    root = Path(data_root) / "ticks" / f"exchange={exchange}"
    base = root / f"symbol={_safe_symbol(symbol)}"
    book_df = _read_stream_tail(base / "stream=book", day, max_shards=max_shards)
    trade_df = _read_stream_tail(base / "stream=trades", day, max_shards=max_shards)
    if book_df is None or trade_df is None:
        return [], {
            "book_rows": 0 if book_df is None else len(book_df),
            "trade_rows": 0 if trade_df is None else len(trade_df),
            "span_seconds": 0.0,
            "missing_stream": True,
        }

    end_ts = int(max(book_df["ts_ms"].max(), trade_df["ts_ms"].max()))
    cutoff = end_ts - lookback_minutes * 60_000
    book_df = book_df[book_df["ts_ms"] >= cutoff]
    trade_df = trade_df[trade_df["ts_ms"] >= cutoff]

    events: list[tuple[int, str, object]] = []
    for r in book_df.itertuples():
        try:
            top = TopOfBook(
                symbol=symbol,
                bid=float(r.bid),
                bid_size=float(r.bid_qty),
                ask=float(r.ask),
                ask_size=float(r.ask_qty),
                event_time=datetime.fromtimestamp(int(r.ts_ms) / 1000, tz=UTC),
            )
        except (AttributeError, TypeError, ValueError):
            continue
        events.append((int(r.ts_ms), "book", top))
    for r in trade_df.itertuples():
        try:
            tick = TradeTick(
                symbol=symbol,
                price=float(r.price),
                quantity=float(r.amount),
                taker_side=str(r.side),
                event_time=datetime.fromtimestamp(int(r.ts_ms) / 1000, tz=UTC),
            )
        except (AttributeError, TypeError, ValueError):
            continue
        events.append((int(r.ts_ms), "trade", tick))

    events.sort(key=lambda e: (e[0], 0 if e[1] == "book" else 1))
    span = (events[-1][0] - events[0][0]) / 1000 if len(events) > 1 else 0.0
    return events, {
        "book_rows": int(len(book_df)),
        "trade_rows": int(len(trade_df)),
        "events": len(events),
        "span_seconds": round(span, 3),
        "lookback_minutes": lookback_minutes,
        "max_shards": max_shards,
        "missing_stream": False,
    }


def scout_policy(filter_config: LaneFilterConfig = LaneFilterConfig()) -> dict:
    registry = DEFAULT_SCALPER_PARAMETER_REGISTRY
    return {
        "status": "research_only",
        "can_trade": False,
        "can_promote": False,
        "requires_conservative_replay": True,
        "requires_untouched_judgment": True,
        "principle": (
            "fast scout rows are not signals; they only prioritize lanes for "
            "the conservative l2_research_loop replay/tournament path"
        ),
        "active_research_families": [
            f.family_id for f in registry.active_research_families()
        ],
        "tombstoned_families": [
            {"family_id": f.family_id, "evidence": f.evidence}
            for f in registry.tombstoned_families()
        ],
        "lane_filters": lane_filter_policy(filter_config),
    }


def run_fast_l2_scout(
    data_root: Path | str = "data",
    *,
    targets: tuple[ResearchTarget, ...] | None = None,
    days: tuple[str, ...] | None = None,
    lookback_minutes: int = 30,
    max_shards: int = 40,
    max_results: int = 100,
    config: EdgeMinerConfig = EdgeMinerConfig(),
    filter_config: LaneFilterConfig = LaneFilterConfig(),
) -> dict:
    root = Path(data_root)
    targets = targets or load_research_targets()
    days = days if days is not None else _scalper_research_days(root, targets)

    results: list[EdgeHypothesisResult] = []
    lanes: list[dict] = []
    filter_decisions: list[LaneFilterDecision] = []
    for target in targets:
        for day in days:
            events, stats = load_recent_tick_events(
                root,
                target.exchange,
                target.symbol,
                day,
                lookback_minutes=lookback_minutes,
                max_shards=max_shards,
            )
            evidence = LaneFilterEvidence.from_events(
                events,
                exchange=target.exchange,
                symbol=target.symbol,
                day=day,
                timeframe=target.timeframe,
                stats=stats,
            )
            filter_decision = evaluate_lane_filters(evidence, filter_config)
            filter_decisions.append(filter_decision)
            lane_results = list(
                mine_events(
                    events,
                    exchange=target.exchange,
                    symbol=target.symbol,
                    day=day,
                    config=config,
                )
            ) if events and filter_decision.passed else []
            results.extend(lane_results)
            best = lane_results[0].to_dict() if lane_results else None
            lanes.append({
                "exchange": target.exchange,
                "symbol": target.symbol,
                "timeframe": target.timeframe,
                "day": day,
                "stats": stats,
                "filter_evidence": evidence.to_dict(),
                "filter_decision": filter_decision.to_dict(),
                "best": best,
                "state": (
                    "FILTERED_LANE"
                    if not filter_decision.passed else (
                        best["state"] if best else (
                            "MISSING_TICK_DATA"
                            if stats.get("missing_stream") else "NO_EDGE_OBSERVED"
                        )
                    )
                ),
                "can_trade": False,
            })

    results = sorted(results, key=_result_sort_key)
    top = [r.to_dict() for r in results[:max_results]]
    summary = _summary(top, lanes, tuple(filter_decisions))
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "scout_id": "fast_l2_scout_v1",
        "policy": scout_policy(filter_config),
        "days": list(days),
        "lookback_minutes": lookback_minutes,
        "max_shards": max_shards,
        "targets": [asdict(t) for t in targets],
        "summary": summary,
        "lanes": lanes,
        "top_results": top,
        "can_trade": False,
        "can_promote": False,
    }


def publish_scout(payload: dict, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(out)


def _summary(
    results: list[dict],
    lanes: list[dict],
    filter_decisions: tuple[LaneFilterDecision, ...] = (),
) -> dict:
    states: dict[str, int] = {}
    routes: dict[str, int] = {}
    for r in results:
        states[str(r.get("state", "UNKNOWN"))] = states.get(str(r.get("state", "UNKNOWN")), 0) + 1
        route = ((r.get("route_decision") or {}).get("route") or "BLOCKED")
        routes[route] = routes.get(route, 0) + 1
    best = results[0] if results else None
    return {
        "lanes": len(lanes),
        "lanes_with_data": sum(
            1 for lane in lanes if not lane["stats"].get("missing_stream")
        ),
        "filtered_lanes": sum(
            1 for decision in filter_decisions if not decision.passed
        ),
        "lane_filters": summarize_filter_decisions(filter_decisions),
        "results": len(results),
        "states": states,
        "routes": routes,
        "edge_candidates": states.get("EDGE_CANDIDATE_MAKER", 0)
        + states.get("EDGE_CANDIDATE_TAKER", 0),
        "maker_only": routes.get("MAKER_ONLY", 0),
        "taker_allowed": routes.get("TAKER_ALLOWED", 0),
        "blocked": routes.get("BLOCKED", 0),
        "best": {
            "exchange": best.get("exchange"),
            "symbol": best.get("symbol"),
            "family": best.get("family"),
            "side": best.get("side"),
            "horizon_ms": best.get("horizon_ms"),
            "state": best.get("state"),
            "route": (best.get("route_decision") or {}).get("route"),
            "samples": best.get("samples"),
            "avg_forward_bps": best.get("avg_forward_bps"),
            "avg_net_bps": best.get("avg_net_bps"),
            "profit_factor": best.get("profit_factor"),
        } if best else None,
    }


def _result_sort_key(r: EdgeHypothesisResult) -> tuple[int, float, float, int, str]:
    state_rank = {
        "EDGE_CANDIDATE_TAKER": 0,
        "EDGE_CANDIDATE_MAKER": 1,
        "UNDER_SAMPLED": 2,
        "BELOW_BREAKEVEN": 3,
    }.get(r.state, 4)
    return (
        state_rank,
        -(r.profit_factor or 0.0),
        -(r.avg_net_bps or -999.0),
        -r.samples,
        r.hypothesis_id,
    )


def _parse_days(
    raw: str | None,
    data_root: Path,
    targets: tuple[ResearchTarget, ...],
) -> tuple[str, ...]:
    if not raw or raw.lower() == "latest":
        return _scalper_research_days(data_root, targets)
    return _split_csv(raw)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="run fast recent-slice L2 scout")
    p.add_argument("--data-root", default="data")
    p.add_argument("--out", default=f"research/live_research/{L2_SCOUT_LATEST}")
    p.add_argument("--days", default="latest")
    p.add_argument("--exchanges")
    p.add_argument("--symbols")
    p.add_argument("--lookback-minutes", type=int,
                   default=_env_int("L2_SCOUT_LOOKBACK_MINUTES", 30))
    p.add_argument("--max-shards", type=int,
                   default=_env_int("L2_SCOUT_MAX_SHARDS", 40))
    p.add_argument("--max-results", type=int,
                   default=_env_int("L2_SCOUT_MAX_RESULTS", 100))
    p.add_argument("--interval-seconds", type=int,
                   default=_env_int("L2_SCOUT_INTERVAL_SECONDS", 0))
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    root = Path(args.data_root)
    targets = load_research_targets(
        exchanges=_split_csv(args.exchanges) or None,
        symbols=_split_csv(args.symbols) or None,
    )
    out = Path(args.out)

    while True:
        started = time.time()
        days = _parse_days(args.days, root, targets)
        payload = run_fast_l2_scout(
            root,
            targets=targets,
            days=days,
            lookback_minutes=args.lookback_minutes,
            max_shards=args.max_shards,
            max_results=args.max_results,
        )
        publish_scout(payload, out)
        logger.info(
            "fast l2 scout: %d lanes, %d results, %s best, %.1fs -> %s",
            payload["summary"]["lanes"],
            payload["summary"]["results"],
            payload["summary"]["best"],
            time.time() - started,
            out,
        )
        if args.json:
            print(json.dumps(payload, indent=2))
        if args.interval_seconds <= 0:
            break
        time.sleep(args.interval_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
