"""Unified research evidence index tests."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from vnedge.research.evidence_store import (
    build_research_evidence_index,
    publish_research_evidence_index,
)


def test_evidence_store_normalizes_pine_scanner_arena_and_fee_wall(tmp_path):
    reports = tmp_path / "live_research"
    reports.mkdir()
    pine = tmp_path / "pine_research_kb.json"
    pine.write_text(
        json.dumps(
            {
                "generated_at": "2026-07-23T00:00:00+00:00",
                "records": [
                    {
                        "script_id": "fvg_source",
                        "title": "FVG Source",
                        "url": "user_supplied_pine:fvg.pine",
                        "source_available": True,
                        "source_sha256": "a" * 64,
                        "crypto_portability": "PORTABLE_WITH_CHANGES",
                        "next_action": "PORT_CAUSAL_FEATURES_AND_REPLAY",
                        "backtests": [
                            {
                                "timeframe": "15m",
                                "status": "failed",
                                "venues": ["delta_india"],
                                "samples": 8,
                                "avg_net_bps": 12.5,
                                "profit_factor": 2.4,
                                "tested_strategy": "fvg_liquidity_breakout_v1",
                                "tested_symbol": "ETH/USD:USD",
                                "evidence_source": "daily_scalper_cadence_latest.json",
                                "blocker": "under-sampled",
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (reports / "scanner_backtest_uplift_latest.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-07-23T00:01:00+00:00",
                "top_uplifts": [
                    {
                        "row_id": "doge_15m",
                        "exchange": "delta_india",
                        "symbol": "DOGE/USD:USD",
                        "timeframe": "15m",
                        "strategy_id": "vnedge_algo_ml_pro_v1",
                        "mode": "smart_ladder",
                        "samples": 87,
                        "avg_net_bps": -1.44,
                        "visual_avg_bps": 11.06,
                        "profit_factor": 1.2,
                        "failure_mode": "FEE_WALL_NEAR_MISS",
                        "uplift_action": "TEST_MAKER_FIRST_CONTEXT_FILTERED_ROUTE",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (reports / "scanner_tournament_latest.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-07-23T00:01:30+00:00",
                "candidates": [
                    {
                        "candidate_id": "sats_btc_bybit_5m",
                        "strategy_id": "sats_5m_scalper_v1",
                        "exchange": "bybit",
                        "symbol": "BTC/USDT:USDT",
                        "timeframe": "5m",
                        "verdict": "STRICT_PROOF_WATCHLIST",
                        "routed": 31,
                        "avg_selected_net_bps": 31.25,
                        "profit_factor": 1.72,
                        "win_rate_pct": 61.0,
                        "recommended_action": "PRE_REGISTER_UNTOUCHED_JUDGMENT_WINDOW",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (reports / "alpha_arena_lite_latest.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-07-23T00:02:00+00:00",
                "scorecards": [
                    {
                        "candidate_id": "eth_sparse",
                        "strategy_id": "luxara_live_plan_qtm_v1",
                        "exchange": "delta_india",
                        "symbol": "ETH/USD:USD",
                        "timeframes": ["5m"],
                        "arena_verdict": "EXPAND_UNTOUCHED_SAMPLE",
                        "next_action": "RUN_FROZEN_SETUP_ON_NEXT_UNTOUCHED_WINDOW",
                        "metrics": {
                            "top_avg_net_bps": 497.83,
                            "best_profit_factor": 999.0,
                            "max_samples": 3,
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (reports / "fee_wall_forensics_latest.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-07-23T00:03:00+00:00",
                "reports": [
                    {
                        "exchange": "bybit",
                        "symbol": "BTC/USDT:USDT",
                        "timeframe": "5m",
                        "strategy": "sats_5m_scalper_v1",
                        "opportunity_count": 31,
                        "summary": {
                            "verdict": "MAKER_EDGE",
                            "routed": 31,
                            "avg_selected_net_bps": 31.25,
                            "profit_factor": 1.72,
                            "win_rate_pct": 61.0,
                            "primary_blocker": "clears fee wall",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    payload = build_research_evidence_index(
        report_dir=reports,
        pine_kb_path=pine,
        now=datetime(2026, 7, 23, tzinfo=UTC),
    )

    assert payload["evidence_store_id"] == "research_evidence_index_v1"
    assert payload["summary"]["total_records"] == 5
    assert payload["summary"]["completed_records"] == 5
    assert payload["summary"]["positive_after_cost"] == 4
    assert payload["summary"]["strict_fee_wall_breakers"] == 2
    assert payload["summary"]["canonical_strict_candidates"] == 1
    assert payload["summary"]["untouched_judgment_queue"] == 1
    assert payload["summary"]["sparse_positives"] == 2
    assert payload["summary"]["source_counts"]["pine_script_backtest"] == 1
    assert payload["summary"]["source_counts"]["fee_wall_forensics"] == 1
    assert payload["summary"]["source_counts"]["scanner_tournament"] == 1
    assert payload["fee_wall_breakers"][0]["strategy_id"] == "sats_5m_scalper_v1"
    assert payload["fee_wall_breakers"][0]["can_trade"] is False
    assert payload["fee_wall_breakers"][0]["can_promote"] is False
    assert payload["canonical_candidates"][0]["strategy_id"] == "sats_5m_scalper_v1"
    assert payload["canonical_candidates"][0]["evidence_count"] == 2
    assert payload["canonical_candidates"][0]["next_action"] == "PRE_REGISTER_UNTOUCHED_JUDGMENT"
    assert payload["untouched_judgment_queue"][0]["can_trade"] is False
    assert payload["top_positive"][0]["avg_net_bps"] == 497.83
    assert payload["operator_answer"].startswith("Evidence index found 2 strict fee-wall")
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False


def test_publish_evidence_store_writes_json_feed_and_sqlite(tmp_path):
    reports = tmp_path / "live_research"
    reports.mkdir()
    pine = tmp_path / "pine.json"
    pine.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "script_id": "range_source",
                        "source_available": True,
                        "backtests": [
                            {
                                "timeframe": "5m",
                                "status": "failed",
                                "samples": 22,
                                "avg_net_bps": -4.25,
                                "profit_factor": 0.82,
                                "tested_strategy": "range_expansion_breakout_v1",
                                "blocker": "fee wall",
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    payload = build_research_evidence_index(report_dir=reports, pine_kb_path=pine)
    out = tmp_path / "evidence_index_latest.json"
    sqlite_path = tmp_path / "evidence_index.sqlite"
    feed = tmp_path / "evidence_index_feed.jsonl"

    publish_research_evidence_index(payload, out=out, sqlite_path=sqlite_path, feed=feed)

    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["summary"]["negative_after_cost"] == 1
    assert len(feed.read_text(encoding="utf-8").splitlines()) == 1
    with sqlite3.connect(sqlite_path) as conn:
        rows = conn.execute(
            "SELECT strategy_id, timeframe, avg_net_bps, can_trade, can_promote "
            "FROM evidence_records"
        ).fetchall()
    assert rows == [("range_expansion_breakout_v1", "5m", -4.25, 0, 0)]
