"""Historical Binance USDM aggTrades backfill into the tick lake.

    python -m vnedge.data.aggtrades_backfill --symbols BTC/USDT:USDT --days 60

Downloads the official Binance Vision daily aggTrades ZIP dumps
(https://data.binance.vision/data/futures/um/daily/aggTrades/...), converts
them to the EXACT trade schema the live tick recorder writes
(ts_ms/price/amount/side), and publishes atomic Parquet shards into the same
tick-lake layout — so months of taker-side event-family research can run
today instead of waiting weeks of live recording.

Honesty guards:
- Backfilled tape lives under a DISTINCT exchange dir (``binanceusdm_hist``)
  so it can never be mistaken for live-recorded tape.
- Side mapping is the aggressor convention the recorder uses:
  ``is_buyer_maker=true`` means the taker SOLD -> side "sell".
- Only the trades stream exists — history has no order-book tape, so any
  replay over it must use a taker-only cost model (no maker assumptions).
- Zero execution / credentials: public data files in, Parquet files out.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import logging
import os
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

HIST_EXCHANGE_ID = "binanceusdm_hist"
BASE_URL = "https://data.binance.vision/data/futures/um/daily/aggTrades"
AGGTRADES_COLUMNS = (
    "agg_trade_id", "price", "quantity", "first_trade_id",
    "last_trade_id", "transact_time", "is_buyer_maker",
)
TRADE_SCHEMA = ("ts_ms", "price", "amount", "side")
_RETRIES = 3
_RETRY_BACKOFF = 2.0


def binance_market_id(symbol: str) -> str:
    """"BTC/USDT:USDT" -> "BTCUSDT" (Binance Vision file naming)."""
    return symbol.split(":")[0].replace("/", "")


def lake_symbol(symbol: str) -> str:
    """Symbol directory name used by the tick lake (same rule as the recorder)."""
    return symbol.split(":")[0].replace("/", "")


def daily_zip_url(symbol: str, day: date) -> str:
    market = binance_market_id(symbol)
    return f"{BASE_URL}/{market}/{market}-aggTrades-{day.isoformat()}.zip"


def _to_bool(value: object) -> bool:
    """is_buyer_maker arrives as bool or as "true"/"false" strings."""
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1"}:
        return True
    if text in {"false", "0"}:
        return False
    raise ValueError(f"unparseable is_buyer_maker value: {value!r}")


def convert_aggtrades(df: pd.DataFrame) -> pd.DataFrame:
    """Map the Binance aggTrades columns onto the recorder's trade schema.

    is_buyer_maker=True means the BUYER was the maker, i.e. the aggressor
    (taker) SOLD -> side "sell". False -> taker bought -> side "buy".
    """
    missing = {"price", "quantity", "transact_time", "is_buyer_maker"} - set(df.columns)
    if missing:
        raise ValueError(f"aggTrades frame missing columns: {sorted(missing)}")
    maker = df["is_buyer_maker"].map(_to_bool)
    out = pd.DataFrame({
        "ts_ms": df["transact_time"].astype("int64"),
        "price": df["price"].astype("float64"),
        "amount": df["quantity"].astype("float64"),
        "side": maker.map({True: "sell", False: "buy"}).astype(str),
    })
    return out.sort_values("ts_ms", kind="stable", ignore_index=True)


def parse_aggtrades_csv(raw: bytes) -> pd.DataFrame:
    """Read one aggTrades CSV (header verified live 2026-07: agg_trade_id,
    price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker).
    Older dumps shipped without a header; both are handled."""
    sample = raw[:256].decode("utf-8", errors="replace")
    first_field = next(csv.reader(io.StringIO(sample)))[0].strip().lower()
    has_header = first_field == "agg_trade_id"
    df = pd.read_csv(
        io.BytesIO(raw),
        header=0 if has_header else None,
        names=None if has_header else list(AGGTRADES_COLUMNS),
    )
    df.columns = [str(c).strip().lower() for c in df.columns]
    return df


def frame_from_zip_bytes(raw: bytes) -> pd.DataFrame:
    """Extract the single CSV member of a Binance Vision ZIP and parse it."""
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not names:
            raise ValueError("aggTrades zip contains no CSV member")
        return parse_aggtrades_csv(zf.read(names[0]))


def shard_dir(data_root: Path | str, symbol: str, day: str,
              exchange: str = HIST_EXCHANGE_ID) -> Path:
    return (Path(data_root) / "ticks" / f"exchange={exchange}"
            / f"symbol={lake_symbol(symbol)}" / "stream=trades" / day)


def day_has_shards(data_root: Path | str, symbol: str, day: str,
                   exchange: str = HIST_EXCHANGE_ID) -> bool:
    d = shard_dir(data_root, symbol, day, exchange)
    return d.is_dir() and any(d.glob("*.parquet"))


def write_trade_shard(trades: pd.DataFrame, data_root: Path | str, symbol: str,
                      day: str, exchange: str = HIST_EXCHANGE_ID) -> Path:
    """Atomically publish one day of converted trades as a tick-lake shard.

    Same discipline as the live recorder's _Buffer: write to a hidden temp
    file in the target dir, then os.replace — a concurrent reader never sees
    a partial shard. Layout: ticks/exchange=<x>/symbol=<s>/stream=trades/<day>/.
    """
    if list(trades.columns) != list(TRADE_SCHEMA):
        raise ValueError(f"trade shard must have columns {TRADE_SCHEMA}, "
                         f"got {list(trades.columns)}")
    if trades.empty:
        raise ValueError("refusing to write an empty trade shard")
    d = shard_dir(data_root, symbol, day, exchange)
    d.mkdir(parents=True, exist_ok=True)
    first_ts = int(trades["ts_ms"].iloc[0])
    name = f"{first_ts}-aggtrades.parquet"
    final = d / name
    tmp = d / f".{name}.tmp"
    trades.to_parquet(tmp, index=False)
    os.replace(tmp, final)   # atomic publish
    return final


@dataclass
class BackfillReport:
    written: list[str] = field(default_factory=list)   # "SYMBOL day" labels
    skipped_existing: list[str] = field(default_factory=list)
    missing_upstream: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    rows_written: int = 0

    @property
    def summary(self) -> str:
        return (f"backfill: {len(self.written)} day-files written "
                f"({self.rows_written} trades), {len(self.skipped_existing)} skipped "
                f"(already present), {len(self.missing_upstream)} missing upstream, "
                f"{len(self.failed)} failed")


def backfill_days(end: date, days: int) -> list[date]:
    """The N UTC days ending at `end` inclusive, oldest first."""
    if days < 1:
        raise ValueError("days must be >= 1")
    return [end - timedelta(days=offset) for offset in range(days - 1, -1, -1)]


async def _fetch(session, url: str) -> bytes | None:
    """GET one archive; None on 404 (day not published), retries on transient
    failures. Binance Vision is a static file host — no API rate limits."""
    for attempt in range(1, _RETRIES + 1):
        try:
            async with session.get(url) as resp:
                if resp.status == 404:
                    return None
                resp.raise_for_status()
                return await resp.read()
        except Exception as exc:  # noqa: BLE001 — bounded retry, then surface
            if attempt == _RETRIES:
                raise
            logger.warning("fetch failed (%s/%s) %s: %s", attempt, _RETRIES, url, exc)
            await asyncio.sleep(_RETRY_BACKOFF * attempt)
    return None


async def backfill(
    symbols: list[str],
    *,
    days: int,
    data_root: Path | str = "data",
    end: date | None = None,
    exchange: str = HIST_EXCHANGE_ID,
    concurrency: int = 4,
    force: bool = False,
) -> BackfillReport:
    """Download + convert + shard the last `days` UTC days of aggTrades.

    Idempotent: a symbol/day that already has shards is skipped unless
    `force=True`. The newest fully published day is normally yesterday (UTC);
    days Binance has not published yet are recorded as missing, not errors.
    """
    import aiohttp

    end = end or (datetime.now(UTC).date() - timedelta(days=1))
    day_list = backfill_days(end, days)
    report = BackfillReport()
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(session, symbol: str, day: date) -> None:
        day_key = day.strftime("%Y%m%d")
        label = f"{binance_market_id(symbol)} {day_key}"
        if not force and day_has_shards(data_root, symbol, day_key, exchange):
            report.skipped_existing.append(label)
            return
        async with sem:
            try:
                raw = await _fetch(session, daily_zip_url(symbol, day))
            except Exception as exc:  # noqa: BLE001 — one day must not kill the run
                logger.error("giving up on %s: %s", label, exc)
                report.failed.append(label)
                return
        if raw is None:
            report.missing_upstream.append(label)
            return
        try:
            trades = convert_aggtrades(frame_from_zip_bytes(raw))
            write_trade_shard(trades, data_root, symbol, day_key, exchange)
        except Exception as exc:  # noqa: BLE001
            logger.error("convert/write failed for %s: %s", label, exc)
            report.failed.append(label)
            return
        report.written.append(label)
        report.rows_written += len(trades)
        logger.info("wrote %s: %d trades", label, len(trades))

    timeout = aiohttp.ClientTimeout(total=300)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        await asyncio.gather(*(
            _one(session, symbol, day)
            for symbol in symbols
            for day in day_list
        ))
    return report


def _split_csv(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(
        description="backfill Binance USDM aggTrades history into the tick lake"
    )
    p.add_argument("--symbols", default="BTC/USDT:USDT",
                   help="comma-separated perp symbols, e.g. BTC/USDT:USDT,ETH/USDT:USDT")
    p.add_argument("--days", type=int, default=60)
    p.add_argument("--end", help="last UTC day to fetch (YYYY-MM-DD); default yesterday")
    p.add_argument("--data-root", default="data")
    p.add_argument("--exchange", default=HIST_EXCHANGE_ID,
                   help="tick-lake exchange dir (keep the _hist suffix so "
                        "backfilled tape is never mistaken for live recording)")
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--force", action="store_true", help="re-download existing days")
    args = p.parse_args(argv)
    symbols = _split_csv(args.symbols)
    if not symbols:
        p.error("--symbols must name at least one symbol")
    end = date.fromisoformat(args.end) if args.end else None
    report = asyncio.run(backfill(
        symbols,
        days=args.days,
        data_root=args.data_root,
        end=end,
        exchange=args.exchange,
        concurrency=args.concurrency,
        force=args.force,
    ))
    print(report.summary)
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
