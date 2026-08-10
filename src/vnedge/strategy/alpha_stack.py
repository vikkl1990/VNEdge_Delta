"""VNEDGE AlphaStack: causal SMC/confluence research lane.

This is the bot-grade version of the visual signal stacks traders often use
manually: structure breaks, liquidity sweeps, fair-value gaps, trend/momentum
alignment, volume confirmation, and explicit stops/targets. It deliberately
does not import TradingView/Pine logic. Every feature is computed from closed
bars only and must pass walk-forward before it can become even a shadow lane.
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
    zscore,
)
from vnedge.strategy.regime import merge_funding


@dataclass(frozen=True)
class AlphaStackParams:
    structure_window: int = 48
    atr_window: int = 24
    atr_pct_window: int = 240
    ema_fast: int = 21
    ema_slow: int = 55
    vwap_window: int = 96
    er_window: int = 48
    momentum_window: int = 6
    volume_z_window: int = 96
    min_er: float = 0.22
    min_volume_z: float = 0.25
    min_score: float = 5.0
    min_score_delta: float = 1.0
    min_atr_pct: float = 0.05
    max_atr_pct: float = 0.92
    fvg_min_atr: float = 0.20
    displacement_atr: float = 0.75
    equal_level_atr_tolerance: float = 0.20
    stop_atr_mult: float = 1.50
    stop_buffer_atr: float = 0.20
    take_profit_r: float = 2.0
    max_funding_against: float = 0.0007


def alpha_stack_warmup_bars(params: AlphaStackParams) -> int:
    return max(
        params.structure_window + 2,
        params.atr_window + params.atr_pct_window,
        params.ema_slow + 1,
        params.vwap_window + 1,
        params.er_window + 1,
        params.volume_z_window + 1,
    )


def add_alpha_stack_columns(
    candles: pd.DataFrame,
    params: AlphaStackParams = AlphaStackParams(),
) -> pd.DataFrame:
    """Add AlphaStack features using trailing/shifted data only."""
    df = candles.copy()
    df["atr"] = atr(df, params.atr_window)
    df["atr_pct"] = rolling_percentile(df["atr"], params.atr_pct_window)
    df["er"] = efficiency_ratio(df["close"], params.er_window)
    df["ema_fast"] = ema(df["close"], params.ema_fast)
    df["ema_slow"] = ema(df["close"], params.ema_slow)
    df["volume_z"] = zscore(df["volume"], params.volume_z_window)
    df["momentum"] = df["close"].pct_change(params.momentum_window)
    df["macd_proxy"] = ema(df["close"], 12) - ema(df["close"], 26)

    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    vol_sum = df["volume"].rolling(params.vwap_window).sum()
    df["rolling_vwap"] = (
        (typical * df["volume"]).rolling(params.vwap_window).sum()
        / vol_sum.replace(0.0, float("nan"))
    )

    df["prior_high"] = prior_high(df["high"], params.structure_window)
    df["prior_low"] = prior_low(df["low"], params.structure_window)
    df["range_mid"] = (df["prior_high"] + df["prior_low"]) / 2.0
    df["in_discount"] = df["close"] <= df["range_mid"]
    df["in_premium"] = df["close"] >= df["range_mid"]

    df["bos_up"] = df["close"] > df["prior_high"]
    df["bos_down"] = df["close"] < df["prior_low"]
    df["choch_up"] = df["bos_up"] & (df["ema_fast"].shift(1) < df["ema_slow"].shift(1))
    df["choch_down"] = df["bos_down"] & (
        df["ema_fast"].shift(1) > df["ema_slow"].shift(1)
    )

    df["sweep_low"] = (df["low"] < df["prior_low"]) & (df["close"] > df["prior_low"])
    df["sweep_high"] = (df["high"] > df["prior_high"]) & (
        df["close"] < df["prior_high"]
    )
    tolerance = df["atr"].shift(1) * params.equal_level_atr_tolerance
    df["equal_low_pool"] = (df["low"].shift(1) - df["prior_low"].shift(1)).abs() <= tolerance
    df["equal_high_pool"] = (
        df["high"].shift(1) - df["prior_high"].shift(1)
    ).abs() <= tolerance

    bull_gap = df["low"] - df["high"].shift(2)
    bear_gap = df["low"].shift(2) - df["high"]
    df["bullish_fvg"] = bull_gap >= df["atr"] * params.fvg_min_atr
    df["bearish_fvg"] = bear_gap >= df["atr"] * params.fvg_min_atr
    body = (df["close"] - df["open"]).abs()
    df["displacement_up"] = (df["close"] > df["open"]) & (
        body >= df["atr"] * params.displacement_atr
    )
    df["displacement_down"] = (df["close"] < df["open"]) & (
        body >= df["atr"] * params.displacement_atr
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

    df["trend_up"] = (
        (df["ema_fast"] > df["ema_slow"])
        & (df["close"] > df["rolling_vwap"])
        & (df["er"] >= params.min_er)
    )
    df["trend_down"] = (
        (df["ema_fast"] < df["ema_slow"])
        & (df["close"] < df["rolling_vwap"])
        & (df["er"] >= params.min_er)
    )
    df["momentum_up"] = (df["momentum"] > 0) & (df["macd_proxy"] > 0)
    df["momentum_down"] = (df["momentum"] < 0) & (df["macd_proxy"] < 0)
    df["volatility_ok"] = (
        (df["atr_pct"] >= params.min_atr_pct)
        & (df["atr_pct"] <= params.max_atr_pct)
    )
    df["volume_confirmed"] = df["volume_z"] >= params.min_volume_z

    long_structure = df["bos_up"] | df["choch_up"] | df["sweep_low"]
    short_structure = df["bos_down"] | df["choch_down"] | df["sweep_high"]
    long_liquidity = (
        df["sweep_low"]
        | df["equal_low_pool"]
        | df["bullish_fvg"]
        | df["bull_order_block_proxy"]
    )
    short_liquidity = (
        df["sweep_high"]
        | df["equal_high_pool"]
        | df["bearish_fvg"]
        | df["bear_order_block_proxy"]
    )
    df["long_score"] = (
        2.0 * df["trend_up"].astype(float)
        + 2.0 * long_structure.astype(float)
        + 1.5 * long_liquidity.astype(float)
        + 1.0 * df["momentum_up"].astype(float)
        + 1.0 * df["volume_confirmed"].astype(float)
        + 0.5 * df["in_discount"].astype(float)
        + 0.5 * df["volatility_ok"].astype(float)
    )
    df["short_score"] = (
        2.0 * df["trend_down"].astype(float)
        + 2.0 * short_structure.astype(float)
        + 1.5 * short_liquidity.astype(float)
        + 1.0 * df["momentum_down"].astype(float)
        + 1.0 * df["volume_confirmed"].astype(float)
        + 0.5 * df["in_premium"].astype(float)
        + 0.5 * df["volatility_ok"].astype(float)
    )
    return df


class AlphaStackConfluence(BaseStrategy):
    strategy_id = "alpha_stack_confluence_v1"

    def __init__(
        self,
        funding: pd.DataFrame | None = None,
        *,
        structure_window: int = 48,
        min_score: float = 5.0,
        min_score_delta: float = 1.0,
        stop_atr_mult: float = 1.5,
        take_profit_r: float = 2.0,
        max_funding_against: float = 0.0007,
        params: AlphaStackParams | None = None,
    ) -> None:
        base = params or AlphaStackParams()
        self.params = replace(
            base,
            structure_window=structure_window,
            min_score=min_score,
            min_score_delta=min_score_delta,
            stop_atr_mult=stop_atr_mult,
            take_profit_r=take_profit_r,
            max_funding_against=max_funding_against,
        )
        self.funding = funding
        self.warmup_bars = alpha_stack_warmup_bars(self.params)

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        return merge_funding(add_alpha_stack_columns(candles, self.params), self.funding)

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
        long_score = float(row["long_score"])
        short_score = float(row["short_score"])
        funding_rate = float(row["funding_rate"])

        if (
            long_score >= self.params.min_score
            and long_score >= short_score + self.params.min_score_delta
            and funding_rate <= self.params.max_funding_against
        ):
            stop = self._long_stop(row, close, atr_value)
            if stop <= 0 or stop >= close:
                return None
            risk = close - stop
            target = max(
                close + self.params.take_profit_r * risk,
                float(row["prior_high"]) if float(row["prior_high"]) > close else close,
            )
            return SignalIntent(
                "long",
                stop_price=stop,
                take_profit_price=target,
                reason=self._reason("long", row, long_score, short_score),
            )

        if (
            short_score >= self.params.min_score
            and short_score >= long_score + self.params.min_score_delta
            and funding_rate >= -self.params.max_funding_against
        ):
            stop = self._short_stop(row, close, atr_value)
            if stop <= close:
                return None
            risk = stop - close
            target = min(
                close - self.params.take_profit_r * risk,
                float(row["prior_low"]) if float(row["prior_low"]) < close else close,
            )
            return SignalIntent(
                "short",
                stop_price=stop,
                take_profit_price=target,
                reason=self._reason("short", row, long_score, short_score),
            )
        return None

    def _long_stop(self, row: pd.Series, close: float, atr_value: float) -> float:
        structure_anchor = min(float(row["prior_low"]), float(row["low"]))
        structure_stop = structure_anchor - self.params.stop_buffer_atr * atr_value
        atr_stop = close - self.params.stop_atr_mult * atr_value
        return min(structure_stop, atr_stop)

    def _short_stop(self, row: pd.Series, close: float, atr_value: float) -> float:
        structure_anchor = max(float(row["prior_high"]), float(row["high"]))
        structure_stop = structure_anchor + self.params.stop_buffer_atr * atr_value
        atr_stop = close + self.params.stop_atr_mult * atr_value
        return max(structure_stop, atr_stop)

    @staticmethod
    def _reason(side: str, row: pd.Series, long_score: float, short_score: float) -> str:
        active = [
            name
            for name in (
                "trend_up" if side == "long" else "trend_down",
                "bos_up" if side == "long" else "bos_down",
                "choch_up" if side == "long" else "choch_down",
                "sweep_low" if side == "long" else "sweep_high",
                "bullish_fvg" if side == "long" else "bearish_fvg",
                "bull_order_block_proxy" if side == "long" else "bear_order_block_proxy",
                "volume_confirmed",
                "momentum_up" if side == "long" else "momentum_down",
            )
            if bool(row.get(name, False))
        ]
        return (
            f"alpha_stack {side} score L/S={long_score:.1f}/{short_score:.1f}; "
            f"features={','.join(active) or 'none'}; "
            f"atrPct={float(row['atr_pct']):.2f}; "
            f"funding={float(row['funding_rate']):+.4%}; "
            "route=research_only_maker_first"
        )
