"""VNEDGE Quant Signal Pack.

This is the bot-native version of the commercial indicator stacks operators
often use manually: SMC structure, liquidity sweeps, FVG/order-block retests,
squeeze/momentum release, VWAP deviation, volume impulse, and multi-horizon
bias. It deliberately does not copy TradingView/Pine scripts. Every column is
causal, every emitted signal has an explicit stop/target, and the lane remains
research/shadow-only until it clears the normal VNEDGE promotion machinery.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import math

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
    zscore,
)
from vnedge.strategy.regime import merge_funding


QUANT_SIGNAL_FAMILIES: tuple[str, ...] = (
    "liquidity_sweep",
    "fvg_retest",
    "order_block",
    "squeeze_release",
    "vwap_reclaim",
    "structure_break",
    "confluence",
)
QUANT_SIGNAL_SIDES: tuple[str, ...] = ("long", "short")


@dataclass(frozen=True)
class QuantSignalPackParams:
    structure_window: int = 48
    liquidity_window: int = 72
    atr_window: int = 24
    atr_pct_window: int = 240
    ema_fast: int = 21
    ema_mid: int = 55
    ema_slow: int = 144
    er_window: int = 48
    vwap_window: int = 96
    volume_z_window: int = 96
    squeeze_window: int = 48
    squeeze_pct_window: int = 240
    squeeze_lookback: int = 12
    fvg_min_atr: float = 0.18
    displacement_atr: float = 0.60
    min_er: float = 0.18
    min_volume_z: float = 0.35
    min_score: float = 5.0
    min_score_delta: float = 1.0
    min_atr_pct: float = 0.04
    max_atr_pct: float = 0.96
    squeeze_max_pct: float = 0.35
    vwap_extreme_atr: float = 1.25
    stop_atr_mult: float = 1.35
    stop_buffer_atr: float = 0.15
    take_profit_r: float = 2.0
    max_funding_against: float = 0.0008
    allowed_sides: tuple[str, ...] = ()
    allowed_families: tuple[str, ...] = ()


def quant_signal_pack_warmup_bars(params: QuantSignalPackParams) -> int:
    return max(
        params.structure_window + 3,
        params.liquidity_window + 3,
        params.atr_window + params.atr_pct_window,
        params.ema_slow + 1,
        params.er_window + 1,
        params.vwap_window + 1,
        params.volume_z_window + 1,
        params.squeeze_window + params.squeeze_pct_window,
    )


def add_quant_signal_pack_columns(
    candles: pd.DataFrame,
    params: QuantSignalPackParams = QuantSignalPackParams(),
) -> pd.DataFrame:
    """Add causal signal-pack columns to a candle frame."""
    df = candles.copy()
    df["atr"] = atr(df, params.atr_window)
    df["atr_pct"] = rolling_percentile(df["atr"], params.atr_pct_window)
    df["er"] = efficiency_ratio(df["close"], params.er_window)
    df["ema_fast"] = ema(df["close"], params.ema_fast)
    df["ema_mid"] = ema(df["close"], params.ema_mid)
    df["ema_slow"] = ema(df["close"], params.ema_slow)
    df["volume_z"] = zscore(df["volume"], params.volume_z_window)

    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    vol_sum = df["volume"].rolling(params.vwap_window).sum()
    df["rolling_vwap"] = (
        (typical * df["volume"]).rolling(params.vwap_window).sum()
        / vol_sum.replace(0.0, float("nan"))
    )
    df["vwap_distance_atr"] = (
        (df["close"] - df["rolling_vwap"]) / df["atr"].replace(0.0, float("nan"))
    )

    df["prior_high"] = prior_high(df["high"], params.structure_window)
    df["prior_low"] = prior_low(df["low"], params.structure_window)
    df["liquidity_high"] = prior_high(df["high"], params.liquidity_window)
    df["liquidity_low"] = prior_low(df["low"], params.liquidity_window)
    df["range_mid"] = (df["prior_high"] + df["prior_low"]) / 2.0
    df["in_discount"] = df["close"] <= df["range_mid"]
    df["in_premium"] = df["close"] >= df["range_mid"]

    df["bias_long"] = (
        (df["close"] > df["ema_fast"])
        & (df["ema_fast"] > df["ema_mid"])
        & (df["ema_mid"] >= df["ema_slow"])
        & (df["er"] >= params.min_er)
    )
    df["bias_short"] = (
        (df["close"] < df["ema_fast"])
        & (df["ema_fast"] < df["ema_mid"])
        & (df["ema_mid"] <= df["ema_slow"])
        & (df["er"] >= params.min_er)
    )
    df["bos_up"] = df["close"] > df["prior_high"]
    df["bos_down"] = df["close"] < df["prior_low"]
    df["choch_up"] = df["bos_up"] & (df["ema_fast"].shift(1) < df["ema_mid"].shift(1))
    df["choch_down"] = df["bos_down"] & (
        df["ema_fast"].shift(1) > df["ema_mid"].shift(1)
    )

    df["sweep_low"] = (
        (df["low"] < df["liquidity_low"])
        & (df["close"] > df["liquidity_low"])
        & (df["close"] > df["open"])
    )
    df["sweep_high"] = (
        (df["high"] > df["liquidity_high"])
        & (df["close"] < df["liquidity_high"])
        & (df["close"] < df["open"])
    )

    body = (df["close"] - df["open"]).abs()
    df["body_atr"] = body / df["atr"].replace(0.0, float("nan"))
    df["displacement_up"] = (df["close"] > df["open"]) & (
        df["body_atr"] >= params.displacement_atr
    )
    df["displacement_down"] = (df["close"] < df["open"]) & (
        df["body_atr"] >= params.displacement_atr
    )
    df["volume_impulse"] = df["volume_z"] >= params.min_volume_z

    bull_gap = df["low"] - df["high"].shift(2)
    bear_gap = df["low"].shift(2) - df["high"]
    df["bullish_fvg_created"] = (
        (bull_gap >= df["atr"] * params.fvg_min_atr) & df["displacement_up"]
    )
    df["bearish_fvg_created"] = (
        (bear_gap >= df["atr"] * params.fvg_min_atr) & df["displacement_down"]
    )
    bull_floor = df["high"].shift(2).where(df["bullish_fvg_created"])
    bull_ceiling = df["low"].where(df["bullish_fvg_created"])
    bear_floor = df["high"].where(df["bearish_fvg_created"])
    bear_ceiling = df["low"].shift(2).where(df["bearish_fvg_created"])
    df["active_bull_fvg_floor"] = bull_floor.ffill().shift(1)
    df["active_bull_fvg_ceiling"] = bull_ceiling.ffill().shift(1)
    df["active_bear_fvg_floor"] = bear_floor.ffill().shift(1)
    df["active_bear_fvg_ceiling"] = bear_ceiling.ffill().shift(1)
    df["bullish_fvg_retest"] = (
        df["active_bull_fvg_ceiling"].notna()
        & (df["low"] <= df["active_bull_fvg_ceiling"])
        & (df["close"] > df["active_bull_fvg_floor"])
        & (df["close"] > df["open"])
    )
    df["bearish_fvg_retest"] = (
        df["active_bear_fvg_floor"].notna()
        & (df["high"] >= df["active_bear_fvg_floor"])
        & (df["close"] < df["active_bear_fvg_ceiling"])
        & (df["close"] < df["open"])
    )
    df["bull_order_block_proxy"] = (
        (df["close"].shift(1) < df["open"].shift(1))
        & df["displacement_up"]
        & (df["close"] > df["high"].shift(1))
    )
    df["bear_order_block_proxy"] = (
        (df["close"].shift(1) > df["open"].shift(1))
        & df["displacement_down"]
        & (df["close"] < df["low"].shift(1))
    )

    basis = sma(df["close"], params.squeeze_window)
    bb_width = (4.0 * df["close"].rolling(params.squeeze_window).std()) / basis
    df["squeeze_pct"] = rolling_percentile(bb_width, params.squeeze_pct_window)
    df["squeeze_recent"] = (
        df["squeeze_pct"]
        .le(params.squeeze_max_pct)
        .rolling(params.squeeze_lookback)
        .max()
        .shift(1)
        .fillna(0.0)
        .astype(bool)
    )
    release_high = prior_high(df["close"], max(6, params.squeeze_lookback))
    release_low = prior_low(df["close"], max(6, params.squeeze_lookback))
    df["squeeze_release_up"] = (
        df["squeeze_recent"]
        & (df["close"] > release_high)
        & df["volume_impulse"]
        & df["displacement_up"]
    )
    df["squeeze_release_down"] = (
        df["squeeze_recent"]
        & (df["close"] < release_low)
        & df["volume_impulse"]
        & df["displacement_down"]
    )

    df["vwap_reclaim_long"] = (
        (df["low"] < df["rolling_vwap"] - params.vwap_extreme_atr * df["atr"])
        & (df["close"] > df["rolling_vwap"])
        & (df["close"] > df["open"])
    )
    df["vwap_reclaim_short"] = (
        (df["high"] > df["rolling_vwap"] + params.vwap_extreme_atr * df["atr"])
        & (df["close"] < df["rolling_vwap"])
        & (df["close"] < df["open"])
    )

    df["volatility_ok"] = (
        (df["atr_pct"] >= params.min_atr_pct)
        & (df["atr_pct"] <= params.max_atr_pct)
    )
    df["long_score"] = (
        2.0 * df["bias_long"].astype(float)
        + 1.5 * (df["bos_up"] | df["choch_up"]).astype(float)
        + 2.0 * df["sweep_low"].astype(float)
        + 1.5 * df["bullish_fvg_retest"].astype(float)
        + 1.0 * df["bull_order_block_proxy"].astype(float)
        + 1.5 * df["squeeze_release_up"].astype(float)
        + 1.5 * df["vwap_reclaim_long"].astype(float)
        + 0.75 * df["volume_impulse"].astype(float)
        + 0.75 * df["displacement_up"].astype(float)
        + 0.5 * df["in_discount"].astype(float)
        + 0.5 * df["volatility_ok"].astype(float)
    )
    df["short_score"] = (
        2.0 * df["bias_short"].astype(float)
        + 1.5 * (df["bos_down"] | df["choch_down"]).astype(float)
        + 2.0 * df["sweep_high"].astype(float)
        + 1.5 * df["bearish_fvg_retest"].astype(float)
        + 1.0 * df["bear_order_block_proxy"].astype(float)
        + 1.5 * df["squeeze_release_down"].astype(float)
        + 1.5 * df["vwap_reclaim_short"].astype(float)
        + 0.75 * df["volume_impulse"].astype(float)
        + 0.75 * df["displacement_down"].astype(float)
        + 0.5 * df["in_premium"].astype(float)
        + 0.5 * df["volatility_ok"].astype(float)
    )
    return df


class QuantSignalPack(BaseStrategy):
    strategy_id = "quant_signal_pack_v1"

    def __init__(
        self,
        funding: pd.DataFrame | None = None,
        *,
        structure_window: int = 48,
        min_score: float = 5.0,
        min_score_delta: float = 1.0,
        min_volume_z: float | None = None,
        stop_atr_mult: float = 1.35,
        take_profit_r: float = 2.0,
        max_funding_against: float = 0.0008,
        allowed_sides: tuple[str, ...] | list[str] | None = None,
        allowed_families: tuple[str, ...] | list[str] | None = None,
        params: QuantSignalPackParams | None = None,
    ) -> None:
        base = params or QuantSignalPackParams()
        self.params = replace(
            base,
            structure_window=structure_window,
            min_score=min_score,
            min_score_delta=min_score_delta,
            min_volume_z=base.min_volume_z if min_volume_z is None else min_volume_z,
            stop_atr_mult=stop_atr_mult,
            take_profit_r=take_profit_r,
            max_funding_against=max_funding_against,
            allowed_sides=_validate_filter(
                "allowed_sides",
                base.allowed_sides if allowed_sides is None else tuple(allowed_sides),
                QUANT_SIGNAL_SIDES,
            ),
            allowed_families=_validate_filter(
                "allowed_families",
                base.allowed_families
                if allowed_families is None
                else tuple(allowed_families),
                QUANT_SIGNAL_FAMILIES,
            ),
        )
        self.funding = funding
        self.warmup_bars = quant_signal_pack_warmup_bars(self.params)

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        return merge_funding(
            add_quant_signal_pack_columns(candles, self.params),
            self.funding,
        )

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        row = df.iloc[index]
        required = ("atr", "atr_pct", "long_score", "short_score", "prior_high", "prior_low")
        if any(math.isnan(float(row[col])) for col in required):
            return None
        if not bool(row["volatility_ok"]):
            return None
        close = float(row["close"])
        atr_value = float(row["atr"])
        if atr_value <= 0:
            return None
        funding_rate = float(row["funding_rate"])
        long_score = float(row["long_score"])
        short_score = float(row["short_score"])

        if (
            self._side_allowed("long")
            and long_score >= self.params.min_score
            and long_score >= short_score + self.params.min_score_delta
            and funding_rate <= self.params.max_funding_against
        ):
            family = _dominant_family("long", row)
            if not self._family_allowed(family):
                return None
            stop = self._long_stop(row, close, atr_value)
            if stop <= 0 or stop >= close:
                return None
            target = close + self.params.take_profit_r * (close - stop)
            return SignalIntent(
                "long",
                stop_price=stop,
                take_profit_price=target,
                reason=self._reason("long", row, long_score, short_score),
            )

        if (
            self._side_allowed("short")
            and short_score >= self.params.min_score
            and short_score >= long_score + self.params.min_score_delta
            and funding_rate >= -self.params.max_funding_against
        ):
            family = _dominant_family("short", row)
            if not self._family_allowed(family):
                return None
            stop = self._short_stop(row, close, atr_value)
            if stop <= close:
                return None
            target = close - self.params.take_profit_r * (stop - close)
            return SignalIntent(
                "short",
                stop_price=stop,
                take_profit_price=target,
                reason=self._reason("short", row, long_score, short_score),
            )
        return None

    def _long_stop(self, row: pd.Series, close: float, atr_value: float) -> float:
        structure_stop = min(float(row["low"]), close - 0.5 * atr_value)
        return structure_stop - self.params.stop_buffer_atr * atr_value

    def _short_stop(self, row: pd.Series, close: float, atr_value: float) -> float:
        structure_stop = max(float(row["high"]), close + 0.5 * atr_value)
        return structure_stop + self.params.stop_buffer_atr * atr_value

    def _side_allowed(self, side: str) -> bool:
        return not self.params.allowed_sides or side in self.params.allowed_sides

    def _family_allowed(self, family: str) -> bool:
        return (
            not self.params.allowed_families
            or family in self.params.allowed_families
        )

    @staticmethod
    def _reason(side: str, row: pd.Series, long_score: float, short_score: float) -> str:
        names = (
            "bias_long" if side == "long" else "bias_short",
            "bos_up" if side == "long" else "bos_down",
            "choch_up" if side == "long" else "choch_down",
            "sweep_low" if side == "long" else "sweep_high",
            "bullish_fvg_retest" if side == "long" else "bearish_fvg_retest",
            "bull_order_block_proxy" if side == "long" else "bear_order_block_proxy",
            "squeeze_release_up" if side == "long" else "squeeze_release_down",
            "vwap_reclaim_long" if side == "long" else "vwap_reclaim_short",
            "volume_impulse",
        )
        active = [name for name in names if bool(row.get(name, False))]
        family = _dominant_family(side, row)
        return (
            f"quant_signal_pack {side} {family} score L/S={long_score:.1f}/{short_score:.1f}; "
            f"features={','.join(active) or 'none'}; "
            f"atrPct={float(row['atr_pct']):.2f}; "
            f"vwapDistAtr={float(row['vwap_distance_atr']):+.2f}; "
            f"funding={float(row['funding_rate']):+.4%}; "
            "route=research_only_maker_first"
        )


def _dominant_family(side: str, row: pd.Series) -> str:
    if side == "long":
        checks = (
            ("liquidity_sweep", "sweep_low"),
            ("fvg_retest", "bullish_fvg_retest"),
            ("order_block", "bull_order_block_proxy"),
            ("squeeze_release", "squeeze_release_up"),
            ("vwap_reclaim", "vwap_reclaim_long"),
            ("structure_break", "bos_up"),
        )
    else:
        checks = (
            ("liquidity_sweep", "sweep_high"),
            ("fvg_retest", "bearish_fvg_retest"),
            ("order_block", "bear_order_block_proxy"),
            ("squeeze_release", "squeeze_release_down"),
            ("vwap_reclaim", "vwap_reclaim_short"),
            ("structure_break", "bos_down"),
        )
    for label, column in checks:
        if bool(row.get(column, False)):
            return label
    return "confluence"


def _validate_filter(
    name: str,
    values: tuple[str, ...],
    allowed: tuple[str, ...],
) -> tuple[str, ...]:
    unknown = sorted(set(values) - set(allowed))
    if unknown:
        raise ValueError(f"{name} contains unknown values: {unknown}")
    return values
