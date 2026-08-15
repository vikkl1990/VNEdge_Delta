"""Causal post-absorption direction study on verified Delta event tape.

Absorption is treated as an opportunity, not a direction.  After a frozen
delay the shared live/replay event engine supplies the first available book
and trade-flow snapshot.  Small mechanical rule families then classify the
response as either a level reclaim (reversal), a level failure
(continuation), or silence.  Entries and exits remain real public prints and
all reported returns include the canonical Delta taker cost model.

This module is development research only.  It never constructs a
``SignalCandidate`` and cannot grant scanner, paper, promotion, or live
authority.
"""

from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import os
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile

from vnedge.replay.models import ReplayConfig
from vnedge.replay.store import DeltaShardEventStore
from vnedge.research.event_episodes import (
    collapse_independent_episodes,
    episode_report_dict,
    matched_movement_control_gate,
)
from vnedge.research.event_response_atlas import PriceTape, _exit_diagnosis, _load_tapes
from vnedge.research.strategy_evidence_registry import route_cost_contract
from vnedge.scalping.delta_engine.absorption import AbsorptionDetectorConfig
from vnedge.scalping.delta_engine.event_trigger import (
    DeltaVerifiedEventBridge,
    EventDrivenTriggerLayer,
    EventMarketSnapshot,
    EventTriggerConfig,
)
from vnedge.scalping.delta_engine.fee_model import DeltaFeeModel
from vnedge.scalping.delta_engine.signal_generator import SignalGateConfig

DEFAULT_RESPONSE_DELAYS_MS = (250, 500, 1_000, 2_000, 3_000)
DEFAULT_OUTCOME_HORIZONS_MS = (300_000, 600_000, 900_000, 1_800_000)
DEFAULT_TICK_SIZES = {"BTCUSD": 0.5, "ETHUSD": 0.05}


@dataclass(frozen=True)
class AbsorptionEvent:
    key: str
    symbol: str
    decision_ts_us: int
    level_price: float
    reversal_direction: int
    strength: float
    stacked: bool

    def __post_init__(self) -> None:
        if not self.key or not self.symbol or self.decision_ts_us < 0:
            raise ValueError("absorption event identity is incomplete")
        if self.level_price <= 0 or self.reversal_direction not in {-1, 1}:
            raise ValueError("absorption event price/direction is invalid")
        object.__setattr__(self, "symbol", self.symbol.upper())


@dataclass(frozen=True)
class ResponseSnapshot:
    event_key: str
    symbol: str
    response_delay_ms: int
    requested_ts_us: int
    captured_ts_us: int
    last_trade_price: float
    mid: float
    spread_bps: float
    book_imbalance: float
    flow_imbalance: float
    aggressive_buy_usd: float = 0.0
    aggressive_sell_usd: float = 0.0
    open_interest: float | None = None
    open_interest_delta: float | None = None
    basis_bps: float | None = None
    market_truth_ready: bool = False
    market_truth_blockers: tuple[str, ...] = ()
    trade_book_join_ok: bool = False

    def __post_init__(self) -> None:
        if self.response_delay_ms < 0 or self.captured_ts_us < self.requested_ts_us:
            raise ValueError("response snapshot violates the causal clock")
        if self.last_trade_price <= 0 or self.mid <= 0 or self.spread_bps < 0:
            raise ValueError("response snapshot prices/spread are invalid")
        if not -1 <= self.book_imbalance <= 1 or not -1 <= self.flow_imbalance <= 1:
            raise ValueError("response snapshot imbalances must be in [-1, 1]")
        if self.aggressive_buy_usd < 0 or self.aggressive_sell_usd < 0:
            raise ValueError("response snapshot aggressive notional cannot be negative")
        object.__setattr__(self, "market_truth_blockers", tuple(self.market_truth_blockers))
        object.__setattr__(self, "symbol", self.symbol.upper())


