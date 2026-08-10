"""Cascade reversion — causal threshold, detection, resolution, costs, folding."""

import json

import pandas as pd
import pytest

import vnedge.research.continuous_research as cr
from vnedge.research.cascade_reversion import (
    CASCADE_REVERSION_LATEST,
    CascadeDetector,
    CascadeParams,
    CascadeReversionReplayer,
    LiquidationEvent,
    TradePrint,
    _ActiveCascade,
    _OpenEvaluation,
    cascade_reversion_policy,
    cascade_verdict,
    cost_models_for,
    discover_liquidation_days,
    render_report,
    run_cascade_reversion,
    write_cascade_reversion_payload,
)

SYM = "BTC/USDT:USDT"
EX = "binanceusdm"
T0 = 1_752_000_000_000          # exact ms epoch, deterministic
C0 = T0 + 300_000               # cascade epoch: warmup strictly before this

P = CascadeParams(
    burst_window_ms=10_000,
    trailing_window_ms=600_000,
    threshold_pct=0.95,
    min_history_events=10,
    min_burst_notional_usd=100.0,
    one_sided_min=0.80,
    exhaustion_peak_frac=0.25,
    exhaustion_quiet_ms=5_000,
    pre_vwap_window_ms=60_000,
    stop_buffer_frac=0.10,
    timeout_ms=60_000,
    min_events_for_candidate=20,
)


def _liq(ts_ms, price, notional, side):
    return LiquidationEvent(ts_ms=ts_ms, price=price, amount=notional / price,
                            side=side, notional_usd=notional)


def _trade(ts_ms, price, qty=1.0):
    return TradePrint(ts_ms=ts_ms, price=price, amount=qty)


def _warmup_liqs():
    """12 small liquidations, each alone in its burst window -> every trailing
    rolling sum is exactly 10, so the p95 threshold is exactly 10."""
    return [
        _liq(C0 - 240_000 + i * 20_000, 100.0, 10.0, "sell" if i % 2 else "buy")
        for i in range(12)
    ]


def _pre_trades():
    """60 one-lot prints at exactly 100 in the minute before C0 -> VWAP 100."""
    return [_trade(C0 - 59_000 + i * 1_000, 100.0) for i in range(60)]


def _sell_burst():
    """One-sided sell cascade (longs forced out): 300 USD in 3s, extreme 98.5."""
    return [
        _liq(C0 + 1_000, 99.5, 100.0, "sell"),
        _liq(C0 + 2_000, 99.0, 100.0, "sell"),
        _liq(C0 + 3_000, 98.5, 100.0, "sell"),
    ]


def _cascade_trades():
    """Prints during the cascade + the entry print after the quiet window."""
    return [
        _trade(C0 + 1_500, 99.4),
        _trade(C0 + 2_500, 99.0),
        _trade(C0 + 3_500, 98.6),
        _trade(C0 + 5_000, 98.7),   # quiet only 2s — not yet exhausted
        _trade(C0 + 8_500, 98.8),   # 5.5s after last big liq — ENTRY print
    ]


def _run(liqs, trades, params=P, exchange=EX):
    replayer = CascadeReversionReplayer(params, cost_models_for(exchange))
    return replayer.run(liqs, trades, exchange=exchange, symbol=SYM, day="20260708")


# --- Causal threshold --------------------------------------------------------------


def test_threshold_causality_by_truncation():
    """Streaming fire decisions must equal decisions on the truncated tape:
    the threshold at event k uses only events < k, never the future."""
    tape = _warmup_liqs() + _sell_burst() + [
        _liq(C0 + 30_000, 98.0, 400.0, "sell"),
        _liq(C0 + 45_000, 98.2, 5.0, "buy"),
        _liq(C0 + 60_000, 98.1, 250.0, "sell"),
    ]
    full = CascadeDetector(P)
    full_decisions = [full.on_liquidation(ev) is not None for ev in tape]
    assert any(full_decisions)                       # the tape does fire
    for k in range(len(tape)):
        det = CascadeDetector(P)
        prefix = [det.on_liquidation(ev) is not None for ev in tape[: k + 1]]
        assert prefix == full_decisions[: k + 1]


