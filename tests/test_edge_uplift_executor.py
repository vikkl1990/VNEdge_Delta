"""Edge uplift executor turns agent ideas into research-only tasks."""

import json
import stat
from datetime import UTC, datetime

from vnedge.research.edge_uplift_executor import (
    causal_port_pack_v1,
    main,
    publish_edge_uplift_executor,
    run_edge_uplift_executor,
)


def _write_inputs(tmp_path):
    uplift = tmp_path / "pine_edge_uplift_agent_latest.json"
    scanner = tmp_path / "scanner_tournament_latest.json"
    uplift.write_text(json.dumps({
        "agent_id": "pine_edge_uplift_agent_v1",
        "summary": {"experiments": 3},
        "experiments": [
            {
                "experiment_id": "exec_fvg",
                "experiment_type": "execution_filtered_replay",
                "recommended_port": "fvg_liquidity_breakout_v1",
                "primitive_stack": ["liquidity_zone", "sweep_reclaim", "fvg_displacement"],
                "source_script_ids": ["fvg_a", "fvg_b"],
                "source_titles": ["FVG A", "FVG B"],
                "failed_cells": 12,
                "positive_cells": 1,
                "best_avg_net_bps": -3.2,
                "best_profit_factor": 0.93,
                "salvage_score": 84,
            },
            {
                "experiment_id": "judge_range",
                "experiment_type": "untouched_judgment_candidate",
                "recommended_port": "range_expansion_breakout_v1",
                "primitive_stack": ["range_breakout", "volume_participation"],
                "source_script_ids": ["range_a"],
                "source_titles": ["Range A"],
                "failed_cells": 0,
                "positive_cells": 4,
                "best_avg_net_bps": 31.4,
                "best_profit_factor": 1.7,
                "salvage_score": 91,
            },
            {
                "experiment_id": "feature_bank",
                "experiment_type": "edge_model_feature_bank",
                "recommended_port": "trend_momentum_context_v1",
                "primitive_stack": ["bbp_histogram", "momentum_confirm"],
                "source_script_ids": ["bbp_a"],
                "source_titles": ["BBP A"],
                "failed_cells": 20,
                "positive_cells": 0,
                "best_avg_net_bps": -18.0,
                "best_profit_factor": 0.61,
                "salvage_score": 67,
            },
        ],
        "can_trade": False,
        "can_promote": False,
    }))
    scanner.write_text(json.dumps({
        "truth_layer": "scanner_tournament_v1",
        "summary": {
            "positive_watchlists": 2,
            "strict_watchlists": 1,
            "can_trade": False,
            "can_promote": False,
        },
        "candidates": [
            {
                "rank": 1,
                "candidate_id": "smc_playbook_scalper_v1__delta__ETHUSD__5m",
                "verdict": "DISCOVERY_WATCHLIST",
                "recommended_action": "KEEP_RELAXED_RESEARCH_ON_AND_TRAIN_EDGE_MODEL",
                "score": 42.0,
                "exchange": "delta_india",
                "symbol": "ETH/USD:USD",
                "timeframe": "5m",
                "strategy_id": "smc_playbook_scalper_v1",
                "routed": 16,
                "avg_selected_net_bps": 7.5,
                "profit_factor": 1.21,
                "dominant_route": "MAKER",
                "strict_watchlist": False,
            },
            {
                "rank": 2,
                "candidate_id": "luxara_live_plan_qtm_v1__bybit__SOLUSDT__15m",
                "verdict": "STRICT_PROOF_WATCHLIST",
                "recommended_action": "PRE_REGISTER_UNTOUCHED_JUDGMENT_WINDOW",
                "score": 80.0,
                "exchange": "bybit",
                "symbol": "SOL/USDT:USDT",
                "timeframe": "15m",
                "strategy_id": "luxara_live_plan_qtm_v1",
                "routed": 24,
                "avg_selected_net_bps": 33.1,
                "profit_factor": 1.83,
                "dominant_route": "MAKER_THEN_TAKER",
                "strict_watchlist": True,
            },
        ],
        "can_trade": False,
        "can_promote": False,
    }))
    return uplift, scanner


