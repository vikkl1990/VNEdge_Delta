"""Exploratory causal research for two StealthTrail-derived hypotheses.

The supplied Pine indicator is not copied into runtime.  This module tests two
independent, frozen economic contracts built from its useful idea: an adaptive
trend band as context rather than an entry by itself.

``stealthtrail_retest_scalper_v1``
    15m adaptive-band transition -> completed 1h EMA bias -> 5m EMA retest ->
    1m continuation trigger.  Next-open entry, 50% at 1R, remainder at 2.5R,
    stop-first ambiguity, 30 minute time stop, and 14.8 bps round-trip costs.

``stealthtrail_eth_swing_v1``
    ETH-only 1h/4h adaptive transition with completed higher-timeframe bias,
    next-bar entry, wick-aware band stop, 3R target, multi-hour time stop,
    14.8 bps execution costs, and actual causally available funding cashflows.

Both are exploratory research artifacts.  They cannot register a live scanner,
promote a strategy, create a paper manifest, or route an order.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean
from typing import Literal

import numpy as np
import pandas as pd

from vnedge.research.delta_funding_audit import (
    RawFetch,
    audit_raw_funding,
    fetch_raw_funding_candles,
)
from vnedge.research.stealthtrail_supertrend_parity import (
    TrailSignal,
    _atr,
    _rsi,
    compute_default_state,
    detect_default_signals,
)
from vnedge.research.strat_trap_vwap_parity import DEFAULT_CACHE, load_symbol, resample_closed

SCALPER_ID = "stealthtrail_retest_scalper_v1"
SWING_ID = "stealthtrail_eth_swing_v1"
DEFAULT_OUTPUT = Path("research/live_research/stealthtrail_retest_research_latest.json")
DEFAULT_FUNDING_CACHE = Path("research/live_research/stealthtrail_eth_funding_hourly.parquet")


@dataclass(frozen=True)
class RetestConfig:
    cost_bps: float = 14.8
    minimum_target_cost_multiple: float = 4.0
    setup_expiry_minutes: int = 60
    minimum_stop_bps: float = 8.0
    maximum_stop_bps: float = 40.0
    retest_tolerance_atr: float = 0.25
    stop_buffer_atr: float = 0.10
    trigger_rsi: float = 52.0
    final_target_r: float = 2.5
    time_stop_minutes: int = 30


@dataclass(frozen=True)
class SwingConfig:
    cost_bps: float = 14.8
    target_r: float = 3.0
    minimum_stop_bps: float = 20.0
    maximum_stop_bps: float = 350.0
    one_hour_time_stop_hours: int = 72
    four_hour_time_stop_hours: int = 336
    funding_interval_hours: int = 8


DEFAULT_RETEST_CONFIG = RetestConfig()
DEFAULT_SWING_CONFIG = SwingConfig()


@dataclass(frozen=True)
class ResearchTrade:
    scanner_id: str
    symbol: str
    timeframe: str
    side: Literal["long", "short"]
    decision_ts: str
    entry_ts: str
    exit_ts: str
    entry_price: float
    exit_price: float
    exit_reason: str
    hold_minutes: float
    risk_bps: float
    target_bps: float
    mfe_bps: float
    mae_bps: float
    gross_bps: float
    execution_cost_bps: float
    funding_bps: float
    net_bps: float
    partial_tp1: bool


@dataclass
class _RetestSetup:
    signal: TrailSignal
    available_at: pd.Timestamp
    expires_at: pd.Timestamp
    measured_move: float
    retest_extreme: float | None = None
    confirmed_at: pd.Timestamp | None = None
    confirm_atr: float | None = None


def _available(frame: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    result = frame.copy()
    result["available_at"] = result.index + pd.Timedelta(timeframe)
    return result.set_index("available_at", drop=False)


def _ema_bias(frame: pd.DataFrame) -> pd.Series:
    fast = frame["close"].ewm(span=20, adjust=False, min_periods=20).mean()
    slow = frame["close"].ewm(span=50, adjust=False, min_periods=50).mean()
    return pd.Series(np.where(fast > slow, 1, -1), index=frame.index)


def _asof(series: pd.Series, timestamp: pd.Timestamp) -> float | None:
    eligible = series.loc[:timestamp]
    if eligible.empty:
        return None
    value = eligible.iloc[-1]
    return float(value) if np.isfinite(value) else None


def _directional_bps(entry: float, price: float, side: str) -> float:
    return ((price / entry - 1.0) if side == "long" else (1.0 - price / entry)) * 10_000.0


def _path_excursions(row: pd.Series, entry: float, side: str) -> tuple[float, float]:
    if side == "long":
        return (float(row.high) / entry - 1.0) * 10_000.0, (1.0 - float(row.low) / entry) * 10_000.0
    return (1.0 - float(row.low) / entry) * 10_000.0, (float(row.high) / entry - 1.0) * 10_000.0


def _summarize(rows: Sequence[ResearchTrade]) -> dict[str, object]:
    if not rows:
        return {"trades": 0, "average_net_bps": None, "profit_factor": 0.0}
    net = [row.net_bps for row in rows]
    gains = sum(value for value in net if value > 0)
    losses = abs(sum(value for value in net if value < 0))
    return {
        "trades": len(rows),
        "longs": sum(row.side == "long" for row in rows),
        "shorts": sum(row.side == "short" for row in rows),
        "win_rate": fmean(value > 0 for value in net),
        "average_gross_bps": fmean(row.gross_bps for row in rows),
        "average_funding_bps": fmean(row.funding_bps for row in rows),
        "average_net_bps": fmean(net),
        "total_net_bps": sum(net),
        "profit_factor": gains / losses if losses else (None if gains else 0.0),
        "average_mfe_bps": fmean(row.mfe_bps for row in rows),
        "average_mae_bps": fmean(row.mae_bps for row in rows),
        "median_hold_minutes": float(np.median([row.hold_minutes for row in rows])),
        "tp1_rate": fmean(row.partial_tp1 for row in rows),
        "exit_reasons": {
            reason: sum(row.exit_reason == reason for row in rows)
            for reason in sorted({row.exit_reason for row in rows})
        },
    }


def _finalize_scalper(
    *,
    symbol: str,
    side: Literal["long", "short"],
    decision_ts: pd.Timestamp,
    entry_ts: pd.Timestamp,
    exit_ts: pd.Timestamp,
    entry: float,
    exit_price: float,
    reason: str,
    risk_bps: float,
    target_bps: float,
    mfe: float,
    mae: float,
    tp1: bool,
    config: RetestConfig,
) -> ResearchTrade:
    terminal = _directional_bps(entry, exit_price, side)
    gross = 0.5 * risk_bps + 0.5 * terminal if tp1 else terminal
    return ResearchTrade(
        scanner_id=SCALPER_ID,
        symbol=symbol,
        timeframe="15m/5m/1m",
        side=side,
        decision_ts=decision_ts.isoformat(),
        entry_ts=entry_ts.isoformat(),
        exit_ts=exit_ts.isoformat(),
        entry_price=entry,
        exit_price=exit_price,
        exit_reason=reason,
        hold_minutes=(exit_ts - entry_ts).total_seconds() / 60.0,
        risk_bps=risk_bps,
        target_bps=target_bps,
        mfe_bps=max(0.0, mfe),
        mae_bps=max(0.0, mae),
        gross_bps=gross,
        execution_cost_bps=config.cost_bps,
        funding_bps=0.0,
        net_bps=gross - config.cost_bps,
        partial_tp1=tp1,
    )


def _resolve_scalper_bar(
    active: dict[str, object],
    row: pd.Series,
    close_ts: pd.Timestamp,
    *,
    symbol: str,
    config: RetestConfig,
) -> ResearchTrade | None:
    side = str(active["side"])
    entry = float(active["entry"])
    favorable, adverse = _path_excursions(row, entry, side)
    active["mfe"] = max(float(active["mfe"]), favorable)
    active["mae"] = max(float(active["mae"]), adverse)
    stop = float(active["stop"])
    tp1_price = float(active["tp1_price"])
    tp2_price = float(active["tp2_price"])
    tp1_done = bool(active["tp1_done"])
    stop_hit = float(row.low) <= stop if side == "long" else float(row.high) >= stop
    tp2_hit = float(row.high) >= tp2_price if side == "long" else float(row.low) <= tp2_price
    tp1_hit = float(row.high) >= tp1_price if side == "long" else float(row.low) <= tp1_price
    if stop_hit:
        exit_price, reason = stop, "break_even" if tp1_done else "stop"
    elif tp2_hit:
        exit_price, reason = tp2_price, "tp2"
        active["tp1_done"] = True
    elif close_ts - pd.Timestamp(active["entry_ts"]) >= pd.Timedelta(
        minutes=config.time_stop_minutes
    ):
        exit_price, reason = float(row.close), "time_stop"
    else:
        if tp1_hit and not tp1_done:
            active["tp1_done"] = True
            # Ratchet from the next bar; the current OHLC ordering is unknown.
            active["stop"] = entry
        return None
    return _finalize_scalper(
        symbol=symbol,
        side=side,  # type: ignore[arg-type]
        decision_ts=pd.Timestamp(active["decision_ts"]),
        entry_ts=pd.Timestamp(active["entry_ts"]),
        exit_ts=close_ts,
        entry=entry,
        exit_price=exit_price,
        reason=reason,
        risk_bps=float(active["risk_bps"]),
        target_bps=float(active["target_bps"]),
        mfe=float(active["mfe"]),
        mae=float(active["mae"]),
        tp1=bool(active["tp1_done"]),
        config=config,
    )


def simulate_retest_scalper(
    minute: pd.DataFrame,
    *,
    symbol: str,
    config: RetestConfig = DEFAULT_RETEST_CONFIG,
) -> tuple[list[ResearchTrade], dict[str, int]]:
    """Run the fixed multi-timeframe retest state machine."""

    bars_5m = resample_closed(minute, "5m")
    bars_15m = resample_closed(minute, "15m")
    bars_1h = resample_closed(minute, "1h")
    state_5m = _available(compute_default_state(bars_5m), "5m")
    state_15m, signals_15m = detect_default_signals(bars_15m)
    state_15m = _available(state_15m, "15m")
    bias_1h = _ema_bias(bars_1h)
    bias_1h.index = bias_1h.index + pd.Timedelta(hours=1)
    ema20_5m = bars_5m["close"].ewm(span=20, adjust=False, min_periods=20).mean()
    ema20_5m.index = ema20_5m.index + pd.Timedelta(minutes=5)
    atr_1m = _atr(minute, 14)
    rsi_1m = _rsi(minute["close"], 14)
    signals_by_time = {
        signal.timestamp + pd.Timedelta(minutes=15): signal for signal in signals_15m
    }
    five_by_time = {timestamp: row for timestamp, row in state_5m.iterrows()}
    fifteen_by_time = {timestamp: row for timestamp, row in state_15m.iterrows()}
    funnel = {
        "evaluated_1m": 0,
        "15m_transitions": len(signals_15m),
        "htf_bias_rejected": 0,
        "setups_born": 0,
        "5m_retests": 0,
        "1m_triggers": 0,
        "risk_rejected": 0,
        "target_cost_rejected": 0,
        "active_rejected": 0,
        "entries": 0,
    }
    setup: _RetestSetup | None = None
    pending: dict[str, object] | None = None
    active: dict[str, object] | None = None
    trades: list[ResearchTrade] = []

    for position, (bar_start, row) in enumerate(minute.iterrows()):
        close_ts = pd.Timestamp(bar_start) + pd.Timedelta(minutes=1)
        funnel["evaluated_1m"] += 1

        if active is not None:
            resolved = _resolve_scalper_bar(active, row, close_ts, symbol=symbol, config=config)
            if resolved is not None:
                trades.append(resolved)
                active = None

        opened_on_this_bar = False
        if pending is not None and pd.Timestamp(bar_start) >= pd.Timestamp(pending["entry_ts"]):
            if active is not None:
                funnel["active_rejected"] += 1
            else:
                entry = float(row.open)
                side = str(pending["side"])
                stop = float(pending["stop"])
                risk = entry - stop if side == "long" else stop - entry
                risk_bps = risk / entry * 10_000.0
                measured_target_bps = float(pending["measured_move"]) / entry * 10_000.0
                final_target_bps = min(config.final_target_r * risk_bps, measured_target_bps)
                if not config.minimum_stop_bps <= risk_bps <= config.maximum_stop_bps:
                    funnel["risk_rejected"] += 1
                elif final_target_bps < config.minimum_target_cost_multiple * config.cost_bps:
                    funnel["target_cost_rejected"] += 1
                else:
                    direction = 1.0 if side == "long" else -1.0
                    active = {
                        "side": side,
                        "decision_ts": pending["decision_ts"],
                        "entry_ts": pd.Timestamp(bar_start),
                        "entry": entry,
                        "stop": stop,
                        "tp1_price": entry + direction * risk,
                        "tp2_price": entry + direction * final_target_bps / 10_000.0 * entry,
                        "risk_bps": risk_bps,
                        "target_bps": final_target_bps,
                        "tp1_done": False,
                        "mfe": 0.0,
                        "mae": 0.0,
                    }
                    funnel["entries"] += 1
                    opened_on_this_bar = True
            pending = None

        # Next-open means the entry candle's complete range is exposed to the
        # stop/target.  Never skip it as many visual indicators do.
        if opened_on_this_bar and active is not None:
            resolved = _resolve_scalper_bar(active, row, close_ts, symbol=symbol, config=config)
            if resolved is not None:
                trades.append(resolved)
                active = None

        new_signal = signals_by_time.get(close_ts)
        if new_signal is not None:
            direction = 1 if new_signal.side == "long" else -1
            htf_bias = _asof(bias_1h, close_ts)
            if htf_bias != direction:
                funnel["htf_bias_rejected"] += 1
                setup = None
            else:
                row_15m = fifteen_by_time[close_ts]
                measured_move = float(row_15m.high - row_15m.low)
                prior = state_15m.loc[:close_ts].iloc[-20:]
                if len(prior) >= 2:
                    measured_move = float(prior.high.max() - prior.low.min())
                setup = _RetestSetup(
                    signal=new_signal,
                    available_at=close_ts,
                    expires_at=close_ts + pd.Timedelta(minutes=config.setup_expiry_minutes),
                    measured_move=measured_move,
                )
                funnel["setups_born"] += 1

        if setup is not None and close_ts > setup.expires_at:
            setup = None
        five = five_by_time.get(close_ts)
        if setup is not None and five is not None and close_ts > setup.available_at:
            side = setup.signal.side
            ema20 = _asof(ema20_5m, close_ts)
            # Use only the closed 5m bar currently being processed.
            ema20 = float(five.close) if ema20 is None else ema20
            tolerance = config.retest_tolerance_atr * float(five.atr)
            aligned = int(five.trend) == (1 if side == "long" else -1)
            if side == "long":
                retest = (
                    aligned
                    and float(five.low) <= ema20 + tolerance
                    and float(five.close) > ema20
                    and float(five.close) > float(five.open)
                )
                extreme = float(five.low)
            else:
                retest = (
                    aligned
                    and float(five.high) >= ema20 - tolerance
                    and float(five.close) < ema20
                    and float(five.close) < float(five.open)
                )
                extreme = float(five.high)
            if retest:
                setup.retest_extreme = extreme
                setup.confirm_atr = float(five.atr)
                setup.confirmed_at = close_ts
                funnel["5m_retests"] += 1

        if (
            setup is not None
            and setup.confirmed_at is not None
            and close_ts > setup.confirmed_at
            and position > 0
            and pending is None
        ):
            side = setup.signal.side
            previous = minute.iloc[position - 1]
            atr_value = float(atr_1m.iloc[position])
            rsi_value = float(rsi_1m.iloc[position])
            body_ok = (
                abs(float(row.close - row.open)) >= 0.20 * atr_value
                if np.isfinite(atr_value)
                else False
            )
            if side == "long":
                trigger = (
                    float(row.close) > float(previous.high)
                    and float(row.close) > float(row.open)
                    and rsi_value >= config.trigger_rsi
                )
                stop = float(setup.retest_extreme) - config.stop_buffer_atr * float(
                    setup.confirm_atr
                )
            else:
                trigger = (
                    float(row.close) < float(previous.low)
                    and float(row.close) < float(row.open)
                    and rsi_value <= 100.0 - config.trigger_rsi
                )
                stop = float(setup.retest_extreme) + config.stop_buffer_atr * float(
                    setup.confirm_atr
                )
            if trigger and body_ok:
                funnel["1m_triggers"] += 1
                pending = {
                    "side": side,
                    "decision_ts": close_ts,
                    "entry_ts": close_ts,
                    "stop": stop,
                    "measured_move": setup.measured_move,
                }
                setup = None
    return trades, funnel


def _funding_cashflow_bps(
    funding: pd.DataFrame,
    *,
    side: str,
    entry_ts: pd.Timestamp,
    exit_ts: pd.Timestamp,
    interval_hours: int,
) -> float:
    if funding.empty:
        return 0.0
    checkpoints = pd.date_range(
        entry_ts.ceil(f"{interval_hours}h"), exit_ts, freq=f"{interval_hours}h"
    )
    total = 0.0
    ordered = funding.sort_values("available_at").set_index("available_at")
    for checkpoint in checkpoints:
        values = ordered.loc[:checkpoint, "funding_rate"]
        if not values.empty:
            rate = float(values.iloc[-1])
            total += (1.0 if side == "long" else -1.0) * rate * 10_000.0
    return total


def simulate_eth_swing(
    minute: pd.DataFrame,
    funding: pd.DataFrame,
    *,
    timeframe: Literal["1h", "4h"],
    config: SwingConfig = DEFAULT_SWING_CONFIG,
) -> tuple[list[ResearchTrade], dict[str, int]]:
    bars = resample_closed(minute, timeframe)
    state, signals = detect_default_signals(bars)
    if timeframe == "1h":
        context_tf = "4h"
        context = resample_closed(minute, context_tf)
        context_span = pd.Timedelta(hours=4)
    else:
        context_tf = "1d"
        grouped = minute.resample("1D", label="left", closed="left")
        context = grouped.agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
            source_bars=("close", "count"),
        )
        context = context[context["source_bars"] == 1440].drop(columns="source_bars").dropna()
        context_span = pd.Timedelta(days=1)
    context_bias = _ema_bias(context)
    context_bias.index = context_bias.index + context_span
    span = pd.Timedelta(timeframe)
    time_stop = pd.Timedelta(
        hours=config.one_hour_time_stop_hours
        if timeframe == "1h"
        else config.four_hour_time_stop_hours
    )
    trades: list[ResearchTrade] = []
    funnel = {
        "raw_transitions": len(signals),
        "htf_bias_rejected": 0,
        "risk_rejected": 0,
        "entries": 0,
        "resolved": 0,
    }
    blocked = -1
    for signal in signals:
        entry_position = signal.index + 1
        if signal.index <= blocked or entry_position >= len(state):
            continue
        decision_ts = signal.timestamp + span
        bias = _asof(context_bias, decision_ts)
        direction = 1 if signal.side == "long" else -1
        if bias != direction:
            funnel["htf_bias_rejected"] += 1
            continue
        entry_ts = pd.Timestamp(state.index[entry_position])
        entry = float(state.iloc[entry_position].open)
        stop = signal.band
        risk = entry - stop if direction == 1 else stop - entry
        risk_bps = risk / entry * 10_000.0
        if not config.minimum_stop_bps <= risk_bps <= config.maximum_stop_bps:
            funnel["risk_rejected"] += 1
            continue
        funnel["entries"] += 1
        target = entry + direction * config.target_r * risk
        target_bps = config.target_r * risk_bps
        mfe = mae = 0.0
        for position in range(entry_position, len(state)):
            row = state.iloc[position]
            close_ts = pd.Timestamp(state.index[position]) + span
            favorable, adverse = _path_excursions(row, entry, signal.side)
            mfe, mae = max(mfe, favorable), max(mae, adverse)
            stop_hit = float(row.low) <= stop if direction == 1 else float(row.high) >= stop
            target_hit = float(row.high) >= target if direction == 1 else float(row.low) <= target
            if stop_hit:
                exit_price, reason = stop, "stop"
            elif target_hit:
                exit_price, reason = target, "target"
            elif close_ts - entry_ts >= time_stop:
                exit_price, reason = float(row.close), "time_stop"
            else:
                candidate = float(row.band)
                stop = max(stop, candidate) if direction == 1 else min(stop, candidate)
                continue
            funding_bps = _funding_cashflow_bps(
                funding,
                side=signal.side,
                entry_ts=entry_ts,
                exit_ts=close_ts,
                interval_hours=config.funding_interval_hours,
            )
            gross = _directional_bps(entry, exit_price, signal.side)
            trades.append(
                ResearchTrade(
                    scanner_id=SWING_ID,
                    symbol="ETHUSD",
                    timeframe=timeframe,
                    side=signal.side,
                    decision_ts=decision_ts.isoformat(),
                    entry_ts=entry_ts.isoformat(),
                    exit_ts=close_ts.isoformat(),
                    entry_price=entry,
                    exit_price=exit_price,
                    exit_reason=reason,
                    hold_minutes=(close_ts - entry_ts).total_seconds() / 60.0,
                    risk_bps=risk_bps,
                    target_bps=target_bps,
                    mfe_bps=max(0.0, mfe),
                    mae_bps=max(0.0, mae),
                    gross_bps=gross,
                    execution_cost_bps=config.cost_bps,
                    funding_bps=funding_bps,
                    net_bps=gross - config.cost_bps - funding_bps,
                    partial_tp1=False,
                )
            )
            blocked = position
            funnel["resolved"] += 1
            break
    return trades, funnel


async def load_funding(
    *,
    cache: Path,
    start: datetime,
    end: datetime,
    refresh: bool,
) -> tuple[pd.DataFrame, dict[str, object]]:
    if cache.exists() and not refresh:
        frame = pd.read_parquet(cache)
        frame["available_at"] = pd.to_datetime(frame["available_at"], utc=True)
        return frame, {"source": "cache", "rows": len(frame), "passed": True}
    raw = await fetch_raw_funding_candles("ETHUSD", start, end)
    settings = {
        "expected_cadence_seconds": 3600,
        "raw_to_canonical_divisor": 100.0,
        "maximum_abs_raw_percent_sanity": 5.0,
        "maximum_abs_canonical_fraction_sanity": 0.05,
        "maximum_missing_fraction": 0.001,
    }
    frame, audit = audit_raw_funding(
        RawFetch(raw.symbol, raw.rows, raw.request_count), start, end, settings
    )
    if not audit.get("passed"):
        raise ValueError(f"funding integrity failed: {audit}")
    cache.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(cache, index=False)
    return frame, {"source": "delta_public_api", **audit}


async def run_research(
    *,
    cache_dir: Path = DEFAULT_CACHE,
    funding_cache: Path = DEFAULT_FUNDING_CACHE,
    refresh_funding: bool = False,
) -> tuple[dict[str, object], list[ResearchTrade]]:
    minute_by_symbol = {symbol: load_symbol(cache_dir, symbol) for symbol in ("BTCUSD", "ETHUSD")}
    start = minute_by_symbol["ETHUSD"].index.min().to_pydatetime()
    end = (minute_by_symbol["ETHUSD"].index.max() + pd.Timedelta(hours=1)).to_pydatetime()
    funding, funding_audit = await load_funding(
        cache=funding_cache, start=start, end=end, refresh=refresh_funding
    )
    rows: list[ResearchTrade] = []
    funnels: dict[str, object] = {}
    for symbol, minute in minute_by_symbol.items():
        trades, funnel = simulate_retest_scalper(minute, symbol=symbol)
        rows.extend(trades)
        funnels[symbol] = funnel
    for timeframe in ("1h", "4h"):
        swing_rows, swing_funnel = simulate_eth_swing(
            minute_by_symbol["ETHUSD"], funding, timeframe=timeframe
        )
        rows.extend(swing_rows)
        funnels[f"ETHUSD:{timeframe}:swing"] = swing_funnel

    groups: dict[str, object] = {}
    for scanner in (SCALPER_ID, SWING_ID):
        scanner_rows = [row for row in rows if row.scanner_id == scanner]
        groups[scanner] = _summarize(scanner_rows)
        scanner_timeframes = ("1h", "4h") if scanner == SWING_ID else ("15m/5m/1m",)
        for timeframe in scanner_timeframes:
            groups[f"{scanner}:{timeframe}"] = _summarize(
                [row for row in scanner_rows if row.timeframe == timeframe]
            )
        for symbol in sorted({row.symbol for row in scanner_rows}):
            groups[f"{scanner}:{symbol}"] = _summarize(
                [row for row in scanner_rows if row.symbol == symbol]
            )
    signature = "\n".join(
        f"{row.scanner_id}|{row.symbol}|{row.timeframe}|{row.decision_ts}|{row.side}|{row.exit_reason}|{row.net_bps:.10f}"
        for row in rows
    )
    report = {
        "schema_version": "vnedge.stealthtrail_retest_research.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "contracts": {"scalper": asdict(RetestConfig()), "swing": asdict(SwingConfig())},
        "data_windows": {
            symbol: {
                "start": frame.index.min().isoformat(),
                "end": frame.index.max().isoformat(),
                "rows": len(frame),
            }
            for symbol, frame in minute_by_symbol.items()
        },
        "funding_integrity": funding_audit,
        "funnels": funnels,
        "results": groups,
        "limitations": [
            "exploratory on previously inspected 2025-2026 price history; not untouched evidence",
            "candle OHLC cannot recover intrabar ordering, so stop-first is enforced",
            "no maker-fill assumption and no order route",
        ],
        "deterministic_hash": hashlib.sha256(signature.encode()).hexdigest(),
        "research_only": True,
        "can_trade": False,
        "can_promote": False,
        "order_route": "absent",
    }
    return report, rows


def publish(
    report: dict[str, object], rows: Sequence[ResearchTrade], *, output: Path = DEFAULT_OUTPUT
) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    pd.DataFrame(asdict(row) for row in rows).to_parquet(
        output.with_name(output.stem + "_trades.parquet"), index=False
    )
    return output


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run StealthTrail retest and ETH swing research")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--funding-cache", type=Path, default=DEFAULT_FUNDING_CACHE)
    parser.add_argument("--refresh-funding", action="store_true")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    report, rows = asyncio.run(
        run_research(
            cache_dir=args.cache_dir,
            funding_cache=args.funding_cache,
            refresh_funding=args.refresh_funding,
        )
    )
    print(publish(report, rows, output=args.output))
    print(json.dumps(report["results"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
