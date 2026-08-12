"""Deterministic Delta event replay, validation, holdout, and safety contracts."""

from __future__ import annotations

import hashlib
import json
import zlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from vnedge.exchange.delta_event_recorder import DeltaEventRecorderConfig, RotatingEventWriter
from vnedge.execution.journal import DecisionJournal
from vnedge.replay.engine import EventReplayEngine
from vnedge.replay.models import HoldoutManifest, RecordedEvent, ReplayConfig, ReplayWindow
from vnedge.replay.outcomes import ReplayForwardTracker
from vnedge.replay.store import DeltaShardEventStore
from vnedge.replay.validator import validate_recorded_events
from vnedge.scalping.delta_engine.absorption import AbsorptionDetectorConfig
from vnedge.scalping.delta_engine.event_trigger import (
    EventDrivenTriggerLayer,
    EventTriggerConfig,
    SustainedFlowImbalanceScanner,
)
from vnedge.scalping.delta_engine.fee_model import DeltaFeeModel
from vnedge.scalping.delta_engine.signal_generator import SignalGateConfig
from vnedge.scalping.delta_engine.types import Side, SignalCandidate

BASE = datetime(2026, 8, 9, 0, 0, tzinfo=UTC)
BASE_US = int(BASE.timestamp() * 1_000_000)


def checksum(asks: list[list[str]], bids: list[list[str]]) -> int:
    asks_text = ",".join(f"{price}:{size}" for price, size in asks[:10])
    bids_text = ",".join(f"{price}:{size}" for price, size in bids[:10])
    return zlib.crc32(f"{asks_text}|{bids_text}".encode()) & 0xFFFFFFFF


def envelope(
    index: int,
    offset_ms: int,
    channel: str,
    message: dict[str, object],
) -> dict[str, object]:
    exchange_us = BASE_US + offset_ms * 1_000
    return {
        "schema_version": "vnedge.delta_public_event.v1",
        "session_id": "replay_fixture",
        "connection_id": "conn_000001",
        "event_index": index,
        "local_recv_ns": exchange_us * 1_000 + 20_000_000,
        "local_monotonic_ns": 10_000_000_000 + offset_ms * 1_000_000,
        "record_kind": "exchange",
        "channel": channel,
        "symbol": "BTCUSD",
        "exchange_timestamp_us": exchange_us,
        "action": message.get("action"),
        "sequence": message.get("seq"),
        "checksum": message.get("cs"),
        "raw_text": json.dumps(message, separators=(",", ":")),
        "research_only": True,
        "can_trade": False,
        "can_promote": False,
    }


def write_fixture(root: Path, *, gap: bool = False, bad_checksum: bool = False) -> None:
    asks = [["101", "1"]]
    bids = [["100", "10"]]
    snapshot = {
        "type": "ob_updates",
        "action": "snapshot",
        "sy": "BTCUSD",
        "ts": BASE_US,
        "seq": 1,
        "a": asks,
        "b": bids,
        "cs": checksum(asks, bids),
    }
    if bad_checksum:
        snapshot["cs"] = int(snapshot["cs"]) + 1
    rows: list[tuple[str, dict[str, object]]] = [
        ("ob_updates", envelope(1, 0, "ob_updates", snapshot))
    ]
    if gap:
        updated_bids = [["100", "10"], ["99", "1"]]
        update = {
            "type": "ob_updates",
            "action": "update",
            "sy": "BTCUSD",
            "ts": BASE_US + 50_000,
            "seq": 3,
            "a": [],
            "b": [["99", "1"]],
            "cs": checksum(asks, updated_bids),
        }
        rows.append(("ob_updates", envelope(2, 50, "ob_updates", update)))
        next_index = 3
    else:
        next_index = 2
    for position, (offset_ms, price) in enumerate(
        ((100, "100.5"), (300, "100.5"), (500, "100.5"), (600, "100.5"), (800, "101")),
        next_index,
    ):
        message = {
            "type": "trades",
            "s": "1",
            "p": price,
            "r": "t",
            "sy": "BTCUSD",
            "t": BASE_US + offset_ms * 1_000,
            "ts": BASE_US + offset_ms * 1_000,
        }
        rows.append(("trades", envelope(position, offset_ms, "trades", message)))
    writer = RotatingEventWriter(
        DeltaEventRecorderConfig(
            symbols=("BTCUSD",),
            channels=("ob_updates", "trades"),
            output_dir=root,
            compression="gzip",
        ),
        session_id="replay_fixture",
    )
    writer.write_batch(rows)
    writer.close()