@dataclass(frozen=True)
class DirectionRule:
    rule_id: str
    reclaim_ticks: float
    minimum_flow_alignment: float = 0.0
    minimum_book_alignment: float = 0.0

    def __post_init__(self) -> None:
        if not self.rule_id or self.reclaim_ticks <= 0:
            raise ValueError("direction rule identity/reclaim must be valid")
        if not 0 <= self.minimum_flow_alignment <= 1:
            raise ValueError("flow threshold must be in [0, 1]")
        if not 0 <= self.minimum_book_alignment <= 1:
            raise ValueError("book threshold must be in [0, 1]")


DEFAULT_RULES = (
    DirectionRule("price_reclaim_2t", 2.0),
    DirectionRule("price_flow_2t_0.15", 2.0, minimum_flow_alignment=0.15),
    DirectionRule("price_book_2t_0.10", 2.0, minimum_book_alignment=0.10),
    DirectionRule(
        "price_flow_book_2t_0.15_0.10",
        2.0,
        minimum_flow_alignment=0.15,
        minimum_book_alignment=0.10,
    ),
    DirectionRule(
        "price_flow_book_3t_0.30_0.20",
        3.0,
        minimum_flow_alignment=0.30,
        minimum_book_alignment=0.20,
    ),
)


@dataclass
class _Metrics:
    observations: int = 0
    gross_sum: float = 0.0
    net_sum: float = 0.0
    gains: float = 0.0
    losses: float = 0.0
    mfe_sum: float = 0.0
    mae_sum: float = 0.0
    positive: int = 0
    clear_cost: int = 0
    mfe_after_cost_sum: float = 0.0
    time_to_mfe_ms_sum: float = 0.0
    capture_ratio_sum: float = 0.0
    capture_ratio_samples: int = 0
    exit_diagnoses: Counter[str] = field(default_factory=Counter)

    def add(
        self,
        *,
        gross: float,
        net: float,
        mfe: float,
        mae: float,
        cost: float,
        time_to_mfe_ms: float,
    ) -> None:
        self.observations += 1
        self.gross_sum += gross
        self.net_sum += net
        self.mfe_sum += mfe
        self.mae_sum += mae
        if net > 0:
            self.gains += net
            self.positive += 1
        elif net < 0:
            self.losses += abs(net)
        self.clear_cost += mfe >= cost
        self.mfe_after_cost_sum += mfe - cost
        self.time_to_mfe_ms_sum += time_to_mfe_ms
        if mfe > 0:
            self.capture_ratio_sum += gross / mfe
            self.capture_ratio_samples += 1
        self.exit_diagnoses[_exit_diagnosis(net=net, mfe_after_cost=mfe - cost)] += 1

    def to_dict(self) -> dict[str, object]:
        count = self.observations
        return {
            "observations": count,
            "average_gross_bps": self.gross_sum / count if count else 0.0,
            "average_net_bps": self.net_sum / count if count else 0.0,
            "total_net_bps": self.net_sum,
            "profit_factor_net": self.gains / self.losses if self.losses else None,
            "net_positive_rate": self.positive / count if count else 0.0,
            "average_mfe_bps": self.mfe_sum / count if count else 0.0,
            "average_mae_bps": self.mae_sum / count if count else 0.0,
            "average_mfe_after_cost_bps": (self.mfe_after_cost_sum / count if count else 0.0),
            "average_time_to_mfe_ms": (self.time_to_mfe_ms_sum / count if count else 0.0),
            "average_capture_ratio": (
                self.capture_ratio_sum / self.capture_ratio_samples
                if self.capture_ratio_samples
                else None
            ),
            "fee_wall_break_rate_pct": self.clear_cost / count * 100.0 if count else 0.0,
            "exit_diagnosis_counts": dict(self.exit_diagnoses),
        }


