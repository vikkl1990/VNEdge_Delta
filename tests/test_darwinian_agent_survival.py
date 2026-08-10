from datetime import UTC, datetime

from vnedge.research.darwinian_agent_survival import (
    COHORT_SCALPER,
    COHORT_SWING_4H,
    REPORT_ID,
    STATE_EXTINCTION,
    STATE_LEADING,
    STATE_MUTED,
    build_darwinian_agent_survival,
    publish_darwinian_agent_survival,
)


def _survival(rows):
    return {
        "report_id": "lane_survival_v1",
        "generated_at": "2026-07-31T00:00:00+00:00",
        "rows": rows,
    }


def _governor(rows):
    return {
        "report_id": "paper_lane_governor_v1",
        "generated_at": "2026-07-31T00:00:00+00:00",
        "rows": rows,
    }


def _scanner(rows):
    return {
        "scanner_id": "realtime_scanner_v1",
        "generated_at": "2026-07-31T00:00:00+00:00",
        "rows": rows,
    }


def _alpha(scorecards):
    return {
        "report_id": "alpha_arena_lite_v1",
        "generated_at": "2026-07-31T00:00:00+00:00",
        "scorecards": scorecards,
    }


def _survival_row(
    strategy_id: str,
    *,
    lane_id: str,
    state: str,
    decision: str,
    closed: int,
    net: float,
    bps: float,
    pf: float,
    timeframe: str = "5m",
):
    return {
        "lane_id": lane_id,
        "exchange": "delta_india",
        "symbol": "ETH/USD:USD",
        "timeframe": timeframe,
        "strategy_id": strategy_id,
        "survival_state": state,
        "decision": decision,
        "closed_trades": closed,
        "closed_net_pnl_usd": net,
        "fees_usd": 2.0,
        "avg_closed_trade_net_bps": bps,
        "profit_factor": pf,
        "live_signals": closed + 3,
        "paper_order_intents": closed,
    }


def test_darwinian_survival_upweights_winner_and_downweights_loser():
    payload = build_darwinian_agent_survival(
        survival=_survival(
            [
                _survival_row(
                    "stealth_trail_bbp_v1",
                    lane_id="winner",
                    state="PAPER_SURVIVOR_CANDIDATE",
                    decision="KEEP_PAPER",
                    closed=24,
                    net=48.0,
                    bps=32.0,
                    pf=1.9,
                ),
                _survival_row(
                    "quant_signal_pack_v1",
                    lane_id="loser",
                    state="DEMOTE_TO_SHADOW",
                    decision="DEMOTE_TO_SHADOW",
                    closed=22,
                    net=-44.0,
                    bps=-36.0,
                    pf=0.42,
                ),
            ]
        ),
        governor=_governor(
            [
                {
                    "lane_id": "winner",
                    "strategy_id": "stealth_trail_bbp_v1",
                    "exchange": "delta_india",
                    "symbol": "ETH/USD:USD",
                    "timeframe": "5m",
                    "governor_bucket": "SURVIVOR_TOURNAMENT",
                    "action": "KEEP_PAPER_SURVIVOR",
                    "closed_trades": 24,
                    "avg_closed_trade_net_bps": 32.0,
                    "profit_factor": 1.9,
                    "closed_net_pnl_usd": 48.0,
                },
                {
                    "lane_id": "loser",
                    "strategy_id": "quant_signal_pack_v1",
                    "exchange": "delta_india",
                    "symbol": "ETH/USD:USD",
                    "timeframe": "5m",
                    "governor_bucket": "DEMOTION_QUEUE",
                    "action": "DEMOTE_TO_SHADOW_RECOMMENDED",
                    "closed_trades": 22,
                    "avg_closed_trade_net_bps": -36.0,
                    "profit_factor": 0.42,
                    "closed_net_pnl_usd": -44.0,
                },
            ]
        ),
        scanner=_scanner(
            [
                {
                    "lane_id": "winner",
                    "strategy_id": "stealth_trail_bbp_v1",
                    "exchange": "delta_india",
                    "symbol": "ETH/USD:USD",
                    "timeframe": "5m",
                    "state": "FIRING",
                    "funnel": {"live_signals": 12, "paper_order_intents": 8},
                    "uplift": {"action": "KEEP_ROUTE"},
                }
            ]
        ),
        previous={
            "agents": [
                {"agent_id": "stealth_trail_bbp_v1", "darwinian_weight": 1.0},
                {"agent_id": "quant_signal_pack_v1", "darwinian_weight": 1.0},
            ]
        },
        now=datetime(2026, 7, 31, tzinfo=UTC),
    )

    rows = {row["agent_id"]: row for row in payload["agents"]}
    assert payload["report_id"] == REPORT_ID
    assert rows["stealth_trail_bbp_v1"]["survival_state"] == STATE_LEADING
    assert rows["stealth_trail_bbp_v1"]["darwinian_weight"] > 1.0
    assert rows["quant_signal_pack_v1"]["survival_state"] == STATE_EXTINCTION
    assert rows["quant_signal_pack_v1"]["darwinian_weight"] < 1.0
    assert payload["summary"]["upweighted_agents"] == 1
    assert payload["summary"]["downweighted_agents"] == 1
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False


