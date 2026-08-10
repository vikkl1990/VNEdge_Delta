"""Runner configuration for paper/shadow loops."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from vnedge.config.risk_config import RiskConfig
from vnedge.risk.position_sizer import SymbolLimits
from vnedge.risk.protections import ProtectionConfig
from vnedge.runtime.daily_factory import DailySignalFactoryConfig


class RunnerMode(str, Enum):
    PAPER = "paper"
    SHADOW = "shadow"


class RunnerConfig(BaseModel):
    model_config = {"frozen": True, "arbitrary_types_allowed": True}

    mode: RunnerMode = RunnerMode.SHADOW  # safe default, like everything here
    symbol: str = "BTC/USDT:USDT"
    timeframe: str = "1h"
    starting_equity_usd: float = Field(default=500.0, gt=0)
    spread_bps: float = Field(default=1.0, ge=0)
    slippage_est_bps: float = Field(default=2.0, ge=0)
    max_holding_bars: int = Field(default=48, ge=1)
    reconcile_every_bars: int = Field(default=24, ge=1)
    # Dynamic ATR-chandelier trailing on the active-exit remainder (DEFAULT OFF).
    # Same engine + canonical ATR the backtester uses, so research and runtime
    # trail identically. 0.0 = legacy arm-and-lock. Only enable in BOTH backtest
    # and runtime together, or the two diverge.
    trail_atr_mult: float = Field(default=0.0, ge=0.0)
    trail_atr_window: int = Field(default=14, ge=1)
    # LEGACY ALIAS (PR #92) — maps into protections.cooldown_bars_after_stop
    # via effective_protections(). Semantics were refined when the protections
    # state machine landed: the cooldown now arms on STOP exits only (a winner
    # closing is not evidence the entry condition went bad).
    # DEFAULT OFF: enabling this changes entry behavior — running trials are
    # frozen, so it may only be turned on via a pre-registered future protocol.
    post_exit_cooldown_bars: int = Field(default=0, ge=0)
    # Entry protections (risk/protections.py): post-stop cooldown and the
    # stop-window guard. ALL DEFAULT OFF; enabling any of them on a trial
    # requires pre-registration (docs/PROTECTIONS.md). Exits are never
    # affected — the state machine has no exit-blocking path at all.
    protections: ProtectionConfig = Field(default_factory=ProtectionConfig)
    # Daily signal-factory discipline. Default OFF so existing judged trials
    # keep their exact behavior; enable per lane/profile when the operating
    # contract is intraday-only: no late entries, force-flat before session
    # close, and stop after the daily target is banked.
    daily_factory: DailySignalFactoryConfig = Field(
        default_factory=DailySignalFactoryConfig
    )
    # Tick-granular STOP monitoring: between bar closes, the idle loop checks
    # the live top-of-book against the open plan's stop and exits reduce-only
    # on breach. STOPS ONLY — a stop is capital protection, so it gets the
    # finest granularity available; take-profits stay bar-close because TP
    # timing is strategy semantics that the backtester models at bar
    # granularity (tick-level TPs would make paper diverge from research).
    tick_stops_enabled: bool = True
    risk: RiskConfig = Field(default_factory=RiskConfig)
    limits: SymbolLimits = Field(
        default=SymbolLimits(
            min_qty=0.0001, qty_step=0.0001, min_notional_usd=5.0,
            maintenance_margin_rate=0.005,
        )
    )

    def effective_protections(self) -> ProtectionConfig:
        """Protections with the legacy post_exit_cooldown_bars alias folded in.

        The stricter of the two cooldown values wins, so a config that sets
        either field keeps its protection.
        """
        if self.post_exit_cooldown_bars <= self.protections.cooldown_bars_after_stop:
            return self.protections
        return self.protections.model_copy(
            update={"cooldown_bars_after_stop": self.post_exit_cooldown_bars}
        )