class _ContextCollector:
    scanner_id = "post_absorption_direction_context_collector"
    observes_unconfirmed_events = True

    def __init__(
        self,
        events: tuple[AbsorptionEvent, ...],
        delays_ms: tuple[int, ...],
        *,
        maximum_capture_lag_ms: int,
    ) -> None:
        self._events = {event.key: event for event in events}
        self._pending: dict[str, list[tuple[int, str, int]]] = defaultdict(list)
        for event in events:
            for delay_ms in delays_ms:
                heapq.heappush(
                    self._pending[event.symbol],
                    (event.decision_ts_us + delay_ms * 1_000, event.key, delay_ms),
                )
        self.maximum_capture_lag_us = maximum_capture_lag_ms * 1_000
        self.snapshots: dict[tuple[str, int], ResponseSnapshot] = {}
        self.missed = 0

    def evaluate(self, context: EventMarketSnapshot) -> None:
        current_us = int(context.decision_ts.timestamp() * 1_000_000)
        pending = self._pending[context.symbol]
        while pending and pending[0][0] <= current_us:
            requested_us, event_key, delay_ms = heapq.heappop(pending)
            if current_us - requested_us > self.maximum_capture_lag_us:
                self.missed += 1
                continue
            last_trade = float(context.features.get("last_trade_price") or 0.0)
            if last_trade <= 0 or not context.book_healthy:
                self.missed += 1
                continue
            self.snapshots[(event_key, delay_ms)] = ResponseSnapshot(
                event_key=event_key,
                symbol=context.symbol,
                response_delay_ms=delay_ms,
                requested_ts_us=requested_us,
                captured_ts_us=current_us,
                last_trade_price=last_trade,
                mid=context.mid,
                spread_bps=context.spread_bps,
                book_imbalance=context.book_imbalance,
                flow_imbalance=context.flow_imbalance,
                aggressive_buy_usd=context.aggressive_buy_usd,
                aggressive_sell_usd=context.aggressive_sell_usd,
                open_interest=context.open_interest,
                open_interest_delta=context.open_interest_delta,
                basis_bps=context.basis_bps,
                market_truth_ready=context.market_truth_ready,
                market_truth_blockers=context.market_truth_blockers,
                trade_book_join_ok=bool(context.features.get("trade_book_join_ok")),
            )

    def finalize(self) -> None:
        self.missed += sum(len(rows) for rows in self._pending.values())
        self._pending.clear()


def classify_direction(
    event: AbsorptionEvent,
    snapshot: ResponseSnapshot,
    rule: DirectionRule,
    *,
    tick_size: float,
) -> tuple[str, int] | None:
    """Return (state, direction) from information available at the snapshot."""

    if tick_size <= 0 or event.key != snapshot.event_key or event.symbol != snapshot.symbol:
        raise ValueError("event/snapshot/tick contract mismatch")
    signed_ticks = (
        event.reversal_direction * (snapshot.last_trade_price - event.level_price) / tick_size
    )
    if signed_ticks >= rule.reclaim_ticks:
        state = "reversal_reclaim"
        direction = event.reversal_direction
    elif signed_ticks <= -rule.reclaim_ticks:
        state = "continuation_failure"
        direction = -event.reversal_direction
    else:
        return None
    if direction * snapshot.flow_imbalance < rule.minimum_flow_alignment:
        return None
    if direction * snapshot.book_imbalance < rule.minimum_book_alignment:
        return None
    return state, direction


