"""Experimental strategy interface for event-driven scalpers.

This is intentionally separate from BaseStrategy, whose contract is candle
oriented. Scalper strategies react to book/trade/timer/fill events, but they
still return ordinary OrderIntent objects so the same gateway, journal, and
OrderManager remain the only execution path.

Status: research scaffold only. The production paper/shadow runners still use
registered candle/manifest lanes; no live loop dispatches BaseScalperStrategy
subclasses yet.
"""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass
from datetime import datetime

from vnedge.risk.risk_manager import AccountState, OrderIntent
from vnedge.scalping.features import ScalperFeatures
from vnedge.scalping.microstructure import MarketMicroState


@dataclass(frozen=True)
class QuoteIntent:
    intent: OrderIntent
    expected_edge_bps: float
    ttl_ms: int
    post_only: bool = True
    reason: str = ""

    def __post_init__(self) -> None:
        if self.ttl_ms <= 0:
            raise ValueError("quote ttl_ms must be positive")


@dataclass(frozen=True)
class CancelIntent:
    client_order_id: str
    reason: str


@dataclass(frozen=True)
class ScalperDecisionContext:
    market: MarketMicroState
    features: ScalperFeatures
    account: AccountState
    now: datetime


class BaseScalperStrategy(ABC):
    """Optional event hooks for a future hot loop.

    Do not treat subclasses of this interface as production scalper lanes until
    a runner explicitly wires them through the risk gateway and journal.
    """

    strategy_id: str = "unnamed_scalper"

    def on_book_update(
        self, context: ScalperDecisionContext
    ) -> QuoteIntent | CancelIntent | OrderIntent | None:
        return None

    def on_trade_update(
        self, context: ScalperDecisionContext
    ) -> QuoteIntent | CancelIntent | OrderIntent | None:
        return None

    def on_timer(
        self, context: ScalperDecisionContext
    ) -> QuoteIntent | CancelIntent | OrderIntent | None:
        return None

    def on_fill(self, fill: object) -> None:
        """Strategies may update local alpha state, but never place orders here."""
        return None
