from __future__ import annotations

import json
from datetime import UTC, datetime

from vnedge.research.paper_lane_root_cause import (
    ROOT_ENTRY_QUALITY_BLOCKED,
    ROOT_EXIT_OR_CAPTURE_BLOCKED,
    ROOT_NO_CLOSED_PAPER_TRADES,
    ROOT_READY_FOR_HUMAN_REVIEW,
    ROOT_ROUTE_OR_JOURNAL_BROKEN,
    STAGE_ENTRY,
    STAGE_EXIT,
    STAGE_MARKET,
    STAGE_ROUTE,
    build_paper_lane_root_cause,
    publish_paper_lane_root_cause,
    render_report,
)


def _lane(**updates):
    row = {
        "lane_key": "stealth|delta_india|ETH/USD|5m",
        "exchange": "delta_india",
        "symbol": "ETH/USD",
        "timeframe": "5m",
        "strategy_id": "stealth_trail_bbp_v1",
        "trial_id": "stealth_delta_eth_5m",
    }
    row.update(updates)
    return row


def test_route_failure_wins_over_negative_performance():
    payload = build_paper_lane_root_cause(
        activation={"rows": [_lane(activation_state="PAPER_RUNNING")]},
        route={"rows": [_lane(doctor_state="ROUTE_READY_JOURNAL_MISSING")]},
        cadence={"rows": [_lane(cadence_state="EVALUATING_NO_SIGNAL")]},
        performance={
            "rows": [
                _lane(
                    state="PAPER_ACTIVE_NEGATIVE",
                    closed_trades=12,
                    net_pnl_usd=-9.25,
                    next_action="mine exits",
                )
            ]
        },
        now=datetime(2026, 8, 1, tzinfo=UTC),
    )

    row = payload["rows"][0]
    assert row["root_cause"] == ROOT_ROUTE_OR_JOURNAL_BROKEN
    assert row["stage"] == STAGE_ROUTE
    assert row["severity"] == "P1"
    assert row["can_trade"] is False
    assert payload["can_promote"] is False
    assert payload["summary"]["route_blocked"] == 1


def test_entry_autopsy_blocks_before_exit_when_present():
    payload = build_paper_lane_root_cause(
        activation={"rows": [_lane(activation_state="PAPER_RUNNING")]},
        route={"rows": [_lane(doctor_state="JOURNAL_ACTIVE")]},
        cadence={"rows": [_lane(cadence_state="EVALUATING_SIGNAL_SEEN")]},
        entry_autopsy={
            "rows": [
                _lane(
                    entry_state="ENTRY_DIRECTION_DRIFT",
                    primary_failure="signal flipped before fill",
                    next_action="tighten entry freshness",
                )
            ]
        },
        exit_autopsy={
            "rows": [
                _lane(
                    loss_driver="STOP_DOMINATED",
                    next_action="repair trailing stop",
                )
            ]
        },
    )

    row = payload["rows"][0]
    assert row["root_cause"] == ROOT_ENTRY_QUALITY_BLOCKED
    assert row["stage"] == STAGE_ENTRY
    assert row["action"] == "tighten entry freshness"
    assert "ENTRY_DIRECTION_DRIFT" in row["blockers"]
    assert payload["summary"]["entry_blocked"] == 1


def test_exit_capture_issue_is_separate_from_negative_pnl():
    payload = build_paper_lane_root_cause(
        activation={"rows": [_lane(activation_state="PAPER_RUNNING")]},
        route={"rows": [_lane(doctor_state="JOURNAL_ACTIVE")]},
        cadence={"rows": [_lane(cadence_state="EVALUATING_SIGNAL_SEEN")]},
        performance={
            "rows": [
                _lane(
                    state="PAPER_ACTIVE_NEGATIVE",
                    closed_trades=9,
                    net_pnl_usd=-3.4,
                    profit_factor=0.72,
                )
            ]
        },
        exit_autopsy={
            "rows": [
                _lane(
                    loss_driver="TP_CAPTURE_WEAK",
                    avg_net_bps=-14.2,
                    next_action="use trailing capture before TP3 wait",
                )
            ]
        },
    )

    row = payload["rows"][0]
    assert row["root_cause"] == ROOT_EXIT_OR_CAPTURE_BLOCKED
    assert row["stage"] == STAGE_EXIT
    assert row["metrics"]["closed_trades"] == 9
    assert row["metrics"]["avg_net_bps"] == -14.2
    assert payload["summary"]["exit_blocked"] == 1


def test_review_ready_and_no_outcome_rows_remain_read_only(tmp_path):
    review = _lane(
        lane_key="pullback|binance|BTC/USDT|4h",
        exchange="binance",
        symbol="BTC/USDT",
        timeframe="4h",
        strategy_id="quantified_pullback_reversion_v1",
    )
    waiting = _lane(
        lane_key="fvg|bybit|SOL/USDT|15m",
        exchange="bybit",
        symbol="SOL/USDT",
        timeframe="15m",
        strategy_id="fvg_liquidity_breakout_v1",
    )
    payload = build_paper_lane_root_cause(
        activation={
            "rows": [
                dict(review, activation_state="PAPER_RUNNING"),
                dict(waiting, activation_state="PAPER_ONLINE_WAITING"),
            ]
        },
        route={
            "rows": [
                dict(review, doctor_state="JOURNAL_ACTIVE"),
                dict(waiting, doctor_state="JOURNAL_ACTIVE"),
            ]
        },
        cadence={
            "rows": [
                dict(review, cadence_state="EVALUATING_SIGNAL_SEEN"),
                dict(waiting, cadence_state="EVALUATING_NO_SIGNAL"),
            ]
        },
        performance={
            "rows": [
                dict(
                    review,
                    state="PAPER_PROMOTION_CANDIDATE",
                    closed_trades=24,
                    net_pnl_usd=12.0,
                ),
                dict(waiting, state="PAPER_ONLINE_NO_TRADES", closed_trades=0),
            ]
        },
    )

    by_lane = {r["lane_key"]: r for r in payload["rows"]}
    assert by_lane["pullback|binance|btc/usdt|4h"]["root_cause"] == (
        ROOT_READY_FOR_HUMAN_REVIEW
    )
    assert by_lane["fvg|bybit|sol/usdt|15m"]["root_cause"] == (
        ROOT_NO_CLOSED_PAPER_TRADES
    )
    assert by_lane["fvg|bybit|sol/usdt|15m"]["stage"] == STAGE_MARKET
    assert payload["policy"]["can_trade"] is False
    assert payload["policy"]["can_promote"] is False

    out = tmp_path / "root.json"
    feed = tmp_path / "root.jsonl"
    publish_paper_lane_root_cause(payload, out, feed)
    assert json.loads(out.read_text())["report_id"] == "paper_lane_root_cause_v1"
    assert json.loads(feed.read_text().splitlines()[0])["can_trade"] is False
    assert "Paper lane root-cause matrix" in render_report(payload)
