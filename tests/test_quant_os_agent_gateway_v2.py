"""Quant OS Agent Gateway v2 durable task/event/artifact ledger."""

import hashlib

import pytest
from fastapi.testclient import TestClient

from vnedge.agent_gateway.auth import AgentToken, AgentTokenStore
from vnedge.agent_gateway.task_registry import (
    TASK_COMPLETED,
    TASK_QUEUED,
    QuantOSAgentGateway,
)
from vnedge.dashboard.app import SnapshotProvider, create_app


def _dashboard_client(tmp_path, *, store=None):
    provider = SnapshotProvider()
    provider.publish({"mode": "shadow", "equity": 500.0})
    return TestClient(
        create_app(
            provider,
            token="dashboard-secret",
            agent_token_store=store,
            agent_audit_path=tmp_path / "audit.jsonl",
            agent_jobs_dir=tmp_path / "jobs",
            quant_os_agent_gateway_dir=tmp_path / "quant_os",
        )
    )


def _agent_store(scopes=("R", "W_RESEARCH")):
    return AgentTokenStore(
        [
            AgentToken.from_secret(
                name="arena-agent",
                token="agent-secret",
                scopes=scopes,
                markets=("delta_india:ETH/USD:USD",),
                lanes=("*",),
                rate_limit_per_min=1_000,
            )
        ]
    )


def _agent_headers():
    return {"Authorization": "Bearer agent-secret"}


def test_task_registry_replays_tasks_events_and_artifacts(tmp_path):
    gateway = QuantOSAgentGateway(tmp_path / "quant_os")

    task = gateway.create_task(
        kind="alpha_arena.seed",
        objective="score source-backed Pine ports before replay",
        requested_by="operator",
        priority=88,
        target={"exchange": "delta_india", "symbol": "ETH/USD:USD", "timeframe": "5m"},
        payload={"family": "fvg_liquidity_breakout_v1"},
    )
    assert task["status"] == TASK_QUEUED
    gateway.start_task(task["task_id"], lease_owner="arena-worker-1")
    event = gateway.emit_event(
        task["task_id"],
        event_type="REPLAY_WINDOW_SELECTED",
        message="using untouched 30d split",
        payload={"bars": 8640},
    )
    artifact = gateway.register_content_artifact(
        task["task_id"],
        artifact_type="replay_plan",
        summary="candidate matrix",
        content={"rows": 12, "strict": True},
        metadata={"source": "unit"},
    )
    completed = gateway.complete_task(task["task_id"], message="proof packet ready")

    reloaded = QuantOSAgentGateway(tmp_path / "quant_os")
    snapshot = reloaded.snapshot()

    assert completed["status"] == TASK_COMPLETED
    assert snapshot["summary"]["total_tasks"] == 1
    assert snapshot["summary"]["completed"] == 1
    assert snapshot["summary"]["events"] == 5
    assert snapshot["summary"]["artifacts"] == 1
    assert snapshot["tasks"][0]["artifact_ids"] == [artifact["artifact_id"]]
    assert any(row["event_id"] == event["event_id"] for row in snapshot["events"]["recent"])
    assert snapshot["can_trade"] is False
    assert snapshot["can_promote"] is False
    artifact_path = (
        tmp_path / "quant_os" / "artifacts" / task["task_id"] / f"{artifact['artifact_id']}.json"
    )
    assert artifact_path.exists()
    assert artifact["sha256"] == hashlib.sha256(artifact_path.read_bytes()).hexdigest()

    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="gateway directory"):
        gateway.register_artifact(
            task["task_id"],
            artifact_path=outside,
            artifact_type="external_probe",
            summary="blocked",
        )


def test_dashboard_quant_os_routes_are_auth_gated_and_stream_events(tmp_path):
    gateway = QuantOSAgentGateway(tmp_path / "quant_os")
    task = gateway.create_task(
        kind="alpha_arena.seed",
        objective="prepare Alpha Arena Lite ranking packet",
        requested_by="unit",
    )
    gateway.emit_event(task["task_id"], event_type="HEARTBEAT", message="alive")
    client = _dashboard_client(tmp_path)

    assert client.get("/quant-os/agent-gateway").status_code == 401

    snapshot = client.get("/quant-os/agent-gateway?token=dashboard-secret").json()
    assert snapshot["summary"]["total_tasks"] == 1
    assert snapshot["tasks"][0]["objective"] == "prepare Alpha Arena Lite ranking packet"
    assert snapshot["alpha_arena_lite"]["foundation_ready"] is True
    assert snapshot["can_trade"] is False

    events = client.get("/quant-os/agent-gateway/events?token=dashboard-secret")
    assert events.status_code == 200
    assert events.json()["events"]["count"] >= 2
    stream = client.get(
        "/quant-os/agent-gateway/events?token=dashboard-secret",
        headers={"Accept": "text/event-stream"},
    )
    assert stream.status_code == 200
    assert "event: HEARTBEAT" in stream.text
    assert "event: heartbeat" in stream.text


