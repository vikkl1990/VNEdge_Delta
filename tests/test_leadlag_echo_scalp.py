"""Lead-lag echo scalp — causal impulses, lag estimate, queue-aware maker fills,
dual cost models, dual-venue loading, verdicts, folding."""

import json
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

import vnedge.research.continuous_research as cr
from vnedge.research.leadlag_echo_scalp import (
    LEADLAG_ECHO_SCALP_LATEST,
    DEFAULT_PAIRS,
    EchoScalpParams,
    EchoScalpReplayer,
    FollowerTrade,
    LeaderImpulseDetector,
    LeaderTrade,
    LeadLagPair,
    cost_models_for,
    discover_overlap_days,
    echo_verdict,
    estimate_leadlag,
    leadlag_echo_scalp_policy,
    load_follower_books,
    load_follower_trades,
    load_leader_trades,
    render_report,
    run_leadlag_echo_scalp,
    write_leadlag_echo_scalp_payload,
)
from vnedge.scalping.depth import OrderBookL2

PAIR = LeadLagPair("BTC", "binanceusdm", "BTC/USDT:USDT", "delta_india", "BTC/USD:USD")
T0 = 1_752_000_000_000

P = EchoScalpParams(
    impulse_window_ms=1_000,
    impulse_threshold_bps=8.0,
    impulse_cooldown_ms=3_000,
    response_threshold_bps=3.0,
    max_lag_ms=5_000,
    maker_ttl_ms=2_000,
    target_bps=6.0,
    stop_bps=6.0,
    hold_ms=10_000,
    notional_usd=100.0,
    min_events_for_candidate=20,
)


def _lt(ts_ms, price, amount=1.0):
    return LeaderTrade(ts_ms=ts_ms, price=price, amount=amount)


def _ft(ts_ms, price, amount, side):
    return FollowerTrade(ts_ms=ts_ms, price=price, amount=amount, taker_side=side)


def _book(ts_ms, bid, bid_qty, ask, ask_qty):
    return (ts_ms, OrderBookL2(
        symbol="BTC/USD:USD",
        bids=((bid, bid_qty),),
        asks=((ask, ask_qty),),
        event_time=datetime.fromtimestamp(ts_ms / 1000, tz=UTC),
    ))


def _run(leader, books, ftrades, params=P):
    replayer = EchoScalpReplayer(params, cost_models_for(PAIR.follower_exchange))
    return replayer.run(
        leader, books, ftrades,
        base=PAIR.base, leader_exchange=PAIR.leader_exchange,
        follower_exchange=PAIR.follower_exchange,
        follower_symbol=PAIR.follower_symbol, day="20260709")


# --- Leader impulse detection ------------------------------------------------------


def test_first_trade_never_fires_empty_window():
    det = LeaderImpulseDetector(P)
    # a lone first print has no trailing reference — it cannot be an impulse
    assert det.on_trade(_lt(T0, 100.0)) is None


def test_impulse_fires_up_and_direction():
    det = LeaderImpulseDetector(P)
    assert det.on_trade(_lt(T0, 100.0)) is None
    fired = det.on_trade(_lt(T0 + 500, 100.10))       # +10 bps over 500ms
    assert fired is not None
    assert fired.direction == "buy"
    assert fired.ref_price == pytest.approx(100.0)
    assert fired.leader_price == pytest.approx(100.10)
    assert fired.move_bps == pytest.approx(10.0)


def test_impulse_fires_down():
    det = LeaderImpulseDetector(P)
    det.on_trade(_lt(T0, 100.0))
    fired = det.on_trade(_lt(T0 + 400, 99.90))        # -10 bps
    assert fired is not None
    assert fired.direction == "sell"
    assert fired.move_bps == pytest.approx(-10.0)


def test_below_threshold_does_not_fire():
    det = LeaderImpulseDetector(P)
    det.on_trade(_lt(T0, 100.0))
    assert det.on_trade(_lt(T0 + 400, 100.05)) is None  # +5 bps < 8


def test_cooldown_suppresses_refires():
    det = LeaderImpulseDetector(P)
    # steadily climbing tape (+5 bps every 300ms): every 1s window shows a big
    # move, but the 3s cooldown lets it fire only once until it elapses.
    prices = [100.0 + 0.05 * i for i in range(13)]
    fires = [det.on_trade(_lt(T0 + 300 * i, prices[i])) is not None
             for i in range(13)]
    assert fires[2] is True                # first fire (ref 100.0, +10 bps)
    assert not any(fires[3:12])            # suppressed all through the cooldown
    assert fires[12] is True               # 3s elapsed -> fires again


