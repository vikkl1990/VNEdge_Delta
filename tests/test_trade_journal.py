import json

from vnedge.dashboard.trade_journal import build_trade_journal


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")


def test_trade_journal_projects_fills_orders_and_virtual_trades(tmp_path):
    write_jsonl(
        tmp_path / "alpha.fills.jsonl",
        [
            {
                "ts": "2026-07-16T01:00:00+00:00",
                "mode": "paper",
                "venue": "delta_india",
                "strategy_id": "sats_5m_scalper_v1",
                "symbol": "ETH/USD:USD",
                "side": "buy",
                "quantity": 0.2,
                "price": 1780.0,
                "fee_usd": 0.09,
                "realized_pnl_usd": 0.0,
                "client_order_id": "entry-1",
                "hash": "h1",
            },
            {
                "ts": "2026-07-16T01:08:00+00:00",
                "mode": "paper",
                "venue": "delta_india",
                "strategy_id": "sats_5m_scalper_v1",
                "symbol": "ETH/USD:USD",
                "side": "sell",
                "quantity": 0.2,
                "price": 1788.0,
                "fee_usd": 0.1,
                "realized_pnl_usd": 1.6,
                "client_order_id": "exit-1",
                "hash": "h2",
            },
        ],
    )
    write_jsonl(
        tmp_path / "alpha.journal.jsonl",
        [
            {
                "ts": "2026-07-16T01:00:00+00:00",
                "kind": "risk_decision",
                "payload": {"client_order_id": "entry-1", "approved": True},
            },
            {
                "ts": "2026-07-16T01:00:01+00:00",
                "kind": "order_intent",
                "payload": {
                    "client_order_id": "entry-1",
                    "intent": {
                        "symbol": "ETH/USD:USD",
                        "side": "long",
                        "quantity": 0.2,
                        "order_type": "market",
                        "reduce_only": False,
                        "strategy_id": "sats_5m_scalper_v1",
                    },
                },
            },
            {
                "ts": "2026-07-16T01:00:02+00:00",
                "kind": "order_acknowledged",
                "payload": {
                    "client_order_id": "entry-1",
                    "exchange_order_id": "ex-1",
                },
            },
            {
                "ts": "2026-07-16T01:05:00+00:00",
                "kind": "shadow_outcome",
                "payload": {
                    "intent_key": "shadow-k",
                    "resolution": "target",
                    "virtual_net_usd": 2.25,
                    "side": "long",
                    "entry_price": 1781.0,
                    "exit_price": 1790.0,
                    "fees_usd": 0.2,
                    "bar_ts": "2026-07-16T01:05:00+00:00",
                },
            },
            {
                "ts": "2026-07-16T01:06:00+00:00",
                "kind": "scalp_shadow_outcome",
                "payload": {
                    "intent_key": "scalp-k",
                    "family": "cascade",
                    "resolution": "timeout",
                    "side": "short",
                    "virtual_net_usd": -0.4,
                    "taker_net_bps": -8.0,
                    "maker_net_bps": -3.0,
                    "entry_price": 1789.0,
                    "exit_price": 1790.0,
                },
            },
        ],
    )
    snapshot = {
        "lane_id": "alpha",
        "ts": "2026-07-16T01:09:00+00:00",
        "positions": [
            {
                "symbol": "ETH/USD:USD",
                "side": "long",
                "quantity": 0.2,
                "entry_price": 1780.0,
                "mark_price": 1788.0,
                "notional_usd": 357.6,
                "unrealized_usd": 1.6,
            }
        ],
        "open_orders": [],
        "session": {
            "trade_log": [
                {
                    "ts": "2026-07-16T01:00:00+00:00",
                    "event": "order_submitted",
                    "detail": "long ETH",
                }
            ]
        },
    }

    payload = build_trade_journal(
        snapshot=snapshot,
        journal_dir=tmp_path,
        history_path=tmp_path / "alpha.equity.jsonl",
        lane="alpha",
    )

    assert payload["policy"]["read_only"] is True
    assert payload["can_trade"] is False
    assert payload["summary"]["positions"] == 1
    assert payload["summary"]["fills"] == 2
    assert payload["summary"]["closed_trades"] == 3
    assert payload["summary"]["actual_closed_trades"] == 1
    assert payload["summary"]["shadow_closed_trades"] == 2
    assert payload["summary"]["actual_realized_pnl_usd"] == 1.6
    assert payload["summary"]["fees_usd"] == 0.19
    assert payload["summary"]["actual_closed_net_usd"] == 1.41
    assert payload["summary"]["actual_closed_fees_usd"] == 0.19
    assert payload["summary"]["virtual_net_usd"] == 1.85
    assert payload["orders"][0]["state"] == "acknowledged"
    assert {row["kind"] for row in payload["closed_trades"]} == {
        "actual_closing_fill",
        "shadow_outcome",
        "scalp_shadow_outcome",
    }
    assert any(event["event"] == "order_acknowledged" for event in payload["events"])


