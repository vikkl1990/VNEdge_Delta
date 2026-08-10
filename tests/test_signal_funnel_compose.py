"""Signal-funnel deployment contracts."""

from pathlib import Path

import yaml


def compose_services() -> dict:
    return yaml.safe_load(Path("docker-compose.yml").read_text())["services"]


def test_event_leadlag_miner_refreshes_candidate_feed_on_interval():
    services = compose_services()
    service = services["event-leadlag-miner"]

    assert service["command"][:3] == ["python", "-m", "vnedge.research.event_leadlag_alpha"]
    assert "--interval-seconds" in service["command"]
    assert "./data:/app/data:ro" in service["volumes"]
    assert "./research/live_research:/app/research/live_research" in service["volumes"]


def test_dashboard_reads_pine_research_kb_from_host_artifact():
    service = compose_services()["multi-lane-shadow"]

    assert "./research/pine_scripts:/app/research/pine_scripts:ro" in service["volumes"]


def test_pine_backtest_evidence_refreshes_matrix_overlay():
    service = compose_services()["pine-backtest-evidence"]

    assert service["user"] == "${VNEDGE_CONTAINER_UID:-1000}:${VNEDGE_CONTAINER_GID:-1000}"
    assert service["command"][:3] == ["python", "-m", "vnedge.research.pine_backtest_evidence"]
    assert "--interval-seconds" in service["command"]
    assert "--report-dir" in service["command"]
    assert "./research/pine_scripts:/app/research/pine_scripts" in service["volumes"]
    assert "./research/live_research:/app/research/live_research" in service["volumes"]
    assert set(service["depends_on"]) >= {
        "daily-scalper-pack",
        "daily-scalper-cadence",
        "alpha-distillation",
        "orderflow-footprint-miner",
        "event-leadlag-miner",
        "candidate-replay-executor",
        "fee-wall-forensics",
        "pine-alpha-distiller",
    }


def test_pine_alpha_distiller_refreshes_source_intention_artifact():
    service = compose_services()["pine-alpha-distiller"]

    assert service["user"] == "${VNEDGE_CONTAINER_UID:-1000}:${VNEDGE_CONTAINER_GID:-1000}"
    assert service["command"][:3] == ["python", "-m", "vnedge.research.pine_alpha_distiller"]
    assert "--interval-seconds" in service["command"]
    assert "--source-dir" in service["command"]
    assert "research/pine_scripts/sources" in service["command"]
    assert "--out" in service["command"]
    assert "research/live_research/pine_alpha_distiller_latest.json" in service["command"]
    assert "./research/pine_scripts:/app/research/pine_scripts:ro" in service["volumes"]
    assert "./research/live_research:/app/research/live_research" in service["volumes"]


def test_pine_edge_uplift_agent_recycles_failed_evidence_only():
    service = compose_services()["pine-edge-uplift-agent"]

    assert service["user"] == "${VNEDGE_CONTAINER_UID:-1000}:${VNEDGE_CONTAINER_GID:-1000}"
    assert service["command"][:3] == ["python", "-m", "vnedge.research.pine_edge_uplift_agent"]
    assert "--interval-seconds" in service["command"]
    assert "--distiller" in service["command"]
    assert "--out" in service["command"]
    assert "research/live_research/pine_edge_uplift_agent_latest.json" in service["command"]
    assert "./research/pine_scripts:/app/research/pine_scripts:ro" in service["volumes"]
    assert "./research/live_research:/app/research/live_research" in service["volumes"]
    assert service["depends_on"] == ["pine-backtest-evidence"]


def test_edge_uplift_executor_materializes_agent_tasks():
    service = compose_services()["edge-uplift-executor"]

    assert service["user"] == "${VNEDGE_CONTAINER_UID:-1000}:${VNEDGE_CONTAINER_GID:-1000}"
    assert service["command"][:3] == ["python", "-m", "vnedge.research.edge_uplift_executor"]
    assert "--interval-seconds" in service["command"]
    assert "--uplift" in service["command"]
    assert "research/live_research/pine_edge_uplift_agent_latest.json" in service["command"]
    assert "--scanner" in service["command"]
    assert "research/live_research/scanner_tournament_latest.json" in service["command"]
    assert "--fee-wall" in service["command"]
    assert "research/live_research/fee_wall_forensics_latest.json" in service["command"]
    assert "--out" in service["command"]
    assert "research/live_research/edge_uplift_experiments_latest.json" in service["command"]
    assert "./research/live_research:/app/research/live_research" in service["volumes"]
    assert set(service["depends_on"]) == {
        "pine-edge-uplift-agent",
        "scanner-tournament",
        "fee-wall-forensics",
        "scanner-backtest-uplift",
    }


