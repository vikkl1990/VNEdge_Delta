"""Direction-agnostic response atlas for recorded Delta market events.

The atlas does not generate signals. It asks three questions in order:

1. Did enough movement exist after the event to clear realistic costs?
2. Was reversal or continuation the better causal response?
3. Did waiting for additional reaction time improve or destroy the response?

Every entry and exit is the first recorded public trade available at-or-after
the declared delay/horizon. Both directions are evaluated from the same entry
print. Results are discovery evidence only; no cell grants scanner, paper, or
live authority.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from bisect import bisect_left
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from vnedge.exchange.delta_public_schema import parse_public_trade
from vnedge.replay.models import ReplayConfig
from vnedge.replay.store import DeltaShardEventStore
from vnedge.research.event_episodes import (
    collapse_independent_episodes,
    episode_report_dict,
    matched_movement_control_gate,
)
from vnedge.research.strategy_evidence_registry import route_cost_contract
from vnedge.scalping.delta_engine.fee_model import DeltaFeeModel

DEFAULT_DELAYS_MS = (0, 250, 1_000)
DEFAULT_HORIZONS_MS = (60_000, 180_000, 300_000, 600_000, 900_000, 1_800_000)


@dataclass(frozen=True)
class EventObservation:
    key: str
    symbol: str
    decision_ts_us: int
    reversal_direction: int
    strength: float
    volume_percentile: float
    stacked: bool


@dataclass(frozen=True)
class PriceTape:
    """Availability-ordered public trade prices for one symbol."""

    timestamps_us: tuple[int, ...]
    prices: tuple[float, ...]

    def __post_init__(self) -> None:
        if len(self.timestamps_us) != len(self.prices):
            raise ValueError("price tape timestamps/prices length mismatch")
        if any(price <= 0 for price in self.prices):
            raise ValueError("price tape contains a non-positive trade")
        if any(
            following < previous
            for previous, following in zip(self.timestamps_us, self.timestamps_us[1:])
        ):
            raise ValueError("price tape must be availability ordered")

    def first_at_or_after(self, ts_us: int) -> int | None:
        index = bisect_left(self.timestamps_us, ts_us)
        return index if index < len(self.timestamps_us) else None

    def extrema(self, start: int, stop: int) -> tuple[float, float]:
        if not 0 <= start < stop <= len(self.prices):
            raise ValueError("invalid price-tape range")
        values = self.prices[start:stop]
        return min(values), max(values)

    def directional_path_stats(
        self,
        start: int,
        stop: int,
        *,
        direction: int,
        entry_price: float,
    ) -> tuple[float, float, float]:
        """Return MFE, MAE and time-to-MFE using only the declared path."""

        if direction not in {-1, 1} or entry_price <= 0 or not 0 <= start < stop <= len(self.prices):
            raise ValueError("invalid directional path contract")
        path = self.prices[start:stop]
        if direction > 0:
            favorable_price = max(path)
            adverse_price = min(path)
            mfe = max(0.0, (favorable_price / entry_price - 1.0) * 10_000.0)
            mae = max(0.0, (1.0 - adverse_price / entry_price) * 10_000.0)
            mfe_index = path.index(favorable_price)
        else:
            favorable_price = min(path)
            adverse_price = max(path)
            mfe = max(0.0, (1.0 - favorable_price / entry_price) * 10_000.0)
            mae = max(0.0, (adverse_price / entry_price - 1.0) * 10_000.0)
            mfe_index = path.index(favorable_price)
        time_to_mfe_ms = max(
            0.0,
            (self.timestamps_us[start + mfe_index] - self.timestamps_us[start]) / 1_000.0,
        )
        return mfe, mae, time_to_mfe_ms


@dataclass
class _Metrics:
    observations: int = 0
    gross_sum: float = 0.0
    net_sum: float = 0.0
    mfe_sum: float = 0.0
    mae_sum: float = 0.0
    gains: float = 0.0
    losses: float = 0.0
    positive_net: int = 0
    clear_cost: int = 0
    clear_two_costs: int = 0
    clear_three_costs: int = 0
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
            self.positive_net += 1
        elif net < 0:
            self.losses += abs(net)
        self.clear_cost += mfe >= cost
        self.clear_two_costs += mfe >= 2.0 * cost
        self.clear_three_costs += mfe >= 3.0 * cost
        self.mfe_after_cost_sum += mfe - cost
        self.time_to_mfe_ms_sum += time_to_mfe_ms
        if mfe > 0:
            self.capture_ratio_sum += gross / mfe
            self.capture_ratio_samples += 1
        self.exit_diagnoses[_exit_diagnosis(net=net, mfe_after_cost=mfe - cost)] += 1

    def to_dict(self) -> dict[str, object]:
        n = self.observations
        return {
            "observations": n,
            "average_gross_bps": self.gross_sum / n if n else 0.0,
            "average_net_bps": self.net_sum / n if n else 0.0,
            "profit_factor_net": self.gains / self.losses if self.losses else None,
            "net_positive_rate": self.positive_net / n if n else 0.0,
            "average_mfe_bps": self.mfe_sum / n if n else 0.0,
            "average_mae_bps": self.mae_sum / n if n else 0.0,
            "mfe_clears_cost_rate": self.clear_cost / n if n else 0.0,
            "mfe_clears_2x_cost_rate": self.clear_two_costs / n if n else 0.0,
            "mfe_clears_3x_cost_rate": self.clear_three_costs / n if n else 0.0,
            "average_mfe_after_cost_bps": self.mfe_after_cost_sum / n if n else 0.0,
            "average_time_to_mfe_ms": self.time_to_mfe_ms_sum / n if n else 0.0,
            "average_capture_ratio": (
                self.capture_ratio_sum / self.capture_ratio_samples
                if self.capture_ratio_samples
                else None
            ),
            "fee_wall_break_rate_pct": self.clear_cost / n * 100.0 if n else 0.0,
            "exit_diagnosis_counts": dict(self.exit_diagnoses),
        }


def _exit_diagnosis(*, net: float, mfe_after_cost: float) -> str:
    if mfe_after_cost <= 0:
        return "MOVE_NEVER_CLEARED_COST"
    if net > 0:
        return "CAPTURED_AFTER_COST"
    return "GAVE_BACK_FEE_WALL_MOVE"


def build_event_response_atlas(
    journal_path: Path | str,
    *,
    event_root: Path | str = "data/delta_events",
    output_path: Path | str = "research/live_research/event_response_atlas_latest.json",
    delays_ms: tuple[int, ...] = DEFAULT_DELAYS_MS,
    horizons_ms: tuple[int, ...] = DEFAULT_HORIZONS_MS,
    maximum_entry_wait_ms: int = 2_000,
    minimum_cell_observations: int = 30,
    episode_separation_ms: int = 30_000,
    require_control_gate: bool = True,
    control_delay_ms: int = 250,
    control_horizon_ms: int = 900_000,
    control_offset_ms: int = 1_800_000,
    control_exclusion_ms: int = 60_000,
    minimum_control_pairs: int = 30,
    minimum_control_uplift_bps: float = 0.0,
    tapes: Mapping[str, PriceTape] | None = None,
    code_version: str = "local-research",
) -> dict[str, object]:
    """Build a frozen descriptive atlas from existing absorption events."""

    if not delays_ms or not horizons_ms:
        raise ValueError("at least one entry delay and response horizon are required")
    if any(value < 0 for value in delays_ms):
        raise ValueError("entry delays cannot be negative")
    if any(value <= 0 for value in horizons_ms):
        raise ValueError("response horizons must be positive")
    if maximum_entry_wait_ms < 0 or minimum_cell_observations <= 0:
        raise ValueError("entry wait and minimum cell observations must be valid")
    delays_ms = tuple(sorted(set(delays_ms)))
    horizons_ms = tuple(sorted(set(horizons_ms)))
    raw_observations, duplicates = _load_observations(Path(journal_path))
    collapsed, episode_report = collapse_independent_episodes(
        raw_observations,
        separation_ms=episode_separation_ms,
    )
    observations = list(collapsed)
    symbols = tuple(sorted({row.symbol for row in observations}))
    loaded_tapes = dict(tapes or _load_tapes(
        Path(event_root), observations, horizons_ms[-1], delays_ms[-1], code_version
    ))
    fee_model = DeltaFeeModel(default_slippage_bps_per_leg=1.5)
    route_contract = route_cost_contract("taker_full_14_8")
    control_qualification = matched_movement_control_gate(
        observations,
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

    matrix: dict[tuple[str, str, int, int], _Metrics] = defaultdict(_Metrics)
    feature_cells: dict[tuple[str, str, int, int, str, str, str], _Metrics] = defaultdict(
        _Metrics
    )
    opportunity: dict[tuple[str, int, int], _Metrics] = defaultdict(_Metrics)
    chronological: dict[tuple[str, str], list[tuple[int, float]]] = defaultdict(list)
    evaluated_entries = 0
    missed_entries = 0
    incomplete_horizons = 0

    for observation in observations if exit_testing_authorized else ():
        tape = loaded_tapes.get(observation.symbol)
        if tape is None:
            missed_entries += len(delays_ms)
            continue
        for delay_ms in delays_ms:
            requested_entry_us = observation.decision_ts_us + delay_ms * 1_000
            entry_index = tape.first_at_or_after(requested_entry_us)
            if entry_index is None:
                missed_entries += 1
                continue
            entry_ts_us = tape.timestamps_us[entry_index]
            if entry_ts_us - requested_entry_us > maximum_entry_wait_ms * 1_000:
                missed_entries += 1
                continue
            evaluated_entries += 1
            entry_price = tape.prices[entry_index]
            for horizon_ms in horizons_ms:
                exit_index = tape.first_at_or_after(entry_ts_us + horizon_ms * 1_000)
                if exit_index is None:
                    incomplete_horizons += 1
                    continue
                exit_price = tape.prices[exit_index]
                cost_bps = fee_model.breakdown(
                    observation.symbol,
                    entry_is_maker=False,
                    exit_is_maker=False,
                    hold_seconds=horizon_ms / 1_000.0,
                    scalper_opted_in=False,
                ).total_bps
                long_mfe, long_mae, long_time_to_mfe = tape.directional_path_stats(
                    entry_index, exit_index + 1, direction=1, entry_price=entry_price
                )
                short_mfe, short_mae, short_time_to_mfe = tape.directional_path_stats(
                    entry_index, exit_index + 1, direction=-1, entry_price=entry_price
                )
                best_mfe = max(long_mfe, short_mfe)
                best_time_to_mfe = (
                    long_time_to_mfe if long_mfe >= short_mfe else short_time_to_mfe
                )
                # Direction-neutral opportunity is a hindsight upper bound. Its
                # net field must never be read as an executable strategy return.
                opportunity[(observation.symbol, delay_ms, horizon_ms)].add(
                    gross=best_mfe,
                    net=best_mfe - cost_bps,
                    mfe=best_mfe,
                    mae=0.0,
                    cost=cost_bps,
                    time_to_mfe_ms=best_time_to_mfe,
                )
                for hypothesis, direction in (
                    ("reversal", observation.reversal_direction),
                    ("continuation", -observation.reversal_direction),
                ):
                    gross = direction * (exit_price / entry_price - 1.0) * 10_000.0
                    mfe = long_mfe if direction > 0 else short_mfe
                    mae = long_mae if direction > 0 else short_mae
                    time_to_mfe = long_time_to_mfe if direction > 0 else short_time_to_mfe
                    net = gross - cost_bps
                    matrix[(observation.symbol, hypothesis, delay_ms, horizon_ms)].add(
                        gross=gross,
                        net=net,
                        mfe=mfe,
                        mae=mae,
                        cost=cost_bps,
                        time_to_mfe_ms=time_to_mfe,
                    )
                    feature_cells[
                        (
                            observation.symbol,
                            hypothesis,
                            delay_ms,
                            horizon_ms,
                            "stacked" if observation.stacked else "single_level",
                            _strength_bucket(observation.strength),
                            _volume_bucket(observation.volume_percentile),
                        )
                    ].add(
                        gross=gross,
                        net=net,
                        mfe=mfe,
                        mae=mae,
                        cost=cost_bps,
                        time_to_mfe_ms=time_to_mfe,
                    )
                    if delay_ms == 250 and horizon_ms == 900_000:
                        chronological[(observation.symbol, hypothesis)].append(
                            (observation.decision_ts_us, net)
                        )

    matrix_rows = _rows(matrix, ("symbol", "hypothesis", "entry_delay_ms", "horizon_ms"))
    opportunity_rows = _rows(
        opportunity, ("symbol", "entry_delay_ms", "horizon_ms"), opportunity=True
    )
    feature_rows = _rows(
        feature_cells,
        (
            "symbol",
            "hypothesis",
            "entry_delay_ms",
            "horizon_ms",
            "structure",
            "strength_bucket",
            "volume_bucket",
        ),
    )
    eligible_feature_rows = [
        row for row in feature_rows if row["observations"] >= minimum_cell_observations
    ]
    best_cells = sorted(
        eligible_feature_rows,
        key=lambda row: (float(row["average_net_bps"]), int(row["observations"])),
        reverse=True,
    )[:20]
    stable_positive = [
        row
        for row in best_cells
        if float(row["average_net_bps"]) > 0
        and (row["profit_factor_net"] or 0.0) > 1.0
    ]
    source_window = {
        "first_decision_ts": _iso(observations[0].decision_ts_us) if observations else None,
        "last_decision_ts": _iso(observations[-1].decision_ts_us) if observations else None,
    }
    payload: dict[str, object] = {
        "schema_version": "vnedge.event_response_atlas.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "atlas_id": "delta_absorption_response_atlas_v1",
        "source": {
            "journal_path": str(journal_path),
            "event_root": str(event_root),
            "event_family": "absorption",
            "raw_detections": len(raw_observations),
            "independent_episodes": len(observations),
            "collapsed_detections": episode_report.collapsed_detections,
            "duplicate_observations_ignored": duplicates,
            "symbols": list(symbols),
            **source_window,
            "tape_trades": {
                symbol: len(tape.prices) for symbol, tape in sorted(loaded_tapes.items())
            },
            "availability_clock": "local_recv_ns",
            "entry_fill": "first public trade at-or-after decision plus delay",
            "exit_fill": "first public trade at-or-after horizon",
            "manifest_verification": "enforced by DeltaShardEventStore",
        },
        "contract": {
            "entry_delays_ms": list(delays_ms),
            "horizons_ms": list(horizons_ms),
            "maximum_entry_wait_ms": maximum_entry_wait_ms,
            "route_cost_contract": route_contract.to_dict(),
            "minimum_cell_observations": minimum_cell_observations,
            "directions": ["reversal", "continuation"],
            "code_version": code_version,
            "episode_separation_ms": episode_separation_ms,
            "control_gate_required": require_control_gate,
        },
        "episode_collapse": episode_report_dict(episode_report),
        "control_qualification": control_qualification,
        "coverage": {
            "evaluated_entries": evaluated_entries,
            "missed_entries": missed_entries,
            "incomplete_horizons": incomplete_horizons,
        },
        "opportunity_atlas": opportunity_rows,
        "direction_entry_exit_matrix": matrix_rows,
        "feature_cells": feature_rows,
        "best_discovery_cells": best_cells,
        "chronological_stability": _chronological_halves(chronological),
        "diagnosis": {
            "sufficiently_populated_cells": len(eligible_feature_rows),
            "positive_discovery_cells": len(stable_positive),
            "best_cell": best_cells[0] if best_cells else None,
            "verdict": (
                "CONTROL_GATE_FAILED_EXIT_TESTING_BLOCKED"
                if not exit_testing_authorized
                else
                "DISCOVERY_CELL_REQUIRES_FUTURE_CONFIRMATION"
                if stable_positive
                else "NO_AFTER_COST_DIRECTIONAL_CELL_FOUND"
            ),
            "next_step": (
                "Collect independent episodes until selected events beat nearby controls."
                if not exit_testing_authorized
                else
                "Freeze one economically coherent cell and confirm it on future tape."
                if stable_positive
                else "Collect more event families and tape; do not loosen thresholds."
            ),
            "warning": (
                "The opportunity atlas uses hindsight MFE only to establish that movement "
                "existed. It is not a tradable return. Exit testing is blocked until "
                "independent event episodes outperform matched nearby controls."
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


def _load_observations(path: Path) -> tuple[list[EventObservation], int]:
    if not path.is_file():
        raise FileNotFoundError(f"event research journal not found: {path}")
    found: dict[str, EventObservation] = {}
    duplicates = 0
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            try:
                envelope = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid journal JSON at line {line_number}") from exc
            if envelope.get("kind") != "delta_absorption_research_observation":
                continue
            raw = envelope.get("payload")
            event = raw.get("event") if isinstance(raw, dict) else None
            if not isinstance(raw, dict) or not isinstance(event, dict):
                raise TypeError(f"malformed absorption observation at line {line_number}")
            key = str(raw.get("key") or "")
            decision = _parse_iso_us(raw.get("decision_ts"))
            direction = int(event.get("reversal_direction") or 0)
            if not key or decision is None or direction not in {-1, 1}:
                raise ValueError(f"incomplete absorption observation at line {line_number}")
            row = EventObservation(
                key=key,
                symbol=str(raw.get("symbol") or event.get("symbol") or "").upper(),
                decision_ts_us=decision,
                reversal_direction=direction,
                strength=float(event.get("strength") or 0.0),
                volume_percentile=float(raw.get("volume_percentile") or 0.0),
                stacked=bool(event.get("is_stacked") or raw.get("was_stacked")),
            )
            previous = found.get(key)
            if previous is not None:
                if previous != row:
                    raise ValueError(f"conflicting duplicate observation: {key}")
                duplicates += 1
                continue
            found[key] = row
    return sorted(found.values(), key=lambda row: (row.decision_ts_us, row.key)), duplicates


def _load_tapes(
    root: Path,
    observations: list[EventObservation],
    maximum_horizon_ms: int,
    maximum_delay_ms: int,
    code_version: str,
) -> Mapping[str, PriceTape]:
    if not observations:
        return {}
    padding = timedelta(minutes=10)
    first = datetime.fromtimestamp(observations[0].decision_ts_us / 1_000_000, tz=UTC) - padding
    last = datetime.fromtimestamp(observations[-1].decision_ts_us / 1_000_000, tz=UTC)
    last += timedelta(milliseconds=maximum_horizon_ms + maximum_delay_ms) + padding
    config = ReplayConfig(
        symbols=tuple(sorted({row.symbol for row in observations})),
        start_ts_us=int(first.timestamp() * 1_000_000),
        end_ts_us=int(last.timestamp() * 1_000_000),
        channels=("trades",),
        enable_feature_engine=False,
        enable_scanner=False,
        journal_mode="none",
        code_version=code_version,
    )
    timestamps: dict[str, list[int]] = defaultdict(list)
    prices: dict[str, list[float]] = defaultdict(list)
    last_identity: dict[str, tuple[int, float, float, str] | None] = defaultdict(lambda: None)
    for event in DeltaShardEventStore(root).iter_events(config):
        trade = parse_public_trade(event.raw_message)
        identity = (
            int(trade.trade_timestamp_us or event.exchange_timestamp_us),
            trade.price,
            trade.size,
            trade.aggressor_side,
        )
        # Adjacent duplicate publications across overlapping recorder sessions
        # must not overweight a price path. Genuine later identical trades have
        # a distinct exchange timestamp and are retained.
        if identity == last_identity[event.symbol]:
            continue
        last_identity[event.symbol] = identity
        timestamps[event.symbol].append(event.local_recv_ns // 1_000)
        prices[event.symbol].append(trade.price)
    return {
        symbol: PriceTape(tuple(timestamps[symbol]), tuple(prices[symbol]))
        for symbol in sorted(timestamps)
    }


def _rows(
    groups: Mapping[tuple[Any, ...], _Metrics],
    names: tuple[str, ...],
    *,
    opportunity: bool = False,
) -> list[dict[str, object]]:
    rows = []
    for key, metrics in groups.items():
        row = {name: value for name, value in zip(names, key)}
        row.update(metrics.to_dict())
        if opportunity:
            row["hindsight_opportunity_only"] = True
        rows.append(row)
    return sorted(rows, key=lambda row: tuple(str(row[name]) for name in names))


def _chronological_halves(
    groups: Mapping[tuple[str, str], list[tuple[int, float]]]
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for (symbol, hypothesis), values in sorted(groups.items()):
        ordered = sorted(values)
        split = len(ordered) // 2
        halves = (ordered[:split], ordered[split:])
        rows.append(
            {
                "symbol": symbol,
                "hypothesis": hypothesis,
                "entry_delay_ms": 250,
                "horizon_ms": 900_000,
                "first_half": _net_summary(value for _, value in halves[0]),
                "second_half": _net_summary(value for _, value in halves[1]),
            }
        )
    return rows


def _net_summary(values: Iterable[float]) -> dict[str, float | int | None]:
    rows = list(values)
    gains = sum(value for value in rows if value > 0)
    losses = abs(sum(value for value in rows if value < 0))
    return {
        "observations": len(rows),
        "average_net_bps": sum(rows) / len(rows) if rows else 0.0,
        "profit_factor_net": gains / losses if losses else None,
    }


def _strength_bucket(value: float) -> str:
    if value < 0.65:
        return "low_lt_0.65"
    if value < 0.80:
        return "medium_0.65_0.80"
    return "high_gte_0.80"


def _volume_bucket(value: float) -> str:
    if value < 0.50:
        return "lower_half"
    if value < 0.80:
        return "upper_mid_0.50_0.80"
    return "top_0.80_1.00"


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


def _iso(ts_us: int) -> str:
    return datetime.fromtimestamp(ts_us / 1_000_000, tz=UTC).isoformat()


def _result_hash(payload: Mapping[str, object]) -> str:
    stable = {
        key: payload[key]
        for key in (
            "atlas_id",
            "source",
            "contract",
            "episode_collapse",
            "control_qualification",
            "coverage",
            "opportunity_atlas",
            "direction_entry_exit_matrix",
            "feature_cells",
            "chronological_stability",
        )
    }
    raw = json.dumps(stable, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


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
        default=Path("research/live_research/event_response_atlas_latest.json"),
    )
    parser.add_argument("--delays-ms", type=_csv_ints, default=DEFAULT_DELAYS_MS)
    parser.add_argument("--horizons-ms", type=_csv_ints, default=DEFAULT_HORIZONS_MS)
    parser.add_argument("--maximum-entry-wait-ms", type=int, default=2_000)
    parser.add_argument("--minimum-cell-observations", type=int, default=30)
    parser.add_argument("--code-version", default="local-research")
    args = parser.parse_args(argv)
    payload = build_event_response_atlas(
        args.journal,
        event_root=args.event_root,
        output_path=args.output,
        delays_ms=args.delays_ms,
        horizons_ms=args.horizons_ms,
        maximum_entry_wait_ms=args.maximum_entry_wait_ms,
        minimum_cell_observations=args.minimum_cell_observations,
        code_version=args.code_version,
    )
    print(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
