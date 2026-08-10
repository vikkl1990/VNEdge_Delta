from datetime import UTC, datetime

from vnedge.research.paper_lane_governor import (
    ACTION_DEMOTE_TO_SHADOW_RECOMMENDED,
    ACTION_EXTEND_PAPER_SAMPLE,
    ACTION_HOLD_PAPER_PROBATION,
    ACTION_KEEP_PAPER_SURVIVOR,
    ACTION_REPAIR_LEDGER,
    ACTION_REPAIR_ROUTE_OR_CADENCE,
    ACTION_WAIT_FOR_TRADE_EVIDENCE,
    BUCKET_DEMOTION_QUEUE,
    BUCKET_NO_EVIDENCE,
    BUCKET_PAPER_ROSTER,
    BUCKET_PROBATION_QUEUE,
    BUCKET_REPAIR_QUEUE,
    BUCKET_SURVIVOR_TOURNAMENT,
    build_paper_lane_governor,
)


def _survival(rows):
    return {
        "report_id": "lane_survival_v1",
        "generated_at": "2026-07-29T00:00:00+00:00",
        "rows": rows,
        "summary": {"total_lanes": len(rows)},
        "can_trade": False,
        "can_promote": False,
    }


def _row(
    lane_id: str,
    *,
    state: str,
    decision: str,
    closed: int,
    net: float,
    pf: float,
    bps: float | None,
    score: float = 60.0,
):
    return {
        "lane_id": lane_id,
        "exchange": "delta_india",
        "symbol": "ETH/USD:USD",
        "timeframe": "5m",
        "strategy_id": "stealth_trail_bbp_v1",
        "survival_state": state,
        "survival_score": score,
        "decision": decision,
        "closed_trades": closed,
        "profit_factor": pf,
        "fees_usd": 2.5,
        "closed_net_pnl_usd": net,
        "avg_closed_trade_net_bps": bps,
        "live_signals": 10,
        "paper_order_intents": max(0, closed),
        "exit_quality": {"label": "CAPTURE_OK" if net >= 0 else "FEE_WALL_DRAG"},
        "blockers": [],
        "can_trade": False,
        "can_promote": False,
    }


def test_governor_places_survivor_candidate_in_tournament():
    payload = build_paper_lane_governor(
        survival=_survival(
            [
                _row(
                    "winner",
                    state="PAPER_SURVIVOR_CANDIDATE",
                    decision="KEEP_PAPER",
                    closed=24,
                    net=40.0,
                    pf=1.9,
                    bps=32.0,
                    score=92.0,
                )
            ]
        ),
        now=datetime(2026, 7, 29, tzinfo=UTC),
    )

    row = payload["rows"][0]
    assert row["action"] == ACTION_KEEP_PAPER_SURVIVOR
    assert row["governor_bucket"] == BUCKET_SURVIVOR_TOURNAMENT
    assert payload["summary"]["survivor_tournament"] == 1
    assert payload["proposed_roster"]["survivor_tournament"][0]["lane_id"] == "winner"
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False


def test_governor_extends_positive_under_sampled_lane():
    payload = build_paper_lane_governor(
        survival=_survival(
            [
                _row(
                    "under_sampled",
                    state="PAPER_OBSERVE_MORE",
                    decision="OBSERVE_MORE",
                    closed=4,
                    net=10.0,
                    pf=2.0,
                    bps=79.0,
                )
            ]
        ),
        now=datetime(2026, 7, 29, tzinfo=UTC),
    )

    row = payload["rows"][0]
    assert row["action"] == ACTION_EXTEND_PAPER_SAMPLE
    assert row["governor_bucket"] == BUCKET_PAPER_ROSTER
    assert row["closed_trades_needed"] == 16
    assert row["autopsy"]["sample_gap"] == 16


