"""Aggressive PAPER sizing profile: fixed isolated margin at up to 30x.

The whole point of the design is that it can be aggressive WITHOUT being unsafe:
leverage is auto-reduced per trade so the stop always fires before liquidation
(max loss <= margin), it never exceeds the 30x hard cap, and the DEFAULT config
(and therefore the live path) is completely unchanged."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from vnedge.config.risk_config import RiskConfig
from vnedge.risk.kill_switch import KillSwitch
from vnedge.risk.position_sizer import SymbolLimits, size_position
from vnedge.risk.risk_manager import (
    AccountState,
    MarketState,
    OrderIntent,
    PreTradeRiskGateway,
)
from vnedge.runtime.multi_lane import LaneSpec, _lane_risk_config
from vnedge.runtime.runner_config import RunnerMode

_LIM = SymbolLimits(min_qty=1e-6, qty_step=1e-6, min_notional_usd=5.0, maintenance_margin_rate=0.005)
_SPEC = LaneSpec(lane_id="x", exchange="binanceusdm", symbol="BTC/USDT:USDT",
                 daily_loss_usd=10.0, mode=RunnerMode.SHADOW)


def _aggressive() -> RiskConfig:
    return _lane_risk_config(_SPEC, {"MULTI_LANE_FIXED_MARGIN": "50",
                                     "MULTI_LANE_FIXED_MARGIN_LEVERAGE": "30",
                                     "MULTI_LANE_DAILY_LOSS_HALT": "0"})


@pytest.mark.parametrize("stop_pct,exp_lev_max", [(1.0, 30), (3.0, 30), (5.0, 20), (10.0, 10)])
def test_fixed_margin_maxes_leverage_on_tight_stops_reduces_on_wide(stop_pct, exp_lev_max):
    cfg = _aggressive()
    entry = 60000.0
    stop = entry * (1 - stop_pct / 100.0)
    r = size_position(equity_usd=500.0, entry_price=entry, stop_price=stop,
                      side="long", config=cfg, limits=_LIM)
    assert r.approved, r.reasons
    # isolated margin is always exactly the committed $50
    assert abs(r.notional_usd / r.required_leverage - 50.0) < 0.5
    # tight stops get max leverage; wide stops get less (never more)
    assert r.required_leverage <= exp_lev_max
    # THE INVARIANT: worst-case loss never exceeds the fixed margin
    assert r.risk_usd <= 50.0 + 1e-6


def test_fixed_margin_never_exceeds_30x_even_if_env_asks_for_50():
    cfg = _lane_risk_config(_SPEC, {"MULTI_LANE_FIXED_MARGIN": "50",
                                    "MULTI_LANE_FIXED_MARGIN_LEVERAGE": "50"})
    assert cfg.max_leverage_per_position == 30  # clamped to the hard cap
    r = size_position(equity_usd=500.0, entry_price=60000, stop_price=59400,
                      side="long", config=cfg, limits=_LIM)
    assert r.required_leverage <= 30.0


def test_daily_loss_halt_off_lets_a_losing_day_keep_trading():
    cfg = _aggressive()
    assert cfg.daily_loss_halt_enabled is False
    r = size_position(equity_usd=500.0, entry_price=60000, stop_price=59400,
                      side="long", config=cfg, limits=_LIM)
    intent = OrderIntent(symbol="BTC/USDT:USDT", side="long", quantity=r.quantity,
                         notional_usd=r.notional_usd, leverage=r.required_leverage, strategy_id="s")
    acct = AccountState(equity_usd=500.0, daily_pnl_usd=-40.0, peak_equity_usd=500.0,
                        open_positions=0, total_exposure_usd=0.0)
    mkt = MarketState(symbol="BTC/USDT:USDT", last_update=datetime.now(UTC), spread_bps=2.0,
                      estimated_slippage_bps=3.0, funding_rate=0.0, exchange_healthy=True)
    gw = PreTradeRiskGateway(cfg, KillSwitch(kill_file=Path("/tmp/no-kill-a")))
    decision = gw.evaluate(intent, acct, mkt)
    assert decision.approved, decision.failed_checks
    # the check is SKIPPED, not merely passed — no bypass, just a no-op
    assert not any("daily_loss_limit" in c for c in decision.passed_checks + decision.failed_checks)


def test_default_config_is_unchanged_and_still_halts():
    # no env -> risk-based sizing, halt ON, 5x cap, tight exposure (the live model)
    cfg = _lane_risk_config(_SPEC, {})
    assert cfg.fixed_margin_usd is None
    assert cfg.daily_loss_halt_enabled is True
    assert cfg.max_leverage_per_position == 5
    # and the same -$40 day is HALTED under the default
    intent = OrderIntent(symbol="BTC/USDT:USDT", side="long", quantity=0.01,
                         notional_usd=100.0, leverage=1.0, strategy_id="s")
    acct = AccountState(equity_usd=500.0, daily_pnl_usd=-40.0, peak_equity_usd=500.0,
                        open_positions=0, total_exposure_usd=0.0)
    mkt = MarketState(symbol="BTC/USDT:USDT", last_update=datetime.now(UTC), spread_bps=2.0,
                      estimated_slippage_bps=3.0, funding_rate=0.0, exchange_healthy=True)
    gw = PreTradeRiskGateway(cfg, KillSwitch(kill_file=Path("/tmp/no-kill-b")))
    decision = gw.evaluate(intent, acct, mkt)
    assert not decision.approved
    assert any("daily_loss_limit" in c for c in decision.failed_checks)


def test_risk_based_default_still_produces_sub_1x_positions():
    # sanity: without the profile, sizing is the small risk-based notional
    cfg = _lane_risk_config(_SPEC, {})
    r = size_position(equity_usd=500.0, entry_price=60000, stop_price=59400,
                      side="long", config=cfg, limits=_LIM)
    assert r.approved
    assert r.notional_usd < 500.0  # sub-1x, as before the profile
