import json
from datetime import UTC, datetime

from vnedge.research.public_bot_inspiration import (
    PUBLIC_BOT_INSPIRATION_ID,
    SOURCES,
    publish_public_bot_inspiration,
    run_public_bot_inspiration,
)


def test_public_bot_inspiration_reviews_all_requested_links():
    payload = run_public_bot_inspiration(now=datetime(2026, 7, 16, tzinfo=UTC))

    assert payload["audit_id"] == PUBLIC_BOT_INSPIRATION_ID
    assert payload["summary"]["sources_reviewed"] == 15
    assert payload["summary"]["patterns_reviewed"] >= 10
    assert payload["source_scope"]["no_strategy_code_copied"] is True
    assert payload["source_scope"]["not_profit_evidence"] is True

    reviewed_urls = {
        source["url"] for source in payload["source_scope"]["links_reviewed"]
    }
    assert reviewed_urls == {source.url for source in SOURCES}
    assert {
        "https://github.com/DeviaVir/zenbot",
        "https://github.com/freqtrade/freqtrade",
        "https://github.com/Superalgos/Strategy-BTC-BB-Top-Bounce",
    }.issubset(reviewed_urls)


def test_public_bot_inspiration_is_research_only_and_prioritizes_gaps():
    payload = run_public_bot_inspiration(now=datetime(2026, 7, 16, tzinfo=UTC))

    assert payload["policy"]["research_only"] is True
    assert payload["policy"]["can_trade"] is False
    assert payload["policy"]["can_promote"] is False
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False

    top_ids = [row["pattern_id"] for row in payload["top_adaptations"]]
    assert "strategy_benchmark_database" in top_ids[:3]
    assert "mtf_zoom_in_event_chain" in top_ids[:3]
    for row in payload["coverage_matrix"]:
        assert row["can_trade"] is False
        assert row["can_promote"] is False
        assert row["safety_gate"]
        assert set(row["source_names"]).issubset({source.name for source in SOURCES})


def test_public_bot_inspiration_runtime_status_resolves_owned_surfaces():
    payload = run_public_bot_inspiration(now=datetime(2026, 7, 16, tzinfo=UTC))
    by_id = {row["pattern_id"]: row for row in payload["coverage_matrix"]}

    maker_taker = by_id["maker_fee_order_and_taker_fallback"]
    assert maker_taker["runtime_status"] == "covered"
    assert "vnedge.execution.maker_taker_executor" in maker_taker["resolved_surfaces"]

    benchmark = by_id["strategy_benchmark_database"]
    assert benchmark["runtime_status"] == "gap"
    assert benchmark["proposed_build"] == "strategy_benchmark_index_v1"


def test_publish_public_bot_inspiration_writes_latest_and_feed(tmp_path):
    payload = run_public_bot_inspiration(now=datetime(2026, 7, 16, tzinfo=UTC))
    latest = tmp_path / "public_bot_inspiration_latest.json"
    feed = tmp_path / "public_bot_inspiration_feed.jsonl"

    written = publish_public_bot_inspiration(payload, latest, feed)
    publish_public_bot_inspiration(payload, latest, feed)

    assert written == latest
    latest_doc = json.loads(latest.read_text())
    assert latest_doc["summary"]["sources_reviewed"] == 15
    lines = feed.read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["policy"]["can_trade"] is False
