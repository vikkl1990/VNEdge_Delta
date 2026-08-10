from vnedge.research.optimizer_scorecard import (
    OptimizerScorecardConfig,
    build_optimizer_scorecard,
    optimizer_scorecard_policy,
)


def test_scorecard_separates_hard_filters_from_weighted_fitness():
    card = build_optimizer_scorecard(
        net_usd=18.0,
        trades=8,
        fees_usd=6.0,
        profit_factor=1.55,
        payoff_ratio=2.1,
        profitable_windows_pct=75.0,
        config=OptimizerScorecardConfig(min_trades=10, min_profit_factor=1.25),
    )

    filters = {item["name"]: item for item in card["hard_filters"]}
    components = {item["name"]: item for item in card["components"]}

    assert card["can_trade"] is False
    assert card["can_promote"] is False
    assert card["hard_filters_passed"] is False
    assert filters["min_trades"]["passed"] is False
    assert filters["positive_net_after_fees"]["passed"] is True
    assert filters["min_profit_factor"]["passed"] is True
    assert components["profit_factor"]["contribution"] > 0
    assert components["trade_sample"]["raw"] == 8.0
    assert card["score"] > 0


def test_scorecard_policy_is_research_only():
    policy = optimizer_scorecard_policy(
        OptimizerScorecardConfig(min_trades=20, min_profit_factor=1.5)
    )

    assert policy["source"].startswith("OctoBot optimizer")
    assert policy["research_only"] is True
    assert policy["can_trade"] is False
    assert policy["can_promote"] is False
    assert policy["config"]["min_trades"] == 20
    assert "min_profit_factor" in policy["hard_filters"]
