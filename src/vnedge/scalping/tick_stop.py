"""Tick-level stop engine.

Stops are risk infrastructure, not strategy goodwill. A future scalper loop
registers a stop as soon as a fill opens risk, then evaluates this engine on
every fresh top-of-book event. Triggered stops produce reduce-only intents
that must still pass through OrderManager and the existing gateway.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from vnedge.risk.risk_manager import OrderIntent
from vnedge.scalping.microstructure import TopOfBook


@dataclass(frozen=True)
class StopRegistration:
    position_id: str
    symbol: str
    side: Literal["long", "short"]
    quantity: float
    stop_price: float
    strategy_id: str = "tick_stop"

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError("stop quantity must be positive")
        if self.stop_price <= 0:
            raise ValueError("stop price must be positive")


class TickStopEngine:
    def __init__(self) -> None:
        self._stops: dict[str, StopRegistration] = {}

    def register(self, stop: StopRegistration) -> None:
        self._stops[stop.position_id] = stop

    def cancel(self, position_id: str) -> None:
        self._stops.pop(position_id, None)

    def evaluate(self, top: TopOfBook) -> tuple[OrderIntent, ...]:
        intents: list[OrderIntent] = []
        for position_id, stop in list(self._stops.items()):
            if stop.symbol != top.symbol:
                continue
            triggered = (
                top.bid <= stop.stop_price if stop.side == "long"
                else top.ask >= stop.stop_price
            )
            if not triggered:
                continue
            self._stops.pop(position_id)
            intents.append(
                OrderIntent(
                    symbol=stop.symbol,
                    side="short" if stop.side == "long" else "long",
                    quantity=stop.quantity,
                    notional_usd=0.0,
                    leverage=1.0,
                    reduce_only=True,
                    strategy_id=stop.strategy_id,
                )
            )
        return tuple(intents)

    @property
    def active_count(self) -> int:
        return len(self._stops)
