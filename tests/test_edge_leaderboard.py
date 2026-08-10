"""Edge leaderboard and promotion queue."""

from vnedge.research.edge_leaderboard import build_edge_leaderboard


def record(
    *,
    strategy="quant_signal_pack_v1",
    verdict="REJECT",
    net=10.0,
    trades=20,
    fees=5.0,
    pf=1.4,
    payoff=1.5,
    family_attribution=None,
    auto=False,
):
    payload = {
        "exchange": "binanceusdm",
        "symbol": "BTC/USDT:USDT",
        "timeframe": "1h",
        "strategy": strategy,
        "verdict": verdict,
        "oos_net_usd": net,
        "oos_trades": trades,
        "total_fees_usd": fees,
        "profit_factor": pf,
        "payoff_ratio": payoff,
        "profitable_windows_pct": 70.0,
        "gates": "offensive",
        "auto": auto,
    }
    if family_attribution is not None:
        payload["family_attribution"] = family_attribution
    return payload


def test_passed_strategy_enters_judgment_queue_with_taker_route():
    board = build_edge_leaderboard([
        record(verdict="PASS", net=55.0, trades=30, fees=20.0, pf=2.0, payoff=2.2),
        record(strategy="trend_continuation_v1", net=-5.0, trades=40, pf=0.8),
    ])

    top = board["rows"][0]
    assert top["promotion_tier"] == "JUDGMENT_READY"
    assert top["route_decision"] == "TAKER_ALLOWED"
    assert top["optimizer_fitness"]["hard_filters_passed"] is True
    assert top["optimizer_fitness"]["can_trade"] is False
    assert top["can_trade"] is False
    assert top["requires_untouched_judgment"] is True
    assert board["promotion_queue"][0]["next_step"] == "pre_register_untouched_judgment"
    assert board["promotion_queue"][0]["optimizer_fitness"]["hard_filters_passed"] is True
    assert board["policy"]["can_promote"] is False
    assert board["policy"]["optimizer_scorecard"]["can_trade"] is False
    assert board["summary"]["optimizer_hard_filters_passed"] == 1


def test_latest_rejected_judgment_blocks_rolling_pass_from_queue():
    rec = record(
        strategy="trend_continuation_v1",
        verdict="PASS",
        net=140.0,
        trades=51,
        fees=20.0,
        pf=2.0,
        payoff=2.2,
    )
    rec["exchange"] = "bybit"
    rec["symbol"] = "XRP/USDT:USDT"
    board = build_edge_leaderboard(
        [rec],
        judgment_records=[{
            "kind": "judgment",
            "exchange": "bybit",
            "symbol": "XRP/USDT:USDT",
            "strategy_id": "trend_continuation_v1",
            "verdict": "REJECT",
            "window_start": "2024-07-10",
            "window_end": "2025-07-10",
        }],
    )

    row = board["rows"][0]
    assert row["promotion_tier"] == "BLOCKED"
    assert row["route_decision"] == "BLOCKED"
    assert "latest_untouched_judgment_rejected" in row["blockers"]
    assert row["latest_judgment"]["verdict"] == "REJECT"
    assert board["promotion_queue"] == []
    assert board["summary"]["judgment_rejected"] == 1
    assert board["policy"]["judgment_overlay"]["latest_reject_blocks_queue"] is True


def test_quant_family_attribution_creates_isolated_variant_queue_item():
    board = build_edge_leaderboard([
        record(
            net=-12.0,
            trades=40,
            pf=0.9,
            family_attribution={
                "liquidity_sweep": {
                    "trades": 15,
                    "net_usd": 22.0,
                    "total_fees_usd": 9.0,
                    "profit_factor": 1.45,
                    "payoff_ratio": 1.7,
                    "win_rate_pct": 53.0,
                },
                "structure_break": {
                    "trades": 18,
                    "net_usd": -35.0,
                    "total_fees_usd": 8.0,
                    "profit_factor": 0.6,
                    "payoff_ratio": 0.8,
                    "win_rate_pct": 30.0,
                },
            },
        )
    ])

    family = next(r for r in board["rows"] if r["family"] == "liquidity_sweep")
    assert family["scope"] == "family"
    assert family["candidate_id"] == "quant_signal_pack_v1__liquidity_sweep_only"
    assert family["promotion_tier"] == "VARIANT_RESEARCH_READY"
    assert family["route_decision"] == "MAKER_ONLY"
    queue = next(q for q in board["promotion_queue"] if q["family"] == "liquidity_sweep")
    assert queue["next_step"] == "run_isolated_family_variant"


