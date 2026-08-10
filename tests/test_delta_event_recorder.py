"""Delta event recorder: exact wire archive, L2 proofs, and durable shards."""

from __future__ import annotations

import asyncio
import gzip
import json
import zlib
from pathlib import Path

import pytest

from vnedge.exchange.delta_event_recorder import (
    DELTA_PUBLIC_WS_URL,
    BookIntegrityError,
    DeltaBookIntegrityValidator,
    DeltaEventRecorder,
    DeltaEventRecorderConfig,
    RotatingEventWriter,
    SubscriptionIntegrityError,
    verify_event_shard,
    verify_event_tree,
)

BASE_NS = 1_783_532_640_000_000_000


def _checksum(asks: list[list[str]], bids: list[list[str]]) -> int:
    asks_sorted = sorted(asks, key=lambda level: float(level[0]))[:10]
    bids_sorted = sorted(bids, key=lambda level: float(level[0]), reverse=True)[:10]
    asks_text = ",".join(f"{price}:{size}" for price, size in asks_sorted)
    bids_text = ",".join(f"{price}:{size}" for price, size in bids_sorted)
    return zlib.crc32(f"{asks_text}|{bids_text}".encode()) & 0xFFFFFFFF


def _snapshot(*, sequence: int = 100) -> dict[str, object]:
    asks = [["101.0", "2"], ["102.0", "3"]]
    bids = [["100.0", "4"], ["99.0", "5"]]
    return {
        "type": "ob_updates",
        "action": "snapshot",
        "sy": "BTCUSD",
        "seq": sequence,
        "cs": _checksum(asks, bids),
        "a": asks,
        "b": bids,
        "ts": BASE_NS // 1_000,
    }


def test_public_config_normalizes_symbols_and_mark_subscription(tmp_path: Path) -> None:
    config = DeltaEventRecorderConfig(
        symbols=("BTC/USD:USD", "ethusd"),
        channels=("trades", "mark_price"),
        output_dir=tmp_path,
        compression="gzip",
    )
    assert config.symbols == ("BTCUSD", "ETHUSD")
    assert config.url == DELTA_PUBLIC_WS_URL
    assert config.subscriptions() == [
        {"name": "trades", "symbols": ["BTCUSD", "ETHUSD"]},
        {"name": "mark_price", "symbols": ["MARK:BTCUSD", "MARK:ETHUSD"]},
    ]

    with_l2 = DeltaEventRecorderConfig(
        symbols=("BTCUSD", "ETHUSD"),
        channels=("ob_l2", "ticker"),
        output_dir=tmp_path,
        compression="gzip",
    )
    assert with_l2.subscriptions() == [
        {"name": "ob_l2", "symbols": ["BTCUSD"]},
        {"name": "ob_l2", "symbols": ["ETHUSD"]},
        {"name": "ticker", "symbols": ["BTCUSD", "ETHUSD"]},
    ]
    status = DeltaEventRecorderConfig(
        symbols=("BTCUSD",),
        channels=("system_status",),
        output_dir=tmp_path,
        compression="gzip",
    )
    assert status.subscriptions() == [{"name": "system_status"}]


def test_config_rejects_private_endpoint_and_non_public_channels(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="public websocket"):
        DeltaEventRecorderConfig(url="wss://socket.india.delta.exchange", output_dir=tmp_path)
    with pytest.raises(ValueError, match="unsupported/non-public"):
        DeltaEventRecorderConfig(channels=("orders",), output_dir=tmp_path)


def test_book_validator_accepts_snapshot_and_contiguous_update() -> None:
    validator = DeltaBookIntegrityValidator()
    health = validator.observe(_snapshot())
    assert health == {
        "symbol": "BTCUSD",
        "action": "snapshot",
        "sequence": 100,
        "checksum": _snapshot()["cs"],
        "asks": 2,
        "bids": 2,
        "healthy": True,
    }

    asks = [["101.0", "7"], ["103.0", "1"]]
    bids = [["100.0", "4"], ["98.0", "9"]]
    update = {
        "type": "ob_updates",
        "action": "update",
        "sy": "BTCUSD",
        "seq": 101,
        "a": [["101.0", "7"], ["102.0", "0"], ["103.0", "1"]],
        "b": [["99.0", "0"], ["98.0", "9"]],
        "cs": _checksum(asks, bids),
    }
    assert validator.observe(update)["healthy"] is True


