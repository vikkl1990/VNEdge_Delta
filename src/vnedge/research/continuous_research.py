"""Continuous research loop — the slow loop, made continuous.

    python -m vnedge.research.continuous_research

Every cycle (default hourly — walk-forward output only changes when a new
candle lands):

  1. refresh candles + funding via REST (incremental; full 365d backfill on
     first run), through the same quality gate as every other ingest
  2. re-run walk-forward for every registered strategy family x symbol on
     the rolling window
  3. evaluate promotion gates (sparse gates for event strategies, standard
     otherwise) and publish verdicts to research/live_research/latest.json
     (atomic) + an append-only feed.jsonl

Governance: this process has NO access to execution. It shares nothing with
the trial container except read-only market data and the research output
directory. Gates are the same frozen objects the judgment runs use; a PASS
here is a *candidate* signal prompting a proper pre-registered judgment on
untouched data — never an auto-promotion.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
from ccxt.base.errors import NotSupported

from vnedge.backtest.backtester import BacktestConfig
from vnedge.backtest.walk_forward import (
    OFFENSIVE_GATES,
    SPARSE_STRATEGY_GATES,
    PromotionGates,
    WalkForwardResult,
    evaluate_promotion,
    param_grid,
    walk_forward,
)
from vnedge.data.candle_ingestor import ingest_candles
from vnedge.data.ccxt_client import CcxtPublicClient
from vnedge.data.funding_ingestor import ingest_funding
from vnedge.data.parquet_store import ParquetStore
from vnedge.research import data_burn
from vnedge.risk.protections import STOP_EXIT_REASONS
from vnedge.research.ai_candidate_research import (
    build_ai_candidates_payload,
    write_ai_candidates_payload,
)
from vnedge.research.alpha_factory import alpha_factory_policy, run_alpha_factory
from vnedge.research.edge_leaderboard import build_edge_leaderboard
from vnedge.research.edge_agents import EdgeResearchAgent, runnable_variant_proposals
from vnedge.research.factor_ranker import (
    build_factor_ranker_payload,
    write_factor_ranker_payload,
)
from vnedge.research.public_bot_inspiration import (
    publish_public_bot_inspiration,
    run_public_bot_inspiration,
)
from vnedge.research.shadow_perf_reader import DEFAULT_JOURNAL_DIR, read_shadow_perf
from vnedge.research.shadow_manifest import (
    generate_shadow_manifest,
    load_shadow_manifest,
    write_shadow_manifest,
)
from vnedge.research.scalper_edge_miner import mine_recorded_days
from vnedge.research.scalper_scanners import (
    scan_recorded_days,
    scanner_policy,
    select_recorder_targets,
)
from vnedge.research.scalper_focus import build_scalper_focus
from vnedge.research.strategy_diagnostics import diagnose
from vnedge.research.universe import (
    ResearchTarget,
    load_research_targets,
    summarize_universe,
)
from vnedge.strategy.funding_mean_reversion import FundingMeanReversion
from vnedge.strategy.funding_squeeze_continuation import FundingSqueezeContinuation
from vnedge.strategy.panic_reversal import PanicReversal
from vnedge.strategy.alpha_stack import AlphaStackConfluence
from vnedge.strategy.quant_signal_pack import QuantSignalPack
from vnedge.strategy.trend_continuation import TrendContinuation
from vnedge.strategy.trend_retest import TrendRetest
from vnedge.strategy.vol_expansion_breakout import VolatilityExpansionBreakout
from vnedge.scalping.parameter_registry import DEFAULT_SCALPER_PARAMETER_REGISTRY

logger = logging.getLogger(__name__)

EXCHANGE = "binanceusdm"  # backward-compatible default for tests/helpers
TIMEFRAME = "1h"
LOOKBACK_DAYS = 365
INTERVAL_SECONDS = float(os.environ.get("RESEARCH_INTERVAL_SECONDS", "3600"))
OUT_DIR = Path("research/live_research")

# The strategy/symbol pair currently running in the governed paper trial —
# drift detection watches THIS series and alerts the operator. It never
# mutates the trial.
TRIAL_STRATEGY = "funding_mean_reversion_v1"
TRIAL_EXCHANGE = "binanceusdm"
TRIAL_SYMBOL = "BTC/USDT:USDT"
DRIFT_CONSECUTIVE_REJECTS = 3
SCALPER_DISCOVERY_FLOW = (
    "tick_l2_recorder",
    "edge_miner",
    "scanner_ranking",
    "conservative_replay",
    "untouched_judgment",
    "paper_shadow_after_human_approval",
)
_QUANT_ENTRY_RE = re.compile(
    r"\bquant_signal_pack\s+(?P<side>long|short)\s+"
    r"(?P<family>[a-z_]+)\s+score\b"
)


def side_attribution(result: WalkForwardResult) -> dict:
    """Break OOS performance by trade side. For funding-MR this doubles as
    funding-sign attribution: shorts fade positive-funding crowding, longs
    fade negative. A PASS carried by one side is a pre-registration prompt
    for a side-specific variant — never an in-place tweak."""
    out = {}
    for side in ("long", "short"):
        trades = [
            t for w in result.windows for t in w.test_trades if t.side == side
        ]
        wins = sum(1 for t in trades if t.net_pnl_usd > 0)
        out[side] = {
            "trades": len(trades),
            "net_usd": round(sum(t.net_pnl_usd for t in trades), 2),
            "win_rate_pct": round(wins / len(trades) * 100.0, 1) if trades else 0.0,
        }
    return out


def _trade_metrics(trades) -> dict:
    trades = tuple(trades)
    wins = [t.net_pnl_usd for t in trades if t.net_pnl_usd > 0]
    losses = [-t.net_pnl_usd for t in trades if t.net_pnl_usd <= 0]
    payoff = (
        round((sum(wins) / len(wins)) / (sum(losses) / len(losses)), 2)
        if wins and losses
        else 0.0
    )
    if losses:
        profit_factor = round(sum(wins) / sum(losses), 2)
    else:
        profit_factor = 999.0 if wins else 0.0
    return {
        "trades": len(trades),
        "net_usd": round(sum(t.net_pnl_usd for t in trades), 2),
        "win_rate_pct": round(len(wins) / len(trades) * 100.0, 1) if trades else 0.0,
        "profit_factor": profit_factor,
        "payoff_ratio": payoff,
        "total_fees_usd": round(sum(t.fees_usd for t in trades), 2),
    }


def quant_family_attribution(result: WalkForwardResult) -> dict:
    """Break Quant Signal Pack OOS trades by the dominant signal family.

    This is the feedback loop the scalper needs: if aggregate Quant rejects,
    the agent can still discover whether one family is profitable while the
    rest is fee/noise drag.
    """
    buckets: dict[str, list] = {}
    for window in result.windows:
        for trade in window.test_trades:
            match = _QUANT_ENTRY_RE.search(trade.entry_reason)
            if not match:
                continue
            family = match.group("family")
            buckets.setdefault(family, []).append(trade)
    return {family: _trade_metrics(trades) for family, trades in sorted(buckets.items())}


def wf_record(
    strategy: str, symbol: str, result: WalkForwardResult, gates: PromotionGates,
    gates_label: str = "standard", exchange: str = EXCHANGE, timeframe: str = TIMEFRAME,
) -> dict:
    decision = evaluate_promotion(result, gates)
    trades = sum(w.test_metrics.num_trades for w in result.windows)
    traded = sum(1 for w in result.windows if w.test_metrics.num_trades > 0)
    all_trades = [t for w in result.windows for t in w.test_trades]
    total_fees = round(sum(t.fees_usd for t in all_trades), 2)
    wins = [t.net_pnl_usd for t in all_trades if t.net_pnl_usd > 0]
    losses = [-t.net_pnl_usd for t in all_trades if t.net_pnl_usd <= 0]
    payoff = round(
        (sum(wins) / len(wins)) / (sum(losses) / len(losses)), 2
    ) if wins and losses else 0.0
    if losses:
        profit_factor = round(sum(wins) / sum(losses), 2)
    else:
        profit_factor = 999.0 if wins else 0.0
    # Longest run of consecutive stop exits across the OOS trade sequence —
    # feeds the diagnostics' consecutive_stops tag (engine-level protection
    # proposals, never strategy-param tuning).
    stop_streak = streak = 0
    for t in all_trades:
        if t.exit_reason in STOP_EXIT_REASONS:
            streak += 1
            stop_streak = max(stop_streak, streak)
        else:
            streak = 0
    record = {
        "attribution": side_attribution(result),
        "exchange": exchange,
        "gates": gates_label,
        "strategy": strategy,
        "symbol": symbol,
        "timeframe": timeframe,
        "windows": len(result.windows),
        "traded_windows": traded,
        "oos_trades": trades,
        "oos_net_usd": round(result.oos_net_profit_usd, 2),
        "profitable_windows_pct": round(result.oos_profitable_window_pct, 1),
        "total_fees_usd": total_fees,
        "profit_factor": profit_factor,
        "payoff_ratio": payoff,
        "max_consecutive_stops": stop_streak,
        "verdict": "PASS" if decision.passed else "REJECT",
        "reasons": list(decision.reject_reasons),
        "updated": datetime.now(UTC).isoformat(),
    }
    families = quant_family_attribution(result)
    if families:
        record["family_attribution"] = families
    return record


def read_feed_series(
    strategy: str, symbol: str, exchange: str = EXCHANGE, limit: int = 48
) -> list[dict]:
    """Prior cycles' records for one strategy/symbol, oldest first."""
    path = OUT_DIR / "feed.jsonl"
    if not path.exists():
        return []
    series = []
    for line in path.read_text().strip().splitlines()[-limit:]:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        for r in payload.get("results", []):
            if (
                r.get("strategy") == strategy
                and r.get("symbol") == symbol
                and r.get("exchange", EXCHANGE) == exchange
            ):
                series.append(r)
    return series


