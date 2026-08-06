"""Offline PELT diagnostics plus causal CUSUM attribution for Delta scalping.

PELT deliberately remains a future-aware research overlay. It never enters the
live context or scanner gates. The live/replay context uses the independent
sequential CUSUM implementation in ``delta_engine.change_point``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from tempfile import NamedTemporaryFile

import numpy as np

from vnedge.research.delta_scalper_attribution import normalize_trade, summarize
from vnedge.research.delta_scalper_backtest import _load_candles, _parse_date
from vnedge.scalping.delta_engine.types import Candle

DEFAULT_BACKTEST = Path("research/live_research/delta_scalper_backtest_latest.json")
DEFAULT_OUTPUT = Path("research/live_research/delta_scalper_change_points_latest.json")


@dataclass(frozen=True)
class PeltConfig:
    minimum_segment_bars: int = 60
    penalty_multiplier: float = 12.0
    candidate_jump_bars: int = 15
    merge_tolerance_minutes: int = 5

    def __post_init__(self) -> None:
        if self.minimum_segment_bars < 10:
            raise ValueError("PELT minimum segment must be at least 10 bars")
        if self.penalty_multiplier <= 0:
            raise ValueError("PELT penalty multiplier must be positive")
        if self.candidate_jump_bars <= 0:
            raise ValueError("PELT candidate jump must be positive")


def _robust_standardize(values: np.ndarray) -> np.ndarray:
    median = float(np.median(values))
    mad = float(np.median(np.abs(values - median))) * 1.4826
    scale = mad if mad > 1e-12 else float(np.std(values))
    if scale <= 1e-12:
        return np.zeros_like(values, dtype=float)
    return (values - median) / scale


def pelt_mean_changes(
    values: list[float] | np.ndarray,
    *,
    minimum_segment_bars: int,
    penalty_multiplier: float,
    candidate_jump_bars: int = 1,
) -> list[int]:
    """Return exact-grid PELT break indices for a piecewise-constant mean.

    Segment cost is residual sum of squares after robust global scaling. The
    candidate grid may be coarsened for long 1m histories; observations remain
    1m and the chosen grid resolution is reported in the artifact.
    """
    raw = np.asarray(values, dtype=float)
    size = len(raw)
    if size < minimum_segment_bars * 2:
        return []
    data = _robust_standardize(raw)
    prefix = np.concatenate(([0.0], np.cumsum(data)))
    prefix_sq = np.concatenate(([0.0], np.cumsum(data * data)))
    penalty = penalty_multiplier * math.log(size)

    def cost(start: np.ndarray, end: int) -> np.ndarray:
        count = end - start
        total = prefix[end] - prefix[start]
        total_sq = prefix_sq[end] - prefix_sq[start]
        return np.maximum(0.0, total_sq - total * total / count)

    endpoints = list(range(minimum_segment_bars, size + 1, candidate_jump_bars))
    if endpoints[-1] != size:
        endpoints.append(size)
    objective: dict[int, float] = {0: -penalty}
    parent: dict[int, int] = {}
    admissible = [0]
    endpoint_set = set(endpoints)
    for end in endpoints:
        new_start = end - minimum_segment_bars
        if new_start in endpoint_set and new_start in objective:
            admissible.append(new_start)
        starts = np.asarray(
            [start for start in admissible if end - start >= minimum_segment_bars],
            dtype=int,
        )
        if not len(starts):
            continue
        segment_costs = cost(starts, end)
        totals = np.asarray([objective[int(start)] for start in starts])
        totals = totals + segment_costs + penalty
        best_index = int(np.argmin(totals))
        objective[end] = float(totals[best_index])
        parent[end] = int(starts[best_index])
        prune_limit = objective[end]
        admissible = [
            int(start)
            for start, value in zip(starts, totals - penalty)
            if float(value) <= prune_limit
        ]
    if size not in parent:
        return []
    changes: list[int] = []
    cursor = size
    while cursor in parent and parent[cursor] > 0:
        cursor = parent[cursor]
        changes.append(cursor)
    return sorted(changes)


def _series(candles: list[Candle]) -> tuple[list[datetime], list[float], list[float], int]:
    timestamps: list[datetime] = []
    returns: list[float] = []
    volatility: list[float] = []
    gaps = 0
    for previous, current in pairwise(candles):
        elapsed = (current.ts - previous.ts).total_seconds()
        contiguous = 0 < elapsed <= 90
        if not contiguous:
            gaps += max(1, round(elapsed / 60) - 1)
        prior_close = previous.close if contiguous else current.open
        timestamps.append(current.ts)
        returns.append(math.log(current.close / prior_close) * 10_000.0)
        true_range = max(
            current.high - current.low,
            abs(current.high - prior_close),
            abs(current.low - prior_close),
        )
        range_bps = true_range / current.close * 10_000.0
        volatility.append(math.log(max(range_bps, 1e-9)))
    return timestamps, returns, volatility, gaps


def detect_pelt_events(
    candles: list[Candle],
    config: PeltConfig,
) -> tuple[list[dict], dict]:
    timestamps, returns, volatility, gaps = _series(candles)
    return_indices = pelt_mean_changes(
        returns,
        minimum_segment_bars=config.minimum_segment_bars,
        penalty_multiplier=config.penalty_multiplier,
        candidate_jump_bars=config.candidate_jump_bars,
    )
    volatility_indices = pelt_mean_changes(
        volatility,
        minimum_segment_bars=config.minimum_segment_bars,
        penalty_multiplier=config.penalty_multiplier,
        candidate_jump_bars=config.candidate_jump_bars,
    )
    raw = sorted(
        [(timestamps[index], "return_mean") for index in return_indices]
        + [(timestamps[index], "realized_volatility") for index in volatility_indices]
    )
    merged: list[dict] = []
    tolerance = config.merge_tolerance_minutes * 60
    for ts, signal in raw:
        if merged and (ts - datetime.fromisoformat(merged[-1]["ts"])).total_seconds() <= tolerance:
            merged[-1]["signals"] = sorted({*merged[-1]["signals"], signal})
            continue
        merged.append({"ts": ts.isoformat(), "signals": [signal]})
    return merged, {
        "closed_1m_bars": len(candles),
        "missing_1m_bars": gaps,
        "return_change_points": len(return_indices),
        "volatility_change_points": len(volatility_indices),
        "merged_change_points": len(merged),
    }


def _window(minutes: float | None) -> str:
    if minutes is None:
        return "no_prior_change"
    if minutes <= 30:
        return "00-30m"
    if minutes <= 60:
        return "30-60m"
    if minutes <= 240:
        return "01-04h"
    return "04h+"


def _attach_pelt_window(trades: list[dict], events: dict[str, list[dict]]) -> list[dict]:
    event_times = {
        symbol: [datetime.fromisoformat(event["ts"]) for event in rows]
        for symbol, rows in events.items()
    }
    output: list[dict] = []
    for trade in trades:
        symbol = str(trade.get("symbol") or "").upper()
        decision = datetime.fromisoformat(str(trade["decision_ts"]))
        times = event_times.get(symbol, [])
        index = bisect_right(times, decision) - 1
        minutes = (
            (decision - times[index]).total_seconds() / 60.0 if index >= 0 else None
        )
        output.append({**trade, "offline_pelt_window": _window(minutes)})
    return output


def _dimension(rows: list[dict], field: str) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        groups[str(row.get(field) or "unknown")].append(row)
    return [
        {field: key, **summarize(members)}
        for key, members in sorted(groups.items())
    ]


def _regime_transition_alignment(
    trades: list[dict], events: dict[str, list[dict]]
) -> dict:
    distances: list[float] = []
    transitions = 0
    for symbol in sorted(events):
        members = sorted(
            (row for row in trades if row["symbol"] == symbol),
            key=lambda row: row["decision_ts"],
        )
        event_times = [datetime.fromisoformat(row["ts"]) for row in events[symbol]]
        previous = None
        for row in members:
            label = (row["trend_regime"], row["volatility_regime"])
            if previous is not None and label != previous and event_times:
                transitions += 1
                stamp = datetime.fromisoformat(row["decision_ts"])
                nearest = min(abs((stamp - event).total_seconds()) for event in event_times)
                distances.append(nearest / 60.0)
            previous = label
    return {
        "accepted_trade_label_transitions": transitions,
        "within_30m_of_pelt": sum(value <= 30 for value in distances),
        "within_60m_of_pelt": sum(value <= 60 for value in distances),
        "median_nearest_pelt_minutes": (
            float(np.median(distances)) if distances else None
        ),
        "limitation": "uses regime labels at accepted trades, not every market bar",
    }


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


async def run(args: argparse.Namespace) -> dict:
    backtest = json.loads(Path(args.backtest).read_text(encoding="utf-8"))
    start = _parse_date(args.start) if args.start else _parse_date(backtest["window"]["start"])
    end = _parse_date(args.end, end=True) if args.end else _parse_date(backtest["window"]["end"])
    symbols = tuple(backtest.get("markets", {}).keys())
    config = PeltConfig(
        minimum_segment_bars=args.minimum_segment_bars,
        penalty_multiplier=args.penalty_multiplier,
        candidate_jump_bars=args.candidate_jump_bars,
        merge_tolerance_minutes=args.merge_tolerance_minutes,
    )
    events: dict[str, list[dict]] = {}
    market_summaries: dict[str, dict] = {}
    for symbol in symbols:
        candles = await _load_candles(
            symbol,
            start,
            end,
            cache_dir=Path(args.cache_dir),
            refresh=False,
        )
        events[symbol], market_summaries[symbol] = detect_pelt_events(candles, config)
    historical = sorted(
        (
            normalize_trade(row, source="historical_backtest")
            for market in backtest.get("markets", {}).values()
            for row in market.get("trades", [])
        ),
        key=lambda row: row["exit_ts"],
    )
    untouched_fraction = float(
        (backtest.get("untouched_window") or {}).get("untouched_fraction") or 0.20
    )
    split = max(1, int(len(historical) * (1 - untouched_fraction))) if historical else 0
    selection = _attach_pelt_window(historical[:split], events)
    causal_cusum_ready = any(
        row.get("change_point_window") not in {None, "unavailable"}
        for row in historical
    )
    payload = {
        "report_id": "delta_scalper_change_points_v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "pelt_configuration": config.__dict__,
        "market_summaries": market_summaries,
        "offline_pelt_events": events,
        "selection_window": {
            "trades": len(selection),
            "post_pelt_expectancy": _dimension(selection, "offline_pelt_window"),
            "regime_transition_alignment": _regime_transition_alignment(
                selection, events
            ),
        },
        "causal_cusum": {
            "available_in_backtest": causal_cusum_ready,
            "selection_expectancy": (
                _dimension(historical[:split], "change_point_window")
                if causal_cusum_ready
                else []
            ),
        },
        "frozen_untouched_window": {
            "summary": summarize(historical[split:]),
            "subgroup_attribution_performed": False,
            "protected_from_threshold_selection": True,
        },
        "policy": {
            "research_only": True,
            "offline_pelt_is_future_aware": True,
            "offline_pelt_eligible_for_scanner_gate": False,
            "causal_cusum_used_for_signal": False,
            "causal_cusum_used_for_execution": False,
            "parameters_frozen_in_yaml": True,
            "can_trade": False,
            "can_promote": False,
        },
        "can_trade": False,
        "can_promote": False,
    }
    _atomic_json(Path(args.output), payload)
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backtest", default=str(DEFAULT_BACKTEST))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--cache-dir", default="data/delta_scalper_cache")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--minimum-segment-bars", type=int, default=60)
    parser.add_argument("--penalty-multiplier", type=float, default=12.0)
    parser.add_argument("--candidate-jump-bars", type=int, default=15)
    parser.add_argument("--merge-tolerance-minutes", type=int, default=5)
    return parser


def main() -> None:
    payload = asyncio.run(run(_parser().parse_args()))
    print(json.dumps({"markets": payload["market_summaries"]}, indent=2))


if __name__ == "__main__":
    main()
