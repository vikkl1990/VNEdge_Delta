"""Live paper session — deterministic tests via a fake feed."""

import asyncio
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from vnedge.data.schemas import normalize_candles
from vnedge.execution.journal import DecisionJournal
from vnedge.execution.order_manager import OrderManager
from vnedge.paper.fill_model import FillModel
from vnedge.paper.paper_reconciliation import ReconciliationReport
from vnedge.paper.paper_broker import PaperBroker
from vnedge.paper.simulated_exchange import PaperOrderRequest, SimulatedExchange
from vnedge.risk.kill_switch import KillSwitch
from vnedge.risk.risk_manager import MarketState, PreTradeRiskGateway
from vnedge.runtime.daily_factory import DailySignalFactoryConfig
from vnedge.runtime.live_paper import LivePaperSession, _extract_strategy_thresholds
from vnedge.runtime.runner_config import RunnerConfig, RunnerMode
from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent
from vnedge.strategy.quant_signal_pack import QuantSignalPack

BASE = 1_750_000_000_000
MIN = 60_000
SYM = "BTC/USDT:USDT"


class FakeFeed:
    """Same surface as LiveMarketFeed, scripted content, no network."""

    exchange_id = "fake"

    def __init__(self, rows, quote=(99.99, 100.01), stale: bool = False):
        self.closed_candles = asyncio.Queue()
        for row in rows:
            self.closed_candles.put_nowait(row)
        self.quote = quote
        self.funding_rate = 0.0001
        self.stale = stale
        self.healthy = True

    def staleness_seconds(self, now=None):
        return 9_999.0 if self.stale else 0.5

    def market_state(self) -> MarketState:
        last = datetime.now(UTC) - (
            timedelta(hours=3) if self.stale else timedelta(milliseconds=100)
        )
        bid, ask = self.quote
        return MarketState(
            symbol=SYM, last_update=last,
            spread_bps=(ask - bid) / ((ask + bid) / 2) * 10_000,
            estimated_slippage_bps=2.0, funding_rate=self.funding_rate,
            exchange_healthy=self.healthy,
        )


class AlwaysLong(BaseStrategy):
    strategy_id = "always_long"
    warmup_bars = 2

    def prepare(self, candles):
        return candles.copy()

    def signal(self, df, index):
        close = float(df["close"].iloc[index])
        return SignalIntent("long", stop_price=close * 0.95,
                            take_profit_price=close * 1.10)


class LadderLong(AlwaysLong):
    strategy_id = "ladder_long"

    def signal(self, df, index):
        close = float(df["close"].iloc[index])
        return SignalIntent(
            "long",
            stop_price=close * 0.95,
            take_profit_price=close * 1.10,
            take_profit_levels=(close * 1.03, close * 1.06, close * 1.10),
            reason="ladder plan",
        )


def test_eval_threshold_extraction_reads_frozen_strategy_params():
    strategy = QuantSignalPack(
        None,
        min_score=6.0,
        min_score_delta=1.25,
        min_volume_z=0.55,
    )

    thresholds = _extract_strategy_thresholds(
        strategy, ("min_score", "min_score_delta", "min_volume_z")
    )

    assert thresholds == {
        "min_score": 6.0,
        "min_score_delta": 1.25,
        "min_volume_z": 0.55,
    }


def history(n=5) -> pd.DataFrame:
    return normalize_candles(
        [[BASE + i * MIN, 100.0, 100.5, 99.5, 100.0, 5.0] for i in range(n)]
    )


def live_rows(start=5, n=3, low=99.5, high=100.5):
    return [[BASE + (start + i) * MIN, 100.0, high, low, 100.0, 5.0] for i in range(n)]


def timed_rows(start: str, offsets: tuple[int, ...], low=99.5, high=100.5):
    base = pd.Timestamp(start, tz="UTC")
    return [
        [
            int((base + pd.Timedelta(minutes=offset)).timestamp() * 1000),
            100.0,
            high,
            low,
            100.0,
            5.0,
        ]
        for offset in offsets
    ]


def build_session(tmp_path, feed, strategy=None, script=None, mode=RunnerMode.PAPER,
                  tick_stops_enabled=True, post_exit_cooldown_bars=1,
                  trial_meta=None, daily_factory=None, max_holding_bars=48):
    config = RunnerConfig(mode=mode, symbol=SYM, reconcile_every_bars=2,
                          tick_stops_enabled=tick_stops_enabled,
                          post_exit_cooldown_bars=post_exit_cooldown_bars,
                          max_holding_bars=max_holding_bars,
                          daily_factory=daily_factory or DailySignalFactoryConfig())
    exchange = SimulatedExchange(FillModel(), config.starting_equity_usd)
    journal = DecisionJournal(tmp_path / "journal.jsonl")
    kill = KillSwitch(kill_file=tmp_path / "KILL")
    gateway = PreTradeRiskGateway(config.risk, kill)
    om = OrderManager(gateway, journal, PaperBroker(exchange, script=script))
    session = LivePaperSession(
        strategy or AlwaysLong(), feed, history(), config,
        gateway=gateway, order_manager=om, exchange=exchange, journal=journal,
        trial_meta=trial_meta,
    )
    return session, exchange


