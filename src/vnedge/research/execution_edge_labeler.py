"""Execution-aware forward labels for scanner and strategy events.

Most public indicator stacks answer a chart question: did price move after a
visual trigger?  VNEDGE needs the executable question instead: after the next
fill, realistic route cost, protective stop, and target, was there any net
edge left?  This module is the small truth layer for that question.

It is research-only.  It never submits orders, never promotes lanes, and never
turns an assumed maker touch into fill proof.  Candle labels are useful for
diagnosis and ranking; maker fill proof must still come from replay or live
shadow outcomes.
"""

from __future__ import annotations

import argparse
import inspect
import json
import math
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from statistics import mean
from typing import Iterable, Literal, Mapping

import pandas as pd

from vnedge.data.parquet_store import ParquetStore
from vnedge.scalping.parameter_registry import (
    DEFAULT_SCALPER_PARAMETER_REGISTRY,
    ExchangeFeeProfile,
)
from vnedge.strategy.base_strategy import BaseStrategy
from vnedge.strategy.strategy_registry import get_strategy_class

ExecutionRoute = Literal["MAKER_ONLY", "TAKER_ALLOWED"]
OutcomeKind = Literal["target", "stop", "horizon", "invalid"]
TruthVerdict = Literal[
    "NO_EVENTS",
    "UNDER_SAMPLED",
    "LOW_FILL_CONFIDENCE",
    "NEGATIVE_AFTER_COST",
    "MAKER_EDGE",
    "TAKER_EDGE",
]


