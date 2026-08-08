from vnedge.research.session_liquidity_sweep_report import build_setup_tables


def test_setup_tables_include_zero_setup_days_and_rejection_attribution():
    payload = {
        "untouched": {"status": "sealed"},
        "selection": {
            "metrics": {
                "score_started_at": "2025-01-01T00:01:00+00:00",
                "score_ended_at": "2025-01-03T23:59:00+00:00",
            },
            "setup_records": [
                {
                    "decision_ts": "2025-01-02T13:45:00+00:00",
                    "symbol": "ETHUSD",
                    "session": "new_york",
                    "entry_geometry": {
                        "status": "rejected",
                        "stop_distance_bps": 20.0,
                        "cost_multiple": 1.35,
                    },
                }
            ],
        },
    }

    tables = build_setup_tables(payload)

    assert list(tables["daily"]["setups"]) == [0, 1, 0]
    assert tables["daily"].iloc[1]["entry_rejections"] == 1
    assert tables["monthly"].iloc[0]["eth_setups"] == 1