def test_governor_recommends_shadow_demote_for_negative_lane():
    payload = build_paper_lane_governor(
        survival=_survival(
            [
                _row(
                    "bleeder",
                    state="DEMOTE_TO_SHADOW",
                    decision="DEMOTE_TO_SHADOW",
                    closed=6,
                    net=-18.0,
                    pf=0.42,
                    bps=-35.0,
                )
            ]
        ),
        now=datetime(2026, 7, 29, tzinfo=UTC),
    )

    row = payload["rows"][0]
    assert row["action"] == ACTION_DEMOTE_TO_SHADOW_RECOMMENDED
    assert row["governor_bucket"] == BUCKET_DEMOTION_QUEUE
    assert row["autopsy"]["primary_failure"] == "negative_after_fee_wall"
    assert row["autopsy"]["fee_wall_gap_bps"] == 60.0
    assert payload["summary"]["demotion_queue"] == 1


def test_governor_holds_negative_under_sampled_lane_out_of_active_roster():
    payload = build_paper_lane_governor(
        survival=_survival(
            [
                _row(
                    "probation",
                    state="PAPER_PROBATION",
                    decision="OBSERVE_MORE",
                    closed=2,
                    net=-4.0,
                    pf=0.9,
                    bps=-8.0,
                ),
                _row(
                    "healthy_sample",
                    state="PAPER_OBSERVE_MORE",
                    decision="OBSERVE_MORE",
                    closed=4,
                    net=8.0,
                    pf=1.6,
                    bps=28.0,
                ),
            ]
        ),
        now=datetime(2026, 7, 29, tzinfo=UTC),
    )

    rows = {r["lane_id"]: r for r in payload["rows"]}
    assert rows["probation"]["action"] == ACTION_HOLD_PAPER_PROBATION
    assert rows["probation"]["governor_bucket"] == BUCKET_PROBATION_QUEUE
    assert rows["probation"]["autopsy"]["primary_failure"] == "negative_paper_probation"
    assert rows["healthy_sample"]["governor_bucket"] == BUCKET_PAPER_ROSTER
    assert payload["summary"]["paper_roster"] == 1
    assert payload["summary"]["probation_queue"] == 1
    assert [r["lane_id"] for r in payload["proposed_roster"]["paper_lanes"]] == [
        "healthy_sample"
    ]
    assert [r["lane_id"] for r in payload["proposed_roster"]["probation_shadow_watch"]] == [
        "probation"
    ]


def test_governor_routes_stale_and_ledger_lanes_to_repair_queue():
    payload = build_paper_lane_governor(
        survival=_survival(
            [
                _row(
                    "stale",
                    state="STALE_NO_JUDGMENT",
                    decision="REPAIR_ROUTE_OR_CADENCE",
                    closed=3,
                    net=5.0,
                    pf=1.4,
                    bps=20.0,
                ),
                _row(
                    "ledger",
                    state="LEDGER_REPAIR_REQUIRED",
                    decision="REPAIR_LEDGER",
                    closed=20,
                    net=60.0,
                    pf=2.0,
                    bps=50.0,
                ),
            ]
        ),
        now=datetime(2026, 7, 29, tzinfo=UTC),
    )

    actions = {r["lane_id"]: r["action"] for r in payload["rows"]}
    assert actions["stale"] == ACTION_REPAIR_ROUTE_OR_CADENCE
    assert actions["ledger"] == ACTION_REPAIR_LEDGER
    assert all(r["governor_bucket"] == BUCKET_REPAIR_QUEUE for r in payload["rows"])
    assert payload["summary"]["repair_queue"] == 2


def test_governor_marks_no_trade_evidence_without_promotion():
    payload = build_paper_lane_governor(
        survival=_survival(
            [
                _row(
                    "quiet",
                    state="NO_TRADE_EVIDENCE",
                    decision="OBSERVE_MORE",
                    closed=0,
                    net=0.0,
                    pf=0.0,
                    bps=None,
                )
            ]
        ),
        now=datetime(2026, 7, 29, tzinfo=UTC),
    )

    row = payload["rows"][0]
    assert row["action"] == ACTION_WAIT_FOR_TRADE_EVIDENCE
    assert row["governor_bucket"] == BUCKET_NO_EVIDENCE
    assert row["tournament"]["tier"] == "WAIT_FOR_FIRST_CLOSE"
