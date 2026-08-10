"""Filtered replay executor: fresh-slice proof after condition mining."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from vnedge.research.filtered_replay_executor import (
    FilteredReplayConfig,
    filtered_replay_policy,
    run_filtered_replay,
)
from vnedge.research.candidate_replay_executor import CandidateReplayConfig
from vnedge.research.execution_condition_miner import ExecutionConditionConfig
from vnedge.scalping.microstructure import TopOfBook, TradeTick

SYM = "SOL/USDT:USDT"
T0 = 1_783_750_000_000
DAY = datetime.fromtimestamp(T0 / 1000, tz=UTC).strftime("%Y%m%d")
PREV_DAY = (datetime.fromtimestamp(T0 / 1000, tz=UTC) - timedelta(days=1)).strftime("%Y%m%d")
CANDIDATE_ID = f"orderflow_footprint|binanceusdm|{SYM}|{DAY}|{T0}|buy"


def _dt(ts_ms: int) -> datetime:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC)


def _book(ts_ms: int, bid: float = 100.0, ask: float = 100.02) -> TopOfBook:
    return TopOfBook(SYM, bid, 5.0, ask, 5.0, _dt(ts_ms))


def _trade(ts_ms: int, price: float, qty: float, side: str) -> TradeTick:
    return TradeTick(SYM, price, qty, side, _dt(ts_ms))


def _positive_events() -> list[tuple[int, str, object]]:
    return [
        (T0 - 1_000, "trade", _trade(T0 - 1_000, 100.0, 1.0, "buy")),
        (T0, "book", _book(T0, 100.0, 100.02)),
        (T0 + 50, "trade", _trade(T0 + 50, 99.99, 1.0, "sell")),
        (T0 + 100, "book", _book(T0 + 100, 100.2, 100.22)),
    ]


def _no_pre_tape_events() -> list[tuple[int, str, object]]:
    return [
        (T0, "book", _book(T0, 100.0, 100.02)),
        (T0 + 50, "trade", _trade(T0 + 50, 99.99, 1.0, "sell")),
    ]


def _write_inputs(tmp_path, *, seen_day: str = PREV_DAY, filter_name: str | None = None):
    orderflow = tmp_path / "orderflow.json"
    replay = tmp_path / "candidate_replay.json"
    conditions = tmp_path / "conditions.json"
    filter_name = filter_name or "require_pre_event_trade_through_proxy"
    orderflow.write_text(json.dumps({
        "top_candidates": [
            {
                "candidate_id": CANDIDATE_ID,
                "exchange": "binanceusdm",
                "symbol": SYM,
                "day": DAY,
                "family": "orderflow_footprint_v1",
                "side": "buy",
                "timeframe": "60s",
                "state": "ORDERFLOW_CANDIDATE",
                "end_ts_ms": T0,
                "score": 99.0,
            }
        ]
    }))
    replay.write_text(json.dumps({
        "rows": [
            {
                "candidate_id": CANDIDATE_ID,
                "source": "orderflow_footprint",
                "family": "orderflow_footprint_v1",
                "exchange": "binanceusdm",
                "symbol": SYM,
                "day": seen_day,
                "side": "buy",
                "trigger_ts": _dt(T0).isoformat(),
                "verdict": "NO_FILLS",
            }
        ]
    }))
    conditions.write_text(json.dumps({
        "candidate_conditions": [
            {
                "candidate_id": CANDIDATE_ID,
                "source": "orderflow_footprint",
                "family": "orderflow_footprint_v1",
                "exchange": "binanceusdm",
                "symbol": SYM,
                "rows": 1,
                "primary_bucket": "TOUCH_ONLY_QUEUE_RISK",
                "recommended_action": "RUN_FILTERED_REPLAY_FROM_EXECUTION_CONDITIONS",
                "confidence": 1.0,
                "filter_proposal": {
                    "filter": filter_name,
                    "min_pre_signed_notional_usd": 50.0,
                    "must_replay_fresh_window": True,
                },
            }
        ]
    }))
    return orderflow, replay, conditions


def _config() -> FilteredReplayConfig:
    return FilteredReplayConfig(
        max_orderflow_specs=5,
        replay=CandidateReplayConfig(max_spread_bps=3.0, min_replay_fills=1),
        conditions=ExecutionConditionConfig(max_spread_bps=3.0),
        min_pre_signed_notional_usd=50.0,
    )


def test_filtered_replay_runs_fresh_causal_filter(monkeypatch, tmp_path):
    from vnedge.research import candidate_replay_executor as replay_executor
    from vnedge.research import filtered_replay_executor as filtered_executor

    monkeypatch.setattr(filtered_executor, "load_tick_events", lambda *_args: _positive_events())
    monkeypatch.setattr(replay_executor, "load_tick_events", lambda *_args: _positive_events())
    orderflow, replay, conditions = _write_inputs(tmp_path, seen_day=PREV_DAY)

    payload = run_filtered_replay(
        tmp_path,
        event_leadlag_path=tmp_path / "missing.json",
        orderflow_path=orderflow,
        candidate_replay_path=replay,
        condition_path=conditions,
        config=_config(),
    )

    assert payload["executor_id"] == "filtered_replay_executor_v1"
    assert payload["can_trade"] is False
    assert filtered_replay_policy()["can_promote"] is False
    assert payload["summary"]["accepted_specs"] == 1
    assert payload["summary"]["replay_candidates"] == 1
    assert payload["rows"][0]["verdict"] == "REPLAY_CANDIDATE"
    assert payload["rows"][0]["filter_name"] == "require_pre_event_trade_through_proxy"
    assert payload["filter_decisions"][0]["reason"] == "FILTER_ACCEPTED"


def test_filtered_replay_excludes_seen_replay_day(monkeypatch, tmp_path):
    from vnedge.research import candidate_replay_executor as replay_executor
    from vnedge.research import filtered_replay_executor as filtered_executor

    monkeypatch.setattr(filtered_executor, "load_tick_events", lambda *_args: _positive_events())
    monkeypatch.setattr(replay_executor, "load_tick_events", lambda *_args: _positive_events())
    orderflow, replay, conditions = _write_inputs(tmp_path, seen_day=DAY)

    payload = run_filtered_replay(
        tmp_path,
        event_leadlag_path=tmp_path / "missing.json",
        orderflow_path=orderflow,
        candidate_replay_path=replay,
        condition_path=conditions,
        config=_config(),
    )

    assert payload["summary"]["accepted_specs"] == 0
    assert payload["summary"]["replay_candidates"] == 0
    assert payload["filter_decisions"][0]["reason"] == "SEEN_REPLAY_WINDOW_EXCLUDED"


def test_filtered_replay_rejects_when_pre_entry_filter_fails(monkeypatch, tmp_path):
    from vnedge.research import candidate_replay_executor as replay_executor
    from vnedge.research import filtered_replay_executor as filtered_executor

    monkeypatch.setattr(filtered_executor, "load_tick_events", lambda *_args: _no_pre_tape_events())
    monkeypatch.setattr(replay_executor, "load_tick_events", lambda *_args: _no_pre_tape_events())
    orderflow, replay, conditions = _write_inputs(tmp_path, seen_day=PREV_DAY)

    payload = run_filtered_replay(
        tmp_path,
        event_leadlag_path=tmp_path / "missing.json",
        orderflow_path=orderflow,
        candidate_replay_path=replay,
        condition_path=conditions,
        config=_config(),
    )

    assert payload["rows"] == []
    assert payload["summary"]["accepted_specs"] == 0
    assert payload["filter_decisions"][0]["reason"] == "FILTER_REJECTED_PRE_TAPE"
