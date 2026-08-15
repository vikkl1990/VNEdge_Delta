from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from vnedge.dashboard.sse_health import build_health_payload, health_event_generator


def _snapshot() -> dict:
    return {
        "generated_at": "2026-08-10T10:00:00+00:00",
        "architecture": {
            "components": {
                "scanner_engine": "available_all_hypotheses_rejected_and_disabled"
            }
        },
        "rows": [
            {
                "strategy_id": "delta_scalper_engine_v1",
                "symbol": "BTCUSD",
                "latest_eval_ts": "2026-08-10T10:00:00+00:00",
                "latest_eval": {
                    "journal_write_success": True,
                    "signal": None,
                    "l2_confirmation": {"status": "fresh", "imbalance": -0.25},
                },
            },
            {
                "strategy_id": "delta_scalper_engine_v1",
                "symbol": "ETHUSD",
                "latest_eval_ts": "2026-08-10T10:00:01+00:00",
                "latest_eval": {
                    "journal_write_success": True,
                    "signal": None,
                    "l2_confirmation": {"status": "fresh", "imbalance": 0.2},
                },
            },
        ],
        "can_trade": False,
        "can_promote": False,
    }


def test_health_payload_is_compact_truthful_and_locked():
    recorder = {
        "state": "RECORDING",
        "connections": 1,
        "events": 1234,
        "updated_at": "2026-08-10T10:00:02+00:00",
        "feed_delay_us": {"p95_us": 450_000, "max_us": 2_000_000},
        "gap_guard": {"healthy": True, "integrity_faults": 0},
    }

    payload = build_health_payload(
        _snapshot(), recorder, now=datetime(2026, 8, 10, 10, 0, 10, tzinfo=UTC)
    )

    assert payload["snapshot_available"] is True
    assert payload["snapshot_fresh"] is True
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False
    assert payload["order_route"] == "absent"
    assert payload["enabled_scanners"] == []
    assert payload["journal_ok"] is True
    assert payload["p95_feed_lag_ms"] == 450.0
    assert payload["max_feed_lag_ms"] == 2000.0
    assert payload["gap_guard"] == {"healthy": True, "integrity_faults": 0}
    assert payload["markets"]["BTCUSD"]["l2_fresh"] is True
    assert payload["markets"]["BTCUSD"]["l2_imbalance"] == -0.25
    assert payload["markets"]["BTCUSD"]["price"] is None
    assert payload["markets"]["BTCUSD"]["bias_4h"] is None
    assert payload["per_timeframe_feed_lag_available"] is False


def test_health_payload_fails_closed_when_snapshot_is_missing():
    payload = build_health_payload({}, {})

    assert payload["error"] == "snapshot_unavailable"
    assert payload["snapshot_available"] is False
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False
    assert payload["enabled_scanners"] == []


def test_health_payload_marks_old_snapshot_and_l2_as_stale():
    payload = build_health_payload(
        _snapshot(),
        {"updated_at": "2026-08-10T11:00:00+00:00"},
        now=datetime(2026, 8, 10, 11, 0, 0, tzinfo=UTC),
    )

    assert payload["snapshot_available"] is True
    assert payload["snapshot_fresh"] is False
    assert payload["error"] == "snapshot_stale"
    assert payload["markets"]["BTCUSD"]["l2_fresh"] is False
    assert payload["markets"]["BTCUSD"]["l2_status"] == "snapshot_stale"


class _OneEventRequest:
    def __init__(self) -> None:
        self.calls = 0

    async def is_disconnected(self) -> bool:
        self.calls += 1
        return self.calls > 1


def test_health_event_generator_emits_named_compact_event():
    async def run() -> str:
        generator = health_event_generator(
            _OneEventRequest(),
            lambda: (_snapshot(), {"state": "RECORDING"}),
            interval_seconds=0,
        )
        try:
            return await anext(generator)
        finally:
            await generator.aclose()

    event = asyncio.run(run())
    assert event.startswith("event: health\ndata: ")
    payload = json.loads(event.split("data: ", 1)[1])
    assert payload["schema_version"] == "vnedge.delta_health_stream.v1"
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False
    assert payload["stream_ts"]


def test_health_payload_merges_event_tape_and_reconnect_truth():
    recorder = {
        "state": "RECORDING",
        "events": 50,
        "connection": {
            "connected": True,
            "attempts": 3,
            "reconnects": 2,
            "disconnects": 2,
            "disconnect_reasons": {"ConnectionClosed": 2},
        },
        "feed_delay_by_channel": {"trades": {"p95_us": 25_000}},
        "feed_timestamp_quality": {"negative_samples": 4},
    }
    trigger = {
        "market_states": {
            "BTCUSD": {
                "price": 70_000.0,
                "book_age_ms": 12.0,
                "htf": {"available": False, "bias": None},
            }
        },
        "funnel": {"events": 50, "counterfactual_observations": 3},
        "rejection_reasons": {"no_signal": 10},
        "counterfactual_absorption": {"open": 1, "completed": 2},
    }

    payload = build_health_payload(
        _snapshot(),
        recorder,
        trigger,
        now=datetime(2026, 8, 10, 10, 0, 10, tzinfo=UTC),
    )

    assert payload["markets"]["BTCUSD"]["price"] == 70_000.0
    assert payload["markets"]["BTCUSD"]["l2_age_ms"] == 12.0
    assert payload["markets"]["BTCUSD"]["mtf_status"] == "not_published"
    assert payload["recorder"]["connected"] is True
    assert payload["recorder"]["reconnects"] == 2
    assert payload["event_funnel"]["counterfactual_observations"] == 3
    assert payload["counterfactual_absorption"]["open"] == 1
    assert payload["can_trade"] is False