@dataclass(frozen=True)
class EdgeLabelerConfig:
    """Conservative defaults for candle-level truth labels."""

    horizon_bars: int = 12
    min_samples: int = 10
    min_avg_net_bps: float = 0.5
    min_profit_factor: float = 1.15
    taker_min_avg_net_bps: float = 2.0
    taker_min_profit_factor: float = 1.30
    default_maker_fill_probability: float = 0.35
    min_maker_fill_probability: float = 0.50
    include_sizing_blockers: bool = True

    def __post_init__(self) -> None:
        if self.horizon_bars < 1:
            raise ValueError("horizon_bars must be >= 1")
        if self.min_samples < 1:
            raise ValueError("min_samples must be >= 1")
        for name, value in (
            ("default_maker_fill_probability", self.default_maker_fill_probability),
            ("min_maker_fill_probability", self.min_maker_fill_probability),
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be in [0, 1]")


@dataclass(frozen=True)
class SignalEvent:
    """One raw scanner/strategy entry candidate to be forward-labeled."""

    event_id: str
    ts: pd.Timestamp
    side: Literal["long", "short"]
    stop_price: float
    take_profit_price: float | None = None
    source_id: str = ""
    strategy_id: str = ""
    route: ExecutionRoute = "MAKER_ONLY"
    expected_edge_bps: float | None = None
    fill_probability: float | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.side not in {"long", "short"}:
            raise ValueError("side must be long or short")
        if self.route not in {"MAKER_ONLY", "TAKER_ALLOWED"}:
            raise ValueError("route must be MAKER_ONLY or TAKER_ALLOWED")
        if self.stop_price <= 0:
            raise ValueError("stop_price must be positive")
        if self.take_profit_price is not None and self.take_profit_price <= 0:
            raise ValueError("take_profit_price must be positive")


@dataclass(frozen=True)
class EventTruthLabel:
    """Forward outcome for one event."""

    event_id: str
    ts: str
    side: str
    route: ExecutionRoute
    source_id: str
    strategy_id: str
    entry_ts: str | None
    entry_price: float | None
    stop_price: float
    take_profit_price: float | None
    outcome: OutcomeKind
    exit_ts: str | None
    exit_price: float | None
    gross_bps: float | None
    route_cost_bps: float
    net_bps: float | None
    mfe_bps: float | None
    mae_bps: float | None
    mfe_after_cost_bps: float | None
    risk_bps: float | None
    target_bps: float | None
    hold_bars: int | None
    time_to_mfe_bars: int | None
    time_to_mae_bars: int | None
    capture_ratio: float | None
    max_r: float | None
    min_r: float | None
    exit_diagnosis: str
    fill_probability: float
    fill_evidence: str
    blockers: tuple[str, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)

    @property
    def executable(self) -> bool:
        return not self.blockers and self.net_bps is not None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class EdgeTruthSummary:
    """Aggregate executable truth for a scanner/strategy lane."""

    verdict: TruthVerdict
    samples: int
    executable_samples: int
    positive_net_samples: int
    avg_net_bps: float | None
    avg_gross_bps: float | None
    avg_mfe_bps: float | None
    avg_mae_bps: float | None
    avg_mfe_after_cost_bps: float | None
    avg_hold_bars: float | None
    avg_time_to_mfe_bars: float | None
    avg_capture_ratio: float | None
    fee_wall_break_rate_pct: float
    exit_diagnosis_counts: dict[str, int]
    profit_factor: float | None
    target_rate_pct: float
    stop_rate_pct: float
    maker_assumed_samples: int
    taker_samples: int
    avg_fill_probability: float | None
    primary_blocker: str
    can_trade: bool = False
    can_promote: bool = False
    requires_untouched_judgment: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


def label_events(
    candles: pd.DataFrame,
    events: Iterable[SignalEvent],
    *,
    exchange: str = "binanceusdm",
    config: EdgeLabelerConfig = EdgeLabelerConfig(),
    fee_profile: ExchangeFeeProfile | None = None,
) -> tuple[EventTruthLabel, ...]:
    """Label raw events against future candles.

    The event timestamp is treated as the closed decision bar.  Entry is the
    next candle open, matching the VNEDGE backtester.  Forward bars begin with
    the entry bar; if stop and target both appear inside the same candle the
    stop wins, preserving the project's conservative intrabar rule.
    """

    df = _validated_candles(candles)
    timestamps = list(df["timestamp"])
    fees = fee_profile or DEFAULT_SCALPER_PARAMETER_REGISTRY.fee_profile(exchange)
    labels = [
        _label_one(df, timestamps, event, config=config, fees=fees)
        for event in events
    ]
    return tuple(labels)


def label_strategy_events(
    candles: pd.DataFrame,
    strategy: BaseStrategy,
    *,
    exchange: str = "binanceusdm",
    source_id: str | None = None,
    route: ExecutionRoute = "MAKER_ONLY",
    config: EdgeLabelerConfig = EdgeLabelerConfig(),
    fee_profile: ExchangeFeeProfile | None = None,
) -> tuple[EventTruthLabel, ...]:
    """Run a registered strategy causally and label every raw signal."""

    events = strategy_signal_events(
        candles,
        strategy,
        source_id=source_id,
        route=route,
    )
    return label_events(
        candles,
        events,
        exchange=exchange,
        config=config,
        fee_profile=fee_profile,
    )


def strategy_signal_events(
    candles: pd.DataFrame,
    strategy: BaseStrategy,
    *,
    source_id: str | None = None,
    route: ExecutionRoute = "MAKER_ONLY",
    fill_probability: float | None = None,
    expected_edge_bps: float | None = None,
) -> tuple[SignalEvent, ...]:
    """Extract raw strategy events without forward labeling them.

    This keeps the scanner/opportunity table separate from the truth labeler.
    The event timestamp remains the decision bar close; downstream labelers
    still fill at the next candle open.
    """

    if candles.empty:
        return ()
    df = strategy.prepare(candles).reset_index(drop=True)
    if len(df) != len(candles):
        raise ValueError("strategy.prepare() must not add or drop rows")
    events: list[SignalEvent] = []
    start = max(strategy.warmup_bars, 1)
    for index in range(start, len(df) - 1):
        intent = strategy.signal(df, index)
        if intent is None:
            continue
        ts = pd.Timestamp(df["timestamp"].iloc[index])
        intent_expected_edge, intent_fill_probability = _intent_route_metadata(
            intent.reason
        )
        events.append(
            SignalEvent(
                event_id=f"{strategy.strategy_id}|{ts.isoformat()}|{len(events)}",
                ts=ts,
                side=intent.side,
                stop_price=float(intent.stop_price),
                take_profit_price=(
                    float(intent.take_profit_price)
                    if intent.take_profit_price is not None
                    else None
                ),
                source_id=source_id or strategy.strategy_id,
                strategy_id=strategy.strategy_id,
                route=route,
                expected_edge_bps=(
                    expected_edge_bps
                    if expected_edge_bps is not None
                    else intent_expected_edge
                ),
                fill_probability=(
                    fill_probability
                    if fill_probability is not None
                    else intent_fill_probability
                ),
                metadata={"reason": intent.reason, "bar_index": index},
            )
        )
    return tuple(events)


def summarize_truth(
    labels: Iterable[EventTruthLabel],
    *,
    config: EdgeLabelerConfig = EdgeLabelerConfig(),
) -> EdgeTruthSummary:
    rows = tuple(labels)
    if not rows:
        return EdgeTruthSummary(
            verdict="NO_EVENTS",
            samples=0,
            executable_samples=0,
            positive_net_samples=0,
            avg_net_bps=None,
            avg_gross_bps=None,
            avg_mfe_bps=None,
            avg_mae_bps=None,
            avg_mfe_after_cost_bps=None,
            avg_hold_bars=None,
            avg_time_to_mfe_bars=None,
            avg_capture_ratio=None,
            fee_wall_break_rate_pct=0.0,
            exit_diagnosis_counts={},
            profit_factor=None,
            target_rate_pct=0.0,
            stop_rate_pct=0.0,
            maker_assumed_samples=0,
            taker_samples=0,
            avg_fill_probability=None,
            primary_blocker="no raw scanner events",
        )

    executable = [r for r in rows if r.executable and r.net_bps is not None]
    valid_forward = [r for r in rows if r.net_bps is not None]
    net = [float(r.net_bps) for r in valid_forward if r.net_bps is not None]
    gross = [float(r.gross_bps) for r in valid_forward if r.gross_bps is not None]
    mfe = [float(r.mfe_bps) for r in valid_forward if r.mfe_bps is not None]
    mae = [float(r.mae_bps) for r in valid_forward if r.mae_bps is not None]
    mfe_after_cost = [
        float(r.mfe_after_cost_bps)
        for r in valid_forward
        if r.mfe_after_cost_bps is not None
    ]
    hold_bars = [float(r.hold_bars) for r in valid_forward if r.hold_bars is not None]
    time_to_mfe = [
        float(r.time_to_mfe_bars)
        for r in valid_forward
        if r.time_to_mfe_bars is not None
    ]
    capture = [
        float(r.capture_ratio)
        for r in valid_forward
        if r.capture_ratio is not None
    ]
    wins = [v for v in net if v > 0]
    losses = [-v for v in net if v < 0]
    pf = (sum(wins) / sum(losses)) if wins and losses else (999.0 if wins else None)
    avg_net = mean(net) if net else None
    avg_fill = mean([r.fill_probability for r in rows]) if rows else None
    maker_assumed = sum(1 for r in rows if r.fill_evidence == "maker_assumed")
    taker = sum(1 for r in rows if r.route == "TAKER_ALLOWED")
    verdict, blocker = _verdict(rows, executable, avg_net, pf, avg_fill, config)
    return EdgeTruthSummary(
        verdict=verdict,
        samples=len(rows),
        executable_samples=len(executable),
        positive_net_samples=len(wins),
        avg_net_bps=_round_or_none(avg_net),
        avg_gross_bps=_round_or_none(mean(gross) if gross else None),
        avg_mfe_bps=_round_or_none(mean(mfe) if mfe else None),
        avg_mae_bps=_round_or_none(mean(mae) if mae else None),
        avg_mfe_after_cost_bps=_round_or_none(
            mean(mfe_after_cost) if mfe_after_cost else None
        ),
        avg_hold_bars=_round_or_none(mean(hold_bars) if hold_bars else None),
        avg_time_to_mfe_bars=_round_or_none(
            mean(time_to_mfe) if time_to_mfe else None
        ),
        avg_capture_ratio=_round_or_none(mean(capture) if capture else None),
        fee_wall_break_rate_pct=_fee_wall_break_rate(valid_forward),
        exit_diagnosis_counts=dict(Counter(r.exit_diagnosis for r in valid_forward)),
        profit_factor=_round_or_none(pf),
        target_rate_pct=_outcome_rate(valid_forward, "target"),
        stop_rate_pct=_outcome_rate(valid_forward, "stop"),
        maker_assumed_samples=maker_assumed,
        taker_samples=taker,
        avg_fill_probability=_round_or_none(avg_fill),
        primary_blocker=blocker,
    )


def build_truth_report(
    *,
    exchange: str,
    symbol: str,
    timeframe: str,
    labels: Iterable[EventTruthLabel],
    config: EdgeLabelerConfig = EdgeLabelerConfig(),
    strategy_id: str | None = None,
) -> dict:
    label_tuple = tuple(labels)
    summary = summarize_truth(label_tuple, config=config)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "truth_layer": "execution_edge_labeler_v1",
        "exchange": exchange,
        "symbol": symbol,
        "timeframe": timeframe,
        "strategy": strategy_id,
        "policy": {
            "research_only": True,
            "can_trade": False,
            "can_promote": False,
            "requires_untouched_judgment": True,
            "candle_fill_warning": (
                "TAKER labels use next-open fills. MAKER labels only carry an "
                "assumed fill probability unless replay/shadow evidence supplies it."
            ),
        },
        "config": asdict(config),
        "summary": summary.to_dict(),
        "labels": [label.to_dict() for label in label_tuple],
    }


