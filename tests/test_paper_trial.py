"""Governed paper trial — manifest validation, live funding growth, wiring."""

import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from vnedge.data.schemas import normalize_candles, normalize_funding
from vnedge.governance.crypto import GovernanceSigner
from vnedge.governance.promotion_policy import DEFAULT_PROMOTION_POLICY
from vnedge.governance.proofs import (
    ArtifactDigest,
    GateCheck,
    PaperEligibilityProof,
    PromotionMetrics,
    sha256_json,
    sha256_text,
)
from vnedge.governance.signed_envelope import SignedPaperEligibilityEnvelope
from vnedge.runtime.paper_trial import (
    LiveFundingMR,
    TrialManifest,
    append_trial_report,
    build_trial_session,
)

BASE = 1_750_000_000_000
HOUR = 3_600_000

MANIFEST = Path("research/paper_trials/funding_mr_btc_v1_20260703.yaml")


def manifest_dict(**overrides) -> dict:
    base = {
        "trial_id": "t1", "strategy": "funding_mean_reversion_v1",
        "exchange": "delta_india",
        "symbol": "BTC/USDT:USDT", "timeframe": "1h", "mode": "live_data_paper",
        "approved_by": "human", "strategy_params": {"extreme_pct": 0.85},
        "starting_equity": 500, "daily_loss_limit_usd": 10,
        "live_orders_enabled": False, "promotion_source_commit": "3b56d20",
        "cost_contract": "taker_full_14_8",
    }
    base.update(overrides)
    return base


def write_manifest(tmp_path, **overrides) -> Path:
    from datetime import UTC, datetime, timedelta

    import yaml

    path = tmp_path / "m.yaml"
    payload = manifest_dict(**overrides)
    evidence_payload = json.dumps({"proof": {"passed": True}, "can_trade": False})
    evidence_path = tmp_path / "strategy_evidence.json"
    evidence_path.write_text(evidence_payload)
    evidence_hash = hashlib.sha256(evidence_payload.encode()).hexdigest()
    costs_path = tmp_path / "cost_contracts.yaml"
    costs_path.write_text(yaml.safe_dump({
        "schema_version": "vnedge.cost_contracts.v1",
        "version": "1.0",
        "cost_contracts": {
            "taker_full_14_8": {
                "description": "test Delta taker contract",
                "route": "taker_taker",
                "entry_liquidity": "taker",
                "exit_liquidity": "taker",
                "base_entry_fee_bps": 5.0,
                "base_exit_fee_bps": 5.0,
                "gst_bps": 1.8,
                "slippage_bps": 3.0,
                "safety_buffer_bps": 0.0,
                "total_roundtrip_bps": 14.8,
                "maker_fee_bps": 2.36,
                "taker_fee_bps": 5.90,
                "slippage_bps_per_leg": 1.5,
                "includes_gst": True,
                "calibration_status": "test",
            }
        },
    }))
    registry_path = tmp_path / "strategy_registry.yaml"
    registry_path.write_text(yaml.safe_dump({
        "schema_version": "vnedge.strategy_registry.v1",
        "version": "1.0",
        "registry_id": "paper-test",
        "cost_contracts_file": str(costs_path),
        "strategies": {
            str(payload["strategy"]): {
                "id": str(payload["strategy"]),
                "display": False,
                "type": "swing",
                "status": "paper",
                "cost_contract": "taker_full_14_8",
                "evidence": {
                    "primary": str(evidence_path),
                    "hash": "sha256:" + evidence_hash,
                    "required_fields": ["proof.passed"],
                },
                "authority": {"observation": True, "paper": True, "live": False},
            }
        },
        "global_authority": {
            "paper_trials_enabled": True,
            "can_trade": False,
            "can_promote": False,
            "live_orders_enabled": False,
            "order_route": "absent",
        },
    }))
    payload["strategy_registry_path"] = registry_path.name
    cost_model = {
        "maker_fee_bps": 2.36,
        "taker_fee_bps": 5.90,
        "slippage_bps_per_leg": 1.5,
        "round_trip_bps": 14.8,
        "includes_gst": True,
        "cost_contract": "taker_full_14_8",
    }
    signer = GovernanceSigner.generate(issuer="test-governance")
    strategy_params = payload.get("strategy_params", {})
    commit = str(payload.get("promotion_source_commit", ""))
    artifacts = (
        ArtifactDigest(name="strategy_config", sha256=sha256_json(strategy_params)),
        ArtifactDigest(name="source_commit", sha256=sha256_text(commit)),
        ArtifactDigest(name="exchange", sha256=sha256_text(str(payload["exchange"]))),
        ArtifactDigest.from_json("dataset_window", {"sealed": True}),
        ArtifactDigest.from_json("cost_model", cost_model),
        ArtifactDigest(name="cost_contract", sha256=sha256_text("taker_full_14_8")),
        ArtifactDigest(
            name="strategy_registry",
            sha256=hashlib.sha256(registry_path.read_bytes()).hexdigest(),
        ),
        ArtifactDigest(name="strategy_evidence", sha256=evidence_hash),
    )
    proof = PaperEligibilityProof.issue_from_evaluator(
        issuer=signer.issuer,
        strategy_id=str(payload["strategy"]),
        symbol=str(payload["symbol"]),
        policy=DEFAULT_PROMOTION_POLICY,
        artifacts=artifacts,
        metrics=PromotionMetrics(
            completed_trades=60,
            average_net_bps=30,
            profit_factor=1.6,
        ),
        gate_checks=(
            GateCheck(
                gate="governance",
                passed=True,
                actual=True,
                required=True,
                reason="test proof",
            ),
        ),
        selection_proof_hash="1" * 64,
        untouched_proof_hash="2" * 64,
        human_approval_proof_hash="3" * 64,
        created_at=datetime.now(UTC),
        ttl=timedelta(days=1),
    )
    envelope = SignedPaperEligibilityEnvelope.issue(proof, signer=signer)
    proof_path = tmp_path / "paper_eligibility.json"
    proof_path.write_text(envelope.model_dump_json(indent=2))
    (tmp_path / "governance_keyring.json").write_text(json.dumps({"keys": [{
        "issuer": signer.issuer,
        "issuer_pubkey": signer.issuer_pubkey,
    }]}))
    payload["eligibility_proof_path"] = proof_path.name
    path.write_text(yaml.safe_dump(payload))
    return path


