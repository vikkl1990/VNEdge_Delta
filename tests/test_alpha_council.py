"""Alpha Council deterministic agent debate."""

import json

from vnedge.research.alpha_council import (
    collect_candidates,
    publish_alpha_council,
    run_alpha_council,
)


def write_json(path, payload):
    path.write_text(json.dumps(payload))


def test_alpha_council_ranks_event_leadlag_but_keeps_replay_gate(tmp_path):
    write_json(
        tmp_path / "event_leadlag_latest.json",
        {
            "generated_at": "2026-07-08T00:00:00Z",
            "hypotheses": [
                {
                    "hypothesis_id": "leadlag|SOL|binance->delta",
                    "state": "EDGE_CANDIDATE_MAKER",
                    "follower_exchange": "delta_india",
                    "follower_symbol": "SOL/USD:USD",
                    "horizon_min": 15,
                    "route_decision": "MAKER_ONLY",
                    "samples": 33,
                    "maker_avg_net_bps": 11.8,
                    "maker_profit_factor": 1.98,
                    "taker_profit_factor": 1.62,
                    "win_rate_pct": 27.3,
                }
            ],
        },
    )

    payload = run_alpha_council(tmp_path)

    assert payload["can_trade"] is False
    assert payload["can_promote"] is False
    assert payload["summary"]["high_priority"] == 1
    debate = payload["debates"][0]
    assert debate["candidate"]["family"] == "cross_venue_event_leadlag_v1"
    assert debate["next_action"] == "RUN_CONSERVATIVE_L2_REPLAY"
    assert "requires_conservative_l2_replay" in debate["vetoes"]
    assert "maker_fill_unproven" in debate["vetoes"]
    assert {row["agent_id"] for row in debate["debate"]} == {
        "edge_advocate",
        "skeptic",
        "execution_specialist",
        "risk_governor",
        "research_director",
    }


def test_alpha_council_sends_candle_pass_to_untouched_judgment(tmp_path):
    write_json(
        tmp_path / "latest.json",
        {
            "generated_at": "2026-07-08T00:00:00Z",
            "results": [
                {
                    "exchange": "delta_india",
                    "symbol": "XRP/USD:USD",
                    "timeframe": "1h",
                    "strategy": "volatility_expansion_breakout_v1",
                    "verdict": "PASS",
                    "oos_net_usd": 23.47,
                    "oos_trades": 24,
                    "profit_factor": 1.45,
                    "payoff_ratio": 1.9,
                    "profitable_windows_pct": 71.4,
                }
            ],
        },
    )

    payload = run_alpha_council(tmp_path)

    debate = payload["debates"][0]
    assert debate["candidate"]["source"] == "rolling_walk_forward"
    assert debate["next_action"] == "PRE_REGISTER_UNTOUCHED_JUDGMENT"
    assert "requires_untouched_judgment" in debate["vetoes"]
    assert debate["can_trade"] is False


def test_alpha_council_under_sampled_l2_candidate_records_more_ticks(tmp_path):
    write_json(
        tmp_path / "l2_scout_latest.json",
        {
            "top_results": [
                {
                    "exchange": "delta_india",
                    "symbol": "SOL/USD:USD",
                    "family": "absorption_reversal",
                    "state": "UNDER_SAMPLED",
                    "route_decision": {"route": "BLOCKED"},
                    "samples": 4,
                    "avg_net_bps": 3.2,
                    "profit_factor": 1.4,
                    "horizon_ms": 5000,
                }
            ]
        },
    )

    candidates = collect_candidates(tmp_path)
    payload = run_alpha_council(tmp_path)

    assert any(candidate.source == "fast_l2_scout" for candidate in candidates)
    debate = next(
        row for row in payload["debates"]
        if row["candidate"]["source"] == "fast_l2_scout"
    )
    assert debate["candidate"]["source"] == "fast_l2_scout"
    assert debate["next_action"] == "RECORD_MORE_TICKS"
    assert "needs_more_samples" in debate["vetoes"]
    assert debate["can_trade"] is False


