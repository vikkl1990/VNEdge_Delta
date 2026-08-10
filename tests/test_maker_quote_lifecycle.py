"""Maker quote lifecycle auditor."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from vnedge.research.maker_quote_lifecycle import (
    STATE_NO_QUOTE_LIFECYCLE_WIRING,
    STATE_QUOTE_LIFECYCLE_PAPER_REVIEW,
    STATE_TAKER_FALLBACK_FORBIDDEN,
    build_maker_quote_lifecycle,
    main,
    publish_maker_quote_lifecycle,
)

NOW = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)


def _write_journal(root: Path, lane: str, rows: list[dict]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{lane}.journal.jsonl"
    path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )


def _started(strategy_id: str = "stealth_trail_bbp_v1") -> dict:
    return {
        "ts": NOW.isoformat(),
        "kind": "executor_started",
        "payload": {
            "executor_id": "exec-1",
            "exchange": "delta_india",
            "symbol": "ETH/USD:USD",
            "timeframe": "5m",
            "strategy_id": strategy_id,
            "expected_edge_bps": 48.0,
        },
    }


def _route(route: str, *, allowed: bool, net: float, coverage: float) -> dict:
    return {
        "ts": NOW.isoformat(),
        "kind": "executor_route_check",
        "payload": {
            "executor_id": "exec-1",
            "route": route,
            "allowed": allowed,
            "expected_edge_bps": net + 8.0,
            "cost_bps": 8.0,
            "net_edge_bps": net,
            "cost_coverage": coverage,
            "failed_checks": [] if allowed else ["cost_coverage 0.70 < required 1.50"],
        },
    }


def _maker_submitted() -> dict:
    return {
        "ts": NOW.isoformat(),
        "kind": "executor_maker_submitted",
        "payload": {"executor_id": "exec-1", "state": "acknowledged"},
    }


def _finished(state: str, *, maker_qty: float = 0.0, reason: str = "") -> dict:
    return {
        "ts": NOW.isoformat(),
        "kind": "executor_finished",
        "payload": {
            "executor_id": "exec-1",
            "state": state,
            "maker_filled_quantity": maker_qty,
            "taker_quantity": 0.0,
            "reason": reason,
        },
    }


def test_lifecycle_marks_review_ready_only_with_maker_and_paper_proof(tmp_path):
    journal_dir = tmp_path / "journals"
    lane = "stealth_trail_bbp_delta_eth_5m"
    rows = []
    for _ in range(6):
        rows.extend([
            _started(),
            _route("maker", allowed=True, net=45.0, coverage=6.6),
            _maker_submitted(),
            _finished("maker_filled", maker_qty=1.0, reason="maker_filled_before_fallback"),
        ])
    _write_journal(journal_dir, lane, rows)

    payload = build_maker_quote_lifecycle(
        journal_dir=journal_dir,
        performance={
            "report_id": "paper_lane_performance_v1",
            "rows": [{
                "lane_id": lane,
                "state": "PAPER_PROMOTION_CANDIDATE",
                "exchange": "delta_india",
                "symbol": "ETH/USD:USD",
                "timeframe": "5m",
                "strategy_id": "stealth_trail_bbp_v1",
                "closed_trades": 24,
                "profit_factor": 1.82,
                "net_pnl_usd": 14.2,
                "avg_closed_trade_net_bps": 31.4,
            }],
        },
        exit_autopsy={
            "report_id": "paper_trade_exit_autopsy_v1",
            "rows": [{"lane_id": lane, "avg_net_bps": 31.4, "avg_fee_bps": 6.0}],
        },
        now=NOW,
    )

    row = payload["rows"][0]
    assert row["lifecycle_state"] == STATE_QUOTE_LIFECYCLE_PAPER_REVIEW
    assert row["maker_attempts"] == 6
    assert row["maker_fill_rate_pct"] == 100.0
    assert row["queue_proof_state"] == "MAKER_FILL_AND_OUTCOME_OBSERVED"
    assert payload["summary"]["quote_lifecycle_review_ready"] == 1
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False


def test_lifecycle_blocks_taker_fallback_when_fee_math_does_not_clear(tmp_path):
    journal_dir = tmp_path / "journals"
    lane = "trend_delta_btc_5m"
    rows = []
    for _ in range(5):
        rows.extend([
            _started("trend_continuation_v1"),
            _route("maker", allowed=True, net=12.0, coverage=2.0),
            _maker_submitted(),
            _route("taker_fallback", allowed=False, net=5.0, coverage=0.7),
            _finished("taker_blocked", reason="taker_fallback_edge_below_hurdle"),
        ])
    _write_journal(journal_dir, lane, rows)

    payload = build_maker_quote_lifecycle(journal_dir=journal_dir, now=NOW)
    row = payload["rows"][0]

    assert row["lifecycle_state"] == STATE_TAKER_FALLBACK_FORBIDDEN
    assert row["taker_fallback_math"]["fallback_allowed_by_math"] is False
    assert row["next_action"] == "KEEP_MAKER_ONLY_DO_NOT_CHASE_TAKER"
    assert payload["summary"]["taker_fallback_forbidden"] == 1


def test_lifecycle_names_performance_lanes_with_no_executor_wiring(tmp_path):
    lane = "quant_signal_pack_binance_sol_15m"

    payload = build_maker_quote_lifecycle(
        journal_dir=tmp_path / "missing",
        performance={
            "rows": [{
                "lane_id": lane,
                "strategy_id": "quant_signal_pack_v1",
                "exchange": "binance",
                "symbol": "SOL/USDT:USDT",
                "timeframe": "15m",
                "closed_trades": 3,
                "profit_factor": 0.6,
                "net_pnl_usd": -1.5,
            }],
        },
        now=NOW,
    )

    row = payload["rows"][0]
    assert row["lifecycle_state"] == STATE_NO_QUOTE_LIFECYCLE_WIRING
    assert row["blockers"] == ["no executor_maker_submitted journal proof"]
    assert payload["summary"]["no_quote_lifecycle_wiring"] == 1


def test_publish_and_cli_write_feed_safe_artifacts(tmp_path):
    out = tmp_path / "latest.json"
    feed = tmp_path / "feed.jsonl"
    payload = build_maker_quote_lifecycle(journal_dir=tmp_path / "missing", now=NOW)

    publish_maker_quote_lifecycle(payload, out, feed)

    assert json.loads(out.read_text())["report_id"] == "maker_quote_lifecycle_v1"
    assert json.loads(feed.read_text().splitlines()[-1])["can_trade"] is False

    cli_out = tmp_path / "cli.json"
    rc = main([
        "--journal-dir", str(tmp_path / "missing"),
        "--out", str(cli_out),
        "--feed", str(tmp_path / "cli_feed.jsonl"),
    ])

    assert rc == 0
    assert json.loads(cli_out.read_text())["mode"] == "read_only_maker_quote_lifecycle"