def test_vnedge_algo_ml_pro_contract_matrix_refreshes_delta_replay():
    service = compose_services()["vnedge-algo-ml-pro-contract-matrix"]

    assert service["command"][:3] == [
        "python",
        "-m",
        "vnedge.research.vnedge_algo_ml_pro_contract_matrix",
    ]
    assert "--interval-seconds" in service["command"]
    assert "--sizing-mode" in service["command"]
    assert "delta_contract_risk" in service["command"]
    assert "--delta-live-product-spec" in service["command"]
    assert "--acknowledge-high-leverage" in service["command"]
    assert "research/live_research/vnedge_algo_ml_pro_contract_matrix_latest.json" in service["command"]
    assert "./data:/app/data:ro" in service["volumes"]
    assert "./research/live_research:/app/research/live_research" in service["volumes"]
    assert service["depends_on"] == ["pine-backtest-evidence"]


def test_paper_route_doctor_explains_missing_journals():
    service = compose_services()["paper-route-doctor"]

    assert service["command"][:3] == [
        "python",
        "-m",
        "vnedge.research.paper_route_doctor",
    ]
    assert "--interval-seconds" in service["command"]
    assert "--stale-after-hours" in service["command"]
    assert "./logs:/app/logs:ro" in service["volumes"]
    assert "./research/live_research:/app/research/live_research" in service["volumes"]
    assert service["depends_on"] == ["paper-lane-activation"]


def test_promotion_review_runbook_publishes_red_team_operator_packet():
    service = compose_services()["promotion-review-runbook"]

    assert service["command"][:3] == [
        "python",
        "-m",
        "vnedge.research.promotion_review_runbook",
    ]
    assert "--interval-seconds" in service["command"]
    assert "${PROMOTION_REVIEW_RUNBOOK_INTERVAL_SECONDS:-300}" in service["command"]
    assert "--out" in service["command"]
    assert "research/live_research/promotion_review_runbook_latest.json" in service["command"]
    assert "--runbook-feed" in service["command"]
    assert "research/live_research/promotion_review_runbook_feed.jsonl" in service["command"]
    assert "--red-team-out" in service["command"]
    assert "research/live_research/promotion_red_team_latest.json" in service["command"]
    assert "--print" in service["command"]
    assert "./research/live_research:/app/research/live_research" in service["volumes"]
    assert "./research/paper_trials:/app/research/paper_trials:ro" in service["volumes"]
    assert "./research/judgments:/app/research/judgments:ro" in service["volumes"]
    assert service["depends_on"] == ["research-loop"]


def test_paper_lane_cadence_monitors_live_evaluation_cadence():
    service = compose_services()["paper-lane-cadence"]

    assert service["command"][:3] == [
        "python",
        "-m",
        "vnedge.research.paper_lane_cadence",
    ]
    assert "--interval-seconds" in service["command"]
    assert "--grace-multiplier" in service["command"]
    assert "--min-eval-sla-seconds" in service["command"]
    assert "./logs:/app/logs:ro" in service["volumes"]
    assert "./research/live_research:/app/research/live_research" in service["volumes"]
    assert service["depends_on"] == ["paper-lane-activation", "paper-route-doctor"]


def test_operator_actions_publishes_joined_action_feed():
    service = compose_services()["operator-actions"]

    assert service["command"][:3] == [
        "python",
        "-m",
        "vnedge.research.operator_actions",
    ]
    assert "--interval-seconds" in service["command"]
    assert "${OPERATOR_ACTIONS_INTERVAL_SECONDS:-60}" in service["command"]
    assert "--exit-autopsy" in service["command"]
    assert "research/live_research/paper_trade_exit_autopsy_latest.json" in service["command"]
    assert "--contract-reconciler" in service["command"]
    assert "research/live_research/paper_trade_contract_reconciler_latest.json" in service["command"]
    assert "research/live_research/operator_actions_latest.json" in service["command"]
    assert "research/live_research/operator_actions_feed.jsonl" in service["command"]
    assert "--print" in service["command"]
    assert "./research/live_research:/app/research/live_research" in service["volumes"]
    assert set(service["depends_on"]) == {
        "paper-lane-activation",
        "paper-route-doctor",
        "paper-lane-cadence",
        "paper-lane-performance",
        "paper-trade-exit-autopsy",
        "lane-firing-causality",
    }


