from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import pandas as pd
import pytest

from vnedge.research.kronos_inference import KronosInferenceConfig
from vnedge.research.kronos_permutation_matrix import (
    KronosPermutationConfig,
    aggregate_causal_candles,
    evaluate_frozen_permutation_holdback,
    load_canonical_minute_cache,
    run_kronos_permutation_matrix,
    write_holdback_report,
    write_matrix_report,
)


def minute_candles(rows: int = 900, *, offset: float = 0.0) -> pd.DataFrame:
    timestamps = pd.date_range("2025-01-01", periods=rows, freq="1min", tz="UTC")
    close = [100.0 + offset + index * 0.2 for index in range(rows)]
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": [value - 0.01 for value in close],
            "high": [value + 0.05 for value in close],
            "low": [value - 0.05 for value in close],
            "close": close,
            "volume": [1000.0 + index for index in range(rows)],
        }
    )


class BatchBullishBackend:
    metadata: ClassVar[dict[str, str]] = {
        "backend": "batch_bullish_test_double",
        "source_revision": "a" * 40,
    }

    def __init__(self) -> None:
        self.batch_calls = 0

    def predict(
        self,
        context: pd.DataFrame,
        context_timestamps: pd.Series,
        future_timestamps: pd.Series,
        *,
        seed: int,
        config: KronosInferenceConfig,
    ) -> pd.DataFrame:
        return self._forecast(context, future_timestamps, seed, config)

    def predict_batch(
        self,
        contexts: list[pd.DataFrame],
        context_timestamps: list[pd.Series],
        future_timestamps: list[pd.Series],
        *,
        seed: int,
        config: KronosInferenceConfig,
    ) -> list[pd.DataFrame]:
        self.batch_calls += 1
        return [
            self._forecast(context, future, seed, config)
            for context, future in zip(contexts, future_timestamps, strict=True)
        ]

    @staticmethod
    def _forecast(
        context: pd.DataFrame,
        future: pd.Series,
        seed: int,
        config: KronosInferenceConfig,
    ) -> pd.DataFrame:
        base = float(context["close"].iloc[-1])
        shift = (seed - config.seed) * 0.01
        rows = []
        for index, timestamp in enumerate(future, start=1):
            close = base + shift + index * 2.0
            rows.append(
                {
                    "timestamp": timestamp,
                    "open": close - 0.05,
                    "high": close + 0.10,
                    "low": close - 0.10,
                    "close": close,
                }
            )
        return pd.DataFrame(rows).set_index("timestamp")


def test_causal_aggregation_uses_only_complete_epoch_aligned_bars() -> None:
    source = minute_candles(12)
    aggregated = aggregate_causal_candles(source, "5m")
    assert len(aggregated) == 2
    assert aggregated.iloc[0]["open"] == source.iloc[0]["open"]
    assert aggregated.iloc[0]["close"] == source.iloc[4]["close"]
    assert aggregated.iloc[0]["volume"] == pytest.approx(source.iloc[:5]["volume"].sum())


def test_cache_loader_deduplicates_identical_overlap_and_rejects_conflict(
    tmp_path: Path,
) -> None:
    frame = minute_candles(60)
    frame.iloc[:40].to_parquet(tmp_path / "BTCUSD_1m_a.parquet", index=False)
    frame.iloc[20:].to_parquet(tmp_path / "BTCUSD_1m_b.parquet", index=False)
    loaded = load_canonical_minute_cache("BTCUSD", cache_dir=tmp_path)
    assert len(loaded) == 60

    conflict = frame.iloc[20:].copy()
    conflict.loc[conflict.index[0], "close"] += 10
    conflict.to_parquet(tmp_path / "BTCUSD_1m_c.parquet", index=False)
    with pytest.raises(ValueError, match="conflict"):
        load_canonical_minute_cache("BTCUSD", cache_dir=tmp_path)


def test_matrix_covers_declared_cartesian_grid_and_keeps_tail_unopened(
    tmp_path: Path,
) -> None:
    config = KronosPermutationConfig(
        symbols=("BTCUSD", "ETHUSD"),
        timeframes=("1m", "5m"),
        lookbacks=(32,),
        horizon_hours=(1 / 60,),
        temperatures=(1.0,),
        top_ps=(0.9,),
        generated_sample_paths=2,
        sample_path_subsets=(1, 2),
        routes=("maker_taker",),
        min_expected_net_bps=(0.0,),
        min_confidences=(0.5,),
        min_reward_risks=(0.0,),
        observations_per_base=4,
    )
    backend = BatchBullishBackend()
    report = run_kronos_permutation_matrix(
        {
            "BTCUSD": minute_candles(),
            "ETHUSD": minute_candles(offset=100.0),
        },
        backend=backend,
        config=config,
        now=datetime(2026, 8, 8, tzinfo=UTC),
    )

    assert report["completion"] == {
        "expected_base_runs": 4,
        "completed_base_runs": 4,
        "failed_base_runs": 0,
        "scored_permutations": 8,
        "complete": True,
    }
    assert backend.batch_calls == 8  # 4 bases x 2 separately seeded paths
    assert report["matrix_contract"]["holdback_evaluated"] is False
    assert report["eligible_selection_candidates"] == []  # only four observations per row
    assert all(row["artifacts_verified"] == 4 for row in report["base_runs"])
    by_timeframe = {
        timeframe: [
            row["accepted_selection"]["avg_net_bps"]
            for row in report["ranked_permutations"]
            if row["timeframe"] == timeframe
        ]
        for timeframe in ("1m", "5m")
    }
    assert all(value < 0 for value in by_timeframe["1m"])  # fee wall is applied
    assert all(value > 0 for value in by_timeframe["5m"])
    assert report["can_trade"] is False and report["can_promote"] is False

    json_path, csv_path = write_matrix_report(
        report,
        json_path=tmp_path / "matrix.json",
        csv_path=tmp_path / "matrix.csv",
    )
    assert json_path.is_file()
    assert csv_path is not None and csv_path.is_file()


