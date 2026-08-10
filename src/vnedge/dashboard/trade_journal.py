"""Read-only trade journal projection for the dashboard.

The decision journal remains the source of truth for decisions and order
state; the fill ledger remains the source of truth for executions. This module
builds an operator-friendly view across those append-only files: positions,
orders, fills, resolved trades, and event chronology.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class TradeJournalConfig:
    tail_bytes: int = 4_000_000
    max_rows: int = 200


def build_trade_journal(
    *,
    snapshot: dict | None,
    journal_dir: Path | str | None,
    history_path: Path | str | None = None,
    lane: str = "",
    since: str | None = None,
    limit: int = 200,
    config: TradeJournalConfig = TradeJournalConfig(),
) -> dict[str, Any]:
    """Build a dashboard trade journal from snapshot + append-only artifacts.

    ``lane`` filters to one lane id. Empty lane means fleet view: scan every
    lane journal/fill ledger and include the current primary snapshot's live
    positions/orders.
    """
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    root = Path(journal_dir) if journal_dir is not None else None
    lane = lane.strip()
    limit = max(1, min(int(limit), config.max_rows))
    since_dt = _parse_dt(since)

    # Fleet view (no lane filter) restricts to the lanes we ACTIVELY run, so
    # retired lanes' on-disk ledgers never leak into the aggregate. A single
    # named lane is always honored directly.
    active = _active_lane_ids(snapshot) if not lane else None

    fills = _fill_rows(root, lane=lane, since=since_dt, config=config, active=active)
    journal_rows = _journal_rows(
        root, lane=lane, since=since_dt, config=config, active=active
    )

    positions = _snapshot_positions(snapshot, lane)
    snapshot_orders = _snapshot_orders(snapshot, lane)
    snapshot_fills = _snapshot_fills(snapshot, lane, since_dt)
    if not fills:
        fills = snapshot_fills

    order_rows, journal_events, virtual_trades = _project_journals(journal_rows)
    order_rows = _merge_snapshot_orders(order_rows, snapshot_orders)

    closed_trades = _build_closed_trades(fills, journal_rows, virtual_trades)
    actual_closed = [
        row for row in closed_trades if row.get("kind") == "actual_closing_fill"
    ]
    shadow_closed = [
        row for row in closed_trades if row.get("kind") != "actual_closing_fill"
    ]
    events = _snapshot_events(snapshot, lane, since_dt) + journal_events

    fills = _sort_recent(fills)[:limit]
    order_rows = _sort_recent(order_rows)[:limit]
    closed_trades = _sort_recent(closed_trades)[:limit]
    events = _sort_recent(events)[:limit]

    actual_realized = sum(_float(row.get("realized_pnl_usd")) for row in fills)
    fees = sum(_float(row.get("fee_usd")) for row in fills)
    virtual_net = sum(_float(row.get("virtual_net_usd")) for row in closed_trades)
    actual_closed_net = sum(_float(row.get("net_after_this_fill_fee_usd")) for row in actual_closed)
    actual_closed_fees = sum(_float(row.get("fee_usd")) for row in actual_closed)
    lane_counts = _lane_counts(snapshot)

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "lane": lane or "all",
        "summary": {
            "positions": len(positions),
            "orders": len(order_rows),
            "open_orders": sum(1 for row in order_rows if _is_open_order(row)),
            "fills": len(fills),
            "closed_trades": len(closed_trades),
            "actual_closed_trades": len(actual_closed),
            "shadow_closed_trades": len(shadow_closed),
            "events": len(events),
            "journals_scanned": _count_paths(root, ".journal.jsonl", lane, active),
            "fill_ledgers_scanned": _count_paths(root, ".fills.jsonl", lane, active),
            "active_lanes": len(active) if active is not None else None,
            "actual_realized_pnl_usd": round(actual_realized, 6),
            "fees_usd": round(fees, 6),
            "virtual_net_usd": round(virtual_net, 6),
            "actual_closed_net_usd": round(actual_closed_net, 6),
            "actual_closed_fees_usd": round(actual_closed_fees, 6),
            "lane_position_counts": lane_counts,
            "lane_pnl": _lane_pnl_rollup(closed_trades),
            "cohort_pnl": _cohort_pnl_rollup(closed_trades),
            "history_lane": _primary_lane(history_path),
        },
        "positions": positions[:limit],
        "orders": order_rows,
        "fills": fills,
        "closed_trades": closed_trades,
        "events": events,
        "policy": {
            "read_only": True,
            "can_trade": False,
            "can_promote": False,
            "source": "snapshot + decision journals + hash-chained fill ledgers",
        },
        "can_trade": False,
        "can_promote": False,
    }


def _tail_lines(path: Path, max_bytes: int) -> list[str]:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            data = handle.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]
    return [line for line in lines if line.strip()]


def _iter_jsonl(path: Path, *, max_bytes: int) -> Iterable[dict[str, Any]]:
    for line in _tail_lines(path, max_bytes):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            yield row


def _paths(
    root: Path | None, suffix: str, lane: str, active: set[str] | None = None
) -> list[Path]:
    if root is None or not root.is_dir():
        return []
    if lane:
        candidate = root / f"{lane}{suffix}"
        return [candidate] if candidate.exists() else []
    paths = sorted(root.glob(f"*{suffix}"))
    if active:
        # Fleet view: only the lanes we ACTIVELY run (present in the live
        # snapshot). Retired lanes leave ledgers on disk; they must not
        # pollute the aggregate P&L or the closed-trade list.
        paths = [p for p in paths if _lane_from_path(p, suffix) in active]
    return paths


def _count_paths(
    root: Path | None, suffix: str, lane: str, active: set[str] | None = None
) -> int:
    return len(_paths(root, suffix, lane, active))


def _active_lane_ids(snapshot: dict[str, Any]) -> set[str]:
    """Lane ids currently RUNNING per the live snapshot — the primary lane plus
    every lane in the multi-lane array. Empty set means we can't tell (single
    lane session / demo / no snapshot), and the caller falls back to scanning
    all ledgers rather than showing nothing."""
    ids: set[str] = set()
    primary = snapshot.get("lane_id")
    if primary:
        ids.add(str(primary))
    for lane in snapshot.get("lanes") or []:
        if isinstance(lane, dict) and lane.get("lane_id"):
            ids.add(str(lane["lane_id"]))
    return ids


def _lane_from_path(path: Path, suffix: str) -> str:
    return path.name.removesuffix(suffix)


def _parse_dt(value: object) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso_from_ms(value: object) -> str | None:
    try:
        return datetime.fromtimestamp(float(value) / 1000.0, tz=UTC).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _record_ts(record: dict[str, Any], payload: dict[str, Any] | None = None) -> str:
    payload = payload if isinstance(payload, dict) else {}
    for key in ("ts", "bar_ts", "resolved_bar_ts"):
        if record.get(key):
            return str(record[key])
        if payload.get(key):
            return str(payload[key])
    for key in ("exit_ts_ms", "entry_ts_ms", "impulse_ts_ms"):
        converted = _iso_from_ms(payload.get(key))
        if converted:
            return converted
    return ""


def _after_since(ts: object, since: datetime | None) -> bool:
    if since is None:
        return True
    parsed = _parse_dt(ts)
    return parsed is not None and parsed >= since


def _float(value: object, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed != parsed:
        return default
    return parsed


def _fill_rows(
    root: Path | None,
    *,
    lane: str,
    since: datetime | None,
    config: TradeJournalConfig,
    active: set[str] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in _paths(root, ".fills.jsonl", lane, active):
        lane_id = _lane_from_path(path, ".fills.jsonl")
        for raw in _iter_jsonl(path, max_bytes=config.tail_bytes):
            ts = _record_ts(raw)
            if not _after_since(ts, since):
                continue
            rows.append(
                {
                    "lane": lane_id,
                    "ts": ts,
                    "mode": raw.get("mode", ""),
                    "venue": raw.get("venue", ""),
                    "strategy_id": raw.get("strategy_id", ""),
                    "symbol": raw.get("symbol", ""),
                    "side": raw.get("side", ""),
                    "quantity": _float(raw.get("quantity")),
                    "price": _float(raw.get("price")),
                    "fee_usd": _float(raw.get("fee_usd")),
                    "realized_pnl_usd": _float(raw.get("realized_pnl_usd")),
                    "client_order_id": raw.get("client_order_id", ""),
                    "exchange_seq": raw.get("exchange_seq", raw.get("seq", "")),
                    "hash": raw.get("hash", ""),
                    "source": "fill_ledger",
                }
            )
    return rows


def _journal_rows(
    root: Path | None,
    *,
    lane: str,
    since: datetime | None,
    config: TradeJournalConfig,
    active: set[str] | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    rows: list[tuple[str, dict[str, Any]]] = []
    for path in _paths(root, ".journal.jsonl", lane, active):
        lane_id = _lane_from_path(path, ".journal.jsonl")
        for raw in _iter_jsonl(path, max_bytes=config.tail_bytes):
            ts = _record_ts(raw, raw.get("payload") if isinstance(raw.get("payload"), dict) else {})
            if not _after_since(ts, since):
                continue
            rows.append((lane_id, raw))
    return rows


def _project_journals(
    journal_rows: list[tuple[str, dict[str, Any]]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    orders: dict[str, dict[str, Any]] = {}
    events: list[dict[str, Any]] = []
    virtual_trades: list[dict[str, Any]] = []
    for lane, record in journal_rows:
        kind = str(record.get("kind") or "")
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        ts = _record_ts(record, payload)
        if kind in _ORDER_KINDS:
            _apply_order_event(orders, lane, ts, kind, payload)
        if kind in _EVENT_KINDS:
            events.append(
                {
                    "lane": lane,
                    "ts": ts,
                    "event": kind,
                    "detail": _event_detail(kind, payload),
                    "source": "decision_journal",
                }
            )
        if kind == "shadow_outcome":
            virtual_trades.append(_shadow_outcome_trade(lane, ts, payload))
        elif kind == "scalp_shadow_outcome":
            virtual_trades.append(_scalp_outcome_trade(lane, ts, payload))
    return list(orders.values()), events, virtual_trades


_ORDER_KINDS = {
    "risk_decision",
    "order_intent",
    "order_acknowledged",
    "order_rejected",
    "order_timeout_unknown",
    "order_refused",
    "order_cancel",
    "order_fill_sync",
    "order_resolved",
}

_EVENT_KINDS = _ORDER_KINDS | {
    "shadow_intent",
    "shadow_outcome",
    "scalp_shadow_intent",
    "scalp_shadow_outcome",
    "live_paper_exit",
    "paper_exit",
    "tick_stop_exit",
    "exit_plan_cleared_after_reconciliation",
    "exit_plan_preserved_after_reconciliation",
    "daily_report",
    "lane_eval",
    "executor_finished",
    "executor_scalper_risk_decision",
}


def _order_id(payload: dict[str, Any]) -> str:
    return str(payload.get("client_order_id") or payload.get("order_id") or "")


def _ensure_order(
    orders: dict[str, dict[str, Any]], lane: str, ts: str, coid: str
) -> dict[str, Any]:
    key = coid or f"{lane}|unknown|{len(orders)}"
    row = orders.setdefault(
        key,
        {
            "lane": lane,
            "ts": ts,
            "client_order_id": coid,
            "exchange_order_id": "",
            "symbol": "",
            "side": "",
            "order_type": "",
            "quantity": 0.0,
            "limit_price": None,
            "reduce_only": False,
            "strategy_id": "",
            "state": "observed",
            "last_event": "",
            "reason": "",
            "source": "decision_journal",
        },
    )
    row["ts"] = max(str(row.get("ts") or ""), ts)
    return row


def _apply_order_event(
    orders: dict[str, dict[str, Any]],
    lane: str,
    ts: str,
    kind: str,
    payload: dict[str, Any],
) -> None:
    coid = _order_id(payload)
    intent = payload.get("intent") if isinstance(payload.get("intent"), dict) else {}
    if not coid and isinstance(intent, dict):
        coid = str(payload.get("client_order_id") or "")
    row = _ensure_order(orders, lane, ts, coid)
    row["last_event"] = kind
    if intent:
        row.update(
            {
                "symbol": intent.get("symbol", row.get("symbol", "")),
                "side": intent.get("side", row.get("side", "")),
                "order_type": intent.get("order_type", row.get("order_type", "")),
                "quantity": _float(intent.get("quantity"), row.get("quantity", 0.0)),
                "limit_price": intent.get("limit_price", row.get("limit_price")),
                "reduce_only": bool(intent.get("reduce_only", row.get("reduce_only"))),
                "strategy_id": intent.get("strategy_id", row.get("strategy_id", "")),
            }
        )
    if kind == "risk_decision":
        row["state"] = "risk_approved" if payload.get("approved") else "risk_rejected"
        row["reason"] = ", ".join(payload.get("failed_checks") or [])
    elif kind == "order_intent":
        row["state"] = "intent_created"
    elif kind == "order_acknowledged":
        row["state"] = "acknowledged"
        row["exchange_order_id"] = str(payload.get("exchange_order_id") or "")
    elif kind == "order_rejected":
        row["state"] = "rejected"
        row["reason"] = str(payload.get("reason") or "")
    elif kind == "order_timeout_unknown":
        row["state"] = "timeout_unknown"
        row["reason"] = str(payload.get("detail") or "")
    elif kind == "order_refused":
        row["state"] = "refused"
        row["reason"] = str(payload.get("reason") or "")
    elif kind == "order_cancel":
        row["state"] = str(payload.get("venue_state") or "cancelled")
        row["filled_quantity"] = _float(payload.get("filled_quantity"))
        row["reason"] = str(payload.get("reason") or "")
    elif kind == "order_fill_sync":
        row["state"] = str(payload.get("state") or row.get("state"))
        row["filled_quantity"] = _float(payload.get("filled_quantity"))
        row["fees_paid"] = _float(payload.get("fees_paid"))
    elif kind == "order_resolved":
        row["state"] = "resolved"
        row["reason"] = str(payload.get("venue_state") or payload.get("reason") or "")


def _event_detail(kind: str, payload: dict[str, Any]) -> str:
    if kind == "lane_eval":
        fired = "fired" if payload.get("fired") else "waiting"
        reason = payload.get("signal_reason") or payload.get("skip_reason") or ""
        return f"{fired}: {reason}".strip(": ")
    if kind in {"shadow_outcome", "scalp_shadow_outcome"}:
        net = payload.get("virtual_net_usd", payload.get("taker_net_usd"))
        return (
            f"{payload.get('resolution', 'resolved')} {payload.get('side', '')} "
            f"virtual {net}"
        )
    if kind in {"shadow_intent", "scalp_shadow_intent"}:
        approved = "approved" if payload.get("approved") else "rejected"
        return f"{approved}: {payload.get('signal_reason', '')}".strip(": ")
    if kind == "risk_decision":
        return "approved" if payload.get("approved") else ", ".join(
            payload.get("failed_checks") or []
        )
    if kind == "order_intent":
        intent = payload.get("intent") if isinstance(payload.get("intent"), dict) else {}
        return f"{intent.get('side', '')} {intent.get('quantity', '')} {intent.get('symbol', '')}"
    if kind == "order_acknowledged":
        return f"exchange_order_id={payload.get('exchange_order_id', '')}"
    if kind in {"live_paper_exit", "paper_exit", "tick_stop_exit"}:
        return f"{payload.get('reason', kind)} {payload.get('state', '')}".strip()
    if kind == "daily_report":
        return str(payload.get("summary") or "")
    return ", ".join(f"{key}={value}" for key, value in list(payload.items())[:5])


def _with_captured_bps(trade: dict[str, Any]) -> dict[str, Any]:
    """Attach `captured_bps` — how much the trade actually captured, in basis
    points (the fee-wall yardstick). Gross price move when entry+exit prices are
    known (shadow), else net PnL on notional (paper fills). `captured_bps_basis`
    says which.

    NOTE: TP1/TP2/TP3 ladder progress is deliberately absent — the v1 bar-close
    exit engine closes on a SINGLE take-profit price, so partial scale-outs are
    never recorded. Surfacing them needs a runtime change, not a display change.
    """
    entry = _float(trade.get("entry_price"))
    exit_ = _float(trade.get("exit_price"))
    side = str(trade.get("side", "")).lower()
    direction = 1.0 if side in ("long", "buy") else -1.0 if side in ("short", "sell") else 0.0
    captured, basis = None, None
    if entry > 0 and exit_ > 0 and direction != 0.0:
        captured = round((exit_ / entry - 1.0) * direction * 1e4, 1)
        basis = "gross"
    else:
        realized = _float(trade.get("realized_pnl_usd"))
        qty = abs(_float(trade.get("quantity")))
        if realized != 0.0 and exit_ > 0 and qty > 0:
            captured = round(realized / (exit_ * qty) * 1e4, 1)
            basis = "net"
    out = dict(trade)
    out["captured_bps"] = captured
    out["captured_bps_basis"] = basis
    return out


def _intent_economics(
    journal_rows: list[tuple[str, dict[str, Any]]]
) -> tuple[dict[str, dict[str, float]], dict[str, dict[str, float]]]:
    """Leverage + notional from journaled intents so trades can show margin.

    order_intent is keyed by client_order_id (paper trades join on their entry
    order); shadow_intent by intent_key (shadow outcomes join on that)."""
    by_coid: dict[str, dict[str, float]] = {}
    by_key: dict[str, dict[str, float]] = {}
    for _lane, record in journal_rows:
        kind = str(record.get("kind") or "")
        if kind not in ("order_intent", "shadow_intent"):
            continue
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else record
        intent = payload.get("intent") if isinstance(payload.get("intent"), dict) else {}
        if not intent:
            continue
        econ = {
            "leverage": _float(intent.get("leverage")),
            "notional_usd": _float(intent.get("notional_usd")),
        }
        if econ["leverage"] <= 0 and econ["notional_usd"] <= 0:
            continue
        if kind == "order_intent":
            coid = str(payload.get("client_order_id") or intent.get("client_order_id") or "")
            if coid:
                by_coid[coid] = econ
        else:
            key = str(payload.get("intent_key") or "")
            if key:
                by_key[key] = econ
    return by_coid, by_key


def _attach_economics(trade: dict[str, Any], econ: dict[str, float] | None) -> None:
    """Attach leverage / notional / margin to a trade in place. Notional falls
    back to entry_price × quantity when the intent didn't carry it."""
    if not econ:
        return
    lev = _float(econ.get("leverage"))
    notional = _float(econ.get("notional_usd"))
    if notional <= 0:
        notional = _float(trade.get("entry_price")) * abs(_float(trade.get("quantity")))
    trade["leverage"] = round(lev, 2) if lev > 0 else None
    trade["notional_usd"] = round(notional, 2) if notional > 0 else None
    if lev > 0 and notional > 0:
        trade["margin_usd"] = round(notional / lev, 2)


