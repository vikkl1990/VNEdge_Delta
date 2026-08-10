"""Tick-lake maintenance — compaction, retention, hist exclusion, pressure guard."""

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from vnedge.data.tick_lake_maintenance import (
    PRESSURE_FLOOR_DAYS,
    compact_day,
    main,
    run_maintenance,
)
from vnedge.scalping.replay_backtester import load_tick_events

NOW = datetime(2026, 7, 9, 12, 0, tzinfo=UTC)


def _day(age_days: int) -> str:
    return (NOW - timedelta(days=age_days)).strftime("%Y%m%d")


def _ts(day: str, i: int) -> int:
    base = datetime.strptime(day, "%Y%m%d").replace(tzinfo=UTC)
    return int(base.timestamp() * 1000) + i * 1000


def _book_rows(day: str, n: int, start: int = 0) -> list[dict]:
    return [{"ts_ms": _ts(day, start + i), "bid": 100.0, "bid_qty": 1.0,
             "ask": 100.1, "ask_qty": 2.0} for i in range(n)]


def _trade_rows(day: str, n: int, start: int = 0) -> list[dict]:
    return [{"ts_ms": _ts(day, start + i), "price": 100.05, "amount": 0.5,
             "side": "buy" if i % 2 else "sell"} for i in range(n)]


def _stream_dir(root: Path, stream: str, *, exchange: str = "binanceusdm",
                symbol: str = "BTCUSDT") -> Path:
    return (root / "ticks" / f"exchange={exchange}" / f"symbol={symbol}"
            / f"stream={stream}")


def _write_shard(root: Path, stream: str, day: str, rows: list[dict], seq: int,
                 *, exchange: str = "binanceusdm", symbol: str = "BTCUSDT") -> Path:
    d = _stream_dir(root, stream, exchange=exchange, symbol=symbol) / day
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{rows[0]['ts_ms']}-{seq:06d}.parquet"
    pd.DataFrame(rows).to_parquet(path, index=False)
    return d


