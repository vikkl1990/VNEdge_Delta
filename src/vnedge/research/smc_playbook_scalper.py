"""SMC playbook scalper research runner.

Research-only miner for the explicit playbook:
HTF bias -> premium/discount -> liquidity/zone setup -> CHoCH -> 1m trigger
-> room-to-liquidity -> fee-aware walk-forward.

A positive result here is not a trading permission. It is a prompt for an
untouched judgment window and human approval, same as the rest of VNEDGE.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

import pandas as pd

from vnedge.backtest.backtester import BacktestConfig, run_backtest
from vnedge.backtest.metrics import compute_metrics
from vnedge.backtest.walk_forward import PromotionGates, param_grid, walk_forward
from vnedge.data.parquet_store import ParquetStore
from vnedge.research.continuous_research import wf_record
from vnedge.research.edge_leaderboard import build_edge_leaderboard
from vnedge.research.universe import load_research_targets
from vnedge.strategy.smc_playbook_scalper import SMCPlaybookScalper


BASE_TIMEFRAME = "15m"
CONTEXT_TIMEFRAMES = ("1h", "4h")
TRIGGER_TIMEFRAME = "1m"
DEFAULT_OUT = "research/live_research/smc_playbook_scalper_latest.json"

SMC_SCALPER_GATES = PromotionGates(
    min_splits=3,
    min_total_oos_trades=12,
    min_profit_factor=1.20,
    max_window_drawdown_pct=10.0,
    reject_zero_trade_windows=False,
    min_windows_with_trades_pct=50.0,
    min_payoff_ratio=1.20,
    max_single_trade_profit_share=0.45,
)

SMC_SCALPER_GRID = tuple(
    param_grid(
        structure_window=[16, 24],
        liquidity_window=[48],
        setup_lookback=[6],
        min_zone_quality=[3.0],
        min_room_r=[1.0, 1.3],
        stop_buffer_atr=[0.10],
        min_stop_bps=[100.0],
        take_profit_r=[1.5],
        require_1m_trigger=[True],
        trigger_profile=["momentum"],
    )
)
SMC_SMOKE_GRID = SMC_SCALPER_GRID[:1]


@dataclass(frozen=True)
class SMCPlaybookCandidate:
    exchange: str
    symbol: str
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
        sides = "".join(self.allowed_sides)
        return f"smc_playbook_scalper_v1__{self.exchange}__{safe_symbol}__{sides}"


def default_candidates(*, max_candidates: int | None = None) -> tuple[SMCPlaybookCandidate, ...]:
    out = [
        SMCPlaybookCandidate(target.exchange, target.symbol)
        for target in load_research_targets(timeframe=BASE_TIMEFRAME)
    ]
    return tuple(out[:max_candidates] if max_candidates is not None else out)


def run_smc_playbook_research(
    data_root: Path | str,
    *,
    candidates: Iterable[SMCPlaybookCandidate] | None = None,
    max_candidates: int | None = None,
    lookback_days: int = 120,
    train_days: int = 30,
    test_days: int = 7,
    fast_smoke: bool = False,
) -> dict:
    store = ParquetStore(data_root)
    candidate_list = (
        tuple(candidates)
        if candidates is not None else default_candidates(max_candidates=max_candidates)
    )
    if candidates is not None and max_candidates is not None:
        candidate_list = candidate_list[:max_candidates]
    records = [
        run_candidate(
            store,
            candidate,
            lookback_days=lookback_days,
            train_days=train_days,
            test_days=test_days,
            fast_smoke=fast_smoke,
        )
        for candidate in candidate_list
    ]
    return build_smc_report(
        records,
        candidates=candidate_list,
        lookback_days=lookback_days,
        train_days=train_days,
        test_days=test_days,
        fast_smoke=fast_smoke,
    )


def run_candidate(
    store: ParquetStore,
    candidate: SMCPlaybookCandidate,
    *,
    lookback_days: int,
    train_days: int,
    test_days: int,
    fast_smoke: bool = False,
) -> dict:
    try:
        candles = store.read_candles(candidate.exchange, candidate.symbol, BASE_TIMEFRAME)
        context_1h = store.read_candles(candidate.exchange, candidate.symbol, "1h")
        context_4h = store.read_candles(candidate.exchange, candidate.symbol, "4h")
        trigger_1m = store.read_candles(candidate.exchange, candidate.symbol, "1m")
    except FileNotFoundError as exc:
        return _untestable_record(candidate, f"missing data lane: {exc}")

    candles = _window(candles, lookback_days=lookback_days)
    if candles.empty:
        return _untestable_record(candidate, "15m base lane has no rows in lookback window")

    latest = candles["timestamp"].iloc[-1]
    context_cutoff = latest - pd.Timedelta(days=lookback_days + 45)
    trigger_cutoff = latest - pd.Timedelta(days=lookback_days + 2)
    context_1h = context_1h[context_1h["timestamp"] >= context_cutoff].reset_index(drop=True)
    context_4h = context_4h[context_4h["timestamp"] >= context_cutoff].reset_index(drop=True)
    trigger_1m = trigger_1m[trigger_1m["timestamp"] >= trigger_cutoff].reset_index(drop=True)

    train_bars = train_days * 24 * 4
    test_bars = test_days * 24 * 4
    if len(candles) < train_bars + test_bars:
        return _untestable_record(
            candidate,
            f"only {len(candles)} 15m bars; need >= {train_bars + test_bars}",
        )

    funding = _empty_funding_frame()
    config = BacktestConfig(max_holding_bars=16)

    def factory(**params):
        return SMCPlaybookScalper(
            funding=funding,
            context_1h=context_1h,
            context_4h=context_4h,
            trigger_1m=trigger_1m,
            base_timeframe=BASE_TIMEFRAME,
            allowed_sides=candidate.allowed_sides,
            **params,
        )

    if fast_smoke:
        return _smoke_record(
            candidate,
            candles,
            funding,
            factory(**SMC_SMOKE_GRID[0]),
            config,
            lookback_days=lookback_days,
        )

    try:
        result = walk_forward(
            candles,
            funding,
            factory,
            list(SMC_SCALPER_GRID),
            config,
            train_bars=train_bars,
            test_bars=test_bars,
            symbol=candidate.symbol,
            timeframe=BASE_TIMEFRAME,
        )
    except ValueError as exc:
        return _untestable_record(candidate, str(exc))

    record = wf_record(
        "smc_playbook_scalper_v1",
        candidate.symbol,
        result,
        SMC_SCALPER_GATES,
        gates_label="smc_scalper",
        exchange=candidate.exchange,
        timeframe=BASE_TIMEFRAME,
    )
    record.update(_record_meta(candidate))
    record["signal_cadence"] = _signal_cadence(
        candles,
        factory(**SMC_SMOKE_GRID[0]),
        lookback_days=lookback_days,
    )
    return record


def build_smc_report(
    records: list[dict],
    *,
    candidates: tuple[SMCPlaybookCandidate, ...],
    lookback_days: int,
    train_days: int,
    test_days: int,
    fast_smoke: bool,
) -> dict:
    leaderboard = build_edge_leaderboard(records)
    return {
        "updated": datetime.now(UTC).isoformat(),
        "strategy": "smc_playbook_scalper_v1",
        "policy": {
            "can_trade": False,
            "can_promote": False,
            "research_only": True,
            "requires_untouched_judgment": True,
            "requires_human_approval": True,
            "route_policy": "maker-first; taker fallback only after PF/payoff proof",
            "minimum_expectation": "net positive after fees in OOS walk-forward",
        },
        "flow": [
            "load_4h_1h_context",
            "load_15m_setup_lane",
            "load_1m_trigger_lane",
            "require_htf_bias_and_premium_discount",
            "require_sweep_zone_choch_sequence",
            "require_rejection_or_displacement_trigger",
            "require_room_to_external_liquidity",
            "walk_forward_after_fees",
            "pre_register_untouched_judgment_if_candidate",
        ],
        "parameters": {
            "lookback_days": lookback_days,
            "train_days": train_days,
            "test_days": test_days,
            "fast_smoke": fast_smoke,
            "base_timeframe": BASE_TIMEFRAME,
            "context_timeframes": list(CONTEXT_TIMEFRAMES),
            "trigger_timeframe": TRIGGER_TIMEFRAME,
            "grid_size": len(SMC_SCALPER_GRID if not fast_smoke else SMC_SMOKE_GRID),
            "max_holding_bars_15m": 16,
            "gates": {
                "min_splits": SMC_SCALPER_GATES.min_splits,
                "min_total_oos_trades": SMC_SCALPER_GATES.min_total_oos_trades,
                "min_profit_factor": SMC_SCALPER_GATES.min_profit_factor,
                "min_payoff_ratio": SMC_SCALPER_GATES.min_payoff_ratio,
                "max_window_drawdown_pct": SMC_SCALPER_GATES.max_window_drawdown_pct,
            },
        },
        "candidates": [asdict(candidate) for candidate in candidates],
        "summary": _summary(records),
        "results": records,
        "edge_leaderboard": leaderboard,
        "note": (
            "Research verdict only. PASS means candidate for untouched judgment; "
            "it does not enable paper, shadow, or live trading."
        ),
        "can_trade": False,
        "can_promote": False,
    }


def _smoke_record(
    candidate: SMCPlaybookCandidate,
    candles: pd.DataFrame,
    funding: pd.DataFrame,
    strategy: SMCPlaybookScalper,
    config: BacktestConfig,
    *,
    lookback_days: int,
) -> dict:
    result = run_backtest(
        candles,
        funding,
        strategy,
        config,
        symbol=candidate.symbol,
        timeframe=BASE_TIMEFRAME,
    )
    metrics = compute_metrics(result)
    trades = tuple(result.trades)
    wins = [trade.net_pnl_usd for trade in trades if trade.net_pnl_usd > 0]
    losses = [-trade.net_pnl_usd for trade in trades if trade.net_pnl_usd <= 0]
    profit_factor = (
        round(sum(wins) / sum(losses), 2)
        if losses else 999.0 if wins else 0.0
    )
    payoff = (
        round((sum(wins) / len(wins)) / (sum(losses) / len(losses)), 2)
        if wins and losses else 0.0
    )
    reasons: list[str] = []
    if metrics.num_trades < 5:
        reasons.append(f"smoke trades too few: {metrics.num_trades} < 5")
    if metrics.net_profit_usd <= 0:
        reasons.append(f"smoke net not positive after costs: ${metrics.net_profit_usd:.2f}")
    if profit_factor < 1.0:
        reasons.append(f"smoke profit factor below 1.0: {profit_factor:.2f}")

    record = {
        "attribution": _side_attribution(trades),
        "exchange": candidate.exchange,
        "gates": "smc_smoke",
        "strategy": "smc_playbook_scalper_v1",
        "symbol": candidate.symbol,
        "timeframe": BASE_TIMEFRAME,
        "windows": 1,
        "traded_windows": 1 if metrics.num_trades else 0,
        "oos_trades": metrics.num_trades,
        "oos_net_usd": round(metrics.net_profit_usd, 2),
        "profitable_windows_pct": 100.0 if metrics.net_profit_usd > 0 else 0.0,
        "total_fees_usd": round(metrics.total_fees_usd, 2),
        "skipped_by_sizing": result.skipped_by_sizing,
        "profit_factor": profit_factor,
        "payoff_ratio": payoff,
        "max_consecutive_stops": 0,
        "verdict": "SMOKE_POSITIVE" if not reasons else "SMOKE_REJECT",
        "reasons": reasons,
        "updated": datetime.now(UTC).isoformat(),
        "signal_cadence": _signal_cadence(candles, strategy, lookback_days=lookback_days),
        "smoke_backtest": True,
    }
    record.update(_record_meta(candidate))
    return record


def _record_meta(candidate: SMCPlaybookCandidate) -> dict:
    return {
        "candidate_id": candidate.candidate_id,
        "allowed_sides": list(candidate.allowed_sides),
        "base_timeframe": BASE_TIMEFRAME,
        "context_timeframes": list(CONTEXT_TIMEFRAMES),
        "trigger_timeframe": TRIGGER_TIMEFRAME,
        "playbook": {
            "context": "HTF bias, premium/discount, external liquidity",
            "setup": "strong/weak swing, OB/FVG, sweep, CHoCH/BOS",
            "trigger": "rejection or displacement from selected zone",
            "plan": "structural SL, room-to-liquidity, TP1/TP2/TP3, BE after TP1",
        },
        "route_policy": "research_only_maker_first_until_paper_proven",
        "can_trade": False,
        "can_promote": False,
        "requires_human_approval": True,
        "requires_untouched_judgment": True,
    }


def _summary(records: list[dict]) -> dict:
    testable = [r for r in records if r.get("verdict") != "UNTESTABLE"]
    passes = [r for r in testable if r.get("verdict") == "PASS"]
    best = max(testable, key=lambda r: float(r.get("oos_net_usd", 0.0)), default=None)
    return {
        "candidates": len(records),
        "testable": len(testable),
        "untestable": len(records) - len(testable),
        "passes": len(passes),
        "positive_after_fees": sum(
            1 for r in testable if float(r.get("oos_net_usd", 0.0)) > 0.0
        ),
        "raw_signals": sum(
            int((r.get("signal_cadence") or {}).get("raw_signals") or 0)
            for r in records
        ),
        "best": {
            "candidate_id": best.get("candidate_id"),
            "exchange": best.get("exchange"),
            "symbol": best.get("symbol"),
            "verdict": best.get("verdict"),
            "oos_net_usd": best.get("oos_net_usd"),
            "oos_trades": best.get("oos_trades"),
            "profit_factor": best.get("profit_factor"),
        }
        if best else None,
    }


def _signal_cadence(
    candles: pd.DataFrame,
    strategy: SMCPlaybookScalper,
    *,
    lookback_days: int,
) -> dict:
    try:
        df = strategy.prepare(candles).reset_index(drop=True)
    except Exception as exc:  # noqa: BLE001 - diagnostic only
        return {"raw_signals": 0, "signals_per_day": 0.0, "error": str(exc)}

    start = max(strategy.warmup_bars, 1)
    end = max(start, len(df) - 1)
    long_count = 0
    short_count = 0
    for index in range(start, end):
        intent = strategy.signal(df, index)
        if intent is None:
            continue
        if intent.side == "long":
            long_count += 1
        else:
            short_count += 1
    raw = long_count + short_count
    span_days = _span_days(df, fallback=lookback_days)
    return {
        "raw_signals": raw,
        "long_signals": long_count,
        "short_signals": short_count,
        "bars_scanned": max(end - start, 0),
        "span_days": round(span_days, 2),
        "signals_per_day": round(raw / span_days, 3) if span_days > 0 else 0.0,
    }


def _side_attribution(trades: tuple) -> dict:
    out = {}
    for side in ("long", "short"):
        side_trades = [trade for trade in trades if trade.side == side]
        wins = sum(1 for trade in side_trades if trade.net_pnl_usd > 0)
        out[side] = {
            "trades": len(side_trades),
            "net_usd": round(sum(trade.net_pnl_usd for trade in side_trades), 2),
            "win_rate_pct": (
                round(wins / len(side_trades) * 100.0, 1) if side_trades else 0.0
            ),
        }
    return out


def _untestable_record(candidate: SMCPlaybookCandidate, reason: str) -> dict:
    record = {
        "exchange": candidate.exchange,
        "symbol": candidate.symbol,
        "timeframe": BASE_TIMEFRAME,
        "strategy": "smc_playbook_scalper_v1",
        "windows": 0,
        "traded_windows": 0,
        "oos_trades": 0,
        "oos_net_usd": 0.0,
        "profitable_windows_pct": 0.0,
        "total_fees_usd": 0.0,
        "skipped_by_sizing": 0,
        "profit_factor": 0.0,
        "payoff_ratio": 0.0,
        "verdict": "UNTESTABLE",
        "reasons": [reason],
        "updated": datetime.now(UTC).isoformat(),
        "signal_cadence": {
            "raw_signals": 0,
            "signals_per_day": 0.0,
            "bars_scanned": 0,
        },
    }
    record.update(_record_meta(candidate))
    return record


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


def _span_days(df: pd.DataFrame, *, fallback: int) -> float:
    if len(df) < 2:
        return float(fallback)
    delta = df["timestamp"].iloc[-1] - df["timestamp"].iloc[0]
    days = delta.total_seconds() / 86_400
    return max(days, 1e-9)


def parse_candidate(value: str) -> SMCPlaybookCandidate:
    parts = value.split("|")
    if len(parts) not in {2, 3}:
        raise argparse.ArgumentTypeError(
            "candidate must be exchange|symbol or exchange|symbol|side1,side2"
        )
    sides = (
        tuple(side.strip() for side in parts[2].split(",") if side.strip())
        if len(parts) == 3 else ("long", "short")
    )
    return SMCPlaybookCandidate(parts[0], parts[1], sides)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run VNEDGE SMC playbook scalper research")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--lookback-days", type=int, default=120)
    parser.add_argument("--train-days", type=int, default=30)
    parser.add_argument("--test-days", type=int, default=7)
    parser.add_argument("--max-candidates", type=int, default=None)
    parser.add_argument("--candidate", type=parse_candidate, action="append", default=None)
    parser.add_argument("--interval-seconds", type=int, default=0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--fast-smoke", action="store_true")
    parser.add_argument("--json", action="store_true", help="print full report JSON")
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
    report = run_smc_playbook_research(
        args.data_root,
        candidates=tuple(args.candidate) if args.candidate else None,
        max_candidates=args.max_candidates,
        lookback_days=args.lookback_days,
        train_days=args.train_days,
        test_days=args.test_days,
        fast_smoke=args.fast_smoke,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(report, indent=2, default=str))
    tmp.replace(out)
    return report


def _format_summary(report: dict) -> str:
    lines = [
        "=== SMC playbook scalper ===",
        f"updated: {report['updated']}",
        (
            "summary: "
            f"{report['summary']['passes']} pass, "
            f"{report['summary']['positive_after_fees']} positive-after-fees, "
            f"{report['summary']['untestable']} untestable, "
            f"{report['summary']['raw_signals']} raw signals"
        ),
    ]
    for record in report["results"]:
        reasons = "; ".join(record.get("reasons", [])[:2]) or "ok"
        cadence = record.get("signal_cadence") or {}
        lines.append(
            f"  {record['exchange']} {record['symbol']}: "
            f"{record['verdict']} net=${float(record.get('oos_net_usd', 0.0)):+.2f} "
            f"trades={record.get('oos_trades', 0)} "
            f"signals/day={float(cadence.get('signals_per_day') or 0.0):.2f} "
            f"pf={float(record.get('profit_factor', 0.0)):.2f} - {reasons}"
        )
    lines.append("output is research-only; no paper/shadow/live state changed")
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