def test_causal_port_pack_has_scalper_gates_and_routes():
    ports = causal_port_pack_v1()

    fvg = ports["fvg_liquidity_breakout_v1"]
    assert fvg.pass_gates["expected_net_edge_bps_gt"] == 25.0
    assert fvg.pass_gates["profit_factor_gt"] == 1.5
    assert fvg.pass_gates["min_historical_trades"] == 20
    assert "delta_india" in fvg.venues
    assert "5m" in fvg.trigger_timeframes
    assert any("taker allowed only" in rule for rule in fvg.execution_rules)
    assert "auto_trade" in fvg.blocked_actions


def test_edge_uplift_executor_builds_research_only_queue(tmp_path):
    uplift, scanner = _write_inputs(tmp_path)

    payload = run_edge_uplift_executor(
        uplift_path=uplift,
        scanner_path=scanner,
        fee_wall_path=None,
        now=datetime(2026, 7, 19, tzinfo=UTC),
    )

    assert payload["executor_id"] == "edge_uplift_executor_v1"
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False
    assert payload["policy"]["blocked_actions"] == [
        "auto_trade",
        "auto_promote",
        "copy_protected_pine_source",
        "relax_live_risk_gates",
        "use_seen_data_for_judgment",
    ]
    assert payload["summary"]["tasks_total"] == 3
    assert payload["summary"]["ready_for_replay"] == 1
    assert payload["summary"]["ready_for_untouched_judgment"] == 1
    assert payload["summary"]["feature_bank_only"] == 1

    by_experiment = {row["experiment_id"]: row for row in payload["tasks"]}
    assert by_experiment["exec_fvg"]["status"] == "READY_FOR_REPLAY"
    assert by_experiment["exec_fvg"]["scanner_support"]["state"] == "DISCOVERY_WATCHLIST"
    assert by_experiment["judge_range"]["status"] == "READY_FOR_UNTOUCHED_JUDGMENT"
    assert by_experiment["judge_range"]["scanner_support"]["state"] == "STRICT_WATCHLIST"
    assert by_experiment["feature_bank"]["status"] == "FEATURE_BANK_ONLY"
    assert all(row["can_trade"] is False for row in payload["tasks"])
    assert all(row["can_promote"] is False for row in payload["tasks"])


def test_publish_edge_uplift_executor_is_feed_safe_and_readable(tmp_path):
    uplift, scanner = _write_inputs(tmp_path)
    payload = run_edge_uplift_executor(
        uplift_path=uplift,
        scanner_path=scanner,
        fee_wall_path=None,
    )
    out = tmp_path / "edge_uplift_experiments_latest.json"
    feed = tmp_path / "edge_uplift_experiments_feed.jsonl"

    publish_edge_uplift_executor(payload, out=out, feed=feed)
    publish_edge_uplift_executor(payload, out=out, feed=feed)

    saved = json.loads(out.read_text())
    rows = [json.loads(line) for line in feed.read_text().splitlines()]
    assert saved["summary"]["tasks_total"] == 3
    assert rows[-1]["executor_id"] == "edge_uplift_executor_v1"
    assert rows[-1]["can_trade"] is False
    assert stat.S_IMODE(out.stat().st_mode) == 0o644
    assert stat.S_IMODE(feed.stat().st_mode) == 0o644


def test_edge_uplift_executor_cli_writes_artifact(tmp_path, capsys):
    uplift, scanner = _write_inputs(tmp_path)
    out = tmp_path / "executor.json"
    feed = tmp_path / "executor.jsonl"

    assert main([
        "--uplift",
        str(uplift),
        "--scanner",
        str(scanner),
        "--fee-wall",
        str(tmp_path / "missing_fee_wall.json"),
        "--out",
        str(out),
        "--feed",
        str(feed),
    ]) == 0

    printed = capsys.readouterr().out
    payload = json.loads(out.read_text())
    assert "edge uplift executor" in printed
    assert payload["summary"]["ready_for_replay"] == 1


