"""Agent Gateway job runner stays research-only while producing evidence."""

from __future__ import annotations

import json

import pandas as pd

from vnedge.agent_gateway.job_runner import JobOutcome, run_pending_jobs
from vnedge.agent_gateway.jobs import (
    BLOCKED_STATUS,
    DONE_STATUS,
    FAILED_STATUS,
    PENDING_STATUS,
    create_backtest_job,
    read_job,
)
from vnedge.agent_gateway.seed_jobs import DEFAULT_SEED_REQUESTS, seed_default_jobs
from vnedge.data.parquet_store import ParquetStore


def _request(**overrides):
    return {
        "strategy_id": "trend_continuation_v1",
        "exchange": "binanceusdm",
        "symbol": "BTC/USDT:USDT",
        "timeframe": "1h",
        "initial_capital_usd": 500.0,
        "commission_bps": None,
        "slippage_bps": None,
        "strict_mode": True,
        "live_orders_enabled": False,
        "parameters": {"breakout_bars": 5, "max_holding_bars": 8},
        **overrides,
    }


def _candles(rows: int = 340) -> pd.DataFrame:
    ts = pd.date_range("2026-01-01", periods=rows, freq="h", tz="UTC")
    close = pd.Series(range(rows), dtype=float) * 0.75 + 100.0
    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": close - 0.2,
            "high": close + 0.8,
            "low": close - 0.8,
            "close": close,
            "volume": 1000.0,
        }
    )


def test_runner_claims_pending_job_and_persists_terminal_result(tmp_path):
    jobs_dir = tmp_path / "jobs"
    artifact_dir = tmp_path / "artifacts"
    job = create_backtest_job(jobs_dir=jobs_dir, agent="agent", request=_request())

    completed = run_pending_jobs(
        jobs_dir=jobs_dir,
        artifact_dir=artifact_dir,
        executor=lambda _: JobOutcome(
            status=DONE_STATUS,
            result={"runner": "test", "value": 1},
        ),
    )

    assert len(completed) == 1
    stored = read_job(jobs_dir, job["job_id"])
    assert stored is not None
    assert stored["status"] == DONE_STATUS
    assert stored["result"]["value"] == 1
    assert stored["result"]["can_trade"] is False
    assert stored["result"]["can_promote"] is False
    assert stored["result"]["live_orders_enabled"] is False
    assert (artifact_dir / f"{job['job_id']}.json").exists()
    history = [json.loads(line) for line in (jobs_dir / "jobs.jsonl").read_text().splitlines()]
    assert [event["status"] for event in history] == [
        PENDING_STATUS,
        "RUNNING_RESEARCH_ONLY",
        DONE_STATUS,
    ]


def test_runner_records_executor_failure_without_reopening_trading(tmp_path):
    jobs_dir = tmp_path / "jobs"
    job = create_backtest_job(jobs_dir=jobs_dir, agent="agent", request=_request())

    def explode(_job):
        raise RuntimeError("boom")

    completed = run_pending_jobs(jobs_dir=jobs_dir, artifact_dir=None, executor=explode)

    assert completed[0]["job_id"] == job["job_id"]
    assert completed[0]["status"] == FAILED_STATUS
    assert completed[0]["can_trade"] is False
    assert "boom" in completed[0]["error"]


def test_registered_strategy_job_backtests_local_parquet_data(tmp_path):
    data_root = tmp_path / "data"
    jobs_dir = tmp_path / "jobs"
    store = ParquetStore(data_root)
    store.upsert_candles("binanceusdm", "BTC/USDT:USDT", "1h", _candles())
    request = _request(parameters={"breakout_bars": 5, "max_holding_bars": 8, "note": "ignored"})
    job = create_backtest_job(jobs_dir=jobs_dir, agent="agent", request=request)

    completed = run_pending_jobs(
        jobs_dir=jobs_dir,
        data_root=data_root,
        artifact_dir=tmp_path / "artifacts",
    )

    assert completed[0]["job_id"] == job["job_id"]
    assert completed[0]["status"] == DONE_STATUS
    result = completed[0]["result"]
    assert result["execution"] == "registered_strategy_backtest"
    assert result["bars"] == 340
    assert result["accepted_parameters"] == {"breakout_bars": 5}
    assert result["ignored_parameters"] == ["note"]
    assert result["metrics"]["num_trades"] >= 0
    assert result["promotion_verdict"] == "NOT_EVALUATED_AGENT_JOB"
    assert result["can_trade"] is False
    assert result["can_promote"] is False


def test_runner_blocks_missing_data_and_live_enabled_requests(tmp_path):
    jobs_dir = tmp_path / "jobs"
    missing = create_backtest_job(jobs_dir=jobs_dir, agent="agent", request=_request())
    live = create_backtest_job(
        jobs_dir=jobs_dir,
        agent="agent",
        request=_request(live_orders_enabled=True),
    )

    completed = run_pending_jobs(
        jobs_dir=jobs_dir,
        data_root=tmp_path / "data",
        artifact_dir=None,
        max_jobs=2,
    )

    by_id = {job["job_id"]: job for job in completed}
    assert by_id[missing["job_id"]]["status"] == BLOCKED_STATUS
    assert "market data unavailable" in by_id[missing["job_id"]]["blocked_reason"]
    assert by_id[live["job_id"]]["status"] == BLOCKED_STATUS
    assert "live_orders_enabled" in by_id[live["job_id"]]["blocked_reason"]
    assert by_id[live["job_id"]]["can_trade"] is False


