"""Canonical market-data schemas and raw-payload normalizers.

Every dataset in the system uses these shapes. Rules that hold everywhere:

- timestamps are timezone-aware UTC (``datetime64[ns, UTC]``), column name
  ``timestamp``
- dataframes are sorted ascending by timestamp with duplicates dropped
  (normalizers guarantee it mechanically; the quality gate re-verifies it as
  defense in depth, since data can also arrive via merges or manual files)
- prices/rates are float64

Raw CCXT payloads enter through the ``normalize_*`` functions and nothing
else; strategies and the backtester only ever see these canonical frames.
"""

from __future__ import annotations

import pandas as pd

TIMEFRAME_MS: dict[str, int] = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
}

CANDLE_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]
FUNDING_COLUMNS = ["timestamp", "funding_rate"]
# open_interest is in base units (contracts/coins); open_interest_value is the
# quote-currency notional and may be NaN on venues that don't report it.
OPEN_INTEREST_COLUMNS = ["timestamp", "open_interest", "open_interest_value"]


def _finalize(df: pd.DataFrame) -> pd.DataFrame:
    """Common tail: UTC timestamps, dedupe, sort, fresh index."""
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.drop_duplicates(subset="timestamp", keep="first")
    return df.sort_values("timestamp").reset_index(drop=True)


def empty_candles() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.Series(dtype="datetime64[ns, UTC]"),
            **{c: pd.Series(dtype="float64") for c in CANDLE_COLUMNS[1:]},
        }
    )


def normalize_candles(raw: list[list]) -> pd.DataFrame:
    """CCXT fetch_ohlcv rows ``[ms, open, high, low, close, volume]``."""
    if not raw:
        return empty_candles()
    df = pd.DataFrame((row[:6] for row in raw), columns=CANDLE_COLUMNS)
    for col in CANDLE_COLUMNS[1:]:
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("float64")
    return _finalize(df)


def normalize_funding(raw: list[dict]) -> pd.DataFrame:
    """CCXT fetch_funding_rate_history entries (``timestamp``/``fundingRate``)."""
    rows = [
        (item["timestamp"], item.get("fundingRate"))
        for item in raw
        if item.get("timestamp") is not None
    ]
    if not rows:
        return pd.DataFrame(
            {
                "timestamp": pd.Series(dtype="datetime64[ns, UTC]"),
                "funding_rate": pd.Series(dtype="float64"),
            }
        )
    df = pd.DataFrame(rows, columns=FUNDING_COLUMNS)
    df["funding_rate"] = pd.to_numeric(df["funding_rate"], errors="coerce").astype("float64")
    return _finalize(df)


def normalize_open_interest(raw: list[dict]) -> pd.DataFrame:
    """CCXT fetch_open_interest_history entries."""
    rows = [
        (
            item["timestamp"],
            item.get("openInterestAmount"),
            item.get("openInterestValue"),
        )
        for item in raw
        if item.get("timestamp") is not None
    ]
    if not rows:
        return pd.DataFrame(
            {
                "timestamp": pd.Series(dtype="datetime64[ns, UTC]"),
                "open_interest": pd.Series(dtype="float64"),
                "open_interest_value": pd.Series(dtype="float64"),
            }
        )
    df = pd.DataFrame(rows, columns=OPEN_INTEREST_COLUMNS)
    for col in ("open_interest", "open_interest_value"):
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("float64")
    return _finalize(df)