def _lake_files(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


# --- compaction --------------------------------------------------------------------


def test_compact_day_merges_sorts_and_stays_readable(tmp_path):
    day = _day(2)
    # book shards deliberately written out of time order to prove sorting
    book_dir = _write_shard(tmp_path, "book", day, _book_rows(day, 5, start=10), 1)
    _write_shard(tmp_path, "book", day, _book_rows(day, 5, start=0), 0)
    trades_dir = _write_shard(tmp_path, "trades", day, _trade_rows(day, 4, start=0), 0)
    _write_shard(tmp_path, "trades", day, _trade_rows(day, 3, start=4), 1)

    before = load_tick_events(tmp_path, "binanceusdm", "BTC/USDT:USDT", day)
    assert len(before) == 17

    res_book = compact_day(book_dir, now=NOW)
    res_trades = compact_day(trades_dir, now=NOW)
    assert res_book == {"day": day, "shards": 2, "rows": 10, "dry_run": False}
    assert res_trades["rows"] == 7

    for d in (book_dir, trades_dir):
        files = list(d.glob("*.parquet"))
        assert [f.name for f in files] == [f"compacted-{day}.parquet"]
        df = pd.read_parquet(files[0])
        assert list(df["ts_ms"]) == sorted(df["ts_ms"])  # ts-sorted output

    after = load_tick_events(tmp_path, "binanceusdm", "BTC/USDT:USDT", day)
    assert [(ts, kind) for ts, kind, _ in after] == [(ts, kind) for ts, kind, _ in before]


def test_compact_day_never_touches_today(tmp_path):
    day = _day(0)
    d = _write_shard(tmp_path, "book", day, _book_rows(day, 3), 0)
    _write_shard(tmp_path, "book", day, _book_rows(day, 3, start=3), 1)
    names_before = sorted(p.name for p in d.iterdir())
    assert compact_day(d, now=NOW) is None
    assert sorted(p.name for p in d.iterdir()) == names_before


def test_compact_day_is_idempotent_and_merges_stragglers(tmp_path):
    day = _day(3)
    d = _write_shard(tmp_path, "book", day, _book_rows(day, 4), 0)
    assert compact_day(d, now=NOW)["rows"] == 4
    # second run on an already-compacted day is a no-op
    assert compact_day(d, now=NOW) is None
    assert [p.name for p in d.glob("*.parquet")] == [f"compacted-{day}.parquet"]
    # a straggler shard flushed later is merged into a fresh single file
    _write_shard(tmp_path, "book", day, _book_rows(day, 2, start=100), 7)
    res = compact_day(d, now=NOW)
    assert res["rows"] == 6 and res["shards"] == 2
    files = list(d.glob("*.parquet"))
    assert len(files) == 1
    assert len(pd.read_parquet(files[0])) == 6


def test_compact_day_dry_run_writes_nothing(tmp_path):
    day = _day(2)
    d = _write_shard(tmp_path, "book", day, _book_rows(day, 3), 0)
    _write_shard(tmp_path, "book", day, _book_rows(day, 3, start=3), 1)
    files_before = _lake_files(tmp_path)
    res = compact_day(d, now=NOW, dry_run=True)
    assert res == {"day": day, "shards": 2, "rows": 6, "dry_run": True}
    assert _lake_files(tmp_path) == files_before


# --- retention ---------------------------------------------------------------------


def test_retention_deletes_per_stream_horizons(tmp_path):
    keep_book = _write_shard(tmp_path, "book", _day(5), _book_rows(_day(5), 2), 0)
    drop_book = _write_shard(tmp_path, "book", _day(25), _book_rows(_day(25), 2), 0)
    keep_tr = _write_shard(tmp_path, "trades", _day(30), _trade_rows(_day(30), 2), 0)
    drop_tr = _write_shard(tmp_path, "trades", _day(70), _trade_rows(_day(70), 2), 0)
    keep_liq = _write_shard(tmp_path, "liquidations", _day(100),
                            _trade_rows(_day(100), 2), 0)
    drop_liq = _write_shard(tmp_path, "liquidations", _day(130),
                            _trade_rows(_day(130), 2), 0)
    # legacy single-file layout is aged out too
    legacy = _stream_dir(tmp_path, "book") / f"{_day(40)}.parquet"
    pd.DataFrame(_book_rows(_day(40), 2)).to_parquet(legacy, index=False)

    report = run_maintenance(tmp_path, now=NOW, usage_pct=lambda p: 10.0)

    assert keep_book.exists() and keep_tr.exists() and keep_liq.exists()
    assert not drop_book.exists() and not drop_tr.exists() and not drop_liq.exists()
    assert not legacy.exists()
    assert report["streams"]["book"]["days_deleted"] == 2
    assert report["streams"]["trades"]["days_deleted"] == 1
    assert report["streams"]["liquidations"]["days_deleted"] == 1
    assert not report["pressure"]["triggered"]


def test_retention_horizons_come_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TICK_RETENTION_BOOK_DAYS", "3")
    keep = _write_shard(tmp_path, "book", _day(2), _book_rows(_day(2), 2), 0)
    drop = _write_shard(tmp_path, "book", _day(5), _book_rows(_day(5), 2), 0)
    report = run_maintenance(tmp_path, now=NOW, usage_pct=lambda p: 10.0)
    assert report["retention_days"]["book"] == 3
    assert keep.exists() and not drop.exists()


def test_unknown_stream_types_are_never_aged_out(tmp_path):
    d = _write_shard(tmp_path, "funding_events", _day(500),
                     _trade_rows(_day(500), 2), 0)
    run_maintenance(tmp_path, now=NOW, usage_pct=lambda p: 10.0)
    assert d.exists()


def test_hist_archive_is_never_touched(tmp_path):
    # ancient backfill archive: multiple shards, way past every horizon
    day = _day(400)
    hist = _write_shard(tmp_path, "trades", day, _trade_rows(day, 3), 0,
                        exchange="binanceusdm_hist")
    _write_shard(tmp_path, "trades", day, _trade_rows(day, 3, start=3), 1,
                 exchange="binanceusdm_hist")
    hist_book = _write_shard(tmp_path, "book", day, _book_rows(day, 3), 0,
                             exchange="binanceusdm_hist")
    files_before = _lake_files(tmp_path)

    # normal sweep AND permanent disk pressure: hist survives untouched (not
    # even compacted), and is reported as excluded
    report = run_maintenance(tmp_path, now=NOW, usage_pct=lambda p: 95.0)
    assert hist.exists() and hist_book.exists()
    assert _lake_files(tmp_path) == files_before
    assert "binanceusdm_hist" in report["excluded_exchanges"]
    assert report["excluded_bytes"] > 0


# --- disk-pressure guard -------------------------------------------------------------


class _UsageByRemainingDays:
    """Fake usage: base + per-day weight for each surviving old book day, so
    usage drops as the sweep deletes them — call-count independent."""

    def __init__(self, root: Path, days: list[str], base: float, per_day: float):
        self.root = root
        self.days = days
        self.base = base
        self.per_day = per_day

    def __call__(self, _path) -> float:
        book = _stream_dir(self.root, "book")
        remaining = sum(1 for d in self.days if (book / d).exists())
        return self.base + self.per_day * remaining


def test_pressure_deletes_oldest_book_first_until_below_threshold(tmp_path):
    old_days = [_day(20), _day(15), _day(10)]
    for day in old_days:
        _write_shard(tmp_path, "book", day, _book_rows(day, 2), 0)
    young = _write_shard(tmp_path, "book", _day(5), _book_rows(_day(5), 2), 0)
    trades = _write_shard(tmp_path, "trades", _day(30), _trade_rows(_day(30), 2), 0)

    # 3 old days -> 92%; deleting _day(20) -> 88%; deleting _day(15) -> 84% <= 85 stop
    usage = _UsageByRemainingDays(tmp_path, old_days, base=80.0, per_day=4.0)
    report = run_maintenance(tmp_path, now=NOW, usage_pct=usage)

    book = _stream_dir(tmp_path, "book")
    assert not (book / _day(20)).exists()
    assert not (book / _day(15)).exists()
    assert (book / _day(10)).exists()          # stopped once below threshold
    assert young.exists() and trades.exists()
    assert report["pressure"]["triggered"]
    assert [d["day"] for d in report["pressure"]["deleted"]] == \
        sorted([_day(20), _day(15)])           # oldest first
    assert not report["pressure"]["still_above"]


def test_pressure_never_deletes_below_the_floor(tmp_path, caplog):
    old_days = [_day(20), _day(10)]
    for day in old_days:
        _write_shard(tmp_path, "book", day, _book_rows(day, 2), 0)
    floor_edge = _write_shard(tmp_path, "book", _day(PRESSURE_FLOOR_DAYS),
                              _book_rows(_day(PRESSURE_FLOOR_DAYS), 2), 0)
    young = _write_shard(tmp_path, "book", _day(2), _book_rows(_day(2), 2), 0)
    trades = _write_shard(tmp_path, "trades", _day(30), _trade_rows(_day(30), 2), 0)

    with caplog.at_level(logging.CRITICAL):
        report = run_maintenance(tmp_path, now=NOW, usage_pct=lambda p: 95.0)

    book = _stream_dir(tmp_path, "book")
    assert not (book / _day(20)).exists() and not (book / _day(10)).exists()
    # exactly-floor-age and younger book days survive; trades untouched
    assert floor_edge.exists() and young.exists() and trades.exists()
    assert report["pressure"]["still_above"]
    critical = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert len(critical) >= 2  # trigger + unresolved alert


# --- dry run + CLI -------------------------------------------------------------------


def test_dry_run_deletes_and_writes_nothing(tmp_path):
    day = _day(3)
    _write_shard(tmp_path, "book", day, _book_rows(day, 3), 0)
    _write_shard(tmp_path, "book", day, _book_rows(day, 3, start=3), 1)
    _write_shard(tmp_path, "book", _day(15), _book_rows(_day(15), 2), 0)
    _write_shard(tmp_path, "book", _day(25), _book_rows(_day(25), 2), 0)
    _write_shard(tmp_path, "trades", _day(70), _trade_rows(_day(70), 2), 0)
    files_before = _lake_files(tmp_path)

    report = run_maintenance(tmp_path, now=NOW, dry_run=True,
                             usage_pct=lambda p: 95.0)

    assert _lake_files(tmp_path) == files_before   # nothing created or deleted
    assert report["dry_run"]
    assert report["streams"]["book"]["days_compacted"] == 2   # _day(3) + _day(15)
    assert report["streams"]["book"]["shards_merged"] == 3
    assert report["streams"]["book"]["days_deleted"] == 2     # retention + pressure plan
    assert report["streams"]["trades"]["days_deleted"] == 1
    assert report["pressure"]["triggered"] and report["pressure"]["deleted"]


def test_cli_dry_run_prints_report(tmp_path, capsys):
    day = _day(2)
    _write_shard(tmp_path, "book", day, _book_rows(day, 3), 0)
    assert main(["--data-root", str(tmp_path), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "tick-lake maintenance report" in out
    assert "DRY-RUN" in out
    assert "book" in out


def test_compact_verification_failure_keeps_shards(tmp_path, monkeypatch, caplog):
    day = _day(2)
    d = _write_shard(tmp_path, "book", day, _book_rows(day, 3), 0)
    _write_shard(tmp_path, "book", day, _book_rows(day, 3, start=3), 1)

    import vnedge.data.tick_lake_maintenance as tlm

    class _BadMeta:
        class metadata:
            num_rows = 999

    monkeypatch.setattr(tlm.pq, "ParquetFile", lambda _p: _BadMeta)
    with caplog.at_level(logging.ERROR):
        assert compact_day(d, now=NOW) is None
    assert len(list(d.glob("*.parquet"))) == 2      # shards untouched
    assert not list(d.glob(".*.tmp"))               # temp cleaned up
    assert any("row-count mismatch" in r.message for r in caplog.records)
