"""Selection-only rolling Granger diagnostics for BTC and ETH.

This module never loads or scores the sealed v1 tail. A positive result may
justify writing a distinct preregistered v2 contract; it cannot enable trading.
"""

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
import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from scipy.stats import f as f_distribution

from vnedge.research.delta_scalper_backtest import _load_candles
from vnedge.scalping.delta_engine.types import Candle

DEFAULT_CONFIG = Path("configs/research/btc_eth_lead_lag_causal_discovery_v1.yaml")
DEFAULT_CACHE = Path("data/delta_scalper_cache")
DEFAULT_OUTPUT = Path("research/live_research/btc_eth_lead_lag_causal_discovery_v1_latest.json")


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DiscoveryDataConfig(_FrozenModel):
    leader_symbol: Literal["BTCUSD"] = "BTCUSD"
    follower_symbol: Literal["ETHUSD"] = "ETHUSD"
    start: datetime
    end: datetime
    sealed_tail_start: datetime
    source_timeframe: Literal["1m"] = "1m"
    require_exact_timestamp_match: Literal[True] = True
    require_complete_5m_buckets: Literal[True] = True
    sealed_tail_access_forbidden: Literal[True] = True

    @model_validator(mode="after")
    def validate_boundary(self) -> DiscoveryDataConfig:
        if (
            self.start.tzinfo is None
            or self.end.tzinfo is None
            or self.sealed_tail_start.tzinfo is None
        ):
            raise ValueError("all discovery timestamps must be timezone-aware")
        if not self.start < self.end < self.sealed_tail_start:
            raise ValueError("study end must precede the sealed tail")
        return self


class DiscoveryDesignConfig(_FrozenModel):
    timeframes_minutes: tuple[Literal[1, 5], ...]
    directions: tuple[Literal["btc_to_eth", "eth_to_btc"], ...]
    maximum_lags: tuple[int, ...]
    rolling_window_days: int = Field(ge=30)
    rolling_step_days: int = Field(ge=1)
    train_fraction: float = Field(gt=0.5, lt=0.9)
    minimum_observations_per_window: int = Field(ge=100)
    include_intercept: Literal[True] = True
    full_period_result_role: Literal["descriptive_only"] = "descriptive_only"

    @model_validator(mode="after")
    def validate_grid(self) -> DiscoveryDesignConfig:
        if self.timeframes_minutes != (1, 5):
            raise ValueError("v1 timeframes are frozen at 1m and 5m")
        if self.directions != ("btc_to_eth", "eth_to_btc"):
            raise ValueError("both frozen directions are required")
        if self.maximum_lags != (1, 2, 3, 6):
            raise ValueError("v1 maximum lags are frozen at 1, 2, 3, and 6")
        return self


class DiscoveryStatisticsConfig(_FrozenModel):
    alpha: float = Field(gt=0.0, lt=0.05)
    multiple_test_correction: Literal["benjamini_hochberg_per_rolling_window"] = (
        "benjamini_hochberg_per_rolling_window"
    )
    test: Literal["nested_ols_f_test"] = "nested_ols_f_test"
    target: Literal["log_return"] = "log_return"
    economic_check: Literal["chronological_out_of_sample_mse_and_sign_accuracy"] = (
        "chronological_out_of_sample_mse_and_sign_accuracy"
    )


class DiscoveryAdvancementConfig(_FrozenModel):
    minimum_valid_rolling_windows: int = Field(ge=2)
    minimum_corrected_significant_fraction: float = Field(ge=0.0, le=1.0)
    minimum_positive_oos_fraction: float = Field(ge=0.0, le=1.0)
    minimum_median_oos_mse_improvement_pct: float
    minimum_median_sign_accuracy_uplift_pp: float
    minimum_directionality_ratio: float = Field(ge=1.0)
    positive_result_authorizes: Literal["preregistration_of_a_distinct_v2_only"] = (
        "preregistration_of_a_distinct_v2_only"
    )
    scanner_backtest_authorized: Literal[False] = False
    old_v1_tail_may_open: Literal[False] = False


class CausalDiscoveryConfig(_FrozenModel):
    study_id: Literal["btc_eth_lead_lag_causal_discovery_v1"]
    status: Literal["preregistered"]
    research_only: Literal[True]
    can_trade: Literal[False]
    can_promote: Literal[False]
    data: DiscoveryDataConfig
    design: DiscoveryDesignConfig
    statistics: DiscoveryStatisticsConfig
    advancement: DiscoveryAdvancementConfig
    policy: dict[str, str]


