"""Execution-realistic replay profile tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from vnedge.research.execution_replay_profile import (
    EXECUTION_PROFILE_ID,
    build_execution_replay_profile_report,
    prediction_market_settlement_components,
    publish_execution_replay_profile,
)


def test_execution_replay_profile_separates_economics_from_fill_truth() -> None:
    evidence_index = {
        "records": [
            {
                "record_id": "fee-wall-1",
                "source_kind": "fee_wall_forensics",
                "source_artifact": "fee_wall_forensics_latest.json",
                "strategy_id": "sats_5m_scalper_v1",
                "exchange": "bybit",
                "symbol": "BTC/USDT:USDT",
                "timeframe": "5m",
                "status": "passed",
                "verdict": "MAKER_EDGE",
                "samples": 31,
                "avg_net_bps": 31.25,
                "profit_factor": 1.72,
                "route": "MAKER",
            },
            {
                "record_id": "replay-1",
                "source_kind": "candidate_replay",
                "source_artifact": "candidate_replay_latest.json",
                "strategy_id": "fvg_liquidity_breakout_v1",
                "exchange": "delta_india",
                "symbol": "ETH/USD:USD",
                "timeframe": "15m",
                "status": "passed",
                "verdict": "MAKER_EDGE",
                "samples": 42,
                "avg_net_bps": 38.5,
                "profit_factor": 1.91,
                "route": "MAKER",
            },
            {
                "record_id": "pm-1",
                "source_kind": "filtered_replay",
                "source_artifact": "filtered_replay_latest.json",
                "strategy_id": "prediction_market_port_attempt",
                "exchange": "binance",
                "symbol": "BTC/USDT:USDT",
                "timeframe": "5m",
                "status": "passed",
                "verdict": "EDGE",
                "samples": 50,
                "avg_net_bps": 44.0,
                "profit_factor": 1.8,
                "route": "TAKER",
                "metadata": {
                    "assumption": "hold_to_resolution with complementary binary_pair hedge"
                },
            },
            {
                "record_id": "weak-1",
                "source_kind": "scanner_tournament",
                "source_artifact": "scanner_tournament_latest.json",
                "strategy_id": "weak_bbp_v1",
                "exchange": "delta_india",
                "symbol": "SOL/USD:USD",
                "timeframe": "5m",
                "status": "failed",
                "verdict": "NEGATIVE",
                "samples": 18,
                "avg_net_bps": 7.1,
                "profit_factor": 1.1,
            },
        ]
    }

    payload = build_execution_replay_profile_report(
        evidence_index=evidence_index,
        candidate_replay={"config": {"queue_aware": False}},
        fee_wall={},
        now=datetime(2026, 7, 24, tzinfo=UTC),
    )

    assert payload["execution_profile_id"] == EXECUTION_PROFILE_ID
    assert payload["summary"]["records"] == 4
    assert payload["summary"]["strict_economic_rows"] == 3
    assert payload["summary"]["execution_truth_ready"] == 1
    assert payload["summary"]["requires_execution_replay_before_paper"] == 1
    assert payload["summary"]["settlement_blocked_rows"] == 1
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False
    assert payload["live_orders_enabled"] is False

    replay = next(
        row for row in payload["rows"] if row["strategy_id"] == "fvg_liquidity_breakout_v1"
    )
    assert replay["profile_id"] == "L3_L2_TRADE_THROUGH_REPLAY"
    assert replay["execution_truth_ready"] is True
    assert replay["next_action"] == "PRE_REGISTER_UNTOUCHED_WINDOW_THEN_PAPER_REVIEW"

    candle = next(row for row in payload["rows"] if row["strategy_id"] == "sats_5m_scalper_v1")
    assert candle["profile_id"] == "L1_CANDLE_FORWARD_ROUTE_LABEL"
    assert candle["requires_execution_replay_before_paper"] is True
    assert "candle_forward_label_is_not_order_fill_evidence" in candle["blockers"]

    settlement = next(
        row for row in payload["rows"] if row["strategy_id"] == "prediction_market_port_attempt"
    )
    assert settlement["settlement_portability"] == "BLOCKED_PREDICTION_MARKET_ASSUMPTION"
    assert settlement["execution_truth_ready"] is False
    assert settlement["next_action"] == "REWRITE_WITH_PERP_EXITS_AND_REPLAY"
    assert "prediction_market_hold_to_resolution_not_portable_to_perps" in settlement["blockers"]


def test_execution_replay_profile_can_require_queue_aware_profile() -> None:
    payload = build_execution_replay_profile_report(
        evidence_index={
            "records": [
                {
                    "source_kind": "candidate_replay",
                    "source_artifact": "candidate_replay_latest.json",
                    "strategy_id": "queue_candidate",
                    "exchange": "binance",
                    "symbol": "BTC/USDT:USDT",
                    "timeframe": "5m",
                    "samples": 24,
                    "avg_net_bps": 26.0,
                    "profit_factor": 1.6,
                    "route": "MAKER",
                }
            ]
        },
        candidate_replay={"config": {"queue_aware": True}},
    )

    row = payload["rows"][0]
    assert row["profile_id"] == "L4_L2_QUEUE_AWARE_MAKER_REPLAY"
    assert row["execution_truth_ready"] is True
    assert payload["summary"]["l3_or_l4_rows"] == 1


def test_prediction_market_settlement_matrix_blocks_non_portable_payoff_logic() -> None:
    components = {row.component_id: row for row in prediction_market_settlement_components()}

    assert components["terminal_binary_payoff"].portable is False
    assert components["hold_to_resolution"].crypto_perp_verdict == "NOT_PORTABLE_TO_PERPETUALS"
    assert components["ledger_replay"].portable is True
    assert components["coverage_and_gap_penalties"].portable is True


def test_publish_execution_replay_profile_writes_snapshot_and_feed(tmp_path) -> None:
    payload = build_execution_replay_profile_report(evidence_index={"records": []})
    out = tmp_path / "execution_replay_profile_latest.json"
    feed = tmp_path / "execution_replay_profile_feed.jsonl"

    publish_execution_replay_profile(payload, out=out, feed=feed)

    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["execution_profile_id"] == EXECUTION_PROFILE_ID
    feed_rows = [json.loads(line) for line in feed.read_text(encoding="utf-8").splitlines()]
    assert feed_rows == [
        {
            "execution_profile_id": EXECUTION_PROFILE_ID,
            "generated_at": saved["generated_at"],
            "summary": saved["summary"],
            "can_trade": False,
            "can_promote": False,
        }
    ]
