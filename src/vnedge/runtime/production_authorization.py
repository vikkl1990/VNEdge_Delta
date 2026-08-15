"""Fail-closed loader for signed operating-mode authorizations."""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

from vnedge.governance.signed_envelope import (
    NonceReplayStore,
    SignedStageAuthorization,
    load_governance_keyring,
    load_signed_stage_authorization,
)
from vnedge.research.strategy_evidence_registry import (
    DEFAULT_REGISTRY,
    build_registry_snapshot,
    strategy_authority_blockers,
)

_SHA256_HEX = frozenset("0123456789abcdef")


@dataclass(frozen=True)
class ProductionAuthorizationResult:
    authorized: bool
    blockers: tuple[str, ...]
    authorization: SignedStageAuthorization | None = None


def current_source_commit() -> str:
    override = os.environ.get("VNEDGE_BUILD_SHA", "").strip()
    if override and override != "dev":
        return override
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unavailable"


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_production_authorization(
    *,
    strategy_id: str,
    symbol: str,
    target_stage: str,
    config_path: str | Path,
    consume: bool,
) -> ProductionAuthorizationResult:
    authorization_path = os.environ.get("VNEDGE_STAGE_AUTHORIZATION", "").strip()
    keyring_path = os.environ.get("VNEDGE_GOVERNANCE_KEYRING", "").strip()
    if not authorization_path:
        return ProductionAuthorizationResult(False, ("signed stage authorization is missing",))
    if not keyring_path:
        return ProductionAuthorizationResult(False, ("governance keyring is missing",))
    try:
        authorization = load_signed_stage_authorization(authorization_path)
        keyring = load_governance_keyring(keyring_path)
        config_digest = sha256_file(config_path)
    except (OSError, ValueError) as exc:
        return ProductionAuthorizationResult(False, (str(exc),))
    blockers = authorization.verify(
        keyring=keyring,
        strategy_id=strategy_id,
        symbol=symbol,
        source_commit=current_source_commit(),
        config_sha256=config_digest,
        target_stage=target_stage,
    )
    if blockers:
        return ProductionAuthorizationResult(False, blockers, authorization)
    registry_path = Path(os.environ.get("VNEDGE_STRATEGY_REGISTRY", str(DEFAULT_REGISTRY)))
    registry_blockers = strategy_authority_blockers(
        strategy_id,
        purpose="live",
        registry_path=registry_path,
    )
    if registry_blockers:
        return ProductionAuthorizationResult(False, registry_blockers, authorization)
    try:
        registry = build_registry_snapshot(registry_path)
        runtime_config = yaml.safe_load(Path(config_path).read_text())
        scanner_config = (
            runtime_config.get("scanner_authority")
            if isinstance(runtime_config, dict)
            else None
        )
        scanner_config = scanner_config if isinstance(scanner_config, dict) else {}
        registry_entry = registry["strategies"][strategy_id]
    except (OSError, KeyError, TypeError, ValueError, yaml.YAMLError) as exc:
        return ProductionAuthorizationResult(
            False,
            (f"canonical strategy registry validation failed: {exc}",),
            authorization,
        )
    registry_contract = (registry_entry.get("route_cost_contract") or {}).get("contract_id")
    if scanner_config.get("scanner_id") != strategy_id:
        return ProductionAuthorizationResult(
            False,
            ("runtime scanner differs from canonical production authorization",),
            authorization,
        )
    if scanner_config.get("cost_contract") != registry_contract:
        return ProductionAuthorizationResult(
            False,
            ("runtime cost contract differs from canonical strategy registry",),
            authorization,
        )
    expected_previous = {
        "live_small": "shadow",
        "live_full": "live_small",
        "emergency_reduce_only": "live_small",
    }.get(target_stage)
    if expected_previous is None:
        return ProductionAuthorizationResult(
            False,
            (f"unsupported production stage transition target: {target_stage}",),
            authorization,
        )
    if authorization.current_stage != expected_previous:
        return ProductionAuthorizationResult(
            False,
            (
                (
                    "invalid operating-mode transition: "
                    f"{authorization.current_stage} -> {target_stage}; "
                    f"expected {expected_previous} -> {target_stage}"
                ),
            ),
            authorization,
        )
    claim_blockers = _validate_ladder_claims(
        authorization.claims,
        target_stage=target_stage,
    )
    if claim_blockers:
        return ProductionAuthorizationResult(False, claim_blockers, authorization)
    if consume:
        store = NonceReplayStore(
            os.environ.get(
                "VNEDGE_GOVERNANCE_NONCE_DB",
                "data/governance_nonces.sqlite3",
            )
        )
        if not store.consume(
            nonce=authorization.nonce,
            proof_hash=hashlib.sha256(authorization.signing_payload()).hexdigest(),
            purpose=f"stage:{target_stage}:{strategy_id}:{symbol}",
        ):
            return ProductionAuthorizationResult(
                False,
                ("signed stage authorization nonce was already consumed",),
                authorization,
            )
    return ProductionAuthorizationResult(True, (), authorization)


def _valid_sha256(value: object) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(char in _SHA256_HEX for char in text)


def _validate_ladder_claims(
    claims: dict,
    *,
    target_stage: str,
) -> tuple[str, ...]:
    """Require an explicit, hash-linked lower-rung evidence chain.

    ``approved=true`` alone is deliberately insufficient.  Each promotion
    authorization must name the immutable paper and shadow proofs it relies
    on, and live-full additionally requires the completed live-small proof.
    """

    failures: list[str] = []
    for flag in ("untouched_validated", "paper_validated", "shadow_validated"):
        if claims.get(flag) is not True:
            failures.append(f"stage authorization claim {flag}=true is required")
    for field in ("untouched_proof_hash", "paper_proof_hash", "shadow_proof_hash"):
        if not _valid_sha256(claims.get(field)):
            failures.append(f"stage authorization requires SHA-256 {field}")
    if target_stage == "live_full":
        if claims.get("live_small_validated") is not True:
            failures.append(
                "stage authorization claim live_small_validated=true is required"
            )
        if not _valid_sha256(claims.get("live_small_proof_hash")):
            failures.append(
                "stage authorization requires SHA-256 live_small_proof_hash"
            )
    return tuple(failures)
