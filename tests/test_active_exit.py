

def test_dynamic_atr_trail_ratchets_and_tightens_only():
    from vnedge.runtime.active_exit import ActiveExitState
    from vnedge.strategy.base_strategy import SignalIntent

    st = ActiveExitState.from_signal(
        SignalIntent(side="long", stop_price=98.0, take_profit_price=None,
                     take_profit_levels=(), reason="t"),
        entry_price=100.0, quantity=1.0, trail_atr_mult=2.0,
    )
    # bar runs to 105; no exit; trail arms the stop to 105 - 2*ATR(1) = 103
    assert st.resolve_bar(high=105.0, low=100.0, close=104.0, position_quantity=1.0) is None
    st.trail_stop(atr=1.0)
    assert st.current_stop == 103.0                      # ratcheted up from 98
    # a flat/weaker bar must NOT loosen it
    assert st.resolve_bar(high=104.0, low=103.5, close=104.0, position_quantity=1.0) is None
    st.trail_stop(atr=1.0)                                # mfe still 105 -> stop stays 103
    assert st.current_stop == 103.0
    # price runs higher -> trail follows UP
    st.resolve_bar(high=110.0, low=104.0, close=109.0, position_quantity=1.0)
    st.trail_stop(atr=1.0)
    assert st.current_stop == 108.0                      # 110 - 2
    # a later bar dipping to 108 exits at the trailed stop (locked +8%)
    d = st.resolve_bar(high=109.0, low=107.0, close=108.0, position_quantity=1.0)
    assert d is not None and d.final and d.exit_price == 108.0


def test_trail_off_is_legacy_arm_and_lock():
    from vnedge.runtime.active_exit import ActiveExitState
    from vnedge.strategy.base_strategy import SignalIntent

    st = ActiveExitState.from_signal(
        SignalIntent(side="long", stop_price=98.0, take_profit_price=None,
                     take_profit_levels=(), reason="t"),
        entry_price=100.0, quantity=1.0, trail_atr_mult=0.0,
    )
    st.resolve_bar(high=110.0, low=100.0, close=109.0, position_quantity=1.0)
    st.trail_stop(atr=1.0)
    assert st.current_stop == 98.0  # no trailing when mult=0
