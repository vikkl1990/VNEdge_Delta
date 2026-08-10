import json
from datetime import UTC, datetime, timedelta

from vnedge.research.paper_trade_entry_autopsy import (
    STATE_CAPTURE_HEALTHY,
    STATE_CONTEXT_MISSING,
    STATE_DIRECTION_DRIFT,
    STATE_SIGNAL_STALE,
    PaperTradeEntryAutopsyConfig,
    build_paper_trade_entry_autopsy,
    publish_paper_trade_entry_autopsy,
)


def _write_jsonl(path, rows):
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _journal_record(kind, payload, ts):
    return {"ts": ts, "kind": kind, "payload": payload}


def _fill(ts, coid, side, price, realized, *, fee=0.05):
    return {
        "ts": ts,
        "mode": "paper",
        "venue": "delta_india",
        "strategy_id": "stealth_trail_bbp_v1",
        "symbol": "ETH/USD:USD",
        "side": side,
        "quantity": 1.0,
        "price": price,
        "fee_usd": fee,
        "realized_pnl_usd": realized,
        "client_order_id": coid,
    }


def _lane_eval(ts, *, side="long", fired=True, edge=40.0, reason="bbp trail long"):
    payload = {
        "bar_ts": ts,
        "strategy_id": "stealth_trail_bbp_v1",
        "exchange": "delta_india",
        "symbol": "ETH/USD:USD",
        "timeframe": "5m",
        "mode": "paper",
        "fired": fired,
        "signal_reason": reason if fired else None,
        "skip_reason": None if fired else "no threshold features",
        "signal": {
            "side": side,
            "stop_price": 99.0 if side == "long" else 101.0,
            "take_profit_price": 101.0 if side == "long" else 99.0,
            "reason": reason,
        } if fired else None,
        "features": {
            "expected_net_edge_bps_long": edge if side == "long" else 0.0,
            "expected_net_edge_bps_short": edge if side == "short" else 0.0,
        },
        "thresholds": {"min_expected_net_edge_bps": 25.0},
        "backfill": False,
    }
    return _journal_record("lane_eval", payload, ts)


def _closed_trades(tmp_path, lane, specs, *, eval_builder=None):
    base = datetime(2026, 7, 30, 0, 0, tzinfo=UTC)
    fills = []
    journals = [
        _journal_record(
            "paper_lane_heartbeat",
            {
                "exchange": "delta_india",
                "symbol": "ETH/USD:USD",
                "timeframe": "5m",
                "strategy_id": "stealth_trail_bbp_v1",
                "mode": "paper",
            },
            base.isoformat(),
        )
    ]
    for idx, spec in enumerate(specs, start=1):
        entry_dt = base + timedelta(minutes=idx * 60)
        exit_dt = entry_dt + timedelta(minutes=5)
        if eval_builder is not None:
            journals.append(eval_builder(idx, entry_dt))
        entry_coid = f"{lane}_entry_{idx}"
        exit_coid = f"{lane}_exit_{idx}"
        fills.append(_fill(entry_dt.isoformat(), entry_coid, "buy", 100.0, 0.0))
        fills.append(
            _fill(
                exit_dt.isoformat(),
                exit_coid,
                "sell",
                float(spec["exit_price"]),
                float(spec["realized"]),
            )
        )
    _write_jsonl(tmp_path / f"{lane}.fills.jsonl", fills)
    _write_jsonl(tmp_path / f"{lane}.journal.jsonl", journals)


def test_entry_autopsy_marks_healthy_fresh_same_direction_entries(tmp_path):
    _closed_trades(
        tmp_path,
        "healthy_lane_5m",
        [
            {"exit_price": 101.0, "realized": 1.0},
            {"exit_price": 101.1, "realized": 1.1},
            {"exit_price": 100.9, "realized": 0.9},
        ],
        eval_builder=lambda _idx, entry_dt: _lane_eval(
            (entry_dt - timedelta(minutes=1)).isoformat(),
            side="long",
            edge=45.0,
        ),
    )

    payload = build_paper_trade_entry_autopsy(
        journal_dir=tmp_path,
        config=PaperTradeEntryAutopsyConfig(min_closed_trades=3),
        now=datetime(2026, 7, 30, tzinfo=UTC),
    )

    row = payload["rows"][0]
    assert row["entry_state"] == STATE_CAPTURE_HEALTHY
    assert row["closed_trades"] == 3
    assert row["matched_signal_count"] == 3
    assert row["avg_entry_delay_bars"] == 0.2
    assert row["missing_context_rate"] == 0.0
    assert row["direction_drift_rate"] == 0.0
    assert row["avg_net_bps"] > 25.0
    assert payload["summary"]["healthy_entries"] == 1
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False


