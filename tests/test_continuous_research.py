"""Continuous research loop — record shape, publishing, dashboard endpoint."""

import json

import pandas as pd
from fastapi.testclient import TestClient

from vnedge.backtest.metrics import BacktestMetrics
from vnedge.backtest.walk_forward import (
    SPARSE_STRATEGY_GATES,
    WalkForwardResult,
    WindowResult,
)
from vnedge.dashboard.app import SnapshotProvider, create_app
from vnedge.research import continuous_research as cr

BASE = 1_750_000_000_000


def ts(i):
    return pd.Timestamp(BASE + i * 3_600_000, unit="ms", tz="UTC")


def metrics(num_trades=6, net=15.0, win_rate=60.0):
    return BacktestMetrics(
        num_trades=num_trades, skipped_by_sizing=0, net_profit_usd=net,
        return_pct=net / 5, max_drawdown_pct=2.0, sharpe=1.0, sortino=1.1,
        profit_factor=1.5, win_rate_pct=win_rate, avg_win_usd=6.0,
        avg_loss_usd=-4.0, total_fees_usd=1.0, total_funding_usd=0.0,
        exit_reasons={},
    )


def make_result(n=5):
    return WalkForwardResult(windows=tuple(
        WindowResult(i, ts(i * 100), ts(i * 100 + 60), ts(i * 100 + 90),
                     {"p": 1}, metrics(), metrics())
        for i in range(n)
    ))


def test_wf_record_pass():
    record = cr.wf_record("funding_mean_reversion_v1", "BTC/USDT:USDT",
                          make_result(), SPARSE_STRATEGY_GATES)
    assert record["verdict"] == "PASS"
    assert record["oos_trades"] == 30
    assert record["windows"] == 5
    assert record["reasons"] == []
    assert record["profit_factor"] == 0.0
    for field in ("strategy", "symbol", "oos_net_usd",
                  "profitable_windows_pct", "traded_windows", "updated"):
        assert field in record


def test_wf_record_reject_carries_reasons():
    record = cr.wf_record("x", "BTC/USDT:USDT", make_result(2), SPARSE_STRATEGY_GATES)
    assert record["verdict"] == "REJECT"
    assert any("splits" in r for r in record["reasons"])


def test_run_walk_forwards_marks_delta_funding_lanes_untestable(monkeypatch, tmp_path):
    from vnedge.data.parquet_store import ParquetStore

    store = ParquetStore(tmp_path)
    candles = pd.DataFrame({
        "timestamp": [ts(i) for i in range(2000)],
        "open": [100.0] * 2000,
        "high": [101.0] * 2000,
        "low": [99.0] * 2000,
        "close": [100.0] * 2000,
        "volume": [10.0] * 2000,
    })
    store.upsert_candles("delta_india", "BTC/USD:USD", "1h", candles)
    monkeypatch.setattr(cr, "walk_forward", lambda *a, **k: make_result())

    records = cr.run_walk_forwards(
        store, cr.ResearchTarget("delta_india", "BTC/USD:USD", "1h")
    )

    by_strategy = {record["strategy"]: record for record in records}
    assert by_strategy["funding_mean_reversion_v1"]["verdict"] == "UNTESTABLE"
    assert by_strategy["funding_squeeze_continuation_v1"]["verdict"] == "UNTESTABLE"
    assert by_strategy["trend_continuation_v1"]["exchange"] == "delta_india"
    assert by_strategy["trend_continuation_v1"]["verdict"] in {"PASS", "REJECT"}


def test_run_walk_forwards_respects_strategy_allowlist(monkeypatch, tmp_path):
    from vnedge.data.parquet_store import ParquetStore

    store = ParquetStore(tmp_path)
    candles = pd.DataFrame({
        "timestamp": [ts(i) for i in range(2000)],
        "open": [100.0] * 2000,
        "high": [101.0] * 2000,
        "low": [99.0] * 2000,
        "close": [100.0] * 2000,
        "volume": [10.0] * 2000,
    })
    store.upsert_candles("binanceusdm", "BTC/USDT:USDT", "1h", candles)
    monkeypatch.setenv("RESEARCH_STRATEGIES", "quant_signal_pack_v1")
    monkeypatch.setattr(cr, "walk_forward", lambda *a, **k: make_result())

    records = cr.run_walk_forwards(
        store, cr.ResearchTarget("binanceusdm", "BTC/USDT:USDT", "1h")
    )

    assert [record["strategy"] for record in records] == ["quant_signal_pack_v1"]


