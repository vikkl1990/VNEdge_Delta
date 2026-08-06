"""Selection-only CUSUM interaction attribution with sample-size controls.

The final untouched tail is never decomposed. Cells are descriptive metadata
for later meta-labeling and cannot enable scanner or execution gates.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from statistics import stdev
from tempfile import NamedTemporaryFile
from typing import Any

import pandas as pd

from vnedge.research.delta_scalper_attribution import normalize_trade, summarize

DEFAULT_BACKTEST = Path("research/live_research/delta_scalper_backtest_latest.json")
DEFAULT_OUTPUT = Path(
    "research/live_research/delta_scalper_cusum_interactions_latest.json"
)
DEFAULT_ARTIFACT_DIR = Path("research/cusum_interactions")
INTERACTION_FIELDS = (
    "change_point_window",
    "scanner_id",
    "symbol",
    "trend_regime",
    "volatility_regime",
)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def _historical(backtest: dict) -> list[dict]:
    return sorted(
        [
            normalize_trade(row, source="historical_backtest")
            for report in (backtest.get("markets") or {}).values()
            if isinstance(report, dict)
            for row in report.get("trades") or []
            if isinstance(row, dict)
        ],
        key=lambda row: str(row.get("exit_ts") or ""),
    )


def _cell(
    values: tuple[str, ...],
    members: list[dict],
    baseline_average: float,
    total_trades: int,
) -> dict:
    metrics = summarize(members)
    net = [float(row.get("net_bps") or 0.0) for row in members]
    average = float(metrics["average_net_bps"])
    standard_error = stdev(net) / len(net) ** 0.5 if len(net) > 1 else 0.0
    fields = dict(zip(INTERACTION_FIELDS, values))
    return {
        "key": " | ".join(values),
        **fields,
        **metrics,
        "average_net_standard_error_bps": standard_error,
        "average_net_ci95_low_bps": average - 1.96 * standard_error,
        "average_net_ci95_high_bps": average + 1.96 * standard_error,
        "average_net_uplift_vs_selection_bps": average - baseline_average,
        "pct_of_selection_trades": len(members) / total_trades * 100 if total_trades else 0.0,
    }


def interaction_cells(rows: list[dict]) -> list[dict]:
    groups: dict[tuple[str, ...], list[dict]] = defaultdict(list)
    for row in rows:
        values = tuple(str(row.get(field) or "unknown") for field in INTERACTION_FIELDS)
        groups[values].append(row)
    baseline_average = float(summarize(rows)["average_net_bps"])
    return sorted(
        (
            _cell(values, members, baseline_average, len(rows))
            for values, members in groups.items()
        ),
        key=lambda row: (-int(row["trades"]), str(row["key"])),
    )


def build_report(
    backtest: dict,
    *,
    minimum_eligible_trades: int = 100,
    minimum_diagnostic_trades: int = 80,
    near_zero_average_bps: float = -1.0,
    materially_worse_delta_bps: float = -2.0,
) -> dict[str, Any]:
    if minimum_eligible_trades < minimum_diagnostic_trades:
        raise ValueError("eligible minimum cannot be below diagnostic minimum")
    historical = _historical(backtest)
    fraction = float(
        (backtest.get("untouched_window") or {}).get("untouched_fraction") or 0.20
    )
    split = max(1, int(len(historical) * (1 - fraction))) if historical else 0
    selection = historical[:split]
    frozen = historical[split:]
    cells = interaction_cells(selection)
    eligible = [row for row in cells if int(row["trades"]) >= minimum_eligible_trades]
    near_threshold = [
        row
        for row in cells
        if minimum_diagnostic_trades
        <= int(row["trades"])
        < minimum_eligible_trades
    ]
    positive = [
        row
        for row in eligible
        if float(row["average_net_bps"]) > 0
        and (row["profit_factor"] is None or float(row["profit_factor"]) > 1)
    ]
    near_zero = [
        row
        for row in eligible
        if near_zero_average_bps <= float(row["average_net_bps"]) <= 0
    ]
    baseline = summarize(selection)
    worse_cutoff = float(baseline["average_net_bps"]) + materially_worse_delta_bps
    materially_worse = [
        row for row in eligible if float(row["average_net_bps"]) <= worse_cutoff
    ]
    imbalance_recent = [
        row
        for row in eligible
        if row["scanner_id"] == "delta_imbalance_fade_v1"
        and row["change_point_window"] == "00-30m"
    ]
    best = sorted(
        eligible,
        key=lambda row: (
            -float(row["average_net_bps"]),
            -float(row["profit_factor"] or 0.0),
            -int(row["trades"]),
        ),
    )
    worst = sorted(
        eligible,
        key=lambda row: (
            float(row["average_net_bps"]),
            float(row["profit_factor"] or 0.0),
            -int(row["trades"]),
        ),
    )
    return {
        "report_id": "delta_scalper_cusum_interactions_v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "interaction": "CUSUM window × scanner × symbol × trend regime × volatility regime",
        "sample_policy": {
            "eligible_minimum_trades": minimum_eligible_trades,
            "near_threshold_minimum_trades": minimum_diagnostic_trades,
            "near_zero_average_net_at_least_bps": near_zero_average_bps,
            "materially_worse_vs_selection_average_bps": materially_worse_delta_bps,
            "thresholds_preregistered_before_results": True,
        },
        "selection_window": {
            "trades": len(selection),
            "summary": baseline,
            "raw_cells": len(cells),
            "eligible_cells": len(eligible),
            "near_threshold_cells": len(near_threshold),
        },
        "eligible_cells": eligible,
        "near_threshold_diagnostics": near_threshold,
        "findings": {
            "positive_cells": positive,
            "near_zero_cells": near_zero,
            "best_eligible_cells": best[:20],
            "worst_eligible_cells": worst[:20],
            "materially_worse_cells": materially_worse,
            "imbalance_fade_recent_shift_cells": sorted(
                imbalance_recent,
                key=lambda row: -float(row["average_net_bps"]),
            ),
            "positive_cell_count": len(positive),
            "near_zero_cell_count": len(near_zero),
            "materially_worse_cell_count": len(materially_worse),
        },
        "frozen_untouched_window": {
            "trades": len(frozen),
            "summary": summarize(frozen),
            "subgroup_attribution_performed": False,
            "protected_from_interaction_selection": True,
        },
        "policy": {
            "research_only": True,
            "cusum_role": "journaled_meta_label_feature_only",
            "require_shift_enabled": False,
            "avoid_shift_enabled": False,
            "bocpd_enabled": False,
            "pelt_eligible_for_live_gate": False,
            "interaction_used_for_scanner_gate": False,
            "interaction_used_for_execution": False,
            "can_trade": False,
            "can_promote": False,
        },
        "can_trade": False,
        "can_promote": False,
    }


def write_artifacts(report: dict[str, Any], artifact_dir: Path) -> None:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(report["eligible_cells"]).to_csv(
        artifact_dir / "eligible_cells.csv", index=False
    )
    pd.DataFrame(report["near_threshold_diagnostics"]).to_csv(
        artifact_dir / "near_threshold_cells.csv", index=False
    )
    _atomic_json(artifact_dir / "interaction_report.json", report)


def run(args: argparse.Namespace) -> dict[str, Any]:
    backtest = json.loads(args.backtest.read_text(encoding="utf-8"))
    report = build_report(
        backtest,
        minimum_eligible_trades=args.minimum_eligible_trades,
        minimum_diagnostic_trades=args.minimum_diagnostic_trades,
        near_zero_average_bps=args.near_zero_average_bps,
        materially_worse_delta_bps=args.materially_worse_delta_bps,
    )
    _atomic_json(args.output, report)
    write_artifacts(report, args.artifact_dir)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backtest", type=Path, default=DEFAULT_BACKTEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--artifact-dir", type=Path, default=DEFAULT_ARTIFACT_DIR)
    parser.add_argument("--minimum-eligible-trades", type=int, default=100)
    parser.add_argument("--minimum-diagnostic-trades", type=int, default=80)
    parser.add_argument("--near-zero-average-bps", type=float, default=-1.0)
    parser.add_argument("--materially-worse-delta-bps", type=float, default=-2.0)
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.minimum_diagnostic_trades <= 0:
        raise ValueError("minimum diagnostic trades must be positive")
    report = run(args)
    print(
        json.dumps(
            {
                "selection": report["selection_window"],
                "positive_cells": report["findings"]["positive_cell_count"],
                "near_zero_cells": report["findings"]["near_zero_cell_count"],
                "materially_worse_cells": report["findings"][
                    "materially_worse_cell_count"
                ],
                "best": report["findings"]["best_eligible_cells"][:3],
            },
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
