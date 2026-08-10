"""Paper-lane activation truth board.

This module answers a narrower question than lane promotion readiness:

    "Which lanes are actually approved and wired for live-data paper trading?"

It is deliberately read-only. It reconciles paper-trial manifests, runtime
paper-lane routes, current paper journals, lane-readiness evidence, and the
real-time scanner artifact. It never starts, promotes, edits, or trades a lane.
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from vnedge.config.risk_config import ABSOLUTE_MAX_LEVERAGE, HIGH_LEVERAGE_THRESHOLD
from vnedge.exchange.venue_specs import venue_symbol_limits
from vnedge.strategy.strategy_registry import get_strategy_class

DEFAULT_RESEARCH_DIR = Path("research/live_research")
DEFAULT_READINESS = DEFAULT_RESEARCH_DIR / "lane_promotion_readiness_latest.json"
DEFAULT_SCANNER = DEFAULT_RESEARCH_DIR / "realtime_scanner_latest.json"
DEFAULT_MANIFEST_DIR = Path("research/paper_trials")
DEFAULT_JOURNAL_DIR = Path("logs/paper_trials")
DEFAULT_OUT = DEFAULT_RESEARCH_DIR / "paper_lane_activation_latest.json"
DEFAULT_FEED = DEFAULT_RESEARCH_DIR / "paper_lane_activation_feed.jsonl"

ACTIVATION_PAPER_RUNNING = "PAPER_RUNNING"
ACTIVATION_PAPER_ONLINE_WAITING = "PAPER_ONLINE_WAITING"
ACTIVATION_ROUTE_READY_NO_JOURNAL = "PAPER_ROUTE_READY_NO_JOURNAL"
ACTIVATION_NEEDS_HUMAN_APPROVAL = "NEEDS_HUMAN_PAPER_APPROVAL"
ACTIVATION_ROUTE_BLOCKED = "ROUTE_BLOCKED"
ACTIVATION_MANIFEST_UNSAFE = "MANIFEST_UNSAFE"
ACTIVATION_BLOCKED_NEGATIVE = "BLOCKED_NEGATIVE_EDGE"
ACTIVATION_NEEDS_ADAPTER = "NEEDS_RUNTIME_ADAPTER"
ACTIVATION_OBSERVE_ONLY = "OBSERVE_ONLY"

ROUTE_READY = "ROUTE_READY"
ROUTE_RUNNING = "ROUTE_RUNNING"
ROUTE_BLOCKED = "ROUTE_BLOCKED"
ROUTE_UNPROVEN = "ROUTE_UNPROVEN"

STATUS_PAPER_ACTIVE = "PAPER_ACTIVE"
STATUS_PAPER_WAITING = "PAPER_WAITING_FOR_SIGNAL"
STATUS_PAPER_REVIEW_READY = "PAPER_REVIEW_READY"


@dataclass(frozen=True)
class PaperLaneActivationConfig:
    """Operator paper experiment request.

    The values below model the user's current paper sizing question. They do
    not alter the frozen paper manifests or the live runtime.
    """

    requested_margin_usd: float = 100.0
    requested_leverage: float = 25.0
    live_margin_usd: float = 100.0
    live_leverage: float = 5.0
    high_leverage_ack: bool = False
    max_rows: int = 160

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_paper_lane_activation(
    *,
    readiness: Mapping[str, Any] | None = None,
    scanner: Mapping[str, Any] | None = None,
    readiness_path: Path | str = DEFAULT_READINESS,
    scanner_path: Path | str = DEFAULT_SCANNER,
    manifest_dir: Path | str = DEFAULT_MANIFEST_DIR,
    journal_dir: Path | str = DEFAULT_JOURNAL_DIR,
    desired_specs: Iterable[Mapping[str, Any] | Any] | None = None,
    config: PaperLaneActivationConfig = PaperLaneActivationConfig(),
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a paper activation report from persisted operator artifacts."""
    now = now or datetime.now(UTC)
    readiness_path = Path(readiness_path)
    scanner_path = Path(scanner_path)
    manifest_dir = Path(manifest_dir)
    journal_dir = Path(journal_dir)

    readiness_payload = (
        dict(readiness)
        if isinstance(readiness, Mapping)
        else _read_json_payload(readiness_path, {"rows": [], "summary": {}})
    )
    scanner_payload = (
        dict(scanner)
        if isinstance(scanner, Mapping)
        else _read_json_payload(scanner_path, {"rows": [], "summary": {}})
    )
    manifest_candidates = _load_manifest_candidates(manifest_dir)
    route_index, route_errors = _desired_route_index(desired_specs)
    readiness_index = _index_rows(readiness_payload.get("rows", []))
    scanner_index = _index_rows(scanner_payload.get("rows", []))
    journal_index = _journal_index(journal_dir)

    rows: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    for manifest in manifest_candidates:
        row = _manifest_activation_row(
            manifest,
            readiness_index=readiness_index,
            scanner_index=scanner_index,
            route_index=route_index,
            journal_index=journal_index,
            config=config,
        )
        rows.append(row)
        seen_keys.add(row["lane_key"])

    for row in _readiness_only_rows(
        readiness_payload.get("rows", []),
        seen_keys=seen_keys,
        scanner_index=scanner_index,
        route_index=route_index,
        journal_index=journal_index,
        config=config,
    ):
        rows.append(row)
        seen_keys.add(row["lane_key"])

    rows.sort(key=_row_sort_key)
    rows = rows[: max(1, int(config.max_rows))]
    summary = _summary(rows, manifest_candidates=manifest_candidates, route_errors=route_errors)
    return {
        "generated_at": now.isoformat(),
        "report_id": "paper_lane_activation_v1",
        "mode": "read_only_activation_truth",
        "policy": {
            "status": "read_only",
            "can_trade": False,
            "can_promote": False,
            "live_orders_allowed": False,
            "requires_manifest": True,
            "requires_runtime_route": True,
            "requires_journal_evidence_for_active": True,
            "dashboard_inputs_are_plan_only": True,
        },
        "risk_limits": {
            "high_leverage_threshold": HIGH_LEVERAGE_THRESHOLD,
            "absolute_max_leverage": ABSOLUTE_MAX_LEVERAGE,
        },
        "config": config.to_dict(),
        "inputs": {
            "readiness_path": str(readiness_path),
            "scanner_path": str(scanner_path),
            "manifest_dir": str(manifest_dir),
            "journal_dir": str(journal_dir),
        },
        "summary": summary,
        "boards": _boards(rows),
        "rows": rows,
        "operator_answer": _operator_answer(summary),
        "can_trade": False,
        "can_promote": False,
    }