def _trade_net(trade: dict[str, Any]) -> float:
    """A closed trade's net P&L — virtual (shadow) first, then the paper fill's
    net, then raw realized. One place so the view and the rollup agree."""
    for key in ("virtual_net_usd", "net_after_this_fill_fee_usd", "realized_pnl_usd"):
        if trade.get(key) is not None:
            return _float(trade[key])
    return 0.0


_KNOWN_EXCHANGES = ("binanceusdm", "bybit", "delta_india")
_TIMEFRAME_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "1d": 86400,
}


def _lane_exchange(lane_id: str) -> str:
    """The venue a lane runs on, recovered from its id (lanes embed the ccxt
    exchange, e.g. ``..._bybit_...``)."""
    lane = str(lane_id or "")
    for exchange in _KNOWN_EXCHANGES:
        if exchange in lane:
            return exchange
    return ""


def _lane_timeframe_seconds(lane_id: str) -> int:
    lane = str(lane_id or "")
    # longest tokens first so "15m" isn't shadowed by "5m", "4h" not by "1h"
    for tf in ("15m", "30m", "5m", "3m", "1m", "6h", "4h", "2h", "1h", "1d"):
        if f"_{tf}_" in lane or lane.endswith(f"_{tf}"):
            return _TIMEFRAME_SECONDS.get(tf, 0)
    return 0