def test_book_validator_fails_closed_on_sequence_gap_and_requires_snapshot() -> None:
    validator = DeltaBookIntegrityValidator()
    validator.observe(_snapshot())
    gap = {**_snapshot(sequence=102), "action": "update", "a": [], "b": []}
    with pytest.raises(BookIntegrityError) as caught:
        validator.observe(gap)
    assert caught.value.marker == "__ob_sequence_gap__"
    assert caught.value.details["expected"] == 101

    gap["seq"] = 103
    with pytest.raises(BookIntegrityError) as caught:
        validator.observe(gap)
    assert caught.value.marker == "__ob_update_without_snapshot__"


def test_book_validator_fails_closed_on_checksum_mismatch() -> None:
    validator = DeltaBookIntegrityValidator()
    bad = _snapshot()
    bad["cs"] = int(bad["cs"]) + 1
    with pytest.raises(BookIntegrityError) as caught:
        validator.observe(bad)
    assert caught.value.marker == "__ob_checksum_mismatch__"
    assert caught.value.details["provided"] != caught.value.details["computed"]


def _envelope(index: int, local_recv_ns: int) -> dict[str, object]:
    return {
        "schema_version": "vnedge.delta_public_event.v1",
        "session_id": "test_session",
        "connection_id": "conn_000001",
        "event_index": index,
        "local_recv_ns": local_recv_ns,
        "local_monotonic_ns": index,
        "record_kind": "exchange",
        "research_only": True,
        "can_trade": False,
        "can_promote": False,
    }


def test_writer_rotates_atomically_and_manifests_verify(tmp_path: Path) -> None:
    config = DeltaEventRecorderConfig(
        symbols=("BTCUSD",),
        channels=("trades",),
        output_dir=tmp_path,
        rotate_seconds=3_600,
        compression="gzip",
    )
    writer = RotatingEventWriter(config, session_id="test_session")
    writer.write_batch(
        [
            ("trades", _envelope(1, BASE_NS)),
            ("trades", _envelope(2, BASE_NS + 3_600_000_000_000)),
        ]
    )
    shards = writer.close()

    assert len(shards) == 2
    assert not list(tmp_path.rglob("*.partial"))
    for shard in shards:
        verification = verify_event_shard(shard)
        assert verification.passed, verification.issues
        assert verification.records == 1
        assert Path(f"{shard}.manifest.json").is_file()


def test_zstd_shard_round_trip_when_optional_dependency_is_installed(
    tmp_path: Path,
) -> None:
    pytest.importorskip("zstandard")
    config = DeltaEventRecorderConfig(
        symbols=("BTCUSD",),
        channels=("trades",),
        output_dir=tmp_path,
        compression="zstd",
    )
    writer = RotatingEventWriter(config, session_id="zstd_test")
    writer.write_batch([("trades", _envelope(1, BASE_NS))])
    shard = writer.close()[0]
    assert shard.name.endswith(".jsonl.zst")
    result = verify_event_shard(shard)
    assert result.passed is True
    assert result.records == 1


def test_verifier_detects_tampered_finalized_shard(tmp_path: Path) -> None:
    config = DeltaEventRecorderConfig(
        symbols=("BTCUSD",),
        channels=("trades",),
        output_dir=tmp_path,
        compression="gzip",
    )
    writer = RotatingEventWriter(config, session_id="tamper_test")
    writer.write_batch([("trades", _envelope(1, BASE_NS))])
    shard = writer.close()[0]
    with shard.open("ab") as handle:
        handle.write(b"tamper")
    result = verify_event_shard(shard)
    assert result.passed is False
    assert "compressed SHA-256 mismatch" in result.issues


def test_tree_verifier_rejects_crash_partials(tmp_path: Path) -> None:
    config = DeltaEventRecorderConfig(
        symbols=("BTCUSD",),
        channels=("trades",),
        output_dir=tmp_path,
        compression="gzip",
    )
    writer = RotatingEventWriter(config, session_id="tree_test")
    writer.write_batch([("trades", _envelope(1, BASE_NS))])
    writer.close()
    clean = verify_event_tree(tmp_path)
    assert clean.passed is True
    assert clean.storage_passed is True and clean.continuity_passed is True
    assert clean.shards == 1 and clean.records == 1

    partial = tmp_path / "crashed.jsonl.gz.partial"
    partial.write_bytes(b"incomplete")
    dirty = verify_event_tree(tmp_path)
    assert dirty.passed is False
    assert dirty.partial_files == (str(partial),)


