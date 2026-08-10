"""Live-trader entrypoint — the gate chain must refuse (and build NO live client)
unless all three gates + the fail-closed checklist + credentials are satisfied."""

import asyncio

from vnedge.config.settings import Settings
from vnedge.data.schemas import normalize_candles
from vnedge.runtime.live_trader_main import (
    _EXIT_CHECKLIST,
    _EXIT_GATES,
    _EXIT_OK,
    LiveTraderRunConfig,
    run_live_trader,
)
from vnedge.strategy.base_strategy import BaseStrategy

BASE = 1_750_000_000_000
HOUR = 3_600_000
LIVE_ENV = {
    "trading_mode": "live_small",
    "live_trading_enabled": True,
    "confirm_live_trading": "I_UNDERSTAND_THIS_IS_HIGH_RISK",
}
CFG = LiveTraderRunConfig(exchange="binanceusdm", symbol="BTC/USDT:USDT", timeframe="1h")


class Fac:
    """Records whether it was called; returns a fixed object."""

    def __init__(self, obj):
        self.obj = obj
        self.calls = 0

    def __call__(self, *a, **k):
        self.calls += 1
        return self.obj


class FakeAdapter:
    async def close(self):
        pass


class FakeAccount:
    pass


class FakeFeed:
    def __init__(self):
        self.closed_candles = asyncio.Queue()
        self.started = self.stopped = False

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True


class FakeStrategy(BaseStrategy):
    strategy_id = "funding_mean_reversion_v1"
    warmup_bars = 2

    def prepare(self, candles):
        return candles.copy()

    def signal(self, df, index):
        return None


def _warmup():
    async def load(config, bars):
        return normalize_candles(
            [[BASE + i * HOUR, 100.0, 101.0, 99.0, 100.0, 10.0] for i in range(5)]
        )
    return load


def _live_env(monkeypatch, tmp_path):
    # The same setup the drill's happy path uses to CLEAR the pre-live checklist.
    monkeypatch.setenv("VNEDGE_EXEC_API_KEY", "k")
    monkeypatch.setenv("VNEDGE_EXEC_API_SECRET", "s")
    monkeypatch.setenv("PRE_LIVE_LADDER_ATTESTED", "1")
    monkeypatch.setenv("KILL_FILE", str(tmp_path / "KILL"))
    monkeypatch.setenv("DECISION_JOURNAL", str(tmp_path / "dj.jsonl"))
    monkeypatch.chdir(tmp_path)


def _facs():
    return {
        "adapter_factory": Fac(FakeAdapter()),
        "feed_factory": Fac(FakeFeed()),
        "account_factory": Fac(FakeAccount()),
        "strategy_factory": Fac(FakeStrategy()),
        "warmup_loader": _warmup(),
    }


async def test_refuses_without_three_gates_and_builds_no_live_client(tmp_path, monkeypatch):
    _live_env(monkeypatch, tmp_path)
    facs = _facs()
    code = await run_live_trader(Settings(), CFG, max_bars=0, **facs)  # backtest — gates closed
    assert code == _EXIT_GATES
    # the whole point: NOT ONE live client is constructed when gated out
    assert facs["adapter_factory"].calls == 0
    assert facs["feed_factory"].calls == 0
    assert facs["account_factory"].calls == 0


async def test_refuses_when_checklist_not_cleared(tmp_path, monkeypatch):
    _live_env(monkeypatch, tmp_path)
    monkeypatch.delenv("PRE_LIVE_LADDER_ATTESTED")  # ladder not attested -> checklist fails
    facs = _facs()
    code = await run_live_trader(Settings(**LIVE_ENV), CFG, max_bars=0, **facs)
    assert code == _EXIT_CHECKLIST
    assert facs["adapter_factory"].calls == 0  # still no live client


async def test_all_gates_open_wires_and_runs(tmp_path, monkeypatch):
    _live_env(monkeypatch, tmp_path)
    facs = _facs()
    code = await run_live_trader(Settings(**LIVE_ENV), CFG, max_bars=0, **facs)
    assert code == _EXIT_OK
    # gates open -> every live component is wired, and the feed is started + stopped
    assert facs["adapter_factory"].calls == 1
    assert facs["feed_factory"].calls == 1
    assert facs["account_factory"].calls == 1
    assert facs["strategy_factory"].calls == 1
    assert facs["feed_factory"].obj.started and facs["feed_factory"].obj.stopped
