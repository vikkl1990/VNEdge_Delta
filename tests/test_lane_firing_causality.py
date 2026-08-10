"""Lane firing causality: joined scanner + promotion truth."""

from datetime import UTC, datetime

from vnedge.research.lane_firing_causality import build_lane_firing_causality


NOW = datetime(2026, 7, 26, 13, 0, tzinfo=UTC)


def _scanner_row(**overrides):
    row = {
        "lane_id": "sats_eth_delta_shadow",
        "row_type": "runtime_lane",
        "exchange": "delta_india",
        "symbol": "ETH/USD:USD",
        "timeframe": "5m",
        "strategy_id": "sats_5m_scalper_v1",
        "mode": "shadow",
        "state": "WAITING",
        "why": "volume_z 0.1/1; below trigger",
        "funnel": {
            "evals": 40,
            "live_evals": 40,
            "live_signals": 0,
            "shadow_intents": 0,
            "approved_shadow_intents": 0,
            "rejected_shadow_intents": 0,
            "shadow_outcomes": 0,
            "risk_decisions": 0,
            "paper_order_intents": 0,
            "paper_exits": 0,
        },
        "latest_eval": {
            "fired": False,
            "features": {"volume_z": 0.1, "body_atr": 0.6},
            "thresholds": {"min_volume_z": 1.0},
        },
        "gate_diagnostics": {"readiness_score": 0.55},
    }
    row.update(overrides)
    return row


def _ready_row(**overrides):
    row = {
        "row_type": "runtime_shadow_lane",
        "lane_id": "sats_eth_delta_shadow",
        "exchange": "delta_india",
        "symbol": "ETH/USD:USD",
        "timeframe": "5m",
        "strategy_id": "sats_5m_scalper_v1",
        "mode": "shadow",
        "status": "SHADOW_NOT_FIRING",
        "canonical_status": "SHADOW:WAITING_FOR_OUTCOMES",
        "primary_blocker": "no resolved shadow_outcome records for this lane",
        "funnel": {
            "stage": "SHADOW",
            "state": "WAITING_FOR_OUTCOMES",
            "next_stage": "SHADOW",
        },
        "paper_review_ready": False,
        "paper_active": False,
        "live_ready": False,
        "evidence": {"virtual_trades": 0},
        "next_action": "keep shadow lane running",
    }
    row.update(overrides)
    return row


def test_waiting_lane_explains_current_trigger_blocker():
    payload = build_lane_firing_causality(
        readiness={"rows": [_ready_row()]},
        scanner={"rows": [_scanner_row()]},
        now=NOW,
    )

    row = payload["rows"][0]
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False
    assert row["flow"]["data"]["state"] == "PASS"
    assert row["flow"]["setup"]["state"] == "PASS"
    assert row["flow"]["trigger"]["state"] == "BLOCK"
    assert row["primary_blocker"]["stage"] == "trigger"
    assert row["paper_decision"]["state"] == "NEEDS_SHADOW_OUTCOMES"
    assert "volume_z" in row["why_no_trade_minute"]
    assert payload["summary"]["needs_shadow_outcomes"] == 1


def test_near_trigger_lane_is_watchlist_not_trade_permission():
    payload = build_lane_firing_causality(
        readiness={"rows": [_ready_row()]},
        scanner={"rows": [_scanner_row(state="NEAR_TRIGGER", why="score 4.8/5 (96% of trigger)")]},
        now=NOW,
    )

    row = payload["rows"][0]
    assert row["flow"]["trigger"]["state"] == "WAIT"
    assert row["scanner_state"] == "NEAR_TRIGGER"
    assert payload["summary"]["near_trigger"] == 1
    assert payload["operator_answer"].startswith("No lane is firing now; 1 lane")


def test_fired_lane_without_route_flags_risk_and_execution_wait():
    payload = build_lane_firing_causality(
        readiness={"rows": [_ready_row()]},
        scanner={
            "rows": [
                _scanner_row(
                    state="FIRING",
                    why="LONG score passed",
                    funnel={"evals": 41, "live_evals": 41, "live_signals": 1},
                    latest_eval={"fired": True, "features": {"score": 5.4}},
                )
            ]
        },
        now=NOW,
    )

    row = payload["rows"][0]
    assert row["flow"]["trigger"]["state"] == "PASS"
    assert row["flow"]["risk"]["state"] == "WAIT"
    assert row["flow"]["execution"]["state"] == "WAIT"
    assert row["primary_blocker"]["stage"] == "risk"
    assert "signal fired, but risk is not complete" in row["why_no_trade_minute"]


def test_paper_review_ready_is_promoted_to_board_but_not_auto_promoted():
    ready = _ready_row(
        status="PAPER_REVIEW_READY",
        canonical_status="SHADOW:READY_FOR_PAPER_REVIEW",
        primary_blocker="no blocker recorded",
        funnel={
            "stage": "SHADOW",
            "state": "READY_FOR_PAPER_REVIEW",
            "next_stage": "PAPER",
        },
        paper_review_ready=True,
        evidence={"virtual_trades": 12, "net_usd": 6.0, "profit_factor": 1.8},
    )
    payload = build_lane_firing_causality(
        readiness={"rows": [ready]},
        scanner={"rows": [_scanner_row()]},
        now=NOW,
    )

    row = payload["rows"][0]
    assert row["paper_decision"]["state"] == "READY_FOR_PAPER_REVIEW"
    assert row["primary_blocker"]["category"] == "paper_review"
    assert payload["summary"]["paper_review_ready"] == 1
    assert payload["promotion_board"]["ready_for_review"][0]["strategy_id"] == (
        "sats_5m_scalper_v1"
    )
    assert payload["promotion_board"]["can_promote"] is False


def test_replay_positive_without_scanner_requires_shadow_adapter():
    payload = build_lane_firing_causality(
        readiness={
            "rows": [
                _ready_row(
                    row_type="filtered_replay_shadow_trial",
                    exchange="bybit",
                    symbol="SOL/USDT:USDT",
                    timeframe="5m",
                    strategy_id="event_leadlag_shadow",
                    status="REPLAY_POSITIVE_NEEDS_SHADOW_ADAPTER",
                    canonical_status="REPLAY:NEEDS_SHADOW_ADAPTER",
                    primary_blocker="replay-positive event has no runtime shadow adapter",
                    funnel={
                        "stage": "REPLAY",
                        "state": "NEEDS_SHADOW_ADAPTER",
                        "next_stage": "SHADOW",
                    },
                )
            ]
        },
        scanner={"rows": []},
        now=NOW,
    )

    row = payload["rows"][0]
    assert row["scanner_state"] == "NO_SCANNER"
    assert row["flow"]["execution"]["state"] == "BLOCK"
    assert row["primary_blocker"]["stage"] == "execution"
    assert row["primary_blocker"]["action"] == "build runtime shadow adapter"
    assert row["paper_decision"]["state"] == "NEEDS_SHADOW_ADAPTER"
    assert payload["summary"]["needs_shadow_adapter"] == 1


def test_scanner_only_row_is_a_manifest_tracking_gap():
    payload = build_lane_firing_causality(
        readiness={"rows": []},
        scanner={"rows": [_scanner_row()]},
        now=NOW,
    )

    row = payload["rows"][0]
    assert row["paper_decision"]["state"] == "UNTRACKED_SCANNER"
    assert row["primary_blocker"]["category"] == "manifest_tracking"
    assert payload["summary"]["untracked_scanner"] == 1
