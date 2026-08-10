"""Venue-specific fee tiers and contract specs — reality-checked against the
live exchange, not assumed from one Binance-shaped default.

Historically every lane (Binance, Bybit, Delta) shared ONE fee/limits config:
- fees: Binance USDT-M standard (taker 5.0 bps). Bybit's standard linear-perp
  taker is 0.055% = 5.5 bps, so every Bybit lane was modelled ~0.5 bps/side too
  cheap, overstating its edge.
- limits: a BTC-shaped ``min_qty=qty_step=0.0001`` for EVERY symbol. Real Bybit
  lot steps are 10x-10000x larger for altcoins (DOGE trades in whole coins), so
  sizing produced quantities Bybit would re-round or reject.

The Bybit contract specs below were verified against the Bybit V5
``/v5/market/instruments-info?category=linear`` API on 2026-07-24. Funding
interval is 480m (8h) for all of these symbols, so the codebase-wide 8h funding
assumption is correct for our universe (kept as a note, not a code path).

Bybit and Binance USDT-M specs are tabulated from their live APIs; other venues
(Delta and any untabulated symbol) fall through to the historical default, so
their behaviour is unchanged. Fees are still routed per-venue: Bybit taker 5.5,
Binance/others 5.0.
"""

from __future__ import annotations

from vnedge.paper.fill_model import FillModel
from vnedge.risk.position_sizer import SymbolLimits

# --- fees -------------------------------------------------------------------
# Standard (non-VIP) taker fee in bps, keyed by ccxt exchange id. Maker is 2.0
# bps on both venues; the paper fill model charges taker on both legs (v1 uses
# market fills), so only the taker rate is venue-routed here.
_DEFAULT_TAKER_BPS = 5.0  # Binance USDT-M standard tier
_TAKER_BPS_BY_EXCHANGE: dict[str, float] = {
    "bybit": 5.5,  # Bybit linear-perp standard taker = 0.055%
}
_SLIPPAGE_BPS = 2.0  # venue-agnostic pessimistic paper slippage (unchanged)


def venue_taker_bps(exchange: str) -> float:
    """Standard taker fee (bps) for a venue, defaulting to the Binance tier."""
    return _TAKER_BPS_BY_EXCHANGE.get(exchange.lower(), _DEFAULT_TAKER_BPS)


def venue_fill_model(exchange: str) -> FillModel:
    """Paper FillModel with the venue's real taker fee wired in."""
    return FillModel(
        taker_fee_bps=venue_taker_bps(exchange),
        slippage_bps=_SLIPPAGE_BPS,
    )


# --- contract specs ---------------------------------------------------------
# Historical default (kept as the fallback for venues/symbols not tabulated).
_DEFAULT_LIMITS = SymbolLimits(
    min_qty=0.0001,
    qty_step=0.0001,
    min_notional_usd=5.0,
    maintenance_margin_rate=0.005,
)

# Real Bybit V5 linear-perp specs (instruments-info, 2026-07-24). min_notional
# is 5 USDT for all; maintenance_margin_rate uses the base-tier 0.5% (our <$1k
# positions never leave the base tier). Keyed by the ccxt unified symbol.
_BYBIT_LIMITS: dict[str, SymbolLimits] = {
    "BTC/USDT:USDT": SymbolLimits(0.001, 0.001, 5.0, 0.005),
    "ETH/USDT:USDT": SymbolLimits(0.01, 0.01, 5.0, 0.005),
    "SOL/USDT:USDT": SymbolLimits(0.1, 0.1, 5.0, 0.005),
    "XRP/USDT:USDT": SymbolLimits(0.1, 0.1, 5.0, 0.005),
    "DOGE/USDT:USDT": SymbolLimits(1.0, 1.0, 5.0, 0.005),
    "BNB/USDT:USDT": SymbolLimits(0.01, 0.01, 5.0, 0.005),
}

# Real Binance USDT-M linear-perp specs (fapi /fapi/v1/exchangeInfo, 2026-07-24).
# NOTE the min-notional: Binance requires >= 50 USDT for BTC and >= 20 USDT for
# ETH (not the flat 5 that Bybit and our default assume), so risk-based sizes
# below those are rejected by the venue — our sizer must reject them too.
# fundingInfo confirms 8h interval for all of these symbols.
_BINANCE_LIMITS: dict[str, SymbolLimits] = {
    "BTC/USDT:USDT": SymbolLimits(0.001, 0.001, 50.0, 0.005),
    "ETH/USDT:USDT": SymbolLimits(0.001, 0.001, 20.0, 0.005),
    "SOL/USDT:USDT": SymbolLimits(0.01, 0.01, 5.0, 0.005),
    "XRP/USDT:USDT": SymbolLimits(0.1, 0.1, 5.0, 0.005),
    "DOGE/USDT:USDT": SymbolLimits(1.0, 1.0, 5.0, 0.005),
    "BNB/USDT:USDT": SymbolLimits(0.01, 0.01, 5.0, 0.005),
}

_LIMITS_BY_EXCHANGE: dict[str, dict[str, SymbolLimits]] = {
    "bybit": _BYBIT_LIMITS,
    "binanceusdm": _BINANCE_LIMITS,
}


def venue_symbol_limits(exchange: str, symbol: str) -> SymbolLimits:
    """Real per-(venue, symbol) contract limits, falling back to the historical
    BTC-shaped default for venues/symbols not tabulated (Binance, Delta, and any
    symbol we haven't verified)."""
    return _LIMITS_BY_EXCHANGE.get(exchange.lower(), {}).get(symbol, _DEFAULT_LIMITS)
