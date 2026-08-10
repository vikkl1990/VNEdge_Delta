"""Execution-aware edge labels for scanner events."""

from datetime import UTC

import pandas as pd

from vnedge.research.execution_edge_labeler import (
    EdgeLabelerConfig,
    SignalEvent,
    build_truth_report,
    label_events,
    label_strategy_events,
    summarize_truth,
)
from vnedge.scalping.parameter_registry import ExchangeFeeProfile
from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent


def candles(rows):
    ts = pd.date_range("2026-07-14T00:00:00Z", periods=len(rows), freq="5min")
    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": [r[0] for r in rows],
            "high": [r[1] for r in rows],
            "low": [r[2] for r in rows],
            "close": [r[3] for r in rows],
            "volume": [100.0] * len(rows),
        }
    )


def fee(**kwargs):
    base = dict(
        exchange="test",
        maker_bps=1.0,
        taker_bps=1.0,
        slippage_bps=0.0,
        safety_buffer_bps=0.0,
    )
    base.update(kwargs)
    return ExchangeFeeProfile(**base)


def test_label_event_records_mfe_mae_and_positive_maker_edge():
    df = candles(
        [
            (100.0, 100.2, 99.8, 100.0),
            (100.0, 100.1, 99.9, 100.0),
            (100.0, 101.0, 99.6, 100.5),
            (100.5, 102.2, 100.4, 102.0),
            (102.0, 102.4, 101.5, 102.1),
        ]
    )
    event = SignalEvent(
        event_id="sats|1",
        ts=df["timestamp"].iloc[1],
        side="long",
        stop_price=99.0,
        take_profit_price=102.0,
        source_id="sats",
        strategy_id="sats_5m_scalper_v1",
        route="MAKER_ONLY",
        fill_probability=0.8,
    )

    labels = label_events(
        df,
        [event],
        exchange="test",
        fee_profile=fee(),
        config=EdgeLabelerConfig(min_samples=1),
    )
    label = labels[0]

    assert label.entry_price == 100.0
    assert label.outcome == "target"
    assert label.gross_bps == 200.0
    assert label.net_bps == 198.0
    assert label.mfe_bps == 220.0
    assert label.mae_bps == -40.0
    assert label.mfe_after_cost_bps == 218.0
    assert label.risk_bps == 100.0
    assert label.target_bps == 200.0
    assert label.hold_bars == 2
    assert label.time_to_mfe_bars == 2
    assert label.time_to_mae_bars == 1
    assert label.capture_ratio == 0.9091
    assert label.exit_diagnosis == "CAPTURED_AFTER_COST"
    assert label.max_r == 2.2
    summary = summarize_truth(labels, config=EdgeLabelerConfig(min_samples=1))
    assert summary.verdict == "MAKER_EDGE"
    assert summary.avg_net_bps == 198.0
    assert summary.avg_mfe_after_cost_bps == 218.0
    assert summary.avg_hold_bars == 2.0
    assert summary.avg_time_to_mfe_bars == 2.0
    assert summary.avg_capture_ratio == 0.9091
    assert summary.fee_wall_break_rate_pct == 100.0
    assert summary.exit_diagnosis_counts == {"CAPTURED_AFTER_COST": 1}
    assert summary.profit_factor == 999.0


def test_fee_wall_blocks_flat_signal_after_route_cost():
    df = candles(
        [
            (100.0, 100.1, 99.9, 100.0),
            (100.0, 100.1, 99.9, 100.0),
            (100.0, 100.05, 99.95, 100.02),
            (100.02, 100.04, 99.98, 100.01),
            (100.01, 100.03, 99.99, 100.01),
        ]
    )
    event = SignalEvent(
        event_id="flat|1",
        ts=df["timestamp"].iloc[1],
        side="long",
        stop_price=99.0,
        source_id="flat",
        strategy_id="demo",
        route="TAKER_ALLOWED",
    )

    labels = label_events(
        df,
        [event],
        exchange="test",
        fee_profile=fee(taker_bps=5.0, slippage_bps=1.0),
        config=EdgeLabelerConfig(min_samples=1),
    )

    assert labels[0].gross_bps == 1.0
    assert labels[0].route_cost_bps == 11.0
    assert labels[0].net_bps == -10.0
    assert labels[0].mfe_after_cost_bps < 0.0
    assert labels[0].exit_diagnosis == "MOVE_NEVER_CLEARED_COST"
    summary = summarize_truth(labels, config=EdgeLabelerConfig(min_samples=1))
    assert summary.verdict == "NEGATIVE_AFTER_COST"
    assert summary.primary_blocker == "average net/PF below maker breakeven"
    assert summary.fee_wall_break_rate_pct == 0.0