def test_entry_autopsy_marks_stale_signal_entries(tmp_path):
    _closed_trades(
        tmp_path,
        "stale_lane_5m",
        [
            {"exit_price": 99.5, "realized": -0.5},
            {"exit_price": 99.4, "realized": -0.6},
            {"exit_price": 99.6, "realized": -0.4},
        ],
        eval_builder=lambda _idx, entry_dt: _lane_eval(
            (entry_dt - timedelta(minutes=30)).isoformat(),
            side="long",
            edge=35.0,
        ),
    )

    payload = build_paper_trade_entry_autopsy(
        journal_dir=tmp_path,
        config=PaperTradeEntryAutopsyConfig(min_closed_trades=3),
        now=datetime(2026, 7, 30, tzinfo=UTC),
    )

    row = payload["rows"][0]
    assert row["entry_state"] == STATE_SIGNAL_STALE
    assert row["stale_entry_rate"] == 1.0
    assert row["avg_entry_delay_bars"] == 6.0
    assert "signal TTL" in row["next_action"]
    assert payload["summary"]["stale_signal_lanes"] == 1


def test_entry_autopsy_marks_missing_signal_context(tmp_path):
    _closed_trades(
        tmp_path,
        "missing_context_lane_5m",
        [
            {"exit_price": 99.5, "realized": -0.5},
            {"exit_price": 99.4, "realized": -0.6},
            {"exit_price": 99.6, "realized": -0.4},
        ],
    )

    payload = build_paper_trade_entry_autopsy(
        journal_dir=tmp_path,
        config=PaperTradeEntryAutopsyConfig(min_closed_trades=3),
        now=datetime(2026, 7, 30, tzinfo=UTC),
    )

    row = payload["rows"][0]
    assert row["entry_state"] == STATE_CONTEXT_MISSING
    assert row["missing_context_rate"] == 1.0
    assert "signal-to-order linkage" in row["next_action"]
    assert payload["summary"]["missing_context_lanes"] == 1


def test_entry_autopsy_marks_signal_direction_drift(tmp_path):
    _closed_trades(
        tmp_path,
        "direction_drift_lane_5m",
        [
            {"exit_price": 99.5, "realized": -0.5},
            {"exit_price": 99.4, "realized": -0.6},
            {"exit_price": 99.6, "realized": -0.4},
        ],
        eval_builder=lambda _idx, entry_dt: _lane_eval(
            (entry_dt - timedelta(minutes=1)).isoformat(),
            side="short",
            edge=40.0,
        ),
    )

    payload = build_paper_trade_entry_autopsy(
        journal_dir=tmp_path,
        config=PaperTradeEntryAutopsyConfig(min_closed_trades=3),
        now=datetime(2026, 7, 30, tzinfo=UTC),
    )

    row = payload["rows"][0]
    assert row["entry_state"] == STATE_DIRECTION_DRIFT
    assert row["direction_drift_rate"] == 1.0
    assert "side mapping" in row["next_action"]
    assert payload["summary"]["direction_drift_lanes"] == 1


def test_entry_autopsy_publish_writes_latest_and_feed(tmp_path):
    payload = build_paper_trade_entry_autopsy(
        journal_dir=tmp_path,
        now=datetime(2026, 7, 30, tzinfo=UTC),
    )
    out = tmp_path / "latest.json"
    feed = tmp_path / "feed.jsonl"

    publish_paper_trade_entry_autopsy(payload, out, feed)

    assert json.loads(out.read_text())["mode"] == "read_only_paper_trade_entry_autopsy"
    assert json.loads(feed.read_text().strip())["report_id"] == "paper_trade_entry_autopsy_v1"