def build_post_event_direction_study(
    journal_path: Path | str,
    *,
    event_root: Path | str = "data/delta_events",
    output_path: Path | str = (
        "research/live_research/post_absorption_direction_study_latest.json"
    ),
    delays_ms: tuple[int, ...] = DEFAULT_RESPONSE_DELAYS_MS,
    horizons_ms: tuple[int, ...] = DEFAULT_OUTCOME_HORIZONS_MS,
    rules: tuple[DirectionRule, ...] = DEFAULT_RULES,
    tick_sizes: Mapping[str, float] = DEFAULT_TICK_SIZES,
    maximum_capture_lag_ms: int = 250,
    maximum_entry_wait_ms: int = 2_000,
    minimum_selection_observations: int = 100,
    minimum_validation_observations: int = 40,
    episode_separation_ms: int = 30_000,
    require_control_gate: bool = True,
    control_delay_ms: int = 250,
    control_horizon_ms: int = 900_000,
    control_offset_ms: int = 1_800_000,
    control_exclusion_ms: int = 60_000,
    minimum_control_pairs: int = 30,
    minimum_control_uplift_bps: float = 0.0,
    observations: tuple[AbsorptionEvent, ...] | None = None,
    response_snapshots: Mapping[tuple[str, int], ResponseSnapshot] | None = None,
    tapes: Mapping[str, PriceTape] | None = None,
    code_version: str = "local-research",
) -> dict[str, object]:
    if not delays_ms or not horizons_ms or not rules:
        raise ValueError("delays, horizons, and rules are required")
    if any(value <= 0 for value in delays_ms + horizons_ms):
        raise ValueError("delays and horizons must be positive")
    if maximum_capture_lag_ms < 0 or maximum_entry_wait_ms < 0:
        raise ValueError("capture and entry waits cannot be negative")
    delays_ms = tuple(sorted(set(delays_ms)))
    horizons_ms = tuple(sorted(set(horizons_ms)))
    raw_events, duplicates = (
        (observations, 0)
        if observations is not None
        else _load_absorption_events(Path(journal_path))
    )
    events, episode_report = collapse_independent_episodes(
        raw_events,
        separation_ms=episode_separation_ms,
    )
    if not events:
        raise ValueError("no absorption observations are available")
    symbols = tuple(sorted({event.symbol for event in events}))
    ticks = {symbol.upper(): float(value) for symbol, value in tick_sizes.items()}
    if any(symbol not in ticks or ticks[symbol] <= 0 for symbol in symbols):
        raise ValueError("positive tick sizes are required for every observed symbol")

    loaded_tapes = dict(
        tapes
        or _load_tapes(
            Path(event_root),
            list(events),
            horizons_ms[-1],
            delays_ms[-1] + maximum_capture_lag_ms,
            code_version,
        )
    )
    control_qualification = matched_movement_control_gate(
        events,
        loaded_tapes,
        delay_ms=control_delay_ms,
        horizon_ms=control_horizon_ms,
        nearby_offset_ms=control_offset_ms,
        event_exclusion_ms=control_exclusion_ms,
        maximum_entry_wait_ms=maximum_entry_wait_ms,
        minimum_pairs=minimum_control_pairs,
        minimum_uplift_bps=minimum_control_uplift_bps,
    )
    exit_testing_authorized = (
        control_qualification["passed"] is True or require_control_gate is False
    )
    if response_snapshots is None and exit_testing_authorized:
        snapshots, missed_snapshots, replay_events = _capture_snapshots(
            Path(event_root),
            events,
            delays_ms,
            maximum_capture_lag_ms=maximum_capture_lag_ms,
            code_version=code_version,
        )
    elif response_snapshots is not None and exit_testing_authorized:
        snapshots = {
            key: value
            for key, value in response_snapshots.items()
            if any(event.key == key[0] for event in events)
        }
        missed_snapshots = len(events) * len(delays_ms) - len(snapshots)
        replay_events = 0
    else:
        snapshots = {}
        missed_snapshots = len(events) * len(delays_ms)
        replay_events = 0
    cutoff_index = min(len(events) - 1, max(0, int(len(events) * 0.70) - 1))
    selection_cutoff_us = events[cutoff_index].decision_ts_us
    fee_model = DeltaFeeModel(default_slippage_bps_per_leg=1.5)
    route_contract = route_cost_contract("taker_full_14_8")
    groups: dict[tuple[str, str, str, int, int, str], _Metrics] = defaultdict(_Metrics)
    classified: dict[tuple[str, int], int] = defaultdict(int)
    late_entries = 0
    incomplete_horizons = 0

    for event in events if exit_testing_authorized else ():
        tape = loaded_tapes.get(event.symbol)
        if tape is None:
            continue
        split = "selection_70" if event.decision_ts_us <= selection_cutoff_us else "validation_30"
        for delay_ms in delays_ms:
            snapshot = snapshots.get((event.key, delay_ms))
            if snapshot is None:
                continue
            entry_index = tape.first_at_or_after(snapshot.captured_ts_us)
            if entry_index is None or (
                tape.timestamps_us[entry_index] - snapshot.captured_ts_us
                > maximum_entry_wait_ms * 1_000
            ):
                late_entries += 1
                continue
            entry_price = tape.prices[entry_index]
            entry_ts_us = tape.timestamps_us[entry_index]
            for rule in rules:
                decision = classify_direction(
                    event,
                    snapshot,
                    rule,
                    tick_size=ticks[event.symbol],
                )
                if decision is None:
                    continue
                state, direction = decision
                classified[(rule.rule_id, delay_ms)] += 1
                for horizon_ms in horizons_ms:
                    exit_index = tape.first_at_or_after(entry_ts_us + horizon_ms * 1_000)
                    if exit_index is None:
                        incomplete_horizons += 1
                        continue
                    exit_price = tape.prices[exit_index]
                    gross = direction * (exit_price / entry_price - 1.0) * 10_000.0
                    mfe, mae, time_to_mfe_ms = tape.directional_path_stats(
                        entry_index,
                        exit_index + 1,
                        direction=direction,
                        entry_price=entry_price,
                    )
                    cost = fee_model.breakdown(
                        event.symbol,
                        entry_is_maker=False,
                        exit_is_maker=False,
                        hold_seconds=horizon_ms / 1_000.0,
                        scalper_opted_in=False,
                    ).total_bps
                    net = gross - cost
                    for partition in ("all_development", split):
                        groups[
                            (
                                partition,
                                event.symbol,
                                rule.rule_id,
                                delay_ms,
                                horizon_ms,
                                state,
                            )
                        ].add(
                            gross=gross,
                            net=net,
                            mfe=mfe,
                            mae=mae,
                            cost=cost,
                            time_to_mfe_ms=time_to_mfe_ms,
                        )

    rows = _metric_rows(groups)
    comparisons = _selection_validation_comparisons(
        rows,
        minimum_selection_observations=minimum_selection_observations,
        minimum_validation_observations=minimum_validation_observations,
    )
    supported = [
        row
        for row in comparisons
        if float(row["selection"]["average_net_bps"]) > 0
        and float(row["validation"]["average_net_bps"]) > 0
        and float(row["selection"]["profit_factor_net"] or 0.0) > 1.0
        and float(row["validation"]["profit_factor_net"] or 0.0) > 1.0
    ]
    payload: dict[str, object] = {
        "schema_version": "vnedge.post_absorption_direction_study.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "study_id": "post_absorption_direction_reclaim_failure_v1",
        "source": {
            "journal_path": str(journal_path),
            "event_root": str(event_root),
            "raw_detections": len(raw_events),
            "independent_episodes": len(events),
            "collapsed_detections": episode_report.collapsed_detections,
            "duplicates_ignored": duplicates,
            "symbols": list(symbols),
            "first_decision_ts": _iso(events[0].decision_ts_us),
            "last_decision_ts": _iso(events[-1].decision_ts_us),
            "verified_replay_events": replay_events,
            "availability_clock": "local_recv_ns",
        },
        "contract": {
            "response_delays_ms": list(delays_ms),
            "outcome_horizons_ms": list(horizons_ms),
            "maximum_capture_lag_ms": maximum_capture_lag_ms,
            "maximum_entry_wait_ms": maximum_entry_wait_ms,
            "tick_sizes": ticks,
            "rules": [rule.__dict__ for rule in rules],
            "entry_fill": "first public trade at-or-after captured response state",
            "exit_fill": "first public trade at-or-after outcome horizon",
            "route_cost_contract": route_contract.to_dict(),
            "selection_split": "first 70 percent of already-seen development tape",
            "validation_split": "last 30 percent of already-seen development tape",
            "code_version": code_version,
            "episode_separation_ms": episode_separation_ms,
            "control_gate_required": require_control_gate,
        },
        "episode_collapse": episode_report_dict(episode_report),
        "control_qualification": control_qualification,
        "coverage": {
            "requested_response_states": len(events) * len(delays_ms),
            "captured_response_states": len(snapshots),
            "missed_response_states": missed_snapshots,
            "late_or_missing_entries": late_entries,
            "incomplete_horizons": incomplete_horizons,
            "classified_by_rule_delay": [
                {"rule_id": key[0], "response_delay_ms": key[1], "observations": value}
                for key, value in sorted(classified.items())
            ],
        },
        "economic_matrix": rows,
        "selection_validation_comparisons": comparisons,
        "best_comparisons": comparisons[:20],
        "diagnosis": {
            "supported_development_cells": len(supported),
            "best_comparison": comparisons[0] if comparisons else None,
            "verdict": (
                "CONTROL_GATE_FAILED_EXIT_TESTING_BLOCKED"
                if not exit_testing_authorized
                else "DEVELOPMENT_CELL_REQUIRES_FUTURE_CONFIRMATION"
                if supported
                else "NO_STABLE_AFTER_COST_DIRECTION_RULE_FOUND"
            ),
            "warning": (
                "Both partitions are already-seen development data. This is not a sealed "
                "holdout and cannot authorize a scanner or paper trading."
            ),
        },
        "development_window_only": True,
        "sealed_holdout_opened": False,
        "scanner_implementation_authorized": False,
        "paper_authorized": False,
        "can_trade": False,
        "can_promote": False,
        "order_route": "absent",
    }
    payload["deterministic_result_hash"] = _result_hash(payload)
    _atomic_json(Path(output_path), payload)
    return payload