async def test_closed_candle_triggers_full_pipeline(tmp_path):
    feed = FakeFeed(live_rows(n=1))
    session, exchange = build_session(tmp_path, feed)
    report = await session.run(max_bars=1)
    assert report.bars_processed == 1
    assert report.signals_generated == 1
    assert report.orders_submitted == 1
    assert len(exchange.get_positions()) == 1  # filled at live quote
    fill = exchange.get_fills()[0]
    assert fill.price == pytest.approx(100.01 * (1 + 2 / 10_000))  # ask + slippage


async def test_stale_feed_blocks_entries(tmp_path):
    feed = FakeFeed(live_rows(n=1), stale=True)
    session, exchange = build_session(tmp_path, feed)
    report = await session.run(max_bars=1)
    assert report.signals_generated == 1
    assert report.risk_rejects == 1  # data_freshness failed at the gateway
    assert exchange.get_positions() == []


async def test_shadow_live_evaluates_and_journals_without_submission(tmp_path):
    feed = FakeFeed(live_rows(n=1))
    session, exchange = build_session(tmp_path, feed, mode=RunnerMode.SHADOW)

    report = await session.run(max_bars=1)

    assert report.mode == "shadow_live"
    # startup prime (latest seeded bar) + one forward bar = two shadow intents
    assert report.signals_generated == 2
    assert report.shadow_approved == 2
    assert report.orders_submitted == 0
    assert report.fills == 0
    assert exchange.get_positions() == []
    records = [r for r in session.journal.read_all() if r["kind"] == "shadow_intent"]
    assert len(records) == 2
    assert all(r["payload"]["approved"] is True for r in records)


async def test_shadow_prime_fires_on_startup_without_a_new_bar(tmp_path):
    # no new candles arrive; the seeded latest bar is armed (AlwaysLong).
    # a restart must NOT discard that — the startup prime journals it.
    feed = FakeFeed([])
    session, exchange = build_session(tmp_path, feed, mode=RunnerMode.SHADOW)

    report = await session.run(max_bars=0)

    records = [r for r in session.journal.read_all() if r["kind"] == "shadow_intent"]
    assert len(records) == 1          # primed off the seeded bar, zero new bars
    assert report.shadow_approved == 1
    assert report.orders_submitted == 0
    assert report.fills == 0
    assert exchange.get_positions() == []


async def test_paper_mode_is_not_primed_on_startup(tmp_path):
    # paper/live must never re-enter the latest bar on restart (double-position)
    feed = FakeFeed([])
    session, exchange = build_session(tmp_path, feed, mode=RunnerMode.PAPER)

    await session.run(max_bars=0)

    assert session.orders_submitted == 0      # nothing submitted from a prime
    assert exchange.get_positions() == []


async def test_paper_runner_heartbeats_even_without_signals_or_new_bars(tmp_path):
    feed = FakeFeed([])
    session, exchange = build_session(tmp_path, feed, mode=RunnerMode.PAPER)

    await session.run(max_bars=0)

    assert session.orders_submitted == 0
    assert exchange.get_positions() == []
    heartbeats = [
        r["payload"]
        for r in session.journal.read_all()
        if r["kind"] == "paper_lane_heartbeat"
    ]
    assert len(heartbeats) == 1
    assert heartbeats[0]["reason"] == "runner_started"
    assert heartbeats[0]["mode"] == "paper"
    assert heartbeats[0]["strategy_id"] == "always_long"
    assert heartbeats[0]["symbol"] == SYM
    assert heartbeats[0]["evals"] == 0
    assert heartbeats[0]["orders_submitted"] == 0
    assert heartbeats[0]["why_no_trade"] == "runner_started"


async def test_paper_observation_prime_journals_without_restart_order(tmp_path):
    feed = FakeFeed([])
    session, exchange = build_session(
        tmp_path,
        feed,
        mode=RunnerMode.PAPER,
        trial_meta={"trial_id": "always_long_paper_observation"},
    )

    await session.run(max_bars=0)

    assert session.orders_submitted == 0
    assert session.live_signals == 1
    assert session.signals == 0
    assert exchange.get_positions() == []
    evals = [r["payload"] for r in session.journal.read_all() if r["kind"] == "lane_eval"]
    assert len(evals) == 3
    assert [e["backfill"] for e in evals] == [True, True, False]
    assert evals[-1]["fired"] is True
    assert evals[-1]["skip_reason"] == "paper_observation_prime: no restart order submitted"


async def test_non_forward_candles_dropped(tmp_path):
    stale_row = [[BASE + 2 * MIN, 100.0, 100.5, 99.5, 100.0, 5.0]]  # inside history
    feed = FakeFeed(stale_row + live_rows(n=1))
    session, exchange = build_session(tmp_path, feed)
    report = await session.run(max_bars=1)
    assert session.dropped_candles == 1
    assert report.bars_processed == 1  # only the valid candle counted


async def test_daily_factory_entry_cutoff_blocks_live_paper_entries(tmp_path):
    feed = FakeFeed(live_rows(n=1))
    session, exchange = build_session(
        tmp_path,
        feed,
        daily_factory=DailySignalFactoryConfig(
            enabled=True,
            entry_cutoff_minute=0,
            force_flatten_minute=1439,
        ),
    )

    report = await session.run(max_bars=1)

    assert report.signals_generated == 0
    assert report.orders_submitted == 0
    assert exchange.get_positions() == []
    evals = [r["payload"] for r in session.journal.read_all() if r["kind"] == "lane_eval"]
    assert evals[-1]["skip_reason"].startswith("daily_factory_entry_cutoff")


