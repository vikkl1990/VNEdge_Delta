"""Agent Gateway: scoped AI access without execution privileges."""

import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from vnedge.agent_gateway.auth import (
    AgentToken,
    AgentTokenStore,
    parse_agent_tokens_json,
    sha256_token,
)
from vnedge.dashboard.app import SnapshotProvider, create_app


def _agent_client(tmp_path, *, scopes=("R", "B"), markets=("binanceusdm:BTC/USDT:USDT",)):
    tmp_path.mkdir(parents=True, exist_ok=True)
    provider = SnapshotProvider()
    provider.publish(
        {
            "mode": "shadow",
            "equity": 500.0,
            "risk_status": "ok",
            "strategy_id": "funding_mean_reversion_v1",
            "lanes": [
                {
                    "lane_id": "funding_btc_binance",
                    "exchange": "binanceusdm",
                    "symbol": "BTC/USDT:USDT",
                    "timeframe": "1h",
                    "mode": "paper",
                    "strategy_id": "funding_mean_reversion_v1",
                    "state": "WAITING",
                    "fills": 2,
                    "virtual_pnl": 3.25,
                    "last_reason": "funding 0.05/0.85",
                }
            ],
        }
    )
    research = tmp_path / "latest.json"
    research.write_text(json.dumps({"results": [{"strategy_id": "x"}], "can_trade": False}))
    audit = tmp_path / "agent_audit.jsonl"
    jobs = tmp_path / "jobs"
    store = AgentTokenStore(
        [
            AgentToken.from_secret(
                name="alpha-agent",
                token="agent-secret",
                scopes=scopes,
                markets=markets,
                lanes=("*",),
                rate_limit_per_min=1_000,
            )
        ]
    )
    client = TestClient(
        create_app(
            provider,
            token="dashboard-secret",
            research_path=research,
            agent_token_store=store,
            agent_audit_path=audit,
            agent_jobs_dir=jobs,
        )
    )
    return client, audit, jobs


def _agent_headers(token="agent-secret"):
    return {"Authorization": f"Bearer {token}"}


def test_parse_agent_tokens_hashes_raw_secret_and_accepts_sha256():
    raw = json.dumps(
        [
            {
                "name": "dev-agent",
                "token": "raw-secret",
                "scopes": ["R", "B"],
                "paper_only": True,
            },
            {
                "name": "hashed-agent",
                "token_sha256": sha256_token("hashed-secret"),
                "scopes": "R",
            },
        ]
    )

    tokens = parse_agent_tokens_json(raw)

    assert [token.name for token in tokens] == ["dev-agent", "hashed-agent"]
    assert tokens[0].token_sha256 == sha256_token("raw-secret")
    assert "raw-secret" not in repr(tokens[0])
    assert tokens[0].scopes == frozenset({"R", "B"})
    assert tokens[1].scopes == frozenset({"R"})


def test_agent_token_expiry_and_rate_limit():
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    store = AgentTokenStore(
        [
            AgentToken.from_secret(
                name="expired",
                token="expired-secret",
                scopes=("R",),
                expires_at=past,
            ),
            AgentToken.from_secret(
                name="limited",
                token="limited-secret",
                scopes=("R",),
                rate_limit_per_min=1,
            ),
        ]
    )

    expired = store.authenticate("expired-secret")
    assert not expired.authorized
    assert "expired" in (expired.reason or "")

    assert store.authenticate("limited-secret", monotonic_now=10.0).authorized
    limited = store.authenticate("limited-secret", monotonic_now=11.0)
    assert not limited.authorized
    assert "rate limit" in (limited.reason or "")


def test_gateway_is_not_mounted_without_agent_tokens(tmp_path):
    provider = SnapshotProvider()
    provider.publish({"mode": "shadow"})
    client = TestClient(create_app(provider, token="dashboard-secret"))

    assert client.get("/api/agent/v1/health").status_code == 404


def test_agent_and_dashboard_tokens_are_separate(tmp_path):
    client, _, _ = _agent_client(tmp_path)

    assert client.get("/state", headers=_agent_headers()).status_code == 401
    assert (
        client.get(
            "/api/agent/v1/whoami",
            headers={"Authorization": "Bearer dashboard-secret"},
        ).status_code
        == 401
    )