def test_alpha_council_routes_orderflow_candidates_to_replay(tmp_path):
    write_json(
        tmp_path / "orderflow_footprint_latest.json",
        {
            "miner_id": "orderflow_footprint_v1",
            "can_trade": False,
            "candidates": [
                {
                    "candidate_id": "orderflow_footprint|delta|SOL|20260706|1|buy",
                    "exchange": "delta_india",
                    "symbol": "SOL/USD:USD",
                    "day": "20260706",
                    "family": "orderflow_footprint_v1",
                    "side": "buy",
                    "timeframe": "60s",
                    "state": "ORDERFLOW_CANDIDATE",
                    "route_decision": "REPLAY_REQUIRED",
                    "samples": 42,
                    "stacked_run_length": 4,
                    "score": 88.2,
                    "delta_ratio": 0.82,
                    "price_change_bps": 9.4,
                    "cvd_notional_usd": 125_000.0,
                    "total_notional_usd": 22_000.0,
                    "trade_count": 42,
                    "can_trade": False,
                    "can_promote": False,
                }
            ],
        },
    )

    payload = run_alpha_council(tmp_path)
    debate = next(
        row for row in payload["debates"]
        if row["candidate"]["source"] == "orderflow_footprint"
    )

    assert debate["candidate"]["family"] == "orderflow_footprint_v1"
    assert debate["next_action"] == "RUN_CONSERVATIVE_L2_REPLAY"
    assert "requires_conservative_l2_replay" in debate["vetoes"]
    assert debate["can_trade"] is False
    assert debate["can_promote"] is False


def test_alpha_council_replay_failure_blocks_event_candidate(tmp_path):
    hypothesis_id = (
        "cross_venue_event_leadlag_v1|SOL|binanceusdm->delta_india|"
        "long|15m|ret>=4bps|z>=1.8|volZ>=-0.25|lag<=0.5x/6bps"
    )
    write_json(
        tmp_path / "event_leadlag_latest.json",
        {
            "hypotheses": [
                {
                    "hypothesis_id": hypothesis_id,
                    "state": "EDGE_CANDIDATE_MAKER",
                    "follower_exchange": "delta_india",
                    "follower_symbol": "SOL/USD:USD",
                    "horizon_min": 15,
                    "route_decision": "MAKER_ONLY",
                    "samples": 36,
                    "maker_avg_net_bps": 10.4,
                    "maker_profit_factor": 1.91,
                    "win_rate_pct": 30.0,
                }
            ],
        },
    )
    write_json(
        tmp_path / "candidate_replay_latest.json",
        {
            "rows": [
                {
                    "candidate_id": hypothesis_id,
                    "source": "event_leadlag_alpha",
                    "verdict": "NO_FILLS",
                    "quotes": 5,
                    "fills": 0,
                    "fill_rate_pct": 0.0,
                    "net_usd": 0.0,
                    "avg_net_bps": 0.0,
                    "profit_factor": 0.0,
                }
            ],
        },
    )

    payload = run_alpha_council(tmp_path)
    debate = next(
        row for row in payload["debates"]
        if row["candidate"]["candidate_id"] == f"event_leadlag|{hypothesis_id}"
    )

    assert debate["candidate"]["metrics"]["replay_verdict"] == "NO_FILLS"
    assert debate["candidate"]["evidence"]["execution_replay"]["quotes"] == 5
    assert debate["next_action"] == "MINE_PRE_EVENT_EXECUTION_CONDITIONS"
    assert debate["council_verdict"] == "EXECUTION_REPLAY_FAILED"
    assert "maker_fill_failed" in debate["vetoes"]
    assert "execution_replay_failed" in debate["vetoes"]
    assert "requires_conservative_l2_replay" not in debate["vetoes"]
    assert debate["can_trade"] is False


