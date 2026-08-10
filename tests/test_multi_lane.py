"""Multi-lane shadow — provider fan-in, primary flat snapshot, comparison array."""

import json

from vnedge.runtime import multi_lane
from vnedge.runtime.multi_lane import LaneSpec, MultiLaneProvider, MultiLaneShadowRunner
from vnedge.runtime.multi_lane_shadow import (
    build_lane_specs_from_env,
    crypto_trend_doge_shadow_lanes,
    desired_lane_specs,
    fee_wall_paper_probe_lanes,
    lane_specs_fingerprint,
    paper_observation_lanes,
)
from vnedge.runtime.runner_config import RunnerMode
from vnedge.strategy.luxara_live_plan_qtm import LuxaraLivePlanQTMScanner
from vnedge.strategy.luxy_ut_bot_forecast import LuxyUTBotForecastScanner
from vnedge.strategy.stealth_trail_bbp import STEALTH_TRAIL_BBP_ID, StealthTrailBBPScanner


def snap(equity, fills=0, realized=0.0, symbol="BTC/USDT:USDT",
         strategy_id="funding_mean_reversion_v1"):
    return {
        "mode": "paper (live data)", "symbol": symbol, "equity": equity,
        "strategy_id": strategy_id,
        "realized_pnl": realized, "unrealized_pnl": 0.0, "fills": fills,
        "fees_usd": 0.5 * fills, "risk_status": "ok",
        "feed_health": {"candles": "ok"}, "positions": [],
    }


def test_empty_provider_returns_none():
    assert MultiLaneProvider("a").latest() is None


def test_primary_lane_is_flat_top_level():
    p = MultiLaneProvider("binance")
    p.sink("bybit", "bybit").publish(snap(510.0))
    p.sink("binance", "binanceusdm").publish(snap(505.0))
    out = p.latest()
    # top-level flat snapshot = the PRIMARY (binance) lane, not the first published
    assert out["equity"] == 505.0
    assert out["lane_id"] == "binance"


def test_lanes_comparison_array():
    p = MultiLaneProvider("binance")
    p.sink("binance", "binanceusdm").publish(snap(505.0, fills=2, realized=5.0))
    p.sink("bybit", "bybit").publish(snap(498.0, fills=3, realized=-2.0))
    out = p.latest()
    lanes = out["lanes"]
    assert len(lanes) == 2
    by_ex = {lane["exchange"]: lane for lane in lanes}
    assert by_ex["binanceusdm"]["equity"] == 505.0 and by_ex["binanceusdm"]["fills"] == 2
    assert by_ex["bybit"]["realized_pnl"] == -2.0
    # dashboard lane matrix labels mode + strategy per lane
    assert by_ex["binanceusdm"]["mode"] == "paper (live data)"
    assert by_ex["binanceusdm"]["strategy_id"] == "funding_mean_reversion_v1"
    for lane in lanes:
        for f in ("lane_id", "exchange", "symbol", "mode", "strategy_id",
                  "equity", "realized_pnl",
                  "fills", "fees_usd", "risk_status", "feed"):
            assert f in lane


def test_lane_summary_carries_feed_and_eval_observability():
    p = MultiLaneProvider("binance")
    s = snap(505.0)
    s["feed_health"] = {
        "candles": "ok",
        "exchange": "binanceusdm (live ws)",
        "last_update_ms": 1234.0,
    }
    s["session"] = {
        "evals": 8,
        "live_evals": 5,
        "backfill_evals": 3,
        "live_signals": 2,
        "backfill_signals": 1,
        "last_eval": {
            "fired": False,
            "features": {"funding_pct": 0.62, "close_z": -0.4},
            "thresholds": {"extreme_pct": 0.85, "z_entry": 1.5},
        },
        "why_no_trade": "last_eval_no_signal",
    }
    p.sink("binance", "binanceusdm").publish(s)
    lane = p.latest()["lanes"][0]
    assert lane["feed_mode"] == "binanceusdm (live ws)"
    assert lane["staleness_ms"] == 1234.0
    assert lane["last_eval"]["features"]["funding_pct"] == 0.62
    assert lane["last_eval"]["thresholds"]["z_entry"] == 1.5
    assert lane["why_no_trade"] == "last_eval_no_signal"
    assert lane["funnel"]["live_evals"] == 5
    assert lane["funnel"]["backfill_evals"] == 3
    assert lane["funnel"]["live_signals"] == 2
    assert lane["funnel"]["backfill_signals"] == 1
    assert lane["trade_compatibility"]["gateway_required"] is True


def test_lane_summary_carries_daily_factory_state():
    p = MultiLaneProvider("binance")
    s = snap(505.0)
    s["session"] = {
        "daily_factory": {
            "enabled": True,
            "entries_today": 1,
            "max_entries_per_day": 3,
            "entry_block_reason": None,
            "force_flatten_due": False,
        }
    }
    p.sink("binance", "binanceusdm").publish(s)

    lane = p.latest()["lanes"][0]

    assert lane["daily_factory"]["enabled"] is True
    assert lane["daily_factory"]["entries_today"] == 1


def test_lane_summary_degrades_without_feed_or_eval():
    p = MultiLaneProvider("binance")
    p.sink("binance", "binanceusdm").publish(snap(505.0))
    lane = p.latest()["lanes"][0]
    assert lane["feed_mode"] == ""
    assert lane["staleness_ms"] is None
    assert lane["last_eval"] is None


def test_lane_order_is_publish_order():
    p = MultiLaneProvider("binance")
    p.sink("bybit", "bybit").publish(snap(1.0))
    p.sink("binance", "binanceusdm").publish(snap(2.0))
    assert [lane["exchange"] for lane in p.latest()["lanes"]] == ["bybit", "binanceusdm"]


