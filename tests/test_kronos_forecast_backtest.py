from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import pandas as pd
import pytest

from vnedge.research.kronos_forecast_backtest import (
    KronosBacktestConfig,
    run_kronos_forecast_backtest,
    write_backtest_report,
)
from vnedge.research.kronos_forecast_gate import KronosForecastGateConfig
from vnedge.research.kronos_inference import KronosInferenceConfig


class BullishBackend:
    metadata: ClassVar[dict[str, str]] = {"backend": "test", "revision": "a" * 40}

    def __init__(self) -> None:
        self.last_context_timestamps: list[pd.Timestamp] = []

    def predict(self, context, context_timestamps, future_timestamps, *, seed, config):
        self.last_context_timestamps.append(context_timestamps.iloc[-1])
        base = float(context["close"].iloc[-1])
        rows = []
        for index, timestamp in enumerate(future_timestamps, start=1):
            close = base + index
            rows.append({
                "timestamp": timestamp,
                "open": close - 0.2,
                "high": close + 0.3,
                "low": close - 0.4,
                "close": close,
            })
        return pd.DataFrame(rows).set_index("timestamp")


def rising_candles(rows: int = 48) -> pd.DataFrame:
    timestamps = pd.date_range("2026-01-01", periods=rows, freq="1h", tz="UTC")
    base = [100.0 + index for index in range(rows)]
    return pd.DataFrame({
        "timestamp": timestamps,
        "open": base,
        "high": [value + 1.2 for value in base],
        "low": [value - 0.2 for value in base],
        "close": [value + 1.0 for value in base],
        "volume": [1000.0] * rows,
    })


def test_backtest_is_chronological_next_open_fee_aware_and_non_promoting():
    frame = rising_candles()
    backend = BullishBackend()
    inference = KronosInferenceConfig(
        lookback_bars=32,
        horizon_bars=2,
        sample_paths=1,
        seed=7,
    )
    report = run_kronos_forecast_backtest(
        frame,
        symbol="BTCUSD",
        timeframe="1h",
        backend=backend,
        inference_config=inference,
        gate_config=KronosForecastGateConfig(
            min_expected_net_bps=0,
            min_confidence=0,
            min_reward_risk=0,
            max_adverse_bps=10_000,
            maker_taker_cost_bps=8,
            safety_buffer_bps=0,
        ),
        backtest_config=KronosBacktestConfig(stride_bars=3),
        now=datetime(2026, 2, 1, tzinfo=UTC),
    )

    assert report["summary"]["observations"] == 5
    assert report["summary"]["direction_accuracy"] == 1.0
    assert report["summary"]["avg_net_bps"] > 0
    assert report["summary"]["profit_factor"] == "Infinity"
    assert report["gate_passed"]["observations"] == 5
    assert report["side_breakdown"]["long"]["observations"] == 5
    assert report["side_breakdown"]["short"]["observations"] == 0
    assert report["contract"]["entry"] == "next bar open"
    assert report["contract"]["evaluation_tail_is_untouched"] is False
    assert report["rows"][0]["entry_price"] == frame.iloc[32]["open"]
    assert report["rows"][0]["segment"] == "selection"
    assert report["rows"][-1]["segment"] == "evaluation_tail"
    assert backend.last_context_timestamps[0] == frame.iloc[31]["timestamp"]
    assert report["can_trade"] is False
    assert report["can_promote"] is False


def test_backtest_report_is_hash_checked_before_write(tmp_path: Path):
    report = run_kronos_forecast_backtest(
        rising_candles(),
        symbol="ETHUSD",
        timeframe="1h",
        backend=BullishBackend(),
        inference_config=KronosInferenceConfig(
            lookback_bars=32,
            horizon_bars=2,
            sample_paths=1,
        ),
        backtest_config=KronosBacktestConfig(stride_bars=5, max_observations=2),
        now=datetime(2026, 2, 1, tzinfo=UTC),
    )
    path = write_backtest_report(report, tmp_path / "report.json")
    assert json.loads(path.read_text())["payload_sha256"] == report["payload_sha256"]

    report["summary"]["avg_net_bps"] = 999
    with pytest.raises(ValueError, match="invalid payload hash"):
        write_backtest_report(report, tmp_path / "tampered.json")


def test_backtest_rejects_insufficient_history():
    with pytest.raises(ValueError, match="at least"):
        run_kronos_forecast_backtest(
            rising_candles(33),
            symbol="BTCUSD",
            timeframe="1h",
            backend=BullishBackend(),
            inference_config=KronosInferenceConfig(
                lookback_bars=32,
                horizon_bars=2,
                sample_paths=1,
            ),
        )