def test_alpha_council_routes_condition_mined_failure_to_filtered_replay(tmp_path):
    hypothesis_id = "leadlag|SOL|binanceusdm->delta_india|long|15m"
    write_json(
        tmp_path / "event_leadlag_latest.json",
        {
            "hypotheses": [
                {
                    "hypothesis_id": hypothesis_id,
                    "state": "EDGE_CANDIDATE_MAKER",
                    "follower_exchange": "delta_india",
                    "follower_symbol": "SOL/USD:USD",
                    "horizon_min": 15,
                    "route_decision": "MAKER_ONLY",
                    "samples": 36,
                    "maker_avg_net_bps": 10.4,
                    "maker_profit_factor": 1.91,
                }
            ],
        },
    )
    write_json(
        tmp_path / "candidate_replay_latest.json",
        {
            "rows": [
                {
                    "candidate_id": hypothesis_id,
                    "source": "event_leadlag_alpha",
                    "verdict": "NO_FILLS",
                    "quotes": 5,
                    "fills": 0,
                    "net_usd": 0.0,
                }
            ],
        },
    )
    write_json(
        tmp_path / "execution_condition_latest.json",
        {
            "candidate_conditions": [
                {
                    "candidate_id": hypothesis_id,
                    "source": "event_leadlag_alpha",
                    "family": "cross_venue_event_leadlag_v1",
                    "primary_bucket": "TOUCH_ONLY_QUEUE_RISK",
                    "recommended_action": "RUN_FILTERED_REPLAY_FROM_EXECUTION_CONDITIONS",
                    "confidence": 1.0,
                    "rows": 5,
                    "filter_proposal": {
                        "filter": "require_pre_event_trade_through_proxy",
                        "must_replay_fresh_window": True,
                    },
                }
            ],
        },
    )

    payload = run_alpha_council(tmp_path)
    debate = next(
        row for row in payload["debates"]
        if row["candidate"]["candidate_id"] == f"event_leadlag|{hypothesis_id}"
    )

    assert debate["candidate"]["metrics"]["execution_condition_bucket"] == (
        "TOUCH_ONLY_QUEUE_RISK"
    )
    assert debate["candidate"]["metrics"]["execution_condition_filter"] == (
        "require_pre_event_trade_through_proxy"
    )
    assert debate["next_action"] == "RUN_FILTERED_REPLAY_FROM_EXECUTION_CONDITIONS"
    assert debate["council_verdict"] == "EXECUTION_REPLAY_FAILED"
    assert debate["can_trade"] is False


def test_alpha_council_routes_filtered_replay_pass_to_shadow_queue(tmp_path):
    candidate_id = "orderflow_footprint|delta_india|SOL/USD:USD|20260706|1000|buy"
    write_json(
        tmp_path / "orderflow_footprint_latest.json",
        {
            "candidates": [
                {
                    "candidate_id": candidate_id,
                    "exchange": "delta_india",
                    "symbol": "SOL/USD:USD",
                    "day": "20260706",
                    "family": "orderflow_footprint_v1",
                    "side": "buy",
                    "timeframe": "60s",
                    "state": "ORDERFLOW_CANDIDATE",
                    "route_decision": "REPLAY_REQUIRED",
                    "samples": 42,
                    "score": 91.0,
                }
            ],
        },
    )
    write_json(
        tmp_path / "candidate_replay_latest.json",
        {
            "rows": [
                {
                    "candidate_id": candidate_id,
                    "source": "orderflow_footprint",
                    "verdict": "NO_FILLS",
                    "quotes": 5,
                    "fills": 0,
                    "net_usd": 0.0,
                }
            ],
        },
    )
    write_json(
        tmp_path / "execution_condition_latest.json",
        {
            "candidate_conditions": [
                {
                    "candidate_id": candidate_id,
                    "source": "orderflow_footprint",
                    "primary_bucket": "TOUCH_ONLY_QUEUE_RISK",
                    "recommended_action": "RUN_FILTERED_REPLAY_FROM_EXECUTION_CONDITIONS",
                    "confidence": 1.0,
                    "rows": 5,
                    "filter_proposal": {"filter": "require_pre_event_trade_through_proxy"},
                }
            ],
        },
    )
    write_json(
        tmp_path / "filtered_replay_latest.json",
        {
            "rows": [
                {
                    "candidate_id": candidate_id,
                    "source": "orderflow_footprint",
                    "verdict": "REPLAY_CANDIDATE",
                    "quotes": 12,
                    "fills": 8,
                    "fill_rate_pct": 66.7,
                    "net_usd": 2.5,
                    "avg_net_bps": 11.4,
                    "profit_factor": 1.7,
                    "filter_name": "require_pre_event_trade_through_proxy",
                    "condition_bucket": "TOUCH_ONLY_QUEUE_RISK",
                }
            ],
        },
    )

    payload = run_alpha_council(tmp_path)
    debate = next(
        row for row in payload["debates"]
        if row["candidate"]["candidate_id"] == candidate_id
    )

    assert debate["candidate"]["metrics"]["filtered_replay_verdict"] == "REPLAY_CANDIDATE"
    assert debate["next_action"] == "QUEUE_SHADOW_TRIAL_AFTER_REPLAY"
    assert debate["council_verdict"] == "HIGH_PRIORITY_RESEARCH"
    assert "requires_shadow_trial_after_replay" in debate["vetoes"]
    assert "execution_replay_failed" not in debate["vetoes"]
    assert debate["can_trade"] is False


