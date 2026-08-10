"""Risk-based position sizing.

Size is derived from *how much we are willing to lose*, never from leverage:

    quantity = (equity * risk_per_trade) / |entry - stop|

Leverage is then computed as the margin-efficiency consequence of that size
and validated against the configured caps, including the liquidation buffer:
a stop that would never be reached because the position liquidates first is
not a stop, and such orders are rejected here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from vnedge.config.risk_config import RiskConfig


@dataclass(frozen=True)
class SizingResult:
    approved: bool
    quantity: float = 0.0
    notional_usd: float = 0.0
    required_leverage: float = 0.0
    risk_usd: float = 0.0
    reasons: tuple[str, ...] = ()  # non-empty when rejected — explainability


@dataclass(frozen=True)
class SymbolLimits:
    """Exchange-imposed constraints for one symbol."""

    min_qty: float
    qty_step: float
    min_notional_usd: float
    # Fraction of position notional at which the venue liquidates, e.g. 0.005
    # for 0.5% maintenance margin. Used for a conservative liquidation estimate.
    maintenance_margin_rate: float


def size_position(
    *,
    equity_usd: float,
    entry_price: float,
    stop_price: float,
    side: str,  # "long" | "short"
    config: RiskConfig,
    limits: SymbolLimits,
) -> SizingResult:
    """Compute an order size, or reject with explicit reasons."""
    reasons: list[str] = []

    if side not in ("long", "short"):
        return SizingResult(approved=False, reasons=(f"invalid side: {side}",))
    if entry_price <= 0 or stop_price <= 0:
        return SizingResult(approved=False, reasons=("non-positive price",))

    stop_distance = entry_price - stop_price if side == "long" else stop_price - entry_price
    if stop_distance <= 0:
        return SizingResult(
            approved=False,
            reasons=(f"stop {stop_price} is on the wrong side of entry {entry_price} for {side}",),
        )

    stop_pct = stop_distance / entry_price
    if config.fixed_margin_usd is not None:
        # ISOLATED fixed-margin sizing (opt-in, paper-only). notional =
        # margin x leverage, but the leverage is capped per-trade so the stop
        # always sits INSIDE the isolated liquidation distance (with the buffer)
        # — which guarantees the stop fires before liquidation, so the worst
        # case loss is <= the fixed margin. Wide stops therefore get less
        # leverage (and a smaller position), never a blow-up.
        buffer = config.min_liquidation_buffer_pct / 100.0
        denom = stop_pct * (1.0 + buffer) + limits.maintenance_margin_rate
        safe_lev = (1.0 / denom) if denom > 0 else 1.0
        iso_lev = max(1, min(config.max_leverage_per_position, int(math.floor(safe_lev))))
        raw_qty = (config.fixed_margin_usd * iso_lev) / entry_price
    else:
        risk_usd = equity_usd * config.risk_per_trade_pct / 100.0
        raw_qty = risk_usd / stop_distance

    # Round DOWN to the exchange quantity step — rounding up would exceed the
    # risk budget.
    qty = math.floor(raw_qty / limits.qty_step) * limits.qty_step
    if qty < limits.min_qty:
        reasons.append(
            f"risk-based qty {raw_qty:.8f} below exchange minimum {limits.min_qty} — "
            "symbol untradeable at this equity/stop; do NOT widen risk to force entry"
        )

    notional = qty * entry_price
    if notional < limits.min_notional_usd and not reasons:
        reasons.append(
            f"notional ${notional:.2f} below exchange minimum ${limits.min_notional_usd:.2f}"
        )

    if notional > config.max_exposure_per_symbol_usd:
        reasons.append(
            f"notional ${notional:.2f} exceeds per-symbol cap "
            f"${config.max_exposure_per_symbol_usd:.2f}"
        )

    # Implied leverage of the trade. In fixed-margin (isolated) mode the margin
    # is the fixed isolated margin the operator committed — that is the leverage
    # they chose and what liquidation is computed against. In the default
    # risk-based model it is notional / whole-account equity.
    if config.fixed_margin_usd is not None:
        required_leverage = notional / config.fixed_margin_usd
    else:
        required_leverage = notional / equity_usd if equity_usd > 0 else float("inf")
    if required_leverage > config.max_leverage_per_position:
        reasons.append(
            f"implied leverage {required_leverage:.1f}x exceeds cap "
            f"{config.max_leverage_per_position}x"
        )

    # Liquidation buffer: conservative estimate of liquidation distance for an
    # isolated position at the configured leverage. The stop must sit safely
    # inside it.
    lev = max(required_leverage, 1.0)
    liq_distance = entry_price * (1.0 / lev - limits.maintenance_margin_rate)
    required_buffer = stop_distance * (1.0 + config.min_liquidation_buffer_pct / 100.0)
    if liq_distance <= required_buffer:
        reasons.append(
            f"liquidation distance {liq_distance:.2f} too close to stop distance "
            f"{stop_distance:.2f} (need >= {required_buffer:.2f}); stop would not protect"
        )

    if reasons:
        return SizingResult(approved=False, reasons=tuple(reasons))

    return SizingResult(
        approved=True,
        quantity=qty,
        notional_usd=notional,
        required_leverage=required_leverage,
        risk_usd=qty * stop_distance,
    )
