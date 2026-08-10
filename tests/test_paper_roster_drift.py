from datetime import UTC, datetime

from vnedge.research.paper_roster_drift import (
    PaperRosterDriftConfig,
    STATE_DEMOTION_STILL_RUNNING,
    STATE_EXPECTED_MISSING,
    STATE_EXPECTED_RUNNING,
    STATE_EXTRA_RUNNING_PAPER,
    build_paper_roster_drift,
)


def test_paper_roster_drift_flags_extra_and_demotion_running_lanes():
    governor = {
        "proposed_roster": {
            "paper_lanes": [
                {
                    "lane_id": "approved_alpha",
                    "strategy_id": "alpha",
                    "exchange": "delta_india",
                    "symbol": "ETH/USD:USD",
                    "timeframe": "5m",
                }
            ],
            "demote_to_shadow": [
                {
                    "lane_id": "bad_lane",
                    "strategy_id": "beta",
                    "exchange": "delta_india",
                    "symbol": "BTC/USD:USD",
                    "timeframe": "5m",
                }
            ],
        }
    }
    scanner = {
        "rows": [
            {
                "lane_id": "approved_alpha",
                "mode": "paper (live data)",
                "strategy_id": "alpha",
                "exchange": "delta_india",
                "symbol": "ETH/USD:USD",
                "timeframe": "5m",
                "state": "WAITING",
                "age_seconds": 30,
                "stale_after_seconds": 900,
            },
            {
                "lane_id": "bad_lane",
                "mode": "paper (live data)",
                "strategy_id": "beta",
                "exchange": "delta_india",
                "symbol": "BTC/USD:USD",
                "timeframe": "5m",
                "state": "WAITING",
                "age_seconds": 30,
                "stale_after_seconds": 900,
            },
            {
                "lane_id": "extra_gamma",
                "mode": "paper (live data)",
                "strategy_id": "gamma",
                "exchange": "bybit",
                "symbol": "SOL/USDT:USDT",
                "timeframe": "15m",
                "state": "FIRING",
                "age_seconds": 30,
                "stale_after_seconds": 900,
            },
        ]
    }

    payload = build_paper_roster_drift(
        governor=governor,
        scanner=scanner,
        activation={"rows": []},
        now=datetime(2026, 7, 31, tzinfo=UTC),
    )

    states = {row["lane_id"]: row["drift_state"] for row in payload["rows"]}
    assert states["approved_alpha"] == STATE_EXPECTED_RUNNING
    assert states["bad_lane"] == STATE_DEMOTION_STILL_RUNNING
    assert states["extra_gamma"] == STATE_EXTRA_RUNNING_PAPER
    assert payload["summary"]["expected_paper_lanes"] == 1
    assert payload["summary"]["actual_paper_lanes"] == 3
    assert payload["summary"]["extra_paper_lanes"] == 2
    assert payload["summary"]["demotion_queue_running"] == 1
    assert payload["summary"]["drift_detected"] is True
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False
    assert "demotion-queue" in payload["operator_answer"]


def test_paper_roster_drift_matches_suffix_lanes_by_signature_and_reports_missing():
    governor = {
        "proposed_roster": {
            "paper_lanes": [
                {
                    "lane_id": "human_name",
                    "strategy_id": "alpha",
                    "exchange": "delta_india",
                    "symbol": "ETH/USD:USD",
                    "timeframe": "5m",
                },
                {
                    "lane_id": "missing_lane",
                    "strategy_id": "omega",
                    "exchange": "bybit",
                    "symbol": "XRP/USDT:USDT",
                    "timeframe": "1h",
                },
            ],
        }
    }
    scanner = {
        "rows": [
            {
                "lane_id": "alpha_delta_india_ethusd_5m_paper_observation",
                "mode": "paper (live data)",
                "strategy_id": "alpha",
                "exchange": "delta_india",
                "symbol": "ETH/USD",
                "timeframe": "5m",
                "age_seconds": 60,
                "stale_after_seconds": 900,
            }
        ]
    }

    payload = build_paper_roster_drift(
        governor=governor,
        scanner=scanner,
        activation={"rows": []},
        now=datetime(2026, 7, 31, tzinfo=UTC),
    )

    states = {row["strategy_id"]: row["drift_state"] for row in payload["rows"]}
    assert states["alpha"] == STATE_EXPECTED_RUNNING
    assert states["omega"] == STATE_EXPECTED_MISSING
    assert payload["summary"]["expected_running"] == 1
    assert payload["summary"]["missing_paper_lanes"] == 1


