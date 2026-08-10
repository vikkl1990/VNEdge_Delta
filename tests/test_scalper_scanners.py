"""Scalper scanners — rank lanes without granting trading permission."""

from vnedge.research.scalper_replay_diagnostics import (
    ScalperReplayDiagnostics,
    ScalperReplayRow,
    TickSampleStats,
)
from vnedge.research.scalper_scanners import (
    ScalperScannerConfig,
    decide_execution_route,
    scan_diagnostics,
    select_recorder_targets,
    scanner_policy,
)


def stats(**kw):
    base = dict(
        events=30_000,
        book_events=15_000,
        trade_events=15_000,
        span_seconds=8 * 3_600.0,
        spread_bps_p50=0.2,
        spread_bps_p95=0.8,
        abs_imbalance_p90=0.7,
        taker_buy_ratio=0.55,
    )
    base.update(kw)
    return TickSampleStats(**base)


def row(**kw):
    base = dict(
        min_imbalance=0.35,
        max_spread_bps=2.0,
        quotes=200,
        filled=40,
        missed=120,
        open_at_end=0,
        fill_rate_pct=20.0,
        net_usd=1.2,
        avg_net_bps=3.0,
        avg_adverse_bps=-2.0,
        verdict="CANDIDATE",
        profit_factor=2.5,
    )
    base.update(kw)
    return ScalperReplayRow(**base)


def report(*, blocker="CANDIDATE_FOUND", s=None, rows=None):
    replay_rows = [row()] if rows is None else rows
    return ScalperReplayDiagnostics(
        exchange="binanceusdm",
        symbol="BTC/USDT:USDT",
        day="20260704",
        stats=s or stats(),
        rows=tuple(replay_rows),
        primary_blocker=blocker,
        action="test",
    )


def cfg(**kw):
    params = dict(
        min_sample_seconds=3_600.0,
        min_book_events=100,
        min_trade_events=100,
        min_fills=10,
        min_fill_rate_pct=5.0,
        maker_min_profit_factor=1.15,
        taker_min_profit_factor=1.8,
        min_avg_net_bps=1.0,
        max_avg_adverse_bps=8.0,
        min_candidate_score=85.0,
    )
    params.update(kw)
    return ScalperScannerConfig(**params)


def test_scanner_marks_strong_replay_as_candidate_but_non_trading():
    scan = scan_diagnostics(report(), cfg())

    assert scan.state == "REPLAY_CANDIDATE"
    assert scan.recorder_priority == 100.0
    assert scan.edge_score >= 85.0
    assert scan.route_decision.route == "MAKER_ONLY"
    assert scan.can_trade is False
    assert scan.can_promote is False
    assert scan.requires_untouched_judgment is True


def test_under_sampled_tight_lane_is_record_more_not_candidate():
    short = stats(span_seconds=900.0, book_events=50, trade_events=50)
    weak_row = row(filled=2, fill_rate_pct=2.0, net_usd=-0.1, avg_net_bps=-2.0)

    scan = scan_diagnostics(
        report(blocker="UNDER_SAMPLED_TICKS", s=short, rows=[weak_row]),
        cfg(),
    )

    assert scan.state == "RECORD_MORE"
    assert scan.recorder_priority > scan.edge_score
    assert "record" in scan.next_action


def test_sufficient_negative_replay_hits_cost_wall_and_gets_low_priority():
    negative = row(
        filled=30, fill_rate_pct=15.0, net_usd=-1.5,
        avg_net_bps=-4.0, profit_factor=0.55,
    )

    scan = scan_diagnostics(
        report(blocker="NEGATIVE_EDGE_AFTER_COST", rows=[negative]),
        cfg(),
    )

    assert scan.state == "REJECTED_COST_WALL"
    assert scan.route_decision.route == "BLOCKED"
    assert scan.recorder_priority <= 20.0
    assert "do not trade" in scan.next_action


def test_missing_tick_data_requests_recorder_start():
    scan = scan_diagnostics(
        report(blocker="NO_TICK_DATA", s=stats(events=0, book_events=0, trade_events=0), rows=[]),
        cfg(),
    )

    assert scan.state == "MISSING_TICK_DATA"
    assert scan.recorder_priority == 55.0
    assert scan.best_row is None
    assert "recorder" in scan.next_action


def test_recorder_target_selection_skips_cost_wall_for_same_lane():
    candidate = scan_diagnostics(report(), cfg())
    cost_wall = scan_diagnostics(
        report(blocker="NEGATIVE_EDGE_AFTER_COST",
               rows=[row(net_usd=-1.0, avg_net_bps=-3.0, profit_factor=0.4)]),
        cfg(),
    )
    missing = scan_diagnostics(
        report(blocker="NO_TICK_DATA", s=stats(events=0, book_events=0, trade_events=0), rows=[]),
        cfg(),
    )

    selected = select_recorder_targets([cost_wall, missing, candidate], limit=1)

    assert selected == (candidate,)


def test_route_decision_allows_taker_only_when_pf_and_extra_cost_clear():
    strong = row(avg_net_bps=5.0, profit_factor=2.2)
    route = decide_execution_route(strong, cfg())

    assert route.route == "TAKER_ALLOWED"
    assert route.taker_breakeven_bps == 4.0


def test_route_decision_blocks_below_breakeven_even_with_fills():
    weak = row(filled=25, avg_net_bps=0.2, profit_factor=2.5)
    route = decide_execution_route(weak, cfg())

    assert route.route == "BLOCKED"
    assert "breakeven" in route.reason


def test_scanner_policy_exposes_family_lifecycle_without_trade_permission():
    policy = scanner_policy()

    assert policy["can_trade"] is False
    assert policy["can_promote"] is False
    assert "forced_flow_continuation" in policy["active_research_families"]
    assert "book_imbalance_continuation" not in policy["active_research_families"]
    assert policy["tombstoned_families"][0]["family_id"] == (
        "book_imbalance_continuation"
    )
    assert "all 120 configs" in policy["tombstoned_families"][0]["evidence"]
