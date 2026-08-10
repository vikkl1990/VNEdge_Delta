"""Trade Analyzer OS.

This is the joined, operator-facing analyst layer over paper trades.  The
separate entry and exit autopsies answer useful narrow questions; this module
turns them into one lane verdict and one recent-trade card stream:

* did the trade have fresh signal context?
* did it clear the fee wall?
* did it reach profit and give it back?
* did exits preserve or leak the move?

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
    _float,
    _trade_net,
    build_trade_journal,
)
from vnedge.research.paper_trade_entry_autopsy import (
    PaperTradeEntryAutopsyConfig,
    build_paper_trade_entry_autopsy,
)
from vnedge.research.paper_trade_exit_autopsy import (
    PaperTradeExitAutopsyConfig,
    build_paper_trade_exit_autopsy,
)

DEFAULT_JOURNAL_DIR = Path("logs/paper_trials")
DEFAULT_OUT = Path("research/live_research/trade_analyzer_os_latest.json")
DEFAULT_FEED = Path("research/live_research/trade_analyzer_os_feed.jsonl")

DIAG_NO_CLOSED_TRADES = "NO_CLOSED_TRADES"
DIAG_ENTRY_CONTEXT_GAP = "ENTRY_CONTEXT_GAP"
DIAG_STALE_OR_DRIFT_ENTRY = "STALE_OR_DRIFT_ENTRY"
DIAG_GIVEBACK_DOMINATED = "GIVEBACK_DOMINATED"
DIAG_STOP_DOMINATED = "STOP_DOMINATED"
DIAG_FEE_WALL_DOMINATED = "FEE_WALL_DOMINATED"
DIAG_OVERNIGHT_HOLD_DRIFT = "OVERNIGHT_HOLD_DRIFT"
DIAG_WEAK_FOLLOW_THROUGH = "WEAK_FOLLOW_THROUGH"
DIAG_EXIT_METADATA_GAP = "EXIT_METADATA_GAP"
DIAG_HEALTHY_CAPTURE = "HEALTHY_CAPTURE"
DIAG_OBSERVE_MORE = "OBSERVE_MORE"


@dataclass(frozen=True)
class TradeAnalyzerOSConfig:
    tail_bytes: int = 8_000_000
    max_rows: int = 120
    max_trade_cards: int = 80
    min_closed_trades: int = 5
    min_profit_factor: float = 1.5
    min_avg_net_bps: float = 25.0
    fee_wall_bps: float = 8.0
    giveback_arm_bps: float = 15.0
    giveback_min_bps: float = 10.0
    max_intraday_hold_seconds: float = 24 * 60 * 60

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_trade_analyzer_os(
    *,
    journal_dir: Path | str = DEFAULT_JOURNAL_DIR,
    config: TradeAnalyzerOSConfig = TradeAnalyzerOSConfig(),
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a read-only trade analyzer payload from append-only artifacts."""

    now = now or datetime.now(UTC)
    root = Path(journal_dir)
    journal_config = TradeJournalConfig(
        tail_bytes=config.tail_bytes,
        max_rows=max(config.max_rows, config.max_trade_cards, 1),
    )
    journal = build_trade_journal(
        snapshot=None,
        journal_dir=root,
        lane="",
        limit=max(config.max_rows, config.max_trade_cards, 1),
        config=journal_config,
    )
    entry = build_paper_trade_entry_autopsy(
        journal_dir=root,
        config=PaperTradeEntryAutopsyConfig(
            tail_bytes=config.tail_bytes,
            max_rows=config.max_rows,
            min_closed_trades=config.min_closed_trades,
            min_profit_factor=config.min_profit_factor,
            min_avg_net_bps=config.min_avg_net_bps,
            fee_wall_bps=config.fee_wall_bps,
        ),
        now=now,
    )
    exit_ = build_paper_trade_exit_autopsy(
        journal_dir=root,
        config=PaperTradeExitAutopsyConfig(
            tail_bytes=config.tail_bytes,
            max_rows=config.max_rows,
            min_closed_trades=config.min_closed_trades,
            min_profit_factor=config.min_profit_factor,
            min_avg_net_bps=config.min_avg_net_bps,
            fee_wall_bps=config.fee_wall_bps,
        ),
        now=now,
    )

    actual_trades = [
        trade
        for trade in journal.get("closed_trades", [])
        if trade.get("kind") == "actual_closing_fill"
    ]
    trades_by_lane: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in actual_trades:
        trades_by_lane[str(trade.get("lane") or "")].append(trade)

    entry_by_lane = {
        str(row.get("lane_id") or ""): row
        for row in entry.get("rows", [])
        if row.get("lane_id")
    }
    exit_by_lane = {
        str(row.get("lane_id") or ""): row
        for row in exit_.get("rows", [])
        if row.get("lane_id")
    }
    lane_ids = sorted(
        set(trades_by_lane) | set(entry_by_lane) | set(exit_by_lane)
    )
    rows = [
        _lane_analysis(
            lane_id=lane,
            trades=trades_by_lane.get(lane, []),
            entry=entry_by_lane.get(lane, {}),
            exit_=exit_by_lane.get(lane, {}),
            config=config,
        )
        for lane in lane_ids
        if lane
    ]
    rows.sort(key=_row_sort_key)
    rows = rows[: max(1, int(config.max_rows))]
    trade_cards = _recent_trade_cards(actual_trades, config)
    summary = _summary(rows, trade_cards)

    return {
        "generated_at": now.isoformat(),
        "report_id": "trade_analyzer_os_v1",
        "mode": "read_only_trade_analyzer_os",
        "config": config.to_dict(),
        "inputs": {"journal_dir": str(root)},
        "summary": summary,
        "rows": rows,
        "recent_trades": trade_cards,
        "operator_answer": _operator_answer(summary),
        "joined_reports": {
            "entry_autopsy": entry.get("report_id"),
            "exit_autopsy": exit_.get("report_id"),
            "trade_journal": journal.get("generated_at"),
        },
        "policy": {
            "read_only": True,
            "can_trade": False,
            "can_promote": False,
            "scope": "closed paper fills joined to entry context and exit metadata",
        },
        "can_trade": False,
        "can_promote": False,
    }


