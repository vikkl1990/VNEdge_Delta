"""Forward evidence, L2 context, and promotion-lock tests."""

import json
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from vnedge.dashboard.app import SnapshotProvider, create_app
from vnedge.execution.journal import DecisionJournal
from vnedge.research.scanner_forward_evidence import (
    build_expanded_backtest_payload,
    build_forward_evidence_payload,
    journal_fresh_alerts,
    read_l2_confirmation,
    resolve_forward_outcomes,
)


def hourly(start: str, periods: int, *, base: float = 100.0) -> pd.DataFrame:
    stamps = pd.date_range(start, periods=periods, freq="1h", tz=UTC)
    close = base + np.arange(periods, dtype=float)
    return pd.DataFrame(
        {
            "timestamp": stamps,
            "open": close,
            "high": close + 2.0,
            "low": close - 1.0,
            "close": close + 1.0,
            "volume": np.full(periods, 1_000.0),
        }
    )


def live_payload(observed_at: datetime) -> dict:
    bar_start = observed_at - timedelta(hours=1)
    return {
        "generated_at": (observed_at + timedelta(minutes=20)).isoformat(),
        "scanner_id": "mtf_amf_rejection_scanner_v1",
        "symbols": {
            "BTCUSD": {
                "summary": {
                    "latest_alert": {
                        "scanner_id": "mtf_amf_rejection_scanner_v1",
                        "symbol": "BTCUSD",
                        "bar_start": bar_start.isoformat(),
                        "observed_at": observed_at.isoformat(),
                        "side": "long",
                        "setup": "causal_test",
                    }
                }
            }
        },
        "can_trade": False,
        "can_promote": False,
    }


def test_fresh_alert_is_journaled_once_and_resolved_at_all_horizons(tmp_path):
    observed = datetime(2026, 8, 5, 1, tzinfo=UTC)
    candles = hourly("2026-08-05T01:00:00Z", 30)
    payload = live_payload(observed)
    journal = DecisionJournal(tmp_path / "scanner.jsonl")

    first = journal_fresh_alerts(
        payload,
        {"BTCUSD": candles},
        journal=journal,
        now=observed + timedelta(minutes=20),
    )
    second = journal_fresh_alerts(
        payload,
        {"BTCUSD": candles},
        journal=journal,
        now=observed + timedelta(minutes=25),
    )

    assert first == 1
    assert second == 0
    alerts = [record for record in journal.read_all() if record["kind"] == "scanner_alert"]
    assert len(alerts) == 1
    assert alerts[0]["payload"]["entry_price"] == pytest.approx(100.0)
    assert alerts[0]["payload"]["entry_basis"] == "next_1h_candle_open"

    added = resolve_forward_outcomes(
        {"BTCUSD": candles},
        journal=journal,
        now=observed + timedelta(hours=30),
    )
    assert added == 4
    outcomes = [
        record["payload"]
        for record in journal.read_all()
        if record["kind"] == "scanner_forward_outcome"
    ]
    assert {row["horizon_hours"] for row in outcomes} == {1, 4, 12, 24}
    one = next(row for row in outcomes if row["horizon_hours"] == 1)
    assert one["gross_return_bps"] == pytest.approx(100.0)
    assert one["net_return_bps"] == pytest.approx(88.0)
    assert one["mfe_bps"] == pytest.approx(200.0)
    assert one["mae_bps"] == pytest.approx(-100.0)

    assert resolve_forward_outcomes(
        {"BTCUSD": candles}, journal=journal, now=observed + timedelta(hours=31)
    ) == 0


def test_l2_confirmation_is_causal_context_only(tmp_path):
    at = datetime(2026, 8, 5, 1, tzinfo=UTC)
    event_time = at - timedelta(seconds=10)
    session = tmp_path / "session"
    session.mkdir()
    pd.DataFrame(
        [
            {
                "recv_ts_us": int(event_time.timestamp() * 1_000_000),
                "symbol": "BTCUSD",
                "weighted_obi": 0.6,
                "tfi_5s": 0.4,
                "microprice_dev_bps": 0.2,
                "spread_bps": 0.8,
                "feed_age_ms": 12.0,
                "book_valid": True,
            }
        ]
    ).to_parquet(session / "events_20260805T010000_000001.parquet")

    confirmation = read_l2_confirmation(tmp_path, symbol="BTCUSD", at=at, side="long")

    assert confirmation.status == "aligned"
    assert confirmation.age_seconds == pytest.approx(10.0)
    assert confirmation.context_only is True
    assert confirmation.used_for_signal is False
    assert confirmation.used_for_execution is False
    assert confirmation.used_for_promotion is False


def scanner_candles(hours: int = 820) -> tuple[pd.DataFrame, pd.DataFrame]:
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
    four_ts = pd.date_range("2024-12-01", periods=(hours // 4) + 200, freq="4h", tz=UTC)
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


def test_expanded_backtest_keeps_thresholds_causal_and_paper_off():
    btc = scanner_candles()
    eth = scanner_candles()

    payload = build_expanded_backtest_payload({"BTCUSD": btc, "ETHUSD": eth})

    assert payload["summary"]["alerts"] > 0
    assert payload["policy"]["thresholds_unchanged_across_markets"] is True
    assert payload["policy"]["completed_candles_only"] is True
    assert payload["policy"]["no_repainting"] is True
    assert payload["promotion"]["selected_horizon_hours"] in {1, 4, 12, 24}
    assert payload["promotion"]["paper_trading_enabled"] is False
    assert payload["promotion"]["l2_used_in_assessment"] is False
    assert set(payload["period_breakdown"]) == {"daily", "weekly", "monthly", "quarterly"}
    assert payload["period_breakdown"]["daily"]
    assert {gate["name"] for gate in payload["promotion"]["gates"]} >= {
        "positive_untouched_markets",
        "profit_factor_after_12bps",
        "single_market_positive_contribution",
        "single_month_positive_contribution",
        "no_repainting",
        "completed_candles_only",
    }
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False


def test_dashboard_exposes_forward_evidence_read_only(tmp_path):
    journal = DecisionJournal(tmp_path / "scanner.jsonl")
    evidence = build_forward_evidence_payload(journal)
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(evidence))
    provider = SnapshotProvider()
    provider.publish({"mode": "research scanner observation"})
    client = TestClient(
        create_app(
            provider,
            token="dashboard-token",
            scanner_forward_evidence_path=path,
        )
    )

    assert client.get("/scanner-evidence").status_code == 401
    response = client.get("/scanner-evidence?token=dashboard-token")
    assert response.status_code == 200
    assert response.json()["policy"]["l2_is_confirmation_only"] is True
    assert response.json()["can_trade"] is False
    assert response.json()["can_promote"] is False
