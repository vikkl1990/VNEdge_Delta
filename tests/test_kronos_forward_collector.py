from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import pandas as pd

import vnedge.research.kronos_forward_collector as module
from vnedge.research.kronos_forward_collector import (
    CollectorPaths,
    KronosForwardCollector,
    build_forward_report,
)


class FakeBackend:
    metadata: ClassVar[dict[str, str]] = {
        "backend": "deterministic_forward_test",
        "source_revision": "f" * 40,
    }

    def predict(
        self,
        context: pd.DataFrame,
        context_timestamps: pd.Series,
        future_timestamps: pd.Series,
        *,
        seed: int,
        config,
    ) -> pd.DataFrame:
        base = float(context["close"].iloc[-1])
        rows = []
        for step, timestamp in enumerate(future_timestamps, start=1):
            close = base * (1.0 + 0.002 * step)
            rows.append(
                {
                    "timestamp": timestamp,
                    "open": close - 0.2,
                    "high": close + 0.5,
                    "low": close - 0.5,
                    "close": close,
                }
            )
        return pd.DataFrame(rows).set_index("timestamp")


class Cost:
    route = "taker_taker"
    total_roundtrip_bps = 14.8


def candles(rows: int = 220) -> pd.DataFrame:
    timestamps = pd.date_range("2026-01-01", periods=rows, freq="1h", tz="UTC")
    close = [2000.0 + index for index in range(rows)]
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": [value - 0.2 for value in close],
            "high": [value + 2.0 for value in close],
            "low": [value - 2.0 for value in close],
            "close": close,
            "volume": [1000.0 + index for index in range(rows)],
        }
    )


def collector(tmp_path: Path, monkeypatch) -> KronosForwardCollector:
    monkeypatch.setattr(module, "route_cost_contract", lambda *args, **kwargs: Cost())
    monkeypatch.setattr(module, "strategy_authority_blockers", lambda *args, **kwargs: ())
    return KronosForwardCollector(
        paths=CollectorPaths(
            journal=tmp_path / "journal.jsonl",
            latest=tmp_path / "latest.json",
            artifacts=tmp_path / "artifacts",
        ),
        backend=FakeBackend(),
        registry_path=tmp_path / "registry.yaml",
        version="test-version",
    )


def test_stale_closed_candle_is_not_backfilled_as_forward_evidence(tmp_path, monkeypatch):
    engine = collector(tmp_path, monkeypatch)
    frame = candles(150)
    latest_close = frame["timestamp"].iloc[-1] + pd.Timedelta(hours=1)
    now = latest_close.to_pydatetime() + pd.Timedelta(minutes=30)

    report = engine.run_once(frame, now=now)

    assert report["status"] == "WAITING_FOR_FRESH_1H_CLOSE"
    assert report["summary"]["evaluations"] == 0
    assert report["summary"]["journaled_observations"] == 0
    assert report["can_trade"] is False


def test_forward_lane_uses_next_open_one_active_observation_and_12h_costed_exit(
    tmp_path, monkeypatch
):
    engine = collector(tmp_path, monkeypatch)
    full = candles()
    decision_open_index = 149
    decision_ts = full["timestamp"].iloc[decision_open_index] + pd.Timedelta(hours=1)
    first_now = decision_ts.to_pydatetime() + pd.Timedelta(minutes=5)
    first_frame = full.loc[full["timestamp"] <= decision_ts].copy()

    first = engine.run_once(first_frame, now=first_now)
    assert first["status"] == "OBSERVATION_ACCEPTED_ENTRY_PENDING"
    assert first["summary"]["evaluations"] == 1
    assert first["summary"]["journaled_observations"] == 1
    assert first["summary"]["entries_captured"] == 0

    second = engine.run_once(
        first_frame,
        now=first_now + pd.Timedelta(minutes=1),
    )
    assert second["status"] == "OBSERVATION_ACTIVE"
    assert second["summary"]["entries_captured"] == 1
    assert second["summary"]["evaluations"] == 1

    outcome_now = decision_ts.to_pydatetime() + pd.Timedelta(hours=12, minutes=20)
    outcome_frame = full.loc[
        full["timestamp"] < decision_ts + pd.Timedelta(hours=12)
    ].copy()
    final = engine.run_once(outcome_frame, now=outcome_now)

    assert final["summary"]["resolved_outcomes"] == 1
    assert final["summary"]["active_observation"] is False
    result = final["recent_outcomes"][0]
    expected_entry = float(full.loc[full["timestamp"] == decision_ts, "open"].iloc[0])
    expected_exit = float(outcome_frame["close"].iloc[-1])
    expected_gross = (expected_exit / expected_entry - 1.0) * 10_000.0
    assert result["entry_price"] == expected_entry
    assert result["hold_bars"] == 12
    assert result["gross_bps"] == expected_gross
    assert result["net_bps"] == expected_gross - 14.8
    assert result["exit_reason"] == "vertical_barrier_12h"
    assert final["can_trade"] is False
    assert final["selection_gate"]["paper_authorized"] is False


def test_report_never_opens_authority_even_when_economics_pass():
    records = []
    for index in range(60):
        records.append(
            {
                "kind": module.OUTCOME_KIND,
                "payload": {
                    "decision_id": f"decision-{index}",
                    "gross_bps": 44.8,
                    "net_bps": 30.0,
                },
            }
        )
    report = build_forward_report(
        records,
        contract=module.KronosForwardContract(),
        cost_bps=14.8,
        status="TEST",
        generated_at=datetime(2026, 1, 1, tzinfo=UTC),
        version="test",
    )
    assert report["selection_gate"]["passed"] is True
    assert report["selection_gate"]["paper_authorized"] is False
    assert report["selection_gate"]["live_authorized"] is False
    assert report["can_trade"] is False
    assert report["can_promote"] is False
