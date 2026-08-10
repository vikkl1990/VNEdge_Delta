"""Fee-aware edge leaderboard and candidate queue.

This is a research/governance view only. It ranks rolling walk-forward rows and
Quant family attributions so operators can decide what deserves untouched-data
judgment next. It never promotes, never places orders, and never bypasses the
mode ladder.

Live shadow evidence: when a ``shadow_perf`` payload (from
``vnedge.research.shadow_perf_reader``) is supplied, strategy rows gain a
``live_shadow`` sub-dict with the lane's virtual track record and — at
``LIVE_SHADOW_MIN_TRADES`` or more virtual trades — an annotation:
``LIVE_SHADOW_POSITIVE`` (positive virtual net) or ``LIVE_SHADOW_NEGATIVE``
(an honest demotion signal for the human). The annotation adjusts the ranking
score by ``LIVE_SHADOW_SCORE_BONUS`` in the matching direction but NEVER
changes a promotion tier, never promotes, and never flips can_trade /
can_promote — it is evidence for the human, not a gate.
"""

from __future__ import annotations

import math
from typing import Iterable

from vnedge.research.optimizer_scorecard import (
    OptimizerScorecardConfig,
    build_optimizer_scorecard,
    optimizer_scorecard_policy,
)
from vnedge.research.shadow_perf_reader import index_shadow_perf, shadow_perf_key
from vnedge.scalping.parameter_registry import (
    DEFAULT_SCALPER_PARAMETER_REGISTRY,
    ScalperParameterRegistry,
)

MIN_CANDIDATE_TRADES = 10

# Live shadow annotation thresholds: below the minimum the track record is
# shown but not annotated (too little evidence either way). The score bonus
# is a ranking nudge only — annotation, not promotion.
LIVE_SHADOW_MIN_TRADES = 5
LIVE_SHADOW_SCORE_BONUS = 6.0
LIVE_SHADOW_POSITIVE = "LIVE_SHADOW_POSITIVE"
LIVE_SHADOW_NEGATIVE = "LIVE_SHADOW_NEGATIVE"


def build_edge_leaderboard(
    records: Iterable[dict],
    *,
    registry: ScalperParameterRegistry = DEFAULT_SCALPER_PARAMETER_REGISTRY,
    max_rows: int = 50,
    max_queue: int = 20,
    shadow_perf: dict | None = None,
    judgment_records: Iterable[dict] | None = None,
) -> dict:
    """Build a ranked research leaderboard from rolling records.

    Strategy rows are ranked directly. Quant rows with ``family_attribution``
    also emit family-probe rows, which can become isolated-variant candidates
    but never direct promotion candidates. ``shadow_perf`` (the
    shadow_perf_reader payload) joins each strategy row to its live shadow
    lane's virtual track record — annotation-only evidence, never a gate.
    """
    shadow_index = index_shadow_perf(shadow_perf)
    judgment_index = _latest_judgments(judgment_records or ())
    rows: list[dict] = []
    for record in records:
        strategy_row = _row_from_record(
            record,
            registry=registry,
            shadow_index=shadow_index,
            judgment_index=judgment_index,
        )
        if strategy_row is not None:
            rows.append(strategy_row)
        for family, stats in sorted(record.get("family_attribution", {}).items()):
            family_row = _row_from_record(
                record,
                registry=registry,
                family=family,
                family_stats=stats,
                shadow_index=shadow_index,
                judgment_index=judgment_index,
            )
            if family_row is not None:
                rows.append(family_row)

    rows.sort(key=_rank_key)
    ranked_rows = []
    for idx, row in enumerate(rows[:max_rows], start=1):
        ranked_rows.append({**row, "rank": idx})

    queue = [_queue_entry(row) for row in ranked_rows if _queued(row)]
    return {
        "policy": _policy(registry),
        "summary": _summary(ranked_rows, queue),
        "rows": ranked_rows,
        "promotion_queue": queue[:max_queue],
    }


