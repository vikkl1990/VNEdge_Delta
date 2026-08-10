"""Causality and safety contract for the MTF/AMF research scanner."""

from datetime import UTC, datetime
from itertools import pairwise
from urllib.parse import parse_qs, urlparse

import numpy as np
import pandas as pd
import pytest

from vnedge.research.mtf_amf_rejection_scanner import (
    MtfAmfScannerConfig,
    build_delta_live_scanner_payload,
    build_mtf_amf_feature_frame,
    build_scanner_payload,
    fetch_delta_public_candles,
    scan_mtf_amf_rejections,
)


def candles(hours: int = 420) -> tuple[pd.DataFrame, pd.DataFrame]:
    one_ts = pd.date_range("2025-01-01", periods=hours, freq="1h", tz=UTC)
    close = 100.0 + np.sin(np.arange(hours) * 0.7)
    one = pd.DataFrame(
        {
            "timestamp": one_ts,
            "open": close,
            "high": np.full(hours, 101.6),
            "low": np.full(hours, 98.4),
            "close": close,
            "volume": np.full(hours, 1_000.0),
        }
    )
    four_ts = pd.date_range("2024-12-20", periods=(hours // 4) + 90, freq="4h", tz=UTC)
    four_close = 100.0 + 0.4 * np.sin(np.arange(len(four_ts)) * 0.3)
    four = pd.DataFrame(
        {
            "timestamp": four_ts,
            "open": four_close,
            "high": np.full(len(four_ts), 101.6),
            "low": np.full(len(four_ts), 98.4),
            "close": four_close,
            "volume": np.full(len(four_ts), 4_000.0),
        }
    )
    return one, four


def test_scanner_emits_non_overlapping_research_only_alerts():
    one, four = candles()

    alerts = scan_mtf_amf_rejections(one, four, symbol="ETHUSD")

    assert len(alerts) >= 2
    assert all(alert.research_only for alert in alerts)
    assert all(alert.can_trade is False and alert.can_promote is False for alert in alerts)
    positions = {ts.isoformat(): pos for pos, ts in enumerate(one["timestamp"])}
    gaps = [
        positions[right.bar_start] - positions[left.bar_start] for left, right in pairwise(alerts)
    ]
    assert min(gaps) > MtfAmfScannerConfig().cooldown_bars


def test_current_one_hour_bar_is_excluded_from_its_range():
    one, four = candles(180)
    base = build_mtf_amf_feature_frame(one, four)
    changed = one.copy()
    changed.loc[changed.index[-1], "high"] = 999.0
    changed.loc[changed.index[-1], "low"] = 1.0

    mutated = build_mtf_amf_feature_frame(changed, four)

    assert mutated.iloc[-1]["one_hour_high"] == pytest.approx(base.iloc[-1]["one_hour_high"])
    assert mutated.iloc[-1]["one_hour_low"] == pytest.approx(base.iloc[-1]["one_hour_low"])


def test_unfinished_four_hour_bar_cannot_change_joined_level():
    one, four = candles(180)
    # End the 1h data inside the latest 4h candle, then mutate that 4h candle.
    latest_one = one.iloc[:-2].copy()
    signal_start = latest_one.iloc[-1]["timestamp"]
    forming = four[four["timestamp"] <= signal_start].index[-1]
    base = build_mtf_amf_feature_frame(latest_one, four)
    changed = four.copy()
    changed.loc[forming, "high"] = 999.0
    changed.loc[forming, "low"] = 1.0

    mutated = build_mtf_amf_feature_frame(latest_one, changed)

    assert mutated.iloc[-1]["four_hour_high"] == pytest.approx(base.iloc[-1]["four_hour_high"])
    assert mutated.iloc[-1]["four_hour_low"] == pytest.approx(base.iloc[-1]["four_hour_low"])


def test_appending_future_data_does_not_rewrite_past_features_or_alerts():
    one, four = candles()
    cutoff = 300
    short_one = one.iloc[:cutoff].copy()
    cutoff_ts = short_one.iloc[-1]["timestamp"]
    short_four = four[four["timestamp"] <= cutoff_ts].copy()

    before = build_mtf_amf_feature_frame(short_one, short_four)
    after = build_mtf_amf_feature_frame(one, four).iloc[:cutoff]
    columns = [
        "one_hour_high",
        "one_hour_low",
        "four_hour_high",
        "four_hour_low",
        "amf_histogram",
        "amf_regime",
    ]
    pd.testing.assert_frame_equal(before[columns], after[columns], check_exact=False, rtol=1e-12)

    before_alerts = scan_mtf_amf_rejections(short_one, short_four, symbol="BTCUSD")
    after_alerts = tuple(
        alert
        for alert in scan_mtf_amf_rejections(one, four, symbol="BTCUSD")
        if pd.Timestamp(alert.bar_start) <= cutoff_ts
    )
    assert before_alerts == after_alerts


def test_payload_has_no_execution_or_promotion_route():
    one, four = candles()

    payload = build_scanner_payload(
        one,
        four,
        symbol="SOLUSD",
        now=datetime(2026, 8, 4, tzinfo=UTC),
    )

    assert payload["can_trade"] is False
    assert payload["can_promote"] is False
    assert payload["policy"]["registered_strategy"] is False
    assert payload["policy"]["order_route_present"] is False
    assert payload["policy"]["bracket_exit_status"] == "failed_audit_not_implemented"
    assert payload["policy"]["observation_fill_model"] == "next_open_to_fixed_horizon_close"
    assert {row["horizon_bars"] for row in payload["summary"]["fixed_horizon_observations"]} == {
        15,
        40,
        62,
    }


def test_evidence_locked_timeframes_reject_parameter_drift():
    with pytest.raises(ValueError, match="supports 1h"):
        MtfAmfScannerConfig(chart_timeframe="5m")


def test_epoch_second_timestamp_resolution_is_normalized_for_asof_join():
    one, four = candles(180)
    one["timestamp"] = one["timestamp"].astype("int64") // 1_000_000_000
    four["timestamp"] = four["timestamp"].astype("int64") // 1_000_000_000
    one["timestamp"] = pd.to_datetime(one["timestamp"], unit="s", utc=True)
    four["timestamp"] = pd.to_datetime(four["timestamp"], unit="s", utc=True)

    frame = build_mtf_amf_feature_frame(one, four)

    assert len(frame) == len(one)


def test_delta_fetch_pages_and_excludes_forming_candle():
    now = datetime(2026, 8, 4, 16, 30, tzinfo=UTC)
    calls: list[str] = []

    def getter(url: str) -> dict:
        calls.append(url)
        query = parse_qs(urlparse(url).query)
        start = int(query["start"][0])
        end = int(query["end"][0])
        step = 3_600
        return {
            "success": True,
            "result": [
                {
                    "time": stamp,
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0,
                    "close": 100.5,
                    "volume": 10.0,
                }
                for stamp in range(start - (start % step), end + step, step)
            ],
        }

    frame = fetch_delta_public_candles("ETHUSD", "1h", days=80, now=now, http_get_json=getter)

    assert len(calls) == 2
    assert (frame["timestamp"] + pd.Timedelta(hours=1) <= now).all()
    assert frame["timestamp"].is_unique

    with_forming = fetch_delta_public_candles(
        "ETHUSD",
        "1h",
        days=80,
        now=now,
        http_get_json=getter,
        include_incomplete=True,
    )
    assert with_forming.iloc[-1]["timestamp"] == pd.Timestamp("2026-08-04T16:00:00Z")
    assert (with_forming["timestamp"] <= now).all()


def test_delta_live_payload_is_multi_symbol_and_cannot_trade():
    now = datetime(2026, 8, 4, 16, 0, tzinfo=UTC)

    def getter(url: str) -> dict:
        query = parse_qs(urlparse(url).query)
        start = int(query["start"][0])
        end = int(query["end"][0])
        resolution = query["resolution"][0]
        step = 3_600 if resolution == "1h" else 14_400
        rows = []
        for index, stamp in enumerate(range(start - (start % step), end, step)):
            close = 100.0 + np.sin(index * 0.7)
            rows.append(
                {
                    "time": stamp,
                    "open": close,
                    "high": 101.6,
                    "low": 98.4,
                    "close": close,
                    "volume": 10.0,
                }
            )
        return {"success": True, "result": rows}

    payload = build_delta_live_scanner_payload(
        (symbol for symbol in ("BTCUSD", "ETHUSD")),
        days=30,
        now=now,
        http_get_json=getter,
    )

    assert payload["summary"]["requested_symbols"] == 2
    assert payload["summary"]["healthy_symbols"] == 2
    assert payload["summary"]["error_symbols"] == 0
    assert set(payload["symbols"]) == {"BTCUSD", "ETHUSD"}
    assert payload["policy"]["public_data_only"] is True
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False
