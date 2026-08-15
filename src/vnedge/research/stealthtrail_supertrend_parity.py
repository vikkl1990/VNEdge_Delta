"""Causal audit of StealthTrail SuperTrend ML Pro v5.1.0 defaults.

This is an independent research port, not a production scanner.  It preserves
the economic hypothesis (adaptive SuperTrend regime flips plus RSI momentum)
while making the accounting gap explicit:

* ``indicator_native`` enters at the signal close, uses close-only stops,
  permits unlimited holds, and charges no costs (the supplied Pine display).
* ``causal_30m`` enters at the next bar open, resolves OHLC ambiguity stop
  first, caps a scalp at 30 minutes, and deducts configured Delta costs.

The Pine defaults call the MTF setting "Moderate", but only ``Strict`` blocks
an unaligned signal.  The so-called ML filter is also disabled by default and
is a fixed weighted score rather than a fitted statistical model.  Therefore
neither affects the default signal stream ported here.
"""

from __future__ import annotations

import argparse
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

from vnedge.research.strat_trap_vwap_parity import (
    DEFAULT_CACHE,
    DEFAULT_TIMEFRAMES,
    RESAMPLE_RULES,
    load_symbol,
    resample_closed,
)

SCANNER_ID = "stealthtrail_supertrend_parity_v1"
DEFAULT_OUTPUT = Path("research/live_research/stealthtrail_supertrend_parity_latest.json")


@dataclass(frozen=True)
class TrailSignal:
    index: int
    timestamp: pd.Timestamp
    side: Literal["long", "short"]
    signal_close: float
    atr: float
    band: float
    rsi: float
    effective_multiplier: float


@dataclass(frozen=True)
class TrailOutcome:
    symbol: str
    timeframe: str
    ledger: Literal["indicator_native", "causal_30m"]
    signal_ts: str
    side: Literal["long", "short"]
    entry_ts: str
    exit_ts: str
    entry_price: float
    exit_price: float
    exit_reason: str
    bars_held: int
    minutes_held: float
    mfe_bps: float
    mae_bps: float
    gross_bps: float
    cost_bps: float
    net_bps: float


def _rma(values: pd.Series, length: int) -> pd.Series:
    return values.ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean()


def _atr(frame: pd.DataFrame, length: int) -> pd.Series:
    previous = frame["close"].shift(1)
    tr = pd.concat(
        (
            frame["high"] - frame["low"],
            (frame["high"] - previous).abs(),
            (frame["low"] - previous).abs(),
        ),
        axis=1,
    ).max(axis=1)
    return _rma(tr, length)


def _rsi(close: pd.Series, length: int) -> pd.Series:
    delta = close.diff()
    gain = _rma(delta.clip(lower=0.0), length)
    loss = _rma((-delta.clip(upper=0.0)), length)
    ratio = gain / loss.replace(0.0, np.nan)
    result = 100.0 - 100.0 / (1.0 + ratio)
    return result.where(loss != 0.0, 100.0).fillna(50.0)


def _lerp_bank(bank: dict[int, pd.Series], dynamic_length: np.ndarray) -> np.ndarray:
    lengths = sorted(bank)
    arrays = {length: bank[length].to_numpy(dtype=float) for length in lengths}
    output = np.full(len(dynamic_length), np.nan)
    for row, raw_length in enumerate(dynamic_length):
        if not np.isfinite(raw_length):
            continue
        length = int(raw_length)
        if length <= lengths[0]:
            output[row] = arrays[lengths[0]][row]
            continue
        if length >= lengths[-1]:
            output[row] = arrays[lengths[-1]][row]
            continue
        upper_index = int(np.searchsorted(lengths, length, side="left"))
        lower, upper = lengths[upper_index - 1], lengths[upper_index]
        weight = (length - lower) / (upper - lower)
        output[row] = arrays[lower][row] + weight * (arrays[upper][row] - arrays[lower][row])
    return output


