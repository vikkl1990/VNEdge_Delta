from datetime import UTC, datetime

import pytest

from vnedge.research.event_direction_response import (
    AbsorptionEvent,
    DirectionRule,
    ResponseSnapshot,
    build_post_event_direction_study,
    classify_direction,
)
from vnedge.research.event_response_atlas import PriceTape


def _event(*, direction: int = 1, ts_us: int = 1_000_000) -> AbsorptionEvent:
    return AbsorptionEvent(
        key=f"event-{ts_us}",
        symbol="ETHUSD",
        decision_ts_us=ts_us,
        level_price=100.0,
        reversal_direction=direction,
        strength=0.8,
        stacked=False,
    )


def _snapshot(
    event: AbsorptionEvent,
    *,
    price: float,
    flow: float,
    book: float,
    delay_ms: int = 250,
) -> ResponseSnapshot:
    requested = event.decision_ts_us + delay_ms * 1_000
    return ResponseSnapshot(
        event_key=event.key,
        symbol=event.symbol,
        response_delay_ms=delay_ms,
        requested_ts_us=requested,
        captured_ts_us=requested + 50_000,
        last_trade_price=price,
        mid=price,
        spread_bps=1.0,
        book_imbalance=book,
        flow_imbalance=flow,
    )


def test_direction_classifier_separates_reclaim_failure_and_ambiguity():
    event = _event(direction=1)
    rule = DirectionRule("aligned", 2.0, 0.15, 0.10)

    assert classify_direction(
        event,
        _snapshot(event, price=100.20, flow=0.4, book=0.3),
        rule,
        tick_size=0.05,
    ) == ("reversal_reclaim", 1)
    assert classify_direction(
        event,
        _snapshot(event, price=99.80, flow=-0.4, book=-0.3),
        rule,
        tick_size=0.05,
    ) == ("continuation_failure", -1)
    assert (
        classify_direction(
            event,
            _snapshot(event, price=100.20, flow=-0.4, book=0.3),
            rule,
            tick_size=0.05,
        )
        is None
    )


def test_study_uses_post_response_entry_costs_and_locked_authority(tmp_path):
    events = tuple(_event(ts_us=1_000_000 + index * 1_000_000) for index in range(10))
    snapshots = {
        (event.key, 250): _snapshot(event, price=100.20, flow=0.4, book=0.3)
        for event in events
    }
    timestamps: list[int] = []
    prices: list[float] = []
    for event in events:
        response_ts = event.decision_ts_us + 300_000
        timestamps.extend((response_ts, response_ts + 300_000_000))
        prices.extend((100.20, 101.202))
    ordered = sorted(zip(timestamps, prices))
    tape = PriceTape(tuple(row[0] for row in ordered), tuple(row[1] for row in ordered))
    rule = DirectionRule("aligned", 2.0, 0.15, 0.10)

    result = build_post_event_direction_study(
        tmp_path / "unused.jsonl",
        output_path=tmp_path / "study.json",
        delays_ms=(250,),
        horizons_ms=(300_000,),
        rules=(rule,),
        observations=events,
        response_snapshots=snapshots,
        tapes={"ETHUSD": tape},
        tick_sizes={"ETHUSD": 0.05},
        minimum_selection_observations=1,
        minimum_validation_observations=1,
        episode_separation_ms=0,
        require_control_gate=False,
        code_version="test",
    )

    all_row = next(row for row in result["economic_matrix"] if row["partition"] == "all_development")
    assert all_row["observations"] == 10
    assert all_row["average_gross_bps"] == pytest.approx(100.0)
    assert all_row["average_net_bps"] == pytest.approx(85.2)
    assert all_row["average_mfe_after_cost_bps"] == pytest.approx(85.2)
    # The shared tape contains subsequent events before each nominal horizon;
    # report the first actual print that reached MFE, not the horizon itself.
    assert all_row["average_time_to_mfe_ms"] == pytest.approx(295_500.0)
    assert all_row["average_capture_ratio"] == pytest.approx(1.0)
    assert all_row["fee_wall_break_rate_pct"] == pytest.approx(100.0)
    assert all_row["exit_diagnosis_counts"] == {"CAPTURED_AFTER_COST": 10}
    assert result["sealed_holdout_opened"] is False
    assert result["scanner_implementation_authorized"] is False
    assert result["paper_authorized"] is False
    assert result["can_trade"] is False
    assert result["can_promote"] is False


def test_response_snapshot_rejects_future_clock_regression():
    event = _event()
    with pytest.raises(ValueError, match="causal clock"):
        ResponseSnapshot(
            event_key=event.key,
            symbol=event.symbol,
            response_delay_ms=250,
            requested_ts_us=2_000_000,
            captured_ts_us=1_999_999,
            last_trade_price=100.0,
            mid=100.0,
            spread_bps=1.0,
            book_imbalance=0.0,
            flow_imbalance=0.0,
        )


def test_generated_timestamp_is_not_part_of_deterministic_hash(tmp_path):
    event = _event()
    snapshot = _snapshot(event, price=100.20, flow=0.4, book=0.3)
    tape = PriceTape(
        (snapshot.captured_ts_us, snapshot.captured_ts_us + 300_000_000),
        (100.20, 101.202),
    )
    kwargs = {
        "delays_ms": (250,),
        "horizons_ms": (300_000,),
        "rules": (DirectionRule("aligned", 2.0, 0.15, 0.10),),
        "observations": (event,),
        "response_snapshots": {(event.key, 250): snapshot},
        "tapes": {"ETHUSD": tape},
        "tick_sizes": {"ETHUSD": 0.05},
        "minimum_selection_observations": 1,
        "minimum_validation_observations": 1,
        "episode_separation_ms": 0,
        "require_control_gate": False,
        "code_version": "test",
    }
    first = build_post_event_direction_study(
        tmp_path / "unused.jsonl", output_path=tmp_path / "one.json", **kwargs
    )
    second = build_post_event_direction_study(
        tmp_path / "unused.jsonl", output_path=tmp_path / "two.json", **kwargs
    )

    assert first["deterministic_result_hash"] == second["deterministic_result_hash"]
    assert datetime.fromisoformat(first["generated_at"]).tzinfo == UTC
