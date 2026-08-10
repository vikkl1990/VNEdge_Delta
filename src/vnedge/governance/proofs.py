"""Canonical, hash-linked runtime proofs for promotion boundaries.

SHA-256 provides deterministic integrity and chain linkage. It is not an
identity signature; signer authentication is a separate migration step and
must not be implied by these digests.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vnedge.governance.promotion_policy import PromotionPolicy

GENESIS_HASH = "0" * 64
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
REQUIRED_PAPER_ARTIFACTS = frozenset(
    {"strategy_config", "source_commit", "dataset_window", "cost_model"}
)


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=str,
    )


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_sha256(value: str, field_name: str) -> None:
    if not _SHA256.fullmatch(value):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 hex digest")


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ArtifactDigest(_FrozenModel):
    name: str
    sha256: str
    locator: str = ""

    @model_validator(mode="after")
    def validate_digest(self) -> ArtifactDigest:
        if not self.name:
            raise ValueError("artifact name is required")
        _require_sha256(self.sha256, f"artifact {self.name}")
        return self

    @classmethod
    def from_json(cls, name: str, value: Any, *, locator: str = "") -> ArtifactDigest:
        return cls(name=name, sha256=sha256_json(value), locator=locator)

    @classmethod
    def from_text(cls, name: str, value: str, *, locator: str = "") -> ArtifactDigest:
        return cls(name=name, sha256=sha256_text(value), locator=locator)

    @classmethod
    def from_file(cls, name: str, path: str | Path) -> ArtifactDigest:
        resolved = Path(path)
        return cls(name=name, sha256=sha256_file(resolved), locator=str(resolved))


class GateCheck(_FrozenModel):
    gate: str
    passed: bool
    actual: float | int | str | bool | None = None
    required: float | int | str | bool | None = None
    reason: str


class PromotionMetrics(_FrozenModel):
    completed_trades: int = Field(ge=0)
    average_net_bps: float = Field(allow_inf_nan=False)
    profit_factor: float = Field(ge=0, allow_inf_nan=False)


class VerifiableProof(_FrozenModel):
    proof_schema_version: Literal["1"] = "1"
    proof_type: str
    issuer: str
    strategy_id: str
    symbol: str
    policy_version: str
    policy_sha256: str
    created_at: datetime
    expires_at: datetime
    previous_proof_hash: str = GENESIS_HASH
    artifacts: tuple[ArtifactDigest, ...]
    claims: dict[str, Any]
    proof_hash: str

    @model_validator(mode="after")
    def validate_integrity(self) -> VerifiableProof:
        for field_name in ("proof_type", "issuer", "strategy_id", "symbol", "policy_version"):
            if not getattr(self, field_name):
                raise ValueError(f"{field_name} is required")
        _require_sha256(self.policy_sha256, "policy_sha256")
        _require_sha256(self.previous_proof_hash, "previous_proof_hash")
        _require_sha256(self.proof_hash, "proof_hash")
        if self.created_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("proof timestamps must be timezone-aware")
        if self.expires_at <= self.created_at:
            raise ValueError("proof expiry must be after creation")
        names = [artifact.name for artifact in self.artifacts]
        if len(names) != len(set(names)):
            raise ValueError("proof artifact names must be unique")
        if self.proof_hash != self.compute_hash():
            raise ValueError("proof hash does not match its canonical payload")
        return self

    def canonical_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"proof_hash"})

    def compute_hash(self) -> str:
        return sha256_json(self.canonical_payload())

    def verify(
        self,
        *,
        policy: PromotionPolicy,
        now: datetime | None = None,
        strategy_id: str | None = None,
        symbol: str | None = None,
    ) -> tuple[str, ...]:
        failures: list[str] = []
        current = (now or datetime.now(UTC)).astimezone(UTC)
        if self.compute_hash() != self.proof_hash:
            failures.append("canonical proof hash mismatch")
        if self.policy_version != policy.policy_version:
            failures.append(
                f"policy version mismatch: {self.policy_version} != {policy.policy_version}"
            )
        if self.policy_sha256 != policy.sha256:
            failures.append("promotion policy digest mismatch")
        if current >= self.expires_at.astimezone(UTC):
            failures.append("proof expired")
        if current < self.created_at.astimezone(UTC):
            failures.append("proof creation timestamp is in the future")
        if strategy_id is not None and self.strategy_id != strategy_id:
            failures.append(f"strategy mismatch: {self.strategy_id} != {strategy_id}")
        if symbol is not None and self.symbol != symbol:
            failures.append(f"symbol mismatch: {self.symbol} != {symbol}")
        return tuple(failures)

    @classmethod
    def issue(
        cls,
        *,
        proof_type: str,
        issuer: str,
        strategy_id: str,
        symbol: str,
        policy: PromotionPolicy,
        artifacts: tuple[ArtifactDigest, ...],
        claims: dict[str, Any],
        previous_proof_hash: str = GENESIS_HASH,
        created_at: datetime | None = None,
        ttl: timedelta = timedelta(days=30),
    ) -> VerifiableProof:
        created = (created_at or datetime.now(UTC)).astimezone(UTC)
        values: dict[str, Any] = {
            "proof_schema_version": "1",
            "proof_type": proof_type,
            "issuer": issuer,
            "strategy_id": strategy_id,
            "symbol": symbol,
            "policy_version": policy.policy_version,
            "policy_sha256": policy.sha256,
            "created_at": created,
            "expires_at": created + ttl,
            "previous_proof_hash": previous_proof_hash,
            "artifacts": artifacts,
            "claims": claims,
        }
        values["proof_hash"] = sha256_json(
            cls.model_construct(**values, proof_hash=GENESIS_HASH).model_dump(
                mode="json", exclude={"proof_hash"}
            )
        )
        return cls.model_validate(values)


class PaperEligibilityProof(VerifiableProof):
    proof_type: Literal["paper_eligibility"] = "paper_eligibility"
    metrics: PromotionMetrics
    gate_checks: tuple[GateCheck, ...]
    selection_proof_hash: str
    untouched_proof_hash: str
    human_approval_proof_hash: str
    eligible: Literal[True] = True

    @model_validator(mode="after")
    def validate_eligibility(self) -> PaperEligibilityProof:
        for name in (
            "selection_proof_hash",
            "untouched_proof_hash",
            "human_approval_proof_hash",
        ):
            _require_sha256(getattr(self, name), name)
        if not self.gate_checks or not all(check.passed for check in self.gate_checks):
            raise ValueError("paper eligibility proof requires every gate to pass")
        if self.previous_proof_hash != self.human_approval_proof_hash:
            raise ValueError("paper eligibility must link to the human approval proof")
        return self

    @classmethod
    def issue_from_evaluator(
        cls,
        *,
        issuer: str,
        strategy_id: str,
        symbol: str,
        policy: PromotionPolicy,
        artifacts: tuple[ArtifactDigest, ...],
        metrics: PromotionMetrics,
        gate_checks: tuple[GateCheck, ...],
        selection_proof_hash: str,
        untouched_proof_hash: str,
        human_approval_proof_hash: str,
        created_at: datetime,
        ttl: timedelta,
    ) -> PaperEligibilityProof:
        values: dict[str, Any] = {
            "proof_schema_version": "1",
            "proof_type": "paper_eligibility",
            "issuer": issuer,
            "strategy_id": strategy_id,
            "symbol": symbol,
            "policy_version": policy.policy_version,
            "policy_sha256": policy.sha256,
            "created_at": created_at,
            "expires_at": created_at + ttl,
            "previous_proof_hash": human_approval_proof_hash,
            "artifacts": artifacts,
            "claims": {"eligible": True},
            "metrics": metrics,
            "gate_checks": gate_checks,
            "selection_proof_hash": selection_proof_hash,
            "untouched_proof_hash": untouched_proof_hash,
            "human_approval_proof_hash": human_approval_proof_hash,
            "eligible": True,
        }
        constructed = cls.model_construct(**values, proof_hash=GENESIS_HASH)
        values["proof_hash"] = sha256_json(constructed.model_dump(mode="json", exclude={"proof_hash"}))
        return cls.model_validate(values)


class PaperEligibilityDecision(_FrozenModel):
    passed: bool
    checks: tuple[GateCheck, ...]
    blockers: tuple[str, ...]
    proof: PaperEligibilityProof | None = None


class PromotionPolicyEvaluator:
    def __init__(self, policy: PromotionPolicy, *, issuer: str) -> None:
        if not issuer:
            raise ValueError("promotion proof issuer is required")
        self.policy = policy
        self.issuer = issuer

    def evaluate_paper_eligibility(
        self,
        *,
        selection_proof: VerifiableProof,
        untouched_proof: VerifiableProof,
        human_approval_proof: VerifiableProof,
        metrics: PromotionMetrics,
        artifacts: tuple[ArtifactDigest, ...],
        now: datetime | None = None,
        ttl: timedelta = timedelta(days=30),
    ) -> PaperEligibilityDecision:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        prior = (selection_proof, untouched_proof, human_approval_proof)
        expected_types = ("selection_evidence", "untouched_evidence", "human_approval")
        blockers: list[str] = []
        for proof, expected_type in zip(prior, expected_types, strict=True):
            if proof.proof_type != expected_type:
                blockers.append(f"expected {expected_type} proof, got {proof.proof_type}")
            blockers.extend(proof.verify(policy=self.policy, now=current))
        strategy_ids = {proof.strategy_id for proof in prior}
        symbols = {proof.symbol for proof in prior}
        if len(strategy_ids) != 1:
            blockers.append("proof chain strategy identities do not match")
        if len(symbols) != 1:
            blockers.append("proof chain symbols do not match")
        if untouched_proof.previous_proof_hash != selection_proof.proof_hash:
            blockers.append("untouched proof does not link to selection proof")
        if human_approval_proof.previous_proof_hash != untouched_proof.proof_hash:
            blockers.append("human approval proof does not link to untouched proof")
        if selection_proof.claims.get("passed") is not True:
            blockers.append("selection evidence did not pass")
        if untouched_proof.claims.get("passed") is not True:
            blockers.append("untouched evidence did not pass")
        if human_approval_proof.claims.get("approved") is not True:
            blockers.append("human approval proof is not approved")
        artifact_names = {artifact.name for artifact in artifacts}
        missing_artifacts = sorted(REQUIRED_PAPER_ARTIFACTS - artifact_names)
        if missing_artifacts:
            blockers.append(
                "paper eligibility is missing required artifact digests: "
                + ", ".join(missing_artifacts)
            )
        artifact_map = {artifact.name: artifact.sha256 for artifact in artifacts}
        for proof in prior:
            proof_artifact_map = {
                artifact.name: artifact.sha256 for artifact in proof.artifacts
            }
            for artifact_name in sorted(REQUIRED_PAPER_ARTIFACTS):
                if proof_artifact_map.get(artifact_name) != artifact_map.get(artifact_name):
                    blockers.append(
                        f"{proof.proof_type} artifact digest mismatch: {artifact_name}"
                    )

        requirements = self.policy.governance
        checks = (
            GateCheck(
                gate="minimum_trades",
                passed=metrics.completed_trades >= requirements.minimum_trades,
                actual=metrics.completed_trades,
                required=requirements.minimum_trades,
                reason="completed trades must clear governance minimum",
            ),
            GateCheck(
                gate="minimum_average_net_bps",
                passed=metrics.average_net_bps >= requirements.minimum_average_net_bps,
                actual=metrics.average_net_bps,
                required=requirements.minimum_average_net_bps,
                reason="after-cost average net bps must clear governance minimum",
            ),
            GateCheck(
                gate="minimum_profit_factor",
                passed=metrics.profit_factor >= requirements.minimum_profit_factor,
                actual=metrics.profit_factor,
                required=requirements.minimum_profit_factor,
                reason="profit factor must clear governance minimum",
            ),
        )
        blockers.extend(check.reason for check in checks if not check.passed)
        if blockers:
            return PaperEligibilityDecision(
                passed=False,
                checks=checks,
                blockers=tuple(blockers),
            )
        proof = PaperEligibilityProof.issue_from_evaluator(
            issuer=self.issuer,
            strategy_id=selection_proof.strategy_id,
            symbol=selection_proof.symbol,
            policy=self.policy,
            artifacts=artifacts,
            metrics=metrics,
            gate_checks=checks,
            selection_proof_hash=selection_proof.proof_hash,
            untouched_proof_hash=untouched_proof.proof_hash,
            human_approval_proof_hash=human_approval_proof.proof_hash,
            created_at=current,
            ttl=ttl,
        )
        return PaperEligibilityDecision(
            passed=True,
            checks=checks,
            blockers=(),
            proof=proof,
        )
