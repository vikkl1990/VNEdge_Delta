"""Isolated publisher for the Delta event-time research readiness gate.

This process reads finalized recorder shards, advances the recoverable
continuity epoch, and refreshes the production-readiness projection.  It owns
no scanner, broker, account client, or order route.  A failed audit is data,
not permission: every artifact remains ``can_trade=false``.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from vnedge.research.event_continuity import qualify_event_continuity
from vnedge.runtime.scanner_authority import publish_production_readiness

DEFAULT_STATUS = Path(
    "research/live_research/event_readiness_publisher_latest.json"
)


def code_version(repo: Path | None = None) -> str:
    repo = repo or Path.cwd()
    configured = os.environ.get("VNEDGE_BUILD_SHA", "").strip()
    if configured:
        return configured
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).strip()
        dirty = bool(
            subprocess.check_output(
                ["git", "status", "--porcelain", "--untracked-files=normal"],
                cwd=repo,
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=5,
            ).strip()
        )
    except (OSError, subprocess.SubprocessError):
        return "local-unversioned"
    return f"{commit}{'+dirty' if dirty else ''}"


def publish_once(
    *,
    event_root: Path,
    contract: Path,
    continuity_output: Path,
    continuity_cache: Path,
    production_manifest: Path,
    production_output: Path,
    status_output: Path,
    version: str,
) -> dict[str, Any]:
    started = time.monotonic()
    continuity = qualify_event_continuity(
        event_root,
        contract_path=contract,
        output_path=continuity_output,
        cache_path=continuity_cache,
        code_version=version,
    )
    production = publish_production_readiness(
        production_output,
        production_manifest,
    )
    qualification = dict(continuity.get("qualification") or {})
    payload = {
        "schema_version": "vnedge.event_readiness_publisher.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "code_version": version,
        "status": "READY" if qualification.get("data_ready") is True else "COLLECTING",
        "duration_seconds": time.monotonic() - started,
        "qualified_events": int(qualification.get("qualified_events") or 0),
        "target_events": int(qualification.get("target_events") or 0),
        "qualified_days": float(qualification.get("qualified_days") or 0.0),
        "target_days": float(qualification.get("target_days") or 0.0),
        "blockers": list(qualification.get("blockers") or []),
        "scanner_implementation_authorized": False,
        "selection_authorized": False,
        "production_scanner_state": (production.get("scanner") or {}).get("state"),
        "research_only": True,
        "order_route": "absent",
        "can_trade": False,
        "can_promote": False,
    }
    _atomic_json(status_output, payload)
    return payload


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-root", type=Path, default=Path("data/delta_events"))
    parser.add_argument(
        "--contract",
        type=Path,
        default=Path("configs/research/failed_auction_response_v1.yaml"),
    )
    parser.add_argument(
        "--continuity-output",
        type=Path,
        default=Path("research/live_research/delta_event_continuity_latest.json"),
    )
    parser.add_argument(
        "--continuity-cache",
        type=Path,
        default=Path("research/live_research/delta_event_continuity_shard_cache.json"),
    )
    parser.add_argument(
        "--production-manifest", type=Path, default=Path("configs/production_live.yaml")
    )
    parser.add_argument(
        "--production-output",
        type=Path,
        default=Path("research/live_research/production_readiness_latest.json"),
    )
    parser.add_argument("--status-output", type=Path, default=DEFAULT_STATUS)
    parser.add_argument("--interval-seconds", type=float, default=300.0)
    parser.add_argument("--once", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.interval_seconds < 30 and not args.once:
        raise SystemExit("interval must be at least 30 seconds")
    stopped = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    version = code_version()
    while not stopped:
        try:
            payload = publish_once(
                event_root=args.event_root,
                contract=args.contract,
                continuity_output=args.continuity_output,
                continuity_cache=args.continuity_cache,
                production_manifest=args.production_manifest,
                production_output=args.production_output,
                status_output=args.status_output,
                version=version,
            )
            print(json.dumps(payload, sort_keys=True), flush=True)
        except Exception as exc:  # noqa: BLE001 - status remains fail-closed
            error = {
                "schema_version": "vnedge.event_readiness_publisher.v1",
                "generated_at": datetime.now(UTC).isoformat(),
                "status": "ERROR",
                "error": f"{type(exc).__name__}: {str(exc)[:500]}",
                "research_only": True,
                "scanner_implementation_authorized": False,
                "selection_authorized": False,
                "order_route": "absent",
                "can_trade": False,
                "can_promote": False,
            }
            _atomic_json(args.status_output, error)
            print(json.dumps(error, sort_keys=True), flush=True)
        if args.once:
            break
        deadline = time.monotonic() + args.interval_seconds
        while not stopped and time.monotonic() < deadline:
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
