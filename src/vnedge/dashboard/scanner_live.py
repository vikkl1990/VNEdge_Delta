"""Serve the local dashboard from live research scanner snapshots only.

This process has no exchange adapter, broker, order manager, credentials, or
execution route. It projects public-candle scanner health into the dashboard's
read-only state and scanner-tape endpoints.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import uvicorn

from vnedge.dashboard.app import (
    SnapshotProvider,
    create_app,
    event_research_infrastructure_payload,
)
from vnedge.dashboard.scanner_bridge import dashboard_scanner_payload
from vnedge.research.event_continuity import qualify_event_continuity
from vnedge.research.strategy_evidence_registry import (
    DEFAULT_REGISTRY,
    attach_verified_lane_evidence,
    dashboard_metric_semantics,
    relabel_uncalibrated_edge_fields,
)

HOST = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
PORT = int(os.environ.get("DASHBOARD_PORT", "8080"))
TOKEN = os.environ.get("DASHBOARD_TOKEN", "").strip()
if not TOKEN and os.environ.get("VNEDGE_ALLOW_DEMO_TOKEN", "").lower() in {
    "1",
    "true",
    "yes",
}:
    TOKEN = "vnedge-demo"
SCANNER_PATH = Path(
    os.environ.get(
        "DASHBOARD_SCANNER_PATH",
        "research/live_research/mtf_amf_rejection_scanner_latest.json",
    )
)
SCANNER_EVIDENCE_PATH = Path(
    os.environ.get(
        "DASHBOARD_SCANNER_EVIDENCE_PATH",
        "research/live_research/mtf_amf_forward_evidence_latest.json",
    )
)
DELTA_SCALPER_PATH = Path(
    os.environ.get(
        "DASHBOARD_DELTA_SCALPER_PATH",
        "research/live_research/delta_scalper_engine_latest.json",
    )
)
DELTA_SCALPER_ATTRIBUTION_PATH = Path(
    os.environ.get(
        "DASHBOARD_DELTA_SCALPER_ATTRIBUTION_PATH",
        "research/live_research/delta_scalper_attribution_latest.json",
    )
)
DELTA_SCALPER_SWEEP_PATH = Path(
    os.environ.get(
        "DASHBOARD_DELTA_SCALPER_SWEEP_PATH",
        "research/live_research/delta_scalper_threshold_sweep_latest.json",
    )
)
DELTA_SCALPER_REGIME_SWEEP_PATH = Path(
    os.environ.get(
        "DASHBOARD_DELTA_SCALPER_REGIME_SWEEP_PATH",
        "research/live_research/delta_scalper_regime_sweep_latest.json",
    )
)
DELTA_SCALPER_CHANGE_POINTS_PATH = Path(
    os.environ.get(
        "DASHBOARD_DELTA_SCALPER_CHANGE_POINTS_PATH",
        "research/live_research/delta_scalper_change_points_latest.json",
    )
)
DELTA_SCALPER_META_LABEL_PATH = Path(
    os.environ.get(
        "DASHBOARD_DELTA_SCALPER_META_LABEL_PATH",
        "research/live_research/delta_scalper_meta_label_latest.json",
    )
)
DELTA_SCALPER_TRIPLE_BARRIER_META_LABEL_PATH = Path(
    os.environ.get(
        "DASHBOARD_DELTA_SCALPER_TRIPLE_BARRIER_META_LABEL_PATH",
        "research/live_research/delta_scalper_meta_label_triple_barrier_latest.json",
    )
)
DELTA_SCALPER_LIGHTGBM_META_LABEL_PATH = Path(
    os.environ.get(
        "DASHBOARD_DELTA_SCALPER_LIGHTGBM_META_LABEL_PATH",
        "research/live_research/delta_scalper_lightgbm_meta_latest.json",
    )
)
DELTA_SCALPER_CATEGORICAL_ENCODING_PATH = Path(
    os.environ.get(
        "DASHBOARD_DELTA_SCALPER_CATEGORICAL_ENCODING_PATH",
        "research/live_research/delta_scalper_categorical_encoding_latest.json",
    )
)
DELTA_SCALPER_CUSUM_INTERACTIONS_PATH = Path(
    os.environ.get(
        "DASHBOARD_DELTA_SCALPER_CUSUM_INTERACTIONS_PATH",
        "research/live_research/delta_scalper_cusum_interactions_latest.json",
    )
)
DELTA_SCALPER_LIGHTGBM_SHAP_PATH = Path(
    os.environ.get(
        "DASHBOARD_DELTA_SCALPER_LIGHTGBM_SHAP_PATH",
        "research/live_research/delta_scalper_lightgbm_shap_latest.json",
    )
)
DELTA_ACTIVE_COST_EVIDENCE_PATH = Path(
    os.environ.get(
        "DASHBOARD_DELTA_ACTIVE_COST_EVIDENCE_PATH",
        "research/live_research/delta_active_cost_evidence_latest.json",
    )
)
COMBINED_SCANNER_PATH = Path(
    os.environ.get(
        "DASHBOARD_COMBINED_SCANNER_PATH",
        "research/live_research/dashboard_scanner_combined_latest.json",
    )
)
DELTA_EVENT_ROOT = Path(os.environ.get("DASHBOARD_DELTA_EVENT_ROOT", "data/delta_events"))
EVENT_TRIGGER_TELEMETRY_PATH = Path(
    os.environ.get(
        "DASHBOARD_EVENT_TRIGGER_TELEMETRY_PATH",
        "research/live_research/delta_event_trigger_telemetry_latest.json",
    )
)
ABSORPTION_DASHBOARD_PATH = Path(
    os.environ.get(
        "DASHBOARD_ABSORPTION_PATH",
        "research/live_research/delta_absorption_dashboard_latest.json",
    )
)
EVENT_REPLAY_DIR = Path(os.environ.get("DASHBOARD_EVENT_REPLAY_DIR", "research/event_replay"))
REPLAY_DETERMINISM_PROOF_PATH = Path(
    os.environ.get(
        "DASHBOARD_REPLAY_DETERMINISM_PROOF_PATH",
        "research/event_replay/replay_determinism_latest.json",
    )
)
EVENT_CONTINUITY_PATH = Path(
    os.environ.get(
        "DASHBOARD_EVENT_CONTINUITY_PATH",
        "research/live_research/delta_event_continuity_latest.json",
    )
)
EVENT_CONTINUITY_CACHE_PATH = Path(
    os.environ.get(
        "DASHBOARD_EVENT_CONTINUITY_CACHE_PATH",
        "research/live_research/delta_event_continuity_shard_cache.json",
    )
)
EVENT_CONTINUITY_CONTRACT_PATH = Path(
    os.environ.get(
        "DASHBOARD_EVENT_CONTINUITY_CONTRACT_PATH",
        "configs/research/failed_auction_response_v1.yaml",
    )
)
EVENT_CONTINUITY_REFRESH_SECONDS = max(
    60.0, float(os.environ.get("DASHBOARD_EVENT_CONTINUITY_REFRESH_SECONDS", "300"))
)
REFRESH_EVENT_CONTINUITY_IN_DASHBOARD = os.environ.get(
    "DASHBOARD_REFRESH_EVENT_CONTINUITY", "0"
).strip().lower() in {"1", "true", "yes", "on"}
EVENT_CONTINUITY_CODE_VERSION = os.environ.get(
    "DASHBOARD_EVENT_CONTINUITY_CODE_VERSION", "local-runtime"
)
HTF_STRUCTURE_V2_PATH = Path(
    os.environ.get(
        "DASHBOARD_HTF_STRUCTURE_V2_PATH",
        "research/live_research/htf_structure_break_v2_latest.json",
    )
)
FAILED_AUCTION_READINESS_PATH = Path(
    os.environ.get(
        "DASHBOARD_FAILED_AUCTION_READINESS_PATH",
        "research/live_research/failed_auction_response_v1_readiness_latest.json",
    )
)
EVENT_RESPONSE_ATLAS_PATH = Path(
    os.environ.get(
        "DASHBOARD_EVENT_RESPONSE_ATLAS_PATH",
        "research/live_research/event_response_atlas_latest.json",
    )
)
POST_ABSORPTION_DIRECTION_PATH = Path(
    os.environ.get(
        "DASHBOARD_POST_ABSORPTION_DIRECTION_PATH",
        "research/live_research/post_absorption_direction_study_latest.json",
    )
)
KRONOS_MATRIX_PATH = Path(
    os.environ.get(
        "DASHBOARD_KRONOS_MATRIX_PATH",
        "research/live_research/kronos_permutation_matrix_latest.json",
    )
)
KRONOS_CONFIRMATION_PATH = Path(
    os.environ.get(
        "DASHBOARD_KRONOS_CONFIRMATION_PATH",
        "research/live_research/kronos_permutation_confirmation_latest.json",
    )
)
FORCED_FLOW_DIR = Path(
    os.environ.get(
        "DASHBOARD_FORCED_FLOW_DIR",
        "research/live_research/delta_forced_flow_panel",
    )
)
STRATEGY_EVIDENCE_REGISTRY_PATH = Path(
    os.environ.get(
        "VNEDGE_STRATEGY_REGISTRY",
        os.environ.get("DASHBOARD_STRATEGY_EVIDENCE_REGISTRY", str(DEFAULT_REGISTRY)),
    )
)


def _read_payload(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def read_scanner_payload(
    path: Path = SCANNER_PATH, *, include_legacy_research: bool = True
) -> dict[str, Any]:
    primary = _read_payload(path) if include_legacy_research else {}
    scalper = _read_payload(DELTA_SCALPER_PATH)
    attribution = _read_payload(DELTA_SCALPER_ATTRIBUTION_PATH)
    threshold_sweep = _read_payload(DELTA_SCALPER_SWEEP_PATH)
    regime_sweep = _read_payload(DELTA_SCALPER_REGIME_SWEEP_PATH)
    change_points = _read_payload(DELTA_SCALPER_CHANGE_POINTS_PATH)
    meta_label = _read_payload(DELTA_SCALPER_META_LABEL_PATH)
    triple_barrier_meta_label = _read_payload(DELTA_SCALPER_TRIPLE_BARRIER_META_LABEL_PATH)
    lightgbm_meta_label = _read_payload(DELTA_SCALPER_LIGHTGBM_META_LABEL_PATH)
    categorical_encoding = _read_payload(DELTA_SCALPER_CATEGORICAL_ENCODING_PATH)
    cusum_interactions = _read_payload(DELTA_SCALPER_CUSUM_INTERACTIONS_PATH)
    lightgbm_shap = _read_payload(DELTA_SCALPER_LIGHTGBM_SHAP_PATH)
    active_cost_evidence = _read_payload(DELTA_ACTIVE_COST_EVIDENCE_PATH)
    if not primary and not scalper:
        return {
            "generated_at": None,
            "mode": "combined_research_scanners",
            "rows": [],
            "errors": {"scanner": "scanner snapshot unavailable"},
            "can_trade": False,
            "can_promote": False,
        }
    current = datetime.now(UTC)
    bridges = [
        dashboard_scanner_payload(payload, now=current) for payload in (primary, scalper) if payload
    ]
    raw_rows = [
        row for bridge in bridges for row in bridge.get("rows", []) if isinstance(row, dict)
    ]
    rows, withheld_lanes, registry = attach_verified_lane_evidence(
        raw_rows,
        registry_path=STRATEGY_EVIDENCE_REGISTRY_PATH,
    )
    generated_values = [
        bridge.get("generated_at") for bridge in bridges if bridge.get("generated_at")
    ]
    source_ages = [
        float(row["source_age_seconds"])
        for row in rows
        if isinstance(row.get("source_age_seconds"), (int, float))
        and not isinstance(row.get("source_age_seconds"), bool)
    ]
    result = {
        "generated_at": max(generated_values, default=None),
        "scanner_id": "vnedge_combined_research_scanners_v1",
        "mode": "combined_research_scanners",
        "summary": {
            "connected_symbols": len(rows),
            "firing": sum(row.get("state") == "FIRING" for row in rows),
            "waiting": sum(row.get("state") == "WAITING" for row in rows),
            "stale": sum(row.get("state") == "DATA_STALE" for row in rows),
            "errors": sum(row.get("state") == "DATA_ERROR" for row in rows),
            "source_age_seconds": max(source_ages, default=None),
            "withheld_unverified_lanes": len(withheld_lanes),
        },
        "rows": rows,
        "withheld_lanes": withheld_lanes,
        "strategy_evidence_registry": registry,
        "metric_semantics": dashboard_metric_semantics(),
        "sources": [bridge.get("mode") for bridge in bridges],
        "delta_scalper": {
            "summary": scalper.get("summary"),
            "architecture": scalper.get("architecture"),
            "fee_model": scalper.get("fee_model"),
            "backtest_summary": scalper.get("backtest_summary"),
            "fee_effectiveness": scalper.get("fee_effectiveness"),
            "robust_validation": scalper.get("robust_validation"),
            "untouched_window": scalper.get("untouched_window"),
            "attribution": attribution or None,
            "threshold_sweep": threshold_sweep or None,
            "regime_sweep": regime_sweep or None,
            "change_points": change_points or None,
            "meta_label": meta_label or None,
            "triple_barrier_meta_label": triple_barrier_meta_label or None,
            "lightgbm_meta_label": lightgbm_meta_label or None,
            "categorical_encoding": categorical_encoding or None,
            "cusum_interactions": cusum_interactions or None,
            "lightgbm_shap": lightgbm_shap or None,
            "active_cost_evidence": active_cost_evidence or None,
        }
        if scalper
        else None,
        "policy": {
            "research_only": True,
            "order_route_present": False,
            "can_trade": False,
            "can_promote": False,
        },
        "can_trade": False,
        "can_promote": False,
    }
    return relabel_uncalibrated_edge_fields(result)


def publish_combined_payload(payload: dict[str, Any]) -> None:
    COMBINED_SCANNER_PATH.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=COMBINED_SCANNER_PATH.parent,
        prefix=f".{COMBINED_SCANNER_PATH.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, COMBINED_SCANNER_PATH)


def build_scanner_snapshot(
    payload: dict[str, Any],
    *,
    now: datetime | None = None,
    research_infrastructure: dict[str, object] | None = None,
) -> dict:
    current = now or datetime.now(UTC)
    bridge = dashboard_scanner_payload(payload, now=current)
    summary = bridge.get("summary") if isinstance(bridge.get("summary"), dict) else {}
    source_age_seconds = summary.get("source_age_seconds")
    feed_state = (
        "stale"
        if int(summary.get("stale") or 0) > 0
        else "error"
        if int(summary.get("errors") or 0) > 0
        else "ok"
    )
    raw_rows = bridge.get("rows") if isinstance(bridge.get("rows"), list) else []
    rows, withheld_lanes, registry = attach_verified_lane_evidence(
        [row for row in raw_rows if isinstance(row, dict)],
        registry_path=STRATEGY_EVIDENCE_REGISTRY_PATH,
    )
    lanes = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        latest = row.get("latest_eval") if isinstance(row.get("latest_eval"), dict) else {}
        evaluations = row.get("evaluations")
        if not isinstance(evaluations, int) or isinstance(evaluations, bool) or evaluations < 0:
            evaluations = None
        historical_alerts = row.get("historical_alerts", row.get("alerts", 0))
        if (
            not isinstance(historical_alerts, int)
            or isinstance(historical_alerts, bool)
            or historical_alerts < 0
        ):
            historical_alerts = 0
        lanes.append(
            {
                "lane_id": f"scanner_{str(row.get('symbol') or '').lower()}",
                "mode": "research_observation",
                "strategy_id": row.get("strategy_id"),
                "strategy_label": (
                    registry.get("strategies", {})
                    .get(str(row.get("strategy_id") or ""), {})
                    .get("label")
                ),
                "hypothesis_class": row.get("hypothesis_class"),
                "trade_horizon": row.get("trade_horizon"),
                "lifecycle": row.get("lifecycle"),
                "edge_claim": row.get("edge_claim"),
                "evidence": {
                    "artifact": row.get("evidence_artifact"),
                    "sha256": row.get("evidence_sha256"),
                    "verified": row.get("registry_verified") is True,
                    "metrics": row.get("evidence_metrics") or {},
                    "warnings": row.get("evidence_warnings") or [],
                },
                "route_cost_contract": row.get("route_cost_contract"),
                "exchange": "delta_india",
                "symbol": row.get("symbol"),
                "timeframe": row.get("timeframe"),
                "feed": "ok" if row.get("state") not in {"DATA_STALE", "DATA_ERROR"} else "stale",
                "staleness_ms": float(row.get("source_age_seconds") or 0) * 1_000.0,
                "positions": 0,
                "realized_pnl": 0.0,
                "unrealized_pnl": 0.0,
                "price": None,
                "funding_rate": None,
                "last_eval": {
                    "fired": row.get("state") == "FIRING",
                    "side": latest.get("side"),
                    "signal_reason": row.get("why"),
                    "l2_confirmation": latest.get("l2_confirmation"),
                },
                "last_fired_ts": row.get("last_alert_ts")
                or (row.get("latest_eval_ts") if row.get("state") == "FIRING" else None),
                "funnel": {
                    "live_evals": evaluations,
                    "live_signals": 1 if row.get("state") == "FIRING" else 0,
                    "historical_alerts": historical_alerts,
                },
                "trade_compatibility": {
                    "state": "RESEARCH_ONLY",
                    "reason": "scanner observation only; no paper or order route",
                },
                "can_trade": False,
                "can_promote": False,
            }
        )

    result = {
        "ts": current.isoformat(),
        "mode": "research scanner observation",
        "symbol": ",".join(
            str(row.get("symbol")) for row in rows if isinstance(row, dict) and row.get("symbol")
        ),
        "strategy_id": str(payload.get("scanner_id") or "mtf_amf_rejection_scanner_v1"),
        "recent_alerts": [],
        "price": None,
        "funding_rate": 0.0,
        "session": {
            "scanner_source_generated_at": payload.get("generated_at"),
            "connected_symbols": summary.get("connected_symbols", 0),
            "firing": summary.get("firing", 0),
            "errors": summary.get("errors", 0),
            "withheld_unverified_lanes": len(withheld_lanes),
        },
        "trial": None,
        "live_trading_enabled": False,
        "kill_switch_active": False,
        "equity": 0.0,
        "peak_equity": 0.0,
        "realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
        "daily_pnl": 0.0,
        "consecutive_losses": 0,
        "risk_status": "execution_locked_research_only",
        "feed_health": {
            "exchange": "delta_india public candles",
            "candles": feed_state,
            "funding": "not_used",
            "open_interest": "not_used",
            "last_update_ms": (
                float(source_age_seconds) * 1_000.0 if source_age_seconds is not None else None
            ),
        },
        "positions": [],
        "open_orders": [],
        "recent_fills": [],
        "fills": 0,
        "fees_usd": 0.0,
        "last_risk_reject": None,
        "last_journal_write": "scanner snapshot only",
        "lanes": lanes,
        "withheld_lanes": withheld_lanes,
        "strategy_evidence_registry": registry,
        "metric_semantics": dashboard_metric_semantics(),
        "can_trade": False,
        "can_promote": False,
        "orders_sent": 0,
        "research_infrastructure": research_infrastructure or {},
    }
    return relabel_uncalibrated_edge_fields(result)


async def main() -> None:
    if not TOKEN:
        raise RuntimeError(
            "DASHBOARD_TOKEN is required. For an explicit local demo only, set "
            "VNEDGE_ALLOW_DEMO_TOKEN=true."
        )
    provider = SnapshotProvider()
    combined = read_scanner_payload()
    publish_combined_payload(combined)
    # The operator homepage and readiness contract are Delta BTCUSD/ETHUSD.
    # Legacy multi-market research remains available on /realtime-scanner.
    initial = read_scanner_payload(include_legacy_research=False)
    infrastructure = event_research_infrastructure_payload(
        event_root=DELTA_EVENT_ROOT,
        event_trigger_telemetry_path=EVENT_TRIGGER_TELEMETRY_PATH,
        absorption_dashboard_path=ABSORPTION_DASHBOARD_PATH,
        event_replay_dir=EVENT_REPLAY_DIR,
        replay_determinism_proof_path=REPLAY_DETERMINISM_PROOF_PATH,
        event_continuity_path=EVENT_CONTINUITY_PATH,
        kronos_matrix_path=KRONOS_MATRIX_PATH,
        kronos_confirmation_path=KRONOS_CONFIRMATION_PATH,
        forced_flow_dir=FORCED_FLOW_DIR,
        htf_structure_v2_path=HTF_STRUCTURE_V2_PATH,
        failed_auction_readiness_path=FAILED_AUCTION_READINESS_PATH,
        event_response_atlas_path=EVENT_RESPONSE_ATLAS_PATH,
        post_absorption_direction_path=POST_ABSORPTION_DIRECTION_PATH,
    )
    provider.publish(build_scanner_snapshot(initial, research_infrastructure=infrastructure))
    app = create_app(
        provider,
        token=TOKEN,
        snapshot_hz=2.0,
        realtime_scanner_path=COMBINED_SCANNER_PATH,
        delta_scalper_path=DELTA_SCALPER_PATH,
        delta_active_cost_evidence_path=DELTA_ACTIVE_COST_EVIDENCE_PATH,
        scanner_forward_evidence_path=SCANNER_EVIDENCE_PATH,
        delta_event_root=DELTA_EVENT_ROOT,
        event_trigger_telemetry_path=EVENT_TRIGGER_TELEMETRY_PATH,
        absorption_dashboard_path=ABSORPTION_DASHBOARD_PATH,
        event_replay_dir=EVENT_REPLAY_DIR,
        replay_determinism_proof_path=REPLAY_DETERMINISM_PROOF_PATH,
        event_continuity_path=EVENT_CONTINUITY_PATH,
        kronos_matrix_path=KRONOS_MATRIX_PATH,
        kronos_confirmation_path=KRONOS_CONFIRMATION_PATH,
        forced_flow_dir=FORCED_FLOW_DIR,
        htf_structure_v2_path=HTF_STRUCTURE_V2_PATH,
        failed_auction_readiness_path=FAILED_AUCTION_READINESS_PATH,
        event_response_atlas_path=EVENT_RESPONSE_ATLAS_PATH,
        post_absorption_direction_path=POST_ABSORPTION_DIRECTION_PATH,
    )
    server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=PORT, log_level="warning"))

    async def publish_forever() -> None:
        while True:
            combined = read_scanner_payload()
            publish_combined_payload(combined)
            payload = read_scanner_payload(include_legacy_research=False)
            infrastructure = event_research_infrastructure_payload(
                event_root=DELTA_EVENT_ROOT,
                event_trigger_telemetry_path=EVENT_TRIGGER_TELEMETRY_PATH,
                absorption_dashboard_path=ABSORPTION_DASHBOARD_PATH,
                event_replay_dir=EVENT_REPLAY_DIR,
                replay_determinism_proof_path=REPLAY_DETERMINISM_PROOF_PATH,
                event_continuity_path=EVENT_CONTINUITY_PATH,
                kronos_matrix_path=KRONOS_MATRIX_PATH,
                kronos_confirmation_path=KRONOS_CONFIRMATION_PATH,
                forced_flow_dir=FORCED_FLOW_DIR,
                htf_structure_v2_path=HTF_STRUCTURE_V2_PATH,
                failed_auction_readiness_path=FAILED_AUCTION_READINESS_PATH,
                event_response_atlas_path=EVENT_RESPONSE_ATLAS_PATH,
                post_absorption_direction_path=POST_ABSORPTION_DIRECTION_PATH,
            )
            provider.publish(
                build_scanner_snapshot(payload, research_infrastructure=infrastructure)
            )
            await asyncio.sleep(2.0)

    async def refresh_continuity_forever() -> None:
        while True:
            try:
                await asyncio.to_thread(
                    qualify_event_continuity,
                    DELTA_EVENT_ROOT,
                    contract_path=EVENT_CONTINUITY_CONTRACT_PATH,
                    output_path=EVENT_CONTINUITY_PATH,
                    cache_path=EVENT_CONTINUITY_CACHE_PATH,
                    code_version=EVENT_CONTINUITY_CODE_VERSION,
                )
            except Exception as exc:  # noqa: BLE001 - dashboard stays fail-closed
                print(f"Continuity qualification refresh failed: {exc!r}")
            await asyncio.sleep(EVENT_CONTINUITY_REFRESH_SECONDS)

    print(f"VNEDGE scanner dashboard: http://{HOST}:{PORT}/?token={TOKEN}")
    tasks = [server.serve(), publish_forever()]
    # Continuity qualification is CPU-heavy research work.  It is disabled in
    # the read-only web process by default so evidence refresh cannot starve
    # health checks or the operator UI.  A dedicated publisher may opt in.
    if REFRESH_EVENT_CONTINUITY_IN_DASHBOARD:
        tasks.append(refresh_continuity_forever())
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