def _row_from_record(
    record: dict,
    *,
    registry: ScalperParameterRegistry,
    family: str | None = None,
    family_stats: dict | None = None,
    shadow_index: dict[str, dict] | None = None,
    judgment_index: dict[tuple[str, str, str], dict] | None = None,
) -> dict | None:
    verdict = str(record.get("verdict", "REJECT"))
    if verdict == "UNTESTABLE":
        return None

    stats = family_stats or record
    trades = int(stats.get("trades", record.get("oos_trades", 0)) or 0)
    net = _finite_float(stats.get("net_usd", record.get("oos_net_usd", 0.0)))
    if trades <= 0 and net == 0.0 and verdict != "PASS":
        return None

    exchange = str(record.get("exchange", "binanceusdm"))
    symbol = str(record.get("symbol", ""))
    timeframe = str(record.get("timeframe", "1h"))
    strategy = str(record.get("strategy", ""))
    scope = "family" if family else "strategy"
    lane_id = "|".join(
        part for part in (exchange, symbol, timeframe, strategy, family or "") if part
    )
    fees = _finite_float(
        stats.get("total_fees_usd", stats.get("fees_usd", record.get("total_fees_usd", 0.0)))
    )
    profit_factor = _finite_float(stats.get("profit_factor", record.get("profit_factor", 0.0)))
    payoff = _finite_float(stats.get("payoff_ratio", record.get("payoff_ratio", 0.0)))
    route, blockers = _route_and_blockers(
        exchange=exchange,
        registry=registry,
        net=net,
        fees=fees,
        profit_factor=profit_factor,
        payoff=payoff,
        trades=trades,
    )
    execution_truth = _execution_truth_view(record.get("execution_truth"))
    truth_annotation = _execution_truth_annotation(execution_truth)
    if truth_annotation in {
        "TRUTH_UNDER_SAMPLED",
        "TRUTH_LOW_FILL_CONFIDENCE",
        "TRUTH_NEGATIVE_AFTER_COST",
        "TRUTH_NO_EVENTS",
    }:
        blockers = [*blockers, truth_annotation.lower()]
        route = "BLOCKED"
    elif truth_annotation == "TRUTH_TAKER_EDGE":
        route = "TAKER_ALLOWED"
    elif truth_annotation == "TRUTH_MAKER_EDGE" and route != "BLOCKED":
        route = "MAKER_ONLY"
    latest_judgment = None
    if family is None and judgment_index:
        latest_judgment = judgment_index.get((exchange, symbol, strategy))
        if latest_judgment and latest_judgment.get("verdict") == "REJECT":
            blockers = [*blockers, "latest_untouched_judgment_rejected"]
            route = "BLOCKED"
    tier = _promotion_tier(
        scope=scope,
        verdict=verdict,
        auto=bool(record.get("auto")),
        route=route,
        net=net,
        blockers=blockers,
    )
    fee_multiple = round(net / fees, 2) if fees > 0 else None
    avg_net = round(net / trades, 4) if trades else 0.0
    fee_drag = _fee_drag_pct(net, fees)
    gate = registry.family("book_imbalance_continuation").route_gate
    optimizer_fitness = build_optimizer_scorecard(
        net_usd=net,
        trades=trades,
        fees_usd=fees,
        profit_factor=profit_factor,
        payoff_ratio=payoff,
        profitable_windows_pct=_finite_float(record.get("profitable_windows_pct", 0.0)),
        config=OptimizerScorecardConfig(
            min_trades=MIN_CANDIDATE_TRADES,
            min_profit_factor=gate.maker_min_profit_factor,
        ),
    )
    # Live shadow evidence joins STRATEGY rows only: a family probe shares its
    # parent's shadow lane, so attributing the whole lane's virtual PnL to one
    # family would fabricate evidence.
    live_shadow = None
    if family is None and shadow_index:
        live_shadow = _live_shadow_view(
            shadow_index.get(shadow_perf_key(strategy, exchange, symbol))
        )
    live_shadow_annotation = _live_shadow_annotation(live_shadow)
    score = _score(
        verdict=verdict,
        tier=tier,
        route=route,
        net=net,
        trades=trades,
        profit_factor=profit_factor,
        payoff=payoff,
        fee_multiple=fee_multiple,
        profitable_windows_pct=_finite_float(record.get("profitable_windows_pct", 0.0)),
    )
    if live_shadow_annotation == LIVE_SHADOW_POSITIVE:
        score = round(score + LIVE_SHADOW_SCORE_BONUS, 2)
    elif live_shadow_annotation == LIVE_SHADOW_NEGATIVE:
        score = round(score - LIVE_SHADOW_SCORE_BONUS, 2)
    if truth_annotation in {"TRUTH_MAKER_EDGE", "TRUTH_TAKER_EDGE"}:
        score = round(score + 4.0, 2)
    elif truth_annotation:
        score = round(score - 4.0, 2)
    candidate_id = (
        f"{strategy}__{family}_only"
        if family and strategy == "quant_signal_pack_v1"
        else strategy
    )
    return {
        "lane_id": lane_id,
        "scope": scope,
        "exchange": exchange,
        "symbol": symbol,
        "timeframe": timeframe,
        "strategy": strategy,
        "candidate_id": candidate_id,
        "family": family,
        "parent_strategy": strategy if family else record.get("parent"),
        "auto": bool(record.get("auto")),
        "verdict": verdict,
        "promotion_tier": tier,
        "route_decision": route,
        "score": score,
        "oos_net_usd": round(net, 2),
        "oos_trades": trades,
        "avg_net_usd_per_trade": avg_net,
        "total_fees_usd": round(fees, 2),
        "net_fee_multiple": fee_multiple,
        "fee_drag_pct": fee_drag,
        "profit_factor": profit_factor,
        "payoff_ratio": payoff,
        "profitable_windows_pct": _finite_float(record.get("profitable_windows_pct", 0.0)),
        "optimizer_fitness": optimizer_fitness,
        "gates": record.get("gates", "standard"),
        "blockers": blockers,
        "latest_judgment": latest_judgment,
        "live_shadow": live_shadow,
        "live_shadow_annotation": live_shadow_annotation,
        "execution_truth": execution_truth,
        "execution_truth_annotation": truth_annotation,
        "can_trade": False,
        "can_promote": False,
        "requires_human_approval": True,
        "requires_untouched_judgment": True,
    }


