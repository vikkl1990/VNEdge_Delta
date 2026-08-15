"""Prospective ETHUSD 1h Kronos evidence collector.

This lane converts the encouraging historical Kronos diagnostic into an honest
forward experiment.  It deliberately has no paper or execution integration:

* decisions are made only after a completed 1h candle;
* entry is the next available 1h open;
* exactly one 12h observation may be active at a time;
* outcomes use the canonical 14.8 bps taker/taker cost contract;
* forecasts and state transitions are append-only and hash chained;
* old bars are never backfilled as prospective decisions.

The module may publish dashboard telemetry, but it cannot trade or promote.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import pandas as pd

from vnedge.execution.journal import DecisionJournal
from vnedge.research.kronos_forecast_gate import KronosForecastGateConfig
from vnedge.research.kronos_inference import (
    DEFAULT_KRONOS_REPO,
    KronosBackend,
    KronosInferenceConfig,
    UpstreamKronosBackend,
    generate_kronos_forecast,
    write_forecast_artifact,
)
from vnedge.research.mtf_amf_rejection_scanner import fetch_delta_public_candles
from vnedge.research.strategy_evidence_registry import (
    DEFAULT_REGISTRY,
    route_cost_contract,
    strategy_authority_blockers,
)
from vnedge.runtime_version import code_version

SCHEMA_VERSION = "vnedge.kronos_forward_evidence.v1"
STRATEGY_ID = "kronos_ethusd_1h_forward_v1"
SYMBOL = "ETHUSD"
TIMEFRAME = "1h"
DECISION_KIND = "kronos_forward_observation"
ENTRY_KIND = "kronos_forward_entry"
OUTCOME_KIND = "kronos_forward_outcome"
EVALUATION_KIND = "kronos_forward_evaluation"


@dataclass(frozen=True)
class KronosForwardContract:
    """Frozen prospective experiment contract."""

    strategy_id: str = STRATEGY_ID
    symbol: str = SYMBOL
    timeframe: str = TIMEFRAME
    hold_bars: int = 12
    required_observations: int = 60
    cost_contract: str = "taker_full_14_8"
    max_decision_delay_seconds: int = 10 * 60
    candle_history_days: int = 30
    average_net_gate_bps: float = 10.0
    profit_factor_gate: float = 1.30
    one_active_observation: bool = True
    entry_rule: str = "next_1h_open_after_closed_decision_candle"
    exit_rule: str = "close_of_twelfth_1h_bar_after_entry"
    direction_rule: str = "pinned_kronos_gate_selected_side"

    def __post_init__(self) -> None:
        if self.hold_bars < 1 or self.required_observations < 1:
            raise ValueError("hold bars and required observations must be positive")
        if self.max_decision_delay_seconds < 0 or self.candle_history_days < 7:
            raise ValueError("invalid prospective timing configuration")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT_FORWARD_CONTRACT = KronosForwardContract()


FROZEN_INFERENCE_CONFIG = KronosInferenceConfig(
    lookback_bars=128,
    horizon_bars=12,
    sample_paths=4,
    seed=42,
    temperature=1.0,
    top_k=0,
    top_p=0.9,
    max_context=2048,
    clip=5.0,
    device="cpu",
    timestamp_convention="open",
    local_files_only=True,
)


def frozen_gate_config(cost_bps: float) -> KronosForecastGateConfig:
    return KronosForecastGateConfig(
        favorable_weight=0.3,
        terminal_weight=0.7,
        maker_taker_cost_bps=8.0,
        taker_taker_cost_bps=cost_bps,
        max_adverse_bps=180.0,
        max_horizon_bars=64,
        min_confidence=0.55,
        min_expected_net_bps=25.0,
        min_reward_risk=1.2,
        safety_buffer_bps=5.0,
    )


@dataclass(frozen=True)
class CollectorPaths:
    journal: Path
    latest: Path
    artifacts: Path


class KronosForwardCollector:
    """State derived entirely from a hash-chained append-only journal."""

    def __init__(
        self,
        *,
        paths: CollectorPaths,
        backend: KronosBackend,
        contract: KronosForwardContract = DEFAULT_FORWARD_CONTRACT,
        registry_path: Path | str = DEFAULT_REGISTRY,
        version: str | None = None,
    ) -> None:
        self.paths = paths
        self.backend = backend
        self.contract = contract
        self.registry_path = Path(registry_path)
        self.version = version or code_version()
        self.journal = DecisionJournal(paths.journal, hash_chain=True)
        if not self.journal.available:
            raise RuntimeError("Kronos forward evidence journal is unavailable")
        cost = route_cost_contract(contract.cost_contract, registry_path=self.registry_path)
        if cost.route != "taker_taker":
            raise ValueError("Kronos forward v1 requires a taker/taker cost contract")
        self.cost_bps = float(cost.total_roundtrip_bps)
        if not math.isclose(self.cost_bps, 14.8, abs_tol=1e-9):
            raise ValueError("Kronos forward v1 is frozen to the 14.8 bps cost wall")
        blockers = strategy_authority_blockers(
            contract.strategy_id,
            purpose="observation",
            registry_path=self.registry_path,
        )
        if blockers:
            raise ValueError("canonical observation authority blocked: " + "; ".join(blockers))

    def run_once(self, candles: pd.DataFrame, *, now: datetime) -> dict[str, Any]:
        current = _utc(now)
        frame = _canonical_frame(candles)
        self._resolve_pending(frame, current)
        records = self.journal.read_all()
        active = _active_observation(records)
        completed = _completed(frame, current)
        status = "WAITING_FOR_CLOSED_1H_CANDLE"

        if active is not None:
            status = "OBSERVATION_ACTIVE"
        elif len(completed) < FROZEN_INFERENCE_CONFIG.lookback_bars:
            status = "INSUFFICIENT_CAUSAL_HISTORY"
        else:
            latest_open = pd.Timestamp(completed["timestamp"].iloc[-1])
            decision_ts = latest_open + pd.Timedelta(hours=1)
            decision_id = _decision_id(decision_ts)
            already_evaluated = _has_decision_evaluation(records, decision_id)
            age_seconds = (pd.Timestamp(current) - decision_ts).total_seconds()
            if already_evaluated:
                status = "LATEST_CANDLE_ALREADY_EVALUATED"
            elif age_seconds < 0:
                status = "WAITING_FOR_CLOSED_1H_CANDLE"
            elif age_seconds > self.contract.max_decision_delay_seconds:
                status = "WAITING_FOR_FRESH_1H_CLOSE"
            else:
                status = self._evaluate_latest(
                    completed,
                    decision_ts=decision_ts,
                    decision_id=decision_id,
                    now=current,
                )

        report = build_forward_report(
            self.journal.read_all(),
            contract=self.contract,
            cost_bps=self.cost_bps,
            status=status,
            generated_at=current,
            version=self.version,
        )
        _atomic_json(self.paths.latest, report)
        return report

    def _evaluate_latest(
        self,
        completed: pd.DataFrame,
        *,
        decision_ts: pd.Timestamp,
        decision_id: str,
        now: datetime,
    ) -> str:
        artifact = generate_kronos_forecast(
            completed,
            symbol=self.contract.symbol,
            timeframe=self.contract.timeframe,
            decision_timestamp=decision_ts,
            backend=self.backend,
            config=FROZEN_INFERENCE_CONFIG,
            gate_config=frozen_gate_config(self.cost_bps),
            route="taker_taker",
            now=now,
        )
        artifact_path = self.paths.artifacts / f"{decision_id}.json"
        write_forecast_artifact(artifact, artifact_path)
        gate = artifact.gate_decision
        evaluation = {
            "decision_id": decision_id,
            "strategy_id": self.contract.strategy_id,
            "symbol": self.contract.symbol,
            "timeframe": self.contract.timeframe,
            "decision_timestamp": decision_ts.isoformat(),
            "verdict": gate.get("verdict"),
            "selected_side": gate.get("selected_side"),
            "primary_blocker": gate.get("primary_blocker"),
            "artifact": str(artifact_path),
            "artifact_id": artifact.artifact_id,
            "artifact_payload_sha256": artifact.payload_sha256,
            "code_version": self.version,
            "can_trade": False,
            "can_promote": False,
        }
        if not self.journal.append(EVALUATION_KIND, evaluation):
            raise RuntimeError("unable to append Kronos evaluation")
        if gate.get("verdict") != "FORECAST_GATE_PASS":
            return "FORECAST_REJECTED"
        side = str(gate.get("selected_side") or "")
        if side not in {"long", "short"}:
            raise RuntimeError("passing forecast has no valid selected side")
        observation = {
            **evaluation,
            "side": side,
            "hold_bars": self.contract.hold_bars,
            "cost_contract": self.contract.cost_contract,
            "round_trip_cost_bps": self.cost_bps,
            "entry_rule": self.contract.entry_rule,
            "exit_rule": self.contract.exit_rule,
            "observation_state": "ENTRY_PENDING",
        }
        if not self.journal.append(DECISION_KIND, observation):
            raise RuntimeError("unable to append Kronos observation")
        return "OBSERVATION_ACCEPTED_ENTRY_PENDING"

    def _resolve_pending(self, frame: pd.DataFrame, now: datetime) -> None:
        records = self.journal.read_all()
        observations = _payloads(records, DECISION_KIND)
        entries = {row["decision_id"]: row for row in _payloads(records, ENTRY_KIND)}
        outcomes = {row["decision_id"]: row for row in _payloads(records, OUTCOME_KIND)}
        indexed = frame.set_index("timestamp", drop=False)

        for observation in observations:
            decision_id = str(observation["decision_id"])
            if decision_id in outcomes:
                continue
            entry = entries.get(decision_id)
            if entry is None:
                entry_ts = pd.Timestamp(observation["decision_timestamp"])
                if entry_ts not in indexed.index:
                    continue
                row = indexed.loc[entry_ts]
                if isinstance(row, pd.DataFrame):
                    row = row.iloc[-1]
                entry = {
                    "decision_id": decision_id,
                    "entry_timestamp": entry_ts.isoformat(),
                    "entry_price": float(row["open"]),
                    "entry_source": "next_1h_open",
                    "can_trade": False,
                    "can_promote": False,
                }
                if not self.journal.append(ENTRY_KIND, entry):
                    raise RuntimeError("unable to append Kronos forward entry")
                entries[decision_id] = entry

            entry_ts = pd.Timestamp(entry["entry_timestamp"])
            exit_available = entry_ts + pd.Timedelta(hours=self.contract.hold_bars)
            if pd.Timestamp(now) < exit_available:
                continue
            path = frame.loc[
                (frame["timestamp"] >= entry_ts)
                & (frame["timestamp"] < exit_available)
            ].sort_values("timestamp")
            if len(path) != self.contract.hold_bars:
                continue
            expected = pd.date_range(
                entry_ts,
                periods=self.contract.hold_bars,
                freq="1h",
                tz="UTC",
            )
            if list(path["timestamp"]) != list(expected):
                continue
            outcome = _outcome(
                observation,
                entry,
                path,
                cost_bps=self.cost_bps,
                exit_available=exit_available,
            )
            if not self.journal.append(OUTCOME_KIND, outcome):
                raise RuntimeError("unable to append Kronos forward outcome")


def _outcome(
    observation: dict[str, Any],
    entry: dict[str, Any],
    path: pd.DataFrame,
    *,
    cost_bps: float,
    exit_available: pd.Timestamp,
) -> dict[str, Any]:
    side = str(observation["side"])
    direction = 1.0 if side == "long" else -1.0
    entry_price = float(entry["entry_price"])
    exit_price = float(path["close"].iloc[-1])
    gross_bps = direction * (exit_price / entry_price - 1.0) * 10_000.0
    if side == "long":
        mfe_bps = (float(path["high"].max()) / entry_price - 1.0) * 10_000.0
        mae_bps = (float(path["low"].min()) / entry_price - 1.0) * 10_000.0
    else:
        mfe_bps = (entry_price / float(path["low"].min()) - 1.0) * 10_000.0
        mae_bps = (entry_price / float(path["high"].max()) - 1.0) * 10_000.0
    return {
        "decision_id": observation["decision_id"],
        "side": side,
        "entry_timestamp": entry["entry_timestamp"],
        "entry_price": entry_price,
        "exit_timestamp": exit_available.isoformat(),
        "exit_price": exit_price,
        "hold_bars": len(path),
        "gross_bps": gross_bps,
        "cost_bps": cost_bps,
        "net_bps": gross_bps - cost_bps,
        "mfe_bps": mfe_bps,
        "mae_bps": mae_bps,
        "exit_reason": "vertical_barrier_12h",
        "can_trade": False,
        "can_promote": False,
    }


def build_forward_report(
    records: list[dict[str, Any]],
    *,
    contract: KronosForwardContract,
    cost_bps: float,
    status: str,
    generated_at: datetime,
    version: str,
) -> dict[str, Any]:
    evaluations = _payloads(records, EVALUATION_KIND)
    observations = _payloads(records, DECISION_KIND)
    entries = _payloads(records, ENTRY_KIND)
    outcomes = _payloads(records, OUTCOME_KIND)
    net = [float(row["net_bps"]) for row in outcomes]
    gross = [float(row["gross_bps"]) for row in outcomes]
    wins = sum(value > 0 for value in net)
    gains = sum(value for value in net if value > 0)
    losses = abs(sum(value for value in net if value < 0))
    pf = gains / losses if losses else None
    pf_for_gate = pf if pf is not None else (math.inf if gains else 0.0)
    average_net = sum(net) / len(net) if net else None
    gate_passes = sum(row.get("verdict") == "FORECAST_GATE_PASS" for row in evaluations)
    progress = len(outcomes) / contract.required_observations
    economics_pass = (
        len(outcomes) >= contract.required_observations
        and average_net is not None
        and average_net >= contract.average_net_gate_bps
        and pf_for_gate >= contract.profit_factor_gate
    )
    active = _active_observation(records)
    report = {
        "schema_version": SCHEMA_VERSION,
        "strategy_id": contract.strategy_id,
        "generated_at": _utc(generated_at).isoformat(),
        "code_version": version,
        "mode": "prospective_research_shadow_only",
        "status": status,
        "contract": contract.to_dict(),
        "inference_config": FROZEN_INFERENCE_CONFIG.to_dict(),
        "gate_config": frozen_gate_config(cost_bps).to_dict(),
        "cost_contract": {
            "id": contract.cost_contract,
            "route": "taker_taker",
            "round_trip_bps": cost_bps,
        },
        "summary": {
            "evaluations": len(evaluations),
            "gate_passes": gate_passes,
            "journaled_observations": len(observations),
            "entries_captured": len(entries),
            "resolved_outcomes": len(outcomes),
            "required_observations": contract.required_observations,
            "progress_pct": min(progress, 1.0) * 100.0,
            "active_observation": active is not None,
            "average_gross_bps": (sum(gross) / len(gross)) if gross else None,
            "average_net_bps": average_net,
            "total_net_bps": sum(net),
            "profit_factor": pf,
            "profit_factor_infinite": bool(gains and not losses),
            "win_rate": wins / len(net) if net else None,
        },
        "selection_gate": {
            "passed": economics_pass,
            "minimum_observations_passed": len(outcomes) >= contract.required_observations,
            "average_net_passed": (
                average_net is not None and average_net >= contract.average_net_gate_bps
            ),
            "profit_factor_passed": (
                pf_for_gate >= contract.profit_factor_gate if net else False
            ),
            "sealed_holdout_opened": False,
            "paper_authorized": False,
            "live_authorized": False,
        },
        "active_observation": active,
        "recent_outcomes": outcomes[-10:],
        "journal": {
            "path": str(contract.strategy_id + ".jsonl"),
            "records": len(records),
            "hash_chained": True,
        },
        "operator_answer": (
            f"{len(outcomes)}/{contract.required_observations} independent prospective "
            "12h ETHUSD observations resolved. Paper and live remain locked."
        ),
        "research_only": True,
        "can_trade": False,
        "can_promote": False,
    }
    report["payload_sha256"] = _sha256_json(report)
    return report


def _completed(frame: pd.DataFrame, now: datetime) -> pd.DataFrame:
    return frame.loc[frame["timestamp"] + pd.Timedelta(hours=1) <= pd.Timestamp(now)].copy()


def _canonical_frame(candles: pd.DataFrame) -> pd.DataFrame:
    required = {"timestamp", "open", "high", "low", "close", "volume"}
    missing = sorted(required - set(candles.columns))
    if missing:
        raise ValueError("candles missing columns: " + ", ".join(missing))
    frame = candles[list(required)].copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="raise").astype(float)
    return frame.sort_values("timestamp").drop_duplicates("timestamp", keep="last").reset_index(drop=True)


def _payloads(records: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [dict(row["payload"]) for row in records if row.get("kind") == kind]


def _active_observation(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    outcomes = {row["decision_id"] for row in _payloads(records, OUTCOME_KIND)}
    for observation in reversed(_payloads(records, DECISION_KIND)):
        if observation.get("decision_id") not in outcomes:
            return observation
    return None


def _has_decision_evaluation(records: list[dict[str, Any]], decision_id: str) -> bool:
    return any(row.get("decision_id") == decision_id for row in _payloads(records, EVALUATION_KIND))


def _decision_id(decision_ts: pd.Timestamp) -> str:
    raw = f"{STRATEGY_ID}|{SYMBOL}|{decision_ts.isoformat()}".encode()
    return f"kef_{hashlib.sha256(raw).hexdigest()[:20]}"


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w", dir=path.parent, prefix=path.name, suffix=".tmp", delete=False, encoding="utf-8"
    ) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def _default_paths(root: Path) -> CollectorPaths:
    live = root / "research" / "live_research"
    return CollectorPaths(
        journal=live / "kronos_ethusd_1h_forward.jsonl",
        latest=live / "kronos_ethusd_1h_forward_latest.json",
        artifacts=live / "kronos_ethusd_1h_forecasts",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--kronos-repo", type=Path, default=DEFAULT_KRONOS_REPO)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--interval-seconds", type=float, default=60.0)
    parser.add_argument("--once", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    contract = KronosForwardContract()
    backend = UpstreamKronosBackend(repo=args.kronos_repo, config=FROZEN_INFERENCE_CONFIG)
    collector = KronosForwardCollector(
        paths=_default_paths(args.repo_root),
        backend=backend,
        contract=contract,
        registry_path=args.registry,
        version=code_version(args.repo_root),
    )
    while True:
        now = datetime.now(UTC)
        candles = fetch_delta_public_candles(
            SYMBOL,
            "1h",
            days=contract.candle_history_days,
            now=now,
            include_incomplete=True,
        )
        report = collector.run_once(candles, now=now)
        print(json.dumps({"status": report["status"], "summary": report["summary"]}))
        if args.once:
            return 0
        time.sleep(max(args.interval_seconds, 1.0))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
