"""Fee-wall paper-probe actual outcome report tests."""

import json

from vnedge.research.fee_wall_probe_actuals import (
    STATE_NOT_LAUNCHED,
    STATE_ONLINE_NO_TRADES,
    STATE_PAPER_ACTIVE_NEGATIVE,
    STATE_PAPER_ACTIVE_PROFITABLE,
    build_fee_wall_probe_actuals,
    main,
)


def probe(strategy="stealth_trail_bbp_v1", exchange="bybit", symbol="DOGE/USDT:USDT", timeframe="1h"):
    return {
        "probe_id": f"{strategy}__{exchange}__doge_usdt_usdt__{timeframe}",
        "rank": 1,
        "exchange": exchange,
        "symbol": symbol,
        "timeframe": timeframe,
        "strategy": strategy,
        "strategy_id": strategy,
        "verdict": "MAKER_EDGE",
        "avg_selected_net_bps": 32.2,
        "profit_factor": 1.78,
        "routed": 11,
        "fee_wall_break_rate_pct": 91.0,
        "paper_margin_usd": 100.0,
        "paper_leverage": 25.0,
    }


def perf_row(lane_id: str, state: str, *, closed=2, net=7.5, pf=1.8):
    return {
        "lane_id": lane_id,
        "state": state,
        "latest_ts": "2026-07-30T10:00:00+00:00",
        "closed_trades": closed,
        "live_signals": 4,
        "paper_order_intents": 3,
        "fills": closed * 2,
        "net_pnl_usd": net,
        "closed_net_pnl_usd": net,
        "fees_usd": 1.2,
        "profit_factor": pf,
        "latest_why_no_trade": "last_eval_no_signal",
    }


def test_fee_wall_probe_actuals_join_manifest_to_paper_performance():
    profitable_lane = "fee_wall_stealth_trail_bbp_bybit_doge_usdt_usdt_1h_paper_probe"
    negative_lane = "fee_wall_luxara_live_plan_qtm_binanceusdm_sol_usdt_usdt_15m_paper_probe"
    no_trade_lane = "fee_wall_luxy_ut_bot_forecast_delta_india_sol_usd_usd_15m_paper_probe"
    manifest = {
        "manifest_id": "fee_wall_paper_probe_bridge_v1",
        "generated_at": "2026-07-30T09:00:00+00:00",
        "paper_probes": [
            probe(),
            probe(
                strategy="luxara_live_plan_qtm_v1",
                exchange="binanceusdm",
                symbol="SOL/USDT:USDT",
                timeframe="15m",
            ),
            probe(
                strategy="luxy_ut_bot_forecast_v1",
                exchange="delta_india",
                symbol="SOL/USDT:USDT",
                timeframe="15m",
            ),
            probe(
                strategy="quantified_fee_wall_sniper_v1",
                exchange="delta_india",
                symbol="ETH/USDT:USDT",
                timeframe="5m",
            ),
        ],
    }
    performance = {
        "rows": [
            perf_row(profitable_lane, "PAPER_ACTIVE_PROFITABLE", closed=4, net=12.0, pf=2.2),
            perf_row(negative_lane, "PAPER_ACTIVE_NEGATIVE", closed=2, net=-8.0, pf=0.0),
            perf_row(no_trade_lane, "PAPER_ONLINE_NO_TRADES", closed=0, net=0.0, pf=0.0),
        ],
    }

    report = build_fee_wall_probe_actuals(manifest=manifest, performance=performance)

    states = {row["lane_id"]: row["actual_state"] for row in report["rows"]}
    assert states[profitable_lane] == STATE_PAPER_ACTIVE_PROFITABLE
    assert states[negative_lane] == STATE_PAPER_ACTIVE_NEGATIVE
    assert states[no_trade_lane] == STATE_ONLINE_NO_TRADES
    assert (
        states["fee_wall_quantified_fee_wall_sniper_delta_india_eth_usd_usd_5m_paper_probe"]
        == STATE_NOT_LAUNCHED
    )
    assert report["summary"]["published_probes"] == 4
    assert report["summary"]["closed_trade_probes"] == 2
    assert report["summary"]["profitable_probes"] == 1
    assert report["summary"]["negative_probes"] == 1
    assert report["summary"]["not_launched"] == 1
    assert report["can_trade"] is False
    assert report["can_promote"] is False


def test_fee_wall_probe_actuals_cli_writes_artifact(tmp_path):
    manifest = tmp_path / "probes.json"
    performance = tmp_path / "perf.json"
    out = tmp_path / "actuals.json"
    feed = tmp_path / "feed.jsonl"
    lane = "fee_wall_stealth_trail_bbp_bybit_doge_usdt_usdt_1h_paper_probe"
    manifest.write_text(json.dumps({"paper_probes": [probe()]}))
    performance.write_text(json.dumps({"rows": [perf_row(lane, "PAPER_ACTIVE_NEGATIVE", net=-3.0, pf=0.5)]}))

    code = main(
        [
            "--manifest",
            str(manifest),
            "--performance",
            str(performance),
            "--route-doctor",
            str(tmp_path / "missing.json"),
            "--out",
            str(out),
            "--feed",
            str(feed),
            "--once",
        ]
    )

    assert code == 0
    saved = json.loads(out.read_text())
    assert saved["rows"][0]["actual_state"] == STATE_PAPER_ACTIVE_NEGATIVE
    assert json.loads(feed.read_text().splitlines()[-1])["negative_probes"] == 1
