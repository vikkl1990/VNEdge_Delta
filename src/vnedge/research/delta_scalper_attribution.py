"""Loss attribution for the research-only Delta scalper engine.

The selection window may be decomposed for diagnosis. The chronologically
frozen final window is deliberately kept aggregate-only so subgroup inspection
cannot silently turn it into training data.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean, median
from tempfile import NamedTemporaryFile
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
DEFAULT_BACKTEST = Path("research/live_research/delta_scalper_backtest_latest.json")
DEFAULT_JOURNAL = Path("logs/delta_scalper/delta_scalper_shadow.journal.jsonl")
DEFAULT_OUTPUT = Path("research/live_research/delta_scalper_attribution_latest.json")


def _float(row: dict, key: str) -> float:
    try:
        return float(row.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _optional_float(row: dict, key: str) -> float | None:
    value = row.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _timestamp(value: object) -> datetime:
    stamp = datetime.fromisoformat(str(value))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.astimezone(UTC)


def _hold_bucket(seconds: float) -> str:
    if seconds <= 60:
        return "00-01m"
    if seconds <= 5 * 60:
        return "01-05m"
    if seconds <= 15 * 60:
        return "05-15m"
    if seconds <= 30 * 60:
        return "15-30m"
    return "30m+"


def _numeric_bucket(
    value: float | None,
    boundaries: tuple[float, ...],
    labels: tuple[str, ...],
) -> str:
    if value is None:
        return "unavailable"
    for boundary, label in zip(boundaries, labels):
        if value < boundary:
            return label
    return labels[-1]


def normalize_trade(row: dict, *, source: str) -> dict:
    entry_ts = _timestamp(row["entry_ts"])
    expected_move = _optional_float(row, "expected_move_bps")
    probability = _optional_float(row, "scalper_probability")
    confidence = _optional_float(row, "confidence")
    l2_quality = row.get("l2_quality")
    if not l2_quality:
        l2_quality = (
            "historical_unavailable" if source == "historical_backtest" else "unavailable"
        )
    return {
        **row,
        "source": source,
        "scanner_id": str(row.get("scanner_id") or "unknown"),
        "symbol": str(row.get("symbol") or "unknown").upper(),
        "regime": str(row.get("regime_at_entry") or row.get("regime") or "unknown"),
        "side": str(row.get("side") or "unknown").lower(),
        "exit_reason": str(row.get("exit_reason") or "unknown"),
        "entry_hour_utc": f"{entry_ts.hour:02d}:00 UTC",
        "entry_hour_ist": f"{entry_ts.astimezone(IST).hour:02d}:00 IST",
        "hold_bucket": _hold_bucket(_float(row, "hold_seconds")),
        "move_size_bucket": _numeric_bucket(
            expected_move,
            (12.0, 18.0, 24.0, 30.0),
            ("<12bps", "12-18bps", "18-24bps", "24-30bps", "30bps+"),
        ),
        "probability_bucket": _numeric_bucket(
            probability,
            (0.74, 0.78, 0.82, 0.86),
            ("<0.74", "0.74-0.78", "0.78-0.82", "0.82-0.86", "0.86+"),
        ),
        "confidence_bucket": _numeric_bucket(
            confidence,
            (0.68, 0.76, 0.84, 0.90),
            ("<0.68", "0.68-0.76", "0.76-0.84", "0.84-0.90", "0.90+"),
        ),
        "l2_quality": str(l2_quality),
        "net_bps": _float(row, "net_bps"),
        "gross_bps": _float(row, "gross_bps"),
        "cost_bps": _float(row, "cost_bps"),
        "mfe_bps": _float(row, "mfe_bps"),
        "mae_bps": _float(row, "mae_bps"),
        "expected_net_bps": _float(row, "expected_net_bps"),
    }


def summarize(rows: Iterable[dict]) -> dict:
    members = list(rows)
    net = [_float(row, "net_bps") for row in members]
    wins = [value for value in net if value > 0]
    losses = [-value for value in net if value < 0]
    gross = [_float(row, "gross_bps") for row in members]
    costs = [_float(row, "cost_bps") for row in members]
    mfe = [_float(row, "mfe_bps") for row in members]
    mae = [_float(row, "mae_bps") for row in members]
    expected = [_float(row, "expected_net_bps") for row in members]
    count = len(members)
    return {
        "trades": count,
        "net_bps": sum(net),
        "average_net_bps": fmean(net) if net else 0.0,
        "median_net_bps": median(net) if net else 0.0,
        "gross_bps": sum(gross),
        "cost_bps": sum(costs),
        "average_cost_bps": fmean(costs) if costs else 0.0,
        "hit_rate": len(wins) / count if count else 0.0,
        "false_signal_rate": sum(value <= 0 for value in net) / count if count else 0.0,
        "profit_factor": sum(wins) / sum(losses) if losses else None,
        "average_win_bps": fmean(wins) if wins else 0.0,
        "average_loss_bps": fmean(losses) if losses else 0.0,
        "average_mfe_bps": fmean(mfe) if mfe else 0.0,
        "average_mae_bps": fmean(mae) if mae else 0.0,
        "average_expected_net_bps": fmean(expected) if expected else 0.0,
        "expectation_error_bps": (
            fmean(realized - predicted for realized, predicted in zip(net, expected))
            if net
            else 0.0
        ),
        "same_bar_ambiguity_rate": (
            sum(bool(row.get("same_bar_ambiguous")) for row in members) / count
            if count
            else 0.0
        ),
    }


def _pearson(left: list[float], right: list[float]) -> float | None:
    if len(left) < 2 or len(left) != len(right):
        return None
    left_mean = fmean(left)
    right_mean = fmean(right)
    numerator = sum(
        (x_value - left_mean) * (y_value - right_mean)
        for x_value, y_value in zip(left, right)
    )
    left_scale = sum((value - left_mean) ** 2 for value in left) ** 0.5
    right_scale = sum((value - right_mean) ** 2 for value in right) ** 0.5
    denominator = left_scale * right_scale
    return numerator / denominator if denominator else None


def signal_quality_diagnostics(rows: list[dict]) -> dict[str, dict]:
    diagnostics: dict[str, dict] = {}
    for field in ("expected_move_bps", "scalper_probability", "confidence"):
        samples = [
            (value, _float(row, "net_bps"))
            for row in rows
            if (value := _optional_float(row, field)) is not None
        ]
        values = [value for value, _ in samples]
        net = [realized for _, realized in samples]
        wins = [float(realized > 0) for realized in net]
        diagnostics[field] = {
            "observations": len(samples),
            "correlation_with_net_bps": _pearson(values, net),
            "correlation_with_win": _pearson(values, wins),
            "higher_score_improves_outcomes": (
                _pearson(values, net) is not None
                and float(_pearson(values, net) or 0.0) > 0
            ),
        }
        if field == "scalper_probability" and samples:
            diagnostics[field].update(
                {
                    "average_predicted_probability": fmean(values),
                    "empirical_hit_rate": fmean(wins),
                    "brier_score": fmean(
                        (probability - won) ** 2
                        for probability, won in zip(values, wins)
                    ),
                }
            )
    return diagnostics


def _dimension(
    rows: list[dict],
    fields: tuple[str, ...],
    *,
    key_name: str,
) -> list[dict]:
    groups: dict[tuple[str, ...], list[dict]] = defaultdict(list)
    for row in rows:
        groups[tuple(str(row.get(field) or "unknown") for field in fields)].append(row)
    output = []
    for values, members in groups.items():
        labels = dict(zip(fields, values))
        output.append(
            {
                "key": " | ".join(values),
                "dimension": key_name,
                **labels,
                **summarize(members),
            }
        )
    return sorted(output, key=lambda item: (-int(item["trades"]), str(item["key"])))


DIMENSIONS: dict[str, tuple[str, ...]] = {
    "scanner": ("scanner_id",),
    "regime": ("regime",),
    "symbol": ("symbol",),
    "side": ("side",),
    "entry_hour_utc": ("entry_hour_utc",),
    "entry_hour_ist": ("entry_hour_ist",),
    "exit_reason": ("exit_reason",),
    "hold_bucket": ("hold_bucket",),
    "move_size_bucket": ("move_size_bucket",),
    "probability_bucket": ("probability_bucket",),
    "confidence_bucket": ("confidence_bucket",),
    "l2_quality": ("l2_quality",),
    "scanner_symbol": ("scanner_id", "symbol"),
    "scanner_regime": ("scanner_id", "regime"),
    "scanner_move_size": ("scanner_id", "move_size_bucket"),
    "scanner_probability": ("scanner_id", "probability_bucket"),
    "scanner_confidence": ("scanner_id", "confidence_bucket"),
    "symbol_regime": ("symbol", "regime"),
    "symbol_hour_ist": ("symbol", "entry_hour_ist"),
}


def dimension_tables(rows: list[dict]) -> dict[str, list[dict]]:
    return {
        name: _dimension(rows, fields, key_name=name)
        for name, fields in DIMENSIONS.items()
    }


def _loss_clusters(tables: dict[str, list[dict]], minimum_trades: int) -> list[dict]:
    allowed = {
        "scanner_symbol",
        "scanner_regime",
        "scanner_move_size",
        "scanner_probability",
        "scanner_confidence",
        "symbol_regime",
        "symbol_hour_ist",
    }
    candidates = [
        row
        for name, rows in tables.items()
        if name in allowed
        for row in rows
        if int(row["trades"]) >= minimum_trades
    ]
    return sorted(
        candidates,
        key=lambda row: (float(row["net_bps"]), float(row["average_net_bps"])),
    )[:20]


def _false_signal_clusters(
    tables: dict[str, list[dict]], minimum_trades: int
) -> list[dict]:
    allowed = {
        "scanner_symbol",
        "scanner_regime",
        "scanner_move_size",
        "scanner_probability",
        "scanner_confidence",
        "symbol_regime",
        "symbol_hour_ist",
    }
    candidates = [
        row
        for name, rows in tables.items()
        if name in allowed
        for row in rows
        if int(row["trades"]) >= minimum_trades
    ]
    return sorted(
        candidates,
        key=lambda row: (-float(row["false_signal_rate"]), -int(row["trades"])),
    )[:20]


def _flatten_historical(backtest: dict) -> list[dict]:
    return sorted(
        [
            normalize_trade(trade, source="historical_backtest")
            for market in (backtest.get("markets") or {}).values()
            if isinstance(market, dict)
            for trade in (market.get("trades") or [])
            if isinstance(trade, dict)
        ],
        key=lambda row: str(row.get("exit_ts") or ""),
    )


def load_forward_outcomes(path: Path) -> list[dict]:
    outcomes: list[dict] = []
    if not path.exists():
        return outcomes
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("kind") != "delta_scalper_shadow_outcome":
            continue
        payload = record.get("payload")
        if isinstance(payload, dict):
            outcomes.append(normalize_trade(payload, source="live_shadow"))
    return sorted(outcomes, key=lambda row: str(row.get("exit_ts") or ""))


def _automated_findings(tables: dict[str, list[dict]], summary: dict) -> list[dict]:
    findings: list[dict] = []
    symbols = tables.get("symbol", [])
    if symbols:
        worst = min(symbols, key=lambda row: float(row["net_bps"]))
        findings.append(
            {
                "kind": "largest_symbol_loss",
                "key": worst["key"],
                "trades": worst["trades"],
                "net_bps": worst["net_bps"],
                "average_net_bps": worst["average_net_bps"],
            }
        )
    scanners = tables.get("scanner", [])
    if scanners:
        worst = min(scanners, key=lambda row: float(row["net_bps"]))
        findings.append(
            {
                "kind": "largest_scanner_loss",
                "key": worst["key"],
                "trades": worst["trades"],
                "net_bps": worst["net_bps"],
                "false_signal_rate": worst["false_signal_rate"],
            }
        )
    findings.append(
        {
            "kind": "cost_and_excursion_baseline",
            "average_cost_bps": summary["average_cost_bps"],
            "average_mfe_bps": summary["average_mfe_bps"],
            "average_mae_bps": summary["average_mae_bps"],
            "expectation_error_bps": summary["expectation_error_bps"],
        }
    )
    return findings


def _attribution_section(rows: list[dict], *, minimum_cluster_trades: int) -> dict:
    tables = dimension_tables(rows)
    summary = summarize(rows)
    return {
        "summary": summary,
        "signal_quality_diagnostics": signal_quality_diagnostics(rows),
        "dimensions": tables,
        "largest_loss_clusters": _loss_clusters(tables, minimum_cluster_trades),
        "highest_false_signal_clusters": _false_signal_clusters(
            tables, minimum_cluster_trades
        ),
        "automated_findings": _automated_findings(tables, summary),
    }


def build_attribution_report(
    backtest: dict,
    live_outcomes: list[dict],
    *,
    minimum_cluster_trades: int = 50,
) -> dict:
    historical = _flatten_historical(backtest)
    fraction = float(
        (backtest.get("untouched_window") or {}).get("untouched_fraction") or 0.20
    )
    split = max(1, int(len(historical) * (1 - fraction))) if historical else 0
    selection = historical[:split]
    frozen = historical[split:]
    normalized_live = [
        row if row.get("source") == "live_shadow" else normalize_trade(row, source="live_shadow")
        for row in live_outcomes
    ]
    return {
        "report_id": "delta_scalper_attribution_v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "backtest_generated_at": backtest.get("generated_at"),
        "selection_window": {
            "start_exit_ts": selection[0].get("exit_ts") if selection else None,
            "end_exit_ts": selection[-1].get("exit_ts") if selection else None,
            "fraction": 1 - fraction,
            **_attribution_section(
                selection, minimum_cluster_trades=minimum_cluster_trades
            ),
        },
        "frozen_untouched_window": {
            "start_exit_ts": frozen[0].get("exit_ts") if frozen else None,
            "end_exit_ts": frozen[-1].get("exit_ts") if frozen else None,
            "fraction": fraction,
            "summary": summarize(frozen),
            "subgroup_attribution_performed": False,
            "protected_from_threshold_selection": True,
        },
        "live_shadow": {
            "status": "ready" if normalized_live else "insufficient_completed_outcomes",
            **_attribution_section(
                normalized_live, minimum_cluster_trades=max(1, minimum_cluster_trades)
            ),
        },
        "policy": {
            "research_only": True,
            "diagnostic_not_optimization": True,
            "thresholds_changed": False,
            "frozen_untouched_decomposed": False,
            "can_trade": False,
            "can_promote": False,
        },
        "can_trade": False,
        "can_promote": False,
    }


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def generate_report(
    *,
    backtest_path: Path = DEFAULT_BACKTEST,
    journal_path: Path = DEFAULT_JOURNAL,
    output_path: Path = DEFAULT_OUTPUT,
    minimum_cluster_trades: int = 50,
) -> dict:
    backtest = json.loads(backtest_path.read_text(encoding="utf-8"))
    report = build_attribution_report(
        backtest,
        load_forward_outcomes(journal_path),
        minimum_cluster_trades=minimum_cluster_trades,
    )
    report["sources"] = {
        "backtest": str(backtest_path),
        "live_journal": str(journal_path),
    }
    _atomic_json(output_path, report)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backtest", type=Path, default=DEFAULT_BACKTEST)
    parser.add_argument("--journal", type=Path, default=DEFAULT_JOURNAL)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--minimum-cluster-trades", type=int, default=50)
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.minimum_cluster_trades <= 0:
        raise ValueError("minimum cluster trades must be positive")
    report = generate_report(
        backtest_path=args.backtest,
        journal_path=args.journal,
        output_path=args.output,
        minimum_cluster_trades=args.minimum_cluster_trades,
    )
    print(json.dumps(report["selection_window"]["summary"], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
