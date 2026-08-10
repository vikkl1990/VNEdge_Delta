import json
from datetime import UTC, datetime, timedelta

from vnedge.research.realtime_scanner import (
    STATE_FIRING,
    STATE_NEAR_TRIGGER,
    STATE_WAITING,
    RealtimeScannerConfig,
    build_realtime_scanner,
    publish_realtime_scanner,
)


NOW = datetime(2026, 7, 12, 10, 0, tzinfo=UTC)


def write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, default=str) + "\n")


def record(kind, payload, minutes_ago=1):
    return {
        "ts": (NOW - timedelta(minutes=minutes_ago)).isoformat(),
        "kind": kind,
        "payload": payload,
    }


def lane_eval(*, fired=False, funding=0.62, z=0.4, mode="shadow"):
    return {
        "bar_ts": (NOW - timedelta(minutes=1)).isoformat(),
        "strategy_id": "funding_mean_reversion",
        "symbol": "BTC/USDT:USDT",
        "mode": mode,
        "fired": fired,
        "signal_reason": "funding stretched" if fired else None,
        "skip_reason": None,
        "features": {"funding_pct": funding, "close_z": z},
        "thresholds": {"extreme_pct": 0.85, "z_entry": 1.5},
        "backfill": False,
    }


def test_runtime_lane_waiting_with_threshold_gap(tmp_path):
    journal = tmp_path / "logs" / "funding_mr_binanceusdm_btc_shadow.journal.jsonl"
    write_jsonl(journal, [record("lane_eval", lane_eval(funding=0.62, z=-0.4))])

    payload = build_realtime_scanner(
        research_dir=tmp_path / "research",
        journal_dir=tmp_path / "logs",
        now=NOW,
    )

    assert payload["mode"] == "live_observation_not_replay"
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False
    assert payload["summary"]["waiting"] == 1
    row = payload["rows"][0]
    assert row["state"] == STATE_WAITING
    assert row["exchange"] == "binanceusdm"
    assert "funding" in row["why"]
    assert row["proximity"][0]["name"] == "funding"
    assert row["requires_replay_for_promotion"] is True


def test_runtime_lane_near_trigger(tmp_path):
    journal = tmp_path / "logs" / "funding_mr_bybit_btc_shadow.journal.jsonl"
    write_jsonl(journal, [record("lane_eval", lane_eval(funding=0.82, z=0.2))])

    payload = build_realtime_scanner(
        research_dir=tmp_path / "research",
        journal_dir=tmp_path / "logs",
        config=RealtimeScannerConfig(near_trigger_ratio=0.90),
        now=NOW,
    )

    row = payload["rows"][0]
    assert row["state"] == STATE_NEAR_TRIGGER
    assert payload["summary"]["near_trigger"] == 1
    assert "96%" in row["why"]


def test_runtime_lane_reports_quant_score_proximity(tmp_path):
    journal = tmp_path / "logs" / "quant_signal_pack_v1_bybit_eth_shadow.journal.jsonl"
    write_jsonl(journal, [
        record(
            "lane_eval",
            {
                "bar_ts": (NOW - timedelta(minutes=1)).isoformat(),
                "strategy_id": "quant_signal_pack_v1",
                "symbol": "ETH/USDT:USDT",
                "mode": "shadow",
                "fired": False,
                "signal_reason": None,
                "skip_reason": None,
                "features": {
                    "long_score": 4.5,
                    "short_score": 3.9,
                    "volume_z": 0.2,
                },
                "thresholds": {
                    "min_score": 5.0,
                    "min_score_delta": 1.0,
                    "min_volume_z": 0.35,
                },
                "backfill": False,
            },
        )
    ])

    payload = build_realtime_scanner(
        research_dir=tmp_path / "research",
        journal_dir=tmp_path / "logs",
        config=RealtimeScannerConfig(near_trigger_ratio=0.95),
        now=NOW,
    )

    row = payload["rows"][0]
    names = {item["name"] for item in row["proximity"]}
    assert row["state"] == STATE_WAITING
    assert row["why"].startswith("score 4.5/5")
    assert {"score", "score_delta", "volume_z"} <= names
    assert row["gate_diagnostics"]["primary_blocker"]["name"] == "score"
    assert row["gate_diagnostics"]["primary_blocker"]["category"] == "confluence_quality"
    assert row["uplift"]["action"] == "ISOLATE_STRONGER_SIGNAL_FAMILY"