def test_tree_verifier_surfaces_recorded_feed_integrity_fault(tmp_path: Path) -> None:
    config = DeltaEventRecorderConfig(
        symbols=("BTCUSD",),
        channels=("ob_updates",),
        output_dir=tmp_path,
        compression="gzip",
    )
    event = _envelope(9, BASE_NS)
    event.update(
        {
            "record_kind": "control",
            "marker": "__ob_sequence_gap__",
            "details": {"expected": 5, "got": 7},
        }
    )
    writer = RotatingEventWriter(config, session_id="gap_tree")
    writer.write_batch([("_control", event)])
    writer.close()

    result = verify_event_tree(tmp_path)
    assert result.storage_passed is True
    assert result.continuity_passed is False
    assert result.passed is False
    assert result.integrity_faults[0].endswith(":9:__ob_sequence_gap__")


def test_writer_refuses_to_overwrite_same_session_shard(tmp_path: Path) -> None:
    config = DeltaEventRecorderConfig(
        symbols=("BTCUSD",),
        channels=("trades",),
        output_dir=tmp_path,
        compression="gzip",
    )
    first = RotatingEventWriter(config, session_id="same_session")
    first.write_batch([("trades", _envelope(1, BASE_NS))])
    original = first.close()[0]
    original_bytes = original.read_bytes()

    second = RotatingEventWriter(config, session_id="same_session")
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        second.write_batch([("trades", _envelope(2, BASE_NS))])
    assert original.read_bytes() == original_bytes


