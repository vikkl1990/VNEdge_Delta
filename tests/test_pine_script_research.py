"""Pine script research KB tests."""

import json

import vnedge.research.pine_script_research as pine_research
from vnedge.research.pine_script_research import (
    build_pine_coverage_audit,
    default_pine_research_payload,
    discover_tradingview_catalog_urls,
    extract_tradingview_catalog_page_urls,
    extract_tradingview_script_urls,
    load_pine_research_payload,
    load_pine_extraction_manifest,
    main,
    publish_pine_research_kb,
    review_pine_source,
    summarize_extraction_manifest,
)


def test_default_pine_research_payload_is_research_only():
    payload = default_pine_research_payload()

    assert payload["can_trade"] is False
    assert payload["can_promote"] is False
    assert payload["summary"]["total"] >= 3
    assert payload["summary"]["portable"] >= 1
    assert any(r["script_id"] == "luxara_live_plan_qtm_v1" for r in payload["records"])
    assert any(r["script_id"] == "luxara_break_bounce_v27_v1" for r in payload["records"])


def test_load_pine_research_payload_summarizes_generated_artifact(tmp_path):
    path = tmp_path / "pine_research_kb.json"
    path.write_text(json.dumps({
        "generated_at": "2026-07-18T00:00:00+00:00",
        "source": "unit",
        "backtest_evidence": {
            "evidence_id": "pine_backtest_evidence_v1",
            "completed_cells": 1,
            "status_counts": {"failed": 1, "queued": 1},
            "can_trade": False,
            "can_promote": False,
        },
        "records": [
            {
                "script_id": "open_ut_bot",
                "title": "Open UT Bot",
                "url": "https://www.tradingview.com/script/example/",
                "crypto_portability": "PORTABLE_WITH_CHANGES",
                "crypto_fit_score": 76,
                "backtests": [{"timeframe": "5m", "status": "queued"}],
            },
            {
                "script_id": "protected_overlay",
                "title": "Protected Overlay",
                "url": "https://www.tradingview.com/script/locked/",
                "crypto_portability": "BLOCKED_NO_SOURCE",
                "crypto_fit_score": 5,
                "backtests": [],
            },
        ],
    }))

    payload = load_pine_research_payload(path)

    assert payload["summary"] == {
        "total": 2,
        "source_backed": 0,
        "catalog_only": 2,
        "reconciled_catalog_matches": 0,
        "port_queue": 0,
        "source_requests": 2,
        "portable": 1,
        "needs_source": 1,
        "research_only": 0,
        "blocked_repaint": 0,
        "backtests_queued": 1,
    }
    assert payload["backtest_evidence"]["completed_cells"] == 1
    assert payload["backtest_evidence"]["status_counts"] == {"failed": 1, "queued": 1}
    assert payload["coverage_audit"]["coverage_id"] == "pine_coverage_auditor_v1"
    assert payload["coverage_audit"]["visible_records"] == 2
    assert payload["coverage_audit"]["catalog_only_loaded"] == 2
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False


def test_review_pine_source_flags_repaint_risk():
    source = """
//@version=6
indicator("MTF risky", overlay=true)
htf = request.security(syminfo.tickerid, "60", close, lookahead=barmerge.lookahead_on)
plot(htf)
"""

    record = review_pine_source(
        script_id="mtf_risky",
        title="MTF Risky",
        url="https://www.tradingview.com/script/risky/",
        source=source,
        source_license="MPL-2.0",
    )

    assert record.source_available is True
    assert record.source_sha256
    assert record.crypto_portability == "BLOCKED_REPAINT_RISK"
    assert "lookahead_on" in record.risks
    assert all(cell.status == "blocked" for cell in record.backtests)


def test_review_pine_source_scores_portable_crypto_mechanics():
    source = """
//@version=6
strategy("Breakout", overlay=true)
ema50 = ta.ema(close, 50)
atr14 = ta.atr(14)
volOk = volume > ta.sma(volume, 20) * 2
long = close > ta.highest(high[1], 12) and close > ema50 and volOk
if long
    strategy.entry("L", strategy.long)
    strategy.exit("LX", "L", stop=close - atr14, limit=close + atr14 * 2)
"""

    record = review_pine_source(
        script_id="breakout",
        title="Breakout",
        url="user_supplied",
        source=source,
    )

    assert record.kind == "strategy"
    assert record.crypto_fit_score >= 45
    assert record.crypto_portability in {"PORTABLE", "PORTABLE_WITH_CHANGES"}
    assert "breakout" in record.features
    assert "risk_plan" in record.features


