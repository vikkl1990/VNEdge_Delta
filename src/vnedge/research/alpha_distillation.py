"""Alpha distillation research runner.

The runner backtests the Alpha Distillation Pack over 4h/1h/15m/1m lanes. It
turns the 35 public indicator concepts into auditable feature atoms, then asks
the normal VNEDGE walk-forward machinery whether any atom/route survives fees.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

import pandas as pd

from vnedge.backtest.backtester import BacktestConfig
from vnedge.backtest.fee_model import FeeModel
from vnedge.backtest.metrics import BacktestMetrics
from vnedge.backtest.slippage_model import SlippageModel
from vnedge.backtest.walk_forward import PromotionGates, WalkForwardResult, param_grid, walk_forward
from vnedge.data.parquet_store import ParquetStore
from vnedge.research.edge_leaderboard import build_edge_leaderboard
from vnedge.research.universe import DEFAULT_EXCHANGES, DEFAULT_SYMBOLS, load_research_targets
from vnedge.scalping.parameter_registry import DEFAULT_SCALPER_PARAMETER_REGISTRY
from vnedge.strategy.alpha_distillation_pack import (
    AlphaDistillationPack,
    FEATURE_ATOMS,
    concept_coverage,
    concept_inventory,
)


BASE_TIMEFRAME = "15m"
CONTEXT_TIMEFRAMES = ("4h", "1h")
TRIGGER_TIMEFRAME = "1m"
ALPHA_DISTILLATION_ID = "alpha_distillation_pack_v1"
_ENTRY_RE = re.compile(
    r"\balpha_distillation_pack\s+(?P<side>long|short)\s+"
    r"(?P<atom>[a-z_]+)\s+route=(?P<route>[A-Z_]+)"
)

ALPHA_DISTILLATION_GATES = PromotionGates(
    min_splits=3,
    min_total_oos_trades=18,
    min_profit_factor=1.30,
    max_window_drawdown_pct=8.0,
    reject_zero_trade_windows=False,
    min_windows_with_trades_pct=60.0,
    min_payoff_ratio=1.25,
    max_single_trade_profit_share=0.35,
)


@dataclass(frozen=True)
class AlphaDistillationCandidate:
    exchange: str
    symbol: str
    allowed_atoms: tuple[str, ...] = ()
    allowed_sides: tuple[str, ...] = ("long", "short")

    @property
    def candidate_id(self) -> str:
        safe_symbol = (
            self.symbol.replace("/", "")
            .replace(":", "")
            .replace("-", "")
            .replace("_", "")
            .upper()
        )
        atom = "all_atoms" if not self.allowed_atoms else "_".join(self.allowed_atoms)
        return f"{ALPHA_DISTILLATION_ID}__{self.exchange}__{safe_symbol}__{atom}"


def default_candidates() -> tuple[AlphaDistillationCandidate, ...]:
    """Default all-exchange lane set, bounded to the project's core liquid perps."""
    out: list[AlphaDistillationCandidate] = []
    for target in load_research_targets(
        exchanges=DEFAULT_EXCHANGES,
        symbols=DEFAULT_SYMBOLS,
        timeframe=BASE_TIMEFRAME,
    ):
        out.append(AlphaDistillationCandidate(target.exchange, target.symbol))
    return tuple(out)


def alpha_distillation_policy() -> dict:
    return {
        "strategy": ALPHA_DISTILLATION_ID,
        "research_only": True,
        "can_trade": False,
        "can_promote": False,
        "requires_untouched_judgment": True,
        "requires_human_approval": True,
        "concept_count": len(concept_inventory()),
        "feature_atoms": list(FEATURE_ATOMS),
        "route_policy": (
            "maker-first by default; taker only when expected bps clears the "
            "taker fee wall, then still needs walk-forward and human approval"
        ),
        "copying_policy": (
            "public indicators are distilled into causal atoms; no proprietary "
            "Pine/TradingView logic is copied"
        ),
    }