async def test_daily_factory_force_closes_live_paper_position(tmp_path):
    feed = FakeFeed(timed_rows("2026-08-02T20:00:00Z", (0, 30)))
    session, exchange = build_session(
        tmp_path,
        feed,
        strategy=LongOnce(),
        daily_factory=DailySignalFactoryConfig(
            enabled=True,
            entry_cutoff_minute=20 * 60 + 15,
            force_flatten_minute=20 * 60 + 30,
        ),
    )

    report = await session.run(max_bars=2)

    assert report.orders_submitted == 2
    assert report.fills == 2
    assert exchange.get_positions() == []
    exits = [
        r["payload"]
        for r in session.journal.read_all()
        if r["kind"] == "live_paper_exit"
    ]
    assert exits[-1]["reason"] == "daily_factory_close"


class LongOnce(AlwaysLong):
    strategy_id = "long_once"

    def __init__(self):
        self.fired = False

    def signal(self, df, index):
        if self.fired:
            return None
        self.fired = True
        return super().signal(df, index)


class LadderLongOnce(LongOnce):
    strategy_id = "ladder_long_once"

    def signal(self, df, index):
        if self.fired:
            return None
        self.fired = True
        close = float(df["close"].iloc[index])
        return SignalIntent(
            "long",
            stop_price=close * 0.95,
            take_profit_price=close * 1.06,
            take_profit_levels=(close * 1.02, close * 1.04, close * 1.06),
            reason="test ladder",
        )


async def test_stop_exit_on_live_bar(tmp_path):
    # bar 1 opens position; bar 2's low pierces the 95 stop
    rows = live_rows(n=1) + [[BASE + 6 * MIN, 100.0, 100.2, 94.0, 96.0, 5.0]]
    feed = FakeFeed(rows)
    session, exchange = build_session(tmp_path, feed, strategy=LongOnce())
    report = await session.run(max_bars=2)
    assert exchange.get_positions() == []  # stopped out, flat
    assert report.fills == 2
    assert report.realized_pnl_usd < 0


async def test_max_holding_times_out_paper_position(tmp_path):
    # Parity with the backtester/shadow: a position that never hits its stop (95)
    # or TP (106) must time out at max_holding_bars. Enter on bar 1, then feed
    # three quiet held bars; with max_holding_bars=3 the third forces the exit.
    rows = live_rows(n=1) + live_rows(start=6, n=3)
    feed = FakeFeed(rows)
    session, exchange = build_session(
        tmp_path, feed, strategy=LongOnce(),
        max_holding_bars=3, post_exit_cooldown_bars=0,
    )
    await session.run(max_bars=4)
    assert exchange.get_positions() == []  # timed out, flat
    exits = [
        r["payload"]
        for r in session.journal.read_all()
        if r["kind"] == "live_paper_exit"
    ]
    assert any(e["reason"] == "max_holding" for e in exits), exits


async def test_position_held_below_max_holding_stays_open(tmp_path):
    # The cap must not fire early: two quiet held bars under a cap of 3 keep the
    # position open (guards against an off-by-one that would exit too soon).
    rows = live_rows(n=1) + live_rows(start=6, n=2)
    feed = FakeFeed(rows)
    session, exchange = build_session(
        tmp_path, feed, strategy=LongOnce(),
        max_holding_bars=3, post_exit_cooldown_bars=0,
    )
    await session.run(max_bars=3)
    assert len(exchange.get_positions()) == 1  # still holding


async def test_live_paper_ladder_partial_arms_breakeven(tmp_path):
    rows = [
        [BASE + 5 * MIN, 100.0, 100.5, 99.5, 100.0, 5.0],
        [BASE + 6 * MIN, 100.0, 103.0, 99.5, 102.5, 5.0],
        [BASE + 7 * MIN, 102.5, 103.0, 100.0, 100.5, 5.0],
    ]
    feed = FakeFeed(rows)
    session, exchange = build_session(tmp_path, feed, strategy=LadderLongOnce())

    report = await session.run(max_bars=3)

    assert report.orders_submitted == 3
    assert report.fills == 3
    assert exchange.get_positions() == []
    exits = [
        r["payload"]
        for r in session.journal.read_all()
        if r["kind"] == "live_paper_exit"
    ]
    assert [e["reason"] for e in exits] == ["tp1_partial", "breakeven_stop"]
    assert exits[0]["final"] is False
    assert exits[1]["final"] is True
    assert exits[1]["active_stop_price"] > 100.0
    assert report.reconciliation_mismatches == 0


async def test_post_exit_cooldown_blocks_same_bar_reentry(tmp_path):
    rows = live_rows(n=1) + [[BASE + 6 * MIN, 100.0, 100.2, 94.0, 96.0, 5.0]]
    feed = FakeFeed(rows)
    session, exchange = build_session(tmp_path, feed, strategy=AlwaysLong())

    report = await session.run(max_bars=2)

    assert exchange.get_positions() == []
    assert report.orders_submitted == 2  # entry + stop, no same-bar re-entry
    assert report.signals_generated == 1
    evals = [r["payload"] for r in session.journal.read_all() if r["kind"] == "lane_eval"]
    assert evals[-1]["fired"] is False
    assert evals[-1]["skip_reason"] == "post_exit_cooldown: 1 bar(s) remaining"
    assert "entry_skipped" in [row["event"] for row in session.trade_log]