def compute_default_state(frame: pd.DataFrame) -> pd.DataFrame:
    """Reproduce the default adaptive-band signal state on confirmed bars."""

    close = frame["close"]
    returns = close / close.shift(1) - 1.0
    abs_path = close.diff().abs().rolling(100, min_periods=100).sum()
    er = (close - close.shift(100)).abs() / abs_path.replace(0.0, np.nan)
    atr_100 = _atr(frame, 100)
    atr_20 = _atr(frame, 20)
    norm_vol = atr_100 / close * 100.0
    vol_cluster = atr_20 / atr_100.replace(0.0, np.nan)
    lagged = returns.shift(1)
    numerator = (returns * lagged).rolling(100, min_periods=100).sum()
    denominator = np.sqrt(
        returns.pow(2).rolling(100, min_periods=100).sum()
        * lagged.pow(2).rolling(100, min_periods=100).sum()
    )
    autocorr = (numerator / denominator.replace(0.0, np.nan)).fillna(0.0)
    er_smooth = er.ewm(span=33, adjust=False, min_periods=33).mean()
    vc_smooth = vol_cluster.ewm(span=33, adjust=False, min_periods=33).mean()
    trend_score = er_smooth
    range_score = (1.0 - er_smooth) * (1.0 - autocorr.abs())
    volatile_score = (vc_smooth - 1.0).clip(0.0, 2.0)
    total = (trend_score + range_score + volatile_score).replace(0.0, np.nan)
    wt = (trend_score / total).fillna(1.0 / 3.0)
    wr = (range_score / total).fillna(1.0 / 3.0)
    wv = (volatile_score / total).fillna(1.0 / 3.0)

    effective_len = ((wt * 10.0 + wr * 16.0 + wv * 21.0) * (norm_vol / 1.5).clip(0.7, 1.8)).clip(
        5.0, 50.0
    )
    effective_len = np.trunc(effective_len.fillna(13.0).to_numpy()).astype(int)
    base_mult = ((wt * 2.0 + wr * 3.2 + wv * 3.8) * (norm_vol / 1.0).clip(0.8, 1.5)).clip(1.2, 6.0)
    cushion = (wt * 0.05 + wr * 0.25 + wv * 0.15).clip(0.0, 0.5).fillna(0.15)
    cooldown = np.trunc(
        (wt * 2.0 + wr * 5.0 + wv * 3.0).clip(1.0, 10.0).fillna(3.0).to_numpy()
    ).astype(int)
    rsi_threshold = (wt * 40.0 + wr * 52.0 + wv * 45.0).clip(35.0, 58.0).fillna(45.0)
    rsi_len = np.trunc(np.clip(effective_len * 0.9, 5.0, 30.0)).astype(int)
    smooth_len = np.trunc(np.clip(effective_len * 4.0, 20.0, 200.0)).astype(int)

    atr_bank = {length: _atr(frame, length) for length in (5, 8, 10, 13, 16, 21, 30, 40, 50)}
    atr_value = _lerp_bank(atr_bank, effective_len)
    atr_13 = atr_bank[13]
    sma_bank = {
        length: atr_13.rolling(length, min_periods=length).mean()
        for length in (20, 35, 50, 70, 100, 150, 200)
    }
    atr_sma = _lerp_bank(sma_bank, smooth_len)
    vol_ratio = np.divide(
        atr_value, atr_sma, out=np.ones_like(atr_value), where=np.isfinite(atr_sma) & (atr_sma > 0)
    )
    adaptive_mult = np.clip(base_mult.to_numpy() * vol_ratio, 1.0, 5.0)
    rsi_bank = {length: _rsi(close, length) for length in (5, 7, 9, 11, 13, 16, 20, 25, 30)}
    rsi_value = _lerp_bank(rsi_bank, rsi_len)

    hl2 = ((frame["high"] + frame["low"]) / 2.0).to_numpy()
    close_values = close.to_numpy()
    upper = hl2 + adaptive_mult * atr_value
    lower = hl2 - adaptive_mult * atr_value
    trend = np.ones(len(frame), dtype=np.int8)
    band = np.full(len(frame), np.nan)
    flip = np.zeros(len(frame), dtype=bool)
    bars_since = 100
    direction = 1
    previous_band = np.nan
    for position in range(len(frame)):
        bars_since += 1
        if not np.isfinite(atr_value[position]):
            trend[position] = direction
            continue
        prior = (
            previous_band
            if np.isfinite(previous_band)
            else (lower[position] if direction == 1 else upper[position])
        )
        current = max(lower[position], prior) if direction == 1 else min(upper[position], prior)
        old_direction = direction
        if (
            direction == 1
            and close_values[position] < current - cushion.iloc[position] * atr_value[position]
            and bars_since >= cooldown[position]
        ):
            direction, current, bars_since = -1, upper[position], 0
        elif (
            direction == -1
            and close_values[position] > current + cushion.iloc[position] * atr_value[position]
            and bars_since >= cooldown[position]
        ):
            direction, current, bars_since = 1, lower[position], 0
        flip[position] = direction != old_direction
        trend[position] = direction
        band[position] = current
        previous_band = current

    result = frame.copy()
    result["atr"] = atr_value
    result["rsi"] = rsi_value
    result["effective_multiplier"] = adaptive_mult
    result["trend"] = trend
    result["band"] = band
    result["raw_flip"] = flip
    result["rsi_threshold"] = rsi_threshold.to_numpy()
    result["signal"] = (
        flip
        & (np.arange(len(frame)) >= 100)
        & np.where(
            trend == 1,
            rsi_value >= result["rsi_threshold"].to_numpy(),
            rsi_value <= 100.0 - result["rsi_threshold"].to_numpy(),
        )
    )
    return result


