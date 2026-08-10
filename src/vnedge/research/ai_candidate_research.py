"""AI-authored strategy candidate research — research only, never trades.

    python -m vnedge.research.ai_candidate_research

This is the research surface for strategies written by an AI (see
``vnedge.strategy.ai_sandbox`` and ``docs/AI_SANDBOX.md``). We adopt AI
*generation* leverage — let a model draft a strategy — WITHOUT ever letting
AI-authored code drive execution. An AI strategy is exactly a research
candidate: it must clear the same causality analyzer, the same walk-forward
gates, the same pre-registered untouched-data judgment, and the same human
approval as every hand-written strategy.

Pipeline per file under ``data/strategies/ai/`` (deny-by-default at every
step; a bad file is counted as a rejection, never a crash):

  1. AST validation + restricted-namespace load (``ai_sandbox``). Anything the
     validator does not positively recognise — imports outside a tiny
     whitelist, ``eval``/``exec``/``open``/dunder access, lookahead patterns —
     rejects the file before it is ever executed. The loaded class's
     ``strategy_id`` is force-prefixed ``ai_``.
  2. Causality gate (``vnedge.research.causality_analyzer.analyze_strategy``):
     truncation invariance is REQUIRED. Any violation — or any error trying to
     verify it — REFUSES the candidate (fail closed). No walk-forward runs on a
     strategy we could not prove causal.
  3. Walk-forward on rolling train/test windows, then the frozen promotion
     gates. A passing result is a ``CANDIDATE`` — a prompt for a pre-registered
     judgment on untouched data, never a promotion.

HARD GUARDS, carried on the payload and on every candidate row:
``can_trade=False``, ``can_promote=False``, ``requires_untouched_judgment=True``.
AI strategies are namespaced ``ai_*`` and are NEVER inserted into
``strategy_registry.STRATEGIES`` — nothing here can reach the trading path.

The payload is published to ``research/live_research/ai_candidates.json`` and
folded into the continuous-research document (mirroring the cascade-reversion
folding hook), so it shares the existing research cadence — no new service.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import UTC, datetime
from inspect import signature
from pathlib import Path

import pandas as pd

from vnedge.backtest.backtester import BacktestConfig
from vnedge.backtest.walk_forward import (
    PromotionDecision,
    PromotionGates,
    WalkForwardResult,
    evaluate_promotion,
    param_grid,
    walk_forward,
)
from vnedge.research.causality_analyzer import (
    CausalityReport,
    analyze_strategy,
    synthetic_market,
)
from vnedge.strategy.ai_sandbox import (
    AI_STRATEGY_ID_PREFIX,
    SandboxViolation,
    load_ai_strategy,
    validate_strategy_source,
)
from vnedge.strategy.base_strategy import BaseStrategy

logger = logging.getLogger(__name__)

AI_STRATEGY_DIR = "data/strategies/ai"
AI_CANDIDATES_LATEST = "ai_candidates.json"
FAMILY = "ai_authored"

#: Default rolling-window sizes, matching the standard 1h continuous-research
#: lanes (train 1440 bars / test 720 bars).
DEFAULT_TRAIN_BARS = 1440
DEFAULT_TEST_BARS = 720
DEFAULT_LOOKBACK_DAYS = 365
_MAX_VIOLATIONS = 8  # cap the violation list carried on the payload

# Verdicts (all research-only; none of them authorise trading or promotion):
VERDICT_CANDIDATE = "CANDIDATE"          # passed causality + promotion gates
VERDICT_REJECT = "REJECT"                # causal, but failed promotion gates
VERDICT_REFUSED_LOOKAHEAD = "REFUSED_LOOKAHEAD"  # failed truncation invariance
VERDICT_REFUSED_CAUSALITY_ERROR = "REFUSED_CAUSALITY_ERROR"  # couldn't verify
VERDICT_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"  # not enough bars to walk-forward
VERDICT_ERROR = "ERROR"                  # unexpected per-candidate failure


def ai_candidate_policy() -> dict:
    """Hard-wired governance stamped onto the payload and every candidate."""
    return {
        "family": FAMILY,
        "namespace": AI_STRATEGY_ID_PREFIX,
        "can_trade": False,
        "can_promote": False,
        "requires_untouched_judgment": True,
        "auto_registered": False,
        "note": (
            "AI-authored strategies are research candidates only. They are "
            "AST-validated (deny-by-default), executed in a restricted "
            "namespace, causality-checked (truncation invariance) and "
            "walk-forwarded like any hand-written strategy. A CANDIDATE verdict "
            "is a prompt for pre-registered untouched-data judgment and human "
            "approval — never a promotion, never auto-registered into the "
            "trading path."
        ),
    }


# --- Strategy factory ----------------------------------------------------------


def _accepts_funding(cls: type[BaseStrategy]) -> bool:
    """True if the strategy's ``__init__`` takes an explicit ``funding`` param.

    The causality analyzer and walk-forward both hand strategies the funding
    history visible at their horizon. AI strategies that ignore funding (the
    common case) simply don't declare the parameter; a funding-based AI
    strategy declares ``funding`` and receives it."""
    try:
        return "funding" in signature(cls.__init__).parameters
    except (ValueError, TypeError):
        return False


def _ai_factory(cls: type[BaseStrategy]):
    """Build a factory usable by BOTH ``analyze_strategy`` (called with the
    visible funding positionally) and ``walk_forward`` (called with ``**params``).
    """
    wants_funding = _accepts_funding(cls)

    def factory(funding: pd.DataFrame | None = None, **params) -> BaseStrategy:
        if wants_funding:
            return cls(funding=funding, **params)
        return cls(**params)

    return factory


# --- Directory scan (deny-by-default, reject reasons captured) ------------------


def _scan_ai_dir(
    strategy_dir: str | Path,
) -> tuple[dict[str, tuple[type[BaseStrategy], str]], list[dict]]:
    """Validate + load every ``*.py`` under ``strategy_dir``.

    Returns ``(loaded, rejected)`` where ``loaded`` maps prefixed strategy_id
    to ``(class, filename)`` and ``rejected`` is a list of ``{file, reason}``.
    A file that fails validation, fails to load, or collides on strategy_id is
    recorded in ``rejected`` and skipped — it can never crash the scan.
    """
    directory = Path(strategy_dir)
    loaded: dict[str, tuple[type[BaseStrategy], str]] = {}
    rejected: list[dict] = []
    if not directory.is_dir():
        return loaded, rejected

    for file in sorted(directory.glob("*.py")):
        if file.name.startswith("_"):  # __init__.py / private helpers
            continue
        try:
            source = file.read_text(encoding="utf-8")
        except OSError as exc:
            rejected.append({"file": file.name, "reason": f"read error: {exc}"})
            continue
        report = validate_strategy_source(source)
        if not report.ok:
            rejected.append({"file": file.name, "reason": report.describe()})
            logger.warning("AI candidate: rejected %s\n%s", file.name, report.describe())
            continue
        try:
            cls = load_ai_strategy(source, module_name=f"ai_{file.stem}")
        except SandboxViolation as exc:
            rejected.append({"file": file.name, "reason": exc.report.describe()})
            logger.warning("AI candidate: rejected %s — %s", file.name, exc)
            continue
        except Exception as exc:  # noqa: BLE001 — a bad file must never crash the scan
            rejected.append({"file": file.name, "reason": f"load error: {exc}"})
            logger.warning("AI candidate: failed to load %s: %s", file.name, exc)
            continue
        if cls.strategy_id in loaded:
            rejected.append(
                {"file": file.name, "reason": f"duplicate strategy_id '{cls.strategy_id}'"}
            )
            continue
        loaded[cls.strategy_id] = (cls, file.name)
    return loaded, rejected


# --- Per-candidate evaluation --------------------------------------------------


def _causality_dict(report: CausalityReport) -> dict:
    return {
        "passed": report.passed,
        "n_bars": report.n_bars,
        "warmup_bars": report.warmup_bars,
        "cut_points": list(report.cut_points),
        "feature_columns": len(report.feature_columns),
        "signal_indexes_checked": report.signal_indexes_checked,
        "fired_bars": report.fired_bars,
        "violations": [v.describe() for v in report.violations[:_MAX_VIOLATIONS]],
    }


def _walk_forward_dict(result: WalkForwardResult, decision: PromotionDecision) -> dict:
    trades = sum(w.test_metrics.num_trades for w in result.windows)
    traded = sum(1 for w in result.windows if w.test_metrics.num_trades > 0)
    return {
        "windows": len(result.windows),
        "traded_windows": traded,
        "oos_trades": trades,
        "oos_net_usd": round(result.oos_net_profit_usd, 2),
        "profitable_windows_pct": round(result.oos_profitable_window_pct, 1),
        "passed": decision.passed,
    }


def _base_entry(cls: type[BaseStrategy], source_file: str) -> dict:
    return {
        "strategy_id": cls.strategy_id,
        "source_file": source_file,
        "family": FAMILY,
        "can_trade": False,
        "can_promote": False,
        "requires_untouched_judgment": True,
    }


def evaluate_ai_candidate(
    cls: type[BaseStrategy],
    source_file: str,
    candles: pd.DataFrame,
    funding: pd.DataFrame | None,
    *,
    grid: list[dict],
    config: BacktestConfig,
    gates: PromotionGates,
    train_bars: int,
    test_bars: int,
    symbol: str,
    timeframe: str,
    cut_points: list[int] | None,
) -> dict:
    """Causality-then-walk-forward one AI strategy. Fail closed: any lookahead
    violation (or inability to verify causality) REFUSES the candidate and no
    walk-forward runs."""
    factory = _ai_factory(cls)
    entry = _base_entry(cls, source_file)

    # (1) Causality gate — REQUIRED. Fail closed on violation OR error.
    try:
        report = analyze_strategy(factory, candles, funding, cut_points=cut_points)
    except Exception as exc:  # noqa: BLE001 — cannot verify => refuse, never promote
        entry.update(
            causality=None,
            walk_forward=None,
            verdict=VERDICT_REFUSED_CAUSALITY_ERROR,
            reasons=[f"causality analysis errored: {exc}"],
        )
        return entry
    entry["causality"] = _causality_dict(report)
    if not report.passed:
        entry.update(
            walk_forward=None,
            verdict=VERDICT_REFUSED_LOOKAHEAD,
            reasons=[v.describe() for v in report.violations[:_MAX_VIOLATIONS]],
        )
        return entry

    # (2) Walk-forward + frozen promotion gates.
    try:
        result = walk_forward(
            candles,
            funding,
            factory,
            grid,
            config,
            train_bars=train_bars,
            test_bars=test_bars,
            symbol=symbol,
            timeframe=timeframe,
        )
    except ValueError as exc:
        entry.update(
            walk_forward=None, verdict=VERDICT_INSUFFICIENT_DATA, reasons=[str(exc)]
        )
        return entry

    decision = evaluate_promotion(result, gates)
    entry["walk_forward"] = _walk_forward_dict(result, decision)
    entry["verdict"] = VERDICT_CANDIDATE if decision.passed else VERDICT_REJECT
    entry["reasons"] = list(decision.reject_reasons)
    return entry


# --- Top-level run -------------------------------------------------------------


def run_ai_candidate_research(
    candles: pd.DataFrame,
    funding: pd.DataFrame | None = None,
    *,
    strategy_dir: str | Path = AI_STRATEGY_DIR,
    exchange: str = "binanceusdm",
    symbol: str = "BTC/USDT:USDT",
    timeframe: str = "1h",
    dataset_source: str = "provided",
    grid: list[dict] | None = None,
    gates: PromotionGates | None = None,
    config: BacktestConfig | None = None,
    train_bars: int = DEFAULT_TRAIN_BARS,
    test_bars: int = DEFAULT_TEST_BARS,
    cut_points: list[int] | None = None,
) -> dict:
    """Load, validate, causality-check and walk-forward every AI strategy in
    ``strategy_dir`` on one dataset. Returns the research payload dict.

    Never raises for a bad strategy/file: validation rejects are counted, a
    candidate that errors mid-evaluation is recorded as ``ERROR``. The payload
    and every row carry ``can_trade=False`` / ``can_promote=False`` /
    ``requires_untouched_judgment=True``."""
    grid = grid if grid is not None else param_grid()
    gates = gates if gates is not None else PromotionGates()
    config = config if config is not None else BacktestConfig()

    loaded, rejected = _scan_ai_dir(strategy_dir)
    candidates: list[dict] = []
    for strategy_id in sorted(loaded):
        cls, source_file = loaded[strategy_id]
        try:
            candidates.append(
                evaluate_ai_candidate(
                    cls,
                    source_file,
                    candles,
                    funding,
                    grid=grid,
                    config=config,
                    gates=gates,
                    train_bars=train_bars,
                    test_bars=test_bars,
                    symbol=symbol,
                    timeframe=timeframe,
                    cut_points=cut_points,
                )
            )
        except Exception as exc:  # noqa: BLE001 — one candidate must not sink the surface
            logger.exception("AI candidate %s failed: %s", strategy_id, exc)
            entry = _base_entry(cls, source_file)
            entry.update(
                causality=None, walk_forward=None, verdict=VERDICT_ERROR, reasons=[str(exc)]
            )
            candidates.append(entry)

    verdict_counts: dict[str, int] = {}
    for c in candidates:
        verdict_counts[c["verdict"]] = verdict_counts.get(c["verdict"], 0) + 1

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "policy": ai_candidate_policy(),
        "family": FAMILY,
        "strategy_dir": str(strategy_dir),
        "dataset": {
            "exchange": exchange,
            "symbol": symbol,
            "timeframe": timeframe,
            "bars": int(len(candles)),
            "source": dataset_source,
        },
        "candidates": candidates,
        "rejected_files": rejected,
        "summary": {
            "loaded": len(loaded),
            "rejected_files": len(rejected),
            "candidates": len(candidates),
            "verdict_counts": verdict_counts,
            "can_trade": False,
            "can_promote": False,
            "requires_untouched_judgment": True,
        },
        "can_trade": False,
        "can_promote": False,
        "requires_untouched_judgment": True,
    }


def write_ai_candidates_payload(payload: dict, out_dir: Path | str) -> Path:
    """Atomic publish for the continuous_research folding hook (same
    tmp+replace discipline as cascade_reversion.json)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / AI_CANDIDATES_LATEST
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(path)
    return path