def replay_config(**overrides: object) -> ReplayConfig:
    payload: dict[str, object] = {
        "symbols": ("BTCUSD",),
        "start_ts_us": BASE_US,
        "end_ts_us": BASE_US + 1_000_000,
        "channels": ("ob_updates", "trades"),
        "code_version": "test-commit",
    }
    payload.update(overrides)
    return ReplayConfig(**payload)


def trigger_factory(
    journal: DecisionJournal | None,
    enable_scanner: bool,
    random_seed: int,
) -> EventDrivenTriggerLayer:
    assert random_seed == 42
    fee = DeltaFeeModel(default_slippage_bps_per_leg=1.5)
    scanners = (
        (SustainedFlowImbalanceScanner(fee, probability_prior=0.90),)
        if enable_scanner
        else ()
    )
    return EventDrivenTriggerLayer(
        scanners,
        config=EventTriggerConfig(
            confirmation_ms=400,
            min_eval_interval_ms=50,
            minimum_confirmation_samples=3,
            max_book_age_ms=1_000,
            enabled_symbols=("BTCUSD",),
            absorption=AbsorptionDetectorConfig(enabled=False),
        ),
        gates=SignalGateConfig(
            min_expectancy_bps=8,
            min_probability=0.70,
            min_confidence=0.60,
            allowed_symbols=("BTCUSD",),
        ),
        journal=journal,
    )


def engine(root: Path, output: Path, *, manifest: HoldoutManifest | None = None):
    return EventReplayEngine(
        DeltaShardEventStore(root),
        trigger_factory,
        output_dir=output,
        holdout_manifest=manifest,
        current_code_version="test-commit",
    )


def test_replay_is_deterministic_and_uses_shared_scanner_path(tmp_path: Path) -> None:
    event_root = tmp_path / "events"
    write_fixture(event_root)
    runner = engine(event_root, tmp_path / "outputs")

    first = runner.replay(replay_config())
    second = runner.replay(replay_config())

    assert first.validation.passed
    assert first.events_processed == 6
    assert first.candidates_emitted == 1
    assert first.deterministic_hash == second.deterministic_hash
    assert first.deterministic_hash != "0" * 64
    assert first.summary_metrics["selected_candidates"] == 1
    assert first.summary_metrics["event_forward_outcomes"] == 1
    assert first.summary_metrics["event_net_expectancy_bps"] > 0
    assert first.research_only and not first.can_trade and not first.can_promote
    assert first.to_dict()["order_route"] == "absent"
    assert Path(first.journal_path or "").is_file()
    assert Path(first.result_path or "").is_file()


def test_feature_only_replay_runs_without_scanner_candidates(tmp_path: Path) -> None:
    event_root = tmp_path / "events"
    write_fixture(event_root)
    result = engine(event_root, tmp_path / "outputs").replay(
        replay_config(enable_scanner=False, journal_mode="none")
    )
    assert result.events_processed == 6
    assert result.candidates_emitted == 0
    assert result.journal_path is None
    assert result.summary_metrics["feature_snapshots_hashed"] > 0
    assert result.summary_metrics["events_hashed"] == 6
    assert result.deterministic_hash != hashlib.sha256(b"").hexdigest()


def test_determinism_proof_hashes_feature_state_and_is_persisted(tmp_path: Path) -> None:
    event_root = tmp_path / "events"
    write_fixture(event_root)
    runner = engine(event_root, tmp_path / "outputs")
    config = replay_config(enable_scanner=False, journal_mode="none")
    proof_path = tmp_path / "proofs" / "latest.json"

    proof = runner.verify_determinism(config, proof_path=proof_path)
    persisted = json.loads(proof_path.read_text())

    assert proof.passed is True
    assert proof.first_events == proof.second_events == 6
    assert proof.first_feature_snapshots > 0
    assert proof.first_feature_snapshots == proof.second_feature_snapshots
    assert proof.first_hash == proof.second_hash
    assert persisted["passed"] is True
    assert persisted["can_trade"] is False
    assert persisted["can_promote"] is False
    assert persisted["order_route"] == "absent"