def _live_shadow_view(lane: dict | None) -> dict | None:
    """Compact live-shadow evidence for a leaderboard row (or None)."""
    if not lane:
        return None
    return {
        "virtual_trades": int(lane.get("virtual_trades", 0) or 0),
        "net_usd": _finite_float(lane.get("net_usd", 0.0)),
        "profit_factor": (
            _finite_float(lane["profit_factor"])
            if lane.get("profit_factor") is not None
            else None
        ),
        "win_rate_pct": _finite_float(lane.get("win_rate_pct", 0.0)),
        "span_days": _finite_float(lane.get("span_days", 0.0)),
        "last_resolution_ts": lane.get("last_resolution_ts"),
    }


def _live_shadow_annotation(live_shadow: dict | None) -> str | None:
    """Annotation only — never a tier change, never a promotion."""
    if not live_shadow or live_shadow["virtual_trades"] < LIVE_SHADOW_MIN_TRADES:
        return None
    if live_shadow["net_usd"] > 0:
        return LIVE_SHADOW_POSITIVE
    if live_shadow["net_usd"] < 0:
        return LIVE_SHADOW_NEGATIVE
    return None


def _execution_truth_view(raw: object) -> dict | None:
    """Compact execution-truth summary from execution_edge_labeler output.

    Records may attach either the full report under ``execution_truth`` or just
    the report's ``summary``.  The view is intentionally small so the
    leaderboard can join proof without becoming a second labeler.
    """
    if not isinstance(raw, dict):
        return None
    summary = raw.get("summary") if isinstance(raw.get("summary"), dict) else raw
    if not isinstance(summary, dict):
        return None
    verdict = str(summary.get("verdict") or "")
    if not verdict:
        return None
    return {
        "verdict": verdict,
        "samples": int(summary.get("samples", 0) or 0),
        "executable_samples": int(summary.get("executable_samples", 0) or 0),
        "positive_net_samples": int(summary.get("positive_net_samples", 0) or 0),
        "avg_net_bps": (
            _finite_float(summary["avg_net_bps"])
            if summary.get("avg_net_bps") is not None
            else None
        ),
        "profit_factor": (
            _finite_float(summary["profit_factor"])
            if summary.get("profit_factor") is not None
            else None
        ),
        "avg_fill_probability": (
            _finite_float(summary["avg_fill_probability"])
            if summary.get("avg_fill_probability") is not None
            else None
        ),
        "primary_blocker": str(summary.get("primary_blocker") or ""),
    }


