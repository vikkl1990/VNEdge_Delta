"""Causal parity audit for WillyAlgoTrader STRAT Trap & VWAP Engine v1.9.0.

The supplied Pine indicator is too stateful for the generic TV rule adapter, so
this module ports its *default trade contract* explicitly:

* Trap Bar entry mode (red 2U -> short, green 2D -> long)
* EMA(21) PVTE basis with ATR(100) and 3x outer / 2x inner bands
* balanced risk geometry, sweep-extreme stop, 1R/2R/3R targets
* one active trade per symbol/timeframe

Two ledgers are produced from the same immutable signals.  ``tv_native``
matches the indicator's optimistic display accounting (signal-close entry,
entry bar ignored, target priority, TP1 touch counted as a win, no costs or
vertical barrier).  ``causal_30m`` uses next-bar-open entry, conservative
stop-first OHLC ambiguity, a 30-minute vertical barrier, and Delta costs.

Nothing in this module can register a scanner, promote it, or route an order.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean
from typing import Literal

import numpy as np
import pandas as pd

SCANNER_ID = "strat_trap_vwap_parity_v1"
DEFAULT_CACHE = Path("data/delta_scalper_cache")
DEFAULT_OUTPUT = Path("research/live_research/strat_trap_vwap_parity_latest.json")
DEFAULT_TIMEFRAMES = ("1m", "5m", "15m", "30m", "1h", "4h")
RESAMPLE_RULES = {
    "1m": "1min",
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "1h": "1h",
    "4h": "4h",
}


@dataclass(frozen=True)
class TrapSignal:
    index: int
    timestamp: pd.Timestamp
    side: Literal["long", "short"]
    signal_close: float
    sweep_extreme: float
    atr: float
    pre_entry_reversal_bps: float


@dataclass(frozen=True)
class TrapOutcome:
    symbol: str
    timeframe: str
    ledger: Literal["tv_native", "causal_30m"]
    signal_ts: str
    side: Literal["long", "short"]
    entry_ts: str
    exit_ts: str
    entry_price: float
    exit_price: float
    exit_reason: str
    bars_held: int
    minutes_held: float
    risk_bps: float
    pre_entry_reversal_bps: float
    mfe_bps: float
    mae_bps: float
    gross_bps: float
    cost_bps: float
    net_bps: float
    tv_win: bool


def _atr(frame: pd.DataFrame, length: int = 14) -> pd.Series:
    previous = frame["close"].shift(1)
    true_range = pd.concat(
        (
            frame["high"] - frame["low"],
            (frame["high"] - previous).abs(),
            (frame["low"] - previous).abs(),
        ),
        axis=1,
    ).max(axis=1)
    return true_range.ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean()


def load_symbol(cache_dir: Path, symbol: str) -> pd.DataFrame:
    """Load, normalize, and de-duplicate all local one-minute shards."""

    paths = sorted(cache_dir.glob(f"{symbol.upper()}_1m_*.parquet"))
    if not paths:
        raise FileNotFoundError(f"no cached candles for {symbol}")
    frames = [
        pd.read_parquet(path, columns=["timestamp", "open", "high", "low", "close", "volume"])
        for path in paths
    ]
    frame = pd.concat(frames, ignore_index=True)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame = (
        frame.sort_values("timestamp")
        .drop_duplicates("timestamp", keep="last")
        .set_index("timestamp")
    )
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["open", "high", "low", "close"])
    frame = frame[(frame[["open", "high", "low", "close"]] > 0).all(axis=1)]
    return frame


def resample_closed(frame: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Causally aggregate left-labelled bars from closed one-minute candles."""

    if timeframe not in RESAMPLE_RULES:
        raise ValueError(f"unsupported timeframe {timeframe!r}")
    if timeframe == "1m":
        return frame.copy()
    grouped = frame.resample(RESAMPLE_RULES[timeframe], origin="epoch", label="left", closed="left")
    result = grouped.agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        source_bars=("close", "count"),
    )
    expected = int(pd.Timedelta(RESAMPLE_RULES[timeframe]) / pd.Timedelta(minutes=1))
    return result[result["source_bars"] == expected].drop(columns="source_bars").dropna()


