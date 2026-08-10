"""Research-only shadow runner for event lead-lag candidates.

The event lead-lag miner can surface maker-only hypotheses, but those rows are
not execution-ready. This runner bridges the observability gap: every minute it
re-evaluates the approved Delta-follower hypotheses on stored 1m candles and
journals either a shadow intent or an explicit "why no trade" record.

No orders are submitted here. Candle data cannot prove maker fills, so every
intent carries the maker-fill assumption that must be tested by L2 replay
before any paper/live promotion.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

import pandas as pd

from vnedge.data.candle_ingestor import ingest_candles
from vnedge.data.ccxt_client import CcxtPublicClient
from vnedge.data.parquet_store import ParquetStore
from vnedge.research.event_leadlag_alpha import (
    LEAD_LAG_MINER_ID,
    LeadLagFilter,
    LeadLagMinerConfig,
    prepare_lane,
)
from vnedge.scalping.parameter_registry import DEFAULT_SCALPER_PARAMETER_REGISTRY

RUNNER_ID = "event_leadlag_shadow_runner_v1"
MAKER_FILL_ASSUMPTION = (
    "MAKER_ONLY shadow: join/passive quote on the Delta follower after the "
    "leader event candle closes; candle data does not prove queue position or "
    "fill, so this must pass L2 replay before paper trading."
)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EventLeadLagShadowSpec:
    spec_id: str
    leader_exchange: str
    leader_symbol: str
    follower_exchange: str
    follower_symbol: str
    side: str
    horizon_min: int
    event_filter: LeadLagFilter
    route_decision: str = "MAKER_ONLY"
    notional_usd: float = 100.0
    leverage: float = 1.0
    maker_join_bps: float = 0.5
    source: str = LEAD_LAG_MINER_ID

    @property
    def sign(self) -> float:
        return 1.0 if self.side == "long" else -1.0

    def to_dict(self) -> dict:
        row = asdict(self)
        row["event_filter"] = asdict(self.event_filter)
        return row


@dataclass(frozen=True)
class ShadowCycleConfig:
    miner: LeadLagMinerConfig = field(
        default_factory=lambda: LeadLagMinerConfig(
            timeframe="1m",
            lookback_days=60,
            rolling_window=120,
            horizons_min=(15,),
        )
    )
    max_data_age_minutes: int = 180
    scan_lookback_minutes: int = 5
    refresh_candles_before_eval: bool = False
    refresh_overlap_minutes: int = 30
    refresh_bootstrap_minutes: int = 360

    def __post_init__(self) -> None:
        if self.max_data_age_minutes <= 0:
            raise ValueError("max_data_age_minutes must be positive")
        if self.scan_lookback_minutes <= 0:
            raise ValueError("scan_lookback_minutes must be positive")
        if self.refresh_overlap_minutes <= 0:
            raise ValueError("refresh_overlap_minutes must be positive")
        if self.refresh_bootstrap_minutes <= 0:
            raise ValueError("refresh_bootstrap_minutes must be positive")


def default_specs() -> tuple[EventLeadLagShadowSpec, ...]:
    """Pinned candidates from the 2026-07-07 event lead-lag sweep.

    They are intentionally narrow: SOL/XRP Delta-follower, long-only, 15m
    horizon, maker-only route. Any expansion should come from a fresh miner
    result and a reviewed config change.
    """
    loose = LeadLagFilter(4.0, 1.8, -0.25, 0.50, 6.0)
    strict = LeadLagFilter(8.0, 2.5, 0.50, 0.75, 10.0)
    return (
        EventLeadLagShadowSpec(
            spec_id="sol_binanceusdm_to_delta_india_long_15m",
            leader_exchange="binanceusdm",
            leader_symbol="SOL/USDT:USDT",
            follower_exchange="delta_india",
            follower_symbol="SOL/USD:USD",
            side="long",
            horizon_min=15,
            event_filter=loose,
        ),
        EventLeadLagShadowSpec(
            spec_id="xrp_bybit_to_delta_india_long_15m",
            leader_exchange="bybit",
            leader_symbol="XRP/USDT:USDT",
            follower_exchange="delta_india",
            follower_symbol="XRP/USD:USD",
            side="long",
            horizon_min=15,
            event_filter=strict,
        ),
        EventLeadLagShadowSpec(
            spec_id="xrp_binanceusdm_to_delta_india_long_15m",
            leader_exchange="binanceusdm",
            leader_symbol="XRP/USDT:USDT",
            follower_exchange="delta_india",
            follower_symbol="XRP/USD:USD",
            side="long",
            horizon_min=15,
            event_filter=strict,
        ),
    )


async def refresh_shadow_candles(
    store: ParquetStore,
    specs: Iterable[EventLeadLagShadowSpec],
    *,
    config: ShadowCycleConfig,
    now: datetime,
) -> dict:
    """Refresh only the public 1m lanes required by the pinned shadow specs."""
    targets = sorted({
        (spec.leader_exchange, spec.leader_symbol)
        for spec in specs
    } | {
        (spec.follower_exchange, spec.follower_symbol)
        for spec in specs
    })
    until_ms = int(now.timestamp() * 1000)
    reports_dir = store.root / "reports" / "data_quality"
    results: list[dict] = []
    by_exchange: dict[str, list[str]] = {}
    for exchange, symbol in targets:
        by_exchange.setdefault(exchange, []).append(symbol)

    for exchange, symbols in by_exchange.items():
        try:
            async with CcxtPublicClient(exchange) as client:
                for symbol in symbols:
                    since_ms = _refresh_since_ms(store, exchange, symbol, config, until_ms)
                    try:
                        result = await ingest_candles(
                            client,
                            store,
                            symbol=symbol,
                            timeframe=config.miner.timeframe,
                            since_ms=since_ms,
                            until_ms=until_ms,
                            reports_dir=reports_dir,
                        )
                        results.append({
                            "exchange": exchange,
                            "symbol": symbol,
                            "timeframe": config.miner.timeframe,
                            "persisted": result.persisted,
                            "rows_fetched": result.rows_fetched,
                            "rows_added": result.rows_added,
                            "summary": result.summary,
                        })
                    except Exception as exc:  # pragma: no cover - network/venue dependent
                        logger.exception("shadow candle refresh failed for %s %s", exchange, symbol)
                        results.append({
                            "exchange": exchange,
                            "symbol": symbol,
                            "timeframe": config.miner.timeframe,
                            "persisted": False,
                            "error": str(exc),
                        })
        except Exception as exc:  # pragma: no cover - network/venue dependent
            logger.exception("shadow candle refresh failed to open %s", exchange)
            for symbol in symbols:
                results.append({
                    "exchange": exchange,
                    "symbol": symbol,
                    "timeframe": config.miner.timeframe,
                    "persisted": False,
                    "error": str(exc),
                })

    return {
        "enabled": True,
        "timeframe": config.miner.timeframe,
        "targets": len(targets),
        "persisted": sum(1 for row in results if row.get("persisted")),
        "failed": sum(1 for row in results if not row.get("persisted")),
        "rows_added": sum(int(row.get("rows_added", 0)) for row in results),
        "results": results,
    }


def _refresh_since_ms(
    store: ParquetStore,
    exchange: str,
    symbol: str,
    config: ShadowCycleConfig,
    until_ms: int,
) -> int:
    try:
        candles = store.read_candles(exchange, symbol, config.miner.timeframe)
        latest_ms = int(candles["timestamp"].iloc[-1].value // 1_000_000)
        return max(latest_ms - config.refresh_overlap_minutes * 60_000, 0)
    except FileNotFoundError:
        return max(until_ms - config.refresh_bootstrap_minutes * 60_000, 0)


def run_shadow_cycle(
    data_root: Path | str,
    *,
    specs: Iterable[EventLeadLagShadowSpec] | None = None,
    config: ShadowCycleConfig = ShadowCycleConfig(),
    journal_path: Path | str | None = None,
    out_path: Path | str | None = None,
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now(UTC)
    selected = tuple(specs or default_specs())
    store = ParquetStore(data_root)
    refresh = {"enabled": False}
    if config.refresh_candles_before_eval:
        refresh = asyncio.run(refresh_shadow_candles(store, selected, config=config, now=now))
    seen_intent_keys = (
        load_journal_intent_keys(Path(journal_path)) if journal_path is not None else set()
    )
    evals = []
    for spec in selected:
        row = evaluate_spec(
            store,
            spec,
            config=config,
            now=now,
            seen_intent_keys=seen_intent_keys,
        )
        if row.get("shadow_intent"):
            seen_intent_keys.add(str(row["shadow_intent"]["intent_key"]))
        evals.append(row)
    intents = [row["shadow_intent"] for row in evals if row.get("shadow_intent")]
    payload = {
        "generated_at": now.isoformat(),
        "runner_id": RUNNER_ID,
        "miner_id": LEAD_LAG_MINER_ID,
        "mode": "shadow_research_only",
        "can_trade": False,
        "can_promote": False,
        "requires_l2_replay": True,
        "maker_fill_assumption": MAKER_FILL_ASSUMPTION,
        "candle_refresh": refresh,
        "summary": {
            "specs": len(selected),
            "shadow_intents": len(intents),
            "missed_opportunities": len(selected) - len(intents),
            "states": _count_by(evals, "state"),
        },
        "specs": [spec.to_dict() for spec in selected],
        "evaluations": evals,
        "shadow_intents": intents,
    }
    if journal_path is not None:
        append_cycle_journal(Path(journal_path), evals)
    if out_path is not None:
        atomic_write_json(Path(out_path), payload)
    return payload


def evaluate_spec(
    store: ParquetStore,
    spec: EventLeadLagShadowSpec,
    *,
    config: ShadowCycleConfig,
    now: datetime,
    seen_intent_keys: set[str] | None = None,
) -> dict:
    seen_intent_keys = seen_intent_keys or set()
    base = _base_payload(spec, now)
    try:
        leader = _load_prepared(
            store, spec.leader_exchange, spec.leader_symbol, config.miner
        )
        follower = _load_prepared(
            store, spec.follower_exchange, spec.follower_symbol, config.miner
        )
    except FileNotFoundError as exc:
        return _miss(base, "DATA_MISSING", [str(exc)])

    merged = pd.merge(
        leader,
        follower,
        on="timestamp",
        how="inner",
        suffixes=("_leader", "_follower"),
    ).sort_values("timestamp").reset_index(drop=True)
    if merged.empty:
        return _miss(base, "NO_COMMON_CANDLE", ["leader and follower have no aligned 1m candles"])

    latest = merged.iloc[-1]
    latest_ts = _as_utc(latest["timestamp"])
    latest_age_minutes = max((now - latest_ts).total_seconds() / 60.0, 0.0)
    latest_blockers = _event_blockers(latest, spec)
    if latest_age_minutes > config.max_data_age_minutes:
        latest_blockers.append(
            f"stale_data: latest aligned candle age {latest_age_minutes:.1f}m "
            f"> max {config.max_data_age_minutes}m"
        )

    base.update({
        "event_ts": latest_ts.isoformat(),
        "data_age_minutes": round(latest_age_minutes, 3),
        "latest_aligned_ts": latest_ts.isoformat(),
        "latest_data_age_minutes": round(latest_age_minutes, 3),
        "metrics": _metrics(latest, spec),
        "scan_window_minutes": config.scan_lookback_minutes,
    })
    if latest_age_minutes > config.max_data_age_minutes:
        return _miss(base, "NO_TRADE", latest_blockers)

    now_ts = pd.Timestamp(now)
    if now_ts.tzinfo is None:
        now_ts = now_ts.tz_localize("UTC")
    cutoff = now_ts - pd.Timedelta(minutes=config.scan_lookback_minutes)
    recent = merged[merged["timestamp"] >= cutoff].copy()
    base["scan_rows_checked"] = int(len(recent))
    if recent.empty:
        return _miss(
            base,
            "NO_TRADE",
            [
                "no_aligned_candles_in_scan_window: "
                f"latest={latest_ts.isoformat()} cutoff={cutoff.isoformat()}"
            ],
        )

    blocked_samples: list[dict] = []
    duplicate_keys: list[str] = []
    for _, candidate in recent.iloc[::-1].iterrows():
        event_ts = _as_utc(candidate["timestamp"])
        age_minutes = max((now - event_ts).total_seconds() / 60.0, 0.0)
        blockers = _event_blockers(candidate, spec)
        if age_minutes > config.max_data_age_minutes:
            blockers.append(
                f"stale_data: event candle age {age_minutes:.1f}m "
                f"> max {config.max_data_age_minutes}m"
            )
        if blockers:
            if len(blocked_samples) < 3:
                blocked_samples.append({
                    "event_ts": event_ts.isoformat(),
                    "data_age_minutes": round(age_minutes, 3),
                    "why_no_trade": blockers,
                    "metrics": _metrics(candidate, spec),
                })
            continue

        intent = _shadow_intent(spec, candidate, event_ts)
        intent_key = str(intent["intent_key"])
        if intent_key in seen_intent_keys:
            duplicate_keys.append(intent_key)
            continue

        base.update({
            "event_ts": event_ts.isoformat(),
            "data_age_minutes": round(age_minutes, 3),
            "matched_event_offset_minutes": round(age_minutes, 3),
            "metrics": _metrics(candidate, spec),
            "state": "SHADOW_INTENT",
            "fired": True,
            "why_no_trade": [],
            "shadow_intent": intent,
        })
        return base

    if duplicate_keys:
        return _miss(
            base,
            "DUPLICATE_SUPPRESSED",
            [
                "duplicate_intent_key: "
                f"{duplicate_keys[-1]} already journaled in scan window"
            ],
        )

    if blocked_samples:
        base["recent_blocked_samples"] = blocked_samples
    return _miss(base, "NO_TRADE", latest_blockers or ["no_candidate_in_scan_window"])


def _load_prepared(
    store: ParquetStore,
    exchange: str,
    symbol: str,
    config: LeadLagMinerConfig,
) -> pd.DataFrame:
    candles = store.read_candles(exchange, symbol, config.timeframe)
    cutoff = candles["timestamp"].iloc[-1] - pd.Timedelta(days=config.lookback_days)
    frame = candles[candles["timestamp"] >= cutoff].copy().reset_index(drop=True)
    prepared = prepare_lane(frame, config)
    return prepared.dropna(subset=["ret_bps", "ret_z"]).reset_index(drop=True)


def _event_blockers(row: pd.Series, spec: EventLeadLagShadowSpec) -> list[str]:
    filt = spec.event_filter
    signed_leader = spec.sign * float(row["ret_bps_leader"])
    signed_leader_z = spec.sign * float(row["ret_z_leader"])
    signed_follower = spec.sign * float(row["ret_bps_follower"])
    leader_abs = abs(float(row["ret_bps_leader"]))
    max_same = min(
        leader_abs * filt.max_follower_same_min_ratio,
        filt.max_follower_same_min_bps,
    )
    blockers: list[str] = []
    if signed_leader < filt.min_abs_leader_bps:
        blockers.append(
            f"leader_move_below: {signed_leader:.3f}bps < "
            f"{filt.min_abs_leader_bps:.3f}bps"
        )
    if signed_leader_z < filt.min_abs_leader_z:
        blockers.append(
            f"leader_z_below: {signed_leader_z:.3f} < {filt.min_abs_leader_z:.3f}"
        )
    volume_z = float(row["volume_z_leader"])
    if volume_z < filt.min_volume_z:
        blockers.append(f"volume_z_below: {volume_z:.3f} < {filt.min_volume_z:.3f}")
    if signed_follower > max_same:
        blockers.append(
            f"follower_already_moved: {signed_follower:.3f}bps > "
            f"{max_same:.3f}bps allowed"
        )
    if signed_follower < -filt.max_follower_same_min_bps:
        blockers.append(
            f"follower_moved_opposite: {signed_follower:.3f}bps < "
            f"-{filt.max_follower_same_min_bps:.3f}bps"
        )
    return blockers


def _shadow_intent(
    spec: EventLeadLagShadowSpec,
    row: pd.Series,
    event_ts: datetime,
) -> dict:
    follower_close = float(row["close_follower"])
    buy = spec.side == "long"
    limit_price = follower_close * (
        1.0 - spec.maker_join_bps / 10_000.0 if buy else 1.0 + spec.maker_join_bps / 10_000.0
    )
    quantity = spec.notional_usd / limit_price
    fee = DEFAULT_SCALPER_PARAMETER_REGISTRY.fee_profile(spec.follower_exchange)
    intent_key = (
        f"{RUNNER_ID}|{spec.spec_id}|{int(event_ts.timestamp() * 1000)}"
    )
    return {
        "intent_key": intent_key,
        "approved": False,
        "approval_state": "RESEARCH_SHADOW_ONLY",
        "route_decision": spec.route_decision,
        "maker_fill_assumption": MAKER_FILL_ASSUMPTION,
        "requires_l2_replay": True,
        "missed_fill_policy": "log_only_no_fill_simulation",
        "intent": {
            "symbol": spec.follower_symbol,
            "exchange": spec.follower_exchange,
            "side": spec.side,
            "quantity": quantity,
            "notional_usd": spec.notional_usd,
            "leverage": spec.leverage,
            "reduce_only": False,
            "strategy_id": RUNNER_ID,
            "order_type": "limit",
            "limit_price": limit_price,
        },
        "signal_reason": (
            f"{spec.leader_exchange} {spec.leader_symbol} shock leads "
            f"{spec.follower_exchange} {spec.follower_symbol}; "
            f"{spec.horizon_min}m maker-only Delta-follower candidate"
        ),
        "costs": {
            "exchange": fee.exchange,
            "maker_first_cost_bps": fee.maker_first_cost_bps,
            "taker_round_trip_cost_bps": fee.taker_round_trip_cost_bps,
        },
    }


def _metrics(row: pd.Series, spec: EventLeadLagShadowSpec) -> dict:
    filt = spec.event_filter
    leader_abs = abs(float(row["ret_bps_leader"]))
    max_same = min(
        leader_abs * filt.max_follower_same_min_ratio,
        filt.max_follower_same_min_bps,
    )
    signed_leader = spec.sign * float(row["ret_bps_leader"])
    signed_leader_z = spec.sign * float(row["ret_z_leader"])
    signed_follower = spec.sign * float(row["ret_bps_follower"])
    return {
        "leader_ret_bps": _round(float(row["ret_bps_leader"])),
        "leader_abs_ret_bps": _round(leader_abs),
        "signed_leader_bps": _round(signed_leader),
        "signed_leader_z": _round(signed_leader_z),
        "leader_volume_z": _round(float(row["volume_z_leader"])),
        "signed_follower_same_min_bps": _round(signed_follower),
        "max_follower_same_min_bps": _round(max_same),
        "follower_close": _round(float(row["close_follower"]), digits=8),
        "filter": asdict(filt),
    }


def _base_payload(spec: EventLeadLagShadowSpec, now: datetime) -> dict:
    return {
        "runner_id": RUNNER_ID,
        "generated_at": now.isoformat(),
        "spec_id": spec.spec_id,
        "leader_exchange": spec.leader_exchange,
        "leader_symbol": spec.leader_symbol,
        "follower_exchange": spec.follower_exchange,
        "follower_symbol": spec.follower_symbol,
        "side": spec.side,
        "horizon_min": spec.horizon_min,
        "route_decision": spec.route_decision,
        "maker_fill_assumption": MAKER_FILL_ASSUMPTION,
        "fired": False,
        "can_trade": False,
        "can_promote": False,
    }


def _miss(base: dict, state: str, why: list[str]) -> dict:
    base.update({
        "state": state,
        "fired": False,
        "why_no_trade": why,
        "missed_opportunity": {
            "logged": True,
            "reason_count": len(why),
            "reasons": why,
        },
    })
    return base


def load_journal_intent_keys(path: Path) -> set[str]:
    keys: set[str] = set()
    if not path.exists():
        return keys
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = record.get("payload", {})
            if record.get("kind") == "shadow_intent":
                key = payload.get("intent_key")
                if key:
                    keys.add(str(key))
            eval_intent = payload.get("shadow_intent")
            if isinstance(eval_intent, dict) and eval_intent.get("intent_key"):
                keys.add(str(eval_intent["intent_key"]))
    return keys


def append_cycle_journal(path: Path, evaluations: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for row in evaluations:
            _write_jsonl(f, "event_leadlag_eval", row)
            if row.get("shadow_intent"):
                _write_jsonl(f, "shadow_intent", row["shadow_intent"])


def _write_jsonl(handle, kind: str, payload: dict) -> None:
    record = {
        "ts": datetime.now(UTC).isoformat(),
        "kind": kind,
        "payload": payload,
    }
    handle.write(json.dumps(record, default=str) + "\n")
    handle.flush()


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    tmp.replace(path)


def _count_by(rows: Iterable[dict], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(key, "UNKNOWN"))
        counts[value] = counts.get(value, 0) + 1
    return counts


def _as_utc(value) -> datetime:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.to_pydatetime().astimezone(UTC)


def _round(value: float | None, *, digits: int = 3) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, digits)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run event lead-lag shadow journal")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--journal", default="logs/event_leadlag_shadow/events.jsonl")
    parser.add_argument("--out", default="research/live_research/event_leadlag_shadow_latest.json")
    parser.add_argument("--interval-seconds", type=int, default=60)
    parser.add_argument("--lookback-days", type=int, default=60)
    parser.add_argument("--max-data-age-minutes", type=int, default=180)
    parser.add_argument("--scan-lookback-minutes", type=int, default=5)
    parser.add_argument("--refresh-overlap-minutes", type=int, default=30)
    parser.add_argument("--refresh-bootstrap-minutes", type=int, default=360)
    parser.add_argument(
        "--skip-candle-refresh",
        action="store_true",
        help="do not refresh pinned public 1m candle lanes before evaluation",
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args(argv)
    config = ShadowCycleConfig(
        miner=LeadLagMinerConfig(
            timeframe="1m",
            lookback_days=args.lookback_days,
            rolling_window=120,
            horizons_min=(15,),
        ),
        max_data_age_minutes=args.max_data_age_minutes,
        scan_lookback_minutes=args.scan_lookback_minutes,
        refresh_candles_before_eval=not args.skip_candle_refresh,
        refresh_overlap_minutes=args.refresh_overlap_minutes,
        refresh_bootstrap_minutes=args.refresh_bootstrap_minutes,
    )
    while True:
        payload = run_shadow_cycle(
            args.data_root,
            config=config,
            journal_path=args.journal,
            out_path=args.out,
        )
        if args.json:
            print(json.dumps(payload, indent=2, default=str))
        else:
            summary = payload["summary"]
            print(
                f"{payload['generated_at']} {RUNNER_ID}: "
                f"{summary['shadow_intents']} intents, "
                f"{summary['missed_opportunities']} no-trade evals"
            )
        if args.once:
            return 0
        time.sleep(max(args.interval_seconds, 1))


if __name__ == "__main__":
    sys.exit(main())
