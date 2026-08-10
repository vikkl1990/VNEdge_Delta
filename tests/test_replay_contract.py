"""Scalper Replay Contract conformance — invariants not covered elsewhere.

Locks the same-timestamp ordering tie-break (I7) and the L2 book schema, so the
contract in docs/SCALPER_REPLAY_CONTRACT.md and the code cannot silently drift.
The rest of the invariant table lives in test_replay_backtester.py /
test_tick_recorder.py.
"""

import pandas as pd

from vnedge.exchange.tick_recorder import _book_row
from vnedge.scalping.replay_backtester import load_tick_events

SYM = "BTC/USDT:USDT"
T0 = 1_751_000_000_000


def test_book_precedes_trade_at_equal_timestamp(tmp_path):
    # I7: at equal ts_ms the book state must be applied before the trade that
    # executes against it.
    base = tmp_path / "ticks" / "exchange=binanceusdm" / "symbol=BTCUSDT"
    (base / "stream=book").mkdir(parents=True)
    (base / "stream=trades").mkdir(parents=True)
    pd.DataFrame([{"ts_ms": T0, "bid": 100.0, "bid_qty": 5.0, "ask": 100.1, "ask_qty": 5.0}]
                 ).to_parquet(base / "stream=book" / "20260704.parquet")
    pd.DataFrame([{"ts_ms": T0, "price": 100.0, "amount": 1.0, "side": "sell"}]
                 ).to_parquet(base / "stream=trades" / "20260704.parquet")
    events = load_tick_events(tmp_path, "binanceusdm", SYM, "20260704")
    assert [k for _, k, _ in events] == ["book", "trade"]  # book first at equal ts


# The exact L2 book row schema declared in docs/SCALPER_REPLAY_CONTRACT.md §2.2
CONTRACT_BOOK_COLUMNS = (
    {"ts_ms", "bid", "bid_qty", "ask", "ask_qty"}
    | {f"bid_px_{i}" for i in range(10)} | {f"bid_qty_{i}" for i in range(10)}
    | {f"ask_px_{i}" for i in range(10)} | {f"ask_qty_{i}" for i in range(10)}
)


def test_replay_contract_book_schema():
    ob = {"bids": [[100.0 - i * 0.1, 5.0] for i in range(10)],
          "asks": [[100.1 + i * 0.1, 5.0] for i in range(10)]}
    row = _book_row(ob, levels=10, ts_ms=T0)
    assert set(row) == CONTRACT_BOOK_COLUMNS       # schema locked to the contract
    # L1 aliases equal ladder level 0 (contract §2.2 invariant)
    assert row["bid"] == row["bid_px_0"] and row["ask"] == row["ask_px_0"]
    assert row["bid_qty"] == row["bid_qty_0"] and row["ask_qty"] == row["ask_qty_0"]
