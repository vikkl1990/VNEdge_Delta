from __future__ import annotations

from datetime import UTC, datetime
import json

from vnedge.research.quantified_proof_result_arbiter import (
    QUANTIFIED_PROOF_RESULT_ARBITER_ID,
    build_quantified_proof_result_arbiter_payload,
    load_quantified_proof_result_arbiter_payload,
    publish_quantified_proof_result_arbiter,
)


def _proof_payload() -> dict:
    return {
        "proof_id": "quantified_blueprint_proof_v1",
        "generated_at": "2026-08-01T00:00:00+00:00",
        "summary": {
            "gate": {
                "min_net_bps": 25.0,
                "min_profit_factor": 1.5,
                "min_trades": 20,
            }
        },
        "rows": [
            {
                "port_id": "range_volatility_breakout_reversion_v1",
                "strategy_id": "quantified_fee_wall_sniper_v1",
                "adapter": "fee_wall_sniper_breakout",
                "setup_mode": "breakout_only",
                "canonical_adapter": True,
                "exchange": "delta_india",
                "symbol": "ETH/USD:USD",
                "timeframe": "5m",
                "status": "DONE_RESEARCH_ONLY",
                "verdict": "PROMOTABLE_PROOF_REQUIRES_UNTOUCHED_JUDGMENT",
                "samples": 24,
                "avg_net_bps": 31.2,
                "profit_factor": 1.72,
                "win_rate_pct": 55.0,
            },
            {
                "port_id": "crypto_session_calendar_miner_v1",
                "strategy_id": "quant_signal_pack_v1",
                "adapter": "quant_signal_pack_session_proxy",
                "setup_mode": "session_proxy",
                "canonical_adapter": False,
                "exchange": "binanceusdm",
                "symbol": "BTC/USDT:USDT",
                "timeframe": "15m",
                "status": "DONE_RESEARCH_ONLY",
                "verdict": "PROMOTABLE_PROOF_REQUIRES_UNTOUCHED_JUDGMENT",
                "samples": 31,
                "avg_net_bps": 42.5,
                "profit_factor": 2.1,
            },
            {
                "port_id": "indicator_pack_mtf_v1",
                "strategy_id": "quant_signal_pack_v1",
                "adapter": "quant_signal_pack_mtf_atoms",
                "setup_mode": "indicator_confluence",
                "canonical_adapter": True,
                "exchange": "bybit",
                "symbol": "SOL/USDT:USDT",
                "timeframe": "1h",
                "status": "DONE_RESEARCH_ONLY",
                "verdict": "FEE_WALL_NEAR_MISS",
                "samples": 46,
                "avg_net_bps": -2.4,
                "profit_factor": 0.94,
            },
            {
                "port_id": "pullback_reversion_pack_v1",
                "strategy_id": "quantified_fee_wall_sniper_v1",
                "adapter": "fee_wall_sniper_pullback",
                "setup_mode": "pullback_only",
                "canonical_adapter": True,
                "exchange": "delta_india",
                "symbol": "XRP/USD:USD",
                "timeframe": "4h",
                "status": "BLOCKED_RESEARCH_ONLY",
                "verdict": "BLOCKED_DATA_OR_CONTRACT",
                "blocked_reason": "missing candles",
            },
            {
                "port_id": "bitcoin_crypto_strategy_pack_v1",
                "strategy_id": "quantified_fee_wall_sniper_v1",
                "adapter": "fee_wall_sniper_combined",
                "setup_mode": "pullback_plus_breakout",
                "canonical_adapter": True,
                "exchange": "binanceusdm",
                "symbol": "DOGE/USDT:USDT",
                "timeframe": "1m",
                "status": "PENDING_RESEARCH_ONLY",
                "verdict": "AWAITING_BACKTEST",
            },
        ],
    }