def detect_default_signals(frame: pd.DataFrame) -> tuple[pd.DataFrame, list[TrailSignal]]:
    state = compute_default_state(frame)
    signals: list[TrailSignal] = []
    for position in np.flatnonzero(state["signal"].to_numpy()):
        row = state.iloc[position]
        if not np.isfinite(row.atr) or not np.isfinite(row.band):
            continue
        signals.append(
            TrailSignal(
                index=int(position),
                timestamp=pd.Timestamp(state.index[position]),
                side="long" if int(row.trend) == 1 else "short",
                signal_close=float(row.close),
                atr=float(row.atr),
                band=float(row.band),
                rsi=float(row.rsi),
                effective_multiplier=float(row.effective_multiplier),
            )
        )
    return state, signals


def _movement(row: pd.Series, entry: float, direction: int) -> tuple[float, float]:
    if direction == 1:
        return (float(row.high) / entry - 1.0) * 10_000.0, (1.0 - float(row.low) / entry) * 10_000.0
    return (1.0 - float(row.low) / entry) * 10_000.0, (float(row.high) / entry - 1.0) * 10_000.0


def _outcome(
    *,
    signal: TrailSignal,
    symbol: str,
    timeframe: str,
    ledger: str,
    entry_ts: pd.Timestamp,
    exit_ts: pd.Timestamp,
    entry: float,
    exit_price: float,
    reason: str,
    bars: int,
    mfe: float,
    mae: float,
    cost_bps: float,
) -> TrailOutcome:
    direction = 1 if signal.side == "long" else -1
    gross = direction * (exit_price / entry - 1.0) * 10_000.0
    return TrailOutcome(
        symbol=symbol,
        timeframe=timeframe,
        ledger=ledger,
        signal_ts=signal.timestamp.isoformat(),
        side=signal.side,
        entry_ts=entry_ts.isoformat(),
        exit_ts=exit_ts.isoformat(),
        entry_price=entry,
        exit_price=exit_price,
        exit_reason=reason,
        bars_held=bars,
        minutes_held=(exit_ts - entry_ts).total_seconds() / 60.0,
        mfe_bps=max(0.0, mfe),
        mae_bps=max(0.0, mae),
        gross_bps=gross,
        cost_bps=cost_bps,
        net_bps=gross - cost_bps,
    )


