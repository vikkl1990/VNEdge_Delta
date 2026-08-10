from datetime import UTC, datetime

from vnedge.research.lane_survival import (
    DECISION_DEMOTE_TO_SHADOW,
    DECISION_KEEP_PAPER,
    DECISION_OBSERVE_MORE,
    DECISION_REPAIR_LEDGER,
    DECISION_REPAIR_ROUTE_OR_CADENCE,
    STATE_DEMOTE_TO_SHADOW,
    STATE_LEDGER_REPAIR_REQUIRED,
    STATE_PAPER_OBSERVE_MORE,
    STATE_PAPER_SURVIVOR_CANDIDATE,
    STATE_STALE_NO_JUDGMENT,
    LaneSurvivalConfig,
    build_lane_survival,
)


def _activation(lane_id: str = "alpha") -> dict:
    return {
        "report_id": "paper_lane_activation_v1",
        "generated_at": "2026-07-28T00:00:00+00:00",
        "rows": [
            {
                "trial_id": lane_id,
                "activation_state": "PAPER_RUNNING",
                "exchange": "delta_india",
                "symbol": "ETH/USD:USD",
                "timeframe": "5m",
                "strategy_id": "stealth_trail_bbp_v1",
                "runtime": {"desired_lane_ids": [lane_id]},
            }
        ],
    }


def _cadence(lane_id: str = "alpha", state: str = "EVALUATING_SIGNAL_SEEN") -> dict:
    return {
        "report_id": "paper_lane_cadence_v1",
        "generated_at": "2026-07-28T00:00:00+00:00",
        "rows": [
            {
                "trial_id": lane_id,
                "expected_lane_id": lane_id,
                "cadence_state": state,
                "age_hours": 0.2,
            }
        ],
    }


def _route(lane_id: str = "alpha", state: str = "JOURNAL_ACTIVE") -> dict:
    return {
        "report_id": "paper_route_doctor_v1",
        "generated_at": "2026-07-28T00:00:00+00:00",
        "rows": [
            {
                "trial_id": lane_id,
                "expected_lane_id": lane_id,
                "doctor_state": state,
                "age_hours": 0.2,
            }
        ],
    }


def _perf(
    lane_id: str = "alpha",
    *,
    closed: int,
    net: float,
    pf: float,
    avg_bps: float,
    state: str = "PAPER_ACTIVE_PROFITABLE",
    drift: list[str] | None = None,
    open_fills: int = 0,
    unpaired: int = 0,
    ledger_ok: bool = True,
) -> dict:
    return {
        "report_id": "paper_lane_performance_v1",
        "generated_at": "2026-07-28T00:00:00+00:00",
        "rows": [
            {
                "lane_id": lane_id,
                "exchange": "delta_india",
                "symbol": "ETH/USD:USD",
                "timeframe": "5m",
                "strategy_id": "stealth_trail_bbp_v1",
                "state": state,
                "latest_ts": "2026-07-28T00:00:00+00:00",
                "closed_trades": closed,
                "wins": max(0, closed - 2),
                "losses": min(2, closed),
                "win_rate": (max(0, closed - 2) / closed) if closed else None,
                "profit_factor": pf,
                "fees_usd": 2.0,
                "net_pnl_usd": net,
                "closed_net_pnl_usd": net,
                "avg_closed_trade_net_bps": avg_bps,
                "live_signals": 10,
                "paper_order_intents": closed,
                "fills": closed * 2,
                "open_fill_count": open_fills,
                "open_position_entry_fees_usd": 0.75 if open_fills else 0.0,
                "unpaired_closing_fills": unpaired,
                "journal_drift_flags": drift or [],
                "ledger_ok": ledger_ok,
            }
        ],
    }


def test_lane_survival_marks_paper_survivor_candidate():
    payload = build_lane_survival(
        activation=_activation(),
        cadence=_cadence(),
        route_doctor=_route(),
        performance=_perf(closed=24, net=42.0, pf=1.8, avg_bps=31.0),
        config=LaneSurvivalConfig(min_closed_trades=20),
        now=datetime(2026, 7, 28, tzinfo=UTC),
    )

    row = payload["rows"][0]
    assert row["survival_state"] == STATE_PAPER_SURVIVOR_CANDIDATE
    assert row["decision"] == DECISION_KEEP_PAPER
    assert row["exit_quality"]["label"] == "CAPTURE_OK"
    assert payload["summary"]["survivor_candidates"] == 1
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False


