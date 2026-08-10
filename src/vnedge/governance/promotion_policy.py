"""Single versioned source of promotion thresholds.

The values preserve the behavior that existed before policy centralization.
Changing any threshold requires publishing a new policy version rather than
editing a consumer-specific magic number.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WalkForwardRequirements(_FrozenModel):
    min_splits: int = Field(ge=1)
    min_total_oos_trades: int = Field(ge=1)
    min_profit_factor: float = Field(ge=1.0)
    max_window_drawdown_pct: float = Field(gt=0)
    min_is_retention: float = Field(ge=0, le=1)
    reject_zero_trade_windows: bool
    min_windows_with_trades_pct: float = Field(ge=0, le=100)
    min_payoff_ratio: float = Field(ge=0)
    max_single_trade_profit_share: float = Field(gt=0, le=1)


class WalkForwardProfiles(_FrozenModel):
    standard: WalkForwardRequirements
    sparse: WalkForwardRequirements
    offensive: WalkForwardRequirements


class GovernanceRequirements(_FrozenModel):
    minimum_trades: int = Field(ge=1)
    minimum_average_net_bps: float
    minimum_profit_factor: float = Field(ge=1.0)
    untouched_required: bool = True
    verifier_required: bool = True
    human_approval_required: bool = True


class LadderRequirements(_FrozenModel):
    min_paper_days: float = Field(gt=0)
    min_paper_trades: int = Field(ge=1)
    max_paper_drawdown_pct: float = Field(gt=0)
    min_shadow_days: float = Field(gt=0)
    min_shadow_trades: int = Field(ge=1)
    min_shadow_profit_factor: float = Field(ge=1.0)
    max_shadow_drawdown_pct: float = Field(gt=0)
    min_live_small_days: float = Field(gt=0)
    min_live_small_trades: int = Field(ge=1)
    max_live_small_drawdown_pct: float = Field(gt=0)


class PromotionPolicy(_FrozenModel):
    schema_version: Literal["1"] = "1"
    policy_version: str
    walk_forward: WalkForwardProfiles
    governance: GovernanceRequirements
    ladder: LadderRequirements

    @model_validator(mode="after")
    def validate_version(self) -> PromotionPolicy:
        if not self.policy_version or any(character.isspace() for character in self.policy_version):
            raise ValueError("policy_version must be a non-empty token")
        return self

    @property
    def sha256(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(payload).hexdigest()

    def walk_forward_profile(self, name: str) -> WalkForwardRequirements:
        if name not in {"standard", "sparse", "offensive"}:
            raise ValueError(f"unknown walk-forward policy profile: {name}")
        return getattr(self.walk_forward, name)


_STANDARD = WalkForwardRequirements(
    min_splits=3,
    min_total_oos_trades=10,
    min_profit_factor=1.10,
    max_window_drawdown_pct=15.0,
    min_is_retention=0.25,
    reject_zero_trade_windows=True,
    min_windows_with_trades_pct=0.0,
    min_payoff_ratio=0.0,
    max_single_trade_profit_share=1.0,
)

DEFAULT_PROMOTION_POLICY = PromotionPolicy(
    policy_version="v1.2.0",
    walk_forward=WalkForwardProfiles(
        standard=_STANDARD,
        sparse=_STANDARD.model_copy(
            update={
                "reject_zero_trade_windows": False,
                "min_windows_with_trades_pct": 60.0,
            }
        ),
        offensive=_STANDARD.model_copy(
            update={
                "min_total_oos_trades": 15,
                "min_profit_factor": 1.25,
                "max_window_drawdown_pct": 12.0,
                "reject_zero_trade_windows": False,
                "min_windows_with_trades_pct": 50.0,
                "min_payoff_ratio": 1.8,
                "max_single_trade_profit_share": 0.40,
            }
        ),
    ),
    governance=GovernanceRequirements(
        minimum_trades=20,
        minimum_average_net_bps=25.0,
        minimum_profit_factor=1.50,
    ),
    ladder=LadderRequirements(
        min_paper_days=14.0,
        min_paper_trades=10,
        max_paper_drawdown_pct=6.0,
        min_shadow_days=7.0,
        min_shadow_trades=10,
        min_shadow_profit_factor=1.05,
        max_shadow_drawdown_pct=6.0,
        min_live_small_days=7.0,
        min_live_small_trades=5,
        max_live_small_drawdown_pct=3.0,
    ),
)
