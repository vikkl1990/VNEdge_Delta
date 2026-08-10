import json
from datetime import UTC, datetime, timedelta

from vnedge.research.paper_trade_exit_autopsy import (
    DRIVER_FEE_WALL_DOMINATED,
    DRIVER_STOP_DOMINATED,
    DRIVER_STRATEGY_EXIT_DOMINATED,
    DRIVER_STRATEGY_EXIT_HEALTHY,
    DRIVER_TP_CAPTURE_WEAK,
    PaperTradeExitAutopsyConfig,
    build_paper_trade_exit_autopsy,
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


def _closed_trades(tmp_path, lane, exits):
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
    for idx, exit_spec in enumerate(exits, start=1):
        entry_ts = (base + timedelta(minutes=idx * 10)).isoformat()
        exit_ts = (base + timedelta(minutes=idx * 10 + 5)).isoformat()
        entry_coid = f"{lane}_entry_{idx}"
        exit_coid = f"{lane}_exit_{idx}"
        fills.append(_fill(entry_ts, entry_coid, "buy", 100.0, 0.0))
        fills.append(
            _fill(
                exit_ts,
                exit_coid,
                "sell",
                float(exit_spec["exit_price"]),
                float(exit_spec["realized"]),
            )
        )
        journals.append(
            _journal_record(
                "live_paper_exit",
                {
                    "client_order_id": exit_coid,
                    "reason": exit_spec["reason"],
                    "tp_reached": exit_spec.get("tp_reached", 0),
                    "take_profit_levels": exit_spec.get("take_profit_levels", []),
                },
                exit_ts,
            )
        )
    _write_jsonl(tmp_path / f"{lane}.fills.jsonl", fills)
    _write_jsonl(tmp_path / f"{lane}.journal.jsonl", journals)


def test_exit_autopsy_marks_stop_dominated_paper_lane(tmp_path):
    _closed_trades(
        tmp_path,
        "stop_lane",
        [
            {"reason": "stop", "exit_price": 99.5, "realized": -0.5},
            {"reason": "stop", "exit_price": 99.4, "realized": -0.6},
            {"reason": "stop", "exit_price": 99.6, "realized": -0.4},
        ],
    )

    payload = build_paper_trade_exit_autopsy(
        journal_dir=tmp_path,
        config=PaperTradeExitAutopsyConfig(min_closed_trades=3),
        now=datetime(2026, 7, 30, tzinfo=UTC),
    )

    row = payload["rows"][0]
    assert row["loss_driver"] == DRIVER_STOP_DOMINATED
    assert row["closed_trades"] == 3
    assert row["stop_rate"] == 1.0
    assert row["net_pnl_usd"] < 0
    assert "tighten entry" in row["next_action"]
    assert payload["summary"]["stop_dominated"] == 1
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False


def test_exit_autopsy_uses_legacy_tick_stop_reason_from_journal(tmp_path):
    base = datetime(2026, 7, 30, 0, 0, tzinfo=UTC)
    lane = "legacy_tick_lane"
    _write_jsonl(tmp_path / f"{lane}.fills.jsonl", [
        _fill(base.isoformat(), "entry-1", "buy", 100.0, 0.0),
        _fill((base + timedelta(minutes=5)).isoformat(), "exit-1", "sell", 99.0, -1.0),
    ])
    _write_jsonl(tmp_path / f"{lane}.journal.jsonl", [
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
        ),
        _journal_record(
            "tick_stop_exit",
            {"reason": "tick_stop", "state": "filled", "active_stop_price": 99.0},
            (base + timedelta(minutes=5)).isoformat(),
        ),
    ])

    payload = build_paper_trade_exit_autopsy(
        journal_dir=tmp_path,
        config=PaperTradeExitAutopsyConfig(min_closed_trades=1),
        now=datetime(2026, 7, 30, tzinfo=UTC),
    )

    row = payload["rows"][0]
    assert row["missing_resolution_rate"] == 0.0
    assert row["resolution_counts"]["stop"] == 1
    assert payload["summary"]["metadata_gaps"] == 0


