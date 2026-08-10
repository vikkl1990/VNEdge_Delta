import json
from datetime import UTC, datetime

from vnedge.research.operator_actions import (
    ACTION_COLLECT_OUTCOMES,
    ACTION_FIX_EXIT_QUALITY,
    ACTION_FIX_SIZE_PROFILE,
    ACTION_MINE_CLEAN_ALPHA,
    ACTION_REPAIR_CONTRACT,
    ACTION_REPAIR_ROUTE,
    ACTION_REVIEW_PAPER_CANDIDATE,
    ACTION_RESTORE_CADENCE,
    ACTION_WAIT_FOR_SIGNAL,
    build_operator_actions,
    main as operator_actions_main,
)


NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


def _lane(**overrides):
    row = {
        "lane_key": "stealth|delta_india|eth/usd:usd|5m",
        "trial_id": "stealth_trial",
        "exchange": "delta_india",
        "symbol": "ETH/USD:USD",
        "timeframe": "5m",
        "strategy_id": "stealth_trail_bbp_v1",
    }
    row.update(overrides)
    return row


def test_operator_actions_repair_route_before_cadence_and_performance():
    payload = build_operator_actions(
        activation={
            "report_id": "paper_lane_activation_v1",
            "rows": [_lane(activation_state="PAPER_ONLINE_WAITING")],
        },
        route={
            "report_id": "paper_route_doctor_v1",
            "rows": [
                _lane(
                    doctor_state="ROUTE_READY_JOURNAL_MISSING",
                    next_action="inspect runner write path",
                )
            ],
        },
        cadence={
            "report_id": "paper_lane_cadence_v1",
            "rows": [_lane(cadence_state="EVAL_STALE", next_action="restore cadence")],
        },
        performance={
            "report_id": "paper_lane_performance_v1",
            "rows": [
                _lane(
                    state="PAPER_ACTIVE_NEGATIVE",
                    closed_trades=4,
                    net_pnl_usd=-2.5,
                )
            ],
        },
        profile={
            "report_id": "trade_profile_matrix_v1",
            "rows": [_lane(profile="paper", profile_state="PAPER_PROFILE_READY")],
        },
        causality={"report_id": "lane_firing_causality_v1", "rows": []},
        now=NOW,
    )

    row = payload["rows"][0]
    assert row["bucket"] == ACTION_REPAIR_ROUTE
    assert row["severity"] == "P1"
    assert row["owner"] == "system"
    assert row["action"] == "inspect runner write path"
    assert payload["summary"]["repair_first"] == 1
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False


def test_operator_actions_restore_cadence_when_route_is_active():
    payload = build_operator_actions(
        activation={"rows": [_lane(activation_state="PAPER_ONLINE_WAITING")]},
        route={"rows": [_lane(doctor_state="JOURNAL_ACTIVE")]},
        cadence={
            "rows": [
                _lane(
                    cadence_state="HEARTBEAT_STALE",
                    next_action="restart or inspect the paper runner",
                )
            ]
        },
        performance={"rows": []},
        profile={"rows": [_lane(profile="paper", profile_state="PAPER_PROFILE_READY")]},
        causality={"rows": []},
        now=NOW,
    )

    row = payload["rows"][0]
    assert row["bucket"] == ACTION_RESTORE_CADENCE
    assert row["action"] == "restart or inspect the paper runner"
    assert payload["summary"]["cadence_repairs"] == 1


def test_operator_actions_profile_fix_precedes_paper_review():
    payload = build_operator_actions(
        activation={"rows": [_lane(activation_state="PAPER_RUNNING")]},
        route={"rows": [_lane(doctor_state="JOURNAL_ACTIVE")]},
        cadence={"rows": [_lane(cadence_state="EVALUATING_SIGNAL_SEEN")]},
        performance={
            "rows": [
                _lane(
                    state="PAPER_PROMOTION_CANDIDATE",
                    closed_trades=24,
                    net_pnl_usd=12.0,
                    profit_factor=1.8,
                )
            ]
        },
        profile={
            "rows": [
                _lane(
                    profile="paper",
                    profile_state="PAPER_PROFILE_BLOCKED_BY_RISK",
                    next_action="reduce leverage before route review",
                )
            ]
        },
        causality={"rows": []},
        now=NOW,
    )

    row = payload["rows"][0]
    assert row["bucket"] == ACTION_FIX_SIZE_PROFILE
    assert row["owner"] == "operator"
    assert row["action"] == "reduce leverage before route review"
    assert payload["summary"]["profile_fixes"] == 1


