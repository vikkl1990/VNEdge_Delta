"""Luxara Break & Bounce V27 scanner adapted for VNEDGE research.

The supplied TradingView script is a teaching overlay: build a live setup box
from prior bars, show wick previews, confirm a close/wick breakout, grade the
setup, then draw entry/SL/TP levels. VNEDGE keeps the causal box mechanics and
the five-part grade, but it does not trade preview labels or fixed chart-point
targets. Bot signals are confirmed breakouts only, with volume, box geometry,
room-to-liquidity, expected-edge, and maker-fill gates.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Literal

import pandas as pd

from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent
from vnedge.strategy.indicators import atr, ema, prior_high, prior_low


LUXARA_BREAK_BOUNCE_V27_ID = "luxara_break_bounce_v27_v1"
LUXARA_BREAK_BOUNCE_V27_SIDES: tuple[str, ...] = ("long", "short")
SignalMode = Literal["close_outside_box", "wick_outside_box"]
TrendMode = Literal["both", "with_ema_trend", "counter_ema_trend"]


@dataclass(frozen=True)
class LuxaraBreakBounceV27Params:
    """Frozen scanner parameters for the Break & Bounce research lane."""

    setup_lookback: int = 12
    signal_mode: SignalMode = "close_outside_box"
    cooldown_bars: int = 5

    ema_fast: int = 50
    ema_slow: int = 200
    trend_mode: TrendMode = "both"

    atr_window: int = 14
    volume_sma_window: int = 20
    min_volume_ratio: float = 1.50
    liquidity_lookback: int = 90
    measured_move_fraction: float = 0.75

    min_grade_score: int = 3
    min_box_width_atr: float = 0.55
    max_box_width_atr: float = 2.50
    min_breakout_bps: float = 8.0
    min_room_to_liquidity_bps: float = 0.0
    min_expected_net_edge_bps: float = 80.0
    min_fill_probability: float = 0.55

    stop_atr_mult: float = 0.85
    stop_buffer_atr: float = 0.15
    min_stop_bps: float = 10.0
    take_profit_r: float = 2.20

    taker_entry_bps: float = 5.0
    taker_exit_bps: float = 5.0
    slippage_bps: float = 2.0
    safety_buffer_bps: float = 5.0
    allowed_sides: tuple[str, ...] = ("short",)

    def __post_init__(self) -> None:
        if self.setup_lookback < 5:
            raise ValueError("setup_lookback must be >= 5")
        if self.cooldown_bars < 0:
            raise ValueError("cooldown_bars cannot be negative")
        if self.ema_fast < 1 or self.ema_slow < 1:
            raise ValueError("EMA lengths must be positive")
        if self.atr_window < 1:
            raise ValueError("atr_window must be >= 1")
        if self.volume_sma_window < 1:
            raise ValueError("volume_sma_window must be >= 1")
        if self.liquidity_lookback < self.setup_lookback:
            raise ValueError("liquidity_lookback must be >= setup_lookback")
        if not 0 <= self.min_grade_score <= 5:
            raise ValueError("min_grade_score must be in [0, 5]")
        if self.min_box_width_atr <= 0 or self.max_box_width_atr <= 0:
            raise ValueError("box width ATR bounds must be positive")
        if self.min_box_width_atr > self.max_box_width_atr:
            raise ValueError("min_box_width_atr cannot exceed max_box_width_atr")
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


def luxara_break_bounce_v27_warmup_bars(
    params: LuxaraBreakBounceV27Params,
) -> int:
    return max(
        params.setup_lookback + 1,
        params.ema_fast,
        params.ema_slow,
        params.atr_window + 1,
        params.volume_sma_window,
        params.liquidity_lookback + 1,
    ) + 2


def add_luxara_break_bounce_v27_columns(
    candles: pd.DataFrame,
    params: LuxaraBreakBounceV27Params = LuxaraBreakBounceV27Params(),
) -> pd.DataFrame:
    df = candles.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

    df["bb_atr"] = atr(df, params.atr_window)
    df["bb_ema_fast"] = ema(df["close"], params.ema_fast)
    df["bb_ema_slow"] = ema(df["close"], params.ema_slow)
    df["bb_bull_trend"] = df["bb_ema_fast"] > df["bb_ema_slow"]
    df["bb_bear_trend"] = df["bb_ema_fast"] < df["bb_ema_slow"]
    df["bb_trend_text"] = _trend_text(df)

    df["bb_box_high"] = prior_high(df["high"], params.setup_lookback)
    df["bb_box_low"] = prior_low(df["low"], params.setup_lookback)
    df["bb_box_mid"] = (df["bb_box_high"] + df["bb_box_low"]) / 2.0
    df["bb_box_width"] = df["bb_box_high"] - df["bb_box_low"]
    df["bb_box_width_atr"] = df["bb_box_width"] / df["bb_atr"].replace(0.0, float("nan"))
    df["bb_box_width_bps"] = df["bb_box_width"] / df["close"] * 10_000.0
    df["bb_setup_ready"] = (
        df["bb_box_high"].notna()
        & df["bb_box_low"].notna()
        & (df["bb_box_width"] > 0.0)
    )

    df["bb_volume_ratio"] = df["volume"] / df["volume"].rolling(
        params.volume_sma_window
    ).mean().replace(0.0, float("nan"))
    df["bb_volume_ok"] = df["bb_volume_ratio"] >= params.min_volume_ratio

    allow_long, allow_short = _allowed_by_trend(df, params)
    df["bb_allow_long"] = allow_long
    df["bb_allow_short"] = allow_short

    buy_break_value = df["high"] if params.signal_mode == "wick_outside_box" else df["close"]
    sell_break_value = df["low"] if params.signal_mode == "wick_outside_box" else df["close"]
    df["bb_breakout_bps_long"] = (
        (buy_break_value - df["bb_box_high"]) / df["close"] * 10_000.0
    )
    df["bb_breakout_bps_short"] = (
        (df["bb_box_low"] - sell_break_value) / df["close"] * 10_000.0
    )
    df["bb_preview_long"] = (
        df["bb_setup_ready"]
        & df["bb_allow_long"]
        & df["bb_volume_ok"]
        & (df["high"] > df["bb_box_high"])
        & (df["close"] <= df["bb_box_high"])
        & (df["high"].shift(1) <= df["bb_box_high"].shift(1))
    ).fillna(False)
    df["bb_preview_short"] = (
        df["bb_setup_ready"]
        & df["bb_allow_short"]
        & df["bb_volume_ok"]
        & (df["low"] < df["bb_box_low"])
        & (df["close"] >= df["bb_box_low"])
        & (df["low"].shift(1) >= df["bb_box_low"].shift(1))
    ).fillna(False)
    df["bb_signal_long_raw"] = (
        df["bb_setup_ready"]
        & df["bb_allow_long"]
        & df["bb_volume_ok"]
        & (buy_break_value > df["bb_box_high"])
        & (df["close"].shift(1) <= df["bb_box_high"])
    ).fillna(False)
    df["bb_signal_short_raw"] = (
        df["bb_setup_ready"]
        & df["bb_allow_short"]
        & df["bb_volume_ok"]
        & (sell_break_value < df["bb_box_low"])
        & (df["close"].shift(1) >= df["bb_box_low"])
    ).fillna(False)

    df["bb_grade_score_long"] = _grade_score(df, "long")
    df["bb_grade_score_short"] = _grade_score(df, "short")
    df["bb_grade_long"] = df["bb_grade_score_long"].map(_grade_from_score)
    df["bb_grade_short"] = df["bb_grade_score_short"].map(_grade_from_score)

    df["bb_liquidity_high"] = prior_high(df["high"], params.liquidity_lookback)
    df["bb_liquidity_low"] = prior_low(df["low"], params.liquidity_lookback)
    measured_room = (df["bb_box_width_bps"] * params.measured_move_fraction).clip(lower=0.0)
    overhead_room = ((df["bb_liquidity_high"] - df["close"]) / df["close"] * 10_000.0).clip(lower=0.0)
    downside_room = ((df["close"] - df["bb_liquidity_low"]) / df["close"] * 10_000.0).clip(lower=0.0)
    df["bb_room_to_liquidity_long"] = pd.concat(
        [overhead_room, measured_room], axis=1
    ).max(axis=1)
    df["bb_room_to_liquidity_short"] = pd.concat(
        [downside_room, measured_room], axis=1
    ).max(axis=1)

    (
        df["stop_long"],
        df["target_long"],
        df["tp1_long"],
        df["tp2_long"],
        df["tp3_long"],
        df["expected_gross_bps_long"],
        df["expected_net_edge_bps_long"],
        df["fill_probability_long"],
    ) = _exit_and_edge_columns(df, "long", params)
    (
        df["stop_short"],
        df["target_short"],
        df["tp1_short"],
        df["tp2_short"],
        df["tp3_short"],
        df["expected_gross_bps_short"],
        df["expected_net_edge_bps_short"],
        df["fill_probability_short"],
    ) = _exit_and_edge_columns(df, "short", params)

    df["candidate_long"] = _candidate_side(df, "long", params)
    df["candidate_short"] = _candidate_side(df, "short", params)
    return df


class LuxaraBreakBounceV27Scanner(BaseStrategy):
    strategy_id = LUXARA_BREAK_BOUNCE_V27_ID

    def __init__(
        self,
        funding: pd.DataFrame | None = None,
        *,
        params: LuxaraBreakBounceV27Params | None = None,
        allowed_sides: tuple[str, ...] | list[str] | None = None,
        min_grade_score: int | None = None,
        min_expected_net_edge_bps: float | None = None,
        signal_mode: SignalMode | None = None,
        cooldown_bars: int | None = None,
    ) -> None:
        base = params or LuxaraBreakBounceV27Params()
        self.params = LuxaraBreakBounceV27Params(
            setup_lookback=base.setup_lookback,
            signal_mode=base.signal_mode if signal_mode is None else signal_mode,
            cooldown_bars=base.cooldown_bars if cooldown_bars is None else cooldown_bars,
            ema_fast=base.ema_fast,
            ema_slow=base.ema_slow,
            trend_mode=base.trend_mode,
            atr_window=base.atr_window,
            volume_sma_window=base.volume_sma_window,
            min_volume_ratio=base.min_volume_ratio,
            liquidity_lookback=base.liquidity_lookback,
            measured_move_fraction=base.measured_move_fraction,
            min_grade_score=base.min_grade_score if min_grade_score is None else min_grade_score,
            min_box_width_atr=base.min_box_width_atr,
            max_box_width_atr=base.max_box_width_atr,
            min_breakout_bps=base.min_breakout_bps,
            min_room_to_liquidity_bps=base.min_room_to_liquidity_bps,
            min_expected_net_edge_bps=(
                base.min_expected_net_edge_bps
                if min_expected_net_edge_bps is None
                else min_expected_net_edge_bps
            ),
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
        self.warmup_bars = luxara_break_bounce_v27_warmup_bars(self.params)

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        return add_luxara_break_bounce_v27_columns(candles, self.params)

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        row = df.iloc[index]
        required = (
            "bb_box_high",
            "bb_box_low",
            "bb_atr",
            "bb_volume_ratio",
            "bb_grade_score_long",
            "bb_grade_score_short",
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
        if side not in LUXARA_BREAK_BOUNCE_V27_SIDES:
            return None
        row = df.iloc[index]
        if _is_nan(row.get("bb_atr")):
            return None
        stop, target, _, _, _ = _exit_for_reference(side, float(entry_price), row, self.params)
        return SignalIntent(
            side,
            stop_price=stop,
            take_profit_price=target,
            reason="luxara_break_bounce_v27 rebuilt box-break exit; trailing_stop_first; BE_after_TP1",
        )

    def _ready(self, df: pd.DataFrame, index: int, side: str) -> bool:
        row = df.iloc[index]
        return bool(
            self._side_allowed(side)
            and row[f"candidate_{side}"]
            and float(row[f"bb_grade_score_{side}"]) >= self.params.min_grade_score
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
        trigger = "wick_breakout" if self.params.signal_mode == "wick_outside_box" else "close_breakout"
        return (
            f"luxara_break_bounce_v27 {side}; style=breakout_box; "
            f"trigger={trigger}; grade={row[f'bb_grade_{side}']} "
            f"{int(float(row[f'bb_grade_score_{side}']))}/5; "
            f"trend={row['bb_trend_text']}; "
            f"box={float(row['bb_box_low']):.6g}-{float(row['bb_box_high']):.6g}; "
            f"boxWidthAtr={float(row['bb_box_width_atr']):.2f}; "
            f"breakoutBps={float(row[f'bb_breakout_bps_{side}']):.1f}; "
            f"roomToLiquidity={float(row[f'bb_room_to_liquidity_{side}']):.1f}; "
            f"expectedEdge={edge:.1f}; fillProbability={fill:.2f}; "
            f"volRatio={float(row['bb_volume_ratio']):.2f}; "
            f"tp_ladder={float(row[f'tp1_{side}']):.6g}/{float(row[f'tp2_{side}']):.6g}/{float(row[f'tp3_{side}']):.6g}; "
            f"takerCost={self.params.taker_round_trip_cost_bps:.1f}; "
            "confirmed_only; trailing_stop_first; BE_after_TP1"
        )


def _allowed_by_trend(
    df: pd.DataFrame,
    params: LuxaraBreakBounceV27Params,
) -> tuple[pd.Series, pd.Series]:
    if params.trend_mode == "with_ema_trend":
        return df["bb_bull_trend"], df["bb_bear_trend"]
    if params.trend_mode == "counter_ema_trend":
        return df["bb_bear_trend"], df["bb_bull_trend"]
    return pd.Series(True, index=df.index), pd.Series(True, index=df.index)


def _trend_text(df: pd.DataFrame) -> pd.Series:
    values = []
    for _, row in df.iterrows():
        if bool(row["bb_bull_trend"]):
            values.append("BULLISH")
        elif bool(row["bb_bear_trend"]):
            values.append("BEARISH")
        else:
            values.append("RANGE")
    return pd.Series(values, index=df.index, dtype="object")


def _grade_score(df: pd.DataFrame, side: str) -> pd.Series:
    if side == "long":
        checks = (
            df["bb_bull_trend"],
            df["close"] > df["bb_ema_fast"],
            df["close"] > df["open"],
            df["bb_volume_ok"],
            df["close"] > df["bb_box_high"],
        )
    else:
        checks = (
            df["bb_bear_trend"],
            df["close"] < df["bb_ema_fast"],
            df["close"] < df["open"],
            df["bb_volume_ok"],
            df["close"] < df["bb_box_low"],
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


def _candidate_side(
    df: pd.DataFrame,
    side: str,
    params: LuxaraBreakBounceV27Params,
) -> pd.Series:
    return (
        df[f"bb_signal_{side}_raw"]
        & (df[f"bb_grade_score_{side}"] >= params.min_grade_score)
        & (df["bb_box_width_atr"] >= params.min_box_width_atr)
        & (df["bb_box_width_atr"] <= params.max_box_width_atr)
        & (df[f"bb_breakout_bps_{side}"] >= params.min_breakout_bps)
        & (df[f"bb_room_to_liquidity_{side}"] >= params.min_room_to_liquidity_bps)
    ).fillna(False)


def _exit_and_edge_columns(
    df: pd.DataFrame,
    side: str,
    params: LuxaraBreakBounceV27Params,
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series, pd.Series, pd.Series, pd.Series, pd.Series]:
    stops: list[float] = []
    targets: list[float] = []
    tp1s: list[float] = []
    tp2s: list[float] = []
    tp3s: list[float] = []
    gross_bps: list[float] = []
    net_bps: list[float] = []
    fill_probs: list[float] = []
    for _, row in df.iterrows():
        if _is_nan(row.get("bb_atr")) or _is_nan(row.get("bb_box_high")) or _is_nan(row.get("bb_box_low")):
            stops.append(float("nan"))
            targets.append(float("nan"))
            tp1s.append(float("nan"))
            tp2s.append(float("nan"))
            tp3s.append(float("nan"))
            gross_bps.append(float("nan"))
            net_bps.append(float("nan"))
            fill_probs.append(float("nan"))
            continue
        close = float(row["close"])
        stop, target, tp1, tp2, tp3 = _exit_for_reference(side, close, row, params)
        target_bps = abs(target - close) / close * 10_000.0
        room = max(0.0, float(row.get(f"bb_room_to_liquidity_{side}", 0.0)))
        capped_gross = min(target_bps, room)
        quality = _edge_quality(row, side, params)
        expected_gross = capped_gross * quality
        stops.append(stop)
        targets.append(target)
        tp1s.append(tp1)
        tp2s.append(tp2)
        tp3s.append(tp3)
        gross_bps.append(expected_gross)
        net_bps.append(expected_gross - params.taker_round_trip_cost_bps)
        fill_probs.append(_fill_probability(row, side, params))
    index = df.index
    return (
        pd.Series(stops, index=index, dtype="float64"),
        pd.Series(targets, index=index, dtype="float64"),
        pd.Series(tp1s, index=index, dtype="float64"),
        pd.Series(tp2s, index=index, dtype="float64"),
        pd.Series(tp3s, index=index, dtype="float64"),
        pd.Series(gross_bps, index=index, dtype="float64"),
        pd.Series(net_bps, index=index, dtype="float64"),
        pd.Series(fill_probs, index=index, dtype="float64"),
    )


def _exit_for_reference(
    side: str,
    reference_price: float,
    row: pd.Series,
    params: LuxaraBreakBounceV27Params,
) -> tuple[float, float, float, float, float]:
    atr_value = float(row["bb_atr"])
    min_stop = reference_price * params.min_stop_bps / 10_000.0
    atr_stop = max(params.stop_atr_mult * atr_value, min_stop)
    if side == "long":
        candidates = [reference_price - atr_stop]
        box_high_stop = float(row["bb_box_high"]) - params.stop_buffer_atr * atr_value
        if box_high_stop < reference_price:
            candidates.append(box_high_stop)
        stop = max(candidate for candidate in candidates if candidate < reference_price)
        if reference_price - stop < min_stop:
            stop = reference_price - min_stop
        risk = reference_price - stop
        tp1 = reference_price + risk
        tp2 = reference_price + 2.0 * risk
        tp3 = reference_price + 3.0 * risk
        target = reference_price + params.take_profit_r * risk
    else:
        candidates = [reference_price + atr_stop]
        box_low_stop = float(row["bb_box_low"]) + params.stop_buffer_atr * atr_value
        if box_low_stop > reference_price:
            candidates.append(box_low_stop)
        stop = min(candidate for candidate in candidates if candidate > reference_price)
        if stop - reference_price < min_stop:
            stop = reference_price + min_stop
        risk = stop - reference_price
        tp1 = reference_price - risk
        tp2 = reference_price - 2.0 * risk
        tp3 = reference_price - 3.0 * risk
        target = reference_price - params.take_profit_r * risk
    return stop, target, tp1, tp2, tp3


def _edge_quality(
    row: pd.Series,
    side: str,
    params: LuxaraBreakBounceV27Params,
) -> float:
    grade_quality = float(row.get(f"bb_grade_score_{side}", 0.0)) / 5.0
    volume_ratio = float(row.get("bb_volume_ratio", 0.0))
    volume_quality = min(1.0, max(0.0, (volume_ratio - 1.0) / 2.0))
    breakout_bps = float(row.get(f"bb_breakout_bps_{side}", 0.0))
    box_width_bps = max(1.0, float(row.get("bb_box_width_bps", 1.0)))
    breakout_quality = min(1.0, max(0.0, breakout_bps / (box_width_bps * 0.35)))
    box_width_atr = float(row.get("bb_box_width_atr", params.max_box_width_atr))
    if box_width_atr <= params.min_box_width_atr:
        box_quality = 0.4
    else:
        box_quality = 1.0 - min(
            1.0,
            max(0.0, (box_width_atr - 2.5) / max(1.0, params.max_box_width_atr - 2.5)),
        ) * 0.45
    return min(
        0.88,
        max(
            0.18,
            0.28
            + 0.24 * grade_quality
            + 0.20 * volume_quality
            + 0.18 * breakout_quality
            + 0.10 * box_quality,
        ),
    )


def _fill_probability(
    row: pd.Series,
    side: str,
    params: LuxaraBreakBounceV27Params,
) -> float:
    grade = float(row.get(f"bb_grade_score_{side}", 0.0))
    volume_ratio = float(row.get("bb_volume_ratio", 0.0))
    breakout_bps = float(row.get(f"bb_breakout_bps_{side}", 0.0))
    box_width_bps = max(1.0, float(row.get("bb_box_width_bps", 1.0)))
    breakout_quality = min(1.0, max(0.0, breakout_bps / (box_width_bps * 0.25)))
    volume_quality = min(1.0, max(0.0, (volume_ratio - 1.0) / 2.0))
    raw = 0.22 + grade / 5.0 * 0.28 + volume_quality * 0.18 + breakout_quality * 0.14
    if float(row.get("bb_box_width_atr", 99.0)) > params.max_box_width_atr:
        raw -= 0.08
    return round(min(0.85, max(0.18, raw)), 4)


def _validate_sides(values: tuple[str, ...]) -> tuple[str, ...]:
    unknown = sorted(set(values) - set(LUXARA_BREAK_BOUNCE_V27_SIDES))
    if unknown:
        raise ValueError(f"allowed_sides contains unknown values: {unknown}")
    return values


def _is_nan(value: Any) -> bool:
    try:
        return math.isnan(float(value))
    except (TypeError, ValueError):
        return True
