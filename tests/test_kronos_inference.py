from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import pandas as pd
import pytest

from vnedge.research.kronos_forecast_gate import KronosForecastGateConfig
from vnedge.research.kronos_inference import (
    KronosDependencyError,
    KronosInferenceConfig,
    KronosInputError,
    KronosOutputError,
    UpstreamKronosBackend,
    generate_kronos_forecast,
    generate_kronos_forecast_batch,
    load_forecast_artifact,
    prepare_causal_context,
    write_forecast_artifact,
)


class FakeKronosBackend:
    metadata: ClassVar[dict[str, str]] = {
        "backend": "deterministic_test_double",
        "source_revision": "f" * 40,
    }

    def __init__(self, *, invalid: str | None = None) -> None:
        self.invalid = invalid
        self.seeds: list[int] = []

    def predict(
        self,
        context: pd.DataFrame,
        context_timestamps: pd.Series,
        future_timestamps: pd.Series,
        *,
        seed: int,
        config: KronosInferenceConfig,
    ) -> pd.DataFrame:
        self.seeds.append(seed)
        if self.invalid == "short":
            future_timestamps = future_timestamps[:-1]
        base = float(context["close"].iloc[-1])
        # Different seeds produce separately preserved paths, while every path
        # remains strongly bullish and valid OHLC.
        path_shift = (seed - config.seed) * 0.1
        rows = []
        for step, timestamp in enumerate(future_timestamps, start=1):
            close = base + path_shift + step * 0.8
            rows.append(
                {
                    "timestamp": timestamp,
                    "open": close - 0.4,
                    "high": close + 0.7,
                    "low": close - 0.8,
                    "close": close,
                }
            )
        result = pd.DataFrame(rows).set_index("timestamp")
        if self.invalid == "geometry":
            result.loc[result.index[0], "high"] = result.loc[result.index[0], "low"] - 1
        if self.invalid == "nonfinite":
            result.loc[result.index[0], "close"] = float("nan")
        return result


class FakeBatchKronosBackend(FakeKronosBackend):
    def __init__(self) -> None:
        super().__init__()
        self.batch_calls: list[tuple[int, int]] = []

    def predict_batch(
        self,
        contexts: list[pd.DataFrame],
        context_timestamps: list[pd.Series],
        future_timestamps: list[pd.Series],
        *,
        seed: int,
        config: KronosInferenceConfig,
    ) -> list[pd.DataFrame]:
        self.batch_calls.append((len(contexts), seed))
        return [
            self.predict(
                context,
                timestamps,
                future,
                seed=seed,
                config=config,
            )
            for context, timestamps, future in zip(
                contexts, context_timestamps, future_timestamps, strict=True
            )
        ]


def candles(rows: int = 64, *, timeframe: str = "1h") -> pd.DataFrame:
    freq = {"1m": "1min", "15m": "15min", "1h": "1h"}[timeframe]
    timestamp = pd.date_range("2026-01-01", periods=rows, freq=freq, tz="UTC")
    values = [100.0 + index * 0.1 for index in range(rows)]
    return pd.DataFrame(
        {
            "timestamp": timestamp,
            "open": values,
            "high": [value + 0.5 for value in values],
            "low": [value - 0.5 for value in values],
            "close": [value + 0.2 for value in values],
            "volume": [1000.0 + index for index in range(rows)],
        }
    )


def config(**overrides) -> KronosInferenceConfig:
    values = {"lookback_bars": 32, "horizon_bars": 4, "sample_paths": 3}
    values.update(overrides)
    return KronosInferenceConfig(**values)


def decision_time(frame: pd.DataFrame, timeframe: str = "1h") -> pd.Timestamp:
    step = {
        "1m": pd.Timedelta(minutes=1),
        "15m": pd.Timedelta(minutes=15),
        "1h": pd.Timedelta(hours=1),
    }[timeframe]
    return frame["timestamp"].iloc[-1] + step


def test_config_rejects_unbounded_or_ambiguous_settings():
    with pytest.raises(ValueError, match="lookback_bars"):
        KronosInferenceConfig(lookback_bars=16)
    with pytest.raises(ValueError, match="sample_paths"):
        KronosInferenceConfig(sample_paths=129)
    with pytest.raises(ValueError, match="source_revision"):
        KronosInferenceConfig(source_revision="main")


def test_context_is_closed_contiguous_and_exactly_available_at_decision():
    frame = candles()
    prepared, decision, available = prepare_causal_context(
        frame,
        timeframe="1h",
        decision_timestamp=decision_time(frame),
        config=config(),
    )

    assert len(prepared) == 32
    assert decision == available
    assert prepared["timestamp"].iloc[-1] == frame["timestamp"].iloc[-1]


