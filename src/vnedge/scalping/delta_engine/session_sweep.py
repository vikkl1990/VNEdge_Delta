"""Causal candle-only session liquidity sweep research scanner."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from itertools import pairwise
from pathlib import Path
from statistics import fmean
from types import MappingProxyType
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from vnedge.scalping.delta_engine.fee_model import DeltaFeeModel
from vnedge.scalping.delta_engine.types import Candle, Side, SignalCandidate


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SweepDataConfig(_FrozenModel):
    symbols: tuple[Literal["BTCUSD", "ETHUSD"], ...]
    timeframe: Literal["1m"]
    start: str
    end: str
    require_closed_candles: Literal[True]
    require_zero_missing_minutes: Literal[True]


class SweepSessionConfig(_FrozenModel):
    asian_first_close_utc: time
    asian_last_close_utc: time
    asian_expected_bars: int = Field(ge=1)
    london_first_close_utc: time
    london_last_close_utc: time
    new_york_first_close_utc: time
    new_york_last_close_utc: time
    minimum_asian_range_bps: float = Field(gt=0)


class SweepFilterConfig(_FrozenModel):
    minimum_rejection_wick_ratio: float = Field(gt=0, le=1)
    volume_sma_bars: int = Field(ge=5)
    minimum_volume_ratio: float = Field(gt=1)
    atr_window_bars: int = Field(ge=5)
    atr_percentile_history_bars: int = Field(ge=20)
    maximum_atr_percentile: float = Field(gt=0, lt=1)
    minimum_sweep_bps: float = Field(gt=0)
    maximum_sweep_bps: float = Field(gt=0)

    @model_validator(mode="after")
    def validate_sweep(self) -> SweepFilterConfig:
        if self.maximum_sweep_bps <= self.minimum_sweep_bps:
            raise ValueError("maximum sweep must exceed minimum sweep")
        return self


class SweepExitConfig(_FrozenModel):
    structural_buffer_bps: float = Field(ge=0)
    reward_risk: Literal[1.0]
    minimum_target_cost_multiple: float = Field(gt=1)
    expected_hold_seconds: int = Field(gt=0, le=2_700)
    time_stop_seconds: Literal[2700]


class SweepStructuralPrior(_FrozenModel):
    probability: float = Field(ge=0.5, le=1)
    confidence: float = Field(ge=0, le=1)
    calibrated: Literal[False]
    promotion_eligible: Literal[False]


class SweepCostConfig(_FrozenModel):
    prefer_maker: Literal[False]
    scalper_opted_in: Literal[False]
    deto_enabled: Literal[False]
    maker_fee_bps_pre_tax: float = Field(ge=0)
    taker_fee_bps_pre_tax: float = Field(ge=0)
    gst_rate: float = Field(ge=0)
    slippage_bps_per_leg: float = Field(ge=0)


class SweepValidationConfig(_FrozenModel):
    selection_fraction: float = Field(gt=0, lt=1)
    untouched_fraction: float = Field(gt=0, lt=1)
    split_embargo_minutes: int = Field(ge=45)
    minimum_selection_trades: int = Field(ge=1)
    minimum_selection_profit_factor: float = Field(gt=1)
    minimum_positive_markets: int = Field(ge=1)
    maximum_false_signal_rate: float = Field(gt=0, lt=1)
    maximum_single_market_trade_fraction: float = Field(gt=0.5, le=1)
    untouched_minimum_average_net_bps: float = Field(gt=0)
    untouched_minimum_profit_factor: float = Field(gt=1)
    open_untouched_only_after_selection_pass: Literal[True]

    @model_validator(mode="after")
    def validate_split(self) -> SweepValidationConfig:
        if abs(self.selection_fraction + self.untouched_fraction - 1) > 1e-9:
            raise ValueError("selection and untouched fractions must sum to one")
        return self


class SessionSweepConfig(_FrozenModel):
    contract_id: Literal["session_liquidity_sweep_v1"]
    status: Literal["preregistered"]
    research_only: Literal[True]
    can_trade: Literal[False]
    can_promote: Literal[False]
    data: SweepDataConfig
    sessions: SweepSessionConfig
    filters: SweepFilterConfig
    exit: SweepExitConfig
    structural_prior: SweepStructuralPrior
    costs: SweepCostConfig
    validation: SweepValidationConfig
    policy: dict[str, str]


def load_session_sweep_config(path: Path | str) -> SessionSweepConfig:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"invalid session sweep config: {path}")
    return SessionSweepConfig.model_validate(payload)


@dataclass(frozen=True)
class SessionSweepContext:
    symbol: str
    ts: datetime
    rows: tuple[Candle, ...]

    def __post_init__(self) -> None:
        if not self.rows or self.rows[-1].ts != self.ts:
            raise ValueError("session sweep context must end at decision time")
        if any(row.tf != "1m" or row.ts > self.ts for row in self.rows):
            raise ValueError("session sweep context requires closed 1m candles")


@dataclass(frozen=True)
class SessionSweepSetup:
    setup_id: str
    scanner_id: str
    symbol: str
    session_date: date
    session: str
    side: Side
    decision_ts: datetime
    decision_close: float
    asian_high: float
    asian_low: float
    sweep_extreme: float
    fixed_stop_price: float
    sweep_distance_bps: float
    wick_ratio: float
    volume_ratio: float
    atr_percentile: float
    hierarchy_reason: str

    def to_dict(self) -> dict[str, object]:
        row = self.__dict__.copy()
        row["session_date"] = self.session_date.isoformat()
        row["side"] = self.side.value
        row["decision_ts"] = self.decision_ts.isoformat()
        return row


@dataclass(frozen=True)
class EntryGeometry:
    candidate: SignalCandidate | None
    status: str
    reason: str | None
    entry_price: float
    stop_distance_bps: float
    target_distance_bps: float
    cost_multiple: float

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "reason": self.reason,
            "entry_price": self.entry_price,
            "stop_distance_bps": self.stop_distance_bps,
            "target_distance_bps": self.target_distance_bps,
            "cost_multiple": self.cost_multiple,
        }


def _true_ranges(rows: tuple[Candle, ...]) -> list[float]:
    return [
        max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        )
        for previous, current in pairwise(rows)
    ]


def _atr_percentile(rows: tuple[Candle, ...], window: int, history: int) -> float | None:
    ranges = _true_ranges(rows)
    atr_values = [
        fmean(ranges[end - window : end])
        for end in range(window, len(ranges) + 1)
    ]
    if len(atr_values) < history + 1:
        return None
    current = atr_values[-1]
    prior = atr_values[-(history + 1) : -1]
    return (
        sum(value < current for value in prior)
        + 0.5 * sum(value == current for value in prior)
    ) / len(prior)


class SessionLiquiditySweepScanner:
    scanner_id = "session_liquidity_sweep_v1"

    def __init__(self, config: SessionSweepConfig) -> None:
        self.config = config
        self._date: date | None = None
        self._asian_rows: list[Candle] = []
        self._asian_range: tuple[float, float] | None = None
        self._consumed_sessions: set[str] = set()

    @property
    def minimum_history(self) -> int:
        filters = self.config.filters
        return max(
            filters.volume_sma_bars + 1,
            filters.atr_window_bars + filters.atr_percentile_history_bars + 1,
        )

    def reset(self) -> None:
        self._date = None
        self._asian_rows.clear()
        self._asian_range = None
        self._consumed_sessions.clear()

    def _roll_date(self, current: date) -> None:
        if current != self._date:
            self._date = current
            self._asian_rows.clear()
            self._asian_range = None
            self._consumed_sessions.clear()

    def _session_name(self, value: time) -> str | None:
        sessions = self.config.sessions
        if sessions.london_first_close_utc <= value <= sessions.london_last_close_utc:
            return "london"
        if (
            sessions.new_york_first_close_utc
            <= value
            <= sessions.new_york_last_close_utc
        ):
            return "new_york"
        return None

    def evaluate(
        self, ctx: SessionSweepContext, *, allow_signal: bool = True
    ) -> SessionSweepSetup | None:
        latest = ctx.rows[-1]
        current_date = latest.ts.date()
        current_time = latest.ts.time().replace(tzinfo=None)
        self._roll_date(current_date)
        sessions = self.config.sessions
        if sessions.asian_first_close_utc <= current_time <= sessions.asian_last_close_utc:
            self._asian_rows.append(latest)
            if (
                current_time == sessions.asian_last_close_utc
                and len(self._asian_rows) == sessions.asian_expected_bars
            ):
                high = max(row.high for row in self._asian_rows)
                low = min(row.low for row in self._asian_rows)
                midpoint = (high + low) / 2
                width_bps = (high - low) / midpoint * 10_000 if midpoint else 0.0
                if high > low and width_bps >= sessions.minimum_asian_range_bps:
                    self._asian_range = (high, low)
            return None
        session = self._session_name(current_time)
        if (
            not allow_signal
            or session is None
            or session in self._consumed_sessions
            or self._asian_range is None
            or len(ctx.rows) < self.minimum_history
        ):
            return None
        asian_high, asian_low = self._asian_range
        swept_up = latest.high > asian_high
        swept_down = latest.low < asian_low
        inside = asian_low < latest.close < asian_high
        if swept_up == swept_down or not inside:
            return None
        candle_range = latest.high - latest.low
        if candle_range <= 0:
            return None
        if swept_up:
            side = Side.SHORT
            sweep_extreme = latest.high
            distance = (latest.high / asian_high - 1) * 10_000
            wick = latest.high - max(latest.open, latest.close)
            stop = latest.high * (1 + self.config.exit.structural_buffer_bps / 10_000)
        else:
            side = Side.LONG
            sweep_extreme = latest.low
            distance = (asian_low / latest.low - 1) * 10_000
            wick = min(latest.open, latest.close) - latest.low
            stop = latest.low * (1 - self.config.exit.structural_buffer_bps / 10_000)
        filters = self.config.filters
        wick_ratio = wick / candle_range
        prior_volumes = [row.volume for row in ctx.rows[-(filters.volume_sma_bars + 1) : -1]]
        volume_mean = fmean(prior_volumes)
        volume_ratio = latest.volume / volume_mean if volume_mean > 0 else 0.0
        atr_percentile = _atr_percentile(
            ctx.rows,
            filters.atr_window_bars,
            filters.atr_percentile_history_bars,
        )
        if (
            distance < filters.minimum_sweep_bps
            or distance > filters.maximum_sweep_bps
            or wick_ratio < filters.minimum_rejection_wick_ratio
            or volume_ratio < filters.minimum_volume_ratio
            or atr_percentile is None
            or atr_percentile > filters.maximum_atr_percentile
        ):
            return None
        setup_id = (
            f"{self.scanner_id}:{ctx.symbol}:{current_date.isoformat()}:"
            f"{session}:{side.value}"
        )
        self._consumed_sessions.add(session)
        return SessionSweepSetup(
            setup_id=setup_id,
            scanner_id=self.scanner_id,
            symbol=ctx.symbol,
            session_date=current_date,
            session=session,
            side=side,
            decision_ts=ctx.ts,
            decision_close=latest.close,
            asian_high=asian_high,
            asian_low=asian_low,
            sweep_extreme=sweep_extreme,
            fixed_stop_price=stop,
            sweep_distance_bps=distance,
            wick_ratio=wick_ratio,
            volume_ratio=volume_ratio,
            atr_percentile=atr_percentile,
            hierarchy_reason=(
                f"{session}:asian_range_sweep:{side.value}:close_back_inside:"
                "wick_volume_atr_confirmed"
            ),
        )


def session_sweep_fee_model(config: SessionSweepConfig) -> DeltaFeeModel:
    costs = config.costs
    return DeltaFeeModel(
        deto_enabled=costs.deto_enabled,
        scalper_opted_in=costs.scalper_opted_in,
        maker_fee_bps_pre_tax=costs.maker_fee_bps_pre_tax,
        taker_fee_bps_pre_tax=costs.taker_fee_bps_pre_tax,
        gst_rate=costs.gst_rate,
        default_slippage_bps_per_leg=costs.slippage_bps_per_leg,
    )


def finalize_next_open(
    setup: SessionSweepSetup,
    entry_bar: Candle,
    config: SessionSweepConfig,
    fee_model: DeltaFeeModel,
) -> EntryGeometry:
    entry = entry_bar.open
    if setup.side is Side.LONG:
        stop_bps = (1 - setup.fixed_stop_price / entry) * 10_000
    else:
        stop_bps = (setup.fixed_stop_price / entry - 1) * 10_000
    costs = fee_model.breakdown(
        setup.symbol,
        entry_is_maker=False,
        hold_seconds=config.exit.expected_hold_seconds,
    )
    target_bps = stop_bps * config.exit.reward_risk
    required = costs.total_bps * config.exit.minimum_target_cost_multiple
    cost_multiple = target_bps / costs.total_bps if costs.total_bps else float("inf")
    reason = None
    if stop_bps <= 0:
        reason = "next_open_crossed_fixed_stop"
    elif target_bps < required:
        reason = "structural_target_below_cost_multiple"
    if reason is not None:
        return EntryGeometry(None, "rejected", reason, entry, stop_bps, target_bps, cost_multiple)
    direction = 1 if setup.side is Side.LONG else -1
    target = entry * (1 + direction * target_bps / 10_000)
    probability = config.structural_prior.probability
    raw_expectancy = probability * target_bps - (1 - probability) * stop_bps
    candidate = SignalCandidate(
        scanner_id=setup.scanner_id,
        symbol=setup.symbol,
        side=setup.side,
        decision_ts=setup.decision_ts,
        entry_price=entry,
        stop_loss=setup.fixed_stop_price,
        take_profits=(target,),
        time_stop_seconds=config.exit.time_stop_seconds,
        expected_hold_seconds=config.exit.expected_hold_seconds,
        expected_move_bps=target_bps,
        raw_expectancy_bps=raw_expectancy,
        modeled_cost_bps=costs.total_bps,
        fee_adjusted_expectancy_bps=raw_expectancy - costs.total_bps,
        scalper_probability=probability,
        confidence=config.structural_prior.confidence,
        entry_is_maker=False,
        metadata=MappingProxyType(
            {
                **setup.to_dict(),
                "structural_target_bps": target_bps,
                "structural_cost_multiple": cost_multiple,
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
    return EntryGeometry(candidate, "accepted", None, entry, stop_bps, target_bps, cost_multiple)
