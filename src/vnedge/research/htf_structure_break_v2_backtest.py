"""Selection-only causal replay for the frozen htf_structure_break_v2 contract."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from collections import Counter, deque
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import fmean
from tempfile import NamedTemporaryFile

import pandas as pd
import yaml

from vnedge.research.delta_funding_audit import (
    audit_raw_funding,
    fetch_raw_funding_candles,
)
from vnedge.research.htf_structure_break_backtest import load_cached_candles
from vnedge.scalping.delta_engine.candle_store import ClosedCandleAggregator
from vnedge.scalping.delta_engine.fee_model import DeltaFeeModel
from vnedge.scalping.delta_engine.htf_structure_break_v2 import (
    HTFStructureBreakV2Config,
    HTFStructureBreakV2Scanner,
    HTFStructureV2Context,
    geometry_valid_at_entry,
    htf_structure_v2_fee_model,
    load_htf_structure_break_v2_config,
)
from vnedge.scalping.delta_engine.types import Candle, Side, SignalCandidate

DEFAULT_CONFIG = Path("configs/research/htf_structure_break_v2.yaml")
DEFAULT_FUNDING_AUDIT_CONFIG = Path("configs/research/delta_funding_audit_v1.yaml")
DEFAULT_CACHE = Path("data/delta_scalper_cache")
DEFAULT_OUTPUT = Path("research/live_research/htf_structure_break_v2_latest.json")


@dataclass
class OpenObservation:
    candidate: SignalCandidate
    entry_ts: datetime
    entry_price: float
    stop_price: float
    target_price: float
    mfe_bps: float = 0.0
    mae_bps: float = 0.0


@dataclass(frozen=True)
class HTFV2Trade:
    scanner_id: str
    intent_key: str
    symbol: str
    side: str
    decision_ts: datetime
    entry_ts: datetime
    exit_ts: datetime
    entry_price: float
    exit_price: float
    exit_reason: str
    hold_seconds: int
    gross_bps: float
    base_cost_bps: float
    funding_bps: float
    funding_settlements: int
    cost_bps: float
    net_bps: float
    mfe_bps: float
    mae_bps: float
    stop_bps: float
    target_bps: float
    same_bar_ambiguous: bool

    def to_dict(self) -> dict[str, object]:
        row = self.__dict__.copy()
        for name in ("decision_ts", "entry_ts", "exit_ts"):
            row[name] = getattr(self, name).isoformat()
        return row


@dataclass(frozen=True)
class HTFV2ReplayResult:
    symbol: str
    trades: tuple[HTFV2Trade, ...]
    missing_minutes: int
    unresolved_dropped: int
    pending_geometry_rejections: int
    source_bars: int
    decision_bars: int
    started_at: datetime | None
    ended_at: datetime | None
    scanner_funnel: dict[str, int]
    funding_integrity_passed: bool
    funding_settlement_rows: int


def _parse_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w", dir=path.parent, delete=False, encoding="utf-8"
    ) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def funding_settlements_from_hourly(
    frame: pd.DataFrame, *, interval_seconds: int
) -> tuple[tuple[datetime, float], ...]:
    """Keep only final hourly rates available at actual 8h exchange events."""

    required = {"available_at", "funding_rate"}
    if not required.issubset(frame.columns):
        raise ValueError("funding frame lacks causal availability or rate")
    ordered = frame.sort_values("available_at").drop_duplicates("available_at", keep="last")
    rows: list[tuple[datetime, float]] = []
    for row in ordered.itertuples(index=False):
        stamp = pd.Timestamp(row.available_at)
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize("UTC")
        else:
            stamp = stamp.tz_convert("UTC")
        seconds = int(stamp.timestamp())
        if seconds % interval_seconds == 0:
            rows.append((stamp.to_pydatetime(), float(row.funding_rate)))
    return tuple(rows)


def _crossed_funding(
    settlements: tuple[tuple[datetime, float], ...],
    entry_ts: datetime,
    exit_ts: datetime,
) -> tuple[float, ...]:
    return tuple(rate for ts, rate in settlements if entry_ts < ts <= exit_ts)


def _resolve(
    active: OpenObservation,
    bar: Candle,
    fee_model: DeltaFeeModel,
    settlements: tuple[tuple[datetime, float], ...],
) -> HTFV2Trade | None:
    candidate = active.candidate
    entry = active.entry_price
    hold = int((bar.ts - active.entry_ts).total_seconds())
    if candidate.side is Side.LONG:
        gross = lambda price: (price / entry - 1.0) * 10_000.0
        stop_hit = bar.low <= active.stop_price
        target_hit = bar.high >= active.target_price
        favorable = gross(bar.high)
        adverse = max(0.0, -gross(bar.low))
    else:
        gross = lambda price: (entry / price - 1.0) * 10_000.0
        stop_hit = bar.high >= active.stop_price
        target_hit = bar.low <= active.target_price
        favorable = gross(bar.low)
        adverse = max(0.0, -gross(bar.high))
    active.mfe_bps = max(active.mfe_bps, max(0.0, favorable))
    active.mae_bps = max(active.mae_bps, adverse)
    if stop_hit:
        exit_price, reason = active.stop_price, "stop"
    elif target_hit:
        exit_price, reason = active.target_price, "target"
    elif hold >= candidate.time_stop_seconds:
        exit_price, reason = bar.close, "time_stop"
    else:
        return None

    rates = _crossed_funding(settlements, active.entry_ts, bar.ts)
    funding_bps = fee_model.settled_funding_bps(candidate.side.value, rates)
    costs = fee_model.breakdown(
        candidate.symbol,
        entry_is_maker=False,
        exit_is_maker=False,
        hold_seconds=hold,
        funding_bps=funding_bps,
    )
    base_cost = costs.entry_fee_bps + costs.exit_fee_bps + costs.slippage_bps
    gross_bps = gross(exit_price)
    return HTFV2Trade(
        scanner_id=candidate.scanner_id,
        intent_key=candidate.dedup_key,
        symbol=candidate.symbol,
        side=candidate.side.value,
        decision_ts=candidate.decision_ts,
        entry_ts=active.entry_ts,
        exit_ts=bar.ts,
        entry_price=entry,
        exit_price=exit_price,
        exit_reason=reason,
        hold_seconds=hold,
        gross_bps=gross_bps,
        base_cost_bps=base_cost,
        funding_bps=funding_bps,
        funding_settlements=len(rates),
        cost_bps=costs.total_bps,
        net_bps=gross_bps - costs.total_bps,
        mfe_bps=active.mfe_bps,
        mae_bps=active.mae_bps,
        stop_bps=abs(active.stop_price / entry - 1.0) * 10_000.0,
        target_bps=abs(active.target_price / entry - 1.0) * 10_000.0,
        same_bar_ambiguous=stop_hit and target_hit,
    )


def simulate_selection(
    symbol: str,
    rows: list[Candle],
    settlements: tuple[tuple[datetime, float], ...],
    config: HTFStructureBreakV2Config,
    *,
    decision_end_exclusive: datetime,
    funding_integrity_passed: bool = True,
) -> HTFV2ReplayResult:
    native = symbol.upper()
    fee_model = htf_structure_v2_fee_model(config)
    scanner = HTFStructureBreakV2Scanner(config, fee_model)
    aggregator = ClosedCandleAggregator(("1h", "4h"))
    one_hour: deque[Candle] = deque(maxlen=2_000)
    four_hour: deque[Candle] = deque(maxlen=2_000)
    pending: SignalCandidate | None = None
    active: OpenObservation | None = None
    trades: list[HTFV2Trade] = []
    seen_intents: set[str] = set()
    previous_ts: datetime | None = None
    missing = dropped = geometry_rejections = source_bars = decision_bars = 0
    started: datetime | None = None
    ended: datetime | None = None

    for bar in sorted(rows, key=lambda item: item.ts):
        if bar.tf != "1m":
            raise ValueError("HTF v2 replay accepts closed 1m candles only")
        if previous_ts is not None:
            if bar.ts <= previous_ts:
                raise ValueError("source candles must have unique ascending timestamps")
            gap = max(0, int((bar.ts - previous_ts).total_seconds() // 60) - 1)
            if gap:
                missing += gap
                dropped += int(pending is not None) + int(active is not None)
                pending = None
                active = None
                one_hour.clear()
                four_hour.clear()
                aggregator = ClosedCandleAggregator(("1h", "4h"))
                scanner.reset(native)
        previous_ts = bar.ts
        source_bars += 1

        if pending is not None and active is None:
            valid, _, _, target_bps = geometry_valid_at_entry(
                pending,
                bar.open,
                fee_model,
                config.exit.minimum_target_cost_multiple,
            )
            if valid:
                repriced = replace(
                    pending,
                    entry_price=bar.open,
                    expected_move_bps=target_bps,
                )
                active = OpenObservation(
                    candidate=repriced,
                    entry_ts=bar.ts - timedelta(minutes=1),
                    entry_price=bar.open,
                    stop_price=repriced.stop_loss,
                    target_price=repriced.take_profits[0],
                )
            else:
                geometry_rejections += 1
            pending = None

        if active is not None:
            resolved = _resolve(active, bar, fee_model, settlements)
            if resolved is not None:
                trades.append(resolved)
                active = None

        emitted = aggregator.on_one_minute(native, bar)
        for higher in sorted(emitted, key=lambda item: 0 if item.tf == "4h" else 1):
            if higher.tf == "4h":
                four_hour.append(higher)
                continue
            one_hour.append(higher)
            decision_bars += 1
            if higher.ts >= decision_end_exclusive:
                continue
            started = started or higher.ts
            ended = higher.ts
            if pending is not None or active is not None or not four_hour:
                continue
            candidate = scanner.evaluate(
                HTFStructureV2Context(
                    symbol=native,
                    ts=higher.ts,
                    one_hour=tuple(one_hour),
                    four_hour=tuple(four_hour),
                )
            )
            if candidate is None or candidate.dedup_key in seen_intents:
                continue
            seen_intents.add(candidate.dedup_key)
            pending = candidate

    dropped += int(pending is not None) + int(active is not None)
    return HTFV2ReplayResult(
        symbol=native,
        trades=tuple(trades),
        missing_minutes=missing,
        unresolved_dropped=dropped,
        pending_geometry_rejections=geometry_rejections,
        source_bars=source_bars,
        decision_bars=decision_bars,
        started_at=started,
        ended_at=ended,
        scanner_funnel=dict(scanner.rejections),
        funding_integrity_passed=funding_integrity_passed,
        funding_settlement_rows=len(settlements),
    )


def _profit_factor(trades: tuple[HTFV2Trade, ...]) -> float:
    gains = sum(row.net_bps for row in trades if row.net_bps > 0)
    losses = -sum(row.net_bps for row in trades if row.net_bps < 0)
    return gains / losses if losses else (float("inf") if gains else 0.0)


def replay_metrics(results: tuple[HTFV2ReplayResult, ...]) -> dict[str, object]:
    trades = tuple(
        sorted(
            (trade for result in results for trade in result.trades),
            key=lambda row: row.decision_ts,
        )
    )
    starts = [row.started_at for row in results if row.started_at]
    ends = [row.ended_at for row in results if row.ended_at]
    days = (
        max(1.0, (max(ends) - min(starts)).total_seconds() / 86_400)
        if starts and ends
        else 1.0
    )
    market: dict[str, dict[str, object]] = {}
    for result in results:
        market[result.symbol] = {
            "trades": len(result.trades),
            "net_bps": sum(row.net_bps for row in result.trades),
            "average_net_bps": (
                fmean(row.net_bps for row in result.trades) if result.trades else 0.0
            ),
            "profit_factor": _profit_factor(result.trades),
            "missing_minutes": result.missing_minutes,
            "unresolved_dropped": result.unresolved_dropped,
            "pending_geometry_rejections": result.pending_geometry_rejections,
            "funding_settlement_rows": result.funding_settlement_rows,
            "scanner_funnel": result.scanner_funnel,
        }
    gross = [row.gross_bps for row in trades]
    base_cost = [row.base_cost_bps for row in trades]
    funding = [row.funding_bps for row in trades]
    total_cost = [row.cost_bps for row in trades]
    net = [row.net_bps for row in trades]
    return {
        "trades": len(trades),
        "trades_per_day": len(trades) / days,
        "average_gross_bps": fmean(gross) if gross else 0.0,
        "average_base_cost_bps": fmean(base_cost) if base_cost else 0.0,
        "average_funding_bps": fmean(funding) if funding else 0.0,
        "average_total_cost_bps": fmean(total_cost) if total_cost else 0.0,
        "average_net_bps": fmean(net) if net else 0.0,
        "net_bps": sum(net),
        "profit_factor": _profit_factor(trades),
        "win_rate": sum(value > 0 for value in net) / len(net) if net else 0.0,
        "false_signal_rate": sum(value <= 0 for value in net) / len(net) if net else 0.0,
        "exit_reasons": dict(Counter(row.exit_reason for row in trades)),
        "sides": dict(Counter(row.side for row in trades)),
        "same_bar_ambiguity_rate": (
            sum(row.same_bar_ambiguous for row in trades) / len(trades) if trades else 0.0
        ),
        "funding_settlements_crossed": sum(row.funding_settlements for row in trades),
        "funding_used": all(row.funding_integrity_passed for row in results),
        "missing_minutes": sum(row.missing_minutes for row in results),
        "unresolved_dropped": sum(row.unresolved_dropped for row in results),
        "data_quality_pass": all(
            row.missing_minutes == 0
            and row.unresolved_dropped == 0
            and row.funding_integrity_passed
            for row in results
        ),
        "markets": market,
        "scanner_funnel": dict(
            sum((Counter(row.scanner_funnel) for row in results), Counter())
        ),
    }


def selection_gate(
    results: tuple[HTFV2ReplayResult, ...], config: HTFStructureBreakV2Config
) -> dict[str, object]:
    metrics = replay_metrics(results)
    trades = tuple(
        sorted(
            (trade for result in results for trade in result.trades),
            key=lambda row: row.decision_ts,
        )
    )
    start = _parse_ts(config.data.selection_start)
    end = _parse_ts(config.data.selection_end_exclusive)
    midpoint = start + (end - start) / 2
    first = tuple(row for row in trades if row.decision_ts < midpoint)
    second = tuple(row for row in trades if row.decision_ts >= midpoint)
    markets = metrics["markets"]
    assert isinstance(markets, dict)
    rules = config.validation
    checks = {
        "data_quality": bool(metrics["data_quality_pass"]),
        "funding_used": bool(metrics["funding_used"]),
        "minimum_trades": len(trades) >= rules.minimum_selection_trades,
        "minimum_trades_per_half": (
            len(first) >= rules.minimum_selection_trades_per_half
            and len(second) >= rules.minimum_selection_trades_per_half
        ),
        "minimum_trades_per_market": all(
            int(markets[symbol]["trades"]) >= rules.minimum_selection_trades_per_market
            for symbol in config.data.symbols
        ),
        "average_net": float(metrics["average_net_bps"])
        >= rules.minimum_selection_average_net_bps,
        "profit_factor": float(metrics["profit_factor"])
        >= rules.minimum_selection_profit_factor,
        "gross_clears_cost": float(metrics["average_gross_bps"])
        > float(metrics["average_total_cost_bps"]),
        "frequency": rules.minimum_trades_per_day
        <= float(metrics["trades_per_day"])
        <= rules.maximum_trades_per_day_per_market * len(config.data.symbols),
        "positive_halves": (
            sum(row.net_bps for row in first) > 0
            and sum(row.net_bps for row in second) > 0
        ),
        "positive_markets": all(
            float(markets[symbol]["net_bps"]) > 0 for symbol in config.data.symbols
        ),
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "first_half": {"trades": len(first), "net_bps": sum(row.net_bps for row in first)},
        "second_half": {
            "trades": len(second),
            "net_bps": sum(row.net_bps for row in second),
        },
        "thresholds": rules.model_dump(),
    }


async def _fetch_funding(
    config: HTFStructureBreakV2Config, start: datetime, end: datetime
) -> tuple[dict[str, tuple[tuple[datetime, float], ...]], dict[str, object]]:
    audit_config = yaml.safe_load(DEFAULT_FUNDING_AUDIT_CONFIG.read_text(encoding="utf-8"))
    settings = audit_config["funding_integrity"]
    raw_rows = await asyncio.gather(
        *(fetch_raw_funding_candles(symbol, start, end) for symbol in config.data.symbols)
    )
    settlements: dict[str, tuple[tuple[datetime, float], ...]] = {}
    reports: dict[str, object] = {}
    for raw in raw_rows:
        frame, report = audit_raw_funding(raw, start, end, settings)
        reports[raw.symbol] = report
        if report["passed"]:
            settlements[raw.symbol] = funding_settlements_from_hourly(
                frame,
                interval_seconds=config.costs.funding_settlement_interval_seconds,
            )
        else:
            settlements[raw.symbol] = ()
    return settlements, reports


async def run(args: argparse.Namespace) -> dict[str, object]:
    config_path = Path(args.config)
    config = load_htf_structure_break_v2_config(config_path)
    start = _parse_ts(config.data.selection_start)
    decision_end = _parse_ts(config.data.selection_end_exclusive)
    process_end = decision_end + timedelta(hours=config.data.embargo_hours)
    funding, funding_reports = await _fetch_funding(config, start, process_end)
    loaded = [
        load_cached_candles(Path(args.cache_dir), symbol, start, process_end)
        for symbol in config.data.symbols
    ]
    results = tuple(
        simulate_selection(
            symbol,
            rows,
            funding[symbol],
            config,
            decision_end_exclusive=decision_end,
            funding_integrity_passed=bool(funding_reports[symbol]["passed"]),
        )
        for symbol, rows in zip(config.data.symbols, loaded, strict=True)
    )
    metrics = replay_metrics(results)
    gate = selection_gate(results, config)
    payload: dict[str, object] = {
        "report_id": "htf_structure_break_v2_selection_replay",
        "generated_at": datetime.now(UTC).isoformat(),
        "contract": str(config_path),
        "contract_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "selection_window": {"start": start.isoformat(), "end_exclusive": decision_end.isoformat()},
        "embargo": {"hours": config.data.embargo_hours, "processing_end": process_end.isoformat()},
        "funding_integrity": funding_reports,
        "selection": {
            "metrics": metrics,
            "gate": gate,
            "trades": [trade.to_dict() for result in results for trade in result.trades],
        },
        "untouched": {
            "status": "sealed",
            "start": config.data.untouched_start,
            "end_exclusive": config.data.untouched_end_exclusive,
            "loaded": False,
            "predictions_computed": False,
            "trades_computed": False,
            "eligible_to_open": bool(gate["passed"]),
        },
        "policy": {
            "research_only": True,
            "can_trade": False,
            "can_promote": False,
            "next_1h_interval_open_entry": True,
            "stop_first": True,
            "funding_used": bool(metrics["funding_used"]),
            "l2_used": False,
            "meta_model_used": False,
            "parameters_tunable_on_this_window": False,
        },
        "can_trade": False,
        "can_promote": False,
    }
    _atomic_json(Path(args.output), payload)
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    return parser


def main() -> None:
    payload = asyncio.run(run(_parser().parse_args()))
    print(
        json.dumps(
            {
                "selection": payload["selection"],
                "untouched": payload["untouched"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