def run_alpha_distillation_research(
    data_root: Path | str,
    *,
    candidates: Iterable[AlphaDistillationCandidate] | None = None,
    max_candidates: int | None = None,
    lookback_days: int = 120,
    train_days: int = 30,
    test_days: int = 7,
    require_context: bool = True,
    require_1m_trigger: bool = True,
) -> dict:
    store = ParquetStore(data_root)
    candidate_list = tuple(candidates or default_candidates())
    if max_candidates is not None:
        candidate_list = candidate_list[:max_candidates]
    records = [
        run_candidate(
            store,
            candidate,
            lookback_days=lookback_days,
            train_days=train_days,
            test_days=test_days,
            require_context=require_context,
            require_1m_trigger=require_1m_trigger,
        )
        for candidate in candidate_list
    ]
    return build_alpha_distillation_report(
        records,
        candidates=candidate_list,
        lookback_days=lookback_days,
        train_days=train_days,
        test_days=test_days,
        require_context=require_context,
        require_1m_trigger=require_1m_trigger,
    )


def run_candidate(
    store: ParquetStore,
    candidate: AlphaDistillationCandidate,
    *,
    lookback_days: int,
    train_days: int,
    test_days: int,
    require_context: bool,
    require_1m_trigger: bool,
) -> dict:
    try:
        candles_15m = store.read_candles(candidate.exchange, candidate.symbol, BASE_TIMEFRAME)
        context_1h = store.read_candles(candidate.exchange, candidate.symbol, "1h")
        context_4h = store.read_candles(candidate.exchange, candidate.symbol, "4h")
        trigger_1m = store.read_candles(candidate.exchange, candidate.symbol, "1m")
    except FileNotFoundError as exc:
        return _untestable_record(candidate, f"missing data lane: {exc}")

    candles_15m = _window(candles_15m, lookback_days=lookback_days)
    if candles_15m.empty:
        return _untestable_record(candidate, "15m base lane has no rows in lookback window")

    latest = candles_15m["timestamp"].iloc[-1]
    context_cutoff = latest - pd.Timedelta(days=lookback_days + 45)
    trigger_cutoff = latest - pd.Timedelta(days=lookback_days + 2)
    context_1h = context_1h[context_1h["timestamp"] >= context_cutoff].reset_index(drop=True)
    context_4h = context_4h[context_4h["timestamp"] >= context_cutoff].reset_index(drop=True)
    trigger_1m = trigger_1m[trigger_1m["timestamp"] >= trigger_cutoff].reset_index(drop=True)

    train_bars = train_days * 24 * 4
    test_bars = test_days * 24 * 4
    if len(candles_15m) < train_bars + test_bars:
        return _untestable_record(
            candidate,
            f"only {len(candles_15m)} 15m bars; need >= {train_bars + test_bars}",
        )

    fee = DEFAULT_SCALPER_PARAMETER_REGISTRY.fee_profile(candidate.exchange)
    config = BacktestConfig(
        max_holding_bars=16,
        fees=FeeModel(maker_bps=fee.maker_bps, taker_bps=fee.taker_bps),
        slippage=SlippageModel(bps=fee.slippage_bps),
    )
    funding = _empty_funding_frame()
    # First-pass mining should answer "is there a fee-clearing atom here?"
    # quickly. Keep stop/target stable; the true selection axes are score
    # strictness and maker-vs-taker fee-wall hurdle.
    grid = param_grid(
        min_score=[8.0, 9.0],
        min_score_delta=[1.25],
        min_edge_bps=[fee.maker_first_cost_bps, fee.taker_round_trip_cost_bps],
        stop_atr_mult=[1.0],
        take_profit_r=[1.5],
    )

    def factory(**params):
        return AlphaDistillationPack(
            funding=funding,
            context_1h=context_1h,
            context_4h=context_4h,
            trigger_1m=trigger_1m,
            require_context=require_context,
            require_1m_trigger=require_1m_trigger,
            allowed_atoms=candidate.allowed_atoms,
            allowed_sides=candidate.allowed_sides,
            **params,
        )

    try:
        result = walk_forward(
            candles_15m,
            funding,
            factory,
            grid,
            config,
            train_bars=train_bars,
            test_bars=test_bars,
            symbol=candidate.symbol,
            timeframe=BASE_TIMEFRAME,
        )
    except ValueError as exc:
        return _untestable_record(candidate, str(exc))

    record = _wf_record(candidate, result)
    record.update(
        {
            "candidate_id": candidate.candidate_id,
            "allowed_atoms": list(candidate.allowed_atoms),
            "allowed_sides": list(candidate.allowed_sides),
            "base_timeframe": BASE_TIMEFRAME,
            "context_timeframes": list(CONTEXT_TIMEFRAMES),
            "trigger_timeframe": TRIGGER_TIMEFRAME,
            "route_policy": "maker-first; taker only after fee-wall proof",
            "fee_profile": fee.to_dict(),
            "can_trade": False,
            "can_promote": False,
            "requires_human_approval": True,
            "requires_untouched_judgment": True,
        }
    )
    return record