def test_detector_warmup_blocks_fires():
    det = CascadeDetector(P)
    # huge one-sided events immediately — but zero history: never fires
    for i in range(P.min_history_events - 1):
        assert det.on_liquidation(_liq(T0 + i * 20_000, 100.0, 9_999.0, "sell")) is None


def test_detector_fires_on_one_sided_burst():
    det = CascadeDetector(P)
    for ev in _warmup_liqs():
        assert det.on_liquidation(ev) is None
    fired = det.on_liquidation(_sell_burst()[0])
    assert fired is not None
    assert fired.side == "sell"
    assert fired.start_ms == C0 + 1_000
    assert fired.one_sided_frac == 1.0
    assert fired.burst_notional_usd == pytest.approx(100.0)
    assert fired.threshold_usd == pytest.approx(10.0)
    assert fired.peak_notional_usd == pytest.approx(100.0)
    assert fired.extreme_price == pytest.approx(99.5)


def test_detector_rejects_mixed_side_burst():
    det = CascadeDetector(P)
    for ev in _warmup_liqs():
        det.on_liquidation(ev)
    burst = [
        _liq(C0 + 1_000, 99.5, 50.0, "sell"),
        _liq(C0 + 2_000, 99.0, 50.0, "buy"),
        _liq(C0 + 3_000, 98.5, 50.0, "sell"),
    ]
    assert all(det.on_liquidation(ev) is None for ev in burst)


def test_unknown_side_dilutes_one_sidedness():
    det = CascadeDetector(P)
    for ev in _warmup_liqs():
        det.on_liquidation(ev)
    burst = [
        _liq(C0 + 1_000, 99.5, 50.0, "sell"),
        _liq(C0 + 2_000, 99.0, 50.0, ""),      # unknown: counts to total only
        _liq(C0 + 3_000, 98.5, 50.0, "sell"),
    ]
    assert all(det.on_liquidation(ev) is None for ev in burst)


def test_absolute_floor_blocks_dust_bursts():
    det = CascadeDetector(P)
    # trailing history of tiny sums -> percentile threshold is tiny, but the
    # absolute floor still blocks a 50 USD "burst"
    for i in range(12):
        det.on_liquidation(_liq(C0 - 240_000 + i * 20_000, 100.0, 1.0, "sell"))
    assert det.on_liquidation(_liq(C0 + 1_000, 99.5, 50.0, "sell")) is None


# --- Detection -> exhaustion -> entry ----------------------------------------------


def test_full_cascade_target_resolution():
    liqs = _warmup_liqs() + _sell_burst()
    trades = _pre_trades() + _cascade_trades() + [_trade(C0 + 20_000, 100.2)]
    result = _run(liqs, trades)
    assert result.cascades_detected == 1
    assert result.entries == 1
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.cascade_side == "sell"
    assert row.direction == "buy"                    # against the cascade
    assert row.pre_vwap == pytest.approx(100.0)      # pre-cascade reference
    assert row.extreme_price == pytest.approx(98.5)
    assert row.stop_price == pytest.approx(98.35)    # extreme - 10% of 1.5
    assert row.entry_ts_ms == C0 + 8_500             # first trade after quiet
    assert row.entry_price_raw == pytest.approx(98.8)
    assert row.exit_reason == "target"
    assert row.exit_price_raw == pytest.approx(100.2)
    models = cost_models_for(EX)
    assert row.taker_net_bps == pytest.approx(
        models["taker_taker"].net_bps("buy", 98.8, 100.2))
    assert row.maker_first_net_bps == pytest.approx(
        models["maker_first"].net_bps("buy", 98.8, 100.2))
    assert row.maker_first_net_bps > row.taker_net_bps   # maker model is cheaper


def test_full_cascade_stop_resolution_and_overlap_suppression():
    liqs = _warmup_liqs() + _sell_burst() + [
        _liq(C0 + 15_000, 98.4, 400.0, "sell"),      # fires again while open
    ]
    trades = _pre_trades() + _cascade_trades() + [_trade(C0 + 20_000, 98.3)]
    result = _run(liqs, trades)
    assert result.cascades_detected == 1
    assert result.overlapping_cascades == 1          # suppressed, never queued
    assert len(result.rows) == 1
    assert result.rows[0].exit_reason == "stop"      # 98.3 <= 98.35
    assert result.rows[0].taker_net_bps < 0


