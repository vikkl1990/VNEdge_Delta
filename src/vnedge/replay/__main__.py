"""CLI for deterministic, research-only Delta event replay."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from vnedge.execution.journal import DecisionJournal
from vnedge.replay.engine import EventReplayEngine
from vnedge.replay.manifest import load_holdout_manifest
from vnedge.replay.models import HoldoutManifest, ReplayConfig
from vnedge.replay.store import DeltaShardEventStore
from vnedge.scalping.delta_engine.absorption import (
    AbsorptionDetectorConfig,
    AbsorptionInstrumentConfig,
)
from vnedge.scalping.delta_engine.absorption_research import (
    AbsorptionResearchConfig,
    AbsorptionResearchTracker,
)
from vnedge.scalping.delta_engine.event_trigger import (
    AbsorptionReversalScanner,
    EventDrivenTriggerLayer,
    EventTriggerConfig,
    SustainedFlowImbalanceScanner,
)
from vnedge.scalping.delta_engine.fee_model import DeltaFeeModel
from vnedge.scalping.delta_engine.signal_generator import SignalGateConfig


def _timestamp_us(value: str) -> int:
    try:
        numeric = int(value)
    except ValueError:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return int(parsed.timestamp() * 1_000_000)
    return numeric


def _code_version() -> str:
    dirty = subprocess.run(
        ["git", "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    )
    if dirty.stdout.strip():
        # Local-only research must still be reproducible.  Hash the complete
        # replay-relevant source/config tree instead of requiring a commit or
        # accepting an operator-supplied opaque label.
        root = Path.cwd()
        digest = hashlib.sha256()
        paths = [root / "pyproject.toml"]
        for pattern in ("src/vnedge/**/*.py", "configs/**/*.yaml", "configs/**/*.yml"):
            paths.extend(root.glob(pattern))
        for path in sorted({row for row in paths if row.is_file()}):
            relative = path.relative_to(root).as_posix().encode("utf-8")
            digest.update(len(relative).to_bytes(8, "big"))
            digest.update(relative)
            content = path.read_bytes()
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)
        return f"local-tree-sha256:{digest.hexdigest()}"
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _tick_sizes(rows: list[str]) -> dict[str, float]:
    result: dict[str, float] = {}
    for row in rows:
        symbol, separator, value = row.partition("=")
        if not separator:
            raise ValueError("tick sizes must use SYMBOL=VALUE")
        native = symbol.upper().strip()
        tick = float(value)
        if not native or tick <= 0 or native in result:
            raise ValueError(f"invalid or duplicate tick size: {row}")
        result[native] = tick
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-root", type=Path, default=Path("data/delta_events"))
    parser.add_argument("--symbols", "--symbol", dest="symbols", default="BTCUSD")
    parser.add_argument("--channels", default="ob_updates,trades")
    parser.add_argument("--from", dest="start", required=True)
    parser.add_argument("--to", dest="end", required=True)
    parser.add_argument("--code-version", default=None)
    parser.add_argument("--speed", type=float, default=0.0)
    parser.add_argument("--signal-to-fill-latency-ms", type=int, default=100)
    parser.add_argument("--scanner", choices=("none", "flow", "absorption"), default="flow")
    parser.add_argument("--tick-size", action="append", default=[])
    parser.add_argument("--minimum-absorption-notional", type=float, default=5_000.0)
    parser.add_argument("--holdout-manifest", type=Path)
    parser.add_argument("--sealed-holdout", action="store_true")
    parser.add_argument("--no-journal", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=Path("research/event_replay"))
    return parser


def main() -> int:
    args = _parser().parse_args()
    symbols = tuple(row.strip().upper() for row in args.symbols.split(",") if row.strip())
    channels = tuple(row.strip() for row in args.channels.split(",") if row.strip())
    code_version = args.code_version or _code_version()
    ticks = _tick_sizes(args.tick_size)
    if args.scanner == "absorption" and set(symbols) != set(ticks):
        raise SystemExit("absorption replay requires one --tick-size SYMBOL=VALUE per symbol")
    instruments = tuple(
        AbsorptionInstrumentConfig(
            symbol=symbol,
            tick_size=ticks[symbol],
            minimum_aggressive_notional_usd=args.minimum_absorption_notional,
        )
        for symbol in symbols
        if symbol in ticks
    )
    absorption_config = AbsorptionDetectorConfig(
        enabled=args.scanner == "absorption",
        instruments=instruments,
    )
    fee_model = DeltaFeeModel(default_slippage_bps_per_leg=1.5)

    def trigger_factory(
        journal: DecisionJournal | None,
        enable_scanner: bool,
        random_seed: int,
    ) -> EventDrivenTriggerLayer:
        del random_seed  # Current deterministic scanners have no stochastic component.
        scanners = ()
        research = None
        if enable_scanner and args.scanner == "flow":
            scanners = (SustainedFlowImbalanceScanner(fee_model),)
        elif enable_scanner and args.scanner == "absorption":
            scanners = (AbsorptionReversalScanner(fee_model),)
            research = AbsorptionResearchTracker(
                fee_model,
                instruments,
                config=AbsorptionResearchConfig(),
                journal=journal,
            )
        return EventDrivenTriggerLayer(
            scanners,
            config=EventTriggerConfig(
                enabled_symbols=symbols,
                absorption=absorption_config,
            ),
            gates=SignalGateConfig(allowed_symbols=symbols),
            journal=journal,
            absorption_research=research,
        )

    manifest = (
        load_holdout_manifest(args.holdout_manifest)
        if args.holdout_manifest
        else HoldoutManifest()
    )
    config = ReplayConfig(
        symbols=symbols,
        start_ts_us=_timestamp_us(args.start),
        end_ts_us=_timestamp_us(args.end),
        channels=channels,
        speed_multiplier=args.speed,
        signal_to_fill_latency_ms=args.signal_to_fill_latency_ms,
        enable_scanner=args.scanner != "none",
        journal_mode="none" if args.no_journal else "research",
        sealed_holdout=args.sealed_holdout,
        code_version=code_version,
    )
    engine = EventReplayEngine(
        DeltaShardEventStore(args.event_root),
        trigger_factory,
        output_dir=args.output_dir,
        holdout_manifest=manifest,
        current_code_version=code_version,
    )
    result = engine.replay(config)
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