def build_alpha_distillation_report(
    records: list[dict],
    *,
    candidates: tuple[AlphaDistillationCandidate, ...],
    lookback_days: int,
    train_days: int,
    test_days: int,
    require_context: bool,
    require_1m_trigger: bool,
) -> dict:
    leaderboard = build_edge_leaderboard(records)
    return {
        "updated": datetime.now(UTC).isoformat(),
        "policy": alpha_distillation_policy(),
        "flow": [
            "inventory_35_public_indicator_concepts",
            "distill_to_causal_feature_atoms",
            "load_4h_1h_15m_1m_lanes",
            "score_context_trigger_route_exit",
            "walk_forward_after_realistic_fees",
            "attribute_edge_by_atom_and_route",
            "pre_register_untouched_judgment_if_candidate",
        ],
        "parameters": {
            "lookback_days": lookback_days,
            "train_days": train_days,
            "test_days": test_days,
            "base_timeframe": BASE_TIMEFRAME,
            "context_timeframes": list(CONTEXT_TIMEFRAMES),
            "trigger_timeframe": TRIGGER_TIMEFRAME,
            "require_context": require_context,
            "require_1m_trigger": require_1m_trigger,
            "max_holding_bars_15m": 16,
            "gates": asdict(ALPHA_DISTILLATION_GATES),
        },
        "concept_inventory": concept_inventory(),
        "concept_coverage": concept_coverage(),
        "candidates": [asdict(candidate) for candidate in candidates],
        "summary": _summary(records),
        "results": records,
        "edge_leaderboard": leaderboard,
        "note": (
            "Research verdict only. A PASS means candidate for untouched judgment; "
            "it does not enable paper, shadow, or live trading."
        ),
    }


def atom_attribution(result: WalkForwardResult) -> dict:
    buckets: dict[str, list] = {}
    routes: dict[str, dict[str, int]] = {}
    for window in result.windows:
        for trade in window.test_trades:
            match = _ENTRY_RE.search(trade.entry_reason)
            if not match:
                continue
            atom = match.group("atom")
            route = match.group("route")
            buckets.setdefault(atom, []).append(trade)
            routes.setdefault(atom, {})[route] = routes.setdefault(atom, {}).get(route, 0) + 1
    out = {atom: _trade_metrics(trades) for atom, trades in sorted(buckets.items())}
    for atom, counts in routes.items():
        out.setdefault(atom, {})["routes"] = dict(sorted(counts.items()))
    return out


def _wf_record(
    candidate: AlphaDistillationCandidate,
    result: WalkForwardResult,
) -> dict:
    decision = ALPHA_DISTILLATION_GATES
    from vnedge.backtest.walk_forward import evaluate_promotion

    promotion = evaluate_promotion(result, decision)
    trades = [t for w in result.windows for t in w.test_trades]
    metrics = _aggregate_window_metrics(result)
    return {
        "attribution": _side_attribution(trades),
        "atom_attribution": atom_attribution(result),
        "exchange": candidate.exchange,
        "gates": "alpha_distillation",
        "strategy": ALPHA_DISTILLATION_ID,
        "symbol": candidate.symbol,
        "timeframe": BASE_TIMEFRAME,
        "windows": len(result.windows),
        "traded_windows": sum(1 for w in result.windows if w.test_metrics.num_trades > 0),
        "oos_trades": len(trades),
        "oos_net_usd": round(result.oos_net_profit_usd, 2),
        "profitable_windows_pct": round(result.oos_profitable_window_pct, 1),
        "total_fees_usd": round(sum(t.fees_usd for t in trades), 2),
        "profit_factor": metrics.profit_factor,
        "payoff_ratio": metrics.payoff_ratio,
        "verdict": "PASS" if promotion.passed else "REJECT",
        "reasons": list(promotion.reject_reasons),
        "updated": datetime.now(UTC).isoformat(),
    }


