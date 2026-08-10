"""Competitive crypto-bot capability radar."""

from datetime import UTC, datetime
import json

from vnedge.research.bot_capability_radar import (
    BOT_CAPABILITY_RADAR_ID,
    AWESOME_BOTS_SOURCE,
    peer_catalog,
    publish_bot_capability_radar,
    run_bot_capability_radar,
)


def test_bot_capability_radar_is_research_only_and_source_scoped():
    payload = run_bot_capability_radar(now=datetime(2026, 7, 9, tzinfo=UTC))

    assert payload["radar_id"] == BOT_CAPABILITY_RADAR_ID
    assert payload["source"]["url"] == AWESOME_BOTS_SOURCE
    assert payload["source"]["not_profit_evidence"] is True
    assert payload["policy"]["can_trade"] is False
    assert payload["policy"]["can_promote"] is False
    assert payload["policy"]["live_orders_enabled"] is False
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False
    assert payload["summary"]["capabilities"] >= 6


def test_market_making_quote_lifecycle_is_top_scalper_gap():
    payload = run_bot_capability_radar()
    top_builds = payload["top_builds"]
    by_id = {row["capability_id"]: row for row in payload["capabilities"]}

    assert top_builds[0]["capability_id"] == "maker_quote_lifecycle_engine"
    assert by_id["maker_quote_lifecycle_engine"]["status"] == "partial"
    assert by_id["maker_quote_lifecycle_engine"]["priority_score"] >= 90
    assert by_id["maker_quote_lifecycle_engine"]["should_feed_signal_funnel"] is True
    assert "cancel/replace" in by_id["maker_quote_lifecycle_engine"]["next_build"]
    assert "hummingbot" in by_id["maker_quote_lifecycle_engine"]["inspired_by"]
    assert by_id["portfolio_bot_modes"]["status"] == "watchlist"
    assert by_id["portfolio_bot_modes"]["priority_score"] < 40


def test_status_overrides_recompute_priorities_without_trading_permission():
    payload = run_bot_capability_radar(
        status_overrides={
            "maker_quote_lifecycle_engine": "covered",
            "strategy_sandbox_isolation": "covered",
        }
    )
    by_id = {row["capability_id"]: row for row in payload["capabilities"]}

    assert by_id["maker_quote_lifecycle_engine"]["status"] == "covered"
    assert by_id["maker_quote_lifecycle_engine"]["priority_score"] < 80
    assert payload["top_builds"][0]["capability_id"] != "maker_quote_lifecycle_engine"
    assert payload["can_trade"] is False


def test_peer_catalog_keeps_architecture_patterns_not_profit_claims():
    peers = peer_catalog()

    assert any(peer.name == "freqtrade" for peer in peers)
    assert any(peer.name == "k" and peer.archetype == "low_latency_market_making"
               for peer in peers)
    assert {peer.source_use for peer in peers} == {"architecture_pattern_only"}


def test_publish_bot_capability_radar_is_atomic_and_appends_feed(tmp_path):
    payload = run_bot_capability_radar()
    out = tmp_path / "bot_capability_radar_latest.json"
    feed = tmp_path / "bot_capability_radar_feed.jsonl"

    publish_bot_capability_radar(payload, out, feed)
    publish_bot_capability_radar(payload, out, feed)

    assert json.loads(out.read_text())["radar_id"] == BOT_CAPABILITY_RADAR_ID
    assert not list(tmp_path.glob("*.tmp"))
    assert len(feed.read_text().strip().splitlines()) == 2
