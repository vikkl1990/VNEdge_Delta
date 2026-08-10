"""Live paper session — real-time market data, simulated execution.

The mode ladder's "paper" rung with LIVE data instead of replay: closed
candles arrive from the websocket feed, strategy decisions happen at bar
close, and orders execute immediately at the live quote (which IS the next
bar's start) — the same discipline the backtester and replay runner enforce.

The order path is IDENTICAL to replay and (later) live: strategy → risk
gateway → journal → OrderManager → broker. This module owns only the loop
around it. Staleness is real here: the gateway evaluates against wall-clock
`now` vs the feed's last websocket event, so a stalled stream blocks entries
naturally.

Incremental data-quality gate at the boundary: candles must arrive strictly
forward in time; anything else is dropped and counted, never processed.

Stops additionally get TICK granularity: between bar closes the idle loop
checks the live top-of-book against the open plan's stop and exits
reduce-only on breach, through the exact same gateway/journal/OrderManager
pipeline as bar-close exits. Take-profits deliberately remain bar-close —
a stop is capital protection (delay is unbounded downside), a TP is
strategy semantics that research models at bar granularity.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

import pandas as pd

from vnedge.dashboard.state_snapshot import FeedHealth, build_snapshot
from vnedge.execution.idempotency import make_intent_key
from vnedge.execution.journal import DecisionJournal
from vnedge.execution.order_manager import OrderManager
from vnedge.execution.order_state import OrderState
from vnedge.paper.paper_reconciliation import PaperReconciler
from vnedge.paper.simulated_exchange import SimulatedExchange
from vnedge.risk.position_sizer import size_position
from vnedge.risk.protections import ProtectionState
from vnedge.risk.risk_manager import OrderIntent, PreTradeRiskGateway
from vnedge.runtime.portfolio_tracker import PortfolioTracker
from vnedge.runtime.active_exit import ActiveExitDecision, ActiveExitState
from vnedge.runtime.daily_factory import (
    entry_block_reason,
    session_day,
    should_force_flatten,
)
from vnedge.runtime.run_report import RunReport
from vnedge.runtime.runner_config import RunnerConfig, RunnerMode
from vnedge.runtime.shadow_outcomes import (
    ShadowOutcomeTracker,
    VirtualOutcome,
    is_maker_route_strategy,
)
from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent, StrategyExitIntent
from vnedge.strategy.indicators import atr as _atr_indicator

logger = logging.getLogger(__name__)

_EXIT_ACCEPTED_STATES = frozenset(
    {OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED, OrderState.FILLED}
)
_EXIT_RETRYABLE_STATES = frozenset(
    {OrderState.RISK_REJECTED, OrderState.REJECTED, OrderState.CANCELLED}
)


def _extract_strategy_thresholds(
    strategy: BaseStrategy, threshold_names: tuple[str, ...]
) -> dict[str, float]:
    """Read scanner thresholds from either the strategy or its frozen params."""
    params = getattr(strategy, "params", None)
    thresholds: dict[str, float] = {}
    for attr in threshold_names:
        val = getattr(strategy, attr, None)
        if not _is_numeric(val) and params is not None:
            val = getattr(params, attr, None)
        if _is_numeric(val):
            thresholds[attr] = float(val)
    return thresholds


def _is_numeric(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _signal_payload(sig: SignalIntent | None) -> dict | None:
    if sig is None:
        return None
    return {
        "side": sig.side,
        "stop_price": sig.stop_price,
        "take_profit_price": sig.take_profit_price,
        "take_profit_levels": list(sig.take_profit_levels),
        "reason": sig.reason,
    }


@dataclass
class _LivePlan:
    signal: SignalIntent
    entry_bar_ts: pd.Timestamp
    exit_state: ActiveExitState | None = None

    def __post_init__(self) -> None:
        if self.exit_state is None:
            self.exit_state = ActiveExitState.from_signal(self.signal)


@dataclass
class _PendingMakerEntry:
    """A maker-routed entry resting as a limit, not yet filled.

    Maker-edge strategies post a passive limit instead of crossing the spread;
    it becomes a position only when a later quote touches it (a real maker fill),
    and is cancelled — the trade skipped — if no touch lands within its TTL. The
    immediate runners a maker would miss are missed here too (adverse selection),
    mirroring the shadow-lane discipline.
    """

    plan: _LivePlan
    client_order_id: str
    bars_waited: int = 0


class LivePaperSession:
    def __init__(
        self,
        strategy: BaseStrategy,
        feed,  # LiveMarketFeed or a fake with the same surface
        history: pd.DataFrame,  # warmup seed, gate-validated canonical candles
        config: RunnerConfig,
        *,
        gateway: PreTradeRiskGateway,
        order_manager: OrderManager,
        exchange: SimulatedExchange,
        journal: DecisionJournal,
        snapshot_provider=None,  # optional dashboard hookup
        account_store=None,  # optional PaperAccountStore for crash/restart resume
        alert_engine=None,  # optional AlertEngine — same snapshot, guarded fanout
        equity_history_path=None,  # optional JSONL of (ts, equity) per bar
        trial_meta=None,  # optional dict shown on the dashboard governance panel
        fill_ledger=None,  # optional FillLedger — hash-chained execution record
        funnel_store=None,  # optional LaneFunnelStore — resume counters on restart
    ) -> None:
        self.strategy = strategy
        self.feed = feed
        self.candles = history.reset_index(drop=True)
        self.config = config
        self.gateway = gateway
        self.om = order_manager
        self.exchange = exchange
        self.journal = journal
        self.provider = snapshot_provider
        self.account_store = account_store
        self.alert_engine = alert_engine
        self.equity_history_path = equity_history_path
        self.fill_ledger = fill_ledger
        self.funnel_store = funnel_store
        # when this lane last fired a LIVE signal — lets the dashboard show
        # "last fired 2d ago" so a slow, quiet lane reads as waiting, not dead
        self.last_fired_ts: str | None = None
        # baseline against the EXCHANGE's fill list (resets each session), not
        # the ledger's total record count (survives restarts) — else every
        # post-restart fill would be sliced away and never chained/logged
        self._ledgered_fills = len(exchange.get_fills())
        self.trial_meta = trial_meta
        self.bars_processed = 0
        self._started_at = datetime.now(UTC)
        self.tracker = PortfolioTracker(exchange, config.starting_equity_usd)
        self.reconciler = PaperReconciler(order_manager, exchange)
        self.signals = self.orders_submitted = self.risk_rejects = 0
        self.sizing_skips = self.dropped_candles = self.recon_mismatches = 0
        self.shadow_approved = self.shadow_rejected = 0
        self.evals = self.live_evals = self.backfill_evals = 0
        self.live_signals = self.backfill_signals = 0
        self.tick_stop_exits = 0
        self._last_heartbeat_at: datetime | None = None
        self._shadow_exit_df: pd.DataFrame | None = None
        # SHADOW lanes never fill, so per-lane edge is invisible without
        # virtual resolution: approved intents are resolved forward on
        # closed bars with backtester semantics (journal = durable store).
        self.shadow_outcomes: ShadowOutcomeTracker | None = (
            ShadowOutcomeTracker(
                journal,
                fill_model=exchange.fill_model,
                max_holding_bars=config.max_holding_bars,
                # Strategies whose edge is defined after MAKER fees get the
                # resting-limit route (touch-to-fill + maker entry fee); every
                # other lane stays all-taker. Observability only either way.
                maker_route=is_maker_route_strategy(strategy.strategy_id),
                # Same ATR-chandelier trail as the paper/live ActiveExitState, so
                # a shadow lane predicts its paper twin instead of the legacy
                # fixed-stop exit. Fed the identical _trail_atr() per bar below.
                trail_atr_mult=config.trail_atr_mult,
                strategy_exit=self._shadow_strategy_exit,
            )
            if config.mode is RunnerMode.SHADOW
            else None
        )
        self.last_eval: dict | None = None
        # chronological trade narrative for the dashboard journal panel:
        # fired signals, gateway verdicts, submissions, fills, exits
        from collections import deque
        self.trade_log: deque = deque(maxlen=40)
        self._plan: _LivePlan | None = None
        # A maker-routed entry resting as a limit (touch-to-fill), if any. It is
        # NOT a position until it fills; a new entry cannot fire while it rests.
        self._pending_entry: _PendingMakerEntry | None = None
        self._parked_entries: dict[str, _LivePlan] = {}
        self._pending_exit_orders: dict[str, str] = {}
        self._pending_exit_finals: dict[str, bool] = {}
        self._pending_exit_decisions: dict[str, ActiveExitDecision] = {}
        self._exit_retry_attempts: dict[str, int] = {}
        self._orphan_position_guarded = False
        self._reconciliation_fail_closed = False
        self._bars_since_reconcile = 0
        # Entry protections (post-stop cooldown + stop-window guard) — the
        # same state machine the backtester runs, fed with candle-frame row
        # indexes. Consulted by the entry path ONLY; exits never touch it.
        self.protections = ProtectionState(config.effective_protections())
        self._protection_block_logged = False  # one trade_log event per episode
        self._report_day = None
        self._day_open_equity = config.starting_equity_usd
        self._day_open_fills = 0
        self._factory_day = session_day(self._started_at, self.config.daily_factory)
        self._factory_entries_today = 0
        self._factory_flatten_sent = False

    # --- Internals ---------------------------------------------------------------
    def _sync_quote(self) -> bool:
        if self.feed.quote is None:
            return False
        bid, ask = self.feed.quote
        self.exchange.set_quote(self.config.symbol, bid, ask)
        return True

    def _append_candle(self, raw_row: list) -> bool:
        """Incremental quality gate: strictly-forward timestamps only."""
        ts = pd.to_datetime(raw_row[0], unit="ms", utc=True)
        if len(self.candles) and ts <= self.candles["timestamp"].iloc[-1]:
            self.dropped_candles += 1
            logger.warning("dropped non-forward candle %s", ts)
            return False
        row = {
            "timestamp": ts,
            "open": float(raw_row[1]), "high": float(raw_row[2]),
            "low": float(raw_row[3]), "close": float(raw_row[4]),
            "volume": float(raw_row[5]),
        }
        self.candles = pd.concat(
            [self.candles, pd.DataFrame([row])], ignore_index=True
        )
        return True

    def _log_trade_event(self, event: str, detail: str, now: datetime) -> None:
        self.trade_log.append({
            "ts": now.isoformat(), "event": event, "detail": detail,
        })

    def _mode_label(self) -> str:
        if self.config.mode is RunnerMode.SHADOW:
            return "shadow (live data)"
        return "paper (live data)"

    _RUNNER_HEARTBEAT_SECONDS = 60.0
    # Bars a maker entry limit rests before it is cancelled (touch-to-fill TTL).
    _MAKER_ENTRY_TTL_BARS = 2

    def _why_no_trade(self, reason: str) -> str:
        if self._plan is not None:
            return "position_open: managing exit plan"
        if self.feed.quote is None:
            return "waiting_for_quote"
        if len(self.candles) <= self.strategy.warmup_bars:
            return "warming_up: not enough closed candles"
        if self.last_eval:
            skip = self.last_eval.get("skip_reason")
            if skip:
                return str(skip)
            if self.last_eval.get("fired"):
                if self.sizing_skips > 0 and self.orders_submitted == 0:
                    return "last_signal_rejected_by_sizing"
                if self.risk_rejects > 0 and self.orders_submitted == 0:
                    return "last_signal_rejected_by_gateway"
                return "last_eval_fired: awaiting order/fill/reconciliation state"
            return "last_eval_no_signal"
        return reason

    def _record_runner_heartbeat(
        self, reason: str, now: datetime, *, force: bool = False
    ) -> None:
        if (
            not force
            and self._last_heartbeat_at is not None
            and (now - self._last_heartbeat_at).total_seconds() < self._RUNNER_HEARTBEAT_SECONDS
        ):
            return
        self._last_heartbeat_at = now
        last_bar_ts = None
        if len(self.candles):
            last_bar_ts = self.candles["timestamp"].iloc[-1].isoformat()
        self.journal.append("paper_lane_heartbeat", {
            "reason": reason,
            "why_no_trade": self._why_no_trade(reason),
            "started_at": self._started_at.isoformat(),
            "strategy_id": self.strategy.strategy_id,
            "exchange": getattr(self.feed, "exchange_id", ""),
            "symbol": self.config.symbol,
            "timeframe": self.config.timeframe,
            "mode": self.config.mode.value,
            "runner_state": "in_position" if self._plan is not None else "waiting",
            "bars_processed": self.bars_processed,
            "evals": self.evals,
            "live_evals": self.live_evals,
            "backfill_evals": self.backfill_evals,
            "signals": self.signals,
            "live_signals": self.live_signals,
            "backfill_signals": self.backfill_signals,
            "orders_submitted": self.orders_submitted,
            "risk_rejects": self.risk_rejects,
            "sizing_skips": self.sizing_skips,
            "shadow_approved": self.shadow_approved,
            "shadow_rejected": self.shadow_rejected,
            "recon_mismatches": self.recon_mismatches,
            "dropped_candles": self.dropped_candles,
            "quote_seen": self.feed.quote is not None,
            "feed_staleness_seconds": float(self.feed.staleness_seconds()),
            "last_bar_ts": last_bar_ts,
            "last_eval": self.last_eval,
            "trial_id": (self.trial_meta or {}).get("trial_id"),
            "journal_path": str(self.journal.path),
            "daily_factory": self._daily_factory_payload(now),
        })

    def _roll_daily_factory(self, clock: datetime) -> None:
        if not self.config.daily_factory.enabled:
            return
        day = session_day(clock, self.config.daily_factory)
        if day == self._factory_day:
            return
        self._factory_day = day
        self._factory_entries_today = 0
        self._factory_flatten_sent = False
        self.journal.append("daily_factory_day_started", {
            "day": str(day),
            "timezone": self.config.daily_factory.session_timezone,
            "strategy_id": self.strategy.strategy_id,
            "symbol": self.config.symbol,
            "mode": self.config.mode.value,
        })

    def _daily_factory_payload(self, clock: datetime) -> dict:
        cfg = self.config.daily_factory
        payload = {
            "enabled": cfg.enabled,
            "timezone": cfg.session_timezone,
            "entries_today": self._factory_entries_today,
            "max_entries_per_day": cfg.max_entries_per_day,
            "entry_cutoff_minute": cfg.entry_cutoff_minute,
            "force_flatten_minute": cfg.force_flatten_minute,
            "daily_profit_target_usd": cfg.daily_profit_target_usd,
            "day": str(session_day(clock, cfg)),
            "flatten_sent": self._factory_flatten_sent,
        }
        payload["entry_block_reason"] = self._daily_factory_entry_block_reason(clock)
        payload["force_flatten_due"] = should_force_flatten(clock, cfg)
        return payload

    def _daily_factory_entry_block_reason(self, clock: datetime) -> str | None:
        self._roll_daily_factory(clock)
        return entry_block_reason(
            now=clock,
            config=self.config.daily_factory,
            entries_today=self._factory_entries_today,
            daily_pnl_usd=self.tracker.account_state().daily_pnl_usd,
        )

    def _new_plan(self, sig: SignalIntent, entry_bar_ts) -> _LivePlan:
        return _LivePlan(
            signal=sig,
            entry_bar_ts=pd.Timestamp(entry_bar_ts),
            exit_state=ActiveExitState.from_signal(
                sig, trail_atr_mult=self.config.trail_atr_mult
            ),
        )

    def _trail_atr(self) -> float:
        """Canonical ATR for the trail — the SAME indicator+window the backtester
        uses, so research and runtime trail identically. 0.0 if not warmed."""
        if self.config.trail_atr_mult <= 0.0 or len(self.candles) < 2:
            return 0.0
        try:
            series = _atr_indicator(self.candles, self.config.trail_atr_window)
        except Exception:  # noqa: BLE001 - trailing must never break the exit loop
            return 0.0
        value = float(series.iloc[-1])
        return value if value == value else 0.0  # value==value drops NaN warmup

    def _shadow_strategy_exit(
        self,
        side: str,
        entry_price: float,
        bar: pd.Series,
    ) -> StrategyExitIntent | None:
        df = self._shadow_exit_df
        if df is None or df.empty:
            return None
        try:
            index = int(bar.name)
        except (TypeError, ValueError):
            index = len(df) - 1
        if index < 0 or index >= len(df):
            index = len(df) - 1
        return self.strategy.exit_signal(df, index, side, entry_price)

    def _seed_plan_from_venue(self, plan: _LivePlan, client_order_id: str | None = None) -> None:
        if client_order_id is not None:
            status = self.exchange.get_order_status(client_order_id)
            if status is not None and status.filled_qty > 0:
                plan.exit_state.seed_entry(
                    entry_price=status.avg_fill_price,
                    quantity=status.filled_qty,
                )
                return
        positions = {p.symbol: p for p in self.exchange.get_positions()}
        pos = positions.get(self.config.symbol)
        if pos is not None:
            plan.exit_state.seed_entry(
                entry_price=pos.entry_price,
                quantity=abs(pos.quantity),
            )

    def _current_position_quantity(self) -> float:
        positions = {p.symbol: p for p in self.exchange.get_positions()}
        pos = positions.get(self.config.symbol)
        return abs(pos.quantity) if pos is not None else 0.0

    async def _submit_entry(self, sig: SignalIntent, now: datetime) -> None:
        bid, ask = self.feed.quote
        ref_price = ask if sig.side == "long" else bid
        sizing = size_position(
            equity_usd=self.tracker.equity_usd(), entry_price=ref_price,
            stop_price=sig.stop_price, side=sig.side,
            config=self.config.risk, limits=self.config.limits,
        )
        if not sizing.approved:
            self.sizing_skips += 1
            self._log_trade_event("sizing_skip", f"{sig.side} rejected by sizing: {', '.join(sizing.reasons)}"[:140], now)
            return
        # Maker-edge strategies post a passive resting limit at the near touch
        # (bid for a long, ask for a short) instead of crossing the spread — the
        # route their scorecard edge is defined on. SHADOW never places real
        # orders, so it stays market (its virtual pricing is handled separately).
        maker = (
            self.config.mode is not RunnerMode.SHADOW
            and is_maker_route_strategy(self.strategy.strategy_id)
        )
        intent = OrderIntent(
            symbol=self.config.symbol, side=sig.side, quantity=sizing.quantity,
            notional_usd=sizing.notional_usd,
            leverage=max(sizing.required_leverage, 1.0),
            reduce_only=False, strategy_id=self.strategy.strategy_id,
            order_type="limit" if maker else "market",
            limit_price=(bid if sig.side == "long" else ask) if maker else None,
        )
        decision_bar_ts = self.candles["timestamp"].iloc[-1]
        key = make_intent_key(
            self.strategy.strategy_id, self.config.symbol, sig.side,
            decision_bar_ts,
        )
        if self.config.mode is RunnerMode.SHADOW:
            decision = self.gateway.evaluate(
                intent, self.tracker.account_state(), self.feed.market_state(), now=now
            )
            self.journal.append("shadow_intent", {
                "intent_key": key,
                "approved": decision.approved,
                "failed_checks": list(decision.failed_checks),
                "passed_checks": list(decision.passed_checks),
                "explanation": decision.explanation,
                "intent": asdict(intent),
                "signal_reason": sig.reason,
                # stop/target/decision bar make the intent resolvable into a
                # virtual outcome later (and on restart, from the journal)
                "stop_price": sig.stop_price,
                "take_profit_price": sig.take_profit_price,
                "take_profit_levels": list(sig.take_profit_levels),
                "bar_ts": decision_bar_ts.isoformat(),
            })
            if decision.approved:
                self.shadow_approved += 1
                self._factory_entries_today += 1
                if self.shadow_outcomes is not None:
                    self.shadow_outcomes.track(
                        intent_key=key, side=sig.side,
                        quantity=intent.quantity,
                        notional_usd=intent.notional_usd,
                        stop_price=sig.stop_price,
                        take_profit_price=sig.take_profit_price,
                        decision_bar_ts=decision_bar_ts,
                        signal_reason=sig.reason,
                        take_profit_levels=sig.take_profit_levels,
                    )
                self._log_trade_event(
                    "shadow_approved",
                    f"{sig.side} {intent.quantity:g} @ ~{ref_price:g} — {sig.reason}"[:140],
                    now,
                )
            else:
                self.shadow_rejected += 1
                self._log_trade_event(
                    "shadow_rejected",
                    f"{sig.side} — failed: {', '.join(decision.failed_checks)}"[:140],
                    now,
                )
            return
        order = await self.om.submit(
            intent, self.tracker.account_state(), self.feed.market_state(), key, now=now
        )
        if order.state is OrderState.RISK_REJECTED:
            self.risk_rejects += 1
            self._log_trade_event("risk_rejected", f"{sig.side} — gateway rejected entry"[:140], now)
        else:
            self.orders_submitted += 1
            self._factory_entries_today += 1
            self._log_trade_event(
                "order_submitted",
                f"{sig.side} {intent.quantity:g} ({order.state.value}) — {sig.reason}"[:140],
                now,
            )
            plan = self._new_plan(sig, self.candles["timestamp"].iloc[-1])
            self._seed_plan_from_venue(plan, order.client_order_id)
            venue_status = self.exchange.get_order_status(order.client_order_id)
            filled = venue_status is not None and venue_status.filled_qty > 0
            if maker and order.state is OrderState.ACKNOWLEDGED and not filled:
                # A resting maker limit: NOT a position until a later quote
                # touches it. Park it — a new entry can't fire while it rests.
                self._pending_entry = _PendingMakerEntry(
                    plan=plan, client_order_id=order.client_order_id
                )
            elif order.state is OrderState.ACKNOWLEDGED:
                self._plan = plan
            elif order.state is OrderState.TIMEOUT_UNKNOWN:
                self._parked_entries[order.client_order_id] = plan

    async def _manage_pending_entry(self, now: datetime) -> None:
        """Resolve a resting maker entry: promote to a position once a quote has
        touched it (checked after the bar's quote sync), or cancel it — skipping
        the trade — once its touch-to-fill TTL lapses without a fill."""
        pending = self._pending_entry
        if pending is None:
            return
        status = self.exchange.get_order_status(pending.client_order_id)
        if status is not None and status.filled_qty > 0:
            self._pending_entry = None
            pending.plan.exit_state.seed_entry(
                entry_price=status.avg_fill_price,
                quantity=status.filled_qty,
            )
            self._plan = pending.plan
            self._log_trade_event(
                "maker_entry_filled",
                f"{pending.plan.signal.side} filled @ {status.avg_fill_price:g} (maker)"[:140],
                now,
            )
            return
        pending.bars_waited += 1
        if pending.bars_waited >= self._MAKER_ENTRY_TTL_BARS:
            await self.om.cancel_order(
                pending.client_order_id, reason="maker entry TTL — no touch"
            )
            self._pending_entry = None
            self._log_trade_event(
                "maker_entry_unfilled",
                f"{pending.plan.signal.side} no touch in {pending.bars_waited} bars — skipped"[:140],
                now,
            )

    async def _cancel_pending_entry_for_daily_factory(
        self, clock: datetime, now: datetime
    ) -> None:
        if self._pending_entry is None:
            return
        cfg = self.config.daily_factory
        if not cfg.enabled or not cfg.cancel_resting_entries_at_cutoff:
            return
        if self._daily_factory_entry_block_reason(clock) is None:
            return
        pending = self._pending_entry
        await self.om.cancel_order(
            pending.client_order_id,
            reason="daily factory cutoff — resting entry cancelled",
        )
        self._pending_entry = None
        self.journal.append("daily_factory_pending_entry_cancelled", {
            "client_order_id": pending.client_order_id,
            "strategy_id": self.strategy.strategy_id,
            "symbol": self.config.symbol,
            "clock": clock.isoformat(),
        })
        self._log_trade_event(
            "daily_factory_cancel",
            f"{pending.plan.signal.side} maker entry cancelled at daily cutoff"[:140],
            now,
        )

    async def _enforce_daily_factory_flatten(
        self, clock: datetime, now: datetime
    ) -> None:
        cfg = self.config.daily_factory
        if not should_force_flatten(clock, cfg) or self._factory_flatten_sent:
            return
        await self._cancel_pending_entry_for_daily_factory(clock, now)
        if not self.exchange.get_positions():
            self._factory_flatten_sent = True
            return
        # One deterministic exit key per session day. If a prior submission is
        # unresolved, _submit_exit preserves the plan and avoids double-submit.
        key_ts = int(pd.Timestamp(clock.date()).value)
        pre_plan = self._plan
        pre_state = pre_plan.exit_state if pre_plan is not None else None
        pre_sig = pre_plan.signal if pre_plan is not None else None
        order = await self._submit_exit(
            "daily_factory_close",
            key_ts,
            now,
            quantity=None,
            final=True,
        )
        if order is None:
            return
        if order.state in _EXIT_ACCEPTED_STATES:
            self._factory_flatten_sent = True
        self.journal.append("live_paper_exit", {
            "reason": "daily_factory_close",
            "state": order.state.value,
            "client_order_id": order.client_order_id,
            "take_profit_levels": list(pre_sig.take_profit_levels) if pre_sig else [],
            "tp_number": 0,
            "tp_reached": pre_state.tp_reached() if pre_state is not None else 0,
            "mfe_price": pre_state.mfe_price if pre_state is not None else None,
            "exit_price": None,
            "quantity": None,
            "final": True,
            "active_stop_price": pre_state.current_stop if pre_state is not None else None,
            "breakeven_armed": pre_state.breakeven_armed if pre_state is not None else False,
        })
        self.journal.append("daily_factory_flatten", {
            "state": order.state.value,
            "client_order_id": order.client_order_id,
            "strategy_id": self.strategy.strategy_id,
            "symbol": self.config.symbol,
            "clock": clock.isoformat(),
        })
        self._log_trade_event(
            "daily_factory_close",
            f"force-flat before session close ({order.state.value})"[:140],
            now,
        )

    def _max_holding_hit(self, bar: pd.Series) -> bool:
        """True once the open plan has been held for ``max_holding_bars`` closed
        bars — the SAME time cap the backtester (and shadow) enforce, so a
        paper/live position times out exactly like the config it was judged
        under. Counted statelessly from the persisted ``entry_bar_ts`` against
        the (untrimmed, resume-seeded) candle history, so it survives restarts.
        """
        cap = self.config.max_holding_bars
        if cap <= 0 or self._plan is None:
            return False
        entry_ts = self._plan.entry_bar_ts
        current_ts = pd.Timestamp(bar["timestamp"])
        held = int(
            (
                (self.candles["timestamp"] > entry_ts)
                & (self.candles["timestamp"] <= current_ts)
            ).sum()
        )
        return held >= cap

    async def _manage_exit(self, bar: pd.Series, now: datetime) -> None:
        if self._plan is None:
            return
        sig = self._plan.signal
        decision = self._plan.exit_state.resolve_bar(
            high=float(bar["high"]),
            low=float(bar["low"]),
            close=float(bar["close"]),
            position_quantity=self._current_position_quantity(),
            min_qty=self.config.limits.min_qty,
            qty_step=self.config.limits.qty_step,
            max_holding_hit=self._max_holding_hit(bar),
        )
        if decision is None:
            decision = self._strategy_exit_decision(bar)
        if decision is None:
            # No exit this bar — trail the stop for LATER bars off the canonical
            # ATR (this bar's favorable extreme is already in the exit state; the
            # tightened stop applies next bar, so no intrabar lookahead).
            self._plan.exit_state.trail_stop(self._trail_atr())
            return
        levels = list(sig.take_profit_levels)
        order = await self._submit_exit(
            decision.reason,
            int(bar["timestamp"].value),
            now,
            quantity=decision.quantity,
            final=decision.final,
            decision=decision,
        )
        if order is None:
            return
        if order.state in _EXIT_ACCEPTED_STATES and self._plan is not None:
            self._plan.exit_state.mark_accepted(decision)
        self.journal.append("live_paper_exit", {
            "reason": decision.reason,
            "state": order.state.value,
            "client_order_id": order.client_order_id,
            "take_profit_levels": levels,
            "tp_number": decision.tp_number,
            "tp_reached": decision.tp_reached,
            "mfe_price": decision.mfe_price,
            "exit_price": decision.exit_price,
            "quantity": decision.quantity,
            "final": decision.final,
            "active_stop_price": decision.active_stop_price,
            "breakeven_armed": decision.breakeven_armed,
        })
        self._log_trade_event("exit", f"{decision.reason} ({order.state.value})"[:140], now)

    def _strategy_exit_decision(self, bar: pd.Series) -> ActiveExitDecision | None:
        if self._plan is None:
            return None
        df = self.strategy.prepare(self.candles).reset_index(drop=True)
        if df.empty:
            return None
        index = len(df) - 1
        sig = self._plan.signal
        entry = self._plan.exit_state.entry_price or float(bar["close"])
        exit_sig = self.strategy.exit_signal(df, index, sig.side, entry)
        if exit_sig is None:
            return None
        return ActiveExitDecision(
            reason=exit_sig.reason,
            exit_price=(
                float(exit_sig.exit_price)
                if exit_sig.exit_price is not None
                else float(bar["close"])
            ),
            quantity=None,
            final=True,
            active_stop_price=self._plan.exit_state.current_stop,
            breakeven_armed=self._plan.exit_state.breakeven_armed,
            tp_reached=self._plan.exit_state.tp_reached(),
            mfe_price=self._plan.exit_state.mfe_price,
        )

    async def _submit_exit(
        self,
        reason: str,
        key_ts: int,
        now: datetime,
        *,
        quantity: float | None = None,
        final: bool = True,
        decision: ActiveExitDecision | None = None,
    ):
        """Shared reduce-only exit submission — the ONLY way a plan closes.

        Both the bar-close path (_manage_exit) and the tick-stop path
        (_check_tick_stop) flow through here, so every exit passes the same
        gateway/journal/OrderManager pipeline. Returns the ManagedOrder, or
        None if no position existed or an unresolved prior exit attempt is
        awaiting reconciliation. The plan is cleared only after the exit is
        accepted/filled or the position is confirmed flat."""
        positions = {p.symbol: p for p in self.exchange.get_positions()}
        pos = positions.get(self.config.symbol)
        if pos is None:
            self._clear_exit_plan()
            return None
        close_qty = abs(pos.quantity) if quantity is None else min(abs(pos.quantity), quantity)
        if close_qty <= 0:
            return None
        base_key = f"exit|{self.config.symbol}|{reason}|{key_ts}"
        pending = self._pending_exit_orders.get(base_key)
        if pending is not None:
            pending_order = self.om.orders.get(pending)
            if pending_order is not None and pending_order.state in (
                OrderState.TIMEOUT_UNKNOWN,
                OrderState.RECONCILING,
            ):
                self.journal.append("exit_plan_waiting_reconciliation", {
                    "intent_key": base_key,
                    "client_order_id": pending,
                    "reason": reason,
                    "state": pending_order.state.value,
                })
                return None
            self._pending_exit_orders.pop(base_key, None)
            self._pending_exit_finals.pop(base_key, None)
            self._pending_exit_decisions.pop(base_key, None)
        intent = OrderIntent(
            symbol=self.config.symbol,
            side="short" if pos.quantity > 0 else "long",
            quantity=close_qty, notional_usd=0.0, leverage=1.0,
            reduce_only=True, strategy_id=self.strategy.strategy_id,
        )
        intent_key = self._exit_intent_key(base_key)
        order = await self.om.submit(
            intent, self.tracker.account_state(), self.feed.market_state(),
            intent_key=intent_key,
            now=now,
        )
        self.orders_submitted += 1
        if order.state in _EXIT_ACCEPTED_STATES:
            self._mark_exit_accepted(reason, final=final)
        else:
            self._preserve_exit_plan(
                base_key, order, reason, final=final, decision=decision
            )
        return order

    def _exit_intent_key(self, base_key: str) -> str:
        attempt = self._exit_retry_attempts.get(base_key, 0)
        return base_key if attempt == 0 else f"{base_key}|retry={attempt}"

    def _mark_exit_accepted(self, reason: str, *, final: bool = True) -> None:
        if not final:
            return
        self._clear_exit_plan()
        # A tick stop fires BETWEEN closes, so the exit belongs to the bar
        # currently forming (index len(candles)): a 1-bar cooldown then blocks
        # that bar's entry evaluation, exactly as a bar-close stop blocks its
        # own bar's re-entry.
        exit_bar = len(self.candles) - (0 if reason == "tick_stop" else 1)
        self.protections.on_exit(reason, exit_bar)

    def _clear_exit_plan(self) -> None:
        self._plan = None
        self._pending_exit_orders.clear()
        self._pending_exit_finals.clear()
        self._pending_exit_decisions.clear()
        self._exit_retry_attempts.clear()

    def _preserve_exit_plan(
        self,
        base_key: str,
        order,
        reason: str,
        *,
        final: bool = True,
        decision: ActiveExitDecision | None = None,
    ) -> None:
        if order.state in (OrderState.TIMEOUT_UNKNOWN, OrderState.RECONCILING):
            self._pending_exit_orders[base_key] = order.client_order_id
            self._pending_exit_finals[base_key] = final
            if decision is not None:
                self._pending_exit_decisions[base_key] = decision
        elif order.state in _EXIT_RETRYABLE_STATES:
            self._exit_retry_attempts[base_key] = self._exit_retry_attempts.get(base_key, 0) + 1
        if order.state is OrderState.RISK_REJECTED:
            self.risk_rejects += 1
        self.journal.append("exit_plan_preserved", {
            "intent_key": base_key,
            "client_order_id": order.client_order_id,
            "reason": reason,
            "state": order.state.value,
            "next_retry": self._exit_intent_key(base_key),
        })
        logger.warning(
            "preserving exit plan after %s submit ended %s (%s)",
            reason,
            order.state.value,
            order.client_order_id,
        )

    async def _check_tick_stop(self, now: datetime) -> None:
        """Idle-tick STOP monitoring — capital protection at quote granularity.

        Between bar closes, ONLY the stop is checked against the live
        top-of-book (long: bid <= stop; short: ask >= stop — the side an exit
        would actually fill on, the same trigger convention as
        scalping.tick_stop). Take-profits deliberately stay bar-close: a stop
        is capital protection where every bar of delay is unbounded downside
        (measured 2026-07-06: a short's stop filled at 64,489 vs an intra-bar
        breach much earlier), while a TP is strategy semantics — the
        backtester models TPs at bar granularity, so tick TPs would make
        paper results diverge from research.

        Shadow lanes never hold fills/positions, so no plan is ever armed
        there and this never triggers; the explicit mode guard documents that
        and keeps it true even if a plan were ever armed by mistake.
        """
        if (
            self._plan is None
            or not self.config.tick_stops_enabled
            or self.config.mode is RunnerMode.SHADOW
            or self.feed.quote is None
        ):
            return
        sig = self._plan.signal
        entry_bar_ts = self._plan.entry_bar_ts
        bid, ask = self.feed.quote
        stop_price = self._plan.exit_state.current_stop
        breached = bid <= stop_price if sig.side == "long" else ask >= stop_price
        if not breached:
            return
        self._sync_quote()  # exit must fill at the breach quote, not the last bar's
        # key_ts = entry bar: one tick-stop intent per plan, minted once —
        # never re-derived from the (wall-clock) breach time
        order = await self._submit_exit("tick_stop", int(entry_bar_ts.value), now)
        if order is None:
            return
        if order.state in _EXIT_ACCEPTED_STATES:
            self.tick_stop_exits += 1
        trigger_px = bid if sig.side == "long" else ask
        self.journal.append("tick_stop_exit", {
            "reason": "tick_stop",
            "state": order.state.value,
            "client_order_id": order.client_order_id,
            "side": sig.side,
            "stop_price": stop_price,
            "initial_stop_price": sig.stop_price,
            "take_profit_price": sig.take_profit_price,
            "take_profit_levels": list(sig.take_profit_levels),
            "active_stop_price": stop_price,
            "breakeven_armed": self._plan.exit_state.breakeven_armed if self._plan else True,
            "bid": bid,
            "ask": ask,
            "entry_bar_ts": entry_bar_ts.isoformat(),
            "signal_reason": sig.reason,
        })
        self._log_trade_event(
            "exit",
            f"tick_stop {sig.side} — {'bid' if sig.side == 'long' else 'ask'} "
            f"{trigger_px:g} breached stop {stop_price:g} ({order.state.value})"[:140],
            now,
        )
        # persist immediately — a crash before the next bar must not restore
        # the already-closed position/plan
        self._ledger_new_fills(now)
        if self.account_store is not None:
            self.account_store.save_from(
                self.exchange, self.tracker, plan=self._serialize_plan()
            )

    def _maybe_daily_report(self, now: datetime) -> None:
        """At each UTC day rollover, journal a summary of the finished day
        and push it through the alert notifiers (severity info)."""
        day = now.date()
        if self._report_day is None:
            self._report_day = day
            self._day_open_equity = self.tracker.equity_usd()
            self._day_open_fills = len(self.exchange.get_fills())
            return
        if day == self._report_day:
            return
        equity = self.tracker.equity_usd()
        fills = len(self.exchange.get_fills())
        summary = (
            f"daily report {self._report_day}: equity ${equity:.2f} "
            f"({equity - self._day_open_equity:+.2f}), "
            f"fills {fills - self._day_open_fills} (total {fills}), "
            f"open positions {len(self.exchange.get_positions())}, "
            f"loss streak {self.tracker.consecutive_losses}, "
            f"risk rejects {self.risk_rejects}, recon mismatches {self.recon_mismatches}"
        )
        self.journal.append("daily_report", {"day": str(self._report_day), "summary": summary})
        if self.alert_engine is not None:
            alert = {"ts": now.isoformat(), "rule_id": "daily_report",
                     "severity": "info", "message": summary, "mode": self._mode_label()}
            self.alert_engine.recent.append(alert)
            for notifier in self.alert_engine.notifiers:
                try:
                    notifier.send(alert)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("daily report notifier failed: %s", exc)
        self._report_day = day
        self._day_open_equity = equity
        self._day_open_fills = fills

    def _serialize_plan(self) -> dict | None:
        if self._plan is None:
            return None
        sig = self._plan.signal
        return {
            "side": sig.side,
            "stop_price": sig.stop_price,
            "take_profit_price": sig.take_profit_price,
            "take_profit_levels": list(sig.take_profit_levels),
            "reason": sig.reason,
            "entry_bar_ts": self._plan.entry_bar_ts.isoformat(),
            "active_exit": self._plan.exit_state.to_dict(),
        }

    def restore_plan(self, stored: dict | None) -> None:
        """Re-arm exit management for a restored position.

        Preferred: the exact persisted plan. Legacy snapshots (no plan saved):
        ask the strategy to rebuild one with its own signal() formulas; if it
        cannot, the orphan guard keeps its manual-flatten semantics. Either
        path is journaled — a resumed trade must be as explainable as a fresh
        one."""
        positions = self.exchange.get_positions()
        if not positions or self._plan is not None:
            return
        pos = positions[0]
        if stored is not None and not self._restored_stop_sane(
            pos, stored["side"], float(stored["stop_price"])
        ):
            # corrupted/hand-edited store: refuse the plan; orphan-guard
            # manual-flatten semantics are safer than a bad stop
            self.journal.append("plan_restore_rejected", {
                "reason": "stop fails sanity bounds", "stored": dict(stored),
            })
            logger.warning("restored plan REJECTED (insane stop)")
            return
        if stored is not None:
            sig = SignalIntent(
                stored["side"], stop_price=float(stored["stop_price"]),
                take_profit_price=(float(stored["take_profit_price"])
                                   if stored.get("take_profit_price") is not None else None),
                take_profit_levels=tuple(float(x) for x in stored.get("take_profit_levels") or ()),
                reason=stored.get("reason", "restored plan"),
            )
            self._plan = self._new_plan(sig, pd.Timestamp(stored["entry_bar_ts"]))
            self._plan.exit_state.restore(stored.get("active_exit"))
            self._seed_plan_from_venue(self._plan)
            self.journal.append("plan_restored", dict(stored))
            logger.info("trade plan restored from account store: %s", sig.reason)
            return
        if len(self.candles) <= self.strategy.warmup_bars:
            return
        df = self.strategy.prepare(self.candles)
        sig = self.strategy.synthesize_exit_plan(
            df, len(df) - 1, pos.side, pos.entry_price
        )
        if sig is None:
            return  # orphan guard will handle it (entries halted, manual flatten)
        sig = self._clamp_synthesized_stop(pos, sig)
        self._plan = self._new_plan(sig, df["timestamp"].iloc[-1])
        self._seed_plan_from_venue(self._plan)
        self.journal.append("plan_rebuilt_on_resume", {
            "side": sig.side, "stop_price": sig.stop_price,
            "take_profit_price": sig.take_profit_price,
            "take_profit_levels": list(sig.take_profit_levels),
            "reason": sig.reason,
        })
        logger.info("trade plan REBUILT on resume: %s", sig.reason)

    # A synthesized stop uses CURRENT ATR; after a volatile gap it could sit
    # far wider than the original risk envelope (2026-07-09 audit finding).
    # Cap rebuilt stop distance at this fraction of entry price — well above
    # any normal 1.5-ATR distance, so it binds only in the pathological case.
    _MAX_REBUILT_STOP_PCT = 0.03

    def _restored_stop_sane(self, pos, side: str, stop: float) -> bool:
        if not math.isfinite(stop) or stop <= 0:
            return False
        entry = pos.entry_price
        if side == "long" and not stop < entry:
            return False
        if side == "short" and not stop > entry:
            return False
        return abs(stop - entry) / entry <= 3 * self._MAX_REBUILT_STOP_PCT

    def _clamp_synthesized_stop(self, pos, sig: SignalIntent) -> SignalIntent:
        entry = pos.entry_price
        max_dist = self._MAX_REBUILT_STOP_PCT * entry
        if abs(sig.stop_price - entry) <= max_dist:
            return sig
        clamped = entry - max_dist if sig.side == "long" else entry + max_dist
        self.journal.append("plan_stop_clamped", {
            "side": sig.side, "synthesized_stop": sig.stop_price,
            "clamped_stop": clamped, "entry_price": entry,
        })
        logger.warning("synthesized stop %.6g beyond %.0f%% cap — clamped to %.6g",
                       sig.stop_price, self._MAX_REBUILT_STOP_PCT * 100, clamped)
        return SignalIntent(sig.side, stop_price=clamped,
                            take_profit_price=sig.take_profit_price,
                            take_profit_levels=sig.take_profit_levels,
                            reason=sig.reason + " [stop clamped on rebuild]")

    def _guard_orphaned_position(self) -> None:
        if self._orphan_position_guarded or self._plan is not None \
                or self._pending_entry is not None or self._parked_entries:
            return
        positions = self.exchange.get_positions()
        if not positions:
            return
        self._orphan_position_guarded = True
        reason = (
            "restored paper position without active trade plan — entries halted; "
            "manual reduce-only flatten required"
        )
        self.gateway.kill_switch.activate(reason)
        self.journal.append("orphaned_paper_position", {
            "reason": reason,
            "positions": [
                {"symbol": p.symbol, "side": p.side, "quantity": abs(p.quantity)}
                for p in positions
            ],
        })

    def _fail_closed_on_reconciliation(self, mismatches: tuple[str, ...]) -> None:
        if not mismatches or self._reconciliation_fail_closed:
            return
        self._reconciliation_fail_closed = True
        reason = (
            "reconciliation mismatch — entries halted; reduce-only exits remain allowed"
        )
        self.gateway.kill_switch.activate(reason)
        self.journal.append("reconciliation_fail_closed", {
            "reason": reason,
            "mismatches": list(mismatches),
        })

    # Feature columns worth surfacing per evaluation, when the strategy
    # computes them. Signal proximity ("how close is this lane to firing")
    # is unreadable from a binary fired/not-fired record; these make it visible.
    _EVAL_FEATURES = (
        "funding_pct",
        "close_z",
        "funding_rate",
        "er",
        "atr_pct",
        "long_score",
        "short_score",
        "tqi_long",
        "tqi_short",
        "quality_strength",
        "bull_power",
        "bear_power",
        "bbp",
        "bbp_delta",
        "bbp_hist_z",
        "bbp_hist_slope",
        "stealth_trail",
        "stealth_trend",
        "stealth_distance_atr",
        "rsi",
        "volume_z",
        "mom_persist_long",
        "mom_persist_short",
        "structure_long",
        "structure_short",
        "body_atr",
        "body_percentile",
        "expected_net_edge_bps_long",
        "expected_net_edge_bps_short",
    )
    _EVAL_THRESHOLDS = (
        "extreme_pct",
        "z_entry",
        "breakout_bars",
        "min_score",
        "min_score_delta",
        "min_tqi",
        "min_quality_strength",
        "min_momentum_persistence",
        "min_bbp_atr",
        "min_bbp_z",
        "min_bbp_slope",
        "stealth_trail_atr_mult",
        "min_volume_z",
        "min_body_atr",
        "min_body_percentile",
        "min_expected_net_edge_bps",
        "max_funding_against",
        "min_atr_pct",
        "max_atr_pct",
    )

    def _record_eval(
        self, df: pd.DataFrame, index: int, sig: SignalIntent | None,
        *, backfill: bool = False, skip_reason: str | None = None,
    ) -> None:
        """Journal one strategy evaluation — fired or not — with the feature
        values that drove it. This is the observability record that turns
        'no signal for days' from a mystery into a measurement (how far from
        each threshold every bar actually was)."""
        row = df.iloc[index]
        features = {}
        for col in self._EVAL_FEATURES:
            if col in df.columns:
                val = float(row[col])
                features[col] = None if math.isnan(val) else round(val, 6)
        thresholds = _extract_strategy_thresholds(self.strategy, self._EVAL_THRESHOLDS)
        record = {
            "bar_ts": df["timestamp"].iloc[index].isoformat(),
            "strategy_id": self.strategy.strategy_id,
            "exchange": getattr(self.feed, "exchange_id", ""),
            "symbol": self.config.symbol,
            "timeframe": self.config.timeframe,
            "mode": self.config.mode.value,
            "fired": sig is not None,
            "signal_reason": sig.reason if sig is not None else None,
            "skip_reason": skip_reason,
            "signal": _signal_payload(sig),
            "features": features,
            "thresholds": thresholds,
            "backfill": backfill,
        }
        self.evals += 1
        if backfill:
            self.backfill_evals += 1
            if sig is not None:
                self.backfill_signals += 1
        else:
            self.live_evals += 1
            if sig is not None:
                self.live_signals += 1
                _ts = df["timestamp"].iloc[index]
                self.last_fired_ts = _ts.isoformat() if hasattr(_ts, "isoformat") else str(_ts)
        self.journal.append("lane_eval", record)
        if not backfill:
            self.last_eval = record
        if sig is not None and not backfill:
            from datetime import datetime as _dt
            self._log_trade_event(
                "signal_fired", f"{sig.side} — {sig.reason}"[:140], _dt.now(UTC),
            )
        if sig is not None:
            logger.info(
                "lane eval [%s %s]%s: FIRED %s — %s",
                self.strategy.strategy_id, self.config.symbol,
                " (backfill)" if backfill else "", sig.side, sig.reason,
            )
        elif skip_reason and not backfill:
            from datetime import datetime as _dt
            self._log_trade_event(
                "entry_skipped", skip_reason[:140], _dt.now(UTC),
            )

    def _log_shadow_outcomes(
        self, outcomes: list[VirtualOutcome], now: datetime
    ) -> None:
        for outcome in outcomes:
            self._log_trade_event(
                "shadow_outcome",
                f"virtual {outcome.resolution} {outcome.side} "
                f"{outcome.virtual_net_usd:+.2f} USD after {outcome.bars_held} bars"[:140],
                now,
            )

    def _ledger_new_fills(self, now: datetime) -> None:
        """Append any fills not yet chained. The ledger is resume-aware, so a
        restart continues the chain rather than re-recording old fills."""
        if self.fill_ledger is None:
            return
        fills = self.exchange.get_fills()
        for fill in fills[self._ledgered_fills:]:
            self._log_trade_event(
                "fill",
                f"{'buy' if fill.buy else 'sell'} {fill.quantity:g} @ {fill.price:g} "
                f"fee ${fill.fee_usd:.2f} pnl ${fill.realized_pnl_usd:+.2f}"[:140],
                now,
            )
            self.fill_ledger.append({
                "ts": now.isoformat(),
                "mode": self.config.mode.value,
                "venue": getattr(self.feed, "exchange_id", "paper"),
                "strategy_id": self.strategy.strategy_id,
                "symbol": fill.symbol,
                "side": "buy" if fill.buy else "sell",
                "quantity": fill.quantity,
                "price": fill.price,
                "fee_usd": fill.fee_usd,
                "realized_pnl_usd": fill.realized_pnl_usd,
                "client_order_id": fill.client_order_id,
                "exchange_seq": fill.seq,
            })
        self._ledgered_fills = len(fills)

    def _publish_snapshot(self) -> None:
        if self.provider is None and self.alert_engine is None:
            return
        snapshot = build_snapshot(
            mode=self._mode_label(), live_trading_enabled=False,
            tracker=self.tracker, exchange=self.exchange,
            kill_switch=self.gateway.kill_switch, journal=self.journal,
            order_manager=self.om,
            feed_health=FeedHealth(
                exchange=(
                    f"{getattr(self.feed, 'exchange_id', 'feed')} "
                    f"({getattr(self.feed, 'feed_mode', 'live feed')})"
                ),
                candles="ok" if self.feed.staleness_seconds() < 120 else "stale",
                last_update_ms=self.feed.staleness_seconds() * 1000.0,
            ),
            symbol=self.config.symbol,
            strategy_id=self.strategy.strategy_id,
            recent_alerts=list(self.alert_engine.recent)
            if self.alert_engine is not None else [],
            quote=self.feed.quote,
            funding_rate=getattr(self.feed, "funding_rate", 0.0),
            session_stats={
                "started_at": self._started_at.isoformat(),
                "bars_processed": self.bars_processed,
                "evals": self.evals,
                "live_evals": self.live_evals,
                "backfill_evals": self.backfill_evals,
                "signals": self.signals,
                "live_signals": self.live_signals,
                "backfill_signals": self.backfill_signals,
                "orders_submitted": self.orders_submitted,
                "risk_rejects": self.risk_rejects,
                "sizing_skips": self.sizing_skips,
                "tick_stop_exits": self.tick_stop_exits,
                "shadow_approved": self.shadow_approved,
                "shadow_rejected": self.shadow_rejected,
                "recon_mismatches": self.recon_mismatches,
                "dropped_candles": self.dropped_candles,
                "timeframe": self.config.timeframe,
                "why_no_trade": self._why_no_trade("waiting_for_closed_candle"),
                "last_fired_ts": self.last_fired_ts,
                "last_eval": self.last_eval,
                "shadow_perf": self.shadow_outcomes.stats()
                if self.shadow_outcomes is not None else None,
                "trade_log": list(self.trade_log),
                "fill_ledger": {
                    "records": self.fill_ledger.records,
                    "chained": True,
                } if self.fill_ledger is not None else None,
                "book_metrics": getattr(self.feed, "book_metrics", None),
            },
            trial=self.trial_meta,
        )
        if self.provider is not None:
            self.provider.publish(snapshot)
        if self.alert_engine is not None:
            self.alert_engine.evaluate(snapshot)

    # --- Main loop -----------------------------------------------------------------
    # Seeded bars re-evaluated at shadow startup. 24 gives a full day of
    # observability records after any restart, at negligible cost.
    _SHADOW_PRIME_BACKFILL_BARS = 24

    # Idle tick cadence: while no closed candle is pending, the queue wait
    # times out at this interval and the loop gets a tick — tick-stop check +
    # snapshot publish. Tests shrink it via instance override.
    _IDLE_TICK_SECONDS = 5.0

    async def _shadow_prime(self) -> None:
        """SHADOW lanes only: evaluate recent already-closed (seeded) bars
        once at startup — the newest live (may submit a shadow intent), the
        rest as backfill observability records.

        The live loop otherwise acts only on bars that close AFTER startup, so
        a restart silently discards an already-armed condition while it waits
        up to a full bar for the next close — with frequent restarts a slow-bar
        strategy may never get a single decision opportunity, and the bars a
        restart skipped left no record at all (the 2026-07-04 signal cluster
        was part-missed exactly this way). Shadow lanes never fill, so
        re-evaluating bars is safe: backfilled bars journal lane_eval records
        ONLY — an intent is submitted solely for the newest bar. Deliberately
        NOT done for paper/live modes, where re-entering on restart could
        double a position.
        """
        if self.config.mode is not RunnerMode.SHADOW:
            return
        if len(self.candles) <= self.strategy.warmup_bars:
            return
        # let the live feed publish its first top-of-book so sizing has a
        # real reference price (immediate when a quote is already present)
        for _ in range(30):
            if self.feed.quote is not None:
                break
            await asyncio.sleep(0.5)
        if self.feed.quote is None:
            return
        df = self.strategy.prepare(self.candles)
        last = len(df) - 1
        first = max(self.strategy.warmup_bars, last - self._SHADOW_PRIME_BACKFILL_BARS + 1)
        backfill_fired = 0
        for i in range(first, last):
            sig_i = self.strategy.signal(df, i)
            self._record_eval(df, i, sig_i, backfill=True)
            backfill_fired += sig_i is not None
        sig = self.strategy.signal(df, last)
        self._record_eval(df, last, sig)
        logger.info(
            "shadow prime [%s %s]: %d seeded bars backfilled (%d would have "
            "fired), latest -> %s",
            self.strategy.strategy_id, self.config.symbol,
            max(0, last - first), backfill_fired,
            f"{sig.side} intent" if sig is not None else "no signal",
        )
        if sig is not None:
            self.signals += 1
            await self._submit_entry(sig, datetime.now(UTC))

    def _is_paper_observation_lane(self) -> bool:
        trial_id = str((self.trial_meta or {}).get("trial_id") or "")
        return (
            self.config.mode is RunnerMode.PAPER
            and trial_id.endswith("_paper_observation")
        )

    async def _paper_observation_prime(self) -> None:
        """PAPER observation lanes: prime observability, never restart-enter.

        Governed paper trials deliberately do not prime on startup because a
        restart must not duplicate a live-paper entry. Observation mirrors are
        different: their purpose is operator visibility across many lanes, and
        stale scanner rows after every deploy hide the real state. So they
        journal seeded-bar evaluations exactly like shadow prime, but submit
        no orders or intents. Any fired latest bar is marked as a signal in
        lane_eval telemetry only; the paper order path still waits for a
        forward live candle.
        """
        if not self._is_paper_observation_lane():
            return
        if len(self.candles) <= self.strategy.warmup_bars:
            return
        df = self.strategy.prepare(self.candles)
        last = len(df) - 1
        first = max(self.strategy.warmup_bars, last - self._SHADOW_PRIME_BACKFILL_BARS + 1)
        backfill_fired = 0
        for i in range(first, last):
            sig_i = self.strategy.signal(df, i)
            self._record_eval(df, i, sig_i, backfill=True)
            backfill_fired += sig_i is not None
        sig = self.strategy.signal(df, last)
        self._record_eval(
            df,
            last,
            sig,
            skip_reason=(
                "paper_observation_prime: no restart order submitted"
                if sig is not None else None
            ),
        )
        logger.info(
            "paper observation prime [%s %s]: %d seeded bars backfilled "
            "(%d would have fired), latest -> %s (no restart order)",
            self.strategy.strategy_id,
            self.config.symbol,
            max(0, last - first),
            backfill_fired,
            f"{sig.side} signal" if sig is not None else "no signal",
        )

    async def run(self, *, max_bars: int | None = None,
                  deadline_seconds: float | None = None) -> RunReport:
        started = datetime.now(UTC)
        bars = 0
        prepared_warmup = self.strategy.warmup_bars

        if self.shadow_outcomes is not None and self.shadow_outcomes.has_pending:
            # restart: intents journaled before the shutdown resolve against
            # the seeded history first, so an already-hit stop or target is
            # never mis-resolved later at live prices
            self._shadow_exit_df = self.strategy.prepare(self.candles).reset_index(drop=True)
            self._log_shadow_outcomes(
                self.shadow_outcomes.replay(self._shadow_exit_df), started
            )

        await self._shadow_prime()
        await self._paper_observation_prime()
        self._record_runner_heartbeat("runner_started", started, force=True)

        while True:
            if max_bars is not None and bars >= max_bars:
                break
            if deadline_seconds is not None:
                elapsed = (datetime.now(UTC) - started).total_seconds()
                if elapsed >= deadline_seconds:
                    break
            try:
                raw = await asyncio.wait_for(
                    self.feed.closed_candles.get(), timeout=self._IDLE_TICK_SECONDS
                )
            except asyncio.TimeoutError:
                # capital protection between bars: stops (and ONLY stops) are
                # evaluated against the current quote on every idle tick
                idle_now = datetime.now(UTC)
                await self._check_tick_stop(idle_now)
                if self.feed.quote is not None:
                    self._sync_quote()
                    await self._enforce_daily_factory_flatten(idle_now, idle_now)
                self._record_runner_heartbeat("waiting_for_closed_candle", idle_now)
                self._publish_snapshot()  # keep the dashboard honest while idle
                continue

            now = datetime.now(UTC)
            if not self._append_candle(raw) or not self._sync_quote():
                continue
            bars += 1
            self.bars_processed += 1
            self.tracker.on_bar(now)
            self._maybe_daily_report(now)

            bar = self.candles.iloc[-1]
            bar_clock = pd.Timestamp(bar["timestamp"]).to_pydatetime()
            await self._manage_exit(bar, now)
            await self._enforce_daily_factory_flatten(bar_clock, now)
            await self._manage_pending_entry(now)
            await self._cancel_pending_entry_for_daily_factory(bar_clock, now)
            self._guard_orphaned_position()

            if self._plan is None and self._pending_entry is None \
                    and len(self.candles) > prepared_warmup:
                df = self.strategy.prepare(self.candles)
                idx = len(df) - 1
                factory_block = self._daily_factory_entry_block_reason(bar_clock)
                allowed, block_reason = self.protections.entries_allowed(idx)
                if factory_block is not None:
                    sig = None
                    self._record_eval(df, idx, sig, skip_reason=factory_block)
                    self._log_trade_event(
                        "daily_factory_blocked", factory_block[:140], now
                    )
                elif not allowed:
                    sig = None
                    self._record_eval(df, idx, sig, skip_reason=block_reason)
                    if not self._protection_block_logged:
                        self._log_trade_event(
                            "protection_blocked", block_reason[:140], now
                        )
                        self._protection_block_logged = True
                else:
                    self._protection_block_logged = False
                    sig = self.strategy.signal(df, idx)
                    self._record_eval(df, idx, sig)
                if sig is not None:
                    self.signals += 1
                    await self._submit_entry(sig, now)

            if self.shadow_outcomes is not None:
                # resolve earlier shadow intents against this closed bar; the
                # intent journaled above (bar_ts == this bar) is untouched —
                # its virtual fill is the NEXT bar, like the backtester
                self._shadow_exit_df = self.strategy.prepare(self.candles).reset_index(drop=True)
                self._log_shadow_outcomes(
                    # feed the IDENTICAL canonical ATR the paper/live trail uses,
                    # so the shadow stop ratchets to the same value on the same bar
                    self.shadow_outcomes.resolve_bar(
                        self._shadow_exit_df.iloc[-1], atr=self._trail_atr()
                    ),
                    now,
                )

            self._bars_since_reconcile += 1
            if self._bars_since_reconcile >= self.config.reconcile_every_bars \
                    or self.om.has_unresolved_orders:
                report = self._reconcile()
                self.recon_mismatches += len(report.mismatches)
                self._bars_since_reconcile = 0

            self._ledger_new_fills(now)
            self._record_runner_heartbeat("bar_processed", now, force=True)

            if self.account_store is not None:
                self.account_store.save_from(
                    self.exchange, self.tracker, plan=self._serialize_plan()
                )
            if self.funnel_store is not None:
                self.funnel_store.save_from(self)
            if self.equity_history_path is not None:
                try:
                    import json as _json

                    with open(self.equity_history_path, "a", encoding="utf-8") as f:
                        f.write(_json.dumps({
                            "ts": now.isoformat(),
                            "equity": round(self.tracker.equity_usd(), 4),
                        }) + "\n")
                except OSError as exc:
                    logger.warning("equity history write failed: %s", exc)
            self._publish_snapshot()

        final = self._reconcile()
        self.recon_mismatches += len(final.mismatches)
        fills = self.exchange.get_fills()
        report = RunReport(
            mode=f"{self.config.mode.value}_live", symbol=self.config.symbol,
            strategy_id=self.strategy.strategy_id,
            bars_processed=bars, signals_generated=self.signals,
            orders_submitted=self.orders_submitted, fills=len(fills),
            fees_usd=sum(f.fee_usd for f in fills),
            realized_pnl_usd=self.exchange.get_balances()["USDT"]
            - self.config.starting_equity_usd,
            unrealized_pnl_usd=self.tracker.unrealized_pnl_usd(),
            max_drawdown_pct=0.0,  # session-level dd needs longer runs; journal has equity
            risk_rejects=self.risk_rejects, sizing_skips=self.sizing_skips,
            shadow_approved=self.shadow_approved,
            shadow_rejected=self.shadow_rejected,
            reconciliation_mismatches=self.recon_mismatches,
            final_equity_usd=self.tracker.equity_usd(),
        )
        self.journal.append("live_paper_report", report.to_dict())
        return report

    def _reconcile(self):
        report = self.reconciler.run()
        self._fail_closed_on_reconciliation(report.mismatches)
        for coid in report.resolved_orders:
            plan = self._parked_entries.pop(coid, None)
            if plan is None:
                self._resolve_pending_exit(coid)
                continue
            order = self.om.orders[coid]
            if order.state in _EXIT_ACCEPTED_STATES and self._plan is None:
                self._seed_plan_from_venue(plan, coid)
                self._plan = plan
        return report

    def _resolve_pending_exit(self, client_order_id: str) -> None:
        base_key = next(
            (
                key
                for key, pending_id in self._pending_exit_orders.items()
                if pending_id == client_order_id
            ),
            None,
        )
        if base_key is None:
            return
        order = self.om.orders[client_order_id]
        reason = _exit_reason_from_key(base_key)
        if order.state in _EXIT_ACCEPTED_STATES:
            final = self._pending_exit_finals.pop(base_key, True)
            decision = self._pending_exit_decisions.pop(base_key, None)
            if decision is not None and not decision.final and self._plan is not None:
                self._plan.exit_state.mark_accepted(decision)
            self.journal.append("exit_plan_cleared_after_reconciliation", {
                "intent_key": base_key,
                "client_order_id": client_order_id,
                "reason": reason,
                "state": order.state.value,
                "final": final,
            })
            self._pending_exit_orders.pop(base_key, None)
            self._mark_exit_accepted(reason, final=final)
            return
        if order.state in _EXIT_RETRYABLE_STATES:
            self._pending_exit_orders.pop(base_key, None)
            self._pending_exit_finals.pop(base_key, None)
            self._pending_exit_decisions.pop(base_key, None)
            self._exit_retry_attempts[base_key] = self._exit_retry_attempts.get(base_key, 0) + 1
            self.journal.append("exit_plan_preserved_after_reconciliation", {
                "intent_key": base_key,
                "client_order_id": client_order_id,
                "reason": reason,
                "state": order.state.value,
                "next_retry": self._exit_intent_key(base_key),
            })


def _exit_reason_from_key(intent_key: str) -> str:
    parts = intent_key.split("|")
    return parts[2] if len(parts) > 2 else "exit"
