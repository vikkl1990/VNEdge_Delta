"""Research-winner -> shadow-lane manifest: locked-params guardrail, gated consume."""

import json

from vnedge.research.shadow_manifest import (
    RUNTIME_LOCKED_PARAMS,
    generate_shadow_manifest,
    load_shadow_manifest,
    write_shadow_manifest,
)
from vnedge.runtime.multi_lane_shadow import (
    candidate_shadow_lanes,
    dedupe_lane_specs,
    manifest_shadow_lanes,
)
from vnedge.runtime.multi_lane import LaneSpec
from vnedge.runtime.runner_config import RunnerMode


def _pair(exchange, symbol, strategy, verdict="PASS", net=20.0):
    return {"exchange": exchange, "symbol": symbol, "best_strategy": strategy,
            "timeframe": "1h", "verdict": verdict, "oos_net_usd": net}


def test_locked_strategy_becomes_a_shadow_lane():
    m = generate_shadow_manifest([_pair("bybit", "XRP/USDT:USDT", "trend_continuation_v1")])
    assert len(m["lanes"]) == 1
    lane = m["lanes"][0]
    assert lane["strategy_id"] == "trend_continuation_v1"
    assert lane["mode"] == "shadow"
    assert lane["strategy_params"] == RUNTIME_LOCKED_PARAMS["trend_continuation_v1"]
    assert m["policy"] == {"can_trade": False, "can_promote": False,
                           "requires_untouched_judgment": True, "shadow_only": True}


def test_unlocked_strategy_is_blocked_not_run():
    m = generate_shadow_manifest([_pair("binanceusdm", "SOL/USDT:USDT", "some_unvetted_v9")])
    assert m["lanes"] == []                       # no locked params -> not runnable
    assert len(m["blocked"]) == 1
    assert m["blocked"][0]["strategy_id"] == "some_unvetted_v9"
    assert "locked" in m["blocked"][0]["reason"]


def test_filtered_replay_candidates_become_shadow_trials_not_runtime_lanes():
    payload = {
        "rows": [
            {
                "candidate_id": "orderflow_footprint|binanceusdm|SOL/USDT:USDT|20260710|1|buy",
                "source": "orderflow_footprint",
                "family": "orderflow_footprint_v1",
                "exchange": "binanceusdm",
                "symbol": "SOL/USDT:USDT",
                "day": "20260710",
                "side": "buy",
                "trigger_ts": "2026-07-10T00:00:00+00:00",
                "verdict": "REPLAY_CANDIDATE",
                "quotes": 3,
                "fills": 2,
                "net_usd": 1.25,
                "avg_net_bps": 6.4,
                "profit_factor": 2.2,
                "filter_name": "require_pre_event_trade_through_proxy",
                "condition_bucket": "TOUCH_ONLY_QUEUE_RISK",
            },
            {
                "candidate_id": "loser",
                "exchange": "binanceusdm",
                "symbol": "BTC/USDT:USDT",
                "verdict": "NEGATIVE_EDGE_AFTER_REPLAY",
            },
        ]
    }

    manifest = generate_shadow_manifest([], filtered_replay_payload=payload)

    assert manifest["lanes"] == []
    assert len(manifest["shadow_trials"]) == 1
    trial = manifest["shadow_trials"][0]
    assert trial["status"] == "REPLAY_POSITIVE_NEEDS_SHADOW_ADAPTER"
    assert trial["runtime_strategy_id"] is None
    assert trial["policy"]["can_trade"] is False
    assert trial["policy"]["can_promote"] is False
    assert trial["policy"]["requires_runtime_shadow_adapter"] is True
    assert trial["replay"]["verdict"] == "REPLAY_CANDIDATE"


def test_manifest_dedupes_and_caps():
    pairs = [_pair("binanceusdm", "BTC/USDT:USDT", "funding_mean_reversion_v1")] * 3
    m = generate_shadow_manifest(pairs, max_lanes=5)
    assert len(m["lanes"]) == 1                    # deduped by lane_id


def test_latest_rejected_judgment_blocks_shadow_lane():
    judgments = [{
        "kind": "judgment",
        "exchange": "bybit",
        "symbol": "XRP/USDT:USDT",
        "strategy_id": "trend_continuation_v1",
        "verdict": "REJECT",
        "window_start": "2024-07-10",
        "window_end": "2025-07-10",
    }]
    m = generate_shadow_manifest(
        [_pair("bybit", "XRP/USDT:USDT", "trend_continuation_v1")],
        judgment_records=judgments,
    )
    assert m["lanes"] == []
    assert m["blocked"][0]["strategy_id"] == "trend_continuation_v1"
    assert "judgment rejected" in m["blocked"][0]["reason"]
    assert m["blocked"][0]["latest_judgment"]["verdict"] == "REJECT"