def test_timeout_resolution():
    liqs = _warmup_liqs() + _sell_burst()
    trades = _pre_trades() + _cascade_trades() + [
        _trade(C0 + 30_000, 99.0),                   # inside stop/target, young
        _trade(C0 + 68_600, 99.0),                   # 60.1s after entry
    ]
    result = _run(liqs, trades)
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.exit_reason == "timeout"
    assert row.exit_ts_ms == C0 + 68_600
    assert row.exit_price_raw == pytest.approx(99.0)


def test_end_of_tape_closes_open_evaluation():
    liqs = _warmup_liqs() + _sell_burst()
    trades = _pre_trades() + _cascade_trades()       # ends on the entry print
    result = _run(liqs, trades)
    assert len(result.rows) == 1
    assert result.rows[0].exit_reason == "end"


def test_big_liquidation_during_quiet_resumes_cascade():
    liqs = _warmup_liqs() + _sell_burst() + [
        _liq(C0 + 6_000, 98.4, 100.0, "sell"),       # > 25% of peak: resumes
    ]
    trades = _pre_trades() + _cascade_trades() + [
        _trade(C0 + 12_000, 98.8),                   # 6s after the resume print
        _trade(C0 + 20_000, 100.2),
    ]
    result = _run(liqs, trades)
    assert result.entries == 1
    # the C0+8.5s print is only 2.5s after the resume — entry moved to 12s
    assert result.rows[0].entry_ts_ms == C0 + 12_000
    assert result.rows[0].extreme_price == pytest.approx(98.4)


def test_small_liquidation_during_quiet_does_not_resume():
    liqs = _warmup_liqs() + _sell_burst() + [
        _liq(C0 + 6_000, 98.6, 10.0, "sell"),        # 10% of peak: noise
    ]
    trades = _pre_trades() + _cascade_trades() + [_trade(C0 + 20_000, 100.2)]
    result = _run(liqs, trades)
    assert result.entries == 1
    assert result.rows[0].entry_ts_ms == C0 + 8_500  # entry unchanged


def test_already_reverted_entry_is_skipped():
    liqs = _warmup_liqs() + _sell_burst()
    trades = _pre_trades() + [
        _trade(C0 + 1_500, 99.4),
        _trade(C0 + 3_500, 100.3),                   # price snaps back fast
        _trade(C0 + 8_500, 100.5),                   # entry print >= VWAP target
    ]
    result = _run(liqs, trades)
    assert result.cascades_detected == 1
    assert result.entries == 0
    assert result.skipped_already_reverted == 1
    assert result.rows == []


def test_no_pre_vwap_skips_cascade():
    liqs = _warmup_liqs() + _sell_burst()
    trades = [_trade(C0 + 8_500, 98.8)]              # no trades before the cascade
    result = _run(liqs, trades)
    assert result.cascades_detected == 1
    assert result.skipped_no_pre_vwap == 1
    assert result.entries == 0


def test_buy_cascade_enters_short_and_targets_down():
    """Shorts liquidated (forced BUY orders) push price up; we sell the pop."""
    liqs = _warmup_liqs() + [
        _liq(C0 + 1_000, 100.5, 100.0, "buy"),
        _liq(C0 + 2_000, 101.0, 100.0, "buy"),
        _liq(C0 + 3_000, 101.5, 100.0, "buy"),
    ]
    trades = _pre_trades() + [
        _trade(C0 + 3_500, 101.4),
        _trade(C0 + 8_500, 101.2),                   # entry print
        _trade(C0 + 20_000, 99.9),                   # <= VWAP target
    ]
    result = _run(liqs, trades)
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.cascade_side == "buy"
    assert row.direction == "sell"
    assert row.extreme_price == pytest.approx(101.5)
    assert row.stop_price == pytest.approx(101.65)   # extreme + 10% of 1.5
    assert row.exit_reason == "target"


# --- Tie-break and exit ordering ---------------------------------------------------