def test_operator_actions_promotion_candidate_is_human_review_only():
    payload = build_operator_actions(
        activation={"rows": [_lane(activation_state="PAPER_RUNNING")]},
        route={"rows": [_lane(doctor_state="JOURNAL_ACTIVE")]},
        cadence={"rows": [_lane(cadence_state="EVALUATING_SIGNAL_SEEN")]},
        performance={
            "rows": [
                _lane(
                    state="PAPER_PROMOTION_CANDIDATE",
                    closed_trades=24,
                    net_pnl_usd=12.0,
                    profit_factor=1.8,
                    next_action="eligible for human review",
                )
            ]
        },
        profile={"rows": [_lane(profile="paper", profile_state="PAPER_PROFILE_READY")]},
        causality={
            "rows": [
                _lane(
                    scanner_state="FIRING",
                    paper_decision={"state": "READY_FOR_PAPER_REVIEW"},
                )
            ]
        },
        now=NOW,
    )

    row = payload["rows"][0]
    assert row["bucket"] == ACTION_REVIEW_PAPER_CANDIDATE
    assert row["owner"] == "human"
    assert row["action"] == "eligible for human review"
    assert payload["summary"]["paper_review"] == 1
    assert payload["policy"]["human_paper_review_is_not_promotion"] is True


def test_operator_actions_negative_paper_lane_is_collected_not_promoted():
    payload = build_operator_actions(
        activation={"rows": [_lane(activation_state="PAPER_RUNNING")]},
        route={"rows": [_lane(doctor_state="JOURNAL_ACTIVE")]},
        cadence={"rows": [_lane(cadence_state="EVALUATING_SIGNAL_SEEN")]},
        performance={
            "rows": [
                _lane(
                    state="PAPER_ACTIVE_NEGATIVE",
                    closed_trades=6,
                    net_pnl_usd=-4.2,
                    next_action="mine entry/exit failures; do not promote",
                )
            ]
        },
        profile={"rows": [_lane(profile="paper", profile_state="PAPER_PROFILE_READY")]},
        causality={"rows": []},
        now=NOW,
    )

    row = payload["rows"][0]
    assert row["bucket"] == ACTION_COLLECT_OUTCOMES
    assert row["severity"] == "P2"
    assert row["metrics"]["closed_trades"] == 6
    assert row["metrics"]["net_pnl_usd"] == -4.2
    assert payload["summary"]["negative_paper_lanes"] == 1


def test_operator_actions_uses_exit_autopsy_for_negative_lane_next_action():
    payload = build_operator_actions(
        activation={"rows": [_lane(activation_state="PAPER_RUNNING")]},
        route={"rows": [_lane(doctor_state="JOURNAL_ACTIVE")]},
        cadence={"rows": [_lane(cadence_state="EVALUATING_SIGNAL_SEEN")]},
        performance={
            "rows": [
                _lane(
                    state="PAPER_ACTIVE_NEGATIVE",
                    closed_trades=6,
                    net_pnl_usd=-4.2,
                    next_action="generic negative outcome",
                )
            ]
        },
        exit_autopsy={
            "rows": [
                _lane(
                    loss_driver="STOP_DOMINATED",
                    next_action="tighten entry permission before promotion",
                    avg_net_bps=-18.5,
                    stop_rate=0.83,
                )
            ]
        },
        profile={"rows": [_lane(profile="paper", profile_state="PAPER_PROFILE_READY")]},
        causality={"rows": []},
        now=NOW,
    )

    row = payload["rows"][0]
    assert row["bucket"] == ACTION_FIX_EXIT_QUALITY
    assert row["action"] == "tighten entry permission before promotion"
    assert row["evidence"]["exit_driver"] == "STOP_DOMINATED"
    assert row["metrics"]["avg_net_bps"] == -18.5
    assert payload["summary"]["exit_quality_fixes"] == 1


def test_operator_actions_repairs_contract_before_exit_quality():
    payload = build_operator_actions(
        activation={"rows": [_lane(activation_state="PAPER_RUNNING")]},
        route={"rows": [_lane(doctor_state="JOURNAL_ACTIVE")]},
        cadence={"rows": [_lane(cadence_state="EVALUATING_SIGNAL_SEEN")]},
        performance={
            "rows": [
                _lane(
                    state="PAPER_ACTIVE_NEGATIVE",
                    closed_trades=6,
                    net_pnl_usd=-4.2,
                )
            ]
        },
        contract_reconciler={
            "report_id": "paper_trade_contract_reconciler_v1",
            "rows": [
                _lane(
                    verdict="CONTRACT_BROKEN",
                    critical_violations=2,
                    top_violations={"exit_reduce_only_false": 1},
                    next_action="repair reduce-only exit contract",
                )
            ],
        },
        exit_autopsy={
            "rows": [
                _lane(
                    loss_driver="STOP_DOMINATED",
                    next_action="tighten entry permission",
                )
            ]
        },
        profile={"rows": [_lane(profile="paper", profile_state="PAPER_PROFILE_READY")]},
        causality={"rows": []},
        now=NOW,
    )

    row = payload["rows"][0]
    assert row["bucket"] == ACTION_REPAIR_CONTRACT
    assert row["severity"] == "P1"
    assert row["action"] == "repair reduce-only exit contract"
    assert row["evidence"]["contract_verdict"] == "CONTRACT_BROKEN"
    assert row["metrics"]["critical_contract_violations"] == 2
    assert payload["summary"]["contract_repairs"] == 1


