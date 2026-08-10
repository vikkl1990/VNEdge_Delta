"""Deterministic economic and latency summaries for event replay."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from statistics import fmean

from vnedge.replay.outcomes import ReplayForwardOutcome


def latency_percentiles(values: Iterable[int]) -> dict[str, int | None]:
    rows = sorted(int(value) for value in values)
    if not rows:
        return {"count": 0, "p50_ns": None, "p95_ns": None, "p99_ns": None}

    def at(portion: float) -> int:
        return rows[min(len(rows) - 1, int((len(rows) - 1) * portion))]

    return {
        "count": len(rows),
        "p50_ns": at(0.50),
        "p95_ns": at(0.95),
        "p99_ns": at(0.99),
    }


def replay_economic_summary(
    journal_records: Iterable[Mapping[str, object]],
    *,
    decisions: int,
    evaluated_candidates: int,
    selected_candidates: int,
    replay_outcomes: Iterable[ReplayForwardOutcome] = (),
) -> dict[str, object]:
    outcomes: list[Mapping[str, object]] = []
    for record in journal_records:
        if record.get("kind") != "delta_absorption_research_outcome":
            continue
        payload = record.get("payload")
        if isinstance(payload, Mapping):
            outcomes.append(payload)
    completed = [row for row in outcomes if row.get("realized_exit_reason") != "missed_entry"]
    net = [float(row.get("realized_net_ticks", 0.0)) for row in completed]
    gains = sum(value for value in net if value > 0)
    losses = abs(sum(value for value in net if value < 0))
    event_rows = list(replay_outcomes)
    event_completed = [
        row for row in event_rows if row.exit_reason in {"target_1", "stop", "time_stop"}
    ]
    event_net = [row.net_bps for row in event_completed]
    event_gains = sum(value for value in event_net if value > 0)
    event_losses = abs(sum(value for value in event_net if value < 0))
    return {
        "decisions": decisions,
        "evaluated_candidates": evaluated_candidates,
        "selected_candidates": selected_candidates,
        "resolved_research_outcomes": len(completed),
        "missed_entries": len(outcomes) - len(completed),
        "average_mfe_ticks": (
            fmean(float(row.get("mfe_ticks", 0.0)) for row in completed)
            if completed
            else 0.0
        ),
        "average_mae_ticks": (
            fmean(float(row.get("mae_ticks", 0.0)) for row in completed)
            if completed
            else 0.0
        ),
        "net_expectancy_ticks": fmean(net) if net else 0.0,
        # None is strict-JSON-safe and explicitly means no realized losing ticks.
        "profit_factor": gains / losses if losses else (None if gains else 0.0),
        "target_1_win_rate": (
            fmean(row.get("realized_exit_reason") == "target_1" for row in completed)
            if completed
            else 0.0
        ),
        "false_absorption_rate": (
            fmean(bool(row.get("stopped_out", False)) for row in completed)
            if completed
            else 0.0
        ),
        "event_forward_outcomes": len(event_completed),
        "event_unresolved_tail": len(event_rows) - len(event_completed),
        "event_average_mfe_bps": (
            fmean(row.mfe_bps for row in event_completed) if event_completed else 0.0
        ),
        "event_average_mae_bps": (
            fmean(row.mae_bps for row in event_completed) if event_completed else 0.0
        ),
        "event_net_expectancy_bps": fmean(event_net) if event_net else 0.0,
        "event_profit_factor": (
            event_gains / event_losses
            if event_losses
            else (None if event_gains else 0.0)
        ),
        "event_win_rate": (
            fmean(row.net_bps > 0 for row in event_completed) if event_completed else 0.0
        ),
        "research_only": True,
        "can_trade": False,
    }
