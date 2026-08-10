"""Promotion red-team — the code-calculated prosecutor of passed candidates.

Pins: every charge cites a real number; a fee-drag that flips the edge negative
blocks; a strong candidate draws only the single-symbol caveat; and the red-team
can never promote or trade.
"""

import json

from vnedge.research import data_burn
from vnedge.research.promotion_red_team import (
    CRITICAL,
    prosecute,
    red_team_candidates,
)


STRONG = {
    "oos_net_usd": 100.0, "oos_trades": 50, "total_fees_usd": 20.0,
    "profit_factor": 1.6, "payoff_ratio": 2.0, "profitable_windows_pct": 80.0,
    "windows": 7, "traded_windows": 7,
}


def test_strong_candidate_draws_only_the_single_symbol_caveat():
    brief = prosecute(STRONG, strategy_id="funding_mr", symbol="BTC/USDT")
    d = brief.to_dict()
    assert d["critical_count"] == 0 and d["warn_count"] == 0
    assert [c["name"] for c in d["charges"]] == ["single_symbol"]
    assert d["recommendation"] == "DEFENSIBLE_BUT_HUMAN_GATED"


def test_fee_drag_that_flips_edge_negative_blocks():
    m = dict(STRONG, oos_net_usd=10.0, total_fees_usd=12.0)  # fees > net
    brief = prosecute(m, symbol="BTC/USDT")
    fee = [c for c in brief.charges if c.name == "fee_drag"][0]
    assert fee.severity == CRITICAL
    assert fee.evidence["fee_to_net_ratio"] == 1.2   # cites the real number
    assert brief.recommendation == "DO_NOT_PROMOTE_YET"


def test_two_soft_warnings_need_answers():
    # sparse sample (WARN) + thin payoff (WARN), nothing critical
    m = dict(STRONG, oos_trades=12, payoff_ratio=1.1)
    brief = prosecute(m, symbol="ETH/USDT")
    names = {c.name for c in brief.charges}
    assert "sparse_sample" in names and "thin_payoff" in names
    assert brief.to_dict()["critical_count"] == 0
    assert brief.recommendation == "NEEDS_ANSWERS"


def test_thin_edge_per_trade_is_quantified():
    m = dict(STRONG, oos_net_usd=8.0, oos_trades=40)  # $0.20/trade
    brief = prosecute(m, symbol="XRP/USDT")
    thin = [c for c in brief.charges if c.name == "thin_edge"][0]
    assert thin.evidence["net_per_trade_usd"] == 0.2


def test_red_team_is_always_powerless():
    brief = prosecute(STRONG, symbol="BTC/USDT")
    d = brief.to_dict()
    assert d["can_promote"] is False and d["can_trade"] is False


def test_prosecutes_only_passed_walk_forward_candidates(tmp_path):
    feed = tmp_path / "feed.jsonl"
    rows = [
        {"strategy": "s1", "symbol": "BTC/USDT", "exchange": "b", "timeframe": "1h",
         "verdict": "PASS", "updated": "t", "oos_net_usd": 10.0, "oos_trades": 40,
         "total_fees_usd": 12.0},                                   # PASS → prosecuted
        {"strategy": "s2", "symbol": "ETH/USDT", "exchange": "b", "timeframe": "1h",
         "verdict": "REJECT", "updated": "t", "oos_net_usd": -5.0},  # REJECT → skipped
    ]
    feed.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    out = red_team_candidates(
        feed_path=feed, burn_registry_path=tmp_path / "none.jsonl",
        paper_trials_dir=tmp_path / "none",
    )
    assert out["summary"]["candidates_prosecuted"] == 1
    assert out["briefs"][0]["strategy_id"] == "s1"
    assert out["summary"]["do_not_promote_yet"] == 1
    assert out["policy"]["can_promote"] is False


def test_burn_registry_judgments_are_not_prosecuted_here(tmp_path):
    # Only walk_forward PASSes are candidates; burn-registry judgments carry no
    # walk-forward metrics, so the red-team leaves them to the index.
    burn = tmp_path / "burn_registry.jsonl"
    data_burn.record_judgment("s1", "BTC/USDT", "b", "2024-01-01", "2025-01-01", "PASS", path=burn)
    out = red_team_candidates(
        feed_path=tmp_path / "none.jsonl", burn_registry_path=burn,
        paper_trials_dir=tmp_path / "none",
    )
    assert out["summary"]["candidates_prosecuted"] == 0