def test_janus_cohorts_weight_scanner_regime_without_trading():
    payload = build_darwinian_agent_survival(
        survival=_survival(
            [
                _survival_row(
                    "fast_scalper_v1",
                    lane_id="fast",
                    state="PAPER_SURVIVOR_CANDIDATE",
                    decision="KEEP_PAPER",
                    closed=20,
                    net=35.0,
                    bps=30.0,
                    pf=1.7,
                    timeframe="5m",
                ),
                _survival_row(
                    "slow_swing_v1",
                    lane_id="slow",
                    state="PAPER_ACTIVE_NEGATIVE",
                    decision="OBSERVE_MORE",
                    closed=20,
                    net=-18.0,
                    bps=-12.0,
                    pf=0.8,
                    timeframe="4h",
                ),
            ]
        ),
        governor=_governor([]),
        scanner=_scanner([]),
        alpha_arena=_alpha(
            [
                {
                    "strategy_id": "fast_scalper_v1",
                    "exchange": "delta_india",
                    "symbol": "ETH/USD:USD",
                    "timeframes": ["5m"],
                    "arena_verdict": "PRE_REGISTER_UNTOUCHED_JUDGMENT",
                    "metrics": {
                        "top_avg_net_bps": 31.0,
                        "best_profit_factor": 1.8,
                        "max_samples": 20,
                    },
                }
            ]
        ),
        now=datetime(2026, 7, 31, tzinfo=UTC),
    )

    cohorts = {row["cohort"]: row for row in payload["cohorts"]}
    assert COHORT_SCALPER in cohorts
    assert COHORT_SWING_4H in cohorts
    assert abs(sum(row["janus_weight"] for row in cohorts.values()) - 1.0) < 0.000001
    assert cohorts[COHORT_SCALPER]["janus_weight"] > cohorts[COHORT_SWING_4H]["janus_weight"]
    assert payload["summary"]["janus_regime"] in {
        "SCALPER_5M_DOMINANT",
        "SHORT_WINDOW_COHORTS_WORKING",
        "MIXED_COHORT_REGIME",
    }
    assert all(row["can_trade"] is False for row in payload["cohorts"])


def test_darwinian_survival_muted_agent_and_publish_roundtrip(tmp_path):
    payload = build_darwinian_agent_survival(
        survival=_survival(
            [
                _survival_row(
                    "bad_agent_v1",
                    lane_id="bad",
                    state="PAPER_ACTIVE_NEGATIVE",
                    decision="OBSERVE_MORE",
                    closed=21,
                    net=-30.0,
                    bps=-20.0,
                    pf=0.7,
                    timeframe="15m",
                )
            ]
        ),
        governor=_governor([]),
        scanner=_scanner([]),
        previous_path=None,
        now=datetime(2026, 7, 31, tzinfo=UTC),
    )
    assert payload["agents"][0]["survival_state"] == STATE_MUTED
    assert payload["agents"][0]["influence_state"] == "DOWNWEIGHT"
    assert payload["agents"][0]["next_action"] == "MUTE_IN_CIO_BLEND_UNTIL_NET_EDGE_REPAIRED"

    out = tmp_path / "darwin.json"
    feed = tmp_path / "darwin.jsonl"
    publish_darwinian_agent_survival(payload, out, feed)

    assert out.exists()
    assert feed.exists()
