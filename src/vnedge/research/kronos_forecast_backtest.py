"""Chronological, fee-aware diagnostic for pinned Kronos forecast artifacts.

This is intentionally a fixed-horizon forecast strategy, not a production
execution simulation: decide after one closed candle, enter at the next bar's
open, exit at the configured vertical barrier's close, and subtract the real
route cost.  MFE/MAE are recorded from the intervening actual candles.

The final chronological tail is labelled an evaluation tail, not "untouched".
Only a separately preregistered run may make an untouched-data claim.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import pandas as pd

from vnedge.data.schemas import TIMEFRAME_MS
from vnedge.research.kronos_forecast_gate import (
    ForecastRoute,
    KronosForecastGateConfig,
)
from vnedge.research.kronos_inference import (
    DEFAULT_KRONOS_REPO,
    KronosBackend,
    KronosInferenceConfig,
    UpstreamKronosBackend,
    generate_kronos_forecast,
)


@dataclass(frozen=True)
class KronosBacktestConfig:
    stride_bars: int = 24
    max_observations: int | None = None
    evaluation_tail_fraction: float = 0.20
    route: ForecastRoute = "maker_taker"

    def __post_init__(self) -> None:
        if self.stride_bars < 1:
            raise ValueError("stride_bars must be positive")
        if self.max_observations is not None and self.max_observations < 1:
            raise ValueError("max_observations must be positive when supplied")
        if not 0.05 <= self.evaluation_tail_fraction <= 0.50:
            raise ValueError("evaluation_tail_fraction must be in [0.05, 0.50]")


DEFAULT_BACKTEST_CONFIG = KronosBacktestConfig()
DEFAULT_GATE_CONFIG = KronosForecastGateConfig()


def run_kronos_forecast_backtest(
    candles: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    backend: KronosBackend,
    inference_config: KronosInferenceConfig,
    gate_config: KronosForecastGateConfig = DEFAULT_GATE_CONFIG,
    backtest_config: KronosBacktestConfig = DEFAULT_BACKTEST_CONFIG,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run chronological forecast decisions and fixed-horizon outcomes."""

    if timeframe not in TIMEFRAME_MS:
        raise ValueError(f"unsupported timeframe: {timeframe}")
    required = inference_config.lookback_bars + inference_config.horizon_bars
    if len(candles) < required:
        raise ValueError(f"need at least {required} candles, found {len(candles)}")
    frame = candles.copy(deep=True).reset_index(drop=True)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    last_decision_index = len(frame) - inference_config.horizon_bars - 1
    decision_indices = list(range(
        inference_config.lookback_bars - 1,
        last_decision_index + 1,
        backtest_config.stride_bars,
    ))
    if backtest_config.max_observations is not None:
        decision_indices = decision_indices[: backtest_config.max_observations]
    if not decision_indices:
        raise ValueError("no chronological decision points available")

    step = pd.Timedelta(milliseconds=TIMEFRAME_MS[timeframe])
    route_cost = gate_config.route_cost_bps(backtest_config.route)
    rows: list[dict[str, Any]] = []
    created = now or datetime.now(UTC)
    for decision_index in decision_indices:
        decision_candle = frame.iloc[decision_index]
        decision_timestamp = (
            decision_candle["timestamp"] + step
            if inference_config.timestamp_convention == "open"
            else decision_candle["timestamp"]
        )
        artifact = generate_kronos_forecast(
            frame.iloc[: decision_index + 1],
            symbol=symbol,
            timeframe=timeframe,
            decision_timestamp=decision_timestamp,
            backend=backend,
            config=inference_config,
            gate_config=gate_config,
            route=backtest_config.route,
            now=created,
        )
        selected_side = artifact.gate_decision["selected_side"]
        if selected_side not in {"long", "short"}:
            raise ValueError("forecast gate did not select a side")
        actual = frame.iloc[
            decision_index + 1 : decision_index + 1 + inference_config.horizon_bars
        ]
        selected_score = artifact.gate_decision["scores"][selected_side]
        outcome = _actual_outcome(actual, selected_side, route_cost)
        rows.append({
            "artifact_id": artifact.artifact_id,
            "artifact_sha256": artifact.payload_sha256,
            "decision_timestamp": artifact.decision_timestamp,
            "entry_timestamp": actual["timestamp"].iloc[0].isoformat(),
            "exit_timestamp": actual["timestamp"].iloc[-1].isoformat(),
            "side": selected_side,
            "gate_verdict": artifact.gate_decision["verdict"],
            "gate_pass": artifact.gate_decision["verdict"] == "FORECAST_GATE_PASS",
            "expected_terminal_bps": selected_score["terminal_move_bps"],
            "expected_net_bps": selected_score["expected_net_bps"],
            "forecast_confidence": selected_score["confidence"],
            "forecast_repairs": artifact.forecast_quality["ohlc_geometry_repairs"],
            **outcome,
        })

    split = max(1, int(len(rows) * (1.0 - backtest_config.evaluation_tail_fraction)))
    split = min(split, len(rows))
    for index, row in enumerate(rows):
        row["segment"] = "selection" if index < split else "evaluation_tail"
    selection = rows[:split]
    tail = rows[split:]
    generated_at = pd.Timestamp(created).tz_convert("UTC").isoformat()
    report: dict[str, Any] = {
        "report_id": "kronos_forecast_backtest_v1",
        "generated_at": generated_at,
        "symbol": symbol,
        "timeframe": timeframe,
        "contract": {
            "decision": "closed candle only",
            "entry": "next bar open",
            "exit": f"vertical barrier close after {inference_config.horizon_bars} bars",
            "intrabar_tie_policy": "not applicable; no stop or target in v1 diagnostic",
            "cost_bps": route_cost,
            "route": backtest_config.route,
            "evaluation_tail_is_untouched": False,
        },
        "inference_config": inference_config.to_dict(),
        "gate_config": gate_config.to_dict(),
        "backtest_config": asdict(backtest_config),
        "summary": _metrics(rows),
        "selection": _metrics(selection),
        "evaluation_tail": _metrics(tail),
        "gate_passed": _metrics([row for row in rows if row["gate_pass"]]),
        "selection_gate_passed": _metrics([
            row for row in selection if row["gate_pass"]
        ]),
        "evaluation_tail_gate_passed": _metrics([
            row for row in tail if row["gate_pass"]
        ]),
        "side_breakdown": {
            side: _metrics([row for row in rows if row["side"] == side])
            for side in ("long", "short")
        },
        "rows": rows,
        "operator_answer": (
            "Forecast economics are diagnostic only. This report cannot promote, "
            "paper trade, or claim a sealed untouched result."
        ),
        "can_trade": False,
        "can_promote": False,
        "research_only": True,
    }
    report["payload_sha256"] = _report_hash(report)
    return report


