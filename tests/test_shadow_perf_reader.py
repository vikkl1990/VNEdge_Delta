"""Shadow-lane virtual performance reader — aggregation, degradation, folding.

The reader turns per-lane ``shadow_outcome`` journal records into
per-(strategy, exchange, symbol) live track records for the edge leaderboard.
Read-only and graceful: missing dirs, torn lines, and unattributable journals
degrade to "no data", never to an exception.
"""

import json

from vnedge.research import continuous_research as cr
from vnedge.research.shadow_perf_reader import (
    index_shadow_perf,
    read_shadow_perf,
    shadow_perf_key,
)

STRATEGY = "funding_mean_reversion_v1"
SYMBOL = "BTC/USDT:USDT"


def intent_record(key, *, strategy=STRATEGY, symbol=SYMBOL, approved=True):
    return {
        "ts": "2026-07-01T00:00:00+00:00",
        "kind": "shadow_intent",
        "payload": {
            "intent_key": key,
            "approved": approved,
            "intent": {
                "symbol": symbol,
                "side": "long",
                "quantity": 0.01,
                "notional_usd": 500.0,
                "leverage": 1.0,
                "reduce_only": False,
                "strategy_id": strategy,
            },
            "stop_price": 49000.0,
            "take_profit_price": 51000.0,
            "bar_ts": "2026-07-01T00:00:00+00:00",
            "signal_reason": "test",
        },
    }


def outcome_record(key, net, *, resolution="target", bar_ts="2026-07-02T00:00:00+00:00"):
    return {
        "ts": bar_ts,
        "kind": "shadow_outcome",
        "payload": {
            "intent_key": key,
            "resolution": resolution,
            "bars_held": 3,
            "virtual_net_usd": net,
            "side": "long",
            "entry_price": 50000.0,
            "exit_price": 50000.0 + net,
            "fees_usd": 0.5,
            "bar_ts": bar_ts,
            "signal_reason": "test",
        },
    }


def write_journal(path, records):
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def test_reader_aggregates_one_lane(tmp_path):
    write_journal(
        tmp_path / "funding_mr_binanceusdm_btc_usdt_usdt_shadow.journal.jsonl",
        [
            intent_record("k1"),
            intent_record("k2"),
            intent_record("k3"),
            outcome_record("k1", 10.0, resolution="target",
                           bar_ts="2026-07-02T00:00:00+00:00"),
            outcome_record("k2", -4.0, resolution="stop",
                           bar_ts="2026-07-03T00:00:00+00:00"),
            outcome_record("k3", 2.0, resolution="timeout",
                           bar_ts="2026-07-04T00:00:00+00:00"),
        ],
    )

    payload = read_shadow_perf(tmp_path)

    assert payload["available"] is True
    assert payload["journals_read"] == 1
    assert payload["policy"]["can_trade"] is False
    assert payload["policy"]["can_promote"] is False
    (lane,) = payload["lanes"]
    assert lane["strategy"] == STRATEGY
    assert lane["exchange"] == "binanceusdm"
    assert lane["symbol"] == SYMBOL
    assert lane["virtual_trades"] == 3
    assert lane["wins"] == 2
    assert lane["win_rate_pct"] == 66.7
    assert lane["net_usd"] == 8.0
    assert lane["profit_factor"] == 3.0  # 12 gross win / 4 gross loss
    assert lane["span_days"] == 2.0
    assert lane["last_resolution_ts"] == "2026-07-04T00:00:00+00:00"
    assert lane["resolutions"] == {"stop": 1, "target": 1, "timeout": 1}


def test_reader_dedupes_intent_keys_across_journal_copies(tmp_path):
    records = [
        intent_record("k1"),
        outcome_record("k1", 10.0),
        outcome_record("k1", 10.0),  # duplicate outcome in the same file
    ]
    write_journal(tmp_path / "funding_mr_bybit_btc_usdt_usdt_shadow.journal.jsonl", records)
    # a stray copy of the same lane journal must not double-count
    write_journal(tmp_path / "funding_mr_bybit_btc_usdt_usdt_shadow_copy.journal.jsonl", records)

    (lane,) = read_shadow_perf(tmp_path)["lanes"]
    assert lane["exchange"] == "bybit"
    assert lane["virtual_trades"] == 1
    assert lane["net_usd"] == 10.0
    assert len(lane["source_journals"]) == 2