def publish_paper_lane_activation(
    payload: Mapping[str, Any], out: Path, feed: Path | None = None
) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    tmp.replace(out)
    if feed is not None:
        feed.parent.mkdir(parents=True, exist_ok=True)
        with open(feed, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, default=str) + "\n")


def render_report(payload: Mapping[str, Any], *, limit: int = 40) -> str:
    summary = payload.get("summary", {})
    lines = [
        "=== Paper lane activation ===",
        f"generated: {payload.get('generated_at')}",
        str(payload.get("operator_answer") or ""),
        (
            "summary: "
            f"{summary.get('total_rows', 0)} rows, "
            f"{summary.get('paper_running', 0)} running, "
            f"{summary.get('paper_waiting', 0)} waiting, "
            f"{summary.get('route_ready_no_journal', 0)} route-ready/no-journal, "
            f"{summary.get('needs_human_approval', 0)} need approval, "
            f"{summary.get('route_blocked', 0)} route-blocked"
        ),
    ]
    for row in list(payload.get("rows", []))[:limit]:
        lines.append(
            f"  {row.get('activation_state', ''):<32} "
            f"{row.get('exchange', ''):<14} {row.get('symbol', ''):<14} "
            f"{row.get('timeframe', ''):<3} {row.get('strategy_id', ''):<28} "
            f"{row.get('next_action', '')}"
        )
    lines.append("read-only: can_trade=false can_promote=false")
    return "\n".join(lines)


def _manifest_activation_row(
    manifest: Mapping[str, Any],
    *,
    readiness_index: Mapping[str, dict[str, Any]],
    scanner_index: Mapping[str, dict[str, Any]],
    route_index: Mapping[str, list[dict[str, Any]]],
    journal_index: Mapping[str, dict[str, Any]],
    config: PaperLaneActivationConfig,
) -> dict[str, Any]:
    strategy_id = str(manifest.get("strategy_id") or "")
    exchange = str(manifest.get("exchange") or "")
    symbol = str(manifest.get("symbol") or "")
    timeframe = str(manifest.get("timeframe") or "")
    lane_key = _identity(strategy_id, exchange, symbol, timeframe)
    readiness_row = readiness_index.get(lane_key)
    scanner_row = scanner_index.get(lane_key)
    route_matches = route_index.get(lane_key, [])
    journal = journal_index.get(lane_key) or _journal_by_trial(journal_index, manifest)

    route_checks = _route_checks(
        manifest=manifest,
        strategy_id=strategy_id,
        route_matches=route_matches,
        journal=journal,
    )
    activation_state, route_status, blockers, next_action = _activation_state(
        manifest=manifest,
        readiness_row=readiness_row,
        scanner_row=scanner_row,
        route_checks=route_checks,
        journal=journal,
    )
    sizing_profiles = _sizing_profiles(manifest, config)
    requested = sizing_profiles["paper"]

    return {
        "row_type": "paper_manifest_candidate",
        "lane_key": lane_key,
        "trial_id": manifest.get("trial_id"),
        "manifest_id": manifest.get("manifest_id"),
        "manifest_path": manifest.get("manifest_path"),
        "exchange": exchange,
        "symbol": symbol,
        "timeframe": timeframe,
        "strategy_id": strategy_id,
        "mode": "paper",
        "activation_state": activation_state,
        "route_status": route_status,
        "paper_decision": _paper_decision(activation_state, requested),
        "route_checks": route_checks,
        "requested_experiment": requested,
        "sizing_profiles": sizing_profiles,
        "runtime": {
            "desired_lane_ids": [str(r.get("lane_id") or "") for r in route_matches],
            "journal": journal,
            "readiness_status": readiness_row.get("status") if readiness_row else None,
            "scanner_state": scanner_row.get("state") if scanner_row else None,
            "scanner_why": scanner_row.get("why") if scanner_row else None,
        },
        "evidence": _merged_evidence(readiness_row, scanner_row, journal),
        "blockers": blockers,
        "next_action": next_action,
        "can_trade": False,
        "can_promote": False,
    }


