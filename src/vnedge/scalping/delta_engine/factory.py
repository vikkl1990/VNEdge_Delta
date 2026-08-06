"""One assembly path shared by the live shadow service and offline replay."""

from __future__ import annotations

from dataclasses import dataclass

from vnedge.execution.journal import DecisionJournal
from vnedge.scalping.delta_engine.candle_store import MultiTimeframeCandleStore
from vnedge.scalping.delta_engine.change_point import CausalCusumConfig
from vnedge.scalping.delta_engine.config import DeltaScalperConfig
from vnedge.scalping.delta_engine.context import MarketContextBuilder
from vnedge.scalping.delta_engine.fee_model import DeltaFeeModel
from vnedge.scalping.delta_engine.regime import (
    RegimeConfig,
    RegimeEngine,
    RegimeProfileConfig,
)
from vnedge.scalping.delta_engine.scanners import (
    ImbalanceFadeConfig,
    MomentumBurstConfig,
    MomentumBurstScanner,
    OrderFlowImbalanceFadeScanner,
    Scanner,
)
from vnedge.scalping.delta_engine.signal_generator import (
    DeltaScalperSignalGenerator,
    SignalGateConfig,
)


@dataclass(frozen=True)
class DeltaScalperAssembly:
    context: MarketContextBuilder
    fee_model: DeltaFeeModel
    scanners: tuple[Scanner, ...]
    generator: DeltaScalperSignalGenerator


def build_delta_scalper_assembly(
    store: MultiTimeframeCandleStore,
    config: DeltaScalperConfig,
    *,
    journal: DecisionJournal | None = None,
    deto_enabled: bool = False,
    scalper_opted_in: bool = False,
    slippage_bps: float | None = None,
) -> DeltaScalperAssembly:
    """Build identical context, scanner, fee, and gate modules for live/replay."""

    context = MarketContextBuilder(
        store,
        RegimeEngine(
            RegimeConfig(
                fast_ema=config.features.regime_fast_ema,
                slow_ema=config.features.regime_slow_ema,
                efficiency_window=config.features.regime_efficiency_window,
            )
        ),
        RegimeProfileConfig(
            source_timeframe=config.features.regime_profile_timeframe,
            adx_window=config.features.regime_profile_adx_window,
            strong_trend_adx=config.features.regime_profile_strong_trend_adx,
            range_adx_max=config.features.regime_profile_range_adx_max,
            ema_fast=config.features.regime_profile_ema_fast,
            ema_slow=config.features.regime_profile_ema_slow,
            ema_separation_atr_min=(
                config.features.regime_profile_ema_separation_atr_min
            ),
            atr_window=config.features.regime_profile_atr_window,
            volatility_percentile_window=(
                config.features.regime_profile_percentile_window
            ),
            high_volatility_percentile=(
                config.features.regime_profile_high_vol_percentile
            ),
            low_volatility_percentile=(
                config.features.regime_profile_low_vol_percentile
            ),
            bollinger_window=config.features.regime_profile_bollinger_window,
        ),
        CausalCusumConfig(
            source_timeframe=config.features.change_point_timeframe,
            minimum_history_bars=(
                config.features.change_point_minimum_history_bars
            ),
            baseline_window_bars=(
                config.features.change_point_baseline_window_bars
            ),
            drift_z=config.features.change_point_cusum_drift_z,
            threshold_z=config.features.change_point_cusum_threshold_z,
            cooldown_bars=config.features.change_point_cooldown_bars,
        ),
        max_l2_age_seconds=config.features.max_l2_age_seconds,
    )
    fee_model = DeltaFeeModel(
        deto_enabled=deto_enabled or config.fee_model.deto_enabled,
        scalper_opted_in=scalper_opted_in or config.fee_model.scalper_opted_in,
        maker_fee_bps_pre_tax=config.fee_model.maker_fee_bps_pre_tax,
        taker_fee_bps_pre_tax=config.fee_model.taker_fee_bps_pre_tax,
        gst_rate=config.fee_model.gst_rate,
        default_slippage_bps_per_leg=(
            config.fee_model.default_slippage_bps_per_leg
            if slippage_bps is None
            else slippage_bps
        ),
    )
    scanners: tuple[Scanner, ...] = (
        *(
            (
                MomentumBurstScanner(
                    fee_model,
                    config=MomentumBurstConfig(
                        min_volume_z=config.scanners.momentum_burst.min_volume_z,
                        min_body_ratio=config.scanners.momentum_burst.min_body_ratio,
                        min_breakout_bps=config.scanners.momentum_burst.min_breakout_bps,
                        time_stop_seconds=config.scanners.momentum_burst.time_stop_seconds,
                        prefer_maker=config.scanners.momentum_burst.prefer_maker,
                        enabled_regimes=config.scanners.momentum_burst.enabled_regimes,
                    ),
                ),
            )
            if config.scanners.momentum_burst.enabled
            else ()
        ),
        *(
            (
                OrderFlowImbalanceFadeScanner(
                    fee_model,
                    config=ImbalanceFadeConfig(
                        min_wick_ratio=config.scanners.imbalance_fade.min_wick_ratio,
                        min_stretch_bps=config.scanners.imbalance_fade.min_stretch_bps,
                        time_stop_seconds=config.scanners.imbalance_fade.time_stop_seconds,
                        prefer_maker=config.scanners.imbalance_fade.prefer_maker,
                        enabled_regimes=config.scanners.imbalance_fade.enabled_regimes,
                    ),
                ),
            )
            if config.scanners.imbalance_fade.enabled
            else ()
        ),
    )
    generator = DeltaScalperSignalGenerator(
        context,
        scanners,
        journal=journal,
        gates=SignalGateConfig(
            min_expectancy_bps=config.engine.min_expectancy_bps,
            min_probability=config.engine.min_probability,
            min_confidence=config.engine.min_confidence,
            allowed_symbols=config.engine.symbols,
            primary_timeframes=config.engine.primary_timeframes,
        ),
    )
    return DeltaScalperAssembly(context, fee_model, scanners, generator)