# --- Store-backed entry point (used by the continuous research loop) -----------


def _primary_target(targets) -> tuple[str, str, str]:
    """Pick one (exchange, symbol, timeframe) to run the AI candidates on.
    Prefer a BTC target (deepest history) else the first configured target."""
    if not targets:
        return ("binanceusdm", "BTC/USDT:USDT", "1h")
    for t in targets:
        if "BTC" in t.symbol:
            return (t.exchange, t.symbol, t.timeframe)
    t = targets[0]
    return (t.exchange, t.symbol, t.timeframe)


def _load_candles_from_store(
    store, exchange: str, symbol: str, timeframe: str, lookback_days: int
) -> tuple[pd.DataFrame, pd.DataFrame | None]:
    candles = store.read_candles(exchange, symbol, timeframe)
    if candles is None or candles.empty:
        raise FileNotFoundError(f"no candles for {exchange} {symbol} {timeframe}")
    try:
        funding: pd.DataFrame | None = store.read_funding(exchange, symbol)
    except FileNotFoundError:
        funding = None
    cutoff = candles["timestamp"].iloc[-1] - pd.Timedelta(days=lookback_days)
    c = candles[candles["timestamp"] >= cutoff].reset_index(drop=True)
    f = (
        None
        if funding is None or funding.empty
        else funding[funding["timestamp"] >= cutoff].reset_index(drop=True)
    )
    return c, f