def test_empty_window_cannot_produce_determinism_pass(tmp_path: Path) -> None:
    event_root = tmp_path / "events"
    write_fixture(event_root)
    runner = engine(event_root, tmp_path / "outputs")
    config = replay_config(
        start_ts_us=BASE_US + 2_000_000,
        end_ts_us=BASE_US + 3_000_000,
        enable_scanner=False,
        journal_mode="none",
        fail_on_integrity_error=False,
    )

    proof = runner.verify_determinism(config)

    assert proof.first_events == 0
    assert proof.passed is False


def test_validation_detects_sequence_gap_and_blocks_replay(tmp_path: Path) -> None:
    event_root = tmp_path / "events"
    write_fixture(event_root, gap=True)
    runner = engine(event_root, tmp_path / "outputs")
    report = runner.validate_recording(replay_config())
    assert not report.passed
    assert report.sequence_gaps == 1
    with pytest.raises(ValueError, match="validation failed"):
        runner.replay(replay_config())


def test_validation_detects_semantic_book_checksum_failure(tmp_path: Path) -> None:
    event_root = tmp_path / "events"
    write_fixture(event_root, bad_checksum=True)
    report = engine(event_root, tmp_path / "outputs").validate_recording(replay_config())
    assert not report.passed
    assert report.checksum_failures == 1


def test_symbol_window_validation_convenience_api(tmp_path: Path) -> None:
    event_root = tmp_path / "events"
    write_fixture(event_root)
    report = engine(event_root, tmp_path / "outputs").validate_recording(
        "BTCUSD", BASE_US, BASE_US + 1_000_000
    )
    assert report.passed
    assert report.events == 6


def test_arbitrary_window_warms_book_from_latest_prior_snapshot(tmp_path: Path) -> None:
    event_root = tmp_path / "events"
    write_fixture(event_root)
    config = replay_config(
        start_ts_us=BASE_US + 250_000,
        end_ts_us=BASE_US + 1_000_000,
        enable_scanner=False,
        journal_mode="none",
    )

    stored = list(DeltaShardEventStore(event_root).iter_events(config))
    result = engine(event_root, tmp_path / "outputs").replay(config)

    assert stored[0].envelope["replay_warmup"] is True
    assert stored[0].raw_message["action"] == "snapshot"
    assert result.validation.passed is True
    assert result.events_processed == 4
    assert result.summary_metrics["l2_warmup_events"] == 1


def test_shard_manifest_tampering_is_rejected(tmp_path: Path) -> None:
    event_root = tmp_path / "events"
    write_fixture(event_root)
    shard = next(event_root.rglob("*.jsonl.gz"))
    shard.write_bytes(shard.read_bytes() + b"tamper")
    with pytest.raises(ValueError, match="manifest verification"):
        list(DeltaShardEventStore(event_root).iter_events(replay_config()))


