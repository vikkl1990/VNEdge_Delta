"""Incremental microstructure features for scalpers.

This avoids pandas in the hot path. The engine consumes top-of-book and trade
events and exposes a compact snapshot strategies can inspect on every event.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import pstdev

from vnedge.scalping.microstructure import TopOfBook, TradeTick


@dataclass(frozen=True)
class ScalperFeatures:
    symbol: str
    computed_at: datetime
    mid_price: float
    microprice: float
    spread_bps: float
    book_imbalance: float
    top_depth_usd: float
    trade_count: int
    taker_buy_ratio: float
    signed_trade_notional_usd: float
    realized_vol_bps: float
    last_trade_price: float | None = None


class IncrementalFeatureEngine:
    """Small rolling-window feature calculator for the event loop."""

    def __init__(self, *, max_midpoints: int = 120, max_trades: int = 200) -> None:
        if max_midpoints < 2:
            raise ValueError("max_midpoints must be >= 2")
        if max_trades < 1:
            raise ValueError("max_trades must be >= 1")
        self._max_midpoints = max_midpoints
        self._top: TopOfBook | None = None
        self._midpoints: deque[float] = deque(maxlen=max_midpoints)
        self._trades: deque[TradeTick] = deque(maxlen=max_trades)

    def on_book(self, top: TopOfBook) -> ScalperFeatures:
        if self._top is not None and top.symbol != self._top.symbol:
            raise ValueError(f"engine bound to {self._top.symbol}, got {top.symbol}")
        self._top = top
        self._midpoints.append(top.mid_price)
        return self.snapshot(top.event_time)

    def on_trade(self, trade: TradeTick) -> ScalperFeatures | None:
        if self._top is None:
            self._trades.append(trade)
            return None
        if trade.symbol != self._top.symbol:
            raise ValueError(f"engine bound to {self._top.symbol}, got {trade.symbol}")
        self._trades.append(trade)
        return self.snapshot(trade.event_time)

    def snapshot(self, now: datetime | None = None) -> ScalperFeatures:
        if self._top is None:
            raise RuntimeError("cannot compute scalper features before first book update")
        now = now or datetime.now(UTC)
        total_qty = sum(t.quantity for t in self._trades)
        buy_qty = sum(t.quantity for t in self._trades if t.taker_side == "buy")
        signed_notional = sum(t.signed_notional_usd for t in self._trades)
        returns = [
            math.log(self._midpoints[i] / self._midpoints[i - 1])
            for i in range(1, len(self._midpoints))
            if self._midpoints[i - 1] > 0 and self._midpoints[i] > 0
        ]
        realized_vol_bps = pstdev(returns) * 10_000.0 if len(returns) >= 2 else 0.0
        last_trade = self._trades[-1].price if self._trades else None
        return ScalperFeatures(
            symbol=self._top.symbol,
            computed_at=now,
            mid_price=self._top.mid_price,
            microprice=self._top.microprice,
            spread_bps=self._top.spread_bps,
            book_imbalance=self._top.book_imbalance,
            top_depth_usd=self._top.top_depth_usd,
            trade_count=len(self._trades),
            taker_buy_ratio=(buy_qty / total_qty) if total_qty > 0 else 0.0,
            signed_trade_notional_usd=signed_notional,
            realized_vol_bps=realized_vol_bps,
            last_trade_price=last_trade,
        )