def test_alpha_council_replay_pass_queues_shadow_trial(tmp_path):
    candidate_id = "orderflow_footprint|delta_india|SOL/USD:USD|20260706|1000|buy"
    write_json(
        tmp_path / "orderflow_footprint_latest.json",
        {
            "candidates": [
                {
                    "candidate_id": candidate_id,
                    "exchange": "delta_india",
                    "symbol": "SOL/USD:USD",
                    "day": "20260706",
                    "family": "orderflow_footprint_v1",
                    "side": "buy",
                    "timeframe": "60s",
                    "state": "ORDERFLOW_CANDIDATE",
                    "route_decision": "REPLAY_REQUIRED",
                    "samples": 42,
                    "score": 91.0,
                    "stacked_run_length": 5,
                    "delta_ratio": 0.83,
                    "price_change_bps": 11.2,
                }
            ],
        },
    )
    write_json(
        tmp_path / "candidate_replay_latest.json",
        {
            "rows": [
                {
                    "candidate_id": candidate_id,
                    "source": "orderflow_footprint",
                    "verdict": "REPLAY_CANDIDATE",
                    "quotes": 30,
                    "fills": 18,
                    "fill_rate_pct": 60.0,
                    "net_usd": 3.25,
                    "avg_net_bps": 12.4,
                    "profit_factor": 1.82,
                    "avg_adverse_bps": 0.6,
                }
            ],
        },
    )

    payload = run_alpha_council(tmp_path)
    debate = next(
        row for row in payload["debates"]
        if row["candidate"]["candidate_id"] == candidate_id
    )

    assert debate["candidate"]["metrics"]["replay_verdict"] == "REPLAY_CANDIDATE"
    assert debate["next_action"] == "QUEUE_SHADOW_TRIAL_AFTER_REPLAY"
    assert "requires_shadow_trial_after_replay" in debate["vetoes"]
    assert "requires_conservative_l2_replay" not in debate["vetoes"]
    assert debate["can_trade"] is False
    assert debate["can_promote"] is False


def test_alpha_council_records_more_ticks_for_under_sampled_orderflow(tmp_path):
    write_json(
        tmp_path / "orderflow_footprint_latest.json",
        {
            "candidates": [
                {
                    "candidate_id": "orderflow_footprint|bybit|XRP|20260706|1|sell",
                    "exchange": "bybit",
                    "symbol": "XRP/USDT:USDT",
                    "family": "orderflow_footprint_v1",
                    "timeframe": "60s",
                    "state": "UNDER_SAMPLED_ORDERFLOW",
                    "route_decision": "REPLAY_REQUIRED",
                    "samples": 4,
                    "score": 72.0,
                    "can_trade": False,
                }
            ],
        },
    )

    payload = run_alpha_council(tmp_path)
    debate = next(
        row for row in payload["debates"]
        if row["candidate"]["source"] == "orderflow_footprint"
    )

    assert debate["next_action"] == "RECORD_MORE_TICKS"
    assert "needs_more_samples" in debate["vetoes"]
    assert debate["can_trade"] is False


