from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from vnedge.research.delta_scalper_cusum_interactions import build_report

START = datetime(2025, 1, 1, tzinfo=UTC)


def _trade(index: int, *, positive: bool, trend: str) -> dict:
    entry = START + timedelta(minutes=index)
    return {
        "scanner_id": "delta_imbalance_fade_v1",
        "symbol": "BTCUSD",
        "trend_regime_at_entry": trend,
        "trend_direction_at_entry": "flat" if trend == "range" else "up",
        "volatility_regime_at_entry": "low" if trend == "range" else "high",
        "session_regime_at_entry": "asia",
        "change_point_window_at_entry": "00-30m",
        "side": "long",
        "entry_ts": entry.isoformat(),
        "exit_ts": (entry + timedelta(seconds=60)).isoformat(),
        "exit_reason": "target_1" if positive else "stop",
        "hold_seconds": 60,
        "gross_bps": 12.0 if positive else -8.0,
        "cost_bps": 5.0,
        "net_bps": 7.0 if positive else -13.0,
        "mfe_bps": 14.0 if positive else 3.0,
        "mae_bps": 2.0 if positive else 11.0,
        "expected_net_bps": 9.0,
    }


def test_cusum_interactions_enforce_cell_size_and_protect_frozen_tail():
    trades = [
        *[_trade(index, positive=True, trend="range") for index in range(200)],
        *[
            _trade(index, positive=False, trend="strong_trend")
            for index in range(200, 500)
        ],
    ]
    report = build_report(
        {
            "markets": {"BTCUSD": {"trades": trades}},
            "untouched_window": {"untouched_fraction": 0.20},
        },
        minimum_eligible_trades=100,
        minimum_diagnostic_trades=80,
    )

    assert report["selection_window"]["trades"] == 400
    assert report["selection_window"]["eligible_cells"] == 2
    assert report["findings"]["positive_cell_count"] == 1
    assert report["findings"]["materially_worse_cell_count"] == 1
    best = report["findings"]["best_eligible_cells"][0]
    assert best["trend_regime"] == "range"
    assert best["average_net_ci95_low_bps"] > 0
    frozen = report["frozen_untouched_window"]
    assert frozen["trades"] == 100
    assert frozen["subgroup_attribution_performed"] is False
    assert "eligible_cells" not in frozen
    assert report["policy"]["interaction_used_for_scanner_gate"] is False


def test_cusum_interaction_minimums_are_ordered():
    with pytest.raises(ValueError, match="eligible minimum"):
        build_report(
            {"markets": {}, "untouched_window": {"untouched_fraction": 0.2}},
            minimum_eligible_trades=79,
            minimum_diagnostic_trades=80,
        )
