"""Execution-realistic replay profile for research evidence.

TradingView/Pine and candle-forward scans can prove that a pattern moved after
an event. They do not prove VNEDGE could enter, fill, and exit that move. This
module classifies every research evidence row by the execution realism level it
has actually cleared and separately evaluates which prediction-market
settlement ideas are portable to crypto perpetuals.

Research-only: this report never promotes, never trades, and never changes the
runtime risk gateway.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Iterable, Literal


EXECUTION_PROFILE_ID = "execution_realistic_replay_profile_v1"
DEFAULT_RESEARCH_DIR = Path("research/live_research")
DEFAULT_EVIDENCE_INDEX = DEFAULT_RESEARCH_DIR / "evidence_index_latest.json"
DEFAULT_FEE_WALL = DEFAULT_RESEARCH_DIR / "fee_wall_forensics_latest.json"
DEFAULT_CANDIDATE_REPLAY = DEFAULT_RESEARCH_DIR / "candidate_replay_latest.json"
DEFAULT_OUT = DEFAULT_RESEARCH_DIR / "execution_replay_profile_latest.json"
DEFAULT_FEED = DEFAULT_RESEARCH_DIR / "execution_replay_profile_feed.jsonl"

STRICT_MIN_NET_BPS = 25.0
STRICT_MIN_PROFIT_FACTOR = 1.50
STRICT_MIN_SAMPLES = 20

ProfileId = Literal[
    "L0_SOURCE_OR_VISUAL_ONLY",
    "L1_CANDLE_FORWARD_ROUTE_LABEL",
    "L2_NEXT_TRADE_TAKER_REPLAY",
    "L3_L2_TRADE_THROUGH_REPLAY",
    "L4_L2_QUEUE_AWARE_MAKER_REPLAY",
]


@dataclass(frozen=True)
class ExecutionProfileDefinition:
    profile_id: ProfileId
    rank: int
    title: str
    fill_evidence: str
    latency_model: str
    promotion_meaning: str
    required_before_paper: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SettlementComponent:
    component_id: str
    prediction_market_behavior: str
    crypto_perp_verdict: str
    portable: bool
    vnedge_action: str
    risk_if_misused: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ExecutionEvidenceRow:
    row_id: str
    source_kind: str
    source_artifact: str
    strategy_id: str
    exchange: str
    symbol: str
    timeframe: str
    verdict: str
    status: str
    samples: int
    avg_net_bps: float | None
    profit_factor: float | None
    win_rate_pct: float | None
    route: str
    profile_id: ProfileId
    profile_rank: int
    fill_evidence: str
    latency_model: str
    settlement_model: str
    settlement_portability: str
    strict_economic_edge: bool
    execution_truth_ready: bool
    requires_execution_replay_before_paper: bool
    blockers: tuple[str, ...]
    next_action: str
    can_trade: bool = False
    can_promote: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


EXECUTION_PROFILES: tuple[ExecutionProfileDefinition, ...] = (
    ExecutionProfileDefinition(
        profile_id="L0_SOURCE_OR_VISUAL_ONLY",
        rank=0,
        title="Source, visual, or script-intention evidence only",
        fill_evidence="none",
        latency_model="not modeled",
        promotion_meaning="Useful for research triage, never for paper/live readiness.",
        required_before_paper=(
            "causal VNEDGE port",
            "closed-bar replay with fees/slippage",
            "execution replay profile L3 or L4",
        ),
    ),
    ExecutionProfileDefinition(
        profile_id="L1_CANDLE_FORWARD_ROUTE_LABEL",
        rank=1,
        title="Closed-candle forward route label",
        fill_evidence="candle high/low path only; no order-book fill proof",
        latency_model="bar-close signal timing only",
        promotion_meaning=(
            "Can show the move existed after fees, but not that VNEDGE could "
            "fill it."
        ),
        required_before_paper=(
            "next-trade taker replay for taker paths",
            "L2 trade-through replay for maker paths",
            "venue size/precision validation",
        ),
    ),
    ExecutionProfileDefinition(
        profile_id="L2_NEXT_TRADE_TAKER_REPLAY",
        rank=2,
        title="Next-trade taker replay",
        fill_evidence="entry on the first eligible trade after signal plus slippage",
        latency_model="same-print fills forbidden; reaction latency implicit",
        promotion_meaning="Valid taker feasibility evidence, still needs untouched judgment.",
        required_before_paper=(
            "strict fee wall after taker fees",
            "stop-first exit accounting",
            "untouched-window judgment",
        ),
    ),
    ExecutionProfileDefinition(
        profile_id="L3_L2_TRADE_THROUGH_REPLAY",
        rank=3,
        title="Passive maker trade-through replay",
        fill_evidence="resting maker quote fills only after eligible trade-through evidence",
        latency_model="quote placed after event; same-instant fills blocked",
        promotion_meaning="Valid maker feasibility evidence except full queue position.",
        required_before_paper=(
            "queue-risk annotation",
            "adverse-selection review",
            "untouched-window judgment",
        ),
    ),
    ExecutionProfileDefinition(
        profile_id="L4_L2_QUEUE_AWARE_MAKER_REPLAY",
        rank=4,
        title="L2 queue-aware maker replay",
        fill_evidence="displayed queue ahead must clear before VNEDGE maker fill",
        latency_model="quote/cancel latency modeled or bounded",
        promotion_meaning="Strongest offline execution proof available without L3/MBO.",
        required_before_paper=(
            "fresh data coverage",
            "untouched-window judgment",
            "human approval for paper/shadow manifest",
        ),
    ),
)

PROFILE_BY_ID = {profile.profile_id: profile for profile in EXECUTION_PROFILES}


def prediction_market_settlement_components() -> tuple[SettlementComponent, ...]:
    """Evaluate prediction-market settlement logic against crypto perpetuals."""

    return (
        SettlementComponent(
            component_id="terminal_binary_payoff",
            prediction_market_behavior=(
                "A YES/NO contract settles to a bounded terminal payoff, often 0 or 1."
            ),
            crypto_perp_verdict="NOT_PORTABLE_TO_PERPETUALS",
            portable=False,
            vnedge_action=(
                "Use mark-to-market PnL plus explicit exits; never hold a perp "
                "because a binary terminal payoff assumption looked profitable."
            ),
            risk_if_misused=(
                "Creates fake expectancy by ignoring stop, liquidation, and funding path risk."
            ),
        ),
        SettlementComponent(
            component_id="complementary_pair_arbitrage",
            prediction_market_behavior=(
                "Buying complementary outcomes can lock a terminal sum if both legs fill."
            ),
            crypto_perp_verdict="NOT_PORTABLE_TO_SINGLE_PERP",
            portable=False,
            vnedge_action=(
                "Only reuse the invariant-check pattern for real paired instruments "
                "such as spot/perp basis or cross-venue hedges with hedge failure logic."
            ),
            risk_if_misused="Treats directional exposure as hedged when it is not.",
        ),
        SettlementComponent(
            component_id="hold_to_resolution",
            prediction_market_behavior=(
                "Some strategies hold to event resolution instead of trading out."
            ),
            crypto_perp_verdict="NOT_PORTABLE_TO_PERPETUALS",
            portable=False,
            vnedge_action=(
                "Every VNEDGE perp strategy must define structural stop, time stop, "
                "funding exposure, liquidation distance, and TP/trailing exits."
            ),
            risk_if_misused="Turns a scalp into an unbounded loss carry trade.",
        ),
        SettlementComponent(
            component_id="maker_rebate_or_settlement_credit",
            prediction_market_behavior=(
                "Prediction venues may pay maker rebates from event/fee economics."
            ),
            crypto_perp_verdict="CONDITIONAL_BY_EXCHANGE_FEE_TIER_ONLY",
            portable=False,
            vnedge_action=(
                "Use only actual Binance/Bybit/Delta fee tier and rebate data; "
                "default to no rebate unless the account truly receives it."
            ),
            risk_if_misused="Invents bps that do not exist in the account ledger.",
        ),
        SettlementComponent(
            component_id="ledger_replay",
            prediction_market_behavior=(
                "Replay fills, fees, balances, and resolved PnL from append-only ledgers."
            ),
            crypto_perp_verdict="PORTABLE",
            portable=True,
            vnedge_action=(
                "Keep using append-only decision/fill/funding ledgers and reconcile "
                "against exchange state before any promotion."
            ),
            risk_if_misused="Low; the risk is only incomplete fee/funding capture.",
        ),
        SettlementComponent(
            component_id="coverage_and_gap_penalties",
            prediction_market_behavior=(
                "Result payloads separate requested coverage from loaded coverage and "
                "penalize gaps."
            ),
            crypto_perp_verdict="PORTABLE",
            portable=True,
            vnedge_action=(
                "Require candle, trade, L2, fee, and funding coverage metadata on "
                "every scanner backtest/replay row."
            ),
            risk_if_misused="Low; missing coverage still must block promotion.",
        ),
        SettlementComponent(
            component_id="joint_portfolio_settlement_pnl",
            prediction_market_behavior=(
                "Multiple markets can be settled and scored as one portfolio."
            ),
            crypto_perp_verdict="PORTABLE_AS_PORTFOLIO_MARK_TO_MARKET",
            portable=True,
            vnedge_action=(
                "Adopt portfolio-level equity/drawdown scoring across lanes, but "
                "compute PnL from mark price, fills, fees, funding, and borrow/margin rules."
            ),
            risk_if_misused="Medium if binary settlement math is reused instead of perp PnL.",
        ),
    )


def build_execution_replay_profile_report(
    *,
    evidence_index: dict[str, Any] | None = None,
    candidate_replay: dict[str, Any] | None = None,
    fee_wall: dict[str, Any] | None = None,
    now: datetime | None = None,
    max_rows: int = 250,
) -> dict[str, Any]:
    """Build the execution profile report from existing research evidence."""

    generated = now or datetime.now(UTC)
    evidence_index = evidence_index or {}
    candidate_replay = candidate_replay or {}
    fee_wall = fee_wall or {}
    rows = [
        _profile_row(
            row,
            candidate_replay=candidate_replay,
            fee_wall=fee_wall,
        )
        for row in _evidence_records(evidence_index)
    ]
    rows = sorted(rows, key=_row_rank, reverse=True)
    summary = _summary(rows)
    settlement = [component.to_dict() for component in prediction_market_settlement_components()]
    return {
        "generated_at": generated.isoformat(),
        "execution_profile_id": EXECUTION_PROFILE_ID,
        "summary": summary,
        "profiles": [profile.to_dict() for profile in EXECUTION_PROFILES],
        "settlement_logic_evaluation": {
            "scope": "prediction-market concepts reevaluated for crypto perpetual VNEDGE lanes",
            "crypto_perp_settlement_model": (
                "continuous mark-to-market PnL, explicit exits, funding/fee cashflows, "
                "margin/liquidation constraints; no terminal binary payoff"
            ),
            "components": settlement,
            "portable_components": [
                row["component_id"] for row in settlement if bool(row.get("portable"))
            ],
            "blocked_components": [
                row["component_id"] for row in settlement if not bool(row.get("portable"))
            ],
        },
        "rows": [row.to_dict() for row in rows[:max_rows]],
        "execution_ready_rows": [
            row.to_dict()
            for row in rows
            if row.execution_truth_ready
        ][:25],
        "paper_blocked_rows": [
            row.to_dict()
            for row in rows
            if row.strict_economic_edge and row.requires_execution_replay_before_paper
        ][:25],
        "operator_answer": _operator_answer(summary),
        "policy": {
            "research_only": True,
            "can_trade": False,
            "can_promote": False,
            "live_orders_enabled": False,
            "strict_min_net_bps": STRICT_MIN_NET_BPS,
            "strict_min_profit_factor": STRICT_MIN_PROFIT_FACTOR,
            "strict_min_samples": STRICT_MIN_SAMPLES,
            "paper_rule": (
                "economic edge is not enough; paper/shadow requires L3/L4 execution "
                "truth or an explicit human-approved taker-only exception"
            ),
            "settlement_rule": (
                "prediction-market terminal settlement logic is blocked for crypto "
                "perps; only ledger/coverage/portfolio-scoring concepts are portable"
            ),
        },
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
    }


def publish_execution_replay_profile(
    payload: dict[str, Any],
    *,
    out: Path | str = DEFAULT_OUT,
    feed: Path | str | None = DEFAULT_FEED,
) -> Path:
    out_path = Path(out)
    _atomic_write_json(out_path, payload)
    if feed is not None:
        feed_path = Path(feed)
        feed_path.parent.mkdir(parents=True, exist_ok=True)
        with feed_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_feed_row(payload), sort_keys=True) + "\n")
        feed_path.chmod(0o644)
    return out_path


def _profile_row(
    row: dict[str, Any],
    *,
    candidate_replay: dict[str, Any],
    fee_wall: dict[str, Any],
) -> ExecutionEvidenceRow:
    profile = _classify_profile(row, candidate_replay=candidate_replay, fee_wall=fee_wall)
    definition = PROFILE_BY_ID[profile]
    avg = _float(row.get("avg_net_bps"))
    pf = _float(row.get("profit_factor"))
    samples = _int(row.get("samples"))
    strict = (
        avg is not None
        and avg >= STRICT_MIN_NET_BPS
        and pf is not None
        and pf >= STRICT_MIN_PROFIT_FACTOR
        and samples >= STRICT_MIN_SAMPLES
    )
    settlement_portability, settlement_blockers = _settlement_risk(row)
    blockers = _blockers(
        row,
        profile=definition,
        strict=strict,
        settlement_blockers=settlement_blockers,
    )
    execution_ready = strict and definition.rank >= 3 and not settlement_blockers
    requires_replay = strict and definition.rank < 3 and not settlement_blockers
    return ExecutionEvidenceRow(
        row_id=_row_id(row),
        source_kind=str(row.get("source_kind") or ""),
        source_artifact=str(row.get("source_artifact") or ""),
        strategy_id=str(row.get("strategy_id") or ""),
        exchange=str(row.get("exchange") or ""),
        symbol=str(row.get("symbol") or ""),
        timeframe=str(row.get("timeframe") or ""),
        verdict=str(row.get("verdict") or ""),
        status=str(row.get("status") or ""),
        samples=samples,
        avg_net_bps=avg,
        profit_factor=pf,
        win_rate_pct=_float(row.get("win_rate_pct")),
        route=str(row.get("route") or row.get("mode") or ""),
        profile_id=definition.profile_id,
        profile_rank=definition.rank,
        fill_evidence=definition.fill_evidence,
        latency_model=definition.latency_model,
        settlement_model="crypto_perp_mark_to_market_plus_funding",
        settlement_portability=settlement_portability,
        strict_economic_edge=strict,
        execution_truth_ready=execution_ready,
        requires_execution_replay_before_paper=requires_replay,
        blockers=blockers,
        next_action=_next_action(profile=definition, strict=strict, blockers=blockers),
        metadata={
            "record_id": row.get("record_id"),
            "failure_mode": row.get("failure_mode"),
            "next_action": row.get("next_action"),
            "candidate_id": _nested(row, "metadata", "candidate_id"),
            "script_id": _nested(row, "metadata", "script_id"),
        },
    )


def _classify_profile(
    row: dict[str, Any],
    *,
    candidate_replay: dict[str, Any],
    fee_wall: dict[str, Any],
) -> ProfileId:
    source = str(row.get("source_kind") or "").lower()
    artifact = str(row.get("source_artifact") or "").lower()
    route = f"{row.get('route') or ''} {row.get('mode') or ''}".upper()
    verdict = str(row.get("verdict") or "").upper()

    if source == "candidate_replay" or "candidate_replay" in artifact:
        if bool((candidate_replay.get("config") or {}).get("queue_aware")):
            return "L4_L2_QUEUE_AWARE_MAKER_REPLAY"
        return "L3_L2_TRADE_THROUGH_REPLAY"
    if source in {"event_taker_replay", "filtered_replay"} or "TAKER" in route:
        return "L2_NEXT_TRADE_TAKER_REPLAY"
    if source in {"fee_wall_forensics", "scanner_uplift", "scanner_tournament"}:
        # Fee-wall forensics can prove the path existed after a closed-bar event,
        # but its candle rows still need the execution replay bridge.
        _ = fee_wall
        return "L1_CANDLE_FORWARD_ROUTE_LABEL"
    if source in {"alpha_arena", "contract_matrix"}:
        return "L1_CANDLE_FORWARD_ROUTE_LABEL" if "EDGE" in verdict else "L0_SOURCE_OR_VISUAL_ONLY"
    return "L0_SOURCE_OR_VISUAL_ONLY"


def _settlement_risk(row: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
    blob = json.dumps(row, sort_keys=True, default=str).lower()
    blockers: list[str] = []
    if any(token in blob for token in ("hold_to_resolution", "to resolution", "settlement")):
        blockers.append("prediction_market_hold_to_resolution_not_portable_to_perps")
    if any(token in blob for token in ("binary_pair", "pair_arbitrage", "complementary")):
        blockers.append("prediction_market_complementary_pair_math_not_portable_to_single_perp")
    if "maker_rebate" in blob and "fee_profile" not in blob:
        blockers.append("maker_rebate_requires_actual_crypto_exchange_fee_tier")
    if blockers:
        return "BLOCKED_PREDICTION_MARKET_ASSUMPTION", tuple(blockers)
    return "PERP_MARK_TO_MARKET_SAFE", ()


def _blockers(
    row: dict[str, Any],
    *,
    profile: ExecutionProfileDefinition,
    strict: bool,
    settlement_blockers: tuple[str, ...],
) -> tuple[str, ...]:
    blockers = list(settlement_blockers)
    if strict and profile.rank < 3:
        blockers.append("strict_economic_edge_needs_tick_or_l2_execution_replay_before_paper")
    if profile.rank == 0:
        blockers.append("no_execution_fill_evidence")
    if profile.rank == 1:
        blockers.append("candle_forward_label_is_not_order_fill_evidence")
    if profile.rank == 2:
        blockers.append("taker_replay_must_clear_live_route_and_size_checks")
    if profile.rank == 3:
        blockers.append("maker_trade_through_replay_lacks_full_queue_position")
    if not strict:
        avg = _float(row.get("avg_net_bps"))
        samples = _int(row.get("samples"))
        pf = _float(row.get("profit_factor"))
        if avg is None:
            blockers.append("no_completed_net_bps")
        elif avg < STRICT_MIN_NET_BPS:
            blockers.append("net_bps_below_strict_fee_wall_floor")
        if pf is None or pf < STRICT_MIN_PROFIT_FACTOR:
            blockers.append("profit_factor_below_strict_floor")
        if samples < STRICT_MIN_SAMPLES:
            blockers.append("sample_count_below_strict_floor")
    return tuple(dict.fromkeys(blockers))


def _next_action(
    *,
    profile: ExecutionProfileDefinition,
    strict: bool,
    blockers: tuple[str, ...],
) -> str:
    if "prediction_market_hold_to_resolution_not_portable_to_perps" in blockers:
        return "REWRITE_WITH_PERP_EXITS_AND_REPLAY"
    if strict and profile.rank < 3:
        return "RUN_EXECUTION_REPLAY_PROFILE_L3_OR_L4"
    if strict and profile.rank >= 3:
        return "PRE_REGISTER_UNTOUCHED_WINDOW_THEN_PAPER_REVIEW"
    if profile.rank <= 1:
        return "PORT_CAUSAL_FEATURES_THEN_EXECUTION_REPLAY"
    return "EXPAND_SAMPLE_OR_REPAIR_EXIT_BEFORE_PROMOTION"


def _summary(rows: list[ExecutionEvidenceRow]) -> dict[str, Any]:
    profiles = Counter(row.profile_id for row in rows)
    sources = Counter(row.source_kind for row in rows)
    strict = [row for row in rows if row.strict_economic_edge]
    ready = [row for row in rows if row.execution_truth_ready]
    paper_blocked = [row for row in rows if row.requires_execution_replay_before_paper]
    settlement_blocked = [
        row
        for row in rows
        if row.settlement_portability == "BLOCKED_PREDICTION_MARKET_ASSUMPTION"
    ]
    return {
        "records": len(rows),
        "source_counts": dict(sorted(sources.items())),
        "profile_counts": dict(sorted(profiles.items())),
        "strict_economic_rows": len(strict),
        "execution_truth_ready": len(ready),
        "requires_execution_replay_before_paper": len(paper_blocked),
        "settlement_blocked_rows": len(settlement_blocked),
        "l3_or_l4_rows": sum(1 for row in rows if row.profile_rank >= 3),
        "candle_or_visual_only_rows": sum(1 for row in rows if row.profile_rank <= 1),
        "best_execution_ready_bps": _best(row.avg_net_bps for row in ready),
        "best_strict_bps": _best(row.avg_net_bps for row in strict),
        "can_trade": False,
        "can_promote": False,
    }


def _operator_answer(summary: dict[str, Any]) -> str:
    strict = int(summary.get("strict_economic_rows") or 0)
    ready = int(summary.get("execution_truth_ready") or 0)
    blocked = int(summary.get("requires_execution_replay_before_paper") or 0)
    settlement = int(summary.get("settlement_blocked_rows") or 0)
    if ready:
        return (
            f"{ready} row(s) have strict economics plus L3/L4 execution truth. "
            "They still require untouched-window judgment and human approval."
        )
    if strict and blocked:
        return (
            f"{strict} strict economic row(s) exist, but {blocked} still need "
            "tick/L2 execution replay before paper. Do not trust candle/TV-style "
            "fills as executable edge."
        )
    if settlement:
        return (
            f"{settlement} row(s) contain prediction-market settlement assumptions "
            "that must be rewritten for crypto perp mark-to-market exits."
        )
    return (
        "No execution-truth-ready scanner row yet. Use this report to route "
        "positive candle evidence into taker or queue-aware maker replay."
    )


def _evidence_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    records = payload.get("records")
    if isinstance(records, list):
        return [dict(row) for row in records if isinstance(row, dict)]
    rows: list[dict[str, Any]] = []
    for key in ("fee_wall_breakers", "sparse_positives", "top_positive"):
        value = payload.get(key)
        if isinstance(value, list):
            rows.extend(dict(row) for row in value if isinstance(row, dict))
    return rows


def _row_rank(row: ExecutionEvidenceRow) -> tuple[float, float, int, int, str]:
    avg = row.avg_net_bps if row.avg_net_bps is not None else -1_000_000.0
    pf = min(row.profit_factor if row.profit_factor is not None else -1.0, 999.0)
    return (float(row.execution_truth_ready), avg, pf, row.samples, row.row_id)


def _row_id(row: dict[str, Any]) -> str:
    raw = "|".join(
        str(row.get(key) or "")
        for key in (
            "record_id",
            "source_kind",
            "strategy_id",
            "exchange",
            "symbol",
            "timeframe",
            "verdict",
        )
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _nested(row: dict[str, Any], key: str, nested_key: str) -> Any:
    nested = row.get(key)
    if isinstance(nested, dict):
        return nested.get(nested_key)
    return None


def _best(values: Iterable[float | None]) -> float | None:
    numeric = [float(value) for value in values if value is not None and math.isfinite(value)]
    if not numeric:
        return None
    return round(max(numeric), 4)


def _float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return round(parsed, 6)


def _int(value: Any) -> int:
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    with NamedTemporaryFile(
        "w",
        dir=path.parent,
        prefix=path.name,
        suffix=".tmp",
        delete=False,
        encoding="utf-8",
    ) as tmp:
        tmp.write(encoded)
        tmp_path = Path(tmp.name)
    tmp_path.chmod(0o644)
    tmp_path.replace(path)
    path.chmod(0o644)


def _feed_row(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "execution_profile_id": payload.get("execution_profile_id"),
        "generated_at": payload.get("generated_at"),
        "summary": payload.get("summary", {}),
        "can_trade": False,
        "can_promote": False,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="publish VNEDGE execution-realistic replay profile"
    )
    parser.add_argument("--evidence-index", default=str(DEFAULT_EVIDENCE_INDEX))
    parser.add_argument("--fee-wall", default=str(DEFAULT_FEE_WALL))
    parser.add_argument("--candidate-replay", default=str(DEFAULT_CANDIDATE_REPLAY))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--feed", default=str(DEFAULT_FEED))
    parser.add_argument("--no-feed", action="store_true")
    parser.add_argument("--max-rows", type=int, default=250)
    parser.add_argument("--interval-seconds", type=int, default=0)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    while True:
        payload = build_execution_replay_profile_report(
            evidence_index=_read_json(Path(args.evidence_index)),
            fee_wall=_read_json(Path(args.fee_wall)),
            candidate_replay=_read_json(Path(args.candidate_replay)),
            max_rows=args.max_rows,
        )
        publish_execution_replay_profile(
            payload,
            out=args.out,
            feed=None if args.no_feed else args.feed,
        )
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            summary = payload["summary"]
            print(
                "execution replay profile published: "
                f"records={summary.get('records', 0)} "
                f"strict={summary.get('strict_economic_rows', 0)} "
                f"ready={summary.get('execution_truth_ready', 0)} "
                f"needs_replay={summary.get('requires_execution_replay_before_paper', 0)}",
                flush=True,
            )
        if args.interval_seconds <= 0:
            break
        time.sleep(max(30, args.interval_seconds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
