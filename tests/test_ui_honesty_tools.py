"""UI honesty + tooling changes: fleet aggregate, active-lane journal filter,
per-exchange cost model, and the observation-only paper TP ladder join."""

from __future__ import annotations

import json

import math

from vnedge.dashboard.app import _cost_model_payload
from vnedge.dashboard.trade_journal import build_trade_journal
from vnedge.runtime.multi_lane import MultiLaneProvider, _fleet_aggregate, _json_safe


def _write(path, rows):
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")


def test_fleet_aggregate_splits_paper_shadow_and_excludes_errors():
    lanes = [
        {"lane_id": "a", "mode": "paper (live data)", "equity": 510.0,
         "realized_pnl": 10.0, "unrealized_pnl": 0.0, "fees_usd": 1.0, "risk_status": "ok"},
        {"lane_id": "b", "mode": "paper (live data)", "equity": 483.0,
         "realized_pnl": -17.0, "unrealized_pnl": 0.0, "fees_usd": 2.0, "risk_status": "ok"},
        {"lane_id": "c", "mode": "shadow (live data)", "equity": 500.0,
         "realized_pnl": 0.0, "unrealized_pnl": 0.0, "fees_usd": 0.0, "risk_status": "ok",
         "shadow_perf": {"net_usd": -131.0, "virtual_trades": 103}},
        {"lane_id": "err", "mode": "shadow (live data)", "equity": 0.0,
         "realized_pnl": 0.0, "unrealized_pnl": 0.0, "risk_status": "lane_error"},
    ]
    f = _fleet_aggregate(lanes)
    assert f["lanes"] == 3  # error lane excluded — a crash is not a $0 account
    assert f["equity"] == 1493.0
    assert f["starting_equity"] == 1500.0
    assert f["realized_pnl"] == -7.0
    assert f["paper_lanes"] == 2 and f["shadow_lanes"] == 1
    assert f["shadow_virtual_net_usd"] == -131.0 and f["shadow_virtual_trades"] == 103
    assert f["profitable_lanes"] == 1 and f["losing_lanes"] == 1
    assert f["return_pct"] < 0  # honest: the fleet is net negative
    # paper book excludes the static shadow account (500) — undiluted return
    assert f["paper_equity"] == 993.0
    assert f["paper_starting_equity"] == 1000.0
    assert round(f["paper_return_pct"], 2) == -0.70


def test_fleet_aggregate_attached_to_multilane_snapshot():
    provider = MultiLaneProvider("a")
    base = {"realized_pnl": 0.0, "unrealized_pnl": 0.0, "fills": 0, "fees_usd": 0.0,
            "risk_status": "ok", "feed_health": {"candles": "ok"}, "positions": [],
            "open_orders": [], "session": {}, "mode": "paper"}
    provider._publish("a", "binanceusdm", {**base, "equity": 510.0, "realized_pnl": 10.0})
    provider._publish("b", "bybit", {**base, "equity": 490.0, "realized_pnl": -10.0})
    snap = provider.latest()
    assert "fleet" in snap
    assert snap["fleet"]["equity"] == 1000.0
    assert snap["fleet"]["lanes"] == 2
    # the flat equity is still the primary lane only — fleet is the honest total
    assert snap["equity"] == 510.0


def test_journal_fleet_view_excludes_retired_lanes(tmp_path):
    # active lane: +10 realized
    _write(tmp_path / "live.fills.jsonl", [
        {"ts": "2026-07-27T00:00:00Z", "lane": "live", "side": "buy", "price": 100,
         "quantity": 1, "realized_pnl_usd": 0, "fee_usd": 0.05, "symbol": "BTC/USDT"},
        {"ts": "2026-07-27T01:00:00Z", "lane": "live", "side": "sell", "price": 110,
         "quantity": 1, "realized_pnl_usd": 10, "fee_usd": 0.05, "symbol": "BTC/USDT",
         "client_order_id": "exit-live-1"},
    ])
    # retired lane still on disk: -50 realized — must NOT pollute the fleet view
    _write(tmp_path / "retired.fills.jsonl", [
        {"ts": "2026-01-01T00:00:00Z", "lane": "retired", "side": "buy", "price": 100,
         "quantity": 1, "realized_pnl_usd": 0, "fee_usd": 0.05, "symbol": "ETH/USDT"},
        {"ts": "2026-01-01T01:00:00Z", "lane": "retired", "side": "sell", "price": 50,
         "quantity": 1, "realized_pnl_usd": -50, "fee_usd": 0.05, "symbol": "ETH/USDT"},
    ])
    snap = {"lane_id": "live", "lanes": [{"lane_id": "live"}]}
    payload = build_trade_journal(snapshot=snap, journal_dir=tmp_path, lane="", limit=200)
    assert payload["summary"]["fill_ledgers_scanned"] == 1
    assert payload["summary"]["active_lanes"] == 1
    assert payload["summary"]["actual_realized_pnl_usd"] == 10.0

    # no snapshot -> can't tell what's active -> fall back to scanning all
    fallback = build_trade_journal(snapshot=None, journal_dir=tmp_path, lane="", limit=200)
    assert fallback["summary"]["fill_ledgers_scanned"] == 2


