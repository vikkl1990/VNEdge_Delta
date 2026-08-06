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
from typing import Any

import uvicorn

from vnedge.dashboard.app import SnapshotProvider, create_app
from vnedge.dashboard.scanner_bridge import dashboard_scanner_payload

HOST = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
PORT = int(os.environ.get("DASHBOARD_PORT", "8080"))
TOKEN = os.environ.get("DASHBOARD_TOKEN", "vnedge-demo")
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
COMBINED_SCANNER_PATH = Path(
    os.environ.get(
        "DASHBOARD_COMBINED_SCANNER_PATH",
        "research/live_research/dashboard_scanner_combined_latest.json",
    )
)


def _read_payload(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def read_scanner_payload(path: Path = SCANNER_PATH) -> dict[str, Any]:
    primary = _read_payload(path)
    scalper = _read_payload(DELTA_SCALPER_PATH)
    attribution = _read_payload(DELTA_SCALPER_ATTRIBUTION_PATH)
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
        dashboard_scanner_payload(payload, now=current)
        for payload in (primary, scalper)
        if payload
    ]
    rows = [
        row
        for bridge in bridges
        for row in bridge.get("rows", [])
        if isinstance(row, dict)
    ]
    generated_values = [
        bridge.get("generated_at") for bridge in bridges if bridge.get("generated_at")
    ]
    return {
        "generated_at": max(generated_values, default=None),
        "scanner_id": "vnedge_combined_research_scanners_v1",
        "mode": "combined_research_scanners",
        "summary": {
            "connected_symbols": len(rows),
            "firing": sum(row.get("state") == "FIRING" for row in rows),
            "waiting": sum(row.get("state") == "WAITING" for row in rows),
            "errors": sum(row.get("state") == "DATA_ERROR" for row in rows),
        },
        "rows": rows,
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


def publish_combined_payload(payload: dict[str, Any]) -> None:
    COMBINED_SCANNER_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = COMBINED_SCANNER_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temporary.replace(COMBINED_SCANNER_PATH)


def build_scanner_snapshot(payload: dict[str, Any], *, now: datetime | None = None) -> dict:
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
    rows = bridge.get("rows") if isinstance(bridge.get("rows"), list) else []
    lanes = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        latest = row.get("latest_eval") if isinstance(row.get("latest_eval"), dict) else {}
        lanes.append(
            {
                "lane_id": f"scanner_{str(row.get('symbol') or '').lower()}",
                "mode": "shadow",
                "strategy_id": row.get("strategy_id"),
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
                "last_fired_ts": row.get("latest_eval_ts")
                if row.get("state") == "FIRING"
                else None,
                "funnel": {
                    "live_evals": 1,
                    "live_signals": 1 if row.get("state") == "FIRING" else 0,
                },
                "can_trade": False,
                "can_promote": False,
            }
        )

    return {
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
        "can_trade": False,
        "can_promote": False,
        "orders_sent": 0,
    }


async def main() -> None:
    provider = SnapshotProvider()
    initial = read_scanner_payload()
    publish_combined_payload(initial)
    provider.publish(build_scanner_snapshot(initial))
    app = create_app(
        provider,
        token=TOKEN,
        snapshot_hz=2.0,
        realtime_scanner_path=COMBINED_SCANNER_PATH,
        scanner_forward_evidence_path=SCANNER_EVIDENCE_PATH,
    )
    server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=PORT, log_level="warning"))

    async def publish_forever() -> None:
        while True:
            payload = read_scanner_payload()
            publish_combined_payload(payload)
            provider.publish(build_scanner_snapshot(payload))
            await asyncio.sleep(2.0)

    print(f"VNEDGE scanner dashboard: http://{HOST}:{PORT}/?token={TOKEN}")
    await asyncio.gather(server.serve(), publish_forever())


if __name__ == "__main__":
    asyncio.run(main())