def test_paper_promotion_bridge_publishes_joined_review_feed():
    service = compose_services()["paper-promotion-bridge"]

    assert service["command"][:3] == [
        "python",
        "-m",
        "vnedge.research.paper_promotion_bridge",
    ]
    assert "--interval-seconds" in service["command"]
    assert "${PAPER_PROMOTION_BRIDGE_INTERVAL_SECONDS:-60}" in service["command"]
    assert "--readiness" in service["command"]
    assert "research/live_research/lane_promotion_readiness_latest.json" in service["command"]
    assert "--performance" in service["command"]
    assert "research/live_research/paper_lane_performance_latest.json" in service["command"]
    assert "--contract" in service["command"]
    assert "research/live_research/paper_trade_contract_reconciler_latest.json" in service["command"]
    assert "--maker-quote" in service["command"]
    assert "research/live_research/maker_quote_lifecycle_latest.json" in service["command"]
    assert "--actions" in service["command"]
    assert "research/live_research/operator_actions_latest.json" in service["command"]
    assert "research/live_research/paper_promotion_bridge_latest.json" in service["command"]
    assert "research/live_research/paper_promotion_bridge_feed.jsonl" in service["command"]
    assert "--print" in service["command"]
    assert "./research/live_research:/app/research/live_research" in service["volumes"]
    assert set(service["depends_on"]) == {
        "lane-promotion-readiness",
        "paper-lane-performance",
        "paper-trade-contract-reconciler",
        "maker-quote-lifecycle",
        "operator-actions",
    }


def test_paper_lane_root_cause_publishes_primary_blocker_feed():
    service = compose_services()["paper-lane-root-cause"]

    assert service["command"][:3] == [
        "python",
        "-m",
        "vnedge.research.paper_lane_root_cause",
    ]
    assert "--interval-seconds" in service["command"]
    assert "${PAPER_LANE_ROOT_CAUSE_INTERVAL_SECONDS:-60}" in service["command"]
    assert "--entry-autopsy" in service["command"]
    assert "research/live_research/paper_trade_entry_autopsy_latest.json" in service["command"]
    assert "--exit-autopsy" in service["command"]
    assert "research/live_research/paper_trade_exit_autopsy_latest.json" in service["command"]
    assert "research/live_research/paper_lane_root_cause_latest.json" in service["command"]
    assert "research/live_research/paper_lane_root_cause_feed.jsonl" in service["command"]
    assert "--print" in service["command"]
    assert "./research/live_research:/app/research/live_research" in service["volumes"]
    assert set(service["depends_on"]) == {
        "paper-lane-activation",
        "paper-route-doctor",
        "paper-lane-cadence",
        "paper-lane-performance",
        "paper-trade-exit-autopsy",
        "lane-survival",
        "paper-lane-governor",
        "lane-firing-causality",
    }


def test_paper_trade_exit_autopsy_publishes_exit_quality_evidence():
    service = compose_services()["paper-trade-exit-autopsy"]

    assert service["command"][:3] == [
        "python",
        "-m",
        "vnedge.research.paper_trade_exit_autopsy",
    ]
    assert "--interval-seconds" in service["command"]
    assert "--min-closed-trades" in service["command"]
    assert "--min-profit-factor" in service["command"]
    assert "--min-avg-net-bps" in service["command"]
    assert "--fee-wall-bps" in service["command"]
    assert "./logs:/app/logs:ro" in service["volumes"]
    assert "./research/live_research:/app/research/live_research" in service["volumes"]
    assert service["depends_on"] == ["paper-lane-performance"]


def test_trade_analyzer_os_publishes_joined_trade_verdict():
    service = compose_services()["trade-analyzer-os"]

    assert service["command"][:3] == [
        "python",
        "-m",
        "vnedge.research.trade_analyzer_os",
    ]
    assert "--interval-seconds" in service["command"]
    assert "${TRADE_ANALYZER_OS_INTERVAL_SECONDS:-60}" in service["command"]
    assert "--out" in service["command"]
    assert "research/live_research/trade_analyzer_os_latest.json" in service["command"]
    assert "--feed" in service["command"]
    assert "research/live_research/trade_analyzer_os_feed.jsonl" in service["command"]
    assert "--giveback-arm-bps" in service["command"]
    assert "--giveback-min-bps" in service["command"]
    assert "./logs:/app/logs:ro" in service["volumes"]
    assert "./research/live_research:/app/research/live_research" in service["volumes"]
    assert service["depends_on"] == [
        "paper-trade-entry-autopsy",
        "paper-trade-exit-autopsy",
    ]


