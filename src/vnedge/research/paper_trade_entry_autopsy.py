"""Paper trade entry autopsy.

Paper performance says whether a lane is making money. Exit autopsy says how
the trade closed. This module answers the missing first question: did the paper
trade open from a fresh, same-direction, fee-aware signal context?

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
    _lane_timeframe_seconds,
    _parse_dt,
    _project_journals,
    _trade_net,
)

DEFAULT_JOURNAL_DIR = Path("logs/paper_trials")
DEFAULT_OUT = Path("research/live_research/paper_trade_entry_autopsy_latest.json")
DEFAULT_FEED = Path("research/live_research/paper_trade_entry_autopsy_feed.jsonl")

STATE_NO_CLOSED_TRADES = "ENTRY_NO_CLOSED_TRADES"
STATE_UNDER_SAMPLED = "ENTRY_UNDER_SAMPLED"
STATE_CAPTURE_HEALTHY = "ENTRY_CAPTURE_HEALTHY"
STATE_CONTEXT_MISSING = "ENTRY_CONTEXT_MISSING"
STATE_SIGNAL_STALE = "ENTRY_SIGNAL_STALE"
STATE_DIRECTION_DRIFT = "ENTRY_DIRECTION_DRIFT"
STATE_FEE_WALL_TOO_SMALL = "ENTRY_FEE_WALL_TOO_SMALL"
STATE_NEGATIVE_AFTER_COST = "ENTRY_NEGATIVE_AFTER_COST"
STATE_OBSERVE_MORE = "ENTRY_OBSERVE_MORE"


@dataclass(frozen=True)
class PaperTradeEntryAutopsyConfig:
    tail_bytes: int = 8_000_000
    max_rows: int = 120
    min_closed_trades: int = 5
    min_profit_factor: float = 1.5
    min_avg_net_bps: float = 25.0
    min_expected_edge_bps: float = 25.0
    fee_wall_bps: float = 8.0
    max_signal_age_seconds: float = 600.0
    max_signal_age_bars: float = 2.25
    max_stale_entry_rate: float = 0.25
    max_missing_context_rate: float = 0.20
    max_direction_drift_rate: float = 0.05

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_paper_trade_entry_autopsy(
    *,
    journal_dir: Path | str = DEFAULT_JOURNAL_DIR,
    config: PaperTradeEntryAutopsyConfig = PaperTradeEntryAutopsyConfig(),
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the read-only paper entry autopsy payload."""

    now = now or datetime.now(UTC)
    root = Path(journal_dir)
    journal_config = TradeJournalConfig(
        tail_bytes=config.tail_bytes,
        max_rows=max(config.max_rows, 1),
    )
    fills = _fill_rows(root, lane="", since=None, config=journal_config, active=None)
    journal_rows = _journal_rows(
        root, lane="", since=None, config=journal_config, active=None
    )
    _orders, _events, virtual_trades = _project_journals(journal_rows)
    closed = [
        row
        for row in _build_closed_trades(fills, journal_rows, virtual_trades)
        if row.get("kind") == "actual_closing_fill"
    ]
    evals = _lane_evals(journal_rows)
    metadata = _lane_metadata(journal_rows, fills)
    by_lane: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in closed:
        by_lane[str(trade.get("lane") or "")].append(trade)
    for fill in fills:
        by_lane.setdefault(str(fill.get("lane") or ""), [])
    for lane in evals:
        by_lane.setdefault(lane, [])

    rows = [
        _lane_autopsy(lane, trades, evals.get(lane, []), metadata.get(lane, {}), config)
        for lane, trades in sorted(by_lane.items())
        if lane
    ]
    rows.sort(key=_row_sort_key)
    rows = rows[: max(1, int(config.max_rows))]
    summary = _summary(rows)

    return {
        "generated_at": now.isoformat(),
        "report_id": "paper_trade_entry_autopsy_v1",
        "mode": "read_only_paper_trade_entry_autopsy",
        "config": config.to_dict(),
        "inputs": {"journal_dir": str(root)},
        "summary": summary,
        "rows": rows,
        "operator_answer": _operator_answer(summary),
        "policy": {
            "read_only": True,
            "can_trade": False,
            "can_promote": False,
            "scope": "closed paper fills joined to prior lane_eval signal context",
        },
        "can_trade": False,
        "can_promote": False,
    }


