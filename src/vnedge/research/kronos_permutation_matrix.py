"""Bounded exhaustive Kronos logic/economics matrix across VNEDGE timeframes.

The search space is explicit and finite.  Real pinned Kronos forecasts are
generated only from a chronological selection window; the final tail is held
back and never evaluated by this command. Forecasts are reused when sweeping
fee routes and decision-gate thresholds so economic permutations do not cause
extra model calls or accidental differences in the underlying AI output.

This remains research-only and cannot create a trial or an order route.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, cast

import numpy as np
import pandas as pd

from vnedge.data.schemas import CANDLE_COLUMNS, TIMEFRAME_MS
from vnedge.research.kronos_forecast_gate import (
    ForecastRoute,
    KronosForecastGateConfig,
    score_kronos_forecast_gate,
)
from vnedge.research.kronos_inference import (
    DEFAULT_KRONOS_REPO,
    KronosBackend,
    KronosInferenceConfig,
    UpstreamKronosBackend,
    generate_kronos_forecast_batch,
)

REPORT_ID = "kronos_permutation_matrix_v1"
DEFAULT_CACHE = Path("data/delta_scalper_cache")
DEFAULT_OUTPUT = Path("research/live_research/kronos_permutation_matrix_latest.json")
DEFAULT_CSV = Path("research/live_research/kronos_permutation_matrix_ranked.csv")
ALL_TIMEFRAMES = tuple(TIMEFRAME_MS)


@dataclass(frozen=True)
class KronosPermutationConfig:
    symbols: tuple[str, ...] = ("BTCUSD", "ETHUSD")
    timeframes: tuple[str, ...] = ALL_TIMEFRAMES
    lookbacks: tuple[int, ...] = (64, 128)
    horizon_hours: tuple[float, ...] = (1.0, 4.0, 12.0)
    temperatures: tuple[float, ...] = (1.0,)
    top_ps: tuple[float, ...] = (0.90,)
    generated_sample_paths: int = 4
    sample_path_subsets: tuple[int, ...] = (1, 4)
    routes: tuple[ForecastRoute, ...] = ("maker_taker", "taker_taker")
    min_expected_net_bps: tuple[float, ...] = (0.0, 25.0, 50.0)
    min_confidences: tuple[float, ...] = (0.50, 0.60, 0.70)
    min_reward_risks: tuple[float, ...] = (0.0, 1.20, 1.50)
    observations_per_base: int = 12
    holdback_fraction: float = 0.20
    max_horizon_bars: int = 64
    seed: int = 42
    device: str = "cpu"
    local_files_only: bool = True

    def __post_init__(self) -> None:
        if not self.symbols or not self.timeframes:
            raise ValueError("symbols and timeframes cannot be empty")
        unknown = sorted(set(self.timeframes) - set(TIMEFRAME_MS))
        if unknown:
            raise ValueError(f"unsupported timeframes: {', '.join(unknown)}")
        if any(value < 32 or value > 2048 for value in self.lookbacks):
            raise ValueError("lookbacks must be in [32, 2048]")
        if any(value <= 0 for value in self.horizon_hours):
            raise ValueError("horizon_hours must be positive")
        if any(value <= 0 for value in self.temperatures):
            raise ValueError("temperatures must be positive")
        if any(not 0 < value <= 1 for value in self.top_ps):
            raise ValueError("top_ps must be in (0, 1]")
        if not 1 <= self.generated_sample_paths <= 128:
            raise ValueError("generated_sample_paths must be in [1, 128]")
        if any(
            value < 1 or value > self.generated_sample_paths for value in self.sample_path_subsets
        ):
            raise ValueError("sample_path_subsets must fit generated_sample_paths")
        if self.observations_per_base < 2:
            raise ValueError("observations_per_base must be at least 2")
        if not 0.10 <= self.holdback_fraction <= 0.50:
            raise ValueError("holdback_fraction must be in [0.10, 0.50]")
        if not 1 <= self.max_horizon_bars <= 64:
            raise ValueError("max_horizon_bars must be in [1, 64]")

        for field_name in (
            "symbols",
            "timeframes",
            "lookbacks",
            "horizon_hours",
            "temperatures",
            "top_ps",
            "sample_path_subsets",
            "routes",
            "min_expected_net_bps",
            "min_confidences",
            "min_reward_risks",
        ):
            values = tuple(dict.fromkeys(getattr(self, field_name)))
            if len(values) != len(getattr(self, field_name)):
                raise ValueError(f"{field_name} contains duplicates")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT_MATRIX_CONFIG = KronosPermutationConfig()


def load_canonical_minute_cache(
    symbol: str,
    *,
    cache_dir: Path | str = DEFAULT_CACHE,
) -> pd.DataFrame:
    """Load overlapping local shards into one conflict-free contiguous tape."""

    root = Path(cache_dir)
    paths = sorted(root.glob(f"{symbol.upper()}_1m_*.parquet"))
    if not paths:
        raise FileNotFoundError(f"no cached 1m candles found for {symbol} under {root}")
    frames = [pd.read_parquet(path, columns=CANDLE_COLUMNS) for path in paths]
    frame = pd.concat(frames, ignore_index=True)
    frame["timestamp"] = _normalize_timestamps(frame["timestamp"])
    for column in CANDLE_COLUMNS[1:]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if frame[list(CANDLE_COLUMNS)].isna().any().any():
        raise ValueError(f"{symbol} cache contains null/non-numeric candle fields")

    duplicated = frame[frame.duplicated("timestamp", keep=False)]
    if not duplicated.empty:
        conflicts = duplicated.groupby("timestamp", sort=False)[list(CANDLE_COLUMNS[1:])].nunique(
            dropna=False
        )
        if (conflicts > 1).any(axis=None):
            bad = conflicts.index[(conflicts > 1).any(axis=1)][0]
            raise ValueError(f"{symbol} overlapping cache shards conflict at {bad}")
    frame = (
        frame.drop_duplicates("timestamp", keep="last")
        .sort_values("timestamp")
        .reset_index(drop=True)
    )
    if len(frame) < 2:
        raise ValueError(f"{symbol} cache has insufficient unique candles")
    deltas = frame["timestamp"].diff().iloc[1:]
    gap_mask = deltas != pd.Timedelta(minutes=1)
    if gap_mask.any():
        location = int(np.flatnonzero(gap_mask.to_numpy())[0]) + 1
        raise ValueError(
            f"{symbol} canonical 1m tape is not contiguous at "
            f"{frame['timestamp'].iloc[location].isoformat()}"
        )
    return frame


def aggregate_causal_candles(minutes: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Aggregate complete UTC/epoch-aligned bars and reject hidden gaps."""

    if timeframe not in TIMEFRAME_MS:
        raise ValueError(f"unsupported timeframe: {timeframe}")
    if timeframe == "1m":
        return minutes.copy(deep=True)
    bar_minutes = TIMEFRAME_MS[timeframe] // TIMEFRAME_MS["1m"]
    rule = f"{TIMEFRAME_MS[timeframe]}ms"
    source = minutes.set_index("timestamp")
    grouped = source.resample(rule, origin="epoch", label="left", closed="left")
    candles = grouped.agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }
    )
    counts = grouped["close"].count()
    candles = candles.loc[counts == bar_minutes].dropna().reset_index()
    if len(candles) < 2:
        raise ValueError(f"no complete {timeframe} candles available")
    deltas = candles["timestamp"].diff().iloc[1:]
    expected = pd.Timedelta(milliseconds=TIMEFRAME_MS[timeframe])
    if (deltas != expected).any():
        raise ValueError(f"complete {timeframe} aggregation contains a gap")
    return candles[list(CANDLE_COLUMNS)]


