"""Multi-exchange research universe + bounded edge-agent planning."""

from vnedge.research.edge_agents import EdgeResearchAgent, runnable_variant_proposals
from vnedge.research.universe import (
    ResearchTarget,
    load_research_targets,
    profitable_pairs,
    summarize_universe,
    targets_from_markets,
)


def record(
    *,
    exchange="binanceusdm",
    strategy="funding_mean_reversion_v1",
    symbol="BTC/USDT:USDT",
    verdict="PASS",
    net=25.0,
    trades=18,
    reasons=None,
):
    return {
        "exchange": exchange,
        "strategy": strategy,
        "symbol": symbol,
        "timeframe": "1h",
        "verdict": verdict,
        "oos_net_usd": net,
        "oos_trades": trades,
        "gates": "sparse",
        "reasons": reasons or [],
        "attribution": {
            "long": {"trades": 4, "net_usd": -8.0, "win_rate_pct": 25.0},
            "short": {"trades": 14, "net_usd": 33.0, "win_rate_pct": 64.0},
        },
    }


def test_load_research_targets_allows_per_exchange_symbol_overrides(monkeypatch):
    monkeypatch.setenv("RESEARCH_EXCHANGES", "binanceusdm,bybit")
    monkeypatch.setenv("RESEARCH_SYMBOLS", "BTC/USDT:USDT,ETH/USDT:USDT")
    monkeypatch.setenv("RESEARCH_SYMBOLS_BYBIT", "SOL/USDT:USDT")

    targets = load_research_targets()

    assert targets == (
        ResearchTarget("binanceusdm", "BTC/USDT:USDT", "1h"),
        ResearchTarget("binanceusdm", "ETH/USDT:USDT", "1h"),
        ResearchTarget("bybit", "SOL/USDT:USDT", "1h"),
    )
    assert summarize_universe(targets)["targets_by_exchange"] == {
        "binanceusdm": 2,
        "bybit": 1,
    }


def test_delta_india_defaults_to_usd_settled_perp_symbols(monkeypatch):
    monkeypatch.setenv("RESEARCH_EXCHANGES", "binanceusdm,delta_india")
    monkeypatch.setenv("RESEARCH_SYMBOLS", "BTC/USDT:USDT,ETH/USDT:USDT")
    monkeypatch.delenv("RESEARCH_SYMBOLS_DELTA_INDIA", raising=False)

    targets = load_research_targets()

    assert ResearchTarget("binanceusdm", "BTC/USDT:USDT", "1h") in targets
    assert ResearchTarget("delta_india", "BTC/USD:USD", "1h") in targets
    assert ResearchTarget("delta_india", "ETH/USD:USD", "1h") in targets


def test_targets_from_markets_discovers_active_linear_derivatives_only():
    markets = {
        "BTC/USDT:USDT": {
            "symbol": "BTC/USDT:USDT",
            "active": True,
            "swap": True,
            "linear": True,
            "quote": "USDT",
            "settle": "USDT",
        },
        "ETH/USDC:USDC": {
            "symbol": "ETH/USDC:USDC",
            "active": True,
            "future": True,
            "linear": True,
            "quote": "USDC",
            "settle": "USDC",
        },
        "DOGE/USDT": {
            "symbol": "DOGE/USDT",
            "active": True,
            "spot": True,
            "quote": "USDT",
        },
        "BTC/USD:BTC": {
            "symbol": "BTC/USD:BTC",
            "active": True,
            "swap": True,
            "linear": False,
            "quote": "USD",
            "settle": "BTC",
        },
        "SOL/USDT:USDT-260626-C": {
            "symbol": "SOL/USDT:USDT-260626-C",
            "active": True,
            "option": True,
            "quote": "USDT",
        },
        "XRP/USDT:USDT": {
            "symbol": "XRP/USDT:USDT",
            "active": False,
            "swap": True,
            "linear": True,
            "quote": "USDT",
        },
    }

    targets = targets_from_markets("bybit", markets, quote_assets=("USDT", "USDC"))

    assert targets == (
        ResearchTarget("bybit", "BTC/USDT:USDT", "1h"),
        ResearchTarget("bybit", "ETH/USDC:USDC", "1h"),
    )
    assert targets_from_markets("bybit", markets, max_symbols=1) == (
        ResearchTarget("bybit", "BTC/USDT:USDT", "1h"),
    )


def test_profitable_pairs_keeps_best_lane_per_exchange_symbol():
    rows = [
        record(strategy="trend_continuation_v1", net=5.0, trades=16, verdict="REJECT"),
        record(strategy="funding_mean_reversion_v1", net=20.0, trades=18, verdict="PASS"),
        record(exchange="bybit", net=12.0, trades=12, verdict="REJECT"),
    ]

    pairs = profitable_pairs(rows)

    assert [p.exchange for p in pairs] == ["binanceusdm", "bybit"]
    assert pairs[0].best_strategy == "funding_mean_reversion_v1"
    assert pairs[0].verdict == "PASS"
    assert pairs[1].oos_net_usd == 12.0


def test_edge_agent_proposals_are_exploratory_and_non_trading():
    rows = [
        record(verdict="REJECT", net=-4.0, trades=20,
               reasons=["aggregate OOS net $-4.00 is not positive"]),
        record(exchange="bybit", verdict="PASS", net=18.0, trades=15),
    ]
    targets = (
        ResearchTarget("binanceusdm", "BTC/USDT:USDT"),
        ResearchTarget("bybit", "BTC/USDT:USDT"),
        ResearchTarget("delta_india", "BTC/USDT:USDT"),
    )

    plan = EdgeResearchAgent(max_variant_proposals=3).plan(rows, targets=targets)
    variants = runnable_variant_proposals(plan)

    assert plan.policy["can_trade"] is False
    assert plan.policy["can_promote"] is False
    assert plan.policy["requires_untouched_judgment"] is True
    assert any(p["proposal_type"] == "pre_registered_judgment" for p in plan.proposals)
    assert any(p["proposal_type"] == "cross_exchange_validation" and
               p["exchange"] == "delta_india" for p in plan.proposals)
    assert variants
    assert variants[0]["proposal_id"].startswith("variant|binanceusdm|BTC/USDT:USDT")
    assert variants[0]["auto_runnable"] is True
    assert variants[0]["can_trade"] is False