def build_ai_candidates_payload(
    store,
    targets=(),
    *,
    strategy_dir: str | Path = AI_STRATEGY_DIR,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    train_bars: int = DEFAULT_TRAIN_BARS,
    test_bars: int = DEFAULT_TEST_BARS,
) -> dict:
    """Resolve a dataset from ``store`` and run the AI candidate research.

    Falls back to the deterministic synthetic market when the primary target's
    candles are not available (so the causality gate still runs offline). The
    fallback is honest about being too short to walk-forward — those candidates
    land as ``INSUFFICIENT_DATA``, never as a spurious pass."""
    exchange, symbol, timeframe = _primary_target(targets)
    dataset_source = "parquet"
    try:
        candles, funding = _load_candles_from_store(
            store, exchange, symbol, timeframe, lookback_days
        )
    except (FileNotFoundError, KeyError, ValueError) as exc:
        logger.info(
            "AI candidate research: no parquet candles for %s %s %s (%s) — "
            "using synthetic market for causality",
            exchange, symbol, timeframe, exc,
        )
        candles, funding = synthetic_market()
        dataset_source = "synthetic_fallback"
    return run_ai_candidate_research(
        candles,
        funding,
        strategy_dir=strategy_dir,
        exchange=exchange,
        symbol=symbol,
        timeframe=timeframe,
        dataset_source=dataset_source,
        train_bars=train_bars,
        test_bars=test_bars,
    )