def test_paper_roster_drift_can_use_activation_when_scanner_is_empty():
    governor = {
        "proposed_roster": {
            "paper_lanes": [
                {
                    "lane_id": "approved_alpha",
                    "strategy_id": "alpha",
                    "exchange": "delta_india",
                    "symbol": "ETH/USD:USD",
                    "timeframe": "5m",
                }
            ],
        }
    }
    activation = {
        "rows": [
            {
                "trial_id": "approved_alpha",
                "activation_state": "PAPER_ONLINE_WAITING",
                "strategy_id": "alpha",
                "exchange": "delta_india",
                "symbol": "ETH/USD:USD",
                "timeframe": "5m",
                "runtime": {"desired_lane_ids": ["approved_alpha"]},
                "evidence": {
                    "paper_journal": {"last_ts": "2026-07-31T00:00:00+00:00"}
                },
            }
        ]
    }

    payload = build_paper_roster_drift(
        governor=governor,
        scanner={"rows": []},
        activation=activation,
        now=datetime(2026, 7, 31, tzinfo=UTC),
    )

    assert payload["summary"]["actual_paper_lanes"] == 1
    assert payload["summary"]["drift_detected"] is False
    assert payload["rows"][0]["drift_state"] == STATE_EXPECTED_RUNNING


def test_paper_roster_drift_ignores_stale_paper_evidence_as_historical():
    governor = {
        "proposed_roster": {
            "paper_lanes": [
                {
                    "lane_id": "approved_alpha",
                    "strategy_id": "alpha",
                    "exchange": "delta_india",
                    "symbol": "ETH/USD:USD",
                    "timeframe": "5m",
                }
            ],
            "demote_to_shadow": [
                {
                    "lane_id": "bad_lane",
                    "strategy_id": "beta",
                    "exchange": "delta_india",
                    "symbol": "BTC/USD:USD",
                    "timeframe": "5m",
                }
            ],
        }
    }
    scanner = {
        "rows": [
            {
                "lane_id": "approved_alpha",
                "mode": "paper (live data)",
                "strategy_id": "alpha",
                "exchange": "delta_india",
                "symbol": "ETH/USD:USD",
                "timeframe": "5m",
                "state": "WAITING",
                "age_seconds": 60,
                "stale_after_seconds": 900,
            },
            {
                "lane_id": "bad_lane",
                "mode": "paper (live data)",
                "strategy_id": "beta",
                "exchange": "delta_india",
                "symbol": "BTC/USD:USD",
                "timeframe": "5m",
                "state": "STALE",
                "age_seconds": 36_000,
                "stale_after_seconds": 900,
            },
            {
                "lane_id": "old_extra",
                "mode": "paper (live data)",
                "strategy_id": "gamma",
                "exchange": "bybit",
                "symbol": "SOL/USDT:USDT",
                "timeframe": "15m",
                "trade_lifecycle": {"stage": "STALE"},
                "age_seconds": 36_000,
                "stale_after_seconds": 900,
            },
        ]
    }
    activation = {
        "rows": [
            {
                "trial_id": "old_activation_lane",
                "activation_state": "PAPER_RUNNING",
                "strategy_id": "delta",
                "exchange": "binanceusdm",
                "symbol": "DOGE/USDT:USDT",
                "timeframe": "1h",
                "runtime": {"desired_lane_ids": ["old_activation_lane"]},
                "evidence": {
                    "paper_journal": {"last_ts": "2026-07-30T00:00:00+00:00"}
                },
            }
        ]
    }

    payload = build_paper_roster_drift(
        governor=governor,
        scanner=scanner,
        activation=activation,
        config=PaperRosterDriftConfig(max_runtime_age_seconds=3 * 60 * 60),
        now=datetime(2026, 7, 31, tzinfo=UTC),
    )

    states = {row["lane_id"]: row["drift_state"] for row in payload["rows"]}
    assert states["approved_alpha"] == STATE_EXPECTED_RUNNING
    assert "bad_lane" not in states
    assert "old_extra" not in states
    assert "old_activation_lane" not in states
    assert payload["summary"]["actual_paper_lanes"] == 1
    assert payload["summary"]["extra_paper_lanes"] == 0
    assert payload["summary"]["demotion_queue_running"] == 0
    assert payload["summary"]["stale_paper_evidence_lanes"] == 3
    assert payload["summary"]["drift_detected"] is False
    assert "stale paper evidence" in payload["operator_answer"]