def test_updates_replace_not_append():
    p = MultiLaneProvider("binance")
    sink = p.sink("binance", "binanceusdm")
    sink.publish(snap(500.0))
    sink.publish(snap(507.0))  # same lane updates
    assert len(p.latest()["lanes"]) == 1
    assert p.latest()["equity"] == 507.0


def test_falls_back_to_first_lane_when_primary_absent():
    p = MultiLaneProvider("nonexistent_primary")
    p.sink("bybit", "bybit").publish(snap(499.0))
    out = p.latest()
    assert out["lane_id"] == "bybit"  # primary missing -> first published lane


def test_lane_spec_defaults():
    spec = LaneSpec(lane_id="x", exchange="bybit", symbol="BTC/USDT:USDT")
    assert spec.starting_equity == 500.0
    assert spec.daily_loss_usd == 10.0
    assert spec.is_primary is False
    assert spec.mode is RunnerMode.SHADOW


def test_daily_factory_env_builds_global_policy():
    spec = LaneSpec(lane_id="x", exchange="binanceusdm", symbol="BTC/USDT:USDT")

    cfg = multi_lane._lane_daily_factory_config(spec, {
        "MULTI_LANE_DAILY_FACTORY_ENABLED": "1",
        "MULTI_LANE_DAILY_FACTORY_TIMEZONE": "Asia/Kolkata",
        "MULTI_LANE_DAILY_FACTORY_ENTRY_CUTOFF_MINUTE": "1320",
        "MULTI_LANE_DAILY_FACTORY_FORCE_FLATTEN_MINUTE": "1430",
        "MULTI_LANE_DAILY_FACTORY_MAX_ENTRIES_PER_DAY": "2",
        "MULTI_LANE_DAILY_FACTORY_DAILY_PROFIT_TARGET_USD": "8.5",
    })

    assert cfg.enabled is True
    assert cfg.session_timezone == "Asia/Kolkata"
    assert cfg.entry_cutoff_minute == 1320
    assert cfg.force_flatten_minute == 1430
    assert cfg.max_entries_per_day == 2
    assert cfg.daily_profit_target_usd == 8.5


def test_daily_factory_env_strategy_override_wins():
    spec = LaneSpec(
        lane_id="x",
        exchange="binanceusdm",
        symbol="BTC/USDT:USDT",
        strategy_id="daily_scalper_pack_v1",
    )

    cfg = multi_lane._lane_daily_factory_config(spec, {
        "MULTI_LANE_DAILY_FACTORY_ENABLED": "0",
        "MULTI_LANE_DAILY_FACTORY_DAILY_SCALPER_PACK_V1_ENABLED": "1",
        "MULTI_LANE_DAILY_FACTORY_ENTRY_CUTOFF_MINUTE": "1320",
        "MULTI_LANE_DAILY_FACTORY_DAILY_SCALPER_PACK_V1_ENTRY_CUTOFF_MINUTE": "1200",
        "MULTI_LANE_DAILY_FACTORY_FORCE_FLATTEN_MINUTE": "1430",
    })

    assert cfg.enabled is True
    assert cfg.entry_cutoff_minute == 1200
    assert cfg.force_flatten_minute == 1430


def test_multi_lane_builds_stealth_trail_bbp_strategy():
    strategy = multi_lane._build_single_strategy(STEALTH_TRAIL_BBP_ID, {}, None, None)

    assert isinstance(strategy, StealthTrailBBPScanner)


def test_multi_lane_builds_lux_scanner_strategies():
    luxy = multi_lane._build_single_strategy(
        "luxy_ut_bot_forecast_v1", {}, None, None
    )
    luxara = multi_lane._build_single_strategy(
        "luxara_live_plan_qtm_v1", {}, None, None
    )

    assert isinstance(luxy, LuxyUTBotForecastScanner)
    assert isinstance(luxara, LuxaraLivePlanQTMScanner)


def test_multi_lane_builds_crypto_trend_atr_margin_strategy():
    from vnedge.strategy.crypto_trend_atr_margin import CryptoTrendAtrMargin

    strategy = multi_lane._build_single_strategy(
        "crypto_trend_atr_margin_v1",
        {"take_profit_r": None},
        None,
        None,
    )

    assert isinstance(strategy, CryptoTrendAtrMargin)


def test_publish_error_adds_faulted_lane():
    p = MultiLaneProvider("binance")
    p.publish_error("bybit", "bybit", "BTC/USDT:USDT", "build failed")
    out = p.latest()
    assert out["risk_status"] == "lane_error"
    assert out["lanes"][0]["feed"] == "error"
    assert out["lanes"][0]["trade_compatibility"]["state"] == "BLOCKED"


def test_runtime_control_metadata_reaches_snapshot():
    p = MultiLaneProvider(
        "binance",
        runtime_control={"lane_set_hash": "abc123", "orders_allowed": False},
    )
    p.sink("binance", "binanceusdm").publish(snap(505.0))
    out = p.latest()
    assert out["runtime_control"]["lane_set_hash"] == "abc123"
    assert out["runtime_control"]["orders_allowed"] is False


def test_negative_shadow_perf_marks_lane_probation():
    p = MultiLaneProvider("lane")
    s = snap(500.0)
    s["mode"] = "shadow (live data)"
    s["session"] = {
        "shadow_perf": {
            "virtual_trades": 2,
            "wins": 0,
            "losses": 2,
            "net_usd": -10.0,
            "profit_factor": 0.0,
            "open_intents": 0,
            "resolutions": {"stop": 2, "target": 0, "timeout": 0},
            "status": "SHADOW_PROBATION",
            "trade_compatible": False,
        }
    }
    p.sink("lane", "bybit").publish(s)
    compat = p.latest()["lanes"][0]["trade_compatibility"]
    assert compat["state"] == "SHADOW_PROBATION"
    assert compat["real_orders_allowed"] is False


