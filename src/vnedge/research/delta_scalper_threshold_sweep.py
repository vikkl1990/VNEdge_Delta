"""Preregistered, causal gate sweep for the Delta scalper research engine.

All variants share one closed-candle candidate ledger. Each variant then owns
an independent next-open, one-position-at-a-time simulation using the canonical
backtester exit resolver. Configurations are ranked on the selection window
only. The frozen tail is opened once, and only if the selected configuration
passes the preregistered selection gates.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from statistics import fmean
from tempfile import NamedTemporaryFile
from typing import Protocol

import numpy as np

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
from vnedge.scalping.delta_engine.config import DeltaScalperConfig, load_delta_scalper_config
from vnedge.scalping.delta_engine.factory import build_delta_scalper_assembly
from vnedge.scalping.delta_engine.types import Candle, SignalCandidate
from vnedge.scalping.delta_engine.validation import robust_validation_report

DEFAULT_CONFIG = Path("configs/delta_scalper.yaml")
DEFAULT_BACKTEST = Path("research/live_research/delta_scalper_backtest_latest.json")
DEFAULT_CACHE = Path("data/delta_scalper_cache")
DEFAULT_OUTPUT = Path("research/live_research/delta_scalper_threshold_sweep_latest.json")


@dataclass(frozen=True)
class GateVariant:
    config_id: str
    family: str
    value: float
    min_probability: float
    min_confidence: float
    min_expectancy_bps: float
    min_expected_move_bps: float = 0.0
    min_net_fee_multiple: float = 0.0

    def accepts(self, candidate: SignalCandidate) -> bool:
        return (
            candidate.scalper_probability >= self.min_probability
            and candidate.confidence >= self.min_confidence
            and candidate.fee_adjusted_expectancy_bps >= self.min_expectancy_bps
            and candidate.expected_move_bps >= self.min_expected_move_bps
            and candidate.fee_adjusted_expectancy_bps
            >= self.min_net_fee_multiple * candidate.modeled_cost_bps
        )

    def to_dict(self) -> dict[str, object]:
        return self.__dict__.copy()


class CandidateVariant(Protocol):
    config_id: str

    def accepts(self, candidate: SignalCandidate) -> bool: ...

    def to_dict(self) -> dict[str, object]: ...


@dataclass(frozen=True)
class CandidateLedger:
    symbol: str
    candidates_by_close: dict[datetime, tuple[SignalCandidate, ...]]
    missing_one_minute_bars: int


@dataclass(frozen=True)
class VariantSimulation:
    symbol: str
    trades: tuple[BacktestTrade, ...]
    gap_unresolved_dropped: int
    window_end_censored: int
    missing_one_minute_bars: int
    started_at: datetime | None
    ended_at: datetime | None


def preregistered_variants(config: DeltaScalperConfig) -> tuple[GateVariant, ...]:
    base = config.engine
    variants = [
        GateVariant(
            "baseline",
            "baseline",
            0.0,
            base.min_probability,
            base.min_confidence,
            base.min_expectancy_bps,
        )
    ]
    for value in (0.74, 0.78, 0.82, 0.86):
        variants.append(
            GateVariant(
                f"probability_{value:.2f}",
                "probability",
                value,
                value,
                base.min_confidence,
                base.min_expectancy_bps,
            )
        )
    for value in (0.68, 0.76, 0.84, 0.90):
        variants.append(
            GateVariant(
                f"confidence_{value:.2f}",
                "confidence",
                value,
                base.min_probability,
                value,
                base.min_expectancy_bps,
            )
        )
    for value in (12.0, 16.0, 20.0, 24.0):
        variants.append(
            GateVariant(
                f"expectancy_{value:.0f}bps",
                "minimum_expectancy_bps",
                value,
                base.min_probability,
                base.min_confidence,
                value,
            )
        )
    for value in (12.0, 18.0, 24.0, 30.0):
        variants.append(
            GateVariant(
                f"move_{value:.0f}bps",
                "minimum_expected_move_bps",
                value,
                base.min_probability,
                base.min_confidence,
                base.min_expectancy_bps,
                min_expected_move_bps=value,
            )
        )
    for value in (1.0, 2.0, 3.0):
        variants.append(
            GateVariant(
                f"net_fee_multiple_{value:.0f}x",
                "minimum_net_fee_multiple",
                value,
                base.min_probability,
                base.min_confidence,
                base.min_expectancy_bps,
                min_net_fee_multiple=value,
            )
        )
    return tuple(variants)


def build_candidate_ledger(
    symbol: str,
    rows: list[Candle],
    config: DeltaScalperConfig,
    *,
    scalper_opted_in: bool,
    deto_enabled: bool,
) -> tuple[CandidateLedger, CausalScalperBacktester]:
    native = symbol.upper()
    store = MultiTimeframeCandleStore(
        max_bars_per_timeframe=config.features.max_bars_per_timeframe
    )
    assembly = build_delta_scalper_assembly(
        store,
        config,
        scalper_opted_in=scalper_opted_in,
        deto_enabled=deto_enabled,
    )
    resolver = CausalScalperBacktester(assembly.generator, assembly.fee_model, store)
    aggregator = ClosedCandleAggregator()
    previous: datetime | None = None
    missing = 0
    ledger: dict[datetime, tuple[SignalCandidate, ...]] = {}
    for bar in sorted(rows, key=lambda item: item.ts):
        if bar.tf != "1m":
            raise ValueError("candidate ledger accepts closed 1m candles only")
        if previous is not None and bar.ts <= previous:
            raise ValueError("1m candles must have unique ascending timestamps")
        if previous is not None and bar.ts - previous != timedelta(minutes=1):
            missing += max(1, int((bar.ts - previous).total_seconds() // 60) - 1)
            store.reset_symbol(native)
            aggregator = ClosedCandleAggregator()
        previous = bar.ts
        store.append_closed(native, bar, observed_at=bar.ts)
        for higher in aggregator.on_one_minute(native, bar):
            store.append_closed(native, higher, observed_at=bar.ts)
        decision = assembly.generator.on_candle_closed(native, "1m", now=bar.ts)
        ledger[bar.ts] = decision.evaluated
    return CandidateLedger(native, ledger, missing), resolver


def simulate_variant(
    rows: list[Candle],
    ledger: CandidateLedger,
    resolver: CausalScalperBacktester,
    variant: CandidateVariant,
) -> VariantSimulation:
    ordered = sorted(rows, key=lambda item: item.ts)
    pending: SignalCandidate | None = None
    open_trade: OpenBacktestTrade | None = None
    trades: list[BacktestTrade] = []
    previous: datetime | None = None
    missing = 0
    gap_unresolved = 0
    for bar in ordered:
        if previous is not None and bar.ts - previous != timedelta(minutes=1):
            missing += max(1, int((bar.ts - previous).total_seconds() // 60) - 1)
            gap_unresolved += int(open_trade is not None)
            pending = None
            open_trade = None
        previous = bar.ts
        if pending is not None and open_trade is None:
            open_trade = OpenBacktestTrade(pending, bar)
            pending = None
        if open_trade is not None:
            outcome = resolver.resolve_on_bar(open_trade, bar)
            if outcome is not None:
                trades.append(outcome)
                open_trade = None
        if pending is None and open_trade is None:
            accepted = tuple(
                candidate
                for candidate in ledger.candidates_by_close.get(bar.ts, ())
                if variant.accepts(candidate)
            )
            if accepted:
                pending = max(accepted, key=lambda candidate: candidate.rank_score)
    window_end_censored = int(open_trade is not None or pending is not None)
    return VariantSimulation(
        ledger.symbol,
        tuple(trades),
        gap_unresolved,
        window_end_censored,
        missing,
        ordered[0].ts if ordered else None,
        ordered[-1].ts if ordered else None,
    )


def _profit_factor(trades: list[BacktestTrade]) -> float | None:
    wins = sum(trade.net_bps for trade in trades if trade.net_bps > 0)
    losses = sum(-trade.net_bps for trade in trades if trade.net_bps < 0)
    return wins / losses if losses else None


def _max_drawdown(trades: list[BacktestTrade]) -> float:
    equity = peak = drawdown = 0.0
    for trade in sorted(trades, key=lambda item: item.exit_ts):
        equity += trade.net_bps
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return drawdown


def summarize_simulations(simulations: list[VariantSimulation]) -> dict:
    trades = sorted(
        [trade for simulation in simulations for trade in simulation.trades],
        key=lambda item: item.exit_ts,
    )
    market_breakdown = {}
    frequencies = []
    for simulation in simulations:
        market_trades = list(simulation.trades)
        days = (
            max(
                1.0,
                (simulation.ended_at - simulation.started_at).total_seconds() / 86_400,
            )
            if simulation.started_at and simulation.ended_at
            else 1.0
        )
        frequencies.append(len(market_trades) / days)
        market_breakdown[simulation.symbol] = {
            "trades": len(market_trades),
            "net_bps": sum(trade.net_bps for trade in market_trades),
            "average_net_bps": (
                fmean(trade.net_bps for trade in market_trades) if market_trades else 0.0
            ),
            "profit_factor": _profit_factor(market_trades),
            "false_signal_rate": (
                sum(trade.net_bps <= 0 for trade in market_trades) / len(market_trades)
                if market_trades
                else 0.0
            ),
            "trades_per_day": len(market_trades) / days,
        }
    return {
        "trades": len(trades),
        "net_bps": sum(trade.net_bps for trade in trades),
        "average_net_bps": fmean(trade.net_bps for trade in trades) if trades else 0.0,
        "profit_factor": _profit_factor(trades),
        "false_signal_rate": (
            sum(trade.net_bps <= 0 for trade in trades) / len(trades) if trades else 0.0
        ),
        "max_drawdown_bps": _max_drawdown(trades),
        "trades_per_day": sum(frequencies),
        "positive_markets": sum(row["net_bps"] > 0 for row in market_breakdown.values()),
        "market_breakdown": market_breakdown,
        "missing_one_minute_bars": sum(
            simulation.missing_one_minute_bars for simulation in simulations
        ),
        "gap_unresolved_trades_dropped": sum(
            simulation.gap_unresolved_dropped for simulation in simulations
        ),
        "window_end_trades_censored": sum(
            simulation.window_end_censored for simulation in simulations
        ),
        "data_quality_pass": all(
            simulation.missing_one_minute_bars == 0
            and simulation.gap_unresolved_dropped == 0
            for simulation in simulations
        ),
    }


def _daily_returns(simulations: list[VariantSimulation], days: list[date]) -> list[float]:
    by_day: dict[date, float] = defaultdict(float)
    for simulation in simulations:
        for trade in simulation.trades:
            by_day[trade.exit_ts.date()] += trade.net_bps / 10_000.0
    return [by_day[day] for day in days]


def _frozen_boundary(backtest: dict) -> datetime:
    trades = sorted(
        [
            trade
            for market in (backtest.get("markets") or {}).values()
            if isinstance(market, dict)
            for trade in (market.get("trades") or [])
            if isinstance(trade, dict)
        ],
        key=lambda row: str(row.get("exit_ts") or ""),
    )
    if not trades:
        raise ValueError("baseline backtest has no trades")
    fraction = float(
        (backtest.get("untouched_window") or {}).get("untouched_fraction") or 0.20
    )
    split = min(len(trades) - 1, max(1, int(len(trades) * (1 - fraction))))
    return datetime.fromisoformat(str(trades[split]["exit_ts"]))


def _selection_pass(summary: dict, baseline: dict) -> tuple[bool, list[str]]:
    reasons = []
    baseline_trades = max(1, int(baseline["trades"]))
    reduction = 1 - int(summary["trades"]) / baseline_trades
    if not 0.60 <= reduction <= 0.80:
        reasons.append("frequency_reduction_outside_60_80_pct")
    if int(summary["trades"]) < 500:
        reasons.append("fewer_than_500_selection_trades")
    if summary["profit_factor"] is None or float(summary["profit_factor"]) < 1.10:
        reasons.append("selection_profit_factor_below_1_10")
    if float(summary["average_net_bps"]) < 1.0:
        reasons.append("selection_average_net_below_1_bps")
    if int(summary["positive_markets"]) < 1:
        reasons.append("no_positive_selection_market")
    return not reasons, reasons


def _diagnostic_rank(row: dict) -> tuple[float, float, float, int]:
    summary = row["selection"]
    pf = float(summary["profit_factor"] or 0.0)
    return (
        float(row["selection_gate_pass"]),
        pf,
        float(summary["average_net_bps"]),
        int(summary["trades"]),
    )


async def run_experiment(
    args: argparse.Namespace,
    *,
    variant_factory: Callable[
        [DeltaScalperConfig], tuple[CandidateVariant, ...]
    ],
    report_id: str,
    experiment_constraints: dict[str, object],
) -> dict:
    config = load_delta_scalper_config(args.config)
    baseline_backtest = json.loads(args.backtest.read_text(encoding="utf-8"))
    window = baseline_backtest.get("window") or {}
    start = datetime.fromisoformat(args.start or str(window.get("start")))
    end = datetime.fromisoformat(args.end or str(window.get("end")))
    boundary = _frozen_boundary(baseline_backtest)
    symbols = tuple(args.symbols.split(",")) if args.symbols else config.engine.symbols
    variants = variant_factory(config)
    rows_by_symbol: dict[str, list[Candle]] = {}
    ledgers: dict[str, CandidateLedger] = {}
    resolvers: dict[str, CausalScalperBacktester] = {}
    for symbol in symbols:
        native = symbol.strip().upper()
        rows = await _load_candles(
            native,
            start,
            end,
            cache_dir=args.cache_dir,
            refresh=args.refresh,
        )
        ledger, resolver = build_candidate_ledger(
            native,
            rows,
            config,
            scalper_opted_in=args.scalper_opted_in,
            deto_enabled=args.deto,
        )
        rows_by_symbol[native] = rows
        ledgers[native] = ledger
        resolvers[native] = resolver
    variant_rows = []
    simulations_by_variant: dict[str, list[VariantSimulation]] = {}
    for variant in variants:
        simulations = [
            simulate_variant(
                [bar for bar in rows_by_symbol[symbol] if bar.ts < boundary],
                ledgers[symbol],
                resolvers[symbol],
                variant,
            )
            for symbol in rows_by_symbol
        ]
        simulations_by_variant[variant.config_id] = simulations
        variant_rows.append({"config": variant.to_dict(), "selection": summarize_simulations(simulations)})
    baseline_summary = next(
        row["selection"] for row in variant_rows if row["config"]["config_id"] == "baseline"
    )
    for row in variant_rows:
        summary = row["selection"]
        summary["frequency_reduction_vs_baseline"] = 1 - int(summary["trades"]) / max(
            1, int(baseline_summary["trades"])
        )
        passed, reasons = _selection_pass(summary, baseline_summary)
        row["selection_gate_pass"] = passed
        row["selection_gate_reasons"] = reasons
    diagnostic = max(variant_rows, key=_diagnostic_rank)
    selected = diagnostic if diagnostic["selection_gate_pass"] else None
    selection_start = min(rows[0].ts for rows in rows_by_symbol.values() if rows)
    day_count = max(1, (boundary.date() - selection_start.date()).days + 1)
    days = [selection_start.date() + timedelta(days=index) for index in range(day_count)]
    matrix = np.column_stack(
        [
            _daily_returns(simulations_by_variant[variant.config_id], days)
            for variant in variants
        ]
    )
    diagnostic_index = next(
        index
        for index, variant in enumerate(variants)
        if variant.config_id == diagnostic["config"]["config_id"]
    )
    robustness = robust_validation_report(
        matrix,
        selected_config=diagnostic_index,
        label_horizon=1,
    )
    frozen_result: dict[str, object]
    if selected is None:
        frozen_result = {
            "status": "not_run_selection_gate_failed",
            "window_consumed": False,
            "reason": "no preregistered variant cleared the selection gates",
        }
    else:
        variant = next(
            item
            for item in variants
            if item.config_id == selected["config"]["config_id"]
        )
        frozen_simulations = [
            simulate_variant(
                [bar for bar in rows_by_symbol[symbol] if bar.ts >= boundary],
                ledgers[symbol],
                resolvers[symbol],
                variant,
            )
            for symbol in rows_by_symbol
        ]
        summary = summarize_simulations(frozen_simulations)
        frozen_result = {
            "status": "evaluated_once",
            "window_consumed": True,
            "config_id": variant.config_id,
            "summary": summary,
            "success_gates": {
                "profit_factor_above_1_30": (
                    summary["profit_factor"] is not None
                    and float(summary["profit_factor"]) > 1.30
                ),
                "average_net_above_3_bps": float(summary["average_net_bps"]) > 3.0,
                "positive_markets_at_least_one": int(summary["positive_markets"]) >= 1,
            },
        }
    payload = {
        "report_id": report_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "preregistered_before_results": True,
        "candidate_ledger_shared_across_variants": True,
        "variant_count": len(variants),
        "selection_window": {
            "start": start.isoformat(),
            "end_exclusive": boundary.isoformat(),
        },
        "frozen_window": {
            "start": boundary.isoformat(),
            "end": end.isoformat(),
            **frozen_result,
        },
        "baseline_selection": baseline_summary,
        "evaluated_variants": sorted(variant_rows, key=_diagnostic_rank, reverse=True),
        "best_diagnostic_config": diagnostic,
        "selected_config": selected,
        "robust_validation": robustness.to_dict(),
        "research_constraints": {
            "realistic_fees": True,
            "next_open_entries": True,
            "stop_first_ambiguity": True,
            "l2_hard_gate_tested": False,
            "l2_reason": "historical candle data cannot reconstruct causal L2 confirmation",
            **experiment_constraints,
            "can_trade": False,
            "can_promote": False,
        },
        "can_trade": False,
        "can_promote": False,
    }
    _atomic_json(args.output, payload)
    return payload


async def run(args: argparse.Namespace) -> dict:
    return await run_experiment(
        args,
        variant_factory=preregistered_variants,
        report_id="delta_scalper_threshold_sweep_v1",
        experiment_constraints={"single_parameter_family_per_variant": True},
    )


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--backtest", type=Path, default=DEFAULT_BACKTEST)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--symbols")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--scalper-opted-in", action="store_true")
    parser.add_argument("--deto", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    return parser


def main() -> None:
    payload = asyncio.run(run(_parser().parse_args()))
    print(
        json.dumps(
            {
                "best_diagnostic_config": payload["best_diagnostic_config"],
                "selected_config": payload["selected_config"],
                "frozen_window": payload["frozen_window"],
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