def publish_trade_analyzer_os(
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
        "=== Trade Analyzer OS ===",
        f"generated: {payload.get('generated_at')}",
        str(payload.get("operator_answer") or ""),
        (
            "summary: "
            f"{summary.get('closed_trades', 0)} closed, "
            f"{summary.get('negative_lanes', 0)} negative lanes, "
            f"{summary.get('giveback_dominated', 0)} giveback, "
            f"{summary.get('fee_wall_dominated', 0)} fee-wall, "
            f"{summary.get('healthy_capture', 0)} healthy"
        ),
    ]
    for row in list(payload.get("rows", []))[:limit]:
        lines.append(
            f"  {row.get('primary_diagnosis', ''):<24} {row.get('lane_id', ''):<42} "
            f"{row.get('closed_trades', 0):>3} closed "
            f"net ${row.get('net_pnl_usd', 0.0):>8.2f} "
            f"avg {row.get('avg_net_bps', '--')!s:>7}bps "
            f"{row.get('next_action', '')}"
        )
    lines.append("read-only: can_trade=false can_promote=false")
    return "\n".join(lines)


def _lane_analysis(
    *,
    lane_id: str,
    trades: list[dict[str, Any]],
    entry: Mapping[str, Any],
    exit_: Mapping[str, Any],
    config: TradeAnalyzerOSConfig,
) -> dict[str, Any]:
    cards = [_trade_card(trade, config) for trade in trades]
    closed = len(cards)
    net = sum(_float(card.get("net_usd")) for card in cards)
    fees = sum(_float(card.get("fee_usd")) for card in cards)
    wins = sum(1 for card in cards if _float(card.get("net_usd")) > 0)
    avg_net = _avg(
        [_float(card["captured_bps"]) for card in cards if card.get("captured_bps") is not None]
    )
    avg_mfe = _avg(
        [_float(card["mfe_bps"]) for card in cards if card.get("mfe_bps") is not None]
    )
    avg_giveback = _avg(
        [
            _float(card["giveback_bps"])
            for card in cards
            if card.get("giveback_bps") is not None
        ]
    )
    diagnoses = Counter(str(card.get("trade_diagnosis") or "") for card in cards)
    primary, action, blockers = _diagnose_lane(
        cards=cards,
        entry=entry,
        exit_=exit_,
        config=config,
    )
    return {
        "lane_id": lane_id,
        "exchange": entry.get("exchange") or exit_.get("exchange") or _first(cards, "exchange"),
        "symbol": entry.get("symbol") or exit_.get("symbol") or _first(cards, "symbol"),
        "timeframe": entry.get("timeframe") or exit_.get("timeframe"),
        "strategy_id": entry.get("strategy_id") or exit_.get("strategy_id"),
        "mode": entry.get("mode") or exit_.get("mode"),
        "closed_trades": closed,
        "wins": wins,
        "losses": sum(1 for card in cards if _float(card.get("net_usd")) < 0),
        "win_rate": round(wins / closed, 4) if closed else None,
        "net_pnl_usd": round(net, 6),
        "fees_usd": round(fees, 6),
        "avg_net_bps": round(avg_net, 4) if avg_net is not None else None,
        "avg_mfe_bps": round(avg_mfe, 4) if avg_mfe is not None else None,
        "avg_giveback_bps": round(avg_giveback, 4) if avg_giveback is not None else None,
        "entry_state": entry.get("entry_state"),
        "exit_driver": exit_.get("loss_driver"),
        "entry_next_action": entry.get("next_action"),
        "exit_next_action": exit_.get("next_action"),
        "trade_diagnosis_counts": dict(sorted(diagnoses.items())),
        "giveback_trades": diagnoses[DIAG_GIVEBACK_DOMINATED],
        "fee_wall_trades": diagnoses[DIAG_FEE_WALL_DOMINATED],
        "stop_trades": diagnoses[DIAG_STOP_DOMINATED],
        "overnight_hold_trades": diagnoses[DIAG_OVERNIGHT_HOLD_DRIFT],
        "healthy_trades": diagnoses[DIAG_HEALTHY_CAPTURE],
        "primary_diagnosis": primary,
        "next_action": action,
        "blockers": blockers,
        "recent_trades": cards[-5:],
    }