def test_lane_specs_fingerprint_changes_when_manifest_set_changes():
    a = [LaneSpec("a", "binanceusdm", "BTC/USDT:USDT")]
    b = [LaneSpec("a", "binanceusdm", "ETH/USDT:USDT")]
    assert lane_specs_fingerprint(a) == lane_specs_fingerprint(a)
    assert lane_specs_fingerprint(a) != lane_specs_fingerprint(b)


def test_lane_specs_expand_from_env():
    # single explicit mode: pure exchange x symbol grid expansion
    specs = build_lane_specs_from_env({
        "MULTI_LANE_EXCHANGES": "binanceusdm,bybit",
        "MULTI_LANE_SYMBOLS": "BTC/USDT:USDT,ETH/USDT:USDT",
        "MULTI_LANE_MODES": "shadow",
        "MULTI_LANE_PRIMARY_EXCHANGE": "bybit",
        "MULTI_LANE_PRIMARY_SYMBOL": "ETH/USDT:USDT",
    })
    assert len(specs) == 4
    primary = [spec for spec in specs if spec.is_primary]
    assert len(primary) == 1
    assert primary[0].exchange == "bybit"
    assert primary[0].symbol == "ETH/USDT:USDT"
    assert all(spec.mode is RunnerMode.SHADOW for spec in specs)


def test_lane_specs_default_runs_both_modes_per_venue():
    # default env: Binance/Bybit governed paper+shadow plus Delta shadow.
    specs = build_lane_specs_from_env({})
    assert len(specs) == 5
    assert {s.mode for s in specs} == {RunnerMode.PAPER, RunnerMode.SHADOW}
    ids = {s.lane_id for s in specs}
    # governed paper trials keep their exact ids (continue their account files)
    assert "funding_mr_btc_v1_20260703" in ids
    assert "funding_mr_bybit_20260704" in ids
    # shadow lanes are distinct, isolated ids
    assert "funding_mr_binanceusdm_btc_usdt_usdt_shadow" in ids
    assert "funding_mr_bybit_btc_usdt_usdt_shadow" in ids
    assert "trend_continuation_delta_india_btc_usd_usd_shadow" in ids
    delta = next(s for s in specs if s.exchange == "delta_india")
    assert delta.symbol == "BTC/USD:USD"
    assert delta.mode is RunnerMode.SHADOW
    assert delta.strategy_id == "trend_continuation_v1"
    # the flat top-level snapshot is the governed Binance PAPER lane
    primary = [s for s in specs if s.is_primary]
    assert len(primary) == 1
    assert primary[0].lane_id == "funding_mr_btc_v1_20260703"
    assert primary[0].mode is RunnerMode.PAPER


def test_delta_paper_requires_explicit_opt_in():
    specs = build_lane_specs_from_env({
        "MULTI_LANE_EXCHANGES": "delta_india",
        "MULTI_LANE_SYMBOLS": "BTC/USDT:USDT",
        "MULTI_LANE_MODES": "paper,shadow",
    })
    assert len(specs) == 1
    assert specs[0].exchange == "delta_india"
    assert specs[0].symbol == "BTC/USD:USD"
    assert specs[0].mode is RunnerMode.SHADOW
    assert specs[0].strategy_id == "trend_continuation_v1"


def test_delta_paper_opt_in_still_uses_candle_only_strategy():
    specs = build_lane_specs_from_env({
        "MULTI_LANE_EXCHANGES": "delta_india",
        "MULTI_LANE_SYMBOLS": "BTC/USDT:USDT",
        "MULTI_LANE_MODES": "paper",
        "MULTI_LANE_DELTA_PAPER": "1",
    })
    assert len(specs) == 1
    assert specs[0].symbol == "BTC/USD:USD"
    assert specs[0].mode is RunnerMode.PAPER
    assert specs[0].strategy_id == "trend_continuation_v1"


def test_crypto_trend_doge_shadow_lane_enabled_by_default():
    lanes = crypto_trend_doge_shadow_lanes({})
    # Shadow twin + the human-approved paper lane (prereg PASS 2026-08-03).
    assert len(lanes) == 2
    lane = lanes[0]
    assert lane.lane_id == "crypto_trend_doge_binanceusdm_shadow"
    assert lane.exchange == "binanceusdm"
    assert lane.symbol == "DOGE/USDT:USDT"
    assert lane.timeframe == "1h"
    assert lane.strategy_id == "crypto_trend_atr_margin_v1"
    assert lane.mode is RunnerMode.SHADOW
    assert lane.strategy_params["take_profit_r"] is None
    # P0 parity (2026-08-03): the shadow twin now runs the SAME trail 3x as the
    # paper lane, so shadow predicts paper/live instead of the legacy fixed stop.
    assert lane.trail_atr_mult == 3.0


def test_crypto_trend_doge_paper_lane_runs_judged_exit():
    # The paper lane must run the EXACT exit its promotion evidence was measured
    # on: active-exit + ATR chandelier trail 3x (trail_atr_mult=3.0).
    paper = [
        l for l in crypto_trend_doge_shadow_lanes({})
        if l.mode is RunnerMode.PAPER
    ]
    assert len(paper) == 1
    lane = paper[0]
    assert lane.lane_id == "crypto_trend_doge_binanceusdm_paper"
    assert lane.strategy_id == "crypto_trend_atr_margin_v1"
    assert lane.trail_atr_mult == 3.0


