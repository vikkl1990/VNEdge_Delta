"""Order manager pipeline — journaling order, duplicates, TIMEOUT_UNKNOWN policy."""

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from vnedge.config.risk_config import RiskConfig
from vnedge.execution.idempotency import (
    IntentRegistry,
    make_intent_key,
    mint_client_order_id,
)
from vnedge.execution.journal import DecisionJournal
from vnedge.execution.order_manager import (
    AdapterRejection,
    AdapterTimeout,
    OrderManager,
)
from vnedge.execution.order_state import OrderState as S
from vnedge.risk.kill_switch import KillSwitch
from vnedge.risk.risk_manager import AccountState, MarketState, OrderIntent, PreTradeRiskGateway


# --- Fakes / fixtures -----------------------------------------------------------

class AckAdapter:
    """Acks everything; records the journal kinds visible at submit time so
    tests can prove intent-journaling happens BEFORE submission."""

    def __init__(self, journal: DecisionJournal):
        self.journal = journal
        self.submissions: list[tuple[str, list[str]]] = []

    async def submit_order(self, order):
        kinds = [r["kind"] for r in self.journal.read_all()]
        self.submissions.append((order.client_order_id, kinds))
        return f"ex_{len(self.submissions)}"


class TimeoutAdapter:
    async def submit_order(self, order):
        raise AdapterTimeout("no ack within deadline")


class RejectAdapter:
    async def submit_order(self, order):
        raise AdapterRejection("insufficient margin at venue")


@pytest.fixture
def journal(tmp_path):
    return DecisionJournal(tmp_path / "journal.jsonl")


@pytest.fixture
def gateway(tmp_path):
    return PreTradeRiskGateway(RiskConfig(), KillSwitch(kill_file=tmp_path / "KILL"))


def intent(**overrides) -> OrderIntent:
    defaults = dict(
        symbol="BTC/USDT:USDT", side="long", quantity=0.001,
        notional_usd=110.0, leverage=3.0, reduce_only=False, strategy_id="test",
    )
    defaults.update(overrides)
    return OrderIntent(**defaults)


def account(**overrides) -> AccountState:
    defaults = dict(
        equity_usd=800.0, daily_pnl_usd=-5.0, peak_equity_usd=850.0,
        open_positions=0, exposure_by_symbol_usd={}, total_exposure_usd=0.0,
    )
    defaults.update(overrides)
    return AccountState(**defaults)


def market() -> MarketState:
    return MarketState(
        symbol="BTC/USDT:USDT",
        last_update=datetime.now(UTC) - timedelta(seconds=1),
        spread_bps=1.0, estimated_slippage_bps=2.0,
        funding_rate=0.0001, exchange_healthy=True,
    )


def key(i: int = 0, side: str = "long") -> str:
    return make_intent_key("test", "BTC/USDT:USDT", side,
                           pd.Timestamp(1_750_000_000_000 + i * 3_600_000, unit="ms", tz="UTC"))


# --- Idempotency primitives -------------------------------------------------------

def test_intent_key_is_deterministic():
    assert key(1) == key(1)
    assert key(1) != key(2)


def test_client_order_ids_are_unique():
    ids = {mint_client_order_id() for _ in range(200)}
    assert len(ids) == 200


def test_registry_blocks_duplicates():
    reg = IntentRegistry()
    assert reg.register("k1", "oid1")
    assert not reg.register("k1", "oid2")
    assert reg.existing_order_id("k1") == "oid1"


# --- Pipeline ----------------------------------------------------------------------

async def test_happy_path_acknowledged(journal, gateway):
    adapter = AckAdapter(journal)
    om = OrderManager(gateway, journal, adapter)
    order = await om.submit(intent(), account(), market(), key(0))
    assert order.state is S.ACKNOWLEDGED
    assert order.exchange_order_id == "ex_1"
    kinds = [r["kind"] for r in journal.read_all()]
    assert kinds == ["risk_decision", "order_intent", "order_acknowledged"]


