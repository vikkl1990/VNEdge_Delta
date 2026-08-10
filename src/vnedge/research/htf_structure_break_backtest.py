"""Selection-only causal replay for preregistered htf_structure_break_v1."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from collections import Counter, deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import fmean
from tempfile import NamedTemporaryFile

import pandas as pd

from vnedge.scalping.delta_engine.backtester import (
    BacktestTrade,
    CausalScalperBacktester,
    OpenBacktestTrade,
)
from vnedge.scalping.delta_engine.candle_store import ClosedCandleAggregator, MultiTimeframeCandleStore
from vnedge.scalping.delta_engine.htf_structure_break import (
    HTFStructureBreakConfig,
    HTFStructureBreakScanner,
    HTFStructureContext,
    htf_structure_fee_model,
    load_htf_structure_break_config,
)
from vnedge.scalping.delta_engine.types import Candle, SignalCandidate

DEFAULT_CONFIG = Path("configs/research/htf_structure_break_v1.yaml")
DEFAULT_CACHE = Path("data/delta_scalper_cache")
DEFAULT_OUTPUT = Path("research/live_research/htf_structure_break_v1_latest.json")


@dataclass(frozen=True)
class HTFReplayResult:
    symbol: str
    trades: tuple[BacktestTrade, ...]
    missing_minutes: int
    unresolved_dropped: int
    source_bars: int
    decision_bars: int
    started_at: datetime | None
    ended_at: datetime | None
    scanner_funnel: dict[str, int] | None = None


def _parse_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def load_cached_candles(
    cache_dir: Path, symbol: str, start: datetime, end: datetime
) -> list[Candle]:
    """Read only existing local shards; this selection run never downloads or opens the tail."""
    frames = [pd.read_parquet(path) for path in sorted(cache_dir.glob(f"{symbol}_1m_*.parquet"))]
    if not frames:
        raise FileNotFoundError(f"no cached candles for {symbol}")
    frame = pd.concat(frames, ignore_index=True).drop_duplicates("timestamp", keep="last")
    stamps = pd.to_datetime(frame["timestamp"], utc=True)
    frame = frame.loc[(stamps >= start) & (stamps < end)].copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame = frame.sort_values("timestamp")
    return [
        Candle(
            ts=row.timestamp.to_pydatetime() + timedelta(minutes=1),
            open=float(row.open),
            high=float(row.high),
            low=float(row.low),
            close=float(row.close),
            volume=float(row.volume),
            tf="1m",
        )
        for row in frame.itertuples(index=False)
    ]


def simulate_selection(
    symbol: str,
    rows: list[Candle],
    config: HTFStructureBreakConfig,
    *,
    decision_end_exclusive: datetime,
) -> HTFReplayResult:
    native = symbol.upper()
    fee = htf_structure_fee_model(config)
    scanner = HTFStructureBreakScanner(config, fee)
    resolver = CausalScalperBacktester(
        None,  # type: ignore[arg-type] -- canonical stop-first resolver only
        fee,
        MultiTimeframeCandleStore(),
    )
    aggregator = ClosedCandleAggregator(("1h", "4h"))
    one_hour: deque[Candle] = deque(maxlen=1_500)
    four_hour: deque[Candle] = deque(maxlen=1_500)
    pending: SignalCandidate | None = None
    active: OpenBacktestTrade | None = None
    trades: list[BacktestTrade] = []
    previous_ts: datetime | None = None
    missing = 0
    dropped = 0
    source_bars = 0
    decision_bars = 0
    started: datetime | None = None
    ended: datetime | None = None
    for bar in sorted(rows, key=lambda item: item.ts):
        if bar.tf != "1m":
            raise ValueError("HTF replay accepts closed 1m candles only")
        if previous_ts is not None:
            if bar.ts <= previous_ts:
                raise ValueError("source candles must have unique ascending timestamps")
            gap = max(0, int((bar.ts - previous_ts).total_seconds() // 60) - 1)
            if gap:
                missing += gap
                dropped += int(pending is not None) + int(active is not None)
                pending = None
                active = None
                aggregator = ClosedCandleAggregator(("1h", "4h"))
                one_hour.clear()
                four_hour.clear()
                scanner.reset(native)
        previous_ts = bar.ts
        source_bars += 1
        if pending is not None and active is None:
            active = OpenBacktestTrade(pending, bar)
            pending = None
        if active is not None:
            resolved = resolver.resolve_on_bar(active, bar)
            if resolved is not None:
                trades.append(resolved)
                active = None
        emitted = aggregator.on_one_minute(native, bar)
        # At shared closes the 4h state must exist before the 1h decision.
        for higher in sorted(emitted, key=lambda item: 0 if item.tf == "4h" else 1):
            if higher.tf == "4h":
                four_hour.append(higher)
                scanner.update_four_hour(native, tuple(four_hour))
                continue
            one_hour.append(higher)
            decision_bars += 1
            if higher.ts >= decision_end_exclusive:
                continue
            started = started or higher.ts
            ended = higher.ts
            if pending is None and active is None and four_hour:
                pending = scanner.evaluate(
                    HTFStructureContext(
                        symbol=native,
                        ts=higher.ts,
                        one_hour=tuple(one_hour),
                        four_hour=tuple(four_hour),
                    )
                )
    dropped += int(pending is not None) + int(active is not None)
    return HTFReplayResult(
        symbol=native,
        trades=tuple(trades),
        missing_minutes=missing,
        unresolved_dropped=dropped,
        source_bars=source_bars,
        decision_bars=decision_bars,
        started_at=started,
        ended_at=ended,
        scanner_funnel=dict(scanner.rejections),
    )


def _profit_factor(trades: tuple[BacktestTrade, ...]) -> float:
    gains = sum(row.net_bps for row in trades if row.net_bps > 0)
    losses = -sum(row.net_bps for row in trades if row.net_bps < 0)
    return gains / losses if losses else (float("inf") if gains else 0.0)


def replay_metrics(results: tuple[HTFReplayResult, ...]) -> dict[str, object]:
    trades = tuple(sorted((trade for result in results for trade in result.trades), key=lambda row: row.decision_ts))
    starts = [row.started_at for row in results if row.started_at]
    ends = [row.ended_at for row in results if row.ended_at]
    days = max(1.0, (max(ends) - min(starts)).total_seconds() / 86_400) if starts and ends else 1.0
    market: dict[str, dict[str, object]] = {}
    for result in results:
        market[result.symbol] = {
            "trades": len(result.trades),
            "net_bps": sum(row.net_bps for row in result.trades),
            "average_net_bps": fmean(row.net_bps for row in result.trades) if result.trades else 0.0,
            "profit_factor": _profit_factor(result.trades),
            "missing_minutes": result.missing_minutes,
            "unresolved_dropped": result.unresolved_dropped,
            "scanner_funnel": result.scanner_funnel or {},
        }
    gross = [row.gross_bps for row in trades]
    costs = [row.cost_bps for row in trades]
    net = [row.net_bps for row in trades]
    return {
        "trades": len(trades),
        "trades_per_day": len(trades) / days,
        "average_gross_bps": fmean(gross) if gross else 0.0,
        "average_cost_bps": fmean(costs) if costs else 0.0,
        "average_net_bps": fmean(net) if net else 0.0,
        "net_bps": sum(net),
        "profit_factor": _profit_factor(trades),
        "win_rate": sum(value > 0 for value in net) / len(net) if net else 0.0,
        "false_signal_rate": sum(value <= 0 for value in net) / len(net) if net else 0.0,
        "exit_reasons": dict(Counter(row.exit_reason for row in trades)),
        "sides": dict(Counter(row.side.value for row in trades)),
        "same_bar_ambiguity_rate": sum(row.same_bar_ambiguous for row in trades) / len(trades) if trades else 0.0,
        "missing_minutes": sum(row.missing_minutes for row in results),
        "unresolved_dropped": sum(row.unresolved_dropped for row in results),
        "data_quality_pass": all(row.missing_minutes == 0 and row.unresolved_dropped == 0 for row in results),
        "markets": market,
        "scanner_funnel": dict(
            sum((Counter(row.scanner_funnel or {}) for row in results), Counter())
        ),
    }


def selection_gate(results: tuple[HTFReplayResult, ...], config: HTFStructureBreakConfig) -> dict[str, object]:
    metrics = replay_metrics(results)
    trades = tuple(sorted((trade for result in results for trade in result.trades), key=lambda row: row.decision_ts))
    midpoint = _parse_ts(config.data.selection_start) + (
        _parse_ts(config.data.selection_end_exclusive) - _parse_ts(config.data.selection_start)
    ) / 2
    first = tuple(row for row in trades if row.decision_ts < midpoint)
    second = tuple(row for row in trades if row.decision_ts >= midpoint)
    markets = metrics["markets"]
    assert isinstance(markets, dict)
    v = config.validation
    checks = {
        "data_quality": bool(metrics["data_quality_pass"]),
        "minimum_trades": len(trades) >= v.minimum_selection_trades,
        "minimum_trades_per_half": len(first) >= v.minimum_selection_trades_per_half and len(second) >= v.minimum_selection_trades_per_half,
        "minimum_trades_per_market": all(int(markets[symbol]["trades"]) >= v.minimum_selection_trades_per_market for symbol in config.data.symbols),
        "average_net": float(metrics["average_net_bps"]) >= v.minimum_selection_average_net_bps,
        "profit_factor": float(metrics["profit_factor"]) >= v.minimum_selection_profit_factor,
        "gross_clears_cost": float(metrics["average_gross_bps"]) > float(metrics["average_cost_bps"]),
        "frequency": float(metrics["trades_per_day"]) <= v.maximum_trades_per_day_per_market * len(config.data.symbols) and float(metrics["trades_per_day"]) >= v.minimum_trades_per_day,
        "positive_halves": sum(row.net_bps for row in first) > 0 and sum(row.net_bps for row in second) > 0,
        "positive_markets": all(float(markets[symbol]["net_bps"]) > 0 for symbol in config.data.symbols),
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "first_half": {"trades": len(first), "net_bps": sum(row.net_bps for row in first)},
        "second_half": {"trades": len(second), "net_bps": sum(row.net_bps for row in second)},
        "thresholds": v.model_dump(),
    }


async def run(args: argparse.Namespace) -> dict[str, object]:
    config_path = Path(args.config)
    config = load_htf_structure_break_config(config_path)
    start = _parse_ts(config.data.selection_start)
    decision_end = _parse_ts(config.data.selection_end_exclusive)
    process_end = decision_end + timedelta(hours=config.data.embargo_hours)
    loaded = [
        load_cached_candles(Path(args.cache_dir), symbol, start, process_end)
        for symbol in config.data.symbols
    ]
    results = tuple(
        simulate_selection(symbol, rows, config, decision_end_exclusive=decision_end)
        for symbol, rows in zip(config.data.symbols, loaded, strict=True)
    )
    metrics = replay_metrics(results)
    gate = selection_gate(results, config)
    payload: dict[str, object] = {
        "report_id": "htf_structure_break_v1_selection_replay",
        "generated_at": datetime.now(UTC).isoformat(),
        "contract": str(config_path),
        "contract_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "selection_window": {"start": start.isoformat(), "end_exclusive": decision_end.isoformat()},
        "embargo": {"hours": config.data.embargo_hours, "processing_end": process_end.isoformat()},
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
            "next_1h_open_entry": True,
            "stop_first": True,
            "l2_used": False,
            "funding_used": False,
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
    print(json.dumps({"selection": payload["selection"], "untouched": payload["untouched"]}, indent=2))


if __name__ == "__main__":
    main()
