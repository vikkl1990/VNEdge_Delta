"""Live verified-event research observer remains telemetry-only."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from vnedge.runtime.delta_event_research import DeltaLiveEventResearchObserver


def envelope(
    *, index: int, channel: str, message: dict[str, object], offset_ms: int
) -> dict[str, object]:
    now_us = int(datetime(2026, 8, 10, tzinfo=UTC).timestamp() * 1_000_000)
    return {
        "record_kind": "exchange",
        "channel": channel,
        "symbol": "BTCUSD",
        "event_index": index,
        "exchange_timestamp_us": now_us + offset_ms * 1_000,
        "local_recv_ns": (now_us + offset_ms * 1_000 + 20_000) * 1_000,
        "local_monotonic_ns": 1_000_000_000 + offset_ms * 1_000_000,
        "raw_text": json.dumps(message),
    }


async def test_live_observer_publishes_running_locked_telemetry(tmp_path: Path) -> None:
    telemetry = tmp_path / "telemetry.json"
    absorption = tmp_path / "absorption.json"
    observer = DeltaLiveEventResearchObserver(
        symbols=("BTCUSD",),
        telemetry_path=telemetry,
        absorption_path=absorption,
        journal_path=tmp_path / "journal.jsonl",
        publish_interval_seconds=60,
    )
    snapshot = {
        "type": "ob_updates",
        "action": "snapshot",
        "sy": "BTCUSD",
        "seq": 1,
        "a": [["101", "1"]],
        "b": [["100", "10"]],
    }
    trade = {
        "type": "trades",
        "sy": "BTCUSD",
        "p": "100.5",
        "s": "1",
        "r": "t",
        "t": int(datetime(2026, 8, 10, tzinfo=UTC).timestamp() * 1_000_000),
    }
    await observer.consume(envelope(index=1, channel="ob_updates", message=snapshot, offset_ms=0))
    await observer.consume(envelope(index=2, channel="trades", message=trade, offset_ms=100))
    await observer.publish()

    payload = json.loads(telemetry.read_text())
    assert payload["state"] == "OBSERVING"
    assert payload["events_observed"] == 2
    assert payload["research_only"] is True
    assert payload["can_trade"] is False
    assert payload["order_route"] == "absent"
    absorption_payload = json.loads(absorption.read_text())
    assert absorption_payload["status"] == "not_configured"
