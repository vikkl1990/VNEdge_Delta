"""Selection-only integrity audit and extreme-event diagnostic for Delta funding.

The script deliberately keeps the validation tail unopened. Raw funding candle
timestamps are interval starts; every close becomes usable one hour later.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import urllib.parse
from bisect import bisect_right
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import fmean, median
from tempfile import NamedTemporaryFile
from typing import Any

import pandas as pd
import yaml

from vnedge.data.delta_native_history import DELTA_INDIA_API_URL, _http_get_json
from vnedge.exchange.delta_ws import delta_native_symbol
from vnedge.research.delta_scalper_backtest import _load_candles
from vnedge.scalping.delta_engine.types import Candle

DEFAULT_CONFIG = Path("configs/research/delta_funding_audit_v1.yaml")
DEFAULT_CACHE = Path("data/delta_scalper_cache")
DEFAULT_OUTPUT = Path("research/live_research/delta_funding_audit_v1_latest.json")
DEFAULT_EVENT_OUTPUT = Path("research/live_research/delta_funding_audit_v1_events.parquet")
_PAGE_ROWS = 1000


@dataclass(frozen=True)
class RawFetch:
    symbol: str
    rows: tuple[dict[str, Any], ...]
    request_count: int


def _parse_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


async def fetch_raw_funding_candles(
    symbol: str,
    start: datetime,
    end: datetime,
    *,
    resolution: str = "1h",
    step_seconds: int = 3600,
    base_url: str = DELTA_INDIA_API_URL,
    http_get_json: Callable[[str], dict] | None = None,
) -> RawFetch:
    """Fetch raw pages without cleaning so duplicates/malformed rows are auditable."""
    if start >= end:
        raise ValueError("start must be before end")
    get = http_get_json or _http_get_json
    native = delta_native_symbol(symbol)
    cursor = int(start.timestamp())
    end_s = int(end.timestamp())
    window = _PAGE_ROWS * step_seconds
    rows: list[dict[str, Any]] = []
    requests = 0
    while cursor < end_s:
        window_end = min(cursor + window, end_s)
        query = urllib.parse.urlencode(
            {
                "resolution": resolution,
                "symbol": f"FUNDING:{native}",
                "start": cursor,
                "end": window_end,
            }
        )
        payload = await asyncio.to_thread(get, f"{base_url}/v2/history/candles?{query}")
        requests += 1
        if not isinstance(payload, dict) or not payload.get("success", False):
            raise ValueError(f"Delta funding API error for {native}: {payload!r}")
        result = payload.get("result") or []
        if not isinstance(result, list):
            raise TypeError(f"Delta funding result is not a list for {native}")
        rows.extend(item if isinstance(item, dict) else {"_malformed": item} for item in result)
        cursor = window_end
    return RawFetch(native, tuple(rows), requests)


def audit_raw_funding(
    raw: RawFetch,
    start: datetime,
    end: datetime,
    settings: dict[str, Any],
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Validate raw schema, cadence, units, and conservative availability."""
    step = int(settings["expected_cadence_seconds"])
    divisor = float(settings["raw_to_canonical_divisor"])
    valid_rows: list[dict[str, Any]] = []
    malformed = 0
    ohlc_invalid = 0
    for item in raw.rows:
        try:
            raw_start_s = int(item["time"])
            values = {name: float(item[name]) for name in ("open", "high", "low", "close")}
        except (KeyError, TypeError, ValueError, OverflowError):
            malformed += 1
            continue
        if not (
            values["low"] <= values["open"] <= values["high"]
            and values["low"] <= values["close"] <= values["high"]
        ):
            ohlc_invalid += 1
        valid_rows.append(
            {
                "symbol": raw.symbol,
                "raw_start_s": raw_start_s,
                "raw_start": pd.Timestamp(raw_start_s, unit="s", tz="UTC"),
                "available_at": pd.Timestamp(raw_start_s + step, unit="s", tz="UTC"),
                "raw_percent": values["close"],
                "funding_rate": values["close"] / divisor,
            }
        )
    frame = pd.DataFrame(valid_rows)
    if frame.empty:
        report = {
            "symbol": raw.symbol,
            "passed": False,
            "raw_rows": len(raw.rows),
            "malformed_rows": malformed,
            "failure": "no_valid_rows",
        }
        return frame, report

    duplicate_mask = frame.duplicated("raw_start_s", keep=False)
    duplicate_rows = int(duplicate_mask.sum())
    duplicate_timestamps = int(frame.loc[duplicate_mask, "raw_start_s"].nunique())
    conflicting_duplicates = 0
    if duplicate_timestamps:
        conflicting_duplicates = int(
            (frame.loc[duplicate_mask].groupby("raw_start_s")["raw_percent"].nunique() > 1).sum()
        )
    frame = (
        frame.drop_duplicates("raw_start_s", keep="last")
        .sort_values("raw_start_s")
        .reset_index(drop=True)
    )
    incomplete = int((frame["available_at"] > pd.Timestamp(end)).sum())
    frame = frame.loc[frame["available_at"] <= pd.Timestamp(end)].copy()
    expected = pd.date_range(
        pd.Timestamp(start) + pd.Timedelta(seconds=step),
        pd.Timestamp(end),
        freq=pd.Timedelta(seconds=step),
    )
    actual = pd.DatetimeIndex(frame["available_at"])
    missing = expected.difference(actual)
    unexpected = actual.difference(expected)
    diffs = frame["raw_start_s"].diff().dropna().astype(int)
    cadence_counts = {str(key): int(value) for key, value in Counter(diffs).items()}
    raw_limit = float(settings["maximum_abs_raw_percent_sanity"])
    canonical_limit = float(settings["maximum_abs_canonical_fraction_sanity"])
    missing_fraction = len(missing) / len(expected) if len(expected) else 1.0
    checks = {
        "has_rows": not frame.empty,
        "timestamps_hour_aligned": bool((frame["raw_start_s"] % step == 0).all()),
        "no_conflicting_duplicates": conflicting_duplicates == 0,
        "no_ohlc_inversions": ohlc_invalid == 0,
        "raw_percent_units_plausible": bool(frame["raw_percent"].abs().max() <= raw_limit),
        "canonical_fraction_units_plausible": bool(
            frame["funding_rate"].abs().max() <= canonical_limit
        ),
        "settled_only": bool((frame["available_at"] <= pd.Timestamp(end)).all()),
        "no_unexpected_timestamps": len(unexpected) == 0,
        "coverage_within_gap_budget": (
            missing_fraction <= float(settings["maximum_missing_fraction"])
        ),
    }
    report = {
        "symbol": raw.symbol,
        "passed": all(checks.values()),
        "checks": checks,
        "request_count": raw.request_count,
        "raw_rows": len(raw.rows),
        "valid_rows_before_dedup": len(valid_rows),
        "settled_unique_rows": len(frame),
        "malformed_rows": malformed,
        "ohlc_invalid_rows": ohlc_invalid,
        "duplicate_rows": duplicate_rows,
        "duplicate_timestamps": duplicate_timestamps,
        "conflicting_duplicate_timestamps": conflicting_duplicates,
        "forming_rows_excluded": incomplete,
        "expected_rows": len(expected),
        "missing_rows": len(missing),
        "missing_fraction": missing_fraction,
        "first_missing": [value.isoformat() for value in missing[:10]],
        "unexpected_rows": len(unexpected),
        "cadence_seconds": cadence_counts,
        "coverage": {
            "first_raw_start": frame["raw_start"].min().isoformat() if len(frame) else None,
            "last_raw_start": frame["raw_start"].max().isoformat() if len(frame) else None,
            "first_available_at": frame["available_at"].min().isoformat() if len(frame) else None,
            "last_available_at": frame["available_at"].max().isoformat() if len(frame) else None,
        },
        "raw_percent": _series_summary(frame["raw_percent"]),
        "canonical_fraction": _series_summary(frame["funding_rate"]),
        "availability": {
            "raw_timestamp_semantics": "hourly_candle_start",
            "offset_seconds": step,
            "join_key": "available_at",
            "forward_fill_from_raw_start": False,
        },
    }
    return frame.reset_index(drop=True), report


