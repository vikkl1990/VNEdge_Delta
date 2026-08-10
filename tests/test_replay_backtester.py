"""Tick replay backtester — fill model, exits, adverse selection, fees, loader."""

from datetime import UTC, datetime

import pandas as pd
import pytest

from vnedge.scalping.microstructure import TopOfBook, TradeTick
from vnedge.scalping.parameter_registry import ExitPolicy
from vnedge.scalping.replay_backtester import (
    ImbalanceScalper,
    ReplayFees,
    ReplayQuote,
    TickReplayBacktester,
    load_tick_events,
)

SYM = "BTC/USDT:USDT"
T0 = 1_750_000_000_000


def book(ts_ms, bid, bid_sz, ask, ask_sz):
    return (ts_ms, "book", TopOfBook(
        symbol=SYM, bid=bid, bid_size=bid_sz, ask=ask, ask_size=ask_sz,
        event_time=datetime.fromtimestamp(ts_ms / 1000, tz=UTC)))


def trade(ts_ms, price, qty, side):
    return (ts_ms, "trade", TradeTick(
        symbol=SYM, price=price, quantity=qty, taker_side=side,
        event_time=datetime.fromtimestamp(ts_ms / 1000, tz=UTC)))


class AlwaysBuy:
    """Quotes a buy on the first book, wide ttl, tight-ish stop/target."""
    def __init__(self, stop_bps=50.0, target_bps=50.0, ttl_ms=100_000):
        self.stop_bps, self.target_bps, self.ttl_ms = stop_bps, target_bps, ttl_ms
        self._done = False

    def quote(self, features, top):
        if self._done:
            return None
        self._done = True
        return ReplayQuote("buy", self.ttl_ms, self.stop_bps, self.target_bps)


def test_conservative_fill_requires_trade_through():
    # resting bid at 100.0; a BUY-taker trade must NOT fill it (wrong side)
    events = [
        book(T0, 100.0, 5.0, 100.1, 5.0),
        trade(T0 + 10, 100.0, 1.0, "buy"),    # buyer lifting ask — not our fill
        trade(T0 + 20, 100.05, 1.0, "buy"),
    ]
    res = TickReplayBacktester().run(events, AlwaysBuy())
    assert res.quotes_placed == 1
    assert res.filled == 0  # never filled by a same-side taker


def test_touch_at_limit_does_not_fill():
    # a seller printing AT our bid (not through it) assumes we're front-of-queue;
    # conservative model refuses the fill and lets the quote expire.
    events = [
        book(T0, 100.0, 5.0, 100.1, 5.0),
        trade(T0 + 10, 100.0, 1.0, "sell"),        # AT the bid, not through
        book(T0 + 5000, 100.0, 5.0, 100.1, 5.0),   # ttl expiry
    ]
    res = TickReplayBacktester().run(events, AlwaysBuy(ttl_ms=1000))
    assert res.filled == 0 and res.missed_fills == 1


def test_same_instant_trade_does_not_fill():
    # a trade at the SAME ms as quote placement was already in flight before our
    # order could join the queue — the latency guard rejects it.
    events = [
        book(T0, 100.0, 5.0, 100.1, 5.0),          # quote placed at T0
        trade(T0, 99.99, 1.0, "sell"),             # same ms, through-price
        book(T0 + 5000, 100.0, 5.0, 100.1, 5.0),
    ]
    res = TickReplayBacktester().run(events, AlwaysBuy(ttl_ms=1000))
    assert res.filled == 0


def test_stale_quote_expires_on_trade_event_before_fill():
    # Expiry must be checked on every event, not only book updates. Otherwise a
    # long-dead quote can fill just because the next event happens to be a trade.
    events = [
        book(T0, 100.0, 5.0, 100.1, 5.0),
        trade(T0 + 2000, 99.99, 1.0, "sell"),
    ]
    res = TickReplayBacktester().run(events, AlwaysBuy(ttl_ms=1000))
    assert res.filled == 0
    assert res.missed_fills == 1


