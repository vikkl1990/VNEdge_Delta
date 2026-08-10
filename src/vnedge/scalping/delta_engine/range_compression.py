"""Preregistered causal range-compression breakout research scanner."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from statistics import fmean, median, pstdev
from types import MappingProxyType
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from vnedge.scalping.delta_engine.change_point import CausalCusumConfig
from vnedge.scalping.delta_engine.fee_model import DeltaFeeModel
from vnedge.scalping.delta_engine.regime import _true_ranges
from vnedge.scalping.delta_engine.types import (
    Candle,
    ChangePointProfile,
    Side,
    SignalCandidate,
)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CompressionDataConfig(_FrozenModel):
    symbols: tuple[Literal["BTCUSD", "ETHUSD"], ...] = ("BTCUSD", "ETHUSD")
    source_timeframe: Literal["1m"] = "1m"
    decision_timeframe: Literal["5m"] = "5m"
    start: str
    end: str
    require_closed_candles: Literal[True] = True
    require_zero_missing_minutes: Literal[True] = True


class CompressionDefinitionConfig(_FrozenModel):
    bollinger_window_bars: int = Field(default=20, ge=5)
    percentile_history_bars: int = Field(default=200, ge=50)
    maximum_width_percentile: float = Field(default=0.20, gt=0, lt=0.5)
    compression_observation_bars: int = Field(default=12, ge=2)
    minimum_compressed_bars: int = Field(default=6, ge=1)
    require_immediately_prior_compression: Literal[True] = True
    range_lookback_bars: int = Field(default=12, ge=2)

    @model_validator(mode="after")
    def validate_observation(self) -> CompressionDefinitionConfig:
        if self.minimum_compressed_bars > self.compression_observation_bars:
            raise ValueError("minimum compressed bars exceeds observation window")
        return self


class CompressionBreakoutConfig(_FrozenModel):
    minimum_close_beyond_range_bps: float = Field(default=2.0, gt=0)
    minimum_body_ratio: float = Field(default=0.60, gt=0, le=1)
    volume_history_bars: int = Field(default=20, ge=5)
    minimum_relative_volume: float = Field(default=1.50, gt=1)
    atr_window_bars: int = Field(default=14, ge=5)
    minimum_true_range_atr_multiple: float = Field(default=1.20, gt=1)
    require_recent_cusum_shift: Literal[True] = True
    maximum_bars_since_cusum_shift: int = Field(default=3, ge=0)
    cooldown_minutes: int = Field(default=360, ge=0)


class CompressionExitConfig(_FrozenModel):
    structural_buffer_bps: float = Field(default=2.0, ge=0)
    min_stop_bps: float = Field(default=15.0, gt=0)
    max_stop_bps: float = Field(default=60.0, gt=0)
    reject_stop_above_maximum: Literal[True] = True
    reward_risk: float = Field(default=2.50, gt=1)
    minimum_target_cost_multiple: float = Field(default=3.50, gt=1)
    expected_hold_seconds: int = Field(default=900, gt=0, le=1_800)
    time_stop_seconds: int = Field(default=1_800, gt=0, le=1_800)

    @model_validator(mode="after")
    def validate_exit(self) -> CompressionExitConfig:
        if self.max_stop_bps <= self.min_stop_bps:
            raise ValueError("max stop must exceed min stop")
        if self.expected_hold_seconds > self.time_stop_seconds:
            raise ValueError("expected hold cannot exceed time stop")
        return self


class CompressionStructuralPrior(_FrozenModel):
    probability: float = Field(default=0.75, ge=0.5, le=1)
    confidence: float = Field(default=0.65, ge=0, le=1)
    calibrated: Literal[False] = False
    promotion_eligible: Literal[False] = False


class CompressionCostConfig(_FrozenModel):
    prefer_maker: Literal[False] = False
    scalper_opted_in: Literal[False] = False
    deto_enabled: Literal[False] = False
    maker_fee_bps_pre_tax: float = Field(default=2.0, ge=0)
    taker_fee_bps_pre_tax: float = Field(default=5.0, ge=0)
    gst_rate: float = Field(default=0.18, ge=0)
    slippage_bps_per_leg: float = Field(default=1.5, ge=0)


class CompressionValidationConfig(_FrozenModel):
    selection_fraction: float = Field(default=0.80, gt=0, lt=1)
    untouched_fraction: float = Field(default=0.20, gt=0, lt=1)
    split_embargo_minutes: int = Field(default=30, ge=30)
    minimum_selection_trades: int = Field(default=100, ge=1)
    minimum_selection_trades_per_half: int = Field(default=35, ge=1)
    minimum_selection_trades_per_market: int = Field(default=30, ge=1)
    minimum_selection_average_net_bps: float = Field(default=3.0, gt=0)
    minimum_selection_profit_factor: float = Field(default=1.20, gt=1)
    minimum_trades_per_day: float = Field(default=0.10, gt=0)
    maximum_trades_per_day: float = Field(default=4.0, gt=0)
    require_positive_selection_halves: Literal[True] = True
    require_positive_selection_markets: Literal[True] = True
    open_untouched_only_after_selection_pass: Literal[True] = True

    @model_validator(mode="after")
    def validate_split(self) -> CompressionValidationConfig:
        if abs(self.selection_fraction + self.untouched_fraction - 1.0) > 1e-9:
            raise ValueError("selection and untouched fractions must sum to one")
        if self.maximum_trades_per_day <= self.minimum_trades_per_day:
            raise ValueError("maximum frequency must exceed minimum frequency")
        return self


class RangeCompressionConfig(_FrozenModel):
    contract_id: Literal["range_compression_breakout_v1"]
    status: Literal["preregistered"]
    research_only: Literal[True]
    can_trade: Literal[False]
    can_promote: Literal[False]
    data: CompressionDataConfig
    compression: CompressionDefinitionConfig = CompressionDefinitionConfig()
    breakout: CompressionBreakoutConfig = CompressionBreakoutConfig()
    cusum: CausalCusumConfig = CausalCusumConfig()
    exit: CompressionExitConfig = CompressionExitConfig()
    structural_prior: CompressionStructuralPrior = CompressionStructuralPrior()
    costs: CompressionCostConfig = CompressionCostConfig()
    validation: CompressionValidationConfig = CompressionValidationConfig()
    policy: dict[str, str]


def load_range_compression_config(path: Path | str) -> RangeCompressionConfig:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"invalid range-compression config: {path}")
    return RangeCompressionConfig.model_validate(payload)


@dataclass(frozen=True)
class RangeCompressionContext:
    symbol: str
    ts: datetime
    five_minute: tuple[Candle, ...]
    change_point: ChangePointProfile

    def __post_init__(self) -> None:
        if not self.five_minute or self.five_minute[-1].ts != self.ts:
            raise ValueError("range-compression context must end at decision time")
        if any(row.tf != "5m" or row.ts > self.ts for row in self.five_minute):
            raise ValueError("range-compression context requires closed 5m candles")


def _width(closes: list[float]) -> float:
    mean = fmean(closes)
    return 4.0 * pstdev(closes) / mean if mean else 0.0


def _percentile(values: list[float], current: float) -> float:
    return (
        sum(value < current for value in values)
        + 0.5 * sum(value == current for value in values)
    ) / len(values)


def _body_ratio(row: Candle) -> float:
    span = max(row.high - row.low, row.close * 1e-9)
    return abs(row.close - row.open) / span


def _price_at_bps(price: float, side: Side, bps: float) -> float:
    direction = 1.0 if side is Side.LONG else -1.0
    return price * (1.0 + direction * bps / 10_000.0)


class RangeCompressionBreakoutScanner:
    scanner_id = "range_compression_breakout_v1"

    def __init__(self, config: RangeCompressionConfig, fee_model: DeltaFeeModel) -> None:
        self.config = config
        self.fee_model = fee_model
        self._last_setup_id: str | None = None
        self._last_fire_ts: datetime | None = None
        self._processed_compression_ts: datetime | None = None
        self._compression_closes: deque[float] = deque(
            maxlen=self.config.compression.bollinger_window_bars
        )
        self._width_history: deque[float] = deque(
            maxlen=self.config.compression.percentile_history_bars
        )
        self._compression_history: deque[bool] = deque(
            maxlen=self.config.compression.compression_observation_bars + 1
        )

    @property
    def minimum_history(self) -> int:
        compression = self.config.compression
        return max(
            compression.bollinger_window_bars
            + compression.percentile_history_bars
            + compression.compression_observation_bars,
            compression.range_lookback_bars + 1,
            self.config.breakout.volume_history_bars + 1,
            self.config.breakout.atr_window_bars + 1,
        )

    def reset(self) -> None:
        self._last_setup_id = None
        self._last_fire_ts = None
        self._reset_compression_state()

    def _reset_compression_state(self) -> None:
        self._processed_compression_ts = None
        self._compression_closes.clear()
        self._width_history.clear()
        self._compression_history.clear()

    def _compression_flags(self, rows: tuple[Candle, ...]) -> list[bool]:
        """Incrementally reproduce each bar's past-only width percentile."""
        settings = self.config.compression
        start = 0
        if self._processed_compression_ts is not None:
            matches = [
                index
                for index, row in enumerate(rows)
                if row.ts == self._processed_compression_ts
            ]
            if matches:
                start = matches[-1] + 1
            else:
                self._reset_compression_state()
        for row in rows[start:]:
            self._compression_closes.append(row.close)
            compressed = False
            if len(self._compression_closes) == settings.bollinger_window_bars:
                current = _width(list(self._compression_closes))
                self._width_history.append(current)
                compressed = (
                    len(self._width_history) == settings.percentile_history_bars
                    and _percentile(list(self._width_history), current)
                    <= settings.maximum_width_percentile
                )
            self._compression_history.append(compressed)
            self._processed_compression_ts = row.ts
        flags = list(self._compression_history)
        return flags[-(settings.compression_observation_bars + 1) : -1]

    def evaluate(self, ctx: RangeCompressionContext) -> SignalCandidate | None:
        if len(ctx.five_minute) < self.minimum_history:
            return None
        breakout = self.config.breakout
        compression = self.config.compression
        flags = self._compression_flags(ctx.five_minute)
        if sum(flags) < compression.minimum_compressed_bars:
            return None
        if compression.require_immediately_prior_compression and not flags[-1]:
            return None
        change = ctx.change_point
        if (
            breakout.require_recent_cusum_shift
            and (
                not change.detector_ready
                or change.bars_since_shift is None
                or change.bars_since_shift > breakout.maximum_bars_since_cusum_shift
            )
        ):
            return None

        latest = ctx.five_minute[-1]
        prior = ctx.five_minute[:-1]
        range_rows = prior[-compression.range_lookback_bars :]
        range_high = max(row.high for row in range_rows)
        range_low = min(row.low for row in range_rows)
        up_bps = (latest.close / range_high - 1.0) * 10_000
        down_bps = (range_low / latest.close - 1.0) * 10_000
        if up_bps >= breakout.minimum_close_beyond_range_bps:
            side = Side.LONG
            close_beyond_bps = up_bps
        elif down_bps >= breakout.minimum_close_beyond_range_bps:
            side = Side.SHORT
            close_beyond_bps = down_bps
        else:
            return None
        if _body_ratio(latest) < breakout.minimum_body_ratio:
            return None
        volume_history = [row.volume for row in prior[-breakout.volume_history_bars :]]
        baseline_volume = median(volume_history)
        relative_volume = latest.volume / baseline_volume if baseline_volume > 0 else 0.0
        if relative_volume < breakout.minimum_relative_volume:
            return None
        atr = fmean(_true_ranges(prior)[-breakout.atr_window_bars :])
        previous_close = prior[-1].close
        true_range = max(
            latest.high - latest.low,
            abs(latest.high - previous_close),
            abs(latest.low - previous_close),
        )
        expansion_multiple = true_range / atr if atr > 0 else 0.0
        if expansion_multiple < breakout.minimum_true_range_atr_multiple:
            return None
        if self._last_fire_ts is not None and ctx.ts - self._last_fire_ts < timedelta(
            minutes=breakout.cooldown_minutes
        ):
            return None
        setup_id = f"{ctx.symbol}:{prior[-1].ts.isoformat()}:{side.value}"
        if setup_id == self._last_setup_id:
            return None

        exit_config = self.config.exit
        if side is Side.LONG:
            raw_stop_bps = (1.0 - latest.low / latest.close) * 10_000
        else:
            raw_stop_bps = (latest.high / latest.close - 1.0) * 10_000
        raw_stop_bps += exit_config.structural_buffer_bps
        if exit_config.reject_stop_above_maximum and raw_stop_bps > exit_config.max_stop_bps:
            return None
        stop_bps = max(exit_config.min_stop_bps, raw_stop_bps)
        costs = self.fee_model.breakdown(
            ctx.symbol,
            entry_is_maker=self.config.costs.prefer_maker,
            hold_seconds=exit_config.expected_hold_seconds,
        )
        target_bps = max(
            stop_bps * exit_config.reward_risk,
            costs.total_bps * exit_config.minimum_target_cost_multiple,
        )
        probability = self.config.structural_prior.probability
        confidence = self.config.structural_prior.confidence
        raw_expectancy = probability * target_bps - (1.0 - probability) * stop_bps
        candidate = SignalCandidate(
            scanner_id=self.scanner_id,
            symbol=ctx.symbol,
            side=side,
            decision_ts=ctx.ts,
            entry_price=latest.close,
            stop_loss=_price_at_bps(latest.close, side, -stop_bps),
            take_profits=(_price_at_bps(latest.close, side, target_bps),),
            time_stop_seconds=exit_config.time_stop_seconds,
            expected_hold_seconds=exit_config.expected_hold_seconds,
            expected_move_bps=target_bps,
            raw_expectancy_bps=raw_expectancy,
            modeled_cost_bps=costs.total_bps,
            fee_adjusted_expectancy_bps=raw_expectancy - costs.total_bps,
            scalper_probability=probability,
            confidence=confidence,
            entry_is_maker=False,
            metadata=MappingProxyType(
                {
                    "signal_type": "range_compression_breakout",
                    "setup_id": setup_id,
                    "compression_flags": tuple(flags),
                    "compressed_bar_count": sum(flags),
                    "range_high": range_high,
                    "range_low": range_low,
                    "close_beyond_range_bps": close_beyond_bps,
                    "body_ratio": _body_ratio(latest),
                    "relative_volume": relative_volume,
                    "true_range_atr_multiple": expansion_multiple,
                    "cusum": change.to_dict(),
                    "structural_prior": {
                        "calibrated": False,
                        "promotion_eligible": False,
                    },
                    "l2_confirmation": {
                        "status": "unavailable",
                        "used_for_signal": False,
                        "used_for_execution": False,
                    },
                    "fee_breakdown": costs.to_dict(),
                }
            ),
        )
        self._last_setup_id = setup_id
        self._last_fire_ts = ctx.ts
        return candidate


def range_compression_fee_model(config: RangeCompressionConfig) -> DeltaFeeModel:
    costs = config.costs
    return DeltaFeeModel(
        deto_enabled=costs.deto_enabled,
        scalper_opted_in=costs.scalper_opted_in,
        maker_fee_bps_pre_tax=costs.maker_fee_bps_pre_tax,
        taker_fee_bps_pre_tax=costs.taker_fee_bps_pre_tax,
        gst_rate=costs.gst_rate,
        default_slippage_bps_per_leg=costs.slippage_bps_per_leg,
    )
