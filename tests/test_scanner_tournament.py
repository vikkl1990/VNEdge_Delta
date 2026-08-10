"""Scanner tournament keeps discovery permissive and promotion strict."""

import json
import stat

import pandas as pd

from vnedge.research.execution_edge_router import OpportunityRoute
from vnedge.research.scanner_tournament import (
    build_scanner_tournament_progress,
    build_scanner_tournament_report,
    discovery_relaxed_profile,
    paper_probe_profile,
    publish_progress,
    publish_report,
    scanner_tournament_profile,
    strict_proof_profile,
)
from vnedge.research.universe import ResearchTarget


def route(
    i: int,
    *,
    strategy_id: str,
    net: float,
    timeframe: str = "5m",
    symbol: str = "ETH/USD:USD",
    action: str = "MAKER",
) -> OpportunityRoute:
    ts = pd.Timestamp("2026-07-01T00:00:00Z") + pd.Timedelta(minutes=5 * i)
    return OpportunityRoute(
        event_id=f"{strategy_id}-{i}",
        ts=ts.isoformat(),
        side="long" if i % 2 == 0 else "short",
        source_id="test",
        strategy_id=strategy_id,
        action=action,
        reason="maker route clears relaxed research discovery edge",
        selected_route="MAKER_ONLY" if action != "SKIP" else None,
        selected_net_bps=net if action != "SKIP" else None,
        selected_gross_bps=net + 7.0 if action != "SKIP" else None,
        selected_cost_bps=7.0 if action != "SKIP" else None,
        maker_net_bps=net,
        maker_gross_bps=net + 7.0,
        maker_cost_bps=7.0,
        taker_net_bps=net - 5.0,
        taker_gross_bps=net + 7.0,
        taker_cost_bps=12.0,
        maker_fill_probability=0.55,
        expected_edge_bps=max(net + 7.0, 0.0),
        outcome="target" if net > 0 else "stop",
        mfe_bps=abs(net) + 10.0,
        mae_bps=-8.0,
        risk_bps=20.0,
        metadata={
            "exchange": "delta_india",
            "symbol": symbol,
            "timeframe": timeframe,
            "reason": "score=1.0; features=bbp,trail,structure",
        },
    )


def make_mixed_routes() -> tuple[OpportunityRoute, ...]:
    good = tuple(
        route(i, strategy_id="stealth_trail_bbp_v1", net=18.0 if i % 3 else -4.0)
        for i in range(24)
    )
    weak = tuple(
        route(
            i,
            strategy_id="luxara_live_plan_qtm_v1",
            net=-10.0 if i % 2 else 6.0,
            symbol="BTC/USD:USD",
        )
        for i in range(16)
    )
    strict = tuple(
        route(
            i,
            strategy_id="luxy_ut_bot_forecast_v1",
            net=40.0 if i % 4 else -4.0,
            timeframe="15m",
            symbol="SOL/USD:USD",
        )
        for i in range(28)
    )
    return (*good, *weak, *strict)


def test_discovery_profile_lowers_only_research_thresholds():
    strict = strict_proof_profile()
    relaxed = discovery_relaxed_profile()
    probe = paper_probe_profile()

    assert relaxed.router_config.min_expected_net_edge_bps < strict.router_config.min_expected_net_edge_bps
    assert relaxed.router_config.min_samples < strict.router_config.min_samples
    assert probe.router_config.min_expected_net_edge_bps < strict.router_config.min_expected_net_edge_bps
    assert relaxed.can_trade is False
    assert relaxed.can_promote is False
    assert relaxed.live_governance_unchanged is True
    assert scanner_tournament_profile("discovery_relaxed").lowered_governance_scope == "research_discovery_only"


def test_scanner_tournament_ranks_positive_candidate_without_trade_permission():
    report = build_scanner_tournament_report(
        make_mixed_routes(),
        profile=discovery_relaxed_profile(),
        targets=(ResearchTarget("delta_india", "ETH/USD:USD", "5m"),),
        strategy_ids=("stealth_trail_bbp_v1", "luxara_live_plan_qtm_v1"),
        lookback_days=30,
        data_coverage={"attempted": 1, "available": 1, "missing": 0},
    )

    assert report["truth_layer"] == "scanner_tournament_v1"
    assert report["policy"]["can_trade"] is False
    assert report["policy"]["can_promote"] is False
    assert report["policy"]["live_governance_unchanged"] is True
    assert report["summary"]["research_governance_lowered"] is True
    assert report["summary"]["positive_watchlists"] >= 1

    top = report["candidates"][0]
    assert top["strategy_id"] == "luxy_ut_bot_forecast_v1"
    assert top["verdict"] == "STRICT_PROOF_WATCHLIST"
    assert top["can_trade"] is False
    assert top["can_promote"] is False
    assert top["recommended_action"] == "PRE_REGISTER_UNTOUCHED_JUDGMENT_WINDOW"


def test_publish_report_writes_atomic_latest_and_feed(tmp_path):
    report = build_scanner_tournament_report(
        make_mixed_routes(),
        profile=discovery_relaxed_profile(),
        targets=(ResearchTarget("delta_india", "ETH/USD:USD", "5m"),),
        strategy_ids=("stealth_trail_bbp_v1",),
        lookback_days=30,
    )
    output = tmp_path / "scanner_tournament_latest.json"
    feed = tmp_path / "scanner_tournament_feed.jsonl"

    publish_report(report, output, feed)

    saved = json.loads(output.read_text())
    feed_rows = [json.loads(line) for line in feed.read_text().splitlines()]
    assert saved["policy"]["lowered_governance_scope"] == "research_discovery_only"
    assert saved["summary"]["can_trade"] is False
    assert feed_rows[-1]["can_promote"] is False
    assert feed_rows[-1]["truth_layer"] == "scanner_tournament_v1"
    assert stat.S_IMODE(output.stat().st_mode) == 0o644
    assert stat.S_IMODE(feed.stat().st_mode) == 0o644


def test_progress_payload_is_visibility_only_and_atomic(tmp_path):
    target = ResearchTarget("delta_india", "ETH/USD:USD", "5m")
    progress = build_scanner_tournament_progress(
        status="running",
        phase="labeling_opportunities",
        started_at="2026-07-19T00:00:00+00:00",
        profile=discovery_relaxed_profile(),
        targets=(target,),
        strategy_ids=("stealth_trail_bbp_v1", "luxara_live_plan_qtm_v1"),
        lookback_days=30,
        completed_work_units=1,
        total_work_units=2,
        current_target=target,
        current_strategy="luxara_live_plan_qtm_v1",
        rows=1440,
        routes=12,
        output_path="research/live_research/scanner_tournament_latest.json",
        stale_after_seconds=900,
    )

    assert progress["truth_layer"] == "scanner_tournament_progress_v1"
    assert progress["progress_pct"] == 50.0
    assert progress["stale_after_seconds"] == 900
    assert progress["current_target"]["exchange"] == "delta_india"
    assert progress["current_strategy"] == "luxara_live_plan_qtm_v1"
    assert progress["can_trade"] is False
    assert progress["can_promote"] is False
    assert progress["policy"]["can_trade"] is False

    output = tmp_path / "scanner_tournament_progress.json"
    publish_progress(progress, output)
    saved = json.loads(output.read_text())
    assert saved["status"] == "running"
    assert saved["current_rows"] == 1440
    assert stat.S_IMODE(output.stat().st_mode) == 0o644