def test_trade_at_exact_ttl_boundary_does_not_fill():
    events = [
        book(T0, 100.0, 5.0, 100.1, 5.0),
        trade(T0 + 1000, 99.99, 1.0, "sell"),
    ]
    res = TickReplayBacktester().run(events, AlwaysBuy(ttl_ms=1000))
    assert res.filled == 0
    assert res.missed_fills == 1


def test_quote_censored_at_replay_end_is_not_missed():
    events = [
        book(T0, 100.0, 5.0, 100.1, 5.0),
    ]
    res = TickReplayBacktester().run(events, AlwaysBuy(ttl_ms=1000))
    assert res.filled == 0
    assert res.missed_fills == 0
    assert res.open_quotes_at_end == 1


def test_seller_hitting_bid_fills_us():
    events = [
        book(T0, 100.0, 5.0, 100.1, 5.0),
        trade(T0 + 10, 99.99, 1.0, "sell"),  # sells through our bid -> fill at 100.0
        book(T0 + 20, 100.6, 5.0, 100.7, 5.0),  # price ran up -> target
    ]
    res = TickReplayBacktester(ReplayFees(maker_bps=2, taker_bps=5, slippage_bps=0)
                               ).run(events, AlwaysBuy(target_bps=40.0))
    assert res.filled == 1
    t = res.trades[0]
    assert t.exit_reason == "target"
    assert t.entry_price == pytest.approx(100.0)
    # gross ~ (100.6 - 100.0)/100 = 60bps; net = 60 - 7 fees = ~53bps
    assert t.gross_bps == pytest.approx(60.0, abs=0.5)
    assert t.net_bps == pytest.approx(t.gross_bps - 7.0, abs=0.01)


def test_stop_exit_is_a_loss():
    events = [
        book(T0, 100.0, 5.0, 100.1, 5.0),
        trade(T0 + 10, 99.99, 1.0, "sell"),  # sells through our bid -> fill at 100.0
        book(T0 + 20, 99.4, 5.0, 99.5, 5.0),       # price dropped -> stop
    ]
    res = TickReplayBacktester(ReplayFees(slippage_bps=0)).run(
        events, AlwaysBuy(stop_bps=40.0))
    assert res.filled == 1
    assert res.trades[0].exit_reason == "stop"
    assert res.trades[0].net_bps < 0


def test_long_target_requires_bid_through_target_not_just_ask():
    events = [
        book(T0, 100.0, 5.0, 100.1, 5.0),
        trade(T0 + 10, 99.99, 1.0, "sell"),  # sells THROUGH our 100.0 bid -> fill at 100.0
        book(T0 + 20, 100.1, 5.0, 100.5, 5.0),  # ask through target, bid not sellable there
        book(T0 + 30, 100.5, 5.0, 100.6, 5.0),  # now the bid can realize target
    ]
    res = TickReplayBacktester(ReplayFees(slippage_bps=0)).run(
        events, AlwaysBuy(target_bps=40.0))
    assert res.filled == 1
    assert res.trades[0].exit_reason == "target"
    assert res.trades[0].exit_ts == datetime.fromtimestamp((T0 + 30) / 1000, tz=UTC)


class AlwaysSell:
    def __init__(self, stop_bps=50.0, target_bps=50.0, ttl_ms=100_000):
        self.stop_bps, self.target_bps, self.ttl_ms = stop_bps, target_bps, ttl_ms
        self._done = False

    def quote(self, features, top):
        if self._done:
            return None
        self._done = True
        return ReplayQuote("sell", self.ttl_ms, self.stop_bps, self.target_bps)


def test_short_target_requires_ask_through_target_not_just_bid():
    events = [
        book(T0, 100.0, 5.0, 100.1, 5.0),
        trade(T0 + 10, 100.11, 1.0, "buy"),  # buys THROUGH our 100.1 ask -> fill at 100.1
        book(T0 + 20, 99.5, 5.0, 100.0, 5.0),  # bid through target, ask not buyable there
        book(T0 + 30, 99.4, 5.0, 99.6, 5.0),   # now the ask can realize target
    ]
    res = TickReplayBacktester(ReplayFees(slippage_bps=0)).run(
        events, AlwaysSell(target_bps=40.0))
    assert res.filled == 1
    assert res.trades[0].exit_reason == "target"
    assert res.trades[0].exit_ts == datetime.fromtimestamp((T0 + 30) / 1000, tz=UTC)


