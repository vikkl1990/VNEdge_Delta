"""Compact, fail-closed Server-Sent Events for the Delta dashboard.

The stream is deliberately read-only. It projects the same atomic research
snapshot used by ``/delta-scalper`` plus the public event-recorder status. It
does not construct an exchange client, broker, risk gateway, or order route.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import Request

_SNAPSHOT_MAX_AGE_SECONDS = float(
    os.environ.get("VNEDGE_READY_MAX_SOURCE_AGE_SECONDS", "900")
)
_RECORDER_MAX_AGE_SECONDS = float(
    os.environ.get("VNEDGE_RECORDER_STATUS_MAX_AGE_SECONDS", "60")
)


def _safe_float(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _age_seconds(value: object, now: datetime) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max(0.0, (now - parsed.astimezone(UTC)).total_seconds())


def _enabled_scanners(snapshot: dict[str, Any]) -> list[str]:
    architecture = snapshot.get("architecture")
    components = architecture.get("components") if isinstance(architecture, dict) else {}
    scanner_state = str(components.get("scanner_engine") or "unknown")
    if scanner_state in {
        "available_all_hypotheses_rejected_and_disabled",
        "disabled",
        "none",
        "unknown",
    }:
        return []
    return [scanner_state]


def build_health_payload(
    snapshot: dict[str, Any],
    recorder_status: dict[str, Any] | None = None,
    event_trigger: dict[str, Any] | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a small UI-ready health payload without fabricating fields."""

    observed_at = now or datetime.now(UTC)
    recorder = recorder_status or {}
    trigger = event_trigger or {}
    snapshot_age_seconds = _age_seconds(snapshot.get("generated_at"), observed_at)
    snapshot_available = bool(snapshot.get("generated_at") and snapshot.get("rows"))
    snapshot_fresh = bool(
        snapshot_available
        and snapshot_age_seconds is not None
        and snapshot_age_seconds <= _SNAPSHOT_MAX_AGE_SECONDS
    )
    recorder_age_seconds = _age_seconds(recorder.get("updated_at"), observed_at)
    recorder_fresh = bool(
        recorder_age_seconds is not None
        and recorder_age_seconds <= _RECORDER_MAX_AGE_SECONDS
    )
    event_states = (
        trigger.get("market_states") if isinstance(trigger.get("market_states"), dict) else {}
    )
    counterfactual = (
        trigger.get("counterfactual_absorption")
        if isinstance(trigger.get("counterfactual_absorption"), dict)
        else {}
    )
    rows = [
        row
        for row in snapshot.get("rows", [])
        if isinstance(row, dict) and row.get("strategy_id") == "delta_scalper_engine_v1"
    ]
    rows_by_symbol = {str(row.get("symbol")): row for row in rows}
    gap_guard = recorder.get("gap_guard") if isinstance(recorder.get("gap_guard"), dict) else {}
    feed_delay = (
        recorder.get("feed_delay_us") if isinstance(recorder.get("feed_delay_us"), dict) else {}
    )
    markets: dict[str, dict[str, Any]] = {}
    journal_states: list[bool] = []
    latest_eval_times: list[str] = []
    for symbol in ("BTCUSD", "ETHUSD"):
        row = rows_by_symbol.get(symbol, {})
        latest = row.get("latest_eval") if isinstance(row.get("latest_eval"), dict) else {}
        l2 = (
            latest.get("l2_confirmation") if isinstance(latest.get("l2_confirmation"), dict) else {}
        )
        journal_state = latest.get("journal_write_success")
        if isinstance(journal_state, bool):
            journal_states.append(journal_state)
        latest_eval_ts = row.get("latest_eval_ts")
        if latest_eval_ts:
            latest_eval_times.append(str(latest_eval_ts))
        event_state = event_states.get(symbol) if isinstance(event_states.get(symbol), dict) else {}
        htf = event_state.get("htf") if isinstance(event_state.get("htf"), dict) else {}
        truth = (
            event_state.get("market_truth")
            if isinstance(event_state.get("market_truth"), dict)
            else {}
        )
        markets[symbol] = {
            "price": _safe_float(event_state.get("price")),
            "change_24h_pct": None,
            "price_status": (
                "event_tape"
                if _safe_float(event_state.get("price")) is not None
                else "not_published"
            ),
            "l2_age_ms": _safe_float(event_state.get("book_age_ms")),
            "l2_fresh": snapshot_fresh and l2.get("status") == "fresh",
            "l2_status": (
                l2.get("status") or "unavailable"
                if snapshot_fresh
                else "snapshot_stale"
            ),
            "l2_imbalance": _safe_float(l2.get("imbalance")),
            "feed_lag_ms": None,
            "gap_guard": (
                "healthy"
                if gap_guard.get("healthy") is True
                else "fault"
                if gap_guard.get("healthy") is False
                else "unavailable"
            ),
            "active_observation": (
                "counterfactual_absorption" if int(counterfactual.get("open") or 0) > 0 else None
            ),
            "bias_4h": htf.get("bias"),
            "stack_aligned": None,
            "mtf_status": "available" if htf.get("available") is True else "not_published",
            "market_truth_ready": truth.get("ready") is True,
            "market_truth_blockers": truth.get("blockers") or [],
            "basis_bps": _safe_float(truth.get("basis_bps")),
            "trade_book_join_ms": _safe_float(truth.get("trade_book_join_lag_ms")),
            "last_signal": latest.get("signal"),
            "last_eval_ts": latest_eval_ts,
        }

    snapshot_available = bool(snapshot.get("generated_at") and rows)
    journal_ok: bool | None
    if journal_states:
        journal_ok = all(journal_states)
    else:
        journal_ok = None
    max_feed_lag_us = _safe_float(feed_delay.get("max_us"))
    p95_feed_lag_us = _safe_float(feed_delay.get("p95_us"))
    connection = recorder.get("connection") if isinstance(recorder.get("connection"), dict) else {}
    timestamp_quality = (
        recorder.get("feed_timestamp_quality")
        if isinstance(recorder.get("feed_timestamp_quality"), dict)
        else {}
    )
    return {
        "schema_version": "vnedge.delta_health_stream.v1",
        "snapshot_generated_at": snapshot.get("generated_at"),
        "snapshot_available": snapshot_available,
        "snapshot_fresh": snapshot_fresh,
        "snapshot_age_seconds": snapshot_age_seconds,
        "snapshot_max_age_seconds": _SNAPSHOT_MAX_AGE_SECONDS,
        "error": (
            None
            if snapshot_fresh
            else "snapshot_stale"
            if snapshot_available
            else "snapshot_unavailable"
        ),
        "mode": "RESEARCH",
        "can_trade": False,
        "can_promote": False,
        "order_route": "absent",
        "enabled_scanners": _enabled_scanners(snapshot),
        "journal_ok": journal_ok,
        "last_eval_ts": max(latest_eval_times, default=None),
        "max_feed_lag_ms": max_feed_lag_us / 1_000 if max_feed_lag_us is not None else None,
        "p95_feed_lag_ms": p95_feed_lag_us / 1_000 if p95_feed_lag_us is not None else None,
        "feed_lag_scope": "event_tape",
        "feed_delay_by_channel": recorder.get("feed_delay_by_channel") or {},
        "feed_delay_corrected_by_channel": (recorder.get("feed_delay_corrected_by_channel") or {}),
        "timestamp_quality": timestamp_quality,
        "event_funnel": trigger.get("funnel") or {},
        "event_rejection_reasons": trigger.get("rejection_reasons") or {},
        "counterfactual_absorption": counterfactual,
        "per_timeframe_feed_lag_available": False,
        "gap_guard": {
            "healthy": gap_guard.get("healthy"),
            "integrity_faults": int(gap_guard.get("integrity_faults") or 0),
        },
        "recorder": {
            "state": str(recorder.get("state") or "unavailable").upper(),
            "fresh": recorder_fresh,
            "age_seconds": recorder_age_seconds,
            "maximum_age_seconds": _RECORDER_MAX_AGE_SECONDS,
            "connected": connection.get("connected"),
            "active_connection_id": connection.get("active_connection_id"),
            "connection_attempts": int(
                connection.get("attempts") or recorder.get("connections") or 0
            ),
            "connections": int(connection.get("attempts") or recorder.get("connections") or 0),
            "reconnects": int(connection.get("reconnects") or 0),
            "disconnects": int(connection.get("disconnects") or 0),
            "disconnect_reasons": connection.get("disconnect_reasons") or {},
            "events": int(recorder.get("events") or 0),
            "updated_at": recorder.get("updated_at"),
        },
        "markets": markets,
    }


