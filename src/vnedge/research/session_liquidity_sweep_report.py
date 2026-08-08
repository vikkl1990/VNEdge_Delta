"""Generate calendar-complete official and setup tables for session sweep v1."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from pathlib import Path

import pandas as pd

from vnedge.research.range_compression_report import build_period_tables

DEFAULT_INPUT = Path(
    "research/live_research/session_liquidity_sweep_v1_latest.json"
)
DEFAULT_OUTPUT_DIR = Path(
    "research/live_research/session_liquidity_sweep_v1_periods"
)


def build_setup_tables(payload: dict) -> dict[str, pd.DataFrame]:
    if payload["untouched"]["status"] != "sealed":
        raise ValueError("session sweep v1 setup report expects a sealed tail")
    selection = payload["selection"]
    start = pd.Timestamp(selection["metrics"]["score_started_at"]).floor("D")
    end = pd.Timestamp(selection["metrics"]["score_ended_at"]).floor("D")
    calendar = pd.date_range(start, end, freq="D")
    records = pd.json_normalize(selection["setup_records"], sep=".")
    if records.empty:
        records = pd.DataFrame(columns=["decision_ts"])
    records["decision_ts"] = pd.to_datetime(records["decision_ts"], utc=True)
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
        keys = list(dict.fromkeys(keyer(value) for value in calendar))
        record_keys = (
            records["decision_ts"].map(keyer) if len(records) else pd.Series(dtype=str)
        )
        rows = []
        for key in keys:
            group = records.loc[record_keys == key]
            rows.append(
                {
                    "period": key,
                    "setups": len(group),
                    "entry_rejections": (
                        int((group["entry_geometry.status"] == "rejected").sum())
                        if len(group)
                        else 0
                    ),
                    "btc_setups": int((group["symbol"] == "BTCUSD").sum()) if len(group) else 0,
                    "eth_setups": int((group["symbol"] == "ETHUSD").sum()) if len(group) else 0,
                    "london_setups": int((group["session"] == "london").sum()) if len(group) else 0,
                    "new_york_setups": int((group["session"] == "new_york").sum()) if len(group) else 0,
                    "median_stop_bps": (
                        float(group["entry_geometry.stop_distance_bps"].median())
                        if len(group)
                        else 0.0
                    ),
                    "maximum_cost_multiple": (
                        float(group["entry_geometry.cost_multiple"].max())
                        if len(group)
                        else 0.0
                    ),
                }
            )
        tables[name] = pd.DataFrame(rows)
    return tables


def write_reports(input_path: Path, output_dir: Path) -> dict[str, Path]:
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    tables = {
        **{f"official_{key}": value for key, value in build_period_tables(payload).items()},
        **{f"setups_{key}": value for key, value in build_setup_tables(payload).items()},
    }
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
    paths = write_reports(args.input, args.output_dir)
    print(json.dumps({key: str(value) for key, value in paths.items()}, indent=2))


if __name__ == "__main__":
    main()
