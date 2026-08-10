from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from vnedge.research.agentic_research_os import (
    AGENTIC_RESEARCH_OS_ID,
    AgenticResearchOSConfig,
    build_agentic_research_os_from_files,
    publish_agentic_research_os,
    run_agentic_research_os,
)


NOW = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)


def test_agentic_research_os_ranks_verifier_retire_and_stale_task_actions():
    old = (NOW - timedelta(hours=4)).isoformat()
    vibe = {
        "generated_at": NOW.isoformat(),
        "summary": {"hypotheses": 3, "active": 1, "monitoring": 0, "decayed": 1, "disabled": 1},
        "cards": [
            {
                "hypothesis_id": "h_verify",
                "candidate_id": "c_fvg",
                "family": "fvg_liquidity_breakout_v1",
                "exchange": "delta_india",
                "symbol": "ETH/USD:USD",
                "timeframe": "5m",
                "lifecycle_state": "ACTIVE",
                "vetoes": ["requires_untouched_judgment"],
                "blocked_by": ["untouched_window"],
                "next_action": "PRE_REGISTER_UNTOUCHED_JUDGMENT",
            },
            {
                "hypothesis_id": "h_decay",
                "candidate_id": "c_leadlag",
                "family": "leadlag_delta_follower_v1",
                "exchange": "bybit",
                "symbol": "XRP/USDT:USDT",
                "timeframe": "15m",
                "lifecycle_state": "DECAYED",
                "times_seen": 4,
                "decay_score": 75,
                "vetoes": ["negative_edge_after_cost"],
            },
        ],
    }
    gateway = {
        "generated_at": NOW.isoformat(),
        "summary": {"total_tasks": 1, "active": 1},
        "artifacts": {"recent": []},
        "tasks": [
            {
                "task_id": "qtask_stale",
                "kind": "alpha_arena.experiment",
                "objective": "stale proof task",
                "status": "QUEUED_RESEARCH_ONLY",
                "updated_at": old,
                "target": {"exchange": "delta_india", "symbol": "ETH/USD:USD", "timeframe": "5m"},
                "payload": {"family": "fvg_liquidity_breakout_v1"},
            }
        ],
    }
    arena = {
        "generated_at": NOW.isoformat(),
        "summary": {"candidate_count": 2},
        "scorecards": [
            {
                "candidate_id": "a_sparse",
                "strategy_id": "luxara_live_plan_qtm_v1",
                "exchange": "delta_india",
                "symbol": "ETH/USD:USD",
                "timeframes": ["5m"],
                "arena_verdict": "EXPAND_UNTOUCHED_SAMPLE",
                "metrics": {"top_avg_net_bps": 497.83, "best_profit_factor": 999.0},
            },
            {
                "candidate_id": "a_salvage",
                "strategy_id": "sats_5m_scalper_v1",
                "exchange": "bybit",
                "symbol": "BTC/USDT:USDT",
                "timeframes": ["5m"],
                "arena_verdict": "EXECUTION_SALVAGE_REQUIRED",
                "metrics": {"top_avg_net_bps": -1.2, "required_uplift_bps": 26.2},
            },
        ],
    }
    quant_loop = {
        "generated_at": NOW.isoformat(),
        "summary": {"readiness_level": "L3_GOVERNED_RESEARCH_READY"},
        "gate_checks": [
            {"gate_id": "stale_loop", "status": "WARN", "detail": "loop heartbeat late"}
        ],
    }
    paper = {
        "generated_at": NOW.isoformat(),
        "summary": {"active_negative": 1},
        "rows": [
            {
                "lane_id": "paper_bad",
                "state": "PAPER_ACTIVE_NEGATIVE",
                "strategy_id": "fvg_liquidity_breakout_v1",
                "exchange": "delta_india",
                "symbol": "ETH/USD:USD",
                "timeframe": "5m",
                "net_pnl_usd": -2.2,
                "closed_trades": 4,
            }
        ],
    }

    payload = run_agentic_research_os(
        vibe_payload=vibe,
        alpha_arena_payload=arena,
        gateway_snapshot=gateway,
        quant_loop_payload=quant_loop,
        paper_performance_payload=paper,
        now=NOW,
    )

    assert payload["os_id"] == AGENTIC_RESEARCH_OS_ID
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False
    assert payload["live_orders_enabled"] is False
    assert payload["summary"]["operator_actions"] >= 6
    assert payload["summary"]["critical_actions"] >= 1
    actions = {row["action"] for row in payload["operator_queue"]}
    assert "RETIRE_HYPOTHESIS" in actions
    assert "REQUEST_UNTOUCHED_VERIFIER" in actions
    assert "RECLAIM_OR_FAIL_STALE_TASK" in actions
    assert "EXPAND_SAMPLE_ON_NEXT_UNTOUCHED_WINDOW" in actions
    assert "RUN_EXECUTION_SALVAGE_BEFORE_MORE_ENTRIES" in actions
    assert "DECAY_OR_REPAIR_PAPER_LANE" in actions
    assert payload["operator_queue"][0]["severity"] == "critical"
    assert all(row["can_trade"] is False for row in payload["operator_queue"])
    assert all(row["can_promote"] is False for row in payload["agent_scorecards"])
    assert {row["source"] for row in payload["source_status"]} == {
        "vibe_intelligence",
        "alpha_arena_lite",
        "quant_os_agent_gateway",
        "quant_loop_governance",
        "paper_lane_performance",
    }