def test_runtime_lane_with_passed_gates_and_cooldown_reports_runtime_blocker(tmp_path):
    journal = tmp_path / "logs" / "quant_signal_pack_v1_bybit_eth_shadow.journal.jsonl"
    write_jsonl(journal, [
        record(
            "lane_eval",
            {
                "bar_ts": (NOW - timedelta(minutes=1)).isoformat(),
                "strategy_id": "quant_signal_pack_v1",
                "symbol": "ETH/USDT:USDT",
                "mode": "shadow",
                "fired": False,
                "signal_reason": None,
                "skip_reason": "post_exit_cooldown: 1 bar(s) remaining",
                "features": {
                    "long_score": 7.0,
                    "short_score": 1.0,
                    "volume_z": 1.2,
                },
                "thresholds": {
                    "min_score": 5.0,
                    "min_score_delta": 1.0,
                    "min_volume_z": 0.35,
                },
                "backfill": False,
            },
        )
    ])

    payload = build_realtime_scanner(
        research_dir=tmp_path / "research",
        journal_dir=tmp_path / "logs",
        now=NOW,
    )

    row = payload["rows"][0]
    assert row["state"] == STATE_WAITING
    assert row["gate_diagnostics"]["all_gates_passed"] is True
    assert row["gate_diagnostics"]["primary_blocker"] is None
    assert row["uplift"]["action"] == "WAIT_FOR_COOLDOWN_CLEAR"
    assert row["uplift"]["priority"] == "observe"


def test_runtime_lane_with_passed_gates_and_no_signal_reports_hidden_veto(tmp_path):
    journal = tmp_path / "logs" / "sats_5m_scalper_delta_eth_shadow.journal.jsonl"
    write_jsonl(journal, [
        record(
            "lane_eval",
            {
                "bar_ts": (NOW - timedelta(minutes=1)).isoformat(),
                "strategy_id": "sats_5m_scalper_v1",
                "symbol": "ETH/USD:USD",
                "mode": "shadow",
                "fired": False,
                "signal_reason": None,
                "skip_reason": None,
                "features": {
                    "tqi_long": 0.70,
                    "tqi_short": 0.10,
                    "quality_strength": 0.25,
                    "bbp": 0.40,
                    "volume_z": 1.20,
                },
                "thresholds": {
                    "min_tqi": 0.58,
                    "min_quality_strength": 0.08,
                    "min_bbp_atr": 0.10,
                    "min_volume_z": -0.75,
                },
                "backfill": False,
            },
        )
    ])

    payload = build_realtime_scanner(
        research_dir=tmp_path / "research",
        journal_dir=tmp_path / "logs",
        now=NOW,
    )

    row = payload["rows"][0]
    assert row["state"] == STATE_WAITING
    assert payload["summary"]["near_trigger"] == 0
    assert row["gate_diagnostics"]["all_gates_passed"] is True
    assert row["gate_diagnostics"]["primary_blocker"] is None
    assert "hidden veto" in row["why"]
    assert row["final_why_no_trade"].startswith("all published scanner gates passed")
    assert row["trade_lifecycle"]["stage"] == "HIDDEN_VETO"
    assert payload["summary"]["hidden_veto_lanes"] == 1
    assert row["uplift"]["action"] == "EXPOSE_DECISION_BLOCKER_TELEMETRY"
    assert row["uplift"]["priority"] == "instrument"