def test_latest_pass_after_prior_reject_allows_shadow_lane():
    judgments = [
        {
            "kind": "judgment",
            "exchange": "binanceusdm",
            "symbol": "BTC/USDT:USDT",
            "strategy_id": "funding_mean_reversion_v1",
            "verdict": "REJECT",
            "window_start": "2025-07-02",
            "window_end": "2026-07-02",
        },
        {
            "kind": "judgment",
            "exchange": "binanceusdm",
            "symbol": "BTC/USDT:USDT",
            "strategy_id": "funding_mean_reversion_v1",
            "verdict": "PASS",
            "window_start": "2024-07-03",
            "window_end": "2025-07-03",
        },
    ]
    m = generate_shadow_manifest(
        [_pair("binanceusdm", "BTC/USDT:USDT", "funding_mean_reversion_v1")],
        judgment_records=judgments,
    )
    assert len(m["lanes"]) == 1
    assert m["lanes"][0]["latest_judgment"]["verdict"] == "PASS"


def test_write_and_load_roundtrip(tmp_path):
    m = generate_shadow_manifest([_pair("bybit", "XRP/USDT:USDT", "trend_continuation_v1")])
    write_shadow_manifest(m, tmp_path)
    assert not list(tmp_path.glob("*.tmp"))        # atomic, no leftover
    loaded = load_shadow_manifest(tmp_path)
    assert loaded["lanes"][0]["lane_id"] == m["lanes"][0]["lane_id"]


def test_consumption_is_on_by_default_for_shadow_only_locked_lanes(tmp_path):
    write_shadow_manifest(
        generate_shadow_manifest([_pair("bybit", "XRP/USDT:USDT", "trend_continuation_v1")]),
        tmp_path)
    env = {"MULTI_LANE_RESEARCH_DIR": str(tmp_path)}   # MANIFEST_ENABLED unset -> on
    specs = manifest_shadow_lanes(env)
    assert len(specs) == 1
    assert specs[0].mode is RunnerMode.SHADOW
    assert specs[0].exchange == "bybit"
    assert specs[0].symbol == "XRP/USDT:USDT"


def test_consumption_can_be_disabled(tmp_path):
    write_shadow_manifest(
        generate_shadow_manifest([_pair("bybit", "XRP/USDT:USDT", "trend_continuation_v1")]),
        tmp_path)
    env = {"MULTI_LANE_RESEARCH_DIR": str(tmp_path), "MULTI_LANE_MANIFEST_ENABLED": "0"}
    assert manifest_shadow_lanes(env) == []


def test_consumption_when_enabled_yields_shadow_lanespecs(tmp_path):
    write_shadow_manifest(
        generate_shadow_manifest([_pair("bybit", "XRP/USDT:USDT", "funding_mean_reversion_v1")]),
        tmp_path)
    env = {"MULTI_LANE_MANIFEST_ENABLED": "1", "MULTI_LANE_RESEARCH_DIR": str(tmp_path)}
    specs = manifest_shadow_lanes(env)
    assert len(specs) == 1
    assert specs[0].mode is RunnerMode.SHADOW
    assert specs[0].strategy_id == "funding_mean_reversion_v1"


def test_curated_and_manifest_lanes_do_not_duplicate(tmp_path):
    # a manifest lane matching a curated lane_id is not added twice
    manifest = {"lanes": [{"lane_id": "trend_continuation_xrp_bybit_shadow",
                           "exchange": "bybit", "symbol": "XRP/USDT:USDT",
                           "strategy_id": "trend_continuation_v1", "mode": "shadow"}]}
    (tmp_path / "shadow_lanes.json").write_text(json.dumps(manifest))
    env = {"MULTI_LANE_MANIFEST_ENABLED": "1", "MULTI_LANE_RESEARCH_DIR": str(tmp_path)}
    ids = [s.lane_id for s in candidate_shadow_lanes(env)]
    assert ids.count("trend_continuation_xrp_bybit_shadow") == 1


def test_curated_and_manifest_semantic_twins_do_not_duplicate(tmp_path):
    # same exchange/symbol/timeframe/strategy/mode as curated XRP, but the
    # research manifest names it with its generated lane id.
    manifest = {"lanes": [{"lane_id": "trend_continuation_v1_bybit_xrpusdt_shadow",
                           "exchange": "bybit", "symbol": "XRP/USDT:USDT",
                           "strategy_id": "trend_continuation_v1", "mode": "shadow"}]}
    (tmp_path / "shadow_lanes.json").write_text(json.dumps(manifest))
    env = {"MULTI_LANE_RESEARCH_DIR": str(tmp_path)}
    lanes = candidate_shadow_lanes(env)
    twins = [
        s for s in lanes
        if s.exchange == "bybit"
        and s.symbol == "XRP/USDT:USDT"
        and s.strategy_id == "trend_continuation_v1"
        and s.mode is RunnerMode.SHADOW
    ]
    assert len(twins) == 1


def test_dedupe_lane_specs_preserves_first_and_blocks_duplicate_id_or_identity():
    base = LaneSpec(
        "a", "bybit", "XRP/USDT:USDT", strategy_id="trend_continuation_v1"
    )
    duplicate_id = LaneSpec("a", "binanceusdm", "BTC/USDT:USDT")
    semantic_twin = LaneSpec(
        "b", "bybit", "XRP/USDT:USDT", strategy_id="trend_continuation_v1"
    )
    distinct = LaneSpec(
        "c", "bybit", "SOL/USDT:USDT", strategy_id="trend_continuation_v1"
    )

    assert dedupe_lane_specs([base, duplicate_id, semantic_twin, distinct]) == [
        base,
        distinct,
    ]
