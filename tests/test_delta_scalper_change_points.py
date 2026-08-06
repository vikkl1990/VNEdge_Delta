from __future__ import annotations

import numpy as np

from vnedge.research.delta_scalper_change_points import pelt_mean_changes


def test_pelt_finds_known_piecewise_mean_shift():
    rng = np.random.default_rng(7)
    values = np.concatenate(
        (rng.normal(0.0, 0.25, 180), rng.normal(3.0, 0.25, 180))
    )
    changes = pelt_mean_changes(
        values,
        minimum_segment_bars=30,
        penalty_multiplier=8.0,
        candidate_jump_bars=1,
    )

    assert changes
    assert min(abs(change - 180) for change in changes) <= 3


def test_pelt_does_not_invent_change_for_short_series():
    assert (
        pelt_mean_changes(
            [0.0] * 30,
            minimum_segment_bars=20,
            penalty_multiplier=8.0,
        )
        == []
    )