def _label_one(
    df: pd.DataFrame,
    timestamps: list[pd.Timestamp],
    event: SignalEvent,
    *,
    config: EdgeLabelerConfig,
    fees: ExchangeFeeProfile,
) -> EventTruthLabel:
    event_ts = _normalize_ts(event.ts)
    decision_index = _decision_index(timestamps, event_ts)
    blockers: list[str] = []
    if decision_index is None:
        return _invalid_label(event, fees, "event timestamp before first candle")
    entry_index = decision_index + 1
    if entry_index >= len(df):
        return _invalid_label(event, fees, "no next candle for entry")

    entry_bar = df.iloc[entry_index]
    entry_price = float(entry_bar["open"])
    risk = _risk_bps(event.side, entry_price, event.stop_price)
    if risk is None or risk <= 0:
        blockers.append("invalid_stop_geometry")

    fill_probability, fill_evidence = _fill_probability(event, config)
    if (
        event.route == "MAKER_ONLY"
        and fill_probability < config.min_maker_fill_probability
    ):
        blockers.append("maker_fill_probability_below_floor")

    route_cost = _route_cost_bps(event.route, fees)
    horizon_end = min(len(df) - 1, entry_index + config.horizon_bars - 1)
    future = df.iloc[entry_index : horizon_end + 1]
    if future.empty:
        return _invalid_label(event, fees, "no forward candles")

    outcome, exit_index, raw_exit = _resolve_exit(event, future, entry_index)
    if raw_exit is None:
        raw_exit = float(future.iloc[-1]["close"])
        exit_index = int(future.index[-1])
    future_to_exit = df.iloc[entry_index : int(exit_index) + 1]
    mfe_bps, mae_bps, time_to_mfe, time_to_mae = _excursion_stats(
        event.side,
        entry_price,
        future_to_exit,
    )
    gross = _signed_bps(event.side, entry_price, raw_exit)
    net = gross - route_cost
    mfe_after_cost = mfe_bps - route_cost
    target_bps = _target_bps(event.side, entry_price, event.take_profit_price)
    hold_bars = int(exit_index) - entry_index + 1
    capture_ratio = _capture_ratio(gross, mfe_bps)
    exit_diagnosis = _exit_diagnosis(
        outcome=outcome,
        net_bps=net,
        mfe_after_cost_bps=mfe_after_cost,
        capture_ratio=capture_ratio,
    )
    max_r = (mfe_bps / risk) if risk and risk > 0 else None
    min_r = (mae_bps / risk) if risk and risk > 0 else None
    if config.include_sizing_blockers and risk is not None and risk <= 0:
        blockers.append("invalid_risk_bps")
    return EventTruthLabel(
        event_id=event.event_id,
        ts=event_ts.isoformat(),
        side=event.side,
        route=event.route,
        source_id=event.source_id,
        strategy_id=event.strategy_id,
        entry_ts=pd.Timestamp(df["timestamp"].iloc[entry_index]).isoformat(),
        entry_price=round(entry_price, 8),
        stop_price=event.stop_price,
        take_profit_price=event.take_profit_price,
        outcome=outcome,
        exit_ts=pd.Timestamp(df["timestamp"].iloc[exit_index]).isoformat()
        if exit_index is not None
        else None,
        exit_price=round(raw_exit, 8),
        gross_bps=round(gross, 4),
        route_cost_bps=round(route_cost, 4),
        net_bps=round(net, 4),
        mfe_bps=round(mfe_bps, 4),
        mae_bps=round(mae_bps, 4),
        mfe_after_cost_bps=round(mfe_after_cost, 4),
        risk_bps=round(risk, 4) if risk is not None else None,
        target_bps=round(target_bps, 4) if target_bps is not None else None,
        hold_bars=hold_bars,
        time_to_mfe_bars=time_to_mfe,
        time_to_mae_bars=time_to_mae,
        capture_ratio=round(capture_ratio, 4) if capture_ratio is not None else None,
        max_r=round(max_r, 4) if max_r is not None else None,
        min_r=round(min_r, 4) if min_r is not None else None,
        exit_diagnosis=exit_diagnosis,
        fill_probability=round(fill_probability, 4),
        fill_evidence=fill_evidence,
        blockers=tuple(dict.fromkeys(blockers)),
        metadata=_event_metadata(event),
    )


