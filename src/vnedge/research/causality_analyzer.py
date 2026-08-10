"""Automated lookahead (causality) detection for strategies.

Adaptation of Freqtrade's lookahead-analysis idea to this codebase: a causal
strategy must be **truncation invariant** — running ``prepare()`` + ``signal()``
on the first ``k`` bars of a series must reproduce exactly what the full-series
run computed for those same bars. If chopping off the future changes a feature
value or a signal in the past, the strategy is reading rows it could not have
seen live.

Method (per strategy):

1. Build a fresh instance and run ``prepare()`` + ``signal()`` over the full
   frame, recording every per-index signal (side/stop/tp) past warmup.
2. For each cut point (default: 5 cuts spread over the back half of the
   series), build a FRESH instance (strategies may mutate internal state in
   ``prepare()``), run it on ``candles.iloc[:cut]`` — with funding likewise
   truncated to what had printed by the last visible bar — and compare the
   overlapping indexes:

   - any fired/side/stop/tp difference (tolerance 1e-9) is a violation;
   - any numeric/bool feature column differing at an overlapping index
     (rtol 1e-9, NaN == NaN) is a violation, reported by column name.

HONEST SCOPE: this proves truncation invariance of the *computed paths* on
the given data — not full branch coverage. A lookahead bug hiding inside a
branch that never executes on this data is not caught. The bundled synthetic
market (trend + chop + spike segments, extreme funding cluster) is designed
to exercise the common regimes, and the CLI supports re-running the same
check on real exchange data.

CLI::

    python -m vnedge.research.causality_analyzer [--strategy id]
    python -m vnedge.research.causality_analyzer --strategy id \
        --days 30 --symbol BTC/USDT:USDT --exchange binanceusdm

Without ``--days`` the deterministic synthetic market is used (offline, the
mode the test suite exercises); with ``--days`` real candles/funding are
fetched via CcxtPublicClient (network-only path, never used by default tests).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd

from vnedge.data.schemas import normalize_candles, normalize_funding
from vnedge.strategy.base_strategy import BaseStrategy

#: Called once per run with the funding history visible at that run's horizon.
#: Every class in ``strategy_registry.STRATEGIES`` satisfies this directly
#: (they all take ``funding`` as the first positional argument).
StrategyFactory = Callable[[pd.DataFrame], BaseStrategy]

_DEFAULT_CUTS = 5
_RTOL = 1e-9
_MAX_SIGNAL_VIOLATIONS_PER_CUT = 5


@dataclass(frozen=True)
class CausalityViolation:
    kind: str  # "signal" | "feature" | "structure"
    cut: int  # truncated series length that exposed the mismatch
    index: int  # bar index of the (first) mismatch
    field: str  # signal field ("fired"/"side"/"stop_price"/...) or column name
    full_value: str
    truncated_value: str
    detail: str = ""

    def describe(self) -> str:
        return (
            f"[{self.kind}] cut={self.cut} index={self.index} {self.field}: "
            f"full={self.full_value} truncated={self.truncated_value}"
            + (f" ({self.detail})" if self.detail else "")
        )


@dataclass(frozen=True)
class CausalityReport:
    strategy_id: str
    n_bars: int
    warmup_bars: int
    cut_points: tuple[int, ...]
    feature_columns: tuple[str, ...]
    signal_indexes_checked: int
    fired_bars: int  # bars past warmup where the FULL run produced an intent
    violations: tuple[CausalityViolation, ...]

    @property
    def passed(self) -> bool:
        return not self.violations

    def describe(self) -> str:
        head = (
            f"{self.strategy_id}: {'PASS' if self.passed else 'FAIL'} — "
            f"{self.n_bars} bars, warmup {self.warmup_bars}, "
            f"cuts {list(self.cut_points)}, "
            f"{len(self.feature_columns)} feature columns, "
            f"{self.signal_indexes_checked} signal comparisons, "
            f"{self.fired_bars} fired bars, "
            f"{len(self.violations)} violations"
        )
        return "\n".join([head, *("  " + v.describe() for v in self.violations)])


# --- Signal capture ------------------------------------------------------------

_SignalTuple = tuple[str, float, float | None] | None  # (side, stop, tp) or None


def _capture_signals(
    strategy: BaseStrategy, df: pd.DataFrame, start: int, stop: int
) -> dict[int, _SignalTuple]:
    out: dict[int, _SignalTuple] = {}
    for i in range(start, stop):
        intent = strategy.signal(df, i)
        if intent is None:
            out[i] = None
        else:
            tp = intent.take_profit_price
            out[i] = (intent.side, float(intent.stop_price), None if tp is None else float(tp))
    return out


def _close_floats(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if math.isnan(a) or math.isnan(b):
        return math.isnan(a) and math.isnan(b)
    return math.isclose(a, b, rel_tol=_RTOL, abs_tol=_RTOL)


def _diff_signal(a: _SignalTuple, b: _SignalTuple) -> list[tuple[str, str, str]]:
    """Field-level differences as (field, full_value, truncated_value)."""
    if a is None and b is None:
        return []
    if (a is None) != (b is None):
        return [("fired", repr(a), repr(b))]
    assert a is not None and b is not None
    diffs: list[tuple[str, str, str]] = []
    if a[0] != b[0]:
        diffs.append(("side", a[0], b[0]))
    if not _close_floats(a[1], b[1]):
        diffs.append(("stop_price", repr(a[1]), repr(b[1])))
    if not _close_floats(a[2], b[2]):
        diffs.append(("take_profit_price", repr(a[2]), repr(b[2])))
    return diffs


# --- Feature comparison --------------------------------------------------------


def _comparable_columns(df: pd.DataFrame) -> list[str]:
    """Numeric + bool feature columns (timestamps and objects are skipped)."""
    return [
        col
        for col in df.columns
        if df[col].dtype.kind in "fiub"
    ]


def _diff_features(
    full: pd.DataFrame, truncated: pd.DataFrame, cut: int
) -> list[CausalityViolation]:
    violations: list[CausalityViolation] = []
    full_cols = set(_comparable_columns(full))
    trunc_cols = set(_comparable_columns(truncated))
    for col in sorted(full_cols ^ trunc_cols):
        violations.append(
            CausalityViolation(
                kind="structure",
                cut=cut,
                index=-1,
                field=col,
                full_value="present" if col in full_cols else "absent",
                truncated_value="present" if col in trunc_cols else "absent",
                detail="prepare() produced different feature columns after truncation",
            )
        )
    for col in sorted(full_cols & trunc_cols):
        a = full[col].iloc[:cut].to_numpy()
        b = truncated[col].to_numpy()
        if a.dtype.kind == "f" or b.dtype.kind == "f":
            equal = np.isclose(
                a.astype(np.float64), b.astype(np.float64),
                rtol=_RTOL, atol=0.0, equal_nan=True,
            )
        else:
            equal = a == b
        if bool(np.all(equal)):
            continue
        bad = np.flatnonzero(~equal)
        first = int(bad[0])
        violations.append(
            CausalityViolation(
                kind="feature",
                cut=cut,
                index=first,
                field=col,
                full_value=repr(a[first]),
                truncated_value=repr(b[first]),
                detail=f"{len(bad)} mismatched rows in overlap (first shown)",
            )
        )
    return violations


# --- Core analysis -------------------------------------------------------------


def _visible_funding(funding: pd.DataFrame | None, last_ts: pd.Timestamp) -> pd.DataFrame | None:
    """Funding history that had printed by ``last_ts`` — a truncated run must
    not be handed funding events from its future."""
    if funding is None or funding.empty:
        return funding
    return funding[funding["timestamp"] <= last_ts].reset_index(drop=True)


def default_cut_points(n_bars: int, warmup_bars: int, n_cuts: int = _DEFAULT_CUTS) -> list[int]:
    """Evenly spaced cuts over the back half of the series, kept past warmup
    so every cut yields at least a few overlapping signal comparisons."""
    lo = max(n_bars // 2, warmup_bars + 4)
    hi = n_bars - 1
    if hi <= lo:
        raise ValueError(
            f"series too short for causality analysis: {n_bars} bars, "
            f"warmup {warmup_bars} — need bars past warmup in the back half"
        )
    return sorted({int(c) for c in np.linspace(lo, hi, n_cuts)})


def analyze_strategy(
    strategy_factory: StrategyFactory,
    candles: pd.DataFrame,
    funding: pd.DataFrame | None = None,
    cut_points: list[int] | None = None,
) -> CausalityReport:
    """Machine-check truncation invariance of one strategy on one dataset.

    ``strategy_factory`` is called once per run (full + each cut) so mutable
    per-instance state cannot leak between runs; it receives the funding
    history visible at that run's horizon.
    """
    n = len(candles)
    full_strategy = strategy_factory(
        _visible_funding(funding, candles["timestamp"].iloc[-1])
    )
    warmup = int(full_strategy.warmup_bars)
    if cut_points is None:
        cut_points = default_cut_points(n, warmup)
    bad_cuts = [c for c in cut_points if not 1 <= c <= n]
    if bad_cuts:
        raise ValueError(f"cut points out of range 1..{n}: {bad_cuts}")

    full_df = full_strategy.prepare(candles)
    full_signals = _capture_signals(full_strategy, full_df, warmup, n)
    fired_bars = sum(1 for s in full_signals.values() if s is not None)

    violations: list[CausalityViolation] = []
    signal_indexes_checked = 0
    for cut in sorted(cut_points):
        truncated_candles = candles.iloc[:cut].reset_index(drop=True)
        strategy = strategy_factory(
            _visible_funding(funding, truncated_candles["timestamp"].iloc[-1])
        )
        truncated_df = strategy.prepare(truncated_candles)
        if len(truncated_df) != cut:
            violations.append(
                CausalityViolation(
                    kind="structure",
                    cut=cut,
                    index=-1,
                    field="__len__",
                    full_value=str(cut),
                    truncated_value=str(len(truncated_df)),
                    detail="prepare() changed the row count",
                )
            )
            continue
        violations.extend(_diff_features(full_df, truncated_df, cut))

        signal_violations = 0
        for i in range(warmup, cut):
            signal_indexes_checked += 1
            truncated_signal = _capture_signals(strategy, truncated_df, i, i + 1)[i]
            for field, full_value, truncated_value in _diff_signal(
                full_signals[i], truncated_signal
            ):
                if signal_violations >= _MAX_SIGNAL_VIOLATIONS_PER_CUT:
                    break
                signal_violations += 1
                violations.append(
                    CausalityViolation(
                        kind="signal",
                        cut=cut,
                        index=i,
                        field=field,
                        full_value=full_value,
                        truncated_value=truncated_value,
                    )
                )

    return CausalityReport(
        strategy_id=full_strategy.strategy_id,
        n_bars=n,
        warmup_bars=warmup,
        cut_points=tuple(sorted(cut_points)),
        feature_columns=tuple(_comparable_columns(full_df)),
        signal_indexes_checked=signal_indexes_checked,
        fired_bars=fired_bars,
        violations=tuple(violations),
    )


# --- Deterministic synthetic market ----------------------------------------------
#
# Seeded, offline, canonicalized through the same normalize_* functions real
# exchange data passes through. Segments deliberately span the regimes the
# registered strategies key on: clean up-trend, chop, a volatility spike with
# a crash-and-snap, a down-trend, and a recovery — plus an 8h funding series
# whose extreme cluster coincides with the spike (crowded-positioning setups
# for the funding strategies).

_SYNTHETIC_START_MS = 1_704_067_200_000  # 2024-01-01T00:00:00Z
_HOUR_MS = 3_600_000
_FUNDING_PERIOD_BARS = 8


def synthetic_market(n_bars: int = 456, seed: int = 7) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Deterministic ~450-bar hourly OHLCV frame + synthetic 8h funding.

    Returns canonical ``(candles, funding)`` frames via
    ``normalize_candles`` / ``normalize_funding``.
    """
    rng = np.random.default_rng(seed)

    # (bars, drift per bar, vol per bar) — fractions of n_bars, defaults sum
    # to 456. The interesting regimes (blow-off, panic, snap-back, extremes)
    # sit in the back half so they land PAST the strategies' warmup (~264
    # bars at defaults) where signals are actually compared.
    plan = [
        (int(n_bars * 96 / 456), 0.0015, 0.004),    # clean up-trend
        (int(n_bars * 120 / 456), 0.0, 0.003),      # chop
        (int(n_bars * 48 / 456), -0.0004, 0.003),   # drifting chop
        (int(n_bars * 24 / 456), 0.006, 0.008),     # blow-off pump (crowded longs)
        (int(n_bars * 12 / 456), -0.010, 0.020),    # panic crash
        (int(n_bars * 12 / 456), 0.006, 0.012),     # violent snap-back
        (int(n_bars * 80 / 456), -0.0015, 0.005),   # down-trend (crowded shorts)
    ]
    plan.append((n_bars - sum(bars for bars, _, _ in plan), 0.0005, 0.0035))  # recovery

    returns: list[np.ndarray] = []
    vols: list[np.ndarray] = []
    for bars, drift, vol in plan:
        returns.append(drift + vol * rng.standard_normal(bars))
        vols.append(np.full(bars, vol))
    r = np.concatenate(returns)
    vol_path = np.concatenate(vols)

    close = 100.0 * np.exp(np.cumsum(r))
    open_ = np.empty_like(close)
    open_[0] = 100.0
    open_[1:] = close[:-1]
    body_hi = np.maximum(open_, close)
    body_lo = np.minimum(open_, close)
    wick = np.abs(rng.standard_normal(n_bars)) * vol_path
    high = body_hi * (1.0 + wick)
    low = body_lo * (1.0 - np.abs(rng.standard_normal(n_bars)) * vol_path)
    # Volume: lognormal base with move-proportional bursts so volume-z gates
    # actually open during the spike segments.
    volume = 1_000.0 * np.exp(0.3 * rng.standard_normal(n_bars)) * (
        1.0 + 8.0 * np.abs(r) / np.maximum(vol_path, 1e-9)
    )

    raw_candles = [
        [
            _SYNTHETIC_START_MS + i * _HOUR_MS,
            float(open_[i]),
            float(high[i]),
            float(low[i]),
            float(close[i]),
            float(volume[i]),
        ]
        for i in range(n_bars)
    ]

    # 8h funding: mild noise, with a rich-positive cluster through the
    # blow-off/panic (crowded longs) and a deep-negative cluster through the
    # down-trend (crowded shorts) — both trailing-percentile extremes.
    pump_start = sum(bars for bars, _, _ in plan[:3])
    crash_end = pump_start + plan[3][0] + plan[4][0]
    down_start = crash_end + plan[5][0]
    down_end = down_start + plan[6][0]
    raw_funding: list[dict] = []
    for i in range(0, n_bars, _FUNDING_PERIOD_BARS):
        rate = float(3e-5 * rng.standard_normal())
        if pump_start - 24 <= i < crash_end:
            rate = float(9e-4 + 3e-4 * rng.random())
        elif down_start + 8 <= i < down_end - 16:
            rate = float(-8e-4 - 3e-4 * rng.random())
        raw_funding.append(
            {"timestamp": _SYNTHETIC_START_MS + i * _HOUR_MS, "fundingRate": rate}
        )

    return normalize_candles(raw_candles), normalize_funding(raw_funding)


