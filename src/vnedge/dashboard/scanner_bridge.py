"""Read-only bridge from research scanner snapshots to dashboard tape rows."""

from __future__ import annotations

from datetime import UTC, datetime


def _parse_timestamp(value: object) -> datetime | None:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.astimezone(UTC)


def dashboard_scanner_payload(payload: dict, *, now: datetime | None = None) -> dict:
    """Adapt a causal MTF/AMF snapshot to the existing scanner-tape contract.

    Existing realtime-scanner payloads already containing ``rows`` pass
    through unchanged. The adapter never grants trading or promotion rights.
    """

    if isinstance(payload.get("rows"), list):
        return payload
    symbols = payload.get("symbols")
    if not isinstance(symbols, dict):
        return payload

    generated_at = payload.get("generated_at")
    generated = _parse_timestamp(generated_at)
    current = now or datetime.now(UTC)
    source_age_seconds = (
        max(0.0, (current - generated).total_seconds()) if generated is not None else None
    )
    source_stale = source_age_seconds is None or source_age_seconds > 15 * 60
    rows: list[dict] = []
    firing = 0

    for symbol, report in sorted(symbols.items()):
        if not isinstance(report, dict):
            continue
        summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
        latest = (
            summary.get("latest_alert") if isinstance(summary.get("latest_alert"), dict) else None
        )
        observed_at = latest.get("observed_at") if latest else None
        observed = _parse_timestamp(observed_at)
        alert_age_seconds = (
            max(0.0, (current - observed).total_seconds()) if observed is not None else None
        )
        fresh_alert = (
            not source_stale and alert_age_seconds is not None and alert_age_seconds <= 75 * 60
        )
        state = "FIRING" if fresh_alert else ("DATA_STALE" if source_stale else "WAITING")
        if fresh_alert:
            firing += 1
        side = str(latest.get("side") or "").lower() if latest else ""
        l2_confirmation = (
            latest.get("l2_confirmation")
            if latest and isinstance(latest.get("l2_confirmation"), dict)
            else {
                "status": "unavailable",
                "context_only": True,
                "used_for_signal": False,
                "used_for_execution": False,
                "used_for_promotion": False,
            }
        )
        timeframe = str((report.get("config") or {}).get("chart_timeframe") or "1h")
        if source_stale:
            why = "public-candle scanner snapshot is stale; refresh required"
        elif latest is None:
            why = "no completed MTF/AMF rejection alert in the loaded window"
        elif fresh_alert:
            why = "fresh completed-candle MTF/AMF rejection observation"
        else:
            why = "waiting; no current live alert"
        rows.append(
            {
                "strategy_id": str(report.get("scanner_id") or payload.get("scanner_id")),
                "exchange": "delta_india",
                "symbol": str(symbol),
                "timeframe": timeframe,
                "state": state,
                # The batch MTF scanner publishes historical alert counts but
                # does not expose a cumulative live evaluation counter. Keep
                # that distinction explicit; the dashboard must not invent 1.
                "evaluations": summary.get("evaluations"),
                "historical_alerts": int(summary.get("alerts") or 0),
                "last_alert_ts": observed_at,
                "latest_eval_ts": observed_at or generated_at,
                "latest_bar_ts": generated_at,
                "latest_eval": {
                    "side": side or None,
                    "signal": {"side": side or None},
                    "l2_confirmation": l2_confirmation,
                    "research_only": True,
                    "can_trade": False,
                    "can_promote": False,
                },
                "trade_lifecycle": {
                    "stage": "fresh_research_alert" if fresh_alert else "observation_only",
                    "side": side or None,
                    "final_why_no_trade": "research-only scanner; no order route",
                },
                "why": why,
                "final_why_no_trade": "research-only scanner; no order route",
                "alert_age_seconds": alert_age_seconds,
                "source_age_seconds": source_age_seconds,
                "can_trade": False,
                "can_promote": False,
                "l2_confirmation": l2_confirmation,
            }
        )

    errors = payload.get("errors") if isinstance(payload.get("errors"), dict) else {}
    for symbol, error in sorted(errors.items()):
        if symbol in symbols:
            continue
        rows.append(
            {
                "strategy_id": str(payload.get("scanner_id") or "public_candle_scanner"),
                "exchange": "delta_india",
                "symbol": str(symbol),
                "timeframe": "1h/4h",
                "state": "DATA_ERROR",
                "latest_eval_ts": generated_at,
                "latest_bar_ts": generated_at,
                "latest_eval": {},
                "trade_lifecycle": {
                    "stage": "data_error",
                    "final_why_no_trade": "research-only scanner; no order route",
                },
                "why": str(error),
                "final_why_no_trade": "research-only scanner; no order route",
                "source_age_seconds": source_age_seconds,
                "can_trade": False,
                "can_promote": False,
            }
        )

    return {
        "generated_at": generated_at,
        "mode": "delta_india_public_candles_dashboard_bridge",
        "summary": {
            "connected_symbols": len(rows),
            "firing": firing,
            "waiting": sum(row["state"] == "WAITING" for row in rows),
            "stale": sum(row["state"] == "DATA_STALE" for row in rows),
            "errors": sum(row["state"] == "DATA_ERROR" for row in rows),
            "source_age_seconds": source_age_seconds,
        },
        "rows": rows,
        "operator_answer": (
            f"connected to {len(rows)} Delta public-candle scanner lanes; "
            "observation only, no order route"
        ),
        "policy": {
            "research_only": True,
            "can_trade": False,
            "can_promote": False,
            "order_route_present": False,
        },
        "can_trade": False,
        "can_promote": False,
    }
