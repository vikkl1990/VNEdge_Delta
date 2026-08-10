"""Momentum Cascade scanner adapted for VNEDGE research.

The operator supplied a TradingView Momentum Cascade concept: one rate-of-
change impulse passed through two EMA stages, with a trend flip only when all
three stages agree.  This module keeps that causal idea, then wraps it in the
execution-aware pieces VNEDGE needs before a scanner can be studied:

- 15m trigger bars with completed 1h bias context,
- structural sweep/break/rejection context,
- volume/body impulse checks,
- structural stop, trailing-first exit plan, TP ladder metadata,
- expected-edge and fill-probability hints for the execution router.

Signals remain research-only until router, model, untouched-data judgment,
shadow, and paper gates prove them.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

import pandas as pd

from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent
from vnedge.strategy.indicators import (
    atr,
    efficiency_ratio,
    ema,
    prior_high,
    prior_low,
    rolling_percentile,
)


MOMENTUM_CASCADE_LYRO_ID = "momentum_cascade_lyro_v1"
MOMENTUM_CASCADE_LYRO_SIDES: tuple[str, ...] = ("long", "short")


@dataclass(frozen=True)
class MomentumCascadeLyroParams:
    """Frozen parameters for the Momentum Cascade scanner."""

    momentum_length: int = 14
    stage_smoothing: int = 14
    atr_window: int = 14
    volume_sma_window: int = 20
    body_percentile_window: int = 80
    structure_window: int = 20
    rejection_wick_body: float = 0.90

    htf_ema_fast: int = 13
    htf_ema_slow: int = 34
    htf_er_window: int = 12
    htf_adx_window: int = 14
    min_1h_er: float = 0.04
    min_1h_adx: float = 8.0

    min_m3_abs: float = 0.02
    min_m3_slope: float = 0.00
    min_coherence: float = 0.40
    min_volume_ratio: float = 0.55
    min_body_atr: float = 1.50
    min_body_percentile: float = 0.45
    min_confidence: float = 55.0
    min_expected_net_edge_bps: float = 80.0
    cooldown_bars: int = 4
    allow_continuations: bool = True
    max_continuation_trend_bars: int = 18

    stop_atr_mult: float = 1.15
    stop_buffer_atr: float = 0.08
    min_stop_bps: float = 12.0
    take_profit_r: float = 2.40

    taker_entry_bps: float = 5.0
    taker_exit_bps: float = 5.0
    slippage_bps: float = 2.0
    safety_buffer_bps: float = 5.0
    allowed_sides: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.momentum_length < 2:
            raise ValueError("momentum_length must be >= 2")
        if self.stage_smoothing < 2:
            raise ValueError("stage_smoothing must be >= 2")
        if self.cooldown_bars < 0:
            raise ValueError("cooldown_bars cannot be negative")
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


def momentum_cascade_lyro_warmup_bars(params: MomentumCascadeLyroParams) -> int:
    local = max(
        params.momentum_length + params.stage_smoothing * 2,
        params.atr_window,
        params.volume_sma_window,
        params.body_percentile_window,
        params.structure_window + 1,
    )
    one_hour = max(
        params.htf_ema_slow,
        params.htf_er_window,
        params.htf_adx_window * 2,
        params.momentum_length + params.stage_smoothing * 2,
    ) * 4
    return max(local, one_hour) + 2


def add_momentum_cascade_lyro_columns(
    candles: pd.DataFrame,
    params: MomentumCascadeLyroParams = MomentumCascadeLyroParams(),
) -> pd.DataFrame:
    df = candles.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

    df["atr"] = atr(df, params.atr_window)
    atr_safe = df["atr"].replace(0.0, float("nan"))
    df["roc_pct"] = df["close"].pct_change(params.momentum_length) * 100.0
    df["cascade_m1"] = df["roc_pct"]
    df["cascade_m2"] = ema(df["cascade_m1"], params.stage_smoothing)
    df["cascade_m3"] = ema(df["cascade_m2"], params.stage_smoothing)
    df["cascade_score"] = _cascade_score(df)
    df["cascade_trend"], df["cascade_trend_bars"] = _cascade_trend(
        df["cascade_score"]
    )
    df["cascade_flip_long"] = (
        (df["cascade_trend"] > 0) & (df["cascade_trend"].shift(1) <= 0)
    )
    df["cascade_flip_short"] = (
        (df["cascade_trend"] < 0) & (df["cascade_trend"].shift(1) >= 0)
    )
    df["cascade_m3_slope"] = df["cascade_m3"].diff()
    df["cascade_m3_accel"] = df["cascade_m3_slope"].diff()
    df["cascade_coherence"] = _cascade_coherence(df)

    body = df["close"] - df["open"]
    body_abs = body.abs()
    upper_wick = df["high"] - df[["open", "close"]].max(axis=1)
    lower_wick = df[["open", "close"]].min(axis=1) - df["low"]
    df["body_atr"] = body_abs / atr_safe
    df["body_percentile"] = rolling_percentile(
        df["body_atr"], params.body_percentile_window
    )
    df["volume_ratio"] = df["volume"] / df["volume"].rolling(
        params.volume_sma_window
    ).mean().replace(0.0, float("nan"))
    df["volume_impulse"] = df["volume_ratio"] >= params.min_volume_ratio
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
        & (df["low"] <= df["prior_low"] + params.stop_buffer_atr * df["atr"])
    )
    df["rejection_short"] = (
        (upper_wick >= params.rejection_wick_body * body_abs.clip(lower=1e-12))
        & (df["close"] < df["open"])
        & (df["high"] >= df["prior_high"] - params.stop_buffer_atr * df["atr"])
    )
    df["structure_event_long"] = (
        df["structure_break_long"] | df["sweep_long"] | df["rejection_long"]
    )
    df["structure_event_short"] = (
        df["structure_break_short"] | df["sweep_short"] | df["rejection_short"]
    )

    df = _merge_context(df, _context_frame(df, params))
    df["htf_score_long"] = _htf_score(df, "long", params)
    df["htf_score_short"] = _htf_score(df, "short", params)
    df["confidence_long"] = _confidence(df, "long", params)
    df["confidence_short"] = _confidence(df, "short", params)
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


class MomentumCascadeLyroScanner(BaseStrategy):
    strategy_id = MOMENTUM_CASCADE_LYRO_ID

    def __init__(
        self,
        funding: pd.DataFrame | None = None,
        *,
        params: MomentumCascadeLyroParams | None = None,
        allowed_sides: tuple[str, ...] | list[str] | None = None,
        min_confidence: float | None = None,
        min_expected_net_edge_bps: float | None = None,
        cooldown_bars: int | None = None,
        allow_continuations: bool | None = None,
    ) -> None:
        base = params or MomentumCascadeLyroParams()
        self.params = MomentumCascadeLyroParams(
            momentum_length=base.momentum_length,
            stage_smoothing=base.stage_smoothing,
            atr_window=base.atr_window,
            volume_sma_window=base.volume_sma_window,
            body_percentile_window=base.body_percentile_window,
            structure_window=base.structure_window,
            rejection_wick_body=base.rejection_wick_body,
            htf_ema_fast=base.htf_ema_fast,
            htf_ema_slow=base.htf_ema_slow,
            htf_er_window=base.htf_er_window,
            htf_adx_window=base.htf_adx_window,
            min_1h_er=base.min_1h_er,
            min_1h_adx=base.min_1h_adx,
            min_m3_abs=base.min_m3_abs,
            min_m3_slope=base.min_m3_slope,
            min_coherence=base.min_coherence,
            min_volume_ratio=base.min_volume_ratio,
            min_body_atr=base.min_body_atr,
            min_body_percentile=base.min_body_percentile,
            min_confidence=base.min_confidence if min_confidence is None else min_confidence,
            min_expected_net_edge_bps=(
                base.min_expected_net_edge_bps
                if min_expected_net_edge_bps is None
                else min_expected_net_edge_bps
            ),
            cooldown_bars=base.cooldown_bars if cooldown_bars is None else cooldown_bars,
            allow_continuations=(
                base.allow_continuations
                if allow_continuations is None
                else allow_continuations
            ),
            max_continuation_trend_bars=base.max_continuation_trend_bars,
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
        self.warmup_bars = momentum_cascade_lyro_warmup_bars(self.params)

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        return add_momentum_cascade_lyro_columns(candles, self.params)

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        row = df.iloc[index]
        required = (
            "cascade_m3",
            "cascade_m3_slope",
            "cascade_trend",
            "htf_score_long",
            "htf_score_short",
            "confidence_long",
            "confidence_short",
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
        if side not in MOMENTUM_CASCADE_LYRO_SIDES:
            return None
        row = df.iloc[index]
        if _is_nan(row.get("atr")):
            return None
        stop, target, _, _, _ = _exit_for_reference(
            side, float(entry_price), row, self.params
        )
        return SignalIntent(
            side,
            stop_price=stop,
            take_profit_price=target,
            reason="momentum_cascade_lyro rebuilt structural trail plan; trailing_stop_first; BE_after_TP1",
        )

    def _ready(self, df: pd.DataFrame, index: int, side: str) -> bool:
        row = df.iloc[index]
        if not self._side_allowed(side):
            return False
        return bool(
            row[f"candidate_{side}"]
            and float(row[f"confidence_{side}"]) >= self.params.min_confidence
            and float(row[f"expected_net_edge_bps_{side}"])
            >= self.params.min_expected_net_edge_bps
            and self._cooldown_ok(df, index, side)
        )

    def _cooldown_ok(self, df: pd.DataFrame, index: int, side: str) -> bool:
        if self.params.cooldown_bars <= 0:
            return True
        start = max(0, index - self.params.cooldown_bars)
        recent = df.iloc[start:index]
        return not bool(recent[f"candidate_{side}"].fillna(False).any())

    def _side_allowed(self, side: str) -> bool:
        return not self.params.allowed_sides or side in self.params.allowed_sides

    def _reason(self, side: str, row: pd.Series) -> str:
        edge = float(row[f"expected_net_edge_bps_{side}"])
        fill = float(row[f"fill_probability_{side}"])
        trigger = "flip" if bool(row[f"cascade_flip_{side}"]) else "continuation"
        events = _active_events(side, row)
        return (
            f"momentum_cascade_lyro {side}; style=15m_cascade/1h_bias; "
            f"trigger={trigger}; score={int(float(row['cascade_score']))}; "
            f"m1={float(row['cascade_m1']):+.3f}; m2={float(row['cascade_m2']):+.3f}; "
            f"m3={float(row['cascade_m3']):+.3f}; m3Slope={float(row['cascade_m3_slope']):+.3f}; "
            f"coherence={float(row['cascade_coherence']):.2f}; "
            f"htfScore={float(row[f'htf_score_{side}']):.1f}; "
            f"confidence={float(row[f'confidence_{side}']):.1f}; "
            f"expectedEdge={edge:.1f}; fillProbability={fill:.2f}; "
            f"volRatio={float(row['volume_ratio']):.2f}; bodyATR={float(row['body_atr']):.2f}; "
            f"events={','.join(events) or 'none'}; "
            f"tp_ladder={float(row[f'tp1_{side}']):.6g}/{float(row[f'tp2_{side}']):.6g}/{float(row[f'tp3_{side}']):.6g}; "
            f"takerCost={self.params.taker_round_trip_cost_bps:.1f}; "
            "trailing_stop_first; BE_after_TP1"
        )


def _cascade_score(df: pd.DataFrame) -> pd.Series:
    m1 = df["cascade_m1"]
    m2 = df["cascade_m2"]
    m3 = df["cascade_m3"]
    valid = m1.notna() & m2.notna() & m3.notna()
    score = (
        (m1 > 0.0).astype(int).where(m1 > 0.0, -1)
        + (m2 > 0.0).astype(int).where(m2 > 0.0, -1)
        + (m3 > 0.0).astype(int).where(m3 > 0.0, -1)
    )
    return score.where(valid, float("nan")).astype("float64")


def _cascade_trend(score: pd.Series) -> tuple[pd.Series, pd.Series]:
    trend: list[int] = []
    bars: list[int] = []
    current = 0
    current_bars = 0
    for raw in score:
        if _is_nan(raw):
            trend.append(current)
            bars.append(current_bars)
            continue
        previous = current
        if float(raw) == 3.0:
            current = 1
        elif float(raw) == -3.0:
            current = -1
        current_bars = current_bars + 1 if current == previous and current != 0 else 1
        trend.append(current)
        bars.append(current_bars)
    index = score.index
    return (
        pd.Series(trend, index=index, dtype="int64"),
        pd.Series(bars, index=index, dtype="int64"),
    )


def _cascade_coherence(df: pd.DataFrame) -> pd.Series:
    denom = (
        df["cascade_m1"].abs()
        + df["cascade_m2"].abs()
        + df["cascade_m3"].abs()
    ).replace(0.0, float("nan"))
    spread = (
        (df["cascade_m1"] - df["cascade_m2"]).abs()
        + (df["cascade_m2"] - df["cascade_m3"]).abs()
    )
    return (1.0 - spread / denom).clip(0.0, 1.0).astype("float64")


def _context_frame(
    df: pd.DataFrame,
    params: MomentumCascadeLyroParams,
) -> pd.DataFrame:
    columns = [
        "timestamp",
        "context_1h_close",
        "context_1h_ema_fast",
        "context_1h_ema_slow",
        "context_1h_er",
        "context_1h_adx",
        "context_1h_cascade_trend",
    ]
    htf = _resample_completed(df, "1h")
    if htf.empty:
        return pd.DataFrame(
            {
                "timestamp": pd.Series(dtype="datetime64[ns, UTC]"),
                **{col: pd.Series(dtype="float64") for col in columns[1:]},
            }
        )
    htf["context_1h_ema_fast"] = ema(htf["close"], params.htf_ema_fast)
    htf["context_1h_ema_slow"] = ema(htf["close"], params.htf_ema_slow)
    htf["context_1h_er"] = efficiency_ratio(htf["close"], params.htf_er_window)
    htf["context_1h_adx"] = _adx(htf, params.htf_adx_window)
    htf["roc_pct"] = htf["close"].pct_change(params.momentum_length) * 100.0
    htf["cascade_m1"] = htf["roc_pct"]
    htf["cascade_m2"] = ema(htf["cascade_m1"], params.stage_smoothing)
    htf["cascade_m3"] = ema(htf["cascade_m2"], params.stage_smoothing)
    htf["cascade_score"] = _cascade_score(htf)
    htf["context_1h_cascade_trend"] = _cascade_trend(htf["cascade_score"])[0]
    return htf[
        [
            "timestamp",
            "close",
            "context_1h_ema_fast",
            "context_1h_ema_slow",
            "context_1h_er",
            "context_1h_adx",
            "context_1h_cascade_trend",
        ]
    ].rename(columns={"close": "context_1h_close"})[columns]


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


def _htf_score(
    df: pd.DataFrame,
    side: str,
    params: MomentumCascadeLyroParams,
) -> pd.Series:
    sign = 1.0 if side == "long" else -1.0
    ema_aligned = sign * (df["context_1h_ema_fast"] - df["context_1h_ema_slow"]) > 0.0
    cascade_aligned = sign * df["context_1h_cascade_trend"] > 0.0
    er_ok = df["context_1h_er"] >= params.min_1h_er
    adx_ok = df["context_1h_adx"] >= params.min_1h_adx
    return (
        ema_aligned.astype(float)
        + cascade_aligned.astype(float)
        + er_ok.astype(float)
        + adx_ok.astype(float)
    ).astype("float64")


def _confidence(
    df: pd.DataFrame,
    side: str,
    params: MomentumCascadeLyroParams,
) -> pd.Series:
    sign = 1.0 if side == "long" else -1.0
    full_cascade = sign * df["cascade_score"] == 3.0
    committed = (sign * df["cascade_m3"]).clip(lower=0.0)
    committed_score = (committed / max(params.min_m3_abs * 4.0, 1e-9)).clip(0.0, 1.0)
    slope_score = (sign * df["cascade_m3_slope"]).clip(lower=0.0)
    slope_score = (slope_score / max(params.min_m3_abs, 1e-9)).clip(0.0, 1.0)
    impulse = df[f"displacement_{side}"].astype(float) * 8.0
    structure = df[f"structure_event_{side}"].astype(float) * 8.0
    volume_score = ((df["volume_ratio"] - params.min_volume_ratio) / 1.5).clip(0.0, 1.0)
    htf_score = (df[f"htf_score_{side}"] / 4.0).clip(0.0, 1.0)
    fresh = (1.0 - (df["cascade_trend_bars"] / max(params.max_continuation_trend_bars, 1))).clip(
        0.0, 1.0
    )
    confidence = (
        full_cascade.astype(float) * 22.0
        + committed_score * 18.0
        + slope_score * 12.0
        + df["cascade_coherence"].fillna(0.0) * 10.0
        + htf_score * 18.0
        + volume_score.fillna(0.0) * 9.0
        + impulse
        + structure
        + fresh * 5.0
    )
    return confidence.clip(0.0, 100.0).astype("float64")


def _candidate_side(
    df: pd.DataFrame,
    side: str,
    params: MomentumCascadeLyroParams,
) -> pd.Series:
    sign = 1.0 if side == "long" else -1.0
    base = (
        (sign * df["cascade_trend"] > 0.0)
        & (sign * df["cascade_m3"] >= params.min_m3_abs)
        & (sign * df["cascade_m3_slope"] >= params.min_m3_slope)
        & (df["cascade_coherence"] >= params.min_coherence)
        & (df[f"htf_score_{side}"] >= 2.0)
        & (df["volume_ratio"] >= params.min_volume_ratio)
        & df[f"displacement_{side}"]
    )
    trigger = df[f"cascade_flip_{side}"]
    if params.allow_continuations:
        continuation = (
            (df["cascade_trend_bars"] <= params.max_continuation_trend_bars)
            & (df[f"displacement_{side}"] | df[f"structure_event_{side}"])
        )
        trigger = trigger | continuation
    return (base & trigger).fillna(False)


def _exit_and_edge_columns(
    df: pd.DataFrame,
    side: str,
    params: MomentumCascadeLyroParams,
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series, pd.Series, pd.Series, pd.Series, pd.Series]:
    stops: list[float] = []
    targets: list[float] = []
    tp1s: list[float] = []
    tp2s: list[float] = []
    tp3s: list[float] = []
    gross_bps: list[float] = []
    net_bps: list[float] = []
    fills: list[float] = []
    for _, row in df.iterrows():
        if _is_nan(row.get("atr")):
            stops.append(float("nan"))
            targets.append(float("nan"))
            tp1s.append(float("nan"))
            tp2s.append(float("nan"))
            tp3s.append(float("nan"))
            gross_bps.append(float("nan"))
            net_bps.append(float("nan"))
            fills.append(float("nan"))
            continue
        close = float(row["close"])
        stop, target, tp1, tp2, tp3 = _exit_for_reference(side, close, row, params)
        gross = abs(target - close) / close * 10_000.0
        confidence = float(row.get(f"confidence_{side}", 0.0))
        htf = min(1.0, max(0.0, float(row.get(f"htf_score_{side}", 0.0)) / 4.0))
        coherence = min(1.0, max(0.0, float(row.get("cascade_coherence", 0.0))))
        expected_gross = gross * (0.48 + 0.36 * confidence / 100.0 + 0.10 * htf + 0.06 * coherence)
        stops.append(stop)
        targets.append(target)
        tp1s.append(tp1)
        tp2s.append(tp2)
        tp3s.append(tp3)
        gross_bps.append(gross)
        net_bps.append(expected_gross - params.taker_round_trip_cost_bps)
        fills.append(_fill_probability(row, side))
    index = df.index
    return (
        pd.Series(stops, index=index, dtype="float64"),
        pd.Series(targets, index=index, dtype="float64"),
        pd.Series(tp1s, index=index, dtype="float64"),
        pd.Series(tp2s, index=index, dtype="float64"),
        pd.Series(tp3s, index=index, dtype="float64"),
        pd.Series(gross_bps, index=index, dtype="float64"),
        pd.Series(net_bps, index=index, dtype="float64"),
        pd.Series(fills, index=index, dtype="float64"),
    )


def _exit_for_reference(
    side: str,
    reference_price: float,
    row: pd.Series,
    params: MomentumCascadeLyroParams,
) -> tuple[float, float, float, float, float]:
    atr_value = float(row["atr"])
    min_stop = reference_price * params.min_stop_bps / 10_000.0
    atr_stop = max(params.stop_atr_mult * atr_value, min_stop)
    if side == "long":
        candidates = [reference_price - atr_stop]
        if not _is_nan(row.get("prior_low")):
            candidates.append(float(row["prior_low"]) - params.stop_buffer_atr * atr_value)
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
        if not _is_nan(row.get("prior_high")):
            candidates.append(float(row["prior_high"]) + params.stop_buffer_atr * atr_value)
        stop = min(candidate for candidate in candidates if candidate > reference_price)
        if stop - reference_price < min_stop:
            stop = reference_price + min_stop
        risk = stop - reference_price
        tp1 = reference_price - risk
        tp2 = reference_price - 2.0 * risk
        tp3 = reference_price - 3.0 * risk
        target = reference_price - params.take_profit_r * risk
    return stop, target, tp1, tp2, tp3


def _fill_probability(row: pd.Series, side: str) -> float:
    confidence = float(row.get(f"confidence_{side}", 0.0))
    volume = float(row.get("volume_ratio", 0.0))
    body = float(row.get("body_atr", 0.0))
    trend_bars = float(row.get("cascade_trend_bars", 99.0))
    freshness = max(0.0, 1.0 - trend_bars / 24.0)
    raw = (
        0.28
        + (confidence - 50.0) / 100.0 * 0.34
        + min(max(volume - 0.8, 0.0), 1.4) * 0.08
        + min(max(body, 0.0), 2.0) * 0.04
        + freshness * 0.10
    )
    return round(min(0.85, max(0.20, raw)), 4)


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
    dx = (
        (plus_di - minus_di).abs()
        / (plus_di + minus_di).replace(0.0, float("nan"))
    ) * 100.0
    return dx.rolling(window).mean()


def _base_delta(timestamps: pd.Series) -> pd.Timedelta:
    deltas = pd.to_datetime(timestamps, utc=True).sort_values().diff().dropna()
    if deltas.empty:
        return pd.Timedelta(minutes=15)
    return deltas.median()


def _active_events(side: str, row: pd.Series) -> list[str]:
    if side == "long":
        checks = (
            ("flip", "cascade_flip_long"),
            ("break", "structure_break_long"),
            ("sweep", "sweep_long"),
            ("rejection", "rejection_long"),
            ("displacement", "displacement_long"),
        )
    else:
        checks = (
            ("flip", "cascade_flip_short"),
            ("break", "structure_break_short"),
            ("sweep", "sweep_short"),
            ("rejection", "rejection_short"),
            ("displacement", "displacement_short"),
        )
    return [label for label, col in checks if bool(row.get(col, False))]


def _validate_sides(values: tuple[str, ...]) -> tuple[str, ...]:
    unknown = sorted(set(values) - set(MOMENTUM_CASCADE_LYRO_SIDES))
    if unknown:
        raise ValueError(f"allowed_sides contains unknown values: {unknown}")
    return values


def _is_nan(value: Any) -> bool:
    try:
        return math.isnan(float(value))
    except (TypeError, ValueError):
        return True
