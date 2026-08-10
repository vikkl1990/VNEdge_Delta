"""Tests for the closed-loop scanner scorecard — deny-by-default selection."""
from __future__ import annotations

import json

from vnedge.research.scanner_scorecard import (
    ScannerScorecard, ScorecardConfig,
    STATUS_ACTIVE, STATUS_REDUCED, STATUS_SUPPRESSED, STATUS_PROBATION,
    sign_permutation_pvalue,
)


def test_consistent_positive_scanner_goes_active():
    card = ScannerScorecard()
    # 30 consistently-positive signals (~+5 bps) — max-possible under sign flips
    v = card.evaluate({"good": [5.0 + (i % 3) for i in range(30)]})["good"]
    assert v.status == STATUS_ACTIVE
    assert v.can_trade
    assert v.expectancy_bps > 2.0
    assert v.perm_p < 0.10


def test_negative_scanner_suppressed():
    card = ScannerScorecard()
    v = card.evaluate({"bad": [-3.0 - (i % 4) for i in range(30)]})["bad"]
    assert v.status == STATUS_SUPPRESSED
    assert not v.can_trade
    assert v.expectancy_bps < 0


def test_high_but_noisy_expectancy_is_NOT_active():
    """The session's core lesson: expectancy alone is not enough — a high mean
    dominated by a couple of large swings must fail the significance gate.
    (+100/-94 x15 => mean +3 bps, but permutation p is large => REDUCED.)"""
    card = ScannerScorecard()
    outcomes = [100.0, -94.0] * 15
    v = card.evaluate({"noisy": outcomes})["noisy"]
    assert v.expectancy_bps >= 2.0            # mean clears the active floor
    assert v.status == STATUS_REDUCED          # but significance gate blocks it
    assert not v.can_trade
    assert v.perm_p >= 0.10


def test_undersampled_is_probation_deny_by_default():
    card = ScannerScorecard()
    v = card.evaluate({"new": [5.0, 6.0, 7.0]})["new"]   # <20 samples
    assert v.status == STATUS_PROBATION
    assert not v.can_trade


def test_unknown_scanner_denied_by_default():
    card = ScannerScorecard()
    card.evaluate({"known": [5.0] * 30})
    assert card.status("never_seen") == STATUS_PROBATION
    assert card.can_trade("never_seen") is False


def test_weak_positive_below_floor_is_reduced():
    card = ScannerScorecard()
    # consistently +0.5 bps: significant direction but below the +2 active floor
    v = card.evaluate({"thin": [0.5] * 30})["thin"]
    assert v.status == STATUS_REDUCED
    assert not v.can_trade


def test_active_scanners_and_can_trade():
    card = ScannerScorecard()
    card.evaluate({
        "win": [5.0] * 30,
        "lose": [-5.0] * 30,
        "thin": [0.4] * 30,
    })
    assert card.active_scanners() == ["win"]
    assert card.can_trade("win")
    assert not card.can_trade("lose")


def test_deterministic():
    a = ScannerScorecard().evaluate({"s": [100.0, -94.0] * 15})["s"]
    b = ScannerScorecard().evaluate({"s": [100.0, -94.0] * 15})["s"]
    assert a.perm_p == b.perm_p and a.status == b.status


def test_permutation_pvalue_bounds():
    # all-positive => observed is the max => tiny p
    p_pos = sign_permutation_pvalue([3.0] * 20, 2000, 1)
    assert p_pos < 0.01
    # symmetric => observed near middle => large p
    p_sym = sign_permutation_pvalue([10.0, -10.0] * 10, 2000, 1)
    assert p_sym > 0.2
    # too few samples => nan
    import math
    assert math.isnan(sign_permutation_pvalue([1.0, 2.0], 2000, 1))


def test_json_serialisation_round_trips_fields():
    card = ScannerScorecard()
    card.evaluate({"win": [5.0] * 30, "lose": [-5.0] * 30})
    doc = json.loads(card.to_json(as_of="2026-07-18T00:00:00Z"))
    assert doc["as_of"] == "2026-07-18T00:00:00Z"
    ids = {v["scanner_id"]: v for v in doc["verdicts"]}
    assert ids["win"]["can_trade"] is True
    assert ids["lose"]["can_trade"] is False
    # sorted by expectancy desc
    assert doc["verdicts"][0]["scanner_id"] == "win"


def test_config_thresholds_respected():
    strict = ScorecardConfig(active_expectancy_bps=10.0)
    card = ScannerScorecard(strict)
    v = card.evaluate({"mid": [5.0] * 30})["mid"]   # +5 bps, below strict 10 floor
    assert v.status == STATUS_REDUCED