def test_paper_trade_contract_reconciler_classifies_runtime_drift_vs_alpha():
    service = compose_services()["paper-trade-contract-reconciler"]

    assert service["command"][:3] == [
        "python",
        "-m",
        "vnedge.research.paper_trade_contract_reconciler",
    ]
    assert "--interval-seconds" in service["command"]
    assert "--min-expected-net-bps" in service["command"]
    assert "--max-fee-bps" in service["command"]
    assert "--max-quantity-drift-pct" in service["command"]
    assert "--max-notional-drift-pct" in service["command"]
    assert "./logs:/app/logs:ro" in service["volumes"]
    assert "./research/live_research:/app/research/live_research" in service["volumes"]
    assert set(service["depends_on"]) == {
        "paper-lane-performance",
        "paper-trade-exit-autopsy",
    }


def test_quantified_blueprint_proof_publishes_complete_port_matrix():
    service = compose_services()["quantified-blueprint-proof"]

    assert service["command"][:3] == [
        "python",
        "-m",
        "vnedge.research.quantified_blueprint_proof",
    ]
    assert "--interval-seconds" in service["command"]
    assert "${QUANTIFIED_BLUEPRINT_PROOF_INTERVAL_SECONDS:-300}" in service["command"]
    assert "--seed-jobs" in service["command"]
    assert "--jobs-dir" in service["command"]
    assert "${AGENT_GATEWAY_JOBS_DIR:-logs/agent_gateway/jobs}" in service["command"]
    assert "--out" in service["command"]
    assert "research/live_research/quantified_blueprint_proof_latest.json" in service["command"]
    assert "--feed" in service["command"]
    assert "research/live_research/quantified_blueprint_proof_feed.jsonl" in service["command"]
    assert "./logs:/app/logs" in service["volumes"]
    assert "./research/live_research:/app/research/live_research" in service["volumes"]
    assert set(service["depends_on"]) == {
        "agent-job-runner",
        "quantified-pullback-reversion-proof",
    }


def test_quantified_proof_result_arbiter_publishes_operator_actions():
    service = compose_services()["quantified-proof-result-arbiter"]

    assert service["command"][:3] == [
        "python",
        "-m",
        "vnedge.research.quantified_proof_result_arbiter",
    ]
    assert "--interval-seconds" in service["command"]
    assert "${QUANTIFIED_PROOF_ARBITER_INTERVAL_SECONDS:-300}" in service["command"]
    assert "--proof" in service["command"]
    assert "research/live_research/quantified_blueprint_proof_latest.json" in service["command"]
    assert "--out" in service["command"]
    assert "research/live_research/quantified_proof_result_arbiter_latest.json" in service["command"]
    assert "--feed" in service["command"]
    assert "research/live_research/quantified_proof_result_arbiter_feed.jsonl" in service["command"]
    assert "./research/live_research:/app/research/live_research" in service["volumes"]
    assert service["depends_on"] == ["quantified-blueprint-proof"]


def test_maker_quote_lifecycle_publishes_execution_path_truth():
    service = compose_services()["maker-quote-lifecycle"]

    assert service["command"][:3] == [
        "python",
        "-m",
        "vnedge.research.maker_quote_lifecycle",
    ]
    assert "--interval-seconds" in service["command"]
    assert "--performance" in service["command"]
    assert "research/live_research/paper_lane_performance_latest.json" in service["command"]
    assert "--exit-autopsy" in service["command"]
    assert "research/live_research/paper_trade_exit_autopsy_latest.json" in service["command"]
    assert "--min-maker-attempts" in service["command"]
    assert "--min-maker-fill-rate-pct" in service["command"]
    assert "--min-taker-net-edge-bps" in service["command"]
    assert "--min-taker-cost-coverage" in service["command"]
    assert "./logs:/app/logs:ro" in service["volumes"]
    assert "./research/live_research:/app/research/live_research" in service["volumes"]
    assert set(service["depends_on"]) == {
        "paper-lane-performance",
        "paper-trade-exit-autopsy",
    }


