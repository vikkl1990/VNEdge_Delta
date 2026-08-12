"""Continuity-aware qualification for finalized Delta recorder epochs.

A qualification epoch is scoped to one recorder session. It starts only after
every required symbol has a checksum-verified L2 snapshot. A recovered websocket
connection may continue the epoch only after fresh verified snapshots for every
symbol; sequence, checksum, protocol, or parse faults split it. Active partial
shards are excluded; abandoned partials are blockers. This module never emits
signals and cannot grant paper, promotion, or trading authority.
"""

from __future__ import annotations

import argparse
import bisect
import gzip
import heapq
import io
import json
import os
from collections import Counter, defaultdict
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
from types import MappingProxyType

from vnedge.exchange.delta_event_recorder import verify_event_shard
from vnedge.replay.models import RecordedEvent
from vnedge.replay.validator import validate_recorded_events
from vnedge.research.failed_auction_readiness import (
    RecordingCoverageSummary,
    evaluate_failed_auction_readiness,
    load_failed_auction_contract,
)

RESET_MARKERS = frozenset(
    {
        "__disconnected__",
        "__ob_exchange_error__",
        "__ob_protocol_error__",
        "__ob_update_without_snapshot__",
        "__ob_sequence_gap__",
        "__ob_checksum_mismatch__",
        "__subscription_error__",
        "__read_timeout__",
        "__unparseable_binary__",
        "__unparseable_json__",
        "__unexpected_json_shape__",
    }
)
RECOVERABLE_CONNECTION_MARKERS = frozenset({"__disconnected__", "__read_timeout__"})
HARD_RESET_MARKERS = RESET_MARKERS - RECOVERABLE_CONNECTION_MARKERS


@dataclass(frozen=True)
class ContinuityEpoch:
    session_id: str
    connection_id: str
    start_recv_ns: int
    end_recv_ns: int
    end_reason: str
    snapshot_symbols: tuple[str, ...]
    provisional: bool
    connection_ids: tuple[str, ...] = ()

    @property
    def duration_seconds(self) -> float:
        return max(0.0, (self.end_recv_ns - self.start_recv_ns) / 1_000_000_000)

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "connection_id": self.connection_id,
            "connection_ids": list(self.connection_ids or (self.connection_id,)),
            "recovered_connections": len(self.connection_ids or (self.connection_id,)),
            "start_recv_ns": self.start_recv_ns,
            "end_recv_ns": self.end_recv_ns,
            "start_at": _iso_ns(self.start_recv_ns),
            "end_at": _iso_ns(self.end_recv_ns),
            "duration_seconds": self.duration_seconds,
            "duration_days": self.duration_seconds / 86_400,
            "end_reason": self.end_reason,
            "snapshot_symbols": list(self.snapshot_symbols),
            "provisional": self.provisional,
        }


