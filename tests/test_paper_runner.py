"""Runner loop — both modes, one execution path, deterministic end to end."""

import pandas as pd
import pytest

from vnedge.data.schemas import normalize_candles
from vnedge.execution.journal import DecisionJournal
from vnedge.execution.order_manager import OrderManager
from vnedge.paper.fill_model import FillModel
from vnedge.paper.paper_broker import PaperBroker
from vnedge.paper.paper_reconciliation import ReconciliationReport
from vnedge.paper.simulated_exchange import SimulatedExchange
from vnedge.risk.kill_switch import KillSwitch
from vnedge.risk.risk_manager import PreTradeRiskGateway
from vnedge.runtime.paper_runner import PaperRunner
from vnedge.runtime.runner_config import RunnerConfig, RunnerMode
from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent

BASE = 1_750_000_000_000
HOUR = 3_600_000
SYM = "BTC/USDT:USDT"
FLAT = (100.0, 101.0, 99.0, 100.0)


def make_candles(bars) -> pd.DataFrame:
    return normalize_candles(
        [[BASE + i * HOUR, o, h, low, c, 10.0] for i, (o, h, low, c) in enumerate(bars)]
    )


class OneShotStrategy(BaseStrategy):
    """Signals a long once, at a chosen bar. Optionally trips a kill switch
    at another bar — deterministic mid-run state changes for tests."""

    strategy_id = "oneshot"
    warmup_bars = 2

    def __init__(self, at_index: int, intent: SignalIntent,
                 kill_switch=None, kill_at: int | None = None):
        self.at_index = at_index
        self.intent = intent
        self.kill_switch = kill_switch
        self.kill_at = kill_at

    def prepare(self, candles):
        return candles.copy()

    def signal(self, df, index):
        if self.kill_switch is not None and index == self.kill_at:
            self.kill_switch.activate("test kill mid-run")
        return self.intent if index == self.at_index else None


LONG = SignalIntent(side="long", stop_price=95.0, take_profit_price=106.0)
LADDER_LONG = SignalIntent(
    side="long",
    stop_price=95.0,
    take_profit_price=106.0,
    take_profit_levels=(102.0, 104.0, 106.0),
    reason="test ladder exit",
)


def build_world(tmp_path, candles, strategy, mode=RunnerMode.PAPER,
                script=None, config_overrides=None):
    config = RunnerConfig(
        mode=mode, symbol=SYM, reconcile_every_bars=3,
        **(config_overrides or {}),
    )
    exchange = SimulatedExchange(FillModel(), config.starting_equity_usd)
    journal = DecisionJournal(tmp_path / "journal.jsonl")
    kill = KillSwitch(kill_file=tmp_path / "KILL")
    gateway = PreTradeRiskGateway(config.risk, kill)
    om = OrderManager(gateway, journal, PaperBroker(exchange, script=script))
    runner = PaperRunner(
        strategy, candles, None, config,
        gateway=gateway, order_manager=om, exchange=exchange, journal=journal,
    )
    return runner, exchange, kill, journal


async def test_paper_round_trip_take_profit(tmp_path):
    bars = [FLAT] * 6 + [(100.0, 107.0, 99.5, 106.5)] + [FLAT] * 3
    runner, exchange, _, journal = build_world(
        tmp_path, make_candles(bars), OneShotStrategy(4, LONG)
    )
    report = await runner.run()

    assert report.mode == "paper"
    assert report.signals_generated == 1
    assert report.orders_submitted == 2  # entry + reduce-only exit
    assert report.fills == 2
    assert exchange.get_positions() == []  # flat at end
    assert report.realized_pnl_usd > 0  # tp at 106 vs entry ~100
    assert report.fees_usd > 0
    assert report.reconciliation_mismatches == 0
    assert report.final_equity_usd == pytest.approx(
        runner.config.starting_equity_usd + report.realized_pnl_usd
    )
    kinds = [r["kind"] for r in journal.read_all()]
    assert "risk_decision" in kinds and "paper_exit" in kinds and "run_report" in kinds


async def test_paper_ladder_captures_tp1_then_breakeven_stop(tmp_path):
    bars = (
        [FLAT] * 6
        + [(100.0, 102.5, 99.5, 102.0)]  # TP1 partial, original stop untouched
        + [(102.0, 102.2, 100.0, 100.5)]  # fee-aware BE stop on remainder
        + [FLAT] * 2
    )
    runner, exchange, _, journal = build_world(
        tmp_path, make_candles(bars), OneShotStrategy(4, LADDER_LONG)
    )
    report = await runner.run()

    assert report.orders_submitted == 3  # entry + TP1 partial + BE stop
    assert report.fills == 3
    assert exchange.get_positions() == []
    exits = [r["payload"] for r in journal.read_all() if r["kind"] == "paper_exit"]
    assert [e["reason"] for e in exits] == ["tp1_partial", "breakeven_stop"]
    assert exits[0]["final"] is False
    assert exits[1]["final"] is True
    assert exits[0]["quantity"] < exits[1]["quantity"]


async def test_stop_exit_is_loss_bounded_by_risk_budget(tmp_path):
    bars = [FLAT] * 6 + [(100.0, 100.5, 94.0, 96.0)] + [FLAT] * 3
    runner, exchange, _, _ = build_world(
        tmp_path, make_candles(bars), OneShotStrategy(4, LONG)
    )
    report = await runner.run()
    assert report.fills == 2
    # 1% risk on $500 = $5; loss must be near budget + costs, never a blowout
    assert -8.0 < report.realized_pnl_usd < 0