def test_legacy_committed_manifest_is_blocked_without_signed_proof():
    with pytest.raises(ValueError, match="eligibility_proof_path"):
        TrialManifest.load(MANIFEST)


def test_live_orders_manifest_refused(tmp_path):
    path = write_manifest(tmp_path, live_orders_enabled=True)
    with pytest.raises(ValueError, match="not a paper trial"):
        TrialManifest.load(path)


def test_manifest_strategy_tamper_is_refused(tmp_path):
    import yaml

    path = write_manifest(tmp_path)
    payload = yaml.safe_load(path.read_text())
    payload["strategy"] = "secret_profit_machine"
    path.write_text(yaml.safe_dump(payload))
    with pytest.raises(ValueError, match="strategy mismatch"):
        TrialManifest.load(path)


def test_manifest_cost_or_exchange_tamper_is_refused(tmp_path):
    import yaml

    path = write_manifest(tmp_path)
    payload = yaml.safe_load(path.read_text())
    payload["cost_contract"] = "not_a_real_contract"
    path.write_text(yaml.safe_dump(payload))
    with pytest.raises((ValueError, KeyError), match="cost contract"):
        TrialManifest.load(path)

    path = write_manifest(tmp_path)
    payload = yaml.safe_load(path.read_text())
    payload["exchange"] = "bybit"
    path.write_text(yaml.safe_dump(payload))
    with pytest.raises(ValueError, match="exchange is not bound"):
        TrialManifest.load(path)

    path = write_manifest(tmp_path)
    payload = yaml.safe_load(path.read_text())
    payload["exchange"] = ""
    path.write_text(yaml.safe_dump(payload))
    with pytest.raises(ValueError, match="explicit supported exchange"):
        TrialManifest.load(path)


def test_string_approval_is_not_an_authority_boundary(tmp_path):
    path = write_manifest(tmp_path, approved_by="ai")
    assert TrialManifest.load(path).strategy == "funding_mean_reversion_v1"


class FundingFeedStub:
    funding_rate = 0.0042
    quote = (99.99, 100.01)


def test_live_funding_mr_appends_feed_rate():
    seed_funding = normalize_funding(
        [{"timestamp": BASE + i * 8 * HOUR, "fundingRate": 0.0001} for i in range(30)]
    )
    candles = normalize_candles(
        [[BASE + i * HOUR, 100.0, 100.5, 99.5, 100.0, 5.0] for i in range(400)]
    )
    strategy = LiveFundingMR(
        seed_funding, FundingFeedStub(),
        funding_pct_window=48, z_window=24,
    )
    rows_before = len(strategy.funding)
    df = strategy.prepare(candles)
    assert len(strategy.funding) == rows_before + 1
    assert strategy.funding["funding_rate"].iloc[-1] == pytest.approx(0.0042)
    # newest bar carries the live rate via backward as-of merge
    assert df["funding_rate"].iloc[-1] == pytest.approx(0.0042)
    # calling again for the same newest bar must not append twice
    strategy.prepare(candles)
    assert len(strategy.funding) == rows_before + 1