def write_backtest_report(report: dict[str, Any], path: Path | str) -> Path:
    expected = report.get("payload_sha256")
    if not expected or expected != _report_hash({k: v for k, v in report.items() if k != "payload_sha256"}):
        raise ValueError("refusing to write backtest report with invalid payload hash")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w", dir=target.parent, prefix=target.name, suffix=".tmp", delete=False, encoding="utf-8"
    ) as handle:
        json.dump(report, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(target)
    return target


def _actual_outcome(actual: pd.DataFrame, side: str, cost_bps: float) -> dict[str, float | bool]:
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
        "entry_price": entry,
        "exit_price": exit_price,
        "actual_gross_bps": round(gross, 6),
        "actual_net_bps": round(gross - cost_bps, 6),
        "actual_mfe_bps": round(favorable, 6),
        "actual_mae_bps": round(adverse, 6),
        "direction_correct": gross > 0,
    }


def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "observations": 0,
            "gate_passes": 0,
            "direction_accuracy": 0.0,
            "avg_gross_bps": 0.0,
            "avg_net_bps": 0.0,
            "profit_factor": 0.0,
            "total_net_bps": 0.0,
            "avg_mfe_bps": 0.0,
            "avg_mae_bps": 0.0,
            "terminal_mae_bps": 0.0,
        }
    net = [float(row["actual_net_bps"]) for row in rows]
    gains = sum(value for value in net if value > 0)
    losses = abs(sum(value for value in net if value < 0))
    profit_factor = gains / losses if losses else (math.inf if gains else 0.0)
    terminal_errors = [
        abs(float(row["expected_terminal_bps"]) - float(row["actual_gross_bps"]))
        for row in rows
    ]
    return {
        "observations": len(rows),
        "gate_passes": sum(bool(row["gate_pass"]) for row in rows),
        "direction_accuracy": round(sum(bool(row["direction_correct"]) for row in rows) / len(rows), 6),
        "avg_gross_bps": round(sum(float(row["actual_gross_bps"]) for row in rows) / len(rows), 6),
        "avg_net_bps": round(sum(net) / len(rows), 6),
        "profit_factor": round(profit_factor, 6) if math.isfinite(profit_factor) else "Infinity",
        "total_net_bps": round(sum(net), 6),
        "avg_mfe_bps": round(sum(float(row["actual_mfe_bps"]) for row in rows) / len(rows), 6),
        "avg_mae_bps": round(sum(float(row["actual_mae_bps"]) for row in rows) / len(rows), 6),
        "terminal_mae_bps": round(sum(terminal_errors) / len(rows), 6),
    }


def _bps(a: float, b: float) -> float:
    return ((a - b) / b) * 10_000.0


def _report_hash(payload: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in payload.items() if key != "payload_sha256"}
    blob = json.dumps(
        unsigned, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


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
    parser.add_argument("--kronos-repo", type=Path, default=DEFAULT_KRONOS_REPO)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--lookback", type=int, default=512)
    parser.add_argument("--horizon", type=int, default=24)
    parser.add_argument("--samples", type=int, default=16)
    parser.add_argument("--stride", type=int, default=24)
    parser.add_argument("--max-observations", type=int, default=None)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--local-files-only", action="store_true")
    parser.add_argument("--route", choices=["maker_taker", "taker_taker"], default="maker_taker")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    candles = _read_candles(args.candles)
    inference = KronosInferenceConfig(
        lookback_bars=args.lookback,
        horizon_bars=args.horizon,
        sample_paths=args.samples,
        seed=args.seed,
        device=args.device,
        local_files_only=args.local_files_only,
    )
    backtest = KronosBacktestConfig(
        stride_bars=args.stride,
        max_observations=args.max_observations,
        route=args.route,
    )
    backend = UpstreamKronosBackend(repo=args.kronos_repo, config=inference)
    report = run_kronos_forecast_backtest(
        candles,
        symbol=args.symbol,
        timeframe=args.timeframe,
        backend=backend,
        inference_config=inference,
        backtest_config=backtest,
    )
    write_backtest_report(report, args.out)
    print(json.dumps({
        "report": str(args.out),
        "summary": report["summary"],
        "payload_sha256": report["payload_sha256"],
        "can_trade": False,
        "can_promote": False,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
