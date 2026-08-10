"""Paper trade exit autopsy.

Paper performance already answers whether a lane is positive or negative. This
module answers why the closed paper trades are behaving that way: stops, fee
drag, weak take-profit capture, timeout exits, strategy-managed exits, or
missing exit metadata.

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
DEFAULT_OUT = Path("research/live_research/paper_trade_exit_autopsy_latest.json")
DEFAULT_FEED = Path("research/live_research/paper_trade_exit_autopsy_feed.jsonl")

DRIVER_NO_CLOSED_TRADES = "NO_CLOSED_TRADES"
DRIVER_UNDER_SAMPLED = "UNDER_SAMPLED"
DRIVER_CAPTURE_HEALTHY = "CAPTURE_HEALTHY"
DRIVER_STOP_DOMINATED = "STOP_DOMINATED"
DRIVER_FEE_WALL_DOMINATED = "FEE_WALL_DOMINATED"
DRIVER_TIMEOUT_DOMINATED = "TIMEOUT_DOMINATED"
DRIVER_TP_CAPTURE_WEAK = "TP_CAPTURE_WEAK"
DRIVER_STRATEGY_EXIT_HEALTHY = "STRATEGY_EXIT_CAPTURE_HEALTHY"
DRIVER_STRATEGY_EXIT_DOMINATED = "STRATEGY_EXIT_DOMINATED"
DRIVER_NEGATIVE_EDGE = "NEGATIVE_EDGE"
DRIVER_LEDGER_OR_EXIT_METADATA_GAP = "LEDGER_OR_EXIT_METADATA_GAP"
DRIVER_OBSERVE_MORE = "OBSERVE_MORE"

STRATEGY_EXIT_PREFIX = "strategy_"


@dataclass(frozen=True)
class PaperTradeExitAutopsyConfig:
    tail_bytes: int = 8_000_000
    max_rows: int = 120
    min_closed_trades: int = 5
    min_profit_factor: float = 1.5
    min_avg_net_bps: float = 25.0
    fee_wall_bps: float = 8.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_paper_trade_exit_autopsy(
    *,
    journal_dir: Path | str = DEFAULT_JOURNAL_DIR,
    config: PaperTradeExitAutopsyConfig = PaperTradeExitAutopsyConfig(),
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the read-only paper exit autopsy payload."""

    now = now or datetime.now(UTC)
    root = Path(journal_dir)
    journal_config = TradeJournalConfig(
        tail_bytes=config.tail_bytes,
        max_rows=max(config.max_rows, 1),
    )
    fills = _fill_rows(
        root,
        lane="",
        since=None,
        config=journal_config,
        active=None,
    )
    journal_rows = _journal_rows(
        root,
        lane="",
        since=None,
        config=journal_config,
        active=None,
    )
    _orders, _events, virtual_trades = _project_journals(journal_rows)
    closed = [
        row
        for row in _build_closed_trades(fills, journal_rows, virtual_trades)
        if row.get("kind") == "actual_closing_fill"
    ]
    metadata = _lane_metadata(journal_rows, fills)
    by_lane: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in closed:
        by_lane[str(trade.get("lane") or "")].append(trade)
    for fill in fills:
        lane = str(fill.get("lane") or "")
        by_lane.setdefault(lane, [])

    rows = [
        _lane_autopsy(lane, trades, metadata.get(lane, {}), config)
        for lane, trades in sorted(by_lane.items())
    ]
    rows.sort(key=_row_sort_key)
    rows = rows[: max(1, int(config.max_rows))]
    summary = _summary(rows)

    return {
        "generated_at": now.isoformat(),
        "report_id": "paper_trade_exit_autopsy_v1",
        "mode": "read_only_paper_trade_exit_autopsy",
        "config": config.to_dict(),
        "inputs": {"journal_dir": str(root)},
        "summary": summary,
        "rows": rows,
        "operator_answer": _operator_answer(summary),
        "policy": {
            "read_only": True,
            "can_trade": False,
            "can_promote": False,
            "scope": "closed paper fills only; shadow outcomes stay in trade journal",
        },
        "can_trade": False,
        "can_promote": False,
    }


