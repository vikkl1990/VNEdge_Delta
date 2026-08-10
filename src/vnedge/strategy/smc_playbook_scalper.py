"""SMC playbook scalper.

This lane encodes the operator playbook explicitly instead of treating SMC
features as a loose score:

1. HTF permission: 1h aligned, 4h not hostile, base candle in discount/premium.
2. Setup: sweep, FVG/order-block retest, and CHoCH/BOS around selected liquidity.
3. Trigger: rejection or displacement from the selected zone.
4. Plan: structural stop, room-to-external-liquidity gate, TP1/TP2/TP3 metadata.

It is not a copy of any TradingView script. Every column is causal and the
strategy remains promotion-gated like every other VNEDGE lane.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import math

import pandas as pd

from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent
from vnedge.strategy.indicators import ema, prior_high, prior_low, zscore
from vnedge.strategy.quant_signal_pack import (
    QuantSignalPackParams,
    add_quant_signal_pack_columns,
)
from vnedge.strategy.regime import merge_funding


SMC_PLAYBOOK_SIDES: tuple[str, ...] = ("long", "short")
SMC_TRIGGER_PROFILES: tuple[str, ...] = ("strict", "momentum", "directional")


@dataclass(frozen=True)
class SMCPlaybookParams:
    structure_window: int = 24
    liquidity_window: int = 64
    setup_lookback: int = 8
    atr_window: int = 14
    atr_pct_window: int = 192
    ema_fast: int = 12
    ema_mid: int = 32
    ema_slow: int = 96
    er_window: int = 32
    volume_z_window: int = 64
    fvg_min_atr: float = 0.12
    displacement_atr: float = 0.45
    min_er: float = 0.10
    min_volume_z: float = -0.25
    min_atr_pct: float = 0.03
    max_atr_pct: float = 0.98
    min_zone_quality: float = 3.0
    min_room_r: float = 1.20
    stop_buffer_atr: float = 0.12
    min_stop_bps: float = 100.0
    take_profit_r: float = 2.0
    max_funding_against: float = 0.0008
    require_1m_trigger: bool = True
    trigger_profile: str = "momentum"
    allowed_sides: tuple[str, ...] = ()


def smc_playbook_warmup_bars(params: SMCPlaybookParams) -> int:
    return max(
        params.structure_window + params.setup_lookback + 3,
        params.liquidity_window * 2 + 3,
        params.atr_window + params.atr_pct_window,
        params.ema_slow + 1,
        params.er_window + 1,
        params.volume_z_window + 1,
    )


def smc_default_params() -> SMCPlaybookParams:
    return SMCPlaybookParams()


def add_smc_playbook_columns(
    candles: pd.DataFrame,
    params: SMCPlaybookParams = SMCPlaybookParams(),
) -> pd.DataFrame:
    """Add causal SMC playbook features."""
    qparams = QuantSignalPackParams(
        structure_window=params.structure_window,
        liquidity_window=params.liquidity_window,
        atr_window=params.atr_window,
        atr_pct_window=params.atr_pct_window,
        ema_fast=params.ema_fast,
        ema_mid=params.ema_mid,
        ema_slow=params.ema_slow,
        er_window=params.er_window,
        volume_z_window=params.volume_z_window,
        fvg_min_atr=params.fvg_min_atr,
        displacement_atr=params.displacement_atr,
        min_er=params.min_er,
        min_volume_z=params.min_volume_z,
        min_atr_pct=params.min_atr_pct,
        max_atr_pct=params.max_atr_pct,
        stop_buffer_atr=params.stop_buffer_atr,
        take_profit_r=params.take_profit_r,
        max_funding_against=params.max_funding_against,
    )
    df = add_quant_signal_pack_columns(candles, qparams)
    lookback = max(params.setup_lookback, 1)
    liquidity_target = max(params.liquidity_window * 2, params.liquidity_window + 1)

    df["external_liquidity_high"] = prior_high(df["high"], liquidity_target)
    df["external_liquidity_low"] = prior_low(df["low"], liquidity_target)
    df["dealing_mid"] = (df["external_liquidity_high"] + df["external_liquidity_low"]) / 2.0
    df["smc_in_discount"] = df["close"] <= df["dealing_mid"]
    df["smc_in_premium"] = df["close"] >= df["dealing_mid"]

    lower_wick = df[["open", "close"]].min(axis=1) - df["low"]
    upper_wick = df["high"] - df[["open", "close"]].max(axis=1)
    wick_floor = 0.20 * df["atr"]
    df["smc_rejection_long"] = (
        (df["close"] > df["open"])
        & (lower_wick >= wick_floor)
        & (df["close"] > df["low"] + 0.50 * (df["high"] - df["low"]))
    )
    df["smc_rejection_short"] = (
        (df["close"] < df["open"])
        & (upper_wick >= wick_floor)
        & (df["close"] < df["high"] - 0.50 * (df["high"] - df["low"]))
    )

    df["smc_strong_swing_low"] = (
        df["sweep_low"]
        & (df["displacement_up"] | df["volume_impulse"] | df["choch_up"])
    )
    df["smc_strong_swing_high"] = (
        df["sweep_high"]
        & (df["displacement_down"] | df["volume_impulse"] | df["choch_down"])
    )
    df["smc_weak_swing_high"] = (
        (df["high"] >= df["liquidity_high"])
        & ~df["smc_strong_swing_high"]
    )
    df["smc_weak_swing_low"] = (
        (df["low"] <= df["liquidity_low"])
        & ~df["smc_strong_swing_low"]
    )

    bull_event = df["smc_strong_swing_low"] | df["bull_order_block_proxy"] | df["bullish_fvg_created"]
    bear_event = df["smc_strong_swing_high"] | df["bear_order_block_proxy"] | df["bearish_fvg_created"]
    bull_floor = df["low"].where(bull_event).ffill().shift(1)
    bull_ceiling = df[["open", "close"]].min(axis=1).where(bull_event).ffill().shift(1)
    bear_floor = df[["open", "close"]].max(axis=1).where(bear_event).ffill().shift(1)
    bear_ceiling = df["high"].where(bear_event).ffill().shift(1)

    df["smc_bull_zone_floor"] = df["active_bull_fvg_floor"].combine_first(bull_floor)
    df["smc_bull_zone_ceiling"] = df["active_bull_fvg_ceiling"].combine_first(bull_ceiling)
    df["smc_bear_zone_floor"] = df["active_bear_fvg_floor"].combine_first(bear_floor)
    df["smc_bear_zone_ceiling"] = df["active_bear_fvg_ceiling"].combine_first(bear_ceiling)

    df["smc_bull_zone_touch"] = (
        df["smc_bull_zone_ceiling"].notna()
        & (df["low"] <= df["smc_bull_zone_ceiling"])
        & (df["close"] > df["smc_bull_zone_floor"])
    )
    df["smc_bear_zone_touch"] = (
        df["smc_bear_zone_floor"].notna()
        & (df["high"] >= df["smc_bear_zone_floor"])
        & (df["close"] < df["smc_bear_zone_ceiling"])
    )

    df["smc_sweep_low_recent"] = _recent(df["sweep_low"], lookback)
    df["smc_sweep_high_recent"] = _recent(df["sweep_high"], lookback)
    df["smc_bull_zone_recent"] = _recent(
        df["bullish_fvg_retest"] | df["bull_order_block_proxy"] | df["smc_bull_zone_touch"],
        lookback,
    )
    df["smc_bear_zone_recent"] = _recent(
        df["bearish_fvg_retest"] | df["bear_order_block_proxy"] | df["smc_bear_zone_touch"],
        lookback,
    )
    df["smc_choch_up_recent"] = _recent(df["choch_up"] | df["bos_up"], lookback)
    df["smc_choch_down_recent"] = _recent(df["choch_down"] | df["bos_down"], lookback)

    df["smc_displacement_trigger_long"] = (
        df["displacement_up"] & (df["volume_z"].fillna(0.0) >= params.min_volume_z)
    )
    df["smc_displacement_trigger_short"] = (
        df["displacement_down"] & (df["volume_z"].fillna(0.0) >= params.min_volume_z)
    )
    df["smc_rejection_trigger_long"] = (
        df["smc_rejection_long"] & (df["smc_bull_zone_recent"] | df["smc_sweep_low_recent"])
    )
    df["smc_rejection_trigger_short"] = (
        df["smc_rejection_short"] & (df["smc_bear_zone_recent"] | df["smc_sweep_high_recent"])
    )
    df["smc_trigger_long"] = df["smc_displacement_trigger_long"] | df["smc_rejection_trigger_long"]
    df["smc_trigger_short"] = df["smc_displacement_trigger_short"] | df["smc_rejection_trigger_short"]

    df["smc_long_setup"] = (
        df["smc_sweep_low_recent"]
        & df["smc_bull_zone_recent"]
        & df["smc_choch_up_recent"]
    )
    df["smc_short_setup"] = (
        df["smc_sweep_high_recent"]
        & df["smc_bear_zone_recent"]
        & df["smc_choch_down_recent"]
    )
    df["smc_long_quality"] = (
        df["smc_sweep_low_recent"].astype(float)
        + df["smc_bull_zone_recent"].astype(float)
        + df["smc_choch_up_recent"].astype(float)
        + df["smc_trigger_long"].astype(float)
        + df["smc_in_discount"].astype(float)
        + df["volatility_ok"].astype(float)
    )
    df["smc_short_quality"] = (
        df["smc_sweep_high_recent"].astype(float)
        + df["smc_bear_zone_recent"].astype(float)
        + df["smc_choch_down_recent"].astype(float)
        + df["smc_trigger_short"].astype(float)
        + df["smc_in_premium"].astype(float)
        + df["volatility_ok"].astype(float)
    )
    return df


class SMCPlaybookScalper(BaseStrategy):
    strategy_id = "smc_playbook_scalper_v1"

    def __init__(
        self,
        funding: pd.DataFrame | None = None,
        *,
        context_1h: pd.DataFrame | None = None,
        context_4h: pd.DataFrame | None = None,
        trigger_1m: pd.DataFrame | None = None,
        base_timeframe: str = "15m",
        allowed_sides: tuple[str, ...] | list[str] | None = None,
        structure_window: int = 24,
        liquidity_window: int = 64,
        setup_lookback: int = 8,
        min_zone_quality: float = 3.0,
        min_room_r: float = 1.20,
        stop_buffer_atr: float = 0.12,
        min_stop_bps: float = 100.0,
        take_profit_r: float = 2.0,
        require_1m_trigger: bool = True,
        trigger_profile: str = "momentum",
        params: SMCPlaybookParams | None = None,
    ) -> None:
        base = params or smc_default_params()
        if trigger_profile not in SMC_TRIGGER_PROFILES:
            raise ValueError(
                f"unknown trigger_profile {trigger_profile!r}; "
                f"expected one of {SMC_TRIGGER_PROFILES}"
            )
        self.params = replace(
            base,
            structure_window=structure_window,
            liquidity_window=liquidity_window,
            setup_lookback=setup_lookback,
            min_zone_quality=min_zone_quality,
            min_room_r=min_room_r,
            stop_buffer_atr=stop_buffer_atr,
            min_stop_bps=min_stop_bps,
            take_profit_r=take_profit_r,
            require_1m_trigger=require_1m_trigger,
            trigger_profile=trigger_profile,
            allowed_sides=_validate_filter(
                "allowed_sides",
                base.allowed_sides if allowed_sides is None else tuple(allowed_sides),
                SMC_PLAYBOOK_SIDES,
            ),
        )
        self.funding = funding
        self.context_1h = context_1h
        self.context_4h = context_4h
        self.trigger_1m = trigger_1m
        self.base_timeframe = base_timeframe
        self.warmup_bars = smc_playbook_warmup_bars(self.params)

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        df = merge_funding(
            add_smc_playbook_columns(candles, self.params),
            self.funding,
        )
        df["_decision_ts"] = df["timestamp"] + _timeframe_delta(self.base_timeframe)
        df = _merge_context(df, self.context_1h, "ctx_1h", "1h", self.params)
        df = _merge_context(df, self.context_4h, "ctx_4h", "4h", self.params)
        df = _merge_trigger(df, self.trigger_1m)
        return df.drop(columns=["_decision_ts"])

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        row = df.iloc[index]
        required = (
            "atr",
            "atr_pct",
            "external_liquidity_high",
            "external_liquidity_low",
            "smc_long_quality",
            "smc_short_quality",
            "funding_rate",
        )
        if any(_is_nan(row.get(col)) for col in required):
            return None
        if not bool(row.get("volatility_ok", False)):
            return None

        long_plan = self._plan("long", row)
        short_plan = self._plan("short", row)
        long_ok = self._entry_ok("long", row, long_plan)
        short_ok = self._entry_ok("short", row, short_plan)
        if long_ok and (not short_ok or long_plan["quality"] >= short_plan["quality"]):
            return self._intent("long", row, long_plan)
        if short_ok:
            return self._intent("short", row, short_plan)
        return None

    def synthesize_exit_plan(
        self, df: pd.DataFrame, index: int, side: str, entry_price: float
    ) -> SignalIntent | None:
        if side not in SMC_PLAYBOOK_SIDES:
            return None
        row = df.iloc[index]
        plan = self._plan(side, row, entry_price=entry_price)
        if not _valid_plan(side, entry_price, plan):
            return None
        return self._intent(side, row, plan, entry_price=entry_price)

    def _entry_ok(self, side: str, row: pd.Series, plan: dict) -> bool:
        if not self._side_allowed(side):
            return False
        if not _valid_plan(side, _float(row.get("close")), plan):
            return False
        if plan["room_r"] < self.params.min_room_r:
            return False
        if plan["quality"] < self.params.min_zone_quality:
            return False
        if side == "long":
            return bool(
                row.get("smc_long_setup", False)
                and row.get("smc_trigger_long", False)
                and self._context_allowed("long", row)
                and self._trigger_allowed("long", row)
                and _float(row.get("funding_rate")) <= self.params.max_funding_against
            )
        return bool(
            row.get("smc_short_setup", False)
            and row.get("smc_trigger_short", False)
            and self._context_allowed("short", row)
            and self._trigger_allowed("short", row)
            and _float(row.get("funding_rate")) >= -self.params.max_funding_against
        )

    def _context_allowed(self, side: str, row: pd.Series) -> bool:
        one_aligned = _ctx_aligned(row, "ctx_1h", side)
        one_opposed = _ctx_opposed(row, "ctx_1h", side)
        four_opposed = _ctx_opposed(row, "ctx_4h", side)
        four_aligned = _ctx_aligned(row, "ctx_4h", side)
        if side == "long":
            base_zone = bool(row.get("smc_in_discount", False) or row.get("sweep_low", False))
            htf_zone = bool(row.get("ctx_1h_smc_in_discount", False) and not one_opposed)
        else:
            base_zone = bool(row.get("smc_in_premium", False) or row.get("sweep_high", False))
            htf_zone = bool(row.get("ctx_1h_smc_in_premium", False) and not one_opposed)
        # SMC reversals often start while EMA-style HTF bias is neutral. Allow
        # the 1h premium/discount location to provide context when the 1h/4h
        # structure is not actively hostile; still require the base candle to
        # be in the selected side of the dealing range.
        directional_context = one_aligned or htf_zone
        return bool(directional_context and not four_opposed and (four_aligned or base_zone))

    def _trigger_allowed(self, side: str, row: pd.Series) -> bool:
        if not self.params.require_1m_trigger:
            return True
        value = row.get(_trigger_column(self.params.trigger_profile, side))
        if value is None:
            value = row.get("trigger_1m_long" if side == "long" else "trigger_1m_short")
        if isinstance(value, bool):
            return value
        if pd.isna(value):
            return False
        return bool(value)

    def _plan(
        self,
        side: str,
        row: pd.Series,
        *,
        entry_price: float | None = None,
    ) -> dict:
        close = _float(row.get("close"))
        entry = close if entry_price is None else float(entry_price)
        atr_value = _float(row.get("atr"))
        if side == "long":
            zone_floor = _float(row.get("smc_bull_zone_floor"), default=float("nan"))
            structural = min(_finite_or(zone_floor, _float(row.get("low"))), _float(row.get("low")))
            stop = structural - self.params.stop_buffer_atr * atr_value
            stop = min(stop, entry - _minimum_stop_distance(entry, self.params.min_stop_bps))
            risk = entry - stop
            target_liq = _float(row.get("external_liquidity_high"))
            room = target_liq - entry
            quality = _float(row.get("smc_long_quality"), default=0.0)
        else:
            zone_ceiling = _float(row.get("smc_bear_zone_ceiling"), default=float("nan"))
            structural = max(_finite_or(zone_ceiling, _float(row.get("high"))), _float(row.get("high")))
            stop = structural + self.params.stop_buffer_atr * atr_value
            stop = max(stop, entry + _minimum_stop_distance(entry, self.params.min_stop_bps))
            risk = stop - entry
            target_liq = _float(row.get("external_liquidity_low"))
            room = entry - target_liq
            quality = _float(row.get("smc_short_quality"), default=0.0)
        room_r = room / risk if risk > 0 else float("nan")
        tp1 = entry + (risk if side == "long" else -risk)
        tp2 = entry + (2.0 * risk if side == "long" else -2.0 * risk)
        tp3 = entry + (
            self.params.take_profit_r * risk
            if side == "long"
            else -self.params.take_profit_r * risk
        )
        return {
            "entry": entry,
            "stop": stop,
            "risk": risk,
            "target_liq": target_liq,
            "room_r": room_r,
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3,
            "quality": quality,
        }

    def _intent(
        self,
        side: str,
        row: pd.Series,
        plan: dict,
        *,
        entry_price: float | None = None,
    ) -> SignalIntent:
        entry = _float(row.get("close")) if entry_price is None else float(entry_price)
        features = _active_features(side, row)
        reason = (
            f"smc_playbook_scalper {side}; "
            f"context=HTF_bias+premium_discount; "
            f"setup=sweep+zone+choch; trigger=rejection_or_displacement; "
            f"quality={plan['quality']:.1f}; roomR={plan['room_r']:.2f}; "
            f"entry={entry:.6g}; stop={plan['stop']:.6g}; "
            f"minStopBps={self.params.min_stop_bps:.0f}; "
            f"tp1={plan['tp1']:.6g}; tp2={plan['tp2']:.6g}; tp3={plan['tp3']:.6g}; "
            "be_after_tp1=true; "
            f"external_liquidity={plan['target_liq']:.6g}; "
            f"features={','.join(features) or 'none'}; "
            f"funding={_float(row.get('funding_rate')):+.4%}; "
            "route=research_only_maker_first"
        )
        return SignalIntent(
            side=side,
            stop_price=float(plan["stop"]),
            take_profit_price=float(plan["tp3"]),
            reason=reason,
        )

    def _side_allowed(self, side: str) -> bool:
        return not self.params.allowed_sides or side in self.params.allowed_sides


def _recent(series: pd.Series, lookback: int) -> pd.Series:
    return series.fillna(False).astype(float).rolling(lookback, min_periods=1).max().astype(bool)


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
    params: SMCPlaybookParams,
) -> pd.DataFrame:
    if context is None or context.empty:
        return base
    ctx = add_smc_playbook_columns(context, params)
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
        "smc_in_discount",
        "smc_in_premium",
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
    trig["trigger_1m_long"] = trig["trigger_1m_momentum_long"]
    trig["trigger_1m_short"] = trig["trigger_1m_momentum_short"]
    trig["_available_ts"] = trig["timestamp"] + _timeframe_delta("1m")
    trigger_cols = ["trigger_1m_long", "trigger_1m_short"]
    for profile in SMC_TRIGGER_PROFILES:
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


def _trigger_column(profile: str, side: str) -> str:
    return f"trigger_1m_{profile}_{side}"


def _timeframe_delta(timeframe: str) -> pd.Timedelta:
    unit = timeframe[-1].lower()
    amount = int(timeframe[:-1])
    if unit == "m":
        return pd.Timedelta(minutes=amount)
    if unit == "h":
        return pd.Timedelta(hours=amount)
    if unit == "d":
        return pd.Timedelta(days=amount)
    raise ValueError(f"unsupported timeframe {timeframe!r}")


def _active_features(side: str, row: pd.Series) -> list[str]:
    names = (
        (
            "smc_sweep_low_recent",
            "smc_bull_zone_recent",
            "smc_choch_up_recent",
            "smc_trigger_long",
            "smc_in_discount",
            "trigger_1m_long",
        )
        if side == "long"
        else (
            "smc_sweep_high_recent",
            "smc_bear_zone_recent",
            "smc_choch_down_recent",
            "smc_trigger_short",
            "smc_in_premium",
            "trigger_1m_short",
        )
    )
    return [name for name in names if bool(row.get(name, False))]


def _valid_plan(side: str, entry: float, plan: dict) -> bool:
    stop = _float(plan.get("stop"), default=float("nan"))
    tp3 = _float(plan.get("tp3"), default=float("nan"))
    room_r = _float(plan.get("room_r"), default=float("nan"))
    if any(math.isnan(v) or not math.isfinite(v) for v in (entry, stop, tp3, room_r)):
        return False
    if entry <= 0 or stop <= 0 or tp3 <= 0:
        return False
    if side == "long":
        return stop < entry < tp3
    return tp3 < entry < stop


def _float(value, *, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out


def _finite_or(value: float, fallback: float) -> float:
    return value if math.isfinite(value) and not math.isnan(value) else fallback


def _minimum_stop_distance(entry: float, min_stop_bps: float) -> float:
    return max(entry * min_stop_bps / 10_000.0, 0.0)


def _is_nan(value) -> bool:
    try:
        return bool(pd.isna(value) or math.isnan(float(value)))
    except (TypeError, ValueError):
        return True


def _validate_filter(
    name: str,
    values: tuple[str, ...],
    allowed: tuple[str, ...],
) -> tuple[str, ...]:
    unknown = sorted(set(values) - set(allowed))
    if unknown:
        raise ValueError(f"{name} contains unknown values: {unknown}")
    return values
