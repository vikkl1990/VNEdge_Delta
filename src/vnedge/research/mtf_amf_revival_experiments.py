"""Preregistered selection-only swing successors to confirmed MTF rejection v2.

The frozen v2 report remains untouched.  This module publishes three distinct
research lines so frequency, direction permission, and exit protection are not
silently optimized as one strategy:

* v2.1 changes only the confirmation validity window from one to two hours;
* v3 adds a causal completed-4h permission filter to short setups;
* v3.1 adds fee-aware stop protection after a completed 15m close at +1R.

None of these scanners is registered for paper or live execution. All reports
stop before the sealed untouched window. V3/V3.1 are sparse multi-hour swing
hypotheses (up to 12 hours), never scalping-edge claims.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from vnedge.research.mtf_amf_confirmed_rejection_v2 import (
    DEFAULT_CONFIG,
    DEFAULT_SELECTION_END,
    DEFAULT_SYMBOLS,
    DEFAULT_UNTOUCHED_START,
    ConfirmedRejectionV2Config,
    Side,
    build_selection_report,
    publish_report,
)
from vnedge.research.mtf_amf_rejection_scanner import fetch_delta_public_candles
from vnedge.strategy.indicators import ema

V21_SCANNER_ID = "mtf_amf_confirmed_rejection_v2_1"
V3_SCANNER_ID = "mtf_amf_directional_rejection_v3"
V31_SCANNER_ID = "mtf_amf_directional_rejection_protected_v3_1"

V21_CONFIG = replace(DEFAULT_CONFIG, confirmation_window_bars=8)
DEFAULT_OUTPUT = Path("research/live_research/mtf_amf_revival_matrix_latest.json")


def _mark_swing_hypothesis(report: dict[str, Any]) -> dict[str, Any]:
    marked = dict(report)
    contract = dict(marked.get("contract") or {})
    contract.update(
        {
            "trade_horizon": "swing",
            "maximum_hold_seconds": 43_200,
            "edge_claim": "unproven_sparse_swing_hypothesis_not_scalping_edge",
        }
    )
    policy = dict(marked.get("policy") or {})
    policy.update(
        {
            "observation_collection_allowed": True,
            "paper_route": "absent",
            "order_route": "absent",
            "can_trade": False,
            "can_promote": False,
        }
    )
    marked["contract"] = contract
    marked["policy"] = policy
    marked["can_trade"] = False
    marked["can_promote"] = False
    return marked


def _directional_feature_enricher(
    _one_hour: pd.DataFrame,
    four_hour: pd.DataFrame,
    feature_frame: pd.DataFrame,
) -> pd.DataFrame:
    """Attach causal completed-4h trend and confirmed-swing features.

    A 4h candle stamped at ``t`` is made available at ``t + 4h``.  A swing
    high uses two left and two right bars, so the candidate two bars back is
    only marked after the current 4h candle has completed.
    """

    four = four_hour.copy()
    four["timestamp"] = pd.to_datetime(four["timestamp"], utc=True).astype(
        "datetime64[ns, UTC]"
    )
    four = four.drop_duplicates("timestamp", keep="last").sort_values("timestamp")
    close = pd.to_numeric(four["close"], errors="coerce")
    high = pd.to_numeric(four["high"], errors="coerce")
    four["direction_ema20"] = ema(close, 20)
    four["direction_ema50"] = ema(close, 50)
    four["direction_ema20_slope_3"] = (
        four["direction_ema20"] - four["direction_ema20"].shift(3)
    )

    candidate = high.shift(2)
    confirmed = candidate.eq(high.rolling(5, min_periods=5).max())
    pivot = candidate.where(confirmed)
    last_pivot = pivot.ffill()
    previous_at_event = last_pivot.shift(1).where(pivot.notna())
    previous_pivot = previous_at_event.ffill()
    four["direction_last_swing_high"] = last_pivot
    four["direction_previous_swing_high"] = previous_pivot
    four["direction_lower_high"] = last_pivot < previous_pivot
    four["direction_bearish"] = (
        (four["direction_ema20"] < four["direction_ema50"])
        & (four["direction_ema20_slope_3"] < 0)
        & four["direction_lower_high"]
    )
    four["available_at"] = four["timestamp"] + pd.Timedelta(hours=4)

    columns = [
        "available_at",
        "direction_ema20",
        "direction_ema50",
        "direction_ema20_slope_3",
        "direction_last_swing_high",
        "direction_previous_swing_high",
        "direction_lower_high",
        "direction_bearish",
    ]
    context = four[columns].dropna(subset=["direction_ema50"]).sort_values("available_at")
    left = feature_frame.copy()
    left["timestamp"] = pd.to_datetime(left["timestamp"], utc=True).astype(
        "datetime64[ns, UTC]"
    )
    enriched = pd.merge_asof(
        left.sort_values("timestamp"),
        context,
        left_on="timestamp",
        right_on="available_at",
        direction="backward",
        allow_exact_matches=True,
    )
    return enriched.reset_index(drop=True)


def _direction_permission(row: pd.Series, side: Side) -> bool:
    """Keep longs unchanged; require completed bearish structure for shorts."""

    if side == "long":
        return True
    required = (
        "direction_bearish",
        "direction_ema20",
        "direction_last_swing_high",
        "upper_level",
    )
    if any(pd.isna(row.get(name)) for name in required):
        return False
    return bool(row["direction_bearish"]) and (
        float(row["close"]) < float(row["direction_ema20"])
        and float(row["upper_level"]) <= float(row["direction_last_swing_high"])
    )


def build_revival_matrix(
    candles_by_symbol: dict[str, tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]],
    *,
    selection_end_exclusive: datetime = DEFAULT_SELECTION_END,
    untouched_start: datetime = DEFAULT_UNTOUCHED_START,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    generated = generated_at or datetime.now(UTC)
    common = {
        "candles_by_symbol": candles_by_symbol,
        "selection_end_exclusive": selection_end_exclusive,
        "untouched_start": untouched_start,
        "config": V21_CONFIG,
        "generated_at": generated,
    }
    v21 = build_selection_report(
        **common,
        scanner_id=V21_SCANNER_ID,
        schema_version="vnedge.mtf_amf_confirmed_rejection.v2_1",
        experiment_notes={
            "change_from_v2": "confirmation window only: 60m to 120m",
            "frozen_v2_modified": False,
        },
    )
    v3 = _mark_swing_hypothesis(build_selection_report(
        **common,
        scanner_id=V3_SCANNER_ID,
        schema_version="vnedge.mtf_amf_directional_rejection.v3",
        feature_enricher=_directional_feature_enricher,
        setup_permission=_direction_permission,
        experiment_notes={
            "change_from_v2_1": (
                "shorts require completed 4h EMA20<EMA50, negative 3-bar EMA20 "
                "slope, confirmed lower swing high, and rejection below EMA20"
            ),
            "long_permission": "unchanged",
        },
    ))
    v31 = _mark_swing_hypothesis(build_selection_report(
        **common,
        scanner_id=V31_SCANNER_ID,
        schema_version="vnedge.mtf_amf_directional_rejection_protected.v3_1",
        feature_enricher=_directional_feature_enricher,
        setup_permission=_direction_permission,
        protection_trigger_r=1.0,
        protection_lock_bps=V21_CONFIG.round_trip_cost_bps,
        experiment_notes={
            "change_from_v3": (
                "after a completed 15m close reaches +1R, later bars use a "
                "tightened stop at entry plus modeled round-trip cost"
            ),
            "arming_candle_can_trigger_protected_stop": False,
        },
    ))
    return {
        "schema_version": "vnedge.mtf_amf_revival_matrix.v1",
        "generated_at": generated.isoformat(),
        "selection_window": {
            "end_exclusive": selection_end_exclusive.isoformat(),
            "untouched_start": untouched_start.isoformat(),
        },
        "experiments": {V21_SCANNER_ID: v21, V3_SCANNER_ID: v3, V31_SCANNER_ID: v31},
        "policy": {
            "research_only": True,
            "untouched_status": "sealed",
            "registered_strategies": [],
            "paper_route": "absent",
            "order_route": "absent",
            "can_trade": False,
            "can_promote": False,
        },
        "can_trade": False,
        "can_promote": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    parser.add_argument("--days", type=int, default=900)
    parser.add_argument("--selection-end", default=DEFAULT_SELECTION_END.isoformat())
    parser.add_argument("--untouched-start", default=DEFAULT_UNTOUCHED_START.isoformat())
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    selection_end = datetime.fromisoformat(args.selection_end).astimezone(UTC)
    untouched_start = datetime.fromisoformat(args.untouched_start).astimezone(UTC)
    symbols = tuple(s.strip().upper() for s in args.symbols.split(",") if s.strip())
    frames = {
        symbol: tuple(
            fetch_delta_public_candles(symbol, timeframe, days=args.days, now=selection_end)
            for timeframe in ("1h", "4h", "15m")
        )
        for symbol in symbols
    }
    payload = build_revival_matrix(
        frames,
        selection_end_exclusive=selection_end,
        untouched_start=untouched_start,
    )
    publish_report(payload, args.out)
    summary = {
        name: {
            "trades": row["selection"]["metrics"]["trades"],
            "avg_net_bps": row["selection"]["metrics"]["average_net_bps"],
            "profit_factor": row["selection"]["metrics"]["profit_factor"],
            "selection_pass": row["selection"]["gate"]["passed"],
        }
        for name, row in payload["experiments"].items()
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
