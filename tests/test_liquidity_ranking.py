"""Per-symbol liquidity & fee-wall ranking."""

import pandas as pd
import pytest

from vnedge.exchange.tick_recorder import _book_row
from vnedge.research.liquidity_ranking import (
    compute_profile,
    list_recorded_symbols,
    profile_day,
    rank_symbols,
)
from vnedge.scalping.depth import OrderBookL2
from vnedge.scalping.replay_backtester import ReplayFees

SYM = "BTC/USDT:USDT"
T0 = 1_751_000_000_000


def _books(spread_bps, n=200, qty=5.0):
    # build n snapshots with a fixed spread around mid 100
    half = 100.0 * spread_bps / 2 / 10_000.0
    from datetime import UTC, datetime
    out = []
    for i in range(n):
        bid = 100.0 - half
        ask = 100.0 + half
        b = OrderBookL2(SYM, ((bid, qty), (bid - 0.1, qty)),
                        ((ask, qty), (ask + 0.1, qty)),
                        datetime.fromtimestamp((T0 + i * 1000) / 1000, tz=UTC))
        out.append((T0 + i * 1000, b))
    return out


def test_fee_wall_and_spread_ratio():
    fees = ReplayFees(maker_bps=2.0, taker_bps=5.0, slippage_bps=1.0)  # wall = 8bps
    # tight-spread symbol: spread 0.5bps << 8bps fee wall
    p = compute_profile("binanceusdm", SYM, _books(0.5), None, fees, near_bps=5.0)
    assert p.fee_wall_bps == 8.0
    assert p.spread_bps_p50 == pytest.approx(0.5)
    assert p.spread_to_fee_wall == pytest.approx(0.5 / 8.0)
    assert p.spread_clears_fee_wall_pct == 0.0        # never clears
    assert p.verdict == "SPREAD_BELOW_FEE_WALL"


def test_wide_spread_clears_fee_wall():
    fees = ReplayFees(maker_bps=1.0, taker_bps=1.0, slippage_bps=0.0)  # wall = 2bps
    p = compute_profile("binanceusdm", SYM, _books(5.0), None, fees, near_bps=50.0)
    assert p.spread_clears_fee_wall_pct == 100.0      # 5bps >= 2bps every snapshot
    assert p.spread_to_fee_wall > 1.0
    assert p.verdict == "SPREAD_CLEARS"


def test_under_sampled_verdict():
    p = compute_profile("binanceusdm", SYM, _books(5.0, n=10), None)
    assert p.book_snapshots == 10
    assert p.verdict == "UNDER_SAMPLED"


def test_ranking_orders_by_spread_to_fee_wall():
    fees = ReplayFees()
    tight = compute_profile("binanceusdm", "AAA/USDT:USDT", _books(0.5), None, fees)
    wide = compute_profile("binanceusdm", "BBB/USDT:USDT", _books(4.0), None, fees)
    ranked = rank_symbols([tight, wide])
    assert [p.symbol for p in ranked] == ["BBB/USDT:USDT", "AAA/USDT:USDT"]


def _write_day(tmp_path, symbol, spread_bps, n_book=200, n_trade=50):
    safe = symbol.split(":")[0].replace("/", "")
    base = tmp_path / "ticks" / "exchange=binanceusdm" / f"symbol={safe}"
    (base / "stream=book" / "20260705").mkdir(parents=True)
    (base / "stream=trades" / "20260705").mkdir(parents=True)
    half = 100.0 * spread_bps / 2 / 10_000.0
    rows = []
    for i in range(n_book):
        ob = {"bids": [[100.0 - half - j * 0.1, 5.0] for j in range(10)],
              "asks": [[100.0 + half + j * 0.1, 5.0] for j in range(10)]}
        rows.append(_book_row(ob, 10, T0 + i * 1000))
    pd.DataFrame(rows).to_parquet(base / "stream=book" / "20260705" / "s.parquet")
    pd.DataFrame([{"ts_ms": T0 + i * 1000, "price": 100.0, "amount": 1.0, "side": "buy"}
                  for i in range(n_trade)]
                 ).to_parquet(base / "stream=trades" / "20260705" / "s.parquet")


def test_profile_day_over_recorded_symbols(tmp_path):
    _write_day(tmp_path, "BTC/USDT:USDT", spread_bps=0.5)
    _write_day(tmp_path, "DOGE/USDT:USDT", spread_bps=6.0)
    assert set(list_recorded_symbols(tmp_path, "binanceusdm")) == {
        "BTC/USDT:USDT", "DOGE/USDT:USDT"}
    profiles = profile_day(tmp_path, "binanceusdm", "20260705")
    # wider-spread DOGE ranks above tight BTC
    assert [p.symbol for p in profiles][0] == "DOGE/USDT:USDT"
    btc = next(p for p in profiles if p.symbol == "BTC/USDT:USDT")
    assert btc.trades == 50 and btc.trades_per_min > 0