def detect_default_traps(frame: pd.DataFrame) -> list[TrapSignal]:
    """Port the Pine default Trap-Bar + PVTE gate on confirmed closes."""

    high = frame["high"]
    low = frame["low"]
    close = frame["close"]
    open_ = frame["open"]
    previous_high = high.shift(1)
    previous_low = low.shift(1)
    inside = (high <= previous_high) & (low >= previous_low)
    outside = (high > previous_high) & (low < previous_low)
    cls = np.select(
        [inside, outside, high > previous_high],
        [1, 3, 2],
        default=-2,
    )
    risk_atr = _atr(frame, 14)
    pvte_atr = _atr(frame, 100)
    basis = close.ewm(span=21, adjust=False, min_periods=21).mean()
    outer_up = basis + 3.0 * pvte_atr
    outer_down = basis - 3.0 * pvte_atr
    inner_up = basis + 2.0 * pvte_atr
    inner_down = basis - 2.0 * pvte_atr
    cross_up_outer = (close > outer_up) & (close.shift(1) <= outer_up.shift(1))
    cross_down_outer = (close < outer_down) & (close.shift(1) >= outer_down.shift(1))
    cross_down_inner = (close < inner_down) & (close.shift(1) >= inner_down.shift(1))
    cross_up_inner = (close > inner_up) & (close.shift(1) <= inner_up.shift(1))

    regime = np.zeros(len(frame), dtype=np.int8)
    current = 0
    for position in range(len(frame)):
        # Pine resolves regime exits before entries on every confirmed close.
        if current == 1 and bool(cross_down_inner.iloc[position]):
            current = 0
        if current == -1 and bool(cross_up_inner.iloc[position]):
            current = 0
        if bool(cross_up_outer.iloc[position]):
            current = 1
        if bool(cross_down_outer.iloc[position]):
            current = -1
        regime[position] = current

    signals: list[TrapSignal] = []
    for position, timestamp in enumerate(frame.index):
        atr_value = float(risk_atr.iloc[position])
        if not np.isfinite(atr_value) or atr_value <= 0:
            continue
        classification = int(cls[position])
        trap_short = classification == 2 and close.iloc[position] < open_.iloc[position]
        trap_long = classification == -2 and close.iloc[position] > open_.iloc[position]
        side: Literal["long", "short"] | None = None
        if trap_long and regime[position] == 1:
            side = "long"
            extreme = float(low.iloc[position])
        elif trap_short and regime[position] == -1:
            side = "short"
            extreme = float(high.iloc[position])
        if side is None:
            continue
        signal_close = float(close.iloc[position])
        direction = 1.0 if side == "long" else -1.0
        pre_move = direction * (signal_close / extreme - 1.0) * 10_000.0
        signals.append(
            TrapSignal(
                index=position,
                timestamp=pd.Timestamp(timestamp),
                side=side,
                signal_close=signal_close,
                sweep_extreme=extreme,
                atr=atr_value,
                pre_entry_reversal_bps=max(0.0, pre_move),
            )
        )
    return signals


def _geometry(signal: TrapSignal, entry: float) -> tuple[float, float, float, float, float]:
    direction = 1.0 if signal.side == "long" else -1.0
    if direction > 0:
        stop = min(signal.sweep_extreme - 0.25 * signal.atr, entry - 0.5 * signal.atr)
        risk = entry - stop
    else:
        stop = max(signal.sweep_extreme + 0.25 * signal.atr, entry + 0.5 * signal.atr)
        risk = stop - entry
    return (
        stop,
        entry + direction * risk,
        entry + direction * 2.0 * risk,
        entry + direction * 3.0 * risk,
        risk,
    )


def _bar_span(timeframe: str) -> pd.Timedelta:
    return pd.Timedelta(RESAMPLE_RULES[timeframe])


