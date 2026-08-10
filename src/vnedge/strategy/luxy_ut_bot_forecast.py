"""Lux/UT-style forecast scanner adapted for VNEDGE research.

This is a causal, VNEDGE-native scanner inspired by the operator's supplied
TradingView UT/forecast workflow. It does not copy Pine code into execution.
The useful trading package is represented as testable OHLCV features:

- adaptive UT/ATR trailing stop with chop-aware widening,
- 1h and 4h EMA/ER/ADX context,
- SuperTrend-style confirmation,
- RSI divergence, structure, displacement, and nearby S/R pressure,
- confidence, forecast bars, expected net edge, and maker-fill estimates.

Signals remain research-only until the normal router, model, untouched-data
judgment, shadow, and paper gates prove them.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

import pandas as pd

from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent
from vnedge.strategy.indicators import atr, efficiency_ratio, ema, prior_high, prior_low


LUXY_UT_BOT_FORECAST_ID = "luxy_ut_bot_forecast_v1"
LUXY_UT_BOT_FORECAST_SIDES: tuple[str, ...] = ("long", "short")


@dataclass(frozen=True)
class LuxyUTBotForecastParams:
    """Frozen scanner parameters for the Lux/UT-style research lane."""

    ut_key: float = 1.5
    ut_atr_window: int = 10
    crypto_asset_multiplier: float = 1.5
    adaptive_low_vol_multiplier: float = 0.85
    adaptive_high_vol_multiplier: float = 1.15
    volatility_ratio_window: int = 50
    efficiency_window: int = 10
    chop_strength: float = 1.1
    volume_sma_window: int = 20
    min_volume_ratio: float = 0.75

    supertrend_atr_window: int = 10
    supertrend_multiplier: float = 3.0
    adx_window: int = 14
    adx_threshold: float = 15.0

    mtf_fast_ema: int = 9
    mtf_slow_ema: int = 21
    mtf_er_window: int = 10
    mtf_adx_window: int = 14
    min_1h_er: float = 0.04
    min_4h_er: float = 0.03
    min_1h_adx: float = 8.0
    min_4h_adx: float = 7.0

    rsi_window: int = 14
    divergence_pivot_window: int = 5
    divergence_recent_window: int = 20
    structure_window: int = 10
    zone_window: int = 96
    zone_width_atr: float = 0.50
    displacement_atr_floor: float = 0.35
    displacement_percentile_floor: float = 0.60
    body_percentile_window: int = 100

    min_confidence: float = 60.0
    min_expected_net_edge_bps: float = 25.0
    cooldown_bars: int = 3
    allow_continuations: bool = False

    stop_atr_window: int = 14
    stop_atr_multiplier: float = 1.20
    stop_buffer_atr: float = 0.10
    min_stop_bps: float = 18.0
    take_profit_r: float = 2.0
    taker_entry_bps: float = 5.0
    taker_exit_bps: float = 5.0
    slippage_bps: float = 2.0
    safety_buffer_bps: float = 5.0
    allowed_sides: tuple[str, ...] = ()

    @property
    def taker_round_trip_cost_bps(self) -> float:
        return (
            self.taker_entry_bps
            + self.taker_exit_bps
            + self.slippage_bps
            + self.safety_buffer_bps
        )


def luxy_ut_bot_forecast_warmup_bars(params: LuxyUTBotForecastParams) -> int:
    local = max(
        params.ut_atr_window,
        params.volatility_ratio_window,
        params.efficiency_window,
        params.volume_sma_window,
        params.supertrend_atr_window * 2,
        params.adx_window * 2,
        params.rsi_window + params.divergence_pivot_window * 2,
        params.structure_window + 1,
        params.zone_window,
        params.body_percentile_window,
    )
    one_hour = max(
        params.mtf_slow_ema,
        params.mtf_er_window,
        params.mtf_adx_window * 2,
        params.ut_atr_window,
    ) * 4
    four_hour = max(
        params.mtf_slow_ema,
        params.mtf_er_window,
        params.mtf_adx_window * 2,
        params.ut_atr_window,
    ) * 16
    return max(local, one_hour, four_hour) + 2


def add_luxy_ut_bot_forecast_columns(
    candles: pd.DataFrame,
    params: LuxyUTBotForecastParams = LuxyUTBotForecastParams(),
) -> pd.DataFrame:
    df = candles.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

    df["atr_ut"] = atr(df, params.ut_atr_window)
    df["atr_stop"] = atr(df, params.stop_atr_window)
    df["atr_14"] = atr(df, 14)
    atr_safe = df["atr_14"].replace(0.0, float("nan"))
    df["ema_fast"] = ema(df["close"], params.mtf_fast_ema)
    df["ema_slow"] = ema(df["close"], params.mtf_slow_ema)
    df["volume_ratio"] = df["volume"] / df["volume"].rolling(
        params.volume_sma_window
    ).mean().replace(0.0, float("nan"))
    df["efficiency_ratio"] = efficiency_ratio(df["close"], params.efficiency_window)
    df["volatility_ratio"] = df["atr_ut"] / df["atr_ut"].rolling(
        params.volatility_ratio_window
    ).mean().replace(0.0, float("nan"))
    df["adaptive_multiplier"] = _adaptive_multiplier(df["volatility_ratio"], params)
    df["trail_distance"] = (
        df["atr_ut"]
        * params.ut_key
        * params.crypto_asset_multiplier
        * df["adaptive_multiplier"]
        * df["volume_ratio"].clip(lower=0.8, upper=1.2)
        * (1.0 + (1.0 - df["efficiency_ratio"].fillna(0.0)).clip(0.0, 1.0) * params.chop_strength)
    )
    df["ut_trail"], df["ut_trend"], df["ut_trend_bars"] = _ut_trailing_stop(df)
    df["ut_flip_long"] = (df["ut_trend"] > 0) & (df["ut_trend"].shift(1) < 0)
    df["ut_flip_short"] = (df["ut_trend"] < 0) & (df["ut_trend"].shift(1) > 0)

    df["supertrend"], df["supertrend_trend"], df["supertrend_strength"] = _supertrend(
        df, params
    )
    df["adx"] = _adx(df, params.adx_window)
    df["rsi"] = _rsi(df["close"], params.rsi_window)
    (
        df["bullish_divergence"],
        df["bearish_divergence"],
        df["bars_since_divergence"],
        df["last_divergence_side"],
    ) = _divergence_columns(df, params)

    body = df["close"] - df["open"]
    body_abs = body.abs()
    df["body_atr"] = body_abs / atr_safe
    df["body_percentile"] = _rolling_percentile(
        df["body_atr"], params.body_percentile_window
    )
    df["displacement_long"] = (
        (body > 0.0)
        & (df["body_atr"] >= params.displacement_atr_floor)
        & (df["body_percentile"] >= params.displacement_percentile_floor)
    )
    df["displacement_short"] = (
        (body < 0.0)
        & (df["body_atr"] >= params.displacement_atr_floor)
        & (df["body_percentile"] >= params.displacement_percentile_floor)
    )

    df["prior_high"] = prior_high(df["high"], params.structure_window)
    df["prior_low"] = prior_low(df["low"], params.structure_window)
    swing_mid = (df["prior_high"] + df["prior_low"]) / 2.0
    swing_half_range = (df["prior_high"] - df["prior_low"]) / 2.0
    df["structure_mid"] = swing_mid
    df["structure_bullish"] = df["close"] > swing_mid
    df["structure_bearish"] = df["close"] <= swing_mid
    df["structure_break_long"] = df["close"] > df["prior_high"]
    df["structure_break_short"] = df["close"] < df["prior_low"]
    df["structure_strength"] = (
        (df["close"] - swing_mid).abs() / swing_half_range.replace(0.0, float("nan")) * 100.0
    ).clip(0.0, 100.0)
    upper_wick = df["high"] - df[["open", "close"]].max(axis=1)
    lower_wick = df[["open", "close"]].min(axis=1) - df["low"]
    df["rejection_long"] = (
        (lower_wick >= body_abs.clip(lower=1e-12))
        & (df["close"] > df["open"])
        & (df["low"] <= df["ut_trail"] + params.stop_buffer_atr * df["atr_14"])
    )
    df["rejection_short"] = (
        (upper_wick >= body_abs.clip(lower=1e-12))
        & (df["close"] < df["open"])
        & (df["high"] >= df["ut_trail"] - params.stop_buffer_atr * df["atr_14"])
    )
    df["sweep_long"] = (
        (df["low"] < df["prior_low"])
        & (df["close"] > df["prior_low"])
        & (df["close"] > df["open"])
    )
    df["sweep_short"] = (
        (df["high"] > df["prior_high"])
        & (df["close"] < df["prior_high"])
        & (df["close"] < df["open"])
    )
    df["choch_long"] = df["structure_break_long"] & (df["ut_trend"].shift(1) < 0)
    df["choch_short"] = df["structure_break_short"] & (df["ut_trend"].shift(1) > 0)

    zone_tol = params.zone_width_atr * df["atr_14"]
    df["zone_resistance"] = prior_high(df["high"], params.zone_window)
    df["zone_support"] = prior_low(df["low"], params.zone_window)
    df["near_resistance"] = (
        (df["zone_resistance"] >= df["close"])
        & ((df["zone_resistance"] - df["close"]).abs() <= zone_tol)
    )
    df["near_support"] = (
        (df["zone_support"] <= df["close"])
        & ((df["close"] - df["zone_support"]).abs() <= zone_tol)
    )
    df["sr_pressure"] = _sr_pressure(df)

    df = _merge_context(df, _context_frame(df, "1h", params, prefix="context_1h"))
    df = _merge_context(df, _context_frame(df, "4h", params, prefix="context_4h"))
    df["mtf_score_long"] = _mtf_score(df, "long", params)
    df["mtf_score_short"] = _mtf_score(df, "short", params)
    df["mtf_aligned_long"] = df["mtf_score_long"] >= 2.0
    df["mtf_aligned_short"] = df["mtf_score_short"] >= 2.0

    df["confidence_long"] = _confidence(df, "long", params)
    df["confidence_short"] = _confidence(df, "short", params)
    (
        df["forecast_avg_bars"],
        df["forecast_max_bars"],
    ) = _forecast_bars(df["ut_trend"], sample_window=50)

    df["candidate_long"] = _candidate_side(df, "long", params)
    df["candidate_short"] = _candidate_side(df, "short", params)
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
    return df


class LuxyUTBotForecastScanner(BaseStrategy):
    strategy_id = LUXY_UT_BOT_FORECAST_ID

    def __init__(
        self,
        funding: pd.DataFrame | None = None,
        *,
        params: LuxyUTBotForecastParams | None = None,
        allowed_sides: tuple[str, ...] | list[str] | None = None,
        min_confidence: float | None = None,
        min_expected_net_edge_bps: float | None = None,
        cooldown_bars: int | None = None,
    ) -> None:
        base = params or LuxyUTBotForecastParams()
        self.params = LuxyUTBotForecastParams(
            ut_key=base.ut_key,
            ut_atr_window=base.ut_atr_window,
            crypto_asset_multiplier=base.crypto_asset_multiplier,
            adaptive_low_vol_multiplier=base.adaptive_low_vol_multiplier,
            adaptive_high_vol_multiplier=base.adaptive_high_vol_multiplier,
            volatility_ratio_window=base.volatility_ratio_window,
            efficiency_window=base.efficiency_window,
            chop_strength=base.chop_strength,
            volume_sma_window=base.volume_sma_window,
            min_volume_ratio=base.min_volume_ratio,
            supertrend_atr_window=base.supertrend_atr_window,
            supertrend_multiplier=base.supertrend_multiplier,
            adx_window=base.adx_window,
            adx_threshold=base.adx_threshold,
            mtf_fast_ema=base.mtf_fast_ema,
            mtf_slow_ema=base.mtf_slow_ema,
            mtf_er_window=base.mtf_er_window,
            mtf_adx_window=base.mtf_adx_window,
            min_1h_er=base.min_1h_er,
            min_4h_er=base.min_4h_er,
            min_1h_adx=base.min_1h_adx,
            min_4h_adx=base.min_4h_adx,
            rsi_window=base.rsi_window,
            divergence_pivot_window=base.divergence_pivot_window,
            divergence_recent_window=base.divergence_recent_window,
            structure_window=base.structure_window,
            zone_window=base.zone_window,
            zone_width_atr=base.zone_width_atr,
            displacement_atr_floor=base.displacement_atr_floor,
            displacement_percentile_floor=base.displacement_percentile_floor,
            body_percentile_window=base.body_percentile_window,
            min_confidence=base.min_confidence if min_confidence is None else min_confidence,
            min_expected_net_edge_bps=(
                base.min_expected_net_edge_bps
                if min_expected_net_edge_bps is None
                else min_expected_net_edge_bps
            ),
            cooldown_bars=base.cooldown_bars if cooldown_bars is None else cooldown_bars,
            allow_continuations=base.allow_continuations,
            stop_atr_window=base.stop_atr_window,
            stop_atr_multiplier=base.stop_atr_multiplier,
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
        self.warmup_bars = luxy_ut_bot_forecast_warmup_bars(self.params)

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        return add_luxy_ut_bot_forecast_columns(candles, self.params)

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        row = df.iloc[index]
        required = (
            "ut_trail",
            "ut_trend",
            "confidence_long",
            "confidence_short",
            "mtf_score_long",
            "mtf_score_short",
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
        if self._ready(row, "long") and self._cooldown_ok(df, index, "long"):
            return SignalIntent(
                "long",
                stop_price=float(row["stop_long"]),
                take_profit_price=float(row["target_long"]),
                reason=self._reason("long", row),
            )
        if self._ready(row, "short") and self._cooldown_ok(df, index, "short"):
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
        if side not in LUXY_UT_BOT_FORECAST_SIDES:
            return None
        row = df.iloc[index]
        if _is_nan(row.get("atr_stop")):
            return None
        stop, target, _, _, _ = _exit_for_reference(side, float(entry_price), row, self.params)
        return SignalIntent(
            side,
            stop_price=stop,
            take_profit_price=target,
            reason="luxy_ut_bot_forecast rebuilt UT trail plan; trailing_stop_first; BE_after_TP1",
        )

    def _ready(self, row: pd.Series, side: str) -> bool:
        if not self._side_allowed(side):
            return False
        return bool(
            row[f"candidate_{side}"]
            and float(row[f"confidence_{side}"]) >= self.params.min_confidence
            and float(row[f"expected_net_edge_bps_{side}"])
            >= self.params.min_expected_net_edge_bps
        )

    def _cooldown_ok(self, df: pd.DataFrame, index: int, side: str) -> bool:
        if self.params.cooldown_bars <= 0:
            return True
        start = max(0, index - self.params.cooldown_bars)
        trigger_col = f"candidate_{side}"
        recent = df.iloc[start:index]
        return not bool(recent[trigger_col].fillna(False).any())

    def _side_allowed(self, side: str) -> bool:
        return not self.params.allowed_sides or side in self.params.allowed_sides

    def _reason(self, side: str, row: pd.Series) -> str:
        events = _active_events(side, row)
        confidence = float(row[f"confidence_{side}"])
        edge = float(row[f"expected_net_edge_bps_{side}"])
        fill = float(row[f"fill_probability_{side}"])
        return (
            f"luxy_ut_bot_forecast {side}; style=day_trading_15m; "
            f"trigger={'flip' if bool(row[f'ut_flip_{side}']) else 'continuation'}; "
            f"confidence={confidence:.1f}; expectedEdge={edge:.1f}; "
            f"fillProbability={fill:.2f}; mtfScore={float(row[f'mtf_score_{side}']):.1f}; "
            f"adx={float(row['adx']):.1f}; er={float(row['efficiency_ratio']):.2f}; "
            f"volRatio={float(row['volume_ratio']):.2f}; bodyATR={float(row['body_atr']):.2f}; "
            f"structure={float(row['structure_strength']):.1f}; srPressure={float(row['sr_pressure']):+.1f}; "
            f"divergence={int(float(row['last_divergence_side']))}; "
            f"forecastAvgBars={float(row['forecast_avg_bars']):.1f}; "
            f"forecastMaxBars={float(row['forecast_max_bars']):.1f}; "
            f"events={','.join(events) or 'none'}; "
            f"trail={float(row['ut_trail']):.6g}; "
            f"tp_ladder={float(row[f'tp1_{side}']):.6g}/{float(row[f'tp2_{side}']):.6g}/{float(row[f'tp3_{side}']):.6g}; "
            f"takerCost={self.params.taker_round_trip_cost_bps:.1f}; "
            "trailing_stop_first; BE_after_TP1"
        )


def _adaptive_multiplier(
    volatility_ratio: pd.Series, params: LuxyUTBotForecastParams
) -> pd.Series:
    values: list[float] = []
    for raw in volatility_ratio:
        if _is_nan(raw):
            values.append(1.0)
        elif float(raw) < 0.8:
            values.append(params.adaptive_low_vol_multiplier)
        elif float(raw) > 1.2:
            values.append(params.adaptive_high_vol_multiplier)
        else:
            values.append(1.0)
    return pd.Series(values, index=volatility_ratio.index, dtype="float64")


def _ut_trailing_stop(df: pd.DataFrame) -> tuple[pd.Series, pd.Series, pd.Series]:
    trail: list[float] = []
    trend: list[int] = []
    trend_bars: list[int] = []
    for i in range(len(df)):
        close = float(df["close"].iloc[i])
        dist = float(df["trail_distance"].iloc[i])
        prev_close = float(df["close"].iloc[i - 1]) if i > 0 else close
        if i == 0 or math.isnan(dist):
            current_trend = 1 if close >= float(df["open"].iloc[i]) else -1
            current_trail = close - dist if current_trend > 0 and not math.isnan(dist) else close + dist
            if math.isnan(current_trail):
                current_trail = close
            current_bars = 1
        else:
            prev_stop = trail[-1]
            if close > prev_stop and prev_close > prev_stop:
                current_trail = max(prev_stop, close - dist)
            elif close < prev_stop and prev_close < prev_stop:
                current_trail = min(prev_stop, close + dist)
            elif close > prev_stop:
                current_trail = close - dist
            else:
                current_trail = close + dist
            current_trend = 1 if close > current_trail else -1
            current_bars = trend_bars[-1] + 1 if current_trend == trend[-1] else 1
        trail.append(current_trail)
        trend.append(current_trend)
        trend_bars.append(current_bars)
    index = df.index
    return (
        pd.Series(trail, index=index, dtype="float64"),
        pd.Series(trend, index=index, dtype="int64"),
        pd.Series(trend_bars, index=index, dtype="int64"),
    )


def _supertrend(
    df: pd.DataFrame, params: LuxyUTBotForecastParams
) -> tuple[pd.Series, pd.Series, pd.Series]:
    h = df["high"]
    l = df["low"]
    c = df["close"]
    st_atr = atr(df, params.supertrend_atr_window)
    hl2 = (h + l) / 2.0
    basic_upper = hl2 + params.supertrend_multiplier * st_atr
    basic_lower = hl2 - params.supertrend_multiplier * st_atr
    final_upper: list[float] = []
    final_lower: list[float] = []
    trend: list[int] = []
    line: list[float] = []
    strength: list[float] = []
    for i in range(len(df)):
        upper = float(basic_upper.iloc[i])
        lower = float(basic_lower.iloc[i])
        close = float(c.iloc[i])
        if i == 0 or math.isnan(upper) or math.isnan(lower):
            current_trend = 1 if close >= float(df["open"].iloc[i]) else -1
            final_upper.append(upper)
            final_lower.append(lower)
        else:
            prev_close = float(c.iloc[i - 1])
            prev_upper = final_upper[-1]
            prev_lower = final_lower[-1]
            upper_band = (
                upper
                if math.isnan(prev_upper) or upper < prev_upper or prev_close > prev_upper
                else prev_upper
            )
            lower_band = (
                lower
                if math.isnan(prev_lower) or lower > prev_lower or prev_close < prev_lower
                else prev_lower
            )
            prev_trend = trend[-1]
            if prev_trend <= 0 and close > (prev_upper if not math.isnan(prev_upper) else upper):
                current_trend = 1
            elif prev_trend >= 0 and close < (prev_lower if not math.isnan(prev_lower) else lower):
                current_trend = -1
            else:
                current_trend = prev_trend
            final_upper.append(upper_band)
            final_lower.append(lower_band)
        trend.append(current_trend)
        st_line = final_lower[-1] if current_trend > 0 else final_upper[-1]
        line.append(st_line)
        atr_value = float(st_atr.iloc[i])
        if math.isnan(atr_value) or atr_value <= 0 or math.isnan(st_line):
            strength.append(float("nan"))
        else:
            strength.append(min(100.0, abs(close - st_line) / atr_value * 25.0))
    index = df.index
    return (
        pd.Series(line, index=index, dtype="float64"),
        pd.Series(trend, index=index, dtype="int64"),
        pd.Series(strength, index=index, dtype="float64"),
    )


def _context_frame(
    df: pd.DataFrame,
    rule: str,
    params: LuxyUTBotForecastParams,
    *,
    prefix: str,
) -> pd.DataFrame:
    columns = [
        "timestamp",
        f"{prefix}_close",
        f"{prefix}_ema_fast",
        f"{prefix}_ema_slow",
        f"{prefix}_er",
        f"{prefix}_adx",
    ]
    htf = _resample_completed(df, rule)
    if htf.empty:
        return pd.DataFrame(
            {
                "timestamp": pd.Series(dtype="datetime64[ns, UTC]"),
                **{col: pd.Series(dtype="float64") for col in columns[1:]},
            }
        )
    htf[f"{prefix}_ema_fast"] = ema(htf["close"], params.mtf_fast_ema)
    htf[f"{prefix}_ema_slow"] = ema(htf["close"], params.mtf_slow_ema)
    htf[f"{prefix}_er"] = efficiency_ratio(htf["close"], params.mtf_er_window)
    htf[f"{prefix}_adx"] = _adx(htf, params.mtf_adx_window)
    return htf[
        [
            "timestamp",
            "close",
            f"{prefix}_ema_fast",
            f"{prefix}_ema_slow",
            f"{prefix}_er",
            f"{prefix}_adx",
        ]
    ].rename(columns={"close": f"{prefix}_close"})[columns]


def _resample_completed(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    if df.empty:
        return df.copy()
    src = df[["timestamp", "open", "high", "low", "close", "volume"]].copy()
    src["timestamp"] = pd.to_datetime(src["timestamp"], utc=True)
    src = src.set_index("timestamp")
    htf = src.resample(rule, label="left", closed="left").agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }
    ).dropna()
    if htf.empty:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    base_delta = _base_delta(df["timestamp"])
    complete_offset = max(pd.Timedelta(rule) - base_delta, pd.Timedelta(0))
    htf = htf.reset_index()
    htf["timestamp"] = htf["timestamp"] + complete_offset
    return htf.reset_index(drop=True)


def _merge_context(df: pd.DataFrame, context: pd.DataFrame) -> pd.DataFrame:
    if context.empty:
        return df
    left = df.sort_values("timestamp").copy()
    right = context.sort_values("timestamp").copy()
    left["timestamp"] = pd.to_datetime(left["timestamp"], utc=True).astype(
        "datetime64[ns, UTC]"
    )
    right["timestamp"] = pd.to_datetime(right["timestamp"], utc=True).astype(
        "datetime64[ns, UTC]"
    )
    merged = pd.merge_asof(left, right, on="timestamp", direction="backward")
    return merged.sort_index()


def _mtf_score(df: pd.DataFrame, side: str, params: LuxyUTBotForecastParams) -> pd.Series:
    sign = 1.0 if side == "long" else -1.0
    local = sign * (df["ema_fast"] - df["ema_slow"])
    ctx_1h = sign * (df["context_1h_ema_fast"] - df["context_1h_ema_slow"])
    ctx_4h = sign * (df["context_4h_ema_fast"] - df["context_4h_ema_slow"])
    score = (
        (local > 0.0).astype(float)
        + (
            (ctx_1h > 0.0)
            & (df["context_1h_er"] >= params.min_1h_er)
            & (df["context_1h_adx"] >= params.min_1h_adx)
        ).astype(float)
        + (
            (ctx_4h > 0.0)
            & (df["context_4h_er"] >= params.min_4h_er)
            & (df["context_4h_adx"] >= params.min_4h_adx)
        ).astype(float)
    )
    return score.astype("float64")


def _confidence(
    df: pd.DataFrame, side: str, params: LuxyUTBotForecastParams
) -> pd.Series:
    sign = 1 if side == "long" else -1
    trend_aligned = sign * df["ut_trend"] > 0
    st_aligned = sign * df["supertrend_trend"] > 0
    structure_aligned = df["structure_bullish"] if side == "long" else df["structure_bearish"]
    mtf_aligned = df[f"mtf_score_{side}"] >= 2.0
    div_aligned = df["last_divergence_side"] == sign
    div_opposed = df["last_divergence_side"] == -sign
    div_recent = df["bars_since_divergence"] <= params.divergence_recent_window
    vol_ratio = df["volume_ratio"].fillna(0.0)
    vol_score = pd.Series(3.0, index=df.index, dtype="float64")
    vol_score = vol_score.mask(vol_ratio >= 1.0, 6.0)
    vol_score = vol_score.mask(vol_ratio >= 1.5, 8.0)
    vol_score = vol_score.mask(vol_ratio >= 2.0, 10.0)
    confidence = (
        trend_aligned.astype(float).where(trend_aligned, 0.5) * 26.0
        + st_aligned.astype(float).where(st_aligned, 0.19) * (12.0 + df["supertrend_strength"].fillna(0.0).clip(0.0, 100.0) / 100.0 * 9.0)
        + structure_aligned.astype(float).where(structure_aligned, 0.22) * (11.0 + df["structure_strength"].fillna(0.0).clip(0.0, 100.0) / 100.0 * 7.0)
        + (df["adx"] >= params.adx_threshold).astype(float).where(df["adx"] >= params.adx_threshold, 0.31) * 13.0
        + mtf_aligned.astype(float).where(mtf_aligned, 0.25) * 8.0
        + vol_score
        + pd.Series(2.0, index=df.index, dtype="float64").mask(
            div_recent & div_aligned, 4.0
        ).mask(div_recent & div_opposed, 0.0)
    )
    return confidence.clip(0.0, 100.0).astype("float64")


def _candidate_side(
    df: pd.DataFrame, side: str, params: LuxyUTBotForecastParams
) -> pd.Series:
    sign = 1 if side == "long" else -1
    base = (
        (sign * df["ut_trend"] > 0)
        & df[f"mtf_aligned_{side}"]
        & (df["volume_ratio"] >= params.min_volume_ratio)
    )
    trigger = df[f"ut_flip_{side}"]
    if params.allow_continuations:
        continuation = (
            base
            & (
                df[f"displacement_{side}"]
                | df[f"rejection_{side}"]
                | df[f"sweep_{side}"]
                | df[f"choch_{side}"]
                | df[f"structure_break_{side}"]
            )
        )
        trigger = trigger | continuation
    return (base & trigger).fillna(False)


def _exit_and_edge_columns(
    df: pd.DataFrame, side: str, params: LuxyUTBotForecastParams
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
        if _is_nan(row.get("atr_stop")):
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
        risk = abs(close - stop)
        gross = abs(target - close) / close * 10_000.0
        confidence = float(row.get(f"confidence_{side}", 0.0))
        mtf_quality = min(1.0, max(0.0, float(row.get(f"mtf_score_{side}", 0.0)) / 3.0))
        sr_penalty = max(0.0, -float(row.get("sr_pressure", 0.0)) if side == "long" else float(row.get("sr_pressure", 0.0)))
        expected_gross = gross * (0.55 + 0.45 * confidence / 100.0) * (0.85 + 0.15 * mtf_quality)
        expected_net = expected_gross - params.taker_round_trip_cost_bps - sr_penalty
        fill_probability = _fill_probability(row, side)
        stops.append(stop)
        targets.append(target)
        tp1s.append(tp1)
        tp2s.append(tp2)
        tp3s.append(tp3)
        gross_bps.append(gross)
        net_bps.append(expected_net)
        fill_probs.append(fill_probability)
        if risk <= 0:
            raise ValueError("invalid stop geometry in luxy_ut_bot_forecast")
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
    params: LuxyUTBotForecastParams,
) -> tuple[float, float, float, float, float]:
    atr_value = float(row["atr_stop"])
    min_stop = reference_price * params.min_stop_bps / 10_000.0
    atr_stop = max(params.stop_atr_multiplier * atr_value, min_stop)
    if side == "long":
        candidates = [reference_price - atr_stop]
        if not _is_nan(row.get("prior_low")):
            candidates.append(float(row["prior_low"]) - params.stop_buffer_atr * atr_value)
        if not _is_nan(row.get("ut_trail")) and float(row["ut_trail"]) < reference_price:
            candidates.append(float(row["ut_trail"]) - params.stop_buffer_atr * atr_value)
        stop = max(candidate for candidate in candidates if candidate < reference_price)
        if reference_price - stop < min_stop:
            stop = reference_price - min_stop
        risk = reference_price - stop
        tp1 = reference_price + risk
        tp2 = reference_price + params.take_profit_r * risk
        tp3 = reference_price + 3.0 * risk
        target = tp2
    else:
        candidates = [reference_price + atr_stop]
        if not _is_nan(row.get("prior_high")):
            candidates.append(float(row["prior_high"]) + params.stop_buffer_atr * atr_value)
        if not _is_nan(row.get("ut_trail")) and float(row["ut_trail"]) > reference_price:
            candidates.append(float(row["ut_trail"]) + params.stop_buffer_atr * atr_value)
        stop = min(candidate for candidate in candidates if candidate > reference_price)
        if stop - reference_price < min_stop:
            stop = reference_price + min_stop
        risk = stop - reference_price
        tp1 = reference_price - risk
        tp2 = reference_price - params.take_profit_r * risk
        tp3 = reference_price - 3.0 * risk
        target = tp2
    return stop, target, tp1, tp2, tp3


def _fill_probability(row: pd.Series, side: str) -> float:
    confidence = float(row.get(f"confidence_{side}", 0.0))
    vol_ratio = float(row.get("volume_ratio", 0.0))
    trail_dist = abs(float(row.get("close", 0.0)) - float(row.get("ut_trail", 0.0)))
    atr_value = float(row.get("atr_14", float("nan")))
    proximity = 0.0 if _is_nan(atr_value) or atr_value <= 0 else max(0.0, 1.0 - trail_dist / (2.0 * atr_value))
    raw = 0.35 + (confidence - 50.0) / 100.0 * 0.35 + min(max(vol_ratio - 1.0, -0.5), 1.5) * 0.08 + proximity * 0.12
    return round(min(0.85, max(0.20, raw)), 4)


def _forecast_bars(
    trend: pd.Series, *, sample_window: int
) -> tuple[pd.Series, pd.Series]:
    avgs: list[float] = []
    maxes: list[float] = []
    durations_by_side: dict[int, list[int]] = {1: [], -1: []}
    current_side = 0
    current_len = 0
    for raw in trend:
        side = 1 if float(raw) > 0 else -1
        if current_side == 0:
            current_side = side
            current_len = 1
        elif side == current_side:
            current_len += 1
        else:
            if current_len > 1:
                bucket = durations_by_side[current_side]
                bucket.append(current_len)
                del bucket[:-sample_window]
            current_side = side
            current_len = 1
        bucket = durations_by_side[side]
        if len(bucket) >= 3:
            avg = _ewa(bucket, 0.9)
            stdev = _ewa_stdev(bucket, avg, 0.9)
            max_bar = min(avg + 2.0 * stdev, float(sorted(bucket)[int(0.9 * (len(bucket) - 1))]))
        elif bucket:
            avg = _ewa(bucket, 0.9)
            max_bar = avg * 2.5
        else:
            avg = 8.0
            max_bar = 16.0
        avgs.append(avg)
        maxes.append(max_bar)
    index = trend.index
    return (
        pd.Series(avgs, index=index, dtype="float64"),
        pd.Series(maxes, index=index, dtype="float64"),
    )


def _divergence_columns(
    df: pd.DataFrame, params: LuxyUTBotForecastParams
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    p = params.divergence_pivot_window
    lows = df["low"].to_list()
    highs = df["high"].to_list()
    rsi_values = df["rsi"].to_list()
    bullish = [False] * len(df)
    bearish = [False] * len(df)
    bars_since: list[int] = []
    last_side: list[int] = []
    last_low: tuple[float, float] | None = None
    last_high: tuple[float, float] | None = None
    last_div_index: int | None = None
    last_div_side = 0
    for i in range(len(df)):
        if i >= p * 2:
            center = i - p
            low_window = lows[center - p : center + p + 1]
            high_window = highs[center - p : center + p + 1]
            if len(low_window) == p * 2 + 1 and not _is_nan(rsi_values[center]):
                if lows[center] == min(low_window):
                    if last_low is not None and lows[center] < last_low[0] and rsi_values[center] > last_low[1]:
                        bullish[i] = True
                        last_div_index = i
                        last_div_side = 1
                    last_low = (float(lows[center]), float(rsi_values[center]))
                if highs[center] == max(high_window):
                    if last_high is not None and highs[center] > last_high[0] and rsi_values[center] < last_high[1]:
                        bearish[i] = True
                        last_div_index = i
                        last_div_side = -1
                    last_high = (float(highs[center]), float(rsi_values[center]))
        bars_since.append(999 if last_div_index is None else i - last_div_index)
        last_side.append(last_div_side)
    index = df.index
    return (
        pd.Series(bullish, index=index, dtype="bool"),
        pd.Series(bearish, index=index, dtype="bool"),
        pd.Series(bars_since, index=index, dtype="int64"),
        pd.Series(last_side, index=index, dtype="int64"),
    )


def _rsi(close: pd.Series, window: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0).rolling(window).mean()
    loss = (-delta.clip(upper=0.0)).rolling(window).mean()
    rs = gain / loss.replace(0.0, float("nan"))
    return 100.0 - (100.0 / (1.0 + rs))


def _adx(df: pd.DataFrame, window: int) -> pd.Series:
    high = df["high"]
    low = df["low"]
    close = df["close"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0.0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0.0), 0.0)
    tr = pd.concat(
        [
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr_sum = tr.rolling(window).sum().replace(0.0, float("nan"))
    plus_di = 100.0 * plus_dm.rolling(window).sum() / atr_sum
    minus_di = 100.0 * minus_dm.rolling(window).sum() / atr_sum
    dx = ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, float("nan"))) * 100.0
    return dx.rolling(window).mean()


def _rolling_percentile(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window).apply(
        lambda w: (w < w[-1]).mean() + 0.5 * (w == w[-1]).mean(), raw=True
    )


def _sr_pressure(df: pd.DataFrame) -> pd.Series:
    pressure = pd.Series(0.0, index=df.index, dtype="float64")
    pressure = pressure.mask(df["near_support"], 1.0)
    pressure = pressure.mask(df["near_resistance"], -1.0)
    pressure = pressure.mask(df["near_support"] & df["near_resistance"], 0.0)
    return pressure


def _active_events(side: str, row: pd.Series) -> list[str]:
    checks = (
        ("ut_flip", f"ut_flip_{side}"),
        ("displacement", f"displacement_{side}"),
        ("rejection", f"rejection_{side}"),
        ("sweep", f"sweep_{side}"),
        ("choch", f"choch_{side}"),
        ("break", f"structure_break_{side}"),
    )
    return [label for label, col in checks if bool(row.get(col, False))]


def _base_delta(timestamps: pd.Series) -> pd.Timedelta:
    deltas = pd.to_datetime(timestamps, utc=True).sort_values().diff().dropna()
    if deltas.empty:
        return pd.Timedelta(minutes=15)
    return deltas.median()


def _ewa(values: list[int], decay: float) -> float:
    weights = [decay ** (len(values) - 1 - i) for i in range(len(values))]
    denom = sum(weights)
    return sum(weight * value for weight, value in zip(weights, values, strict=True)) / denom


def _ewa_stdev(values: list[int], avg: float, decay: float) -> float:
    weights = [decay ** (len(values) - 1 - i) for i in range(len(values))]
    denom = sum(weights)
    return math.sqrt(
        sum(weight * (value - avg) ** 2 for weight, value in zip(weights, values, strict=True))
        / denom
    )


def _validate_sides(values: tuple[str, ...]) -> tuple[str, ...]:
    unknown = sorted(set(values) - set(LUXY_UT_BOT_FORECAST_SIDES))
    if unknown:
        raise ValueError(f"allowed_sides contains unknown values: {unknown}")
    return values


def _is_nan(value: Any) -> bool:
    try:
        return math.isnan(float(value))
    except (TypeError, ValueError):
        return True
