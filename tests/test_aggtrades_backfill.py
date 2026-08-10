"""aggTrades backfill — schema conversion, atomic shard layout, idempotency."""

import io
import zipfile
from datetime import date

import pandas as pd
import pytest

import vnedge.data.aggtrades_backfill as ab
from vnedge.data.aggtrades_backfill import (
    HIST_EXCHANGE_ID,
    backfill,
    backfill_days,
    convert_aggtrades,
    daily_zip_url,
    day_has_shards,
    frame_from_zip_bytes,
    parse_aggtrades_csv,
    write_trade_shard,
)
from vnedge.scalping.microstructure import TradeTick
from vnedge.scalping.replay_backtester import load_tick_events

DAY_TS = 1_751_000_000_000  # 2025-06-27 UTC, fixed ms timestamp
DAY = pd.to_datetime(DAY_TS, unit="ms", utc=True).strftime("%Y%m%d")

# header verified live against data.binance.vision (2026-07)
CSV_HEADER = ("agg_trade_id,price,quantity,first_trade_id,last_trade_id,"
              "transact_time,is_buyer_maker")


def _raw_frame():
    return pd.DataFrame({
        "agg_trade_id": [2, 1],  # deliberately unsorted by time
        "price": [100.5, 100.0],
        "quantity": [2.0, 1.5],
        "first_trade_id": [20, 10],
        "last_trade_id": [21, 11],
        "transact_time": [DAY_TS + 1000, DAY_TS],
        "is_buyer_maker": ["false", "true"],
    })


def test_daily_zip_url_uses_binance_market_id():
    assert daily_zip_url("BTC/USDT:USDT", date(2026, 7, 5)) == (
        "https://data.binance.vision/data/futures/um/daily/aggTrades/"
        "BTCUSDT/BTCUSDT-aggTrades-2026-07-05.zip"
    )


def test_convert_maps_is_buyer_maker_to_taker_side():
    out = convert_aggtrades(_raw_frame())
    assert list(out.columns) == ["ts_ms", "price", "amount", "side"]
    # sorted by time: first row is the earlier trade (is_buyer_maker=true)
    assert list(out["ts_ms"]) == [DAY_TS, DAY_TS + 1000]
    # buyer was maker => the TAKER sold => side "sell"
    assert list(out["side"]) == ["sell", "buy"]
    assert list(out["price"]) == [100.0, 100.5]
    assert list(out["amount"]) == [1.5, 2.0]


def test_convert_accepts_real_bools_and_mixed_case_strings():
    df = _raw_frame()
    df["is_buyer_maker"] = [False, True]
    assert list(convert_aggtrades(df)["side"]) == ["sell", "buy"]
    df["is_buyer_maker"] = ["False", "TRUE"]
    assert list(convert_aggtrades(df)["side"]) == ["sell", "buy"]


def test_convert_rejects_missing_columns():
    with pytest.raises(ValueError, match="missing columns"):
        convert_aggtrades(_raw_frame().drop(columns=["is_buyer_maker"]))


def _csv_bytes(with_header: bool) -> bytes:
    rows = [
        f"1,100.0,1.5,10,11,{DAY_TS},true",
        f"2,100.5,2.0,20,21,{DAY_TS + 1000},false",
    ]
    lines = ([CSV_HEADER] if with_header else []) + rows
    return ("\n".join(lines) + "\n").encode()


@pytest.mark.parametrize("with_header", [True, False])
def test_parse_csv_with_and_without_header(with_header):
    df = parse_aggtrades_csv(_csv_bytes(with_header))
    assert list(df["transact_time"]) == [DAY_TS, DAY_TS + 1000]
    out = convert_aggtrades(df)
    assert list(out["side"]) == ["sell", "buy"]


def _zip_bytes(csv: bytes, name: str = "X-aggTrades-2026-07-05.csv") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, csv)
    return buf.getvalue()


def test_frame_from_zip_bytes_round_trip():
    df = frame_from_zip_bytes(_zip_bytes(_csv_bytes(with_header=True)))
    assert list(convert_aggtrades(df)["side"]) == ["sell", "buy"]


def test_frame_from_zip_without_csv_member_fails():
    with pytest.raises(ValueError, match="no CSV member"):
        frame_from_zip_bytes(_zip_bytes(b"x", name="readme.txt"))


def test_shard_layout_is_atomic_distinct_and_replayable(tmp_path):
    trades = convert_aggtrades(_raw_frame())
    path = write_trade_shard(trades, tmp_path, "BTC/USDT:USDT", DAY)

    # exact tick-lake layout, under the DISTINCT historical exchange dir
    assert path == (tmp_path / "ticks" / f"exchange={HIST_EXCHANGE_ID}"
                    / "symbol=BTCUSDT" / "stream=trades" / DAY
                    / f"{DAY_TS}-aggtrades.parquet")
    assert not list(path.parent.glob(".*.tmp"))  # no leftover temp files
    # never lands in the live-recorded exchange dir
    assert not (tmp_path / "ticks" / "exchange=binanceusdm").exists()

    # readable by the standard event loader: trades-only day, book absent
    events = load_tick_events(tmp_path, HIST_EXCHANGE_ID, "BTC/USDT:USDT", DAY)
    assert [kind for _ts, kind, _obj in events] == ["trade", "trade"]
    ticks = [obj for _ts, _kind, obj in events]
    assert all(isinstance(t, TradeTick) for t in ticks)
    assert [t.taker_side for t in ticks] == ["sell", "buy"]
    assert [t.price for t in ticks] == [100.0, 100.5]
    # and nothing leaks into the live exchange's event space
    assert load_tick_events(tmp_path, "binanceusdm", "BTC/USDT:USDT", DAY) == []


def test_write_trade_shard_rejects_bad_frames(tmp_path):
    with pytest.raises(ValueError, match="columns"):
        write_trade_shard(_raw_frame(), tmp_path, "BTC/USDT:USDT", DAY)
    empty = convert_aggtrades(_raw_frame()).iloc[0:0]
    with pytest.raises(ValueError, match="empty"):
        write_trade_shard(empty, tmp_path, "BTC/USDT:USDT", DAY)


def test_backfill_days_oldest_first():
    days = backfill_days(date(2026, 7, 5), 3)
    assert days == [date(2026, 7, 3), date(2026, 7, 4), date(2026, 7, 5)]
    with pytest.raises(ValueError):
        backfill_days(date(2026, 7, 5), 0)


async def test_backfill_writes_then_skips_existing(tmp_path, monkeypatch):
    end = date(2025, 6, 27)  # the UTC day of DAY_TS
    fetched = []

    async def fake_fetch(session, url):
        fetched.append(url)
        if "2025-06-26" in url:
            return None  # upstream has not published this day
        return _zip_bytes(_csv_bytes(with_header=True))

    monkeypatch.setattr(ab, "_fetch", fake_fetch)
    report = await backfill(["BTC/USDT:USDT"], days=2, data_root=tmp_path, end=end)
    assert report.written == ["BTCUSDT 20250627"]
    assert report.missing_upstream == ["BTCUSDT 20250626"]
    assert report.rows_written == 2
    assert day_has_shards(tmp_path, "BTC/USDT:USDT", DAY)

    # idempotent: second run downloads only the still-missing day
    fetched.clear()
    report2 = await backfill(["BTC/USDT:USDT"], days=2, data_root=tmp_path, end=end)
    assert report2.skipped_existing == ["BTCUSDT 20250627"]
    assert report2.written == []
    assert fetched == [daily_zip_url("BTC/USDT:USDT", date(2025, 6, 26))]
