from __future__ import annotations

import json
from datetime import UTC, datetime

from vnedge.governance.crypto import GovernanceKeyring, GovernanceSigner
from vnedge.governance.promotion_policy import DEFAULT_PROMOTION_POLICY
from vnedge.governance.proofs import (
    ArtifactDigest,
    GateCheck,
    PaperEligibilityProof,
    PromotionMetrics,
)
from vnedge.governance.signed_envelope import (
    NonceReplayStore,
    SignedPaperEligibilityEnvelope,
    SignedStageAuthorization,
    load_governance_keyring,
)


def _paper_proof(issuer: str, now: datetime) -> PaperEligibilityProof:
    artifacts = (
        ArtifactDigest.from_json("strategy_config", {"threshold": 2}),
        ArtifactDigest.from_text("source_commit", "abc123"),
        ArtifactDigest.from_json("dataset_window", {"end": "sealed"}),
        ArtifactDigest.from_json("cost_model", {"round_trip_bps": 14.8}),
    )
    checks = (
        GateCheck(gate="minimum_trades", passed=True, actual=60, required=60, reason="ok"),
    )
    return PaperEligibilityProof.issue_from_evaluator(
        issuer=issuer,
        strategy_id="scanner_v1",
        symbol="ETHUSD",
        policy=DEFAULT_PROMOTION_POLICY,
        artifacts=artifacts,
        metrics=PromotionMetrics(completed_trades=60, average_net_bps=4, profit_factor=1.4),
        gate_checks=checks,
        selection_proof_hash="1" * 64,
        untouched_proof_hash="2" * 64,
        human_approval_proof_hash="3" * 64,
        created_at=now,
        ttl=__import__("datetime").timedelta(days=1),
    )


def test_signed_paper_envelope_and_nonce_are_fail_closed(tmp_path):
    now = datetime(2026, 8, 13, tzinfo=UTC)
    signer = GovernanceSigner.generate(issuer="production-governance")
    proof = _paper_proof(signer.issuer, now)
    envelope = SignedPaperEligibilityEnvelope.issue(proof, signer=signer)
    keyring = GovernanceKeyring((signer.trusted_record(),))

    assert envelope.verify(
        keyring=keyring,
        policy=DEFAULT_PROMOTION_POLICY,
        strategy_id="scanner_v1",
        symbol="ETHUSD",
        now=now,
    ) == ()
    assert envelope.verify(
        keyring=keyring,
        policy=DEFAULT_PROMOTION_POLICY,
        strategy_id="different",
        symbol="ETHUSD",
        now=now,
    )

    store = NonceReplayStore(tmp_path / "nonces.sqlite3")
    assert store.consume(nonce=envelope.nonce, proof_hash=proof.proof_hash, purpose="paper")
    assert not store.consume(nonce=envelope.nonce, proof_hash=proof.proof_hash, purpose="paper")


def test_stage_authorization_binds_commit_config_and_target():
    now = datetime(2026, 8, 13, tzinfo=UTC)
    signer = GovernanceSigner.generate(issuer="production-governance")
    keyring = GovernanceKeyring((signer.trusted_record(),))
    authorization = SignedStageAuthorization.issue(
        signer=signer,
        strategy_id="scanner_v1",
        symbol="ETHUSD",
        source_commit="abc123",
        config_sha256="4" * 64,
        current_stage="shadow",
        target_stage="live_small",
        claims={"approved": True},
        created_at=now,
    )

    assert authorization.verify(
        keyring=keyring,
        strategy_id="scanner_v1",
        symbol="ETHUSD",
        source_commit="abc123",
        config_sha256="4" * 64,
        target_stage="live_small",
        now=now,
    ) == ()
    assert authorization.verify(
        keyring=keyring,
        strategy_id="scanner_v1",
        symbol="ETHUSD",
        source_commit="wrong",
        config_sha256="4" * 64,
        target_stage="live_small",
        now=now,
    )


def test_public_keyring_file_loader(tmp_path):
    signer = GovernanceSigner.generate(issuer="production-governance")
    path = tmp_path / "keyring.json"
    path.write_text(json.dumps({"keys": [{
        "issuer": signer.issuer,
        "issuer_pubkey": signer.issuer_pubkey,
    }]}))
    assert load_governance_keyring(path).key_ids == (signer.key_id,)
