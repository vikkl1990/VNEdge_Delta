"""Automated diagnosis + bounded uplift-variant proposals."""

from vnedge.research.strategy_diagnostics import diagnose


def record(strategy="funding_mean_reversion_v1", symbol="BTC/USDT:USDT",
           verdict="REJECT", reasons=None, long_net=0.0, short_net=0.0,
           oos_net=-5.0, family_attribution=None):
    payload = {
        "strategy": strategy, "symbol": symbol, "verdict": verdict,
        "reasons": reasons or [], "oos_net_usd": oos_net,
        "attribution": {
            "long": {"net_usd": long_net, "trades": 5, "win_rate_pct": 50.0},
            "short": {"net_usd": short_net, "trades": 5, "win_rate_pct": 50.0},
        },
    }
    if family_attribution is not None:
        payload["family_attribution"] = family_attribution
    return payload


def test_pass_is_healthy_no_suggestions():
    d = diagnose(record(verdict="PASS"))
    assert d.healthy and d.suggestions == ()


def test_side_skew_proposes_side_restriction():
    # short carries (+40), long drags (-5), net still positive but the drag
    # side should be dropped
    d = diagnose(record(
        reasons=["aggregate OOS net $-2.00 is not positive"],
        long_net=-5.0, short_net=40.0, oos_net=-2.0,
    ))
    assert "short side carries; long drags" in d.notes
    ids = [s.variant_id for s in d.suggestions]
    assert "funding_mean_reversion_v1__short_only" in ids
    sug = next(s for s in d.suggestions if "short_only" in s.variant_id)
    assert sug.fixed_params == {"allowed_sides": ["short"]}
    assert sug.goal == "side_restrict"


def test_long_carries_proposes_long_only():
    d = diagnose(record(long_net=40.0, short_net=-5.0, oos_net=-2.0,
                        reasons=["aggregate OOS net not positive"]))
    ids = [s.variant_id for s in d.suggestions]
    assert "funding_mean_reversion_v1__long_only" in ids


def test_low_payoff_proposes_quality_variant():
    d = diagnose(record(
        strategy="volatility_expansion_breakout_v1",
        reasons=["payoff ratio 1.20 < 1.8 (avg win / avg loss)"],
    ))
    goals = [s.goal for s in d.suggestions]
    assert "increase_quality" in goals


def test_quant_pack_family_attribution_proposes_family_restriction():
    d = diagnose(record(
        strategy="quant_signal_pack_v1",
        reasons=["profit factor 1.10 < 1.25"],
        family_attribution={
            "liquidity_sweep": {
                "trades": 12, "net_usd": 42.0, "profit_factor": 1.4,
                "payoff_ratio": 2.1, "win_rate_pct": 50.0,
            },
            "structure_break": {
                "trades": 20, "net_usd": -25.0, "profit_factor": 0.7,
                "payoff_ratio": 0.9, "win_rate_pct": 35.0,
            },
        },
    ))
    assert "liquidity_sweep family carries; mixed families drag" in d.notes
    ids = [s.variant_id for s in d.suggestions]
    assert "quant_signal_pack_v1__liquidity_sweep_only" in ids
    sug = next(s for s in d.suggestions if "liquidity_sweep_only" in s.variant_id)
    assert sug.fixed_params == {"allowed_families": ["liquidity_sweep"]}
    assert sug.goal == "family_restrict"


def test_quant_pack_without_family_winner_focuses_flow_families():
    d = diagnose(record(
        strategy="quant_signal_pack_v1",
        reasons=["payoff ratio 1.20 < 1.8 (avg win / avg loss)"],
    ))
    ids = [s.variant_id for s in d.suggestions]
    assert "quant_signal_pack_v1__sweep_fvg_squeeze_only" in ids


def test_too_few_trades_proposes_frequency_variant():
    d = diagnose(record(
        strategy="panic_reversal_v1",
        reasons=["12 total OOS trades (need >= 15)"],
    ))
    ids = [s.variant_id for s in d.suggestions]
    assert "panic_reversal_v1__looser_panic" in ids


def test_win_concentration_refuses_to_tune():
    d = diagnose(record(
        strategy="volatility_expansion_breakout_v1",
        reasons=["single trade contributes 60% of gross profit — one lucky "
                 "trade is not an edge"],
    ))
    assert any("MORE DATA" in n for n in d.notes)
    # concentration alone offers no parameter suggestion (that would be overfit)
    assert all(s.goal != "increase_frequency" for s in d.suggestions)


def test_is_oos_collapse_flags_overfit_no_tune():
    d = diagnose(record(reasons=["IS/OOS collapse: OOS net retains 2% of IS"]))
    assert any("overfit" in n.lower() for n in d.notes)


def test_suggestions_bounded_to_three():
    d = diagnose(record(
        strategy="volatility_expansion_breakout_v1",
        reasons=["payoff ratio low", "profit factor low",
                 "aggregate OOS net not positive", "drawdown too high"],
    ))
    assert len(d.suggestions) <= 3
    # no duplicate variant ids
    ids = [s.variant_id for s in d.suggestions]
    assert len(ids) == len(set(ids))


