"""First causal replay for preregistered session_liquidity_sweep_v1."""

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
from vnedge.scalping.delta_engine.candle_store import MultiTimeframeCandleStore
from vnedge.scalping.delta_engine.session_sweep import (
    SessionLiquiditySweepScanner,
    SessionSweepConfig,
    SessionSweepContext,
    SessionSweepSetup,
    finalize_next_open,
    load_session_sweep_config,
    session_sweep_fee_model,
)
from vnedge.scalping.delta_engine.types import Candle

DEFAULT_CONFIG = Path("configs/research/session_liquidity_sweep_v1.yaml")
DEFAULT_CACHE = Path("data/delta_scalper_cache")
DEFAULT_OUTPUT = Path(
    "research/live_research/session_liquidity_sweep_v1_latest.json"
)


@dataclass(frozen=True)
class SweepReplayResult:
    symbol: str
    trades: tuple[BacktestTrade, ...]
    setup_records: tuple[dict[str, object], ...]
    missing_minutes: int
    unresolved_dropped: int
    source_bars: int
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
    config: SessionSweepConfig,
    *,
    score_start: datetime | None = None,
    score_end: datetime | None = None,
    process_end: datetime | None = None,
) -> SweepReplayResult:
    native = symbol.upper()
    scanner = SessionLiquiditySweepScanner(config)
    fee_model = session_sweep_fee_model(config)
    resolver = CausalScalperBacktester(
        None,  # type: ignore[arg-type] -- canonical exit resolver only
        fee_model,
        MultiTimeframeCandleStore(),
    )
    history: deque[Candle] = deque(maxlen=max(scanner.minimum_history, 160))
    pending: SessionSweepSetup | None = None
    active: OpenBacktestTrade | None = None
    trades: list[BacktestTrade] = []
    setup_records: list[dict[str, object]] = []
    previous_ts: datetime | None = None
    missing = 0
    dropped = 0
    processed = 0
    scored: list[datetime] = []
    for bar in sorted(rows, key=lambda item: item.ts):
        if bar.tf != "1m":
            raise ValueError("session-sweep replay accepts 1m candles only")
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
                history.clear()
                scanner.reset()
        previous_ts = bar.ts
        processed += 1
        if pending is not None and active is None:
            geometry = finalize_next_open(pending, bar, config, fee_model)
            setup_records.append({**pending.to_dict(), "entry_geometry": geometry.to_dict()})
            if geometry.candidate is not None:
                active = OpenBacktestTrade(geometry.candidate, bar)
            pending = None
        if active is not None:
            resolved = resolver.resolve_on_bar(active, bar)
            if resolved is not None:
                trades.append(resolved)
                active = None
        history.append(bar)
        scoring = (score_start is None or bar.ts >= score_start) and (
            score_end is None or bar.ts <= score_end
        )
        if scoring:
            scored.append(bar.ts)
        setup = scanner.evaluate(
            SessionSweepContext(native, bar.ts, tuple(history)),
            allow_signal=scoring and pending is None and active is None,
        )
        if setup is not None:
            pending = setup
    dropped += int(pending is not None) + int(active is not None)
    return SweepReplayResult(
        symbol=native,
        trades=tuple(trades),
        setup_records=tuple(setup_records),
        missing_minutes=missing,
        unresolved_dropped=dropped,
        source_bars=processed,
        score_started_at=scored[0] if scored else None,
        score_ended_at=scored[-1] if scored else None,
    )


def _profit_factor(trades: tuple[BacktestTrade, ...]) -> float:
    gains = sum(row.net_bps for row in trades if row.net_bps > 0)
    losses = -sum(row.net_bps for row in trades if row.net_bps < 0)
    return gains / losses if losses else (float("inf") if gains else 0.0)


