"""Bounded, closed-only multi-timeframe candle state."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from vnedge.scalping.delta_engine.types import Candle

TIMEFRAME_SECONDS: dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1_800,
    "1h": 3_600,
    "4h": 14_400,
}


class MultiTimeframeCandleStore:
    """Stores only bars whose close has been proven.

    ``append_closed`` expects ``Candle.ts`` to be the close timestamp.  Delta's
    websocket callback carries the start timestamp, so ``from_delta_row`` is
    the canonical adapter and adds the timeframe duration exactly once.
    """

    def __init__(self, *, max_bars_per_timeframe: int = 600) -> None:
        if max_bars_per_timeframe < 2:
            raise ValueError("max_bars_per_timeframe must be >= 2")
        self._max_bars = max_bars_per_timeframe
        self._rows: dict[tuple[str, str], deque[Candle]] = defaultdict(
            lambda: deque(maxlen=self._max_bars)
        )
        self._callbacks: list[Callable[[str, Candle], None]] = []

    def append_closed(self, symbol: str, candle: Candle, *, observed_at: datetime) -> bool:
        current = observed_at.replace(tzinfo=UTC) if observed_at.tzinfo is None else observed_at
        if candle.ts > current.astimezone(UTC):
            raise ValueError("cannot store a candle that has not closed")
        key = (symbol.upper(), candle.tf)
        rows = self._rows[key]
        if rows and candle.ts < rows[-1].ts:
            raise ValueError("out-of-order closed candle")
        if rows and candle.ts == rows[-1].ts:
            if candle == rows[-1]:
                return False
            raise ValueError("conflicting duplicate closed candle")
        rows.append(candle)
        for callback in tuple(self._callbacks):
            callback(symbol.upper(), candle)
        return True

    def on_closed_candle(self, callback: Callable[[str, Candle], None]) -> None:
        """Register a synchronous callback for the owning asyncio event loop."""
        self._callbacks.append(callback)

    def from_delta_row(
        self,
        symbol: str,
        timeframe: str,
        row: list[float] | tuple[float, ...],
        *,
        observed_at: datetime,
    ) -> bool:
        """Ingest Delta ``[start_ms,o,h,l,c,v]`` emitted on rollover."""
        if timeframe not in TIMEFRAME_SECONDS:
            raise ValueError(f"unsupported timeframe: {timeframe}")
        if len(row) < 6:
            raise ValueError("Delta candle row must have six fields")
        start = datetime.fromtimestamp(float(row[0]) / 1000.0, tz=UTC)
        candle = Candle(
            ts=start + timedelta(seconds=TIMEFRAME_SECONDS[timeframe]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
            tf=timeframe,
        )
        # This adapter is called only by DeltaPublicWsClient.on_candle, which
        # emits the previous bar after observing a newer interval. That
        # rollover is stronger close proof than a locally skewed wall clock.
        proven_at = max(observed_at, candle.ts)
        return self.append_closed(symbol, candle, observed_at=proven_at)

    def recent(self, symbol: str, timeframe: str, limit: int | None = None) -> tuple[Candle, ...]:
        rows = tuple(self._rows.get((symbol.upper(), timeframe), ()))
        if limit is None:
            return rows
        if limit < 0:
            raise ValueError("limit cannot be negative")
        return rows[-limit:] if limit else ()

    def latest(self, symbol: str, timeframe: str) -> Candle | None:
        rows = self._rows.get((symbol.upper(), timeframe))
        return rows[-1] if rows else None

    def get_candles(self, symbol: str, timeframe: str, n: int = 200) -> tuple[Candle, ...]:
        return self.recent(symbol, timeframe, n)

    def get_latest_closed(self, symbol: str, timeframe: str) -> Candle | None:
        return self.latest(symbol, timeframe)

    def snapshot(
        self,
        symbol: str,
        timeframes: tuple[str, ...] = ("1m", "5m", "15m", "1h", "4h"),
        *,
        limit: int | None = None,
        limits: dict[str, int] | None = None,
    ) -> dict[str, tuple[Candle, ...]]:
        bounded = limits or {"1m": 96, "5m": 96, "15m": 64, "1h": 96, "4h": 64}
        return {
            tf: self.recent(symbol, tf, limit if limit is not None else bounded.get(tf, 64))
            for tf in timeframes
        }

    def ready(
        self,
        symbol: str,
        required: dict[str, int] | None = None,
    ) -> bool:
        needed = required or {"1m": 30, "5m": 30, "15m": 24, "1h": 36, "4h": 12}
        return all(len(self.recent(symbol, tf)) >= count for tf, count in needed.items())

    def reset_symbol(self, symbol: str) -> None:
        """Drop all rolling state after a feed gap; no feature may bridge it."""
        native = symbol.upper()
        for key in [key for key in self._rows if key[0] == native]:
            del self._rows[key]


class ClosedCandleAggregator:
    """Causally aggregate closed 1m candles into higher closed bars."""

    def __init__(self, timeframes: tuple[str, ...] = ("5m", "15m", "1h", "4h")) -> None:
        if any(tf not in TIMEFRAME_SECONDS or tf == "1m" for tf in timeframes):
            raise ValueError("aggregator timeframes must be supported and above 1m")
        self.timeframes = timeframes
        self._pending: dict[tuple[str, str], list[Candle]] = defaultdict(list)

    @staticmethod
    def _bucket_end(ts: datetime, seconds: int) -> datetime:
        epoch = int(ts.timestamp())
        end = ((epoch - 1) // seconds + 1) * seconds
        return datetime.fromtimestamp(end, tz=UTC)

    def on_one_minute(self, symbol: str, candle: Candle) -> tuple[Candle, ...]:
        if candle.tf != "1m":
            raise ValueError("aggregator accepts 1m candles only")
        emitted: list[Candle] = []
        for tf in self.timeframes:
            seconds = TIMEFRAME_SECONDS[tf]
            key = (symbol.upper(), tf)
            rows = self._pending[key]
            bucket_end = self._bucket_end(candle.ts, seconds)
            if rows and self._bucket_end(rows[0].ts, seconds) != bucket_end:
                rows.clear()  # gap or partial bucket: fail closed, never emit it
            rows.append(candle)
            if candle.ts == bucket_end and len(rows) == seconds // 60:
                emitted.append(
                    Candle(
                        ts=bucket_end,
                        open=rows[0].open,
                        high=max(row.high for row in rows),
                        low=min(row.low for row in rows),
                        close=rows[-1].close,
                        volume=sum(row.volume for row in rows),
                        tf=tf,
                    )
                )
                rows.clear()
        return tuple(emitted)
