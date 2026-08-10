"""Venue-specific fees and contract limits reflect real exchange data.

Guards the Bybit reality-check refactor: Bybit lanes must be charged Bybit's
real taker fee (5.5 bps, not the Binance 5.0 default) and sized with Bybit's
real per-symbol lot steps (DOGE trades in whole coins, not 0.0001 increments),
while non-Bybit venues keep the historical defaults unchanged.
"""

import pytest

from vnedge.config.risk_config import RiskConfig
from vnedge.exchange.venue_specs import (
    venue_fill_model,
    venue_symbol_limits,
    venue_taker_bps,
)
from vnedge.risk.position_sizer import SymbolLimits, size_position


# --- fees -------------------------------------------------------------------
def test_bybit_taker_fee_is_5_5_bps():
    assert venue_taker_bps("bybit") == 5.5
    assert venue_taker_bps("Bybit") == 5.5  # case-insensitive


def test_non_bybit_venues_keep_binance_taker():
    assert venue_taker_bps("binanceusdm") == 5.0
    assert venue_taker_bps("delta_india") == 5.0
    assert venue_taker_bps("something_new") == 5.0


def test_venue_fill_model_wires_the_right_taker():
    bybit = venue_fill_model("bybit")
    binance = venue_fill_model("binanceusdm")
    assert bybit.taker_fee_bps == 5.5
    assert binance.taker_fee_bps == 5.0
    # slippage stays venue-agnostic and pessimistic
    assert bybit.slippage_bps == 2.0 == binance.slippage_bps


# --- contract limits --------------------------------------------------------
@pytest.mark.parametrize(
    "symbol,min_qty,step",
    [
        ("BTC/USDT:USDT", 0.001, 0.001),
        ("ETH/USDT:USDT", 0.01, 0.01),
        ("SOL/USDT:USDT", 0.1, 0.1),
        ("XRP/USDT:USDT", 0.1, 0.1),
        ("DOGE/USDT:USDT", 1.0, 1.0),
        ("BNB/USDT:USDT", 0.01, 0.01),
    ],
)
def test_bybit_symbol_limits_match_live_instruments_info(symbol, min_qty, step):
    limits = venue_symbol_limits("bybit", symbol)
    assert limits.min_qty == min_qty
    assert limits.qty_step == step
    assert limits.min_notional_usd == 5.0


@pytest.mark.parametrize(
    "symbol,step,min_notional",
    [
        ("BTC/USDT:USDT", 0.001, 50.0),  # Binance BTC min-notional is 50, not 5
        ("ETH/USDT:USDT", 0.001, 20.0),  # ETH is 20
        ("SOL/USDT:USDT", 0.01, 5.0),
        ("XRP/USDT:USDT", 0.1, 5.0),
        ("DOGE/USDT:USDT", 1.0, 5.0),
        ("BNB/USDT:USDT", 0.01, 5.0),
    ],
)
def test_binance_symbol_limits_match_live_exchange_info(symbol, step, min_notional):
    limits = venue_symbol_limits("binanceusdm", symbol)
    assert limits.qty_step == step
    assert limits.min_notional_usd == min_notional


def test_unknown_venue_and_symbol_fall_back_to_default():
    # An untabulated venue falls back to the historical default.
    other = venue_symbol_limits("delta_india", "BTC/USD:USD")
    assert other.qty_step == 0.0001
    # A tabulated venue but untabulated symbol also falls back, safely.
    unknown = venue_symbol_limits("bybit", "PEPE/USDT:USDT")
    assert unknown.qty_step == 0.0001


def test_binance_btc_min_notional_rejects_below_50():
    """A BTC order that clears min-qty but whose notional is under Binance's
    50 USDT floor must be rejected — the old flat 5 USDT default let it pass.
    A min-lot (0.001 BTC) is only < 50 USDT when price is < 50k, so use 40k."""
    limits = venue_symbol_limits("binanceusdm", "BTC/USDT:USDT")
    sizing = size_position(
        equity_usd=100.0,
        entry_price=40000.0,
        stop_price=39000.0,
        side="long",
        config=RiskConfig(risk_per_trade_pct=1.0),
        limits=limits,
    )
    # qty floors to the 0.001 min-lot = 40 USDT notional, under the 50 floor
    assert not sizing.approved
    assert any("notional" in r.lower() for r in sizing.reasons)
    # ...and the SAME sizing would have been ACCEPTED under the old flat-5 default
    old = size_position(
        equity_usd=100.0, entry_price=40000.0, stop_price=39000.0, side="long",
        config=RiskConfig(risk_per_trade_pct=1.0),
        limits=SymbolLimits(0.0001, 0.0001, 5.0, 0.005),
    )
    assert old.approved


# --- the correctness payoff: sizing a whole-coin instrument -----------------
def test_bybit_doge_sizing_floors_to_whole_coins():
    """With the old 0.0001 step, a DOGE size like 123.4 would pass through as
    123.4 — a quantity Bybit rejects (step is 1). With the real step it floors
    to a whole coin, matching what the venue accepts."""
    limits = venue_symbol_limits("bybit", "DOGE/USDT:USDT")
    sizing = size_position(
        equity_usd=500.0,
        entry_price=0.10,
        stop_price=0.099,
        side="long",
        config=RiskConfig(),
        limits=limits,
    )
    assert sizing.approved
    # quantity is an integer number of DOGE (multiple of step=1)
    assert sizing.quantity == pytest.approx(round(sizing.quantity))