def publish_paper_trade_entry_autopsy(
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
        "=== Paper trade entry autopsy ===",
        f"generated: {payload.get('generated_at')}",
        str(payload.get("operator_answer") or ""),
        (
            "summary: "
            f"{summary.get('lanes_with_closed_trades', 0)} lanes with closed trades, "
            f"{summary.get('closed_trades', 0)} closed, "
            f"{summary.get('stale_signal_lanes', 0)} stale-entry, "
            f"{summary.get('missing_context_lanes', 0)} missing-context, "
            f"{summary.get('direction_drift_lanes', 0)} direction-drift"
        ),
    ]
    for row in list(payload.get("rows", []))[:limit]:
        avg_delay = row.get("avg_entry_delay_bars")
        delay = "--" if avg_delay is None else f"{_float(avg_delay):.2f} bars"
        lines.append(
            f"  {row.get('entry_state', ''):<26} {row.get('lane_id', ''):<42} "
            f"{row.get('closed_trades', 0):>3} closed "
            f"avg {row.get('avg_net_bps', '--')!s:>7}bps "
            f"delay {delay:<9} "
            f"{row.get('next_action', '')}"
        )
    lines.append("read-only: can_trade=false can_promote=false")
    return "\n".join(lines)


def _lane_evals(
    journal_rows: list[tuple[str, dict[str, Any]]]
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for lane, record in journal_rows:
        if str(record.get("kind") or "") != "lane_eval":
            continue
        payload = record.get("payload") if isinstance(record.get("payload"), Mapping) else {}
        ts = _parse_dt(payload.get("bar_ts") or record.get("ts"))
        if ts is None:
            continue
        row = dict(payload)
        row["lane"] = str(lane)
        row["ts"] = ts.isoformat()
        row["fired"] = bool(payload.get("fired"))
        out[str(lane)].append(row)
    for rows in out.values():
        rows.sort(key=lambda row: str(row.get("ts") or ""))
    return dict(out)


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


def _lane_autopsy(
    lane_id: str,
    trades: list[dict[str, Any]],
    evals: list[dict[str, Any]],
    meta: Mapping[str, Any],
    config: PaperTradeEntryAutopsyConfig,
) -> dict[str, Any]:
    trades = sorted(trades, key=lambda row: str(row.get("entry_ts") or row.get("ts") or ""))
    nets = [_trade_net(row) for row in trades]
    net_bps = [_net_bps(row) for row in trades]
    net_bps = [value for value in net_bps if value is not None]
    fee_bps = [_fee_bps(row) for row in trades]
    fee_bps = [value for value in fee_bps if value is not None]
    contexts = [_entry_context(lane_id, trade, evals, config) for trade in trades]
    closed = len(trades)
    missing = sum(1 for ctx in contexts if ctx["context_state"] == "missing")
    stale = sum(1 for ctx in contexts if ctx["context_state"] == "stale")
    drift = sum(1 for ctx in contexts if ctx["direction_drift"])
    low_edge = sum(
        1
        for ctx in contexts
        if ctx["expected_edge_bps"] is not None
        and ctx["expected_edge_bps"] < config.min_expected_edge_bps
    )
    delay_seconds = [
        _float(ctx.get("signal_age_seconds"))
        for ctx in contexts
        if ctx.get("signal_age_seconds") is not None
    ]
    delay_bars = [
        _float(ctx.get("entry_delay_bars"))
        for ctx in contexts
        if ctx.get("entry_delay_bars") is not None
    ]
    expected_edges = [
        _float(ctx["expected_edge_bps"])
        for ctx in contexts
        if ctx.get("expected_edge_bps") is not None
    ]
    gross_profit = sum(value for value in nets if value > 0)
    gross_loss = abs(sum(value for value in nets if value < 0))
    wins = sum(1 for value in nets if value > 0)
    row = {
        "lane_id": lane_id,
        "exchange": meta.get("exchange", ""),
        "symbol": meta.get("symbol", ""),
        "timeframe": meta.get("timeframe", ""),
        "strategy_id": meta.get("strategy_id", ""),
        "mode": meta.get("mode", ""),
        "closed_trades": closed,
        "wins": wins,
        "losses": sum(1 for value in nets if value < 0),
        "win_rate": round(wins / closed, 4) if closed else None,
        "profit_factor": _profit_factor(gross_profit, gross_loss, wins, closed),
        "net_pnl_usd": round(sum(nets), 6),
        "avg_net_bps": round(_avg(net_bps), 4) if net_bps else None,
        "avg_fee_bps": round(_avg(fee_bps), 4) if fee_bps else None,
        "avg_expected_edge_bps": round(_avg(expected_edges), 4) if expected_edges else None,
        "avg_signal_age_seconds": round(_avg(delay_seconds), 2) if delay_seconds else None,
        "avg_entry_delay_bars": round(_avg(delay_bars), 4) if delay_bars else None,
        "missing_context_rate": round(missing / closed, 4) if closed else 0.0,
        "stale_entry_rate": round(stale / closed, 4) if closed else 0.0,
        "direction_drift_rate": round(drift / closed, 4) if closed else 0.0,
        "low_expected_edge_rate": round(low_edge / closed, 4) if closed else 0.0,
        "matched_signal_count": sum(1 for ctx in contexts if ctx.get("matched_fired")),
        "evals_seen": len(evals),
        "live_evals_seen": sum(1 for row in evals if not row.get("backfill")),
        "fired_evals_seen": sum(1 for row in evals if row.get("fired")),
        "recent_entries": [
            _slim_context(trade, ctx)
            for trade, ctx in zip(trades[-5:], contexts[-5:])
        ],
    }
    state, action, blockers = _diagnose(row, config)
    row["entry_state"] = state
    row["next_action"] = action
    row["blockers"] = blockers
    return row


def _entry_context(
    lane_id: str,
    trade: Mapping[str, Any],
    evals: list[dict[str, Any]],
    config: PaperTradeEntryAutopsyConfig,
) -> dict[str, Any]:
    entry_dt = _parse_dt(trade.get("entry_ts"))
    if entry_dt is None:
        return {
            "context_state": "missing",
            "matched_fired": False,
            "direction_drift": False,
            "signal_age_seconds": None,
            "entry_delay_bars": None,
            "expected_edge_bps": None,
            "reason": "missing entry timestamp",
        }
    fired_before = [
        row
        for row in evals
        if row.get("fired")
        and _parse_dt(row.get("ts")) is not None
        and _parse_dt(row.get("ts")) <= entry_dt
    ]
    latest_eval_before = [
        row for row in evals
        if _parse_dt(row.get("ts")) is not None and _parse_dt(row.get("ts")) <= entry_dt
    ]
    matched = fired_before[-1] if fired_before else None
    fallback = latest_eval_before[-1] if latest_eval_before else None
    if matched is None:
        return {
            "context_state": "missing",
            "matched_fired": False,
            "direction_drift": False,
            "signal_age_seconds": None,
            "entry_delay_bars": None,
            "expected_edge_bps": None,
            "reason": str((fallback or {}).get("skip_reason") or "no prior fired lane_eval"),
            "nearest_eval_fired": bool((fallback or {}).get("fired")),
        }
    signal_dt = _parse_dt(matched.get("ts"))
    age = (entry_dt - signal_dt).total_seconds() if signal_dt is not None else None
    tf = _lane_timeframe_seconds(lane_id)
    if tf <= 0:
        tf = _timeframe_seconds(str(matched.get("timeframe") or ""))
    delay_bars = age / tf if age is not None and tf > 0 else None
    allowed_age = max(
        float(config.max_signal_age_seconds),
        float(tf) * float(config.max_signal_age_bars) if tf > 0 else 0.0,
    )
    signal = matched.get("signal") if isinstance(matched.get("signal"), Mapping) else {}
    signal_side = _norm_side(signal.get("side"))
    trade_side = _norm_side(trade.get("side"))
    direction_drift = bool(signal_side and trade_side and signal_side != trade_side)
    expected_edge = _expected_edge_bps(matched, trade_side or signal_side)
    stale = bool(age is not None and age > allowed_age)
    return {
        "context_state": "stale" if stale else "matched",
        "matched_fired": True,
        "direction_drift": direction_drift,
        "signal_age_seconds": round(age, 2) if age is not None else None,
        "entry_delay_bars": round(delay_bars, 4) if delay_bars is not None else None,
        "expected_edge_bps": round(expected_edge, 4) if expected_edge is not None else None,
        "signal_side": signal_side,
        "trade_side": trade_side,
        "reason": str(matched.get("signal_reason") or signal.get("reason") or ""),
    }


def _expected_edge_bps(row: Mapping[str, Any], side: str) -> float | None:
    features = row.get("features") if isinstance(row.get("features"), Mapping) else {}
    keys = []
    if side == "long":
        keys.extend(("expected_net_edge_bps_long", "algo_expected_net_edge_bps_long"))
    elif side == "short":
        keys.extend(("expected_net_edge_bps_short", "algo_expected_net_edge_bps_short"))
    keys.extend(("expected_net_edge_bps", "expected_edge_bps", "edge_bps"))
    for key in keys:
        value = features.get(key, row.get(key))
        if value is not None:
            return _float(value)
    return None


def _diagnose(
    row: Mapping[str, Any], config: PaperTradeEntryAutopsyConfig
) -> tuple[str, str, list[str]]:
    closed = int(row.get("closed_trades") or 0)
    net = _float(row.get("net_pnl_usd"))
    pf = _float(row.get("profit_factor"))
    avg_net = _float(row.get("avg_net_bps")) if row.get("avg_net_bps") is not None else None
    avg_expected = (
        _float(row.get("avg_expected_edge_bps"))
        if row.get("avg_expected_edge_bps") is not None
        else None
    )
    missing = _float(row.get("missing_context_rate"))
    stale = _float(row.get("stale_entry_rate"))
    drift = _float(row.get("direction_drift_rate"))
    low_edge = _float(row.get("low_expected_edge_rate"))
    blockers: list[str] = []

    if closed <= 0:
        return (
            STATE_NO_CLOSED_TRADES,
            "collect closed paper entries before judging entry quality",
            ["no closed paper fills reconstructed"],
        )
    if missing > config.max_missing_context_rate:
        blockers.append(f"missing fired signal context {missing:.0%}")
        return (
            STATE_CONTEXT_MISSING,
            "journal signal-to-order linkage before trusting this paper lane",
            blockers,
        )
    if drift > config.max_direction_drift_rate:
        blockers.append(f"entry side disagrees with signal {drift:.0%}")
        return (
            STATE_DIRECTION_DRIFT,
            "repair side mapping before any promotion discussion",
            blockers,
        )
    if stale > config.max_stale_entry_rate:
        blockers.append(f"stale entries {stale:.0%}")
        return (
            STATE_SIGNAL_STALE,
            "tighten signal TTL; reject entries after the setup has decayed",
            blockers,
        )
    if (
        avg_expected is not None
        and avg_expected < config.min_expected_edge_bps
        and (net <= 0 or low_edge >= 0.5)
    ):
        blockers.append(
            f"expected edge {avg_expected:.2f}bps below {config.min_expected_edge_bps:.2f}bps"
        )
        return (
            STATE_FEE_WALL_TOO_SMALL,
            "raise expected-move gate or force maker-only until edge clears fees",
            blockers,
        )
    under_sampled = closed < config.min_closed_trades
    if under_sampled:
        blockers.append(f"sample {closed} < {config.min_closed_trades}")
    if (
        not under_sampled
        and net >= 0
        and pf >= config.min_profit_factor
        and avg_net is not None
        and avg_net >= config.min_avg_net_bps
    ):
        return (
            STATE_CAPTURE_HEALTHY,
            "keep collecting; candidate still needs normal promotion proof",
            blockers,
        )
    if avg_net is not None and net <= 0 and avg_net < config.fee_wall_bps:
        blockers.append(f"avg net {avg_net:.2f}bps versus fee wall {config.fee_wall_bps:.2f}bps")
        return (
            STATE_FEE_WALL_TOO_SMALL,
            "require larger expected move or maker-first execution before paper promotion",
            blockers,
        )
    if under_sampled:
        return (
            STATE_UNDER_SAMPLED,
            "observe more closed entries before lane action",
            blockers,
        )
    if net < 0:
        blockers.append("closed paper net is negative after fees")
        return (
            STATE_NEGATIVE_AFTER_COST,
            "return lane to research and mine entry-context failure clusters",
            blockers,
        )
    return (
        STATE_OBSERVE_MORE,
        "positive but below entry-quality proof; collect more outcomes",
        blockers,
    )


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    states = Counter(str(row.get("entry_state") or "") for row in rows)
    closed_rows = [row for row in rows if int(row.get("closed_trades") or 0) > 0]
    closed = sum(int(row.get("closed_trades") or 0) for row in rows)
    net = sum(_float(row.get("net_pnl_usd")) for row in rows)
    return {
        "total_lanes": len(rows),
        "lanes_with_closed_trades": len(closed_rows),
        "closed_trades": closed,
        "negative_lanes": sum(1 for row in closed_rows if _float(row.get("net_pnl_usd")) < 0),
        "healthy_entries": states[STATE_CAPTURE_HEALTHY],
        "stale_signal_lanes": states[STATE_SIGNAL_STALE],
        "missing_context_lanes": states[STATE_CONTEXT_MISSING],
        "direction_drift_lanes": states[STATE_DIRECTION_DRIFT],
        "fee_wall_too_small_lanes": states[STATE_FEE_WALL_TOO_SMALL],
        "under_sampled_lanes": states[STATE_UNDER_SAMPLED],
        "net_pnl_usd": round(net, 6),
        "state_counts": dict(sorted(states.items())),
        "can_trade": False,
        "can_promote": False,
    }


def _operator_answer(summary: Mapping[str, Any]) -> str:
    closed = int(summary.get("closed_trades") or 0)
    if closed <= 0:
        return "No closed paper trades are available for entry autopsy yet."
    if int(summary.get("missing_context_lanes") or 0):
        return (
            "Some paper entries cannot be linked to fired lane_eval records; "
            "fix journaling before trusting trade quality."
        )
    if int(summary.get("direction_drift_lanes") or 0):
        return (
            "Some paper entries disagree with their fired signal direction; "
            "side mapping must be repaired before promotion."
        )
    if int(summary.get("stale_signal_lanes") or 0):
        return (
            "Some paper entries opened after the signal had decayed; tighten "
            "signal TTL and reject late entries."
        )
    if int(summary.get("fee_wall_too_small_lanes") or 0):
        return (
            "Some paper entries are too small to clear the fee wall; require "
            "larger expected move or maker-first routing."
        )
    if int(summary.get("healthy_entries") or 0):
        return (
            "At least one lane has healthy paper entry capture, but normal "
            "sample and promotion proof still apply."
        )
    return "Paper entries are linked, but no lane has enough healthy entry evidence for promotion."


def _trade_notional(trade: Mapping[str, Any]) -> float:
    notional = _float(trade.get("notional_usd"))
    if notional > 0:
        return notional
    entry = _float(trade.get("entry_price"))
    qty = abs(_float(trade.get("quantity")))
    return entry * qty if entry > 0 and qty > 0 else 0.0


def _net_bps(trade: Mapping[str, Any]) -> float | None:
    notional = _trade_notional(trade)
    if notional <= 0:
        return None
    return _trade_net(dict(trade)) / notional * 10_000.0


def _fee_bps(trade: Mapping[str, Any]) -> float | None:
    notional = _trade_notional(trade)
    if notional <= 0:
        return None
    return _float(trade.get("fee_usd"), _float(trade.get("fees_usd"))) / notional * 10_000.0


def _profit_factor(gross_profit: float, gross_loss: float, wins: int, closed: int) -> float:
    if closed <= 0:
        return 0.0
    if gross_loss <= 1e-12:
        return 999.0 if wins > 0 else 0.0
    return round(gross_profit / gross_loss, 4)


def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _norm_side(value: object) -> str:
    text = str(value or "").lower()
    if text in {"long", "buy"}:
        return "long"
    if text in {"short", "sell"}:
        return "short"
    return ""


def _timeframe_seconds(value: str) -> int:
    return {
        "1m": 60,
        "3m": 180,
        "5m": 300,
        "15m": 900,
        "30m": 1800,
        "1h": 3600,
        "2h": 7200,
        "4h": 14400,
        "6h": 21600,
        "1d": 86400,
    }.get(value, 0)


def _slim_context(trade: Mapping[str, Any], ctx: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "entry_ts": trade.get("entry_ts", ""),
        "exit_ts": trade.get("ts", ""),
        "symbol": trade.get("symbol", ""),
        "side": trade.get("side", ""),
        "net_pnl_usd": round(_trade_net(dict(trade)), 6),
        "net_bps": round(_net_bps(trade), 4) if _net_bps(trade) is not None else None,
        "context_state": ctx.get("context_state", ""),
        "signal_age_seconds": ctx.get("signal_age_seconds"),
        "entry_delay_bars": ctx.get("entry_delay_bars"),
        "expected_edge_bps": ctx.get("expected_edge_bps"),
        "direction_drift": bool(ctx.get("direction_drift")),
        "reason": ctx.get("reason", ""),
    }


_STATE_ORDER = {
    STATE_CONTEXT_MISSING: 0,
    STATE_DIRECTION_DRIFT: 1,
    STATE_SIGNAL_STALE: 2,
    STATE_FEE_WALL_TOO_SMALL: 3,
    STATE_NEGATIVE_AFTER_COST: 4,
    STATE_UNDER_SAMPLED: 5,
    STATE_NO_CLOSED_TRADES: 6,
    STATE_OBSERVE_MORE: 7,
    STATE_CAPTURE_HEALTHY: 8,
}


def _row_sort_key(row: Mapping[str, Any]) -> tuple[int, float, str]:
    state = str(row.get("entry_state") or "")
    net = _float(row.get("net_pnl_usd"))
    return (_STATE_ORDER.get(state, 99), net, str(row.get("lane_id") or ""))


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _rate_float(value: str) -> float:
    parsed = float(value)
    if not 0.0 <= parsed <= 1.0:
        raise argparse.ArgumentTypeError("must be between 0 and 1")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal-dir", type=Path, default=DEFAULT_JOURNAL_DIR)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--feed", type=Path, default=DEFAULT_FEED)
    parser.add_argument("--interval-seconds", type=_positive_float, default=60.0)
    parser.add_argument("--tail-bytes", type=_positive_int, default=8_000_000)
    parser.add_argument("--max-rows", type=_positive_int, default=120)
    parser.add_argument("--min-closed-trades", type=_positive_int, default=5)
    parser.add_argument("--min-profit-factor", type=_positive_float, default=1.5)
    parser.add_argument("--min-avg-net-bps", type=_positive_float, default=25.0)
    parser.add_argument("--min-expected-edge-bps", type=_positive_float, default=25.0)
    parser.add_argument("--fee-wall-bps", type=_positive_float, default=8.0)
    parser.add_argument("--max-signal-age-seconds", type=_positive_float, default=600.0)
    parser.add_argument("--max-signal-age-bars", type=_positive_float, default=2.25)
    parser.add_argument("--max-stale-entry-rate", type=_rate_float, default=0.25)
    parser.add_argument("--max-missing-context-rate", type=_rate_float, default=0.20)
    parser.add_argument("--max-direction-drift-rate", type=_rate_float, default=0.05)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--print", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = PaperTradeEntryAutopsyConfig(
        tail_bytes=args.tail_bytes,
        max_rows=args.max_rows,
        min_closed_trades=args.min_closed_trades,
        min_profit_factor=args.min_profit_factor,
        min_avg_net_bps=args.min_avg_net_bps,
        min_expected_edge_bps=args.min_expected_edge_bps,
        fee_wall_bps=args.fee_wall_bps,
        max_signal_age_seconds=args.max_signal_age_seconds,
        max_signal_age_bars=args.max_signal_age_bars,
        max_stale_entry_rate=args.max_stale_entry_rate,
        max_missing_context_rate=args.max_missing_context_rate,
        max_direction_drift_rate=args.max_direction_drift_rate,
    )
    while True:
        payload = build_paper_trade_entry_autopsy(
            journal_dir=args.journal_dir,
            config=config,
        )
        publish_paper_trade_entry_autopsy(payload, args.out, args.feed)
        if args.print:
            print(render_report(payload))
        if args.once:
            return 0
        time.sleep(args.interval_seconds)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
