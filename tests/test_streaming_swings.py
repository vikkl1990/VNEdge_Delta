from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from vnedge.scalping.delta_engine.streaming_swings import IncrementalSwingTracker
from vnedge.scalping.delta_engine.types import Candle

START = datetime(2025, 1, 1, tzinfo=UTC)


def _bar(index: int, high: float, low: float, timeframe: str = "1h") -> Candle:
    mid = (high + low) / 2.0
    return Candle(START + timedelta(hours=index + 1), mid, high, low, mid, 1.0, timeframe)


def test_incremental_tracker_waits_for_every_right_hand_bar():
    tracker = IncrementalSwingTracker("1h", left=3, right=3, min_swing_bps=0.0)
    rows = [
        _bar(index, high, high - 2.0)
        for index, high in enumerate((10.0, 11.0, 12.0, 20.0, 12.0, 11.0, 10.0))
    ]

    for row in rows[:-1]:
        assert tracker.update(row) == ()
    confirmed = tracker.update(rows[-1])

    assert len(confirmed) == 1
    assert confirmed[0].kind == "high"
    assert confirmed[0].pivot_index == 3
    assert confirmed[0].confirmed_index == 6
    assert confirmed[0].ts == rows[3].ts
    assert confirmed[0].confirmed_at == rows[6].ts


def test_local_candle_range_strength_is_explicit_and_filterable():
    rows = (
        _bar(0, 100.05, 99.98),
        _bar(1, 100.10, 100.00),
        _bar(2, 100.06, 100.01),
    )
    accepted = IncrementalSwingTracker("1h", left=1, right=1, min_swing_bps=8.0)
    rejected = IncrementalSwingTracker("1h", left=1, right=1, min_swing_bps=12.0)

    accepted_update = tuple(item for row in rows for item in accepted.update(row))
    rejected_update = tuple(item for row in rows for item in rejected.update(row))

    assert len(accepted_update) == 1
    assert accepted_update[0].strength_bps == pytest.approx(9.995, rel=1e-3)
    assert rejected_update == ()


def test_more_extreme_same_type_swing_replaces_active_but_remains_in_archive():
    tracker = IncrementalSwingTracker("1h", left=1, right=1, min_swing_bps=0.0)
    highs = (100.0, 110.0, 100.0, 120.0, 100.0)
    lows = (90.0, 91.0, 92.0, 93.0, 94.0)
    updates = [item for index in range(5) for item in tracker.update(_bar(index, highs[index], lows[index]))]

    assert [swing.price for swing in updates] == [110.0, 120.0]
    assert [swing.price for swing in tracker.swings] == [120.0]
    assert [swing.price for swing in tracker.confirmed_archive] == [110.0, 120.0]
    assert tracker.last_swing("high").pivot_index == 3
    assert tracker.last_swing("high").confirmed_index == 4


def test_weaker_same_type_swing_is_not_reported_as_newly_confirmed():
    tracker = IncrementalSwingTracker("1h", left=1, right=1, min_swing_bps=0.0)
    highs = (100.0, 110.0, 100.0, 105.0, 100.0)
    lows = (90.0, 91.0, 92.0, 93.0, 94.0)
    updates = [item for index in range(5) for item in tracker.update(_bar(index, highs[index], lows[index]))]

    assert [swing.price for swing in updates] == [110.0]
    assert [swing.price for swing in tracker.swings] == [110.0]
    assert [swing.price for swing in tracker.confirmed_archive] == [110.0]


def test_tracker_rejects_wrong_timeframe_and_regressing_timestamp():
    tracker = IncrementalSwingTracker("1h", left=1, right=1)
    first = _bar(0, 101.0, 99.0)
    tracker.update(first)

    with pytest.raises(ValueError, match="timeframe"):
        tracker.update(_bar(1, 101.0, 99.0, "15m"))
    with pytest.raises(ValueError, match="ascending"):
        tracker.update(first)


def test_active_swings_adapt_to_existing_bos_choch_contract():
    tracker = IncrementalSwingTracker("1h", left=1, right=1, min_swing_bps=0.0)
    highs = (100.0, 110.0, 100.0)
    lows = (90.0, 91.0, 92.0)
    for index in range(3):
        tracker.update(_bar(index, highs[index], lows[index]))

    adapted = tracker.confirmed_for_structure()

    assert len(adapted) == 1
    assert adapted[0].kind == "high"
    assert adapted[0].candle_index == 1
    assert adapted[0].confirmed_at == _bar(2, 100.0, 92.0).ts