def test_context_rejects_naive_timestamps_gaps_and_stale_decisions():
    frame = candles()
    naive = frame.copy()
    naive["timestamp"] = naive["timestamp"].dt.tz_localize(None)
    with pytest.raises(KronosInputError, match="timezone-aware"):
        prepare_causal_context(
            naive,
            timeframe="1h",
            decision_timestamp=decision_time(frame),
            config=config(),
        )

    gapped = frame.drop(index=50).reset_index(drop=True)
    with pytest.raises(KronosInputError, match="gaps"):
        prepare_causal_context(
            gapped,
            timeframe="1h",
            decision_timestamp=decision_time(frame),
            config=config(),
        )

    with pytest.raises(KronosInputError, match="decision must equal"):
        prepare_causal_context(
            frame,
            timeframe="1h",
            decision_timestamp=decision_time(frame) + pd.Timedelta(hours=1),
            config=config(),
        )

    future = pd.concat(
        [
            frame,
            pd.DataFrame(
                [
                    {
                        **frame.iloc[-1].to_dict(),
                        "timestamp": frame["timestamp"].iloc[-1] + pd.Timedelta(hours=1),
                    }
                ]
            ),
        ],
        ignore_index=True,
    )
    with pytest.raises(KronosInputError, match="unavailable at decision time"):
        prepare_causal_context(
            future,
            timeframe="1h",
            decision_timestamp=decision_time(frame),
            config=config(),
        )


def test_generation_preserves_independent_paths_and_is_research_only():
    frame = candles()
    backend = FakeKronosBackend()
    artifact = generate_kronos_forecast(
        frame,
        symbol="BTCUSD",
        timeframe="1h",
        decision_timestamp=decision_time(frame),
        backend=backend,
        config=config(seed=100),
        gate_config=KronosForecastGateConfig(
            min_expected_net_bps=0,
            min_confidence=0,
            min_reward_risk=0,
            max_adverse_bps=10_000,
        ),
        now=datetime(2026, 1, 4, tzinfo=UTC),
    )

    assert backend.seeds == [100, 101, 102]
    assert len(artifact.forecast) == 12
    assert {row["sample_id"] for row in artifact.forecast} == {
        "sample_000",
        "sample_001",
        "sample_002",
    }
    assert artifact.context_rows == 32
    assert artifact.gate_decision["selected_side"] == "long"
    assert artifact.verify() is True
    assert artifact.can_trade is False
    assert artifact.can_promote is False
    assert artifact.research_only is True


def test_batch_generation_uses_accelerated_backend_and_seals_each_artifact():
    first = candles()
    second = candles()
    second[["open", "high", "low", "close"]] += 50.0
    backend = FakeBatchKronosBackend()
    artifacts = generate_kronos_forecast_batch(
        [first, second],
        symbols=["BTCUSD", "ETHUSD"],
        timeframe="1h",
        decision_timestamps=[decision_time(first), decision_time(second)],
        backend=backend,
        config=config(seed=200, sample_paths=2),
        now=datetime(2026, 1, 4, tzinfo=UTC),
    )

    assert backend.batch_calls == [(2, 200), (2, 201)]
    assert [artifact.symbol for artifact in artifacts] == ["BTCUSD", "ETHUSD"]
    assert all(artifact.verify() for artifact in artifacts)
    assert all(
        artifact.forecast_quality["inference_api"] == "predict_batch" for artifact in artifacts
    )
    assert all(artifact.forecast_quality["batch_size"] == 2 for artifact in artifacts)


def test_batch_generation_rejects_misaligned_requests():
    with pytest.raises(KronosInputError, match="must align"):
        generate_kronos_forecast_batch(
            [candles()],
            symbols=["BTCUSD", "ETHUSD"],
            timeframe="1h",
            decision_timestamps=[decision_time(candles())],
            backend=FakeKronosBackend(),
            config=config(),
        )


@pytest.mark.parametrize("failure", ["short", "nonfinite"])
def test_generation_rejects_bad_model_output(failure: str):
    frame = candles()
    with pytest.raises(KronosOutputError):
        generate_kronos_forecast(
            frame,
            symbol="BTCUSD",
            timeframe="1h",
            decision_timestamp=decision_time(frame),
            backend=FakeKronosBackend(invalid=failure),
            config=config(),
        )


def test_generation_repairs_and_discloses_upstream_ohlc_geometry():
    frame = candles()
    artifact = generate_kronos_forecast(
        frame,
        symbol="BTCUSD",
        timeframe="1h",
        decision_timestamp=decision_time(frame),
        backend=FakeKronosBackend(invalid="geometry"),
        config=config(sample_paths=1),
    )

    assert artifact.forecast_quality["ohlc_geometry_repairs"] == 1
    assert all(
        row["high"] >= max(row["open"], row["close"])
        and row["low"] <= min(row["open"], row["close"])
        for row in artifact.forecast
    )
    assert artifact.verify() is True


def test_artifact_round_trip_and_tamper_detection(tmp_path: Path):
    frame = candles()
    artifact = generate_kronos_forecast(
        frame,
        symbol="ETHUSD",
        timeframe="1h",
        decision_timestamp=decision_time(frame),
        backend=FakeKronosBackend(),
        config=config(),
        now=datetime(2026, 1, 4, tzinfo=UTC),
    )
    path = write_forecast_artifact(artifact, tmp_path / "forecast.json")

    loaded = load_forecast_artifact(path)
    assert loaded == artifact
    assert loaded.verify() is True

    payload = json.loads(path.read_text())
    payload["forecast"][0]["close"] += 500
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="hash verification"):
        load_forecast_artifact(path)


def test_upstream_backend_fails_closed_on_missing_checkout(tmp_path: Path):
    with pytest.raises(KronosDependencyError, match="checkout missing"):
        UpstreamKronosBackend(repo=tmp_path / "not-there", config=config())