def test_trial_session_wiring_and_report(tmp_path):
    import asyncio

    from tests.test_live_paper import FakeFeed  # scripted feed, no network

    manifest = TrialManifest.load(write_manifest(tmp_path))
    history = normalize_candles(
        [[BASE + i * HOUR, 100.0, 100.5, 99.5, 100.0, 5.0] for i in range(400)]
    )
    seed_funding = normalize_funding(
        [{"timestamp": BASE + i * 8 * HOUR, "fundingRate": 0.0001} for i in range(30)]
    )
    feed = FakeFeed([[BASE + 400 * HOUR, 100.0, 100.5, 99.5, 100.0, 5.0]])
    feed.funding_rate = 0.0001

    session = build_trial_session(
        manifest, feed, history, seed_funding, journal_dir=tmp_path
    )
    # the manifest's daily-loss number reached the actual gateway config
    assert session.config.risk.max_daily_loss_usd == 10.0
    assert session.config.starting_equity_usd == 500.0
    assert session.exchange.fill_model.taker_fee_bps == pytest.approx(5.9)
    assert session.exchange.fill_model.maker_fee_bps == pytest.approx(2.36)
    assert session.exchange.fill_model.slippage_bps == pytest.approx(1.5)

    report = asyncio.run(session.run(max_bars=1))
    assert report.bars_processed == 1

    reports_path = tmp_path / "t1.reports.jsonl"
    append_trial_report(manifest, report, reports_path)
    record = json.loads(reports_path.read_text().strip())
    assert record["trial_id"] == "t1"
    assert record["promotion_source_commit"] == "3b56d20"
    assert record["exchange"] == "delta_india"
    assert record["cost_contract"] == "taker_full_14_8"
    assert record["cost_model"]["round_trip_bps"] == 14.8
    assert record["report"]["mode"] == "paper_live"


def test_trial_session_refuses_wrong_symbol_account(tmp_path):
    """build_trial_session passes manifest expectations to restore_into."""
    from tests.test_live_paper import FakeFeed

    manifest = TrialManifest.load(write_manifest(tmp_path))
    history = normalize_candles(
        [[BASE + i * HOUR, 100.0, 100.5, 99.5, 100.0, 5.0] for i in range(400)]
    )
    seed_funding = normalize_funding(
        [{"timestamp": BASE + i * 8 * HOUR, "fundingRate": 0.0001} for i in range(30)]
    )
    feed = FakeFeed([[BASE + 400 * HOUR, 100.0, 100.5, 99.5, 100.0, 5.0]])
    # a moved/edited store holding a position in a DIFFERENT symbol
    (tmp_path / "t1.account.json").write_text(json.dumps({
        "trial_id": "t1", "saved_at": "2026-07-08T00:00:00+00:00",
        "starting_equity": 500.0, "balance_usd": 500.0,
        "positions": [
            {"symbol": "ETH/USDT:USDT", "quantity": 1.0, "entry_price": 100.0}
        ],
        "tracker": {}, "plan": None,
    }))
    with pytest.raises(ValueError, match="wrong-symbol"):
        build_trial_session(
            manifest, feed, history, seed_funding, journal_dir=tmp_path
        )


class SettledFundingFeedStub:
    """Feed exposing SETTLED prints — the venue-with-history case."""
    funding_rate = 0.0042          # predicted — must NOT enter the series
    quote = (99.99, 100.01)

    def __init__(self, events):
        self.funding_events = events


def test_live_funding_mr_prefers_settled_events_over_predicted():
    from vnedge.strategy.funding_mean_reversion import FundingMeanReversion

    seed = [{"timestamp": BASE + i * 8 * HOUR, "fundingRate": 0.0001} for i in range(28)]
    seed_funding = normalize_funding(seed)
    candles = normalize_candles(
        [[BASE + i * HOUR, 100.0, 100.5, 99.5, 100.0, 5.0] for i in range(400)]
    )
    # two settled prints newer than the seed tail (e.g. printed since lane build)
    new_prints = [
        (BASE + 28 * 8 * HOUR, 0.0007),
        (BASE + 29 * 8 * HOUR, 0.0009),
    ]
    strategy = LiveFundingMR(
        seed_funding, SettledFundingFeedStub(new_prints),
        funding_pct_window=48, z_window=24,
    )
    df = strategy.prepare(candles)

    # settled prints merged; the predicted 0.0042 must appear NOWHERE
    assert strategy.funding["funding_rate"].iloc[-1] == pytest.approx(0.0009)
    assert not (strategy.funding["funding_rate"] == pytest.approx(0.0042)).any()
    assert len(strategy.funding) == len(seed_funding) + 2

    # live construction == research construction, feature-for-feature
    research_series = normalize_funding(
        seed + [
            {"timestamp": ts, "fundingRate": fr} for ts, fr in new_prints
        ]
    )
    research = FundingMeanReversion(
        research_series, funding_pct_window=48, z_window=24
    )
    df_research = research.prepare(candles)
    pd.testing.assert_series_equal(df["funding_pct"], df_research["funding_pct"])

    # idempotent: same events re-merged change nothing
    n = len(strategy.funding)
    strategy.prepare(candles)
    assert len(strategy.funding) == n