def test_review_pine_source_detects_libraries():
    source = """
// This Pine Script code is subject to the Mozilla Public License 2.0
//@version=6
library("Range Breakout Helpers")
export isBreakout(float closePrice, float highLevel) =>
    closePrice > highLevel
"""

    record = review_pine_source(
        script_id="range_breakout_helpers",
        title="Range Breakout Helpers",
        url="https://www.tradingview.com/script/example/",
        source=source,
    )

    assert record.kind == "library"
    assert record.source_available is True


def test_publish_pine_research_kb_reviews_source_directory(tmp_path):
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    pine = source_dir / "open_breakout.pine"
    pine.write_text(
        """
// This Pine Script code is subject to the Mozilla Public License 2.0
//@version=6
strategy("Open Breakout", overlay=true)
atr = ta.atr(14)
long = close > ta.highest(high[1], 20) and volume > ta.sma(volume, 20)
if long
    strategy.entry("L", strategy.long)
    strategy.exit("LX", "L", stop=close - atr, limit=close + atr * 2)
""",
        encoding="utf-8",
    )
    output = tmp_path / "pine_research_kb.json"

    payload = publish_pine_research_kb(
        source_dir=source_dir,
        output_path=output,
        include_defaults=False,
        source_label="unit",
    )

    saved = json.loads(output.read_text(encoding="utf-8"))
    assert payload["summary"] == saved["summary"]
    assert payload["source"] == saved["source"]
    assert saved["summary"]["total"] == 1
    record = saved["records"][0]
    assert record["script_id"] == "open_breakout"
    assert record["title"] == "Open Breakout"
    assert record["source_license"] == "MPL-2.0"
    assert record["source_sha256"]
    assert record["can_trade"] is False
    assert record["can_promote"] is False


def test_extract_tradingview_script_urls_dedupes_catalog_payload():
    html = """
<a href="/script/A47z5YCR-Luxy-UT-God-Mode-UT-Bot-Forecast-Signals-Zones-and-Risk/">
<a href="https://www.tradingview.com/script/A47z5YCR-Luxy-UT-God-Mode-UT-Bot-Forecast-Signals-Zones-and-Risk/#chart-view-comment-form">
<a href="https://in.tradingview.com/script/gnAGO9QR-Momentum-Cascade-Lyro-RS/">
"""

    urls = extract_tradingview_script_urls(html)

    assert urls == (
        "https://www.tradingview.com/script/A47z5YCR-Luxy-UT-God-Mode-UT-Bot-Forecast-Signals-Zones-and-Risk/",
        "https://www.tradingview.com/script/gnAGO9QR-Momentum-Cascade-Lyro-RS/",
    )


def test_extract_tradingview_catalog_page_urls_dedupes():
    html = """
<a href="/scripts/scalping/">
<a href="https://in.tradingview.com/scripts/scalping/?sort=recent">
<a href="/scripts/orderblocks/">
<a href="/script/A47z5YCR-Luxy-UT-God-Mode-UT-Bot-Forecast-Signals-Zones-and-Risk/">
"""

    urls = extract_tradingview_catalog_page_urls(html)

    assert urls == (
        "https://www.tradingview.com/scripts/scalping/",
        "https://www.tradingview.com/scripts/orderblocks/",
    )


def test_discover_tradingview_catalog_urls_crawls_accessible_frontier(monkeypatch):
    pages = {
        "https://www.tradingview.com/scripts/crypto/": """
            <a href="/script/AAAA1111-Crypto-Flow-Setup/">
            <a href="/scripts/scalping/">
        """,
        "https://www.tradingview.com/scripts/scalping/": """
            <a href="/script/BBBB2222-Five-Minute-Scalp-Trend/">
        """,
    }

    monkeypatch.setattr(
        pine_research,
        "_fetch_catalog_html",
        lambda url: pages.get(url, ""),
    )

    shallow = discover_tradingview_catalog_urls(
        catalog_discovery_urls=["https://www.tradingview.com/scripts/crypto/"],
        discovery_depth=0,
        max_pages=4,
    )
    deep = discover_tradingview_catalog_urls(
        catalog_discovery_urls=["https://www.tradingview.com/scripts/crypto/"],
        discovery_depth=1,
        max_pages=4,
    )

    assert shallow == ("https://www.tradingview.com/script/AAAA1111-Crypto-Flow-Setup/",)
    assert deep == (
        "https://www.tradingview.com/script/AAAA1111-Crypto-Flow-Setup/",
        "https://www.tradingview.com/script/BBBB2222-Five-Minute-Scalp-Trend/",
    )


