"""Causal event-episode quality qualification for Delta research.

The engine sits between raw event detection and directional/exit research.  It
collapses correlated detector publications, reconstructs point-in-time market
truth from the verified event store, scores abnormal flow with a frozen
multi-component contract, and compares selected episodes with nearby controls
matched by symbol, UTC session, and pre-event volatility.

It never creates a SignalCandidate.  Direction and exit testing remain locked
unless the aggregate matched-control gate passes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from vnedge.research.event_direction_response import (
    AbsorptionEvent,
    ResponseSnapshot,
    _capture_snapshots,
)
from vnedge.research.event_response_atlas import PriceTape, _load_tapes
from vnedge.research.strategy_evidence_registry import route_cost_contract

DEFAULT_DELAYS_MS = (250, 1_000, 3_000)
DEFAULT_OUTPUT = Path("research/live_research/event_episode_quality_latest.json")
DEFAULT_TICK_SIZES = {"BTCUSD": 0.5, "ETHUSD": 0.05}


@dataclass(frozen=True)
class EventEpisodeQualityConfig:
    episode_separation_ms: int = 30_000
    quality_window_ms: int = 3_000
    maximum_snapshot_lag_ms: int = 250
    control_horizon_ms: int = 900_000
    control_entry_delay_ms: int = 250
    control_exclusion_ms: int = 60_000
    control_offsets_ms: tuple[int, ...] = (
        1_800_000,
        -1_800_000,
        3_600_000,
        -3_600_000,
        86_400_000,
        -86_400_000,
        172_800_000,
        -172_800_000,
        259_200_000,
        -259_200_000,
    )
    pre_event_volatility_ms: int = 300_000
    maximum_control_volatility_ratio: float = 1.75
    abnormal_score_threshold: float = 0.65
    minimum_control_pairs: int = 30
    minimum_control_uplift_bps: float = 0.0
    minimum_control_win_rate: float = 0.50
    minimum_response_bps: float = 3.0
    basis_full_scale_bps: float = 5.0
    oi_delta_full_scale_fraction: float = 0.0005
    cost_contract_id: str = "taker_full_14_8"

    def __post_init__(self) -> None:
        if self.episode_separation_ms < self.quality_window_ms:
            raise ValueError("episode separation must cover the quality window")
        if self.quality_window_ms <= 0 or self.maximum_snapshot_lag_ms < 0:
            raise ValueError("quality window/snapshot lag contract is invalid")
        if not self.control_offsets_ms or any(value == 0 for value in self.control_offsets_ms):
            raise ValueError("non-zero matched-control offsets are required")
        if self.minimum_control_pairs <= 0:
            raise ValueError("minimum control pairs must be positive")
        if not 0 <= self.abnormal_score_threshold <= 1:
            raise ValueError("abnormal score threshold must be in [0, 1]")
        if not 0 <= self.minimum_control_win_rate <= 1:
            raise ValueError("control win-rate threshold must be in [0, 1]")
        if self.maximum_control_volatility_ratio < 1:
            raise ValueError("control volatility ratio must be >= 1")


@dataclass(frozen=True)
class QualityDetection:
    key: str
    symbol: str
    decision_ts_us: int
    price: float
    reversal_direction: int
    aggressive_notional_usd: float
    dynamic_minimum_notional_usd: float
    price_range_ticks: float
    absorption_ratio: float
    resting_hold_ratio: float
    replenishment_ratio: float
    refresh_count: int
    strength: float
    volume_percentile: float
    stacked: bool

    def __post_init__(self) -> None:
        if not self.key or not self.symbol or self.decision_ts_us < 0:
            raise ValueError("quality detection identity is incomplete")
        if self.price <= 0 or self.reversal_direction not in {-1, 1}:
            raise ValueError("quality detection price/direction is invalid")
        if self.aggressive_notional_usd < 0 or self.dynamic_minimum_notional_usd <= 0:
            raise ValueError("quality detection notional contract is invalid")
        object.__setattr__(self, "symbol", self.symbol.upper())


@dataclass(frozen=True)
class QualityEpisode:
    key: str
    symbol: str
    decision_ts_us: int
    quality_available_ts_us: int
    anchor: QualityDetection
    raw_detection_count: int
    causal_detection_count: int
    direction_conflict: bool


def collapse_quality_episodes(
    detections: Iterable[QualityDetection],
    *,
    separation_ms: int,
    quality_window_ms: int,
) -> tuple[tuple[QualityEpisode, ...], dict[str, Any]]:
    """Collapse correlated detections while keeping the first causal anchor.

    Only detections observed inside the frozen quality window may influence
    episode diagnostics. Later duplicates are counted but never used to move
    the episode timestamp or retroactively improve its score.
    """

    ordered = sorted(detections, key=lambda row: (row.symbol, row.decision_ts_us, row.key))
    grouped: list[list[QualityDetection]] = []
    latest_by_symbol: dict[str, int] = {}
    group_by_symbol: dict[str, list[QualityDetection]] = {}
    separation_us = separation_ms * 1_000
    for row in ordered:
        current = group_by_symbol.get(row.symbol)
        previous = latest_by_symbol.get(row.symbol)
        if current is None or previous is None or row.decision_ts_us - previous > separation_us:
            current = []
            grouped.append(current)
            group_by_symbol[row.symbol] = current
        current.append(row)
        latest_by_symbol[row.symbol] = row.decision_ts_us

    episodes: list[QualityEpisode] = []
    for rows in grouped:
        first = rows[0]
        cutoff = first.decision_ts_us + quality_window_ms * 1_000
        causal = [row for row in rows if row.decision_ts_us <= cutoff]
        episodes.append(
            QualityEpisode(
                key=first.key,
                symbol=first.symbol,
                decision_ts_us=first.decision_ts_us,
                quality_available_ts_us=cutoff,
                anchor=first,
                raw_detection_count=len(rows),
                causal_detection_count=len(causal),
                direction_conflict=len({row.reversal_direction for row in causal}) > 1,
            )
        )
    episodes.sort(key=lambda row: (row.decision_ts_us, row.key))
    return tuple(episodes), {
        "raw_detections": len(ordered),
        "independent_episodes": len(episodes),
        "collapsed_detections": len(ordered) - len(episodes),
        "episode_separation_ms": separation_ms,
        "quality_window_ms": quality_window_ms,
        "policy": "first causal anchor; score freezes after quality window",
    }


def build_event_episode_quality(
    journal_path: Path | str,
    *,
    event_root: Path | str = "data/delta_events",
    output_path: Path | str = DEFAULT_OUTPUT,
    config: EventEpisodeQualityConfig | None = None,
    tick_sizes: Mapping[str, float] = DEFAULT_TICK_SIZES,
    detections: tuple[QualityDetection, ...] | None = None,
    snapshots: Mapping[tuple[str, int], ResponseSnapshot] | None = None,
    tapes: Mapping[str, PriceTape] | None = None,
    code_version: str = "local-research",
) -> dict[str, Any]:
    """Build one deterministic, research-only quality report."""

    contract = config or EventEpisodeQualityConfig()
    raw, duplicates = (
        (detections, 0) if detections is not None else _load_detections(Path(journal_path))
    )
    episodes, collapse = collapse_quality_episodes(
        raw,
        separation_ms=contract.episode_separation_ms,
        quality_window_ms=contract.quality_window_ms,
    )
    if not episodes:
        raise ValueError("no event detections are available for quality scoring")
    ticks = {str(symbol).upper(): float(value) for symbol, value in tick_sizes.items()}
    if any(row.symbol not in ticks or ticks[row.symbol] <= 0 for row in episodes):
        raise ValueError("positive tick sizes are required for every episode symbol")

    absorption_events = tuple(
        AbsorptionEvent(
            key=row.key,
            symbol=row.symbol,
            decision_ts_us=row.decision_ts_us,
            level_price=row.anchor.price,
            reversal_direction=row.anchor.reversal_direction,
            strength=row.anchor.strength,
            stacked=row.anchor.stacked,
        )
        for row in episodes
    )
    delays = tuple(sorted({*DEFAULT_DELAYS_MS, contract.quality_window_ms}))
    if snapshots is None:
        captured, missed_snapshots, replay_events = _capture_snapshots(
            Path(event_root),
            absorption_events,
            delays,
            maximum_capture_lag_ms=contract.maximum_snapshot_lag_ms,
            code_version=code_version,
        )
    else:
        captured = dict(snapshots)
        missed_snapshots = len(episodes) * len(delays) - len(captured)
        replay_events = 0
    loaded_tapes = dict(
        tapes
        or _load_tapes(
            Path(event_root),
            list(absorption_events),
            contract.control_horizon_ms,
            contract.quality_window_ms,
            code_version,
        )
    )
    cost = route_cost_contract(contract.cost_contract_id)
    cost_bps = float(cost.round_trip_cost_bps)

    episode_rows: list[dict[str, Any]] = []
    selected: list[QualityEpisode] = []
    stage_rejections: Counter[str] = Counter()
    for episode in episodes:
        row = _score_episode(
            episode,
            captured,
            loaded_tapes.get(episode.symbol),
            tick_size=ticks[episode.symbol],
            config=contract,
            cost_bps=cost_bps,
        )
        episode_rows.append(row)
        for reason in row["rejection_reasons"]:
            stage_rejections[str(reason)] += 1
        if row["market_truth_complete"] and row["abnormal_score_passed"]:
            selected.append(episode)

    controls = _matched_context_controls(
        list(episodes),
        loaded_tapes,
        selected_keys={row.key for row in selected},
        all_episode_times=episodes,
        config=contract,
    )
    by_key = {str(row["event_key"]): row for row in controls["pairs"]}
    market_truth_complete = sum(bool(row["market_truth_complete"]) for row in episode_rows)
    abnormal_selected = sum(bool(row["abnormal_score_passed"]) for row in episode_rows)
    control_outperformed = 0
    for row in episode_rows:
        pair = by_key.get(str(row["episode_key"]))
        row["matched_control"] = pair
        row["control_outperformed"] = bool(
            row["abnormal_score_passed"] and pair and float(pair["uplift_bps"]) > 0
        )
        control_outperformed += int(row["control_outperformed"])
        if row["abnormal_score_passed"] and pair is None:
            row["rejection_reasons"].append("CONTROL_UNAVAILABLE")
            stage_rejections["CONTROL_UNAVAILABLE"] += 1
        elif row["abnormal_score_passed"] and not row["control_outperformed"]:
            row["rejection_reasons"].append("CONTROL_NOT_OUTPERFORMED")
            stage_rejections["CONTROL_NOT_OUTPERFORMED"] += 1

    control_gate_passed = controls["passed"] is True
    directional = sum(
        bool(row["directional_confirmation"])
        for row in episode_rows
        if control_gate_passed and row["control_outperformed"]
    )
    fee_wall = sum(
        bool(row["cleared_fee_wall"])
        for row in episode_rows
        if control_gate_passed and row["control_outperformed"] and row["directional_confirmation"]
    )
    if not control_gate_passed:
        stage_rejections["AGGREGATE_CONTROL_GATE_FAILED"] = abnormal_selected

    funnel = [
        _stage("raw_events", "Raw events", len(raw), len(raw), "OBSERVED"),
        _stage(
            "independent_episodes",
            "Independent episodes",
            len(episodes),
            len(raw),
            "COLLAPSED",
        ),
        _stage(
            "market_truth_complete",
            "Market-truth complete",
            market_truth_complete,
            len(episodes),
            "PASSED" if market_truth_complete else "BLOCKED",
        ),
        _stage(
            "abnormal_vs_control",
            "Abnormal vs control",
            control_outperformed,
            abnormal_selected,
            "PASSED" if control_gate_passed else "BLOCKED",
        ),
        _stage(
            "directional_confirmation",
            "Directional confirmation",
            directional,
            control_outperformed,
            "RESEARCH" if control_gate_passed else "LOCKED_BY_CONTROL",
        ),
        _stage(
            "cleared_fee_wall",
            "Cleared fee wall",
            fee_wall,
            directional,
            "RESEARCH" if control_gate_passed else "LOCKED_BY_CONTROL",
        ),
        _stage(
            "simulated_outcome",
            "Simulated outcome",
            0,
            fee_wall,
            "LOCKED_PENDING_PREREGISTERED_EXIT",
        ),
    ]
    report: dict[str, Any] = {
        "schema_version": "vnedge.event_episode_quality.v1",
        "engine_id": "delta_event_episode_quality_v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "source": {
            "journal_path": str(journal_path),
            "event_root": str(event_root),
            "duplicates_ignored": duplicates,
            "verified_replay_events": replay_events,
            "missed_quality_snapshots": missed_snapshots,
            "symbols": sorted({row.symbol for row in episodes}),
        },
        "contract": {
            **asdict(contract),
            "component_weights": {
                "aggressive_volume_surprise": 0.25,
                "book_depletion_replenishment": 0.20,
                "price_response_efficiency": 0.20,
                "oi_basis_dislocation": 0.15,
                "persistence_reclaim_failure": 0.20,
            },
            "availability_clock": "local_receive",
            "route_cost_contract": cost.to_dict(),
            "code_version": code_version,
        },
        "episode_collapse": collapse,
        "funnel": funnel,
        "rejection_diagnostics": {
            "by_reason": dict(sorted(stage_rejections.items())),
            "episodes_with_rejection": sum(bool(row["rejection_reasons"]) for row in episode_rows),
        },
        "control_qualification": controls,
        "episodes": episode_rows,
        "diagnosis": {
            "verdict": (
                "QUALITY_GATE_PASSED_DIRECTION_RESEARCH_ALLOWED"
                if control_gate_passed
                else "NO_ABNORMAL_CONTROL_OUTPERFORMANCE"
            ),
            "exit_testing_authorized": control_gate_passed,
            "next_step": (
                "Freeze one direction rule; do not tune the quality score on its outcomes."
                if control_gate_passed
                else "Collect and improve abnormal-event selection; exit search remains blocked."
            ),
        },
        "parallel_swing_collection": {
            "scanner_id": "mtf_amf_directional_rejection_v3",
            "policy": "separate evidence stream; never mixed with event episodes",
            "required_observations": 60,
        },
        "development_window_only": True,
        "sealed_holdout_opened": False,
        "scanner_implementation_authorized": False,
        "paper_authorized": False,
        "can_trade": False,
        "can_promote": False,
        "order_route": "absent",
    }
    report["deterministic_result_hash"] = _result_hash(report)
    _atomic_json(Path(output_path), report)
    return report


def _score_episode(
    episode: QualityEpisode,
    snapshots: Mapping[tuple[str, int], ResponseSnapshot],
    tape: PriceTape | None,
    *,
    tick_size: float,
    config: EventEpisodeQualityConfig,
    cost_bps: float,
) -> dict[str, Any]:
    delays = tuple(sorted({*DEFAULT_DELAYS_MS, config.quality_window_ms}))
    states = [snapshots.get((episode.key, delay)) for delay in delays]
    missing = [delay for delay, state in zip(delays, states) if state is None]
    present = [state for state in states if state is not None]
    last = present[-1] if present else None
    blockers: list[str] = []
    if missing:
        blockers.append("QUALITY_SNAPSHOT_MISSING")
    if episode.direction_conflict:
        blockers.append("DIRECTION_CONFLICT_WITHIN_QUALITY_WINDOW")
    if not present or any(not state.trade_book_join_ok for state in present):
        blockers.append("TRADE_BOOK_JOIN_INCOMPLETE")
    if last is None or last.basis_bps is None:
        blockers.append("BASIS_UNAVAILABLE")
    if last is None or last.open_interest is None or last.open_interest_delta is None:
        blockers.append("OPEN_INTEREST_DISLOCATION_UNAVAILABLE")
    market_truth_complete = not blockers

    detection = episode.anchor
    ratio = detection.aggressive_notional_usd / detection.dynamic_minimum_notional_usd
    aggressive = min(1.0, math.log1p(max(0.0, ratio)) / math.log(4.0))
    book_alignment = (
        sum(abs(state.book_imbalance) for state in present) / len(present) if present else 0.0
    )
    book = min(
        1.0,
        0.45 * max(detection.resting_hold_ratio, detection.replenishment_ratio)
        + 0.20 * min(1.0, detection.refresh_count / 3.0)
        + 0.20 * detection.absorption_ratio
        + 0.15 * book_alignment,
    )
    movement_bps, directional_mfe_bps, direction_state = _response_features(
        episode,
        states,
        tape,
        tick_size=tick_size,
        quality_window_ms=config.quality_window_ms,
    )
    price_efficiency = min(1.0, movement_bps / config.minimum_response_bps) * (
        1.0 / (1.0 + max(0.0, detection.price_range_ticks))
    )
    if last is not None and last.open_interest not in (None, 0):
        oi_fraction = abs(float(last.open_interest_delta or 0.0)) / float(last.open_interest)
    else:
        oi_fraction = 0.0
    basis_score = (
        min(1.0, abs(float(last.basis_bps or 0.0)) / config.basis_full_scale_bps) if last else 0.0
    )
    oi_score = min(1.0, oi_fraction / config.oi_delta_full_scale_fraction)
    dislocation = 0.5 * basis_score + 0.5 * oi_score
    persistence = _persistence_score(present, direction_state)
    components = {
        "aggressive_volume_surprise": aggressive,
        "book_depletion_replenishment": book,
        "price_response_efficiency": price_efficiency,
        "oi_basis_dislocation": dislocation,
        "persistence_reclaim_failure": persistence,
    }
    score = (
        0.25 * aggressive
        + 0.20 * book
        + 0.20 * price_efficiency
        + 0.15 * dislocation
        + 0.20 * persistence
    )
    score_passed = market_truth_complete and score >= config.abnormal_score_threshold
    if market_truth_complete and not score_passed:
        blockers.append("ABNORMAL_SCORE_BELOW_THRESHOLD")
    return {
        "episode_key": episode.key,
        "symbol": episode.symbol,
        "decision_ts": _iso(episode.decision_ts_us),
        "quality_available_ts": _iso(episode.quality_available_ts_us),
        # Explicit location identity keeps downstream clean-room studies from
        # parsing prices or directions out of the human-readable event key.
        # Both values were present at the original detection timestamp.
        "anchor_price": episode.anchor.price,
        "anchor_reversal_direction": episode.anchor.reversal_direction,
        "raw_detection_count": episode.raw_detection_count,
        "causal_detection_count": episode.causal_detection_count,
        "market_truth_complete": market_truth_complete,
        "missing_snapshot_delays_ms": missing,
        "quality_components": components,
        "abnormal_score": score,
        "abnormal_score_passed": score_passed,
        "response_movement_bps": movement_bps,
        "directional_confirmation": direction_state,
        "directional_mfe_bps": directional_mfe_bps,
        "cleared_fee_wall": bool(direction_state and directional_mfe_bps >= cost_bps),
        "rejection_reasons": blockers,
    }


def _response_features(
    episode: QualityEpisode,
    states: list[ResponseSnapshot | None],
    tape: PriceTape | None,
    *,
    tick_size: float,
    quality_window_ms: int,
) -> tuple[float, float, str | None]:
    present = [state for state in states if state is not None]
    if not present:
        return 0.0, 0.0, None
    last = present[-1]
    signed_ticks = (last.last_trade_price - episode.anchor.price) / tick_size
    price_direction = 1 if signed_ticks >= 2 else -1 if signed_ticks <= -2 else 0
    flow_votes = sum(
        1 if state.flow_imbalance > 0.15 else -1 if state.flow_imbalance < -0.15 else 0
        for state in present
    )
    flow_direction = 1 if flow_votes > 0 else -1 if flow_votes < 0 else 0
    direction_state: str | None = None
    if price_direction and price_direction == flow_direction:
        direction_state = (
            "reversal_reclaim"
            if price_direction == episode.anchor.reversal_direction
            else "continuation_failure"
        )
    movement = abs(last.last_trade_price / episode.anchor.price - 1.0) * 10_000.0
    mfe = 0.0
    if tape is not None and direction_state is not None:
        start = tape.first_at_or_after(episode.quality_available_ts_us)
        end = tape.first_at_or_after(
            episode.quality_available_ts_us + max(quality_window_ms, 900_000) * 1_000
        )
        if start is not None and end is not None and end >= start:
            direction = (
                episode.anchor.reversal_direction
                if direction_state == "reversal_reclaim"
                else -episode.anchor.reversal_direction
            )
            mfe, _, _ = tape.directional_path_stats(
                start, end + 1, direction=direction, entry_price=tape.prices[start]
            )
    return movement, mfe, direction_state


def _persistence_score(states: list[ResponseSnapshot], direction_state: str | None) -> float:
    if not states or direction_state is None:
        return 0.0
    direction = 1 if states[-1].last_trade_price >= states[0].last_trade_price else -1
    aligned = sum(
        direction * state.flow_imbalance >= 0.15 and direction * state.book_imbalance >= 0.10
        for state in states
    )
    return aligned / len(states)


def _matched_context_controls(
    episodes: list[QualityEpisode],
    tapes: Mapping[str, PriceTape],
    *,
    selected_keys: set[str],
    all_episode_times: Iterable[QualityEpisode],
    config: EventEpisodeQualityConfig,
) -> dict[str, Any]:
    event_times: dict[str, tuple[int, ...]] = defaultdict(tuple)
    grouped: dict[str, list[int]] = defaultdict(list)
    for row in all_episode_times:
        grouped[row.symbol].append(row.quality_available_ts_us)
    event_times = {symbol: tuple(sorted(values)) for symbol, values in grouped.items()}
    used: set[tuple[str, int]] = set()
    pairs: list[dict[str, Any]] = []
    unavailable = 0
    selected_unavailable = 0
    for episode in episodes:
        tape = tapes.get(episode.symbol)
        if tape is None:
            unavailable += 1
            selected_unavailable += int(episode.key in selected_keys)
            continue
        event_ts = episode.quality_available_ts_us
        event_movement = _best_movement(
            tape,
            event_ts,
            delay_ms=config.control_entry_delay_ms,
            horizon_ms=config.control_horizon_ms,
        )
        event_vol = _pre_event_volatility(tape, event_ts, config.pre_event_volatility_ms)
        if event_movement is None or event_vol is None:
            unavailable += 1
            selected_unavailable += int(episode.key in selected_keys)
            continue
        matched: tuple[int, float, float] | None = None
        for offset in config.control_offsets_ms:
            candidate = event_ts + offset * 1_000
            if candidate < 0 or (episode.symbol, candidate) in used:
                continue
            if _session(candidate) != _session(event_ts):
                continue
            if any(
                abs(candidate - other) <= config.control_exclusion_ms * 1_000
                for other in event_times[episode.symbol]
            ):
                continue
            control_vol = _pre_event_volatility(tape, candidate, config.pre_event_volatility_ms)
            control_movement = _best_movement(
                tape,
                candidate,
                delay_ms=config.control_entry_delay_ms,
                horizon_ms=config.control_horizon_ms,
            )
            if control_vol is None or control_movement is None:
                continue
            low = min(event_vol, control_vol)
            high = max(event_vol, control_vol)
            if low == 0 and high > 0:
                continue
            if low > 0 and high / low > config.maximum_control_volatility_ratio:
                continue
            matched = (candidate, control_movement, control_vol)
            break
        if matched is None:
            unavailable += 1
            selected_unavailable += int(episode.key in selected_keys)
            continue
        control_ts, control_movement, control_vol = matched
        used.add((episode.symbol, control_ts))
        pairs.append(
            {
                "event_key": episode.key,
                "symbol": episode.symbol,
                "session": _session(event_ts),
                "volatility_bucket": _volatility_bucket(event_vol),
                "event_ts_us": event_ts,
                "control_ts_us": control_ts,
                "event_pre_volatility_bps": event_vol,
                "control_pre_volatility_bps": control_vol,
                "event_best_mfe_bps": event_movement,
                "control_best_mfe_bps": control_movement,
                "uplift_bps": event_movement - control_movement,
            }
        )
    selected_pairs = [row for row in pairs if str(row["event_key"]) in selected_keys]
    count = len(selected_pairs)
    average_event = (
        sum(row["event_best_mfe_bps"] for row in selected_pairs) / count if count else 0.0
    )
    average_control = (
        sum(row["control_best_mfe_bps"] for row in selected_pairs) / count if count else 0.0
    )
    uplift = average_event - average_control
    win_rate = sum(row["uplift_bps"] > 0 for row in selected_pairs) / count if count else 0.0
    checks = {
        "minimum_pairs": count >= config.minimum_control_pairs,
        "positive_mean_uplift": uplift > config.minimum_control_uplift_bps,
        "pair_win_rate": win_rate > config.minimum_control_win_rate,
    }
    return {
        "method": "symbol_session_prevol_matched_direction_neutral_mfe",
        "episodes_considered": len(episodes),
        "all_matched_pairs": len(pairs),
        "all_unavailable_pairs": unavailable,
        "selected_episodes": len(selected_keys),
        "matched_pairs": count,
        "unavailable_pairs": selected_unavailable,
        "average_event_best_mfe_bps": average_event,
        "average_control_best_mfe_bps": average_control,
        "average_uplift_bps": uplift,
        "event_outperformance_rate": win_rate,
        "checks": checks,
        "passed": all(checks.values()),
        "exit_testing_authorized": all(checks.values()),
        "pairs": pairs,
    }


def _best_movement(tape: PriceTape, ts_us: int, *, delay_ms: int, horizon_ms: int) -> float | None:
    start = tape.first_at_or_after(ts_us + delay_ms * 1_000)
    if start is None:
        return None
    end = tape.first_at_or_after(tape.timestamps_us[start] + horizon_ms * 1_000)
    if end is None:
        return None
    entry = tape.prices[start]
    long_mfe, _, _ = tape.directional_path_stats(start, end + 1, direction=1, entry_price=entry)
    short_mfe, _, _ = tape.directional_path_stats(start, end + 1, direction=-1, entry_price=entry)
    return max(long_mfe, short_mfe)


def _pre_event_volatility(tape: PriceTape, ts_us: int, window_ms: int) -> float | None:
    start = tape.first_at_or_after(ts_us - window_ms * 1_000)
    end = tape.first_at_or_after(ts_us)
    if start is None or end is None or end <= start:
        return None
    prices = tape.prices[start : end + 1]
    return (max(prices) / min(prices) - 1.0) * 10_000.0


def _session(ts_us: int) -> str:
    hour = datetime.fromtimestamp(ts_us / 1_000_000, tz=UTC).hour
    if hour < 8:
        return "asia"
    if hour < 13:
        return "london"
    if hour < 21:
        return "new_york"
    return "late_utc"


def _volatility_bucket(value: float) -> str:
    if value < 5:
        return "low"
    if value < 15:
        return "medium"
    return "high"


def _stage(stage_id: str, label: str, value: int, target: int, state: str) -> dict[str, Any]:
    return {"id": stage_id, "label": label, "value": value, "target": target, "state": state}


def _load_detections(path: Path) -> tuple[tuple[QualityDetection, ...], int]:
    if not path.is_file():
        raise FileNotFoundError(f"event research journal not found: {path}")
    found: dict[str, QualityDetection] = {}
    duplicates = 0
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            envelope = json.loads(line)
            if envelope.get("kind") != "delta_absorption_research_observation":
                continue
            raw = envelope.get("payload")
            event = raw.get("event") if isinstance(raw, dict) else None
            if not isinstance(raw, dict) or not isinstance(event, dict):
                raise TypeError(f"malformed absorption observation at line {line_number}")
            decision = _parse_iso_us(raw.get("decision_ts"))
            row = QualityDetection(
                key=str(raw.get("key") or ""),
                symbol=str(raw.get("symbol") or event.get("symbol") or ""),
                decision_ts_us=decision if decision is not None else -1,
                price=float(event.get("price") or 0.0),
                reversal_direction=int(event.get("reversal_direction") or 0),
                aggressive_notional_usd=float(event.get("aggressive_notional_usd") or 0.0),
                dynamic_minimum_notional_usd=float(
                    event.get("dynamic_minimum_notional_usd") or 0.0
                ),
                price_range_ticks=float(event.get("price_range_ticks") or 0.0),
                absorption_ratio=float(event.get("absorption_ratio") or 0.0),
                resting_hold_ratio=float(event.get("resting_hold_ratio") or 0.0),
                replenishment_ratio=float(event.get("replenishment_ratio") or 0.0),
                refresh_count=int(event.get("refresh_count") or 0),
                strength=float(event.get("strength") or 0.0),
                volume_percentile=float(raw.get("volume_percentile") or 0.0),
                stacked=bool(event.get("is_stacked") or raw.get("was_stacked")),
            )
            previous = found.get(row.key)
            if previous is not None:
                if previous != row:
                    raise ValueError(f"conflicting duplicate detection: {row.key}")
                duplicates += 1
            else:
                found[row.key] = row
    rows = tuple(sorted(found.values(), key=lambda row: (row.decision_ts_us, row.key)))
    return rows, duplicates


def _parse_iso_us(value: object) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1_000_000)


def _iso(value: int) -> str:
    return datetime.fromtimestamp(value / 1_000_000, tz=UTC).isoformat()


def _result_hash(payload: Mapping[str, Any]) -> str:
    stable = {
        key: payload[key]
        for key in (
            "engine_id",
            "source",
            "contract",
            "episode_collapse",
            "funnel",
            "rejection_diagnostics",
            "control_qualification",
            "episodes",
            "diagnosis",
        )
    }
    encoded = json.dumps(stable, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", type=Path, default=Path("logs/delta_event_research.jsonl"))
    parser.add_argument("--event-root", type=Path, default=Path("data/delta_events"))
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--code-version", default="local-research")
    args = parser.parse_args(argv)
    result = build_event_episode_quality(
        args.journal,
        event_root=args.event_root,
        output_path=args.output,
        code_version=args.code_version,
    )
    print(
        json.dumps(
            {
                "engine_id": result["engine_id"],
                "funnel": result["funnel"],
                "diagnosis": result["diagnosis"],
                "deterministic_result_hash": result["deterministic_result_hash"],
                "can_trade": False,
                "can_promote": False,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
