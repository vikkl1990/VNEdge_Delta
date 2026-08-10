"""Application settings — env-driven, secrets never in code.

Loads from environment variables (prefix ``VNEDGE_``) and ``.env``. Nested
models use ``__`` as the delimiter, e.g. ``VNEDGE_RISK__MAX_DAILY_LOSS_USD=20``.

Operating-mode ladder (each step must be validated before the next):

    backtest -> paper -> shadow -> live_small -> live_full

    shadow                 live data through the full pipeline; orders are
                           journaled and evaluated, never sent.
    live_small             real orders, equity capped by live_small_capital_cap_usd.
    emergency_reduce_only  real orders allowed, but ONLY position-reducing ones.

Live-order gate — THREE independent conditions, so no single mistaken env
change can enable live trading:
    1. trading_mode is live_small / live_full / emergency_reduce_only
    2. live_trading_enabled = true
    3. confirm_live_trading = "I_UNDERSTAND_THIS_IS_HIGH_RISK" (exact phrase)
"""

from __future__ import annotations

from enum import Enum

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from vnedge.config.risk_config import RiskConfig

LIVE_CONFIRMATION_PHRASE = "I_UNDERSTAND_THIS_IS_HIGH_RISK"


class TradingMode(str, Enum):
    BACKTEST = "backtest"
    PAPER = "paper"
    SHADOW = "shadow"
    LIVE_SMALL = "live_small"
    LIVE_FULL = "live_full"
    EMERGENCY_REDUCE_ONLY = "emergency_reduce_only"


_LIVE_MODES = frozenset(
    {TradingMode.LIVE_SMALL, TradingMode.LIVE_FULL, TradingMode.EMERGENCY_REDUCE_ONLY}
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="VNEDGE_",
        env_file=".env",
        env_nested_delimiter="__",
        extra="ignore",
        frozen=True,
    )

    trading_mode: TradingMode = TradingMode.BACKTEST
    live_trading_enabled: bool = Field(
        default=False,
        description="Gate 2 of 3 for live orders.",
    )
    confirm_live_trading: str = Field(
        default="",
        description=f"Gate 3 of 3: must equal '{LIVE_CONFIRMATION_PHRASE}' exactly.",
    )
    live_small_capital_cap_usd: float = Field(
        default=100.0, gt=0,
        description="Equity ceiling enforced while in live_small mode.",
    )

    risk: RiskConfig = Field(default_factory=RiskConfig)

    base_currency: str = "USDT"
    log_level: str = "INFO"
    # Presence of this file trips the kill switch — an operator can halt the
    # bot from a shell with `touch KILL` even if the process is unresponsive
    # to normal signals.
    kill_switch_file: str = "KILL"

    @property
    def is_live(self) -> bool:
        """True only when all three live gates are open. Emergency reduce-only
        counts as live because flattening real positions sends real orders."""
        return (
            self.trading_mode in _LIVE_MODES
            and self.live_trading_enabled
            and self.confirm_live_trading == LIVE_CONFIRMATION_PHRASE
        )

    @property
    def entries_allowed(self) -> bool:
        """False in emergency_reduce_only: only position-reducing orders may flow."""
        return self.trading_mode is not TradingMode.EMERGENCY_REDUCE_ONLY