def test_crypto_trend_doge_paper_lane_can_be_disabled():
    lanes = crypto_trend_doge_shadow_lanes({"MULTI_LANE_CRYPTO_TREND_DOGE_PAPER": "0"})
    assert [l.mode for l in lanes] == [RunnerMode.SHADOW]


def test_crypto_trend_doge_shadow_lane_can_be_disabled():
    assert crypto_trend_doge_shadow_lanes({"MULTI_LANE_CRYPTO_TREND_DOGE": "0"}) == []


def test_paper_observation_lanes_disabled_by_default():
    # helper returns nothing with the flag unset...
    specs = build_lane_specs_from_env({})
    assert paper_observation_lanes(specs, {}) == []


def test_paper_observation_flag_off_is_a_no_op_on_desired_set():
    # ...and the full desired set is byte-identical whether the flag is unset
    # or explicitly "0" — flag-off adds ZERO lanes (safety req #1).
    base = desired_lane_specs({})
    off = desired_lane_specs({"MULTI_LANE_PAPER_OBSERVE_ALL": "0"})
    ids_base = [s.lane_id for s in base]
    assert ids_base == [s.lane_id for s in off]
    assert not any(s.lane_id.endswith("_paper_observation") for s in base)
    assert lane_specs_fingerprint(base) == lane_specs_fingerprint(off)


def test_governor_paper_roster_filter_keeps_only_proposed_paper_lanes(tmp_path):
    governor = {
        "proposed_roster": {
            "paper_lanes": [
                {
                    "lane_id": "funding_mr_btc_v1_20260703",
                    "strategy_id": "funding_mean_reversion_v1",
                    "exchange": "binanceusdm",
                    "symbol": "BTC/USDT:USDT",
                    "timeframe": "1h",
                }
            ]
        }
    }
    path = tmp_path / "paper_lane_governor_latest.json"
    path.write_text(json.dumps(governor))

    specs = desired_lane_specs({
        "MULTI_LANE_GOVERNOR_PAPER_ROSTER_ONLY": "1",
        "MULTI_LANE_PAPER_GOVERNOR_PATH": str(path),
    })

    paper_ids = [spec.lane_id for spec in specs if spec.mode is RunnerMode.PAPER]
    assert paper_ids == ["funding_mr_btc_v1_20260703"]
    assert next(spec for spec in specs if spec.is_primary).lane_id == "funding_mr_btc_v1_20260703"
    assert any(spec.mode is RunnerMode.SHADOW for spec in specs)


def test_governor_paper_roster_filter_can_match_by_signature_and_reassign_primary(tmp_path):
    governor = {
        "proposed_roster": {
            "paper_lanes": [
                {
                    "lane_id": "human_readable_alias",
                    "strategy_id": "funding_mean_reversion_v1",
                    "exchange": "bybit",
                    "symbol": "BTC/USDT",
                    "timeframe": "1h",
                }
            ]
        }
    }
    path = tmp_path / "paper_lane_governor_latest.json"
    path.write_text(json.dumps(governor))

    specs = desired_lane_specs({
        "MULTI_LANE_GOVERNOR_PAPER_ROSTER_ONLY": "1",
        "MULTI_LANE_PAPER_GOVERNOR_PATH": str(path),
    })

    paper_ids = [spec.lane_id for spec in specs if spec.mode is RunnerMode.PAPER]
    assert paper_ids == ["funding_mr_bybit_20260704"]
    assert next(spec for spec in specs if spec.is_primary).lane_id == "funding_mr_bybit_20260704"


def test_governor_paper_roster_filter_fails_open_when_report_is_missing(tmp_path):
    base = desired_lane_specs({})
    filtered = desired_lane_specs({
        "MULTI_LANE_GOVERNOR_PAPER_ROSTER_ONLY": "1",
        "MULTI_LANE_PAPER_GOVERNOR_PATH": str(tmp_path / "missing.json"),
    })

    assert [spec.lane_id for spec in filtered] == [spec.lane_id for spec in base]


def test_paper_observation_mirrors_shadow_only_lanes_without_duplicate_trials():
    # PRUNE_DEAD off: this exercises the MIRRORING logic against the full roster,
    # independent of which strategies the evidence-prune removes.
    specs = desired_lane_specs({"MULTI_LANE_PAPER_OBSERVE_ALL": "1", "MULTI_LANE_PRUNE_DEAD": "0"})
    ids = {spec.lane_id for spec in specs}

    # Governed BTC/Bybit paper-trial ledgers stay canonical and are not mirrored.
    assert "funding_mr_btc_v1_20260703" in ids
    assert "funding_mr_bybit_20260704" in ids
    assert "funding_mr_binanceusdm_btc_usdt_usdt_paper_observation" not in ids
    assert "funding_mr_bybit_btc_usdt_usdt_paper_observation" not in ids

    # Shadow-only lanes (no equivalent paper trial) get isolated paper ledgers.
    assert "trend_continuation_delta_india_btc_usd_usd_paper_observation" in ids
    assert "funding_mr_delta_india_btc_usd_usd_paper_observation" in ids
    assert "trend_continuation_xrp_bybit_paper_observation" in ids

    observed = [
        spec for spec in specs if spec.lane_id.endswith("_paper_observation")
    ]
    assert observed
    assert all(spec.mode is RunnerMode.PAPER for spec in observed)
    assert all(not spec.is_primary for spec in observed)
    # isolated ledgers: every observation id is unique across the runtime set
    assert len(ids) == len(specs)


