"""Dashboard — auth gates, snapshot schema, read-only surface."""

import json
import logging
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from vnedge.config.risk_config import RiskConfig
from vnedge.dashboard.app import SnapshotProvider, create_app
from vnedge.dashboard.auth import DashboardUser, TokenStore, parse_users_env
from vnedge.dashboard.state_snapshot import FeedHealth, build_snapshot
from vnedge.agent_gateway.jobs import DONE_STATUS, create_backtest_job, update_job
from vnedge.execution.journal import DecisionJournal
from vnedge.execution.order_manager import OrderManager
from vnedge.paper.fill_model import FillModel
from vnedge.paper.paper_broker import PaperBroker
from vnedge.paper.simulated_exchange import PaperOrderRequest, SimulatedExchange
from vnedge.risk.kill_switch import KillSwitch
from vnedge.risk.risk_manager import PreTradeRiskGateway
from vnedge.runtime.portfolio_tracker import PortfolioTracker

SYM = "BTC/USDT:USDT"


@pytest.fixture
def client():
    provider = SnapshotProvider()
    provider.publish({"mode": "shadow", "equity": 500.0})
    return TestClient(create_app(provider, token="t3st-token"))


def test_empty_token_refused_at_construction():
    with pytest.raises(ValueError, match="no token, no dashboard"):
        create_app(SnapshotProvider(), token="")


def test_state_requires_token(client):
    assert client.get("/state").status_code == 401
    assert client.get("/state?token=wrong").status_code == 401


def test_state_with_token(client):
    r = client.get("/state", headers={"Authorization": "Bearer t3st-token"})
    assert r.status_code == 200
    assert r.json()["mode"] == "shadow"
    assert client.get("/state?token=t3st-token").status_code == 200


def test_scorecard_endpoint_auth_gated_and_shaped(client):
    """Read-only /scorecard: auth-gated, returns per-strategy edge rows + the
    promotion-probe queue (empty fallback when the artifact is absent, as here)."""
    assert client.get("/scorecard").status_code == 401
    assert client.get("/scorecard?token=wrong").status_code == 401
    r = client.get("/scorecard?token=t3st-token")
    assert r.status_code == 200
    payload = r.json()
    assert isinstance(payload["strategies"], list)
    assert isinstance(payload["probes"], list)
    assert isinstance(payload["probe_actuals"], list)
    assert isinstance(payload["probe_actuals_summary"], dict)
    assert payload["can_trade"] is False and payload["can_promote"] is False


def test_darwinian_agent_survival_endpoint_auth_gated_and_shaped(tmp_path):
    provider = SnapshotProvider()
    provider.publish({"mode": "shadow", "equity": 500.0})
    report = tmp_path / "darwinian.json"
    report.write_text(
        json.dumps(
            {
                "report_id": "darwinian_agent_survival_v1",
                "summary": {"agent_count": 1, "upweighted_agents": 1},
                "cohorts": [{"cohort": "scalper_5m", "janus_weight": 1.0}],
                "agents": [{"agent_id": "stealth_trail_bbp_v1", "darwinian_weight": 1.05}],
                "operator_answer": "top agent survives",
                "can_trade": False,
                "can_promote": False,
            }
        )
    )
    app = create_app(
        provider,
        token="t3st-token",
        darwinian_agent_survival_path=report,
    )
    c = TestClient(app)

    assert c.get("/darwinian-agent-survival").status_code == 401
    r = c.get("/darwinian-agent-survival?token=t3st-token")
    assert r.status_code == 200
    payload = r.json()
    assert payload["summary"]["agent_count"] == 1
    assert payload["agents"][0]["agent_id"] == "stealth_trail_bbp_v1"
    assert payload["can_trade"] is False and payload["can_promote"] is False


def test_session_regime_endpoint_auth_gated_and_shaped(tmp_path):
    provider = SnapshotProvider()
    lane = "mystrat_binanceusdm_btcusdt_1h_shadow"
    provider.publish({"mode": "shadow", "equity": 500.0,
                      "lanes": [{"lane_id": lane}], "lane_id": lane})
    (tmp_path / f"{lane}.journal.jsonl").write_text(
        json.dumps({
            "ts": "2026-08-01T15:00:00+00:00", "kind": "shadow_outcome",
            "payload": {"symbol": "BTC/USDT:USDT", "side": "long",
                        "resolution": "stop", "entry_price": 100.0,
                        "exit_price": 97.0, "virtual_net_usd": -3.0,
                        "fees_usd": 0.0, "intent_key": "k1", "bars_held": 0},
        }) + "\n"
    )
    app = create_app(provider, token="t3st-token", journal_dir=tmp_path)
    c = TestClient(app)

    assert c.get("/session-regime").status_code == 401
    r = c.get("/session-regime?token=t3st-token")
    assert r.status_code == 200
    p = r.json()
    assert p["can_trade"] is False and p["can_promote"] is False
    assert p["overall"]["trades"] == 1
    us = [s for s in p["by_session"] if s["session"] == "us"][0]
    assert us["net_usd"] == -3.0
    assert p["worst_session"]["session"] == "us"


def test_delta_5m_event_clock_endpoint_auth_gated_and_research_only(tmp_path):
    provider = SnapshotProvider()
    provider.publish({"mode": "shadow", "equity": 500.0})
    report = tmp_path / "delta_5m.json"
    report.write_text(
        json.dumps(
            {
                "report_id": "delta_5m_event_clock_v1",
                "summary": {"ready_now": 1, "seconds_to_next_decision": 0},
                "rows": [
                    {
                        "symbol": "ETH/USD:USD",
                        "route": "MAKER_ONLY",
                        "direction": "UP",
                        "paper_execution_ready": True,
                    }
                ],
                "operator_answer": "Delta 5m paper route ready",
                "can_trade": False,
                "can_promote": False,
            }
        )
    )
    app = create_app(
        provider,
        token="t3st-token",
        delta_5m_event_clock_path=report,
    )
    c = TestClient(app)

    assert c.get("/delta-5m-event-clock").status_code == 401
    r = c.get("/delta-5m-event-clock?token=t3st-token")
    assert r.status_code == 200
    payload = r.json()
    assert payload["summary"]["ready_now"] == 1
    assert payload["rows"][0]["paper_execution_ready"] is True
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False


def test_meta_and_fleet_endpoints_auth_gated(client):
    """Read-only /meta (build/host/uptime) and /fleet (container status, empty
    until the host reporter runs) are token-gated and JSON-shaped."""
    for path in ("/meta", "/fleet"):
        assert client.get(path).status_code == 401
        assert client.get(path + "?token=wrong").status_code == 401
    meta = client.get("/meta?token=t3st-token")
    assert meta.status_code == 200
    body = meta.json()
    assert "build_sha" in body and "host" in body and "uptime_seconds" in body
    fleet = client.get("/fleet?token=t3st-token")
    assert fleet.status_code == 200
    assert isinstance(fleet.json().get("services"), list)


def test_external_repo_synthesis_endpoint_auth_gated_and_research_only(client):
    assert client.get("/external-repo-synthesis").status_code == 401
    assert client.get("/external-repo-synthesis?token=wrong").status_code == 401

    r = client.get("/external-repo-synthesis?token=t3st-token")
    assert r.status_code == 200
    payload = r.json()
    assert payload["synthesis_id"] == "external_repo_synthesis_20260731"
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False
    assert any(
        track["track_id"] == "terminal_operator_shell_v1"
        for track in payload["build_tracks"]
    )


def test_dashboard_shell_is_not_cached(client):
    """The SPA ships on every deploy — the shell must not be browser-cached, or
    a stale cached page shows empty panels against a live backend."""
    r = client.get("/")
    assert r.status_code == 200
    assert "no-store" in r.headers.get("cache-control", "").lower()


def test_dashboard_home_is_delta_research_sidecar(client):
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    assert "VNEDGE · Delta India Research Sidecar" in html
    assert 'value !== null && value !== undefined && value !== ""' in html
    assert "Paper &amp; Live Trading Locked" in html
    assert "Checking Delta publisher freshness" in html
    assert "No validated after-cost edge" in html
    assert "Historical values are never presented as live" in html
    assert "Provisional event scanner is collecting simulated outcomes." in html
    assert 'fetch("/delta-scalper"' in html
    assert 'fetch("/event-research-infrastructure"' in html
    assert 'href="/research-lab"' in html
    assert "BTCUSD" in html
    assert "ETHUSD" in html
    assert "Local Event, AI and Safety Build" in html
    assert "Delta Research Lanes" in html
    assert "Signal Funnel" in html
    assert "Live Market Context" in html
    assert 'localDemoHost?"vnedge-demo":""' in html
    assert "Dashboard session unavailable" in html
    assert "System Health" in html
    assert "Multi-TF Coherence" in html
    assert "Observations &amp; Latest Decisions" in html
    assert "/api/delta/health/stream" in html
    assert "new EventSource" in html
    assert 'href="/delta-research#evidence"' in html
    assert 'id="laneGrid"' in html
    assert 'id="signalFunnel"' in html
    assert 'data-funnel-stage=' in html
    for capability in (
        "Delta Event Recorder",
        "Incremental Event Trigger",
        "Absorption Detector",
        "Deterministic Event Replay",
        "Kronos AI Research",
        "Forced-Flow Panel",
        "Governance Proof Chain",
        "Delta Execution Safety",
        "TradingView Rule Adapter",
    ):
        assert capability in html


def test_delta_research_detail_is_secondary_read_only_view(client):
    response = client.get("/delta-research")
    assert response.status_code == 200
    html = response.text
    assert "Delta Research Detail" in html
    assert "Evidence &amp; Backtests" in html
    assert "Data Integrity" in html
    assert "Config &amp; Scanners" in html
    assert "Latency &amp; Feeds" in html
    assert "can_trade=false" in html
    assert 'fetch("/delta-scalper"' in html
    assert 'fetch("/event-research-infrastructure"' in html


def test_delta_endpoint_reports_locked_identity_and_unproven_edge(tmp_path):
    provider = SnapshotProvider()
    provider.publish({"mode": "research"})
    app = create_app(
        provider,
        token="t3st-token",
        delta_scalper_path=tmp_path / "missing-delta-snapshot.json",
    )
    payload = TestClient(app).get("/delta-scalper?token=t3st-token").json()

    assert payload["identity"] == {
        "product": "VNEDGE Delta India Research Laboratory",
        "runtime": "delta_scalper_research_shadow",
        "primary_symbols": ["BTCUSD", "ETHUSD"],
        "validated_after_cost_edge": False,
        "active_research_direction": "event_time_data_integrity_and_replay",
    }
    assert payload["policy"] == {
        "research_only": True,
        "l2_is_confirmation_only": True,
        "paper_trading": False,
        "live_trading": False,
        "validated_edge": False,
        "order_route": "absent",
        "broker": "absent",
        "can_trade": False,
        "can_promote": False,
    }


def test_delta_endpoint_downgrades_old_snapshot_and_nested_l2(tmp_path):
    provider = SnapshotProvider()
    provider.publish({"mode": "research"})
    path = tmp_path / "delta.json"
    path.write_text(json.dumps({
        "generated_at": "2020-01-01T00:00:00+00:00",
        "rows": [{
            "strategy_id": "delta_scalper_engine_v1",
            "symbol": "ETHUSD",
            "state": "WAITING",
            "latest_eval": {"l2_confirmation": {"status": "fresh"}},
        }],
        "architecture": {"components": {"scanner_engine": "disabled"}},
    }))
    app = create_app(provider, token="t3st-token", delta_scalper_path=path)

    payload = TestClient(app).get("/delta-scalper?token=t3st-token").json()

    assert payload["freshness"]["fresh"] is False
    assert payload["rows"][0]["state"] == "DATA_STALE"
    assert payload["lanes"][0]["l2_status"] == "snapshot_stale"
    assert payload["system_health"]["snapshot_fresh"] is False


