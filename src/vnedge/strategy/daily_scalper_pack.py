"""Daily scalper pack.

This is the practical VNEDGE daily-scalping lane: 4h/1h context, 15m setup,
1m trigger confirmation, and isolated Quant Signal Pack families. It is not a
tick-HFT scalper. It aims for a handful of intraday trades that survive fees
and the normal promotion gates.
"""

from __future__ import annotations

from dataclasses import replace
import math

import pandas as pd

from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent
from vnedge.strategy.indicators import ema, zscore
from vnedge.strategy.quant_signal_pack import (
    QuantSignalPack,
    QuantSignalPackParams,
    add_quant_signal_pack_columns,
)


DAILY_SCALPER_FAMILIES: tuple[str, ...] = (
    "structure_break",
    "order_block",
    "squeeze_release",
    "fvg_retest",
)
TRIGGER_PROFILES: tuple[str, ...] = ("strict", "momentum", "directional")


def daily_scalper_default_params() -> QuantSignalPackParams:
    """15m-native defaults. These are deliberately shorter than the 1h research
    pack while still requiring enough history to avoid one-candle noise."""
    return QuantSignalPackParams(
        structure_window=32,
        liquidity_window=48,
        atr_window=14,
        atr_pct_window=192,
        ema_fast=12,
        ema_mid=32,
        ema_slow=96,
        er_window=32,
        vwap_window=64,
        volume_z_window=64,
        squeeze_window=32,
        squeeze_pct_window=192,
        squeeze_lookback=8,
        fvg_min_atr=0.15,
        displacement_atr=0.50,
        min_er=0.12,
        min_volume_z=0.25,
        min_score=4.5,
        min_score_delta=0.75,
        min_atr_pct=0.04,
        max_atr_pct=0.97,
        squeeze_max_pct=0.35,
        vwap_extreme_atr=1.10,
        stop_atr_mult=1.10,
        stop_buffer_atr=0.10,
        take_profit_r=1.50,
        max_funding_against=0.0008,
    )