def _series_summary(series: pd.Series) -> dict[str, float]:
    return {
        "minimum": float(series.min()),
        "p01": float(series.quantile(0.01)),
        "median": float(series.median()),
        "p99": float(series.quantile(0.99)),
        "maximum": float(series.max()),
        "mean": float(series.mean()),
        "zero_fraction": float((series == 0).mean()),
    }


def detect_extreme_events(
    frame: pd.DataFrame,
    *,
    history: int,
    threshold: float,
    cooldown_hours: int,
) -> pd.DataFrame:
    """Causal crossing events using only prints available before the current print."""
    ordered = frame.sort_values("available_at").copy()
    prior = ordered["funding_rate"].shift(1)
    mean = prior.rolling(history, min_periods=history).mean()
    std = prior.rolling(history, min_periods=history).std(ddof=0)
    ordered["zscore"] = (ordered["funding_rate"] - mean) / std.replace(0.0, math.nan)
    magnitude = ordered["zscore"].abs()
    previous_magnitude = magnitude.shift(1)
    crossing = magnitude.ge(threshold) & (
        previous_magnitude.isna() | previous_magnitude.lt(threshold)
    )
    candidates = ordered.loc[crossing].copy()
    keep: list[int] = []
    last: pd.Timestamp | None = None
    cooldown = pd.Timedelta(hours=cooldown_hours)
    for index, row in candidates.iterrows():
        event_at = row["available_at"]
        if last is None or event_at - last >= cooldown:
            keep.append(index)
            last = event_at
    return candidates.loc[keep].reset_index(drop=True)


