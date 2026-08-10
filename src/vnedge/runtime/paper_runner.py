"""Paper/shadow runner — the loop that makes the mode ladder walkable.

    bar -> quotes -> tracker -> [pending entry fill] -> exit management
        -> signal at close -> periodic reconciliation -> report

One loop serves both modes ON PURPOSE: a separate shadow runner would be a
second execution path that could drift from the paper path — the exact thing
the design forbids. In SHADOW mode the pipeline runs up to and including the
risk verdict, journals it, and stops there: no submission, no balance change.

Execution discipline mirrors the backtester: signals are taken at bar close
and filled at the next bar's open; stops are checked against bar high/low
with stop-beats-take-profit ordering; exits are reduce-only market orders
through the same OrderManager as entries. There is no strategy-to-broker
shortcut anywhere in this file.

Unknown-order policy: an entry that lands in TIMEOUT_UNKNOWN parks its trade
plan; the OrderManager already blocks further risk-increasing submissions,
and the runner activates or discards the plan when scheduled reconciliation
resolves the order from venue truth.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

from vnedge.execution.idempotency import make_intent_key
from vnedge.execution.journal import DecisionJournal
from vnedge.execution.order_manager import OrderManager
from vnedge.execution.order_state import ManagedOrder, OrderState
from vnedge.paper.paper_reconciliation import PaperReconciler
from vnedge.paper.simulated_exchange import SimulatedExchange
from vnedge.risk.position_sizer import size_position
from vnedge.risk.risk_manager import OrderIntent, PreTradeRiskGateway
from vnedge.runtime.market_replay import MarketReplay, quote_from_price
from vnedge.runtime.active_exit import ActiveExitDecision, ActiveExitState
from vnedge.runtime.portfolio_tracker import PortfolioTracker
from vnedge.runtime.run_report import RunReport
from vnedge.runtime.runner_config import RunnerConfig, RunnerMode
from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent

logger = logging.getLogger(__name__)

_EXIT_ACCEPTED_STATES = frozenset(
    {OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED, OrderState.FILLED}
)


@dataclass
class _TradePlan:
    signal: SignalIntent
    order: ManagedOrder
    entry_bar: int
    exit_state: ActiveExitState


class PaperRunner:
    def __init__(
        self,
        strategy: BaseStrategy,
        candles: pd.DataFrame,
        funding: pd.DataFrame | None,
        config: RunnerConfig,
        *,
        gateway: PreTradeRiskGateway,
        order_manager: OrderManager,
        exchange: SimulatedExchange,
        journal: DecisionJournal,
        on_bar=None,  # optional async hook(bar_index, ts) — pacing/snapshots
    ) -> None:
        self.strategy = strategy
        self.candles = candles
        self.config = config
        self.on_bar = on_bar
        self.gateway = gateway
        self.om = order_manager
        self.exchange = exchange
        self.journal = journal
        self.tracker = PortfolioTracker(exchange, config.starting_equity_usd)
        self.reconciler = PaperReconciler(order_manager, exchange)
        self.replay = MarketReplay(
            candles, funding, symbol=config.symbol,
            spread_bps=config.spread_bps, slippage_est_bps=config.slippage_est_bps,
        )
        # counters
        self.signals = self.orders_submitted = self.risk_rejects = 0
        self.sizing_skips = self.shadow_approved = self.shadow_rejected = 0
        self.recon_mismatches = 0
        self._reconciliation_fail_closed = False

    # --- Helpers -----------------------------------------------------------------
    def _set_quote(self, price: float) -> None:
        bid, ask = quote_from_price(price, self.config.spread_bps)
        self.exchange.set_quote(self.config.symbol, bid, ask)

    def _build_entry_intent(self, sig: SignalIntent, ref_price: float) -> OrderIntent | None:
        sizing = size_position(
            equity_usd=self.tracker.equity_usd(), entry_price=ref_price,
            stop_price=sig.stop_price, side=sig.side,
            config=self.config.risk, limits=self.config.limits,
        )
        if not sizing.approved:
            self.sizing_skips += 1
            logger.info("sizing skipped entry: %s", sizing.reasons)
            return None
        return OrderIntent(
            symbol=self.config.symbol, side=sig.side, quantity=sizing.quantity,
            notional_usd=sizing.notional_usd,
            leverage=max(sizing.required_leverage, 1.0),
            reduce_only=False, strategy_id=self.strategy.strategy_id,
        )

    def _seed_plan_from_venue(self, plan: _TradePlan) -> None:
        status = self.exchange.get_order_status(plan.order.client_order_id)
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

    async def _submit_exit(
        self,
        plan: _TradePlan,
        bar_ts,
        reason: str,
        *,
        quantity: float | None = None,
        final: bool = True,
    ) -> ManagedOrder | None:
        positions = {p.symbol: p for p in self.exchange.get_positions()}
        pos = positions.get(self.config.symbol)
        if pos is None:
            return None
        close_qty = abs(pos.quantity) if quantity is None else min(abs(pos.quantity), quantity)
        if close_qty <= 0:
            return None
        intent = OrderIntent(
            symbol=self.config.symbol,
            side="short" if pos.quantity > 0 else "long",
            quantity=close_qty, notional_usd=0.0, leverage=1.0,
            reduce_only=True, strategy_id=self.strategy.strategy_id,
        )
        order = await self.om.submit(
            intent, self.tracker.account_state(), self.replay.market_state(self._bar_index),
            intent_key=f"exit|{self.config.symbol}|{reason}|{int(bar_ts.value)}",
            now=bar_ts.to_pydatetime(),
        )
        self.orders_submitted += 1
        self.journal.append("paper_exit", {
            "reason": reason,
            "state": order.state.value,
            "ts": str(bar_ts),
            "quantity": close_qty,
            "final": final,
            "active_stop_price": plan.exit_state.current_stop,
            "breakeven_armed": plan.exit_state.breakeven_armed,
        })
        return order

    def _active_exit_decision(
        self,
        plan: _TradePlan,
        *,
        bar: pd.Series,
        max_holding_hit: bool,
    ) -> ActiveExitDecision | None:
        positions = {p.symbol: p for p in self.exchange.get_positions()}
        pos = positions.get(self.config.symbol)
        if pos is None:
            return ActiveExitDecision(
                reason="flat_position",
                exit_price=float(bar["close"]),
                quantity=None,
                final=True,
                active_stop_price=plan.exit_state.current_stop,
                breakeven_armed=plan.exit_state.breakeven_armed,
            )
        return plan.exit_state.resolve_bar(
            high=float(bar["high"]),
            low=float(bar["low"]),
            close=float(bar["close"]),
            position_quantity=abs(pos.quantity),
            min_qty=self.config.limits.min_qty,
            qty_step=self.config.limits.qty_step,
            max_holding_hit=max_holding_hit,
        )

    def _strategy_exit_decision(
        self,
        plan: _TradePlan,
        *,
        df: pd.DataFrame,
        index: int,
        bar: pd.Series,
    ) -> ActiveExitDecision | None:
        entry = plan.exit_state.entry_price or plan.order.intent.notional_usd / max(
            plan.order.intent.quantity, 1e-12
        )
        exit_sig = self.strategy.exit_signal(df, index, plan.signal.side, entry)
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
            active_stop_price=plan.exit_state.current_stop,
            breakeven_armed=plan.exit_state.breakeven_armed,
            tp_reached=plan.exit_state.tp_reached(),
            mfe_price=plan.exit_state.mfe_price,
        )

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

    def _reconcile(self, resolved_plans: dict[str, _TradePlan]) -> _TradePlan | None:
        """Run reconciliation; activate/discard parked plans; return the plan
        that became active (if its entry turned out FILLED)."""
        report = self.reconciler.run()
        self.recon_mismatches += len(report.mismatches)
        self._fail_closed_on_reconciliation(report.mismatches)
        activated: _TradePlan | None = None
        for coid in report.resolved_orders:
            plan = resolved_plans.pop(coid, None)
            if plan is None:
                continue
            state = self.om.orders[coid].state
            if state in (OrderState.FILLED, OrderState.PARTIALLY_FILLED,
                         OrderState.ACKNOWLEDGED):
                activated = plan
            # REJECTED/CANCELLED: lost submission — plan simply dissolves.
        return activated

    # --- Main loop -----------------------------------------------------------------
    async def run(self) -> RunReport:
        cfg = self.config
        df = self.strategy.prepare(self.candles).reset_index(drop=True)
        n = len(df)
        start = max(self.strategy.warmup_bars, 1)

        pending_signal: SignalIntent | None = None
        plan: _TradePlan | None = None
        parked: dict[str, _TradePlan] = {}  # TIMEOUT_UNKNOWN entries by coid
        equities: list[float] = []

        for j in range(start, n):
            self._bar_index = j
            bar = df.iloc[j]
            ts = bar["timestamp"]

            # 1) quote at bar open; tracker rolls the bar
            self._set_quote(float(bar["open"]))
            market = self.replay.market_state(j)
            self.tracker.on_bar(ts)

            # 2) fill last bar's signal at this bar's open
            if pending_signal is not None and plan is None and not parked:
                intent = self._build_entry_intent(pending_signal, float(bar["open"]))
                if intent is not None:
                    key = make_intent_key(
                        self.strategy.strategy_id, cfg.symbol, intent.side, ts
                    )
                    if cfg.mode is RunnerMode.SHADOW:
                        decision = self.gateway.evaluate(
                            intent, self.tracker.account_state(), market, now=ts,
                        )
                        self.journal.append("shadow_intent", {
                            "intent_key": key, "approved": decision.approved,
                            "explanation": decision.explanation,
                            "signal_reason": pending_signal.reason,
                        })
                        if decision.approved:
                            self.shadow_approved += 1
                        else:
                            self.shadow_rejected += 1
                    else:
                        order = await self.om.submit(
                            intent, self.tracker.account_state(), market, key,
                            now=ts.to_pydatetime(),
                        )
                        if order.state is OrderState.RISK_REJECTED:
                            self.risk_rejects += 1
                        else:
                            self.orders_submitted += 1
                            exit_state = ActiveExitState.from_signal(pending_signal)
                            new_plan = _TradePlan(pending_signal, order, j, exit_state)
                            self._seed_plan_from_venue(new_plan)
                            if order.state is OrderState.TIMEOUT_UNKNOWN:
                                parked[order.client_order_id] = new_plan
                            elif order.state is OrderState.ACKNOWLEDGED:
                                plan = new_plan
            pending_signal = None

            # 3) exit management (paper mode) — stop first, always
            if plan is not None:
                decision = self._active_exit_decision(
                    plan,
                    bar=bar,
                    max_holding_hit=False,
                )
                if decision is None:
                    decision = self._strategy_exit_decision(
                        plan, df=df, index=j, bar=bar
                    )
                if decision is None:
                    decision = self._active_exit_decision(
                        plan,
                        bar=bar,
                        max_holding_hit=j - plan.entry_bar >= cfg.max_holding_bars,
                    )
                if decision is not None:
                    if decision.reason == "flat_position":
                        plan = None
                    else:
                        self._set_quote(decision.exit_price)  # fill at trigger level
                        order = await self._submit_exit(
                            plan,
                            ts,
                            decision.reason,
                            quantity=decision.quantity,
                            final=decision.final,
                        )
                        if order is not None and order.state in _EXIT_ACCEPTED_STATES:
                            plan.exit_state.mark_accepted(decision)
                            if decision.final:
                                plan = None

            # 4) mark to close; new signal only when flat and nothing parked
            self._set_quote(float(bar["close"]))
            if (
                plan is None and not parked and pending_signal is None
                and j < n - 1
            ):
                sig = self.strategy.signal(df, j)
                if sig is not None:
                    self.signals += 1
                    pending_signal = sig

            # 5) periodic reconciliation
            if (j - start) % cfg.reconcile_every_bars == 0 or parked:
                activated = self._reconcile(parked)
                if activated is not None and plan is None:
                    self._seed_plan_from_venue(activated)
                    plan = activated

            equities.append(self.tracker.equity_usd())
            if self.on_bar is not None:
                await self.on_bar(j, ts)

        # final reconciliation
        self._reconcile(parked)

        peak, max_dd = 0.0, 0.0
        for eq in equities:
            peak = max(peak, eq)
            if peak > 0:
                max_dd = max(max_dd, (peak - eq) / peak * 100.0)

        fills = self.exchange.get_fills()
        report = RunReport(
            mode=cfg.mode.value, symbol=cfg.symbol,
            strategy_id=self.strategy.strategy_id,
            bars_processed=n - start, signals_generated=self.signals,
            orders_submitted=self.orders_submitted, fills=len(fills),
            fees_usd=sum(f.fee_usd for f in fills),
            realized_pnl_usd=self.exchange.get_balances()["USDT"]
            - cfg.starting_equity_usd,
            unrealized_pnl_usd=self.tracker.unrealized_pnl_usd(),
            max_drawdown_pct=max_dd,
            risk_rejects=self.risk_rejects, sizing_skips=self.sizing_skips,
            shadow_approved=self.shadow_approved,
            shadow_rejected=self.shadow_rejected,
            reconciliation_mismatches=self.recon_mismatches,
            final_equity_usd=self.tracker.equity_usd(),
        )
        self.journal.append("run_report", report.to_dict())
        logger.info(report.summary)
        return report