def test_adverse_selection_measured():
    # fill, then mid drifts against the long before recovering to target
    events = [
        book(T0, 100.0, 5.0, 100.1, 5.0),
        trade(T0 + 10, 99.99, 1.0, "sell"),  # sells THROUGH our 100.0 bid -> fill at 100.0
        book(T0 + 20, 99.7, 5.0, 99.8, 5.0),       # adverse: mid down ~30bps
        book(T0 + 30, 100.6, 5.0, 100.7, 5.0),     # then up to target
    ]
    res = TickReplayBacktester(ReplayFees(slippage_bps=0)).run(
        events, AlwaysBuy(stop_bps=60.0, target_bps=40.0))
    assert res.filled == 1
    assert res.trades[0].adverse_bps < -20.0  # captured the adverse move


def test_adverse_cut_exit_can_leave_before_static_stop():
    policy = ExitPolicy(
        "test_cut", "adverse_cut", ttl_ms=10_000, stop_bps=60.0,
        target_bps=80.0, max_hold_ms=10_000, adverse_cut_bps=20.0,
    )
    events = [
        book(T0, 100.0, 5.0, 100.1, 5.0),
        trade(T0 + 10, 99.99, 1.0, "sell"),
        book(T0 + 20, 99.7, 5.0, 99.8, 5.0),  # not static stop, but adverse cut
    ]
    res = TickReplayBacktester(ReplayFees(slippage_bps=0), exit_policy=policy).run(
        events, AlwaysBuy(stop_bps=60.0, target_bps=80.0))

    assert res.filled == 1
    assert res.trades[0].exit_reason == "adverse_cut"
    assert -60.0 < res.trades[0].gross_bps < -20.0


def test_trailing_exit_locks_in_partial_move():
    policy = ExitPolicy(
        "test_trail", "adaptive_trail", ttl_ms=10_000, stop_bps=60.0,
        target_bps=100.0, max_hold_ms=10_000, trail_after_bps=30.0,
        trail_distance_bps=20.0,
    )
    events = [
        book(T0, 100.0, 5.0, 100.1, 5.0),
        trade(T0 + 10, 99.99, 1.0, "sell"),
        book(T0 + 20, 100.5, 5.0, 100.6, 5.0),  # favorable > 30bps
        book(T0 + 30, 100.25, 5.0, 100.35, 5.0),  # retrace triggers trail
    ]
    res = TickReplayBacktester(ReplayFees(slippage_bps=0), exit_policy=policy).run(
        events, AlwaysBuy(stop_bps=60.0, target_bps=100.0))

    assert res.filled == 1
    assert res.trades[0].exit_reason == "trail"
    assert res.trades[0].gross_bps > 0


def test_end_close_uses_tradable_bid_not_mid_for_long():
    events = [
        book(T0, 100.0, 5.0, 100.1, 5.0),
        trade(T0 + 10, 99.99, 1.0, "sell"),
        book(T0 + 20, 100.0, 5.0, 102.0, 5.0),  # wide spread; mid would flatter exit
    ]
    res = TickReplayBacktester(ReplayFees(slippage_bps=0)).run(
        events, AlwaysBuy(stop_bps=1000.0, target_bps=1000.0))
    assert res.filled == 1
    assert res.trades[0].exit_reason == "end"
    assert res.trades[0].exit_price == pytest.approx(100.0)
    assert res.trades[0].gross_bps == pytest.approx(0.0)