def _diagnose_lane(
    *,
    cards: list[dict[str, Any]],
    entry: Mapping[str, Any],
    exit_: Mapping[str, Any],
    config: TradeAnalyzerOSConfig,
) -> tuple[str, str, list[str]]:
    closed = len(cards)
    if closed <= 0:
        return (
            DIAG_NO_CLOSED_TRADES,
            "collect closed paper trades before diagnosing this lane",
            ["no closed paper fills reconstructed"],
        )
    entry_state = str(entry.get("entry_state") or "")
    if entry_state == "ENTRY_CONTEXT_MISSING":
        return (
            DIAG_ENTRY_CONTEXT_GAP,
            "repair signal-to-order linkage before trusting paper outcomes",
            [str(entry.get("next_action") or "entry context missing")],
        )
    if entry_state in {"ENTRY_SIGNAL_STALE", "ENTRY_DIRECTION_DRIFT"}:
        return (
            DIAG_STALE_OR_DRIFT_ENTRY,
            "fix entry TTL or side mapping before exit tuning",
            [str(entry.get("next_action") or entry_state)],
        )
    counts = Counter(str(card.get("trade_diagnosis") or "") for card in cards)
    if counts[DIAG_GIVEBACK_DOMINATED] / closed >= 0.35:
        return (
            DIAG_GIVEBACK_DOMINATED,
            "test fee-aware breakeven/profit-lock; trades are reaching profit then leaking it",
            [f"{counts[DIAG_GIVEBACK_DOMINATED]}/{closed} trades gave back MFE"],
        )
    if str(exit_.get("loss_driver") or "") == "LEDGER_OR_EXIT_METADATA_GAP":
        return (
            DIAG_EXIT_METADATA_GAP,
            "repair exit reason/MFE journaling before trusting analyzer output",
            [str(exit_.get("next_action") or "exit metadata missing")],
        )
    if counts[DIAG_STOP_DOMINATED] / closed >= 0.45:
        return (
            DIAG_STOP_DOMINATED,
            "tighten setup permission and invalidation; stop exits dominate",
            [f"{counts[DIAG_STOP_DOMINATED]}/{closed} stopped trades"],
        )
    if counts[DIAG_OVERNIGHT_HOLD_DRIFT] / closed >= 0.25:
        return (
            DIAG_OVERNIGHT_HOLD_DRIFT,
            "enforce daily factory close or intraday timeout; trades are overstaying the mandate",
            [f"{counts[DIAG_OVERNIGHT_HOLD_DRIFT]}/{closed} trades exceeded intraday hold"],
        )
    if counts[DIAG_FEE_WALL_DOMINATED] / closed >= 0.35:
        return (
            DIAG_FEE_WALL_DOMINATED,
            "require larger expected move or maker-first execution before more paper risk",
            [f"{counts[DIAG_FEE_WALL_DOMINATED]}/{closed} trades below fee wall"],
        )
    healthy = counts[DIAG_HEALTHY_CAPTURE]
    if (
        closed >= config.min_closed_trades
        and healthy / closed >= 0.5
        and _float(entry.get("profit_factor")) >= config.min_profit_factor
    ):
        return (
            DIAG_HEALTHY_CAPTURE,
            "keep collecting proof; lane has analyzable capture but still needs promotion gates",
            [f"{healthy}/{closed} trades captured enough after costs"],
        )
    if closed < config.min_closed_trades:
        return (
            DIAG_OBSERVE_MORE,
            "sample is thin; keep observing before changing the lane",
            [f"sample {closed} < {config.min_closed_trades}"],
        )
    return (
        DIAG_WEAK_FOLLOW_THROUGH,
        "mine setup context; entries are not producing enough post-fee follow-through",
        ["no dominant mechanical fault isolated"],
    )


