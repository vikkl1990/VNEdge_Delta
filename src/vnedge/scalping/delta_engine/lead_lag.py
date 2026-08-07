"""Causal BTC-to-ETH closed-candle lead-lag research scanner."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from statistics import fmean, pstdev
from types import MappingProxyType
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from vnedge.scalping.delta_engine.fee_model import DeltaFeeModel
from vnedge.scalping.delta_engine.regime import _true_ranges
from vnedge.scalping.delta_engine.types import Candle, Side, SignalCandidate


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LeadLagDataConfig(_FrozenModel):
    leader_symbol: Literal["BTCUSD"] = "BTCUSD"
    follower_symbol: Literal["ETHUSD"] = "ETHUSD"
    timeframe: Literal["1m"] = "1m"
    start: str
    end: str
    require_exact_timestamp_match: bool = True
    require_closed_candles: bool = True
    require_zero_missing_minutes: bool = True


class LeadLagSignalConfig(_FrozenModel):
    impulse_lookback_bars: int = Field(default=5, ge=2)
    volume_window_bars: int = Field(default=20, ge=10)
    min_btc_impulse_bps: float = Field(default=20.0, gt=0)
    min_btc_last_bar_bps: float = Field(default=5.0, gt=0)
    min_btc_volume_z: float = 1.0
    max_eth_absolute_lag_bps: float = Field(default=12.0, gt=0)
    max_eth_follow_ratio: float = Field(default=0.40, ge=0, lt=1)
    min_lead_gap_bps: float = Field(default=12.0, gt=0)
    min_eth_trigger_body_ratio: float = Field(default=0.50, ge=0, le=1)
    min_eth_trigger_volume_z: float = 0.50
    cooldown_minutes: int = Field(default=240, ge=0)


class LeadLagExitConfig(_FrozenModel):
    atr_window_bars: int = Field(default=14, ge=5)
    stop_atr_fraction: float = Field(default=0.80, gt=0)
    min_stop_bps: float = Field(default=12.0, gt=0)
    max_stop_bps: float = Field(default=40.0, gt=0)
    reward_risk: float = Field(default=2.50, gt=1)
    minimum_target_cost_multiple: float = Field(default=3.50, gt=1)
    expected_hold_seconds: int = Field(default=900, gt=0, le=1_800)
    time_stop_seconds: int = Field(default=1_680, gt=0, le=1_800)

    @model_validator(mode="after")
    def validate_exit(self) -> LeadLagExitConfig:
        if self.max_stop_bps <= self.min_stop_bps:
            raise ValueError("max stop must exceed min stop")
        if self.expected_hold_seconds > self.time_stop_seconds:
            raise ValueError("expected hold cannot exceed time stop")
        return self


class StructuralPriorConfig(_FrozenModel):
    probability: float = Field(default=0.75, ge=0.5, le=1)
    confidence: float = Field(default=0.65, ge=0, le=1)
    calibrated: Literal[False] = False
    promotion_eligible: Literal[False] = False


class LeadLagCostConfig(_FrozenModel):
    prefer_maker: bool = False
    scalper_opted_in: bool = False
    deto_enabled: bool = False
    maker_fee_bps_pre_tax: float = Field(default=2.0, ge=0)
    taker_fee_bps_pre_tax: float = Field(default=5.0, ge=0)
    gst_rate: float = Field(default=0.18, ge=0)
    slippage_bps_per_leg: float = Field(default=1.5, ge=0)


class LeadLagValidationConfig(_FrozenModel):
    selection_fraction: float = Field(default=0.80, gt=0, lt=1)
    untouched_fraction: float = Field(default=0.20, gt=0, lt=1)
    split_embargo_minutes: int = Field(default=30, ge=28)
    minimum_selection_trades: int = Field(default=100, ge=1)
    minimum_selection_trades_per_half: int = Field(default=35, ge=1)
    minimum_selection_average_net_bps: float = Field(default=3.0, gt=0)
    minimum_selection_profit_factor: float = Field(default=1.20, gt=1)
    minimum_trades_per_day: float = Field(default=0.10, gt=0)
    maximum_trades_per_day: float = Field(default=4.0, gt=0)
    require_positive_selection_halves: bool = True
    open_untouched_only_after_selection_pass: Literal[True] = True

    @model_validator(mode="after")
    def validate_split(self) -> LeadLagValidationConfig:
        if abs(self.selection_fraction + self.untouched_fraction - 1.0) > 1e-9:
            raise ValueError("selection and untouched fractions must sum to one")
        if self.maximum_trades_per_day <= self.minimum_trades_per_day:
            raise ValueError("maximum frequency must exceed minimum frequency")
        return self


class BtcEthLeadLagConfig(_FrozenModel):
    contract_id: Literal["btc_eth_lead_lag_v1"] = "btc_eth_lead_lag_v1"
    status: Literal["preregistered"] = "preregistered"
    research_only: Literal[True] = True
    can_trade: Literal[False] = False
    can_promote: Literal[False] = False
    data: LeadLagDataConfig
    signal: LeadLagSignalConfig = LeadLagSignalConfig()
    exit: LeadLagExitConfig = LeadLagExitConfig()
    structural_prior: StructuralPriorConfig = StructuralPriorConfig()
    costs: LeadLagCostConfig = LeadLagCostConfig()
    validation: LeadLagValidationConfig = LeadLagValidationConfig()
    policy: dict[str, str]


def load_lead_lag_config(path: Path | str) -> BtcEthLeadLagConfig:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"invalid lead-lag config: {path}")
    return BtcEthLeadLagConfig.model_validate(payload)


@dataclass(frozen=True)
class LeadLagContext:
    ts: datetime
    btc: tuple[Candle, ...]
    eth: tuple[Candle, ...]

    def __post_init__(self) -> None:
        if not self.btc or not self.eth:
            raise ValueError("lead-lag context requires both markets")
        if self.btc[-1].ts != self.ts or self.eth[-1].ts != self.ts:
            raise ValueError("lead-lag context timestamps must match")
        if any(row.tf != "1m" or row.ts > self.ts for row in (*self.btc, *self.eth)):
            raise ValueError("lead-lag context must contain closed 1m candles only")


def _return_bps(rows: tuple[Candle, ...], bars: int) -> float:
    return (rows[-1].close / rows[-(bars + 1)].close - 1.0) * 10_000


def _volume_z(rows: tuple[Candle, ...], window: int) -> float:
    history = [row.volume for row in rows[-(window + 1) : -1]]
    if len(history) < window:
        return 0.0
    deviation = pstdev(history)
    return (rows[-1].volume - fmean(history)) / deviation if deviation else 0.0


def _body_ratio(row: Candle) -> float:
    span = max(row.high - row.low, row.close * 1e-9)
    return abs(row.close - row.open) / span


def _price_at_bps(price: float, side: Side, bps: float) -> float:
    direction = 1.0 if side is Side.LONG else -1.0
    return price * (1.0 + direction * bps / 10_000.0)


class BtcEthLeadLagScanner:
    scanner_id = "btc_eth_lead_lag_v1"

    def __init__(self, config: BtcEthLeadLagConfig, fee_model: DeltaFeeModel) -> None:
        self.config = config
        self.fee_model = fee_model
        self._last_setup_id: str | None = None
        self._last_fire_ts: datetime | None = None

    @property
    def minimum_history(self) -> int:
        return max(
            self.config.signal.impulse_lookback_bars + 1,
            self.config.signal.volume_window_bars + 1,
            self.config.exit.atr_window_bars + 1,
        )

    def reset(self) -> None:
        self._last_setup_id = None
        self._last_fire_ts = None

    def evaluate(self, ctx: LeadLagContext) -> SignalCandidate | None:
        if len(ctx.btc) < self.minimum_history or len(ctx.eth) < self.minimum_history:
            return None
        signal = self.config.signal
        btc_move = _return_bps(ctx.btc, signal.impulse_lookback_bars)
        eth_move = _return_bps(ctx.eth, signal.impulse_lookback_bars)
        if abs(btc_move) < signal.min_btc_impulse_bps:
            return None
        side = Side.LONG if btc_move > 0 else Side.SHORT
        direction = 1.0 if side is Side.LONG else -1.0
        btc_last = _return_bps(ctx.btc, 1)
        if direction * btc_last < signal.min_btc_last_bar_bps:
            return None
        btc_volume_z = _volume_z(ctx.btc, signal.volume_window_bars)
        if btc_volume_z < signal.min_btc_volume_z:
            return None
        if abs(eth_move) > signal.max_eth_absolute_lag_bps:
            return None
        directional_eth = direction * eth_move
        if directional_eth > abs(btc_move) * signal.max_eth_follow_ratio:
            return None
        lead_gap = direction * (btc_move - eth_move)
        if lead_gap < signal.min_lead_gap_bps:
            return None
        latest_eth, previous_eth = ctx.eth[-1], ctx.eth[-2]
        trigger = (
            latest_eth.close > latest_eth.open and latest_eth.close > previous_eth.high
            if side is Side.LONG
            else latest_eth.close < latest_eth.open and latest_eth.close < previous_eth.low
        )
        eth_volume_z = _volume_z(ctx.eth, signal.volume_window_bars)
        body_ratio = _body_ratio(latest_eth)
        if (
            not trigger
            or body_ratio < signal.min_eth_trigger_body_ratio
            or eth_volume_z < signal.min_eth_trigger_volume_z
        ):
            return None
        setup_id = f"BTCUSD:{ctx.ts.isoformat()}:{side.value}"
        if setup_id == self._last_setup_id:
            return None
        if self._last_fire_ts is not None and ctx.ts - self._last_fire_ts < timedelta(
            minutes=signal.cooldown_minutes
        ):
            return None

        exit_config = self.config.exit
        entry = latest_eth.close
        atr = fmean(_true_ranges(ctx.eth)[-exit_config.atr_window_bars :])
        stop_bps = min(
            exit_config.max_stop_bps,
            max(
                exit_config.min_stop_bps,
                atr / entry * 10_000 * exit_config.stop_atr_fraction,
            ),
        )
        costs = self.fee_model.breakdown(
            "ETHUSD",
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
            symbol="ETHUSD",
            side=side,
            decision_ts=ctx.ts,
            entry_price=entry,
            stop_loss=_price_at_bps(entry, side, -stop_bps),
            take_profits=(_price_at_bps(entry, side, target_bps),),
            time_stop_seconds=exit_config.time_stop_seconds,
            expected_hold_seconds=exit_config.expected_hold_seconds,
            expected_move_bps=target_bps,
            raw_expectancy_bps=raw_expectancy,
            modeled_cost_bps=costs.total_bps,
            fee_adjusted_expectancy_bps=raw_expectancy - costs.total_bps,
            scalper_probability=probability,
            confidence=confidence,
            entry_is_maker=self.config.costs.prefer_maker,
            metadata=MappingProxyType(
                {
                    "signal_type": "btc_eth_lead_lag_continuation",
                    "setup_id": setup_id,
                    "leader_symbol": "BTCUSD",
                    "follower_symbol": "ETHUSD",
                    "btc_impulse_bps": btc_move,
                    "btc_last_bar_bps": btc_last,
                    "eth_lag_return_bps": eth_move,
                    "lead_gap_bps": lead_gap,
                    "btc_volume_z": btc_volume_z,
                    "eth_trigger_volume_z": eth_volume_z,
                    "eth_trigger_body_ratio": body_ratio,
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


def lead_lag_fee_model(config: BtcEthLeadLagConfig) -> DeltaFeeModel:
    costs = config.costs
    return DeltaFeeModel(
        deto_enabled=costs.deto_enabled,
        scalper_opted_in=costs.scalper_opted_in,
        maker_fee_bps_pre_tax=costs.maker_fee_bps_pre_tax,
        taker_fee_bps_pre_tax=costs.taker_fee_bps_pre_tax,
        gst_rate=costs.gst_rate,
        default_slippage_bps_per_leg=costs.slippage_bps_per_leg,
    )
