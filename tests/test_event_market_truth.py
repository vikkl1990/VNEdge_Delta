"""Causal higher-timeframe adapter shared by live event research and replay."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd

from vnedge.scalping.delta_engine.event_market_truth import (
    EventHigherTimeframeContextService,
)
from vnedge.scalping.delta_engine.event_trigger import (
    EventDrivenTriggerLayer,
    EventTriggerConfig,
    TradeEvent,
)


async def test_htf_service_seeds_closed_context_and_rolls_event_candle(monkeypatch) -> None:
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    seconds = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400}

    async def fake_history(symbol, *, resolution, start_s, end_s):
        del symbol, start_s, end_s
        step = seconds[resolution]
        count = {"1m": 100, "5m": 100, "15m": 100, "1h": 50, "4h": 20}[resolution]
        starts = [now - timedelta(seconds=step * value) for value in range(count, 0, -1)]
        return pd.DataFrame(
            {
                "timestamp": pd.to_datetime(starts, utc=True),
                "open": [100.0 + index for index in range(count)],
                "high": [101.0 + index for index in range(count)],
                "low": [99.0 + index for index in range(count)],
                "close": [100.5 + index for index in range(count)],
                "volume": [10.0] * count,
            }
        )

    monkeypatch.setattr(
        "vnedge.scalping.delta_engine.event_market_truth.fetch_delta_candle_history",
        fake_history,
    )
    trigger = EventDrivenTriggerLayer(
        (),
        config=EventTriggerConfig(enabled_symbols=("BTCUSD",)),
    )
    service = EventHigherTimeframeContextService(trigger, ("BTCUSD",))
    await service.seed(now=now)
    state = trigger.market_states()["BTCUSD"]
    assert state["htf"]["available"] is True
    assert state["htf"]["bias"] == 1
    assert service.telemetry()["status"] == "healthy"

    service.on_trade(
        TradeEvent(
            symbol="BTCUSD",
            price=205.0,
            size=1.0,
            side="buy",
            exchange_ts=now + timedelta(seconds=5),
            publish_ts=now + timedelta(seconds=5, milliseconds=100),
            received_at=now + timedelta(seconds=5, milliseconds=120),
            received_monotonic_ns=1_000_000,
        )
    )
    service.on_trade(
        TradeEvent(
            symbol="BTCUSD",
            price=206.0,
            size=1.0,
            side="buy",
            exchange_ts=now + timedelta(minutes=1, seconds=5),
            publish_ts=now + timedelta(minutes=1, seconds=5, milliseconds=100),
            received_at=now + timedelta(minutes=1, seconds=5, milliseconds=120),
            received_monotonic_ns=61_000_000_000,
        )
    )
    assert service.store.latest("BTCUSD", "1m").close == 205.0
