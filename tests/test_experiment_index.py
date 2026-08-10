"""Experiment index — the read-only join over VNEDGE's scattered run stores.

The discipline these tests pin: only a pre-registered untouched-judgment PASS is
``promotable``; rolling walk-forward and paper-forward runs are surfaced but
never promotable via this index. And it must never duplicate a write path — it
only reads existing artifacts.
"""

import json

from vnedge.research import data_burn
from vnedge.research.experiment_index import (
    KIND_PAPER_TRIAL,
    KIND_UNTOUCHED_JUDGMENT,
    KIND_WALK_FORWARD,
    PROV_UNTOUCHED,
    best,
    build_experiment_index,
    distinct,
    query,
    _records_from_feed,
)
from vnedge.research.experiment_index import RunRecord


def _write_feed(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def _feed_row(**kw):
    base = {
        "strategy": "funding_mr", "symbol": "BTC/USDT", "exchange": "binanceusdm",
        "timeframe": "1h", "verdict": "REJECT", "updated": "2026-07-30T00:00:00+00:00",
        "oos_net_usd": 5.0, "oos_trades": 20, "profit_factor": 1.2, "reasons": ["thin"],
    }
    base.update(kw)
    return base


def test_index_joins_all_three_stores(tmp_path):
    feed = tmp_path / "feed.jsonl"
    _write_feed(feed, [_feed_row(), _feed_row(verdict="PASS", oos_net_usd=30.0)])
    burn = tmp_path / "burn_registry.jsonl"
    data_burn.record_judgment(
        "funding_mr", "BTC/USDT", "binanceusdm",
        "2024-07-03T00:00:00+00:00", "2025-07-03T00:00:00+00:00", "PASS",
        note="round3", path=burn,
    )
    trials = tmp_path / "paper_trials"
    trials.mkdir()
    (trials / "btc.reports.jsonl").write_text(json.dumps({
        "ts": "2026-07-29T00:00:00+00:00", "trial_id": "btc_v1", "run_commit": "abc123",
        "manifest_strategy": "funding_mr",
        "report": {"strategy_id": "funding_mr", "symbol": "BTC/USDT",
                   "realized_pnl_usd": 12.5, "final_equity_usd": 512.5},
    }) + "\n")

    payload = build_experiment_index(
        feed_path=feed, burn_registry_path=burn, paper_trials_dir=trials,
    )
    kinds = payload["summary"]["by_kind"]
    assert kinds[KIND_WALK_FORWARD] == 2
    assert kinds[KIND_UNTOUCHED_JUDGMENT] == 1
    assert kinds[KIND_PAPER_TRIAL] == 1
    assert payload["summary"]["total"] == 4


def test_only_untouched_judgment_pass_is_promotable(tmp_path):
    feed = tmp_path / "feed.jsonl"
    # A PASS in rolling research must NOT be promotable — the trap this guards.
    _write_feed(feed, [_feed_row(verdict="PASS", oos_net_usd=99.0)])
    burn = tmp_path / "burn_registry.jsonl"
    data_burn.record_judgment(
        "funding_mr", "BTC/USDT", "binanceusdm",
        "2024-07-03T00:00:00+00:00", "2025-07-03T00:00:00+00:00", "PASS", path=burn,
    )
    data_burn.record_judgment(
        "trend_cont", "ETH/USDT", "binanceusdm",
        "2024-01-01T00:00:00+00:00", "2024-06-01T00:00:00+00:00", "REJECT", path=burn,
    )
    payload = build_experiment_index(feed_path=feed, burn_registry_path=burn,
                                     paper_trials_dir=tmp_path / "none")
    promotable = payload["promotable"]
    assert len(promotable) == 1
    assert promotable[0]["strategy_id"] == "funding_mr"
    assert promotable[0]["data_provenance"] == PROV_UNTOUCHED
    assert promotable[0]["verdict"] == "PASS"


def test_query_best_distinct(tmp_path):
    records = [
        RunRecord("a", KIND_WALK_FORWARD, "s1", "BTC/USDT", "b", "1h", "PASS",
                  "rolling_research", False, "t", metrics={"oos_net_usd": 10.0}),
        RunRecord("b", KIND_WALK_FORWARD, "s1", "ETH/USDT", "b", "1h", "REJECT",
                  "rolling_research", False, "t", metrics={"oos_net_usd": 40.0}),
        RunRecord("c", KIND_UNTOUCHED_JUDGMENT, "s2", "BTC/USDT", "b", "", "PASS",
                  PROV_UNTOUCHED, True, "t", metrics={}),
    ]
    assert {r.run_id for r in query(records, strategy_id="s1")} == {"a", "b"}
    assert [r.run_id for r in query(records, promotable=True)] == ["c"]
    assert [r.run_id for r in best(records, metric="oos_net_usd", limit=1)] == ["b"]
    assert distinct(records, "symbol") == ["BTC/USDT", "ETH/USDT"]


def test_missing_files_yield_empty_index(tmp_path):
    payload = build_experiment_index(
        feed_path=tmp_path / "nope.jsonl",
        burn_registry_path=tmp_path / "nobody.jsonl",
        paper_trials_dir=tmp_path / "nothing",
    )
    assert payload["summary"]["total"] == 0
    assert payload["promotable"] == []
    assert payload["policy"]["can_promote"] is False


def test_torn_jsonl_tail_does_not_lose_the_index(tmp_path):
    feed = tmp_path / "feed.jsonl"
    feed.write_text(json.dumps(_feed_row()) + "\n" + '{"strategy": "broke", "verd')  # torn tail
    records = _records_from_feed(feed)
    assert len(records) == 1 and records[0].strategy_id == "funding_mr"


def test_auto_explore_rows_are_labelled_and_non_promotable(tmp_path):
    feed = tmp_path / "feed.jsonl"
    _write_feed(feed, [_feed_row(verdict="PASS", auto=True)])
    recs = _records_from_feed(feed)
    assert recs[0].note == "auto_explore"
    assert recs[0].promotable is False
