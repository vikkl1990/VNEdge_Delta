"""Lane-health auditor — desired vs active vs freshness vs signal counts.

Covers every per-lane verdict (OK / STALE / SILENT / MISSING / ORPHAN),
the cron exit-code contract (1 on MISSING or STALE, 0 otherwise), and the
snapshot integration in MultiLaneProvider (lane_health key, never crashes).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from vnedge.runtime.lane_health import (
    SILENT_EVAL_SECONDS,
    VERDICT_MISSING,
    VERDICT_OK,
    VERDICT_ORPHAN,
    VERDICT_PROBATION,
    VERDICT_SILENT,
    VERDICT_STALE,
    LaneHealthReport,
    audit_lanes,
    main,
)
from vnedge.runtime.multi_lane import LaneSpec, MultiLaneProvider
from vnedge.runtime.multi_lane_shadow import desired_lane_specs
from vnedge.runtime.runner_config import RunnerMode

NOW = 1_751_900_000.0  # fixed 'now' for deterministic ages

HOUR = 3600.0

# Deterministic single-lane environment for CLI-level tests: one binance
# shadow funding-MR lane, no candidate/manifest/delta extras.
CLI_ENV = {
    "MULTI_LANE_EXCHANGES": "binanceusdm",
    "MULTI_LANE_SYMBOLS": "BTC/USDT:USDT",
    "MULTI_LANE_MODES": "shadow",
    "MULTI_LANE_CANDIDATES": "0",
    "MULTI_LANE_CRYPTO_TREND_DOGE": "0",
    "MULTI_LANE_DELTA_FUNDING_MR": "0",
}


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat()


def spec(lane_id: str, timeframe: str = "1h") -> LaneSpec:
    return LaneSpec(
        lane_id=lane_id,
        exchange="binanceusdm",
        symbol="BTC/USDT:USDT",
        timeframe=timeframe,
    )


def write_journal(
    journal_dir: Path,
    lane_id: str,
    records: list[tuple[float, str]],
    *,
    now: float = NOW,
    suffix: str = ".journal.jsonl",
) -> Path:
    """Write (age_seconds, kind) records, oldest first, as journal JSONL."""
    journal_dir.mkdir(parents=True, exist_ok=True)
    path = journal_dir / f"{lane_id}{suffix}"
    with open(path, "w", encoding="utf-8") as handle:
        for age, kind in records:
            record = {"ts": _iso(now - age), "kind": kind, "payload": {}}
            handle.write(json.dumps(record) + "\n")
    return path


def ok_records(now_age: float = 30.0) -> list[tuple[float, str]]:
    """A healthy tail: old start, then a fresh lane_eval."""
    return [(2 * HOUR, "session_start"), (HOUR, "lane_eval"), (now_age, "lane_eval")]


def audit_one(journal_dir: Path, lane_spec: LaneSpec, now: float = NOW):
    report = audit_lanes(journal_dir, desired=[lane_spec], now=now)
    return report, report.rows[0]


# --- per-lane verdicts ----------------------------------------------------------------


def test_verdict_ok(tmp_path):
    write_journal(tmp_path, "lane_a", ok_records())
    report, row = audit_one(tmp_path, spec("lane_a"))
    assert row.verdict == VERDICT_OK
    assert row.exists_active and row.evaluating and not row.stale
    assert report.healthy
    assert report.production_healthy
    assert report.production_summary() == "1/1 trade-compatible"
    assert report.summary() == "1/1 OK"
    assert report.totals == {
        "desired": 1, "active": 1, "ok": 1,
        "stale": 0, "probation": 0, "silent": 0, "missing": 0, "orphan": 0,
        "trade_compatible": 1, "production_blockers": 0,
    }


def test_verdict_stale_when_records_older_than_3_bars(tmp_path):
    # 1h timeframe -> stale threshold 3h; newest record is 4h old.
    write_journal(tmp_path, "lane_a", [(6 * HOUR, "lane_eval"), (4 * HOUR, "lane_eval")])
    report, row = audit_one(tmp_path, spec("lane_a"))
    assert row.verdict == VERDICT_STALE
    assert row.stale and row.exists_active
    assert row.last_record_age_seconds is not None
    assert row.last_record_age_seconds > 3 * HOUR
    assert not report.healthy


def test_stale_threshold_scales_with_timeframe(tmp_path):
    # A 4h-old record is STALE on 1h bars but fine on 4h bars (limit 12h).
    write_journal(tmp_path, "lane_a", [(25 * HOUR, "lane_eval"), (4 * HOUR, "lane_eval")])
    _, row_1h = audit_one(tmp_path, spec("lane_a", timeframe="1h"))
    _, row_4h = audit_one(tmp_path, spec("lane_a", timeframe="4h"))
    assert row_1h.verdict == VERDICT_STALE
    assert row_4h.verdict == VERDICT_OK


def test_verdict_silent_journaling_but_not_evaluating(tmp_path):
    # Active for 25h, records still flowing, but not one lane_eval record:
    # the plumbing is alive while the strategy loop is broken.
    write_journal(tmp_path, "lane_a", [
        (25 * HOUR, "session_start"),
        (12 * HOUR, "order_intent"),
        (60.0, "equity_mark"),
    ])
    report, row = audit_one(tmp_path, spec("lane_a"))
    assert row.verdict == VERDICT_SILENT
    assert not row.evaluating and not row.stale
    assert row.last_eval_age_seconds is None
    # SILENT alone does not trip the cron contract (MISSING/STALE only).
    assert report.healthy
    assert not report.production_healthy


def test_verdict_silent_when_last_eval_older_than_24h(tmp_path):
    write_journal(tmp_path, "lane_a", [
        (30 * HOUR, "lane_eval"),
        (60.0, "equity_mark"),
    ])
    _, row = audit_one(tmp_path, spec("lane_a"))
    assert row.verdict == VERDICT_SILENT
    assert row.last_eval_age_seconds is not None
    assert row.last_eval_age_seconds > SILENT_EVAL_SECONDS


def test_fresh_lane_without_eval_is_not_silent_yet(tmp_path):
    # Started 1h ago with no lane_eval yet — a fresh start, not a broken loop.
    write_journal(tmp_path, "lane_a", [(HOUR, "session_start"), (30.0, "equity_mark")])
    _, row = audit_one(tmp_path, spec("lane_a"))
    assert row.verdict == VERDICT_OK


def test_verdict_missing_desired_lane_without_journal(tmp_path):
    report, row = audit_one(tmp_path, spec("lane_a"))
    assert row.verdict == VERDICT_MISSING
    assert not row.exists_active
    assert not report.healthy
    assert report.totals["missing"] == 1
    assert report.summary() == "1 MISSING"


def test_verdict_orphan_journal_without_desired_spec(tmp_path):
    write_journal(tmp_path, "lane_a", ok_records())
    write_journal(tmp_path, "left_behind", ok_records())
    report = audit_lanes(tmp_path, desired=[spec("lane_a")], now=NOW)
    by_id = {row.lane_id: row for row in report.rows}
    assert by_id["lane_a"].verdict == VERDICT_OK
    assert by_id["left_behind"].verdict == VERDICT_ORPHAN
    assert by_id["left_behind"].exists_active
    # orphans are attention noise, not a cron failure
    assert report.healthy
    assert not report.production_healthy
    assert report.totals["orphan"] == 1
    assert report.totals["active"] == 1  # orphans not counted as active desired lanes


def test_shadow_probation_when_virtual_outcomes_are_net_negative(tmp_path):
    lane = LaneSpec(
        lane_id="shadow_lane",
        exchange="bybit",
        symbol="BTC/USDT:USDT",
        mode=RunnerMode.SHADOW,
    )
    write_journal(tmp_path, "shadow_lane", ok_records())
    with open(tmp_path / "shadow_lane.journal.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "ts": _iso(NOW - 10.0),
            "kind": "shadow_outcome",
            "payload": {"virtual_net_usd": -5.25, "resolution": "stop"},
        }) + "\n")
    report = audit_lanes(tmp_path, desired=[lane], now=NOW)
    row = report.rows[0]
    assert row.verdict == VERDICT_PROBATION
    assert row.shadow_virtual_trades == 1
    assert row.shadow_net_usd == -5.25
    assert row.trade_compatible is False
    assert report.healthy  # warning, not a missing/stale process failure
    assert not report.production_healthy
    assert report.summary() == "1 SHADOW_PROBATION"
    assert report.production_summary() == "1 SHADOW_PROBATION"


def test_equity_file_freshness_counts_for_staleness(tmp_path):
    # Journal tail is 5h old but the equity historian is still writing:
    # the lane is alive, not STALE (newest of journal/equity wins).
    write_journal(tmp_path, "lane_a", [(6 * HOUR, "lane_eval"), (5 * HOUR, "lane_eval")])
    write_journal(tmp_path, "lane_a", [(10.0, "equity_mark")], suffix=".equity.jsonl")
    _, row = audit_one(tmp_path, spec("lane_a"))
    assert row.verdict == VERDICT_OK
    assert not row.stale


def test_empty_journal_file_ages_into_stale_via_mtime(tmp_path):
    path = tmp_path / "lane_a.journal.jsonl"
    path.touch()
    old = time.time() - 5 * HOUR
    os.utime(path, (old, old))
    _, row = audit_one(tmp_path, spec("lane_a"), now=time.time())
    assert row.verdict == VERDICT_STALE


def test_malformed_lines_are_tolerated(tmp_path):
    path = write_journal(tmp_path, "lane_a", ok_records())
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("not json at all\n{truncated\n")
    _, row = audit_one(tmp_path, spec("lane_a"))
    assert row.verdict == VERDICT_OK


def test_missing_journal_dir_reports_all_missing(tmp_path):
    report = audit_lanes(tmp_path / "does_not_exist",
                         desired=[spec("a"), spec("b")], now=NOW)
    assert [row.verdict for row in report.rows] == [VERDICT_MISSING, VERDICT_MISSING]
    assert not report.healthy


def test_summary_orders_problems_by_severity(tmp_path):
    write_journal(tmp_path, "stale_lane", [(9 * HOUR, "lane_eval")])
    write_journal(tmp_path, "silent_lane", [(30 * HOUR, "session_start"), (60.0, "mark")])
    write_journal(tmp_path, "orphan_lane", ok_records())
    report = audit_lanes(
        tmp_path,
        desired=[spec("missing_lane"), spec("stale_lane"), spec("silent_lane")],
        now=NOW,
    )
    assert report.summary() == "1 MISSING, 1 STALE, 1 SILENT, 1 ORPHAN"
    assert not report.healthy
    assert not report.production_healthy


def test_report_dict_and_snapshot_shapes(tmp_path):
    write_journal(tmp_path, "lane_a", ok_records())
    report = audit_lanes(tmp_path, desired=[spec("lane_a"), spec("gone")], now=NOW)

    full = report.to_dict()
    assert full["healthy"] is False
    assert full["process_healthy"] is False
    assert full["production_healthy"] is False
    assert full["journal_dir"] == str(tmp_path)
    assert {row["lane_id"] for row in full["rows"]} == {"lane_a", "gone"}

    snap = report.to_snapshot()
    assert snap["healthy"] is False
    assert snap["process_healthy"] is False
    assert snap["production_healthy"] is False
    assert snap["summary"] == "1 MISSING"
    assert snap["production_summary"] == "1 MISSING"
    assert snap["totals"]["ok"] == 1
    # only problem lanes are listed, keeping the dashboard payload small
    assert snap["problems"] == [
        {
            "lane_id": "gone",
            "verdict": VERDICT_MISSING,
            "age_seconds": None,
            "detail": "desired lane has no journal file",
            "trade_compatible": False,
        }
    ]
    json.dumps(full), json.dumps(snap)  # both must be JSON-serializable


def test_healthy_property_contract():
    def report_with(verdict: str) -> LaneHealthReport:
        from vnedge.runtime.lane_health import LaneHealthRow
        return LaneHealthReport(
            generated_at="", journal_dir="",
            rows=(LaneHealthRow(lane_id="x", verdict=verdict),),
        )

    assert report_with(VERDICT_OK).healthy
    assert report_with(VERDICT_SILENT).healthy
    assert report_with(VERDICT_PROBATION).healthy
    assert report_with(VERDICT_ORPHAN).healthy
    assert not report_with(VERDICT_STALE).healthy
    assert not report_with(VERDICT_MISSING).healthy
    assert report_with(VERDICT_OK).production_healthy
    assert not report_with(VERDICT_SILENT).production_healthy
    assert not report_with(VERDICT_PROBATION).production_healthy
    assert not report_with(VERDICT_ORPHAN).production_healthy
    assert not report_with(VERDICT_STALE).production_healthy
    assert not report_with(VERDICT_MISSING).production_healthy


# --- CLI exit-code contract -------------------------------------------------------


def _cli_lane() -> LaneSpec:
    specs = desired_lane_specs(CLI_ENV)
    assert len(specs) == 1, "CLI_ENV must resolve to exactly one lane"
    return specs[0]


def test_cli_exit_0_when_clean(tmp_path, capsys):
    lane = _cli_lane()
    write_journal(tmp_path, lane.lane_id, ok_records(), now=time.time())
    code = main(["--journal-dir", str(tmp_path)], environ=CLI_ENV)
    assert code == 0
    out = capsys.readouterr().out
    assert VERDICT_OK in out and lane.lane_id in out


def test_cli_exit_1_on_missing(tmp_path, capsys):
    code = main(["--journal-dir", str(tmp_path)], environ=CLI_ENV)
    assert code == 1
    assert VERDICT_MISSING in capsys.readouterr().out


def test_cli_exit_1_on_stale(tmp_path, capsys):
    lane = _cli_lane()
    write_journal(tmp_path, lane.lane_id, [(9 * HOUR, "lane_eval")], now=time.time())
    code = main(["--journal-dir", str(tmp_path)], environ=CLI_ENV)
    assert code == 1
    assert VERDICT_STALE in capsys.readouterr().out


def test_cli_exit_0_when_only_silent(tmp_path, capsys):
    lane = _cli_lane()
    write_journal(
        tmp_path, lane.lane_id,
        [(30 * HOUR, "session_start"), (30.0, "equity_mark")],
        now=time.time(),
    )
    code = main(["--journal-dir", str(tmp_path)], environ=CLI_ENV)
    assert code == 0
    assert VERDICT_SILENT in capsys.readouterr().out


def test_cli_json_output(tmp_path, capsys):
    lane = _cli_lane()
    write_journal(tmp_path, lane.lane_id, ok_records(), now=time.time())
    code = main(["--journal-dir", str(tmp_path), "--json"], environ=CLI_ENV)
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["healthy"] is True
    assert payload["process_healthy"] is True
    assert payload["production_healthy"] is True
    assert payload["summary"] == "1/1 OK"
    assert payload["production_summary"] == "1/1 trade-compatible"
    assert payload["rows"][0]["lane_id"] == lane.lane_id


def test_cli_module_entrypoint(tmp_path):
    """python -m vnedge.runtime.lane_health works and honors the exit contract."""
    src_dir = Path(__file__).resolve().parents[1] / "src"
    env = {**os.environ, **CLI_ENV}
    env["PYTHONPATH"] = str(src_dir) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-m", "vnedge.runtime.lane_health",
         "--journal-dir", str(tmp_path)],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 1, proc.stderr  # empty dir -> desired lane MISSING
    assert VERDICT_MISSING in proc.stdout


# --- snapshot integration (MultiLaneProvider) --------------------------------------


def lane_snap(symbol="BTC/USDT:USDT"):
    return {
        "mode": "shadow (live data)", "symbol": symbol, "equity": 500.0,
        "realized_pnl": 0.0, "unrealized_pnl": 0.0, "fills": 0,
        "fees_usd": 0.0, "risk_status": "ok",
        "feed_health": {"candles": "ok"}, "positions": [],
    }


def test_provider_snapshot_exposes_lane_health(tmp_path):
    lane = spec("lane_a")
    write_journal(tmp_path, "lane_a", ok_records(), now=time.time())
    provider = MultiLaneProvider("lane_a", lane_specs=[lane], journal_dir=tmp_path)
    provider.sink("lane_a", "binanceusdm").publish(lane_snap())
    out = provider.latest()
    health = out["lane_health"]
    assert health["healthy"] is True
    assert health["production_healthy"] is True
    assert health["summary"] == "1/1 OK"
    assert health["production_summary"] == "1/1 trade-compatible"
    assert health["problems"] == []


def test_provider_lane_health_flags_missing_lane(tmp_path):
    provider = MultiLaneProvider(
        "lane_a", lane_specs=[spec("lane_a"), spec("never_started")],
        journal_dir=tmp_path,
    )
    write_journal(tmp_path, "lane_a", ok_records(), now=time.time())
    provider.sink("lane_a", "binanceusdm").publish(lane_snap())
    health = provider.latest()["lane_health"]
    assert health["healthy"] is False
    assert health["production_healthy"] is False
    assert health["totals"]["missing"] == 1
    assert health["problems"][0]["lane_id"] == "never_started"


def test_provider_survives_absent_journal_dir(tmp_path):
    provider = MultiLaneProvider(
        "lane_a", lane_specs=[spec("lane_a")],
        journal_dir=tmp_path / "never" / "created",
    )
    provider.sink("lane_a", "binanceusdm").publish(lane_snap())
    out = provider.latest()  # must not raise
    assert out["lane_health"]["healthy"] is False
    assert out["lane_health"]["production_healthy"] is False
    assert out["lane_health"]["totals"]["missing"] == 1


def test_provider_without_health_config_omits_key():
    provider = MultiLaneProvider("lane_a")
    provider.sink("lane_a", "binanceusdm").publish(lane_snap())
    assert "lane_health" not in provider.latest()


def test_provider_swallows_audit_failures(tmp_path, monkeypatch):
    import vnedge.runtime.lane_health as lane_health_mod

    def boom(*args, **kwargs):
        raise RuntimeError("audit exploded")

    monkeypatch.setattr(lane_health_mod, "audit_lanes", boom)
    provider = MultiLaneProvider(
        "lane_a", lane_specs=[spec("lane_a")], journal_dir=tmp_path
    )
    provider.sink("lane_a", "binanceusdm").publish(lane_snap())
    out = provider.latest()  # observability must never take down the snapshot
    assert "lane_health" not in out
    assert out["lane_id"] == "lane_a"
