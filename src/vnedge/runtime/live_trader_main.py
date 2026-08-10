"""Live-trader entrypoint — the ONLY runtime wiring that can run a
``LiveTraderSession`` against a real venue.

Its whole job is to be a fail-closed gate before any live client is ever built:

  1. the three live gates (``settings.is_live``: a live_* mode AND
     ``live_trading_enabled`` AND the exact confirmation phrase);
  2. the fail-closed pre-live checklist (kill switch clear, risk config frozen,
     mode ladder attested, reconciliation clean, journal writable, ...);
  3. mainnet trade-only credentials present.

If ANY of those is not satisfied it logs why and returns non-zero WITHOUT
constructing a single live client — wiring this entrypoint therefore does not
make accidental live trading any easier: the operator still has to open every
gate, install keys, and attest the ladder. Only when all pass does it wire the
proven live components (execution adapter, read-only account provider, live
feed, gateway, order manager, reconciler) and run the session.

Live dependency constructors are injected (``*_factory``) so the gate chain is
testable end-to-end without ever touching a real venue.

Run (only meaningful once the operator has opened the gates + installed keys):
  python -m vnedge.runtime.live_trader_main --exchange delta_india \
      --symbol BTC/USD:USD --timeframe 1h --strategy funding_mean_reversion_v1
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from dataclasses import dataclass

import pandas as pd

from vnedge.config.settings import Settings
from vnedge.data.schemas import normalize_candles
from vnedge.exchange.venue_specs import venue_symbol_limits
from vnedge.execution.journal import DecisionJournal
from vnedge.execution.live_reconciliation import LiveReconciler
from vnedge.execution.order_manager import OrderManager
from vnedge.risk.kill_switch import KillSwitch
from vnedge.risk.risk_manager import PreTradeRiskGateway
from vnedge.runtime.live_trader import LiveTraderSession
from vnedge.runtime.pre_live_checklist import run_pre_live_checklist_from_env

logger = logging.getLogger(__name__)

#: exit codes — non-zero means "refused, no live client built".
_EXIT_OK = 0
_EXIT_GATES = 10
_EXIT_CHECKLIST = 11
_EXIT_CREDENTIALS = 12

_WARMUP_BARS = 500


@dataclass(frozen=True)
class LiveTraderRunConfig:
    exchange: str
    symbol: str
    timeframe: str = "1h"
    strategy_id: str = "funding_mean_reversion_v1"


def _credentials_present() -> bool:
    return bool(os.environ.get("VNEDGE_EXEC_API_KEY")) and bool(
        os.environ.get("VNEDGE_EXEC_API_SECRET")
    )


async def _default_warmup(config: LiveTraderRunConfig, bars: int) -> pd.DataFrame:
    from vnedge.data.ccxt_client import CcxtPublicClient

    async with CcxtPublicClient(config.exchange) as rest:
        raw = await rest.fetch_candles(config.symbol, config.timeframe, limit=bars)
    return normalize_candles(raw)


def _default_adapter(config: LiveTraderRunConfig):
    # The mainnet execution client. Constructed ONLY after the gate chain passes.
    from vnedge.exchange.live_execution import CcxtExecutionAdapter

    return CcxtExecutionAdapter(
        config.exchange,
        api_key=os.environ["VNEDGE_EXEC_API_KEY"],
        api_secret=os.environ["VNEDGE_EXEC_API_SECRET"],
        testnet=False,
        live_confirmed=True,
    )


def _default_account(config: LiveTraderRunConfig):
    from vnedge.exchange.readonly_account import CcxtReadOnlyAccountProvider

    return CcxtReadOnlyAccountProvider(exchange_id=config.exchange)


def _default_feed(config: LiveTraderRunConfig):
    from vnedge.exchange.live_feed import LiveMarketFeed

    return LiveMarketFeed(
        config.exchange, symbol=config.symbol, timeframe=config.timeframe
    )


def _default_strategy(strategy_id: str):
    from vnedge.strategy.strategy_registry import get_strategy_class

    return get_strategy_class(strategy_id)()


async def run_live_trader(
    settings: Settings,
    config: LiveTraderRunConfig,
    *,
    adapter_factory=None,
    account_factory=None,
    feed_factory=None,
    strategy_factory=None,
    warmup_loader=None,
    max_bars: int | None = None,
) -> int:
    """Enforce the full gate chain, then (only if it clears) wire + run the
    live session. Returns 0 on a clean run, a non-zero code if refused."""
    # --- Gate 1: three live gates -------------------------------------------------
    if not settings.is_live:
        logger.error(
            "REFUSED: three live gates not open (mode=%s, enabled=%s, phrase_ok=%s). "
            "No live client constructed.",
            settings.trading_mode.value, settings.live_trading_enabled,
            settings.confirm_live_trading != "",
        )
        return _EXIT_GATES

    # --- Gate 2: fail-closed pre-live checklist -----------------------------------
    checklist = run_pre_live_checklist_from_env(settings)
    if not checklist.cleared:
        logger.error(
            "REFUSED: pre-live checklist not cleared (%s). No live client constructed.",
            ", ".join(f.name for f in checklist.failures),
        )
        return _EXIT_CHECKLIST

    # --- Gate 3: mainnet trade-only credentials -----------------------------------
    if not _credentials_present():
        logger.error(
            "REFUSED: VNEDGE_EXEC_API_KEY/SECRET not set. No live client constructed."
        )
        return _EXIT_CREDENTIALS

    # All gates open — NOW it is safe to build live clients.
    logger.warning(
        "ALL GATES OPEN — starting LIVE trader on %s %s (%s). This places REAL orders.",
        config.exchange, config.symbol, config.strategy_id,
    )
    adapter = (adapter_factory or _default_adapter)(config)
    account = (account_factory or _default_account)(config)
    feed = (feed_factory or _default_feed)(config)
    strategy = (strategy_factory or _default_strategy)(config.strategy_id)
    history = await (warmup_loader or _default_warmup)(config, _WARMUP_BARS)

    journal = DecisionJournal(f"logs/live/{config.exchange}_{config.strategy_id}.journal.jsonl")
    gateway = PreTradeRiskGateway(settings.risk, KillSwitch(kill_file="KILL"))
    om = OrderManager(gateway, journal, adapter)
    reconciler = LiveReconciler(om, adapter)
    limits = venue_symbol_limits(config.exchange, config.symbol)

    session = LiveTraderSession(
        strategy, feed, history, settings=settings, gateway=gateway,
        order_manager=om, reconciler=reconciler, account_provider=account,
        symbol=config.symbol, limits=limits, pre_live_report=checklist,
    )
    try:
        await feed.start()
        await session.run(max_bars=max_bars)
    finally:
        try:
            await feed.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("feed stop failed: %s", exc)
        try:
            await adapter.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("adapter close failed: %s", exc)
    return _EXIT_OK


def _parse_args(argv=None) -> LiveTraderRunConfig:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--exchange", required=True)
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--timeframe", default="1h")
    ap.add_argument("--strategy", dest="strategy_id", default="funding_mean_reversion_v1")
    a = ap.parse_args(argv)
    return LiveTraderRunConfig(
        exchange=a.exchange, symbol=a.symbol, timeframe=a.timeframe,
        strategy_id=a.strategy_id,
    )


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    config = _parse_args(argv)
    return asyncio.run(run_live_trader(settings, config))


if __name__ == "__main__":
    raise SystemExit(main())