def _dummy_position(direction, stop, target):
    start = CascadeDetector(P)  # unused; only need a structurally valid cascade
    del start
    from vnedge.research.cascade_reversion import CascadeStart

    cascade = _ActiveCascade(
        start=CascadeStart(side="sell" if direction == "buy" else "buy",
                           start_ms=0, detected_ms=0, burst_notional_usd=1.0,
                           one_sided_frac=1.0, threshold_usd=1.0,
                           peak_notional_usd=1.0, extreme_price=100.0),
        peak_notional=1.0, last_significant_ms=0, extreme_price=100.0,
        pre_vwap=target,
    )
    return _OpenEvaluation(cascade=cascade, direction=direction, entry_ts_ms=0,
                           entry_price=100.0, stop_price=stop, target_price=target)


def test_stop_wins_stop_vs_target_tie():
    replayer = CascadeReversionReplayer(P, cost_models_for(EX))
    # degenerate stop == target: a print touching both must resolve as STOP
    long_pos = _dummy_position("buy", stop=99.0, target=99.0)
    assert replayer._exit_reason(long_pos, _trade(1_000, 99.0)) == "stop"
    short_pos = _dummy_position("sell", stop=101.0, target=101.0)
    assert replayer._exit_reason(short_pos, _trade(1_000, 101.0)) == "stop"


def test_exact_touch_semantics_and_timeout_ordering():
    replayer = CascadeReversionReplayer(P, cost_models_for(EX))
    pos = _dummy_position("buy", stop=99.0, target=101.0)
    assert replayer._exit_reason(pos, _trade(1_000, 99.0)) == "stop"      # <= stop
    assert replayer._exit_reason(pos, _trade(1_000, 101.0)) == "target"   # >= target
    assert replayer._exit_reason(pos, _trade(1_000, 100.0)) is None
    # stop/target beat timeout on the same print
    assert replayer._exit_reason(pos, _trade(P.timeout_ms, 99.0)) == "stop"
    assert replayer._exit_reason(pos, _trade(P.timeout_ms, 100.0)) == "timeout"


# --- Cost models -------------------------------------------------------------------


def test_taker_taker_cost_math():
    model = cost_models_for("binanceusdm")["taker_taker"]
    assert model.round_trip_cost_bps == pytest.approx(12.0)   # 2*(5 + 1)
    assert not model.assumed_maker_fill
    # flat prices: net is exactly minus the full round trip (slippage adverse
    # on both legs, taker fee on both legs)
    assert model.net_bps("buy", 100.0, 100.0) == pytest.approx(-12.0, abs=0.01)
    assert model.net_bps("sell", 100.0, 100.0) == pytest.approx(-12.0, abs=0.01)


def test_maker_first_cost_math_and_flag():
    model = cost_models_for("binanceusdm")["maker_first"]
    assert model.round_trip_cost_bps == pytest.approx(8.0)    # 2 + 5 + 1
    assert model.assumed_maker_fill
    assert "ASSUMED_MAKER_FILL" in model.to_dict()["caveat"]
    assert model.net_bps("buy", 100.0, 100.0) == pytest.approx(-8.0, abs=0.01)
    assert model.net_bps("sell", 100.0, 100.0) == pytest.approx(-8.0, abs=0.01)


def test_cost_model_slippage_always_adverse():
    model = cost_models_for("binanceusdm")["taker_taker"]
    up = model.net_bps("buy", 100.0, 101.0)
    down = model.net_bps("sell", 101.0, 100.0)
    # hand-derived: buy 100.01 (slip up), sell 100.9899 (slip down), fees 10:
    # gross_up = 0.9799/100.01*1e4, gross_down uses the short entry as base
    assert up == pytest.approx(0.9799 / 100.01 * 1e4 - 10.0, abs=1e-6)
    assert down == pytest.approx(0.9799 / 100.9899 * 1e4 - 10.0, abs=1e-6)
    # a frictionless 100bps winner nets 90 after fees alone; slippage on both
    # legs must strictly reduce that further
    assert up < 90.0 and down < 90.0
    with pytest.raises(ValueError):
        model.net_bps("hold", 100.0, 100.0)


def test_hist_exchange_maps_to_live_fee_profile():
    assert cost_models_for("binanceusdm_hist") == cost_models_for("binanceusdm")


# --- Params ------------------------------------------------------------------------