def test_agentic_research_os_tolerates_missing_and_malformed_inputs():
    payload = run_agentic_research_os(
        gateway_snapshot={"generated_at": NOW.isoformat(), "artifacts": []},
        now=NOW,
    )

    assert payload["summary"]["operator_actions"] == 0
    assert payload["policy"]["orders_allowed"] is False
    assert payload["source_status"][0]["state"] == "MISSING"


def test_agentic_research_os_builds_from_files_and_publishes_latest_and_feed(tmp_path):
    vibe = tmp_path / "vibe.json"
    arena = tmp_path / "arena.json"
    gateway = tmp_path / "gateway.json"
    quant_loop = tmp_path / "loop.json"
    paper = tmp_path / "paper.json"
    out = tmp_path / "latest.json"
    feed = tmp_path / "feed.jsonl"
    vibe.write_text(
        json.dumps(
            {
                "generated_at": NOW.isoformat(),
                "summary": {"hypotheses": 1, "active": 1},
                "cards": [
                    {
                        "hypothesis_id": "h1",
                        "family": "fvg_liquidity_breakout_v1",
                        "lifecycle_state": "ACTIVE",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    arena.write_text(json.dumps({"generated_at": NOW.isoformat(), "summary": {}}), encoding="utf-8")
    gateway.write_text(
        json.dumps({"generated_at": NOW.isoformat(), "summary": {}}), encoding="utf-8"
    )
    quant_loop.write_text(
        json.dumps({"generated_at": NOW.isoformat(), "summary": {}}), encoding="utf-8"
    )
    paper.write_text(json.dumps({"generated_at": NOW.isoformat(), "summary": {}}), encoding="utf-8")

    payload = build_agentic_research_os_from_files(
        vibe_path=vibe,
        alpha_arena_path=arena,
        gateway_snapshot_path=gateway,
        quant_loop_path=quant_loop,
        paper_performance_path=paper,
        now=NOW,
        config=AgenticResearchOSConfig(max_actions=5),
    )
    written = publish_agentic_research_os(payload, out=out, feed=feed)

    assert written == out
    latest = json.loads(out.read_text(encoding="utf-8"))
    feed_rows = [json.loads(line) for line in feed.read_text(encoding="utf-8").splitlines()]
    assert latest["os_id"] == AGENTIC_RESEARCH_OS_ID
    assert latest["summary"]["operator_actions"] == 1
    assert feed_rows[-1]["os_id"] == AGENTIC_RESEARCH_OS_ID
    assert feed_rows[-1]["can_trade"] is False