def test_runtime_lane_reports_sats_and_stealth_proximity(tmp_path):
    journal = tmp_path / "logs" / "sats_5m_scalper_delta_sol_shadow.journal.jsonl"
    write_jsonl(journal, [
        record(
            "lane_eval",
            {
                "bar_ts": (NOW - timedelta(minutes=1)).isoformat(),
                "strategy_id": "sats_5m_scalper_v1",
                "symbol": "SOL/USD:USD",
                "mode": "shadow",
                "fired": False,
                "signal_reason": None,
                "skip_reason": None,
                "features": {
                    "tqi_long": 0.51,
                    "tqi_short": 0.30,
                    "quality_strength": 0.21,
                    "mom_persist_long": 0.44,
                    "bbp": 0.08,
                    "volume_z": -0.90,
                    "expected_net_edge_bps_long": 22.0,
                },
                "thresholds": {
                    "min_tqi": 0.58,
                    "min_quality_strength": 0.08,
                    "min_momentum_persistence": 0.55,
                    "min_bbp_atr": 0.10,
                    "min_volume_z": -0.75,
                    "min_expected_net_edge_bps": 25.0,
                },
                "backfill": False,
            },
        )
    ])

    payload = build_realtime_scanner(
        research_dir=tmp_path / "research",
        journal_dir=tmp_path / "logs",
        config=RealtimeScannerConfig(near_trigger_ratio=0.95),
        now=NOW,
    )

    row = payload["rows"][0]
    names = {item["name"] for item in row["proximity"]}
    assert row["state"] == STATE_WAITING
    assert "expected_net_edge_bps" in names
    assert {"tqi", "quality_strength", "momentum_persistence", "bbp_atr", "volume_z"} <= names
    assert row["gate_diagnostics"]["primary_blocker"]["name"] == "expected_net_edge_bps"
    assert row["gate_diagnostics"]["primary_blocker"]["category"] == "cost_edge"
    assert row["uplift"]["action"] == "REPAIR_EXECUTION_ROUTE_OR_SKIP"


def test_runtime_staleness_uses_lane_timeframe(tmp_path):
    logs = tmp_path / "logs"
    five_min = logs / "sats_5m_scalper_delta_eth_paper_observation.journal.jsonl"
    hourly = logs / "funding_mr_btc_v1_20260703.journal.jsonl"
    write_jsonl(five_min, [
        record(
            "lane_eval",
            {
                **lane_eval(funding=0.62, z=-0.4, mode="paper"),
                "strategy_id": "sats_5m_scalper_v1",
                "symbol": "ETH/USD:USD",
                "timeframe": "5m",
            },
            minutes_ago=16,
        )
    ])
    write_jsonl(hourly, [
        record(
            "lane_eval",
            {
                **lane_eval(funding=0.62, z=-0.4, mode="paper"),
                "timeframe": "1h",
            },
            minutes_ago=179,
        )
    ])

    payload = build_realtime_scanner(
        research_dir=tmp_path / "research",
        journal_dir=logs,
        now=NOW,
    )

    rows = {row["lane_id"]: row for row in payload["rows"]}
    assert rows["sats_5m_scalper_delta_eth_paper_observation"]["state"] == "STALE"
    assert rows["sats_5m_scalper_delta_eth_paper_observation"]["stale_after_seconds"] == 900.0
    assert rows["funding_mr_btc_v1_20260703"]["state"] == STATE_WAITING
    assert rows["funding_mr_btc_v1_20260703"]["stale_after_seconds"] == 10_800.0
    assert payload["summary"]["paper_fresh_lanes"] == 1
    assert payload["summary"]["paper_stale_lanes"] == 1


def test_paper_summary_distinguishes_fresh_and_order_producing(tmp_path):
    logs = tmp_path / "logs"
    fresh = logs / "fresh_paper_observation.journal.jsonl"
    stale = logs / "stale_paper_observation.journal.jsonl"
    write_jsonl(
        fresh,
        [
            record("lane_eval", lane_eval(fired=True, mode="paper"), minutes_ago=10),
            record(
                "order_intent",
                {
                    "intent_key": "k1",
                    "client_order_id": "cid1",
                    "intent": {"symbol": "BTC/USDT:USDT", "side": "long"},
                },
                minutes_ago=9,
            ),
        ],
    )
    write_jsonl(stale, [
        record("lane_eval", lane_eval(funding=0.2, mode="paper"), minutes_ago=240)
    ])

    payload = build_realtime_scanner(
        research_dir=tmp_path / "research",
        journal_dir=logs,
        now=NOW,
    )

    summary = payload["summary"]
    assert summary["paper_lanes"] == 2
    assert summary["paper_fresh_lanes"] == 1
    assert summary["paper_stale_lanes"] == 1
    assert summary["paper_order_lanes"] == 1
    assert summary["paper_active_lanes_1h"] == 1
    assert summary["paper_order_intents_1h"] == 1
    assert summary["paper_order_intents_24h"] == 1
    assert "1/2 fresh" in payload["operator_answer"]


