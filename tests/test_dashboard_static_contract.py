from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "src/vnedge/dashboard/static/index.html"
QUANTIFIED = ROOT / "src/vnedge/dashboard/static/quantified_strategy_lab.html"
APP = ROOT / "src/vnedge/dashboard/app.py"


def _index() -> str:
    return INDEX.read_text()


def _quantified() -> str:
    return QUANTIFIED.read_text()


def test_promote_view_has_joined_operator_lifecycle_console():
    html = _index()
    assert "Operator Lifecycle Console" in html
    assert 'id="lifecycleConsole"' in html
    assert (
        "activation, route doctor, cadence, trade profile, performance, survival, "
        "governor, and causality"
    ) in html
    assert "Planner inputs are still read-only" in html


def test_promote_view_has_read_only_operator_action_queue():
    html = _index()
    assert "Operator Action Queue" in html
    assert 'id="operatorActionQueue"' in html
    assert "function collectLifecycleRows" in html
    assert "function renderOperatorActionQueue" in html
    assert "function pollOperatorActions" in html
    assert 'poll("/operator-actions"' in html
    assert "REPAIR_ROUTE" in html
    assert "REPAIR_PAPER_CONTRACT" in html
    assert "REVIEW_PAPER_CANDIDATE" in html
    assert "COLLECT_OUTCOMES" in html
    assert "evidence-ranked only; it cannot approve, promote, or trade" in html

def test_promote_view_has_paper_lane_root_cause_matrix():
    html = _index()
    assert "Paper Lane Root-Cause Matrix" in html
    assert 'id="paperLaneRootCause"' in html
    assert 'id="paperRootCauseMeta"' in html
    assert "function renderPaperLaneRootCause" in html
    assert "/paper-lane-root-cause" in html
    assert "route, cadence, sizing, entry, exit, and performance evidence" in html


def test_promote_view_has_paper_trade_entry_autopsy_panel():
    html = _index()
    assert "Paper Trade Entry Autopsy" in html
    assert 'id="paperTradeEntryAutopsy"' in html
    assert "/paper-trade-entry-autopsy" in html
    assert "function renderPaperTradeEntryAutopsy" in html
    assert "Stale entries" in html
    assert "missing ctx" in html


def test_promote_view_has_trade_analyzer_os_panel():
    html = _index()
    app = APP.read_text()
    assert "Trade Analyzer OS" in html
    assert 'id="tradeAnalyzerOS"' in html
    assert 'id="tradeAnalyzerMeta"' in html
    assert "function renderTradeAnalyzerOS" in html
    assert "function pollTradeAnalyzerOS" in html
    assert 'poll("/trade-analyzer-os"' in html
    assert "Giveback lanes" in html
    assert "Daily hold drift" in html
    assert '@app.get("/trade-analyzer-os")' in app


def test_promote_view_has_paper_contract_truth_surface():
    html = _index()
    app = APP.read_text()
    assert "Paper Contract Truth" in html
    assert 'id="paperContractTruth"' in html
    assert 'id="paperContractMeta"' in html
    assert "function renderPaperContractTruth" in html
    assert "function pollPaperContractTruth" in html
    assert 'poll("/paper-trade-contract-reconciler"' in html
    assert "REPAIR_PAPER_CONTRACT" in html
    assert "MINE_CLEAN_ALPHA" in html
    assert '@app.get("/paper-trade-contract-reconciler")' in app


def test_promote_view_has_paper_promotion_bridge_surface():
    html = _index()
    app = APP.read_text()
    assert "Paper Promotion Bridge" in html
    assert 'id="paperPromotionBridge"' in html
    assert 'id="paperPromotionBridgeMeta"' in html
    assert "function renderPaperPromotionBridge" in html
    assert "function pollPaperPromotionBridge" in html
    assert 'poll("/paper-promotion-bridge"' in html
    assert "PAPER_REVIEW_READY" in html
    assert "MINE_CLEAN_ALPHA" in html
    assert "read-only · no auto-promotion" in html
    assert '@app.get("/paper-promotion-bridge")' in app


def test_dashboard_has_self_health_console_for_poll_and_ws_truth():
    html = _index()
    assert "Dashboard Self-Health" in html
    assert 'id="dashboardHealth"' in html
    assert "function recordPoll" in html
    assert "function renderDashboardHealth" in html
    assert "st.lastStatus" in html
    assert "wsHealth" in html
    assert "endpoint status, browser poll failures, and payload freshness" in html


def test_dashboard_has_agentic_research_os_supervisor_panel():
    html = _index()
    app = APP.read_text()
    assert "Agentic Research OS" in html
    assert 'id="agenticOs"' in html
    assert 'id="agenticOsMeta"' in html
    assert "function renderAgenticResearchOS" in html
    assert "function pollAgenticResearchOS" in html
    assert 'poll("/agentic-research-os"' in html
    assert '@app.get("/agentic-research-os")' in app
    assert "research-only supervisor; no trade authority" in html