def test_negative_or_sub_pf_rows_are_blocked_not_queued():
    board = build_edge_leaderboard([
        record(net=12.0, trades=25, pf=1.05),
        record(strategy="trend_continuation_v1", net=-1.0, trades=25, pf=2.0),
    ])

    assert all(row["promotion_tier"] == "BLOCKED" for row in board["rows"])
    assert all(not row["optimizer_fitness"]["hard_filters_passed"] for row in board["rows"])
    assert board["promotion_queue"] == []
    assert board["summary"]["blocked"] == 2


def test_optimizer_scorecard_marks_near_miss_without_promotion():
    board = build_edge_leaderboard([
        record(net=120.0, trades=9, fees=20.0, pf=2.8, payoff=2.8),
    ])

    row = board["rows"][0]
    assert row["promotion_tier"] == "BLOCKED"
    assert row["optimizer_fitness"]["hard_filters_passed"] is False
    assert row["optimizer_fitness"]["near_miss"] is True
    assert "min_trades" in {
        item["name"]
        for item in row["optimizer_fitness"]["hard_filters"]
        if not item["passed"]
    }
    assert row["can_promote"] is False
    assert board["summary"]["optimizer_near_misses"] == 1


def test_auto_pass_requires_human_review_not_direct_judgment():
    board = build_edge_leaderboard([
        record(verdict="PASS", auto=True, net=35.0, trades=20, pf=1.5, payoff=2.0)
    ])

    assert board["rows"][0]["promotion_tier"] == "AUTO_PASS_REVIEW"
    assert (
        board["promotion_queue"][0]["next_step"]
        == "human_review_auto_variant_then_pre_register"
    )



def shadow_perf(
    *,
    trades=6,
    net=12.0,
    strategy="quant_signal_pack_v1",
    exchange="binanceusdm",
    symbol="BTC/USDT:USDT",
):
    return {
        "available": True,
        "journals_read": 1,
        "lanes": [{
            "strategy": strategy,
            "exchange": exchange,
            "symbol": symbol,
            "virtual_trades": trades,
            "wins": max(trades - 2, 0),
            "win_rate_pct": 55.0,
            "net_usd": net,
            "profit_factor": 1.4 if net > 0 else 0.6,
            "span_days": 3.5,
            "last_resolution_ts": "2026-07-06T00:00:00+00:00",
            "resolutions": {"stop": 2, "target": trades - 2, "timeout": 0},
            "source_journals": ["lane_shadow.journal.jsonl"],
        }],
    }


def test_live_shadow_positive_annotation_adds_score_bonus_only():
    rec = record(verdict="PASS", net=55.0, trades=30, fees=20.0, pf=2.0, payoff=2.2)
    base = build_edge_leaderboard([rec])["rows"][0]
    board = build_edge_leaderboard([rec], shadow_perf=shadow_perf(trades=6, net=12.0))

    row = board["rows"][0]
    assert row["live_shadow_annotation"] == "LIVE_SHADOW_POSITIVE"
    assert row["live_shadow"]["virtual_trades"] == 6
    assert row["live_shadow"]["net_usd"] == 12.0
    assert row["live_shadow"]["span_days"] == 3.5
    assert row["score"] == base["score"] + 6.0
    # annotation ONLY: tier + governance flags never move
    assert row["promotion_tier"] == base["promotion_tier"] == "JUDGMENT_READY"
    assert row["can_trade"] is False
    assert row["can_promote"] is False
    assert row["requires_untouched_judgment"] is True
    queue = board["promotion_queue"][0]
    assert queue["live_shadow_annotation"] == "LIVE_SHADOW_POSITIVE"
    assert queue["live_shadow"]["virtual_trades"] == 6
    assert queue["can_promote"] is False
    assert board["summary"]["live_shadow_positive"] == 1
    assert board["summary"]["live_shadow_tracked"] == 1


def test_live_shadow_negative_annotation_is_honest_demotion_signal():
    rec = record(verdict="PASS", net=55.0, trades=30, fees=20.0, pf=2.0, payoff=2.2)
    base = build_edge_leaderboard([rec])["rows"][0]
    board = build_edge_leaderboard([rec], shadow_perf=shadow_perf(trades=8, net=-9.0))

    row = board["rows"][0]
    assert row["live_shadow_annotation"] == "LIVE_SHADOW_NEGATIVE"
    assert row["score"] == base["score"] - 6.0
    assert row["promotion_tier"] == base["promotion_tier"]  # never a tier change
    assert board["summary"]["live_shadow_negative"] == 1