def test_stale_window_prices_drop_out():
    det = LeaderImpulseDetector(P)
    det.on_trade(_lt(T0, 100.0))
    # 2s later the T0 reference has left the 1s window; small recent move only
    assert det.on_trade(_lt(T0 + 2_000, 100.05)) is None


def test_impulse_detection_is_causal_prefix_stable():
    """The stream of fire/no-fire decisions on a prefix must equal the prefix of
    the decisions on the full tape — no future print ever changes a past one."""
    tape = [
        _lt(T0, 100.0), _lt(T0 + 300, 100.02), _lt(T0 + 600, 100.12),
        _lt(T0 + 1_200, 100.20), _lt(T0 + 4_500, 99.50), _lt(T0 + 5_000, 99.40),
        _lt(T0 + 9_000, 100.30),
    ]
    full = LeaderImpulseDetector(P)
    full_dec = [full.on_trade(t) is not None for t in tape]
    assert any(full_dec)
    for k in range(len(tape)):
        det = LeaderImpulseDetector(P)
        prefix = [det.on_trade(t) is not None for t in tape[: k + 1]]
        assert prefix == full_dec[: k + 1]


# --- Cross-venue lag estimator -----------------------------------------------------


def test_lag_estimate_measures_forward_response():
    det = LeaderImpulseDetector(P)
    det.on_trade(_lt(T0, 100.0))
    imp = det.on_trade(_lt(T0 + 500, 100.10))
    books = [
        _book(T0 + 400, 99.99, 5.0, 100.01, 5.0),     # base mid 100.00
        _book(T0 + 700, 99.99, 5.0, 100.01, 5.0),     # +0 bps — no response
        _book(T0 + 900, 100.05, 5.0, 100.07, 5.0),    # mid 100.06 -> +6 bps
    ]
    stats = estimate_leadlag([imp], books, P)
    assert stats.impulses == 1
    assert stats.responded == 1
    assert stats.median_lag_ms == pytest.approx(400.0)   # T0+900 - T0+500
    assert stats.response_rate_pct == pytest.approx(100.0)


def test_lag_estimate_no_response_within_horizon():
    det = LeaderImpulseDetector(P)
    det.on_trade(_lt(T0, 100.0))
    imp = det.on_trade(_lt(T0 + 500, 100.10))
    # follower never moves up enough before max_lag_ms (5s) elapses
    books = [_book(T0 + 400, 99.99, 5.0, 100.01, 5.0),
             _book(T0 + 6_000, 100.20, 5.0, 100.22, 5.0)]  # past the horizon
    stats = estimate_leadlag([imp], books, P)
    assert stats.responded == 0
    assert stats.median_lag_ms is None


def test_lag_estimate_ignores_wrong_direction_move():
    det = LeaderImpulseDetector(P)
    det.on_trade(_lt(T0, 100.0))
    imp = det.on_trade(_lt(T0 + 500, 100.10))          # UP impulse
    books = [_book(T0 + 400, 99.99, 5.0, 100.01, 5.0),
             _book(T0 + 800, 99.90, 5.0, 99.92, 5.0)]   # follower goes DOWN
    stats = estimate_leadlag([imp], books, P)
    assert stats.responded == 0


def test_lag_estimate_no_book_before_impulse():
    det = LeaderImpulseDetector(P)
    det.on_trade(_lt(T0, 100.0))
    impulse = det.on_trade(_lt(T0 + 500, 100.10))
    books = [_book(T0 + 700, 100.05, 5.0, 100.07, 5.0)]  # only AFTER the impulse
    stats = estimate_leadlag([impulse], books, P)
    assert stats.impulses == 1
    assert stats.responded == 0                         # no base mid to compare


# --- Echo scalp replay: full resolution --------------------------------------------


def _up_impulse_leader():
    return [_lt(T0, 100.0), _lt(T0 + 500, 100.10)]      # buy impulse at T0+500


