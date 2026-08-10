"""Scalper lane filters — explain why a lane is worth mining or blocked."""

from datetime import UTC, datetime

from vnedge.research.scalper_lane_filters import (
    FILTER_ID,
    LaneFilterConfig,
    LaneFilterEvidence,
    evaluate_lane_filters,
    lane_filter_policy,
    summarize_filter_decisions,
)
from vnedge.scalping.microstructure import TopOfBook, TradeTick


BASE = 1_783_003_600_000


def _events(
    *,
    rows: int = 30,
    spread_bps: float = 1.0,
    depth_usd: float = 1_500.0,
    notional_per_trade: float = 100.0,
    trend_step: float = 0.02,
) -> list[tuple[int, str, object]]:
    events: list[tuple[int, str, object]] = []
    for i in range(rows):
        ts = BASE + i * 100
        mid = 100.0 + i * trend_step
        half_spread = mid * spread_bps / 20_000.0
        qty = depth_usd / (2.0 * mid)
        events.append((
            ts,
            "book",
            TopOfBook(
                symbol="BTC/USDT:USDT",
                bid=mid - half_spread,
                bid_size=qty,
                ask=mid + half_spread,
                ask_size=qty,
                event_time=datetime.fromtimestamp(ts / 1000, tz=UTC),
            ),
        ))
        events.append((
            ts + 1,
            "trade",
            TradeTick(
                symbol="BTC/USDT:USDT",
                price=mid,
                quantity=notional_per_trade / mid,
                taker_side="buy",
                event_time=datetime.fromtimestamp((ts + 1) / 1000, tz=UTC),
            ),
        ))
    return events


def _evidence(**kwargs) -> LaneFilterEvidence:
    return LaneFilterEvidence.from_events(
        _events(**kwargs),
        exchange="binanceusdm",
        symbol="BTC/USDT:USDT",
        day="20260706",
    )


def test_healthy_lane_passes_with_replay_and_shadow_warnings():
    decision = evaluate_lane_filters(_evidence())

    assert decision.filter_id == FILTER_ID
    assert decision.passed is True
    assert decision.state == "FILTER_WARN"
    assert decision.primary_blocker is None
    assert [check.name for check in decision.checks] == list(
        LaneFilterConfig().enabled_filters
    )
    assert {check.name for check in decision.checks if check.severity == "warn"} == {
        "replay_state",
        "shadow_performance",
    }
    assert decision.can_trade is False
    assert decision.can_promote is False


def test_volume_filter_blocks_low_notional_after_coverage_passes():
    decision = evaluate_lane_filters(_evidence(notional_per_trade=1.0))

    assert decision.passed is False
    assert decision.primary_blocker == "volume"
    assert decision.checks[1].name == "volume"
    assert "notional" in decision.checks[1].reason


def test_spread_depth_precision_and_volatility_filters_block_bad_lanes():
    spread = evaluate_lane_filters(_evidence(spread_bps=20.0))
    depth = evaluate_lane_filters(_evidence(depth_usd=20.0))
    precision = evaluate_lane_filters(_evidence(trend_step=1.0))
    quiet = evaluate_lane_filters(_evidence(trend_step=0.0001))

    assert spread.primary_blocker == "spread"
    assert depth.primary_blocker == "depth"
    assert precision.primary_blocker == "precision"
    assert quiet.primary_blocker == "volatility"


def test_replay_state_filter_blocks_tombstoned_replay_state():
    evidence = _evidence()
    evidence = LaneFilterEvidence(
        **{**evidence.to_dict(), "replay_state": "REJECTED_COST_WALL"}
    )

    decision = evaluate_lane_filters(evidence)

    assert decision.passed is False
    assert decision.primary_blocker == "replay_state"
    assert "REJECTED_COST_WALL" in decision.checks[6].reason


def test_shadow_performance_filter_blocks_negative_mature_shadow_sample():
    evidence = _evidence()
    evidence = LaneFilterEvidence(
        **{
            **evidence.to_dict(),
            "replay_state": "REPLAY_CANDIDATE",
            "shadow_virtual_trades": 8,
            "shadow_profit_factor": 0.6,
            "shadow_net_usd": -2.4,
        }
    )

    decision = evaluate_lane_filters(evidence)

    assert decision.passed is False
    assert decision.primary_blocker == "shadow_performance"
    assert decision.checks[-1].metrics["shadow_virtual_trades"] == 8


def test_filter_summary_and_policy_are_research_only():
    good = evaluate_lane_filters(_evidence())
    bad = evaluate_lane_filters(_evidence(spread_bps=20.0))
    summary = summarize_filter_decisions((good, bad))
    policy = lane_filter_policy()

    assert summary["lanes"] == 2
    assert summary["blocked"] == 1
    assert summary["primary_blockers"] == {"spread": 1}
    assert summary["can_trade"] is False
    assert policy["can_trade"] is False
    assert policy["can_promote"] is False


def test_fast_l2_scout_applies_filters_before_mining(monkeypatch):
    from vnedge.research import fast_l2_scout as scout
    from vnedge.research.universe import ResearchTarget

    calls = []

    def fake_loader(*args, **kwargs):
        return _events(spread_bps=20.0), {
            "book_rows": 30,
            "trade_rows": 30,
            "span_seconds": 3.0,
            "missing_stream": False,
        }

    def fake_mine(*args, **kwargs):
        calls.append("mine")
        return ()

    monkeypatch.setattr(scout, "load_recent_tick_events", fake_loader)
    monkeypatch.setattr(scout, "mine_events", fake_mine)

    payload = scout.run_fast_l2_scout(
        "unused",
        targets=(ResearchTarget("binanceusdm", "BTC/USDT:USDT"),),
        days=("20260706",),
    )

    assert calls == []
    assert payload["summary"]["filtered_lanes"] == 1
    assert payload["lanes"][0]["state"] == "FILTERED_LANE"
    assert payload["lanes"][0]["filter_decision"]["primary_blocker"] == "spread"
