"""Dashboard bridge for the public-candle MTF/AMF scanner."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from vnedge.dashboard import scanner_live
from vnedge.dashboard.app import SnapshotProvider, create_app
from vnedge.dashboard.scanner_bridge import dashboard_scanner_payload
from vnedge.dashboard.scanner_live import build_scanner_snapshot


def test_combined_snapshot_publish_is_atomic_under_concurrency(
    tmp_path, monkeypatch
) -> None:
    output = tmp_path / "combined.json"
    monkeypatch.setattr(scanner_live, "COMBINED_SCANNER_PATH", output)

    payloads = [{"writer": index, "can_trade": False} for index in range(20)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(scanner_live.publish_combined_payload, payloads))

    published = json.loads(output.read_text())
    assert published in payloads
    assert published["can_trade"] is False
    assert not list(tmp_path.glob("*.tmp"))


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
    assert rows["BTCUSD"]["historical_alerts"] == 3
    assert rows["BTCUSD"]["evaluations"] is None
    assert rows["BTCUSD"]["last_alert_ts"] == (now - timedelta(minutes=20)).isoformat()
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


def test_runtime_heartbeat_cannot_hide_a_stale_closed_candle():
    now = datetime(2026, 8, 5, 1, 0, tzinfo=UTC)
    payload = {
        "generated_at": now.isoformat(),
        "mode": "delta_scalper_research_shadow",
        "rows": [
            {
                "strategy_id": "delta_scalper_engine_v1",
                "symbol": "BTCUSD",
                "state": "WAITING",
                "latest_bar_ts": (now - timedelta(minutes=20)).isoformat(),
            }
        ],
    }

    adapted = dashboard_scanner_payload(payload, now=now)

    assert adapted["rows"][0]["state"] == "DATA_STALE"
    assert adapted["rows"][0]["source_age_seconds"] == 20 * 60
    assert adapted["summary"]["source_age_seconds"] == 20 * 60


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
    btc = next(lane for lane in snapshot["lanes"] if lane["symbol"] == "BTCUSD")
    assert btc["funnel"] == {
        "live_evals": None,
        "live_signals": 1,
        "historical_alerts": 3,
    }
    assert btc["last_fired_ts"] == (now - timedelta(minutes=20)).isoformat()
    assert btc["trade_compatibility"]["state"] == "RESEARCH_ONLY"


def test_scalper_lane_preserves_real_evaluation_counter():
    now = datetime(2026, 8, 5, 1, 0, tzinfo=UTC)
    payload = {
        "generated_at": now.isoformat(),
        "mode": "delta_scalper_research_shadow",
        "rows": [
            {
                "strategy_id": "delta_scalper_engine_v1",
                "symbol": "BTCUSD",
                "timeframe": "1m/5m",
                "state": "WAITING",
                "evaluations": 1_131,
                "alerts": 0,
                "latest_eval": {},
            }
        ],
    }

    snapshot = build_scanner_snapshot(payload, now=now)

    lane = snapshot["lanes"][0]
    assert lane["funnel"]["live_evals"] == 1_131
    assert lane["funnel"]["historical_alerts"] == 0
    assert lane["mode"] == "research_observation"


def test_dashboard_merges_existing_and_delta_scalper_rows(tmp_path, monkeypatch):
    now = datetime.now(UTC)
    primary = tmp_path / "primary.json"
    scalper = tmp_path / "scalper.json"
    attribution = tmp_path / "attribution.json"
    sweep = tmp_path / "sweep.json"
    regime_sweep = tmp_path / "regime-sweep.json"
    change_points = tmp_path / "change-points.json"
    meta_label = tmp_path / "meta-label.json"
    triple_meta_label = tmp_path / "triple-meta-label.json"
    lightgbm_meta_label = tmp_path / "lightgbm-meta-label.json"
    categorical_encoding = tmp_path / "categorical-encoding.json"
    cusum_interactions = tmp_path / "cusum-interactions.json"
    lightgbm_shap = tmp_path / "lightgbm-shap.json"
    active_cost = tmp_path / "active-cost.json"
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
    sweep.write_text(
        json.dumps({"report_id": "delta_scalper_threshold_sweep_v1", "can_trade": False})
    )
    monkeypatch.setattr(scanner_live, "DELTA_SCALPER_SWEEP_PATH", sweep)
    regime_sweep.write_text(
        json.dumps({"report_id": "delta_scalper_regime_sweep_v1", "can_trade": False})
    )
    monkeypatch.setattr(scanner_live, "DELTA_SCALPER_REGIME_SWEEP_PATH", regime_sweep)
    change_points.write_text(
        json.dumps({"report_id": "delta_scalper_change_points_v1", "can_trade": False})
    )
    monkeypatch.setattr(scanner_live, "DELTA_SCALPER_CHANGE_POINTS_PATH", change_points)
    meta_label.write_text(
        json.dumps({"report_id": "delta_scalper_meta_label_v1", "can_trade": False})
    )
    monkeypatch.setattr(scanner_live, "DELTA_SCALPER_META_LABEL_PATH", meta_label)
    triple_meta_label.write_text(
        json.dumps(
            {
                "report_id": "delta_scalper_meta_label_triple_barrier_v1",
                "can_trade": False,
            }
        )
    )
    monkeypatch.setattr(
        scanner_live,
        "DELTA_SCALPER_TRIPLE_BARRIER_META_LABEL_PATH",
        triple_meta_label,
    )
    lightgbm_meta_label.write_text(
        json.dumps(
            {
                "report_id": "delta_scalper_lightgbm_meta_v1",
                "can_trade": False,
            }
        )
    )
    monkeypatch.setattr(
        scanner_live,
        "DELTA_SCALPER_LIGHTGBM_META_LABEL_PATH",
        lightgbm_meta_label,
    )
    categorical_encoding.write_text(
        json.dumps(
            {
                "report_id": "delta_scalper_categorical_encoding_v1",
                "can_trade": False,
            }
        )
    )
    monkeypatch.setattr(
        scanner_live,
        "DELTA_SCALPER_CATEGORICAL_ENCODING_PATH",
        categorical_encoding,
    )
    cusum_interactions.write_text(
        json.dumps(
            {
                "report_id": "delta_scalper_cusum_interactions_v1",
                "can_trade": False,
            }
        )
    )
    monkeypatch.setattr(
        scanner_live,
        "DELTA_SCALPER_CUSUM_INTERACTIONS_PATH",
        cusum_interactions,
    )
    lightgbm_shap.write_text(
        json.dumps(
            {
                "report_id": "delta_scalper_lightgbm_shap_v1",
                "can_trade": False,
            }
        )
    )
    monkeypatch.setattr(
        scanner_live,
        "DELTA_SCALPER_LIGHTGBM_SHAP_PATH",
        lightgbm_shap,
    )
    active_cost.write_text(
        json.dumps(
            {
                "schema_version": "vnedge.delta_active_cost_evidence.v1",
                "metrics": {"profit_factor": 0.15},
                "can_trade": False,
            }
        )
    )
    monkeypatch.setattr(
        scanner_live,
        "DELTA_ACTIVE_COST_EVIDENCE_PATH",
        active_cost,
    )

    combined = scanner_live.read_scanner_payload(primary)

    assert len(combined["rows"]) == 4
    assert any(row.get("strategy_id") == "delta_scalper_engine_v1" for row in combined["rows"])
    assert combined["delta_scalper"]["architecture"]["version"] == "1.0"
    assert combined["delta_scalper"]["attribution"]["report_id"] == ("delta_scalper_attribution_v1")
    assert combined["delta_scalper"]["threshold_sweep"]["report_id"] == (
        "delta_scalper_threshold_sweep_v1"
    )
    assert combined["delta_scalper"]["regime_sweep"]["report_id"] == (
        "delta_scalper_regime_sweep_v1"
    )
    assert combined["delta_scalper"]["change_points"]["report_id"] == (
        "delta_scalper_change_points_v1"
    )
    assert combined["delta_scalper"]["meta_label"]["report_id"] == ("delta_scalper_meta_label_v1")
    assert combined["delta_scalper"]["triple_barrier_meta_label"]["report_id"] == (
        "delta_scalper_meta_label_triple_barrier_v1"
    )
    assert combined["delta_scalper"]["lightgbm_meta_label"]["report_id"] == (
        "delta_scalper_lightgbm_meta_v1"
    )
    assert combined["delta_scalper"]["categorical_encoding"]["report_id"] == (
        "delta_scalper_categorical_encoding_v1"
    )
    assert combined["delta_scalper"]["cusum_interactions"]["report_id"] == (
        "delta_scalper_cusum_interactions_v1"
    )
    assert combined["delta_scalper"]["lightgbm_shap"]["report_id"] == (
        "delta_scalper_lightgbm_shap_v1"
    )
    assert combined["delta_scalper"]["active_cost_evidence"]["metrics"][
        "profit_factor"
    ] == 0.15
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
    lane_ids = {lane["lane_id"] for lane in payload["lanes"]}
    assert lane_ids == {
        "delta_scalper_btcusd",
        "registry_kronos_ethusd_1h_forward_v1",
        "registry_mtf_amf_directional_rejection_v3",
    }
    delta_lane = next(
        lane for lane in payload["lanes"] if lane["lane_id"] == "delta_scalper_btcusd"
    )
    assert delta_lane["why_no_signal"].startswith("All primary scanner hypotheses")
    registry_lane = next(
        lane
        for lane in payload["lanes"]
        if lane["lane_id"] == "registry_mtf_amf_directional_rejection_v3"
    )
    assert registry_lane["evidence"]["verified"] is True
    assert "SAMPLE_BELOW_REQUIRED" in registry_lane["evidence"]["warnings"]
    assert payload["signal_funnel"]["stages"][1] == {
        "id": "scanners",
        "label": "Enabled scanners",
        "count": 0,
        "state": "BLOCKED",
    }
    assert payload["system_health"]["snapshot_available"] is True
    assert payload["system_health"]["journal"] == "unavailable"
    assert payload["multi_tf_state"]["BTCUSD"]["available"] is False
    assert payload["multi_tf_state"]["BTCUSD"]["stack_aligned"] is None
    assert payload["observations"]["count"] == 0
    assert payload["recent_decisions"][0]["decision"] == "MONITOR_ONLY"
    assert payload["panels"]["backtest_summary"]["profit_factor"] == 0.4
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False


def test_delta_scalper_endpoint_uses_recomputed_active_cost_pf(tmp_path):
    path = tmp_path / "combined-active-cost.json"
    active_cost_path = tmp_path / "active-cost-evidence.json"
    active_cost = {
        "metrics": {
            "trades": 10,
            "net_bps": -120.0,
            "average_net_bps": -12.0,
            "profit_factor": 0.15,
        },
        "markets": {"BTCUSD": {"profit_factor": 0.15}},
        "positive_markets": 0,
        "fee_model": {"scalper_opted_in": False},
    }
    active_cost_path.write_text(json.dumps(active_cost))
    path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "rows": [
                    {
                        "strategy_id": "delta_scalper_engine_v1",
                        "symbol": "BTCUSD",
                        "state": "WAITING",
                    }
                ],
                "delta_scalper": {
                    "backtest_summary": {
                        "trades": 10,
                        "profit_factor": 0.4,
                        "data_quality_pass": False,
                    },
                    "fee_effectiveness": [],
                },
                "can_trade": False,
                "can_promote": False,
            }
        )
    )
    provider = SnapshotProvider()
    provider.publish({"mode": "research"})
    client = TestClient(
        create_app(
            provider,
            token="token",
            delta_scalper_path=path,
            delta_active_cost_evidence_path=active_cost_path,
        )
    )

    payload = client.get("/delta-scalper?token=token").json()
    active = payload["panels"]["backtest_summary"]

    assert active["profit_factor"] == 0.15
    assert active["average_net_bps"] == -12.0
    assert active["markets"]["BTCUSD"]["profit_factor"] == 0.15
    assert "recomputed from per-trade" in active["profit_factor_note"]
    assert payload["can_trade"] is False


def test_delta_scalper_endpoint_reads_dedicated_snapshot_directly(tmp_path):
    path = tmp_path / "delta_scalper_engine_latest.json"
    path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "mode": "delta_scalper_research_shadow",
                "summary": {
                    "connected_symbols": 2,
                    "evaluations": 2855,
                    "alerts": 0,
                },
                "architecture": {
                    "components": {
                        "scanner_engine": "available_all_hypotheses_rejected_and_disabled"
                    }
                },
                "backtest_summary": {"trades": 19521, "profit_factor": 0.402},
                "rows": [
                    {
                        "strategy_id": "delta_scalper_engine_v1",
                        "symbol": "BTCUSD",
                        "state": "WAITING",
                        "evaluations": 1400,
                        "alerts": 0,
                        "latest_eval": {
                            "active_regime": "quiet",
                            "pipeline_duration_us": 2500,
                            "pipeline_trace": [
                                {
                                    "name": "fee_probability_confidence_gates",
                                    "status": "complete",
                                    "detail": "0/0 accepted",
                                }
                            ],
                            "l2_confirmation": {"status": "fresh"},
                        },
                    },
                    {
                        "strategy_id": "delta_scalper_engine_v1",
                        "symbol": "ETHUSD",
                        "state": "WAITING",
                        "evaluations": 1455,
                        "alerts": 0,
                        "latest_eval": {
                            "active_regime": "quiet",
                            "pipeline_duration_us": 1800,
                            "pipeline_trace": [
                                {
                                    "name": "fee_probability_confidence_gates",
                                    "status": "complete",
                                    "detail": "0/0 accepted",
                                }
                            ],
                            "l2_confirmation": {"status": "fresh"},
                        },
                    },
                ],
                "can_trade": False,
                "can_promote": False,
            }
        )
    )
    provider = SnapshotProvider()
    provider.publish({"mode": "shadow", "lanes": [{"symbol": "DOGE/USDT:USDT"}]})
    client = TestClient(create_app(provider, token="token", delta_scalper_path=path))

    payload = client.get("/delta-scalper?token=token").json()
    assert [row["symbol"] for row in payload["rows"]] == ["BTCUSD", "ETHUSD"]
    assert payload["identity"]["product"] == "VNEDGE Delta India Research Laboratory"
    assert payload["identity"]["validated_after_cost_edge"] is False
    assert payload["policy"]["validated_edge"] is False
    assert payload["policy"]["order_route"] == "absent"
    assert payload["policy"]["broker"] == "absent"
    assert payload["scanner_status"]["enabled_count"] == 0
    runtime_lanes = [
        lane for lane in payload["lanes"] if lane["lane_id"].startswith("delta_scalper_")
    ]
    assert [lane["symbol"] for lane in runtime_lanes] == ["BTCUSD", "ETHUSD"]
    assert runtime_lanes[0]["latest_candidates"] == 0
    assert runtime_lanes[0]["latest_accepted"] == 0
    registry_lane = next(
        lane
        for lane in payload["lanes"]
        if lane["lane_id"] == "registry_mtf_amf_directional_rejection_v3"
    )
    assert registry_lane["evidence"]["verified"] is True
    assert registry_lane["route_cost_contract"]["contract_id"] == "taker_full_14_8"
    funnel = {stage["id"]: stage for stage in payload["signal_funnel"]["stages"]}
    assert funnel["evaluations"]["count"] == 2855
    assert funnel["scanners"]["state"] == "BLOCKED"
    assert funnel["candidates"]["state"] == "NOT_REACHED"
    assert "intentional safety state" in payload["signal_funnel"]["blocker"]
    assert payload["panels"]["summary"]["evaluations"] == 2855
    assert payload["panels"]["backtest_summary"]["profit_factor"] == 0.402
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False
