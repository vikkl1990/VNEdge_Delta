"""Paper trade contract reconciler.

Paper performance says whether lanes made money. Exit autopsy says how they
exited. This module answers a colder question before we blame alpha:

Did each closed paper trade honor the runtime contract it was supposed to run
under: entry intent, exit intent, symbol/side, reduce-only exit, leverage,
margin/notional, fee drag, and exit-plan metadata?

Read-only by design: it cannot start, stop, promote, demote, or trade a lane.
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vnedge.dashboard.trade_journal import (
    TradeJournalConfig,
    _build_closed_trades,
    _fill_rows,
    _float,
    _journal_rows,
    _project_journals,
    _trade_net,
)

DEFAULT_JOURNAL_DIR = Path("logs/paper_trials")
DEFAULT_OUT = Path("research/live_research/paper_trade_contract_reconciler_latest.json")
DEFAULT_FEED = Path("research/live_research/paper_trade_contract_reconciler_feed.jsonl")

VERDICT_NO_CLOSED_TRADES = "NO_CLOSED_TRADES"
VERDICT_CONTRACT_BROKEN = "CONTRACT_BROKEN"
VERDICT_FEE_WALL_BREACH = "FEE_WALL_BREACH"
VERDICT_CONTRACT_OK_NEGATIVE_ALPHA = "CONTRACT_OK_NEGATIVE_ALPHA"
VERDICT_CONTRACT_OK_EDGE_DEFICIT = "CONTRACT_OK_EDGE_DEFICIT"
VERDICT_CONTRACT_OK_PROFITABLE = "CONTRACT_OK_PROFITABLE"
TRADE_CONTRACT_OK = "CONTRACT_OK"

ALPHA_NEGATIVE_AFTER_COST = "NEGATIVE_AFTER_COST"
ALPHA_BELOW_EDGE_TARGET = "BELOW_EDGE_TARGET"
ALPHA_EDGE_TARGET_MET = "EDGE_TARGET_MET"

CRITICAL_VIOLATIONS = {
    "missing_entry_intent",
    "missing_exit_intent",
    "entry_symbol_mismatch",
    "entry_side_mismatch",
    "exit_side_mismatch",
    "entry_reduce_only_true",
    "exit_reduce_only_false",
    "entry_quantity_drift",
    "exit_quantity_drift",
    "entry_notional_drift",
    "missing_leverage",
    "missing_margin_or_notional",
    "missing_exit_metadata",
    "missing_exit_resolution",
}


@dataclass(frozen=True)
class PaperTradeContractReconcilerConfig:
    tail_bytes: int = 8_000_000
    max_rows: int = 120
    max_trade_samples: int = 160
    min_expected_net_bps: float = 25.0
    max_fee_bps: float = 12.0
    max_quantity_drift_pct: float = 1.0
    max_notional_drift_pct: float = 10.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _IntentContract:
    lane_id: str
    ts: str
    client_order_id: str
    symbol: str
    side: str
    order_type: str
    quantity: float
    limit_price: float
    reduce_only: bool
    strategy_id: str
    leverage: float
    notional_usd: float

    @property
    def margin_usd(self) -> float:
        if self.notional_usd > 0 and self.leverage > 0:
            return self.notional_usd / self.leverage
        return 0.0


def build_paper_trade_contract_reconciler(
    *,
    journal_dir: Path | str = DEFAULT_JOURNAL_DIR,
    config: PaperTradeContractReconcilerConfig = PaperTradeContractReconcilerConfig(),
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the read-only paper trade contract reconciliation payload."""

    now = now or datetime.now(UTC)
    root = Path(journal_dir)
    journal_config = TradeJournalConfig(
        tail_bytes=config.tail_bytes,
        max_rows=max(config.max_rows, 1),
    )
    fills = _fill_rows(root, lane="", since=None, config=journal_config, active=None)
    journal_rows = _journal_rows(root, lane="", since=None, config=journal_config, active=None)
    _orders, _events, virtual_trades = _project_journals(journal_rows)
    closed = [
        row
        for row in _build_closed_trades(fills, journal_rows, virtual_trades)
        if row.get("kind") == "actual_closing_fill"
    ]
    intents = _intent_contracts(journal_rows)
    lane_meta = _lane_metadata(journal_rows, fills)
    lanes = sorted({str(fill.get("lane") or "") for fill in fills} | set(lane_meta))

    trade_rows = [_trade_contract_row(trade, intents, config) for trade in closed]
    by_lane: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in trade_rows:
        by_lane[str(row.get("lane_id") or "")].append(row)
    for lane in lanes:
        by_lane.setdefault(lane, [])

    rows = [
        _lane_contract_row(lane, trades, lane_meta.get(lane, {}), config)
        for lane, trades in sorted(by_lane.items())
    ]
    rows.sort(key=_row_sort_key)
    rows = rows[: max(1, int(config.max_rows))]
    summary = _summary(rows)
    samples = sorted(trade_rows, key=_trade_sample_sort_key)[: max(1, int(config.max_trade_samples))]

    return {
        "generated_at": now.isoformat(),
        "report_id": "paper_trade_contract_reconciler_v1",
        "mode": "read_only_paper_contract_reconciliation",
        "config": config.to_dict(),
        "inputs": {"journal_dir": str(root)},
        "summary": summary,
        "boards": _boards(rows),
        "rows": rows,
        "trade_samples": samples,
        "operator_answer": _operator_answer(summary),
        "policy": {
            "read_only": True,
            "can_trade": False,
            "can_promote": False,
            "scope": "closed paper fills only; shadow outcomes stay in trade journal",
            "promotion_requires_clean_contract_and_untouched_judgment": True,
        },
        "can_trade": False,
        "can_promote": False,
    }


