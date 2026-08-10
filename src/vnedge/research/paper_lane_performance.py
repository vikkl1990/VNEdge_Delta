"""Paper lane performance ledger.

This is the paper-stage companion to ``paper_lane_activation``. Activation
answers "is the lane wired and alive?"; this module answers "what has the lane
actually produced so far?" from append-only decision journals and fill ledgers.

Read-only by design: it cannot start, stop, promote, or trade a lane.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from collections import Counter, defaultdict, deque
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_JOURNAL_DIR = Path("logs/paper_trials")
DEFAULT_OUT = Path("research/live_research/paper_lane_performance_latest.json")
DEFAULT_FEED = Path("research/live_research/paper_lane_performance_feed.jsonl")

STATE_PAPER_PROMOTION_CANDIDATE = "PAPER_PROMOTION_CANDIDATE"
STATE_PAPER_ACTIVE_PROFITABLE = "PAPER_ACTIVE_PROFITABLE"
STATE_PAPER_ACTIVE_NEGATIVE = "PAPER_ACTIVE_NEGATIVE"
STATE_PAPER_ACTIVE_FLAT = "PAPER_ACTIVE_FLAT"
STATE_PAPER_ONLINE_NO_TRADES = "PAPER_ONLINE_NO_TRADES"
STATE_NO_RECENT_PROOF = "NO_RECENT_PROOF"
STATE_LEDGER_CORRUPT = "LEDGER_CORRUPT"


@dataclass(frozen=True)
class PaperLanePerformanceConfig:
    tail_bytes: int = 6_000_000
    max_rows: int = 180
    min_closed_trades: int = 20
    min_profit_factor: float = 1.5
    min_net_pnl_usd: float = 0.0
    stale_after_hours: float = 3.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _ClosedTradeStats:
    nets: list[float]
    notional_usd: float
    unpaired_closing_fills: int
    open_fill_count: int
    open_entry_fees_usd: float


def build_paper_lane_performance(
    *,
    journal_dir: Path | str = DEFAULT_JOURNAL_DIR,
    config: PaperLanePerformanceConfig = PaperLanePerformanceConfig(),
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    root = Path(journal_dir)
    journal_rows = _journal_index(root, config=config)
    fill_rows = _fill_index(root, config=config)
    all_lanes = sorted(set(journal_rows) | set(fill_rows))
    rows = [
        _lane_row(lane, journal_rows.get(lane, []), fill_rows.get(lane, []), now, config)
        for lane in all_lanes
    ]
    rows = [r for r in rows if _is_paper_surface(r)]
    rows.sort(key=_row_sort_key)
    rows = rows[: max(1, int(config.max_rows))]
    summary = _summary(rows)
    return {
        "generated_at": now.isoformat(),
        "report_id": "paper_lane_performance_v1",
        "mode": "read_only_paper_performance",
        "config": config.to_dict(),
        "inputs": {"journal_dir": str(root)},
        "summary": summary,
        "boards": _boards(rows),
        "rows": rows,
        "operator_answer": _operator_answer(summary),
        "policy": {
            "read_only": True,
            "can_trade": False,
            "can_promote": False,
            "promotion_requires_human_judgment": True,
        },
        "can_trade": False,
        "can_promote": False,
    }


def publish_paper_lane_performance(
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
        "=== Paper lane performance ===",
        f"generated: {payload.get('generated_at')}",
        str(payload.get("operator_answer") or ""),
        (
            "summary: "
            f"{summary.get('total_lanes', 0)} lanes, "
            f"{summary.get('lanes_with_closed_trades', 0)} with closed trades, "
            f"{summary.get('promotion_candidates', 0)} promotion candidates, "
            f"net ${summary.get('net_pnl_usd', 0.0):.2f}, "
            f"fees ${summary.get('fees_usd', 0.0):.2f}"
        ),
    ]
    for row in list(payload.get("rows", []))[:limit]:
        lines.append(
            f"  {row.get('state', ''):<28} {row.get('lane_id', ''):<38} "
            f"{row.get('closed_trades', 0):>3} trades "
            f"net ${row.get('net_pnl_usd', 0.0):>8.2f} "
            f"PF {row.get('profit_factor', 0.0):>5.2f} "
            f"{row.get('next_action', '')}"
        )
    lines.append("read-only: can_trade=false can_promote=false")
    return "\n".join(lines)


def _journal_index(
    root: Path, *, config: PaperLanePerformanceConfig
) -> dict[str, list[dict[str, Any]]]:
    rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if not root.is_dir():
        return rows
    for path in sorted(root.glob("*.journal.jsonl")):
        lane = path.name.removesuffix(".journal.jsonl")
        for record in _iter_jsonl(path, max_bytes=config.tail_bytes):
            if isinstance(record, Mapping):
                rows[lane].append(dict(record))
    return rows


def _fill_index(
    root: Path, *, config: PaperLanePerformanceConfig
) -> dict[str, list[dict[str, Any]]]:
    rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if not root.is_dir():
        return rows
    for path in sorted(root.glob("*.fills.jsonl")):
        lane = path.name.removesuffix(".fills.jsonl")
        ok = _verify_chain(path)
        for record in _iter_jsonl(path, max_bytes=config.tail_bytes):
            if isinstance(record, Mapping):
                row = dict(record)
                row["_ledger_ok"] = ok
                rows[lane].append(row)
    return rows


def _lane_row(
    lane_id: str,
    journals: list[dict[str, Any]],
    fills: list[dict[str, Any]],
    now: datetime,
    config: PaperLanePerformanceConfig,
) -> dict[str, Any]:
    counters: Counter[str] = Counter()
    latest_ts: str | None = None
    first_ts: str | None = None
    latest_bar_ts: str | None = None
    latest_why = ""
    latest_signal_reason = ""
    exchange = ""
    symbol = ""
    timeframe = ""
    strategy_id = ""
    mode = ""

    for record in journals:
        ts = _record_ts(record)
        if ts:
            first_ts = first_ts or ts
            latest_ts = ts
        kind = str(record.get("kind") or "")
        counters[kind] += 1
        payload = record.get("payload") if isinstance(record.get("payload"), Mapping) else {}
        if kind in {"lane_eval", "paper_lane_heartbeat"}:
            exchange = str(payload.get("exchange") or exchange)
            symbol = str(payload.get("symbol") or symbol)
            timeframe = str(payload.get("timeframe") or timeframe)
            strategy_id = str(payload.get("strategy_id") or strategy_id)
            mode = str(payload.get("mode") or mode)
            latest_bar_ts = str(payload.get("bar_ts") or payload.get("last_bar_ts") or latest_bar_ts or "")
        if kind == "lane_eval":
            if bool(payload.get("fired")) and not bool(payload.get("backfill")):
                counters["live_signals"] += 1
                latest_signal_reason = str(payload.get("signal_reason") or latest_signal_reason)
            if not bool(payload.get("backfill")):
                counters["live_evals"] += 1
            latest_why = str(
                payload.get("skip_reason")
                or payload.get("signal_reason")
                or latest_why
                or ""
            )
        elif kind == "paper_lane_heartbeat":
            latest_why = str(payload.get("why_no_trade") or payload.get("reason") or latest_why)
        elif kind == "order_intent":
            intent = payload.get("intent") if isinstance(payload.get("intent"), Mapping) else {}
            symbol = str(intent.get("symbol") or symbol)
            strategy_id = str(intent.get("strategy_id") or strategy_id)
        elif kind == "live_paper_report":
            report = payload.get("report") if isinstance(payload.get("report"), Mapping) else payload
            if isinstance(report, Mapping):
                symbol = str(report.get("symbol") or symbol)
                strategy_id = str(report.get("strategy_id") or strategy_id)
                mode = str(report.get("mode") or mode)

    realized = 0.0
    fees = 0.0
    ledger_ok = True
    for fill in fills:
        ledger_ok = ledger_ok and bool(fill.get("_ledger_ok", True))
        exchange = str(fill.get("venue") or exchange)
        symbol = str(fill.get("symbol") or symbol)
        strategy_id = str(fill.get("strategy_id") or strategy_id)
        mode = str(fill.get("mode") or mode)
        pnl = _float(fill.get("realized_pnl_usd"))
        fee = _float(fill.get("fee_usd"))
        fees += fee
        realized += pnl

    closed_stats = _closed_trade_stats(fills)
    closing_nets = closed_stats.nets
    closed_net_pnl = sum(closing_nets)
    net_pnl = realized - fees
    gross_profit = sum(x for x in closing_nets if x > 0)
    gross_loss = abs(sum(x for x in closing_nets if x < 0))
    pf = _profit_factor(gross_profit, gross_loss)
    closed = len(closing_nets)
    wins = sum(1 for x in closing_nets if x > 0)
    win_rate = wins / closed if closed else None
    avg_trade = sum(closing_nets) / closed if closed else None
    avg_bps = (
        (closed_net_pnl / closed_stats.notional_usd) * 10_000
        if closed_stats.notional_usd > 0
        else None
    )
    latest_dt = _parse_dt(latest_ts)
    age_hours = (now - latest_dt).total_seconds() / 3600 if latest_dt else None
    state, blockers, next_action = _state(
        closed_trades=closed,
        net_pnl_usd=net_pnl,
        profit_factor=pf,
        ledger_ok=ledger_ok,
        journal_events=sum(counters.values()),
        age_hours=age_hours,
        config=config,
    )
    live_evals = int(counters.get("live_evals") or 0)
    live_signals = int(counters.get("live_signals") or 0)
    order_intents = int(counters.get("order_intent") or 0)
    drift_flags = _journal_drift_flags(closed_stats)
    return {
        "lane_id": lane_id,
        "exchange": exchange or _exchange_hint(lane_id),
        "symbol": symbol,
        "timeframe": timeframe or _timeframe_hint(lane_id),
        "strategy_id": strategy_id or lane_id,
        "mode": mode,
        "state": state,
        "blockers": blockers,
        "next_action": next_action,
        "first_ts": first_ts,
        "latest_ts": latest_ts,
        "latest_bar_ts": latest_bar_ts or None,
        "age_hours": round(age_hours, 4) if age_hours is not None else None,
        "latest_why_no_trade": latest_why or None,
        "latest_signal_reason": latest_signal_reason or None,
        "journal_events": sum(counters.values()),
        "paper_lane_heartbeats": int(counters.get("paper_lane_heartbeat") or 0),
        "evals": int(counters.get("lane_eval") or 0),
        "live_evals": live_evals,
        "live_signals": live_signals,
        "live_fire_rate": round(live_signals / live_evals, 6) if live_evals else None,
        "risk_decisions": int(counters.get("risk_decision") or 0),
        "paper_order_intents": order_intents,
        "paper_order_acknowledged": int(counters.get("order_acknowledged") or 0),
        "paper_exits": int(counters.get("live_paper_exit") or 0),
        "paper_reports": int(counters.get("live_paper_report") or 0),
        "fills": len(fills),
        "closed_trades": closed,
        "wins": wins,
        "losses": sum(1 for x in closing_nets if x < 0),
        "win_rate": round(win_rate, 6) if win_rate is not None else None,
        "gross_profit_usd": round(gross_profit, 6),
        "gross_loss_usd": round(gross_loss, 6),
        "profit_factor": pf,
        "realized_pnl_usd": round(realized, 6),
        "fees_usd": round(fees, 6),
        "net_pnl_usd": round(net_pnl, 6),
        "closed_net_pnl_usd": round(closed_net_pnl, 6),
        "avg_closed_trade_net_usd": round(avg_trade, 6) if avg_trade is not None else None,
        "avg_closed_trade_net_bps": round(avg_bps, 4) if avg_bps is not None else None,
        "open_fill_count": int(closed_stats.open_fill_count),
        "open_position_entry_fees_usd": round(closed_stats.open_entry_fees_usd, 6),
        "unpaired_closing_fills": int(closed_stats.unpaired_closing_fills),
        "journal_drift_flags": drift_flags,
        "ledger_ok": ledger_ok,
        "can_trade": False,
        "can_promote": False,
    }


def _is_paper_surface(row: Mapping[str, Any]) -> bool:
    mode = str(row.get("mode") or "").lower()
    return (
        mode == "paper"
        or int(row.get("paper_order_intents") or 0) > 0
        or int(row.get("fills") or 0) > 0
        or int(row.get("paper_exits") or 0) > 0
        or str(row.get("lane_id") or "").endswith("_paper_observation")
    )


def _state(
    *,
    closed_trades: int,
    net_pnl_usd: float,
    profit_factor: float,
    ledger_ok: bool,
    journal_events: int,
    age_hours: float | None,
    config: PaperLanePerformanceConfig,
) -> tuple[str, list[str], str]:
    if not ledger_ok:
        return (
            STATE_LEDGER_CORRUPT,
            ["fill ledger hash chain failed verification"],
            "stop using this lane's performance until ledger is repaired from source truth",
        )
    if age_hours is None or journal_events == 0:
        return (
            STATE_NO_RECENT_PROOF,
            ["no paper journal or fill-ledger proof found"],
            "start/repair runner before judging paper performance",
        )
    if age_hours > config.stale_after_hours:
        return (
            STATE_NO_RECENT_PROOF,
            [f"latest paper proof is stale: {age_hours:.1f}h old"],
            "restart or inspect the paper runner before judging performance",
        )
    if closed_trades == 0:
        return (
            STATE_PAPER_ONLINE_NO_TRADES,
            ["online, but no closed paper trades yet"],
            "let it run until enough closed trades exist; use why-no-trade to tune scanner",
        )
    if (
        closed_trades >= config.min_closed_trades
        and net_pnl_usd > config.min_net_pnl_usd
        and profit_factor >= config.min_profit_factor
    ):
        return (
            STATE_PAPER_PROMOTION_CANDIDATE,
            [],
            "eligible for human review; still requires untouched judgment and live checklist",
        )
    if net_pnl_usd > 0:
        return (
            STATE_PAPER_ACTIVE_PROFITABLE,
            _sample_blockers(closed_trades, profit_factor, config),
            "continue collecting sample before any promotion decision",
        )
    if net_pnl_usd < 0:
        return (
            STATE_PAPER_ACTIVE_NEGATIVE,
            ["negative paper net after fees"],
            "mine entry/exit failures; do not promote",
        )
    return (
        STATE_PAPER_ACTIVE_FLAT,
        ["flat net performance after fees"],
        "continue or tighten scanner; no promotion evidence yet",
    )


def _sample_blockers(
    closed_trades: int, profit_factor: float, config: PaperLanePerformanceConfig
) -> list[str]:
    blockers: list[str] = []
    if closed_trades < config.min_closed_trades:
        blockers.append(
            f"needs {config.min_closed_trades - closed_trades} more closed trade(s)"
        )
    if profit_factor < config.min_profit_factor:
        blockers.append(
            f"PF {profit_factor:.2f} below {config.min_profit_factor:.2f}"
        )
    return blockers or ["needs human review"]


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(str(r.get("state") or "") for r in rows)
    closed_rows = [r for r in rows if int(r.get("closed_trades") or 0) > 0]
    fees = sum(_float(r.get("fees_usd")) for r in rows)
    net = sum(_float(r.get("net_pnl_usd")) for r in rows)
    closed_net = sum(_float(r.get("closed_net_pnl_usd")) for r in rows)
    return {
        "total_lanes": len(rows),
        "paper_online": sum(
            1 for r in rows if str(r.get("state")) != STATE_NO_RECENT_PROOF
        ),
        "lanes_with_closed_trades": len(closed_rows),
        "promotion_candidates": counts[STATE_PAPER_PROMOTION_CANDIDATE],
        "profitable_lanes": counts[STATE_PAPER_ACTIVE_PROFITABLE] + counts[STATE_PAPER_PROMOTION_CANDIDATE],
        "negative_lanes": counts[STATE_PAPER_ACTIVE_NEGATIVE],
        "online_no_trades": counts[STATE_PAPER_ONLINE_NO_TRADES],
        "stale_or_missing": counts[STATE_NO_RECENT_PROOF],
        "ledger_corrupt": counts[STATE_LEDGER_CORRUPT],
        "journal_events": sum(int(r.get("journal_events") or 0) for r in rows),
        "heartbeats": sum(int(r.get("paper_lane_heartbeats") or 0) for r in rows),
        "evals": sum(int(r.get("evals") or 0) for r in rows),
        "live_signals": sum(int(r.get("live_signals") or 0) for r in rows),
        "order_intents": sum(int(r.get("paper_order_intents") or 0) for r in rows),
        "fills": sum(int(r.get("fills") or 0) for r in rows),
        "closed_trades": sum(int(r.get("closed_trades") or 0) for r in rows),
        "fees_usd": round(fees, 6),
        "net_pnl_usd": round(net, 6),
        "closed_net_pnl_usd": round(closed_net, 6),
        "open_position_entry_fees_usd": round(
            sum(_float(r.get("open_position_entry_fees_usd")) for r in rows), 6
        ),
        "open_fill_count": sum(int(r.get("open_fill_count") or 0) for r in rows),
        "unpaired_closing_fills": sum(
            int(r.get("unpaired_closing_fills") or 0) for r in rows
        ),
        "journal_drift_lanes": sum(1 for r in rows if r.get("journal_drift_flags")),
        "state_counts": dict(sorted(counts.items())),
        "can_trade": False,
        "can_promote": False,
    }


def _boards(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    return {
        "promotion_candidates": [
            _slim(r) for r in rows if r.get("state") == STATE_PAPER_PROMOTION_CANDIDATE
        ],
        "profitable": [
            _slim(r) for r in rows if r.get("state") == STATE_PAPER_ACTIVE_PROFITABLE
        ],
        "negative": [
            _slim(r) for r in rows if r.get("state") == STATE_PAPER_ACTIVE_NEGATIVE
        ],
        "waiting_for_sample": [
            _slim(r) for r in rows if r.get("state") == STATE_PAPER_ONLINE_NO_TRADES
        ],
        "fix_first": [
            _slim(r)
            for r in rows
            if r.get("state") in {STATE_NO_RECENT_PROOF, STATE_LEDGER_CORRUPT}
        ],
    }


def _slim(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "lane_id": row.get("lane_id"),
        "exchange": row.get("exchange"),
        "symbol": row.get("symbol"),
        "timeframe": row.get("timeframe"),
        "strategy_id": row.get("strategy_id"),
        "state": row.get("state"),
        "closed_trades": row.get("closed_trades"),
        "net_pnl_usd": row.get("net_pnl_usd"),
        "profit_factor": row.get("profit_factor"),
        "latest_why_no_trade": row.get("latest_why_no_trade"),
        "next_action": row.get("next_action"),
    }


def _operator_answer(summary: Mapping[str, Any]) -> str:
    candidates = int(summary.get("promotion_candidates") or 0)
    profitable = int(summary.get("profitable_lanes") or 0)
    negative = int(summary.get("negative_lanes") or 0)
    no_trades = int(summary.get("online_no_trades") or 0)
    if candidates:
        return f"{candidates} paper lane(s) have enough positive evidence for human review."
    if profitable:
        return f"{profitable} paper lane(s) are positive but still need sample/PF proof."
    if negative:
        return f"{negative} paper lane(s) are negative after fees; mine entry/exit failures before promotion."
    if no_trades:
        return f"{no_trades} paper lane(s) are online but have not closed trades yet."
    return "No current paper performance proof is available."


def _row_sort_key(row: Mapping[str, Any]) -> tuple[int, float, str]:
    priority = {
        STATE_PAPER_PROMOTION_CANDIDATE: 0,
        STATE_PAPER_ACTIVE_PROFITABLE: 1,
        STATE_PAPER_ACTIVE_NEGATIVE: 2,
        STATE_PAPER_ACTIVE_FLAT: 3,
        STATE_PAPER_ONLINE_NO_TRADES: 4,
        STATE_LEDGER_CORRUPT: 5,
        STATE_NO_RECENT_PROOF: 6,
    }.get(str(row.get("state") or ""), 9)
    return (priority, -_float(row.get("net_pnl_usd")), str(row.get("lane_id") or ""))


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


def _record_ts(record: Mapping[str, Any]) -> str:
    payload = record.get("payload") if isinstance(record.get("payload"), Mapping) else {}
    for key in ("ts", "bar_ts", "resolved_bar_ts"):
        if record.get(key):
            return str(record[key])
        if payload.get(key):
            return str(payload[key])
    return ""


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


def _float(value: object, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed != parsed:
        return default
    return parsed


def _profit_factor(gross_profit: float, gross_loss: float) -> float:
    if gross_loss > 0:
        return round(gross_profit / gross_loss, 6)
    if gross_profit > 0:
        return 999.0
    return 0.0


def _closed_trade_stats(fills: list[dict[str, Any]]) -> _ClosedTradeStats:
    """Pair paper fills into complete trades and charge both entry + exit fees.

    Fill ledgers record realized PnL on the closing fill, while the opening fill
    carries its own fee. Lane PF/bps must therefore be computed from complete
    entry->exit pairs; otherwise the per-trade view drifts from fleet net PnL.
    """
    open_fifo: deque[dict[str, Any]] = deque()
    nets: list[float] = []
    notional = 0.0
    unpaired = 0
    for fill in sorted(fills, key=lambda row: str(row.get("ts") or "")):
        realized = _float(fill.get("realized_pnl_usd"))
        if abs(realized) <= 1e-12:
            open_fifo.append(fill)
            continue
        entry = open_fifo.popleft() if open_fifo else None
        if entry is None:
            unpaired += 1
        entry_fee = _float(entry.get("fee_usd")) if entry else 0.0
        exit_fee = _float(fill.get("fee_usd"))
        nets.append(realized - entry_fee - exit_fee)
        price = _float(fill.get("price"))
        qty = abs(_float(fill.get("quantity")))
        if price > 0 and qty > 0:
            notional += price * qty
    return _ClosedTradeStats(
        nets=nets,
        notional_usd=notional,
        unpaired_closing_fills=unpaired,
        open_fill_count=len(open_fifo),
        open_entry_fees_usd=sum(_float(fill.get("fee_usd")) for fill in open_fifo),
    )


def _journal_drift_flags(stats: _ClosedTradeStats) -> list[str]:
    flags: list[str] = []
    if stats.unpaired_closing_fills:
        flags.append(f"{stats.unpaired_closing_fills} unpaired closing fill(s)")
    if stats.open_fill_count:
        flags.append(f"{stats.open_fill_count} open fill(s) awaiting close")
    if stats.open_entry_fees_usd > 0:
        flags.append(f"${stats.open_entry_fees_usd:.2f} open entry-fee drag")
    return flags


def _verify_chain(path: Path) -> bool:
    try:
        from vnedge.execution.fill_ledger import verify_chain

        return bool(verify_chain(path).ok)
    except (OSError, ValueError):
        return False


def _exchange_hint(lane_id: str) -> str:
    lowered = lane_id.lower()
    if "delta" in lowered:
        return "delta_india"
    if "bybit" in lowered:
        return "bybit"
    if "binance" in lowered:
        return "binanceusdm"
    return ""


def _timeframe_hint(lane_id: str) -> str:
    for token in ("1m", "5m", "15m", "1h", "4h"):
        if f"_{token}" in lane_id.lower() or lane_id.lower().endswith(token):
            return token
    return ""


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal-dir", type=Path, default=DEFAULT_JOURNAL_DIR)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--feed", type=Path, default=DEFAULT_FEED)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval-seconds", type=float, default=60.0)
    parser.add_argument("--min-closed-trades", type=int, default=20)
    parser.add_argument("--min-profit-factor", type=float, default=1.5)
    parser.add_argument("--stale-after-hours", type=float, default=3.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    config = PaperLanePerformanceConfig(
        min_closed_trades=args.min_closed_trades,
        min_profit_factor=args.min_profit_factor,
        stale_after_hours=args.stale_after_hours,
    )
    while True:
        payload = build_paper_lane_performance(
            journal_dir=args.journal_dir,
            config=config,
        )
        publish_paper_lane_performance(payload, args.out, args.feed)
        print(render_report(payload), flush=True)
        if args.once:
            return 0
        time.sleep(max(1.0, float(args.interval_seconds)))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
