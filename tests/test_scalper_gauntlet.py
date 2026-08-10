"""1m scalper strategy signal logic + tick recorder buffer/flush."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd

from vnedge.data.schemas import normalize_candles
from vnedge.exchange.tick_recorder import TickRecorder, _Buffer
from vnedge.strategy.scalper_1m import Scalper1m

BASE = 1_750_000_000_000
MIN = 60_000


def candles(bars):
    """bars: list of (open, high, low, close, volume)."""
    raw = [[BASE + i * MIN, o, h, low, c, v] for i, (o, h, low, c, v) in enumerate(bars)]
    return normalize_candles(raw)


def small_scalper(**kw):
    params = dict(flow_window=2, volume_z_window=20, momentum_bars=2,
                  atr_window=10, min_volume_z=0.0)
    params.update(kw)
    return Scalper1m(**params)


def test_scalp_long_on_buy_flow_and_momentum():
    # rising bars that close near their highs (buy flow) with a volume pop
    bars = [(100.0, 100.5, 99.8, 100.0, 10.0)] * 25
    for i in range(6):
        p = 100.0 + i * 0.5
        bars.append((p, p + 0.6, p - 0.05, p + 0.55, 30.0))  # close near high
    strat = small_scalper()
    df = strat.prepare(candles(bars))
    fired = [strat.signal(df, i) for i in range(strat.warmup_bars, len(df))]
    longs = [s for s in fired if s and s.side == "long"]
    assert longs, "expected a long scalp on buy-flow + momentum"
    s = longs[0]
    assert s.stop_price < s.take_profit_price  # stop below, target above
    assert "scalp long" in s.reason


def test_scalp_short_on_sell_flow():
    bars = [(100.0, 100.2, 99.5, 100.0, 10.0)] * 25
    for i in range(6):
        p = 100.0 - i * 0.5
        bars.append((p, p + 0.05, p - 0.6, p - 0.55, 30.0))  # close near low
    strat = small_scalper()
    df = strat.prepare(candles(bars))
    fired = [strat.signal(df, i) for i in range(strat.warmup_bars, len(df))]
    shorts = [s for s in fired if s and s.side == "short"]
    assert shorts and shorts[0].stop_price > shorts[0].take_profit_price


def test_no_signal_below_volume_floor():
    bars = [(100.0, 100.6, 99.9, 100.55, 10.0)] * 40  # buy flow but flat volume
    strat = small_scalper(min_volume_z=3.0)  # unreachable floor
    df = strat.prepare(candles(bars))
    assert all(strat.signal(df, i) is None for i in range(strat.warmup_bars, len(df)))


def test_stop_required_by_construction():
    # SignalIntent itself forbids non-positive stop; scalper never emits one
    strat = small_scalper()
    bars = [(100.0, 100.5, 99.8, 100.0, 10.0)] * 30
    df = strat.prepare(candles(bars))
    for i in range(strat.warmup_bars, len(df)):
        s = strat.signal(df, i)
        if s is not None:
            assert s.stop_price > 0


def test_fee_sweep_script_still_runs_from_scripts_dir():
    """The fee-wall sweep CLI moved out of the package to scripts/; it must
    stay runnable as `python scripts/scalper_fee_sweep.py --help`."""
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "scripts" / "scalper_fee_sweep.py"
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(repo_root / "src"), env.get("PYTHONPATH")) if p
    )
    proc = subprocess.run(
        [sys.executable, str(script), "--help"],
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "fee-wall" in proc.stdout


# --- tick recorder buffer ---------------------------------------------------------

def test_buffer_flushes_to_parquet(tmp_path):
    buf = _Buffer(tmp_path, "binanceusdm", "BTC/USDT:USDT", "trades")
    for i in range(3):
        buf.add({"ts_ms": BASE + i * 1000, "price": 100.0 + i, "amount": 1.0, "side": "buy"})
    n = buf.flush(now=0.0)
    assert n == 3
    files = list((tmp_path / "ticks").rglob("*.parquet"))
    assert len(files) == 1
    df = pd.read_parquet(files[0])
    assert len(df) == 3 and df["price"].iloc[-1] == 102.0


def test_buffer_append_and_day_split(tmp_path):
    buf = _Buffer(tmp_path, "binanceusdm", "BTC/USDT:USDT", "book")
    day1 = BASE                       # some UTC day
    day2 = BASE + 86_400_000          # next day
    buf.add({"ts_ms": day1, "bid": 1.0})
    buf.add({"ts_ms": day2, "bid": 2.0})
    buf.flush(now=0.0)
    files = sorted((tmp_path / "ticks").rglob("*.parquet"))
    assert len(files) == 2  # split across two day directories, one shard each
    # a second flush writes a NEW shard (atomic, never rewrites) — day1 gets a
    # second shard; its rows accumulate across shards, not within one file
    buf.add({"ts_ms": day1 + 1000, "bid": 1.5})
    buf.flush(now=0.0)
    day1_str = pd.to_datetime(day1, unit="ms", utc=True).strftime("%Y%m%d")
    day1_dir = (tmp_path / "ticks" / "exchange=binanceusdm"
                / "symbol=BTCUSDT" / "stream=book" / day1_str)
    shards = sorted(day1_dir.glob("*.parquet"))
    assert len(shards) == 2  # two flushes -> two shards, never a rewritten file
    rows = pd.concat([pd.read_parquet(s) for s in shards], ignore_index=True)
    assert list(rows["bid"]) == [1.0, 1.5]  # both day-1 rows, across shards


def test_should_flush_thresholds(tmp_path):
    buf = _Buffer(tmp_path, "b", "BTC/USDT:USDT", "trades")
    assert not buf.should_flush(now=0.0)  # empty
    buf.add({"ts_ms": BASE})
    assert not buf.should_flush(now=1.0)         # 1 row, 1s: no
    assert buf.should_flush(now=100.0)           # time threshold crossed


def test_recorder_records_from_fake_stream(tmp_path):
    """No network: drive watch_trades from a scripted fake, confirm persist."""

    class FakeCcxtPro:
        def __init__(self, *_a, **_k):
            self._sent = False

        async def watch_trades(self, symbol):
            if self._sent:
                raise asyncio.CancelledError
            self._sent = True
            return [{"timestamp": BASE, "price": 100.0, "amount": 2.0, "side": "buy"}]

        async def watch_order_book(self, symbol, limit=5):
            raise asyncio.CancelledError

        async def close(self):
            pass

    rec = TickRecorder.__new__(TickRecorder)
    rec._ex = FakeCcxtPro()
    rec.exchange_id = "binanceusdm"
    rec.symbols = ["BTC/USDT:USDT"]
    rec.root = tmp_path
    rec.trade_count = 0
    rec.book_count = 0

    async def drive():
        task = asyncio.create_task(rec._watch_trades("BTC/USDT:USDT", lambda: 0.0))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())
    assert rec.trade_count == 1
    files = list((tmp_path / "ticks").rglob("*.parquet"))
    assert files and pd.read_parquet(files[0])["price"].iloc[0] == 100.0
