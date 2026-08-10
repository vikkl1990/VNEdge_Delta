"""Risk config validation — the leverage policy must be enforced at load time."""

import pytest
from pydantic import ValidationError

from vnedge.config.risk_config import ABSOLUTE_MAX_LEVERAGE, RiskConfig


def test_defaults_are_conservative():
    cfg = RiskConfig()
    assert cfg.max_leverage_per_position <= 5
    assert cfg.max_daily_loss_usd == 20.0
    assert cfg.acknowledge_high_leverage is False


def test_leverage_above_10x_requires_acknowledgment():
    with pytest.raises(ValidationError, match="liquidates"):
        RiskConfig(max_leverage_per_position=15)


def test_leverage_above_10x_allowed_with_acknowledgment():
    cfg = RiskConfig(max_leverage_per_position=15, acknowledge_high_leverage=True)
    assert cfg.max_leverage_per_position == 15


def test_absolute_leverage_ceiling():
    with pytest.raises(ValidationError):
        RiskConfig(
            max_leverage_per_position=ABSOLUTE_MAX_LEVERAGE + 1,
            acknowledge_high_leverage=True,
        )


def test_symbol_exposure_cannot_exceed_total():
    with pytest.raises(ValidationError, match="max_total_exposure_usd"):
        RiskConfig(max_exposure_per_symbol_usd=2000.0, max_total_exposure_usd=1000.0)


def test_config_is_frozen():
    cfg = RiskConfig()
    with pytest.raises(ValidationError):
        cfg.max_daily_loss_usd = 10_000.0  # type: ignore[misc]
