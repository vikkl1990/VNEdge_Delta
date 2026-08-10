"""Pine alpha distiller tests."""

import json

from vnedge.research.pine_alpha_distiller import (
    main,
    publish_pine_alpha_distiller,
    run_pine_alpha_distiller,
)
from vnedge.research.pine_script_research import publish_pine_research_kb


def test_pine_alpha_distiller_routes_source_backed_liquidity_task(tmp_path):
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    (source_dir / "fvg_sweep_breakout.pine").write_text(
        """
//@version=6
strategy("FVG Sweep Breakout", overlay=true)
atr = ta.atr(14)
volOk = volume > ta.sma(volume, 20) * 1.8
zoneTop = ta.highest(high[1], 20)
sweep = low < ta.lowest(low[1], 12) and close > open
long = sweep and close > zoneTop and volOk
if long
    strategy.entry("L", strategy.long)
    strategy.exit("LX", "L", stop=close - atr, limit=close + atr * 2.5)
""",
        encoding="utf-8",
    )
    kb = tmp_path / "kb.json"
    publish_pine_research_kb(
        source_dir=source_dir,
        output_path=kb,
        include_defaults=False,
        source_label="unit",
    )

    payload = run_pine_alpha_distiller(kb_path=kb, source_dir=source_dir)

    assert payload["distiller_id"] == "pine_alpha_distiller_v1"
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False
    assert payload["summary"]["source_backed_reviewed"] == 1
    row = payload["script_distillations"][0]
    assert row["action"] == "PORT_CANDIDATE"
    assert row["recommended_port"] == "fvg_liquidity_breakout_v1"
    assert row["intention"]["intent_family"] == "liquidity_sweep_reclaim"
    assert "sweeps" in row["intention"]["trading_thesis"]
    assert "closed-bar reclaim" in row["intention"]["trigger"]
    assert any("structural SL" in atom for atom in row["intention"]["portable_atoms"])
    assert {"liquidity_zone", "sweep_reclaim", "range_breakout", "volume_participation"}.issubset(
        set(row["primitives"])
    )
    assert payload["summary"]["intention_clusters"] == 1
    assert payload["intention_clusters"][0]["intent_family"] == "liquidity_sweep_reclaim"
    assert "Context: 1h bias" in payload["intention_clusters"][0]["playbook"]
    assert tuple(payload["port_tasks"][0]["gate_before_shadow"][-3:]) == (
        "expected net edge >25 bps after fees, slippage, and safety buffer",
        "PF >1.5 and at least 20 historical trades",
        "untouched-window judgment passes before paper or shadow promotion",
    )


def test_pine_alpha_distiller_quarantines_repaint_when_requested(tmp_path):
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    (source_dir / "mtf_risky.pine").write_text(
        """
//@version=6
indicator("MTF Risky", overlay=true)
htf = request.security(syminfo.tickerid, "60", close, lookahead=barmerge.lookahead_on)
plot(htf)
""",
        encoding="utf-8",
    )
    kb = tmp_path / "kb.json"
    publish_pine_research_kb(
        source_dir=source_dir,
        output_path=kb,
        include_defaults=False,
        source_label="unit",
    )

    quarantine_payload = run_pine_alpha_distiller(kb_path=kb, source_dir=source_dir)
    portable_only_payload = run_pine_alpha_distiller(
        kb_path=kb,
        source_dir=source_dir,
        include_repaint=False,
    )

    assert portable_only_payload["summary"]["source_backed_reviewed"] == 0
    assert quarantine_payload["summary"]["causality_quarantine"] == 1
    row = quarantine_payload["script_distillations"][0]
    assert row["recommended_port"] == "causality_quarantine_v1"
    assert row["intention"]["intent_family"] == "causality_quarantine"
    assert "unconfirmed higher-timeframe reads" in row["intention"]["non_portable_atoms"]
    assert "lookahead_on" in row["risks"]


