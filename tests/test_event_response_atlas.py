import json
from datetime import UTC, datetime

import pytest

from vnedge.research.event_response_atlas import PriceTape, build_event_response_atlas


def _write_observation(path, *, key="event-1", direction=1) -> None:
    payload = {
        "kind": "delta_absorption_research_observation",
        "payload": {
            "key": key,
            "decision_ts": datetime.fromtimestamp(1, tz=UTC).isoformat(),
            "symbol": "ETHUSD",
            "volume_percentile": 0.9,
            "event": {
                "symbol": "ETHUSD",
                "reversal_direction": direction,
                "strength": 0.85,
                "is_stacked": True,
            },
        },
    }
    path.write_text(json.dumps(payload) + "\n")


def test_atlas_separates_opportunity_direction_delay_and_cost(tmp_path):
    journal = tmp_path / "journal.jsonl"
    output = tmp_path / "atlas.json"
    _write_observation(journal)
    tape = PriceTape(
        timestamps_us=(1_000_000, 1_250_000, 61_250_000, 181_250_000),
        prices=(100.0, 100.0, 101.0, 99.0),
    )

    result = build_event_response_atlas(
        journal,
        output_path=output,
        delays_ms=(0, 250),
        horizons_ms=(60_000, 180_000),
        minimum_cell_observations=1,
        require_control_gate=False,
        tapes={"ETHUSD": tape},
        code_version="test",
    )

    rows = result["direction_entry_exit_matrix"]
    reversal_60 = next(
        row
        for row in rows
        if row["hypothesis"] == "reversal"
        and row["entry_delay_ms"] == 250
        and row["horizon_ms"] == 60_000
    )
    continuation_60 = next(
        row
        for row in rows
        if row["hypothesis"] == "continuation"
        and row["entry_delay_ms"] == 250
        and row["horizon_ms"] == 60_000
    )
    assert reversal_60["average_gross_bps"] == pytest.approx(100.0)
    assert reversal_60["average_net_bps"] == pytest.approx(85.2)
    assert reversal_60["average_mfe_after_cost_bps"] == pytest.approx(85.2)
    assert reversal_60["average_time_to_mfe_ms"] == pytest.approx(60_000.0)
    assert reversal_60["average_capture_ratio"] == pytest.approx(1.0)
    assert reversal_60["fee_wall_break_rate_pct"] == pytest.approx(100.0)
    assert reversal_60["exit_diagnosis_counts"] == {"CAPTURED_AFTER_COST": 1}
    assert continuation_60["average_gross_bps"] == pytest.approx(-100.0)
    assert result["opportunity_atlas"][0]["hindsight_opportunity_only"] is True
    assert result["can_trade"] is False
    assert result["can_promote"] is False
    assert result["scanner_implementation_authorized"] is False
    assert result["sealed_holdout_opened"] is False
    assert json.loads(output.read_text())["deterministic_result_hash"] == result[
        "deterministic_result_hash"
    ]


def test_atlas_rejects_late_entry_print(tmp_path):
    journal = tmp_path / "journal.jsonl"
    _write_observation(journal)
    tape = PriceTape(
        timestamps_us=(5_000_000, 65_000_000),
        prices=(100.0, 101.0),
    )

    result = build_event_response_atlas(
        journal,
        output_path=tmp_path / "atlas.json",
        delays_ms=(0,),
        horizons_ms=(60_000,),
        maximum_entry_wait_ms=2_000,
        minimum_cell_observations=1,
        require_control_gate=False,
        tapes={"ETHUSD": tape},
    )

    assert result["coverage"]["evaluated_entries"] == 0
    assert result["coverage"]["missed_entries"] == 1
    assert result["direction_entry_exit_matrix"] == []


def test_price_tape_must_be_availability_ordered():
    with pytest.raises(ValueError, match="availability ordered"):
        PriceTape((2, 1), (100.0, 101.0))


def test_atlas_hash_is_deterministic_across_output_times(tmp_path):
    journal = tmp_path / "journal.jsonl"
    _write_observation(journal)
    tape = PriceTape(
        timestamps_us=(1_000_000, 61_000_000),
        prices=(100.0, 101.0),
    )
    first = build_event_response_atlas(
        journal,
        output_path=tmp_path / "first.json",
        delays_ms=(0,),
        horizons_ms=(60_000,),
        minimum_cell_observations=1,
        require_control_gate=False,
        tapes={"ETHUSD": tape},
    )
    second = build_event_response_atlas(
        journal,
        output_path=tmp_path / "second.json",
        delays_ms=(0,),
        horizons_ms=(60_000,),
        minimum_cell_observations=1,
        require_control_gate=False,
        tapes={"ETHUSD": tape},
    )

    assert first["deterministic_result_hash"] == second["deterministic_result_hash"]
