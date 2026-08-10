"""Execution-condition miner for replay-failed scalper candidates."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from vnedge.research.execution_condition_miner import (
    ExecutionConditionConfig,
    analyze_replay_row,
    miner_policy,
    run_execution_condition_miner,
)
from vnedge.scalping.microstructure import TopOfBook, TradeTick

SYM = "SOL/USDT:USDT"
T0 = 1_783_740_000_000
DAY = datetime.fromtimestamp(T0 / 1000, tz=UTC).strftime("%Y%m%d")


def _dt(ts_ms: int) -> datetime:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC)


def _book(
    ts_ms: int,
    bid: float = 100.0,
    ask: float = 100.02,
    bid_qty: float = 5.0,
    ask_qty: float = 5.0,
) -> TopOfBook:
    return TopOfBook(SYM, bid, bid_qty, ask, ask_qty, _dt(ts_ms))


def _trade(ts_ms: int, price: float, qty: float, side: str) -> TradeTick:
    return TradeTick(SYM, price, qty, side, _dt(ts_ms))


def _row(verdict: str, *, avg_net_bps=None, avg_adverse_bps=None):
    return {
        "candidate_id": "orderflow|sol|1",
        "source": "orderflow_footprint",
        "family": "orderflow_footprint_v1",
        "exchange": "binanceusdm",
        "symbol": SYM,
        "day": DAY,
        "side": "buy",
        "trigger_ts": _dt(T0).isoformat(),
        "verdict": verdict,
        "avg_net_bps": avg_net_bps,
        "avg_adverse_bps": avg_adverse_bps,
    }


def test_condition_miner_classifies_no_quote_spread_gate():
    events = [
        (T0 - 1_000, "book", _book(T0 - 1_000, 100.0, 100.01)),
        (T0, "book", _book(T0, 100.0, 101.0)),
    ]

    mined = analyze_replay_row(
        "unused",
        _row("NO_QUOTE"),
        config=ExecutionConditionConfig(max_spread_bps=3.0),
        events=events,
    )

    assert mined.reason_bucket == "SPREAD_TOO_WIDE"
    assert mined.recommended_action == "RUN_FILTERED_REPLAY_FROM_EXECUTION_CONDITIONS"
    assert mined.quoteable is False
    assert mined.proposal["filter"] == "require_quote_window_spread_bps_lte"
    assert mined.can_trade is False


def test_condition_miner_classifies_touch_only_queue_risk():
    events = [
        (T0 - 500, "trade", _trade(T0 - 500, 100.0, 2.0, "buy")),
        (T0, "book", _book(T0, 100.0, 100.02)),
        (T0 + 100, "trade", _trade(T0 + 100, 100.0, 1.0, "sell")),
    ]

    mined = analyze_replay_row(
        "unused",
        _row("NO_FILLS"),
        config=ExecutionConditionConfig(max_spread_bps=3.0),
        events=events,
    )

    assert mined.reason_bucket == "TOUCH_ONLY_QUEUE_RISK"
    assert mined.touch_trade_count == 1
    assert mined.through_trade_count == 0
    assert mined.recommended_action == "RUN_FILTERED_REPLAY_FROM_EXECUTION_CONDITIONS"
    assert mined.proposal["filter"] == "require_pre_event_trade_through_proxy"


def test_run_execution_condition_miner_publishes_research_only_payload(monkeypatch, tmp_path):
    from vnedge.research import execution_condition_miner as miner

    calls = []

    def fake_load(*args):
        calls.append(args)
        return [
            (T0, "book", _book(T0, 100.0, 100.02)),
            (T0 + 50, "trade", _trade(T0 + 50, 99.99, 1.0, "sell")),
        ]

    monkeypatch.setattr(miner, "load_tick_events", fake_load)
    replay = tmp_path / "candidate_replay_latest.json"
    replay.write_text(json.dumps({
        "rows": [
            _row("NEGATIVE_EDGE_AFTER_REPLAY", avg_net_bps=-8.2),
            {**_row("NO_FILLS"), "candidate_id": "orderflow|sol|2"},
        ]
    }))

    payload = run_execution_condition_miner(tmp_path, replay_path=replay)

    assert payload["miner_id"] == "execution_condition_miner_v1"
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False
    assert miner_policy()["can_trade"] is False
    assert payload["summary"]["rows"] == 2
    assert payload["candidate_conditions"][0]["candidate_id"] == "orderflow|sol|1"
    assert len(calls) == 1
