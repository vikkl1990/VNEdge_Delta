"""Taker-only event replay — features, cost math, deterministic trades, folding."""

import json
from dataclasses import asdict
from datetime import UTC, datetime

import pandas as pd
import pytest

import vnedge.research.continuous_research as cr
from vnedge.data.aggtrades_backfill import HIST_EXCHANGE_ID, write_trade_shard
from vnedge.research.event_taker_replay import (
    EVENT_TAKER_LATEST,
    ForcedFlowBurstScalper,
    TakerFees,
    TakerReplayBacktester,
    TakerSignal,
    TradeFlowEngine,
    VolatilityImpulseScalper,
    build_row,
    event_taker_policy,
    run_event_taker_replay,
    write_event_taker_payload,
)
from vnedge.scalping.microstructure import TradeTick

SYM = "BTC/USDT:USDT"
T0 = 1_751_000_000_000  # 2025-06-27 UTC, exact second boundary
DAY = pd.to_datetime(T0, unit="ms", utc=True).strftime("%Y%m%d")


def _tick(ts_ms, price, qty, side):
    return (ts_ms, "trade", TradeTick(
        symbol=SYM, price=price, quantity=qty, taker_side=side,
        event_time=datetime.fromtimestamp(ts_ms / 1000, tz=UTC),
    ))


def _warmup(seconds=30, price=100.0):
    """One print per second at a flat price, alternating taker side, so the
    baseline has zero mean signed flow and zero volatility."""
    return [
        _tick(T0 + i * 1000, price, 1.0, "buy" if i % 2 == 0 else "sell")
        for i in range(seconds)
    ]


def _buy_burst_tape():
    """Warmup, then a violent one-sided buy burst, then a rally print that
    clears the forced-flow target. Every number is deterministic."""
    events = _warmup()
    events += [
        _tick(T0 + 30_000, 100.05, 50.0, "buy"),   # signal print
        _tick(T0 + 30_100, 100.10, 50.0, "buy"),   # entry print (next print)
        _tick(T0 + 30_200, 100.15, 50.0, "buy"),
        _tick(T0 + 31_000, 100.25, 1.0, "buy"),    # target print
    ]
    return events


# --- TradeFlowEngine ---------------------------------------------------------------


def test_engine_warms_up_before_emitting_features():
    engine = TradeFlowEngine(min_baseline_buckets=20)
    feats = [engine.on_trade(obj) for _ts, _kind, obj in _warmup(25)]
    # buckets close as later trades arrive: first 20 prints see < 20 closed
    assert all(f is None for f in feats[:20])
    assert all(f is not None for f in feats[20:])


def test_engine_burst_z_and_velocity_signs():
    engine = TradeFlowEngine()
    feats = None
    for _ts, _kind, obj in _buy_burst_tape()[:31]:  # through the signal print
        feats = engine.on_trade(obj)
    assert feats is not None
    assert feats.signed_flow_z > 3.0          # 50-lot burst vs ±100 USD baseline
    assert feats.price_velocity_bps == pytest.approx(5.0, abs=0.1)
    # flat warmup prices -> no vol impulse on this tape
    assert feats.vol_impulse_ratio == 0.0


def test_engine_survives_dead_gaps_bounded():
    engine = TradeFlowEngine()
    engine.on_trade(_tick(T0, 100.0, 1.0, "buy")[2])
    # hours of dead tape: must fast-forward, not spin millions of buckets
    feats = engine.on_trade(_tick(T0 + 8 * 3_600_000, 100.0, 1.0, "buy")[2])
    assert feats is not None
    assert feats.signed_flow_z == 0.0         # zero-flow baseline -> zero z


# --- Cost model --------------------------------------------------------------------


def test_taker_round_trip_cost():
    assert TakerFees(taker_bps=5.0, slippage_bps=1.0).round_trip_cost_bps == 12.0
    assert TakerFees(taker_bps=4.0, slippage_bps=0.5).round_trip_cost_bps == 9.0


def test_slippage_always_adverse():
    bt = TakerReplayBacktester(TakerFees(taker_bps=5.0, slippage_bps=1.0))
    assert bt._entry_price("buy", 100.0) == pytest.approx(100.01)   # pay up to buy
    assert bt._entry_price("sell", 100.0) == pytest.approx(99.99)   # hit down to sell
    assert bt._exit_price("buy", 100.0) == pytest.approx(99.99)     # long exits by selling
    assert bt._exit_price("sell", 100.0) == pytest.approx(100.01)   # short exits by buying