def test_runtime_lane_firing_counts_shadow_intent(tmp_path):
    journal = tmp_path / "logs" / "funding_mr_delta_india_btc_shadow.journal.jsonl"
    write_jsonl(
        journal,
        [
            record("lane_eval", lane_eval(fired=True, funding=0.91, z=0.1)),
            record(
                "shadow_intent",
                {
                    "intent_key": "k1",
                    "approved": True,
                    "intent": {
                        "exchange": "delta_india",
                        "symbol": "BTC/USD:USD",
                        "side": "long",
                        "notional_usd": 500.0,
                        "strategy_id": "funding_mean_reversion",
                    },
                    "signal_reason": "funding stretched",
                },
            ),
        ],
    )

    payload = build_realtime_scanner(
        research_dir=tmp_path / "research",
        journal_dir=tmp_path / "logs",
        now=NOW,
    )

    row = payload["rows"][0]
    assert row["state"] == STATE_FIRING
    assert row["funnel"]["live_signals"] == 1
    assert row["funnel"]["shadow_intents"] == 1
    assert row["latest_shadow_intent"]["approved"] is True
    assert payload["summary"]["firing"] == 1


def test_runtime_paper_lane_reports_order_and_exit_activity(tmp_path):
    journal = tmp_path / "logs" / "funding_mr_btc_v1_20260703.journal.jsonl"
    write_jsonl(
        journal,
        [
            record("lane_eval", lane_eval(fired=True, funding=0.91, z=-1.8, mode="paper")),
            record("risk_decision", {"approved": True}),
            record(
                "order_intent",
                {
                    "intent_key": "k-paper",
                    "client_order_id": "vne_123",
                    "intent": {
                        "symbol": "BTC/USDT:USDT",
                        "side": "long",
                        "quantity": 0.01,
                        "strategy_id": "funding_mean_reversion",
                        "reduce_only": False,
                    },
                },
            ),
            record("order_acknowledged", {"intent_key": "k-paper"}),
            record("live_paper_exit", {"reason": "take_profit", "state": "filled"}),
        ],
    )

    payload = build_realtime_scanner(
        research_dir=tmp_path / "research",
        journal_dir=tmp_path / "logs",
        now=NOW,
    )

    row = payload["rows"][0]
    assert row["mode"] == "paper"
    assert row["state"] == STATE_FIRING
    assert row["funnel"]["paper_order_intents"] == 1
    assert row["funnel"]["paper_exits"] == 1
    assert row["latest_paper_order"]["client_order_id"] == "vne_123"
    assert row["trade_lifecycle"]["stage"] == "EXIT_RECORDED"
    assert payload["summary"]["paper_lanes"] == 1
    assert payload["summary"]["paper_firing"] == 1
    assert payload["summary"]["paper_order_intents"] == 1
    assert "Paper activity: 1/1 fresh, 1 orders in 1h, 1 in 24h" in payload["operator_answer"]


