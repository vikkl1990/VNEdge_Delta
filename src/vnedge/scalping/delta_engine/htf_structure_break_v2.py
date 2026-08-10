"""Frozen EMA-aligned, close-only HTF structure-break research hypothesis."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from vnedge.scalping.delta_engine.fee_model import DeltaFeeModel
from vnedge.scalping.delta_engine.mechanical_structure import (
    ConfirmedSwing,
    MechanicalStructureConfig,
    confirmed_swings,
)
from vnedge.scalping.delta_engine.types import Candle, Side, SignalCandidate


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HTFV2DataConfig(_FrozenModel):
    symbols: tuple[Literal["BTCUSD", "ETHUSD"], ...]
    source_timeframe: Literal["1m"]
    decision_timeframe: Literal["1h"]
    bias_timeframe: Literal["4h"]
    selection_start: str
    selection_end_exclusive: str
    embargo_hours: int = Field(ge=48)
    untouched_start: str
    untouched_end_exclusive: str
    require_closed_candles: Literal[True]


class HTFV2BiasConfig(_FrozenModel):
    ema_fast_4h: int = Field(ge=2)
    ema_slow_4h: int = Field(ge=3)

    @model_validator(mode="after")
    def validate_windows(self) -> HTFV2BiasConfig:
        if self.ema_slow_4h <= self.ema_fast_4h:
            raise ValueError("slow 4h EMA must exceed fast EMA")
        return self


class HTFV2StructureConfig(_FrozenModel):
    swing_left_bars: Literal[5]
    swing_right_bars: Literal[5]
    minimum_swing_bps: float = Field(ge=0)
    event_type: Literal["bos"]
    require_event_direction_matches_4h: Literal[True]
    target_level_sources: tuple[Literal["1h", "4h"], ...]
    target_rule: Literal[
        "nearest_confirmed_same_kind_swing_beyond_decision_close"
    ]
    stop_rule: Literal["broken_swing_plus_0_10_atr"]
    atr_window_1h: int = Field(ge=2)
    atr_stop_buffer_multiple: float = Field(gt=0)


class HTFV2ExitConfig(_FrozenModel):
    minimum_target_cost_multiple: float = Field(ge=5)
    expected_hold_seconds: int = Field(gt=0)
    time_stop_seconds: Literal[43200]

    @model_validator(mode="after")
    def validate_hold(self) -> HTFV2ExitConfig:
        if self.expected_hold_seconds > self.time_stop_seconds:
            raise ValueError("expected hold cannot exceed time stop")
        return self


class HTFV2CostConfig(_FrozenModel):
    prefer_maker: Literal[False]
    scalper_opted_in: Literal[False]
    deto_enabled: Literal[False]
    maker_fee_bps_pre_tax: Literal[2.0]
    taker_fee_bps_pre_tax: Literal[5.0]
    gst_rate: Literal[0.18]
    slippage_bps_per_leg: Literal[1.5]
    baseline_round_trip_bps: Literal[14.8]
    funding_required_for_all_in_result: Literal[True]
    funding_settlement_interval_seconds: Literal[28800]


class HTFV2LatencyConfig(_FrozenModel):
    max_feed_lag_ms: int = Field(gt=0)
    gap_fail_closed: Literal[True]


class HTFV2ValidationConfig(_FrozenModel):
    minimum_selection_trades: int = Field(ge=1)
    minimum_selection_trades_per_half: int = Field(ge=1)
    minimum_selection_trades_per_market: int = Field(ge=1)
    minimum_selection_average_net_bps: float = Field(gt=0)
    minimum_selection_profit_factor: float = Field(gt=1)
    minimum_trades_per_day: float = Field(gt=0)
    maximum_trades_per_day_per_market: float = Field(gt=0)
    require_gross_expectancy_above_cost: Literal[True]
    require_positive_selection_halves: Literal[True]
    require_positive_selection_markets: Literal[True]
    open_untouched_only_after_selection_pass: Literal[True]


class HTFStructureBreakV2Config(_FrozenModel):
    contract_id: Literal["htf_structure_break_v2"]
    status: Literal["preregistered"]
    research_only: Literal[True]
    can_trade: Literal[False]
    can_promote: Literal[False]
    data: HTFV2DataConfig
    bias: HTFV2BiasConfig
    structure: HTFV2StructureConfig
    exit: HTFV2ExitConfig
    costs: HTFV2CostConfig
    latency: HTFV2LatencyConfig
    validation: HTFV2ValidationConfig
    policy: dict[str, str]


def load_htf_structure_break_v2_config(
    path: str | Path,
) -> HTFStructureBreakV2Config:
    source = Path(path)
    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError("HTF structure v2 contract must be a mapping")
    config = HTFStructureBreakV2Config.model_validate(raw)
    selection_end = datetime.fromisoformat(config.data.selection_end_exclusive)
    untouched_start = datetime.fromisoformat(config.data.untouched_start)
    embargo = (untouched_start - selection_end).total_seconds()
    if embargo < config.data.embargo_hours * 3600:
        raise ValueError("untouched window does not respect the frozen embargo")
    return config


def htf_structure_v2_fee_model(config: HTFStructureBreakV2Config) -> DeltaFeeModel:
    costs = config.costs
    model = DeltaFeeModel(
        deto_enabled=costs.deto_enabled,
        scalper_opted_in=costs.scalper_opted_in,
        maker_fee_bps_pre_tax=costs.maker_fee_bps_pre_tax,
        taker_fee_bps_pre_tax=costs.taker_fee_bps_pre_tax,
        gst_rate=costs.gst_rate,
        default_slippage_bps_per_leg=costs.slippage_bps_per_leg,
    )
    actual = model.breakdown(
        "BTCUSD", entry_is_maker=False, exit_is_maker=False, hold_seconds=0
    ).total_bps
    if abs(actual - costs.baseline_round_trip_bps) > 1e-9:
        raise ValueError("declared baseline round-trip cost does not match fee inputs")
    return model


@dataclass(frozen=True)
class HTFStructureV2Context:
    symbol: str
    ts: datetime
    one_hour: tuple[Candle, ...]
    four_hour: tuple[Candle, ...]

    def __post_init__(self) -> None:
        if not self.one_hour or self.one_hour[-1].ts != self.ts:
            raise ValueError("context must end on the decision 1h close")
        if any(row.tf != "1h" or row.ts > self.ts for row in self.one_hour):
            raise ValueError("context contains invalid or future 1h bars")
        if any(row.tf != "4h" or row.ts > self.ts for row in self.four_hour):
            raise ValueError("context contains invalid or future 4h bars")


def _ema(values: tuple[float, ...], span: int) -> float:
    if len(values) < span:
        raise ValueError("insufficient EMA history")
    alpha = 2.0 / (span + 1.0)
    value = sum(values[:span]) / span
    for item in values[span:]:
        value = alpha * item + (1.0 - alpha) * value
    return value


def _atr(rows: tuple[Candle, ...], window: int) -> float:
    if len(rows) < window + 1:
        raise ValueError("insufficient ATR history")
    values: list[float] = []
    for previous, current in zip(rows[-window - 1 : -1], rows[-window:], strict=True):
        values.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    return sum(values) / len(values)


def _nearest_target(
    swings: tuple[ConfirmedSwing, ...], side: Side, close: float
) -> ConfirmedSwing | None:
    if side is Side.LONG:
        candidates = [row for row in swings if row.kind == "high" and row.price > close]
    else:
        candidates = [row for row in swings if row.kind == "low" and row.price < close]
    return min(candidates, key=lambda row: abs(row.price - close)) if candidates else None


class HTFStructureBreakV2Scanner:
    scanner_id = "htf_structure_break_v2"

    def __init__(
        self, config: HTFStructureBreakV2Config, fee_model: DeltaFeeModel
    ) -> None:
        self.config = config
        self.fee_model = fee_model
        self._broken: dict[str, set[str]] = {}
        self.rejections: dict[str, int] = {}
        self._swing_config = MechanicalStructureConfig(
            swing_left=config.structure.swing_left_bars,
            swing_right=config.structure.swing_right_bars,
            minimum_swing_bps=config.structure.minimum_swing_bps,
            trend_lookback=5,
        )

    def _count(self, key: str) -> None:
        self.rejections[key] = self.rejections.get(key, 0) + 1

    def reset(self, symbol: str) -> None:
        self._broken.pop(symbol.upper(), None)

    def evaluate(self, context: HTFStructureV2Context) -> SignalCandidate | None:
        self._count("evaluations")
        config = self.config
        if len(context.four_hour) < config.bias.ema_slow_4h:
            self._count("ema_warmup")
            return None
        closes = tuple(row.close for row in context.four_hour)
        fast = _ema(closes, config.bias.ema_fast_4h)
        slow = _ema(closes, config.bias.ema_slow_4h)
        bias = 1 if fast > slow else -1 if fast < slow else 0
        if bias == 0:
            self._count("flat_4h_bias")
            return None

        one_swings = confirmed_swings(context.one_hour, self._swing_config)
        kind = "high" if bias > 0 else "low"
        broken = self._broken.setdefault(context.symbol.upper(), set())
        swing = next(
            (row for row in reversed(one_swings) if row.kind == kind and row.swing_id not in broken),
            None,
        )
        if swing is None:
            self._count("no_unbroken_swing")
            return None
        latest = context.one_hour[-1]
        previous = context.one_hour[-2] if len(context.one_hour) >= 2 else None
        crossed = (
            previous is not None
            and (
                previous.close <= swing.price < latest.close
                if bias > 0
                else previous.close >= swing.price > latest.close
            )
        )
        if not crossed:
            self._count("no_bias_aligned_bos")
            return None
        broken.add(swing.swing_id)

        try:
            atr = _atr(context.one_hour, config.structure.atr_window_1h)
        except ValueError:
            self._count("atr_warmup")
            return None
        side = Side.LONG if bias > 0 else Side.SHORT
        four_swings = confirmed_swings(context.four_hour, self._swing_config)
        target = _nearest_target(one_swings + four_swings, side, latest.close)
        if target is None:
            self._count("no_causal_target")
            return None
        buffer = config.structure.atr_stop_buffer_multiple * atr
        stop = swing.price - buffer if side is Side.LONG else swing.price + buffer
        valid_geometry = (
            stop < latest.close < target.price
            if side is Side.LONG
            else target.price < latest.close < stop
        )
        if not valid_geometry:
            self._count("invalid_decision_geometry")
            return None
        target_bps = abs(target.price / latest.close - 1.0) * 10_000.0
        costs = self.fee_model.breakdown(
            context.symbol,
            entry_is_maker=False,
            exit_is_maker=False,
            hold_seconds=config.exit.expected_hold_seconds,
        )
        if target_bps < costs.total_bps * config.exit.minimum_target_cost_multiple:
            self._count("target_below_5x_cost")
            return None
        stop_bps = abs(stop / latest.close - 1.0) * 10_000.0
        self._count("emitted")
        return SignalCandidate(
            scanner_id=self.scanner_id,
            symbol=context.symbol.upper(),
            side=side,
            decision_ts=context.ts,
            entry_price=latest.close,
            stop_loss=stop,
            take_profits=(target.price,),
            time_stop_seconds=config.exit.time_stop_seconds,
            expected_hold_seconds=config.exit.expected_hold_seconds,
            expected_move_bps=target_bps,
            raw_expectancy_bps=0.0,
            modeled_cost_bps=costs.total_bps,
            fee_adjusted_expectancy_bps=-costs.total_bps,
            scalper_probability=0.5,
            confidence=0.5,
            entry_is_maker=False,
            metadata=MappingProxyType(
                {
                    "signal_type": self.scanner_id,
                    "bias_4h": bias,
                    "ema_fast_4h": fast,
                    "ema_slow_4h": slow,
                    "event_1h": "bos",
                    "broken_swing_id": swing.swing_id,
                    "broken_swing_price": swing.price,
                    "atr_1h": atr,
                    "atr_stop_buffer": buffer,
                    "stop_bps_at_decision": stop_bps,
                    "target_swing_id": target.swing_id,
                    "target_source_timeframe": target.timeframe,
                    "target_cost_multiple_at_decision": target_bps / costs.total_bps,
                    "funding_included_at_decision": False,
                }
            ),
        )


def geometry_valid_at_entry(
    candidate: SignalCandidate,
    entry_price: float,
    fee_model: DeltaFeeModel,
    minimum_target_cost_multiple: float,
) -> tuple[bool, str, float, float]:
    """Recheck absolute exits against the actual next-interval open."""

    target = candidate.take_profits[0]
    if candidate.side is Side.LONG:
        valid_order = candidate.stop_loss < entry_price < target
    else:
        valid_order = target < entry_price < candidate.stop_loss
    if not valid_order:
        return False, "invalid_absolute_geometry", 0.0, 0.0
    target_bps = abs(target / entry_price - 1.0) * 10_000.0
    stop_bps = abs(candidate.stop_loss / entry_price - 1.0) * 10_000.0
    costs = fee_model.breakdown(
        candidate.symbol,
        entry_is_maker=False,
        exit_is_maker=False,
        hold_seconds=candidate.expected_hold_seconds,
    )
    if target_bps < minimum_target_cost_multiple * costs.total_bps:
        return False, "target_below_5x_cost_at_entry", stop_bps, target_bps
    return True, "accepted", stop_bps, target_bps