def replay_metrics(results: tuple[SweepReplayResult, ...]) -> dict[str, object]:
    trades = tuple(
        sorted(
            (trade for result in results for trade in result.trades),
            key=lambda row: row.decision_ts,
        )
    )
    records = [record for result in results for record in result.setup_records]
    starts = [result.score_started_at for result in results if result.score_started_at]
    ends = [result.score_ended_at for result in results if result.score_ended_at]
    days = max(1.0, (max(ends) - min(starts)).total_seconds() / 86_400) if starts and ends else 1.0
    net = [row.net_bps for row in trades]
    gross = [row.gross_bps for row in trades]
    costs = [row.cost_bps for row in trades]
    market_rows: dict[str, dict[str, object]] = {}
    for result in results:
        market_net = [row.net_bps for row in result.trades]
        market_rows[result.symbol] = {
            "setups": len(result.setup_records),
            "entry_rejections": sum(
                record["entry_geometry"]["status"] == "rejected"
                for record in result.setup_records
            ),
            "trades": len(result.trades),
            "net_bps": sum(market_net),
            "average_net_bps": fmean(market_net) if market_net else 0.0,
            "profit_factor": _profit_factor(result.trades),
            "missing_minutes": result.missing_minutes,
            "unresolved_dropped": result.unresolved_dropped,
        }
    maximum_market_fraction = (
        max((len(result.trades) for result in results), default=0) / len(trades)
        if trades
        else 0.0
    )
    return {
        "setups": len(records),
        "entry_rejections": sum(
            record["entry_geometry"]["status"] == "rejected" for record in records
        ),
        "entry_rejection_reasons": dict(
            Counter(
                record["entry_geometry"]["reason"]
                for record in records
                if record["entry_geometry"]["status"] == "rejected"
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
        "exit_reasons": dict(Counter(row.exit_reason for row in trades)),
        "sessions": dict(Counter(record["session"] for record in records)),
        "sides": dict(Counter(row.side.value for row in trades)),
        "maximum_single_market_trade_fraction": maximum_market_fraction,
        "positive_markets": sum(
            float(row["net_bps"]) > 0 for row in market_rows.values()
        ),
        "missing_minutes": sum(result.missing_minutes for result in results),
        "unresolved_dropped": sum(result.unresolved_dropped for result in results),
        "data_quality_pass": all(
            result.missing_minutes == 0 and result.unresolved_dropped == 0
            for result in results
        ),
        "markets": market_rows,
        "score_started_at": min(starts).isoformat() if starts else None,
        "score_ended_at": max(ends).isoformat() if ends else None,
    }


def selection_gate(
    results: tuple[SweepReplayResult, ...], config: SessionSweepConfig
) -> dict[str, object]:
    metrics = replay_metrics(results)
    validation = config.validation
    checks = {
        "data_quality": bool(metrics["data_quality_pass"]),
        "minimum_trades": int(metrics["trades"]) >= validation.minimum_selection_trades,
        "gross_clears_cost": (
            float(metrics["average_gross_bps"]) > float(metrics["average_cost_bps"])
        ),
        "positive_net": float(metrics["average_net_bps"]) > 0,
        "profit_factor": (
            float(metrics["profit_factor"]) >= validation.minimum_selection_profit_factor
        ),
        "positive_markets": (
            int(metrics["positive_markets"]) >= validation.minimum_positive_markets
        ),
        "false_signal_rate": (
            float(metrics["false_signal_rate"]) < validation.maximum_false_signal_rate
        ),
        "market_concentration": (
            float(metrics["maximum_single_market_trade_fraction"])
            <= validation.maximum_single_market_trade_fraction
        ),
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "thresholds": validation.model_dump(),
    }


async def run(args: argparse.Namespace) -> dict[str, object]:
    config_path = Path(args.config)
    config = load_session_sweep_config(config_path)
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
    metrics = replay_metrics(selection_results)
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
        tail_metrics = replay_metrics(tail_results)
        untouched = {
            "status": "opened_after_selection_pass",
            "predictions_computed": True,
            "trades_computed": True,
            "opened_once": True,
            "metrics": tail_metrics,
            "promotion_evidence": {
                "average_net": (
                    float(tail_metrics["average_net_bps"])
                    > config.validation.untouched_minimum_average_net_bps
                ),
                "profit_factor": (
                    float(tail_metrics["profit_factor"])
                    >= config.validation.untouched_minimum_profit_factor
                ),
                "both_markets_positive": int(tail_metrics["positive_markets"]) == 2,
                "human_single_market_exception_allowed_but_not_granted": True,
            },
            "trades": [
                trade.to_dict() for result in tail_results for trade in result.trades
            ],
            "setup_records": [
                record for result in tail_results for record in result.setup_records
            ],
        }
    payload: dict[str, object] = {
        "report_id": "session_liquidity_sweep_v1_first_replay",
        "generated_at": datetime.now(UTC).isoformat(),
        "contract": str(config_path),
        "contract_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "split": {
            "boundary": split_ts.isoformat(),
            "selection_decision_end": selection_end.isoformat(),
            "selection_fraction": config.validation.selection_fraction,
            "untouched_fraction": config.validation.untouched_fraction,
            "embargo_minutes": config.validation.split_embargo_minutes,
        },
        "selection": {
            "metrics": metrics,
            "gate": gate,
            "trades": [
                trade.to_dict()
                for result in selection_results
                for trade in result.trades
            ],
            "setup_records": [
                record
                for result in selection_results
                for record in result.setup_records
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
            "regime_used": False,
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
