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
    assert payload["research_modules"]["governance"]["status"] == (
        "SIGNED_PROOF_MIGRATION_PENDING"
    )
    assert payload["research_modules"]["governance"]["paper_manifest_signature_required"] is False
    assert payload["research_modules"]["delta_execution_safety"][
        "dashboard_runtime_connected"
    ] is False
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
    (event_root / "BTCUSD.jsonl.gz.manifest.json").write_text(
        json.dumps({"records": 123})
    )
    trigger = tmp_path / "trigger.json"
    trigger.write_text(
        json.dumps(
            {
                "counts": {"events": 123, "evaluations": 7, "selected": 1},
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
        )
    )

    payload = client.get("/event-research-infrastructure?token=token").json()

    assert payload["recorder"]["status"] == "RECORDED"
    assert payload["recorder"]["recorded_events_from_manifests"] == 123
    assert payload["event_trigger"]["status"] == "OBSERVING"
    assert payload["event_trigger"]["counts"]["selected"] == 1
    assert payload["absorption"]["status"] == "ACTIVE"
    assert payload["absorption"]["observations"] == 1
    assert payload["absorption"]["liquidation_strength_applied_to_signal"] is False
    assert payload["replay"]["status"] == "VALIDATED_RESULT"
    assert payload["replay"]["deterministic_hash"] == "a" * 64
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False


def test_event_research_endpoint_reports_fresh_active_recorder(tmp_path: Path) -> None:
    event_root = tmp_path / "events"
    event_root.mkdir()
    (event_root / ".active.jsonl.gz.partial").write_bytes(b"live")
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
    client = TestClient(
        create_app(provider, token="token", delta_event_root=event_root)
    )

    recorder = client.get("/event-research-infrastructure?token=token").json()[
        "recorder"
    ]
    assert recorder["status"] == "RECORDING"
    assert recorder["live_events"] == 456
    assert recorder["active_session"] == "rec_live"
    assert recorder["partial_files"] == 1


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
    htf = client.get("/event-research-infrastructure?token=token").json()[
        "research_modules"
    ]["htf_structure_break"]
    assert htf["status"] == "SELECTION_REJECTED"
    assert htf["scanner_funnel"]["target_below_5x_cost"] == 8
    assert htf["untouched"] == {"status": "sealed", "loaded": False}
    assert htf["can_trade"] is False
