"""Export strict Route A triple-barrier labels from Delta scalper outcomes.

The exporter understands the checked-in backtest report shape
(`markets.<symbol>.trades`) and flattened live-journal rows. Explicit labels
written by the shared path simulator are authoritative. Legacy rows fall back
to exit-reason reconstruction. MFE recovery for time stops is opt-in because it
is only an approximation when the recorded target contract differs.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import pandas as pd

DEFAULT_INPUT = Path("research/live_research/delta_scalper_backtest_latest.json")
DEFAULT_OUTPUT = Path("research/live_research/delta_scalper_with_tb_labels.parquet")
DEFAULT_SUMMARY = Path(
    "research/meta_labeling_triple_barrier/route_a_export_summary.json"
)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def _records(payload: object) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [dict(row) for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        raise TypeError("journal must be a JSON object or array")
    markets = payload.get("markets")
    if isinstance(markets, dict):
        records: list[dict[str, Any]] = []
        for symbol, report in markets.items():
            if not isinstance(report, dict):
                continue
            for row in report.get("trades") or []:
                if isinstance(row, dict):
                    record = dict(row)
                    record.setdefault("symbol", str(symbol).upper())
                    records.append(record)
        return records
    rows = payload.get("rows")
    if isinstance(rows, list):
        return [dict(row) for row in rows if isinstance(row, dict)]
    trades = payload.get("trades")
    if isinstance(trades, list):
        return [dict(row) for row in trades if isinstance(row, dict)]
    raise ValueError("journal contains no markets.*.trades, rows, or trades collection")


def load_journal(path: Path) -> pd.DataFrame:
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = _records(payload)
    if not records:
        raise ValueError("journal contains no outcome rows")
    return pd.json_normalize(records, sep=".")


def _first(frame: pd.DataFrame, keys: tuple[str, ...]) -> pd.Series:
    result = pd.Series(pd.NA, index=frame.index, dtype="object")
    for key in keys:
        if key in frame:
            result = result.where(result.notna(), frame[key])
    return result


def enrich_triple_barrier_labels(
    frame: pd.DataFrame,
    *,
    allow_mfe_time_stop_recovery: bool = False,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    net = pd.to_numeric(
        _first(frame, ("net_bps", "forward_outcome.net_bps")), errors="coerce"
    )
    trades = frame.loc[net.notna()].copy()
    if trades.empty:
        raise ValueError("journal has no resolved outcomes with net_bps")
    net = net.loc[trades.index]
    reason = _first(
        trades,
        ("exit_reason", "forward_outcome.exit_reason", "resolution"),
    ).fillna("").astype(str).str.lower()
    explicit = pd.to_numeric(
        _first(
            trades,
            (
                "triple_barrier.label",
                "triple_barrier_label",
                "forward_outcome.triple_barrier.label",
                "forward_outcome.triple_barrier_label",
            ),
        ),
        errors="coerce",
    )
    tp = pd.to_numeric(
        _first(
            trades,
            (
                "triple_barrier.tp_distance_bps",
                "upper_barrier_bps",
                "planned_target_bps",
                "forward_outcome.triple_barrier.tp_distance_bps",
                "forward_outcome.upper_barrier_bps",
                "forward_outcome.planned_target_bps",
            ),
        ),
        errors="coerce",
    ).abs()
    mfe = pd.to_numeric(
        _first(trades, ("mfe_bps", "forward_outcome.mfe_bps")), errors="coerce"
    )

    target = reason.isin(("target", "target_1", "take_profit"))
    stop = reason.eq("stop")
    time_stop = reason.isin(("time_stop", "timeout", "vertical"))
    label = target.astype("int8")
    source = pd.Series("exit_reason_strict", index=trades.index, dtype="object")
    first_barrier = pd.Series("unknown", index=trades.index, dtype="object")
    first_barrier.loc[target] = "upper"
    first_barrier.loc[stop] = "lower"
    first_barrier.loc[time_stop] = "vertical"

    explicit_mask = explicit.notna()
    label.loc[explicit_mask] = explicit.loc[explicit_mask].astype("int8")
    source.loc[explicit_mask] = "shared_path_simulator"
    recovered = pd.Series(False, index=trades.index)
    if allow_mfe_time_stop_recovery:
        recovered = time_stop & ~explicit_mask & mfe.notna() & tp.notna() & (mfe >= tp)
        label.loc[recovered] = 1
        first_barrier.loc[recovered] = "upper_inferred_from_mfe"
        source.loc[recovered] = "mfe_time_stop_approximation"

    trades["tb_label"] = label
    trades["tb_first_barrier"] = first_barrier
    trades["tb_label_source"] = source
    trades["tb_tp_distance_bps"] = tp
    trades["net_positive"] = (net > 0).astype("int8")
    trades["net_gt_4bps"] = (net > 4).astype("int8")
    trades["net_bps_resolved"] = net

    cross = pd.crosstab(reason, label)
    summary: dict[str, Any] = {
        "input_rows": len(frame),
        "resolved_outcomes": len(trades),
        "positive_labels": int(label.sum()),
        "positive_label_rate": float(label.mean()),
        "net_positive": int((net > 0).sum()),
        "net_gt_4bps": int((net > 4).sum()),
        "tb_vs_net_gt_4bps_disagreements": int((label != (net > 4)).sum()),
        "explicit_shared_simulator_labels": int(explicit_mask.sum()),
        "mfe_time_stop_recovery_enabled": allow_mfe_time_stop_recovery,
        "mfe_time_stop_labels_recovered": int(recovered.sum()),
        "unknown_exit_reasons": int((first_barrier == "unknown").sum()),
        "exit_reason_by_label": {
            str(index): {str(column): int(value) for column, value in row.items()}
            for index, row in cross.to_dict(orient="index").items()
        },
        "policy": {
            "explicit_path_simulator_label_is_authoritative": True,
            "strict_time_stop_is_failure": True,
            "mfe_recovery_is_approximate_and_opt_in": True,
            "predicted_move_is_not_used_as_a_target_fallback": True,
        },
    }
    return trades.reset_index(drop=True), summary


def run(
    input_path: Path,
    output_path: Path,
    summary_path: Path,
    *,
    allow_mfe_time_stop_recovery: bool = False,
) -> dict[str, Any]:
    frame = load_journal(input_path)
    trades, summary = enrich_triple_barrier_labels(
        frame,
        allow_mfe_time_stop_recovery=allow_mfe_time_stop_recovery,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    trades.to_parquet(temporary, index=False)
    temporary.replace(output_path)
    summary.update(
        {
            "input": str(input_path),
            "output": str(output_path),
            "summary": str(summary_path),
        }
    )
    _atomic_json(summary_path, summary)
    return summary


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument(
        "--allow-mfe-time-stop-recovery",
        action="store_true",
        help="approximate legacy time-stop labels from MFE; disabled by default",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    summary = run(
        args.input,
        args.output,
        args.summary,
        allow_mfe_time_stop_recovery=args.allow_mfe_time_stop_recovery,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
