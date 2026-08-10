"""Orderflow footprint miner."""

from datetime import UTC, datetime
import json

from vnedge.research.orderflow_footprint import (
    FootprintConfig,
    build_footprint_bars,
    publish_orderflow_footprint,
    run_orderflow_footprint,
)
from vnedge.research.universe import ResearchTarget
from vnedge.scalping.microstructure import TopOfBook, TradeTick


DAY = "20260706"
SYM = "BTC/USDT:USDT"


def _dt(ts_ms: int) -> datetime:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC)


def _book(ts_ms: int, mid: float = 100.0) -> tuple[int, str, TopOfBook]:
    return (
        ts_ms,
        "book",
        TopOfBook(
            symbol=SYM,
            bid=mid - 0.01,
            bid_size=10.0,
            ask=mid + 0.01,
            ask_size=3.0,
            event_time=_dt(ts_ms),
        ),
    )


def _trade(ts_ms: int, price: float, qty: float, side: str) -> tuple[int, str, TradeTick]:
    return (
        ts_ms,
        "trade",
        TradeTick(symbol=SYM, price=price, quantity=qty, taker_side=side, event_time=_dt(ts_ms)),
    )


def _stacked_buy_events() -> list[tuple[int, str, object]]:
    start = 1_783_000_020_000
    events: list[tuple[int, str, object]] = []
    for i in range(4):
        ts = start + i * 60_000
        base = 100.0 + i
        events.append(_book(ts, base))
        events.append(_trade(ts + 1_000, base, 10.0, "buy"))
        events.append(_trade(ts + 20_000, base + 0.08, 12.0, "buy"))
        events.append(_trade(ts + 40_000, base + 0.12, 1.0, "sell"))
    return events


def test_footprint_bars_aggregate_delta_and_cvd():
    events = [
        _book(1_783_000_000_000, 100.0),
        _trade(1_783_000_001_000, 100.0, 2.0, "buy"),
        _trade(1_783_000_002_000, 101.0, 1.0, "sell"),
        _trade(1_783_000_061_000, 102.0, 3.0, "buy"),
    ]

    bars = build_footprint_bars(events, config=FootprintConfig(min_price_move_bps=0.0))

    assert len(bars) == 2
    assert bars[0].buy_volume == 2.0
    assert bars[0].sell_volume == 1.0
    assert bars[0].delta_notional_usd == 99.0
    assert bars[0].cvd_notional_usd == 99.0
    assert bars[1].cvd_notional_usd == 405.0
    assert bars[0].avg_spread_bps is not None
    assert bars[0].avg_book_imbalance is not None


def test_stacked_imbalance_proxy_marks_consecutive_buy_bars():
    bars = build_footprint_bars(
        _stacked_buy_events(),
        config=FootprintConfig(
            stacked_window=3,
            min_delta_ratio=0.50,
            min_price_move_bps=1.0,
            min_trades_per_bar=1,
            min_bar_notional_usd=1.0,
        ),
    )

    assert [bar.stacked_buy_imbalance for bar in bars] == [False, False, True, True]
    assert bars[2].stacked_run_length == 3
    assert all(not bar.stacked_sell_imbalance for bar in bars)


def test_orderflow_report_is_research_only_and_replay_routed(monkeypatch, tmp_path):
    from vnedge.research import orderflow_footprint as miner

    monkeypatch.setattr(
        miner,
        "load_tick_events",
        lambda root, exchange, symbol, day: _stacked_buy_events(),
    )

    payload = run_orderflow_footprint(
        tmp_path,
        targets=(ResearchTarget("binanceusdm", SYM),),
        days=(DAY,),
        config=FootprintConfig(
            min_bars=3,
            stacked_window=3,
            min_delta_ratio=0.50,
            min_price_move_bps=1.0,
            min_trades_per_bar=1,
            min_bar_notional_usd=1.0,
            min_lane_notional_usd=1.0,
        ),
        max_candidates=10,
    )

    assert payload["miner_id"] == "orderflow_footprint_v1"
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False
    assert payload["policy"]["can_trade"] is False
    assert payload["summary"]["candidates"] == 2
    assert payload["lanes"][0]["state"] == "ORDERFLOW_CANDIDATE"
    assert payload["candidates"][0]["route_decision"] == "REPLAY_REQUIRED"
    assert payload["candidates"][0]["requires_conservative_replay"] is True
    assert payload["candidates"][0]["can_trade"] is False


def test_orderflow_missing_ticks_do_not_create_candidates(monkeypatch, tmp_path):
    from vnedge.research import orderflow_footprint as miner

    monkeypatch.setattr(miner, "load_tick_events", lambda root, exchange, symbol, day: [])

    payload = run_orderflow_footprint(
        tmp_path,
        targets=(ResearchTarget("binanceusdm", SYM),),
        days=(DAY,),
    )

    assert payload["summary"]["candidates"] == 0
    assert payload["lanes"][0]["state"] == "MISSING_TICK_DATA"
    assert payload["can_trade"] is False


def test_orderflow_publish_is_atomic_and_appends_feed(tmp_path):
    payload = {
        "miner_id": "orderflow_footprint_v1",
        "can_trade": False,
        "can_promote": False,
    }
    out = tmp_path / "orderflow_footprint_latest.json"
    feed = tmp_path / "orderflow_footprint_feed.jsonl"

    publish_orderflow_footprint(payload, out, feed)
    publish_orderflow_footprint(payload, out, feed)

    assert json.loads(out.read_text())["miner_id"] == "orderflow_footprint_v1"
    assert not list(tmp_path.glob("*.tmp"))
    assert len(feed.read_text().strip().splitlines()) == 2
