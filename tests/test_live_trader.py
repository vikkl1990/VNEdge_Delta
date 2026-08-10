"""Live trader runtime — three-gate enforcement + wiring via fakes (no keys)."""

import asyncio
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from vnedge.config.risk_config import RiskConfig
from vnedge.config.settings import LIVE_CONFIRMATION_PHRASE, Settings, TradingMode
from vnedge.data.schemas import normalize_candles
from vnedge.execution.journal import DecisionJournal
from vnedge.execution.live_reconciliation import LiveReconciler
from vnedge.execution.order_manager import FlattenTarget, OrderManager
from vnedge.execution.private_stream import PrivateStreamHealth
from vnedge.risk.kill_switch import KillSwitch
from vnedge.risk.position_sizer import SymbolLimits
from vnedge.risk.risk_manager import AccountState, MarketState, PreTradeRiskGateway
from vnedge.runtime.pre_live_checklist import run_pre_live_checklist
from vnedge.runtime.live_trader import LiveTraderSession
from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent

BASE = 1_750_000_000_000
HOUR = 3_600_000
SYM = "BTC/USDT:USDT"
LIMITS = SymbolLimits(min_qty=0.0001, qty_step=0.0001, min_notional_usd=5.0,
                      maintenance_margin_rate=0.005)


def live_settings(mode=TradingMode.LIVE_SMALL, enabled=True,
                  phrase=LIVE_CONFIRMATION_PHRASE, **kw):
    kw.setdefault("live_small_capital_cap_usd", 100_000.0)
    return Settings(_env_file=None, trading_mode=mode, live_trading_enabled=enabled,
                    confirm_live_trading=phrase, risk=RiskConfig(), **kw)


class FakeFeed:
    exchange_id = "binanceusdm"

    def __init__(self, rows, quote=(99.99, 100.01)):
        self.closed_candles = asyncio.Queue()
        for r in rows:
            self.closed_candles.put_nowait(r)
        self.quote = quote

    def market_state(self):
        return MarketState(SYM, datetime.now(UTC) - timedelta(milliseconds=100),
                           spread_bps=2.0, estimated_slippage_bps=2.0,
                           funding_rate=0.0001, exchange_healthy=True)


class FakeLiveAdapter:
    """Implements the ExecutionAdapter + fetch_order_status surface."""

    def __init__(self, script=None):
        self.submitted = []
        self._script = list(script or [])
        self._status = {}

    async def submit_order(self, order):
        self.submitted.append(order.client_order_id)
        behavior = self._script.pop(0) if self._script else "ack"
        if behavior == "timeout":
            from vnedge.execution.order_manager import AdapterTimeout
            raise AdapterTimeout("no ack")
        if behavior == "reject":
            from vnedge.execution.order_manager import AdapterRejection
            raise AdapterRejection("venue rejected")
        if behavior == "timeout_reached":
            from vnedge.execution.order_manager import AdapterTimeout

            self._status[order.client_order_id] = {
                "status": "closed",
                "filled": order.intent.quantity,
            }
            raise AdapterTimeout("ack lost after venue accepted")
        self._status[order.client_order_id] = {"status": "closed", "filled": order.intent.quantity}
        return f"ex_{len(self.submitted)}"

    async def cancel_order(self, order):
        return "cancelled"

    async def fetch_order_status(self, order):
        return self._status.get(order.client_order_id)


class FakeAccounts:
    def __init__(self, equity=800.0, positions=None):
        self._equity = equity
        self._positions = positions or []

    async def account_state(self):
        return AccountState(equity_usd=self._equity, daily_pnl_usd=0.0,
                            peak_equity_usd=self._equity, open_positions=len(self._positions))

    async def open_positions(self):
        return list(self._positions)


def wire(settings, feed, adapter, accounts, tmp_path, strategy, **session_kw):
    journal = DecisionJournal(tmp_path / "j.jsonl")
    gateway = PreTradeRiskGateway(settings.risk, KillSwitch(kill_file=tmp_path / "K"))
    om = OrderManager(gateway, journal, adapter)
    reconciler = LiveReconciler(om, adapter)
    hist = normalize_candles([[BASE + i * HOUR, 100.0, 101.0, 99.0, 100.0, 10.0]
                              for i in range(5)])
    return LiveTraderSession(
        strategy, feed, hist, settings=settings, gateway=gateway, order_manager=om,
        reconciler=reconciler, account_provider=accounts, symbol=SYM, limits=LIMITS,
        **session_kw,
    ), om


class OneShotLong(BaseStrategy):
    strategy_id = "oneshot"
    warmup_bars = 2

    def __init__(self, at_bar=6):
        self.at_bar = at_bar
        self._fired = False

    def prepare(self, candles):
        return candles.copy()

    def signal(self, df, index):
        if self._fired or len(df) < self.at_bar:
            return None
        self._fired = True
        return SignalIntent("long", stop_price=95.0, take_profit_price=106.0)