def test_matrix_report_rejects_tampering(tmp_path: Path) -> None:
    config = KronosPermutationConfig(
        symbols=("BTCUSD",),
        timeframes=("5m",),
        lookbacks=(32,),
        horizon_hours=(1 / 12,),
        temperatures=(1.0,),
        top_ps=(0.9,),
        generated_sample_paths=1,
        sample_path_subsets=(1,),
        routes=("maker_taker",),
        min_expected_net_bps=(0.0,),
        min_confidences=(0.5,),
        min_reward_risks=(0.0,),
        observations_per_base=2,
    )
    report = run_kronos_permutation_matrix(
        {"BTCUSD": minute_candles()},
        backend=BatchBullishBackend(),
        config=config,
        now=datetime(2026, 8, 8, tzinfo=UTC),
    )
    report["runtime_seconds"] = 999
    with pytest.raises(ValueError, match="invalid payload hash"):
        write_matrix_report(report, json_path=tmp_path / "tampered.json")


def test_frozen_permutation_evaluates_only_reserved_tail(tmp_path: Path) -> None:
    candles = minute_candles(1_200)
    config = KronosPermutationConfig(
        symbols=("ETHUSD",),
        timeframes=("1m",),
        lookbacks=(32,),
        horizon_hours=(1 / 60,),
        temperatures=(1.0,),
        top_ps=(0.9,),
        generated_sample_paths=4,
        sample_path_subsets=(4,),
        routes=("maker_taker",),
        min_expected_net_bps=(0.0,),
        min_confidences=(0.5,),
        min_reward_risks=(0.0,),
        observations_per_base=30,
    )
    backend = BatchBullishBackend()
    selection = run_kronos_permutation_matrix(
        {"ETHUSD": candles},
        backend=backend,
        config=config,
        now=datetime(2026, 8, 8, tzinfo=UTC),
    )
    candidate_id = selection["ranked_permutations"][0]["permutation_id"]
    holdback = evaluate_frozen_permutation_holdback(
        {"ETHUSD": candles},
        selection_report=selection,
        permutation_id=candidate_id,
        backend=backend,
        observations=30,
        now=datetime(2026, 8, 8, 1, tzinfo=UTC),
    )

    assert holdback["selection_report_sha256"] == selection["payload_sha256"]
    assert holdback["permutation_id"] == candidate_id
    assert holdback["all_forecasts"]["observations"] == 30
    assert holdback["contract"]["historically_untouched_claim"] is False
    assert pd.Timestamp(holdback["window"]["first_decision"]) >= pd.Timestamp(
        selection["data_window"]["held_back_start"]
    )
    assert holdback["monthly"]
    assert holdback["can_trade"] is False and holdback["can_promote"] is False
    assert write_holdback_report(holdback, tmp_path / "holdback.json").is_file()


def test_holdback_rejects_tampered_selection_report() -> None:
    config = KronosPermutationConfig(
        symbols=("ETHUSD",),
        timeframes=("1m",),
        lookbacks=(32,),
        horizon_hours=(1 / 60,),
        generated_sample_paths=4,
        sample_path_subsets=(4,),
        routes=("maker_taker",),
        min_expected_net_bps=(0.0,),
        min_confidences=(0.5,),
        min_reward_risks=(0.0,),
        observations_per_base=30,
    )
    candles = minute_candles(1_200)
    backend = BatchBullishBackend()
    selection = run_kronos_permutation_matrix(
        {"ETHUSD": candles},
        backend=backend,
        config=config,
        now=datetime(2026, 8, 8, tzinfo=UTC),
    )
    selection["runtime_seconds"] = 99
    with pytest.raises(ValueError, match="payload hash is invalid"):
        evaluate_frozen_permutation_holdback(
            {"ETHUSD": candles},
            selection_report=selection,
            permutation_id=selection["ranked_permutations"][0]["permutation_id"],
            backend=backend,
        )