def test_quantified_proof_arbiter_splits_operator_actions():
    payload = build_quantified_proof_result_arbiter_payload(
        proof_payload=_proof_payload(),
        proof_path=None,
        now=datetime(2026, 8, 1, tzinfo=UTC),
    )

    assert payload["arbiter_id"] == QUANTIFIED_PROOF_RESULT_ARBITER_ID
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False
    summary = payload["summary"]
    assert summary["total_cells"] == 5
    assert summary["ready_for_judgment"] == 1
    assert summary["proxy_edges"] == 1
    assert summary["fee_wall_near_misses"] == 1
    assert summary["data_repairs"] == 1
    assert summary["awaiting_backtest"] == 1

    buckets = {row["port_id"]: row["bucket"] for row in payload["action_queue"]}
    assert buckets["range_volatility_breakout_reversion_v1"] == (
        "READY_FOR_UNTOUCHED_JUDGMENT"
    )
    assert buckets["crypto_session_calendar_miner_v1"] == (
        "PROXY_EDGE_NEEDS_CANONICAL_PORT"
    )
    assert buckets["indicator_pack_mtf_v1"] == "FEE_WALL_NEAR_MISS"

    actions = {row["bucket"]: row["next_action"] for row in payload["action_queue"]}
    assert actions["READY_FOR_UNTOUCHED_JUDGMENT"] == "QUEUE_UNTOUCHED_WINDOW_JUDGMENT"
    assert actions["PROXY_EDGE_NEEDS_CANONICAL_PORT"] == (
        "BUILD_CANONICAL_PORT_BEFORE_JUDGMENT"
    )
    assert actions["FEE_WALL_NEAR_MISS"] == "MINE_EXIT_CAPTURE_AND_ROUTE_FILTERS"
    assert payload["policy"]["blocked_actions"] == [
        "auto_promote_from_backtest",
        "paper_trade_from_proxy_adapter",
        "relax_fee_wall_gate",
        "rerun_burned_window",
    ]


def test_quantified_proof_arbiter_port_summary_uses_all_rows_not_display_limit():
    proof = _proof_payload()
    proof["rows"] = [
        {
            "port_id": f"port_{idx}",
            "strategy_id": "quant_signal_pack_v1",
            "exchange": "binanceusdm",
            "symbol": "BTC/USDT:USDT",
            "timeframe": "5m",
            "status": "DONE_RESEARCH_ONLY",
            "verdict": "FEE_WALL_NEAR_MISS",
            "samples": 20,
            "avg_net_bps": -1.0,
            "profit_factor": 0.99,
        }
        for idx in range(100)
    ]

    payload = build_quantified_proof_result_arbiter_payload(
        proof_payload=proof,
        proof_path=None,
    )

    assert payload["summary"]["total_cells"] == 100
    assert payload["summary"]["fee_wall_near_misses"] == 100
    assert len(payload["action_queue"]) == 80
    assert len(payload["port_summary"]) == 100


def test_quantified_proof_arbiter_repairs_win_over_stale_positive_metrics():
    proof = _proof_payload()
    proof["rows"] = [{
        "port_id": "broken_proxy",
        "strategy_id": "quant_signal_pack_v1",
        "adapter": "proxy_adapter",
        "canonical_adapter": False,
        "exchange": "delta_india",
        "symbol": "ETH/USD:USD",
        "timeframe": "5m",
        "status": "BLOCKED_RESEARCH_ONLY",
        "verdict": "BLOCKED_DATA_OR_CONTRACT",
        "samples": 99,
        "avg_net_bps": 60.0,
        "profit_factor": 2.5,
        "blocked_reason": "missing source candles",
    }]

    payload = build_quantified_proof_result_arbiter_payload(
        proof_payload=proof,
        proof_path=None,
    )

    action = payload["action_queue"][0]
    assert action["bucket"] == "DATA_REPAIR"
    assert action["next_action"] == "REPAIR_DATA_COVERAGE_OR_SYMBOL_MAPPING"
    assert payload["summary"]["proxy_edges"] == 0
    assert payload["summary"]["data_repairs"] == 1


def test_quantified_proof_arbiter_publish_and_load_round_trip(tmp_path):
    payload = build_quantified_proof_result_arbiter_payload(
        proof_payload=_proof_payload(),
        proof_path=None,
    )
    out = tmp_path / "quantified_proof_result_arbiter_latest.json"
    feed = tmp_path / "quantified_proof_result_arbiter_feed.jsonl"

    publish_quantified_proof_result_arbiter(payload, out=out, feed=feed)
    loaded = load_quantified_proof_result_arbiter_payload(out)

    assert loaded["arbiter_id"] == QUANTIFIED_PROOF_RESULT_ARBITER_ID
    assert loaded["summary"]["ready_for_judgment"] == 1
    assert feed.exists()
    assert QUANTIFIED_PROOF_RESULT_ARBITER_ID in feed.read_text(encoding="utf-8")