def test_reader_skips_malformed_lines_and_unattributable_journals(tmp_path):
    good = tmp_path / "trend_continuation_xrp_bybit_shadow.journal.jsonl"
    good.write_text(
        "not json at all\n"
        + json.dumps(["a", "list"]) + "\n"
        + json.dumps(intent_record("k1", strategy="trend_continuation_v1",
                                   symbol="XRP/USDT:USDT")) + "\n"
        + json.dumps(outcome_record("k1", 5.0)) + "\n"
    )
    # outcomes with no shadow_intent identity: never guess — skip the journal
    write_journal(
        tmp_path / "mystery_shadow.journal.jsonl", [outcome_record("kx", 99.0)]
    )
    # non-shadow journals are never even read
    write_journal(tmp_path / "paper_lane.journal.jsonl", [outcome_record("kp", 42.0)])

    payload = read_shadow_perf(tmp_path)

    assert payload["journals_read"] == 1
    (lane,) = payload["lanes"]
    assert lane["strategy"] == "trend_continuation_v1"
    assert lane["exchange"] == "bybit"
    assert lane["net_usd"] == 5.0


def test_reader_missing_dir_degrades_gracefully(tmp_path):
    payload = read_shadow_perf(tmp_path / "does_not_exist")
    assert payload["available"] is False
    assert payload["lanes"] == []
    assert payload["journals_read"] == 0


def test_exchange_inference_prefers_longest_match(tmp_path):
    write_journal(
        tmp_path / "funding_mr_delta_india_btc_usd_usd_shadow.journal.jsonl",
        [intent_record("k1", symbol="BTC/USD:USD"), outcome_record("k1", 1.0)],
    )
    write_journal(
        tmp_path / "someexoticvenue_shadow.journal.jsonl",
        [intent_record("k2"), outcome_record("k2", 2.0)],
    )

    lanes = {lane["exchange"]: lane for lane in read_shadow_perf(tmp_path)["lanes"]}
    assert set(lanes) == {"delta_india", "unknown"}
    assert lanes["delta_india"]["symbol"] == "BTC/USD:USD"


def test_index_shadow_perf_builds_join_keys(tmp_path):
    write_journal(
        tmp_path / "funding_mr_binanceusdm_btc_usdt_usdt_shadow.journal.jsonl",
        [intent_record("k1"), outcome_record("k1", 3.0)],
    )
    index = index_shadow_perf(read_shadow_perf(tmp_path))
    key = shadow_perf_key(STRATEGY, "binanceusdm", SYMBOL)
    assert key == f"{STRATEGY}|binanceusdm|BTCUSDT"
    assert index[key]["net_usd"] == 3.0
    assert index_shadow_perf(None) == {}
    assert index_shadow_perf({}) == {}


def test_publish_folds_live_shadow_perf_into_latest(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "OUT_DIR", tmp_path / "live_research")
    perf = {
        "available": True,
        "journals_read": 1,
        "lanes": [{"strategy": STRATEGY, "exchange": "binanceusdm",
                   "symbol": SYMBOL, "virtual_trades": 6, "net_usd": 9.5}],
    }
    cr.publish(cr.ResearchPayload(started=0.0, live_shadow_perf=perf))
    latest = json.loads((tmp_path / "live_research" / "latest.json").read_text())
    assert latest["live_shadow_perf"]["available"] is True
    assert latest["live_shadow_perf"]["lanes"][0]["virtual_trades"] == 6

    cr.publish(cr.ResearchPayload(started=0.0))  # absent -> {} placeholder, never a crash
    latest = json.loads((tmp_path / "live_research" / "latest.json").read_text())
    assert latest["live_shadow_perf"] == {}