def test_paper_trade_entry_autopsy_publishes_entry_quality_evidence():
    service = compose_services()["paper-trade-entry-autopsy"]

    assert service["command"][:3] == [
        "python",
        "-m",
        "vnedge.research.paper_trade_entry_autopsy",
    ]
    assert "--interval-seconds" in service["command"]
    assert "--min-closed-trades" in service["command"]
    assert "--min-profit-factor" in service["command"]
    assert "--min-expected-edge-bps" in service["command"]
    assert "--max-signal-age-seconds" in service["command"]
    assert "--max-signal-age-bars" in service["command"]
    assert "./logs:/app/logs:ro" in service["volumes"]
    assert "./research/live_research:/app/research/live_research" in service["volumes"]
    assert service["depends_on"] == ["paper-lane-performance"]


def test_lane_survival_reconciles_paper_truth_boards():
    service = compose_services()["lane-survival"]

    assert service["command"][:3] == [
        "python",
        "-m",
        "vnedge.research.lane_survival",
    ]
    assert "--interval-seconds" in service["command"]
    assert "--min-closed-trades" in service["command"]
    assert "--min-profit-factor" in service["command"]
    assert "--min-avg-net-bps" in service["command"]
    assert "./research/live_research:/app/research/live_research" in service["volumes"]
    # Lean-by-default (2026-08-02): paper-route-doctor / paper-lane-cadence moved
    # to the `research` profile. lane-survival reads their output from the shared
    # research volume (stale-OK), so it no longer GATES on them — only on the two
    # core paper-lane services. Detangling is what keeps the core startable
    # without pulling the whole research cluster in.
    assert set(service["depends_on"]) == {
        "paper-lane-activation",
        "paper-lane-performance",
    }


def test_paper_lane_governor_publishes_roster_recommendations():
    service = compose_services()["paper-lane-governor"]

    assert service["command"][:3] == [
        "python",
        "-m",
        "vnedge.research.paper_lane_governor",
    ]
    assert "--interval-seconds" in service["command"]
    assert "--min-closed-trades" in service["command"]
    assert "--min-profit-factor" in service["command"]
    assert "--min-avg-net-bps" in service["command"]
    assert "--demote-after-negative-closed" in service["command"]
    assert "--max-paper-roster" in service["command"]
    assert "--max-tournament-lanes" in service["command"]
    assert "./research/live_research:/app/research/live_research" in service["volumes"]
    assert service["depends_on"] == ["lane-survival"]


def test_paper_roster_drift_reconciles_governor_with_runtime_reality():
    service = compose_services()["paper-roster-drift"]

    assert service["command"][:3] == [
        "python",
        "-m",
        "vnedge.research.paper_roster_drift",
    ]
    assert "--interval-seconds" in service["command"]
    assert "--governor" in service["command"]
    assert "research/live_research/paper_lane_governor_latest.json" in service["command"]
    assert "--scanner" in service["command"]
    assert "research/live_research/realtime_scanner_latest.json" in service["command"]
    assert "--activation" in service["command"]
    assert "research/live_research/paper_lane_activation_latest.json" in service["command"]
    assert "--out" in service["command"]
    assert "research/live_research/paper_roster_drift_latest.json" in service["command"]
    assert "--feed" in service["command"]
    assert "research/live_research/paper_roster_drift_feed.jsonl" in service["command"]
    assert "./research/live_research:/app/research/live_research" in service["volumes"]
    assert set(service["depends_on"]) == {
        "paper-lane-governor",
        "realtime-scanner",
        "paper-lane-activation",
    }


def test_paper_lane_activation_publishes_server_side_profile_ack():
    service = compose_services()["paper-lane-activation"]

    assert service["command"][:3] == [
        "python",
        "-m",
        "vnedge.research.paper_lane_activation",
    ]
    assert "--requested-margin-usd" in service["command"]
    assert "--requested-leverage" in service["command"]
    assert "--live-margin-usd" in service["command"]
    assert "--live-leverage" in service["command"]
    assert "--high-leverage-ack" in service["command"]
    assert "${PAPER_LANE_ACTIVATION_HIGH_LEVERAGE_ACK:-0}" in service["command"]


def test_scanner_backtest_uplift_mines_matrix_and_tournament_failures():
    service = compose_services()["scanner-backtest-uplift"]

    assert service["command"][:3] == [
        "python",
        "-m",
        "vnedge.research.scanner_backtest_uplift",
    ]
    assert "--interval-seconds" in service["command"]
    assert "research/live_research/vnedge_algo_ml_pro_contract_matrix_latest.json" in service["command"]
    assert "research/live_research/scanner_tournament_latest.json" in service["command"]
    assert "research/live_research/scanner_backtest_uplift_latest.json" in service["command"]
    assert "./research/live_research:/app/research/live_research" in service["volumes"]
    assert set(service["depends_on"]) == {
        "vnedge-algo-ml-pro-contract-matrix",
        "scanner-tournament",
    }