def test_full_scalp_maker_beats_taker_on_echo():
    leader = _up_impulse_leader()
    books = [
        _book(T0 + 400, 99.99, 5.0, 100.01, 5.0),        # pre-impulse (entry book)
        _book(T0 + 800, 100.08, 5.0, 100.10, 5.0),       # echo up -> both exit target
    ]
    ftrades = [_ft(T0 + 600, 99.99, 5.0, "sell")]        # clears the maker queue
    result = _run(leader, books, ftrades)
    assert result.impulses_detected == 1
    assert result.scalps_opened == 1
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.direction == "buy"
    # taker crossed the spread: entry at the ask 100.01
    assert row.taker_entry_price == pytest.approx(100.01)
    assert row.taker_exit_reason == "target"
    assert row.taker_exit_price == pytest.approx(100.08)   # sold into the bid
    assert row.taker_entry_walk_slippage_bps == pytest.approx(1.0)  # ask vs mid
    assert row.taker_fully_filled is True
    # maker rested at the bid 99.99 and filled once the 5.0 queue cleared
    assert row.maker_filled is True
    assert row.maker_fill_lag_ms == 100                    # T0+600 - T0+500
    assert row.maker_entry_price == pytest.approx(99.99)
    assert row.maker_exit_reason == "target"
    # the thesis: maker net > taker net on the same echo
    assert row.maker_net_bps > row.taker_net_bps
    models = cost_models_for(PAIR.follower_exchange)
    assert row.taker_net_bps == pytest.approx(
        models["taker_taker"].net_bps("buy", 100.01, 100.08))
    assert row.maker_net_bps == pytest.approx(
        models["maker_first"].net_bps("buy", 99.99, 100.08))
    assert row.taker_net_bps < 0 < row.maker_net_bps       # taker floor loses; maker wins


def test_scalp_stop_resolution():
    leader = _up_impulse_leader()
    books = [
        _book(T0 + 400, 99.99, 5.0, 100.01, 5.0),
        _book(T0 + 800, 99.93, 5.0, 99.95, 5.0),          # drops through the stops
    ]
    ftrades = [_ft(T0 + 600, 99.99, 5.0, "sell")]
    result = _run(leader, books, ftrades)
    row = result.rows[0]
    assert row.taker_exit_reason == "stop"                # bid 99.93 <= 99.949
    assert row.maker_exit_reason == "stop"                # bid 99.93 <= 99.984
    assert row.taker_net_bps < 0 and row.maker_net_bps < 0


def test_scalp_timeout_resolution():
    leader = _up_impulse_leader()
    books = [
        _book(T0 + 400, 99.99, 5.0, 100.01, 5.0),
        _book(T0 + 5_000, 100.02, 5.0, 100.04, 5.0),      # inside stop/target, young
        _book(T0 + 11_000, 100.02, 5.0, 100.04, 5.0),     # > hold_ms after entry
    ]
    ftrades = [_ft(T0 + 600, 99.99, 5.0, "sell")]
    result = _run(leader, books, ftrades)
    row = result.rows[0]
    assert row.taker_exit_reason == "timeout"
    assert row.maker_exit_reason == "timeout"


def test_end_of_tape_closes_open_legs():
    leader = _up_impulse_leader()
    books = [
        _book(T0 + 400, 99.99, 5.0, 100.01, 5.0),
        _book(T0 + 800, 100.02, 5.0, 100.04, 5.0),        # neither stop nor target
    ]
    ftrades = [_ft(T0 + 600, 99.99, 5.0, "sell")]
    result = _run(leader, books, ftrades)
    assert result.unresolved_at_end == 1
    row = result.rows[0]
    assert row.taker_exit_reason == "end"
    assert row.maker_exit_reason == "end"


# --- Queue-aware follower maker fill -----------------------------------------------


def test_maker_fills_only_after_displayed_size_ahead_clears():
    leader = _up_impulse_leader()
    books = [
        _book(T0 + 400, 99.99, 5.0, 100.01, 5.0),         # queue ahead = 5.0
        _book(T0 + 1_500, 100.08, 5.0, 100.10, 5.0),      # echo up (exit)
    ]
    ftrades = [
        _ft(T0 + 600, 99.99, 2.0, "sell"),                # consumes 2.0 (< 5.0)
        _ft(T0 + 700, 99.99, 3.5, "sell"),                # 5.5 >= 5.0 -> fills
    ]
    result = _run(leader, books, ftrades)
    row = result.rows[0]
    assert row.maker_filled is True
    assert row.maker_fill_lag_ms == 200                   # filled at T0+700


