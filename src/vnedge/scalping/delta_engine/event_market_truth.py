"""Causal higher-timeframe truth adapter for the Delta event research path."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from vnedge.data.delta_native_history import fetch_delta_candle_history
from vnedge.scalping.delta_engine.candle_store import (
    TIMEFRAME_SECONDS,
    ClosedCandleAggregator,
    MultiTimeframeCandleStore,
)
from vnedge.scalping.delta_engine.context import MarketContextBuilder
from vnedge.scalping.delta_engine.event_trigger import (
    EventDrivenTriggerLayer,
    HigherTimeframeContext,
    TradeEvent,
)
from vnedge.scalping.delta_engine.types import Candle

TIMEFRAMES = ("1m", "5m", "15m", "1h", "4h")
LOOKBACK_DAYS = {"1m": 2, "5m": 4, "15m": 12, "1h": 45, "4h": 120}


class EventHigherTimeframeContextService:
    """Seed and refresh immutable, proven-closed HTF context from Delta REST.

    This service is deliberately separate from event features: it may veto or
    describe an event candidate, but it never fabricates intra-bar order flow.
    """

    def __init__(
        self,
        trigger: EventDrivenTriggerLayer,
        symbols: tuple[str, ...],
    ) -> None:
        self.trigger = trigger
        self.symbols = tuple(symbol.upper() for symbol in symbols)
        self.store = MultiTimeframeCandleStore(max_bars_per_timeframe=700)
        self.context = MarketContextBuilder(self.store)
        self.aggregator = ClosedCandleAggregator()
        self._minute: dict[str, dict[str, float | datetime]] = {}
        self.last_refresh_at: datetime | None = None
        self.errors: dict[str, str] = {}

    async def seed(self, *, now: datetime | None = None) -> None:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        for symbol in self.symbols:
            await self._refresh_symbol(symbol, current, seed=True)
        self.last_refresh_at = current

    async def refresh(self, *, now: datetime | None = None) -> None:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        for symbol in self.symbols:
            await self._refresh_symbol(symbol, current, seed=False)
        self.last_refresh_at = current

    async def run(self, *, interval_seconds: float = 60.0) -> None:
        if interval_seconds < 10:
            raise ValueError("HTF refresh interval must be at least 10 seconds")
        while True:
            await asyncio.sleep(interval_seconds)
            await self.refresh()

    async def _refresh_symbol(
        self,
        symbol: str,
        current: datetime,
        *,
        seed: bool,
    ) -> None:
        try:
            for timeframe in TIMEFRAMES:
                step = timedelta(seconds=TIMEFRAME_SECONDS[timeframe])
                latest = self.store.latest(symbol, timeframe)
                start = (
                    current - timedelta(days=LOOKBACK_DAYS[timeframe])
                    if seed or latest is None
                    else latest.ts - step
                )
                frame = await fetch_delta_candle_history(
                    symbol,
                    resolution=timeframe,
                    start_s=int(start.timestamp()),
                    end_s=int(current.timestamp()),
                )
                for row in frame.itertuples(index=False):
                    candle = Candle(
                        ts=row.timestamp.to_pydatetime() + step,
                        open=float(row.open),
                        high=float(row.high),
                        low=float(row.low),
                        close=float(row.close),
                        volume=float(row.volume),
                        tf=timeframe,
                    )
                    last = self.store.latest(symbol, timeframe)
                    if candle.ts <= current and (last is None or candle.ts > last.ts):
                        self.store.append_closed(symbol, candle, observed_at=current)
            market = self.context.build(symbol, now=current)
            self._publish_context(symbol, market, current)
            self.errors.pop(symbol, None)
        except Exception as exc:  # noqa: BLE001 - feed failure stays fail-closed
            self.errors[symbol] = f"{type(exc).__name__}: {str(exc)[:240]}"

    def on_trade(self, event: TradeEvent) -> None:
        """Causally roll event trades into closed 1m and higher-timeframe bars."""

        event_ts = event.exchange_ts or event.publish_ts or event.received_at
        minute_start = event_ts.replace(second=0, microsecond=0)
        current = self._minute.get(event.symbol)
        if current is not None and minute_start < current["start"]:
            return
        if current is None or minute_start > current["start"]:
            if current is not None:
                candle = Candle(
                    ts=current["start"] + timedelta(minutes=1),
                    open=float(current["open"]),
                    high=float(current["high"]),
                    low=float(current["low"]),
                    close=float(current["close"]),
                    volume=float(current["volume"]),
                    tf="1m",
                )
                latest = self.store.latest(event.symbol, "1m")
                if latest is None or candle.ts > latest.ts:
                    self.store.append_closed(
                        event.symbol,
                        candle,
                        observed_at=event.received_at,
                    )
                    for higher in self.aggregator.on_one_minute(event.symbol, candle):
                        higher_latest = self.store.latest(event.symbol, higher.tf)
                        if higher_latest is None or higher.ts > higher_latest.ts:
                            self.store.append_closed(
                                event.symbol,
                                higher,
                                observed_at=event.received_at,
                            )
                    try:
                        market = self.context.build(event.symbol, now=event.received_at)
                        self._publish_context(event.symbol, market, event.received_at)
                    except (RuntimeError, ValueError) as exc:
                        self.errors[event.symbol] = f"{type(exc).__name__}: {str(exc)[:240]}"
            current = {
                "start": minute_start,
                "open": event.price,
                "high": event.price,
                "low": event.price,
                "close": event.price,
                "volume": event.size,
            }
            self._minute[event.symbol] = current
            return
        current["high"] = max(float(current["high"]), event.price)
        current["low"] = min(float(current["low"]), event.price)
        current["close"] = event.price
        current["volume"] = float(current["volume"]) + event.size

    def _publish_context(self, symbol: str, market, current: datetime) -> None:
        profile = market.regime_profile
        one_hour = market.candles.get("1h", ())
        four_hour = market.candles.get("4h", ())
        hour_move = one_hour[-1].close / one_hour[-13].close - 1.0 if len(one_hour) >= 13 else 0.0
        four_hour_move = (
            four_hour[-1].close / four_hour[-7].close - 1.0 if len(four_hour) >= 7 else 0.0
        )
        bias = (
            1
            if hour_move > 0 and four_hour_move > 0
            else -1
            if hour_move < 0 and four_hour_move < 0
            else 0
        )
        vwap_rows = one_hour[-24:]
        volume = sum(row.volume for row in vwap_rows)
        vwap = (
            sum(row.close * row.volume for row in vwap_rows) / volume
            if volume > 0
            else market.candles["1m"][-1].close
        )
        price = market.candles["1m"][-1].close
        change = profile.change_point
        if not change.detector_ready:
            cusum = "warming"
        elif change.regime_shift:
            cusum = "shift"
        else:
            cusum = "stable"
        self.trigger.update_higher_timeframe_context(
            HigherTimeframeContext(
                symbol=symbol,
                available_at=current,
                bias=bias,
                regime=(f"{market.regime.value}|{profile.trend.value}|{profile.volatility.value}"),
                vwap_distance_bps=(price / vwap - 1.0) * 10_000.0,
                cusum_state=cusum,
            )
        )
        self.errors.pop(symbol, None)

    def telemetry(self) -> dict[str, object]:
        return {
            "status": "healthy" if not self.errors else "degraded",
            "last_refresh_at": (self.last_refresh_at.isoformat() if self.last_refresh_at else None),
            "errors": dict(self.errors),
            "source": "delta_rest_proven_closed_candles",
            "timeframes": list(TIMEFRAMES),
            "research_only": True,
            "can_trade": False,
        }
