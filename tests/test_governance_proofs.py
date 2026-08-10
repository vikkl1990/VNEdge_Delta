from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from vnedge.governance.promotion_policy import DEFAULT_PROMOTION_POLICY
from vnedge.governance.proofs import (
    ArtifactDigest,
    PaperEligibilityProof,
    PromotionMetrics,
    PromotionPolicyEvaluator,
    VerifiableProof,
)


def required_artifacts() -> tuple[ArtifactDigest, ...]:
    return (
        ArtifactDigest.from_json("strategy_config", {"scanner": "candidate_v1"}),
        ArtifactDigest.from_text("source_commit", "0123456789abcdef"),
        ArtifactDigest.from_json(
            "dataset_window", {"start": "2025-01-01", "end": "2026-06-30"}
        ),
        ArtifactDigest.from_json("cost_model", {"round_trip_bps": 12.0}),
    )


def proof_chain(
    *,
    created_at: datetime,
    ttl: timedelta = timedelta(days=30),
) -> tuple[VerifiableProof, VerifiableProof, VerifiableProof]:
    policy = DEFAULT_PROMOTION_POLICY
    artifacts = required_artifacts()
    common = {
        "issuer": "vnedge-test",
        "strategy_id": "candidate_v1",
        "symbol": "BTCUSD",
        "policy": policy,
        "artifacts": artifacts,
        "created_at": created_at,
        "ttl": ttl,
    }
    selection = VerifiableProof.issue(
        proof_type="selection_evidence",
        claims={"passed": True},
        **common,
    )
    untouched = VerifiableProof.issue(
        proof_type="untouched_evidence",
        claims={"passed": True},
        previous_proof_hash=selection.proof_hash,
        **common,
    )
    human = VerifiableProof.issue(
        proof_type="human_approval",
        claims={"approved": True, "approver": "test-operator"},
        previous_proof_hash=untouched.proof_hash,
        **common,
    )
    return selection, untouched, human


def test_policy_is_versioned_frozen_and_has_stable_digest():
    policy = DEFAULT_PROMOTION_POLICY

    assert policy.policy_version == "v1.2.0"
    assert len(policy.sha256) == 64
    assert policy.sha256 == DEFAULT_PROMOTION_POLICY.sha256
    with pytest.raises(ValidationError):
        policy.governance.minimum_profit_factor = 1.0


def test_evaluator_issues_round_trip_verifiable_paper_proof():
    now = datetime(2026, 8, 8, 12, tzinfo=UTC)
    selection, untouched, human = proof_chain(created_at=now)
    decision = PromotionPolicyEvaluator(
        DEFAULT_PROMOTION_POLICY, issuer="promotion-policy-evaluator"
    ).evaluate_paper_eligibility(
        selection_proof=selection,
        untouched_proof=untouched,
        human_approval_proof=human,
        metrics=PromotionMetrics(
            completed_trades=30,
            average_net_bps=30.0,
            profit_factor=1.6,
        ),
        artifacts=required_artifacts(),
        now=now,
    )

    assert decision.passed
    assert decision.blockers == ()
    assert decision.proof is not None
    assert decision.proof.previous_proof_hash == human.proof_hash
    assert decision.proof.verify(policy=DEFAULT_PROMOTION_POLICY, now=now) == ()
    restored = PaperEligibilityProof.model_validate_json(decision.proof.model_dump_json())
    assert restored == decision.proof


def test_evaluator_rejects_failed_economics_and_missing_artifacts():
    now = datetime(2026, 8, 8, 12, tzinfo=UTC)
    selection, untouched, human = proof_chain(created_at=now)
    decision = PromotionPolicyEvaluator(
        DEFAULT_PROMOTION_POLICY, issuer="promotion-policy-evaluator"
    ).evaluate_paper_eligibility(
        selection_proof=selection,
        untouched_proof=untouched,
        human_approval_proof=human,
        metrics=PromotionMetrics(
            completed_trades=30,
            average_net_bps=24.0,
            profit_factor=1.4,
        ),
        artifacts=required_artifacts()[:2],
        now=now,
    )

    assert not decision.passed
    assert decision.proof is None
    assert any("required artifact digests" in blocker for blocker in decision.blockers)
    assert "after-cost average net bps must clear governance minimum" in decision.blockers
    assert "profit factor must clear governance minimum" in decision.blockers


def test_evaluator_rejects_broken_proof_chain():
    now = datetime(2026, 8, 8, 12, tzinfo=UTC)
    selection, untouched, _human = proof_chain(created_at=now)
    unlinked_human = VerifiableProof.issue(
        proof_type="human_approval",
        issuer="vnedge-test",
        strategy_id="candidate_v1",
        symbol="BTCUSD",
        policy=DEFAULT_PROMOTION_POLICY,
        artifacts=required_artifacts(),
        claims={"approved": True},
        created_at=now,
    )

    decision = PromotionPolicyEvaluator(
        DEFAULT_PROMOTION_POLICY, issuer="promotion-policy-evaluator"
    ).evaluate_paper_eligibility(
        selection_proof=selection,
        untouched_proof=untouched,
        human_approval_proof=unlinked_human,
        metrics=PromotionMetrics(
            completed_trades=30,
            average_net_bps=30.0,
            profit_factor=1.6,
        ),
        artifacts=required_artifacts(),
        now=now,
    )

    assert not decision.passed
    assert "human approval proof does not link to untouched proof" in decision.blockers


def test_evaluator_rejects_artifact_substitution_after_approval():
    now = datetime(2026, 8, 8, 12, tzinfo=UTC)
    selection, untouched, human = proof_chain(created_at=now)
    substituted = list(required_artifacts())
    substituted[0] = ArtifactDigest.from_json(
        "strategy_config", {"scanner": "substituted_after_approval"}
    )

    decision = PromotionPolicyEvaluator(
        DEFAULT_PROMOTION_POLICY, issuer="promotion-policy-evaluator"
    ).evaluate_paper_eligibility(
        selection_proof=selection,
        untouched_proof=untouched,
        human_approval_proof=human,
        metrics=PromotionMetrics(
            completed_trades=30,
            average_net_bps=30.0,
            profit_factor=1.6,
        ),
        artifacts=tuple(substituted),
        now=now,
    )

    assert not decision.passed
    assert any("strategy_config" in blocker for blocker in decision.blockers)


def test_proof_tampering_is_rejected_during_deserialization():
    now = datetime(2026, 8, 8, 12, tzinfo=UTC)
    selection, _, _ = proof_chain(created_at=now)
    payload = selection.model_dump(mode="json")
    payload["claims"] = {"passed": False}

    with pytest.raises(ValidationError, match="proof hash"):
        VerifiableProof.model_validate(payload)


def test_proof_rejects_policy_drift_and_expiry():
    created = datetime(2026, 8, 1, 12, tzinfo=UTC)
    selection, _, _ = proof_chain(created_at=created, ttl=timedelta(days=1))
    changed_policy = DEFAULT_PROMOTION_POLICY.model_copy(
        update={"policy_version": "v1.2.1"}
    )

    failures = selection.verify(
        policy=changed_policy,
        now=created + timedelta(days=2),
    )

    assert "proof expired" in failures
    assert any("policy version mismatch" in failure for failure in failures)
    assert "promotion policy digest mismatch" in failures
