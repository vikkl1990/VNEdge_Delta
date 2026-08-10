"""First causal replay for preregistered range_compression_breakout_v1."""

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

from vnedge.research.delta_scalper_backtest import _load_candles
from vnedge.scalping.delta_engine.backtester import (
    BacktestTrade,
    CausalScalperBacktester,
    OpenBacktestTrade,
)
from vnedge.scalping.delta_engine.candle_store import (
    ClosedCandleAggregator,
    MultiTimeframeCandleStore,
)
from vnedge.scalping.delta_engine.change_point import CausalCusumDetector
from vnedge.scalping.delta_engine.range_compression import (
    RangeCompressionBreakoutScanner,
    RangeCompressionConfig,
    RangeCompressionContext,
    load_range_compression_config,
    range_compression_fee_model,
)
from vnedge.scalping.delta_engine.types import Candle, SignalCandidate

DEFAULT_CONFIG = Path("configs/research/range_compression_breakout_v1.yaml")
DEFAULT_CACHE = Path("data/delta_scalper_cache")
DEFAULT_OUTPUT = Path(
    "research/live_research/range_compression_breakout_v1_latest.json"
)


@dataclass(frozen=True)
class CompressionReplayResult:
    symbol: str
    trades: tuple[BacktestTrade, ...]
    missing_minutes: int
    unresolved_dropped: int
    source_bars: int
    decision_bars: int
    score_started_at: datetime | None
    score_ended_at: datetime | None


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def _parse_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def simulate_symbol_window(
    symbol: str,
    rows: list[Candle],
    config: RangeCompressionConfig,
    *,
    score_start: datetime | None = None,
    score_end: datetime | None = None,
    process_end: datetime | None = None,
) -> CompressionReplayResult:
    native = symbol.upper()
    fee_model = range_compression_fee_model(config)
    scanner = RangeCompressionBreakoutScanner(config, fee_model)
    detector = CausalCusumDetector(config.cusum)
    resolver = CausalScalperBacktester(
        None,  # type: ignore[arg-type] -- canonical exit resolver only
        fee_model,
        MultiTimeframeCandleStore(),
    )
    aggregator = ClosedCandleAggregator(("5m",))
    history: deque[Candle] = deque(maxlen=max(scanner.minimum_history, 300))
    pending: SignalCandidate | None = None
    active: OpenBacktestTrade | None = None
    trades: list[BacktestTrade] = []
    previous_ts: datetime | None = None
    missing = 0
    dropped = 0
    source_bars = 0
    decision_bars = 0
    scored: list[datetime] = []
    for bar in sorted(rows, key=lambda item: item.ts):
        if bar.tf != "1m":
            raise ValueError("range-compression replay accepts 1m candles only")
        if process_end is not None and bar.ts > process_end:
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
                aggregator = ClosedCandleAggregator(("5m",))
                history.clear()
                detector = CausalCusumDetector(config.cusum)
                scanner.reset()
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
        for five_minute in aggregator.on_one_minute(native, bar):
            decision_bars += 1
            history.append(five_minute)
            change_point = detector.update(tuple(history))
            scoring = (score_start is None or five_minute.ts >= score_start) and (
                score_end is None or five_minute.ts <= score_end
            )
            if scoring:
                scored.append(five_minute.ts)
            if scoring and pending is None and active is None:
                candidate = scanner.evaluate(
                    RangeCompressionContext(
                        native,
                        five_minute.ts,
                        tuple(history),
                        change_point,
                    )
                )
                if candidate is not None:
                    pending = candidate
    dropped += int(pending is not None) + int(active is not None)
    return CompressionReplayResult(
        symbol=native,
        trades=tuple(trades),
        missing_minutes=missing,
        unresolved_dropped=dropped,
        source_bars=source_bars,
        decision_bars=decision_bars,
        score_started_at=scored[0] if scored else None,
        score_ended_at=scored[-1] if scored else None,
    )


def _profit_factor(trades: tuple[BacktestTrade, ...]) -> float:
    gains = sum(row.net_bps for row in trades if row.net_bps > 0)
    losses = -sum(row.net_bps for row in trades if row.net_bps < 0)
    return gains / losses if losses else (float("inf") if gains else 0.0)


def replay_metrics(
    results: tuple[CompressionReplayResult, ...],
) -> dict[str, object]:
    trades = tuple(
        sorted(
            (trade for result in results for trade in result.trades),
            key=lambda row: row.decision_ts,
        )
    )
    starts = [row.score_started_at for row in results if row.score_started_at is not None]
    ends = [row.score_ended_at for row in results if row.score_ended_at is not None]
    days = max(1.0, (max(ends) - min(starts)).total_seconds() / 86_400) if starts and ends else 1.0
    net = [row.net_bps for row in trades]
    gross = [row.gross_bps for row in trades]
    costs = [row.cost_bps for row in trades]
    by_market: dict[str, dict[str, object]] = {}
    for result in results:
        market_net = [row.net_bps for row in result.trades]
        by_market[result.symbol] = {
            "trades": len(result.trades),
            "net_bps": sum(market_net),
            "average_net_bps": fmean(market_net) if market_net else 0.0,
            "profit_factor": _profit_factor(result.trades),
            "missing_minutes": result.missing_minutes,
            "unresolved_dropped": result.unresolved_dropped,
        }
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
        "same_bar_ambiguity_rate": (
            sum(row.same_bar_ambiguous for row in trades) / len(trades) if trades else 0.0
        ),
        "missing_minutes": sum(row.missing_minutes for row in results),
        "unresolved_dropped": sum(row.unresolved_dropped for row in results),
        "data_quality_pass": all(
            row.missing_minutes == 0 and row.unresolved_dropped == 0 for row in results
        ),
        "markets": by_market,
        "score_started_at": min(starts).isoformat() if starts else None,
        "score_ended_at": max(ends).isoformat() if ends else None,
    }