def test_multi_venue_perps_desk_is_secondary_research_lab(client):
    r = client.get("/research-lab")
    assert r.status_code == 200
    html = r.text
    assert "Autonomous Perps Desk" in html
    assert 'id="vnmark"' in html                       # the logo mark
    for view in ("desk", "journal", "promote", "research", "system", "about"):
        assert f'data-view="{view}"' in html
    # desk cockpit containers, wired to the snapshot in JS
    assert 'id="lanes"' in html                        # active-lanes blotter
    assert 'id="tape"' in html                         # live signal tape
    assert 'id="watch"' in html                        # coverage strip
    assert 'id="eqChart"' in html                      # equity curve
    assert "Research Lanes" in html
    assert "Research Observation Tape" in html
    assert "Delta 5m Event Clock" in html
    assert "/delta-5m-event-clock" in html
    assert "Exchange Connections" in html
    assert "Safety Gates" in html
    assert "Paper Route Doctor" in html
    assert "/paper-route-doctor" in html
    assert "Paper Cadence Monitor" in html
    assert "/paper-lane-cadence" in html
    assert "Trade Profile Matrix" in html
    assert "/trade-profile-matrix" in html
    assert "Lane Survival Engine" in html
    assert "/lane-survival" in html
    assert "Paper Lane Governor" in html
    assert "/paper-lane-governor" in html
    # #358 renamed this panel "Paper Roster Drift" -> "Unified Lane Roster"
    # (same id/endpoint, now the governor-bounded roster view).
    assert "Unified Lane Roster" in html
    assert "/paper-roster-drift" in html
    assert "Maker Quote Lifecycle" in html
    assert "/maker-quote-lifecycle" in html
    # honest safety posture stays visible
    assert "no live orders" in html
    assert "SHADOW" in html
    # reads the read-only websocket stream (backend contract unchanged)
    assert "/ws?token=" in html
    assert "new WebSocket" in html
    assert 'id="profile"' in html                      # account menu
    # the old lab surface is gone
    assert "Quant Command Deck" not in html
    assert "Live Readiness Ladder" not in html
    assert "Trade Journal Ledger" not in html
    assert "next live phase" not in html


def test_cost_model_route_auth_gated_and_real_numbers(client):
    """Fee-wall honesty: /cost-model returns the REAL round-trip cost models,
    read from the research + paper constants (not hardcoded in the UI)."""
    assert client.get("/cost-model").status_code == 401
    assert client.get("/cost-model?token=wrong").status_code == 401
    r = client.get("/cost-model?token=t3st-token")
    assert r.status_code == 200
    assert r.headers["X-Dashboard-User"] == "operator"
    payload = r.json()
    # Numbers come from the same source the engines use.
    from vnedge.scalping.delta_engine.fee_model import DeltaFeeModel
    from vnedge.scalping.parameter_registry import (
        DEFAULT_SCALPER_PARAMETER_REGISTRY as registry,
    )

    fee = registry.fee_profile("delta_india")
    paper = DeltaFeeModel(default_slippage_bps_per_leg=1.5)
    assert payload["exchange"] == "delta_india"
    assert payload["maker_bps"] == fee.maker_bps
    assert payload["taker_bps"] == fee.taker_bps
    assert payload["slippage_bps"] == fee.slippage_bps
    # Maker entry is passive; only the taker exit pays modeled impact.
    assert payload["maker_first_rt_bps"] == pytest.approx(
        paper.maker_bps + paper.taker_bps + paper.default_slippage_bps_per_leg
    )
    # Both taker legs pay GST-inclusive fees and adverse slippage.
    assert payload["taker_rt_bps"] == pytest.approx(
        2 * paper.taker_bps + 2 * paper.default_slippage_bps_per_leg
    )
    assert payload["maker_first_rt_bps"] == 9.76
    assert payload["taker_rt_bps"] == 14.8
    assert payload["paper_fill_model"]["taker_fee_bps"] == paper.taker_bps
    assert payload["paper_fill_model"]["maker_fee_bps"] == paper.maker_bps
    assert payload["paper_fill_model"]["slippage_bps_per_leg"] == 1.5
    assert payload["paper_fill_model"]["taker_rt_bps"] == 14.8
    assert payload["paper_fill_model"]["includes_gst"] is True


def test_stale_paper_activation_is_not_reported_online(tmp_path):
    provider = SnapshotProvider()
    provider.publish({"mode": "shadow"})
    artifact = tmp_path / "paper_activation.json"
    artifact.write_text(
        json.dumps(
            {
                "generated_at": "2020-01-01T00:00:00+00:00",
                "summary": {"paper_online": 1, "paper_running": 1, "paper_waiting": 0},
                "rows": [{"activation_state": "PAPER_RUNNING", "route_status": "RUNNING"}],
                "paper_simulation_route_open": True,
                "can_trade": False,
                "can_promote": False,
            }
        )
    )
    app = create_app(
        provider,
        token="t3st-token",
        paper_lane_activation_path=artifact,
    )
    payload = TestClient(app).get("/paper-lane-activation?token=t3st-token").json()
    assert payload["freshness"]["status"] == "STALE"
    assert payload["summary"]["reported_paper_online"] == 1
    assert payload["summary"]["paper_online"] == 0
    assert payload["rows"][0]["activation_state"] == "RUNTIME_EVIDENCE_STALE"
    assert payload["paper_simulation_route_open"] is False


