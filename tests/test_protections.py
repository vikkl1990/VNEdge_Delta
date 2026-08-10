"""Entry-protection state machine + engine parity.

The protections module is a pure state machine (risk/protections.py) that
both engines consult identically: cooldown after STOP exits only, and a
stop-window guard. Defaults are OFF — zero behavior change unless a config
explicitly enables a protection.
"""

import pytest
from pydantic import ValidationError

from vnedge.risk.protections import ProtectionConfig, ProtectionState


def state(**kwargs) -> ProtectionState:
    return ProtectionState(ProtectionConfig(**kwargs))


# --- Defaults: everything OFF ------------------------------------------------------


def test_defaults_are_off():
    cfg = ProtectionConfig()
    assert cfg.cooldown_bars_after_stop == 0
    assert cfg.max_stops_per_window == 0
    assert cfg.stop_window_bars == 0
    assert not cfg.enabled


def test_default_config_never_blocks_even_after_stops():
    s = state()
    for bar in range(10):
        s.on_exit("stop", bar)
        allowed, reason = s.entries_allowed(bar)
        assert allowed and reason is None


# --- Cooldown after stop -----------------------------------------------------------


def test_cooldown_blocks_same_bar_reentry():
    s = state(cooldown_bars_after_stop=1)
    s.on_exit("stop", 5)
    allowed, reason = s.entries_allowed(5)
    assert not allowed
    assert reason == "post_exit_cooldown: 1 bar(s) remaining"
    assert s.entries_allowed(6) == (True, None)


def test_cooldown_counts_down_across_bars():
    s = state(cooldown_bars_after_stop=3)
    s.on_exit("stop", 10)
    assert s.entries_allowed(10) == (False, "post_exit_cooldown: 3 bar(s) remaining")
    assert s.entries_allowed(11) == (False, "post_exit_cooldown: 2 bar(s) remaining")
    assert s.entries_allowed(12) == (False, "post_exit_cooldown: 1 bar(s) remaining")
    assert s.entries_allowed(13) == (True, None)


def test_cooldown_applies_only_to_stop_exits():
    # Refinement over the original any-exit cooldown: a winner closing is not
    # evidence the entry condition went bad.
    s = state(cooldown_bars_after_stop=2)
    for reason in ("take_profit", "max_holding", "end_of_data"):
        s.on_exit(reason, 5)
        assert s.entries_allowed(5) == (True, None)


def test_tick_stop_counts_as_stop():
    s = state(cooldown_bars_after_stop=1)
    s.on_exit("tick_stop", 7)
    allowed, _ = s.entries_allowed(7)
    assert not allowed


def test_overlapping_stops_extend_not_shrink_cooldown():
    s = state(cooldown_bars_after_stop=3)
    s.on_exit("stop", 10)  # blocks 10, 11, 12
    s.on_exit("stop", 11)  # blocks through 13
    assert s.entries_allowed(13)[0] is False
    assert s.entries_allowed(14)[0] is True


# --- Stop-window guard -------------------------------------------------------------


def test_window_guard_blocks_at_max_stops():
    s = state(max_stops_per_window=2, stop_window_bars=8)
    s.on_exit("stop", 5)
    assert s.entries_allowed(5) == (True, None)  # one stop: under the limit
    s.on_exit("stop", 9)
    allowed, reason = s.entries_allowed(9)
    assert not allowed
    assert reason == "stop_window_guard: 2 stops in last 8 bars (max 2)"


def test_window_guard_releases_when_stops_age_out():
    s = state(max_stops_per_window=2, stop_window_bars=8)
    s.on_exit("stop", 5)
    s.on_exit("stop", 9)
    assert s.entries_allowed(12)[0] is False  # (4, 12] holds both stops
    assert s.entries_allowed(13)[0] is True   # (5, 13] holds only bar 9


def test_window_guard_ignores_non_stop_exits():
    s = state(max_stops_per_window=1, stop_window_bars=100)
    s.on_exit("take_profit", 5)
    s.on_exit("max_holding", 6)
    assert s.entries_allowed(7) == (True, None)
    s.on_exit("stop", 8)
    assert s.entries_allowed(8)[0] is False


def test_combined_block_reports_every_active_reason():
    s = state(cooldown_bars_after_stop=2, max_stops_per_window=1, stop_window_bars=8)
    s.on_exit("stop", 5)
    allowed, reason = s.entries_allowed(5)
    assert not allowed
    assert reason == (
        "post_exit_cooldown: 2 bar(s) remaining; "
        "stop_window_guard: 1 stops in last 8 bars (max 1)"
    )