def test_agent_gateway_v2_can_create_events_and_artifacts_but_not_trade(tmp_path):
    client = _dashboard_client(tmp_path, store=_agent_store())
    payload = {
        "kind": "alpha_arena.experiment",
        "objective": "rank ETH Delta source-backed FVG and trail exits",
        "priority": 91,
        "target": {
            "exchange": "delta_india",
            "symbol": "ETH/USD:USD",
            "timeframe": "5m",
        },
        "payload": {"families": ["fvg_liquidity_breakout_v1", "trail_exit_lab_v1"]},
        "live_orders_enabled": False,
    }

    health = client.get("/api/agent/v2/health")
    assert health.status_code == 200
    assert health.json()["live_orders_available"] is False

    created = client.post("/api/agent/v2/tasks", json=payload, headers=_agent_headers())
    assert created.status_code == 202
    task = created.json()
    assert task["task_id"].startswith("qtask_")
    assert task["status"] == TASK_QUEUED
    assert task["can_trade"] is False
    assert task["can_promote"] is False

    event = client.post(
        f"/api/agent/v2/tasks/{task['task_id']}/events",
        json={
            "event_type": "BACKTEST_STARTED",
            "message": "started replay",
            "payload": {"tf": "5m"},
        },
        headers=_agent_headers(),
    )
    assert event.status_code == 202
    artifact = client.post(
        f"/api/agent/v2/tasks/{task['task_id']}/artifacts",
        json={
            "artifact_type": "arena_scorecard",
            "summary": "first scorecard",
            "content": {"pf": 1.2, "net_bps": -4.5},
            "metadata": {"oos": True},
        },
        headers=_agent_headers(),
    )
    assert artifact.status_code == 202
    assert artifact.json()["sha256"]
    external_path = tmp_path / "outside-scorecard.json"
    external_path.write_text("{}", encoding="utf-8")
    external = client.post(
        f"/api/agent/v2/tasks/{task['task_id']}/artifacts",
        json={
            "artifact_type": "arena_scorecard",
            "summary": "outside path should be rejected",
            "path": str(external_path),
        },
        headers=_agent_headers(),
    )
    assert external.status_code == 400
    assert "gateway directory" in external.json()["detail"]

    snapshot = client.get("/api/agent/v2/tasks", headers=_agent_headers()).json()
    assert snapshot["summary"]["total_tasks"] == 1
    assert snapshot["summary"]["artifacts"] == 1
    assert snapshot["tasks"][0]["status"] == TASK_QUEUED
    assert snapshot["can_trade"] is False
    assert snapshot["can_promote"] is False


def test_agent_gateway_v2_requires_write_scope_and_market_allowlist(tmp_path):
    read_only = _dashboard_client(tmp_path / "ro", store=_agent_store(scopes=("R",)))
    denied = read_only.post(
        "/api/agent/v2/tasks",
        json={"kind": "alpha", "objective": "blocked"},
        headers=_agent_headers(),
    )
    assert denied.status_code == 403

    scoped = _dashboard_client(tmp_path / "scoped", store=_agent_store())
    blocked_market = scoped.post(
        "/api/agent/v2/tasks",
        json={
            "kind": "alpha",
            "objective": "blocked market",
            "target": {"exchange": "bybit", "symbol": "BTC/USDT:USDT"},
        },
        headers=_agent_headers(),
    )
    assert blocked_market.status_code == 403
    assert "market not allowed" in blocked_market.json()["detail"]

    live_attempt = scoped.post(
        "/api/agent/v2/tasks",
        json={"kind": "alpha", "objective": "bad", "live_orders_enabled": True},
        headers=_agent_headers(),
    )
    assert live_attempt.status_code == 422