def publish_paper_trade_exit_autopsy(
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
        "=== Paper trade exit autopsy ===",
        f"generated: {payload.get('generated_at')}",
        str(payload.get("operator_answer") or ""),
        (
            "summary: "
            f"{summary.get('lanes_with_closed_trades', 0)} lanes with closed trades, "
            f"{summary.get('closed_trades', 0)} closed, "
            f"{summary.get('negative_lanes', 0)} negative, "
            f"{summary.get('stop_dominated', 0)} stop-dominated, "
            f"{summary.get('strategy_exit_healthy', 0)} smart-exit healthy, "
            f"{summary.get('fee_wall_dominated', 0)} fee-wall dominated"
        ),
    ]
    for row in list(payload.get("rows", []))[:limit]:
        lines.append(
            f"  {row.get('loss_driver', ''):<28} {row.get('lane_id', ''):<42} "
            f"{row.get('closed_trades', 0):>3} closed "
            f"net ${row.get('net_pnl_usd', 0.0):>8.2f} "
            f"avg {row.get('avg_net_bps', 0.0):>7.2f}bps "
            f"{row.get('next_action', '')}"
        )
    lines.append("read-only: can_trade=false can_promote=false")
    return "\n".join(lines)


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


def _lane_autopsy(
    lane_id: str,
    trades: list[dict[str, Any]],
    meta: Mapping[str, Any],
    config: PaperTradeExitAutopsyConfig,
) -> dict[str, Any]:
    trades = sorted(trades, key=lambda row: str(row.get("ts") or ""))
    nets = [_trade_net(row) for row in trades]
    fees = [_trade_fee(row) for row in trades]
    gross_profit = sum(value for value in nets if value > 0)
    gross_loss = abs(sum(value for value in nets if value < 0))
    closed = len(trades)
    wins = sum(1 for value in nets if value > 0)
    net = sum(nets)
    fee_total = sum(fees)
    net_bps = [_net_bps(row) for row in trades]
    net_bps = [value for value in net_bps if value is not None]
    fee_bps = [_fee_bps(row) for row in trades]
    fee_bps = [value for value in fee_bps if value is not None]
    captured_bps = [
        _float(row.get("captured_bps"))
        for row in trades
        if row.get("captured_bps") is not None
    ]
    holds = [
        _float(row.get("hold_seconds"))
        for row in trades
        if row.get("hold_seconds") is not None
    ]
    resolutions = Counter(_resolution(row) for row in trades)
    exit_families = Counter(_exit_family(_resolution(row)) for row in trades)
    tp_counts = Counter(str(int(_float(row.get("tp_reached")))) for row in trades)
    missing_resolution = sum(1 for row in trades if not _resolution(row))
    strategy_exit_trades = [
        trade for trade in trades if _is_strategy_exit(_resolution(trade))
    ]
    strategy_exit_nets = [_trade_net(row) for row in strategy_exit_trades]
    strategy_exit_bps = [
        value
        for value in (_net_bps(row) for row in strategy_exit_trades)
        if value is not None
    ]
    strategy_exit_net = sum(strategy_exit_nets)
    strategy_exit_wins = sum(1 for value in strategy_exit_nets if value > 0)
    profit_factor = _profit_factor(gross_profit, gross_loss, wins, closed)
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
        "profit_factor": profit_factor,
        "net_pnl_usd": round(net, 6),
        "fees_usd": round(fee_total, 6),
        "avg_net_bps": round(_avg(net_bps), 4) if net_bps else None,
        "avg_fee_bps": round(_avg(fee_bps), 4) if fee_bps else None,
        "avg_gross_captured_bps": round(_avg(captured_bps), 4) if captured_bps else None,
        "avg_hold_seconds": round(_avg(holds), 2) if holds else None,
        "resolution_counts": dict(sorted(resolutions.items())),
        "exit_family_counts": dict(sorted(exit_families.items())),
        "tp_reached_counts": dict(sorted(tp_counts.items())),
        "take_profit_rate": _rate(resolutions, {"take_profit", "target"}, closed),
        "stop_rate": _rate(resolutions, {"stop", "tick_stop"}, closed),
        "timeout_rate": _rate(resolutions, {"max_holding", "timeout"}, closed),
        "strategy_exit_count": len(strategy_exit_trades),
        "strategy_exit_rate": round(len(strategy_exit_trades) / closed, 4) if closed else 0.0,
        "strategy_exit_net_pnl_usd": round(strategy_exit_net, 6),
        "strategy_exit_avg_net_bps": (
            round(_avg(strategy_exit_bps), 4) if strategy_exit_bps else None
        ),
        "strategy_exit_win_rate": (
            round(strategy_exit_wins / len(strategy_exit_trades), 4)
            if strategy_exit_trades
            else None
        ),
        "missing_resolution_rate": round(missing_resolution / closed, 4) if closed else 0.0,
        "recent_trades": [_slim_trade(row) for row in trades[-5:]],
    }
    driver, action, blockers = _diagnose(row, config)
    row["loss_driver"] = driver
    row["next_action"] = action
    row["blockers"] = blockers
    return row


