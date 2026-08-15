"""Lossless Delta India public event recorder with book-integrity proofs.

This recorder uses only the new public websocket endpoint.  It archives exact
wire JSON and never imports order, account, credential, or risk-routing code.

The critical distinction from a generic websocket logger is that ``ob_updates``
is actively reconstructed in memory. Every snapshot/update must satisfy:

* an initial snapshot exists,
* sequence increments by exactly one,
* the CRC32 checksum matches Delta's documented top-ten book checksum.

Any failure is written to the control stream and forces a reconnect, which in
turn obtains a fresh snapshot. Finalized compressed shards have atomic SHA-256
manifests. Crash-interrupted ``.partial`` files are never silently treated as
complete research data.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import gzip
import hashlib
import inspect
import io
import json
import logging
import os
import re
import signal
import time
import zlib
from collections import Counter, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Literal

from vnedge.exchange.delta_public_schema import delta_message_timestamp

logger = logging.getLogger(__name__)

DELTA_PUBLIC_WS_URL = "wss://public-socket.india.delta.exchange"
EVENT_SCHEMA_VERSION = "vnedge.delta_public_event.v1"
MANIFEST_SCHEMA_VERSION = "vnedge.delta_event_shard_manifest.v1"
STATUS_SCHEMA_VERSION = "vnedge.delta_event_recorder_status.v1"
LATENCY_WINDOW_SIZE = 10_000
MAX_CREDIBLE_FEED_DELAY_US = 300_000_000
FEED_DELAY_SLA_US = 500_000
FEED_DELAY_STALE_BACKLOG_US = 5_000_000


def classify_corrected_feed_delay(delay_us: int | None) -> str:
    """Give source latency an explicit, non-promotional interpretation.

    The corrected delay is telemetry only and never changes replay ordering.
    Missing or outlier timestamps fail closed for event-time decisions.
    """

    if delay_us is None:
        return "UNKNOWN"
    if delay_us <= FEED_DELAY_SLA_US:
        return "ON_TIME"
    if delay_us <= FEED_DELAY_STALE_BACKLOG_US:
        return "DELAYED"
    return "STALE_BACKLOG"

PUBLIC_CHANNELS = (
    "trades",
    "ob_l2",
    "ob_updates",
    "ticker",
    "funding_rate",
    "spot_price",
    "mark_price",
    "system_status",
)
PUBLIC_CHANNEL_SET = frozenset(PUBLIC_CHANNELS)
DEFAULT_SYMBOLS = ("BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD", "AAVEUSD")

Compression = Literal["auto", "gzip", "zstd"]


class BookIntegrityError(RuntimeError):
    """A raw book event was archived but the local reconstruction is invalid."""

    def __init__(self, marker: str, details: Mapping[str, Any]) -> None:
        super().__init__(f"{marker}: {dict(details)}")
        self.marker = marker
        self.details = dict(details)


class RecorderDependencyError(RuntimeError):
    """An explicitly requested optional compression/runtime dependency is absent."""


class SubscriptionIntegrityError(RuntimeError):
    """Delta rejected part of the requested public market-data contract."""


class _SignedLatencyWindow:
    """Bounded wall-clock delay evidence, including clock-skew negatives."""

    def __init__(self, size: int = LATENCY_WINDOW_SIZE) -> None:
        self.values: deque[int] = deque(maxlen=size)

    def add(self, value: int | None) -> None:
        if value is not None:
            self.values.append(value)

    def summary(self) -> dict[str, int | None]:
        if not self.values:
            return {
                "count": 0,
                "p50_us": None,
                "p95_us": None,
                "p99_us": None,
                "min_us": None,
                "max_us": None,
                "negative_samples": 0,
            }
        rows = sorted(self.values)

        def percentile(value: float) -> int:
            return rows[min(len(rows) - 1, int((len(rows) - 1) * value))]

        return {
            "count": len(rows),
            "p50_us": percentile(0.50),
            "p95_us": percentile(0.95),
            "p99_us": percentile(0.99),
            "min_us": rows[0],
            "max_us": rows[-1],
            "negative_samples": sum(value < 0 for value in rows),
        }


class _ClockOffsetWindow:
    """Bounded, transparent lower-bound estimate of exchange clock skew.

    Network transit time cannot be negative, so the first percentile of the
    signed receive-minus-exchange sample is a conservative offset estimate.
    It is telemetry only: raw event timestamps and replay ordering are never
    rewritten with it.
    """

    def __init__(self, size: int = LATENCY_WINDOW_SIZE) -> None:
        self.values: deque[int] = deque(maxlen=size)

    def add(self, value: int) -> None:
        self.values.append(value)

    @property
    def estimated_offset_us(self) -> int | None:
        if not self.values:
            return None
        rows = sorted(self.values)
        lower = rows[min(len(rows) - 1, int((len(rows) - 1) * 0.01))]
        return min(0, lower)

    def corrected(self, value: int) -> int:
        return max(0, value - (self.estimated_offset_us or 0))


@dataclass(frozen=True)
class DeltaEventRecorderConfig:
    symbols: tuple[str, ...] = DEFAULT_SYMBOLS
    channels: tuple[str, ...] = PUBLIC_CHANNELS
    url: str = DELTA_PUBLIC_WS_URL
    output_dir: Path = Path("data/delta_events")
    rotate_seconds: int = 3600
    fsync_seconds: float = 5.0
    read_timeout_seconds: float = 40.0
    queue_max: int = 50_000
    batch_max: int = 1_000
    stats_seconds: float = 30.0
    max_backoff_seconds: float = 30.0
    compression: Compression = "auto"

    def __post_init__(self) -> None:
        normalized = tuple(_native_symbol(symbol) for symbol in self.symbols)
        if not normalized:
            raise ValueError("at least one symbol is required")
        if len(set(normalized)) != len(normalized):
            raise ValueError("symbols must be unique after normalization")
        if len(normalized) > 100:
            raise ValueError("Delta permits at most 100 symbols per public channel connection")
        unknown = sorted(set(self.channels) - PUBLIC_CHANNEL_SET)
        if unknown:
            raise ValueError(f"unsupported/non-public channels: {', '.join(unknown)}")
        if not self.channels:
            raise ValueError("at least one channel is required")
        if self.url != DELTA_PUBLIC_WS_URL:
            raise ValueError("event recorder must use Delta's public websocket endpoint")
        if self.rotate_seconds < 60:
            raise ValueError("rotate_seconds must be at least 60")
        if self.fsync_seconds <= 0 or self.read_timeout_seconds < 35:
            raise ValueError("fsync must be positive and read timeout must be >=35s")
        if self.queue_max < 100 or self.batch_max < 1 or self.batch_max > self.queue_max:
            raise ValueError("require queue_max>=100 and 1<=batch_max<=queue_max")
        if self.stats_seconds <= 0 or self.max_backoff_seconds <= 0:
            raise ValueError("stats_seconds and max_backoff_seconds must be positive")
        if self.compression not in {"auto", "gzip", "zstd"}:
            raise ValueError("compression must be auto, gzip, or zstd")
        object.__setattr__(self, "symbols", normalized)
        object.__setattr__(self, "output_dir", Path(self.output_dir))

    def subscriptions(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for channel in self.channels:
            if channel == "system_status":
                rows.append({"name": channel})
                continue
            # Delta's public endpoint rejects ob_l2 when one subscription row
            # contains more than one symbol. Other channels accept the batch.
            if channel == "ob_l2":
                rows.extend({"name": channel, "symbols": [symbol]} for symbol in self.symbols)
                continue
            symbols = (
                [f"MARK:{symbol}" for symbol in self.symbols]
                if channel == "mark_price"
                else list(self.symbols)
            )
            rows.append({"name": channel, "symbols": symbols})
        return rows


@dataclass
class _BookState:
    asks: dict[Decimal, tuple[str, str]] = field(default_factory=dict)
    bids: dict[Decimal, tuple[str, str]] = field(default_factory=dict)
    last_sequence: int | None = None
    valid: bool = False


class DeltaBookIntegrityValidator:
    """Deterministically rebuild and checksum each ``ob_updates`` book."""

    def __init__(self) -> None:
        self._states: dict[str, _BookState] = {}

    def reset(self) -> None:
        self._states.clear()

    @property
    def valid_symbols(self) -> tuple[str, ...]:
        """Symbols backed by a checksum-valid snapshot in the active connection."""

        return tuple(
            sorted(symbol for symbol, state in self._states.items() if state.valid)
        )

    def observe(self, message: Mapping[str, Any]) -> dict[str, Any]:
        symbol = _message_symbol(message)
        if not symbol:
            raise BookIntegrityError("__ob_protocol_error__", {"reason": "missing symbol"})
        action = message.get("action")
        if action == "error":
            self._states.pop(symbol, None)
            raise BookIntegrityError(
                "__ob_exchange_error__",
                {"symbol": symbol, "message": str(message.get("msg") or "snapshot error")},
            )
        sequence = _integer_field(message, "seq")
        checksum = _integer_field(message, "cs")
        if action == "snapshot":
            state = _BookState()
            state.asks = _snapshot_side(message.get("a"), side="asks")
            state.bids = _snapshot_side(message.get("b"), side="bids")
            state.last_sequence = sequence
            state.valid = True
            self._states[symbol] = state
        elif action == "update":
            current = self._states.get(symbol)
            if current is None or not current.valid:
                raise BookIntegrityError(
                    "__ob_update_without_snapshot__",
                    {"symbol": symbol, "got": sequence},
                )
            previous_sequence = current.last_sequence
            if previous_sequence is None:
                raise BookIntegrityError(
                    "__ob_update_without_snapshot__",
                    {"symbol": symbol, "got": sequence},
                )
            state = current
            expected = previous_sequence + 1
            if sequence != expected:
                self._states.pop(symbol, None)
                missed = sequence - expected if sequence > expected else None
                raise BookIntegrityError(
                    "__ob_sequence_gap__",
                    {
                        "symbol": symbol,
                        "expected": expected,
                        "got": sequence,
                        "missed": missed,
                        "kind": "forward_gap" if sequence > expected else "reset_or_rollback",
                    },
                )
            _apply_updates(state.asks, message.get("a"), side="asks")
            _apply_updates(state.bids, message.get("b"), side="bids")
            state.last_sequence = sequence
        else:
            self._states.pop(symbol, None)
            raise BookIntegrityError(
                "__ob_protocol_error__",
                {"symbol": symbol, "reason": f"invalid action {action!r}"},
            )

        computed, checksum_text = _book_checksum(state)
        if computed != checksum:
            self._states.pop(symbol, None)
            raise BookIntegrityError(
                "__ob_checksum_mismatch__",
                {
                    "symbol": symbol,
                    "sequence": sequence,
                    "provided": checksum,
                    "computed": computed,
                    "checksum_text_sha256": hashlib.sha256(
                        checksum_text.encode("utf-8")
                    ).hexdigest(),
                },
            )
        return {
            "symbol": symbol,
            "action": action,
            "sequence": sequence,
            "checksum": checksum,
            "asks": len(state.asks),
            "bids": len(state.bids),
            "healthy": True,
        }


@dataclass(frozen=True)
class ShardVerification:
    path: str
    passed: bool
    records: int
    issues: tuple[str, ...]
    compressed_sha256: str
    uncompressed_sha256: str


@dataclass(frozen=True)
class EventTreeVerification:
    root: str
    passed: bool
    storage_passed: bool
    continuity_passed: bool
    shards: int
    records: int
    failed_shards: tuple[str, ...]
    partial_files: tuple[str, ...]
    orphan_manifests: tuple[str, ...]
    integrity_faults: tuple[str, ...]
    issues: tuple[str, ...]


class _CompressedShardSink:
    """One crash-visible partial file which becomes immutable on close."""

    def __init__(
        self,
        *,
        directory: Path,
        channel: str,
        bucket_start: datetime,
        session_id: str,
        part: int,
        compression: Compression,
    ) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.channel = channel
        self.session_id = session_id
        self.compression = _resolve_compression(compression)
        stamp = bucket_start.strftime("%Y-%m-%d_%H")
        extension = "jsonl.zst" if self.compression == "zstd" else "jsonl.gz"
        name = f"{channel}_{stamp}_{session_id}_{part:06d}.{extension}"
        self.final_path = directory / name
        self.partial_path = directory / f".{name}.partial"
        self.manifest_path = directory / f"{name}.manifest.json"
        collisions = [
            path.name
            for path in (self.final_path, self.partial_path, self.manifest_path)
            if path.exists()
        ]
        if collisions:
            raise FileExistsError(
                "refusing to overwrite existing event-shard artifacts: " + ", ".join(collisions)
            )
        self._raw = self.partial_path.open("xb")
        if self.compression == "zstd":
            zstd = _import_zstandard()
            self._writer = zstd.ZstdCompressor(level=3).stream_writer(self._raw, closefd=False)
        else:
            self._writer = gzip.GzipFile(filename="", fileobj=self._raw, mode="wb", mtime=0)
        self._uncompressed_hash = hashlib.sha256()
        self.records = 0
        self.first_local_recv_ns: int | None = None
        self.last_local_recv_ns: int | None = None
        self.closed = False

    def write(self, line: bytes, local_recv_ns: int) -> None:
        if self.closed:
            raise RuntimeError("cannot write closed event shard")
        self._writer.write(line)
        self._uncompressed_hash.update(line)
        self.records += 1
        if self.first_local_recv_ns is None:
            self.first_local_recv_ns = local_recv_ns
        self.last_local_recv_ns = local_recv_ns

    def flush_durable(self) -> None:
        if self.closed:
            return
        if self.compression == "zstd":
            zstd = _import_zstandard()
            self._writer.flush(zstd.FLUSH_BLOCK)
        else:
            self._writer.flush()
        self._raw.flush()
        os.fsync(self._raw.fileno())

    def close(self) -> Path:
        if self.closed:
            return self.final_path
        try:
            self._writer.close()
            self._raw.flush()
            os.fsync(self._raw.fileno())
        finally:
            self._raw.close()
        self.partial_path.replace(self.final_path)
        _fsync_directory(self.final_path.parent)
        compressed_hash = _sha256_file(self.final_path)
        manifest = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "path": self.final_path.name,
            "channel": self.channel,
            "session_id": self.session_id,
            "compression": self.compression,
            "records": self.records,
            "first_local_recv_ns": self.first_local_recv_ns,
            "last_local_recv_ns": self.last_local_recv_ns,
            "compressed_bytes": self.final_path.stat().st_size,
            "compressed_sha256": compressed_hash,
            "uncompressed_sha256": self._uncompressed_hash.hexdigest(),
            "finalized_at": datetime.now(UTC).isoformat(),
            "research_only": True,
            "can_trade": False,
            "can_promote": False,
        }
        _atomic_json(self.manifest_path, manifest)
        self.closed = True
        return self.final_path


class RotatingEventWriter:
    """Single-writer owner of all hourly channel shards."""

    def __init__(
        self,
        config: DeltaEventRecorderConfig,
        *,
        session_id: str,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.session_id = _safe_id(session_id)
        self._monotonic_clock = monotonic_clock
        self._sinks: dict[str, tuple[int, _CompressedShardSink]] = {}
        self._parts: Counter[str] = Counter()
        self._last_fsync = monotonic_clock()
        self.finalized: list[Path] = []

    def _sink(self, channel: str, local_recv_ns: int) -> _CompressedShardSink:
        seconds = local_recv_ns // 1_000_000_000
        bucket = int(seconds // self.config.rotate_seconds)
        current = self._sinks.get(channel)
        if current is not None and current[0] == bucket:
            return current[1]
        if current is not None:
            self.finalized.append(current[1].close())
        instant = datetime.fromtimestamp(bucket * self.config.rotate_seconds, tz=UTC)
        day = instant.strftime("%Y-%m-%d")
        directory = self.config.output_dir / day / channel
        part = self._parts[channel]
        self._parts[channel] += 1
        sink = _CompressedShardSink(
            directory=directory,
            channel=channel,
            bucket_start=instant,
            session_id=self.session_id,
            part=part,
            compression=self.config.compression,
        )
        self._sinks[channel] = (bucket, sink)
        return sink

    def write_batch(self, batch: list[tuple[str, dict[str, Any]]]) -> None:
        for channel, envelope in batch:
            local_recv_ns = int(envelope["local_recv_ns"])
            line = (
                json.dumps(
                    envelope,
                    separators=(",", ":"),
                    sort_keys=True,
                    ensure_ascii=True,
                    allow_nan=False,
                ).encode("utf-8")
                + b"\n"
            )
            self._sink(channel, local_recv_ns).write(line, local_recv_ns)
        now = self._monotonic_clock()
        if now - self._last_fsync >= self.config.fsync_seconds:
            for _, sink in self._sinks.values():
                sink.flush_durable()
            self._last_fsync = now

    def close(self) -> tuple[Path, ...]:
        for _, sink in list(self._sinks.values()):
            self.finalized.append(sink.close())
        self._sinks.clear()
        return tuple(self.finalized)


class DeltaEventRecorder:
    """Async public-feed recorder; all filesystem writes have one owner."""

    def __init__(
        self,
        config: DeltaEventRecorderConfig | None = None,
        *,
        connect: Callable[[str], Any] | None = None,
        wall_clock_ns: Callable[[], int] = time.time_ns,
        monotonic_clock_ns: Callable[[], int] = time.monotonic_ns,
        session_id: str | None = None,
        verified_event_observer: Callable[[Mapping[str, Any]], object] | None = None,
    ) -> None:
        config = config or DeltaEventRecorderConfig()
        self.config = config
        self.session_id = _safe_id(session_id or _new_session_id())
        self._connect = connect or _default_connect
        self._wall_clock_ns = wall_clock_ns
        self._monotonic_clock_ns = monotonic_clock_ns
        self._verified_event_observer = verified_event_observer
        self._queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue(
            maxsize=config.queue_max
        )
        self._writer = RotatingEventWriter(config, session_id=self.session_id)
        self._book = DeltaBookIntegrityValidator()
        self._stop = asyncio.Event()
        self.counts: Counter[str] = Counter()
        self.control_markers: Counter[str] = Counter()
        self.connection_count = 0
        self.queue_high_water = 0
        self._event_index = 0
        self.started_at = datetime.now(UTC)
        self.last_wire_recv_ns: int | None = None
        self.last_feed_delay_us: int | None = None
        self.last_raw_feed_delay_us: int | None = None
        self.feed_delay_us = _SignedLatencyWindow()
        self.corrected_feed_delay_us = _SignedLatencyWindow()
        self.feed_delay_by_channel: dict[str, _SignedLatencyWindow] = {}
        self.corrected_feed_delay_by_channel: dict[str, _SignedLatencyWindow] = {}
        self.clock_offset_by_channel: dict[str, _ClockOffsetWindow] = {}
        self.feed_timestamp_outliers: Counter[str] = Counter()
        self.feed_timestamp_missing: Counter[str] = Counter()
        self.feed_timestamp_units: Counter[str] = Counter()
        self.feed_delay_classifications: Counter[str] = Counter()
        self.connected = False
        self.active_connection_id: str | None = None
        self.connected_since: str | None = None
        self.disconnect_count = 0
        self.reconnect_count = 0
        self.disconnect_reasons: Counter[str] = Counter()
        self.last_disconnect: dict[str, Any] | None = None

    def request_stop(self) -> None:
        self._stop.set()

    async def _put(self, channel: str, envelope: dict[str, Any]) -> None:
        await self._queue.put((channel, envelope))
        self.queue_high_water = max(self.queue_high_water, self._queue.qsize())
        self.counts[channel] += 1

    def _base_envelope(self, *, connection_id: str) -> dict[str, Any]:
        self._event_index += 1
        return {
            "schema_version": EVENT_SCHEMA_VERSION,
            "session_id": self.session_id,
            "connection_id": connection_id,
            "event_index": self._event_index,
            "local_recv_ns": self._wall_clock_ns(),
            "local_monotonic_ns": self._monotonic_clock_ns(),
            "research_only": True,
            "can_trade": False,
            "can_promote": False,
        }

    async def _control(
        self,
        marker: str,
        details: Mapping[str, Any],
        *,
        connection_id: str,
    ) -> None:
        self.control_markers[marker] += 1
        envelope = self._base_envelope(connection_id=connection_id)
        envelope.update(
            {
                "record_kind": "control",
                "marker": marker,
                "details": dict(details),
            }
        )
        await self._put("_control", envelope)

    async def _handle_wire(self, raw: str | bytes, *, connection_id: str) -> None:
        received = self._base_envelope(connection_id=connection_id)
        self.last_wire_recv_ns = int(received["local_recv_ns"])
        if isinstance(raw, bytes):
            try:
                raw_text = raw.decode("utf-8")
            except UnicodeDecodeError:
                received.update(
                    {
                        "record_kind": "wire_error",
                        "marker": "__unparseable_binary__",
                        "raw_base64": base64.b64encode(raw).decode("ascii"),
                    }
                )
                await self._put("_wire_error", received)
                return
        else:
            raw_text = raw
        try:
            message = json.loads(raw_text)
        except (json.JSONDecodeError, TypeError):
            received.update(
                {
                    "record_kind": "wire_error",
                    "marker": "__unparseable_json__",
                    "raw_text": raw_text,
                }
            )
            await self._put("_wire_error", received)
            return
        if not isinstance(message, dict):
            received.update(
                {
                    "record_kind": "wire_error",
                    "marker": "__unexpected_json_shape__",
                    "raw_text": raw_text,
                }
            )
            await self._put("_wire_error", received)
            return
        channel = str(message.get("type") or "_unknown")
        symbol = _message_symbol(message)
        event_timestamp = delta_message_timestamp(message, channel=channel)
        publish_timestamp = delta_message_timestamp(message, channel=channel, publish=True)
        envelope = received
        envelope.update(
            {
                "record_kind": "exchange",
                "channel": channel,
                "symbol": symbol,
                "exchange_timestamp_us": event_timestamp.value_us,
                "publish_timestamp_us": publish_timestamp.value_us,
                "exchange_timestamp_raw": event_timestamp.raw_value,
                "exchange_timestamp_source": event_timestamp.source_key,
                "exchange_timestamp_unit": event_timestamp.source_unit,
                "publish_timestamp_raw": publish_timestamp.raw_value,
                "publish_timestamp_source": publish_timestamp.source_key,
                "publish_timestamp_unit": publish_timestamp.source_unit,
                "action": message.get("action"),
                "sequence": message.get("seq"),
                "checksum": message.get("cs"),
                "raw_text": raw_text,
            }
        )
        latency_timestamp_us = envelope["publish_timestamp_us"] or envelope[
            "exchange_timestamp_us"
        ]
        latency_reference = (
            "publish_timestamp" if envelope["publish_timestamp_us"] is not None else "event_timestamp"
        )
        envelope["latency_timestamp_source"] = latency_reference
        if event_timestamp.source_unit:
            self.feed_timestamp_units[f"{channel}:{event_timestamp.source_unit}"] += 1
        if latency_timestamp_us is not None:
            raw_feed_delay_us = (
                int(envelope["local_recv_ns"]) // 1_000 - int(latency_timestamp_us)
            )
            envelope["raw_feed_delay_us"] = raw_feed_delay_us
            self.last_raw_feed_delay_us = raw_feed_delay_us
            if abs(raw_feed_delay_us) <= MAX_CREDIBLE_FEED_DELAY_US:
                self.last_feed_delay_us = raw_feed_delay_us
                self.feed_delay_us.add(raw_feed_delay_us)
                raw_window = self.feed_delay_by_channel.setdefault(
                    channel, _SignedLatencyWindow()
                )
                raw_window.add(raw_feed_delay_us)
                offset = self.clock_offset_by_channel.setdefault(
                    channel, _ClockOffsetWindow()
                )
                offset.add(raw_feed_delay_us)
                corrected = offset.corrected(raw_feed_delay_us)
                envelope["clock_offset_estimate_us"] = offset.estimated_offset_us
                envelope["corrected_feed_delay_us"] = corrected
                delay_classification = classify_corrected_feed_delay(corrected)
                envelope["feed_delay_classification"] = delay_classification
                envelope["decision_eligible"] = delay_classification == "ON_TIME"
                self.feed_delay_classifications[
                    f"{channel}:{delay_classification}"
                ] += 1
                self.corrected_feed_delay_us.add(corrected)
                self.corrected_feed_delay_by_channel.setdefault(
                    channel, _SignedLatencyWindow()
                ).add(corrected)
            else:
                self.feed_timestamp_outliers[channel] += 1
                envelope["feed_timestamp_outlier"] = True
                envelope["feed_delay_classification"] = "UNKNOWN"
                envelope["decision_eligible"] = False
                self.feed_delay_classifications[f"{channel}:UNKNOWN"] += 1
        else:
            self.feed_timestamp_missing[channel] += 1
            envelope["feed_delay_classification"] = "UNKNOWN"
            envelope["decision_eligible"] = False
            self.feed_delay_classifications[f"{channel}:UNKNOWN"] += 1
        # Archive the exact wire message before testing integrity. A bad delta
        # is evidence too, but it is never applied beyond this point.
        await self._put(channel, envelope)
        if channel == "subscriptions":
            acknowledgements = message.get("channels")
            errors = (
                [
                    dict(row)
                    for row in acknowledgements
                    if isinstance(row, dict) and row.get("error")
                ]
                if isinstance(acknowledgements, list)
                else [{"error": "malformed subscription acknowledgement"}]
            )
            if errors:
                await self._control(
                    "__subscription_error__",
                    {"errors": errors},
                    connection_id=connection_id,
                )
                raise SubscriptionIntegrityError(str(errors))
        if channel == "ob_updates":
            try:
                health = self._book.observe(message)
                if health["action"] == "snapshot":
                    await self._control(
                        "__ob_snapshot_valid__", health, connection_id=connection_id
                    )
            except BookIntegrityError as exc:
                await self._control(exc.marker, exc.details, connection_id=connection_id)
                raise
        await self._notify_verified_event(envelope, connection_id=connection_id)

    async def _notify_verified_event(
        self,
        envelope: Mapping[str, Any],
        *,
        connection_id: str,
    ) -> None:
        """Fan out only parseable, book-verified events to research observers.

        Recorder durability always wins: an observer failure is captured as a
        control event and cannot interrupt the lossless recording connection.
        """

        observer = self._verified_event_observer
        if observer is None:
            return
        try:
            result = observer(envelope)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:  # noqa: BLE001 - observer failures are evidence
            self.counts["_research_observer_error"] += 1
            await self._control(
                "__research_observer_error__",
                {"error_type": type(exc).__name__, "error": str(exc)[:1_000]},
                connection_id=connection_id,
            )

    async def _session(self, *, connection_id: str) -> None:
        self._book.reset()
        async with self._connect(self.config.url) as websocket:
            await websocket.send(json.dumps({"type": "enable_heartbeat"}))
            await websocket.send(
                json.dumps(
                    {
                        "type": "subscribe",
                        "payload": {"channels": self.config.subscriptions()},
                    }
                )
            )
            await self._control(
                "__connected__",
                {"url": self.config.url, "subscriptions": self.config.subscriptions()},
                connection_id=connection_id,
            )
            self.connected = True
            self.active_connection_id = connection_id
            self.connected_since = datetime.now(UTC).isoformat()
            while not self._stop.is_set():
                receive_task = asyncio.create_task(websocket.recv())
                stop_task = asyncio.create_task(self._stop.wait())
                done, pending = await asyncio.wait(
                    {receive_task, stop_task},
                    timeout=self.config.read_timeout_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    await self._control(
                        "__read_timeout__",
                        {"seconds": self.config.read_timeout_seconds},
                        connection_id=connection_id,
                    )
                    raise RuntimeError("Delta public websocket read timeout")
                if receive_task in done:
                    stop_task.cancel()
                    await asyncio.gather(stop_task, return_exceptions=True)
                    raw = receive_task.result()
                    await self._handle_wire(raw, connection_id=connection_id)
                    continue
                if stop_task in done:
                    receive_task.cancel()
                    await asyncio.gather(receive_task, return_exceptions=True)
                    break

    async def _connection_loop(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            self.connection_count += 1
            if self.connection_count > 1:
                self.reconnect_count += 1
            connection_id = f"conn_{self.connection_count:06d}"
            started = time.monotonic()
            try:
                await self._session(connection_id=connection_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - every transport fault is evidence
                reason = type(exc).__name__
                self.disconnect_count += 1
                self.disconnect_reasons[reason] += 1
                self.last_disconnect = {
                    "connection_id": connection_id,
                    "error_type": reason,
                    "error": str(exc)[:1_000],
                    "at": datetime.now(UTC).isoformat(),
                }
                await self._control(
                    "__disconnected__",
                    {"error_type": type(exc).__name__, "error": str(exc)[:1_000]},
                    connection_id=connection_id,
                )
            else:
                reason = "clean"
                self.disconnect_count += 1
                self.disconnect_reasons[reason] += 1
                self.last_disconnect = {
                    "connection_id": connection_id,
                    "error_type": reason,
                    "at": datetime.now(UTC).isoformat(),
                }
                await self._control(
                    "__disconnected__", {"error_type": "clean"}, connection_id=connection_id
                )
            self.connected = False
            self.active_connection_id = None
            self.connected_since = None
            if self._stop.is_set():
                break
            healthy_duration = time.monotonic() - started
            backoff = (
                1.0
                if healthy_duration >= 60
                else min(backoff * 2.0, self.config.max_backoff_seconds)
            )
            await self._control(
                "__reconnect_wait__", {"seconds": backoff}, connection_id=connection_id
            )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            except TimeoutError:
                pass

    async def _writer_loop(self) -> None:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            batch = [item]
            while len(batch) < self.config.batch_max:
                try:
                    next_item = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if next_item is None:
                    await asyncio.to_thread(self._writer.write_batch, batch)
                    return
                batch.append(next_item)
            await asyncio.to_thread(self._writer.write_batch, batch)

    async def _stats_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.config.stats_seconds)
            except TimeoutError:
                snapshot = {
                    "counts": dict(self.counts),
                    "queue_depth": self._queue.qsize(),
                    "queue_high_water": self.queue_high_water,
                    "connections": self.connection_count,
                }
                logger.info("Delta event recorder stats: %s", snapshot)
                await self._control(
                    "__stats__",
                    snapshot,
                    connection_id=f"conn_{self.connection_count:06d}",
                )
                await self._publish_status("RECORDING")

    def _status_payload(self, state: str) -> dict[str, Any]:
        integrity_faults = sum(
            count
            for marker, count in self.control_markers.items()
            if marker in _INTEGRITY_FAULT_MARKERS
        )
        required_book_symbols = (
            set(self.config.symbols) if "ob_updates" in self.config.channels else set()
        )
        valid_book_symbols = set(self._book.valid_symbols)
        active_books_ready = required_book_symbols.issubset(valid_book_symbols)
        active_integrity_fault = integrity_faults > 0 and not active_books_ready
        return {
            "schema_version": STATUS_SCHEMA_VERSION,
            "state": state,
            "session_id": self.session_id,
            "pid": os.getpid(),
            "started_at": self.started_at.isoformat(),
            "updated_at": datetime.now(UTC).isoformat(),
            "last_wire_recv_ns": self.last_wire_recv_ns,
            "symbols": list(self.config.symbols),
            "channels": list(self.config.channels),
            "rotate_seconds": self.config.rotate_seconds,
            "stats_seconds": self.config.stats_seconds,
            "counts": dict(self.counts),
            "events": sum(self.counts.values()),
            "connections": self.connection_count,
            "connection": {
                "connected": self.connected,
                "active_connection_id": self.active_connection_id,
                "connected_since": self.connected_since,
                "attempts": self.connection_count,
                "reconnects": self.reconnect_count,
                "disconnects": self.disconnect_count,
                "disconnect_reasons": dict(self.disconnect_reasons),
                "last_disconnect": self.last_disconnect,
            },
            "queue_depth": self._queue.qsize(),
            "queue_high_water": self.queue_high_water,
            "feed_delay_us": self.feed_delay_us.summary(),
            "feed_delay_corrected_us": self.corrected_feed_delay_us.summary(),
            "feed_delay_by_channel": {
                channel: window.summary()
                for channel, window in sorted(self.feed_delay_by_channel.items())
            },
            "feed_delay_corrected_by_channel": {
                channel: window.summary()
                for channel, window in sorted(
                    self.corrected_feed_delay_by_channel.items()
                )
            },
            "last_feed_delay_us": self.last_feed_delay_us,
            "feed_timestamp_quality": {
                "maximum_credible_absolute_delay_us": MAX_CREDIBLE_FEED_DELAY_US,
                "outlier_samples": sum(self.feed_timestamp_outliers.values()),
                "outliers_by_channel": dict(self.feed_timestamp_outliers),
                "missing_by_channel": dict(self.feed_timestamp_missing),
                "units_by_channel": dict(self.feed_timestamp_units),
                "estimated_clock_offset_us_by_channel": {
                    channel: window.estimated_offset_us
                    for channel, window in sorted(self.clock_offset_by_channel.items())
                },
                "latency_reference": "publish timestamp when present, otherwise event timestamp",
                "raw_timestamps_preserved": True,
                "clock_offset_applied_to_replay_order": False,
                "last_raw_delay_us": self.last_raw_feed_delay_us,
                "negative_samples": self.feed_delay_us.summary()["negative_samples"],
                "decision_latency_sla_us": FEED_DELAY_SLA_US,
                "stale_backlog_threshold_us": FEED_DELAY_STALE_BACKLOG_US,
                "classification_counts": dict(self.feed_delay_classifications),
                "decision_policy": (
                    "state may update from verified delayed events; candidate evaluation "
                    "requires ON_TIME source latency"
                ),
            },
            "gap_guard": {
                # Historical faults are permanent evidence. Operational
                # health may recover only after the reconnect supplies fresh,
                # checksum-valid snapshots for every required symbol.
                "integrity_faults": integrity_faults,
                "historical_integrity_faults": integrity_faults,
                "markers": dict(self.control_markers),
                "healthy": not active_integrity_fault,
                "active_fault": active_integrity_fault,
                "recovered": integrity_faults > 0 and active_books_ready,
                "required_book_symbols": sorted(required_book_symbols),
                "valid_book_symbols": sorted(valid_book_symbols),
                "recovery_rule": (
                    "fresh checksum-valid snapshots for every required symbol"
                ),
            },
            "research_only": True,
            "can_trade": False,
            "can_promote": False,
        }

    async def _publish_status(self, state: str) -> None:
        payload = self._status_payload(state)
        await asyncio.to_thread(
            _atomic_json,
            self.config.output_dir / "_recorder_status.json",
            payload,
        )

    async def run(self) -> dict[str, Any]:
        await self._publish_status("RECORDING")
        writer_task = asyncio.create_task(self._writer_loop(), name="delta-event-writer")
        connection_task = asyncio.create_task(
            self._connection_loop(), name="delta-event-connection"
        )
        stats_task = asyncio.create_task(self._stats_loop(), name="delta-event-stats")
        cancelled = False
        finalized: tuple[Path, ...] = ()
        try:
            done, _ = await asyncio.wait(
                {writer_task, connection_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if writer_task in done and not connection_task.done():
                error = writer_task.exception()
                self._stop.set()
                connection_task.cancel()
                await asyncio.gather(connection_task, return_exceptions=True)
                raise RuntimeError("Delta event writer stopped unexpectedly") from error
            if connection_task in done and not self._stop.is_set():
                error = connection_task.exception()
                raise RuntimeError("Delta event connection loop stopped unexpectedly") from error
            # Normal completion is driven by request_stop(). Drain everything.
            await self._control(
                "__shutdown__",
                {"counts": dict(self.counts), "queue_high_water": self.queue_high_water},
                connection_id=f"conn_{self.connection_count:06d}",
            )
            await self._queue.put(None)
            await writer_task
        except asyncio.CancelledError:
            cancelled = True
            self._stop.set()
        finally:
            if not connection_task.done():
                connection_task.cancel()
                await asyncio.gather(connection_task, return_exceptions=True)
            if not writer_task.done():
                await self._queue.put(None)
                await asyncio.shield(writer_task)
            if not stats_task.done():
                stats_task.cancel()
                await asyncio.gather(stats_task, return_exceptions=True)
            finalized = await asyncio.to_thread(self._writer.close)
            await asyncio.shield(self._publish_status("STOPPED"))
        if cancelled:
            raise asyncio.CancelledError
        return {
            "session_id": self.session_id,
            "events": sum(self.counts.values()),
            "counts": dict(self.counts),
            "connections": self.connection_count,
            "queue_high_water": self.queue_high_water,
            "finalized_shards": [str(path) for path in finalized],
            "can_trade": False,
            "can_promote": False,
            "research_only": True,
        }


def verify_event_shard(path: Path | str) -> ShardVerification:
    shard = Path(path)
    manifest_path = Path(str(shard) + ".manifest.json")
    issues: list[str] = []
    if not shard.is_file():
        return ShardVerification(str(shard), False, 0, ("shard missing",), "", "")
    if not manifest_path.is_file():
        return ShardVerification(
            str(shard), False, 0, ("manifest missing; shard may be partial",), "", ""
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    compressed_hash = _sha256_file(shard)
    if compressed_hash != manifest.get("compressed_sha256"):
        issues.append("compressed SHA-256 mismatch")
    uncompressed = hashlib.sha256()
    records = 0
    try:
        with _open_compressed_reader(shard) as reader:
            for line in reader:
                uncompressed.update(line)
                payload = json.loads(line)
                if payload.get("schema_version") != EVENT_SCHEMA_VERSION:
                    issues.append(f"record {records}: schema mismatch")
                records += 1
    except Exception as exc:  # noqa: BLE001 - verifier must return failures, never crash
        issues.append(f"cannot decode shard: {type(exc).__name__}: {exc}")
    uncompressed_hash = uncompressed.hexdigest()
    if records != manifest.get("records"):
        issues.append(f"record count mismatch: manifest={manifest.get('records')} actual={records}")
    if uncompressed_hash != manifest.get("uncompressed_sha256"):
        issues.append("uncompressed SHA-256 mismatch")
    return ShardVerification(
        path=str(shard),
        passed=not issues,
        records=records,
        issues=tuple(issues),
        compressed_sha256=compressed_hash,
        uncompressed_sha256=uncompressed_hash,
    )


def verify_event_tree(root: Path | str) -> EventTreeVerification:
    directory = Path(root)
    issues: list[str] = []
    shards = (
        sorted(
            {
                *directory.rglob("*.jsonl.gz"),
                *directory.rglob("*.jsonl.zst"),
            }
        )
        if directory.is_dir()
        else []
    )
    partials = (
        tuple(str(path) for path in sorted(directory.rglob("*.partial")))
        if directory.is_dir()
        else ()
    )
    manifests = sorted(directory.rglob("*.manifest.json")) if directory.is_dir() else []
    orphan_manifests = tuple(
        str(path) for path in manifests if not Path(str(path)[: -len(".manifest.json")]).is_file()
    )
    if not directory.is_dir():
        issues.append("event root missing or not a directory")
    if not shards:
        issues.append("no finalized event shards found")
    if partials:
        issues.append(f"{len(partials)} crash-partial file(s) present")
    if orphan_manifests:
        issues.append(f"{len(orphan_manifests)} orphan manifest(s) present")

    results = [verify_event_shard(path) for path in shards]
    failed = tuple(result.path for result in results if not result.passed)
    if failed:
        issues.append(f"{len(failed)} finalized shard(s) failed verification")
    storage_passed = not issues
    integrity_faults = _scan_integrity_faults(shards)
    if integrity_faults:
        issues.append(f"{len(integrity_faults)} recorded feed-integrity fault(s)")
    continuity_passed = not integrity_faults
    return EventTreeVerification(
        root=str(directory),
        passed=not issues,
        storage_passed=storage_passed,
        continuity_passed=continuity_passed,
        shards=len(results),
        records=sum(result.records for result in results),
        failed_shards=failed,
        partial_files=partials,
        orphan_manifests=orphan_manifests,
        integrity_faults=integrity_faults,
        issues=tuple(issues),
    )


_INTEGRITY_FAULT_MARKERS = frozenset(
    {
        "__ob_exchange_error__",
        "__ob_protocol_error__",
        "__ob_update_without_snapshot__",
        "__ob_sequence_gap__",
        "__ob_checksum_mismatch__",
        "__subscription_error__",
        "__read_timeout__",
    }
)


def _scan_integrity_faults(shards: list[Path]) -> tuple[str, ...]:
    faults: list[str] = []
    fault_connections: set[tuple[str, str]] = set()
    unexplained_disconnects: list[tuple[tuple[str, str], str]] = []
    for shard in shards:
        if shard.parent.name not in {"_control", "_wire_error"}:
            continue
        try:
            with _open_compressed_reader(shard) as reader:
                for line in reader:
                    payload = json.loads(line)
                    marker = payload.get("marker")
                    connection_key = (
                        str(payload.get("session_id", "unknown_session")),
                        str(payload.get("connection_id", "unknown_connection")),
                    )
                    disconnected = marker == "__disconnected__" and (
                        payload.get("details", {}).get("error_type") != "clean"
                    )
                    label = ":".join(
                        str(value)
                        for value in (
                            *connection_key,
                            payload.get("event_index", "unknown_event"),
                            marker or "__wire_error__",
                        )
                    )
                    if payload.get("record_kind") == "wire_error" or (
                        marker in _INTEGRITY_FAULT_MARKERS
                    ):
                        faults.append(label)
                        fault_connections.add(connection_key)
                    elif disconnected:
                        unexplained_disconnects.append((connection_key, label))
        except Exception as exc:  # noqa: BLE001 - surfaced as a failed audit record
            faults.append(f"{shard}:semantic_scan_failed:{type(exc).__name__}:{exc}")
    faults.extend(
        label
        for connection_key, label in unexplained_disconnects
        if connection_key not in fault_connections
    )
    return tuple(faults)


def _snapshot_side(raw: Any, *, side: str) -> dict[Decimal, tuple[str, str]]:
    book: dict[Decimal, tuple[str, str]] = {}
    _apply_updates(book, raw, side=side)
    return book


def _apply_updates(book: dict[Decimal, tuple[str, str]], raw: Any, *, side: str) -> None:
    if not isinstance(raw, list):
        raise BookIntegrityError("__ob_protocol_error__", {"reason": f"{side} not a list"})
    for index, level in enumerate(raw):
        if not isinstance(level, list) or len(level) != 2:
            raise BookIntegrityError(
                "__ob_protocol_error__", {"reason": f"invalid {side} level {index}"}
            )
        price_text, size_text = str(level[0]), str(level[1])
        try:
            price = Decimal(price_text)
            size = Decimal(size_text)
        except InvalidOperation as exc:
            raise BookIntegrityError(
                "__ob_protocol_error__", {"reason": f"non-decimal {side} level {index}"}
            ) from exc
        if not price.is_finite() or not size.is_finite() or price <= 0 or size < 0:
            raise BookIntegrityError(
                "__ob_protocol_error__", {"reason": f"invalid {side} values at {index}"}
            )
        if size == 0:
            book.pop(price, None)
        else:
            book[price] = (price_text, size_text)


def _book_checksum(state: _BookState) -> tuple[int, str]:
    asks = sorted(state.asks.items(), key=lambda item: item[0])[:10]
    bids = sorted(state.bids.items(), key=lambda item: item[0], reverse=True)[:10]
    asks_text = ",".join(f"{raw_price}:{raw_size}" for _, (raw_price, raw_size) in asks)
    bids_text = ",".join(f"{raw_price}:{raw_size}" for _, (raw_price, raw_size) in bids)
    text = f"{asks_text}|{bids_text}"
    return zlib.crc32(text.encode("utf-8")) & 0xFFFFFFFF, text


def _integer_field(message: Mapping[str, Any], field_name: str) -> int:
    try:
        return int(message[field_name])
    except (KeyError, TypeError, ValueError) as exc:
        raise BookIntegrityError(
            "__ob_protocol_error__", {"reason": f"missing/invalid {field_name}"}
        ) from exc


def _message_symbol(message: Mapping[str, Any]) -> str | None:
    value = message.get("sy") or message.get("symbol")
    return str(value) if value is not None else None


def _exchange_timestamp_us(message: Mapping[str, Any]) -> int | None:
    # Prefer the underlying event/update time. ``ts`` is the server publish
    # timestamp on compact trades and order-book snapshots.
    for key in ("t", "lts", "ts", "timestamp"):
        value = message.get(key)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    return None


def _publish_timestamp_us(message: Mapping[str, Any]) -> int | None:
    value = message.get("ts")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _native_symbol(value: str) -> str:
    base = str(value).strip().split(":", 1)[0]
    normalized = re.sub(r"[^A-Za-z0-9]+", "", base).upper()
    if not normalized:
        raise ValueError(f"invalid symbol: {value!r}")
    return normalized


def _safe_id(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_")
    if not normalized:
        raise ValueError("session id is empty after sanitization")
    return normalized[:80]


def _new_session_id() -> str:
    return datetime.now(UTC).strftime("rec_%Y%m%dT%H%M%S_%f")


def _resolve_compression(requested: Compression) -> Literal["gzip", "zstd"]:
    if requested == "gzip":
        return "gzip"
    if requested == "zstd":
        _import_zstandard()
        return "zstd"
    try:
        _import_zstandard()
    except RecorderDependencyError:
        return "gzip"
    return "zstd"


def _import_zstandard() -> Any:
    try:
        import zstandard
    except ImportError as exc:
        raise RecorderDependencyError(
            "zstandard is unavailable; install the event-recorder extra or select gzip"
        ) from exc
    return zstandard


def _open_compressed_reader(path: Path):
    if path.name.endswith(".jsonl.gz"):
        return gzip.open(path, "rb")
    if path.name.endswith(".jsonl.zst"):
        zstd = _import_zstandard()
        stream = zstd.ZstdDecompressor().stream_reader(path.open("rb"))
        return io.BufferedReader(stream)
    raise ValueError(f"unsupported event shard extension: {path.name}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w", dir=path.parent, prefix=path.name, suffix=".tmp", delete=False, encoding="utf-8"
    ) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    temporary.replace(path)
    _fsync_directory(path.parent)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _default_connect(url: str):
    import websockets

    return websockets.connect(
        url,
        ping_interval=15,
        ping_timeout=10,
        max_size=None,
        max_queue=2_048,
        close_timeout=5,
    )


async def _run_for(recorder: DeltaEventRecorder, duration_seconds: float | None) -> dict[str, Any]:
    if duration_seconds is None:
        return await recorder.run()
    task = asyncio.create_task(recorder.run())
    try:
        await asyncio.sleep(duration_seconds)
        recorder.request_stop()
        return await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def _run_with_signals(
    recorder: DeltaEventRecorder, duration_seconds: float | None
) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(name, recorder.request_stop)
        except (NotImplementedError, RuntimeError):
            continue
        installed.append(name)
    try:
        return await _run_for(recorder, duration_seconds)
    finally:
        for name in installed:
            loop.remove_signal_handler(name)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    parser.add_argument("--channels", default=",".join(PUBLIC_CHANNELS))
    parser.add_argument("--output-dir", type=Path, default=Path("data/delta_events"))
    parser.add_argument("--compression", choices=["auto", "gzip", "zstd"], default="auto")
    parser.add_argument("--duration-seconds", type=float, default=None)
    parser.add_argument("--queue-max", type=int, default=50_000)
    parser.add_argument("--batch-max", type=int, default=1_000)
    verification = parser.add_mutually_exclusive_group()
    verification.add_argument(
        "--verify-shard",
        type=Path,
        default=None,
        help="verify one finalized shard and exit without opening a websocket",
    )
    verification.add_argument(
        "--verify-root",
        type=Path,
        default=None,
        help="verify every finalized shard and flag partials/orphans under a root",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.verify_shard is not None:
        verification = verify_event_shard(args.verify_shard)
        print(json.dumps(verification.__dict__, indent=2, sort_keys=True))
        return 0 if verification.passed else 2
    if args.verify_root is not None:
        tree = verify_event_tree(args.verify_root)
        print(json.dumps(tree.__dict__, indent=2, sort_keys=True))
        return 0 if tree.passed else 2
    symbols = tuple(value.strip() for value in args.symbols.split(",") if value.strip())
    channels = tuple(value.strip() for value in args.channels.split(",") if value.strip())
    config = DeltaEventRecorderConfig(
        symbols=symbols,
        channels=channels,
        output_dir=args.output_dir,
        compression=args.compression,
        queue_max=args.queue_max,
        batch_max=args.batch_max,
    )
    recorder = DeltaEventRecorder(config)
    result = asyncio.run(_run_with_signals(recorder, args.duration_seconds))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