async def test_zero_post_exit_cooldown_keeps_legacy_reentry_behavior(tmp_path):
    rows = live_rows(n=1) + [[BASE + 6 * MIN, 100.0, 100.2, 94.0, 96.0, 5.0]]
    feed = FakeFeed(rows)
    session, exchange = build_session(
        tmp_path, feed, strategy=AlwaysLong(), post_exit_cooldown_bars=0
    )

    report = await session.run(max_bars=2)

    assert len(exchange.get_positions()) == 1
    assert report.orders_submitted == 3  # entry + stop + immediate re-entry
    assert report.signals_generated == 2


async def test_timeout_reached_entry_activates_plan_after_reconciliation(tmp_path):
    rows = live_rows(n=1) + [[BASE + 6 * MIN, 100.0, 100.2, 94.0, 96.0, 5.0]]
    feed = FakeFeed(rows)
    session, exchange = build_session(
        tmp_path, feed, strategy=LongOnce(), script=["timeout_reached"]
    )

    report = await session.run(max_bars=2)

    assert report.orders_submitted == 2  # timed-out entry + reduce-only stop
    assert report.fills == 2
    assert exchange.get_positions() == []
    assert report.reconciliation_mismatches == 0


async def test_restored_orphan_position_trips_kill_and_blocks_entries(tmp_path):
    feed = FakeFeed(live_rows(n=1))
    session, exchange = build_session(tmp_path, feed, strategy=AlwaysLong())
    exchange.set_quote(SYM, bid=99.99, ask=100.01)
    exchange.submit_order(PaperOrderRequest("restored", SYM, True, 1.0))
    existing_fills = len(exchange.get_fills())

    report = await session.run(max_bars=1)

    assert session.gateway.kill_switch.is_active
    assert report.risk_rejects == 1
    assert len(exchange.get_fills()) == existing_fills
    kinds = [r["kind"] for r in session.journal.read_all()]
    assert "orphaned_paper_position" in kinds


def test_reconciliation_mismatch_trips_live_session_fail_closed_once(tmp_path):
    feed = FakeFeed([])
    session, _ = build_session(tmp_path, feed)

    session.reconciler.run = lambda: ReconciliationReport((), ("internal vs venue",))
    session._reconcile()
    session._reconcile()

    assert session.gateway.kill_switch.is_active
    records = [
        r for r in session.journal.read_all()
        if r["kind"] == "reconciliation_fail_closed"
    ]
    assert len(records) == 1
    assert records[0]["payload"]["mismatches"] == ["internal vs venue"]


async def test_lane_eval_journaled_for_every_evaluated_bar(tmp_path):
    feed = FakeFeed(live_rows(n=2))
    session, _ = build_session(tmp_path, feed)
    await session.run(max_bars=2)

    evals = [r for r in session.journal.read_all() if r["kind"] == "lane_eval"]
    # paper mode: no prime; bar 1 evaluated (fires, opens a plan), bar 2 is
    # in-position -> entry evaluation is skipped by design (exit mgmt runs)
    assert len(evals) == 1
    for r in evals:
        assert r["payload"]["fired"] is True  # AlwaysLong fires every bar
        assert r["payload"]["backfill"] is False
        assert r["payload"]["strategy_id"] == "always_long"
        assert r["payload"]["exchange"] == "fake"
        assert r["payload"]["timeframe"] == "1h"
        assert r["payload"]["signal"]["side"] == "long"
        assert r["payload"]["signal"]["take_profit_levels"] == []
        assert "features" in r["payload"] and "thresholds" in r["payload"]
    # the newest evaluation is surfaced for the dashboard snapshot
    assert session.last_eval is not None
    assert session.last_eval["fired"] is True


async def test_shadow_prime_backfills_observability_records(tmp_path):
    # 5 seeded bars, warmup 2 -> bars 2,3 backfill + bar 4 live prime
    feed = FakeFeed([])
    session, exchange = build_session(tmp_path, feed, mode=RunnerMode.SHADOW)
    await session.run(max_bars=0)

    evals = [r["payload"] for r in session.journal.read_all()
             if r["kind"] == "lane_eval"]
    assert len(evals) == 3
    assert [e["backfill"] for e in evals] == [True, True, False]
    assert all(e["fired"] for e in evals)     # AlwaysLong
    # backfilled bars journal observations ONLY — a single intent, latest bar
    intents = [r for r in session.journal.read_all() if r["kind"] == "shadow_intent"]
    assert len(intents) == 1
    assert exchange.get_positions() == []
    # last_eval reflects the latest (non-backfill) bar
    assert session.last_eval is not None and session.last_eval["backfill"] is False


async def test_fills_are_chained_into_the_ledger(tmp_path):
    from vnedge.execution.fill_ledger import FillLedger, verify_chain

    feed = FakeFeed(live_rows(n=1))
    session, exchange = build_session(tmp_path, feed)
    session.fill_ledger = FillLedger(tmp_path / "fills.jsonl")
    await session.run(max_bars=1)

    assert len(exchange.get_fills()) == 1
    report = verify_chain(tmp_path / "fills.jsonl")
    assert report.ok and report.records == 1
    rec = __import__("json").loads((tmp_path / "fills.jsonl").read_text())
    assert rec["symbol"] == SYM and rec["mode"] == "paper"
    assert rec["strategy_id"] == "always_long"