def _capture_snapshots(
    event_root: Path,
    events: tuple[AbsorptionEvent, ...],
    delays_ms: tuple[int, ...],
    *,
    maximum_capture_lag_ms: int,
    code_version: str,
) -> tuple[dict[tuple[str, int], ResponseSnapshot], int, int]:
    collector = _ContextCollector(
        events,
        delays_ms,
        maximum_capture_lag_ms=maximum_capture_lag_ms,
    )
    symbols = tuple(sorted({event.symbol for event in events}))
    trigger = EventDrivenTriggerLayer(
        (collector,),
        config=EventTriggerConfig(
            min_eval_interval_ms=50,
            enabled_symbols=symbols,
            absorption=AbsorptionDetectorConfig(enabled=False),
            require_htf_context=False,
            require_reference_prices=False,
            require_trade_book_join=False,
        ),
        gates=SignalGateConfig(allowed_symbols=symbols),
    )
    bridge = DeltaVerifiedEventBridge(trigger)
    padding = timedelta(minutes=2)
    first = datetime.fromtimestamp(events[0].decision_ts_us / 1_000_000, tz=UTC) - padding
    last = datetime.fromtimestamp(events[-1].decision_ts_us / 1_000_000, tz=UTC)
    last += timedelta(milliseconds=delays_ms[-1] + maximum_capture_lag_ms) + padding
    config = ReplayConfig(
        symbols=symbols,
        start_ts_us=int(first.timestamp() * 1_000_000),
        end_ts_us=int(last.timestamp() * 1_000_000),
        # The quality layer consumes the same collector and needs point-in-time
        # basis/OI truth in addition to trade and book state. Missing channels
        # remain explicit missing-data blockers; they are never imputed.
        channels=(
            "trades",
            "ob_updates",
            "ticker",
            "funding_rate",
            "spot_price",
            "mark_price",
        ),
        enable_feature_engine=True,
        enable_scanner=True,
        journal_mode="none",
        code_version=code_version,
    )
    replay_events = 0
    for recorded in DeltaShardEventStore(event_root).iter_events(config):
        bridge.consume(dict(recorded.envelope), integrity_verified=True)
        if recorded.envelope.get("replay_warmup") is not True:
            replay_events += 1
    collector.finalize()
    return collector.snapshots, collector.missed, replay_events