def _diagnose(
    row: Mapping[str, Any], config: PaperTradeExitAutopsyConfig
) -> tuple[str, str, list[str]]:
    closed = int(row.get("closed_trades") or 0)
    net = _float(row.get("net_pnl_usd"))
    pf = _float(row.get("profit_factor"))
    avg_net_bps = row.get("avg_net_bps")
    avg_net = _float(avg_net_bps) if avg_net_bps is not None else None
    avg_fee = _float(row.get("avg_fee_bps")) if row.get("avg_fee_bps") is not None else None
    stop_rate = _float(row.get("stop_rate"))
    timeout_rate = _float(row.get("timeout_rate"))
    tp_rate = _float(row.get("take_profit_rate"))
    strategy_exit_rate = _float(row.get("strategy_exit_rate"))
    strategy_exit_net = _float(row.get("strategy_exit_net_pnl_usd"))
    strategy_exit_avg = (
        _float(row.get("strategy_exit_avg_net_bps"))
        if row.get("strategy_exit_avg_net_bps") is not None
        else None
    )
    missing = _float(row.get("missing_resolution_rate"))
    blockers: list[str] = []

    if closed <= 0:
        return (
            DRIVER_NO_CLOSED_TRADES,
            "collect closed paper exits before judging the lane",
            ["no closed paper fills reconstructed"],
        )
    if missing >= 0.5:
        return (
            DRIVER_LEDGER_OR_EXIT_METADATA_GAP,
            "repair exit reason journaling before reading performance",
            [f"{missing:.0%} of closed trades have no exit reason"],
        )
    under_sampled = closed < config.min_closed_trades
    if under_sampled:
        blockers.append(f"sample {closed} < {config.min_closed_trades}")
    if (
        not under_sampled
        and strategy_exit_rate >= 0.25
        and strategy_exit_net > 0
        and strategy_exit_avg is not None
        and strategy_exit_avg >= config.fee_wall_bps
    ):
        return (
            DRIVER_STRATEGY_EXIT_HEALTHY,
            "keep strategy-managed exits; compare earlier TP/BE capture before promotion",
            blockers,
        )
    if not under_sampled and net < 0 and strategy_exit_rate >= 0.35 and strategy_exit_net <= 0:
        blockers.append(
            f"strategy exits {strategy_exit_rate:.0%}, net ${strategy_exit_net:.2f}"
        )
        return (
            DRIVER_STRATEGY_EXIT_DOMINATED,
            "review neutral/reversal exit timing; smart exits are closing negative trades",
            blockers,
        )
    if (
        not under_sampled
        and net >= 0
        and pf >= config.min_profit_factor
        and avg_net is not None
        and avg_net >= config.min_avg_net_bps
    ):
        return (
            DRIVER_CAPTURE_HEALTHY,
            "keep collecting; candidate still needs normal promotion proof",
            blockers,
        )
    if net < 0 and stop_rate >= 0.55:
        blockers.append(f"stop exits {stop_rate:.0%}")
        return (
            DRIVER_STOP_DOMINATED,
            "tighten entry permission or demote until setup quality improves",
            blockers,
        )
    if net <= 0 and timeout_rate >= 0.4:
        blockers.append(f"timeout exits {timeout_rate:.0%}")
        return (
            DRIVER_TIMEOUT_DOMINATED,
            "shorten stale holds or add earlier invalidation before timeout",
            blockers,
        )
    if avg_net is not None and avg_fee is not None and (
        (net < 0 and avg_net < config.fee_wall_bps)
        or (avg_fee >= max(1.0, abs(avg_net)) * 0.6 and net <= 0)
    ):
        blockers.append(f"avg net {avg_net:.2f}bps versus fee wall {config.fee_wall_bps:.2f}bps")
        return (
            DRIVER_FEE_WALL_DOMINATED,
            "require larger expected move or maker-first execution before paper promotion",
            blockers,
        )
    if tp_rate >= 0.35 and (avg_net is None or avg_net < config.min_avg_net_bps):
        blockers.append(f"TP exits {tp_rate:.0%} but capture below {config.min_avg_net_bps:.1f}bps")
        return (
            DRIVER_TP_CAPTURE_WEAK,
            "improve trail/scale-out rules; TP is firing but capture is too small",
            blockers,
        )
    if under_sampled:
        return (
            DRIVER_UNDER_SAMPLED,
            "observe more closed exits before lane action",
            blockers,
        )
    if net < 0:
        blockers.append("closed paper net is negative after fees")
        return (
            DRIVER_NEGATIVE_EDGE,
            "return lane to research and mine entry/exit failure clusters",
            blockers,
        )
    return (
        DRIVER_OBSERVE_MORE,
        "positive but below promotion quality; collect more outcomes",
        blockers,
    )


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    drivers = Counter(str(row.get("loss_driver") or "") for row in rows)
    closed_rows = [row for row in rows if int(row.get("closed_trades") or 0) > 0]
    total_closed = sum(int(row.get("closed_trades") or 0) for row in rows)
    net = sum(_float(row.get("net_pnl_usd")) for row in rows)
    fees = sum(_float(row.get("fees_usd")) for row in rows)
    return {
        "total_lanes": len(rows),
        "lanes_with_closed_trades": len(closed_rows),
        "closed_trades": total_closed,
        "negative_lanes": sum(1 for row in closed_rows if _float(row.get("net_pnl_usd")) < 0),
        "positive_lanes": sum(1 for row in closed_rows if _float(row.get("net_pnl_usd")) > 0),
        "stop_dominated": drivers[DRIVER_STOP_DOMINATED],
        "fee_wall_dominated": drivers[DRIVER_FEE_WALL_DOMINATED],
        "timeout_dominated": drivers[DRIVER_TIMEOUT_DOMINATED],
        "tp_capture_weak": drivers[DRIVER_TP_CAPTURE_WEAK],
        "strategy_exit_healthy": drivers[DRIVER_STRATEGY_EXIT_HEALTHY],
        "strategy_exit_dominated": drivers[DRIVER_STRATEGY_EXIT_DOMINATED],
        "strategy_exit_trades": sum(int(row.get("strategy_exit_count") or 0) for row in rows),
        "strategy_exit_net_pnl_usd": round(
            sum(_float(row.get("strategy_exit_net_pnl_usd")) for row in rows),
            6,
        ),
        "metadata_gaps": drivers[DRIVER_LEDGER_OR_EXIT_METADATA_GAP],
        "healthy_capture": drivers[DRIVER_CAPTURE_HEALTHY],
        "net_pnl_usd": round(net, 6),
        "fees_usd": round(fees, 6),
        "driver_counts": dict(sorted(drivers.items())),
        "can_trade": False,
        "can_promote": False,
    }