class DailyScalperPack(BaseStrategy):
    strategy_id = "daily_scalper_pack_v1"

    def __init__(
        self,
        funding: pd.DataFrame | None = None,
        *,
        context_1h: pd.DataFrame | None = None,
        context_4h: pd.DataFrame | None = None,
        trigger_1m: pd.DataFrame | None = None,
        allowed_families: tuple[str, ...] | list[str] | None = None,
        allowed_sides: tuple[str, ...] | list[str] | None = None,
        structure_window: int = 32,
        min_score: float = 4.5,
        min_score_delta: float = 0.75,
        stop_atr_mult: float = 1.10,
        take_profit_r: float = 1.50,
        require_1m_trigger: bool = True,
        trigger_profile: str = "strict",
        params: QuantSignalPackParams | None = None,
    ) -> None:
        if trigger_profile not in TRIGGER_PROFILES:
            raise ValueError(
                f"unknown trigger_profile {trigger_profile!r}; "
                f"expected one of {TRIGGER_PROFILES}"
            )
        self.funding = funding
        self.context_1h = context_1h
        self.context_4h = context_4h
        self.trigger_1m = trigger_1m
        self.require_1m_trigger = require_1m_trigger
        self.trigger_profile = trigger_profile
        base_params = params or daily_scalper_default_params()
        base_params = replace(
            base_params,
            allowed_families=tuple(allowed_families or base_params.allowed_families),
            allowed_sides=tuple(allowed_sides or base_params.allowed_sides),
        )
        self._base = QuantSignalPack(
            funding=funding,
            structure_window=structure_window,
            min_score=min_score,
            min_score_delta=min_score_delta,
            stop_atr_mult=stop_atr_mult,
            take_profit_r=take_profit_r,
            allowed_families=base_params.allowed_families,
            allowed_sides=base_params.allowed_sides,
            params=base_params,
        )
        self.warmup_bars = self._base.warmup_bars

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        df = self._base.prepare(candles).copy()
        df["_decision_ts"] = df["timestamp"] + _timeframe_delta("15m")
        df = _merge_context(df, self.context_1h, "ctx_1h", "1h")
        df = _merge_context(df, self.context_4h, "ctx_4h", "4h")
        df = _merge_trigger(df, self.trigger_1m)
        return df.drop(columns=["_decision_ts"])

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        intent = self._base.signal(df, index)
        if intent is None:
            return None
        row = df.iloc[index]
        if not self._context_allowed(intent.side, row):
            return None
        if not self._trigger_allowed(intent.side, row):
            return None
        reason = (
            f"daily_scalper_pack {intent.side}; "
            f"context=4h/1h_aligned; trigger=1m_confirmed; {intent.reason}"
        )
        return SignalIntent(
            side=intent.side,
            stop_price=intent.stop_price,
            take_profit_price=intent.take_profit_price,
            reason=reason,
        )

    @staticmethod
    def _context_allowed(side: str, row: pd.Series) -> bool:
        one_aligned = _ctx_aligned(row, "ctx_1h", side)
        four_opposed = _ctx_opposed(row, "ctx_4h", side)
        four_aligned = _ctx_aligned(row, "ctx_4h", side)
        if side == "long":
            not_extreme = _float(row.get("ctx_1h_atr_pct")) <= 0.98
        else:
            not_extreme = _float(row.get("ctx_1h_atr_pct")) <= 0.98
        return bool(one_aligned and not four_opposed and (four_aligned or not_extreme))

    def _trigger_allowed(self, side: str, row: pd.Series) -> bool:
        if not self.require_1m_trigger:
            return True
        col = _trigger_column(self.trigger_profile, side)
        value = row.get(col)
        if value is None and self.trigger_profile == "strict":
            value = row.get("trigger_1m_long" if side == "long" else "trigger_1m_short")
        if isinstance(value, bool):
            return value
        if pd.isna(value):
            return not self.require_1m_trigger
        return bool(value)


def _ctx_aligned(row: pd.Series, prefix: str, side: str) -> bool:
    if side == "long":
        return bool(
            row.get(f"{prefix}_bias_long", False)
            or row.get(f"{prefix}_bos_up", False)
            or row.get(f"{prefix}_choch_up", False)
        )
    return bool(
        row.get(f"{prefix}_bias_short", False)
        or row.get(f"{prefix}_bos_down", False)
        or row.get(f"{prefix}_choch_down", False)
    )


def _ctx_opposed(row: pd.Series, prefix: str, side: str) -> bool:
    if side == "long":
        return bool(row.get(f"{prefix}_bias_short", False) and row.get(f"{prefix}_bos_down", False))
    return bool(row.get(f"{prefix}_bias_long", False) and row.get(f"{prefix}_bos_up", False))


def _merge_context(
    base: pd.DataFrame,
    context: pd.DataFrame | None,
    prefix: str,
    timeframe: str,
) -> pd.DataFrame:
    if context is None or context.empty:
        return base
    ctx = add_quant_signal_pack_columns(context, daily_scalper_default_params())
    keep = [
        "timestamp",
        "bias_long",
        "bias_short",
        "bos_up",
        "bos_down",
        "choch_up",
        "choch_down",
        "atr_pct",
        "er",
    ]
    ctx = ctx[keep].copy()
    ctx["_available_ts"] = ctx["timestamp"] + _timeframe_delta(timeframe)
    ctx = ctx.drop(columns=["timestamp"]).rename(
        columns={c: f"{prefix}_{c}" for c in keep if c != "timestamp"}
    )
    out = pd.merge_asof(
        base.sort_values("_decision_ts"),
        ctx.sort_values("_available_ts"),
        left_on="_decision_ts",
        right_on="_available_ts",
        direction="backward",
    )
    return out.drop(columns=["_available_ts"])