def test_publish_pine_research_kb_adds_catalog_backlog(tmp_path):
    html = tmp_path / "catalog.html"
    html.write_text(
        """
<a href="/script/Vgitqk52-Order-Flow-Volume-Delta-CVD-Absorption-Divergence-LunqFX/">
<a href="/script/ODpXONLY-Liquidity-Sweep-Detector-Pro/">
""",
        encoding="utf-8",
    )
    output = tmp_path / "kb.json"

    payload = publish_pine_research_kb(
        source_dir=tmp_path / "missing",
        catalog_html_files=[html],
        output_path=output,
        include_defaults=False,
        source_label="unit_catalog",
    )

    assert payload["summary"]["total"] == 2
    assert payload["summary"]["catalog_only"] == 2
    assert payload["summary"]["source_backed"] == 0
    assert payload["summary"]["needs_source"] == 2
    assert {row["crypto_portability"] for row in payload["records"]} == {"BLOCKED_NO_SOURCE"}
    assert all(row["decision"] == "WAIT_FOR_OPEN_SOURCE_OR_USER_EXPORT" for row in payload["records"])
    assert {row["source_status"] for row in payload["records"]} == {"CATALOG_METADATA_ONLY"}
    assert all("not executable Pine source" in row["source_explanation"] for row in payload["records"])
    assert all("export/paste" in row["source_next_step"] for row in payload["records"])


def test_publish_pine_research_kb_reconciles_catalog_match_with_source(tmp_path):
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    (source_dir / "luxy_ut_god_forecast.pine").write_text(
        """
//@version=6
indicator("Luxy UT GOD - UT-BOT Forecast", overlay=true)
ema = ta.ema(close, 21)
atr = ta.atr(14)
long = close > ema and volume > ta.sma(volume, 20)
plotshape(long)
alertcondition(long, "long")
""",
        encoding="utf-8",
    )
    html = tmp_path / "catalog.html"
    html.write_text(
        '<a href="/script/A47z5YCR-Luxy-UT-God-Mode-UT-Bot-Forecast-Signals-Zones-and-Risk/">',
        encoding="utf-8",
    )
    output = tmp_path / "kb.json"

    payload = publish_pine_research_kb(
        source_dir=source_dir,
        catalog_html_files=[html],
        output_path=output,
        include_defaults=False,
    )

    assert payload["summary"]["total"] == 1
    assert payload["summary"]["source_backed"] == 1
    assert payload["summary"]["catalog_only"] == 0
    assert payload["summary"]["reconciled_catalog_matches"] == 1
    record = payload["records"][0]
    assert record["script_id"] == "luxy_ut_god_forecast"
    assert record["discovery_status"] == "SOURCE_BACKED_CATALOG_MATCH"
    assert record["source_status"] == "SOURCE_BACKED_CATALOG_MATCH"
    assert "Pine source is present" in record["source_explanation"]
    assert record["catalog_urls"] == (
        "https://www.tradingview.com/script/A47z5YCR-Luxy-UT-God-Mode-UT-Bot-Forecast-Signals-Zones-and-Risk/",
    )
    assert record["next_action"] == "PORT_CAUSAL_FEATURES_AND_REPLAY"


def test_publish_pine_research_kb_attaches_extraction_manifest_provenance(tmp_path):
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    source = source_dir / "advanced_fibonacci_golden_zone_hexatrades.pine"
    source.write_text(
        """
//@version=6
indicator("Advanced Fibonacci Golden Zone [HexaTrades]", overlay=true)
pivot = ta.pivothigh(high, 15, 5)
golden = close > ta.ema(close, 20) and not na(pivot)
alertcondition(golden, "bullish golden zone")
""",
        encoding="utf-8",
    )
    manifest = tmp_path / "extraction_manifest.jsonl"
    manifest.write_text(
        json.dumps({
            "started_at": "2026-07-18T14:00:00Z",
            "status": "extracted",
            "title": "Advanced Fibonacci Golden Zone [HexaTrades]",
            "url": "https://www.tradingview.com/script/gmmtYKS3-Advanced-Fibonacci-Golden-Zone-HexaTrades/",
            "output": str(source),
            "source_lines": 7,
            "source_sha256": "abc123",
            "priority_score": 47,
        })
        + "\n"
        + json.dumps({
            "started_at": "2026-07-18T14:01:00Z",
            "status": "blocked_missing_source_tab",
            "title": "Protected Script",
            "url": "https://www.tradingview.com/script/LOCKED-Protected/",
        })
        + "\n",
        encoding="utf-8",
    )

    payload = publish_pine_research_kb(
        source_dir=source_dir,
        extraction_manifest_files=[manifest],
        output_path=tmp_path / "kb.json",
        include_defaults=False,
    )

    assert payload["source_extraction"]["attempted"] == 2
    assert payload["source_extraction"]["extracted"] == 1
    assert payload["source_extraction"]["blocked"] == 1
    assert payload["source_extraction"]["source_tab_failures"] == 1
    assert payload["coverage_audit"]["source_tab_failures"] == 1
    assert payload["coverage_audit"]["deduped_source_exports"] == 0
    record = payload["records"][0]
    assert record["url"] == (
        "https://www.tradingview.com/script/gmmtYKS3-Advanced-Fibonacci-Golden-Zone-HexaTrades/"
    )
    assert record["source_status"] == "SOURCE_BACKED_CATALOG_MATCH"
    assert record["source_origin"] == "tradingview_open_source_browser"
    assert record["source_extraction"]["extracted_at"] == "2026-07-18T14:00:00Z"