async def test_trade_log_narrates_signal_to_verdict(tmp_path):
    feed = FakeFeed(live_rows(n=1))
    session, _ = build_session(tmp_path, feed, mode=RunnerMode.SHADOW)
    await session.run(max_bars=1)

    events = [e["event"] for e in session.trade_log]
    # prime fires + live bar fires; each approved by the gateway in shadow
    assert events.count("signal_fired") == 2
    assert events.count("shadow_approved") == 2
    assert all("ts" in e and "detail" in e for e in session.trade_log)


async def test_trade_log_records_fills_in_paper(tmp_path):
    from vnedge.execution.fill_ledger import FillLedger

    feed = FakeFeed(live_rows(n=1))
    session, exchange = build_session(tmp_path, feed)
    session.fill_ledger = FillLedger(tmp_path / "fills.jsonl")
    await session.run(max_bars=1)

    events = [e["event"] for e in session.trade_log]
    assert "order_submitted" in events
    assert "fill" in events


async def test_plan_survives_restart_via_account_store(tmp_path):
    from vnedge.paper.account_store import PaperAccountStore

    # session 1: trade opens, plan saved with the account
    feed = FakeFeed(live_rows(n=1))
    session, exchange = build_session(tmp_path, feed, strategy=LadderLong())
    session.account_store = PaperAccountStore(tmp_path / "acct.json", "t1")
    await session.run(max_bars=1)
    assert session._plan is not None
    stored = session.account_store.load()
    assert stored["plan"]["side"] == "long"
    assert stored["plan"]["stop_price"] == session._plan.signal.stop_price
    assert stored["plan"]["take_profit_levels"] == list(session._plan.signal.take_profit_levels)

    # session 2 (restart): restore -> plan re-armed, orphan guard NOT tripped
    feed2 = FakeFeed([])
    session2, exchange2 = build_session(tmp_path, feed2)
    session2.account_store = PaperAccountStore(tmp_path / "acct.json", "t1")
    resumed = session2.account_store.restore_into(exchange2, session2.tracker)
    assert resumed and exchange2.get_positions()
    session2.restore_plan(session2.account_store.load().get("plan"))
    assert session2._plan is not None
    assert session2._plan.signal.take_profit_levels == tuple(stored["plan"]["take_profit_levels"])
    await session2.run(max_bars=0)
    assert not session2.gateway.kill_switch.is_active  # no orphan trip
    records = [r["kind"] for r in session2.journal.read_all()]
    assert "orphaned_paper_position" not in records


async def test_legacy_snapshot_without_plan_synthesizes_for_funding_mr(tmp_path):
    from vnedge.data.schemas import normalize_funding
    from vnedge.strategy.funding_mean_reversion import FundingMeanReversion

    funding = normalize_funding(
        [{"timestamp": BASE - i * 8 * 60 * MIN, "fundingRate": 0.0001} for i in range(40)][::-1]
    )
    strat = FundingMeanReversion(funding, funding_pct_window=24, z_window=8)
    hist = normalize_candles(
        [[BASE + i * MIN, 100.0, 100.5, 99.5, 100.0 + 0.01 * i, 5.0] for i in range(400)]
    )
    feed = FakeFeed([])
    session, exchange = build_session(tmp_path, feed, strategy=strat, mode=RunnerMode.PAPER)
    session.candles = hist
    # legacy restore: position exists, no plan stored
    exchange.set_quote(SYM, 100.0, 100.1)
    from vnedge.paper.simulated_exchange import PaperOrderRequest
    exchange.submit_order(PaperOrderRequest("legacy", SYM, False, 0.5))
    session.restore_plan(None)
    assert session._plan is not None
    assert session._plan.signal.side == "short"
    assert session._plan.signal.stop_price > 100.0     # stop above short entry
    kinds = [r["kind"] for r in session.journal.read_all()]
    assert "plan_rebuilt_on_resume" in kinds


# --- Tick-level stop monitoring ---------------------------------------------------
# Stops get quote granularity between bar closes; take-profits stay bar-close.


async def run_idle_ticks(session, seconds=0.15):
    """Drive the run loop's idle (TimeoutError) branch with a shrunken tick."""
    session._IDLE_TICK_SECONDS = 0.01
    await session.run(deadline_seconds=seconds)


async def test_tick_stop_breach_exits_between_bars(tmp_path):
    feed = FakeFeed(live_rows(n=1))
    session, exchange = build_session(tmp_path, feed, strategy=LongOnce())
    await session.run(max_bars=1)
    assert session._plan is not None
    assert len(exchange.get_positions()) == 1
    stop = session._plan.signal.stop_price  # 100 * 0.95 = 95

    feed.quote = (94.0, 94.02)  # bid pierces the stop between bars
    await run_idle_ticks(session)

    assert exchange.get_positions() == []      # flat — stopped out on the tick
    assert session._plan is None               # plan cleared, entries re-enabled
    assert session.tick_stop_exits == 1
    fills = exchange.get_fills()
    assert len(fills) == 2
    exit_fill = fills[-1]
    assert not exit_fill.buy
    # filled at the BREACH quote (bid - slippage), not the last bar's close
    assert exit_fill.price == pytest.approx(94.0 * (1 - 2 / 10_000))
    records = [r for r in session.journal.read_all() if r["kind"] == "tick_stop_exit"]
    assert len(records) == 1
    payload = records[0]["payload"]
    assert payload["side"] == "long"
    assert payload["stop_price"] == pytest.approx(stop)
    assert payload["bid"] == 94.0 and payload["ask"] == 94.02
    assert payload["state"] == "acknowledged"
    # the exit went through the FULL OrderManager pipeline as reduce-only
    intents = [r for r in session.journal.read_all() if r["kind"] == "order_intent"]
    assert intents[-1]["payload"]["intent"]["reduce_only"] is True
    assert intents[-1]["payload"]["intent_key"].startswith(f"exit|{SYM}|tick_stop|")