def _load_absorption_events(path: Path) -> tuple[tuple[AbsorptionEvent, ...], int]:
    if not path.is_file():
        raise FileNotFoundError(f"event research journal not found: {path}")
    found: dict[str, AbsorptionEvent] = {}
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
            row = AbsorptionEvent(
                key=str(raw.get("key") or ""),
                symbol=str(raw.get("symbol") or event.get("symbol") or ""),
                decision_ts_us=decision if decision is not None else -1,
                level_price=float(event.get("price") or 0.0),
                reversal_direction=int(event.get("reversal_direction") or 0),
                strength=float(event.get("strength") or 0.0),
                stacked=bool(event.get("is_stacked") or raw.get("was_stacked")),
            )
            previous = found.get(row.key)
            if previous is not None:
                if previous != row:
                    raise ValueError(f"conflicting duplicate observation: {row.key}")
                duplicates += 1
            else:
                found[row.key] = row
    return tuple(sorted(found.values(), key=lambda row: (row.decision_ts_us, row.key))), duplicates


def _metric_rows(
    groups: Mapping[tuple[str, str, str, int, int, str], _Metrics],
) -> list[dict[str, object]]:
    names = ("partition", "symbol", "rule_id", "response_delay_ms", "horizon_ms", "state")
    rows: list[dict[str, object]] = []
    for key, metrics in groups.items():
        row = {name: value for name, value in zip(names, key)}
        row.update(metrics.to_dict())
        rows.append(row)
    return sorted(rows, key=lambda row: tuple(str(row[name]) for name in names))