def test_gateway_health_whoami_state_lanes_and_research(tmp_path):
    client, audit, _ = _agent_client(tmp_path)

    assert client.get("/api/agent/v1/health").json()["live_orders_available"] is False

    who = client.get("/api/agent/v1/whoami", headers=_agent_headers())
    assert who.status_code == 200
    assert who.headers["X-Agent-Name"] == "alpha-agent"
    assert who.json()["scopes"] == ["B", "R"]
    assert who.json()["paper_only"] is True
    assert who.json()["can_live_trade"] is False

    state = client.get("/api/agent/v1/state", headers=_agent_headers())
    assert state.status_code == 200
    assert state.json()["equity"] == 500.0

    lanes = client.get("/api/agent/v1/lanes", headers=_agent_headers()).json()
    assert lanes["count"] == 1
    assert lanes["can_trade"] is False
    assert lanes["lanes"][0]["lane_id"] == "funding_btc_binance"
    assert lanes["lanes"][0]["can_promote"] is False

    research = client.get("/api/agent/v1/research/latest", headers=_agent_headers()).json()
    assert research["results"][0]["strategy_id"] == "x"

    records = [json.loads(line) for line in audit.read_text().splitlines()]
    assert {record["action"] for record in records} >= {
        "whoami",
        "read_state",
        "read_lanes",
        "read_research_latest",
    }
    assert all(record["hash"] for record in records)
    assert records[0]["prev_hash"] == "0" * 64
    assert records[1]["prev_hash"] == records[0]["hash"]


def test_backtest_job_requires_scope_and_market_allowlist(tmp_path):
    read_only, audit, _ = _agent_client(tmp_path, scopes=("R",))
    payload = {
        "strategy_id": "sats_5m_scalper_v1",
        "exchange": "binanceusdm",
        "symbol": "BTC/USDT:USDT",
        "timeframe": "5m",
        "strict_mode": True,
        "live_orders_enabled": False,
    }

    denied = read_only.post("/api/agent/v1/backtests", json=payload, headers=_agent_headers())
    assert denied.status_code == 403
    assert "missing required agent scope" in denied.json()["detail"]
    assert "DENIED" in audit.read_text()

    scoped, _, _ = _agent_client(tmp_path / "scoped", markets=("bybit:ETH/USDT:USDT",))
    not_allowed = scoped.post("/api/agent/v1/backtests", json=payload, headers=_agent_headers())
    assert not_allowed.status_code == 403
    assert "market not allowed" in not_allowed.json()["detail"]


def test_backtest_request_is_recorded_as_research_only_job(tmp_path):
    client, audit, jobs = _agent_client(tmp_path)
    payload = {
        "strategy_id": "sats_5m_scalper_v1",
        "exchange": "binanceusdm",
        "symbol": "BTC/USDT:USDT",
        "timeframe": "5m",
        "hypothesis_id": "sats-bbp-stealthtrail",
        "strict_mode": True,
        "live_orders_enabled": False,
        "parameters": {"bbp_period": 13},
    }

    response = client.post("/api/agent/v1/backtests", json=payload, headers=_agent_headers())

    assert response.status_code == 202
    job = response.json()
    assert job["job_id"].startswith("agj_")
    assert job["status"] == "PENDING_RESEARCH_ONLY"
    assert job["can_trade"] is False
    assert job["can_promote"] is False
    assert job["live_orders_enabled"] is False
    assert job["request"]["strict_mode"] is True

    job_file = jobs / f"{job['job_id']}.json"
    assert json.loads(job_file.read_text())["job_id"] == job["job_id"]
    assert client.get("/api/agent/v1/jobs", headers=_agent_headers()).json()["count"] == 1
    detail = client.get(f"/api/agent/v1/jobs/{job['job_id']}", headers=_agent_headers())
    assert detail.status_code == 200
    assert detail.json()["request"]["hypothesis_id"] == "sats-bbp-stealthtrail"
    assert "request_backtest" in audit.read_text()


def test_backtest_request_refuses_non_strict_or_live_enabled(tmp_path):
    client, _, _ = _agent_client(tmp_path)
    base = {
        "strategy_id": "sats_5m_scalper_v1",
        "exchange": "binanceusdm",
        "symbol": "BTC/USDT:USDT",
        "timeframe": "5m",
    }

    non_strict = client.post(
        "/api/agent/v1/backtests",
        json={**base, "strict_mode": False},
        headers=_agent_headers(),
    )
    assert non_strict.status_code == 422

    live = client.post(
        "/api/agent/v1/backtests",
        json={**base, "live_orders_enabled": True},
        headers=_agent_headers(),
    )
    assert live.status_code == 422