async def test_stale_feed_warns_but_tick_stop_reduce_only_exits(tmp_path):
    feed = FakeFeed(live_rows(n=1))
    session, exchange = build_session(tmp_path, feed, strategy=LongOnce())
    await session.run(max_bars=1)

    feed.stale = True
    feed.quote = (94.0, 94.02)
    await session._check_tick_stop(datetime.now(UTC))

    assert exchange.get_positions() == []
    assert session._plan is None
    records = [r for r in session.journal.read_all() if r["kind"] == "risk_decision"]
    exit_decision = records[-1]["payload"]
    assert exit_decision["approved"] is True
    assert any("data_freshness" in w for w in exit_decision["warning_checks"])


async def test_rejected_tick_stop_preserves_plan_then_retries_with_new_key(tmp_path):
    feed = FakeFeed(live_rows(n=1))
    session, exchange = build_session(
        tmp_path,
        feed,
        strategy=LongOnce(),
        script=["ok", "reject:venue down"],
    )
    await session.run(max_bars=1)

    feed.quote = (94.0, 94.02)
    await session._check_tick_stop(datetime.now(UTC))

    assert len(exchange.get_positions()) == 1
    assert session._plan is not None
    assert session.tick_stop_exits == 0
    preserved = [r for r in session.journal.read_all() if r["kind"] == "exit_plan_preserved"]
    assert preserved[-1]["payload"]["state"] == "rejected"

    await session._check_tick_stop(datetime.now(UTC))

    assert exchange.get_positions() == []
    assert session._plan is None
    intents = [r["payload"]["intent_key"] for r in session.journal.read_all()
               if r["kind"] == "order_intent"]
    assert intents[-2].startswith(f"exit|{SYM}|tick_stop|")
    assert intents[-1] == intents[-2] + "|retry=1"


async def test_timeout_lost_tick_stop_preserves_until_reconcile_then_retries(tmp_path):
    feed = FakeFeed(live_rows(n=1))
    session, exchange = build_session(
        tmp_path,
        feed,
        strategy=LongOnce(),
        script=["ok", "timeout_lost"],
    )
    await session.run(max_bars=1)

    feed.quote = (94.0, 94.02)
    await session._check_tick_stop(datetime.now(UTC))

    assert len(exchange.get_positions()) == 1
    assert session._plan is not None
    assert session.om.has_unresolved_orders
    await session._check_tick_stop(datetime.now(UTC))
    exit_intents = [r for r in session.journal.read_all()
                    if r["kind"] == "order_intent"
                    and r["payload"]["intent"]["reduce_only"]]
    assert len(exit_intents) == 1

    session._reconcile()
    await session._check_tick_stop(datetime.now(UTC))

    assert exchange.get_positions() == []
    assert session._plan is None
    intents = [r["payload"]["intent_key"] for r in session.journal.read_all()
               if r["kind"] == "order_intent"
               and r["payload"]["intent"]["reduce_only"]]
    assert intents[-1] == intents[-2] + "|retry=1"


async def test_tick_quote_without_breach_does_not_exit(tmp_path):
    feed = FakeFeed(live_rows(n=1))
    session, exchange = build_session(tmp_path, feed, strategy=LongOnce())
    await session.run(max_bars=1)

    feed.quote = (95.5, 95.52)  # drawdown, but still above the 95 stop
    await run_idle_ticks(session, seconds=0.1)

    assert len(exchange.get_positions()) == 1  # still in the trade
    assert session._plan is not None
    assert len(exchange.get_fills()) == 1      # entry fill only
    assert session.tick_stop_exits == 0
    assert not [r for r in session.journal.read_all() if r["kind"] == "tick_stop_exit"]


async def test_tick_stops_disabled_keeps_bar_close_behavior(tmp_path):
    feed = FakeFeed(live_rows(n=1))
    session, exchange = build_session(
        tmp_path, feed, strategy=LongOnce(), tick_stops_enabled=False
    )
    await session.run(max_bars=1)

    feed.quote = (94.0, 94.02)  # breaches the stop, but tick stops are off
    await run_idle_ticks(session, seconds=0.1)
    assert len(exchange.get_positions()) == 1  # untouched between bars
    assert session._plan is not None
    assert not [r for r in session.journal.read_all() if r["kind"] == "tick_stop_exit"]

    # the NEXT closed bar still stops out — the pre-existing bar-close path
    feed.closed_candles.put_nowait([BASE + 6 * MIN, 100.0, 100.2, 94.0, 96.0, 5.0])
    await session.run(max_bars=1)
    assert exchange.get_positions() == []
    kinds = [r["kind"] for r in session.journal.read_all()]
    assert "live_paper_exit" in kinds