def test_publish_atomic_and_feed(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "OUT_DIR", tmp_path / "live_research")
    records = [cr.wf_record("s", "BTC/USDT:USDT", make_result(), SPARSE_STRATEGY_GATES)]
    scalper = {"flow": ["tick_l2_recorder"], "flow_guards": {"can_trade": False}}
    alpha = {"flow": ["mine_structural_hypotheses"], "flow_guards": {"can_trade": False}}
    params = {"version": "test", "can_trade": False}
    leaderboard = {"summary": {"rows": 1}, "policy": {"can_trade": False}}
    ranker = {"summary": {"ready": 1}, "policy": {"can_trade": False}}
    bot_matrix = {
        "summary": {"sources_reviewed": 15},
        "policy": {"can_trade": False, "can_promote": False},
    }
    payload = cr.ResearchPayload(
        records=records, started=0.0, universe={"targets": 1},
        agent_plan={"policy": {"can_trade": False}}, factor_ranker=ranker,
        public_bot_inspiration=bot_matrix,
        scalper_research=scalper,
        alpha_factory=alpha, scalper_parameter_registry=params,
        edge_leaderboard=leaderboard)
    cr.publish(payload)
    cr.publish(payload)
    latest = json.loads((tmp_path / "live_research" / "latest.json").read_text())
    assert latest["results"][0]["verdict"] == "PASS"
    assert latest["results"][0]["exchange"] == "binanceusdm"
    assert latest["universe"]["targets"] == 1
    assert latest["factor_ranker"]["summary"]["ready"] == 1
    assert latest["factor_ranker"]["policy"]["can_trade"] is False
    assert latest["public_bot_inspiration"]["summary"]["sources_reviewed"] == 15
    assert latest["public_bot_inspiration"]["policy"]["can_trade"] is False
    assert latest["public_bot_inspiration"]["policy"]["can_promote"] is False
    assert latest["scalper_research"]["flow"] == ["tick_l2_recorder"]
    assert latest["scalper_research"]["flow_guards"]["can_trade"] is False
    assert latest["alpha_factory"]["flow"] == ["mine_structural_hypotheses"]
    assert latest["alpha_factory"]["flow_guards"]["can_trade"] is False
    assert latest["scalper_parameter_registry"]["version"] == "test"
    assert latest["scalper_parameter_registry"]["can_trade"] is False
    assert latest["edge_leaderboard"]["summary"]["rows"] == 1
    assert latest["edge_leaderboard"]["policy"]["can_trade"] is False
    assert latest["edge_agents"]["policy"]["can_trade"] is False
    assert "not a promotion" in latest["note"]
    feed = (tmp_path / "live_research" / "feed.jsonl").read_text().strip().splitlines()
    assert len(feed) == 2


def test_publish_payload_schema_matches_legacy_kwargs_dict(tmp_path, monkeypatch):
    """The ResearchPayload refactor must not change the latest.json contract:
    the file must be byte-identical to what the old kwargs-style publish wrote
    (same keys, same order, same values, same formatting)."""
    monkeypatch.setattr(cr, "OUT_DIR", tmp_path / "live_research")
    records = [{"strategy": "s", "symbol": "BTC/USDT:USDT", "verdict": "PASS"}]
    cr.publish(cr.ResearchPayload(
        records=records,
        started=0.0,
        drift_alerts=[{"rule_id": "drift_verdict_flip"}],
        auto_state={"tried": ["a", "b"], "total_attempts": 5},
        agent_plan={"policy": {"can_trade": False}},
        edge_leaderboard={"summary": {"rows": 1}},
        universe={"targets": 2},
        factor_ranker={"summary": {"ready": 1}},
        public_bot_inspiration={"summary": {"sources_reviewed": 15}},
        scalper_research={"flow": ["tick_l2_recorder"]},
        alpha_factory={"flow": ["mine_structural_hypotheses"]},
        scalper_parameter_registry={"version": "test"},
        live_shadow_perf={"available": False},
        event_taker_replay={"rows": []},
        cascade_reversion={"targets": []},
        ai_candidates={"candidates": []},
        leadlag_echo_scalp={"targets": []},
    ))
    text = (tmp_path / "live_research" / "latest.json").read_text()
    written = json.loads(text)
    legacy = {  # exactly what the pre-dataclass publish() kwargs produced
        "generated_at": written["generated_at"],      # wall clock, not schema
        "cycle_seconds": written["cycle_seconds"],    # wall clock, not schema
        "lookback_days": cr.LOOKBACK_DAYS,
        "note": "rolling exploratory walk-forward — a PASS is a candidate "
                "signal, not a promotion; judgment requires untouched data. "
                "auto=true rows are machine-proposed uplift variants, "
                "exploratory only.",
        "results": records,
        "drift_alerts": [{"rule_id": "drift_verdict_flip"}],
        "universe": {"targets": 2},
        "factor_ranker": {"summary": {"ready": 1}},
        "public_bot_inspiration": {"summary": {"sources_reviewed": 15}},
        "scalper_research": {"flow": ["tick_l2_recorder"]},
        "alpha_factory": {"flow": ["mine_structural_hypotheses"]},
        "scalper_parameter_registry": {"version": "test"},
        "event_taker_replay": {"rows": []},
        "cascade_reversion": {"targets": []},
        "ai_candidates": {"candidates": []},
        "leadlag_echo_scalp": {"targets": []},
        "realtime_shadow_scalp": {},
        "shadow_lanes": {"lanes": [], "shadow_trials": [], "blocked": []},
        "live_shadow_perf": {"available": False},
        "edge_agents": {"policy": {"can_trade": False}},
        "edge_leaderboard": {"summary": {"rows": 1}},
        "auto_explore": {"total_attempts": 5, "distinct_variants": 2},
    }
    assert text == json.dumps(legacy, indent=2)
    feed_line = (tmp_path / "live_research" / "feed.jsonl").read_text().strip()
    assert feed_line == json.dumps(legacy)


