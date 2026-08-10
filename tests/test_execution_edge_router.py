"""Execution edge router: opportunities become skip/maker/taker truth labels."""

import pandas as pd

from vnedge.research.execution_edge_router import (
    OpportunityRouterConfig,
    build_router_report,
    label_opportunities,
    label_strategy_opportunities,
    summarize_routes,
)
from vnedge.research.execution_edge_labeler import SignalEvent
from vnedge.scalping.parameter_registry import ExchangeFeeProfile
from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent


def candles(rows):
    ts = pd.date_range("2026-07-14T00:00:00Z", periods=len(rows), freq="5min")
    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": [r[0] for r in rows],
            "high": [r[1] for r in rows],
            "low": [r[2] for r in rows],
            "close": [r[3] for r in rows],
            "volume": [100.0] * len(rows),
        }
    )


def fee(**kwargs):
    base = dict(
        exchange="test",
        maker_bps=1.0,
        taker_bps=5.0,
        slippage_bps=1.0,
        safety_buffer_bps=0.0,
    )
    base.update(kwargs)
    return ExchangeFeeProfile(**base)


def event(df, *, stop=99.0, target=101.0, fill_probability=None, expected_edge_bps=None):
    return SignalEvent(
        event_id="edge|1",
        ts=df["timestamp"].iloc[1],
        side="long",
        stop_price=stop,
        take_profit_price=target,
        source_id="test",
        strategy_id="test_strategy",
        fill_probability=fill_probability,
        expected_edge_bps=expected_edge_bps,
    )


def cfg(**kwargs):
    params = dict(
        horizon_bars=3,
        min_samples=1,
        min_expected_net_edge_bps=25.0,
        min_profit_factor=1.5,
        maker_fill_probability=0.65,
        maker_fill_floor=0.5,
        maker_fallback_fill_floor=0.25,
        taker_extra_buffer_bps=5.0,
    )
    params.update(kwargs)
    return OpportunityRouterConfig(**params)


def test_positive_edge_prefers_maker_when_fill_confidence_is_good():
    df = candles(
        [
            (100.0, 100.2, 99.8, 100.0),
            (100.0, 100.2, 99.8, 100.0),
            (100.0, 101.4, 99.9, 101.1),
        ]
    )

    routes = label_opportunities(
        df,
        [event(df, target=101.0)],
        exchange="test",
        config=cfg(),
        fee_profile=fee(),
    )

    assert routes[0].action == "MAKER"
    assert routes[0].selected_route == "MAKER_ONLY"
    assert routes[0].selected_net_bps == 93.0
    assert routes[0].taker_net_bps == 89.0
    assert routes[0].mfe_after_cost_bps == 133.0
    assert routes[0].hold_bars == 1
    assert routes[0].time_to_mfe_bars == 1
    assert routes[0].capture_ratio == 0.7143
    assert routes[0].exit_diagnosis == "CAPTURED_AFTER_COST"
    assert routes[0].can_trade is False
    assert routes[0].paper_margin_usd == 100.0
    assert routes[0].paper_leverage == 25.0
    assert routes[0].paper_notional_usd == 2500.0
    assert routes[0].selected_net_usd == 23.25


def test_low_maker_confidence_uses_fallback_only_when_taker_clears_buffer():
    df = candles(
        [
            (100.0, 100.2, 99.8, 100.0),
            (100.0, 100.2, 99.8, 100.0),
            (100.0, 101.4, 99.9, 101.1),
        ]
    )

    routes = label_opportunities(
        df,
        [event(df, target=101.0, fill_probability=0.30, expected_edge_bps=35.0)],
        exchange="test",
        config=cfg(),
        fee_profile=fee(),
    )

    assert routes[0].action == "MAKER_THEN_TAKER"
    assert routes[0].selected_route == "MAKER_THEN_TAKER"
    assert routes[0].reason == "maker edge exists but fill confidence needs taker fallback"


def test_taker_now_requires_edge_after_taker_extra_buffer():
    df = candles(
        [
            (100.0, 100.2, 99.8, 100.0),
            (100.0, 100.2, 99.8, 100.0),
            (100.0, 101.4, 99.9, 101.1),
        ]
    )

    routes = label_opportunities(
        df,
        [event(df, target=101.0, fill_probability=0.05, expected_edge_bps=35.0)],
        exchange="test",
        config=cfg(),
        fee_profile=fee(),
    )

    assert routes[0].action == "TAKER_NOW"
    assert routes[0].selected_route == "TAKER_ALLOWED"
    assert routes[0].reason == "taker route clears fee wall plus safety buffer"


def test_fee_wall_skips_small_visual_signal():
    df = candles(
        [
            (100.0, 100.2, 99.8, 100.0),
            (100.0, 100.2, 99.8, 100.0),
            (100.0, 100.12, 99.9, 100.08),
        ]
    )

    routes = label_opportunities(
        df,
        [event(df, target=100.08, expected_edge_bps=10.0)],
        exchange="test",
        config=cfg(),
        fee_profile=fee(),
    )

    assert routes[0].action == "SKIP"
    assert routes[0].selected_net_bps is None
    assert routes[0].reason == "ex-ante expected edge below floor"


class AlwaysLongStrategy(BaseStrategy):
    strategy_id = "always_long_router_test"
    warmup_bars = 1

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        return candles.copy()

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        if index == 1:
            return SignalIntent("long", stop_price=99.0, take_profit_price=101.0)
        if index == 3:
            return SignalIntent("long", stop_price=99.0, take_profit_price=102.0)
        return None


def test_strategy_opportunities_summarize_paper_candidate_without_trade_permission():
    df = candles(
        [
            (100.0, 100.2, 99.8, 100.0),
            (100.0, 100.2, 99.8, 100.0),
            (100.0, 101.4, 99.9, 101.1),
            (101.1, 101.2, 100.8, 101.0),
            (101.0, 102.3, 100.9, 102.0),
            (102.0, 102.1, 101.8, 102.0),
        ]
    )

    routes = label_strategy_opportunities(
        df,
        AlwaysLongStrategy(),
        exchange="test",
        config=cfg(min_samples=2),
        fee_profile=fee(),
    )
    summary = summarize_routes(routes, config=cfg(min_samples=2))
    report = build_router_report(
        exchange="test",
        symbol="ETH/USD:USD",
        timeframe="5m",
        strategy_id=AlwaysLongStrategy.strategy_id,
        opportunities=routes,
        config=cfg(min_samples=2),
    )

    assert len(routes) == 2
    assert summary.verdict == "MAKER_EDGE"
    assert summary.paper_candidate is True
    assert summary.avg_hold_bars == 1.0
    assert summary.avg_mfe_after_cost_bps > 120.0
    assert summary.fee_wall_break_rate_pct == 100.0
    assert summary.paper_margin_usd == 100.0
    assert summary.paper_leverage == 25.0
    assert summary.paper_notional_usd == 2500.0
    assert summary.selected_net_usd == 46.2525
    assert summary.exit_diagnosis_counts == {"CAPTURED_AFTER_COST": 2}
    assert summary.can_trade is False
    assert report["policy"]["decision_uses_forward_truth"] is False
    assert report["summary"]["can_promote"] is False