def test_paper_tp_ladder_joins_by_client_order_id(tmp_path):
    _write(tmp_path / "pt.fills.jsonl", [
        {"ts": "2026-07-27T00:00:00Z", "lane": "pt", "side": "buy", "price": 100,
         "quantity": 1, "realized_pnl_usd": 0, "fee_usd": 0.05, "symbol": "ETH/USD"},
        {"ts": "2026-07-27T02:00:00Z", "lane": "pt", "side": "sell", "price": 106,
         "quantity": 1, "realized_pnl_usd": 6, "fee_usd": 0.05, "symbol": "ETH/USD",
         "client_order_id": "exit-abc"},
    ])
    _write(tmp_path / "pt.journal.jsonl", [
        {"ts": "2026-07-27T02:00:00Z", "kind": "live_paper_exit", "payload": {
            "reason": "take_profit", "state": "filled", "client_order_id": "exit-abc",
            "take_profit_levels": [102, 104, 108], "tp_reached": 2, "mfe_price": 106}},
    ])
    snap = {"lane_id": "pt", "lanes": [{"lane_id": "pt"}]}
    payload = build_trade_journal(snapshot=snap, journal_dir=tmp_path, lane="", limit=200)
    paper = [t for t in payload["closed_trades"] if t.get("kind") == "actual_closing_fill"]
    assert len(paper) == 1
    trade = paper[0]
    assert trade["take_profit_levels"] == [102, 104, 108]
    assert trade["tp_reached"] == 2
    assert trade["resolution"] == "take_profit"
    # captured bps still computed from the real entry->exit prices (gross)
    assert trade["captured_bps_basis"] == "gross"


def test_paper_tick_stop_exit_joins_by_client_order_id(tmp_path):
    _write(tmp_path / "pt.fills.jsonl", [
        {"ts": "2026-07-27T00:00:00Z", "lane": "pt", "side": "buy", "price": 100,
         "quantity": 1, "realized_pnl_usd": 0, "fee_usd": 0.05, "symbol": "ETH/USD",
         "client_order_id": "entry-abc"},
        {"ts": "2026-07-27T00:06:00Z", "lane": "pt", "side": "sell", "price": 98,
         "quantity": 1, "realized_pnl_usd": -2, "fee_usd": 0.05, "symbol": "ETH/USD",
         "client_order_id": "exit-stop"},
    ])
    _write(tmp_path / "pt.journal.jsonl", [
        {"ts": "2026-07-27T00:06:00Z", "kind": "tick_stop_exit", "payload": {
            "reason": "tick_stop", "state": "filled", "client_order_id": "exit-stop",
            "take_profit_levels": [102, 104, 108], "active_stop_price": 98,
            "breakeven_armed": False}},
    ])
    snap = {"lane_id": "pt", "lanes": [{"lane_id": "pt"}]}
    payload = build_trade_journal(snapshot=snap, journal_dir=tmp_path, lane="", limit=200)
    trade = next(t for t in payload["closed_trades"] if t.get("kind") == "actual_closing_fill")
    assert trade["resolution"] == "stop"
    assert trade["exit_reason"] == "tick_stop"
    assert trade["exit_metadata_source"] == "client_order_id"
    assert trade["exit_metadata_kind"] == "tick_stop_exit"
    assert trade["active_stop_price"] == 98