def test_dashboard_scanner_tape_renders_trade_lifecycle_truth():
    html = _index()
    assert "trade_lifecycle" in html
    assert "final_why_no_trade" in html
    assert "TP ladder journal-only" in html


def test_dashboard_has_delta_loss_attribution_panel():
    html = _index()
    assert "Delta Loss Attribution" in html
    assert 'id="deltaAttribution"' in html
    assert 'id="deltaAttributionMeta"' in html
    assert "function renderDeltaAttribution" in html
    assert "function pollDeltaAttribution" in html
    assert 'poll("/delta-scalper"' in html
    assert "this is diagnosis, not optimization" in html
    assert "protected_from_threshold_selection" in html


def test_dashboard_renders_daily_signal_factory_lane_truth():
    html = _index()
    assert "function dailyFactoryView" in html
    assert "dailyFactoryChip(l.daily_factory)" in html
    assert "Daily factory" in html
    assert "force flat" in html
    assert "target hit" in html
    assert "day open" in html


def test_dashboard_panel_sections_are_balanced():
    html = _index()
    assert html.count("<section") == html.count("</section>")


def test_dashboard_build_identity_is_runtime_sourced():
    html = _index()
    assert "541e1af" not in html
    assert "(mock)" not in html
    assert 'id="aboutBuild"' in html
    assert 'id="footBuild"' in html
    assert 'id="topBuild"' in html
    assert "function renderBuildMeta" in html


def test_dashboard_has_external_repo_synthesis_panel():
    html = _index()
    assert "External Repo Synthesis" in html
    assert 'id="externalRepoSynthesis"' in html
    assert 'id="externalRepoMeta"' in html
    assert "function renderExternalRepoSynthesis" in html
    assert "/external-repo-synthesis" in html
    assert "Trade authority" in html


def test_dashboard_polled_endpoints_have_app_routes():
    html = _index()
    app = APP.read_text()
    polled: set[str] = set()
    for pattern in (
        r'poll\("([^"]+)"',
        r"poll\('([^']+)'",
        r'fetch\("([^"]+)"',
        r"fetch\('([^']+)'",
    ):
        polled.update(re.findall(pattern, html))
    routes = set(re.findall(r'@app\.(?:get|post|websocket)\("([^"]+)"', app))
    missing = sorted(path for path in polled if path.split("?")[0] not in routes)
    assert not missing


def test_dashboard_has_darwinian_agent_survival_panel():
    html = _index()
    assert "Darwinian Agent Survival" in html
    assert 'id="darwinianAgentSurvival"' in html
    assert "/darwinian-agent-survival" in html
    assert "function renderDarwinianAgentSurvival" in html
    assert "JANUS cohorts" in html


def test_dashboard_has_maker_quote_lifecycle_panel():
    html = _index()
    assert "Maker Quote Lifecycle" in html
    assert 'id="makerQuoteLifecycle"' in html
    assert "/maker-quote-lifecycle" in html
    assert "function renderMakerQuoteLifecycle" in html
    assert "taker fallback" in html


def test_journal_has_pnl_by_cohort_panel():
    html = _index()
    assert "P&amp;L by cohort" in html
    assert 'id="jCohort"' in html
    assert "cohort_pnl" in html            # render reads the summary field
    assert "Deliberate controls" not in html or "jcohcard" in html  # class present
    assert "jcohcard" in html and ".control" in html  # controls visually de-emphasised


def test_nav_links_to_the_quantified_strategy_lab_page():
    html = _index()
    # a real href (separate FileResponse page), not an SPA data-v view-switch
    assert 'id="navLab"' in html
    assert 'href="/quantified-strategy-lab"' in html
    assert "navLab" in html and "quantified-strategy-lab?token=" in html  # token carried


def test_quantified_lab_page_renders_complete_blueprint_proof_matrix():
    html = _quantified()
    app = APP.read_text()
    assert "Blueprint Proof Matrix" in html
    assert "Proof Result Arbiter" in html
    assert "/quantified-strategy-lab/blueprint-proof" in html
    assert "/quantified-strategy-lab/proof-arbiter" in html
    assert "/quantified-strategy-lab/pullback-proof" in html
    assert "proxy cells" in html
    assert "Paper Profile" in html
    assert '@app.get("/quantified-strategy-lab/blueprint-proof")' in app
    assert '@app.get("/quantified-strategy-lab/proof-arbiter")' in app


def test_dashboard_has_ctrl_k_command_palette():
    html = _index()
    # markup
    assert 'id="cmdk"' in html
    assert 'id="cmdkInput"' in html
    assert 'id="cmdkList"' in html
    assert 'role="dialog"' in html and 'aria-modal="true"' in html
    # a real command registry + open/close/run + fuzzy score
    assert "var CMDK=" in html
    assert "function cmdkOpen" in html and "function cmdkRender" in html
    assert "function cmdkScore" in html
    # Ctrl/Cmd-K opens it; reuses existing gotoView navigation
    assert 'toLowerCase()==="k"' in html
    assert "gotoView(" in html
