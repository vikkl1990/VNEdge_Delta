"""Frozen selection study for the Willy-family pool x squeeze hypothesis.

The study deliberately separates one core liquidity-pool sweep from the full
Pool x Squeeze x Volume x Target-room interaction and three single-gate
ablations.  It is selection-only: the sealed tail is never loaded or scored,
exit optimization remains locked until matched controls pass, and AMF evidence
is never read or merged.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean
from tempfile import NamedTemporaryFile
from typing import Any

import pandas as pd
import yaml

from vnedge.research.delta_scalper_backtest import _load_candles
from vnedge.scalping.delta_engine.candle_store import ClosedCandleAggregator
from vnedge.scalping.delta_engine.types import Candle, Side

SCHEMA_VERSION = "vnedge.liquidity_pool_squeeze_study.v1"
STUDY_ID = "liquidity_pool_squeeze_cleanroom_v1"
DEFAULT_CONFIG = Path("configs/research/liquidity_pool_squeeze_v1.yaml")
DEFAULT_CACHE = Path("data/delta_scalper_cache")
DEFAULT_OUTPUT = Path("research/live_research/liquidity_pool_squeeze_v1_latest.json")


@dataclass
class ActivePool:
    pool_id: str
    side: str
    price: float
    touches: int
    touch_volume: float
    first_pivot_index: int
    last_pivot_index: int
    confirmed_at_index: int
    swept_at_index: int | None = None


@dataclass(frozen=True)
class SweepEvent:
    event_id: str
    symbol: str
    decision_index: int
    decision_ts: datetime
    side: Side
    pool_id: str
    pool_side: str
    pool_price: float
    pool_touches: int
    pool_age_bars: int
    sweep_extreme: float
    wick_ratio: float
    relative_volume: float
    squeeze_recent: bool
    bb_width_percentile: float | None
    atr_bps: float
    volatility_bucket: str
    session: str
    opposing_pool_price: float | None
    target_room_bps: float | None
    swept_pool_count: int


@dataclass(frozen=True)
class VariantRules:
    squeeze: bool
    volume: bool
    target_room: bool


@dataclass(frozen=True)
class SimulatedTrade:
    event_id: str
    symbol: str
    variant: str
    side: str
    decision_ts: str
    entry_ts: str
    exit_ts: str
    entry_price: float
    exit_price: float
    exit_reason: str
    hold_bars: int
    gross_bps: float
    cost_bps: float
    net_bps: float
    mfe_bps: float
    mae_bps: float
    capture_ratio: float
    stop_bps: float
    target_bps: float
    same_bar_ambiguous: bool


def load_config(path: Path | str = DEFAULT_CONFIG) -> dict[str, Any]:
    payload = yaml.safe_load(Path(path).read_text())
    if payload.get("contract_id") != STUDY_ID:
        raise ValueError("unexpected liquidity-pool study contract")
    if payload.get("research_only") is not True:
        raise ValueError("study must be research-only")
    data = payload["data"]
    selection_end = _parse_ts(data["selection_end_exclusive"])
    sealed_start = _parse_ts(data["sealed_tail_start"])
    if selection_end > sealed_start:
        raise ValueError("selection window overlaps the sealed tail")
    if payload["policy"].get("sealed_tail_auto_open") is not False:
        raise ValueError("sealed tail must never auto-open")
    if payload["policy"].get("amf_v3_evidence_mixing") != "forbidden":
        raise ValueError("AMF v3 evidence must remain separate")
    return payload


def aggregate_15m(candles: list[Candle]) -> list[Candle]:
    aggregator = ClosedCandleAggregator(("15m",))
    rows: list[Candle] = []
    previous: datetime | None = None
    for candle in sorted(candles, key=lambda row: row.ts):
        if previous is not None and (candle.ts - previous).total_seconds() != 60:
            aggregator = ClosedCandleAggregator(("15m",))
        previous = candle.ts
        rows.extend(aggregator.on_one_minute("REPLAY", candle))
    return rows


def build_feature_frame(candles: list[Candle], config: dict[str, Any]) -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            "timestamp": [row.ts for row in candles],
            "open": [row.open for row in candles],
            "high": [row.high for row in candles],
            "low": [row.low for row in candles],
            "close": [row.close for row in candles],
            "volume": [row.volume for row in candles],
        }
    )
    if frame.empty:
        return frame
    previous_close = frame["close"].shift(1)
    true_range = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    frame["atr"] = true_range.rolling(14, min_periods=14).mean()
    frame["atr_bps"] = frame["atr"] / frame["close"] * 10_000.0
    frame["atr_baseline"] = frame["atr"].shift(1).rolling(96, min_periods=48).median()
    atr_ratio = frame["atr"] / frame["atr_baseline"]
    frame["volatility_bucket"] = "medium"
    frame.loc[atr_ratio < 0.80, "volatility_bucket"] = "low"
    frame.loc[atr_ratio > 1.20, "volatility_bucket"] = "high"

    volume_cfg = config["volume"]
    volume_baseline = (
        frame["volume"]
        .shift(1)
        .rolling(int(volume_cfg["history_bars"]), min_periods=int(volume_cfg["history_bars"]))
        .median()
    )
    frame["relative_volume"] = frame["volume"] / volume_baseline

    squeeze = config["squeeze"]
    window = int(squeeze["bollinger_window_bars"])
    basis = frame["close"].rolling(window, min_periods=window).mean()
    width = 4.0 * frame["close"].rolling(window, min_periods=window).std(ddof=0) / basis
    history = int(squeeze["percentile_history_bars"])
    frame["bb_width"] = width
    frame["bb_width_percentile"] = width.rolling(history, min_periods=history).apply(
        lambda values: (
            (
                sum(value < values[-1] for value in values)
                + 0.5 * sum(value == values[-1] for value in values)
            )
            / len(values)
        ),
        raw=True,
    )
    compressed = frame["bb_width_percentile"] <= float(squeeze["maximum_width_percentile"])
    frame["squeeze_recent"] = (
        compressed.shift(1)
        .rolling(int(squeeze["recent_lookback_bars"]), min_periods=1)
        .max()
        .fillna(0)
        .astype(bool)
    )
    hours = pd.to_datetime(frame["timestamp"], utc=True).dt.hour
    frame["session"] = hours.map(_session)
    return frame


def detect_sweep_events(
    frame: pd.DataFrame,
    *,
    symbol: str,
    config: dict[str, Any],
) -> list[SweepEvent]:
    """Stream confirmed pivots and emit immutable, causal sweep events."""

    if frame.empty:
        return []
    pool_cfg = config["pool"]
    left = int(pool_cfg["pivot_left_bars"])
    right = int(pool_cfg["pivot_right_bars"])
    minimum_touches = int(pool_cfg["minimum_touches"])
    max_age = int(pool_cfg["maximum_age_bars"])
    pools: list[ActivePool] = []
    events: list[SweepEvent] = []
    highs = frame["high"].to_numpy(float)
    lows = frame["low"].to_numpy(float)
    volumes = frame["volume"].to_numpy(float)

    for index, row in frame.iterrows():
        pivot_index = index - right
        if pivot_index >= left:
            start = pivot_index - left
            stop = index + 1
            high_window = highs[start:stop]
            low_window = lows[start:stop]
            if (
                highs[pivot_index] == high_window.max()
                and sum(high_window == highs[pivot_index]) == 1
            ):
                _merge_pivot(
                    pools,
                    side="high",
                    price=float(highs[pivot_index]),
                    volume=float(volumes[pivot_index]),
                    pivot_index=pivot_index,
                    confirmed_at=index,
                    atr=float(row["atr"]),
                    tolerance_atr=float(pool_cfg["merge_tolerance_atr"]),
                    maximum_age_bars=max_age,
                    symbol=symbol,
                )
            if lows[pivot_index] == low_window.min() and sum(low_window == lows[pivot_index]) == 1:
                _merge_pivot(
                    pools,
                    side="low",
                    price=float(lows[pivot_index]),
                    volume=float(volumes[pivot_index]),
                    pivot_index=pivot_index,
                    confirmed_at=index,
                    atr=float(row["atr"]),
                    tolerance_atr=float(pool_cfg["merge_tolerance_atr"]),
                    maximum_age_bars=max_age,
                    symbol=symbol,
                )

        active = [
            pool
            for pool in pools
            if pool.swept_at_index is None
            and pool.touches >= minimum_touches
            and pool.confirmed_at_index <= index
            and index - pool.confirmed_at_index <= max_age
        ]
        swept = [pool for pool in active if _is_swept(pool, row, pool_cfg)]
        swept_high = [pool for pool in swept if pool.side == "high"]
        swept_low = [pool for pool in swept if pool.side == "low"]
        if bool(swept_high) == bool(swept_low):
            continue
        chosen_side = "high" if swept_high else "low"
        same_side = swept_high or swept_low
        chosen = max(same_side, key=lambda pool: (pool.touches, pool.touch_volume))
        side = Side.SHORT if chosen_side == "high" else Side.LONG
        opposing = _nearest_opposing(active, row, side)
        close = float(row["close"])
        target_room = (
            _directional_bps(close, float(opposing.price), side) if opposing is not None else None
        )
        span = max(float(row["high"] - row["low"]), close * 1e-12)
        wick = (
            float(row["high"] - max(row["open"], row["close"]))
            if side is Side.SHORT
            else float(min(row["open"], row["close"]) - row["low"])
        )
        for pool in same_side:
            pool.swept_at_index = index
        events.append(
            SweepEvent(
                event_id=f"{symbol}:{pd.Timestamp(row['timestamp']).isoformat()}:{side.value}",
                symbol=symbol,
                decision_index=int(index),
                decision_ts=_utc(row["timestamp"]),
                side=side,
                pool_id=chosen.pool_id,
                pool_side=chosen.side,
                pool_price=chosen.price,
                pool_touches=chosen.touches,
                pool_age_bars=index - chosen.confirmed_at_index,
                sweep_extreme=float(row["low"] if side is Side.LONG else row["high"]),
                wick_ratio=wick / span,
                relative_volume=float(row["relative_volume"]),
                squeeze_recent=bool(row["squeeze_recent"]),
                bb_width_percentile=_finite_or_none(row["bb_width_percentile"]),
                atr_bps=float(row["atr_bps"]),
                volatility_bucket=str(row["volatility_bucket"]),
                session=str(row["session"]),
                opposing_pool_price=opposing.price if opposing is not None else None,
                target_room_bps=target_room,
                swept_pool_count=len(same_side),
            )
        )
    return events


def variant_events(
    events: list[SweepEvent],
    config: dict[str, Any],
) -> dict[str, list[SweepEvent]]:
    output: dict[str, list[SweepEvent]] = {}
    min_volume = float(config["volume"]["minimum_relative_volume"])
    min_room = float(config["target_room"]["cost_multiple"]) * float(
        config["costs"]["total_roundtrip_bps"]
    )
    for name in config["variants"]["testing_order"]:
        raw = config["variants"][name]
        rules = VariantRules(**raw)
        output[name] = [
            event
            for event in events
            if (not rules.squeeze or event.squeeze_recent)
            and (not rules.volume or event.relative_volume >= min_volume)
            and (
                not rules.target_room
                or (event.target_room_bps is not None and event.target_room_bps >= min_room)
            )
        ]
    return output


def simulate_variant(
    frame: pd.DataFrame,
    events: list[SweepEvent],
    *,
    variant: str,
    config: dict[str, Any],
) -> tuple[list[SimulatedTrade], dict[str, int]]:
    exit_cfg = config["exit"]
    cost = float(config["costs"]["total_roundtrip_bps"])
    cost_floor = cost * float(config["target_room"]["cost_multiple"])
    reward_risk = float(config["target_room"]["fallback_reward_risk"])
    vertical = int(exit_cfg["vertical_barrier_bars"])
    cooldown = int(exit_cfg["cooldown_bars"])
    by_index = {event.decision_index: event for event in events}
    trades: list[SimulatedTrade] = []
    rejected = {"active_or_cooldown": 0, "entry_missing": 0, "invalid_geometry": 0}
    active_until = -1
    last_entry = -cooldown - 1
    for decision_index in sorted(by_index):
        event = by_index[decision_index]
        entry_index = decision_index + 1
        if decision_index <= active_until or entry_index - last_entry < cooldown:
            rejected["active_or_cooldown"] += 1
            continue
        if entry_index >= len(frame):
            rejected["entry_missing"] += 1
            continue
        entry = float(frame.iloc[entry_index]["open"])
        stop_buffer = float(exit_cfg["stop_buffer_bps"])
        stop = (
            event.sweep_extreme * (1.0 - stop_buffer / 10_000.0)
            if event.side is Side.LONG
            else event.sweep_extreme * (1.0 + stop_buffer / 10_000.0)
        )
        stop_bps = -_directional_bps(entry, stop, event.side)
        if stop_bps <= 0 or stop_bps > float(exit_cfg["maximum_stop_bps"]):
            rejected["invalid_geometry"] += 1
            continue
        if event.opposing_pool_price is not None and event.target_room_bps is not None:
            structural_target = float(event.opposing_pool_price)
        else:
            fallback_bps = max(cost_floor, reward_risk * stop_bps)
            structural_target = _move_price(entry, event.side, fallback_bps)
        target_bps = _directional_bps(entry, structural_target, event.side)
        if target_bps <= 0:
            rejected["invalid_geometry"] += 1
            continue
        trade, exit_index = _resolve_trade(
            frame,
            event,
            variant=variant,
            entry_index=entry_index,
            entry=entry,
            stop=stop,
            target=structural_target,
            stop_bps=stop_bps,
            target_bps=target_bps,
            vertical=vertical,
            cost=cost,
        )
        trades.append(trade)
        active_until = exit_index
        last_entry = entry_index
    return trades, rejected


def matched_control_report(
    frame: pd.DataFrame,
    events: list[SweepEvent],
    *,
    config: dict[str, Any],
    excluded_event_indices: set[int] | None = None,
) -> dict[str, Any]:
    controls = config["controls"]
    horizon = int(controls["horizon_bars"])
    lookback = int(controls["maximum_lookback_bars"])
    event_indices = set(excluded_event_indices or ()) | {event.decision_index for event in events}
    used_controls: set[int] = set()
    pairs: list[dict[str, Any]] = []
    for event in sorted(events, key=lambda row: row.decision_index):
        latest = event.decision_index - horizon - 1
        earliest = max(0, event.decision_index - lookback)
        control_index = None
        for candidate in range(latest, earliest - 1, -1):
            row = frame.iloc[candidate]
            if candidate in event_indices or candidate in used_controls:
                continue
            if str(row["session"]) != event.session:
                continue
            if str(row["volatility_bucket"]) != event.volatility_bucket:
                continue
            control_index = candidate
            break
        if control_index is None or event.decision_index + horizon >= len(frame):
            continue
        used_controls.add(control_index)
        event_path = _path_metrics(frame, event.decision_index, event.side, horizon)
        control_path = _path_metrics(frame, control_index, event.side, horizon)
        pairs.append(
            {
                "event_id": event.event_id,
                "event_index": event.decision_index,
                "control_index": control_index,
                "control_precedes_event": control_index + horizon < event.decision_index,
                "event_mfe_bps": event_path["mfe_bps"],
                "control_mfe_bps": control_path["mfe_bps"],
                "mfe_uplift_bps": event_path["mfe_bps"] - control_path["mfe_bps"],
                "event_signed_return_bps": event_path["signed_return_bps"],
                "control_signed_return_bps": control_path["signed_return_bps"],
            }
        )
    uplifts = [float(pair["mfe_uplift_bps"]) for pair in pairs]
    pair_win_rate = sum(value > 0 for value in uplifts) / len(uplifts) if uplifts else 0.0
    average_uplift = fmean(uplifts) if uplifts else 0.0
    min_pairs = int(controls["minimum_matched_pairs"])
    passed = (
        len(pairs) >= min_pairs
        and average_uplift > float(controls["minimum_average_mfe_uplift_bps"])
        and pair_win_rate > float(controls["minimum_pair_win_rate"])
        and all(pair["control_precedes_event"] for pair in pairs)
    )
    fee_wall = float(config["costs"]["total_roundtrip_bps"])
    return {
        "matched_pairs": len(pairs),
        "average_mfe_uplift_bps": average_uplift,
        "pair_win_rate": pair_win_rate,
        "event_fee_wall_break_rate": (
            sum(pair["event_mfe_bps"] >= fee_wall for pair in pairs) / len(pairs) if pairs else 0.0
        ),
        "control_fee_wall_break_rate": (
            sum(pair["control_mfe_bps"] >= fee_wall for pair in pairs) / len(pairs)
            if pairs
            else 0.0
        ),
        "all_controls_precede_events": all(pair["control_precedes_event"] for pair in pairs),
        "passed": passed,
        "requirements": {
            "minimum_matched_pairs": min_pairs,
            "minimum_average_mfe_uplift_bps": float(controls["minimum_average_mfe_uplift_bps"]),
            "minimum_pair_win_rate_exclusive": float(controls["minimum_pair_win_rate"]),
        },
        "pairs": pairs,
    }


def summarize_trades(trades: list[SimulatedTrade], config: dict[str, Any]) -> dict[str, Any]:
    nets = [trade.net_bps for trade in trades]
    grosses = [trade.gross_bps for trade in trades]
    gains = sum(max(0.0, value) for value in nets)
    losses = sum(max(0.0, -value) for value in nets)
    selection = config["selection"]
    average_gross = fmean(grosses) if grosses else 0.0
    average_net = fmean(nets) if nets else 0.0
    profit_factor = gains / losses if losses else (math.inf if gains else 0.0)
    gate = {
        "minimum_trades": len(trades) >= int(selection["minimum_trades"]),
        "average_gross": average_gross > float(selection["minimum_average_gross_bps"]),
        "average_net": average_net > float(selection["minimum_average_net_bps"]),
        "profit_factor": profit_factor >= float(selection["minimum_profit_factor"]),
    }
    return {
        "trades": len(trades),
        "average_gross_bps": average_gross,
        "average_net_bps": average_net,
        "total_net_bps": sum(nets),
        "profit_factor": profit_factor,
        "win_rate": sum(value > 0 for value in nets) / len(nets) if nets else 0.0,
        "average_mfe_bps": fmean([trade.mfe_bps for trade in trades]) if trades else 0.0,
        "average_mae_bps": fmean([trade.mae_bps for trade in trades]) if trades else 0.0,
        "average_capture_ratio": fmean([trade.capture_ratio for trade in trades])
        if trades
        else 0.0,
        "fee_wall_break_rate": (
            sum(trade.mfe_bps >= trade.cost_bps for trade in trades) / len(trades)
            if trades
            else 0.0
        ),
        "exit_reasons": dict(Counter(trade.exit_reason for trade in trades)),
        "by_symbol": _group_trade_metrics(trades, "symbol"),
        "by_side": _group_trade_metrics(trades, "side"),
        "gate_checks": gate,
        "passed": all(gate.values()),
    }


async def run_study(
    config_path: Path | str = DEFAULT_CONFIG,
    *,
    cache_dir: Path | str = DEFAULT_CACHE,
    refresh: bool = False,
    code_version: str = "unknown",
) -> dict[str, Any]:
    config = load_config(config_path)
    start = _parse_ts(config["data"]["start"])
    selection_end = _parse_ts(config["data"]["selection_end_exclusive"])
    sealed_start = _parse_ts(config["data"]["sealed_tail_start"])
    if selection_end > sealed_start:
        raise ValueError("sealed tail would be opened")
    combined: dict[str, dict[str, Any]] = {
        name: {"events": [], "trades": [], "rejections": {}}
        for name in config["variants"]["testing_order"]
    }
    source: dict[str, Any] = {}
    for symbol in config["data"]["symbols"]:
        one_minute = await _load_candles(
            symbol,
            start,
            selection_end,
            cache_dir=Path(cache_dir),
            refresh=refresh,
        )
        # The source API timestamps bars by their open. ``_load_candles``
        # converts them to close timestamps, so the close exactly at the
        # sealed-tail boundary must be excluded from the selection stream.
        one_minute = [row for row in one_minute if row.ts < selection_end]
        missing_minutes = _missing_minutes(one_minute)
        if config["data"].get("require_zero_missing_minutes") and missing_minutes:
            raise ValueError(f"{symbol} selection source has {missing_minutes} missing minutes")
        fifteen = aggregate_15m(one_minute)
        frame = build_feature_frame(fifteen, config)
        events = detect_sweep_events(frame, symbol=symbol, config=config)
        variants = variant_events(events, config)
        source[symbol] = {
            "one_minute_bars": len(one_minute),
            "decision_bars": len(frame),
            "raw_pool_sweeps": len(events),
            "missing_one_minute_bars": missing_minutes,
            "first_decision_ts": _iso(frame.iloc[0]["timestamp"]) if len(frame) else None,
            "last_decision_ts": _iso(frame.iloc[-1]["timestamp"]) if len(frame) else None,
        }
        for name, selected_events in variants.items():
            trades, rejected = simulate_variant(frame, selected_events, variant=name, config=config)
            controls = matched_control_report(
                frame,
                selected_events,
                config=config,
                excluded_event_indices={event.decision_index for event in events},
            )
            combined[name]["events"].extend(asdict(event) for event in selected_events)
            combined[name]["trades"].extend(asdict(trade) for trade in trades)
            combined[name].setdefault("controls_by_symbol", {})[symbol] = controls
            combined[name].setdefault("rejections_by_symbol", {})[symbol] = rejected

    variants_out: dict[str, Any] = {}
    for name in config["variants"]["testing_order"]:
        rows = combined[name]
        trades = [SimulatedTrade(**row) for row in rows["trades"]]
        metrics = summarize_trades(trades, config)
        control_reports = rows["controls_by_symbol"]
        matched_pairs = sum(report["matched_pairs"] for report in control_reports.values())
        uplift_sum = sum(
            report["average_mfe_uplift_bps"] * report["matched_pairs"]
            for report in control_reports.values()
        )
        wins = sum(
            report["pair_win_rate"] * report["matched_pairs"] for report in control_reports.values()
        )
        control_gate = {
            "matched_pairs": matched_pairs,
            "average_mfe_uplift_bps": uplift_sum / matched_pairs if matched_pairs else 0.0,
            "pair_win_rate": wins / matched_pairs if matched_pairs else 0.0,
            "all_controls_precede_events": all(
                report["all_controls_precede_events"] for report in control_reports.values()
            ),
        }
        requirements = config["controls"]
        control_gate["passed"] = (
            matched_pairs >= int(requirements["minimum_matched_pairs"])
            and control_gate["average_mfe_uplift_bps"]
            > float(requirements["minimum_average_mfe_uplift_bps"])
            and control_gate["pair_win_rate"] > float(requirements["minimum_pair_win_rate"])
            and control_gate["all_controls_precede_events"]
        )
        variants_out[name] = {
            "rules": config["variants"][name],
            "candidate_events": len(rows["events"]),
            "selection_metrics": metrics,
            "matched_controls": control_gate,
            "controls_by_symbol": control_reports,
            "rejections_by_symbol": rows["rejections_by_symbol"],
            "trades": rows["trades"],
        }

    full = variants_out["pool_squeeze_full"]
    selection_passed = bool(full["selection_metrics"]["passed"])
    controls_passed = bool(full["matched_controls"]["passed"])
    tail_eligible = selection_passed and controls_passed
    return {
        "schema_version": SCHEMA_VERSION,
        "study_id": STUDY_ID,
        "generated_at": datetime.now(UTC).isoformat(),
        "code_version": code_version,
        "contract": config,
        "selection_window": {
            "start": start.isoformat(),
            "end_exclusive": selection_end.isoformat(),
        },
        "sealed_tail": {
            "starts_at": sealed_start.isoformat(),
            "status": "SEALED",
            "loaded": False,
            "scored": False,
            "eligible_to_open_once": tail_eligible,
            "opened": False,
        },
        "testing_order": list(config["variants"]["testing_order"]),
        "source": source,
        "variants": variants_out,
        "exit_optimization": {
            "status": "ELIGIBLE_NOT_RUN" if controls_passed else "LOCKED_BY_MATCHED_CONTROLS",
            "run": False,
            "reason": (
                "matched-control gate passed; a separately preregistered exit study is required"
                if controls_passed
                else "full interaction did not demonstrate positive matched-control uplift"
            ),
        },
        "amf_v3": {
            "evidence_consumed": False,
            "observations_added": 0,
            "required_observations": 60,
            "separate_hypothesis": True,
        },
        "verdict": (
            "SELECTION_AND_CONTROLS_PASS_TAIL_REQUIRES_EXPLICIT_ONE_TIME_RUN"
            if tail_eligible
            else "REJECTED_OR_INSUFFICIENT_ON_SELECTION_TAIL_REMAINS_SEALED"
        ),
        "research_only": True,
        "can_trade": False,
        "can_promote": False,
    }


def _merge_pivot(
    pools: list[ActivePool],
    *,
    side: str,
    price: float,
    volume: float,
    pivot_index: int,
    confirmed_at: int,
    atr: float,
    tolerance_atr: float,
    maximum_age_bars: int,
    symbol: str,
) -> None:
    tolerance = max(abs(atr) * tolerance_atr if math.isfinite(atr) else 0.0, price * 1e-8)
    candidates = [
        pool
        for pool in pools
        if pool.side == side
        and pool.swept_at_index is None
        and confirmed_at - pool.confirmed_at_index <= maximum_age_bars
        and abs(pool.price - price) <= tolerance
    ]
    if candidates:
        pool = min(candidates, key=lambda item: abs(item.price - price))
        total_volume = pool.touch_volume + max(0.0, volume)
        if total_volume > 0:
            pool.price = (pool.price * pool.touch_volume + price * max(0.0, volume)) / total_volume
        else:
            pool.price = (pool.price * pool.touches + price) / (pool.touches + 1)
        pool.touches += 1
        pool.touch_volume = total_volume
        pool.last_pivot_index = pivot_index
        pool.confirmed_at_index = confirmed_at
        return
    pools.append(
        ActivePool(
            pool_id=f"{symbol}:{side}:{pivot_index}:{confirmed_at}",
            side=side,
            price=price,
            touches=1,
            touch_volume=max(0.0, volume),
            first_pivot_index=pivot_index,
            last_pivot_index=pivot_index,
            confirmed_at_index=confirmed_at,
        )
    )


def _is_swept(pool: ActivePool, row: pd.Series, config: dict[str, Any]) -> bool:
    price = pool.price
    minimum = float(config["minimum_sweep_bps"])
    span = max(float(row["high"] - row["low"]), float(row["close"]) * 1e-12)
    if pool.side == "high":
        wick = float(row["high"] - max(row["open"], row["close"])) / span
        return (
            float(row["high"]) >= price * (1.0 + minimum / 10_000.0)
            and float(row["close"]) < price
            and wick >= float(config["minimum_rejection_wick_ratio"])
        )
    wick = float(min(row["open"], row["close"]) - row["low"]) / span
    return (
        float(row["low"]) <= price * (1.0 - minimum / 10_000.0)
        and float(row["close"]) > price
        and wick >= float(config["minimum_rejection_wick_ratio"])
    )


def _nearest_opposing(pools: list[ActivePool], row: pd.Series, side: Side) -> ActivePool | None:
    close = float(row["close"])
    if side is Side.LONG:
        candidates = [pool for pool in pools if pool.side == "high" and pool.price > close]
        return min(candidates, key=lambda pool: pool.price) if candidates else None
    candidates = [pool for pool in pools if pool.side == "low" and pool.price < close]
    return max(candidates, key=lambda pool: pool.price) if candidates else None


def _resolve_trade(
    frame: pd.DataFrame,
    event: SweepEvent,
    *,
    variant: str,
    entry_index: int,
    entry: float,
    stop: float,
    target: float,
    stop_bps: float,
    target_bps: float,
    vertical: int,
    cost: float,
) -> tuple[SimulatedTrade, int]:
    mfe = 0.0
    mae = 0.0
    exit_index = min(len(frame) - 1, entry_index + vertical - 1)
    exit_price = float(frame.iloc[exit_index]["close"])
    reason = "time_stop"
    ambiguous = False
    for index in range(entry_index, exit_index + 1):
        row = frame.iloc[index]
        if event.side is Side.LONG:
            favorable = (float(row["high"]) / entry - 1.0) * 10_000.0
            adverse = (1.0 - float(row["low"]) / entry) * 10_000.0
            stop_hit = float(row["low"]) <= stop
            target_hit = float(row["high"]) >= target
        else:
            favorable = (entry / float(row["low"]) - 1.0) * 10_000.0
            adverse = (float(row["high"]) / entry - 1.0) * 10_000.0
            stop_hit = float(row["high"]) >= stop
            target_hit = float(row["low"]) <= target
        mae = max(mae, adverse, 0.0)
        # The path inside an OHLC bar is unknown. If the stop was touched,
        # conservatively do not credit favorable movement from that bar; it
        # may have happened only after the stop. This also prevents fee-wall
        # break rates from being inflated by post-stop highs/lows.
        if not stop_hit:
            mfe = max(mfe, favorable, 0.0)
        if stop_hit:
            exit_price, reason, exit_index = stop, "stop", index
            ambiguous = target_hit
            break
        if target_hit:
            exit_price, reason, exit_index = target, "target", index
            break
    gross = _directional_bps(entry, exit_price, event.side)
    net = gross - cost
    capture = max(0.0, gross) / mfe if mfe > 0 else 0.0
    row = frame.iloc[exit_index]
    return (
        SimulatedTrade(
            event_id=event.event_id,
            symbol=event.symbol,
            variant=variant,
            side=event.side.value,
            decision_ts=event.decision_ts.isoformat(),
            entry_ts=_iso(frame.iloc[entry_index]["timestamp"]),
            exit_ts=_iso(row["timestamp"]),
            entry_price=entry,
            exit_price=exit_price,
            exit_reason=reason,
            hold_bars=exit_index - entry_index + 1,
            gross_bps=gross,
            cost_bps=cost,
            net_bps=net,
            mfe_bps=mfe,
            mae_bps=mae,
            capture_ratio=capture,
            stop_bps=stop_bps,
            target_bps=target_bps,
            same_bar_ambiguous=ambiguous,
        ),
        exit_index,
    )


def _path_metrics(
    frame: pd.DataFrame, decision_index: int, side: Side, horizon: int
) -> dict[str, float]:
    entry_index = decision_index + 1
    end = decision_index + horizon
    entry = float(frame.iloc[entry_index]["open"])
    rows = frame.iloc[entry_index : end + 1]
    if side is Side.LONG:
        mfe = (float(rows["high"].max()) / entry - 1.0) * 10_000.0
    else:
        mfe = (entry / float(rows["low"].min()) - 1.0) * 10_000.0
    signed_return = _directional_bps(entry, float(rows.iloc[-1]["close"]), side)
    return {"mfe_bps": max(0.0, mfe), "signed_return_bps": signed_return}


def _group_trade_metrics(
    trades: list[SimulatedTrade], field: str
) -> dict[str, dict[str, float | int]]:
    groups: dict[str, list[SimulatedTrade]] = {}
    for trade in trades:
        groups.setdefault(str(getattr(trade, field)), []).append(trade)
    output: dict[str, dict[str, float | int]] = {}
    for name, rows in sorted(groups.items()):
        gains = sum(max(0.0, row.net_bps) for row in rows)
        losses = sum(max(0.0, -row.net_bps) for row in rows)
        output[name] = {
            "trades": len(rows),
            "average_gross_bps": fmean(row.gross_bps for row in rows),
            "average_net_bps": fmean(row.net_bps for row in rows),
            "profit_factor": gains / losses if losses else (math.inf if gains else 0.0),
        }
    return output


def _directional_bps(start: float, end: float, side: Side) -> float:
    if side is Side.LONG:
        return (end / start - 1.0) * 10_000.0
    return (start / end - 1.0) * 10_000.0


def _move_price(price: float, side: Side, bps: float) -> float:
    return price * (1.0 + (1.0 if side is Side.LONG else -1.0) * bps / 10_000.0)


def _session(hour: int) -> str:
    if 0 <= hour < 8:
        return "asia"
    if 8 <= hour < 13:
        return "europe"
    if 13 <= hour < 21:
        return "us"
    return "overnight"


def _missing_minutes(candles: list[Candle]) -> int:
    missing = 0
    previous: datetime | None = None
    for row in candles:
        if previous is not None:
            seconds = int((row.ts - previous).total_seconds())
            if seconds <= 0:
                raise ValueError("1m candles must be unique and ascending")
            if seconds != 60:
                missing += max(1, seconds // 60 - 1)
        previous = row.ts
    return missing


def _finite_or_none(value: Any) -> float | None:
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _parse_ts(value: str | datetime) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    return _utc(parsed)


def _utc(value: Any) -> datetime:
    parsed = pd.Timestamp(value).to_pydatetime()
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _iso(value: Any) -> str:
    return _utc(value).isoformat()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="pool x squeeze selection study")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--code-version", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    payload = asyncio.run(
        run_study(
            args.config,
            cache_dir=args.cache_dir,
            refresh=args.refresh,
            code_version=args.code_version,
        )
    )
    _atomic_json(Path(args.output), payload)
    print(Path(args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