async def test_no_double_exit_when_next_bar_also_breaches(tmp_path):
    feed = FakeFeed(live_rows(n=1))
    session, exchange = build_session(tmp_path, feed, strategy=LongOnce())
    await session.run(max_bars=1)

    feed.quote = (94.0, 94.02)
    await run_idle_ticks(session)
    assert exchange.get_positions() == []
    assert session.orders_submitted == 2       # entry + tick stop

    # the following bar shows the same breach inside its OHLC range
    feed.closed_candles.put_nowait([BASE + 6 * MIN, 94.0, 94.5, 93.5, 94.0, 5.0])
    await session.run(max_bars=1)

    assert session.orders_submitted == 2       # no second exit submitted
    assert len(exchange.get_fills()) == 2      # entry + single exit
    assert exchange.get_positions() == []
    kinds = [r["kind"] for r in session.journal.read_all()]
    assert kinds.count("tick_stop_exit") == 1
    assert "live_paper_exit" not in kinds


async def test_tick_stop_short_side_triggers_on_ask(tmp_path):
    from vnedge.runtime.live_paper import _LivePlan

    feed = FakeFeed([])
    session, exchange = build_session(tmp_path, feed)
    exchange.set_quote(SYM, 99.99, 100.01)
    exchange.submit_order(PaperOrderRequest("seed-short", SYM, False, 1.0))
    sig = SignalIntent("short", stop_price=105.0, take_profit_price=90.0)
    session._plan = _LivePlan(sig, pd.Timestamp(BASE, unit="ms", tz="UTC"))

    feed.quote = (105.3, 105.4)  # ask pierces the short's stop
    await session._check_tick_stop(datetime.now(UTC))

    assert exchange.get_positions() == []
    assert session._plan is None
    records = [r for r in session.journal.read_all() if r["kind"] == "tick_stop_exit"]
    assert len(records) == 1
    assert records[0]["payload"]["side"] == "short"
    assert records[0]["payload"]["ask"] == 105.4


async def test_shadow_lane_unaffected_by_tick_stops(tmp_path):
    feed = FakeFeed(live_rows(n=1))
    session, exchange = build_session(tmp_path, feed, mode=RunnerMode.SHADOW)
    await session.run(max_bars=1)
    assert session._plan is None               # shadow never arms a plan

    feed.quote = (10.0, 10.02)  # would breach any long stop if a plan existed
    await run_idle_ticks(session, seconds=0.1)

    assert exchange.get_positions() == []
    assert session.orders_submitted == 0
    assert session.tick_stop_exits == 0
    assert not [r for r in session.journal.read_all() if r["kind"] == "tick_stop_exit"]


async def test_tick_stop_mode_guard_holds_even_with_forced_shadow_plan(tmp_path):
    # belt and braces: even if a plan were ever armed in shadow by mistake,
    # the explicit mode guard keeps tick stops from submitting anything
    from vnedge.runtime.live_paper import _LivePlan

    feed = FakeFeed([])
    session, exchange = build_session(tmp_path, feed, mode=RunnerMode.SHADOW)
    sig = SignalIntent("long", stop_price=95.0, take_profit_price=110.0)
    session._plan = _LivePlan(sig, pd.Timestamp(BASE, unit="ms", tz="UTC"))
    feed.quote = (94.0, 94.02)

    await session._check_tick_stop(datetime.now(UTC))

    assert session.orders_submitted == 0
    assert session._plan is not None           # untouched
    assert not [r for r in session.journal.read_all() if r["kind"] == "tick_stop_exit"]


async def test_tick_stop_persists_account_state_immediately(tmp_path):
    from vnedge.paper.account_store import PaperAccountStore

    feed = FakeFeed(live_rows(n=1))
    session, exchange = build_session(tmp_path, feed, strategy=LongOnce())
    session.account_store = PaperAccountStore(tmp_path / "acct.json", "t1")
    await session.run(max_bars=1)
    assert session.account_store.load()["plan"] is not None

    feed.quote = (94.0, 94.02)
    await run_idle_ticks(session)

    # a crash before the next bar must not restore the closed position/plan
    stored = session.account_store.load()
    assert stored["plan"] is None
    assert stored["positions"] == []


async def test_strategy_without_synthesis_still_orphans(tmp_path):
    # AlwaysLong has no synthesize_exit_plan -> orphan guard semantics kept
    feed = FakeFeed([])
    session, exchange = build_session(tmp_path, feed, mode=RunnerMode.PAPER)
    exchange.set_quote(SYM, 100.0, 100.1)
    from vnedge.paper.simulated_exchange import PaperOrderRequest
    exchange.submit_order(PaperOrderRequest("orphan", SYM, True, 0.5))
    session.restore_plan(None)
    assert session._plan is None
    await session.run(max_bars=0)
    session._guard_orphaned_position()
    assert session.gateway.kill_switch.is_active


async def test_synthesized_stop_clamped_after_volatility_gap(tmp_path):
    """Audit finding 2026-07-09: a rebuilt stop uses CURRENT ATR and could sit
    far wider than the original envelope after a volatile restart — it must be
    clamped to the max rebuilt-stop distance and journaled."""
    from vnedge.strategy.base_strategy import SignalIntent as SI

    class WideStopStrategy(AlwaysLong):
        def synthesize_exit_plan(self, df, index, side, entry_price):
            return SI("long", stop_price=entry_price * 0.80,  # 20% away — insane
                      take_profit_price=entry_price * 1.1, reason="wide rebuild")

    feed = FakeFeed([])
    session, exchange = build_session(tmp_path, feed, strategy=WideStopStrategy(),
                                      mode=RunnerMode.PAPER)
    exchange.set_quote(SYM, 100.0, 100.1)
    from vnedge.paper.simulated_exchange import PaperOrderRequest
    exchange.submit_order(PaperOrderRequest("x", SYM, True, 0.5))
    session.restore_plan(None)
    assert session._plan is not None
    entry = exchange.get_positions()[0].entry_price
    dist = abs(session._plan.signal.stop_price - entry) / entry
    assert dist <= 0.03 + 1e-9
    kinds = [r["kind"] for r in session.journal.read_all()]
    assert "plan_stop_clamped" in kinds