def test_stop_wins_when_target_and_stop_share_candle_and_flags_exit_geometry():
    df = candles(
        [
            (100.0, 100.1, 99.9, 100.0),
            (100.0, 100.1, 99.9, 100.0),
            (100.0, 102.5, 98.8, 101.5),
        ]
    )
    event = SignalEvent(
        event_id="tie|1",
        ts=df["timestamp"].iloc[1],
        side="long",
        stop_price=99.0,
        take_profit_price=102.0,
        route="TAKER_ALLOWED",
    )

    label = label_events(
        df,
        [event],
        exchange="test",
        fee_profile=fee(),
        config=EdgeLabelerConfig(min_samples=1),
    )[0]

    assert label.outcome == "stop"
    assert label.gross_bps == -100.0
    assert label.net_bps == -102.0
    assert label.mfe_after_cost_bps == 248.0
    assert label.capture_ratio == -0.4
    assert label.exit_diagnosis == "STOP_BEFORE_TARGET"


class AlwaysLongStrategy(BaseStrategy):
    strategy_id = "always_long_for_truth_test"
    warmup_bars = 1

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        return candles.copy()

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        if index == 1:
            return SignalIntent("long", stop_price=99.0, take_profit_price=102.0)
        return None


def test_strategy_events_are_labeled_through_same_truth_layer():
    df = candles(
        [
            (100.0, 100.1, 99.9, 100.0),
            (100.0, 100.1, 99.9, 100.0),
            (100.0, 101.0, 99.6, 100.5),
            (100.5, 102.2, 100.4, 102.0),
        ]
    )

    labels = label_strategy_events(
        df,
        AlwaysLongStrategy(),
        exchange="test",
        route="MAKER_ONLY",
        fee_profile=fee(),
        config=EdgeLabelerConfig(min_samples=1),
    )
    report = build_truth_report(
        exchange="test",
        symbol="ETH/USD:USD",
        timeframe="5m",
        strategy_id=AlwaysLongStrategy.strategy_id,
        labels=labels,
        config=EdgeLabelerConfig(min_samples=1),
    )

    assert len(labels) == 1
    assert labels[0].strategy_id == AlwaysLongStrategy.strategy_id
    assert report["summary"]["verdict"] == "LOW_FILL_CONFIDENCE"
    assert report["summary"]["avg_net_bps"] == 198.0
    assert report["summary"]["executable_samples"] == 0
    assert report["policy"]["can_trade"] is False
    assert report["summary"]["samples"] == 1


def test_naive_datetime_event_is_treated_as_utc():
    df = candles(
        [
            (100.0, 100.1, 99.9, 100.0),
            (100.0, 100.1, 99.9, 100.0),
            (100.0, 102.2, 99.6, 102.0),
        ]
    )
    naive_ts = df["timestamp"].iloc[1].to_pydatetime().replace(tzinfo=None)
    event = SignalEvent(
        event_id="naive|1",
        ts=pd.Timestamp(naive_ts),
        side="long",
        stop_price=99.0,
        take_profit_price=102.0,
        route="MAKER_ONLY",
        fill_probability=0.75,
    )

    label = label_events(
        df,
        [event],
        exchange="test",
        fee_profile=fee(),
        config=EdgeLabelerConfig(min_samples=1),
    )[0]

    assert label.entry_ts == df["timestamp"].iloc[2].isoformat()
    assert label.outcome == "target"
    assert label.ts.endswith("+00:00")


def test_report_timestamps_are_utc_iso_strings():
    report = build_truth_report(
        exchange="test",
        symbol="BTC/USDT:USDT",
        timeframe="5m",
        strategy_id="none",
        labels=[],
    )

    assert pd.Timestamp(report["generated_at"]).tzinfo is not None
    assert pd.Timestamp(report["generated_at"]).tz_convert(UTC).tzinfo is not None
    assert report["summary"]["verdict"] == "NO_EVENTS"
