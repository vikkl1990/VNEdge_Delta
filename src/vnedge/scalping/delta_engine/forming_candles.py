"""Deterministic forming-candle state derived from ticks or closed 1m bars.

The engine deliberately keeps incomplete candles separate from
``MultiTimeframeCandleStore``.  A forming bar is point-in-time context, never a
proven close.  Live and replay callers use the same explicit event timestamp;
there is no wall-clock access in this module.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from vnedge.scalping.delta_engine.candle_store import TIMEFRAME_SECONDS
from vnedge.scalping.delta_engine.types import (
    Candle,
    FormingCandle,
    MultiTimeframeCandleSnapshot,
    TimeframeCandleState,
)

InputMode = Literal["tick", "one_minute"]
DEFAULT_FORMING_TIMEFRAMES = ("1m", "3m", "5m", "15m", "30m", "1h", "4h")


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _bucket_start(ts: datetime, seconds: int) -> datetime:
    epoch = int(_utc(ts).timestamp())
    return datetime.fromtimestamp((epoch // seconds) * seconds, tz=UTC)


@dataclass
class _MutableForming:
    timeframe: str
    start_ts: datetime
    end_ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    last_update_ts: datetime
    local_received_at: datetime | None = None
    feed_delay_ms: float | None = None
    source_observations: int = 1
    continuity_ok: bool = True

    def update_price(
        self,
        price: float,
        volume: float,
        ts: datetime,
        local_received_at: datetime | None,
        feed_delay_ms: float | None,
    ) -> None:
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.volume += volume
        self.last_update_ts = ts
        self.local_received_at = local_received_at
        self.feed_delay_ms = feed_delay_ms
        self.source_observations += 1

    def update_candle(self, candle: Candle, observed_at: datetime) -> None:
        self.high = max(self.high, candle.high)
        self.low = min(self.low, candle.low)
        self.close = candle.close
        self.volume += candle.volume
        self.last_update_ts = candle.ts
        self.local_received_at = observed_at
        self.feed_delay_ms = (observed_at - candle.ts).total_seconds() * 1_000.0
        self.source_observations += 1


class MultiTimeframeCandleEngine:
    """Maintain completed and forming candles without lookahead.

    Choose one input mode per instance.  Mixing ticks and closed-minute bars is
    rejected because it would double-count volume and make replay/live parity
    ambiguous.
    """

    def __init__(
        self,
        *,
        timeframes: tuple[str, ...] = DEFAULT_FORMING_TIMEFRAMES,
        input_mode: InputMode = "one_minute",
        max_completed_per_timeframe: int = 600,
        dedup_event_ids: int = 100_000,
    ) -> None:
        if input_mode not in {"tick", "one_minute"}:
            raise ValueError("input_mode must be tick or one_minute")
        if max_completed_per_timeframe < 1:
            raise ValueError("max_completed_per_timeframe must be positive")
        if dedup_event_ids < 1:
            raise ValueError("dedup_event_ids must be positive")
        if not timeframes or len(set(timeframes)) != len(timeframes):
            raise ValueError("timeframes must be non-empty and unique")
        unsupported = [item for item in timeframes if item not in TIMEFRAME_SECONDS]
        if unsupported:
            raise ValueError(f"unsupported timeframes: {unsupported}")
        if input_mode == "one_minute" and any(
            TIMEFRAME_SECONDS[item] % 60 for item in timeframes
        ):
            raise ValueError("one_minute mode requires minute-aligned timeframes")

        self.timeframes = tuple(sorted(timeframes, key=TIMEFRAME_SECONDS.__getitem__))
        self.input_mode: InputMode = input_mode
        self._max_completed = max_completed_per_timeframe
        self._completed: dict[tuple[str, str], deque[Candle]] = defaultdict(
            lambda: deque(maxlen=self._max_completed)
        )
        self._forming: dict[tuple[str, str], _MutableForming] = {}
        self._last_event_ts: dict[str, datetime] = {}
        self._last_exchange_ts: dict[str, datetime] = {}
        self._last_local_receive_ts: dict[str, datetime] = {}
        self._last_feed_delay_ms: dict[str, float] = {}
        self._last_minute: dict[str, Candle] = {}
        self._continuity_ok: dict[str, bool] = defaultdict(lambda: True)
        self._last_gap_reason: dict[str, str | None] = defaultdict(lambda: None)
        self._dropped: dict[tuple[str, str], int] = defaultdict(int)
        self._seen_event_ids: dict[str, set[str]] = defaultdict(set)
        self._event_id_order: dict[str, deque[str]] = defaultdict(
            lambda: deque(maxlen=dedup_event_ids)
        )

    @staticmethod
    def _validate_price_volume(price: float, volume: float) -> tuple[float, float]:
        price_value, volume_value = float(price), float(volume)
        if price_value <= 0:
            raise ValueError("price must be positive")
        if volume_value < 0:
            raise ValueError("volume cannot be negative")
        return price_value, volume_value

    def _validate_event_time(self, symbol: str, ts: datetime) -> tuple[str, datetime]:
        native, current = symbol.upper(), _utc(ts)
        previous = self._last_event_ts.get(native)
        if previous is not None and current < previous:
            raise ValueError("event timestamp regression")
        self._last_event_ts[native] = current
        return native, current

    def _deduplicate(self, symbol: str, event_id: str | None) -> bool:
        if event_id is None:
            return False
        key = str(event_id)
        seen, order = self._seen_event_ids[symbol], self._event_id_order[symbol]
        if key in seen:
            return True
        if len(order) == order.maxlen:
            seen.discard(order[0])
        order.append(key)
        seen.add(key)
        return False

    @staticmethod
    def _new_from_tick(
        timeframe: str,
        ts: datetime,
        price: float,
        volume: float,
        local_received_at: datetime | None,
        feed_delay_ms: float | None,
    ) -> _MutableForming:
        start = _bucket_start(ts, TIMEFRAME_SECONDS[timeframe])
        return _MutableForming(
            timeframe=timeframe,
            start_ts=start,
            end_ts=start + timedelta(seconds=TIMEFRAME_SECONDS[timeframe]),
            open=price,
            high=price,
            low=price,
            close=price,
            volume=volume,
            last_update_ts=ts,
            local_received_at=local_received_at,
            feed_delay_ms=feed_delay_ms,
        )

    @staticmethod
    def _new_from_minute(
        timeframe: str, candle: Candle, start: datetime, observed_at: datetime
    ) -> _MutableForming:
        return _MutableForming(
            timeframe=timeframe,
            start_ts=start,
            end_ts=start + timedelta(seconds=TIMEFRAME_SECONDS[timeframe]),
            open=candle.open,
            high=candle.high,
            low=candle.low,
            close=candle.close,
            volume=candle.volume,
            last_update_ts=candle.ts,
            local_received_at=observed_at,
            feed_delay_ms=(observed_at - candle.ts).total_seconds() * 1_000.0,
        )

    def _append_completed(self, symbol: str, candle: Candle) -> None:
        rows = self._completed[(symbol, candle.tf)]
        if rows and candle.ts <= rows[-1].ts:
            if candle == rows[-1]:
                return
            raise ValueError("conflicting or regressing completed candle")
        rows.append(candle)

    def _close_forming(self, symbol: str, state: _MutableForming) -> None:
        self._append_completed(
            symbol,
            Candle(
                ts=state.end_ts,
                open=state.open,
                high=state.high,
                low=state.low,
                close=state.close,
                volume=state.volume,
                tf=state.timeframe,
            ),
        )

    def on_tick(
        self,
        symbol: str,
        *,
        exchange_ts: datetime,
        price: float,
        volume: float = 0.0,
        event_id: str | None = None,
        local_recv_ts: datetime | None = None,
    ) -> bool:
        if self.input_mode != "tick":
            raise RuntimeError("tick input is disabled for this engine")
        price_value, volume_value = self._validate_price_volume(price, volume)
        native = symbol.upper()
        if event_id is not None and str(event_id) in self._seen_event_ids[native]:
            return False
        native, current = self._validate_event_time(symbol, exchange_ts)
        if self._deduplicate(native, event_id):
            return False
        self._last_exchange_ts[native] = current
        local_received = _utc(local_recv_ts) if local_recv_ts is not None else None
        feed_delay_ms = (
            (local_received - current).total_seconds() * 1_000.0
            if local_received is not None
            else None
        )
        if local_received is not None:
            self._last_local_receive_ts[native] = local_received
            self._last_feed_delay_ms[native] = feed_delay_ms

        for timeframe in self.timeframes:
            key = (native, timeframe)
            start = _bucket_start(current, TIMEFRAME_SECONDS[timeframe])
            state = self._forming.get(key)
            if state is None:
                self._forming[key] = self._new_from_tick(
                    timeframe,
                    current,
                    price_value,
                    volume_value,
                    local_received,
                    feed_delay_ms,
                )
                continue
            if start == state.start_ts:
                state.update_price(
                    price_value,
                    volume_value,
                    current,
                    local_received,
                    feed_delay_ms,
                )
                continue
            if start < state.start_ts:
                raise ValueError("tick regressed into an earlier candle bucket")
            if state.continuity_ok:
                self._close_forming(native, state)
            else:
                self._dropped[key] += 1
            if start > state.end_ts:
                self._dropped[key] += int(
                    (start - state.end_ts).total_seconds() // TIMEFRAME_SECONDS[timeframe]
                )
            self._forming[key] = self._new_from_tick(
                timeframe,
                current,
                price_value,
                volume_value,
                local_received,
                feed_delay_ms,
            )
        return True

    def on_closed_one_minute(
        self,
        symbol: str,
        candle: Candle,
        *,
        observed_at: datetime,
    ) -> bool:
        if self.input_mode != "one_minute":
            raise RuntimeError("closed 1m input is disabled for this engine")
        if candle.tf != "1m":
            raise ValueError("one_minute mode accepts closed 1m candles only")
        native, observed = self._validate_event_time(symbol, observed_at)
        if candle.ts > observed:
            raise ValueError("cannot ingest a minute candle before it closes")
        previous = self._last_minute.get(native)
        if previous is not None:
            if candle.ts == previous.ts:
                if candle == previous:
                    return False
                raise ValueError("conflicting duplicate 1m candle")
            if candle.ts < previous.ts:
                raise ValueError("out-of-order 1m candle")
            if candle.ts != previous.ts + timedelta(minutes=1):
                self.mark_gap(
                    native,
                    observed_at=observed,
                    reason=f"1m_gap:{previous.ts.isoformat()}->{candle.ts.isoformat()}",
                )
        self._last_minute[native] = candle
        self._last_exchange_ts[native] = candle.ts
        self._last_local_receive_ts[native] = observed
        self._last_feed_delay_ms[native] = (observed - candle.ts).total_seconds() * 1_000.0

        if "1m" in self.timeframes:
            self._append_completed(native, candle)
        source_start = candle.ts - timedelta(minutes=1)
        for timeframe in self.timeframes:
            if timeframe == "1m":
                continue
            seconds = TIMEFRAME_SECONDS[timeframe]
            key = (native, timeframe)
            start = _bucket_start(source_start, seconds)
            state = self._forming.get(key)
            if state is None or state.start_ts != start:
                if state is not None:
                    self._dropped[key] += 1
                state = self._new_from_minute(timeframe, candle, start, observed)
                # Starting after the bucket boundary means the partial bucket
                # is useful context but can never be promoted as a closed bar.
                state.continuity_ok = source_start == start
                self._forming[key] = state
            else:
                expected_close = state.last_update_ts + timedelta(minutes=1)
                if candle.ts != expected_close:
                    state.continuity_ok = False
                state.update_candle(candle, observed)

            if candle.ts == state.end_ts:
                expected = seconds // 60
                if state.continuity_ok and state.source_observations == expected:
                    self._close_forming(native, state)
                else:
                    self._dropped[key] += 1
                del self._forming[key]
        return True

    def mark_gap(self, symbol: str, *, observed_at: datetime, reason: str) -> None:
        """Invalidate all in-progress bars after an upstream continuity failure."""

        native, current = self._validate_event_time(symbol, observed_at)
        self._continuity_ok[native] = False
        self._last_gap_reason[native] = str(reason)
        for key in [item for item in self._forming if item[0] == native]:
            self._dropped[key] += 1
            del self._forming[key]
        self._last_event_ts[native] = current

    def acknowledge_continuity(self, symbol: str) -> None:
        """Mark the upstream stream healthy again; never restores dropped bars."""

        native = symbol.upper()
        self._continuity_ok[native] = True
        self._last_gap_reason[native] = None

    def reset_symbol(self, symbol: str) -> None:
        native = symbol.upper()
        for mapping in (self._completed, self._forming, self._dropped):
            for key in [item for item in mapping if item[0] == native]:
                del mapping[key]
        self._last_event_ts.pop(native, None)
        self._last_exchange_ts.pop(native, None)
        self._last_local_receive_ts.pop(native, None)
        self._last_feed_delay_ms.pop(native, None)
        self._last_minute.pop(native, None)
        self._continuity_ok.pop(native, None)
        self._last_gap_reason.pop(native, None)
        self._seen_event_ids.pop(native, None)
        self._event_id_order.pop(native, None)

    def completed(
        self, symbol: str, timeframe: str, *, limit: int | None = None
    ) -> tuple[Candle, ...]:
        if timeframe not in self.timeframes:
            raise ValueError(f"timeframe is not configured: {timeframe}")
        rows = tuple(self._completed.get((symbol.upper(), timeframe), ()))
        if limit is None:
            return rows
        if limit < 0:
            raise ValueError("limit cannot be negative")
        return rows[-limit:] if limit else ()

    def snapshot(
        self,
        symbol: str,
        *,
        as_of: datetime | None = None,
        completed_limit: int = 64,
    ) -> MultiTimeframeCandleSnapshot:
        native = symbol.upper()
        latest = self._last_event_ts.get(native)
        if latest is None:
            raise RuntimeError(f"no forming-candle observations for {native}")
        current = _utc(as_of) if as_of is not None else latest
        if current < latest:
            raise ValueError("snapshot as_of predates the latest ingested event")
        states: dict[str, TimeframeCandleState] = {}
        for timeframe in self.timeframes:
            mutable = self._forming.get((native, timeframe))
            forming = None
            if mutable is not None:
                available = min(current, mutable.end_ts)
                elapsed = max(0.0, (available - mutable.start_ts).total_seconds())
                duration = float(TIMEFRAME_SECONDS[timeframe])
                forming = FormingCandle(
                    tf=timeframe,
                    start_ts=mutable.start_ts,
                    end_ts=mutable.end_ts,
                    available_at=available,
                    last_update_ts=mutable.last_update_ts,
                    open=mutable.open,
                    high=mutable.high,
                    low=mutable.low,
                    close=mutable.close,
                    volume=mutable.volume,
                    elapsed_seconds=min(duration, elapsed),
                    remaining_seconds=max(0.0, duration - elapsed),
                    progress=min(1.0, elapsed / duration),
                    source_observations=mutable.source_observations,
                    continuity_ok=mutable.continuity_ok and self._continuity_ok[native],
                    local_received_at=mutable.local_received_at,
                    feed_delay_ms=mutable.feed_delay_ms,
                )
            states[timeframe] = TimeframeCandleState(
                timeframe=timeframe,
                completed=self.completed(native, timeframe, limit=completed_limit),
                forming=forming,
                incomplete_buckets_dropped=self._dropped[(native, timeframe)],
            )
        return MultiTimeframeCandleSnapshot(
            symbol=native,
            available_at=current,
            input_mode=self.input_mode,
            states=states,
            continuity_ok=self._continuity_ok[native],
            last_gap_reason=self._last_gap_reason[native],
            last_exchange_ts=self._last_exchange_ts.get(native),
            last_local_receive_ts=self._last_local_receive_ts.get(native),
            feed_delay_ms=self._last_feed_delay_ms.get(native),
        )
