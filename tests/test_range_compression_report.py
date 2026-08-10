from vnedge.research.range_compression_report import build_period_tables


def test_period_tables_include_zero_trade_calendar_days_and_keep_tail_sealed():
    payload = {
        "untouched": {"status": "sealed"},
        "selection": {
            "metrics": {
                "score_started_at": "2025-01-01T00:05:00+00:00",
                "score_ended_at": "2025-01-03T23:55:00+00:00",
            },
            "trades": [
                {"exit_ts": "2025-01-02T12:00:00+00:00", "net_bps": 4.0}
            ],
        },
    }

    tables = build_period_tables(payload)

    assert list(tables["daily"]["period"]) == [
        "2025-01-01",
        "2025-01-02",
        "2025-01-03",
    ]
    assert list(tables["daily"]["trades"]) == [0, 1, 0]
    assert tables["monthly"].iloc[0]["net_bps"] == 4.0
