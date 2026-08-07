from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np

from vnedge.research.btc_eth_lead_lag_causal_discovery import (
    ReturnFrame,
    benjamini_hochberg,
    granger_window,
    load_discovery_config,
    synchronized_returns,
)
from vnedge.scalping.delta_engine.types import Candle

CONFIG = "configs/research/btc_eth_lead_lag_causal_discovery_v1.yaml"


def _candle(ts: datetime, close: float) -> Candle:
    return Candle(ts, close, close, close, close, 1.0, "1m")


def test_contract_cannot_open_tail_or_authorize_scanner():
    config = load_discovery_config(CONFIG)

    assert config.research_only is True
    assert config.can_trade is False
    assert config.can_promote is False
    assert config.data.end < config.data.sealed_tail_start
    assert config.data.sealed_tail_access_forbidden is True
    assert config.advancement.scanner_backtest_authorized is False
    assert config.advancement.old_v1_tail_may_open is False


def test_synchronized_returns_do_not_cross_a_missing_minute():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    offsets = [0, 1, 2, 4, 5, 6]
    btc = [_candle(start + timedelta(minutes=i), 100.0 + i) for i in offsets]
    eth = [_candle(start + timedelta(minutes=i), 200.0 + i) for i in offsets]

    frame = synchronized_returns(btc, eth, 1)

    assert len(frame.btc) == 4
    assert len(np.unique(frame.segment)) == 2


def test_five_minute_returns_require_complete_buckets():
    start = datetime(2026, 1, 1, 0, 1, tzinfo=UTC)
    btc = [_candle(start + timedelta(minutes=i), 100.0 + i) for i in range(15)]
    eth = [_candle(start + timedelta(minutes=i), 200.0 + i) for i in range(15)]
    del btc[6]

    frame = synchronized_returns(btc, eth, 5)

    assert len(frame.btc) == 0


def test_benjamini_hochberg_is_monotone_in_rank():
    adjusted = benjamini_hochberg([0.01, 0.04, 0.02, 0.20])

    assert adjusted[0] <= adjusted[2] <= adjusted[1] <= adjusted[3]
    assert all(0.0 <= value <= 1.0 for value in adjusted)


def test_granger_window_detects_synthetic_btc_to_eth_precedence():
    rng = np.random.default_rng(42)
    count = 8000
    btc = rng.normal(0.0, 1.0, count)
    eth = np.zeros(count)
    for index in range(1, count):
        eth[index] = 0.8 * btc[index - 1] + rng.normal(0.0, 0.35)
    start = datetime(2025, 1, 1, tzinfo=UTC)
    frame = ReturnFrame(
        timestamps=np.array(
            [start.replace(tzinfo=None) + timedelta(minutes=i) for i in range(count)],
            dtype="datetime64[ns]",
        ),
        btc=btc,
        eth=eth,
        segment=np.zeros(count, dtype=np.int64),
    )

    forward = granger_window(frame, "btc_to_eth", 1, start, start + timedelta(days=6), 0.7, 1000)
    reverse = granger_window(frame, "eth_to_btc", 1, start, start + timedelta(days=6), 0.7, 1000)

    assert forward is not None and reverse is not None
    assert float(forward["p_value"]) < 1e-10
    assert float(forward["oos_mse_improvement_pct"]) > 50.0
    assert float(reverse["oos_mse_improvement_pct"]) < 1.0