def test_load_and_summarize_pine_extraction_manifest(tmp_path):
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        "\n".join([
            json.dumps({
                "started_at": "2026-07-18T13:00:00Z",
                "status": "extracted",
                "url": "https://in.tradingview.com/script/AAAA1111-Open-Source/",
                "output": "/tmp/open_source.pine",
                "source_lines": "12",
            }),
            "not-json",
            json.dumps({
                "started_at": "2026-07-18T13:05:00Z",
                "status": "error",
                "error": "browser stopped",
            }),
        ]),
        encoding="utf-8",
    )

    entries = load_pine_extraction_manifest([manifest])
    summary = summarize_extraction_manifest(entries)

    assert len(entries) == 2
    assert entries[0]["url"] == "https://www.tradingview.com/script/AAAA1111-Open-Source/"
    assert summary["attempted"] == 2
    assert summary["extracted"] == 1
    assert summary["errors"] == 1
    assert summary["retryable_errors"] == 1


def test_summarize_pine_extraction_manifest_uses_latest_url_status(tmp_path):
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        "\n".join([
            json.dumps({
                "started_at": "2026-07-18T13:01:00Z",
                "status": "error",
                "url": "https://www.tradingview.com/script/BBBB2222-Browser-Failed/",
                "error": "source tab timed out",
            }),
            json.dumps({
                "started_at": "2026-07-18T13:05:00Z",
                "status": "extracted",
                "url": "https://www.tradingview.com/script/BBBB2222-Browser-Failed/",
                "output": "/tmp/browser_failed_retry.pine",
                "source_lines": 120,
            }),
        ]),
        encoding="utf-8",
    )

    entries = load_pine_extraction_manifest([manifest])
    summary = summarize_extraction_manifest(entries)
    audit = build_pine_coverage_audit(
        [],
        source_extraction=summary,
        discovered_total=1,
    )

    assert summary["attempted"] == 2
    assert summary["current_attempts"] == 1
    assert summary["extracted"] == 1
    assert summary["raw_extracted"] == 1
    assert summary["errors"] == 0
    assert summary["retryable_errors"] == 0
    assert summary["status_counts"] == {"extracted": 1}
    assert summary["raw_status_counts"] == {"error": 1, "extracted": 1}
    assert audit["retryable_browser_errors"] == 0
    assert all(gap["bucket"] != "RETRYABLE_BROWSER_ERRORS" for gap in audit["gaps"])


def test_pine_coverage_auditor_explains_missing_discovery_backlog(tmp_path):
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    (source_dir / "open_breakout.pine").write_text(
        """
//@version=6
strategy("Open Breakout", overlay=true)
long = close > ta.highest(high[1], 20)
if long
    strategy.entry("L", strategy.long)
""",
        encoding="utf-8",
    )
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        "\n".join([
            json.dumps({
                "started_at": "2026-07-18T13:00:00Z",
                "status": "extracted",
                "url": "https://www.tradingview.com/script/AAAA1111-Open-Breakout/",
                "output": str(source_dir / "open_breakout.pine"),
            }),
            json.dumps({
                "started_at": "2026-07-18T13:01:00Z",
                "status": "error",
                "url": "https://www.tradingview.com/script/BBBB2222-Browser-Failed/",
            }),
            json.dumps({
                "started_at": "2026-07-18T13:02:00Z",
                "status": "failed_no_source_after_click",
                "url": "https://www.tradingview.com/script/CCCC3333-No-Source/",
            }),
        ]),
        encoding="utf-8",
    )

    payload = publish_pine_research_kb(
        source_dir=source_dir,
        extraction_manifest_files=[manifest],
        output_path=tmp_path / "kb.json",
        include_defaults=False,
        discovered_total=10,
    )

    audit = payload["coverage_audit"]
    assert audit["discovered_records"] == 10
    assert audit["visible_records"] == 1
    assert audit["unloaded_catalog_backlog"] == 9
    assert audit["retryable_browser_errors"] == 1
    assert audit["source_tab_failures"] == 1
    assert audit["unresolved_source_backlog"] == 11
    assert {gap["bucket"] for gap in audit["gaps"]} >= {
        "CATALOG_BACKLOG_NOT_LOADED",
        "RETRYABLE_BROWSER_ERRORS",
        "SOURCE_TAB_NOT_CAPTURED",
    }
    assert audit["can_trade"] is False
    assert audit["can_promote"] is False


