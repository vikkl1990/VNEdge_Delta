"""Live Delta event recorder with a locked, feature-only research observer.

The recorder remains the source of truth and validates order-book sequencing
before an envelope reaches the event engine. The observer journals features
and publishes telemetry, but live event hypotheses stay disabled until a
deterministic replay proof exists. It has no order, account, or risk-routing
dependency.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from vnedge.exchange.delta_contracts import fetch_india_contract_spec
from vnedge.exchange.delta_event_recorder import (
    PUBLIC_CHANNELS,
    DeltaEventRecorder,
    DeltaEventRecorderConfig,
)
from vnedge.execution.journal import DecisionJournal
from vnedge.scalping.delta_engine.absorption import (
    AbsorptionDetectorConfig,
    AbsorptionInstrumentConfig,
)
from vnedge.scalping.delta_engine.absorption_research import (
    AbsorptionResearchConfig,
    AbsorptionResearchTracker,
)
from vnedge.scalping.delta_engine.confirmed_absorption import (
    ConfirmedAbsorptionConfig,
    ConfirmedAbsorptionReversalScanner,
)
from vnedge.scalping.delta_engine.event_market_truth import (
    EventHigherTimeframeContextService,
)
from vnedge.scalping.delta_engine.event_trigger import (
    DeltaVerifiedEventBridge,
    EventDrivenTriggerLayer,
    EventTriggerConfig,
)
from vnedge.scalping.delta_engine.fee_model import DeltaFeeModel
from vnedge.scalping.delta_engine.signal_generator import SignalGateConfig
from vnedge.scalping.delta_engine.types import (
    SCALPER_MAX_HOLD_SECONDS,
    classify_trade_horizon,
)


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True, separators=(",", ":"), allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    temporary.replace(path)


class DeltaLiveEventResearchObserver:
    """Verified-envelope consumer that can only produce feature evidence."""

    def __init__(
        self,
        *,
        symbols: tuple[str, ...],
        instruments: tuple[AbsorptionInstrumentConfig, ...] = (),
        metadata_errors: tuple[str, ...] = (),
        telemetry_path: Path = Path(
            "research/live_research/delta_event_trigger_telemetry_latest.json"
        ),
        absorption_path: Path = Path(
            "research/live_research/delta_absorption_dashboard_latest.json"
        ),
        journal_path: Path = Path("logs/delta_event_research.jsonl"),
        publish_interval_seconds: float = 1.0,
    ) -> None:
        if publish_interval_seconds <= 0:
            raise ValueError("publish interval must be positive")
        native_symbols = tuple(symbol.upper() for symbol in symbols)
        absorption = AbsorptionDetectorConfig(
            enabled=bool(instruments),
            instruments=instruments,
        )
        journal = DecisionJournal(journal_path)
        fee_model = DeltaFeeModel(default_slippage_bps_per_leg=1.5)
        absorption_research = (
            AbsorptionResearchTracker(
                fee_model,
                instruments,
                # The original 8/12-tick geometry was smaller than one
                # taker round trip on both BTC and ETH.  This accelerated
                # shadow experiment uses cross-market bps geometry whose
                # first target is roughly three times configured costs.
                config=AbsorptionResearchConfig(
                    target_1_bps=45.0,
                    target_2_bps=75.0,
                    stop_bps=20.0,
                    horizon_ms=900_000,
                    entry_timeout_ms=5_000,
                    one_active_per_symbol=True,
                    return_horizons_ms=(
                        1_000,
                        5_000,
                        15_000,
                        30_000,
                        60_000,
                        300_000,
                        900_000,
                    ),
                ),
                journal=journal,
            )
            if instruments
            else None
        )
        shadow_scanners = (
            (
                ConfirmedAbsorptionReversalScanner(
                    fee_model=fee_model,
                    config=ConfirmedAbsorptionConfig(
                        tick_sizes={row.symbol: row.tick_size for row in instruments},
                        stop_bps=20.0,
                        target_bps=45.0,
                        time_stop_seconds=900,
                        minimum_target_cost_multiple=2.5,
                        probability_prior=0.50,
                    ),
                ),
            )
            if instruments
            else ()
        )
        self.trigger = EventDrivenTriggerLayer(
            shadow_scanners,
            config=EventTriggerConfig(
                enabled_symbols=native_symbols,
                absorption=absorption,
                require_htf_context=True,
                require_reference_prices=True,
                require_trade_book_join=True,
                one_active_observation_per_symbol=True,
                observation_lock_ms=900_000,
            ),
            gates=SignalGateConfig(allowed_symbols=native_symbols),
            journal=journal,
            absorption_research=absorption_research,
        )
        self.htf_context = EventHigherTimeframeContextService(
            self.trigger,
            native_symbols,
        )
        self.bridge = DeltaVerifiedEventBridge(
            self.trigger,
            trade_observer=self.htf_context.on_trade,
        )
        self.symbols = native_symbols
        self.primary_absorption_symbol = (
            "ETHUSD" if "ETHUSD" in native_symbols else native_symbols[0]
        )
        self.metadata_errors = metadata_errors
        self.paper_simulation_enabled = absorption_research is not None
        self.telemetry_path = telemetry_path
        self.absorption_path = absorption_path
        self.publish_interval_ns = int(publish_interval_seconds * 1_000_000_000)
        self.last_publish_monotonic_ns = 0
        self.events_observed = 0

    async def consume(self, envelope: Mapping[str, Any]) -> None:
        self.events_observed += 1
        self.bridge.consume(envelope, integrity_verified=True)
        now_ns = time.monotonic_ns()
        if now_ns - self.last_publish_monotonic_ns >= self.publish_interval_ns:
            await self.publish(now_ns=now_ns)

    async def publish(self, *, now_ns: int | None = None) -> None:
        now_ns = now_ns if now_ns is not None else time.monotonic_ns()
        telemetry = {
            **self.trigger.telemetry(now_ns=now_ns),
            "state": "LIVE_SHADOW_OBSERVING",
            "pid": os.getpid(),
            "updated_at": datetime.now(UTC).isoformat(),
            "symbols": list(self.symbols),
            "events_observed": self.events_observed,
            "market_metadata_errors": list(self.metadata_errors),
            "observer_source": "verified_delta_recorder_envelopes",
            "scanner_policy": "post_absorption_confirmation_shadow_capital_locked",
            "enabled_scanners": [scanner.scanner_id for scanner in self.trigger.scanners],
            "paper_simulation": {
                "enabled": self.paper_simulation_enabled,
                "uses_real_orders": False,
                "benchmark": "raw_absorption_detection_v2",
                "qualified_scanner": "event_absorption_confirmed_reversal_v3_shadow",
                "entry": "next_public_trade",
                "target_bps": 45.0,
                "stop_bps": 20.0,
                "vertical_barrier_seconds": 900,
                "trade_horizon": classify_trade_horizon(900).value,
                "scalper_max_hold_seconds": SCALPER_MAX_HOLD_SECONDS,
                "cost_contract": "taker_full_14_8",
                "status": "COLLECTING" if self.paper_simulation_enabled else "UNAVAILABLE",
            },
            "validated_edge": False,
            "higher_timeframe_context": self.htf_context.telemetry(),
        }
        absorption = self.trigger.absorption_dashboard(
            self.primary_absorption_symbol,
            now_ns=now_ns,
        )
        await asyncio.gather(
            asyncio.to_thread(_atomic_json, self.telemetry_path, telemetry),
            asyncio.to_thread(_atomic_json, self.absorption_path, absorption),
        )
        self.last_publish_monotonic_ns = now_ns


async def _instruments(
    symbols: tuple[str, ...],
) -> tuple[tuple[AbsorptionInstrumentConfig, ...], tuple[str, ...]]:
    instruments: list[AbsorptionInstrumentConfig] = []
    errors: list[str] = []
    for symbol in symbols:
        try:
            spec = await asyncio.to_thread(fetch_india_contract_spec, symbol)
            instruments.append(
                AbsorptionInstrumentConfig.from_delta_contract(
                    spec,
                    minimum_aggressive_notional_usd=5_000.0,
                )
            )
        except Exception as exc:  # noqa: BLE001 - missing metadata disables only absorption
            errors.append(f"{symbol}:{type(exc).__name__}:{str(exc)[:200]}")
    return tuple(instruments), tuple(errors)


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    symbols = tuple(value.strip().upper() for value in args.symbols.split(",") if value.strip())
    channels = tuple(value.strip() for value in args.channels.split(",") if value.strip())
    instruments, metadata_errors = await _instruments(symbols)
    observer = DeltaLiveEventResearchObserver(
        symbols=symbols,
        instruments=instruments,
        metadata_errors=metadata_errors,
        telemetry_path=args.telemetry_path,
        absorption_path=args.absorption_path,
        journal_path=args.journal_path,
    )
    await observer.htf_context.seed()
    recorder = DeltaEventRecorder(
        DeltaEventRecorderConfig(
            symbols=symbols,
            channels=channels,
            output_dir=args.output_dir,
            compression=args.compression,
            queue_max=args.queue_max,
            batch_max=args.batch_max,
        ),
        verified_event_observer=observer.consume,
    )
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, recorder.request_stop)
        except NotImplementedError:  # pragma: no cover - non-POSIX fallback
            pass
    await observer.publish()
    htf_task = asyncio.create_task(
        observer.htf_context.run(),
        name="delta-event-htf-context",
    )
    try:
        result = await recorder.run()
    finally:
        htf_task.cancel()
        await asyncio.gather(htf_task, return_exceptions=True)
    await observer.publish()
    return {
        **result,
        "event_research": observer.trigger.telemetry(),
        "market_metadata_errors": list(metadata_errors),
        "order_route": "absent",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="BTCUSD,ETHUSD")
    parser.add_argument("--channels", default=",".join(PUBLIC_CHANNELS))
    parser.add_argument("--output-dir", type=Path, default=Path("data/delta_events"))
    parser.add_argument("--compression", choices=("auto", "gzip", "zstd"), default="auto")
    parser.add_argument("--queue-max", type=int, default=50_000)
    parser.add_argument("--batch-max", type=int, default=1_000)
    parser.add_argument(
        "--telemetry-path",
        type=Path,
        default=Path("research/live_research/delta_event_trigger_telemetry_latest.json"),
    )
    parser.add_argument(
        "--absorption-path",
        type=Path,
        default=Path("research/live_research/delta_absorption_dashboard_latest.json"),
    )
    parser.add_argument(
        "--journal-path",
        type=Path,
        default=Path("logs/delta_event_research.jsonl"),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = asyncio.run(_run(args))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