def test_fee_wall_paper_probe_bridge_publishes_durable_probe_manifest():
    service = compose_services()["fee-wall-paper-probe-bridge"]

    assert service["command"][:3] == [
        "python",
        "-m",
        "vnedge.research.fee_wall_paper_probe_bridge",
    ]
    assert "--interval-seconds" in service["command"]
    assert "research/live_research/fee_wall_forensics_latest.json" in service["command"]
    assert "research/live_research/fee_wall_paper_probes.json" in service["command"]
    assert "research/live_research/fee_wall_paper_probes_feed.jsonl" in service["command"]
    assert "--min-routed" in service["command"]
    assert "--min-avg-net-bps" in service["command"]
    assert "--min-profit-factor" in service["command"]
    assert "./research/live_research:/app/research/live_research" in service["volumes"]
    assert service["depends_on"] == ["fee-wall-forensics"]


def test_fee_wall_forensics_sweeps_runtime_sniper_roster_by_default():
    service = compose_services()["fee-wall-forensics"]
    command = service["command"]

    assert "--strategies" in command
    strategy_arg = command[command.index("--strategies") + 1]
    assert "FEE_WALL_FORENSICS_STRATEGIES" in strategy_arg
    assert "context_scalper_v2" in strategy_arg
    assert "quantified_fee_wall_sniper_v1" in strategy_arg


def test_fee_wall_probe_actuals_joins_probe_manifest_to_paper_outcomes():
    service = compose_services()["fee-wall-probe-actuals"]

    assert service["command"][:3] == [
        "python",
        "-m",
        "vnedge.research.fee_wall_probe_actuals",
    ]
    assert "--manifest" in service["command"]
    assert "research/live_research/fee_wall_paper_probes.json" in service["command"]
    assert "--performance" in service["command"]
    assert "research/live_research/paper_lane_performance_latest.json" in service["command"]
    assert "--route-doctor" in service["command"]
    assert "research/live_research/paper_route_doctor_latest.json" in service["command"]
    assert "research/live_research/fee_wall_probe_actuals_latest.json" in service["command"]
    assert "research/live_research/fee_wall_probe_actuals_feed.jsonl" in service["command"]
    assert "./research/live_research:/app/research/live_research" in service["volumes"]
    assert set(service["depends_on"]) == {
        "fee-wall-paper-probe-bridge",
        "paper-lane-performance",
        "paper-route-doctor",
    }


def test_alpha_arena_lite_publishes_durable_scanner_scorecards():
    service = compose_services()["alpha-arena-lite"]

    assert service["command"][:3] == [
        "python",
        "-m",
        "vnedge.research.alpha_arena_lite",
    ]
    assert "--interval-seconds" in service["command"]
    assert "research/live_research/scanner_backtest_uplift_latest.json" in service["command"]
    assert "research/live_research/scanner_tournament_latest.json" in service["command"]
    assert "research/live_research/fee_wall_forensics_latest.json" in service["command"]
    assert "research/live_research/alpha_arena_lite_latest.json" in service["command"]
    assert "${QUANT_OS_AGENT_GATEWAY_DIR:-logs/agent_gateway/quant_os}" in service["command"]
    assert "./logs/agent_gateway:/app/logs/agent_gateway" in service["volumes"]
    assert "./research/live_research:/app/research/live_research" in service["volumes"]
    assert set(service["depends_on"]) == {
        "scanner-backtest-uplift",
        "scanner-tournament",
        "fee-wall-forensics",
    }