def test_pine_coverage_auditor_marks_negative_completed_cells():
    audit = build_pine_coverage_audit(
        [
            {
                "script_id": "x",
                "title": "X",
                "url": "user_supplied",
                "source_available": True,
                "crypto_portability": "PORTABLE",
                "backtests": [{"timeframe": "5m", "status": "queued"}],
            }
        ],
        source_extraction={
            "attempted": 1,
            "extracted": 1,
            "errors": 0,
            "status_counts": {"extracted": 1},
        },
        backtest_evidence={
            "completed_cells": 2,
            "positive_completed_cells": 0,
            "status_counts": {"failed": 2},
        },
    )

    assert audit["completed_backtest_cells"] == 2
    assert audit["failed_completed_cells"] == 2
    assert any(gap["bucket"] == "COMPLETED_BUT_NEGATIVE_AFTER_COST" for gap in audit["gaps"])


def test_pine_research_priority_queue_prefers_source_backed_port(tmp_path):
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    (source_dir / "orderflow_delta_breakout.pine").write_text(
        """
//@version=6
strategy("Order Flow Delta Breakout", overlay=true)
delta = volume - ta.sma(volume, 20)
atr = ta.atr(14)
long = close > ta.highest(high[1], 12) and delta > 0
if long
    strategy.entry("L", strategy.long)
    strategy.exit("LX", "L", stop=close - atr, limit=close + atr * 2)
""",
        encoding="utf-8",
    )
    html = tmp_path / "catalog.html"
    html.write_text(
        '<a href="/script/ODpXONLY-Liquidity-Sweep-Detector-Pro/">',
        encoding="utf-8",
    )

    payload = publish_pine_research_kb(
        source_dir=source_dir,
        catalog_html_files=[html],
        output_path=tmp_path / "kb.json",
        include_defaults=False,
    )

    assert payload["priorities"][0]["script_id"] == "orderflow_delta_breakout"
    assert payload["priorities"][0]["next_action"] == "PORT_CAUSAL_FEATURES_AND_REPLAY"
    assert payload["priorities"][0]["source_status"] == "USER_SUPPLIED_SOURCE"
    assert payload["summary"]["source_requests"] == 1


def test_pine_research_cli_publishes_artifact(tmp_path, capsys):
    source = tmp_path / "ut_bot.txt"
    source.write_text(
        """
//@version=6
indicator("UT Bot Clone", overlay=true)
trail = ta.ema(close, 10) - ta.atr(14)
plotshape(close > trail)
alertcondition(close > trail, "long")
""",
        encoding="utf-8",
    )
    output = tmp_path / "kb.json"

    assert main([
        str(source),
        "--source-dir",
        str(tmp_path / "missing"),
        "--output",
        str(output),
        "--no-defaults",
    ]) == 0

    printed = capsys.readouterr().out
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert "pine research KB published" in printed
    assert payload["summary"]["total"] == 1
    assert payload["records"][0]["source_available"] is True


def test_pine_research_cli_imports_catalog_html(tmp_path):
    html = tmp_path / "catalog.html"
    html.write_text(
        '<a href="/script/eqD9PKX8-Anchored-VWAP-Engine-Quantum-Algo/">',
        encoding="utf-8",
    )
    output = tmp_path / "kb.json"

    assert main([
        "--source-dir",
        str(tmp_path / "missing"),
        "--catalog-html",
        str(html),
        "--output",
        str(output),
        "--no-defaults",
    ]) == 0

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["summary"]["catalog_only"] == 1
    assert payload["records"][0]["title"] == "Anchored Vwap Engine Quantum Algo"
    assert payload["records"][0]["source_status"] == "CATALOG_METADATA_ONLY"
