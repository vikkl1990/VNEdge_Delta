"""Unified research evidence index for VNEDGE.

The Pine Lab, scanner tournament, fee-wall forensics, and Alpha Arena all
publish useful facts, but they live as separate JSON artifacts.  This module
normalizes those facts into one read-optimized evidence snapshot, with an
optional SQLite index for fast local queries.  It is research-only: the index
never grants trading or promotion permission.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Iterable


EVIDENCE_STORE_ID = "research_evidence_index_v1"
DEFAULT_RESEARCH_DIR = Path("research/live_research")
DEFAULT_PINE_KB = Path("research/pine_scripts/pine_research_kb.json")
DEFAULT_OUT = DEFAULT_RESEARCH_DIR / "evidence_index_latest.json"
DEFAULT_SQLITE = DEFAULT_RESEARCH_DIR / "evidence_index.sqlite"
DEFAULT_FEED = DEFAULT_RESEARCH_DIR / "evidence_index_feed.jsonl"

STRICT_MIN_NET_BPS = 25.0
STRICT_MIN_PROFIT_FACTOR = 1.50
STRICT_MIN_SAMPLES = 20


@dataclass(frozen=True)
class EvidenceRecord:
    record_id: str
    source_kind: str
    source_artifact: str
    strategy_id: str
    exchange: str
    symbol: str
    timeframe: str
    status: str
    verdict: str
    samples: int = 0
    avg_net_bps: float | None = None
    profit_factor: float | None = None
    win_rate_pct: float | None = None
    route: str = ""
    mode: str = ""
    fee_model: str = ""
    failure_mode: str = ""
    next_action: str = ""
    source_ref: str = ""
    source_hash: str = ""
    generated_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    can_trade: bool = False
    can_promote: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_research_evidence_index(
    *,
    report_dir: Path | str = DEFAULT_RESEARCH_DIR,
    pine_kb_path: Path | str | None = DEFAULT_PINE_KB,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a normalized evidence snapshot from known research artifacts."""

    generated = now or datetime.now(UTC)
    root = Path(report_dir)
    records: list[EvidenceRecord] = []
    pine_path = Path(pine_kb_path) if pine_kb_path is not None else None
    if pine_path is not None:
        records.extend(_records_from_pine_kb(_read_json(pine_path), source_artifact=pine_path.name))

    artifact_loaders = (
        ("scanner_tournament", root / "scanner_tournament_latest.json", _records_from_scanner_tournament),
        ("scanner_uplift", root / "scanner_backtest_uplift_latest.json", _records_from_scanner_uplift),
        ("alpha_arena", root / "alpha_arena_lite_latest.json", _records_from_alpha_arena),
        ("fee_wall_forensics", root / "fee_wall_forensics_latest.json", _records_from_fee_wall_forensics),
        ("contract_matrix", root / "vnedge_algo_ml_pro_contract_matrix_latest.json", _records_from_contract_matrix),
        ("candidate_replay", root / "candidate_replay_latest.json", _records_from_candidate_replay),
        ("filtered_replay", root / "filtered_replay_latest.json", _records_from_filtered_replay),
    )
    loaded_artifacts: list[dict[str, Any]] = []
    missing_artifacts: list[str] = []
    for source_kind, path, loader in artifact_loaders:
        payload = _read_json(path)
        if not payload:
            missing_artifacts.append(path.name)
            continue
        loaded_artifacts.append({"source_kind": source_kind, "artifact": path.name})
        records.extend(loader(payload, source_artifact=path.name))

    deduped = _dedupe(records)
    canonical_candidates = _canonical_strict_candidates(deduped, limit=25)
    summary = _summary(
        deduped,
        loaded_artifacts,
        missing_artifacts,
        canonical_candidates=canonical_candidates,
    )
    return {
        "evidence_store_id": EVIDENCE_STORE_ID,
        "generated_at": generated.isoformat(),
        "summary": summary,
        "records": [record.to_dict() for record in _sort_records(deduped)],
        "top_positive": [record.to_dict() for record in _top_positive(deduped, limit=12)],
        "fee_wall_breakers": [record.to_dict() for record in _fee_wall_breakers(deduped, limit=12)],
        "canonical_candidates": canonical_candidates,
        "untouched_judgment_queue": [
            candidate
            for candidate in canonical_candidates
            if candidate.get("next_action") == "PRE_REGISTER_UNTOUCHED_JUDGMENT"
        ],
        "sparse_positives": [record.to_dict() for record in _sparse_positives(deduped, limit=12)],
        "failure_clusters": _failure_clusters(deduped),
        "operator_answer": _operator_answer(summary),
        "policy": {
            "source": "normalized research artifacts only",
            "strict_min_net_bps": STRICT_MIN_NET_BPS,
            "strict_min_profit_factor": STRICT_MIN_PROFIT_FACTOR,
            "strict_min_samples": STRICT_MIN_SAMPLES,
            "promotion_rule": "index evidence is discovery only; promotion still requires causal port, fee-aware replay, and untouched-window judgment",
        },
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
    }