@dataclass(frozen=True)
class ReturnFrame:
    timestamps: np.ndarray
    btc: np.ndarray
    eth: np.ndarray
    segment: np.ndarray


@dataclass(frozen=True)
class LaggedDesign:
    timestamps: np.ndarray
    target: np.ndarray
    restricted: np.ndarray
    unrestricted: np.ndarray


def load_discovery_config(path: Path | str) -> CausalDiscoveryConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return CausalDiscoveryConfig.model_validate(raw)


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def synchronized_returns(
    btc: list[Candle],
    eth: list[Candle],
    timeframe_minutes: Literal[1, 5],
) -> ReturnFrame:
    btc_close = {row.ts: row.close for row in btc}
    eth_close = {row.ts: row.close for row in eth}
    timestamps = sorted(btc_close.keys() & eth_close.keys())
    if not timestamps:
        return ReturnFrame(np.array([], dtype="datetime64[ns]"), *(np.array([]),) * 3)
    frame = pd.DataFrame(
        {
            "btc": [btc_close[ts] for ts in timestamps],
            "eth": [eth_close[ts] for ts in timestamps],
        },
        index=pd.DatetimeIndex(timestamps),
    )
    if timeframe_minutes == 5:
        bucket = (frame.index - pd.Timedelta(minutes=1)).floor("5min") + pd.Timedelta(minutes=5)
        grouped = frame.assign(bucket=bucket, source_ts=frame.index).groupby("bucket", sort=True)
        counts = grouped.size()
        spans = grouped["source_ts"].max() - grouped["source_ts"].min()
        complete = counts.index[(counts == 5) & (spans == pd.Timedelta(minutes=4))]
        frame = grouped.last().loc[complete, ["btc", "eth"]]
    expected = pd.Timedelta(minutes=timeframe_minutes)
    contiguous = frame.index.to_series().diff().eq(expected)
    segment = (~contiguous).cumsum().to_numpy(dtype=np.int64)
    returns = np.log(frame[["btc", "eth"]]).diff()
    keep = contiguous.to_numpy() & returns.notna().all(axis=1).to_numpy()
    return ReturnFrame(
        timestamps=frame.index.to_numpy(dtype="datetime64[ns]")[keep],
        btc=returns["btc"].to_numpy(dtype=float)[keep],
        eth=returns["eth"].to_numpy(dtype=float)[keep],
        segment=segment[keep],
    )


def _lagged_design(
    frame: ReturnFrame,
    direction: Literal["btc_to_eth", "eth_to_btc"],
    maximum_lag: int,
) -> LaggedDesign:
    source = frame.btc if direction == "btc_to_eth" else frame.eth
    target = frame.eth if direction == "btc_to_eth" else frame.btc
    positions = np.arange(maximum_lag, len(target))
    valid = frame.segment[positions] == frame.segment[positions - maximum_lag]
    positions = positions[valid]
    y = target[positions]
    own = np.column_stack([target[positions - lag] for lag in range(1, maximum_lag + 1)])
    cross = np.column_stack([source[positions - lag] for lag in range(1, maximum_lag + 1)])
    restricted = np.column_stack([np.ones(len(y)), own])
    unrestricted = np.column_stack([restricted, cross])
    return LaggedDesign(frame.timestamps[positions], y, restricted, unrestricted)


def _ols_fit(x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, float, int]:
    beta, _, rank, _ = np.linalg.lstsq(x, y, rcond=None)
    residual = y - x @ beta
    return beta, float(residual @ residual), int(rank)


def granger_window(
    frame: ReturnFrame,
    direction: Literal["btc_to_eth", "eth_to_btc"],
    maximum_lag: int,
    start: datetime,
    end: datetime,
    train_fraction: float,
    minimum_observations: int,
) -> dict[str, object] | None:
    return granger_design_window(
        _lagged_design(frame, direction, maximum_lag),
        direction,
        maximum_lag,
        start,
        end,
        train_fraction,
        minimum_observations,
    )