async def test_corrupted_persisted_plan_rejected(tmp_path):
    """A hand-edited/corrupted store plan (stop on wrong side / absurd) must be
    refused — orphan-guard semantics beat a bad stop."""
    feed = FakeFeed([])
    session, exchange = build_session(tmp_path, feed, mode=RunnerMode.PAPER)
    exchange.set_quote(SYM, 100.0, 100.1)
    from vnedge.paper.simulated_exchange import PaperOrderRequest
    exchange.submit_order(PaperOrderRequest("x", SYM, True, 0.5))
    session.restore_plan({"side": "long", "stop_price": 1.0,   # 99% away
                          "take_profit_price": None,
                          "entry_bar_ts": "2026-07-09T00:00:00+00:00"})
    assert session._plan is None
    kinds = [r["kind"] for r in session.journal.read_all()]
    assert "plan_restore_rejected" in kinds


# --- maker-edge entry routing: resting limit + touch-to-fill TTL -----------------

_MAKER_FEE = 2 / 10_000
_TAKER_FEE = 5 / 10_000


class MakerLongOnce(BaseStrategy):
    """Signals long exactly once; its id is in the maker-route set."""

    strategy_id = "stealth_trail_bbp_v1"
    warmup_bars = 2

    def __init__(self):
        self._fired = False

    def prepare(self, candles):
        return candles.copy()

    def signal(self, df, index):
        if self._fired:
            return None
        self._fired = True
        close = float(df["close"].iloc[index])
        return SignalIntent("long", stop_price=close * 0.95, take_profit_price=close * 1.10)


def _mrow(i, close=100.0):
    return [BASE + i * MIN, 100.0, 100.5, 99.5, close, 5.0]


async def test_maker_edge_entry_rests_as_limit_then_fills_maker_on_touch(tmp_path):
    feed = FakeFeed([_mrow(5)], quote=(99.99, 100.01))
    session, exchange = build_session(tmp_path, feed, strategy=MakerLongOnce())
    await session.run(max_bars=1)
    # posted a passive buy limit at the bid (99.99); ask 100.01 > 99.99 -> rests
    assert session._pending_entry is not None
    assert session._plan is None
    assert exchange.get_positions() == []           # NOT a position yet
    # next bar: price drops so the ask touches the resting limit -> maker fill
    feed.quote = (99.90, 99.98)                      # ask 99.98 <= 99.99
    feed.closed_candles.put_nowait(_mrow(6))
    await session.run(max_bars=1)
    assert session._pending_entry is None and session._plan is not None
    assert len(exchange.get_positions()) == 1
    fill = exchange.get_fills()[-1]
    assert fill.price == pytest.approx(99.99)        # filled at the limit, no cross
    assert fill.fee_usd == pytest.approx(fill.quantity * 99.99 * _MAKER_FEE)  # MAKER fee


async def test_maker_edge_entry_unfilled_is_cancelled_and_skipped(tmp_path):
    feed = FakeFeed([_mrow(5)], quote=(99.99, 100.01))
    session, exchange = build_session(tmp_path, feed, strategy=MakerLongOnce())
    await session.run(max_bars=1)
    assert session._pending_entry is not None
    # price never comes down to the limit; TTL (2 bars) lapses -> cancel & skip
    for i in (6, 7):
        feed.quote = (100.20, 100.30)                # stays above the buy limit
        feed.closed_candles.put_nowait(_mrow(i, close=101.0))
        await session.run(max_bars=1)
    assert session._pending_entry is None            # signalled once, not re-fired
    assert session._plan is None
    assert exchange.get_positions() == []            # the trade was skipped
    assert "maker_entry_unfilled" in [e["event"] for e in session.trade_log]


async def test_non_maker_strategy_still_uses_immediate_market_entry(tmp_path):
    feed = FakeFeed([_mrow(5)], quote=(99.99, 100.01))
    session, exchange = build_session(tmp_path, feed, strategy=AlwaysLong())  # not maker-route
    await session.run(max_bars=1)
    assert session._pending_entry is None
    assert len(exchange.get_positions()) == 1        # market fill, immediate
    fill = exchange.get_fills()[-1]
    assert fill.fee_usd == pytest.approx(fill.quantity * fill.price * _TAKER_FEE)  # taker


def test_runner_config_trail_flows_into_the_exit_state(tmp_path):
    # RunnerConfig.trail_atr_mult must reach the plan's ActiveExitState, so the
    # runtime trails with the SAME engine the backtester uses (parity).
    feed = FakeFeed(live_rows(n=1))
    session, _ = build_session(tmp_path, feed)
    # rebuild the session's config with trailing on (frozen model → new instance)
    session.config = session.config.model_copy(update={"trail_atr_mult": 3.0, "trail_atr_window": 10})
    plan = session._new_plan(
        SignalIntent(side="long", stop_price=99.0, take_profit_price=None,
                     take_profit_levels=(), reason="t"),
        history()["timestamp"].iloc[-1],
    )
    assert plan.exit_state.trail_atr_mult == 3.0
    # and _trail_atr computes a canonical (finite) ATR from the candle history
    assert session._trail_atr() >= 0.0