def _hold_seconds(trade: dict[str, Any]) -> float | None:
    """How long a closed trade was held. Paper: entry fill -> exit fill time.
    Shadow: bars_held x the lane's bar length (no per-fill timestamps there)."""
    entry_dt = _parse_dt(trade.get("entry_ts"))
    exit_dt = _parse_dt(trade.get("ts"))
    if entry_dt is not None and exit_dt is not None and exit_dt >= entry_dt:
        return (exit_dt - entry_dt).total_seconds()
    bars = int(trade.get("bars_held") or 0)
    tf = _lane_timeframe_seconds(str(trade.get("lane", "")))
    if bars > 0 and tf > 0:
        return float(bars * tf)
    return None


def _build_closed_trades(
    fills: list[dict[str, Any]],
    journal_rows: list[tuple[str, dict[str, Any]]],
    virtual_trades: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Reconstruct the closed-trade list: paper trades paired from fills plus
    shadow virtual outcomes, each enriched with its TP ladder, leverage/margin
    and captured bps. Paper trades join the ladder by exit client_order_id and
    the entry intent's economics by entry client_order_id; shadow outcomes join
    their intent by intent_key. Pure projection — no order/timing side effects."""
    exits = _paper_exit_metadata(journal_rows)
    econ_by_coid, econ_by_key = _intent_economics(journal_rows)
    paired = _paired_actual_trades(fills)
    for trade in paired:
        exit_meta, source = _match_paper_exit_metadata(trade, exits)
        if exit_meta:
            _attach_paper_exit_metadata(trade, exit_meta, source)
        _attach_economics(trade, econ_by_coid.get(str(trade.get("entry_client_order_id") or "")))
    for trade in virtual_trades:
        _attach_economics(trade, econ_by_key.get(str(trade.get("intent_key") or "")))
    out = [_with_captured_bps(t) for t in paired + virtual_trades]
    for trade in out:
        trade["exchange"] = trade.get("venue") or _lane_exchange(str(trade.get("lane", "")))
        hold = _hold_seconds(trade)
        if hold is not None:
            trade["hold_seconds"] = round(hold, 1)
    return out


def _lane_pnl_rollup(closed_trades: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per-lane P&L rollup for the journal view: {lane: {closed, net_usd}}."""
    roll: dict[str, dict[str, Any]] = {}
    for trade in closed_trades:
        lane = str(trade.get("lane") or "?")
        entry = roll.setdefault(lane, {"closed": 0, "net_usd": 0.0})
        entry["closed"] += 1
        entry["net_usd"] += _trade_net(trade)
    for entry in roll.values():
        entry["net_usd"] = round(entry["net_usd"], 4)
    return roll


#: Cohort split so the aggregate P&L is read honestly. The headline losses live
#: almost entirely in the DELIBERATE 5m velocity controls, which exist to lose —
#: they feed the meta-labeler mostly-losing fee-walled examples so it learns to
#: reject them. Separating them keeps a scary-looking total from masking the fact
#: that the lanes actually under evaluation are not the bleeders.
_COHORT_ORDER = ("tracked", "research", "control")
_COHORT_LABELS = {
    "tracked": "Tracked candidates",
    "research": "Research net",
    "control": "Deliberate controls",
}
_COHORT_NOTES = {
    "tracked": "human-approved paper + 2nd-eye survivors + fee-wall probes — the promotion pipeline",
    "research": "unvetted exploratory lanes — expected mixed, gated out of promotion",
    "control": "velocity 5m lanes — EXPECTED to lose; ML training fodder, unpromotable",
}


def _lane_cohort(lane_id: str) -> str:
    """Classify a lane into tracked / research / control (see _COHORT_NOTES)."""
    lane = (lane_id or "").lower()
    if lane.startswith("velocity_"):
        return "control"
    if (
        lane.startswith("papertrial_")
        or lane.startswith("evidence_")
        or "_paper_probe" in lane
        or lane.startswith("funding_mr")
    ):
        return "tracked"
    return "research"


def _cohort_pnl_rollup(closed_trades: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """P&L split by cohort so the headline is honest: the deliberate 5m controls
    (which exist to lose) don't get mistaken for the tracked candidates."""
    roll: dict[str, dict[str, Any]] = {
        cohort: {
            "label": _COHORT_LABELS[cohort],
            "note": _COHORT_NOTES[cohort],
            "closed": 0,
            "wins": 0,
            "net_usd": 0.0,
        }
        for cohort in _COHORT_ORDER
    }
    for trade in closed_trades:
        entry = roll[_lane_cohort(str(trade.get("lane") or ""))]
        net = _trade_net(trade)
        entry["closed"] += 1
        entry["wins"] += 1 if net > 0 else 0
        entry["net_usd"] += net
    for entry in roll.values():
        entry["net_usd"] = round(entry["net_usd"], 4)
        entry["win_rate_pct"] = (
            round(entry["wins"] / entry["closed"] * 100, 1) if entry["closed"] else 0.0
        )
    return roll


@dataclass(frozen=True)
class _PaperExitMetadataIndex:
    by_client_order_id: dict[str, dict[str, Any]]
    by_lane: dict[str, list[dict[str, Any]]]


_PAPER_EXIT_METADATA_KINDS = {
    "live_paper_exit",
    "paper_exit",
    "tick_stop_exit",
    "exit_plan_cleared_after_reconciliation",
}


def _paper_exit_metadata(
    journal_rows: list[tuple[str, dict[str, Any]]]
) -> _PaperExitMetadataIndex:
    """Index paper exit metadata by client_order_id and lane/time.

    Modern paper exits write the exit order's client_order_id and join to the
    closing fill exactly. Older tick-stop records predate that field, so we
    also keep a lane-local time index and reconcile them to the nearest close.
    This is still read-only projection: no order state is changed here.
    """
    by_coid: dict[str, dict[str, Any]] = {}
    by_lane: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for lane, record in journal_rows:
        kind = str(record.get("kind") or "")
        if kind not in _PAPER_EXIT_METADATA_KINDS:
            continue
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else record
        meta = _paper_exit_metadata_row(lane, kind, record, payload)
        if meta is None:
            continue
        by_lane[str(lane)].append(meta)
        coid = str(meta.get("client_order_id") or "")
        if not coid:
            continue
        existing = by_coid.get(coid)
        # Prefer the richer live_paper/tick records over reconciliation-only
        # records when both reference the same exit order.
        if existing is None or _metadata_rank(meta) >= _metadata_rank(existing):
            by_coid[coid] = meta
    for rows in by_lane.values():
        rows.sort(key=lambda row: str(row.get("ts") or ""))
    return _PaperExitMetadataIndex(by_client_order_id=by_coid, by_lane=dict(by_lane))


def _paper_exit_metadata_row(
    lane: str,
    kind: str,
    record: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    reason = str(payload.get("reason") or "")
    if not reason and kind == "tick_stop_exit":
        reason = "tick_stop"
    if not reason and kind == "paper_exit":
        reason = "paper_exit"
    ts = _record_ts(record, payload)
    meta = {
        "lane": lane,
        "ts": ts,
        "source_kind": kind,
        "client_order_id": str(payload.get("client_order_id") or ""),
        "reason": reason,
        "resolution": _exit_resolution(reason),
        "state": str(payload.get("state") or ""),
        "levels": payload.get("take_profit_levels") or [],
        "tp_reached": int(_float(payload.get("tp_reached"))),
        "mfe_price": payload.get("mfe_price"),
        "active_stop_price": payload.get("active_stop_price"),
        "breakeven_armed": payload.get("breakeven_armed"),
        "final": payload.get("final"),
    }
    if not meta["client_order_id"] and not meta["ts"]:
        return None
    return meta


def _metadata_rank(meta: Mapping[str, Any]) -> int:
    kind = str(meta.get("source_kind") or "")
    if kind in {"live_paper_exit", "tick_stop_exit"}:
        return 3
    if kind == "paper_exit":
        return 2
    if kind == "exit_plan_cleared_after_reconciliation":
        return 1
    return 0


def _exit_resolution(reason: str) -> str:
    reason = str(reason or "").strip()
    if reason in {"stop", "tick_stop", "breakeven_stop"}:
        return "stop"
    if reason in {"take_profit", "target", "tp"}:
        return "take_profit"
    if reason in {"max_holding", "timeout"}:
        return "timeout"
    return reason


def _match_paper_exit_metadata(
    trade: Mapping[str, Any],
    exits: _PaperExitMetadataIndex,
) -> tuple[dict[str, Any] | None, str]:
    coid = str(trade.get("client_order_id") or "")
    if coid and coid in exits.by_client_order_id:
        return exits.by_client_order_id[coid], "client_order_id"
    lane = str(trade.get("lane") or "")
    candidates = exits.by_lane.get(lane, [])
    if not candidates:
        return None, ""
    entry_dt = _parse_dt(trade.get("entry_ts"))
    exit_dt = _parse_dt(trade.get("ts"))
    tolerance = max(600.0, float(_lane_timeframe_seconds(lane) or 0) * 2.0)
    best: tuple[float, dict[str, Any]] | None = None
    for candidate in candidates:
        event_dt = _parse_dt(candidate.get("ts"))
        if event_dt is None:
            continue
        if exit_dt is None:
            continue
        if entry_dt is not None and event_dt < entry_dt:
            continue
        drift = abs((event_dt - exit_dt).total_seconds())
        if drift > tolerance:
            continue
        if best is None or drift < best[0]:
            best = (drift, candidate)
    if best is None:
        return None, ""
    return best[1], "lane_time_window"


def _attach_paper_exit_metadata(
    trade: dict[str, Any],
    meta: Mapping[str, Any],
    source: str,
) -> None:
    levels = meta.get("levels")
    if isinstance(levels, list):
        trade["take_profit_levels"] = levels
    trade["tp_reached"] = int(_float(meta.get("tp_reached")))
    resolution = str(meta.get("resolution") or "")
    if resolution:
        trade["resolution"] = resolution
    reason = str(meta.get("reason") or "")
    if reason:
        trade["exit_reason"] = reason
    trade["exit_metadata_source"] = source
    trade["exit_metadata_kind"] = meta.get("source_kind")
    if meta.get("state"):
        trade["exit_state"] = meta.get("state")
    for key in ("mfe_price", "active_stop_price", "breakeven_armed"):
        if meta.get(key) is not None:
            trade[key] = meta.get(key)


def _paired_actual_trades(fills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reconstruct COMPLETE paper trades by pairing each opening fill (realized
    PnL 0) with its closing fill (realized != 0) per lane, FIFO.

    A single closing fill only knows the exit price; pairing recovers the ENTRY
    price so the journal shows a real entry -> exit and gross captured bps for
    the paper trials, matching the shadow lanes.
    """
    by_lane: dict[str, list[dict]] = defaultdict(list)
    for fill in fills:
        by_lane[str(fill.get("lane", ""))].append(fill)

    rows: list[dict[str, Any]] = []
    for lane, lane_fills in by_lane.items():
        lane_fills = sorted(lane_fills, key=lambda f: str(f.get("ts", "")))
        open_fifo: list[dict] = []
        for fill in lane_fills:
            realized = _float(fill.get("realized_pnl_usd"))
            if abs(realized) <= 1e-12:
                open_fifo.append(fill)  # opening fill — records the entry
                continue
            entry = open_fifo.pop(0) if open_fifo else None
            entry_price = _float(entry.get("price")) if entry else None
            open_side = str(entry.get("side", "")).lower() if entry else ""
            # trade side = the ENTRY direction (buy opens long, sell opens short)
            side = "long" if open_side == "buy" else "short" if open_side == "sell" else fill.get("side", "")
            fee = _float(fill.get("fee_usd")) + (_float(entry.get("fee_usd")) if entry else 0.0)
            rows.append({
                "lane": lane,
                "ts": fill.get("ts", ""),
                "kind": "actual_closing_fill",
                "symbol": fill.get("symbol", ""),
                "side": side,
                "quantity": fill.get("quantity", 0.0),
                "entry_price": entry_price if entry_price and entry_price > 0 else None,
                "exit_price": fill.get("price", 0.0),
                "realized_pnl_usd": round(realized, 6),
                "fee_usd": round(fee, 6),
                "net_after_this_fill_fee_usd": round(realized - fee, 6),
                "client_order_id": fill.get("client_order_id", ""),
                # the ENTRY order's id — joins to its order_intent for leverage
                "entry_client_order_id": entry.get("client_order_id", "") if entry else "",
                # entry fill time → hold duration (exit ts is `ts`)
                "entry_ts": entry.get("ts", "") if entry else "",
                "venue": fill.get("venue", "") or (entry.get("venue", "") if entry else ""),
                "source": fill.get("source", "fill_ledger"),
            })
    return rows


def _shadow_outcome_trade(lane: str, ts: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "lane": lane,
        "ts": ts,
        "kind": "shadow_outcome",
        "symbol": payload.get("symbol", ""),
        "side": payload.get("side", ""),
        "resolution": payload.get("resolution", ""),
        "entry_price": payload.get("entry_price"),
        "exit_price": payload.get("exit_price"),
        "virtual_net_usd": _float(payload.get("virtual_net_usd")),
        "fees_usd": _float(payload.get("fees_usd")),
        "intent_key": payload.get("intent_key", ""),
        "signal_reason": payload.get("signal_reason", ""),
        "take_profit_levels": payload.get("take_profit_levels") or [],
        "tp_reached": int(payload.get("tp_reached") or 0),
        "bars_held": int(payload.get("bars_held") or 0),
        "source": "decision_journal",
    }


def _scalp_outcome_trade(lane: str, ts: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "lane": lane,
        "ts": ts,
        "kind": "scalp_shadow_outcome",
        "family": payload.get("family", ""),
        "side": payload.get("side", ""),
        "resolution": payload.get("resolution", ""),
        "entry_price": payload.get("entry_price", payload.get("taker_entry_price")),
        "exit_price": payload.get("exit_price", payload.get("taker_exit_price")),
        "virtual_net_usd": _float(payload.get("virtual_net_usd")),
        "taker_net_usd": payload.get("taker_net_usd"),
        "maker_net_usd": payload.get("maker_net_usd"),
        "taker_net_bps": payload.get("taker_net_bps"),
        "maker_net_bps": payload.get("maker_net_bps"),
        "maker_filled": payload.get("maker_filled"),
        "intent_key": payload.get("intent_key", ""),
        "source": "decision_journal",
    }


def _snapshot_positions(snapshot: dict[str, Any], lane: str) -> list[dict[str, Any]]:
    if lane and snapshot.get("lane_id") != lane:
        return []
    lane_id = str(snapshot.get("lane_id") or "primary")
    rows = []
    for pos in snapshot.get("positions") or []:
        if isinstance(pos, dict):
            rows.append({"lane": lane_id, **pos, "source": "snapshot"})
    return rows


def _snapshot_orders(snapshot: dict[str, Any], lane: str) -> list[dict[str, Any]]:
    if lane and snapshot.get("lane_id") != lane:
        return []
    lane_id = str(snapshot.get("lane_id") or "primary")
    rows = []
    for order in snapshot.get("open_orders") or []:
        if isinstance(order, dict):
            rows.append(
                {
                    "lane": lane_id,
                    "ts": snapshot.get("ts", ""),
                    "state": order.get("state", "open"),
                    "last_event": "snapshot_open_order",
                    "source": "snapshot",
                    **order,
                }
            )
    return rows


def _snapshot_fills(
    snapshot: dict[str, Any], lane: str, since: datetime | None
) -> list[dict[str, Any]]:
    if lane and snapshot.get("lane_id") != lane:
        return []
    lane_id = str(snapshot.get("lane_id") or "primary")
    ts = str(snapshot.get("ts") or "")
    if not _after_since(ts, since):
        return []
    rows = []
    for fill in snapshot.get("recent_fills") or []:
        if isinstance(fill, dict):
            rows.append({"lane": lane_id, "ts": ts, "source": "snapshot", **fill})
    return rows


def _snapshot_events(
    snapshot: dict[str, Any], lane: str, since: datetime | None
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for lane_id, log in _snapshot_trade_logs(snapshot, lane):
        for event in log:
            if not isinstance(event, dict):
                continue
            ts = str(event.get("ts") or "")
            if not _after_since(ts, since):
                continue
            events.append(
                {
                    "lane": lane_id,
                    "ts": ts,
                    "event": event.get("event", ""),
                    "detail": event.get("detail", ""),
                    "source": "snapshot_trade_log",
                }
            )
    return events


def _snapshot_trade_logs(
    snapshot: dict[str, Any], lane: str
) -> Iterable[tuple[str, list[dict[str, Any]]]]:
    if lane:
        if snapshot.get("lane_id") == lane:
            session = snapshot.get("session") if isinstance(snapshot.get("session"), dict) else {}
            yield lane, [e for e in session.get("trade_log") or [] if isinstance(e, dict)]
        for entry in snapshot.get("lanes") or []:
            if isinstance(entry, dict) and entry.get("lane_id") == lane:
                yield lane, [e for e in entry.get("trade_log") or [] if isinstance(e, dict)]
        return
    session = snapshot.get("session") if isinstance(snapshot.get("session"), dict) else {}
    if session.get("trade_log"):
        yield str(snapshot.get("lane_id") or "primary"), [
            e for e in session.get("trade_log") or [] if isinstance(e, dict)
        ]
    for entry in snapshot.get("lanes") or []:
        if isinstance(entry, dict):
            yield str(entry.get("lane_id") or "?"), [
                e for e in entry.get("trade_log") or [] if isinstance(e, dict)
            ]


def _merge_snapshot_orders(
    journal_orders: list[dict[str, Any]], snapshot_orders: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_id = {str(row.get("client_order_id") or ""): row for row in journal_orders}
    anonymous: list[dict[str, Any]] = []
    for order in snapshot_orders:
        coid = str(order.get("client_order_id") or "")
        if coid and coid in by_id:
            by_id[coid].update(
                {
                    "state": order.get("state", by_id[coid].get("state")),
                    "source": f"{by_id[coid].get('source', 'journal')}+snapshot",
                    "snapshot_open": True,
                }
            )
        elif coid:
            by_id[coid] = order
        else:
            anonymous.append(order)
    return list(by_id.values()) + anonymous


def _is_open_order(row: dict[str, Any]) -> bool:
    state = str(row.get("state") or "").lower()
    return state in {
        "open",
        "acknowledged",
        "partially_filled",
        "timeout_unknown",
        "reconciling",
        "intent_created",
        "risk_approved",
    }


def _sort_recent(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: str(row.get("ts") or ""), reverse=True)


def _lane_counts(snapshot: dict[str, Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for lane in snapshot.get("lanes") or []:
        if isinstance(lane, dict):
            out[str(lane.get("lane_id") or "?")] = int(_float(lane.get("positions")))
    return out


def _primary_lane(history_path: Path | str | None) -> str:
    if history_path is None:
        return "primary"
    name = Path(history_path).name
    return name.removesuffix(".equity.jsonl") if name.endswith(".equity.jsonl") else "primary"