@dataclass(frozen=True)
class ShardAudit:
    finalized_shards: int
    verified_shards: int
    failed_shards: tuple[str, ...]
    newly_verified_shards: int
    reused_verified_shards: int
    active_partial_files: tuple[str, ...]
    orphan_partial_files: tuple[str, ...]
    verified_paths: tuple[Path, ...]
    cache: Mapping[str, Mapping[str, object]]

    def __post_init__(self) -> None:
        object.__setattr__(self, "failed_shards", tuple(self.failed_shards))
        object.__setattr__(self, "active_partial_files", tuple(self.active_partial_files))
        object.__setattr__(self, "orphan_partial_files", tuple(self.orphan_partial_files))
        object.__setattr__(self, "verified_paths", tuple(self.verified_paths))
        object.__setattr__(
            self,
            "cache",
            MappingProxyType(
                {key: MappingProxyType(dict(value)) for key, value in self.cache.items()}
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "finalized_shards": self.finalized_shards,
            "verified_shards": self.verified_shards,
            "failed_shards": list(self.failed_shards),
            "newly_verified_shards": self.newly_verified_shards,
            "reused_verified_shards": self.reused_verified_shards,
            "active_partial_files": len(self.active_partial_files),
            "orphan_partial_files": len(self.orphan_partial_files),
            "orphan_partial_paths": list(self.orphan_partial_files),
        }


def qualify_event_continuity(
    event_root: Path | str,
    *,
    contract_path: Path | str = Path("configs/research/failed_auction_response_v1.yaml"),
    output_path: Path | str = Path("research/live_research/delta_event_continuity_latest.json"),
    cache_path: Path | str = Path("research/live_research/delta_event_continuity_shard_cache.json"),
    code_version: str = "local",
) -> dict[str, object]:
    root = Path(event_root)
    contract = load_failed_auction_contract(contract_path)
    audit = audit_finalized_shards(root, cache_path=cache_path)
    controls, snapshots, connection_ends = _scan_epoch_markers(
        audit.verified_paths,
        symbols=frozenset(contract.data.symbols),
        channels=frozenset(contract.data.required_channels),
    )
    epochs, last_reset = _build_epochs(
        controls,
        snapshots,
        connection_ends,
        symbols=frozenset(contract.data.symbols),
    )
    longest = max(epochs, key=lambda row: (row.duration_seconds, row.end_recv_ns), default=None)
    latest = max(epochs, key=lambda row: row.end_recv_ns, default=None)
    qualifying = latest
    validation = None
    coverage = None
    readiness = None
    if qualifying is not None:

        def event_factory() -> Iterator[RecordedEvent]:
            return _iter_epoch_events(
                audit.verified_paths,
                qualifying,
                symbols=frozenset(contract.data.symbols),
                channels=frozenset(contract.data.required_channels),
            )

        validation = validate_recorded_events(event_factory())
        coverage = _summarize_epoch(
            event_factory(),
            start_recv_ns=qualifying.start_recv_ns,
            end_recv_ns=qualifying.end_recv_ns,
            symbols=contract.data.symbols,
        )
        readiness = evaluate_failed_auction_readiness(
            contract,
            validation,
            coverage,
            manifest_verified=not audit.failed_shards and not audit.orphan_partial_files,
            partial_files=len(audit.orphan_partial_files),
        )
    blockers = list(readiness.blockers if readiness else ("no_verified_epoch",))
    events = coverage.total_events if coverage else 0
    duration_days = qualifying.duration_seconds / 86_400 if qualifying else 0.0
    events_remaining = max(0, contract.data.minimum_total_events - events)
    days_remaining = max(0.0, contract.data.minimum_continuous_days - duration_days)
    event_rate_per_day = events / duration_days if duration_days > 0 else None
    event_eta_days = (
        events_remaining / event_rate_per_day
        if event_rate_per_day and event_rate_per_day > 0
        else None
    )
    eta_days = max(days_remaining, event_eta_days or 0.0)
    # Shard rotation makes a short open epoch look artificially sparse. Wait for
    # a full day before projecting an ETA from its observed finalized-event rate.
    can_grow = bool(
        qualifying and qualifying.provisional and duration_days >= 1.0 and events >= 10_000
    )
    estimated_ready_at = (
        (datetime.now(UTC) + timedelta(days=eta_days)).isoformat()
        if can_grow and event_rate_per_day
        else None
    )
    payload: dict[str, object] = {
        "schema_version": "vnedge.delta_event_continuity.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "code_version": code_version,
        "contract_id": contract.contract_id,
        "event_root": str(root),
        "audit": audit.to_dict(),
        "epochs": {
            "count": len(epochs),
            "qualification_basis": "latest_recoverable_epoch",
            "latest": latest.to_dict() if latest else None,
            "longest": longest.to_dict() if longest else None,
            "last_reset": last_reset,
        },
        "qualification": {
            "data_ready": bool(readiness and readiness.data_ready),
            "blockers": blockers,
            "qualified_events": events,
            "target_events": contract.data.minimum_total_events,
            "qualified_days": duration_days,
            "target_days": contract.data.minimum_continuous_days,
            "events_remaining": events_remaining,
            "days_remaining": days_remaining,
            "observed_events_per_day": event_rate_per_day,
            "estimated_ready_at": estimated_ready_at,
            "estimate_status": (
                "PROJECTED_FROM_OPEN_EPOCH"
                if estimated_ready_at
                else "INSUFFICIENT_RATE_SAMPLE"
                if qualifying and qualifying.provisional
                else "AWAITING_STABLE_OPEN_EPOCH"
            ),
            "coverage": coverage.to_dict() if coverage else {},
            "semantic_validation": validation.to_dict() if validation else {},
        },
        "scanner_implementation_authorized": False,
        "selection_authorized": False,
        "research_only": True,
        "can_trade": False,
        "can_promote": False,
        "order_route": "absent",
    }
    _atomic_json(Path(output_path), payload)
    _atomic_json(
        Path(cache_path),
        {
            "schema_version": "v1",
            "shards": {key: dict(value) for key, value in audit.cache.items()},
        },
    )
    return payload


def audit_finalized_shards(
    event_root: Path,
    *,
    cache_path: Path | str,
) -> ShardAudit:
    shards = sorted({*event_root.rglob("*.jsonl.gz"), *event_root.rglob("*.jsonl.zst")})
    partials = tuple(sorted(event_root.rglob("*.partial")))
    status = _read_json(event_root / "_recorder_status.json")
    active_session = str(status.get("session_id") or "")
    fresh = _status_is_fresh(status)
    active = tuple(
        str(path) for path in partials if fresh and active_session and active_session in path.name
    )
    orphan = tuple(str(path) for path in partials if str(path) not in set(active))
    previous = _read_json(Path(cache_path)).get("shards")
    cached = previous if isinstance(previous, dict) else {}
    next_cache: dict[str, dict[str, object]] = {}
    verified: list[Path] = []
    failed: list[str] = []
    new_count = 0
    reused = 0
    for shard in shards:
        manifest_path = Path(f"{shard}.manifest.json")
        manifest = _read_json(manifest_path)
        identity = {
            "compressed_sha256": manifest.get("compressed_sha256"),
            "size": shard.stat().st_size,
            "mtime_ns": shard.stat().st_mtime_ns,
            "manifest_mtime_ns": manifest_path.stat().st_mtime_ns
            if manifest_path.is_file()
            else None,
        }
        old = cached.get(str(shard)) if isinstance(cached, dict) else None
        if isinstance(old, dict) and all(old.get(key) == value for key, value in identity.items()):
            passed = old.get("passed") is True
            issues = list(old.get("issues") or [])
            reused += 1
        else:
            result = verify_event_shard(shard)
            passed = result.passed
            issues = list(result.issues)
            new_count += 1
        next_cache[str(shard)] = {**identity, "passed": passed, "issues": issues}
        if passed:
            verified.append(shard)
        else:
            failed.append(str(shard))
    return ShardAudit(
        finalized_shards=len(shards),
        verified_shards=len(verified),
        failed_shards=tuple(failed),
        newly_verified_shards=new_count,
        reused_verified_shards=reused,
        active_partial_files=active,
        orphan_partial_files=orphan,
        verified_paths=tuple(verified),
        cache=next_cache,
    )


def _scan_epoch_markers(
    paths: tuple[Path, ...],
    *,
    symbols: frozenset[str],
    channels: frozenset[str],
) -> tuple[
    list[dict[str, object]],
    dict[tuple[str, str, str], list[int]],
    dict[tuple[str, str], int],
]:
    controls: list[dict[str, object]] = []
    snapshots: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    ends: dict[tuple[str, str], int] = {}
    for path in paths:
        channel_dir = path.parent.name
        if channel_dir not in {"_control", "_wire_error", *channels}:
            continue
        with _reader(path) as reader:
            for line in reader:
                row = json.loads(line)
                session = str(row.get("session_id") or "unknown_session")
                connection = str(row.get("connection_id") or "unknown_connection")
                receive_ns = int(row.get("local_recv_ns") or 0)
                key = (session, connection)
                ends[key] = max(ends.get(key, 0), receive_ns)
                if row.get("record_kind") == "control" or channel_dir in {
                    "_control",
                    "_wire_error",
                }:
                    controls.append(row)
                    continue
                symbol = str(row.get("symbol") or "").split(":", 1)[-1].upper()
                if (
                    symbol in symbols
                    and row.get("channel") == "ob_updates"
                    and row.get("action") == "snapshot"
                ):
                    snapshots[(session, connection, symbol)].append(receive_ns)
    controls.sort(
        key=lambda row: (
            str(row.get("session_id") or ""),
            int(row.get("local_recv_ns") or 0),
            int(row.get("event_index") or 0),
        )
    )
    for values in snapshots.values():
        values.sort()
    return controls, snapshots, ends


def _build_epochs(
    controls: list[dict[str, object]],
    snapshots: dict[tuple[str, str, str], list[int]],
    connection_ends: dict[tuple[str, str], int],
    *,
    symbols: frozenset[str],
) -> tuple[list[ContinuityEpoch], dict[str, object] | None]:
    valid: dict[tuple[str, str], dict[str, int]] = defaultdict(dict)
    connection_epochs: list[ContinuityEpoch] = []
    closed: set[tuple[str, str]] = set()
    hard_resets: list[tuple[str, int, str, str]] = []
    last_reset: dict[str, object] | None = None
    for row in controls:
        session = str(row.get("session_id") or "unknown_session")
        connection = str(row.get("connection_id") or "unknown_connection")
        key = (session, connection)
        marker = str(row.get("marker") or "")
        receive_ns = int(row.get("local_recv_ns") or 0)
        details = row.get("details") if isinstance(row.get("details"), dict) else {}
        if marker == "__ob_snapshot_valid__":
            symbol = str(details.get("symbol") or "").upper()
            candidates = snapshots.get((session, connection, symbol), [])
            position = bisect.bisect_right(candidates, receive_ns) - 1
            if symbol in symbols and position >= 0:
                valid[key][symbol] = candidates[position]
            continue
        if marker not in RESET_MARKERS or key in closed:
            continue
        if symbols.issubset(valid[key]):
            start_ns = max(valid[key][symbol] for symbol in symbols)
            end_ns = receive_ns - 1 if marker in HARD_RESET_MARKERS else receive_ns
            if end_ns > start_ns:
                connection_epochs.append(
                    ContinuityEpoch(
                        session,
                        connection,
                        start_ns,
                        end_ns,
                        marker,
                        tuple(sorted(symbols)),
                        False,
                        (connection,),
                    )
                )
        closed.add(key)
        if marker in HARD_RESET_MARKERS:
            hard_resets.append((session, receive_ns, marker, connection))
            last_reset = {
                "at": _iso_ns(receive_ns),
                "local_recv_ns": receive_ns,
                "reason": marker,
                "session_id": session,
                "connection_id": connection,
            }
    for key, end_ns in connection_ends.items():
        if key in closed or not symbols.issubset(valid[key]):
            continue
        start_ns = max(valid[key][symbol] for symbol in symbols)
        if end_ns > start_ns:
            connection_epochs.append(
                ContinuityEpoch(
                    key[0],
                    key[1],
                    start_ns,
                    end_ns,
                    "open_at_last_finalized_event",
                    tuple(sorted(symbols)),
                    True,
                    (key[1],),
                )
            )
    epochs: list[ContinuityEpoch] = []
    for child in sorted(connection_epochs, key=lambda row: row.start_recv_ns):
        if not epochs:
            epochs.append(child)
            continue
        previous = epochs[-1]
        split = previous.session_id != child.session_id or any(
            session == child.session_id and previous.start_recv_ns < reset_ns <= child.start_recv_ns
            for session, reset_ns, _, _ in hard_resets
        )
        if split:
            epochs.append(child)
            continue
        connections = previous.connection_ids or (previous.connection_id,)
        child_connections = child.connection_ids or (child.connection_id,)
        epochs[-1] = ContinuityEpoch(
            session_id=previous.session_id,
            connection_id=child.connection_id,
            start_recv_ns=previous.start_recv_ns,
            end_recv_ns=max(previous.end_recv_ns, child.end_recv_ns),
            end_reason=child.end_reason,
            snapshot_symbols=previous.snapshot_symbols,
            provisional=child.provisional,
            connection_ids=(*connections, *child_connections),
        )
    return epochs, last_reset


def _iter_epoch_events(
    paths: tuple[Path, ...],
    epoch: ContinuityEpoch,
    *,
    symbols: frozenset[str],
    channels: frozenset[str],
) -> Iterator[RecordedEvent]:
    iterators: list[Iterator[RecordedEvent]] = []
    for path in paths:
        if path.parent.name not in channels:
            continue
        iterators.append(_iter_epoch_shard(path, epoch, symbols=symbols, channels=channels))
    heap: list[tuple[tuple[int, int, int, str], int, RecordedEvent]] = []
    for index, iterator in enumerate(iterators):
        try:
            event = next(iterator)
        except StopIteration:
            continue
        heapq.heappush(heap, (event.order_key, index, event))
    while heap:
        _, index, event = heapq.heappop(heap)
        yield event
        try:
            following = next(iterators[index])
        except StopIteration:
            continue
        heapq.heappush(heap, (following.order_key, index, following))


def _iter_epoch_shard(
    path: Path,
    epoch: ContinuityEpoch,
    *,
    symbols: frozenset[str],
    channels: frozenset[str],
) -> Iterator[RecordedEvent]:
    with _reader(path) as reader:
        for line in reader:
            row = json.loads(line)
            receive_ns = int(row.get("local_recv_ns") or 0)
            symbol = str(row.get("symbol") or "").split(":", 1)[-1].upper()
            if (
                row.get("record_kind") == "exchange"
                and row.get("session_id") == epoch.session_id
                and row.get("connection_id") in (epoch.connection_ids or (epoch.connection_id,))
                and symbol in symbols
                and row.get("channel") in channels
            ):
                warmup = (
                    row.get("channel") == "ob_updates"
                    and row.get("action") == "snapshot"
                    and receive_ns < epoch.start_recv_ns
                )
                if not warmup and not epoch.start_recv_ns <= receive_ns <= epoch.end_recv_ns:
                    continue
                if warmup:
                    row = {**row, "replay_warmup": True}
                yield RecordedEvent.from_envelope(row)


def _summarize_epoch(
    events: Iterator[RecordedEvent],
    *,
    start_recv_ns: int,
    end_recv_ns: int,
    symbols: tuple[str, ...],
) -> RecordingCoverageSummary:
    counts: Counter[str] = Counter()
    pairs: Counter[str] = Counter()
    hours: dict[str, set[int]] = defaultdict(set)
    snapshots: Counter[str] = Counter()
    last_book: dict[str, int] = {}
    interarrival: dict[str, list[int]] = defaultdict(list)
    total = 0
    for event in events:
        if event.channel == "ob_updates" and event.raw_message.get("action") == "snapshot":
            snapshots[event.symbol] += 1
        if event.envelope.get("replay_warmup") is True:
            continue
        total += 1
        counts[event.symbol] += 1
        pair = f"{event.symbol}:{event.channel}"
        pairs[pair] += 1
        hours[pair].add((event.local_recv_ns - start_recv_ns) // 3_600_000_000_000)
        if event.channel != "ob_updates":
            continue
        previous = last_book.get(event.symbol)
        if previous is not None and event.local_recv_ns >= previous:
            interarrival[event.symbol].append(event.local_recv_ns - previous)
        last_book[event.symbol] = event.local_recv_ns
    duration_us = max(1, (end_recv_ns - start_recv_ns) // 1_000)
    return RecordingCoverageSummary(
        requested_start_ts_us=start_recv_ns // 1_000,
        requested_end_ts_us=start_recv_ns // 1_000 + duration_us,
        total_events=total,
        counts_by_symbol=dict(counts),
        counts_by_symbol_channel=dict(pairs),
        covered_hours_by_symbol_channel={key: len(value) for key, value in hours.items()},
        snapshot_counts_by_symbol={symbol: snapshots.get(symbol, 0) for symbol in symbols},
        book_interarrival_p95_ms_by_symbol={
            symbol: _p95_ms(interarrival.get(symbol, [])) for symbol in symbols
        },
    )


def _p95_ms(values: list[int]) -> float | None:
    if not values:
        return None
    rows = sorted(values)
    return rows[min(len(rows) - 1, int((len(rows) - 1) * 0.95))] / 1_000_000


def _reader(path: Path):
    if path.name.endswith(".jsonl.gz"):
        return gzip.open(path, "rb")
    if path.name.endswith(".jsonl.zst"):
        try:
            import zstandard
        except ImportError as exc:  # pragma: no cover - optional environment dependency
            raise RuntimeError("zstandard is required to audit .zst shards") from exc
        return io.BufferedReader(zstandard.ZstdDecompressor().stream_reader(path.open("rb")))
    raise ValueError(f"unsupported event shard: {path}")


def _status_is_fresh(status: Mapping[str, object]) -> bool:
    if str(status.get("state") or "").upper() != "RECORDING":
        return False
    updated = status.get("updated_at")
    if not isinstance(updated, str):
        return False
    try:
        moment = datetime.fromisoformat(updated)
    except ValueError:
        return False
    age = (datetime.now(UTC) - moment.astimezone(UTC)).total_seconds()
    return age <= max(90.0, float(status.get("stats_seconds") or 30.0) * 3)


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _iso_ns(value: int) -> str:
    return datetime.fromtimestamp(value / 1_000_000_000, tz=UTC).isoformat()


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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-root", type=Path, default=Path("data/delta_events"))
    parser.add_argument(
        "--contract",
        type=Path,
        default=Path("configs/research/failed_auction_response_v1.yaml"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("research/live_research/delta_event_continuity_latest.json"),
    )
    parser.add_argument(
        "--cache",
        type=Path,
        default=Path("research/live_research/delta_event_continuity_shard_cache.json"),
    )
    parser.add_argument("--code-version", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    payload = qualify_event_continuity(
        args.event_root,
        contract_path=args.contract,
        output_path=args.output,
        cache_path=args.cache,
        code_version=args.code_version,
    )
    print(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False))
    return 0 if payload["qualification"]["data_ready"] else 2  # type: ignore[index]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