def test_signal_validation():
    with pytest.raises(ValueError):
        TakerSignal("hold", 5.0, 8.0, 1000)
    with pytest.raises(ValueError):
        TakerSignal("buy", 0.0, 8.0, 1000)


# --- Deterministic replay ----------------------------------------------------------


def test_forced_flow_buy_enters_next_print_and_takes_target():
    fees = TakerFees(taker_bps=5.0, slippage_bps=1.0)
    bt = TakerReplayBacktester(fees, notional_usd=100.0)
    result = bt.run(_buy_burst_tape(), ForcedFlowBurstScalper())

    assert result.entries == 1
    assert result.signals_seen == 2   # signal print + one suppressed while long
    assert result.missed_entries == 0
    assert len(result.trades) == 1
    t = result.trades[0]
    assert t.side == "buy"
    assert t.exit_reason == "target"
    # entry fills on the print AFTER the signal (100.10), never the signal print
    entry = 100.10 * (1 + 1 / 10_000)
    exit_px = 100.25 * (1 - 1 / 10_000)
    assert t.entry_price == pytest.approx(entry)
    assert t.exit_price == pytest.approx(exit_px)
    assert t.gross_bps == pytest.approx((exit_px - entry) / entry * 10_000)
    assert t.fees_bps == pytest.approx(10.0)   # taker both legs; slippage in prices
    assert t.net_bps == pytest.approx(t.gross_bps - 10.0)
    assert result.net_usd == pytest.approx(t.net_bps / 10_000 * 100.0)


def test_replay_is_deterministic():
    bt = TakerReplayBacktester()
    a = bt.run(_buy_burst_tape(), ForcedFlowBurstScalper())
    b = bt.run(_buy_burst_tape(), ForcedFlowBurstScalper())
    assert [asdict(t) for t in a.trades] == [asdict(t) for t in b.trades]
    assert (a.signals_seen, a.entries, a.missed_entries) == (
        b.signals_seen, b.entries, b.missed_entries)


def test_sell_burst_stops_out_when_price_snaps_back():
    events = _warmup()
    events += [
        _tick(T0 + 30_000, 99.95, 50.0, "sell"),   # signal print
        _tick(T0 + 30_100, 99.90, 50.0, "sell"),   # entry print
        _tick(T0 + 31_000, 100.20, 1.0, "buy"),    # snap-back through the stop
    ]
    result = TakerReplayBacktester().run(events, ForcedFlowBurstScalper())
    assert result.entries == 1
    t = result.trades[0]
    assert t.side == "sell" and t.exit_reason == "stop"
    entry = 99.90 * (1 - 1 / 10_000)
    exit_px = 100.20 * (1 + 1 / 10_000)
    assert t.net_bps == pytest.approx(
        (entry - exit_px) / entry * 10_000 - 10.0)
    assert t.net_bps < 0


def test_max_hold_timeout_exit():
    events = _warmup()
    events += [
        _tick(T0 + 30_000, 100.05, 50.0, "buy"),
        _tick(T0 + 30_100, 100.10, 50.0, "buy"),   # entry
    ]
    # flat, alternating prints that hit neither stop nor target
    events += [
        _tick(T0 + (31 + i) * 1000, 100.05, 1.0, "buy" if i % 2 == 0 else "sell")
        for i in range(31)
    ]
    result = TakerReplayBacktester().run(events, ForcedFlowBurstScalper())
    assert result.entries == 1
    t = result.trades[0]
    assert t.exit_reason == "timeout"
    # ForcedFlow max_hold_ms=30_000 from the 30.1s entry -> 61s print exits
    assert t.exit_ts == datetime.fromtimestamp((T0 + 61_000) / 1000, tz=UTC)


def test_stale_pending_entry_is_missed_not_filled():
    events = _warmup()
    events.append(_tick(T0 + 30_000, 100.05, 50.0, "buy"))     # signal
    events.append(_tick(T0 + 40_000, 100.60, 1.0, "buy"))      # next print 10s later
    result = TakerReplayBacktester(entry_timeout_ms=5_000).run(
        events, ForcedFlowBurstScalper())
    assert result.entries == 0
    assert result.missed_entries == 1
    assert result.trades == []


def test_signal_on_last_print_never_fills():
    events = _warmup()
    events.append(_tick(T0 + 30_000, 100.05, 50.0, "buy"))     # signal, tape ends
    result = TakerReplayBacktester().run(events, ForcedFlowBurstScalper())
    assert result.entries == 0 and result.missed_entries == 1