def test_delta_paper_observation_can_be_disabled_without_blocking_other_mirrors():
    specs = desired_lane_specs({
        "MULTI_LANE_PAPER_OBSERVE_ALL": "1",
        "MULTI_LANE_DELTA_PAPER_OBSERVE": "0",
        # mirroring logic under test — independent of the evidence-prune:
        "MULTI_LANE_PRUNE_DEAD": "0",
        # Isolate the observation-mirror behavior under test from the deliberate
        # native Delta PAPER trial lane (its own MULTI_LANE_EVIDENCE_PAPER_TRIAL
        # flag), which is not a mirror.
        "MULTI_LANE_EVIDENCE_PAPER_TRIAL": "0",
    })
    ids = {spec.lane_id for spec in specs}

    assert "trend_continuation_xrp_bybit_paper_observation" in ids
    assert not any(
        spec.exchange == "delta_india" and spec.mode is RunnerMode.PAPER
        for spec in specs
    )


def test_fee_wall_strict_candidates_become_isolated_paper_probes(tmp_path):
    report = {
        "generated_at": "2999-01-01T00:00:00+00:00",
        "strict_fee_wall_candidates": [
            {
                "exchange": "binanceusdm",
                "symbol": "BTC/USDT:USDT",
                "timeframe": "15m",
                "strategy": "luxy_ut_bot_forecast_v1",
                "verdict": "MIXED_ROUTE_EDGE",
                "recommended_action": "PRE_REGISTER_UNTOUCHED_JUDGMENT_WINDOW",
                "routed": 16,
                "avg_selected_net_bps": 13.5,
                "profit_factor": 1.72,
            },
            {
                "exchange": "delta_india",
                "symbol": "SOL/USD:USD",
                "timeframe": "15m",
                "strategy": "luxy_ut_bot_forecast_v1",
                "verdict": "MIXED_ROUTE_EDGE",
                "recommended_action": "PRE_REGISTER_UNTOUCHED_JUDGMENT_WINDOW",
                "routed": 23,
                "avg_selected_net_bps": 12.2,
                "profit_factor": 1.43,
            },
            {
                "exchange": "bybit",
                "symbol": "SOL/USDT:USDT",
                "timeframe": "15m",
                "strategy": "stealth_trail_bbp_v1",
                "verdict": "MAKER_EDGE",
                "recommended_action": "PRE_REGISTER_UNTOUCHED_JUDGMENT_WINDOW",
                "routed": 72,
                "avg_selected_net_bps": 8.4,
                "profit_factor": 1.22,
            },
            {
                "exchange": "delta_india",
                "symbol": "ETH/USD:USD",
                "timeframe": "5m",
                "strategy": "quantified_fee_wall_sniper_v1",
                "verdict": "TAKER_EDGE",
                "recommended_action": "PRE_REGISTER_UNTOUCHED_JUDGMENT_WINDOW",
                "routed": 22,
                "avg_selected_net_bps": 18.6,
                "profit_factor": 1.64,
            },
            {
                "exchange": "binanceusdm",
                "symbol": "ETH/USDT:USDT",
                "timeframe": "15m",
                "strategy": "luxy_ut_bot_forecast_v1",
                "verdict": "UNDER_SAMPLED",
                "recommended_action": "EXPAND_SAMPLE_OR_LOWER_TIMEFRAME_TRIGGER",
                "routed": 2,
                "avg_selected_net_bps": 50.0,
                "profit_factor": 999.0,
            },
        ],
    }
    path = tmp_path / "fee_wall_forensics_latest.json"
    path.write_text(json.dumps(report))

    probes = fee_wall_paper_probe_lanes({
        "MULTI_LANE_FEE_WALL_PAPER_PROBES": "1",
        "MULTI_LANE_FEE_WALL_FORENSICS_PATH": str(path),
    })

    ids = {probe.lane_id for probe in probes}
    assert ids == {
        "fee_wall_luxy_ut_bot_forecast_binanceusdm_btc_usdt_usdt_15m_paper_probe",
        "fee_wall_luxy_ut_bot_forecast_delta_india_sol_usd_usd_15m_paper_probe",
        "fee_wall_stealth_trail_bbp_bybit_sol_usdt_usdt_15m_paper_probe",
        "fee_wall_quantified_fee_wall_sniper_delta_india_eth_usd_usd_5m_paper_probe",
    }
    assert all(probe.mode is RunnerMode.PAPER for probe in probes)
    assert all(not probe.is_primary for probe in probes)


def test_fee_wall_paper_probes_are_disabled_and_freshness_guarded(tmp_path):
    stale = {
        "generated_at": "2000-01-01T00:00:00+00:00",
        "strict_fee_wall_candidates": [
            {
                "exchange": "bybit",
                "symbol": "BTC/USDT:USDT",
                "timeframe": "15m",
                "strategy": "luxy_ut_bot_forecast_v1",
                "verdict": "MIXED_ROUTE_EDGE",
                "recommended_action": "PRE_REGISTER_UNTOUCHED_JUDGMENT_WINDOW",
                "routed": 20,
                "avg_selected_net_bps": 20.0,
                "profit_factor": 2.0,
            },
        ],
    }
    path = tmp_path / "fee_wall_forensics_latest.json"
    path.write_text(json.dumps(stale))

    assert fee_wall_paper_probe_lanes({
        "MULTI_LANE_FEE_WALL_FORENSICS_PATH": str(path),
    }) == []
    assert fee_wall_paper_probe_lanes({
        "MULTI_LANE_FEE_WALL_PAPER_PROBES": "1",
        "MULTI_LANE_FEE_WALL_FORENSICS_PATH": str(path),
    }) == []