def _trade_card(trade: Mapping[str, Any], config: TradeAnalyzerOSConfig) -> dict[str, Any]:
    entry = _float(trade.get("entry_price"))
    exit_price = _float(trade.get("exit_price"))
    captured = trade.get("captured_bps")
    captured_bps = _float(captured) if captured is not None else None
    mfe_bps = _mfe_bps(trade)
    giveback = None
    if mfe_bps is not None and captured_bps is not None:
        giveback = max(0.0, mfe_bps - max(captured_bps, 0.0))
    net = _trade_net(dict(trade))
    fee = _float(trade.get("fee_usd"))
    resolution = str(trade.get("resolution") or trade.get("exit_reason") or "")
    diagnosis = _diagnose_trade(
        net_usd=net,
        captured_bps=captured_bps,
        mfe_bps=mfe_bps,
        giveback_bps=giveback,
        resolution=resolution,
        hold_seconds=_float(trade.get("hold_seconds")),
        config=config,
    )
    return {
        "lane_id": trade.get("lane"),
        "ts": trade.get("ts"),
        "entry_ts": trade.get("entry_ts"),
        "exchange": trade.get("exchange") or trade.get("venue"),
        "symbol": trade.get("symbol"),
        "side": trade.get("side"),
        "entry_price": entry if entry > 0 else None,
        "exit_price": exit_price if exit_price > 0 else None,
        "exit_reason": trade.get("exit_reason"),
        "resolution": resolution,
        "net_usd": round(net, 6),
        "fee_usd": round(fee, 6),
        "captured_bps": round(captured_bps, 4) if captured_bps is not None else None,
        "mfe_bps": round(mfe_bps, 4) if mfe_bps is not None else None,
        "giveback_bps": round(giveback, 4) if giveback is not None else None,
        "hold_seconds": trade.get("hold_seconds"),
        "mfe_price": trade.get("mfe_price"),
        "active_stop_price": trade.get("active_stop_price"),
        "breakeven_armed": trade.get("breakeven_armed"),
        "trade_diagnosis": diagnosis,
        "operator_note": _trade_note(diagnosis),
    }


