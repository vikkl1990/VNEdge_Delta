"""Mainnet execution drill — gates, caps, lifecycle, flat-precondition."""

import pytest

from vnedge.config.settings import Settings
from vnedge.execution.journal import DecisionJournal
from vnedge.runtime.execution_drill import (
    _HARD_MAX_DRILL_NOTIONAL,
    DrillConfig,
    run_execution_drill,
)

LIVE_ENV = {
    "trading_mode": "live_small",
    "live_trading_enabled": True,
    "confirm_live_trading": "I_UNDERSTAND_THIS_IS_HIGH_RISK",
}


class FakeAdapter:
    """Happy-path venue: accepts, shows open, cancels clean, stays flat."""

    def __init__(self, *, positions=(), open_orders=(), mid=0.08, equity=250.0):
        self._positions = list(positions)
        self._open_orders = list(open_orders)
        self._mid = mid
        self._equity = equity
        self.submitted = []
        self.cancelled = []
        self.closed = False

    async def fetch_balance(self):
        # A realistic small LIVE account: the drill now routes through the real
        # gateway, whose min_equity gate ($100) correctly rejects sub-$100.
        return {"total_usd": self._equity, "USDT": self._equity}

    async def fetch_positions(self, symbol):
        return self._positions

    async def fetch_open_orders(self, symbol):
        return self._open_orders

    async def fetch_mid_price(self, symbol):
        return self._mid

    def amount_to_precision(self, symbol, amount):
        return float(int(amount))  # whole-unit steps, rounded DOWN

    async def submit_order(self, order):
        self.submitted.append(order)
        return "EX123"

    async def fetch_order_status(self, order):
        return {"status": "open", "filled": 0.0}

    async def cancel_order(self, order):
        self.cancelled.append(order)
        return "canceled"

    async def close(self):
        self.closed = True


def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("VNEDGE_EXEC_API_KEY", "k")
    monkeypatch.setenv("VNEDGE_EXEC_API_SECRET", "s")
    monkeypatch.setenv("PRE_LIVE_LADDER_ATTESTED", "1")
    monkeypatch.setenv("KILL_FILE", str(tmp_path / "KILL"))
    monkeypatch.setenv("DECISION_JOURNAL", str(tmp_path / "dj.jsonl"))
    monkeypatch.chdir(tmp_path)


async def test_drill_refuses_without_three_gates(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    settings = Settings()  # backtest mode — gates closed
    report = await run_execution_drill(
        settings, DrillConfig(exchange_id="binanceusdm"),
        adapter_factory=FakeAdapter,
        journal=DecisionJournal(tmp_path / "drill.jsonl"),
    )
    assert not report.cleared
    assert report.steps[0].name == "live_gates" and not report.steps[0].ok


async def test_drill_happy_path_clears(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    settings = Settings(**LIVE_ENV)
    fake = FakeAdapter()
    report = await run_execution_drill(
        settings, DrillConfig(exchange_id="binanceusdm", order_notional_usd=8.0),
        adapter_factory=lambda: fake,
        journal=DecisionJournal(tmp_path / "drill.jsonl"),
    )
    assert report.cleared, [s for s in report.steps if not s.ok]
    assert len(fake.submitted) == 1
    order = fake.submitted[0]
    assert order.intent.order_type == "limit"
    assert order.intent.limit_price == pytest.approx(0.08 * 0.85)  # 15% below mid
    assert order.intent.notional_usd <= _HARD_MAX_DRILL_NOTIONAL
    assert order.intent.leverage == 1.0
    assert fake.cancelled == fake.submitted
    assert fake.closed


async def test_drill_notional_hard_cap(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    settings = Settings(**LIVE_ENV)
    fake = FakeAdapter(mid=0.08)
    report = await run_execution_drill(
        settings,
        DrillConfig(exchange_id="binanceusdm", order_notional_usd=10_000.0),
        adapter_factory=lambda: fake,
        journal=DecisionJournal(tmp_path / "drill.jsonl"),
    )
    assert report.cleared
    assert fake.submitted[0].intent.notional_usd <= _HARD_MAX_DRILL_NOTIONAL


async def test_drill_refuses_on_existing_exposure(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    settings = Settings(**LIVE_ENV)
    fake = FakeAdapter(positions=[{"contracts": 1.0}])
    report = await run_execution_drill(
        settings, DrillConfig(exchange_id="binanceusdm"),
        adapter_factory=lambda: fake,
        journal=DecisionJournal(tmp_path / "drill.jsonl"),
    )
    assert not report.cleared
    assert fake.submitted == []  # never places an order near real exposure


async def test_drill_blocked_by_checklist(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    monkeypatch.delenv("VNEDGE_EXEC_API_KEY")  # checklist: credentials missing
    settings = Settings(**LIVE_ENV)
    fake = FakeAdapter()
    report = await run_execution_drill(
        settings, DrillConfig(exchange_id="binanceusdm"),
        adapter_factory=lambda: fake,
        journal=DecisionJournal(tmp_path / "drill.jsonl"),
    )
    assert not report.cleared
    assert any(s.name == "pre_live_checklist" and not s.ok for s in report.steps)
    assert fake.submitted == []


async def test_delta_drill_requires_native_read_surfaces(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    settings = Settings(**LIVE_ENV)
    report = await run_execution_drill(
        settings,
        DrillConfig(exchange_id="delta_india", symbol="BTC/USD:USD"),
        journal=DecisionJournal(tmp_path / "drill.jsonl"),
    )
    assert not report.cleared
    check = next(s for s in report.steps if s.name == "delta_native_drill")
    assert not check.ok
    assert "balance" in check.detail and "position" in check.detail


async def test_drill_order_routes_through_the_gateway(tmp_path, monkeypatch):
    # A sub-min-equity account must be REJECTED by the gateway inside the drill —
    # proof the order is evaluated, not bypassed (the invariant Gap 1 restores).
    _env(monkeypatch, tmp_path)
    settings = Settings(**LIVE_ENV)
    fake = FakeAdapter(equity=42.0)  # below the $100 min_equity gate
    report = await run_execution_drill(
        settings, DrillConfig(exchange_id="binanceusdm"),
        adapter_factory=lambda: fake, journal=DecisionJournal(tmp_path / "d.jsonl"),
    )
    assert not report.cleared
    ga = [s for s in report.steps if s.name == "gateway_approved"]
    assert ga and ga[0].ok is False
    assert fake.submitted == []  # rejected before ever reaching the venue


async def test_drill_client_order_id_is_uuid_not_timestamp(tmp_path, monkeypatch):
    # OrderManager mints a uuid client id; the old drill derived it from
    # time.time() (an idempotency-rule violation). Verify it's not numeric.
    _env(monkeypatch, tmp_path)
    settings = Settings(**LIVE_ENV)
    fake = FakeAdapter(equity=250.0)
    await run_execution_drill(
        settings, DrillConfig(exchange_id="binanceusdm"),
        adapter_factory=lambda: fake, journal=DecisionJournal(tmp_path / "d.jsonl"),
    )
    assert len(fake.submitted) == 1
    coid = fake.submitted[0].client_order_id
    assert not coid.replace("drill", "").isdigit()  # not a bare timestamp
    assert "-" in coid or len(coid) >= 16  # uuid-shaped
