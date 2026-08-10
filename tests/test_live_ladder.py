"""Live promotion ladder — explainable, no skipping, no automatic live."""

from datetime import UTC, datetime, timedelta

from vnedge.config.settings import LIVE_CONFIRMATION_PHRASE, Settings, TradingMode
from vnedge.governance.promotion_policy import DEFAULT_PROMOTION_POLICY
from vnedge.governance.proofs import (
    ArtifactDigest,
    PaperEligibilityProof,
    PromotionMetrics,
    PromotionPolicyEvaluator,
    VerifiableProof,
)
from vnedge.runtime.live_ladder import (
    LiveLadderEvidence,
    LiveLadderStage,
    evaluate_live_ladder,
    settings_live_gates_ready,
)


def _paper_proof(*, expired: bool = False) -> PaperEligibilityProof:
    now = datetime.now(UTC)
    created = now - timedelta(days=2) if expired else now
    chain_ttl = timedelta(days=30)
    artifacts = (
        ArtifactDigest.from_json("strategy_config", {"id": "candidate_v1"}),
        ArtifactDigest.from_text("source_commit", "0123456789abcdef"),
        ArtifactDigest.from_json("dataset_window", {"end": "sealed"}),
        ArtifactDigest.from_json("cost_model", {"round_trip_bps": 12.0}),
    )
    common = {
        "issuer": "test",
        "strategy_id": "candidate_v1",
        "symbol": "BTCUSD",
        "policy": DEFAULT_PROMOTION_POLICY,
        "artifacts": artifacts,
        "created_at": created,
        "ttl": chain_ttl,
    }
    selection = VerifiableProof.issue(
        proof_type="selection_evidence", claims={"passed": True}, **common
    )
    untouched = VerifiableProof.issue(
        proof_type="untouched_evidence",
        claims={"passed": True},
        previous_proof_hash=selection.proof_hash,
        **common,
    )
    human = VerifiableProof.issue(
        proof_type="human_approval",
        claims={"approved": True},
        previous_proof_hash=untouched.proof_hash,
        **common,
    )
    decision = PromotionPolicyEvaluator(
        DEFAULT_PROMOTION_POLICY, issuer="test-evaluator"
    ).evaluate_paper_eligibility(
        selection_proof=selection,
        untouched_proof=untouched,
        human_approval_proof=human,
        metrics=PromotionMetrics(
            completed_trades=30,
            average_net_bps=30.0,
            profit_factor=1.6,
        ),
        artifacts=artifacts,
        now=created,
        ttl=timedelta(days=1) if expired else timedelta(days=30),
    )
    assert decision.proof is not None
    return decision.proof


def test_backtest_to_paper_requires_locked_untouched_human_evidence():
    decision = evaluate_live_ladder(
        LiveLadderEvidence(
            current_stage=LiveLadderStage.BACKTEST,
            target_stage=LiveLadderStage.PAPER,
            strategy_id="candidate_v1",
            symbol="BTCUSD",
            paper_eligibility_proof=_paper_proof(),
        )
    )

    assert decision.allowed
    assert decision.blockers == ()
    assert decision.policy_version == DEFAULT_PROMOTION_POLICY.policy_version


def test_backtest_to_paper_lists_all_missing_evidence():
    decision = evaluate_live_ladder(
        LiveLadderEvidence(
            current_stage=LiveLadderStage.BACKTEST,
            target_stage=LiveLadderStage.PAPER,
        )
    )

    assert not decision.allowed
    assert decision.blockers == ("paper requires a valid PaperEligibilityProof",)


def test_backtest_to_paper_rejects_expired_proof():
    decision = evaluate_live_ladder(
        LiveLadderEvidence(
            current_stage=LiveLadderStage.BACKTEST,
            target_stage=LiveLadderStage.PAPER,
            strategy_id="candidate_v1",
            symbol="BTCUSD",
            paper_eligibility_proof=_paper_proof(expired=True),
        )
    )

    assert not decision.allowed
    assert "proof expired" in decision.blockers


