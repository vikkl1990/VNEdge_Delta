"""Maker-first executor with fee-aware taker fallback.

The executor is an orchestration layer, not a broker. It never talks to an
exchange adapter directly; every order still flows through OrderManager, which
means the normal gateway, journal, idempotency, and unresolved-order guards
remain in force.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from enum import Enum

from vnedge.execution.journal import DecisionJournal
from vnedge.execution.order_manager import OrderManager
from vnedge.execution.order_state import ManagedOrder, OrderState
from vnedge.risk.risk_manager import AccountState, MarketState, OrderIntent
from vnedge.scalping.parameter_registry import ExchangeFeeProfile
from vnedge.scalping.microstructure import MarketMicroState
from vnedge.scalping.risk import ScalperRiskGateway
from vnedge.scalping.strategy import QuoteIntent


class ExecutorState(str, Enum):
    BLOCKED = "blocked"
    MAKER_WORKING = "maker_working"
    MAKER_FILLED = "maker_filled"
    MAKER_CANCELLED = "maker_cancelled"
    TAKER_SUBMITTED = "taker_submitted"
    TAKER_BLOCKED = "taker_blocked"
    TIMEOUT_UNKNOWN = "timeout_unknown"


@dataclass(frozen=True)
class RouteCheck:
    route: str
    allowed: bool
    expected_edge_bps: float
    cost_bps: float
    net_edge_bps: float
    cost_coverage: float
    failed_checks: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class MakerTakerExecutionPlan:
    executor_id: str
    intent: OrderIntent
    expected_edge_bps: float
    fee_profile: ExchangeFeeProfile
    maker_ttl_ms: int
    fallback_enabled: bool = True
    min_maker_net_edge_bps: float = 0.0
    min_maker_cost_coverage: float = 1.0
    min_taker_net_edge_bps: float = 0.0
    min_taker_cost_coverage: float = 1.0
    fallback_edge_decay_bps: float = 0.0

    def __post_init__(self) -> None:
        if not self.executor_id:
            raise ValueError("executor_id is required")
        if self.intent.reduce_only:
            raise ValueError("maker/taker entry executor does not manage reduce-only exits")
        if self.intent.limit_price is None or self.intent.limit_price <= 0:
            raise ValueError("maker-first execution requires a positive maker limit_price")
        if self.maker_ttl_ms <= 0:
            raise ValueError("maker_ttl_ms must be positive")
        if self.expected_edge_bps < 0:
            raise ValueError("expected_edge_bps cannot be negative")
        for name, value in (
            ("min_maker_net_edge_bps", self.min_maker_net_edge_bps),
            ("min_maker_cost_coverage", self.min_maker_cost_coverage),
            ("min_taker_net_edge_bps", self.min_taker_net_edge_bps),
            ("min_taker_cost_coverage", self.min_taker_cost_coverage),
            ("fallback_edge_decay_bps", self.fallback_edge_decay_bps),
        ):
            if value < 0:
                raise ValueError(f"{name} cannot be negative")

    @classmethod
    def from_quote_intent(
        cls,
        *,
        executor_id: str,
        quote: QuoteIntent,
        fee_profile: ExchangeFeeProfile,
        fallback_enabled: bool = True,
        min_maker_net_edge_bps: float = 0.0,
        min_maker_cost_coverage: float = 1.0,
        min_taker_net_edge_bps: float = 0.0,
        min_taker_cost_coverage: float = 1.0,
        fallback_edge_decay_bps: float = 0.0,
    ) -> "MakerTakerExecutionPlan":
        """Adapt a scalper QuoteIntent into the maker-first executor contract."""
        if not quote.post_only:
            raise ValueError("maker/taker executor requires QuoteIntent.post_only=True")
        return cls(
            executor_id=executor_id,
            intent=quote.intent,
            expected_edge_bps=quote.expected_edge_bps,
            fee_profile=fee_profile,
            maker_ttl_ms=quote.ttl_ms,
            fallback_enabled=fallback_enabled,
            min_maker_net_edge_bps=min_maker_net_edge_bps,
            min_maker_cost_coverage=min_maker_cost_coverage,
            min_taker_net_edge_bps=min_taker_net_edge_bps,
            min_taker_cost_coverage=min_taker_cost_coverage,
            fallback_edge_decay_bps=fallback_edge_decay_bps,
        )


@dataclass(frozen=True)
class ExecutorReport:
    executor_id: str
    state: ExecutorState
    maker_order: ManagedOrder | None
    taker_order: ManagedOrder | None
    maker_check: RouteCheck
    taker_check: RouteCheck | None
    maker_filled_quantity: float = 0.0
    taker_quantity: float = 0.0
    reason: str = ""

    @property
    def submitted_taker(self) -> bool:
        return self.taker_order is not None

    def to_dict(self) -> dict:
        return {
            "executor_id": self.executor_id,
            "state": self.state.value,
            "maker_client_order_id": (
                None if self.maker_order is None else self.maker_order.client_order_id
            ),
            "taker_client_order_id": (
                None if self.taker_order is None else self.taker_order.client_order_id
            ),
            "maker_check": self.maker_check.to_dict(),
            "taker_check": None if self.taker_check is None else self.taker_check.to_dict(),
            "maker_filled_quantity": self.maker_filled_quantity,
            "taker_quantity": self.taker_quantity,
            "reason": self.reason,
        }


AfterMakerSubmit = Callable[[ManagedOrder], Awaitable[None] | None]


class MakerTakerExecutor:
    """Submit a post-only maker quote, then optionally fall back to taker.

    The taker fallback is deliberately evaluated at fallback time. If the
    remaining edge no longer clears the taker cost hurdle, the executor cancels
    the maker quote and records a no-trade instead of paying the fee wall.
    """

    def __init__(
        self,
        order_manager: OrderManager,
        journal: DecisionJournal,
        *,
        scalper_gateway: ScalperRiskGateway | None = None,
    ) -> None:
        self._om = order_manager
        self._journal = journal
        self._scalper_gateway = scalper_gateway

    async def execute(
        self,
        plan: MakerTakerExecutionPlan,
        *,
        account: AccountState,
        market: MarketState,
        now: datetime | None = None,
        edge_at_fallback_bps: float | None = None,
        account_at_fallback: AccountState | None = None,
        market_at_fallback: MarketState | None = None,
        micro_market: MarketMicroState | None = None,
        micro_market_at_fallback: MarketMicroState | None = None,
        after_maker_submit: AfterMakerSubmit | None = None,
    ) -> ExecutorReport:
        self._journal.append("executor_started", {
            "executor_id": plan.executor_id,
            "strategy_id": plan.intent.strategy_id,
            "symbol": plan.intent.symbol,
            "side": plan.intent.side,
            "expected_edge_bps": plan.expected_edge_bps,
            "maker_ttl_ms": plan.maker_ttl_ms,
            "fallback_enabled": plan.fallback_enabled,
        })

        maker_check = self._route_check(
            "maker",
            edge_bps=plan.expected_edge_bps,
            cost_bps=plan.fee_profile.maker_first_cost_bps,
            min_net_bps=plan.min_maker_net_edge_bps,
            min_cost_coverage=plan.min_maker_cost_coverage,
        )
        self._journal.append("executor_route_check", {
            "executor_id": plan.executor_id,
            **maker_check.to_dict(),
        })
        if not maker_check.allowed:
            return self._finish(
                plan.executor_id,
                ExecutorState.BLOCKED,
                None,
                None,
                maker_check,
                None,
                reason="maker_edge_below_hurdle",
            )

        maker_intent = replace(
            plan.intent,
            order_type="limit",
            time_in_force="PO",
        )
        maker_scalper_failures = self._scalper_risk_failures(
            plan.executor_id,
            "maker",
            maker_intent,
            account,
            micro_market,
            expected_edge_bps=plan.expected_edge_bps,
            now=now,
        )
        if maker_scalper_failures:
            return self._finish(
                plan.executor_id,
                ExecutorState.BLOCKED,
                None,
                None,
                self._block_route(maker_check, maker_scalper_failures),
                None,
                reason="maker_scalper_risk_rejected",
            )
        maker = await self._om.submit(
            maker_intent,
            account,
            self._order_manager_market(market, micro_market),
            intent_key=f"{plan.executor_id}|maker",
            now=now,
        )
        self._record_scalper_order(maker, now)
        self._journal.append("executor_maker_submitted", {
            "executor_id": plan.executor_id,
            "client_order_id": maker.client_order_id,
            "state": maker.state.value,
            "time_in_force": maker.intent.time_in_force,
            "limit_price": maker.intent.limit_price,
        })

        if maker.state is OrderState.TIMEOUT_UNKNOWN:
            return self._finish(
                plan.executor_id,
                ExecutorState.TIMEOUT_UNKNOWN,
                maker,
                None,
                maker_check,
                None,
                reason="maker_submit_timeout_unknown",
            )
        if maker.state is OrderState.RISK_REJECTED or maker.state is OrderState.REJECTED:
            return self._finish(
                plan.executor_id,
                ExecutorState.BLOCKED,
                maker,
                None,
                maker_check,
                None,
                reason=f"maker_submit_{maker.state.value}",
            )

        if after_maker_submit is not None:
            result = after_maker_submit(maker)
            if inspect.isawaitable(result):
                await result

        if maker.state in (OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED):
            maker = await self._om.cancel_order(
                maker.client_order_id,
                reason=f"maker ttl expired after {plan.maker_ttl_ms}ms",
            )
            self._record_scalper_cancel(now)

        maker_filled = max(0.0, maker.filled_quantity)
        if maker.state is OrderState.FILLED:
            return self._finish(
                plan.executor_id,
                ExecutorState.MAKER_FILLED,
                maker,
                None,
                maker_check,
                None,
                maker_filled_quantity=maker.intent.quantity,
                reason="maker_filled_before_fallback",
            )
        if maker.state is OrderState.TIMEOUT_UNKNOWN:
            return self._finish(
                plan.executor_id,
                ExecutorState.TIMEOUT_UNKNOWN,
                maker,
                None,
                maker_check,
                None,
                maker_filled_quantity=maker_filled,
                reason="maker_cancel_timeout_unknown",
            )

        remaining = max(0.0, plan.intent.quantity - maker_filled)
        if remaining <= 1e-12:
            return self._finish(
                plan.executor_id,
                ExecutorState.MAKER_FILLED,
                maker,
                None,
                maker_check,
                None,
                maker_filled_quantity=maker_filled,
                reason="maker_filled_on_cancel",
            )

        fallback_edge = (
            edge_at_fallback_bps
            if edge_at_fallback_bps is not None
            else max(0.0, plan.expected_edge_bps - plan.fallback_edge_decay_bps)
        )
        taker_check = self._route_check(
            "taker_fallback",
            edge_bps=fallback_edge,
            cost_bps=plan.fee_profile.taker_round_trip_cost_bps,
            min_net_bps=plan.min_taker_net_edge_bps,
            min_cost_coverage=plan.min_taker_cost_coverage,
            extra_failed=() if plan.fallback_enabled else ("fallback_disabled",),
        )
        self._journal.append("executor_route_check", {
            "executor_id": plan.executor_id,
            **taker_check.to_dict(),
        })
        if not taker_check.allowed:
            return self._finish(
                plan.executor_id,
                ExecutorState.TAKER_BLOCKED,
                maker,
                None,
                maker_check,
                taker_check,
                maker_filled_quantity=maker_filled,
                reason="taker_fallback_edge_below_hurdle",
            )

        taker_intent = self._taker_intent(plan.intent, remaining)
        taker_account = account_at_fallback or account
        taker_micro_market = micro_market_at_fallback or micro_market
        taker_scalper_failures = self._scalper_risk_failures(
            plan.executor_id,
            "taker_fallback",
            taker_intent,
            taker_account,
            taker_micro_market,
            expected_edge_bps=fallback_edge,
            now=now,
        )
        if taker_scalper_failures:
            return self._finish(
                plan.executor_id,
                ExecutorState.TAKER_BLOCKED,
                maker,
                None,
                maker_check,
                self._block_route(taker_check, taker_scalper_failures),
                maker_filled_quantity=maker_filled,
                reason="taker_fallback_scalper_risk_rejected",
            )

        taker = await self._om.submit(
            taker_intent,
            taker_account,
            self._order_manager_market(market_at_fallback or market, taker_micro_market),
            intent_key=f"{plan.executor_id}|taker_fallback",
            now=now,
        )
        self._record_scalper_order(taker, now)
        self._journal.append("executor_taker_submitted", {
            "executor_id": plan.executor_id,
            "client_order_id": taker.client_order_id,
            "state": taker.state.value,
            "quantity": remaining,
            "expected_edge_bps": fallback_edge,
            "cost_bps": taker_check.cost_bps,
        })
        state = ExecutorState.TAKER_SUBMITTED
        reason = "maker_unfilled_taker_fallback_submitted"
        if taker.state is OrderState.TIMEOUT_UNKNOWN:
            state = ExecutorState.TIMEOUT_UNKNOWN
            reason = "taker_submit_timeout_unknown"
        elif taker.state in (OrderState.RISK_REJECTED, OrderState.REJECTED):
            state = ExecutorState.TAKER_BLOCKED
            reason = f"taker_submit_{taker.state.value}"
        return self._finish(
            plan.executor_id,
            state,
            maker,
            taker,
            maker_check,
            taker_check,
            maker_filled_quantity=maker_filled,
            taker_quantity=remaining,
            reason=reason,
        )

    def _route_check(
        self,
        route: str,
        *,
        edge_bps: float,
        cost_bps: float,
        min_net_bps: float,
        min_cost_coverage: float,
        extra_failed: tuple[str, ...] = (),
    ) -> RouteCheck:
        coverage = float("inf") if cost_bps == 0 else edge_bps / cost_bps
        net = edge_bps - cost_bps
        failed = list(extra_failed)
        if net < min_net_bps:
            failed.append(
                f"net_edge_bps {net:.2f} < required {min_net_bps:.2f}"
            )
        if coverage < min_cost_coverage:
            failed.append(
                f"cost_coverage {coverage:.2f} < required {min_cost_coverage:.2f}"
            )
        return RouteCheck(
            route=route,
            allowed=not failed,
            expected_edge_bps=edge_bps,
            cost_bps=cost_bps,
            net_edge_bps=net,
            cost_coverage=coverage,
            failed_checks=tuple(failed),
        )

    def _scalper_risk_failures(
        self,
        executor_id: str,
        route: str,
        intent: OrderIntent,
        account: AccountState,
        micro_market: MarketMicroState | None,
        *,
        expected_edge_bps: float,
        now: datetime | None,
    ) -> tuple[str, ...]:
        if self._scalper_gateway is None:
            return ()
        if micro_market is None:
            failed = ("scalper_micro_market_missing",)
            self._journal.append("executor_scalper_risk_decision", {
                "executor_id": executor_id,
                "route": route,
                "approved": False,
                "failed_checks": list(failed),
                "passed_checks": [],
                "expected_edge_bps": expected_edge_bps,
            })
            return failed
        decision = self._scalper_gateway.evaluate(
            intent,
            account,
            micro_market,
            expected_edge_bps=expected_edge_bps,
            now=now,
        )
        self._journal.append("executor_scalper_risk_decision", {
            "executor_id": executor_id,
            "route": route,
            "approved": decision.approved,
            "base_approved": decision.base_decision.approved,
            "failed_checks": list(decision.failed_checks),
            "passed_checks": list(decision.passed_checks),
            "expected_edge_bps": expected_edge_bps,
        })
        return () if decision.approved else decision.failed_checks

    def _order_manager_market(
        self,
        market: MarketState,
        micro_market: MarketMicroState | None,
    ) -> MarketState:
        if self._scalper_gateway is None or micro_market is None:
            return market
        return micro_market.to_market_state()

    def _record_scalper_order(self, order: ManagedOrder, now: datetime | None) -> None:
        if self._scalper_gateway is None or order.state is OrderState.RISK_REJECTED:
            return
        self._scalper_gateway.record_order(now)

    def _record_scalper_cancel(self, now: datetime | None) -> None:
        if self._scalper_gateway is not None:
            self._scalper_gateway.record_cancel(now)

    def _block_route(self, check: RouteCheck, failures: tuple[str, ...]) -> RouteCheck:
        return replace(
            check,
            allowed=False,
            failed_checks=check.failed_checks + failures,
        )

    def _taker_intent(self, base: OrderIntent, quantity: float) -> OrderIntent:
        notional = base.notional_usd * (quantity / base.quantity)
        return replace(
            base,
            quantity=quantity,
            notional_usd=notional,
            order_type="market",
            limit_price=None,
            time_in_force=None,
        )

    def _finish(
        self,
        executor_id: str,
        state: ExecutorState,
        maker_order: ManagedOrder | None,
        taker_order: ManagedOrder | None,
        maker_check: RouteCheck,
        taker_check: RouteCheck | None,
        *,
        maker_filled_quantity: float = 0.0,
        taker_quantity: float = 0.0,
        reason: str = "",
    ) -> ExecutorReport:
        report = ExecutorReport(
            executor_id=executor_id,
            state=state,
            maker_order=maker_order,
            taker_order=taker_order,
            maker_check=maker_check,
            taker_check=taker_check,
            maker_filled_quantity=maker_filled_quantity,
            taker_quantity=taker_quantity,
            reason=reason,
        )
        self._journal.append("executor_finished", report.to_dict())
        return report
