"""Durable live-funding accumulation for venues with no funding history.

Delta Exchange India exposes *current* funding but NO historical funding
series, so ``funding_mean_reversion_v1`` can't be REST-seeded the way it is on
Binance/Bybit. This builds the percentile window purely from live observations
and persists every sample to an append-only, fsync'd JSONL so a restart resumes
the window instead of throwing away days of accumulation (the operator's
"don't lose data on restart" requirement applied to the funding series).

Warming up is inherently signal-safe: until the percentile window fills
(``funding_pct_window`` candles — ~10 days at 1h bars), ``funding_pct`` is NaN
and ``FundingMeanReversion.signal()`` already returns ``None`` on any NaN
feature. So an accumulating lane simply produces no signal until it has enough
history — it never trades on a half-built window.

Zero risk: this only augments the funding series a SHADOW lane reads. Every
order still passes the gateway; shadow lanes never fill.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pandas as pd

from vnedge.runtime.paper_trial import LiveFundingMR

logger = logging.getLogger(__name__)

# Synthetic warmup anchor: a single 1970 sample lets the base strategy build
# (it requires a non-empty funding series) without inventing a fake recent
# funding print. It ages out of the percentile window as real samples arrive
# and is never persisted.
_BOOTSTRAP_TS = pd.Timestamp(0, tz="UTC")


def _empty_funding() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.Series(dtype="datetime64[ns, UTC]"),
            "funding_rate": pd.Series(dtype="float64"),
        }
    )


def load_funding_store(path: str | Path) -> pd.DataFrame:
    """Load a persisted funding series as [timestamp (UTC), funding_rate].

    Tolerant: skips malformed lines, dedupes by timestamp, sorts ascending.
    Missing file -> empty (properly typed) frame.
    """
    path = Path(path)
    if not path.exists():
        return _empty_funding()
    rows: list[tuple[int, float]] = []
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                rows.append((int(rec["ts_ms"]), float(rec["funding_rate"])))
            except (ValueError, TypeError, KeyError):
                continue
    if not rows:
        return _empty_funding()
    df = pd.DataFrame(rows, columns=["ts_ms", "funding_rate"])
    df["timestamp"] = pd.to_datetime(df["ts_ms"], unit="ms", utc=True)
    return (
        df[["timestamp", "funding_rate"]]
        .drop_duplicates("timestamp")
        .sort_values("timestamp")
        .reset_index(drop=True)
    )


def append_funding_sample(path: str | Path, ts, funding_rate: float) -> None:
    """Append one funding observation, fsync'd so a crash can't lose it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ts_ms = int(pd.Timestamp(ts).timestamp() * 1000)
    line = json.dumps({"ts_ms": ts_ms, "funding_rate": float(funding_rate)})
    with path.open("a") as fh:
        fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def _combine(stored: pd.DataFrame, seed: pd.DataFrame | None) -> pd.DataFrame:
    frames = [f for f in (stored, seed) if f is not None and not f.empty]
    if not frames:
        return _empty_funding()
    out = pd.concat(frames, ignore_index=True)[["timestamp", "funding_rate"]]
    return (
        out.drop_duplicates("timestamp")
        .sort_values("timestamp")
        .reset_index(drop=True)
    )


def _max_real_ts(df: pd.DataFrame) -> pd.Timestamp:
    real = df[df["timestamp"] > _BOOTSTRAP_TS]
    if real.empty:
        return _BOOTSTRAP_TS
    return real["timestamp"].max()


class LivePersistentFundingMR(LiveFundingMR):
    """``LiveFundingMR`` that persists its accumulated funding to disk.

    On construction it reloads any previously accumulated samples (plus a REST
    seed if one exists) so a restart continues the percentile window. Each
    ``prepare()`` appends the feed's current funding (via the parent) and
    fsyncs any newly-observed samples to the store. With an empty seed (Delta)
    the window is built entirely live, warming up until it fills.
    """

    # keep the in-memory series bounded on a long-running process; the on-disk
    # store stays complete. A few thousand samples covers the percentile window
    # and any recent candle the as-of merge needs many times over.
    _MEMORY_CAP = 5000

    def __init__(self, seed_funding, feed, *, store_path: str | Path, **params) -> None:
        stored = load_funding_store(store_path)
        combined = _combine(stored, seed_funding)
        synthetic = combined.empty
        if synthetic:
            fr = 0.0
            feed_fr = getattr(feed, "funding_rate", None)
            if feed_fr:
                try:
                    fr = float(feed_fr)
                except (TypeError, ValueError):
                    fr = 0.0
            combined = pd.DataFrame(
                [{"timestamp": _BOOTSTRAP_TS, "funding_rate": fr}]
            )
        super().__init__(combined, feed, **params)
        self._store_path = Path(store_path)
        self._persisted_through = _max_real_ts(combined)
        logger.info(
            "funding accumulator %s: %d prior sample(s)%s, store=%s",
            getattr(feed, "exchange_id", "?"),
            0 if synthetic else len(combined),
            " (cold start — warming up)" if synthetic else "",
            self._store_path,
        )

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        # Keep the funding timestamp unit aligned with the candles so the
        # backward as-of merge never trips on datetime64 ms-vs-ns drift (the
        # 1970 warmup anchor and a reloaded store can differ from the candle
        # unit). Value-based, tz-preserving.
        cdtype = candles["timestamp"].dtype
        if not self.funding.empty and self.funding["timestamp"].dtype != cdtype:
            self.funding = self.funding.assign(
                timestamp=self.funding["timestamp"].astype(cdtype)
            )
        out = super().prepare(candles)  # may append the live funding print
        self._persist_new()
        self._trim_memory()
        self._mask_unwarmed(out)
        return out

    def _mask_unwarmed(self, df: pd.DataFrame) -> None:
        """NaN-out funding_pct on bars not covered by REAL funding samples.

        The 1970 warmup anchor back-fills the as-of merge with zeros, so a
        240-bar percentile window can "fill" with synthetic values and rank
        the first real prints as extreme — the deployed Delta lane fired a
        bogus short exactly this way (caught by shadow backfill telemetry on
        2026-07-06). A bar's funding_pct is only meaningful once real samples
        span the entire percentile window ending at that bar; until then it
        must stay NaN, which signal() already treats as no-signal.
        """
        if "funding_pct" not in df.columns:
            return
        real = self.funding[self.funding["timestamp"] > _BOOTSTRAP_TS]
        if real.empty:
            df["funding_pct"] = float("nan")
            return
        first_real = real["timestamp"].min()
        # bars whose 240-bar window reaches back before real coverage began
        idx = df.index[df["timestamp"] < first_real]
        window = self.funding_pct_window
        if len(idx):
            last_anchor_pos = int(df.index.get_indexer([idx[-1]])[0])
            cutoff = last_anchor_pos + window
            df.iloc[:cutoff, df.columns.get_loc("funding_pct")] = float("nan")

    def _persist_new(self) -> None:
        fresh = self.funding[self.funding["timestamp"] > self._persisted_through]
        if fresh.empty:
            return
        for row in fresh.itertuples(index=False):
            if row.timestamp == _BOOTSTRAP_TS:
                continue
            append_funding_sample(self._store_path, row.timestamp, row.funding_rate)
        self._persisted_through = fresh["timestamp"].max()

    def _trim_memory(self) -> None:
        if len(self.funding) > self._MEMORY_CAP:
            self.funding = (
                self.funding.iloc[-self._MEMORY_CAP:].reset_index(drop=True)
            )