def test_runtime_paper_lane_reports_in_position_lifecycle_and_tp_ladder(tmp_path):
    journal = tmp_path / "logs" / "luxy_ut_bot_delta_xrp_paper.journal.jsonl"
    write_jsonl(
        journal,
        [
            record(
                "lane_eval",
                {
                    "bar_ts": (NOW - timedelta(minutes=2)).isoformat(),
                    "strategy_id": "luxy_ut_bot_forecast_v1",
                    "exchange": "delta_india",
                    "symbol": "XRP/USD:USD",
                    "timeframe": "1h",
                    "mode": "paper",
                    "fired": True,
                    "signal_reason": "long trigger flip",
                    "skip_reason": None,
                    "signal": {
                        "side": "long",
                        "stop_price": 1.02,
                        "take_profit_price": 1.09,
                        "take_profit_levels": [1.05, 1.07, 1.09],
                        "reason": "long trigger flip",
                    },
                    "features": {"expected_net_edge_bps_long": 135.0},
                    "thresholds": {"min_expected_net_edge_bps": 25.0},
                    "backfill": False,
                },
                minutes_ago=2,
            ),
            record(
                "order_intent",
                {
                    "intent_key": "k-paper",
                    "client_order_id": "vne_456",
                    "intent": {
                        "symbol": "XRP/USD:USD",
                        "side": "long",
                        "quantity": 10,
                        "strategy_id": "luxy_ut_bot_forecast_v1",
                        "reduce_only": False,
                    },
                },
                minutes_ago=1,
            ),
            record(
                "paper_lane_heartbeat",
                {
                    "runner_state": "in_position",
                    "why_no_trade": "position_open: managing exit plan",
                    "quote_seen": True,
                    "feed_staleness_seconds": 1.2,
                    "last_bar_ts": (NOW - timedelta(minutes=1)).isoformat(),
                    "last_eval": {
                        "fired": True,
                        "signal": {
                            "side": "long",
                            "stop_price": 1.02,
                            "take_profit_price": 1.09,
                            "take_profit_levels": [1.05, 1.07, 1.09],
                        },
                    },
                    "signals": 3,
                    "live_signals": 1,
                    "orders_submitted": 1,
                    "risk_rejects": 0,
                    "sizing_skips": 0,
                },
                minutes_ago=1,
            ),
        ],
    )

    payload = build_realtime_scanner(
        research_dir=tmp_path / "research",
        journal_dir=tmp_path / "logs",
        now=NOW,
    )

    row = payload["rows"][0]
    lifecycle = row["trade_lifecycle"]
    assert lifecycle["stage"] == "IN_POSITION"
    assert lifecycle["in_position"] is True
    assert lifecycle["side"] == "long"
    assert lifecycle["take_profit_levels"] == [1.05, 1.07, 1.09]
    assert lifecycle["exit_engine"]["tp_ladder"] == "journaled_progress_only_not_partial_exit"
    assert row["final_why_no_trade"] == "position_open: managing exit plan"
    assert row["latest_heartbeat"]["runner_state"] == "in_position"
    assert payload["summary"]["in_position_lanes"] == 1
    assert payload["summary"]["tp_ladder_observed_lanes"] == 1
    assert payload["summary"]["tp_ladder_journal_only_lanes"] == 1


def test_event_leadlag_shadow_artifact_is_live_scanner_row(tmp_path):
    research = tmp_path / "research"
    research.mkdir()
    (research / "event_leadlag_shadow_latest.json").write_text(json.dumps({
        "generated_at": NOW.isoformat(),
        "evaluations": [{
            "runner_id": "event_leadlag_shadow_runner",
            "spec_id": "sol_binance_to_delta_long",
            "leader_exchange": "binanceusdm",
            "leader_symbol": "SOL/USDT:USDT",
            "follower_exchange": "delta_india",
            "follower_symbol": "SOL/USD:USD",
            "fired": False,
            "state": "NO_TRADE",
            "why_no_trade": ["leader_move_below: 3bps < 20bps"],
            "metrics": {
                "signed_leader_bps": 3.0,
                "signed_leader_z": 0.5,
                "leader_volume_z": 0.7,
                "filter": {
                    "min_abs_leader_bps": 20.0,
                    "min_abs_leader_z": 1.0,
                    "min_volume_z": 1.0,
                },
            },
        }],
    }))

    payload = build_realtime_scanner(
        research_dir=research,
        journal_dir=tmp_path / "logs",
        now=NOW,
    )

    row = payload["rows"][0]
    assert row["row_type"] == "event_leadlag_shadow"
    assert row["state"] == STATE_WAITING
    assert row["exchange"] == "delta_india"
    assert "leader_move_below" in row["why"]
    assert row["gate_diagnostics"]["primary_blocker"]["category"] == "participation"
    assert row["uplift"]["action"] == "WAIT_FOR_REAL_PARTICIPATION"
    assert payload["summary"]["event_lanes"] == 1
    assert payload["summary"]["top_blocker_categories"]["participation"] == 1


def test_publish_realtime_scanner_atomic(tmp_path):
    payload = {"summary": {"total_rows": 0}, "can_trade": False, "can_promote": False}
    out = tmp_path / "research" / "realtime_scanner_latest.json"
    feed = tmp_path / "research" / "realtime_scanner_feed.jsonl"

    publish_realtime_scanner(payload, out, feed)

    assert json.loads(out.read_text())["can_trade"] is False
    assert json.loads(feed.read_text().splitlines()[0])["can_promote"] is False
