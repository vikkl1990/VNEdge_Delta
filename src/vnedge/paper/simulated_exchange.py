"""Simulated exchange — the first execution counterparty.

Not a fake fill generator: it maintains venue-side truth (balances, net
positions, open orders, fills, order statuses) with exchange-like semantics
the live adapters will eventually share:

- **Idempotent by client_order_id**: resubmitting an id returns the existing
  status instead of double-booking — the venue behavior our idempotency
  design relies on.
- **Reduce-only really reduces**: clamped to the open position; rejected
  when flat.
- Market orders fill immediately at quote ± slippage; limit orders rest and
  fill only when a quote update crosses them, at the limit price.
- Everything is deterministic: no clock, no randomness — a monotonic event
  sequence numbers fills.

No margin model in v1 — position sizing and exposure are enforced upstream
by the risk gateway; the venue enforces order mechanics.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace

from vnedge.paper.fill_model import FillModel

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PaperOrderRequest:
    client_order_id: str
    symbol: str
    buy: bool  # True = buy/long-impact, False = sell/short-impact
    quantity: float
    reduce_only: bool = False
    order_type: str = "market"  # "market" | "limit"
    limit_price: float | None = None


@dataclass
class PaperOrderStatus:
    client_order_id: str
    exchange_order_id: str
    state: str  # "open" | "filled" | "partially_filled" | "cancelled" | "rejected"
    requested_qty: float
    filled_qty: float = 0.0
    avg_fill_price: float = 0.0
    fee_usd: float = 0.0  # cumulative fees charged on this order's fills
    reason: str = ""


@dataclass(frozen=True)
class PaperFill:
    seq: int
    client_order_id: str
    symbol: str
    buy: bool
    quantity: float
    price: float
    fee_usd: float
    realized_pnl_usd: float = 0.0  # nonzero only on position-reducing fills


@dataclass
class PaperPosition:
    symbol: str
    quantity: float  # signed: >0 long, <0 short
    entry_price: float  # weighted average

    @property
    def side(self) -> str:
        return "long" if self.quantity > 0 else "short"


class SimulatedExchange:
    def __init__(self, fill_model: FillModel, starting_balance_usd: float = 1_000.0) -> None:
        self.fill_model = fill_model
        self.balance_usd = starting_balance_usd
        self.quotes: dict[str, tuple[float, float]] = {}  # symbol -> (bid, ask)
        self.positions: dict[str, PaperPosition] = {}
        self.orders: dict[str, PaperOrderStatus] = {}  # by client_order_id
        self._resting: dict[str, PaperOrderRequest] = {}  # open limit orders
        self.fills: list[PaperFill] = []
        self._seq = 0

    # --- Market data ----------------------------------------------------------
    def set_quote(self, symbol: str, bid: float, ask: float) -> None:
        if bid <= 0 or ask <= 0 or bid > ask:
            raise ValueError(f"invalid quote {bid}/{ask} for {symbol}")
        self.quotes[symbol] = (bid, ask)
        self._try_fill_resting(symbol)

    # --- Order entry ------------------------------------------------------------
    def submit_order(self, req: PaperOrderRequest) -> PaperOrderStatus:
        # Idempotent venue: same client id -> same order, no double booking.
        if req.client_order_id in self.orders:
            return self.orders[req.client_order_id]

        self._seq += 1
        status = PaperOrderStatus(
            client_order_id=req.client_order_id,
            exchange_order_id=f"pex_{self._seq}",
            state="open",
            requested_qty=req.quantity,
        )
        self.orders[req.client_order_id] = status

        if req.symbol not in self.quotes:
            status.state, status.reason = "rejected", f"no market data for {req.symbol}"
            return status
        if req.quantity <= 0:
            status.state, status.reason = "rejected", "non-positive quantity"
            return status

        qty = req.quantity
        if req.reduce_only:
            pos = self.positions.get(req.symbol)
            opposing = pos is not None and (pos.quantity > 0) != req.buy
            if not opposing:
                status.state, status.reason = "rejected", "reduce-only with no opposing position"
                return status
            qty = min(qty, abs(pos.quantity))  # venue clamps, never over-closes
            status.requested_qty = qty

        if req.order_type == "market":
            self._execute(req, status, qty)
        elif req.order_type == "limit":
            if req.limit_price is None or req.limit_price <= 0:
                status.state, status.reason = "rejected", "limit order without valid price"
                return status
            self._resting[req.client_order_id] = PaperOrderRequest(
                req.client_order_id, req.symbol, req.buy, qty,
                req.reduce_only, "limit", req.limit_price,
            )
            # Crossing on arrival = marketable limit = taker; only a later quote
            # crossing a still-resting limit earns the maker fee.
            self._try_fill_resting(req.symbol, at_submission=True)
        else:
            status.state, status.reason = "rejected", f"unsupported order type {req.order_type}"
        return status

    def cancel_order(self, client_order_id: str) -> PaperOrderStatus:
        status = self.orders[client_order_id]
        if client_order_id in self._resting:
            del self._resting[client_order_id]
            status.state = "cancelled"
        elif status.state == "open":
            status.state = "cancelled"
        return status

    # --- Fills & accounting --------------------------------------------------------
    def _execute(self, req: PaperOrderRequest, status: PaperOrderStatus, qty: float) -> None:
        bid, ask = self.quotes[req.symbol]
        price = self.fill_model.market_fill_price(bid, ask, req.buy)
        fill_qty = self.fill_model.fill_quantity(qty)
        self._apply_fill(req.client_order_id, req.symbol, req.buy, fill_qty, price)
        status.filled_qty = fill_qty
        status.avg_fill_price = price
        if fill_qty < qty:
            status.state = "partially_filled"
            status.reason = "remainder cancelled (IOC)"
        else:
            status.state = "filled"

    def _fill_limit(self, req: PaperOrderRequest, *, maker: bool) -> None:
        # req.quantity is the REMAINING quantity (partial fills shrink it).
        # `maker` is True only when a resting limit is crossed by a LATER quote
        # (it provided liquidity); an immediately-marketable limit is taker.
        self._apply_fill(
            req.client_order_id, req.symbol, req.buy, req.quantity, req.limit_price,
            maker=maker,
        )
        status = self._record_limit_fill(req, req.quantity)
        status.state = "filled"

    def _record_limit_fill(self, req: PaperOrderRequest, qty: float) -> PaperOrderStatus:
        status = self.orders[req.client_order_id]
        prev = status.filled_qty
        status.avg_fill_price = (
            status.avg_fill_price * prev + req.limit_price * qty
        ) / (prev + qty)
        status.filled_qty = prev + qty
        return status

    def partial_fill(self, client_order_id: str, quantity: float) -> PaperOrderStatus:
        """Deterministically fill PART of a resting limit order at its limit
        price; the remainder keeps resting. Venue-side test hook — no clock,
        no randomness, mirrors a partial maker fill."""
        req = self._resting.get(client_order_id)
        if req is None:
            raise KeyError(f"{client_order_id} is not a resting limit order")
        if not 0 < quantity < req.quantity:
            raise ValueError(
                f"partial fill must be within (0, {req.quantity}), got {quantity}"
            )
        # A resting limit that fills after placement provided liquidity: maker.
        self._apply_fill(
            client_order_id, req.symbol, req.buy, quantity, req.limit_price, maker=True,
        )
        status = self._record_limit_fill(req, quantity)
        status.state = "partially_filled"
        self._resting[client_order_id] = replace(req, quantity=req.quantity - quantity)
        return status

    def _try_fill_resting(self, symbol: str, *, at_submission: bool = False) -> None:
        # at_submission=True means the limit was just placed and is crossing on
        # arrival (marketable -> taker); a later quote crossing it is a maker fill.
        bid, ask = self.quotes[symbol]
        for coid, req in list(self._resting.items()):
            if req.symbol != symbol:
                continue
            crossed = (req.buy and ask <= req.limit_price) or (
                not req.buy and bid >= req.limit_price
            )
            if crossed:
                del self._resting[coid]
                self._fill_limit(req, maker=not at_submission)

    def _apply_fill(
        self, client_order_id: str, symbol: str, buy: bool, qty: float, price: float,
        *, maker: bool = False,
    ) -> None:
        signed = qty if buy else -qty
        fee = self.fill_model.fee_usd(qty * price, maker=maker)
        self.balance_usd -= fee
        order_status = self.orders.get(client_order_id)
        if order_status is not None:
            order_status.fee_usd += fee
        realized = 0.0

        pos = self.positions.get(symbol)
        if pos is None or pos.quantity == 0:
            self.positions[symbol] = PaperPosition(symbol, signed, price)
        elif (pos.quantity > 0) == buy:  # extending
            total = pos.quantity + signed
            pos.entry_price = (
                pos.entry_price * abs(pos.quantity) + price * qty
            ) / abs(total)
            pos.quantity = total
        else:
            # Reducing (possibly through zero).
            closing = min(qty, abs(pos.quantity))
            direction = 1.0 if pos.quantity > 0 else -1.0
            realized = direction * closing * (price - pos.entry_price)
            self.balance_usd += realized
            pos.quantity += signed
            if abs(pos.quantity) < 1e-12:
                del self.positions[symbol]
            elif (pos.quantity > 0) == buy:
                # flipped through zero: remainder is a fresh position at fill price
                pos.entry_price = price

        self._seq += 1
        self.fills.append(
            PaperFill(self._seq, client_order_id, symbol, buy, qty, price, fee, realized)
        )

    # --- Exchange truth (the reconciliation surface) --------------------------------
    def get_order_status(self, client_order_id: str) -> PaperOrderStatus | None:
        return self.orders.get(client_order_id)

    def get_open_orders(self) -> list[PaperOrderStatus]:
        # A partially filled RESTING limit order is still working at the venue.
        return [
            s for s in self.orders.values()
            if s.state == "open" or s.client_order_id in self._resting
        ]

    def get_positions(self) -> list[PaperPosition]:
        return list(self.positions.values())

    def get_balances(self) -> dict[str, float]:
        return {"USDT": self.balance_usd}

    def get_fills(self) -> list[PaperFill]:
        return list(self.fills)