def test_legacy_tick_stop_exit_joins_by_lane_time_window(tmp_path):
    _write(tmp_path / "pt.fills.jsonl", [
        {"ts": "2026-07-27T00:00:00Z", "lane": "pt", "side": "buy", "price": 100,
         "quantity": 1, "realized_pnl_usd": 0, "fee_usd": 0.05, "symbol": "ETH/USD",
         "client_order_id": "entry-abc"},
        {"ts": "2026-07-27T00:06:30Z", "lane": "pt", "side": "sell", "price": 98,
         "quantity": 1, "realized_pnl_usd": -2, "fee_usd": 0.05, "symbol": "ETH/USD",
         "client_order_id": "exit-stop"},
    ])
    # Older tick-stop records did not carry the exit client_order_id. The
    # journal view must still recover the reason by lane + timestamp proximity.
    _write(tmp_path / "pt.journal.jsonl", [
        {"ts": "2026-07-27T00:06:00Z", "kind": "tick_stop_exit", "payload": {
            "reason": "tick_stop", "state": "filled", "active_stop_price": 98}},
    ])
    snap = {"lane_id": "pt", "lanes": [{"lane_id": "pt"}]}
    payload = build_trade_journal(snapshot=snap, journal_dir=tmp_path, lane="", limit=200)
    trade = next(t for t in payload["closed_trades"] if t.get("kind") == "actual_closing_fill")
    assert trade["resolution"] == "stop"
    assert trade["exit_reason"] == "tick_stop"
    assert trade["exit_metadata_source"] == "lane_time_window"


def test_reconciled_exit_plan_backfills_resolution_by_client_order_id(tmp_path):
    _write(tmp_path / "pt.fills.jsonl", [
        {"ts": "2026-07-27T00:00:00Z", "lane": "pt", "side": "buy", "price": 100,
         "quantity": 1, "realized_pnl_usd": 0, "fee_usd": 0.05, "symbol": "ETH/USD",
         "client_order_id": "entry-abc"},
        {"ts": "2026-07-27T00:06:00Z", "lane": "pt", "side": "sell", "price": 103,
         "quantity": 1, "realized_pnl_usd": 3, "fee_usd": 0.05, "symbol": "ETH/USD",
         "client_order_id": "exit-recon"},
    ])
    _write(tmp_path / "pt.journal.jsonl", [
        {"ts": "2026-07-27T00:06:00Z",
         "kind": "exit_plan_cleared_after_reconciliation",
         "payload": {
             "reason": "take_profit", "state": "filled", "client_order_id": "exit-recon"}},
    ])
    snap = {"lane_id": "pt", "lanes": [{"lane_id": "pt"}]}
    payload = build_trade_journal(snapshot=snap, journal_dir=tmp_path, lane="", limit=200)
    trade = next(t for t in payload["closed_trades"] if t.get("kind") == "actual_closing_fill")
    assert trade["resolution"] == "take_profit"
    assert trade["exit_metadata_source"] == "client_order_id"
    assert trade["exit_metadata_kind"] == "exit_plan_cleared_after_reconciliation"


def test_journal_shows_margin_and_leverage(tmp_path):
    # paper trade: entry order_intent carries leverage 5 + notional 500
    _write(tmp_path / "pt.fills.jsonl", [
        {"ts": "2026-07-27T00:00:00Z", "lane": "pt", "side": "buy", "price": 100,
         "quantity": 5, "realized_pnl_usd": 0, "fee_usd": 0.05, "symbol": "ETH/USD",
         "client_order_id": "entry-1"},
        {"ts": "2026-07-27T02:00:00Z", "lane": "pt", "side": "sell", "price": 106,
         "quantity": 5, "realized_pnl_usd": 30, "fee_usd": 0.05, "symbol": "ETH/USD",
         "client_order_id": "exit-1"},
    ])
    _write(tmp_path / "pt.journal.jsonl", [
        {"ts": "2026-07-27T00:00:00Z", "kind": "order_intent", "payload": {
            "client_order_id": "entry-1",
            "intent": {"leverage": 5.0, "notional_usd": 500.0, "quantity": 5}}},
        # a shadow lane trade: shadow_intent (lev 3 / notional 300) -> outcome
        {"ts": "2026-07-27T01:00:00Z", "kind": "shadow_intent", "payload": {
            "intent_key": "k1", "approved": True,
            "intent": {"leverage": 3.0, "notional_usd": 300.0}}},
        {"ts": "2026-07-27T03:00:00Z", "kind": "shadow_outcome", "payload": {
            "intent_key": "k1", "side": "long", "resolution": "take_profit",
            "entry_price": 50, "exit_price": 52, "virtual_net_usd": 1.5}},
    ])
    snap = {"lane_id": "pt", "lanes": [{"lane_id": "pt"}]}
    payload = build_trade_journal(snapshot=snap, journal_dir=tmp_path, lane="", limit=200)
    paper = next(t for t in payload["closed_trades"] if t.get("kind") == "actual_closing_fill")
    assert paper["leverage"] == 5.0
    assert paper["margin_usd"] == 100.0  # 500 notional / 5x
    shadow = next(t for t in payload["closed_trades"] if t.get("kind") == "shadow_outcome")
    assert shadow["leverage"] == 3.0
    assert shadow["margin_usd"] == 100.0  # 300 / 3x


