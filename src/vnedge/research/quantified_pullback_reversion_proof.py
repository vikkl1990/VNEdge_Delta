"""Research-only proof queue for Quantified pullback/reversion ports.

The Quantified Strategy Lab names ``pullback_reversion_pack_v1`` as the first
Chunk-A proof lane, but the lab page should not imply proof until replay jobs
exist and finish. This publisher seeds durable Agent Gateway backtest jobs for
the VNEDGE-owned ``quantified_fee_wall_sniper_v1`` scanner in pullback-only
mode, then summarizes the terminal job evidence back to the dashboard.

No row can trade or promote from this artifact. It is the bridge from idea
inventory to cost-aware evidence.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
import math
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
import time
from typing import Any

from vnedge.agent_gateway.jobs import (
    BLOCKED_STATUS,
    DONE_STATUS,
    FAILED_STATUS,
    PENDING_STATUS,
    RUNNING_STATUS,
    create_backtest_job,
)
from vnedge.strategy.quantified_fee_wall_sniper import QUANTIFIED_FEE_WALL_SNIPER_ID


QUANTIFIED_PULLBACK_REVERSION_PROOF_ID = "quantified_pullback_reversion_proof_v1"
PORT_ID = "pullback_reversion_pack_v1"
DEFAULT_OUT = Path("research/live_research/quantified_pullback_reversion_proof_latest.json")
DEFAULT_FEED = Path("research/live_research/quantified_pullback_reversion_proof_feed.jsonl")
DEFAULT_ARTIFACT_DIR = Path("research/live_research/agent_jobs")
DEFAULT_TIMEFRAMES = ("5m", "15m", "1h", "4h")
DEFAULT_EXCHANGES = ("binanceusdm", "bybit", "delta_india")
DEFAULT_BASES = ("BTC", "ETH", "SOL", "XRP")
TERMINAL_STATUSES = frozenset({DONE_STATUS, BLOCKED_STATUS, FAILED_STATUS})
STATUS_QUEUED = "QUEUED_RESEARCH_ONLY"


def env_agent_jobs_dir(env: dict[str, str] | None = None) -> Path:
    source = os.environ if env is None else env
    return Path(source.get("AGENT_GATEWAY_JOBS_DIR", "logs/agent_gateway/jobs"))


@dataclass(frozen=True)
class QuantifiedPullbackProofConfig:
    exchanges: tuple[str, ...] = DEFAULT_EXCHANGES
    bases: tuple[str, ...] = DEFAULT_BASES
    timeframes: tuple[str, ...] = DEFAULT_TIMEFRAMES
    initial_capital_usd: float = 100.0
    paper_margin_usd: float = 100.0
    paper_leverage: float = 25.0
    min_net_bps: float = 25.0
    min_profit_factor: float = 1.50
    min_trades: int = 20
    default_max_holding_bars: int = 16

    def __post_init__(self) -> None:
        if not self.exchanges:
            raise ValueError("exchanges cannot be empty")
        if not self.bases:
            raise ValueError("bases cannot be empty")
        if not self.timeframes:
            raise ValueError("timeframes cannot be empty")
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


def build_quantified_pullback_reversion_proof_payload(
    *,
    jobs_dir: Path | str | None = None,
    artifact_dir: Path | str | None = DEFAULT_ARTIFACT_DIR,
    config: QuantifiedPullbackProofConfig = QuantifiedPullbackProofConfig(),
    seed_jobs: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    generated = now or datetime.now(UTC)
    jobs_path = Path(jobs_dir) if jobs_dir is not None else env_agent_jobs_dir()
    specs = _matrix_specs(config)
    jobs_before = _jobs_by_hypothesis(jobs_path)
    created = 0
    if seed_jobs:
        for spec in specs:
            if spec["hypothesis_id"] in jobs_before:
                continue
            create_backtest_job(
                jobs_dir=jobs_path,
                agent=QUANTIFIED_PULLBACK_REVERSION_PROOF_ID,
                request=_request_for_spec(spec, config),
            )
            created += 1
        jobs = _jobs_by_hypothesis(jobs_path)
    else:
        jobs = jobs_before

    rows = [
        _row_for_spec(
            spec,
            jobs.get(spec["hypothesis_id"]),
            config=config,
            artifact_dir=Path(artifact_dir) if artifact_dir is not None else None,
        )
        for spec in specs
    ]
    summary = _summary(rows, created=created, config=config)
    return {
        "proof_id": QUANTIFIED_PULLBACK_REVERSION_PROOF_ID,
        "generated_at": generated.isoformat(),
        "port_id": PORT_ID,
        "strategy_id": QUANTIFIED_FEE_WALL_SNIPER_ID,
        "mode": "research_only_agent_gateway_backtest_queue",
        "config": asdict(config),
        "summary": summary,
        "rows": rows,
        "operator_answer": _operator_answer(summary, rows),
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
    }


def publish_quantified_pullback_reversion_proof(
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


def load_quantified_pullback_reversion_proof_payload(path: Path | None = None) -> dict:
    if path is not None and path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {}
        if (
            isinstance(payload, dict)
            and payload.get("proof_id") == QUANTIFIED_PULLBACK_REVERSION_PROOF_ID
        ):
            return payload
    return build_quantified_pullback_reversion_proof_payload(seed_jobs=False)


def _matrix_specs(config: QuantifiedPullbackProofConfig) -> list[dict[str, str | int]]:
    specs: list[dict[str, str | int]] = []
    for exchange in config.exchanges:
        for base in config.bases:
            for timeframe in config.timeframes:
                symbol = _symbol_for(exchange, base)
                hypothesis_id = _hypothesis_id(exchange, symbol, timeframe)
                specs.append(
                    {
                        "hypothesis_id": hypothesis_id,
                        "exchange": exchange,
                        "symbol": symbol,
                        "base": base,
                        "timeframe": timeframe,
                        "max_holding_bars": _max_holding_bars(timeframe, config),
                    }
                )
    return specs


def _symbol_for(exchange: str, base: str) -> str:
    if exchange == "delta_india":
        return f"{base}/USD:USD"
    return f"{base}/USDT:USDT"


def _hypothesis_id(exchange: str, symbol: str, timeframe: str) -> str:
    safe_symbol = symbol.replace("/", "").replace(":", "").replace("-", "").lower()
    return f"{QUANTIFIED_PULLBACK_REVERSION_PROOF_ID}|{exchange}|{safe_symbol}|{timeframe}"


def _max_holding_bars(timeframe: str, config: QuantifiedPullbackProofConfig) -> int:
    return {
        "1m": 30,
        "5m": 18,
        "15m": 16,
        "1h": 12,
        "4h": 8,
    }.get(timeframe, config.default_max_holding_bars)


def _request_for_spec(
    spec: dict[str, str | int],
    config: QuantifiedPullbackProofConfig,
) -> dict[str, Any]:
    return {
        "strategy_id": QUANTIFIED_FEE_WALL_SNIPER_ID,
        "exchange": spec["exchange"],
        "symbol": spec["symbol"],
        "timeframe": spec["timeframe"],
        "initial_capital_usd": config.initial_capital_usd,
        "commission_bps": None,
        "slippage_bps": None,
        "strict_mode": True,
        "live_orders_enabled": False,
        "parameters": {
            "hypothesis_id": spec["hypothesis_id"],
            "port_id": PORT_ID,
            "proof_id": QUANTIFIED_PULLBACK_REVERSION_PROOF_ID,
            "max_holding_bars": spec["max_holding_bars"],
            "paper_margin_usd": config.paper_margin_usd,
            "paper_leverage": config.paper_leverage,
            "paper_notional_usd": config.paper_notional_usd,
            "entry_profile": "pullback_only_close_confirmed_next_open",
            "exit_profile": "TP1_partial_BE_then_trail_time_stop",
            "params": {
                "enabled_setups": ["pullback"],
                "min_expected_net_edge_bps": config.min_net_bps,
            },
        },
    }


def _jobs_by_hypothesis(jobs_dir: Path) -> dict[str, dict[str, Any]]:
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
        if not hypothesis_id.startswith(QUANTIFIED_PULLBACK_REVERSION_PROOF_ID):
            continue
        current = out.get(hypothesis_id)
        if current is None or str(job.get("updated_at") or "") >= str(current.get("updated_at") or ""):
            out[hypothesis_id] = job
    return out


def _row_for_spec(
    spec: dict[str, str | int],
    job: dict[str, Any] | None,
    *,
    config: QuantifiedPullbackProofConfig,
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
        "exchange": spec["exchange"],
        "symbol": spec["symbol"],
        "timeframe": spec["timeframe"],
        "strategy_id": QUANTIFIED_FEE_WALL_SNIPER_ID,
        "port_id": PORT_ID,
        "setup_mode": "pullback_only",
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
        "next_action": _next_action(status, verdict),
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
    }


def _avg_net_bps(
    net_profit_usd: float | None,
    samples: int,
    config: QuantifiedPullbackProofConfig,
) -> float | None:
    if net_profit_usd is None or samples <= 0 or config.paper_notional_usd <= 0:
        return None
    return round(net_profit_usd / (config.paper_notional_usd * samples) * 10_000.0, 4)


def _verdict(
    status: str,
    samples: int,
    avg_net_bps: float | None,
    profit_factor: float | None,
    config: QuantifiedPullbackProofConfig,
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


def _next_action(status: str, verdict: str) -> str:
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
    config: QuantifiedPullbackProofConfig,
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
        "status_counts": dict(statuses),
        "verdict_counts": dict(verdicts),
        "best_avg_net_bps": None if best is None else best["avg_net_bps"],
        "best_profit_factor": None if best is None else best["profit_factor"],
        "best_lane": None if best is None else {
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


def _operator_answer(summary: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    if summary["promotable_proof_candidates"]:
        return (
            f"{summary['promotable_proof_candidates']} pullback proof cell(s) clear "
            "the exploratory gate; queue untouched-window judgment before any paper promotion."
        )
    if summary["completed_cells"]:
        return (
            f"{summary['completed_cells']}/{summary['total_cells']} pullback cells completed; "
            f"{summary['positive_cells']} are positive after costs, "
            f"{summary['blocked_cells']} blocked, {summary['failed_cells']} failed."
        )
    if summary["jobs_created"]:
        return (
            f"Seeded {summary['jobs_created']} pullback proof backtest job(s); "
            "waiting for Agent Gateway runner evidence."
        )
    if any(row["status"] in {PENDING_STATUS, RUNNING_STATUS} for row in rows):
        return "Pullback proof jobs are queued/running; waiting for Agent Gateway runner evidence."
    return "Pullback proof queue has no completed evidence yet; seed jobs to start the replay matrix."


def _feed_record(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "ts": payload.get("generated_at"),
        "proof_id": payload.get("proof_id"),
        "summary": payload.get("summary"),
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish Quantified pullback proof queue")
    parser.add_argument("--jobs-dir", type=Path, default=env_agent_jobs_dir())
    parser.add_argument("--artifact-dir", type=Path, default=DEFAULT_ARTIFACT_DIR)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--feed", type=Path, default=DEFAULT_FEED)
    parser.add_argument("--exchanges", default=",".join(DEFAULT_EXCHANGES))
    parser.add_argument("--bases", default=",".join(DEFAULT_BASES))
    parser.add_argument("--timeframes", default=",".join(DEFAULT_TIMEFRAMES))
    parser.add_argument("--seed-jobs", action="store_true")
    parser.add_argument("--no-feed", action="store_true")
    parser.add_argument("--interval-seconds", type=float, default=0.0)
    args = parser.parse_args(argv)

    config = QuantifiedPullbackProofConfig(
        exchanges=_parse_csv(args.exchanges, DEFAULT_EXCHANGES),
        bases=_parse_csv(args.bases, DEFAULT_BASES),
        timeframes=_parse_csv(args.timeframes, DEFAULT_TIMEFRAMES),
    )
    while True:
        payload = build_quantified_pullback_reversion_proof_payload(
            jobs_dir=args.jobs_dir,
            artifact_dir=args.artifact_dir,
            config=config,
            seed_jobs=args.seed_jobs,
        )
        publish_quantified_pullback_reversion_proof(
            payload,
            out=args.out,
            feed=None if args.no_feed else args.feed,
        )
        s = payload["summary"]
        print(
            "quantified pullback proof "
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