def simulate_tv_native(
    frame: pd.DataFrame,
    signals: Iterable[TrapSignal],
    *,
    symbol: str,
    timeframe: str,
) -> list[TrapOutcome]:
    """Match the Pine trade engine's display and hit-order semantics."""

    outcomes: list[TrapOutcome] = []
    blocked_through = -1
    bar_span = _bar_span(timeframe)
    for signal in signals:
        if signal.index <= blocked_through:
            continue
        entry = signal.signal_close
        stop, tp1, _tp2, tp3, risk = _geometry(signal, entry)
        if risk <= 0 or risk > 3.0 * signal.atr:
            continue
        direction = 1.0 if signal.side == "long" else -1.0
        tp1_reached = False
        be_active = False
        mfe = 0.0
        mae = 0.0
        resolved = False
        # Pine explicitly refuses to inspect the signal/entry candle.
        for position in range(signal.index + 1, len(frame)):
            row = frame.iloc[position]
            favorable = (
                (float(row.high) / entry - 1.0) * 10_000.0
                if direction > 0
                else (1.0 - float(row.low) / entry) * 10_000.0
            )
            adverse = (
                (1.0 - float(row.low) / entry) * 10_000.0
                if direction > 0
                else (float(row.high) / entry - 1.0) * 10_000.0
            )
            mfe = max(mfe, favorable)
            mae = max(mae, adverse)
            effective_stop = entry if be_active else stop
            stop_hit = (
                float(row.low) <= effective_stop
                if direction > 0
                else float(row.high) >= effective_stop
            )
            tp1_hit = float(row.high) >= tp1 if direction > 0 else float(row.low) <= tp1
            tp3_hit = float(row.high) >= tp3 if direction > 0 else float(row.low) <= tp3
            first_tp1 = tp1_hit and not tp1_reached
            if first_tp1:
                tp1_reached = True
            # TP priority: TP3 wins even when the candle also crosses the stop.
            if tp3_hit:
                exit_price = tp3
                reason = "tp3"
            elif stop_hit and not first_tp1:
                exit_price = effective_stop
                reason = "break_even" if be_active else "stop"
            else:
                if first_tp1:
                    be_active = True
                continue
            signal_ts = signal.timestamp + bar_span
            exit_ts = pd.Timestamp(frame.index[position]) + bar_span
            gross = direction * (exit_price / entry - 1.0) * 10_000.0
            outcomes.append(
                TrapOutcome(
                    symbol=symbol,
                    timeframe=timeframe,
                    ledger="tv_native",
                    signal_ts=signal_ts.isoformat(),
                    side=signal.side,
                    entry_ts=signal_ts.isoformat(),
                    exit_ts=exit_ts.isoformat(),
                    entry_price=entry,
                    exit_price=exit_price,
                    exit_reason=reason,
                    bars_held=position - signal.index,
                    minutes_held=(exit_ts - signal_ts).total_seconds() / 60.0,
                    risk_bps=risk / entry * 10_000.0,
                    pre_entry_reversal_bps=signal.pre_entry_reversal_bps,
                    mfe_bps=max(0.0, mfe),
                    mae_bps=max(0.0, mae),
                    gross_bps=gross,
                    cost_bps=0.0,
                    net_bps=gross,
                    tv_win=tp1_reached,
                )
            )
            blocked_through = position
            resolved = True
            break
        if not resolved:
            blocked_through = len(frame)
    return outcomes


