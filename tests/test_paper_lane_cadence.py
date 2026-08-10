import json
from datetime import UTC, datetime

from vnedge.research.paper_lane_cadence import (
    STATE_EVAL_STALE,
    STATE_EVALUATING_NO_SIGNAL,
    STATE_EVALUATING_SIGNAL_SEEN,
    STATE_HEARTBEAT_ONLY_NO_EVAL,
    STATE_JOURNAL_MISSING,
    build_paper_lane_cadence,
)


NOW = datetime(2026, 7, 27, 0, 10, tzinfo=UTC)


def _activation_row(**overrides):
    row = {
        "lane_key": "stealth|delta|eth|5m",
        "trial_id": "manifest_trial",
        "exchange": "delta_india",
        "symbol": "ETH/USD:USD",
        "timeframe": "5m",
        "strategy_id": "stealth_trail_bbp_v1",
        "activation_state": "PAPER_ONLINE_WAITING",
        "route_status": "ROUTE_READY",
        "runtime": {"desired_lane_ids": ["runtime_lane"]},
        "evidence": {},
    }
    row.update(overrides)
    return row


def _record(kind, payload, ts):
    return json.dumps({"ts": ts, "kind": kind, "payload": payload})


def _write_journal(path, rows):
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def test_cadence_marks_recent_live_eval_no_signal(tmp_path):
    _write_journal(
        tmp_path / "runtime_lane.journal.jsonl",
        [
            _record(
                "lane_eval",
                {
                    "strategy_id": "stealth_trail_bbp_v1",
                    "exchange": "delta_india",
                    "symbol": "ETH/USD:USD",
                    "timeframe": "5m",
                    "bar_ts": "2026-07-27T00:05:00+00:00",
                    "fired": False,
                    "skip_reason": "no displacement",
                },
                "2026-07-27T00:08:00+00:00",
            )
        ],
    )

    payload = build_paper_lane_cadence(
        activation={"rows": [_activation_row()]},
        journal_dir=tmp_path,
        now=NOW,
    )

    row = payload["rows"][0]
    assert row["cadence_state"] == STATE_EVALUATING_NO_SIGNAL
    assert row["counts"]["live_evals"] == 1
    assert row["expected_eval_seconds"] == 750
    assert payload["summary"]["cadence_ok"] == 1
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False


def test_cadence_marks_signal_seen(tmp_path):
    _write_journal(
        tmp_path / "runtime_lane.journal.jsonl",
        [
            _record(
                "lane_eval",
                {
                    "strategy_id": "stealth_trail_bbp_v1",
                    "exchange": "delta_india",
                    "symbol": "ETH/USD:USD",
                    "timeframe": "5m",
                    "bar_ts": "2026-07-27T00:05:00+00:00",
                    "fired": True,
                    "signal_reason": "bbp displacement",
                },
                "2026-07-27T00:08:00+00:00",
            )
        ],
    )

    payload = build_paper_lane_cadence(
        activation={"rows": [_activation_row()]},
        journal_dir=tmp_path,
        now=NOW,
    )

    assert payload["rows"][0]["cadence_state"] == STATE_EVALUATING_SIGNAL_SEEN
    assert payload["summary"]["signals_seen"] == 1


def test_cadence_marks_heartbeat_without_eval(tmp_path):
    _write_journal(
        tmp_path / "runtime_lane.journal.jsonl",
        [
            _record(
                "paper_lane_heartbeat",
                {
                    "strategy_id": "stealth_trail_bbp_v1",
                    "exchange": "delta_india",
                    "symbol": "ETH/USD:USD",
                    "timeframe": "5m",
                    "why_no_trade": "waiting for candle close",
                },
                "2026-07-27T00:09:00+00:00",
            )
        ],
    )

    payload = build_paper_lane_cadence(
        activation={"rows": [_activation_row()]},
        journal_dir=tmp_path,
        now=NOW,
    )

    row = payload["rows"][0]
    assert row["cadence_state"] == STATE_HEARTBEAT_ONLY_NO_EVAL
    assert row["counts"]["heartbeats"] == 1
    assert payload["summary"]["heartbeat_only"] == 1


def test_cadence_marks_stale_eval(tmp_path):
    _write_journal(
        tmp_path / "runtime_lane.journal.jsonl",
        [
            _record(
                "lane_eval",
                {
                    "strategy_id": "stealth_trail_bbp_v1",
                    "exchange": "delta_india",
                    "symbol": "ETH/USD:USD",
                    "timeframe": "5m",
                    "fired": False,
                },
                "2026-07-26T23:00:00+00:00",
            )
        ],
    )

    payload = build_paper_lane_cadence(
        activation={"rows": [_activation_row()]},
        journal_dir=tmp_path,
        now=NOW,
    )

    assert payload["rows"][0]["cadence_state"] == STATE_EVAL_STALE
    assert payload["summary"]["stale"] == 1


def test_cadence_marks_active_route_missing_journal(tmp_path):
    payload = build_paper_lane_cadence(
        activation={"rows": [_activation_row()]},
        journal_dir=tmp_path,
        now=NOW,
    )

    row = payload["rows"][0]
    assert row["cadence_state"] == STATE_JOURNAL_MISSING
    assert row["expected_lane_id"] == "runtime_lane"
    assert payload["summary"]["journal_missing"] == 1
