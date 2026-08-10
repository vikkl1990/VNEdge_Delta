"""Public indicator uplift audit: public concepts, VNEDGE gates."""

import json
from datetime import UTC, datetime

from vnedge.research.public_indicator_uplift import (
    PUBLIC_INDICATOR_UPLIFT_ID,
    publish_public_indicator_uplift,
    run_public_indicator_uplift,
)
from vnedge.strategy.alpha_distillation_pack import concept_inventory


def test_willy_uplift_reviews_every_willy_concept_without_trading_permission():
    payload = run_public_indicator_uplift(now=datetime(2026, 7, 13, tzinfo=UTC))
    willy_count = sum(
        1 for row in concept_inventory() if row["vendor_family"] == "WillyAlgo"
    )

    assert payload["audit_id"] == PUBLIC_INDICATOR_UPLIFT_ID
    assert payload["summary"]["concepts_reviewed"] == willy_count
    assert len(payload["assessments"]) == willy_count
    assert payload["policy"]["can_trade"] is False
    assert payload["policy"]["can_promote"] is False
    assert payload["source_scope"]["no_pine_or_proprietary_logic_copied"] is True
    assert {row["can_trade"] for row in payload["assessments"]} == {False}


def test_uplift_routes_surface_the_real_missing_builds():
    payload = run_public_indicator_uplift()
    by_name = {row["concept"]: row for row in payload["assessments"]}

    assert by_name["FVG Retest Engine / SMC Strategy"]["uplift_route"] == (
        "ADD_STATEFUL_IMBALANCE_LIFECYCLE"
    )
    assert by_name["Volume-Weighted S/R Zones"]["uplift_route"] == (
        "ADD_VOLUME_ZONE_REACTION_QUALITY"
    )
    assert by_name["Adaptive Fibonacci Trailing System"]["uplift_route"] == (
        "ADD_FIB_CONTEXT_TAG_ONLY"
    )
    assert by_name["Smart Breakout Targets"]["uplift_route"] == (
        "ADD_TARGET_ROOM_AND_BREAKOUT_QUALITY"
    )
    assert by_name["Liquidity Pools Pro"]["uplift_route"] == (
        "EXTEND_LIQUIDITY_POOL_LIFECYCLE"
    )


def test_coverage_matrix_points_to_existing_vnedge_atoms():
    payload = run_public_indicator_uplift()
    matrix = payload["coverage_matrix"]

    assert "vnedge.strategy.quant_signal_pack" in matrix["fvg_retest"]["existing_modules"]
    assert "vnedge.strategy.volume_profile" in matrix["profile_reclaim"]["existing_modules"]
    assert matrix["trend_trail"]["concept_count"] >= 5
    assert payload["summary"]["high_priority_uplifts"] >= 6


def test_publish_public_indicator_uplift_is_atomic_and_appends_feed(tmp_path):
    payload = run_public_indicator_uplift(now=datetime(2026, 7, 13, tzinfo=UTC))
    out = tmp_path / "public_indicator_uplift_latest.json"
    feed = tmp_path / "public_indicator_uplift_feed.jsonl"

    publish_public_indicator_uplift(payload, out, feed)
    publish_public_indicator_uplift(payload, out, feed)

    assert json.loads(out.read_text())["audit_id"] == PUBLIC_INDICATOR_UPLIFT_ID
    assert not list(tmp_path.glob("*.tmp"))
    assert len(feed.read_text().strip().splitlines()) == 2