def _operator_answer(summary: Mapping[str, Any]) -> str:
    closed = int(summary.get("closed_trades") or 0)
    if closed <= 0:
        return "No closed paper trades are available for exit autopsy yet."
    if int(summary.get("metadata_gaps") or 0):
        return "Some paper exits lack reason metadata; repair journaling before trusting exit diagnostics."
    if int(summary.get("stop_dominated") or 0):
        return "Stop exits are the primary paper-trade loss driver; entry quality and invalidation need work before promotion."
    if int(summary.get("fee_wall_dominated") or 0):
        return "Fee drag is dominating paper outcomes; lanes need larger expected move or maker-first routing."
    if int(summary.get("strategy_exit_dominated") or 0):
        return "Strategy-managed exits are closing negative paper trades; tune exit timing before promotion."
    if int(summary.get("tp_capture_weak") or 0):
        return "Take-profit exits are firing but capture is too small; improve trail/scale-out rules."
    if int(summary.get("strategy_exit_healthy") or 0):
        return "Strategy-managed exits are preserving edge on at least one lane; keep observing before promotion."
    if int(summary.get("healthy_capture") or 0):
        return "At least one lane has healthy paper exit capture, but normal sample and promotion proof still apply."
    return "Paper trades are closing, but no lane has enough healthy exit evidence for promotion."


