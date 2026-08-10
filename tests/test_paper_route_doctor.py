from datetime import UTC, datetime

from vnedge.research.paper_route_doctor import (
    STATE_JOURNAL_ACTIVE,
    STATE_JOURNAL_STALE,
    STATE_ROUTE_READY_JOURNAL_MISSING,
    STATE_RUNNER_SERVICE_DOWN,
    build_paper_route_doctor,
)


def _activation_row(**overrides):
    row = {
        "lane_key": "alpha|delta|eth|5m",
        "trial_id": "manifest_trial",
        "exchange": "delta_india",
        "symbol": "ETH/USD:USD",
        "timeframe": "5m",
        "strategy_id": "stealth_trail_bbp_v1",
        "activation_state": "PAPER_ROUTE_READY_NO_JOURNAL",
        "route_status": "ROUTE_READY",
        "runtime": {"desired_lane_ids": ["runtime_lane_id"]},
        "evidence": {},
    }
    row.update(overrides)
    return row


def test_route_doctor_uses_runtime_lane_id_for_expected_journal(tmp_path):
    payload = build_paper_route_doctor(
        activation={"rows": [_activation_row()]},
        fleet={"services": [{"name": "multi-lane-shadow", "up": True, "status": "Up"}]},
        journal_dir=tmp_path,
        now=datetime(2026, 7, 27, 0, 0, tzinfo=UTC),
    )

    row = payload["rows"][0]
    assert row["doctor_state"] == STATE_ROUTE_READY_JOURNAL_MISSING
    assert row["expected_lane_id"] == "runtime_lane_id"
    assert row["expected_paths"]["journal"].endswith("runtime_lane_id.journal.jsonl")
    assert payload["summary"]["journal_missing"] == 1
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False


def test_route_doctor_marks_runner_down_before_missing_journal(tmp_path):
    payload = build_paper_route_doctor(
        activation={"rows": [_activation_row()]},
        fleet={"services": [{"name": "multi-lane-shadow", "up": False, "status": "Exited"}]},
        journal_dir=tmp_path,
        now=datetime(2026, 7, 27, 0, 0, tzinfo=UTC),
    )

    row = payload["rows"][0]
    assert row["doctor_state"] == STATE_RUNNER_SERVICE_DOWN
    assert payload["summary"]["runner_down"] == 1
    assert "runner is down" in payload["operator_answer"]


def test_route_doctor_distinguishes_active_and_stale_journals(tmp_path):
    fresh = _activation_row(
        trial_id="fresh_trial",
        runtime={"desired_lane_ids": ["fresh_lane"]},
        evidence={
            "paper_journal": {
                "journal": str(tmp_path / "fresh_lane.journal.jsonl"),
                "last_ts": "2026-07-27T00:00:00+00:00",
                "paper_lane_heartbeats": 2,
                "evals": 1,
            }
        },
    )
    stale = _activation_row(
        trial_id="stale_trial",
        runtime={"desired_lane_ids": ["stale_lane"]},
        evidence={
            "paper_journal": {
                "journal": str(tmp_path / "stale_lane.journal.jsonl"),
                "last_ts": "2026-07-26T18:00:00+00:00",
                "paper_lane_heartbeats": 1,
            }
        },
    )

    payload = build_paper_route_doctor(
        activation={"rows": [fresh, stale]},
        fleet={"services": [{"name": "multi-lane-shadow", "up": True, "status": "Up"}]},
        journal_dir=tmp_path,
        now=datetime(2026, 7, 27, 0, 30, tzinfo=UTC),
    )

    states = {row["trial_id"]: row["doctor_state"] for row in payload["rows"]}
    assert states["fresh_trial"] == STATE_JOURNAL_ACTIVE
    assert states["stale_trial"] == STATE_JOURNAL_STALE
    assert payload["summary"]["journal_active"] == 1
    assert payload["summary"]["journal_stale"] == 1
