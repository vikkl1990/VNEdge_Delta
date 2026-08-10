"""Attach VNEDGE research evidence to source-backed Pine KB rows.

The Pine Research Lab starts with script provenance and static AI review.  This
module publishes the next layer: mapped VNEDGE-owned primitive backtest/replay
evidence.  It never executes Pine and never grants trading permission.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Iterable

from vnedge.research.pine_alpha_distiller import DEFAULT_OUT as DEFAULT_DISTILLER_PATH
from vnedge.research.pine_script_research import (
    DEFAULT_PINE_KB_PATH,
    DEFAULT_TIMEFRAMES,
    DEFAULT_VENUES,
    enrich_pine_research_records,
    summarize_records,
)


DEFAULT_REPORT_DIR = Path("research/live_research")
DEFAULT_FEED = Path("research/live_research/pine_backtest_evidence_feed.jsonl")
PINE_BACKTEST_EVIDENCE_ID = "pine_backtest_evidence_v1"

PORT_TO_FAMILIES: dict[str, tuple[str, ...]] = {
    "fvg_liquidity_breakout_v1": ("fvg_retest", "order_block", "structure_break"),
    "range_expansion_breakout_v1": ("squeeze_release", "structure_break"),
    "trend_momentum_context_v1": ("all_atoms",),
}

PORT_TO_ATOMS: dict[str, tuple[str, ...]] = {
    "fvg_liquidity_breakout_v1": ("fvg_retest", "order_block", "activity_zone_reclaim"),
    "range_expansion_breakout_v1": ("squeeze_release", "structure_break"),
    "trend_momentum_context_v1": ("net_volume_flow", "squeeze_release", "activity_zone_reclaim"),
}

DIRECT_15M_PORTS = {
    "fvg_liquidity_breakout_v1",
    "range_expansion_breakout_v1",
    "trend_momentum_context_v1",
}

STRATEGY_TO_PORTS: dict[str, tuple[str, ...]] = {
    "luxara_break_bounce_v27_v1": (
        "fvg_liquidity_breakout_v1",
        "range_expansion_breakout_v1",
    ),
    "luxara_live_plan_qtm_v1": (
        "range_expansion_breakout_v1",
        "trend_momentum_context_v1",
    ),
    "luxy_ut_bot_forecast_v1": (
        "trail_exit_lab_v1",
        "trend_momentum_context_v1",
    ),
    "momentum_cascade_lyro_v1": (
        "trail_exit_lab_v1",
        "trend_momentum_context_v1",
    ),
    "sats_5m_scalper_v1": (
        "range_expansion_breakout_v1",
        "trend_momentum_context_v1",
    ),
    "stealth_trail_bbp_v1": (
        "orderflow_proxy_v1",
        "trail_exit_lab_v1",
        "trend_momentum_context_v1",
    ),
    "fvg_liquidity_breakout_v1": (
        "fvg_liquidity_breakout_v1",
        "trail_exit_lab_v1",
    ),
}


@dataclass(frozen=True)
class PrimitiveEvidence:
    port: str
    timeframe: str
    status: str
    samples: int
    avg_net_bps: float | None
    profit_factor: float | None
    win_rate_pct: float | None
    blocker: str
    source: str
    strategy: str
    exchange: str
    symbol: str
    verdict: str
    updated_at: str = ""

    @property
    def rank(self) -> tuple[int, float, float, int]:
        status_rank = {"passed": 3, "failed": 2, "running": 1}.get(self.status, 0)
        pf = self.profit_factor if self.profit_factor is not None else -1.0
        avg = self.avg_net_bps if self.avg_net_bps is not None else -999999.0
        return (status_rank, pf, avg, self.samples)

    def to_cell(self) -> dict:
        cell = {
            "timeframe": self.timeframe,
            "status": self.status,
            "venues": [self.exchange] if self.exchange else list(DEFAULT_VENUES),
            "samples": self.samples,
            "avg_net_bps": self.avg_net_bps,
            "profit_factor": self.profit_factor,
            "win_rate_pct": self.win_rate_pct,
            "blocker": self.blocker,
            "evidence_source": self.source,
            "tested_strategy": self.strategy,
            "tested_symbol": self.symbol,
            "verdict": self.verdict,
            "updated_at": self.updated_at,
            "can_trade": False,
            "can_promote": False,
        }
        return cell


def publish_pine_backtest_evidence(
    *,
    kb_path: Path | str = DEFAULT_PINE_KB_PATH,
    distiller_path: Path | str = DEFAULT_DISTILLER_PATH,
    report_dir: Path | str = DEFAULT_REPORT_DIR,
    output_path: Path | str | None = None,
    feed_path: Path | str | None = DEFAULT_FEED,
    now: datetime | None = None,
) -> dict:
    """Overlay source-backed primitive evidence onto the Pine research KB."""

    generated = now or datetime.now(UTC)
    kb = _read_json(Path(kb_path))
    distiller = _read_json(Path(distiller_path))
    records = [dict(row) for row in kb.get("records", []) if isinstance(row, dict)]
    distillations = {
        str(row.get("script_id") or ""): dict(row)
        for row in distiller.get("script_distillations", [])
        if isinstance(row, dict)
    }
    evidence = build_evidence_index(Path(report_dir))
    updated = [
        _attach_record_evidence(record, distillations.get(str(record.get("script_id") or "")), evidence)
        for record in records
    ]
    rows = enrich_pine_research_records(updated)
    payload = {
        **{k: v for k, v in kb.items() if k not in {"records", "summary", "priorities"}},
        "generated_at": generated.isoformat(),
        "source": str(kb_path),
        "summary": summarize_records(rows),
        "records": rows,
        "priorities": _priority_queue(rows),
        "backtest_evidence": _evidence_summary(rows, evidence),
        "operator_answer": _operator_answer(rows),
        "can_trade": False,
        "can_promote": False,
    }
    out = Path(output_path) if output_path is not None else Path(kb_path)
    _atomic_write_json(out, payload)
    if feed_path is not None:
        feed = Path(feed_path)
        feed.parent.mkdir(parents=True, exist_ok=True)
        with feed.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
    return payload


def build_evidence_index(report_dir: Path) -> dict[str, dict[str, PrimitiveEvidence]]:
    """Read live research reports and pick the best evidence per port/timeframe."""

    index: dict[str, dict[str, PrimitiveEvidence]] = {}
    _ingest_daily_scalper(index, _read_json(report_dir / "daily_scalper_latest.json"), "daily_scalper_latest.json")
    _ingest_daily_scalper(
        index,
        _read_json(report_dir / "daily_scalper_cadence_latest.json"),
        "daily_scalper_cadence_latest.json",
    )
    _ingest_alpha_distillation(
        index,
        _read_json(report_dir / "alpha_distillation_latest.json"),
        "alpha_distillation_latest.json",
    )
    _ingest_orderflow(index, _read_json(report_dir / "orderflow_footprint_latest.json"))
    _ingest_candidate_replay(index, _read_json(report_dir / "candidate_replay_latest.json"))
    _ingest_event_leadlag(index, _read_json(report_dir / "event_leadlag_latest.json"))
    _ingest_fee_wall_forensics(index, _read_json(report_dir / "fee_wall_forensics_latest.json"))
    return index


def _attach_record_evidence(
    record: dict,
    distillation: dict | None,
    evidence: dict[str, dict[str, PrimitiveEvidence]],
) -> dict:
    row = dict(record)
    source_available = bool(row.get("source_available"))
    verdict = str(row.get("crypto_portability") or "")
    if not source_available:
        row["backtests"] = _blocked_cells("source unavailable; cannot port or backtest")
        return row
    if verdict == "BLOCKED_REPAINT_RISK":
        row["backtests"] = _blocked_cells("causality quarantine: repaint/lookahead risk")
        return row
    if distillation is None:
        row["backtests"] = _queued_cells("source reviewed; awaiting alpha distillation match")
        return row

    port = str(distillation.get("recommended_port") or "")
    action = str(distillation.get("action") or "")
    if action == "CAUSALITY_QUARANTINE" or port == "causality_quarantine_v1":
        row["backtests"] = _blocked_cells("causality quarantine: rewrite HTF/lookahead before replay")
        return row
    if action == "FEATURE_BANK_ONLY" or port in {
        "edge_model_feature_bank_v1",
        "source_feature_library_review_v1",
    }:
        row["backtests"] = _not_applicable_cells(
            f"{port or 'feature bank'} is feature-bank material, not a standalone scanner"
        )
        return row

    cells = []
    port_evidence = evidence.get(port, {})
    for timeframe in DEFAULT_TIMEFRAMES:
        ev = port_evidence.get(timeframe)
        if ev is not None:
            cells.append(ev.to_cell())
            continue
        cells.append(_default_cell_for_missing_port_evidence(port, timeframe))
    row["backtests"] = cells
    return row


def _default_cell_for_missing_port_evidence(port: str, timeframe: str) -> dict:
    if port in DIRECT_15M_PORTS:
        if timeframe == "1m":
            return _cell(
                timeframe,
                "not_applicable",
                "1m is used as trigger context in the current port, not standalone entry proof",
            )
        if timeframe in {"1h", "4h"}:
            return _cell(
                timeframe,
                "not_applicable",
                f"{timeframe} is HTF bias/context in the current port, not standalone entry proof",
            )
    if port == "orderflow_proxy_v1":
        if timeframe == "1m":
            return _cell(
                timeframe,
                "running",
                "orderflow candidates mined; awaiting conservative replay/fill proof",
            )
        return _cell(timeframe, "not_applicable", "orderflow proxy is tick/60s, not candle-TF replay")
    if port == "trail_exit_lab_v1":
        return _cell(
            timeframe,
            "queued",
            "exit overlay lab not run yet; needs entry-journal replay before standalone evidence",
        )
    return _cell(timeframe, "queued", f"{port or 'port'} evidence not published yet")


def _ingest_daily_scalper(index: dict, report: dict, source_name: str) -> None:
    for row in _report_rows(report):
        family = str(row.get("candidate_family") or "")
        for port, families in PORT_TO_FAMILIES.items():
            if family not in families:
                continue
            _add_evidence(index, _evidence_from_wf_row(row, port=port, source=source_name))


def _ingest_alpha_distillation(index: dict, report: dict, source_name: str) -> None:
    for row in _report_rows(report):
        for port, atoms in PORT_TO_ATOMS.items():
            best_atom = _best_atom_attribution(row.get("atom_attribution"), atoms)
            if best_atom is None:
                continue
            atom_name, atom = best_atom
            _add_evidence(
                index,
                _evidence_from_atom(row, atom, atom_name=atom_name, port=port, source=source_name),
            )
        _add_evidence(index, _evidence_from_wf_row(row, port="trend_momentum_context_v1", source=source_name))


def _ingest_orderflow(index: dict, report: dict) -> None:
    rows = _report_rows(report)
    best = max(rows, key=lambda row: float(row.get("score") or 0.0), default=None)
    if best is None:
        return
    _add_evidence(
        index,
        PrimitiveEvidence(
            port="orderflow_proxy_v1",
            timeframe="1m",
            status="running",
            samples=_int(best.get("samples") or best.get("trade_count")),
            avg_net_bps=_float_or_none(best.get("price_change_bps")),
            profit_factor=None,
            win_rate_pct=None,
            blocker=(
                f"{best.get('state') or 'ORDERFLOW_REPLAY_REQUIRED'}; "
                "candidate mined, conservative replay still required"
            ),
            source="orderflow_footprint_latest.json",
            strategy=str(best.get("family") or "orderflow_footprint_v1"),
            exchange=str(best.get("exchange") or ""),
            symbol=str(best.get("symbol") or ""),
            verdict=str(best.get("state") or ""),
            updated_at=str(report.get("generated_at") or ""),
        ),
    )


def _ingest_candidate_replay(index: dict, report: dict) -> None:
    rows = [
        row for row in _report_rows(report)
        if str(row.get("source") or "").lower().startswith("orderflow")
        or "orderflow" in str(row.get("candidate_id") or "").lower()
    ]
    best = max(rows, key=_replay_rank, default=None)
    if best is None:
        return
    _add_evidence(
        index,
        PrimitiveEvidence(
            port="orderflow_proxy_v1",
            timeframe="1m",
            status="passed" if str(best.get("verdict")) == "REPLAY_CANDIDATE" else "failed",
            samples=_int(best.get("fills") or best.get("samples") or best.get("quotes")),
            avg_net_bps=_float_or_none(best.get("avg_net_bps") or best.get("net_bps")),
            profit_factor=_float_or_none(best.get("profit_factor")),
            win_rate_pct=_float_or_none(best.get("win_rate_pct")),
            blocker=str(best.get("verdict") or "candidate replay did not pass"),
            source="candidate_replay_latest.json",
            strategy="candidate_replay_executor_v1",
            exchange=str(best.get("exchange") or ""),
            symbol=str(best.get("symbol") or ""),
            verdict=str(best.get("verdict") or ""),
            updated_at=str(report.get("generated_at") or ""),
        ),
    )


def _ingest_event_leadlag(index: dict, report: dict) -> None:
    best = max(_report_rows(report), key=_leadlag_rank, default=None)
    if best is None:
        return
    _add_evidence(
        index,
        PrimitiveEvidence(
            port="trend_momentum_context_v1",
            timeframe="15m",
            status="running" if str(best.get("state")) == "EDGE_CANDIDATE_MAKER" else "failed",
            samples=_int(best.get("samples")),
            avg_net_bps=_float_or_none(best.get("maker_avg_net_bps")),
            profit_factor=_float_or_none(best.get("maker_profit_factor")),
            win_rate_pct=_float_or_none(best.get("win_rate_pct")),
            blocker=(
                f"{best.get('state') or 'NO_EDGE'}; "
                "event lead-lag is evidence, not Pine-script promotion"
            ),
            source="event_leadlag_latest.json",
            strategy=str(best.get("family") or "cross_venue_event_leadlag_v1"),
            exchange=str(best.get("follower_exchange") or ""),
            symbol=str(best.get("follower_symbol") or ""),
            verdict=str(best.get("state") or ""),
            updated_at=str(report.get("generated_at") or ""),
        ),
    )


def _ingest_fee_wall_forensics(index: dict, report: dict) -> None:
    """Map fee-wall scanner truth onto Pine primitive port families.

    Fee-wall forensics is the current broadest source of VNEDGE-owned replay
    evidence for Pine-inspired scanners.  It is strategy-level evidence, not
    original Pine execution, so rows retain the tested VNEDGE strategy and
    remain research-only.
    """

    for row in _report_rows(report):
        strategy = str(row.get("strategy") or "")
        ports = STRATEGY_TO_PORTS.get(strategy, ())
        if not ports:
            continue
        summary = row.get("summary") if isinstance(row.get("summary"), dict) else {}
        for port in ports:
            _add_evidence(
                index,
                _evidence_from_fee_wall_report(
                    row,
                    summary,
                    port=port,
                    source="fee_wall_forensics_latest.json",
                    updated_at=str(row.get("generated_at") or report.get("generated_at") or ""),
                ),
            )


def _evidence_from_wf_row(row: dict, *, port: str, source: str) -> PrimitiveEvidence:
    status = "passed" if str(row.get("verdict")) == "PASS" else "failed"
    samples = _int(row.get("oos_trades"))
    blocker = str(row.get("verdict") or "UNKNOWN")
    reasons = row.get("reasons")
    if isinstance(reasons, list) and reasons:
        blocker = f"{blocker}: {str(reasons[0])[:160]}"
    return PrimitiveEvidence(
        port=port,
        timeframe=str(row.get("timeframe") or "15m"),
        status=status,
        samples=samples,
        avg_net_bps=_float_or_none(row.get("avg_net_bps")),
        profit_factor=_float_or_none(row.get("profit_factor")),
        win_rate_pct=_win_rate_from_attribution(row.get("attribution"), samples),
        blocker=blocker,
        source=source,
        strategy=str(row.get("strategy") or ""),
        exchange=str(row.get("exchange") or ""),
        symbol=str(row.get("symbol") or ""),
        verdict=str(row.get("verdict") or ""),
        updated_at=str(row.get("updated") or ""),
    )


def _evidence_from_fee_wall_report(
    row: dict,
    summary: dict,
    *,
    port: str,
    source: str,
    updated_at: str,
) -> PrimitiveEvidence:
    verdict = str(summary.get("verdict") or "UNKNOWN")
    status = "passed" if verdict in {"MAKER_EDGE", "TAKER_EDGE", "MIXED_ROUTE_EDGE"} else "failed"
    blocker = str(summary.get("primary_blocker") or verdict)
    exit_counts = summary.get("exit_diagnosis_counts")
    if isinstance(exit_counts, dict) and exit_counts:
        top_exit = max(exit_counts.items(), key=lambda item: int(item[1] or 0))
        blocker = f"{verdict}: {blocker}; exit={top_exit[0]}:{top_exit[1]}"
    else:
        blocker = f"{verdict}: {blocker}"
    return PrimitiveEvidence(
        port=port,
        timeframe=str(row.get("timeframe") or ""),
        status=status,
        samples=_int(summary.get("routed") or row.get("opportunity_count")),
        avg_net_bps=_float_or_none(summary.get("avg_selected_net_bps")),
        profit_factor=_float_or_none(summary.get("profit_factor")),
        win_rate_pct=_float_or_none(summary.get("win_rate_pct")),
        blocker=blocker,
        source=source,
        strategy=str(row.get("strategy") or ""),
        exchange=str(row.get("exchange") or ""),
        symbol=str(row.get("symbol") or ""),
        verdict=verdict,
        updated_at=updated_at,
    )


def _evidence_from_atom(
    row: dict,
    atom: dict,
    *,
    atom_name: str,
    port: str,
    source: str,
) -> PrimitiveEvidence:
    samples = _int(atom.get("trades"))
    pf = _float_or_none(atom.get("profit_factor"))
    net_usd = _float_or_none(atom.get("net_usd")) or 0.0
    parent_passed = str(row.get("verdict")) == "PASS"
    status = "failed"
    blocker = f"atom {atom_name} research only"
    if samples >= 20 and net_usd > 0.0 and pf is not None and pf >= 1.5:
        if parent_passed:
            status = "passed"
            blocker = f"atom {atom_name} passes primitive screen; still needs untouched judgment"
        else:
            blocker = (
                f"atom {atom_name} positive, but parent strategy verdict "
                f"{row.get('verdict') or 'UNKNOWN'} did not pass"
            )
    elif samples < 20:
        blocker = f"atom {atom_name} under-sampled: {samples} < 20 trades"
    elif net_usd <= 0.0:
        blocker = f"atom {atom_name} net not positive after costs"
    elif pf is None or pf < 1.5:
        blocker = f"atom {atom_name} PF {pf if pf is not None else '--'} < 1.5"
    return PrimitiveEvidence(
        port=port,
        timeframe=str(row.get("timeframe") or "15m"),
        status=status,
        samples=samples,
        avg_net_bps=None,
        profit_factor=pf,
        win_rate_pct=_float_or_none(atom.get("win_rate_pct")),
        blocker=blocker,
        source=source,
        strategy=f"{row.get('strategy') or 'alpha_distillation_pack_v1'}:{atom_name}",
        exchange=str(row.get("exchange") or ""),
        symbol=str(row.get("symbol") or ""),
        verdict=status.upper(),
        updated_at=str(row.get("updated") or ""),
    )


def _best_atom_attribution(atom_attribution: object, atoms: tuple[str, ...]) -> tuple[str, dict] | None:
    if not isinstance(atom_attribution, dict):
        return None
    rows = [
        (name, atom_attribution.get(name))
        for name in atoms
        if isinstance(atom_attribution.get(name), dict)
    ]
    return max(rows, key=lambda pair: _atom_rank(pair[1]), default=None)


def _add_evidence(index: dict[str, dict[str, PrimitiveEvidence]], evidence: PrimitiveEvidence) -> None:
    if not evidence.port or not evidence.timeframe:
        return
    by_tf = index.setdefault(evidence.port, {})
    existing = by_tf.get(evidence.timeframe)
    if existing is None or evidence.rank > existing.rank:
        by_tf[evidence.timeframe] = evidence


def _report_rows(report: dict) -> list[dict]:
    if not isinstance(report, dict):
        return []
    for key in ("results", "rows", "reports", "candidates", "hypotheses", "recommendations"):
        value = report.get(key)
        if isinstance(value, list):
            return [dict(row) for row in value if isinstance(row, dict)]
    return []


def _blocked_cells(blocker: str) -> list[dict]:
    return [_cell(tf, "blocked", blocker) for tf in DEFAULT_TIMEFRAMES]


def _queued_cells(blocker: str) -> list[dict]:
    return [_cell(tf, "queued", blocker) for tf in DEFAULT_TIMEFRAMES]


def _not_applicable_cells(blocker: str) -> list[dict]:
    return [_cell(tf, "not_applicable", blocker) for tf in DEFAULT_TIMEFRAMES]


def _cell(timeframe: str, status: str, blocker: str) -> dict:
    return {
        "timeframe": timeframe,
        "status": status,
        "venues": list(DEFAULT_VENUES),
        "samples": 0,
        "avg_net_bps": None,
        "profit_factor": None,
        "win_rate_pct": None,
        "blocker": blocker,
        "can_trade": False,
        "can_promote": False,
    }


def _priority_queue(records: Iterable[dict], *, limit: int = 25) -> list[dict]:
    rows = sorted(
        records,
        key=lambda row: (-int(row.get("priority_score") or 0), str(row.get("script_id") or "")),
    )
    return [
        {
            "script_id": str(row.get("script_id") or ""),
            "title": str(row.get("title") or ""),
            "crypto_portability": str(row.get("crypto_portability") or ""),
            "source_available": bool(row.get("source_available")),
            "mechanism": str(row.get("mechanism") or "unknown"),
            "priority_score": int(row.get("priority_score") or 0),
            "next_action": str(row.get("next_action") or "WAIT"),
            "backtest_summary": _record_backtest_summary(row),
        }
        for row in rows[:limit]
    ]


def _record_backtest_summary(row: dict) -> dict:
    counts = {}
    best_positive_cell: dict | None = None
    best_completed_cell: dict | None = None
    for cell in row.get("backtests") or []:
        if not isinstance(cell, dict):
            continue
        status = str(cell.get("status") or "queued")
        counts[status] = counts.get(status, 0) + 1
        bps = _float_or_none(cell.get("avg_net_bps"))
        if status in {"passed", "failed"}:
            best_completed_cell = _better_cell(best_completed_cell, row, cell)
            if bps is not None and bps > 0:
                best_positive_cell = _better_cell(best_positive_cell, row, cell)
    return {
        "status_counts": counts,
        "best_pf": (
            _float_or_none(best_positive_cell.get("profit_factor"))
            if best_positive_cell is not None else None
        ),
        "best_avg_net_bps": (
            _float_or_none(best_positive_cell.get("avg_net_bps"))
            if best_positive_cell is not None else None
        ),
        "best_completed_pf": (
            _float_or_none(best_completed_cell.get("profit_factor"))
            if best_completed_cell is not None else None
        ),
        "best_completed_avg_net_bps": (
            _float_or_none(best_completed_cell.get("avg_net_bps"))
            if best_completed_cell is not None else None
        ),
    }


def _evidence_summary(rows: list[dict], evidence: dict[str, dict[str, PrimitiveEvidence]]) -> dict:
    counts = {}
    completed = 0
    positive_completed = 0
    best_positive_cell: dict | None = None
    best_completed_cell: dict | None = None
    for row in rows:
        for cell in row.get("backtests") or []:
            if not isinstance(cell, dict):
                continue
            status = str(cell.get("status") or "queued")
            counts[status] = counts.get(status, 0) + 1
            if status in {"passed", "failed"}:
                completed += 1
                best_completed_cell = _better_cell(best_completed_cell, row, cell)
                avg_net_bps = _float_or_none(cell.get("avg_net_bps"))
                if avg_net_bps is not None and avg_net_bps > 0:
                    positive_completed += 1
                    best_positive_cell = _better_cell(best_positive_cell, row, cell)
    ports = {
        port: {tf: asdict(ev) for tf, ev in by_tf.items()}
        for port, by_tf in sorted(evidence.items())
    }
    best_positive_avg = (
        _float_or_none(best_positive_cell.get("avg_net_bps"))
        if best_positive_cell is not None else None
    )
    best_positive_pf = (
        _float_or_none(best_positive_cell.get("profit_factor"))
        if best_positive_cell is not None else None
    )
    best_completed_avg = (
        _float_or_none(best_completed_cell.get("avg_net_bps"))
        if best_completed_cell is not None else None
    )
    best_completed_pf = (
        _float_or_none(best_completed_cell.get("profit_factor"))
        if best_completed_cell is not None else None
    )
    return {
        "evidence_id": PINE_BACKTEST_EVIDENCE_ID,
        "status_counts": dict(sorted(counts.items())),
        "completed_cells": completed,
        "positive_completed_cells": positive_completed,
        "best_positive_avg_net_bps": best_positive_avg,
        "best_positive_profit_factor": best_positive_pf,
        "best_positive_cell": best_positive_cell,
        "best_completed_avg_net_bps": best_completed_avg,
        "best_completed_profit_factor": best_completed_pf,
        "best_completed_cell": best_completed_cell,
        "headline_verdict": _headline_verdict(completed, positive_completed, counts),
        "ports_with_evidence": len(evidence),
        "port_evidence": ports,
        "can_trade": False,
        "can_promote": False,
    }


def _better_cell(existing: dict | None, row: dict, cell: dict) -> dict:
    candidate = {
        "script_id": str(row.get("script_id") or ""),
        "title": str(row.get("title") or ""),
        "timeframe": str(cell.get("timeframe") or ""),
        "status": str(cell.get("status") or "queued"),
        "samples": _int(cell.get("samples")),
        "avg_net_bps": _float_or_none(cell.get("avg_net_bps")),
        "profit_factor": _float_or_none(cell.get("profit_factor")),
        "win_rate_pct": _float_or_none(cell.get("win_rate_pct")),
        "blocker": str(cell.get("blocker") or ""),
        "evidence_source": str(cell.get("evidence_source") or ""),
    }
    if existing is None:
        return candidate
    return candidate if _cell_rank(candidate) > _cell_rank(existing) else existing


def _cell_rank(cell: dict) -> tuple[float, float, int]:
    avg = _float_or_none(cell.get("avg_net_bps"))
    pf = _float_or_none(cell.get("profit_factor"))
    return (
        avg if avg is not None else -999999.0,
        min(pf if pf is not None else -1.0, 999.0),
        _int(cell.get("samples")),
    )


def _headline_verdict(completed: int, positive_completed: int, counts: dict) -> str:
    if positive_completed > 0:
        return "POSITIVE_COMPLETED_EVIDENCE"
    if completed > 0:
        return "NO_POSITIVE_COMPLETED_EDGE"
    if counts.get("blocked", 0) > 0:
        return "BLOCKED_OR_QUARANTINED"
    if counts.get("queued", 0) > 0 or counts.get("running", 0) > 0:
        return "AWAITING_CAUSAL_REPLAY"
    return "NO_BACKTEST_EVIDENCE"


def _operator_answer(rows: list[dict]) -> str:
    total = 0
    completed = 0
    passed = 0
    failed = 0
    for row in rows:
        for cell in row.get("backtests") or []:
            if not isinstance(cell, dict):
                continue
            total += 1
            status = str(cell.get("status") or "")
            if status in {"passed", "failed"}:
                completed += 1
            if status == "passed":
                passed += 1
            if status == "failed":
                failed += 1
    return (
        f"Pine backtest matrix refreshed from VNEDGE-owned research artifacts: "
        f"{completed}/{total} cells have completed primitive evidence "
        f"({passed} passed, {failed} failed). These are research overlays only; "
        "no Pine row can trade or promote without a causal Python port and "
        "untouched-window judgment."
    )


def _read_json(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _atomic_write_json(path: Path, payload: dict) -> None:
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


def _float_or_none(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed or parsed in {float("inf"), float("-inf")}:
        return None
    return round(parsed, 4)


def _int(value: object) -> int:
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0


def _win_rate_from_attribution(attribution: object, samples: int) -> float | None:
    if not isinstance(attribution, dict) or samples <= 0:
        return None
    wins_weighted = 0.0
    counted = 0
    for side in attribution.values():
        if not isinstance(side, dict):
            continue
        trades = _int(side.get("trades"))
        win_rate = _float_or_none(side.get("win_rate_pct"))
        if trades and win_rate is not None:
            wins_weighted += trades * win_rate
            counted += trades
    if not counted:
        return None
    return round(wins_weighted / counted, 2)


def _atom_rank(atom: dict) -> tuple[float, float, int]:
    return (
        _float_or_none(atom.get("profit_factor")) or -1.0,
        _float_or_none(atom.get("net_usd")) or -999999.0,
        _int(atom.get("trades")),
    )


def _replay_rank(row: dict) -> tuple[float, int]:
    verdict_rank = {
        "REPLAY_CANDIDATE": 4,
        "POSITIVE_UNDER_SAMPLED": 3,
        "NEGATIVE_EDGE_AFTER_REPLAY": 2,
        "NO_FILLS": 1,
    }.get(str(row.get("verdict") or ""), 0)
    return (verdict_rank, _int(row.get("fills") or row.get("samples") or row.get("quotes")))


def _leadlag_rank(row: dict) -> tuple[float, float, int]:
    return (
        1.0 if str(row.get("state")) == "EDGE_CANDIDATE_MAKER" else 0.0,
        _float_or_none(row.get("maker_avg_net_bps")) or -999999.0,
        _int(row.get("samples")),
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="overlay VNEDGE primitive backtest evidence onto the Pine research KB"
    )
    parser.add_argument("--kb", default=str(DEFAULT_PINE_KB_PATH))
    parser.add_argument("--distiller", default=str(DEFAULT_DISTILLER_PATH))
    parser.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR))
    parser.add_argument(
        "--output",
        default="",
        help="output KB path; defaults to overwriting --kb atomically",
    )
    parser.add_argument("--feed", default=str(DEFAULT_FEED))
    parser.add_argument("--no-feed", action="store_true")
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=0,
        help="repeat forever at this cadence; 0 means one-shot publish",
    )
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    while True:
        payload = publish_pine_backtest_evidence(
            kb_path=args.kb,
            distiller_path=args.distiller,
            report_dir=args.report_dir,
            output_path=args.output or None,
            feed_path=None if args.no_feed else args.feed,
        )
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            evidence = payload.get("backtest_evidence", {})
            summary = payload.get("summary", {})
            print(
                "pine backtest evidence published: "
                f"total={summary.get('total', 0)} "
                f"queued={summary.get('backtests_queued', 0)} "
                f"completed={evidence.get('completed_cells', 0)} "
                f"status_counts={evidence.get('status_counts', {})}",
                flush=True,
            )
        if args.interval_seconds <= 0:
            break
        time.sleep(max(30, args.interval_seconds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