def evaluate_event_paths(
    symbol: str,
    events: pd.DataFrame,
    candles: list[Candle],
    *,
    horizons: list[int],
    cost_bps: float,
) -> list[dict[str, Any]]:
    """Evaluate both preregistered directions from the first causal 1m open."""
    ordered = sorted(candles, key=lambda bar: bar.ts)
    closes = [bar.ts for bar in ordered]
    rows: list[dict[str, Any]] = []
    for event_index, event in events.iterrows():
        available_at = event["available_at"].to_pydatetime()
        entry_index = bisect_right(closes, available_at)
        if entry_index >= len(ordered):
            continue
        entry_bar = ordered[entry_index]
        entry = float(entry_bar.open)
        funding_sign = 1 if float(event["funding_rate"]) > 0 else -1
        for horizon in horizons:
            end_at = available_at + timedelta(hours=horizon)
            path = [bar for bar in ordered[entry_index:] if bar.ts <= end_at]
            if not path or path[-1].ts < end_at:
                continue
            terminal = float(path[-1].close)
            for orientation, side in (
                ("continuation", funding_sign),
                ("reversal", -funding_sign),
            ):
                if side > 0:
                    mfe = (max(bar.high for bar in path) / entry - 1.0) * 10_000
                    mae = (1.0 - min(bar.low for bar in path) / entry) * 10_000
                else:
                    mfe = (1.0 - min(bar.low for bar in path) / entry) * 10_000
                    mae = (max(bar.high for bar in path) / entry - 1.0) * 10_000
                gross = side * (terminal / entry - 1.0) * 10_000
                rows.append(
                    {
                        "event_id": f"{symbol}:{event['available_at'].isoformat()}",
                        "event_index": int(event_index),
                        "symbol": symbol,
                        "raw_start": event["raw_start"],
                        "available_at": event["available_at"],
                        "entry_bar_close": entry_bar.ts,
                        "entry_price": entry,
                        "funding_rate": float(event["funding_rate"]),
                        "funding_zscore": float(event["zscore"]),
                        "orientation": orientation,
                        "side": "long" if side > 0 else "short",
                        "horizon_hours": horizon,
                        "terminal_gross_bps": gross,
                        "net_bps": gross - cost_bps,
                        "mfe_bps": max(0.0, mfe),
                        "mae_bps": max(0.0, mae),
                        "cost_bps": cost_bps,
                    }
                )
    return rows


def _profit_factor(values: list[float]) -> float:
    gains = sum(value for value in values if value > 0)
    losses = -sum(value for value in values if value < 0)
    return gains / losses if losses else (float("inf") if gains else 0.0)


