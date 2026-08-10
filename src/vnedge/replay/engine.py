"""Deterministic, isolated replay through the live event feature/scanner path."""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import perf_counter_ns

from vnedge.execution.journal import DecisionJournal
from vnedge.replay.metrics import latency_percentiles, replay_economic_summary
from vnedge.replay.models import (
    HoldoutManifest,
    RecordingValidationReport,
    ReplayConfig,
    ReplayResult,
    ReplayTick,
)
from vnedge.replay.outcomes import ReplayForwardTracker
from vnedge.replay.store import EventStore
from vnedge.replay.validator import validate_recorded_events
from vnedge.scalping.delta_engine.event_trigger import (
    DeltaVerifiedEventBridge,
    EventDrivenTriggerLayer,
)

TriggerFactory = Callable[[DecisionJournal | None, bool, int], EventDrivenTriggerLayer]


class DeterministicReplayJournal(DecisionJournal):
    """DecisionJournal format with event-time, rather than wall-time, envelopes."""

    def append(self, kind: str, payload: dict[str, object]) -> bool:
        timestamp = payload.get("decision_ts") or payload.get("resolved_ts")
        if not isinstance(timestamp, str):
            timestamp_us = payload.get("exit_ts_us") or payload.get("decision_ts_us")
            if isinstance(timestamp_us, int):
                from datetime import UTC, datetime

                timestamp = datetime.fromtimestamp(timestamp_us / 1_000_000, tz=UTC).isoformat()
        if not isinstance(timestamp, str):
            timestamp = "1970-01-01T00:00:00+00:00"
        record = {"ts": timestamp, "kind": kind, "payload": payload}
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        record,
                        default=str,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    )
                    + "\n"
                )
                handle.flush()
                os.fsync(handle.fileno())
            return True
        except (OSError, TypeError, ValueError) as exc:
            self._mark_unavailable(str(exc))
            return False


