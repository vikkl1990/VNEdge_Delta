"""Live verified-event research observer remains telemetry-only."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from vnedge.runtime.delta_event_research import DeltaLiveEventResearchObserver
from vnedge.scalping.delta_engine.absorption import AbsorptionInstrumentConfig


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
    assert payload["state"] == "LIVE_SHADOW_OBSERVING"
    assert payload["events_observed"] == 2
    assert payload["research_only"] is True
    assert payload["can_trade"] is False
    assert payload["order_route"] == "absent"
    assert payload["scanner_policy"] == "post_absorption_confirmation_shadow_capital_locked"
    assert payload["enabled_scanners"] == []
    assert payload["paper_simulation"]["enabled"] is False
    assert payload["validated_edge"] is False
    assert payload["counts"]["raw_candidates"] == 0
    assert payload["counts"]["selected"] == 0
    absorption_payload = json.loads(absorption.read_text())
    assert absorption_payload["status"] == "not_configured"


async def test_live_observer_enables_shadow_scanner_without_order_authority(
    tmp_path: Path,
) -> None:
    observer = DeltaLiveEventResearchObserver(
        symbols=("BTCUSD",),
        instruments=(
            AbsorptionInstrumentConfig(
                symbol="BTCUSD",
                tick_size=0.5,
                minimum_aggressive_notional_usd=500,
            ),
        ),
        telemetry_path=tmp_path / "telemetry.json",
        absorption_path=tmp_path / "absorption.json",
        journal_path=tmp_path / "journal.jsonl",
    )
    await observer.publish()
    payload = json.loads((tmp_path / "telemetry.json").read_text())
    assert payload["enabled_scanners"] == [
        "event_absorption_confirmed_reversal_v3_shadow"
    ]
    scanner = payload["scanner_telemetry"][
        "event_absorption_confirmed_reversal_v3_shadow"
    ]
    assert scanner["contract"]["target_bps"] == 45.0
    assert scanner["contract"]["minimum_target_cost_multiple"] == 2.5
    assert scanner["observation_lock_owner"] == "event_trigger_layer_after_shared_gates"
    assert payload["paper_simulation"]["enabled"] is True
    assert payload["paper_simulation"]["uses_real_orders"] is False
    assert payload["paper_simulation"]["benchmark"] == "raw_absorption_detection_v2"
    assert payload["paper_simulation"]["qualified_scanner"] == (
        "event_absorption_confirmed_reversal_v3_shadow"
    )
    assert payload["paper_simulation"]["trade_horizon"] == "scalp"
    assert payload["paper_simulation"]["vertical_barrier_seconds"] == 900
    assert payload["paper_simulation"]["scalper_max_hold_seconds"] == 1800
    assert payload["can_trade"] is False
    assert payload["order_route"] == "absent"