def test_mixed_symbol_replay_preserves_receive_order_despite_exchange_regression(
    tmp_path: Path,
) -> None:
    root = tmp_path / "events"
    btc_asks, btc_bids = [["101", "1"]], [["100", "2"]]
    eth_asks, eth_bids = [["3001", "1"]], [["3000", "2"]]
    btc_message = {
        "type": "ob_updates",
        "action": "snapshot",
        "sy": "BTCUSD",
        "ts": BASE_US + 100_000,
        "seq": 1,
        "a": btc_asks,
        "b": btc_bids,
        "cs": checksum(btc_asks, btc_bids),
    }
    eth_message = {
        "type": "ob_updates",
        "action": "snapshot",
        "sy": "ETHUSD",
        "ts": BASE_US + 50_000,
        "seq": 1,
        "a": eth_asks,
        "b": eth_bids,
        "cs": checksum(eth_asks, eth_bids),
    }
    btc = envelope(1, 0, "ob_updates", btc_message)
    btc["symbol"] = "BTCUSD"
    btc["exchange_timestamp_us"] = BASE_US + 100_000
    eth = envelope(2, 1, "ob_updates", eth_message)
    eth["symbol"] = "ETHUSD"
    eth["exchange_timestamp_us"] = BASE_US + 50_000
    writer = RotatingEventWriter(
        DeltaEventRecorderConfig(
            symbols=("BTCUSD", "ETHUSD"),
            channels=("ob_updates",),
            output_dir=root,
            compression="gzip",
        ),
        session_id="mixed_symbols",
    )
    writer.write_batch([("ob_updates", btc), ("ob_updates", eth)])
    writer.close()

    config = ReplayConfig(
        symbols=("BTCUSD", "ETHUSD"),
        start_ts_us=BASE_US,
        end_ts_us=BASE_US + 1_000_000,
        channels=("ob_updates",),
        enable_scanner=False,
        code_version="test-commit",
    )
    events = list(DeltaShardEventStore(root).iter_events(config))
    report = validate_recorded_events(events)

    assert [event.symbol for event in events] == ["BTCUSD", "ETHUSD"]
    assert report.passed
    assert report.timestamp_regressions == 1


def test_code_version_and_sealed_holdout_fail_closed(tmp_path: Path) -> None:
    event_root = tmp_path / "events"
    write_fixture(event_root)
    sealed = ReplayWindow("sealed-tail", BASE_US, BASE_US + 2_000_000, ("BTCUSD",))
    development = ReplayWindow("seen-selection", BASE_US, BASE_US + 500_000, ("BTCUSD",))
    runner = engine(
        event_root,
        tmp_path / "outputs",
        manifest=HoldoutManifest((development,), (sealed,)),
    )
    with pytest.raises(ValueError, match="version mismatch"):
        runner.replay(replay_config(code_version="wrong"))
    with pytest.raises(PermissionError, match="development"):
        runner.replay(replay_config(sealed_holdout=True))

    with pytest.raises(PermissionError, match="window is sealed"):
        runner.replay(replay_config(journal_mode="none"))

    clean_manifest = HoldoutManifest((), (sealed,))
    clean = engine(event_root, tmp_path / "clean", manifest=clean_manifest)
    result = clean.replay(replay_config(sealed_holdout=True, journal_mode="none"))
    assert result.validation.passed


def test_replay_config_rejects_scanner_without_features() -> None:
    with pytest.raises(ValueError, match="requires the feature engine"):
        replay_config(enable_feature_engine=False, enable_scanner=True)


def test_generic_forward_tracker_uses_next_trade_and_records_stop() -> None:
    tracker = ReplayForwardTracker()
    candidate = SignalCandidate(
        scanner_id="test_event_scanner",
        symbol="BTCUSD",
        side=Side.LONG,
        decision_ts=BASE,
        entry_price=100.0,
        stop_loss=99.9,
        take_profits=(100.2,),
        time_stop_seconds=30,
        expected_hold_seconds=30,
        expected_move_bps=20.0,
        raw_expectancy_bps=20.0,
        modeled_cost_bps=3.0,
        fee_adjusted_expectancy_bps=17.0,
        scalper_probability=0.9,
        confidence=0.9,
        entry_is_maker=False,
    )
    assert tracker.register(candidate, decision_ts_us=BASE_US)
    entry_message = {
        "type": "trades",
        "sy": "BTCUSD",
        "t": BASE_US + 100_000,
        "ts": BASE_US + 100_000,
        "p": "100",
        "s": "1",
        "r": "t",
    }
    stop_message = {
        **entry_message,
        "t": BASE_US + 200_000,
        "ts": BASE_US + 200_000,
        "p": "99.8",
    }
    entry = RecordedEvent.from_envelope(envelope(1, 100, "trades", entry_message))
    stop = RecordedEvent.from_envelope(envelope(2, 200, "trades", stop_message))
    assert tracker.on_event(entry) == ()
    outcome = tracker.on_event(stop)[0]
    assert outcome.entry_price == pytest.approx(100.0)
    assert outcome.exit_reason == "stop"
    assert outcome.gross_bps == pytest.approx(-20.0)
    assert outcome.net_bps == pytest.approx(-23.0)
    assert outcome.can_trade is False