def test_pine_research_page_and_kb_are_auth_gated(tmp_path):
    provider = SnapshotProvider()
    provider.publish({"mode": "shadow", "equity": 500.0})
    kb = tmp_path / "pine_research_kb.json"
    distiller = tmp_path / "pine_alpha_distiller_latest.json"
    tv_rule = tmp_path / "tv_rule_adapter_latest.json"
    progress = tmp_path / "scanner_tournament_progress.json"
    uplift = tmp_path / "pine_edge_uplift_agent_latest.json"
    executor = tmp_path / "edge_uplift_experiments_latest.json"
    scanner_uplift = tmp_path / "scanner_backtest_uplift_latest.json"
    alpha_arena = tmp_path / "alpha_arena_lite_latest.json"
    quant_loop = tmp_path / "quant_loop_governance_latest.json"
    evidence_index = tmp_path / "evidence_index_latest.json"
    execution_profile = tmp_path / "execution_replay_profile_latest.json"
    kb.write_text(json.dumps({
        "generated_at": "2026-07-18T00:00:00+00:00",
        "source": "unit",
        "records": [
            {
                "script_id": "open_breakout",
                "title": "Open Breakout",
                "url": "https://www.tradingview.com/script/open/",
                "crypto_portability": "PORTABLE_WITH_CHANGES",
                "crypto_fit_score": 71,
                "backtests": [{"timeframe": "5m", "status": "queued"}],
            }
        ],
    }))
    distiller.write_text(json.dumps({
        "distiller_id": "pine_alpha_distiller_v1",
        "summary": {"source_backed_reviewed": 1, "port_candidates": 1},
        "port_tasks": [{"recommended_port": "fvg_liquidity_breakout_v1"}],
        "can_trade": False,
        "can_promote": False,
    }))
    tv_rule.write_text(json.dumps({
        "adapter_id": "tv_rule_adapter_v1",
        "status": "READY_FOR_LOCAL_REPLAY",
        "summary": {"assignments": 4, "indicator_assignments": 3, "blockers": 0},
        "rule_spec": {"title": "Local Breakout", "timeframe": "5m", "blockers": []},
        "evaluation": {"long_signals": 3, "short_signals": 1},
        "policy": {
            "network_access": False,
            "tradingview_data_used": False,
            "unofficial_tvscreener_dependency": False,
            "local_closed_candles_only": True,
            "research_only": True,
        },
        "can_trade": False,
        "can_promote": False,
    }))
    progress.write_text(json.dumps({
        "truth_layer": "scanner_tournament_progress_v1",
        "status": "running",
        "phase": "labeling_opportunities",
        "started_at": "2026-07-19T00:00:00+00:00",
        "heartbeat_at": "2026-07-19T00:01:00+00:00",
        "stale_after_seconds": 900,
        "target_count": 2,
        "strategy_count": 3,
        "total_work_units": 6,
        "completed_work_units": 2,
        "progress_pct": 33.33,
        "current_target": {"exchange": "delta_india", "symbol": "ETH/USD:USD", "timeframe": "5m"},
        "current_strategy": "stealth_trail_bbp_v1",
        "can_trade": False,
        "can_promote": False,
    }))
    uplift.write_text(json.dumps({
        "agent_id": "pine_edge_uplift_agent_v1",
        "summary": {
            "promotable_proofs": 1,
            "positive_under_sampled": 0,
            "near_miss_after_cost": 2,
            "experiments": 3,
        },
        "experiments": [{"experiment_type": "execution_filtered_replay"}],
        "can_trade": False,
        "can_promote": False,
    }))
    executor.write_text(json.dumps({
        "executor_id": "edge_uplift_executor_v1",
        "summary": {
            "tasks_total": 2,
            "ready_for_replay": 1,
            "ready_for_untouched_judgment": 0,
            "feature_bank_only": 1,
        },
        "tasks": [{"recommended_port": "fvg_liquidity_breakout_v1"}],
        "can_trade": False,
        "can_promote": False,
    }))
    scanner_uplift.write_text(json.dumps({
        "agent_id": "scanner_backtest_uplift_v1",
        "summary": {
            "evidence_rows": 60,
            "fee_wall_near_misses": 9,
            "visual_only_positive": 15,
            "experiments": 4,
        },
        "top_uplifts": [
            {
                "symbol": "DOGEUSD",
                "timeframe": "15m",
                "mode": "smart_ladder",
                "failure_mode": "FEE_WALL_NEAR_MISS",
                "avg_net_bps": -1.44,
                "profit_factor": 1.2,
                "required_uplift_bps": 26.44,
                "uplift_action": "TEST_MAKER_FIRST_CONTEXT_FILTERED_ROUTE",
            }
        ],
        "experiments": [{"experiment_type": "maker_first_context_filtered_replay"}],
        "operator_answer": "scanner uplift ready",
        "can_trade": False,
        "can_promote": False,
    }))
    alpha_arena.write_text(json.dumps({
        "arena_id": "alpha_arena_lite_v1",
        "summary": {
            "candidate_count": 1,
            "task_count": 1,
            "sample_valid": 0,
            "ready_for_untouched_judgment": 0,
        },
        "scorecards": [
            {
                "strategy_id": "luxara_live_plan_qtm_v1",
                "exchange": "delta_india",
                "symbol": "ETH/USD:USD",
                "arena_verdict": "EXPAND_UNTOUCHED_SAMPLE",
                "next_action": "RUN_FROZEN_SETUP_ON_NEXT_UNTOUCHED_WINDOW",
                "task_id": "qtask_test",
                "metrics": {
                    "top_avg_net_bps": 497.8,
                    "best_profit_factor": 999.0,
                    "max_samples": 3,
                    "sample_required": 20,
                },
            }
        ],
        "operator_answer": "arena ready",
        "can_trade": False,
        "can_promote": False,
    }))
    quant_loop.write_text(json.dumps({
        "governance_id": "quant_loop_governance_v1",
        "summary": {
            "readiness_score": 84,
            "readiness_level": "L3_GOVERNED_RESEARCH_READY",
            "loops_total": 6,
            "loops_ok": 5,
            "loops_waiting": 1,
            "loops_stale": 0,
            "loops_missing": 0,
            "collisions": 0,
            "budget_alerts": 0,
        },
        "loop_cards": [
            {
                "loop_id": "alpha_arena_lite",
                "status": "OK",
                "reason": "artifact is usable",
                "age_minutes": 2.5,
                "action": "EXPAND_SAMPLES_OR_PRE_REGISTER_JUDGMENT",
            }
        ],
        "gate_checks": [{"gate_id": "research_only_scope", "status": "PASS"}],
        "operator_answer": "loop governance ready",
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
    }))
    evidence_index.write_text(json.dumps({
        "evidence_store_id": "research_evidence_index_v1",
        "summary": {
            "total_records": 3,
            "completed_records": 3,
            "positive_after_cost": 2,
            "strict_fee_wall_breakers": 1,
            "sparse_positives": 1,
            "best_avg_net_bps": 31.25,
            "best_profit_factor": 1.72,
        },
        "fee_wall_breakers": [
            {
                "record_id": "r1",
                "source_kind": "fee_wall_forensics",
                "strategy_id": "sats_5m_scalper_v1",
                "exchange": "bybit",
                "symbol": "BTC/USDT:USDT",
                "timeframe": "5m",
                "samples": 31,
                "avg_net_bps": 31.25,
                "profit_factor": 1.72,
                "next_action": "PRE_REGISTER_UNTOUCHED_JUDGMENT",
                "can_trade": False,
                "can_promote": False,
            }
        ],
        "operator_answer": "evidence index ready",
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
    }))
    execution_profile.write_text(json.dumps({
        "execution_profile_id": "execution_realistic_replay_profile_v1",
        "summary": {
            "records": 2,
            "strict_economic_rows": 1,
            "execution_truth_ready": 0,
            "requires_execution_replay_before_paper": 1,
            "l3_or_l4_rows": 0,
            "settlement_blocked_rows": 0,
        },
        "rows": [
            {
                "row_id": "er1",
                "source_kind": "fee_wall_forensics",
                "strategy_id": "sats_5m_scalper_v1",
                "exchange": "bybit",
                "symbol": "BTC/USDT:USDT",
                "timeframe": "5m",
                "samples": 31,
                "avg_net_bps": 31.25,
                "profit_factor": 1.72,
                "profile_id": "L1_CANDLE_FORWARD_ROUTE_LABEL",
                "strict_economic_edge": True,
                "execution_truth_ready": False,
                "requires_execution_replay_before_paper": True,
                "next_action": "RUN_EXECUTION_REPLAY_PROFILE_L3_OR_L4",
                "blockers": ["candle_forward_label_is_not_order_fill_evidence"],
                "can_trade": False,
                "can_promote": False,
            }
        ],
        "paper_blocked_rows": [
            {
                "row_id": "er1",
                "strategy_id": "sats_5m_scalper_v1",
                "exchange": "bybit",
                "symbol": "BTC/USDT:USDT",
                "timeframe": "5m",
                "samples": 31,
                "avg_net_bps": 31.25,
                "profit_factor": 1.72,
                "profile_id": "L1_CANDLE_FORWARD_ROUTE_LABEL",
                "next_action": "RUN_EXECUTION_REPLAY_PROFILE_L3_OR_L4",
            }
        ],
        "operator_answer": "strict row needs execution replay",
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
    }))
    app = create_app(
        provider,
        token="t3st-token",
        pine_research_path=kb,
        pine_alpha_distiller_path=distiller,
        tv_rule_spec_path=tv_rule,
        backtest_progress_path=progress,
        pine_edge_uplift_path=uplift,
        edge_uplift_executor_path=executor,
        scanner_backtest_uplift_path=scanner_uplift,
        alpha_arena_lite_path=alpha_arena,
        quant_loop_governance_path=quant_loop,
        evidence_index_path=evidence_index,
        execution_replay_profile_path=execution_profile,
    )
    client = TestClient(app)

    page = client.get("/pine-research")
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert "Pine Research Lab" in page.text
    assert "/pine-research/kb" in page.text
    assert "/pine-research/distiller" in page.text
    assert "/pine-research/rule-spec" in page.text
    assert "/pine-research/progress" in page.text
    assert "/pine-research/uplift-agent" in page.text
    assert "/pine-research/uplift-executor" in page.text
    assert "/pine-research/scanner-uplift" in page.text
    assert "/pine-research/alpha-arena-lite" in page.text
    assert "/pine-research/quant-loop-governance" in page.text
    assert "/pine-research/evidence-index" in page.text
    assert "/pine-research/execution-profile" in page.text
    assert "Backtest Evidence" in page.text
    assert "Backtest Progress" in page.text
    assert "Agentic Edge Uplift" in page.text
    assert "Scanner Backtest Uplift" in page.text
    assert "Alpha Arena Lite" in page.text
    assert "Quant Loop Governance" in page.text
    assert "Edge Uplift Executor" in page.text
    assert "Unified Evidence Index" in page.text
    assert "Execution Replay Profile" in page.text
    assert "Pine Coverage Auditor" in page.text
    assert "Local TradingView Rule Adapter" in page.text
    assert "renderCoverageAudit" in page.text
    assert "renderBacktestProgress" in page.text
    assert "renderUpliftAgent" in page.text
    assert "renderScannerUplift" in page.text
    assert "renderAlphaArenaLite" in page.text
    assert "renderQuantLoopGovernance" in page.text
    assert "renderUpliftExecutor" in page.text
    assert "renderEvidenceIndex" in page.text
    assert "renderExecutionProfile" in page.text
    assert "renderTVRuleAdapter" in page.text
    assert "AI review" in page.text
    assert "hasCompletedEvidence" in page.text
    assert "publisherEvidenceCounts" in page.text
    assert "evidence_source" in page.text
    assert "read-only" in page.text.lower()
    assert "cannot trade" in page.text.lower()

    assert client.get("/pine-research/kb").status_code == 401
    assert client.get("/pine-research/distiller").status_code == 401
    assert client.get("/pine-research/rule-spec").status_code == 401
    assert client.get("/pine-research/progress").status_code == 401
    assert client.get("/pine-research/uplift-agent").status_code == 401
    assert client.get("/pine-research/uplift-executor").status_code == 401
    assert client.get("/pine-research/scanner-uplift").status_code == 401
    assert client.get("/pine-research/alpha-arena-lite").status_code == 401
    assert client.get("/pine-research/quant-loop-governance").status_code == 401
    assert client.get("/pine-research/evidence-index").status_code == 401
    assert client.get("/pine-research/execution-profile").status_code == 401
    r = client.get("/pine-research/kb?token=t3st-token")
    assert r.status_code == 200
    payload = r.json()
    assert payload["summary"]["total"] == 1
    assert payload["summary"]["portable"] == 1
    assert payload["coverage_audit"]["coverage_id"] == "pine_coverage_auditor_v1"
    assert payload["coverage_audit"]["visible_records"] == 1
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False
    d = client.get("/pine-research/distiller?token=t3st-token")
    assert d.status_code == 200
    distiller_payload = d.json()
    assert distiller_payload["summary"]["port_candidates"] == 1
    assert distiller_payload["can_trade"] is False
    assert distiller_payload["can_promote"] is False
    tv = client.get("/pine-research/rule-spec?token=t3st-token")
    assert tv.status_code == 200
    tv_payload = tv.json()
    assert tv_payload["adapter_id"] == "tv_rule_adapter_v1"
    assert tv_payload["policy"]["network_access"] is False
    assert tv_payload["policy"]["tradingview_data_used"] is False
    assert tv_payload["can_trade"] is False
    assert tv_payload["can_promote"] is False
    p = client.get("/pine-research/progress?token=t3st-token")
    assert p.status_code == 200
    progress_payload = p.json()
    assert progress_payload["truth_layer"] == "scanner_tournament_progress_v1"
    assert progress_payload["status"] == "running"
    assert progress_payload["stale_after_seconds"] == 900
    assert progress_payload["current_strategy"] == "stealth_trail_bbp_v1"
    assert progress_payload["can_trade"] is False
    assert progress_payload["can_promote"] is False
    u = client.get("/pine-research/uplift-agent?token=t3st-token")
    assert u.status_code == 200
    uplift_payload = u.json()
    assert uplift_payload["agent_id"] == "pine_edge_uplift_agent_v1"
    assert uplift_payload["summary"]["promotable_proofs"] == 1
    assert uplift_payload["can_trade"] is False
    assert uplift_payload["can_promote"] is False
    su = client.get("/pine-research/scanner-uplift?token=t3st-token")
    assert su.status_code == 200
    scanner_uplift_payload = su.json()
    assert scanner_uplift_payload["agent_id"] == "scanner_backtest_uplift_v1"
    assert scanner_uplift_payload["summary"]["fee_wall_near_misses"] == 9
    assert scanner_uplift_payload["can_trade"] is False
    assert scanner_uplift_payload["can_promote"] is False
    aal = client.get("/pine-research/alpha-arena-lite?token=t3st-token")
    assert aal.status_code == 200
    alpha_payload = aal.json()
    assert alpha_payload["arena_id"] == "alpha_arena_lite_v1"
    assert alpha_payload["summary"]["candidate_count"] == 1
    assert alpha_payload["can_trade"] is False
    assert alpha_payload["can_promote"] is False
    ql = client.get("/pine-research/quant-loop-governance?token=t3st-token")
    assert ql.status_code == 200
    quant_payload = ql.json()
    assert quant_payload["governance_id"] == "quant_loop_governance_v1"
    assert quant_payload["summary"]["readiness_score"] == 84
    assert quant_payload["can_trade"] is False
    assert quant_payload["can_promote"] is False
    assert quant_payload["live_orders_enabled"] is False
    ei = client.get("/pine-research/evidence-index?token=t3st-token")
    assert ei.status_code == 200
    evidence_payload = ei.json()
    assert evidence_payload["evidence_store_id"] == "research_evidence_index_v1"
    assert evidence_payload["summary"]["strict_fee_wall_breakers"] == 1
    assert evidence_payload["fee_wall_breakers"][0]["can_trade"] is False
    assert evidence_payload["can_promote"] is False
    assert evidence_payload["live_orders_enabled"] is False
    ep = client.get("/pine-research/execution-profile?token=t3st-token")
    assert ep.status_code == 200
    execution_payload = ep.json()
    assert execution_payload["execution_profile_id"] == "execution_realistic_replay_profile_v1"
    assert execution_payload["summary"]["requires_execution_replay_before_paper"] == 1
    assert execution_payload["rows"][0]["can_trade"] is False
    assert execution_payload["can_trade"] is False
    assert execution_payload["can_promote"] is False
    assert execution_payload["live_orders_enabled"] is False
    x = client.get("/pine-research/uplift-executor?token=t3st-token")
    assert x.status_code == 200
    executor_payload = x.json()
    assert executor_payload["executor_id"] == "edge_uplift_executor_v1"
    assert executor_payload["summary"]["ready_for_replay"] == 1
    assert executor_payload["can_trade"] is False
    assert executor_payload["can_promote"] is False


def test_pine_research_missing_kb_falls_back_to_seed(tmp_path):
    provider = SnapshotProvider()
    provider.publish({"mode": "shadow", "equity": 500.0})
    client = TestClient(
        create_app(provider, token="t3st-token", pine_research_path=tmp_path / "missing.json")
    )

    payload = client.get("/pine-research/kb?token=t3st-token").json()

    assert payload["summary"]["total"] >= 3
    assert payload["source"] == "default_seed"
    assert any(r["script_id"] == "tradingview_catalog" for r in payload["records"])


def test_quantified_strategy_lab_serves_title_inventory(tmp_path):
    provider = SnapshotProvider()
    provider.publish({"mode": "shadow", "equity": 500.0})
    client = TestClient(
        create_app(
            provider,
            token="t3st-token",
            quantified_strategy_lab_path=tmp_path / "missing.json",
        )
    )

    page = client.get("/quantified-strategy-lab")
    payload = client.get("/quantified-strategy-lab/kb?token=t3st-token").json()

    assert page.status_code == 200
    assert payload["summary"]["total_strategies"] == 95
    assert payload["summary"]["source_backed_rules"] == 0
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False


def test_quantified_strategy_lab_serves_port_factory(tmp_path):
    provider = SnapshotProvider()
    provider.publish({"mode": "shadow", "equity": 500.0})
    client = TestClient(
        create_app(
            provider,
            token="t3st-token",
            quantified_port_factory_path=tmp_path / "missing.json",
        )
    )

    payload = client.get("/quantified-strategy-lab/port-factory?token=t3st-token").json()

    assert payload["factory_id"] == "quantified_port_factory_v1"
    assert payload["summary"]["chunks"]["A"] == 3
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False


