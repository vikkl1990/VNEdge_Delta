"""Promotion review runbook: operator packet around red-team evidence."""

import json

from vnedge.research.promotion_review_runbook import (
    ACTION_BLOCK,
    ACTION_REVIEW,
    STATE_BLOCKED,
    STATE_HUMAN_REVIEW_READY,
    build_promotion_review_runbook,
    run_once,
)


def test_runbook_blocks_critical_charge_and_stays_powerless():
    payload = build_promotion_review_runbook({
        "red_team_id": "promotion_red_team_v1",
        "briefs": [{
            "strategy_id": "funding_mr",
            "exchange": "binanceusdm",
            "symbol": "BTC/USDT:USDT",
            "input_verdict": "PASS",
            "recommendation": "DO_NOT_PROMOTE_YET",
            "critical_count": 1,
            "warn_count": 0,
            "charges": [{
                "name": "fee_drag",
                "severity": "critical",
                "claim": "fees are 1.20x the net profit",
                "evidence": {"fee_to_net_ratio": 1.2},
                "what_would_answer_it": "maker route proof",
            }],
        }],
    })

    row = payload["rows"][0]
    assert payload["summary"]["blocked_by_red_team"] == 1
    assert payload["summary"]["next_action"] == ACTION_BLOCK
    assert row["review_state"] == STATE_BLOCKED
    assert row["primary_charge"] == "fee_drag"
    assert row["evidence"]["fee_drag"]["fee_to_net_ratio"] == 1.2
    assert any("maker-route" in step for step in row["runbook_steps"])
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False
    assert row["can_trade"] is False
    assert row["can_promote"] is False


def test_runbook_marks_single_info_caveat_as_human_review_ready():
    payload = build_promotion_review_runbook({
        "briefs": [{
            "strategy_id": "strong",
            "exchange": "bybit",
            "symbol": "ETH/USDT:USDT",
            "input_verdict": "PASS",
            "recommendation": "DEFENSIBLE_BUT_HUMAN_GATED",
            "critical_count": 0,
            "warn_count": 0,
            "charges": [{
                "name": "single_symbol",
                "severity": "info",
                "claim": "evidence is a single symbol",
                "evidence": {"symbol": "ETH/USDT:USDT"},
                "what_would_answer_it": "second symbol proof",
            }],
        }],
    })

    row = payload["rows"][0]
    assert row["review_state"] == STATE_HUMAN_REVIEW_READY
    assert row["next_action"] == ACTION_REVIEW
    assert payload["summary"]["human_review_ready"] == 1
    assert "not live approval" in payload["operator_answer"]


def test_run_once_publishes_red_team_and_runbook_from_experiment_feed(tmp_path):
    feed = tmp_path / "feed.jsonl"
    feed.write_text(
        json.dumps({
            "strategy": "s1",
            "symbol": "BTC/USDT:USDT",
            "exchange": "binanceusdm",
            "timeframe": "1h",
            "verdict": "PASS",
            "updated": "2026-07-31T00:00:00+00:00",
            "oos_net_usd": 10.0,
            "oos_trades": 40,
            "total_fees_usd": 12.0,
        }) + "\n",
        encoding="utf-8",
    )
    red_team_out = tmp_path / "promotion_red_team_latest.json"
    out = tmp_path / "promotion_review_runbook_latest.json"
    journal = tmp_path / "promotion_review_runbook_feed.jsonl"

    payload = run_once(
        feed_path=feed,
        burn_registry_path=tmp_path / "burn.jsonl",
        paper_trials_dir=tmp_path / "paper_trials",
        red_team_out=red_team_out,
        out=out,
        feed=journal,
    )

    assert payload["summary"]["blocked_by_red_team"] == 1
    assert out.exists()
    assert red_team_out.exists()
    assert journal.exists()
    persisted = json.loads(out.read_text(encoding="utf-8"))
    assert persisted["runbook_id"] == "promotion_review_runbook_v1"
    assert persisted["rows"][0]["primary_charge"] == "fee_drag"
