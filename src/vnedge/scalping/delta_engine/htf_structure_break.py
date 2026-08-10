"""Mechanical, preregistered higher-timeframe structure-break hypothesis."""

from __future__ import annotations

from dataclasses import dataclass
from collections import Counter
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
    MechanicalStructureTracker,
    StructureUpdate,
)
from vnedge.scalping.delta_engine.types import Candle, Side, SignalCandidate


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HTFDataConfig(_FrozenModel):
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


class HTFStructureConfig(_FrozenModel):
    swing_left_bars: int = Field(ge=1)
    swing_right_bars: int = Field(ge=1)
    minimum_swing_bps_1h: float = Field(gt=0)
    minimum_swing_bps_4h: float = Field(gt=0)
    trend_lookback_swings: int = Field(ge=4)
    allowed_events: tuple[Literal["bos", "choch"], ...]
    require_event_direction_matches_4h: Literal[True]
    target_level_sources: tuple[Literal["1h", "4h"], ...]
    target_rule: Literal[
        "nearest_confirmed_swing_in_trade_direction_beyond_decision_close"
    ]
    stop_rule: Literal["latest_confirmed_opposite_1h_swing"]


class HTFExitConfig(_FrozenModel):
    structural_stop_buffer_bps: float = Field(ge=0)
    maximum_stop_bps: float = Field(gt=0)
    minimum_reward_risk: float = Field(gt=1)
    minimum_target_cost_multiple: float = Field(ge=5)
    expected_hold_seconds: int = Field(gt=0)
    time_stop_seconds: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_hold(self) -> HTFExitConfig:
        if self.expected_hold_seconds > self.time_stop_seconds:
            raise ValueError("expected hold cannot exceed time stop")
        return self


class HTFCostConfig(_FrozenModel):
    prefer_maker: Literal[False]
    scalper_opted_in: Literal[False]
    deto_enabled: Literal[False]
    maker_fee_bps_pre_tax: float = Field(ge=0)
    taker_fee_bps_pre_tax: float = Field(ge=0)
    gst_rate: float = Field(ge=0)
    slippage_bps_per_leg: float = Field(ge=0)


class HTFValidationConfig(_FrozenModel):
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


class HTFStructureBreakConfig(_FrozenModel):
    contract_id: Literal["htf_structure_break_v1"]
    status: Literal["preregistered"]
    research_only: Literal[True]
    can_trade: Literal[False]
    can_promote: Literal[False]
    data: HTFDataConfig
    structure: HTFStructureConfig
    exit: HTFExitConfig
    costs: HTFCostConfig
    validation: HTFValidationConfig
    policy: dict[str, str]


def load_htf_structure_break_config(path: str | Path) -> HTFStructureBreakConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError("HTF structure contract must be a mapping")
    config = HTFStructureBreakConfig.model_validate(raw)
    selection_end = datetime.fromisoformat(config.data.selection_end_exclusive)
    untouched_start = datetime.fromisoformat(config.data.untouched_start)
    if (untouched_start - selection_end).total_seconds() < config.data.embargo_hours * 3600:
        raise ValueError("untouched window does not respect the frozen embargo")
    return config


def htf_structure_fee_model(config: HTFStructureBreakConfig) -> DeltaFeeModel:
    costs = config.costs
    return DeltaFeeModel(
        deto_enabled=costs.deto_enabled,
        scalper_opted_in=costs.scalper_opted_in,
        maker_fee_bps_pre_tax=costs.maker_fee_bps_pre_tax,
        taker_fee_bps_pre_tax=costs.taker_fee_bps_pre_tax,
        gst_rate=costs.gst_rate,
        default_slippage_bps_per_leg=costs.slippage_bps_per_leg,
    )


@dataclass(frozen=True)
class HTFStructureContext:
    symbol: str
    ts: datetime
    one_hour: tuple[Candle, ...]
    four_hour: tuple[Candle, ...]

    def __post_init__(self) -> None:
        if not self.one_hour or self.one_hour[-1].ts != self.ts:
            raise ValueError("HTF context must end on the decision 1h close")
        if any(row.tf != "1h" or row.ts > self.ts for row in self.one_hour):
            raise ValueError("HTF context requires causal closed 1h bars")
        if any(row.tf != "4h" or row.ts > self.ts for row in self.four_hour):
            raise ValueError("HTF context requires causal closed 4h bars")


def _latest_level(swings: tuple[ConfirmedSwing, ...], kind: str, price: float) -> ConfirmedSwing | None:
    candidates = [
        swing
        for swing in swings
        if swing.kind == kind
        and ((kind == "low" and swing.price < price) or (kind == "high" and swing.price > price))
    ]
    return candidates[-1] if candidates else None