def test_quantified_strategy_lab_serves_pullback_proof(tmp_path):
    provider = SnapshotProvider()
    provider.publish({"mode": "shadow", "equity": 500.0})
    blueprint = tmp_path / "quantified_blueprint_proof_latest.json"
    blueprint.write_text(json.dumps({
        "proof_id": "quantified_blueprint_proof_v1",
        "generated_at": "2026-08-01T00:00:00+00:00",
        "summary": {
            "total_cells": 360,
            "completed_cells": 1,
            "ports": 6,
            "proxy_adapter_cells": 120,
        },
        "rows": [{
            "port_id": "range_volatility_breakout_reversion_v1",
            "setup_mode": "breakout_only",
            "canonical_adapter": True,
            "status": "DONE_RESEARCH_ONLY",
            "verdict": "FEE_WALL_NEAR_MISS",
        }],
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
    }))
    arbiter = tmp_path / "quantified_proof_result_arbiter_latest.json"
    arbiter.write_text(json.dumps({
        "arbiter_id": "quantified_proof_result_arbiter_v1",
        "generated_at": "2026-08-01T00:00:00+00:00",
        "summary": {
            "total_cells": 360,
            "actionable_cells": 3,
            "ready_for_judgment": 1,
            "proxy_edges": 1,
        },
        "action_queue": [{
            "bucket": "READY_FOR_UNTOUCHED_JUDGMENT",
            "next_action": "QUEUE_UNTOUCHED_WINDOW_JUDGMENT",
            "can_trade": False,
            "can_promote": False,
        }],
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
    }))
    proof = tmp_path / "quantified_pullback_reversion_proof_latest.json"
    proof.write_text(json.dumps({
        "proof_id": "quantified_pullback_reversion_proof_v1",
        "generated_at": "2026-07-31T00:00:00+00:00",
        "summary": {"total_cells": 1, "completed_cells": 0},
        "rows": [{"status": "PENDING_RESEARCH_ONLY"}],
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
    }))
    client = TestClient(
        create_app(
            provider,
            token="t3st-token",
            quantified_blueprint_proof_path=blueprint,
            quantified_proof_arbiter_path=arbiter,
            quantified_pullback_proof_path=proof,
        )
    )

    page = client.get("/quantified-strategy-lab")
    blueprint_payload = client.get(
        "/quantified-strategy-lab/blueprint-proof?token=t3st-token"
    ).json()
    arbiter_payload = client.get(
        "/quantified-strategy-lab/proof-arbiter?token=t3st-token"
    ).json()
    payload = client.get(
        "/quantified-strategy-lab/pullback-proof?token=t3st-token"
    ).json()

    assert page.status_code == 200
    assert "Blueprint Proof Matrix" in page.text
    assert "Proof Result Arbiter" in page.text
    assert blueprint_payload["proof_id"] == "quantified_blueprint_proof_v1"
    assert blueprint_payload["summary"]["ports"] == 6
    assert blueprint_payload["can_trade"] is False
    assert blueprint_payload["can_promote"] is False
    assert arbiter_payload["arbiter_id"] == "quantified_proof_result_arbiter_v1"
    assert arbiter_payload["summary"]["ready_for_judgment"] == 1
    assert arbiter_payload["can_trade"] is False
    assert arbiter_payload["can_promote"] is False
    assert payload["proof_id"] == "quantified_pullback_reversion_proof_v1"
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False


def test_cost_model_route_has_no_control_verbs(client):
    """The new route is read-only like every other data route."""
    for method in ("post", "put", "delete"):
        assert getattr(client, method)("/cost-model?token=t3st-token").status_code in (404, 405)


def _synthetic_research_doc() -> dict:
    """Mirror how continuous_research folds the scalp/AI surfaces into
    latest.json, with one row per panel so the round-trip test can assert the
    exact nested fields the frontend renders from."""
    agg = {
        "taker_taker": {"events": 12, "net_usd": -0.42, "avg_net_bps": -3.5,
                        "win_rate_pct": 41.0, "profit_factor": 0.8},
        "maker_first": {"events": 12, "net_usd": 0.31, "avg_net_bps": 2.6,
                        "win_rate_pct": 58.0, "profit_factor": 1.4},
    }
    return {
        "results": [],
        "cascade_reversion": {
            "generated_at": "2026-07-14T00:00:00+00:00",
            "targets": [{
                "exchange": "binanceusdm", "symbol": "BTC/USDT:USDT",
                "events": 12, "verdict": "MAKER_ONLY_POSITIVE",
                "days_scanned": ["20260701", "20260702"],
                "days_with_liquidations": ["20260701", "20260702", "20260703"],
                "aggregates": agg, "can_trade": False, "can_promote": False,
            }],
            "summary": {"targets": 1, "events": 12,
                        "verdict_counts": {"MAKER_ONLY_POSITIVE": 1}},
            "can_trade": False, "can_promote": False,
        },
        "leadlag_echo_scalp": {
            "generated_at": "2026-07-14T00:00:00+00:00",
            "targets": [{
                "base": "BTC", "leader_exchange": "binanceusdm",
                "leader_symbol": "BTC/USDT:USDT",
                "follower_exchange": "delta_india", "follower_symbol": "BTCUSD",
                "events": 9, "verdict": "CANDIDATE",
                "overlap_days": ["20260701", "20260702"],
                "days_scanned": ["20260701", "20260702"],
                "lag_estimate": {"impulses": 30, "responded": 21,
                                 "response_rate_pct": 70.0, "caveat": "research estimate only"},
                "aggregates": agg, "can_trade": False, "can_promote": False,
            }],
            "summary": {"targets": 1, "events": 9,
                        "verdict_counts": {"CANDIDATE": 1}},
            "can_trade": False, "can_promote": False,
        },
        "realtime_shadow_scalp": {
            "generated_at": "2026-07-14T00:00:00+00:00",
            "mode": "realtime_shadow_only", "notional_usd": 100.0,
            "lanes": [{
                "family": "cascade_reversion", "exchange": "binanceusdm",
                "symbol": "BTC/USDT:USDT", "verdict": "UNDER_SAMPLED",
                "intents": 3, "virtual_trades": 2, "events_per_hour": 0.5,
                "last_intent_ms": 1_752_000_000_000, "last_event_ms": 1_752_000_100_000,
                "aggregates": agg, "maker_beats_taker": True,
                "can_trade": False, "can_promote": False,
            }],
            "summary": {"lanes": 1, "intents": 3, "virtual_trades": 2,
                        "maker_beats_taker_lanes": 1,
                        "verdict_counts": {"UNDER_SAMPLED": 1}},
            "can_trade": False, "can_promote": False,
        },
        "ai_candidates": {
            "generated_at": "2026-07-14T00:00:00+00:00",
            "candidates": [{
                "strategy_id": "ai_momentum_x", "source_file": "ai_momentum_x.py",
                "family": "ai_authored",
                "causality": {"passed": True, "n_bars": 500},
                "walk_forward": {"windows": 4, "traded_windows": 3, "oos_trades": 18,
                                 "oos_net_usd": 12.5, "profitable_windows_pct": 66.7,
                                 "passed": False},
                "verdict": "REJECT", "reasons": ["profit factor below gate"],
                "can_trade": False, "can_promote": False,
                "requires_untouched_judgment": True,
            }],
            "summary": {"loaded": 1, "rejected_files": 0, "candidates": 1,
                        "verdict_counts": {"REJECT": 1}, "can_trade": False,
                        "can_promote": False, "requires_untouched_judgment": True},
            "can_trade": False, "can_promote": False,
            "requires_untouched_judgment": True,
        },
    }


def test_research_route_delivers_scalp_and_ai_panel_data(tmp_path):
    """Serve-and-assert: a synthetic latest.json with the new keys round-trips
    through GET /research with the exact nested fields the panels render."""
    research = tmp_path / "latest.json"
    research.write_text(json.dumps(_synthetic_research_doc()))
    provider = SnapshotProvider()
    provider.publish({"mode": "shadow"})
    client = TestClient(create_app(provider, token="t3st-token", research_path=research))
    doc = client.get("/research?token=t3st-token").json()

    cr = doc["cascade_reversion"]["targets"][0]
    assert cr["verdict"] == "MAKER_ONLY_POSITIVE"
    assert cr["aggregates"]["taker_taker"]["net_usd"] == -0.42
    assert cr["aggregates"]["maker_first"]["net_usd"] == 0.31  # maker beats taker

    ll = doc["leadlag_echo_scalp"]["targets"][0]
    assert ll["verdict"] == "CANDIDATE"
    assert ll["lag_estimate"]["response_rate_pct"] == 70.0

    lane = doc["realtime_shadow_scalp"]["lanes"][0]
    assert lane["family"] == "cascade_reversion"
    assert lane["last_intent_ms"] == 1_752_000_000_000  # "last fire" delivered

    ai = doc["ai_candidates"]["candidates"][0]
    assert ai["verdict"] == "REJECT"
    assert ai["causality"]["passed"] is True
    assert ai["can_trade"] is False and ai["requires_untouched_judgment"] is True


def test_dashboard_inline_js_parses_under_node():
    """The inline dashboard script must be syntactically valid JS (guards the
    hand-written render functions). Skipped when node is unavailable."""
    import re
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path as _Path

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    static_dir = (
        _Path(__file__).resolve().parents[1] / "src/vnedge/dashboard/static"
    )
    for filename in ("index.html", "delta_home.html"):
        html = (static_dir / filename).read_text()
        scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
        assert scripts, f"no inline script found in {filename}"
        for block in scripts:
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
                fh.write(block)
                path = fh.name
            result = subprocess.run(
                [node, "--check", path], capture_output=True, text=True
            )
            assert result.returncode == 0, f"{filename}: {result.stderr}"


def test_no_snapshot_yet_is_503():
    app = create_app(SnapshotProvider(), token="t3st-token")
    r = TestClient(app).get("/state?token=t3st-token")
    assert r.status_code == 503


def test_websocket_requires_token(client):
    with pytest.raises(Exception):
        with client.websocket_connect("/ws?token=wrong") as ws:
            ws.receive_json()


def test_websocket_pushes_snapshot(client):
    with client.websocket_connect("/ws?token=t3st-token") as ws:
        assert ws.receive_json()["equity"] == 500.0


def test_history_endpoint_auth_and_content(tmp_path):
    import json

    hist = tmp_path / "eq.jsonl"
    hist.write_text(
        "\n".join(json.dumps({"ts": f"2026-07-03T0{i}:00:00+00:00", "equity": 500.0 + i})
                  for i in range(3))
    )
    provider = SnapshotProvider()
    provider.publish({"mode": "paper"})
    client = TestClient(create_app(provider, token="t3st-token", history_path=hist))
    assert client.get("/history").status_code == 401
    points = client.get("/history?token=t3st-token").json()
    assert len(points) == 3
    assert points[-1]["equity"] == 502.0


def test_history_without_file_is_empty(client):
    assert client.get("/history?token=t3st-token").json() == []


