import json
from datetime import UTC, datetime, timedelta

from vnedge.research.trade_analyzer_os import (
    DIAG_ENTRY_CONTEXT_GAP,
    DIAG_GIVEBACK_DOMINATED,
    DIAG_HEALTHY_CAPTURE,
    DIAG_OVERNIGHT_HOLD_DRIFT,
    TradeAnalyzerOSConfig,
    build_trade_analyzer_os,
    publish_trade_analyzer_os,
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


def _lane_eval(ts, *, side="long", edge=45.0):
    payload = {
        "bar_ts": ts,
        "strategy_id": "stealth_trail_bbp_v1",
        "exchange": "delta_india",
        "symbol": "ETH/USD:USD",
        "timeframe": "5m",
        "mode": "paper",
        "fired": True,
        "signal_reason": "stealth trail bbp long",
        "skip_reason": None,
        "signal": {
            "side": side,
            "stop_price": 99.0 if side == "long" else 101.0,
            "take_profit_price": 101.0 if side == "long" else 99.0,
            "reason": "stealth trail bbp long",
        },
        "features": {
            "expected_net_edge_bps_long": edge if side == "long" else 0.0,
            "expected_net_edge_bps_short": edge if side == "short" else 0.0,
        },
        "thresholds": {"min_expected_net_edge_bps": 25.0},
        "backfill": False,
    }
    return _journal_record("lane_eval", payload, ts)


def _closed_trades(tmp_path, lane, specs, *, emit_eval=True):
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
        entry_dt = base + timedelta(minutes=idx * 20)
        exit_dt = entry_dt + timedelta(seconds=float(spec.get("hold_seconds", 300.0)))
        if emit_eval:
            journals.append(_lane_eval((entry_dt - timedelta(minutes=1)).isoformat()))
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
        journals.append(
            _journal_record(
                "live_paper_exit",
                {
                    "client_order_id": exit_coid,
                    "reason": spec.get("reason", "take_profit"),
                    "state": "filled",
                    "tp_reached": spec.get("tp_reached", 1),
                    "take_profit_levels": spec.get("take_profit_levels", []),
                    "mfe_price": spec.get("mfe_price"),
                    "active_stop_price": spec.get("active_stop_price"),
                    "breakeven_armed": spec.get("breakeven_armed"),
                },
                exit_dt.isoformat(),
            )
        )
    _write_jsonl(tmp_path / f"{lane}.fills.jsonl", fills)
    _write_jsonl(tmp_path / f"{lane}.journal.jsonl", journals)


def test_trade_analyzer_os_marks_giveback_dominated_lane(tmp_path):
    _closed_trades(
        tmp_path,
        "giveback_lane_5m",
        [
            {"exit_price": 100.05, "realized": 0.05, "mfe_price": 100.50},
            {"exit_price": 100.04, "realized": 0.04, "mfe_price": 100.45},
            {"exit_price": 100.03, "realized": 0.03, "mfe_price": 100.40},
        ],
    )

    payload = build_trade_analyzer_os(
        journal_dir=tmp_path,
        config=TradeAnalyzerOSConfig(min_closed_trades=3),
        now=datetime(2026, 7, 30, tzinfo=UTC),
    )

    row = payload["rows"][0]
    assert row["primary_diagnosis"] == DIAG_GIVEBACK_DOMINATED
    assert row["giveback_trades"] == 3
    assert row["avg_mfe_bps"] > 40.0
    assert row["avg_giveback_bps"] > 30.0
    assert "breakeven/profit-lock" in row["next_action"]
    assert payload["summary"]["giveback_dominated"] == 1
    assert "Giveback" in payload["operator_answer"]
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False


def test_trade_analyzer_os_marks_missing_entry_context_first(tmp_path):
    _closed_trades(
        tmp_path,
        "context_gap_lane_5m",
        [
            {"exit_price": 99.7, "realized": -0.3, "reason": "stop"},
            {"exit_price": 99.6, "realized": -0.4, "reason": "stop"},
            {"exit_price": 99.8, "realized": -0.2, "reason": "stop"},
        ],
        emit_eval=False,
    )

    payload = build_trade_analyzer_os(
        journal_dir=tmp_path,
        config=TradeAnalyzerOSConfig(min_closed_trades=3),
        now=datetime(2026, 7, 30, tzinfo=UTC),
    )

    row = payload["rows"][0]
    assert row["primary_diagnosis"] == DIAG_ENTRY_CONTEXT_GAP
    assert "signal-to-order linkage" in row["next_action"]
    assert payload["summary"]["entry_context_gap"] == 1
    assert "signal-to-order linkage" in payload["operator_answer"]


def test_trade_analyzer_os_marks_healthy_capture_when_entries_and_exits_align(tmp_path):
    _closed_trades(
        tmp_path,
        "healthy_lane_5m",
        [
            {"exit_price": 101.0, "realized": 1.0, "mfe_price": 101.1},
            {"exit_price": 101.2, "realized": 1.2, "mfe_price": 101.3},
            {"exit_price": 100.9, "realized": 0.9, "mfe_price": 101.0},
        ],
    )

    payload = build_trade_analyzer_os(
        journal_dir=tmp_path,
        config=TradeAnalyzerOSConfig(min_closed_trades=3, min_profit_factor=1.0),
        now=datetime(2026, 7, 30, tzinfo=UTC),
    )

    row = payload["rows"][0]
    assert row["primary_diagnosis"] == DIAG_HEALTHY_CAPTURE
    assert row["healthy_trades"] == 3
    assert row["net_pnl_usd"] > 0
    assert payload["summary"]["healthy_capture"] == 1
    assert "post-fee moves" in payload["operator_answer"]


def test_trade_analyzer_os_flags_overnight_hold_drift(tmp_path):
    _closed_trades(
        tmp_path,
        "overnight_lane_5m",
        [
            {
                "exit_price": 100.7,
                "realized": 0.7,
                "mfe_price": 100.9,
                "hold_seconds": 25 * 60 * 60,
            }
        ],
    )

    payload = build_trade_analyzer_os(
        journal_dir=tmp_path,
        config=TradeAnalyzerOSConfig(min_closed_trades=1),
        now=datetime(2026, 7, 31, tzinfo=UTC),
    )

    row = payload["rows"][0]
    assert row["primary_diagnosis"] == DIAG_OVERNIGHT_HOLD_DRIFT
    assert row["overnight_hold_trades"] == 1
    assert "daily factory close" in row["next_action"]
    assert payload["summary"]["overnight_hold_drift"] == 1


def test_trade_analyzer_os_publish_writes_latest_and_feed(tmp_path):
    payload = build_trade_analyzer_os(
        journal_dir=tmp_path,
        now=datetime(2026, 7, 30, tzinfo=UTC),
    )
    out = tmp_path / "latest.json"
    feed = tmp_path / "feed.jsonl"

    publish_trade_analyzer_os(payload, out, feed)

    assert json.loads(out.read_text())["mode"] == "read_only_trade_analyzer_os"
    assert json.loads(feed.read_text().strip())["report_id"] == "trade_analyzer_os_v1"