def selection_gate(
    results: tuple[CompressionReplayResult, ...],
    config: RangeCompressionConfig,
) -> dict[str, object]:
    metrics = replay_metrics(results)
    trades = tuple(
        sorted(
            (trade for result in results for trade in result.trades),
            key=lambda row: row.decision_ts,
        )
    )
    start = datetime.fromisoformat(str(metrics["score_started_at"]))
    end = datetime.fromisoformat(str(metrics["score_ended_at"]))
    midpoint = start + (end - start) / 2
    first = tuple(row for row in trades if row.decision_ts <= midpoint)
    second = tuple(row for row in trades if row.decision_ts > midpoint)
    validation = config.validation
    market_metrics = metrics["markets"]
    assert isinstance(market_metrics, dict)
    checks = {
        "data_quality": bool(metrics["data_quality_pass"]),
        "minimum_trades": len(trades) >= validation.minimum_selection_trades,
        "minimum_trades_per_half": (
            len(first) >= validation.minimum_selection_trades_per_half
            and len(second) >= validation.minimum_selection_trades_per_half
        ),
        "minimum_trades_per_market": all(
            int(market_metrics[symbol]["trades"])
            >= validation.minimum_selection_trades_per_market
            for symbol in config.data.symbols
        ),
        "average_net": (
            float(metrics["average_net_bps"])
            >= validation.minimum_selection_average_net_bps
        ),
        "profit_factor": (
            float(metrics["profit_factor"])
            >= validation.minimum_selection_profit_factor
        ),
        "gross_clears_cost": (
            float(metrics["average_gross_bps"]) > float(metrics["average_cost_bps"])
        ),
        "frequency": (
            validation.minimum_trades_per_day
            <= float(metrics["trades_per_day"])
            <= validation.maximum_trades_per_day
        ),
        "positive_halves": sum(row.net_bps for row in first) > 0
        and sum(row.net_bps for row in second) > 0,
        "positive_markets": all(
            float(market_metrics[symbol]["net_bps"]) > 0
            for symbol in config.data.symbols
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
        "thresholds": validation.model_dump(),
    }


async def run(args: argparse.Namespace) -> dict[str, object]:
    config_path = Path(args.config)
    config = load_range_compression_config(config_path)
    start = _parse_ts(config.data.start)
    end = _parse_ts(config.data.end)
    loaded = await asyncio.gather(
        *(
            _load_candles(
                symbol,
                start,
                end,
                cache_dir=Path(args.cache_dir),
                refresh=False,
            )
            for symbol in config.data.symbols
        )
    )
    if any(not rows for rows in loaded):
        raise RuntimeError("range-compression replay has an empty market")
    reference = loaded[0]
    split_index = int(len(reference) * config.validation.selection_fraction)
    split_ts = reference[split_index].ts
    selection_end = split_ts - timedelta(minutes=config.validation.split_embargo_minutes)
    selection_results = tuple(
        simulate_symbol_window(
            symbol,
            rows,
            config,
            score_end=selection_end,
            process_end=split_ts,
        )
        for symbol, rows in zip(config.data.symbols, loaded, strict=True)
    )
    selection_metrics = replay_metrics(selection_results)
    gate = selection_gate(selection_results, config)
    untouched: dict[str, object] = {
        "status": "sealed",
        "predictions_computed": False,
        "trades_computed": False,
        "opened_once": False,
    }
    if gate["passed"]:
        tail_results = tuple(
            simulate_symbol_window(symbol, rows, config, score_start=split_ts)
            for symbol, rows in zip(config.data.symbols, loaded, strict=True)
        )
        untouched = {
            "status": "opened_after_selection_pass",
            "predictions_computed": True,
            "trades_computed": True,
            "opened_once": True,
            "metrics": replay_metrics(tail_results),
            "trades": [
                trade.to_dict()
                for result in tail_results
                for trade in result.trades
            ],
        }
    payload: dict[str, object] = {
        "report_id": "range_compression_breakout_v1_first_replay",
        "generated_at": datetime.now(UTC).isoformat(),
        "contract": str(config_path),
        "contract_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "split": {
            "selection_fraction": config.validation.selection_fraction,
            "untouched_fraction": config.validation.untouched_fraction,
            "boundary": split_ts.isoformat(),
            "selection_decision_end": selection_end.isoformat(),
            "embargo_minutes": config.validation.split_embargo_minutes,
        },
        "selection": {
            "metrics": selection_metrics,
            "gate": gate,
            "trades": [
                trade.to_dict()
                for result in selection_results
                for trade in result.trades
            ],
        },
        "untouched": untouched,
        "policy": {
            "research_only": True,
            "can_trade": False,
            "can_promote": False,
            "next_bar_entry": True,
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
    selection = payload["selection"]
    assert isinstance(selection, dict)
    print(json.dumps({"selection": selection["metrics"], "gate": selection["gate"]}, indent=2))


if __name__ == "__main__":
    main()
