"""Chronological indicator-score calibration and dashboard-safe policy tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from vnedge.research.indicator_score_calibration import (
    CalibrationConfig,
    build_indicator_score_calibration,
)


def trade(index: int, *, symbol: str, net_bps: float, quality: float) -> dict[str, object]:
    ts = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=index)
    return {
        "decision_ts": ts.isoformat(),
        "symbol": symbol,
        "scanner_id": "historical_scanner",
        "side": "long",
        "trend_direction_at_entry": "up" if quality > 0.5 else "down",
        "trend_regime_at_entry": "strong_trend" if quality > 0.7 else "range",
        "scalper_probability": quality,
        "confidence": quality,
        "atr_percentile_at_entry": 0.5,
        "bb_width_percentile_at_entry": 0.5,
        "expected_move_bps": 80.0 if quality > 0.7 else 20.0,
        "expected_net_bps": 30.0 if quality > 0.7 else -5.0,
        "modeled_cost_bps": 14.0,
        "planned_stop_bps": 25.0,
        "net_bps": net_bps,
        "mfe_bps": max(net_bps, 1.0),
        "mae_bps": abs(min(net_bps, -1.0)),
        "exit_reason": "target" if net_bps > 0 else "stop",
    }


def test_calibration_uses_selection_boundaries_and_never_promotes() -> None:
    rows = [
        trade(i, symbol="BTCUSD" if i % 2 else "ETHUSD", net_bps=(-8.0 if i < 80 else 8.0), quality=i / 99)
        for i in range(100)
    ]
    payload = {
        "window": {"start": rows[0]["decision_ts"], "end": rows[-1]["decision_ts"]},
        "markets": {
            "BTCUSD": {
                "summary": {"data_quality_pass": True},
                "trades": [row for row in rows if row["symbol"] == "BTCUSD"],
            },
            "ETHUSD": {
                "summary": {"data_quality_pass": True},
                "trades": [row for row in rows if row["symbol"] == "ETHUSD"],
            },
        },
    }
    report = build_indicator_score_calibration(
        payload,
        calibration_config=CalibrationConfig(minimum_top_band_trades=1),
    )
    assert report["chronological_split"]["selection_trades"] == 80
    assert report["chronological_split"]["historical_tail_trades"] == 20
    assert report["chronological_split"]["tail_is_newly_sealed"] is False
    assert len(report["score_boundaries_from_selection"]) == 9
    assert report["can_trade"] is False
    assert report["can_promote"] is False
    assert report["policy"]["used_for_signal"] is False


def test_failed_source_quality_blocks_every_reconstructed_trade() -> None:
    rows = [trade(i, symbol="ETHUSD", net_bps=10.0, quality=0.95) for i in range(20)]
    report = build_indicator_score_calibration(
        {
            "markets": {
                "ETHUSD": {
                    "summary": {"data_quality_pass": False},
                    "trades": rows,
                }
            }
        },
        calibration_config=CalibrationConfig(
            historical_tail_fraction=0.25,
            score_buckets=4,
            minimum_top_band_trades=1,
        ),
    )
    assert report["research_qualified_trades"] == 0
    assert report["gates"]["source_data_quality"]["passed"] is False
    assert report["verdict"] == "NO_CALIBRATED_EDGE"