def test_edge_uplift_executor_uses_fee_wall_support_when_scanner_is_missing(tmp_path):
    uplift = tmp_path / "pine_edge_uplift_agent_latest.json"
    scanner = tmp_path / "missing_scanner.json"
    fee_wall = tmp_path / "fee_wall_forensics_latest.json"
    uplift.write_text(json.dumps({
        "agent_id": "pine_edge_uplift_agent_v1",
        "summary": {"experiments": 1},
        "experiments": [
            {
                "experiment_id": "trail_replay",
                "experiment_type": "execution_filtered_replay",
                "recommended_port": "trail_exit_lab_v1",
                "primitive_stack": ["trend_trail", "momentum_confirm"],
                "source_script_ids": ["luxy"],
                "source_titles": ["Luxy"],
                "failed_cells": 4,
                "positive_cells": 2,
                "best_avg_net_bps": 13.5,
                "best_profit_factor": 1.72,
                "salvage_score": 88,
            }
        ],
        "can_trade": False,
        "can_promote": False,
    }))
    fee_wall.write_text(json.dumps({
        "strict_fee_wall_candidates": [
            {
                "exchange": "binanceusdm",
                "symbol": "BTC/USDT:USDT",
                "timeframe": "15m",
                "strategy": "luxy_ut_bot_forecast_v1",
                "verdict": "MIXED_ROUTE_EDGE",
                "recommended_action": "PRE_REGISTER_UNTOUCHED_JUDGMENT_WINDOW",
                "routed": 16,
                "avg_selected_net_bps": 13.54,
                "profit_factor": 1.73,
            }
        ],
    }))

    payload = run_edge_uplift_executor(
        uplift_path=uplift,
        scanner_path=scanner,
        fee_wall_path=fee_wall,
        now=datetime(2026, 7, 20, tzinfo=UTC),
    )

    task = payload["tasks"][0]
    assert task["status"] == "READY_FOR_REPLAY"
    assert task["scanner_support"]["state"] == "DISCOVERY_WATCHLIST"
    assert task["candidate_matches"][0]["evidence_source"] == "fee_wall_forensics_latest.json"
    assert task["candidate_matches"][0]["strategy_id"] == "luxy_ut_bot_forecast_v1"


def test_edge_uplift_executor_recognizes_registered_confluence_ports(tmp_path):
    uplift = tmp_path / "pine_edge_uplift_agent_latest.json"
    scanner = tmp_path / "scanner_tournament_latest.json"
    uplift.write_text(json.dumps({
        "agent_id": "pine_edge_uplift_agent_v1",
        "summary": {"experiments": 2},
        "experiments": [
            {
                "experiment_id": "range_confluence",
                "experiment_type": "confluence_stack_replay",
                "recommended_port": "range_expansion_breakout_v1",
                "primitive_stack": ["range_breakout", "volume_participation"],
                "source_script_ids": ["luxara"],
                "source_titles": ["Luxara"],
                "failed_cells": 9,
                "positive_cells": 5,
                "best_avg_net_bps": 15.2,
                "best_profit_factor": 1.34,
                "salvage_score": 92,
            },
            {
                "experiment_id": "trend_context",
                "experiment_type": "context_feature_bank",
                "recommended_port": "trend_momentum_context_v1",
                "primitive_stack": ["bbp_histogram", "adx_er_quality"],
                "source_script_ids": ["bbp"],
                "source_titles": ["BBP"],
                "failed_cells": 4,
                "positive_cells": 3,
                "best_avg_net_bps": 6.0,
                "best_profit_factor": 1.1,
                "salvage_score": 80,
            },
        ],
        "can_trade": False,
        "can_promote": False,
    }))
    scanner.write_text(json.dumps({"candidates": []}))

    payload = run_edge_uplift_executor(
        uplift_path=uplift,
        scanner_path=scanner,
        fee_wall_path=None,
        now=datetime(2026, 7, 20, tzinfo=UTC),
    )

    by_id = {row["experiment_id"]: row for row in payload["tasks"]}
    assert by_id["range_confluence"]["status"] == "READY_FOR_REPLAY"
    assert by_id["range_confluence"]["port_implementation"]["state"] == "VNEDGE_PORT_REGISTERED"
    assert "luxara_live_plan_qtm_v1" in by_id["range_confluence"]["port_implementation"]["registered_strategy_aliases"]
    assert by_id["trend_context"]["status"] == "FEATURE_BANK_ONLY"
    assert by_id["trend_context"]["port_implementation"]["state"] == "VNEDGE_PORT_REGISTERED"
    assert payload["summary"]["ready_for_replay"] == 1
    assert payload["summary"]["feature_bank_only"] == 1