async def health_event_generator(
    request: Request,
    snapshot_supplier: Callable[
        [],
        tuple[dict[str, Any], dict[str, Any]]
        | tuple[dict[str, Any], dict[str, Any], dict[str, Any]],
    ],
    *,
    interval_seconds: float = 1.0,
    ping_seconds: float = 15.0,
) -> AsyncIterator[str]:
    """Yield changed health snapshots and periodic keepalives."""

    last_canonical: str | None = None
    last_emit_monotonic = asyncio.get_running_loop().time()
    while not await request.is_disconnected():
        try:
            supplied = snapshot_supplier()
            if len(supplied) == 3:
                snapshot, recorder, trigger = supplied
            else:
                snapshot, recorder = supplied
                trigger = {}
            payload = build_health_payload(snapshot, recorder, trigger)
        except (AttributeError, OSError, TypeError, ValueError):
            # Fail closed and never leak a local parsing/read exception over SSE.
            payload = build_health_payload({}, {})

        canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        now_monotonic = asyncio.get_running_loop().time()
        if canonical != last_canonical:
            event_payload = dict(payload)
            event_payload["stream_ts"] = datetime.now(UTC).isoformat()
            yield (
                "event: health\ndata: "
                + json.dumps(
                    event_payload,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n\n"
            )
            last_canonical = canonical
            last_emit_monotonic = now_monotonic
        elif now_monotonic - last_emit_monotonic >= ping_seconds:
            yield (
                "event: ping\ndata: "
                + json.dumps(
                    {"ts": datetime.now(UTC).isoformat()},
                    separators=(",", ":"),
                )
                + "\n\n"
            )
            last_emit_monotonic = now_monotonic
        await asyncio.sleep(interval_seconds)