def test_one_sided_window_guard_config_rejected():
    with pytest.raises(ValidationError):
        ProtectionConfig(max_stops_per_window=2)  # window bars missing
    with pytest.raises(ValidationError):
        ProtectionConfig(stop_window_bars=8)  # max stops missing


def test_config_is_frozen():
    cfg = ProtectionConfig(cooldown_bars_after_stop=1)
    with pytest.raises(ValidationError):
        cfg.cooldown_bars_after_stop = 5


def test_on_exit_never_raises_while_blocked():
    # Exits are NEVER affected by protections — recording an exit while
    # entries are blocked must work unconditionally (reduce-only invariant).
    s = state(cooldown_bars_after_stop=5, max_stops_per_window=1, stop_window_bars=10)
    s.on_exit("stop", 5)
    assert s.entries_allowed(6)[0] is False
    s.on_exit("stop", 6)  # another exit while blocked: fine
    s.on_exit("take_profit", 7)
    assert s.entries_allowed(7)[0] is False


# --- RunnerConfig legacy alias (PR #92 back-compat) --------------------------------


def test_runner_config_alias_maps_into_protections():
    from vnedge.runtime.runner_config import RunnerConfig

    cfg = RunnerConfig(post_exit_cooldown_bars=2)
    assert cfg.effective_protections().cooldown_bars_after_stop == 2

    # stricter of the two cooldown values wins
    cfg = RunnerConfig(
        post_exit_cooldown_bars=1,
        protections=ProtectionConfig(cooldown_bars_after_stop=3),
    )
    assert cfg.effective_protections().cooldown_bars_after_stop == 3

    # window-guard fields survive the alias fold-in
    cfg = RunnerConfig(
        post_exit_cooldown_bars=4,
        protections=ProtectionConfig(max_stops_per_window=2, stop_window_bars=8),
    )
    eff = cfg.effective_protections()
    assert eff.cooldown_bars_after_stop == 4
    assert eff.max_stops_per_window == 2 and eff.stop_window_bars == 8


def test_runner_config_defaults_leave_protections_off():
    from vnedge.runtime.runner_config import RunnerConfig

    assert not RunnerConfig().effective_protections().enabled


# --- Live runner integration -------------------------------------------------------

from vnedge.execution.journal import DecisionJournal  # noqa: E402
from vnedge.execution.order_manager import OrderManager  # noqa: E402
from vnedge.paper.fill_model import FillModel  # noqa: E402
from vnedge.paper.paper_broker import PaperBroker  # noqa: E402
from vnedge.paper.simulated_exchange import SimulatedExchange  # noqa: E402
from vnedge.risk.kill_switch import KillSwitch  # noqa: E402
from vnedge.risk.risk_manager import PreTradeRiskGateway  # noqa: E402
from vnedge.runtime.live_paper import LivePaperSession  # noqa: E402
from vnedge.runtime.runner_config import RunnerConfig, RunnerMode  # noqa: E402
from tests.test_live_paper import (  # noqa: E402
    BASE, MIN, SYM, AlwaysLong, FakeFeed, history,
)


def build_protected_session(tmp_path, feed, *, protections, strategy=None,
                            post_exit_cooldown_bars=0):
    config = RunnerConfig(
        mode=RunnerMode.PAPER, symbol=SYM, reconcile_every_bars=100,
        protections=protections,
        post_exit_cooldown_bars=post_exit_cooldown_bars,
    )
    exchange = SimulatedExchange(FillModel(), config.starting_equity_usd)
    journal = DecisionJournal(tmp_path / "journal.jsonl")
    gateway = PreTradeRiskGateway(config.risk, KillSwitch(kill_file=tmp_path / "KILL"))
    om = OrderManager(gateway, journal, PaperBroker(exchange))
    session = LivePaperSession(
        strategy or AlwaysLong(), feed, history(), config,
        gateway=gateway, order_manager=om, exchange=exchange, journal=journal,
    )
    return session, exchange


def bar(i, *, low=99.5, high=100.5, close=100.0):
    return [BASE + i * MIN, 100.0, high, low, close, 5.0]


def blocked_evals(session):
    return [r["payload"] for r in session.journal.read_all()
            if r["kind"] == "lane_eval" and r["payload"]["skip_reason"]]


