"""Virtual outcome resolution for shadow lanes.

Unit level: ShadowOutcomeTracker resolves journaled intents with the
backtester's conservative semantics (stop wins ties, timeout at bar close,
taker fees both sides) and never resolves the same intent twice across
restarts — the journal is the durable store.

Integration level: a SHADOW LivePaperSession journals resolvable intents,
resolves them on later closed bars, replays seeded history on restart, and
surfaces per-lane stats as session_stats["shadow_perf"]; paper lanes are
untouched.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from vnedge.data.schemas import normalize_candles
from vnedge.execution.journal import DecisionJournal
from vnedge.execution.order_manager import OrderManager
from vnedge.paper.fill_model import FillModel
from vnedge.paper.paper_broker import PaperBroker
from vnedge.paper.simulated_exchange import SimulatedExchange
from vnedge.risk.kill_switch import KillSwitch
from vnedge.risk.risk_manager import MarketState, PreTradeRiskGateway
from vnedge.runtime.live_paper import LivePaperSession
from vnedge.runtime.multi_lane import MultiLaneProvider
from vnedge.runtime.runner_config import RunnerConfig, RunnerMode
from vnedge.runtime.shadow_outcomes import ShadowOutcomeTracker
from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent, StrategyExitIntent

BASE = 1_750_000_000_000
MIN = 60_000
SYM = "BTC/USDT:USDT"


def ts(i: int) -> pd.Timestamp:
    return pd.to_datetime(BASE + i * MIN, unit="ms", utc=True)


def bar(i: int, high: float = 100.5, low: float = 99.5, close: float = 100.0) -> pd.Series:
    return pd.Series({
        "timestamp": ts(i), "open": 100.0, "high": high, "low": low,
        "close": close, "volume": 5.0,
    })


def journal_intent(
    journal: DecisionJournal, key: str, *, side: str = "long", qty: float = 1.0,
    notional: float = 100.0, stop: float = 95.0, tp: float | None = 110.0,
    bar_index: int = 0, approved: bool = True,
) -> None:
    journal.append("shadow_intent", {
        "intent_key": key,
        "approved": approved,
        "failed_checks": [], "passed_checks": ["all"], "explanation": "test",
        "intent": {
            "symbol": SYM, "side": side, "quantity": qty, "notional_usd": notional,
            "leverage": 1.0, "reduce_only": False, "strategy_id": "test_strategy",
        },
        "signal_reason": "test intent",
        "stop_price": stop,
        "take_profit_price": tp,
        "bar_ts": ts(bar_index).isoformat(),
    })


def tracker_for(tmp_path, **kwargs) -> tuple[ShadowOutcomeTracker, DecisionJournal]:
    journal = DecisionJournal(tmp_path / "journal.jsonl")
    return ShadowOutcomeTracker(journal, **kwargs), journal


# --- unit: resolution semantics ---------------------------------------------------


def test_stop_wins_when_stop_and_target_share_a_bar(tmp_path):
    tracker, journal = tracker_for(tmp_path)
    journal_intent(journal, "k1", stop=95.0, tp=110.0, bar_index=0)
    tracker = ShadowOutcomeTracker(journal)
    # bar 1 hits BOTH the stop (low 94) and the target (high 111)
    outcomes = tracker.resolve_bar(bar(1, high=111.0, low=94.0))
    assert len(outcomes) == 1
    out = outcomes[0]
    assert out.resolution == "stop"
    assert out.exit_price == 95.0
    # gross -5, taker fees both sides: 100*5bps + 95*5bps
    assert out.virtual_net_usd == pytest.approx(-5.0 - (0.05 + 0.0475))
    records = [r for r in journal.read_all() if r["kind"] == "shadow_outcome"]
    assert len(records) == 1
    assert records[0]["payload"]["resolution"] == "stop"
    assert records[0]["payload"]["intent_key"] == "k1"


def test_target_resolution_and_fee_math(tmp_path):
    tracker, journal = tracker_for(tmp_path)
    journal_intent(journal, "k1", stop=95.0, tp=110.0, bar_index=0)
    tracker = ShadowOutcomeTracker(journal)
    outcomes = tracker.resolve_bar(bar(1, high=111.0, low=99.0))
    assert outcomes[0].resolution == "target"
    assert outcomes[0].exit_price == 110.0
    # gross +10, fees 100*5bps entry + 110*5bps exit
    assert outcomes[0].virtual_net_usd == pytest.approx(10.0 - (0.05 + 0.055))


# --- maker route: touch-to-fill + maker fee, all-taker figure kept visible --------


def test_is_maker_route_strategy_matches_maker_edge_set():
    from vnedge.runtime.shadow_outcomes import is_maker_route_strategy
    assert is_maker_route_strategy("stealth_trail_bbp_v1")
    assert is_maker_route_strategy("vnedge_algo_ml_pro_v1")
    assert not is_maker_route_strategy("trend_continuation_v1")
    assert not is_maker_route_strategy("funding_mean_reversion_v1")
    assert not is_maker_route_strategy("")


def test_default_route_is_taker_and_byte_identical(tmp_path):
    tracker, journal = tracker_for(tmp_path)  # maker_route defaults False
    tracker.track(intent_key="t", side="long", quantity=1.0, notional_usd=100.0,
                  stop_price=95.0, take_profit_price=110.0, decision_bar_ts=ts(0))
    out = tracker.resolve_bar(bar(1, high=111.0, low=99.0))[0]
    assert out.route == "taker"
    # taker entry (5bps) + taker exit (5bps) — unchanged from before the route
    assert out.virtual_net_usd == pytest.approx(10.0 - (100 * 0.0005 + 110 * 0.0005))
    assert out.virtual_net_taker_usd == out.virtual_net_usd


def test_maker_route_fills_on_touch_and_charges_maker_fee(tmp_path):
    tracker, journal = tracker_for(tmp_path, maker_route=True)
    tracker.track(intent_key="m", side="long", quantity=1.0, notional_usd=100.0,
                  stop_price=95.0, take_profit_price=110.0, decision_bar_ts=ts(0))
    # bar dips to 99 (touches the resting buy limit at 100) then runs to target
    out = tracker.resolve_bar(bar(1, high=111.0, low=99.0))[0]
    assert out.route == "maker" and out.resolution == "target" and out.exit_price == 110.0
    # entry MAKER fee (2bps) + exit taker (5bps)
    assert out.virtual_net_usd == pytest.approx(10.0 - (100 * 0.0002 + 110 * 0.0005))
    # the conservative all-taker figure is still recorded, and is strictly worse
    assert out.virtual_net_taker_usd == pytest.approx(10.0 - (100 * 0.0005 + 110 * 0.0005))
    assert out.virtual_net_usd > out.virtual_net_taker_usd
    payload = [r for r in journal.read_all() if r["kind"] == "shadow_outcome"][0]["payload"]
    assert payload["route"] == "maker"
    assert tracker.stats()["route"] == "maker" and tracker.stats()["maker_unfilled"] == 0


def test_maker_route_misses_the_untouched_runner(tmp_path):
    tracker, journal = tracker_for(tmp_path, maker_route=True)
    tracker.track(intent_key="m", side="long", quantity=1.0, notional_usd=100.0,
                  stop_price=95.0, take_profit_price=110.0, decision_bar_ts=ts(0))
    # bar gaps up and never trades down to the resting limit (low 101 > 100) —
    # the maker misses this immediate runner (honest adverse selection)
    assert tracker.resolve_bar(bar(1, high=112.0, low=101.0)) == []
    assert tracker.stats()["maker_unfilled"] == 1
    assert tracker.stats()["virtual_trades"] == 0
    kinds = {r["kind"] for r in journal.read_all()}
    assert "shadow_maker_unfilled" in kinds and "shadow_outcome" not in kinds


def test_maker_route_fills_within_ttl_on_a_later_touch(tmp_path):
    tracker, journal = tracker_for(tmp_path, maker_route=True, maker_fill_ttl_bars=2)
    tracker.track(intent_key="m", side="long", quantity=1.0, notional_usd=100.0,
                  stop_price=95.0, take_profit_price=110.0, decision_bar_ts=ts(0))
    assert tracker.resolve_bar(bar(1, high=112.0, low=101.0)) == []  # bar1: no touch
    out = tracker.resolve_bar(bar(2, high=111.0, low=99.0))          # bar2: touches
    assert len(out) == 1 and out[0].route == "maker" and out[0].resolution == "target"
    assert tracker.stats()["maker_unfilled"] == 0 and tracker.stats()["virtual_trades"] == 1


def test_maker_route_short_fills_on_upward_touch(tmp_path):
    tracker, journal = tracker_for(tmp_path, maker_route=True)
    # short: resting sell limit at 100 fills if price rises to touch it
    tracker.track(intent_key="s", side="short", quantity=1.0, notional_usd=100.0,
                  stop_price=105.0, take_profit_price=90.0, decision_bar_ts=ts(0))
    out = tracker.resolve_bar(bar(1, high=101.0, low=89.0))[0]
    assert out.route == "maker" and out.resolution == "target" and out.exit_price == 90.0


# --- TP ladder: recorded, never an exit trigger ----------------------------------


def test_tp_ladder_records_max_reached_without_changing_exit(tmp_path):
    tracker, journal = tracker_for(tmp_path)
    tracker.track(
        intent_key="k", side="long", quantity=1.0, notional_usd=100.0,
        stop_price=95.0, take_profit_price=110.0, decision_bar_ts=ts(0),
        take_profit_levels=(102.0, 105.0, 110.0),
    )
    # bar runs UP to 106 (crosses tp1=102, tp2=105) then also hits the stop.
    outcomes = tracker.resolve_bar(bar(1, high=106.0, low=94.0))
    out = outcomes[0]
    # exit + P&L are byte-identical to the no-ladder case: stop still wins.
    assert out.resolution == "stop" and out.exit_price == 95.0
    assert out.virtual_net_usd == pytest.approx(-5.0 - (0.05 + 0.0475))
    # ...but we record how far it travelled: reached TP2 of 3.
    assert out.tp_reached == 2
    assert out.take_profit_levels == (102.0, 105.0, 110.0)
    payload = [r for r in journal.read_all() if r["kind"] == "shadow_outcome"][0]["payload"]
    assert payload["tp_reached"] == 2
    assert payload["take_profit_levels"] == [102.0, 105.0, 110.0]


def test_tp_ladder_short_side_and_empty_default(tmp_path):
    tracker, journal = tracker_for(tmp_path)
    # short: favourable = price DOWN. tp1=98, tp2=95 (=stop). Runs to 96 then stops.
    tracker.track(
        intent_key="s", side="short", quantity=1.0, notional_usd=100.0,
        stop_price=105.0, take_profit_price=90.0, decision_bar_ts=ts(0),
        take_profit_levels=(98.0, 95.0, 90.0),
    )
    out = tracker.resolve_bar(bar(1, high=105.5, low=96.0))[0]
    assert out.resolution == "stop"          # exit unchanged
    assert out.tp_reached == 1               # low 96 crossed tp1=98 only
    # a plain intent with no ladder records tp_reached 0 (backward compatible)
    tracker.track(intent_key="p", side="long", quantity=1.0, notional_usd=100.0,
                  stop_price=95.0, take_profit_price=110.0, decision_bar_ts=ts(0))
    plain = tracker.resolve_bar(bar(2, high=111.0, low=99.0))[0]
    assert plain.tp_reached == 0 and plain.take_profit_levels == ()


def test_signal_intent_take_profit_levels_defaults_empty():
    intent = SignalIntent("long", stop_price=95.0, take_profit_price=110.0)
    assert intent.take_profit_levels == ()


def test_custom_fee_model_applied_both_sides(tmp_path):
    tracker, journal = tracker_for(tmp_path)
    journal_intent(journal, "k1", stop=95.0, tp=110.0, bar_index=0)
    tracker = ShadowOutcomeTracker(journal, fill_model=FillModel(taker_fee_bps=10.0))
    outcomes = tracker.resolve_bar(bar(1, high=111.0, low=99.0))
    assert outcomes[0].fees_usd == pytest.approx(0.10 + 0.11)
    assert outcomes[0].virtual_net_usd == pytest.approx(10.0 - 0.21)


def test_timeout_resolution_at_bar_close(tmp_path):
    tracker, journal = tracker_for(tmp_path)
    journal_intent(journal, "k1", stop=95.0, tp=110.0, bar_index=0)
    tracker = ShadowOutcomeTracker(journal, max_holding_bars=2)
    # fill bar (bars_held=0) then one more bar (1): still open
    assert tracker.resolve_bar(bar(1)) == []
    assert tracker.resolve_bar(bar(2)) == []
    # bars_held reaches max_holding_bars => timeout at THIS bar's close,
    # mirroring run_backtest's `j - entry_bar >= max_holding_bars`
    outcomes = tracker.resolve_bar(bar(3, close=101.0))
    assert outcomes[0].resolution == "timeout"
    assert outcomes[0].bars_held == 2
    assert outcomes[0].exit_price == 101.0
    assert outcomes[0].virtual_net_usd == pytest.approx(
        1.0 - (0.05 + 101.0 * 0.0005)
    )


def test_strategy_exit_hook_resolves_shadow_outcome_at_close(tmp_path):
    def strategy_exit(side, entry_price, bar):
        return StrategyExitIntent("strategy_neutral", exit_price=float(bar["close"]))

    tracker, _ = tracker_for(tmp_path, strategy_exit=strategy_exit)
    tracker.track(intent_key="k", side="long", quantity=1.0, notional_usd=100.0,
                  stop_price=95.0, take_profit_price=None, decision_bar_ts=ts(0))

    outcomes = tracker.resolve_bar(bar(1, high=101.0, low=99.0, close=101.0))

    assert len(outcomes) == 1
    assert outcomes[0].resolution == "strategy_neutral"
    assert outcomes[0].exit_price == 101.0
    assert outcomes[0].virtual_net_usd == pytest.approx(1.0 - (0.05 + 101.0 * 0.0005))


def test_shadow_stop_still_wins_before_strategy_exit_hook(tmp_path):
    def strategy_exit(side, entry_price, bar):
        return StrategyExitIntent("strategy_neutral", exit_price=float(bar["close"]))

    tracker, _ = tracker_for(tmp_path, strategy_exit=strategy_exit)
    tracker.track(intent_key="k", side="long", quantity=1.0, notional_usd=100.0,
                  stop_price=95.0, take_profit_price=None, decision_bar_ts=ts(0))

    outcomes = tracker.resolve_bar(bar(1, high=101.0, low=94.0, close=101.0))

    assert outcomes[0].resolution == "stop"
    assert outcomes[0].exit_price == 95.0


def test_short_side_stop_on_high(tmp_path):
    tracker, journal = tracker_for(tmp_path)
    journal_intent(journal, "k1", side="short", stop=105.0, tp=90.0, bar_index=0)
    tracker = ShadowOutcomeTracker(journal)
    outcomes = tracker.resolve_bar(bar(1, high=106.0, low=99.0))
    assert outcomes[0].resolution == "stop"
    # short stopped above entry: gross = -(105-100) = -5
    assert outcomes[0].virtual_net_usd == pytest.approx(-5.0 - (0.05 + 0.0525))


def test_bars_at_or_before_the_decision_bar_never_resolve(tmp_path):
    tracker, journal = tracker_for(tmp_path)
    journal_intent(journal, "k1", stop=95.0, tp=110.0, bar_index=5)
    tracker = ShadowOutcomeTracker(journal)
    # a crashing bar BEFORE/AT the decision bar is history, not the future
    assert tracker.resolve_bar(bar(4, low=10.0)) == []
    assert tracker.resolve_bar(bar(5, low=10.0)) == []
    assert tracker.has_pending


# --- unit: durability across restarts ---------------------------------------------


def test_restart_does_not_double_resolve(tmp_path):
    tracker, journal = tracker_for(tmp_path)
    journal_intent(journal, "k1", stop=95.0, tp=110.0, bar_index=0)
    tracker = ShadowOutcomeTracker(journal)
    assert len(tracker.resolve_bar(bar(1, low=94.0))) == 1

    rebuilt = ShadowOutcomeTracker(journal)  # "restart": reload from journal
    assert not rebuilt.has_pending
    assert rebuilt.resolve_bar(bar(2, low=90.0)) == []
    # stats survive the restart, loaded from the journaled outcome
    assert rebuilt.stats()["virtual_trades"] == 1
    records = [r for r in journal.read_all() if r["kind"] == "shadow_outcome"]
    assert len(records) == 1  # still exactly one — never re-journaled


def test_rejected_and_legacy_intents_are_not_resolved(tmp_path):
    tracker, journal = tracker_for(tmp_path)
    journal_intent(journal, "rejected", approved=False, bar_index=0)
    # legacy record predating outcome tracking: no stop/target/bar_ts
    journal.append("shadow_intent", {
        "intent_key": "legacy", "approved": True,
        "intent": {"side": "long", "quantity": 1.0, "notional_usd": 100.0},
        "signal_reason": "old record",
    })
    tracker = ShadowOutcomeTracker(journal)
    assert not tracker.has_pending
    assert tracker.resolve_bar(bar(1, low=1.0, high=1_000.0)) == []


def test_duplicate_intent_keys_resolve_once(tmp_path):
    tracker, journal = tracker_for(tmp_path)
    journal_intent(journal, "k1", stop=95.0, tp=110.0, bar_index=0)
    journal_intent(journal, "k1", stop=95.0, tp=110.0, bar_index=0)  # re-primed
    tracker = ShadowOutcomeTracker(journal)
    assert len(tracker.resolve_bar(bar(1, low=94.0))) == 1
    assert tracker.stats()["virtual_trades"] == 1


# --- unit: per-lane stats ----------------------------------------------------------


def test_stats_aggregate_wins_net_and_profit_factor(tmp_path):
    tracker, journal = tracker_for(tmp_path)
    journal_intent(journal, "w1", stop=95.0, tp=110.0, bar_index=0)
    journal_intent(journal, "w2", stop=95.0, tp=110.0, bar_index=0)
    journal_intent(journal, "open", stop=1.0, tp=None, bar_index=0)
    tracker = ShadowOutcomeTracker(journal)
    # bar 1 hits the target only: w1 & w2 close as wins, "open" stays open
    outcomes = tracker.resolve_bar(bar(1, high=111.0, low=99.0))
    assert {o.intent_key for o in outcomes} == {"w1", "w2"}
    stats = tracker.stats()
    assert stats["virtual_trades"] == 2
    assert stats["wins"] == 2
    assert stats["losses"] == 0
    assert stats["open_intents"] == 1
    assert stats["profit_factor"] is None  # no losses yet — undefined, not inf
    assert stats["resolutions"]["target"] == 2
    assert stats["status"] == "OBSERVE"
    assert stats["trade_compatible"] is True


def test_profit_factor_with_mixed_outcomes(tmp_path):
    tracker, journal = tracker_for(tmp_path)
    journal_intent(journal, "w", stop=95.0, tp=110.0, bar_index=0)
    journal_intent(journal, "l", stop=95.0, tp=110.0, bar_index=2)
    tracker = ShadowOutcomeTracker(journal)
    tracker.resolve_bar(bar(1, high=111.0, low=99.0))   # w -> target +9.895
    tracker.resolve_bar(bar(3, high=100.5, low=94.0))   # l -> stop  -5.0975
    stats = tracker.stats()
    assert stats["virtual_trades"] == 2 and stats["wins"] == 1
    assert stats["net_usd"] == pytest.approx(9.895 - 5.0975, abs=1e-4)
    assert stats["profit_factor"] == pytest.approx(9.895 / 5.0975, abs=1e-3)
    assert stats["status"] == "OBSERVE"


def test_replay_resolves_against_seen_history(tmp_path):
    tracker, journal = tracker_for(tmp_path)
    journal_intent(journal, "k1", stop=95.0, tp=110.0, bar_index=0)
    tracker = ShadowOutcomeTracker(journal)
    candles = pd.DataFrame([bar(0), bar(1), bar(2, low=94.0), bar(3)])
    outcomes = tracker.replay(candles)
    assert len(outcomes) == 1
    assert outcomes[0].resolution == "stop"
    assert outcomes[0].bars_held == 1  # filled at bar 1, stopped at bar 2


# --- integration: LivePaperSession shadow lanes -------------------------------------


class FakeFeed:
    """Same surface as LiveMarketFeed, scripted content, no network."""

    exchange_id = "fake"

    def __init__(self, rows, quote=(99.99, 100.01)):
        self.closed_candles = asyncio.Queue()
        for row in rows:
            self.closed_candles.put_nowait(row)
        self.quote = quote
        self.funding_rate = 0.0001
        self.healthy = True

    def staleness_seconds(self, now=None):
        return 0.5

    def market_state(self) -> MarketState:
        bid, ask = self.quote
        return MarketState(
            symbol=SYM, last_update=datetime.now(UTC) - timedelta(milliseconds=100),
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


class CaptureProvider:
    def __init__(self):
        self.snapshots = []

    def publish(self, snapshot):
        self.snapshots.append(snapshot)


def history(n=5) -> pd.DataFrame:
    return normalize_candles(
        [[BASE + i * MIN, 100.0, 100.5, 99.5, 100.0, 5.0] for i in range(n)]
    )


def build_session(tmp_path, feed, *, mode=RunnerMode.SHADOW, hist=None, provider=None):
    config = RunnerConfig(mode=mode, symbol=SYM, reconcile_every_bars=2)
    exchange = SimulatedExchange(FillModel(), config.starting_equity_usd)
    journal = DecisionJournal(tmp_path / "journal.jsonl")
    kill = KillSwitch(kill_file=tmp_path / "KILL")
    gateway = PreTradeRiskGateway(config.risk, kill)
    om = OrderManager(gateway, journal, PaperBroker(exchange))
    return LivePaperSession(
        AlwaysLong(), feed, hist if hist is not None else history(), config,
        gateway=gateway, order_manager=om, exchange=exchange, journal=journal,
        snapshot_provider=provider,
    )


async def test_shadow_session_resolves_intents_into_virtual_outcomes(tmp_path):
    # bar 5 is quiet; bar 6 crashes through the 95 stop of every open intent
    rows = [
        [BASE + 5 * MIN, 100.0, 100.5, 99.5, 100.0, 5.0],
        [BASE + 6 * MIN, 100.0, 100.5, 94.0, 96.0, 5.0],
    ]
    provider = CaptureProvider()
    session = build_session(tmp_path, FakeFeed(rows), provider=provider)
    await session.run(max_bars=2)

    intents = [r for r in session.journal.read_all() if r["kind"] == "shadow_intent"]
    # prime (bar 4) + bar 5 + bar 6 — each now carries stop/target/bar_ts
    assert len(intents) == 3
    for record in intents:
        payload = record["payload"]
        assert {"stop_price", "take_profit_price", "bar_ts"} <= payload.keys()
    # the prime + bar-5 intents fired off a 100.0 close: stop 95, target 110
    assert intents[0]["payload"]["stop_price"] == pytest.approx(95.0)
    assert intents[1]["payload"]["take_profit_price"] == pytest.approx(110.0)

    outcomes = [r for r in session.journal.read_all() if r["kind"] == "shadow_outcome"]
    # bar 6 stops the prime intent (bar 4) and the bar-5 intent; the bar-6
    # intent fills next bar and stays open
    assert len(outcomes) == 2
    assert all(o["payload"]["resolution"] == "stop" for o in outcomes)
    stats = session.shadow_outcomes.stats()
    assert stats["virtual_trades"] == 2 and stats["wins"] == 0
    assert stats["net_usd"] < 0
    assert stats["status"] == "SHADOW_PROBATION"
    assert stats["trade_compatible"] is False
    assert stats["open_intents"] == 1
    # surfaced to the dashboard through session_stats
    assert provider.snapshots[-1]["session"]["shadow_perf"] == stats
    events = [e["event"] for e in session.trade_log]
    assert events.count("shadow_outcome") == 2


async def test_restart_replays_history_and_never_double_resolves(tmp_path):
    # session 1: two intents journaled (prime bar 4 + live bar 5), none resolved
    session1 = build_session(
        tmp_path, FakeFeed([[BASE + 5 * MIN, 100.0, 100.5, 99.5, 100.0, 5.0]])
    )
    await session1.run(max_bars=1)
    assert session1.shadow_outcomes.stats() == {
        "virtual_trades": 0, "wins": 0, "losses": 0, "net_usd": 0.0,
        "profit_factor": None, "open_intents": 2,
        "resolutions": {"stop": 0, "target": 0, "timeout": 0},
        "status": "OBSERVE", "trade_compatible": True,
        "route": "taker", "net_taker_usd": 0.0, "maker_unfilled": 0,
    }

    # restart: seeded history now includes bar 6, which broke the stops while
    # the session was down; a quiet live bar 7 follows
    hist = normalize_candles(
        [[BASE + i * MIN, 100.0, 100.5, 99.5, 100.0, 5.0] for i in range(6)]
        + [[BASE + 6 * MIN, 100.0, 100.5, 94.0, 96.0, 5.0]]
    )
    session2 = build_session(
        tmp_path, FakeFeed([[BASE + 7 * MIN, 100.0, 100.5, 99.5, 100.0, 5.0]]),
        hist=hist,
    )
    await session2.run(max_bars=1)
    outcomes = [r for r in session2.journal.read_all() if r["kind"] == "shadow_outcome"]
    assert len(outcomes) == 2  # both stale intents resolved from seeded history
    assert all(o["payload"]["resolution"] == "stop" for o in outcomes)

    # third start: nothing pending from those keys, nothing re-resolved
    session3 = build_session(
        tmp_path, FakeFeed([[BASE + 8 * MIN, 100.0, 100.5, 94.0, 96.0, 5.0]]),
        hist=normalize_candles(
            [[BASE + i * MIN, 100.0, 100.5, 99.5, 100.0, 5.0] for i in range(8)]
        ),
    )
    await session3.run(max_bars=1)
    outcomes = [r for r in session3.journal.read_all() if r["kind"] == "shadow_outcome"]
    stopped_keys = [o["payload"]["intent_key"] for o in outcomes]
    assert len(stopped_keys) == len(set(stopped_keys))  # no key resolved twice
    assert session3.shadow_outcomes.stats()["virtual_trades"] == len(stopped_keys)


async def test_paper_mode_has_no_virtual_outcome_tracking(tmp_path):
    provider = CaptureProvider()
    session = build_session(
        tmp_path, FakeFeed([[BASE + 5 * MIN, 100.0, 100.5, 99.5, 100.0, 5.0]]),
        mode=RunnerMode.PAPER, provider=provider,
    )
    await session.run(max_bars=1)
    assert session.shadow_outcomes is None
    kinds = {r["kind"] for r in session.journal.read_all()}
    assert "shadow_outcome" not in kinds and "shadow_intent" not in kinds
    assert provider.snapshots[-1]["session"]["shadow_perf"] is None


# --- integration: multi-lane summary ------------------------------------------------


def test_multi_lane_summary_exposes_shadow_perf():
    perf = {"virtual_trades": 3, "wins": 2, "losses": 1, "net_usd": 12.4,
            "profit_factor": 2.1, "open_intents": 1,
            "resolutions": {"stop": 1, "target": 2, "timeout": 0},
            "status": "OBSERVE", "trade_compatible": True}
    provider = MultiLaneProvider("lane_a")
    provider.sink("lane_a", "binanceusdm").publish({
        "mode": "shadow (live data)", "symbol": SYM, "equity": 500.0,
        "session": {"shadow_perf": perf, "bars_processed": 10},
    })
    provider.sink("lane_b", "bybit").publish({
        "mode": "paper (live data)", "symbol": SYM, "equity": 500.0,
        "session": {"shadow_perf": None, "bars_processed": 10},
    })
    lanes = {lane["lane_id"]: lane for lane in provider.latest()["lanes"]}
    assert lanes["lane_a"]["shadow_perf"] == perf
    assert lanes["lane_b"]["shadow_perf"] is None
