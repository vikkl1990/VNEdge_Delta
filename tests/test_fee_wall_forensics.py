"""Batch fee-wall forensics preserves sparse edge and exit-failure evidence."""

import json
import stat

import pandas as pd

from vnedge.research.execution_edge_router import OpportunityRoute
from vnedge.research.fee_wall_forensics import (
    DEFAULT_SELECTED_STRATEGIES,
    build_fee_wall_forensics_progress,
    build_fee_wall_forensics_report,
    publish_json,
    append_feed,
)
from vnedge.research.universe import ResearchTarget


def route(
    i: int,
    *,
    strategy_id: str = "luxara_live_plan_qtm_v1",
    exchange: str = "delta_india",
    symbol: str = "ETH/USD:USD",
    timeframe: str = "15m",
    net: float | None = 40.0,
    routed: bool = True,
    mfe_after_cost: float | None = 60.0,
    diagnosis: str = "CAPTURED_AFTER_COST",
) -> OpportunityRoute:
    ts = pd.Timestamp("2026-07-01T00:00:00Z") + pd.Timedelta(minutes=15 * i)
    action = "MAKER" if routed else "SKIP"
    return OpportunityRoute(
        event_id=f"{strategy_id}-{i}",
        ts=ts.isoformat(),
        side="long",
        source_id="test",
        strategy_id=strategy_id,
        action=action,
        reason="maker route clears edge",
        selected_route="MAKER_ONLY" if routed else None,
        selected_net_bps=net if routed else None,
        selected_gross_bps=(net + 7.0) if routed and net is not None else None,
        selected_cost_bps=7.0 if routed else None,
        maker_net_bps=net,
        maker_gross_bps=(net + 7.0) if net is not None else None,
        maker_cost_bps=7.0,
        taker_net_bps=(net - 5.0) if net is not None else None,
        taker_gross_bps=(net + 7.0) if net is not None else None,
        taker_cost_bps=12.0,
        maker_fill_probability=0.60,
        expected_edge_bps=(net + 7.0) if net is not None else None,
        outcome="target" if net is not None and net > 0 else "stop",
        mfe_bps=(mfe_after_cost + 7.0) if mfe_after_cost is not None else None,
        mae_bps=-12.0,
        risk_bps=20.0,
        mfe_after_cost_bps=mfe_after_cost,
        hold_bars=3,
        time_to_mfe_bars=2,
        capture_ratio=0.70,
        exit_diagnosis=diagnosis,
        metadata={
            "exchange": exchange,
            "symbol": symbol,
            "timeframe": timeframe,
            "reason": "features=bbp,trail,structure",
        },
    )


def router_report(
    *,
    routes: tuple[OpportunityRoute, ...],
    exchange: str = "delta_india",
    symbol: str = "ETH/USD:USD",
    timeframe: str = "15m",
    strategy: str = "luxara_live_plan_qtm_v1",
    min_samples: int = 10,
) -> dict:
    from vnedge.research.execution_edge_router import (
        OpportunityRouterConfig,
        build_router_report,
    )

    report = build_router_report(
        exchange=exchange,
        symbol=symbol,
        timeframe=timeframe,
        strategy_id=strategy,
        opportunities=routes,
        config=OpportunityRouterConfig(
            horizon_bars=8,
            min_samples=min_samples,
            min_expected_net_edge_bps=8.0,
            min_profit_factor=1.15,
        ),
    )
    report["opportunity_count"] = len(routes)
    report["opportunities_omitted"] = len(routes)
    report["opportunities"] = []
    return report


def test_fee_wall_forensics_defaults_include_runtime_sniper_families():
    assert "context_scalper_v2" in DEFAULT_SELECTED_STRATEGIES
    assert "quantified_fee_wall_sniper_v1" in DEFAULT_SELECTED_STRATEGIES