def _diagnose_trade(
    *,
    net_usd: float,
    captured_bps: float | None,
    mfe_bps: float | None,
    giveback_bps: float | None,
    resolution: str,
    hold_seconds: float,
    config: TradeAnalyzerOSConfig,
) -> str:
    if hold_seconds > config.max_intraday_hold_seconds:
        return DIAG_OVERNIGHT_HOLD_DRIFT
    if (
        mfe_bps is not None
        and giveback_bps is not None
        and mfe_bps >= config.giveback_arm_bps
        and giveback_bps >= config.giveback_min_bps
        and (net_usd <= 0 or (captured_bps is not None and captured_bps < config.fee_wall_bps))
    ):
        return DIAG_GIVEBACK_DOMINATED
    if resolution in {"stop", "tick_stop", "breakeven_stop"} and net_usd < 0:
        return DIAG_STOP_DOMINATED
    if captured_bps is not None and captured_bps < config.fee_wall_bps:
        return DIAG_FEE_WALL_DOMINATED
    if net_usd > 0 and captured_bps is not None and captured_bps >= config.min_avg_net_bps:
        return DIAG_HEALTHY_CAPTURE
    return DIAG_WEAK_FOLLOW_THROUGH


def _trade_note(diagnosis: str) -> str:
    return {
        DIAG_GIVEBACK_DOMINATED: "profit was available; breakeven/profit-lock should be tested",
        DIAG_STOP_DOMINATED: "invalidated before follow-through; entry filter or stop placement needs work",
        DIAG_FEE_WALL_DOMINATED: "move did not pay execution cost",
        DIAG_OVERNIGHT_HOLD_DRIFT: "trade overstayed the daily close mandate",
        DIAG_HEALTHY_CAPTURE: "captured a post-fee move",
        DIAG_WEAK_FOLLOW_THROUGH: "no strong follow-through after entry",
    }.get(diagnosis, "observe")


def _mfe_bps(trade: Mapping[str, Any]) -> float | None:
    entry = _float(trade.get("entry_price"))
    mfe = _float(trade.get("mfe_price"))
    if entry <= 0 or mfe <= 0:
        return None
    side = str(trade.get("side") or "").lower()
    direction = 1.0 if side in {"long", "buy"} else -1.0 if side in {"short", "sell"} else 0.0
    if direction == 0.0:
        return None
    return (mfe / entry - 1.0) * direction * 1e4


def _recent_trade_cards(
    trades: list[dict[str, Any]], config: TradeAnalyzerOSConfig
) -> list[dict[str, Any]]:
    cards = [_trade_card(trade, config) for trade in trades]
    cards.sort(key=lambda row: str(row.get("ts") or ""), reverse=True)
    return cards[: max(1, int(config.max_trade_cards))]


def _summary(rows: list[dict[str, Any]], trade_cards: list[dict[str, Any]]) -> dict[str, Any]:
    lane_diags = Counter(str(row.get("primary_diagnosis") or "") for row in rows)
    trade_diags = Counter(str(row.get("trade_diagnosis") or "") for row in trade_cards)
    closed = sum(int(row.get("closed_trades") or 0) for row in rows)
    net = sum(_float(row.get("net_pnl_usd")) for row in rows)
    fees = sum(_float(row.get("fees_usd")) for row in rows)
    return {
        "total_lanes": len(rows),
        "lanes_with_closed_trades": sum(1 for row in rows if int(row.get("closed_trades") or 0) > 0),
        "closed_trades": closed,
        "negative_lanes": sum(1 for row in rows if _float(row.get("net_pnl_usd")) < 0),
        "positive_lanes": sum(1 for row in rows if _float(row.get("net_pnl_usd")) > 0),
        "net_pnl_usd": round(net, 6),
        "fees_usd": round(fees, 6),
        "giveback_dominated": lane_diags[DIAG_GIVEBACK_DOMINATED],
        "entry_context_gap": lane_diags[DIAG_ENTRY_CONTEXT_GAP],
        "stale_or_drift_entry": lane_diags[DIAG_STALE_OR_DRIFT_ENTRY],
        "stop_dominated": lane_diags[DIAG_STOP_DOMINATED],
        "overnight_hold_drift": lane_diags[DIAG_OVERNIGHT_HOLD_DRIFT],
        "fee_wall_dominated": lane_diags[DIAG_FEE_WALL_DOMINATED],
        "healthy_capture": lane_diags[DIAG_HEALTHY_CAPTURE],
        "trade_diagnosis_counts": dict(sorted(trade_diags.items())),
        "lane_diagnosis_counts": dict(sorted(lane_diags.items())),
        "can_trade": False,
        "can_promote": False,
    }


