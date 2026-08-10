"""Alpha Arena Lite scorecards and Quant OS task sync."""

from datetime import UTC, datetime
import json

from vnedge.agent_gateway.task_registry import QuantOSAgentGateway
from vnedge.research.alpha_arena_lite import (
    AlphaArenaGateConfig,
    publish_alpha_arena_lite,
    run_alpha_arena_lite,
)


def _sparse_uplift_payload() -> dict:
    row_id = "luxara_live_plan_qtm_v1|delta_india|ETHUSDUSD|4h|MAKER"
    return {
        "agent_id": "scanner_backtest_uplift_v1",
        "summary": {"experiments": 1},
        "top_uplifts": [
            {
                "rank": 1,
                "row_id": row_id,
                "failure_mode": "SPARSE_POSITIVE",
                "exchange": "delta_india",
                "symbol": "ETH/USD:USD",
                "timeframe": "4h",
                "strategy_id": "luxara_live_plan_qtm_v1",
                "mode": "MAKER",
                "samples": 3,
                "avg_net_bps": 497.8282,
                "visual_avg_bps": 510.0,
                "profit_factor": 999.0,
                "win_rate_pct": 100.0,
                "required_uplift_bps": 0.0,
                "fee_drag_bps": 12.1718,
                "uplift_action": "EXTEND_SAMPLE_ON_NEXT_UNTOUCHED_WINDOW",
            }
        ],
        "experiments": [
            {
                "experiment_id": "sample_expansion|delta_india|ETH/USD:USD|luxara_live_plan_qtm_v1",
                "priority": 1,
                "experiment_type": "sample_expansion",
                "target_rows": [row_id],
                "exchange": "delta_india",
                "symbol": "ETH/USD:USD",
                "timeframes": ["4h"],
                "strategy_id": "luxara_live_plan_qtm_v1",
                "hypothesis": "sparse ETH structure may persist",
                "required_change": "Run the same frozen setup on untouched data.",
                "expected_effect": "Separate sparse signal from luck.",
                "guardrails": ["research-only output; no paper/live promotion"],
            }
        ],
        "can_trade": False,
        "can_promote": False,
    }


def _scanner_payload() -> dict:
    return {
        "truth_layer": "scanner_tournament_v1",
        "candidates": [
            {
                "strategy_id": "luxara_live_plan_qtm_v1",
                "exchange": "delta_india",
                "symbol": "ETH/USD:USD",
                "timeframe": "4h",
                "opportunities": 40,
                "routed": 3,
                "avg_mfe_bps": 640.0,
                "avg_mae_bps": -42.0,
            }
        ],
    }


def _strict_uplift_payload() -> dict:
    row_id = "luxara_live_plan_qtm_v1|bybit|SOLUSDTUSDT|15m|MAKER"
    return {
        "agent_id": "scanner_backtest_uplift_v1",
        "summary": {"promotable_proof_candidates": 1, "experiments": 0},
        "top_uplifts": [
            {
                "rank": 1,
                "row_id": row_id,
                "failure_mode": "PROMOTABLE_PROOF_CANDIDATE",
                "exchange": "bybit",
                "symbol": "SOL/USDT:USDT",
                "timeframe": "15m",
                "strategy_id": "luxara_live_plan_qtm_v1",
                "mode": "MAKER",
                "samples": 31,
                "avg_net_bps": 31.3993,
                "visual_avg_bps": 43.8,
                "profit_factor": 2.0975,
                "win_rate_pct": 61.3,
                "required_uplift_bps": 0.0,
                "fee_drag_bps": 12.5,
                "uplift_action": "PRE_REGISTER_UNTOUCHED_JUDGMENT",
            }
        ],
        "experiments": [],
        "can_trade": False,
        "can_promote": False,
    }


def _strict_fee_wall_payload() -> dict:
    return {
        "truth_layer": "fee_wall_forensics_v1",
        "strict_fee_wall_candidates": [
            {
                "exchange": "bybit",
                "symbol": "SOL/USDT:USDT",
                "timeframe": "15m",
                "strategy": "luxara_live_plan_qtm_v1",
                "routed": 31,
                "avg_selected_net_bps": 31.3993,
                "profit_factor": 2.0975,
                "win_rate_pct": 61.3,
                "recommended_action": "PRE_REGISTER_UNTOUCHED_JUDGMENT_WINDOW",
            }
        ],
    }


