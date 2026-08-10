"""Batch contract-risk matrix helpers for the VNEDGE Algo ML Pro scanner."""

from vnedge.research.vnedge_algo_ml_pro_contract_matrix import (
    compact_replay_payload,
    run_contract_risk_matrix,
)


def test_compact_replay_payload_keeps_sizing_and_fee_fields():
    row = compact_replay_payload(
        {
            "exchange": "delta_india",
            "symbol": "ETHUSD",
            "timeframe": "5m",
            "strategy_id": "vnedge_algo_ml_pro_v1",
            "capture_mode": "smart_ladder",
            "bars": 100,
            "sizing_mode": "delta_contract_risk",
            "paper_margin_usd": 100.0,
            "paper_leverage": 25.0,
            "summary": {
                "closed_trades": 12,
                "win_rate_pct": 50.0,
                "profit_factor_r": 1.25,
                "visual_avg_bps": 4.0,
                "fee_aware_avg_bps": -8.5,
                "visual_paper_usd": 10.0,
                "fee_aware_paper_usd": -20.0,
                "promotion_gate": {"passed": False},
                "exit_reason_counts": {"SL": 7, "TP3": 5},
                "position_sizing": {
                    "actual_notional_usd_avg": 376.2,
                    "margin_usd_avg": 15.048,
                    "contracts_avg": 20.0,
                },
            },
        }
    )

    assert row["exchange"] == "delta_india"
    assert row["mode"] == "smart_ladder"
    assert row["fee_avg_bps"] == -8.5
    assert row["actual_notional_avg"] == 376.2
    assert row["contracts_avg"] == 20.0


def test_contract_matrix_records_errors_without_trade_permission(tmp_path):
    payload = run_contract_risk_matrix(
        data_root=tmp_path,
        exchange="delta_india",
        symbols=("MISSINGUSD",),
        timeframes=("5m",),
        capture_modes=("pine_tp3",),
        sizing_mode="fixed_notional",
    )

    assert payload["truth_layer"] == "vnedge_algo_ml_pro_contract_matrix_v1"
    assert payload["summary"]["errors"] == 1
    assert payload["rows"] == []
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False