def simulate_indicator_native(
    state: pd.DataFrame, signals: Sequence[TrailSignal], *, symbol: str, timeframe: str
) -> list[TrailOutcome]:
    """Replay the indicator's single visual position, including reversals."""

    rows: list[TrailOutcome] = []
    span = pd.Timedelta(RESAMPLE_RULES[timeframe])
    signal_by_index = {signal.index: signal for signal in signals}
    active: TrailSignal | None = None
    entry = stop = target = mfe = mae = 0.0
    entry_index = -1

    def close_position(position: int, exit_price: float, reason: str) -> None:
        assert active is not None
        entry_ts = active.timestamp + span
        exit_ts = pd.Timestamp(state.index[position]) + span
        rows.append(
            _outcome(
                signal=active,
                symbol=symbol,
                timeframe=timeframe,
                ledger="indicator_native",
                entry_ts=entry_ts,
                exit_ts=exit_ts,
                entry=entry,
                exit_price=exit_price,
                reason=reason,
                bars=position - entry_index,
                mfe=mfe,
                mae=mae,
                cost_bps=0.0,
            )
        )

    for position in range(len(state)):
        row = state.iloc[position]
        if active is not None and position > entry_index:
            direction = 1 if active.side == "long" else -1
            favorable, adverse = _movement(row, entry, direction)
            mfe, mae = max(mfe, favorable), max(mae, adverse)
            stop_hit = float(row.close) <= stop if direction == 1 else float(row.close) >= stop
            target_hit = float(row.high) >= target if direction == 1 else float(row.low) <= target
            if stop_hit:
                exit_price, reason = stop, "close_stop"
            elif target_hit:
                exit_price, reason = target, "tp3"
            else:
                candidate = float(row.band)
                stop = max(stop, candidate) if direction == 1 else min(stop, candidate)
                exit_price = np.nan
            if np.isfinite(exit_price):
                close_position(position, float(exit_price), reason)
                active = None

        signal = signal_by_index.get(position)
        if signal is None:
            continue
        if active is not None:
            close_position(position, float(row.close), "reverse")
        active = signal
        entry = signal.signal_close
        direction = 1 if signal.side == "long" else -1
        stop = signal.band
        target = entry + direction * 6.0 * signal.atr
        mfe = mae = 0.0
        entry_index = position
    return rows


def simulate_causal_30m(
    state: pd.DataFrame,
    signals: Sequence[TrailSignal],
    *,
    symbol: str,
    timeframe: str,
    cost_bps: float,
) -> list[TrailOutcome]:
    rows: list[TrailOutcome] = []
    blocked = -1
    span = pd.Timedelta(RESAMPLE_RULES[timeframe])
    for signal in signals:
        entry_position = signal.index + 1
        if signal.index <= blocked or entry_position >= len(state):
            continue
        entry = float(state.iloc[entry_position].open)
        entry_ts = pd.Timestamp(state.index[entry_position])
        direction = 1 if signal.side == "long" else -1
        stop = signal.band
        # A next-open gap through the signal band is not a valid executable setup.
        if (direction == 1 and entry <= stop) or (direction == -1 and entry >= stop):
            continue
        target = entry + direction * 6.0 * signal.atr
        mfe = mae = 0.0
        for position in range(entry_position, len(state)):
            row = state.iloc[position]
            favorable, adverse = _movement(row, entry, direction)
            mfe, mae = max(mfe, favorable), max(mae, adverse)
            stop_hit = float(row.low) <= stop if direction == 1 else float(row.high) >= stop
            target_hit = float(row.high) >= target if direction == 1 else float(row.low) <= target
            close_ts = pd.Timestamp(state.index[position]) + span
            if stop_hit:
                exit_price, reason = stop, "stop"
            elif target_hit:
                exit_price, reason = target, "tp3"
            elif close_ts - entry_ts >= pd.Timedelta(minutes=30):
                exit_price, reason = float(row.close), "time_stop"
            else:
                candidate = float(row.band)
                stop = max(stop, candidate) if direction == 1 else min(stop, candidate)
                continue
            rows.append(
                _outcome(
                    signal=signal,
                    symbol=symbol,
                    timeframe=timeframe,
                    ledger="causal_30m",
                    entry_ts=entry_ts,
                    exit_ts=close_ts,
                    entry=entry,
                    exit_price=exit_price,
                    reason=reason,
                    bars=position - entry_position + 1,
                    mfe=mfe,
                    mae=mae,
                    cost_bps=cost_bps,
                )
            )
            blocked = position
            break
    return rows