def _validated_candles(candles: pd.DataFrame) -> pd.DataFrame:
    required = {"timestamp", "open", "high", "low", "close"}
    missing = sorted(required - set(candles.columns))
    if missing:
        raise ValueError(f"candles missing columns: {missing}")
    df = candles.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    if not df["timestamp"].is_monotonic_increasing or df["timestamp"].duplicated().any():
        raise ValueError("candles must be sorted and unique by timestamp")
    return df.reset_index(drop=True)


def _decision_index(timestamps: list[pd.Timestamp], event_ts: pd.Timestamp) -> int | None:
    event_ts = _normalize_ts(event_ts)
    pos = pd.Index(timestamps).searchsorted(event_ts, side="right") - 1
    return int(pos) if pos >= 0 else None


def _normalize_ts(value: object) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize(UTC)
    return ts.tz_convert(UTC)


def _route_cost_bps(route: ExecutionRoute, fees: ExchangeFeeProfile) -> float:
    if route == "TAKER_ALLOWED":
        return fees.taker_round_trip_cost_bps
    return fees.maker_first_cost_bps


def _fill_probability(
    event: SignalEvent,
    config: EdgeLabelerConfig,
) -> tuple[float, str]:
    if event.route == "TAKER_ALLOWED":
        return 1.0, "taker_next_open"
    if event.fill_probability is not None:
        return float(event.fill_probability), "maker_supplied"
    return config.default_maker_fill_probability, "maker_assumed"


