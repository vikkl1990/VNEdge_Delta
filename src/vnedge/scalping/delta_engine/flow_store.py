"""Rolling L2 and aggressor-trade state for the Delta scalper context."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from statistics import fmean, pstdev


@dataclass(frozen=True)
class SequenceHealth:
    channel: str
    symbol: str
    last_sequence: int | None
    gaps: int
    regressions: int

    @property
    def healthy(self) -> bool:
        return self.gaps == 0 and self.regressions == 0


class ChannelSequenceTracker:
    """Tracks optional venue sequences; absence is explicit, never invented."""

    def __init__(self) -> None:
        self._last: dict[tuple[str, str], int] = {}
        self._gaps: dict[tuple[str, str], int] = defaultdict(int)
        self._regressions: dict[tuple[str, str], int] = defaultdict(int)

    def observe(self, channel: str, symbol: str, sequence: int | None) -> SequenceHealth:
        key = (channel, symbol.upper())
        if sequence is not None:
            current = int(sequence)
            previous = self._last.get(key)
            if previous is not None:
                if current > previous + 1:
                    self._gaps[key] += current - previous - 1
                elif current <= previous:
                    self._regressions[key] += 1
            if previous is None or current > previous:
                self._last[key] = current
        return self.snapshot(channel, symbol)

    def snapshot(self, channel: str, symbol: str) -> SequenceHealth:
        key = (channel, symbol.upper())
        return SequenceHealth(
            channel=channel,
            symbol=symbol.upper(),
            last_sequence=self._last.get(key),
            gaps=self._gaps[key],
            regressions=self._regressions[key],
        )


@dataclass(frozen=True)
class FlowSnapshot:
    symbol: str
    observed_at: datetime | None
    raw_imbalance: float
    imbalance_z: float
    cvd_usd: float
    buy_aggression_ratio: float
    absorption_score: float
    depth_usd: float
    mid_price: float | None
    sequence: SequenceHealth


class L2TradeFlowStore:
    """Bounded hot-path state; all calculations use already-seen events."""

    def __init__(
        self,
        *,
        imbalance_history: int = 240,
        trade_window_seconds: int = 15,
    ) -> None:
        if imbalance_history < 2 or trade_window_seconds < 1:
            raise ValueError("flow-store windows must be positive")
        self.imbalance_history = imbalance_history
        self.trade_window_seconds = trade_window_seconds
        self.sequence = ChannelSequenceTracker()
        self._imbalances: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=self.imbalance_history)
        )
        self._trades: dict[str, deque[tuple[datetime, float]]] = defaultdict(deque)
        self._last_at: dict[str, datetime] = {}
        self._mid: dict[str, float] = {}
        self._previous_mid: dict[str, float] = {}
        self._depth: dict[str, float] = {}

    @staticmethod
    def _level(level: object) -> tuple[float, float] | None:
        if not isinstance(level, dict):
            return None
        try:
            return float(level["limit_price"]), float(level["size"])
        except (KeyError, TypeError, ValueError):
            return None

    def on_book(
        self,
        symbol: str,
        bids: list,
        asks: list,
        *,
        observed_at: datetime,
        sequence: int | None = None,
        levels: int = 5,
    ) -> FlowSnapshot:
        native = symbol.upper()
        bid_levels = [row for item in bids[:levels] if (row := self._level(item))]
        ask_levels = [row for item in asks[:levels] if (row := self._level(item))]
        if not bid_levels or not ask_levels:
            raise ValueError("book update requires bid and ask levels")
        bid_size = sum(size / (index + 1) for index, (_, size) in enumerate(bid_levels))
        ask_size = sum(size / (index + 1) for index, (_, size) in enumerate(ask_levels))
        total = bid_size + ask_size
        imbalance = (bid_size - ask_size) / total if total else 0.0
        history = self._imbalances[native]
        history.append(imbalance)
        self._previous_mid[native] = self._mid.get(native, 0.0)
        self._mid[native] = (bid_levels[0][0] + ask_levels[0][0]) / 2.0
        self._depth[native] = sum(px * size for px, size in bid_levels + ask_levels)
        self._last_at[native] = observed_at.astimezone(UTC)
        self.sequence.observe("l2_orderbook", native, sequence)
        return self.snapshot(native, now=observed_at)

    def on_trade(
        self,
        symbol: str,
        *,
        price: float,
        size: float,
        side: str,
        observed_at: datetime,
        sequence: int | None = None,
    ) -> FlowSnapshot:
        if price <= 0 or size <= 0 or side not in {"buy", "sell"}:
            raise ValueError("invalid aggressor trade")
        native = symbol.upper()
        sign = 1.0 if side == "buy" else -1.0
        self._trades[native].append((observed_at.astimezone(UTC), sign * price * size))
        self._last_at[native] = observed_at.astimezone(UTC)
        self.sequence.observe("all_trades", native, sequence)
        self._prune(native, observed_at)
        return self.snapshot(native, now=observed_at)

    def _prune(self, symbol: str, now: datetime) -> None:
        cutoff = now.astimezone(UTC) - timedelta(seconds=self.trade_window_seconds)
        trades = self._trades[symbol]
        while trades and trades[0][0] < cutoff:
            trades.popleft()

    def snapshot(self, symbol: str, *, now: datetime | None = None) -> FlowSnapshot:
        native = symbol.upper()
        current = now or datetime.now(UTC)
        self._prune(native, current)
        history = self._imbalances[native]
        raw = history[-1] if history else 0.0
        deviation = pstdev(history) if len(history) > 1 else 0.0
        zscore = (raw - fmean(history)) / deviation if deviation else 0.0
        signed = [value for _, value in self._trades[native]]
        buys = sum(max(0.0, value) for value in signed)
        sells = sum(max(0.0, -value) for value in signed)
        cvd = buys - sells
        aggression = buys / (buys + sells) if buys + sells else 0.5
        mid = self._mid.get(native)
        previous = self._previous_mid.get(native)
        move_bps = (
            abs(mid / previous - 1) * 10_000 if mid and previous and previous > 0 else 0.0
        )
        depth = self._depth.get(native, 0.0)
        flow_to_depth = min(1.0, abs(cvd) / depth) if depth > 0 else 0.0
        # High aggressive flow with little mid-price response is absorption.
        absorption = flow_to_depth * max(0.0, 1.0 - min(1.0, move_bps / 3.0))
        return FlowSnapshot(
            symbol=native,
            observed_at=self._last_at.get(native),
            raw_imbalance=raw,
            imbalance_z=zscore,
            cvd_usd=cvd,
            buy_aggression_ratio=aggression,
            absorption_score=absorption,
            depth_usd=depth,
            mid_price=mid,
            sequence=self.sequence.snapshot("l2_orderbook", native),
        )
