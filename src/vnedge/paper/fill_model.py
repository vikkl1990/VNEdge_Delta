"""Paper fill model — deliberately pessimistic.

Market buys fill at ask plus slippage; market sells at bid minus slippage;
fees are always charged; limit orders fill only when price actually crosses,
and then at the limit price (never with imaginary improvement). Optional
deterministic partial fills for market orders (remainder is cancelled,
IOC-style) so partial-fill handling is testable without randomness.

If paper results look better than backtest results, the fill model is wrong.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class FillModel(BaseModel):
    model_config = {"frozen": True}

    slippage_bps: float = Field(default=2.0, ge=0)
    taker_fee_bps: float = Field(default=5.0, ge=0)
    # Maker (resting-limit) fee. Only charged when a caller explicitly routes a
    # leg maker AND models the resting-fill realism itself (touch-to-fill); the
    # default fee path stays taker, so no existing caller is silently cheapened.
    maker_fee_bps: float = Field(default=2.0, ge=0)
    # None = full fills. 0.5 = market orders fill half, rest cancelled.
    partial_fill_fraction: float | None = Field(default=None, gt=0, le=1.0)

    def market_fill_price(self, bid: float, ask: float, buy: bool) -> float:
        adj = self.slippage_bps / 10_000.0
        return ask * (1 + adj) if buy else bid * (1 - adj)

    def fee_usd(self, notional_usd: float, *, maker: bool = False) -> float:
        bps = self.maker_fee_bps if maker else self.taker_fee_bps
        return abs(notional_usd) * bps / 10_000.0

    def fill_quantity(self, requested: float) -> float:
        if self.partial_fill_fraction is None:
            return requested
        return requested * self.partial_fill_fraction
