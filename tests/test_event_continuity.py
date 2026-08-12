from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from vnedge.exchange.delta_event_recorder import DeltaEventRecorderConfig, RotatingEventWriter
from vnedge.research.event_continuity import qualify_event_continuity

BASE_NS = int(datetime(2026, 8, 12, tzinfo=UTC).timestamp() * 1_000_000_000)


def _row(
    index: int,
    offset_ms: int,
    *,
    channel: str,
    symbol: str = "",
    marker: str | None = None,
    action: str | None = None,
    connection: str = "conn_000001",
) -> dict[str, object]:
    receive_ns = BASE_NS + offset_ms * 1_000_000
    row: dict[str, object] = {
        "schema_version": "vnedge.delta_public_event.v1",
        "session_id": "continuity_test",
        "connection_id": connection,
        "event_index": index,
        "local_recv_ns": receive_ns,
        "local_monotonic_ns": receive_ns,
        "record_kind": "control" if marker else "exchange",
        "research_only": True,
        "can_trade": False,
        "can_promote": False,
    }
    if marker:
        row.update(
            {
                "marker": marker,
                "details": {"symbol": symbol} if symbol else {"error_type": "test"},
            }
        )
        return row
    message = {
        "type": channel,
        "sy": symbol,
        "action": action,
        "ts": receive_ns // 1_000,
        "seq": index,
        "cs": index,
        "p": "100",
        "s": "1",
        "r": "t",
    }
    row.update(
        {
            "channel": channel,
            "symbol": symbol,
            "exchange_timestamp_us": receive_ns // 1_000,
            "action": action,
            "sequence": index if channel == "ob_updates" else None,
            "checksum": index if channel == "ob_updates" else None,
            "raw_text": json.dumps(message),
        }
    )
    return row


def _write(root: Path, rows: list[tuple[str, dict[str, object]]]) -> None:
    writer = RotatingEventWriter(
        DeltaEventRecorderConfig(
            symbols=("BTCUSD", "ETHUSD"),
            channels=("trades", "ob_updates"),
            output_dir=root,
            compression="gzip",
        ),
        session_id="continuity_test",
    )
    writer.write_batch(rows)
    writer.close()


def test_epoch_starts_after_all_snapshots_and_ends_on_disconnect(tmp_path: Path) -> None:
    root = tmp_path / "events"
    rows = [
        ("ob_updates", _row(1, 100, channel="ob_updates", symbol="BTCUSD", action="snapshot")),
        (
            "_control",
            _row(2, 101, channel="_control", symbol="BTCUSD", marker="__ob_snapshot_valid__"),
        ),
        ("trades", _row(3, 150, channel="trades", symbol="BTCUSD")),
        ("ob_updates", _row(4, 200, channel="ob_updates", symbol="ETHUSD", action="snapshot")),
        (
            "_control",
            _row(5, 201, channel="_control", symbol="ETHUSD", marker="__ob_snapshot_valid__"),
        ),
        ("trades", _row(6, 250, channel="trades", symbol="ETHUSD")),
        ("_control", _row(7, 500, channel="_control", marker="__disconnected__")),
    ]
    _write(root, rows)

    payload = qualify_event_continuity(
        root,
        output_path=tmp_path / "result.json",
        cache_path=tmp_path / "cache.json",
        code_version="test",
    )

    latest = payload["epochs"]["latest"]
    assert latest["start_recv_ns"] == BASE_NS + 200_000_000
    assert latest["end_recv_ns"] == BASE_NS + 500_000_000
    assert latest["end_reason"] == "__disconnected__"
    assert latest["provisional"] is False
    assert payload["qualification"]["qualified_events"] == 2
    assert payload["qualification"]["coverage"]["snapshot_counts_by_symbol"] == {
        "BTCUSD": 1,
        "ETHUSD": 1,
    }
    assert payload["scanner_implementation_authorized"] is False
    assert payload["can_trade"] is False


def test_integrity_fault_splits_epochs_and_longest_is_not_stitched(tmp_path: Path) -> None:
    root = tmp_path / "events"
    rows = [
        ("ob_updates", _row(1, 100, channel="ob_updates", symbol="BTCUSD", action="snapshot")),
        (
            "_control",
            _row(2, 101, channel="_control", symbol="BTCUSD", marker="__ob_snapshot_valid__"),
        ),
        ("ob_updates", _row(3, 110, channel="ob_updates", symbol="ETHUSD", action="snapshot")),
        (
            "_control",
            _row(4, 111, channel="_control", symbol="ETHUSD", marker="__ob_snapshot_valid__"),
        ),
        ("trades", _row(5, 200, channel="trades", symbol="BTCUSD")),
        (
            "_control",
            _row(6, 400, channel="_control", symbol="BTCUSD", marker="__ob_sequence_gap__"),
        ),
        (
            "ob_updates",
            _row(
                7,
                500,
                channel="ob_updates",
                symbol="BTCUSD",
                action="snapshot",
                connection="conn_000002",
            ),
        ),
        (
            "_control",
            _row(
                8,
                501,
                channel="_control",
                symbol="BTCUSD",
                marker="__ob_snapshot_valid__",
                connection="conn_000002",
            ),
        ),
        (
            "ob_updates",
            _row(
                9,
                510,
                channel="ob_updates",
                symbol="ETHUSD",
                action="snapshot",
                connection="conn_000002",
            ),
        ),
        (
            "_control",
            _row(
                10,
                511,
                channel="_control",
                symbol="ETHUSD",
                marker="__ob_snapshot_valid__",
                connection="conn_000002",
            ),
        ),
        ("trades", _row(11, 600, channel="trades", symbol="ETHUSD", connection="conn_000002")),
    ]
    _write(root, rows)

    payload = qualify_event_continuity(
        root,
        output_path=tmp_path / "result.json",
        cache_path=tmp_path / "cache.json",
        code_version="test",
    )

    assert payload["epochs"]["count"] == 2
    assert payload["epochs"]["last_reset"]["reason"] == "__ob_sequence_gap__"
    assert payload["epochs"]["latest"]["connection_id"] == "conn_000002"
    assert payload["epochs"]["qualification_basis"] == "latest_recoverable_epoch"
    assert payload["qualification"]["qualified_events"] == 2
    assert payload["qualification"]["qualified_days"] < 1


