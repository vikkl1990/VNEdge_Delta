"""Pine backtest-evidence overlay tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from vnedge.research.pine_backtest_evidence import publish_pine_backtest_evidence


def test_pine_backtest_evidence_attaches_primitive_results(tmp_path):
    kb = tmp_path / "kb.json"
    distiller = tmp_path / "distiller.json"
    reports = tmp_path / "reports"
    reports.mkdir()
    out = tmp_path / "out.json"
    kb.write_text(json.dumps({
        "generated_at": "2026-07-18T00:00:00+00:00",
        "source": "unit",
        "records": [
            {
                "script_id": "fvg_source",
                "title": "FVG Source",
                "url": "user_supplied_pine:fvg.pine",
                "source_available": True,
                "source_sha256": "a" * 64,
                "source_lines": 40,
                "crypto_portability": "PORTABLE_WITH_CHANGES",
                "crypto_fit_score": 80,
                "backtests": [{"timeframe": "15m", "status": "queued"}],
            }
        ],
    }), encoding="utf-8")
    distiller.write_text(json.dumps({
        "script_distillations": [
            {
                "script_id": "fvg_source",
                "recommended_port": "fvg_liquidity_breakout_v1",
                "action": "PORT_CANDIDATE",
            }
        ]
    }), encoding="utf-8")
    (reports / "daily_scalper_cadence_latest.json").write_text(json.dumps({
        "results": [
            {
                "strategy": "daily_scalper_pack_v1",
                "candidate_family": "fvg_retest",
                "exchange": "binanceusdm",
                "symbol": "SOL/USDT:USDT",
                "timeframe": "15m",
                "oos_trades": 8,
                "avg_net_bps": 12.5,
                "profit_factor": 2.4,
                "verdict": "SMOKE_REJECT",
                "reasons": ["smoke trades too few: 8 < 20"],
                "updated": "2026-07-18T12:00:00+00:00",
            }
        ]
    }), encoding="utf-8")

    payload = publish_pine_backtest_evidence(
        kb_path=kb,
        distiller_path=distiller,
        report_dir=reports,
        output_path=out,
        feed_path=None,
        now=datetime(2026, 7, 18, tzinfo=UTC),
    )

    record = payload["records"][0]
    cells = {cell["timeframe"]: cell for cell in record["backtests"]}
    assert cells["15m"]["status"] == "failed"
    assert cells["15m"]["samples"] == 8
    assert cells["15m"]["avg_net_bps"] == 12.5
    assert cells["15m"]["profit_factor"] == 2.4
    assert cells["15m"]["evidence_source"] == "daily_scalper_cadence_latest.json"
    assert cells["1m"]["status"] == "not_applicable"
    assert cells["1h"]["status"] == "not_applicable"
    assert payload["backtest_evidence"]["completed_cells"] == 1
    assert payload["backtest_evidence"]["positive_completed_cells"] == 1
    assert payload["backtest_evidence"]["best_positive_avg_net_bps"] == 12.5
    assert payload["backtest_evidence"]["best_positive_profit_factor"] == 2.4
    assert payload["backtest_evidence"]["headline_verdict"] == "POSITIVE_COMPLETED_EVIDENCE"
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False
    assert json.loads(out.read_text(encoding="utf-8"))["summary"]["backtests_queued"] == 1
    assert out.stat().st_mode & 0o777 == 0o644


def test_pine_backtest_evidence_blocks_catalog_and_repaint_rows(tmp_path):
    kb = tmp_path / "kb.json"
    distiller = tmp_path / "distiller.json"
    kb.write_text(json.dumps({
        "records": [
            {
                "script_id": "catalog_only",
                "title": "Catalog Only",
                "url": "https://www.tradingview.com/script/example/",
                "source_available": False,
                "crypto_portability": "BLOCKED_NO_SOURCE",
            },
            {
                "script_id": "repaint_source",
                "title": "Repaint Source",
                "source_available": True,
                "crypto_portability": "BLOCKED_REPAINT_RISK",
            },
        ],
    }), encoding="utf-8")
    distiller.write_text(json.dumps({
        "script_distillations": [
            {
                "script_id": "repaint_source",
                "recommended_port": "causality_quarantine_v1",
                "action": "CAUSALITY_QUARANTINE",
            }
        ]
    }), encoding="utf-8")

    payload = publish_pine_backtest_evidence(
        kb_path=kb,
        distiller_path=distiller,
        report_dir=tmp_path / "reports",
        output_path=tmp_path / "out.json",
        feed_path=None,
        now=datetime(2026, 7, 18, tzinfo=UTC),
    )

    by_id = {row["script_id"]: row for row in payload["records"]}
    assert {cell["status"] for cell in by_id["catalog_only"]["backtests"]} == {"blocked"}
    assert {cell["status"] for cell in by_id["repaint_source"]["backtests"]} == {"blocked"}
    assert "source unavailable" in by_id["catalog_only"]["backtests"][0]["blocker"]
    assert "causality" in by_id["repaint_source"]["backtests"][0]["blocker"]


def test_pine_backtest_evidence_does_not_headline_pf_without_positive_net(tmp_path):
    kb = tmp_path / "kb.json"
    distiller = tmp_path / "distiller.json"
    reports = tmp_path / "reports"
    reports.mkdir()
    kb.write_text(json.dumps({
        "generated_at": "2026-07-18T00:00:00+00:00",
        "source": "unit",
        "records": [
            {
                "script_id": "negative_pf999",
                "title": "Negative PF 999",
                "url": "user_supplied_pine:negative.pine",
                "source_available": True,
                "source_sha256": "b" * 64,
                "source_lines": 40,
                "crypto_portability": "PORTABLE",
                "crypto_fit_score": 80,
            }
        ],
    }), encoding="utf-8")
    distiller.write_text(json.dumps({
        "script_distillations": [
            {
                "script_id": "negative_pf999",
                "recommended_port": "range_expansion_breakout_v1",
                "action": "PORT_CANDIDATE",
            }
        ]
    }), encoding="utf-8")
    (reports / "daily_scalper_cadence_latest.json").write_text(json.dumps({
        "results": [
            {
                "strategy": "daily_scalper_pack_v1",
                "candidate_family": "squeeze_release",
                "exchange": "delta_india",
                "symbol": "ETH/USD:USD",
                "timeframe": "15m",
                "oos_trades": 1,
                "avg_net_bps": -15.57,
                "profit_factor": 999.0,
                "verdict": "SMOKE_REJECT",
                "reasons": ["negative after costs"],
                "updated": "2026-07-18T12:00:00+00:00",
            }
        ]
    }), encoding="utf-8")

    payload = publish_pine_backtest_evidence(
        kb_path=kb,
        distiller_path=distiller,
        report_dir=reports,
        output_path=tmp_path / "out.json",
        feed_path=None,
        now=datetime(2026, 7, 18, tzinfo=UTC),
    )

    evidence = payload["backtest_evidence"]
    assert evidence["completed_cells"] == 1
    assert evidence["positive_completed_cells"] == 0
    assert evidence["best_positive_avg_net_bps"] is None
    assert evidence["best_positive_profit_factor"] is None
    assert evidence["best_completed_avg_net_bps"] == -15.57
    assert evidence["best_completed_profit_factor"] == 999.0
    assert evidence["headline_verdict"] == "NO_POSITIVE_COMPLETED_EDGE"


def test_pine_backtest_evidence_ingests_fee_wall_forensics(tmp_path):
    kb = tmp_path / "kb.json"
    distiller = tmp_path / "distiller.json"
    reports = tmp_path / "reports"
    reports.mkdir()
    kb.write_text(json.dumps({
        "generated_at": "2026-07-18T00:00:00+00:00",
        "source": "unit",
        "records": [
            {
                "script_id": "range_source",
                "title": "Range Source",
                "source_available": True,
                "crypto_portability": "PORTABLE",
                "crypto_fit_score": 80,
            },
            {
                "script_id": "trail_source",
                "title": "Trail Source",
                "source_available": True,
                "crypto_portability": "PORTABLE_WITH_CHANGES",
                "crypto_fit_score": 75,
            },
        ],
    }), encoding="utf-8")
    distiller.write_text(json.dumps({
        "script_distillations": [
            {
                "script_id": "range_source",
                "recommended_port": "range_expansion_breakout_v1",
                "action": "PORT_CANDIDATE",
            },
            {
                "script_id": "trail_source",
                "recommended_port": "trail_exit_lab_v1",
                "action": "PORT_CANDIDATE",
            },
        ]
    }), encoding="utf-8")
    (reports / "fee_wall_forensics_latest.json").write_text(json.dumps({
        "generated_at": "2026-07-19T12:00:00+00:00",
        "reports": [
            {
                "exchange": "binanceusdm",
                "symbol": "SOL/USDT:USDT",
                "timeframe": "15m",
                "strategy": "luxara_live_plan_qtm_v1",
                "generated_at": "2026-07-19T12:01:00+00:00",
                "opportunity_count": 12,
                "summary": {
                    "verdict": "MAKER_EDGE",
                    "routed": 12,
                    "avg_selected_net_bps": 18.25,
                    "profit_factor": 1.32,
                    "win_rate_pct": 58.3,
                    "primary_blocker": "maker-first opportunities clear edge gate",
                    "exit_diagnosis_counts": {"CAPTURED_AFTER_COST": 8},
                },
            },
            {
                "exchange": "bybit",
                "symbol": "BTC/USDT:USDT",
                "timeframe": "5m",
                "strategy": "luxy_ut_bot_forecast_v1",
                "opportunity_count": 2,
                "summary": {
                    "verdict": "UNDER_SAMPLED",
                    "routed": 2,
                    "avg_selected_net_bps": 42.0,
                    "profit_factor": 999.0,
                    "win_rate_pct": 100.0,
                    "primary_blocker": "only 2 routed events; need >= 10",
                    "exit_diagnosis_counts": {"CAPTURED_AFTER_COST": 2},
                },
            },
        ],
    }), encoding="utf-8")

    payload = publish_pine_backtest_evidence(
        kb_path=kb,
        distiller_path=distiller,
        report_dir=reports,
        output_path=tmp_path / "out.json",
        feed_path=None,
        now=datetime(2026, 7, 20, tzinfo=UTC),
    )

    by_id = {row["script_id"]: row for row in payload["records"]}
    range_cells = {cell["timeframe"]: cell for cell in by_id["range_source"]["backtests"]}
    trail_cells = {cell["timeframe"]: cell for cell in by_id["trail_source"]["backtests"]}
    assert range_cells["15m"]["status"] == "passed"
    assert range_cells["15m"]["evidence_source"] == "fee_wall_forensics_latest.json"
    assert range_cells["15m"]["tested_strategy"] == "luxara_live_plan_qtm_v1"
    assert range_cells["15m"]["samples"] == 12
    assert range_cells["15m"]["avg_net_bps"] == 18.25
    assert "MAKER_EDGE" in range_cells["15m"]["blocker"]
    assert trail_cells["5m"]["status"] == "failed"
    assert trail_cells["5m"]["avg_net_bps"] == 42.0
    assert "UNDER_SAMPLED" in trail_cells["5m"]["blocker"]
    assert payload["backtest_evidence"]["completed_cells"] == 2
    assert payload["backtest_evidence"]["positive_completed_cells"] == 2