def _aggregate_window_metrics(result: WalkForwardResult) -> BacktestMetrics:
    trades = [t for w in result.windows for t in w.test_trades]
    wins = [t.net_pnl_usd for t in trades if t.net_pnl_usd > 0]
    losses = [-t.net_pnl_usd for t in trades if t.net_pnl_usd <= 0]
    profit_factor = round(sum(wins) / sum(losses), 2) if losses else (999.0 if wins else 0.0)
    payoff = (
        round((sum(wins) / len(wins)) / (sum(losses) / len(losses)), 2)
        if wins and losses
        else 0.0
    )
    return BacktestMetrics(
        num_trades=len(trades),
        skipped_by_sizing=0,
        net_profit_usd=round(sum(t.net_pnl_usd for t in trades), 2),
        return_pct=0.0,
        max_drawdown_pct=max((w.test_metrics.max_drawdown_pct for w in result.windows), default=0.0),
        sharpe=0.0,
        sortino=0.0,
        profit_factor=profit_factor,
        win_rate_pct=round(len(wins) / len(trades) * 100.0, 1) if trades else 0.0,
        avg_win_usd=round(sum(wins) / len(wins), 2) if wins else 0.0,
        avg_loss_usd=round(-sum(losses) / len(losses), 2) if losses else 0.0,
        total_fees_usd=round(sum(t.fees_usd for t in trades), 2),
        total_funding_usd=round(sum(t.funding_usd for t in trades), 2),
        exit_reasons={},
        payoff_ratio=payoff,
    )


def _trade_metrics(trades) -> dict:
    trades = tuple(trades)
    wins = [t.net_pnl_usd for t in trades if t.net_pnl_usd > 0]
    losses = [-t.net_pnl_usd for t in trades if t.net_pnl_usd <= 0]
    return {
        "trades": len(trades),
        "net_usd": round(sum(t.net_pnl_usd for t in trades), 2),
        "win_rate_pct": round(len(wins) / len(trades) * 100.0, 1) if trades else 0.0,
        "profit_factor": round(sum(wins) / sum(losses), 2)
        if losses else (999.0 if wins else 0.0),
        "payoff_ratio": round((sum(wins) / len(wins)) / (sum(losses) / len(losses)), 2)
        if wins and losses else 0.0,
        "total_fees_usd": round(sum(t.fees_usd for t in trades), 2),
    }


def _side_attribution(trades) -> dict:
    out = {}
    for side in ("long", "short"):
        side_trades = [t for t in trades if t.side == side]
        wins = [t for t in side_trades if t.net_pnl_usd > 0]
        out[side] = {
            "trades": len(side_trades),
            "net_usd": round(sum(t.net_pnl_usd for t in side_trades), 2),
            "win_rate_pct": round(len(wins) / len(side_trades) * 100.0, 1)
            if side_trades else 0.0,
        }
    return out


def _summary(records: list[dict]) -> dict:
    testable = [r for r in records if r.get("verdict") != "UNTESTABLE"]
    passes = [r for r in records if r.get("verdict") == "PASS"]
    positives = [
        r for r in testable
        if float(r.get("oos_net_usd", 0.0)) > 0.0
    ]
    best = max(testable, key=lambda r: float(r.get("oos_net_usd", 0.0)), default=None)
    return {
        "candidates": len(records),
        "testable": len(testable),
        "untestable": len(records) - len(testable),
        "passes": len(passes),
        "positive_after_fees": len(positives),
        "best": {
            "candidate_id": best.get("candidate_id"),
            "exchange": best.get("exchange"),
            "symbol": best.get("symbol"),
            "verdict": best.get("verdict"),
            "oos_net_usd": best.get("oos_net_usd"),
            "oos_trades": best.get("oos_trades"),
            "profit_factor": best.get("profit_factor"),
        } if best else None,
    }