def summarize(rows: Sequence[TrailOutcome]) -> dict[str, object]:
    if not rows:
        return {"trades": 0, "profit_factor": 0.0, "average_net_bps": None}
    net = [row.net_bps for row in rows]
    gains = sum(value for value in net if value > 0)
    losses = abs(sum(value for value in net if value < 0))
    return {
        "trades": len(rows),
        "longs": sum(row.side == "long" for row in rows),
        "shorts": sum(row.side == "short" for row in rows),
        "win_rate": fmean(value > 0 for value in net),
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
) -> tuple[dict[str, object], list[TrailOutcome]]:
    all_rows: list[TrailOutcome] = []
    cells: dict[str, object] = {}
    windows: dict[str, object] = {}
    for symbol in symbols:
        minute = load_symbol(cache_dir, symbol)
        windows[symbol] = {
            "rows_1m": len(minute),
            "start": minute.index.min().isoformat(),
            "end": minute.index.max().isoformat(),
        }
        for timeframe in timeframes:
            bars = resample_closed(minute, timeframe)
            state, signals = detect_default_signals(bars)
            native = simulate_indicator_native(state, signals, symbol=symbol, timeframe=timeframe)
            eligible = pd.Timedelta(RESAMPLE_RULES[timeframe]) <= pd.Timedelta(minutes=30)
            causal = (
                simulate_causal_30m(
                    state, signals, symbol=symbol, timeframe=timeframe, cost_bps=cost_bps
                )
                if eligible
                else []
            )
            all_rows.extend(native + causal)
            cells[f"{symbol}:{timeframe}"] = {
                "bars": len(bars),
                "raw_signals": len(signals),
                "scalper_30m_eligible": eligible,
                "indicator_native": summarize(native),
                "causal_30m": summarize(causal),
            }
    native_rows = [row for row in all_rows if row.ledger == "indicator_native"]
    causal_rows = [row for row in all_rows if row.ledger == "causal_30m"]
    eligible_tfs = {
        tf for tf in timeframes if pd.Timedelta(RESAMPLE_RULES[tf]) <= pd.Timedelta(minutes=30)
    }
    signature = "\n".join(
        f"{row.symbol}|{row.timeframe}|{row.ledger}|{row.signal_ts}|{row.side}|{row.exit_reason}|{row.net_bps:.10f}"
        for row in all_rows
    )
    report = {
        "schema_version": "vnedge.stealthtrail_supertrend_parity.v1",
        "scanner_id": SCANNER_ID,
        "generated_at": datetime.now(UTC).isoformat(),
        "data_windows": windows,
        "cost_bps": cost_bps,
        "source_contract": {
            "name": "StealthTrail SuperTrend ML Pro",
            "version": "5.1.0",
            "ported_defaults": "auto-tuned adaptive SuperTrend flip + RSI momentum",
        },
        "audit_findings": {
            "ml_default": "disabled; weighted heuristic, not a trained ML model",
            "mtf_default": "Moderate does not block signals; MTF only influences the disabled ML score",
            "native_accounting": "signal-close entry; close-only stop; no fees; no time stop; TP1/TP2 do not realize profit",
            "causal_accounting": "next-open entry; stop-first wick execution; 30m time stop; Delta costs",
        },
        "cells": cells,
        "aggregate": {
            "indicator_native_all_timeframes": summarize(native_rows),
            "indicator_native_scalper_timeframes": summarize(
                [row for row in native_rows if row.timeframe in eligible_tfs]
            ),
            "causal_30m": summarize(causal_rows),
        },
        "deterministic_hash": hashlib.sha256(signature.encode()).hexdigest(),
        "research_only": True,
        "can_trade": False,
        "can_promote": False,
        "order_route": "absent",
    }
    return report, all_rows


def publish(
    report: dict[str, object], rows: Sequence[TrailOutcome], *, output: Path = DEFAULT_OUTPUT
) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    pd.DataFrame(asdict(row) for row in rows).to_parquet(
        output.with_name(output.stem + "_trades.parquet"), index=False
    )
    return output


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit StealthTrail SuperTrend Pine defaults")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--symbols", default="BTCUSD,ETHUSD")
    parser.add_argument("--timeframes", default=",".join(DEFAULT_TIMEFRAMES))
    parser.add_argument("--cost-bps", type=float, default=14.8)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    report, rows = run_audit(
        cache_dir=args.cache_dir,
        symbols=tuple(item.strip().upper() for item in args.symbols.split(",") if item.strip()),
        timeframes=tuple(item.strip() for item in args.timeframes.split(",") if item.strip()),
        cost_bps=args.cost_bps,
    )
    print(publish(report, rows, output=args.output))
    print(json.dumps(report["aggregate"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
