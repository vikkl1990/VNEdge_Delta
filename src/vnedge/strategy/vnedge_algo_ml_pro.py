"""VNEDGE Algo SuperTrend ML Pro + BBP scanner.

This is a source-backed, causal port of the operator-supplied open Pine script
``VNEDGE ALGO SuperTrend ML Pro + BBP``.  It keeps the executable mechanics:
adaptive SuperTrend flips, BBP pressure, HTF EMA alignment, ADX/RSI/volume
features, ML-style confidence scoring, and SL/TP geometry.  Chart-only tables,
labels, fills, and spectral drawings are intentionally excluded.

The strategy emits research/paper intents only.  It does not grant live
permission, does not alter risk limits, and treats the requested 100 USD x 25x
paper sizing as a reporting lens in the research router.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Literal

import pandas as pd

from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent
from vnedge.strategy.indicators import atr, efficiency_ratio, ema, prior_high, prior_low


VNEDGE_ALGO_ML_PRO_ID = "vnedge_algo_ml_pro_v1"
VNEDGE_ALGO_ML_PRO_SIDES: tuple[str, ...] = ("long", "short")


def _tp_ladder(row: pd.Series, side: str) -> tuple[float, ...]:
    """The (tp1, tp2, tp3) take-profit ladder for the journal — NaN-safe."""
    out: list[float] = []
    for i in (1, 2, 3):
        value = row.get(f"tp{i}_{side}")
        if value is not None and not pd.isna(value):
            out.append(float(value))
    return tuple(out)


@dataclass(frozen=True)
class VNEDGEAlgoMLProParams:
    """Frozen parameters matching the supplied Pine defaults where executable."""

    auto_tune: bool = True
    atr_length: int = 13
    base_multiplier: float = 2.5
    profile_lookback: int = 100
    regime_sensitivity: float = 1.0
    min_multiplier: float = 1.0
    max_multiplier: float = 5.0
    cushion_atr: float = 0.15
    cooldown_bars: int = 3

    use_mtf: bool = True
    mtf_mode: Literal["auto", "manual"] = "auto"
    mtf_manual_rule: str = "4h"
    mtf_strictness: Literal["loose", "moderate", "strict"] = "moderate"
    htf_ema_fast: int = 20
    htf_ema_slow: int = 50

    use_ml_filter: bool = False
    ml_gate: float = 21.0
    self_learn_gate: bool = False
    eval_horizon_bars: int = 15
    ml_weights: tuple[float, ...] = (
        0.15,
        0.08,
        0.15,
        -0.08,
        0.10,
        0.08,
        0.08,
        0.04,
        0.12,
        0.10,
        0.08,
        0.06,
        0.04,
    )
    ml_bias: float = 0.0

    use_momentum: bool = True
    rsi_length: int = 13
    rsi_threshold: float = 45.0
    use_volume_filter: bool = False
    volume_multiplier: float = 1.20
    min_rr: float = 0.0

    bbp_length: int = 13
    bbp_norm_lookback: int = 100
    bbp_strong_threshold: float = 0.45

    sl_mode: Literal["atr", "band", "fixed_pct"] = "band"
    sl_atr_multiplier: float = 1.5
    sl_fixed_pct: float = 2.0
    tp_levels: int = 3
    tp1_atr_multiplier: float = 2.0
    tp2_atr_multiplier: float = 4.0
    tp3_atr_multiplier: float = 6.0
    use_trailing: bool = True
    trail_mode: Literal["atr", "band", "atr_to_band"] = "band"
    trail_atr_multiplier: float = 1.2
    min_stop_bps: float = 8.0

    taker_entry_bps: float = 5.0
    taker_exit_bps: float = 5.0
    slippage_bps: float = 2.0
    safety_buffer_bps: float = 5.0
    min_expected_net_edge_bps: float = 0.0
    min_fill_probability: float = 0.20
    allowed_sides: tuple[str, ...] = ()

    @property
    def taker_round_trip_cost_bps(self) -> float:
        return (
            self.taker_entry_bps
            + self.taker_exit_bps
            + self.slippage_bps
            + self.safety_buffer_bps
        )


def vnedge_algo_ml_pro_warmup_bars(params: VNEDGEAlgoMLProParams) -> int:
    local = max(
        params.profile_lookback,
        params.atr_length,
        params.bbp_length,
        params.bbp_norm_lookback,
        params.rsi_length,
        params.htf_ema_slow,
        55,
    )
    # 5m charts use a 30m HTF by default; 50 HTF bars need 300 5m bars.
    return max(local, params.htf_ema_slow * 6) + 2


class VNEDGEAlgoMLProScanner(BaseStrategy):
    strategy_id = VNEDGE_ALGO_ML_PRO_ID

    def __init__(
        self,
        funding: pd.DataFrame | None = None,
        *,
        params: VNEDGEAlgoMLProParams | None = None,
        allowed_sides: tuple[str, ...] | list[str] | None = None,
        min_expected_net_edge_bps: float | None = None,
        use_ml_filter: bool | None = None,
        ml_gate: float | None = None,
    ) -> None:
        base = params or VNEDGEAlgoMLProParams()
        self.params = VNEDGEAlgoMLProParams(
            auto_tune=base.auto_tune,
            atr_length=base.atr_length,
            base_multiplier=base.base_multiplier,
            profile_lookback=base.profile_lookback,
            regime_sensitivity=base.regime_sensitivity,
            min_multiplier=base.min_multiplier,
            max_multiplier=base.max_multiplier,
            cushion_atr=base.cushion_atr,
            cooldown_bars=base.cooldown_bars,
            use_mtf=base.use_mtf,
            mtf_mode=base.mtf_mode,
            mtf_manual_rule=base.mtf_manual_rule,
            mtf_strictness=base.mtf_strictness,
            htf_ema_fast=base.htf_ema_fast,
            htf_ema_slow=base.htf_ema_slow,
            use_ml_filter=base.use_ml_filter if use_ml_filter is None else use_ml_filter,
            ml_gate=base.ml_gate if ml_gate is None else ml_gate,
            self_learn_gate=base.self_learn_gate,
            eval_horizon_bars=base.eval_horizon_bars,
            ml_weights=base.ml_weights,
            ml_bias=base.ml_bias,
            use_momentum=base.use_momentum,
            rsi_length=base.rsi_length,
            rsi_threshold=base.rsi_threshold,
            use_volume_filter=base.use_volume_filter,
            volume_multiplier=base.volume_multiplier,
            min_rr=base.min_rr,
            bbp_length=base.bbp_length,
            bbp_norm_lookback=base.bbp_norm_lookback,
            bbp_strong_threshold=base.bbp_strong_threshold,
            sl_mode=base.sl_mode,
            sl_atr_multiplier=base.sl_atr_multiplier,
            sl_fixed_pct=base.sl_fixed_pct,
            tp_levels=base.tp_levels,
            tp1_atr_multiplier=base.tp1_atr_multiplier,
            tp2_atr_multiplier=base.tp2_atr_multiplier,
            tp3_atr_multiplier=base.tp3_atr_multiplier,
            use_trailing=base.use_trailing,
            trail_mode=base.trail_mode,
            trail_atr_multiplier=base.trail_atr_multiplier,
            min_stop_bps=base.min_stop_bps,
            taker_entry_bps=base.taker_entry_bps,
            taker_exit_bps=base.taker_exit_bps,
            slippage_bps=base.slippage_bps,
            safety_buffer_bps=base.safety_buffer_bps,
            min_expected_net_edge_bps=(
                base.min_expected_net_edge_bps
                if min_expected_net_edge_bps is None
                else min_expected_net_edge_bps
            ),
            min_fill_probability=base.min_fill_probability,
            allowed_sides=_validate_sides(
                tuple(base.allowed_sides if allowed_sides is None else allowed_sides)
            ),
        )
        self.funding = funding
        self.warmup_bars = vnedge_algo_ml_pro_warmup_bars(self.params)

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        return add_vnedge_algo_ml_pro_columns(candles, self.params)

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        row = df.iloc[index]
        required = (
            "raw_flip",
            "trend_dir",
            "st_band",
            "atr_value",
            "rsi",
            "ml_score",
            "stop_long",
            "stop_short",
            "tp3_long",
            "tp3_short",
            "expected_net_edge_bps_long",
            "expected_net_edge_bps_short",
            "fill_probability_long",
            "fill_probability_short",
        )
        if any(_is_nan(row.get(col)) for col in required):
            return None
        if self._ready(row, "long"):
            return SignalIntent(
                "long",
                stop_price=float(row["stop_long"]),
                take_profit_price=float(row["tp3_long"]),
                take_profit_levels=_tp_ladder(row, "long"),
                reason=self._reason(row, "long"),
            )
        if self._ready(row, "short"):
            return SignalIntent(
                "short",
                stop_price=float(row["stop_short"]),
                take_profit_price=float(row["tp3_short"]),
                take_profit_levels=_tp_ladder(row, "short"),
                reason=self._reason(row, "short"),
            )
        return None

    def synthesize_exit_plan(
        self, df: pd.DataFrame, index: int, side: str, entry_price: float
    ) -> SignalIntent | None:
        row = df.iloc[index]
        if side not in VNEDGE_ALGO_ML_PRO_SIDES or _is_nan(row.get("atr_value")):
            return None
        stop = _stop_price(side, float(entry_price), row, self.params)
        tp1, _, tp3 = _target_ladder(side, float(entry_price), float(row["atr_value"]), self.params)
        return SignalIntent(
            side,
            stop_price=stop,
            take_profit_price=tp3,
            reason=(
                "vnedge_algo_ml_pro rebuilt TP/SL plan; "
                f"tp1={tp1:.6g}; tp3={tp3:.6g}; trailing={self.params.trail_mode}"
            ),
        )

    def _side_allowed(self, side: str) -> bool:
        return not self.params.allowed_sides or side in self.params.allowed_sides

    def _ready(self, row: pd.Series, side: str) -> bool:
        direction = 1 if side == "long" else -1
        ml_pass = (
            not self.params.use_ml_filter
            or float(row["ml_score"]) >= float(row["effective_ml_gate"])
        )
        return bool(
            self._side_allowed(side)
            and bool(row["raw_flip"])
            and int(row["trend_dir"]) == direction
            and bool(row[f"classic_filters_ok_{side}"])
            and ml_pass
            and float(row[f"expected_net_edge_bps_{side}"])
            >= self.params.min_expected_net_edge_bps
            and float(row[f"fill_probability_{side}"]) >= self.params.min_fill_probability
        )

    def _reason(self, row: pd.Series, side: str) -> str:
        edge = float(row[f"expected_net_edge_bps_{side}"])
        fill = float(row[f"fill_probability_{side}"])
        tp1 = float(row[f"tp1_{side}"])
        tp2 = float(row[f"tp2_{side}"])
        tp3 = float(row[f"tp3_{side}"])
        return (
            f"vnedge_algo_ml_pro {side}; source=VNEDGE_ALGO_v6.0.1; "
            f"tf=trigger; htf={row['htf_rule']}:{row['htf_bias']}; "
            f"regime={row['regime']}; ml={float(row['ml_score']):.1f}; "
            f"adx={float(row['adx']):.1f}; rsi={float(row['rsi']):.1f}; "
            f"bbp={float(row['bbp']):+.6g}; bbpStrength={float(row['bbp_strength']):+.2f}; "
            f"band={float(row['st_band']):.6g}; rr={float(row[f'rr_{side}']):.2f}; "
            f"expectedNet={edge:.1f}; fillProbability={fill:.2f}; "
            f"takerCost={self.params.taker_round_trip_cost_bps:.1f}; "
            f"tp_ladder={tp1:.6g}/{tp2:.6g}/{tp3:.6g}; "
            "paperMargin=100; paperLeverage=25; paperNotional=2500; "
            "SL_first; TP3_closeout; trailing_band"
        )


def add_vnedge_algo_ml_pro_columns(
    candles: pd.DataFrame,
    params: VNEDGEAlgoMLProParams = VNEDGEAlgoMLProParams(),
) -> pd.DataFrame:
    df = candles.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df["atr_base"] = atr(df, params.atr_length).fillna(df["high"] - df["low"])
    df["atr_value"] = df["atr_base"]
    df["atr_profile"] = atr(df, params.profile_lookback)
    df["atr_20"] = atr(df, 20)
    df["atr_smooth"] = df["atr_base"].rolling(
        int(max(params.atr_length * 4, 20))
    ).mean()
    atr_safe = df["atr_base"].replace(0.0, float("nan"))

    df["efficiency_ratio"] = efficiency_ratio(df["close"], params.profile_lookback).fillna(0.5)
    returns = df["close"].pct_change()
    ac_window = int(min(max(params.profile_lookback, 50), 100))
    df["auto_corr"] = returns.rolling(ac_window).corr(returns.shift(1)).fillna(0.0)
    df["vol_cluster"] = (
        df["atr_20"] / df["atr_profile"].replace(0.0, float("nan"))
    ).fillna(1.0)
    ema_smooth = int(max(params.profile_lookback / 3, 10))
    df["er_smooth"] = ema(df["efficiency_ratio"], ema_smooth)
    df["vc_smooth"] = ema(df["vol_cluster"], ema_smooth)
    trend_score = df["er_smooth"] * params.regime_sensitivity
    range_score = (
        (1.0 - df["er_smooth"]) * (1.0 - df["auto_corr"].abs()) * params.regime_sensitivity
    )
    vol_score = (df["vc_smooth"] - 1.0).clip(lower=0.0, upper=2.0) * params.regime_sensitivity
    score_total = (trend_score + range_score + vol_score).replace(0.0, float("nan"))
    df["regime"] = [
        "TRENDING" if t >= r and t >= v else "RANGING" if r >= t and r >= v else "VOLATILE"
        for t, r, v in zip(trend_score, range_score, vol_score, strict=True)
    ]
    df["regime_confidence"] = (
        pd.concat([trend_score, range_score, vol_score], axis=1).max(axis=1)
        / score_total
        * 100.0
    ).fillna(33.0)
    w_t = (trend_score / score_total).fillna(1.0 / 3.0)
    w_r = (range_score / score_total).fillna(1.0 / 3.0)
    w_v = (vol_score / score_total).fillna(1.0 / 3.0)
    norm_vol = ((df["atr_profile"] / df["close"].replace(0.0, float("nan"))) * 100.0).fillna(1.0)
    if params.auto_tune:
        effective_base = (
            (w_t * 2.0 + w_r * 3.2 + w_v * 3.8)
            * (norm_vol / 1.0).clip(lower=0.8, upper=1.5)
        ).clip(lower=1.2, upper=6.0)
        df["effective_cushion"] = (w_t * 0.05 + w_r * 0.25 + w_v * 0.15).clip(
            lower=0.0, upper=0.5
        )
        df["effective_cooldown"] = (w_t * 2.0 + w_r * 5.0 + w_v * 3.0).clip(
            lower=1.0, upper=10.0
        ).round()
        df["effective_rsi_threshold"] = (
            w_t * 40.0 + w_r * 52.0 + w_v * 45.0
        ).clip(lower=35.0, upper=58.0)
    else:
        effective_base = pd.Series(params.base_multiplier, index=df.index)
        df["effective_cushion"] = params.cushion_atr
        df["effective_cooldown"] = params.cooldown_bars
        df["effective_rsi_threshold"] = params.rsi_threshold
    vol_ratio = (df["atr_base"] / df["atr_smooth"].replace(0.0, float("nan"))).fillna(1.0)
    df["adaptive_multiplier"] = (effective_base * vol_ratio).clip(
        lower=params.min_multiplier,
        upper=params.max_multiplier,
    )
    (
        df["st_band"],
        df["trend_dir"],
        df["upper_band"],
        df["lower_band"],
        df["bars_since_flip"],
    ) = _adaptive_supertrend(df, params)
    df["raw_flip"] = df["trend_dir"] != df["trend_dir"].shift(1)
    df["raw_flip"] = df["raw_flip"].fillna(False)

    htf_rule = _auto_htf_rule(df) if params.mtf_mode == "auto" else params.mtf_manual_rule
    df["htf_rule"] = htf_rule
    df = _merge_context(df, _htf_context(df, htf_rule, params))
    df["htf_bias"] = df["htf_trend"].map({1: "bull", -1: "bear"}).fillna("unknown")
    if params.use_mtf:
        df["mtf_aligned"] = df["trend_dir"] == df["htf_trend"]
    else:
        df["mtf_aligned"] = True
    if params.use_mtf and params.mtf_strictness == "strict":
        df["mtf_hard_block"] = ~df["mtf_aligned"].fillna(False)
    else:
        df["mtf_hard_block"] = False

    df["adx"] = _adx(df, 14)
    df["rsi"] = _rsi(df["close"], params.rsi_length)
    df["bbp_ema"] = ema(df["close"], params.bbp_length)
    df["bull_power"] = df["high"] - df["bbp_ema"]
    df["bear_power"] = df["low"] - df["bbp_ema"]
    df["bbp"] = df["bull_power"] + df["bear_power"]
    bbp_abs_max = df["bbp"].abs().rolling(params.bbp_norm_lookback).max()
    df["bbp_strength"] = (df["bbp"] / bbp_abs_max.replace(0.0, float("nan"))).clip(
        lower=-1.0, upper=1.0
    )
    df["bbp_strong"] = df["bbp_strength"].abs() >= params.bbp_strong_threshold
    df["volume_sma20"] = df["volume"].rolling(20).mean()
    df["volume_ratio"] = df["volume"] / df["volume_sma20"].replace(0.0, float("nan"))
    if params.use_volume_filter:
        df["volume_ok"] = df["volume"] > df["volume_sma20"].fillna(0.0) * params.volume_multiplier
    else:
        df["volume_ok"] = True
    if params.use_momentum:
        df["momentum_ok_long"] = df["rsi"] >= df["effective_rsi_threshold"]
        df["momentum_ok_short"] = df["rsi"] <= (100.0 - df["effective_rsi_threshold"])
    else:
        df["momentum_ok_long"] = True
        df["momentum_ok_short"] = True

    df["prior_high_10"] = prior_high(df["high"], 10)
    df["prior_low_10"] = prior_low(df["low"], 10)
    df["prior_high_20"] = prior_high(df["high"], 20)
    df["prior_low_20"] = prior_low(df["low"], 20)
    df["bull_divergence"], df["bear_divergence"] = _divergence(df)
    df["near_volume_zone"], df["dist_to_hv"] = _volume_zone(df)

    df["stop_long"] = df.apply(lambda row: _stop_price("long", float(row["close"]), row, params), axis=1)
    df["stop_short"] = df.apply(lambda row: _stop_price("short", float(row["close"]), row, params), axis=1)
    df["rr_long"] = _rr(df, "long", params)
    df["rr_short"] = _rr(df, "short", params)
    df["rr_ok_long"] = params.min_rr <= 0.0 or df["rr_long"] >= params.min_rr
    df["rr_ok_short"] = params.min_rr <= 0.0 or df["rr_short"] >= params.min_rr

    df["classic_filters_ok_long"] = (
        df["momentum_ok_long"] & df["volume_ok"] & df["rr_ok_long"] & ~df["mtf_hard_block"]
    )
    df["classic_filters_ok_short"] = (
        df["momentum_ok_short"] & df["volume_ok"] & df["rr_ok_short"] & ~df["mtf_hard_block"]
    )
    df["ml_score"] = _ml_score(df, params)
    df["effective_ml_gate"] = params.ml_gate
    if params.use_ml_filter:
        df["ml_pass"] = df["ml_score"] >= df["effective_ml_gate"]
    else:
        df["ml_pass"] = True
    df["confirmed_long"] = (
        df["raw_flip"] & (df["trend_dir"] == 1) & df["classic_filters_ok_long"] & df["ml_pass"]
    )
    df["confirmed_short"] = (
        df["raw_flip"] & (df["trend_dir"] == -1) & df["classic_filters_ok_short"] & df["ml_pass"]
    )

    for side in VNEDGE_ALGO_ML_PRO_SIDES:
        tp1, tp2, tp3 = _target_ladder_series(df, side, params)
        df[f"tp1_{side}"] = tp1
        df[f"tp2_{side}"] = tp2
        df[f"tp3_{side}"] = tp3
        gross = _target_gross_bps(df, side, tp3)
        quality = _signal_quality(df, side, params)
        df[f"expected_gross_bps_{side}"] = gross
        df[f"expected_net_edge_bps_{side}"] = (
            gross * quality - params.taker_round_trip_cost_bps
        )
        df[f"fill_probability_{side}"] = (0.25 + 0.60 * quality).clip(
            lower=0.0, upper=0.95
        )
    return df


def _adaptive_supertrend(
    df: pd.DataFrame,
    params: VNEDGEAlgoMLProParams,
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series, pd.Series]:
    hl2 = (df["high"] + df["low"]) / 2.0
    upper = hl2 + df["adaptive_multiplier"] * df["atr_base"]
    lower = hl2 - df["adaptive_multiplier"] * df["atr_base"]
    st_band: list[float] = []
    trend: list[int] = []
    bars_since: list[int] = []
    current_trend = 1
    current_band = float("nan")
    since_flip = 100
    for pos, (i, row) in enumerate(df.iterrows()):
        atr_value = float(row["atr_base"])
        close = float(row["close"])
        upper_band = float(upper.loc[i])
        lower_band = float(lower.loc[i])
        if pos == 0 or not math.isfinite(atr_value):
            current_band = lower_band
            trend.append(current_trend)
            st_band.append(current_band)
            bars_since.append(since_flip)
            continue
        since_flip += 1
        prev_band = current_band if math.isfinite(current_band) else (
            lower_band if current_trend == 1 else upper_band
        )
        cushion = float(row["effective_cushion"]) * atr_value
        cooldown = int(row["effective_cooldown"])
        if current_trend == 1:
            current_band = max(lower_band, prev_band)
            if close < (current_band - cushion) and since_flip >= cooldown:
                current_trend = -1
                current_band = upper_band
                since_flip = 0
        else:
            current_band = min(upper_band, prev_band)
            if close > (current_band + cushion) and since_flip >= cooldown:
                current_trend = 1
                current_band = lower_band
                since_flip = 0
        trend.append(current_trend)
        st_band.append(current_band)
        bars_since.append(since_flip)
    index = df.index
    return (
        pd.Series(st_band, index=index, dtype="float64"),
        pd.Series(trend, index=index, dtype="int64"),
        upper.astype("float64"),
        lower.astype("float64"),
        pd.Series(bars_since, index=index, dtype="int64"),
    )


def _htf_context(
    df: pd.DataFrame,
    rule: str,
    params: VNEDGEAlgoMLProParams,
) -> pd.DataFrame:
    htf = _resample_completed(df, rule)
    if htf.empty:
        return pd.DataFrame(
            {
                "timestamp": pd.Series(dtype="datetime64[ns, UTC]"),
                "htf_ema_fast": pd.Series(dtype="float64"),
                "htf_ema_slow": pd.Series(dtype="float64"),
                "htf_trend": pd.Series(dtype="float64"),
            }
        )
    htf["htf_ema_fast"] = ema(htf["close"], params.htf_ema_fast)
    htf["htf_ema_slow"] = ema(htf["close"], params.htf_ema_slow)
    htf["htf_trend"] = (htf["htf_ema_fast"] > htf["htf_ema_slow"]).map(
        {True: 1, False: -1}
    )
    return htf[["timestamp", "htf_ema_fast", "htf_ema_slow", "htf_trend"]]


def _ml_score(df: pd.DataFrame, params: VNEDGEAlgoMLProParams) -> pd.Series:
    direction = df["trend_dir"]
    f1 = ((df["rsi"] - 50.0) * 2.0).where(direction == 1, (50.0 - df["rsi"]) * 2.0)
    f1 = (f1 + 50.0).clip(lower=0.0, upper=100.0)
    f2 = (df["volume_ratio"].fillna(1.0) * 50.0).clip(lower=0.0, upper=100.0)
    f3 = (df["er_smooth"] * 100.0).clip(lower=0.0, upper=100.0)
    f4 = ((2.0 - df["vol_cluster"]) * 50.0).clip(lower=0.0, upper=100.0)
    band_dist = ((df["close"] - df["st_band"]) / df["atr_base"].replace(0.0, float("nan"))).where(
        direction == 1,
        (df["st_band"] - df["close"]) / df["atr_base"].replace(0.0, float("nan")),
    )
    f5 = _sigmoid100(band_dist.fillna(0.0), midpoint=0.5, steepness=3.0)
    macd_line = ema(df["close"], 12) - ema(df["close"], 26)
    macd_signal = ema(macd_line, 9)
    macd_norm = (macd_line - macd_signal) / df["atr_base"].replace(0.0, float("nan"))
    f6 = _sigmoid100(macd_norm.where(direction == 1, -macd_norm).fillna(0.0), 0.0, 5.0)
    hh = df["high"].rolling(10).max()
    ll = df["low"].rolling(10).min()
    hh_prev = df["high"].shift(10).rolling(10).max()
    ll_prev = df["low"].shift(10).rolling(10).min()
    f7_long = 20.0 + (hh > hh_prev).astype(float) * 30.0 + (ll > ll_prev).astype(float) * 30.0
    f7_short = 20.0 + (ll < ll_prev).astype(float) * 30.0 + (hh < hh_prev).astype(float) * 30.0
    f7 = f7_long.where(direction == 1, f7_short)
    f8 = df["regime_confidence"].clip(lower=0.0, upper=100.0)
    f9 = pd.Series(50.0, index=df.index)
    if params.use_mtf:
        f9 = df["mtf_aligned"].map({True: 100.0, False: 0.0}).astype("float64")
    f10 = (df["adx"] * 2.5).clip(lower=0.0, upper=100.0)
    f11 = pd.Series(50.0, index=df.index)
    f11 = f11.mask((direction == 1) & df["bull_divergence"], 100.0)
    f11 = f11.mask((direction == -1) & df["bear_divergence"], 100.0)
    f11 = f11.mask((direction == 1) & df["bear_divergence"], 10.0)
    f11 = f11.mask((direction == -1) & df["bull_divergence"], 10.0)
    f12 = pd.Series(50.0, index=df.index)
    f12 = f12.mask(df["near_volume_zone"], 100.0)
    f12 = f12.mask(~df["near_volume_zone"], ((3.0 - df["dist_to_hv"]) / 3.0 * 100.0).clip(0.0, 50.0))
    f13 = pd.Series(50.0, index=df.index)
    features = (f1, f2, f3, f4, f5, f6, f7, f8, f9, f10, f11, f12, f13)
    raw = pd.Series(params.ml_bias, index=df.index, dtype="float64")
    weight_sum = 0.0
    for weight, feature in zip(params.ml_weights, features, strict=True):
        raw = raw + weight * feature.fillna(50.0)
        weight_sum += abs(weight)
    normalized = raw / weight_sum if weight_sum > 0 else pd.Series(50.0, index=df.index)
    return _sigmoid100(normalized, midpoint=50.0, steepness=0.08)


def _signal_quality(
    df: pd.DataFrame,
    side: str,
    params: VNEDGEAlgoMLProParams,
) -> pd.Series:
    signed_bbp = df["bbp_strength"] if side == "long" else -df["bbp_strength"]
    bbp_component = ((signed_bbp + 1.0) / 2.0).clip(0.0, 1.0)
    ml_component = (df["ml_score"] / 100.0).clip(0.0, 1.0)
    adx_component = (df["adx"] / 35.0).clip(0.0, 1.0)
    rr_component = (df[f"rr_{side}"] / 3.0).clip(0.0, 1.0)
    mtf_component = df["mtf_aligned"].map({True: 1.0, False: 0.35}).fillna(0.35)
    flip_component = 1.0 / (1.0 + df["bars_since_flip"].clip(lower=0) / 10.0)
    quality = (
        0.28 * ml_component
        + 0.18 * bbp_component
        + 0.16 * adx_component
        + 0.18 * rr_component
        + 0.12 * mtf_component
        + 0.08 * flip_component
    )
    if params.mtf_strictness == "loose":
        quality = quality + 0.05
    return quality.clip(0.0, 1.0)


def _stop_price(
    side: str,
    entry: float,
    row: pd.Series,
    params: VNEDGEAlgoMLProParams,
) -> float:
    atr_value = float(row.get("atr_base", row.get("atr_value", float("nan"))))
    band = float(row.get("st_band", entry))
    min_dist = entry * params.min_stop_bps / 10_000.0
    if not math.isfinite(atr_value) or atr_value <= 0:
        atr_value = max(abs(float(row.get("high", entry)) - float(row.get("low", entry))), min_dist)
    if params.sl_mode == "atr":
        dist = max(params.sl_atr_multiplier * atr_value, min_dist)
        return entry - dist if side == "long" else entry + dist
    if params.sl_mode == "fixed_pct":
        return entry * (1.0 - params.sl_fixed_pct / 100.0) if side == "long" else entry * (
            1.0 + params.sl_fixed_pct / 100.0
        )
    if side == "long":
        stop = band if 0.0 < band < entry else entry - params.sl_atr_multiplier * atr_value
        return min(entry - min_dist, max(stop, entry - params.sl_atr_multiplier * atr_value * 3.0))
    stop = band if band > entry else entry + params.sl_atr_multiplier * atr_value
    return max(entry + min_dist, min(stop, entry + params.sl_atr_multiplier * atr_value * 3.0))


def _target_ladder(
    side: str,
    entry: float,
    atr_value: float,
    params: VNEDGEAlgoMLProParams,
) -> tuple[float, float, float]:
    direction = 1.0 if side == "long" else -1.0
    tp1 = entry + direction * params.tp1_atr_multiplier * atr_value
    tp2 = entry + direction * params.tp2_atr_multiplier * atr_value
    tp3 = entry + direction * params.tp3_atr_multiplier * atr_value
    return tp1, tp2, tp3


def _target_ladder_series(
    df: pd.DataFrame,
    side: str,
    params: VNEDGEAlgoMLProParams,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    direction = 1.0 if side == "long" else -1.0
    return (
        df["close"] + direction * params.tp1_atr_multiplier * df["atr_base"],
        df["close"] + direction * params.tp2_atr_multiplier * df["atr_base"],
        df["close"] + direction * params.tp3_atr_multiplier * df["atr_base"],
    )


def _rr(df: pd.DataFrame, side: str, params: VNEDGEAlgoMLProParams) -> pd.Series:
    stop = df[f"stop_{side}"]
    risk = (df["close"] - stop).abs()
    reward = params.tp1_atr_multiplier * df["atr_base"]
    return reward / risk.replace(0.0, float("nan"))


def _target_gross_bps(df: pd.DataFrame, side: str, target: pd.Series) -> pd.Series:
    if side == "long":
        return ((target - df["close"]) / df["close"] * 10_000.0).clip(lower=0.0)
    return ((df["close"] - target) / df["close"] * 10_000.0).clip(lower=0.0)


def _auto_htf_rule(df: pd.DataFrame) -> str:
    delta = _base_delta(df["timestamp"])
    minutes = max(int(round(delta.total_seconds() / 60.0)), 1)
    if minutes <= 3:
        return "15min"
    if minutes <= 5:
        return "30min"
    if minutes <= 15:
        return "1h"
    if minutes <= 30:
        return "2h"
    if minutes <= 120:
        return "4h"
    if minutes <= 240:
        return "1D"
    return "1W"


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
    complete_offset = pd.Timedelta(rule) - _base_delta(df["timestamp"])
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
    merged = pd.merge_asof(left, right, on="timestamp", direction="backward")
    return merged.sort_index()


def _base_delta(timestamps: pd.Series) -> pd.Timedelta:
    deltas = pd.to_datetime(timestamps, utc=True).sort_values().diff().dropna()
    if deltas.empty:
        return pd.Timedelta(minutes=5)
    return deltas.median()


def _rsi(series: pd.Series, window: int) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()
    avg_loss = loss.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()
    rs = avg_gain / avg_loss.replace(0.0, float("nan"))
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi.fillna(50.0)


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
    smooth_tr = tr.ewm(alpha=1.0 / window, adjust=False).mean().replace(0.0, float("nan"))
    plus_di = 100.0 * plus_dm.ewm(alpha=1.0 / window, adjust=False).mean() / smooth_tr
    minus_di = 100.0 * minus_dm.ewm(alpha=1.0 / window, adjust=False).mean() / smooth_tr
    dx = ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, float("nan"))) * 100.0
    return dx.ewm(alpha=1.0 / window, adjust=False).mean()


def _divergence(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    rsi = df["rsi"]
    price_low = df["low"]
    price_high = df["high"]
    recent_low = prior_low(price_low, 10)
    recent_high = prior_high(price_high, 10)
    recent_rsi_low = prior_low(rsi, 10)
    recent_rsi_high = prior_high(rsi, 10)
    bull = (price_low < recent_low) & (rsi > recent_rsi_low)
    bear = (price_high > recent_high) & (rsi < recent_rsi_high)
    return bull.fillna(False), bear.fillna(False)


def _volume_zone(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    hv_price: list[float] = []
    for i in range(len(df)):
        start = max(0, i - 50 + 1)
        window = df.iloc[start : i + 1]
        if window.empty:
            hv_price.append(float("nan"))
            continue
        idx = window["volume"].idxmax()
        hv_price.append(float(window.loc[idx, "close"]))
    hv = pd.Series(hv_price, index=df.index)
    dist = ((df["close"] - hv).abs() / df["atr_base"].replace(0.0, float("nan"))).abs()
    return (dist <= 0.5).fillna(False), dist


def _sigmoid100(series: pd.Series, midpoint: float, steepness: float) -> pd.Series:
    return 100.0 / (1.0 + ((-(series - midpoint) * steepness).clip(-60, 60)).map(math.exp))


def _validate_sides(sides: tuple[str, ...]) -> tuple[str, ...]:
    invalid = sorted(set(sides) - set(VNEDGE_ALGO_ML_PRO_SIDES))
    if invalid:
        raise ValueError(f"invalid sides: {invalid}")
    return tuple(dict.fromkeys(sides))


def _is_nan(value: object) -> bool:
    try:
        return value is None or pd.isna(value)
    except TypeError:
        return False
