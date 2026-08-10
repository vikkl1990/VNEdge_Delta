"""Fee-wall paper-probe bridge tests."""

from __future__ import annotations

import json
import stat

from vnedge.research.fee_wall_paper_probe_bridge import (
    ProbeBridgeConfig,
    build_fee_wall_paper_probe_manifest,
    main,
    publish_fee_wall_paper_probe_manifest,
)


def candidate(**overrides) -> dict:
    row = {
        "exchange": "delta_india",
        "symbol": "ETH/USD:USD",
        "timeframe": "5m",
        "strategy": "quantified_fee_wall_sniper_v1",
        "verdict": "TAKER_EDGE",
        "recommended_action": "PRE_REGISTER_UNTOUCHED_JUDGMENT_WINDOW",
        "routed": 24,
        "avg_selected_net_bps": 18.5,
        "profit_factor": 1.72,
        "fee_wall_break_rate_pct": 62.0,
        "paper_margin_usd": 100.0,
        "paper_leverage": 25.0,
    }
    row.update(overrides)
    return row


def test_fee_wall_probe_bridge_publishes_only_strict_fee_breakers():
    payload = {
        "generated_at": "2026-07-30T00:00:00+00:00",
        "truth_layer": "fee_wall_forensics_v1",
        "strict_fee_wall_candidates": [
            candidate(avg_selected_net_bps=12.0, profit_factor=1.4),
            candidate(
                symbol="BTC/USDT:USDT",
                strategy="luxara_live_plan_qtm_v1",
                verdict="MAKER_EDGE",
                avg_selected_net_bps=28.0,
                profit_factor=2.1,
            ),
            candidate(strategy="visual_only_v1"),
            candidate(routed=2),
            candidate(avg_selected_net_bps=2.0),
            candidate(profit_factor=0.8),
            candidate(verdict="UNDER_SAMPLED"),
        ],
    }

    manifest = build_fee_wall_paper_probe_manifest(
        payload,
        config=ProbeBridgeConfig(max_probes=4),
    )

    assert manifest["manifest_id"] == "fee_wall_paper_probe_bridge_v1"
    assert manifest["policy"]["paper_only"] is True
    assert manifest["policy"]["can_trade_live"] is False
    assert manifest["summary"]["source_candidates"] == 7
    assert manifest["summary"]["eligible_candidates"] == 2
    assert manifest["summary"]["published_probes"] == 2
    assert [row["strategy"] for row in manifest["paper_probes"]] == [
        "luxara_live_plan_qtm_v1",
        "quantified_fee_wall_sniper_v1",
    ]
    assert manifest["paper_probes"][1]["verdict"] == "TAKER_EDGE"
    assert any(
        "strategy_not_probe_allowed" in row["rejected_reasons"]
        for row in manifest["rejected"]
    )


def test_fee_wall_probe_bridge_publish_is_atomic_and_feed_safe(tmp_path):
    manifest = build_fee_wall_paper_probe_manifest(
        {"strict_fee_wall_candidates": [candidate()]},
    )
    out = tmp_path / "fee_wall_paper_probes.json"
    feed = tmp_path / "fee_wall_paper_probes_feed.jsonl"

    publish_fee_wall_paper_probe_manifest(manifest, out=out, feed=feed)
    publish_fee_wall_paper_probe_manifest(manifest, out=out, feed=feed)

    saved = json.loads(out.read_text(encoding="utf-8"))
    feed_rows = [json.loads(line) for line in feed.read_text(encoding="utf-8").splitlines()]
    assert saved["summary"]["published_probes"] == 1
    assert len(feed_rows) == 2
    assert feed_rows[-1]["top_strategy"] == "quantified_fee_wall_sniper_v1"
    assert stat.S_IMODE(out.stat().st_mode) == 0o644
    assert stat.S_IMODE(feed.stat().st_mode) == 0o644


def test_fee_wall_probe_bridge_cli_writes_manifest(tmp_path):
    source = tmp_path / "fee_wall_forensics_latest.json"
    out = tmp_path / "fee_wall_paper_probes.json"
    source.write_text(
        json.dumps({"strict_fee_wall_candidates": [candidate()]}),
        encoding="utf-8",
    )

    rc = main([
        "--input",
        str(source),
        "--out",
        str(out),
        "--feed",
        "",
    ])

    assert rc == 0
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["paper_probes"][0]["strategy"] == "quantified_fee_wall_sniper_v1"
