"""Result arbiter for the Quantified Strategy Lab proof matrix.

The blueprint proof publisher answers "what happened in every cell?"  This
module answers the operator question that follows: "what do we do next?"

It is deliberately deterministic and research-only. It never grants paper/live
permission; even proof-grade rows only become untouched-window judgment tasks.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
import math
from pathlib import Path
from tempfile import NamedTemporaryFile
import time
from typing import Any, Literal, Mapping

from vnedge.research.quantified_blueprint_proof import DEFAULT_OUT as DEFAULT_PROOF


QUANTIFIED_PROOF_RESULT_ARBITER_ID = "quantified_proof_result_arbiter_v1"
DEFAULT_OUT = Path("research/live_research/quantified_proof_result_arbiter_latest.json")
DEFAULT_FEED = Path("research/live_research/quantified_proof_result_arbiter_feed.jsonl")

DecisionBucket = Literal[
    "READY_FOR_UNTOUCHED_JUDGMENT",
    "PROXY_EDGE_NEEDS_CANONICAL_PORT",
    "EXTEND_SPARSE_POSITIVE",
    "EXIT_ROUTE_UPLIFT",
    "FEE_WALL_NEAR_MISS",
    "DATA_REPAIR",
    "REPLAY_REPAIR",
    "METRICS_REPAIR",
    "AWAITING_BACKTEST",
    "NO_TRADE_RESEARCH",
    "NEGATIVE_REJECT",
]


@dataclass(frozen=True)
class QuantifiedProofArbiterConfig:
    min_net_bps: float = 25.0
    min_profit_factor: float = 1.50
    min_trades: int = 20
    near_miss_floor_bps: float = -10.0
    max_actions: int = 80

    def __post_init__(self) -> None:
        if self.min_net_bps <= 0:
            raise ValueError("min_net_bps must be positive")
        if self.min_profit_factor < 1.0:
            raise ValueError("min_profit_factor must be >= 1")
        if self.min_trades < 1:
            raise ValueError("min_trades must be positive")
        if self.max_actions < 1:
            raise ValueError("max_actions must be positive")


@dataclass(frozen=True)
class ArbiterAction:
    rank: int
    action_id: str
    bucket: DecisionBucket
    priority: int
    next_action: str
    use_as: str
    rationale: str
    port_id: str
    exchange: str
    symbol: str
    timeframe: str
    strategy_id: str
    setup_mode: str
    adapter: str
    canonical_adapter: bool
    status: str
    verdict: str
    samples: int
    avg_net_bps: float | None
    required_uplift_bps: float | None
    profit_factor: float | None
    win_rate_pct: float | None
    can_trade: bool = False
    can_promote: bool = False
    live_orders_enabled: bool = False
    requires_untouched_judgment: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_quantified_proof_result_arbiter_payload(
    *,
    proof_payload: Mapping[str, Any] | None = None,
    proof_path: Path | str | None = DEFAULT_PROOF,
    config: QuantifiedProofArbiterConfig | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    generated = now or datetime.now(UTC)
    if proof_payload is not None:
        proof = dict(proof_payload)
    elif proof_path is not None:
        proof = _read_json(Path(proof_path))
    else:
        proof = {}
    cfg = config or _config_from_proof(proof)
    raw_rows = proof.get("rows")
    rows = tuple(row for row in raw_rows if isinstance(row, dict)) if isinstance(raw_rows, list) else ()
    all_actions = _rank_actions(
        (_action_for_row(row, cfg) for row in rows),
        max(len(rows), 1),
    )
    action_queue = all_actions[: cfg.max_actions]
    port_summary = _port_summary(all_actions, rows)
    summary = _summary(all_actions, rows, port_summary, cfg, proof)
    return {
        "arbiter_id": QUANTIFIED_PROOF_RESULT_ARBITER_ID,
        "generated_at": generated.isoformat(),
        "source": {
            "proof_id": proof.get("proof_id") or "missing",
            "proof_generated_at": proof.get("generated_at"),
            "proof_path": str(proof_path or "inline_payload"),
        },
        "config": asdict(cfg),
        "summary": summary,
        "port_summary": port_summary,
        "action_queue": [action.to_dict() for action in action_queue],
        "operator_answer": _operator_answer(summary, action_queue),
        "policy": _policy(),
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
    }


def publish_quantified_proof_result_arbiter(
    payload: dict[str, Any],
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


def load_quantified_proof_result_arbiter_payload(path: Path | None = None) -> dict:
    if path is not None and path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {}
        if (
            isinstance(payload, dict)
            and payload.get("arbiter_id") == QUANTIFIED_PROOF_RESULT_ARBITER_ID
        ):
            return payload
    return build_quantified_proof_result_arbiter_payload()


def _action_for_row(row: Mapping[str, Any], config: QuantifiedProofArbiterConfig) -> ArbiterAction:
    bucket = _bucket(row, config)
    next_action, use_as = _action_and_use(row, bucket)
    avg = _float(row.get("avg_net_bps"))
    required = None if avg is None else round(max(0.0, config.min_net_bps - avg), 4)
    return ArbiterAction(
        rank=0,
        action_id=_action_id(row, bucket),
        bucket=bucket,
        priority=_priority(row, bucket, config),
        next_action=next_action,
        use_as=use_as,
        rationale=_rationale(row, bucket, required, config),
        port_id=str(row.get("port_id") or "unknown_port"),
        exchange=str(row.get("exchange") or "unknown_exchange"),
        symbol=str(row.get("symbol") or "unknown_symbol"),
        timeframe=str(row.get("timeframe") or "unknown_tf"),
        strategy_id=str(row.get("strategy_id") or "unknown_strategy"),
        setup_mode=str(row.get("setup_mode") or ""),
        adapter=str(row.get("adapter") or ""),
        canonical_adapter=bool(row.get("canonical_adapter", True)),
        status=str(row.get("status") or ""),
        verdict=str(row.get("verdict") or ""),
        samples=_int(row.get("samples")),
        avg_net_bps=avg,
        required_uplift_bps=required,
        profit_factor=_float(row.get("profit_factor")),
        win_rate_pct=_float(row.get("win_rate_pct")),
    )


def _bucket(row: Mapping[str, Any], config: QuantifiedProofArbiterConfig) -> DecisionBucket:
    status = str(row.get("status") or "")
    verdict = str(row.get("verdict") or "")
    canonical = bool(row.get("canonical_adapter", True))
    avg = _float(row.get("avg_net_bps"))
    pf = _float(row.get("profit_factor"))
    samples = _int(row.get("samples"))
    clears_edge = (
        avg is not None
        and pf is not None
        and avg >= config.min_net_bps
        and pf >= config.min_profit_factor
    )
    if verdict == "BLOCKED_DATA_OR_CONTRACT" or "BLOCKED" in status:
        return "DATA_REPAIR"
    if verdict == "FAILED_REPLAY_ENGINE" or "FAILED" in status:
        return "REPLAY_REPAIR"
    if verdict == "INCOMPLETE_METRICS":
        return "METRICS_REPAIR"
    if not canonical and clears_edge:
        return "PROXY_EDGE_NEEDS_CANONICAL_PORT"
    if verdict == "PROMOTABLE_PROOF_REQUIRES_UNTOUCHED_JUDGMENT":
        return "READY_FOR_UNTOUCHED_JUDGMENT"
    if verdict == "SPARSE_POSITIVE_EXTEND_SAMPLE" or (
        clears_edge and samples < config.min_trades
    ):
        return "EXTEND_SPARSE_POSITIVE"
    if verdict == "POSITIVE_BUT_FEE_WALL_THIN":
        return "EXIT_ROUTE_UPLIFT"
    if verdict == "FEE_WALL_NEAR_MISS" or (
        avg is not None and config.near_miss_floor_bps < avg <= 0.0
    ):
        return "FEE_WALL_NEAR_MISS"
    if verdict == "AWAITING_BACKTEST" or "PENDING" in status or "RUNNING" in status:
        return "AWAITING_BACKTEST"
    if verdict == "NO_TRADES":
        return "NO_TRADE_RESEARCH"
    return "NEGATIVE_REJECT"


def _action_and_use(row: Mapping[str, Any], bucket: DecisionBucket) -> tuple[str, str]:
    if bucket == "READY_FOR_UNTOUCHED_JUDGMENT":
        return "QUEUE_UNTOUCHED_WINDOW_JUDGMENT", "candidate_evidence"
    if bucket == "PROXY_EDGE_NEEDS_CANONICAL_PORT":
        return "BUILD_CANONICAL_PORT_BEFORE_JUDGMENT", "port_build_spec"
    if bucket == "EXTEND_SPARSE_POSITIVE":
        return "EXTEND_SAMPLE_ON_NEXT_UNTOUCHED_WINDOW", "sample_expansion"
    if bucket == "EXIT_ROUTE_UPLIFT":
        return "TEST_TP1_BE_TRAIL_AND_MAKER_FIRST_FILTERS", "exit_route_experiment"
    if bucket == "FEE_WALL_NEAR_MISS":
        return "MINE_EXIT_CAPTURE_AND_ROUTE_FILTERS", "fee_wall_salvage"
    if bucket == "DATA_REPAIR":
        return "REPAIR_DATA_COVERAGE_OR_SYMBOL_MAPPING", "infrastructure_fix"
    if bucket == "REPLAY_REPAIR":
        return "REPAIR_REPLAY_ENGINE_OR_STRATEGY_CONTRACT", "engine_fix"
    if bucket == "METRICS_REPAIR":
        return "REPAIR_MISSING_METRICS", "evidence_quality"
    if bucket == "AWAITING_BACKTEST":
        return "WAIT_FOR_AGENT_JOB_RUNNER", "queue_monitoring"
    if bucket == "NO_TRADE_RESEARCH":
        return "REVIEW_TRIGGER_DENSITY_WITHOUT_RELAXING_FEE_GATE", "density_diagnostic"
    return "REJECT_OR_RECYCLE_AS_CONTEXT_FEATURE", "negative_evidence"


def _priority(
    row: Mapping[str, Any],
    bucket: DecisionBucket,
    config: QuantifiedProofArbiterConfig,
) -> int:
    base = {
        "READY_FOR_UNTOUCHED_JUDGMENT": 1000,
        "PROXY_EDGE_NEEDS_CANONICAL_PORT": 940,
        "EXTEND_SPARSE_POSITIVE": 880,
        "EXIT_ROUTE_UPLIFT": 760,
        "FEE_WALL_NEAR_MISS": 700,
        "DATA_REPAIR": 620,
        "REPLAY_REPAIR": 580,
        "METRICS_REPAIR": 540,
        "NO_TRADE_RESEARCH": 360,
        "AWAITING_BACKTEST": 240,
        "NEGATIVE_REJECT": 120,
    }[bucket]
    avg = _float(row.get("avg_net_bps"))
    pf = _float(row.get("profit_factor"))
    samples = _int(row.get("samples"))
    edge_bonus = 0 if avg is None else int(max(-100.0, min(100.0, avg)) * 2)
    pf_bonus = 0 if pf is None else int(max(0.0, min(3.0, pf)) * 30)
    sample_bonus = min(samples, config.min_trades) * 2
    return base + edge_bonus + pf_bonus + sample_bonus


def _rank_actions(actions: Any, limit: int) -> list[ArbiterAction]:
    ranked = sorted(actions, key=lambda row: (row.priority, row.port_id), reverse=True)
    return [
        ArbiterAction(**{**action.to_dict(), "rank": idx})
        for idx, action in enumerate(ranked[:limit], start=1)
    ]


def _summary(
    actions: list[ArbiterAction],
    rows: tuple[Mapping[str, Any], ...],
    port_summary: list[dict[str, Any]],
    config: QuantifiedProofArbiterConfig,
    proof: Mapping[str, Any],
) -> dict[str, Any]:
    buckets = Counter(action.bucket for action in actions)
    statuses = Counter(str(row.get("status") or "") for row in rows)
    verdicts = Counter(str(row.get("verdict") or "") for row in rows)
    completed = sum(1 for row in rows if str(row.get("status") or "") == "DONE_RESEARCH_ONLY")
    top = actions[0] if actions else None
    return {
        "total_cells": len(rows),
        "completed_cells": completed,
        "ports": len(port_summary),
        "actionable_cells": sum(
            buckets[key]
            for key in (
                "READY_FOR_UNTOUCHED_JUDGMENT",
                "PROXY_EDGE_NEEDS_CANONICAL_PORT",
                "EXTEND_SPARSE_POSITIVE",
                "EXIT_ROUTE_UPLIFT",
                "FEE_WALL_NEAR_MISS",
                "DATA_REPAIR",
                "REPLAY_REPAIR",
                "METRICS_REPAIR",
            )
        ),
        "ready_for_judgment": buckets["READY_FOR_UNTOUCHED_JUDGMENT"],
        "proxy_edges": buckets["PROXY_EDGE_NEEDS_CANONICAL_PORT"],
        "sparse_positives": buckets["EXTEND_SPARSE_POSITIVE"],
        "exit_route_uplifts": buckets["EXIT_ROUTE_UPLIFT"],
        "fee_wall_near_misses": buckets["FEE_WALL_NEAR_MISS"],
        "data_repairs": buckets["DATA_REPAIR"],
        "replay_repairs": buckets["REPLAY_REPAIR"],
        "metric_repairs": buckets["METRICS_REPAIR"],
        "awaiting_backtest": buckets["AWAITING_BACKTEST"],
        "no_trade_rows": buckets["NO_TRADE_RESEARCH"],
        "negative_rejects": buckets["NEGATIVE_REJECT"],
        "bucket_counts": dict(buckets),
        "status_counts": dict(statuses),
        "verdict_counts": dict(verdicts),
        "top_action": None if top is None else top.to_dict(),
        "source_proof_summary": proof.get("summary") or {},
        "gate": {
            "min_net_bps": config.min_net_bps,
            "min_profit_factor": config.min_profit_factor,
            "min_trades": config.min_trades,
        },
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
    }


def _port_summary(
    actions: list[ArbiterAction],
    rows: tuple[Mapping[str, Any], ...],
) -> list[dict[str, Any]]:
    by_port: dict[str, list[ArbiterAction]] = defaultdict(list)
    for action in actions:
        by_port[action.port_id].append(action)
    all_ports = sorted({str(row.get("port_id") or "unknown_port") for row in rows})
    out: list[dict[str, Any]] = []
    for port in all_ports:
        port_actions = by_port.get(port, [])
        port_rows = [row for row in rows if str(row.get("port_id") or "unknown_port") == port]
        completed = [
            row for row in port_rows
            if str(row.get("status") or "") == "DONE_RESEARCH_ONLY"
        ]
        best = max(
            completed,
            key=lambda row: _float(row.get("avg_net_bps")) or -1e9,
            default={},
        )
        buckets = Counter(action.bucket for action in port_actions)
        top = port_actions[0] if port_actions else None
        out.append({
            "port_id": port,
            "total_cells": len(port_rows),
            "completed_cells": len(completed),
            "actionable_cells": sum(
                count for bucket, count in buckets.items()
                if bucket not in {"AWAITING_BACKTEST", "NEGATIVE_REJECT"}
            ),
            "ready_for_judgment": buckets["READY_FOR_UNTOUCHED_JUDGMENT"],
            "proxy_edges": buckets["PROXY_EDGE_NEEDS_CANONICAL_PORT"],
            "fee_wall_near_misses": buckets["FEE_WALL_NEAR_MISS"],
            "negative_rejects": buckets["NEGATIVE_REJECT"],
            "awaiting_backtest": buckets["AWAITING_BACKTEST"],
            "best_avg_net_bps": _float(best.get("avg_net_bps")),
            "best_profit_factor": _float(best.get("profit_factor")),
            "top_action": None if top is None else top.next_action,
            "top_bucket": None if top is None else top.bucket,
            "can_trade": False,
            "can_promote": False,
        })
    return sorted(
        out,
        key=lambda row: (
            row["ready_for_judgment"],
            row["proxy_edges"],
            row["fee_wall_near_misses"],
            row["best_avg_net_bps"] if row["best_avg_net_bps"] is not None else -1e9,
        ),
        reverse=True,
    )


def _operator_answer(summary: Mapping[str, Any], actions: list[ArbiterAction]) -> str:
    if summary["ready_for_judgment"]:
        return (
            f"{summary['ready_for_judgment']} canonical proof cell(s) are ready "
            "for untouched-window judgment; do not promote before burn-registry review."
        )
    if summary["proxy_edges"]:
        return (
            f"{summary['proxy_edges']} proxy edge cell(s) look promising; build canonical "
            "session/rotation ports before any judgment."
        )
    if summary["fee_wall_near_misses"] or summary["exit_route_uplifts"]:
        return (
            f"{summary['fee_wall_near_misses'] + summary['exit_route_uplifts']} cell(s) "
            "are close enough to mine exits, maker routing, and context filters."
        )
    if summary["data_repairs"] or summary["replay_repairs"]:
        return (
            f"{summary['data_repairs'] + summary['replay_repairs']} cell(s) need data "
            "or replay repair before the proof matrix can be trusted."
        )
    if summary["awaiting_backtest"]:
        return (
            f"{summary['awaiting_backtest']} proof cell(s) are waiting for Agent Gateway "
            "runner evidence."
        )
    if actions:
        return "Completed cells are mostly negative; recycle only as context features."
    return "No proof rows are available yet; wait for the blueprint proof publisher."


def _rationale(
    row: Mapping[str, Any],
    bucket: DecisionBucket,
    required: float | None,
    config: QuantifiedProofArbiterConfig,
) -> str:
    avg = _float(row.get("avg_net_bps"))
    pf = _float(row.get("profit_factor"))
    samples = _int(row.get("samples"))
    if bucket == "READY_FOR_UNTOUCHED_JUDGMENT":
        return (
            f"Clears {config.min_net_bps:g} bps, PF {config.min_profit_factor:g}, "
            f"and {config.min_trades} trades on canonical adapter; needs untouched proof."
        )
    if bucket == "PROXY_EDGE_NEEDS_CANONICAL_PORT":
        return "Edge clears proof math but adapter is proxy-only; canonical port is required."
    if bucket == "EXTEND_SPARSE_POSITIVE":
        return (
            f"Positive proof math with {samples} trades; extend sample without tuning "
            "before promotion review."
        )
    if bucket in {"EXIT_ROUTE_UPLIFT", "FEE_WALL_NEAR_MISS"}:
        return (
            f"Avg {avg if avg is not None else '--'} bps, PF {pf if pf is not None else '--'}; "
            f"needs {required if required is not None else '--'} bps uplift versus gate."
        )
    if bucket == "DATA_REPAIR":
        return str(row.get("blocked_reason") or "Data/contract unavailable for this cell.")
    if bucket == "REPLAY_REPAIR":
        return str(row.get("error") or "Replay engine failed on this cell.")
    if bucket == "NO_TRADE_RESEARCH":
        return "No fills/trades; diagnose trigger density and data coverage, not gate relaxation."
    if bucket == "AWAITING_BACKTEST":
        return "Queued or running; wait for durable Agent Gateway result."
    return "Negative after costs; reject as a lane and keep only as context evidence."


def _action_id(row: Mapping[str, Any], bucket: DecisionBucket) -> str:
    parts = (
        bucket,
        str(row.get("port_id") or "port"),
        str(row.get("exchange") or "exchange"),
        str(row.get("symbol") or "symbol").replace("/", "").replace(":", ""),
        str(row.get("timeframe") or "tf"),
    )
    return "|".join(parts)


def _config_from_proof(proof: Mapping[str, Any] | None) -> QuantifiedProofArbiterConfig:
    summary = (proof or {}).get("summary") if isinstance(proof, Mapping) else {}
    gate = summary.get("gate") if isinstance(summary, Mapping) else {}
    if not isinstance(gate, Mapping):
        gate = {}
    return QuantifiedProofArbiterConfig(
        min_net_bps=_float(gate.get("min_net_bps")) or 25.0,
        min_profit_factor=_float(gate.get("min_profit_factor")) or 1.50,
        min_trades=_int(gate.get("min_trades")) or 20,
    )


def _policy() -> dict[str, Any]:
    return {
        "research_only": True,
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
        "blocked_actions": [
            "auto_promote_from_backtest",
            "paper_trade_from_proxy_adapter",
            "relax_fee_wall_gate",
            "rerun_burned_window",
        ],
        "promotion_boundary": (
            "Only canonical proof-grade rows may queue untouched-window judgment. "
            "The arbiter itself has no promotion authority."
        ),
    }


def _feed_record(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "ts": payload.get("generated_at"),
        "arbiter_id": payload.get("arbiter_id"),
        "summary": payload.get("summary"),
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
    }


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _int(value: Any) -> int:
    try:
        if value is None:
            return 0
        return int(value)
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish Quantified proof result arbiter")
    parser.add_argument("--proof", type=Path, default=DEFAULT_PROOF)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--feed", type=Path, default=DEFAULT_FEED)
    parser.add_argument("--no-feed", action="store_true")
    parser.add_argument("--interval-seconds", type=float, default=0.0)
    args = parser.parse_args(argv)

    while True:
        payload = build_quantified_proof_result_arbiter_payload(proof_path=args.proof)
        publish_quantified_proof_result_arbiter(
            payload,
            out=args.out,
            feed=None if args.no_feed else args.feed,
        )
        summary = payload["summary"]
        print(
            "quantified proof arbiter "
            f"{summary['actionable_cells']} actionable / "
            f"{summary['ready_for_judgment']} judgment-ready / "
            f"{summary['fee_wall_near_misses']} fee-wall near",
            flush=True,
        )
        if args.interval_seconds <= 0:
            return 0
        time.sleep(args.interval_seconds)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
