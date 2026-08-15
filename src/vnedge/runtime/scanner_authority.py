"""Single source of truth for scanner and production-ladder readiness.

The dashboard, operators, and runtime all consume the same immutable manifest
and evidence artifacts.  File presence is never promoted to eligibility: the
reported state is derived from actual preregistered gate checks and explicit
signed-proof availability.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from vnedge.governance.signed_envelope import (
    load_governance_keyring,
    load_signed_stage_authorization,
)

DEFAULT_MANIFEST = Path("configs/production_live.yaml")


def build_production_readiness(
    manifest_path: str | Path = DEFAULT_MANIFEST,
) -> dict[str, Any]:
    path = Path(manifest_path)
    manifest = _yaml(path)
    authority = _mapping(manifest.get("scanner_authority"))
    evidence_path = _resolve(path, str(authority.get("selection_evidence") or ""))
    evidence = _json(evidence_path)
    scanner_id = str(authority.get("scanner_id") or "")
    experiments = _mapping(evidence.get("experiments"))
    experiment = _mapping(experiments.get(scanner_id))
    selection = _mapping(experiment.get("selection"))
    metrics = _mapping(selection.get("metrics"))
    gate = _mapping(selection.get("gate"))
    checks = _mapping(gate.get("checks"))
    minimum = int(authority.get("selection_minimum_trades") or 0)
    observed = int(metrics.get("trades") or 0)
    evidence_matches = (
        bool(experiment)
        and experiment.get("contract", {}).get("scanner_id") == scanner_id
        and observed >= 0
    )
    selection_passed = evidence_matches and gate.get("passed") is True
    untouched = _mapping(experiment.get("untouched"))
    untouched_opened = untouched.get("loaded") is True

    proof_path = os.environ.get("VNEDGE_STAGE_AUTHORIZATION", "").strip()
    keyring_path = os.environ.get("VNEDGE_GOVERNANCE_KEYRING", "").strip()
    signed_authorization_parseable = False
    if proof_path and keyring_path:
        try:
            load_signed_stage_authorization(proof_path)
            load_governance_keyring(keyring_path)
            signed_authorization_parseable = True
        except ValueError:
            signed_authorization_parseable = False

    blockers: list[str] = []
    if not evidence_matches:
        blockers.append("selection evidence is missing or does not match scanner contract")
    if observed < minimum:
        blockers.append(f"selection sample {observed}/{minimum} trades")
    for name, passed in checks.items():
        if passed is not True:
            blockers.append(f"selection gate failed: {name}")
    if not selection_passed:
        blockers.append("preregistered selection gate has not passed")
    if not untouched_opened:
        blockers.append("sealed untouched evaluation has not been authorized or run")
    if not signed_authorization_parseable:
        blockers.append("no parseable signed stage authorization and trusted keyring")

    scanner_observing = authority.get("enabled_for_observation") is True
    scanner_state = (
        "OBSERVATION_ONLY"
        if scanner_observing
        else "BLOCKED_BY_SELECTION"
        if evidence_matches and not selection_passed
        else "DISABLED"
    )
    paper_allowed = (
        selection_passed
        and untouched_opened
        and authority.get("enabled_for_paper") is True
    )
    live_allowed = paper_allowed and authority.get("enabled_for_live") is True
    ladder = [
        _stage("research", True, "causal scanner code and selection evidence available"),
        _stage(
            "observation",
            scanner_observing,
            "collects sparse swing candidates only; no fills or scalping-edge claim",
        ),
        _stage("selection", selection_passed, f"{observed}/{minimum} trades"),
        _stage("untouched", untouched_opened, str(untouched.get("status") or "sealed")),
        _stage("paper", paper_allowed, "requires signed PaperEligibilityProof"),
        _stage("shadow", False, "requires completed paper proof"),
        _stage("live_small", live_allowed, "requires signed single-use authorization"),
        _stage("live_full", False, "requires completed live-small proof"),
    ]
    return {
        "schema_version": "vnedge.production_readiness.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "venue": manifest.get("venue"),
        "markets": manifest.get("markets", []),
        "scanner": {
            "scanner_id": scanner_id,
            "state": scanner_state,
            "hypothesis_class": authority.get("hypothesis_class"),
            "trade_horizon": authority.get("trade_horizon"),
            "maximum_hold_seconds": authority.get("maximum_hold_seconds"),
            "edge_claim": authority.get("edge_claim"),
            "selection_trades": observed,
            "selection_minimum": minimum,
            "average_net_bps": metrics.get("average_net_bps"),
            "profit_factor": metrics.get("profit_factor"),
            "selection_passed": selection_passed,
            "untouched_status": untouched.get("status", "sealed"),
        },
        "ladder": ladder,
        "blockers": list(dict.fromkeys(blockers)),
        "authority": {
            "paper_allowed": paper_allowed,
            "live_allowed": live_allowed,
            "signed_authorization_parseable": signed_authorization_parseable,
            "can_trade": False,
            "can_promote": False,
        },
    }


def publish_production_readiness(
    output: str | Path,
    manifest_path: str | Path = DEFAULT_MANIFEST,
) -> dict[str, Any]:
    payload = build_production_readiness(manifest_path)
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)
    return payload


def _stage(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"stage": name, "passed": bool(passed), "detail": detail}


def _resolve(manifest: Path, locator: str) -> Path:
    candidate = Path(locator)
    if candidate.is_absolute():
        return candidate
    local_candidate = manifest.parent / candidate
    if local_candidate.exists():
        return local_candidate
    if candidate.exists():
        return candidate
    repo_candidate = manifest.parent.parent / candidate
    return repo_candidate


def _mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _yaml(path: Path) -> dict[str, Any]:
    try:
        payload = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"unable to load production manifest: {path}") from exc
    if not isinstance(payload, dict):
        raise TypeError("production manifest must be an object")
    return payload


def _json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return _mapping(payload)


def main() -> None:
    import argparse
    import time

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("research/live_research/production_readiness_latest.json"),
    )
    parser.add_argument("--interval-seconds", type=float, default=0.0)
    args = parser.parse_args()
    if args.interval_seconds < 0:
        parser.error("--interval-seconds cannot be negative")
    while True:
        print(json.dumps(publish_production_readiness(args.out, args.manifest), indent=2))
        if args.interval_seconds == 0:
            break
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    main()