def test_fee_wall_probe_manifest_is_durable_after_source_artifact_ages(tmp_path):
    manifest = {
        "approved_by": "human",
        "approval": "promote to live-data paper probes",
        "generated_at": "2000-01-01T00:00:00+00:00",
        "paper_probes": [
            {
                "exchange": "binanceusdm",
                "symbol": "SOL/USDT:USDT",
                "timeframe": "1h",
                "strategy": "luxara_live_plan_qtm_v1",
                "verdict": "MAKER_EDGE",
                "recommended_action": "PRE_REGISTER_UNTOUCHED_JUDGMENT_WINDOW",
                "routed": 10,
                "avg_selected_net_bps": 15.2,
                "profit_factor": 1.34,
            },
        ],
    }
    path = tmp_path / "fee_wall_paper_probes.json"
    path.write_text(json.dumps(manifest))

    probes = fee_wall_paper_probe_lanes({
        "MULTI_LANE_FEE_WALL_PAPER_PROBES": "1",
        "MULTI_LANE_FEE_WALL_PAPER_PROBES_PATH": str(path),
    })

    assert [probe.lane_id for probe in probes] == [
        "fee_wall_luxara_live_plan_qtm_binanceusdm_sol_usdt_usdt_1h_paper_probe"
    ]


def test_lane_specs_reject_unknown_mode():
    import pytest
    with pytest.raises(ValueError, match="unknown multi-lane mode"):
        build_lane_specs_from_env({"MULTI_LANE_MODES": "paper,bogus"})


async def test_runner_continues_when_one_lane_build_fails(monkeypatch, tmp_path):
    events = []

    class FakeFeed:
        def __init__(self, lane_id):
            self.lane_id = lane_id

        async def start(self):
            events.append(("start", self.lane_id))

        async def stop(self):
            events.append(("stop", self.lane_id))

    class FakeSession:
        def __init__(self, lane_id):
            self.lane_id = lane_id

        async def run(self, *, deadline_seconds=None):
            events.append(("run", self.lane_id, deadline_seconds))

    async def fake_build_lane(spec, provider, journal_dir):
        if spec.exchange == "bad":
            raise RuntimeError("boom")
        return multi_lane._LaneRuntime(
            spec=spec,
            session=FakeSession(spec.lane_id),
            feed=FakeFeed(spec.lane_id),
        )

    monkeypatch.setattr(multi_lane, "build_lane", fake_build_lane)
    provider = MultiLaneProvider("good")
    runner = MultiLaneShadowRunner(
        [
            LaneSpec("bad", "bad", "BTC/USDT:USDT"),
            LaneSpec("good", "bybit", "BTC/USDT:USDT"),
        ],
        tmp_path,
        provider,
    )

    await runner.run(deadline_seconds=0.01)

    assert ("start", "good") in events
    assert ("run", "good", 0.01) in events
    assert ("stop", "good") in events
    latest = provider.latest()
    assert latest is not None
    faulted = [lane for lane in latest["lanes"] if lane["lane_id"] == "bad"]
    assert faulted and faulted[0]["risk_status"] == "lane_error"


async def test_build_lane_refuses_wrong_symbol_account(monkeypatch, tmp_path):
    """build_lane passes the spec's symbol/equity expectations to restore_into."""
    import json

    import pytest

    class FakeFeed:
        quote = (100.0, 100.5)
        funding_rate = 0.0001

    monkeypatch.setattr(
        multi_lane, "acquire_market_feed",
        lambda exchange_id, *, symbol, timeframe="1m": FakeFeed(),
    )

    class FakeRest:  # skip the network warmup
        def __init__(self, exchange):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def fetch_candles(self, *a, **k):
            return []

        async def fetch_funding_history(self, *a, **k):
            return []

    monkeypatch.setattr(multi_lane, "CcxtPublicClient", FakeRest)

    # a moved/edited store holding a position in a DIFFERENT symbol
    (tmp_path / "x.account.json").write_text(json.dumps({
        "trial_id": "x", "saved_at": "2026-07-08T00:00:00+00:00",
        "starting_equity": 500.0, "balance_usd": 500.0,
        "positions": [
            {"symbol": "ETH/USDT:USDT", "quantity": 1.0, "entry_price": 100.0}
        ],
        "tracker": {}, "plan": None,
    }))
    spec = LaneSpec(lane_id="x", exchange="bybit", symbol="BTC/USDT:USDT",
                    timeframe="1h", strategy_id="trend_continuation_v1")
    provider = MultiLaneProvider("x")

    with pytest.raises(ValueError, match="wrong-symbol"):
        await multi_lane.build_lane(spec, provider, journal_dir=tmp_path)


def test_build_strategy_selects_trend_continuation():
    import pandas as pd

    from vnedge.runtime.multi_lane import _build_strategy
    from vnedge.strategy.trend_continuation import TrendContinuation

    spec = LaneSpec(lane_id="x", exchange="bybit", symbol="XRP/USDT:USDT",
                    strategy_id="trend_continuation_v1", strategy_params={})
    strat = _build_strategy(
        spec, pd.DataFrame(columns=["timestamp", "funding_rate"]), feed=None)
    assert isinstance(strat, TrendContinuation)
    assert strat.strategy_id == "trend_continuation_v1"


def test_build_strategy_selects_quant_signal_pack():
    import pandas as pd

    from vnedge.runtime.multi_lane import _build_strategy
    from vnedge.strategy.quant_signal_pack import QuantSignalPack

    spec = LaneSpec(lane_id="x", exchange="bybit", symbol="SOL/USDT:USDT",
                    strategy_id="quant_signal_pack_v1", strategy_params={})
    strat = _build_strategy(
        spec, pd.DataFrame(columns=["timestamp", "funding_rate"]), feed=None)
    assert isinstance(strat, QuantSignalPack)
    assert strat.strategy_id == "quant_signal_pack_v1"