def simulate_causal_30m(
    frame: pd.DataFrame,
    signals: Iterable[TrapSignal],
    *,
    symbol: str,
    timeframe: str,
    cost_bps: float,
) -> list[TrapOutcome]:
    """Next-open, stop-first, cost-aware version capped at 30 minutes."""

    outcomes: list[TrapOutcome] = []
    blocked_through = -1
    bar_span = _bar_span(timeframe)
    for signal in signals:
        entry_position = signal.index + 1
        if signal.index <= blocked_through or entry_position >= len(frame):
            continue
        entry_ts = pd.Timestamp(frame.index[entry_position])
        entry = float(frame.iloc[entry_position].open)
        stop, tp1, _tp2, tp3, risk = _geometry(signal, entry)
        if risk <= 0 or risk > 3.0 * signal.atr:
            continue
        direction = 1.0 if signal.side == "long" else -1.0
        tp1_reached = False
        be_active = False
        mfe = 0.0
        mae = 0.0
        exit_price = entry
        exit_reason = "time_stop"
        exit_position = entry_position
        for position in range(entry_position, len(frame)):
            timestamp = pd.Timestamp(frame.index[position])
            close_timestamp = timestamp + bar_span
            row = frame.iloc[position]
            favorable = (
                (float(row.high) / entry - 1.0) * 10_000.0
                if direction > 0
                else (1.0 - float(row.low) / entry) * 10_000.0
            )
            adverse = (
                (1.0 - float(row.low) / entry) * 10_000.0
                if direction > 0
                else (float(row.high) / entry - 1.0) * 10_000.0
            )
            mfe = max(mfe, favorable)
            mae = max(mae, adverse)
            effective_stop = entry if be_active else stop
            stop_hit = (
                float(row.low) <= effective_stop
                if direction > 0
                else float(row.high) >= effective_stop
            )
            tp1_hit = float(row.high) >= tp1 if direction > 0 else float(row.low) <= tp1
            tp3_hit = float(row.high) >= tp3 if direction > 0 else float(row.low) <= tp3
            # Without tick ordering, adverse-first is the only honest rule.
            if stop_hit:
                exit_price = effective_stop
                exit_reason = "break_even" if be_active else "stop"
                exit_position = position
                break
            if tp3_hit:
                exit_price = tp3
                exit_reason = "tp3"
                tp1_reached = True
                exit_position = position
                break
            if tp1_hit:
                tp1_reached = True
                be_active = True
            if (close_timestamp - entry_ts).total_seconds() >= 30 * 60:
                exit_price = float(row.close)
                exit_reason = "time_stop"
                exit_position = position
                break
        signal_ts = signal.timestamp + bar_span
        exit_ts = pd.Timestamp(frame.index[exit_position]) + bar_span
        gross = direction * (exit_price / entry - 1.0) * 10_000.0
        outcomes.append(
            TrapOutcome(
                symbol=symbol,
                timeframe=timeframe,
                ledger="causal_30m",
                signal_ts=signal_ts.isoformat(),
                side=signal.side,
                entry_ts=entry_ts.isoformat(),
                exit_ts=exit_ts.isoformat(),
                entry_price=entry,
                exit_price=exit_price,
                exit_reason=exit_reason,
                bars_held=exit_position - entry_position + 1,
                minutes_held=(exit_ts - entry_ts).total_seconds() / 60.0,
                risk_bps=risk / entry * 10_000.0,
                pre_entry_reversal_bps=signal.pre_entry_reversal_bps,
                mfe_bps=max(0.0, mfe),
                mae_bps=max(0.0, mae),
                gross_bps=gross,
                cost_bps=cost_bps,
                net_bps=gross - cost_bps,
                tv_win=tp1_reached,
            )
        )
        blocked_through = exit_position
    return outcomes


def summarize(rows: Sequence[TrapOutcome]) -> dict[str, object]:
    if not rows:
        return {"trades": 0, "positive_net": 0, "profit_factor": 0.0}
    net = [row.net_bps for row in rows]
    gains = sum(value for value in net if value > 0)
    losses = abs(sum(value for value in net if value < 0))
    return {
        "trades": len(rows),
        "longs": sum(row.side == "long" for row in rows),
        "shorts": sum(row.side == "short" for row in rows),
        "tv_wins": sum(row.tv_win for row in rows),
        "tv_win_rate": fmean(row.tv_win for row in rows),
        "positive_net": sum(value > 0 for value in net),
        "positive_net_rate": fmean(value > 0 for value in net),
        "average_pre_entry_reversal_bps": fmean(row.pre_entry_reversal_bps for row in rows),
        "average_risk_bps": fmean(row.risk_bps for row in rows),
        "average_mfe_bps": fmean(row.mfe_bps for row in rows),
        "average_mae_bps": fmean(row.mae_bps for row in rows),
        "average_gross_bps": fmean(row.gross_bps for row in rows),
        "average_net_bps": fmean(net),
        "total_net_bps": sum(net),
        "profit_factor": gains / losses if losses else (None if gains else 0.0),
        "median_minutes_held": float(np.median([row.minutes_held for row in rows])),
        "exit_reasons": {
            reason: sum(row.exit_reason == reason for row in rows)
            for reason in sorted({row.exit_reason for row in rows})
        },
    }


