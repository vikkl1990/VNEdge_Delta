"""Risk configuration — the single source of truth for every risk limit.

Every limit here is enforced by the pre-trade risk gateway
(:mod:`vnedge.risk.risk_manager`). There is deliberately no code path that
places an order without passing through those checks.

Leverage policy (decided 2026-07-02):
    - Default per-position leverage is 5x.
    - Anything above 10x requires ``acknowledge_high_leverage=True`` — an
      explicit, written opt-in that the operator understands liquidation risk.
    - 30x is the absolute ceiling; config validation rejects higher values.
    - Position SIZE is always derived from risk-per-trade and stop distance,
      never from leverage. Leverage only determines margin efficiency.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

# Above this, the operator must explicitly acknowledge liquidation risk.
HIGH_LEVERAGE_THRESHOLD = 10
# Absolute ceiling. Not configurable. Raising it means editing this file and
# owning that decision in code review.
ABSOLUTE_MAX_LEVERAGE = 30


class RiskConfig(BaseModel):
    """All hard risk limits. Frozen at startup; changing limits mid-session
    requires a restart so every change is deliberate and logged."""

    model_config = {"frozen": True}

    # --- Loss limits ---------------------------------------------------------
    max_daily_loss_usd: float = Field(
        default=20.0, gt=0,
        description="Fixed USD daily-loss halt. Effective limit is "
        "min(this, max_daily_loss_pct of equity) — whichever is tighter wins.",
    )
    max_daily_loss_pct: float = Field(
        default=2.0, gt=0, le=10,
        description="Percentage leg of the daily-loss halt, applied to peak equity.",
    )
    daily_loss_halt_enabled: bool = Field(
        default=True,
        description=(
            "When False, the daily-loss halt check is skipped. PAPER/experiment "
            "use only — the live path must keep this True. Defaults True so live "
            "and every other lane keep the halt; only a lane that explicitly opts "
            "out (an aggressive paper profile) turns it off."
        ),
    )
    max_drawdown_pct: float = Field(
        default=15.0, gt=0, le=50,
        description="Peak-to-trough equity drawdown (%) that disables trading until manual reset.",
    )
    min_account_equity_usd: float = Field(
        default=100.0, gt=0,
        description="Below this equity, no new positions may be opened.",
    )

    # --- Position sizing -----------------------------------------------------
    risk_per_trade_pct: float = Field(
        default=1.0, gt=0, le=3.0,
        description="Max % of equity lost if the stop is hit. Drives position size.",
    )
    fixed_margin_usd: float | None = Field(
        default=None, gt=0,
        description=(
            "OPT-IN, PAPER-ONLY alternative to risk-based sizing (the default and "
            "the locked live model stay None = risk-based). When set, each trade "
            "commits this isolated margin and sizes notional = margin x leverage, "
            "where leverage is capped per-trade so the stop always sits INSIDE the "
            "isolated liquidation distance — so max loss <= this margin is "
            "guaranteed. Still bounded by max_leverage_per_position (<= 30) + the "
            "high-leverage ack. This deliberately departs from 'size from risk, "
            "never leverage' and must never be set on a live config."
        ),
    )
    max_consecutive_losses: int = Field(
        default=4, ge=1, le=20,
        description=(
            "After this many consecutive losing trades, no new entries until "
            "manual review. A losing streak usually means the regime changed "
            "before the strategy noticed."
        ),
    )
    max_open_positions: int = Field(default=2, ge=1, le=10)
    max_exposure_per_symbol_usd: float = Field(default=500.0, gt=0)
    max_total_exposure_usd: float = Field(default=1000.0, gt=0)

    # --- Leverage ------------------------------------------------------------
    max_leverage_per_position: int = Field(
        default=5, ge=1, le=ABSOLUTE_MAX_LEVERAGE,
        description="Hard cap on exchange leverage setting per position.",
    )
    acknowledge_high_leverage: bool = Field(
        default=False,
        description=f"Must be True to configure leverage above {HIGH_LEVERAGE_THRESHOLD}x.",
    )
    max_effective_account_leverage: float = Field(
        default=2.0, gt=0, le=10.0,
        description="Cap on total notional / equity across all positions.",
    )
    min_liquidation_buffer_pct: float = Field(
        default=20.0, gt=0,
        description=(
            "Liquidation price must be at least this % further from entry than "
            "the stop loss. A stop that sits near the liquidation price is not a stop."
        ),
    )

    # --- Market-quality gates ------------------------------------------------
    max_spread_bps: float = Field(
        default=5.0, gt=0,
        description="Reject entries when bid/ask spread exceeds this (basis points).",
    )
    max_slippage_bps: float = Field(
        default=10.0, gt=0,
        description="Reject entries when estimated slippage exceeds this (basis points).",
    )
    max_data_staleness_seconds: float = Field(
        default=5.0, gt=0,
        description="Reject orders when market data is older than this.",
    )
    max_abs_funding_rate: float = Field(
        default=0.001,  # 0.1% per 8h interval
        gt=0,
        description="Reject entries paying funding above this per-interval rate against us.",
    )

    @model_validator(mode="after")
    def _validate_leverage_policy(self) -> "RiskConfig":
        if (
            self.max_leverage_per_position > HIGH_LEVERAGE_THRESHOLD
            and not self.acknowledge_high_leverage
        ):
            raise ValueError(
                f"max_leverage_per_position={self.max_leverage_per_position} exceeds "
                f"{HIGH_LEVERAGE_THRESHOLD}x. At this leverage a "
                f"~{100 / self.max_leverage_per_position:.1f}% adverse move liquidates "
                "the position. Set acknowledge_high_leverage=true only if you accept that."
            )
        return self

    @model_validator(mode="after")
    def _validate_exposure_consistency(self) -> "RiskConfig":
        if self.max_exposure_per_symbol_usd > self.max_total_exposure_usd:
            raise ValueError(
                "max_exposure_per_symbol_usd cannot exceed max_total_exposure_usd"
            )
        return self