def test_quant_loop_governance_publishes_loop_readiness():
    service = compose_services()["quant-loop-governance"]

    assert service["user"] == "${VNEDGE_CONTAINER_UID:-1000}:${VNEDGE_CONTAINER_GID:-1000}"
    assert service["command"][:3] == [
        "python",
        "-m",
        "vnedge.research.quant_loop_governance",
    ]
    assert "--interval-seconds" in service["command"]
    assert "${QUANT_LOOP_GOVERNANCE_INTERVAL_SECONDS:-1800}" in service["command"]
    assert "governance/loop_gates.yaml" in service["command"]
    assert "research/quant_loop_state.json" in service["command"]
    assert "research/live_research/alpha_arena_lite_latest.json" in service["command"]
    assert "research/live_research/scanner_backtest_uplift_latest.json" in service["command"]
    assert "research/live_research/scanner_tournament_progress.json" in service["command"]
    assert "logs/agent_gateway/quant_os/snapshot.json" in service["command"]
    assert "research/live_research/quant_loop_governance_latest.json" in service["command"]
    assert "research/live_research/quant_loop_run_log.jsonl" in service["command"]
    assert "./governance:/app/governance:ro" in service["volumes"]
    assert "./logs/agent_gateway:/app/logs/agent_gateway:ro" in service["volumes"]
    assert "./research/quant_loop_state.json:/app/research/quant_loop_state.json:ro" in service["volumes"]
    assert "./research/live_research:/app/research/live_research" in service["volumes"]
    assert set(service["depends_on"]) == {
        "alpha-arena-lite",
        "scanner-backtest-uplift",
        "scanner-tournament",
    }


def test_evidence_index_publisher_reconciles_research_artifacts():
    service = compose_services()["evidence-index-publisher"]

    assert service["user"] == "${VNEDGE_CONTAINER_UID:-1000}:${VNEDGE_CONTAINER_GID:-1000}"
    assert service["command"][:3] == [
        "python",
        "-m",
        "vnedge.research.evidence_store",
    ]
    assert "--interval-seconds" in service["command"]
    assert "${EVIDENCE_INDEX_INTERVAL_SECONDS:-300}" in service["command"]
    assert "research/live_research/evidence_index_latest.json" in service["command"]
    assert "research/live_research/evidence_index.sqlite" in service["command"]
    assert "research/live_research/evidence_index_feed.jsonl" in service["command"]
    assert "./research/pine_scripts:/app/research/pine_scripts:ro" in service["volumes"]
    assert "./research/live_research:/app/research/live_research" in service["volumes"]
    # Lean-by-default (2026-08-02): the six research producers moved to the
    # `research` profile. evidence-index-publisher aggregates their artifacts by
    # READING them from the shared research volume (stale-OK), so it no longer
    # gates on them — it runs in the lean core with no depends_on, indexing
    # whatever artifacts are present.
    assert "profiles" not in service  # stays in the always-on core
    assert "depends_on" not in service


def test_execution_replay_profile_publishes_execution_truth_surface():
    service = compose_services()["execution-replay-profile"]

    assert service["user"] == "${VNEDGE_CONTAINER_UID:-1000}:${VNEDGE_CONTAINER_GID:-1000}"
    assert service["command"][:3] == [
        "python",
        "-m",
        "vnedge.research.execution_replay_profile",
    ]
    assert "--interval-seconds" in service["command"]
    assert "${EXECUTION_REPLAY_PROFILE_INTERVAL_SECONDS:-300}" in service["command"]
    assert "research/live_research/evidence_index_latest.json" in service["command"]
    assert "research/live_research/fee_wall_forensics_latest.json" in service["command"]
    assert "research/live_research/candidate_replay_latest.json" in service["command"]
    assert "research/live_research/execution_replay_profile_latest.json" in service["command"]
    assert "research/live_research/execution_replay_profile_feed.jsonl" in service["command"]
    assert "./research/live_research:/app/research/live_research" in service["volumes"]
    assert set(service["depends_on"]) == {
        "evidence-index-publisher",
        "fee-wall-forensics",
        "candidate-replay-executor",
    }


def test_scanner_tournament_lowers_only_research_discovery_governance():
    service = compose_services()["scanner-tournament"]

    assert service["command"][:3] == ["python", "-m", "vnedge.research.scanner_tournament"]
    assert "--profile" in service["command"]
    assert "${SCANNER_TOURNAMENT_PROFILE:-discovery_relaxed}" in service["command"]
    assert "--timeframes" in service["command"]
    assert "${SCANNER_TOURNAMENT_TIMEFRAMES:-1m,5m,15m,1h,4h}" in service["command"]
    assert "--progress" in service["command"]
    assert "research/live_research/scanner_tournament_progress.json" in service["command"]
    assert "./data:/app/data:ro" in service["volumes"]
    assert "./research/live_research:/app/research/live_research" in service["volumes"]
    assert "RESEARCH_EXCHANGES" in service["environment"]


def test_event_leadlag_shadow_can_refresh_public_candle_context():
    services = compose_services()
    service = services["event-leadlag-shadow"]

    assert service["command"][:3] == [
        "python",
        "-m",
        "vnedge.runtime.event_leadlag_shadow_runner",
    ]
    assert "--refresh-bootstrap-minutes" in service["command"]
    assert "./data:/app/data" in service["volumes"]
    assert "./data:/app/data:ro" not in service["volumes"]