def test_exit_autopsy_marks_fee_wall_dominated_small_winners(tmp_path):
    _closed_trades(
        tmp_path,
        "fee_lane",
        [
            {"reason": "take_profit", "exit_price": 100.05, "realized": 0.05, "tp_reached": 1},
            {"reason": "take_profit", "exit_price": 100.05, "realized": 0.05, "tp_reached": 1},
            {"reason": "take_profit", "exit_price": 100.05, "realized": 0.05, "tp_reached": 1},
        ],
    )

    payload = build_paper_trade_exit_autopsy(
        journal_dir=tmp_path,
        config=PaperTradeExitAutopsyConfig(min_closed_trades=3, fee_wall_bps=8.0),
        now=datetime(2026, 7, 30, tzinfo=UTC),
    )

    row = payload["rows"][0]
    assert row["loss_driver"] == DRIVER_FEE_WALL_DOMINATED
    assert row["avg_gross_captured_bps"] > 0
    assert row["avg_net_bps"] < 0
    assert row["fees_usd"] > abs(row["net_pnl_usd"])
    assert "maker-first" in row["next_action"]
    assert payload["summary"]["fee_wall_dominated"] == 1


def test_exit_autopsy_marks_weak_take_profit_capture(tmp_path):
    _closed_trades(
        tmp_path,
        "tp_lane",
        [
            {
                "reason": "take_profit",
                "exit_price": 100.3,
                "realized": 0.3,
                "tp_reached": 1,
                "take_profit_levels": [100.3, 100.6, 100.9],
            },
            {
                "reason": "take_profit",
                "exit_price": 100.3,
                "realized": 0.3,
                "tp_reached": 1,
                "take_profit_levels": [100.3, 100.6, 100.9],
            },
            {
                "reason": "take_profit",
                "exit_price": 100.3,
                "realized": 0.3,
                "tp_reached": 1,
                "take_profit_levels": [100.3, 100.6, 100.9],
            },
        ],
    )

    payload = build_paper_trade_exit_autopsy(
        journal_dir=tmp_path,
        config=PaperTradeExitAutopsyConfig(min_closed_trades=3, min_avg_net_bps=25.0),
        now=datetime(2026, 7, 30, tzinfo=UTC),
    )

    row = payload["rows"][0]
    assert row["loss_driver"] == DRIVER_TP_CAPTURE_WEAK
    assert row["take_profit_rate"] == 1.0
    assert row["avg_net_bps"] < 25.0
    assert row["tp_reached_counts"]["1"] == 3
    assert "scale-out" in row["next_action"]


def test_exit_autopsy_scores_healthy_strategy_managed_exits(tmp_path):
    _closed_trades(
        tmp_path,
        "smart_exit_lane",
        [
            {"reason": "strategy_neutral_long", "exit_price": 101.0, "realized": 1.0},
            {"reason": "strategy_neutral_short", "exit_price": 99.0, "realized": 1.0},
            {"reason": "strategy_reversal_long", "exit_price": 101.2, "realized": 1.2},
        ],
    )

    payload = build_paper_trade_exit_autopsy(
        journal_dir=tmp_path,
        config=PaperTradeExitAutopsyConfig(min_closed_trades=3, fee_wall_bps=8.0),
        now=datetime(2026, 7, 30, tzinfo=UTC),
    )

    row = payload["rows"][0]
    assert row["loss_driver"] == DRIVER_STRATEGY_EXIT_HEALTHY
    assert row["strategy_exit_count"] == 3
    assert row["strategy_exit_rate"] == 1.0
    assert row["strategy_exit_avg_net_bps"] > 8.0
    assert row["exit_family_counts"]["strategy_exit"] == 3
    assert payload["summary"]["strategy_exit_healthy"] == 1
    assert payload["summary"]["strategy_exit_trades"] == 3
    assert "Strategy-managed exits" in payload["operator_answer"]


def test_exit_autopsy_flags_strategy_managed_exits_that_bleed(tmp_path):
    _closed_trades(
        tmp_path,
        "bad_smart_exit_lane",
        [
            {"reason": "strategy_neutral_long", "exit_price": 99.7, "realized": -0.3},
            {"reason": "strategy_neutral_long", "exit_price": 99.8, "realized": -0.2},
            {"reason": "strategy_reversal_short", "exit_price": 100.4, "realized": -0.4},
        ],
    )

    payload = build_paper_trade_exit_autopsy(
        journal_dir=tmp_path,
        config=PaperTradeExitAutopsyConfig(min_closed_trades=3),
        now=datetime(2026, 7, 30, tzinfo=UTC),
    )

    row = payload["rows"][0]
    assert row["loss_driver"] == DRIVER_STRATEGY_EXIT_DOMINATED
    assert row["strategy_exit_count"] == 3
    assert row["strategy_exit_net_pnl_usd"] < 0
    assert "smart exits are closing negative trades" in row["next_action"]
    assert payload["summary"]["strategy_exit_dominated"] == 1