def run_kronos_permutation_matrix(
    minute_candles: Mapping[str, pd.DataFrame],
    *,
    backend: KronosBackend,
    config: KronosPermutationConfig = DEFAULT_MATRIX_CONFIG,
    now: datetime | None = None,
    progress: bool = False,
) -> dict[str, Any]:
    """Generate the finite forecast grid and sweep every declared gate tuple."""

    started = time.monotonic()
    generated_at = pd.Timestamp(now or datetime.now(UTC)).tz_convert("UTC")
    canonical: dict[str, pd.DataFrame] = {}
    for symbol in config.symbols:
        if symbol not in minute_candles:
            raise ValueError(f"missing minute candles for {symbol}")
        frame = minute_candles[symbol].copy(deep=True)
        frame["timestamp"] = _normalize_timestamps(frame["timestamp"])
        canonical[symbol] = frame.sort_values("timestamp").reset_index(drop=True)

    common_start = max(frame["timestamp"].iloc[0] for frame in canonical.values())
    common_end = min(frame["timestamp"].iloc[-1] for frame in canonical.values())
    span = common_end - common_start
    holdback_start = common_start + span * (1.0 - config.holdback_fraction)
    base_rows: list[dict[str, Any]] = []
    combination_rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    base_specs = list(_base_specs(config))
    expected_base_runs = len(config.symbols) * len(base_specs)
    completed = 0

    for symbol in config.symbols:
        aggregated = {
            timeframe: aggregate_causal_candles(canonical[symbol], timeframe)
            for timeframe in config.timeframes
        }
        for timeframe, lookback, horizon_bars, horizon_hours, temperature, top_p in base_specs:
            spec = {
                "symbol": symbol,
                "timeframe": timeframe,
                "lookback_bars": lookback,
                "horizon_bars": horizon_bars,
                "requested_horizon_hours": horizon_hours,
                "temperature": temperature,
                "top_p": top_p,
            }
            base_id = _stable_id("kbase", spec)
            try:
                frame = aggregated[timeframe]
                step = pd.Timedelta(milliseconds=TIMEFRAME_MS[timeframe])
                selection = frame.loc[(frame["timestamp"] + step) <= holdback_start].reset_index(
                    drop=True
                )
                decision_indices = _decision_indices(
                    len(selection),
                    lookback=lookback,
                    horizon=horizon_bars,
                    requested=config.observations_per_base,
                )
                inference = KronosInferenceConfig(
                    lookback_bars=lookback,
                    horizon_bars=horizon_bars,
                    sample_paths=config.generated_sample_paths,
                    seed=config.seed,
                    temperature=temperature,
                    top_p=top_p,
                    device=config.device,
                    local_files_only=config.local_files_only,
                )
                contexts = [
                    selection.iloc[index - lookback + 1 : index + 1].reset_index(drop=True)
                    for index in decision_indices
                ]
                decisions = [
                    selection["timestamp"].iloc[index] + step for index in decision_indices
                ]
                artifacts = generate_kronos_forecast_batch(
                    contexts,
                    symbols=[symbol] * len(contexts),
                    timeframe=timeframe,
                    decision_timestamps=decisions,
                    backend=backend,
                    config=inference,
                    now=generated_at.to_pydatetime(),
                )
                if not all(artifact.verify() for artifact in artifacts):
                    raise ValueError("one or more generated artifacts failed hash verification")
                observations = [
                    {
                        "context": context,
                        "forecast": pd.DataFrame(artifact.forecast),
                        "actual": selection.iloc[index + 1 : index + 1 + horizon_bars].reset_index(
                            drop=True
                        ),
                        "artifact": artifact,
                    }
                    for context, artifact, index in zip(
                        contexts, artifacts, decision_indices, strict=True
                    )
                ]
                base_rows.append(
                    {
                        "base_id": base_id,
                        **spec,
                        "actual_horizon_hours": round(
                            horizon_bars * TIMEFRAME_MS[timeframe] / 3_600_000, 6
                        ),
                        "observations": len(observations),
                        "first_decision": artifacts[0].decision_timestamp,
                        "last_decision": artifacts[-1].decision_timestamp,
                        "artifacts_verified": len(artifacts),
                        "forecast_rows": sum(len(artifact.forecast) for artifact in artifacts),
                        "ohlc_geometry_repairs": sum(
                            int(artifact.forecast_quality["ohlc_geometry_repairs"])
                            for artifact in artifacts
                        ),
                        "artifact_hashes": [artifact.payload_sha256 for artifact in artifacts],
                    }
                )
                combination_rows.extend(
                    _score_base_permutations(
                        base_id=base_id,
                        spec=spec,
                        observations=observations,
                        config=config,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - incomplete matrix is explicit evidence
                errors.append(
                    {
                        "base_id": base_id,
                        **spec,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
            completed += 1
            if progress:
                print(
                    f"Kronos matrix {completed}/{expected_base_runs}: "
                    f"{symbol} {timeframe} lb={lookback} h={horizon_bars} "
                    f"T={temperature} p={top_p}",
                    flush=True,
                )

    combination_rows.sort(
        key=lambda row: (
            -float(row["accepted_selection"]["avg_net_bps"]),
            -_numeric_profit_factor(row["accepted_selection"]["profit_factor"]),
            -int(row["accepted_selection"]["observations"]),
            row["permutation_id"],
        )
    )
    minimum_economic_sample = 30
    minimum_paths_for_confidence = 4
    eligible = [
        row
        for row in combination_rows
        if row["accepted_selection"]["observations"] >= minimum_economic_sample
        and row["sample_paths"] >= minimum_paths_for_confidence
        and row["accepted_selection"]["avg_net_bps"] > 0
        and _numeric_profit_factor(row["accepted_selection"]["profit_factor"]) >= 1.20
    ]
    report: dict[str, Any] = {
        "report_id": REPORT_ID,
        "generated_at": generated_at.isoformat(),
        "matrix_config": config.to_dict(),
        "matrix_contract": {
            "data": "local conflict-free contiguous 1m cache; causal complete-bar aggregation",
            "decision": "after closed candle only",
            "entry": "next bar open",
            "exit": "vertical barrier close at the forecast horizon",
            "forecast_reuse": "same sealed AI paths rescored across all gate/cost permutations",
            "holdback_evaluated": False,
            "holdback_claim": "reserved matrix tail; not called untouched because history was used elsewhere",
            "minimum_economic_sample": minimum_economic_sample,
            "minimum_sample_paths_for_confidence": minimum_paths_for_confidence,
            "multiple_testing": (
                "all rankings are exploratory selection-window evidence; no winner may be promoted"
            ),
        },
        "data_window": {
            "common_start": common_start.isoformat(),
            "common_end": common_end.isoformat(),
            "selection_end_exclusive": holdback_start.isoformat(),
            "held_back_start": holdback_start.isoformat(),
            "held_back_end": common_end.isoformat(),
        },
        "completion": {
            "expected_base_runs": expected_base_runs,
            "completed_base_runs": len(base_rows),
            "failed_base_runs": len(errors),
            "scored_permutations": len(combination_rows),
            "complete": not errors and len(base_rows) == expected_base_runs,
        },
        "base_runs": base_rows,
        "ranked_permutations": combination_rows,
        "eligible_selection_candidates": eligible,
        "errors": errors,
        "runtime_seconds": round(time.monotonic() - started, 3),
        "operator_answer": _operator_answer(combination_rows, eligible, errors),
        "can_trade": False,
        "can_promote": False,
        "research_only": True,
    }
    report["payload_sha256"] = _report_hash(report)
    return report


def write_matrix_report(
    report: Mapping[str, Any],
    *,
    json_path: Path | str,
    csv_path: Path | str | None = None,
) -> tuple[Path, Path | None]:
    expected = report.get("payload_sha256")
    if not expected or expected != _report_hash(report):
        raise ValueError("refusing to write permutation report with invalid payload hash")
    target = Path(json_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w",
        dir=target.parent,
        prefix=target.name,
        suffix=".tmp",
        delete=False,
        encoding="utf-8",
    ) as handle:
        json.dump(dict(report), handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(target)

    written_csv: Path | None = None
    if csv_path is not None:
        written_csv = Path(csv_path)
        written_csv.parent.mkdir(parents=True, exist_ok=True)
        flattened = [_flatten_ranked_row(row) for row in report["ranked_permutations"]]
        pd.DataFrame(flattened).to_csv(written_csv, index=False)
    return target, written_csv


def evaluate_frozen_permutation_holdback(
    minute_candles: Mapping[str, pd.DataFrame],
    *,
    selection_report: Mapping[str, Any],
    permutation_id: str,
    backend: KronosBackend,
    observations: int = 60,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Open the matrix holdback once for one already-selected permutation."""

    if selection_report.get("payload_sha256") != _report_hash(selection_report):
        raise ValueError("selection report payload hash is invalid")
    matches = [
        row
        for row in selection_report.get("ranked_permutations", [])
        if row.get("permutation_id") == permutation_id
    ]
    if len(matches) != 1:
        raise ValueError(f"expected exactly one frozen permutation {permutation_id}")
    if observations < 30:
        raise ValueError("holdback evaluation requires at least 30 observations")
    candidate = matches[0]
    symbol = str(candidate["symbol"])
    timeframe = str(candidate["timeframe"])
    if symbol not in minute_candles:
        raise ValueError(f"missing minute candles for {symbol}")
    frame = aggregate_causal_candles(minute_candles[symbol], timeframe)
    frame["timestamp"] = _normalize_timestamps(frame["timestamp"])
    step = pd.Timedelta(milliseconds=TIMEFRAME_MS[timeframe])
    holdback_start = pd.Timestamp(selection_report["data_window"]["held_back_start"])
    holdback_end = pd.Timestamp(selection_report["data_window"]["held_back_end"])
    lookback = int(candidate["lookback_bars"])
    horizon = int(candidate["horizon_bars"])
    first_candidates = np.flatnonzero(((frame["timestamp"] + step) >= holdback_start).to_numpy())
    if not len(first_candidates):
        raise ValueError("holdback start is after available candles")
    first = max(lookback - 1, int(first_candidates[0]))
    last_candidates = np.flatnonzero(((frame["timestamp"] + step) <= holdback_end).to_numpy())
    if not len(last_candidates):
        raise ValueError("holdback end is before available candles")
    last = min(int(last_candidates[-1]) - horizon, len(frame) - horizon - 1)
    if last < first:
        raise ValueError("holdback cannot support candidate lookback and horizon")
    count = min(observations, last - first + 1)
    decision_indices = list(dict.fromkeys(np.linspace(first, last, num=count, dtype=int).tolist()))
    matrix_config = selection_report["matrix_config"]
    inference = KronosInferenceConfig(
        lookback_bars=lookback,
        horizon_bars=horizon,
        sample_paths=int(matrix_config["generated_sample_paths"]),
        seed=int(matrix_config["seed"]),
        temperature=float(candidate["temperature"]),
        top_p=float(candidate["top_p"]),
        device=str(matrix_config["device"]),
        local_files_only=bool(matrix_config["local_files_only"]),
    )
    contexts = [
        frame.iloc[index - lookback + 1 : index + 1].reset_index(drop=True)
        for index in decision_indices
    ]
    decisions = [frame["timestamp"].iloc[index] + step for index in decision_indices]
    generated_at = pd.Timestamp(now or datetime.now(UTC)).tz_convert("UTC")
    artifacts = generate_kronos_forecast_batch(
        contexts,
        symbols=[symbol] * len(contexts),
        timeframe=timeframe,
        decision_timestamps=decisions,
        backend=backend,
        config=inference,
        now=generated_at.to_pydatetime(),
    )
    if not all(artifact.verify() for artifact in artifacts):
        raise ValueError("holdback artifact hash verification failed")

    route = cast(ForecastRoute, candidate["route"])
    gate_config = KronosForecastGateConfig(
        min_expected_net_bps=float(candidate["min_expected_net_bps"]),
        min_confidence=float(candidate["min_confidence"]),
        min_reward_risk=float(candidate["min_reward_risk"]),
        max_horizon_bars=int(matrix_config["max_horizon_bars"]),
    )
    allowed = {f"sample_{index:03d}" for index in range(int(candidate["sample_paths"]))}
    rows: list[dict[str, Any]] = []
    for index, context, artifact in zip(decision_indices, contexts, artifacts, strict=True):
        forecast = pd.DataFrame(artifact.forecast)
        subset = forecast.loc[forecast["sample_id"].isin(allowed)].copy()
        decision = score_kronos_forecast_gate(
            context,
            subset,
            config=gate_config,
            route=route,
        )
        side = decision.selected_side
        if side not in {"long", "short"}:
            raise ValueError("holdback forecast failed to select a side")
        actual = frame.iloc[index + 1 : index + 1 + horizon].reset_index(drop=True)
        rows.append(
            {
                "artifact_id": artifact.artifact_id,
                "decision_timestamp": artifact.decision_timestamp,
                "entry_timestamp": actual["timestamp"].iloc[0].isoformat(),
                "exit_timestamp": actual["timestamp"].iloc[-1].isoformat(),
                "gate_pass": decision.verdict == "FORECAST_GATE_PASS",
                "verdict": decision.verdict,
                "side": side,
                **_actual_outcome(actual, side, gate_config.route_cost_bps(route)),
            }
        )
    accepted = [row for row in rows if row["gate_pass"]]
    accepted_metrics = _metrics(accepted)
    profit_factor = _numeric_profit_factor(accepted_metrics["profit_factor"])
    research_screen = {
        "minimum_sample": accepted_metrics["observations"] >= 30,
        "average_net_bps": accepted_metrics["avg_net_bps"] >= 3.0,
        "profit_factor": profit_factor >= 1.30,
        "sample_paths": int(candidate["sample_paths"]) >= 4,
    }
    report: dict[str, Any] = {
        "report_id": "kronos_frozen_permutation_holdback_v1",
        "generated_at": generated_at.isoformat(),
        "selection_report_sha256": selection_report["payload_sha256"],
        "permutation_id": permutation_id,
        "candidate": candidate,
        "contract": {
            "holdback_opened_once": True,
            "decision": "closed candle only",
            "entry": "next bar open",
            "exit": f"vertical barrier close after {horizon} {timeframe} bar(s)",
            "route_cost_bps": gate_config.route_cost_bps(route),
            "historically_untouched_claim": False,
            "reason": "period was reserved by this matrix but used by earlier VNEDGE research",
        },
        "window": {
            "start": holdback_start.isoformat(),
            "end": holdback_end.isoformat(),
            "first_decision": artifacts[0].decision_timestamp,
            "last_decision": artifacts[-1].decision_timestamp,
        },
        "all_forecasts": _metrics(rows),
        "accepted_holdback": accepted_metrics,
        "research_screen": {
            **research_screen,
            "passed": all(research_screen.values()),
        },
        "monthly": _period_metrics(rows, "M"),
        "rows": rows,
        "operator_answer": (
            "Research screen passed, but prior use of this historical period and the large "
            "selection search prohibit promotion. Forward shadow evidence is required."
            if all(research_screen.values())
            else "Frozen candidate failed the holdback research screen and must not be promoted."
        ),
        "can_trade": False,
        "can_promote": False,
        "research_only": True,
    }
    report["payload_sha256"] = _report_hash(report)
    return report


def write_holdback_report(report: Mapping[str, Any], path: Path | str) -> Path:
    if report.get("payload_sha256") != _report_hash(report):
        raise ValueError("refusing to write holdback report with invalid payload hash")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w",
        dir=target.parent,
        prefix=target.name,
        suffix=".tmp",
        delete=False,
        encoding="utf-8",
    ) as handle:
        json.dump(dict(report), handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(target)
    return target


def _base_specs(
    config: KronosPermutationConfig,
) -> list[tuple[str, int, int, float, float, float]]:
    specs: list[tuple[str, int, int, float, float, float]] = []
    for timeframe, lookback, temperature, top_p in itertools.product(
        config.timeframes, config.lookbacks, config.temperatures, config.top_ps
    ):
        seen_horizons: set[int] = set()
        for hours in config.horizon_hours:
            bars = min(
                config.max_horizon_bars,
                max(1, math.ceil(hours * 3_600_000 / TIMEFRAME_MS[timeframe])),
            )
            if bars in seen_horizons:
                continue
            seen_horizons.add(bars)
            specs.append((timeframe, lookback, bars, hours, temperature, top_p))
    return specs


def _decision_indices(
    rows: int,
    *,
    lookback: int,
    horizon: int,
    requested: int,
) -> list[int]:
    first = lookback - 1
    last = rows - horizon - 1
    if last < first:
        raise ValueError(
            f"insufficient selection candles: rows={rows} lookback={lookback} horizon={horizon}"
        )
    count = min(requested, last - first + 1)
    indices = np.linspace(first, last, num=count, dtype=int).tolist()
    return list(dict.fromkeys(indices))


def _score_base_permutations(
    *,
    base_id: str,
    spec: Mapping[str, Any],
    observations: list[dict[str, Any]],
    config: KronosPermutationConfig,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for sample_paths, route, min_net, min_confidence, min_rr in itertools.product(
        config.sample_path_subsets,
        config.routes,
        config.min_expected_net_bps,
        config.min_confidences,
        config.min_reward_risks,
    ):
        gate_config = KronosForecastGateConfig(
            min_expected_net_bps=min_net,
            min_confidence=min_confidence,
            min_reward_risk=min_rr,
            max_horizon_bars=config.max_horizon_bars,
        )
        outcome_rows: list[dict[str, Any]] = []
        for observation in observations:
            forecast = cast(pd.DataFrame, observation["forecast"])
            allowed = {f"sample_{index:03d}" for index in range(sample_paths)}
            subset = forecast.loc[forecast["sample_id"].isin(allowed)].copy()
            context = cast(pd.DataFrame, observation["context"])
            decision = score_kronos_forecast_gate(
                context,
                subset,
                config=gate_config,
                route=route,
            )
            side = decision.selected_side
            if side not in {"long", "short"}:
                raise ValueError("forecast permutation failed to select a side")
            actual = _actual_outcome(
                cast(pd.DataFrame, observation["actual"]),
                side,
                gate_config.route_cost_bps(route),
            )
            outcome_rows.append(
                {
                    "gate_pass": decision.verdict == "FORECAST_GATE_PASS",
                    "side": side,
                    "verdict": decision.verdict,
                    **actual,
                }
            )
        permutation = {
            **spec,
            "sample_paths": sample_paths,
            "route": route,
            "min_expected_net_bps": min_net,
            "min_confidence": min_confidence,
            "min_reward_risk": min_rr,
        }
        accepted = [row for row in outcome_rows if row["gate_pass"]]
        rows.append(
            {
                "permutation_id": _stable_id("kperm", permutation),
                "base_id": base_id,
                **permutation,
                "all_forecasts": _metrics(outcome_rows),
                "accepted_selection": _metrics(accepted),
                "verdict_counts": _counts(row["verdict"] for row in outcome_rows),
                "side_counts": _counts(row["side"] for row in outcome_rows),
            }
        )
    return rows


def _actual_outcome(actual: pd.DataFrame, side: str, cost_bps: float) -> dict[str, Any]:
    if actual.empty:
        raise ValueError("actual path is empty")
    entry = float(actual["open"].iloc[0])
    exit_price = float(actual["close"].iloc[-1])
    high = float(actual["high"].max())
    low = float(actual["low"].min())
    if side == "long":
        gross = _bps(exit_price, entry)
        favorable = max(0.0, _bps(high, entry))
        adverse = max(0.0, _bps(entry, low))
    else:
        gross = _bps(entry, exit_price)
        favorable = max(0.0, _bps(entry, low))
        adverse = max(0.0, _bps(high, entry))
    return {
        "actual_gross_bps": gross,
        "actual_net_bps": gross - cost_bps,
        "actual_mfe_bps": favorable,
        "actual_mae_bps": adverse,
        "direction_correct": gross > 0,
    }


def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "observations": 0,
            "direction_accuracy": 0.0,
            "avg_gross_bps": 0.0,
            "avg_net_bps": 0.0,
            "total_net_bps": 0.0,
            "profit_factor": 0.0,
            "avg_mfe_bps": 0.0,
            "avg_mae_bps": 0.0,
        }
    net = [float(row["actual_net_bps"]) for row in rows]
    gains = sum(value for value in net if value > 0)
    losses = abs(sum(value for value in net if value < 0))
    profit_factor = gains / losses if losses else (math.inf if gains else 0.0)
    return {
        "observations": len(rows),
        "direction_accuracy": round(
            sum(bool(row["direction_correct"]) for row in rows) / len(rows), 6
        ),
        "avg_gross_bps": round(sum(float(row["actual_gross_bps"]) for row in rows) / len(rows), 6),
        "avg_net_bps": round(sum(net) / len(rows), 6),
        "total_net_bps": round(sum(net), 6),
        "profit_factor": round(profit_factor, 6) if math.isfinite(profit_factor) else "Infinity",
        "avg_mfe_bps": round(sum(float(row["actual_mfe_bps"]) for row in rows) / len(rows), 6),
        "avg_mae_bps": round(sum(float(row["actual_mae_bps"]) for row in rows) / len(rows), 6),
    }


def _period_metrics(rows: list[dict[str, Any]], frequency: str) -> list[dict[str, Any]]:
    """Summarize all forecasts and accepted forecasts in calendar periods."""

    if not rows:
        return []
    frame = pd.DataFrame(rows)
    timestamps = pd.to_datetime(frame["decision_timestamp"], utc=True)
    frame["period"] = timestamps.dt.tz_localize(None).dt.to_period(frequency).astype(str)
    summaries: list[dict[str, Any]] = []
    for period, group in frame.groupby("period", sort=True):
        period_rows = group.drop(columns="period").to_dict(orient="records")
        accepted = [row for row in period_rows if bool(row["gate_pass"])]
        summaries.append(
            {
                "period": str(period),
                "all_forecasts": _metrics(period_rows),
                "accepted": _metrics(accepted),
            }
        )
    return summaries


def _counts(values: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        key = str(value)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _operator_answer(
    rows: list[dict[str, Any]],
    eligible: list[dict[str, Any]],
    errors: list[dict[str, Any]],
) -> str:
    if errors:
        return (
            f"Matrix incomplete: {len(errors)} base run(s) failed. Results cannot be used "
            "for selection until every declared run succeeds."
        )
    if eligible:
        return (
            f"{len(eligible)} selection-window permutation(s) clear the exploratory PF/net/sample "
            "screen. They remain multiple-tested research candidates; the holdback was not opened."
        )
    positive = sum(
        row["accepted_selection"]["observations"] > 0
        and row["accepted_selection"]["avg_net_bps"] > 0
        for row in rows
    )
    return (
        f"No permutation clears the minimum economic sample and PF/net screen. "
        f"{positive} sparse row(s) are positive but are not promotion evidence."
    )


def _flatten_ranked_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return (
        {
            key: value
            for key, value in row.items()
            if key not in {"all_forecasts", "accepted_selection", "verdict_counts", "side_counts"}
        }
        | {f"all_{key}": value for key, value in row["all_forecasts"].items()}
        | {f"accepted_{key}": value for key, value in row["accepted_selection"].items()}
    )


def _numeric_profit_factor(value: Any) -> float:
    if value == "Infinity":
        return math.inf
    return float(value)


def _bps(a: float, b: float) -> float:
    return ((a - b) / b) * 10_000.0


def _normalize_timestamps(values: pd.Series) -> pd.Series:
    if pd.api.types.is_numeric_dtype(values):
        finite = pd.to_numeric(values, errors="coerce")
        magnitude = float(finite.dropna().abs().median())
        unit = "us" if magnitude >= 1e14 else "ms" if magnitude >= 1e11 else "s"
        return pd.to_datetime(finite, unit=unit, utc=True)
    return pd.to_datetime(values, utc=True)


def _stable_id(prefix: str, payload: Mapping[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return f"{prefix}_{hashlib.sha256(blob).hexdigest()[:20]}"


def _report_hash(payload: Mapping[str, Any]) -> str:
    unsigned = {key: value for key, value in payload.items() if key != "payload_sha256"}
    blob = json.dumps(unsigned, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(blob).hexdigest()


def _csv_tuple(value: str, cast_type: Any) -> tuple[Any, ...]:
    return tuple(cast_type(item.strip()) for item in value.split(",") if item.strip())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--csv-out", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--kronos-repo", type=Path, default=DEFAULT_KRONOS_REPO)
    parser.add_argument("--symbols", default=",".join(DEFAULT_MATRIX_CONFIG.symbols))
    parser.add_argument("--timeframes", default=",".join(ALL_TIMEFRAMES))
    parser.add_argument("--lookbacks", default="64,128")
    parser.add_argument("--horizon-hours", default="1,4,12")
    parser.add_argument("--temperatures", default="1.0")
    parser.add_argument("--top-ps", default="0.9")
    parser.add_argument("--samples", type=int, default=4)
    parser.add_argument("--sample-subsets", default="1,4")
    parser.add_argument("--observations", type=int, default=12)
    parser.add_argument("--holdback-fraction", type=float, default=0.20)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--allow-model-download", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = KronosPermutationConfig(
        symbols=cast(tuple[str, ...], _csv_tuple(args.symbols, str)),
        timeframes=cast(tuple[str, ...], _csv_tuple(args.timeframes, str)),
        lookbacks=cast(tuple[int, ...], _csv_tuple(args.lookbacks, int)),
        horizon_hours=cast(tuple[float, ...], _csv_tuple(args.horizon_hours, float)),
        temperatures=cast(tuple[float, ...], _csv_tuple(args.temperatures, float)),
        top_ps=cast(tuple[float, ...], _csv_tuple(args.top_ps, float)),
        generated_sample_paths=args.samples,
        sample_path_subsets=cast(tuple[int, ...], _csv_tuple(args.sample_subsets, int)),
        observations_per_base=args.observations,
        holdback_fraction=args.holdback_fraction,
        seed=args.seed,
        device=args.device,
        local_files_only=not args.allow_model_download,
    )
    minute_candles = {
        symbol: load_canonical_minute_cache(symbol, cache_dir=args.cache_dir)
        for symbol in config.symbols
    }
    backend_config = KronosInferenceConfig(
        lookback_bars=max(config.lookbacks),
        horizon_bars=max(config.max_horizon_bars, 1),
        sample_paths=config.generated_sample_paths,
        seed=config.seed,
        device=config.device,
        local_files_only=config.local_files_only,
    )
    backend = UpstreamKronosBackend(repo=args.kronos_repo, config=backend_config)
    report = run_kronos_permutation_matrix(
        minute_candles,
        backend=backend,
        config=config,
        progress=True,
    )
    write_matrix_report(report, json_path=args.out, csv_path=args.csv_out)
    print(
        json.dumps(
            {
                "report": str(args.out),
                "ranked_csv": str(args.csv_out),
                "completion": report["completion"],
                "operator_answer": report["operator_answer"],
                "payload_sha256": report["payload_sha256"],
                "can_trade": False,
                "can_promote": False,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if report["completion"]["complete"] else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
