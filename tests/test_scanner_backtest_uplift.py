"""Scanner backtest uplift: failed proof becomes usable research direction."""

from datetime import UTC, datetime
import json

import pytest

from vnedge.research.scanner_backtest_uplift import (
    ScannerEvidenceRow,
    classify_evidence_row,
    publish_scanner_backtest_uplift,
    run_scanner_backtest_uplift,
)


def test_fee_wall_near_miss_gets_execution_uplift_action():
    row = ScannerEvidenceRow(
        evidence_source="matrix",
        exchange="delta_india",
        symbol="DOGEUSD",
        timeframe="15m",
        strategy_id="vnedge_algo_ml_pro_v1",
        mode="smart_ladder",
        samples=87,
        avg_net_bps=-1.44,
        visual_avg_bps=11.06,
        profit_factor=1.1978,
        win_rate_pct=41.0,
    )

    uplift = classify_evidence_row(row)

    assert uplift.failure_mode == "FEE_WALL_NEAR_MISS"
    assert uplift.uplift_action == "TEST_MAKER_FIRST_CONTEXT_FILTERED_ROUTE"
    assert uplift.required_uplift_bps == pytest.approx(26.44)
    assert uplift.fee_drag_bps == pytest.approx(12.5)
    assert uplift.can_trade is False
    assert uplift.can_promote is False


def test_sparse_positive_extends_window_without_tuning():
    row = ScannerEvidenceRow(
        evidence_source="matrix",
        exchange="delta_india",
        symbol="BTCUSD",
        timeframe="1h",
        strategy_id="vnedge_algo_ml_pro_v1",
        mode="smart_ladder",
        samples=11,
        avg_net_bps=3.97,
        visual_avg_bps=16.47,
        profit_factor=1.0617,
        win_rate_pct=45.0,
    )

    uplift = classify_evidence_row(row)

    assert uplift.failure_mode == "SPARSE_POSITIVE"
    assert uplift.uplift_action == "EXTEND_SAMPLE_ON_NEXT_UNTOUCHED_WINDOW"


def test_report_ranks_real_matrix_rows_and_creates_experiments(tmp_path):
    matrix = {
        "rows": [
            {
                "exchange": "delta_india",
                "symbol": "BTCUSD",
                "timeframe": "1h",
                "strategy_id": "vnedge_algo_ml_pro_v1",
                "mode": "smart_ladder",
                "closed": 11,
                "fee_avg_bps": 3.97,
                "visual_avg_bps": 16.47,
                "pf_r": 1.0617,
                "win_rate_pct": 45.0,
                "passed": False,
            },
            {
                "exchange": "delta_india",
                "symbol": "DOGEUSD",
                "timeframe": "15m",
                "strategy_id": "vnedge_algo_ml_pro_v1",
                "mode": "smart_ladder",
                "closed": 87,
                "fee_avg_bps": -1.44,
                "visual_avg_bps": 11.06,
                "pf_r": 1.1978,
                "win_rate_pct": 41.0,
                "passed": False,
            },
            {
                "exchange": "delta_india",
                "symbol": "ETHUSD",
                "timeframe": "4h",
                "strategy_id": "vnedge_algo_ml_pro_v1",
                "mode": "pine_tp3",
                "closed": 0,
                "fee_avg_bps": None,
                "visual_avg_bps": None,
                "pf_r": None,
                "passed": False,
            },
        ]
    }

    payload = run_scanner_backtest_uplift(
        evidence_payloads=[matrix],
        source_names=["delta_contract_matrix"],
        now=datetime(2026, 7, 21, tzinfo=UTC),
    )

    assert payload["agent_id"] == "scanner_backtest_uplift_v1"
    assert payload["summary"]["evidence_rows"] == 3
    assert payload["summary"]["fee_wall_near_misses"] == 1
    assert payload["summary"]["can_trade"] is False
    assert payload["experiments"]
    assert payload["experiments"][0]["can_promote"] is False
    by_mode = payload["summary"]["failure_modes"]
    assert by_mode["SPARSE_POSITIVE"] == 1
    assert by_mode["NO_TRADES"] == 1

    out = tmp_path / "scanner_backtest_uplift_latest.json"
    feed = tmp_path / "scanner_backtest_uplift_feed.jsonl"
    publish_scanner_backtest_uplift(payload, out=out, feed=feed)
    saved = json.loads(out.read_text())
    assert saved["agent_id"] == "scanner_backtest_uplift_v1"
    assert len(feed.read_text().splitlines()) == 1


def test_report_does_not_drop_payloads_when_source_names_are_short():
    payload = run_scanner_backtest_uplift(
        evidence_payloads=[
            {"rows": [{"symbol": "BTCUSD", "timeframe": "4h", "closed": 0}]},
            {"rows": [{"symbol": "ETHUSD", "timeframe": "4h", "closed": 0}]},
        ],
        source_names=["only_first_named"],
    )

    assert payload["summary"]["evidence_rows"] == 2