async def test_signal_fills_next_bar_open_not_signal_bar(tmp_path):
    candles = make_candles([FLAT] * 8)
    runner, exchange, _, _ = build_world(
        tmp_path, candles, OneShotStrategy(4, LONG),
        config_overrides={"max_holding_bars": 100},
    )
    await runner.run()
    entry_fill = exchange.get_fills()[0]
    # bar 5 open = 100.0 -> ask with 1bp spread, +2bp slippage
    expected = 100.0 * (1 + 0.5 / 10_000) * (1 + 2 / 10_000)
    assert entry_fill.price == pytest.approx(expected)


async def test_shadow_mode_changes_nothing(tmp_path):
    bars = [FLAT] * 6 + [(100.0, 107.0, 99.5, 106.5)] + [FLAT] * 3
    runner, exchange, _, journal = build_world(
        tmp_path, make_candles(bars), OneShotStrategy(4, LONG), mode=RunnerMode.SHADOW
    )
    report = await runner.run()

    assert report.mode == "shadow"
    assert report.signals_generated == 1
    assert report.shadow_approved == 1
    assert report.orders_submitted == 0
    assert report.fills == 0
    assert exchange.get_fills() == []
    assert report.final_equity_usd == pytest.approx(runner.config.starting_equity_usd)
    shadow_records = [r for r in journal.read_all() if r["kind"] == "shadow_intent"]
    assert len(shadow_records) == 1
    assert shadow_records[0]["payload"]["approved"] is True


async def test_timeout_unknown_parks_plan_until_reconciled(tmp_path):
    candles = make_candles([FLAT] * 12)
    runner, exchange, _, _ = build_world(
        tmp_path, candles, OneShotStrategy(4, LONG),
        script=["timeout_reached"],
        config_overrides={"max_holding_bars": 3},
    )
    report = await runner.run()
    # entry landed at venue despite lost ack; reconciliation (every 3 bars)
    # resolved it, the plan activated, and max-holding exited it.
    assert report.orders_submitted == 2
    assert exchange.get_positions() == []
    assert report.reconciliation_mismatches == 0


def test_reconciliation_mismatch_trips_runner_fail_closed_once(tmp_path):
    candles = make_candles([FLAT] * 8)
    runner, _, kill, journal = build_world(
        tmp_path, candles, OneShotStrategy(4, LONG)
    )

    runner.reconciler.run = lambda: ReconciliationReport((), ("internal vs venue",))
    runner._reconcile({})
    runner._reconcile({})

    assert kill.is_active
    records = [
        r for r in journal.read_all()
        if r["kind"] == "reconciliation_fail_closed"
    ]
    assert len(records) == 1
    assert records[0]["payload"]["mismatches"] == ["internal vs venue"]


async def test_kill_switch_blocks_runner_entries(tmp_path):
    """Kill switch tripped at signal time: the entry must be risk-rejected
    and no position can ever open. (Exit-under-kill policy is proven at the
    gateway and OrderManager levels — see test_kill_switch_never_blocks_
    reduce_only_exits and test_kill_switch_then_emergency_flatten — and the
    runner routes exits through that exact OrderManager path.)"""
    candles = make_candles([FLAT] * 10)

    class KillAtSignal(OneShotStrategy):
        def signal(self, df, index):
            if index == self.at_index:
                self.kill_switch.activate("tripped at signal time")
                return self.intent
            return None

    config = RunnerConfig(mode=RunnerMode.PAPER, symbol=SYM, reconcile_every_bars=3)
    exchange = SimulatedExchange(FillModel(), config.starting_equity_usd)
    journal = DecisionJournal(tmp_path / "journal.jsonl")
    kill = KillSwitch(kill_file=tmp_path / "KILL")
    gateway = PreTradeRiskGateway(config.risk, kill)
    om = OrderManager(gateway, journal, PaperBroker(exchange))
    strategy = KillAtSignal(4, LONG, kill_switch=kill)
    runner = PaperRunner(strategy, candles, None, config,
                         gateway=gateway, order_manager=om,
                         exchange=exchange, journal=journal)
    report = await runner.run()

    assert report.signals_generated == 1
    assert report.risk_rejects == 1  # kill switch blocked the entry
    assert report.fills == 0
    assert exchange.get_positions() == []
    # and the rejection is journaled with the kill switch named
    decisions = [r for r in journal.read_all() if r["kind"] == "risk_decision"]
    assert any("kill_switch" in str(d["payload"]["failed_checks"]) for d in decisions)


async def test_report_is_machine_readable(tmp_path):
    candles = make_candles([FLAT] * 8)
    runner, _, _, _ = build_world(tmp_path, candles, OneShotStrategy(4, LONG))
    report = await runner.run()
    payload = report.to_dict()
    for field in ("mode", "symbol", "strategy_id", "bars_processed",
                  "signals_generated", "orders_submitted", "fills", "fees_usd",
                  "realized_pnl_usd", "unrealized_pnl_usd", "max_drawdown_pct",
                  "risk_rejects", "reconciliation_mismatches", "final_equity_usd"):
        assert field in payload