def test_sparse_positive_becomes_durable_research_task_not_paper(tmp_path):
    payload = run_alpha_arena_lite(
        uplift_payload=_sparse_uplift_payload(),
        scanner_payload=_scanner_payload(),
        gateway_dir=tmp_path / "quant_os",
        now=datetime(2026, 7, 23, tzinfo=UTC),
    )

    assert payload["arena_id"] == "alpha_arena_lite_v1"
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False
    assert payload["summary"]["candidate_count"] == 1
    assert payload["summary"]["sparse_positive"] == 1
    assert payload["summary"]["sample_valid"] == 0
    assert payload["summary"]["task_count"] == 1
    assert payload["gateway"]["tasks_created"] == 1
    assert payload["gateway"]["artifacts_registered"] == 1

    card = payload["scorecards"][0]
    assert card["arena_verdict"] == "EXPAND_UNTOUCHED_SAMPLE"
    assert card["metrics"]["top_avg_net_bps"] == 497.8282
    assert card["metrics"]["sample_gap"] == 17
    assert card["metrics"]["pf_is_sparse_synthetic"] is True
    assert card["metrics"]["scanner_avg_mfe_bps"] == 640.0
    assert card["execution_plan"]["paper_ready"] is False
    assert card["task_id"].startswith("qtask_")

    snapshot = QuantOSAgentGateway(tmp_path / "quant_os").snapshot(limit=10)
    assert snapshot["summary"]["total_tasks"] == 1
    assert snapshot["summary"]["artifacts"] == 1
    assert snapshot["tasks"][0]["payload"]["alpha_arena_lite"]["candidate_id"] == card["candidate_id"]


def test_strict_candidate_becomes_canonical_untouched_judgment_task(tmp_path):
    payload = run_alpha_arena_lite(
        uplift_payload=_strict_uplift_payload(),
        scanner_payload={},
        fee_wall_payload=_strict_fee_wall_payload(),
        gateway_dir=tmp_path / "quant_os",
        now=datetime(2026, 7, 24, tzinfo=UTC),
    )

    assert payload["summary"]["candidate_count"] == 1
    assert payload["summary"]["ready_for_untouched_judgment"] == 1
    assert payload["summary"]["sparse_positive"] == 0
    assert payload["summary"]["task_count"] == 1

    card = payload["scorecards"][0]
    assert card["candidate_id"] == "strict_judgment|bybit|SOLUSDTUSDT|15m|luxara_live_plan_qtm_v1"
    assert card["task_kind"] == "alpha_arena_lite.untouched_judgment"
    assert card["arena_verdict"] == "PRE_REGISTER_UNTOUCHED_JUDGMENT"
    assert card["next_action"] == "ASK_OPERATOR_TO_APPROVE_ONE_SHOT_UNTOUCHED_JUDGMENT"
    assert card["metrics"]["source_evidence_count"] == 2
    assert card["metrics"]["max_samples"] == 31
    assert card["gate_checks"]["sample_valid"] is True
    assert card["untouched_window_plan"]["status"] == "READY_FOR_OPERATOR_PRE_REGISTRATION"
    assert card["execution_plan"]["paper_ready"] is False
    assert card["can_trade"] is False


def test_gateway_sync_is_idempotent_for_unchanged_scorecards(tmp_path):
    first = run_alpha_arena_lite(
        uplift_payload=_sparse_uplift_payload(),
        scanner_payload=_scanner_payload(),
        gateway_dir=tmp_path / "quant_os",
    )
    second = run_alpha_arena_lite(
        uplift_payload=_sparse_uplift_payload(),
        scanner_payload=_scanner_payload(),
        gateway_dir=tmp_path / "quant_os",
    )

    assert first["gateway"]["tasks_created"] == 1
    assert second["gateway"]["tasks_created"] == 0
    assert second["gateway"]["tasks_reused"] == 1
    assert second["gateway"]["artifacts_registered"] == 0
    assert second["gateway"]["artifacts_skipped"] == 1
    snapshot = QuantOSAgentGateway(tmp_path / "quant_os").snapshot(limit=10)
    assert snapshot["summary"]["total_tasks"] == 1
    assert snapshot["summary"]["artifacts"] == 1


def test_promotable_mode_is_still_only_untouched_judgment_ready(tmp_path):
    payload = _sparse_uplift_payload()
    payload["top_uplifts"][0].update(
        {
            "failure_mode": "PROMOTABLE_PROOF_CANDIDATE",
            "samples": 31,
            "avg_net_bps": 38.0,
            "profit_factor": 1.72,
        }
    )

    report = run_alpha_arena_lite(
        uplift_payload=payload,
        scanner_payload={},
        gateway_dir=tmp_path / "quant_os",
        sync_gateway=False,
        config=AlphaArenaGateConfig(min_net_bps=25.0, min_profit_factor=1.5, min_trades=20),
    )

    card = report["scorecards"][0]
    assert card["arena_verdict"] == "PRE_REGISTER_UNTOUCHED_JUDGMENT"
    assert card["gate_checks"]["sample_valid"] is True
    assert card["gate_checks"]["fee_wall_valid"] is True
    assert card["gate_checks"]["profit_factor_valid"] is True
    assert card["can_trade"] is False
    assert report["summary"]["ready_for_untouched_judgment"] == 1


def test_publish_alpha_arena_lite_writes_valid_json_and_feed(tmp_path):
    payload = run_alpha_arena_lite(
        uplift_payload=_sparse_uplift_payload(),
        scanner_payload={},
        gateway_dir=tmp_path / "quant_os",
        sync_gateway=False,
    )

    out = tmp_path / "alpha_arena_lite_latest.json"
    feed = tmp_path / "alpha_arena_lite_feed.jsonl"
    publish_alpha_arena_lite(payload, out=out, feed=feed)

    assert json.loads(out.read_text())["arena_id"] == "alpha_arena_lite_v1"
    assert json.loads(feed.read_text().splitlines()[0])["can_trade"] is False
