"""Causal Delta India historical panel for coarse forced-flow research.

This module builds data evidence, not a strategy.  Its cascade candidate is a
candle-level proxy for OI contraction coinciding with an unusually large price
range.  It must never be described as observed liquidation flow: historical L2,
trade aggression, and actual liquidation events are not present in this panel.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import numpy as np
import pandas as pd

from vnedge.data.data_quality_gate import validate_candles
from vnedge.data.delta_native_history import (
    DELTA_INDIA_API_URL,
    fetch_delta_candle_history,
    fetch_delta_funding_history,
)
from vnedge.exchange.delta_ws import delta_native_symbol

RESOLUTION_SECONDS: dict[str, int] = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1_800,
    "1h": 3_600,
    "2h": 7_200,
    "4h": 14_400,
    "6h": 21_600,
    "1d": 86_400,
    "1w": 604_800,
}
_HTTP_ATTEMPTS = 4
_HTTP_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class ForcedFlowPanelConfig:
    resolution: str = "5m"
    rolling_window: int = 500
    min_history: int = 100
    oi_contraction_quantile: float = 0.05
    range_expansion_quantile: float = 0.95
    untouched_fraction: float = 0.20
    minimum_source_coverage: float = 0.95

    def __post_init__(self) -> None:
        if self.resolution not in RESOLUTION_SECONDS:
            raise ValueError(f"unsupported Delta resolution: {self.resolution}")
        if self.rolling_window < 20:
            raise ValueError("rolling_window must be at least 20 bars")
        if not 20 <= self.min_history <= self.rolling_window:
            raise ValueError("min_history must be in [20, rolling_window]")
        if not 0.0 < self.oi_contraction_quantile < 0.5:
            raise ValueError("oi_contraction_quantile must be in (0, 0.5)")
        if not 0.5 < self.range_expansion_quantile < 1.0:
            raise ValueError("range_expansion_quantile must be in (0.5, 1)")
        if not 0.0 < self.untouched_fraction < 0.5:
            raise ValueError("untouched_fraction must be in (0, 0.5)")
        if not 0.0 < self.minimum_source_coverage <= 1.0:
            raise ValueError("minimum_source_coverage must be in (0, 1]")


@dataclass(frozen=True)
class ForcedFlowPanelResult:
    symbol: str
    index_symbol: str
    start_s: int
    end_s: int
    panel: pd.DataFrame
    quality: Mapping[str, Any]


async def fetch_delta_spot_index_symbol(
    symbol: str,
    *,
    base_url: str = DELTA_INDIA_API_URL,
    http_get_json: Callable[[str], dict] | None = None,
) -> str:
    """Read the exact spot index from the product contract, never guess it."""

    native = delta_native_symbol(symbol)
    get = http_get_json or _panel_http_get_json
    payload = await asyncio.to_thread(get, f"{base_url}/v2/products/{native}")
    if not isinstance(payload, dict) or not payload.get("success", False):
        raise ValueError(f"Delta product API error for {native}: {payload!r}")
    result = payload.get("result")
    spot_index = result.get("spot_index") if isinstance(result, dict) else None
    index_symbol = spot_index.get("symbol") if isinstance(spot_index, dict) else None
    if not isinstance(index_symbol, str) or not index_symbol.strip():
        raise ValueError(f"Delta product {native} has no spot index symbol")
    return index_symbol.strip()


def compute_causal_forced_flow_features(
    panel: pd.DataFrame,
    config: ForcedFlowPanelConfig,
) -> pd.DataFrame:
    """Add point-in-time features using only observations before each bar.

    The current OI change and range are compared with thresholds calculated
    from *shifted* history.  This is the critical difference from a full-sample
    quantile, which leaks future distribution information into old signals.
    """

    required = {"timestamp", "available_at", "open", "high", "low", "close", "volume"}
    missing = required - set(panel.columns)
    if missing:
        raise ValueError(f"panel missing required columns: {sorted(missing)}")
    out = panel.sort_values("timestamp").drop_duplicates("timestamp", keep="last").copy()
    out["range_bps"] = (out["high"] - out["low"]) / out["close"] * 10_000.0
    out["ret_bps"] = out["close"].pct_change(fill_method=None) * 10_000.0

    if {"mark", "index"}.issubset(out.columns):
        valid_index = out["index"].where(out["index"] != 0)
        out["basis"] = out["mark"] - valid_index
        out["basis_bps"] = out["basis"] / valid_index * 10_000.0

    if "oi" not in out.columns or not out["oi"].notna().any():
        out["forced_flow_score"] = np.nan
        out["cascade_flag"] = pd.Series(pd.NA, index=out.index, dtype="boolean")
        out["cascade_side"] = pd.Series(pd.NA, index=out.index, dtype="string")
        return out.reset_index(drop=True)

    out["oi_chg"] = out["oi"].diff()
    out["oi_chg_pct"] = out["oi"].pct_change(fill_method=None) * 100.0
    prior_oi = out["oi_chg"].shift(1)
    prior_range = out["range_bps"].shift(1)
    rolling_oi = prior_oi.rolling(config.rolling_window, min_periods=config.min_history)
    rolling_range = prior_range.rolling(
        config.rolling_window,
        min_periods=config.min_history,
    )
    out["oi_contraction_threshold"] = rolling_oi.quantile(
        config.oi_contraction_quantile
    )
    out["range_expansion_threshold_bps"] = rolling_range.quantile(
        config.range_expansion_quantile
    )

    oi_mean = rolling_oi.mean()
    oi_std = rolling_oi.std(ddof=0).replace(0.0, np.nan)
    range_mean = rolling_range.mean()
    range_std = rolling_range.std(ddof=0).replace(0.0, np.nan)
    oi_contraction_z = (oi_mean - out["oi_chg"]) / oi_std
    range_expansion_z = (out["range_bps"] - range_mean) / range_std
    ready = out["oi_contraction_threshold"].notna() & out[
        "range_expansion_threshold_bps"
    ].notna()
    out["forced_flow_score"] = (
        oi_contraction_z.clip(lower=0.0) * range_expansion_z.clip(lower=0.0)
    ).where(ready)
    candidate = (
        (out["oi_chg"] <= out["oi_contraction_threshold"])
        & (out["range_bps"] >= out["range_expansion_threshold_bps"])
    ).where(ready)
    out["cascade_flag"] = candidate.astype("boolean")
    side = pd.Series(pd.NA, index=out.index, dtype="string")
    side.loc[candidate.fillna(False) & out["ret_bps"].lt(0)] = "long_liquidation_proxy"
    side.loc[candidate.fillna(False) & out["ret_bps"].gt(0)] = "short_liquidation_proxy"
    side.loc[candidate.fillna(False) & out["ret_bps"].eq(0)] = "direction_unknown"
    out["cascade_side"] = side
    return out.reset_index(drop=True)


async def build_delta_forced_flow_panel(
    symbol: str,
    *,
    start_s: int,
    end_s: int,
    config: ForcedFlowPanelConfig | None = None,
    base_url: str = DELTA_INDIA_API_URL,
    http_get_json: Callable[[str], dict] | None = None,
) -> ForcedFlowPanelResult:
    """Build a causally timestamped price/mark/index/OI/funding panel."""

    settings = config or ForcedFlowPanelConfig()
    if start_s >= end_s:
        raise ValueError("start_s must be before end_s")
    if end_s > int(time.time()):
        raise ValueError("end_s cannot be in the future; forming candles are forbidden")
    native = delta_native_symbol(symbol)
    get = http_get_json or _panel_http_get_json
    index_symbol = await fetch_delta_spot_index_symbol(
        native,
        base_url=base_url,
        http_get_json=get,
    )

    async def candles(series_symbol: str) -> pd.DataFrame:
        return await fetch_delta_candle_history(
            series_symbol,
            resolution=settings.resolution,
            start_s=start_s,
            end_s=end_s,
            base_url=base_url,
            http_get_json=get,
        )

    price = await candles(native)
    if price.empty:
        raise ValueError(f"Delta returned no traded-price candles for {native}")
    mark = await candles(f"MARK:{native}")
    index = await candles(index_symbol)
    oi = await candles(f"OI:{native}")
    funding_days = max(1, math.ceil((end_s - start_s) / 86_400) + 1)
    funding = await fetch_delta_funding_history(
        native,
        days=funding_days,
        resolution="1h",
        base_url=base_url,
        now_s=end_s,
        http_get_json=get,
    )

    panel = price.copy()
    seconds = RESOLUTION_SECONDS[settings.resolution]
    panel["available_at"] = (
        panel["timestamp"] + pd.to_timedelta(seconds, unit="s")
    ).dt.as_unit("ms")
    for frame, name in ((mark, "mark"), (index, "index"), (oi, "oi")):
        values = frame[["timestamp", "close"]].rename(columns={"close": name})
        panel = panel.merge(values, on="timestamp", how="left", validate="one_to_one")

    if funding.empty:
        panel["funding_available_at"] = pd.NaT
        panel["funding_rate"] = np.nan
    else:
        settled = funding.rename(columns={"timestamp": "funding_available_at"}).sort_values(
            "funding_available_at"
        )
        panel = pd.merge_asof(
            panel.sort_values("available_at"),
            settled,
            left_on="available_at",
            right_on="funding_available_at",
            direction="backward",
            allow_exact_matches=True,
        )
    panel.insert(0, "symbol", native)
    panel = compute_causal_forced_flow_features(panel, settings)

    candle_report = validate_candles(
        panel[["timestamp", "open", "high", "low", "close", "volume"]],
        settings.resolution,
        allow_gaps=True,
        dataset=f"delta:{native}:{settings.resolution}",
    )
    source_coverage = {
        name: float(panel[name].notna().mean()) if name in panel else 0.0
        for name in ("mark", "index", "oi", "funding_rate")
    }
    feature_ready = int(panel["cascade_flag"].notna().sum())
    expected_first_open = pd.Timestamp(start_s, unit="s", tz="UTC")
    expected_last_open_s = (end_s // seconds) * seconds - seconds
    expected_last_open = pd.Timestamp(expected_last_open_s, unit="s", tz="UTC")
    boundary_checks = {
        "covers_requested_start": bool(panel["timestamp"].iloc[0] <= expected_first_open),
        "covers_last_closed_bar": bool(panel["timestamp"].iloc[-1] >= expected_last_open),
        "no_availability_after_end": bool(
            (panel["available_at"] <= pd.Timestamp(end_s, unit="s", tz="UTC")).all()
        ),
    }
    coverage_passed = all(
        value >= settings.minimum_source_coverage for value in source_coverage.values()
    )
    quality: dict[str, Any] = {
        "passed": candle_report.passed and coverage_passed and all(boundary_checks.values()),
        "candle_report": candle_report.to_dict(),
        "source_coverage": source_coverage,
        "minimum_source_coverage": settings.minimum_source_coverage,
        "source_coverage_passed": coverage_passed,
        "requested_boundary_checks": boundary_checks,
        "cascade_proxy_available": source_coverage["oi"] > 0.0,
        "feature_ready_rows": feature_ready,
        "causal_thresholds": True,
        "funding_timestamp_semantics": "settled_close_available_at_raw_start_plus_1h",
        "claims_actual_liquidations": False,
    }
    return ForcedFlowPanelResult(native, index_symbol, start_s, end_s, panel, quality)


def _panel_http_get_json(url: str) -> dict:
    """Bounded retrying public-data GET used by long panel downloads.

    A permanently missing page raises and aborts the symbol. Returning a panel
    with a silent hole would be more dangerous than returning no panel at all.
    """

    for attempt in range(1, _HTTP_ATTEMPTS + 1):
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "vnedge-delta-forced-flow-panel/1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:
                decoded = json.loads(response.read().decode("utf-8"))
            if not isinstance(decoded, dict):
                raise TypeError(f"Delta returned a non-object payload for {url}")
            return decoded
        except urllib.error.HTTPError as exc:
            retryable = exc.code == 429 or 500 <= exc.code < 600
            if not retryable or attempt == _HTTP_ATTEMPTS:
                raise
            reset_ms = exc.headers.get("X-RATE-LIMIT-RESET")
            delay = float(reset_ms) / 1000.0 if reset_ms else 0.5 * 2 ** (attempt - 1)
        except (TimeoutError, urllib.error.URLError):
            if attempt == _HTTP_ATTEMPTS:
                raise
            delay = 0.5 * 2 ** (attempt - 1)
        time.sleep(min(10.0, max(0.1, delay)))
    raise RuntimeError("unreachable Delta retry state")


def write_panel_artifacts(
    result: ForcedFlowPanelResult,
    config: ForcedFlowPanelConfig,
    output_dir: Path,
) -> dict[str, Any]:
    """Atomically write selection/untouched panels and their evidence manifest."""

    output_dir.mkdir(parents=True, exist_ok=True)
    split = int(len(result.panel) * (1.0 - config.untouched_fraction))
    if split <= 0 or split >= len(result.panel):
        raise ValueError("panel is too short for the configured untouched split")
    selection = result.panel.iloc[:split].copy()
    untouched = result.panel.iloc[split:].copy()
    stem = f"{result.symbol}_{config.resolution}"
    selection_path = output_dir / f"{stem}_selection.parquet"
    untouched_path = output_dir / f"{stem}_untouched.parquet"
    _atomic_parquet(selection_path, selection)
    _atomic_parquet(untouched_path, untouched)
    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "symbol": result.symbol,
        "index_symbol": result.index_symbol,
        "start_s": result.start_s,
        "end_s": result.end_s,
        "config": asdict(config),
        "quality": dict(result.quality),
        "selection": _artifact_record(selection_path, selection),
        "untouched": {
            **_artifact_record(untouched_path, untouched),
            "economics_evaluated": False,
            "sealed_for_single_evaluation": True,
        },
    }
    manifest_path = output_dir / f"{stem}_manifest.json"
    _atomic_json(manifest_path, manifest)
    return {**manifest, "manifest_path": str(manifest_path)}


def _artifact_record(path: Path, frame: pd.DataFrame) -> dict[str, Any]:
    return {
        "path": str(path),
        "rows": len(frame),
        "first_available_at": frame["available_at"].iloc[0].isoformat(),
        "last_available_at": frame["available_at"].iloc[-1].isoformat(),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _atomic_parquet(path: Path, frame: pd.DataFrame) -> None:
    temporary = path.with_suffix(".parquet.tmp")
    frame.to_parquet(temporary, index=False)
    temporary.replace(path)


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    with NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def _parse_end(value: str | None) -> int:
    if not value:
        return int(datetime.now(UTC).timestamp())
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.astimezone(UTC).timestamp())


async def _run_cli(args: argparse.Namespace) -> int:
    config = ForcedFlowPanelConfig(
        resolution=args.resolution,
        rolling_window=args.rolling_window,
        min_history=args.min_history,
        untouched_fraction=args.untouched_fraction,
    )
    end_s = _parse_end(args.end)
    start_s = end_s - args.days * 86_400
    output = Path(args.output)
    failures = 0
    for symbol in [item.strip() for item in args.symbols.split(",") if item.strip()]:
        try:
            result = await build_delta_forced_flow_panel(
                symbol,
                start_s=start_s,
                end_s=end_s,
                config=config,
            )
            if not result.quality["passed"]:
                raise ValueError(f"panel data-quality gate failed: {result.quality}")
            manifest = write_panel_artifacts(result, config, output)
            selection_rows = int(manifest["selection"]["rows"])
            selection = result.panel.iloc[:selection_rows]
            candidates = int(selection["cascade_flag"].fillna(False).sum())
            print(
                f"{result.symbol}: {len(result.panel):,} rows; "
                f"selection cascade candidates={candidates:,}; "
                f"untouched economics not evaluated; quality={result.quality['passed']}"
            )
        except Exception as exc:  # noqa: BLE001 - isolate independent CLI symbols
            failures += 1
            print(f"{symbol}: FAILED: {exc}")
    return 1 if failures else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="BTCUSD,ETHUSD")
    parser.add_argument("--resolution", choices=sorted(RESOLUTION_SECONDS), default="5m")
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--end", help="reproducible ISO-8601 UTC end; defaults to now")
    parser.add_argument("--output", default="research/live_research/delta_forced_flow_panel")
    parser.add_argument("--rolling-window", type=int, default=500)
    parser.add_argument("--min-history", type=int, default=100)
    parser.add_argument("--untouched-fraction", type=float, default=0.20)
    args = parser.parse_args()
    if args.days <= 0:
        parser.error("--days must be positive")
    raise SystemExit(asyncio.run(_run_cli(args)))


if __name__ == "__main__":
    main()