def test_params_validation():
    with pytest.raises(ValueError):
        CascadeParams(threshold_pct=1.5)
    with pytest.raises(ValueError):
        CascadeParams(one_sided_min=0.3)
    with pytest.raises(ValueError):
        CascadeParams(exhaustion_peak_frac=0.0)
    with pytest.raises(ValueError):
        CascadeParams(trailing_window_ms=1_000, burst_window_ms=60_000)


def test_params_from_env(monkeypatch):
    monkeypatch.setenv("CASCADE_ONE_SIDED_MIN", "0.9")
    monkeypatch.setenv("CASCADE_EXHAUSTION_QUIET_MS", "30000")
    monkeypatch.setenv("CASCADE_THRESHOLD_PCT", "not-a-number")   # ignored
    params = CascadeParams.from_env()
    assert params.one_sided_min == 0.9
    assert params.exhaustion_quiet_ms == 30_000
    assert params.threshold_pct == CascadeParams().threshold_pct


# --- Verdicts ----------------------------------------------------------------------


def test_verdict_vocabulary():
    assert cascade_verdict(5, 10.0, 10.0, 20) == "UNDER_SAMPLED"
    assert cascade_verdict(0, 0.0, 0.0, 20) == "UNDER_SAMPLED"
    assert cascade_verdict(25, 3.0, -1.0, 20) == "CANDIDATE"
    assert cascade_verdict(25, -1.0, 2.0, 20) == "MAKER_ONLY_POSITIVE"
    assert cascade_verdict(25, -1.0, -0.5, 20) == "NEGATIVE_EDGE"
    assert cascade_verdict(20, 0.0, 0.0, 20) == "NEGATIVE_EDGE"   # flat is not edge


def test_policy_guards():
    policy = cascade_reversion_policy()
    assert policy["can_trade"] is False
    assert policy["can_promote"] is False
    assert policy["requires_untouched_judgment"] is True
    assert policy["requires_human_approval"] is True


# --- Day scanner / absent-day handling ---------------------------------------------


def _write_shard(root, exchange, stream, day, df, name="0001.parquet"):
    d = root / "ticks" / f"exchange={exchange}" / "symbol=BTCUSDT" / f"stream={stream}" / day
    d.mkdir(parents=True)
    df.to_parquet(d / name, index=False)


def _liq_df(liqs):
    return pd.DataFrame([{
        "ts_ms": e.ts_ms, "price": e.price, "amount": e.amount,
        "side": e.side, "notional_usd": e.notional_usd,
    } for e in liqs])


def _trade_df(trades):
    return pd.DataFrame([{
        "ts_ms": t.ts_ms, "price": t.price, "amount": t.amount, "side": "buy",
    } for t in trades])


def test_scanner_handles_absent_streams_and_days(tmp_path):
    # day 1: liquidations but NO trade tape; day 2: both; other symbols: nothing
    _write_shard(tmp_path, EX, "liquidations", "20260708", _liq_df(_warmup_liqs()))
    _write_shard(tmp_path, EX, "liquidations", "20260709",
                 _liq_df(_warmup_liqs() + _sell_burst()))
    _write_shard(tmp_path, EX, "trades", "20260709",
                 _trade_df(_pre_trades() + _cascade_trades()
                           + [_trade(C0 + 20_000, 100.2)]))
    payload = run_cascade_reversion(
        tmp_path, exchanges=(EX,), symbols=(SYM, "ETH/USDT:USDT"), params=P)
    by_symbol = {t["symbol"]: t for t in payload["targets"]}
    btc = by_symbol[SYM]
    assert btc["days_missing_trades"] == ["20260708"]
    assert btc["days_scanned"] == ["20260709"]
    assert btc["events"] == 1
    assert btc["verdict"] == "UNDER_SAMPLED"          # 1 event < 20
    assert btc["can_trade"] is False and btc["can_promote"] is False
    assert btc["aggregates"]["taker_taker"]["events"] == 1
    assert btc["aggregates"]["maker_first"]["net_usd"] > \
        btc["aggregates"]["taker_taker"]["net_usd"]
    eth = by_symbol["ETH/USDT:USDT"]
    assert eth["days_with_liquidations"] == []        # absent stream: graceful
    assert eth["events"] == 0
    assert payload["summary"]["events"] == 1
    assert payload["can_trade"] is False
    assert "ASSUMED_MAKER_FILL" in payload["cost_models"]["maker_first"]["caveat"]
    assert render_report(payload)                     # never crashes


