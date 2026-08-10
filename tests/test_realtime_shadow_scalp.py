"""Real-time shadow scalp — detector reuse regression (live == batch), live-tick
firing, virtual resolution (stop-wins-ties, queue-aware maker, timeout),
restart resume without double-resolve, aggregation/folding, and the hard
no-order/no-gateway invariant."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

import vnedge.research.continuous_research as cr
import vnedge.runtime.realtime_shadow_scalp as m
from vnedge.execution.journal import DecisionJournal
from vnedge.research.cascade_reversion import (
    CascadeParams,
    CascadeReversionReplayer,
    LiquidationEvent,
    TradePrint,
)
from vnedge.research.cascade_reversion import cost_models_for as cascade_models
from vnedge.research.leadlag_echo_scalp import (
    EchoScalpParams,
    EchoScalpReplayer,
    FollowerTrade,
    LeaderTrade,
    LeadLagPair,
)
from vnedge.research.leadlag_echo_scalp import cost_models_for as echo_models
from vnedge.research.shadow_perf_reader import read_shadow_perf
from vnedge.scalping.depth import OrderBookL2

EX = "binanceusdm"
SYM = "BTC/USDT:USDT"
T0 = 1_752_000_000_000
C0 = T0 + 300_000

CP = CascadeParams(
    burst_window_ms=10_000, trailing_window_ms=600_000, threshold_pct=0.95,
    min_history_events=10, min_burst_notional_usd=100.0, one_sided_min=0.80,
    exhaustion_peak_frac=0.25, exhaustion_quiet_ms=5_000, pre_vwap_window_ms=60_000,
    stop_buffer_frac=0.10, timeout_ms=60_000, min_events_for_candidate=20,
)

EP = EchoScalpParams(
    impulse_window_ms=1_000, impulse_threshold_bps=8.0, impulse_cooldown_ms=3_000,
    response_threshold_bps=3.0, max_lag_ms=5_000, maker_ttl_ms=2_000,
    target_bps=6.0, stop_bps=6.0, hold_ms=10_000, notional_usd=100.0,
    min_events_for_candidate=20,
)

PAIR = LeadLagPair("BTC", "binanceusdm", "BTC/USDT:USDT", "delta_india", "BTC/USD:USD")


# --- synthetic tape builders (mirror the batch-family tests) -----------------------

def _liq(ts_ms, price, notional, side):
    return LiquidationEvent(ts_ms=ts_ms, price=price, amount=notional / price,
                            side=side, notional_usd=notional)


def _tr(ts_ms, price, qty=1.0):
    return TradePrint(ts_ms=ts_ms, price=price, amount=qty)


def _warmup_liqs():
    return [_liq(C0 - 240_000 + i * 20_000, 100.0, 10.0, "sell" if i % 2 else "buy")
            for i in range(12)]


def _sell_burst():
    return [_liq(C0 + 1_000, 99.5, 100.0, "sell"),
            _liq(C0 + 2_000, 99.0, 100.0, "sell"),
            _liq(C0 + 3_000, 98.5, 100.0, "sell")]


def _pre_trades():
    return [_tr(C0 - 59_000 + i * 1_000, 100.0) for i in range(60)]


def _cascade_trades():
    return [_tr(C0 + 1_500, 99.4), _tr(C0 + 2_500, 99.0), _tr(C0 + 3_500, 98.6),
            _tr(C0 + 5_000, 98.7), _tr(C0 + 8_500, 98.8)]


def _lt(ts_ms, price, amount=1.0):
    return LeaderTrade(ts_ms=ts_ms, price=price, amount=amount)


def _ft(ts_ms, price, amount, side):
    return FollowerTrade(ts_ms=ts_ms, price=price, amount=amount, taker_side=side)


def _book(ts_ms, bid, bid_qty, ask, ask_qty):
    return (ts_ms, OrderBookL2(
        symbol="BTC/USD:USD", bids=((bid, bid_qty),), asks=((ask, ask_qty),),
        event_time=datetime.fromtimestamp(ts_ms / 1000, tz=UTC)))


# --- drivers: feed ticks in the batch merge order so live == batch is exact --------

def _drive_cascade(lane, liqs, trades):
    merged = [(e.ts_ms, 0, e) for e in liqs] + [(t.ts_ms, 1, t) for t in trades]
    merged.sort(key=lambda x: (x[0], x[1]))
    for _ts, kind, obj in merged:
        (lane.on_liquidation if kind == 0 else lane.on_trade)(obj)


def _drive_echo(lane, leader, books, ftrades):
    merged = ([(ts, 0, bk) for ts, bk in books]
              + [(t.ts_ms, 1, t) for t in ftrades]
              + [(t.ts_ms, 2, t) for t in leader])
    merged.sort(key=lambda x: (x[0], x[1]))
    for _ts, kind, obj in merged:
        if kind == 0:
            lane.on_follower_book(obj)
        elif kind == 1:
            lane.on_follower_trade(obj)
        else:
            lane.on_leader_trade(obj)


def _records(path: Path, kind: str) -> list[dict]:
    out = []
    for line in path.read_text().splitlines():
        rec = json.loads(line)
        if rec["kind"] == kind:
            out.append(rec["payload"])
    return out


def _cascade_lane(tmp_path, name="c"):
    return m.CascadeShadowLane(
        venue=EX, symbol=SYM,
        journal=DecisionJournal(tmp_path / f"{name}.journal.jsonl"),
        params=CP, notional_usd=100.0)


def _echo_lane(tmp_path, name="e"):
    return m.EchoShadowLane(
        pair=PAIR, journal=DecisionJournal(tmp_path / f"{name}.journal.jsonl"),
        params=EP)


# --- policy / invariants -----------------------------------------------------------

def test_policy_guards():
    policy = m.realtime_shadow_scalp_policy()
    assert policy["can_trade"] is False
    assert policy["can_promote"] is False
    assert policy["requires_untouched_judgment"] is True
    assert policy["requires_human_approval"] is True
    assert policy["requires_replay_before_promotion"] is True


def test_no_order_or_gateway_symbols_imported():
    """The hard line: a shadow runner must not reach an order or gateway path."""
    src = Path(m.__file__).read_text()
    forbidden = [
        "PreTradeRiskGateway", "order_manager", "OrderManager",
        "ExecutionAdapter", "live_execution", "simulated_exchange",
        ".submit(", "place_order", "create_order", "risk_manager",
    ]
    for token in forbidden:
        assert token not in src, f"forbidden order/gateway symbol present: {token}"
    # nothing the module imports is an execution/gateway module
    import vnedge.runtime.realtime_shadow_scalp as mod
    imported = set(vars(mod))
    for banned in ("OrderManager", "PreTradeRiskGateway", "SimulatedExchange",
                   "LiveExecutionAdapter"):
        assert banned not in imported


def test_every_payload_declares_no_trade_no_promote(tmp_path):
    runner = m.RealtimeShadowScalpRunner(
        cascade_targets=((EX, SYM),), echo_pairs=(PAIR,),
        journal_dir=tmp_path / "j", out_dir=tmp_path / "o",
        cascade_params=CP, echo_params=EP)
    payload = runner.build_payload()
    assert payload["can_trade"] is False and payload["can_promote"] is False
    assert payload["summary"]["can_trade"] is False
    assert payload["summary"]["can_promote"] is False
    for lane in payload["lanes"]:
        assert lane["can_trade"] is False and lane["can_promote"] is False


# --- detector reuse regression: live lane == batch replayer ------------------------

def test_cascade_live_equals_batch(tmp_path):
    liqs = _warmup_liqs() + _sell_burst()
    trades = _pre_trades() + _cascade_trades() + [_tr(C0 + 20_000, 100.2)]

    batch = CascadeReversionReplayer(CP, cascade_models(EX)).run(
        liqs, trades, exchange=EX, symbol=SYM, day="20260708")
    assert len(batch.rows) == 1
    brow = batch.rows[0]

    lane = _cascade_lane(tmp_path)
    _drive_cascade(lane, liqs, trades)
    outs = _records(lane.journal.path, "scalp_shadow_outcome")
    assert len(outs) == 1
    o = outs[0]
    assert o["resolution"] == brow.exit_reason
    assert o["taker_net_bps"] == pytest.approx(brow.taker_net_bps, abs=1e-3)
    assert o["maker_net_bps"] == pytest.approx(brow.maker_first_net_bps, abs=1e-3)
    # internal accumulator keeps full precision (journal rounds for storage only)
    assert lane._taker_bps[0] == pytest.approx(brow.taker_net_bps)
    assert lane._maker_bps[0] == pytest.approx(brow.maker_first_net_bps)


def test_echo_live_equals_batch(tmp_path):
    leader = [_lt(T0, 100.0), _lt(T0 + 500, 100.10)]
    books = [_book(T0, 99.99, 50, 100.01, 50), _book(T0 + 500, 99.99, 10, 100.01, 10),
             _book(T0 + 1500, 100.06, 50, 100.08, 50),
             _book(T0 + 3000, 100.10, 50, 100.12, 50)]
    ftrades = [_ft(T0 + 800, 99.99, 20, "sell")]

    batch = EchoScalpReplayer(EP, echo_models(PAIR.follower_exchange)).run(
        leader, books, ftrades, base="BTC", leader_exchange="binanceusdm",
        follower_exchange="delta_india", follower_symbol="BTC/USD:USD",
        day="20260709")
    assert len(batch.rows) == 1
    brow = batch.rows[0]

    lane = _echo_lane(tmp_path)
    _drive_echo(lane, leader, books, ftrades)
    outs = _records(lane.journal.path, "scalp_shadow_outcome")
    assert len(outs) == 1
    o = outs[0]
    assert o["resolution"] == brow.taker_exit_reason
    assert o["maker_filled"] == brow.maker_filled
    assert o["taker_net_bps"] == pytest.approx(brow.taker_net_bps, abs=1e-3)
    if brow.maker_net_bps is not None:
        assert o["maker_net_bps"] == pytest.approx(brow.maker_net_bps, abs=1e-3)


# --- live-tick firing (both families) ----------------------------------------------

def test_cascade_fires_intent_on_live_ticks(tmp_path):
    lane = _cascade_lane(tmp_path)
    liqs = _warmup_liqs() + _sell_burst()
    trades = _pre_trades() + _cascade_trades()  # ends on the entry print, no exit
    _drive_cascade(lane, liqs, trades)
    intents = _records(lane.journal.path, "scalp_shadow_intent")
    assert len(intents) == 1
    intent = intents[0]
    assert intent["family"] == "cascade"
    assert intent["approved"] is False
    assert intent["assumed_maker_fill"] is True
    assert "ASSUMED_MAKER_FILL" in intent["maker_fill_caveat"]
    assert intent["intent"]["strategy_id"] == "cascade_reversion_v1"
    assert intent["intent"]["side"] == "long"      # buy against a sell cascade
    assert "taker_taker" in intent["expected_edge_bps"]
    assert "maker_first" in intent["expected_edge_bps"]
    assert lane.open_count() == 1                   # open, not yet resolved


def test_echo_fires_intent_on_live_ticks(tmp_path):
    lane = _echo_lane(tmp_path)
    lane.on_follower_book(_book(T0, 99.99, 50, 100.01, 50)[1])
    lane.on_leader_trade(_lt(T0, 100.0))
    lane.on_leader_trade(_lt(T0 + 500, 100.10))     # +10bps impulse -> fires
    intents = _records(lane.journal.path, "scalp_shadow_intent")
    assert len(intents) == 1
    intent = intents[0]
    assert intent["family"] == "leadlag_echo"
    assert intent["assumed_queue_fill"] is True
    assert "ASSUMED_QUEUE_FILL" in intent["maker_fill_caveat"]
    assert intent["intent"]["strategy_id"] == "leadlag_echo_scalp_v1"
    assert intent["intent"]["side"] == "long"
    assert lane.open_count() == 1


def test_echo_no_book_no_fire(tmp_path):
    lane = _echo_lane(tmp_path)
    lane.on_leader_trade(_lt(T0, 100.0))
    lane.on_leader_trade(_lt(T0 + 500, 100.10))     # impulse but no follower book
    assert _records(lane.journal.path, "scalp_shadow_intent") == []
    assert lane.open_count() == 0


# --- virtual resolution: target / stop-wins-ties / timeout / queue-aware maker -----

def test_cascade_target_resolution_and_maker_beats_taker(tmp_path):
    lane = _cascade_lane(tmp_path)
    liqs = _warmup_liqs() + _sell_burst()
    trades = _pre_trades() + _cascade_trades() + [_tr(C0 + 20_000, 100.2)]
    _drive_cascade(lane, liqs, trades)
    outs = _records(lane.journal.path, "scalp_shadow_outcome")
    assert len(outs) == 1
    assert outs[0]["resolution"] == "target"
    # cascade maker_first drops the entry slippage + halves the entry fee, so on
    # the SAME fill path it always beats taker
    assert outs[0]["maker_net_bps"] > outs[0]["taker_net_bps"]
    assert lane.summary()["maker_beats_taker"] is True


def test_cascade_stop_wins_tie(tmp_path):
    lane = _cascade_lane(tmp_path)
    liqs = _warmup_liqs() + _sell_burst()
    # exit print lands exactly on the stop (98.35) — stop must win the tie
    trades = _pre_trades() + _cascade_trades() + [_tr(C0 + 20_000, 98.35)]
    _drive_cascade(lane, liqs, trades)
    outs = _records(lane.journal.path, "scalp_shadow_outcome")
    assert len(outs) == 1
    assert outs[0]["resolution"] == "stop"
    assert outs[0]["taker_net_bps"] < 0


def test_cascade_timeout_resolution(tmp_path):
    lane = _cascade_lane(tmp_path)
    liqs = _warmup_liqs() + _sell_burst()
    trades = _pre_trades() + _cascade_trades() + [
        _tr(C0 + 30_000, 99.0),      # inside stop/target, young
        _tr(C0 + 68_600, 99.0),      # 60.1s after entry -> timeout
    ]
    _drive_cascade(lane, liqs, trades)
    outs = _records(lane.journal.path, "scalp_shadow_outcome")
    assert len(outs) == 1
    assert outs[0]["resolution"] == "timeout"


def test_echo_queue_aware_maker_fill(tmp_path):
    """The maker leg fills only once follower taker volume clears the queue
    displayed ahead of it — the reused FIFO model, live."""
    lane = _echo_lane(tmp_path)
    leader = [_lt(T0, 100.0), _lt(T0 + 500, 100.10)]
    books = [_book(T0, 99.99, 50, 100.01, 50), _book(T0 + 500, 99.99, 10, 100.01, 10),
             _book(T0 + 1500, 100.06, 50, 100.08, 50),
             _book(T0 + 3000, 100.10, 50, 100.12, 50)]
    ftrades = [_ft(T0 + 800, 99.99, 20, "sell")]  # 20 clears the 10 resting ahead
    _drive_echo(lane, leader, books, ftrades)
    outs = _records(lane.journal.path, "scalp_shadow_outcome")
    assert len(outs) == 1
    assert outs[0]["maker_filled"] is True
    assert outs[0]["maker_net_bps"] is not None
    assert outs[0]["maker_fill_lag_ms"] is not None


def test_echo_maker_misses_when_queue_never_clears(tmp_path):
    lane = _echo_lane(tmp_path)
    leader = [_lt(T0, 100.0), _lt(T0 + 500, 100.10)]
    # deep queue (1000) + no follower trades -> maker never fills, TTL expires
    books = [_book(T0, 99.99, 1000, 100.01, 1000),
             _book(T0 + 500, 99.99, 1000, 100.01, 1000),
             _book(T0 + 3000, 100.10, 50, 100.12, 50),   # TTL (2s) elapsed
             _book(T0 + 12000, 100.10, 50, 100.12, 50)]  # taker hold timeout
    _drive_echo(lane, leader, books, [])
    outs = _records(lane.journal.path, "scalp_shadow_outcome")
    assert len(outs) == 1
    assert outs[0]["maker_filled"] is False
    assert outs[0]["maker_net_bps"] is None          # no maker evidence -> None
    assert outs[0]["taker_net_bps"] is not None       # taker floor always resolves


# --- restart resume without double-resolve -----------------------------------------

def test_restart_rebuilds_open_and_resolves_once(tmp_path):
    jpath = tmp_path / "c.journal.jsonl"
    liqs = _warmup_liqs() + _sell_burst()

    # session 1: enter, no exit trade yet -> intent only, still open
    lane1 = m.CascadeShadowLane(venue=EX, symbol=SYM,
                                journal=DecisionJournal(jpath), params=CP)
    _drive_cascade(lane1, liqs, _pre_trades() + _cascade_trades())
    assert lane1.open_count() == 1
    assert len(_records(jpath, "scalp_shadow_intent")) == 1
    assert _records(jpath, "scalp_shadow_outcome") == []

    # session 2 (restart): rebuild the open scalp, feed the exit -> one outcome
    lane2 = m.CascadeShadowLane(venue=EX, symbol=SYM,
                                journal=DecisionJournal(jpath), params=CP)
    assert lane2.open_count() == 1                    # rebuilt from the journal
    assert lane2.summary()["intents"] == 1            # intent count seeded
    lane2.on_trade(_tr(C0 + 20_000, 100.2))           # target
    assert lane2.open_count() == 0
    assert len(_records(jpath, "scalp_shadow_intent")) == 1
    assert len(_records(jpath, "scalp_shadow_outcome")) == 1

    # session 3 (restart): resolved key is skipped -> more ticks never re-resolve
    lane3 = m.CascadeShadowLane(venue=EX, symbol=SYM,
                                journal=DecisionJournal(jpath), params=CP)
    assert lane3.open_count() == 0
    assert lane3.summary()["virtual_trades"] == 1     # aggregate carried forward
    lane3.on_trade(_tr(C0 + 25_000, 101.0))
    lane3.on_trade(_tr(C0 + 30_000, 99.0))
    assert len(_records(jpath, "scalp_shadow_outcome")) == 1  # no double-resolve


def test_echo_restart_no_double_resolve(tmp_path):
    jpath = tmp_path / "e.journal.jsonl"
    # session 1: open a scalp, do not resolve (deep queue, no exit book yet)
    lane1 = m.EchoShadowLane(pair=PAIR, journal=DecisionJournal(jpath), params=EP)
    lane1.on_follower_book(_book(T0, 99.99, 1000, 100.01, 1000)[1])
    lane1.on_leader_trade(_lt(T0, 100.0))
    lane1.on_leader_trade(_lt(T0 + 500, 100.10))
    assert lane1.open_count() == 1
    assert len(_records(jpath, "scalp_shadow_intent")) == 1

    # session 2 (restart): rebuild + resolve exactly once
    lane2 = m.EchoShadowLane(pair=PAIR, journal=DecisionJournal(jpath), params=EP)
    assert lane2.open_count() == 1
    lane2.on_follower_book(_book(T0 + 3000, 100.10, 50, 100.12, 50)[1])   # TTL gone
    lane2.on_follower_book(_book(T0 + 12000, 100.10, 50, 100.12, 50)[1])  # timeout
    assert lane2.open_count() == 0
    assert len(_records(jpath, "scalp_shadow_outcome")) == 1

    # session 3 (restart): resolved -> never re-resolves on further ticks
    lane3 = m.EchoShadowLane(pair=PAIR, journal=DecisionJournal(jpath), params=EP)
    assert lane3.open_count() == 0
    lane3.on_follower_book(_book(T0 + 20000, 100.20, 50, 100.22, 50)[1])
    assert len(_records(jpath, "scalp_shadow_outcome")) == 1


# --- aggregation, publish, folding, shadow-perf ------------------------------------

def test_runner_publish_and_aggregate(tmp_path):
    runner = m.RealtimeShadowScalpRunner(
        cascade_targets=((EX, SYM),), echo_pairs=(),
        journal_dir=tmp_path / "logs" / "scalp_shadow", out_dir=tmp_path / "research",
        cascade_params=CP)
    lane = runner.cascade_lanes[(EX, SYM)]
    _drive_cascade(lane, _warmup_liqs() + _sell_burst(),
                   _pre_trades() + _cascade_trades() + [_tr(C0 + 20_000, 100.2)])
    path = runner.publish()
    assert path.name == m.REALTIME_SHADOW_SCALP_LATEST
    assert not list((tmp_path / "research").glob("*.tmp"))   # atomic publish
    payload = json.loads(path.read_text())
    assert payload["runner_id"] == "realtime_shadow_scalp_v1"
    assert payload["summary"]["virtual_trades"] == 1
    assert payload["lanes"][0]["aggregates"]["taker_taker"]["events"] == 1
    assert m.render_report(payload)                          # never crashes


def test_folds_into_continuous_research(tmp_path, monkeypatch):
    out_dir = tmp_path / "live_research"
    monkeypatch.setattr(cr, "OUT_DIR", out_dir)
    assert cr._load_realtime_shadow_scalp_latest() == {}     # absent -> {}

    runner = m.RealtimeShadowScalpRunner(
        cascade_targets=((EX, SYM),), journal_dir=tmp_path / "j", out_dir=out_dir,
        cascade_params=CP)
    _drive_cascade(runner.cascade_lanes[(EX, SYM)], _warmup_liqs() + _sell_burst(),
                   _pre_trades() + _cascade_trades() + [_tr(C0 + 20_000, 100.2)])
    runner.publish()

    cr.publish(cr.ResearchPayload(
        started=0.0, realtime_shadow_scalp=cr._load_realtime_shadow_scalp_latest()))
    latest = json.loads((out_dir / "latest.json").read_text())
    assert latest["realtime_shadow_scalp"]["runner_id"] == "realtime_shadow_scalp_v1"
    assert latest["realtime_shadow_scalp"]["policy"]["can_trade"] is False

    (out_dir / m.REALTIME_SHADOW_SCALP_LATEST).write_text("{corrupt")
    assert cr._load_realtime_shadow_scalp_latest() == {}     # unreadable -> {}


def test_shadow_perf_reader_folds_scalp_journals(tmp_path):
    scalp_dir = tmp_path / "scalp_shadow"
    scalp_dir.mkdir()
    lane = m.CascadeShadowLane(
        venue=EX, symbol=SYM,
        journal=DecisionJournal(scalp_dir / f"cascade_{EX}_BTCUSDT.journal.jsonl"),
        params=CP, notional_usd=100.0)
    _drive_cascade(lane, _warmup_liqs() + _sell_burst(),
                   _pre_trades() + _cascade_trades() + [_tr(C0 + 20_000, 100.2)])

    # opt-in scalp dir; the primary dir need not exist
    perf = read_shadow_perf(tmp_path / "absent", scalp_journal_dir=scalp_dir)
    assert perf["available"] is True
    assert len(perf["lanes"]) == 1
    row = perf["lanes"][0]
    assert row["strategy"] == "cascade_reversion_v1"
    assert row["exchange"] == "binanceusdm"          # resolved from filename
    assert row["virtual_trades"] == 1
    assert row["net_usd"] == pytest.approx(lane.summary()["aggregates"]
                                           ["taker_taker"]["net_usd"], abs=1e-3)


def test_shadow_perf_reader_default_ignores_scalp_dir(tmp_path):
    # without the opt-in, behavior is unchanged (existing callers unaffected)
    perf = read_shadow_perf(tmp_path)
    assert perf["available"] is False


# --- follower book conversion ------------------------------------------------------

def test_delta_book_from_arrays():
    buy = [{"limit_price": "100.0", "size": 5}, {"limit_price": "99.9", "size": 3}]
    sell = [{"limit_price": "100.1", "size": 4}]
    book = m.delta_book_from_arrays("BTC/USD:USD", buy, sell, T0)
    assert book is not None
    assert book.best_bid == pytest.approx(100.0)
    assert book.best_ask == pytest.approx(100.1)
    # crossed / empty / malformed all degrade to None (last good book stays)
    assert m.delta_book_from_arrays("X", [], sell, T0) is None
    crossed = [{"limit_price": "100.2", "size": 1}]
    assert m.delta_book_from_arrays("X", crossed, sell, T0) is None
    assert m.delta_book_from_arrays("X", [{"nope": 1}], sell, T0) is None
