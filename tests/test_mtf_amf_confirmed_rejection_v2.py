"""Correctness and safety tests for the separately versioned scanner revival."""

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from vnedge.research.mtf_amf_confirmed_rejection_v2 import (
    ConfirmedRejectionV2Config,
    build_selection_report,
    replay_selection,
)
from vnedge.research import mtf_amf_confirmed_rejection_v2 as scanner_v2
from vnedge.research.mtf_amf_revival_experiments import (
    _direction_permission,
    _directional_feature_enricher,
)


def candles(hours: int = 700) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
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
    four_ts = pd.date_range("2024-11-01", periods=hours // 4 + 200, freq="4h", tz=UTC)
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
    fifteen_ts = pd.date_range("2025-01-01", periods=hours * 4, freq="15min", tz=UTC)
    fifteen_close = 100.0 + np.sin(np.arange(hours * 4) * 0.175)
    fifteen = pd.DataFrame(
        {
            "timestamp": fifteen_ts,
            "open": fifteen_close,
            "high": fifteen_close + 0.6,
            "low": fifteen_close - 0.6,
            "close": fifteen_close,
            "volume": np.full(hours * 4, 250.0),
        }
    )
    return one, four, fifteen


def test_replay_never_decides_at_or_after_selection_end():
    one, four, fifteen = candles()
    end = datetime(2025, 1, 20, tzinfo=UTC)

    trades, _ = replay_selection(
        one, four, fifteen, symbol="ETHUSD", decision_end_exclusive=end
    )

    assert all(pd.Timestamp(trade.setup_ts) < end for trade in trades)


def test_report_keeps_tail_sealed_and_has_no_routes():
    one, four, fifteen = candles()
    payload = build_selection_report(
        {"BTCUSD": (one, four, fifteen), "ETHUSD": (one, four, fifteen)},
        selection_end_exclusive=datetime(2025, 1, 25, tzinfo=UTC),
        untouched_start=datetime(2025, 1, 27, tzinfo=UTC),
        config=ConfirmedRejectionV2Config(minimum_selection_trades=1),
    )

    assert payload["untouched"]["status"] == "sealed"
    assert payload["untouched"]["loaded"] is False
    assert payload["untouched"]["predictions_computed"] is False
    assert payload["policy"]["registered_strategy"] is False
    assert payload["policy"]["paper_route"] == "absent"
    assert payload["policy"]["order_route"] == "absent"
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False


def test_appending_tail_cannot_change_selection_results():
    one, four, fifteen = candles()
    end = datetime(2025, 1, 20, tzinfo=UTC)
    short_one = one.loc[one["timestamp"] < end].copy()
    short_four = four.loc[four["timestamp"] < end].copy()
    short_fifteen = fifteen.loc[fifteen["timestamp"] < end].copy()

    before, before_funnel = replay_selection(
        short_one,
        short_four,
        short_fifteen,
        symbol="BTCUSD",
        decision_end_exclusive=end,
    )
    after, after_funnel = replay_selection(
        one, four, fifteen, symbol="BTCUSD", decision_end_exclusive=end
    )

    assert before == after
    assert before_funnel == after_funnel


def test_entry_is_next_15m_open_and_same_bar_ambiguity_resolves_stop_first(
    monkeypatch,
):
    warmup = scanner_v2.BASE_CONFIG.warmup_bars
    setup_ts = pd.Timestamp("2025-02-01T00:00:00Z")
    rows = []
    for pos in range(warmup + 1):
        rows.append(
            {
                "timestamp": setup_ts - pd.Timedelta(hours=warmup - pos),
                "open": 100.0,
                "high": 100.4,
                "low": 99.6,
                "close": 100.0,
                "atr": 1.0,
                "amf_histogram": 0.0,
                "amf_regime": 1.0,
                "upper_distance_atr": 10.0,
                "lower_distance_atr": 10.0,
                "upper_level": 101.0,
                "lower_level": 99.0,
            }
        )
    rows[-1].update(
        {
            "open": 99.8,
            "high": 100.4,
            "low": 99.0,
            "close": 100.2,
            "amf_histogram": 1.0,
            "amf_regime": 0.1,
            "lower_distance_atr": 0.0,
            "lower_level": 99.5,
        }
    )
    feature_frame = pd.DataFrame(rows)
    monkeypatch.setattr(
        scanner_v2,
        "build_mtf_amf_feature_frame",
        lambda *_args, **_kwargs: feature_frame,
    )

    fifteen_ts = pd.date_range(
        setup_ts + pd.Timedelta(hours=1), periods=60, freq="15min", tz=UTC
    )
    fifteen = pd.DataFrame(
        {
            "timestamp": fifteen_ts,
            "open": np.full(60, 100.0),
            "high": np.full(60, 100.2),
            "low": np.full(60, 99.8),
            "close": np.full(60, 100.0),
            "volume": np.full(60, 100.0),
        }
    )
    # Completed confirmation candle.  The next bar is the only legal entry.
    fifteen.loc[0, ["open", "high", "low", "close"]] = [100.0, 101.0, 99.5, 100.8]
    # Both the 98.9 stop and 102.2 target are touched after entry; the
    # conservative resolver must take the stop.
    fifteen.loc[1, ["open", "high", "low", "close"]] = [100.0, 103.0, 98.0, 101.0]

    trades, funnel = replay_selection(
        pd.DataFrame(),
        pd.DataFrame(),
        fifteen,
        symbol="ETHUSD",
        decision_end_exclusive=datetime(2025, 2, 3, tzinfo=UTC),
    )

    assert funnel["entered"] == 1
    assert len(trades) == 1
    trade = trades[0]
    assert pd.Timestamp(trade.entry_ts) == fifteen_ts[1]
    assert trade.entry_price == 100.0
    assert trade.stop_bps == pytest.approx(110.0)
    assert trade.target_bps == pytest.approx(220.0)
    assert trade.exit_reason == "stop"
    assert trade.same_bar_ambiguous is True
    assert trade.gross_bps == pytest.approx(-110.0)
    assert trade.net_bps == pytest.approx(-124.8)


def test_short_permission_requires_completed_bearish_structure():
    base = {
        "direction_bearish": True,
        "direction_ema20": 101.0,
        "direction_last_swing_high": 103.0,
        "upper_level": 102.0,
        "close": 100.0,
    }

    assert _direction_permission(pd.Series(base), "long") is True
    assert _direction_permission(pd.Series(base), "short") is True
    assert _direction_permission(
        pd.Series({**base, "direction_bearish": False}), "short"
    ) is False
    assert _direction_permission(
        pd.Series({**base, "close": 102.0}), "short"
    ) is False
    assert _direction_permission(
        pd.Series({**base, "upper_level": 104.0}), "short"
    ) is False


def test_direction_features_do_not_change_when_future_4h_bars_are_appended():
    one, four, _ = candles(hours=900)
    feature = pd.DataFrame({"timestamp": one["timestamp"]})
    cutoff = pd.Timestamp("2025-01-25T00:00:00Z")
    short_four = four.loc[four["timestamp"] + pd.Timedelta(hours=4) <= cutoff]
    short_feature = feature.loc[feature["timestamp"] <= cutoff]

    before = _directional_feature_enricher(one, short_four, short_feature)
    after = _directional_feature_enricher(one, four, short_feature)

    columns = [
        "direction_ema20",
        "direction_ema50",
        "direction_ema20_slope_3",
        "direction_last_swing_high",
        "direction_previous_swing_high",
        "direction_lower_high",
        "direction_bearish",
    ]
    pd.testing.assert_frame_equal(before[columns], after[columns])


def test_protected_stop_arms_only_after_completed_close(monkeypatch):
    warmup = scanner_v2.BASE_CONFIG.warmup_bars
    setup_ts = pd.Timestamp("2025-02-01T00:00:00Z")
    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range(
                setup_ts - pd.Timedelta(hours=warmup),
                periods=warmup + 1,
                freq="1h",
                tz=UTC,
            ),
            "open": np.full(warmup + 1, 100.0),
            "high": np.full(warmup + 1, 100.4),
            "low": np.full(warmup + 1, 99.6),
            "close": np.full(warmup + 1, 100.0),
            "atr": np.full(warmup + 1, 1.0),
            "amf_histogram": np.zeros(warmup + 1),
            "amf_regime": np.ones(warmup + 1),
            "upper_distance_atr": np.full(warmup + 1, 10.0),
            "lower_distance_atr": np.full(warmup + 1, 10.0),
            "upper_level": np.full(warmup + 1, 101.0),
            "lower_level": np.full(warmup + 1, 99.0),
        }
    )
    frame.loc[warmup, [
        "open", "high", "low", "close", "amf_histogram", "amf_regime",
        "lower_distance_atr", "lower_level",
    ]] = [99.8, 100.4, 99.0, 100.2, 1.0, 0.1, 0.0, 99.5]
    monkeypatch.setattr(
        scanner_v2, "build_mtf_amf_feature_frame", lambda *_args, **_kwargs: frame
    )
    stamps = pd.date_range(setup_ts + pd.Timedelta(hours=1), periods=60, freq="15min", tz=UTC)
    fifteen = pd.DataFrame(
        {
            "timestamp": stamps,
            "open": np.full(60, 100.0),
            "high": np.full(60, 100.2),
            "low": np.full(60, 99.8),
            "close": np.full(60, 100.0),
            "volume": np.full(60, 100.0),
        }
    )
    fifteen.loc[0, ["open", "high", "low", "close"]] = [100.0, 101.0, 99.5, 100.8]
    # Entry bar closes above +1R without hitting 2R. Protection arms only at close.
    fifteen.loc[1, ["open", "high", "low", "close"]] = [100.0, 101.3, 99.8, 101.2]
    # A later bar retraces through entry + costs.
    fifteen.loc[2, ["open", "high", "low", "close"]] = [100.9, 101.0, 100.0, 100.2]

    trades, funnel = replay_selection(
        pd.DataFrame(),
        pd.DataFrame(),
        fifteen,
        symbol="BTCUSD",
        decision_end_exclusive=datetime(2025, 2, 3, tzinfo=UTC),
        protection_trigger_r=1.0,
        protection_lock_bps=14.8,
    )

    assert funnel["protection_armed"] == 1
    assert trades[0].hold_bars == 2
    assert trades[0].exit_reason == "protected_stop"
    assert trades[0].gross_bps == pytest.approx(14.8)
    assert trades[0].net_bps == pytest.approx(0.0)