# --- CLI -----------------------------------------------------------------------


def render_report(payload: dict) -> str:
    lines = [
        "AI-authored strategy candidates (research only)",
        "policy=research_only can_trade=false can_promote=false "
        "requires_untouched_judgment=true",
        f"dataset: {payload['dataset']['exchange']} {payload['dataset']['symbol']} "
        f"{payload['dataset']['timeframe']} — {payload['dataset']['bars']} bars "
        f"({payload['dataset']['source']})",
        "",
        "verdict                    causal  oos$     trades  strategy_id",
    ]
    for c in payload["candidates"]:
        wf = c.get("walk_forward") or {}
        causal = "n/a" if c.get("causality") is None else str(c["causality"]["passed"])
        lines.append(
            f"{c['verdict']:<26} {causal:<7} "
            f"{wf.get('oos_net_usd', 0.0):>7.2f} "
            f"{wf.get('oos_trades', 0):>6}  {c['strategy_id']}"
        )
    for r in payload["rejected_files"]:
        lines.append(f"REJECTED_FILE              {r['file']}: {r['reason'].splitlines()[0]}")
    if not payload["candidates"] and not payload["rejected_files"]:
        lines.append(f"no AI strategies found under {payload['strategy_dir']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(
        description="AI-authored strategy candidate research (research only)"
    )
    p.add_argument("--data-root", default="data")
    p.add_argument("--strategy-dir", default=AI_STRATEGY_DIR)
    p.add_argument("--exchange", default="binanceusdm")
    p.add_argument("--symbol", default="BTC/USDT:USDT")
    p.add_argument("--timeframe", default="1h")
    p.add_argument("--train-bars", type=int, default=DEFAULT_TRAIN_BARS)
    p.add_argument("--test-bars", type=int, default=DEFAULT_TEST_BARS)
    p.add_argument("--synthetic", action="store_true",
                   help="use the deterministic synthetic market instead of parquet")
    p.add_argument("--out", default="research/live_research",
                   help="directory for ai_candidates.json")
    p.add_argument("--no-publish", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--interval-seconds", type=float, default=0.0,
                   help="rescan cadence; <= 0 runs once and exits")
    args = p.parse_args(argv)

    def _once() -> dict:
        if args.synthetic:
            candles, funding = synthetic_market()
            return run_ai_candidate_research(
                candles, funding, strategy_dir=args.strategy_dir,
                exchange=args.exchange, symbol=args.symbol, timeframe=args.timeframe,
                dataset_source="synthetic", train_bars=args.train_bars,
                test_bars=args.test_bars,
            )
        from vnedge.data.parquet_store import ParquetStore
        from vnedge.research.universe import ResearchTarget

        store = ParquetStore(args.data_root)
        target = ResearchTarget(
            exchange=args.exchange, symbol=args.symbol, timeframe=args.timeframe
        )
        return build_ai_candidates_payload(
            store, (target,), strategy_dir=args.strategy_dir,
            train_bars=args.train_bars, test_bars=args.test_bars,
        )

    while True:
        payload = _once()
        if not args.no_publish:
            path = write_ai_candidates_payload(payload, args.out)
            logger.info("published %s", path)
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(render_report(payload))
        if args.interval_seconds <= 0:
            return 0
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
