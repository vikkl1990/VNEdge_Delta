"""LaneFunnelStore: the funnel counters must resume across restarts instead of
resetting to 0, and never bleed one lane's counters into another."""

from __future__ import annotations

import json

from vnedge.runtime.funnel_store import _COUNTER_FIELDS, LaneFunnelStore


class _Session:
    """Minimal stand-in carrying the counter attributes the store reads/writes."""

    def __init__(self, **overrides):
        for field in _COUNTER_FIELDS:
            setattr(self, field, 0)
        self.last_fired_ts = None
        for key, value in overrides.items():
            setattr(self, key, value)


def test_funnel_store_resumes_counters_across_restart(tmp_path):
    live = _Session(
        bars_processed=10, evals=27, live_evals=20, live_signals=3,
        signals=3, shadow_approved=5, last_fired_ts="2026-07-25T12:00:00+00:00",
    )
    store = LaneFunnelStore(tmp_path / "lane-a.funnel.json", "lane-a")
    store.save_from(live)

    # a fresh process starts every counter at 0 …
    restarted = _Session()
    assert restarted.evals == 0
    # … and the store resumes them instead of leaving them zeroed
    assert store.restore_into(restarted) is True
    assert restarted.evals == 27
    assert restarted.live_signals == 3
    assert restarted.shadow_approved == 5
    assert restarted.last_fired_ts == "2026-07-25T12:00:00+00:00"


def test_funnel_store_refuses_to_mix_lanes(tmp_path):
    store_a = LaneFunnelStore(tmp_path / "shared.funnel.json", "lane-a")
    store_a.save_from(_Session(evals=99))
    # a store pointed at the same file but a different lane id must NOT adopt
    # lane-a's counters (a moved/renamed file is ignored, not trusted)
    other = _Session()
    assert LaneFunnelStore(tmp_path / "shared.funnel.json", "lane-b").restore_into(other) is False
    assert other.evals == 0


def test_funnel_store_missing_or_corrupt_is_not_fatal(tmp_path):
    missing = _Session()
    assert LaneFunnelStore(tmp_path / "absent.funnel.json", "x").restore_into(missing) is False
    assert missing.evals == 0

    bad = tmp_path / "corrupt.funnel.json"
    bad.write_text("{not json")
    corrupt = _Session()
    assert LaneFunnelStore(bad, "x").restore_into(corrupt) is False  # tolerated, not raised


def test_funnel_store_writes_atomically_and_is_readable(tmp_path):
    path = tmp_path / "lane.funnel.json"
    LaneFunnelStore(path, "lane").save_from(_Session(evals=5, live_signals=1))
    payload = json.loads(path.read_text())
    assert payload["lane_id"] == "lane"
    assert payload["counters"]["evals"] == 5
    assert not (tmp_path / "lane.funnel.json.tmp").exists()  # tmp renamed away