def _risk_bps(side: str, entry: float, stop: float) -> float | None:
    if entry <= 0 or stop <= 0:
        return None
    if side == "long":
        return (entry - stop) / entry * 10_000.0
    return (stop - entry) / entry * 10_000.0


def _signed_bps(side: str, entry: float, price: float) -> float:
    move = (price - entry) / entry * 10_000.0
    return move if side == "long" else -move


def _excursion_stats(
    side: str,
    entry: float,
    future: pd.DataFrame,
) -> tuple[float, float, int | None, int | None]:
    best_favorable = -math.inf
    worst_adverse = math.inf
    time_to_mfe: int | None = None
    time_to_mae: int | None = None
    for offset, (_, bar) in enumerate(future.iterrows(), start=1):
        high = float(bar["high"])
        low = float(bar["low"])
        if side == "long":
            favorable = (high - entry) / entry * 10_000.0
            adverse = (low - entry) / entry * 10_000.0
        else:
            favorable = (entry - low) / entry * 10_000.0
            adverse = (entry - high) / entry * 10_000.0
        if favorable > best_favorable:
            best_favorable = favorable
            time_to_mfe = offset
        if adverse < worst_adverse:
            worst_adverse = adverse
            time_to_mae = offset

    mfe = max(0.0, best_favorable)
    mae = min(0.0, worst_adverse)
    return (
        mfe,
        mae,
        time_to_mfe if mfe > 0.0 else None,
        time_to_mae if mae < 0.0 else None,
    )


