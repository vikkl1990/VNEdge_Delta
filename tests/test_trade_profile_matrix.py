from vnedge.research.trade_profile_matrix import (
    LIVE_PROFILE_BLOCKED_BY_RISK,
    LIVE_PROFILE_RISK_OK_PRELIVE_REQUIRED,
    PAPER_PROFILE_BLOCKED_BY_RISK,
    PAPER_PROFILE_READY,
    build_trade_profile_matrix,
)


def test_trade_profile_matrix_separates_paper_ready_from_live_prelive_gate():
    payload = build_trade_profile_matrix(
        {
            "report_id": "paper_lane_activation_v1",
            "generated_at": "2026-07-27T00:00:00+00:00",
            "rows": [
                {
                    "lane_key": "alpha|delta|eth|5m",
                    "trial_id": "alpha",
                    "exchange": "delta_india",
                    "symbol": "ETH/USD:USD",
                    "timeframe": "5m",
                    "strategy_id": "vnedge_algo_ml_pro_v1",
                    "activation_state": "PAPER_ONLINE_WAITING",
                    "route_status": "ROUTE_RUNNING",
                    "sizing_profiles": {
                        "paper": {
                            "requested_margin_usd": 100.0,
                            "requested_leverage": 25.0,
                            "requested_notional_usd": 2500.0,
                            "venue_min_notional_usd": 5.0,
                            "venue_spec_source": "delta_fallback_limits_contract_lookup_required_before_live",
                            "risk_compatible": True,
                            "blockers": [],
                        },
                        "live": {
                            "requested_margin_usd": 100.0,
                            "requested_leverage": 5.0,
                            "requested_notional_usd": 500.0,
                            "venue_min_notional_usd": 5.0,
                            "venue_spec_source": "delta_fallback_limits_contract_lookup_required_before_live",
                            "risk_compatible": True,
                            "blockers": [],
                        },
                    },
                }
            ],
        }
    )

    states = {row["profile"]: row["profile_state"] for row in payload["rows"]}
    assert states == {
        "paper": PAPER_PROFILE_READY,
        "live": LIVE_PROFILE_RISK_OK_PRELIVE_REQUIRED,
    }
    assert payload["summary"]["paper_profile_ready"] == 1
    assert payload["summary"]["live_profile_risk_ok_prelive_required"] == 1
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False


def test_trade_profile_matrix_surfaces_risk_blockers():
    payload = build_trade_profile_matrix(
        {
            "rows": [
                {
                    "lane_key": "alpha|delta|eth|5m",
                    "exchange": "delta_india",
                    "symbol": "ETH/USD:USD",
                    "timeframe": "5m",
                    "strategy_id": "vnedge_algo_ml_pro_v1",
                    "activation_state": "PAPER_ROUTE_READY_NO_JOURNAL",
                    "route_status": "ROUTE_READY",
                    "sizing_profiles": {
                        "paper": {
                            "requested_margin_usd": 100.0,
                            "requested_leverage": 31.0,
                            "requested_notional_usd": 3100.0,
                            "risk_compatible": False,
                            "blockers": ["requested leverage 31x exceeds absolute max 30x"],
                        },
                        "live": {
                            "requested_margin_usd": 100.0,
                            "requested_leverage": 31.0,
                            "requested_notional_usd": 3100.0,
                            "risk_compatible": False,
                            "blockers": ["requested leverage 31x exceeds absolute max 30x"],
                        },
                    },
                }
            ]
        }
    )

    states = {row["profile"]: row["profile_state"] for row in payload["rows"]}
    assert states["paper"] == PAPER_PROFILE_BLOCKED_BY_RISK
    assert states["live"] == LIVE_PROFILE_BLOCKED_BY_RISK
    assert payload["summary"]["paper_profile_blocked_by_risk"] == 1
    assert payload["summary"]["live_profile_blocked_by_risk"] == 1
    assert "blocked by sizing risk" in payload["operator_answer"]
