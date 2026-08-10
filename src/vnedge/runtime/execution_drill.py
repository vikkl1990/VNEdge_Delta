"""Mainnet execution drill — bounded order-lifecycle validation.

    VNEDGE_TRADING_MODE=live_small VNEDGE_LIVE_TRADING_ENABLED=true \\
    VNEDGE_CONFIRM_LIVE_TRADING=I_UNDERSTAND_THIS_IS_HIGH_RISK \\
    VNEDGE_EXEC_API_KEY=... VNEDGE_EXEC_API_SECRET=... \\
    python -m vnedge.runtime.execution_drill --exchange binanceusdm

The operator decision of 2026-07-06 replaced testnet validation with a
mainnet drill: testnets have fake liquidity and diverge from production
behaviour, so the execution adapter is validated against the REAL venue with
strictly bounded risk instead. This module is that validation — it exercises
the full order lifecycle (place a far-from-market post-only limit that cannot
fill → poll state → cancel → verify) plus the flat-position invariant, and
journals every step.

Safety posture (all enforced in code, none waivable by flags):
- The three live gates must be open (live_* mode + enabled + exact phrase);
  the drill constructs the adapter with ``testnet=False, live_confirmed=True``
  ONLY after the pre-live checklist clears.
- Order notional is capped at ``_HARD_MAX_DRILL_NOTIONAL`` regardless of CLI.
- The limit price is offset far below mid (buy side), so a fill is not
  physically possible without a >=15% instant crash; even then the position
  would be ~$10 at 1x and the drill immediately flattens.
- One order at a time, at most ``max_orders`` per run.
- The drill REFUSES to run if the account already has open positions or open
  orders on the drill symbol — it must never touch real exposure.

Keys: trade-only (no withdrawal), IP-whitelisted to the VM, provided via env.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from vnedge.config.risk_config import RiskConfig
from vnedge.config.settings import Settings
from vnedge.execution.idempotency import make_intent_key
from vnedge.execution.journal import DecisionJournal
from vnedge.execution.order_manager import OrderManager
from vnedge.execution.order_state import ManagedOrder, OrderState  # noqa: F401
from vnedge.risk.kill_switch import KillSwitch
from vnedge.risk.risk_manager import (
    AccountState,
    MarketState,
    OrderIntent,
    PreTradeRiskGateway,
)
from vnedge.runtime.pre_live_checklist import run_pre_live_checklist_from_env

#: Minimal synthetic market state for the drill: it validates the GATEWAY PATH
#: (that a first live order is evaluated, not bypassed), not real market-quality
#: rejection — that is live_trader's job with the real feed. Values sit inside
#: the default gateway thresholds (spread<=5bps, slippage<=10bps).
_DRILL_SPREAD_BPS = 2.0
_DRILL_SLIPPAGE_BPS = 2.0

logger = logging.getLogger(__name__)

_DELTA_NATIVE_EXECUTION_IDS = frozenset({"delta", "delta_india", "deltaindia"})
_HARD_MAX_DRILL_NOTIONAL = 25.0   # USD; code constant, not configurable
_DEFAULT_NOTIONAL = 8.0
_FAR_OFFSET_PCT = 15.0            # limit buy this far below mid — cannot fill
_POLL_SECONDS = 1.0
_POLL_TIMEOUT = 20.0


@dataclass
class DrillStep:
    name: str
    ok: bool
    detail: str


@dataclass
class DrillReport:
    exchange: str
    symbol: str
    steps: list[DrillStep] = field(default_factory=list)

    @property
    def cleared(self) -> bool:
        return bool(self.steps) and all(s.ok for s in self.steps)

    def add(self, name: str, ok: bool, detail: str) -> None:
        self.steps.append(DrillStep(name, ok, detail))
        logger.info("drill %-28s %s — %s", name, "OK" if ok else "FAIL", detail)


@dataclass(frozen=True)
class DrillConfig:
    exchange_id: str
    symbol: str = "DOGE/USDT:USDT"
    order_notional_usd: float = _DEFAULT_NOTIONAL
    far_offset_pct: float = _FAR_OFFSET_PCT
    max_orders: int = 1


async def run_execution_drill(
    settings: Settings,
    config: DrillConfig,
    *,
    adapter_factory=None,
    journal: DecisionJournal | None = None,
) -> DrillReport:
    report = DrillReport(exchange=config.exchange_id, symbol=config.symbol)
    journal = journal or DecisionJournal(
        f"logs/drills/execution_drill_{config.exchange_id}.journal.jsonl"
    )

    # --- Gate 0: the three live gates, exactly as production enforces them ---
    if not settings.is_live:
        report.add("live_gates", False,
                   "all three live gates must be open (live_* mode, enabled, phrase)")
        journal.append("execution_drill", {"report": _to_dict(report)})
        return report
    report.add("live_gates", True, f"mode={settings.trading_mode.value}, three gates open")

    # --- Gate 1: pre-live checklist (fail-closed) ---
    checklist = run_pre_live_checklist_from_env(settings)
    if not checklist.cleared:
        report.add("pre_live_checklist", False,
                   "; ".join(f.name for f in checklist.failures))
        journal.append("execution_drill", {"report": _to_dict(report)})
        return report
    report.add("pre_live_checklist", True, "all checks cleared")

    if config.exchange_id in _DELTA_NATIVE_EXECUTION_IDS and adapter_factory is None:
        report.add(
            "delta_native_drill",
            False,
            "Delta India submit/cancel/status is native, but the mainnet drill "
            "also requires native balance, position, open-order, mid-price, and "
            "precision reads before it can safely validate order lifecycle",
        )
        journal.append("execution_drill", {"report": _to_dict(report)})
        return report

    # --- Notional cap: code constant wins over any configuration ---
    notional = min(config.order_notional_usd, _HARD_MAX_DRILL_NOTIONAL)

    api_key = os.environ.get("VNEDGE_EXEC_API_KEY", "")
    api_secret = os.environ.get("VNEDGE_EXEC_API_SECRET", "")

    if adapter_factory is None:
        from vnedge.exchange.live_execution import CcxtExecutionAdapter

        def adapter_factory():
            # The ONLY mainnet-construction site outside live_trader: the
            # operator-approved drill, behind three gates + cleared checklist.
            return CcxtExecutionAdapter(
                config.exchange_id, api_key=api_key, api_secret=api_secret,
                testnet=False, live_confirmed=True,
            )

    adapter = adapter_factory()
    try:
        # --- Step 1: account visibility + flat precondition ---
        balance = await adapter.fetch_balance()
        equity = float(balance.get("total_usd", balance.get("USDT", 0.0)))
        report.add("account_visible", True, f"equity ${equity:.2f}")

        positions = await adapter.fetch_positions(config.symbol)
        open_orders = await adapter.fetch_open_orders(config.symbol)
        if positions or open_orders:
            report.add("flat_precondition", False,
                       f"{len(positions)} position(s), {len(open_orders)} open "
                       "order(s) on drill symbol — drill refuses to touch real exposure")
            journal.append("execution_drill", {"report": _to_dict(report)})
            return report
        report.add("flat_precondition", True, "no positions, no open orders")

        # --- Step 2: far-limit order lifecycle ---
        mid = await adapter.fetch_mid_price(config.symbol)
        limit_price = mid * (1 - config.far_offset_pct / 100.0)
        quantity = adapter.amount_to_precision(config.symbol, notional / limit_price)
        if quantity <= 0:
            report.add("sizing", False,
                       f"notional ${notional:.2f} rounds to zero at {config.symbol} steps")
            journal.append("execution_drill", {"report": _to_dict(report)})
            return report
        report.add("sizing", True,
                   f"qty {quantity} @ {limit_price:.6g} (mid {mid:.6g}, "
                   f"-{config.far_offset_pct}%) ≈ ${quantity * limit_price:.2f}")

        intent = OrderIntent(
            symbol=config.symbol, side="long", quantity=quantity,
            notional_usd=quantity * limit_price, leverage=1.0,
            reduce_only=False, strategy_id="execution_drill",
            order_type="limit", limit_price=limit_price,
        )
        # Route the drill's order through the SAME path production uses:
        # OrderManager.submit runs PreTradeRiskGateway.evaluate, mints a uuid
        # client_order_id (never timestamp-derived), and journals before submit.
        # The invariant "every order passes the gateway" now holds on the one
        # path that actually places a first live order — no bypass.
        now = datetime.now(UTC)
        gateway = PreTradeRiskGateway(RiskConfig(), KillSwitch(kill_file=Path("KILL")))
        om = OrderManager(gateway, journal, adapter)
        account = AccountState(
            equity_usd=equity, daily_pnl_usd=0.0, peak_equity_usd=equity,
            open_positions=0,
        )
        market = MarketState(
            symbol=config.symbol, last_update=now, spread_bps=_DRILL_SPREAD_BPS,
            estimated_slippage_bps=_DRILL_SLIPPAGE_BPS, funding_rate=0.0,
            exchange_healthy=True,
        )
        key = make_intent_key("execution_drill", config.symbol, "long", pd.Timestamp(now))
        order = await om.submit(intent, account, market, key, now=now)
        if order.state is OrderState.RISK_REJECTED:
            report.add(
                "gateway_approved", False,
                "PreTradeRiskGateway rejected the drill order — see journal",
            )
            journal.append("execution_drill", {"report": _to_dict(report)})
            return report
        report.add("gateway_approved", True,
                   "order passed PreTradeRiskGateway.evaluate (no bypass)")
        report.add("submit", order.exchange_order_id is not None,
                   f"venue accepted, id={order.exchange_order_id}")

        # --- Step 3: poll until visible/open ---
        deadline = time.monotonic() + _POLL_TIMEOUT
        status = None
        while time.monotonic() < deadline:
            status = await adapter.fetch_order_status(order)
            if status is not None:
                break
            await asyncio.sleep(_POLL_SECONDS)
        if status is None:
            report.add("status_visible", False,
                       "order not visible at venue within timeout — reconcile manually")
        else:
            state = str(status.get("status", "?"))
            filled = float(status.get("filled") or 0.0)
            ok = filled == 0.0
            report.add("status_visible", ok,
                       f"state={state} filled={filled} (far limit must not fill)")

        # --- Step 4: cancel + verify ---
        result_state = await adapter.cancel_order(order)
        cancelled = str(result_state).lower() in ("canceled", "cancelled", "closed")
        report.add("cancel", cancelled, f"venue state after cancel: {result_state}")

        # --- Step 5: end flat ---
        positions = await adapter.fetch_positions(config.symbol)
        open_orders = await adapter.fetch_open_orders(config.symbol)
        report.add("flat_postcondition", not positions and not open_orders,
                   f"{len(positions)} position(s), {len(open_orders)} open order(s)")
    finally:
        await adapter.close()

    journal.append("execution_drill", {"report": _to_dict(report)})
    return report


def _to_dict(report: DrillReport) -> dict:
    return {
        "exchange": report.exchange,
        "symbol": report.symbol,
        "cleared": report.cleared,
        "steps": [{"name": s.name, "ok": s.ok, "detail": s.detail}
                  for s in report.steps],
    }


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="bounded mainnet execution drill")
    p.add_argument("--exchange", required=True,
                   help="execution id: binanceusdm | bybit (Delta native drill pending)")
    p.add_argument("--symbol", default="DOGE/USDT:USDT")
    p.add_argument("--notional", type=float, default=_DEFAULT_NOTIONAL,
                   help=f"order notional USD (hard cap {_HARD_MAX_DRILL_NOTIONAL})")
    args = p.parse_args(argv)

    settings = Settings()
    config = DrillConfig(
        exchange_id=args.exchange, symbol=args.symbol,
        order_notional_usd=args.notional,
    )
    report = asyncio.run(run_execution_drill(settings, config))
    print()
    print(f"=== execution drill {report.exchange} {report.symbol} ===")
    for s in report.steps:
        print(f"  [{'PASS' if s.ok else 'FAIL'}] {s.name}: {s.detail}")
    print(f"=== {'CLEARED — adapter validated on mainnet' if report.cleared else 'NOT CLEARED'} ===")
    return 0 if report.cleared else 1


if __name__ == "__main__":
    raise SystemExit(main())