class EventReplayEngine:
    """Replay owner. It has no risk gateway, broker, or execution adapter."""

    def __init__(
        self,
        store: EventStore,
        trigger_factory: TriggerFactory,
        *,
        output_dir: Path | str = Path("research/event_replay"),
        holdout_manifest: HoldoutManifest | None = None,
        current_code_version: str,
    ) -> None:
        if not current_code_version.strip():
            raise ValueError("current code version is mandatory")
        self.store = store
        self.trigger_factory = trigger_factory
        self.output_dir = Path(output_dir)
        self.holdout_manifest = holdout_manifest or HoldoutManifest()
        self.current_code_version = current_code_version.strip()

    def validate_recording(
        self,
        config: ReplayConfig | str,
        start_ts_us: int | None = None,
        end_ts_us: int | None = None,
        *,
        channels: tuple[str, ...] = ("trades", "ob_updates"),
    ) -> RecordingValidationReport:
        if isinstance(config, str):
            if start_ts_us is None or end_ts_us is None:
                raise TypeError("symbol validation requires start_ts_us and end_ts_us")
            resolved = ReplayConfig(
                symbols=(config,),
                start_ts_us=start_ts_us,
                end_ts_us=end_ts_us,
                channels=channels,
                enable_scanner=False,
                journal_mode="none",
                code_version=self.current_code_version,
            )
        else:
            if start_ts_us is not None or end_ts_us is not None:
                raise TypeError("timestamps must not accompany a ReplayConfig")
            resolved = config
        return validate_recorded_events(self.store.iter_events(resolved))

    def replay_iterator(self, config: ReplayConfig) -> Iterator[ReplayTick]:
        self._guard(config)
        validation = self.validate_recording(config)
        if config.fail_on_integrity_error and not validation.passed:
            raise ValueError("recording validation failed: " + "; ".join(validation.issues))
        journal, _ = self._journal(config)
        forward = ReplayForwardTracker(
            journal,
            signal_to_fill_latency_ms=config.signal_to_fill_latency_ms,
        )
        yield from self._iterate(config, journal, forward)
        forward.finalize()

    def replay(self, config: ReplayConfig) -> ReplayResult:
        self._guard(config)
        validation = self.validate_recording(config)
        if config.fail_on_integrity_error and not validation.passed:
            raise ValueError("recording validation failed: " + "; ".join(validation.issues))
        journal, journal_path = self._journal(config)
        forward = ReplayForwardTracker(
            journal,
            signal_to_fill_latency_ms=config.signal_to_fill_latency_ms,
        )
        feature_latencies: list[int] = []
        decision_latencies: list[int] = []
        selected_payloads: list[dict[str, object]] = []
        events = 0
        decisions = 0
        evaluated = 0
        for tick in self._iterate(config, journal, forward):
            events += 1
            feature_latencies.append(tick.feature_latency_ns)
            if tick.decision_latency_ns:
                decision_latencies.append(tick.decision_latency_ns)
                decisions += 1
            evaluated += len(tick.candidates)
            if tick.selected is not None:
                selected_payloads.append(tick.selected.to_dict())
        forward.finalize()
        candidate_hash = hashlib.sha256()
        for payload in selected_payloads:
            candidate_hash.update(
                json.dumps(
                    payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            )
            candidate_hash.update(b"\n")
        records = journal.read_all() if journal is not None else []
        summary = replay_economic_summary(
            records,
            decisions=decisions,
            evaluated_candidates=evaluated,
            selected_candidates=len(selected_payloads),
            replay_outcomes=forward.outcomes,
        )
        latency = {
            "feature_path": latency_percentiles(feature_latencies),
            "decision_path": latency_percentiles(decision_latencies),
            "measurement_note": (
                "feature_path is wall-runtime of the shared bridge; decision_path is the "
                "event layer's internal receive-to-decision runtime"
            ),
        }
        result_path = self._result_path(config)
        result = ReplayResult(
            config=config,
            events_processed=events,
            gaps_detected=validation.sequence_gaps,
            candidates_emitted=len(selected_payloads),
            journal_path=str(journal_path) if journal_path else None,
            summary_metrics=summary,
            latency_percentiles=latency,
            code_version=self.current_code_version,
            deterministic_hash=candidate_hash.hexdigest(),
            validation=validation,
            result_path=str(result_path),
        )
        self._atomic_json(result_path, result.to_dict())
        return result

    def _iterate(
        self,
        config: ReplayConfig,
        journal: DecisionJournal | None,
        forward: ReplayForwardTracker,
    ) -> Iterator[ReplayTick]:
        trigger = self.trigger_factory(journal, config.enable_scanner, config.random_seed)
        bridge = DeltaVerifiedEventBridge(trigger)
        previous_receive_ns: int | None = None
        for event in self.store.iter_events(config):
            forward.on_event(event)
            if (
                config.speed_multiplier > 0
                and previous_receive_ns is not None
                and event.local_recv_ns > previous_receive_ns
            ):
                time.sleep(
                    (event.local_recv_ns - previous_receive_ns)
                    / 1_000_000_000.0
                    / config.speed_multiplier
                )
            previous_receive_ns = event.local_recv_ns
            if not config.enable_feature_engine:
                yield ReplayTick(event, None, (), None, 0, 0)
                continue
            envelope = dict(event.envelope)
            started = perf_counter_ns()
            decision = bridge.consume(envelope, integrity_verified=True)
            elapsed = perf_counter_ns() - started
            features = decision.context.to_dict() if decision and decision.context else None
            candidates = decision.evaluated if decision else ()
            selected = decision.selected if decision else None
            if selected is not None:
                forward.register(
                    selected,
                    decision_ts_us=event.local_recv_ns // 1_000,
                )
            yield ReplayTick(
                event=event,
                features=features,
                candidates=candidates,
                selected=selected,
                decision_latency_ns=(decision.total_duration_us * 1_000 if decision else 0),
                feature_latency_ns=elapsed,
            )

    def _guard(self, config: ReplayConfig) -> None:
        if config.code_version != self.current_code_version:
            raise ValueError(
                f"replay code version mismatch: requested={config.code_version} "
                f"current={self.current_code_version}"
            )
        self.holdout_manifest.guard(config)

    def _journal(
        self,
        config: ReplayConfig,
    ) -> tuple[DeterministicReplayJournal | None, Path | None]:
        if config.journal_mode == "none":
            return None, None
        self.output_dir.mkdir(parents=True, exist_ok=True)
        path = self._unique_path(config, suffix=".journal.jsonl")
        return DeterministicReplayJournal(path), path

    def _result_path(self, config: ReplayConfig) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        return self._unique_path(config, suffix=".result.json")

    def _unique_path(self, config: ReplayConfig, *, suffix: str) -> Path:
        fingerprint = hashlib.sha256(
            json.dumps(
                config.canonical_dict(), sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()[:16]
        base = self.output_dir / f"replay_{fingerprint}{suffix}"
        if not base.exists():
            return base
        counter = 2
        while True:
            candidate = self.output_dir / f"replay_{fingerprint}_{counter}{suffix}"
            if not candidate.exists():
                return candidate
            counter += 1

    @staticmethod
    def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
        with NamedTemporaryFile(
            "w",
            dir=path.parent,
            prefix=path.name,
            suffix=".tmp",
            delete=False,
            encoding="utf-8",
        ) as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        temporary.replace(path)