def test_volatility_impulse_fires_on_expansion_with_flow():
    # long quiet warmup (tiny alternating vol), then successive jump buckets
    events = [
        _tick(T0 + i * 1000, 100.0 if i % 2 == 0 else 100.01, 1.0,
              "buy" if i % 2 == 0 else "sell")
        for i in range(61)
    ]
    events += [
        _tick(T0 + 61_000, 100.30, 20.0, "buy"),
        _tick(T0 + 62_000, 100.60, 20.0, "buy"),   # impulse + flow z -> signal
        _tick(T0 + 63_000, 100.90, 20.0, "buy"),   # entry print
        _tick(T0 + 64_000, 101.20, 1.0, "buy"),    # clears the 15bps target
    ]
    result = TakerReplayBacktester().run(events, VolatilityImpulseScalper())
    assert result.entries == 1
    t = result.trades[0]
    assert t.side == "buy" and t.exit_reason == "target"
    assert t.entry_price == pytest.approx(100.90 * (1 + 1 / 10_000))


# --- Rows / payload / folding hook -------------------------------------------------


def test_row_verdicts_and_policy_flags():
    fees = TakerFees()
    result = TakerReplayBacktester(fees).run(_buy_burst_tape(), ForcedFlowBurstScalper())
    row = build_row(result, exchange=HIST_EXCHANGE_ID, symbol=SYM, day=DAY,
                    family="forced_flow_continuation", fees=fees)
    assert row.verdict == "UNDER_SAMPLED_POSITIVE"   # 1 positive trade < 20
    assert row.can_trade is False and row.can_promote is False
    assert row.round_trip_cost_bps == 12.0
    assert row.exit_reason_counts == {"target": 1}
    # a losing day is NEGATIVE_EDGE even with entries
    losing = TakerReplayBacktester(fees).run(
        _warmup() + [
            _tick(T0 + 30_000, 99.95, 50.0, "sell"),
            _tick(T0 + 30_100, 99.90, 50.0, "sell"),
            _tick(T0 + 31_000, 100.20, 1.0, "buy"),
        ],
        ForcedFlowBurstScalper(),
    )
    assert build_row(losing, exchange=HIST_EXCHANGE_ID, symbol=SYM, day=DAY,
                     family="forced_flow_continuation", fees=fees).verdict == "NEGATIVE_EDGE"


def test_run_over_backfilled_lake_end_to_end(tmp_path):
    # write the synthetic tape as a real backfilled shard, then replay it
    rows = [
        {"ts_ms": ts, "price": obj.price, "amount": obj.quantity,
         "side": obj.taker_side}
        for ts, _kind, obj in _buy_burst_tape()
    ]
    df = pd.DataFrame(rows, columns=["ts_ms", "price", "amount", "side"])
    write_trade_shard(df, tmp_path, SYM, DAY)

    payload = run_event_taker_replay(tmp_path, [SYM])   # days auto-discovered
    assert payload["policy"]["can_trade"] is False
    assert payload["policy"]["can_promote"] is False
    assert payload["policy"]["execution_model"] == "taker_only"
    assert payload["cost_model"]["round_trip_cost_bps"] == 12.0
    assert payload["days"] == [DAY]
    by_family = {r["family"]: r for r in payload["rows"]}
    assert set(by_family) == {"forced_flow_continuation", "volatility_impulse"}
    ff = by_family["forced_flow_continuation"]
    assert ff["exchange"] == HIST_EXCHANGE_ID
    assert ff["entries"] == 1 and ff["verdict"] == "UNDER_SAMPLED_POSITIVE"
    assert by_family["volatility_impulse"]["verdict"] == "NO_SIGNALS"
    assert payload["summary"]["rows"] == 2


def test_folding_hook_into_continuous_research(tmp_path, monkeypatch):
    out_dir = tmp_path / "live_research"
    monkeypatch.setattr(cr, "OUT_DIR", out_dir)
    assert cr._load_event_taker_latest() == {}          # absent -> {}

    payload = {"policy": event_taker_policy(), "rows": [], "summary": {"rows": 0}}
    path = write_event_taker_payload(payload, out_dir)
    assert path.name == EVENT_TAKER_LATEST
    assert not list(out_dir.glob("*.tmp"))              # atomic publish
    assert cr._load_event_taker_latest() == payload

    cr.publish(cr.ResearchPayload(
        started=0.0, event_taker_replay=cr._load_event_taker_latest()))
    latest = json.loads((out_dir / "latest.json").read_text())
    assert latest["event_taker_replay"]["policy"]["can_trade"] is False
    assert latest["event_taker_replay"]["policy"]["execution_model"] == "taker_only"

    (out_dir / EVENT_TAKER_LATEST).write_text("{corrupt")
    assert cr._load_event_taker_latest() == {}          # unreadable -> {}
