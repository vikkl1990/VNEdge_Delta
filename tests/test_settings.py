"""Live-trading gates — no single env mistake may enable live orders."""

from vnedge.config.settings import LIVE_CONFIRMATION_PHRASE, Settings, TradingMode


def make(**overrides) -> Settings:
    # _env_file=None: tests must not be affected by a developer's local .env
    return Settings(_env_file=None, **overrides)


def test_default_is_backtest_and_not_live():
    s = make()
    assert s.trading_mode is TradingMode.BACKTEST
    assert not s.is_live
    assert s.entries_allowed


def test_mode_alone_is_not_live():
    assert not make(trading_mode=TradingMode.LIVE_SMALL).is_live


def test_mode_plus_flag_is_not_live():
    s = make(trading_mode=TradingMode.LIVE_SMALL, live_trading_enabled=True)
    assert not s.is_live


def test_wrong_confirmation_phrase_is_not_live():
    s = make(
        trading_mode=TradingMode.LIVE_SMALL,
        live_trading_enabled=True,
        confirm_live_trading="yes",
    )
    assert not s.is_live


def test_all_three_gates_open_is_live():
    s = make(
        trading_mode=TradingMode.LIVE_SMALL,
        live_trading_enabled=True,
        confirm_live_trading=LIVE_CONFIRMATION_PHRASE,
    )
    assert s.is_live


def test_shadow_mode_never_live_even_with_gates_open():
    s = make(
        trading_mode=TradingMode.SHADOW,
        live_trading_enabled=True,
        confirm_live_trading=LIVE_CONFIRMATION_PHRASE,
    )
    assert not s.is_live


def test_emergency_reduce_only_is_live_but_blocks_entries():
    s = make(
        trading_mode=TradingMode.EMERGENCY_REDUCE_ONLY,
        live_trading_enabled=True,
        confirm_live_trading=LIVE_CONFIRMATION_PHRASE,
    )
    assert s.is_live
    assert not s.entries_allowed
