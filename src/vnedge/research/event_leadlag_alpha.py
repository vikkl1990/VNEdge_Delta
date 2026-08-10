"""Cross-venue event lead-lag alpha miner.

This is a research-only hunt for the scalper premise that visual MTF patterns
miss: a strong event on one venue may predict a delayed move on another venue
or instrument. The miner works on 1m candles as a cheap first pass before any
tick replay:

* detect leader shocks by return z-score, absolute move, and optional volume
* require the follower to have not already fully repriced in the same minute
* enter the follower at the next candle open
* score exits at fixed minute horizons after realistic maker/taker costs

Rows here are hypotheses. They never trade, never promote, and never bypass
untouched judgment.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

import pandas as pd

from vnedge.data.parquet_store import ParquetStore
from vnedge.research.universe import ResearchTarget, load_research_targets
from vnedge.scalping.parameter_registry import DEFAULT_SCALPER_PARAMETER_REGISTRY
from vnedge.strategy.indicators import zscore

LEAD_LAG_MINER_ID = "cross_venue_event_leadlag_v1"


@dataclass(frozen=True)
class LeadLagFilter:
    min_abs_leader_bps: float
    min_abs_leader_z: float
    min_volume_z: float
    max_follower_same_min_ratio: float
    max_follower_same_min_bps: float

    @property
    def label(self) -> str:
        return (
            f"ret>={self.min_abs_leader_bps:g}bps"
            f"|z>={self.min_abs_leader_z:g}"
            f"|volZ>={self.min_volume_z:g}"
            f"|lag<={self.max_follower_same_min_ratio:g}x"
            f"/{self.max_follower_same_min_bps:g}bps"
        )


@dataclass(frozen=True)
class LeadLagMinerConfig:
    timeframe: str = "1m"
    lookback_days: int = 60
    rolling_window: int = 120
    horizons_min: tuple[int, ...] = (1, 3, 5, 10, 15)
    filters: tuple[LeadLagFilter, ...] = field(
        default_factory=lambda: (
            LeadLagFilter(4.0, 1.8, -0.25, 0.50, 6.0),
            LeadLagFilter(6.0, 2.2, 0.00, 0.50, 8.0),
            LeadLagFilter(8.0, 2.5, 0.50, 0.75, 10.0),
            LeadLagFilter(12.0, 3.0, 1.00, 0.75, 12.0),
        )
    )
    min_samples: int = 20
    maker_min_profit_factor: float = 1.25
    taker_min_profit_factor: float = 1.80
    min_avg_net_bps: float = 0.5
    max_single_win_share: float = 0.35
    max_results: int = 100


@dataclass(frozen=True)
class LeadLagResult:
    miner_id: str
    hypothesis_id: str
    base_asset: str
    family: str
    leader_exchange: str
    leader_symbol: str
    follower_exchange: str
    follower_symbol: str
    side: str
    horizon_min: int
    samples: int
    avg_forward_bps: float | None
    maker_avg_net_bps: float | None
    taker_avg_net_bps: float | None
    maker_profit_factor: float | None
    taker_profit_factor: float | None
    win_rate_pct: float
    single_win_share: float | None
    route_decision: str
    state: str
    filter: dict
    costs: dict
    evidence: dict
    can_trade: bool = False
    can_promote: bool = False
    requires_conservative_replay: bool = True
    requires_untouched_judgment: bool = True
    requires_human_approval: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


def run_event_leadlag_alpha(
    data_root: Path | str,
    *,
    targets: Iterable[ResearchTarget] | None = None,
    config: LeadLagMinerConfig = LeadLagMinerConfig(),
) -> dict:
    store = ParquetStore(data_root)
    selected = tuple(targets or load_research_targets(timeframe=config.timeframe))
    lanes, missing = load_lanes(store, selected, config)
    results = mine_event_leadlag(lanes, config=config)
    ranked = sorted(results, key=_result_sort_key)[: config.max_results]
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "miner_id": LEAD_LAG_MINER_ID,
        "policy": leadlag_policy(config),
        "flow": [
            "hydrate_1m_lanes",
            "align_same_base_across_exchanges",
            "detect_leader_event_shocks",
            "require_follower_lag",
            "enter_follower_next_open",
            "score_forward_horizons_after_costs",
            "queue_tick_replay_if_edge_candidate",
        ],
        "data_lanes": {
            "loaded": [lane_summary(lane) for lane in lanes],
            "missing": missing,
        },
        "summary": summarize_results(ranked, lanes=lanes, missing=missing),
        "hypotheses": [result.to_dict() for result in ranked],
        "replay_queue": [
            result.to_dict()
            for result in ranked
            if result.state in {"EDGE_CANDIDATE_MAKER", "EDGE_CANDIDATE_TAKER"}
        ],
        "can_trade": False,
        "can_promote": False,
        "note": (
            "Cross-venue lead-lag rows are research hypotheses only; candidates "
            "still require tick replay, untouched judgment, paper/shadow, and "
            "human approval."
        ),
    }


def leadlag_policy(config: LeadLagMinerConfig) -> dict:
    registry = DEFAULT_SCALPER_PARAMETER_REGISTRY
    return {
        "status": "research_only",
        "can_trade": False,
        "can_promote": False,
        "requires_conservative_replay": True,
        "requires_untouched_judgment": True,
        "requires_human_approval": True,
        "miner_id": LEAD_LAG_MINER_ID,
        "premise": (
            "Leader venue/pair shock may predict delayed follower repricing; "
            "not a chart-pattern signal and not eligible for direct execution."
        ),
        "timeframe": config.timeframe,
        "lookback_days": config.lookback_days,
        "horizons_min": list(config.horizons_min),
        "min_samples": config.min_samples,
        "maker_min_profit_factor": config.maker_min_profit_factor,
        "taker_min_profit_factor": config.taker_min_profit_factor,
        "min_avg_net_bps": config.min_avg_net_bps,
        "max_single_win_share": config.max_single_win_share,
        "exchange_costs": {
            exchange: fee.to_dict()
            for exchange, fee in registry.exchange_fees.items()
        },
    }


def load_lanes(
    store: ParquetStore,
    targets: Iterable[ResearchTarget],
    config: LeadLagMinerConfig,
) -> tuple[list[dict], list[dict]]:
    lanes: list[dict] = []
    missing: list[dict] = []
    for target in targets:
        try:
            candles = store.read_candles(target.exchange, target.symbol, config.timeframe)
        except FileNotFoundError as exc:
            missing.append({
                "exchange": target.exchange,
                "symbol": target.symbol,
                "timeframe": config.timeframe,
                "reason": str(exc),
            })
            continue
        if candles.empty:
            missing.append({
                "exchange": target.exchange,
                "symbol": target.symbol,
                "timeframe": config.timeframe,
                "reason": "empty candle lane",
            })
            continue
        cutoff = candles["timestamp"].iloc[-1] - pd.Timedelta(days=config.lookback_days)
        frame = candles[candles["timestamp"] >= cutoff].copy().reset_index(drop=True)
        if len(frame) < config.rolling_window + max(config.horizons_min) + 5:
            missing.append({
                "exchange": target.exchange,
                "symbol": target.symbol,
                "timeframe": config.timeframe,
                "reason": (
                    f"only {len(frame)} rows after lookback; need >= "
                    f"{config.rolling_window + max(config.horizons_min) + 5}"
                ),
            })
            continue
        prepared = prepare_lane(frame, config)
        lanes.append({
            "exchange": target.exchange,
            "symbol": target.symbol,
            "base_asset": base_asset(target.symbol),
            "timeframe": config.timeframe,
            "frame": prepared,
        })
    return lanes, missing


def prepare_lane(candles: pd.DataFrame, config: LeadLagMinerConfig) -> pd.DataFrame:
    df = candles.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
    df["ret_bps"] = df["close"].pct_change() * 10_000.0
    df["abs_ret_bps"] = df["ret_bps"].abs()
    rolling_std = df["ret_bps"].rolling(config.rolling_window).std()
    df["ret_z"] = df["ret_bps"] / rolling_std.replace(0.0, float("nan"))
    df["volume_z"] = zscore(df["volume"], config.rolling_window).fillna(0.0)
    return df


def mine_event_leadlag(
    lanes: Iterable[dict],
    *,
    config: LeadLagMinerConfig = LeadLagMinerConfig(),
) -> tuple[LeadLagResult, ...]:
    by_asset: dict[str, list[dict]] = {}
    for lane in lanes:
        by_asset.setdefault(lane["base_asset"], []).append(lane)

    results: list[LeadLagResult] = []
    for asset, asset_lanes in sorted(by_asset.items()):
        if len(asset_lanes) < 2:
            continue
        for leader in asset_lanes:
            for follower in asset_lanes:
                if leader["exchange"] == follower["exchange"]:
                    continue
                results.extend(
                    mine_pair(
                        asset,
                        leader,
                        follower,
                        config=config,
                    )
                )
    return tuple(sorted(results, key=_result_sort_key))


def mine_pair(
    base: str,
    leader: dict,
    follower: dict,
    *,
    config: LeadLagMinerConfig,
) -> list[LeadLagResult]:
    merged = pd.merge(
        leader["frame"],
        follower["frame"],
        on="timestamp",
        how="inner",
        suffixes=("_leader", "_follower"),
    ).sort_values("timestamp").reset_index(drop=True)
    if merged.empty:
        return []

    out: list[LeadLagResult] = []
    for event_filter in config.filters:
        for side, sign in (("long", 1.0), ("short", -1.0)):
            signed_leader = sign * merged["ret_bps_leader"]
            signed_follower_same = sign * merged["ret_bps_follower"]
            max_same = pd.concat(
                [
                    merged["abs_ret_bps_leader"] * event_filter.max_follower_same_min_ratio,
                    pd.Series(event_filter.max_follower_same_min_bps, index=merged.index),
                ],
                axis=1,
            ).min(axis=1)
            event_mask = (
                (signed_leader >= event_filter.min_abs_leader_bps)
                & ((sign * merged["ret_z_leader"]) >= event_filter.min_abs_leader_z)
                & (merged["volume_z_leader"] >= event_filter.min_volume_z)
                & (signed_follower_same <= max_same)
                & (signed_follower_same >= -event_filter.max_follower_same_min_bps)
            )
            if not bool(event_mask.any()):
                continue
            for horizon in config.horizons_min:
                entry = merged["open_follower"].shift(-1)
                exit_ = merged["close_follower"].shift(-horizon)
                raw_bps = sign * ((exit_ - entry) / entry) * 10_000.0
                observations = raw_bps[event_mask].dropna()
                if observations.empty:
                    continue
                out.append(
                    build_result(
                        base,
                        leader,
                        follower,
                        side=side,
                        horizon_min=horizon,
                        event_filter=event_filter,
                        forward_bps=tuple(float(v) for v in observations),
                        config=config,
                    )
                )
    return out


def build_result(
    base: str,
    leader: dict,
    follower: dict,
    *,
    side: str,
    horizon_min: int,
    event_filter: LeadLagFilter,
    forward_bps: tuple[float, ...],
    config: LeadLagMinerConfig,
) -> LeadLagResult:
    fee = DEFAULT_SCALPER_PARAMETER_REGISTRY.fee_profile(follower["exchange"])
    maker_cost = fee.maker_first_cost_bps
    taker_cost = fee.taker_round_trip_cost_bps
    maker_net = tuple(v - maker_cost for v in forward_bps)
    taker_net = tuple(v - taker_cost for v in forward_bps)
    maker_pf = profit_factor(maker_net)
    taker_pf = profit_factor(taker_net)
    avg_forward = _mean(forward_bps)
    maker_avg = _mean(maker_net)
    taker_avg = _mean(taker_net)
    wins = [v for v in maker_net if v > 0.0]
    win_rate = len(wins) / len(maker_net) * 100.0 if maker_net else 0.0
    single_win_share = (
        max(wins) / sum(wins)
        if wins and sum(wins) > 0.0
        else None
    )
    route, state = route_state(
        samples=len(maker_net),
        maker_avg=maker_avg,
        taker_avg=taker_avg,
        maker_pf=maker_pf,
        taker_pf=taker_pf,
        single_win_share=single_win_share,
        config=config,
    )
    hypothesis_id = "|".join(
        (
            LEAD_LAG_MINER_ID,
            base,
            f"{leader['exchange']}->{follower['exchange']}",
            side,
            f"{horizon_min}m",
            event_filter.label,
        )
    )
    return LeadLagResult(
        miner_id=LEAD_LAG_MINER_ID,
        hypothesis_id=hypothesis_id,
        base_asset=base,
        family="cross_venue_event_leadlag",
        leader_exchange=leader["exchange"],
        leader_symbol=leader["symbol"],
        follower_exchange=follower["exchange"],
        follower_symbol=follower["symbol"],
        side=side,
        horizon_min=horizon_min,
        samples=len(maker_net),
        avg_forward_bps=_round(avg_forward),
        maker_avg_net_bps=_round(maker_avg),
        taker_avg_net_bps=_round(taker_avg),
        maker_profit_factor=_round(maker_pf),
        taker_profit_factor=_round(taker_pf),
        win_rate_pct=round(win_rate, 1),
        single_win_share=_round(single_win_share),
        route_decision=route,
        state=state,
        filter=asdict(event_filter),
        costs={
            "exchange": fee.exchange,
            "maker_first_cost_bps": maker_cost,
            "taker_round_trip_cost_bps": taker_cost,
        },
        evidence={
            "forward_bps_sum": round(sum(forward_bps), 3),
            "maker_net_bps_sum": round(sum(maker_net), 3),
            "taker_net_bps_sum": round(sum(taker_net), 3),
            "worst_maker_net_bps": round(min(maker_net), 3),
            "best_maker_net_bps": round(max(maker_net), 3),
            "entry_model": "leader event known after candle close; follower entry next open",
        },
    )


def route_state(
    *,
    samples: int,
    maker_avg: float | None,
    taker_avg: float | None,
    maker_pf: float | None,
    taker_pf: float | None,
    single_win_share: float | None,
    config: LeadLagMinerConfig,
) -> tuple[str, str]:
    if samples < config.min_samples:
        return "BLOCKED", "UNDER_SAMPLED"
    if maker_avg is None or maker_avg < config.min_avg_net_bps:
        return "BLOCKED", "BELOW_COST"
    if maker_pf is None or maker_pf < config.maker_min_profit_factor:
        return "BLOCKED", "BELOW_PF"
    if single_win_share is not None and single_win_share > config.max_single_win_share:
        return "BLOCKED", "LUCKY_TRADE_CONCENTRATION"
    if (
        taker_avg is not None
        and taker_avg >= config.min_avg_net_bps
        and taker_pf is not None
        and taker_pf >= config.taker_min_profit_factor
    ):
        return "TAKER_ALLOWED", "EDGE_CANDIDATE_TAKER"
    return "MAKER_ONLY", "EDGE_CANDIDATE_MAKER"


def summarize_results(results: list[LeadLagResult], *, lanes: list[dict], missing: list[dict]) -> dict:
    states: dict[str, int] = {}
    routes: dict[str, int] = {}
    for result in results:
        states[result.state] = states.get(result.state, 0) + 1
        routes[result.route_decision] = routes.get(result.route_decision, 0) + 1
    best = results[0] if results else None
    return {
        "loaded_lanes": len(lanes),
        "missing_lanes": len(missing),
        "hypotheses": len(results),
        "states": states,
        "routes": routes,
        "edge_candidates": states.get("EDGE_CANDIDATE_MAKER", 0)
        + states.get("EDGE_CANDIDATE_TAKER", 0),
        "best": best.to_dict() if best else None,
        "can_trade": False,
        "can_promote": False,
    }


def lane_summary(lane: dict) -> dict:
    frame = lane["frame"]
    return {
        "exchange": lane["exchange"],
        "symbol": lane["symbol"],
        "base_asset": lane["base_asset"],
        "timeframe": lane["timeframe"],
        "rows": len(frame),
        "start": frame["timestamp"].iloc[0].isoformat(),
        "end": frame["timestamp"].iloc[-1].isoformat(),
    }


def base_asset(symbol: str) -> str:
    return symbol.split("/", maxsplit=1)[0].upper()


def profit_factor(values: tuple[float, ...]) -> float | None:
    wins = sum(v for v in values if v > 0)
    losses = -sum(v for v in values if v <= 0)
    if losses > 0:
        return wins / losses
    return 999.0 if wins > 0 else None


def _mean(values: tuple[float, ...]) -> float | None:
    return sum(values) / len(values) if values else None


def _round(value: float | None, digits: int = 3) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, digits)


def _result_sort_key(result: LeadLagResult) -> tuple[int, float, float, int, str]:
    state_rank = {
        "EDGE_CANDIDATE_TAKER": 0,
        "EDGE_CANDIDATE_MAKER": 1,
        "LUCKY_TRADE_CONCENTRATION": 2,
        "UNDER_SAMPLED": 3,
        "BELOW_PF": 4,
        "BELOW_COST": 5,
    }.get(result.state, 9)
    return (
        state_rank,
        -(result.maker_profit_factor or 0.0),
        -(result.maker_avg_net_bps or -999.0),
        -result.samples,
        result.hypothesis_id,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run cross-venue event lead-lag miner")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--out", default="research/live_research/event_leadlag_latest.json")
    parser.add_argument("--lookback-days", type=int, default=60)
    parser.add_argument("--min-samples", type=int, default=20)
    parser.add_argument("--max-results", type=int, default=100)
    parser.add_argument("--interval-seconds", type=int, default=0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--exchanges", default=None)
    parser.add_argument("--symbols", default=None)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def _split_csv(raw: str | None) -> tuple[str, ...] | None:
    if raw is None:
        return None
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    while True:
        payload = _run_and_publish(args)
        if args.json:
            print(json.dumps(payload, indent=2, default=str))
        else:
            print(format_report(payload))
        if args.once or args.interval_seconds <= 0:
            return 0
        time.sleep(max(args.interval_seconds, 1))


def _run_and_publish(args: argparse.Namespace) -> dict:
    config = LeadLagMinerConfig(
        lookback_days=args.lookback_days,
        min_samples=args.min_samples,
        max_results=args.max_results,
    )
    targets = load_research_targets(
        exchanges=_split_csv(args.exchanges),
        symbols=_split_csv(args.symbols),
        timeframe=config.timeframe,
    )
    payload = run_event_leadlag_alpha(args.data_root, targets=targets, config=config)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    tmp.replace(out)
    return payload


def format_report(payload: dict) -> str:
    summary = payload["summary"]
    lines = [
        "=== Event lead-lag alpha miner ===",
        f"generated: {payload['generated_at']}",
        (
            "summary: "
            f"{summary['edge_candidates']} edge candidates, "
            f"{summary['hypotheses']} hypotheses, "
            f"{summary['loaded_lanes']} lanes loaded, "
            f"{summary['missing_lanes']} missing"
        ),
    ]
    for row in payload["hypotheses"][:20]:
        lines.append(
            f"  {row['state']:<27} {row['base_asset']:<5} "
            f"{row['leader_exchange']}->{row['follower_exchange']} "
            f"{row['side']:<5} h={row['horizon_min']:>2}m "
            f"n={row['samples']:>3} maker={_fmt(row['maker_avg_net_bps'])}bps "
            f"pf={_fmt(row['maker_profit_factor'])} route={row['route_decision']}"
        )
    lines.append("research-only: no paper/shadow/live state changed")
    return "\n".join(lines)


def _fmt(value) -> str:
    return "--" if value is None else f"{float(value):.2f}"


if __name__ == "__main__":
    sys.exit(main())