def _trade_fee(trade: Mapping[str, Any]) -> float:
    if trade.get("fee_usd") is not None:
        return _float(trade.get("fee_usd"))
    return _float(trade.get("fees_usd"))


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
    return _trade_fee(trade) / notional * 10_000.0


def _resolution(trade: Mapping[str, Any]) -> str:
    return str(trade.get("resolution") or "").strip()


def _is_strategy_exit(trade_or_resolution: Mapping[str, Any] | str) -> bool:
    if isinstance(trade_or_resolution, Mapping):
        resolution = _resolution(trade_or_resolution)
    else:
        resolution = str(trade_or_resolution or "").strip()
    return resolution.startswith(STRATEGY_EXIT_PREFIX)


def _exit_family(resolution: str) -> str:
    resolution = str(resolution or "").strip()
    if not resolution:
        return "missing"
    if resolution in {"stop", "tick_stop"}:
        return "stop"
    if resolution in {"take_profit", "target"}:
        return "take_profit"
    if resolution in {"max_holding", "timeout"}:
        return "timeout"
    if resolution.startswith(STRATEGY_EXIT_PREFIX):
        return "strategy_exit"
    return "other"


def _profit_factor(gross_profit: float, gross_loss: float, wins: int, closed: int) -> float:
    if closed <= 0:
        return 0.0
    if gross_loss <= 1e-12:
        return 999.0 if wins > 0 else 0.0
    return round(gross_profit / gross_loss, 4)


def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _rate(counts: Counter[str], names: set[str], total: int) -> float:
    if total <= 0:
        return 0.0
    return round(sum(counts.get(name, 0) for name in names) / total, 4)


def _slim_trade(trade: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "ts": trade.get("ts", ""),
        "symbol": trade.get("symbol", ""),
        "side": trade.get("side", ""),
        "resolution": trade.get("resolution", ""),
        "exit_family": _exit_family(_resolution(trade)),
        "entry_price": trade.get("entry_price"),
        "exit_price": trade.get("exit_price"),
        "net_pnl_usd": round(_trade_net(dict(trade)), 6),
        "fee_usd": round(_trade_fee(trade), 6),
        "net_bps": round(_net_bps(trade), 4) if _net_bps(trade) is not None else None,
        "tp_reached": int(_float(trade.get("tp_reached"))),
        "hold_seconds": trade.get("hold_seconds"),
    }


_DRIVER_ORDER = {
    DRIVER_LEDGER_OR_EXIT_METADATA_GAP: 0,
    DRIVER_STOP_DOMINATED: 1,
    DRIVER_FEE_WALL_DOMINATED: 2,
    DRIVER_TIMEOUT_DOMINATED: 3,
    DRIVER_TP_CAPTURE_WEAK: 4,
    DRIVER_STRATEGY_EXIT_DOMINATED: 5,
    DRIVER_NEGATIVE_EDGE: 6,
    DRIVER_UNDER_SAMPLED: 7,
    DRIVER_NO_CLOSED_TRADES: 8,
    DRIVER_OBSERVE_MORE: 9,
    DRIVER_STRATEGY_EXIT_HEALTHY: 10,
    DRIVER_CAPTURE_HEALTHY: 11,
}


def _row_sort_key(row: Mapping[str, Any]) -> tuple[int, float, str]:
    driver = str(row.get("loss_driver") or "")
    net = _float(row.get("net_pnl_usd"))
    return (_DRIVER_ORDER.get(driver, 99), net, str(row.get("lane_id") or ""))


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
    parser.add_argument("--fee-wall-bps", type=_positive_float, default=8.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--print", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = PaperTradeExitAutopsyConfig(
        tail_bytes=args.tail_bytes,
        max_rows=args.max_rows,
        min_closed_trades=args.min_closed_trades,
        min_profit_factor=args.min_profit_factor,
        min_avg_net_bps=args.min_avg_net_bps,
        fee_wall_bps=args.fee_wall_bps,
    )
    while True:
        payload = build_paper_trade_exit_autopsy(
            journal_dir=args.journal_dir,
            config=config,
        )
        publish_paper_trade_exit_autopsy(payload, args.out, args.feed)
        if args.print:
            print(render_report(payload))
        if args.once:
            return 0
        time.sleep(args.interval_seconds)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
