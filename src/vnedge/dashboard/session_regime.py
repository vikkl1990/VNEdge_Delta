"""Session-regime performance: which time-of-day window each strategy earns in.

Buckets every closed trade by the UTC trading session its ENTRY falls in and
rolls up per-session and per-(strategy x session) performance — trades, win
rate, net $, worst stretch (peak-to-trough of cumulative net), and the
break-even cushion. This is the "magic hour" view: it answers *when* a strategy
makes money, not just whether it does.

Read-only projection over the same append-only journals the trade-journal view
uses (so the active-lane filter and the shadow/paper net definitions match).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .trade_journal import (
    TradeJournalConfig,
    build_trade_journal,
    _hold_seconds,
    _lane_exchange,
    _parse_dt,
    _trade_net,
)

# UTC session bands. Crypto trades 24/7, so "sessions" are the global market
# windows rather than an exchange's open hours. Non-overlapping, cover the full
# day, funding-boundary aware (00/08/16 UTC). Tunable in ONE place.
SESSIONS: tuple[tuple[str, str, int, int], ...] = (
    ("asia", "Asia · 00–08 UTC", 0, 8),
    ("europe", "Europe · 08–13 UTC", 8, 13),
    ("us", "US · 13–20 UTC", 13, 20),
    ("late", "Late · 20–24 UTC", 20, 24),
)
_SESSION_LABEL = {key: label for key, label, _s, _e in SESSIONS}


def _session_key(dt: datetime) -> str:
    hour = dt.hour
    for key, _label, start, end in SESSIONS:
        if start <= hour < end:
            return key
    return SESSIONS[0][0]


def _lane_strategy(lane_id: str) -> str:
    """Strategy id = the lane prefix before its ccxt exchange token."""
    lane = str(lane_id or "")
    exchange = _lane_exchange(lane_id)
    if exchange and f"_{exchange}_" in lane:
        return lane.split(f"_{exchange}_", 1)[0]
    return lane


def _entry_dt(trade: dict[str, Any]) -> datetime | None:
    """Entry time = exit time minus how long the trade was held."""
    exit_dt = _parse_dt(trade.get("ts"))
    if exit_dt is None:
        return None
    hold = _hold_seconds(trade) or 0.0
    return exit_dt - timedelta(seconds=hold)


def _worst_stretch(nets: list[float]) -> float:
    """Most-negative peak-to-trough of cumulative net (a drawdown in $), over
    trades in chronological order. 0.0 when the curve never dips below its peak."""
    peak = 0.0
    cum = 0.0
    worst = 0.0
    for net in nets:
        cum += net
        peak = max(peak, cum)
        worst = min(worst, cum - peak)
    return worst


def _breakeven_cushion(wins: list[float], losses: list[float]) -> float | None:
    """Points by which the win rate clears the break-even win rate implied by the
    payoff (avg_win vs avg_loss). Positive = a real edge over break-even."""
    n = len(wins) + len(losses)
    if n == 0:
        return None
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(-x for x in losses) / len(losses) if losses else 0.0
    denom = avg_win + avg_loss
    if denom <= 0:
        return None
    be_win_pct = avg_loss / denom * 100.0
    win_pct = len(wins) / n * 100.0
    return round(win_pct - be_win_pct, 1)


def _bucket_stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Roll a chronologically-ordered trade list into one cell of stats."""
    nets = [_trade_net(t) for t in trades]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    n = len(nets)
    net = sum(nets)
    return {
        "trades": n,
        "wins": len(wins),
        "win_rate_pct": round(len(wins) / n * 100, 1) if n else 0.0,
        "net_usd": round(net, 4),
        "avg_usd": round(net / n, 4) if n else 0.0,
        "worst_stretch_usd": round(_worst_stretch(nets), 4),
        "cushion_pts": _breakeven_cushion(wins, losses),
    }