def _readiness_only_rows(
    rows: Iterable[Any],
    *,
    seen_keys: set[str],
    scanner_index: Mapping[str, dict[str, Any]],
    route_index: Mapping[str, list[dict[str, Any]]],
    journal_index: Mapping[str, dict[str, Any]],
    config: PaperLaneActivationConfig,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for source in rows:
        if not isinstance(source, Mapping):
            continue
        strategy_id = str(source.get("strategy_id") or source.get("family") or "")
        exchange = str(source.get("exchange") or "")
        symbol = str(source.get("symbol") or "")
        timeframe = str(source.get("timeframe") or "")
        lane_key = _identity(strategy_id, exchange, symbol, timeframe)
        if lane_key in seen_keys:
            continue
        if not strategy_id or not symbol:
            continue
        scanner_row = scanner_index.get(lane_key)
        route_matches = route_index.get(lane_key, [])
        journal = journal_index.get(lane_key)
        pseudo_manifest = {
            "trial_id": source.get("trial_id") or source.get("lane_id"),
            "manifest_id": None,
            "approved_by": None,
            "live_orders_enabled": False,
            "max_leverage": None,
            "starting_equity": None,
            "daily_loss_limit_usd": None,
            "exchange": exchange,
            "symbol": symbol,
            "timeframe": timeframe,
            "strategy_id": strategy_id,
        }
        route_checks = _route_checks(
            manifest=pseudo_manifest,
            strategy_id=strategy_id,
            route_matches=route_matches,
            journal=journal,
        )
        activation_state, route_status, blockers, next_action = _readiness_activation(
            source, route_checks=route_checks, journal=journal
        )
        out.append({
            "row_type": "readiness_observed_lane",
            "lane_key": lane_key,
            "trial_id": source.get("trial_id") or source.get("lane_id"),
            "manifest_id": None,
            "manifest_path": None,
            "exchange": exchange,
            "symbol": symbol,
            "timeframe": timeframe,
            "strategy_id": strategy_id,
            "mode": str(source.get("mode") or "observed"),
            "activation_state": activation_state,
            "route_status": route_status,
            "paper_decision": _paper_decision(activation_state, None),
            "route_checks": route_checks,
            "requested_experiment": _requested_experiment(pseudo_manifest, config),
            "sizing_profiles": _sizing_profiles(pseudo_manifest, config),
            "runtime": {
                "desired_lane_ids": [str(r.get("lane_id") or "") for r in route_matches],
                "journal": journal,
                "readiness_status": source.get("status"),
                "scanner_state": scanner_row.get("state") if scanner_row else None,
                "scanner_why": scanner_row.get("why") if scanner_row else None,
            },
            "evidence": _merged_evidence(source, scanner_row, journal),
            "blockers": blockers,
            "next_action": next_action,
            "can_trade": False,
            "can_promote": False,
        })
    return out


def _activation_state(
    *,
    manifest: Mapping[str, Any],
    readiness_row: Mapping[str, Any] | None,
    scanner_row: Mapping[str, Any] | None,
    route_checks: Mapping[str, Any],
    journal: Mapping[str, Any] | None,
) -> tuple[str, str, list[str], str]:
    if route_checks.get("manifest_safe") is False:
        return (
            ACTIVATION_MANIFEST_UNSAFE,
            ROUTE_BLOCKED,
            ["manifest enables live orders; paper activation refuses it"],
            "fix manifest: live_orders_enabled must remain false for paper",
        )
    if route_checks.get("strategy_registered") is False:
        return (
            ACTIVATION_ROUTE_BLOCKED,
            ROUTE_BLOCKED,
            [f"strategy is not registered: {manifest.get('strategy_id')}"],
            "register the strategy or remove the manifest candidate",
        )
    if route_checks.get("desired_paper_route") is False:
        return (
            ACTIVATION_ROUTE_BLOCKED,
            ROUTE_BLOCKED,
            ["approved paper manifest is not present in the runtime paper lane roster"],
            "wire this manifest into the multi-lane paper roster and restart",
        )

    status = str(readiness_row.get("status") if readiness_row else "")
    journal_orders = int((journal or {}).get("paper_order_intents") or 0)
    journal_evals = int((journal or {}).get("evals") or 0)
    journal_heartbeats = int((journal or {}).get("paper_lane_heartbeats") or 0)
    scanner_state = str(scanner_row.get("state") if scanner_row else "")
    scanner_why = str(scanner_row.get("why") if scanner_row else "")

    if status == STATUS_PAPER_ACTIVE or journal_orders > 0:
        return (
            ACTIVATION_PAPER_RUNNING,
            ROUTE_RUNNING,
            [],
            "monitor paper PnL, exits, drawdown, and journal health",
        )
    if status == STATUS_PAPER_WAITING or journal_evals > 0 or journal_heartbeats > 0:
        why = scanner_why or str((journal or {}).get("latest_why") or "waiting for next strategy signal")
        return (
            ACTIVATION_PAPER_ONLINE_WAITING,
            ROUTE_RUNNING,
            [why],
            "leave paper lane online; inspect scanner blocker if it stays silent",
        )
    if scanner_state in {"FIRING", "NEAR_TRIGGER"}:
        return (
            ACTIVATION_ROUTE_READY_NO_JOURNAL,
            ROUTE_READY,
            ["scanner sees pressure but no paper journal has appeared yet"],
            "verify paper runner container has this lane and journal write access",
        )
    return (
        ACTIVATION_ROUTE_READY_NO_JOURNAL,
        ROUTE_READY,
        ["approved and routed, but no paper journal evidence yet"],
        "restart or inspect multi-lane paper runner; expected journal is missing",
    )


def _readiness_activation(
    row: Mapping[str, Any],
    *,
    route_checks: Mapping[str, Any],
    journal: Mapping[str, Any] | None,
) -> tuple[str, str, list[str], str]:
    status = str(row.get("status") or "")
    blockers = [str(b) for b in (row.get("blockers") or []) if b]
    if status == STATUS_PAPER_REVIEW_READY:
        return (
            ACTIVATION_NEEDS_HUMAN_APPROVAL,
            ROUTE_UNPROVEN,
            blockers or ["paper manifest is not approved yet"],
            "create a locked paper-trial manifest after human approval",
        )
    if status == "REPLAY_POSITIVE_NEEDS_SHADOW_ADAPTER":
        return (
            ACTIVATION_NEEDS_ADAPTER,
            ROUTE_UNPROVEN,
            blockers or ["replay-positive lane has no runtime adapter"],
            "build runtime shadow/paper adapter before paper activation",
        )
    if status in {"SHADOW_NEGATIVE", "SHADOW_PF_TOO_LOW", "BLOCKED"}:
        return (
            ACTIVATION_BLOCKED_NEGATIVE,
            ROUTE_BLOCKED,
            blockers or ["lane is blocked by negative evidence"],
            "mine rejection reasons; do not promote this lane",
        )
    if status in {STATUS_PAPER_ACTIVE, STATUS_PAPER_WAITING} or journal:
        if int((journal or {}).get("paper_order_intents") or 0) > 0:
            return (
                ACTIVATION_PAPER_RUNNING,
                ROUTE_RUNNING,
                blockers,
                "monitor paper PnL, exits, drawdown, and journal health",
            )
        return (
            ACTIVATION_PAPER_ONLINE_WAITING,
            ROUTE_RUNNING,
            blockers or ["paper lane online but no paper order intents yet"],
            "leave paper lane online; inspect scanner blocker if it stays silent",
        )
    if route_checks.get("desired_paper_route"):
        return (
            ACTIVATION_OBSERVE_ONLY,
            ROUTE_READY,
            blockers or ["observed lane has no approved paper manifest"],
            "attach a locked manifest before treating it as a paper trial",
        )
    return (
        ACTIVATION_OBSERVE_ONLY,
        ROUTE_UNPROVEN,
        blockers or ["lane is observation-only"],
        "continue shadow/replay evidence collection",
    )


def _route_checks(
    *,
    manifest: Mapping[str, Any],
    strategy_id: str,
    route_matches: list[dict[str, Any]],
    journal: Mapping[str, Any] | None,
) -> dict[str, Any]:
    live_orders_enabled = bool(manifest.get("live_orders_enabled"))
    approved_by = str(manifest.get("approved_by") or "").lower()
    return {
        "manifest_present": bool(manifest.get("manifest_path")),
        "manifest_approved_by_human": approved_by == "human",
        "manifest_safe": live_orders_enabled is False,
        "strategy_registered": _strategy_registered(strategy_id),
        "desired_paper_route": any(str(r.get("mode") or "").lower() == "paper" for r in route_matches),
        "journal_seen": bool(journal),
        "live_orders_enabled": live_orders_enabled,
    }


def _paper_decision(
    activation_state: str,
    requested: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if activation_state == ACTIVATION_PAPER_RUNNING:
        action = "KEEP_RUNNING"
    elif activation_state == ACTIVATION_PAPER_ONLINE_WAITING:
        action = "KEEP_ONLINE_WAIT_FOR_SIGNAL"
    elif activation_state == ACTIVATION_ROUTE_READY_NO_JOURNAL:
        action = "REPAIR_RUNTIME_JOURNAL_OR_START_CONTAINER"
    elif activation_state == ACTIVATION_NEEDS_HUMAN_APPROVAL:
        action = "HUMAN_APPROVE_PAPER_MANIFEST"
    elif activation_state == ACTIVATION_BLOCKED_NEGATIVE:
        action = "DO_NOT_PROMOTE_MINE_REJECTIONS"
    elif activation_state == ACTIVATION_NEEDS_ADAPTER:
        action = "BUILD_RUNTIME_ADAPTER"
    elif activation_state == ACTIVATION_MANIFEST_UNSAFE:
        action = "FIX_UNSAFE_MANIFEST"
    elif activation_state == ACTIVATION_ROUTE_BLOCKED:
        action = "WIRE_RUNTIME_ROUTE"
    else:
        action = "OBSERVE"
    return {
        "action": action,
        "requested_100_margin_25x_allowed": (
            bool(requested.get("can_run_requested")) if isinstance(requested, Mapping) else False
        ),
        "can_trade": False,
        "can_promote": False,
    }


def _sizing_profiles(
    manifest: Mapping[str, Any],
    config: PaperLaneActivationConfig,
) -> dict[str, Any]:
    return {
        "paper": _requested_experiment(
            manifest,
            config,
            profile="paper",
            margin_usd=config.requested_margin_usd,
            leverage=config.requested_leverage,
        ),
        "live": _requested_experiment(
            manifest,
            config,
            profile="live",
            margin_usd=config.live_margin_usd,
            leverage=config.live_leverage,
        ),
    }


def _requested_experiment(
    manifest: Mapping[str, Any],
    config: PaperLaneActivationConfig,
    *,
    profile: str = "paper",
    margin_usd: float | None = None,
    leverage: float | None = None,
) -> dict[str, Any]:
    exchange = str(manifest.get("exchange") or "")
    symbol = str(manifest.get("symbol") or "")
    margin = float(config.requested_margin_usd if margin_usd is None else margin_usd)
    lev = float(config.requested_leverage if leverage is None else leverage)
    notional = margin * lev
    max_leverage = _optional_float(manifest.get("max_leverage"))
    limits = venue_symbol_limits(exchange, symbol)
    risk_blockers: list[str] = []

    if lev > ABSOLUTE_MAX_LEVERAGE:
        risk_blockers.append(
            f"requested leverage {lev:g}x exceeds absolute max {ABSOLUTE_MAX_LEVERAGE:g}x"
        )
    if max_leverage is not None and lev > max_leverage:
        risk_blockers.append(
            f"requested leverage {lev:g}x exceeds manifest max {max_leverage:g}x"
        )
    if lev > HIGH_LEVERAGE_THRESHOLD and not config.high_leverage_ack:
        risk_blockers.append(
            f"requested leverage above {HIGH_LEVERAGE_THRESHOLD:g}x needs explicit high-leverage acknowledgement"
        )
    if notional < limits.min_notional_usd:
        risk_blockers.append(
            f"requested notional ${notional:.2f} below venue min ${limits.min_notional_usd:.2f}"
        )
    spec_source = "fallback_limits"
    if exchange.lower() in {"binanceusdm", "bybit"}:
        spec_source = "verified_exchange_limits"
    elif exchange.lower() == "delta_india":
        spec_source = "delta_fallback_limits_contract_lookup_required_before_live"

    return {
        "profile": profile,
        "requested_margin_usd": margin,
        "requested_leverage": lev,
        "requested_notional_usd": notional,
        "manifest_max_leverage": max_leverage,
        "venue_min_qty": limits.min_qty,
        "venue_qty_step": limits.qty_step,
        "venue_min_notional_usd": limits.min_notional_usd,
        "venue_spec_source": spec_source,
        "high_leverage_threshold": HIGH_LEVERAGE_THRESHOLD,
        "absolute_max_leverage": ABSOLUTE_MAX_LEVERAGE,
        "risk_compatible": not risk_blockers,
        "can_run_requested": not risk_blockers,
        "can_apply_from_dashboard": False,
        "execution_permission": (
            "paper_manifest_and_runner_required"
            if profile == "paper"
            else "live_ladder_pre_live_checklist_and_code_config_required"
        ),
        "blockers": risk_blockers,
        "control_blockers": [
            "dashboard input is a local sizing plan only; no runtime config is changed"
        ],
    }


def _load_manifest_candidates(manifest_dir: Path) -> list[dict[str, Any]]:
    if not manifest_dir.is_dir():
        return []
    candidates: list[dict[str, Any]] = []
    for path in sorted(manifest_dir.glob("*.yaml")):
        raw = _read_yaml(path)
        if not isinstance(raw, Mapping):
            continue
        if isinstance(raw.get("candidates"), list):
            for item in raw.get("candidates") or []:
                if isinstance(item, Mapping):
                    candidates.append(_candidate_from_manifest(raw, item, path))
        else:
            candidates.append(_candidate_from_manifest(raw, raw, path))
            for item in raw.get("companion_lanes") or []:
                if isinstance(item, Mapping):
                    merged = {**dict(raw), **dict(item)}
                    merged.setdefault("strategy", raw.get("strategy"))
                    merged["trial_id"] = str(
                        item.get("trial_id")
                        or _companion_trial_id(raw.get("trial_id") or path.stem, item)
                    )
                    candidates.append(_candidate_from_manifest(raw, merged, path))
    candidates.sort(key=lambda r: (str(r.get("exchange")), str(r.get("strategy_id")), str(r.get("symbol"))))
    return candidates


def _candidate_from_manifest(
    root: Mapping[str, Any],
    item: Mapping[str, Any],
    path: Path,
) -> dict[str, Any]:
    symbol = str(item.get("symbol") or root.get("symbol") or "")
    exchange = str(item.get("exchange") or item.get("venue") or root.get("exchange") or root.get("venue") or "")
    if not exchange:
        exchange = _infer_exchange(symbol, path)
    return {
        "trial_id": str(item.get("trial_id") or root.get("trial_id") or path.stem),
        "manifest_id": str(root.get("trial_set_id") or root.get("trial_id") or path.stem),
        "manifest_path": str(path),
        "exchange": exchange,
        "symbol": symbol,
        "timeframe": str(item.get("timeframe") or root.get("timeframe") or "1h"),
        "strategy_id": str(item.get("strategy") or item.get("strategy_id") or root.get("strategy") or root.get("strategy_id") or ""),
        "approved_by": root.get("approved_by"),
        "approval_date": root.get("approval_date"),
        "live_orders_enabled": _bool(root.get("live_orders_enabled")),
        "starting_equity": item.get("starting_equity", root.get("starting_equity")),
        "daily_loss_limit_usd": item.get("daily_loss_limit_usd", root.get("daily_loss_limit_usd")),
        "max_leverage": item.get("max_leverage", root.get("max_leverage")),
        "min_duration_days": item.get("min_duration_days", root.get("min_duration_days")),
        "min_trades": item.get("min_trades", root.get("min_trades")),
        "max_drawdown_pct": item.get("max_drawdown_pct", root.get("max_drawdown_pct")),
        "grid_evidence": item.get("grid_evidence") or root.get("research_result"),
    }


def _desired_route_index(
    desired_specs: Iterable[Mapping[str, Any] | Any] | None,
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    errors: list[str] = []
    if desired_specs is None:
        try:
            from vnedge.runtime.multi_lane_shadow import desired_lane_specs

            desired_specs = desired_lane_specs({})
        except Exception as exc:  # pragma: no cover - defensive for minimal installs.
            desired_specs = []
            errors.append(f"could not load runtime lane roster: {exc}")

    out: dict[str, list[dict[str, Any]]] = {}
    for spec in desired_specs:
        row = _spec_to_dict(spec)
        key = _identity(
            str(row.get("strategy_id") or ""),
            str(row.get("exchange") or ""),
            str(row.get("symbol") or ""),
            str(row.get("timeframe") or ""),
        )
        if key.strip("|"):
            out.setdefault(key, []).append(row)
    return out, errors


def _spec_to_dict(spec: Mapping[str, Any] | Any) -> dict[str, Any]:
    if isinstance(spec, Mapping):
        raw = dict(spec)
    else:
        raw = {
            "lane_id": getattr(spec, "lane_id", ""),
            "exchange": getattr(spec, "exchange", ""),
            "symbol": getattr(spec, "symbol", ""),
            "timeframe": getattr(spec, "timeframe", ""),
            "strategy_id": getattr(spec, "strategy_id", ""),
            "mode": getattr(spec, "mode", ""),
        }
    mode = raw.get("mode")
    raw["mode"] = str(getattr(mode, "value", mode) or "")
    return raw


def _index_rows(rows: Any) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(rows, Iterable):
        return out
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        strategy_id = str(row.get("strategy_id") or row.get("family") or "")
        exchange = str(row.get("exchange") or "")
        symbol = str(row.get("symbol") or "")
        timeframe = str(row.get("timeframe") or "")
        key = _identity(strategy_id, exchange, symbol, timeframe)
        if key.strip("|"):
            out.setdefault(key, dict(row))
    return out


def _journal_index(journal_dir: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if not journal_dir.is_dir():
        return out
    for path in sorted(journal_dir.glob("*.journal.jsonl")):
        if path.name.endswith("_shadow.journal.jsonl"):
            continue
        evidence = _paper_journal_evidence(path)
        if evidence is None:
            continue
        key = _identity(
            str(evidence.get("strategy_id") or ""),
            str(evidence.get("exchange") or ""),
            str(evidence.get("symbol") or ""),
            str(evidence.get("timeframe") or ""),
        )
        if key.strip("|"):
            out[key] = evidence
        out[f"trial::{path.stem.removesuffix('.journal')}"] = evidence
    return out


def _paper_journal_evidence(path: Path) -> dict[str, Any] | None:
    counters = Counter()
    strategy_id = ""
    exchange = ""
    symbol = ""
    timeframe = "1h"
    first_ts: str | None = None
    last_ts: str | None = None
    last_why: str | None = None
    latest_heartbeat: dict[str, Any] | None = None

    for record in _iter_jsonl(path, max_bytes=3_000_000):
        ts = str(record.get("ts") or "")
        first_ts = first_ts or ts or None
        last_ts = ts or last_ts
        kind = str(record.get("kind") or "")
        payload = record.get("payload") if isinstance(record.get("payload"), Mapping) else {}
        counters[kind] += 1
        if kind == "lane_eval":
            strategy_id = str(payload.get("strategy_id") or strategy_id)
            exchange = str(payload.get("exchange") or exchange)
            symbol = str(payload.get("symbol") or symbol)
            timeframe = str(payload.get("timeframe") or timeframe)
            last_why = str(
                payload.get("why")
                or payload.get("skip_reason")
                or payload.get("signal_reason")
                or last_why
                or ""
            )
        elif kind == "paper_lane_heartbeat":
            strategy_id = str(payload.get("strategy_id") or strategy_id)
            exchange = str(payload.get("exchange") or exchange)
            symbol = str(payload.get("symbol") or symbol)
            timeframe = str(payload.get("timeframe") or timeframe)
            last_why = str(
                payload.get("why_no_trade")
                or payload.get("reason")
                or last_why
                or ""
            )
            latest_heartbeat = dict(payload)
        elif kind == "order_intent":
            intent = payload.get("intent") if isinstance(payload, Mapping) else {}
            if isinstance(intent, Mapping):
                strategy_id = str(intent.get("strategy_id") or strategy_id)
                symbol = str(intent.get("symbol") or symbol)
        elif kind == "live_paper_report":
            report = payload.get("report") if isinstance(payload, Mapping) else {}
            if isinstance(report, Mapping):
                strategy_id = str(report.get("strategy_id") or strategy_id)
                symbol = str(report.get("symbol") or symbol)

    if not counters:
        return None
    return {
        "journal": str(path),
        "trial_id": path.name.removesuffix(".journal.jsonl"),
        "evals": int(counters.get("lane_eval") or 0),
        "paper_lane_heartbeats": int(counters.get("paper_lane_heartbeat") or 0),
        "risk_decisions": int(counters.get("risk_decision") or 0),
        "paper_order_intents": int(counters.get("order_intent") or 0),
        "paper_order_acknowledged": int(counters.get("order_acknowledged") or 0),
        "paper_exits": int(counters.get("live_paper_exit") or 0),
        "paper_reports": int(counters.get("live_paper_report") or 0),
        "first_ts": first_ts,
        "last_ts": last_ts,
        "latest_why": last_why,
        "exchange": exchange,
        "symbol": symbol,
        "timeframe": timeframe,
        "strategy_id": strategy_id,
        "latest_heartbeat": latest_heartbeat,
    }


def _journal_by_trial(
    journal_index: Mapping[str, dict[str, Any]],
    manifest: Mapping[str, Any],
) -> dict[str, Any] | None:
    trial_id = str(manifest.get("trial_id") or "")
    if not trial_id:
        return None
    return journal_index.get(f"trial::{trial_id}")


def _merged_evidence(
    readiness_row: Mapping[str, Any] | None,
    scanner_row: Mapping[str, Any] | None,
    journal: Mapping[str, Any] | None,
) -> dict[str, Any]:
    evidence = {}
    if readiness_row and isinstance(readiness_row.get("evidence"), Mapping):
        evidence["readiness"] = dict(readiness_row["evidence"])
    if scanner_row:
        evidence["scanner"] = {
            "state": scanner_row.get("state"),
            "why": scanner_row.get("why"),
            "funnel": scanner_row.get("funnel"),
            "gate_diagnostics": scanner_row.get("gate_diagnostics"),
            "uplift": scanner_row.get("uplift"),
        }
    if journal:
        evidence["paper_journal"] = dict(journal)
    return evidence


def _summary(
    rows: list[dict[str, Any]],
    *,
    manifest_candidates: list[dict[str, Any]],
    route_errors: list[str],
) -> dict[str, Any]:
    activation_counts = Counter(str(r.get("activation_state") or "") for r in rows)
    route_counts = Counter(str(r.get("route_status") or "") for r in rows)
    actions = Counter(str(r.get("paper_decision", {}).get("action") or "") for r in rows)
    running = activation_counts[ACTIVATION_PAPER_RUNNING]
    waiting = activation_counts[ACTIVATION_PAPER_ONLINE_WAITING]
    no_journal = activation_counts[ACTIVATION_ROUTE_READY_NO_JOURNAL]
    return {
        "total_rows": len(rows),
        "approved_manifests": sum(
            1 for c in manifest_candidates if str(c.get("approved_by") or "").lower() == "human"
        ),
        "manifest_candidates": len(manifest_candidates),
        "paper_running": running,
        "paper_waiting": waiting,
        "route_ready_no_journal": no_journal,
        "paper_online": running + waiting,
        "paper_journal_heartbeats": sum(
            int(r.get("evidence", {}).get("paper_journal", {}).get("paper_lane_heartbeats") or 0)
            for r in rows
        ),
        "paper_journal_evals": sum(
            int(r.get("evidence", {}).get("paper_journal", {}).get("evals") or 0)
            for r in rows
        ),
        "paper_journal_order_intents": sum(
            int(r.get("evidence", {}).get("paper_journal", {}).get("paper_order_intents") or 0)
            for r in rows
        ),
        "needs_human_approval": activation_counts[ACTIVATION_NEEDS_HUMAN_APPROVAL],
        "route_blocked": activation_counts[ACTIVATION_ROUTE_BLOCKED],
        "manifest_unsafe": activation_counts[ACTIVATION_MANIFEST_UNSAFE],
        "negative_blocked": activation_counts[ACTIVATION_BLOCKED_NEGATIVE],
        "needs_adapter": activation_counts[ACTIVATION_NEEDS_ADAPTER],
        "requested_100_margin_25x_allowed": sum(
            1 for r in rows if r.get("requested_experiment", {}).get("can_run_requested")
        ),
        "paper_profile_risk_compatible": sum(
            1 for r in rows if r.get("sizing_profiles", {}).get("paper", {}).get("risk_compatible")
        ),
        "live_profile_risk_compatible": sum(
            1 for r in rows if r.get("sizing_profiles", {}).get("live", {}).get("risk_compatible")
        ),
        "activation_counts": dict(sorted(activation_counts.items())),
        "route_counts": dict(sorted(route_counts.items())),
        "next_action_counts": dict(sorted(actions.items())),
        "route_errors": route_errors,
        "can_trade": False,
        "can_promote": False,
    }


def _boards(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    def slim(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "trial_id": row.get("trial_id"),
            "exchange": row.get("exchange"),
            "symbol": row.get("symbol"),
            "timeframe": row.get("timeframe"),
            "strategy_id": row.get("strategy_id"),
            "activation_state": row.get("activation_state"),
            "route_status": row.get("route_status"),
            "next_action": row.get("next_action"),
            "latest_heartbeat": row.get("evidence", {}).get("paper_journal", {}).get("latest_heartbeat"),
            "requested_100_margin_25x_allowed": row.get("requested_experiment", {}).get("can_run_requested"),
        }

    return {
        "running": [slim(r) for r in rows if r.get("activation_state") == ACTIVATION_PAPER_RUNNING],
        "waiting_for_signal": [
            slim(r) for r in rows if r.get("activation_state") == ACTIVATION_PAPER_ONLINE_WAITING
        ],
        "ready_to_start": [
            slim(r) for r in rows if r.get("activation_state") == ACTIVATION_ROUTE_READY_NO_JOURNAL
        ],
        "needs_approval": [
            slim(r) for r in rows if r.get("activation_state") == ACTIVATION_NEEDS_HUMAN_APPROVAL
        ],
        "fix_first": [
            slim(r)
            for r in rows
            if r.get("activation_state")
            in {
                ACTIVATION_ROUTE_BLOCKED,
                ACTIVATION_MANIFEST_UNSAFE,
                ACTIVATION_BLOCKED_NEGATIVE,
                ACTIVATION_NEEDS_ADAPTER,
            }
        ],
    }


def _operator_answer(summary: Mapping[str, Any]) -> str:
    online = int(summary.get("paper_online") or 0)
    ready = int(summary.get("route_ready_no_journal") or 0)
    blocked = int(summary.get("route_blocked") or 0) + int(summary.get("manifest_unsafe") or 0)
    approvals = int(summary.get("needs_human_approval") or 0)
    if online:
        return (
            f"{online} paper lane(s) are online; "
            f"{ready} approved route(s) lack journal evidence; "
            f"{blocked} route issue(s) need fixing."
        )
    if ready:
        return (
            f"{ready} paper lane(s) are approved and routed, but no paper journal is visible. "
            "Start or repair the paper runner before evaluating signals."
        )
    if approvals:
        return f"{approvals} lane(s) are paper-review ready but still need a locked human-approved manifest."
    if blocked:
        return f"{blocked} paper activation route issue(s) are blocking the paper ladder."
    return "No active paper lane evidence is visible yet."


def _row_sort_key(row: Mapping[str, Any]) -> tuple[int, str, str, str]:
    priority = {
        ACTIVATION_PAPER_RUNNING: 0,
        ACTIVATION_PAPER_ONLINE_WAITING: 1,
        ACTIVATION_ROUTE_READY_NO_JOURNAL: 2,
        ACTIVATION_NEEDS_HUMAN_APPROVAL: 3,
        ACTIVATION_NEEDS_ADAPTER: 4,
        ACTIVATION_ROUTE_BLOCKED: 5,
        ACTIVATION_MANIFEST_UNSAFE: 6,
        ACTIVATION_BLOCKED_NEGATIVE: 7,
    }.get(str(row.get("activation_state") or ""), 9)
    return (
        priority,
        str(row.get("exchange") or ""),
        str(row.get("symbol") or ""),
        str(row.get("strategy_id") or ""),
    )


def _strategy_registered(strategy_id: str) -> bool:
    if not strategy_id:
        return False
    try:
        get_strategy_class(strategy_id)
    except KeyError:
        return False
    return True


def _identity(strategy_id: str, exchange: str, symbol: str, timeframe: str) -> str:
    return "|".join(
        (
            strategy_id.strip().lower(),
            exchange.strip().lower(),
            symbol.strip().upper(),
            timeframe.strip().lower(),
        )
    )


def _infer_exchange(symbol: str, path: Path) -> str:
    if "/USD:USD" in symbol:
        return "delta_india"
    if "funding_mr_btc" in path.name:
        return "binanceusdm"
    return "binanceusdm"


def _companion_trial_id(root_trial_id: Any, item: Mapping[str, Any]) -> str:
    symbol = str(item.get("symbol") or "lane").lower()
    symbol = symbol.replace("/", "_").replace(":", "_").replace("-", "_")
    timeframe = str(item.get("timeframe") or "tf").lower()
    return f"{root_trial_id}_companion_{symbol}_{timeframe}"


def _optional_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed:
        return None
    return parsed


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _read_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None


def _read_json_payload(path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback
    return payload if isinstance(payload, dict) else fallback


def _iter_jsonl(path: Path, *, max_bytes: int = 1_000_000) -> Iterable[dict[str, Any]]:
    try:
        size = path.stat().st_size
        with open(path, "rb") as handle:
            if size > max_bytes:
                handle.seek(max(0, size - max_bytes))
                handle.readline()
            for raw in handle:
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    yield payload
    except OSError:
        return


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--readiness", type=Path, default=DEFAULT_READINESS)
    parser.add_argument("--scanner", type=Path, default=DEFAULT_SCANNER)
    parser.add_argument("--manifest-dir", type=Path, default=DEFAULT_MANIFEST_DIR)
    parser.add_argument("--journal-dir", type=Path, default=DEFAULT_JOURNAL_DIR)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--feed", type=Path, default=DEFAULT_FEED)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval-seconds", type=float, default=60.0)
    parser.add_argument("--requested-margin-usd", type=float, default=100.0)
    parser.add_argument("--requested-leverage", type=float, default=25.0)
    parser.add_argument("--live-margin-usd", type=float, default=100.0)
    parser.add_argument("--live-leverage", type=float, default=5.0)
    parser.add_argument("--ack-high-leverage", action="store_true")
    parser.add_argument(
        "--high-leverage-ack",
        type=_bool,
        default=False,
        help="Env-friendly equivalent of --ack-high-leverage.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    config = PaperLaneActivationConfig(
        requested_margin_usd=args.requested_margin_usd,
        requested_leverage=args.requested_leverage,
        live_margin_usd=args.live_margin_usd,
        live_leverage=args.live_leverage,
        high_leverage_ack=bool(args.ack_high_leverage or args.high_leverage_ack),
    )
    while True:
        payload = build_paper_lane_activation(
            readiness_path=args.readiness,
            scanner_path=args.scanner,
            manifest_dir=args.manifest_dir,
            journal_dir=args.journal_dir,
            config=config,
        )
        publish_paper_lane_activation(payload, args.out, args.feed)
        print(render_report(payload), flush=True)
        if args.once:
            return 0
        time.sleep(max(1.0, float(args.interval_seconds)))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
