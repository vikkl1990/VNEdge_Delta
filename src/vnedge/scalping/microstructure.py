"""Microstructure state for event-driven scalping.

The existing VNEDGE runtime reasons about closed candles. A scalper needs a
small, immutable snapshot of the current tradable market: top of book,
private-stream health, and the funding/slippage fields needed to reuse the
same pre-trade risk gateway as every other execution path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from vnedge.risk.risk_manager import MarketState


def _as_aware_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


@dataclass(frozen=True)
class TopOfBook:
    symbol: str
    bid: float
    bid_size: float
    ask: float
    ask_size: float
    event_time: datetime
    sequence: int | None = None
    exchange_healthy: bool = True

    def __post_init__(self) -> None:
        if self.bid <= 0 or self.ask <= 0:
            raise ValueError("top-of-book prices must be positive")
        if self.bid > self.ask:
            raise ValueError(f"crossed book for {self.symbol}: {self.bid} > {self.ask}")
        if self.bid_size < 0 or self.ask_size < 0:
            raise ValueError("top-of-book sizes cannot be negative")
        object.__setattr__(self, "event_time", _as_aware_utc(self.event_time))

    @property
    def mid_price(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread_bps(self) -> float:
        return (self.ask - self.bid) / self.mid_price * 10_000.0

    @property
    def top_depth_usd(self) -> float:
        return self.bid * self.bid_size + self.ask * self.ask_size

    @property
    def book_imbalance(self) -> float:
        total = self.bid_size + self.ask_size
        return 0.0 if total <= 0 else (self.bid_size - self.ask_size) / total

    @property
    def microprice(self) -> float:
        total = self.bid_size + self.ask_size
        if total <= 0:
            return self.mid_price
        return (self.ask * self.bid_size + self.bid * self.ask_size) / total

    def age_seconds(self, now: datetime | None = None) -> float:
        now = _as_aware_utc(now or datetime.now(UTC))
        return (now - self.event_time).total_seconds()


@dataclass(frozen=True)
class TradeTick:
    symbol: str
    price: float
    quantity: float
    taker_side: Literal["buy", "sell"]
    event_time: datetime

    def __post_init__(self) -> None:
        if self.price <= 0 or self.quantity <= 0:
            raise ValueError("trade price and quantity must be positive")
        if self.taker_side not in ("buy", "sell"):
            raise ValueError(f"invalid taker side: {self.taker_side}")
        object.__setattr__(self, "event_time", _as_aware_utc(self.event_time))

    @property
    def signed_notional_usd(self) -> float:
        sign = 1.0 if self.taker_side == "buy" else -1.0
        return sign * self.price * self.quantity


@dataclass(frozen=True)
class PrivateStreamState:
    """Freshness of account/order truth from the private user stream."""

    last_event_at: datetime
    connected: bool = True
    open_order_count: int = 0
    position_qty_by_symbol: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "last_event_at", _as_aware_utc(self.last_event_at))
        if self.open_order_count < 0:
            raise ValueError("open_order_count cannot be negative")

    def age_seconds(self, now: datetime | None = None) -> float:
        now = _as_aware_utc(now or datetime.now(UTC))
        return (now - self.last_event_at).total_seconds()


@dataclass(frozen=True)
class MarketMicroState:
    top: TopOfBook
    private: PrivateStreamState | None = None
    funding_rate: float = 0.0
    estimated_slippage_bps: float = 0.0

    @property
    def symbol(self) -> str:
        return self.top.symbol

    def to_market_state(self) -> MarketState:
        """Adapt tick state into the existing gateway's MarketState contract."""
        return MarketState(
            symbol=self.top.symbol,
            last_update=self.top.event_time,
            spread_bps=self.top.spread_bps,
            estimated_slippage_bps=self.estimated_slippage_bps,
            funding_rate=self.funding_rate,
            exchange_healthy=self.top.exchange_healthy,
        )

    def private_age_seconds(self, now: datetime | None = None) -> float:
        if self.private is None:
            return float("inf")
        return self.private.age_seconds(now)