def test_missed_fill_on_ttl_expiry():
    events = [
        book(T0, 100.0, 5.0, 100.1, 5.0),
        book(T0 + 5000, 100.0, 5.0, 100.1, 5.0),   # 5s later, still no fill
    ]
    res = TickReplayBacktester().run(events, AlwaysBuy(ttl_ms=1000))
    assert res.quotes_placed == 1
    assert res.filled == 0
    assert res.missed_fills == 1


def test_imbalance_scalper_quotes_on_heavy_bid():
    s = ImbalanceScalper(min_imbalance=0.3, max_spread_bps=20.0)
    top = TopOfBook(SYM, 100.0, 9.0, 100.1, 1.0,
                    datetime.fromtimestamp(T0 / 1000, tz=UTC))  # bid-heavy
    from vnedge.scalping.features import IncrementalFeatureEngine
    feats = IncrementalFeatureEngine().on_book(top)
    q = s.quote(feats, top)
    assert q is not None and q.side == "buy"


def test_wide_spread_blocks_quote():
    s = ImbalanceScalper(max_spread_bps=1.0)
    top = TopOfBook(SYM, 100.0, 9.0, 101.0, 1.0,
                    datetime.fromtimestamp(T0 / 1000, tz=UTC))  # 100bps spread
    from vnedge.scalping.features import IncrementalFeatureEngine
    feats = IncrementalFeatureEngine().on_book(top)
    assert s.quote(feats, top) is None


def test_load_tick_events_from_parquet(tmp_path):
    base = tmp_path / "ticks" / "exchange=binanceusdm" / "symbol=BTCUSDT"
    (base / "stream=trades").mkdir(parents=True)
    (base / "stream=book").mkdir(parents=True)
    pd.DataFrame([{"ts_ms": T0 + 5, "price": 100.0, "amount": 1.0, "side": "sell"}]
                 ).to_parquet(base / "stream=trades" / "20260704.parquet")
    pd.DataFrame([{"ts_ms": T0, "bid": 100.0, "bid_qty": 5.0, "ask": 100.1, "ask_qty": 5.0}]
                 ).to_parquet(base / "stream=book" / "20260704.parquet")
    events = load_tick_events(tmp_path, "binanceusdm", SYM, "20260704")
    assert len(events) == 2
    assert events[0][1] == "book" and events[1][1] == "trade"  # time-ordered


def test_loader_skips_invalid_trade_side(tmp_path):
    base = tmp_path / "ticks" / "exchange=binanceusdm" / "symbol=BTCUSDT" / "stream=trades"
    base.mkdir(parents=True)
    pd.DataFrame([
        {"ts_ms": T0, "price": 100.0, "amount": 1.0, "side": ""},
        {"ts_ms": T0 + 1, "price": 100.1, "amount": 2.0, "side": "buy"},
    ]).to_parquet(base / "20260704.parquet")
    events = load_tick_events(tmp_path, "binanceusdm", SYM, "20260704")
    assert len(events) == 1
    assert events[0][2].taker_side == "buy"


def test_crossed_book_snapshot_skipped(tmp_path):
    base = tmp_path / "ticks" / "exchange=binanceusdm" / "symbol=BTCUSDT" / "stream=book"
    base.mkdir(parents=True)
    pd.DataFrame([
        {"ts_ms": T0, "bid": 100.2, "bid_qty": 5.0, "ask": 100.0, "ask_qty": 5.0},  # crossed
        {"ts_ms": T0 + 1, "bid": 100.0, "bid_qty": 5.0, "ask": 100.1, "ask_qty": 5.0},
    ]).to_parquet(base / "20260704.parquet")
    events = load_tick_events(tmp_path, "binanceusdm", SYM, "20260704")
    assert len(events) == 1  # crossed snapshot dropped