def test_build_strategy_selects_signal_arbiter_composite():
    import pandas as pd

    from vnedge.runtime.multi_lane import _build_strategy
    from vnedge.strategy.composite import CompositeSignalStrategy

    spec = LaneSpec(
        lane_id="arb",
        exchange="bybit",
        symbol="BTC/USDT:USDT",
        strategy_id="signal_arbiter_v1",
        strategy_params={
            "arbiter": {
                "min_net_edge_bps": 0.0,
                "taker_min_profit_factor": 1.35,
            },
            "strategies": [
                {
                    "strategy_id": "trend_continuation_v1",
                    "expected_edge_bps": 3.0,
                    "expected_cost_bps": 1.0,
                    "profit_factor": 1.2,
                },
                {
                    "strategy_id": "scalper_1m_v1",
                    "source_id": "scalper_fast_lane",
                    "expected_edge_bps": 6.0,
                    "expected_cost_bps": 2.0,
                    "profit_factor": 1.5,
                },
            ],
        },
    )

    strat = _build_strategy(
        spec, pd.DataFrame(columns=["timestamp", "funding_rate"]), feed=None
    )

    assert isinstance(strat, CompositeSignalStrategy)
    assert strat.strategy_id == "signal_arbiter_v1"
    assert [child.strategy_id for child in strat.strategies] == [
        "trend_continuation_v1",
        "scalper_1m_v1",
    ]
    assert strat.candidate_defaults["scalper_1m_v1#2"]["source_id"] == "scalper_fast_lane"


def test_build_strategy_rejects_unknown_id():
    import pandas as pd
    import pytest

    from vnedge.runtime.multi_lane import _build_strategy

    spec = LaneSpec(lane_id="x", exchange="bybit", symbol="XRP/USDT:USDT",
                    strategy_id="not_a_real_strategy_v9")
    with pytest.raises(ValueError, match="unsupported lane strategy_id"):
        _build_strategy(spec, pd.DataFrame(), feed=None)


def test_candidate_shadow_lanes_default_includes_xrp_trend():
    from vnedge.runtime.multi_lane_shadow import candidate_shadow_lanes

    lanes = candidate_shadow_lanes({})
    xrp = next(lane for lane in lanes
               if lane.lane_id == "trend_continuation_xrp_bybit_shadow")
    assert xrp.strategy_id == "trend_continuation_v1"
    assert xrp.symbol == "XRP/USDT:USDT"
    assert xrp.exchange == "bybit"
    assert xrp.mode is RunnerMode.SHADOW      # observe only, never a fill
    assert xrp.is_primary is False            # never the governed flat snapshot


def test_candidate_shadow_lanes_can_be_disabled():
    from vnedge.runtime.multi_lane_shadow import candidate_shadow_lanes

    assert candidate_shadow_lanes({"MULTI_LANE_CANDIDATES": "0"}) == []


# --- Delta native funding backfill wiring -----------------------------------------

def _delta_spec():
    return LaneSpec(lane_id="funding_mr_delta_india_btc_usd_usd_shadow",
                    exchange="delta_india", symbol="BTC/USD:USD",
                    strategy_id="funding_mean_reversion_v1")


def _empty_funding():
    import pandas as pd

    return pd.DataFrame(
        {
            "timestamp": pd.Series(dtype="datetime64[ns, UTC]"),
            "funding_rate": pd.Series(dtype="float64"),
        }
    )


async def test_delta_funding_seed_uses_native_backfill(monkeypatch):
    import pandas as pd

    import vnedge.data.delta_native_history as dnh

    backfill = pd.DataFrame({
        "timestamp": pd.to_datetime([1_000, 2_000], unit="s", utc=True),
        "funding_rate": [0.0001, -0.0002],
    })
    seen = {}

    async def fake_fetch(symbol, days=30, **kwargs):
        seen["symbol"], seen["days"] = symbol, days
        return backfill

    monkeypatch.setattr(dnh, "fetch_delta_funding_history", fake_fetch)
    out = await multi_lane._delta_funding_seed(_delta_spec(), _empty_funding())
    assert out is backfill
    assert seen == {"symbol": "BTC/USD:USD", "days": 30}


async def test_delta_funding_seed_falls_back_on_fetch_failure(monkeypatch):
    # failure posture: today's behaviour (empty seed -> live accumulation
    # behind the warmup mask); the backfill must never crash lane build
    import vnedge.data.delta_native_history as dnh

    async def boom(symbol, days=30, **kwargs):
        raise RuntimeError("api down")

    monkeypatch.setattr(dnh, "fetch_delta_funding_history", boom)
    fallback = _empty_funding()
    out = await multi_lane._delta_funding_seed(_delta_spec(), fallback)
    assert out is fallback


async def test_delta_funding_seed_falls_back_on_empty_backfill(monkeypatch):
    import vnedge.data.delta_native_history as dnh

    async def empty(symbol, days=30, **kwargs):
        return _empty_funding()

    monkeypatch.setattr(dnh, "fetch_delta_funding_history", empty)
    fallback = _empty_funding()
    out = await multi_lane._delta_funding_seed(_delta_spec(), fallback)
    assert out is fallback


async def test_delta_funding_seed_ignores_non_delta_exchanges(monkeypatch):
    import vnedge.data.delta_native_history as dnh

    called = []

    async def fake_fetch(symbol, days=30, **kwargs):
        called.append(symbol)
        return _empty_funding()

    monkeypatch.setattr(dnh, "fetch_delta_funding_history", fake_fetch)
    spec = LaneSpec(lane_id="x", exchange="binanceusdm", symbol="BTC/USDT:USDT",
                    strategy_id="funding_mean_reversion_v1")
    fallback = _empty_funding()
    out = await multi_lane._delta_funding_seed(spec, fallback)
    assert out is fallback
    assert called == []