def test_paper_roster_drift_surfaces_shadow_observation_roster():
    shadow_manifest = {
        "lanes": [
            {
                "lane_id": "funding_btc_shadow",
                "exchange": "binanceusdm",
                "symbol": "BTC/USDT:USDT",
                "timeframe": "1h",
                "strategy_id": "funding_mean_reversion_v1",
                "source_verdict": "PASS",
                "latest_judgment": {"verdict": "PASS"},
            }
        ],
        "blocked": [
            {
                "exchange": "bybit",
                "symbol": "XRP/USDT:USDT",
                "strategy_id": "trend_continuation_v1",
                "reason": "latest untouched judgment rejected",
                "latest_judgment": {"verdict": "REJECT"},
            }
        ],
        "shadow_trials": [
            {
                "trial_id": "shadow_trial_orderflow",
                "exchange": "delta_india",
                "symbol": "ETH/USD:USD",
                "timeframe": "5m",
                "family": "orderflow_footprint_v1",
                "status": "REPLAY_POSITIVE_NEEDS_SHADOW_ADAPTER",
            }
        ],
    }

    payload = build_paper_roster_drift(
        governor={
            "proposed_roster": {
                "paper_lanes": [
                    {
                        "lane_id": "approved_alpha",
                        "strategy_id": "alpha",
                        "exchange": "delta_india",
                        "symbol": "ETH/USD:USD",
                        "timeframe": "5m",
                    }
                ]
            }
        },
        scanner={
            "rows": [
                {
                    "lane_id": "approved_alpha",
                    "mode": "paper (live data)",
                    "strategy_id": "alpha",
                    "exchange": "delta_india",
                    "symbol": "ETH/USD:USD",
                    "timeframe": "5m",
                    "state": "WAITING",
                    "age_seconds": 30,
                    "stale_after_seconds": 900,
                }
            ]
        },
        activation={"rows": []},
        shadow_manifest=shadow_manifest,
        now=datetime(2026, 7, 31, tzinfo=UTC),
    )

    assert payload["mode"] == "read_only_unified_lane_roster"
    assert payload["summary"]["expected_paper_lanes"] == 1
    assert payload["summary"]["actual_paper_lanes"] == 1
    assert payload["summary"]["shadow_observation_lanes"] == 1
    assert payload["summary"]["shadow_blocked_lanes"] == 1
    assert payload["summary"]["shadow_trials_waiting_adapter"] == 1
    assert payload["summary"]["shadow_pass_lanes"] == 1
    states = {row["shadow_state"] for row in payload["shadow_rows"]}
    assert states == {
        "SHADOW_OBSERVING",
        "SHADOW_BLOCKED",
        "SHADOW_TRIAL_WAITING_ADAPTER",
    }
    assert payload["shadow_rows"][0]["can_trade"] is False
    assert "shadow candidate" in payload["shadow_operator_answer"]
    unified_modes = {row["roster_mode"] for row in payload["lane_rows"]}
    unified_states = {row["roster_state"] for row in payload["lane_rows"]}
    assert unified_modes == {"paper", "shadow"}
    assert unified_states == states | {STATE_EXPECTED_RUNNING}
    assert any(row["paper_trading_enabled"] for row in payload["lane_rows"])
    assert any(row["shadow_observation_only"] for row in payload["lane_rows"])
    assert "1/1 paper lanes recent" in payload["roster_operator_answer"]
