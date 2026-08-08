"""Selection-only replay for preregistered continuous MTF contracts."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import fmean
from tempfile import NamedTemporaryFile
from typing import Any

from vnedge.research.delta_scalper_backtest import _group_trades, _load_candles
from vnedge.scalping.delta_engine.backtester import CausalScalperBacktester, OpenBacktestTrade
from vnedge.scalping.delta_engine.candle_store import (
    ClosedCandleAggregator,
    MultiTimeframeCandleStore,
)
from vnedge.scalping.delta_engine.continuous_mtf import (
    ContinuousMultiTFStateMachine,
    ContinuousSetupIntent,
    EntryGeometry,
    finalize_continuous_entry,
    load_continuous_mtf_config,
)
from vnedge.scalping.delta_engine.continuous_mtf_v2 import (
    MechanicalStructureMultiTFStateMachine,
)
from vnedge.scalping.delta_engine.fee_model import DeltaFeeModel
from vnedge.scalping.delta_engine.mechanical_structure import (
    load_mechanical_structure_config,
)
from vnedge.scalping.delta_engine.types import Candle

DEFAULT_CONFIG = Path("configs/research/continuous_mtf_alignment_v1.yaml")
DEFAULT_CACHE = Path("data/delta_scalper_cache")
DEFAULT_OUTPUT = Path("research/live_research/continuous_mtf_alignment_v1_latest.json")


@dataclass(frozen=True)
class ContinuousReplayResult:
    symbol: str
    trade_records: tuple[dict[str, Any], ...]
    intent_records: tuple[dict[str, Any], ...]
    state_counters: dict[str, int]
    source_bars: int
    missing_minutes: int
    unresolved_dropped: int
    score_started_at: datetime | None
    score_ended_at: datetime | None


def _parse_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def _fee_model(raw: dict[str, Any]) -> DeltaFeeModel:
    costs = raw["costs"]
    return DeltaFeeModel(
        deto_enabled=bool(costs["deto_enabled"]),
        scalper_opted_in=bool(costs["scalper_opted_in"]),
        maker_fee_bps_pre_tax=float(costs["maker_fee_bps_pre_tax"]),
        taker_fee_bps_pre_tax=float(costs["taker_fee_bps_pre_tax"]),
        gst_rate=float(costs["gst_rate"]),
        default_slippage_bps_per_leg=float(costs["slippage_bps_per_leg"]),
    )


def _intent_record(intent: ContinuousSetupIntent, geometry: EntryGeometry) -> dict[str, Any]:
    return {
        "intent_id": intent.intent_id,
        "scanner_id": intent.scanner_id,
        "symbol": intent.symbol,
        "side": intent.side.value,
        "decision_ts": intent.decision_ts.isoformat(),
        "decision_price": intent.decision_price,
        "setup": intent.setup.to_dict(),
        "state": intent.state.to_dict(),
        "entry_geometry": geometry.to_dict(),
    }


def simulate_symbol_selection(
    symbol: str,
    rows: list[Candle],
    config_path: Path,
    *,
    score_end: datetime,
    process_end: datetime,
) -> ContinuousReplayResult:
    config, raw = load_continuous_mtf_config(config_path)
    fee_model = _fee_model(raw)
    store = MultiTimeframeCandleStore(max_bars_per_timeframe=700)
    contract_id = str(raw["contract_id"])
    if contract_id == "continuous_mtf_alignment_v2":
        machine = MechanicalStructureMultiTFStateMachine(
            store,
            config,
            load_mechanical_structure_config(config_path),
        )
    elif contract_id == "continuous_mtf_alignment_v1":
        machine = ContinuousMultiTFStateMachine(store, config)
    else:
        raise ValueError(f"unsupported continuous MTF contract: {contract_id}")
    aggregator = ClosedCandleAggregator()
    resolver = CausalScalperBacktester(None, fee_model, store)  # type: ignore[arg-type]
    pending: ContinuousSetupIntent | None = None
    active: OpenBacktestTrade | None = None
    active_intent: ContinuousSetupIntent | None = None
    trade_records: list[dict[str, Any]] = []
    intent_records: list[dict[str, Any]] = []
    seen_intents: set[str] = set()
    previous_ts: datetime | None = None
    missing = 0
    dropped = 0
    processed = 0
    scored: list[datetime] = []
    native = symbol.upper()
    for bar in sorted(rows, key=lambda item: item.ts):
        if bar.tf != "1m":
            raise ValueError("continuous MTF replay accepts 1m source candles only")
        if bar.ts > process_end:
            break
        if previous_ts is not None:
            if bar.ts <= previous_ts:
                raise ValueError("source candles must have unique ascending timestamps")
            gap = max(0, int((bar.ts - previous_ts).total_seconds() // 60) - 1)
            if gap:
                missing += gap
                dropped += int(pending is not None) + int(active is not None)
                pending = None
                active = None
                active_intent = None
                store.reset_symbol(native)
                machine.reset_symbol(native)
                aggregator = ClosedCandleAggregator()
        previous_ts = bar.ts
        processed += 1
        if pending is not None and active is None:
            geometry = finalize_continuous_entry(pending, bar, config, fee_model)
            if pending.intent_id in seen_intents:
                raise RuntimeError("continuous MTF intent journal duplicate")
            seen_intents.add(pending.intent_id)
            intent_records.append(_intent_record(pending, geometry))
            if geometry.candidate is not None:
                active = OpenBacktestTrade(geometry.candidate, bar)
                active_intent = pending
            pending = None
        if active is not None:
            candidate_metadata = dict(active.candidate.metadata)
            resolved = resolver.resolve_on_bar(active, bar)
            if resolved is not None:
                trade_records.append(
                    {
                        **resolved.to_dict(),
                        "candidate_metadata": candidate_metadata,
                        "intent_id": active_intent.intent_id if active_intent else None,
                    }
                )
                active = None
                active_intent = None
        store.append_closed(native, bar, observed_at=bar.ts)
        for higher in aggregator.on_one_minute(native, bar):
            store.append_closed(native, higher, observed_at=bar.ts)
        scoring = bar.ts <= score_end
        if scoring:
            scored.append(bar.ts)
        if scoring and pending is None and active is None:
            pending = machine.claim_intent(native, decision_price=bar.close)
    dropped += int(pending is not None) + int(active is not None)
    return ContinuousReplayResult(
        native,
        tuple(trade_records),
        tuple(intent_records),
        machine.counters(native),
        processed,
        missing,
        dropped,
        scored[0] if scored else None,
        scored[-1] if scored else None,
    )


def _profit_factor(rows: list[dict[str, Any]]) -> float:
    gains = sum(float(row["net_bps"]) for row in rows if float(row["net_bps"]) > 0)
    losses = -sum(float(row["net_bps"]) for row in rows if float(row["net_bps"]) < 0)
    return gains / losses if losses else (float("inf") if gains else 0.0)


def replay_metrics(results: tuple[ContinuousReplayResult, ...]) -> dict[str, Any]:
    trades = sorted(
        (row for result in results for row in result.trade_records),
        key=lambda row: row["exit_ts"],
    )
    intents = [row for result in results for row in result.intent_records]
    starts = [result.score_started_at for result in results if result.score_started_at]
    ends = [result.score_ended_at for result in results if result.score_ended_at]
    days = max(1.0, (max(ends) - min(starts)).total_seconds() / 86_400) if starts and ends else 1.0
    net = [float(row["net_bps"]) for row in trades]
    gross = [float(row["gross_bps"]) for row in trades]
    costs = [float(row["cost_bps"]) for row in trades]
    markets: dict[str, dict[str, Any]] = {}
    for result in results:
        rows = list(result.trade_records)
        values = [float(row["net_bps"]) for row in rows]
        markets[result.symbol] = {
            "intents": len(result.intent_records),
            "entry_rejections": sum(
                row["entry_geometry"]["status"] == "rejected" for row in result.intent_records
            ),
            "trades": len(rows),
            "net_bps": sum(values),
            "average_net_bps": fmean(values) if values else 0.0,
            "profit_factor": _profit_factor(rows),
            "missing_minutes": result.missing_minutes,
            "unresolved_dropped": result.unresolved_dropped,
            "state_counters": result.state_counters,
        }
    maximum_market_fraction = (
        max((len(result.trade_records) for result in results), default=0) / len(trades)
        if trades
        else 0.0
    )
    return {
        "intents": len(intents),
        "entry_rejections": sum(row["entry_geometry"]["status"] == "rejected" for row in intents),
        "entry_rejection_reasons": dict(
            Counter(
                row["entry_geometry"]["reason"]
                for row in intents
                if row["entry_geometry"]["status"] == "rejected"
            )
        ),
        "trades": len(trades),
        "trades_per_day": len(trades) / days,
        "average_gross_bps": fmean(gross) if gross else 0.0,
        "average_cost_bps": fmean(costs) if costs else 0.0,
        "average_net_bps": fmean(net) if net else 0.0,
        "net_bps": sum(net),
        "profit_factor": _profit_factor(trades),
        "win_rate": sum(value > 0 for value in net) / len(net) if net else 0.0,
        "false_signal_rate": sum(value <= 0 for value in net) / len(net) if net else 0.0,
        "exit_reasons": dict(Counter(row["exit_reason"] for row in trades)),
        "setup_types": dict(Counter(row["candidate_metadata"]["signal_type"] for row in trades)),
        "positive_markets": sum(float(row["net_bps"]) > 0 for row in markets.values()),
        "maximum_single_market_trade_fraction": maximum_market_fraction,
        "missing_minutes": sum(result.missing_minutes for result in results),
        "unresolved_dropped": sum(result.unresolved_dropped for result in results),
        "data_quality_pass": all(
            result.missing_minutes == 0 and result.unresolved_dropped == 0 for result in results
        ),
        "markets": markets,
        "score_started_at": min(starts).isoformat() if starts else None,
        "score_ended_at": max(ends).isoformat() if ends else None,
    }


def selection_gate(
    results: tuple[ContinuousReplayResult, ...], raw: dict[str, Any]
) -> dict[str, Any]:
    metrics = replay_metrics(results)
    rules = raw["validation"]
    checks = {
        "data_quality": bool(metrics["data_quality_pass"]),
        "minimum_trades": int(metrics["trades"]) >= int(rules["minimum_selection_trades"]),
        "gross_clears_cost": float(metrics["average_gross_bps"])
        > float(metrics["average_cost_bps"]),
        "positive_net": float(metrics["average_net_bps"]) > 0,
        "profit_factor": float(metrics["profit_factor"])
        >= float(rules["minimum_selection_profit_factor"]),
        "positive_markets": int(metrics["positive_markets"])
        >= int(rules["minimum_positive_markets"]),
        "false_signal_rate": float(metrics["false_signal_rate"])
        < float(rules["maximum_false_signal_rate"]),
        "market_concentration": float(metrics["maximum_single_market_trade_fraction"])
        <= float(rules["maximum_single_market_trade_fraction"]),
    }
    return {"passed": all(checks.values()), "checks": checks, "thresholds": rules}


async def run(args: argparse.Namespace) -> dict[str, Any]:
    config_path = Path(args.config)
    _, raw = load_continuous_mtf_config(config_path)
    start = _parse_ts(raw["data"]["start"])
    boundary = _parse_ts(raw["data"]["selection_boundary"])
    embargo = int(raw["data"]["selection_decision_embargo_minutes"])
    score_end = boundary - timedelta(minutes=embargo)
    symbols = list(raw["data"]["symbols"])
    loaded = await asyncio.gather(
        *(
            _load_candles(
                symbol,
                start,
                boundary,
                cache_dir=Path(args.cache_dir),
                refresh=False,
            )
            for symbol in symbols
        )
    )
    results = tuple(
        simulate_symbol_selection(
            symbol,
            rows,
            config_path,
            score_end=score_end,
            process_end=boundary,
        )
        for symbol, rows in zip(symbols, loaded, strict=True)
    )
    metrics = replay_metrics(results)
    gate = selection_gate(results, raw)
    trades = sorted(
        (row for result in results for row in result.trade_records),
        key=lambda row: row["exit_ts"],
    )
    contract_id = str(raw["contract_id"])
    payload = {
        "report_id": f"{contract_id}_first_selection_replay",
        "generated_at": datetime.now(UTC).isoformat(),
        "contract": str(config_path),
        "contract_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "window": {"start": start.isoformat(), "selection_boundary": boundary.isoformat()},
        "selection": {
            "decision_end": score_end.isoformat(),
            "metrics": metrics,
            "gate": gate,
            "trades": trades,
            "intent_records": [row for result in results for row in result.intent_records],
            "daily": _group_trades(trades, "day", start=start, end=boundary),
            "weekly": _group_trades(trades, "week", start=start, end=boundary),
            "monthly": _group_trades(trades, "month", start=start, end=boundary),
            "quarterly": _group_trades(trades, "quarter", start=start, end=boundary),
        },
        "untouched": {
            "status": "sealed",
            "eligible_to_open": bool(gate["passed"]),
            "automatic_open_forbidden": True,
            "price_values_read": False,
            "predictions_computed": False,
            "trades_computed": False,
            "opened_once": False,
            "start": raw["data"]["selection_boundary"],
            "end": raw["data"]["end"],
        },
        "policy": {
            "single_continuous_state": True,
            "all_shared_timestamp_updates_before_evaluation": True,
            "closed_candles_only": True,
            "next_bar_entry": True,
            "same_bar_stop_and_target": "stop_first",
            "structural_target_never_expanded": True,
            "regime_or_meta_filter_used": False,
            "l2_used": False,
            "research_only": True,
            "can_trade": False,
            "can_promote": False,
            "order_route_present": False,
            "mechanical_bos_choch": contract_id == "continuous_mtf_alignment_v2",
        },
        "can_trade": False,
        "can_promote": False,
    }
    _atomic_json(Path(args.output), payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    payload = asyncio.run(run(args))
    metrics = payload["selection"]["metrics"]
    print(
        json.dumps(
            {
                "selection_passed": payload["selection"]["gate"]["passed"],
                "intents": metrics["intents"],
                "trades": metrics["trades"],
                "average_net_bps": metrics["average_net_bps"],
                "profit_factor": metrics["profit_factor"],
                "untouched": payload["untouched"]["status"],
                "output": str(args.output),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
