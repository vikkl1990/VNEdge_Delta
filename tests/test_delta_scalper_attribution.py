from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from vnedge.research.delta_scalper_attribution import (
    build_attribution_report,
    load_decision_rejections,
)

START = datetime(2025, 1, 1, tzinfo=UTC)


def _trade(index: int) -> dict:
    winner = index % 3 == 0
    symbol = "BTCUSD" if index < 5 else "ETHUSD"
    return {
        "scanner_id": (
            "delta_momentum_burst_v1" if index % 2 == 0 else "delta_imbalance_fade_v1"
        ),
        "symbol": symbol,
        "regime_at_entry": "expanding" if index % 2 == 0 else "quiet",
        "trend_regime_at_entry": "strong_trend" if index % 2 == 0 else "range",
        "trend_direction_at_entry": "up" if index % 2 == 0 else "flat",
        "volatility_regime_at_entry": "high" if index % 2 == 0 else "low",
        "session_regime_at_entry": "overlap" if index % 2 == 0 else "asia",
        "regime_shift_at_entry": index % 4 == 0,
        "change_point_window_at_entry": "00-30m" if index % 4 == 0 else "04h+",
        "side": "long" if index % 2 == 0 else "short",
        "entry_ts": (START + timedelta(hours=index)).isoformat(),
        "exit_ts": (START + timedelta(hours=index, minutes=5)).isoformat(),
        "exit_reason": "target_1" if winner else "stop",
        "hold_seconds": 300,
        "gross_bps": 12.0 if winner else -8.0,
        "cost_bps": 5.0,
        "net_bps": 7.0 if winner else -13.0,
        "mfe_bps": 15.0 if winner else 4.0,
        "mae_bps": 3.0 if winner else 10.0,
        "expected_net_bps": 9.0,
        "expected_move_bps": 20.0,
        "scalper_probability": 0.8,
        "confidence": 0.82,
        "same_bar_ambiguous": False,
    }


def test_attribution_decomposes_selection_and_protects_frozen_window():
    backtest = {
        "generated_at": START.isoformat(),
        "markets": {
            "BTCUSD": {"trades": [_trade(index) for index in range(5)]},
            "ETHUSD": {"trades": [_trade(index) for index in range(5, 10)]},
        },
        "untouched_window": {"untouched_fraction": 0.2},
    }
    report = build_attribution_report(backtest, [], minimum_cluster_trades=1)

    selection = report["selection_window"]
    frozen = report["frozen_untouched_window"]
    assert selection["summary"]["trades"] == 8
    assert frozen["summary"]["trades"] == 2
    assert frozen["protected_from_threshold_selection"] is True
    assert frozen["subgroup_attribution_performed"] is False
    assert "dimensions" not in frozen
    assert {row["symbol"] for row in selection["dimensions"]["symbol"]} == {
        "BTCUSD",
        "ETHUSD",
    }
    assert selection["dimensions"]["entry_hour_ist"][0]["entry_hour_ist"].endswith(
        "IST"
    )
    assert selection["dimensions"]["move_size_bucket"][0]["move_size_bucket"] == (
        "18-24bps"
    )
    assert selection["dimensions"]["probability_bucket"][0]["probability_bucket"] == (
        "0.78-0.82"
    )
    assert selection["dimensions"]["confidence_bucket"][0]["confidence_bucket"] == (
        "0.76-0.84"
    )
    assert selection["dimensions"]["l2_quality"][0]["l2_quality"] == (
        "historical_unavailable"
    )
    assert selection["largest_loss_clusters"]
    full_cross = selection["dimensions"]["scanner_symbol_regime"]
    assert full_cross
    assert full_cross[0]["pct_of_all_trades"] > 0
    assert full_cross[0]["avg_hold_bars"] == 5.0
    assert selection["dimensions"]["scanner_symbol_trend_volatility"]
    assert selection["dimensions"]["change_point_window"]
    assert selection["dimensions"]["scanner_change_point_window"]
    assert {row["trend_regime"] for row in selection["dimensions"]["trend_regime"]} == {
        "strong_trend",
        "range",
    }
    assert selection["frequency_expectancy_scatter"]
    assert selection["structured_frequency_expectancy_scatter"]
    assert report["full_period_aggregate"]["subgroup_attribution_performed"] is False
    assert selection["signal_quality_diagnostics"]["scalper_probability"][
        "observations"
    ] == 8
    assert "brier_score" in selection["signal_quality_diagnostics"][
        "scalper_probability"
    ]
    assert report["live_shadow"]["status"] == "insufficient_completed_outcomes"
    assert report["policy"]["thresholds_changed"] is False


def test_live_shadow_outcomes_are_attributed_separately():
    historical = _trade(0)
    live = {
        **_trade(1),
        "regime": "quiet",
    }
    live.pop("regime_at_entry")
    report = build_attribution_report(
        {
            "markets": {"BTCUSD": {"trades": [historical]}},
            "untouched_window": {"untouched_fraction": 0.2},
        },
        [live],
        minimum_cluster_trades=1,
    )
    assert report["live_shadow"]["status"] == "ready"
    assert report["live_shadow"]["summary"]["trades"] == 1
    assert report["selection_window"]["summary"]["trades"] == 1


def test_live_rejection_attribution_uses_real_journal_reasons(tmp_path):
    journal = tmp_path / "shadow.jsonl"
    journal.write_text(
        json.dumps(
            {
                "kind": "delta_scalper_research_decision",
                "payload": {
                    "symbol": "BTCUSD",
                    "decision_ts": START.isoformat(),
                    "selected": None,
                    "evaluated": [
                        {
                            "scanner_id": "delta_momentum_burst_v1",
                            "metadata": {
                                "regime": "quiet",
                                "l2_confirmation": {"status": "fresh"},
                            },
                        }
                    ],
                    "rejection_reasons": [
                        "delta_momentum_burst_v1:fee_adjusted_expectancy_below_gate"
                    ],
                    "pipeline_trace": [
                        {
                            "name": "context_builder",
                            "status": "complete",
                            "detail": "quiet",
                        }
                    ],
                },
            }
        )
        + "\n"
    )
    rejections = load_decision_rejections(journal)
    report = build_attribution_report(
        {"markets": {}, "untouched_window": {"untouched_fraction": 0.2}},
        [],
        rejections,
        minimum_cluster_trades=1,
    )

    live = report["live_rejections"]
    assert live["status"] == "ready"
    assert live["rejected_reasons"] == 1
    assert live["dimensions"]["scanner_symbol_regime"][0]["key"] == (
        "delta_momentum_burst_v1 | BTCUSD | quiet"
    )
    assert live["dimensions"]["l2_status"][0]["l2_status"] == "fresh"
    assert live["historical_status"].startswith("unavailable")