def test_candidate_replay_agent_job_runs_research_only_adapter(tmp_path, monkeypatch):
    jobs_dir = tmp_path / "jobs"
    request = _request(
        strategy_id="candidate_replay_executor_v1",
        parameters={
            "max_event_leadlag": 7,
            "max_orderflow": 11,
            "min_replay_fills": 3,
            "notional_usd": 250.0,
            "queue_aware": True,
        },
    )
    job = create_backtest_job(jobs_dir=jobs_dir, agent="agent", request=request)
    seen = {}

    def fake_replay(data_root, *, event_leadlag_path, orderflow_path, config):
        seen["data_root"] = data_root
        seen["event_leadlag_path"] = str(event_leadlag_path)
        seen["orderflow_path"] = str(orderflow_path)
        seen["config"] = config.to_dict()
        return {
            "generated_at": "2026-07-15T00:00:00+00:00",
            "summary": {"rows": 1, "replay_candidates": 1, "fills": 5},
            "rows": [
                {
                    "candidate_id": "r1",
                    "verdict": "REPLAY_CANDIDATE",
                    "net_usd": 0.42,
                    "can_trade": False,
                    "can_promote": False,
                }
            ],
            "can_trade": False,
            "can_promote": False,
        }

    monkeypatch.setattr("vnedge.agent_gateway.job_runner.run_candidate_replay", fake_replay)

    completed = run_pending_jobs(
        jobs_dir=jobs_dir,
        data_root=tmp_path / "data",
        artifact_dir=tmp_path / "artifacts",
    )

    assert completed[0]["job_id"] == job["job_id"]
    assert completed[0]["status"] == DONE_STATUS
    result = completed[0]["result"]
    assert result["execution"] == "candidate_replay"
    assert result["summary"]["replay_candidates"] == 1
    assert result["top_rows"][0]["verdict"] == "REPLAY_CANDIDATE"
    assert result["promotion_verdict"] == "NOT_EVALUATED_AGENT_JOB"
    assert result["can_trade"] is False
    assert result["can_promote"] is False
    assert seen["config"]["max_event_leadlag_specs"] == 7
    assert seen["config"]["max_orderflow_specs"] == 11
    assert seen["config"]["min_replay_fills"] == 3
    assert seen["config"]["notional_usd"] == 250.0
    assert seen["config"]["queue_aware"] is True


def test_candidate_replay_adapter_flag_does_not_require_registered_strategy(
    tmp_path, monkeypatch
):
    jobs_dir = tmp_path / "jobs"
    create_backtest_job(
        jobs_dir=jobs_dir,
        agent="agent",
        request=_request(
            strategy_id="agent_microstructure_probe",
            parameters={"adapter": "candidate_replay"},
        ),
    )

    monkeypatch.setattr(
        "vnedge.agent_gateway.job_runner.run_candidate_replay",
        lambda *_args, **_kwargs: {"summary": {}, "rows": [], "can_trade": False},
    )

    completed = run_pending_jobs(jobs_dir=jobs_dir, data_root=tmp_path / "data")

    assert completed[0]["status"] == DONE_STATUS
    assert completed[0]["result"]["execution"] == "candidate_replay"


def test_quantos_seed_jobs_are_idempotent_and_research_only(tmp_path):
    jobs_dir = tmp_path / "jobs"

    first = seed_default_jobs(jobs_dir)
    second = seed_default_jobs(jobs_dir)

    assert first["created_count"] == len(DEFAULT_SEED_REQUESTS)
    assert first["skipped_count"] == 0
    assert second["created_count"] == 0
    assert second["skipped_count"] == len(DEFAULT_SEED_REQUESTS)
    assert first["can_trade"] is False
    assert first["can_promote"] is False

    jobs = sorted(jobs_dir.glob("agj_*.json"))
    assert len(jobs) == len(DEFAULT_SEED_REQUESTS)
    stored = [read_job(jobs_dir, path.stem) for path in jobs]
    assert all(job is not None for job in stored)
    for job in stored:
        assert job["created_by"] == "quantos_seed"
        assert job["can_trade"] is False
        assert job["can_promote"] is False
        assert job["live_orders_enabled"] is False
        assert job["request"]["strict_mode"] is True
        assert job["request"]["live_orders_enabled"] is False


def test_quantos_seed_dry_run_does_not_write(tmp_path):
    jobs_dir = tmp_path / "jobs"

    payload = seed_default_jobs(jobs_dir, dry_run=True)

    assert payload["created_count"] == len(DEFAULT_SEED_REQUESTS)
    assert payload["skipped_count"] == 0
    assert not jobs_dir.exists()


def test_quantos_seed_request_signature_allows_corrected_specs(tmp_path):
    jobs_dir = tmp_path / "jobs"
    create_backtest_job(
        jobs_dir=jobs_dir,
        agent="old_seed",
        request={
            "strategy_id": "sats_5m_scalper_v1",
            "exchange": "delta_india",
            "symbol": "ETH/USDT:USDT",
            "timeframe": "5m",
            "hypothesis_id": "quantos_seed_sats_5m_delta_eth",
            "strict_mode": True,
            "live_orders_enabled": False,
            "parameters": {"seed_id": "quantos_seed_sats_5m_delta_eth"},
        },
    )

    payload = seed_default_jobs(jobs_dir)

    assert payload["created_count"] == len(DEFAULT_SEED_REQUESTS)
    sats_jobs = [
        read_job(jobs_dir, path.stem)
        for path in jobs_dir.glob("agj_*.json")
        if (read_job(jobs_dir, path.stem) or {}).get("request", {}).get("strategy_id")
        == "sats_5m_scalper_v1"
    ]
    symbols = sorted(job["request"]["symbol"] for job in sats_jobs if job is not None)
    assert symbols == ["ETH/USD:USD", "ETH/USDT:USDT"]