def make_record(verdict="PASS", net=20.0, trades=6):
    return {"strategy": "funding_mean_reversion_v1", "symbol": "BTC/USDT:USDT",
            "verdict": verdict, "oos_net_usd": net, "oos_trades": trades,
            "reasons": [] if verdict == "PASS" else ["aggregate OOS net not positive"]}


class FakeDiscoveryRow:
    def __init__(self, state, payload):
        self.state = state
        self._payload = payload

    def to_dict(self):
        return dict(self._payload)


def test_scalper_research_flow_orders_discovery_before_replay(monkeypatch, tmp_path):
    calls = []
    targets = (cr.ResearchTarget("binanceusdm", "BTC/USDT:USDT"),)

    def mine(root, passed_targets, days):
        calls.append("edge_miner")
        assert root == tmp_path
        assert passed_targets == targets
        assert days == ("20260704",)
        return (FakeDiscoveryRow("EDGE_CANDIDATE_MAKER", {"edge": "hypothesis"}),)

    def scan(root, passed_targets, days):
        calls.append("scanner_replay")
        assert root == tmp_path
        assert passed_targets == targets
        assert days == ("20260704",)
        return (
            FakeDiscoveryRow("REPLAY_CANDIDATE", {"lane": "replay-pass"}),
            FakeDiscoveryRow("RECORD_MORE", {"lane": "record-more"}),
        )

    def recorder_targets(scans):
        calls.append("recorder_targets")
        return (scans[1],)

    monkeypatch.setattr(cr, "mine_recorded_days", mine)
    monkeypatch.setattr(cr, "scan_recorded_days", scan)
    monkeypatch.setattr(cr, "select_recorder_targets", recorder_targets)

    payload = cr.run_scalper_research(tmp_path, targets, days=("20260704",))

    assert calls == ["edge_miner", "scanner_replay", "recorder_targets"]
    assert payload["flow"][:4] == [
        "tick_l2_recorder",
        "edge_miner",
        "scanner_ranking",
        "conservative_replay",
    ]
    assert payload["flow_guards"]["scanner_output_is_not_candidate"] is True
    assert payload["flow_guards"]["replay_required_for_candidate"] is True
    assert payload["edge_hypotheses"] == [{"edge": "hypothesis"}]
    assert payload["recorder_targets"] == [{"lane": "record-more"}]
    assert payload["replay_candidates"] == [
        {"lane": "replay-pass", "source": "conservative_replay"}
    ]
    assert payload["focus"]["status"] == "REPLAY_CANDIDATE_READY"
    assert payload["focus"]["can_trade"] is False


def test_scalper_research_no_tick_days_requests_recorder(tmp_path):
    targets = (cr.ResearchTarget("binanceusdm", "BTC/USDT:USDT"),)
    payload = cr.run_scalper_research(tmp_path, targets)

    assert payload["days"] == []
    assert payload["edge_hypotheses"] == []
    assert payload["scanner_results"] == []
    assert "recorder" in payload["note"]
    assert payload["flow_guards"]["can_trade"] is False
    assert "focus" not in payload


def test_attribution_by_side():
    from vnedge.backtest.backtester import Trade

    def trade(side, net):
        return Trade(side=side, quantity=1.0, entry_ts=ts(0), entry_price=100.0,
                     exit_ts=ts(1), exit_price=100.0 + net, exit_reason="stop",
                     gross_pnl_usd=net, fees_usd=0.0, funding_usd=0.0,
                     entry_reason="t")

    windows = tuple(
        WindowResult(i, ts(0), ts(1), ts(2), {}, metrics(), metrics(),
                     test_trades=(trade("long", 10.0), trade("short", -4.0)))
        for i in range(3)
    )
    att = cr.side_attribution(WalkForwardResult(windows=windows))
    assert att["long"] == {"trades": 3, "net_usd": 30.0, "win_rate_pct": 100.0}
    assert att["short"]["trades"] == 3 and att["short"]["net_usd"] == -12.0