def publish_paper_trade_contract_reconciler(
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


def render_report(payload: Mapping[str, Any], *, limit: int = 30) -> str:
    summary = payload.get("summary", {})
    lines = [
        "=== Paper trade contract reconciler ===",
        f"generated: {payload.get('generated_at')}",
        str(payload.get("operator_answer") or ""),
        (
            "summary: "
            f"{summary.get('total_lanes', 0)} lanes, "
            f"{summary.get('closed_trades', 0)} closed trades, "
            f"{summary.get('contract_broken_lanes', 0)} broken-contract lanes, "
            f"{summary.get('fee_wall_breach_lanes', 0)} fee-wall lanes, "
            f"{summary.get('contract_ok_negative_lanes', 0)} clean-negative lanes"
        ),
    ]
    for row in list(payload.get("rows", []))[:limit]:
        lines.append(
            f"  {row.get('verdict', ''):<30} {row.get('lane_id', ''):<42} "
            f"{row.get('closed_trades', 0):>3} closed "
            f"net ${row.get('net_pnl_usd', 0.0):>8.2f} "
            f"avg {row.get('avg_net_bps', 0.0):>7.2f}bps "
            f"{row.get('next_action', '')}"
        )
    lines.append("read-only: can_trade=false can_promote=false")
    return "\n".join(lines)


def _intent_contracts(
    journal_rows: list[tuple[str, dict[str, Any]]]
) -> dict[str, _IntentContract]:
    contracts: dict[str, _IntentContract] = {}
    for lane, record in journal_rows:
        if str(record.get("kind") or "") != "order_intent":
            continue
        payload = record.get("payload") if isinstance(record.get("payload"), Mapping) else {}
        intent = payload.get("intent") if isinstance(payload.get("intent"), Mapping) else {}
        coid = str(payload.get("client_order_id") or intent.get("client_order_id") or "")
        if not coid:
            continue
        contracts[coid] = _IntentContract(
            lane_id=str(lane),
            ts=str(record.get("ts") or payload.get("ts") or ""),
            client_order_id=coid,
            symbol=str(intent.get("symbol") or ""),
            side=str(intent.get("side") or "").lower(),
            order_type=str(intent.get("order_type") or ""),
            quantity=abs(_float(intent.get("quantity"))),
            limit_price=_float(intent.get("limit_price")),
            reduce_only=bool(intent.get("reduce_only")),
            strategy_id=str(intent.get("strategy_id") or ""),
            leverage=_float(intent.get("leverage")),
            notional_usd=_float(intent.get("notional_usd")),
        )
    return contracts


def _lane_metadata(
    journal_rows: list[tuple[str, dict[str, Any]]],
    fills: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    meta: dict[str, dict[str, Any]] = defaultdict(dict)
    for fill in fills:
        lane = str(fill.get("lane") or "")
        if not lane:
            continue
        row = meta[lane]
        for src, dest in (
            ("venue", "exchange"),
            ("strategy_id", "strategy_id"),
            ("symbol", "symbol"),
            ("mode", "mode"),
        ):
            value = fill.get(src)
            if value and not row.get(dest):
                row[dest] = value
    for lane, record in journal_rows:
        payload = record.get("payload") if isinstance(record.get("payload"), Mapping) else {}
        intent = payload.get("intent") if isinstance(payload.get("intent"), Mapping) else {}
        row = meta[str(lane)]
        row["last_journal_kind"] = str(record.get("kind") or row.get("last_journal_kind") or "")
        for src, dest in (
            ("exchange", "exchange"),
            ("strategy_id", "strategy_id"),
            ("symbol", "symbol"),
            ("timeframe", "timeframe"),
            ("mode", "mode"),
        ):
            value = payload.get(src)
            if value:
                row[dest] = value
        for src, dest in (
            ("strategy_id", "strategy_id"),
            ("symbol", "symbol"),
            ("mode", "mode"),
        ):
            value = intent.get(src)
            if value:
                row[dest] = value
    return {lane: dict(row) for lane, row in meta.items()}


def _trade_contract_row(
    trade: Mapping[str, Any],
    intents: Mapping[str, _IntentContract],
    config: PaperTradeContractReconcilerConfig,
) -> dict[str, Any]:
    lane = str(trade.get("lane") or "")
    entry_coid = str(trade.get("entry_client_order_id") or "")
    exit_coid = str(trade.get("client_order_id") or "")
    entry_intent = intents.get(entry_coid)
    exit_intent = intents.get(exit_coid)
    violations: list[str] = []
    warnings: list[str] = []

    if entry_intent is None:
        violations.append("missing_entry_intent")
    if exit_intent is None:
        violations.append("missing_exit_intent")
    if entry_intent is not None:
        _check_entry_contract(trade, entry_intent, config, violations, warnings)
    if exit_intent is not None:
        _check_exit_contract(trade, exit_intent, config, violations, warnings)

    if _float(trade.get("leverage")) <= 0:
        violations.append("missing_leverage")
    if _notional_usd(trade, entry_intent) <= 0 or _margin_usd(trade, entry_intent) <= 0:
        violations.append("missing_margin_or_notional")
    if not str(trade.get("exit_metadata_source") or ""):
        violations.append("missing_exit_metadata")
    if not str(trade.get("resolution") or ""):
        violations.append("missing_exit_resolution")
    if str(trade.get("resolution") or "") == "take_profit" and not trade.get("take_profit_levels"):
        warnings.append("take_profit_ladder_missing")

    fee_bps = _fee_bps(trade, entry_intent)
    if fee_bps > config.max_fee_bps:
        warnings.append("fee_wall_breach")
    net_bps = _net_bps(trade, entry_intent)
    net = _trade_net(dict(trade))
    alpha_state = _alpha_state(net_bps, net, config)
    contract_state = (
        VERDICT_CONTRACT_BROKEN
        if any(v in CRITICAL_VIOLATIONS for v in violations)
        else VERDICT_FEE_WALL_BREACH
        if fee_bps > config.max_fee_bps
        else TRADE_CONTRACT_OK
    )

    return {
        "lane_id": lane,
        "ts": str(trade.get("ts") or ""),
        "entry_ts": str(trade.get("entry_ts") or ""),
        "exchange": str(trade.get("exchange") or trade.get("venue") or ""),
        "strategy_id": str(trade.get("strategy_id") or (entry_intent.strategy_id if entry_intent else "")),
        "symbol": str(trade.get("symbol") or ""),
        "side": str(trade.get("side") or ""),
        "quantity": _float(trade.get("quantity")),
        "entry_price": _float(trade.get("entry_price")),
        "exit_price": _float(trade.get("exit_price")),
        "entry_client_order_id": entry_coid,
        "exit_client_order_id": exit_coid,
        "net_pnl_usd": round(net, 6),
        "fee_usd": round(_float(trade.get("fee_usd")), 6),
        "notional_usd": round(_notional_usd(trade, entry_intent), 4),
        "margin_usd": round(_margin_usd(trade, entry_intent), 4),
        "leverage": round(_float(trade.get("leverage") or (entry_intent.leverage if entry_intent else 0.0)), 4),
        "gross_captured_bps": trade.get("captured_bps"),
        "fee_bps": round(fee_bps, 4),
        "net_bps": round(net_bps, 4),
        "alpha_state": alpha_state,
        "contract_state": contract_state,
        "violations": sorted(set(violations)),
        "warnings": sorted(set(warnings)),
        "exit_resolution": str(trade.get("resolution") or ""),
        "exit_reason": str(trade.get("exit_reason") or ""),
        "tp_reached": int(_float(trade.get("tp_reached"))),
        "exit_metadata_source": str(trade.get("exit_metadata_source") or ""),
    }


def _check_entry_contract(
    trade: Mapping[str, Any],
    intent: _IntentContract,
    config: PaperTradeContractReconcilerConfig,
    violations: list[str],
    warnings: list[str],
) -> None:
    if intent.reduce_only:
        violations.append("entry_reduce_only_true")
    trade_symbol = str(trade.get("symbol") or "")
    if intent.symbol and trade_symbol and intent.symbol != trade_symbol:
        violations.append("entry_symbol_mismatch")
    expected_side = _trade_side_from_entry(intent.side)
    if expected_side and expected_side != str(trade.get("side") or "").lower():
        violations.append("entry_side_mismatch")
    _check_quantity_drift(
        prefix="entry",
        actual=abs(_float(trade.get("quantity"))),
        intended=intent.quantity,
        config=config,
        violations=violations,
    )
    intended_notional = _intent_notional(intent)
    fill_notional = _fill_notional(trade)
    if intended_notional > 0 and fill_notional > 0:
        drift = abs(fill_notional - intended_notional) / intended_notional * 100.0
        if drift > config.max_notional_drift_pct:
            violations.append("entry_notional_drift")
        elif drift > config.max_notional_drift_pct / 2.0:
            warnings.append("entry_notional_near_drift")


def _check_exit_contract(
    trade: Mapping[str, Any],
    intent: _IntentContract,
    config: PaperTradeContractReconcilerConfig,
    violations: list[str],
    warnings: list[str],
) -> None:
    if not intent.reduce_only:
        violations.append("exit_reduce_only_false")
    close_side = _exit_side_from_trade(str(trade.get("side") or ""))
    if close_side and intent.side and close_side != intent.side:
        violations.append("exit_side_mismatch")
    _check_quantity_drift(
        prefix="exit",
        actual=abs(_float(trade.get("quantity"))),
        intended=intent.quantity,
        config=config,
        violations=violations,
    )
    if intent.strategy_id and trade.get("strategy_id") and intent.strategy_id != trade.get("strategy_id"):
        warnings.append("exit_strategy_id_differs")


def _check_quantity_drift(
    *,
    prefix: str,
    actual: float,
    intended: float,
    config: PaperTradeContractReconcilerConfig,
    violations: list[str],
) -> None:
    if intended <= 0:
        return
    drift = abs(actual - intended) / intended * 100.0
    if drift > config.max_quantity_drift_pct:
        violations.append(f"{prefix}_quantity_drift")


def _trade_side_from_entry(side: str) -> str:
    side = str(side or "").lower()
    if side in {"buy", "long"}:
        return "long"
    if side in {"sell", "short"}:
        return "short"
    return ""


def _exit_side_from_trade(side: str) -> str:
    side = str(side or "").lower()
    if side in {"long", "buy"}:
        return "sell"
    if side in {"short", "sell"}:
        return "buy"
    return ""


def _intent_notional(intent: _IntentContract | None) -> float:
    if intent is None:
        return 0.0
    if intent.notional_usd > 0:
        return intent.notional_usd
    if intent.limit_price > 0 and intent.quantity > 0:
        return intent.limit_price * intent.quantity
    return 0.0


def _fill_notional(trade: Mapping[str, Any]) -> float:
    entry = _float(trade.get("entry_price"))
    quantity = abs(_float(trade.get("quantity")))
    if entry > 0 and quantity > 0:
        return entry * quantity
    return 0.0


def _notional_usd(trade: Mapping[str, Any], intent: _IntentContract | None) -> float:
    for value in (trade.get("notional_usd"), _intent_notional(intent), _fill_notional(trade)):
        out = _float(value)
        if out > 0:
            return out
    return 0.0


def _margin_usd(trade: Mapping[str, Any], intent: _IntentContract | None) -> float:
    margin = _float(trade.get("margin_usd"))
    if margin > 0:
        return margin
    notional = _notional_usd(trade, intent)
    leverage = _float(trade.get("leverage") or (intent.leverage if intent else 0.0))
    if notional > 0 and leverage > 0:
        return notional / leverage
    return 0.0


def _fee_bps(trade: Mapping[str, Any], intent: _IntentContract | None) -> float:
    notional = _notional_usd(trade, intent)
    if notional <= 0:
        return 0.0
    return _float(trade.get("fee_usd")) / notional * 1e4


def _net_bps(trade: Mapping[str, Any], intent: _IntentContract | None) -> float:
    notional = _notional_usd(trade, intent)
    if notional <= 0:
        return 0.0
    return _trade_net(dict(trade)) / notional * 1e4


def _alpha_state(net_bps: float, net_usd: float, config: PaperTradeContractReconcilerConfig) -> str:
    if net_usd <= 0:
        return ALPHA_NEGATIVE_AFTER_COST
    if net_bps < config.min_expected_net_bps:
        return ALPHA_BELOW_EDGE_TARGET
    return ALPHA_EDGE_TARGET_MET


def _lane_contract_row(
    lane_id: str,
    trades: list[dict[str, Any]],
    meta: Mapping[str, Any],
    config: PaperTradeContractReconcilerConfig,
) -> dict[str, Any]:
    counts = Counter()
    warning_counts = Counter()
    net = 0.0
    fees = 0.0
    notionals = 0.0
    net_bps_values: list[float] = []
    fee_bps_values: list[float] = []
    wins = 0
    for trade in trades:
        counts.update(trade.get("violations") or [])
        warning_counts.update(trade.get("warnings") or [])
        net += _float(trade.get("net_pnl_usd"))
        fees += _float(trade.get("fee_usd"))
        notionals += _float(trade.get("notional_usd"))
        net_bps_values.append(_float(trade.get("net_bps")))
        fee_bps_values.append(_float(trade.get("fee_bps")))
        wins += 1 if _float(trade.get("net_pnl_usd")) > 0 else 0

    closed = len(trades)
    avg_net_bps = sum(net_bps_values) / closed if closed else 0.0
    avg_fee_bps = sum(fee_bps_values) / closed if closed else 0.0
    critical = sum(counts[v] for v in CRITICAL_VIOLATIONS)
    fee_wall = warning_counts.get("fee_wall_breach", 0)
    verdict = _lane_verdict(closed, critical, fee_wall, net, avg_net_bps, config)
    return {
        "lane_id": lane_id,
        "exchange": str(meta.get("exchange") or ""),
        "symbol": str(meta.get("symbol") or ""),
        "timeframe": str(meta.get("timeframe") or ""),
        "strategy_id": str(meta.get("strategy_id") or ""),
        "mode": str(meta.get("mode") or ""),
        "closed_trades": closed,
        "wins": wins,
        "win_rate_pct": round(wins / closed * 100.0, 2) if closed else 0.0,
        "net_pnl_usd": round(net, 6),
        "fees_usd": round(fees, 6),
        "notional_usd": round(notionals, 4),
        "avg_net_bps": round(avg_net_bps, 4),
        "avg_fee_bps": round(avg_fee_bps, 4),
        "critical_violations": critical,
        "fee_wall_breaches": fee_wall,
        "verdict": verdict,
        "top_violations": dict(counts.most_common(6)),
        "top_warnings": dict(warning_counts.most_common(6)),
        "next_action": _next_action(verdict, counts, warning_counts, avg_net_bps, config),
        "can_promote": False,
    }


def _lane_verdict(
    closed: int,
    critical_violations: int,
    fee_wall_breaches: int,
    net_pnl_usd: float,
    avg_net_bps: float,
    config: PaperTradeContractReconcilerConfig,
) -> str:
    if closed <= 0:
        return VERDICT_NO_CLOSED_TRADES
    if critical_violations > 0:
        return VERDICT_CONTRACT_BROKEN
    if fee_wall_breaches > 0:
        return VERDICT_FEE_WALL_BREACH
    if net_pnl_usd <= 0:
        return VERDICT_CONTRACT_OK_NEGATIVE_ALPHA
    if avg_net_bps < config.min_expected_net_bps:
        return VERDICT_CONTRACT_OK_EDGE_DEFICIT
    return VERDICT_CONTRACT_OK_PROFITABLE


def _next_action(
    verdict: str,
    violations: Counter[str],
    warnings: Counter[str],
    avg_net_bps: float,
    config: PaperTradeContractReconcilerConfig,
) -> str:
    if verdict == VERDICT_NO_CLOSED_TRADES:
        return "WAIT_FOR_CLOSED_PAPER_TRADES"
    if verdict == VERDICT_CONTRACT_BROKEN:
        if violations.get("missing_entry_intent") or violations.get("missing_exit_intent"):
            return "REPAIR_ORDER_INTENT_LINEAGE_BEFORE_ALPHA_REVIEW"
        if violations.get("exit_reduce_only_false") or violations.get("exit_side_mismatch"):
            return "REPAIR_REDUCE_ONLY_EXIT_CONTRACT"
        if violations.get("missing_exit_metadata") or violations.get("missing_exit_resolution"):
            return "WIRE_EXIT_PLAN_METADATA_TO_FILL_LEDGER"
        if violations.get("missing_leverage") or violations.get("missing_margin_or_notional"):
            return "WIRE_LEVERAGE_MARGIN_NOTIONAL_IN_INTENTS"
        return "REPAIR_PAPER_EXECUTION_CONTRACT"
    if verdict == VERDICT_FEE_WALL_BREACH:
        return "ROUTE_MAKER_FIRST_OR_REQUIRE_LARGER_EXPECTED_MOVE"
    if verdict == VERDICT_CONTRACT_OK_NEGATIVE_ALPHA:
        return "CONTRACT_CLEAN_MINE_ENTRY_EXIT_ALPHA"
    if verdict == VERDICT_CONTRACT_OK_EDGE_DEFICIT:
        deficit = max(0.0, config.min_expected_net_bps - avg_net_bps)
        return f"EDGE_DEFICIT_{deficit:.1f}BPS_TUNE_CAPTURE_OR_SKIP"
    if warnings:
        return "CONTRACT_CLEAN_REVIEW_WARNINGS_BEFORE_PROMOTION"
    return "CONTRACT_CLEAN_KEEP_PAPER_OBSERVATION"


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(str(row.get("verdict") or "") for row in rows)
    closed_trades = sum(int(row.get("closed_trades") or 0) for row in rows)
    net = sum(_float(row.get("net_pnl_usd")) for row in rows)
    fees = sum(_float(row.get("fees_usd")) for row in rows)
    return {
        "total_lanes": len(rows),
        "closed_trades": closed_trades,
        "lanes_with_closed_trades": sum(1 for row in rows if int(row.get("closed_trades") or 0) > 0),
        "contract_broken_lanes": counts.get(VERDICT_CONTRACT_BROKEN, 0),
        "fee_wall_breach_lanes": counts.get(VERDICT_FEE_WALL_BREACH, 0),
        "contract_ok_negative_lanes": counts.get(VERDICT_CONTRACT_OK_NEGATIVE_ALPHA, 0),
        "edge_deficit_lanes": counts.get(VERDICT_CONTRACT_OK_EDGE_DEFICIT, 0),
        "contract_ok_profitable_lanes": counts.get(VERDICT_CONTRACT_OK_PROFITABLE, 0),
        "no_closed_trade_lanes": counts.get(VERDICT_NO_CLOSED_TRADES, 0),
        "net_pnl_usd": round(net, 6),
        "fees_usd": round(fees, 6),
        "verdict_counts": dict(counts),
        "critical_violations": sum(int(row.get("critical_violations") or 0) for row in rows),
        "fee_wall_breaches": sum(int(row.get("fee_wall_breaches") or 0) for row in rows),
    }


def _boards(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    return {
        "repair_first": [r for r in rows if r.get("verdict") == VERDICT_CONTRACT_BROKEN][:12],
        "fee_wall": [r for r in rows if r.get("verdict") == VERDICT_FEE_WALL_BREACH][:12],
        "clean_negative_alpha": [
            r for r in rows if r.get("verdict") == VERDICT_CONTRACT_OK_NEGATIVE_ALPHA
        ][:12],
        "edge_deficit": [r for r in rows if r.get("verdict") == VERDICT_CONTRACT_OK_EDGE_DEFICIT][:12],
        "contract_clean_profitable": [
            r for r in rows if r.get("verdict") == VERDICT_CONTRACT_OK_PROFITABLE
        ][:12],
    }


def _operator_answer(summary: Mapping[str, Any]) -> str:
    if int(summary.get("closed_trades") or 0) <= 0:
        return "No closed paper trades are available for contract reconciliation yet."
    if int(summary.get("contract_broken_lanes") or 0) > 0:
        return (
            "Some paper lanes have broken execution/journal contracts. Fix those before "
            "using their P&L as alpha evidence."
        )
    if int(summary.get("fee_wall_breach_lanes") or 0) > 0:
        return "Contracts are mostly clean, but some lanes are paying too much fee drag."
    if int(summary.get("contract_ok_negative_lanes") or 0) > 0:
        return "The losing lanes are contract-clean: the remaining problem is alpha/exit quality."
    return "Closed paper trades reconcile cleanly against their runtime contract."


def _row_sort_key(row: Mapping[str, Any]) -> tuple[int, int, float, str]:
    rank = {
        VERDICT_CONTRACT_BROKEN: 0,
        VERDICT_FEE_WALL_BREACH: 1,
        VERDICT_CONTRACT_OK_NEGATIVE_ALPHA: 2,
        VERDICT_CONTRACT_OK_EDGE_DEFICIT: 3,
        VERDICT_CONTRACT_OK_PROFITABLE: 4,
        VERDICT_NO_CLOSED_TRADES: 5,
    }.get(str(row.get("verdict") or ""), 9)
    return (
        rank,
        -int(row.get("critical_violations") or 0),
        _float(row.get("net_pnl_usd")),
        str(row.get("lane_id") or ""),
    )


def _trade_sample_sort_key(row: Mapping[str, Any]) -> tuple[int, float, str]:
    rank = {
        VERDICT_CONTRACT_BROKEN: 0,
        VERDICT_FEE_WALL_BREACH: 1,
        TRADE_CONTRACT_OK: 2,
    }.get(str(row.get("contract_state") or ""), 9)
    return (rank, _float(row.get("net_pnl_usd")), str(row.get("ts") or ""))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal-dir", type=Path, default=DEFAULT_JOURNAL_DIR)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--feed", type=Path, default=DEFAULT_FEED)
    parser.add_argument("--interval-seconds", type=float, default=0.0)
    parser.add_argument("--tail-bytes", type=int, default=PaperTradeContractReconcilerConfig.tail_bytes)
    parser.add_argument("--max-rows", type=int, default=PaperTradeContractReconcilerConfig.max_rows)
    parser.add_argument(
        "--max-trade-samples",
        type=int,
        default=PaperTradeContractReconcilerConfig.max_trade_samples,
    )
    parser.add_argument(
        "--min-expected-net-bps",
        type=float,
        default=PaperTradeContractReconcilerConfig.min_expected_net_bps,
    )
    parser.add_argument(
        "--max-fee-bps",
        type=float,
        default=PaperTradeContractReconcilerConfig.max_fee_bps,
    )
    parser.add_argument(
        "--max-quantity-drift-pct",
        type=float,
        default=PaperTradeContractReconcilerConfig.max_quantity_drift_pct,
    )
    parser.add_argument(
        "--max-notional-drift-pct",
        type=float,
        default=PaperTradeContractReconcilerConfig.max_notional_drift_pct,
    )
    parser.add_argument("--print", action="store_true", dest="print_report")
    return parser.parse_args()


def _build_config(args: argparse.Namespace) -> PaperTradeContractReconcilerConfig:
    return PaperTradeContractReconcilerConfig(
        tail_bytes=args.tail_bytes,
        max_rows=args.max_rows,
        max_trade_samples=args.max_trade_samples,
        min_expected_net_bps=args.min_expected_net_bps,
        max_fee_bps=args.max_fee_bps,
        max_quantity_drift_pct=args.max_quantity_drift_pct,
        max_notional_drift_pct=args.max_notional_drift_pct,
    )


def main() -> None:
    args = _parse_args()
    config = _build_config(args)
    while True:
        payload = build_paper_trade_contract_reconciler(
            journal_dir=args.journal_dir,
            config=config,
        )
        publish_paper_trade_contract_reconciler(payload, args.out, args.feed)
        if args.print_report:
            print(render_report(payload), flush=True)
        if args.interval_seconds <= 0:
            return
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    main()