def _target_bps(side: str, entry: float, target: float | None) -> float | None:
    if target is None or entry <= 0 or target <= 0:
        return None
    if side == "long":
        return (target - entry) / entry * 10_000.0
    return (entry - target) / entry * 10_000.0


def _capture_ratio(gross_bps: float, mfe_bps: float) -> float | None:
    if mfe_bps <= 0.0:
        return None
    return gross_bps / mfe_bps


def _exit_diagnosis(
    *,
    outcome: OutcomeKind,
    net_bps: float,
    mfe_after_cost_bps: float,
    capture_ratio: float | None,
) -> str:
    if net_bps > 0.0:
        return "CAPTURED_AFTER_COST"
    if mfe_after_cost_bps <= 0.0:
        return "MOVE_NEVER_CLEARED_COST"
    if outcome == "stop":
        return "STOP_BEFORE_TARGET"
    if capture_ratio is not None and capture_ratio < 0.25:
        return "GAVE_BACK_FEE_WALL_MOVE"
    if outcome == "horizon":
        return "HORIZON_EXIT_FAILED_CAPTURE"
    return "NEGATIVE_AFTER_COST"


def _resolve_exit(
    event: SignalEvent,
    future: pd.DataFrame,
    entry_index: int,
) -> tuple[OutcomeKind, int | None, float | None]:
    for offset, (_, bar) in enumerate(future.iterrows()):
        idx = entry_index + offset
        high = float(bar["high"])
        low = float(bar["low"])
        if event.side == "long":
            if low <= event.stop_price:
                return "stop", idx, float(event.stop_price)
            if event.take_profit_price is not None and high >= event.take_profit_price:
                return "target", idx, float(event.take_profit_price)
        else:
            if high >= event.stop_price:
                return "stop", idx, float(event.stop_price)
            if event.take_profit_price is not None and low <= event.take_profit_price:
                return "target", idx, float(event.take_profit_price)
    idx = int(future.index[-1])
    return "horizon", idx, float(future.iloc[-1]["close"])