# --- THE GATE ---------------------------------------------------------------------

@pytest.mark.parametrize("settings", [
    Settings(_env_file=None, trading_mode=TradingMode.PAPER),                       # not a live mode
    Settings(_env_file=None, trading_mode=TradingMode.LIVE_SMALL, live_trading_enabled=False),
    Settings(_env_file=None, trading_mode=TradingMode.LIVE_SMALL, live_trading_enabled=True,
             confirm_live_trading="wrong"),
])
def test_refuses_to_run_without_all_three_gates(settings, tmp_path):
    with pytest.raises(RuntimeError, match="three live gates"):
        wire(settings, FakeFeed([]), FakeLiveAdapter(), FakeAccounts(), tmp_path, OneShotLong())


def test_constructs_when_all_gates_open(tmp_path):
    session, _ = wire(live_settings(), FakeFeed([]), FakeLiveAdapter(),
                      FakeAccounts(), tmp_path, OneShotLong())
    assert session.entries_allowed


def test_refuses_failed_pre_live_report(tmp_path):
    settings = live_settings()
    report = run_pre_live_checklist(
        settings=settings,
        risk_config=settings.risk,
        kill_switch_active=True,
        has_unresolved_orders=False,
        journal_path=tmp_path / "j.jsonl",
        credentials_present=True,
        lower_rungs_validated=True,
    )
    assert not report.cleared
    with pytest.raises(RuntimeError, match="pre-live checklist"):
        wire(
            settings,
            FakeFeed([]),
            FakeLiveAdapter(),
            FakeAccounts(),
            tmp_path,
            OneShotLong(),
            pre_live_report=report,
        )


# --- Wiring -----------------------------------------------------------------------

def bar(i, o=100.0, h=101.0, low=99.0, c=100.0):
    return [BASE + (5 + i) * HOUR, o, h, low, c, 10.0]


async def test_entry_flows_through_real_adapter(tmp_path):
    adapter = FakeLiveAdapter()
    feed = FakeFeed([bar(0)])
    session, om = wire(live_settings(), feed, adapter, FakeAccounts(), tmp_path,
                       OneShotLong(at_bar=6))
    await session.run(max_bars=1)
    assert session.signals == 1
    assert len(adapter.submitted) == 1  # real adapter received the order
    assert session.orders_submitted == 1


async def test_emergency_reduce_only_blocks_entries(tmp_path):
    session, om = wire(live_settings(mode=TradingMode.EMERGENCY_REDUCE_ONLY),
                       FakeFeed([bar(0)]), FakeLiveAdapter(), FakeAccounts(),
                       tmp_path, OneShotLong(at_bar=6))
    assert not session.entries_allowed
    await session.run(max_bars=1)
    assert session.orders_submitted == 0  # no entry in reduce-only mode


async def test_capital_cap_refuses_entry(tmp_path):
    adapter = FakeLiveAdapter()
    session, om = wire(live_settings(live_small_capital_cap_usd=100.0),
                       FakeFeed([bar(0)]), adapter, FakeAccounts(equity=500.0),
                       tmp_path, OneShotLong(at_bar=6))
    await session.run(max_bars=1)
    assert adapter.submitted == []  # equity over cap -> no order


async def test_required_private_stream_blocks_entries_when_stale(tmp_path):
    adapter = FakeLiveAdapter()
    health = PrivateStreamHealth(connected=False)
    session, om = wire(
        live_settings(),
        FakeFeed([bar(0)]),
        adapter,
        FakeAccounts(),
        tmp_path,
        OneShotLong(at_bar=6),
        require_private_stream=True,
        private_stream_health=health,
    )

    await session.run(max_bars=1)

    assert adapter.submitted == []
    assert session.private_stream_ready() is False


async def test_required_private_stream_allows_entries_when_fresh(tmp_path):
    adapter = FakeLiveAdapter()
    health = PrivateStreamHealth(connected=True, last_event_at=datetime.now(UTC))
    session, om = wire(
        live_settings(),
        FakeFeed([bar(0)]),
        adapter,
        FakeAccounts(),
        tmp_path,
        OneShotLong(at_bar=6),
        require_private_stream=True,
        private_stream_health=health,
    )

    await session.run(max_bars=1)

    assert len(adapter.submitted) == 1


async def test_emergency_flatten_submits_reduce_only(tmp_path):
    adapter = FakeLiveAdapter()
    accounts = FakeAccounts(positions=[FlattenTarget(SYM, "long", 0.01)])
    session, om = wire(live_settings(), FakeFeed([]), adapter, accounts, tmp_path,
                       OneShotLong())
    await session.emergency_flatten()
    assert len(adapter.submitted) == 1
    flat_order = next(iter(om.orders.values()))
    assert flat_order.intent.reduce_only is True


