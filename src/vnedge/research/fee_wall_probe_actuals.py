"""Join fee-wall paper-probe manifests to actual paper outcomes.

The fee-wall bridge publishes which scanner candidates deserve live-data PAPER
sample expansion. This module answers the operator's next question: did those
probes actually launch, and what have their paper journals/fill ledgers
produced so far?

Read-only. It cannot start lanes, promote candidates, or place orders.
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
import json
from pathlib import Path
import time
from typing import Any


DEFAULT_RESEARCH_DIR = Path("research/live_research")
DEFAULT_MANIFEST = DEFAULT_RESEARCH_DIR / "fee_wall_paper_probes.json"
DEFAULT_PERFORMANCE = DEFAULT_RESEARCH_DIR / "paper_lane_performance_latest.json"
DEFAULT_ROUTE_DOCTOR = DEFAULT_RESEARCH_DIR / "paper_route_doctor_latest.json"
DEFAULT_OUT = DEFAULT_RESEARCH_DIR / "fee_wall_probe_actuals_latest.json"
DEFAULT_FEED = DEFAULT_RESEARCH_DIR / "fee_wall_probe_actuals_feed.jsonl"

STATE_PAPER_PROMOTION_CANDIDATE = "PAPER_PROMOTION_CANDIDATE"
STATE_PAPER_ACTIVE_PROFITABLE = "PAPER_ACTIVE_PROFITABLE"
STATE_PAPER_ACTIVE_NEGATIVE = "PAPER_ACTIVE_NEGATIVE"
STATE_ONLINE_NO_TRADES = "ONLINE_NO_TRADES"
STATE_JOURNAL_STALE_OR_MISSING = "JOURNAL_STALE_OR_MISSING"
STATE_NOT_LAUNCHED = "NOT_LAUNCHED"


def build_fee_wall_probe_actuals(
    *,
    manifest: Mapping[str, Any],
    performance: Mapping[str, Any] | None = None,
    route_doctor: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return paper outcome status for every published fee-wall probe."""

    generated = now or datetime.now(UTC)
    probes = [
        dict(row)
        for row in manifest.get("paper_probes", []) or []
        if isinstance(row, Mapping)
    ]
    perf_by_lane = {
        str(row.get("lane_id") or ""): row
        for row in (performance or {}).get("rows", []) or []
        if isinstance(row, Mapping) and row.get("lane_id")
    }
    doctor_by_lane = _doctor_index(route_doctor or {})

    rows = [
        _actual_row(probe, perf_by_lane=perf_by_lane, doctor_by_lane=doctor_by_lane)
        for probe in probes
    ]
    rows.sort(key=_row_sort_key)
    summary = _summary(rows)
    return {
        "generated_at": generated.isoformat(),
        "report_id": "fee_wall_probe_actuals_v1",
        "mode": "read_only_paper_probe_actuals",
        "source_manifest_id": manifest.get("manifest_id"),
        "source_generated_at": manifest.get("generated_at"),
        "summary": summary,
        "rows": rows,
        "operator_answer": _operator_answer(summary),
        "policy": {
            "read_only": True,
            "paper_only": True,
            "can_trade": False,
            "can_promote": False,
            "promotion_requires_untouched_judgment": True,
        },
        "can_trade": False,
        "can_promote": False,
    }


def publish_fee_wall_probe_actuals(
    payload: Mapping[str, Any], *, out: Path | str = DEFAULT_OUT, feed: Path | str | None = DEFAULT_FEED
) -> None:
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    tmp.replace(out_path)
    out_path.chmod(0o644)
    if feed is not None:
        feed_path = Path(feed)
        feed_path.parent.mkdir(parents=True, exist_ok=True)
        with feed_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_feed_record(payload), sort_keys=True, default=str) + "\n")
        feed_path.chmod(0o644)