# --- CLI -------------------------------------------------------------------------


def _fetch_real_market(
    exchange: str, symbol: str, days: int, timeframe: str
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Network path (CLI only — never exercised by default tests)."""
    import asyncio
    import time

    from vnedge.data.ccxt_client import CcxtPublicClient

    until_ms = int(time.time() * 1000)
    since_ms = until_ms - days * 86_400_000

    async def _fetch() -> tuple[list[list], list[dict]]:
        async with CcxtPublicClient(exchange) as client:
            raw_candles = await client.fetch_candles(symbol, timeframe, since_ms, until_ms)
            raw_funding = await client.fetch_funding_history(symbol, since_ms, until_ms)
        return raw_candles, raw_funding

    raw_candles, raw_funding = asyncio.run(_fetch())
    candles = normalize_candles(raw_candles)
    if len(candles) > 1:
        candles = candles.iloc[:-1].reset_index(drop=True)  # drop the forming bar
    return candles, normalize_funding(raw_funding)


def main(argv: list[str] | None = None) -> int:
    import argparse

    from vnedge.strategy.strategy_registry import STRATEGIES

    parser = argparse.ArgumentParser(
        description="Machine-check strategies for lookahead via truncation invariance."
    )
    parser.add_argument("--strategy", help="registry id (default: all registered strategies)")
    parser.add_argument(
        "--days", type=int, help="fetch this many days of real data instead of synthetic"
    )
    parser.add_argument("--symbol", default="BTC/USDT:USDT")
    parser.add_argument("--exchange", default="binanceusdm")
    parser.add_argument("--timeframe", default="1h")
    args = parser.parse_args(argv)

    if args.strategy is not None and args.strategy not in STRATEGIES:
        parser.error(f"unknown strategy '{args.strategy}' — registered: {sorted(STRATEGIES)}")
    strategy_ids = [args.strategy] if args.strategy else sorted(STRATEGIES)

    if args.days is not None:
        candles, funding = _fetch_real_market(
            args.exchange, args.symbol, args.days, args.timeframe
        )
        print(
            f"real data: {args.exchange} {args.symbol} {args.timeframe} — "
            f"{len(candles)} candles, {len(funding)} funding rows"
        )
    else:
        candles, funding = synthetic_market()
        print(f"synthetic market: {len(candles)} candles, {len(funding)} funding rows")

    failed = 0
    for strategy_id in strategy_ids:
        report = analyze_strategy(STRATEGIES[strategy_id], candles, funding)
        print(report.describe())
        failed += 0 if report.passed else 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
