"""Dashboard bridge for the public-candle MTF/AMF scanner."""

import json
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from vnedge.dashboard import scanner_live
from vnedge.dashboard.app import SnapshotProvider, create_app
from vnedge.dashboard.scanner_bridge import dashboard_scanner_payload
from vnedge.dashboard.scanner_live import build_scanner_snapshot


def mtf_payload(now: datetime) -> dict:
    fresh = (now - timedelta(minutes=20)).isoformat()
    return {
        "generated_at": now.isoformat(),
        "scanner_id": "mtf_amf_rejection_scanner_v1",
        "mode": "delta_india_public_candles_research_only",
        "symbols": {
            "BTCUSD": {
                "scanner_id": "mtf_amf_rejection_scanner_v1",
                "config": {"chart_timeframe": "1h"},
                "summary": {
                    "alerts": 3,
                    "latest_alert": {
                        "symbol": "BTCUSD",
                        "side": "short",
                        "observed_at": fresh,
                        "l2_confirmation": {
                            "status": "aligned",
                            "context_only": True,
                            "used_for_execution": False,
                        },
                        "can_trade": False,
                        "can_promote": False,
                    },
                },
            },
            "ETHUSD": {
                "scanner_id": "mtf_amf_rejection_scanner_v1",
                "config": {"chart_timeframe": "1h"},
                "summary": {"alerts": 0, "latest_alert": None},
            },
        },
        "errors": {"SOLUSD": "temporary public API error"},
        "can_trade": False,
        "can_promote": False,
    }


def test_mtf_scanner_is_adapted_to_three_safe_dashboard_rows():
    now = datetime(2026, 8, 5, 1, 0, tzinfo=UTC)

    payload = dashboard_scanner_payload(mtf_payload(now), now=now)

    rows = {row["symbol"]: row for row in payload["rows"]}
    assert payload["summary"] == {
        "connected_symbols": 3,
        "firing": 1,
        "waiting": 1,
        "stale": 0,
        "errors": 1,
        "source_age_seconds": 0.0,
    }
    assert rows["BTCUSD"]["state"] == "FIRING"
    assert rows["BTCUSD"]["latest_eval"]["side"] == "short"
    assert rows["BTCUSD"]["latest_eval"]["l2_confirmation"]["status"] == "aligned"
    assert rows["BTCUSD"]["latest_eval"]["l2_confirmation"]["used_for_execution"] is False
    assert rows["ETHUSD"]["state"] == "WAITING"
    assert rows["SOLUSD"]["state"] == "DATA_ERROR"
    assert all(row["can_trade"] is False for row in rows.values())
    assert all(row["can_promote"] is False for row in rows.values())
    assert payload["policy"]["order_route_present"] is False


def test_stale_source_cannot_display_a_firing_scanner():
    now = datetime(2026, 8, 5, 1, 0, tzinfo=UTC)
    source = mtf_payload(now - timedelta(minutes=16))

    payload = dashboard_scanner_payload(source, now=now)

    assert payload["summary"]["firing"] == 0
    assert {row["state"] for row in payload["rows"] if row["symbol"] != "SOLUSD"} == {"DATA_STALE"}


def test_existing_realtime_scanner_contract_passes_through_unchanged():
    payload = {
        "mode": "live_observation_not_replay",
        "summary": {"near_trigger": 1},
        "rows": [{"strategy_id": "existing", "state": "NEAR_TRIGGER"}],
        "can_trade": False,
        "can_promote": False,
    }

    assert dashboard_scanner_payload(payload) is payload


def test_token_gated_endpoint_reads_mtf_scanner_file(tmp_path):
    path = tmp_path / "mtf_scanner.json"
    path.write_text(json.dumps(mtf_payload(datetime.now(UTC))))
    provider = SnapshotProvider()
    provider.publish({"mode": "paper (demo replay)"})
    client = TestClient(create_app(provider, token="dashboard-token", realtime_scanner_path=path))

    assert client.get("/realtime-scanner").status_code == 401
    response = client.get("/realtime-scanner?token=dashboard-token")

    assert response.status_code == 200
    assert response.json()["summary"]["connected_symbols"] == 3
    assert response.json()["can_trade"] is False
    assert response.json()["can_promote"] is False


def test_scanner_dashboard_snapshot_has_no_demo_or_execution_state():
    now = datetime(2026, 8, 5, 1, 0, tzinfo=UTC)

    snapshot = build_scanner_snapshot(mtf_payload(now), now=now)

    assert snapshot["mode"] == "research scanner observation"
    assert snapshot["session"]["connected_symbols"] == 3
    assert len(snapshot["lanes"]) == 3
    assert snapshot["positions"] == []
    assert snapshot["open_orders"] == []
    assert snapshot["recent_fills"] == []
    assert snapshot["orders_sent"] == 0
    assert snapshot["live_trading_enabled"] is False
    assert snapshot["can_trade"] is False
    assert snapshot["can_promote"] is False
    assert all(lane["can_trade"] is False for lane in snapshot["lanes"])


def test_dashboard_merges_existing_and_delta_scalper_rows(tmp_path, monkeypatch):
    now = datetime.now(UTC)
    primary = tmp_path / "primary.json"
    scalper = tmp_path / "scalper.json"
    attribution = tmp_path / "attribution.json"
    primary.write_text(json.dumps(mtf_payload(now)))
    scalper.write_text(
        json.dumps(
            {
                "generated_at": now.isoformat(),
                "mode": "delta_scalper_research_shadow",
                "architecture": {"version": "1.0", "safety": {"can_trade": False}},
                "rows": [
                    {
                        "strategy_id": "delta_scalper_engine_v1",
                        "symbol": "BTCUSD",
                        "state": "WAITING",
                        "can_trade": False,
                        "can_promote": False,
                    }
                ],
                "can_trade": False,
                "can_promote": False,
            }
        )
    )
    monkeypatch.setattr(scanner_live, "DELTA_SCALPER_PATH", scalper)
    attribution.write_text(
        json.dumps({"report_id": "delta_scalper_attribution_v1", "can_trade": False})
    )
    monkeypatch.setattr(scanner_live, "DELTA_SCALPER_ATTRIBUTION_PATH", attribution)

    combined = scanner_live.read_scanner_payload(primary)

    assert len(combined["rows"]) == 4
    assert any(row.get("strategy_id") == "delta_scalper_engine_v1" for row in combined["rows"])
    assert combined["delta_scalper"]["architecture"]["version"] == "1.0"
    assert combined["delta_scalper"]["attribution"]["report_id"] == (
        "delta_scalper_attribution_v1"
    )
    assert combined["policy"]["order_route_present"] is False
    assert combined["can_trade"] is False


def test_delta_scalper_endpoint_exposes_research_panels_only(tmp_path):
    path = tmp_path / "combined.json"
    path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "rows": [
                    {
                        "strategy_id": "delta_scalper_engine_v1",
                        "symbol": "BTCUSD",
                        "state": "WAITING",
                    },
                    {"strategy_id": "other", "symbol": "ETHUSD"},
                ],
                "delta_scalper": {
                    "backtest_summary": {"profit_factor": 0.4},
                    "fee_effectiveness": [],
                },
                "can_trade": False,
                "can_promote": False,
            }
        )
    )
    provider = SnapshotProvider()
    provider.publish({"mode": "research"})
    client = TestClient(create_app(provider, token="token", realtime_scanner_path=path))

    assert client.get("/delta-scalper").status_code == 401
    response = client.get("/delta-scalper?token=token")

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["rows"]) == 1
    assert payload["panels"]["backtest_summary"]["profit_factor"] == 0.4
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False