def render_report(payload: Mapping[str, Any], *, limit: int = 20) -> str:
    summary = payload.get("summary", {})
    lines = [
        "=== Fee-wall paper-probe actuals ===",
        f"generated: {payload.get('generated_at')}",
        str(payload.get("operator_answer") or ""),
        (
            "summary: "
            f"{summary.get('published_probes', 0)} probes, "
            f"{summary.get('closed_trade_probes', 0)} with closed trades, "
            f"{summary.get('profitable_probes', 0)} profitable, "
            f"{summary.get('negative_probes', 0)} negative, "
            f"net ${summary.get('net_pnl_usd', 0.0):.2f}"
        ),
    ]
    for row in list(payload.get("rows", []) or [])[:limit]:
        lines.append(
            f"  {row.get('actual_state', ''):<28} {row.get('lane_id', ''):<62} "
            f"{row.get('closed_trades', 0):>3} trades "
            f"net ${row.get('net_pnl_usd', 0.0):>8.2f} "
            f"PF {row.get('profit_factor', 0.0):>5.2f} "
            f"{row.get('next_action', '')}"
        )
    lines.append("read-only: can_trade=false can_promote=false")
    return "\n".join(lines)


def _actual_row(
    probe: Mapping[str, Any],
    *,
    perf_by_lane: Mapping[str, Mapping[str, Any]],
    doctor_by_lane: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    exchange = str(probe.get("exchange") or "")
    symbol = _delta_india_symbol(exchange, str(probe.get("symbol") or ""))
    timeframe = str(probe.get("timeframe") or "")
    strategy = str(probe.get("strategy") or probe.get("strategy_id") or "")
    lane_id = _fee_wall_probe_lane_id(exchange, symbol, timeframe, strategy)
    perf = perf_by_lane.get(lane_id)
    doctor = doctor_by_lane.get(lane_id)
    state, next_action = _actual_state(perf, doctor)
    return {
        "probe_id": probe.get("probe_id") or _probe_id(exchange, symbol, timeframe, strategy),
        "lane_id": lane_id,
        "rank": probe.get("rank"),
        "exchange": exchange,
        "symbol": symbol,
        "timeframe": timeframe,
        "strategy": strategy,
        "strategy_id": strategy,
        "verdict": probe.get("verdict"),
        "expected_avg_net_bps": _float_or_none(probe.get("avg_selected_net_bps")),
        "expected_profit_factor": _float_or_none(probe.get("profit_factor")),
        "expected_routed": int(_float(probe.get("routed"))),
        "expected_fee_wall_break_rate_pct": _float_or_none(probe.get("fee_wall_break_rate_pct")),
        "paper_margin_usd": _float_or_none(probe.get("paper_margin_usd")),
        "paper_leverage": _float_or_none(probe.get("paper_leverage")),
        "actual_state": state,
        "next_action": next_action,
        "doctor_state": doctor.get("doctor_state") if doctor else None,
        "journal_seen": bool(doctor.get("journal_seen")) if doctor else bool(perf),
        "latest_why_no_trade": (perf or {}).get("latest_why_no_trade") or (doctor or {}).get("latest_why"),
        "latest_ts": (perf or {}).get("latest_ts") or (doctor or {}).get("latest_ts"),
        "age_hours": (perf or {}).get("age_hours") or (doctor or {}).get("age_hours"),
        "closed_trades": int(_float((perf or {}).get("closed_trades"))),
        "live_signals": int(_float((perf or {}).get("live_signals"))),
        "order_intents": int(_float((perf or {}).get("paper_order_intents"))),
        "fills": int(_float((perf or {}).get("fills"))),
        "net_pnl_usd": round(_float((perf or {}).get("net_pnl_usd")), 6),
        "closed_net_pnl_usd": round(_float((perf or {}).get("closed_net_pnl_usd")), 6),
        "fees_usd": round(_float((perf or {}).get("fees_usd")), 6),
        "profit_factor": _float((perf or {}).get("profit_factor")),
        "win_rate": (perf or {}).get("win_rate"),
        "avg_closed_trade_net_bps": (perf or {}).get("avg_closed_trade_net_bps"),
        "journal_drift_flags": list((perf or {}).get("journal_drift_flags") or []),
        "can_trade": False,
        "can_promote": False,
    }


def _actual_state(
    perf: Mapping[str, Any] | None, doctor: Mapping[str, Any] | None
) -> tuple[str, str]:
    if perf:
        perf_state = str(perf.get("state") or "")
        if perf_state == "PAPER_PROMOTION_CANDIDATE":
            return STATE_PAPER_PROMOTION_CANDIDATE, "human review only; still needs untouched judgment"
        if perf_state == "PAPER_ACTIVE_PROFITABLE":
            return STATE_PAPER_ACTIVE_PROFITABLE, "keep collecting paper sample; no promotion yet"
        if perf_state == "PAPER_ACTIVE_NEGATIVE":
            return STATE_PAPER_ACTIVE_NEGATIVE, "mine entry/exit failures; do not promote"
        if perf_state == "PAPER_ONLINE_NO_TRADES":
            return STATE_ONLINE_NO_TRADES, "wait for sample or inspect why-no-trade"
        return STATE_JOURNAL_STALE_OR_MISSING, str(perf.get("next_action") or "repair stale/missing paper proof")
    if doctor:
        doctor_state = str(doctor.get("doctor_state") or "")
        if doctor_state == "JOURNAL_ACTIVE":
            return STATE_ONLINE_NO_TRADES, "journal exists; wait for first closed paper trade"
        if doctor_state in {"JOURNAL_STALE", "ROUTE_READY_JOURNAL_MISSING"}:
            return STATE_JOURNAL_STALE_OR_MISSING, str(doctor.get("next_action") or "repair paper journal proof")
    return STATE_NOT_LAUNCHED, "restart/reload multi-lane after manifest change or inspect probe route"


def _doctor_index(payload: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    out: dict[str, Mapping[str, Any]] = {}
    for row in payload.get("rows", []) or []:
        if not isinstance(row, Mapping):
            continue
        for key in [row.get("expected_lane_id"), row.get("trial_id"), row.get("lane_key")]:
            if key:
                out[str(key)] = row
        for key in row.get("route_lane_ids") or []:
            if key:
                out[str(key)] = row
    return out


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    states = Counter(str(row.get("actual_state") or "") for row in rows)
    closed_rows = [row for row in rows if int(row.get("closed_trades") or 0) > 0]
    return {
        "published_probes": len(rows),
        "launched_probes": len(rows) - states[STATE_NOT_LAUNCHED],
        "closed_trade_probes": len(closed_rows),
        "profitable_probes": states[STATE_PAPER_ACTIVE_PROFITABLE] + states[STATE_PAPER_PROMOTION_CANDIDATE],
        "negative_probes": states[STATE_PAPER_ACTIVE_NEGATIVE],
        "online_no_trades": states[STATE_ONLINE_NO_TRADES],
        "stale_or_missing": states[STATE_JOURNAL_STALE_OR_MISSING],
        "not_launched": states[STATE_NOT_LAUNCHED],
        "promotion_candidates": states[STATE_PAPER_PROMOTION_CANDIDATE],
        "closed_trades": sum(int(row.get("closed_trades") or 0) for row in rows),
        "live_signals": sum(int(row.get("live_signals") or 0) for row in rows),
        "order_intents": sum(int(row.get("order_intents") or 0) for row in rows),
        "fills": sum(int(row.get("fills") or 0) for row in rows),
        "net_pnl_usd": round(sum(_float(row.get("net_pnl_usd")) for row in rows), 6),
        "closed_net_pnl_usd": round(sum(_float(row.get("closed_net_pnl_usd")) for row in rows), 6),
        "fees_usd": round(sum(_float(row.get("fees_usd")) for row in rows), 6),
        "state_counts": dict(sorted(states.items())),
        "can_trade": False,
        "can_promote": False,
    }


def _operator_answer(summary: Mapping[str, Any]) -> str:
    candidates = int(summary.get("promotion_candidates") or 0)
    profitable = int(summary.get("profitable_probes") or 0)
    negative = int(summary.get("negative_probes") or 0)
    no_trades = int(summary.get("online_no_trades") or 0)
    missing = int(summary.get("stale_or_missing") or 0) + int(summary.get("not_launched") or 0)
    if candidates:
        return f"{candidates} fee-wall probe(s) need human review; no live promotion is automatic."
    if negative:
        return f"{negative} fee-wall probe(s) are already negative in paper; mine exits/entry drift before adding capital."
    if profitable:
        return f"{profitable} fee-wall probe(s) are positive but still need sample and untouched judgment."
    if no_trades:
        return f"{no_trades} fee-wall probe(s) are online but have no closed paper trades yet."
    if missing:
        return f"{missing} fee-wall probe(s) lack fresh paper proof; reload or repair the runner first."
    return "No fee-wall paper probes are published right now."


def _row_sort_key(row: Mapping[str, Any]) -> tuple[int, float, int, str]:
    priority = {
        STATE_PAPER_PROMOTION_CANDIDATE: 0,
        STATE_PAPER_ACTIVE_PROFITABLE: 1,
        STATE_PAPER_ACTIVE_NEGATIVE: 2,
        STATE_ONLINE_NO_TRADES: 3,
        STATE_JOURNAL_STALE_OR_MISSING: 4,
        STATE_NOT_LAUNCHED: 5,
    }.get(str(row.get("actual_state") or ""), 9)
    return (
        priority,
        -_float(row.get("net_pnl_usd")),
        int(_float(row.get("rank"), 999)),
        str(row.get("lane_id") or ""),
    )


def _feed_record(payload: Mapping[str, Any]) -> dict[str, Any]:
    summary = payload.get("summary", {}) if isinstance(payload.get("summary"), Mapping) else {}
    return {
        "generated_at": payload.get("generated_at"),
        "report_id": payload.get("report_id"),
        "published_probes": summary.get("published_probes"),
        "closed_trade_probes": summary.get("closed_trade_probes"),
        "profitable_probes": summary.get("profitable_probes"),
        "negative_probes": summary.get("negative_probes"),
        "net_pnl_usd": summary.get("net_pnl_usd"),
        "can_trade": False,
        "can_promote": False,
    }


def _read_json_payload(path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback
    return payload if isinstance(payload, dict) else fallback


def _fee_wall_probe_lane_id(exchange: str, symbol: str, timeframe: str, strategy_id: str) -> str:
    strategy_slug = strategy_id.removesuffix("_v1")
    return f"fee_wall_{strategy_slug}_{exchange}_{_slug_symbol(symbol)}_{timeframe}_paper_probe"


def _probe_id(exchange: str, symbol: str, timeframe: str, strategy: str) -> str:
    return f"{strategy}__{exchange}__{_slug_symbol(symbol)}__{timeframe}"


def _delta_india_symbol(exchange: str, symbol: str) -> str:
    if exchange == "delta_india" and "/USDT" in symbol:
        base = symbol.split("/", maxsplit=1)[0]
        return f"{base}/USD:USD"
    return symbol


def _slug_symbol(symbol: str) -> str:
    return symbol.lower().replace("/", "_").replace(":", "_").replace("-", "_")


def _float(value: object, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed != parsed:
        return default
    return parsed


def _float_or_none(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed:
        return None
    return parsed


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--performance", type=Path, default=DEFAULT_PERFORMANCE)
    parser.add_argument("--route-doctor", type=Path, default=DEFAULT_ROUTE_DOCTOR)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--feed", type=Path, default=DEFAULT_FEED)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval-seconds", type=float, default=60.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    while True:
        payload = build_fee_wall_probe_actuals(
            manifest=_read_json_payload(args.manifest, {"paper_probes": []}),
            performance=_read_json_payload(args.performance, {"rows": []}),
            route_doctor=_read_json_payload(args.route_doctor, {"rows": []}),
        )
        publish_fee_wall_probe_actuals(payload, out=args.out, feed=args.feed)
        print(render_report(payload), flush=True)
        if args.once:
            return 0
        time.sleep(max(1.0, float(args.interval_seconds)))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
