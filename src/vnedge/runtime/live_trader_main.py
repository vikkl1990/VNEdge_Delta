"""Live-trader entrypoint — the ONLY runtime wiring that can run a
``LiveTraderSession`` against a real venue.

Its whole job is to be a fail-closed gate before any live client is ever built:

  1. the three live gates (``settings.is_live``: a live_* mode AND
     ``live_trading_enabled`` AND the exact confirmation phrase);
  2. a signed, single-use stage authorization bound to this commit/config;
  3. direct Delta REST reconciliation + authenticated private-stream health;
  4. the fail-closed pre-live checklist;
  5. mainnet trade-only credentials present.

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
from pathlib import Path

import pandas as pd

from vnedge.config.settings import Settings
from vnedge.execution.journal import DecisionJournal
from vnedge.execution.live_reconciliation import LiveReconciler
from vnedge.execution.order_manager import OrderManager
from vnedge.risk.kill_switch import KillSwitch
from vnedge.risk.risk_manager import PreTradeRiskGateway
from vnedge.runtime.live_trader import LiveTraderSession
from vnedge.runtime.pre_live_checklist import run_pre_live_checklist
from vnedge.runtime.production_authorization import verify_production_authorization

logger = logging.getLogger(__name__)

#: exit codes — non-zero means "refused, no live client built".
_EXIT_OK = 0
_EXIT_GATES = 10
_EXIT_CHECKLIST = 11
_EXIT_CREDENTIALS = 12
_EXIT_AUTHORIZATION = 13
_EXIT_RECONCILIATION = 14
_EXIT_VENUE = 15

_WARMUP_BARS = 500


@dataclass(frozen=True)
class LiveTraderRunConfig:
    exchange: str
    symbol: str
    timeframe: str = "1h"
    strategy_id: str = "funding_mean_reversion_v1"
    runtime_config_path: str = "configs/production_live.yaml"


def _credentials_present() -> bool:
    return bool(_secret_value("VNEDGE_EXEC_API_KEY")) and bool(
        _secret_value("VNEDGE_EXEC_API_SECRET")
    )


def _secret_value(name: str) -> str:
    """Read a secret from env or a Docker/Kubernetes ``*_FILE`` mount."""

    direct = os.environ.get(name, "").strip()
    if direct:
        return direct
    locator = os.environ.get(f"{name}_FILE", "").strip()
    if not locator:
        return ""
    try:
        return Path(locator).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


async def _default_warmup(config: LiveTraderRunConfig, bars: int) -> pd.DataFrame:
    import time

    from vnedge.data.delta_native_history import fetch_delta_candle_history
    from vnedge.data.schemas import TIMEFRAME_MS

    step_seconds = TIMEFRAME_MS[config.timeframe] // 1000
    end_s = int(time.time())
    start_s = end_s - (bars + 10) * step_seconds
    return await fetch_delta_candle_history(
        config.symbol,
        resolution=config.timeframe,
        start_s=start_s,
        end_s=end_s,
    )


def _default_feed(config: LiveTraderRunConfig):
    from vnedge.exchange.live_feed import create_market_feed

    return create_market_feed(
        "delta_india", symbol=config.symbol, timeframe=config.timeframe
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

    injected_test_path = adapter_factory is not None
    if (
        not injected_test_path
        and config.exchange.lower() not in {"delta", "delta_india", "deltaindia"}
    ):
        logger.error("REFUSED: production runtime is pinned to Delta India")
        return _EXIT_VENUE

    # Credentials are checked before authorization only to provide a precise
    # operator error.  No authenticated exchange client exists at this point.
    if not _credentials_present():
        logger.error(
            "REFUSED: VNEDGE_EXEC_API_KEY/SECRET not set. No live client constructed."
        )
        return _EXIT_CREDENTIALS

    runtime_config = Path(config.runtime_config_path)
    if not injected_test_path and not runtime_config.is_file():
        logger.error("REFUSED: immutable runtime config is missing: %s", runtime_config)
        return _EXIT_AUTHORIZATION
    if injected_test_path:
        from vnedge.runtime.production_authorization import ProductionAuthorizationResult

        legacy_test_attested = os.environ.get("PRE_LIVE_LADDER_ATTESTED") == "1"
        authorization = ProductionAuthorizationResult(
            legacy_test_attested,
            () if legacy_test_attested else ("injected test ladder is not attested",),
        )
    else:
        authorization = verify_production_authorization(
            strategy_id=config.strategy_id,
            symbol=config.symbol,
            target_stage=settings.trading_mode.value,
            config_path=runtime_config,
            consume=False,
        )
    if not authorization.authorized:
        logger.error("REFUSED: signed stage authorization failed: %s", "; ".join(authorization.blockers))
        return _EXIT_CHECKLIST if injected_test_path else _EXIT_AUTHORIZATION

    # Public warmup and product metadata can be fetched before authenticated
    # clients.  A closed-candle reference is also needed to translate Delta's
    # integer contracts into risk-sizer base units.
    history = await (warmup_loader or _default_warmup)(config, _WARMUP_BARS)
    if history.empty:
        logger.error("REFUSED: no closed-candle warmup data")
        return _EXIT_RECONCILIATION

    journal_path = Path(
        os.environ.get(
            "DECISION_JOURNAL",
            f"logs/live/{config.exchange}_{config.strategy_id}.journal.jsonl",
        )
    )
    journal = DecisionJournal(journal_path, hash_chain=True)
    if not journal.available:
        logger.error("REFUSED: live decision journal integrity check failed")
        return _EXIT_RECONCILIATION
    kill_switch = KillSwitch(kill_file=Path(settings.kill_switch_file))
    gateway = PreTradeRiskGateway(settings.risk, kill_switch)

    safety_wrapper = None
    private_stream = None
    private_stop = asyncio.Event()
    private_task = heartbeat_task = None

    # Injectable factories remain for deterministic tests.  The production
    # branch below is Delta-native and cannot fall back to CCXT execution.
    if adapter_factory is not None:
        adapter = adapter_factory(config)
        account = account_factory(config) if account_factory else None
        limits = __import__(
            "vnedge.exchange.venue_specs", fromlist=["venue_symbol_limits"]
        ).venue_symbol_limits(config.exchange, config.symbol)
    else:
        from vnedge.exchange.delta_account import DeltaReadOnlyAccountProvider
        from vnedge.exchange.delta_contracts import (
            delta_symbol_limits,
            fetch_india_contract_spec,
        )
        from vnedge.exchange.delta_execution import DeltaRestExecutionAdapter
        from vnedge.exchange.delta_execution_safety import (
            DeltaAuthenticatedSafetyClient,
            DeltaDeadmanConfig,
            DeltaExecutionSafetyWrapper,
        )

        spec = await asyncio.to_thread(fetch_india_contract_spec, config.symbol)
        if spec.product_id is None:
            logger.error("REFUSED: Delta product has no product id")
            return _EXIT_RECONCILIATION
        product_ids = {config.symbol: spec.product_id}
        specs = {config.symbol: spec}
        api_key = _secret_value("VNEDGE_EXEC_API_KEY")
        api_secret = _secret_value("VNEDGE_EXEC_API_SECRET")
        safety_client = DeltaAuthenticatedSafetyClient(
            api_key=api_key,
            api_secret=api_secret,
        )
        native = DeltaRestExecutionAdapter(
            api_key=api_key,
            api_secret=api_secret,
            dry_run=False,
            live_confirmed=True,
            product_ids=product_ids,
            contract_specs=specs,
            safety_client=safety_client,
        )
        deadman = DeltaDeadmanConfig(
            heartbeat_id=f"vnedge-{config.strategy_id[:12]}-{spec.product_id}",
            product_symbols=(spec.symbol,),
        )
        safety_wrapper = DeltaExecutionSafetyWrapper(
            native,
            config=deadman,
            journal=journal,
        )
        adapter = safety_wrapper
        currency = os.environ.get("VNEDGE_DELTA_ACCOUNT_CURRENCY", "").strip()
        if not currency:
            logger.error("REFUSED: VNEDGE_DELTA_ACCOUNT_CURRENCY is required")
            await adapter.close()
            return _EXIT_CREDENTIALS
        account = DeltaReadOnlyAccountProvider(
            safety_client=safety_client,
            product_ids=product_ids,
            contract_specs=specs,
            base_currency=currency,
        )
        limits = delta_symbol_limits(
            spec,
            reference_price=float(history["close"].iloc[-1]),
        )

    if account is None:
        logger.error("REFUSED: account truth provider is required")
        return _EXIT_RECONCILIATION
    feed = (feed_factory or _default_feed)(config)
    strategy = (strategy_factory or _default_strategy)(config.strategy_id)
    om = OrderManager(gateway, journal, adapter)
    reconciler = LiveReconciler(om, adapter)

    try:
        await feed.start()
        if safety_wrapper is not None:
            from vnedge.exchange.delta_private_stream import DeltaPrivateStream
            from vnedge.exchange.delta_ws import delta_native_symbol

            await safety_wrapper.start()
            heartbeat_task = asyncio.create_task(
                safety_wrapper.run(private_stop), name="delta-deadman"
            )
            private_stream = DeltaPrivateStream(
                api_key=api_key,
                api_secret=api_secret,
                symbols=(delta_native_symbol(config.symbol),),
                order_manager=om,
            )
            private_task = asyncio.create_task(
                private_stream.run_forever(stop_event=private_stop),
                name="delta-private-stream",
            )
            try:
                await _wait_private_stream(private_stream)
                truth = await safety_wrapper.observe_exchange_truth()
            except Exception as exc:  # noqa: BLE001 - fail closed at venue boundary
                logger.error("REFUSED: Delta reconciliation unavailable: %s", exc)
                return _EXIT_RECONCILIATION
            has_unresolved = truth.open_order_count > 0 or truth.nonzero_position_count > 0
            private_health = private_stream.health
        else:
            # Test/injected paths must provide an explicit clean truth marker;
            # production never enters this branch.
            has_unresolved = False
            private_health = None

        checklist = run_pre_live_checklist(
            settings=settings,
            risk_config=settings.risk,
            kill_switch_active=kill_switch.is_active,
            has_unresolved_orders=has_unresolved,
            journal_path=journal_path,
            credentials_present=True,
            lower_rungs_validated=authorization.authorized,
            private_stream_required=safety_wrapper is not None,
            private_stream_connected=(private_health.connected if private_health else None),
            private_stream_age_seconds=(private_health.age_seconds() if private_health else None),
        )
        if not checklist.cleared:
            logger.error(
                "REFUSED: direct pre-live checks failed: %s",
                ", ".join(f.name for f in checklist.failures),
            )
            return _EXIT_CHECKLIST
        consumed = (
            authorization
            if injected_test_path
            else verify_production_authorization(
                strategy_id=config.strategy_id,
                symbol=config.symbol,
                target_stage=settings.trading_mode.value,
                config_path=runtime_config,
                consume=True,
            )
        )
        if not consumed.authorized:
            logger.error("REFUSED: authorization consumption failed: %s", "; ".join(consumed.blockers))
            return _EXIT_AUTHORIZATION

        logger.warning(
            "ALL VERIFIED GATES OPEN — starting LIVE trader on %s %s (%s)",
            config.exchange,
            config.symbol,
            config.strategy_id,
        )
        session = LiveTraderSession(
            strategy, feed, history, settings=settings, gateway=gateway,
            order_manager=om, reconciler=reconciler, account_provider=account,
            symbol=config.symbol, limits=limits, pre_live_report=checklist,
            private_stream_health=private_health,
            require_private_stream=safety_wrapper is not None,
            execution_entry_ready=(
                (lambda: safety_wrapper.entry_ready)
                if safety_wrapper is not None else None
            ),
        )
        await session.run(max_bars=max_bars)
    finally:
        private_stop.set()
        if private_stream is not None:
            await private_stream.close()
        for task in (private_task, heartbeat_task):
            if task is not None:
                task.cancel()
        await asyncio.gather(
            *(task for task in (private_task, heartbeat_task) if task is not None),
            return_exceptions=True,
        )
        try:
            await feed.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("feed stop failed: %s", exc)
        try:
            await adapter.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("adapter close failed: %s", exc)
    return _EXIT_OK


async def _wait_private_stream(stream, timeout_seconds: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        if stream.health.connected and stream.health.age_seconds() <= 5.0:
            return
        await asyncio.sleep(0.05)
    raise RuntimeError(
        f"Delta private stream did not authenticate: {stream.health.snapshot()}"
    )


def _parse_args(argv=None) -> LiveTraderRunConfig:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--exchange", required=True)
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--timeframe", default="1h")
    ap.add_argument("--strategy", dest="strategy_id", default="funding_mean_reversion_v1")
    ap.add_argument("--runtime-config", required=True)
    a = ap.parse_args(argv)
    return LiveTraderRunConfig(
        exchange=a.exchange, symbol=a.symbol, timeframe=a.timeframe,
        strategy_id=a.strategy_id, runtime_config_path=a.runtime_config,
    )


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    config = _parse_args(argv)
    return asyncio.run(run_live_trader(settings, config))


if __name__ == "__main__":
    raise SystemExit(main())