def _selection_validation_comparisons(
    rows: list[dict[str, object]],
    *,
    minimum_selection_observations: int,
    minimum_validation_observations: int,
) -> list[dict[str, object]]:
    identity = ("symbol", "rule_id", "response_delay_ms", "horizon_ms", "state")
    indexed = {
        (str(row["partition"]), *(row[name] for name in identity)): row
        for row in rows
        if row["partition"] in {"selection_70", "validation_30"}
    }
    results: list[dict[str, object]] = []
    keys = {key[1:] for key in indexed}
    metric_names = (
        "observations",
        "average_gross_bps",
        "average_net_bps",
        "total_net_bps",
        "profit_factor_net",
        "net_positive_rate",
        "average_mfe_bps",
        "average_mae_bps",
        "average_mfe_after_cost_bps",
        "average_time_to_mfe_ms",
        "average_capture_ratio",
        "fee_wall_break_rate_pct",
        "exit_diagnosis_counts",
    )
    for key in keys:
        selection = indexed.get(("selection_70", *key))
        validation = indexed.get(("validation_30", *key))
        if selection is None or validation is None:
            continue
        if int(selection["observations"]) < minimum_selection_observations:
            continue
        if int(validation["observations"]) < minimum_validation_observations:
            continue
        results.append(
            {
                **{name: value for name, value in zip(identity, key)},
                "selection": {name: selection[name] for name in metric_names},
                "validation": {name: validation[name] for name in metric_names},
            }
        )
    return sorted(
        results,
        key=lambda row: (
            min(
                float(row["selection"]["average_net_bps"]),
                float(row["validation"]["average_net_bps"]),
            ),
            int(row["selection"]["observations"]) + int(row["validation"]["observations"]),
        ),
        reverse=True,
    )


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


def _result_hash(payload: Mapping[str, object]) -> str:
    stable = {
        key: payload[key]
        for key in (
            "study_id",
            "source",
            "contract",
            "episode_collapse",
            "control_qualification",
            "coverage",
            "economic_matrix",
            "selection_validation_comparisons",
        )
    }
    encoded = json.dumps(stable, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
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


def _csv_ints(value: str) -> tuple[int, ...]:
    return tuple(int(item.strip()) for item in value.split(",") if item.strip())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", type=Path, default=Path("logs/delta_event_research.jsonl"))
    parser.add_argument("--event-root", type=Path, default=Path("data/delta_events"))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("research/live_research/post_absorption_direction_study_latest.json"),
    )
    parser.add_argument("--delays-ms", type=_csv_ints, default=DEFAULT_RESPONSE_DELAYS_MS)
    parser.add_argument("--horizons-ms", type=_csv_ints, default=DEFAULT_OUTCOME_HORIZONS_MS)
    parser.add_argument("--maximum-capture-lag-ms", type=int, default=250)
    parser.add_argument("--maximum-entry-wait-ms", type=int, default=2_000)
    parser.add_argument("--code-version", default="local-research")
    args = parser.parse_args(argv)
    result = build_post_event_direction_study(
        args.journal,
        event_root=args.event_root,
        output_path=args.output,
        delays_ms=args.delays_ms,
        horizons_ms=args.horizons_ms,
        maximum_capture_lag_ms=args.maximum_capture_lag_ms,
        maximum_entry_wait_ms=args.maximum_entry_wait_ms,
        code_version=args.code_version,
    )
    print(
        json.dumps(
            {
                "study_id": result["study_id"],
                "coverage": result["coverage"],
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