def test_quant_family_attribution_from_entry_reason():
    from vnedge.backtest.backtester import Trade

    def trade(family, net):
        return Trade(
            side="long", quantity=1.0, entry_ts=ts(0), entry_price=100.0,
            exit_ts=ts(1), exit_price=100.0 + net, exit_reason="take_profit",
            gross_pnl_usd=net, fees_usd=0.0, funding_usd=0.0,
            entry_reason=(
                f"quant_signal_pack long {family} score L/S=6.0/3.0; "
                "features=sweep_low"
            ),
        )

    windows = (
        WindowResult(0, ts(0), ts(1), ts(2), {}, metrics(), metrics(),
                     test_trades=(
                         trade("liquidity_sweep", 10.0),
                         trade("liquidity_sweep", -4.0),
                         trade("fvg_retest", 6.0),
                     )),
    )
    result = WalkForwardResult(windows=windows)

    att = cr.quant_family_attribution(result)
    assert att["liquidity_sweep"]["trades"] == 2
    assert att["liquidity_sweep"]["net_usd"] == 6.0
    assert att["liquidity_sweep"]["profit_factor"] == 2.5
    assert att["liquidity_sweep"]["total_fees_usd"] == 0.0
    assert att["fvg_retest"]["trades"] == 1
    record = cr.wf_record(
        "quant_signal_pack_v1", "BTC/USDT:USDT", result, SPARSE_STRATEGY_GATES
    )
    assert record["family_attribution"]["liquidity_sweep"]["net_usd"] == 6.0


def test_drift_verdict_flip_fires_once():
    prev = [make_record("PASS"), make_record("PASS")]
    alerts = cr.compute_drift_alerts(prev, make_record("REJECT", net=-5.0))
    assert any(a["rule_id"] == "drift_verdict_flip" for a in alerts)
    assert any(a["rule_id"] == "drift_oos_sign_flip" for a in alerts)
    # next cycle: REJECT again — edge conditions must NOT refire
    prev2 = prev + [make_record("REJECT", net=-5.0)]
    again = cr.compute_drift_alerts(prev2, make_record("REJECT", net=-6.0))
    assert not any(a["rule_id"] == "drift_verdict_flip" for a in again)
    assert not any(a["rule_id"] == "drift_oos_sign_flip" for a in again)


def test_drift_consecutive_rejects_fires_exactly_at_threshold():
    prev = [make_record("PASS")] + [make_record("REJECT")] * 2
    alerts = cr.compute_drift_alerts(prev, make_record("REJECT"))
    assert any(a["rule_id"] == "drift_consecutive_rejects" and
               a["severity"] == "critical" for a in alerts)
    prev4 = prev + [make_record("REJECT")]
    again = cr.compute_drift_alerts(prev4, make_record("REJECT"))
    assert not any(a["rule_id"] == "drift_consecutive_rejects" for a in again)


def test_drift_trade_collapse_edge_triggered():
    prev = [make_record(trades=20) for _ in range(8)]
    alerts = cr.compute_drift_alerts(prev, make_record(trades=4))
    assert any(a["rule_id"] == "drift_trade_collapse" for a in alerts)
    prev2 = prev + [make_record(trades=4)]
    again = cr.compute_drift_alerts(prev2, make_record(trades=3))
    assert not any(a["rule_id"] == "drift_trade_collapse" for a in again)


def test_quiet_when_healthy():
    prev = [make_record() for _ in range(10)]
    assert cr.compute_drift_alerts(prev, make_record()) == []


def test_research_endpoint(tmp_path):
    provider = SnapshotProvider()
    provider.publish({"mode": "x"})
    research = tmp_path / "latest.json"
    research.write_text(json.dumps({"generated_at": "t", "results": [{"verdict": "PASS"}]}))
    client = TestClient(create_app(provider, token="t3st", research_path=research))
    assert client.get("/research").status_code == 401
    payload = client.get("/research?token=t3st").json()
    assert payload["results"][0]["verdict"] == "PASS"


def test_research_endpoint_missing_file(tmp_path):
    provider = SnapshotProvider()
    provider.publish({"mode": "x"})
    client = TestClient(create_app(provider, token="t3st",
                                   research_path=tmp_path / "nope.json"))
    assert client.get("/research?token=t3st").json() == {"results": []}
