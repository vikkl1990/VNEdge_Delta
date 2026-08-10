"""Scalper-specific risk checks layered over the existing gateway.

The core invariant remains unchanged: every order still passes
PreTradeRiskGateway.evaluate(). This wrapper adds hot-path checks that matter
for scalping: sub-second book/private-stream freshness, depth, edge after
fees, and rate budgets.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, Field

from vnedge.risk.risk_manager import (
    AccountState,
    OrderIntent,
    PreTradeRiskGateway,
    RiskDecision,
)
from vnedge.scalping.microstructure import MarketMicroState


class ScalperRiskConfig(BaseModel):
    model_config = {"frozen": True}

    max_book_staleness_ms: float = Field(default=750.0, gt=0)
    max_private_staleness_ms: float = Field(default=1_500.0, gt=0)
    max_spread_bps: float = Field(default=3.0, gt=0)
    min_top_depth_usd: float = Field(default=5_000.0, ge=0)
    maker_fee_bps: float = Field(default=2.0, ge=0)
    taker_fee_bps: float = Field(default=5.0, ge=0)
    adverse_selection_buffer_bps: float = Field(default=2.0, ge=0)
    max_orders_per_minute: int = Field(default=30, ge=1)
    max_cancels_per_minute: int = Field(default=60, ge=1)


@dataclass
class ScalperRiskLimits:
    """Sliding-window submit/cancel counters for the hot path."""

    max_orders_per_minute: int
    max_cancels_per_minute: int
    _orders: deque[datetime] | None = None
    _cancels: deque[datetime] | None = None

    def __post_init__(self) -> None:
        self._orders = deque()
        self._cancels = deque()

    def _prune(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=60)
        assert self._orders is not None and self._cancels is not None
        while self._orders and self._orders[0] < cutoff:
            self._orders.popleft()
        while self._cancels and self._cancels[0] < cutoff:
            self._cancels.popleft()

    def record_order(self, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        self._prune(now)
        assert self._orders is not None
        self._orders.append(now)

    def record_cancel(self, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        self._prune(now)
        assert self._cancels is not None
        self._cancels.append(now)

    def order_count(self, now: datetime | None = None) -> int:
        now = now or datetime.now(UTC)
        self._prune(now)
        assert self._orders is not None
        return len(self._orders)

    def cancel_count(self, now: datetime | None = None) -> int:
        now = now or datetime.now(UTC)
        self._prune(now)
        assert self._cancels is not None
        return len(self._cancels)


@dataclass(frozen=True)
class ScalperRiskDecision:
    approved: bool
    base_decision: RiskDecision
    failed_checks: tuple[str, ...]
    passed_checks: tuple[str, ...]

    @property
    def explanation(self) -> str:
        if self.approved:
            return f"APPROVED ({len(self.passed_checks)} scalper checks passed)"
        return "REJECTED: " + "; ".join(self.failed_checks)


class ScalperRiskGateway:
    def __init__(
        self,
        base_gateway: PreTradeRiskGateway,
        config: ScalperRiskConfig = ScalperRiskConfig(),
        limits: ScalperRiskLimits | None = None,
    ) -> None:
        self.base_gateway = base_gateway
        self.config = config
        self.limits = limits or ScalperRiskLimits(
            config.max_orders_per_minute, config.max_cancels_per_minute
        )

    def evaluate(
        self,
        intent: OrderIntent,
        account: AccountState,
        market: MarketMicroState,
        *,
        expected_edge_bps: float = 0.0,
        now: datetime | None = None,
    ) -> ScalperRiskDecision:
        now = now or datetime.now(UTC)
        base = self.base_gateway.evaluate(
            intent, account, market.to_market_state(), now=now
        )
        failed = list(base.failed_checks)
        passed = list(base.passed_checks)

        def check(name: str, ok: bool, detail: str = "") -> None:
            if ok:
                passed.append(name)
            else:
                failed.append(f"{name}: {detail}" if detail else name)

        book_age_ms = market.top.age_seconds(now) * 1000.0
        check(
            "scalper_book_freshness",
            book_age_ms <= self.config.max_book_staleness_ms,
            f"book {book_age_ms:.0f}ms old "
            f"(max {self.config.max_book_staleness_ms:.0f}ms)",
        )

        if not intent.reduce_only:
            private_age_ms = market.private_age_seconds(now) * 1000.0
            private_ok = (
                market.private is not None
                and market.private.connected
                and private_age_ms <= self.config.max_private_staleness_ms
            )
            check(
                "scalper_private_stream",
                private_ok,
                f"private stream stale/disconnected ({private_age_ms:.0f}ms)",
            )
            check(
                "scalper_spread",
                market.top.spread_bps <= self.config.max_spread_bps,
                f"{market.top.spread_bps:.2f}bps > {self.config.max_spread_bps:.2f}bps",
            )
            check(
                "scalper_top_depth",
                market.top.top_depth_usd >= self.config.min_top_depth_usd,
                f"${market.top.top_depth_usd:.2f} < ${self.config.min_top_depth_usd:.2f}",
            )
            check(
                "scalper_order_rate",
                self.limits.order_count(now) < self.config.max_orders_per_minute,
                f"{self.limits.order_count(now)} orders/min "
                f"(max {self.config.max_orders_per_minute})",
            )
            check(
                "scalper_cancel_rate",
                self.limits.cancel_count(now) < self.config.max_cancels_per_minute,
                f"{self.limits.cancel_count(now)} cancels/min "
                f"(max {self.config.max_cancels_per_minute})",
            )
            entry_fee = (
                self.config.maker_fee_bps
                if intent.order_type == "limit"
                else self.config.taker_fee_bps
            )
            required_edge = (
                entry_fee
                + self.config.taker_fee_bps
                + market.estimated_slippage_bps
                + self.config.adverse_selection_buffer_bps
            )
            check(
                "scalper_edge_after_cost",
                expected_edge_bps >= required_edge,
                f"edge {expected_edge_bps:.2f}bps < required {required_edge:.2f}bps",
            )

        return ScalperRiskDecision(
            approved=not failed,
            base_decision=base,
            failed_checks=tuple(failed),
            passed_checks=tuple(passed),
        )

    def record_order(self, now: datetime | None = None) -> None:
        """Record a hot-path order submission attempt after it reaches OrderManager."""
        self.limits.record_order(now)

    def record_cancel(self, now: datetime | None = None) -> None:
        """Record a hot-path cancel attempt after it reaches OrderManager."""
        self.limits.record_cancel(now)
