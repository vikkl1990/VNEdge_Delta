from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import yaml

from vnedge.research.quant_loop_governance import (
    QuantLoopGovernanceConfig,
    publish_quant_loop_audit,
    run_quant_loop_audit,
)

NOW = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)


def _write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _write_gates(path: Path, *, max_runs: int = 96) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "promotion": {
                    "policy_version": "v1.2.0",
                    "burn_registry_required": True,
                    "verifier_required_before_paper": True,
                    "untouched_window_required": True,
                    "sample_min_trades": 20,
                    "min_net_bps": 25.0,
                    "min_profit_factor": 1.5,
                },
                "denylist_paths": [
                    "src/vnedge/risk/",
                    "src/vnedge/live/",
                    "src/vnedge/execution/",
                ],
                "loop_budgets": {
                    "quant_loop_governance": {"max_runs_per_day": max_runs},
                    "alpha_arena_lite": {"max_runs_per_day": 48},
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _alpha_payload(*, duplicate: bool = False) -> dict:
    base = {
        "arena_id": "alpha_arena_lite_v1",
        "generated_at": NOW.isoformat(),
        "summary": {
            "candidate_count": 2 if duplicate else 1,
            "sample_valid": 0,
            "ready_for_untouched_judgment": 0,
        },
        "scorecards": [
            {
                "candidate_id": "candidate_a",
                "strategy_id": "fvg_liquidity_breakout_v1",
                "exchange": "delta_india",
                "symbol": "ETH/USD:USD",
                "timeframes": ["5m"],
                "arena_verdict": "EXPAND_UNTOUCHED_SAMPLE",
                "untouched_window_plan": {
                    "status": "NEXT_UNTOUCHED_EXTENSION_REQUIRED",
                },
                "metrics": {"top_avg_net_bps": 25.5, "max_samples": 4},
            }
        ],
        "can_trade": False,
        "can_promote": False,
    }
    if duplicate:
        base["scorecards"].append(
            {
                "candidate_id": "candidate_b",
                "strategy_id": "fvg_liquidity_breakout_v1",
                "exchange": "delta_india",
                "symbol": "ETH/USD:USD",
                "timeframes": ["5m"],
                "arena_verdict": "EXPAND_UNTOUCHED_SAMPLE",
                "untouched_window_plan": {
                    "status": "NEXT_UNTOUCHED_EXTENSION_REQUIRED",
                },
                "metrics": {"top_avg_net_bps": 24.0, "max_samples": 3},
            }
        )
    return base


def _artifact_paths(tmp_path: Path, *, duplicate: bool = False) -> dict[str, Path]:
    gates = _write_gates(tmp_path / "governance" / "loop_gates.yaml")
    state = _write_json(
        tmp_path / "research" / "quant_loop_state.json",
        {
            "state_id": "quant_loop_state_v1",
            "generated_at": NOW.isoformat(),
            "can_trade": False,
            "can_promote": False,
        },
    )
    alpha = _write_json(tmp_path / "alpha.json", _alpha_payload(duplicate=duplicate))
    scanner = _write_json(
        tmp_path / "scanner_uplift.json",
        {
            "agent_id": "scanner_backtest_uplift_v1",
            "generated_at": NOW.isoformat(),
            "summary": {"evidence_rows": 42},
            "can_trade": False,
            "can_promote": False,
        },
    )
    progress = _write_json(
        tmp_path / "progress.json",
        {
            "truth_layer": "scanner_tournament_progress_v1",
            "status": "completed",
            "heartbeat_at": NOW.isoformat(),
            "total_work_units": 20,
            "completed_work_units": 20,
            "can_trade": False,
            "can_promote": False,
        },
    )
    gateway = _write_json(
        tmp_path / "gateway" / "snapshot.json",
        {
            "gateway_id": "quant_os_agent_gateway_v2",
            "generated_at": NOW.isoformat(),
            "tasks": [],
            "artifacts": {"recent": []},
        },
    )
    return {
        "gates": gates,
        "state": state,
        "alpha": alpha,
        "scanner": scanner,
        "progress": progress,
        "gateway": gateway,
        "run_log": tmp_path / "run_log.jsonl",
    }


def test_quant_loop_governance_reports_healthy_research_loop(tmp_path):
    paths = _artifact_paths(tmp_path)

    payload = run_quant_loop_audit(
        gates_path=paths["gates"],
        state_path=paths["state"],
        alpha_arena_path=paths["alpha"],
        scanner_uplift_path=paths["scanner"],
        scanner_progress_path=paths["progress"],
        gateway_snapshot_path=paths["gateway"],
        run_log_path=paths["run_log"],
        now=NOW,
    )

    assert payload["governance_id"] == "quant_loop_governance_v1"
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False
    assert payload["live_orders_enabled"] is False
    assert payload["summary"]["readiness_level"] == "L3_GOVERNED_RESEARCH_READY"
    assert payload["summary"]["collisions"] == 0
    assert payload["summary"]["alpha_candidates"] == 1
    assert {card["loop_id"] for card in payload["loop_cards"]} >= {
        "machine_readable_gates",
        "alpha_arena_lite",
        "scanner_backtest_uplift",
        "scanner_tournament_progress",
        "quant_os_agent_gateway",
    }
    assert all(row["status"] != "BLOCKED" for row in payload["gate_checks"])
    assert payload["policy"]["min_net_bps"] == 25.0
    assert "research" in payload["operator_answer"].lower()


def test_quant_loop_governance_blocks_duplicate_candidate_locks(tmp_path):
    paths = _artifact_paths(tmp_path, duplicate=True)

    payload = run_quant_loop_audit(
        gates_path=paths["gates"],
        state_path=paths["state"],
        alpha_arena_path=paths["alpha"],
        scanner_uplift_path=paths["scanner"],
        scanner_progress_path=paths["progress"],
        gateway_snapshot_path=paths["gateway"],
        run_log_path=paths["run_log"],
        now=NOW,
    )

    assert payload["summary"]["collisions"] == 1
    assert payload["collisions"][0]["candidate_ids"] == ["candidate_a", "candidate_b"]
    assert any(
        row["gate_id"] == "candidate_collision_control" and row["status"] == "BLOCKED"
        for row in payload["gate_checks"]
    )
    assert "collision" in payload["operator_answer"].lower()


def test_quant_loop_governance_surfaces_budget_alerts(tmp_path):
    paths = _artifact_paths(tmp_path)
    _write_gates(paths["gates"], max_runs=1)
    paths["run_log"].write_text(
        json.dumps(
            {
                "pattern": "quant_loop_governance",
                "generated_at": NOW.isoformat(),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    payload = run_quant_loop_audit(
        gates_path=paths["gates"],
        state_path=paths["state"],
        alpha_arena_path=paths["alpha"],
        scanner_uplift_path=paths["scanner"],
        scanner_progress_path=paths["progress"],
        gateway_snapshot_path=paths["gateway"],
        run_log_path=paths["run_log"],
        now=NOW,
    )

    assert payload["summary"]["budget_alerts"] == 1
    assert payload["budget_alerts"][0]["loop_id"] == "quant_loop_governance"
    assert any(
        row["gate_id"] == "loop_budget_control" and row["status"] == "BLOCKED"
        for row in payload["gate_checks"]
    )


def test_publish_quant_loop_governance_writes_latest_feed_and_run_log(tmp_path):
    paths = _artifact_paths(tmp_path)
    payload = run_quant_loop_audit(
        gates_path=paths["gates"],
        state_path=paths["state"],
        alpha_arena_path=paths["alpha"],
        scanner_uplift_path=paths["scanner"],
        scanner_progress_path=paths["progress"],
        gateway_snapshot_path=paths["gateway"],
        run_log_path=paths["run_log"],
        now=NOW,
    )
    out = tmp_path / "latest.json"
    feed = tmp_path / "feed.jsonl"

    publish_quant_loop_audit(payload, out=out, feed=feed, run_log=paths["run_log"])

    latest = json.loads(out.read_text(encoding="utf-8"))
    feed_row = json.loads(feed.read_text(encoding="utf-8").splitlines()[-1])
    run_row = json.loads(paths["run_log"].read_text(encoding="utf-8").splitlines()[-1])
    assert latest["governance_id"] == "quant_loop_governance_v1"
    assert feed_row["governance_id"] == "quant_loop_governance_v1"
    assert run_row["pattern"] == "quant_loop_governance"
    assert run_row["can_trade"] is False
    assert run_row["can_promote"] is False


def test_quant_loop_governance_marks_stale_artifacts(tmp_path):
    paths = _artifact_paths(tmp_path)
    old = datetime(2026, 7, 22, 0, 0, tzinfo=UTC).isoformat()
    _write_json(paths["progress"], {"status": "running", "heartbeat_at": old})

    payload = run_quant_loop_audit(
        gates_path=paths["gates"],
        state_path=paths["state"],
        alpha_arena_path=paths["alpha"],
        scanner_uplift_path=paths["scanner"],
        scanner_progress_path=paths["progress"],
        gateway_snapshot_path=paths["gateway"],
        run_log_path=paths["run_log"],
        config=QuantLoopGovernanceConfig(max_progress_age_minutes=5),
        now=NOW,
    )

    progress_card = next(
        row for row in payload["loop_cards"] if row["loop_id"] == "scanner_tournament_progress"
    )
    assert progress_card["status"] == "STALE"
    assert payload["summary"]["loops_stale"] == 1
