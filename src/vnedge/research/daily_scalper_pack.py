"""Daily scalper research runner.

This mines the practical intraday scalper shape:

* 4h/1h context permission
* 15m setup candles
* 1m trigger confirmation
* isolated Quant Signal Pack families

It is research-only. A PASS here is a prompt for a pre-registered untouched
judgment window, not permission to trade.
"""

from __future__ import annotations

import argparse
import json
import sys
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
from vnedge.strategy.daily_scalper_pack import (
    DAILY_SCALPER_FAMILIES,
    TRIGGER_PROFILES,
    DailyScalperPack,
)

BASE_TIMEFRAME = "15m"
CONTEXT_TIMEFRAMES = ("1h", "4h")
TRIGGER_TIMEFRAME = "1m"
DEFAULT_DAILY_SCALPER_OUT = "research/live_research/daily_scalper_latest.json"
DEFAULT_CADENCE_OUT = "research/live_research/daily_scalper_cadence_latest.json"

DAILY_SCALPER_GATES = PromotionGates(
    min_splits=3,
    min_total_oos_trades=20,
    min_profit_factor=1.25,
    max_window_drawdown_pct=8.0,
    reject_zero_trade_windows=False,
    min_windows_with_trades_pct=60.0,
    min_payoff_ratio=1.25,
    max_single_trade_profit_share=0.35,
)


@dataclass(frozen=True)
class DailyScalperCandidate:
    exchange: str
    symbol: str
    family: str
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
        return f"daily_scalper_pack_v1__{self.exchange}__{safe_symbol}__{self.family}"


@dataclass(frozen=True)
class DailyScalperCadenceProfile:
    """Named lane-refactor profile for cadence-aware research.

    Profiles widen or tighten the setup/trigger envelope in a pre-declared
    way. They are research-only: even a positive result still needs the normal
    untouched judgment and human approval path before shadow/paper changes.
    """

    name: str
    description: str
    trigger_profile: str
    require_1m_trigger: bool
    grid: tuple[dict, ...]
    max_holding_bars: int = 16
    shadow_refactor_eligible: bool = True

    def baseline_params(self) -> dict:
        return dict(self.grid[0]) if self.grid else {}

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["grid_size"] = len(self.grid)
        payload.pop("grid", None)
        return payload


DAILY_SCALPER_CADENCE_PROFILES: dict[str, DailyScalperCadenceProfile] = {
    "strict": DailyScalperCadenceProfile(
        name="strict",
        description="current production-like 1m confirmation and tight score gates",
        trigger_profile="strict",
        require_1m_trigger=True,
        grid=tuple(param_grid(
            structure_window=[24, 32],
            min_score=[4.0, 4.5],
            min_score_delta=[0.75],
            stop_atr_mult=[0.9, 1.1],
            take_profit_r=[1.2, 1.5],
        )),
    ),
    "balanced": DailyScalperCadenceProfile(
        name="balanced",
        description="moderately wider score gates plus momentum-grade 1m confirmation",
        trigger_profile="momentum",
        require_1m_trigger=True,
        grid=tuple(param_grid(
            structure_window=[16, 24],
            min_score=[3.75, 4.0],
            min_score_delta=[0.50],
            stop_atr_mult=[0.8, 1.0],
            take_profit_r=[1.0, 1.2],
        )),
    ),
    "active": DailyScalperCadenceProfile(
        name="active",
        description="high-cadence directional 1m confirmation; must prove fees harder",
        trigger_profile="directional",
        require_1m_trigger=True,
        grid=tuple(param_grid(
            structure_window=[12, 16],
            min_score=[3.25, 3.50],
            min_score_delta=[0.25],
            stop_atr_mult=[0.7, 0.9],
            take_profit_r=[0.9, 1.1],
        )),
    ),
    "setup_only_diagnostic": DailyScalperCadenceProfile(
        name="setup_only_diagnostic",
        description="diagnostic only: removes 1m trigger to measure trigger bottleneck",
        trigger_profile="directional",
        require_1m_trigger=False,
        grid=tuple(param_grid(
            structure_window=[12, 16],
            min_score=[3.25, 3.50],
            min_score_delta=[0.25],
            stop_atr_mult=[0.7, 0.9],
            take_profit_r=[0.9, 1.1],
        )),
        shadow_refactor_eligible=False,
    ),
}