def _untestable_record(candidate: AlphaDistillationCandidate, reason: str) -> dict:
    return {
        "exchange": candidate.exchange,
        "symbol": candidate.symbol,
        "timeframe": BASE_TIMEFRAME,
        "strategy": ALPHA_DISTILLATION_ID,
        "candidate_id": candidate.candidate_id,
        "allowed_atoms": list(candidate.allowed_atoms),
        "allowed_sides": list(candidate.allowed_sides),
        "windows": 0,
        "traded_windows": 0,
        "oos_trades": 0,
        "oos_net_usd": 0.0,
        "profitable_windows_pct": 0.0,
        "total_fees_usd": 0.0,
        "profit_factor": 0.0,
        "payoff_ratio": 0.0,
        "atom_attribution": {},
        "verdict": "UNTESTABLE",
        "reasons": [reason],
        "updated": datetime.now(UTC).isoformat(),
        "can_trade": False,
        "can_promote": False,
        "requires_human_approval": True,
        "requires_untouched_judgment": True,
    }


def _window(candles: pd.DataFrame, *, lookback_days: int) -> pd.DataFrame:
    if candles.empty:
        return candles
    cutoff = candles["timestamp"].iloc[-1] - pd.Timedelta(days=lookback_days)
    return candles[candles["timestamp"] >= cutoff].reset_index(drop=True)


def _empty_funding_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.Series(dtype="datetime64[ns, UTC]"),
            "funding_rate": pd.Series(dtype="float64"),
        }
    )


def parse_candidate(value: str) -> AlphaDistillationCandidate:
    parts = value.split("|")
    if len(parts) not in {2, 3, 4}:
        raise argparse.ArgumentTypeError(
            "candidate must be exchange|symbol, exchange|symbol|atom1,atom2, "
            "or exchange|symbol|atom1,atom2|long,short"
        )
    atoms = tuple(item.strip() for item in parts[2].split(",") if item.strip()) if len(parts) >= 3 else ()
    unknown = sorted(set(atoms) - set(FEATURE_ATOMS))
    if unknown:
        raise argparse.ArgumentTypeError(f"unknown atom(s): {unknown}")
    sides = (
        tuple(side.strip() for side in parts[3].split(",") if side.strip())
        if len(parts) == 4 else ("long", "short")
    )
    bad_sides = sorted(set(sides) - {"long", "short"})
    if bad_sides:
        raise argparse.ArgumentTypeError(f"unknown side(s): {bad_sides}")
    return AlphaDistillationCandidate(parts[0], parts[1], atoms, sides)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run VNEDGE alpha distillation research")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--out", default="research/live_research/alpha_distillation_latest.json")
    parser.add_argument("--lookback-days", type=int, default=120)
    parser.add_argument("--train-days", type=int, default=30)
    parser.add_argument("--test-days", type=int, default=7)
    parser.add_argument("--max-candidates", type=int, default=None)
    parser.add_argument("--candidate", type=parse_candidate, action="append", default=None)
    parser.add_argument("--no-context", action="store_true", help="diagnostic only")
    parser.add_argument("--no-1m-trigger", action="store_true", help="diagnostic only")
    parser.add_argument("--interval-seconds", type=int, default=0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    while True:
        report = _run_and_publish(args)
        if args.json:
            print(json.dumps(report, indent=2, default=str))
        else:
            print(_format_summary(report))
        if args.once or args.interval_seconds <= 0:
            return 0
        time.sleep(max(args.interval_seconds, 1))


def _run_and_publish(args: argparse.Namespace) -> dict:
    report = run_alpha_distillation_research(
        args.data_root,
        candidates=tuple(args.candidate) if args.candidate else None,
        max_candidates=args.max_candidates,
        lookback_days=args.lookback_days,
        train_days=args.train_days,
        test_days=args.test_days,
        require_context=not args.no_context,
        require_1m_trigger=not args.no_1m_trigger,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(report, indent=2, default=str))
    tmp.replace(out)
    return report


def _format_summary(report: dict) -> str:
    s = report["summary"]
    lines = [
        "=== Alpha distillation pack ===",
        f"updated: {report['updated']}",
        (
            "summary: "
            f"{s['passes']} pass, {s['positive_after_fees']} positive-after-fees, "
            f"{s['untestable']} untestable"
        ),
    ]
    for record in report["results"]:
        reasons = "; ".join(record.get("reasons", [])[:2]) or "ok"
        lines.append(
            f"  {record['exchange']} {record['symbol']}: {record['verdict']} "
            f"net=${float(record.get('oos_net_usd', 0.0)):+.2f} "
            f"trades={record.get('oos_trades', 0)} "
            f"pf={float(record.get('profit_factor', 0.0)):.2f} - {reasons}"
        )
    lines.append("output is research-only; no paper/shadow/live state changed")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