class HTFStructureBreakScanner:
    scanner_id = "htf_structure_break_v1"

    def __init__(self, config: HTFStructureBreakConfig, fee_model: DeltaFeeModel) -> None:
        self.config = config
        self.fee_model = fee_model
        common = dict(
            swing_left=config.structure.swing_left_bars,
            swing_right=config.structure.swing_right_bars,
            trend_lookback=config.structure.trend_lookback_swings,
        )
        self.one_hour_tracker = MechanicalStructureTracker(
            MechanicalStructureConfig(
                minimum_swing_bps=config.structure.minimum_swing_bps_1h, **common
            )
        )
        self.four_hour_tracker = MechanicalStructureTracker(
            MechanicalStructureConfig(
                minimum_swing_bps=config.structure.minimum_swing_bps_4h, **common
            )
        )
        self._four_hour: dict[str, StructureUpdate] = {}
        self._last_event_id: dict[str, str] = {}
        self.rejections: Counter[str] = Counter()

    def reset(self, symbol: str) -> None:
        native = symbol.upper()
        self.one_hour_tracker.reset(native)
        self.four_hour_tracker.reset(native)
        self._four_hour.pop(native, None)
        self._last_event_id.pop(native, None)

    def update_four_hour(self, symbol: str, rows: tuple[Candle, ...]) -> StructureUpdate:
        update = self.four_hour_tracker.update(symbol, "4h", rows)
        self._four_hour[symbol.upper()] = update
        return update

    def evaluate(self, context: HTFStructureContext) -> SignalCandidate | None:
        self.rejections["evaluations"] += 1
        native = context.symbol.upper()
        bias = self._four_hour.get(native)
        if bias is None or bias.trend == 0:
            self.rejections["no_4h_bias"] += 1
            return None
        one_hour = self.one_hour_tracker.update(native, "1h", context.one_hour)
        event = one_hour.event
        if event is None:
            self.rejections["no_1h_event"] += 1
            return None
        self.rejections[f"event_{event.event_type}"] += 1
        if event.event_type not in self.config.structure.allowed_events:
            self.rejections["event_type_blocked"] += 1
            return None
        if event.direction != bias.trend:
            self.rejections["direction_conflict"] += 1
            return None
        if self._last_event_id.get(native) == event.event_id:
            self.rejections["duplicate_event"] += 1
            return None
        self._last_event_id[native] = event.event_id
        close = context.one_hour[-1].close
        side = Side.LONG if event.direction > 0 else Side.SHORT
        all_swings = one_hour.swings + bias.swings
        if side is Side.LONG:
            stop_level = _latest_level(one_hour.swings, "low", close)
            targets = [s for s in all_swings if s.kind == "high" and s.price > close]
        else:
            stop_level = _latest_level(one_hour.swings, "high", close)
            targets = [s for s in all_swings if s.kind == "low" and s.price < close]
        if stop_level is None or not targets:
            self.rejections["no_stop_level" if stop_level is None else "no_target_level"] += 1
            return None
        target_level = min(targets, key=lambda row: abs(row.price - close))
        if side is Side.LONG:
            stop_bps = (1.0 - stop_level.price / close) * 10_000
            target_bps = (target_level.price / close - 1.0) * 10_000
        else:
            stop_bps = (stop_level.price / close - 1.0) * 10_000
            target_bps = (1.0 - target_level.price / close) * 10_000
        stop_bps += self.config.exit.structural_stop_buffer_bps
        costs = self.fee_model.breakdown(
            native,
            entry_is_maker=False,
            hold_seconds=self.config.exit.expected_hold_seconds,
        )
        if stop_bps <= 0:
            self.rejections["invalid_stop"] += 1
            return None
        if stop_bps > self.config.exit.maximum_stop_bps:
            self.rejections["stop_too_wide"] += 1
            return None
        if target_bps < costs.total_bps * self.config.exit.minimum_target_cost_multiple:
            self.rejections["target_below_5x_cost"] += 1
            return None
        if target_bps / stop_bps < self.config.exit.minimum_reward_risk:
            self.rejections["reward_risk_below_minimum"] += 1
            return None
        stop_price = close * (1.0 - stop_bps / 10_000) if side is Side.LONG else close * (1.0 + stop_bps / 10_000)
        target_price = close * (1.0 + target_bps / 10_000) if side is Side.LONG else close * (1.0 - target_bps / 10_000)
        self.rejections["emitted"] += 1
        return SignalCandidate(
            scanner_id=self.scanner_id,
            symbol=native,
            side=side,
            decision_ts=context.ts,
            entry_price=close,
            stop_loss=stop_price,
            take_profits=(target_price,),
            time_stop_seconds=self.config.exit.time_stop_seconds,
            expected_hold_seconds=self.config.exit.expected_hold_seconds,
            expected_move_bps=target_bps,
            raw_expectancy_bps=0.0,
            modeled_cost_bps=costs.total_bps,
            fee_adjusted_expectancy_bps=-costs.total_bps,
            scalper_probability=0.5,
            confidence=0.5,
            entry_is_maker=False,
            metadata=MappingProxyType(
                {
                    "signal_type": "htf_structure_break",
                    "bias_4h": bias.trend,
                    "event_1h": event.event_type,
                    "event_id": event.event_id,
                    "broken_level": event.broken_swing.price,
                    "stop_swing_id": stop_level.swing_id,
                    "target_swing_id": target_level.swing_id,
                    "target_source_timeframe": target_level.timeframe,
                    "target_cost_multiple": target_bps / costs.total_bps,
                    "reward_risk": target_bps / stop_bps,
                    "regime": "htf_structure",
                }
            ),
        )