async def test_live_protection_blocked_logged_once_per_episode(tmp_path):
    # bar5 entry; bar6 stop (episode 1: bars 6,7 blocked); bar8 re-entry;
    # bar9 stop (episode 2: bar 9 blocked) -> exactly TWO protection_blocked
    # trade_log events despite three blocked evaluations.
    rows = [bar(5), bar(6, low=94.0, close=96.0), bar(7), bar(8),
            bar(9, low=90.0, close=96.0)]
    feed = FakeFeed(rows)
    session, _ = build_protected_session(
        tmp_path, feed,
        protections=ProtectionConfig(cooldown_bars_after_stop=2),
    )

    await session.run(max_bars=5)

    skips = blocked_evals(session)
    assert [s["skip_reason"] for s in skips] == [
        "post_exit_cooldown: 2 bar(s) remaining",
        "post_exit_cooldown: 1 bar(s) remaining",
        "post_exit_cooldown: 2 bar(s) remaining",
    ]
    events = [e["event"] for e in session.trade_log]
    assert events.count("protection_blocked") == 2
    assert events.count("entry_skipped") == 3


async def test_live_stop_window_guard_blocks_entries(tmp_path):
    # two stops inside the window -> entries blocked with the guard's reason,
    # and the guard outlasts any cooldown (cooldown deliberately off here).
    rows = [bar(5), bar(6, low=94.0, close=96.0),  # stop 1 (95.0)
            bar(7, low=91.0, close=92.0),          # stop 2 (91.2) same-bar entry
            bar(8)]
    feed = FakeFeed(rows)
    session, exchange = build_protected_session(
        tmp_path, feed,
        protections=ProtectionConfig(max_stops_per_window=2, stop_window_bars=100),
    )

    await session.run(max_bars=4)

    assert exchange.get_positions() == []  # blocked after the second stop
    skips = blocked_evals(session)
    assert [s["skip_reason"] for s in skips] == [
        "stop_window_guard: 2 stops in last 100 bars (max 2)",
        "stop_window_guard: 2 stops in last 100 bars (max 2)",
    ]
    events = [e["event"] for e in session.trade_log]
    assert events.count("protection_blocked") == 1  # one contiguous episode


# --- Backtester integration --------------------------------------------------------

from vnedge.backtest.backtester import BacktestConfig, run_backtest  # noqa: E402
from vnedge.data.schemas import normalize_candles  # noqa: E402
from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent  # noqa: E402


class FixedStopLong(BaseStrategy):
    """Fires long on every flat bar with a FIXED stop/TP — identical decisions
    regardless of fill prices, so both engines see the same exit sequence."""

    strategy_id = "fixed_stop_long"
    warmup_bars = 2

    def prepare(self, candles):
        return candles.copy()

    def signal(self, df, index):
        return SignalIntent("long", stop_price=95.0, take_profit_price=200.0)


def frame(rows):
    return normalize_candles(rows)


def test_backtest_defaults_off_zero_behavior_change():
    # Default config: a stop still allows the same-bar re-entry decision
    # (filled next open) and the blocked list stays empty — the engine is
    # bit-identical to its pre-protections behavior.
    rows = [bar(i) for i in range(5)] + [bar(5, low=94.0, close=96.0)] \
        + [bar(i) for i in range(6, 9)]
    result = run_backtest(frame(rows), None, FixedStopLong(), BacktestConfig())

    assert result.protection_blocked == ()
    assert not result.config.protections.enabled
    assert [t.exit_reason for t in result.trades] == ["stop", "end_of_data"]
    ts = frame(rows)["timestamp"]
    assert result.trades[0].exit_ts == ts.iloc[5]
    assert result.trades[1].entry_ts == ts.iloc[6]  # re-entry decided ON the stop bar


def test_backtest_cooldown_blocks_reentry_decisions():
    rows = [bar(i) for i in range(5)] + [bar(5, low=94.0, close=96.0)] \
        + [bar(i) for i in range(6, 11)]
    config = BacktestConfig(protections=ProtectionConfig(cooldown_bars_after_stop=2))
    result = run_backtest(frame(rows), None, FixedStopLong(), config)

    ts = frame(rows)["timestamp"]
    assert list(result.protection_blocked) == [
        (ts.iloc[5], "post_exit_cooldown: 2 bar(s) remaining"),
        (ts.iloc[6], "post_exit_cooldown: 1 bar(s) remaining"),
    ]
    # re-entry decided at bar 7, filled at bar 8's open
    assert result.trades[1].entry_ts == ts.iloc[8]


def test_backtest_exits_never_blocked_by_protections():
    # While the window guard is saturated, an open position's stop still
    # closes it — protections gate entries only (reduce-only invariant).
    rows = [bar(i) for i in range(5)] + [bar(5, low=94.0, close=96.0),
                                         bar(6), bar(7, low=94.0, close=96.0)] \
        + [bar(i) for i in range(8, 11)]
    config = BacktestConfig(
        protections=ProtectionConfig(max_stops_per_window=1, stop_window_bars=50)
    )
    result = run_backtest(frame(rows), None, FixedStopLong(), config)

    # guard armed by the FIRST stop; the position opened before it cannot
    # exist (entries blocked), so exactly one stop trade — but crucially the
    # stop exit at bar 5 itself went through while the guard was arming.
    assert [t.exit_reason for t in result.trades] == ["stop"]
    reasons = {r for _, r in result.protection_blocked}
    assert reasons == {"stop_window_guard: 1 stops in last 50 bars (max 1)"}


