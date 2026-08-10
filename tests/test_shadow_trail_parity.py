"""P0 parity: the shadow ATR-trail must ratchet to the IDENTICAL stop the
paper/live ActiveExitState produces on the same bars, ATR, and multiplier."""
import pandas as pd

from vnedge.execution.journal import DecisionJournal
from vnedge.runtime.active_exit import ActiveExitState
from vnedge.runtime.shadow_outcomes import ShadowOutcomeTracker
from vnedge.strategy.base_strategy import SignalIntent

MULT = 3.0
ATR = 1.0
ENTRY = 100.0
INIT_STOP = 95.0


def _bar(ts, high, low, close):
    return pd.Series({"timestamp": pd.Timestamp(ts, tz="UTC"),
                      "open": close, "high": high, "low": low,
                      "close": close, "volume": 5.0})


def test_shadow_trail_matches_active_exit_bar_for_bar(tmp_path):
    # A rising long whose lows always stay above the ratcheting stop (mfe-3*ATR).
    bars = [
        (102.0, 100.0, 101.0),
        (104.0, 101.0, 103.0),
        (107.0, 103.0, 106.0),
        (110.0, 106.0, 109.0),
        (109.5, 108.0, 108.5),   # lower high — stop must NOT loosen
        (112.0, 109.0, 111.0),
    ]

    # --- paper/live engine ---
    sig = SignalIntent("long", stop_price=INIT_STOP)
    st = ActiveExitState.from_signal(sig, entry_price=ENTRY, quantity=1.0, trail_atr_mult=MULT)

    # --- shadow engine ---
    tracker = ShadowOutcomeTracker(DecisionJournal(tmp_path / "j.jsonl"), trail_atr_mult=MULT)
    tracker.track(intent_key="k", side="long", quantity=1.0, notional_usd=ENTRY,
                  stop_price=INIT_STOP, take_profit_price=None,
                  decision_bar_ts=pd.Timestamp("2024-01-01T00:00:00Z"))

    paper_stops, shadow_stops = [], []
    for i, (high, low, close) in enumerate(bars, start=1):
        ts = f"2024-01-01T{i:02d}:00:00Z"
        # paper: resolve then trail (same order the runner uses)
        dec = st.resolve_bar(high=high, low=low, close=close, position_quantity=1.0)
        assert dec is None, "test path must not exit early"
        st.trail_stop(ATR)
        paper_stops.append(round(st.current_stop, 8))
        # shadow: resolve_bar does check-then-ratchet internally, same ATR
        tracker.resolve_bar(_bar(ts, high, low, close), atr=ATR)
        shadow_stops.append(round(tracker._pending["k"].stop_price, 8))

    assert shadow_stops == paper_stops, f"parity broken:\n paper ={paper_stops}\n shadow={shadow_stops}"
    # sanity: it actually trailed up, and the lower-high bar did not loosen it
    assert shadow_stops[3] > shadow_stops[0]        # ratcheted up
    assert shadow_stops[4] == shadow_stops[3]        # tighten-only


def test_shadow_trail_off_by_default_is_unchanged(tmp_path):
    # trail_atr_mult=0 (default) must leave the stop fixed — no behaviour change.
    tracker = ShadowOutcomeTracker(DecisionJournal(tmp_path / "j.jsonl"))
    tracker.track(intent_key="k", side="long", quantity=1.0, notional_usd=ENTRY,
                  stop_price=INIT_STOP, take_profit_price=None,
                  decision_bar_ts=pd.Timestamp("2024-01-01T00:00:00Z"))
    for i in range(1, 5):
        tracker.resolve_bar(_bar(f"2024-01-01T{i:02d}:00:00Z", 100.0 + i, 99.0, 100.0 + i), atr=ATR)
    assert tracker._pending["k"].stop_price == INIT_STOP  # never moved


def test_shadow_short_trail_matches_active_exit(tmp_path):
    # falling short — stop ratchets DOWN, tighten-only.
    bars = [(100.5, 99.0, 99.5), (99.0, 97.0, 97.5), (97.5, 94.0, 95.0), (96.0, 93.0, 94.0)]
    sig = SignalIntent("short", stop_price=105.0)
    st = ActiveExitState.from_signal(sig, entry_price=ENTRY, quantity=1.0, trail_atr_mult=MULT)
    tracker = ShadowOutcomeTracker(DecisionJournal(tmp_path / "j.jsonl"), trail_atr_mult=MULT)
    tracker.track(intent_key="k", side="short", quantity=1.0, notional_usd=ENTRY,
                  stop_price=105.0, take_profit_price=None,
                  decision_bar_ts=pd.Timestamp("2024-01-01T00:00:00Z"))
    for i, (high, low, close) in enumerate(bars, start=1):
        dec = st.resolve_bar(high=high, low=low, close=close, position_quantity=1.0)
        assert dec is None
        st.trail_stop(ATR)
        tracker.resolve_bar(_bar(f"2024-01-01T{i:02d}:00:00Z", high, low, close), atr=ATR)
        assert round(tracker._pending["k"].stop_price, 8) == round(st.current_stop, 8)
