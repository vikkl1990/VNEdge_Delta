"""Slippage model: fixed adverse basis points on every fill.

Simple by intent for v1 — a constant penalty in the adverse direction. It can
NEVER improve a fill. A depth-aware model can replace this once order book
snapshots are collected; the interface stays the same.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SlippageModel(BaseModel):
    model_config = {"frozen": True}

    bps: float = Field(default=2.0, ge=0)

    def fill_price(self, reference_price: float, side: str) -> float:
        """Adverse fill for a market order. ``side`` is the order direction:
        'buy' pays up, 'sell' receives less."""
        if side not in ("buy", "sell"):
            raise ValueError(f"invalid order side: {side}")
        adj = self.bps / 10_000.0
        return reference_price * (1 + adj) if side == "buy" else reference_price * (1 - adj)