def default_candidates(*, max_candidates: int | None = None) -> tuple[DailyScalperCandidate, ...]:
    """Build the full configured daily-scalper research universe.

    This is still bounded by the repo's exchange/symbol environment variables,
    but it no longer leaves most pairs/families invisible to the daily scalper
    miner.
    """
    out: list[DailyScalperCandidate] = []
    for target in load_research_targets(timeframe=BASE_TIMEFRAME):
        for family in DAILY_SCALPER_FAMILIES:
            out.append(DailyScalperCandidate(target.exchange, target.symbol, family))
    return tuple(out[:max_candidates] if max_candidates is not None else out)


def run_daily_scalper_research(
    data_root: Path | str,
    *,
    candidates: Iterable[DailyScalperCandidate] | None = None,
    max_candidates: int | None = None,
    lookback_days: int = 120,
    train_days: int = 30,
    test_days: int = 7,
    require_1m_trigger: bool = True,
    profile: DailyScalperCadenceProfile | None = None,
) -> dict:
    store = ParquetStore(data_root)
    if profile is None:
        profile = DAILY_SCALPER_CADENCE_PROFILES["strict"]
        if require_1m_trigger != profile.require_1m_trigger:
            profile = DailyScalperCadenceProfile(
                name="strict_no_1m_trigger" if not require_1m_trigger else "strict",
                description=(
                    "legacy diagnostic strict profile without 1m trigger"
                    if not require_1m_trigger else profile.description
                ),
                trigger_profile=profile.trigger_profile,
                require_1m_trigger=require_1m_trigger,
                grid=profile.grid,
                max_holding_bars=profile.max_holding_bars,
                shadow_refactor_eligible=require_1m_trigger,
            )
    records: list[dict] = []
    candidate_list = (
        tuple(candidates)
        if candidates is not None else default_candidates(max_candidates=max_candidates)
    )
    if candidates is not None and max_candidates is not None:
        candidate_list = candidate_list[:max_candidates]
    for candidate in candidate_list:
        records.append(
            run_candidate(
                store,
                candidate,
                lookback_days=lookback_days,
                train_days=train_days,
                test_days=test_days,
                profile=profile,
            )
        )
    return build_daily_scalper_report(
        records,
        candidates=candidate_list,
        lookback_days=lookback_days,
        train_days=train_days,
        test_days=test_days,
        profile=profile,
    )


def run_daily_scalper_cadence_sweep(
    data_root: Path | str,
    *,
    candidates: Iterable[DailyScalperCandidate] | None = None,
    max_candidates: int | None = 24,
    lookback_days: int = 120,
    train_days: int = 30,
    test_days: int = 7,
    profiles: Iterable[DailyScalperCadenceProfile] | None = None,
    fast_smoke: bool = False,
) -> dict:
    """Backtest lane-refactor profiles for signal cadence and fee survival."""
    profile_list = tuple(profiles or DAILY_SCALPER_CADENCE_PROFILES.values())
    candidate_list = (
        tuple(candidates)
        if candidates is not None else default_candidates(max_candidates=max_candidates)
    )
    if candidates is not None and max_candidates is not None:
        candidate_list = candidate_list[:max_candidates]
    store = ParquetStore(data_root)
    records: list[dict] = []
    for candidate in candidate_list:
        for profile in profile_list:
            records.append(
                run_candidate(
                    store,
                    candidate,
                    lookback_days=lookback_days,
                    train_days=train_days,
                    test_days=test_days,
                    profile=profile,
                    fast_smoke=fast_smoke,
                )
            )
    return build_daily_scalper_cadence_report(
        records,
        candidates=candidate_list,
        profiles=profile_list,
        lookback_days=lookback_days,
        train_days=train_days,
        test_days=test_days,
        fast_smoke=fast_smoke,
    )


