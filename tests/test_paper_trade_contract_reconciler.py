import json
from datetime import UTC, datetime, timedelta

from vnedge.research.paper_trade_contract_reconciler import (
    VERDICT_CONTRACT_BROKEN,
    VERDICT_CONTRACT_OK_NEGATIVE_ALPHA,
    VERDICT_CONTRACT_OK_PROFITABLE,
    VERDICT_NO_CLOSED_TRADES,
    PaperTradeContractReconcilerConfig,
    TRADE_CONTRACT_OK,
    build_paper_trade_contract_reconciler,
)


def _write_jsonl(path, rows):
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _journal_record(kind, payload, ts):
    return {"ts": ts, "kind": kind, "payload": payload}


def _intent(coid, *, side, reduce_only, quantity=1.0, price=100.0, leverage=10.0):
    return {
        "client_order_id": coid,
        "intent": {
            "client_order_id": coid,
            "symbol": "ETH/USD:USD",
            "side": side,
            "order_type": "limit",
            "quantity": quantity,
            "limit_price": price,
            "reduce_only": reduce_only,
            "strategy_id": "stealth_trail_bbp_v1",
            "leverage": leverage,
            "notional_usd": price * quantity,
        },
    }


def _fill(ts, coid, side, price, realized, *, fee=0.02, quantity=1.0):
    return {
        "ts": ts,
        "mode": "paper",
        "venue": "delta_india",
        "strategy_id": "stealth_trail_bbp_v1",
        "symbol": "ETH/USD:USD",
        "side": side,
        "quantity": quantity,
        "price": price,
        "fee_usd": fee,
        "realized_pnl_usd": realized,
        "client_order_id": coid,
    }


def _closed_trade_fixture(
    tmp_path,
    lane,
    *,
    realized,
    exit_price,
    entry_intent=True,
    exit_intent=True,
    exit_reduce_only=True,
):
    base = datetime(2026, 7, 31, 0, 0, tzinfo=UTC)
    entry_ts = base.isoformat()
    exit_ts = (base + timedelta(minutes=5)).isoformat()
    entry_coid = f"{lane}_entry_1"
    exit_coid = f"{lane}_exit_1"
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
            entry_ts,
        ),
        _journal_record(
            "live_paper_exit",
            {
                "client_order_id": exit_coid,
                "reason": "take_profit" if realized > 0 else "stop",
                "tp_reached": 1 if realized > 0 else 0,
                "take_profit_levels": [exit_price] if realized > 0 else [],
            },
            exit_ts,
        ),
    ]
    if entry_intent:
        journals.insert(1, _journal_record("order_intent", _intent(entry_coid, side="buy", reduce_only=False), entry_ts))
    if exit_intent:
        journals.insert(
            -1,
            _journal_record(
                "order_intent",
                _intent(exit_coid, side="sell", reduce_only=exit_reduce_only, price=exit_price),
                exit_ts,
            ),
        )
    fills = [
        _fill(entry_ts, entry_coid, "buy", 100.0, 0.0),
        _fill(exit_ts, exit_coid, "sell", exit_price, realized),
    ]
    _write_jsonl(tmp_path / f"{lane}.journal.jsonl", journals)
    _write_jsonl(tmp_path / f"{lane}.fills.jsonl", fills)


def test_contract_reconciler_marks_clean_profitable_trade(tmp_path):
    _closed_trade_fixture(tmp_path, "clean_profit_lane", realized=1.0, exit_price=101.0)

    payload = build_paper_trade_contract_reconciler(
        journal_dir=tmp_path,
        config=PaperTradeContractReconcilerConfig(min_expected_net_bps=25.0),
        now=datetime(2026, 7, 31, tzinfo=UTC),
    )

    row = payload["rows"][0]
    sample = payload["trade_samples"][0]
    assert row["verdict"] == VERDICT_CONTRACT_OK_PROFITABLE
    assert row["closed_trades"] == 1
    assert row["critical_violations"] == 0
    assert row["avg_net_bps"] > 25.0
    assert sample["contract_state"] == TRADE_CONTRACT_OK
    assert sample["violations"] == []
    assert sample["leverage"] == 10.0
    assert sample["margin_usd"] == 10.0
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False


def test_contract_reconciler_separates_clean_negative_alpha(tmp_path):
    _closed_trade_fixture(tmp_path, "clean_negative_lane", realized=-0.5, exit_price=99.5)

    payload = build_paper_trade_contract_reconciler(
        journal_dir=tmp_path,
        config=PaperTradeContractReconcilerConfig(),
        now=datetime(2026, 7, 31, tzinfo=UTC),
    )

    row = payload["rows"][0]
    assert row["verdict"] == VERDICT_CONTRACT_OK_NEGATIVE_ALPHA
    assert row["critical_violations"] == 0
    assert row["net_pnl_usd"] < 0
    assert row["next_action"] == "CONTRACT_CLEAN_MINE_ENTRY_EXIT_ALPHA"
    assert payload["summary"]["contract_ok_negative_lanes"] == 1


def test_contract_reconciler_marks_missing_intent_as_runtime_contract_gap(tmp_path):
    _closed_trade_fixture(
        tmp_path,
        "missing_intent_lane",
        realized=1.0,
        exit_price=101.0,
        entry_intent=False,
    )

    payload = build_paper_trade_contract_reconciler(
        journal_dir=tmp_path,
        config=PaperTradeContractReconcilerConfig(),
        now=datetime(2026, 7, 31, tzinfo=UTC),
    )

    row = payload["rows"][0]
    sample = payload["trade_samples"][0]
    assert row["verdict"] == VERDICT_CONTRACT_BROKEN
    assert row["critical_violations"] >= 1
    assert "missing_entry_intent" in row["top_violations"]
    assert "missing_entry_intent" in sample["violations"]
    assert row["next_action"] == "REPAIR_ORDER_INTENT_LINEAGE_BEFORE_ALPHA_REVIEW"
    assert payload["summary"]["contract_broken_lanes"] == 1


def test_contract_reconciler_marks_bad_exit_reduce_only(tmp_path):
    _closed_trade_fixture(
        tmp_path,
        "bad_exit_lane",
        realized=1.0,
        exit_price=101.0,
        exit_reduce_only=False,
    )

    payload = build_paper_trade_contract_reconciler(
        journal_dir=tmp_path,
        config=PaperTradeContractReconcilerConfig(),
        now=datetime(2026, 7, 31, tzinfo=UTC),
    )

    row = payload["rows"][0]
    assert row["verdict"] == VERDICT_CONTRACT_BROKEN
    assert "exit_reduce_only_false" in row["top_violations"]
    assert row["next_action"] == "REPAIR_REDUCE_ONLY_EXIT_CONTRACT"


def test_contract_reconciler_keeps_alive_lane_without_closed_trades_visible(tmp_path):
    base = datetime(2026, 7, 31, 0, 0, tzinfo=UTC)
    _write_jsonl(tmp_path / "waiting_lane.journal.jsonl", [
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
    ])

    payload = build_paper_trade_contract_reconciler(
        journal_dir=tmp_path,
        config=PaperTradeContractReconcilerConfig(),
        now=base,
    )

    assert payload["rows"][0]["verdict"] == VERDICT_NO_CLOSED_TRADES
    assert payload["summary"]["no_closed_trade_lanes"] == 1