def test_scanner_on_empty_root(tmp_path):
    payload = run_cascade_reversion(tmp_path, exchanges=(EX,), symbols=(SYM,), params=P)
    assert payload["targets"][0]["verdict"] == "UNDER_SAMPLED"
    assert payload["summary"]["events"] == 0
    assert "UNDER_SAMPLED" in render_report(payload)
    empty = run_cascade_reversion(tmp_path, exchanges=(), symbols=(SYM,), params=P)
    assert "no liquidation stream" in render_report(empty)


def test_loaders_drop_zero_price_rows(tmp_path):
    """Real recorded tapes contain occasional zero-price rows; against a 0.0
    print a short trivially 'hits target' for ~10000 bps and a 0.0 cascade
    extreme produces a negative stop. Loaders must drop them (found on the
    VM's live tape, 2026-07-11)."""
    from vnedge.research.cascade_reversion import (
        load_liquidation_events,
        load_trade_prints,
    )

    liq_df = _liq_df(_warmup_liqs()[:2])
    liq_df.loc[len(liq_df)] = {"ts_ms": C0, "price": 0.0, "amount": 1.0,
                               "side": "sell", "notional_usd": 0.0}
    _write_shard(tmp_path, EX, "liquidations", "20260708", liq_df)
    trade_df = _trade_df(_pre_trades()[:2])
    trade_df.loc[len(trade_df)] = {"ts_ms": C0, "price": 0.0, "amount": 1.0,
                                   "side": "buy"}
    trade_df.loc[len(trade_df)] = {"ts_ms": C0 + 1, "price": 100.0, "amount": 0.0,
                                   "side": "buy"}
    _write_shard(tmp_path, EX, "trades", "20260708", trade_df)

    liqs = load_liquidation_events(tmp_path, EX, SYM, "20260708")
    assert len(liqs) == 2 and all(e.price > 0 for e in liqs)
    trades, source = load_trade_prints(tmp_path, EX, SYM, "20260708")
    assert source == EX
    assert len(trades) == 2 and all(t.price > 0 and t.amount > 0 for t in trades)


def test_discover_days_both_layouts(tmp_path):
    _write_shard(tmp_path, EX, "liquidations", "20260708", _liq_df(_warmup_liqs()[:2]))
    legacy = (tmp_path / "ticks" / f"exchange={EX}" / "symbol=BTCUSDT"
              / "stream=liquidations")
    _liq_df(_warmup_liqs()[:2]).to_parquet(legacy / "20260707.parquet", index=False)
    (legacy / "junk").mkdir()                         # non-day dirs are ignored
    assert discover_liquidation_days(tmp_path, EX, SYM) == ("20260707", "20260708")


# --- Publish + folding hook --------------------------------------------------------


def test_folding_hook_into_continuous_research(tmp_path, monkeypatch):
    out_dir = tmp_path / "live_research"
    monkeypatch.setattr(cr, "OUT_DIR", out_dir)
    assert cr._load_cascade_reversion_latest() == {}          # absent -> {}

    payload = {"policy": cascade_reversion_policy(), "targets": [],
               "summary": {"events": 0}}
    path = write_cascade_reversion_payload(payload, out_dir)
    assert path.name == CASCADE_REVERSION_LATEST
    assert not list(out_dir.glob("*.tmp"))                    # atomic publish
    assert cr._load_cascade_reversion_latest() == payload

    cr.publish(cr.ResearchPayload(
        started=0.0, cascade_reversion=cr._load_cascade_reversion_latest()))
    latest = json.loads((out_dir / "latest.json").read_text())
    assert latest["cascade_reversion"]["policy"]["can_trade"] is False
    assert latest["cascade_reversion"]["policy"]["family"] == \
        "liquidation_cascade_reversion"

    (out_dir / CASCADE_REVERSION_LATEST).write_text("{corrupt")
    assert cr._load_cascade_reversion_latest() == {}          # unreadable -> {}
