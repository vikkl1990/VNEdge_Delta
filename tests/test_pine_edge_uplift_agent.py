"""Agentic Pine edge-uplift planner stays research-only."""

import json
import stat
from datetime import UTC, datetime

from vnedge.research.pine_edge_uplift_agent import (
    main,
    publish_pine_edge_uplift_agent,
    run_pine_edge_uplift_agent,
)


def _write_inputs(tmp_path):
    kb = tmp_path / "pine_research_kb.json"
    distiller = tmp_path / "pine_alpha_distiller_latest.json"
    kb.write_text(json.dumps({
        "generated_at": "2026-07-19T00:00:00+00:00",
        "source": "unit",
        "records": [
            {
                "script_id": "proof_stack",
                "title": "Proof Stack",
                "crypto_fit_score": 82,
                "features": ["breakout", "momentum", "risk_plan"],
                "backtests": [
                    {
                        "timeframe": "15m",
                        "status": "passed",
                        "samples": 24,
                        "avg_net_bps": 31.5,
                        "profit_factor": 1.72,
                        "blocker": "PASS",
                    }
                ],
            },
            {
                "script_id": "near_miss",
                "title": "Near Miss",
                "crypto_fit_score": 76,
                "features": ["liquidity", "volume"],
                "backtests": [
                    {
                        "timeframe": "5m",
                        "status": "failed",
                        "samples": 34,
                        "avg_net_bps": -4.2,
                        "profit_factor": 0.91,
                        "blocker": "fee wall",
                    }
                ],
            },
            {
                "script_id": "hard_negative",
                "title": "Hard Negative",
                "crypto_fit_score": 44,
                "features": ["trend"],
                "backtests": [
                    {
                        "timeframe": "5m",
                        "status": "failed",
                        "samples": 42,
                        "avg_net_bps": -32.0,
                        "profit_factor": 0.22,
                        "blocker": "negative after costs",
                    }
                ],
            },
        ],
        "can_trade": False,
        "can_promote": False,
    }))
    distiller.write_text(json.dumps({
        "distiller_id": "pine_alpha_distiller_v1",
        "script_distillations": [
            {
                "script_id": "proof_stack",
                "recommended_port": "fvg_liquidity_breakout_v1",
                "primitives": ["liquidity_zone", "sweep_reclaim", "momentum_confirm"],
            },
            {
                "script_id": "near_miss",
                "recommended_port": "range_expansion_breakout_v1",
                "primitives": ["range_breakout", "volume_participation", "risk_plan"],
            },
            {
                "script_id": "hard_negative",
                "recommended_port": "trend_momentum_context_v1",
                "primitives": ["trend_trail", "momentum_confirm"],
            },
        ],
        "can_trade": False,
        "can_promote": False,
    }))
    return kb, distiller


def test_pine_edge_uplift_agent_recycles_failures_without_trade_permission(tmp_path):
    kb, distiller = _write_inputs(tmp_path)

    payload = run_pine_edge_uplift_agent(
        kb_path=kb,
        distiller_path=distiller,
        now=datetime(2026, 7, 19, tzinfo=UTC),
    )

    assert payload["agent_id"] == "pine_edge_uplift_agent_v1"
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False
    assert payload["policy"]["blocked_actions"] == [
        "auto_promote",
        "paper_trade_from_failed_cell",
        "copy_protected_source",
        "relax_live_risk_gates",
    ]
    assert payload["summary"]["promotable_proofs"] == 1
    assert payload["summary"]["near_miss_after_cost"] == 1
    assert payload["summary"]["failed_cells"] == 2

    by_script = {row["script_id"]: row for row in payload["top_uplifts"]}
    assert by_script["proof_stack"]["uplift_action"] == "PRE_REGISTER_UNTOUCHED_JUDGMENT"
    assert by_script["near_miss"]["uplift_action"] == "ADD_EXECUTION_FILTER_OR_MAKER_TAKER_ROUTER"
    assert by_script["hard_negative"]["use_as"] in {"context_feature", "feature_bank"}
    assert all(row["can_trade"] is False for row in payload["top_uplifts"])

    experiments = {row["experiment_type"] for row in payload["experiments"]}
    assert "untouched_judgment_candidate" in experiments
    assert "execution_filtered_replay" in experiments
    assert "edge_model_feature_bank" in experiments
    assert all(row["can_promote"] is False for row in payload["experiments"])