def test_backtest_to_paper_rejects_proof_for_another_strategy_or_market():
    decision = evaluate_live_ladder(
        LiveLadderEvidence(
            current_stage=LiveLadderStage.BACKTEST,
            target_stage=LiveLadderStage.PAPER,
            strategy_id="different_candidate",
            symbol="ETHUSD",
            paper_eligibility_proof=_paper_proof(),
        )
    )

    assert not decision.allowed
    assert "strategy mismatch: candidate_v1 != different_candidate" in decision.blockers
    assert "symbol mismatch: BTCUSD != ETHUSD" in decision.blockers


def test_ladder_cannot_skip_from_paper_to_live_small():
    decision = evaluate_live_ladder(
        LiveLadderEvidence(
            current_stage=LiveLadderStage.PAPER,
            target_stage=LiveLadderStage.LIVE_SMALL,
            human_approved=True,
            pre_live_checklist_cleared=True,
            three_live_gates_ready=True,
            shadow_days=30,
            shadow_trades=50,
            shadow_net_usd=25,
            shadow_profit_factor=1.4,
        )
    )

    assert not decision.allowed
    assert any("must advance exactly one rung" in b for b in decision.blockers)


def test_paper_to_shadow_requires_positive_mature_paper_trial():
    decision = evaluate_live_ladder(
        LiveLadderEvidence(
            current_stage=LiveLadderStage.PAPER,
            target_stage=LiveLadderStage.SHADOW,
            human_approved=True,
            paper_days=14,
            paper_trades=10,
            paper_net_usd=1.0,
            paper_max_drawdown_pct=6.0,
        )
    )

    assert decision.allowed


def test_shadow_to_live_small_requires_live_safety_and_shadow_edge():
    decision = evaluate_live_ladder(
        LiveLadderEvidence(
            current_stage=LiveLadderStage.SHADOW,
            target_stage=LiveLadderStage.LIVE_SMALL,
            human_approved=True,
            pre_live_checklist_cleared=True,
            three_live_gates_ready=True,
            reconciliation_clean=True,
            kill_switch_clear=True,
            journal_writable=True,
            shadow_days=7,
            shadow_trades=10,
            shadow_net_usd=5.0,
            shadow_profit_factor=1.05,
            shadow_max_drawdown_pct=6.0,
        )
    )

    assert decision.allowed


def test_shadow_to_live_small_blocks_without_gates_or_profit_factor():
    decision = evaluate_live_ladder(
        LiveLadderEvidence(
            current_stage=LiveLadderStage.SHADOW,
            target_stage=LiveLadderStage.LIVE_SMALL,
            human_approved=True,
            pre_live_checklist_cleared=True,
            shadow_days=7,
            shadow_trades=10,
            shadow_net_usd=5.0,
        )
    )

    assert not decision.allowed
    assert "three live gates are not open" in decision.blockers
    assert "shadow trial profit factor is missing" in decision.blockers


def test_live_small_to_live_full_requires_positive_live_small_observation():
    decision = evaluate_live_ladder(
        LiveLadderEvidence(
            current_stage=LiveLadderStage.LIVE_SMALL,
            target_stage=LiveLadderStage.LIVE_FULL,
            human_approved=True,
            pre_live_checklist_cleared=True,
            three_live_gates_ready=True,
            live_small_days=7,
            live_small_trades=5,
            live_small_net_usd=2.5,
            live_small_max_drawdown_pct=3.0,
        )
    )

    assert decision.allowed


def test_live_full_is_terminal():
    decision = evaluate_live_ladder(
        LiveLadderEvidence(
            current_stage=LiveLadderStage.LIVE_FULL,
            target_stage=LiveLadderStage.LIVE_FULL,
        )
    )

    assert not decision.allowed
    assert "live_full is terminal; no higher promotion rung exists" in decision.blockers


def test_settings_helper_uses_existing_three_gate_contract():
    live = Settings(
        _env_file=None,
        trading_mode=TradingMode.LIVE_SMALL,
        live_trading_enabled=True,
        confirm_live_trading=LIVE_CONFIRMATION_PHRASE,
    )
    shadow = Settings(
        _env_file=None,
        trading_mode=TradingMode.SHADOW,
        live_trading_enabled=True,
        confirm_live_trading=LIVE_CONFIRMATION_PHRASE,
    )

    assert settings_live_gates_ready(live)
    assert not settings_live_gates_ready(shadow)