def test_build_strategy_backfilled_seed_keeps_persistent_accumulator(tmp_path):
    # a NON-empty backfilled seed on a history-less venue must still get the
    # persistent accumulator (store keeps appending live prints on top)
    import pandas as pd

    from vnedge.runtime.funding_accumulator import LivePersistentFundingMR
    from vnedge.runtime.multi_lane import _build_strategy

    seed = pd.DataFrame({
        "timestamp": pd.to_datetime([1_000, 2_000], unit="s", utc=True),
        "funding_rate": [0.0001, -0.0002],
    })

    class _Feed:
        exchange_id = "delta_india"
        funding_rate = 0.0001

    strat = _build_strategy(
        _delta_spec(), seed, feed=_Feed(),
        funding_store_path=tmp_path / "lane.funding.jsonl",
    )
    assert isinstance(strat, LivePersistentFundingMR)
    assert len(strat.funding) == 2  # seeded, not the synthetic 1970 anchor


def test_dead_lane_prune_excludes_proven_dead_keeps_edge():
    """Evidence-based prune removes proven-dead families; keeps the edge set.
    Reversible via MULTI_LANE_PRUNE_DEAD=0."""
    from vnedge.runtime.multi_lane_shadow import _pruned_lane

    def spec(strategy, symbol):
        return LaneSpec(lane_id=f"{strategy}_{symbol}", exchange="bybit",
                        symbol=symbol, strategy_id=strategy, strategy_params={},
                        mode=RunnerMode.SHADOW)

    # pruned
    assert _pruned_lane(spec("alpha_stack_confluence_v1", "BTC/USDT:USDT"))
    assert _pruned_lane(spec("luxy_ut_bot_forecast_v1", "BTC/USDT:USDT"))
    assert _pruned_lane(spec("trend_continuation_v1", "XRP/USDT:USDT"))
    assert _pruned_lane(spec("quant_signal_pack_v1", "DOGE/USDT:USDT"))
    assert _pruned_lane(spec("funding_mean_reversion_v1", "XRP/USDT:USDT"))
    assert _pruned_lane(spec("funding_mean_reversion_v1", "ETH/USDT:USDT"))
    # cut 2026-08-02 from the full-ledger pattern study (proven losers)
    assert _pruned_lane(spec("sats_5m_scalper_v1", "ETH/USD:USD"))   # -$681 / 29% win
    assert _pruned_lane(spec("context_scalper_v2", "ETH/USD:USD"))   # -$27 / 14% win
    # human-directed aggressive hard-cut 2026-08-03 (override; earners only)
    assert _pruned_lane(spec("quant_signal_pack_v1", "ETH/USDT:USDT"))
    assert _pruned_lane(spec("quant_signal_pack_v1", "SOL/USDT:USDT"))
    assert _pruned_lane(spec("volatility_expansion_breakout_v1", "DOGE/USDT:USDT"))
    assert _pruned_lane(spec("vnedge_algo_ml_pro_v1", "ETH/USD:USD"))
    assert _pruned_lane(spec("stealth_trail_bbp_v1", "SOL/USDT:USDT"))
    assert _pruned_lane(spec("luxara_break_bounce_v27_v1", "BTC/USD:USD"))
    assert _pruned_lane(spec("fvg_liquidity_breakout_v1", "ETH/USD:USD"))
    # kept — ONLY the two earners survive the hard-cut
    assert not _pruned_lane(spec("funding_mean_reversion_v1", "BTC/USDT:USDT"))
    assert not _pruned_lane(spec("crypto_trend_atr_margin_v1", "DOGE/USDT:USDT"))


def test_prune_toggle_and_roster_effect():
    env = {"MULTI_LANE_EXCHANGES": "binanceusdm,bybit,delta_india"}
    on = desired_lane_specs({**env, "MULTI_LANE_PRUNE_DEAD": "1"})
    off = desired_lane_specs({**env, "MULTI_LANE_PRUNE_DEAD": "0"})
    assert len(on) <= len(off)  # prune never adds lanes
    # no pruned strategy survives when the filter is on
    assert not any(s.strategy_id in {"alpha_stack_confluence_v1", "luxy_ut_bot_forecast_v1",
                                     "trend_continuation_v1"} for s in on)
    # ...but they can be brought back with the toggle (if present in the base set)
    assert any(s.strategy_id == "trend_continuation_v1" for s in off)


def test_governor_cannot_resurrect_a_pruned_strategy(tmp_path):
    # A stale governor file that proposes a PRUNED strategy as a paper lane must
    # NOT resurrect it — the final-prune pass keeps dead strategies out even when
    # the governor injects them (regression: a cut luxara probe came back
    # 2026-08-03 because governor-added specs bypassed the family prune).
    gov = tmp_path / "governor.json"
    gov.write_text(json.dumps({
        "proposed_roster": {
            "paper_lanes": [{
                "strategy_id": "quant_signal_pack_v1",
                "exchange": "binanceusdm",
                "symbol": "ETH/USDT:USDT",
                "timeframe": "1h",
                "lane_id": "quant_signal_pack_v1_binanceusdm_ethusdt_gov_paper",
            }]
        }
    }))
    specs = desired_lane_specs({
        "MULTI_LANE_EXCHANGES": "binanceusdm,bybit,delta_india",
        "MULTI_LANE_GOVERNOR_PAPER_ROSTER_ONLY": "1",
        "MULTI_LANE_PAPER_GOVERNOR_PATH": str(gov),
    })
    assert not any(s.strategy_id == "quant_signal_pack_v1" for s in specs)
