"""Quantified fee-wall sniper scanner.

VNEDGE-owned, source-inspired scanner distilled from the Quantified Strategy
Lab "Chunk A" families: pullback/IBS/Williams-style reversions, range
compression breakouts, and crypto momentum continuation.  It does not copy a
third-party Pine implementation; it converts the repeated intent into a causal
strategy contract:

1. Context permission from EMA alignment + efficiency ratio.
2. One of two executable setups: pullback continuation or range expansion.
3. Structural stop + TP1/TP2/TP3 ladder.
4. A hard expected-net gate: no signal unless the planned move can pay taker
   fees, slippage, safety buffer, and a minimum profit floor.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
import math
from typing import Literal

import pandas as pd

from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent
from vnedge.strategy.indicators import atr, efficiency_ratio, ema, prior_high, prior_low, zscore


QUANTIFIED_FEE_WALL_SNIPER_ID = "quantified_fee_wall_sniper_v1"
QUANTIFIED_FEE_WALL_SNIPER_SIDES: tuple[str, ...] = ("long", "short")
QUANTIFIED_FEE_WALL_SNIPER_SETUPS: tuple[str, ...] = ("pullback", "breakout")
Side = Literal["long", "short"]


@dataclass(frozen=True)
class QuantifiedFeeWallSniperParams:
    """Frozen scanner parameters.

    Defaults are intentionally fee-first.  A signal must clear roughly 45 bps
    of modeled gross opportunity after quality discount: 5 bps taker entry,
    5 bps taker exit, 2 bps slippage, 8 bps safety buffer, and 25 bps net
    profit floor.
    """

    fast_ema: int = 21
    slow_ema: int = 55
    bias_ema: int = 144
    atr_window: int = 14
    er_window: int = 48
    range_lookback: int = 36
    compression_lookback: int = 48
    structure_window: int = 18
    volume_z_window: int = 48
    bbp_window: int = 13
    pullback_memory_bars: int = 4

    min_er: float = 0.08
    compression_ratio: float = 0.88
    min_body_atr: float = 0.40
    min_volume_z: float = 0.20
    min_bbp_slope_bps: float = 1.0
    pullback_ibs_long: float = 0.38
    pullback_ibs_short: float = 0.62
    pullback_williams_long: float = -62.0
    pullback_williams_short: float = -38.0

    stop_atr_mult: float = 1.10
    stop_structure_atr_buffer: float = 0.12
    min_stop_bps: float = 8.0
    max_stop_bps: float = 180.0
    tp1_r: float = 0.80
    tp2_r: float = 1.60
    tp3_r: float = 2.80
    min_room_to_liquidity_bps: float = 35.0

    taker_entry_bps: float = 5.0
    taker_exit_bps: float = 5.0
    slippage_bps: float = 2.0
    safety_buffer_bps: float = 8.0
    min_expected_net_edge_bps: float = 25.0
    taker_extra_buffer_bps: float = 8.0
    min_quality_score: float = 0.58
    allowed_sides: tuple[str, ...] = ()
    enabled_setups: tuple[str, ...] = QUANTIFIED_FEE_WALL_SNIPER_SETUPS

    @property
    def taker_round_trip_cost_bps(self) -> float:
        return (
            self.taker_entry_bps
            + self.taker_exit_bps
            + self.slippage_bps
            + self.safety_buffer_bps
        )

    @property
    def required_quality_adjusted_gross_bps(self) -> float:
        return self.taker_round_trip_cost_bps + self.min_expected_net_edge_bps

    @property
    def taker_fallback_threshold_bps(self) -> float:
        return self.min_expected_net_edge_bps + self.taker_extra_buffer_bps


def quantified_fee_wall_sniper_warmup_bars(
    params: QuantifiedFeeWallSniperParams,
) -> int:
    return (
        max(
            params.bias_ema,
            params.slow_ema,
            params.er_window,
            params.range_lookback + params.compression_lookback,
            params.structure_window,
            params.volume_z_window,
            params.bbp_window,
        )
        + params.pullback_memory_bars
        + 4
    )


class QuantifiedFeeWallSniper(BaseStrategy):
    """Fee-wall-first scanner for 5m/15m/1h style scalping research."""

    strategy_id = QUANTIFIED_FEE_WALL_SNIPER_ID

    def __init__(
        self,
        funding: pd.DataFrame | None = None,
        *,
        params: QuantifiedFeeWallSniperParams | dict | None = None,
        allowed_sides: tuple[str, ...] | list[str] | None = None,
        min_expected_net_edge_bps: float | None = None,
        **overrides: object,
    ) -> None:
        self.params = _coerce_params(
            params,
            allowed_sides=allowed_sides,
            min_expected_net_edge_bps=min_expected_net_edge_bps,
            overrides=overrides,
        )
        self.funding = funding
        self.warmup_bars = quantified_fee_wall_sniper_warmup_bars(self.params)

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        return add_quantified_fee_wall_sniper_columns(candles, self.params)

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        row = df.iloc[index]
        candidates: list[Side] = []
        for side in QUANTIFIED_FEE_WALL_SNIPER_SIDES:
            if self._ready(row, side):
                candidates.append(side)
        if not candidates:
            return None
        side = max(candidates, key=lambda s: float(row[f"expected_net_edge_bps_{s}"]))
        return SignalIntent(
            side,
            stop_price=float(row[f"stop_{side}"]),
            take_profit_price=float(row[f"tp3_{side}"]),
            take_profit_levels=_tp_ladder(row, side),
            reason=self._reason(row, side),
        )

    def synthesize_exit_plan(
        self, df: pd.DataFrame, index: int, side: str, entry_price: float
    ) -> SignalIntent | None:
        if side not in QUANTIFIED_FEE_WALL_SNIPER_SIDES:
            return None
        row = df.iloc[index]
        if any(
            _is_nan(row.get(col))
            for col in (f"stop_bps_{side}", "close", "atr_value")
        ):
            return None
        entry = float(entry_price)
        stop_bps = float(row[f"stop_bps_{side}"])
        if side == "long":
            stop = entry * (1.0 - stop_bps / 10_000.0)
            tp1 = entry * (1.0 + stop_bps * self.params.tp1_r / 10_000.0)
            tp2 = entry * (1.0 + stop_bps * self.params.tp2_r / 10_000.0)
            tp3 = entry * (1.0 + stop_bps * self.params.tp3_r / 10_000.0)
        else:
            stop = entry * (1.0 + stop_bps / 10_000.0)
            tp1 = entry * (1.0 - stop_bps * self.params.tp1_r / 10_000.0)
            tp2 = entry * (1.0 - stop_bps * self.params.tp2_r / 10_000.0)
            tp3 = entry * (1.0 - stop_bps * self.params.tp3_r / 10_000.0)
        return SignalIntent(
            side,  # type: ignore[arg-type]
            stop_price=stop,
            take_profit_price=tp3,
            take_profit_levels=(tp1, tp2, tp3),
            reason=(
                "quantified_fee_wall_sniper rebuilt exit plan; "
                "SL_first; TP1_partial; BE_after_TP1; trail_then_TP3"
            ),
        )

    def _ready(self, row: pd.Series, side: Side) -> bool:
        return bool(
            self._side_allowed(side)
            and _flag(row, f"setup_{side}")
            and not _is_nan(row.get(f"stop_{side}"))
            and not _is_nan(row.get(f"tp3_{side}"))
            and float(row[f"quality_score_{side}"]) >= self.params.min_quality_score
            and float(row[f"room_to_liquidity_bps_{side}"])
            >= self.params.min_room_to_liquidity_bps
            and float(row[f"expected_net_edge_bps_{side}"])
            >= self.params.min_expected_net_edge_bps
        )

    def _side_allowed(self, side: str) -> bool:
        return not self.params.allowed_sides or side in self.params.allowed_sides

    def _reason(self, row: pd.Series, side: Side) -> str:
        setup = _setup_name(row, side)
        edge = float(row[f"expected_net_edge_bps_{side}"])
        gross = float(row[f"expected_gross_bps_{side}"])
        quality = float(row[f"quality_score_{side}"])
        taker_allowed = edge >= self.params.taker_fallback_threshold_bps
        tp1, tp2, tp3 = _tp_ladder(row, side)
        return (
            f"quantified_fee_wall_sniper {side}; source=quantified_chunk_a; "
            f"setup={setup}; route=maker_first; "
            f"takerFallback={'allowed' if taker_allowed else 'blocked'}; "
            f"expectedGross={gross:.1f}; expectedNet={edge:.1f}; "
            f"feeWall={self.params.taker_round_trip_cost_bps:.1f}; "
            f"profitFloor={self.params.min_expected_net_edge_bps:.1f}; "
            f"quality={quality:.2f}; room={float(row[f'room_to_liquidity_bps_{side}']):.1f}; "
            f"bbp={float(row['bbp_bps']):+.1f}; volumeZ={float(row['volume_z']):+.2f}; "
            f"tp_ladder={tp1:.6g}/{tp2:.6g}/{tp3:.6g}; "
            "paperMargin=100; paperLeverage=25; paperNotional=2500; "
            "entry=close_confirmed_next_open; SL_first; TP1_partial; "
            "BE_after_TP1; trail_then_TP3"
        )


def add_quantified_fee_wall_sniper_columns(
    candles: pd.DataFrame,
    params: QuantifiedFeeWallSniperParams = QuantifiedFeeWallSniperParams(),
) -> pd.DataFrame:
    df = candles.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    close = df["close"]
    high = df["high"]
    low = df["low"]
    open_ = df["open"]
    volume = df["volume"]
    df["ema_fast"] = ema(close, params.fast_ema)
    df["ema_slow"] = ema(close, params.slow_ema)
    df["ema_bias"] = ema(close, params.bias_ema)
    df["atr_value"] = atr(df, params.atr_window)
    atr_safe = df["atr_value"].replace(0.0, float("nan"))
    close_safe = close.replace(0.0, float("nan"))
    df["atr_bps"] = df["atr_value"] / close_safe * 10_000.0
    df["efficiency_ratio"] = efficiency_ratio(close, params.er_window).fillna(0.0)

    df["range_high"] = prior_high(high, params.range_lookback)
    df["range_low"] = prior_low(low, params.range_lookback)
    df["structure_high"] = prior_high(high, params.structure_window)
    df["structure_low"] = prior_low(low, params.structure_window)
    df["range_width_bps"] = (df["range_high"] - df["range_low"]) / close_safe * 10_000.0
    df["range_atr_ratio"] = (df["range_high"] - df["range_low"]) / atr_safe
    df["range_atr_median"] = (
        df["range_atr_ratio"].rolling(params.compression_lookback).median().shift(1)
    )
    df["compression_ready"] = (
        df["range_atr_ratio"].shift(1)
        <= df["range_atr_median"] * params.compression_ratio
    ).fillna(False)

    bar_range = (high - low).replace(0.0, float("nan"))
    df["ibs"] = ((close - low) / bar_range).clip(lower=0.0, upper=1.0)
    structure_range = (df["structure_high"] - df["structure_low"]).replace(
        0.0, float("nan")
    )
    df["williams_r"] = (
        -100.0 * (df["structure_high"] - close) / structure_range
    ).clip(lower=-100.0, upper=0.0)

    df["body_atr"] = (close - open_).abs() / atr_safe
    df["volume_z"] = zscore(volume, params.volume_z_window).fillna(0.0)
    bbp_ema = ema(close, params.bbp_window)
    df["bull_power"] = high - bbp_ema
    df["bear_power"] = low - bbp_ema
    df["bbp_bps"] = (df["bull_power"] + df["bear_power"]) / close_safe * 10_000.0
    df["bbp_slope_bps"] = df["bbp_bps"] - df["bbp_bps"].shift(3)
    df["bbp_z"] = zscore(df["bbp_bps"], params.volume_z_window).fillna(0.0)

    lower_wick = pd.concat([open_, close], axis=1).min(axis=1) - low
    upper_wick = high - pd.concat([open_, close], axis=1).max(axis=1)
    body = (close - open_).abs()
    df["rejection_long"] = (lower_wick >= body * 0.60) & (close >= open_)
    df["rejection_short"] = (upper_wick >= body * 0.60) & (close <= open_)
    df["displacement_long"] = (close > open_) & (df["body_atr"] >= params.min_body_atr)
    df["displacement_short"] = (close < open_) & (df["body_atr"] >= params.min_body_atr)
    df["volume_impulse"] = df["volume_z"] >= params.min_volume_z

    df["bias_long"] = (
        (close >= df["ema_slow"])
        & (df["ema_fast"] >= df["ema_slow"])
        & (close >= df["ema_bias"])
        & (df["efficiency_ratio"] >= params.min_er)
    ).fillna(False)
    df["bias_short"] = (
        (close <= df["ema_slow"])
        & (df["ema_fast"] <= df["ema_slow"])
        & (close <= df["ema_bias"])
        & (df["efficiency_ratio"] >= params.min_er)
    ).fillna(False)

    df["breakout_long"] = (
        df["bias_long"]
        & df["compression_ready"]
        & (close > df["range_high"])
        & df["displacement_long"]
        & df["volume_impulse"]
    )
    df["breakout_short"] = (
        df["bias_short"]
        & df["compression_ready"]
        & (close < df["range_low"])
        & df["displacement_short"]
        & df["volume_impulse"]
    )
    recent_pullback_long = (
        (
            (df["ibs"] <= params.pullback_ibs_long)
            | (df["williams_r"] <= params.pullback_williams_long)
        )
        .rolling(params.pullback_memory_bars)
        .max()
        .fillna(0.0)
        .astype(bool)
    )
    recent_pullback_short = (
        (
            (df["ibs"] >= params.pullback_ibs_short)
            | (df["williams_r"] >= params.pullback_williams_short)
        )
        .rolling(params.pullback_memory_bars)
        .max()
        .fillna(0.0)
        .astype(bool)
    )
    df["pullback_long"] = (
        df["bias_long"]
        & recent_pullback_long
        & df["displacement_long"]
        & (df["bbp_slope_bps"] >= params.min_bbp_slope_bps)
    )
    df["pullback_short"] = (
        df["bias_short"]
        & recent_pullback_short
        & df["displacement_short"]
        & (df["bbp_slope_bps"] <= -params.min_bbp_slope_bps)
    )

    for side in QUANTIFIED_FEE_WALL_SNIPER_SIDES:
        _add_side_geometry(df, side, params)
        df[f"setup_{side}"] = (
            (
                df[f"breakout_{side}"]
                if "breakout" in params.enabled_setups
                else False
            )
            | (
                df[f"pullback_{side}"]
                if "pullback" in params.enabled_setups
                else False
            )
        ).fillna(False)
        df[f"quality_score_{side}"] = _quality_score(df, side, params)
        df[f"expected_gross_bps_{side}"] = _expected_gross_bps(df, side, params)
        df[f"expected_net_edge_bps_{side}"] = (
            df[f"expected_gross_bps_{side}"] * df[f"quality_score_{side}"]
            - params.taker_round_trip_cost_bps
        )
    return df


def _add_side_geometry(
    df: pd.DataFrame,
    side: Side,
    params: QuantifiedFeeWallSniperParams,
) -> None:
    close = df["close"]
    close_safe = close.replace(0.0, float("nan"))
    atr_value = df["atr_value"]
    atr_bps = df["atr_bps"]
    atr_stop_bps = atr_bps * params.stop_atr_mult
    buffer_price = atr_value * params.stop_structure_atr_buffer
    if side == "long":
        structural_stop = df["structure_low"] - buffer_price
        structural_bps = (close - structural_stop) / close_safe * 10_000.0
        raw_stop_bps = structural_bps.where(
            (structural_bps > 0.0) & (structural_bps <= params.max_stop_bps),
            atr_stop_bps,
        )
        stop_bps = raw_stop_bps.clip(
            lower=params.min_stop_bps, upper=params.max_stop_bps
        )
        df["stop_bps_long"] = stop_bps
        df["stop_long"] = close * (1.0 - stop_bps / 10_000.0)
        df["tp1_long"] = close * (1.0 + stop_bps * params.tp1_r / 10_000.0)
        df["tp2_long"] = close * (1.0 + stop_bps * params.tp2_r / 10_000.0)
        df["tp3_long"] = close * (1.0 + stop_bps * params.tp3_r / 10_000.0)
        liquidity = prior_high(df["high"], params.range_lookback * 2)
        room = (liquidity - close) / close_safe * 10_000.0
    else:
        structural_stop = df["structure_high"] + buffer_price
        structural_bps = (structural_stop - close) / close_safe * 10_000.0
        raw_stop_bps = structural_bps.where(
            (structural_bps > 0.0) & (structural_bps <= params.max_stop_bps),
            atr_stop_bps,
        )
        stop_bps = raw_stop_bps.clip(
            lower=params.min_stop_bps, upper=params.max_stop_bps
        )
        df["stop_bps_short"] = stop_bps
        df["stop_short"] = close * (1.0 + stop_bps / 10_000.0)
        df["tp1_short"] = close * (1.0 - stop_bps * params.tp1_r / 10_000.0)
        df["tp2_short"] = close * (1.0 - stop_bps * params.tp2_r / 10_000.0)
        df["tp3_short"] = close * (1.0 - stop_bps * params.tp3_r / 10_000.0)
        liquidity = prior_low(df["low"], params.range_lookback * 2)
        room = (close - liquidity) / close_safe * 10_000.0
    df[f"room_to_liquidity_bps_{side}"] = room.where(room > 0.0, stop_bps * params.tp3_r)


def _quality_score(
    df: pd.DataFrame,
    side: Side,
    params: QuantifiedFeeWallSniperParams,
) -> pd.Series:
    trend = (df["efficiency_ratio"] / max(params.min_er * 3.0, 1e-9)).clip(0.0, 1.0)
    volume = ((df["volume_z"] - params.min_volume_z + 1.0) / 3.0).clip(0.0, 1.0)
    body = (df["body_atr"] / max(params.min_body_atr * 2.0, 1e-9)).clip(0.0, 1.0)
    if side == "long":
        bbp = ((df["bbp_slope_bps"] - params.min_bbp_slope_bps + 5.0) / 20.0).clip(
            0.0, 1.0
        )
    else:
        bbp = ((-df["bbp_slope_bps"] - params.min_bbp_slope_bps + 5.0) / 20.0).clip(
            0.0, 1.0
        )
    setup_bonus = pd.Series(0.0, index=df.index)
    setup_bonus = setup_bonus.mask(df[f"breakout_{side}"], 0.12)
    setup_bonus = setup_bonus.mask(df[f"pullback_{side}"], 0.08)
    score = 0.42 + 0.18 * trend + 0.14 * volume + 0.14 * body + 0.10 * bbp + setup_bonus
    return score.clip(lower=0.0, upper=0.96).fillna(0.0)


def _expected_gross_bps(
    df: pd.DataFrame,
    side: Side,
    params: QuantifiedFeeWallSniperParams,
) -> pd.Series:
    stop_bps = df[f"stop_bps_{side}"]
    rr_gross = stop_bps * params.tp3_r
    room = df[f"room_to_liquidity_bps_{side}"]
    conservative_room = room.where(room > 0.0, rr_gross)
    return pd.concat([rr_gross, conservative_room], axis=1).min(axis=1)


def _tp_ladder(row: pd.Series, side: str) -> tuple[float, float, float]:
    return (
        float(row[f"tp1_{side}"]),
        float(row[f"tp2_{side}"]),
        float(row[f"tp3_{side}"]),
    )


def _setup_name(row: pd.Series, side: str) -> str:
    if _flag(row, f"breakout_{side}"):
        return "range_expansion_breakout"
    if _flag(row, f"pullback_{side}"):
        return "pullback_continuation"
    return "none"


def _coerce_params(
    params: QuantifiedFeeWallSniperParams | dict | None,
    *,
    allowed_sides: tuple[str, ...] | list[str] | None,
    min_expected_net_edge_bps: float | None,
    overrides: dict[str, object],
) -> QuantifiedFeeWallSniperParams:
    if isinstance(params, QuantifiedFeeWallSniperParams):
        base_values = {field.name: getattr(params, field.name) for field in fields(params)}
    else:
        base_values = {
            field.name: getattr(QuantifiedFeeWallSniperParams(), field.name)
            for field in fields(QuantifiedFeeWallSniperParams)
        }
        if isinstance(params, dict):
            base_values.update(params)
        elif params is not None:
            raise TypeError("params must be QuantifiedFeeWallSniperParams, dict, or None")
    names = {field.name for field in fields(QuantifiedFeeWallSniperParams)}
    unknown = sorted(set(overrides) - names)
    if unknown:
        raise ValueError(f"unsupported quantified_fee_wall_sniper params: {unknown}")
    base_values.update(overrides)
    if allowed_sides is not None:
        base_values["allowed_sides"] = tuple(allowed_sides)
    if min_expected_net_edge_bps is not None:
        base_values["min_expected_net_edge_bps"] = float(min_expected_net_edge_bps)
    base_values["allowed_sides"] = _validate_sides(tuple(base_values["allowed_sides"]))
    base_values["enabled_setups"] = _validate_setups(tuple(base_values["enabled_setups"]))
    out = QuantifiedFeeWallSniperParams(**base_values)
    _validate_positive(out)
    return out


def _validate_positive(params: QuantifiedFeeWallSniperParams) -> None:
    for name in (
        "fast_ema",
        "slow_ema",
        "bias_ema",
        "atr_window",
        "er_window",
        "range_lookback",
        "compression_lookback",
        "structure_window",
        "volume_z_window",
        "bbp_window",
        "pullback_memory_bars",
    ):
        if int(getattr(params, name)) <= 0:
            raise ValueError(f"{name} must be positive")
    if params.tp3_r <= params.tp2_r or params.tp2_r <= params.tp1_r:
        raise ValueError("TP ladder must be ordered tp1_r < tp2_r < tp3_r")
    if params.min_expected_net_edge_bps < 0:
        raise ValueError("min_expected_net_edge_bps cannot be negative")
    if params.min_stop_bps <= 0 or params.max_stop_bps <= params.min_stop_bps:
        raise ValueError("stop bps bounds are invalid")


def _validate_sides(sides: tuple[str, ...]) -> tuple[str, ...]:
    invalid = sorted(set(sides) - set(QUANTIFIED_FEE_WALL_SNIPER_SIDES))
    if invalid:
        raise ValueError(f"unsupported side(s): {invalid}")
    return sides


def _validate_setups(setups: tuple[str, ...]) -> tuple[str, ...]:
    invalid = sorted(set(setups) - set(QUANTIFIED_FEE_WALL_SNIPER_SETUPS))
    if invalid:
        raise ValueError(f"unsupported setup(s): {invalid}")
    if not setups:
        raise ValueError("enabled_setups must include at least one setup")
    return setups


def _flag(row: pd.Series, name: str) -> bool:
    value = row.get(name)
    return False if _is_nan(value) else bool(value)


def _is_nan(value: object) -> bool:
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        try:
            return not math.isfinite(float(value))
        except (TypeError, ValueError):
            return False