def summarize_paths(frame: pd.DataFrame) -> dict[str, Any]:
    if frame.empty:
        return {"rows": 0, "groups": []}
    groups: list[dict[str, Any]] = []
    for keys, group in frame.groupby(
        ["orientation", "horizon_hours", "symbol"], sort=True, observed=True
    ):
        orientation, horizon, symbol = keys
        net = group["net_bps"].astype(float).tolist()
        groups.append(
            {
                "orientation": orientation,
                "horizon_hours": int(horizon),
                "symbol": symbol,
                "events": len(group),
                "average_terminal_gross_bps": float(group["terminal_gross_bps"].mean()),
                "average_net_bps": fmean(net),
                "profit_factor": _profit_factor(net),
                "win_rate": sum(value > 0 for value in net) / len(net),
                "false_signal_rate": sum(value <= 0 for value in net) / len(net),
                "median_mfe_bps": float(group["mfe_bps"].median()),
                "average_mfe_bps": float(group["mfe_bps"].mean()),
                "median_mae_bps": float(group["mae_bps"].median()),
                "average_mae_bps": float(group["mae_bps"].mean()),
            }
        )
    return {"rows": len(frame), "groups": groups}


def diagnostic_gate(frame: pd.DataFrame, config: dict[str, Any]) -> dict[str, Any]:
    rules = config["go_no_go"]
    primary = int(config["event_diagnostic"]["primary_horizon_hours"])
    results: dict[str, Any] = {}
    for orientation in config["event_diagnostic"]["orientations"]:
        subset = frame.loc[
            (frame["orientation"] == orientation) & (frame["horizon_hours"] == primary)
        ].sort_values("available_at")
        by_market = {symbol: group for symbol, group in subset.groupby("symbol", observed=True)}
        net = subset["net_bps"].astype(float).tolist()
        event_counts = {symbol: len(group) for symbol, group in by_market.items()}
        split = len(subset) // 2
        first = subset.iloc[:split]
        second = subset.iloc[split:]
        checks = {
            "minimum_total_events": len(subset) >= int(rules["minimum_total_events"]),
            "minimum_events_per_market": all(
                event_counts.get(symbol, 0) >= int(rules["minimum_events_per_market"])
                for symbol in config["data"]["symbols"]
            ),
            "positive_average_net": (
                bool(net) and fmean(net) > float(rules["primary_horizon_average_net_bps_minimum"])
            ),
            "profit_factor": (
                _profit_factor(net) >= float(rules["primary_horizon_profit_factor_minimum"])
            ),
            "false_signal_rate": (
                bool(net)
                and sum(value <= 0 for value in net) / len(net)
                < float(rules["maximum_false_signal_rate"])
            ),
            "market_concentration": (
                bool(net)
                and max(event_counts.values(), default=0) / len(net)
                <= float(rules["maximum_single_market_event_fraction"])
            ),
            "both_markets_positive": all(
                symbol in by_market and float(by_market[symbol]["net_bps"].mean()) > 0
                for symbol in config["data"]["symbols"]
            ),
            "both_chronological_halves_positive": (
                len(first) > 0
                and len(second) > 0
                and float(first["net_bps"].mean()) > 0
                and float(second["net_bps"].mean()) > 0
            ),
            "median_mfe_cost_multiple": (
                len(subset) > 0
                and median(subset["mfe_bps"].astype(float))
                >= float(rules["require_median_mfe_cost_multiple"])
                * float(config["event_diagnostic"]["fee_bps_round_trip"])
            ),
        }
        results[orientation] = {
            "passed": all(checks.values()),
            "checks": checks,
            "events": len(subset),
            "market_events": event_counts,
            "average_net_bps": fmean(net) if net else 0.0,
            "profit_factor": _profit_factor(net),
            "first_half_average_net_bps": (float(first["net_bps"].mean()) if len(first) else 0.0),
            "second_half_average_net_bps": (
                float(second["net_bps"].mean()) if len(second) else 0.0
            ),
        }
    passing = [name for name, result in results.items() if result["passed"]]
    return {
        "decision": "GO_PREREGISTER_SCANNER" if passing else "NO_GO",
        "passing_orientations": passing,
        "primary_horizon_hours": primary,
        "orientations": results,
        "tail_opened": False,
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    config_path = Path(args.config)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    start = _parse_ts(config["data"]["selection_start"])
    end = _parse_ts(config["data"]["selection_end"])
    symbols = list(config["data"]["symbols"])
    raw_fetches = await asyncio.gather(
        *(fetch_raw_funding_candles(symbol, start, end) for symbol in symbols)
    )
    funding_frames: dict[str, pd.DataFrame] = {}
    audit_reports: dict[str, dict[str, Any]] = {}
    for raw in raw_fetches:
        frame, report = audit_raw_funding(raw, start, end, config["funding_integrity"])
        funding_frames[raw.symbol] = frame
        audit_reports[raw.symbol] = report
    integrity_passed = all(report["passed"] for report in audit_reports.values())
    diagnostic: dict[str, Any] = {
        "status": "blocked_by_funding_integrity",
        "events_computed": False,
    }
    event_rows: list[dict[str, Any]] = []
    if integrity_passed:
        candles = await asyncio.gather(
            *(
                _load_candles(
                    symbol,
                    start,
                    end,
                    cache_dir=Path(args.cache_dir),
                    refresh=False,
                )
                for symbol in symbols
            )
        )
        settings = config["event_diagnostic"]
        horizons = [
            int(settings["primary_horizon_hours"]),
            *(int(value) for value in settings["secondary_horizons_hours"]),
        ]
        for symbol, rows in zip(symbols, candles, strict=True):
            events = detect_extreme_events(
                funding_frames[symbol],
                history=int(settings["zscore_history_prints"]),
                threshold=float(settings["absolute_zscore_threshold"]),
                cooldown_hours=int(settings["event_cooldown_hours"]),
            )
            event_rows.extend(
                evaluate_event_paths(
                    symbol,
                    events,
                    rows,
                    horizons=sorted(set(horizons)),
                    cost_bps=float(settings["fee_bps_round_trip"]),
                )
            )
        event_frame = pd.DataFrame(event_rows)
        summary = summarize_paths(event_frame)
        gate = diagnostic_gate(event_frame, config)
        diagnostic = {
            "status": "completed_selection_only",
            "events_computed": True,
            "unique_events": int(event_frame["event_id"].nunique()) if len(event_frame) else 0,
            "summary": summary,
            "gate": gate,
        }
        event_path = Path(args.event_output)
        event_path.parent.mkdir(parents=True, exist_ok=True)
        event_frame.to_parquet(event_path, index=False)
    payload = {
        "report_id": "delta_funding_audit_v1_first_run",
        "generated_at": datetime.now(UTC).isoformat(),
        "contract": str(config_path),
        "contract_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "selection_window": {"start": start.isoformat(), "end": end.isoformat()},
        "integrity": {
            "passed": integrity_passed,
            "symbols": audit_reports,
            "loader_defect_found_and_fixed": {
                "defect": "forming hourly close was timestamped at raw candle start",
                "fix": "exclude forming row and expose close at raw start plus one hour",
            },
        },
        "diagnostic": diagnostic,
        "untouched": {
            "status": "sealed",
            "funding_values_read": False,
            "price_values_read": False,
            "predictions_computed": False,
            "opened_once": False,
            "start": config["data"]["untouched_start"],
            "end": config["data"]["untouched_end"],
        },
        "safety": {
            "research_only": True,
            "can_trade": False,
            "can_promote": False,
            "order_route_present": False,
        },
    }
    _atomic_json(Path(args.output), payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--event-output", type=Path, default=DEFAULT_EVENT_OUTPUT)
    args = parser.parse_args()
    payload = asyncio.run(run(args))
    print(
        json.dumps(
            {
                "integrity_passed": payload["integrity"]["passed"],
                "diagnostic_status": payload["diagnostic"]["status"],
                "decision": payload["diagnostic"].get("gate", {}).get("decision"),
                "untouched": payload["untouched"]["status"],
                "output": str(args.output),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