def _invalid_label(event: SignalEvent, fees: ExchangeFeeProfile, reason: str) -> EventTruthLabel:
    return EventTruthLabel(
        event_id=event.event_id,
        ts=_normalize_ts(event.ts).isoformat(),
        side=event.side,
        route=event.route,
        source_id=event.source_id,
        strategy_id=event.strategy_id,
        entry_ts=None,
        entry_price=None,
        stop_price=event.stop_price,
        take_profit_price=event.take_profit_price,
        outcome="invalid",
        exit_ts=None,
        exit_price=None,
        gross_bps=None,
        route_cost_bps=round(_route_cost_bps(event.route, fees), 4),
        net_bps=None,
        mfe_bps=None,
        mae_bps=None,
        mfe_after_cost_bps=None,
        risk_bps=None,
        target_bps=None,
        hold_bars=None,
        time_to_mfe_bars=None,
        time_to_mae_bars=None,
        capture_ratio=None,
        max_r=None,
        min_r=None,
        exit_diagnosis="INVALID_FORWARD_LABEL",
        fill_probability=0.0,
        fill_evidence="none",
        blockers=(reason,),
        metadata=_event_metadata(event),
    )


def _event_metadata(event: SignalEvent) -> dict:
    metadata = dict(event.metadata)
    if event.expected_edge_bps is not None:
        metadata["expected_edge_bps"] = float(event.expected_edge_bps)
    if event.fill_probability is not None:
        metadata["fill_probability"] = float(event.fill_probability)
    return metadata


_INTENT_KV_RE = re.compile(
    r"(?P<key>[A-Za-z][A-Za-z0-9_]*)(?P<sep>=|:)"
    r"(?P<value>[+-]?(?:\d+(?:\.\d*)?|\.\d+))"
)
_EXPECTED_EDGE_KEYS = {
    "expectededge",
    "expectededgebps",
    "expectednet",
    "expectednetbps",
}
_FILL_PROBABILITY_KEYS = {
    "fillprobability",
    "fillprob",
    "makerfillprobability",
    "makerfillprob",
}


def _intent_route_metadata(reason: str) -> tuple[float | None, float | None]:
    """Extract machine-readable route hints from a strategy reason string."""

    expected_edge: float | None = None
    fill_probability: float | None = None
    for match in _INTENT_KV_RE.finditer(reason or ""):
        key = re.sub(r"[^A-Za-z0-9]", "", match.group("key")).lower()
        try:
            value = float(match.group("value"))
        except ValueError:
            continue
        if not math.isfinite(value):
            continue
        if key in _EXPECTED_EDGE_KEYS:
            expected_edge = value
        elif key in _FILL_PROBABILITY_KEYS:
            fill_probability = max(0.0, min(1.0, value))
    return expected_edge, fill_probability


def _verdict(
    rows: tuple[EventTruthLabel, ...],
    executable: list[EventTruthLabel],
    avg_net: float | None,
    pf: float | None,
    avg_fill: float | None,
    config: EdgeLabelerConfig,
) -> tuple[TruthVerdict, str]:
    if avg_fill is not None and avg_fill < config.min_maker_fill_probability:
        return "LOW_FILL_CONFIDENCE", "maker fill confidence below floor"
    if len(rows) < config.min_samples:
        return "UNDER_SAMPLED", f"only {len(rows)} events; need >= {config.min_samples}"
    if not executable:
        return "NEGATIVE_AFTER_COST", "no executable forward labels"
    if avg_net is None or avg_net < config.min_avg_net_bps or (pf or 0.0) < config.min_profit_factor:
        return "NEGATIVE_AFTER_COST", "average net/PF below maker breakeven"
    taker_rows = [r for r in executable if r.route == "TAKER_ALLOWED"]
    if taker_rows and avg_net >= config.taker_min_avg_net_bps and (pf or 0.0) >= config.taker_min_profit_factor:
        return "TAKER_EDGE", "taker route clears fee wall"
    return "MAKER_EDGE", "maker-first route has positive after-cost edge"


def _round_or_none(value: float | None) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(float(value), 4)


def _outcome_rate(rows: list[EventTruthLabel], outcome: OutcomeKind) -> float:
    if not rows:
        return 0.0
    return round(sum(1 for row in rows if row.outcome == outcome) / len(rows) * 100.0, 2)


