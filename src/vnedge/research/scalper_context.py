"""Multi-timeframe context stack for scalper research.

The context stack is deliberately research-only. It answers whether a
microstructure hypothesis was observed with higher-timeframe support
(`aligned`), against it (`hostile`), in a mixed tape, or without enough
context. It never promotes or blocks live orders by itself.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping, Literal

import pandas as pd

from vnedge.data.parquet_store import ParquetStore


ContextTag = Literal["aligned", "mixed", "hostile", "missing"]
ContextBias = Literal["bullish", "bearish", "neutral", "missing"]

CONTEXT_WEIGHTS: dict[str, float] = {
    "4h": 2.0,
    "1h": 1.5,
    "15m": 1.0,
    "1m": 0.5,
}


@dataclass(frozen=True)
class ContextFrameState:
    timeframe: str
    bias: ContextBias
    timestamp_ms: int | None
    close: float | None
    ema_fast: float | None
    ema_slow: float | None
    slope_bps: float | None
    volatility_bps: float | None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ScalperContextStack:
    frames: tuple[ContextFrameState, ...]

    @property
    def coverage(self) -> int:
        return sum(1 for frame in self.frames if frame.bias != "missing")

    @property
    def score(self) -> float:
        total = 0.0
        for frame in self.frames:
            weight = CONTEXT_WEIGHTS.get(frame.timeframe, 1.0)
            if frame.bias == "bullish":
                total += weight
            elif frame.bias == "bearish":
                total -= weight
        return total

    def tag_for_side(self, side: str) -> ContextTag:
        if self.coverage < 2:
            return "missing"
        signed = self.score if side == "buy" else -self.score
        if signed >= 2.0:
            return "aligned"
        if signed <= -2.0:
            return "hostile"
        return "mixed"

    def summary_for_side(self, side: str) -> dict:
        return {
            "tag": self.tag_for_side(side),
            "score": self.score,
            "coverage": self.coverage,
            "frames": [frame.to_dict() for frame in self.frames],
        }


def load_context_candles(
    data_root: Path | str,
    exchange: str,
    symbol: str,
    timeframes: tuple[str, ...],
) -> dict[str, pd.DataFrame]:
    store = ParquetStore(data_root)
    candles: dict[str, pd.DataFrame] = {}
    for timeframe in timeframes:
        try:
            candles[timeframe] = store.read_candles(exchange, symbol, timeframe)
        except FileNotFoundError:
            continue
    return candles


def build_context_stack(
    candles_by_timeframe: Mapping[str, pd.DataFrame],
    *,
    at_ms: int,
    timeframes: tuple[str, ...] = ("4h", "1h", "15m", "1m"),
) -> ScalperContextStack:
    return ScalperContextStack(
        tuple(
            _frame_state(timeframe, candles_by_timeframe.get(timeframe), at_ms)
            for timeframe in timeframes
        )
    )


def _frame_state(
    timeframe: str,
    candles: pd.DataFrame | None,
    at_ms: int,
) -> ContextFrameState:
    if candles is None or candles.empty or "timestamp" not in candles:
        return _missing(timeframe)

    df = candles.copy()
    ts_ms = _timestamp_ms(df["timestamp"])
    df = df.assign(_ts_ms=ts_ms).sort_values("_ts_ms")
    df = df[df["_ts_ms"] <= at_ms].tail(64)
    if df.empty:
        return _missing(timeframe)

    closes = pd.to_numeric(df["close"], errors="coerce").dropna()
    if closes.empty:
        return _missing(timeframe)

    close = float(closes.iloc[-1])
    ema_fast = float(closes.ewm(span=8, adjust=False).mean().iloc[-1])
    ema_slow = float(closes.ewm(span=21, adjust=False).mean().iloc[-1])
    lookback = min(5, len(closes) - 1)
    slope_bps = 0.0
    if lookback > 0:
        anchor = float(closes.iloc[-lookback - 1])
        if anchor > 0:
            slope_bps = (close - anchor) / anchor * 10_000.0

    bias: ContextBias = "neutral"
    if close >= ema_fast >= ema_slow and slope_bps > 0:
        bias = "bullish"
    elif close <= ema_fast <= ema_slow and slope_bps < 0:
        bias = "bearish"

    volatility_bps = _volatility_bps(df)
    return ContextFrameState(
        timeframe=timeframe,
        bias=bias,
        timestamp_ms=int(df["_ts_ms"].iloc[-1]),
        close=close,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        slope_bps=slope_bps,
        volatility_bps=volatility_bps,
    )


def _timestamp_ms(series: pd.Series) -> pd.Series:
    timestamps = pd.to_datetime(series, utc=True)
    return timestamps.astype("int64") // 1_000_000


def _volatility_bps(df: pd.DataFrame) -> float | None:
    required = {"high", "low", "close"}
    if not required.issubset(df.columns):
        return None
    high = pd.to_numeric(df["high"], errors="coerce")
    low = pd.to_numeric(df["low"], errors="coerce")
    close = pd.to_numeric(df["close"], errors="coerce")
    rng = ((high - low).abs() / close.replace(0, pd.NA) * 10_000.0).dropna()
    if rng.empty:
        return None
    return float(rng.tail(16).mean())


def _missing(timeframe: str) -> ContextFrameState:
    return ContextFrameState(
        timeframe=timeframe,
        bias="missing",
        timestamp_ms=None,
        close=None,
        ema_fast=None,
        ema_slow=None,
        slope_bps=None,
        volatility_bps=None,
    )
