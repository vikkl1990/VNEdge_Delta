"""Pre-trade risk gateway.

Every order intent — from any strategy, in any mode — passes through
:meth:`PreTradeRiskGateway.evaluate` before it may reach an executor. The
gateway runs an ordered list of checks and returns an explainable decision:
approved, or rejected with every failed check named. There is no bypass.

Design notes:
- Checks are pure functions of (intent, account, market) snapshots, so the
  gateway is trivially unit-testable and produces identical decisions in
  backtest, paper, and live modes.
- ALL checks run even after the first failure, so the decision log shows the
  complete picture (an order failing 4 checks is a different situation from
  one failing 1).
- Reduce-only orders skip entry-quality gates (spread, funding, exposure):
  getting OUT of risk must never be blocked by filters designed to stop us
  getting INTO risk. Public-feed health/staleness is recorded as an exit
  warning, not a hard reject; order-mechanics validity checks still apply.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from vnedge.config.risk_config import RiskConfig
from vnedge.risk.kill_switch import KillSwitch

logger = logging.getLogger(__name__)

#: Time-in-force values the live execution adapter understands.
#: "PO" = post-only (maker-or-cancel); the adapter maps it to ccxt's
#: unified ``postOnly`` param rather than ``timeInForce``.
ALLOWED_TIME_IN_FORCE = ("GTC", "IOC", "FOK", "PO")
ALLOWED_BRACKET_TRIGGER_METHODS = ("mark_price", "last_traded_price", "spot_price")


@dataclass(frozen=True)
class ServerSideBracket:
    """Exchange-resident stop/target contract attached atomically to an entry."""

    stop_loss_trigger_price: float
    take_profit_trigger_price: float
    stop_loss_limit_price: float | None = None
    take_profit_limit_price: float | None = None
    trigger_method: str = "mark_price"

    def __post_init__(self) -> None:
        prices = {
            "stop_loss_trigger_price": self.stop_loss_trigger_price,
            "take_profit_trigger_price": self.take_profit_trigger_price,
            "stop_loss_limit_price": self.stop_loss_limit_price,
            "take_profit_limit_price": self.take_profit_limit_price,
        }
        invalid = [name for name, value in prices.items() if value is not None and value <= 0]
        if invalid:
            raise ValueError(f"server-side bracket prices must be positive: {', '.join(invalid)}")
        if self.stop_loss_trigger_price == self.take_profit_trigger_price:
            raise ValueError("server-side bracket stop and target must differ")
        if self.trigger_method not in ALLOWED_BRACKET_TRIGGER_METHODS:
            raise ValueError(
                f"invalid bracket trigger method {self.trigger_method!r} "
                f"(allowed: {', '.join(ALLOWED_BRACKET_TRIGGER_METHODS)})"
            )

    def validate_for(self, side: str, reference_price: float | None = None) -> None:
        if side not in {"long", "short"}:
            raise ValueError(f"invalid bracket side: {side}")
        stop = self.stop_loss_trigger_price
        target = self.take_profit_trigger_price
        if side == "long":
            valid = stop < target and (reference_price is None or stop < reference_price < target)
        else:
            valid = target < stop and (reference_price is None or target < reference_price < stop)
        if not valid:
            anchor = "unknown entry" if reference_price is None else f"entry {reference_price}"
            raise ValueError(
                f"invalid {side} bracket geometry around {anchor}: stop={stop}, target={target}"
            )


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    side: str  # "long" | "short"
    quantity: float
    notional_usd: float
    leverage: float
    reduce_only: bool = False
    strategy_id: str = "unknown"
    order_type: str = "market"  # "market" | "limit"
    limit_price: float | None = None
    # Time-in-force for the live execution adapter: "GTC" | "IOC" | "FOK" |
    # "PO" (post-only). None = venue default. NOTE: nothing sets this yet —
    # it is live-phase preparation only; the paper/simulated venue maps
    # intents field-by-field and ignores it harmlessly.
    time_in_force: str | None = None
    server_side_bracket: ServerSideBracket | None = None

    def __post_init__(self) -> None:
        if self.time_in_force is not None and self.time_in_force not in ALLOWED_TIME_IN_FORCE:
            raise ValueError(
                f"invalid time_in_force {self.time_in_force!r} "
                f"(allowed: {', '.join(ALLOWED_TIME_IN_FORCE)}, or None for venue default)"
            )
        if self.server_side_bracket is not None:
            if self.reduce_only:
                raise ValueError("reduce-only exits cannot create a new server-side bracket")
            self.server_side_bracket.validate_for(self.side, self.limit_price)


@dataclass(frozen=True)
class AccountState:
    equity_usd: float
    daily_pnl_usd: float  # realized + unrealized, resets at UTC midnight
    peak_equity_usd: float
    open_positions: int
    exposure_by_symbol_usd: dict[str, float] = field(default_factory=dict)
    total_exposure_usd: float = 0.0
    consecutive_losses: int = 0  # closed losing trades since the last winner


@dataclass(frozen=True)
class MarketState:
    symbol: str
    last_update: datetime
    spread_bps: float
    estimated_slippage_bps: float
    # Funding rate for the next interval, signed from the LONG side's
    # perspective (positive = longs pay shorts).
    funding_rate: float
    exchange_healthy: bool


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    checked_at: datetime
    intent: OrderIntent
    failed_checks: tuple[str, ...] = ()
    passed_checks: tuple[str, ...] = ()
    warning_checks: tuple[str, ...] = ()

    @property
    def explanation(self) -> str:
        if self.approved:
            if self.warning_checks:
                return (
                    f"APPROVED_WITH_WARNINGS ({len(self.passed_checks)} checks passed; "
                    f"warnings: {'; '.join(self.warning_checks)})"
                )
            return f"APPROVED ({len(self.passed_checks)} checks passed)"
        return "REJECTED: " + "; ".join(self.failed_checks)


class PreTradeRiskGateway:
    def __init__(self, config: RiskConfig, kill_switch: KillSwitch) -> None:
        self._config = config
        self._kill_switch = kill_switch

    @property
    def kill_switch(self) -> KillSwitch:
        return self._kill_switch

    def evaluate(
        self,
        intent: OrderIntent,
        account: AccountState,
        market: MarketState,
        now: datetime | None = None,
    ) -> RiskDecision:
        now = now or datetime.now(UTC)
        cfg = self._config
        failed: list[str] = []
        passed: list[str] = []
        warnings: list[str] = []

        def check(name: str, ok: bool, detail: str = "") -> None:
            if ok:
                passed.append(name)
            else:
                failed.append(f"{name}: {detail}" if detail else name)

        def check_exit_warning(name: str, ok: bool, detail: str = "") -> None:
            if ok:
                passed.append(name)
            elif intent.reduce_only:
                warnings.append(f"{name}: {detail}" if detail else name)
            else:
                failed.append(f"{name}: {detail}" if detail else name)

        # --- Always-on checks (apply to entries AND exits) -------------------
        check_exit_warning("exchange_health", market.exchange_healthy, "exchange unhealthy/degraded")

        staleness = (now - market.last_update).total_seconds()
        check_exit_warning(
            "data_freshness",
            staleness <= cfg.max_data_staleness_seconds,
            f"data {staleness:.1f}s old (max {cfg.max_data_staleness_seconds}s)",
        )
        check("symbol_match", market.symbol == intent.symbol,
              f"market data for {market.symbol}, order for {intent.symbol}")
        check("quantity_valid", intent.quantity > 0, f"qty={intent.quantity}")
        check(
            "order_type_valid",
            intent.order_type in ("market", "limit"),
            f"unsupported order type '{intent.order_type}'",
        )
        if intent.order_type == "limit":
            check(
                "limit_price_valid",
                intent.limit_price is not None and intent.limit_price > 0,
                f"limit order without valid price ({intent.limit_price})",
            )
        check(
            "leverage_cap",
            intent.leverage <= cfg.max_leverage_per_position,
            f"{intent.leverage:.1f}x > cap {cfg.max_leverage_per_position}x",
        )

        # --- Entry-only checks ------------------------------------------------
        if not intent.reduce_only:
            # Kill switch = exits only. It must never block the reduce-only
            # flatten orders that tripping it is supposed to trigger.
            check(
                "kill_switch",
                not self._kill_switch.is_active,
                f"active ({self._kill_switch.reason})",
            )
            check(
                "min_equity",
                account.equity_usd >= cfg.min_account_equity_usd,
                f"equity ${account.equity_usd:.2f} < ${cfg.min_account_equity_usd:.2f}",
            )
            # Two-leg limit: the tighter of fixed-USD and %-of-equity wins.
            # The halt can be disabled (PAPER experiments only — defaults on, so
            # live and every other lane keep it); when off the check is a no-op
            # instead of a bypass, so the gateway still evaluates every order.
            if cfg.daily_loss_halt_enabled:
                daily_limit = min(
                    cfg.max_daily_loss_usd,
                    cfg.max_daily_loss_pct / 100.0 * account.peak_equity_usd,
                )
                check(
                    "daily_loss_limit",
                    account.daily_pnl_usd > -daily_limit,
                    f"daily pnl ${account.daily_pnl_usd:.2f} breaches -${daily_limit:.2f} "
                    f"(min of ${cfg.max_daily_loss_usd:.2f} fixed, "
                    f"{cfg.max_daily_loss_pct}% of peak equity)",
                )
            drawdown_pct = (
                (account.peak_equity_usd - account.equity_usd)
                / account.peak_equity_usd * 100.0
                if account.peak_equity_usd > 0
                else 0.0
            )
            check(
                "max_drawdown",
                drawdown_pct < cfg.max_drawdown_pct,
                f"drawdown {drawdown_pct:.1f}% >= {cfg.max_drawdown_pct}%",
            )
            check(
                "max_open_positions",
                account.open_positions < cfg.max_open_positions,
                f"{account.open_positions} open (max {cfg.max_open_positions})",
            )
            check(
                "consecutive_losses",
                account.consecutive_losses < cfg.max_consecutive_losses,
                f"{account.consecutive_losses} consecutive losses "
                f"(max {cfg.max_consecutive_losses}) — manual review required",
            )
            check(
                "slippage",
                market.estimated_slippage_bps <= cfg.max_slippage_bps,
                f"{market.estimated_slippage_bps:.1f}bps > {cfg.max_slippage_bps}bps",
            )
            symbol_exposure = (
                account.exposure_by_symbol_usd.get(intent.symbol, 0.0) + intent.notional_usd
            )
            check(
                "symbol_exposure",
                symbol_exposure <= cfg.max_exposure_per_symbol_usd,
                f"${symbol_exposure:.2f} > ${cfg.max_exposure_per_symbol_usd:.2f}",
            )
            total_exposure = account.total_exposure_usd + intent.notional_usd
            check(
                "total_exposure",
                total_exposure <= cfg.max_total_exposure_usd,
                f"${total_exposure:.2f} > ${cfg.max_total_exposure_usd:.2f}",
            )
            effective_leverage = (
                total_exposure / account.equity_usd if account.equity_usd > 0 else float("inf")
            )
            check(
                "effective_account_leverage",
                effective_leverage <= cfg.max_effective_account_leverage,
                f"{effective_leverage:.2f}x > {cfg.max_effective_account_leverage}x",
            )
            check(
                "spread",
                market.spread_bps <= cfg.max_spread_bps,
                f"{market.spread_bps:.1f}bps > {cfg.max_spread_bps}bps",
            )
            # Only reject when funding is AGAINST the position; earning
            # funding is not a risk.
            paying_funding = (
                market.funding_rate if intent.side == "long" else -market.funding_rate
            )
            check(
                "funding",
                paying_funding <= cfg.max_abs_funding_rate,
                f"paying {paying_funding:.4%}/interval > {cfg.max_abs_funding_rate:.4%}",
            )

        decision = RiskDecision(
            approved=not failed,
            checked_at=now,
            intent=intent,
            failed_checks=tuple(failed),
            passed_checks=tuple(passed),
            warning_checks=tuple(warnings),
        )
        log = logger.info if decision.approved and not warnings else logger.warning
        log(
            "risk_decision strategy=%s symbol=%s side=%s reduce_only=%s -> %s",
            intent.strategy_id, intent.symbol, intent.side, intent.reduce_only,
            decision.explanation,
        )
        return decision
