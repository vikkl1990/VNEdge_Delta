"""DATrend Suite / NomadaScalper inspired causal scanner.

TradingView marks the referenced script as protected source, so this module is
not a Pine port. It is a VNEDGE-owned proxy built only from the public
description: cycle-band oscillator extremes, golden-marker arming, structure
and cloud gates, volatility/ER quality filters, and a three-dot context panel.
It is research-only like every other scanner until normal evidence gates pass.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np
import pandas as pd

from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent
from vnedge.strategy.indicators import (
    atr,
    efficiency_ratio,
    ema,
    prior_high,
    prior_low,
    rolling_percentile,
    sma,
)


DATREND_NOMADA_SCALPER_ID = "datrend_nomada_scalper_v1"
DATREND_NOMADA_SCALPER_SIDES: tuple[str, ...] = ("long", "short")


@dataclass(frozen=True)
class DATrendNomadaScalperParams:
    """Frozen causal defaults for the protected-source DATrend proxy."""

    anchor_minutes: int = 5
    short_cycle: int = 13
    medium_cycle: int = 34
    long_cycle_mult: int = 4
    cycle_band_atr_mult: float = 1.35

    wma_fast: int = 55
    wma_slow: int = 200
    hold_window: int = 20
    hold_pct: float = 0.75
    slope_lookback: int = 8
    context_memory_window: int = 8

    er_window: int = 14
    er_memory_window: int = 12
    min_er_memory: float = 0.15
    atr_percentile_window: int = 120
    max_atr_percentile: float = 0.97

    use_daily_cloud: bool = True
    cloud_rule: str = "1D"
    cloud_length: int = 8
    ribbon_fast: int = 8
    ribbon_mid: int = 21
    ribbon_slow: int = 34
    bias_ema: int = 55
    rsi_window: int = 14
    rsi_smooth: int = 8
    min_panel_dots: int = 2

    arm_window: int = 12
    extreme_memory_window: int = 18
    pivot_window: int = 24
    stop_atr_mult: float = 1.20
    stop_buffer_atr: float = 0.10
    min_stop_bps: float = 10.0
    take_profit_r: float = 2.50

    taker_entry_bps: float = 5.0
    taker_exit_bps: float = 5.0
    slippage_bps: float = 2.0
    safety_buffer_bps: float = 5.0
    min_expected_net_edge_bps: float = 0.0
    allowed_sides: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.anchor_minutes < 1:
            raise ValueError("anchor_minutes must be >= 1")
        if self.short_cycle < 1 or self.medium_cycle < 1:
            raise ValueError("cycle lengths must be >= 1")
        if self.long_cycle_mult < 1:
            raise ValueError("long_cycle_mult must be >= 1")
        if not 0.0 <= self.hold_pct <= 1.0:
            raise ValueError("hold_pct must be in [0, 1]")
        if self.take_profit_r <= 0:
            raise ValueError("take_profit_r must be positive")
        unknown = sorted(set(self.allowed_sides) - set(DATREND_NOMADA_SCALPER_SIDES))
        if unknown:
            raise ValueError(f"allowed_sides contains unknown values: {unknown}")

    @property
    def taker_round_trip_cost_bps(self) -> float:
        return (
            self.taker_entry_bps
            + self.taker_exit_bps
            + self.slippage_bps
            + self.safety_buffer_bps
        )


def datrend_nomada_warmup_bars(
    params: DATrendNomadaScalperParams,
    candles: pd.DataFrame | None = None,
) -> int:
    scale = _timeframe_scale(candles, params) if candles is not None else 1
    local = max(
        _scaled(params.wma_slow, scale),
        _scaled(params.medium_cycle * params.long_cycle_mult, scale),
        _scaled(params.atr_percentile_window, scale),
        _scaled(params.pivot_window, scale),
        _scaled(params.bias_ema, scale),
    )
    return local + _scaled(max(params.arm_window, params.extreme_memory_window), scale) + 5


def add_datrend_nomada_columns(
    candles: pd.DataFrame,
    params: DATrendNomadaScalperParams = DATrendNomadaScalperParams(),
) -> pd.DataFrame:
    df = candles.copy()
    df["timestamp"] = _utc_ns(df["timestamp"])
    scale = _timeframe_scale(df, params)

    short_cycle = _scaled(params.short_cycle, scale)
    medium_cycle = _scaled(params.medium_cycle, scale)
    long_cycle = _scaled(params.medium_cycle * params.long_cycle_mult, scale)
    wma_fast_window = _scaled(params.wma_fast, scale)
    wma_slow_window = _scaled(params.wma_slow, scale)
    hold_window = _scaled(params.hold_window, scale)
    slope_lookback = _scaled(params.slope_lookback, scale)
    context_memory = _scaled(params.context_memory_window, scale)
    er_window = _scaled(params.er_window, scale)
    er_memory = _scaled(params.er_memory_window, scale)
    atr_window = _scaled(params.short_cycle, scale)
    atr_percentile_window = _scaled(params.atr_percentile_window, scale)
    arm_window = _scaled(params.arm_window, scale)
    extreme_memory = _scaled(params.extreme_memory_window, scale)
    pivot_window = _scaled(params.pivot_window, scale)

    df["datrend_atr"] = atr(df, atr_window)
    atr_safe = df["datrend_atr"].replace(0.0, float("nan"))
    df["cycle_short_mid"] = ema(df["close"], short_cycle)
    df["cycle_medium_mid"] = ema(df["close"], medium_cycle)
    df["cycle_long_mid"] = ema(df["close"], long_cycle)

    medium_half_width = df["datrend_atr"] * params.cycle_band_atr_mult
    medium_lower = df["cycle_medium_mid"] - medium_half_width
    medium_upper = df["cycle_medium_mid"] + medium_half_width
    medium_width = (medium_upper - medium_lower).replace(0.0, float("nan"))
    df["datrend_fast"] = (df["close"] - medium_lower) / medium_width
    df["datrend_slow"] = (df["cycle_short_mid"] - medium_lower) / medium_width

    long_half_width = df["datrend_atr"] * params.cycle_band_atr_mult * 1.20
    long_lower = df["cycle_long_mid"] - long_half_width
    long_upper = df["cycle_long_mid"] + long_half_width
    long_width = (long_upper - long_lower).replace(0.0, float("nan"))
    df["datrend_macro"] = (df["close"] - long_lower) / long_width
    df["macro_long"] = df["datrend_macro"] > 0.55
    df["macro_short"] = df["datrend_macro"] < 0.45

    df["datrend_er"] = efficiency_ratio(df["close"], er_window)
    df["datrend_er_memory"] = df["datrend_er"].rolling(er_memory).max()
    df["datrend_atr_percentile"] = rolling_percentile(
        df["datrend_atr"], atr_percentile_window
    )
    df["volatility_ok"] = df["datrend_atr_percentile"] <= params.max_atr_percentile

    df["datrend_wma_fast"] = _wma(df["close"], wma_fast_window)
    df["datrend_wma_slow"] = _wma(df["close"], wma_slow_window)
    above_both = (df["close"] > df["datrend_wma_fast"]) & (
        df["close"] > df["datrend_wma_slow"]
    )
    below_both = (df["close"] < df["datrend_wma_fast"]) & (
        df["close"] < df["datrend_wma_slow"]
    )
    df["trend_hold_long"] = above_both.rolling(hold_window).mean()
    df["trend_hold_short"] = below_both.rolling(hold_window).mean()
    df["trend_gate_long"] = (
        (df["trend_hold_long"] >= params.hold_pct)
        & (df["datrend_wma_fast"] > df["datrend_wma_slow"])
        & (df["datrend_wma_fast"] > df["datrend_wma_fast"].shift(slope_lookback))
    )
    df["trend_gate_short"] = (
        (df["trend_hold_short"] >= params.hold_pct)
        & (df["datrend_wma_fast"] < df["datrend_wma_slow"])
        & (df["datrend_wma_fast"] < df["datrend_wma_fast"].shift(slope_lookback))
    )

    if params.use_daily_cloud:
        df = _merge_daily_cloud(df, params)
        df["cloud_gate_long"] = df["close"] > df["daily_cloud_upper"]
        df["cloud_gate_short"] = df["close"] < df["daily_cloud_lower"]
    else:
        df["daily_cloud_upper"] = float("nan")
        df["daily_cloud_lower"] = float("nan")
        df["cloud_gate_long"] = True
        df["cloud_gate_short"] = True

    df["datrend_vwap"] = _daily_vwap(df)
    df["ribbon_fast"] = ema(df["close"], _scaled(params.ribbon_fast, scale))
    df["ribbon_mid"] = ema(df["close"], _scaled(params.ribbon_mid, scale))
    df["ribbon_slow"] = ema(df["close"], _scaled(params.ribbon_slow, scale))
    df["bias_ema"] = ema(df["close"], _scaled(params.bias_ema, scale))
    ribbon_high = df[["ribbon_fast", "ribbon_mid", "ribbon_slow"]].max(axis=1)
    ribbon_low = df[["ribbon_fast", "ribbon_mid", "ribbon_slow"]].min(axis=1)
    df["panel_c1_long"] = (
        (df["close"] > df["datrend_wma_fast"])
        & (df["close"] > df["datrend_wma_slow"])
        & (df["close"] > ribbon_high)
        & (df["close"] > df["datrend_vwap"])
    )
    df["panel_c1_short"] = (
        (df["close"] < df["datrend_wma_fast"])
        & (df["close"] < df["datrend_wma_slow"])
        & (df["close"] < ribbon_low)
        & (df["close"] < df["datrend_vwap"])
    )
    ha_open, ha_close = _heikin_ashi(df)
    df["ha_close"] = ha_close
    df["ha_open"] = ha_open
    df["panel_c2_long"] = df["ha_close"] > df["ha_open"]
    df["panel_c2_short"] = df["ha_close"] < df["ha_open"]
    df["datrend_rsi"] = _rsi(df["close"], _scaled(params.rsi_window, scale))
    df["datrend_rs_smooth"] = ema(df["datrend_rsi"], _scaled(params.rsi_smooth, scale))
    df["panel_c3_long"] = df["datrend_rs_smooth"] > df["datrend_rs_smooth"].shift(1)
    df["panel_c3_short"] = df["datrend_rs_smooth"] < df["datrend_rs_smooth"].shift(1)
    df["panel_score_long"] = (
        df["panel_c1_long"].astype(int)
        + df["panel_c2_long"].astype(int)
        + df["panel_c3_long"].astype(int)
    )
    df["panel_score_short"] = (
        df["panel_c1_short"].astype(int)
        + df["panel_c2_short"].astype(int)
        + df["panel_c3_short"].astype(int)
    )

    fast = df["datrend_fast"]
    slow = df["datrend_slow"]
    df["datrend_reclaim_long"] = (fast.shift(1) < 0.0) & (fast >= 0.0)
    df["datrend_rejection_short"] = (fast.shift(1) > 1.0) & (fast <= 1.0)
    df["datrend_cross_long"] = (fast.shift(1) <= slow.shift(1)) & (fast > slow)
    df["datrend_cross_short"] = (fast.shift(1) >= slow.shift(1)) & (fast < slow)
    df["datrend_extreme_long"] = fast.rolling(extreme_memory).min() < 0.0
    df["datrend_extreme_short"] = fast.rolling(extreme_memory).max() > 1.0

    context_long = _context_gate(df, "long", params)
    context_short = _context_gate(df, "short", params)
    df["datrend_context_long_raw"] = context_long
    df["datrend_context_short_raw"] = context_short
    df["datrend_context_long"] = (
        context_long.rolling(context_memory).max().fillna(False).astype(bool)
    )
    df["datrend_context_short"] = (
        context_short.rolling(context_memory).max().fillna(False).astype(bool)
    )
    df["datrend_golden_long"], df["datrend_golden_short"] = _golden_markers(
        df, arm_window=arm_window
    )

    df["datrend_prior_high"] = prior_high(df["high"], pivot_window)
    df["datrend_prior_low"] = prior_low(df["low"], pivot_window)
    (
        df["stop_long"],
        df["target_long"],
        df["expected_gross_bps_long"],
        df["expected_net_edge_bps_long"],
        df["fill_probability_long"],
    ) = _exit_columns(df, "long", params)
    (
        df["stop_short"],
        df["target_short"],
        df["expected_gross_bps_short"],
        df["expected_net_edge_bps_short"],
        df["fill_probability_short"],
    ) = _exit_columns(df, "short", params)
    df["datrend_ready_long"] = (
        df["datrend_golden_long"]
        & df["datrend_context_long"]
        & (df["expected_net_edge_bps_long"] >= params.min_expected_net_edge_bps)
    ).fillna(False)
    df["datrend_ready_short"] = (
        df["datrend_golden_short"]
        & df["datrend_context_short"]
        & (df["expected_net_edge_bps_short"] >= params.min_expected_net_edge_bps)
    ).fillna(False)
    return df


class DATrendNomadaScalper(BaseStrategy):
    strategy_id = DATREND_NOMADA_SCALPER_ID

    def __init__(
        self,
        funding: pd.DataFrame | None = None,
        *,
        params: DATrendNomadaScalperParams | None = None,
        allowed_sides: tuple[str, ...] | list[str] | None = None,
        min_expected_net_edge_bps: float | None = None,
    ) -> None:
        base = params or DATrendNomadaScalperParams()
        self.params = DATrendNomadaScalperParams(
            anchor_minutes=base.anchor_minutes,
            short_cycle=base.short_cycle,
            medium_cycle=base.medium_cycle,
            long_cycle_mult=base.long_cycle_mult,
            cycle_band_atr_mult=base.cycle_band_atr_mult,
            wma_fast=base.wma_fast,
            wma_slow=base.wma_slow,
            hold_window=base.hold_window,
            hold_pct=base.hold_pct,
            slope_lookback=base.slope_lookback,
            context_memory_window=base.context_memory_window,
            er_window=base.er_window,
            er_memory_window=base.er_memory_window,
            min_er_memory=base.min_er_memory,
            atr_percentile_window=base.atr_percentile_window,
            max_atr_percentile=base.max_atr_percentile,
            use_daily_cloud=base.use_daily_cloud,
            cloud_rule=base.cloud_rule,
            cloud_length=base.cloud_length,
            ribbon_fast=base.ribbon_fast,
            ribbon_mid=base.ribbon_mid,
            ribbon_slow=base.ribbon_slow,
            bias_ema=base.bias_ema,
            rsi_window=base.rsi_window,
            rsi_smooth=base.rsi_smooth,
            min_panel_dots=base.min_panel_dots,
            arm_window=base.arm_window,
            extreme_memory_window=base.extreme_memory_window,
            pivot_window=base.pivot_window,
            stop_atr_mult=base.stop_atr_mult,
            stop_buffer_atr=base.stop_buffer_atr,
            min_stop_bps=base.min_stop_bps,
            take_profit_r=base.take_profit_r,
            taker_entry_bps=base.taker_entry_bps,
            taker_exit_bps=base.taker_exit_bps,
            slippage_bps=base.slippage_bps,
            safety_buffer_bps=base.safety_buffer_bps,
            min_expected_net_edge_bps=(
                base.min_expected_net_edge_bps
                if min_expected_net_edge_bps is None
                else min_expected_net_edge_bps
            ),
            allowed_sides=tuple(base.allowed_sides if allowed_sides is None else allowed_sides),
        )
        self.funding = funding
        self.warmup_bars = datrend_nomada_warmup_bars(self.params)

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        df = add_datrend_nomada_columns(candles, self.params)
        self.warmup_bars = datrend_nomada_warmup_bars(self.params, df)
        return df

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        row = df.iloc[index]
        required = (
            "datrend_ready_long",
            "datrend_ready_short",
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
        if self._side_allowed("long") and bool(row["datrend_ready_long"]):
            return SignalIntent(
                "long",
                stop_price=float(row["stop_long"]),
                take_profit_price=float(row["target_long"]),
                reason=self._reason(row, "long"),
            )
        if self._side_allowed("short") and bool(row["datrend_ready_short"]):
            return SignalIntent(
                "short",
                stop_price=float(row["stop_short"]),
                take_profit_price=float(row["target_short"]),
                reason=self._reason(row, "short"),
            )
        return None

    def synthesize_exit_plan(
        self, df: pd.DataFrame, index: int, side: str, entry_price: float
    ) -> SignalIntent | None:
        if side not in DATREND_NOMADA_SCALPER_SIDES:
            return None
        row = df.iloc[index]
        if _is_nan(row.get("datrend_atr")):
            return None
        stop = _reference_stop(side, float(entry_price), row, self.params)
        target = _target_from_stop(side, float(entry_price), stop, self.params)
        return SignalIntent(
            side,
            stop_price=stop,
            take_profit_price=target,
            reason=(
                "datrend_nomada rebuilt golden-marker plan; protected_source_proxy; "
                "trailing_stop_first; BE_after_TP1_candidate"
            ),
        )

    def _side_allowed(self, side: str) -> bool:
        return not self.params.allowed_sides or side in self.params.allowed_sides

    def _reason(self, row: pd.Series, side: str) -> str:
        edge = float(row[f"expected_net_edge_bps_{side}"])
        fill = float(row[f"fill_probability_{side}"])
        return (
            f"datrend_nomada_scalper {side}; protected_source_proxy; "
            "trigger=golden_marker; context=cycle_extreme+structure+daily_cloud+panel; "
            f"fast={float(row['datrend_fast']):.2f}; slow={float(row['datrend_slow']):.2f}; "
            f"macro={float(row['datrend_macro']):.2f}; "
            f"erMemory={float(row['datrend_er_memory']):.2f}; "
            f"atrPct={float(row['datrend_atr_percentile']):.2f}; "
            f"panel={int(row[f'panel_score_{side}'])}/3; "
            f"expectedEdge={edge:.1f}; fillProbability={fill:.2f}; "
            f"takerCost={self.params.taker_round_trip_cost_bps:.1f}; "
            "closed_bar_only; no_protected_pine_source_copied"
        )


def _timeframe_scale(
    df: pd.DataFrame | None,
    params: DATrendNomadaScalperParams,
) -> int:
    if df is None or len(df) < 3 or "timestamp" not in df:
        return 1
    ts = pd.to_datetime(df["timestamp"], utc=True)
    minutes = ts.diff().dt.total_seconds().dropna().median() / 60.0
    if not math.isfinite(minutes) or minutes <= 0:
        return 1
    if minutes >= params.anchor_minutes:
        return 1
    return max(1, int(round(params.anchor_minutes / minutes)))


def _scaled(window: int, scale: int) -> int:
    return max(1, int(round(window * scale)))


def _wma(series: pd.Series, window: int) -> pd.Series:
    if window <= 1:
        return series.astype("float64")
    weights = np.arange(1, window + 1, dtype="float64")
    denominator = float(weights.sum())
    return series.rolling(window).apply(lambda values: float(np.dot(values, weights) / denominator), raw=True)


def _daily_vwap(df: pd.DataFrame) -> pd.Series:
    session = pd.to_datetime(df["timestamp"], utc=True).dt.floor("1D")
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = typical * df["volume"]
    cum_pv = pv.groupby(session).cumsum()
    cum_volume = df["volume"].groupby(session).cumsum().replace(0.0, float("nan"))
    return cum_pv / cum_volume


def _heikin_ashi(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    ha_close = (df["open"] + df["high"] + df["low"] + df["close"]) / 4.0
    opens: list[float] = []
    prev_open = float((df["open"].iloc[0] + df["close"].iloc[0]) / 2.0) if len(df) else float("nan")
    prev_close = float(ha_close.iloc[0]) if len(df) else float("nan")
    for i in range(len(df)):
        if i == 0:
            value = prev_open
        else:
            value = (prev_open + prev_close) / 2.0
        opens.append(value)
        prev_open = value
        prev_close = float(ha_close.iloc[i])
    return pd.Series(opens, index=df.index, dtype="float64"), ha_close


def _rsi(close: pd.Series, window: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0).rolling(window).mean()
    loss = (-delta.clip(upper=0.0)).rolling(window).mean()
    rs = gain / loss.replace(0.0, float("nan"))
    return 100.0 - (100.0 / (1.0 + rs))


def _merge_daily_cloud(
    df: pd.DataFrame,
    params: DATrendNomadaScalperParams,
) -> pd.DataFrame:
    if df.empty:
        return df
    src = df[["timestamp", "close"]].copy().set_index("timestamp")
    daily = src.resample(params.cloud_rule, label="left", closed="left").last().dropna()
    if daily.empty:
        out = df.copy()
        out["daily_cloud_upper"] = float("nan")
        out["daily_cloud_lower"] = float("nan")
        return out
    daily = daily.reset_index()
    daily["cloud_sma"] = sma(daily["close"], params.cloud_length)
    daily["cloud_wma"] = _wma(daily["close"], params.cloud_length)
    daily["cloud_ema"] = ema(daily["close"], params.cloud_length)
    daily["daily_cloud_upper"] = daily[["cloud_sma", "cloud_wma", "cloud_ema"]].max(axis=1)
    daily["daily_cloud_lower"] = daily[["cloud_sma", "cloud_wma", "cloud_ema"]].min(axis=1)
    daily["timestamp"] = daily["timestamp"] + pd.Timedelta(params.cloud_rule)
    right = daily[["timestamp", "daily_cloud_upper", "daily_cloud_lower"]].dropna()
    left = df.sort_values("timestamp").copy()
    left["timestamp"] = _utc_ns(left["timestamp"])
    right = right.sort_values("timestamp").copy()
    right["timestamp"] = _utc_ns(right["timestamp"])
    merged = pd.merge_asof(left, right, on="timestamp", direction="backward")
    return merged.sort_index()


def _utc_ns(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True).astype("datetime64[ns, UTC]")


def _context_gate(
    df: pd.DataFrame,
    side: str,
    params: DATrendNomadaScalperParams,
) -> pd.Series:
    return (
        df[f"trend_gate_{side}"]
        & df[f"macro_{side}"]
        & df[f"cloud_gate_{side}"]
        & df["volatility_ok"]
        & (df["datrend_er_memory"] >= params.min_er_memory)
        & (df[f"panel_score_{side}"] >= params.min_panel_dots)
    ).fillna(False)


def _golden_markers(
    df: pd.DataFrame,
    *,
    arm_window: int,
) -> tuple[pd.Series, pd.Series]:
    long_markers: list[bool] = []
    short_markers: list[bool] = []
    long_arm = 0
    short_arm = 0
    for _, row in df.iterrows():
        long_marker = False
        short_marker = False
        if bool(row.get("datrend_context_long", False)) and bool(row.get("datrend_reclaim_long", False)):
            long_arm = arm_window
        if bool(row.get("datrend_context_short", False)) and bool(row.get("datrend_rejection_short", False)):
            short_arm = arm_window
        if (
            long_arm > 0
            and bool(row.get("datrend_context_long", False))
            and bool(row.get("datrend_cross_long", False))
            and bool(row.get("datrend_extreme_long", False))
        ):
            long_marker = True
            long_arm = 0
        if (
            short_arm > 0
            and bool(row.get("datrend_context_short", False))
            and bool(row.get("datrend_cross_short", False))
            and bool(row.get("datrend_extreme_short", False))
        ):
            short_marker = True
            short_arm = 0
        long_markers.append(long_marker)
        short_markers.append(short_marker)
        long_arm = max(0, long_arm - 1)
        short_arm = max(0, short_arm - 1)
    return (
        pd.Series(long_markers, index=df.index, dtype=bool),
        pd.Series(short_markers, index=df.index, dtype=bool),
    )


def _exit_columns(
    df: pd.DataFrame,
    side: str,
    params: DATrendNomadaScalperParams,
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series, pd.Series]:
    stops: list[float] = []
    targets: list[float] = []
    gross_bps: list[float] = []
    net_bps: list[float] = []
    fill_probabilities: list[float] = []
    for _, row in df.iterrows():
        close = float(row["close"])
        stop = _reference_stop(side, close, row, params)
        target = _target_from_stop(side, close, stop, params)
        direction = 1.0 if side == "long" else -1.0
        gross = (target - close) / close * 10_000.0 * direction
        net = gross - params.taker_round_trip_cost_bps
        panel = float(row.get(f"panel_score_{side}", 0.0) or 0.0)
        er_memory = float(row.get("datrend_er_memory", 0.0) or 0.0)
        atr_percentile = float(row.get("datrend_atr_percentile", 1.0) or 1.0)
        fill = max(
            0.10,
            min(0.88, 0.24 + panel * 0.12 + er_memory * 0.25 + (1.0 - atr_percentile) * 0.12),
        )
        stops.append(stop)
        targets.append(target)
        gross_bps.append(gross)
        net_bps.append(net)
        fill_probabilities.append(fill)
    index = df.index
    return (
        pd.Series(stops, index=index, dtype="float64"),
        pd.Series(targets, index=index, dtype="float64"),
        pd.Series(gross_bps, index=index, dtype="float64"),
        pd.Series(net_bps, index=index, dtype="float64"),
        pd.Series(fill_probabilities, index=index, dtype="float64"),
    )


def _reference_stop(
    side: str,
    entry_price: float,
    row: pd.Series,
    params: DATrendNomadaScalperParams,
) -> float:
    atr_value = float(row.get("datrend_atr", float("nan")))
    if _is_nan(atr_value) or atr_value <= 0:
        atr_value = entry_price * 0.002
    buffer = params.stop_buffer_atr * atr_value
    min_distance = entry_price * params.min_stop_bps / 10_000.0
    atr_distance = params.stop_atr_mult * atr_value
    if side == "long":
        structural = float(row.get("datrend_prior_low", float("nan")))
        candidate = structural - buffer if not _is_nan(structural) else entry_price - atr_distance
        return min(candidate, entry_price - min_distance)
    structural = float(row.get("datrend_prior_high", float("nan")))
    candidate = structural + buffer if not _is_nan(structural) else entry_price + atr_distance
    return max(candidate, entry_price + min_distance)


def _target_from_stop(
    side: str,
    entry_price: float,
    stop_price: float,
    params: DATrendNomadaScalperParams,
) -> float:
    risk = abs(entry_price - stop_price)
    if risk <= 0:
        risk = entry_price * params.min_stop_bps / 10_000.0
    return entry_price + risk * params.take_profit_r if side == "long" else entry_price - risk * params.take_profit_r


def _is_nan(value: object) -> bool:
    try:
        return bool(pd.isna(value))
    except TypeError:
        return True