def _operator_answer(summary: Mapping[str, Any]) -> str:
    if int(summary.get("closed_trades") or 0) <= 0:
        return "No closed paper trades are available for trade analysis yet."
    if int(summary.get("entry_context_gap") or 0):
        return "First repair signal-to-order linkage; paper outcomes cannot be trusted without entry context."
    if int(summary.get("stale_or_drift_entry") or 0):
        return "Entry timing/side drift is visible; fix TTL and side mapping before tuning exits."
    if int(summary.get("giveback_dominated") or 0):
        return "Giveback is the highest-value repair: test breakeven/profit-lock before adding more scanners."
    if int(summary.get("stop_dominated") or 0):
        return "Stops dominate; improve setup permission and invalidation before promotion."
    if int(summary.get("overnight_hold_drift") or 0):
        return "Hold-time drift is visible; enforce the daily factory close before judging edge."
    if int(summary.get("fee_wall_dominated") or 0):
        return "Fee wall still dominates; require larger expected move or maker-first routing."
    if int(summary.get("healthy_capture") or 0):
        return "At least one lane is capturing post-fee moves; keep collecting proof through normal gates."
    return "Trades are analyzable, but no lane has a clean dominant repair or promotion-quality edge yet."


def _row_sort_key(row: Mapping[str, Any]) -> tuple[int, float, str]:
    priority = {
        DIAG_ENTRY_CONTEXT_GAP: 0,
        DIAG_STALE_OR_DRIFT_ENTRY: 1,
        DIAG_GIVEBACK_DOMINATED: 2,
        DIAG_STOP_DOMINATED: 3,
        DIAG_OVERNIGHT_HOLD_DRIFT: 4,
        DIAG_FEE_WALL_DOMINATED: 5,
        DIAG_EXIT_METADATA_GAP: 6,
        DIAG_WEAK_FOLLOW_THROUGH: 7,
        DIAG_OBSERVE_MORE: 8,
        DIAG_HEALTHY_CAPTURE: 9,
        DIAG_NO_CLOSED_TRADES: 10,
    }
    diag = str(row.get("primary_diagnosis") or "")
    return (
        priority.get(diag, 9),
        _float(row.get("net_pnl_usd")),
        str(row.get("lane_id") or ""),
    )


def _first(rows: list[Mapping[str, Any]], key: str) -> Any:
    for row in rows:
        if row.get(key):
            return row.get(key)
    return ""


def _avg(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal-dir", type=Path, default=DEFAULT_JOURNAL_DIR)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--feed", type=Path, default=DEFAULT_FEED)
    parser.add_argument("--interval-seconds", type=float, default=0.0)
    parser.add_argument("--max-rows", type=int, default=TradeAnalyzerOSConfig.max_rows)
    parser.add_argument("--min-closed-trades", type=int, default=TradeAnalyzerOSConfig.min_closed_trades)
    parser.add_argument("--min-profit-factor", type=float, default=TradeAnalyzerOSConfig.min_profit_factor)
    parser.add_argument("--min-avg-net-bps", type=float, default=TradeAnalyzerOSConfig.min_avg_net_bps)
    parser.add_argument("--fee-wall-bps", type=float, default=TradeAnalyzerOSConfig.fee_wall_bps)
    parser.add_argument("--giveback-arm-bps", type=float, default=TradeAnalyzerOSConfig.giveback_arm_bps)
    parser.add_argument("--giveback-min-bps", type=float, default=TradeAnalyzerOSConfig.giveback_min_bps)
    parser.add_argument("--print", action="store_true")
    args = parser.parse_args(argv)

    config = TradeAnalyzerOSConfig(
        max_rows=max(1, int(args.max_rows)),
        min_closed_trades=max(1, int(args.min_closed_trades)),
        min_profit_factor=float(args.min_profit_factor),
        min_avg_net_bps=float(args.min_avg_net_bps),
        fee_wall_bps=float(args.fee_wall_bps),
        giveback_arm_bps=float(args.giveback_arm_bps),
        giveback_min_bps=float(args.giveback_min_bps),
    )
    while True:
        payload = build_trade_analyzer_os(journal_dir=args.journal_dir, config=config)
        publish_trade_analyzer_os(payload, args.out, args.feed)
        if args.print:
            print(render_report(payload))
        if args.interval_seconds <= 0:
            return 0
        time.sleep(float(args.interval_seconds))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
