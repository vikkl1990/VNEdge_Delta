"""FVG + liquidity breakout scanner distilled from source-backed Pine research.

The Pine Research Lab keeps surfacing the same useful primitive cluster:
liquidity sweep/reclaim, fair-value-gap retest, displacement, participation,
room to the next liquidity pool, and a trade plan with TP1/TP2/TP3 plus a
break-even trail.  This module ports that *idea* into a VNEDGE-owned causal
scanner.  It does not execute Pine code and it does not grant promotion.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

import pandas as pd

from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent
from vnedge.strategy.indicators import atr, efficiency_ratio, ema, prior_high, prior_low


FVG_LIQUIDITY_BREAKOUT_ID = "fvg_liquidity_breakout_v1"
FVG_LIQUIDITY_BREAKOUT_SIDES: tuple[str, ...] = ("long", "short")


@dataclass(frozen=True)
class FvgLiquidityBreakoutParams:
    """Frozen scanner parameters for the causal FVG/liquidity lane."""

    atr_window: int = 14
    ema_window: int = 13
    fvg_displacement_atr: float = 0.50
    fvg_ttl_bars: int = 48
    fvg_invalidation_atr: float = 0.12

    volume_z_window: int = 30
    body_percentile_window: int = 100
    structure_window: int = 20
    min_body_atr: float = 0.55
    min_body_percentile: float = 0.60
    min_volume_z: float = 0.35
    min_room_to_liquidity_bps: float = 35.0

    confirm_ema_fast: int = 8
    confirm_ema_slow: int = 21
    confirm_er_window: int = 10
    min_15m_er: float = 0.06

    bias_ema_fast: int = 13
    bias_ema_slow: int = 34
    bias_er_window: int = 12
    min_1h_er: float = 0.08

    stop_atr_mult: float = 1.00
    stop_buffer_atr: float = 0.10
    min_stop_bps: float = 12.0
    take_profit_r: float = 3.0
    min_expected_net_edge_bps: float = 25.0

    taker_entry_bps: float = 5.0
    taker_exit_bps: float = 5.0
    slippage_bps: float = 2.0
    safety_buffer_bps: float = 5.0
    allowed_sides: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.atr_window < 1:
            raise ValueError("atr_window must be >= 1")
        if self.fvg_ttl_bars < 1:
            raise ValueError("fvg_ttl_bars must be >= 1")
        if self.take_profit_r <= 0:
            raise ValueError("take_profit_r must be positive")
        if self.min_expected_net_edge_bps < 0:
            raise ValueError("min_expected_net_edge_bps cannot be negative")
        unknown = sorted(set(self.allowed_sides) - set(FVG_LIQUIDITY_BREAKOUT_SIDES))
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


@dataclass(frozen=True)
class FvgLiquidityBreakoutPromotionGate:
    min_expected_net_edge_bps: float = 25.0
    min_profit_factor: float = 1.50
    min_trades: int = 20

    def evaluate(
        self,
        *,
        avg_net_edge_bps: float | None,
        profit_factor: float | None,
        num_trades: int,
    ) -> tuple[bool, tuple[str, ...]]:
        reasons: list[str] = []
        if avg_net_edge_bps is None or avg_net_edge_bps <= self.min_expected_net_edge_bps:
            reasons.append("expected_net_edge_bps<=25")
        if profit_factor is None or profit_factor <= self.min_profit_factor:
            reasons.append("profit_factor<=1.5")
        if num_trades < self.min_trades:
            reasons.append("historical_trades<20")
        return not reasons, tuple(reasons)


FVG_LIQUIDITY_BREAKOUT_PROMOTION_GATE = FvgLiquidityBreakoutPromotionGate()


def fvg_liquidity_breakout_warmup_bars(params: FvgLiquidityBreakoutParams) -> int:
    one_hour_bars = max(params.bias_ema_slow, params.bias_er_window, params.atr_window) * 12
    fifteen_bars = max(
        params.confirm_ema_slow, params.confirm_er_window, params.atr_window
    ) * 3
    local_bars = max(
        params.ema_window,
        params.atr_window,
        params.volume_z_window,
        params.body_percentile_window,
        params.structure_window + 1,
        params.fvg_ttl_bars,
    )
    return max(one_hour_bars, fifteen_bars, local_bars) + 2


def add_fvg_liquidity_breakout_columns(
    candles: pd.DataFrame,
    params: FvgLiquidityBreakoutParams = FvgLiquidityBreakoutParams(),
) -> pd.DataFrame:
    df = candles.copy()
    df["timestamp"] = _utc_ns(df["timestamp"])
    df["fvg_atr"] = atr(df, params.atr_window)
    atr_safe = df["fvg_atr"].replace(0.0, float("nan"))
    df["fvg_ema"] = ema(df["close"], params.ema_window)

    body = df["close"] - df["open"]
    body_abs = body.abs()
    df["fvg_body_atr"] = body_abs / atr_safe
    df["fvg_body_percentile"] = _rolling_percentile(
        df["fvg_body_atr"], params.body_percentile_window
    )
    df["fvg_volume_z"] = _zscore(df["volume"], params.volume_z_window)
    df["fvg_prior_high"] = prior_high(df["high"], params.structure_window)
    df["fvg_prior_low"] = prior_low(df["low"], params.structure_window)

    df["fvg_bull_raw"] = (
        (df["low"] > df["high"].shift(2))
        & (df["fvg_body_atr"].shift(1) >= params.fvg_displacement_atr)
        & (df["close"].shift(1) > df["open"].shift(1))
    )
    df["fvg_bear_raw"] = (
        (df["high"] < df["low"].shift(2))
        & (df["fvg_body_atr"].shift(1) >= params.fvg_displacement_atr)
        & (df["close"].shift(1) < df["open"].shift(1))
    )
    (
        df["active_bull_fvg_low"],
        df["active_bull_fvg_high"],
        df["active_bull_fvg_age"],
    ) = _active_fvg_zone(df, "long", params)
    (
        df["active_bear_fvg_low"],
        df["active_bear_fvg_high"],
        df["active_bear_fvg_age"],
    ) = _active_fvg_zone(df, "short", params)

    df["fvg_retest_long"] = (
        (df["low"] <= df["active_bull_fvg_high"])
        & (df["close"] >= df["active_bull_fvg_low"])
        & (df["close"] > df["open"])
        & (df["active_bull_fvg_age"] > 0)
    )
    df["fvg_retest_short"] = (
        (df["high"] >= df["active_bear_fvg_low"])
        & (df["close"] <= df["active_bear_fvg_high"])
        & (df["close"] < df["open"])
        & (df["active_bear_fvg_age"] > 0)
    )
    df["sweep_reclaim_long"] = (
        (df["low"] < df["fvg_prior_low"])
        & (df["close"] > df["fvg_prior_low"])
        & (df["close"] > df["open"])
    )
    df["sweep_reclaim_short"] = (
        (df["high"] > df["fvg_prior_high"])
        & (df["close"] < df["fvg_prior_high"])
        & (df["close"] < df["open"])
    )
    df["structure_break_long"] = df["close"] > df["fvg_prior_high"]
    df["structure_break_short"] = df["close"] < df["fvg_prior_low"]
    df["displacement_long"] = _displacement(df, body, "long", params)
    df["displacement_short"] = _displacement(df, body, "short", params)

    df = _merge_context(df, _context_frame(df, "15min", params, prefix="confirm_15m"))
    df = _merge_context(df, _context_frame(df, "1h", params, prefix="bias_1h"))
    df["confirm_15m_long"] = (
        (df["confirm_15m_close"] > df["confirm_15m_ema_fast"])
        & (df["confirm_15m_ema_fast"] >= df["confirm_15m_ema_slow"])
        & (df["confirm_15m_er"] >= params.min_15m_er)
    )
    df["confirm_15m_short"] = (
        (df["confirm_15m_close"] < df["confirm_15m_ema_fast"])
        & (df["confirm_15m_ema_fast"] <= df["confirm_15m_ema_slow"])
        & (df["confirm_15m_er"] >= params.min_15m_er)
    )
    df["bias_1h_long"] = (
        (df["bias_1h_close"] > df["bias_1h_ema_fast"])
        & (df["bias_1h_ema_fast"] >= df["bias_1h_ema_slow"])
        & (df["bias_1h_er"] >= params.min_1h_er)
    )
    df["bias_1h_short"] = (
        (df["bias_1h_close"] < df["bias_1h_ema_fast"])
        & (df["bias_1h_ema_fast"] <= df["bias_1h_ema_slow"])
        & (df["bias_1h_er"] >= params.min_1h_er)
    )

    df["room_to_liquidity_bps_long"] = (
        (df["fvg_prior_high"] - df["close"]) / df["close"] * 10_000.0
    )
    df["room_to_liquidity_bps_short"] = (
        (df["close"] - df["fvg_prior_low"]) / df["close"] * 10_000.0
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


class FvgLiquidityBreakoutScanner(BaseStrategy):
    strategy_id = FVG_LIQUIDITY_BREAKOUT_ID

    def __init__(
        self,
        funding: pd.DataFrame | None = None,
        *,
        params: FvgLiquidityBreakoutParams | None = None,
        allowed_sides: tuple[str, ...] | list[str] | None = None,
        min_expected_net_edge_bps: float | None = None,
        min_room_to_liquidity_bps: float | None = None,
        min_volume_z: float | None = None,
        min_body_atr: float | None = None,
        min_body_percentile: float | None = None,
    ) -> None:
        base = params or FvgLiquidityBreakoutParams()
        self.params = FvgLiquidityBreakoutParams(
            atr_window=base.atr_window,
            ema_window=base.ema_window,
            fvg_displacement_atr=base.fvg_displacement_atr,
            fvg_ttl_bars=base.fvg_ttl_bars,
            fvg_invalidation_atr=base.fvg_invalidation_atr,
            volume_z_window=base.volume_z_window,
            body_percentile_window=base.body_percentile_window,
            structure_window=base.structure_window,
            min_body_atr=base.min_body_atr if min_body_atr is None else min_body_atr,
            min_body_percentile=(
                base.min_body_percentile if min_body_percentile is None else min_body_percentile
            ),
            min_volume_z=base.min_volume_z if min_volume_z is None else min_volume_z,
            min_room_to_liquidity_bps=(
                base.min_room_to_liquidity_bps
                if min_room_to_liquidity_bps is None
                else min_room_to_liquidity_bps
            ),
            confirm_ema_fast=base.confirm_ema_fast,
            confirm_ema_slow=base.confirm_ema_slow,
            confirm_er_window=base.confirm_er_window,
            min_15m_er=base.min_15m_er,
            bias_ema_fast=base.bias_ema_fast,
            bias_ema_slow=base.bias_ema_slow,
            bias_er_window=base.bias_er_window,
            min_1h_er=base.min_1h_er,
            stop_atr_mult=base.stop_atr_mult,
            stop_buffer_atr=base.stop_buffer_atr,
            min_stop_bps=base.min_stop_bps,
            take_profit_r=base.take_profit_r,
            min_expected_net_edge_bps=(
                base.min_expected_net_edge_bps
                if min_expected_net_edge_bps is None
                else min_expected_net_edge_bps
            ),
            taker_entry_bps=base.taker_entry_bps,
            taker_exit_bps=base.taker_exit_bps,
            slippage_bps=base.slippage_bps,
            safety_buffer_bps=base.safety_buffer_bps,
            allowed_sides=_validate_sides(
                tuple(base.allowed_sides if allowed_sides is None else allowed_sides)
            ),
        )
        self.funding = funding
        self.warmup_bars = fvg_liquidity_breakout_warmup_bars(self.params)

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        return add_fvg_liquidity_breakout_columns(candles, self.params)

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        row = df.iloc[index]
        required = (
            "fvg_atr",
            "fvg_volume_z",
            "fvg_body_atr",
            "fvg_body_percentile",
            "confirm_15m_long",
            "confirm_15m_short",
            "bias_1h_long",
            "bias_1h_short",
            "stop_long",
            "stop_short",
            "target_long",
            "target_short",
            "expected_net_edge_bps_long",
            "expected_net_edge_bps_short",
        )
        if any(_is_nan(row.get(col)) for col in required):
            return None
        if self._ready(row, "long"):
            return SignalIntent(
                "long",
                stop_price=float(row["stop_long"]),
                take_profit_price=float(row["target_long"]),
                reason=self._reason("long", row),
            )
        if self._ready(row, "short"):
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
        if side not in FVG_LIQUIDITY_BREAKOUT_SIDES:
            return None
        row = df.iloc[index]
        if _is_nan(row.get("fvg_atr")):
            return None
        stop, target, tp1, tp2 = _exit_for_reference(side, float(entry_price), row, self.params)
        return SignalIntent(
            side,
            stop_price=stop,
            take_profit_price=target,
            reason=(
                "fvg_liquidity_breakout rebuilt structural exit; "
                f"tp_ladder={tp1:.6g}/{tp2:.6g}/{target:.6g}; "
                "trailing_stop_first; BE_after_TP1; smart_capture=TP1_or_trail"
            ),
        )

    def _ready(self, row: pd.Series, side: str) -> bool:
        return bool(
            self._side_allowed(side)
            and row[f"candidate_{side}"]
            and float(row[f"expected_net_edge_bps_{side}"]) >= self.params.min_expected_net_edge_bps
        )

    def _side_allowed(self, side: str) -> bool:
        return not self.params.allowed_sides or side in self.params.allowed_sides

    def _reason(self, side: str, row: pd.Series) -> str:
        edge = float(row[f"expected_net_edge_bps_{side}"])
        gross = float(row[f"expected_gross_bps_{side}"])
        events = _active_events(side, row)
        zone = _zone_text(side, row)
        return (
            f"fvg_liquidity_breakout {side}; mtf=5m_trigger/15m_setup/1h_bias; "
            f"events={','.join(events) or 'none'}; zone={zone}; "
            f"bodyATR={float(row['fvg_body_atr']):.2f}; "
            f"bodyPct={float(row['fvg_body_percentile']):.2f}; "
            f"volZ={float(row['fvg_volume_z']):+.2f}; "
            f"roomToLiquidity={float(row[f'room_to_liquidity_bps_{side}']):.1f}; "
            f"grossRoom={gross:.1f}; expectedEdge={edge:.1f}; "
            f"fillProbability={float(row[f'fill_probability_{side}']):.2f}; "
            f"takerCost={self.params.taker_round_trip_cost_bps:.1f}; "
            f"tp_ladder={float(row[f'tp1_{side}']):.6g}/"
            f"{float(row[f'tp2_{side}']):.6g}/{float(row[f'target_{side}']):.6g}; "
            "trailing_stop_first; BE_after_TP1; smart_capture=TP1_or_trail"
        )


def _active_fvg_zone(
    df: pd.DataFrame,
    side: str,
    params: FvgLiquidityBreakoutParams,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    lows: list[float] = []
    highs: list[float] = []
    ages: list[float] = []
    active_low = float("nan")
    active_high = float("nan")
    age = 0
    for i, row in df.iterrows():
        atr_value = float(row["fvg_atr"]) if not _is_nan(row.get("fvg_atr")) else float("nan")
        if side == "long" and bool(row.get("fvg_bull_raw", False)):
            active_low = float(df["high"].shift(2).loc[i])
            active_high = float(row["low"])
            age = 0
        elif side == "short" and bool(row.get("fvg_bear_raw", False)):
            active_low = float(row["high"])
            active_high = float(df["low"].shift(2).loc[i])
            age = 0
        elif not _is_nan(active_low) and not _is_nan(active_high):
            age += 1
            expired = age > params.fvg_ttl_bars
            invalidated = _invalidated(
                row, side, active_low, active_high, atr_value, params
            )
            if expired or invalidated:
                active_low = float("nan")
                active_high = float("nan")
                age = 0
        lows.append(active_low)
        highs.append(active_high)
        ages.append(float(age) if not _is_nan(active_low) else float("nan"))
    index = df.index
    return (
        pd.Series(lows, index=index, dtype="float64"),
        pd.Series(highs, index=index, dtype="float64"),
        pd.Series(ages, index=index, dtype="float64"),
    )


def _invalidated(
    row: pd.Series,
    side: str,
    zone_low: float,
    zone_high: float,
    atr_value: float,
    params: FvgLiquidityBreakoutParams,
) -> bool:
    buffer = 0.0 if _is_nan(atr_value) else params.fvg_invalidation_atr * atr_value
    if side == "long":
        return float(row["close"]) < zone_low - buffer
    return float(row["close"]) > zone_high + buffer


def _displacement(
    df: pd.DataFrame,
    body: pd.Series,
    side: str,
    params: FvgLiquidityBreakoutParams,
) -> pd.Series:
    direction_ok = body > 0.0 if side == "long" else body < 0.0
    return (
        direction_ok
        & (df["fvg_body_atr"] >= params.min_body_atr)
        & (df["fvg_body_percentile"] >= params.min_body_percentile)
        & (df["fvg_volume_z"] >= params.min_volume_z)
    ).fillna(False)


def _candidate_side(
    df: pd.DataFrame,
    side: str,
    params: FvgLiquidityBreakoutParams,
) -> pd.Series:
    return (
        df[f"confirm_15m_{side}"]
        & df[f"bias_1h_{side}"]
        & df[f"displacement_{side}"]
        & (df[f"fvg_retest_{side}"] | df[f"sweep_reclaim_{side}"] | df[f"structure_break_{side}"])
        & (df[f"room_to_liquidity_bps_{side}"] >= params.min_room_to_liquidity_bps)
        & (df[f"fill_probability_{side}"] >= 0.25)
    ).fillna(False)


def _context_frame(
    df: pd.DataFrame,
    rule: str,
    params: FvgLiquidityBreakoutParams,
    *,
    prefix: str,
) -> pd.DataFrame:
    columns = [
        "timestamp",
        f"{prefix}_close",
        f"{prefix}_ema_fast",
        f"{prefix}_ema_slow",
        f"{prefix}_er",
    ]
    htf = _resample_completed(df, rule)
    if htf.empty:
        return pd.DataFrame(
            {
                "timestamp": pd.Series(dtype="datetime64[ns, UTC]"),
                **{col: pd.Series(dtype="float64") for col in columns[1:]},
            }
        )
    if prefix == "confirm_15m":
        ema_fast_window = params.confirm_ema_fast
        ema_slow_window = params.confirm_ema_slow
        er_window = params.confirm_er_window
    else:
        ema_fast_window = params.bias_ema_fast
        ema_slow_window = params.bias_ema_slow
        er_window = params.bias_er_window
    htf[f"{prefix}_ema_fast"] = ema(htf["close"], ema_fast_window)
    htf[f"{prefix}_ema_slow"] = ema(htf["close"], ema_slow_window)
    htf[f"{prefix}_er"] = efficiency_ratio(htf["close"], er_window)
    return htf[
        ["timestamp", "close", f"{prefix}_ema_fast", f"{prefix}_ema_slow", f"{prefix}_er"]
    ].rename(columns={"close": f"{prefix}_close"})[columns]


def _resample_completed(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    if df.empty:
        return df.copy()
    src = df[["timestamp", "open", "high", "low", "close", "volume"]].copy()
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
    complete_offset = max(pd.Timedelta(rule) - _base_delta(df["timestamp"]), pd.Timedelta(0))
    htf = htf.reset_index()
    htf["timestamp"] = htf["timestamp"] + complete_offset
    return htf.reset_index(drop=True)


def _merge_context(df: pd.DataFrame, context: pd.DataFrame) -> pd.DataFrame:
    if context.empty:
        return df
    left = df.sort_values("timestamp").copy()
    right = context.sort_values("timestamp").copy()
    left["timestamp"] = _utc_ns(left["timestamp"])
    right["timestamp"] = _utc_ns(right["timestamp"])
    merged = pd.merge_asof(left, right, on="timestamp", direction="backward")
    return merged.sort_index()


def _exit_and_edge_columns(
    df: pd.DataFrame,
    side: str,
    params: FvgLiquidityBreakoutParams,
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series, pd.Series, pd.Series, pd.Series]:
    stops: list[float] = []
    targets: list[float] = []
    tp1s: list[float] = []
    tp2s: list[float] = []
    gross_bps: list[float] = []
    net_bps: list[float] = []
    fills: list[float] = []
    for _, row in df.iterrows():
        if _is_nan(row.get("fvg_atr")):
            stops.append(float("nan"))
            targets.append(float("nan"))
            tp1s.append(float("nan"))
            tp2s.append(float("nan"))
            gross_bps.append(float("nan"))
            net_bps.append(float("nan"))
            fills.append(float("nan"))
            continue
        close = float(row["close"])
        stop, target, tp1, tp2 = _exit_for_reference(side, close, row, params)
        full_gross = abs(target - close) / close * 10_000.0
        room = max(0.0, float(row.get(f"room_to_liquidity_bps_{side}", 0.0)))
        executable_gross = min(full_gross, room) if room > 0.0 else full_gross
        quality = _quality_score(side, row)
        expected_gross = executable_gross * quality
        stops.append(stop)
        targets.append(target)
        tp1s.append(tp1)
        tp2s.append(tp2)
        gross_bps.append(executable_gross)
        net_bps.append(expected_gross - params.taker_round_trip_cost_bps)
        fills.append(_fill_probability(side, row))
    index = df.index
    return (
        pd.Series(stops, index=index, dtype="float64"),
        pd.Series(targets, index=index, dtype="float64"),
        pd.Series(tp1s, index=index, dtype="float64"),
        pd.Series(tp2s, index=index, dtype="float64"),
        pd.Series(gross_bps, index=index, dtype="float64"),
        pd.Series(net_bps, index=index, dtype="float64"),
        pd.Series(fills, index=index, dtype="float64"),
    )


def _exit_for_reference(
    side: str,
    reference_price: float,
    row: pd.Series,
    params: FvgLiquidityBreakoutParams,
) -> tuple[float, float, float, float]:
    atr_value = float(row["fvg_atr"])
    min_stop = reference_price * params.min_stop_bps / 10_000.0
    atr_stop = max(params.stop_atr_mult * atr_value, min_stop)
    if side == "long":
        candidates = [reference_price - atr_stop]
        if not _is_nan(row.get("active_bull_fvg_low")):
            candidates.append(
                float(row["active_bull_fvg_low"]) - params.stop_buffer_atr * atr_value
            )
        if not _is_nan(row.get("fvg_prior_low")):
            candidates.append(float(row["fvg_prior_low"]) - params.stop_buffer_atr * atr_value)
        valid = [candidate for candidate in candidates if 0.0 < candidate < reference_price]
        stop = max(valid) if valid else reference_price - atr_stop
        if reference_price - stop < min_stop:
            stop = reference_price - min_stop
        risk = reference_price - stop
        tp1 = reference_price + risk
        tp2 = reference_price + 2.0 * risk
        target = reference_price + params.take_profit_r * risk
    else:
        candidates = [reference_price + atr_stop]
        if not _is_nan(row.get("active_bear_fvg_high")):
            candidates.append(
                float(row["active_bear_fvg_high"]) + params.stop_buffer_atr * atr_value
            )
        if not _is_nan(row.get("fvg_prior_high")):
            candidates.append(float(row["fvg_prior_high"]) + params.stop_buffer_atr * atr_value)
        valid = [candidate for candidate in candidates if candidate > reference_price]
        stop = min(valid) if valid else reference_price + atr_stop
        if stop - reference_price < min_stop:
            stop = reference_price + min_stop
        risk = stop - reference_price
        tp1 = reference_price - risk
        tp2 = reference_price - 2.0 * risk
        target = reference_price - params.take_profit_r * risk
    return stop, target, tp1, tp2


def _quality_score(side: str, row: pd.Series) -> float:
    structure = 0.20 if bool(row.get(f"fvg_retest_{side}", False)) else 0.0
    structure += 0.12 if bool(row.get(f"sweep_reclaim_{side}", False)) else 0.0
    structure += 0.08 if bool(row.get(f"structure_break_{side}", False)) else 0.0
    body = min(1.0, max(0.0, float(row.get("fvg_body_atr", 0.0)) / 1.5))
    volume = min(1.0, max(0.0, (float(row.get("fvg_volume_z", 0.0)) + 0.5) / 2.5))
    percentile = min(1.0, max(0.0, float(row.get("fvg_body_percentile", 0.0))))
    return min(0.92, max(0.25, 0.28 + structure + 0.18 * body + 0.14 * volume + 0.10 * percentile))


def _fill_probability(side: str, row: pd.Series) -> float:
    base = 0.25
    if bool(row.get(f"fvg_retest_{side}", False)):
        base += 0.18
    if bool(row.get(f"sweep_reclaim_{side}", False)):
        base += 0.12
    if bool(row.get(f"structure_break_{side}", False)):
        base += 0.08
    base += min(0.12, max(0.0, float(row.get("fvg_volume_z", 0.0)) * 0.04))
    return round(min(0.85, max(0.20, base)), 4)


def _active_events(side: str, row: pd.Series) -> list[str]:
    checks = (
        ("fvg_retest", f"fvg_retest_{side}"),
        ("sweep_reclaim", f"sweep_reclaim_{side}"),
        ("structure_break", f"structure_break_{side}"),
    )
    return [label for label, col in checks if bool(row.get(col, False))]


def _zone_text(side: str, row: pd.Series) -> str:
    if side == "long":
        low = row.get("active_bull_fvg_low")
        high = row.get("active_bull_fvg_high")
        age = row.get("active_bull_fvg_age")
    else:
        low = row.get("active_bear_fvg_low")
        high = row.get("active_bear_fvg_high")
        age = row.get("active_bear_fvg_age")
    if _is_nan(low) or _is_nan(high):
        return "none"
    return f"{float(low):.6g}-{float(high):.6g}@{int(float(age))}bars"


def _rolling_percentile(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window).apply(
        lambda w: (w < w[-1]).mean() + 0.5 * (w == w[-1]).mean(), raw=True
    )


def _zscore(series: pd.Series, window: int) -> pd.Series:
    mean = series.rolling(window).mean()
    std = series.rolling(window).std()
    return (series - mean) / std.replace(0.0, float("nan"))


def _base_delta(timestamps: pd.Series) -> pd.Timedelta:
    deltas = timestamps.sort_values().diff().dropna()
    if deltas.empty:
        return pd.Timedelta(minutes=5)
    return deltas.median()


def _utc_ns(values: pd.Series) -> pd.Series:
    return pd.Series(
        pd.to_datetime(values, utc=True).astype("datetime64[ns, UTC]"),
        index=values.index,
    )


def _validate_sides(values: tuple[str, ...]) -> tuple[str, ...]:
    unknown = sorted(set(values) - set(FVG_LIQUIDITY_BREAKOUT_SIDES))
    if unknown:
        raise ValueError(f"allowed_sides contains unknown values: {unknown}")
    return values


def _is_nan(value: Any) -> bool:
    try:
        return math.isnan(float(value))
    except (TypeError, ValueError):
        return True