def test_maker_does_not_fill_without_trade_through():
    leader = _up_impulse_leader()
    books = [
        _book(T0 + 400, 99.99, 5.0, 100.01, 5.0),
        _book(T0 + 800, 100.08, 5.0, 100.10, 5.0),
    ]
    # sell trades ABOVE the bid never reach our resting order -> no fill
    ftrades = [_ft(T0 + 600, 100.00, 9.0, "sell")]
    result = _run(leader, books, ftrades)
    row = result.rows[0]
    assert row.maker_filled is False
    assert row.maker_net_bps is None
    assert result.maker_missed == 1
    assert row.taker_exit_reason == "target"              # taker leg still resolves


def test_maker_misses_when_queue_never_clears_before_ttl():
    leader = _up_impulse_leader()
    books = [
        _book(T0 + 400, 99.99, 5.0, 100.01, 5.0),
        _book(T0 + 3_000, 100.08, 5.0, 100.10, 5.0),      # > maker_ttl_ms later
    ]
    ftrades = [_ft(T0 + 600, 99.99, 1.0, "sell")]         # only 1.0 of the 5.0 queue
    result = _run(leader, books, ftrades)
    row = result.rows[0]
    assert row.maker_filled is False
    assert result.maker_missed == 1


def test_no_follower_book_at_impulse_is_skipped():
    leader = _up_impulse_leader()
    books = [_book(T0 + 700, 99.99, 5.0, 100.01, 5.0)]    # book only AFTER impulse
    result = _run(leader, books, [])
    assert result.impulses_detected == 1
    assert result.skipped_no_follower_book == 1
    assert result.scalps_opened == 0
    assert result.rows == []


def test_overlapping_impulse_is_suppressed():
    leader = [
        _lt(T0, 100.0), _lt(T0 + 500, 100.10),            # impulse 1 at T0+500
        _lt(T0 + 4_000, 100.0), _lt(T0 + 4_500, 100.10),  # impulse 2 while 1 open
    ]
    books = [_book(T0 + 400, 99.99, 5.0, 100.01, 5.0)]    # never exits -> stays open
    result = _run(leader, books, [])
    assert result.impulses_detected == 2
    assert result.scalps_opened == 1
    assert result.overlapping_impulses == 1


# --- Cost models -------------------------------------------------------------------


def test_taker_taker_cost_math():
    model = cost_models_for("delta_india")["taker_taker"]
    assert model.round_trip_fee_bps == pytest.approx(10.0)   # 5 + 5 taker
    assert not model.assumed_queue_fill
    assert model.net_bps("buy", 100.0, 100.0) == pytest.approx(-10.0)
    assert model.net_bps("sell", 100.0, 100.0) == pytest.approx(-10.0)
    # a 20 bps favorable move nets 10 after the 10 bps round-trip fee
    assert model.net_bps("buy", 100.0, 100.2) == pytest.approx(
        (0.2 / 100.0 * 10_000.0) - 10.0)


def test_maker_first_cost_math_and_flag():
    model = cost_models_for("delta_india")["maker_first"]
    assert model.round_trip_fee_bps == pytest.approx(7.0)    # 2 maker + 5 taker
    assert model.assumed_queue_fill
    assert "ASSUMED_QUEUE_FILL" in model.to_dict()["caveat"]
    assert model.net_bps("buy", 100.0, 100.0) == pytest.approx(-7.0)
    # maker is 3 bps cheaper round-trip than taker on the same prices
    taker = cost_models_for("delta_india")["taker_taker"]
    assert model.net_bps("buy", 100.0, 100.1) - taker.net_bps("buy", 100.0, 100.1) \
        == pytest.approx(3.0)
    with pytest.raises(ValueError):
        model.net_bps("hold", 100.0, 100.0)


def test_hist_exchange_maps_to_live_fee_profile():
    assert cost_models_for("binanceusdm_hist") == cost_models_for("binanceusdm")


# --- Params ------------------------------------------------------------------------


def test_params_validation():
    with pytest.raises(ValueError):
        EchoScalpParams(impulse_threshold_bps=0.0)
    with pytest.raises(ValueError):
        EchoScalpParams(maker_ttl_ms=0)
    with pytest.raises(ValueError):
        EchoScalpParams(target_bps=-1.0)
    with pytest.raises(ValueError):
        EchoScalpParams(notional_usd=0.0)


def test_params_from_env(monkeypatch):
    monkeypatch.setenv("ECHO_IMPULSE_THRESHOLD_BPS", "12.5")
    monkeypatch.setenv("ECHO_MAKER_TTL_MS", "500")
    monkeypatch.setenv("ECHO_TARGET_BPS", "not-a-number")   # ignored
    params = EchoScalpParams.from_env()
    assert params.impulse_threshold_bps == 12.5
    assert params.maker_ttl_ms == 500
    assert params.target_bps == EchoScalpParams().target_bps


