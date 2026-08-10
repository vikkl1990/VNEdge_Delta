"""Backtest-failure uplift planner for scanner evidence.

This module consumes completed scanner/replay evidence and turns every row into
an operator-useful diagnosis: what failed, how far it is from the fee wall, and
what the next research experiment should be. It is intentionally research-only;
it never grants paper/live permission by itself.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
import json
import math
from pathlib import Path
from tempfile import NamedTemporaryFile
import time
from typing import Iterable, Literal


SCANNER_BACKTEST_UPLIFT_ID = "scanner_backtest_uplift_v1"
DEFAULT_OUT = Path("research/live_research/scanner_backtest_uplift_latest.json")
DEFAULT_FEED = Path("research/live_research/scanner_backtest_uplift_feed.jsonl")

FailureMode = Literal[
    "PROMOTABLE_PROOF_CANDIDATE",
    "SPARSE_POSITIVE",
    "POSITIVE_EDGE_TOO_THIN",
    "POSITIVE_PF_WEAK",
    "FEE_WALL_NEAR_MISS",
    "VISUAL_EDGE_FEE_WALL",
    "PF_STRUCTURE_BUT_FEE_NEGATIVE",
    "OVERSCALP_FEE_BLEED",
    "NO_TRADES",
    "UNDER_SAMPLED_NEGATIVE",
    "NEGATIVE_EDGE",
]


@dataclass(frozen=True)
class ScannerGateConfig:
    min_net_bps: float = 25.0
    min_profit_factor: float = 1.50
    min_trades: int = 20
    near_miss_net_floor_bps: float = -10.0
    visual_edge_floor_bps: float = 0.0

    def __post_init__(self) -> None:
        if self.min_net_bps <= 0:
            raise ValueError("min_net_bps must be positive")
        if self.min_profit_factor < 1.0:
            raise ValueError("min_profit_factor must be >= 1")
        if self.min_trades < 1:
            raise ValueError("min_trades must be >= 1")


@dataclass(frozen=True)
class ScannerEvidenceRow:
    evidence_source: str
    exchange: str
    symbol: str
    timeframe: str
    strategy_id: str
    mode: str
    samples: int
    avg_net_bps: float | None
    visual_avg_bps: float | None = None
    profit_factor: float | None = None
    win_rate_pct: float | None = None
    passed: bool = False
    actual_notional_avg: float | None = None
    margin_avg: float | None = None
    contracts_avg: float | None = None
    exits: dict[str, int] = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ScannerUpliftRow:
    rank: int
    row_id: str
    failure_mode: FailureMode
    uplift_action: str
    use_as: str
    score: float
    required_uplift_bps: float | None
    fee_drag_bps: float | None
    exchange: str
    symbol: str
    timeframe: str
    strategy_id: str
    mode: str
    samples: int
    avg_net_bps: float | None
    visual_avg_bps: float | None
    profit_factor: float | None
    win_rate_pct: float | None
    passed: bool
    actual_notional_avg: float | None
    margin_avg: float | None
    contracts_avg: float | None
    exits: dict[str, int]
    rationale: str
    can_trade: bool = False
    can_promote: bool = False
    requires_untouched_judgment: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ScannerUpliftExperiment:
    experiment_id: str
    priority: int
    experiment_type: str
    target_rows: tuple[str, ...]
    exchange: str
    symbol: str
    timeframes: tuple[str, ...]
    strategy_id: str
    hypothesis: str
    required_change: str
    expected_effect: str
    guardrails: tuple[str, ...]
    can_trade: bool = False
    can_promote: bool = False
    requires_untouched_judgment: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


def run_scanner_backtest_uplift(
    *,
    evidence_payloads: Iterable[dict],
    source_names: Iterable[str] | None = None,
    config: ScannerGateConfig = ScannerGateConfig(),
    max_rows: int = 80,
    max_experiments: int = 12,
    now: datetime | None = None,
) -> dict:
    generated = now or datetime.now(UTC)
    payloads = tuple(evidence_payloads)
    provided_sources = tuple(source_names or ())
    fallback_sources = tuple(
        f"payload_{idx}" for idx, _ in enumerate(payloads, start=1)
    )
    sources = tuple(
        provided_sources[idx] if idx < len(provided_sources) else fallback
        for idx, fallback in enumerate(fallback_sources)
    )
    rows = tuple(
        row
        for payload, source in zip(
            payloads,
            sources,
            strict=False,
        )
        for row in evidence_rows_from_payload(payload, evidence_source=source)
    )
    uplift_rows = _rank_uplifts(
        (classify_evidence_row(row, config=config) for row in rows),
        max_rows=max_rows,
    )
    experiments = _build_experiments(uplift_rows, max_experiments=max_experiments)
    return {
        "agent_id": SCANNER_BACKTEST_UPLIFT_ID,
        "generated_at": generated.isoformat(),
        "summary": _summary(rows, uplift_rows, experiments, config),
        "policy": _policy(config),
        "top_uplifts": [row.to_dict() for row in uplift_rows],
        "experiments": [row.to_dict() for row in experiments],
        "operator_answer": _operator_answer(uplift_rows, experiments),
        "can_trade": False,
        "can_promote": False,
    }


def evidence_rows_from_payload(
    payload: dict,
    *,
    evidence_source: str,
) -> tuple[ScannerEvidenceRow, ...]:
    if isinstance(payload.get("rows"), list):
        return tuple(_row_from_pine_matrix(row, evidence_source) for row in payload["rows"])
    if isinstance(payload.get("candidates"), list):
        return tuple(
            _row_from_scanner_candidate(row, evidence_source)
            for row in payload["candidates"]
            if isinstance(row, dict)
        )
    if payload.get("truth_layer") == "vnedge_algo_ml_pro_pine_replay_v1":
        return (_row_from_pine_payload(payload, evidence_source),)
    return ()


def classify_evidence_row(
    row: ScannerEvidenceRow,
    *,
    config: ScannerGateConfig = ScannerGateConfig(),
) -> ScannerUpliftRow:
    avg = row.avg_net_bps
    visual = row.visual_avg_bps
    pf = row.profit_factor
    fee_drag = _fee_drag_bps(visual, avg)
    required = None if avg is None else round(max(0.0, config.min_net_bps - avg), 4)
    failure = _failure_mode(row, config)
    action, use_as = _uplift_action(failure, row)
    return ScannerUpliftRow(
        rank=0,
        row_id=_row_id(row),
        failure_mode=failure,
        uplift_action=action,
        use_as=use_as,
        score=_score(row, failure, config),
        required_uplift_bps=required,
        fee_drag_bps=fee_drag,
        exchange=row.exchange,
        symbol=row.symbol,
        timeframe=row.timeframe,
        strategy_id=row.strategy_id,
        mode=row.mode,
        samples=row.samples,
        avg_net_bps=avg,
        visual_avg_bps=visual,
        profit_factor=pf,
        win_rate_pct=row.win_rate_pct,
        passed=row.passed,
        actual_notional_avg=row.actual_notional_avg,
        margin_avg=row.margin_avg,
        contracts_avg=row.contracts_avg,
        exits=dict(row.exits),
        rationale=_rationale(row, failure, required, fee_drag, config),
    )


def publish_scanner_backtest_uplift(
    payload: dict,
    *,
    out: Path | str = DEFAULT_OUT,
    feed: Path | str | None = DEFAULT_FEED,
) -> Path:
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    with NamedTemporaryFile(
        "w",
        dir=out_path.parent,
        prefix=out_path.name,
        suffix=".tmp",
        delete=False,
        encoding="utf-8",
    ) as tmp:
        tmp.write(encoded)
        tmp_path = Path(tmp.name)
    tmp_path.chmod(0o644)
    tmp_path.replace(out_path)
    out_path.chmod(0o644)
    if feed is not None:
        feed_path = Path(feed)
        feed_path.parent.mkdir(parents=True, exist_ok=True)
        with feed_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_feed_record(payload), sort_keys=True) + "\n")
        feed_path.chmod(0o644)
    return out_path


def _row_from_pine_matrix(row: dict, evidence_source: str) -> ScannerEvidenceRow:
    return ScannerEvidenceRow(
        evidence_source=evidence_source,
        exchange=str(row.get("exchange") or "delta_india"),
        symbol=str(row.get("symbol") or "unknown"),
        timeframe=str(row.get("timeframe") or "unknown"),
        strategy_id=str(row.get("strategy_id") or "vnedge_algo_ml_pro_v1"),
        mode=str(row.get("mode") or row.get("capture_mode") or "unknown"),
        samples=_int(row.get("closed") if row.get("closed") is not None else row.get("samples")),
        avg_net_bps=_float(row.get("fee_avg_bps") if row.get("fee_avg_bps") is not None else row.get("avg_net_bps")),
        visual_avg_bps=_float(row.get("visual_avg_bps")),
        profit_factor=_float(row.get("pf_r") if row.get("pf_r") is not None else row.get("profit_factor")),
        win_rate_pct=_float(row.get("win_rate_pct")),
        passed=bool(row.get("passed")),
        actual_notional_avg=_float(row.get("actual_notional_avg")),
        margin_avg=_float(row.get("margin_avg")),
        contracts_avg=_float(row.get("contracts_avg")),
        exits={str(k): _int(v) for k, v in dict(row.get("exits") or {}).items()},
        raw=dict(row),
    )


def _row_from_pine_payload(payload: dict, evidence_source: str) -> ScannerEvidenceRow:
    summary = dict(payload.get("summary") or {})
    sizing = dict(summary.get("position_sizing") or {})
    return ScannerEvidenceRow(
        evidence_source=evidence_source,
        exchange=str(payload.get("exchange") or "unknown"),
        symbol=str(payload.get("symbol") or "unknown"),
        timeframe=str(payload.get("timeframe") or "unknown"),
        strategy_id=str(payload.get("strategy_id") or "vnedge_algo_ml_pro_v1"),
        mode=str(payload.get("capture_mode") or "unknown"),
        samples=_int(summary.get("closed_trades")),
        avg_net_bps=_float(summary.get("fee_aware_avg_bps")),
        visual_avg_bps=_float(summary.get("visual_avg_bps")),
        profit_factor=_float(summary.get("profit_factor_r")),
        win_rate_pct=_float(summary.get("win_rate_pct")),
        passed=bool(dict(summary.get("promotion_gate") or {}).get("passed")),
        actual_notional_avg=_float(sizing.get("actual_notional_usd_avg")),
        margin_avg=_float(sizing.get("margin_usd_avg")),
        contracts_avg=_float(sizing.get("contracts_avg")),
        exits={str(k): _int(v) for k, v in dict(summary.get("exit_reason_counts") or {}).items()},
        raw=dict(payload),
    )


def _row_from_scanner_candidate(row: dict, evidence_source: str) -> ScannerEvidenceRow:
    return ScannerEvidenceRow(
        evidence_source=evidence_source,
        exchange=str(row.get("exchange") or "unknown"),
        symbol=str(row.get("symbol") or "unknown"),
        timeframe=str(row.get("timeframe") or "unknown"),
        strategy_id=str(row.get("strategy_id") or "unknown"),
        mode=str(row.get("dominant_route") or "edge_router"),
        samples=_int(row.get("routed")),
        avg_net_bps=_float(row.get("avg_selected_net_bps")),
        visual_avg_bps=_float(row.get("avg_selected_gross_bps")),
        profit_factor=_float(row.get("profit_factor")),
        win_rate_pct=_float(row.get("win_rate_pct")),
        passed=str(row.get("verdict") or "") == "STRICT_PROOF_WATCHLIST",
        exits={},
        raw=dict(row),
    )


def _failure_mode(row: ScannerEvidenceRow, config: ScannerGateConfig) -> FailureMode:
    avg = row.avg_net_bps
    visual = row.visual_avg_bps
    pf = row.profit_factor
    if row.samples <= 0:
        return "NO_TRADES"
    if (
        row.passed
        and row.samples >= config.min_trades
        and avg is not None
        and avg >= config.min_net_bps
        and pf is not None
        and pf >= config.min_profit_factor
    ):
        return "PROMOTABLE_PROOF_CANDIDATE"
    if row.samples < config.min_trades:
        return "SPARSE_POSITIVE" if avg is not None and avg > 0.0 else "UNDER_SAMPLED_NEGATIVE"
    if avg is not None and avg > 0.0:
        if pf is not None and pf < config.min_profit_factor:
            return "POSITIVE_PF_WEAK"
        return "POSITIVE_EDGE_TOO_THIN"
    if visual is not None and visual > config.visual_edge_floor_bps:
        if avg is not None and avg >= config.near_miss_net_floor_bps and (pf or 0.0) >= 1.0:
            return "FEE_WALL_NEAR_MISS"
        return "VISUAL_EDGE_FEE_WALL"
    if pf is not None and pf >= 1.20 and avg is not None and avg < 0.0:
        return "PF_STRUCTURE_BUT_FEE_NEGATIVE"
    if row.timeframe in {"1m", "5m"} and row.samples >= 100 and avg is not None and avg < 0.0:
        return "OVERSCALP_FEE_BLEED"
    return "NEGATIVE_EDGE"


def _uplift_action(failure: FailureMode, row: ScannerEvidenceRow) -> tuple[str, str]:
    if failure == "PROMOTABLE_PROOF_CANDIDATE":
        return "PRE_REGISTER_UNTOUCHED_JUDGMENT", "candidate_proof"
    if failure == "SPARSE_POSITIVE":
        return "EXTEND_SAMPLE_ON_NEXT_UNTOUCHED_WINDOW", "sparse_candidate"
    if failure == "POSITIVE_EDGE_TOO_THIN":
        return "ADD_SELECTIVITY_NOT_FREQUENCY", "context_filter"
    if failure == "POSITIVE_PF_WEAK":
        return "REWORK_EXITS_AND_FALSE_POSITIVE_FILTER", "exit_lab"
    if failure == "FEE_WALL_NEAR_MISS":
        return "TEST_MAKER_FIRST_CONTEXT_FILTERED_ROUTE", "execution_uplift"
    if failure == "VISUAL_EDGE_FEE_WALL":
        return "RECYCLE_AS_FEATURE_AND_REQUIRE_COST_FORECAST", "feature_bank"
    if failure == "PF_STRUCTURE_BUT_FEE_NEGATIVE":
        return "MINE_WIN_CONTEXT_AND_ROUTE_SELECTIVELY", "edge_model_feature"
    if failure == "OVERSCALP_FEE_BLEED":
        return "BLOCK_CONTINUOUS_SCALP_REQUIRE_EVENT_CATALYST", "negative_training_label"
    if failure == "NO_TRADES":
        return "DROP_AS_SCALPER_OR_USE_AS_HTF_CONTEXT", "inactive_context"
    if failure == "UNDER_SAMPLED_NEGATIVE":
        return "COLLECT_MORE_DATA_ONLY_NO_TUNING", "weak_evidence"
    return "REJECT_AS_STANDALONE_ENTRY", "negative_training_label"


def _rationale(
    row: ScannerEvidenceRow,
    failure: FailureMode,
    required_uplift_bps: float | None,
    fee_drag_bps: float | None,
    config: ScannerGateConfig,
) -> str:
    avg = "--" if row.avg_net_bps is None else f"{row.avg_net_bps:.2f}"
    pf = "--" if row.profit_factor is None else f"{row.profit_factor:.2f}"
    req = "--" if required_uplift_bps is None else f"{required_uplift_bps:.2f}"
    drag = "--" if fee_drag_bps is None else f"{fee_drag_bps:.2f}"
    if failure == "FEE_WALL_NEAR_MISS":
        return (
            f"{row.symbol} {row.timeframe} {row.mode} is close but still pays the "
            f"fee wall: avg {avg} bps, PF {pf}, fee drag {drag} bps. Needs about "
            f"{req} bps of extra net edge to reach the {config.min_net_bps:g} bps gate."
        )
    if failure == "POSITIVE_EDGE_TOO_THIN":
        return (
            f"Net result is positive but too thin: avg {avg} bps with "
            f"{row.samples} trades. Increase selectivity, not cadence."
        )
    if failure == "POSITIVE_PF_WEAK":
        return (
            f"Average is positive but PF {pf} is below {config.min_profit_factor:g}; "
            "the next research move is exit quality and false-positive pruning."
        )
    if failure == "SPARSE_POSITIVE":
        return (
            f"Positive but only {row.samples} trades; expand the untouched window "
            "instead of tuning on the seen slice."
        )
    return (
        f"{failure.lower()} on {row.symbol} {row.timeframe}: avg {avg} bps, "
        f"PF {pf}, samples {row.samples}; row is useful as evidence but not promotion."
    )


def _rank_uplifts(
    rows: Iterable[ScannerUpliftRow],
    *,
    max_rows: int,
) -> tuple[ScannerUpliftRow, ...]:
    ranked = sorted(rows, key=lambda row: row.score, reverse=True)
    return tuple(
        ScannerUpliftRow(**{**asdict(row), "rank": rank})
        for rank, row in enumerate(ranked[:max_rows], start=1)
    )


def _build_experiments(
    rows: tuple[ScannerUpliftRow, ...],
    *,
    max_experiments: int,
) -> tuple[ScannerUpliftExperiment, ...]:
    groups: dict[tuple[str, str, str], list[ScannerUpliftRow]] = {}
    for row in rows:
        if row.failure_mode not in {
            "FEE_WALL_NEAR_MISS",
            "POSITIVE_EDGE_TOO_THIN",
            "POSITIVE_PF_WEAK",
            "SPARSE_POSITIVE",
            "PF_STRUCTURE_BUT_FEE_NEGATIVE",
        }:
            continue
        groups.setdefault((row.exchange, row.symbol, row.strategy_id), []).append(row)

    experiments: list[ScannerUpliftExperiment] = []
    for index, ((exchange, symbol, strategy), group) in enumerate(
        sorted(groups.items(), key=lambda item: max(row.score for row in item[1]), reverse=True),
        start=1,
    ):
        top = sorted(group, key=lambda row: row.score, reverse=True)[:4]
        modes = {row.failure_mode for row in top}
        if "FEE_WALL_NEAR_MISS" in modes:
            exp_type = "maker_first_context_filtered_replay"
            change = "Require HTF bias, BBP/ADX alignment, volume impulse, and maker-first route before allowing taker fallback."
            effect = "Cut fee drag and reject visual-only entries that do not forecast >25 bps net."
        elif "POSITIVE_PF_WEAK" in modes:
            exp_type = "exit_overlay_replay"
            change = "Test faster invalidation, BE after TP1, and trail tightening against the same entry timestamps."
            effect = "Raise PF by shrinking tail losses without adding more trades."
        elif "SPARSE_POSITIVE" in modes:
            exp_type = "sample_expansion"
            change = "Run the same frozen setup on a longer untouched window and cross-venue sample."
            effect = "Decide whether sparse positives are stable or random."
        else:
            exp_type = "selectivity_filter_replay"
            change = "Mine winning contexts from the row set and add only causal pre-entry filters."
            effect = "Raise average net bps by trading fewer but stronger setups."
        experiments.append(
            ScannerUpliftExperiment(
                experiment_id=f"{exp_type}|{exchange}|{symbol}|{strategy}",
                priority=index,
                experiment_type=exp_type,
                target_rows=tuple(row.row_id for row in top),
                exchange=exchange,
                symbol=symbol,
                timeframes=tuple(dict.fromkeys(row.timeframe for row in top)),
                strategy_id=strategy,
                hypothesis=_hypothesis(top),
                required_change=change,
                expected_effect=effect,
                guardrails=_guardrails(),
            )
        )
        if len(experiments) >= max_experiments:
            break
    return tuple(experiments)


def _summary(
    rows: tuple[ScannerEvidenceRow, ...],
    uplift_rows: tuple[ScannerUpliftRow, ...],
    experiments: tuple[ScannerUpliftExperiment, ...],
    config: ScannerGateConfig,
) -> dict:
    modes = Counter(row.failure_mode for row in uplift_rows)
    positive_after_cost = [row for row in uplift_rows if (row.avg_net_bps or 0.0) > 0.0]
    visual_only = [
        row for row in uplift_rows
        if (row.visual_avg_bps or 0.0) > 0.0 and (row.avg_net_bps or 0.0) <= 0.0
    ]
    near = [row for row in uplift_rows if row.failure_mode == "FEE_WALL_NEAR_MISS"]
    promotable = [
        row for row in uplift_rows
        if row.failure_mode == "PROMOTABLE_PROOF_CANDIDATE"
    ]
    best = uplift_rows[0] if uplift_rows else None
    return {
        "evidence_rows": len(rows),
        "ranked_rows": len(uplift_rows),
        "promotable_proof_candidates": len(promotable),
        "positive_after_cost": len(positive_after_cost),
        "visual_only_positive": len(visual_only),
        "fee_wall_near_misses": len(near),
        "experiments": len(experiments),
        "failure_modes": dict(modes),
        "gate": asdict(config),
        "best_row_id": best.row_id if best is not None else None,
        "best_failure_mode": best.failure_mode if best is not None else None,
        "best_avg_net_bps": best.avg_net_bps if best is not None else None,
        "best_profit_factor": best.profit_factor if best is not None else None,
        "research_only": True,
        "can_trade": False,
        "can_promote": False,
    }


def _operator_answer(
    rows: tuple[ScannerUpliftRow, ...],
    experiments: tuple[ScannerUpliftExperiment, ...],
) -> str:
    if not rows:
        return "No scanner backtest evidence was available for uplift analysis."
    best = rows[0]
    if best.failure_mode == "PROMOTABLE_PROOF_CANDIDATE":
        return (
            f"Best row is {best.row_id} and clears proof gates on this evidence; "
            "next step is an untouched-window judgment, not live promotion."
        )
    if best.failure_mode in {"FEE_WALL_NEAR_MISS", "POSITIVE_EDGE_TOO_THIN", "POSITIVE_PF_WEAK"}:
        return (
            f"Best row is {best.row_id}: {best.failure_mode}. It is not paper-ready, "
            f"but it gives a concrete uplift target. {len(experiments)} research "
            "experiments are queued around maker-first routing, context filters, and exits."
        )
    return (
        f"Best row is {best.row_id}: {best.failure_mode}. The current scanner evidence "
        "does not break the fee wall; use it as training/failure context before adding trades."
    )


def _policy(config: ScannerGateConfig) -> dict:
    return {
        "research_only": True,
        "can_trade": False,
        "can_promote": False,
        "requires_untouched_judgment": True,
        "min_net_bps": config.min_net_bps,
        "min_profit_factor": config.min_profit_factor,
        "min_trades": config.min_trades,
        "operator_note": (
            "This report mines failed and near-miss scanner backtests. It may create "
            "new research experiments, but it cannot lower live gates or promote lanes."
        ),
    }


def _score(row: ScannerEvidenceRow, failure: FailureMode, config: ScannerGateConfig) -> float:
    avg = row.avg_net_bps if row.avg_net_bps is not None else -50.0
    visual = row.visual_avg_bps if row.visual_avg_bps is not None else avg
    pf = min(row.profit_factor if row.profit_factor is not None else 0.0, 5.0)
    sample_bonus = min(math.sqrt(max(row.samples, 0)), 12.0)
    mode_bonus = {
        "PROMOTABLE_PROOF_CANDIDATE": 100.0,
        "FEE_WALL_NEAR_MISS": 45.0,
        "POSITIVE_EDGE_TOO_THIN": 40.0,
        "POSITIVE_PF_WEAK": 35.0,
        "SPARSE_POSITIVE": 25.0,
        "PF_STRUCTURE_BUT_FEE_NEGATIVE": 18.0,
        "VISUAL_EDGE_FEE_WALL": 10.0,
        "OVERSCALP_FEE_BLEED": -15.0,
        "NO_TRADES": -40.0,
        "UNDER_SAMPLED_NEGATIVE": -30.0,
        "NEGATIVE_EDGE": -25.0,
    }[failure]
    uplift_penalty = max(0.0, config.min_net_bps - avg) * 0.35
    return round(avg + max(visual, 0.0) * 0.20 + pf * 6.0 + sample_bonus + mode_bonus - uplift_penalty, 4)


def _hypothesis(rows: Iterable[ScannerUpliftRow]) -> str:
    rows = tuple(rows)
    best = rows[0]
    return (
        f"{best.strategy_id} on {best.symbol} has structure but not enough net edge; "
        "a causal filter or route overlay should keep the high-MFE contexts and skip fee-wall churn."
    )


def _guardrails() -> tuple[str, ...]:
    return (
        "research-only output; no paper/live promotion",
        "all filters must use pre-entry causal features only",
        "do not tune on a judged window",
        "taker fallback must clear fees, slippage, and safety buffer",
        "new pass still requires untouched-window judgment",
    )


def _row_id(row: ScannerEvidenceRow) -> str:
    symbol = row.symbol.replace("/", "").replace(":", "").replace("-", "")
    return f"{row.strategy_id}|{row.exchange}|{symbol}|{row.timeframe}|{row.mode}"


def _feed_record(payload: dict) -> dict:
    return {
        "agent_id": payload.get("agent_id"),
        "generated_at": payload.get("generated_at"),
        "summary": payload.get("summary", {}),
        "operator_answer": payload.get("operator_answer"),
        "can_trade": False,
        "can_promote": False,
    }


def _fee_drag_bps(visual: float | None, avg: float | None) -> float | None:
    if visual is None or avg is None:
        return None
    value = visual - avg
    return round(value, 4) if math.isfinite(value) else None


def _float(value: object) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _int(value: object) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        return 0
    return out


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    return data if isinstance(data, dict) else {"rows": data}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="research-only scanner backtest uplift")
    parser.add_argument("--input", action="append", required=True, help="JSON evidence payload; repeatable")
    parser.add_argument("--source-name", action="append", help="Optional source label matching --input")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--feed", default=str(DEFAULT_FEED))
    parser.add_argument("--max-rows", type=int, default=80)
    parser.add_argument("--max-experiments", type=int, default=12)
    parser.add_argument("--min-net-bps", type=float, default=25.0)
    parser.add_argument("--min-profit-factor", type=float, default=1.50)
    parser.add_argument("--min-trades", type=int, default=20)
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=0,
        help="repeat forever at this cadence; 0 runs once",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    while True:
        payloads = tuple(_read_json(Path(path)) for path in args.input)
        source_names = tuple(args.source_name or ()) or tuple(Path(path).name for path in args.input)
        report = run_scanner_backtest_uplift(
            evidence_payloads=payloads,
            source_names=source_names,
            config=ScannerGateConfig(
                min_net_bps=args.min_net_bps,
                min_profit_factor=args.min_profit_factor,
                min_trades=args.min_trades,
            ),
            max_rows=args.max_rows,
            max_experiments=args.max_experiments,
        )
        path = publish_scanner_backtest_uplift(
            report,
            out=args.out,
            feed=None if args.feed == "" else args.feed,
        )
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True), flush=True)
        else:
            print(f"scanner backtest uplift wrote {path}", flush=True)
            print(report["operator_answer"], flush=True)
        if args.interval_seconds <= 0:
            break
        time.sleep(max(1, args.interval_seconds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