class _FakeWebSocket:
    def __init__(self, frames: list[str]) -> None:
        self._frames: asyncio.Queue[str] = asyncio.Queue()
        for frame in frames:
            self._frames.put_nowait(frame)
        self.sent: list[dict[str, object]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    async def recv(self) -> str:
        return await self._frames.get()


async def test_recorder_archives_exact_wire_and_stops_without_read_timeout(
    tmp_path: Path,
) -> None:
    snapshot_text = json.dumps(_snapshot(), separators=(",", ":"))
    trade_text = (
        '{"type":"trades","sy":"BTCUSD","p":"62000.5",'
        '"s":3,"r":"t","t":1783532640000000,"ts":1783532640000010}'
    )
    websocket = _FakeWebSocket([snapshot_text, trade_text])
    config = DeltaEventRecorderConfig(
        symbols=("BTCUSD",),
        channels=("ob_updates", "trades"),
        output_dir=tmp_path,
        compression="gzip",
        batch_max=10,
    )
    recorder = DeltaEventRecorder(
        config,
        connect=lambda _url: websocket,
        session_id="end_to_end",
    )
    task = asyncio.create_task(recorder.run())
    for _ in range(100):
        if recorder.counts["ob_updates"] and recorder.counts["trades"]:
            break
        await asyncio.sleep(0.01)
    recorder.request_stop()
    result = await asyncio.wait_for(task, timeout=2.0)

    assert result["can_trade"] is False
    assert result["can_promote"] is False
    assert result["connections"] == 1
    assert websocket.sent[0] == {"type": "enable_heartbeat"}
    assert websocket.sent[1]["type"] == "subscribe"
    assert not any("auth" in json.dumps(message).lower() for message in websocket.sent)
    status = json.loads((tmp_path / "_recorder_status.json").read_text())
    assert status["state"] == "STOPPED"
    assert status["events"] >= 2
    assert status["symbols"] == ["BTCUSD"]
    assert status["can_trade"] is False


async def test_recorder_observer_receives_only_parsed_and_verified_events(
    tmp_path: Path,
) -> None:
    observed: list[dict[str, object]] = []

    async def observer(envelope):
        observed.append(dict(envelope))

    snapshot_text = json.dumps(_snapshot(), separators=(",", ":"))
    trade_text = (
        '{"type":"trades","sy":"BTCUSD","p":"62000.5",'
        '"s":3,"r":"t","t":1783532640000000,"ts":1783532640000010}'
    )
    websocket = _FakeWebSocket([snapshot_text, trade_text])
    recorder = DeltaEventRecorder(
        DeltaEventRecorderConfig(
            symbols=("BTCUSD",),
            channels=("ob_updates", "trades"),
            output_dir=tmp_path,
            compression="gzip",
        ),
        connect=lambda _url: websocket,
        session_id="verified_observer",
        verified_event_observer=observer,
    )
    task = asyncio.create_task(recorder.run())
    for _ in range(100):
        if len(observed) >= 2:
            break
        await asyncio.sleep(0.01)
    recorder.request_stop()
    await asyncio.wait_for(task, timeout=2.0)

    assert [row["channel"] for row in observed] == ["ob_updates", "trades"]
    assert all(row["record_kind"] == "exchange" for row in observed)

    trade_shard = next(tmp_path.rglob("trades_*.jsonl.gz"))
    assert verify_event_shard(trade_shard).passed is True
    with gzip.open(trade_shard, "rt", encoding="utf-8") as handle:
        trade_event = json.loads(handle.readline())
    assert trade_event["raw_text"] == trade_text
    assert trade_event["local_recv_ns"] > 0
    assert trade_event["local_monotonic_ns"] > 0


async def test_recorder_preserves_invalid_wire_bytes_exactly(tmp_path: Path) -> None:
    config = DeltaEventRecorderConfig(
        symbols=("BTCUSD",),
        channels=("trades",),
        output_dir=tmp_path,
        compression="gzip",
    )
    recorder = DeltaEventRecorder(config, session_id="wire_error")
    invalid = b"\xff\xfeprivate-wire-evidence"
    await recorder._handle_wire(invalid, connection_id="conn_000001")
    channel, event = await recorder._queue.get()
    assert channel == "_wire_error"
    assert event["marker"] == "__unparseable_binary__"
    assert event["raw_base64"] == "//5wcml2YXRlLXdpcmUtZXZpZGVuY2U="


async def test_subscription_rejection_is_archived_and_fails_connection(
    tmp_path: Path,
) -> None:
    config = DeltaEventRecorderConfig(
        symbols=("BTCUSD",),
        channels=("ob_l2",),
        output_dir=tmp_path,
        compression="gzip",
    )
    recorder = DeltaEventRecorder(config, session_id="subscription_error")
    raw = json.dumps(
        {
            "type": "subscriptions",
            "channels": [{"name": "ob_l2", "error": "forbidden"}],
        }
    )
    with pytest.raises(SubscriptionIntegrityError):
        await recorder._handle_wire(raw, connection_id="conn_000001")
    exchange_channel, exchange = await recorder._queue.get()
    control_channel, control = await recorder._queue.get()
    assert exchange_channel == "subscriptions"
    assert exchange["raw_text"] == raw
    assert control_channel == "_control"
    assert control["marker"] == "__subscription_error__"


class _ConnectSequence:
    def __init__(self, sockets: list[_FakeWebSocket]) -> None:
        self.sockets = sockets
        self.calls = 0

    def __call__(self, _url: str) -> _FakeWebSocket:
        socket = self.sockets[min(self.calls, len(self.sockets) - 1)]
        self.calls += 1
        return socket


async def test_book_integrity_failure_forces_reconnect_and_fresh_snapshot(
    tmp_path: Path,
) -> None:
    first_snapshot = _snapshot(sequence=10)
    gap = {
        "type": "ob_updates",
        "action": "update",
        "sy": "BTCUSD",
        "seq": 12,
        "cs": first_snapshot["cs"],
        "a": [],
        "b": [],
    }
    second_snapshot = _snapshot(sequence=500)
    connector = _ConnectSequence(
        [
            _FakeWebSocket([json.dumps(first_snapshot), json.dumps(gap)]),
            _FakeWebSocket([json.dumps(second_snapshot)]),
        ]
    )
    config = DeltaEventRecorderConfig(
        symbols=("BTCUSD",),
        channels=("ob_updates",),
        output_dir=tmp_path,
        compression="gzip",
        max_backoff_seconds=0.01,
    )
    recorder = DeltaEventRecorder(
        config,
        connect=connector,
        session_id="integrity_reconnect",
    )
    task = asyncio.create_task(recorder.run())
    for _ in range(200):
        if recorder.connection_count >= 2 and recorder.counts["ob_updates"] >= 3:
            break
        await asyncio.sleep(0.01)
    recorder.request_stop()
    result = await asyncio.wait_for(task, timeout=2.0)

    assert connector.calls >= 2
    assert result["connections"] >= 2
    control_events: list[dict[str, object]] = []
    for shard in tmp_path.rglob("_control_*.jsonl.gz"):
        with gzip.open(shard, "rt", encoding="utf-8") as handle:
            control_events.extend(json.loads(line) for line in handle)
    markers = [event["marker"] for event in control_events]
    assert "__ob_sequence_gap__" in markers
    assert markers.count("__ob_snapshot_valid__") >= 2
