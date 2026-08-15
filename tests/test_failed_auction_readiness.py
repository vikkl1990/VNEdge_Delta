from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from vnedge.replay.models import RecordedEvent, RecordingValidationReport
from vnedge.research.failed_auction_readiness import (
    RecordingCoverageSummary,
    evaluate_failed_auction_readiness,
    load_failed_auction_contract,
    summarize_recording_coverage,
)


ROOT = Path(__file__).resolve().parents[1]


def _validation(**overrides: object) -> RecordingValidationReport:
    defaults: dict[str, object] = {
        "passed": True,
        "events": 5_000_000,
        "book_events": 4_000_000,
        "sequence_gaps": 0,
        "checksum_failures": 0,
        "duplicate_event_ids": 0,
        "timestamp_regressions": 0,
        "local_clock_regressions": 0,
        "missing_exchange_timestamps": 0,
        "clock_delay_percentiles_us": {"p50": 1, "p95": 2, "p99": 3},
        "negative_delay_samples": 0,
        "channel_timestamp_regressions": {},
        "clock_delay_by_channel_us": {},
        "source_delay_classification_by_channel": {},
        "ordering_policy": "local_receive_availability_order",
        "exchange_timestamp_regressions_blocking": False,
        "latency_interpretation": "test",
        "issues": (),
    }
    defaults.update(overrides)
    return RecordingValidationReport(**defaults)  # type: ignore[arg-type]


def _coverage() -> RecordingCoverageSummary:
    hours = 14 * 24
    return RecordingCoverageSummary(
        requested_start_ts_us=0,
        requested_end_ts_us=14 * 86_400_000_000,
        total_events=5_000_000,
        counts_by_symbol={"BTCUSD": 2_500_000, "ETHUSD": 2_500_000},
        counts_by_symbol_channel={
            "BTCUSD:trades": 500_000,
            "BTCUSD:ob_updates": 2_000_000,
            "ETHUSD:trades": 500_000,
            "ETHUSD:ob_updates": 2_000_000,
        },
        covered_hours_by_symbol_channel={
            "BTCUSD:trades": hours,
            "BTCUSD:ob_updates": hours,
            "ETHUSD:trades": hours,
            "ETHUSD:ob_updates": hours,
        },
        snapshot_counts_by_symbol={"BTCUSD": 2, "ETHUSD": 2},
        book_interarrival_p95_ms_by_symbol={"BTCUSD": 100.0, "ETHUSD": 120.0},
    )


def _event(index: int, channel: str, *, action: str | None = None) -> RecordedEvent:
    message: dict[str, object] = {"type": channel}
    if action is not None:
        message["action"] = action
    return RecordedEvent(
        event_id=f"event-{index}",
        symbol="BTCUSD",
        exchange_timestamp_us=index * 1_000_000,
        local_recv_ns=index * 1_000_000_000,
        local_monotonic_ns=index * 1_000_000_000,
        event_index=index,
        sequence=index if channel == "ob_updates" else None,
        checksum=None,
        channel=channel,
        raw_message=message,
        event_type="l2_update" if channel == "ob_updates" else "trade",
        envelope={"record_kind": "exchange"},
        parsed=message,
    )


def test_frozen_contract_is_research_only_and_cost_gate_is_unambiguous() -> None:
    contract = load_failed_auction_contract(
        ROOT / "configs/research/failed_auction_response_v1.yaml"
    )
    assert contract.status == "blocked_pending_data"
    assert contract.research_only is True
    assert contract.can_trade is False
    assert contract.can_promote is False
    assert contract.scanner_implementation_authorized is False
    assert contract.selection_authorized is False
    assert contract.minimum_gross_target_bps(8.0) == pytest.approx(49.0)


def test_readiness_pass_means_data_only_and_never_authorizes_selection() -> None:
    contract = load_failed_auction_contract(
        ROOT / "configs/research/failed_auction_response_v1.yaml"
    )
    report = evaluate_failed_auction_readiness(
        contract,
        _validation(),
        _coverage(),
        manifest_verified=True,
    )
    assert report.data_ready is True
    assert report.blockers == ()
    assert report.scanner_implementation_authorized is False
    assert report.selection_authorized is False
    assert report.can_trade is False
    assert report.can_promote is False


def test_readiness_fails_closed_on_short_or_damaged_recording() -> None:
    contract = load_failed_auction_contract(
        ROOT / "configs/research/failed_auction_response_v1.yaml"
    )
    short = replace(
        _coverage(),
        requested_end_ts_us=86_400_000_000,
        total_events=10,
        counts_by_symbol={"BTCUSD": 10},
        counts_by_symbol_channel={"BTCUSD:trades": 5, "BTCUSD:ob_updates": 5},
        covered_hours_by_symbol_channel={"BTCUSD:trades": 1},
        snapshot_counts_by_symbol={},
        book_interarrival_p95_ms_by_symbol={},
    )
    report = evaluate_failed_auction_readiness(
        contract,
        _validation(passed=False, sequence_gaps=1, issues=("gap",)),
        short,
        manifest_verified=False,
        partial_files=1,
    )
    assert report.data_ready is False
    assert "manifest_verification_failed" in report.blockers
    assert "partial_files_present:1" in report.blockers
    assert "sequence_gaps:1>0" in report.blockers
    assert any(value.startswith("continuous_days:") for value in report.blockers)
    assert any(value.startswith("ETHUSD:events:") for value in report.blockers)


def test_coverage_summary_tracks_snapshots_hours_and_book_interarrival() -> None:
    events = [
        _event(1, "ob_updates", action="snapshot"),
        _event(2, "ob_updates", action="update"),
        _event(3, "trades"),
    ]
    summary = summarize_recording_coverage(
        events,
        start_ts_us=0,
        end_ts_us=4_000_000,
    )
    assert summary.total_events == 3
    assert summary.snapshot_counts_by_symbol == {"BTCUSD": 1}
    assert summary.counts_by_symbol_channel["BTCUSD:ob_updates"] == 2
    assert summary.covered_hours_by_symbol_channel["BTCUSD:trades"] == 1
    assert summary.book_interarrival_p95_ms_by_symbol["BTCUSD"] == 1000.0


def test_cost_gate_rejects_non_positive_stop() -> None:
    contract = load_failed_auction_contract(
        ROOT / "configs/research/failed_auction_response_v1.yaml"
    )
    with pytest.raises(ValueError, match="stop_bps must be positive"):
        contract.minimum_gross_target_bps(0)