async def test_timeout_order_blocks_new_risk_until_reconciled(tmp_path):
    adapter = FakeLiveAdapter(script=["timeout"])
    feed = FakeFeed([bar(0)])
    session, om = wire(live_settings(), feed, adapter, FakeAccounts(), tmp_path,
                       OneShotLong(at_bar=6))
    await session.run(max_bars=1)
    # the entry timed out -> TIMEOUT_UNKNOWN; but fetch_order_status returns None
    # (never recorded), so reconciler resolves it to REJECTED
    assert not om.has_unresolved_orders  # reconciled at end of run


async def test_timeout_reached_entry_plan_survives_reconciliation(tmp_path):
    adapter = FakeLiveAdapter(script=["timeout_reached"])
    feed = FakeFeed([bar(0)])
    session, om = wire(live_settings(), feed, adapter, FakeAccounts(), tmp_path,
                       OneShotLong(at_bar=6))

    await session.run(max_bars=1)

    assert session.orders_submitted == 1
    assert not om.has_unresolved_orders
    assert session._plan is not None


async def test_live_exit_plan_survives_reject_and_retries_with_new_key(tmp_path):
    adapter = FakeLiveAdapter(script=["reject", "ack"])
    accounts = FakeAccounts(positions=[FlattenTarget(SYM, "long", 0.01)])
    session, om = wire(
        live_settings(),
        FakeFeed([]),
        adapter,
        accounts,
        tmp_path,
        OneShotLong(),
    )
    session._plan = SignalIntent("long", stop_price=95.0, take_profit_price=106.0)
    session._entry_bar_ts = pd.Timestamp(BASE, unit="ms", tz="UTC")

    await session._submit_exit("stop", datetime.now(UTC))

    assert session._plan is not None
    assert session.orders_submitted == 1

    await session._submit_exit("stop", datetime.now(UTC))

    assert session._plan is None
    keys = [o.intent_key for o in om.orders.values() if o.intent.reduce_only]
    assert keys[-1] == keys[-2] + "|retry=1"


async def test_live_timeout_lost_exit_plan_waits_for_reconcile_before_retry(tmp_path):
    adapter = FakeLiveAdapter(script=["timeout", "ack"])
    accounts = FakeAccounts(positions=[FlattenTarget(SYM, "long", 0.01)])
    session, om = wire(
        live_settings(),
        FakeFeed([]),
        adapter,
        accounts,
        tmp_path,
        OneShotLong(),
    )
    session._plan = SignalIntent("long", stop_price=95.0, take_profit_price=106.0)
    session._entry_bar_ts = pd.Timestamp(BASE, unit="ms", tz="UTC")

    await session._submit_exit("stop", datetime.now(UTC))
    await session._submit_exit("stop", datetime.now(UTC))

    assert session._plan is not None
    assert session.orders_submitted == 1

    await session._reconcile()
    await session._submit_exit("stop", datetime.now(UTC))

    assert session._plan is None
    keys = [o.intent_key for o in om.orders.values() if o.intent.reduce_only]
    assert keys[-1] == keys[-2] + "|retry=1"


# --- Gap 2: position-level reconciliation fails closed -----------------------------

async def test_position_mismatch_fails_closed(tmp_path):
    # Flat internally, but the venue reports a position we don't track -> mismatch.
    session, _ = wire(live_settings(), FakeFeed([]), FakeLiveAdapter(),
                      FakeAccounts(positions=[{"contracts": 1.0}]), tmp_path, OneShotLong())
    assert session.entries_allowed and session._plan is None  # clean + flat to start
    await session._reconcile_positions()
    assert session.recon_mismatches == 1
    assert session.entries_allowed is False  # failed closed: entries blocked, exits flow


async def test_position_reconciliation_clears_on_clean_pass(tmp_path):
    session, _ = wire(live_settings(), FakeFeed([]), FakeLiveAdapter(),
                      FakeAccounts(positions=[{"contracts": 1.0}]), tmp_path, OneShotLong())
    await session._reconcile_positions()               # trip the halt
    assert session.entries_allowed is False
    session.accounts._positions = []                   # venue now flat — agrees with internal
    await session._reconcile_positions()               # clean settled pass
    assert session.entries_allowed is True
    assert session.recon_mismatches == 1               # the one real mismatch, not re-counted


async def test_position_recon_skips_while_orders_in_flight(tmp_path):
    # Unsettled state (an exit in flight) must NOT be judged against the venue.
    session, _ = wire(live_settings(), FakeFeed([]), FakeLiveAdapter(),
                      FakeAccounts(positions=[{"contracts": 1.0}]), tmp_path, OneShotLong())
    session._pending_exit_orders["k"] = "coid"
    await session._reconcile_positions()
    assert session.recon_mismatches == 0 and session.entries_allowed is True
