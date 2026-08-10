from datetime import UTC, datetime, timedelta
import json

import pandas as pd

from vnedge.research.delta_5m_event_clock import (
    Delta5mEventClockConfig,
    build_delta_5m_event_clock_from_frames,
    publish_delta_5m_event_clock,
)


def candles(*, end_open: datetime, bars: int = 90, trend_bps: float = 2.5) -> pd.DataFrame:
    rows = []
    price = 1_800.0
    start = end_open - timedelta(minutes=5 * (bars - 1))
    for idx in range(bars):
        ts = start + timedelta(minutes=5 * idx)
        drift = price * trend_bps / 10_000.0
        open_ = price
        close = price + drift
        high = max(open_, close) + price * 0.0018
        low = min(open_, close) - price * 0.0012
        volume = 1_000.0 + idx * 20.0 + (250.0 if idx > bars - 8 else 0.0)
        rows.append(
            {
                "timestamp": ts,
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
            }
        )
        price = close
    return pd.DataFrame(rows)


def test_delta_clock_opens_right_after_utc_5m_boundary():
    now = datetime(2026, 7, 21, 10, 0, 20, tzinfo=UTC)
    latest_closed_open = datetime(2026, 7, 21, 9, 55, tzinfo=UTC)

    payload = build_delta_5m_event_clock_from_frames(
        {"ETH/USD:USD": candles(end_open=latest_closed_open)},
        now=now,
    )

    window = payload["event_window"]
    assert window["decision_window_state"] == "OPEN"
    assert window["seconds_to_close"] == 280
    assert window["next_decision_at"] == "2026-07-21T10:00:00+00:00"
    assert payload["summary"]["seconds_to_next_decision"] == 0
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False


def test_delta_clock_waits_after_entry_window_closes():
    now = datetime(2026, 7, 21, 10, 2, 0, tzinfo=UTC)
    latest_closed_open = datetime(2026, 7, 21, 9, 55, tzinfo=UTC)

    payload = build_delta_5m_event_clock_from_frames(
        {"ETH/USD:USD": candles(end_open=latest_closed_open)},
        now=now,
    )

    row = payload["rows"][0]
    assert payload["event_window"]["decision_window_state"] == "CLOSED"
    assert payload["event_window"]["next_decision_at"] == "2026-07-21T10:05:00+00:00"
    assert row["route"] == "WAIT"
    assert row["execution_window_state"] == "WAIT_NEXT_WINDOW"
    assert row["paper_execution_ready"] is False


def test_strong_delta_move_can_clear_taker_fee_wall_in_open_window():
    now = datetime(2026, 7, 21, 10, 0, 15, tzinfo=UTC)
    latest_closed_open = datetime(2026, 7, 21, 9, 55, tzinfo=UTC)
    config = Delta5mEventClockConfig(
        min_probability=0.58,
        taker_min_probability=0.60,
        min_expected_move_bps=8.0,
        profit_buffer_bps=2.0,
    )

    payload = build_delta_5m_event_clock_from_frames(
        {"ETH/USD:USD": candles(end_open=latest_closed_open, trend_bps=14.0)},
        config=config,
        now=now,
    )

    row = payload["rows"][0]
    assert row["direction"] == "UP"
    assert row["probability_up"] > row["probability_down"]
    assert row["route"] in {"TAKER_NOW", "MAKER_THEN_TAKER", "MAKER_ONLY"}
    assert row["paper_execution_ready"] is True
    assert row["live_execution_ready"] is False
    assert row["expected_move_bps"] >= row["maker_first_cost_bps"]


def test_missing_symbol_is_safe_and_read_only():
    now = datetime(2026, 7, 21, 10, 0, 15, tzinfo=UTC)
    payload = build_delta_5m_event_clock_from_frames(
        {},
        missing_symbols={"BTC/USD:USD": "no dataset"},
        now=now,
    )

    row = payload["rows"][0]
    assert row["execution_window_state"] == "DATA_MISSING"
    assert row["paper_execution_ready"] is False
    assert row["route"] == "WAIT"
    assert payload["summary"]["stale_or_missing"] == 1
    assert payload["policy"]["read_only"] is True


def test_publish_writes_latest_and_feed(tmp_path):
    now = datetime(2026, 7, 21, 10, 0, 15, tzinfo=UTC)
    payload = build_delta_5m_event_clock_from_frames(
        {"ETH/USD:USD": candles(end_open=datetime(2026, 7, 21, 9, 55, tzinfo=UTC))},
        now=now,
    )
    out = tmp_path / "delta_5m_event_clock_latest.json"
    feed = tmp_path / "delta_5m_event_clock_feed.jsonl"

    publish_delta_5m_event_clock(payload, out=out, feed=feed)

    saved = json.loads(out.read_text())
    assert saved["report_id"] == "delta_5m_event_clock_v1"
    feed_row = json.loads(feed.read_text().strip())
    assert feed_row["can_trade"] is False
    assert feed_row["can_promote"] is False