def test_publish_pine_edge_uplift_agent_is_atomic_and_feed_safe(tmp_path):
    kb, distiller = _write_inputs(tmp_path)
    payload = run_pine_edge_uplift_agent(kb_path=kb, distiller_path=distiller)
    out = tmp_path / "pine_edge_uplift_agent_latest.json"
    feed = tmp_path / "pine_edge_uplift_agent_feed.jsonl"

    publish_pine_edge_uplift_agent(payload, out=out, feed=feed)
    publish_pine_edge_uplift_agent(payload, out=out, feed=feed)

    saved = json.loads(out.read_text())
    rows = [json.loads(line) for line in feed.read_text().splitlines()]
    assert saved["agent_id"] == "pine_edge_uplift_agent_v1"
    assert len(rows) == 2
    assert rows[-1]["can_trade"] is False
    assert stat.S_IMODE(out.stat().st_mode) == 0o644
    assert stat.S_IMODE(feed.stat().st_mode) == 0o644


def test_pine_edge_uplift_agent_does_not_invent_empty_experiments(tmp_path):
    kb = tmp_path / "empty_kb.json"
    distiller = tmp_path / "empty_distiller.json"
    kb.write_text(json.dumps({"records": [], "can_trade": False, "can_promote": False}))
    distiller.write_text(json.dumps({"script_distillations": []}))

    payload = run_pine_edge_uplift_agent(kb_path=kb, distiller_path=distiller)

    assert payload["summary"]["scripts_reviewed"] == 0
    assert payload["summary"]["experiments"] == 0
    assert payload["experiments"] == []


def test_failed_positive_cell_is_not_promotable_proof(tmp_path):
    kb = tmp_path / "kb.json"
    distiller = tmp_path / "distiller.json"
    kb.write_text(json.dumps({
        "records": [
            {
                "script_id": "failed_positive",
                "title": "Failed Positive",
                "crypto_fit_score": 88,
                "features": ["breakout", "risk_plan"],
                "backtests": [
                    {
                        "timeframe": "15m",
                        "status": "failed",
                        "samples": 30,
                        "avg_net_bps": 42.0,
                        "profit_factor": 1.9,
                        "blocker": "max drawdown",
                    }
                ],
            }
        ],
        "can_trade": False,
        "can_promote": False,
    }))
    distiller.write_text(json.dumps({
        "script_distillations": [
            {
                "script_id": "failed_positive",
                "recommended_port": "range_expansion_breakout_v1",
                "primitives": ["range_breakout", "risk_plan"],
            }
        ],
    }))

    payload = run_pine_edge_uplift_agent(kb_path=kb, distiller_path=distiller)

    row = payload["top_uplifts"][0]
    assert payload["summary"]["promotable_proofs"] == 0
    assert row["agent_verdict"] == "POSITIVE_BUT_UNDER_SAMPLED"
    assert row["uplift_action"] == "EXPAND_REPLAY_WINDOW_AND_CROSS_VENUE"
    assert row["can_promote"] is False


def test_pine_edge_uplift_agent_cli_writes_artifact(tmp_path, capsys):
    kb, distiller = _write_inputs(tmp_path)
    out = tmp_path / "uplift.json"
    feed = tmp_path / "uplift.jsonl"

    assert main([
        "--kb",
        str(kb),
        "--distiller",
        str(distiller),
        "--out",
        str(out),
        "--feed",
        str(feed),
    ]) == 0

    printed = capsys.readouterr().out
    payload = json.loads(out.read_text())
    assert "pine edge uplift agent" in printed
    assert payload["summary"]["experiments"] >= 1