def test_loader_reads_sharded_l2_layout(tmp_path):
    from vnedge.exchange.tick_recorder import _book_row
    book_dir = (tmp_path / "ticks" / "exchange=binanceusdm"
                / "symbol=BTCUSDT" / "stream=book" / "20260704")
    book_dir.mkdir(parents=True)
    ob = {"bids": [[100.0 - i * 0.1, 5.0] for i in range(10)],
          "asks": [[100.1 + i * 0.1, 5.0] for i in range(10)]}
    # two L2 shards; the final events.sort makes ts order deterministic
    pd.DataFrame([_book_row(ob, 10, T0 + 100)]).to_parquet(book_dir / "b.parquet")
    pd.DataFrame([_book_row(ob, 10, T0)]).to_parquet(book_dir / "a.parquet")
    events = load_tick_events(tmp_path, "binanceusdm", SYM, "20260704")
    assert [e[0] for e in events] == [T0, T0 + 100]        # time-ordered
    assert all(k == "book" for _, k, _ in events)
    # L2 rows still feed TopOfBook via their level-0 aliases
    assert events[0][2].bid == 100.0 and events[0][2].ask == 100.1


def test_loader_merges_shard_dir_and_legacy_single_file(tmp_path):
    from vnedge.exchange.tick_recorder import _book_row
    base = (tmp_path / "ticks" / "exchange=binanceusdm"
            / "symbol=BTCUSDT" / "stream=book")
    (base / "20260704").mkdir(parents=True)
    ob = {"bids": [[100.0, 5.0]], "asks": [[100.1, 5.0]]}
    pd.DataFrame([_book_row(ob, 10, T0 + 50)]).to_parquet(base / "20260704" / "s.parquet")
    pd.DataFrame([{"ts_ms": T0, "bid": 100.0, "bid_qty": 5.0, "ask": 100.1, "ask_qty": 5.0}]
                 ).to_parquet(base / "20260704.parquet")     # legacy L1 single file
    events = load_tick_events(tmp_path, "binanceusdm", SYM, "20260704")
    assert [e[0] for e in events] == [T0, T0 + 50]           # both layouts merged, ordered


# --- queue-aware fill model (opt-in) --------------------------------------------

def test_queue_aware_waits_for_queue_ahead_to_clear():
    # resting buy at 100.0 with 5.0 resting ahead of us at placement.
    events = [
        book(T0, 100.0, 5.0, 100.1, 5.0),        # queue_ahead = 5.0
        trade(T0 + 10, 100.0, 2.0, "sell"),       # consumes 2 of 5 -> no fill
        trade(T0 + 20, 100.0, 2.0, "sell"),       # consumes 4 of 5 -> no fill
        trade(T0 + 30, 100.0, 2.0, "sell"),       # cumulative 6 >= 5 -> FILL
    ]
    res = TickReplayBacktester(queue_aware=True).run(events, AlwaysBuy())
    assert res.quotes_placed == 1
    assert res.filled == 1                         # filled once the queue cleared


def test_queue_aware_never_fills_if_queue_never_clears():
    events = [
        book(T0, 100.0, 100.0, 100.1, 5.0),        # deep queue ahead (100)
        trade(T0 + 10, 100.0, 2.0, "sell"),
        trade(T0 + 20, 99.99, 2.0, "sell"),        # trades through, but queue huge
    ]
    res = TickReplayBacktester(queue_aware=True).run(events, AlwaysBuy())
    assert res.filled == 0                          # queue ahead never exhausted


def test_queue_aware_stricter_than_trade_through_on_same_events():
    # one small trade-through: default fills, queue-aware (deep queue) does not
    events = [
        book(T0, 100.0, 50.0, 100.1, 5.0),
        trade(T0 + 10, 99.99, 1.0, "sell"),         # trade-through, qty 1 << queue 50
    ]
    assert TickReplayBacktester().run(events, AlwaysBuy()).filled == 1
    assert TickReplayBacktester(queue_aware=True).run(events, AlwaysBuy()).filled == 0


def test_queue_aware_default_off_preserves_trade_through():
    # sanity: the flag defaults off; identical to the strict model
    events = [
        book(T0, 100.0, 5.0, 100.1, 5.0),
        trade(T0 + 10, 99.99, 1.0, "sell"),         # trade-through fills default model
    ]
    assert TickReplayBacktester().run(events, AlwaysBuy()).filled == 1
