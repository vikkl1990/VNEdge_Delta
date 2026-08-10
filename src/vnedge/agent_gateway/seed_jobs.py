"""Idempotent starter jobs for the Agent Gateway research runner.

These seeds make a fresh Quant OS deployment visibly useful without opening
any execution path. They are ordinary Agent Gateway jobs: research-only,
``strict_mode=true``, ``live_orders_enabled=false``, and every result still
requires the normal untouched-data judgment before promotion.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from vnedge.agent_gateway.app import env_agent_jobs_dir
from vnedge.agent_gateway.jobs import create_backtest_job, list_jobs

SEED_AGENT = "quantos_seed"

DEFAULT_SEED_REQUESTS: tuple[dict[str, Any], ...] = (
    {
        "seed_id": "quantos_seed_sats_5m_delta_eth",
        "strategy_id": "sats_5m_scalper_v1",
        "exchange": "delta_india",
        "symbol": "ETH/USD:USD",
        "timeframe": "5m",
        "hypothesis_id": "quantos_seed_sats_5m_delta_eth",
        "notes": "Starter 5m SATS/BBP/stealth-trail scalper evidence job.",
        "initial_capital_usd": 500.0,
        "commission_bps": None,
        "slippage_bps": None,
        "strict_mode": True,
        "live_orders_enabled": False,
        "parameters": {
            "seed_id": "quantos_seed_sats_5m_delta_eth",
            "min_tqi": 0.58,
            "min_quality_strength": 0.08,
            "min_momentum_persistence": 0.55,
            "max_holding_bars": 16,
            "seed_family": "candle_scalper_backtest",
        },
    },
    {
        "seed_id": "quantos_seed_candidate_replay",
        "strategy_id": "candidate_replay_executor_v1",
        "exchange": "delta_india",
        "symbol": "ETH/USDT:USDT",
        "timeframe": "1m",
        "hypothesis_id": "quantos_seed_candidate_replay",
        "notes": "Starter conservative replay proof task for L2/order-flow candidates.",
        "initial_capital_usd": 500.0,
        "commission_bps": None,
        "slippage_bps": None,
        "strict_mode": True,
        "live_orders_enabled": False,
        "parameters": {
            "seed_id": "quantos_seed_candidate_replay",
            "adapter": "candidate_replay",
            "max_event_leadlag": 3,
            "max_orderflow": 10,
            "min_replay_fills": 5,
            "queue_aware": True,
            "seed_family": "candidate_replay",
        },
    },
    {
        "seed_id": "quantos_seed_ai_ma_cross_btc",
        "strategy_id": "ai_example_ma_cross",
        "exchange": "binanceusdm",
        "symbol": "BTC/USDT:USDT",
        "timeframe": "1h",
        "hypothesis_id": "quantos_seed_ai_ma_cross_btc",
        "notes": "Starter AI-sandbox candidate research job; illustrative, not promoted.",
        "initial_capital_usd": 500.0,
        "commission_bps": None,
        "slippage_bps": None,
        "strict_mode": True,
        "live_orders_enabled": False,
        "parameters": {
            "seed_id": "quantos_seed_ai_ma_cross_btc",
            "train_bars": 1440,
            "test_bars": 720,
            "seed_family": "ai_candidate",
        },
    },
)


def seed_default_jobs(
    jobs_dir: Path | str | None = None,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create the default Quant OS starter jobs once per ledger."""
    path = Path(jobs_dir) if jobs_dir is not None else env_agent_jobs_dir()
    existing = _existing_seed_signatures(path)
    created: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []

    for request in DEFAULT_SEED_REQUESTS:
        seed_id = str(request["seed_id"])
        signature = _seed_signature(request)
        if (seed_id, signature) in existing:
            skipped.append({"seed_id": seed_id, "reason": "already_present"})
            continue

        job_request = deepcopy(request)
        job_request.pop("seed_id", None)
        job_request["strict_mode"] = True
        job_request["live_orders_enabled"] = False
        job_request.setdefault("parameters", {})["seed_signature"] = signature
        if dry_run:
            created.append({"seed_id": seed_id, "status": "DRY_RUN"})
            continue

        job = create_backtest_job(
            jobs_dir=path,
            agent=SEED_AGENT,
            request=job_request,
        )
        created.append(
            {
                "seed_id": seed_id,
                "job_id": str(job["job_id"]),
                "status": str(job["status"]),
            }
        )
        existing.add((seed_id, signature))

    return {
        "jobs_dir": str(path),
        "created": created,
        "skipped": skipped,
        "created_count": len(created),
        "skipped_count": len(skipped),
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
    }


def _existing_seed_signatures(jobs_dir: Path) -> set[tuple[str, str]]:
    signatures: set[tuple[str, str]] = set()
    for job in list_jobs(jobs_dir, limit=500):
        request = job.get("request") if isinstance(job.get("request"), dict) else {}
        params = request.get("parameters") if isinstance(request.get("parameters"), dict) else {}
        seed_id = request.get("hypothesis_id") or params.get("seed_id")
        if not seed_id:
            continue
        stored_signature = params.get("seed_signature")
        signature = str(stored_signature or _seed_signature({"seed_id": seed_id, **request}))
        signatures.add((str(seed_id), signature))
    return signatures


def _seed_signature(request: dict[str, Any]) -> str:
    payload = deepcopy(request)
    payload.pop("notes", None)
    params = payload.get("parameters") if isinstance(payload.get("parameters"), dict) else {}
    params.pop("seed_signature", None)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed safe Quant OS research jobs")
    parser.add_argument(
        "--jobs-dir",
        default=str(env_agent_jobs_dir()),
        help="Agent Gateway jobs directory",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    payload = seed_default_jobs(Path(args.jobs_dir), dry_run=args.dry_run)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            "seeded "
            f"{payload['created_count']} Quant OS job(s), "
            f"skipped {payload['skipped_count']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
