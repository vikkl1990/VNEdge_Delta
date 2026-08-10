"""Verified streaming reader for finalized Delta recorder shards."""

from __future__ import annotations

import gzip
import heapq
import io
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from vnedge.exchange.delta_event_recorder import verify_event_shard
from vnedge.replay.models import RecordedEvent, ReplayConfig


class EventStore(Protocol):
    def iter_events(self, config: ReplayConfig) -> Iterator[RecordedEvent]: ...


class DeltaShardEventStore:
    """K-way merge of manifest-verified shards in recorded availability order."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def shard_paths(self, config: ReplayConfig) -> tuple[Path, ...]:
        if not self.root.is_dir():
            raise FileNotFoundError(f"event recorder root does not exist: {self.root}")
        start = datetime.fromtimestamp(config.start_ts_us / 1_000_000, tz=UTC).date()
        end = datetime.fromtimestamp((config.end_ts_us - 1) / 1_000_000, tz=UTC).date()
        # Local-receive sharding can straddle the exchange UTC day around midnight.
        cursor = start - timedelta(days=1)
        final = end + timedelta(days=1)
        paths: set[Path] = set()
        while cursor <= final:
            day_root = self.root / cursor.isoformat()
            for channel in config.channels:
                directory = day_root / channel
                if directory.is_dir():
                    paths.update(directory.glob("*.jsonl.gz"))
                    paths.update(directory.glob("*.jsonl.zst"))
            cursor += timedelta(days=1)
        return tuple(sorted(paths))

    def iter_events(self, config: ReplayConfig) -> Iterator[RecordedEvent]:
        shards = self.shard_paths(config)
        if not shards:
            return
        iterators: list[Iterator[RecordedEvent]] = []
        for path in shards:
            verification = verify_event_shard(path)
            if not verification.passed:
                raise ValueError(
                    f"event shard failed manifest verification: {path}: "
                    + "; ".join(verification.issues)
                )
            iterators.append(self._iter_shard(path, config))

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

    def _iter_shard(self, path: Path, config: ReplayConfig) -> Iterator[RecordedEvent]:
        previous_key: tuple[int, int, int, str] | None = None
        with self._reader(path) as reader:
            for line_number, line in enumerate(reader, 1):
                envelope = json.loads(line)
                if envelope.get("record_kind") != "exchange":
                    continue
                channel = str(envelope.get("channel") or "")
                if channel not in config.channels:
                    continue
                exchange_ts = envelope.get("exchange_timestamp_us")
                symbol = str(envelope.get("symbol") or "").split(":", 1)[-1].upper()
                if exchange_ts is None or symbol not in config.symbols:
                    continue
                ts_us = int(exchange_ts)
                if not config.start_ts_us <= ts_us < config.end_ts_us:
                    continue
                event = RecordedEvent.from_envelope(envelope)
                if previous_key is not None and event.order_key < previous_key:
                    raise ValueError(
                        f"event shard is not in recorded receive order: {path}:{line_number}"
                    )
                previous_key = event.order_key
                yield event

    @staticmethod
    def _reader(path: Path):
        if path.name.endswith(".jsonl.gz"):
            return gzip.open(path, "rb")
        if path.name.endswith(".jsonl.zst"):
            try:
                import zstandard
            except ImportError as exc:
                raise RuntimeError("zstandard is required to replay .zst event shards") from exc
            stream = zstandard.ZstdDecompressor().stream_reader(path.open("rb"))
            return io.BufferedReader(stream)
        raise ValueError(f"unsupported event shard: {path}")