def run_audit(
    *,
    cache_dir: Path = DEFAULT_CACHE,
    symbols: Sequence[str] = ("BTCUSD", "ETHUSD"),
    timeframes: Sequence[str] = DEFAULT_TIMEFRAMES,
    cost_bps: float = 14.8,
) -> tuple[dict[str, object], list[TrapOutcome]]:
    all_rows: list[TrapOutcome] = []
    cells: dict[str, object] = {}
    data_windows: dict[str, object] = {}
    for symbol in symbols:
        minute = load_symbol(cache_dir, symbol)
        data_windows[symbol] = {
            "rows_1m": len(minute),
            "start": minute.index.min().isoformat(),
            "end": minute.index.max().isoformat(),
        }
        for timeframe in timeframes:
            bars = resample_closed(minute, timeframe)
            signals = detect_default_traps(bars)
            tv_rows = simulate_tv_native(bars, signals, symbol=symbol, timeframe=timeframe)
            eligible_for_30m = pd.Timedelta(RESAMPLE_RULES[timeframe]) <= pd.Timedelta(minutes=30)
            causal_rows = (
                simulate_causal_30m(
                    bars,
                    signals,
                    symbol=symbol,
                    timeframe=timeframe,
                    cost_bps=cost_bps,
                )
                if eligible_for_30m
                else []
            )
            all_rows.extend(tv_rows)
            all_rows.extend(causal_rows)
            cells[f"{symbol}:{timeframe}"] = {
                "bars": len(bars),
                "raw_trap_signals": len(signals),
                "scalper_30m_eligible": eligible_for_30m,
                "tv_native": summarize(tv_rows),
                "causal_30m": summarize(causal_rows),
            }

    causal = [row for row in all_rows if row.ledger == "causal_30m"]
    tv = [row for row in all_rows if row.ledger == "tv_native"]
    eligible_timeframes = {
        timeframe
        for timeframe in timeframes
        if pd.Timedelta(RESAMPLE_RULES[timeframe]) <= pd.Timedelta(minutes=30)
    }
    tv_scalper = [row for row in tv if row.timeframe in eligible_timeframes]
    signature = "\n".join(
        f"{row.symbol}|{row.timeframe}|{row.ledger}|{row.signal_ts}|{row.side}|{row.exit_reason}|{row.net_bps:.10f}"
        for row in all_rows
    )
    report = {
        "schema_version": "vnedge.strat_trap_vwap_parity.v1",
        "scanner_id": SCANNER_ID,
        "generated_at": datetime.now(UTC).isoformat(),
        "source_contract": {
            "name": "STRAT Trap & VWAP Engine [WillyAlgoTrader]",
            "version": "1.9.0",
            "license": "MPL-2.0",
            "ported_defaults": "Trap Bar + PVTE EMA(21)/ATR(100)x3 + balanced risk",
        },
        "data_windows": data_windows,
        "cost_bps": cost_bps,
        "accounting_gap": {
            "tv_native": "signal-close entry; entry bar ignored; TP priority; TP1 touch is win; no cost/time stop",
            "causal_30m": "next-open entry; entry bar included; stop-first; Delta costs; 30m time stop",
        },
        "cells": cells,
        "aggregate": {
            "tv_native_all_timeframes": summarize(tv),
            "tv_native_scalper_eligible": summarize(tv_scalper),
            "causal_30m": summarize(causal),
        },
        "deterministic_hash": hashlib.sha256(signature.encode()).hexdigest(),
        "research_only": True,
        "can_trade": False,
        "can_promote": False,
        "order_route": "absent",
    }
    return report, all_rows


def publish(
    report: dict[str, object],
    rows: Sequence[TrapOutcome],
    *,
    output: Path = DEFAULT_OUTPUT,
) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    detail = output.with_name(output.stem + "_trades.parquet")
    pd.DataFrame(asdict(row) for row in rows).to_parquet(detail, index=False)
    return output


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit STRAT Trap TradingView accounting parity")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--symbols", default="BTCUSD,ETHUSD")
    parser.add_argument("--timeframes", default=",".join(DEFAULT_TIMEFRAMES))
    parser.add_argument("--cost-bps", type=float, default=14.8)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    report, rows = run_audit(
        cache_dir=args.cache_dir,
        symbols=tuple(value.strip().upper() for value in args.symbols.split(",") if value.strip()),
        timeframes=tuple(value.strip() for value in args.timeframes.split(",") if value.strip()),
        cost_bps=args.cost_bps,
    )
    print(publish(report, rows, output=args.output))
    print(json.dumps(report["aggregate"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