def granger_design_window(
    design: LaggedDesign,
    direction: Literal["btc_to_eth", "eth_to_btc"],
    maximum_lag: int,
    start: datetime,
    end: datetime,
    train_fraction: float,
    minimum_observations: int,
) -> dict[str, object] | None:
    start64 = np.datetime64(start.astimezone(UTC).replace(tzinfo=None), "ns")
    end64 = np.datetime64(end.astimezone(UTC).replace(tzinfo=None), "ns")
    mask = (design.timestamps >= start64) & (design.timestamps < end64)
    y = design.target[mask]
    restricted = design.restricted[mask]
    unrestricted = design.unrestricted[mask]
    if len(y) < minimum_observations:
        return None
    split = int(len(y) * train_fraction)
    if split <= unrestricted.shape[1] or len(y) - split < 100:
        return None
    y_train, y_test = y[:split], y[split:]
    r_train, r_test = restricted[:split], restricted[split:]
    u_train, u_test = unrestricted[:split], unrestricted[split:]
    beta_r, rss_r, rank_r = _ols_fit(r_train, y_train)
    beta_u, rss_u, rank_u = _ols_fit(u_train, y_train)
    restrictions = rank_u - rank_r
    residual_dof = len(y_train) - rank_u
    if restrictions <= 0 or residual_dof <= 0 or rss_u <= 0 or rss_r < rss_u:
        return None
    f_stat = ((rss_r - rss_u) / restrictions) / (rss_u / residual_dof)
    p_value = float(f_distribution.sf(f_stat, restrictions, residual_dof))
    prediction_r = r_test @ beta_r
    prediction_u = u_test @ beta_u
    mse_r = float(np.mean(np.square(y_test - prediction_r)))
    mse_u = float(np.mean(np.square(y_test - prediction_u)))
    mse_improvement = 100.0 * (mse_r - mse_u) / mse_r if mse_r > 0 else 0.0
    sign_r = float(np.mean(np.sign(prediction_r) == np.sign(y_test)))
    sign_u = float(np.mean(np.sign(prediction_u) == np.sign(y_test)))
    return {
        "direction": direction,
        "maximum_lag": maximum_lag,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "observations": len(y),
        "train_observations": len(y_train),
        "test_observations": len(y_test),
        "f_statistic": float(f_stat),
        "p_value": p_value,
        "train_incremental_r2": float(1.0 - rss_u / rss_r) if rss_r else 0.0,
        "oos_mse_restricted": mse_r,
        "oos_mse_unrestricted": mse_u,
        "oos_mse_improvement_pct": mse_improvement,
        "oos_sign_accuracy_restricted": sign_r,
        "oos_sign_accuracy_unrestricted": sign_u,
        "oos_sign_accuracy_uplift_pp": 100.0 * (sign_u - sign_r),
    }


def benjamini_hochberg(p_values: list[float]) -> list[float]:
    if not p_values:
        return []
    values = np.asarray(p_values, dtype=float)
    order = np.argsort(values)
    ranked = values[order]
    adjusted_ranked = ranked * len(values) / np.arange(1, len(values) + 1)
    adjusted_ranked = np.minimum.accumulate(adjusted_ranked[::-1])[::-1]
    adjusted = np.empty_like(adjusted_ranked)
    adjusted[order] = np.minimum(adjusted_ranked, 1.0)
    return adjusted.tolist()


def _rolling_windows(config: CausalDiscoveryConfig) -> list[tuple[datetime, datetime]]:
    windows: list[tuple[datetime, datetime]] = []
    cursor = config.data.start.astimezone(UTC)
    end_limit = config.data.end.astimezone(UTC)
    width = timedelta(days=config.design.rolling_window_days)
    step = timedelta(days=config.design.rolling_step_days)
    while cursor + width <= end_limit:
        windows.append((cursor, cursor + width))
        cursor += step
    return windows


