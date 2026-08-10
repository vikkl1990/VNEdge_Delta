"""Causal 5-minute quality-trend scalper.

This is a VNEDGE-native distillation of the 5m visual scalping workflow the
operator shared: trend state, TQI-style quality components, BBP-like momentum
pressure, fixed-R target ladder semantics, and a protective stop. It does not
copy any TradingView/Pine implementation; every feature is computed from
closed candles only and every signal remains subject to the normal gateway,
journal, paper/shadow, and promotion machinery.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

import pandas as pd

from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent
from vnedge.strategy.indicators import atr, efficiency_ratio, ema, prior_high, prior_low


SATS_SIDES: tuple[str, ...] = ("long", "short")


@dataclass(frozen=True)
class Sats5mScalperParams:
    ema_fast: int = 13
    ema_slow: int = 34
    bbp_window: int = 13
    bbp_slope_window: int = 2
    stealth_trail_atr_mult: float = 2.5
    stealth_trail_reclaim_atr: float = 0.25
    er_window: int = 20
    rsi_window: int = 14
    atr_window: int = 14
    atr_pct_window: int = 100
    structure_window: int = 20
    volume_z_window: int = 30
    momentum_persistence_window: int = 5
    min_tqi: float = 0.58
    min_quality_strength: float = 0.08
    min_momentum_persistence: float = 0.55
    min_bbp_atr: float = 0.10
    min_bbp_slope: float = -0.05
    min_volume_z: float = -0.75
    min_long_rsi: float = 50.0
    max_long_rsi: float = 88.0
    min_short_rsi: float = 12.0
    max_short_rsi: float = 50.0
    normal_vol_mid: float = 0.50
    normal_vol_half_width: float = 0.48
    stop_atr_mult: float = 0.95
    stop_buffer_atr: float = 0.08
    min_stop_bps: float = 12.0
    take_profit_r: float = 3.0
    allowed_sides: tuple[str, ...] = ()


def sats_5m_warmup_bars(params: Sats5mScalperParams) -> int:
    return max(
        params.ema_slow + 1,
        params.bbp_window + params.bbp_slope_window + 1,
        params.atr_window + 2,
        params.er_window + 1,
        params.rsi_window + 1,
        params.atr_window + params.atr_pct_window,
        params.structure_window + 1,
        params.volume_z_window + 1,
        params.momentum_persistence_window + 1,
    )


def add_sats_5m_columns(
    candles: pd.DataFrame,
    params: Sats5mScalperParams = Sats5mScalperParams(),
) -> pd.DataFrame:
    df = candles.copy()
    df["atr"] = atr(df, params.atr_window)
    df["atr_pct"] = _rolling_percentile(df["atr"], params.atr_pct_window)
    df["er"] = efficiency_ratio(df["close"], params.er_window)
    df["rsi"] = _rsi(df["close"], params.rsi_window)
    df["ema_fast"] = ema(df["close"], params.ema_fast)
    df["ema_slow"] = ema(df["close"], params.ema_slow)

    pressure_ema = ema(df["close"], params.bbp_window)
    atr_safe = df["atr"].replace(0.0, float("nan"))
    df["bull_power"] = (df["high"] - pressure_ema) / atr_safe
    df["bear_power"] = (df["low"] - pressure_ema) / atr_safe
    df["bbp"] = df["bull_power"] + df["bear_power"]
    df["bbp_delta"] = df["bbp"].diff(params.bbp_slope_window)
    (
        df["stealth_trail"],
        df["stealth_trend"],
        df["stealth_upper_band"],
        df["stealth_lower_band"],
    ) = _stealth_trail(df, params)
    df["stealth_trend_long"] = df["stealth_trend"] > 0
    df["stealth_trend_short"] = df["stealth_trend"] < 0
    df["stealth_flip_long"] = (df["stealth_trend"] > 0) & (
        df["stealth_trend"].shift(1) <= 0
    )
    df["stealth_flip_short"] = (df["stealth_trend"] < 0) & (
        df["stealth_trend"].shift(1) >= 0
    )
    df["stealth_distance_atr"] = (
        (df["close"] - df["stealth_trail"]) / atr_safe
    )

    df["volume_z"] = _zscore(df["volume"], params.volume_z_window)
    up_bar = df["close"].diff() > 0
    down_bar = df["close"].diff() < 0
    df["mom_persist_long"] = up_bar.rolling(
        params.momentum_persistence_window
    ).mean()
    df["mom_persist_short"] = down_bar.rolling(
        params.momentum_persistence_window
    ).mean()

    df["prior_high"] = prior_high(df["high"], params.structure_window)
    df["prior_low"] = prior_low(df["low"], params.structure_window)
    structure_range = (df["prior_high"] - df["prior_low"]).replace(0.0, float("nan"))
    structure_pos = ((df["close"] - df["prior_low"]) / structure_range).clip(0.0, 1.0)
    df["structure_long"] = structure_pos
    df["structure_short"] = 1.0 - structure_pos
    df["structure_break_up"] = df["close"] > df["prior_high"]
    df["structure_break_down"] = df["close"] < df["prior_low"]

    df["trend_long"] = (
        (df["close"] > df["ema_fast"])
        & (df["ema_fast"] >= df["ema_slow"])
        & (df["bbp"] > 0)
        & df["stealth_trend_long"]
    )
    df["trend_short"] = (
        (df["close"] < df["ema_fast"])
        & (df["ema_fast"] <= df["ema_slow"])
        & (df["bbp"] < 0)
        & df["stealth_trend_short"]
    )
    df["bbp_cross_up"] = (df["bbp"] > params.min_bbp_atr) & (
        df["bbp"].shift(1) <= 0
    )
    df["bbp_cross_down"] = (df["bbp"] < -params.min_bbp_atr) & (
        df["bbp"].shift(1) >= 0
    )
    df["trend_resume_long"] = (
        df["trend_long"]
        & (
            (df["low"] <= df["ema_fast"] + 0.25 * df["atr"])
            | (df["low"] <= df["stealth_trail"] + params.stealth_trail_reclaim_atr * df["atr"])
        )
        & (df["close"] > df["open"])
    )
    df["trend_resume_short"] = (
        df["trend_short"]
        & (
            (df["high"] >= df["ema_fast"] - 0.25 * df["atr"])
            | (df["high"] >= df["stealth_trail"] - params.stealth_trail_reclaim_atr * df["atr"])
        )
        & (df["close"] < df["open"])
    )

    efficiency = df["er"].clip(0.0, 1.0)
    volatility = (
        1.0
        - ((df["atr_pct"] - params.normal_vol_mid).abs() / params.normal_vol_half_width)
    ).clip(0.0, 1.0)
    bbp_long = (df["bbp"] / 1.5).clip(0.0, 1.0)
    bbp_short = (-df["bbp"] / 1.5).clip(0.0, 1.0)
    momentum_long = (0.55 * bbp_long + 0.45 * df["mom_persist_long"]).clip(0.0, 1.0)
    momentum_short = (0.55 * bbp_short + 0.45 * df["mom_persist_short"]).clip(0.0, 1.0)

    df["tqi_long"] = (
        0.25 * efficiency
        + 0.20 * volatility
        + 0.25 * df["structure_long"]
        + 0.20 * momentum_long
        + 0.10 * df["trend_long"].astype(float)
    )
    df["tqi_short"] = (
        0.25 * efficiency
        + 0.20 * volatility
        + 0.25 * df["structure_short"]
        + 0.20 * momentum_short
        + 0.10 * df["trend_short"].astype(float)
    )
    df["quality_strength"] = (df["tqi_long"] - df["tqi_short"]).abs()
    df["sats_event_long"] = (
        df["structure_break_up"]
        | df["bbp_cross_up"]
        | df["stealth_flip_long"]
        | df["trend_resume_long"]
    )
    df["sats_event_short"] = (
        df["structure_break_down"]
        | df["bbp_cross_down"]
        | df["stealth_flip_short"]
        | df["trend_resume_short"]
    )
    return df


class Sats5mScalper(BaseStrategy):
    strategy_id = "sats_5m_scalper_v1"

    def __init__(
        self,
        funding: pd.DataFrame | None = None,
        *,
        ema_fast: int = 13,
        ema_slow: int = 34,
        min_tqi: float = 0.58,
        min_quality_strength: float = 0.08,
        min_momentum_persistence: float = 0.55,
        min_bbp_atr: float = 0.10,
        min_bbp_slope: float = -0.05,
        stealth_trail_atr_mult: float = 2.5,
        stealth_trail_reclaim_atr: float = 0.25,
        min_volume_z: float = -0.75,
        stop_atr_mult: float = 0.95,
        take_profit_r: float = 3.0,
        allowed_sides: tuple[str, ...] | list[str] | None = None,
        params: Sats5mScalperParams | None = None,
    ) -> None:
        base = params or Sats5mScalperParams()
        self.params = Sats5mScalperParams(
            ema_fast=ema_fast,
            ema_slow=ema_slow,
            bbp_window=base.bbp_window,
            bbp_slope_window=base.bbp_slope_window,
            stealth_trail_atr_mult=stealth_trail_atr_mult,
            stealth_trail_reclaim_atr=stealth_trail_reclaim_atr,
            er_window=base.er_window,
            rsi_window=base.rsi_window,
            atr_window=base.atr_window,
            atr_pct_window=base.atr_pct_window,
            structure_window=base.structure_window,
            volume_z_window=base.volume_z_window,
            momentum_persistence_window=base.momentum_persistence_window,
            min_tqi=min_tqi,
            min_quality_strength=min_quality_strength,
            min_momentum_persistence=min_momentum_persistence,
            min_bbp_atr=min_bbp_atr,
            min_bbp_slope=min_bbp_slope,
            min_volume_z=min_volume_z,
            min_long_rsi=base.min_long_rsi,
            max_long_rsi=base.max_long_rsi,
            min_short_rsi=base.min_short_rsi,
            max_short_rsi=base.max_short_rsi,
            normal_vol_mid=base.normal_vol_mid,
            normal_vol_half_width=base.normal_vol_half_width,
            stop_atr_mult=stop_atr_mult,
            stop_buffer_atr=base.stop_buffer_atr,
            min_stop_bps=base.min_stop_bps,
            take_profit_r=take_profit_r,
            allowed_sides=_validate_sides(
                tuple(base.allowed_sides if allowed_sides is None else allowed_sides)
            ),
        )
        self.funding = funding
        self.min_tqi = self.params.min_tqi
        self.min_quality_strength = self.params.min_quality_strength
        self.min_momentum_persistence = self.params.min_momentum_persistence
        self.min_bbp_atr = self.params.min_bbp_atr
        self.min_bbp_slope = self.params.min_bbp_slope
        self.stealth_trail_atr_mult = self.params.stealth_trail_atr_mult
        self.min_volume_z = self.params.min_volume_z
        self.take_profit_r = self.params.take_profit_r
        self.warmup_bars = sats_5m_warmup_bars(self.params)

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        return add_sats_5m_columns(candles, self.params)

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        row = df.iloc[index]
        required = (
            "atr",
            "atr_pct",
            "er",
            "rsi",
            "bull_power",
            "bear_power",
            "bbp",
            "bbp_delta",
            "stealth_trail",
            "stealth_trend",
            "stealth_distance_atr",
            "volume_z",
            "mom_persist_long",
            "mom_persist_short",
            "tqi_long",
            "tqi_short",
            "quality_strength",
        )
        if any(_is_nan(row[col]) for col in required):
            return None
        close = float(row["close"])
        atr_value = float(row["atr"])
        if atr_value <= 0:
            return None

        if self._long_ready(row):
            stop, target = self._exit_geometry("long", close, atr_value, row)
            return SignalIntent(
                "long",
                stop_price=stop,
                take_profit_price=target,
                reason=self._reason("long", row, close, stop, target),
            )
        if self._short_ready(row):
            stop, target = self._exit_geometry("short", close, atr_value, row)
            return SignalIntent(
                "short",
                stop_price=stop,
                take_profit_price=target,
                reason=self._reason("short", row, close, stop, target),
            )
        return None

    def synthesize_exit_plan(
        self, df: pd.DataFrame, index: int, side: str, entry_price: float
    ) -> SignalIntent | None:
        row = df.iloc[index]
        if "atr" not in row or _is_nan(row["atr"]):
            return None
        atr_value = float(row["atr"])
        if atr_value <= 0:
            return None
        stop, target = self._exit_geometry(side, float(entry_price), atr_value, row)
        return SignalIntent(
            side, stop_price=stop, take_profit_price=target,
            reason=(
                "sats_5m rebuilt fixed-R plan; "
                f"tp_ladder=1R/2R/{self.params.take_profit_r:.1f}R"
            ),
        )

    def _long_ready(self, row: pd.Series) -> bool:
        return bool(
            self._side_allowed("long")
            and row["trend_long"]
            and row["stealth_trend_long"]
            and row["sats_event_long"]
            and float(row["tqi_long"]) >= self.params.min_tqi
            and float(row["tqi_long"]) >= float(row["tqi_short"]) + self.params.min_quality_strength
            and float(row["quality_strength"]) >= self.params.min_quality_strength
            and float(row["mom_persist_long"]) >= self.params.min_momentum_persistence
            and float(row["bbp"]) >= self.params.min_bbp_atr
            and float(row["bbp_delta"]) >= self.params.min_bbp_slope
            and float(row["volume_z"]) >= self.params.min_volume_z
            and self.params.min_long_rsi <= float(row["rsi"]) <= self.params.max_long_rsi
        )

    def _short_ready(self, row: pd.Series) -> bool:
        return bool(
            self._side_allowed("short")
            and row["trend_short"]
            and row["stealth_trend_short"]
            and row["sats_event_short"]
            and float(row["tqi_short"]) >= self.params.min_tqi
            and float(row["tqi_short"]) >= float(row["tqi_long"]) + self.params.min_quality_strength
            and float(row["quality_strength"]) >= self.params.min_quality_strength
            and float(row["mom_persist_short"]) >= self.params.min_momentum_persistence
            and float(row["bbp"]) <= -self.params.min_bbp_atr
            and float(row["bbp_delta"]) <= -self.params.min_bbp_slope
            and float(row["volume_z"]) >= self.params.min_volume_z
            and self.params.min_short_rsi <= float(row["rsi"]) <= self.params.max_short_rsi
        )

    def _exit_geometry(
        self, side: str, reference_price: float, atr_value: float, row: pd.Series | None = None
    ) -> tuple[float, float]:
        min_stop = reference_price * self.params.min_stop_bps / 10_000.0
        risk = max(self.params.stop_atr_mult * atr_value, min_stop)
        if side == "long":
            stop = reference_price - risk
            if row is not None and "stealth_trail" in row and not _is_nan(row["stealth_trail"]):
                trail_stop = float(row["stealth_trail"]) - self.params.stop_buffer_atr * atr_value
                if trail_stop < reference_price:
                    stop = min(stop, trail_stop)
            final_risk = reference_price - stop
            target = reference_price + self.params.take_profit_r * final_risk
        else:
            stop = reference_price + risk
            if row is not None and "stealth_trail" in row and not _is_nan(row["stealth_trail"]):
                trail_stop = float(row["stealth_trail"]) + self.params.stop_buffer_atr * atr_value
                if trail_stop > reference_price:
                    stop = max(stop, trail_stop)
            final_risk = stop - reference_price
            target = reference_price - self.params.take_profit_r * final_risk
        return stop, target

    def _side_allowed(self, side: str) -> bool:
        return not self.params.allowed_sides or side in self.params.allowed_sides

    def _reason(
        self, side: str, row: pd.Series, close: float, stop: float, target: float
    ) -> str:
        risk = abs(close - stop)
        tp1 = close + risk if side == "long" else close - risk
        tp2 = close + 2.0 * risk if side == "long" else close - 2.0 * risk
        events = _active_events(side, row)
        tqi = float(row["tqi_long"] if side == "long" else row["tqi_short"])
        return (
            f"sats_5m_scalper {side}; events={','.join(events) or 'continuation'}; "
            f"TQI={tqi:.2f}; qStrength={float(row['quality_strength']):.2f}; "
            f"ER={float(row['er']):.2f}; RSI={float(row['rsi']):.1f}; "
            f"BullP={float(row['bull_power']):+.2f}; BearP={float(row['bear_power']):+.2f}; "
            f"BBP={float(row['bbp']):+.2f}; trail={float(row['stealth_trail']):.6g}; "
            f"trailDistAtr={float(row['stealth_distance_atr']):+.2f}; "
            f"volZ={float(row['volume_z']):+.2f}; "
            f"tp_ladder={tp1:.6g}/{tp2:.6g}/{target:.6g}; "
            "route=maker_first_taker_fallback_only_if_edge_covers_fee"
        )


def _active_events(side: str, row: pd.Series) -> list[str]:
    if side == "long":
        checks = (
            ("structure_break", "structure_break_up"),
            ("bbp_cross", "bbp_cross_up"),
            ("stealth_flip", "stealth_flip_long"),
            ("trend_resume", "trend_resume_long"),
        )
    else:
        checks = (
            ("structure_break", "structure_break_down"),
            ("bbp_cross", "bbp_cross_down"),
            ("stealth_flip", "stealth_flip_short"),
            ("trend_resume", "trend_resume_short"),
        )
    return [label for label, col in checks if bool(row.get(col, False))]


def _validate_sides(values: tuple[str, ...]) -> tuple[str, ...]:
    unknown = sorted(set(values) - set(SATS_SIDES))
    if unknown:
        raise ValueError(f"allowed_sides contains unknown values: {unknown}")
    return values


def _is_nan(value) -> bool:
    try:
        return math.isnan(float(value))
    except (TypeError, ValueError):
        return True


def _zscore(series: pd.Series, window: int) -> pd.Series:
    mean = series.rolling(window).mean()
    std = series.rolling(window).std()
    return (series - mean) / std.replace(0.0, float("nan"))


def _stealth_trail(
    df: pd.DataFrame, params: Sats5mScalperParams
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """Causal SuperTrend-style adaptive trail.

    The recursive bands use only previous state and the current closed candle.
    A long regime trails below price; a short regime trails above price.
    """
    hl2 = (df["high"] + df["low"]) / 2.0
    basic_upper = hl2 + params.stealth_trail_atr_mult * df["atr"]
    basic_lower = hl2 - params.stealth_trail_atr_mult * df["atr"]

    final_upper: list[float] = []
    final_lower: list[float] = []
    trend: list[int] = []
    trail: list[float] = []

    for i in range(len(df)):
        upper = float(basic_upper.iloc[i])
        lower = float(basic_lower.iloc[i])
        close = float(df["close"].iloc[i])
        prev_close = float(df["close"].iloc[i - 1]) if i > 0 else close

        if i == 0 or math.isnan(upper) or math.isnan(lower):
            final_upper.append(upper)
            final_lower.append(lower)
            trend.append(1 if close >= float(df["open"].iloc[i]) else -1)
            trail.append(lower if trend[-1] > 0 else upper)
            continue

        prev_upper = final_upper[-1]
        prev_lower = final_lower[-1]
        upper_band = (
            upper
            if upper < prev_upper or prev_close > prev_upper
            else prev_upper
        )
        lower_band = (
            lower
            if lower > prev_lower or prev_close < prev_lower
            else prev_lower
        )

        prev_trend = trend[-1]
        if math.isnan(prev_upper) or math.isnan(prev_lower):
            upper_band = upper
            lower_band = lower
        trigger_upper = upper if math.isnan(prev_upper) else prev_upper
        trigger_lower = lower if math.isnan(prev_lower) else prev_lower
        if prev_trend <= 0 and close > trigger_upper:
            current_trend = 1
        elif prev_trend >= 0 and close < trigger_lower:
            current_trend = -1
        else:
            current_trend = prev_trend

        final_upper.append(upper_band)
        final_lower.append(lower_band)
        trend.append(current_trend)
        trail.append(lower_band if current_trend > 0 else upper_band)

    index = df.index
    return (
        pd.Series(trail, index=index, dtype="float64"),
        pd.Series(trend, index=index, dtype="int64"),
        pd.Series(final_upper, index=index, dtype="float64"),
        pd.Series(final_lower, index=index, dtype="float64"),
    )


def _rsi(close: pd.Series, window: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0).rolling(window).mean()
    loss = (-delta.clip(upper=0.0)).rolling(window).mean()
    rs = gain / loss.replace(0.0, float("nan"))
    rsi = 100.0 - (100.0 / (1.0 + rs))
    rsi = rsi.where(~((gain == 0.0) & (loss == 0.0)), 50.0)
    return rsi.where(~((gain > 0.0) & (loss == 0.0)), 100.0)


def _rolling_percentile(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window).apply(
        lambda w: (w < w[-1]).mean() + 0.5 * (w == w[-1]).mean(), raw=True
    )