def _execution_truth_annotation(execution_truth: dict | None) -> str | None:
    if not execution_truth:
        return None
    verdict = execution_truth.get("verdict")
    return {
        "MAKER_EDGE": "TRUTH_MAKER_EDGE",
        "TAKER_EDGE": "TRUTH_TAKER_EDGE",
        "UNDER_SAMPLED": "TRUTH_UNDER_SAMPLED",
        "LOW_FILL_CONFIDENCE": "TRUTH_LOW_FILL_CONFIDENCE",
        "NEGATIVE_AFTER_COST": "TRUTH_NEGATIVE_AFTER_COST",
        "NO_EVENTS": "TRUTH_NO_EVENTS",
    }.get(str(verdict))


def _route_and_blockers(
    *,
    exchange: str,
    registry: ScalperParameterRegistry,
    net: float,
    fees: float,
    profit_factor: float,
    payoff: float,
    trades: int,
) -> tuple[str, list[str]]:
    gate = registry.family("book_imbalance_continuation").route_gate
    blockers: list[str] = []
    if trades < MIN_CANDIDATE_TRADES:
        blockers.append("too_few_trades")
    if net <= 0:
        blockers.append("net_not_positive_after_fees")
    if profit_factor < gate.maker_min_profit_factor:
        blockers.append("below_maker_profit_factor")

    if blockers:
        return "BLOCKED", blockers

    fee = registry.fee_profile(exchange)
    taker_ready = (
        profit_factor >= gate.taker_min_profit_factor
        and payoff >= 1.8
        and (fees <= 0 or net / fees >= 0.5)
    )
    if taker_ready:
        return "TAKER_ALLOWED", []
    if fee.maker_first_cost_bps < fee.taker_round_trip_cost_bps:
        return "MAKER_ONLY", []
    return "MAKER_ONLY", []


def _promotion_tier(
    *,
    scope: str,
    verdict: str,
    auto: bool,
    route: str,
    net: float,
    blockers: list[str],
) -> str:
    if route == "BLOCKED" or blockers:
        return "BLOCKED"
    if scope == "family":
        return "VARIANT_RESEARCH_READY"
    if verdict == "PASS" and auto:
        return "AUTO_PASS_REVIEW"
    if verdict == "PASS":
        return "JUDGMENT_READY"
    if net > 0:
        return "WATCHLIST"
    return "BLOCKED"


def _queued(row: dict) -> bool:
    return row["promotion_tier"] in {
        "JUDGMENT_READY",
        "AUTO_PASS_REVIEW",
        "VARIANT_RESEARCH_READY",
        "WATCHLIST",
    }


