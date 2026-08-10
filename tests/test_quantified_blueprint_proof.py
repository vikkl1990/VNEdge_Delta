from __future__ import annotations

import json
from datetime import UTC, datetime

from vnedge.agent_gateway.jobs import DONE_STATUS, read_job, update_job
from vnedge.research.quantified_blueprint_proof import (
    QUANTIFIED_BLUEPRINT_PROOF_ID,
    BlueprintProofProfile,
    QuantifiedBlueprintProofConfig,
    build_quantified_blueprint_proof_payload,
    load_quantified_blueprint_proof_payload,
    publish_quantified_blueprint_proof,
)
from vnedge.strategy.quant_signal_pack import QuantSignalPack
from vnedge.strategy.quantified_fee_wall_sniper import QUANTIFIED_FEE_WALL_SNIPER_ID


def _config() -> QuantifiedBlueprintProofConfig:
    return QuantifiedBlueprintProofConfig(
        profiles=(
            BlueprintProofProfile(
                port_id="range_volatility_breakout_reversion_v1",
                strategy_id=QUANTIFIED_FEE_WALL_SNIPER_ID,
                adapter="fee_wall_sniper_breakout",
                setup_mode="breakout_only",
                exchanges=("delta_india",),
                bases=("ETH",),
                timeframes=("5m",),
                strategy_parameters={
                    "params": {"enabled_setups": ["breakout"]},
                    "min_expected_net_edge_bps": 25.0,
                },
            ),
            BlueprintProofProfile(
                port_id="indicator_pack_mtf_v1",
                strategy_id=QuantSignalPack.strategy_id,
                adapter="quant_signal_pack_mtf_atoms",
                setup_mode="indicator_confluence",
                exchanges=("binanceusdm",),
                bases=("BTC",),
                timeframes=("1h",),
                strategy_parameters={
                    "allowed_families": ["structure_break", "confluence"],
                    "min_score": 5.0,
                },
            ),
        )
    )


def test_blueprint_proof_seeds_all_profile_jobs_once(tmp_path):
    jobs_dir = tmp_path / "jobs"

    first = build_quantified_blueprint_proof_payload(
        jobs_dir=jobs_dir,
        config=_config(),
        seed_jobs=True,
        now=datetime(2026, 8, 1, tzinfo=UTC),
    )
    second = build_quantified_blueprint_proof_payload(
        jobs_dir=jobs_dir,
        config=_config(),
        seed_jobs=True,
        now=datetime(2026, 8, 1, tzinfo=UTC),
    )

    assert first["proof_id"] == QUANTIFIED_BLUEPRINT_PROOF_ID
    assert first["summary"]["ports"] == 2
    assert first["summary"]["total_cells"] == 2
    assert first["summary"]["jobs_created"] == 2
    assert second["summary"]["jobs_created"] == 0
    assert second["summary"]["pending_cells"] == 2
    assert first["can_trade"] is False
    assert first["can_promote"] is False

    jobs = [read_job(jobs_dir, path.stem) for path in jobs_dir.glob("agj_*.json")]
    requests = [job["request"] for job in jobs if job is not None]
    ports = {request["parameters"]["port_id"] for request in requests}
    assert ports == {
        "range_volatility_breakout_reversion_v1",
        "indicator_pack_mtf_v1",
    }
    breakout = next(
        request for request in requests
        if request["parameters"]["port_id"] == "range_volatility_breakout_reversion_v1"
    )
    assert breakout["strategy_id"] == QUANTIFIED_FEE_WALL_SNIPER_ID
    assert breakout["parameters"]["params"]["enabled_setups"] == ["breakout"]
    assert breakout["live_orders_enabled"] is False


def test_blueprint_proof_classifies_completed_promotable_candidate(tmp_path):
    jobs_dir = tmp_path / "jobs"
    build_quantified_blueprint_proof_payload(
        jobs_dir=jobs_dir,
        config=_config(),
        seed_jobs=True,
    )
    job_id = next(
        path.stem
        for path in jobs_dir.glob("agj_*.json")
        if (read_job(jobs_dir, path.stem) or {})["request"]["parameters"]["port_id"]
        == "range_volatility_breakout_reversion_v1"
    )
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
            "can_trade": False,
            "can_promote": False,
            "live_orders_enabled": False,
        },
    )

    payload = build_quantified_blueprint_proof_payload(
        jobs_dir=jobs_dir,
        config=_config(),
        seed_jobs=False,
    )

    row = next(
        row for row in payload["rows"]
        if row["port_id"] == "range_volatility_breakout_reversion_v1"
    )
    assert row["avg_net_bps"] == 26.0
    assert row["verdict"] == "PROMOTABLE_PROOF_REQUIRES_UNTOUCHED_JUDGMENT"
    assert payload["summary"]["promotable_proof_candidates"] == 1
    assert payload["summary"]["best_lane"]["port_id"] == "range_volatility_breakout_reversion_v1"
    assert payload["ports"][0]["promotable_proof_candidates"] == 1
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False


def test_blueprint_proof_publish_and_load_round_trip(tmp_path):
    payload = build_quantified_blueprint_proof_payload(
        jobs_dir=tmp_path / "jobs",
        config=_config(),
        seed_jobs=False,
    )
    out = tmp_path / "quantified_blueprint_proof_latest.json"
    feed = tmp_path / "quantified_blueprint_proof_feed.jsonl"

    publish_quantified_blueprint_proof(payload, out=out, feed=feed)
    loaded = load_quantified_blueprint_proof_payload(out)

    assert loaded["proof_id"] == QUANTIFIED_BLUEPRINT_PROOF_ID
    assert loaded["summary"]["ports"] == 2
    assert loaded["summary"]["total_cells"] == 2
    assert feed.exists()
    assert QUANTIFIED_BLUEPRINT_PROOF_ID in feed.read_text(encoding="utf-8")
