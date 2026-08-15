from datetime import UTC, datetime

import pytest

from vnedge.research.range_compression_early_entry_study import (
    ArmedRange,
    _build_candidate,
    _cross_side,
    _threshold,
)
from vnedge.scalping.delta_engine.range_compression import load_range_compression_config
from vnedge.scalping.delta_engine.types import Candle, ChangePointProfile, Side

CONFIG_PATH = "configs/research/range_compression_breakout_v1.yaml"


def _arm() -> ArmedRange:
    return ArmedRange(
        symbol="BTCUSD",
        locked_ts=datetime(2026, 1, 1, tzinfo=UTC),
        high=100.0,
        low=99.0,
        atr_bps=40.0,
        five_minute_volume_median=100.0,
        five_minute_atr=0.4,
        change_point=ChangePointProfile(
            source_timeframe="5m",
            detector_ready=True,
            bars_since_shift=1,
        ),
    )


def test_boundary_side_is_past_only_and_rejects_ambiguous_bar() -> None:
    config = load_range_compression_config(CONFIG_PATH)
    arm = _arm()
    long_threshold = _threshold(arm, Side.LONG, config)
    long_bar = Candle(
        ts=datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
        open=100.0,
        high=long_threshold,
        low=99.5,
        close=100.01,
        volume=10.0,
        tf="1m",
    )
    assert _cross_side(long_bar, arm, config) is Side.LONG

    ambiguous = Candle(
        ts=datetime(2026, 1, 1, 0, 2, tzinfo=UTC),
        open=99.5,
        high=long_threshold,
        low=_threshold(arm, Side.SHORT, config),
        close=99.5,
        volume=10.0,
        tf="1m",
    )
    assert _cross_side(ambiguous, arm, config) is None


def test_candidate_keeps_scalper_cap_and_fee_wall_target() -> None:
    config = load_range_compression_config(CONFIG_PATH)
    candidate = _build_candidate(
        mode="boundary_stop",
        arm=_arm(),
        side=Side.LONG,
        decision_ts=datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
        reference_price=100.02,
        config=config,
        cost_bps=14.8,
        trigger={"kind": "test"},
    )
    stop_bps = (1.0 - candidate.stop_loss / candidate.entry_price) * 10_000.0
    target_bps = (
        candidate.take_profits[0] / candidate.entry_price - 1.0
    ) * 10_000.0

    assert candidate.time_stop_seconds == 1_800
    assert stop_bps == pytest.approx(15.0)
    assert target_bps == pytest.approx(51.8)
    assert candidate.entry_is_maker is False
    assert candidate.metadata["research_only"] is True
