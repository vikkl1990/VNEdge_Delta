"""Market replay — canonical candles become quotes and MarketState.

Feeds the simulated exchange a bid/ask derived from bar prices and builds
the MarketState the risk gateway sees, with `last_update` equal to the bar
timestamp (the runner also evaluates with now=bar timestamp, so staleness
semantics hold in replay exactly as they will live).
"""

from __future__ import annotations

import pandas as pd

from vnedge.risk.risk_manager import MarketState
from vnedge.strategy.regime import merge_funding


def quote_from_price(price: float, spread_bps: float) -> tuple[float, float]:
    half = spread_bps / 2.0 / 10_000.0
    return price * (1 - half), price * (1 + half)


class MarketReplay:
    def __init__(
        self,
        candles: pd.DataFrame,
        funding: pd.DataFrame | None,
        *,
        symbol: str,
        spread_bps: float,
        slippage_est_bps: float,
    ) -> None:
        self.symbol = symbol
        self.spread_bps = spread_bps
        self.slippage_est_bps = slippage_est_bps
        # backward as-of join: each bar carries the last funding rate known then
        self._bars = merge_funding(candles, funding)

    def market_state(self, index: int) -> MarketState:
        bar = self._bars.iloc[index]
        return MarketState(
            symbol=self.symbol,
            last_update=bar["timestamp"].to_pydatetime(),
            spread_bps=self.spread_bps,
            estimated_slippage_bps=self.slippage_est_bps,
            funding_rate=float(bar["funding_rate"]),
            exchange_healthy=True,
        )