# --- Verdicts ----------------------------------------------------------------------


def test_verdict_vocabulary():
    assert echo_verdict(5, 10.0, 10.0, 20) == "UNDER_SAMPLED"
    assert echo_verdict(0, 0.0, 0.0, 20) == "UNDER_SAMPLED"
    assert echo_verdict(25, 3.0, -1.0, 20) == "CANDIDATE"        # taker positive
    assert echo_verdict(25, -1.0, 2.0, 20) == "MAKER_ONLY_POSITIVE"
    assert echo_verdict(25, -1.0, -0.5, 20) == "NEGATIVE_EDGE"
    assert echo_verdict(20, 0.0, 0.0, 20) == "NEGATIVE_EDGE"     # flat is not edge


def test_policy_guards():
    policy = leadlag_echo_scalp_policy()
    assert policy["can_trade"] is False
    assert policy["can_promote"] is False
    assert policy["requires_untouched_judgment"] is True
    assert policy["requires_human_approval"] is True
    assert policy["family"] == "cross_venue_leadlag_echo_scalp"


# --- Dual-venue day loading + scanner ----------------------------------------------


def _leader_trade_df(trades):
    return pd.DataFrame([{
        "ts_ms": t.ts_ms, "price": t.price, "amount": t.amount, "side": "buy",
    } for t in trades])


def _follower_trade_df(trades):
    return pd.DataFrame([{
        "ts_ms": t.ts_ms, "price": t.price, "amount": t.amount, "side": t.taker_side,
    } for t in trades])


def _l2_book_df(books, levels=10):
    """Build a recorded L2 book frame from (ts_ms, OrderBookL2) tuples; only
    level 0 is populated, deeper levels are NaN (padded empties)."""
    rows = []
    for ts_ms, bk in books:
        row = {"ts_ms": ts_ms}
        for i in range(levels):
            row[f"bid_px_{i}"] = bk.bids[0][0] if i == 0 else np.nan
            row[f"bid_qty_{i}"] = bk.bids[0][1] if i == 0 else np.nan
            row[f"ask_px_{i}"] = bk.asks[0][0] if i == 0 else np.nan
            row[f"ask_qty_{i}"] = bk.asks[0][1] if i == 0 else np.nan
        rows.append(row)
    return pd.DataFrame(rows)


def _write_shard(root, exchange, symbol_safe, stream, day, df, name="0001.parquet"):
    d = (root / "ticks" / f"exchange={exchange}" / f"symbol={symbol_safe}"
         / f"stream={stream}" / day)
    d.mkdir(parents=True)
    df.to_parquet(d / name, index=False)


def _seed_day(root, day):
    """Leader trades (binance BTCUSDT) + follower L2 book & trades (delta BTCUSD)."""
    leader = _up_impulse_leader()
    _write_shard(root, "binanceusdm", "BTCUSDT", "trades", day,
                 _leader_trade_df(leader))
    books = [
        _book(T0 + 400, 99.99, 5.0, 100.01, 5.0),
        _book(T0 + 800, 100.08, 5.0, 100.10, 5.0),
    ]
    _write_shard(root, "delta_india", "BTCUSD", "book", day, _l2_book_df(books))
    _write_shard(root, "delta_india", "BTCUSD", "trades", day,
                 _follower_trade_df([_ft(T0 + 600, 99.99, 5.0, "sell")]))


def test_loaders_round_trip(tmp_path):
    _seed_day(tmp_path, "20260709")
    leader, source = load_leader_trades(tmp_path, PAIR, "20260709")
    assert source == "binanceusdm"
    assert len(leader) == 2 and all(t.price > 0 for t in leader)
    books = load_follower_books(tmp_path, PAIR, "20260709")
    assert len(books) == 2
    assert books[0][1].best_bid == pytest.approx(99.99)
    ftrades = load_follower_trades(tmp_path, PAIR, "20260709")
    assert len(ftrades) == 1 and ftrades[0].taker_side == "sell"