def test_alpha_council_and_workbench_endpoints_are_auth_gated(tmp_path):
    council = tmp_path / "alpha_council_latest.json"
    workbench = tmp_path / "alpha_workbench_latest.json"
    vibe = tmp_path / "vibe_intelligence_latest.json"
    readiness = tmp_path / "lane_promotion_readiness_latest.json"
    scanner = tmp_path / "realtime_scanner_latest.json"
    causality = tmp_path / "lane_firing_causality_latest.json"
    council.write_text(json.dumps({
        "summary": {"debated": 2},
        "debates": [{"next_action": "RUN_CONSERVATIVE_L2_REPLAY"}],
        "can_trade": False,
        "can_promote": False,
    }))
    workbench.write_text(json.dumps({
        "summary": {"open_tasks": 1},
        "tasks": [{"task_type": "conservative_replay"}],
        "can_trade": False,
        "can_promote": False,
    }))
    vibe.write_text(json.dumps({
        "summary": {"active": 1},
        "cards": [{"lifecycle_state": "ACTIVE"}],
        "can_trade": False,
        "can_promote": False,
    }))
    readiness.write_text(json.dumps({
        "summary": {"paper_review_ready": 1},
        "rows": [{"status": "PAPER_REVIEW_READY"}],
        "can_trade": False,
        "can_promote": False,
    }))
    scanner.write_text(json.dumps({
        "mode": "live_observation_not_replay",
        "summary": {"near_trigger": 1},
        "rows": [{"state": "NEAR_TRIGGER"}],
        "can_trade": False,
        "can_promote": False,
    }))
    causality.write_text(json.dumps({
        "report_id": "lane_firing_causality_v1",
        "summary": {"near_trigger": 1, "paper_review_ready": 1},
        "promotion_board": {"ready_for_review": [{"strategy_id": "alpha"}]},
        "rows": [{"scanner_state": "NEAR_TRIGGER"}],
        "can_trade": False,
        "can_promote": False,
    }))
    paper_activation = tmp_path / "paper_activation.json"
    paper_activation.write_text(json.dumps({
        "mode": "read_only_activation_truth",
        "summary": {"paper_online": 2},
        "rows": [{
            "lane_key": "alpha|delta_india|eth/usd:usd|5m",
            "activation_state": "PAPER_ONLINE_WAITING",
            "exchange": "delta_india",
            "symbol": "ETH/USD:USD",
            "timeframe": "5m",
            "strategy_id": "stealth_trail_bbp_v1",
            "sizing_profiles": {
                "paper": {
                    "profile": "paper",
                    "risk_compatible": True,
                    "requested_margin_usd": 100,
                    "requested_leverage": 25,
                    "requested_notional_usd": 2500,
                }
            },
        }],
        "can_trade": False,
        "can_promote": False,
    }))
    paper_route_doctor = tmp_path / "paper_route_doctor.json"
    paper_route_doctor.write_text(json.dumps({
        "mode": "read_only_paper_route_doctor",
        "summary": {"journal_missing": 1},
        "rows": [{
            "lane_key": "alpha|delta_india|eth/usd:usd|5m",
            "doctor_state": "ROUTE_READY_JOURNAL_MISSING",
            "exchange": "delta_india",
            "symbol": "ETH/USD:USD",
            "timeframe": "5m",
            "strategy_id": "stealth_trail_bbp_v1",
            "next_action": "inspect runner write path",
        }],
        "runner_service": {"state": "up", "up": True},
        "can_trade": False,
        "can_promote": False,
    }))
    paper_lane_cadence = tmp_path / "paper_lane_cadence.json"
    paper_lane_cadence.write_text(json.dumps({
        "mode": "read_only_paper_lane_cadence",
        "summary": {"cadence_ok": 1, "stale": 0},
        "rows": [{
            "lane_key": "alpha|delta_india|eth/usd:usd|5m",
            "cadence_state": "EVALUATING_NO_SIGNAL",
            "exchange": "delta_india",
            "symbol": "ETH/USD:USD",
            "timeframe": "5m",
            "strategy_id": "stealth_trail_bbp_v1",
        }],
        "can_trade": False,
        "can_promote": False,
    }))
    paper_lane_performance = tmp_path / "paper_lane_performance.json"
    paper_lane_performance.write_text(json.dumps({
        "mode": "read_only_paper_performance",
        "summary": {"online_no_trades": 1},
        "rows": [{
            "lane_key": "alpha|delta_india|eth/usd:usd|5m",
            "state": "PAPER_ONLINE_NO_TRADES",
            "exchange": "delta_india",
            "symbol": "ETH/USD:USD",
            "timeframe": "5m",
            "strategy_id": "stealth_trail_bbp_v1",
            "closed_trades": 0,
        }],
        "can_trade": False,
        "can_promote": False,
    }))
    paper_exit_autopsy = tmp_path / "paper_trade_exit_autopsy.json"
    paper_exit_autopsy.write_text(json.dumps({
        "mode": "read_only_paper_trade_exit_autopsy",
        "summary": {"stop_dominated": 1, "closed_trades": 3},
        "rows": [{
            "lane_key": "alpha|delta_india|eth/usd:usd|5m",
            "loss_driver": "STOP_DOMINATED",
            "exchange": "delta_india",
            "symbol": "ETH/USD:USD",
            "timeframe": "5m",
            "strategy_id": "stealth_trail_bbp_v1",
            "closed_trades": 3,
        }],
        "can_trade": False,
        "can_promote": False,
    }))
    paper_entry_autopsy = tmp_path / "paper_trade_entry_autopsy.json"
    paper_entry_autopsy.write_text(json.dumps({
        "mode": "read_only_paper_trade_entry_autopsy",
        "summary": {"stale_signal_lanes": 1, "closed_trades": 3},
        "rows": [{
            "lane_key": "alpha|delta_india|eth/usd:usd|5m",
            "entry_state": "ENTRY_SIGNAL_STALE",
            "exchange": "delta_india",
            "symbol": "ETH/USD:USD",
            "timeframe": "5m",
            "strategy_id": "stealth_trail_bbp_v1",
            "closed_trades": 3,
        }],
        "can_trade": False,
        "can_promote": False,
    }))
    trade_analyzer_os = tmp_path / "trade_analyzer_os.json"
    trade_analyzer_os.write_text(json.dumps({
        "mode": "read_only_trade_analyzer_os",
        "summary": {
            "giveback_dominated": 1,
            "closed_trades": 3,
            "overnight_hold_drift": 0,
        },
        "rows": [{
            "lane_id": "alpha|delta_india|eth/usd:usd|5m",
            "primary_diagnosis": "GIVEBACK_DOMINATED",
            "exchange": "delta_india",
            "symbol": "ETH/USD:USD",
            "timeframe": "5m",
            "strategy_id": "stealth_trail_bbp_v1",
            "closed_trades": 3,
        }],
        "recent_trades": [],
        "can_trade": False,
        "can_promote": False,
    }))
    maker_quote_lifecycle = tmp_path / "maker_quote_lifecycle.json"
    maker_quote_lifecycle.write_text(json.dumps({
        "mode": "read_only_maker_quote_lifecycle",
        "summary": {
            "maker_attempts": 3,
            "maker_fill_unproven": 1,
            "taker_fallback_forbidden": 1,
        },
        "rows": [{
            "lane_id": "alpha|delta_india|eth/usd:usd|5m",
            "lifecycle_state": "MAKER_FILL_UNPROVEN",
            "exchange": "delta_india",
            "symbol": "ETH/USD:USD",
            "timeframe": "5m",
            "strategy_id": "stealth_trail_bbp_v1",
            "maker_attempts": 3,
            "maker_fill_rate_pct": 0.0,
            "next_action": "COLLECT_OR_REPAIR_MAKER_FILL_TELEMETRY",
        }],
        "can_trade": False,
        "can_promote": False,
    }))
    paper_contract = tmp_path / "paper_trade_contract.json"
    paper_contract.write_text(json.dumps({
        "report_id": "paper_trade_contract_reconciler_v1",
        "mode": "read_only_paper_contract_reconciliation",
        "summary": {"contract_broken_lanes": 1, "closed_trades": 3},
        "rows": [{
            "lane_key": "alpha|delta_india|eth/usd:usd|5m",
            "verdict": "CONTRACT_BROKEN",
            "exchange": "delta_india",
            "symbol": "ETH/USD:USD",
            "timeframe": "5m",
            "strategy_id": "stealth_trail_bbp_v1",
            "closed_trades": 3,
            "critical_violations": 1,
            "next_action": "repair reduce-only exit contract",
        }],
        "can_trade": False,
        "can_promote": False,
    }))
    paper_promotion_bridge = tmp_path / "paper_promotion_bridge.json"
    paper_promotion_bridge.write_text(json.dumps({
        "report_id": "paper_promotion_bridge_v1",
        "mode": "read_only_paper_promotion_bridge",
        "summary": {"repair_first": 1, "paper_review_ready": 0},
        "rows": [{
            "lane_key": "alpha|delta_india|eth/usd:usd|5m",
            "decision": "REPAIR_FIRST",
            "stage": "REPAIR",
            "owner": "system",
            "exchange": "delta_india",
            "symbol": "ETH/USD:USD",
            "timeframe": "5m",
            "strategy_id": "stealth_trail_bbp_v1",
            "next_action": "repair contract first",
        }],
        "can_trade": False,
        "can_promote": False,
    }))
    lane_survival = tmp_path / "lane_survival.json"
    lane_survival.write_text(json.dumps({
        "mode": "read_only_lane_survival",
        "summary": {"survivor_candidates": 0, "demote_to_shadow": 1},
        "rows": [{"survival_state": "DEMOTE_TO_SHADOW"}],
        "can_trade": False,
        "can_promote": False,
    }))
    paper_lane_governor = tmp_path / "paper_lane_governor.json"
    paper_lane_governor.write_text(json.dumps({
        "mode": "read_only_paper_lane_governor",
        "summary": {"paper_roster": 1, "demotion_queue": 1},
        "rows": [{"governor_bucket": "DEMOTION_QUEUE"}],
        "proposed_roster": {"paper_lanes": [{"lane_id": "alpha"}]},
        "can_trade": False,
        "can_promote": False,
    }))
    paper_roster_drift = tmp_path / "paper_roster_drift.json"
    paper_roster_drift.write_text(json.dumps({
        "mode": "read_only_unified_lane_roster",
        "summary": {"extra_paper_lanes": 2, "missing_paper_lanes": 0},
        "rows": [{"drift_state": "EXTRA_RUNNING_PAPER", "lane_id": "extra"}],
        "lane_rows": [{
            "roster_mode": "paper",
            "roster_state": "EXTRA_RUNNING_PAPER",
            "lane_id": "extra",
        }],
        "can_trade": False,
        "can_promote": False,
    }))
    promotion_runbook = tmp_path / "promotion_review_runbook.json"
    promotion_runbook.write_text(json.dumps({
        "runbook_id": "promotion_review_runbook_v1",
        "summary": {"blocked_by_red_team": 1, "human_review_ready": 0},
        "rows": [{
            "review_state": "BLOCKED_BY_RED_TEAM",
            "strategy_id": "funding_mr",
            "primary_charge": "fee_drag",
            "can_trade": False,
            "can_promote": False,
        }],
        "operator_answer": "blocked by red-team charges",
        "can_trade": False,
        "can_promote": False,
    }))
    provider = SnapshotProvider()
    provider.publish({"mode": "shadow"})
    client = TestClient(create_app(
        provider,
        token="t3st-token",
        alpha_council_path=council,
        alpha_workbench_path=workbench,
        vibe_intelligence_path=vibe,
        lane_readiness_path=readiness,
        promotion_review_runbook_path=promotion_runbook,
        realtime_scanner_path=scanner,
        lane_firing_causality_path=causality,
        paper_lane_activation_path=paper_activation,
        paper_route_doctor_path=paper_route_doctor,
        paper_lane_cadence_path=paper_lane_cadence,
        paper_lane_performance_path=paper_lane_performance,
        paper_trade_entry_autopsy_path=paper_entry_autopsy,
        paper_trade_exit_autopsy_path=paper_exit_autopsy,
        trade_analyzer_os_path=trade_analyzer_os,
        maker_quote_lifecycle_path=maker_quote_lifecycle,
        paper_trade_contract_reconciler_path=paper_contract,
        paper_promotion_bridge_path=paper_promotion_bridge,
        lane_survival_path=lane_survival,
        paper_lane_governor_path=paper_lane_governor,
        paper_roster_drift_path=paper_roster_drift,
    ))

    assert client.get("/alpha-council").status_code == 401
    assert client.get("/alpha-workbench").status_code == 401
    assert client.get("/vibe-intelligence").status_code == 401
    assert client.get("/lane-readiness").status_code == 401
    assert client.get("/promotion-review-runbook").status_code == 401
    assert client.get("/realtime-scanner").status_code == 401
    assert client.get("/lane-firing-causality").status_code == 401
    assert client.get("/paper-lane-activation").status_code == 401
    assert client.get("/paper-route-doctor").status_code == 401
    assert client.get("/paper-lane-cadence").status_code == 401
    assert client.get("/paper-trade-entry-autopsy").status_code == 401
    assert client.get("/paper-trade-exit-autopsy").status_code == 401
    assert client.get("/trade-analyzer-os").status_code == 401
    assert client.get("/maker-quote-lifecycle").status_code == 401
    assert client.get("/paper-trade-contract-reconciler").status_code == 401
    assert client.get("/paper-promotion-bridge").status_code == 401
    assert client.get("/lane-survival").status_code == 401
    assert client.get("/paper-lane-governor").status_code == 401
    assert client.get("/paper-roster-drift").status_code == 401
    assert client.get("/trade-profile-matrix").status_code == 401
    assert client.get("/operator-actions").status_code == 401
    assert client.get("/alpha-council?token=t3st-token").json()["summary"]["debated"] == 2
    assert client.get("/alpha-workbench?token=t3st-token").json()["summary"]["open_tasks"] == 1
    vibe_payload = client.get("/vibe-intelligence?token=t3st-token").json()
    assert vibe_payload["summary"]["active"] == 1
    assert vibe_payload["cards"][0]["lifecycle_state"] == "ACTIVE"
    assert vibe_payload["can_promote"] is False
    lane_payload = client.get("/lane-readiness?token=t3st-token").json()
    assert lane_payload["summary"]["paper_review_ready"] == 1
    assert lane_payload["can_promote"] is False
    promotion_payload = client.get("/promotion-review-runbook?token=t3st-token").json()
    assert promotion_payload["summary"]["blocked_by_red_team"] == 1
    assert promotion_payload["rows"][0]["primary_charge"] == "fee_drag"
    assert promotion_payload["can_trade"] is False
    assert promotion_payload["can_promote"] is False
    scanner_payload = client.get("/realtime-scanner?token=t3st-token").json()
    assert scanner_payload["summary"]["near_trigger"] == 1
    assert scanner_payload["mode"] == "live_observation_not_replay"
    assert scanner_payload["can_trade"] is False
    causality_payload = client.get("/lane-firing-causality?token=t3st-token").json()
    assert causality_payload["summary"]["paper_review_ready"] == 1
    assert causality_payload["promotion_board"]["ready_for_review"][0]["strategy_id"] == "alpha"
    assert causality_payload["can_promote"] is False
    paper_activation_payload = client.get("/paper-lane-activation?token=t3st-token").json()
    assert paper_activation_payload["summary"]["paper_online"] == 2
    assert paper_activation_payload["mode"] == "read_only_activation_truth"
    assert paper_activation_payload["can_trade"] is False
    paper_route_payload = client.get("/paper-route-doctor?token=t3st-token").json()
    assert paper_route_payload["summary"]["journal_missing"] == 1
    assert paper_route_payload["mode"] == "read_only_paper_route_doctor"
    assert paper_route_payload["can_trade"] is False
    assert paper_route_payload["can_promote"] is False
    paper_cadence_payload = client.get("/paper-lane-cadence?token=t3st-token").json()
    assert paper_cadence_payload["summary"]["cadence_ok"] == 1
    assert paper_cadence_payload["mode"] == "read_only_paper_lane_cadence"
    assert paper_cadence_payload["can_trade"] is False
    assert paper_cadence_payload["can_promote"] is False
    paper_entry_payload = client.get("/paper-trade-entry-autopsy?token=t3st-token").json()
    assert paper_entry_payload["summary"]["stale_signal_lanes"] == 1
    assert paper_entry_payload["mode"] == "read_only_paper_trade_entry_autopsy"
    assert paper_entry_payload["can_trade"] is False
    assert paper_entry_payload["can_promote"] is False
    paper_exit_payload = client.get("/paper-trade-exit-autopsy?token=t3st-token").json()
    assert paper_exit_payload["summary"]["stop_dominated"] == 1
    assert paper_exit_payload["mode"] == "read_only_paper_trade_exit_autopsy"
    assert paper_exit_payload["can_trade"] is False
    assert paper_exit_payload["can_promote"] is False
    trade_analyzer_payload = client.get("/trade-analyzer-os?token=t3st-token").json()
    assert trade_analyzer_payload["summary"]["giveback_dominated"] == 1
    assert trade_analyzer_payload["mode"] == "read_only_trade_analyzer_os"
    assert trade_analyzer_payload["can_trade"] is False
    assert trade_analyzer_payload["can_promote"] is False
    maker_quote_payload = client.get("/maker-quote-lifecycle?token=t3st-token").json()
    assert maker_quote_payload["summary"]["maker_attempts"] == 3
    assert maker_quote_payload["mode"] == "read_only_maker_quote_lifecycle"
    assert maker_quote_payload["can_trade"] is False
    assert maker_quote_payload["can_promote"] is False
    paper_contract_payload = client.get(
        "/paper-trade-contract-reconciler?token=t3st-token"
    ).json()
    assert paper_contract_payload["summary"]["contract_broken_lanes"] == 1
    assert paper_contract_payload["mode"] == "read_only_paper_contract_reconciliation"
    assert paper_contract_payload["can_trade"] is False
    assert paper_contract_payload["can_promote"] is False
    bridge_payload = client.get("/paper-promotion-bridge?token=t3st-token").json()
    assert bridge_payload["summary"]["repair_first"] == 1
    assert bridge_payload["mode"] == "read_only_paper_promotion_bridge"
    assert bridge_payload["rows"][0]["decision"] == "REPAIR_FIRST"
    assert bridge_payload["can_trade"] is False
    assert bridge_payload["can_promote"] is False
    lane_survival_payload = client.get("/lane-survival?token=t3st-token").json()
    assert lane_survival_payload["summary"]["demote_to_shadow"] == 1
    assert lane_survival_payload["mode"] == "read_only_lane_survival"
    assert lane_survival_payload["can_trade"] is False
    assert lane_survival_payload["can_promote"] is False
    paper_governor_payload = client.get("/paper-lane-governor?token=t3st-token").json()
    assert paper_governor_payload["summary"]["demotion_queue"] == 1
    assert paper_governor_payload["mode"] == "read_only_paper_lane_governor"
    assert paper_governor_payload["can_trade"] is False
    assert paper_governor_payload["can_promote"] is False
    paper_roster_payload = client.get("/paper-roster-drift?token=t3st-token").json()
    assert paper_roster_payload["summary"]["extra_paper_lanes"] == 2
    assert paper_roster_payload["mode"] == "read_only_unified_lane_roster"
    assert paper_roster_payload["lane_rows"][0]["roster_mode"] == "paper"
    assert paper_roster_payload["can_trade"] is False
    assert paper_roster_payload["can_promote"] is False
    trade_profile_payload = client.get("/trade-profile-matrix?token=t3st-token").json()
    assert trade_profile_payload["mode"] == "read_only_trade_profile_planner"
    assert trade_profile_payload["can_trade"] is False
    assert trade_profile_payload["can_promote"] is False
    operator_actions_payload = client.get("/operator-actions?token=t3st-token").json()
    assert operator_actions_payload["mode"] == "read_only_operator_action_queue"
    assert operator_actions_payload["summary"]["route_repairs"] == 1
    assert operator_actions_payload["rows"][0]["bucket"] == "REPAIR_ROUTE"
    assert operator_actions_payload["rows"][0]["action"] == "inspect runner write path"
    assert (
        operator_actions_payload["source_report_ids"]["contract_reconciler"]
        == "paper_trade_contract_reconciler_v1"
    )
    assert operator_actions_payload["can_trade"] is False
    assert operator_actions_payload["can_promote"] is False