def compute_drift_alerts(prev: list[dict], current: dict) -> list[dict]:
    """Edge-triggered drift detection for the live-trial strategy's rolling
    profile. Fires on TRANSITIONS (so hourly cycles don't re-alarm), and
    protects the trial by informing the operator — never by mutating it."""
    alerts: list[dict] = []
    now = datetime.now(UTC).isoformat()

    def alert(rule: str, severity: str, message: str) -> None:
        alerts.append({"ts": now, "rule_id": rule, "severity": severity,
                       "message": message, "mode": "research"})

    last = prev[-1] if prev else None
    if last:
        if last["verdict"] == "PASS" and current["verdict"] == "REJECT":
            alert("drift_verdict_flip", "warning",
                  f"rolling verdict flipped PASS->REJECT for "
                  f"{current['strategy']} {current['symbol']}: "
                  + "; ".join(current["reasons"][:3]))
        if last["oos_net_usd"] > 0 >= current["oos_net_usd"]:
            alert("drift_oos_sign_flip", "warning",
                  f"rolling OOS net turned negative: ${current['oos_net_usd']:+.2f} "
                  f"(was ${last['oos_net_usd']:+.2f})")

    trade_counts = [r["oos_trades"] for r in prev if r["oos_trades"] > 0]
    if len(trade_counts) >= 6:
        median = sorted(trade_counts)[len(trade_counts) // 2]
        collapsed = current["oos_trades"] < 0.5 * median
        was_collapsed = last is not None and last["oos_trades"] < 0.5 * median
        if collapsed and not was_collapsed:
            alert("drift_trade_collapse", "warning",
                  f"OOS trade count collapsed: {current['oos_trades']} vs "
                  f"trailing median {median}")

    rejects = 0
    for r in reversed(prev + [current]):
        if r["verdict"] == "REJECT":
            rejects += 1
        else:
            break
    if rejects == DRIFT_CONSECUTIVE_REJECTS:  # fires exactly once at the threshold
        alert("drift_consecutive_rejects", "critical",
              f"{rejects} consecutive rolling REJECT cycles for "
              f"{current['strategy']} {current['symbol']} — review the live "
              f"paper trial for regime decay (do not tune mid-trial)")
    return alerts


async def refresh_data(store: ParquetStore, target: ResearchTarget | str) -> bool:
    """Incremental refresh through the quality gate; full backfill if the
    store is empty. Returns False when the gate rejects (research skips the
    symbol this cycle — fail closed, never research on bad data)."""
    until_ms = int(time.time() * 1000)
    target = _as_target(target)
    try:
        candles = store.read_candles(target.exchange, target.symbol, target.timeframe)
        since_ms = int(candles["timestamp"].iloc[-1].value // 1_000_000) - 2 * 3_600_000
    except FileNotFoundError:
        since_ms = until_ms - LOOKBACK_DAYS * 86_400_000
        logger.info("%s: no local data — full %dd backfill", target.label, LOOKBACK_DAYS)

    # Funding prints only every 8h: a short incremental window would fetch
    # zero rows and (correctly) fail the empty-dataset gate. Always look
    # back >= 48h for funding; the upsert dedupes the overlap.
    funding_since_ms = min(since_ms, until_ms - 48 * 3_600_000)
    async with CcxtPublicClient(target.exchange) as client:
        c = await ingest_candles(
            client, store, symbol=target.symbol, timeframe=target.timeframe,
            since_ms=since_ms, until_ms=until_ms,
        )
        try:
            f = await ingest_funding(
                client, store, symbol=target.symbol,
                since_ms=funding_since_ms, until_ms=until_ms,
            )
            funding_ok = f.persisted
            funding_summary = f.report.summary
        except NotSupported as exc:
            logger.info("%s: funding history unavailable: %s", target.label, exc)
            funding_ok = True
            funding_summary = "funding history unsupported; candle-only lanes continue"
    if not (c.persisted and funding_ok):
        logger.error("%s: quality gate rejected refresh (%s / %s)",
                     target.label, c.report.summary, funding_summary)
        return False
    return True


def run_walk_forwards(store: ParquetStore, target: ResearchTarget | str) -> list[dict]:
    target = _as_target(target)
    candles = store.read_candles(target.exchange, target.symbol, target.timeframe)
    try:
        funding = store.read_funding(target.exchange, target.symbol)
    except FileNotFoundError:
        funding = _empty_funding_frame()
    cutoff = candles["timestamp"].iloc[-1] - pd.Timedelta(days=LOOKBACK_DAYS)
    c = candles[candles["timestamp"] >= cutoff].reset_index(drop=True)
    f = funding[funding["timestamp"] >= cutoff].reset_index(drop=True)
    config = BacktestConfig()
    records = []

    lanes = [
        ("funding_mean_reversion_v1",
         lambda **p: FundingMeanReversion(funding=f, **p),
         param_grid(extreme_pct=[0.85, 0.95], z_entry=[1.5, 2.5]),
         720, SPARSE_STRATEGY_GATES, "sparse", True),
        ("trend_continuation_v1",
         lambda **p: TrendContinuation(funding=f, **p),
         param_grid(breakout_bars=[48, 96], take_profit_r=[2.0, 3.0]),
         360, PromotionGates(), "standard", False),
        # --- offensive lanes (milestone 10A): research-only ---
        ("volatility_expansion_breakout_v1",
         lambda **p: VolatilityExpansionBreakout(funding=f, **p),
         param_grid(breakout_bars=[48, 96]),
         720, OFFENSIVE_GATES, "offensive", False),
        ("panic_reversal_v1",
         lambda **p: PanicReversal(funding=f, **p),
         param_grid(drop_z_entry=[-2.5, -3.0]),
         720, OFFENSIVE_GATES, "offensive", False),
        ("funding_squeeze_continuation_v1",
         lambda **p: FundingSqueezeContinuation(funding=f, **p),
         param_grid(extreme_pct=[0.88, 0.94]),
         720, OFFENSIVE_GATES, "offensive", True),
        ("alpha_stack_confluence_v1",
         lambda **p: AlphaStackConfluence(funding=f, **p),
         param_grid(structure_window=[24, 48], min_score=[5.0, 6.0],
                    take_profit_r=[1.5, 2.0]),
         720, OFFENSIVE_GATES, "offensive", False),
        ("quant_signal_pack_v1",
         lambda **p: QuantSignalPack(funding=f, **p),
         param_grid(structure_window=[24, 48], min_score=[5.0, 6.0],
                    take_profit_r=[1.5, 2.0]),
         720, OFFENSIVE_GATES, "offensive", False),
        ("trend_retest_v1",
         lambda **p: TrendRetest(funding=f, **p),
         param_grid(min_score=[70.0, 80.0], take_profit_r=[2.0, 3.0]),
         720, OFFENSIVE_GATES, "offensive", False),
    ]
    enabled = _enabled_research_strategies()
    for name, factory, grid, test_bars, gates, label, requires_funding in lanes:
        if enabled is not None and name not in enabled:
            continue
        if requires_funding and f.empty:
            records.append(
                _skipped_record(
                    name, target, gates_label=label,
                    reason="funding history unavailable for this venue",
                )
            )
            continue
        result = walk_forward(
            c, f, factory, grid, config,
            train_bars=1440, test_bars=test_bars, symbol=target.symbol,
            timeframe=target.timeframe,
        )
        records.append(
            wf_record(
                name, target.symbol, result, gates, gates_label=label,
                exchange=target.exchange, timeframe=target.timeframe,
            )
        )
    return records


def _skipped_record(
    strategy: str,
    target: ResearchTarget,
    *,
    gates_label: str,
    reason: str,
) -> dict:
    return {
        "attribution": {
            "long": {"trades": 0, "net_usd": 0.0, "win_rate_pct": 0.0},
            "short": {"trades": 0, "net_usd": 0.0, "win_rate_pct": 0.0},
        },
        "exchange": target.exchange,
        "gates": gates_label,
        "strategy": strategy,
        "symbol": target.symbol,
        "timeframe": target.timeframe,
        "windows": 0,
        "traded_windows": 0,
        "oos_trades": 0,
        "oos_net_usd": 0.0,
        "profitable_windows_pct": 0.0,
        "total_fees_usd": 0.0,
        "payoff_ratio": 0.0,
        "verdict": "UNTESTABLE",
        "reasons": [reason],
        "updated": datetime.now(UTC).isoformat(),
    }


def _as_target(target: ResearchTarget | str) -> ResearchTarget:
    if isinstance(target, ResearchTarget):
        return target
    return ResearchTarget(exchange=EXCHANGE, symbol=target, timeframe=TIMEFRAME)


_GATES = {
    "sparse": SPARSE_STRATEGY_GATES,
    "offensive": OFFENSIVE_GATES,
    "standard": PromotionGates(),
}

_FUNDING_HISTORY_REQUIRED = {
    "funding_mean_reversion_v1",
    "funding_squeeze_continuation_v1",
}


def _empty_funding_frame() -> pd.DataFrame:
    return pd.DataFrame({
        "timestamp": pd.Series(dtype="datetime64[ns, UTC]"),
        "funding_rate": pd.Series(dtype="float64"),
    })


def _build_strategy(strategy_id: str, funding, **params):
    from vnedge.strategy.strategy_registry import STRATEGIES

    return STRATEGIES[strategy_id](funding, **params)


def _load_auto_state() -> dict:
    path = OUT_DIR / "auto_explore.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            pass
    return {"tried": [], "total_attempts": 0}


def _save_auto_state(state: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "auto_explore.json").write_text(json.dumps(state, indent=2))


def auto_explore(
    store: ParquetStore,
    records: list[dict],
    *,
    targets: tuple[ResearchTarget, ...] = (),
    max_variants: int = 2,
    burn_registry_path: Path | str | None = None,
) -> list[dict]:
    """Bounded auto-uplift: for the closest-to-passing failing lanes, run
    their top diagnostic suggestion as an EXPLORATORY variant. Results are
    labeled auto=true; a rolling PASS here is a candidate, never a promotion.

    Attempts are keyed by proposal_id + the OOS data-window fingerprint
    (window end day + params hash): the same variant is never re-run on a
    materially-same window, and every run is recorded in the data-burn
    registry as an exploratory_burn — the mechanical trail that judgment
    rounds check before touching a window. Legacy (pre-fingerprint) keys in
    auto_explore.json are still honored and skipped."""
    state = _load_auto_state()
    tried = set(state["tried"])
    config = BacktestConfig()
    variants: list[dict] = []
    registry_path = (
        Path(burn_registry_path) if burn_registry_path is not None
        else data_burn.DEFAULT_REGISTRY_PATH
    )
    plan = EdgeResearchAgent(max_variant_proposals=max_variants).plan(
        records, targets=targets
    )

    for proposal in runnable_variant_proposals(plan):
        if len(variants) >= max_variants:
            break
        legacy_key = proposal["proposal_id"]
        if legacy_key in tried:  # backward compat: old permanent keys
            continue
        target = ResearchTarget(
            exchange=proposal["exchange"],
            symbol=proposal["symbol"],
            timeframe=proposal["timeframe"],
        )
        candles = store.read_candles(target.exchange, target.symbol, target.timeframe)
        try:
            funding = store.read_funding(target.exchange, target.symbol)
        except FileNotFoundError:
            funding = _empty_funding_frame()
        if proposal["strategy_id"] in _FUNDING_HISTORY_REQUIRED and funding.empty:
            tried.add(legacy_key)  # venue-level gap — permanent skip
            logger.info(
                "auto-explore %s skipped: funding history unavailable for %s",
                legacy_key, target.label,
            )
            continue
        cutoff = candles["timestamp"].iloc[-1] - pd.Timedelta(days=LOOKBACK_DAYS)
        c = candles[candles["timestamp"] >= cutoff].reset_index(drop=True)
        f = funding[funding["timestamp"] >= cutoff].reset_index(drop=True)
        key = legacy_key + "|" + data_burn.window_fingerprint(
            c["timestamp"].iloc[-1],
            {
                "fixed_params": proposal["fixed_params"],
                "grid_axes": proposal["grid_axes"],
                "test_bars": proposal["test_bars"],
            },
        )
        if key in tried:  # same variant on a materially-same window
            continue
        factory = (lambda proposal=proposal, f=f: (
            lambda **p: _build_strategy(
                proposal["strategy_id"], f,
                **{**proposal["fixed_params"], **p},
            )
        ))()
        grid = param_grid(**proposal["grid_axes"]) if proposal["grid_axes"] else [{}]
        try:
            result = walk_forward(
                c, f, factory, grid, config, train_bars=1440,
                test_bars=proposal["test_bars"], symbol=target.symbol,
                timeframe=target.timeframe,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("auto-explore %s failed: %s", key, exc)
            tried.add(legacy_key)  # deterministic failure — permanent skip
            continue
        rec = wf_record(
            proposal["variant_id"], target.symbol, result,
            _GATES[proposal["gates_label"]], gates_label=proposal["gates_label"],
            exchange=target.exchange, timeframe=target.timeframe,
        )
        rec["auto"] = True
        rec["parent"] = proposal["parent_strategy"]
        rec["goal"] = proposal["goal"]
        rec["rationale"] = proposal["rationale"]
        rec["agent"] = proposal["agent"]
        rec["proposal_id"] = proposal["proposal_id"]
        variants.append(rec)
        tried.add(key)
        state["total_attempts"] += 1
        # Mechanical burn trail: the variant saw the full lookback range
        # (train + test), so the whole range is burned for judgment purposes.
        try:
            data_burn.record_burn(
                strategy_id=proposal["variant_id"],
                symbol=target.symbol,
                exchange=target.exchange,
                window_start=c["timestamp"].iloc[0],
                window_end=c["timestamp"].iloc[-1],
                verdict=rec["verdict"],
                note=f"auto_explore {key} (parent {proposal['parent_strategy']})",
                path=registry_path,
            )
        except Exception as exc:  # noqa: BLE001 — trail must not kill the cycle
            logger.warning("auto-explore burn record failed for %s: %s", key, exc)
        logger.info("auto-explore %s: %s (oos $%+.2f) — %s",
                    key, rec["verdict"], rec["oos_net_usd"], proposal["goal"])

    state["tried"] = sorted(tried)
    _save_auto_state(state)
    return variants


def run_scalper_research(
    data_root: Path | str,
    targets: tuple[ResearchTarget, ...],
    *,
    days: tuple[str, ...] | None = None,
) -> dict:
    """Run the scalper slow-loop in discovery-first order.

    This publishes research artifacts only. Edge-miner rows are hypotheses;
    scanner rows are diagnostics; replay candidates can only come from the
    scanner/replay diagnostic path.
    """
    root = Path(data_root)
    targets = targets[:_env_int("SCALPER_RESEARCH_MAX_TARGETS", 12)]
    days = days or _scalper_research_days(root, targets)
    payload = {
        "policy": scanner_policy(),
        "flow": list(SCALPER_DISCOVERY_FLOW),
        "flow_guards": {
            "edge_miner_before_scanner": True,
            "scanner_output_is_not_candidate": True,
            "replay_required_for_candidate": True,
            "can_trade": False,
            "can_promote": False,
        },
        "targets": [asdict(t) for t in targets],
        "days": list(days),
        "edge_hypotheses": [],
        "scanner_results": [],
        "recorder_targets": [],
        "replay_candidates": [],
    }
    if not days:
        payload["note"] = "no recorded tick/L2 days found; run the public recorder first"
        return payload

    edge_results = mine_recorded_days(root, targets, days)
    scans = scan_recorded_days(root, targets, days)
    recorder_targets = select_recorder_targets(scans)
    max_rows = _env_int("SCALPER_RESEARCH_MAX_ROWS", 50)
    payload["edge_hypotheses"] = [r.to_dict() for r in edge_results[:max_rows]]
    payload["scanner_results"] = [s.to_dict() for s in scans[:max_rows]]
    payload["recorder_targets"] = [s.to_dict() for s in recorder_targets]
    payload["replay_candidates"] = [
        {**s.to_dict(), "source": "conservative_replay"}
        for s in scans
        if s.state == "REPLAY_CANDIDATE"
    ][:max_rows]
    payload["focus"] = build_scalper_focus(
        scans,
        edge_results,
        recorder_targets=recorder_targets,
        days=days,
        max_lanes=_env_int("SCALPER_FOCUS_MAX_LANES", 12),
        max_hypotheses=_env_int("SCALPER_FOCUS_MAX_HYPOTHESES", 12),
    )
    return payload


def _scalper_research_enabled() -> bool:
    return os.environ.get("SCALPER_RESEARCH_ENABLED", "1").lower() not in {
        "0", "false", "no", "off",
    }


def _alpha_factory_enabled() -> bool:
    return os.environ.get("ALPHA_FACTORY_ENABLED", "1").lower() not in {
        "0", "false", "no", "off",
    }


def _factor_ranker_enabled() -> bool:
    return os.environ.get("FACTOR_RANKER_ENABLED", "1").lower() not in {
        "0", "false", "no", "off",
    }


def _public_bot_inspiration_enabled() -> bool:
    return os.environ.get("PUBLIC_BOT_INSPIRATION_ENABLED", "1").lower() not in {
        "0", "false", "no", "off",
    }


def _ai_candidate_research_enabled() -> bool:
    return os.environ.get("AI_CANDIDATE_RESEARCH_ENABLED", "1").lower() not in {
        "0", "false", "no", "off",
    }


def _enabled_research_strategies() -> set[str] | None:
    configured = set(_split_csv(os.environ.get("RESEARCH_STRATEGIES")))
    return configured or None


def _load_l2_latest() -> dict:
    """Last output of the decoupled l2-research-loop, or {} if absent/unreadable."""
    return _read_optional_json(OUT_DIR / "l2_latest.json")


def _load_event_taker_latest() -> dict:
    """Last output of the taker-only aggTrades event replay
    (vnedge.research.event_taker_replay), or {} if absent/unreadable."""
    return _read_optional_json(OUT_DIR / "event_taker_replay.json")


def _load_cascade_reversion_latest() -> dict:
    """Last output of the liquidation-cascade reversion replay
    (vnedge.research.cascade_reversion), or {} if absent/unreadable."""
    return _read_optional_json(OUT_DIR / "cascade_reversion.json")


def _load_ai_candidates_latest() -> dict:
    """Last output of the AI-authored strategy candidate research
    (vnedge.research.ai_candidate_research), or {} if absent/unreadable."""
    return _read_optional_json(OUT_DIR / "ai_candidates.json")


def _load_leadlag_echo_scalp_latest() -> dict:
    """Last output of the cross-venue lead-lag echo scalp replay
    (vnedge.research.leadlag_echo_scalp), or {} if absent/unreadable."""
    return _read_optional_json(OUT_DIR / "leadlag_echo_scalp.json")


def _load_realtime_shadow_scalp_latest() -> dict:
    """Last output of the real-time shadow scalp runner
    (vnedge.runtime.realtime_shadow_scalp), or {} if absent/unreadable."""
    return _read_optional_json(OUT_DIR / "realtime_shadow_scalp.json")


def _read_optional_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _split_csv(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _scalper_research_days(root: Path, targets: tuple[ResearchTarget, ...]) -> tuple[str, ...]:
    explicit = _split_csv(os.environ.get("SCALPER_RESEARCH_DAYS"))
    if explicit:
        return explicit
    limit = _env_int("SCALPER_RESEARCH_MAX_DAYS", 1)
    days = sorted(_available_tick_days(root, targets))
    return tuple(days[-limit:])


def _available_tick_days(root: Path, targets: tuple[ResearchTarget, ...]) -> set[str]:
    days: set[str] = set()
    for target in targets:
        safe = target.symbol.split(":")[0].replace("/", "")
        symbol_root = root / "ticks" / f"exchange={target.exchange}" / f"symbol={safe}"
        book_days = _stream_days(symbol_root / "stream=book")
        trade_days = _stream_days(symbol_root / "stream=trades")
        days.update(book_days & trade_days)
    return days


def _stream_days(stream_root: Path) -> set[str]:
    if not stream_root.exists():
        return set()
    days = {p.stem for p in stream_root.glob("*.parquet")}
    days.update(p.name for p in stream_root.iterdir() if p.is_dir())
    return {d for d in days if len(d) == 8 and d.isdigit()}


@dataclass(frozen=True)
class ResearchPayload:
    """Everything one research cycle publishes to latest.json / feed.jsonl.

    Fields mirror the published document's sections; all are optional so
    partial publishers (tests, degraded cycles) get the same empty-section
    placeholders the dashboard expects.
    """

    records: list[dict] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    drift_alerts: list[dict] = field(default_factory=list)
    auto_state: dict = field(default_factory=dict)
    agent_plan: dict = field(default_factory=dict)
    edge_leaderboard: dict = field(default_factory=dict)
    universe: dict = field(default_factory=dict)
    factor_ranker: dict = field(default_factory=dict)
    public_bot_inspiration: dict = field(default_factory=dict)
    scalper_research: dict = field(default_factory=dict)
    alpha_factory: dict = field(default_factory=dict)
    scalper_parameter_registry: dict = field(default_factory=dict)
    live_shadow_perf: dict = field(default_factory=dict)
    event_taker_replay: dict = field(default_factory=dict)
    cascade_reversion: dict = field(default_factory=dict)
    leadlag_echo_scalp: dict = field(default_factory=dict)
    realtime_shadow_scalp: dict = field(default_factory=dict)
    # AI-authored strategy candidates (research only — never trades/promotes)
    ai_candidates: dict = field(default_factory=dict)


def publish(payload: ResearchPayload) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    doc = {
        "generated_at": datetime.now(UTC).isoformat(),
        "cycle_seconds": round(time.time() - payload.started, 1),
        "lookback_days": LOOKBACK_DAYS,
        "note": "rolling exploratory walk-forward — a PASS is a candidate "
                "signal, not a promotion; judgment requires untouched data. "
                "auto=true rows are machine-proposed uplift variants, "
                "exploratory only.",
        "results": payload.records,
        "drift_alerts": payload.drift_alerts or [],
        "universe": payload.universe or {},
        "factor_ranker": payload.factor_ranker or {},
        "public_bot_inspiration": payload.public_bot_inspiration or {},
        "scalper_research": payload.scalper_research or {},
        "alpha_factory": payload.alpha_factory or {},
        "scalper_parameter_registry": payload.scalper_parameter_registry or {},
        "event_taker_replay": payload.event_taker_replay or {},
        "cascade_reversion": payload.cascade_reversion or {},
        # AI-authored strategy candidates: AST-validated, causality-gated,
        # walk-forwarded — research only, never a trade or a promotion.
        "ai_candidates": payload.ai_candidates or {},
        "leadlag_echo_scalp": payload.leadlag_echo_scalp or {},
        # live-tick firing of the scalp detectors (real-time shadow, never a
        # trade); real-time only accelerates evidence, it does not gate.
        "realtime_shadow_scalp": payload.realtime_shadow_scalp or {},
        "shadow_lanes": load_shadow_manifest(OUT_DIR),
        # live virtual track record of the shadow lanes (read-only journal
        # aggregation; observability evidence, never a gate)
        "live_shadow_perf": payload.live_shadow_perf or {},
        "edge_agents": payload.agent_plan or {},
        "edge_leaderboard": payload.edge_leaderboard or {},
        "auto_explore": {
            "total_attempts": (payload.auto_state or {}).get("total_attempts", 0),
            "distinct_variants": len((payload.auto_state or {}).get("tried", [])),
        },
    }
    tmp = OUT_DIR / "latest.json.tmp"
    tmp.write_text(json.dumps(doc, indent=2))
    tmp.replace(OUT_DIR / "latest.json")
    with open(OUT_DIR / "feed.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(doc) + "\n")


async def run_cycle() -> list[dict]:
    started = time.time()
    store = ParquetStore("data")
    records: list[dict] = []
    targets = load_research_targets()
    for target in targets:
        try:
            if not await refresh_data(store, target):
                continue
            records.extend(run_walk_forwards(store, target))
        except Exception as exc:  # noqa: BLE001 — one symbol must not kill the loop
            logger.exception("research cycle failed for %s: %s", target.label, exc)

    # Drift watch on the live-trial strategy (read feed BEFORE publishing
    # this cycle, so `prev` excludes the current record).
    drift: list[dict] = []
    current = next((r for r in records if r["strategy"] == TRIAL_STRATEGY
                    and r["symbol"] == TRIAL_SYMBOL
                    and r.get("exchange", EXCHANGE) == TRIAL_EXCHANGE), None)
    if current is not None:
        drift = compute_drift_alerts(
            read_feed_series(TRIAL_STRATEGY, TRIAL_SYMBOL, TRIAL_EXCHANGE), current
        )
        if drift:
            from vnedge.monitoring.notifiers import LogNotifier, TelegramNotifier

            notifiers = [LogNotifier()]
            telegram = TelegramNotifier.from_env()
            if telegram is not None:
                notifiers.append(telegram)
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            with open(OUT_DIR / "alerts.jsonl", "a", encoding="utf-8") as fh:
                for a in drift:
                    fh.write(json.dumps(a) + "\n")
                    for n in notifiers:
                        try:
                            n.send(a)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("drift notifier failed: %s", exc)

    # Attach failure diagnoses to every REJECT (cheap, always on): "why did
    # it fail, and what bounded variant might uplift it".
    for r in records:
        if r["verdict"] == "REJECT":
            diag = diagnose(r)
            r["diagnosis"] = {
                "failure_tags": list(diag.failure_tags),
                "notes": list(diag.notes),
                "suggested_variants": [s.variant_id for s in diag.suggestions],
            }

    # Bounded auto-explore: run the top suggestion for the closest-to-passing
    # lanes as exploratory variants. Never promotes, never touches the trial.
    try:
        variants = auto_explore(store, records, targets=targets)
        records.extend(variants)
    except Exception as exc:  # noqa: BLE001 — auto-explore must never kill a cycle
        logger.exception("auto-explore failed: %s", exc)

    agent_plan = EdgeResearchAgent().plan(records, targets=targets)
    # Live shadow-lane virtual track record (read-only journal aggregation).
    # Degrades to {} when the journals aren't mounted/present — the ranking
    # then simply carries no live_shadow evidence.
    live_shadow_perf: dict = {}
    try:
        live_shadow_perf = read_shadow_perf(
            os.environ.get("SHADOW_PERF_JOURNAL_DIR", str(DEFAULT_JOURNAL_DIR)),
            scalp_journal_dir=os.environ.get(
                "SCALP_SHADOW_JOURNAL_DIR", "logs/scalp_shadow"
            ),
        )
    except Exception as exc:  # noqa: BLE001 — observability must not kill a cycle
        logger.exception("shadow perf read failed: %s", exc)
    judgment_records: list[dict] = []
    try:
        judgment_records = data_burn.read_records()
    except Exception as exc:  # noqa: BLE001 — governance overlay must not kill research
        logger.exception("burn registry read failed: %s", exc)
    edge_leaderboard = build_edge_leaderboard(
        records,
        max_rows=_env_int("EDGE_LEADERBOARD_MAX_ROWS", 50),
        max_queue=_env_int("EDGE_PROMOTION_QUEUE_MAX_ROWS", 20),
        shadow_perf=live_shadow_perf,
        judgment_records=judgment_records,
    )
    factor_ranker: dict = {}
    if _factor_ranker_enabled():
        try:
            factor_ranker = build_factor_ranker_payload(store, targets)
            write_factor_ranker_payload(factor_ranker, OUT_DIR)
        except Exception as exc:  # noqa: BLE001 — triage must not kill research
            logger.exception("factor ranker failed: %s", exc)
            factor_ranker = {
                "policy": {
                    "research_only": True,
                    "can_trade": False,
                    "can_promote": False,
                },
                "error": str(exc),
            }
    public_bot_inspiration: dict = {}
    if _public_bot_inspiration_enabled():
        try:
            public_bot_inspiration = run_public_bot_inspiration()
            publish_public_bot_inspiration(
                public_bot_inspiration,
                OUT_DIR / "public_bot_inspiration_latest.json",
                OUT_DIR / "public_bot_inspiration_feed.jsonl",
            )
        except Exception as exc:  # noqa: BLE001 — link triage must not kill research
            logger.exception("public bot inspiration matrix failed: %s", exc)
            public_bot_inspiration = {
                "policy": {
                    "research_only": True,
                    "can_trade": False,
                    "can_promote": False,
                },
                "error": str(exc),
            }
    # research -> shadow bridge: turn profitable winners with locked params into
    # a shadow-lane manifest (cheap, candle data only). Never trades/promotes.
    try:
        write_shadow_manifest(
            generate_shadow_manifest(
                list(agent_plan.profitable_pairs),
                judgment_records=judgment_records,
                filtered_replay_payload=_read_optional_json(
                    OUT_DIR / "filtered_replay_latest.json"
                ),
            ),
            OUT_DIR,
        )
    except Exception as exc:  # noqa: BLE001 — manifest gen must not kill research
        logger.exception("shadow manifest generation failed: %s", exc)
    scalper_research: dict = {}
    alpha_factory: dict = {}
    if _scalper_research_enabled():
        try:
            scalper_research = run_scalper_research("data", targets)
        except Exception as exc:  # noqa: BLE001 — discovery must not kill candle research
            logger.exception("scalper research discovery failed: %s", exc)
            scalper_research = {
                "policy": scanner_policy(),
                "flow": list(SCALPER_DISCOVERY_FLOW),
                "error": str(exc),
                "flow_guards": {
                    "can_trade": False,
                    "can_promote": False,
                    "replay_required_for_candidate": True,
                },
            }
    if _alpha_factory_enabled():
        try:
            alpha_days = tuple(
                scalper_research.get("days")
                or _scalper_research_days(Path("data"), targets)
            )
            alpha_factory = run_alpha_factory(
                "data",
                targets,
                days=alpha_days,
                max_rows=_env_int("ALPHA_FACTORY_MAX_ROWS", 50),
            )
        except Exception as exc:  # noqa: BLE001 — alpha mining must not kill research
            logger.exception("alpha factory failed: %s", exc)
            alpha_factory = {
                "policy": alpha_factory_policy(),
                "error": str(exc),
                "flow_guards": {
                    "raw_hypothesis_is_not_signal": True,
                    "conservative_replay_required": True,
                    "can_trade": False,
                    "can_promote": False,
                },
            }
    # When the inline L2 passes are disabled (default), fold in the decoupled
    # l2-research-loop's last output so the dashboard still shows L2 discovery
    # without the candle cycle ever scanning the tape.
    l2 = _load_l2_latest()
    scalper_research = scalper_research or l2.get("scalper_research", {})
    alpha_factory = alpha_factory or l2.get("alpha_factory", {})
    scalper_parameter_registry = (
        l2.get("scalper_parameter_registry")
        or DEFAULT_SCALPER_PARAMETER_REGISTRY.to_dict()
    )
    # AI-authored strategy candidates: load + AST-validate + causality-check +
    # walk-forward the data/strategies/ai/ files and fold the result in. Runs on
    # the existing cadence (no new service); a bad file is counted, never fatal.
    if _ai_candidate_research_enabled():
        try:
            write_ai_candidates_payload(
                build_ai_candidates_payload(store, targets), OUT_DIR
            )
        except Exception as exc:  # noqa: BLE001 — AI research must never kill a cycle
            logger.exception("ai candidate research failed: %s", exc)
    publish(ResearchPayload(
        records=records,
        started=started,
        drift_alerts=drift,
        auto_state=_load_auto_state(),
        agent_plan={
            "profitable_pairs": list(agent_plan.profitable_pairs),
            "proposals": list(agent_plan.proposals),
            "policy": agent_plan.policy,
        },
        edge_leaderboard=edge_leaderboard,
        universe=summarize_universe(targets),
        factor_ranker=factor_ranker,
        public_bot_inspiration=public_bot_inspiration,
        scalper_research=scalper_research,
        alpha_factory=alpha_factory,
        scalper_parameter_registry=scalper_parameter_registry,
        live_shadow_perf=live_shadow_perf,
        event_taker_replay=_load_event_taker_latest(),
        cascade_reversion=_load_cascade_reversion_latest(),
        ai_candidates=_load_ai_candidates_latest(),
        leadlag_echo_scalp=_load_leadlag_echo_scalp_latest(),
        realtime_shadow_scalp=_load_realtime_shadow_scalp_latest(),
    ))
    for r in records:
        tag = " [auto]" if r.get("auto") else ""
        logger.info("%s %s %s: %s (oos $%+.2f, %d trades, %d windows)%s",
                    r.get("exchange", EXCHANGE), r["strategy"], r["symbol"], r["verdict"],
                    r["oos_net_usd"], r["oos_trades"], r["windows"], tag)
    return records


async def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    logger.info(
        "continuous research loop: %s every %.0fs",
        [t.label for t in load_research_targets()], INTERVAL_SECONDS,
    )
    while True:
        try:
            await run_cycle()
        except Exception as exc:  # noqa: BLE001
            logger.exception("cycle crashed: %s", exc)
        await asyncio.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