def test_alpha_council_routes_positive_rejects_to_specific_repair(tmp_path):
    write_json(
        tmp_path / "latest.json",
        {
            "generated_at": "2026-07-08T00:00:00Z",
            "results": [
                {
                    "exchange": "bybit",
                    "symbol": "XRP/USDT:USDT",
                    "timeframe": "1h",
                    "strategy": "trend_continuation_v1",
                    "verdict": "REJECT",
                    "oos_net_usd": 104.42,
                    "oos_trades": 48,
                    "profit_factor": 1.75,
                    "payoff_ratio": 2.07,
                    "profitable_windows_pct": 57.1,
                    "reasons": [
                        "one zero-trade test window",
                        "IS/OOS collapse retention below gate",
                    ],
                },
                {
                    "exchange": "binanceusdm",
                    "symbol": "ETH/USDT:USDT",
                    "timeframe": "1h",
                    "strategy": "quant_signal_pack_v1",
                    "verdict": "REJECT",
                    "oos_net_usd": 82.24,
                    "oos_trades": 61,
                    "profit_factor": 1.23,
                    "payoff_ratio": 1.08,
                    "reasons": ["aggregate OOS PF below gate", "payoff ratio below gate"],
                },
            ],
        },
    )

    payload = run_alpha_council(tmp_path)
    actions = {
        row["candidate"]["symbol"]: row["next_action"]
        for row in payload["debates"]
        if row["candidate"]["source"] == "rolling_walk_forward"
    }

    assert actions["XRP/USDT:USDT"] == "CHECK_ZERO_WINDOW_STABILITY"
    assert actions["ETH/USDT:USDT"] == "REPAIR_EXIT_PAYOFF"
    assert payload["summary"]["sources"]["rolling_walk_forward"] == 2


def test_alpha_council_flags_missing_research_artifacts(tmp_path):
    write_json(tmp_path / "latest.json", {"generated_at": "now", "results": []})

    payload = run_alpha_council(tmp_path)

    refreshes = [
        row for row in payload["debates"]
        if row["candidate"]["source"] == "artifact_health"
    ]
    assert refreshes
    assert all(row["next_action"] == "REFRESH_STALE_ARTIFACT" for row in refreshes)
    assert any(
        row["candidate"]["symbol"] == "daily_scalper_latest.json"
        for row in refreshes
    )


def test_alpha_council_routes_stressed_bitcoin_regime_to_replay_split(tmp_path):
    write_json(
        tmp_path / "bitcoin_regime_latest.json",
        {
            "schema_version": "bitcoin_regime_v1",
            "source": {"status": "ok"},
            "policy": {"can_trade": False, "can_promote": False},
            "summary": {
                "stress_state": "stressed",
                "stress_score": 6.75,
                "source_status": "ok",
            },
            "mempool": {"stress_state": "stressed"},
            "features": {
                "fee_pressure_score": 3.0,
                "mempool_pressure_score": 3.0,
                "fastest_fee_sat_vb": 85.0,
                "mempool_vsize_vb": 420_000_000,
                "mempool_tx_count": 240_000,
                "fee_spike_z": 2.4,
                "mempool_pressure_z": 1.2,
            },
        },
    )

    payload = run_alpha_council(tmp_path)
    debate = next(
        row for row in payload["debates"]
        if row["candidate"]["source"] == "bitcoin_regime"
    )

    assert debate["candidate"]["route_decision"] == "CONTEXT_ONLY"
    assert debate["next_action"] == "SPLIT_REPLAY_BY_BTC_REGIME"
    assert debate["can_trade"] is False
    assert "context_only_no_trade" in debate["vetoes"]
    assert "context_only_no_execution" in debate["vetoes"]
    assert "requires_replay_context_split" in debate["vetoes"]


def test_alpha_council_routes_bad_bitcoin_source_to_health_refresh(tmp_path):
    write_json(
        tmp_path / "bitcoin_regime_latest.json",
        {
            "schema_version": "bitcoin_regime_v1",
            "source": {"status": "node_unsynced"},
            "summary": {
                "stress_state": "missing",
                "stress_score": 0.0,
                "source_status": "node_unsynced",
            },
            "mempool": {"stress_state": "missing"},
            "features": {},
        },
    )

    payload = run_alpha_council(tmp_path)
    debate = next(
        row for row in payload["debates"]
        if row["candidate"]["source"] == "bitcoin_regime"
    )

    assert debate["candidate"]["state"] == "BTC_SOURCE_NODE_UNSYNCED"
    assert debate["next_action"] == "REFRESH_BITCOIN_NODE_HEALTH"
    assert debate["can_promote"] is False


def test_alpha_council_publish_is_atomic_and_appends_feed(tmp_path):
    payload = run_alpha_council(tmp_path)
    out = tmp_path / "alpha_council_latest.json"
    feed = tmp_path / "alpha_council_feed.jsonl"

    publish_alpha_council(payload, out, feed)
    publish_alpha_council(payload, out, feed)

    assert json.loads(out.read_text())["council_id"] == "alpha_agent_council_v1"
    assert not list(tmp_path.glob("*.tmp"))
    assert len(feed.read_text().strip().splitlines()) == 2