def _merge_trigger(base: pd.DataFrame, trigger_1m: pd.DataFrame | None) -> pd.DataFrame:
    if trigger_1m is None or trigger_1m.empty:
        return base
    trig = trigger_1m.copy()
    trig["m1_ema_fast"] = ema(trig["close"], 9)
    trig["m1_ema_mid"] = ema(trig["close"], 21)
    trig["m1_momentum_3"] = trig["close"] - trig["close"].shift(3)
    trig["m1_volume_z"] = zscore(trig["volume"], 60)
    trig["trigger_1m_strict_long"] = (
        (trig["close"] > trig["m1_ema_fast"])
        & (trig["m1_ema_fast"] >= trig["m1_ema_mid"])
        & (trig["m1_momentum_3"] > 0)
        & (trig["m1_volume_z"].fillna(0.0) >= -0.25)
    )
    trig["trigger_1m_strict_short"] = (
        (trig["close"] < trig["m1_ema_fast"])
        & (trig["m1_ema_fast"] <= trig["m1_ema_mid"])
        & (trig["m1_momentum_3"] < 0)
        & (trig["m1_volume_z"].fillna(0.0) >= -0.25)
    )
    trig["trigger_1m_momentum_long"] = (
        (trig["close"] > trig["m1_ema_fast"])
        & (trig["m1_momentum_3"] > 0)
        & (trig["m1_volume_z"].fillna(0.0) >= -0.50)
    )
    trig["trigger_1m_momentum_short"] = (
        (trig["close"] < trig["m1_ema_fast"])
        & (trig["m1_momentum_3"] < 0)
        & (trig["m1_volume_z"].fillna(0.0) >= -0.50)
    )
    trig["trigger_1m_directional_long"] = (
        (trig["close"] > trig["m1_ema_mid"])
        & (trig["m1_momentum_3"] >= 0)
        & (trig["m1_volume_z"].fillna(0.0) >= -0.75)
    )
    trig["trigger_1m_directional_short"] = (
        (trig["close"] < trig["m1_ema_mid"])
        & (trig["m1_momentum_3"] <= 0)
        & (trig["m1_volume_z"].fillna(0.0) >= -0.75)
    )
    # Backward-compatible aliases used by existing tests and any old artifacts.
    trig["trigger_1m_long"] = trig["trigger_1m_strict_long"]
    trig["trigger_1m_short"] = trig["trigger_1m_strict_short"]
    trig["_available_ts"] = trig["timestamp"] + _timeframe_delta("1m")
    trigger_cols = ["trigger_1m_long", "trigger_1m_short"]
    for profile in TRIGGER_PROFILES:
        trigger_cols.extend(
            [_trigger_column(profile, "long"), _trigger_column(profile, "short")]
        )
    trig = trig[["_available_ts", *dict.fromkeys(trigger_cols)]]
    out = pd.merge_asof(
        base.sort_values("_decision_ts"),
        trig.sort_values("_available_ts"),
        left_on="_decision_ts",
        right_on="_available_ts",
        direction="backward",
    )
    return out.drop(columns=["_available_ts"])


def _timeframe_delta(timeframe: str) -> pd.Timedelta:
    if timeframe.endswith("m"):
        return pd.Timedelta(minutes=int(timeframe[:-1]))
    if timeframe.endswith("h"):
        return pd.Timedelta(hours=int(timeframe[:-1]))
    if timeframe.endswith("d"):
        return pd.Timedelta(days=int(timeframe[:-1]))
    raise ValueError(f"unsupported timeframe: {timeframe}")


def _trigger_column(profile: str, side: str) -> str:
    if profile not in TRIGGER_PROFILES:
        raise ValueError(
            f"unknown trigger_profile {profile!r}; expected one of {TRIGGER_PROFILES}"
        )
    if side not in {"long", "short"}:
        raise ValueError(f"unknown trigger side: {side!r}")
    return f"trigger_1m_{profile}_{side}"


def _float(value) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if math.isnan(out) else out
