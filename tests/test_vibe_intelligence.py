"""Persistent Vibe Intelligence lifecycle tests."""

import json

from vnedge.research.vibe_intelligence import (
    build_hypothesis_cards,
    publish_vibe_intelligence,
    run_vibe_intelligence,
)


def debate(
    action,
    candidate_id,
    *,
    source="event_leadlag_alpha",
    priority=88.0,
    verdict="HIGH_PRIORITY_RESEARCH",
    vetoes=None,
    evidence=None,
):
    return {
        "candidate": {
            "candidate_id": candidate_id,
            "source": source,
            "family": "cross_venue_event_leadlag_v1",
            "exchange": "delta_india",
            "symbol": "SOL/USD:USD",
            "timeframe": "15m",
            "state": "EDGE_CANDIDATE_MAKER",
            "route_decision": "MAKER_ONLY",
            "metrics": {"samples": 31, "maker_avg_net_bps": 9.4},
            "evidence": evidence or {"hypothesis_id": candidate_id, "samples": 31},
        },
        "priority_score": priority,
        "council_verdict": verdict,
        "next_action": action,
        "can_trade": False,
        "can_promote": False,
        "vetoes": vetoes or ["requires_conservative_l2_replay", "maker_fill_unproven"],
        "debate": [
            {
                "agent_id": "research_director",
                "argument": "run the next proof step before any shadow discussion",
            }
        ],
    }


def council_payload(*rows):
    return {
        "generated_at": "2026-07-14T00:00:00+00:00",
        "council_id": "alpha_agent_council_v1",
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
        "debates": list(rows),
    }


def test_vibe_intelligence_builds_active_hypothesis_from_council_and_workbench(tmp_path):
    payload = council_payload(
        debate(
            "RUN_CONSERVATIVE_L2_REPLAY",
            "event_leadlag|SOL|binanceusdm->delta_india",
        )
    )

    report = run_vibe_intelligence(tmp_path, store_dir=None, council_payload=payload)

    assert report["can_trade"] is False
    assert report["can_promote"] is False
    assert report["policy"]["can_trade"] is False
    assert report["policy"]["can_promote"] is False
    assert report["summary"]["active"] == 1
    card = report["cards"][0]
    assert card["lifecycle_state"] == "ACTIVE"
    assert card["workbench_task_id"]
    assert card["task_type"] == "conservative_replay"
    assert "conservative_replay_result" in card["blocked_by"]
    assert card["can_trade"] is False
    assert card["live_orders_enabled"] is False


def test_vibe_intelligence_marks_execution_replay_failures_as_decayed(tmp_path):
    payload = council_payload(
        debate(
            "MINE_PRE_EVENT_EXECUTION_CONDITIONS",
            "event_leadlag|XRP|binanceusdm->delta_india|15m",
            priority=42.0,
            verdict="EXECUTION_REPLAY_FAILED",
            vetoes=["maker_fill_failed", "execution_replay_failed"],
        )
    )

    cards = build_hypothesis_cards(
        payload,
        {"tasks": []},
        previous_records={},
    )

    assert len(cards) == 1
    assert cards[0].lifecycle_state == "DECAYED"
    assert cards[0].decay_score >= 60
    assert cards[0].health_score < 50
    assert cards[0].can_promote is False


def test_vibe_intelligence_routes_shadow_queue_to_monitoring(tmp_path):
    payload = council_payload(
        debate(
            "QUEUE_SHADOW_TRIAL_AFTER_REPLAY",
            "orderflow_footprint|delta_india|SOL/USD:USD|20260706|1000|buy",
            source="orderflow_footprint",
            priority=91.0,
            vetoes=["requires_shadow_trial_after_replay"],
        )
    )

    report = run_vibe_intelligence(tmp_path, store_dir=None, council_payload=payload)

    card = report["cards"][0]
    assert card["lifecycle_state"] == "MONITORING"
    assert card["task_type"] == "shadow_trial_after_replay"
    assert "human_approved_shadow_manifest" in card["blocked_by"]
    assert report["summary"]["monitoring"] == 1


def test_vibe_intelligence_persistence_keeps_stable_hypothesis_memory(tmp_path):
    payload = council_payload(
        debate(
            "RUN_CONSERVATIVE_L2_REPLAY",
            "event_leadlag|SOL|binanceusdm->delta_india",
        )
    )
    store = tmp_path / "vibe_intelligence"

    first = run_vibe_intelligence(tmp_path, store_dir=store, council_payload=payload)
    second = run_vibe_intelligence(tmp_path, store_dir=store, council_payload=payload)

    assert first["persistence"]["new_hypotheses"] == 1
    assert second["persistence"]["new_hypotheses"] == 0
    assert second["persistence"]["unchanged_hypotheses"] == 1
    assert second["cards"][0]["times_seen"] == 2
    chunks = list((store / "chunks").glob("*.json"))
    assert len(chunks) == 1
    manifest = json.loads((store / "manifest.json").read_text())
    record = next(iter(manifest["hypotheses"].values()))
    assert record["times_seen"] == 2
    assert record["can_trade"] is False


def test_vibe_intelligence_disables_repeated_decayed_hypotheses(tmp_path):
    payload = council_payload(
        debate(
            "MINE_PRE_EVENT_EXECUTION_CONDITIONS",
            "event_leadlag|XRP|binanceusdm->delta_india|15m",
            priority=42.0,
            verdict="EXECUTION_REPLAY_FAILED",
            vetoes=["maker_fill_failed", "execution_replay_failed"],
        )
    )
    previous = {
        "event_leadlag|XRP|binanceusdm->delta_india|15m": {
            "times_seen": 4,
            "decay_observations": 2,
            "lifecycle_state": "DECAYED",
        }
    }

    cards = build_hypothesis_cards(payload, {"tasks": []}, previous_records=previous)

    assert cards[0].lifecycle_state == "DISABLED"
    assert cards[0].times_seen == 5


def test_publish_vibe_intelligence_writes_latest_and_feed(tmp_path):
    payload = {"intelligence_id": "vibe_intelligence_v1", "summary": {"hypotheses": 0}}
    latest = tmp_path / "latest.json"
    feed = tmp_path / "feed.jsonl"

    publish_vibe_intelligence(payload, latest, feed)

    assert json.loads(latest.read_text()) == payload
    assert json.loads(feed.read_text().strip()) == payload