def publish_research_evidence_index(
    payload: dict[str, Any],
    *,
    out: Path | str = DEFAULT_OUT,
    sqlite_path: Path | str | None = DEFAULT_SQLITE,
    feed: Path | str | None = DEFAULT_FEED,
) -> Path:
    """Publish the JSON snapshot and optional SQLite side index."""

    out_path = Path(out)
    _atomic_write_json(out_path, payload)
    if sqlite_path is not None:
        write_sqlite_index(payload, sqlite_path)
    if feed is not None:
        feed_path = Path(feed)
        feed_path.parent.mkdir(parents=True, exist_ok=True)
        with feed_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_feed_record(payload), sort_keys=True) + "\n")
        feed_path.chmod(0o644)
    return out_path


def write_sqlite_index(payload: dict[str, Any], sqlite_path: Path | str) -> Path:
    """Write a compact SQLite index for operator/local research queries."""

    path = Path(sqlite_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE evidence_records (
                record_id TEXT PRIMARY KEY,
                source_kind TEXT NOT NULL,
                source_artifact TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                exchange TEXT NOT NULL,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                status TEXT NOT NULL,
                verdict TEXT NOT NULL,
                samples INTEGER NOT NULL,
                avg_net_bps REAL,
                profit_factor REAL,
                win_rate_pct REAL,
                route TEXT NOT NULL,
                mode TEXT NOT NULL,
                fee_model TEXT NOT NULL,
                failure_mode TEXT NOT NULL,
                next_action TEXT NOT NULL,
                source_ref TEXT NOT NULL,
                source_hash TEXT NOT NULL,
                generated_at TEXT NOT NULL,
                can_trade INTEGER NOT NULL,
                can_promote INTEGER NOT NULL,
                metadata_json TEXT NOT NULL
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO evidence_records VALUES (
                :record_id, :source_kind, :source_artifact, :strategy_id,
                :exchange, :symbol, :timeframe, :status, :verdict, :samples,
                :avg_net_bps, :profit_factor, :win_rate_pct, :route, :mode,
                :fee_model, :failure_mode, :next_action, :source_ref,
                :source_hash, :generated_at, :can_trade, :can_promote,
                :metadata_json
            )
            """,
            [_sqlite_row(row) for row in payload.get("records", []) if isinstance(row, dict)],
        )
        conn.execute("CREATE INDEX idx_evidence_edge ON evidence_records(avg_net_bps, profit_factor, samples)")
        conn.execute("CREATE INDEX idx_evidence_lane ON evidence_records(strategy_id, exchange, symbol, timeframe)")
        conn.execute("CREATE INDEX idx_evidence_source ON evidence_records(source_kind, source_artifact)")
    path.chmod(0o644)
    return path


def _records_from_pine_kb(payload: dict[str, Any], *, source_artifact: str) -> list[EvidenceRecord]:
    rows: list[EvidenceRecord] = []
    generated_at = str(payload.get("generated_at") or "")
    for script in payload.get("records") or []:
        if not isinstance(script, dict):
            continue
        script_id = str(script.get("script_id") or "")
        for cell in script.get("backtests") or []:
            if not isinstance(cell, dict):
                continue
            strategy_id = str(cell.get("tested_strategy") or cell.get("recommended_port") or script_id)
            status = _status_from_cell(cell)
            rows.append(
                _record(
                    source_kind="pine_script_backtest",
                    source_artifact=source_artifact,
                    strategy_id=strategy_id,
                    exchange=_first(cell.get("venues")) or str(cell.get("exchange") or ""),
                    symbol=str(cell.get("tested_symbol") or cell.get("symbol") or ""),
                    timeframe=str(cell.get("timeframe") or ""),
                    status=status,
                    verdict=str(cell.get("verdict") or status.upper()),
                    samples=_int(cell.get("samples")),
                    avg_net_bps=_float(cell.get("avg_net_bps")),
                    profit_factor=_float(cell.get("profit_factor")),
                    win_rate_pct=_float(cell.get("win_rate_pct")),
                    failure_mode=str(cell.get("blocker") or ""),
                    next_action=str(script.get("next_action") or ""),
                    source_ref=str(cell.get("evidence_source") or ""),
                    source_hash=str(script.get("source_sha256") or ""),
                    generated_at=str(cell.get("updated_at") or generated_at),
                    metadata={
                        "script_id": script_id,
                        "title": script.get("title"),
                        "url": script.get("url"),
                        "crypto_portability": script.get("crypto_portability"),
                        "source_available": bool(script.get("source_available")),
                    },
                )
            )
    return rows


def _records_from_scanner_tournament(payload: dict[str, Any], *, source_artifact: str) -> list[EvidenceRecord]:
    generated_at = str(payload.get("generated_at") or "")
    rows: list[EvidenceRecord] = []
    for row in payload.get("candidates") or []:
        if not isinstance(row, dict):
            continue
        rows.append(
            _record(
                source_kind="scanner_tournament",
                source_artifact=source_artifact,
                strategy_id=str(row.get("strategy_id") or ""),
                exchange=str(row.get("exchange") or ""),
                symbol=str(row.get("symbol") or ""),
                timeframe=str(row.get("timeframe") or ""),
                status=_status_from_net_and_verdict(_float(row.get("avg_selected_net_bps")), str(row.get("verdict") or "")),
                verdict=str(row.get("verdict") or ""),
                samples=_int(row.get("routed") or row.get("opportunities")),
                avg_net_bps=_float(row.get("avg_selected_net_bps")),
                profit_factor=_float(row.get("profit_factor")),
                win_rate_pct=_float(row.get("win_rate_pct")),
                route=str(row.get("dominant_route") or ""),
                failure_mode=str(row.get("primary_blocker") or row.get("verdict") or ""),
                next_action=str(row.get("recommended_action") or ""),
                generated_at=generated_at,
                metadata={
                    "candidate_id": row.get("candidate_id"),
                    "score": row.get("score"),
                    "avg_mfe_bps": row.get("avg_mfe_bps"),
                    "avg_mae_bps": row.get("avg_mae_bps"),
                    "action_counts": row.get("action_counts"),
                },
            )
        )
    return rows


def _records_from_scanner_uplift(payload: dict[str, Any], *, source_artifact: str) -> list[EvidenceRecord]:
    generated_at = str(payload.get("generated_at") or "")
    rows: list[EvidenceRecord] = []
    for row in payload.get("top_uplifts") or []:
        if not isinstance(row, dict):
            continue
        failure = str(row.get("failure_mode") or "")
        rows.append(
            _record(
                source_kind="scanner_uplift",
                source_artifact=source_artifact,
                strategy_id=str(row.get("strategy_id") or ""),
                exchange=str(row.get("exchange") or ""),
                symbol=str(row.get("symbol") or ""),
                timeframe=str(row.get("timeframe") or ""),
                status=_status_from_net_and_verdict(_float(row.get("avg_net_bps")), failure),
                verdict=failure,
                samples=_int(row.get("samples")),
                avg_net_bps=_float(row.get("avg_net_bps")),
                profit_factor=_float(row.get("profit_factor")),
                win_rate_pct=_float(row.get("win_rate_pct")),
                mode=str(row.get("mode") or ""),
                failure_mode=failure,
                next_action=str(row.get("uplift_action") or ""),
                generated_at=generated_at,
                metadata={
                    "row_id": row.get("row_id"),
                    "visual_avg_bps": row.get("visual_avg_bps"),
                    "required_uplift_bps": row.get("required_uplift_bps"),
                    "fee_drag_bps": row.get("fee_drag_bps"),
                    "use_as": row.get("use_as"),
                    "rationale": row.get("rationale"),
                },
            )
        )
    return rows


def _records_from_alpha_arena(payload: dict[str, Any], *, source_artifact: str) -> list[EvidenceRecord]:
    generated_at = str(payload.get("generated_at") or "")
    rows: list[EvidenceRecord] = []
    for card in payload.get("scorecards") or []:
        if not isinstance(card, dict):
            continue
        metrics = card.get("metrics") if isinstance(card.get("metrics"), dict) else {}
        rows.append(
            _record(
                source_kind="alpha_arena",
                source_artifact=source_artifact,
                strategy_id=str(card.get("strategy_id") or ""),
                exchange=str(card.get("exchange") or ""),
                symbol=str(card.get("symbol") or ""),
                timeframe=",".join(str(tf) for tf in card.get("timeframes") or []) or "",
                status=_arena_status(str(card.get("arena_verdict") or "")),
                verdict=str(card.get("arena_verdict") or ""),
                samples=_int(metrics.get("max_samples") or metrics.get("aggregate_samples")),
                avg_net_bps=_float(metrics.get("top_avg_net_bps")),
                profit_factor=_float(metrics.get("best_profit_factor")),
                win_rate_pct=_float(metrics.get("win_rate_pct")),
                mode=str(metrics.get("dominant_mode") or ""),
                failure_mode=",".join((card.get("failure_modes") or {}).keys())
                if isinstance(card.get("failure_modes"), dict)
                else "",
                next_action=str(card.get("next_action") or ""),
                generated_at=generated_at,
                metadata={
                    "candidate_id": card.get("candidate_id"),
                    "task_id": card.get("task_id"),
                    "arena_score": card.get("arena_score"),
                    "gate_checks": card.get("gate_checks"),
                    "untouched_window_plan": card.get("untouched_window_plan"),
                },
            )
        )
    return rows


def _records_from_fee_wall_forensics(payload: dict[str, Any], *, source_artifact: str) -> list[EvidenceRecord]:
    rows: list[EvidenceRecord] = []
    generated_at = str(payload.get("generated_at") or "")
    for row in _payload_rows(payload):
        summary = row.get("summary") if isinstance(row.get("summary"), dict) else {}
        verdict = str(summary.get("verdict") or row.get("verdict") or "")
        rows.append(
            _record(
                source_kind="fee_wall_forensics",
                source_artifact=source_artifact,
                strategy_id=str(row.get("strategy") or row.get("strategy_id") or ""),
                exchange=str(row.get("exchange") or ""),
                symbol=str(row.get("symbol") or ""),
                timeframe=str(row.get("timeframe") or ""),
                status=_status_from_net_and_verdict(_float(summary.get("avg_selected_net_bps")), verdict),
                verdict=verdict,
                samples=_int(summary.get("routed") or row.get("opportunity_count")),
                avg_net_bps=_float(summary.get("avg_selected_net_bps")),
                profit_factor=_float(summary.get("profit_factor")),
                win_rate_pct=_float(summary.get("win_rate_pct")),
                route=str(summary.get("dominant_route") or row.get("route") or ""),
                failure_mode=str(summary.get("primary_blocker") or ""),
                next_action=str(summary.get("next_action") or ""),
                generated_at=str(row.get("generated_at") or generated_at),
                metadata={
                    "opportunity_count": row.get("opportunity_count"),
                    "exit_diagnosis_counts": summary.get("exit_diagnosis_counts"),
                    "maker_first": summary.get("maker_first"),
                    "taker_allowed": summary.get("taker_allowed"),
                },
            )
        )
    return rows


def _records_from_contract_matrix(payload: dict[str, Any], *, source_artifact: str) -> list[EvidenceRecord]:
    generated_at = str(payload.get("generated_at") or "")
    rows: list[EvidenceRecord] = []
    for row in payload.get("rows") or []:
        if not isinstance(row, dict):
            continue
        avg = _float(row.get("fee_avg_bps") or row.get("avg_net_bps"))
        passed = bool(row.get("passed"))
        rows.append(
            _record(
                source_kind="contract_matrix",
                source_artifact=source_artifact,
                strategy_id=str(row.get("strategy_id") or "vnedge_algo_ml_pro_v1"),
                exchange=str(row.get("exchange") or ""),
                symbol=str(row.get("symbol") or ""),
                timeframe=str(row.get("timeframe") or ""),
                status="passed" if passed else _status_from_net_and_verdict(avg, "FAILED"),
                verdict="PASSED" if passed else "FAILED",
                samples=_int(row.get("closed") or row.get("samples")),
                avg_net_bps=avg,
                profit_factor=_float(row.get("pf_r") or row.get("profit_factor")),
                win_rate_pct=_float(row.get("win_rate_pct")),
                mode=str(row.get("mode") or ""),
                failure_mode=str(row.get("failure_mode") or row.get("blocker") or ""),
                next_action=str(row.get("next_action") or ""),
                generated_at=generated_at,
                metadata={
                    "visual_avg_bps": row.get("visual_avg_bps"),
                    "margin_avg": row.get("margin_avg"),
                    "actual_notional_avg": row.get("actual_notional_avg"),
                },
            )
        )
    return rows


def _records_from_candidate_replay(payload: dict[str, Any], *, source_artifact: str) -> list[EvidenceRecord]:
    return _records_from_replay_payload(
        payload,
        source_kind="candidate_replay",
        source_artifact=source_artifact,
    )


def _records_from_filtered_replay(payload: dict[str, Any], *, source_artifact: str) -> list[EvidenceRecord]:
    return _records_from_replay_payload(
        payload,
        source_kind="filtered_replay",
        source_artifact=source_artifact,
    )


def _records_from_replay_payload(
    payload: dict[str, Any],
    *,
    source_kind: str,
    source_artifact: str,
) -> list[EvidenceRecord]:
    generated_at = str(payload.get("generated_at") or "")
    rows: list[EvidenceRecord] = []
    for row in _payload_rows(payload):
        avg = _float(row.get("avg_net_bps") or row.get("net_bps") or row.get("maker_avg_net_bps"))
        verdict = str(row.get("verdict") or row.get("state") or "")
        rows.append(
            _record(
                source_kind=source_kind,
                source_artifact=source_artifact,
                strategy_id=str(row.get("strategy") or row.get("strategy_id") or row.get("family") or ""),
                exchange=str(row.get("exchange") or row.get("follower_exchange") or ""),
                symbol=str(row.get("symbol") or row.get("follower_symbol") or ""),
                timeframe=str(row.get("timeframe") or ""),
                status=_status_from_net_and_verdict(avg, verdict),
                verdict=verdict,
                samples=_int(row.get("fills") or row.get("samples") or row.get("quotes")),
                avg_net_bps=avg,
                profit_factor=_float(row.get("profit_factor") or row.get("maker_profit_factor")),
                win_rate_pct=_float(row.get("win_rate_pct")),
                route=str(row.get("route") or row.get("side") or ""),
                failure_mode=str(row.get("primary_blocker") or row.get("reason") or ""),
                next_action=str(row.get("next_action") or ""),
                generated_at=generated_at,
                metadata={"candidate_id": row.get("candidate_id"), "source": row.get("source")},
            )
        )
    return rows


def _record(**kwargs: Any) -> EvidenceRecord:
    payload = {**kwargs}
    record_id = _record_id(payload)
    return EvidenceRecord(record_id=record_id, **payload)


def _summary(
    records: list[EvidenceRecord],
    loaded_artifacts: list[dict[str, Any]],
    missing_artifacts: list[str],
    *,
    canonical_candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    source_counts = Counter(record.source_kind for record in records)
    status_counts = Counter(record.status for record in records)
    failure_counts = Counter(record.failure_mode or record.verdict or record.status for record in records)
    completed = sum(1 for record in records if record.avg_net_bps is not None)
    positive = sum(1 for record in records if (record.avg_net_bps or -999999.0) > 0)
    negative = sum(1 for record in records if record.avg_net_bps is not None and record.avg_net_bps < 0)
    strict = _fee_wall_breakers(records, limit=10_000)
    sparse = _sparse_positives(records, limit=10_000)
    best = _top_positive(records, limit=1)
    return {
        "total_records": len(records),
        "loaded_artifacts": loaded_artifacts,
        "missing_artifacts": missing_artifacts,
        "source_counts": dict(sorted(source_counts.items())),
        "status_counts": dict(sorted(status_counts.items())),
        "completed_records": completed,
        "positive_after_cost": positive,
        "negative_after_cost": negative,
        "strict_fee_wall_breakers": len(strict),
        "canonical_strict_candidates": len(canonical_candidates),
        "untouched_judgment_queue": sum(
            1
            for candidate in canonical_candidates
            if candidate.get("next_action") == "PRE_REGISTER_UNTOUCHED_JUDGMENT"
        ),
        "sparse_positives": len(sparse),
        "best_avg_net_bps": best[0].avg_net_bps if best else None,
        "best_profit_factor": best[0].profit_factor if best else None,
        "best_record_id": best[0].record_id if best else None,
        "top_failure_modes": [
            {"failure_mode": mode, "count": count}
            for mode, count in failure_counts.most_common(12)
        ],
        "can_trade": False,
        "can_promote": False,
    }


def _operator_answer(summary: dict[str, Any]) -> str:
    total = int(summary.get("total_records") or 0)
    strict = int(summary.get("strict_fee_wall_breakers") or 0)
    canonical = int(summary.get("canonical_strict_candidates") or 0)
    sparse = int(summary.get("sparse_positives") or 0)
    positive = int(summary.get("positive_after_cost") or 0)
    completed = int(summary.get("completed_records") or 0)
    if strict:
        return (
            f"Evidence index found {strict} strict fee-wall breaker row(s), "
            f"canonicalized into {canonical} untouched-judgment candidate(s) "
            f"across {total} normalized rows. They are still research-only until "
            "causal port, replay, and untouched-window judgment clear."
        )
    if sparse:
        return (
            f"Evidence index found {sparse} sparse positive row(s) but no strict "
            f"fee-wall breaker yet. Expand untouched samples before paper promotion."
        )
    if positive:
        return (
            f"Evidence index found {positive}/{completed} completed positive row(s), "
            "but none clear the 25 bps / PF 1.5 / 20-trade strict screen."
        )
    if completed:
        return (
            f"Evidence index normalized {completed} completed row(s); none are "
            "positive after costs. Mine failures for execution and exit uplift."
        )
    return "Evidence index has no completed backtest/replay rows yet; publish scanner/Pine evidence first."


def _sort_records(records: Iterable[EvidenceRecord]) -> list[EvidenceRecord]:
    return sorted(records, key=_record_rank, reverse=True)


def _top_positive(records: Iterable[EvidenceRecord], *, limit: int) -> list[EvidenceRecord]:
    return [
        row
        for row in _sort_records(records)
        if row.avg_net_bps is not None and row.avg_net_bps > 0
    ][:limit]


def _fee_wall_breakers(records: Iterable[EvidenceRecord], *, limit: int) -> list[EvidenceRecord]:
    return [
        row
        for row in _sort_records(records)
        if row.avg_net_bps is not None
        and row.avg_net_bps >= STRICT_MIN_NET_BPS
        and row.profit_factor is not None
        and row.profit_factor >= STRICT_MIN_PROFIT_FACTOR
        and row.samples >= STRICT_MIN_SAMPLES
    ][:limit]


def _sparse_positives(records: Iterable[EvidenceRecord], *, limit: int) -> list[EvidenceRecord]:
    return [
        row
        for row in _sort_records(records)
        if row.avg_net_bps is not None and row.avg_net_bps > 0 and row.samples < STRICT_MIN_SAMPLES
    ][:limit]


def _canonical_strict_candidates(
    records: Iterable[EvidenceRecord],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str, str], list[EvidenceRecord]] = {}
    for record in _fee_wall_breakers(records, limit=10_000):
        key = (
            record.strategy_id,
            record.exchange,
            record.symbol,
            record.timeframe,
        )
        if all(key):
            groups.setdefault(key, []).append(record)

    candidates: list[dict[str, Any]] = []
    for (strategy_id, exchange, symbol, timeframe), rows in groups.items():
        best = max(rows, key=_record_rank)
        source_kinds = sorted({row.source_kind for row in rows})
        source_artifacts = sorted({row.source_artifact for row in rows})
        routes = sorted({row.route for row in rows if row.route})
        modes = sorted({row.mode for row in rows if row.mode})
        evidence_sources = [
            {
                "source_kind": row.source_kind,
                "source_artifact": row.source_artifact,
                "record_id": row.record_id,
                "status": row.status,
                "verdict": row.verdict,
                "samples": row.samples,
                "avg_net_bps": row.avg_net_bps,
                "profit_factor": row.profit_factor,
                "next_action": row.next_action,
            }
            for row in sorted(rows, key=lambda item: (item.source_kind, item.source_artifact))
        ]
        candidates.append(
            {
                "candidate_id": _canonical_candidate_id(
                    strategy_id=strategy_id,
                    exchange=exchange,
                    symbol=symbol,
                    timeframe=timeframe,
                ),
                "strategy_id": strategy_id,
                "exchange": exchange,
                "symbol": symbol,
                "timeframe": timeframe,
                "route": ",".join(routes),
                "mode": ",".join(modes),
                "samples": best.samples,
                "avg_net_bps": best.avg_net_bps,
                "profit_factor": best.profit_factor,
                "win_rate_pct": best.win_rate_pct,
                "source_kinds": source_kinds,
                "source_artifacts": source_artifacts,
                "evidence_record_ids": [row.record_id for row in rows],
                "evidence_sources": evidence_sources,
                "evidence_count": len(rows),
                "strict_gate": {
                    "min_net_bps": STRICT_MIN_NET_BPS,
                    "min_profit_factor": STRICT_MIN_PROFIT_FACTOR,
                    "min_samples": STRICT_MIN_SAMPLES,
                    "sample_valid": best.samples >= STRICT_MIN_SAMPLES,
                    "fee_wall_valid": (
                        best.avg_net_bps is not None
                        and best.avg_net_bps >= STRICT_MIN_NET_BPS
                    ),
                    "profit_factor_valid": (
                        best.profit_factor is not None
                        and best.profit_factor >= STRICT_MIN_PROFIT_FACTOR
                    ),
                    "passed": True,
                },
                "next_action": "PRE_REGISTER_UNTOUCHED_JUDGMENT",
                "requires_untouched_judgment": True,
                "can_trade": False,
                "can_promote": False,
                "live_orders_enabled": False,
            }
        )
    return sorted(
        candidates,
        key=lambda row: (
            _float(row.get("avg_net_bps")) or -999999.0,
            _float(row.get("profit_factor")) or -1.0,
            _int(row.get("samples")),
            str(row.get("candidate_id") or ""),
        ),
        reverse=True,
    )[:limit]


def _failure_clusters(records: Iterable[EvidenceRecord]) -> list[dict[str, Any]]:
    grouped: dict[str, list[EvidenceRecord]] = {}
    for record in records:
        key = record.failure_mode or record.verdict or record.status or "UNKNOWN"
        grouped.setdefault(key, []).append(record)
    out = []
    for failure_mode, rows in grouped.items():
        avg_values = [row.avg_net_bps for row in rows if row.avg_net_bps is not None]
        out.append(
            {
                "failure_mode": failure_mode,
                "count": len(rows),
                "positive_after_cost": sum(1 for value in avg_values if value > 0),
                "avg_net_bps_mean": round(sum(avg_values) / len(avg_values), 4) if avg_values else None,
                "top_source_kind": Counter(row.source_kind for row in rows).most_common(1)[0][0],
            }
        )
    return sorted(out, key=lambda row: int(row["count"]), reverse=True)[:12]


def _record_rank(record: EvidenceRecord) -> tuple[float, float, int, str]:
    avg = record.avg_net_bps if record.avg_net_bps is not None else -999999.0
    pf = min(record.profit_factor if record.profit_factor is not None else -1.0, 999.0)
    return (avg, pf, record.samples, record.record_id)


def _dedupe(records: Iterable[EvidenceRecord]) -> list[EvidenceRecord]:
    by_id: dict[str, EvidenceRecord] = {}
    for record in records:
        existing = by_id.get(record.record_id)
        if existing is None or _record_rank(record) > _record_rank(existing):
            by_id[record.record_id] = record
    return list(by_id.values())


def _status_from_cell(cell: dict[str, Any]) -> str:
    status = str(cell.get("status") or "").lower()
    return status if status else _status_from_net_and_verdict(_float(cell.get("avg_net_bps")), "")


def _status_from_net_and_verdict(avg: float | None, verdict: str) -> str:
    verdict_u = verdict.upper()
    if any(token in verdict_u for token in ("PASS", "EDGE", "CANDIDATE", "WATCHLIST")):
        return "passed" if avg is not None and avg >= STRICT_MIN_NET_BPS else "candidate"
    if "NO_TRADES" in verdict_u or "UNDER" in verdict_u or "SPARSE" in verdict_u:
        return "sparse"
    if "REJECT" in verdict_u or "FAIL" in verdict_u or "NEGATIVE" in verdict_u:
        return "failed"
    if avg is None:
        return "queued"
    return "positive" if avg > 0 else "failed"


def _arena_status(verdict: str) -> str:
    if "EXPAND" in verdict or "READY" in verdict or "CANDIDATE" in verdict:
        return "candidate"
    if "REJECT" in verdict or "FAIL" in verdict:
        return "failed"
    return "queued" if not verdict else verdict.lower()


def _payload_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("rows", "reports", "candidates", "results", "scorecards"):
        value = payload.get(key)
        if isinstance(value, list):
            return [dict(row) for row in value if isinstance(row, dict)]
    return []


def _sqlite_row(row: dict[str, Any]) -> dict[str, Any]:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    return {
        **{key: row.get(key) for key in (
            "record_id",
            "source_kind",
            "source_artifact",
            "strategy_id",
            "exchange",
            "symbol",
            "timeframe",
            "status",
            "verdict",
            "samples",
            "avg_net_bps",
            "profit_factor",
            "win_rate_pct",
            "route",
            "mode",
            "fee_model",
            "failure_mode",
            "next_action",
            "source_ref",
            "source_hash",
            "generated_at",
        )},
        "can_trade": 1 if row.get("can_trade") else 0,
        "can_promote": 1 if row.get("can_promote") else 0,
        "metadata_json": json.dumps(metadata, sort_keys=True),
    }


def _record_id(payload: dict[str, Any]) -> str:
    parts = [
        payload.get("source_kind", ""),
        payload.get("source_artifact", ""),
        payload.get("strategy_id", ""),
        payload.get("exchange", ""),
        payload.get("symbol", ""),
        payload.get("timeframe", ""),
        payload.get("verdict", ""),
        payload.get("source_ref", ""),
        str((payload.get("metadata") or {}).get("script_id") or ""),
        str((payload.get("metadata") or {}).get("candidate_id") or ""),
    ]
    raw = "\x1f".join(str(part) for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _canonical_candidate_id(
    *,
    strategy_id: str,
    exchange: str,
    symbol: str,
    timeframe: str,
) -> str:
    raw = "|".join(
        (
            "strict",
            strategy_id,
            exchange,
            symbol.replace("/", "").replace(":", "").replace("-", ""),
            timeframe,
        )
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
    return f"{raw}|{digest}"


def _feed_record(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "evidence_store_id": payload.get("evidence_store_id"),
        "generated_at": payload.get("generated_at"),
        "summary": payload.get("summary", {}),
        "can_trade": False,
        "can_promote": False,
    }


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


def _first(value: Any) -> str:
    if isinstance(value, list) and value:
        return str(value[0])
    return ""


def _float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed or parsed in {float("inf"), float("-inf")}:
        return None
    return round(parsed, 4)


def _int(value: Any) -> int:
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="publish unified VNEDGE research evidence index")
    parser.add_argument("--report-dir", default=str(DEFAULT_RESEARCH_DIR))
    parser.add_argument("--pine-kb", default=str(DEFAULT_PINE_KB))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--sqlite", default=str(DEFAULT_SQLITE))
    parser.add_argument("--feed", default=str(DEFAULT_FEED))
    parser.add_argument("--no-sqlite", action="store_true")
    parser.add_argument("--no-feed", action="store_true")
    parser.add_argument("--interval-seconds", type=int, default=0)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    while True:
        payload = build_research_evidence_index(
            report_dir=args.report_dir,
            pine_kb_path=args.pine_kb,
        )
        publish_research_evidence_index(
            payload,
            out=args.out,
            sqlite_path=None if args.no_sqlite else args.sqlite,
            feed=None if args.no_feed else args.feed,
        )
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            summary = payload["summary"]
            print(
                "evidence index published: "
                f"records={summary.get('total_records', 0)} "
                f"completed={summary.get('completed_records', 0)} "
                f"positive={summary.get('positive_after_cost', 0)} "
                f"strict={summary.get('strict_fee_wall_breakers', 0)}",
                flush=True,
            )
        if args.interval_seconds <= 0:
            break
        time.sleep(max(30, args.interval_seconds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