def _queue_entry(row: dict) -> dict:
    next_step = {
        "JUDGMENT_READY": "pre_register_untouched_judgment",
        "AUTO_PASS_REVIEW": "human_review_auto_variant_then_pre_register",
        "VARIANT_RESEARCH_READY": "run_isolated_family_variant",
        "WATCHLIST": "collect_more_and_retest",
    }[row["promotion_tier"]]
    return {
        "queue_id": f"edge_queue|{row['lane_id']}",
        "rank": row["rank"],
        "promotion_tier": row["promotion_tier"],
        "next_step": next_step,
        "route_decision": row["route_decision"],
        "candidate_id": row["candidate_id"],
        "exchange": row["exchange"],
        "symbol": row["symbol"],
        "timeframe": row["timeframe"],
        "strategy": row["strategy"],
        "family": row["family"],
        "score": row["score"],
        "oos_net_usd": row["oos_net_usd"],
        "oos_trades": row["oos_trades"],
        "profit_factor": row["profit_factor"],
        "payoff_ratio": row["payoff_ratio"],
        "optimizer_fitness": row["optimizer_fitness"],
        "live_shadow": row["live_shadow"],
        "live_shadow_annotation": row["live_shadow_annotation"],
        "execution_truth": row["execution_truth"],
        "execution_truth_annotation": row["execution_truth_annotation"],
        "latest_judgment": row["latest_judgment"],
        "can_trade": False,
        "can_promote": False,
        "requires_human_approval": True,
        "requires_untouched_judgment": True,
    }


def _score(
    *,
    verdict: str,
    tier: str,
    route: str,
    net: float,
    trades: int,
    profit_factor: float,
    payoff: float,
    fee_multiple: float | None,
    profitable_windows_pct: float,
) -> float:
    score = 0.0
    score += min(max(profit_factor - 1.0, 0.0), 2.0) * 18.0
    score += min(max(payoff, 0.0), 3.0) / 3.0 * 15.0
    score += min(max(trades, 0), 60) / 60.0 * 15.0
    score += min(max(profitable_windows_pct, 0.0), 100.0) / 100.0 * 10.0
    score += min(max(net, 0.0), 100.0) / 100.0 * 15.0
    if fee_multiple is not None:
        score += min(max(fee_multiple, 0.0), 3.0) / 3.0 * 10.0
    score += 12.0 if verdict == "PASS" else 0.0
    score += 5.0 if tier in {"VARIANT_RESEARCH_READY", "AUTO_PASS_REVIEW"} else 0.0
    score += 3.0 if route == "TAKER_ALLOWED" else 1.0 if route == "MAKER_ONLY" else 0.0
    return round(score, 2)


def _rank_key(row: dict) -> tuple:
    tier_rank = {
        "JUDGMENT_READY": 0,
        "AUTO_PASS_REVIEW": 1,
        "VARIANT_RESEARCH_READY": 2,
        "WATCHLIST": 3,
        "BLOCKED": 4,
    }
    route_rank = {"TAKER_ALLOWED": 0, "MAKER_ONLY": 1, "BLOCKED": 2}
    return (
        tier_rank.get(row["promotion_tier"], 9),
        route_rank.get(row["route_decision"], 9),
        -float(row["score"]),
        -float(row["oos_net_usd"]),
        row["exchange"],
        row["symbol"],
        row["strategy"],
        row.get("family") or "",
    )


