"""Dashboard truth surface for the research-only Delta event stack."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from vnedge.dashboard.app import SnapshotProvider, create_app
from vnedge.dashboard.scanner_live import build_scanner_snapshot


def test_event_research_endpoint_distinguishes_installed_from_running(tmp_path: Path) -> None:
    provider = SnapshotProvider()
    provider.publish({"mode": "research scanner observation", "can_trade": False})
    client = TestClient(
        create_app(
            provider,
            token="token",
            delta_event_root=tmp_path / "events",
            event_trigger_telemetry_path=tmp_path / "trigger.json",
            absorption_dashboard_path=tmp_path / "absorption.json",
            event_replay_dir=tmp_path / "replay",
        )
    )

    assert client.get("/event-research-infrastructure").status_code == 401
    payload = client.get("/event-research-infrastructure?token=token").json()

    assert payload["recorder"]["status"] == "WAITING_FOR_TAPE"
    assert payload["event_trigger"]["status"] == "IMPLEMENTED_NOT_RUNNING"
    assert payload["absorption"]["status"] == "AWAITING_EVENT_TAPE"
    assert payload["replay"]["status"] == "WAITING_FOR_TAPE"
    assert payload["research_modules"]["governance"]["status"] == "SIGNED_ENVELOPES_ENFORCED"
    assert payload["research_modules"]["governance"]["paper_manifest_signature_required"] is True
    assert payload["research_modules"]["governance"]["single_use_nonce_required"] is True
    assert (
        payload["research_modules"]["delta_execution_safety"]["dashboard_runtime_connected"]
        is False
    )
    assert payload["research_modules"]["tv_rule_adapter"]["implementation"] == "available"
    assert payload["research_modules"]["tv_rule_adapter"]["can_trade"] is False
    assert payload["safety"] == {
        "research_only": True,
        "can_trade": False,
        "can_promote": False,
        "order_route": "absent",
        "live_state_shared": False,
    }


def test_event_research_endpoint_reads_runtime_artifacts_without_promoting(
    tmp_path: Path,
) -> None:
    event_root = tmp_path / "events"
    event_root.mkdir()
    (event_root / "BTCUSD.jsonl.gz").write_bytes(b"")
    (event_root / "BTCUSD.jsonl.gz.manifest.json").write_text(json.dumps({"records": 123}))
    trigger = tmp_path / "trigger.json"
    trigger.write_text(
        json.dumps(
            {
                "counts": {"events": 123, "evaluations": 7, "selected": 1},
                "scanner_telemetry": {
                    "event_absorption_confirmed_reversal_v3_shadow": {
                        "counts": {"setups": 5, "confirmed": 2}
                    }
                },
                "feed_delay": {"p95_us": 20_000},
                "receive_to_decision": {"p95_us": 4_000},
                "research_only": True,
                "can_trade": False,
            }
        )
    )
    absorption = tmp_path / "absorption.json"
    absorption.write_text(
        json.dumps(
            {
                "status": "active",
                "symbol": "BTCUSD",
                "strength_gauge": 72.5,
                "timeline": [{"price": 100.0}],
                "liquidation_strength_applied_to_signal": False,
            }
        )
    )
    replay_dir = tmp_path / "replay"
    replay_dir.mkdir()
    (replay_dir / "run.result.json").write_text(
        json.dumps(
            {
                "events_processed": 123,
                "candidates_emitted": 1,
                "deterministic_hash": "a" * 64,
                "validation": {"passed": True, "sequence_gaps": 0},
                "summary_metrics": {"event_net_expectancy_bps": 5.0},
                "can_trade": False,
                "can_promote": False,
            }
        )
    )
    provider = SnapshotProvider()
    provider.publish({"mode": "research"})
    client = TestClient(
        create_app(
            provider,
            token="token",
            delta_event_root=event_root,
            event_trigger_telemetry_path=trigger,
            absorption_dashboard_path=absorption,
            event_replay_dir=replay_dir,
            replay_determinism_proof_path=tmp_path / "missing-proof.json",
        )
    )

    payload = client.get("/event-research-infrastructure?token=token").json()

    assert payload["recorder"]["status"] == "RECORDED"
    assert payload["recorder"]["recorded_events_from_manifests"] == 123
    assert payload["event_trigger"]["status"] == "OBSERVING"
    assert payload["event_trigger"]["counts"]["selected"] == 1
    assert payload["event_trigger"]["scanner_telemetry"][
        "event_absorption_confirmed_reversal_v3_shadow"
    ]["counts"] == {"setups": 5, "confirmed": 2}
    assert payload["absorption"]["status"] == "ACTIVE"
    assert payload["absorption"]["observations"] == 1
    assert payload["absorption"]["liquidation_strength_applied_to_signal"] is False
    assert payload["replay"]["status"] == "VALIDATED_RESULT"
    assert payload["replay"]["deterministic_hash"] == "a" * 64
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False


def test_indicator_calibration_is_read_only_and_embedded_in_delta_home(tmp_path: Path) -> None:
    calibration = tmp_path / "indicator.json"
    calibration.write_text(
        json.dumps(
            {
                "verdict": "NO_CALIBRATED_EDGE",
                "source": {"trades": 19_521},
                "top_score_band": {"average_net_bps": -8.99, "profit_factor": 0.32},
                "deciles": [{"decile": 10, "historical_tail": {"trades": 268}}],
                "policy": {"can_trade": True, "used_for_signal": True},
                "can_trade": True,
                "can_promote": True,
            }
        )
    )
    delta = tmp_path / "delta.json"
    delta.write_text(json.dumps({"rows": [], "delta_scalper": {}}))
    provider = SnapshotProvider()
    provider.publish({"mode": "research"})
    client = TestClient(
        create_app(
            provider,
            token="token",
            delta_scalper_path=delta,
            indicator_score_calibration_path=calibration,
        )
    )

    assert client.get("/indicator-score-calibration").status_code == 401
    payload = client.get("/indicator-score-calibration?token=token").json()
    assert payload["source"]["trades"] == 19_521
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False
    assert payload["policy"]["used_for_signal"] is False
    embedded = client.get("/delta-scalper?token=token").json()["panels"][
        "indicator_score_calibration"
    ]
    assert embedded["verdict"] == "NO_CALIBRATED_EDGE"
    assert embedded["can_trade"] is False


def test_event_research_endpoint_reports_fresh_active_recorder(tmp_path: Path) -> None:
    event_root = tmp_path / "events"
    event_root.mkdir()
    (event_root / ".trades_rec_live_0001.jsonl.gz.partial").write_bytes(b"live")
    (event_root / "_recorder_status.json").write_text(
        json.dumps(
            {
                "schema_version": "vnedge.delta_event_recorder_status.v1",
                "state": "RECORDING",
                "session_id": "rec_live",
                "updated_at": datetime.now(UTC).isoformat(),
                "stats_seconds": 30,
                "events": 456,
                "counts": {"trades": 123, "ob_updates": 333},
                "can_trade": False,
                "can_promote": False,
            }
        )
    )
    provider = SnapshotProvider()
    provider.publish({"mode": "research"})
    client = TestClient(create_app(provider, token="token", delta_event_root=event_root))

    recorder = client.get("/event-research-infrastructure?token=token").json()["recorder"]
    assert recorder["status"] == "RECORDING"
    assert recorder["live_events"] == 456
    assert recorder["active_session"] == "rec_live"
    assert recorder["partial_files"] == 1
    assert recorder["active_partial_files"] == 1
    assert recorder["orphan_partial_files"] == 0
    assert recorder["storage"]["disk_free_bytes"] > 0


def test_event_research_surfaces_failed_auction_readiness_without_authority(
    tmp_path: Path,
) -> None:
    readiness = tmp_path / "readiness.json"
    readiness.write_text(
        json.dumps(
            {
                "contract_id": "failed_auction_response_v1",
                "data_ready": False,
                "scanner_implementation_authorized": False,
                "selection_authorized": False,
                "blockers": ["total_events:100<5000000"],
                "coverage": {"total_events": 100, "requested_days": 1.0},
                "tree_verification": {"passed": True},
                "semantic_validation": {"passed": True},
                "can_trade": False,
                "can_promote": False,
            }
        )
    )
    provider = SnapshotProvider()
    provider.publish({"mode": "research"})
    client = TestClient(
        create_app(
            provider,
            token="token",
            failed_auction_readiness_path=readiness,
            replay_determinism_proof_path=tmp_path / "missing-proof.json",
            event_continuity_path=tmp_path / "missing-continuity.json",
        )
    )

    payload = client.get("/event-research-infrastructure?token=token").json()
    proof = payload["failed_auction_readiness"]

    assert proof["events"] == 100
    assert proof["target_events"] == 5_000_000
    assert proof["blocker_count"] == 1
    assert proof["data_ready"] is False
    assert proof["scanner_implementation_authorized"] is False
    assert proof["can_trade"] is False
    assert proof["stages"][2]["id"] == "determinism"
    assert proof["stages"][2]["state"] == "BLOCKED"
    assert proof["stages"][3]["state"] == "NOT_AUTHORIZED"


def test_continuity_qualification_replaces_legacy_coverage_without_authority(
    tmp_path: Path,
) -> None:
    readiness = tmp_path / "legacy-readiness.json"
    readiness.write_text(
        json.dumps(
            {
                "contract_id": "failed_auction_response_v1",
                "data_ready": True,
                "scanner_implementation_authorized": True,
                "selection_authorized": True,
                "coverage": {"total_events": 9_999_999, "requested_days": 99.0},
                "blockers": [],
                "can_trade": True,
                "can_promote": True,
            }
        )
    )
    continuity = tmp_path / "continuity.json"
    continuity.write_text(
        json.dumps(
            {
                "contract_id": "failed_auction_response_v1",
                "qualification": {
                    "data_ready": False,
                    "blockers": ["minimum_total_events:2074<5000000"],
                    "qualified_events": 2_074,
                    "target_events": 5_000_000,
                    "qualified_days": 0.02,
                    "target_days": 14.0,
                    "events_remaining": 4_997_926,
                    "days_remaining": 13.98,
                    "estimated_ready_at": None,
                    "estimate_status": "AWAITING_STABLE_OPEN_EPOCH",
                    "semantic_validation": {"passed": True},
                },
                "epochs": {
                    "count": 3,
                    "latest": {"provisional": True, "end_reason": "open"},
                    "longest": {"duration_days": 0.02},
                    "last_reset": {"reason": "__ob_sequence_gap__"},
                },
                "audit": {
                    "verified_shards": 12,
                    "failed_shards": [],
                    "active_partial_files": 2,
                    "orphan_partial_files": 0,
                },
                "scanner_implementation_authorized": True,
                "selection_authorized": True,
                "can_trade": True,
                "can_promote": True,
            }
        )
    )
    provider = SnapshotProvider()
    provider.publish({"mode": "research"})
    client = TestClient(
        create_app(
            provider,
            token="token",
            failed_auction_readiness_path=readiness,
            event_continuity_path=continuity,
        )
    )

    proof = client.get("/event-research-infrastructure?token=token").json()[
        "failed_auction_readiness"
    ]

    assert proof["data_ready"] is False
    assert proof["blockers"] == ["minimum_total_events:2074<5000000"]
    assert proof["continuity"]["qualified_events"] == 2_074
    assert proof["continuity"]["active_partial_files"] == 2
    assert proof["tree_passed"] is True
    assert proof["semantic_passed"] is True
    assert proof["scanner_implementation_authorized"] is False
    assert proof["selection_authorized"] is False
    assert proof["stages"][0]["value"] == 2_074
    assert proof["can_trade"] is False
    assert proof["can_promote"] is False


def test_dashboard_surfaces_verified_feature_replay_without_granting_authority(
    tmp_path: Path,
) -> None:
    determinism = tmp_path / "determinism.json"
    config = {
        "symbols": ["BTCUSD", "ETHUSD"],
        "channels": ["ob_updates", "trades"],
        "start_ts_us": 1,
        "end_ts_us": 2,
        "speed_multiplier": 0.0,
        "enable_feature_engine": True,
        "enable_scanner": False,
        "journal_mode": "none",
        "sealed_holdout": False,
        "random_seed": 42,
        "signal_to_fill_latency_ms": 100,
        "code_version": "tree-hash",
        "fail_on_integrity_error": True,
    }
    determinism.write_text(
        json.dumps(
            {
                "passed": True,
                "hash_match": True,
                "events_match": True,
                "feature_snapshots_match": True,
                "validation_match": True,
                "first_hash": "a" * 64,
                "second_hash": "a" * 64,
                "first_validation_hash": "b" * 64,
                "second_validation_hash": "b" * 64,
                "first_events": 2_074,
                "second_events": 2_074,
                "first_feature_snapshots": 2_070,
                "second_feature_snapshots": 2_070,
                "code_version": "tree-hash",
                "config": config,
                "can_trade": True,
                "can_promote": True,
            }
        )
    )
    provider = SnapshotProvider()
    provider.publish({"mode": "research"})
    client = TestClient(
        create_app(
            provider,
            token="token",
            replay_determinism_proof_path=determinism,
        )
    )

    payload = client.get("/event-research-infrastructure?token=token").json()
    readiness = payload["failed_auction_readiness"]
    proof = payload["replay"]["determinism_proof"]

    assert readiness["replay_determinism_passed"] is True
    assert readiness["replay_feature_snapshots"] == 2_070
    assert readiness["stages"][2]["state"] == "PASSED"
    assert proof["passed"] is True
    assert proof["can_trade"] is False
    assert proof["can_promote"] is False


def test_scanner_state_can_carry_same_event_research_truth() -> None:
    infrastructure = {
        "schema_version": "vnedge.event_research_infrastructure.v1",
        "recorder": {"status": "WAITING_FOR_TAPE"},
        "safety": {"can_trade": False, "order_route": "absent"},
    }

    snapshot = build_scanner_snapshot(
        {"scanner_id": "empty", "symbols": {}},
        research_infrastructure=infrastructure,
    )

    assert snapshot["research_infrastructure"] == infrastructure
    assert snapshot["can_trade"] is False
    assert snapshot["orders_sent"] == 0


def test_dashboard_surfaces_response_atlas_without_granting_authority(tmp_path: Path) -> None:
    atlas = tmp_path / "atlas.json"
    atlas.write_text(
        json.dumps(
            {
                "generated_at": "2026-08-13T00:00:00+00:00",
                    "source": {
                        "raw_detections": 700,
                        "independent_episodes": 500,
                        "symbols": ["BTCUSD", "ETHUSD"],
                    },
                    "contract": {"route_cost_contract": {"round_trip_cost_bps": 14.8}},
                    "control_qualification": {"passed": True, "matched_pairs": 500},
                "coverage": {"evaluated_entries": 1_200},
                "diagnosis": {
                    "verdict": "NO_AFTER_COST_DIRECTIONAL_CELL_FOUND",
                    "best_cell": {"average_net_bps": -8.0},
                },
                "opportunity_atlas": [{"symbol": "ETHUSD", "average_mfe_bps": 30.0}],
                "direction_entry_exit_matrix": [
                    {"symbol": "ETHUSD", "hypothesis": "reversal"}
                ],
                "best_discovery_cells": [],
                "deterministic_result_hash": "a" * 64,
                "can_trade": False,
                "can_promote": False,
            }
        )
    )
    provider = SnapshotProvider()
    provider.publish({"mode": "research"})
    client = TestClient(
        create_app(provider, token="token", event_response_atlas_path=atlas)
    )

    response = client.get("/event-research-infrastructure?token=token").json()[
        "response_atlas"
    ]

    assert response["status"] == "NO_AFTER_COST_DIRECTIONAL_CELL_FOUND"
    assert response["source"]["independent_episodes"] == 500
    assert response["control_qualification"]["passed"] is True
    assert response["diagnosis"]["best_cell"]["average_net_bps"] == -8.0
    assert response["scanner_implementation_authorized"] is False
    assert response["can_trade"] is False
    assert response["can_promote"] is False


def test_dashboard_surfaces_post_event_direction_failure_without_authority(
    tmp_path: Path,
) -> None:
    study = tmp_path / "direction.json"
    study.write_text(
        json.dumps(
            {
                "generated_at": "2026-08-14T00:00:00+00:00",
                    "source": {
                        "raw_detections": 10_000,
                        "independent_episodes": 8_907,
                        "symbols": ["BTCUSD", "ETHUSD"],
                    },
                    "contract": {"route_cost_contract": {"round_trip_cost_bps": 14.8}},
                    "control_qualification": {"passed": True, "matched_pairs": 8_907},
                "coverage": {"captured_response_states": 39_307},
                "diagnosis": {
                    "verdict": "NO_STABLE_AFTER_COST_DIRECTION_RULE_FOUND",
                    "supported_development_cells": 0,
                    "best_comparison": {
                        "symbol": "BTCUSD",
                        "selection": {"average_net_bps": -14.32},
                        "validation": {"average_net_bps": -14.26},
                    },
                },
                "best_comparisons": [],
                "deterministic_result_hash": "b" * 64,
                "can_trade": False,
                "can_promote": False,
            }
        )
    )
    provider = SnapshotProvider()
    provider.publish({"mode": "research"})
    client = TestClient(
        create_app(provider, token="token", post_absorption_direction_path=study)
    )

    response = client.get("/event-research-infrastructure?token=token").json()[
        "post_absorption_direction"
    ]

    assert response["status"] == "NO_STABLE_AFTER_COST_DIRECTION_RULE_FOUND"
    assert response["source"]["independent_episodes"] == 8_907
    assert response["control_qualification"]["passed"] is True
    assert response["diagnosis"]["best_comparison"]["validation"][
        "average_net_bps"
    ] == -14.26
    assert response["scanner_implementation_authorized"] is False
    assert response["paper_authorized"] is False
    assert response["can_trade"] is False
    assert response["can_promote"] is False


def test_dashboard_surfaces_rejected_htf_selection_without_opening_tail(tmp_path: Path) -> None:
    artifact = tmp_path / "htf.json"
    artifact.write_text(
        json.dumps(
            {
                "selection": {
                    "metrics": {
                        "trades": 0,
                        "net_bps": 0,
                        "profit_factor": 0,
                        "scanner_funnel": {"event_bos": 10, "target_below_5x_cost": 8},
                    },
                    "gate": {"passed": False},
                },
                "untouched": {"status": "sealed", "loaded": False},
                "can_trade": False,
            }
        )
    )
    provider = SnapshotProvider()
    provider.publish({"mode": "research"})
    client = TestClient(create_app(provider, token="token", htf_structure_path=artifact))
    htf = client.get("/event-research-infrastructure?token=token").json()["research_modules"][
        "htf_structure_break"
    ]
    assert htf["status"] == "SELECTION_REJECTED"
    assert htf["scanner_funnel"]["target_below_5x_cost"] == 8
    assert htf["untouched"] == {"status": "sealed", "loaded": False}
    assert htf["can_trade"] is False


def test_dashboard_surfaces_htf_v2_economics_with_tail_sealed(tmp_path: Path) -> None:
    artifact = tmp_path / "htf_v2.json"
    artifact.write_text(
        json.dumps(
            {
                "selection": {
                    "metrics": {
                        "trades": 100,
                        "net_bps": -1049.96,
                        "average_gross_bps": 4.51,
                        "average_total_cost_bps": 15.01,
                        "average_net_bps": -10.50,
                        "profit_factor": 0.843,
                        "funding_used": True,
                        "markets": {"BTCUSD": {"trades": 27}, "ETHUSD": {"trades": 73}},
                    },
                    "gate": {"passed": False},
                },
                "untouched": {"status": "sealed", "loaded": False},
                "can_trade": False,
                "can_promote": False,
            }
        )
    )
    provider = SnapshotProvider()
    provider.publish({"mode": "research"})
    client = TestClient(create_app(provider, token="token", htf_structure_v2_path=artifact))

    htf = client.get("/event-research-infrastructure?token=token").json()["research_modules"][
        "htf_structure_break_v2"
    ]

    assert htf["status"] == "SELECTION_REJECTED"
    assert htf["selection_trades"] == 100
    assert htf["average_gross_bps"] == 4.51
    assert htf["average_cost_bps"] == 15.01
    assert htf["average_net_bps"] == -10.50
    assert htf["untouched"] == {"status": "sealed", "loaded": False}
    assert htf["can_trade"] is False
    assert htf["can_promote"] is False