def test_agent_jobs_endpoint_is_dashboard_gated_and_summarized(tmp_path):
    jobs_dir = tmp_path / "jobs"
    job = create_backtest_job(
        jobs_dir=jobs_dir,
        agent="quantos_seed",
        request={
            "strategy_id": "sats_5m_scalper_v1",
            "exchange": "delta_india",
            "symbol": "ETH/USDT:USDT",
            "timeframe": "5m",
            "hypothesis_id": "seed-sats",
            "strict_mode": True,
            "live_orders_enabled": False,
            "parameters": {"seed_id": "seed-sats"},
        },
    )
    update_job(
        jobs_dir,
        job["job_id"],
        status=DONE_STATUS,
        result={"metrics": {"net_profit_usd": 1.25, "num_trades": 3}},
    )
    provider = SnapshotProvider()
    provider.publish({"mode": "shadow"})
    client = TestClient(create_app(provider, token="t3st-token", agent_jobs_dir=jobs_dir))

    assert client.get("/agent-jobs").status_code == 401
    payload = client.get("/agent-jobs?token=t3st-token").json()

    assert payload["summary"]["total"] == 1
    assert payload["summary"]["done"] == 1
    assert payload["summary"]["gateway_http_mounted"] is False
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False
    assert payload["jobs"][0]["adapter"] == "registered_backtest"
    assert payload["jobs"][0]["hypothesis_id"] == "seed-sats"
    assert payload["jobs"][0]["result_summary"] == "net +1.25 USD / trades 3"


def test_agent_jobs_missing_dir_is_safe(tmp_path):
    provider = SnapshotProvider()
    provider.publish({"mode": "shadow"})
    client = TestClient(
        create_app(provider, token="t3st-token", agent_jobs_dir=tmp_path / "missing")
    )

    payload = client.get("/agent-jobs?token=t3st-token").json()
    assert payload["summary"]["total"] == 0
    assert payload["jobs"] == []
    assert payload["live_orders_enabled"] is False


def test_agentic_research_os_endpoint_is_dashboard_gated(tmp_path):
    agentic = tmp_path / "agentic_research_os_latest.json"
    agentic.write_text(
        json.dumps(
            {
                "os_id": "agentic_research_os_v2",
                "summary": {"operator_actions": 2, "critical_actions": 1},
                "agent_scorecards": [{"agent_id": "task_ledger", "can_promote": False}],
                "operator_queue": [{"action": "RECLAIM_OR_FAIL_STALE_TASK", "can_trade": False}],
                "source_status": [{"source": "quant_os_agent_gateway", "state": "OK"}],
                "operator_answer": "repair stale task",
                "can_trade": False,
                "can_promote": False,
                "live_orders_enabled": False,
            }
        ),
        encoding="utf-8",
    )
    provider = SnapshotProvider()
    provider.publish({"mode": "shadow"})
    client = TestClient(
        create_app(provider, token="t3st-token", agentic_research_os_path=agentic)
    )

    assert client.get("/agentic-research-os").status_code == 401
    payload = client.get("/agentic-research-os?token=t3st-token").json()
    assert payload["os_id"] == "agentic_research_os_v2"
    assert payload["summary"]["critical_actions"] == 1
    assert payload["operator_queue"][0]["can_trade"] is False
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False
    assert payload["live_orders_enabled"] is False


def test_alpha_council_and_workbench_missing_files_are_safe(tmp_path):
    provider = SnapshotProvider()
    provider.publish({"mode": "shadow"})
    client = TestClient(create_app(
        provider,
        token="t3st-token",
        alpha_council_path=tmp_path / "missing_council.json",
        alpha_workbench_path=tmp_path / "missing_workbench.json",
        vibe_intelligence_path=tmp_path / "missing_vibe.json",
        lane_readiness_path=tmp_path / "missing_readiness.json",
        realtime_scanner_path=tmp_path / "missing_scanner.json",
        lane_firing_causality_path=tmp_path / "missing_causality.json",
        paper_lane_activation_path=tmp_path / "missing_paper_activation.json",
    ))

    council = client.get("/alpha-council?token=t3st-token").json()
    workbench = client.get("/alpha-workbench?token=t3st-token").json()
    vibe = client.get("/vibe-intelligence?token=t3st-token").json()
    readiness = client.get("/lane-readiness?token=t3st-token").json()
    scanner = client.get("/realtime-scanner?token=t3st-token").json()
    causality = client.get("/lane-firing-causality?token=t3st-token").json()
    paper_activation = client.get("/paper-lane-activation?token=t3st-token").json()
    assert council == {"summary": {}, "debates": [], "can_trade": False, "can_promote": False}
    assert workbench == {"summary": {}, "tasks": [], "can_trade": False, "can_promote": False}
    assert vibe == {"summary": {}, "cards": [], "can_trade": False, "can_promote": False}
    assert readiness == {
        "summary": {},
        "rows": [],
        "operator_answer": "lane readiness report unavailable",
        "can_trade": False,
        "can_promote": False,
    }
    assert scanner["summary"] == {}
    assert scanner["rows"] == []
    assert scanner["operator_answer"] == "real-time scanner report unavailable"
    assert scanner["mode"] == "live_observation_not_replay"
    assert scanner["strategy_evidence_registry"]["registry_id"] == (
        "vnedge_canonical_strategy_evidence_registry"
    )
    assert scanner["withheld_lanes"] == []
    assert scanner["can_trade"] is False
    assert scanner["can_promote"] is False
    assert causality == {
        "summary": {},
        "promotion_board": {},
        "rows": [],
        "operator_answer": "lane firing causality report unavailable",
        "can_trade": False,
        "can_promote": False,
    }
    assert paper_activation == {
        "summary": {},
        "boards": {},
        "rows": [],
        "operator_answer": "paper lane activation report unavailable",
        "mode": "read_only_activation_truth",
        "can_trade": False,
        "can_promote": False,
    }


