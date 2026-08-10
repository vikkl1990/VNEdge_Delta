import json
from datetime import UTC, datetime

from vnedge.execution.fill_ledger import FillLedger
from vnedge.research.paper_lane_performance import (
    STATE_PAPER_ONLINE_NO_TRADES,
    STATE_PAPER_PROMOTION_CANDIDATE,
    PaperLanePerformanceConfig,
    build_paper_lane_performance,
)


def _journal_record(kind, payload, ts="2026-07-26T00:00:00+00:00"):
    return json.dumps({"ts": ts, "kind": kind, "payload": payload})


def test_paper_performance_marks_heartbeat_lane_online_without_trades(tmp_path):
    journal = tmp_path / "stealth.journal.jsonl"
    journal.write_text(
        _journal_record(
            "paper_lane_heartbeat",
            {
                "strategy_id": "stealth_trail_bbp_v1",
                "exchange": "delta_india",
                "symbol": "ETH/USD:USD",
                "timeframe": "5m",
                "mode": "paper",
                "why_no_trade": "last_eval_no_signal",
            },
        )
        + "\n",
        encoding="utf-8",
    )

    payload = build_paper_lane_performance(
        journal_dir=tmp_path,
        now=datetime(2026, 7, 26, 0, 1, tzinfo=UTC),
    )

    row = payload["rows"][0]
    assert row["state"] == STATE_PAPER_ONLINE_NO_TRADES
    assert row["paper_lane_heartbeats"] == 1
    assert row["closed_trades"] == 0
    assert row["latest_why_no_trade"] == "last_eval_no_signal"
    assert payload["summary"]["online_no_trades"] == 1
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False


def test_paper_performance_scores_hash_chained_positive_lane(tmp_path):
    journal = tmp_path / "alpha.journal.jsonl"
    journal.write_text(
        "\n".join(
            [
                _journal_record(
                    "lane_eval",
                    {
                        "strategy_id": "vnedge_algo_ml_pro_v1",
                        "exchange": "delta_india",
                        "symbol": "ETH/USD:USD",
                        "timeframe": "4h",
                        "mode": "paper",
                        "bar_ts": "2026-07-26T00:00:00+00:00",
                        "fired": True,
                        "signal_reason": "trend long",
                        "backfill": False,
                    },
                ),
                _journal_record(
                    "order_intent",
                    {
                        "client_order_id": "vne_1",
                        "intent": {
                            "strategy_id": "vnedge_algo_ml_pro_v1",
                            "symbol": "ETH/USD:USD",
                            "side": "long",
                        },
                    },
                ),
                _journal_record(
                    "order_acknowledged",
                    {"client_order_id": "vne_1", "exchange_order_id": "ex_1"},
                ),
                _journal_record("live_paper_exit", {"reason": "take_profit", "state": "filled"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    ledger = FillLedger(tmp_path / "alpha.fills.jsonl")
    ledger.append(
        {
            "ts": "2026-07-26T00:00:01+00:00",
            "mode": "paper",
            "venue": "delta_india",
            "strategy_id": "vnedge_algo_ml_pro_v1",
            "symbol": "ETH/USD:USD",
            "side": "buy",
            "quantity": 1.0,
            "price": 100.0,
            "fee_usd": 0.1,
            "realized_pnl_usd": 0.0,
            "client_order_id": "vne_1",
        }
    )
    ledger.append(
        {
            "ts": "2026-07-26T00:10:00+00:00",
            "mode": "paper",
            "venue": "delta_india",
            "strategy_id": "vnedge_algo_ml_pro_v1",
            "symbol": "ETH/USD:USD",
            "side": "sell",
            "quantity": 1.0,
            "price": 103.0,
            "fee_usd": 0.1,
            "realized_pnl_usd": 3.0,
            "client_order_id": "vne_exit",
        }
    )

    payload = build_paper_lane_performance(
        journal_dir=tmp_path,
        config=PaperLanePerformanceConfig(min_closed_trades=1),
        now=datetime(2026, 7, 26, 0, 11, tzinfo=UTC),
    )

    row = payload["rows"][0]
    assert row["state"] == STATE_PAPER_PROMOTION_CANDIDATE
    assert row["live_evals"] == 1
    assert row["live_signals"] == 1
    assert row["paper_order_intents"] == 1
    assert row["fills"] == 2
    assert row["closed_trades"] == 1
    assert row["profit_factor"] == 999.0
    assert row["realized_pnl_usd"] == 3.0
    assert row["fees_usd"] == 0.2
    assert row["net_pnl_usd"] == 2.8
    assert row["closed_net_pnl_usd"] == 2.8
    assert row["avg_closed_trade_net_bps"] == 271.8447
    assert row["open_fill_count"] == 0
    assert row["unpaired_closing_fills"] == 0
    assert row["journal_drift_flags"] == []
    assert payload["summary"]["promotion_candidates"] == 1
    assert payload["summary"]["net_pnl_usd"] == 2.8
    assert payload["summary"]["closed_net_pnl_usd"] == 2.8


def test_paper_performance_flags_open_fill_fee_drift(tmp_path):
    journal = tmp_path / "open_lane.journal.jsonl"
    journal.write_text(
        _journal_record(
            "paper_lane_heartbeat",
            {
                "strategy_id": "quant_signal_pack_v1",
                "exchange": "binanceusdm",
                "symbol": "ETH/USDT:USDT",
                "timeframe": "1h",
                "mode": "paper",
                "why_no_trade": "position_open: managing exit plan",
            },
        )
        + "\n",
        encoding="utf-8",
    )
    ledger = FillLedger(tmp_path / "open_lane.fills.jsonl")
    ledger.append(
        {
            "ts": "2026-07-26T00:00:01+00:00",
            "mode": "paper",
            "venue": "binanceusdm",
            "strategy_id": "quant_signal_pack_v1",
            "symbol": "ETH/USDT:USDT",
            "side": "buy",
            "quantity": 0.1,
            "price": 1800.0,
            "fee_usd": 0.09,
            "realized_pnl_usd": 0.0,
            "client_order_id": "entry-open",
        }
    )

    payload = build_paper_lane_performance(
        journal_dir=tmp_path,
        now=datetime(2026, 7, 26, 0, 1, tzinfo=UTC),
    )

    row = payload["rows"][0]
    assert row["closed_trades"] == 0
    assert row["net_pnl_usd"] == -0.09
    assert row["closed_net_pnl_usd"] == 0.0
    assert row["open_fill_count"] == 1
    assert row["open_position_entry_fees_usd"] == 0.09
    assert row["journal_drift_flags"] == [
        "1 open fill(s) awaiting close",
        "$0.09 open entry-fee drag",
    ]
    assert payload["summary"]["journal_drift_lanes"] == 1
