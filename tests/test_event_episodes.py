from dataclasses import dataclass

import pytest

from vnedge.research.event_episodes import (
    collapse_independent_episodes,
    matched_movement_control_gate,
)
from vnedge.research.event_response_atlas import PriceTape


@dataclass(frozen=True)
class Event:
    key: str
    symbol: str
    decision_ts_us: int


def test_repeated_detections_are_collapsed_to_first_causal_episode():
    events = (
        Event("a", "ETHUSD", 1_000_000),
        Event("b", "ETHUSD", 20_000_000),
        Event("c", "ETHUSD", 55_000_000),
        Event("d", "BTCUSD", 2_000_000),
    )
    episodes, report = collapse_independent_episodes(events, separation_ms=30_000)
    assert [row.key for row in episodes] == ["a", "d", "c"]
    assert report.raw_detections == 4
    assert report.independent_episodes == 3
    assert report.collapsed_detections == 1


def test_control_gate_requires_event_movement_to_beat_nearby_non_events():
    event = Event("a", "ETHUSD", 2_000_000)
    # Previous control starts at 1.0s; event at 2.0s. Both have a 500ms path.
    tape = PriceTape(
        (1_000_000, 1_500_000, 2_000_000, 2_500_000),
        (100.0, 100.1, 100.0, 101.0),
    )
    gate = matched_movement_control_gate(
        (event,),
        {"ETHUSD": tape},
        delay_ms=0,
        horizon_ms=500,
        nearby_offset_ms=1_000,
        event_exclusion_ms=100,
        maximum_entry_wait_ms=0,
        minimum_pairs=1,
        minimum_uplift_bps=0,
        minimum_pair_win_rate=0.5,
    )
    assert gate["passed"] is True
    assert gate["average_event_best_mfe_bps"] == pytest.approx(100.0)
    assert gate["average_control_best_mfe_bps"] == pytest.approx(10.0)


def test_control_gate_fails_closed_when_no_matched_pair_exists():
    event = Event("a", "ETHUSD", 1_000_000)
    tape = PriceTape((1_000_000, 1_500_000), (100.0, 101.0))
    gate = matched_movement_control_gate(
        (event,),
        {"ETHUSD": tape},
        horizon_ms=500,
        nearby_offset_ms=10_000,
        minimum_pairs=1,
    )
    assert gate["passed"] is False
    assert gate["exit_testing_authorized"] is False
