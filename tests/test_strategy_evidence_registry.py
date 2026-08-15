import hashlib
import json

import yaml

from vnedge.research.strategy_evidence_registry import (
    attach_verified_lane_evidence,
    build_registry_snapshot,
    relabel_uncalibrated_edge_fields,
    route_cost_contract,
    strategy_authority_blockers,
)


def _registry(
    tmp_path,
    *,
    digest: str,
    required_fields=None,
    status: str = "shadow",
    paper: bool = False,
    global_paper: bool = False,
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    artifact = tmp_path / "evidence.json"
    artifact.write_text(json.dumps({"proof": {"passed": True}, "can_trade": False}))
    costs = tmp_path / "costs.yaml"
    costs.write_text(
        yaml.safe_dump(
            {
                "schema_version": "vnedge.cost_contracts.v1",
                "version": "1.0",
                "cost_contracts": {
                    "route": {
                        "description": "taker/taker test",
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
                        "taker_fee_bps": 5.9,
                        "slippage_bps_per_leg": 1.5,
                        "includes_gst": True,
                        "calibration_status": "test",
                    }
                },
            }
        )
    )
    path = tmp_path / "registry.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "vnedge.strategy_registry.v1",
                "version": "1.0",
                "registry_id": "test",
                "cost_contracts_file": str(costs),
                "strategies": {
                    "scanner": {
                        "id": "scanner",
                        "display": True,
                        "type": "swing",
                        "status": status,
                        "hypothesis_class": "test",
                        "cost_contract": "route",
                        "evidence": {
                            "primary": "evidence.json",
                            "hash": "sha256:" + digest,
                            "required_fields": required_fields or ["proof.passed"],
                            "metrics": {
                                "passed": {"path": "proof.passed", "expected": True}
                            },
                            "required_n": 10,
                            "single_window": True,
                        },
                        "authority": {
                            "observation": status == "shadow",
                            "paper": paper,
                            "live": False,
                        },
                    }
                },
                "global_authority": {
                    "paper_trials_enabled": global_paper,
                    "can_trade": False,
                    "can_promote": False,
                    "live_orders_enabled": False,
                    "order_route": "absent",
                },
            }
        )
    )
    return path, artifact


def test_registry_only_publishes_hash_verified_lanes(tmp_path):
    artifact_payload = json.dumps({"proof": {"passed": True}, "can_trade": False})
    digest = hashlib.sha256(artifact_payload.encode()).hexdigest()
    registry, artifact = _registry(tmp_path, digest=digest)

    rows, withheld, snapshot = attach_verified_lane_evidence(
        [{"strategy_id": "scanner", "symbol": "ETHUSD"}], registry_path=registry
    )
    assert not withheld
    assert rows[0]["evidence_sha256"] == digest
    assert rows[0]["trade_horizon"] == "swing"
    assert rows[0]["evidence_metrics"] == {"passed": True}
    assert rows[0]["evidence_warnings"] == ["SINGLE_WINDOW"]
    assert rows[0]["can_trade"] is False
    assert snapshot["all_displayed_evidence_verified"] is True

    artifact.write_text('{"proof":{"passed":false}}')
    rows, withheld, snapshot = attach_verified_lane_evidence(
        [{"strategy_id": "scanner", "symbol": "ETHUSD"}], registry_path=registry
    )
    assert rows == []
    assert "sha256_mismatch" in withheld[0]["reason"]
    assert snapshot["all_displayed_evidence_verified"] is False


def test_registry_rejects_missing_required_evidence_field(tmp_path):
    artifact_payload = json.dumps({"proof": {"passed": True}, "can_trade": False})
    digest = hashlib.sha256(artifact_payload.encode()).hexdigest()
    registry, _ = _registry(tmp_path, digest=digest, required_fields=["proof.missing"])
    result = build_registry_snapshot(registry)
    assert result["strategies"]["scanner"]["evidence"]["verified"] is False
    assert "missing_required_fields" in result["invalid_evidence"][0]["reason"]


def test_cost_contract_is_explicit_and_structural_field_is_relabelled(tmp_path):
    artifact_payload = json.dumps({"proof": {"passed": True}, "can_trade": False})
    digest = hashlib.sha256(artifact_payload.encode()).hexdigest()
    registry, _ = _registry(tmp_path, digest=digest)
    contract = route_cost_contract("route", registry_path=registry)
    assert contract.round_trip_cost_bps == 14.8
    assert contract.entry_liquidity == "taker"
    value = relabel_uncalibrated_edge_fields(
        {"expected_edge_bps": 20, "expected_net_edge_bps_long": 5, "average_net_bps": -2}
    )
    assert value == {
        "heuristic_projected_gross_bps": 20,
        "structural_net_headroom_bps_long": 5,
        "average_net_bps": -2,
    }


def test_disabled_status_is_absolute_and_paper_requires_registry_authority(tmp_path):
    payload = json.dumps({"proof": {"passed": True}, "can_trade": False})
    digest = hashlib.sha256(payload.encode()).hexdigest()
    disabled, _ = _registry(
        tmp_path / "disabled",
        digest=digest,
        status="disabled",
        paper=True,
        global_paper=True,
    )
    assert "absolute" in " ".join(
        strategy_authority_blockers("scanner", purpose="paper", registry_path=disabled)
    )

    paper_root = tmp_path / "paper"
    paper_root.mkdir()
    paper, _ = _registry(
        paper_root,
        digest=digest,
        status="paper",
        paper=True,
        global_paper=True,
    )
    assert strategy_authority_blockers(
        "scanner", purpose="paper", registry_path=paper
    ) == ()