def test_verified_reconnect_continues_same_session_epoch(tmp_path: Path) -> None:
    root = tmp_path / "events"
    rows = [
        ("ob_updates", _row(1, 100, channel="ob_updates", symbol="BTCUSD", action="snapshot")),
        (
            "_control",
            _row(2, 101, channel="_control", symbol="BTCUSD", marker="__ob_snapshot_valid__"),
        ),
        ("ob_updates", _row(3, 110, channel="ob_updates", symbol="ETHUSD", action="snapshot")),
        (
            "_control",
            _row(4, 111, channel="_control", symbol="ETHUSD", marker="__ob_snapshot_valid__"),
        ),
        ("trades", _row(5, 200, channel="trades", symbol="BTCUSD")),
        ("_control", _row(6, 400, channel="_control", marker="__disconnected__")),
        (
            "ob_updates",
            _row(
                7,
                500,
                channel="ob_updates",
                symbol="BTCUSD",
                action="snapshot",
                connection="conn_000002",
            ),
        ),
        (
            "_control",
            _row(
                8,
                501,
                channel="_control",
                symbol="BTCUSD",
                marker="__ob_snapshot_valid__",
                connection="conn_000002",
            ),
        ),
        (
            "ob_updates",
            _row(
                9,
                510,
                channel="ob_updates",
                symbol="ETHUSD",
                action="snapshot",
                connection="conn_000002",
            ),
        ),
        (
            "_control",
            _row(
                10,
                511,
                channel="_control",
                symbol="ETHUSD",
                marker="__ob_snapshot_valid__",
                connection="conn_000002",
            ),
        ),
        ("trades", _row(11, 600, channel="trades", symbol="ETHUSD", connection="conn_000002")),
    ]
    _write(root, rows)

    payload = qualify_event_continuity(
        root,
        output_path=tmp_path / "result.json",
        cache_path=tmp_path / "cache.json",
        code_version="test",
    )

    assert payload["epochs"]["count"] == 1
    assert payload["epochs"]["latest"]["connection_ids"] == [
        "conn_000001",
        "conn_000002",
    ]
    assert payload["epochs"]["latest"]["recovered_connections"] == 2
    assert payload["epochs"]["last_reset"] is None
    assert payload["qualification"]["estimated_ready_at"] is None
    assert payload["qualification"]["estimate_status"] == "INSUFFICIENT_RATE_SAMPLE"


def test_active_partials_are_not_misclassified_as_crash_files(tmp_path: Path) -> None:
    root = tmp_path / "events"
    root.mkdir()
    (root / ".trades_rec_live_0001.jsonl.gz.partial").write_bytes(b"active")
    (root / "orphan.jsonl.gz.partial").write_bytes(b"orphan")
    (root / "_recorder_status.json").write_text(
        json.dumps(
            {
                "state": "RECORDING",
                "session_id": "rec_live",
                "updated_at": datetime.now(UTC).isoformat(),
                "stats_seconds": 30,
            }
        )
    )

    payload = qualify_event_continuity(
        root,
        output_path=tmp_path / "result.json",
        cache_path=tmp_path / "cache.json",
        code_version="test",
    )

    assert payload["audit"]["active_partial_files"] == 1
    assert payload["audit"]["orphan_partial_files"] == 1
    assert payload["qualification"]["data_ready"] is False


def test_shard_verification_cache_is_reused(tmp_path: Path) -> None:
    root = tmp_path / "events"
    _write(
        root,
        [
            ("ob_updates", _row(1, 100, channel="ob_updates", symbol="BTCUSD", action="snapshot")),
            (
                "_control",
                _row(2, 101, channel="_control", symbol="BTCUSD", marker="__ob_snapshot_valid__"),
            ),
        ],
    )
    cache = tmp_path / "cache.json"
    first = qualify_event_continuity(
        root,
        output_path=tmp_path / "first.json",
        cache_path=cache,
        code_version="test",
    )
    second = qualify_event_continuity(
        root,
        output_path=tmp_path / "second.json",
        cache_path=cache,
        code_version="test",
    )

    assert first["audit"]["newly_verified_shards"] > 0
    assert second["audit"]["newly_verified_shards"] == 0
    assert second["audit"]["reused_verified_shards"] == first["audit"]["finalized_shards"]
