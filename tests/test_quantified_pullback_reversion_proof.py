from __future__ import annotations

from datetime import UTC, datetime

from vnedge.agent_gateway.jobs import DONE_STATUS, read_job, update_job
from vnedge.research.quantified_pullback_reversion_proof import (
    PORT_ID,
    QUANTIFIED_PULLBACK_REVERSION_PROOF_ID,
    QuantifiedPullbackProofConfig,
    build_quantified_pullback_reversion_proof_payload,
    load_quantified_pullback_reversion_proof_payload,
    publish_quantified_pullback_reversion_proof,
)
from vnedge.strategy.quantified_fee_wall_sniper import QUANTIFIED_FEE_WALL_SNIPER_ID


def _config() -> QuantifiedPullbackProofConfig:
    return QuantifiedPullbackProofConfig(
        exchanges=("delta_india",),
        bases=("ETH",),
        timeframes=("5m",),
    )


def test_quantified_pullback_proof_seeds_research_jobs_once(tmp_path):
    jobs_dir = tmp_path / "jobs"

    first = build_quantified_pullback_reversion_proof_payload(
        jobs_dir=jobs_dir,
        config=_config(),
        seed_jobs=True,
        now=datetime(2026, 7, 31, tzinfo=UTC),
    )
    second = build_quantified_pullback_reversion_proof_payload(
        jobs_dir=jobs_dir,
        config=_config(),
        seed_jobs=True,
        now=datetime(2026, 7, 31, tzinfo=UTC),
    )

    assert first["proof_id"] == QUANTIFIED_PULLBACK_REVERSION_PROOF_ID
    assert first["summary"]["total_cells"] == 1
    assert first["summary"]["jobs_created"] == 1
    assert second["summary"]["jobs_created"] == 0
    assert second["summary"]["pending_cells"] == 1
    assert first["can_trade"] is False
    assert first["can_promote"] is False

    job_path = next(jobs_dir.glob("agj_*.json"))
    job = read_job(jobs_dir, job_path.stem)
    assert job is not None
    request = job["request"]
    assert request["strategy_id"] == QUANTIFIED_FEE_WALL_SNIPER_ID
    assert request["exchange"] == "delta_india"
    assert request["symbol"] == "ETH/USD:USD"
    assert request["initial_capital_usd"] == 100.0
    assert request["live_orders_enabled"] is False
    assert request["parameters"]["port_id"] == PORT_ID
    assert request["parameters"]["paper_margin_usd"] == 100.0
    assert request["parameters"]["paper_leverage"] == 25.0
    assert request["parameters"]["params"]["enabled_setups"] == ["pullback"]


def test_quantified_pullback_proof_classifies_completed_fee_wall_breaker(tmp_path):
    jobs_dir = tmp_path / "jobs"
    build_quantified_pullback_reversion_proof_payload(
        jobs_dir=jobs_dir,
        config=_config(),
        seed_jobs=True,
    )
    job_id = next(jobs_dir.glob("agj_*.json")).stem

    update_job(
        jobs_dir,
        job_id,
        status=DONE_STATUS,
        result={
            "metrics": {
                "num_trades": 20,
                "net_profit_usd": 130.0,
                "profit_factor": 1.62,
                "win_rate_pct": 55.0,
            },
            "artifact_path": "research/live_research/agent_jobs/example.json",
            "can_trade": False,
            "can_promote": False,
            "live_orders_enabled": False,
        },
    )

    payload = build_quantified_pullback_reversion_proof_payload(
        jobs_dir=jobs_dir,
        config=_config(),
        seed_jobs=False,
    )

    row = payload["rows"][0]
    assert row["status"] == DONE_STATUS
    assert row["avg_net_bps"] == 26.0
    assert row["verdict"] == "PROMOTABLE_PROOF_REQUIRES_UNTOUCHED_JUDGMENT"
    assert payload["summary"]["promotable_proof_candidates"] == 1
    assert payload["summary"]["best_lane"]["symbol"] == "ETH/USD:USD"
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False


def test_quantified_pullback_proof_publish_and_load_round_trip(tmp_path):
    payload = build_quantified_pullback_reversion_proof_payload(
        jobs_dir=tmp_path / "jobs",
        config=_config(),
        seed_jobs=False,
    )
    out = tmp_path / "proof.json"
    feed = tmp_path / "proof.jsonl"

    publish_quantified_pullback_reversion_proof(payload, out=out, feed=feed)
    loaded = load_quantified_pullback_reversion_proof_payload(out)

    assert loaded["proof_id"] == QUANTIFIED_PULLBACK_REVERSION_PROOF_ID
    assert loaded["summary"]["total_cells"] == 1
    assert loaded["summary"]["matched_jobs"] == 0
    assert loaded["summary"]["jobs_reused"] == 0
    assert feed.exists()