def test_lane_survival_keeps_positive_under_sampled_lane_observing():
    payload = build_lane_survival(
        activation=_activation(),
        cadence=_cadence(),
        route_doctor=_route(),
        performance=_perf(closed=4, net=10.0, pf=2.0, avg_bps=79.0),
        now=datetime(2026, 7, 28, tzinfo=UTC),
    )

    row = payload["rows"][0]
    assert row["survival_state"] == STATE_PAPER_OBSERVE_MORE
    assert row["decision"] == DECISION_OBSERVE_MORE
    assert "needs 16 more closed trade(s)" in row["blockers"]
    assert row["exit_quality"]["label"] == "CAPTURE_OK"


def test_lane_survival_demotes_negative_fee_bleed_lane():
    payload = build_lane_survival(
        activation=_activation(),
        cadence=_cadence(),
        route_doctor=_route(),
        performance=_perf(
            closed=6,
            net=-18.0,
            pf=0.42,
            avg_bps=-35.0,
            state="PAPER_ACTIVE_NEGATIVE",
        ),
        now=datetime(2026, 7, 28, tzinfo=UTC),
    )

    row = payload["rows"][0]
    assert row["survival_state"] == STATE_DEMOTE_TO_SHADOW
    assert row["decision"] == DECISION_DEMOTE_TO_SHADOW
    assert row["exit_quality"]["label"] == "FEE_WALL_DRAG"
    assert payload["summary"]["demote_to_shadow"] == 1


def test_lane_survival_refuses_judgment_on_stale_route():
    payload = build_lane_survival(
        activation=_activation(),
        cadence=_cadence(state="EVAL_STALE"),
        route_doctor=_route(state="JOURNAL_STALE"),
        performance=_perf(
            closed=5,
            net=6.0,
            pf=1.6,
            avg_bps=30.0,
            state="NO_RECENT_PROOF",
        ),
        now=datetime(2026, 7, 28, tzinfo=UTC),
    )

    row = payload["rows"][0]
    assert row["survival_state"] == STATE_STALE_NO_JUDGMENT
    assert row["decision"] == DECISION_REPAIR_ROUTE_OR_CADENCE
    assert any("stale/no current judgment" in item for item in row["blockers"])


def test_lane_survival_blocks_ledger_drift_before_scoring():
    # Real corruption = an unpaired closing fill (a close with no matching entry).
    payload = build_lane_survival(
        activation=_activation(),
        cadence=_cadence(),
        route_doctor=_route(),
        performance=_perf(
            closed=20,
            net=60.0,
            pf=2.0,
            avg_bps=50.0,
            unpaired=1,
            ledger_ok=False,
            drift=["1 unpaired closing fill(s)"],
        ),
        now=datetime(2026, 7, 28, tzinfo=UTC),
    )

    row = payload["rows"][0]
    assert row["survival_state"] == STATE_LEDGER_REPAIR_REQUIRED
    assert row["decision"] == DECISION_REPAIR_LEDGER
    assert row["survival_score"] < 90


def test_open_position_is_not_ledger_repair():
    """A lane holding a normal open position (entry fill awaiting its close, with
    the benign 'awaiting close' / 'entry-fee drag' drift flags) must NOT be
    mislabelled LEDGER_REPAIR_REQUIRED. Real corruption is an unpaired *closing*
    fill; an open entry is not. Regression for the false-positive that flagged 9
    live, mostly-profitable lanes as broken and docked their agent score."""
    payload = build_lane_survival(
        activation=_activation(),
        cadence=_cadence(),
        route_doctor=_route(),
        performance=_perf(
            closed=4,
            net=9.5,
            pf=1.9,
            avg_bps=74.0,
            open_fills=1,
            unpaired=0,
            ledger_ok=True,
            drift=["1 open fill(s) awaiting close", "$0.75 open entry-fee drag"],
        ),
        now=datetime(2026, 7, 28, tzinfo=UTC),
    )
    row = payload["rows"][0]
    assert row["survival_state"] != STATE_LEDGER_REPAIR_REQUIRED
    assert row["decision"] != DECISION_REPAIR_LEDGER


def test_lane_survival_prefers_runtime_lane_id_over_manifest_trial_id():
    activation = _activation("manifest_alpha")
    activation["rows"][0]["runtime"]["desired_lane_ids"] = ["runtime_alpha"]
    payload = build_lane_survival(
        activation=activation,
        cadence=_cadence("runtime_alpha"),
        route_doctor=_route("runtime_alpha"),
        performance=_perf(
            "runtime_alpha",
            closed=4,
            net=10.0,
            pf=2.0,
            avg_bps=79.0,
        ),
        now=datetime(2026, 7, 28, tzinfo=UTC),
    )

    assert len(payload["rows"]) == 1
    assert payload["rows"][0]["lane_id"] == "runtime_alpha"