def test_snapshot_never_serves_non_finite_floats():
    # A single inf/nan (e.g. a degenerate quote's spread_bps) must never reach
    # the wire — Starlette serializes with allow_nan=False, so it would 500
    # /state and drop /ws, silently freezing the whole dashboard on stale data.
    import json

    provider = MultiLaneProvider("a")
    provider._publish("a", "binanceusdm", {
        "equity": 500.0, "realized_pnl": 0.0, "unrealized_pnl": 0.0, "fills": 0,
        "fees_usd": 0.0, "risk_status": "ok", "feed_health": {"candles": "ok"},
        "positions": [], "open_orders": [], "session": {"nested": [float("nan")]},
        "mode": "shadow",
        "price": {"bid": 0, "ask": 0, "mid": 0, "spread_bps": float("inf")},
    })
    snap = provider.latest()
    json.dumps(snap, allow_nan=False)  # must not raise
    assert snap["lanes"][0]["price"]["spread_bps"] is None


def test_json_safe_scrubs_inf_and_nan_recursively():
    dirty = {"a": math.inf, "b": math.nan, "c": [-math.inf, 2.0], "d": {"e": 1.5}, "s": "x"}
    clean = _json_safe(dirty)
    assert clean["a"] is None and clean["b"] is None
    assert clean["c"] == [None, 2.0]
    assert clean["d"]["e"] == 1.5 and clean["s"] == "x"


def test_journal_enriches_trades_with_exchange_hold_and_lane_rollup(tmp_path):
    # paper trade with an open+close fill, on a venue-named lane, 2.5h apart
    _write(tmp_path / "quant_signal_pack_v1_bybit_ethusdt_shadow.fills.jsonl", [
        {"ts": "2026-07-27T00:00:00Z", "lane": "quant_signal_pack_v1_bybit_ethusdt_shadow",
         "side": "buy", "price": 100, "quantity": 1, "realized_pnl_usd": 0, "fee_usd": 0.05,
         "symbol": "ETH/USDT", "venue": "bybit"},
        {"ts": "2026-07-27T02:30:00Z", "lane": "quant_signal_pack_v1_bybit_ethusdt_shadow",
         "side": "sell", "price": 110, "quantity": 1, "realized_pnl_usd": 10, "fee_usd": 0.05,
         "symbol": "ETH/USDT", "venue": "bybit"},
    ])
    snap = {"lane_id": "quant_signal_pack_v1_bybit_ethusdt_shadow",
            "lanes": [{"lane_id": "quant_signal_pack_v1_bybit_ethusdt_shadow"}]}
    payload = build_trade_journal(snapshot=snap, journal_dir=tmp_path, lane="", limit=50)
    trade = next(t for t in payload["closed_trades"] if t.get("kind") == "actual_closing_fill")
    assert trade["exchange"] == "bybit"
    assert trade["hold_seconds"] == 9000.0  # 2.5h entry->exit
    # per-lane rollup present and correct
    roll = payload["summary"]["lane_pnl"]
    assert roll["quant_signal_pack_v1_bybit_ethusdt_shadow"]["closed"] == 1
    assert roll["quant_signal_pack_v1_bybit_ethusdt_shadow"]["net_usd"] > 0


def test_cost_model_exposes_all_venues():
    cm = _cost_model_payload()
    by = {e["exchange"]: e for e in cm["exchanges"]}
    assert {"binanceusdm", "bybit", "delta_india"} <= set(by)
    # every venue carries a human label and a round-trip cost the calculator uses
    for prof in by.values():
        assert prof["label"]
        assert prof["taker_round_trip_cost_bps"] > 0
    assert by["bybit"]["taker_bps"] == 5.5  # bybit's taker differs from binance
