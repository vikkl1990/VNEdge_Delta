"""Live trader runtime.

The paper session's counterpart that submits REAL orders through the
CcxtExecutionAdapter. Every safety property is preserved and one is added:

- THREE-GATE ENFORCEMENT: the session refuses to construct unless
  settings.is_live (a live_* mode AND live_trading_enabled AND the exact
  confirmation phrase). There is no way to run it with a real adapter
  without all three. The adapter adds ``live_confirmed`` as a fourth
  mainnet-construction gate; testnet/sandbox execution is not a validation
  path for scalping.
- No bypass of PreTradeRiskGateway: every intent goes through OrderManager,
  exactly as in paper.
- emergency_reduce_only mode blocks new entries; only reduce-only exits flow.
- capital cap: entries are refused once account equity reaches the
  live_small ceiling (defence in depth on top of the gateway's exposure caps).
- Unknown orders are resolved by LiveReconciler against venue truth, never
  by assumption; while any is unresolved, new risk is blocked.

Account state (equity, positions) comes from the venue via an
AccountProvider, so the gateway sees real balances. Built to be exercised
with fakes now (no keys) and wired to a live CcxtAccountProvider later.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Protocol

import pandas as pd

from vnedge.config.settings import Settings, TradingMode
from vnedge.execution.live_reconciliation import LiveReconciler
from vnedge.execution.order_manager import FlattenTarget, OrderManager
from vnedge.execution.order_state import OrderState
from vnedge.risk.position_sizer import SymbolLimits, size_position
from vnedge.risk.risk_manager import AccountState, OrderIntent
from vnedge.runtime.run_report import RunReport
from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent

logger = logging.getLogger(__name__)

_EXIT_ACCEPTED_STATES = frozenset(
    {OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED, OrderState.FILLED}
)
_EXIT_RETRYABLE_STATES = frozenset(
    {OrderState.RISK_REJECTED, OrderState.REJECTED, OrderState.CANCELLED}
)


class AccountProvider(Protocol):
    """Supplies live account truth to the gateway."""

    async def account_state(self) -> AccountState: ...
    async def open_positions(self) -> list[FlattenTarget]: ...


class PrivateStreamHealthProvider(Protocol):
    connected: bool

    def age_seconds(self, now: datetime | None = None) -> float: ...


class LiveTraderSession:
    def __init__(
        self,
        strategy: BaseStrategy,
        feed,  # LiveMarketFeed or a fake with the same surface
        history: pd.DataFrame,
        *,
        settings: Settings,
        gateway,  # PreTradeRiskGateway
        order_manager: OrderManager,
        reconciler: LiveReconciler,
        account_provider: AccountProvider,
        symbol: str,
        limits: SymbolLimits,
        reconcile_every_bars: int = 1,
        pre_live_report=None,
        private_stream_health: PrivateStreamHealthProvider | None = None,
        require_private_stream: bool = False,
        max_private_stream_age_seconds: float = 5.0,
    ) -> None:
        # --- THE GATE: no live trader without all three live gates open ---
        if not settings.is_live:
            raise RuntimeError(
                "LiveTraderSession requires all three live gates: a live_* mode, "
                "live_trading_enabled=true, and the exact confirmation phrase. "
                f"Current: mode={settings.trading_mode.value}, "
                f"enabled={settings.live_trading_enabled}, "
                f"phrase_ok={settings.confirm_live_trading != ''}"
            )
        if pre_live_report is not None and not pre_live_report.cleared:
            failures = ", ".join(f.name for f in pre_live_report.failures)
            raise RuntimeError(f"pre-live checklist not cleared: {failures}")
        if require_private_stream and private_stream_health is None:
            raise RuntimeError("require_private_stream=True needs private_stream_health")
        self.strategy = strategy
        self.feed = feed
        self.candles = history.reset_index(drop=True)
        self.settings = settings
        self.gateway = gateway
        self.om = order_manager
        self.reconciler = reconciler
        self.accounts = account_provider
        self.symbol = symbol
        self.limits = limits
        self.reconcile_every_bars = reconcile_every_bars
        self.private_stream_health = private_stream_health
        self.require_private_stream = require_private_stream
        self.max_private_stream_age_seconds = max_private_stream_age_seconds
        self.signals = self.orders_submitted = self.risk_rejects = 0
        self.sizing_skips = self.recon_mismatches = 0
        self._plan: SignalIntent | None = None
        self._entry_bar_ts = None
        self._parked_entries = {}
        self._pending_exit_orders: dict[str, str] = {}
        self._exit_retry_attempts: dict[str, int] = {}
        self._bars = 0
        # Runtime fail-closed latch: set when position reconciliation finds the
        # venue diverging from internal state; blocks new entries (exits still
        # flow) until a clean settled pass clears it. The static
        # EMERGENCY_REDUCE_ONLY mode can't be flipped at runtime, so a live
        # divergence needs this latch to actually halt entries.
        self._reconciliation_halt = False

    @property
    def entries_allowed(self) -> bool:
        """emergency_reduce_only mode — or a reconciliation halt — allows exits only."""
        return (
            self.settings.trading_mode is not TradingMode.EMERGENCY_REDUCE_ONLY
            and not self._reconciliation_halt
        )

    def private_stream_ready(self, now: datetime | None = None) -> bool:
        if not self.require_private_stream:
            return True
        health = self.private_stream_health
        if health is None or not health.connected:
            return False
        return health.age_seconds(now) <= self.max_private_stream_age_seconds

    async def _submit_entry(self, sig: SignalIntent, now: datetime) -> None:
        account = await self.accounts.account_state()
        if account.equity_usd >= self.settings.live_small_capital_cap_usd \
                and self.settings.trading_mode is TradingMode.LIVE_SMALL:
            logger.warning("equity $%.2f at/above live_small cap $%.2f — entry refused",
                           account.equity_usd, self.settings.live_small_capital_cap_usd)
            return
        bid, ask = self.feed.quote
        ref = ask if sig.side == "long" else bid
        sizing = size_position(
            equity_usd=account.equity_usd, entry_price=ref, stop_price=sig.stop_price,
            side=sig.side, config=self.settings.risk, limits=self.limits,
        )
        if not sizing.approved:
            self.sizing_skips += 1
            return
        intent = OrderIntent(
            symbol=self.symbol, side=sig.side, quantity=sizing.quantity,
            notional_usd=sizing.notional_usd,
            leverage=max(sizing.required_leverage, 1.0),
            reduce_only=False, strategy_id=self.strategy.strategy_id,
        )
        from vnedge.execution.idempotency import make_intent_key

        key = make_intent_key(self.strategy.strategy_id, self.symbol, sig.side,
                              self.candles["timestamp"].iloc[-1])
        order = await self.om.submit(intent, account, self.feed.market_state(), key, now=now)
        if order.state is OrderState.RISK_REJECTED:
            self.risk_rejects += 1
        elif order.state is OrderState.ACKNOWLEDGED:
            self.orders_submitted += 1
            self._plan = sig
            self._entry_bar_ts = self.candles["timestamp"].iloc[-1]
        elif order.state is OrderState.TIMEOUT_UNKNOWN:
            self.orders_submitted += 1
            self._parked_entries[order.client_order_id] = (
                sig,
                self.candles["timestamp"].iloc[-1],
            )

    async def _submit_exit(self, reason: str, now: datetime) -> None:
        positions = await self.accounts.open_positions()
        pos = next((p for p in positions if p.symbol == self.symbol), None)
        if pos is None:
            self._clear_exit_plan()
            return
        intent = OrderIntent(
            symbol=self.symbol, side="short" if pos.side == "long" else "long",
            quantity=pos.quantity, notional_usd=0.0, leverage=1.0,
            reduce_only=True, strategy_id=self.strategy.strategy_id,
        )
        account = await self.accounts.account_state()
        key_ts = (
            int(pd.Timestamp(self._entry_bar_ts).value)
            if self._entry_bar_ts is not None
            else int(now.timestamp() * 1000)
        )
        base_key = f"exit|{self.symbol}|{reason}|{key_ts}"
        pending = self._pending_exit_orders.get(base_key)
        if pending is not None:
            pending_order = self.om.orders.get(pending)
            if pending_order is not None and pending_order.state in (
                OrderState.TIMEOUT_UNKNOWN,
                OrderState.RECONCILING,
            ):
                return
            self._pending_exit_orders.pop(base_key, None)
        order = await self.om.submit(
            intent, account, self.feed.market_state(),
            intent_key=self._exit_intent_key(base_key), now=now,
        )
        self.orders_submitted += 1
        if order.state in _EXIT_ACCEPTED_STATES:
            self._clear_exit_plan()
        else:
            self._preserve_exit_plan(base_key, order)

    def _exit_intent_key(self, base_key: str) -> str:
        attempt = self._exit_retry_attempts.get(base_key, 0)
        return base_key if attempt == 0 else f"{base_key}|retry={attempt}"

    def _clear_exit_plan(self) -> None:
        self._plan = None
        self._entry_bar_ts = None
        self._pending_exit_orders.clear()
        self._exit_retry_attempts.clear()

    def _preserve_exit_plan(self, base_key: str, order) -> None:
        if order.state in (OrderState.TIMEOUT_UNKNOWN, OrderState.RECONCILING):
            self._pending_exit_orders[base_key] = order.client_order_id
        elif order.state in _EXIT_RETRYABLE_STATES:
            self._exit_retry_attempts[base_key] = self._exit_retry_attempts.get(base_key, 0) + 1
        if order.state is OrderState.RISK_REJECTED:
            self.risk_rejects += 1
        logger.warning(
            "preserving live exit plan after submit ended %s (%s)",
            order.state.value,
            order.client_order_id,
        )

    async def emergency_flatten(self) -> None:
        """Close every venue position reduce-only through the normal pipeline."""
        positions = await self.accounts.open_positions()
        account = await self.accounts.account_state()
        markets = {self.symbol: self.feed.market_state()}
        fid = f"flatten|{int(datetime.now(UTC).timestamp() * 1000)}"
        await self.om.emergency_flatten(positions, account, markets, fid,
                                        now=datetime.now(UTC))

    async def run(self, *, max_bars: int | None = None) -> RunReport:
        import asyncio

        while max_bars is None or self._bars < max_bars:
            try:
                raw = await asyncio.wait_for(self.feed.closed_candles.get(), timeout=5.0)
            except asyncio.TimeoutError:
                await self._reconcile()
                continue
            now = datetime.now(UTC)
            ts = pd.to_datetime(raw[0], unit="ms", utc=True)
            if len(self.candles) and ts <= self.candles["timestamp"].iloc[-1]:
                continue
            row = {"timestamp": ts, "open": float(raw[1]), "high": float(raw[2]),
                   "low": float(raw[3]), "close": float(raw[4]), "volume": float(raw[5])}
            self.candles = pd.concat([self.candles, pd.DataFrame([row])], ignore_index=True)
            self._bars += 1

            bar = self.candles.iloc[-1]
            # exits first (always allowed, even in emergency_reduce_only)
            if self._plan is not None:
                sig = self._plan
                hit = None
                if sig.side == "long":
                    if float(bar["low"]) <= sig.stop_price:
                        hit = "stop"
                    elif sig.take_profit_price and float(bar["high"]) >= sig.take_profit_price:
                        hit = "take_profit"
                else:
                    if float(bar["high"]) >= sig.stop_price:
                        hit = "stop"
                    elif sig.take_profit_price and float(bar["low"]) <= sig.take_profit_price:
                        hit = "take_profit"
                if hit is not None:
                    await self._submit_exit(hit, now)

            # entries only when allowed, flat, and nothing unresolved
            if (self.entries_allowed and self._plan is None
                    and not self.om.has_unresolved_orders
                    and self.private_stream_ready(now)
                    and len(self.candles) > self.strategy.warmup_bars):
                df = self.strategy.prepare(self.candles)
                sig = self.strategy.signal(df, len(df) - 1)
                if sig is not None:
                    self.signals += 1
                    await self._submit_entry(sig, now)

            if self._bars % self.reconcile_every_bars == 0 or self.om.has_unresolved_orders:
                await self._reconcile()

        await self._reconcile()
        return self._report()

    async def _reconcile(self) -> None:
        try:
            resolved = await self.reconciler.resolve_unknown_orders()
        except Exception as exc:  # noqa: BLE001 — reconciliation errors must not crash the loop
            logger.error("live reconciliation failed: %s", exc)
            return
        for coid in resolved:
            parked = self._parked_entries.pop(coid, None)
            if parked is None:
                self._resolve_pending_exit(coid)
                continue
            order = self.om.orders[coid]
            if order.state in _EXIT_ACCEPTED_STATES and self._plan is None:
                self._plan, self._entry_bar_ts = parked
        await self._reconcile_positions()

    async def _reconcile_positions(self) -> None:
        """Position-level reconciliation: the venue is truth. Only runs in a
        SETTLED state (no orders in flight), so transient order-flight gaps never
        false-trigger. A persistent divergence — a venue position we don't track,
        or a position we believe we hold that's vanished (external liquidation) —
        counts a mismatch and FAILS CLOSED (reduce-only) until a clean pass
        clears it. This is the halt the mismatch counter was reported for but
        nothing ever triggered."""
        if (self.om.has_unresolved_orders or self._parked_entries
                or self._pending_exit_orders):
            return  # in flight — not a settled state to judge the venue against
        try:
            account = await self.accounts.account_state()
        except Exception as exc:  # noqa: BLE001 — a failed read must not crash the loop
            logger.error("position reconciliation read failed: %s", exc)
            return
        expected = self._plan is not None          # we believe we hold a position
        actual = account.open_positions > 0        # the venue's truth
        if expected != actual:
            self.recon_mismatches += 1
            self._reconciliation_halt = True
            logger.error(
                "position mismatch: internal plan=%s vs venue positions=%d — "
                "FAIL CLOSED (reduce-only) until a clean pass",
                expected, account.open_positions,
            )
        elif self._reconciliation_halt:
            self._reconciliation_halt = False       # settled + agreeing → re-open
            logger.info("position reconciliation clean — entries re-enabled")

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
        if order.state in _EXIT_ACCEPTED_STATES:
            self._clear_exit_plan()
            return
        if order.state in _EXIT_RETRYABLE_STATES:
            self._pending_exit_orders.pop(base_key, None)
            self._exit_retry_attempts[base_key] = self._exit_retry_attempts.get(base_key, 0) + 1

    def _report(self) -> RunReport:
        return RunReport(
            mode=self.settings.trading_mode.value, symbol=self.symbol,
            strategy_id=self.strategy.strategy_id, bars_processed=self._bars,
            signals_generated=self.signals, orders_submitted=self.orders_submitted,
            fills=0, fees_usd=0.0, realized_pnl_usd=0.0, unrealized_pnl_usd=0.0,
            max_drawdown_pct=0.0, risk_rejects=self.risk_rejects,
            sizing_skips=self.sizing_skips, shadow_approved=0, shadow_rejected=0,
            reconciliation_mismatches=self.recon_mismatches,
            final_equity_usd=0.0,
        )
