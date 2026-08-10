"""All-blueprint proof matrix for the Quantified Strategy Lab.

The first lab proof only covered ``pullback_reversion_pack_v1``. This publisher
extends the same Agent Gateway evidence path across every currently syncable
Quantified blueprint, while staying honest about adapter maturity:

* Chunk-A executable ports use the VNEDGE-owned fee-wall sniper directly.
* Indicator MTF uses the VNEDGE-owned Quant Signal Pack.
* Session/calendar and relative-strength ports are marked as proxy adapters
  until true session-settlement and portfolio-rank ports exist.

Rows are research-only and cannot trade or promote. A positive/promotable row
still needs untouched-window judgment and human approval.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
import json
import math
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
import time
from typing import Any, Mapping

from vnedge.agent_gateway.jobs import (
    BLOCKED_STATUS,
    DONE_STATUS,
    FAILED_STATUS,
    PENDING_STATUS,
    RUNNING_STATUS,
    create_backtest_job,
)
from vnedge.research.quantified_pullback_reversion_proof import (
    QUANTIFIED_PULLBACK_REVERSION_PROOF_ID,
)
from vnedge.strategy.quant_signal_pack import QuantSignalPack
from vnedge.strategy.quantified_fee_wall_sniper import QUANTIFIED_FEE_WALL_SNIPER_ID


QUANTIFIED_BLUEPRINT_PROOF_ID = "quantified_blueprint_proof_v1"
DEFAULT_OUT = Path("research/live_research/quantified_blueprint_proof_latest.json")
DEFAULT_FEED = Path("research/live_research/quantified_blueprint_proof_feed.jsonl")
DEFAULT_ARTIFACT_DIR = Path("research/live_research/agent_jobs")
STATUS_QUEUED = "QUEUED_RESEARCH_ONLY"

DEFAULT_EXCHANGES = ("binanceusdm", "bybit", "delta_india")
DEFAULT_BASES = ("BTC", "ETH", "SOL", "XRP")
DEFAULT_TIMEFRAMES = ("1m", "5m", "15m", "1h", "4h")
DEFAULT_GATE_NET_BPS = 25.0
DEFAULT_GATE_PF = 1.50
DEFAULT_GATE_TRADES = 20


def env_agent_jobs_dir(env: dict[str, str] | None = None) -> Path:
    source = os.environ if env is None else env
    return Path(source.get("AGENT_GATEWAY_JOBS_DIR", "logs/agent_gateway/jobs"))


@dataclass(frozen=True)
class BlueprintProofProfile:
    port_id: str
    strategy_id: str
    adapter: str
    setup_mode: str
    exchanges: tuple[str, ...] = DEFAULT_EXCHANGES
    bases: tuple[str, ...] = DEFAULT_BASES
    timeframes: tuple[str, ...] = DEFAULT_TIMEFRAMES
    strategy_parameters: Mapping[str, Any] = field(default_factory=dict)
    canonical_adapter: bool = True
    source_count_hint: int = 0


@dataclass(frozen=True)
class QuantifiedBlueprintProofConfig:
    profiles: tuple[BlueprintProofProfile, ...] = field(default_factory=tuple)
    initial_capital_usd: float = 100.0
    paper_margin_usd: float = 100.0
    paper_leverage: float = 25.0
    min_net_bps: float = DEFAULT_GATE_NET_BPS
    min_profit_factor: float = DEFAULT_GATE_PF
    min_trades: int = DEFAULT_GATE_TRADES
    default_max_holding_bars: int = 16

    def __post_init__(self) -> None:
        if self.initial_capital_usd <= 0:
            raise ValueError("initial_capital_usd must be positive")
        if self.paper_margin_usd <= 0:
            raise ValueError("paper_margin_usd must be positive")
        if self.paper_leverage <= 0:
            raise ValueError("paper_leverage must be positive")
        if self.min_net_bps <= 0:
            raise ValueError("min_net_bps must be positive")
        if self.min_profit_factor < 1.0:
            raise ValueError("min_profit_factor must be >= 1")
        if self.min_trades < 1:
            raise ValueError("min_trades must be positive")

    @property
    def paper_notional_usd(self) -> float:
        return self.paper_margin_usd * self.paper_leverage

    @property
    def active_profiles(self) -> tuple[BlueprintProofProfile, ...]:
        return self.profiles or default_profiles()


def default_profiles() -> tuple[BlueprintProofProfile, ...]:
    return (
        BlueprintProofProfile(
            port_id="bitcoin_crypto_strategy_pack_v1",
            strategy_id=QUANTIFIED_FEE_WALL_SNIPER_ID,
            adapter="fee_wall_sniper_combined",
            setup_mode="pullback_plus_breakout",
            strategy_parameters={
                "params": {"enabled_setups": ["pullback", "breakout"]},
                "min_expected_net_edge_bps": DEFAULT_GATE_NET_BPS,
            },
            source_count_hint=1,
        ),
        BlueprintProofProfile(
            port_id="range_volatility_breakout_reversion_v1",
            strategy_id=QUANTIFIED_FEE_WALL_SNIPER_ID,
            adapter="fee_wall_sniper_breakout",
            setup_mode="breakout_only",
            strategy_parameters={
                "params": {"enabled_setups": ["breakout"]},
                "min_expected_net_edge_bps": DEFAULT_GATE_NET_BPS,
            },
            source_count_hint=48,
        ),
        BlueprintProofProfile(
            port_id="pullback_reversion_pack_v1",
            strategy_id=QUANTIFIED_FEE_WALL_SNIPER_ID,
            adapter="fee_wall_sniper_pullback",
            setup_mode="pullback_only",
            strategy_parameters={
                "params": {"enabled_setups": ["pullback"]},
                "min_expected_net_edge_bps": DEFAULT_GATE_NET_BPS,
            },
            source_count_hint=5,
        ),
        BlueprintProofProfile(
            port_id="indicator_pack_mtf_v1",
            strategy_id=QuantSignalPack.strategy_id,
            adapter="quant_signal_pack_mtf_atoms",
            setup_mode="indicator_confluence",
            strategy_parameters={
                "allowed_families": [
                    "liquidity_sweep",
                    "fvg_retest",
                    "order_block",
                    "squeeze_release",
                    "vwap_reclaim",
                    "structure_break",
                    "confluence",
                ],
                "min_score": 5.0,
                "min_score_delta": 1.0,
                "take_profit_r": 2.0,
            },
            source_count_hint=16,
        ),
        BlueprintProofProfile(
            port_id="crypto_session_calendar_miner_v1",
            strategy_id=QuantSignalPack.strategy_id,
            adapter="quant_signal_pack_session_proxy",
            setup_mode="session_proxy",
            strategy_parameters={
                "allowed_families": ["vwap_reclaim", "squeeze_release", "structure_break"],
                "min_score": 4.75,
                "min_score_delta": 0.75,
                "take_profit_r": 1.8,
            },
            canonical_adapter=False,
            source_count_hint=24,
        ),
        BlueprintProofProfile(
            port_id="crypto_relative_strength_rotation_v1",
            strategy_id=QuantSignalPack.strategy_id,
            adapter="quant_signal_pack_rotation_proxy",
            setup_mode="rotation_proxy",
            exchanges=("binanceusdm", "bybit"),
            bases=("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"),
            strategy_parameters={
                "allowed_families": ["structure_break", "squeeze_release", "confluence"],
                "min_score": 5.25,
                "min_score_delta": 1.0,
                "take_profit_r": 2.2,
            },
            canonical_adapter=False,
            source_count_hint=1,
        ),
    )


def build_quantified_blueprint_proof_payload(
    *,
    jobs_dir: Path | str | None = None,
    artifact_dir: Path | str | None = DEFAULT_ARTIFACT_DIR,
    config: QuantifiedBlueprintProofConfig = QuantifiedBlueprintProofConfig(),
    seed_jobs: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    generated = now or datetime.now(UTC)
    jobs_path = Path(jobs_dir) if jobs_dir is not None else env_agent_jobs_dir()
    specs = _matrix_specs(config)
    jobs_before = _jobs_by_hypothesis(jobs_path, {str(spec["hypothesis_id"]) for spec in specs})
    created = 0
    if seed_jobs:
        for spec in specs:
            if spec["hypothesis_id"] in jobs_before:
                continue
            create_backtest_job(
                jobs_dir=jobs_path,
                agent=QUANTIFIED_BLUEPRINT_PROOF_ID,
                request=_request_for_spec(spec, config),
            )
            created += 1
        jobs = _jobs_by_hypothesis(jobs_path, {str(spec["hypothesis_id"]) for spec in specs})
    else:
        jobs = jobs_before

    rows = [
        _row_for_spec(
            spec,
            jobs.get(str(spec["hypothesis_id"])),
            config=config,
            artifact_dir=Path(artifact_dir) if artifact_dir is not None else None,
        )
        for spec in specs
    ]
    summary = _summary(rows, created=created, config=config)
    return {
        "proof_id": QUANTIFIED_BLUEPRINT_PROOF_ID,
        "generated_at": generated.isoformat(),
        "mode": "research_only_all_blueprint_agent_gateway_backtest_queue",
        "config": {
            **asdict(config),
            "profiles": [_profile_dict(profile) for profile in config.active_profiles],
        },
        "summary": summary,
        "ports": _ports_summary(rows),
        "rows": rows,
        "operator_answer": _operator_answer(summary),
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
    }


def publish_quantified_blueprint_proof(
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


def load_quantified_blueprint_proof_payload(path: Path | None = None) -> dict:
    if path is not None and path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {}
        if isinstance(payload, dict) and payload.get("proof_id") == QUANTIFIED_BLUEPRINT_PROOF_ID:
            return payload
    return build_quantified_blueprint_proof_payload(seed_jobs=False)


def _matrix_specs(config: QuantifiedBlueprintProofConfig) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for profile in config.active_profiles:
        for exchange in profile.exchanges:
            for base in profile.bases:
                for timeframe in profile.timeframes:
                    symbol = _symbol_for(exchange, base)
                    specs.append(
                        {
                            "hypothesis_id": _hypothesis_id(profile, exchange, symbol, timeframe),
                            "port_id": profile.port_id,
                            "strategy_id": profile.strategy_id,
                            "adapter": profile.adapter,
                            "setup_mode": profile.setup_mode,
                            "exchange": exchange,
                            "symbol": symbol,
                            "base": base,
                            "timeframe": timeframe,
                            "max_holding_bars": _max_holding_bars(timeframe, config),
                            "strategy_parameters": dict(profile.strategy_parameters),
                            "canonical_adapter": profile.canonical_adapter,
                            "source_count_hint": profile.source_count_hint,
                        }
                    )
    return specs


def _symbol_for(exchange: str, base: str) -> str:
    if exchange == "delta_india":
        return f"{base}/USD:USD"
    return f"{base}/USDT:USDT"


def _hypothesis_id(
    profile: BlueprintProofProfile,
    exchange: str,
    symbol: str,
    timeframe: str,
) -> str:
    safe_symbol = symbol.replace("/", "").replace(":", "").replace("-", "").lower()
    if profile.port_id == "pullback_reversion_pack_v1":
        return f"{QUANTIFIED_PULLBACK_REVERSION_PROOF_ID}|{exchange}|{safe_symbol}|{timeframe}"
    return (
        f"{QUANTIFIED_BLUEPRINT_PROOF_ID}|{profile.port_id}|"
        f"{exchange}|{safe_symbol}|{timeframe}"
    )


def _max_holding_bars(timeframe: str, config: QuantifiedBlueprintProofConfig) -> int:
    return {
        "1m": 30,
        "5m": 18,
        "15m": 16,
        "1h": 12,
        "4h": 8,
    }.get(timeframe, config.default_max_holding_bars)


def _request_for_spec(
    spec: Mapping[str, Any],
    config: QuantifiedBlueprintProofConfig,
) -> dict[str, Any]:
    parameters = {
        "hypothesis_id": spec["hypothesis_id"],
        "port_id": spec["port_id"],
        "proof_id": QUANTIFIED_BLUEPRINT_PROOF_ID,
        "adapter": spec["adapter"],
        "setup_mode": spec["setup_mode"],
        "canonical_adapter": bool(spec["canonical_adapter"]),
        "max_holding_bars": spec["max_holding_bars"],
        "paper_margin_usd": config.paper_margin_usd,
        "paper_leverage": config.paper_leverage,
        "paper_notional_usd": config.paper_notional_usd,
        **dict(spec.get("strategy_parameters") or {}),
    }
    return {
        "strategy_id": spec["strategy_id"],
        "exchange": spec["exchange"],
        "symbol": spec["symbol"],
        "timeframe": spec["timeframe"],
        "initial_capital_usd": config.initial_capital_usd,
        "commission_bps": None,
        "slippage_bps": None,
        "strict_mode": True,
        "live_orders_enabled": False,
        "parameters": parameters,
    }


def _jobs_by_hypothesis(jobs_dir: Path, desired: set[str]) -> dict[str, dict[str, Any]]:
    if not jobs_dir.exists():
        return {}
    out: dict[str, dict[str, Any]] = {}
    for path in sorted(jobs_dir.glob("agj_*.json")):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(job, dict):
            continue
        request = job.get("request") if isinstance(job.get("request"), dict) else {}
        params = request.get("parameters") if isinstance(request.get("parameters"), dict) else {}
        hypothesis_id = str(params.get("hypothesis_id") or "")
        if hypothesis_id not in desired:
            continue
        current = out.get(hypothesis_id)
        if current is None or str(job.get("updated_at") or "") >= str(current.get("updated_at") or ""):
            out[hypothesis_id] = job
    return out


def _row_for_spec(
    spec: Mapping[str, Any],
    job: dict[str, Any] | None,
    *,
    config: QuantifiedBlueprintProofConfig,
    artifact_dir: Path | None,
) -> dict[str, Any]:
    status = str(job.get("status") if job else STATUS_QUEUED)
    result = job.get("result") if isinstance(job, dict) and isinstance(job.get("result"), dict) else {}
    metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
    samples = _int(metrics.get("num_trades"))
    net = _float(metrics.get("net_profit_usd"))
    pf = _float(metrics.get("profit_factor"))
    win_rate = _float(metrics.get("win_rate_pct"))
    avg_net_bps = _avg_net_bps(net, samples, config)
    verdict = _verdict(status, samples, avg_net_bps, pf, config)
    artifact_path = result.get("artifact_path")
    if not artifact_path and artifact_dir is not None and job is not None:
        candidate = artifact_dir / f"{job.get('job_id')}.json"
        artifact_path = str(candidate) if candidate.exists() else None
    return {
        "hypothesis_id": spec["hypothesis_id"],
        "job_id": job.get("job_id") if job else None,
        "status": status,
        "verdict": verdict,
        "port_id": spec["port_id"],
        "strategy_id": spec["strategy_id"],
        "adapter": spec["adapter"],
        "setup_mode": spec["setup_mode"],
        "canonical_adapter": bool(spec["canonical_adapter"]),
        "exchange": spec["exchange"],
        "symbol": spec["symbol"],
        "timeframe": spec["timeframe"],
        "max_holding_bars": spec["max_holding_bars"],
        "paper_margin_usd": config.paper_margin_usd,
        "paper_leverage": config.paper_leverage,
        "paper_notional_usd": config.paper_notional_usd,
        "samples": samples,
        "net_profit_usd": net,
        "avg_net_bps": avg_net_bps,
        "profit_factor": pf,
        "win_rate_pct": win_rate,
        "blocked_reason": job.get("blocked_reason") if job else None,
        "error": job.get("error") if job else None,
        "artifact_path": artifact_path,
        "next_action": _next_action(status, verdict, bool(spec["canonical_adapter"])),
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
    }


def _avg_net_bps(
    net_profit_usd: float | None,
    samples: int,
    config: QuantifiedBlueprintProofConfig,
) -> float | None:
    if net_profit_usd is None or samples <= 0 or config.paper_notional_usd <= 0:
        return None
    return round(net_profit_usd / (config.paper_notional_usd * samples) * 10_000.0, 4)


def _verdict(
    status: str,
    samples: int,
    avg_net_bps: float | None,
    profit_factor: float | None,
    config: QuantifiedBlueprintProofConfig,
) -> str:
    if status in {PENDING_STATUS, RUNNING_STATUS, STATUS_QUEUED}:
        return "AWAITING_BACKTEST"
    if status == BLOCKED_STATUS:
        return "BLOCKED_DATA_OR_CONTRACT"
    if status == FAILED_STATUS:
        return "FAILED_REPLAY_ENGINE"
    if status != DONE_STATUS:
        return "UNKNOWN_STATUS"
    if samples <= 0:
        return "NO_TRADES"
    if avg_net_bps is None or profit_factor is None:
        return "INCOMPLETE_METRICS"
    clears_edge = avg_net_bps >= config.min_net_bps and profit_factor >= config.min_profit_factor
    if clears_edge and samples >= config.min_trades:
        return "PROMOTABLE_PROOF_REQUIRES_UNTOUCHED_JUDGMENT"
    if clears_edge:
        return "SPARSE_POSITIVE_EXTEND_SAMPLE"
    if avg_net_bps > 0 and profit_factor >= 1.0:
        return "POSITIVE_BUT_FEE_WALL_THIN"
    if avg_net_bps > -10.0:
        return "FEE_WALL_NEAR_MISS"
    return "NEGATIVE_EDGE_AFTER_COST"


def _next_action(status: str, verdict: str, canonical_adapter: bool) -> str:
    if not canonical_adapter and verdict in {
        "PROMOTABLE_PROOF_REQUIRES_UNTOUCHED_JUDGMENT",
        "SPARSE_POSITIVE_EXTEND_SAMPLE",
        "POSITIVE_BUT_FEE_WALL_THIN",
    }:
        return "BUILD_CANONICAL_PORT_BEFORE_PROMOTION_REVIEW"
    if verdict == "PROMOTABLE_PROOF_REQUIRES_UNTOUCHED_JUDGMENT":
        return "QUEUE_UNTOUCHED_WINDOW_JUDGMENT"
    if verdict == "SPARSE_POSITIVE_EXTEND_SAMPLE":
        return "EXTEND_SAMPLE_ON_NEXT_UNTOUCHED_WINDOW"
    if verdict in {"POSITIVE_BUT_FEE_WALL_THIN", "FEE_WALL_NEAR_MISS"}:
        return "MINE_EXIT_CAPTURE_AND_ROUTE_FILTERS"
    if verdict == "AWAITING_BACKTEST":
        return "WAIT_FOR_AGENT_JOB_RUNNER"
    if status == BLOCKED_STATUS:
        return "REPAIR_DATA_COVERAGE_OR_SYMBOL_MAPPING"
    if status == FAILED_STATUS:
        return "REPAIR_REPLAY_ENGINE"
    return "KEEP_RESEARCH_ONLY"


def _summary(
    rows: list[dict[str, Any]],
    *,
    created: int,
    config: QuantifiedBlueprintProofConfig,
) -> dict[str, Any]:
    statuses = Counter(str(row["status"]) for row in rows)
    verdicts = Counter(str(row["verdict"]) for row in rows)
    completed = [row for row in rows if row["status"] == DONE_STATUS]
    matched_jobs = sum(1 for row in rows if row.get("job_id"))
    positive = [
        row
        for row in completed
        if row["avg_net_bps"] is not None and float(row["avg_net_bps"]) > 0.0
    ]
    promotable = [
        row
        for row in completed
        if row["verdict"] == "PROMOTABLE_PROOF_REQUIRES_UNTOUCHED_JUDGMENT"
    ]
    sparse = [row for row in completed if row["verdict"] == "SPARSE_POSITIVE_EXTEND_SAMPLE"]
    best = max(
        (row for row in completed if row["avg_net_bps"] is not None),
        key=lambda row: float(row["avg_net_bps"]),
        default=None,
    )
    return {
        "total_cells": len(rows),
        "ports": len({str(row["port_id"]) for row in rows}),
        "jobs_created": created,
        "jobs_reused": max(0, matched_jobs - created),
        "matched_jobs": matched_jobs,
        "completed_cells": len(completed),
        "pending_cells": statuses.get(PENDING_STATUS, 0),
        "running_cells": statuses.get(RUNNING_STATUS, 0),
        "blocked_cells": statuses.get(BLOCKED_STATUS, 0),
        "failed_cells": statuses.get(FAILED_STATUS, 0),
        "queued_cells": statuses.get(STATUS_QUEUED, 0),
        "positive_cells": len(positive),
        "sparse_positive_cells": len(sparse),
        "promotable_proof_candidates": len(promotable),
        "proxy_adapter_cells": sum(1 for row in rows if not row.get("canonical_adapter")),
        "status_counts": dict(statuses),
        "verdict_counts": dict(verdicts),
        "best_avg_net_bps": None if best is None else best["avg_net_bps"],
        "best_profit_factor": None if best is None else best["profit_factor"],
        "best_lane": None if best is None else {
            "port_id": best["port_id"],
            "exchange": best["exchange"],
            "symbol": best["symbol"],
            "timeframe": best["timeframe"],
            "verdict": best["verdict"],
        },
        "gate": {
            "min_net_bps": config.min_net_bps,
            "min_profit_factor": config.min_profit_factor,
            "min_trades": config.min_trades,
        },
        "paper_profile": {
            "margin_usd": config.paper_margin_usd,
            "leverage": config.paper_leverage,
            "notional_usd": config.paper_notional_usd,
        },
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
    }


def _ports_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_port: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_port[str(row["port_id"])].append(row)
    out: list[dict[str, Any]] = []
    for port, port_rows in by_port.items():
        completed = [r for r in port_rows if r["status"] == DONE_STATUS]
        best = max(
            (r for r in completed if r["avg_net_bps"] is not None),
            key=lambda r: float(r["avg_net_bps"]),
            default=None,
        )
        out.append({
            "port_id": port,
            "strategy_id": port_rows[0]["strategy_id"],
            "adapter": port_rows[0]["adapter"],
            "setup_mode": port_rows[0]["setup_mode"],
            "canonical_adapter": bool(port_rows[0]["canonical_adapter"]),
            "total_cells": len(port_rows),
            "completed_cells": len(completed),
            "positive_cells": sum(
                1 for r in completed
                if r["avg_net_bps"] is not None and float(r["avg_net_bps"]) > 0.0
            ),
            "promotable_proof_candidates": sum(
                1 for r in completed
                if r["verdict"] == "PROMOTABLE_PROOF_REQUIRES_UNTOUCHED_JUDGMENT"
            ),
            "best_avg_net_bps": None if best is None else best["avg_net_bps"],
            "best_profit_factor": None if best is None else best["profit_factor"],
            "best_lane": None if best is None else {
                "exchange": best["exchange"],
                "symbol": best["symbol"],
                "timeframe": best["timeframe"],
                "verdict": best["verdict"],
            },
        })
    return sorted(
        out,
        key=lambda row: (
            0 if row["best_avg_net_bps"] is not None else 1,
            -(float(row["best_avg_net_bps"]) if row["best_avg_net_bps"] is not None else -1e9),
            str(row["port_id"]),
        ),
    )


def _operator_answer(summary: dict[str, Any]) -> str:
    if summary["promotable_proof_candidates"]:
        return (
            f"{summary['promotable_proof_candidates']} blueprint proof cell(s) clear "
            "the exploratory gate; queue untouched-window judgment before any paper promotion."
        )
    if summary["completed_cells"]:
        return (
            f"{summary['completed_cells']}/{summary['total_cells']} blueprint cells completed "
            f"across {summary['ports']} ports; {summary['positive_cells']} are positive after costs, "
            f"{summary['blocked_cells']} blocked, {summary['failed_cells']} failed."
        )
    if summary["jobs_created"]:
        return (
            f"Seeded {summary['jobs_created']} blueprint proof backtest job(s); "
            "waiting for Agent Gateway runner evidence."
        )
    if summary["pending_cells"] or summary["running_cells"]:
        return "Blueprint proof jobs are queued/running; waiting for Agent Gateway runner evidence."
    return "Blueprint proof queue has no completed evidence yet; seed jobs to start the replay matrix."


def _profile_dict(profile: BlueprintProofProfile) -> dict[str, Any]:
    return {
        "port_id": profile.port_id,
        "strategy_id": profile.strategy_id,
        "adapter": profile.adapter,
        "setup_mode": profile.setup_mode,
        "exchanges": list(profile.exchanges),
        "bases": list(profile.bases),
        "timeframes": list(profile.timeframes),
        "strategy_parameters": dict(profile.strategy_parameters),
        "canonical_adapter": profile.canonical_adapter,
        "source_count_hint": profile.source_count_hint,
    }


def _feed_record(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "ts": payload.get("generated_at"),
        "proof_id": payload.get("proof_id"),
        "summary": payload.get("summary"),
        "ports": payload.get("ports"),
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
    }


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
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _parse_csv(raw: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if raw is None or not raw.strip():
        return default
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _filter_profiles(
    profiles: tuple[BlueprintProofProfile, ...],
    wanted: tuple[str, ...],
) -> tuple[BlueprintProofProfile, ...]:
    if not wanted:
        return profiles
    wanted_set = set(wanted)
    return tuple(profile for profile in profiles if profile.port_id in wanted_set)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish Quantified all-blueprint proof queue")
    parser.add_argument("--jobs-dir", type=Path, default=env_agent_jobs_dir())
    parser.add_argument("--artifact-dir", type=Path, default=DEFAULT_ARTIFACT_DIR)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--feed", type=Path, default=DEFAULT_FEED)
    parser.add_argument("--ports", default="")
    parser.add_argument("--seed-jobs", action="store_true")
    parser.add_argument("--no-feed", action="store_true")
    parser.add_argument("--interval-seconds", type=float, default=0.0)
    args = parser.parse_args(argv)

    config = QuantifiedBlueprintProofConfig(
        profiles=_filter_profiles(default_profiles(), _parse_csv(args.ports, ())),
    )
    while True:
        payload = build_quantified_blueprint_proof_payload(
            jobs_dir=args.jobs_dir,
            artifact_dir=args.artifact_dir,
            config=config,
            seed_jobs=args.seed_jobs,
        )
        publish_quantified_blueprint_proof(
            payload,
            out=args.out,
            feed=None if args.no_feed else args.feed,
        )
        s = payload["summary"]
        print(
            "quantified blueprint proof "
            f"{s['completed_cells']}/{s['total_cells']} completed / "
            f"{s['jobs_created']} jobs created / "
            f"{s['promotable_proof_candidates']} candidates",
            flush=True,
        )
        if args.interval_seconds <= 0:
            return 0
        time.sleep(args.interval_seconds)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
