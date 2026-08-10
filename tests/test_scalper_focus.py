"""Scalper focus report — explain why scalping is not ready."""

from vnedge.research.scalper_focus import build_scalper_focus


def scan(**overrides):
    base = {
        "exchange": "binanceusdm",
        "symbol": "BTC/USDT:USDT",
        "day": "20260706",
        "state": "RECORD_MORE",
        "primary_blocker": "UNDER_SAMPLED_TICKS",
        "edge_score": 52.0,
        "recorder_priority": 92.0,
        "gates": {"sample": False, "edge_after_cost": False},
        "next_action": "record 4.0h more before judging",
        "route_decision": {
            "route": "BLOCKED",
            "maker_breakeven_bps": 0.5,
            "maker_min_profit_factor": 1.15,
        },
        "best_row": {
            "quotes": 100,
            "filled": 40,
            "fill_rate_pct": 40.0,
            "profit_factor": 0.04,
            "avg_net_bps": -8.5,
            "net_usd": -3.2,
            "exit_policy_id": "static_fast",
        },
    }
    base.update(overrides)
    return base


def hypothesis(**overrides):
    base = {
        "exchange": "binanceusdm",
        "symbol": "BTC/USDT:USDT",
        "day": "20260706",
        "family": "pressure_continuation",
        "side": "sell",
        "horizon_ms": 5_000,
        "state": "BELOW_BREAKEVEN",
        "samples": 100,
        "profit_factor": 0.2,
        "avg_net_bps": -6.0,
        "avg_forward_bps": 1.0,
        "win_rate_pct": 45.0,
        "hypothesis_id": "pressure_continuation|imb>=0.35|flow>=0.58",
        "route_decision": {
            "route": "BLOCKED",
            "maker_breakeven_bps": 0.5,
            "maker_min_profit_factor": 1.15,
        },
    }
    base.update(overrides)
    return base


def test_focus_marks_data_collection_when_ticks_are_missing_or_short():
    focus = build_scalper_focus([
        scan(state="MISSING_TICK_DATA", primary_blocker="NO_TICK_DATA",
             best_row=None, recorder_priority=55.0),
        scan(),
    ], [], days=("20260706",))

    assert focus["status"] == "SCALPER_DATA_COLLECTION"
    assert focus["can_trade"] is False
    assert focus["summary"]["missing_tick_data"] == 1
    assert focus["summary"]["record_more"] == 1
    assert "record tick/L2" in focus["next_actions"][0]
    assert focus["family_lifecycle"]["tombstoned"][0]["family_id"] == (
        "book_imbalance_continuation"
    )
    assert focus["next_family_priority"][0]["family_id"] == "forced_flow_continuation"
    assert "forced_flow_continuation" in focus["next_actions"][2]
    assert focus["recorder_campaign"][0]["can_trade"] is False


def test_focus_promotes_edge_hypothesis_to_replay_work_not_trade():
    focus = build_scalper_focus([], [
        hypothesis(
            state="EDGE_CANDIDATE_MAKER",
            profit_factor=1.5,
            avg_net_bps=2.0,
            route_decision={"route": "MAKER_ONLY", "maker_breakeven_bps": 0.5,
                            "maker_min_profit_factor": 1.15},
        )
    ])

    assert focus["status"] == "EDGE_HYPOTHESIS_READY"
    assert focus["summary"]["edge_candidates"] == 1
    assert focus["hypothesis_focus"][0]["route"] == "MAKER_ONLY"
    assert "conservative replay" in focus["next_actions"][0]
    assert focus["can_promote"] is False


def test_focus_exposes_cost_wall_gap_for_negative_replay():
    focus = build_scalper_focus([scan(state="REJECTED_COST_WALL")], [hypothesis()])

    lane = focus["cost_wall"]["closest_scanner_lanes"][0]
    assert lane["maker_gap"]["net_gap_bps"] == -9.0
    assert lane["maker_gap"]["pf_gap"] == -1.11
    assert focus["summary"]["cost_wall"] == 1
    assert "book_imbalance_continuation" in focus["next_actions"][1]
    assert "liquidity_vacuum_continuation" in focus["next_actions"][2]


def test_focus_handles_legacy_scalar_payloads():
    focus = build_scalper_focus([], ["legacy-edge"], recorder_targets=["legacy-target"])

    assert focus["status"] == "SCALPER_COST_WALL"
    assert focus["summary"]["edge_hypotheses"] == 1
