"""Machine-readable architecture and safety contract for the Delta scalper."""

from __future__ import annotations


def architecture_manifest() -> dict[str, object]:
    """Describe the deployed v1 topology without implying execution authority."""

    return {
        "name": "VNEDGE Delta India Scalper Engine",
        "version": "1.0",
        "identity": {
            "scope": "local_delta_india_research_laboratory",
            "primary_symbols": ["BTCUSD", "ETHUSD"],
            "validated_after_cost_edge": False,
            "paper_trading": False,
            "live_trading": False,
            "active_research_direction": "event_time_data_integrity_and_replay",
        },
        "runtime": {
            "process_model": "single_process_asyncio_research_sidecar",
            "main_kernel_embedded": False,
            "offline_replay_uses_live_modules": True,
        },
        "components": {
            "public_websocket": "active",
            "rest_backfill": "active",
            "multi_timeframe_candles": "active",
            "l2_trade_flow_store": "active_confirmation_only",
            "context_and_regime": "active",
            "move_predictor": "legacy_benchmark_only",
            "scanner_engine": "available_all_hypotheses_rejected_and_disabled",
            "fee_and_signal_gates": "active",
            "forward_outcome_tracker": "active_orderless",
            "existing_risk_gateway_adapter": "available_not_invoked",
            "order_manager": "not_constructed",
            "broker": "not_constructed",
        },
        "decision_flow": [
            "closed_candle",
            "immutable_market_context",
            "pluggable_scanners",
            "fee_adjusted_ranking_and_gates",
            "exactly_once_research_journal",
            "next_bar_forward_measurement",
        ],
        "safety": {
            "research_only": True,
            "closed_candles_only": True,
            "l2_confirmation_only": True,
            "risk_gateway_bypass": False,
            "order_route_present": False,
            "can_trade": False,
            "can_promote": False,
        },
        "retired_primary_hypotheses": [
            "momentum_burst_v1",
            "imbalance_fade_v1",
            "hierarchical_pullback_v1",
            "btc_eth_lead_lag_v1",
            "range_compression_breakout_v1",
            "session_liquidity_sweep_v1",
            "continuous_mtf_alignment_v1",
            "continuous_mtf_alignment_v2",
        ],
    }