def test_diagnosis_is_json_serializable():
    import json

    d = diagnose(record(short_net=40.0, long_net=-5.0, oos_net=-2.0,
                        reasons=["aggregate OOS net not positive"]))
    json.dumps(d.to_dict())  # must not raise


# --- Consecutive-stop clustering -> engine-level protection proposals --------------


def test_consecutive_stops_proposes_protection_variant():
    payload = record(reasons=["aggregate OOS net not positive"])
    payload["max_consecutive_stops"] = 4
    payload["gates"] = "sparse"
    d = diagnose(payload)

    assert "consecutive_stops" in d.failure_tags
    assert any("post-stop cooldown" in n for n in d.notes)
    sug = next(s for s in d.suggestions if s.goal == "protect_after_stop")
    assert sug.variant_id == "funding_mean_reversion_v1__stop_cooldown"
    # whitelisted engine-config axes, namespaced so they can never be
    # mistaken for strategy constructor params
    assert sug.grid_axes == {"protections.cooldown_bars_after_stop": [3, 6]}
    assert sug.fixed_params == {}
    assert sug.gates_label == "sparse"
    assert sug.auto_runnable is False  # research proposal ONLY
    assert len(d.suggestions) <= 3


def test_consecutive_stops_tag_also_parsed_from_reason_text():
    d = diagnose(record(reasons=["4 consecutive stops in OOS trade sequence"]))
    assert "consecutive_stops" in d.failure_tags
    assert any(s.goal == "protect_after_stop" for s in d.suggestions)


def test_short_stop_runs_do_not_propose_protections():
    payload = record(reasons=["aggregate OOS net not positive"])
    payload["max_consecutive_stops"] = 2  # below the threshold
    d = diagnose(payload)
    assert "consecutive_stops" not in d.failure_tags
    assert all(s.goal != "protect_after_stop" for s in d.suggestions)


def test_protection_variant_excluded_from_auto_explore():
    from vnedge.research.edge_agents import (
        EdgeResearchAgent,
        runnable_variant_proposals,
    )

    payload = record(reasons=["aggregate OOS net not positive"])
    payload["max_consecutive_stops"] = 5
    plan = EdgeResearchAgent().plan([payload])

    proposed = [p for p in plan.proposals
                if p.get("goal") == "protect_after_stop"]
    assert len(proposed) == 1  # visible to humans on the research feed
    assert proposed[0]["auto_runnable"] is False
    # ... but the auto-explorer never picks it up
    runnable = runnable_variant_proposals(plan)
    assert all(p["goal"] != "protect_after_stop" for p in runnable)


def test_ordinary_variants_remain_auto_runnable():
    from vnedge.research.edge_agents import (
        EdgeResearchAgent,
        runnable_variant_proposals,
    )

    plan = EdgeResearchAgent().plan(
        [record(reasons=["payoff ratio 1.20 < 1.8 (avg win / avg loss)"],
                strategy="volatility_expansion_breakout_v1")]
    )
    assert any(p["goal"] == "increase_quality"
               for p in runnable_variant_proposals(plan))


def test_wf_record_reports_max_consecutive_stops():
    import pandas as pd

    from vnedge.backtest.backtester import Trade
    from vnedge.backtest.metrics import BacktestMetrics
    from vnedge.backtest.walk_forward import WalkForwardResult, WindowResult
    from vnedge.research import continuous_research as cr
    from vnedge.backtest.walk_forward import SPARSE_STRATEGY_GATES

    def ts(i):
        return pd.Timestamp(1_750_000_000_000 + i * 3_600_000, unit="ms", tz="UTC")

    def trade(exit_reason, net=1.0):
        return Trade(side="long", quantity=1.0, entry_ts=ts(0), entry_price=100.0,
                     exit_ts=ts(1), exit_price=100.0 + net, exit_reason=exit_reason,
                     gross_pnl_usd=net, fees_usd=0.0, funding_usd=0.0,
                     entry_reason="t")

    def m():
        return BacktestMetrics(
            num_trades=6, skipped_by_sizing=0, net_profit_usd=15.0, return_pct=3.0,
            max_drawdown_pct=2.0, sharpe=1.0, sortino=1.1, profit_factor=1.5,
            win_rate_pct=60.0, avg_win_usd=6.0, avg_loss_usd=-4.0,
            total_fees_usd=1.0, total_funding_usd=0.0, exit_reasons={},
        )

    windows = (
        WindowResult(0, ts(0), ts(1), ts(2), {}, m(), m(), test_trades=(
            trade("stop", -1.0), trade("stop", -1.0), trade("take_profit"),
            trade("stop", -1.0), trade("tick_stop", -1.0), trade("stop", -1.0),
        )),
    )
    rec = cr.wf_record("x", "BTC/USDT:USDT",
                       WalkForwardResult(windows=windows), SPARSE_STRATEGY_GATES)
    assert rec["max_consecutive_stops"] == 3  # stop, tick_stop, stop run
