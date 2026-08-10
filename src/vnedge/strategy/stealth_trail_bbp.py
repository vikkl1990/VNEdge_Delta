"""Human trade fingerprint scanner: HTF bias + stealth trail + BBP pressure.

This scanner models the observable parts of the operator's 5m chart workflow:
1h bias, 15m confirmation, 5m trigger, bull/bear power pressure, a SuperTrend-
style trail, displacement, volume impulse, and market-structure events. It is a
VNEDGE-native implementation from OHLCV only; it does not copy any proprietary
TradingView/Pine source. Signals remain research/shadow candidates until the
normal promotion machinery proves them on untouched data.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

import pandas as pd

from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent
from vnedge.strategy.indicators import atr, efficiency_ratio, ema, prior_high, prior_low


STEALTH_TRAIL_BBP_ID = "stealth_trail_bbp_v1"
HUMAN_TRADE_FINGERPRINT_ID = "human_trade_fingerprint_v1"
STEALTH_TRAIL_BBP_SIDES: tuple[str, ...] = ("long", "short")


@dataclass(frozen=True)
class StealthTrailBBPParams:
    """Frozen scanner parameters.

    The defaults are intentionally fee-aware: a taker fallback must clear the
    modeled round-trip cost and still leave at least 25 bps of expected net room.
    """

    ema_window: int = 13
    atr_window: int = 14
    bbp_z_window: int = 30
    bbp_slope_window: int = 3
    volume_z_window: int = 30
    displacement_pct_window: int = 100
    structure_window: int = 20
    stealth_trail_atr_mult: float = 2.5
    stealth_trail_reclaim_atr: float = 0.20
    rejection_wick_body: float = 1.20

    confirm_ema_fast: int = 8
    confirm_ema_slow: int = 21
    confirm_er_window: int = 10
    confirm_adx_window: int = 10
    min_15m_er: float = 0.08

    bias_ema_fast: int = 13
    bias_ema_slow: int = 34
    bias_er_window: int = 12
    bias_adx_window: int = 14
    min_1h_er: float = 0.12
    min_1h_adx: float = 12.0

    min_bbp_z: float = 0.20
    min_bbp_slope: float = 0.00
    min_volume_z: float = 0.40
    min_body_atr: float = 0.45
    min_body_percentile: float = 0.60
    min_expected_net_edge_bps: float = 25.0

    stop_atr_mult: float = 1.00
    stop_buffer_atr: float = 0.08
    min_stop_bps: float = 12.0
    take_profit_r: float = 3.0

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


@dataclass(frozen=True)
class StealthTrailBBPPromotionGate:
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


STEALTH_TRAIL_BBP_PROMOTION_GATE = StealthTrailBBPPromotionGate()


def stealth_trail_bbp_warmup_bars(params: StealthTrailBBPParams) -> int:
    one_hour_bars = max(
        params.bias_ema_slow,
        params.bias_er_window,
        params.bias_adx_window * 2,
        params.atr_window,
    ) * 12
    fifteen_minute_bars = max(
        params.confirm_ema_slow,
        params.confirm_er_window,
        params.confirm_adx_window * 2,
        params.atr_window,
    ) * 3
    local_bars = max(
        params.ema_window,
        params.atr_window,
        params.bbp_z_window + params.bbp_slope_window,
        params.volume_z_window,
        params.displacement_pct_window,
        params.structure_window + 1,
    )
    return max(one_hour_bars, fifteen_minute_bars, local_bars) + 2


def add_stealth_trail_bbp_columns(
    candles: pd.DataFrame,
    params: StealthTrailBBPParams = StealthTrailBBPParams(),
) -> pd.DataFrame:
    df = candles.copy()
    df["atr_5m"] = atr(df, params.atr_window)
    atr_safe = df["atr_5m"].replace(0.0, float("nan"))
    df["ema13"] = ema(df["close"], params.ema_window)

    df["bull_power"] = df["high"] - df["ema13"]
    df["bear_power"] = df["low"] - df["ema13"]
    df["bbp_hist"] = df["bull_power"] + df["bear_power"]
    df["bbp_hist_atr"] = df["bbp_hist"] / atr_safe
    df["bbp_hist_slope"] = df["bbp_hist_atr"].diff(params.bbp_slope_window)
    df["bbp_hist_z"] = _zscore(df["bbp_hist_atr"], params.bbp_z_window)

    (
        df["stealth_trail"],
        df["stealth_trend"],
        df["stealth_upper_band"],
        df["stealth_lower_band"],
    ) = _stealth_trail(df, params)

    body = df["close"] - df["open"]
    body_abs = body.abs()
    upper_wick = df["high"] - df[["open", "close"]].max(axis=1)
    lower_wick = df[["open", "close"]].min(axis=1) - df["low"]
    df["body_atr"] = body_abs / atr_safe
    df["body_percentile"] = _rolling_percentile(df["body_atr"], params.displacement_pct_window)
    df["volume_z"] = _zscore(df["volume"], params.volume_z_window)

    df["prior_high"] = prior_high(df["high"], params.structure_window)
    df["prior_low"] = prior_low(df["low"], params.structure_window)
    df["structure_break_long"] = df["close"] > df["prior_high"]
    df["structure_break_short"] = df["close"] < df["prior_low"]
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
    df["rejection_long"] = (
        (lower_wick >= params.rejection_wick_body * body_abs.clip(lower=1e-12))
        & (df["close"] > df["open"])
        & (
            (df["low"] <= df["ema13"])
            | (df["low"] <= df["stealth_trail"] + params.stealth_trail_reclaim_atr * df["atr_5m"])
        )
    )
    df["rejection_short"] = (
        (upper_wick >= params.rejection_wick_body * body_abs.clip(lower=1e-12))
        & (df["close"] < df["open"])
        & (
            (df["high"] >= df["ema13"])
            | (df["high"] >= df["stealth_trail"] - params.stealth_trail_reclaim_atr * df["atr_5m"])
        )
    )
    df["choch_long"] = df["structure_break_long"] & (df["stealth_trend"].shift(1) < 0)
    df["choch_short"] = df["structure_break_short"] & (df["stealth_trend"].shift(1) > 0)

    df["displacement_long"] = (
        (body > 0.0)
        & (df["body_atr"] >= params.min_body_atr)
        & (df["body_percentile"] >= params.min_body_percentile)
    )
    df["displacement_short"] = (
        (body < 0.0)
        & (df["body_atr"] >= params.min_body_atr)
        & (df["body_percentile"] >= params.min_body_percentile)
    )
    df["structure_event_long"] = (
        df["structure_break_long"] | df["sweep_long"] | df["rejection_long"] | df["choch_long"]
    )
    df["structure_event_short"] = (
        df["structure_break_short"] | df["sweep_short"] | df["rejection_short"] | df["choch_short"]
    )

    df = _merge_context(df, _context_frame(df, "15min", params, prefix="confirm_15m"))
    df = _merge_context(df, _context_frame(df, "1h", params, prefix="bias_1h"))

    df["confirm_15m_long"] = (
        (df["confirm_15m_close"] > df["confirm_15m_ema_fast"])
        & (df["confirm_15m_ema_fast"] >= df["confirm_15m_ema_slow"])
        & (df["confirm_15m_bbp_hist_atr"] > 0.0)
        & (df["confirm_15m_stealth_trend"] > 0)
        & (df["confirm_15m_er"] >= params.min_15m_er)
    )
    df["confirm_15m_short"] = (
        (df["confirm_15m_close"] < df["confirm_15m_ema_fast"])
        & (df["confirm_15m_ema_fast"] <= df["confirm_15m_ema_slow"])
        & (df["confirm_15m_bbp_hist_atr"] < 0.0)
        & (df["confirm_15m_stealth_trend"] < 0)
        & (df["confirm_15m_er"] >= params.min_15m_er)
    )
    df["bias_1h_long"] = (
        (df["bias_1h_close"] > df["bias_1h_ema_fast"])
        & (df["bias_1h_ema_fast"] >= df["bias_1h_ema_slow"])
        & (df["bias_1h_er"] >= params.min_1h_er)
        & (df["bias_1h_adx"] >= params.min_1h_adx)
    )
    df["bias_1h_short"] = (
        (df["bias_1h_close"] < df["bias_1h_ema_fast"])
        & (df["bias_1h_ema_fast"] <= df["bias_1h_ema_slow"])
        & (df["bias_1h_er"] >= params.min_1h_er)
        & (df["bias_1h_adx"] >= params.min_1h_adx)
    )

    (
        df["stop_long"],
        df["target_long"],
        df["expected_gross_bps_long"],
        df["expected_net_edge_bps_long"],
    ) = _exit_columns(df, "long", params)
    (
        df["stop_short"],
        df["target_short"],
        df["expected_gross_bps_short"],
        df["expected_net_edge_bps_short"],
    ) = _exit_columns(df, "short", params)
    return df


class StealthTrailBBPScanner(BaseStrategy):
    strategy_id = STEALTH_TRAIL_BBP_ID

    def __init__(
        self,
        funding: pd.DataFrame | None = None,
        *,
        params: StealthTrailBBPParams | None = None,
        allowed_sides: tuple[str, ...] | list[str] | None = None,
        min_expected_net_edge_bps: float | None = None,
        min_bbp_z: float | None = None,
        min_volume_z: float | None = None,
        min_body_atr: float | None = None,
        min_body_percentile: float | None = None,
    ) -> None:
        base = params or StealthTrailBBPParams()
        self.params = StealthTrailBBPParams(
            ema_window=base.ema_window,
            atr_window=base.atr_window,
            bbp_z_window=base.bbp_z_window,
            bbp_slope_window=base.bbp_slope_window,
            volume_z_window=base.volume_z_window,
            displacement_pct_window=base.displacement_pct_window,
            structure_window=base.structure_window,
            stealth_trail_atr_mult=base.stealth_trail_atr_mult,
            stealth_trail_reclaim_atr=base.stealth_trail_reclaim_atr,
            rejection_wick_body=base.rejection_wick_body,
            confirm_ema_fast=base.confirm_ema_fast,
            confirm_ema_slow=base.confirm_ema_slow,
            confirm_er_window=base.confirm_er_window,
            confirm_adx_window=base.confirm_adx_window,
            min_15m_er=base.min_15m_er,
            bias_ema_fast=base.bias_ema_fast,
            bias_ema_slow=base.bias_ema_slow,
            bias_er_window=base.bias_er_window,
            bias_adx_window=base.bias_adx_window,
            min_1h_er=base.min_1h_er,
            min_1h_adx=base.min_1h_adx,
            min_bbp_z=base.min_bbp_z if min_bbp_z is None else min_bbp_z,
            min_bbp_slope=base.min_bbp_slope,
            min_volume_z=base.min_volume_z if min_volume_z is None else min_volume_z,
            min_body_atr=base.min_body_atr if min_body_atr is None else min_body_atr,
            min_body_percentile=(
                base.min_body_percentile if min_body_percentile is None else min_body_percentile
            ),
            min_expected_net_edge_bps=(
                base.min_expected_net_edge_bps
                if min_expected_net_edge_bps is None
                else min_expected_net_edge_bps
            ),
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
        self.warmup_bars = stealth_trail_bbp_warmup_bars(self.params)

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        return add_stealth_trail_bbp_columns(candles, self.params)

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        row = df.iloc[index]
        required = (
            "atr_5m",
            "bbp_hist_z",
            "bbp_hist_slope",
            "stealth_trail",
            "stealth_trend",
            "volume_z",
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
        if any(_is_nan(row[col]) for col in required):
            return None
        if self._long_ready(row):
            return SignalIntent(
                "long",
                stop_price=float(row["stop_long"]),
                take_profit_price=float(row["target_long"]),
                reason=self._reason("long", row),
            )
        if self._short_ready(row):
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
        row = df.iloc[index]
        if side not in STEALTH_TRAIL_BBP_SIDES or _is_nan(row.get("atr_5m")):
            return None
        stop, target = _exit_for_reference(side, float(entry_price), row, self.params)
        return SignalIntent(
            side, stop_price=stop, take_profit_price=target,
            reason="stealth_trail_bbp rebuilt structural trail plan; TP1/TP2/TP3/BE policy",
        )

    def _long_ready(self, row: pd.Series) -> bool:
        return bool(
            self._side_allowed("long")
            and row["bias_1h_long"]
            and row["confirm_15m_long"]
            and float(row["stealth_trend"]) > 0
            and float(row["bbp_hist_z"]) >= self.params.min_bbp_z
            and float(row["bbp_hist_slope"]) >= self.params.min_bbp_slope
            and float(row["volume_z"]) >= self.params.min_volume_z
            and row["displacement_long"]
            and row["structure_event_long"]
            and float(row["expected_net_edge_bps_long"]) >= self.params.min_expected_net_edge_bps
        )

    def _short_ready(self, row: pd.Series) -> bool:
        return bool(
            self._side_allowed("short")
            and row["bias_1h_short"]
            and row["confirm_15m_short"]
            and float(row["stealth_trend"]) < 0
            and float(row["bbp_hist_z"]) <= -self.params.min_bbp_z
            and float(row["bbp_hist_slope"]) <= -self.params.min_bbp_slope
            and float(row["volume_z"]) >= self.params.min_volume_z
            and row["displacement_short"]
            and row["structure_event_short"]
            and float(row["expected_net_edge_bps_short"]) >= self.params.min_expected_net_edge_bps
        )

    def _side_allowed(self, side: str) -> bool:
        return not self.params.allowed_sides or side in self.params.allowed_sides

    def _reason(self, side: str, row: pd.Series) -> str:
        close = float(row["close"])
        stop = float(row[f"stop_{side}"])
        target = float(row[f"target_{side}"])
        risk = abs(close - stop)
        tp1 = close + risk if side == "long" else close - risk
        tp2 = close + 2.0 * risk if side == "long" else close - 2.0 * risk
        events = _active_events(side, row)
        edge = float(row[f"expected_net_edge_bps_{side}"])
        gross = float(row[f"expected_gross_bps_{side}"])
        return (
            f"stealth_trail_bbp {side}; mtf=5m_trigger/15m_confirm/1h_bias; "
            f"events={','.join(events) or 'none'}; BBPz={float(row['bbp_hist_z']):+.2f}; "
            f"BBPslope={float(row['bbp_hist_slope']):+.2f}; volZ={float(row['volume_z']):+.2f}; "
            f"bodyATR={float(row['body_atr']):.2f}; trail={float(row['stealth_trail']):.6g}; "
            f"grossRoom={gross:.1f}bps; expectedNet={edge:.1f}bps; "
            f"takerCost={self.params.taker_round_trip_cost_bps:.1f}bps; "
            f"takerFallback={'allowed' if edge >= self.params.min_expected_net_edge_bps else 'blocked'}; "
            f"tp_ladder={tp1:.6g}/{tp2:.6g}/{target:.6g}; BE_after_TP1"
        )


class HumanTradeFingerprintScanner(StealthTrailBBPScanner):
    strategy_id = HUMAN_TRADE_FINGERPRINT_ID


def _context_frame(
    df: pd.DataFrame,
    rule: str,
    params: StealthTrailBBPParams,
    *,
    prefix: str,
) -> pd.DataFrame:
    columns = [
        "timestamp",
        f"{prefix}_close",
        f"{prefix}_atr",
        f"{prefix}_ema_fast",
        f"{prefix}_ema_slow",
        f"{prefix}_er",
        f"{prefix}_adx",
        f"{prefix}_bbp_hist_atr",
        f"{prefix}_stealth_trend",
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
        adx_window = params.confirm_adx_window
    else:
        ema_fast_window = params.bias_ema_fast
        ema_slow_window = params.bias_ema_slow
        er_window = params.bias_er_window
        adx_window = params.bias_adx_window

    htf[f"{prefix}_atr"] = atr(htf, params.atr_window)
    htf[f"{prefix}_ema_fast"] = ema(htf["close"], ema_fast_window)
    htf[f"{prefix}_ema_slow"] = ema(htf["close"], ema_slow_window)
    htf[f"{prefix}_er"] = efficiency_ratio(htf["close"], er_window)
    htf[f"{prefix}_adx"] = _adx(htf, adx_window)
    pressure_ema = ema(htf["close"], params.ema_window)
    atr_safe = htf[f"{prefix}_atr"].replace(0.0, float("nan"))
    htf[f"{prefix}_bbp_hist_atr"] = ((htf["high"] - pressure_ema) + (htf["low"] - pressure_ema)) / atr_safe
    htf[f"{prefix}_stealth_trend"] = _stealth_trail(htf, params)[1]
    return htf[
        [
            "timestamp",
            "close",
            f"{prefix}_atr",
            f"{prefix}_ema_fast",
            f"{prefix}_ema_slow",
            f"{prefix}_er",
            f"{prefix}_adx",
            f"{prefix}_bbp_hist_atr",
            f"{prefix}_stealth_trend",
        ]
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
    base_delta = _base_delta(df["timestamp"])
    complete_offset = pd.Timedelta(rule) - base_delta
    complete_offset = max(complete_offset, pd.Timedelta(0))
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
    merged = pd.merge_asof(
        left,
        right,
        on="timestamp",
        direction="backward",
    )
    return merged.sort_index()


def _exit_columns(
    df: pd.DataFrame,
    side: str,
    params: StealthTrailBBPParams,
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    stops: list[float] = []
    targets: list[float] = []
    gross_bps: list[float] = []
    net_bps: list[float] = []
    for _, row in df.iterrows():
        if _is_nan(row.get("atr_5m")):
            stops.append(float("nan"))
            targets.append(float("nan"))
            gross_bps.append(float("nan"))
            net_bps.append(float("nan"))
            continue
        close = float(row["close"])
        stop, target = _exit_for_reference(side, close, row, params)
        if side == "long":
            gross = max((target - close) / close * 10_000.0, 0.0)
        else:
            gross = max((close - target) / close * 10_000.0, 0.0)
        stops.append(stop)
        targets.append(target)
        gross_bps.append(gross)
        net_bps.append(gross - params.taker_round_trip_cost_bps)
    index = df.index
    return (
        pd.Series(stops, index=index, dtype="float64"),
        pd.Series(targets, index=index, dtype="float64"),
        pd.Series(gross_bps, index=index, dtype="float64"),
        pd.Series(net_bps, index=index, dtype="float64"),
    )


def _exit_for_reference(
    side: str,
    reference_price: float,
    row: pd.Series,
    params: StealthTrailBBPParams,
) -> tuple[float, float]:
    atr_value = float(row["atr_5m"])
    min_stop = reference_price * params.min_stop_bps / 10_000.0
    atr_stop = params.stop_atr_mult * atr_value
    if side == "long":
        candidates = [reference_price - atr_stop]
        if not _is_nan(row.get("prior_low")):
            candidates.append(float(row["prior_low"]) - params.stop_buffer_atr * atr_value)
        if not _is_nan(row.get("stealth_trail")) and float(row["stealth_trail"]) < reference_price:
            candidates.append(float(row["stealth_trail"]) - params.stop_buffer_atr * atr_value)
        valid_candidates = [
            candidate for candidate in candidates if 0.0 < candidate < reference_price
        ]
        stop = (
            max(valid_candidates)
            if valid_candidates
            else _fallback_stop(side, reference_price, min_stop, atr_stop)
        )
        if reference_price - stop < min_stop:
            stop = reference_price - min_stop
        risk = reference_price - stop
        tp3 = reference_price + params.take_profit_r * risk
        room_target = (
            float(row["prior_high"]) + params.stop_buffer_atr * atr_value
            if not _is_nan(row.get("prior_high")) and float(row["prior_high"]) > reference_price
            else tp3
        )
        target = min(tp3, room_target) if room_target > reference_price else tp3
    else:
        candidates = [reference_price + atr_stop]
        if not _is_nan(row.get("prior_high")):
            candidates.append(float(row["prior_high"]) + params.stop_buffer_atr * atr_value)
        if not _is_nan(row.get("stealth_trail")) and float(row["stealth_trail"]) > reference_price:
            candidates.append(float(row["stealth_trail"]) + params.stop_buffer_atr * atr_value)
        valid_candidates = [
            candidate for candidate in candidates if candidate > reference_price
        ]
        stop = (
            min(valid_candidates)
            if valid_candidates
            else _fallback_stop(side, reference_price, min_stop, atr_stop)
        )
        if stop - reference_price < min_stop:
            stop = reference_price + min_stop
        risk = stop - reference_price
        tp3 = reference_price - params.take_profit_r * risk
        room_target = (
            float(row["prior_low"]) - params.stop_buffer_atr * atr_value
            if not _is_nan(row.get("prior_low")) and float(row["prior_low"]) < reference_price
            else tp3
        )
        target = max(tp3, room_target) if room_target < reference_price else tp3
    return stop, target


def _fallback_stop(
    side: str,
    reference_price: float,
    min_stop: float,
    atr_stop: float,
) -> float:
    distance = max(min_stop, abs(atr_stop), reference_price * 1e-8)
    if side == "long":
        return max(reference_price - distance, reference_price * 1e-8)
    return reference_price + distance


def _stealth_trail(
    df: pd.DataFrame, params: StealthTrailBBPParams
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    hl2 = (df["high"] + df["low"]) / 2.0
    atr_col = "atr_5m" if "atr_5m" in df.columns else "confirm_15m_atr"
    if atr_col not in df.columns:
        atr_col = "bias_1h_atr" if "bias_1h_atr" in df.columns else "atr"
    atr_values = df[atr_col]
    basic_upper = hl2 + params.stealth_trail_atr_mult * atr_values
    basic_lower = hl2 - params.stealth_trail_atr_mult * atr_values

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
        upper_band = upper if upper < prev_upper or prev_close > prev_upper else prev_upper
        lower_band = lower if lower > prev_lower or prev_close < prev_lower else prev_lower
        if math.isnan(prev_upper) or math.isnan(prev_lower):
            upper_band = upper
            lower_band = lower
        prev_trend = trend[-1]
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


def _zscore(series: pd.Series, window: int) -> pd.Series:
    mean = series.rolling(window).mean()
    std = series.rolling(window).std()
    return (series - mean) / std.replace(0.0, float("nan"))


def _rolling_percentile(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window).apply(
        lambda w: (w < w[-1]).mean() + 0.5 * (w == w[-1]).mean(), raw=True
    )


def _base_delta(timestamps: pd.Series) -> pd.Timedelta:
    deltas = timestamps.sort_values().diff().dropna()
    if deltas.empty:
        return pd.Timedelta(minutes=5)
    return deltas.median()


def _active_events(side: str, row: pd.Series) -> list[str]:
    if side == "long":
        checks = (
            ("sweep", "sweep_long"),
            ("choch", "choch_long"),
            ("break", "structure_break_long"),
            ("rejection", "rejection_long"),
        )
    else:
        checks = (
            ("sweep", "sweep_short"),
            ("choch", "choch_short"),
            ("break", "structure_break_short"),
            ("rejection", "rejection_short"),
        )
    return [label for label, col in checks if bool(row.get(col, False))]


def _validate_sides(values: tuple[str, ...]) -> tuple[str, ...]:
    unknown = sorted(set(values) - set(STEALTH_TRAIL_BBP_SIDES))
    if unknown:
        raise ValueError(f"allowed_sides contains unknown values: {unknown}")
    return values


def _is_nan(value: Any) -> bool:
    try:
        return math.isnan(float(value))
    except (TypeError, ValueError):
        return True