def run_candidate(
    store: ParquetStore,
    candidate: DailyScalperCandidate,
    *,
    lookback_days: int,
    train_days: int,
    test_days: int,
    profile: DailyScalperCadenceProfile | None = None,
    fast_smoke: bool = False,
) -> dict:
    profile = profile or DAILY_SCALPER_CADENCE_PROFILES["strict"]
    try:
        candles_15m = store.read_candles(
            candidate.exchange, candidate.symbol, BASE_TIMEFRAME
        )
        context_1h = store.read_candles(candidate.exchange, candidate.symbol, "1h")
        context_4h = store.read_candles(candidate.exchange, candidate.symbol, "4h")
        trigger_1m = store.read_candles(candidate.exchange, candidate.symbol, "1m")
    except FileNotFoundError as exc:
        return _untestable_record(candidate, f"missing data lane: {exc}", profile=profile)

    candles_15m = _window(candles_15m, lookback_days=lookback_days)
    if candles_15m.empty:
        return _untestable_record(
            candidate, "15m base lane has no rows in lookback window", profile=profile
        )

    latest = candles_15m["timestamp"].iloc[-1]
    context_cutoff = latest - pd.Timedelta(days=lookback_days + 40)
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
            profile=profile,
        )

    funding = _empty_funding_frame()
    config = BacktestConfig(max_holding_bars=profile.max_holding_bars)
    grid = list(profile.grid)

    def factory(**params):
        return DailyScalperPack(
            funding=funding,
            context_1h=context_1h,
            context_4h=context_4h,
            trigger_1m=trigger_1m,
            allowed_families=(candidate.family,),
            allowed_sides=candidate.allowed_sides,
            require_1m_trigger=profile.require_1m_trigger,
            trigger_profile=profile.trigger_profile,
            **params,
        )

    if fast_smoke:
        return _smoke_backtest_record(
            candidate,
            profile,
            candles_15m,
            funding,
            factory(**profile.baseline_params()),
            config,
            lookback_days=lookback_days,
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
        return _untestable_record(candidate, str(exc), profile=profile)

    record = wf_record(
        "daily_scalper_pack_v1",
        candidate.symbol,
        result,
        DAILY_SCALPER_GATES,
        gates_label="daily_scalper",
        exchange=candidate.exchange,
        timeframe=BASE_TIMEFRAME,
    )
    record.update(
        {
            "candidate_id": candidate.candidate_id,
            "candidate_family": candidate.family,
            "allowed_sides": list(candidate.allowed_sides),
            "base_timeframe": BASE_TIMEFRAME,
            "context_timeframes": list(CONTEXT_TIMEFRAMES),
            "trigger_timeframe": TRIGGER_TIMEFRAME,
            "cadence_profile": profile.name,
            "cadence_profile_description": profile.description,
            "cadence_profile_eligible_for_shadow_refactor": (
                profile.shadow_refactor_eligible
            ),
            "profile_grid_size": len(profile.grid),
            "trigger_profile": profile.trigger_profile,
            "route_policy": "research_only_maker_first_until_paper_proven",
            "can_trade": False,
            "can_promote": False,
            "requires_human_approval": True,
            "requires_untouched_judgment": True,
        }
    )
    record["signal_cadence"] = _signal_cadence(
        candles_15m,
        factory(**profile.baseline_params()),
        lookback_days=lookback_days,
    )
    return record


def build_daily_scalper_report(
    records: list[dict],
    *,
    candidates: tuple[DailyScalperCandidate, ...],
    lookback_days: int,
    train_days: int,
    test_days: int,
    profile: DailyScalperCadenceProfile,
) -> dict:
    leaderboard = build_edge_leaderboard(records)
    return {
        "updated": datetime.now(UTC).isoformat(),
        "strategy": "daily_scalper_pack_v1",
        "policy": {
            "can_trade": False,
            "can_promote": False,
            "research_only": True,
            "requires_untouched_judgment": True,
            "requires_human_approval": True,
            "route_policy": "maker-first; taker only after strong PF/payoff proof",
            "minimum_expectation": "net positive after fees in OOS walk-forward",
        },
        "flow": [
            "select_candidate_from_edge_leaderboard",
            "load_4h_1h_15m_1m_lanes",
            "train_on_15m_setups",
            "gate_by_4h_1h_context",
            "confirm_with_1m_trigger",
            "walk_forward_after_fees",
            "pre_register_untouched_judgment_if_candidate",
        ],
        "parameters": {
            "lookback_days": lookback_days,
            "train_days": train_days,
            "test_days": test_days,
            "base_timeframe": BASE_TIMEFRAME,
            "context_timeframes": list(CONTEXT_TIMEFRAMES),
            "trigger_timeframe": TRIGGER_TIMEFRAME,
            "require_1m_trigger": profile.require_1m_trigger,
            "cadence_profile": profile.to_dict(),
            "max_holding_bars_15m": profile.max_holding_bars,
            "gates": {
                "min_splits": DAILY_SCALPER_GATES.min_splits,
                "min_total_oos_trades": DAILY_SCALPER_GATES.min_total_oos_trades,
                "min_profit_factor": DAILY_SCALPER_GATES.min_profit_factor,
                "min_payoff_ratio": DAILY_SCALPER_GATES.min_payoff_ratio,
                "max_window_drawdown_pct": DAILY_SCALPER_GATES.max_window_drawdown_pct,
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
    }


def build_daily_scalper_cadence_report(
    records: list[dict],
    *,
    candidates: tuple[DailyScalperCandidate, ...],
    profiles: tuple[DailyScalperCadenceProfile, ...],
    lookback_days: int,
    train_days: int,
    test_days: int,
    fast_smoke: bool = False,
) -> dict:
    recommendations = _cadence_recommendations(records)
    return {
        "updated": datetime.now(UTC).isoformat(),
        "strategy": "daily_scalper_cadence_refactor_v1",
        "policy": {
            "can_trade": False,
            "can_promote": False,
            "research_only": True,
            "requires_untouched_judgment": True,
            "requires_human_approval": True,
            "purpose": (
                "find lane refactors that increase live signal cadence while "
                "still surviving fee-aware walk-forward"
            ),
            "fast_smoke": fast_smoke,
        },
        "flow": [
            "select_candidate_from_universe",
            "run_strict_balanced_active_trigger_profiles",
            "measure_raw_signal_cadence",
            "walk_forward_after_fees",
            "compare_against_strict_baseline",
            "recommend_shadow_refactor_only_if_more_cadence_and_positive_oos",
        ],
        "parameters": {
            "lookback_days": lookback_days,
            "train_days": train_days,
            "test_days": test_days,
            "base_timeframe": BASE_TIMEFRAME,
            "context_timeframes": list(CONTEXT_TIMEFRAMES),
            "trigger_timeframe": TRIGGER_TIMEFRAME,
            "fast_smoke": fast_smoke,
            "profiles": [p.to_dict() for p in profiles],
            "gates": {
                "min_splits": DAILY_SCALPER_GATES.min_splits,
                "min_total_oos_trades": DAILY_SCALPER_GATES.min_total_oos_trades,
                "min_profit_factor": DAILY_SCALPER_GATES.min_profit_factor,
                "min_payoff_ratio": DAILY_SCALPER_GATES.min_payoff_ratio,
                "max_window_drawdown_pct": DAILY_SCALPER_GATES.max_window_drawdown_pct,
            },
        },
        "candidates": [asdict(candidate) for candidate in candidates],
        "summary": _cadence_summary(records, recommendations),
        "recommendations": recommendations,
        "results": records,
        "note": (
            "Research verdict only. Recommendations are lane-refactor candidates; "
            "they do not enable paper, shadow, or live trading."
        ),
        "can_trade": False,
        "can_promote": False,
    }


def _summary(records: list[dict]) -> dict:
    testable = [r for r in records if r.get("verdict") != "UNTESTABLE"]
    passes = [r for r in records if r.get("verdict") == "PASS"]
    best = max(
        testable,
        key=lambda r: float(r.get("oos_net_usd", 0.0)),
        default=None,
    )
    return {
        "candidates": len(records),
        "testable": len(testable),
        "untestable": len(records) - len(testable),
        "passes": len(passes),
        "positive_after_fees": sum(
            1 for r in testable if float(r.get("oos_net_usd", 0.0)) > 0.0
        ),
        "best": {
            "candidate_id": best.get("candidate_id"),
            "exchange": best.get("exchange"),
            "symbol": best.get("symbol"),
            "family": best.get("candidate_family"),
            "verdict": best.get("verdict"),
            "oos_net_usd": best.get("oos_net_usd"),
            "oos_trades": best.get("oos_trades"),
            "profit_factor": best.get("profit_factor"),
        }
        if best
        else None,
    }


def _smoke_backtest_record(
    candidate: DailyScalperCandidate,
    profile: DailyScalperCadenceProfile,
    candles_15m: pd.DataFrame,
    funding: pd.DataFrame,
    strategy: DailyScalperPack,
    config: BacktestConfig,
    *,
    lookback_days: int,
) -> dict:
    result = run_backtest(
        candles_15m,
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

    return {
        "attribution": _side_attribution_from_trades(trades),
        "exchange": candidate.exchange,
        "gates": "cadence_smoke",
        "strategy": "daily_scalper_pack_v1",
        "symbol": candidate.symbol,
        "timeframe": BASE_TIMEFRAME,
        "windows": 1,
        "traded_windows": 1 if metrics.num_trades else 0,
        "oos_trades": metrics.num_trades,
        "oos_net_usd": round(metrics.net_profit_usd, 2),
        "profitable_windows_pct": 100.0 if metrics.net_profit_usd > 0 else 0.0,
        "total_fees_usd": round(metrics.total_fees_usd, 2),
        "profit_factor": profit_factor,
        "payoff_ratio": payoff,
        "max_consecutive_stops": 0,
        "verdict": "SMOKE_POSITIVE" if not reasons else "SMOKE_REJECT",
        "reasons": reasons,
        "updated": datetime.now(UTC).isoformat(),
        "candidate_id": candidate.candidate_id,
        "candidate_family": candidate.family,
        "allowed_sides": list(candidate.allowed_sides),
        "base_timeframe": BASE_TIMEFRAME,
        "context_timeframes": list(CONTEXT_TIMEFRAMES),
        "trigger_timeframe": TRIGGER_TIMEFRAME,
        "cadence_profile": profile.name,
        "cadence_profile_description": profile.description,
        "cadence_profile_eligible_for_shadow_refactor": profile.shadow_refactor_eligible,
        "profile_grid_size": len(profile.grid),
        "trigger_profile": profile.trigger_profile,
        "route_policy": "research_only_maker_first_until_paper_proven",
        "signal_cadence": _signal_cadence(
            candles_15m,
            strategy,
            lookback_days=lookback_days,
        ),
        "cadence_smoke_backtest": True,
        "can_trade": False,
        "can_promote": False,
        "requires_human_approval": True,
        "requires_untouched_judgment": True,
    }


def _side_attribution_from_trades(trades: tuple) -> dict:
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


def _cadence_summary(records: list[dict], recommendations: list[dict]) -> dict:
    testable = [r for r in records if r.get("verdict") != "UNTESTABLE"]
    return {
        "candidates": len({r.get("candidate_id") for r in records}),
        "profile_rows": len(records),
        "testable_rows": len(testable),
        "untestable_rows": len(records) - len(testable),
        "passes": sum(1 for r in testable if r.get("verdict") == "PASS"),
        "positive_after_fees": sum(
            1 for r in testable if float(r.get("oos_net_usd", 0.0)) > 0.0
        ),
        "refactor_candidates": sum(
            1 for r in recommendations
            if r.get("action") in {
                "PRE_REGISTER_UNTOUCHED_JUDGMENT",
                "SHADOW_REFACTOR_CANDIDATE",
            }
        ),
        "do_not_refactor": sum(
            1 for r in recommendations if r.get("action") == "DO_NOT_WIDEN"
        ),
        "best": recommendations[0] if recommendations else None,
    }


def _cadence_recommendations(records: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for record in records:
        grouped.setdefault(str(record.get("candidate_id") or ""), []).append(record)

    out: list[dict] = []
    for candidate_id, rows in grouped.items():
        if not candidate_id:
            continue
        strict = next(
            (row for row in rows if row.get("cadence_profile") == "strict"),
            rows[0],
        )
        eligible = [
            row for row in rows
            if row.get("verdict") != "UNTESTABLE"
            and bool(row.get("cadence_profile_eligible_for_shadow_refactor", True))
        ]
        diagnostic = [
            row for row in rows
            if not bool(row.get("cadence_profile_eligible_for_shadow_refactor", True))
        ]
        best = max(eligible, key=_cadence_rank, default=None)
        if best is None:
            best = max(rows, key=_raw_signal_rank)
        strict_trades = int(strict.get("oos_trades") or 0)
        best_trades = int(best.get("oos_trades") or 0)
        strict_raw = _raw_signal_count(strict)
        best_raw = _raw_signal_count(best)
        best_net = float(best.get("oos_net_usd") or 0.0)
        action = "KEEP_STRICT_OR_RECORD_MORE"
        reasons: list[str] = []
        if best.get("verdict") == "PASS" and best_trades > strict_trades:
            action = "PRE_REGISTER_UNTOUCHED_JUDGMENT"
            reasons.append("profile passes gates and improves OOS trade cadence")
        elif (
            best_net > 0
            and best_trades > strict_trades
            and float(best.get("profit_factor") or 0.0) >= 1.0
        ):
            action = "SHADOW_REFACTOR_CANDIDATE"
            reasons.append("more OOS trades with positive post-fee net")
        elif best_trades > strict_trades and best_net <= 0:
            action = "DO_NOT_WIDEN"
            reasons.append("extra trades do not clear the fee wall")
        elif strict_raw == 0 and best_raw > 0 and best_net > 0:
            action = "SHADOW_REFACTOR_CANDIDATE"
            reasons.append("strict profile is silent; refactor profile has positive OOS")
        else:
            reasons.append("no eligible profile improved both cadence and fee-adjusted edge")

        setup_only = max(diagnostic, key=_raw_signal_rank, default=None)
        if (
            setup_only is not None
            and _raw_signal_count(setup_only) > max(3 * max(strict_raw, 1), 10)
        ):
            reasons.append(
                "setup-only diagnostic fires much more often; 1m confirmation is a bottleneck"
            )

        out.append({
            "candidate_id": candidate_id,
            "exchange": best.get("exchange"),
            "symbol": best.get("symbol"),
            "family": best.get("candidate_family"),
            "action": action,
            "best_profile": _compact_profile_result(best),
            "strict_profile": _compact_profile_result(strict),
            "cadence_uplift_vs_strict": (
                round(best_trades / strict_trades, 2) if strict_trades else None
            ),
            "raw_signal_uplift_vs_strict": (
                round(best_raw / strict_raw, 2) if strict_raw else None
            ),
            "reasons": reasons,
            "can_trade": False,
            "can_promote": False,
        })
    out.sort(key=_recommendation_rank)
    return out


def _cadence_rank(record: dict) -> tuple:
    net = float(record.get("oos_net_usd") or 0.0)
    trades = int(record.get("oos_trades") or 0)
    profit_factor = float(record.get("profit_factor") or 0.0)
    signals_per_day = float(
        (record.get("signal_cadence") or {}).get("signals_per_day") or 0.0
    )
    return (
        record.get("verdict") == "PASS",
        net > 0,
        trades >= DAILY_SCALPER_GATES.min_total_oos_trades,
        round(profit_factor, 4),
        round(net, 4),
        trades,
        round(signals_per_day, 4),
    )


def _raw_signal_rank(record: dict) -> tuple:
    return (_raw_signal_count(record), int(record.get("oos_trades") or 0))


def _recommendation_rank(record: dict) -> tuple:
    action_rank = {
        "PRE_REGISTER_UNTOUCHED_JUDGMENT": 0,
        "SHADOW_REFACTOR_CANDIDATE": 1,
        "KEEP_STRICT_OR_RECORD_MORE": 2,
        "DO_NOT_WIDEN": 3,
    }.get(str(record.get("action")), 4)
    best = record.get("best_profile") or {}
    return (
        action_rank,
        -float(best.get("oos_net_usd") or 0.0),
        -int(best.get("oos_trades") or 0),
    )


def _compact_profile_result(record: dict) -> dict:
    cadence = record.get("signal_cadence") or {}
    return {
        "profile": record.get("cadence_profile"),
        "verdict": record.get("verdict"),
        "oos_net_usd": record.get("oos_net_usd"),
        "oos_trades": record.get("oos_trades"),
        "profit_factor": record.get("profit_factor"),
        "payoff_ratio": record.get("payoff_ratio"),
        "signals_per_day": cadence.get("signals_per_day"),
        "raw_signals": cadence.get("raw_signals"),
        "trigger_profile": record.get("trigger_profile"),
        "eligible_for_shadow_refactor": record.get(
            "cadence_profile_eligible_for_shadow_refactor"
        ),
        "reasons": list(record.get("reasons", []))[:3],
    }


def _raw_signal_count(record: dict) -> int:
    return int((record.get("signal_cadence") or {}).get("raw_signals") or 0)


def _signal_cadence(
    candles: pd.DataFrame,
    strategy: DailyScalperPack,
    *,
    lookback_days: int,
) -> dict:
    try:
        df = strategy.prepare(candles).reset_index(drop=True)
    except Exception as exc:  # noqa: BLE001 - diagnostic only
        return {"raw_signals": 0, "signals_per_day": 0.0, "error": str(exc)}

    start = max(strategy.warmup_bars, 1)
    end = max(start, len(df) - 1)  # a signal needs a next bar open to fill
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


def _span_days(df: pd.DataFrame, *, fallback: int) -> float:
    if len(df) < 2:
        return float(fallback)
    delta = df["timestamp"].iloc[-1] - df["timestamp"].iloc[0]
    days = delta.total_seconds() / 86_400
    return max(days, 1e-9)


def _untestable_record(
    candidate: DailyScalperCandidate,
    reason: str,
    *,
    profile: DailyScalperCadenceProfile | None = None,
) -> dict:
    profile = profile or DAILY_SCALPER_CADENCE_PROFILES["strict"]
    return {
        "exchange": candidate.exchange,
        "symbol": candidate.symbol,
        "timeframe": BASE_TIMEFRAME,
        "strategy": "daily_scalper_pack_v1",
        "candidate_id": candidate.candidate_id,
        "candidate_family": candidate.family,
        "allowed_sides": list(candidate.allowed_sides),
        "base_timeframe": BASE_TIMEFRAME,
        "context_timeframes": list(CONTEXT_TIMEFRAMES),
        "trigger_timeframe": TRIGGER_TIMEFRAME,
        "cadence_profile": profile.name,
        "cadence_profile_description": profile.description,
        "cadence_profile_eligible_for_shadow_refactor": (
            profile.shadow_refactor_eligible
        ),
        "profile_grid_size": len(profile.grid),
        "trigger_profile": profile.trigger_profile,
        "signal_cadence": {
            "raw_signals": 0,
            "signals_per_day": 0.0,
            "bars_scanned": 0,
        },
        "route_policy": "research_only_maker_first_until_paper_proven",
        "windows": 0,
        "traded_windows": 0,
        "oos_trades": 0,
        "oos_net_usd": 0.0,
        "profitable_windows_pct": 0.0,
        "total_fees_usd": 0.0,
        "profit_factor": 0.0,
        "payoff_ratio": 0.0,
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


def parse_candidate(value: str) -> DailyScalperCandidate:
    parts = value.split("|")
    if len(parts) not in {3, 4}:
        raise argparse.ArgumentTypeError(
            "candidate must be exchange|symbol|family or exchange|symbol|family|side1,side2"
        )
    sides = (
        tuple(side.strip() for side in parts[3].split(",") if side.strip())
        if len(parts) == 4
        else ("long", "short")
    )
    return DailyScalperCandidate(parts[0], parts[1], parts[2], sides)


def parse_profile(value: str) -> DailyScalperCadenceProfile:
    try:
        return DAILY_SCALPER_CADENCE_PROFILES[value]
    except KeyError:
        raise argparse.ArgumentTypeError(
            "profile must be one of "
            + ", ".join(sorted(DAILY_SCALPER_CADENCE_PROFILES))
        ) from None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run VNEDGE daily scalper research")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--out", default=DEFAULT_DAILY_SCALPER_OUT)
    parser.add_argument("--lookback-days", type=int, default=120)
    parser.add_argument("--train-days", type=int, default=30)
    parser.add_argument("--test-days", type=int, default=7)
    parser.add_argument("--max-candidates", type=int, default=None)
    parser.add_argument("--interval-seconds", type=int, default=0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--no-1m-trigger",
        action="store_true",
        help="diagnostic only: allow setup without 1m confirmation when 1m lane is sparse",
    )
    parser.add_argument(
        "--cadence-sweep",
        action="store_true",
        help=(
            "compare strict/balanced/active lane-refactor profiles and publish "
            "daily_scalper_cadence_latest.json by default"
        ),
    )
    parser.add_argument(
        "--fast-smoke",
        action="store_true",
        help=(
            "with --cadence-sweep, run one baseline-parameter fee-aware "
            "backtest per profile instead of full walk-forward"
        ),
    )
    parser.add_argument(
        "--profile",
        type=parse_profile,
        action="append",
        default=None,
        help=(
            "cadence profile to run "
            f"({', '.join(sorted(DAILY_SCALPER_CADENCE_PROFILES))}); "
            "repeatable with --cadence-sweep"
        ),
    )
    parser.add_argument(
        "--trigger-profile",
        choices=TRIGGER_PROFILES,
        default=None,
        help="diagnostic alias: run the matching built-in cadence profile when possible",
    )
    parser.add_argument(
        "--candidate",
        type=parse_candidate,
        action="append",
        default=None,
        help="exchange|symbol|family or exchange|symbol|family|long,short",
    )
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
    profiles = _profiles_from_args(args)
    if args.cadence_sweep:
        max_candidates = args.max_candidates if args.max_candidates is not None else 24
        report = run_daily_scalper_cadence_sweep(
            args.data_root,
            candidates=tuple(args.candidate) if args.candidate else None,
            max_candidates=max_candidates,
            lookback_days=args.lookback_days,
            train_days=args.train_days,
            test_days=args.test_days,
            profiles=profiles or None,
            fast_smoke=args.fast_smoke,
        )
        out = Path(
            DEFAULT_CADENCE_OUT if args.out == DEFAULT_DAILY_SCALPER_OUT else args.out
        )
    else:
        profile = profiles[0] if profiles else None
        report = run_daily_scalper_research(
            args.data_root,
            candidates=tuple(args.candidate) if args.candidate else None,
            max_candidates=args.max_candidates,
            lookback_days=args.lookback_days,
            train_days=args.train_days,
            test_days=args.test_days,
            require_1m_trigger=not args.no_1m_trigger,
            profile=profile,
        )
        out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(report, indent=2, default=str))
    tmp.replace(out)
    return report


def _profiles_from_args(args: argparse.Namespace) -> tuple[DailyScalperCadenceProfile, ...]:
    profiles = tuple(args.profile or ())
    if args.trigger_profile is None:
        return profiles
    by_trigger = {
        profile.trigger_profile: profile
        for profile in DAILY_SCALPER_CADENCE_PROFILES.values()
        if profile.shadow_refactor_eligible
    }
    selected = by_trigger.get(args.trigger_profile)
    if selected is None:
        return profiles
    return profiles + (() if selected in profiles else (selected,))


def _format_summary(report: dict) -> str:
    if report.get("strategy") == "daily_scalper_cadence_refactor_v1":
        return _format_cadence_summary(report)
    lines = [
        "=== Daily scalper pack ===",
        f"updated: {report['updated']}",
        (
            "summary: "
            f"{report['summary']['passes']} pass, "
            f"{report['summary']['positive_after_fees']} positive-after-fees, "
            f"{report['summary']['untestable']} untestable"
        ),
    ]
    for record in report["results"]:
        reasons = "; ".join(record.get("reasons", [])[:2]) or "ok"
        lines.append(
            f"  {record['exchange']} {record['symbol']} {record['candidate_family']}: "
            f"{record['verdict']} net=${float(record.get('oos_net_usd', 0.0)):+.2f} "
            f"trades={record.get('oos_trades', 0)} "
            f"pf={float(record.get('profit_factor', 0.0)):.2f} — {reasons}"
        )
    lines.append("output is research-only; no paper/shadow/live state changed")
    return "\n".join(lines)


def _format_cadence_summary(report: dict) -> str:
    summary = report["summary"]
    lines = [
        "=== Daily scalper cadence refactor ===",
        f"updated: {report['updated']}",
        (
            "summary: "
            f"{summary['refactor_candidates']} refactor candidate(s), "
            f"{summary['positive_after_fees']} positive profile rows, "
            f"{summary['do_not_refactor']} do-not-widen"
        ),
    ]
    for row in report.get("recommendations", [])[:20]:
        best = row.get("best_profile") or {}
        lines.append(
            f"  {row['action']:<30} {row.get('exchange')} {row.get('symbol')} "
            f"{row.get('family')} profile={best.get('profile')} "
            f"net=${float(best.get('oos_net_usd') or 0.0):+.2f} "
            f"trades={best.get('oos_trades')} "
            f"signals/day={float(best.get('signals_per_day') or 0.0):.2f}"
        )
    lines.append("output is research-only; no paper/shadow/live state changed")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