def test_operator_actions_mines_alpha_when_contract_is_clean_but_negative():
    payload = build_operator_actions(
        activation={"rows": [_lane(activation_state="PAPER_RUNNING")]},
        route={"rows": [_lane(doctor_state="JOURNAL_ACTIVE")]},
        cadence={"rows": [_lane(cadence_state="EVALUATING_SIGNAL_SEEN")]},
        performance={
            "rows": [
                _lane(
                    state="PAPER_ACTIVE_NEGATIVE",
                    closed_trades=8,
                    net_pnl_usd=-3.1,
                )
            ]
        },
        contract_reconciler={
            "rows": [
                _lane(
                    verdict="CONTRACT_OK_NEGATIVE_ALPHA",
                    avg_net_bps=-12.5,
                    avg_fee_bps=7.2,
                    next_action="contract clean; mine entry and exit alpha",
                )
            ]
        },
        profile={"rows": [_lane(profile="paper", profile_state="PAPER_PROFILE_READY")]},
        causality={"rows": []},
        now=NOW,
    )

    row = payload["rows"][0]
    assert row["bucket"] == ACTION_MINE_CLEAN_ALPHA
    assert row["owner"] == "research"
    assert row["action"] == "contract clean; mine entry and exit alpha"
    assert row["metrics"]["contract_avg_net_bps"] == -12.5
    assert payload["summary"]["clean_alpha_mining"] == 1


def test_operator_actions_wait_for_live_signal_when_lane_online():
    payload = build_operator_actions(
        activation={"rows": [_lane(activation_state="PAPER_ONLINE_WAITING")]},
        route={"rows": [_lane(doctor_state="JOURNAL_ACTIVE")]},
        cadence={"rows": [_lane(cadence_state="EVALUATING_NO_SIGNAL")]},
        performance={"rows": []},
        profile={"rows": [_lane(profile="paper", profile_state="PAPER_PROFILE_READY")]},
        causality={
            "rows": [
                _lane(
                    scanner_state="NEAR_TRIGGER",
                    why_no_trade_minute="near trigger; waiting for close confirmation",
                )
            ]
        },
        now=NOW,
    )

    row = payload["rows"][0]
    assert row["bucket"] == ACTION_WAIT_FOR_SIGNAL
    assert row["owner"] == "market"
    assert "near trigger" in row["action"]


def test_operator_actions_cli_publishes_latest_and_feed_with_profile_derivation(tmp_path):
    activation = tmp_path / "activation.json"
    route = tmp_path / "route.json"
    cadence = tmp_path / "cadence.json"
    performance = tmp_path / "performance.json"
    causality = tmp_path / "causality.json"
    contract = tmp_path / "contract.json"
    out = tmp_path / "operator_actions_latest.json"
    feed = tmp_path / "operator_actions_feed.jsonl"

    activation.write_text(
        json.dumps(
            {
                "report_id": "paper_lane_activation_v1",
                "rows": [
                    _lane(
                        activation_state="PAPER_ONLINE_WAITING",
                        sizing_profiles={
                            "paper": {
                                "profile": "paper",
                                "risk_compatible": False,
                                "blockers": ["paper leverage exceeds manifest max"],
                                "requested_margin_usd": 100,
                                "requested_leverage": 25,
                                "requested_notional_usd": 2500,
                            }
                        },
                    )
                ],
            }
        ),
        encoding="utf-8",
    )
    route.write_text(json.dumps({"rows": [_lane(doctor_state="JOURNAL_ACTIVE")]}))
    cadence.write_text(json.dumps({"rows": [_lane(cadence_state="EVALUATING_NO_SIGNAL")]}))
    performance.write_text(json.dumps({"rows": []}))
    contract.write_text(json.dumps({"rows": []}))
    causality.write_text(json.dumps({"rows": []}))

    assert operator_actions_main(
        [
            "--once",
            "--activation",
            str(activation),
            "--route",
            str(route),
            "--cadence",
            str(cadence),
            "--performance",
            str(performance),
            "--contract-reconciler",
            str(contract),
            "--causality",
            str(causality),
            "--out",
            str(out),
            "--feed",
            str(feed),
            "--max-rows",
            "10",
        ]
    ) == 0

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["mode"] == "read_only_operator_action_queue"
    assert payload["source_report_ids"]["profile"] == "trade_profile_matrix_v1"
    assert payload["rows"][0]["bucket"] == ACTION_FIX_SIZE_PROFILE
    assert payload["rows"][0]["action"] == "paper leverage exceeds manifest max"
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False
    assert feed.read_text(encoding="utf-8").strip()