def test_trade_journal_days_filter_and_lane_filter(tmp_path):
    write_jsonl(
        tmp_path / "alpha.fills.jsonl",
        [
            {
                "ts": "2026-07-01T00:00:00+00:00",
                "symbol": "BTC/USDT:USDT",
                "side": "buy",
                "quantity": 0.01,
                "price": 100.0,
            }
        ],
    )
    write_jsonl(
        tmp_path / "beta.fills.jsonl",
        [
            {
                "ts": "2026-07-16T00:00:00+00:00",
                "symbol": "ETH/USD:USD",
                "side": "sell",
                "quantity": 0.02,
                "price": 1800.0,
            }
        ],
    )

    payload = build_trade_journal(
        snapshot={},
        journal_dir=tmp_path,
        lane="beta",
        since="2026-07-10T00:00:00+00:00",
    )

    assert payload["lane"] == "beta"
    assert payload["summary"]["fill_ledgers_scanned"] == 1
    assert [row["lane"] for row in payload["fills"]] == ["beta"]


# --- cohort P&L split (honest headline) ------------------------------------------

from vnedge.dashboard.trade_journal import _cohort_pnl_rollup, _lane_cohort


def test_lane_cohort_classification():
    assert _lane_cohort("velocity_sats_5m_scalper_delta_india_eth_usd_usd_shadow") == "control"
    assert _lane_cohort("papertrial_stealth_trail_bbp_v1_delta_india_eth_usd_usd_4h") == "tracked"
    assert _lane_cohort("evidence_vnedge_algo_ml_pro_v1_delta_india_eth_usd_usd_4h_shadow") == "tracked"
    assert _lane_cohort("fee_wall_luxy_ut_bot_forecast_bybit_btc_usdt_usdt_15m_paper_probe") == "tracked"
    assert _lane_cohort("funding_mr_btc_v1_20260703") == "tracked"
    assert _lane_cohort("quant_signal_pack_v1_binanceusdm_ethusdt_shadow") == "research"
    assert _lane_cohort("") == "research"


def test_cohort_rollup_separates_controls_from_tracked():
    trades = [
        {"lane": "velocity_sats_5m_scalper_delta_india_eth_usd_usd_shadow", "virtual_net_usd": -10.0},
        {"lane": "velocity_sats_5m_scalper_delta_india_btc_usd_usd_shadow", "virtual_net_usd": -5.0},
        {"lane": "funding_mr_btc_v1_20260703", "virtual_net_usd": 4.0},
        {"lane": "papertrial_stealth_trail_bbp_v1_delta_india_eth_usd_usd_4h", "virtual_net_usd": -1.0},
        {"lane": "quant_signal_pack_v1_binanceusdm_ethusdt_shadow", "virtual_net_usd": -3.0},
    ]
    roll = _cohort_pnl_rollup(trades)
    assert set(roll) == {"tracked", "research", "control"}
    assert roll["control"]["closed"] == 2 and roll["control"]["net_usd"] == -15.0
    assert roll["control"]["wins"] == 0 and roll["control"]["win_rate_pct"] == 0.0
    assert roll["tracked"]["closed"] == 2 and roll["tracked"]["net_usd"] == 3.0
    assert roll["tracked"]["wins"] == 1 and roll["tracked"]["win_rate_pct"] == 50.0
    assert roll["research"]["closed"] == 1 and roll["research"]["net_usd"] == -3.0
    # every cohort carries its human label + honesty note
    assert roll["control"]["label"] == "Deliberate controls"
    assert "EXPECTED to lose" in roll["control"]["note"]


def test_cohort_rollup_present_in_journal_summary(tmp_path):
    out = build_trade_journal(snapshot=None, journal_dir=tmp_path, history_path=tmp_path / "none")
    assert "cohort_pnl" in out["summary"]
    assert set(out["summary"]["cohort_pnl"]) == {"tracked", "research", "control"}