def build_session_regime(
    *,
    snapshot: dict | None,
    journal_dir: Path | str | None,
    lane: str = "",
    since: str | None = None,
    limit: int = 4000,
    tail_bytes: int = 24_000_000,
) -> dict[str, Any]:
    """Session-regime rollup over closed trades (shadow virtual + paper actual).

    ``lane`` filters to one lane; empty = fleet view (active lanes only, so
    retired ledgers never leak — inherited from build_trade_journal).

    ``tail_bytes`` is larger than the trade-journal default because these
    ledgers are dominated by per-bar eval/heartbeat records; a bigger tail
    captures a wider recent window of the (rare) closed-trade records. It is
    still bounded — this is a recent-window view, not an all-time backfill.
    """
    limit = max(1, min(int(limit), 20000))
    journal = build_trade_journal(
        snapshot=snapshot,
        journal_dir=journal_dir,
        lane=lane,
        since=since,
        limit=limit,
        config=TradeJournalConfig(max_rows=limit, tail_bytes=tail_bytes),
    )
    closed = journal.get("closed_trades", [])

    # (strategy, session) -> chronological trade list
    cells: dict[tuple[str, str], list[tuple[datetime, dict[str, Any]]]] = {}
    per_session: dict[str, list[tuple[datetime, dict[str, Any]]]] = {}
    strategies: set[str] = set()
    undated = 0
    for trade in closed:
        entry_dt = _entry_dt(trade)
        if entry_dt is None:
            undated += 1
            continue
        session = _session_key(entry_dt)
        strategy = _lane_strategy(str(trade.get("lane") or ""))
        strategies.add(strategy)
        cells.setdefault((strategy, session), []).append((entry_dt, trade))
        per_session.setdefault(session, []).append((entry_dt, trade))

    def _ordered(rows: list[tuple[datetime, dict[str, Any]]]) -> list[dict[str, Any]]:
        return [t for _dt, t in sorted(rows, key=lambda x: x[0])]

    session_keys = [key for key, _l, _s, _e in SESSIONS]

    by_session = []
    for key in session_keys:
        stats = _bucket_stats(_ordered(per_session.get(key, [])))
        # winner: strategy with the highest net in this session
        contenders = [
            (strat, sum(_trade_net(t) for _d, t in cells.get((strat, key), [])))
            for strat in strategies
            if cells.get((strat, key))
        ]
        winner = max(contenders, key=lambda x: x[1])[0] if contenders else None
        by_session.append({"session": key, "label": _SESSION_LABEL[key],
                            "winner": winner, **stats})

    matrix = []
    for strat in sorted(strategies):
        all_rows = _ordered(
            [row for key in session_keys for row in cells.get((strat, key), [])]
        )
        row = {
            "strategy": strat,
            "total": _bucket_stats(all_rows),
            "sessions": {
                key: _bucket_stats(_ordered(cells.get((strat, key), [])))
                for key in session_keys
            },
        }
        matrix.append(row)
    # busiest / most-profitable strategies first
    matrix.sort(key=lambda r: (-r["total"]["trades"], -r["total"]["net_usd"]))

    overall = _bucket_stats(_ordered([row for rows in per_session.values() for row in rows]))
    dated = overall["trades"]
    best = max(by_session, key=lambda s: s["net_usd"]) if dated else None
    worst = min(by_session, key=lambda s: s["net_usd"]) if dated else None

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "lane": lane or "all",
        "sessions": [
            {"key": k, "label": l, "start": s, "end": e} for k, l, s, e in SESSIONS
        ],
        "overall": overall,
        "by_session": by_session,
        "matrix": matrix,
        "best_session": {"session": best["session"], "label": best["label"],
                         "net_usd": best["net_usd"]} if best else None,
        "worst_session": {"session": worst["session"], "label": worst["label"],
                          "net_usd": worst["net_usd"]} if worst else None,
        "summary": {
            "closed_trades": len(closed),
            "dated_trades": dated,
            "undated_trades": undated,
            "strategies": len(strategies),
            "active_lanes": journal.get("summary", {}).get("active_lanes"),
        },
        "policy": {"read_only": True, "can_trade": False, "can_promote": False,
                   "source": "closed trades bucketed by UTC entry session"},
        "can_trade": False,
        "can_promote": False,
    }