def _summaries(
    rows: list[dict[str, object]], config: CausalDiscoveryConfig
) -> list[dict[str, object]]:
    summaries: list[dict[str, object]] = []
    alpha = config.statistics.alpha
    for timeframe in config.design.timeframes_minutes:
        for direction in config.design.directions:
            for lag in config.design.maximum_lags:
                selected = [
                    row
                    for row in rows
                    if row["timeframe_minutes"] == timeframe
                    and row["direction"] == direction
                    and row["maximum_lag"] == lag
                ]
                improvements = [float(row["oos_mse_improvement_pct"]) for row in selected]
                sign_uplifts = [float(row["oos_sign_accuracy_uplift_pp"]) for row in selected]
                summaries.append(
                    {
                        "timeframe_minutes": timeframe,
                        "direction": direction,
                        "maximum_lag": lag,
                        "valid_windows": len(selected),
                        "corrected_significant_fraction": (
                            sum(float(row["adjusted_p_value"]) <= alpha for row in selected)
                            / len(selected)
                            if selected
                            else 0.0
                        ),
                        "positive_oos_fraction": (
                            sum(value > 0 for value in improvements) / len(improvements)
                            if improvements
                            else 0.0
                        ),
                        "median_oos_mse_improvement_pct": (
                            float(np.median(improvements)) if improvements else 0.0
                        ),
                        "median_sign_accuracy_uplift_pp": (
                            float(np.median(sign_uplifts)) if sign_uplifts else 0.0
                        ),
                        "median_train_incremental_r2": (
                            float(
                                np.median([float(row["train_incremental_r2"]) for row in selected])
                            )
                            if selected
                            else 0.0
                        ),
                    }
                )
    lookup = {
        (row["timeframe_minutes"], row["direction"], row["maximum_lag"]): row for row in summaries
    }
    threshold = config.advancement
    for row in summaries:
        reverse_direction = "eth_to_btc" if row["direction"] == "btc_to_eth" else "btc_to_eth"
        reverse = lookup[(row["timeframe_minutes"], reverse_direction, row["maximum_lag"])]
        own = float(row["median_oos_mse_improvement_pct"])
        reverse_value = float(reverse["median_oos_mse_improvement_pct"])
        ratio = (
            float("inf")
            if reverse_value <= 0 and own > 0
            else (own / reverse_value if reverse_value > 0 else 0.0)
        )
        row["directionality_ratio"] = ratio
        checks = {
            "minimum_windows": int(row["valid_windows"]) >= threshold.minimum_valid_rolling_windows,
            "corrected_significance": float(row["corrected_significant_fraction"])
            >= threshold.minimum_corrected_significant_fraction,
            "positive_oos": float(row["positive_oos_fraction"])
            >= threshold.minimum_positive_oos_fraction,
            "median_oos_improvement": own >= threshold.minimum_median_oos_mse_improvement_pct,
            "sign_accuracy_uplift": float(row["median_sign_accuracy_uplift_pp"])
            >= threshold.minimum_median_sign_accuracy_uplift_pp,
            "directionality": ratio >= threshold.minimum_directionality_ratio,
            "required_direction": row["direction"] == "btc_to_eth",
        }
        row["advancement_checks"] = checks
        row["advances"] = all(checks.values())
    return summaries


async def run(args: argparse.Namespace) -> dict[str, object]:
    config_path = Path(args.config)
    config = load_discovery_config(config_path)
    config_bytes = config_path.read_bytes()
    start = config.data.start.astimezone(UTC)
    end = config.data.end.astimezone(UTC)
    if end >= config.data.sealed_tail_start.astimezone(UTC):
        raise ValueError("discovery window would open the sealed v1 tail")
    btc, eth = await asyncio.gather(
        _load_candles(config.data.leader_symbol, start, end, Path(args.cache_dir)),
        _load_candles(config.data.follower_symbol, start, end, Path(args.cache_dir)),
    )
    frames = {
        timeframe: synchronized_returns(btc, eth, timeframe)
        for timeframe in config.design.timeframes_minutes
    }
    windows = _rolling_windows(config)
    rolling_rows: list[dict[str, object]] = []
    descriptive: list[dict[str, object]] = []
    for timeframe, frame in frames.items():
        for direction in config.design.directions:
            for lag in config.design.maximum_lags:
                design = _lagged_design(frame, direction, lag)
                for window_start, window_end in windows:
                    row = granger_design_window(
                        design,
                        direction,
                        lag,
                        window_start,
                        window_end,
                        config.design.train_fraction,
                        config.design.minimum_observations_per_window,
                    )
                    if row is not None:
                        row["timeframe_minutes"] = timeframe
                        rolling_rows.append(row)
                row = granger_design_window(
                    design,
                    direction,
                    lag,
                    start,
                    end,
                    config.design.train_fraction,
                    config.design.minimum_observations_per_window,
                )
                if row is not None:
                    row["timeframe_minutes"] = timeframe
                    descriptive.append(row)
    for window_start, window_end in windows:
        window_rows = [
            row
            for row in rolling_rows
            if row["window_start"] == window_start.isoformat()
            and row["window_end"] == window_end.isoformat()
        ]
        adjusted = benjamini_hochberg([float(row["p_value"]) for row in window_rows])
        for row, adjusted_p in zip(window_rows, adjusted, strict=True):
            row["adjusted_p_value"] = adjusted_p
    summaries = _summaries(rolling_rows, config)
    payload: dict[str, object] = {
        "study_id": config.study_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "config": str(config_path),
        "config_sha256": hashlib.sha256(config_bytes).hexdigest(),
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
                    "maximum_lag": row["maximum_lag"],
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
        )
    )


if __name__ == "__main__":
    main()