# --- Engine parity: identical blocked decisions in backtest and live runner --------


PARITY_PROTECTIONS = ProtectionConfig(
    cooldown_bars_after_stop=2, max_stops_per_window=2, stop_window_bars=8
)


def parity_rows():
    """Bars 0..16 @ ~100 with stop-piercing lows (94 < 95) at bars 5 and 9."""
    return [
        bar(i, low=94.0, close=96.0) if i in (5, 9) else bar(i)
        for i in range(17)
    ]


async def test_engine_parity_same_blocked_decisions(tmp_path):
    rows = parity_rows()

    # Backtest: full frame, decisions at close, fills next open.
    result = run_backtest(
        frame(rows), None, FixedStopLong(),
        BacktestConfig(protections=PARITY_PROTECTIONS),
    )
    bt_blocked = [(ts.isoformat(), reason) for ts, reason in result.protection_blocked]

    # Live runner: seed exactly the warmup bars, stream the rest — the first
    # evaluated bar index matches the backtester's loop start.
    feed = FakeFeed([r for r in rows[2:]])
    config = RunnerConfig(
        mode=RunnerMode.PAPER, symbol=SYM, reconcile_every_bars=100,
        tick_stops_enabled=False, protections=PARITY_PROTECTIONS,
    )
    exchange = SimulatedExchange(FillModel(), config.starting_equity_usd)
    journal = DecisionJournal(tmp_path / "journal.jsonl")
    gateway = PreTradeRiskGateway(config.risk, KillSwitch(kill_file=tmp_path / "KILL"))
    om = OrderManager(gateway, journal, PaperBroker(exchange))
    session = LivePaperSession(
        FixedStopLong(), feed, frame(rows[:2]), config,
        gateway=gateway, order_manager=om, exchange=exchange, journal=journal,
    )
    await session.run(max_bars=15)
    live_blocked = [
        (s["bar_ts"], s["skip_reason"]) for s in blocked_evals(session)
    ]

    assert bt_blocked == live_blocked
    # the sequence exercises cooldown-only, combined, and guard-only blocks
    expected_ts = frame(rows)["timestamp"]
    assert bt_blocked == [
        (expected_ts.iloc[5].isoformat(), "post_exit_cooldown: 2 bar(s) remaining"),
        (expected_ts.iloc[6].isoformat(), "post_exit_cooldown: 1 bar(s) remaining"),
        (expected_ts.iloc[9].isoformat(),
         "post_exit_cooldown: 2 bar(s) remaining; "
         "stop_window_guard: 2 stops in last 8 bars (max 2)"),
        (expected_ts.iloc[10].isoformat(),
         "post_exit_cooldown: 1 bar(s) remaining; "
         "stop_window_guard: 2 stops in last 8 bars (max 2)"),
        (expected_ts.iloc[11].isoformat(),
         "stop_window_guard: 2 stops in last 8 bars (max 2)"),
        (expected_ts.iloc[12].isoformat(),
         "stop_window_guard: 2 stops in last 8 bars (max 2)"),
    ]
    # both engines re-entered after release: two stops then a live position
    assert [t.exit_reason for t in result.trades[:2]] == ["stop", "stop"]
    assert len(exchange.get_positions()) == 1


async def test_live_take_profit_exit_does_not_arm_cooldown(tmp_path):
    # Refinement vs PR #92's any-exit cooldown: a take-profit exit leaves the
    # very same bar free to re-enter even with the legacy alias enabled.
    rows = [bar(5), bar(6, high=110.5, close=104.0)]  # TP 110 hit at bar 6
    feed = FakeFeed(rows)
    session, exchange = build_protected_session(
        tmp_path, feed,
        protections=ProtectionConfig(),
        post_exit_cooldown_bars=1,  # legacy alias, now stop-only
    )

    report = await session.run(max_bars=2)

    exits = [r["payload"] for r in session.journal.read_all()
             if r["kind"] == "live_paper_exit"]
    assert [e["reason"] for e in exits] == ["take_profit"]
    assert blocked_evals(session) == []          # no cooldown skip journaled
    assert report.signals_generated == 2         # bar 5 entry + bar 6 re-entry
    assert len(exchange.get_positions()) == 1    # re-entered on the TP bar