def test_live_shadow_below_min_trades_shows_track_record_without_annotation():
    board = build_edge_leaderboard(
        [record(verdict="PASS", net=55.0, trades=30, fees=20.0, pf=2.0, payoff=2.2)],
        shadow_perf=shadow_perf(trades=4, net=50.0),
    )
    row = board["rows"][0]
    assert row["live_shadow"]["virtual_trades"] == 4
    assert row["live_shadow_annotation"] is None
    assert board["summary"]["live_shadow_positive"] == 0


def test_live_shadow_never_attributed_to_family_probe_rows():
    board = build_edge_leaderboard(
        [record(
            net=-12.0, trades=40, pf=0.9,
            family_attribution={
                "liquidity_sweep": {
                    "trades": 15, "net_usd": 22.0, "total_fees_usd": 9.0,
                    "profit_factor": 1.45, "payoff_ratio": 1.7,
                },
            },
        )],
        shadow_perf=shadow_perf(trades=10, net=20.0),
    )
    family = next(r for r in board["rows"] if r["family"] == "liquidity_sweep")
    assert family["live_shadow"] is None
    assert family["live_shadow_annotation"] is None


def test_no_shadow_perf_rows_carry_null_live_shadow():
    board = build_edge_leaderboard(
        [record(verdict="PASS", net=55.0, trades=30, fees=20.0, pf=2.0, payoff=2.2)]
    )
    row = board["rows"][0]
    assert row["live_shadow"] is None
    assert row["live_shadow_annotation"] is None
    assert board["policy"]["live_shadow"]["annotation_only"] is True
    assert board["policy"]["live_shadow"]["never_auto_promotes"] is True
    assert board["policy"]["live_shadow"]["min_virtual_trades"] == 5


def test_execution_truth_negative_blocks_otherwise_passing_lane():
    rec = record(verdict="PASS", net=55.0, trades=30, fees=20.0, pf=2.0, payoff=2.2)
    rec["execution_truth"] = {
        "summary": {
            "verdict": "NEGATIVE_AFTER_COST",
            "samples": 44,
            "executable_samples": 44,
            "positive_net_samples": 11,
            "avg_net_bps": -2.8,
            "profit_factor": 0.72,
            "avg_fill_probability": 1.0,
            "primary_blocker": "average net/PF below maker breakeven",
        }
    }

    board = build_edge_leaderboard([rec])
    row = board["rows"][0]

    assert row["promotion_tier"] == "BLOCKED"
    assert row["route_decision"] == "BLOCKED"
    assert row["execution_truth_annotation"] == "TRUTH_NEGATIVE_AFTER_COST"
    assert "truth_negative_after_cost" in row["blockers"]
    assert board["promotion_queue"] == []
    assert board["summary"]["execution_truth_tracked"] == 1
    assert board["summary"]["execution_truth_blocked"] == 1
    assert board["policy"]["execution_truth"]["never_auto_promotes"] is True


def test_execution_truth_taker_edge_can_upgrade_route_but_not_promote_to_trade():
    rec = record(verdict="PASS", net=55.0, trades=30, fees=20.0, pf=2.0, payoff=2.2)
    rec["execution_truth"] = {
        "verdict": "TAKER_EDGE",
        "samples": 50,
        "executable_samples": 50,
        "positive_net_samples": 32,
        "avg_net_bps": 3.4,
        "profit_factor": 1.55,
        "avg_fill_probability": 1.0,
        "primary_blocker": "taker route clears fee wall",
    }

    board = build_edge_leaderboard([rec])
    row = board["rows"][0]

    assert row["promotion_tier"] == "JUDGMENT_READY"
    assert row["route_decision"] == "TAKER_ALLOWED"
    assert row["execution_truth"]["avg_net_bps"] == 3.4
    assert row["execution_truth_annotation"] == "TRUTH_TAKER_EDGE"
    assert row["can_trade"] is False
    assert board["promotion_queue"][0]["execution_truth_annotation"] == "TRUTH_TAKER_EDGE"
    assert board["summary"]["execution_truth_positive"] == 1
