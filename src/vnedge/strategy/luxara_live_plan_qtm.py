"""Luxara Live Plan QTM scanner adapted for VNEDGE research.

The supplied TradingView script is a live-plan overlay: an ATR trail produces
BUY/SELL flips, while EMA, RSI, candle color, and support/resistance midline
produce a 5-point grade plus entry/SL/TP plan lines.  VNEDGE keeps that causal
workflow but replaces fixed chart-point targets with ATR/bps-aware exits and
execution-router metadata.

This is not a live trading approval.  The scanner is research-only until the
router, edge model, untouched-data judgment, shadow, and paper gates prove it.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Literal

import pandas as pd

from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent
from vnedge.strategy.indicators import atr, ema, prior_high, prior_low


LUXARA_LIVE_PLAN_QTM_ID = "luxara_live_plan_qtm_v1"
LUXARA_LIVE_PLAN_QTM_SIDES: tuple[str, ...] = ("long", "short")
SignalMode = Literal["trend_flip", "close_confirmed", "full_confirmed"]


@dataclass(frozen=True)
class LuxaraLivePlanQTMParams:
    """Frozen scanner parameters for the QTM live-plan research lane."""

    atr_period: int = 10
    atr_multiplier: float = 1.0
    signal_mode: SignalMode = "trend_flip"
    cooldown_bars: int = 0

    ema_length: int = 50
    use_ema_filter: bool = False
    rsi_length: int = 7
    rsi_buy_level: float = 55.0
    rsi_bear_level: float = 45.0
    structure_lookback: int = 70

    min_grade_score: int = 3
    min_volume_ratio: float = 1.50
    volume_sma_window: int = 20
    min_expected_net_edge_bps: float = 30.0
    min_room_to_liquidity_bps: float = 50.0
    min_fill_probability: float = 0.35

    stop_atr_mult: float = 1.20
    stop_buffer_atr: float = 0.08
    min_stop_bps: float = 10.0
    take_profit_r: float = 2.60

    taker_entry_bps: float = 5.0
    taker_exit_bps: float = 5.0
    slippage_bps: float = 2.0
    safety_buffer_bps: float = 5.0
    allowed_sides: tuple[str, ...] = ("long",)

    def __post_init__(self) -> None:
        if self.atr_period < 1:
            raise ValueError("atr_period must be >= 1")
        if self.atr_multiplier <= 0:
            raise ValueError("atr_multiplier must be positive")
        if self.cooldown_bars < 0:
            raise ValueError("cooldown_bars cannot be negative")
        if not 0 <= self.min_grade_score <= 5:
            raise ValueError("min_grade_score must be in [0, 5]")
        if self.take_profit_r <= 0:
            raise ValueError("take_profit_r must be positive")

    @property
    def taker_round_trip_cost_bps(self) -> float:
        return (
            self.taker_entry_bps
            + self.taker_exit_bps
            + self.slippage_bps
            + self.safety_buffer_bps
        )


def luxara_live_plan_qtm_warmup_bars(params: LuxaraLivePlanQTMParams) -> int:
    return max(
        params.atr_period + 2,
        params.ema_length,
        params.rsi_length + 2,
        params.structure_lookback + 1,
        params.volume_sma_window,
    ) + 2


def add_luxara_live_plan_qtm_columns(
    candles: pd.DataFrame,
    params: LuxaraLivePlanQTMParams = LuxaraLivePlanQTMParams(),
) -> pd.DataFrame:
    df = candles.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

    df["qtm_atr"] = atr(df, params.atr_period)
    df["qtm_ema"] = ema(df["close"], params.ema_length)
    df["qtm_rsi"] = _rsi(df["close"], params.rsi_length)
    df["qtm_resistance"] = df["high"].rolling(params.structure_lookback).max()
    df["qtm_support"] = df["low"].rolling(params.structure_lookback).min()
    df["qtm_midline"] = (df["qtm_resistance"] + df["qtm_support"]) / 2.0
    df["qtm_above_midline"] = df["close"] >= df["qtm_midline"]
    df["qtm_below_midline"] = df["close"] < df["qtm_midline"]
    df["qtm_volume_ratio"] = df["volume"] / df["volume"].rolling(
        params.volume_sma_window
    ).mean().replace(0.0, float("nan"))

    df["qtm_trail"], df["qtm_trend"], df["qtm_trend_bars"] = _qtm_trail(df, params)
    df["qtm_raw_buy"] = (
        (df["close"] > df["qtm_trail"]) & (df["close"].shift(1) <= df["qtm_trail"].shift(1))
    )
    df["qtm_raw_sell"] = (
        (df["close"] < df["qtm_trail"]) & (df["close"].shift(1) >= df["qtm_trail"].shift(1))
    )
    df["qtm_close_buy_ok"] = df["close"] > df["open"]
    df["qtm_close_sell_ok"] = df["close"] < df["open"]
    df["qtm_rsi_bull"] = df["qtm_rsi"] >= params.rsi_buy_level
    df["qtm_rsi_bear"] = df["qtm_rsi"] <= params.rsi_bear_level
    if params.use_ema_filter:
        df["qtm_ema_buy_ok"] = df["close"] >= df["qtm_ema"]
        df["qtm_ema_sell_ok"] = df["close"] <= df["qtm_ema"]
    else:
        df["qtm_ema_buy_ok"] = True
        df["qtm_ema_sell_ok"] = True

    df["qtm_grade_score_long"] = _grade_score(df, "long")
    df["qtm_grade_score_short"] = _grade_score(df, "short")
    df["qtm_grade_long"] = df["qtm_grade_score_long"].map(_grade_from_score)
    df["qtm_grade_short"] = df["qtm_grade_score_short"].map(_grade_from_score)
    df["qtm_signal_long"] = _mode_signal(df, "long", params)
    df["qtm_signal_short"] = _mode_signal(df, "short", params)
    df["qtm_prior_high"] = prior_high(df["high"], params.structure_lookback)
    df["qtm_prior_low"] = prior_low(df["low"], params.structure_lookback)
    df["qtm_structure_room_long"] = (
        (df["qtm_resistance"] - df["close"]) / df["close"] * 10_000.0
    )
    df["qtm_structure_room_short"] = (
        (df["close"] - df["qtm_support"]) / df["close"] * 10_000.0
    )

    (
        df["stop_long"],
        df["target_long"],
        df["tp1_long"],
        df["tp2_long"],
        df["expected_gross_bps_long"],
        df["expected_net_edge_bps_long"],
        df["fill_probability_long"],
    ) = _exit_and_edge_columns(df, "long", params)
    (
        df["stop_short"],
        df["target_short"],
        df["tp1_short"],
        df["tp2_short"],
        df["expected_gross_bps_short"],
        df["expected_net_edge_bps_short"],
        df["fill_probability_short"],
    ) = _exit_and_edge_columns(df, "short", params)
    df["candidate_long"] = _candidate_side(df, "long", params)
    df["candidate_short"] = _candidate_side(df, "short", params)
    return df


class LuxaraLivePlanQTMScanner(BaseStrategy):
    strategy_id = LUXARA_LIVE_PLAN_QTM_ID

    def __init__(
        self,
        funding: pd.DataFrame | None = None,
        *,
        params: LuxaraLivePlanQTMParams | None = None,
        allowed_sides: tuple[str, ...] | list[str] | None = None,
        min_grade_score: int | None = None,
        min_expected_net_edge_bps: float | None = None,
        signal_mode: SignalMode | None = None,
        cooldown_bars: int | None = None,
    ) -> None:
        base = params or LuxaraLivePlanQTMParams()
        self.params = LuxaraLivePlanQTMParams(
            atr_period=base.atr_period,
            atr_multiplier=base.atr_multiplier,
            signal_mode=base.signal_mode if signal_mode is None else signal_mode,
            cooldown_bars=base.cooldown_bars if cooldown_bars is None else cooldown_bars,
            ema_length=base.ema_length,
            use_ema_filter=base.use_ema_filter,
            rsi_length=base.rsi_length,
            rsi_buy_level=base.rsi_buy_level,
            rsi_bear_level=base.rsi_bear_level,
            structure_lookback=base.structure_lookback,
            min_grade_score=base.min_grade_score if min_grade_score is None else min_grade_score,
            min_volume_ratio=base.min_volume_ratio,
            volume_sma_window=base.volume_sma_window,
            min_expected_net_edge_bps=(
                base.min_expected_net_edge_bps
                if min_expected_net_edge_bps is None
                else min_expected_net_edge_bps
            ),
            min_room_to_liquidity_bps=base.min_room_to_liquidity_bps,
            min_fill_probability=base.min_fill_probability,
            stop_atr_mult=base.stop_atr_mult,
            stop_buffer_atr=base.stop_buffer_atr,
            min_stop_bps=base.min_stop_bps,
            take_profit_r=base.take_profit_r,
            taker_entry_bps=base.taker_entry_bps,
            taker_exit_bps=base.taker_exit_bps,
            slippage_bps=base.slippage_bps,
            safety_buffer_bps=base.safety_buffer_bps,
            allowed_sides=_validate_sides(
                tuple(base.allowed_sides if allowed_sides is None else allowed_sides)
            ),
        )
        self.funding = funding
        self.warmup_bars = luxara_live_plan_qtm_warmup_bars(self.params)

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        return add_luxara_live_plan_qtm_columns(candles, self.params)

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        row = df.iloc[index]
        required = (
            "qtm_trail",
            "qtm_trend",
            "qtm_rsi",
            "qtm_midline",
            "qtm_grade_score_long",
            "qtm_grade_score_short",
            "stop_long",
            "stop_short",
            "target_long",
            "target_short",
            "expected_net_edge_bps_long",
            "expected_net_edge_bps_short",
            "fill_probability_long",
            "fill_probability_short",
        )
        if any(_is_nan(row.get(col)) for col in required):
            return None
        if self._ready(df, index, "long"):
            return SignalIntent(
                "long",
                stop_price=float(row["stop_long"]),
                take_profit_price=float(row["target_long"]),
                reason=self._reason("long", row),
            )
        if self._ready(df, index, "short"):
            return SignalIntent(
                "short",
                stop_price=float(row["stop_short"]),
                take_profit_price=float(row["target_short"]),
                reason=self._reason("short", row),
            )
        return None

    def synthesize_exit_plan(
        self, df: pd.DataFrame, index: int, side: str, entry_price: float
    ) -> SignalIntent | None:
        if side not in LUXARA_LIVE_PLAN_QTM_SIDES:
            return None
        row = df.iloc[index]
        if _is_nan(row.get("qtm_atr")):
            return None
        stop, target, _, _ = _exit_for_reference(side, float(entry_price), row, self.params)
        return SignalIntent(
            side,
            stop_price=stop,
            take_profit_price=target,
            reason="luxara_live_plan_qtm rebuilt ATR live-plan exit; trailing_stop_first; BE_after_TP1",
        )

    def _ready(self, df: pd.DataFrame, index: int, side: str) -> bool:
        row = df.iloc[index]
        return bool(
            self._side_allowed(side)
            and row[f"candidate_{side}"]
            and float(row[f"qtm_grade_score_{side}"]) >= self.params.min_grade_score
            and float(row[f"expected_net_edge_bps_{side}"]) >= self.params.min_expected_net_edge_bps
            and float(row[f"fill_probability_{side}"]) >= self.params.min_fill_probability
            and self._cooldown_ok(df, index, side)
        )

    def _cooldown_ok(self, df: pd.DataFrame, index: int, side: str) -> bool:
        if self.params.cooldown_bars <= 0:
            return True
        start = max(0, index - self.params.cooldown_bars)
        return not bool(df.iloc[start:index][f"candidate_{side}"].fillna(False).any())

    def _side_allowed(self, side: str) -> bool:
        return not self.params.allowed_sides or side in self.params.allowed_sides

    def _reason(self, side: str, row: pd.Series) -> str:
        edge = float(row[f"expected_net_edge_bps_{side}"])
        fill = float(row[f"fill_probability_{side}"])
        return (
            f"luxara_live_plan_qtm {side}; style=qtm_live_plan; "
            f"mode={self.params.signal_mode}; grade={row[f'qtm_grade_{side}']} "
            f"{int(float(row[f'qtm_grade_score_{side}']))}/5; "
            f"rsi={float(row['qtm_rsi']):.2f}; "
            f"structure={'above_midline' if bool(row['qtm_above_midline']) else 'below_midline'}; "
            f"trail={float(row['qtm_trail']):.6g}; trendBars={int(float(row['qtm_trend_bars']))}; "
            f"roomToLiquidity={float(row[f'qtm_structure_room_{side}']):.1f}; "
            f"roomFloor={self.params.min_room_to_liquidity_bps:.1f}; "
            f"expectedEdge={edge:.1f}; fillProbability={fill:.2f}; "
            f"volRatio={float(row['qtm_volume_ratio']):.2f}; "
            f"tp_ladder={float(row[f'tp1_{side}']):.6g}/{float(row[f'tp2_{side}']):.6g}; "
            f"takerCost={self.params.taker_round_trip_cost_bps:.1f}; "
            "trailing_stop_first; BE_after_TP1"
        )


def _qtm_trail(
    df: pd.DataFrame,
    params: LuxaraLivePlanQTMParams,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    trail: list[float] = []
    trend: list[int] = []
    trend_bars: list[int] = []
    for i in range(len(df)):
        src = float(df["close"].iloc[i])
        n_loss = float(df["qtm_atr"].iloc[i]) * params.atr_multiplier
        prev_trail = trail[-1] if trail and not _is_nan(trail[-1]) else src
        prev_src = float(df["close"].iloc[i - 1]) if i > 0 else src
        if _is_nan(n_loss):
            current_trail = float("nan")
        elif src > prev_trail and prev_src > prev_trail:
            current_trail = max(prev_trail, src - n_loss)
        elif src < prev_trail and prev_src < prev_trail:
            current_trail = min(prev_trail, src + n_loss)
        elif src > prev_trail:
            current_trail = src - n_loss
        else:
            current_trail = src + n_loss

        if _is_nan(current_trail):
            current_trend = trend[-1] if trend else 0
        elif src > current_trail:
            current_trend = 1
        elif src < current_trail:
            current_trend = -1
        else:
            current_trend = trend[-1] if trend else 0
        previous = trend[-1] if trend else 0
        bars = trend_bars[-1] + 1 if current_trend == previous and current_trend != 0 else 1
        trail.append(current_trail)
        trend.append(current_trend)
        trend_bars.append(bars)
    index = df.index
    return (
        pd.Series(trail, index=index, dtype="float64"),
        pd.Series(trend, index=index, dtype="int64"),
        pd.Series(trend_bars, index=index, dtype="int64"),
    )


def _grade_score(df: pd.DataFrame, side: str) -> pd.Series:
    if side == "long":
        checks = (
            df["qtm_trend"] == 1,
            df["qtm_ema_buy_ok"],
            df["qtm_rsi_bull"],
            df["qtm_above_midline"],
            df["qtm_close_buy_ok"],
        )
    else:
        checks = (
            df["qtm_trend"] == -1,
            df["qtm_ema_sell_ok"],
            df["qtm_rsi_bear"],
            df["qtm_below_midline"],
            df["qtm_close_sell_ok"],
        )
    score = sum(check.astype(int) for check in checks)
    return score.astype("float64")


def _grade_from_score(score: float) -> str:
    if score >= 5:
        return "A+"
    if score == 4:
        return "A"
    if score == 3:
        return "B"
    return "C"


def _mode_signal(
    df: pd.DataFrame,
    side: str,
    params: LuxaraLivePlanQTMParams,
) -> pd.Series:
    if side == "long":
        raw = df["qtm_raw_buy"]
        close_ok = df["qtm_close_buy_ok"]
        full_ok = close_ok & df["qtm_ema_buy_ok"] & df["qtm_rsi_bull"] & df["qtm_above_midline"]
    else:
        raw = df["qtm_raw_sell"]
        close_ok = df["qtm_close_sell_ok"]
        full_ok = close_ok & df["qtm_ema_sell_ok"] & df["qtm_rsi_bear"] & df["qtm_below_midline"]
    if params.signal_mode == "trend_flip":
        return raw.fillna(False)
    if params.signal_mode == "close_confirmed":
        return (raw & close_ok).fillna(False)
    return (raw & full_ok).fillna(False)


def _candidate_side(
    df: pd.DataFrame,
    side: str,
    params: LuxaraLivePlanQTMParams,
) -> pd.Series:
    return (
        df[f"qtm_signal_{side}"]
        & (df[f"qtm_grade_score_{side}"] >= params.min_grade_score)
        & (df["qtm_volume_ratio"] >= params.min_volume_ratio)
        & (df[f"qtm_structure_room_{side}"] >= params.min_room_to_liquidity_bps)
    ).fillna(False)


def _exit_and_edge_columns(
    df: pd.DataFrame,
    side: str,
    params: LuxaraLivePlanQTMParams,
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series, pd.Series, pd.Series, pd.Series]:
    stops: list[float] = []
    targets: list[float] = []
    tp1s: list[float] = []
    tp2s: list[float] = []
    gross_bps: list[float] = []
    net_bps: list[float] = []
    fill_probs: list[float] = []
    for _, row in df.iterrows():
        if _is_nan(row.get("qtm_atr")):
            stops.append(float("nan"))
            targets.append(float("nan"))
            tp1s.append(float("nan"))
            tp2s.append(float("nan"))
            gross_bps.append(float("nan"))
            net_bps.append(float("nan"))
            fill_probs.append(float("nan"))
            continue
        close = float(row["close"])
        stop, target, tp1, tp2 = _exit_for_reference(side, close, row, params)
        gross = abs(target - close) / close * 10_000.0
        grade_quality = float(row.get(f"qtm_grade_score_{side}", 0.0)) / 5.0
        room = max(0.0, float(row.get(f"qtm_structure_room_{side}", 0.0)))
        room_quality = min(1.0, room / max(gross, 1.0))
        rsi_value = float(row.get("qtm_rsi", 50.0))
        rsi_quality = (
            min(1.0, max(0.0, (rsi_value - params.rsi_buy_level) / 20.0))
            if side == "long"
            else min(1.0, max(0.0, (params.rsi_bear_level - rsi_value) / 20.0))
        )
        expected_gross = gross * (0.42 + 0.32 * grade_quality + 0.16 * room_quality + 0.10 * rsi_quality)
        stops.append(stop)
        targets.append(target)
        tp1s.append(tp1)
        tp2s.append(tp2)
        gross_bps.append(gross)
        net_bps.append(expected_gross - params.taker_round_trip_cost_bps)
        fill_probs.append(_fill_probability(row, side))
    index = df.index
    return (
        pd.Series(stops, index=index, dtype="float64"),
        pd.Series(targets, index=index, dtype="float64"),
        pd.Series(tp1s, index=index, dtype="float64"),
        pd.Series(tp2s, index=index, dtype="float64"),
        pd.Series(gross_bps, index=index, dtype="float64"),
        pd.Series(net_bps, index=index, dtype="float64"),
        pd.Series(fill_probs, index=index, dtype="float64"),
    )


def _exit_for_reference(
    side: str,
    reference_price: float,
    row: pd.Series,
    params: LuxaraLivePlanQTMParams,
) -> tuple[float, float, float, float]:
    atr_value = float(row["qtm_atr"])
    min_stop = reference_price * params.min_stop_bps / 10_000.0
    atr_stop = max(params.stop_atr_mult * atr_value, min_stop)
    if side == "long":
        candidates = [reference_price - atr_stop]
        if not _is_nan(row.get("qtm_prior_low")):
            candidates.append(float(row["qtm_prior_low"]) - params.stop_buffer_atr * atr_value)
        if not _is_nan(row.get("qtm_trail")) and float(row["qtm_trail"]) < reference_price:
            candidates.append(float(row["qtm_trail"]) - params.stop_buffer_atr * atr_value)
        stop = max(candidate for candidate in candidates if candidate < reference_price)
        if reference_price - stop < min_stop:
            stop = reference_price - min_stop
        risk = reference_price - stop
        tp1 = reference_price + risk
        tp2 = reference_price + params.take_profit_r * risk
        target = tp2
    else:
        candidates = [reference_price + atr_stop]
        if not _is_nan(row.get("qtm_prior_high")):
            candidates.append(float(row["qtm_prior_high"]) + params.stop_buffer_atr * atr_value)
        if not _is_nan(row.get("qtm_trail")) and float(row["qtm_trail"]) > reference_price:
            candidates.append(float(row["qtm_trail"]) + params.stop_buffer_atr * atr_value)
        stop = min(candidate for candidate in candidates if candidate > reference_price)
        if stop - reference_price < min_stop:
            stop = reference_price + min_stop
        risk = stop - reference_price
        tp1 = reference_price - risk
        tp2 = reference_price - params.take_profit_r * risk
        target = tp2
    return stop, target, tp1, tp2


def _fill_probability(row: pd.Series, side: str) -> float:
    grade = float(row.get(f"qtm_grade_score_{side}", 0.0))
    vol = float(row.get("qtm_volume_ratio", 0.0))
    trend_bars = float(row.get("qtm_trend_bars", 99.0))
    freshness = max(0.0, 1.0 - trend_bars / 20.0)
    raw = 0.25 + grade / 5.0 * 0.30 + min(max(vol - 0.5, 0.0), 2.0) * 0.07 + freshness * 0.12
    return round(min(0.85, max(0.20, raw)), 4)


def _rsi(close: pd.Series, window: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0).rolling(window).mean()
    loss = (-delta.clip(upper=0.0)).rolling(window).mean()
    rs = gain / loss.replace(0.0, float("nan"))
    return 100.0 - (100.0 / (1.0 + rs))


def _validate_sides(values: tuple[str, ...]) -> tuple[str, ...]:
    unknown = sorted(set(values) - set(LUXARA_LIVE_PLAN_QTM_SIDES))
    if unknown:
        raise ValueError(f"allowed_sides contains unknown values: {unknown}")
    return values


def _is_nan(value: Any) -> bool:
    try:
        return math.isnan(float(value))
    except (TypeError, ValueError):
        return True