def test_pine_alpha_distiller_routes_trail_exit_and_orderflow(tmp_path):
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    (source_dir / "trail_exit.pine").write_text(
        """
//@version=6
strategy("ATR Trail Exit Plan", overlay=true)
trail = ta.supertrend(2.0, 10)
long = close > ta.ema(close, 55)
if long
    strategy.entry("L", strategy.long)
    strategy.exit("LX", "L", stop=close - ta.atr(14), limit=close + ta.atr(14) * 3)
""",
        encoding="utf-8",
    )
    (source_dir / "cvd_absorption.pine").write_text(
        """
//@version=6
strategy("CVD Footprint Absorption", overlay=true)
cvd = ta.cum(volume * math.sign(close - open))
long = close > open and cvd > ta.sma(cvd, 20)
if long
    strategy.entry("L", strategy.long)
    strategy.exit("LX", "L", stop=low, limit=close + ta.atr(14) * 2)
""",
        encoding="utf-8",
    )
    kb = tmp_path / "kb.json"
    publish_pine_research_kb(
        source_dir=source_dir,
        output_path=kb,
        include_defaults=False,
        source_label="unit",
    )

    payload = run_pine_alpha_distiller(kb_path=kb, source_dir=source_dir)
    ports = {row["recommended_port"] for row in payload["script_distillations"]}
    by_port = {row["recommended_port"]: row for row in payload["script_distillations"]}

    assert "trail_exit_lab_v1" in ports
    assert "orderflow_proxy_v1" in ports
    assert by_port["trail_exit_lab_v1"]["intention"]["bot_use"].startswith("Exit overlay")
    assert by_port["orderflow_proxy_v1"]["intention"]["execution_bias"].startswith("Maker-first")
    assert payload["summary"]["queued_backtest_cells"] == 24


def test_pine_alpha_distiller_publish_never_writes_source_code(tmp_path):
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    (source_dir / "range_breakout.pine").write_text(
        """
//@version=6
strategy("Range Breakout", overlay=true)
long = close > ta.highest(high[1], 20) and volume > ta.sma(volume, 20)
if long
    strategy.entry("L", strategy.long)
""",
        encoding="utf-8",
    )
    kb = tmp_path / "kb.json"
    publish_pine_research_kb(
        source_dir=source_dir,
        output_path=kb,
        include_defaults=False,
        source_label="unit",
    )
    out = tmp_path / "distiller.json"
    feed = tmp_path / "feed.jsonl"

    payload = run_pine_alpha_distiller(kb_path=kb, source_dir=source_dir)
    publish_pine_alpha_distiller(payload, out=out, feed=feed)
    encoded = out.read_text(encoding="utf-8")

    assert json.loads(encoded)["policy"]["research_only"] is True
    assert "//@version" not in encoded
    assert "strategy(" not in encoded
    assert "indicator(" not in encoded
    assert "plotshape" not in encoded


def test_pine_alpha_distiller_cli_writes_artifact(tmp_path, capsys):
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    (source_dir / "breakout.pine").write_text(
        """
//@version=6
strategy("Open Breakout", overlay=true)
long = close > ta.highest(high[1], 12) and volume > ta.sma(volume, 20)
if long
    strategy.entry("L", strategy.long)
""",
        encoding="utf-8",
    )
    kb = tmp_path / "kb.json"
    publish_pine_research_kb(
        source_dir=source_dir,
        output_path=kb,
        include_defaults=False,
        source_label="unit",
    )
    out = tmp_path / "latest.json"

    rc = main([
        "--kb",
        str(kb),
        "--source-dir",
        str(source_dir),
        "--out",
        str(out),
        "--feed",
        str(tmp_path / "feed.jsonl"),
    ])

    assert rc == 0
    assert str(out) in capsys.readouterr().out
    assert json.loads(out.read_text(encoding="utf-8"))["summary"]["port_candidates"] == 1
