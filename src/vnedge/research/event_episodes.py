"""Independent event episodes and matched movement-control qualification.

Repeated detector publications around one market event are correlated samples,
not independent evidence. This module keeps the first causal detection in an
episode and requires its subsequent direction-neutral movement to exceed a
nearby, non-event control before any direction or exit family may be searched.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping, Protocol, TypeVar


class PriceTapeLike(Protocol):
    timestamps_us: tuple[int, ...]
    prices: tuple[float, ...]

    def first_at_or_after(self, ts_us: int) -> int | None: ...

    def directional_path_stats(
        self,
        start: int,
        stop: int,
        *,
        direction: int,
        entry_price: float,
    ) -> tuple[float, float, float]: ...


T = TypeVar("T")


@dataclass(frozen=True)
class EpisodeCollapseReport:
    raw_detections: int
    independent_episodes: int
    collapsed_detections: int
    separation_ms: int
    policy: str = "first causal detection; new episode only after a quiet separation window"


def collapse_independent_episodes(
    events: Iterable[T],
    *,
    separation_ms: int = 30_000,
) -> tuple[tuple[T, ...], EpisodeCollapseReport]:
    """Collapse same-symbol detections joined by less than ``separation_ms``."""

    if separation_ms < 0:
        raise ValueError("episode separation cannot be negative")
    ordered = sorted(
        events,
        key=lambda row: (
            str(getattr(row, "symbol")).upper(),
            int(getattr(row, "decision_ts_us")),
            str(getattr(row, "key")),
        ),
    )
    kept: list[T] = []
    last_detection_us: dict[str, int] = {}
    separation_us = separation_ms * 1_000
    for event in ordered:
        symbol = str(getattr(event, "symbol")).upper()
        decision_us = int(getattr(event, "decision_ts_us"))
        previous = last_detection_us.get(symbol)
        if previous is None or decision_us - previous > separation_us:
            kept.append(event)
        last_detection_us[symbol] = decision_us
    kept.sort(key=lambda row: (int(getattr(row, "decision_ts_us")), str(getattr(row, "key"))))
    report = EpisodeCollapseReport(
        raw_detections=len(ordered),
        independent_episodes=len(kept),
        collapsed_detections=len(ordered) - len(kept),
        separation_ms=separation_ms,
    )
    return tuple(kept), report


def matched_movement_control_gate(
    events: Iterable[Any],
    tapes: Mapping[str, PriceTapeLike],
    *,
    delay_ms: int = 250,
    horizon_ms: int = 900_000,
    nearby_offset_ms: int = 1_800_000,
    event_exclusion_ms: int = 60_000,
    maximum_entry_wait_ms: int = 2_000,
    minimum_pairs: int = 30,
    minimum_uplift_bps: float = 0.0,
    minimum_pair_win_rate: float = 0.50,
) -> dict[str, Any]:
    """Compare event movement with one-to-one nearby non-event controls.

    The outcome is direction neutral (the better of long/short MFE), so this
    stage establishes only abnormal opportunity. It cannot establish direction
    or a tradeable exit. The exit grid must remain blocked unless this gate
    passes.
    """

    if delay_ms < 0 or horizon_ms <= 0 or nearby_offset_ms <= 0:
        raise ValueError("control delay/horizon/offset contract is invalid")
    if event_exclusion_ms < 0 or maximum_entry_wait_ms < 0 or minimum_pairs <= 0:
        raise ValueError("control exclusion/wait/sample contract is invalid")
    if not 0 <= minimum_pair_win_rate <= 1:
        raise ValueError("minimum pair win rate must be in [0, 1]")

    ordered = sorted(
        events,
        key=lambda row: (int(getattr(row, "decision_ts_us")), str(getattr(row, "key"))),
    )
    event_times: dict[str, list[int]] = defaultdict(list)
    for event in ordered:
        event_times[str(getattr(event, "symbol")).upper()].append(
            int(getattr(event, "decision_ts_us"))
        )
    pairs: list[dict[str, Any]] = []
    unavailable = 0
    excluded_controls = 0
    used_controls: set[tuple[str, int]] = set()
    offset_us = nearby_offset_ms * 1_000
    exclusion_us = event_exclusion_ms * 1_000

    for event in ordered:
        symbol = str(getattr(event, "symbol")).upper()
        decision_us = int(getattr(event, "decision_ts_us"))
        tape = tapes.get(symbol)
        if tape is None:
            unavailable += 1
            continue
        event_mfe = _best_mfe(
            tape,
            decision_us,
            delay_ms=delay_ms,
            horizon_ms=horizon_ms,
            maximum_entry_wait_ms=maximum_entry_wait_ms,
        )
        if event_mfe is None:
            unavailable += 1
            continue
        control_target: int | None = None
        for candidate in (decision_us - offset_us, decision_us + offset_us):
            if candidate < 0 or (symbol, candidate) in used_controls:
                continue
            if any(abs(candidate - ts) <= exclusion_us for ts in event_times[symbol]):
                excluded_controls += 1
                continue
            if _best_mfe(
                tape,
                candidate,
                delay_ms=delay_ms,
                horizon_ms=horizon_ms,
                maximum_entry_wait_ms=maximum_entry_wait_ms,
            ) is not None:
                control_target = candidate
                break
        if control_target is None:
            unavailable += 1
            continue
        control_mfe = _best_mfe(
            tape,
            control_target,
            delay_ms=delay_ms,
            horizon_ms=horizon_ms,
            maximum_entry_wait_ms=maximum_entry_wait_ms,
        )
        if control_mfe is None:  # defensive; availability was checked above
            unavailable += 1
            continue
        used_controls.add((symbol, control_target))
        pairs.append(
            {
                "event_key": str(getattr(event, "key")),
                "symbol": symbol,
                "event_ts_us": decision_us,
                "control_ts_us": control_target,
                "event_best_mfe_bps": event_mfe,
                "control_best_mfe_bps": control_mfe,
                "uplift_bps": event_mfe - control_mfe,
            }
        )

    pair_count = len(pairs)
    avg_event = sum(row["event_best_mfe_bps"] for row in pairs) / pair_count if pairs else 0.0
    avg_control = (
        sum(row["control_best_mfe_bps"] for row in pairs) / pair_count if pairs else 0.0
    )
    uplift = avg_event - avg_control
    win_rate = (
        sum(row["uplift_bps"] > 0 for row in pairs) / pair_count if pairs else 0.0
    )
    checks = {
        "minimum_pairs": pair_count >= minimum_pairs,
        "positive_mean_uplift": uplift > minimum_uplift_bps,
        "pair_win_rate": win_rate > minimum_pair_win_rate,
    }
    return {
        "method": "one_to_one_nearby_non_event_direction_neutral_mfe",
        "delay_ms": delay_ms,
        "horizon_ms": horizon_ms,
        "nearby_offset_ms": nearby_offset_ms,
        "event_exclusion_ms": event_exclusion_ms,
        "minimum_pairs": minimum_pairs,
        "minimum_uplift_bps": minimum_uplift_bps,
        "minimum_pair_win_rate": minimum_pair_win_rate,
        "matched_pairs": pair_count,
        "unavailable_pairs": unavailable,
        "excluded_control_candidates": excluded_controls,
        "average_event_best_mfe_bps": avg_event,
        "average_control_best_mfe_bps": avg_control,
        "average_uplift_bps": uplift,
        "event_outperformance_rate": win_rate,
        "checks": checks,
        "passed": all(checks.values()),
        "exit_testing_authorized": all(checks.values()),
        "pairs": pairs,
    }


def episode_report_dict(report: EpisodeCollapseReport) -> dict[str, Any]:
    return asdict(report)


def _best_mfe(
    tape: PriceTapeLike,
    decision_us: int,
    *,
    delay_ms: int,
    horizon_ms: int,
    maximum_entry_wait_ms: int,
) -> float | None:
    requested = decision_us + delay_ms * 1_000
    start = tape.first_at_or_after(requested)
    if start is None or tape.timestamps_us[start] - requested > maximum_entry_wait_ms * 1_000:
        return None
    end = tape.first_at_or_after(tape.timestamps_us[start] + horizon_ms * 1_000)
    if end is None:
        return None
    entry = tape.prices[start]
    long_mfe, _, _ = tape.directional_path_stats(start, end + 1, direction=1, entry_price=entry)
    short_mfe, _, _ = tape.directional_path_stats(
        start, end + 1, direction=-1, entry_price=entry
    )
    return max(long_mfe, short_mfe)
