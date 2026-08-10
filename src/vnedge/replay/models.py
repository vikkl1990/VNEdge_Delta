"""Immutable contracts for deterministic Delta event replay."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vnedge.scalping.delta_engine.types import SignalCandidate

ReplayJournalMode = Literal["research", "none"]


@dataclass(frozen=True)
class RecordedEvent:
    """One exact recorder envelope plus a parsed, immutable routing view."""

    event_id: str
    symbol: str
    exchange_timestamp_us: int
    local_recv_ns: int
    local_monotonic_ns: int
    event_index: int
    sequence: int | None
    checksum: str | None
    channel: str
    raw_message: Mapping[str, object]
    event_type: str
    envelope: Mapping[str, object] = field(repr=False)
    parsed: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if not self.event_id or not self.symbol or not self.channel or not self.event_type:
            raise ValueError("recorded event identity fields are required")
        if min(
            self.exchange_timestamp_us,
            self.local_recv_ns,
            self.local_monotonic_ns,
            self.event_index,
        ) < 0:
            raise ValueError("recorded event timestamps/index cannot be negative")
        if self.sequence is not None and self.sequence < 0:
            raise ValueError("recorded event sequence cannot be negative")
        object.__setattr__(self, "symbol", self.symbol.upper())
        object.__setattr__(self, "raw_message", MappingProxyType(dict(self.raw_message)))
        object.__setattr__(self, "envelope", MappingProxyType(dict(self.envelope)))
        if self.parsed is not None:
            object.__setattr__(self, "parsed", MappingProxyType(dict(self.parsed)))

    @property
    def order_key(self) -> tuple[int, int, int, str]:
        """Canonical *availability* order declared by the research contract.

        Signals may only use messages after the local process received them.
        Exchange timestamps remain metadata and may legitimately regress across
        symbols/channels because publication and network delays differ.
        """

        return (
            self.local_recv_ns,
            self.local_monotonic_ns,
            self.event_index,
            self.event_id,
        )

    @classmethod
    def from_envelope(cls, envelope: Mapping[str, object]) -> RecordedEvent:
        if envelope.get("record_kind") != "exchange":
            raise ValueError("recorded event must be an exchange envelope")
        raw_text = envelope.get("raw_text")
        if not isinstance(raw_text, str):
            raise TypeError("recorded event is missing exact raw_text")
        message = json.loads(raw_text)
        if not isinstance(message, dict):
            raise TypeError("recorded event raw JSON must be an object")
        try:
            exchange_ts = int(envelope["exchange_timestamp_us"])
            local_recv_ns = int(envelope["local_recv_ns"])
            local_monotonic_ns = int(envelope["local_monotonic_ns"])
            event_index = int(envelope["event_index"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("recorded event has incomplete timestamps/index") from exc
        session = str(envelope.get("session_id") or "unknown_session")
        connection = str(envelope.get("connection_id") or "unknown_connection")
        symbol = str(
            envelope.get("symbol") or message.get("sy") or message.get("symbol") or ""
        ).split(":", 1)[-1]
        channel = str(envelope.get("channel") or message.get("type") or "")
        sequence_raw = envelope.get("sequence")
        checksum_raw = envelope.get("checksum")
        sequence = int(sequence_raw) if sequence_raw is not None else None
        event_type = {
            "ob_updates": "l2_update",
            "trades": "trade",
            "funding_rate": "funding",
            "liquidation": "liquidation",
            "liquidations": "liquidation",
        }.get(channel, channel)
        return cls(
            event_id=f"{session}:{connection}:{event_index:020d}",
            symbol=symbol,
            exchange_timestamp_us=exchange_ts,
            local_recv_ns=local_recv_ns,
            local_monotonic_ns=local_monotonic_ns,
            event_index=event_index,
            sequence=sequence,
            checksum=str(checksum_raw) if checksum_raw is not None else None,
            channel=channel,
            raw_message=message,
            event_type=event_type,
            envelope=envelope,
            parsed=message,
        )


class ReplayConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbols: tuple[str, ...]
    start_ts_us: int = Field(ge=0)
    end_ts_us: int = Field(gt=0)
    channels: tuple[str, ...] = ("trades", "ob_updates")
    speed_multiplier: float = Field(default=0.0, ge=0)
    enable_feature_engine: bool = True
    enable_scanner: bool = True
    journal_mode: ReplayJournalMode = "research"
    sealed_holdout: bool = False
    random_seed: int = 42
    signal_to_fill_latency_ms: int = Field(default=100, ge=0, le=60_000)
    code_version: str = Field(min_length=1)
    fail_on_integrity_error: bool = True

    @model_validator(mode="after")
    def validate_window(self) -> ReplayConfig:
        if self.end_ts_us <= self.start_ts_us:
            raise ValueError("replay end_ts_us must be after start_ts_us")
        symbols = tuple(str(symbol).upper().strip() for symbol in self.symbols)
        channels = tuple(str(channel).strip() for channel in self.channels)
        if not symbols or len(set(symbols)) != len(symbols) or any(not row for row in symbols):
            raise ValueError("replay symbols must be non-empty and unique")
        if not channels or len(set(channels)) != len(channels) or any(
            not row for row in channels
        ):
            raise ValueError("replay channels must be non-empty and unique")
        if self.enable_scanner and not self.enable_feature_engine:
            raise ValueError("scanner replay requires the feature engine")
        object.__setattr__(self, "symbols", symbols)
        object.__setattr__(self, "channels", channels)
        object.__setattr__(self, "code_version", self.code_version.strip())
        return self

    def canonical_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json")


@dataclass(frozen=True)
class RecordingValidationReport:
    passed: bool
    events: int
    book_events: int
    sequence_gaps: int
    checksum_failures: int
    duplicate_event_ids: int
    timestamp_regressions: int
    local_clock_regressions: int
    missing_exchange_timestamps: int
    clock_delay_percentiles_us: Mapping[str, int | None]
    negative_delay_samples: int
    channel_timestamp_regressions: Mapping[str, int]
    clock_delay_by_channel_us: Mapping[str, Mapping[str, int | None]]
    issues: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "clock_delay_percentiles_us",
            MappingProxyType(dict(self.clock_delay_percentiles_us)),
        )
        object.__setattr__(
            self,
            "channel_timestamp_regressions",
            MappingProxyType(dict(self.channel_timestamp_regressions)),
        )
        object.__setattr__(
            self,
            "clock_delay_by_channel_us",
            MappingProxyType(
                {
                    channel: MappingProxyType(dict(summary))
                    for channel, summary in self.clock_delay_by_channel_us.items()
                }
            ),
        )
        object.__setattr__(self, "issues", tuple(self.issues))

    def to_dict(self) -> dict[str, object]:
        return {
            **self.__dict__,
            "clock_delay_percentiles_us": dict(self.clock_delay_percentiles_us),
            "channel_timestamp_regressions": dict(
                self.channel_timestamp_regressions
            ),
            "clock_delay_by_channel_us": {
                channel: dict(summary)
                for channel, summary in self.clock_delay_by_channel_us.items()
            },
            "issues": list(self.issues),
        }


@dataclass(frozen=True)
class ReplayTick:
    event: RecordedEvent
    features: Mapping[str, object] | None
    candidates: tuple[SignalCandidate, ...]
    selected: SignalCandidate | None
    decision_latency_ns: int
    feature_latency_ns: int

    def __post_init__(self) -> None:
        if self.features is not None:
            object.__setattr__(self, "features", MappingProxyType(dict(self.features)))
        object.__setattr__(self, "candidates", tuple(self.candidates))
        if self.decision_latency_ns < 0 or self.feature_latency_ns < 0:
            raise ValueError("replay latency cannot be negative")


@dataclass(frozen=True)
class ReplayResult:
    config: ReplayConfig
    events_processed: int
    gaps_detected: int
    candidates_emitted: int
    journal_path: str | None
    summary_metrics: Mapping[str, object]
    latency_percentiles: Mapping[str, object]
    code_version: str
    deterministic_hash: str
    validation: RecordingValidationReport
    result_path: str | None = None
    research_only: bool = True
    can_trade: bool = False
    can_promote: bool = False

    def __post_init__(self) -> None:
        if not self.research_only or self.can_trade or self.can_promote:
            raise ValueError("event replay must remain research-only")
        object.__setattr__(self, "summary_metrics", MappingProxyType(dict(self.summary_metrics)))
        object.__setattr__(
            self,
            "latency_percentiles",
            MappingProxyType(dict(self.latency_percentiles)),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "vnedge.event_replay_result.v1",
            "config": self.config.canonical_dict(),
            "events_processed": self.events_processed,
            "gaps_detected": self.gaps_detected,
            "candidates_emitted": self.candidates_emitted,
            "journal_path": self.journal_path,
            "summary_metrics": dict(self.summary_metrics),
            "latency_percentiles": dict(self.latency_percentiles),
            "code_version": self.code_version,
            "deterministic_hash": self.deterministic_hash,
            "validation": self.validation.to_dict(),
            "result_path": self.result_path,
            "research_only": True,
            "can_trade": False,
            "can_promote": False,
            "order_route": "absent",
        }


@dataclass(frozen=True)
class ReplayWindow:
    name: str
    start_ts_us: int
    end_ts_us: int
    symbols: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name or self.start_ts_us < 0 or self.end_ts_us <= self.start_ts_us:
            raise ValueError("invalid replay manifest window")
        object.__setattr__(self, "symbols", tuple(row.upper() for row in self.symbols))

    def overlaps(self, config: ReplayConfig) -> bool:
        time_overlap = self.start_ts_us < config.end_ts_us and config.start_ts_us < self.end_ts_us
        symbol_overlap = not self.symbols or bool(set(self.symbols) & set(config.symbols))
        return time_overlap and symbol_overlap


@dataclass(frozen=True)
class HoldoutManifest:
    development_windows: tuple[ReplayWindow, ...] = ()
    sealed_windows: tuple[ReplayWindow, ...] = ()
    source: Path | None = None

    def guard(self, config: ReplayConfig) -> None:
        sealed_overlap = [row.name for row in self.sealed_windows if row.overlaps(config)]
        if not config.sealed_holdout:
            if sealed_overlap:
                raise PermissionError(
                    "requested window is sealed; set sealed_holdout=true for an explicit "
                    "evaluation: " + ", ".join(sealed_overlap)
                )
            return
        conflicts = [row.name for row in self.development_windows if row.overlaps(config)]
        if conflicts:
            raise PermissionError(
                "sealed replay overlaps development window(s): " + ", ".join(conflicts)
            )
        covered = any(
            row.start_ts_us <= config.start_ts_us
            and config.end_ts_us <= row.end_ts_us
            and (not row.symbols or set(config.symbols).issubset(row.symbols))
            for row in self.sealed_windows
        )
        if not covered:
            raise PermissionError("sealed replay window is not declared in the holdout manifest")
