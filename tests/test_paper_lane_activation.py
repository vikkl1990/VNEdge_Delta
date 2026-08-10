import json

from vnedge.research.paper_lane_activation import (
    ACTIVATION_MANIFEST_UNSAFE,
    ACTIVATION_NEEDS_HUMAN_APPROVAL,
    ACTIVATION_PAPER_ONLINE_WAITING,
    ACTIVATION_PAPER_RUNNING,
    ACTIVATION_ROUTE_BLOCKED,
    PaperLaneActivationConfig,
    _parse_args,
    build_paper_lane_activation,
)


def test_paper_activation_marks_running_only_with_manifest_route_and_journal(tmp_path):
    manifest_dir = tmp_path / "manifests"
    manifest_dir.mkdir()
    (manifest_dir / "funding.yaml").write_text(
        """
trial_id: funding_trial
strategy: funding_mean_reversion_v1
symbol: "BTC/USDT:USDT"
timeframe: 1h
approved_by: human
live_orders_enabled: false
max_leverage: 5
""",
        encoding="utf-8",
    )
    journal_dir = tmp_path / "journals"
    journal_dir.mkdir()
    (journal_dir / "funding_trial.journal.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "ts": "2026-07-26T00:00:00+00:00",
                        "kind": "lane_eval",
                        "payload": {
                            "strategy_id": "funding_mean_reversion_v1",
                            "exchange": "binanceusdm",
                            "symbol": "BTC/USDT:USDT",
                            "timeframe": "1h",
                            "why": "funding waiting",
                        },
                    }
                ),
                json.dumps(
                    {
                        "ts": "2026-07-26T01:00:00+00:00",
                        "kind": "order_intent",
                        "payload": {
                            "intent": {
                                "strategy_id": "funding_mean_reversion_v1",
                                "symbol": "BTC/USDT:USDT",
                            }
                        },
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    payload = build_paper_lane_activation(
        manifest_dir=manifest_dir,
        journal_dir=journal_dir,
        readiness={"rows": []},
        scanner={"rows": []},
        desired_specs=[
            {
                "lane_id": "funding_trial",
                "exchange": "binanceusdm",
                "symbol": "BTC/USDT:USDT",
                "timeframe": "1h",
                "strategy_id": "funding_mean_reversion_v1",
                "mode": "paper",
            }
        ],
    )

    row = payload["rows"][0]
    assert row["activation_state"] == ACTIVATION_PAPER_RUNNING
    assert row["route_checks"]["manifest_approved_by_human"] is True
    assert row["route_checks"]["desired_paper_route"] is True
    assert row["route_checks"]["journal_seen"] is True
    assert row["requested_experiment"]["can_run_requested"] is False
    assert "exceeds manifest max" in row["requested_experiment"]["blockers"][0]
    assert row["sizing_profiles"]["paper"]["profile"] == "paper"
    assert row["sizing_profiles"]["live"]["profile"] == "live"
    assert row["sizing_profiles"]["live"]["can_apply_from_dashboard"] is False
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False


def test_paper_activation_marks_heartbeat_only_lane_online_waiting(tmp_path):
    manifest_dir = tmp_path / "manifests"
    manifest_dir.mkdir()
    (manifest_dir / "stealth.yaml").write_text(
        """
trial_id: stealth_trial
strategy: stealth_trail_bbp_v1
exchange: delta_india
symbol: "ETH/USD:USD"
timeframe: 5m
approved_by: human
live_orders_enabled: false
max_leverage: 25
""",
        encoding="utf-8",
    )
    journal_dir = tmp_path / "journals"
    journal_dir.mkdir()
    (journal_dir / "stealth_trial.journal.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-07-26T00:00:00+00:00",
                "kind": "paper_lane_heartbeat",
                "payload": {
                    "strategy_id": "stealth_trail_bbp_v1",
                    "exchange": "delta_india",
                    "symbol": "ETH/USD:USD",
                    "timeframe": "5m",
                    "mode": "paper",
                    "reason": "waiting_for_closed_candle",
                    "why_no_trade": "last_eval_no_signal",
                    "bars_processed": 0,
                    "evals": 0,
                    "orders_submitted": 0,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    payload = build_paper_lane_activation(
        manifest_dir=manifest_dir,
        journal_dir=journal_dir,
        readiness={"rows": []},
        scanner={"rows": []},
        desired_specs=[
            {
                "lane_id": "stealth_trial",
                "exchange": "delta_india",
                "symbol": "ETH/USD:USD",
                "timeframe": "5m",
                "strategy_id": "stealth_trail_bbp_v1",
                "mode": "paper",
            }
        ],
        config=PaperLaneActivationConfig(high_leverage_ack=True),
    )

    row = payload["rows"][0]
    assert row["activation_state"] == ACTIVATION_PAPER_ONLINE_WAITING
    assert row["route_checks"]["journal_seen"] is True
    assert row["evidence"]["paper_journal"]["paper_lane_heartbeats"] == 1
    assert row["evidence"]["paper_journal"]["evals"] == 0
    assert row["blockers"] == ["last_eval_no_signal"]
    assert payload["summary"]["paper_online"] == 1
    assert payload["summary"]["paper_journal_heartbeats"] == 1


def test_paper_activation_surfaces_paper_review_ready_without_manifest(tmp_path):
    payload = build_paper_lane_activation(
        manifest_dir=tmp_path / "missing",
        journal_dir=tmp_path / "journals",
        readiness={
            "rows": [
                {
                    "status": "PAPER_REVIEW_READY",
                    "exchange": "delta_india",
                    "symbol": "ETH/USD:USD",
                    "timeframe": "4h",
                    "strategy_id": "stealth_trail_bbp_v1",
                    "blockers": [],
                }
            ]
        },
        scanner={"rows": []},
        desired_specs=[],
    )

    assert payload["rows"][0]["activation_state"] == ACTIVATION_NEEDS_HUMAN_APPROVAL
    assert payload["rows"][0]["next_action"] == (
        "create a locked paper-trial manifest after human approval"
    )
    assert payload["summary"]["needs_human_approval"] == 1


def test_paper_activation_blocks_manifest_not_wired_to_paper_route(tmp_path):
    manifest_dir = tmp_path / "manifests"
    manifest_dir.mkdir()
    (manifest_dir / "stealth.yaml").write_text(
        """
trial_id: stealth_trial
strategy: stealth_trail_bbp_v1
symbol: "ETH/USD:USD"
timeframe: 4h
approved_by: human
live_orders_enabled: false
max_leverage: 25
""",
        encoding="utf-8",
    )

    payload = build_paper_lane_activation(
        manifest_dir=manifest_dir,
        journal_dir=tmp_path / "journals",
        readiness={"rows": []},
        scanner={"rows": []},
        desired_specs=[],
        config=PaperLaneActivationConfig(high_leverage_ack=True),
    )

    row = payload["rows"][0]
    assert row["activation_state"] == ACTIVATION_ROUTE_BLOCKED
    assert row["route_checks"]["strategy_registered"] is True
    assert row["route_checks"]["desired_paper_route"] is False
    assert row["requested_experiment"]["can_run_requested"] is True
    assert row["sizing_profiles"]["paper"]["requested_notional_usd"] == 2500.0
    assert row["sizing_profiles"]["live"]["requested_notional_usd"] == 500.0
    assert payload["summary"]["paper_profile_risk_compatible"] == 1
    assert payload["summary"]["live_profile_risk_compatible"] == 1


def test_paper_activation_live_profile_uses_own_margin_and_leverage(tmp_path):
    manifest_dir = tmp_path / "manifests"
    manifest_dir.mkdir()
    (manifest_dir / "stealth.yaml").write_text(
        """
trial_id: stealth_trial
strategy: stealth_trail_bbp_v1
symbol: "ETH/USD:USD"
timeframe: 4h
approved_by: human
live_orders_enabled: false
max_leverage: 30
""",
        encoding="utf-8",
    )

    payload = build_paper_lane_activation(
        manifest_dir=manifest_dir,
        journal_dir=tmp_path / "journals",
        readiness={"rows": []},
        scanner={"rows": []},
        desired_specs=[],
        config=PaperLaneActivationConfig(
            requested_margin_usd=80.0,
            requested_leverage=8.0,
            live_margin_usd=40.0,
            live_leverage=31.0,
            high_leverage_ack=True,
        ),
    )

    row = payload["rows"][0]
    assert row["sizing_profiles"]["paper"]["risk_compatible"] is True
    assert row["sizing_profiles"]["paper"]["requested_notional_usd"] == 640.0
    assert row["sizing_profiles"]["live"]["risk_compatible"] is False
    assert row["sizing_profiles"]["live"]["requested_notional_usd"] == 1240.0
    assert "absolute max" in row["sizing_profiles"]["live"]["blockers"][0]


def test_paper_activation_cli_accepts_env_friendly_high_leverage_ack():
    args = _parse_args(["--high-leverage-ack", "true"])

    assert args.high_leverage_ack is True


def test_paper_activation_refuses_unsafe_live_orders_manifest(tmp_path):
    manifest_dir = tmp_path / "manifests"
    manifest_dir.mkdir()
    (manifest_dir / "unsafe.yaml").write_text(
        """
trial_id: unsafe_trial
strategy: funding_mean_reversion_v1
symbol: "BTC/USDT:USDT"
timeframe: 1h
approved_by: human
live_orders_enabled: true
max_leverage: 5
""",
        encoding="utf-8",
    )

    payload = build_paper_lane_activation(
        manifest_dir=manifest_dir,
        journal_dir=tmp_path / "journals",
        readiness={"rows": []},
        scanner={"rows": []},
        desired_specs=[
            {
                "lane_id": "unsafe_trial",
                "exchange": "binanceusdm",
                "symbol": "BTC/USDT:USDT",
                "timeframe": "1h",
                "strategy_id": "funding_mean_reversion_v1",
                "mode": "paper",
            }
        ],
    )

    row = payload["rows"][0]
    assert row["activation_state"] == ACTIVATION_MANIFEST_UNSAFE
    assert row["route_checks"]["manifest_safe"] is False
    assert row["can_trade"] is False


def test_paper_activation_gives_companion_lanes_unique_trial_ids(tmp_path):
    manifest_dir = tmp_path / "manifests"
    manifest_dir.mkdir()
    (manifest_dir / "bundle.yaml").write_text(
        """
trial_id: vnedge_algo_ml_pro_eth_4h
strategy: vnedge_algo_ml_pro_v1
symbol: "ETH/USD:USD"
timeframe: 4h
approved_by: human
live_orders_enabled: false
max_leverage: 5
companion_lanes:
  - symbol: "DOGE/USD:USD"
    timeframe: 1h
""",
        encoding="utf-8",
    )

    payload = build_paper_lane_activation(
        manifest_dir=manifest_dir,
        journal_dir=tmp_path / "journals",
        readiness={"rows": []},
        scanner={"rows": []},
        desired_specs=[
            {
                "lane_id": "eth",
                "exchange": "delta_india",
                "symbol": "ETH/USD:USD",
                "timeframe": "4h",
                "strategy_id": "vnedge_algo_ml_pro_v1",
                "mode": "paper",
            },
            {
                "lane_id": "doge",
                "exchange": "delta_india",
                "symbol": "DOGE/USD:USD",
                "timeframe": "1h",
                "strategy_id": "vnedge_algo_ml_pro_v1",
                "mode": "paper",
            },
        ],
    )

    trial_ids = {row["trial_id"] for row in payload["rows"]}
    assert trial_ids == {
        "vnedge_algo_ml_pro_eth_4h",
        "vnedge_algo_ml_pro_eth_4h_companion_doge_usd_usd_1h",
    }
