from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from vnedge.research import scanner_brew


def _observation(index: int, *, direction: int = 1) -> dict:
    ts = datetime.fromtimestamp(1 + index * 3, tz=UTC)
    return {
        "kind": "delta_absorption_research_outcome",
        "payload": {
            "key": f"k{index}",
            "symbol": "BTCUSD",
            "decision_ts": ts.isoformat(),
            "entry_price": 100.0,
            "realized_gross_ticks": 8.0,
            "hit_target_1": True,
            "stopped_out": index == 0,
            "was_stacked": False,
            "volume_percentile": 0.9,
            "event": {"reversal_direction": direction, "strength": 0.95},
        },
    }


def test_scanner_brew_is_cost_aware_non_overlapping_and_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = tmp_path / "journal.jsonl"
    journal.write_text("\n".join(json.dumps(_observation(index)) for index in range(2)) + "\n")
    tape = {
        "BTCUSD": {
            "times": [
                1_100_000_000,
                2_100_000_000,
                4_100_000_000,
                5_100_000_000,
                7_100_000_000,
                8_100_000_000,
            ],
            "prices": [100.0, 101.0, 100.0, 101.0, 100.0, 101.0],
        },
        "ETHUSD": {"times": [1], "prices": [100.0]},
    }
    monkeypatch.setattr(scanner_brew, "_load_trade_tape", lambda *args, **kwargs: tape)

    payload = scanner_brew.build_scanner_brew_report(
        journal,
        tmp_path,
        output_path=tmp_path / "result.json",
        entry_delays_ms=(100,),
        hold_horizons_ms=(1_000,),
        round_trip_cost_bps=14.8,
    )

    best = payload["diagnosis"]["best_variant_min_100"]
    assert best is None
    raw = next(row for row in payload["all_variants"] if row["recipe"] == "raw_absorption_reversal")
    assert raw["observations"] == 2
    assert raw["average_gross_bps"] == pytest.approx(100.0)
    assert raw["average_net_bps"] == pytest.approx(85.2)
    assert payload["flaw_decomposition"]["exit"]["stopped_then_later_hit_target"] == 1
    capture = payload["movement_capture"]
    assert capture["verdict"] == "NO_PROVEN_MOVEMENT_SELECTION_OR_AFTER_COST_CAPTURE"
    assert capture["horizons"]
    assert capture["longest_horizon_decomposition"]["round_trip_cost_bps"] == 14.8
    assert payload["scanner_implementation_authorized"] is False
    assert payload["selection_authorized"] is False
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False


def test_scanner_brew_rejects_non_positive_cost(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cost"):
        scanner_brew.build_scanner_brew_report(
            tmp_path / "missing.jsonl",
            tmp_path,
            round_trip_cost_bps=0,
        )


def test_capture_summary_separates_opportunity_direction_and_exit() -> None:
    summary = scanner_brew._capture_summary(
        [
            {
                "oracle_path_bps": 20.0,
                "selected_mfe_bps": 12.0,
                "selected_mae_bps": 8.0,
                "fixed_exit_gross_bps": 3.0,
                "absolute_fixed_exit_bps": 3.0,
            },
            {
                "oracle_path_bps": 10.0,
                "selected_mfe_bps": 4.0,
                "selected_mae_bps": 10.0,
                "fixed_exit_gross_bps": -2.0,
                "absolute_fixed_exit_bps": 2.0,
            },
        ],
        14.8,
    )

    assert summary["average_oracle_path_bps"] == 15.0
    assert summary["average_selected_mfe_bps"] == 8.0
    assert summary["average_fixed_exit_gross_bps"] == 0.5
    assert summary["direction_accuracy"] == 0.5
    assert summary["oracle_path_cost_clear_rate"] == 0.5
    assert summary["selected_mfe_cost_clear_rate"] == 0.0
