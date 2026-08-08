"""Generate calendar-complete peer-review tables from the frozen v1 replay."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from pathlib import Path

import pandas as pd

DEFAULT_INPUT = Path(
    "research/live_research/range_compression_breakout_v1_latest.json"
)
DEFAULT_OUTPUT_DIR = Path("research/live_research/range_compression_breakout_v1_periods")


def _metrics(values: pd.Series) -> dict[str, float | int]:
    gains = float(values[values > 0].sum())
    losses = float(-values[values < 0].sum())
    count = len(values)
    return {
        "trades": count,
        "net_bps": float(values.sum()),
        "average_net_bps": float(values.mean()) if count else 0.0,
        "profit_factor": gains / losses if losses else (float("inf") if gains else 0.0),
        "win_rate": float((values > 0).mean()) if count else 0.0,
        "false_signal_rate": float((values <= 0).mean()) if count else 0.0,
    }


def build_period_tables(payload: dict) -> dict[str, pd.DataFrame]:
    untouched = payload.get("untouched", {})
    if untouched.get("status") != "sealed":
        raise ValueError("v1 period report expects the untouched tail to remain sealed")
    selection = payload["selection"]
    metrics = selection["metrics"]
    start = pd.Timestamp(metrics["score_started_at"]).floor("D")
    end = pd.Timestamp(metrics["score_ended_at"]).floor("D")
    calendar = pd.date_range(start, end, freq="D")
    trades = pd.DataFrame(selection["trades"])
    if trades.empty:
        trades = pd.DataFrame(columns=["exit_ts", "net_bps"])
    trades["exit_ts"] = pd.to_datetime(trades["exit_ts"], utc=True)

    keyers: dict[str, Callable[[pd.Timestamp], str]] = {
        "daily": lambda value: value.strftime("%Y-%m-%d"),
        "weekly": lambda value: (
            f"{value.isocalendar().year}-W{value.isocalendar().week:02d}"
        ),
        "monthly": lambda value: value.strftime("%Y-%m"),
        "quarterly": lambda value: f"{value.year}-Q{value.quarter}",
    }
    tables: dict[str, pd.DataFrame] = {}
    for name, keyer in keyers.items():
        calendar_keys = list(dict.fromkeys(keyer(value) for value in calendar))
        trade_keys = trades["exit_ts"].map(keyer) if len(trades) else pd.Series(dtype=str)
        rows = []
        for key in calendar_keys:
            values = trades.loc[trade_keys == key, "net_bps"].astype(float)
            rows.append({"period": key, **_metrics(values)})
        tables[name] = pd.DataFrame(rows)
    return tables


def write_period_tables(input_path: Path, output_dir: Path) -> dict[str, Path]:
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    tables = build_period_tables(payload)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for name, frame in tables.items():
        path = output_dir / f"{name}.csv"
        temporary = path.with_suffix(".csv.tmp")
        frame.to_csv(temporary, index=False)
        temporary.replace(path)
        paths[name] = path
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    paths = write_period_tables(args.input, args.output_dir)
    print(json.dumps({name: str(path) for name, path in paths.items()}, indent=2))


if __name__ == "__main__":
    main()
