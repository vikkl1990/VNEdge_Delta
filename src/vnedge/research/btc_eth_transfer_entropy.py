"""Selection-only rolling transfer-entropy study for synchronized BTC and ETH."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal

import numpy as np
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from vnedge.research.btc_eth_lead_lag_causal_discovery import (
    ReturnFrame,
    benjamini_hochberg,
    synchronized_returns,
)
from vnedge.research.delta_scalper_backtest import _load_candles

DEFAULT_CONFIG = Path("configs/research/btc_eth_transfer_entropy_v1.yaml")
DEFAULT_CACHE = Path("data/delta_scalper_cache")
DEFAULT_OUTPUT = Path("research/live_research/btc_eth_transfer_entropy_v1_latest.json")


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TransferEntropyDataConfig(_FrozenModel):
    leader_symbol: Literal["BTCUSD"]
    follower_symbol: Literal["ETHUSD"]
    start: datetime
    end: datetime
    sealed_tail_start: datetime
    source_timeframe: Literal["1m"]
    require_exact_timestamp_match: Literal[True]
    require_complete_5m_buckets: Literal[True]
    sealed_tail_access_forbidden: Literal[True]

    @model_validator(mode="after")
    def validate_boundary(self) -> TransferEntropyDataConfig:
        timestamps = (self.start, self.end, self.sealed_tail_start)
        if any(value.tzinfo is None for value in timestamps):
            raise ValueError("all study timestamps must be timezone-aware")
        if not self.start < self.end < self.sealed_tail_start:
            raise ValueError("study end must precede the sealed tail")
        return self


class TransferEntropyEncodingConfig(_FrozenModel):
    method: Literal["within_window_quantile_tertiles"]
    states: Literal[3]
    target: Literal["log_return"]
    history_lengths: tuple[Literal[1, 2, 3], ...]
    forecast_horizon_bars: Literal[1]

    @model_validator(mode="after")
    def validate_history(self) -> TransferEntropyEncodingConfig:
        if self.history_lengths != (1, 2, 3):
            raise ValueError("v1 histories are frozen at 1, 2, and 3 bars")
        return self


class TransferEntropyRollingConfig(_FrozenModel):
    timeframes_minutes: tuple[Literal[1, 5], ...]
    directions: tuple[Literal["btc_to_eth", "eth_to_btc"], ...]
    window_days: int = Field(ge=30)
    step_days: int = Field(ge=1)
    minimum_observations: int = Field(ge=1000)

    @model_validator(mode="after")
    def validate_grid(self) -> TransferEntropyRollingConfig:
        if self.timeframes_minutes != (1, 5):
            raise ValueError("v1 timeframes are frozen at 1m and 5m")
        if self.directions != ("btc_to_eth", "eth_to_btc"):
            raise ValueError("both directions are required")
        return self


class TransferEntropySurrogateConfig(_FrozenModel):
    method: Literal["segmentwise_circular_shift"]
    count: int = Field(ge=19)
    random_seed: int
    minimum_shift_bars: int = Field(ge=4)
    preserve_source_autocorrelation: Literal[True]
    preserve_gap_segments: Literal[True]


class TransferEntropyStatisticsConfig(_FrozenModel):
    units: Literal["bits"]
    estimator: Literal["discrete_plugin_with_surrogate_bias_correction"]
    alpha: float = Field(gt=0.0, lt=0.1)
    multiple_test_correction: Literal["benjamini_hochberg_per_rolling_window"]
    full_period_result_role: Literal["descriptive_only"]


class TransferEntropyAdvancementConfig(_FrozenModel):
    minimum_valid_rolling_windows: int = Field(ge=2)
    minimum_corrected_significant_fraction: float = Field(ge=0.0, le=1.0)
    minimum_positive_effective_te_fraction: float = Field(ge=0.0, le=1.0)
    minimum_median_effective_te_bits: float = Field(ge=0.0)
    minimum_median_normalized_effective_te: float = Field(ge=0.0)
    minimum_directionality_ratio: float = Field(ge=1.0)
    positive_result_authorizes: Literal["preregistration_of_a_distinct_v2_only"]
    scanner_backtest_authorized: Literal[False]
    old_v1_tail_may_open: Literal[False]


class TransferEntropyConfig(_FrozenModel):
    study_id: Literal["btc_eth_transfer_entropy_v1"]
    status: Literal["preregistered"]
    research_only: Literal[True]
    can_trade: Literal[False]
    can_promote: Literal[False]
    data: TransferEntropyDataConfig
    encoding: TransferEntropyEncodingConfig
    rolling: TransferEntropyRollingConfig
    surrogates: TransferEntropySurrogateConfig
    statistics: TransferEntropyStatisticsConfig
    advancement: TransferEntropyAdvancementConfig
    policy: dict[str, str]


@dataclass(frozen=True)
class DiscreteWindow:
    timestamps: np.ndarray
    btc: np.ndarray
    eth: np.ndarray
    segment: np.ndarray


def load_transfer_entropy_config(path: Path | str) -> TransferEntropyConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return TransferEntropyConfig.model_validate(raw)


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, default=str, allow_nan=False)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def _quantile_states(values: np.ndarray) -> np.ndarray:
    edges = np.quantile(values, [1.0 / 3.0, 2.0 / 3.0])
    return np.searchsorted(edges, values, side="right").astype(np.int8)


def _discrete_window(frame: ReturnFrame, start: datetime, end: datetime) -> DiscreteWindow:
    start64 = np.datetime64(start.astimezone(UTC).replace(tzinfo=None), "ns")
    end64 = np.datetime64(end.astimezone(UTC).replace(tzinfo=None), "ns")
    mask = (frame.timestamps >= start64) & (frame.timestamps < end64)
    btc = frame.btc[mask]
    eth = frame.eth[mask]
    return DiscreteWindow(
        frame.timestamps[mask],
        _quantile_states(btc),
        _quantile_states(eth),
        frame.segment[mask],
    )


def _history_code(
    values: np.ndarray, positions: np.ndarray, history: int, states: int
) -> np.ndarray:
    code = np.zeros(len(positions), dtype=np.int64)
    multiplier = 1
    for lag in range(1, history + 1):
        code += values[positions - lag].astype(np.int64) * multiplier
        multiplier *= states
    return code


def _entropy(code: np.ndarray) -> float:
    counts = np.bincount(code)
    probabilities = counts[counts > 0].astype(float) / len(code)
    return float(-np.sum(probabilities * np.log2(probabilities)))


def transfer_entropy_bits(
    source: np.ndarray,
    target: np.ndarray,
    segment: np.ndarray,
    history: int,
    states: int = 3,
) -> tuple[float, float, int]:
    positions = np.arange(history, len(target))
    positions = positions[segment[positions] == segment[positions - history]]
    if not len(positions):
        return 0.0, 0.0, 0
    future = target[positions].astype(np.int64)
    target_past = _history_code(target, positions, history, states)
    source_past = _history_code(source, positions, history, states)
    past_states = states**history
    future_target = future + states * target_past
    target_source = target_past + past_states * source_past
    all_states = future + states * target_past + states * past_states * source_past
    h_future_target = _entropy(future_target)
    h_target_source = _entropy(target_source)
    h_target = _entropy(target_past)
    h_all = _entropy(all_states)
    te = max(0.0, h_future_target + h_target_source - h_target - h_all)
    conditional_entropy = max(0.0, h_future_target - h_target)
    return te, conditional_entropy, len(positions)


def _segmentwise_shift(
    values: np.ndarray,
    segment: np.ndarray,
    rng: np.random.Generator,
    minimum_shift: int,
) -> np.ndarray:
    shifted = values.copy()
    for segment_id in np.unique(segment):
        positions = np.flatnonzero(segment == segment_id)
        size = len(positions)
        if size <= 2 * minimum_shift:
            shifted[positions] = values[positions][::-1]
            continue
        offset = int(rng.integers(minimum_shift, size - minimum_shift + 1))
        shifted[positions] = np.roll(values[positions], offset)
    return shifted


def transfer_entropy_test(
    window: DiscreteWindow,
    direction: Literal["btc_to_eth", "eth_to_btc"],
    history: int,
    surrogate_count: int,
    seed: int,
    minimum_shift: int,
) -> dict[str, object]:
    source = window.btc if direction == "btc_to_eth" else window.eth
    target = window.eth if direction == "btc_to_eth" else window.btc
    observed, conditional_entropy, observations = transfer_entropy_bits(
        source, target, window.segment, history
    )
    rng = np.random.default_rng(seed)
    null = np.empty(surrogate_count, dtype=float)
    for index in range(surrogate_count):
        surrogate = _segmentwise_shift(source, window.segment, rng, minimum_shift)
        null[index] = transfer_entropy_bits(surrogate, target, window.segment, history)[0]
    null_mean = float(np.mean(null))
    null_std = float(np.std(null, ddof=1)) if len(null) > 1 else 0.0
    effective = observed - null_mean
    empirical_p = (1 + int(np.sum(null >= observed))) / (surrogate_count + 1)
    return {
        "direction": direction,
        "history": history,
        "observations": observations,
        "observed_te_bits": observed,
        "surrogate_mean_te_bits": null_mean,
        "surrogate_std_te_bits": null_std,
        "effective_te_bits": effective,
        "normalized_effective_te": (
            effective / conditional_entropy if conditional_entropy > 0 else 0.0
        ),
        "target_conditional_entropy_bits": conditional_entropy,
        "z_score": (observed - null_mean) / null_std if null_std > 0 else None,
        "empirical_p_value": empirical_p,
    }


def _rolling_windows(config: TransferEntropyConfig) -> list[tuple[datetime, datetime]]:
    windows: list[tuple[datetime, datetime]] = []
    cursor = config.data.start.astimezone(UTC)
    limit = config.data.end.astimezone(UTC)
    width = timedelta(days=config.rolling.window_days)
    step = timedelta(days=config.rolling.step_days)
    while cursor + width <= limit:
        windows.append((cursor, cursor + width))
        cursor += step
    return windows


def _cell_seed(base: int, timeframe: int, direction: str, history: int, window: int) -> int:
    direction_offset = 0 if direction == "btc_to_eth" else 500_000
    return base + timeframe * 10_000 + direction_offset + history * 100 + window


def _summaries(
    rows: list[dict[str, object]], config: TransferEntropyConfig
) -> list[dict[str, object]]:
    summaries: list[dict[str, object]] = []
    alpha = config.statistics.alpha
    for timeframe in config.rolling.timeframes_minutes:
        for direction in config.rolling.directions:
            for history in config.encoding.history_lengths:
                selected = [
                    row
                    for row in rows
                    if row["timeframe_minutes"] == timeframe
                    and row["direction"] == direction
                    and row["history"] == history
                ]
                effects = [float(row["effective_te_bits"]) for row in selected]
                normalized = [float(row["normalized_effective_te"]) for row in selected]
                summaries.append(
                    {
                        "timeframe_minutes": timeframe,
                        "direction": direction,
                        "history": history,
                        "valid_windows": len(selected),
                        "corrected_significant_fraction": (
                            sum(float(row["adjusted_p_value"]) <= alpha for row in selected)
                            / len(selected)
                            if selected
                            else 0.0
                        ),
                        "positive_effective_te_fraction": (
                            sum(value > 0 for value in effects) / len(effects) if effects else 0.0
                        ),
                        "median_effective_te_bits": (float(np.median(effects)) if effects else 0.0),
                        "median_normalized_effective_te": (
                            float(np.median(normalized)) if normalized else 0.0
                        ),
                    }
                )
    lookup = {
        (row["timeframe_minutes"], row["direction"], row["history"]): row for row in summaries
    }
    gates = config.advancement
    for row in summaries:
        reverse_direction = "eth_to_btc" if row["direction"] == "btc_to_eth" else "btc_to_eth"
        reverse = lookup[(row["timeframe_minutes"], reverse_direction, row["history"])]
        own = float(row["median_effective_te_bits"])
        reverse_effect = float(reverse["median_effective_te_bits"])
        ratio = own / reverse_effect if reverse_effect > 0 else None
        directionality = own > 0 if ratio is None else ratio >= gates.minimum_directionality_ratio
        checks = {
            "minimum_windows": int(row["valid_windows"]) >= gates.minimum_valid_rolling_windows,
            "corrected_significance": float(row["corrected_significant_fraction"])
            >= gates.minimum_corrected_significant_fraction,
            "positive_effect_stability": float(row["positive_effective_te_fraction"])
            >= gates.minimum_positive_effective_te_fraction,
            "effective_te_size": own >= gates.minimum_median_effective_te_bits,
            "normalized_effect_size": float(row["median_normalized_effective_te"])
            >= gates.minimum_median_normalized_effective_te,
            "directionality": directionality,
            "required_direction": row["direction"] == "btc_to_eth",
        }
        row["directionality_ratio"] = ratio
        row["advancement_checks"] = checks
        row["advances"] = all(checks.values())
    return summaries


async def run(args: argparse.Namespace) -> dict[str, object]:
    config_path = Path(args.config)
    config = load_transfer_entropy_config(config_path)
    start = config.data.start.astimezone(UTC)
    end = config.data.end.astimezone(UTC)
    if end >= config.data.sealed_tail_start.astimezone(UTC):
        raise ValueError("transfer-entropy window would open the sealed v1 tail")
    cache_dir = Path(args.cache_dir)
    btc, eth = await asyncio.gather(
        _load_candles(config.data.leader_symbol, start, end, cache_dir=cache_dir, refresh=False),
        _load_candles(config.data.follower_symbol, start, end, cache_dir=cache_dir, refresh=False),
    )
    frames = {
        timeframe: synchronized_returns(btc, eth, timeframe)
        for timeframe in config.rolling.timeframes_minutes
    }
    windows = _rolling_windows(config)
    rolling_rows: list[dict[str, object]] = []
    for window_index, (window_start, window_end) in enumerate(windows):
        window_rows: list[dict[str, object]] = []
        for timeframe, frame in frames.items():
            discrete = _discrete_window(frame, window_start, window_end)
            if len(discrete.btc) < config.rolling.minimum_observations:
                continue
            for direction in config.rolling.directions:
                for history in config.encoding.history_lengths:
                    row = transfer_entropy_test(
                        discrete,
                        direction,
                        history,
                        config.surrogates.count,
                        _cell_seed(
                            config.surrogates.random_seed,
                            timeframe,
                            direction,
                            history,
                            window_index,
                        ),
                        config.surrogates.minimum_shift_bars,
                    )
                    row.update(
                        {
                            "timeframe_minutes": timeframe,
                            "window_start": window_start.isoformat(),
                            "window_end": window_end.isoformat(),
                        }
                    )
                    window_rows.append(row)
        adjusted = benjamini_hochberg([float(row["empirical_p_value"]) for row in window_rows])
        for row, adjusted_p in zip(window_rows, adjusted, strict=True):
            row["adjusted_p_value"] = adjusted_p
        rolling_rows.extend(window_rows)
    summaries = _summaries(rolling_rows, config)
    descriptive: list[dict[str, object]] = []
    for timeframe, frame in frames.items():
        discrete = _discrete_window(frame, start, end)
        for direction in config.rolling.directions:
            for history in config.encoding.history_lengths:
                row = transfer_entropy_test(
                    discrete,
                    direction,
                    history,
                    config.surrogates.count,
                    _cell_seed(
                        config.surrogates.random_seed,
                        timeframe,
                        direction,
                        history,
                        len(windows) + 1,
                    ),
                    config.surrogates.minimum_shift_bars,
                )
                row["timeframe_minutes"] = timeframe
                descriptive.append(row)
    descriptive_adjusted = benjamini_hochberg(
        [float(row["empirical_p_value"]) for row in descriptive]
    )
    for row, adjusted_p in zip(descriptive, descriptive_adjusted, strict=True):
        row["adjusted_p_value"] = adjusted_p
    payload: dict[str, object] = {
        "study_id": config.study_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "config": str(config_path),
        "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "sealed_tail": {
            "starts_at": config.data.sealed_tail_start.astimezone(UTC).isoformat(),
            "accessed": False,
            "predictions_computed": False,
            "trades_computed": False,
        },
        "data": {
            str(timeframe): {
                "return_observations": len(frame.btc),
                "segments": len(np.unique(frame.segment)),
            }
            for timeframe, frame in frames.items()
        },
        "rolling_windows": len(windows),
        "rolling_results": rolling_rows,
        "summaries": summaries,
        "descriptive_full_period": descriptive,
        "advancement": {
            "passed": any(bool(row["advances"]) for row in summaries),
            "eligible_cells": [
                {
                    "timeframe_minutes": row["timeframe_minutes"],
                    "direction": row["direction"],
                    "history": row["history"],
                }
                for row in summaries
                if row["advances"]
            ],
            "authorizes": config.advancement.positive_result_authorizes,
            "scanner_backtest_authorized": False,
        },
        "safety": {
            "research_only": True,
            "can_trade": False,
            "can_promote": False,
            "old_v1_tail_may_open": False,
            "structural_causation_claimed": False,
            "transaction_cost_claimed": False,
        },
    }
    _atomic_json(Path(args.output), payload)
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    return parser


def main() -> None:
    payload = asyncio.run(run(_parser().parse_args()))
    print(
        json.dumps(
            {
                "study_id": payload["study_id"],
                "window": payload["window"],
                "sealed_tail": payload["sealed_tail"],
                "data": payload["data"],
                "rolling_windows": payload["rolling_windows"],
                "summaries": payload["summaries"],
                "advancement": payload["advancement"],
            },
            indent=2,
            default=str,
            allow_nan=False,
        )
    )


if __name__ == "__main__":
    main()