def _fee_wall_break_rate(rows: list[EventTruthLabel]) -> float:
    if not rows:
        return 0.0
    cleared = sum(
        1
        for row in rows
        if row.mfe_after_cost_bps is not None and row.mfe_after_cost_bps > 0.0
    )
    return round(cleared / len(rows) * 100.0, 2)


def _window(df: pd.DataFrame, lookback_days: int | None) -> pd.DataFrame:
    if lookback_days is None or lookback_days <= 0 or df.empty:
        return df.reset_index(drop=True)
    latest = pd.Timestamp(df["timestamp"].iloc[-1])
    cutoff = latest - pd.Timedelta(days=lookback_days)
    return df[pd.to_datetime(df["timestamp"], utc=True) >= cutoff].reset_index(drop=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="label strategy/scanner events by executable post-fee forward outcome"
    )
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--exchange", required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--timeframe", default="5m")
    parser.add_argument("--strategy", required=True)
    parser.add_argument("--route", choices=("MAKER_ONLY", "TAKER_ALLOWED"), default="MAKER_ONLY")
    parser.add_argument("--lookback-days", type=int, default=30)
    parser.add_argument("--horizon-bars", type=int, default=12)
    parser.add_argument("--min-samples", type=int, default=10)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    store = ParquetStore(args.data_root)
    try:
        candles = _window(
            store.read_candles(args.exchange, args.symbol, args.timeframe),
            args.lookback_days,
        )
    except FileNotFoundError as exc:
        print(
            _missing_candles_message(
                store,
                args.exchange,
                args.symbol,
                args.timeframe,
                exc,
            ),
            file=sys.stderr,
        )
        return 2
    strategy_cls = get_strategy_class(args.strategy)
    strategy = _instantiate_strategy(strategy_cls, store, args.exchange, args.symbol)
    config = EdgeLabelerConfig(
        horizon_bars=args.horizon_bars,
        min_samples=args.min_samples,
    )
    labels = label_strategy_events(
        candles,
        strategy,
        exchange=args.exchange,
        route=args.route,
        config=config,
    )
    report = build_truth_report(
        exchange=args.exchange,
        symbol=args.symbol,
        timeframe=args.timeframe,
        strategy_id=args.strategy,
        labels=labels,
        config=config,
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        s = report["summary"]
        print(
            "execution edge truth "
            f"{args.exchange} {args.symbol} {args.timeframe} {args.strategy}: "
            f"{s['verdict']} samples={s['samples']} "
            f"avg_net_bps={s['avg_net_bps']} pf={s['profit_factor']} "
            f"blocker={s['primary_blocker']}"
        )
    return 0


def _instantiate_strategy(
    strategy_cls: type[BaseStrategy],
    store: ParquetStore,
    exchange: str,
    symbol: str,
) -> BaseStrategy:
    params = inspect.signature(strategy_cls).parameters
    if "funding" not in params:
        return strategy_cls()
    try:
        funding = store.read_funding(exchange, symbol)
    except FileNotFoundError:
        funding = None
    if funding is None and params["funding"].default is inspect.Signature.empty:
        raise FileNotFoundError(
            f"{strategy_cls.strategy_id} requires funding data for {exchange}:{symbol}"
        )
    return strategy_cls(funding=funding)


def _missing_candles_message(
    store: ParquetStore,
    exchange: str,
    symbol: str,
    timeframe: str,
    exc: FileNotFoundError,
) -> str:
    expected = store.candles_path(exchange, symbol, timeframe)
    symbol_dir = expected.parent.parent
    available = sorted(
        path.parent.name.removeprefix("timeframe=")
        for path in symbol_dir.glob("timeframe=*/candles.parquet")
    )
    available_text = ", ".join(available) if available else "none visible"
    return (
        f"{exc}. Missing candle lane is data/config, not a strategy verdict. "
        f"Expected {expected}. Visible timeframes for {exchange}:{symbol}: "
        f"{available_text}. If the host has the parquet file but this command "
        "does not, check the container data mount."
    )


if __name__ == "__main__":
    raise SystemExit(main())
