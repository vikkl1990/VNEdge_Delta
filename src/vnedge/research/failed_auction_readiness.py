"""Fail-closed data readiness audit for failed_auction_response_v1.

This module deliberately contains no scanner or signal emitter. It proves only
whether a frozen event window is fit to begin implementing/replaying the
hypothesis; it can never authorize paper or live trading.
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from types import MappingProxyType
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from vnedge.exchange.delta_event_recorder import verify_event_tree
from vnedge.replay.models import RecordedEvent, RecordingValidationReport
from vnedge.replay.models import ReplayConfig
from vnedge.replay.store import DeltaShardEventStore
from vnedge.replay.validator import validate_recorded_events


class FailedAuctionDataContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbols: tuple[str, ...]
    required_channels: tuple[str, ...]
    minimum_continuous_days: int = Field(ge=1)
    minimum_total_events: int = Field(ge=1)
    minimum_events_per_symbol: int = Field(ge=1)
    minimum_trade_events_per_symbol: int = Field(ge=1)
    minimum_book_events_per_symbol: int = Field(ge=1)
    minimum_hour_coverage_ratio: float = Field(gt=0, le=1)
    maximum_book_interarrival_p95_ms: float = Field(gt=0)
    minimum_snapshots_per_symbol: int = Field(ge=1)
    maximum_sequence_gaps: Literal[0]
    maximum_checksum_failures: Literal[0]
    maximum_duplicate_event_ids: Literal[0]
    maximum_receive_order_regressions: Literal[0]
    require_manifest_verification: Literal[True]
    require_no_partial_files: Literal[True]
    incomplete_depth_policy: Literal["discard_until_verified_snapshot"]
    integrity_fault_policy: Literal["reset_and_block_until_verified_snapshot"]
    event_order: Literal["local_receive_then_monotonic_then_event_index"]
    minimum_history_is_readiness_only: Literal[True]

    @model_validator(mode="after")
    def normalize(self) -> FailedAuctionDataContract:
        symbols = tuple(value.upper().strip() for value in self.symbols)
        channels = tuple(value.strip() for value in self.required_channels)
        if not symbols or len(symbols) != len(set(symbols)):
            raise ValueError("data symbols must be non-empty and unique")
        if set(channels) != {"trades", "ob_updates"}:
            raise ValueError("v1 requires exactly trades and ob_updates")
        object.__setattr__(self, "symbols", symbols)
        object.__setattr__(self, "required_channels", channels)
        return self


class FailedAuctionCosts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    entry_is_maker: Literal[False]
    exit_is_maker: Literal[False]
    scalper_opted_in: Literal[False]
    deto_enabled: Literal[False]
    maker_fee_bps_pre_tax: float = Field(ge=0)
    taker_fee_bps_pre_tax: float = Field(ge=0)
    gst_rate: float = Field(ge=0)
    slippage_bps_per_leg: float = Field(ge=0)
    baseline_round_trip_bps: float = Field(gt=0)
    gate_formula: Literal[
        "(gross_target_bps - cost_bps) / (stop_bps + cost_bps) >= minimum_net_reward_risk"
    ]
    apply_cost_once_in_realized_pnl: Literal[True]
    extra_cost_cushion_in_gate: Literal[False]

    @model_validator(mode="after")
    def validate_baseline_cost(self) -> FailedAuctionCosts:
        expected = (
            2 * self.taker_fee_bps_pre_tax * (1 + self.gst_rate)
            + 2 * self.slippage_bps_per_leg
        )
        if abs(expected - self.baseline_round_trip_bps) > 1e-9:
            raise ValueError("baseline_round_trip_bps does not match frozen taker costs")
        return self


class FailedAuctionDetection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    attack_window_ms: int = Field(gt=0)
    minimum_attack_duration_ms: int = Field(gt=0)
    minimum_aggression_share: float = Field(gt=0.5, le=1)
    minimum_aggressive_notional_usd: float = Field(gt=0)
    maximum_price_progress_ticks: float = Field(gt=0)
    replenish_to_pre_attack_ratio: float = Field(gt=0, le=1)
    replenishment_deadline_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_windows(self) -> FailedAuctionDetection:
        if self.minimum_attack_duration_ms >= self.attack_window_ms:
            raise ValueError("minimum attack duration must be below attack window")
        return self


class FailedAuctionConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    window_ms: int = Field(gt=0)
    minimum_microprice_turn_ticks: float = Field(gt=0)
    minimum_reversal_flow_share: float = Field(gt=0.5, le=1)
    retest_window_ms: int = Field(gt=0)
    retest_band_ticks: float = Field(gt=0)
    retest_hold_ms: int = Field(gt=0)
    trade_through_tolerance_ticks: Literal[0.0]
    require_failed_retest: Literal[True]


class FailedAuctionEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fill_style: Literal["taker_marketable_limit"]
    signal_to_fill_latency_ms: int = Field(gt=0)
    candidate_expiry_ms: int = Field(gt=0)
    maximum_slippage_bps_per_leg: float = Field(ge=0)
    long_fill: Literal["first_ask_after_latency"]
    short_fill: Literal["first_bid_after_latency"]
    revalidate_geometry_at_fill: Literal[True]
    same_event_fill_forbidden: Literal[True]

    @model_validator(mode="after")
    def validate_expiry(self) -> FailedAuctionEntry:
        if self.candidate_expiry_ms <= self.signal_to_fill_latency_ms:
            raise ValueError("candidate expiry must exceed signal-to-fill latency")
        return self


class FailedAuctionExit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    stop_noise_ticks: float = Field(gt=0)
    stop_spread_multiple: float = Field(gt=0)
    stop_recent_noise_window_ms: int = Field(gt=0)
    stop_recent_noise_percentile: float = Field(gt=0, le=1)
    target_rule: Literal["opposite_bound_of_pre_attack_micro_range"]
    target_lookback_ms: int = Field(gt=0)
    minimum_net_reward_risk: float = Field(gt=0)
    vertical_barrier_seconds: int = Field(gt=0)
    flow_invalidation_exit_enabled: Literal[False]
    trailing_stop_enabled: Literal[False]
    partial_targets_enabled: Literal[False]
    same_interval_ambiguity: Literal["stop_first"]
    data_integrity_failure: Literal["close_counterfactual_at_last_tradable_quote"]


class FailedAuctionDuplicates(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    one_active_observation_per_symbol: Literal[True]
    same_level_cooldown_ms: int = Field(gt=0)
    minimum_distinct_level_ticks: float = Field(gt=0)
    second_failure_after_cooldown_allowed: Literal[True]
    opposite_side_overlap_policy: Literal["reject_both"]
    exactly_once_key: Literal[
        "contract_id:symbol:side:absorption_level:first_hit_event_id"
    ]


class FailedAuctionValidation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    chronological_selection_fraction: float = Field(gt=0, lt=1)
    sealed_untouched_fraction: float = Field(gt=0, lt=1)
    minimum_selection_detections: int = Field(gt=0)
    minimum_selection_filled_trades: int = Field(gt=0)
    minimum_filled_trades_per_half: int = Field(gt=0)
    minimum_filled_trades_per_symbol: int = Field(gt=0)
    maximum_single_symbol_trade_share: float = Field(gt=0, le=1)
    minimum_selection_average_net_bps: float = Field(gt=0)
    minimum_selection_profit_factor: float = Field(gt=1)
    maximum_false_signal_rate: float = Field(ge=0, lt=1)
    require_positive_gross_expectancy: Literal[True]
    require_positive_net_expectancy: Literal[True]
    require_positive_chronological_halves: Literal[True]
    require_positive_each_symbol: Literal[True]
    open_untouched_only_after_all_selection_gates_pass: Literal[True]

    @model_validator(mode="after")
    def validate_split(self) -> FailedAuctionValidation:
        if abs(
            self.chronological_selection_fraction + self.sealed_untouched_fraction - 1
        ) > 1e-9:
            raise ValueError("selection and untouched fractions must sum to one")
        return self


class FailedAuctionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    parameter_search: Literal["forbidden_in_v1"]
    regime_filter: Literal["forbidden_in_v1"]
    session_filter: Literal["forbidden_in_v1"]
    meta_labeling: Literal["forbidden_until_standalone_selection_pass"]
    leverage_role: Literal["pnl_scaling_only_never_signal_or_edge"]
    scanner_code_policy: Literal["do_not_implement_until_data_readiness_passes"]
    selection_policy: Literal["do_not_run_until_live_replay_determinism_passes"]
    untouched_policy: Literal["open_once_only_after_selection_pass"]


class FailedAuctionResponseContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_id: Literal["failed_auction_response_v1"]
    version: Literal["1.0.0"]
    status: Literal["blocked_pending_data"]
    research_only: Literal[True]
    can_trade: Literal[False]
    can_promote: Literal[False]
    scanner_implementation_authorized: Literal[False]
    selection_authorized: Literal[False]
    data: FailedAuctionDataContract
    detection: FailedAuctionDetection
    confirmation: FailedAuctionConfirmation
    entry: FailedAuctionEntry
    exit: FailedAuctionExit
    costs: FailedAuctionCosts
    duplicates: FailedAuctionDuplicates
    validation: FailedAuctionValidation
    policy: FailedAuctionPolicy

    def minimum_gross_target_bps(self, stop_bps: float) -> float:
        """Unambiguous net-R gate; realized PnL applies cost exactly once."""

        if stop_bps <= 0:
            raise ValueError("stop_bps must be positive")
        cost = self.costs.baseline_round_trip_bps
        return cost + self.exit.minimum_net_reward_risk * (stop_bps + cost)


def load_failed_auction_contract(
    path: Path | str = Path("configs/research/failed_auction_response_v1.yaml"),
) -> FailedAuctionResponseContract:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("failed-auction contract must be a YAML object")
    return FailedAuctionResponseContract.model_validate(payload)


@dataclass(frozen=True)
class RecordingCoverageSummary:
    requested_start_ts_us: int
    requested_end_ts_us: int
    total_events: int
    counts_by_symbol: Mapping[str, int]
    counts_by_symbol_channel: Mapping[str, int]
    covered_hours_by_symbol_channel: Mapping[str, int]
    snapshot_counts_by_symbol: Mapping[str, int]
    book_interarrival_p95_ms_by_symbol: Mapping[str, float | None]

    def __post_init__(self) -> None:
        for name in (
            "counts_by_symbol",
            "counts_by_symbol_channel",
            "covered_hours_by_symbol_channel",
            "snapshot_counts_by_symbol",
            "book_interarrival_p95_ms_by_symbol",
        ):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))

    @property
    def requested_days(self) -> float:
        return (self.requested_end_ts_us - self.requested_start_ts_us) / 86_400_000_000

    @property
    def requested_hours(self) -> int:
        duration = self.requested_end_ts_us - self.requested_start_ts_us
        return max(1, (duration + 3_600_000_000 - 1) // 3_600_000_000)

    def to_dict(self) -> dict[str, object]:
        return {
            "requested_start_ts_us": self.requested_start_ts_us,
            "requested_end_ts_us": self.requested_end_ts_us,
            "requested_days": self.requested_days,
            "requested_hours": self.requested_hours,
            "total_events": self.total_events,
            "counts_by_symbol": dict(self.counts_by_symbol),
            "counts_by_symbol_channel": dict(self.counts_by_symbol_channel),
            "covered_hours_by_symbol_channel": dict(
                self.covered_hours_by_symbol_channel
            ),
            "snapshot_counts_by_symbol": dict(self.snapshot_counts_by_symbol),
            "book_interarrival_p95_ms_by_symbol": dict(
                self.book_interarrival_p95_ms_by_symbol
            ),
        }


def _p95_ms(values_ns: list[int]) -> float | None:
    if not values_ns:
        return None
    rows = sorted(values_ns)
    value = rows[min(len(rows) - 1, int((len(rows) - 1) * 0.95))]
    return value / 1_000_000


def summarize_recording_coverage(
    events: Iterable[RecordedEvent],
    *,
    start_ts_us: int,
    end_ts_us: int,
) -> RecordingCoverageSummary:
    if end_ts_us <= start_ts_us:
        raise ValueError("coverage window end must follow start")
    symbol_counts: Counter[str] = Counter()
    pair_counts: Counter[str] = Counter()
    hours: dict[str, set[int]] = defaultdict(set)
    snapshots: Counter[str] = Counter()
    last_book_receive: dict[str, int] = {}
    book_interarrivals: dict[str, list[int]] = defaultdict(list)
    total = 0
    for event in events:
        if event.envelope.get("replay_warmup") is True:
            continue
        if not start_ts_us <= event.exchange_timestamp_us < end_ts_us:
            continue
        total += 1
        symbol_counts[event.symbol] += 1
        pair = f"{event.symbol}:{event.channel}"
        pair_counts[pair] += 1
        hours[pair].add((event.exchange_timestamp_us - start_ts_us) // 3_600_000_000)
        if event.channel != "ob_updates":
            continue
        if event.raw_message.get("action") == "snapshot":
            snapshots[event.symbol] += 1
        previous = last_book_receive.get(event.symbol)
        if previous is not None and event.local_recv_ns >= previous:
            book_interarrivals[event.symbol].append(event.local_recv_ns - previous)
        last_book_receive[event.symbol] = event.local_recv_ns
    return RecordingCoverageSummary(
        requested_start_ts_us=start_ts_us,
        requested_end_ts_us=end_ts_us,
        total_events=total,
        counts_by_symbol=dict(symbol_counts),
        counts_by_symbol_channel=dict(pair_counts),
        covered_hours_by_symbol_channel={key: len(value) for key, value in hours.items()},
        snapshot_counts_by_symbol=dict(snapshots),
        book_interarrival_p95_ms_by_symbol={
            symbol: _p95_ms(values) for symbol, values in book_interarrivals.items()
        },
    )


@dataclass(frozen=True)
class FailedAuctionReadinessReport:
    data_ready: bool
    blockers: tuple[str, ...]
    coverage: RecordingCoverageSummary
    research_only: bool = True
    scanner_implementation_authorized: bool = False
    selection_authorized: bool = False
    can_trade: bool = False
    can_promote: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "data_ready": self.data_ready,
            "blockers": list(self.blockers),
            "coverage": self.coverage.to_dict(),
            "research_only": self.research_only,
            "scanner_implementation_authorized": self.scanner_implementation_authorized,
            "selection_authorized": self.selection_authorized,
            "can_trade": self.can_trade,
            "can_promote": self.can_promote,
        }


def evaluate_failed_auction_readiness(
    contract: FailedAuctionResponseContract,
    validation: RecordingValidationReport,
    coverage: RecordingCoverageSummary,
    *,
    manifest_verified: bool,
    partial_files: int = 0,
) -> FailedAuctionReadinessReport:
    rules = contract.data
    blockers: list[str] = []
    if not manifest_verified:
        blockers.append("manifest_verification_failed")
    if partial_files:
        blockers.append(f"partial_files_present:{partial_files}")
    if not validation.passed:
        blockers.append("semantic_recording_validation_failed")
    checks = (
        (validation.sequence_gaps, rules.maximum_sequence_gaps, "sequence_gaps"),
        (validation.checksum_failures, rules.maximum_checksum_failures, "checksum_failures"),
        (
            validation.duplicate_event_ids,
            rules.maximum_duplicate_event_ids,
            "duplicate_event_ids",
        ),
        (
            validation.local_clock_regressions,
            rules.maximum_receive_order_regressions,
            "receive_order_regressions",
        ),
    )
    for actual, maximum, name in checks:
        if actual > maximum:
            blockers.append(f"{name}:{actual}>{maximum}")
    if coverage.requested_days < rules.minimum_continuous_days:
        blockers.append(
            f"continuous_days:{coverage.requested_days:.3f}<{rules.minimum_continuous_days}"
        )
    if coverage.total_events < rules.minimum_total_events:
        blockers.append(f"total_events:{coverage.total_events}<{rules.minimum_total_events}")
    for symbol in rules.symbols:
        total = coverage.counts_by_symbol.get(symbol, 0)
        trades = coverage.counts_by_symbol_channel.get(f"{symbol}:trades", 0)
        books = coverage.counts_by_symbol_channel.get(f"{symbol}:ob_updates", 0)
        if total < rules.minimum_events_per_symbol:
            blockers.append(f"{symbol}:events:{total}<{rules.minimum_events_per_symbol}")
        if trades < rules.minimum_trade_events_per_symbol:
            blockers.append(
                f"{symbol}:trade_events:{trades}<{rules.minimum_trade_events_per_symbol}"
            )
        if books < rules.minimum_book_events_per_symbol:
            blockers.append(
                f"{symbol}:book_events:{books}<{rules.minimum_book_events_per_symbol}"
            )
        if coverage.snapshot_counts_by_symbol.get(symbol, 0) < rules.minimum_snapshots_per_symbol:
            blockers.append(f"{symbol}:verified_snapshot_missing")
        p95 = coverage.book_interarrival_p95_ms_by_symbol.get(symbol)
        if p95 is None or p95 > rules.maximum_book_interarrival_p95_ms:
            blockers.append(
                f"{symbol}:book_interarrival_p95_ms:{p95}>{rules.maximum_book_interarrival_p95_ms}"
            )
        for channel in rules.required_channels:
            key = f"{symbol}:{channel}"
            covered = coverage.covered_hours_by_symbol_channel.get(key, 0)
            ratio = covered / coverage.requested_hours
            if ratio < rules.minimum_hour_coverage_ratio:
                blockers.append(
                    f"{key}:hour_coverage:{ratio:.6f}<{rules.minimum_hour_coverage_ratio}"
                )
    unique = tuple(dict.fromkeys(blockers))
    return FailedAuctionReadinessReport(
        data_ready=not unique,
        blockers=unique,
        coverage=coverage,
    )


def _timestamp_us(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1_000_000)


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit whether Delta event tape can support failed-auction research"
    )
    parser.add_argument("--event-root", type=Path, required=True)
    parser.add_argument("--start", required=True, help="inclusive ISO-8601 timestamp")
    parser.add_argument("--end", required=True, help="exclusive ISO-8601 timestamp")
    parser.add_argument(
        "--contract",
        type=Path,
        default=Path("configs/research/failed_auction_response_v1.yaml"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(
            "research/live_research/failed_auction_response_v1_readiness_latest.json"
        ),
    )
    parser.add_argument("--code-version", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    contract = load_failed_auction_contract(args.contract)
    config = ReplayConfig(
        symbols=contract.data.symbols,
        start_ts_us=_timestamp_us(args.start),
        end_ts_us=_timestamp_us(args.end),
        channels=contract.data.required_channels,
        enable_scanner=False,
        journal_mode="none",
        code_version=args.code_version,
    )
    store = DeltaShardEventStore(args.event_root)
    tree = verify_event_tree(args.event_root)
    validation = validate_recorded_events(store.iter_events(config))
    coverage = summarize_recording_coverage(
        store.iter_events(config),
        start_ts_us=config.start_ts_us,
        end_ts_us=config.end_ts_us,
    )
    report = evaluate_failed_auction_readiness(
        contract,
        validation,
        coverage,
        manifest_verified=tree.passed,
        partial_files=len(tree.partial_files),
    )
    payload = {
        **report.to_dict(),
        "contract_id": contract.contract_id,
        "contract_version": contract.version,
        "code_version": config.code_version,
        "event_root": str(args.event_root),
        "tree_verification": {
            "passed": tree.passed,
            "storage_passed": tree.storage_passed,
            "continuity_passed": tree.continuity_passed,
            "shards": tree.shards,
            "records": tree.records,
            "partial_files": len(tree.partial_files),
            "integrity_faults": len(tree.integrity_faults),
            "issues": list(tree.issues),
        },
        "semantic_validation": validation.to_dict(),
    }
    _atomic_json(args.output, payload)
    print(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False))
    return 0 if report.data_ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