def test_fee_wall_forensics_marks_sparse_positive_without_trade_permission():
    sparse = tuple(route(i, net=55.0) for i in range(4))
    negative = tuple(
        route(
            i,
            strategy_id="sats_5m_scalper_v1",
            exchange="binanceusdm",
            symbol="BTC/USDT:USDT",
            net=-8.0,
            mfe_after_cost=-2.0,
            diagnosis="MOVE_NEVER_CLEARED_COST",
        )
        for i in range(12)
    )
    report = build_fee_wall_forensics_report(
        (
            router_report(routes=sparse),
            router_report(
                routes=negative,
                exchange="binanceusdm",
                symbol="BTC/USDT:USDT",
                strategy="sats_5m_scalper_v1",
            ),
        ),
        targets=(ResearchTarget("delta_india", "ETH/USD:USD", "15m"),),
        strategy_ids=("luxara_live_plan_qtm_v1", "sats_5m_scalper_v1"),
        min_samples=10,
    )

    assert report["truth_layer"] == "fee_wall_forensics_v1"
    assert report["policy"]["can_trade"] is False
    assert report["summary"]["positive_avg_net_reports"] == 1
    assert report["summary"]["sample_expansion_candidates"] == 1
    assert report["sample_expansion_candidates"][0]["strategy"] == "luxara_live_plan_qtm_v1"
    assert report["sample_expansion_candidates"][0]["recommended_action"] == (
        "EXPAND_SAMPLE_OR_LOWER_TIMEFRAME_TRIGGER"
    )
    assert report["sample_expansion_candidates"][0]["can_promote"] is False


def test_fee_wall_forensics_marks_exit_salvage_when_move_exists_but_exit_fails():
    gave_back = tuple(
        route(
            i,
            strategy_id="stealth_trail_bbp_v1",
            exchange="bybit",
            symbol="SOL/USDT:USDT",
            net=-3.0,
            mfe_after_cost=35.0,
            diagnosis="GAVE_BACK_FEE_WALL_MOVE",
        )
        for i in range(12)
    )
    report = build_fee_wall_forensics_report(
        (
            router_report(
                routes=gave_back,
                exchange="bybit",
                symbol="SOL/USDT:USDT",
                strategy="stealth_trail_bbp_v1",
            ),
        ),
        min_samples=10,
    )

    assert report["summary"]["exit_salvage_candidates"] == 1
    assert report["exit_salvage_candidates"][0]["recommended_action"] == (
        "REBUILD_EXIT_TRAIL_OR_EARLIER_TARGET_CAPTURE"
    )
    assert report["summary"]["exit_diagnosis_counts"]["GAVE_BACK_FEE_WALL_MOVE"] == 12


def test_fee_wall_forensics_publishers_are_atomic_and_feed_is_compact(tmp_path):
    report = build_fee_wall_forensics_report(
        (router_report(routes=tuple(route(i, net=20.0) for i in range(10))),),
        min_samples=10,
    )
    out = tmp_path / "fee_wall_forensics_latest.json"
    feed = tmp_path / "fee_wall_forensics_feed.jsonl"

    publish_json(report, out)
    append_feed(report, feed)

    saved = json.loads(out.read_text())
    feed_row = json.loads(feed.read_text().splitlines()[-1])
    assert saved["truth_layer"] == "fee_wall_forensics_v1"
    assert saved["summary"]["strict_fee_wall_candidates"] == 1
    assert saved["strict_fee_wall_candidates"][0]["recommended_action"] == (
        "PRE_REGISTER_UNTOUCHED_JUDGMENT_WINDOW"
    )
    assert feed_row["truth_layer"] == "fee_wall_forensics_v1"
    assert feed_row["can_trade"] is False
    assert stat.S_IMODE(out.stat().st_mode) == 0o644
    assert stat.S_IMODE(feed.stat().st_mode) == 0o644


def test_fee_wall_forensics_progress_is_visibility_only():
    target = ResearchTarget("delta_india", "ETH/USD:USD", "15m")
    progress = build_fee_wall_forensics_progress(
        status="running",
        phase="labeling_opportunities",
        started_at="2026-07-19T00:00:00+00:00",
        targets=(target,),
        strategy_ids=("luxara_live_plan_qtm_v1", "sats_5m_scalper_v1"),
        lookback_days=30,
        completed_work_units=1,
        total_work_units=2,
        current_target=target,
        current_strategy="sats_5m_scalper_v1",
        rows=2880,
        routes=4,
        output_path="research/live_research/fee_wall_forensics_latest.json",
        routes_output_path="research/live_research/fee_wall_forensics_routes_latest.jsonl",
    )

    assert progress["truth_layer"] == "fee_wall_forensics_progress_v1"
    assert progress["progress_pct"] == 50.0
    assert progress["current_target"]["exchange"] == "delta_india"
    assert progress["current_routes"] == 4
    assert progress["can_trade"] is False
    assert progress["policy"]["live_governance_unchanged"] is True
