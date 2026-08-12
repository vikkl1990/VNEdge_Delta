"""Read-only economic attribution for event-time absorption observations.

This report answers one narrow question: does any sufficiently populated,
pre-existing feature cell show positive *gross* expectancy before Delta costs?
It never changes detector thresholds, implements a scanner, or grants authority.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any


def build_event_edge_attribution(
    journal_path: Path | str,
    *,
    output_path: Path | str = Path(
        "research/live_research/delta_event_edge_attribution_latest.json"
    ),
    minimum_cell_trades: int = 100,
) -> dict[str, object]:
    if minimum_cell_trades <= 0:
        raise ValueError("minimum_cell_trades must be positive")
    rows, duplicate_outcomes = _load_outcomes(Path(journal_path))
    completed = [row for row in rows if row["realized_exit_reason"] != "missed_entry"]
    enriched = [_enrich(row) for row in completed]
    dimensions: dict[str, Callable[[dict[str, Any]], str]] = {
        "symbol": lambda row: str(row["symbol"]),
        "side": lambda row: "long" if row["event"]["reversal_direction"] > 0 else "short",
        "stacked": lambda row: str(bool(row["was_stacked"])).lower(),
        "strength_quintile": lambda row: _quintile(float(row["event"]["strength"])),
        "volume_quintile": lambda row: _quintile(float(row["volume_percentile"])),
        "hour_utc": lambda row: str(row["decision_ts"])[11:13],
    }
    views = {
        name: _group(rows=enriched, key=key, minimum_cell_trades=minimum_cell_trades)
        for name, key in dimensions.items()
    }
    interactions = _interaction_cells(enriched, minimum_cell_trades=minimum_cell_trades)
    overall = _metrics(enriched)
    fixed_horizon_economics = _fixed_horizon_metrics(enriched)
    chronological = sorted(enriched, key=lambda row: str(row["decision_ts"]))
    split = len(chronological) // 2
    chronological_halves = {
        "first_half": _metrics(chronological[:split]),
        "second_half": _metrics(chronological[split:]),
    }
    positive_gross_cells = sorted(
        (row for row in interactions if row["average_gross_bps"] > 0),
        key=lambda row: row["average_gross_bps"],
        reverse=True,
    )
    positive_net_cells = sorted(
        (row for row in interactions if row["average_net_bps"] > 0),
        key=lambda row: row["average_net_bps"],
        reverse=True,
    )
    payload: dict[str, object] = {
        "schema_version": "vnedge.event_edge_attribution.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "source": {
            "journal_path": str(journal_path),
            "outcomes": len(rows),
            "completed": len(completed),
            "missed_entries": len(rows) - len(completed),
            "duplicate_outcomes_ignored": duplicate_outcomes,
            "first_decision_ts": chronological[0]["decision_ts"] if chronological else None,
            "last_decision_ts": chronological[-1]["decision_ts"] if chronological else None,
        },
        "economics": overall,
        "fixed_horizon_economics": fixed_horizon_economics,
        "chronological_halves": chronological_halves,
        "views": views,
        "interaction_cells": interactions,
        "diagnosis": {
            "minimum_cell_trades": minimum_cell_trades,
            "positive_gross_interaction_cells": len(positive_gross_cells),
            "positive_net_interaction_cells": len(positive_net_cells),
            "best_gross_cell": positive_gross_cells[0] if positive_gross_cells else None,
            "best_net_cell": positive_net_cells[0] if positive_net_cells else None,
            "standalone_edge_supported": bool(
                overall["average_net_bps"] > 0 and (overall["profit_factor_net"] or 0.0) > 1
            ),
            "verdict": (
                "POSITIVE_CELL_REQUIRES_FROZEN_CONFIRMATION"
                if positive_net_cells
                else "NO_AFTER_COST_EDGE"
            ),
            "note": (
                "Cells are diagnostic only and cannot be used to tune the current "
                "frozen failed-auction v1 contract. Any follow-up must be separately "
                "preregistered and confirmed on future data."
            ),
        },
        "research_only": True,
        "scanner_implementation_authorized": False,
        "selection_authorized": False,
        "can_trade": False,
        "can_promote": False,
        "order_route": "absent",
    }
    _atomic_json(Path(output_path), payload)
    return payload


def _load_outcomes(path: Path) -> tuple[list[dict[str, Any]], int]:
    outcomes: dict[str, dict[str, Any]] = {}
    duplicate_count = 0
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            try:
                envelope = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid journal JSON at line {line_number}") from exc
            if envelope.get("kind") != "delta_absorption_research_outcome":
                continue
            payload = envelope.get("payload")
            if isinstance(payload, dict):
                key = str(payload.get("key", ""))
                if not key:
                    raise ValueError(f"outcome missing exactly-once key at line {line_number}")
                previous = outcomes.get(key)
                if previous is not None:
                    if previous != payload:
                        raise ValueError(f"conflicting duplicate outcome key: {key}")
                    duplicate_count += 1
                    continue
                outcomes[key] = payload
    return list(outcomes.values()), duplicate_count


def _enrich(row: dict[str, Any]) -> dict[str, Any]:
    symbol = str(row["symbol"])
    tick_size = 0.5 if symbol == "BTCUSD" else 0.05 if symbol == "ETHUSD" else None
    if tick_size is None:
        raise ValueError(f"unsupported attribution symbol: {symbol}")
    entry = float(row["entry_price"])
    if entry <= 0:
        raise ValueError("completed observation entry_price must be positive")
    scale = tick_size / entry * 10_000.0
    return {
        **row,
        "gross_bps": float(row["realized_gross_ticks"]) * scale,
        "cost_bps": float(row["cost_ticks"]) * scale,
        "net_bps": float(row["realized_net_ticks"]) * scale,
        "mfe_bps": float(row["mfe_ticks"]) * scale,
        "mae_bps": float(row["mae_ticks"]) * scale,
    }


def _quintile(value: float) -> str:
    return f"q{min(5, max(1, math.ceil(value * 5)))}"


def _profit_factor(values: Iterable[float]) -> float | None:
    rows = list(values)
    gains = sum(value for value in rows if value > 0)
    losses = abs(sum(value for value in rows if value < 0))
    if losses:
        return gains / losses
    return None if gains else 0.0


def _metrics(rows: list[dict[str, Any]]) -> dict[str, object]:
    if not rows:
        return {
            "trades": 0,
            "average_gross_bps": 0.0,
            "average_cost_bps": 0.0,
            "average_net_bps": 0.0,
            "profit_factor_gross": 0.0,
            "profit_factor_net": 0.0,
            "target_1_exit_rate": 0.0,
            "path_target_1_touch_rate": 0.0,
            "stop_then_target_touch_rate": 0.0,
            "false_signal_rate": 0.0,
            "average_mfe_bps": 0.0,
            "average_mae_bps": 0.0,
            "gross_positive_rate": 0.0,
            "net_positive_rate": 0.0,
            "cost_clear_mfe_rate": 0.0,
            "cost_plus_3bps_clear_mfe_rate": 0.0,
            "two_x_cost_clear_mfe_rate": 0.0,
            "average_best_path_net_bps": 0.0,
            "average_positive_gross_bps": 0.0,
        }
    size = len(rows)
    positive_gross = [row["gross_bps"] for row in rows if row["gross_bps"] > 0]
    return {
        "trades": size,
        "average_gross_bps": sum(row["gross_bps"] for row in rows) / size,
        "average_cost_bps": sum(row["cost_bps"] for row in rows) / size,
        "average_net_bps": sum(row["net_bps"] for row in rows) / size,
        "profit_factor_gross": _profit_factor(row["gross_bps"] for row in rows),
        "profit_factor_net": _profit_factor(row["net_bps"] for row in rows),
        "target_1_exit_rate": sum(row["realized_exit_reason"] == "target_1" for row in rows) / size,
        "path_target_1_touch_rate": sum(row["hit_target_1"] for row in rows) / size,
        "stop_then_target_touch_rate": sum(
            bool(row["stopped_out"]) and bool(row["hit_target_1"]) for row in rows
        )
        / size,
        "false_signal_rate": sum(row["stopped_out"] for row in rows) / size,
        "average_mfe_bps": sum(row["mfe_bps"] for row in rows) / size,
        "average_mae_bps": sum(row["mae_bps"] for row in rows) / size,
        "gross_positive_rate": sum(row["gross_bps"] > 0 for row in rows) / size,
        "net_positive_rate": sum(row["net_bps"] > 0 for row in rows) / size,
        "cost_clear_mfe_rate": sum(row["mfe_bps"] >= row["cost_bps"] for row in rows) / size,
        "cost_plus_3bps_clear_mfe_rate": sum(
            row["mfe_bps"] >= row["cost_bps"] + 3.0 for row in rows
        )
        / size,
        "two_x_cost_clear_mfe_rate": sum(row["mfe_bps"] >= 2.0 * row["cost_bps"] for row in rows)
        / size,
        # This is a hindsight upper bound, not a tradable return. It answers
        # whether the post-event path moved far enough to justify a future,
        # separately preregistered entry/exit hypothesis.
        "average_best_path_net_bps": sum(row["mfe_bps"] - row["cost_bps"] for row in rows) / size,
        "average_positive_gross_bps": (
            sum(positive_gross) / len(positive_gross) if positive_gross else 0.0
        ),
    }


def _fixed_horizon_metrics(rows: list[dict[str, Any]]) -> dict[str, object]:
    grouped: dict[int, list[tuple[float, float]]] = defaultdict(list)
    for row in rows:
        values = row.get("horizon_returns_bps")
        if not isinstance(values, dict):
            continue
        for raw_horizon, raw_gross in values.items():
            try:
                horizon_ms = int(raw_horizon)
                gross_bps = float(raw_gross)
            except (TypeError, ValueError):
                continue
            grouped[horizon_ms].append((gross_bps, float(row["cost_bps"])))
    result: dict[str, object] = {}
    for horizon_ms, values in sorted(grouped.items()):
        gross = [row[0] for row in values]
        net = [row[0] - row[1] for row in values]
        result[str(horizon_ms)] = {
            "observations": len(values),
            "average_gross_bps": sum(gross) / len(gross),
            "average_net_bps": sum(net) / len(net),
            "gross_positive_rate": sum(value > 0 for value in gross) / len(gross),
            "net_positive_rate": sum(value > 0 for value in net) / len(net),
            "profit_factor_gross": _profit_factor(gross),
            "profit_factor_net": _profit_factor(net),
        }
    return result


def _group(
    *,
    rows: list[dict[str, Any]],
    key: Callable[[dict[str, Any]], str],
    minimum_cell_trades: int,
) -> list[dict[str, object]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[key(row)].append(row)
    result = [
        {"cell": cell, **_metrics(values)}
        for cell, values in grouped.items()
        if len(values) >= minimum_cell_trades
    ]
    return sorted(result, key=lambda row: row["average_gross_bps"], reverse=True)


def _interaction_cells(
    rows: list[dict[str, Any]], *, minimum_cell_trades: int
) -> list[dict[str, object]]:
    grouped: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = (
            str(row["symbol"]),
            "long" if row["event"]["reversal_direction"] > 0 else "short",
            str(bool(row["was_stacked"])).lower(),
            _quintile(float(row["event"]["strength"])),
            _quintile(float(row["volume_percentile"])),
        )
        grouped[key].append(row)
    result = []
    for (symbol, side, stacked, strength, volume), values in grouped.items():
        if len(values) < minimum_cell_trades:
            continue
        result.append(
            {
                "symbol": symbol,
                "side": side,
                "stacked": stacked == "true",
                "strength_quintile": strength,
                "volume_quintile": volume,
                **_metrics(values),
            }
        )
    return sorted(result, key=lambda row: row["average_gross_bps"], reverse=True)


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", type=Path, default=Path("logs/delta_event_research.jsonl"))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("research/live_research/delta_event_edge_attribution_latest.json"),
    )
    parser.add_argument("--minimum-cell-trades", type=int, default=100)
    args = parser.parse_args(argv)
    payload = build_event_edge_attribution(
        args.journal,
        output_path=args.output,
        minimum_cell_trades=args.minimum_cell_trades,
    )
    print(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
