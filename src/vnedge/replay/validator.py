"""Semantic validation of a selected recorder window before replay."""

from __future__ import annotations

import json
from collections.abc import Iterable

from vnedge.exchange.delta_event_recorder import BookIntegrityError, DeltaBookIntegrityValidator
from vnedge.replay.models import RecordedEvent, RecordingValidationReport


def _percentiles(values: list[int]) -> dict[str, int | None]:
    if not values:
        return {"p50": None, "p95": None, "p99": None}
    rows = sorted(values)

    def at(portion: float) -> int:
        return rows[min(len(rows) - 1, int((len(rows) - 1) * portion))]

    return {"p50": at(0.50), "p95": at(0.95), "p99": at(0.99)}


def validate_recorded_events(events: Iterable[RecordedEvent]) -> RecordingValidationReport:
    book = DeltaBookIntegrityValidator()
    seen: set[str] = set()
    events_count = 0
    book_events = 0
    sequence_gaps = 0
    checksum_failures = 0
    duplicate_ids = 0
    timestamp_regressions = 0
    local_regressions = 0
    missing_exchange_timestamps = 0
    delays: list[int] = []
    issues: list[str] = []
    previous_order: tuple[int, int, int, str] | None = None
    previous_exchange_us: int | None = None
    previous_local_ns: int | None = None

    for event in events:
        events_count += 1
        if event.event_id in seen:
            duplicate_ids += 1
            issues.append(f"duplicate_event_id:{event.event_id}")
        seen.add(event.event_id)
        if event.exchange_timestamp_us < 0:
            missing_exchange_timestamps += 1
        if previous_order is not None and event.order_key < previous_order:
            local_regressions += 1
            issues.append(f"receive_order_regression:{event.event_id}")
        if (
            previous_exchange_us is not None
            and event.exchange_timestamp_us < previous_exchange_us
        ):
            # Expected across independently published symbols/channels. Report
            # it, but never reorder causally available messages to hide it.
            timestamp_regressions += 1
        if previous_local_ns is not None and event.local_monotonic_ns < previous_local_ns:
            local_regressions += 1
            # Expected when canonical exchange-time ordering differs from wire arrival.
            # It is reported for clock analysis but is not a recording-integrity failure.
        previous_order = event.order_key
        previous_exchange_us = event.exchange_timestamp_us
        previous_local_ns = event.local_monotonic_ns
        delays.append((event.local_recv_ns - event.exchange_timestamp_us * 1_000) // 1_000)
        if event.channel != "ob_updates":
            continue
        book_events += 1
        try:
            book.observe(json.loads(str(event.envelope["raw_text"])))
        except BookIntegrityError as exc:
            if exc.marker == "__ob_sequence_gap__":
                sequence_gaps += 1
            elif exc.marker == "__ob_checksum_mismatch__":
                checksum_failures += 1
            else:
                sequence_gaps += 1
            issues.append(f"{event.event_id}:{exc.marker}")

    if events_count == 0:
        issues.append("empty_replay_window")
    passed = not issues
    return RecordingValidationReport(
        passed=passed,
        events=events_count,
        book_events=book_events,
        sequence_gaps=sequence_gaps,
        checksum_failures=checksum_failures,
        duplicate_event_ids=duplicate_ids,
        timestamp_regressions=timestamp_regressions,
        local_clock_regressions=local_regressions,
        missing_exchange_timestamps=missing_exchange_timestamps,
        clock_delay_percentiles_us=_percentiles(delays),
        issues=tuple(issues),
    )