def _history_world(tmp_path):
    """Two lanes' equity files + fills + a snapshot trade log, all exportable."""
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    old = (now - timedelta(days=10)).isoformat()
    recent = (now - timedelta(hours=2)).isoformat()
    _write_jsonl(tmp_path / "alpha.equity.jsonl", [
        {"ts": old, "equity": 500.0},
        {"ts": recent, "equity": 510.0},
    ])
    _write_jsonl(tmp_path / "beta.equity.jsonl", [
        {"ts": recent, "equity": 250.0},
    ])
    _write_jsonl(tmp_path / "beta.fills.jsonl", [
        {"ts": recent, "symbol": SYM, "side": "buy", "quantity": 0.01,
         "price": 100.0, "fee_usd": 0.02, "realized_pnl_usd": 0.0,
         "client_order_id": "c1", "prev_hash": "0" * 64, "hash": "aa"},
    ])
    provider = SnapshotProvider()
    provider.publish({
        "mode": "shadow", "lane_id": "alpha",
        "session": {"trade_log": [
            {"ts": recent, "event": "signal_fired", "detail": "primary lane log"},
        ]},
        "lanes": [
            {"lane_id": "alpha", "trade_log": []},
            {"lane_id": "beta", "trade_log": [
                {"ts": old, "event": "fill", "detail": "old fill"},
                {"ts": recent, "event": "exit", "detail": "flat"},
            ]},
        ],
    })
    client = TestClient(create_app(
        provider, token="t3st-token",
        history_path=tmp_path / "alpha.equity.jsonl", journal_dir=tmp_path,
    ))
    return client, old, recent


def test_history_lane_and_days_params(tmp_path):
    client, old, recent = _history_world(tmp_path)

    # default: primary lane (alpha), full history
    points = client.get("/history?token=t3st-token").json()
    assert [p["equity"] for p in points] == [500.0, 510.0]

    # lane switch
    beta = client.get("/history?token=t3st-token&lane=beta").json()
    assert [p["equity"] for p in beta] == [250.0]

    # days filter drops the 10-day-old point
    fresh = client.get("/history?token=t3st-token&days=7").json()
    assert [p["equity"] for p in fresh] == [510.0]
    assert client.get("/history?token=t3st-token&days=30&lane=alpha").json() == points

    # invalid params are rejected, not swallowed
    assert client.get("/history?token=t3st-token&lane=../evil").status_code == 400
    assert client.get("/history?token=t3st-token&days=soon").status_code == 400
    assert client.get("/history?token=t3st-token&days=-1").status_code == 400

    # unknown lane is empty, not an error
    assert client.get("/history?token=t3st-token&lane=ghost").json() == []


def test_export_csv_shape_and_auth(tmp_path):
    import csv
    import io

    client, old, recent = _history_world(tmp_path)
    assert client.get("/export.csv").status_code == 401
    assert client.get("/export.csv?token=wrong").status_code == 401
    assert client.get("/export.csv?token=t3st-token&lane=../evil").status_code == 400

    r = client.get("/export.csv?token=t3st-token&lane=beta")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert 'filename="vnedge_beta.csv"' in r.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert set(rows[0]) == {"record_type", "ts", "lane", "equity", "event",
                            "detail", "symbol", "side", "quantity", "price",
                            "fee_usd", "realized_pnl_usd", "client_order_id"}
    by_type = {}
    for row in rows:
        by_type.setdefault(row["record_type"], []).append(row)
    assert all(row["lane"] == "beta" for row in rows)
    assert [e["equity"] for e in by_type["equity"]] == ["250.0"]
    assert {t["event"] for t in by_type["trade_log"]} == {"fill", "exit"}
    fill = by_type["fill"][0]
    assert (fill["symbol"], fill["side"], fill["client_order_id"]) == (SYM, "buy", "c1")
    assert fill["fee_usd"] == "0.02"

    # default lane = primary (alpha): its equity + the primary session log
    primary = list(csv.DictReader(io.StringIO(
        client.get("/export.csv?token=t3st-token").text)))
    assert all(row["lane"] == "alpha" for row in primary)
    assert {row["record_type"] for row in primary} == {"equity", "trade_log"}
    assert any(row["detail"] == "primary lane log" for row in primary)

    # days filter applies to every record type
    windowed = list(csv.DictReader(io.StringIO(
        client.get("/export.csv?token=t3st-token&lane=beta&days=7").text)))
    assert all(row["ts"] >= old for row in windowed)
    assert not any(row["detail"] == "old fill" for row in windowed)


def _write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def test_trade_journal_route_projects_journal_and_fill_ledgers(tmp_path):
    provider = SnapshotProvider()
    provider.publish({
        "lane_id": "alpha",
        "ts": "2026-07-16T03:00:00+00:00",
        "positions": [
            {
                "symbol": SYM,
                "side": "long",
                "quantity": 0.01,
                "entry_price": 100.0,
                "mark_price": 105.0,
                "notional_usd": 1.05,
                "unrealized_usd": 0.05,
            }
        ],
        "open_orders": [
            {
                "client_order_id": "working-1",
                "state": "open",
                "side": "long",
                "order_type": "limit",
                "requested_qty": 0.01,
                "limit_price": 99.0,
            }
        ],
        "session": {
            "trade_log": [
                {
                    "ts": "2026-07-16T02:55:00+00:00",
                    "event": "order_submitted",
                    "detail": "working long limit",
                }
            ]
        },
    })
    _write_jsonl(tmp_path / "alpha.fills.jsonl", [
        {
            "ts": "2026-07-16T02:58:00+00:00",
            "symbol": SYM,
            "side": "sell",
            "quantity": 0.01,
            "price": 105.0,
            "fee_usd": 0.01,
            "realized_pnl_usd": 0.05,
            "client_order_id": "exit-1",
            "hash": "abc",
        }
    ])
    _write_jsonl(tmp_path / "alpha.journal.jsonl", [
        {
            "ts": "2026-07-16T02:50:00+00:00",
            "kind": "order_intent",
            "payload": {
                "client_order_id": "working-1",
                "intent": {
                    "symbol": SYM,
                    "side": "long",
                    "quantity": 0.01,
                    "order_type": "limit",
                    "strategy_id": "test_strategy",
                },
            },
        },
        {
            "ts": "2026-07-16T02:59:00+00:00",
            "kind": "shadow_outcome",
            "payload": {
                "intent_key": "v1",
                "resolution": "target",
                "side": "long",
                "virtual_net_usd": 0.33,
                "entry_price": 100.0,
                "exit_price": 103.0,
                "fees_usd": 0.02,
            },
        },
    ])
    client = TestClient(create_app(
        provider, token="t3st-token",
        history_path=tmp_path / "alpha.equity.jsonl", journal_dir=tmp_path,
    ))

    assert client.get("/trade-journal").status_code == 401
    assert client.get("/trade-journal?token=wrong").status_code == 401
    assert client.get("/trade-journal?token=t3st-token&lane=../bad").status_code == 400
    assert client.get("/trade-journal?token=t3st-token&limit=nope").status_code == 400

    response = client.get("/trade-journal?token=t3st-token&lane=alpha")
    assert response.status_code == 200
    payload = response.json()
    assert payload["policy"]["read_only"] is True
    assert payload["policy"]["can_trade"] is False
    assert payload["summary"]["positions"] == 1
    assert payload["summary"]["open_orders"] == 1
    assert payload["summary"]["fills"] == 1
    assert payload["summary"]["closed_trades"] == 2
    assert payload["orders"][0]["client_order_id"] == "working-1"
    assert {row["kind"] for row in payload["closed_trades"]} == {
        "actual_closing_fill",
        "shadow_outcome",
    }
    assert any(event["source"] == "snapshot_trade_log" for event in payload["events"])


def _incident_world(tmp_path):
    """alerts.jsonl + one lane journal with a mix of incident and routine kinds."""
    alerts = tmp_path / "alerts.jsonl"
    _write_jsonl(alerts, [
        {"ts": "2026-07-10T02:00:00+00:00", "rule_id": "feed_stale",
         "severity": "critical", "message": "feed stale: 130s since last event"},
        {"ts": "2026-07-10T03:00:00+00:00", "rule_id": "new_fill",
         "severity": "info", "message": "fill #1"},  # notification, not incident
        {"ts": "2026-07-10T04:00:00+00:00", "rule_id": "loss_streak",
         "severity": "warning", "message": "3 consecutive losing round trips"},
    ])
    _write_jsonl(tmp_path / "btc_lane.journal.jsonl", [
        {"ts": "2026-07-10T01:00:00+00:00", "kind": "order_intent",
         "payload": {"client_order_id": "x"}},  # routine, not incident
        {"ts": "2026-07-10T05:00:00+00:00", "kind": "reconciliation_fail_closed",
         "payload": {"mismatches": ["position drift"]}},
        {"ts": "2026-07-10T00:30:00+00:00", "kind": "orphaned_paper_position",
         "payload": {"symbol": SYM}},
        {"ts": "2026-07-10T00:15:00+00:00", "kind": "plan_restore_rejected",
         "payload": {"reason": "wrong symbol"}},
        {"ts": "2026-07-10T06:00:00+00:00", "kind": "emergency_flatten_started",
         "payload": {"flatten_id": "f1"}},
    ])
    provider = SnapshotProvider()
    provider.publish({"mode": "shadow"})
    return TestClient(create_app(
        provider, token="t3st-token", alerts_path=alerts, journal_dir=tmp_path
    ))


def test_incidents_requires_token(tmp_path):
    client = _incident_world(tmp_path)
    assert client.get("/incidents").status_code == 401
    assert client.get("/incidents?token=wrong").status_code == 401


def test_incidents_merges_orders_and_maps_severity(tmp_path):
    client = _incident_world(tmp_path)
    incidents = client.get("/incidents?token=t3st-token").json()

    # merged from both sources, reverse-chronological
    stamps = [i["ts"] for i in incidents]
    assert stamps == sorted(stamps, reverse=True)
    by_source = {i["source"]: i for i in incidents}
    assert "alert:feed_stale" in by_source
    assert "journal:btc_lane" in {i["source"] for i in incidents}

    # routine records are excluded from the incident timeline
    assert not any("new_fill" in i["source"] for i in incidents)
    assert not any(i["message"].startswith("order_intent") for i in incidents)

    # severity mapping: journal kinds carry hard-coded severities
    sev = {i["message"].split(" — ")[0]: i["severity"] for i in incidents
           if i["source"].startswith("journal:")}
    assert sev["reconciliation_fail_closed"] == "critical"
    assert sev["orphaned_paper_position"] == "warning"
    assert sev["plan_restore_rejected"] == "warning"
    assert sev["emergency_flatten_started"] == "critical"

    # every incident links a runbook anchor
    assert all(i["runbook"].startswith("/runbooks#") for i in incidents)
    kill = next(i for i in incidents if "emergency_flatten" in i["message"])
    assert kill["runbook"] == "/runbooks#kill-switch-and-flatten"


def test_incidents_limit_param_and_missing_files(tmp_path):
    client = _incident_world(tmp_path)
    assert len(client.get("/incidents?token=t3st-token&limit=2").json()) == 2
    assert client.get("/incidents?token=t3st-token&limit=nope").status_code == 400

    provider = SnapshotProvider()
    provider.publish({"mode": "shadow"})
    bare = TestClient(create_app(
        provider, token="t3st-token",
        alerts_path=tmp_path / "missing" / "alerts.jsonl",
        journal_dir=tmp_path / "missing",
    ))
    assert bare.get("/incidents?token=t3st-token").json() == []


