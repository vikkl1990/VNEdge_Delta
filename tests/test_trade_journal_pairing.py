"""Paper trades are reconstructed by pairing open + close fills."""

from vnedge.dashboard.trade_journal import _paired_actual_trades, _with_captured_bps


def test_pairs_open_and_close_into_a_complete_long_trade():
    fills = [
        {"lane": "L", "ts": "2026-07-15T13:00:00Z", "side": "buy", "quantity": 0.84,
         "price": 582.76, "fee_usd": 0.24, "realized_pnl_usd": 0.0},
        {"lane": "L", "ts": "2026-07-15T16:00:00Z", "side": "sell", "quantity": 0.84,
         "price": 576.51, "fee_usd": 0.24, "realized_pnl_usd": -5.25},
    ]
    trades = _paired_actual_trades(fills)
    assert len(trades) == 1
    t = trades[0]
    assert t["side"] == "long"                 # opened with a buy
    assert t["entry_price"] == 582.76 and t["exit_price"] == 576.51
    assert round(t["net_after_this_fill_fee_usd"], 2) == round(-5.25 - 0.48, 2)
    enriched = _with_captured_bps(t)
    assert enriched["captured_bps_basis"] == "gross"   # real entry->exit move
    assert enriched["captured_bps"] < 0                # long that dropped


def test_short_trade_and_unpaired_close_is_safe():
    fills = [
        {"lane": "L", "ts": "t1", "side": "sell", "quantity": 1.0, "price": 100.0,
         "fee_usd": 0.05, "realized_pnl_usd": 0.0},                       # opens short
        {"lane": "L", "ts": "t2", "side": "buy", "quantity": 1.0, "price": 98.0,
         "fee_usd": 0.05, "realized_pnl_usd": 2.0},                       # closes it
        {"lane": "L", "ts": "t3", "side": "buy", "quantity": 1.0, "price": 97.0,
         "fee_usd": 0.05, "realized_pnl_usd": 3.0},                       # close, no open
    ]
    trades = _paired_actual_trades(fills)
    assert trades[0]["side"] == "short" and trades[0]["entry_price"] == 100.0
    # a close with no matching open still produces a row (no entry price)
    assert trades[1]["entry_price"] is None
    # captured_bps falls back to net-on-notional when there's no entry price
    assert _with_captured_bps(trades[1])["captured_bps_basis"] == "net"


def test_separate_lanes_do_not_cross_pair():
    fills = [
        {"lane": "A", "ts": "1", "side": "buy", "quantity": 1.0, "price": 10.0, "fee_usd": 0.0, "realized_pnl_usd": 0.0},
        {"lane": "B", "ts": "1", "side": "buy", "quantity": 1.0, "price": 20.0, "fee_usd": 0.0, "realized_pnl_usd": 0.0},
        {"lane": "A", "ts": "2", "side": "sell", "quantity": 1.0, "price": 11.0, "fee_usd": 0.0, "realized_pnl_usd": 1.0},
    ]
    trades = _paired_actual_trades(fills)
    assert len(trades) == 1
    assert trades[0]["lane"] == "A" and trades[0]["entry_price"] == 10.0
