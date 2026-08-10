"""State snapshot — the single, self-contained object the dashboard shows.

Built by the bot side and handed to the UI server as a plain dict. Every
snapshot is complete (docs/DESIGN.md §6): reconnecting clients never need
history, and a burst of market activity can never firehose the browser —
the UI only ever sees the latest coalesced state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from vnedge.execution.journal import DecisionJournal
from vnedge.execution.order_manager import OrderManager
from vnedge.execution.order_state import OrderState
from vnedge.paper.simulated_exchange import SimulatedExchange
from vnedge.risk.kill_switch import KillSwitch
from vnedge.runtime.portfolio_tracker import PortfolioTracker


@dataclass(frozen=True)
class FeedHealth:
    exchange: str
    candles: str = "ok"
    funding: str = "ok"
    open_interest: str = "ok"
    last_update_ms: float = 0.0


def _risk_status(
    kill: KillSwitch, journal: DecisionJournal, om: OrderManager
) -> str:
    if kill.is_active:
        return "kill_switch_active"
    if not journal.available:
        return "journal_unavailable"
    if om.has_unresolved_orders:
        return "reconciling"
    return "ok"


def _last_risk_reject(om: OrderManager) -> str | None:
    last = None
    for order in om.orders.values():  # insertion-ordered
        if order.state is OrderState.RISK_REJECTED and order.history:
            last = order.history[-1].note
    return last


def build_snapshot(
    *,
    mode: str,
    live_trading_enabled: bool,
    tracker: PortfolioTracker,
    exchange: SimulatedExchange,
    kill_switch: KillSwitch,
    journal: DecisionJournal,
    order_manager: OrderManager,
    feed_health: FeedHealth,
    symbol: str = "",
    strategy_id: str = "",
    recent_alerts: list[dict] | None = None,
    quote: tuple[float, float] | None = None,
    funding_rate: float = 0.0,
    session_stats: dict | None = None,
    trial: dict | None = None,
) -> dict:
    account = tracker.account_state()
    now = datetime.now(UTC)
    positions = []
    for pos in exchange.get_positions():
        # A just-resumed session can hold a restored position BEFORE the feed
        # publishes its first quote; mark at entry until real data arrives
        # (a raw lookup here killed both position-holding lanes on 2026-07-07).
        quote = exchange.quotes.get(pos.symbol)
        mark = (quote[0] + quote[1]) / 2.0 if quote else pos.entry_price
        notional = abs(pos.quantity) * mark
        positions.append(
            {
                "symbol": pos.symbol,
                "side": pos.side,
                "quantity": abs(pos.quantity),
                "entry_price": pos.entry_price,
                "mark_price": mark,
                "notional_usd": notional,
                "unrealized_usd": pos.quantity * (mark - pos.entry_price),
            }
        )
    venue_orders = {s.client_order_id: s for s in exchange.get_open_orders()}
    managed_orders = {
        coid: order for coid, order in order_manager.orders.items()
        if coid in venue_orders or order.is_unresolved
    }
    open_order_ids = list(venue_orders)
    open_order_ids.extend(coid for coid in managed_orders if coid not in venue_orders)
    open_orders = []
    for coid in open_order_ids:
        status = venue_orders.get(coid)
        managed = managed_orders.get(coid)
        intent = managed.intent if managed is not None else None
        last_event = managed.history[-1] if managed is not None and managed.history else None
        state = managed.state.value if managed is not None else status.state
        open_orders.append({
            "client_order_id": coid,
            "exchange_order_id": (
                managed.exchange_order_id if managed is not None and managed.exchange_order_id
                else status.exchange_order_id if status is not None else ""
            ),
            "state": state,
            "side": intent.side if intent is not None else "",
            "order_type": intent.order_type if intent is not None else "",
            "limit_price": intent.limit_price if intent is not None else None,
            "reduce_only": intent.reduce_only if intent is not None else False,
            "requested_qty": (
                status.requested_qty if status is not None else intent.quantity if intent is not None else 0.0
            ),
            "filled_qty": status.filled_qty if status is not None else 0.0,
            "avg_fill_price": status.avg_fill_price if status is not None else 0.0,
            "state_age_ms": (
                (now - last_event.timestamp).total_seconds() * 1000.0
                if last_event is not None else None
            ),
            "last_note": last_event.note if last_event is not None else "",
            "reason": status.reason if status is not None else "",
        })
    recent_fills = [
        {
            "seq": fill.seq,
            "client_order_id": fill.client_order_id,
            "symbol": fill.symbol,
            "side": "buy" if fill.buy else "sell",
            "quantity": fill.quantity,
            "price": fill.price,
            "notional_usd": abs(fill.quantity * fill.price),
            "fee_usd": fill.fee_usd,
            "realized_pnl_usd": fill.realized_pnl_usd,
        }
        for fill in exchange.get_fills()[-12:]
    ]
    return {
        "ts": now.isoformat(),
        "mode": mode,
        "symbol": symbol,
        "strategy_id": strategy_id,
        "recent_alerts": recent_alerts or [],
        "price": (
            {
                "bid": quote[0],
                "ask": quote[1],
                "mid": (quote[0] + quote[1]) / 2.0,
                # guard against a zero/degenerate quote (warmup, missing book):
                # dividing by a zero mid yields inf, and a single inf makes the
                # whole /state payload fail JSON serialization (500).
                "spread_bps": (
                    (quote[1] - quote[0]) / ((quote[0] + quote[1]) / 2.0) * 10_000.0
                    if (quote[0] + quote[1]) > 0 else 0.0
                ),
            }
            if quote is not None else None
        ),
        "funding_rate": funding_rate,
        "session": session_stats or {},
        "trial": trial,
        "live_trading_enabled": live_trading_enabled,
        "kill_switch_active": kill_switch.is_active,
        "equity": tracker.equity_usd(),
        "peak_equity": tracker.peak_equity_usd,
        "realized_pnl": exchange.get_balances()["USDT"] - tracker.starting_equity_usd,
        "unrealized_pnl": tracker.unrealized_pnl_usd(),
        "daily_pnl": account.daily_pnl_usd,
        "consecutive_losses": account.consecutive_losses,
        "risk_status": _risk_status(kill_switch, journal, order_manager),
        "feed_health": {
            "exchange": feed_health.exchange,
            "candles": feed_health.candles,
            "funding": feed_health.funding,
            "open_interest": feed_health.open_interest,
            "last_update_ms": feed_health.last_update_ms,
        },
        "positions": positions,
        "open_orders": open_orders,
        "recent_fills": recent_fills,
        "fills": len(exchange.get_fills()),
        "fees_usd": sum(f.fee_usd for f in exchange.get_fills()),
        "last_risk_reject": _last_risk_reject(order_manager),
        "last_journal_write": "ok" if journal.available else "unavailable",
    }
