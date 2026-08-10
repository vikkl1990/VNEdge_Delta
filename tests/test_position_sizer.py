"""Position sizing — risk-based math, exchange minimums, liquidation buffer."""

from vnedge.config.risk_config import RiskConfig
from vnedge.risk.position_sizer import SymbolLimits, size_position

BTC_LIMITS = SymbolLimits(
    min_qty=0.001, qty_step=0.001, min_notional_usd=100.0, maintenance_margin_rate=0.004
)
# A small-contract venue (Delta-style) friendlier to micro accounts.
SMALL_LIMITS = SymbolLimits(
    min_qty=0.0001, qty_step=0.0001, min_notional_usd=1.0, maintenance_margin_rate=0.005
)


def test_size_is_risk_based():
    """$400 equity, 1% risk = $4. Stop $1 away on a $100 asset -> 4 units."""
    result = size_position(
        equity_usd=400.0, entry_price=100.0, stop_price=99.0, side="long",
        config=RiskConfig(), limits=SMALL_LIMITS,
    )
    assert result.approved, result.reasons
    assert abs(result.quantity - 4.0) < 1e-9
    assert abs(result.risk_usd - 4.0) < 1e-9


def test_btc_untradeable_at_micro_size():
    """$200 equity, 1% risk = $2. BTC at 100k with a 5% stop -> qty 0.0004,
    below Binance's 0.001 minimum. Must reject, never widen risk."""
    result = size_position(
        equity_usd=200.0, entry_price=100_000.0, stop_price=95_000.0, side="long",
        config=RiskConfig(), limits=BTC_LIMITS,
    )
    assert not result.approved
    assert any("below exchange minimum" in r for r in result.reasons)


def test_per_symbol_exposure_cap_enforced():
    """$800 equity with a tight stop implies $800 notional -> exceeds the $500
    per-symbol default cap and must be rejected."""
    result = size_position(
        equity_usd=800.0, entry_price=100.0, stop_price=99.0, side="long",
        config=RiskConfig(), limits=SMALL_LIMITS,
    )
    assert not result.approved
    assert any("per-symbol cap" in r for r in result.reasons)


def test_wrong_side_stop_rejected():
    result = size_position(
        equity_usd=400.0, entry_price=100.0, stop_price=101.0, side="long",
        config=RiskConfig(), limits=SMALL_LIMITS,
    )
    assert not result.approved
    assert any("wrong side" in r for r in result.reasons)


def test_short_side_math():
    result = size_position(
        equity_usd=400.0, entry_price=100.0, stop_price=101.0, side="short",
        config=RiskConfig(), limits=SMALL_LIMITS,
    )
    assert result.approved, result.reasons
    assert abs(result.quantity - 4.0) < 1e-9


def test_quantity_rounds_down_to_step():
    """Rounding must never round UP past the risk budget."""
    result = size_position(
        equity_usd=377.0, entry_price=100.0, stop_price=99.0, side="long",
        config=RiskConfig(), limits=SMALL_LIMITS,
    )
    assert result.approved, result.reasons
    assert result.risk_usd <= 377.0 * 0.01 + 1e-9


def test_implied_leverage_above_cap_rejected():
    """Tight stop -> large size -> implied leverage above the 5x default cap."""
    cfg = RiskConfig(risk_per_trade_pct=3.0, max_exposure_per_symbol_usd=5000.0,
                     max_total_exposure_usd=5000.0)
    # risk $3 over a $0.30 stop -> qty 10, notional $1000 on $100 equity = 10x.
    result = size_position(
        equity_usd=100.0, entry_price=100.0, stop_price=99.7, side="long",
        config=cfg, limits=SMALL_LIMITS,
    )
    assert not result.approved
    assert any("implied leverage" in r for r in result.reasons)


def test_low_leverage_wide_stop_approved():
    cfg = RiskConfig(risk_per_trade_pct=3.0, max_exposure_per_symbol_usd=5000.0,
                     max_total_exposure_usd=5000.0)
    # risk $3 over a $20 stop -> qty 0.15, notional $15 -> 0.15x leverage.
    result = size_position(
        equity_usd=100.0, entry_price=100.0, stop_price=80.0, side="long",
        config=cfg, limits=SMALL_LIMITS,
    )
    assert result.approved, result.reasons
