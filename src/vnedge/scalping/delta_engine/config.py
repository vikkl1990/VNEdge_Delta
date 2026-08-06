"""Validated YAML configuration for the Delta scalper research engine."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from vnedge.scalping.delta_engine.types import Regime


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EngineSettings(_StrictModel):
    mode: str = "research"
    symbols: tuple[str, ...] = ("BTCUSD", "ETHUSD")
    primary_timeframes: tuple[str, ...] = ("1m", "5m")
    min_probability: float = Field(default=0.70, ge=0, le=1)
    min_confidence: float = Field(default=0.60, ge=0, le=1)
    min_expectancy_bps: float = Field(default=8.0, ge=0)
    live_orders_enabled: bool = False
    can_promote: bool = False

    @model_validator(mode="after")
    def enforce_research_lock(self) -> EngineSettings:
        if self.mode != "research" or self.live_orders_enabled or self.can_promote:
            raise ValueError("delta scalper v1 configuration must remain research-only")
        if not self.symbols or any(not symbol.strip() for symbol in self.symbols):
            raise ValueError("at least one non-empty symbol is required")
        if not self.primary_timeframes:
            raise ValueError("at least one primary timeframe is required")
        if len(set(self.primary_timeframes)) != len(self.primary_timeframes):
            raise ValueError("primary timeframes must be unique")
        if any(tf not in {"1m", "5m"} for tf in self.primary_timeframes):
            raise ValueError("only 1m and 5m may trigger scanner evaluation")
        return self


class FeeModelSettings(_StrictModel):
    deto_enabled: bool = False
    scalper_opted_in: bool = False
    maker_fee_bps_pre_tax: float = Field(default=2.0, ge=0)
    taker_fee_bps_pre_tax: float = Field(default=5.0, ge=0)
    gst_rate: float = Field(default=0.18, ge=0)
    default_slippage_bps_per_leg: float = Field(default=1.5, ge=0)


class FeatureSettings(_StrictModel):
    max_bars_per_timeframe: int = Field(default=700, ge=100)
    l2_imbalance_history: int = Field(default=240, ge=20)
    trade_flow_window_seconds: int = Field(default=15, ge=1)
    max_l2_age_seconds: float = Field(default=2.0, gt=0)
    regime_fast_ema: int = Field(default=12, ge=2)
    regime_slow_ema: int = Field(default=36, ge=3)
    regime_efficiency_window: int = Field(default=12, ge=2)
    regime_profile_timeframe: str = "5m"
    regime_profile_adx_window: int = Field(default=14, ge=5)
    regime_profile_strong_trend_adx: float = Field(default=30.0, gt=0)
    regime_profile_range_adx_max: float = Field(default=22.0, ge=0)
    regime_profile_ema_fast: int = Field(default=20, ge=2)
    regime_profile_ema_slow: int = Field(default=50, ge=3)
    regime_profile_ema_separation_atr_min: float = Field(default=0.8, gt=0)
    regime_profile_atr_window: int = Field(default=14, ge=5)
    regime_profile_percentile_window: int = Field(default=200, ge=50)
    regime_profile_high_vol_percentile: float = Field(default=0.75, gt=0, lt=1)
    regime_profile_low_vol_percentile: float = Field(default=0.30, gt=0, lt=1)
    regime_profile_bollinger_window: int = Field(default=20, ge=5)
    change_point_timeframe: str = "5m"
    change_point_minimum_history_bars: int = Field(default=50, ge=20)
    change_point_baseline_window_bars: int = Field(default=200, ge=20)
    change_point_cusum_drift_z: float = Field(default=0.50, gt=0)
    change_point_cusum_threshold_z: float = Field(default=8.0, gt=0)
    change_point_cooldown_bars: int = Field(default=6, ge=0)

    @model_validator(mode="after")
    def validate_regime_windows(self) -> FeatureSettings:
        if self.regime_slow_ema <= self.regime_fast_ema:
            raise ValueError("regime_slow_ema must exceed regime_fast_ema")
        if self.regime_profile_timeframe not in {"1m", "5m"}:
            raise ValueError("regime_profile_timeframe must be 1m or 5m")
        if self.regime_profile_ema_slow <= self.regime_profile_ema_fast:
            raise ValueError("regime profile slow EMA must exceed fast EMA")
        if self.regime_profile_strong_trend_adx <= self.regime_profile_range_adx_max:
            raise ValueError("strong trend ADX must exceed range ADX")
        if (
            self.regime_profile_low_vol_percentile
            >= self.regime_profile_high_vol_percentile
        ):
            raise ValueError("low volatility percentile must be below high")
        if self.change_point_timeframe not in {"1m", "5m"}:
            raise ValueError("change_point_timeframe must be 1m or 5m")
        if (
            self.change_point_baseline_window_bars
            < self.change_point_minimum_history_bars
        ):
            raise ValueError("change-point baseline must cover minimum history")
        if (
            self.change_point_cusum_threshold_z
            <= self.change_point_cusum_drift_z
        ):
            raise ValueError("change-point CUSUM threshold must exceed drift")
        return self


class MomentumSettings(_StrictModel):
    enabled: bool = True
    prefer_maker: bool = True
    min_volume_z: float = 0.75
    min_body_ratio: float = Field(default=0.55, ge=0, le=1)
    min_breakout_bps: float = Field(default=0.4, ge=0)
    time_stop_seconds: int = Field(default=1_680, gt=0, le=1_800)
    enabled_regimes: tuple[Regime, ...] = (
        Regime.QUIET,
        Regime.TRENDING_UP,
        Regime.TRENDING_DOWN,
        Regime.EXPANDING,
        Regime.FUNDING_EXTREME,
    )

    @model_validator(mode="after")
    def validate_enabled_regimes(self) -> MomentumSettings:
        if not self.enabled_regimes or Regime.UNKNOWN in self.enabled_regimes:
            raise ValueError("momentum enabled_regimes must be non-empty and exclude unknown")
        if len(set(self.enabled_regimes)) != len(self.enabled_regimes):
            raise ValueError("momentum enabled_regimes must be unique")
        return self


class ImbalanceFadeSettings(_StrictModel):
    enabled: bool = True
    prefer_maker: bool = True
    min_wick_ratio: float = Field(default=0.48, ge=0, le=1)
    min_stretch_bps: float = Field(default=7.0, ge=0)
    time_stop_seconds: int = Field(default=1_680, gt=0, le=1_800)
    enabled_regimes: tuple[Regime, ...] = (Regime.QUIET, Regime.EXPANDING)

    @model_validator(mode="after")
    def validate_enabled_regimes(self) -> ImbalanceFadeSettings:
        if not self.enabled_regimes or Regime.UNKNOWN in self.enabled_regimes:
            raise ValueError("fade enabled_regimes must be non-empty and exclude unknown")
        if len(set(self.enabled_regimes)) != len(self.enabled_regimes):
            raise ValueError("fade enabled_regimes must be unique")
        return self


class ScannerSettings(_StrictModel):
    momentum_burst: MomentumSettings = MomentumSettings()
    imbalance_fade: ImbalanceFadeSettings = ImbalanceFadeSettings()


class PromotionSettings(_StrictModel):
    paper_only_after_all_gates: bool = True
    minimum_positive_markets: int = Field(default=2, ge=2)
    minimum_profit_factor_after_costs: float = Field(default=1.2, gt=1)
    maximum_single_market_share: float = Field(default=0.70, gt=0, lt=1)
    require_multiple_months: bool = True
    require_closed_candles: bool = True
    require_no_repainting: bool = True


class DeltaScalperConfig(_StrictModel):
    engine: EngineSettings = EngineSettings()
    fee_model: FeeModelSettings = FeeModelSettings()
    features: FeatureSettings = FeatureSettings()
    scanners: ScannerSettings = ScannerSettings()
    promotion: PromotionSettings = PromotionSettings()


def load_delta_scalper_config(
    path: Path | str = "configs/delta_scalper.yaml",
) -> DeltaScalperConfig:
    source = Path(path)
    payload = yaml.safe_load(source.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"invalid Delta scalper config: {source}")
    return DeltaScalperConfig.model_validate(payload)
