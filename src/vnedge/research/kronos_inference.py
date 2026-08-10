"""Pinned, causal Kronos inference for VNEDGE research.

The upstream Kronos predictor is deliberately isolated behind a tiny backend
protocol.  This module owns the safety-sensitive parts of the integration:

* closed-candle and timestamp validation,
* exact model/tokenizer/source revisions,
* separately seeded sample paths (upstream ``sample_count`` averages paths),
* immutable, hash-verifiable research artifacts,
* fail-closed conversion into the existing fee-aware forecast gate.

It never imports execution code, creates an order, or changes a promotion
manifest.  Model downloads happen only when an operator explicitly runs the
CLI and does not request ``--local-files-only``.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import random
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Literal, Protocol, cast, runtime_checkable

import numpy as np
import pandas as pd

from vnedge.data.data_quality_gate import validate_candles
from vnedge.data.schemas import CANDLE_COLUMNS, TIMEFRAME_MS
from vnedge.research.kronos_forecast_gate import (
    ForecastRoute,
    ForecastSide,
    KronosForecastGateConfig,
    score_kronos_forecast_gate,
)

TimestampConvention = Literal["open", "close"]

SCHEMA_VERSION = "vnedge.kronos_forecast.v1"
UPSTREAM_SOURCE_REVISION = "67b630e67f6a18c9e9be918d9b4337c960db1e9a"
KRONOS_MINI_MODEL_REVISION = "f4e68697d9d5aed55cef5c96aabc3376bcad9f81"
KRONOS_2K_TOKENIZER_REVISION = "26966d0035065a0cae0ebad7af8ece35bc1fb51c"
DEFAULT_KRONOS_REPO = Path("models/kronos/upstream")


class KronosDependencyError(RuntimeError):
    """Kronos source or optional inference dependencies are unavailable."""


class KronosInputError(ValueError):
    """Historical context violates the causal inference contract."""


class KronosOutputError(ValueError):
    """The model returned an incomplete or invalid forecast path."""


@dataclass(frozen=True)
class KronosInferenceConfig:
    """Frozen parameters and revisions for one reproducible inference run."""

    model_id: str = "NeoQuasar/Kronos-mini"
    model_revision: str = KRONOS_MINI_MODEL_REVISION
    tokenizer_id: str = "NeoQuasar/Kronos-Tokenizer-2k"
    tokenizer_revision: str = KRONOS_2K_TOKENIZER_REVISION
    source_revision: str = UPSTREAM_SOURCE_REVISION
    lookback_bars: int = 512
    horizon_bars: int = 24
    sample_paths: int = 16
    seed: int = 42
    temperature: float = 1.0
    top_k: int = 0
    top_p: float = 0.90
    max_context: int = 2048
    clip: float = 5.0
    device: str = "cpu"
    timestamp_convention: TimestampConvention = "open"
    local_files_only: bool = False

    def __post_init__(self) -> None:
        for name in ("model_id", "model_revision", "tokenizer_id", "tokenizer_revision"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} is required")
        if len(self.source_revision) != 40:
            raise ValueError("source_revision must be a full 40-character git revision")
        if self.lookback_bars < 32:
            raise ValueError("lookback_bars must be at least 32")
        if self.lookback_bars > self.max_context:
            raise ValueError("lookback_bars cannot exceed max_context")
        if self.horizon_bars < 1:
            raise ValueError("horizon_bars must be positive")
        if not 1 <= self.sample_paths <= 128:
            raise ValueError("sample_paths must be in [1, 128]")
        if self.temperature <= 0:
            raise ValueError("temperature must be positive")
        if self.top_k < 0:
            raise ValueError("top_k cannot be negative")
        if not 0 < self.top_p <= 1:
            raise ValueError("top_p must be in (0, 1]")
        if self.clip <= 0:
            raise ValueError("clip must be positive")
        if self.timestamp_convention not in {"open", "close"}:
            raise ValueError("timestamp_convention must be open or close")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT_INFERENCE_CONFIG = KronosInferenceConfig()
DEFAULT_GATE_CONFIG = KronosForecastGateConfig()


class KronosBackend(Protocol):
    """Minimal interface implemented by the real and test inference backends."""

    @property
    def metadata(self) -> Mapping[str, Any]: ...

    def predict(
        self,
        context: pd.DataFrame,
        context_timestamps: pd.Series,
        future_timestamps: pd.Series,
        *,
        seed: int,
        config: KronosInferenceConfig,
    ) -> pd.DataFrame: ...


@runtime_checkable
class KronosBatchBackend(KronosBackend, Protocol):
    """Optional accelerated interface; semantics match separately seeded paths."""

    def predict_batch(
        self,
        contexts: list[pd.DataFrame],
        context_timestamps: list[pd.Series],
        future_timestamps: list[pd.Series],
        *,
        seed: int,
        config: KronosInferenceConfig,
    ) -> list[pd.DataFrame]: ...


@dataclass(frozen=True)
class KronosForecastArtifact:
    """Hash-backed output of one causal Kronos decision point."""

    artifact_id: str
    created_at: str
    symbol: str
    timeframe: str
    decision_timestamp: str
    context_first_timestamp: str
    context_last_timestamp: str
    context_available_at: str
    context_rows: int
    context_sha256: str
    inference_config: dict[str, Any]
    backend: dict[str, Any]
    forecast_quality: dict[str, Any]
    forecast: tuple[dict[str, Any], ...]
    gate_decision: dict[str, Any]
    payload_sha256: str
    schema_version: str = SCHEMA_VERSION
    can_trade: bool = False
    can_promote: bool = False
    research_only: bool = True

    def unsigned_payload(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in asdict(self).items()
            if key not in {"artifact_id", "payload_sha256"}
        }

    def verify(self) -> bool:
        digest = _sha256_json(self.unsigned_payload())
        expected_id = f"kronos_{digest[:20]}"
        return self.payload_sha256 == digest and self.artifact_id == expected_id

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class UpstreamKronosBackend:
    """Real backend loaded from an explicitly pinned local Kronos checkout."""

    def __init__(self, *, repo: Path | str, config: KronosInferenceConfig) -> None:
        self.repo = Path(repo).expanduser().resolve()
        revision = _validate_upstream_checkout(self.repo, config.source_revision)
        Kronos, KronosTokenizer, KronosPredictor = _load_upstream_classes(self.repo)
        load_kwargs = {
            "revision": config.model_revision,
            "local_files_only": config.local_files_only,
        }
        tokenizer_kwargs = {
            "revision": config.tokenizer_revision,
            "local_files_only": config.local_files_only,
        }
        try:
            tokenizer = KronosTokenizer.from_pretrained(config.tokenizer_id, **tokenizer_kwargs)
            model = Kronos.from_pretrained(config.model_id, **load_kwargs)
            tokenizer.eval()
            model.eval()
            self._predictor = KronosPredictor(
                model,
                tokenizer,
                device=config.device,
                max_context=config.max_context,
                clip=config.clip,
            )
        except Exception as exc:  # dependency/hub failures need one actionable boundary
            raise KronosDependencyError(f"unable to initialize pinned Kronos model: {exc}") from exc
        self._metadata = {
            "backend": "upstream_kronos",
            "source_repo": str(self.repo),
            "source_revision": revision,
            "model_id": config.model_id,
            "model_revision": config.model_revision,
            "tokenizer_id": config.tokenizer_id,
            "tokenizer_revision": config.tokenizer_revision,
            "device": config.device,
        }

    @property
    def metadata(self) -> Mapping[str, Any]:
        return self._metadata

    def predict(
        self,
        context: pd.DataFrame,
        context_timestamps: pd.Series,
        future_timestamps: pd.Series,
        *,
        seed: int,
        config: KronosInferenceConfig,
    ) -> pd.DataFrame:
        torch = _import_torch()
        _seed_everything(seed, torch)
        with torch.inference_mode():
            # sample_count=1 is intentional. Upstream averages sample_count
            # paths, which destroys the distribution required by our gate.
            return cast(
                pd.DataFrame,
                self._predictor.predict(
                    df=context,
                    x_timestamp=context_timestamps,
                    y_timestamp=future_timestamps,
                    pred_len=config.horizon_bars,
                    T=config.temperature,
                    top_k=config.top_k,
                    top_p=config.top_p,
                    sample_count=1,
                    verbose=False,
                ),
            )

    def predict_batch(
        self,
        contexts: list[pd.DataFrame],
        context_timestamps: list[pd.Series],
        future_timestamps: list[pd.Series],
        *,
        seed: int,
        config: KronosInferenceConfig,
    ) -> list[pd.DataFrame]:
        if not contexts:
            return []
        if not (len(contexts) == len(context_timestamps) == len(future_timestamps)):
            raise KronosInputError("batch inputs must have equal non-zero lengths")
        torch = _import_torch()
        _seed_everything(seed, torch)
        with torch.inference_mode():
            predicted = self._predictor.predict_batch(
                df_list=contexts,
                x_timestamp_list=context_timestamps,
                y_timestamp_list=future_timestamps,
                pred_len=config.horizon_bars,
                T=config.temperature,
                top_k=config.top_k,
                top_p=config.top_p,
                sample_count=1,
                verbose=False,
            )
        return cast(list[pd.DataFrame], predicted)


def generate_kronos_forecast(
    candles: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    decision_timestamp: datetime | pd.Timestamp | str,
    backend: KronosBackend,
    config: KronosInferenceConfig = DEFAULT_INFERENCE_CONFIG,
    gate_config: KronosForecastGateConfig = DEFAULT_GATE_CONFIG,
    route: ForecastRoute = "maker_taker",
    side: ForecastSide | None = None,
    now: datetime | None = None,
) -> KronosForecastArtifact:
    """Generate, validate, score and seal one forecast artifact."""

    return generate_kronos_forecast_batch(
        [candles],
        symbols=[symbol],
        timeframe=timeframe,
        decision_timestamps=[decision_timestamp],
        backend=backend,
        config=config,
        gate_config=gate_config,
        route=route,
        side=side,
        now=now,
    )[0]


def generate_kronos_forecast_batch(
    candle_sets: list[pd.DataFrame],
    *,
    symbols: list[str],
    timeframe: str,
    decision_timestamps: list[datetime | pd.Timestamp | str],
    backend: KronosBackend,
    config: KronosInferenceConfig = DEFAULT_INFERENCE_CONFIG,
    gate_config: KronosForecastGateConfig = DEFAULT_GATE_CONFIG,
    route: ForecastRoute = "maker_taker",
    side: ForecastSide | None = None,
    now: datetime | None = None,
) -> list[KronosForecastArtifact]:
    """Generate independently sealed artifacts with optional batched inference."""

    if not candle_sets:
        raise KronosInputError("at least one candle set is required")
    if not (len(candle_sets) == len(symbols) == len(decision_timestamps)):
        raise KronosInputError("batch candle sets, symbols, and decisions must align")
    if any(not symbol.strip() for symbol in symbols):
        raise KronosInputError("symbol is required")

    prepared = [
        prepare_causal_context(
            candles,
            timeframe=timeframe,
            decision_timestamp=decision,
            config=config,
        )
        for candles, decision in zip(candle_sets, decision_timestamps, strict=True)
    ]
    contexts = [row[0] for row in prepared]
    futures = [_future_timestamps(context, timeframe, config) for context in contexts]
    features = [_model_features(context) for context in contexts]
    paths: list[list[pd.DataFrame]] = [[] for _ in contexts]
    repairs = [0 for _ in contexts]
    uses_batch_backend = isinstance(backend, KronosBatchBackend) and len(contexts) > 1
    for sample_id in range(config.sample_paths):
        if uses_batch_backend:
            predicted_rows = cast(KronosBatchBackend, backend).predict_batch(
                [frame.copy(deep=True) for frame in features],
                [context["timestamp"].copy(deep=True) for context in contexts],
                [future.copy() for future in futures],
                seed=config.seed + sample_id,
                config=config,
            )
        else:
            predicted_rows = [
                backend.predict(
                    feature.copy(deep=True),
                    context["timestamp"].copy(deep=True),
                    future.copy(),
                    seed=config.seed + sample_id,
                    config=config,
                )
                for feature, context, future in zip(features, contexts, futures, strict=True)
            ]
        if len(predicted_rows) != len(contexts):
            raise KronosOutputError(
                "batch backend returned a different number of forecasts than contexts"
            )
        for index, (predicted, future) in enumerate(zip(predicted_rows, futures, strict=True)):
            path, path_repairs = _normalize_forecast_path(predicted, future)
            repairs[index] += path_repairs
            path.insert(0, "sample_id", f"sample_{sample_id:03d}")
            paths[index].append(path)

    created = _as_utc_timestamp(now or datetime.now(UTC), field="created_at")
    artifacts: list[KronosForecastArtifact] = []
    for index, context in enumerate(contexts):
        forecast = pd.concat(paths[index], ignore_index=True)
        artifacts.append(
            _seal_forecast_artifact(
                context=context,
                decision=prepared[index][1],
                available_at=prepared[index][2],
                symbol=symbols[index],
                timeframe=timeframe,
                forecast=forecast,
                geometry_repairs=repairs[index],
                batch_size=len(contexts) if uses_batch_backend else 1,
                backend=backend,
                config=config,
                gate_config=gate_config,
                route=route,
                side=side,
                created=created,
            )
        )
    return artifacts


def _seal_forecast_artifact(
    *,
    context: pd.DataFrame,
    decision: pd.Timestamp,
    available_at: pd.Timestamp,
    symbol: str,
    timeframe: str,
    forecast: pd.DataFrame,
    geometry_repairs: int,
    batch_size: int,
    backend: KronosBackend,
    config: KronosInferenceConfig,
    gate_config: KronosForecastGateConfig,
    route: ForecastRoute,
    side: ForecastSide | None,
    created: pd.Timestamp,
) -> KronosForecastArtifact:
    """Seal one already-normalized forecast without changing its economics."""

    gate = score_kronos_forecast_gate(
        context,
        forecast,
        config=gate_config,
        route=route,
        side=side,
    ).to_dict()
    context_hash = _context_hash(context)
    forecast_records = tuple(_forecast_records(forecast))
    unsigned = {
        "created_at": created.isoformat(),
        "symbol": symbol,
        "timeframe": timeframe,
        "decision_timestamp": decision.isoformat(),
        "context_first_timestamp": context["timestamp"].iloc[0].isoformat(),
        "context_last_timestamp": context["timestamp"].iloc[-1].isoformat(),
        "context_available_at": available_at.isoformat(),
        "context_rows": len(context),
        "context_sha256": context_hash,
        "inference_config": config.to_dict(),
        "backend": dict(backend.metadata),
        "forecast_quality": {
            "paths": config.sample_paths,
            "rows": len(forecast),
            "ohlc_geometry_repairs": geometry_repairs,
            "inference_api": "predict_batch" if batch_size > 1 else "predict",
            "batch_size": batch_size,
            "repair_policy": "high=max(raw_high,open,close);low=min(raw_low,open,close)",
            "nonfinite_or_nonpositive_policy": "reject",
        },
        "forecast": forecast_records,
        "gate_decision": gate,
        "schema_version": SCHEMA_VERSION,
        "can_trade": False,
        "can_promote": False,
        "research_only": True,
    }
    digest = _sha256_json(unsigned)
    return KronosForecastArtifact(
        artifact_id=f"kronos_{digest[:20]}",
        payload_sha256=digest,
        **unsigned,
    )


def prepare_causal_context(
    candles: pd.DataFrame,
    *,
    timeframe: str,
    decision_timestamp: datetime | pd.Timestamp | str,
    config: KronosInferenceConfig,
) -> tuple[pd.DataFrame, pd.Timestamp, pd.Timestamp]:
    """Validate and freeze exactly the data available at decision time."""

    if timeframe not in TIMEFRAME_MS:
        raise KronosInputError(f"unsupported timeframe: {timeframe}")
    if not isinstance(candles, pd.DataFrame):
        raise KronosInputError("candles must be a pandas DataFrame")
    missing = [column for column in CANDLE_COLUMNS if column not in candles.columns]
    if missing:
        raise KronosInputError(f"candles missing columns: {', '.join(missing)}")
    frame = candles[CANDLE_COLUMNS].copy(deep=True)
    try:
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=False)
    except Exception as exc:
        raise KronosInputError(f"invalid candle timestamps: {exc}") from exc
    if not isinstance(frame["timestamp"].dtype, pd.DatetimeTZDtype):
        raise KronosInputError("candle timestamps must be timezone-aware UTC")
    frame["timestamp"] = frame["timestamp"].dt.tz_convert("UTC")
    decision = _as_utc_timestamp(decision_timestamp, field="decision_timestamp")
    step = pd.Timedelta(milliseconds=TIMEFRAME_MS[timeframe])
    if config.timestamp_convention == "open":
        available = frame["timestamp"] + step
    else:
        available = frame["timestamp"]
    future_rows = int((available > decision).sum())
    if future_rows:
        raise KronosInputError(f"candles contain {future_rows} row(s) unavailable at decision time")
    eligible = frame.loc[available <= decision].copy()
    if len(eligible) < config.lookback_bars:
        raise KronosInputError(
            f"need {config.lookback_bars} closed candles, found {len(eligible)} by decision time"
        )
    context = eligible.tail(config.lookback_bars).reset_index(drop=True)
    quality = validate_candles(context, timeframe, allow_gaps=False, dataset="kronos_context")
    if not quality.passed:
        raise KronosInputError(quality.summary)
    available_at = (
        context["timestamp"].iloc[-1] + step
        if config.timestamp_convention == "open"
        else context["timestamp"].iloc[-1]
    )
    if available_at != decision:
        raise KronosInputError(
            "decision must equal the availability time of the latest closed candle "
            f"({available_at.isoformat()})"
        )
    return context, decision, available_at


def write_forecast_artifact(artifact: KronosForecastArtifact, path: Path | str) -> Path:
    """Atomically publish a verified research artifact."""

    if not artifact.verify():
        raise ValueError("refusing to write an artifact with an invalid payload hash")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w", dir=target.parent, prefix=target.name, suffix=".tmp", delete=False, encoding="utf-8"
    ) as handle:
        json.dump(artifact.to_dict(), handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(target)
    return target


def load_forecast_artifact(path: Path | str) -> KronosForecastArtifact:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    payload["forecast"] = tuple(payload["forecast"])
    artifact = KronosForecastArtifact(**payload)
    if not artifact.verify():
        raise ValueError("forecast artifact hash verification failed")
    return artifact


def _future_timestamps(
    context: pd.DataFrame,
    timeframe: str,
    config: KronosInferenceConfig,
) -> pd.Series:
    step = pd.Timedelta(milliseconds=TIMEFRAME_MS[timeframe])
    last = context["timestamp"].iloc[-1]
    first = last + step
    return pd.Series(pd.date_range(first, periods=config.horizon_bars, freq=step, tz="UTC"))


def _model_features(context: pd.DataFrame) -> pd.DataFrame:
    features = context[["open", "high", "low", "close", "volume"]].copy()
    features["amount"] = features["volume"] * features[["open", "high", "low", "close"]].mean(
        axis=1
    )
    return features


def _normalize_forecast_path(
    forecast: pd.DataFrame,
    expected_timestamps: pd.Series,
) -> tuple[pd.DataFrame, int]:
    if not isinstance(forecast, pd.DataFrame):
        raise KronosOutputError("backend forecast must be a pandas DataFrame")
    missing = [column for column in ("open", "high", "low", "close") if column not in forecast]
    if missing:
        raise KronosOutputError(f"forecast missing columns: {', '.join(missing)}")
    if len(forecast) != len(expected_timestamps):
        raise KronosOutputError(
            f"forecast length {len(forecast)} does not match horizon {len(expected_timestamps)}"
        )
    frame = forecast[["open", "high", "low", "close"]].copy().reset_index(drop=True)
    for column in frame.columns:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    values = frame.to_numpy(dtype=float)
    if not np.isfinite(values).all() or (values <= 0).any():
        raise KronosOutputError("forecast OHLC values must be finite and positive")
    bad_range = (
        (frame["high"] < frame["low"])
        | (frame["high"] < frame[["open", "close"]].max(axis=1))
        | (frame["low"] > frame[["open", "close"]].min(axis=1))
    )
    repairs = int(bad_range.sum())
    if repairs:
        # Upstream forecasts each field independently and can violate candle
        # geometry. Envelope repair is deterministic and conservative: it
        # never invents a more favorable extreme than an already predicted
        # open/close. Every repaired row is counted in the sealed artifact.
        frame["high"] = frame[["open", "high", "close"]].max(axis=1)
        frame["low"] = frame[["open", "low", "close"]].min(axis=1)
    frame.insert(0, "timestamp", expected_timestamps)
    return frame, repairs


def _forecast_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in frame.to_dict("records"):
        records.append(
            {
                "sample_id": str(row["sample_id"]),
                "timestamp": cast(pd.Timestamp, row["timestamp"]).isoformat(),
                **{column: float(row[column]) for column in ("open", "high", "low", "close")},
            }
        )
    return records


def _context_hash(context: pd.DataFrame) -> str:
    records = []
    for row in context.to_dict("records"):
        records.append(
            {
                "timestamp": cast(pd.Timestamp, row["timestamp"]).isoformat(),
                **{column: float(row[column]) for column in CANDLE_COLUMNS[1:]},
            }
        )
    return _sha256_json(records)


def _sha256_json(payload: Any) -> str:
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _as_utc_timestamp(value: Any, *, field: str) -> pd.Timestamp:
    parsed = pd.Timestamp(value)
    if parsed.tzinfo is None:
        raise KronosInputError(f"{field} must be timezone-aware")
    return parsed.tz_convert("UTC")


def _validate_upstream_checkout(repo: Path, expected_revision: str) -> str:
    if not (repo / "model" / "__init__.py").is_file():
        raise KronosDependencyError(f"Kronos source checkout missing at {repo}")
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise KronosDependencyError(f"cannot verify Kronos source revision: {exc}") from exc
    revision = result.stdout.strip()
    if revision != expected_revision:
        raise KronosDependencyError(
            f"Kronos source revision mismatch: expected {expected_revision}, found {revision}"
        )
    return revision


def _load_upstream_classes(repo: Path) -> tuple[Any, Any, Any]:
    # Upstream currently imports ``model.module`` by its top-level package
    # name inside model/kronos.py.  Load that exact name, but reject any
    # pre-existing package from another path so dependency substitution cannot
    # silently change the implementation being evaluated.
    package_name = "model"
    expected_init = (repo / "model" / "__init__.py").resolve()
    existing = sys.modules.get(package_name)
    if existing is not None:
        existing_file = Path(str(getattr(existing, "__file__", ""))).resolve()
        if existing_file != expected_init:
            raise KronosDependencyError(
                f"top-level model package already loaded from unexpected path: {existing_file}"
            )
    else:
        init_path = expected_init
        spec = importlib.util.spec_from_file_location(
            package_name,
            init_path,
            submodule_search_locations=[str(init_path.parent)],
        )
        if spec is None or spec.loader is None:
            raise KronosDependencyError("cannot construct Kronos import specification")
        module = importlib.util.module_from_spec(spec)
        sys.modules[package_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            sys.modules.pop(package_name, None)
            raise KronosDependencyError(
                "cannot import Kronos; install the foundation-forecast extra"
            ) from exc
    module = sys.modules[package_name]
    return module.Kronos, module.KronosTokenizer, module.KronosPredictor


def _import_torch() -> Any:
    try:
        import torch
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise KronosDependencyError(
            "PyTorch is unavailable; install with pip install -e '.[foundation-forecast]'"
        ) from exc
    return torch


def _seed_everything(seed: int, torch: Any) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if hasattr(torch, "cuda") and torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
    if hasattr(torch, "use_deterministic_algorithms"):
        torch.use_deterministic_algorithms(True)


def _read_candles(path: Path) -> pd.DataFrame:
    if path.suffix.lower() == ".parquet":
        return pd.read_parquet(path)
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path)
    raise ValueError("--candles must be a .csv or .parquet file")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candles", type=Path, required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--timeframe", choices=sorted(TIMEFRAME_MS), required=True)
    parser.add_argument(
        "--decision-time", default=None, help="UTC ISO; defaults to latest candle close"
    )
    parser.add_argument("--kronos-repo", type=Path, default=DEFAULT_KRONOS_REPO)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--lookback", type=int, default=512)
    parser.add_argument("--horizon", type=int, default=24)
    parser.add_argument("--samples", type=int, default=16)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--timestamp-convention", choices=["open", "close"], default="open")
    parser.add_argument("--local-files-only", action="store_true")
    parser.add_argument("--route", choices=["maker_taker", "taker_taker"], default="maker_taker")
    parser.add_argument("--side", choices=["long", "short"], default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    candles = _read_candles(args.candles)
    if "timestamp" not in candles:
        raise KronosInputError("candle file requires timestamp column")
    candles["timestamp"] = pd.to_datetime(candles["timestamp"], utc=True)
    step = pd.Timedelta(milliseconds=TIMEFRAME_MS[args.timeframe])
    latest = candles["timestamp"].max()
    decision_time = args.decision_time or (
        latest + step if args.timestamp_convention == "open" else latest
    )
    config = KronosInferenceConfig(
        lookback_bars=args.lookback,
        horizon_bars=args.horizon,
        sample_paths=args.samples,
        seed=args.seed,
        device=args.device,
        timestamp_convention=args.timestamp_convention,
        local_files_only=args.local_files_only,
    )
    backend = UpstreamKronosBackend(repo=args.kronos_repo, config=config)
    artifact = generate_kronos_forecast(
        candles,
        symbol=args.symbol,
        timeframe=args.timeframe,
        decision_timestamp=decision_time,
        backend=backend,
        config=config,
        route=args.route,
        side=args.side,
    )
    write_forecast_artifact(artifact, args.out)
    print(
        json.dumps(
            {
                "artifact": str(args.out),
                "artifact_id": artifact.artifact_id,
                "verified": artifact.verify(),
                "verdict": artifact.gate_decision["verdict"],
                "selected_side": artifact.gate_decision["selected_side"],
                "can_trade": False,
                "can_promote": False,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