def test_runbooks_route_is_auth_gated_and_anchored(client):
    assert client.get("/runbooks").status_code == 401
    r = client.get("/runbooks?token=t3st-token")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    # anchors the incident links point at, from the real docs/RUNBOOKS.md
    for anchor in ("kill-switch-and-flatten", "reconciliation-fail-closed",
                   "orphaned-paper-position", "plan-restore-rejected",
                   "general-triage"):
        assert f"id='{anchor}'" in r.text
    assert "NEVER auto-resets" in r.text


def test_runbooks_custom_path_and_missing_file(tmp_path):
    provider = SnapshotProvider()
    provider.publish({"mode": "shadow"})
    doc = tmp_path / "RUNBOOKS.md"
    doc.write_text("# Title\n\n## My Incident Type\n\n- check <thing> & act\n")
    client = TestClient(create_app(
        provider, token="t3st-token", runbooks_path=doc
    ))
    r = client.get("/runbooks?token=t3st-token")
    assert "id='my-incident-type'" in r.text
    assert "&lt;thing&gt; &amp; act" in r.text  # body is escaped, not interpreted

    gone = TestClient(create_app(
        provider, token="t3st-token", runbooks_path=tmp_path / "nope.md"
    ))
    assert gone.get("/runbooks?token=t3st-token").status_code == 404


def test_no_control_routes_exist(client):
    """Read-only invariant: nothing accepts POST/PUT/DELETE."""
    for method in ("post", "put", "delete"):
        for path in ("/state", "/kill", "/orders", "/config"):
            assert getattr(client, method)(f"{path}?token=t3st-token").status_code in (404, 405)


def test_snapshot_schema_from_wired_world(tmp_path):
    exchange = SimulatedExchange(FillModel(), 500.0)
    exchange.set_quote(SYM, bid=100.0, ask=100.0)
    exchange.submit_order(PaperOrderRequest("o1", SYM, True, 1.0))
    exchange.submit_order(
        PaperOrderRequest("o2", SYM, True, 1.0, order_type="limit", limit_price=99.0)
    )
    tracker = PortfolioTracker(exchange, 500.0)
    kill = KillSwitch(kill_file=tmp_path / "KILL")
    journal = DecisionJournal(tmp_path / "j.jsonl")
    om = OrderManager(PreTradeRiskGateway(RiskConfig(), kill), journal, PaperBroker(exchange))

    snap = build_snapshot(
        mode="paper", live_trading_enabled=False, tracker=tracker,
        exchange=exchange, kill_switch=kill, journal=journal,
        order_manager=om, feed_health=FeedHealth(exchange="test"),
    )
    for field in ("ts", "mode", "live_trading_enabled", "kill_switch_active",
                  "equity", "realized_pnl", "unrealized_pnl", "daily_pnl",
                  "consecutive_losses", "risk_status", "feed_health",
                  "positions", "open_orders", "recent_fills", "last_risk_reject",
                  "last_journal_write"):
        assert field in snap
    assert snap["risk_status"] == "ok"
    assert len(snap["positions"]) == 1
    assert snap["positions"][0]["side"] == "long"
    assert snap["positions"][0]["notional_usd"] == 100.0
    assert snap["open_orders"][0]["client_order_id"] == "o2"
    assert snap["open_orders"][0]["exchange_order_id"].startswith("pex_")
    assert "state_age_ms" in snap["open_orders"][0]
    assert snap["recent_fills"][0]["client_order_id"] == "o1"
    assert snap["recent_fills"][0]["notional_usd"] == pytest.approx(100.02)
    assert snap["recent_fills"][0]["side"] == "buy"

    kill.activate("test")
    snap2 = build_snapshot(
        mode="paper", live_trading_enabled=False, tracker=tracker,
        exchange=exchange, kill_switch=kill, journal=journal,
        order_manager=om, feed_health=FeedHealth(exchange="test"),
    )
    assert snap2["risk_status"] == "kill_switch_active"
    assert snap2["kill_switch_active"] is True


# ---------------------------------------------------------------------------
# Per-user auth: token store, roles, expiry, back-compat (auth.py)
# ---------------------------------------------------------------------------


def test_parse_users_env_roles_and_expiry():
    users = parse_users_env(
        "alice:tok-a:viewer;bob:tok-b:OPERATOR:2027-01-01T00:00:00+00:00"
    )
    assert [u.name for u in users] == ["alice", "bob"]
    assert users[0].role == "viewer" and users[0].expires_at is None
    assert users[1].role == "operator"  # role is case-insensitive
    assert users[1].expires_at is not None
    assert users[1].expires_at.tzinfo is not None
    assert users[1].expires_at.year == 2027


def test_parse_users_env_naive_expiry_assumed_utc():
    (user,) = parse_users_env("carol:tok-c:viewer:2027-06-01T12:00:00")
    assert user.expires_at == datetime(2027, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_parse_users_env_malformed_entries_skipped_loudly(caplog):
    raw = (
        "good:tok-good:viewer"          # valid
        ";too-short"                    # < 3 fields
        ";badrole:tok-role:admin"       # unknown role
        ";:tok-empty:viewer"            # empty name
        ";badexp:tok-exp:viewer:not-a-date"  # unparseable expiry
        ";good:tok-dupe:operator"       # duplicate name
        ";;"                            # blank entries ignored quietly
    )
    with caplog.at_level(logging.WARNING, logger="vnedge.dashboard.auth"):
        users = parse_users_env(raw)
    assert [u.name for u in users] == ["good"]
    skipped = [r for r in caplog.records if "skipped" in r.getMessage()]
    assert len(skipped) == 5  # every malformed entry is called out loudly
    # Token values must never appear in logs.
    logged = " ".join(r.getMessage() for r in caplog.records)
    for secret in ("tok-good", "tok-role", "tok-empty", "tok-exp", "tok-dupe"):
        assert secret not in logged


def test_token_store_from_env_back_compat_single_token():
    store = TokenStore.from_env({"DASHBOARD_TOKEN": "legacy-secret"})
    assert len(store) == 1
    result = store.authenticate("legacy-secret")
    assert result.authorized
    assert result.name == "operator" and result.role == "operator"
    assert result.expires_at is None
    assert not store.authenticate("wrong").authorized


def test_token_store_from_env_merges_users_and_legacy_token():
    store = TokenStore.from_env({
        "DASHBOARD_USERS": "alice:tok-a:viewer",
        "DASHBOARD_TOKEN": "legacy-secret",
    })
    assert len(store) == 2
    assert store.authenticate("tok-a").name == "alice"
    assert store.authenticate("legacy-secret").name == "operator"


def test_token_store_expired_token_rejected_with_reason(caplog):
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    store = TokenStore([DashboardUser("eve", "tok-e", "viewer", expires_at=past)])
    with caplog.at_level(logging.WARNING, logger="vnedge.dashboard.auth"):
        result = store.authenticate("tok-e")
    assert not result.authorized
    assert result.name == "eve"
    assert "expired" in (result.reason or "")
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "eve" in logged and "tok-e" not in logged


def test_token_store_future_expiry_still_valid():
    future = datetime.now(timezone.utc) + timedelta(days=30)
    store = TokenStore([DashboardUser("dan", "tok-d", "operator", expires_at=future)])
    result = store.authenticate("tok-d")
    assert result.authorized and result.name == "dan" and result.expires_at == future


def test_token_store_auth_events_logged_without_tokens(caplog):
    store = TokenStore([DashboardUser("alice", "tok-a", "viewer")])
    with caplog.at_level(logging.INFO, logger="vnedge.dashboard.auth"):
        assert store.authenticate("tok-a").authorized
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "alice" in logged and "viewer" in logged and "tok-a" not in logged


# ---------------------------------------------------------------------------
# Per-user auth wired into the app: identity header, expiry, WS
# ---------------------------------------------------------------------------


def _multi_user_client() -> TestClient:
    provider = SnapshotProvider()
    provider.publish({"mode": "shadow", "equity": 500.0})
    store = TokenStore([
        DashboardUser("alice", "tok-alice", "viewer"),
        DashboardUser("bob", "tok-bob", "operator"),
        DashboardUser(
            "expired-carl", "tok-carl", "viewer",
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        ),
    ])
    return TestClient(create_app(provider, token_store=store))


def test_multi_user_tokens_accepted_with_identity_header():
    client = _multi_user_client()
    r = client.get("/state", headers={"Authorization": "Bearer tok-alice"})
    assert r.status_code == 200
    assert r.headers["X-Dashboard-User"] == "alice"
    r2 = client.get("/state?token=tok-bob")
    assert r2.status_code == 200
    assert r2.headers["X-Dashboard-User"] == "bob"


def test_multi_user_wrong_token_rejected():
    client = _multi_user_client()
    r = client.get("/state?token=not-a-token")
    assert r.status_code == 401
    assert r.json()["detail"] == "missing or invalid token"
    assert "X-Dashboard-User" not in r.headers


def test_expired_token_rejected_with_clear_reason_over_http():
    client = _multi_user_client()
    r = client.get("/state?token=tok-carl")
    assert r.status_code == 401
    assert "expired" in r.json()["detail"]


def test_identity_header_on_all_data_routes():
    client = _multi_user_client()
    for path in (
        "/state",
        "/history",
        "/research",
        "/cost-model",
        "/alpha-council",
        "/alpha-workbench",
        "/lane-readiness",
        "/realtime-scanner",
        "/lane-firing-causality",
        "/paper-lane-activation",
        "/maker-quote-lifecycle",
        "/lane-survival",
        "/paper-lane-governor",
        "/paper-roster-drift",
    ):
        r = client.get(f"{path}?token=tok-alice")
        assert r.status_code in (200, 503), path
        assert r.headers["X-Dashboard-User"] == "alice", path


def test_back_compat_shared_token_is_operator_identity(client):
    r = client.get("/state?token=t3st-token")
    assert r.status_code == 200
    assert r.headers["X-Dashboard-User"] == "operator"


def test_websocket_multi_user_snapshot_carries_connection_count():
    client = _multi_user_client()
    with client.websocket_connect("/ws?token=tok-alice") as ws:
        payload = ws.receive_json()
        assert payload["equity"] == 500.0
        assert payload["dashboard_connections"] == 1


def test_websocket_expired_token_rejected():
    client = _multi_user_client()
    with pytest.raises(Exception):
        with client.websocket_connect("/ws?token=tok-carl") as ws:
            ws.receive_json()


def test_snapshot_marks_restored_position_at_entry_without_quote(tmp_path):
    """Regression (2026-07-07): a resumed session holds a restored position
    BEFORE the feed's first quote — build_snapshot must not KeyError (it
    killed both position-holding lanes); it marks at entry until data."""
    exchange = SimulatedExchange(FillModel(), 500.0)
    exchange.set_quote(SYM, bid=100.0, ask=100.0)
    exchange.submit_order(PaperOrderRequest("x1", SYM, False, 0.5))
    exchange.quotes.clear()  # simulate restart: position restored, no quote yet

    tracker = PortfolioTracker(exchange, 500.0)
    kill = KillSwitch(kill_file=tmp_path / "KILL")
    journal = DecisionJournal(tmp_path / "j.jsonl")
    om = OrderManager(PreTradeRiskGateway(RiskConfig(), kill), journal, PaperBroker(exchange))

    snap = build_snapshot(
        mode="paper", live_trading_enabled=False, tracker=tracker,
        exchange=exchange, kill_switch=kill, journal=journal,
        order_manager=om, feed_health=FeedHealth(exchange="test"),
    )
    pos = snap["positions"][0]
    assert pos["mark_price"] == pos["entry_price"]
    assert pos["unrealized_usd"] == 0.0


def test_health_is_unauthenticated_liveness(client):
    # /health must answer 200 WITHOUT a token — container healthchecks + the TLS
    # proxy hit it with no credentials. It reveals no state.
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
    # and it is a pure liveness probe, not an auth bypass into real data
    assert client.get("/state").status_code == 401