async def test_intent_journaled_before_venue_submission(journal, gateway):
    adapter = AckAdapter(journal)
    om = OrderManager(gateway, journal, adapter)
    await om.submit(intent(), account(), market(), key(0))
    _, kinds_at_submit = adapter.submissions[0]
    assert "order_intent" in kinds_at_submit  # journaled BEFORE the venue call


async def test_risk_rejection_never_reaches_adapter(journal, gateway):
    adapter = AckAdapter(journal)
    om = OrderManager(gateway, journal, adapter)
    bad = intent(leverage=25.0)  # over the 5x cap
    order = await om.submit(bad, account(), market(), key(0))
    assert order.state is S.RISK_REJECTED
    assert adapter.submissions == []


async def test_duplicate_intent_dropped(journal, gateway):
    adapter = AckAdapter(journal)
    om = OrderManager(gateway, journal, adapter)
    first = await om.submit(intent(), account(), market(), key(0))
    second = await om.submit(intent(), account(), market(), key(0))
    assert first.state is S.ACKNOWLEDGED
    assert second.state is S.RISK_REJECTED
    assert "duplicate" in second.history[-1].note
    assert len(adapter.submissions) == 1


async def test_venue_rejection_is_terminal(journal, gateway):
    om = OrderManager(gateway, journal, RejectAdapter())
    order = await om.submit(intent(), account(), market(), key(0))
    assert order.state is S.REJECTED
    assert "insufficient margin" in order.history[-1].note


async def test_timeout_unknown_blocks_new_risk_but_not_exits(journal, gateway):
    om = OrderManager(gateway, journal, TimeoutAdapter())
    stuck = await om.submit(intent(), account(), market(), key(0))
    assert stuck.state is S.TIMEOUT_UNKNOWN
    assert om.has_unresolved_orders

    # new risk-increasing order: refused before the gateway even runs
    om._adapter = AckAdapter(journal)
    blocked = await om.submit(intent(), account(), market(), key(1))
    assert blocked.state is S.RISK_REJECTED
    assert "TIMEOUT_UNKNOWN" in blocked.history[-1].note

    # reduce-only exit: flows through
    exit_order = await om.submit(
        intent(reduce_only=True, side="short"),
        account(open_positions=1), market(), key(2, side="short"),
    )
    assert exit_order.state is S.ACKNOWLEDGED


async def test_reconciliation_unblocks_entries(journal, gateway):
    om = OrderManager(gateway, journal, TimeoutAdapter())
    stuck = await om.submit(intent(), account(), market(), key(0))
    om.begin_reconciliation(stuck.client_order_id)
    om.resolve_order(stuck.client_order_id, S.FILLED, "exchange shows filled")
    assert stuck.state is S.FILLED
    assert not om.has_unresolved_orders

    om._adapter = AckAdapter(journal)
    order = await om.submit(intent(), account(), market(), key(1))
    assert order.state is S.ACKNOWLEDGED


class PlainAckAdapter:
    async def submit_order(self, order):
        return "ex_plain"


async def test_unavailable_journal_means_exits_only(tmp_path, gateway):
    dead_journal = DecisionJournal(tmp_path)  # path IS a directory -> unwritable
    assert not dead_journal.available
    om = OrderManager(gateway, dead_journal, PlainAckAdapter())

    entry = await om.submit(intent(), account(), market(), key(0))
    assert entry.state is S.RISK_REJECTED
    assert "journal unavailable" in entry.history[-1].note

    exit_order = await om.submit(
        intent(reduce_only=True, side="short"),
        account(open_positions=1), market(), key(1, side="short"),
    )
    assert exit_order.state is S.ACKNOWLEDGED  # getting out is never blocked


def test_journal_roundtrip(tmp_path):
    j = DecisionJournal(tmp_path / "j.jsonl")
    assert j.append("test_event", {"a": 1})
    assert j.append("test_event", {"a": 2})
    records = j.read_all()
    assert len(records) == 2
    assert records[1]["payload"]["a"] == 2
    assert records[0]["kind"] == "test_event"
