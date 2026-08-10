"""Candidate replay executor: event-to-passive-quote proof path."""

from __future__ import annotations

from datetime import UTC, datetime

from vnedge.scalping.microstructure import TradeTick
from vnedge.research.candidate_replay_executor import (
    CandidateReplayConfig,
    EventReplaySpec,
    TimedEventScalper,
    _orderflow_specs,
    replay_policy,
    replay_specs,
    replay_spec,
    run_candidate_replay,
)
from vnedge.scalping.features import IncrementalFeatureEngine
from vnedge.scalping.microstructure import TopOfBook

SYM = "BTC/USDT:USDT"
T0 = 1_783_700_000_000
DAY = datetime.fromtimestamp(T0 / 1000, tz=UTC).strftime("%Y%m%d")


def _dt(ts_ms: int) -> datetime:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC)


def _book(ts_ms: int, bid: float = 100.0, ask: float = 100.1) -> TopOfBook:
    return TopOfBook(SYM, bid, 5.0, ask, 5.0, _dt(ts_ms))


def _trade(ts_ms: int, price: float, qty: float, side: str) -> TradeTick:
    return TradeTick(SYM, price, qty, side, _dt(ts_ms))


def _positive_events() -> list[tuple[int, str, object]]:
    return [
        (T0 - 5_000, "book", _book(T0 - 5_000, 99.9, 100.0)),
        (T0, "book", _book(T0, 100.0, 100.1)),
        (T0 + 50, "trade", _trade(T0 + 50, 99.99, 1.0, "sell")),
        (T0 + 100, "book", _book(T0 + 100, 100.7, 100.8)),
    ]


def _touch_events() -> list[tuple[int, str, object]]:
    return [
        (T0, "book", _book(T0, 100.0, 100.1)),
        (T0 + 50, "trade", _trade(T0 + 50, 100.0, 1.0, "sell")),
    ]


def test_timed_event_scalper_quotes_once_after_trigger_only():
    spec = EventReplaySpec(
        "c1", "test", "family", "binanceusdm", SYM, DAY, "buy", T0, 60_000
    )
    scalper = TimedEventScalper(spec, CandidateReplayConfig(max_spread_bps=20.0))
    engine = IncrementalFeatureEngine()

    before = _book(T0 - 1)
    assert scalper.quote(engine.on_book(before), before) is None

    at_trigger = _book(T0)
    quote = scalper.quote(engine.on_book(at_trigger), at_trigger)
    assert quote is not None
    assert quote.side == "buy"
    assert scalper.quote(engine.on_book(_book(T0 + 1)), _book(T0 + 1)) is None


def test_replay_spec_uses_conservative_trade_through_fill(monkeypatch, tmp_path):
    from vnedge.research import candidate_replay_executor as executor

    monkeypatch.setattr(executor, "load_tick_events", lambda *_args: _positive_events())
    spec = EventReplaySpec(
        "orderflow|x", "orderflow_footprint", "orderflow_footprint_v1",
        "binanceusdm", SYM, DAY, "buy", T0, 60_000,
    )

    row = replay_spec(
        tmp_path,
        spec,
        config=CandidateReplayConfig(max_spread_bps=20.0, min_replay_fills=1),
    )

    assert row.quotes == 1
    assert row.fills == 1
    assert row.verdict == "REPLAY_CANDIDATE"
    assert row.can_trade is False
    assert row.net_usd > 0


def test_orderflow_specs_are_research_only_and_use_bar_close_trigger():
    payload = {
        "top_candidates": [
            {
                "candidate_id": "orderflow|1",
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
    }

    specs = _orderflow_specs(payload, config=CandidateReplayConfig())

    assert len(specs) == 1
    assert specs[0].trigger_ts_ms == T0
    assert specs[0].horizon_ms == 60_000
    assert replay_policy()["can_trade"] is False


def test_run_candidate_replay_publishes_research_only_summary(monkeypatch, tmp_path):
    from vnedge.research import candidate_replay_executor as executor

    monkeypatch.setattr(executor, "load_tick_events", lambda *_args: _positive_events())
    orderflow = tmp_path / "orderflow.json"
    orderflow.write_text(
        """
        {
          "top_candidates": [{
            "candidate_id": "orderflow|1",
            "exchange": "binanceusdm",
            "symbol": "BTC/USDT:USDT",
            "day": "%s",
            "family": "orderflow_footprint_v1",
            "side": "buy",
            "timeframe": "60s",
            "state": "ORDERFLOW_CANDIDATE",
            "end_ts_ms": %d
          }]
        }
        """
        % (DAY, T0)
    )

    payload = run_candidate_replay(
        tmp_path,
        event_leadlag_path=tmp_path / "missing.json",
        orderflow_path=orderflow,
        config=CandidateReplayConfig(max_spread_bps=20.0, min_replay_fills=1),
    )

    assert payload["can_trade"] is False
    assert payload["can_promote"] is False
    assert payload["summary"]["rows"] == 1
    assert payload["summary"]["replay_candidates"] == 1


def test_replay_spec_rejects_touch_fill_that_does_not_trade_through(monkeypatch, tmp_path):
    from vnedge.research import candidate_replay_executor as executor

    monkeypatch.setattr(executor, "load_tick_events", lambda *_args: _touch_events())
    spec = EventReplaySpec(
        "orderflow|touch", "orderflow_footprint", "orderflow_footprint_v1",
        "binanceusdm", SYM, DAY, "buy", T0, 60_000,
    )

    row = replay_spec(tmp_path, spec, config=CandidateReplayConfig(max_spread_bps=20.0))

    assert row.quotes == 1
    assert row.fills == 0
    assert row.verdict == "NO_FILLS"


def test_replay_specs_loads_each_tick_lane_once(monkeypatch, tmp_path):
    from vnedge.research import candidate_replay_executor as executor

    calls = []

    def fake_load(*args):
        calls.append(args)
        return _positive_events()

    monkeypatch.setattr(executor, "load_tick_events", fake_load)
    specs = [
        EventReplaySpec(
            f"orderflow|{i}", "orderflow_footprint", "orderflow_footprint_v1",
            "binanceusdm", SYM, DAY, "buy", T0 + i, 60_000,
        )
        for i in range(3)
    ]

    rows = replay_specs(
        tmp_path,
        specs,
        config=CandidateReplayConfig(max_spread_bps=20.0, min_replay_fills=1),
    )

    assert len(rows) == 3
    assert len(calls) == 1