def test_leader_trade_hist_fallback(tmp_path):
    # only the binanceusdm_hist archive has the leader tape for this day
    leader = _up_impulse_leader()
    _write_shard(tmp_path, "binanceusdm_hist", "BTCUSDT", "trades", "20260709",
                 _leader_trade_df(leader))
    books = [_book(T0 + 400, 99.99, 5.0, 100.01, 5.0)]
    _write_shard(tmp_path, "delta_india", "BTCUSD", "book", "20260709",
                 _l2_book_df(books))
    got, source = load_leader_trades(tmp_path, PAIR, "20260709")
    assert source == "binanceusdm_hist"
    assert len(got) == 2
    assert discover_overlap_days(tmp_path, PAIR) == ("20260709",)


def test_scanner_full_run_and_missing_follower_day(tmp_path):
    _seed_day(tmp_path, "20260709")
    # day with a leader tape + follower BOOK but NO follower trade tape
    _write_shard(tmp_path, "binanceusdm", "BTCUSDT", "trades", "20260710",
                 _leader_trade_df(_up_impulse_leader()))
    _write_shard(tmp_path, "delta_india", "BTCUSD", "book", "20260710",
                 _l2_book_df([_book(T0 + 400, 99.99, 5.0, 100.01, 5.0),
                              _book(T0 + 800, 100.08, 5.0, 100.10, 5.0)]))
    payload = run_leadlag_echo_scalp(tmp_path, pairs=(PAIR,), params=P)
    target = payload["targets"][0]
    assert target["overlap_days"] == ["20260709", "20260710"]
    assert target["days_scanned"] == ["20260709", "20260710"]
    assert target["days_missing_follower_trades"] == ["20260710"]
    # day 1 maker fills (queue cleared); day 2 has no follower trades -> maker miss
    assert target["events"] == 2
    assert target["maker_fills"] == 1
    assert target["verdict"] == "UNDER_SAMPLED"          # 2 events < 20
    assert target["can_trade"] is False and target["can_promote"] is False
    assert target["aggregates"]["taker_taker"]["events"] == 2
    assert target["aggregates"]["maker_first"]["events"] == 1   # only filled makers
    assert "ASSUMED_QUEUE_FILL" in payload["cost_models"]["maker_first"]["caveat"]
    assert render_report(payload)


def test_scanner_absent_follower_is_graceful(tmp_path):
    # leader tape exists but no delta follower book at all -> no overlap days
    _write_shard(tmp_path, "binanceusdm", "BTCUSDT", "trades", "20260709",
                 _leader_trade_df(_up_impulse_leader()))
    payload = run_leadlag_echo_scalp(tmp_path, pairs=(PAIR,), params=P)
    target = payload["targets"][0]
    assert target["overlap_days"] == []
    assert target["events"] == 0
    assert target["verdict"] == "UNDER_SAMPLED"
    assert payload["summary"]["events"] == 0
    assert render_report(payload)


def test_scanner_empty_root(tmp_path):
    payload = run_leadlag_echo_scalp(tmp_path, pairs=DEFAULT_PAIRS, params=P)
    assert {t["verdict"] for t in payload["targets"]} == {"UNDER_SAMPLED"}
    assert payload["summary"]["events"] == 0
    empty = run_leadlag_echo_scalp(tmp_path, pairs=(), params=P)
    assert "no leader/follower overlap days" in render_report(empty)


# --- Publish + folding hook --------------------------------------------------------


def test_folding_hook_into_continuous_research(tmp_path, monkeypatch):
    out_dir = tmp_path / "live_research"
    monkeypatch.setattr(cr, "OUT_DIR", out_dir)
    assert cr._load_leadlag_echo_scalp_latest() == {}         # absent -> {}

    payload = {"policy": leadlag_echo_scalp_policy(), "targets": [],
               "summary": {"events": 0}}
    path = write_leadlag_echo_scalp_payload(payload, out_dir)
    assert path.name == LEADLAG_ECHO_SCALP_LATEST
    assert not list(out_dir.glob("*.tmp"))                    # atomic publish
    assert cr._load_leadlag_echo_scalp_latest() == payload

    cr.publish(cr.ResearchPayload(
        started=0.0, leadlag_echo_scalp=cr._load_leadlag_echo_scalp_latest()))
    latest = json.loads((out_dir / "latest.json").read_text())
    assert latest["leadlag_echo_scalp"]["policy"]["can_trade"] is False
    assert latest["leadlag_echo_scalp"]["policy"]["family"] == \
        "cross_venue_leadlag_echo_scalp"

    (out_dir / LEADLAG_ECHO_SCALP_LATEST).write_text("{corrupt")
    assert cr._load_leadlag_echo_scalp_latest() == {}         # unreadable -> {}
