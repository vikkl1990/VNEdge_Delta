"""Composite signal strategy — many hypotheses, one runtime intent."""

import pandas as pd

from vnedge.execution.signal_arbiter import ArbiterConfig, SignalArbiter
from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent
from vnedge.strategy.composite import CompositeSignalStrategy


class FixedSignalStrategy(BaseStrategy):
    def __init__(self, strategy_id: str, intent: SignalIntent | None, warmup: int = 0):
        self.strategy_id = strategy_id
        self.intent = intent
        self.warmup_bars = warmup

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        out = candles.copy()
        out[f"prepared_{self.strategy_id}"] = True
        return out

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        assert f"prepared_{self.strategy_id}" in df.columns
        return self.intent


def candles() -> pd.DataFrame:
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=3, tz="UTC"),
        "open": [100.0, 100.0, 100.0],
        "high": [101.0, 101.0, 101.0],
        "low": [99.0, 99.0, 99.0],
        "close": [100.0, 100.0, 100.0],
        "volume": [10.0, 10.0, 10.0],
    })


def test_composite_returns_highest_edge_child_signal():
    low = FixedSignalStrategy(
        "low_edge",
        SignalIntent("long", stop_price=99.0, reason="low fired"),
    )
    high = FixedSignalStrategy(
        "high_edge",
        SignalIntent("short", stop_price=101.0, reason="high fired"),
    )
    strategy = CompositeSignalStrategy(
        [low, high],
        SignalArbiter(ArbiterConfig(max_per_symbol=2)),
        symbol="BTC/USDT:USDT",
        candidate_defaults={
            "low_edge": {"expected_edge_bps": 2.0, "expected_cost_bps": 1.0},
            "high_edge": {"expected_edge_bps": 7.0, "expected_cost_bps": 1.0},
        },
    )

    df = strategy.prepare(candles())
    signal = strategy.signal(df, 2)

    assert signal is not None
    assert signal.side == "short"
    assert "high fired" in signal.reason
    assert "arbiter_selected source=high_edge#2" in signal.reason
    assert strategy.last_decision.best is not None
    assert strategy.last_decision.best.strategy_id == "high_edge"


def test_composite_ignores_blocked_route_and_keeps_runtime_contract():
    blocked = FixedSignalStrategy(
        "blocked_edge",
        SignalIntent("long", stop_price=99.0, reason="blocked fired"),
    )
    maker = FixedSignalStrategy(
        "maker_edge",
        SignalIntent("long", stop_price=98.0, reason="maker fired"),
    )
    strategy = CompositeSignalStrategy(
        [blocked, maker],
        SignalArbiter(),
        symbol="ETH/USDT:USDT",
        candidate_defaults={
            "blocked_edge": {
                "expected_edge_bps": 100.0,
                "expected_cost_bps": 1.0,
                "route": "BLOCKED",
            },
            "maker_edge": {
                "expected_edge_bps": 3.0,
                "expected_cost_bps": 1.0,
                "route": "MAKER_ONLY",
            },
        },
    )

    signal = strategy.signal(strategy.prepare(candles()), 2)

    assert signal is not None
    assert signal.stop_price == 98.0
    assert strategy.last_decision.best is not None
    assert strategy.last_decision.best.source_id == "maker_edge#2"
    assert any(r.reason == "route_blocked" for r in strategy.last_decision.rejected)


def test_composite_respects_child_warmup():
    warm = FixedSignalStrategy(
        "warm",
        SignalIntent("long", stop_price=99.0, reason="warm fired"),
        warmup=5,
    )
    ready = FixedSignalStrategy(
        "ready",
        SignalIntent("short", stop_price=101.0, reason="ready fired"),
    )
    strategy = CompositeSignalStrategy(
        [warm, ready],
        SignalArbiter(),
        symbol="BTC/USDT:USDT",
        candidate_defaults={
            "warm": {"expected_edge_bps": 20.0, "expected_cost_bps": 1.0},
            "ready": {"expected_edge_bps": 2.0, "expected_cost_bps": 1.0},
        },
    )

    signal = strategy.signal(strategy.prepare(candles()), 2)

    assert signal is not None
    assert signal.side == "short"
    assert strategy.last_decision.best is not None
    assert strategy.last_decision.best.strategy_id == "ready"
