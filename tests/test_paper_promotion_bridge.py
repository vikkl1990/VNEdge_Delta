"""Paper promotion bridge tests."""

import json

from vnedge.research.paper_promotion_bridge import (
    DECISION_MINE_ALPHA,
    DECISION_PAPER_REVIEW_READY,
    DECISION_REPAIR_FIRST,
    PaperPromotionBridgeConfig,
    build_paper_promotion_bridge,
    publish_paper_promotion_bridge,
)


LANE = {
    "strategy_id": "vnedge_algo_ml_pro_v1",
    "exchange": "delta_india",
    "symbol": "ETH/USD",
    "timeframe": "5m",
}


def _readiness(status="PAPER_ACTIVE"):
    return {
        "report_id": "lane_promotion_readiness_v1",
        "rows": [{**LANE, "status": status, "next_action": "observe paper lane"}],
    }


def _performance(state="PAPER_PROMOTION_CANDIDATE", closed=25, pf=1.8, net=42.0):
    return {
        "report_id": "paper_lane_performance_v1",
        "rows": [
            {
                **LANE,
                "lane_id": "vnedge_ml_eth_delta",
                "state": state,
                "closed_trades": closed,
                "profit_factor": pf,
                "net_pnl_usd": net,
                "next_action": "review paper evidence",
            }
        ],
    }


def _contract(verdict="CONTRACT_OK_PROFITABLE", closed=25, avg=37.5, net=42.0):
    return {
        "report_id": "paper_trade_contract_reconciler_v1",
        "rows": [
            {
                **LANE,
                "lane_id": "vnedge_ml_eth_delta",
                "verdict": verdict,
                "closed_trades": closed,
                "net_pnl_usd": net,
                "avg_net_bps": avg,
                "avg_fee_bps": 8.0,
                "critical_violations": 0,
                "fee_wall_breaches": 0,
                "next_action": "contract clean",
            }
        ],
    }


def _quote(state="QUOTE_LIFECYCLE_PAPER_REVIEW"):
    return {
        "report_id": "maker_quote_lifecycle_v1",
        "rows": [
            {
                **LANE,
                "lane_id": "vnedge_ml_eth_delta",
                "lifecycle_state": state,
                "maker_attempts": 12,
                "maker_fill_rate_pct": 41.2,
                "avg_net_bps": 37.5,
                "next_action": "human review before any promotion",
            }
        ],
    }


def _actions(bucket="REVIEW_PAPER_CANDIDATE"):
    return {
        "report_id": "operator_actions_v1",
        "rows": [
            {
                **LANE,
                "lane_key": "vnedge_algo_ml_pro_v1|delta_india|ETH/USD|5m",
                "bucket": bucket,
                "owner": "human",
                "action": "review paper evidence for the next promotion step",
            }
        ],
    }


def test_bridge_keeps_contract_broken_candidate_in_repair_bucket():
    payload = build_paper_promotion_bridge(
        readiness=_readiness(),
        performance=_performance(),
        contract=_contract("CONTRACT_BROKEN"),
        maker_quote=_quote(),
        actions=_actions("REPAIR_PAPER_CONTRACT"),
    )

    row = payload["rows"][0]
    assert row["decision"] == DECISION_REPAIR_FIRST
    assert row["stage"] == "REPAIR"
    assert row["owner"] == "human"
    assert row["paper_review_ready"] is False
    assert "REPAIR_PAPER_CONTRACT" in row["blockers"][0]
    assert payload["summary"]["repair_first"] == 1
    assert payload["can_promote"] is False


def test_bridge_routes_contract_clean_negative_lane_to_alpha_mining():
    payload = build_paper_promotion_bridge(
        readiness=_readiness(),
        performance=_performance(state="PAPER_ACTIVE_NEGATIVE", closed=22, pf=0.8, net=-18.0),
        contract=_contract("CONTRACT_OK_NEGATIVE_ALPHA", closed=22, avg=-14.2, net=-18.0),
        maker_quote=_quote(),
        actions=_actions("MINE_CLEAN_ALPHA"),
    )

    row = payload["rows"][0]
    assert row["decision"] == DECISION_MINE_ALPHA
    assert row["stage"] == "ALPHA"
    assert row["owner"] == "research"
    assert "alpha insufficient" in row["blockers"][0]
    assert payload["summary"]["mine_alpha"] == 1
    assert payload["policy"]["human_review_is_not_promotion"] is True


def test_bridge_marks_clean_profitable_candidate_for_human_review_only():
    payload = build_paper_promotion_bridge(
        readiness=_readiness(),
        performance=_performance(),
        contract=_contract(),
        maker_quote=_quote(),
        actions=_actions(),
        config=PaperPromotionBridgeConfig(min_closed_trades=20, min_profit_factor=1.5, min_avg_net_bps=25.0),
    )

    row = payload["rows"][0]
    assert row["decision"] == DECISION_PAPER_REVIEW_READY
    assert row["stage"] == "HUMAN_REVIEW"
    assert row["owner"] == "human"
    assert row["paper_review_ready"] is True
    assert row["live_ready"] is False
    assert row["can_promote"] is False
    assert payload["summary"]["paper_review_ready"] == 1
    assert "ready for human review" in payload["operator_answer"]


def test_bridge_publish_writes_latest_and_feed(tmp_path):
    payload = build_paper_promotion_bridge(
        readiness=_readiness(),
        performance=_performance(),
        contract=_contract(),
        maker_quote=_quote(),
        actions=_actions(),
    )
    out = tmp_path / "paper_promotion_bridge_latest.json"
    feed = tmp_path / "paper_promotion_bridge_feed.jsonl"

    publish_paper_promotion_bridge(payload, out, feed)

    assert json.loads(out.read_text())["report_id"] == "paper_promotion_bridge_v1"
    assert json.loads(feed.read_text().strip())["mode"] == "read_only_paper_promotion_bridge"