def _summary(rows: list[dict], queue: list[dict]) -> dict:
    return {
        "rows": len(rows),
        "queued": len(queue),
        "judgment_ready": sum(1 for r in rows if r["promotion_tier"] == "JUDGMENT_READY"),
        "variant_ready": sum(1 for r in rows if r["promotion_tier"] == "VARIANT_RESEARCH_READY"),
        "watchlist": sum(1 for r in rows if r["promotion_tier"] == "WATCHLIST"),
        "blocked": sum(1 for r in rows if r["promotion_tier"] == "BLOCKED"),
        "judgment_rejected": sum(
            1 for r in rows
            if (r.get("latest_judgment") or {}).get("verdict") == "REJECT"
        ),
        "maker_only": sum(1 for r in rows if r["route_decision"] == "MAKER_ONLY"),
        "taker_allowed": sum(1 for r in rows if r["route_decision"] == "TAKER_ALLOWED"),
        "live_shadow_tracked": sum(1 for r in rows if r["live_shadow"] is not None),
        "live_shadow_positive": sum(
            1 for r in rows if r["live_shadow_annotation"] == LIVE_SHADOW_POSITIVE
        ),
        "live_shadow_negative": sum(
            1 for r in rows if r["live_shadow_annotation"] == LIVE_SHADOW_NEGATIVE
        ),
        "execution_truth_tracked": sum(1 for r in rows if r["execution_truth"] is not None),
        "execution_truth_positive": sum(
            1 for r in rows
            if r["execution_truth_annotation"] in {"TRUTH_MAKER_EDGE", "TRUTH_TAKER_EDGE"}
        ),
        "execution_truth_blocked": sum(
            1 for r in rows
            if str(r.get("execution_truth_annotation") or "").startswith("TRUTH_")
            and r["execution_truth_annotation"]
            not in {"TRUTH_MAKER_EDGE", "TRUTH_TAKER_EDGE"}
        ),
        "optimizer_hard_filters_passed": sum(
            1 for r in rows if r["optimizer_fitness"]["hard_filters_passed"]
        ),
        "optimizer_near_misses": sum(
            1 for r in rows if r["optimizer_fitness"]["near_miss"]
        ),
    }


def _policy(registry: ScalperParameterRegistry) -> dict:
    gate = registry.family("book_imbalance_continuation").route_gate
    return {
        "status": "research_only",
        "can_trade": False,
        "can_promote": False,
        "min_candidate_trades": MIN_CANDIDATE_TRADES,
        "route_gate": gate.to_dict(),
        "exchange_fees": {
            exchange: profile.to_dict()
            for exchange, profile in registry.exchange_fees.items()
        },
        "required_next_steps": [
            "human_review",
            "pre_registered_untouched_judgment",
            "paper_or_shadow_after_approval",
        ],
        "live_shadow": {
            "min_virtual_trades": LIVE_SHADOW_MIN_TRADES,
            "score_bonus": LIVE_SHADOW_SCORE_BONUS,
            "positive_annotation": LIVE_SHADOW_POSITIVE,
            "negative_annotation": LIVE_SHADOW_NEGATIVE,
            "annotation_only": True,
            "never_auto_promotes": True,
        },
        "judgment_overlay": {
            "enabled": True,
            "latest_reject_blocks_queue": True,
        },
        "execution_truth": {
            "enabled": True,
            "source": "vnedge.research.execution_edge_labeler",
            "blocks_queue_when_negative_or_under_sampled": True,
            "annotation_only_for_score": False,
            "never_auto_promotes": True,
        },
        "optimizer_scorecard": optimizer_scorecard_policy(
            OptimizerScorecardConfig(
                min_trades=MIN_CANDIDATE_TRADES,
                min_profit_factor=gate.maker_min_profit_factor,
            )
        ),
    }


def _latest_judgments(records: Iterable[dict]) -> dict[tuple[str, str, str], dict]:
    latest: dict[tuple[str, str, str], dict] = {}
    for record in records:
        if record.get("kind") != "judgment":
            continue
        exchange = record.get("exchange")
        symbol = record.get("symbol")
        strategy = record.get("strategy_id")
        if not (exchange and symbol and strategy):
            continue
        latest[(str(exchange), str(symbol), str(strategy))] = {
            "verdict": record.get("verdict"),
            "window_start": record.get("window_start"),
            "window_end": record.get("window_end"),
            "note": record.get("note", ""),
        }
    return latest


def _fee_drag_pct(net: float, fees: float) -> float:
    denom = max(abs(net) + fees, 1e-9)
    return round(fees / denom * 100.0, 1)


def _finite_float(value, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if math.isinf(out):
        return 999.0 if out > 0 else default
    if math.isnan(out):
        return default
    return round(out, 4)