def test_daily_scalper_and_distillation_refresh_on_slow_interval():
    services = compose_services()
    daily = services["daily-scalper-pack"]
    distill = services["alpha-distillation"]

    assert daily["command"][:3] == ["python", "-m", "vnedge.research.daily_scalper_pack"]
    assert "--interval-seconds" in daily["command"]
    assert "--max-candidates" in daily["command"]
    assert "./data:/app/data:ro" in daily["volumes"]
    assert "./research/live_research:/app/research/live_research" in daily["volumes"]

    assert distill["command"][:3] == ["python", "-m", "vnedge.research.alpha_distillation"]
    assert "--interval-seconds" in distill["command"]
    assert "--max-candidates" in distill["command"]
    assert "./data:/app/data:ro" in distill["volumes"]
    assert "./research/live_research:/app/research/live_research" in distill["volumes"]


def test_orderflow_footprint_miner_refreshes_replay_required_artifact():
    services = compose_services()
    service = services["orderflow-footprint-miner"]

    assert service["command"][:3] == ["python", "-m", "vnedge.research.orderflow_footprint"]
    assert "--interval-seconds" in service["command"]
    assert "--bar-seconds" in service["command"]
    assert "--max-candidates" in service["command"]
    assert "./data:/app/data:ro" in service["volumes"]
    assert "./research/live_research:/app/research/live_research" in service["volumes"]


def test_alpha_council_waits_for_research_artifact_producers():
    service = compose_services()["alpha-council"]

    assert set(service["depends_on"]) >= {
        "daily-scalper-pack",
        "alpha-distillation",
        "scanner-tournament",
        "event-leadlag-miner",
        "orderflow-footprint-miner",
        "bitcoin-regime-sensor",
    }


def test_bitcoin_regime_sensor_is_context_only():
    service = compose_services()["bitcoin-regime-sensor"]

    assert service["command"][:3] == ["python", "-m", "vnedge.research.bitcoin_regime"]
    assert "--interval-seconds" in service["command"]
    assert "./research/live_research:/app/research/live_research" in service["volumes"]
    assert all("/app/data" not in volume for volume in service["volumes"])
    assert all("/app/logs" not in volume for volume in service["volumes"])
    assert "MEMPOOL_API_BASE" in service["environment"]


def test_multi_lane_shadow_has_a_health_gated_liveness_probe():
    # P0: the service that serves the dashboard must expose a healthcheck so
    # dependents can gate on service_healthy and the --force-recreate race can't
    # strand the fleet.
    service = compose_services()["multi-lane-shadow"]
    hc = service.get("healthcheck") or {}
    test = " ".join(hc.get("test") or [])
    assert "/health" in test and "8080" in test
    assert "start_period" in hc  # covers lane warmup so it isn't marked unhealthy early


def test_dashboard_tls_is_gated_on_dashboard_health():
    svc = compose_services()["dashboard-tls"]
    dep = svc["depends_on"]
    # long-form dependency gating the public proxy on the dashboard being HEALTHY
    assert isinstance(dep, dict)
    assert dep["multi-lane-shadow"]["condition"] == "service_healthy"


def test_lean_core_is_the_default_profile_and_research_is_opt_in():
    """Lean-by-default (2026-08-02): exactly the 13 core services run by default;
    the other services carry the `research` profile (opt-in). No core service may
    depend on a profiled service, or `docker compose up` would drag the whole
    research cluster back in and re-wedge the box."""
    services = compose_services()
    core = {name for name, cfg in services.items() if "profiles" not in cfg}
    research = {name for name, cfg in services.items() if "research" in (cfg.get("profiles") or [])}
    assert len(services) == len(core) + len(research)
    assert core == {
        "multi-lane-shadow", "dashboard-tls", "realtime-scanner", "lane-survival",
        "paper-lane-governor", "paper-lane-performance", "paper-roster-drift",
        "evidence-index-publisher", "ml-pipeline-status", "paper-lane-activation",
        "lane-firing-causality", "bitcoin-regime-sensor", "delta-5m-event-clock",
    }
    for name in core:
        dep = services[name].get("depends_on") or []
        deps = set(dep.keys()) if isinstance(dep, dict) else set(dep)
        assert not (deps & research), f"{name} gates on research service(s): {deps & research}"
