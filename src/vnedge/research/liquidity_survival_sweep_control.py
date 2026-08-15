"""Causal location + event-quality study for liquidity survival sweeps.

This module does not implement a tradable scanner.  It qualifies already
independent Event Episode Quality observations with point-in-time liquidity
location, online survival evidence, structural target room, and earlier
matched controls.  Exit research remains fail-closed until the control gate
passes.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import pandas as pd
import yaml

from vnedge.research.delta_scalper_backtest import _load_candles
from vnedge.research.event_response_atlas import EventObservation, PriceTape, _load_tapes
from vnedge.research.strategy_evidence_registry import route_cost_contract
from vnedge.scalping.delta_engine.candle_store import ClosedCandleAggregator
from vnedge.scalping.delta_engine.types import Candle

SCHEMA_VERSION = "vnedge.liquidity_survival_sweep_control.v1"
STUDY_ID = "liquidity_survival_sweep_control_v1"
DEFAULT_CONFIG = Path("configs/research/liquidity_survival_sweep_control_v1.yaml")
DEFAULT_OUTPUT = Path("research/live_research/liquidity_survival_sweep_control_v1_latest.json")


@dataclass(frozen=True)
class ScaleSpec:
    name: str
    timeframe: str
    pivot_left: int
    pivot_right: int
    maximum_age_bars: int


@dataclass(frozen=True)
class ConfirmedLevel:
    level_id: str
    symbol: str
    side: str
    price: float
    scale: str
    confirmed_at: datetime
    expires_at: datetime


@dataclass
class TrackedPool:
    pool_id: str
    symbol: str
    side: str
    price: float
    low: float
    high: float
    confirmed_at: datetime
    expires_at: datetime
    members: set[str] = field(default_factory=set)
    scales: set[str] = field(default_factory=set)
    touches: int = 0
    last_interaction_index: int | None = None
    active: bool = True


@dataclass(frozen=True)
class SurvivalEstimate:
    probability: float
    resolved_interactions: int
    successes: int
    source: str


@dataclass(frozen=True)
class LocationSnapshot:
    episode_key: str
    symbol: str
    available_at: datetime
    anchor_price: float
    reversal_direction: int
    matched_pool_id: str | None
    pool_side: str | None
    pool_price: float | None
    merged_members: int
    scales: tuple[str, ...]
    age_bars_15m: int
    touches: int
    survival_probability: float
    survival_resolved_interactions: int
    survival_source: str
    target_pool_id: str | None
    target_price: float | None
    target_room_bps: float | None
    invalidation_bps: float | None
    location_fresh: bool


@dataclass(frozen=True)
class QualityEpisodeRecord:
    key: str
    symbol: str
    decision_ts: datetime
    quality_available_ts: datetime
    anchor_price: float
    reversal_direction: int
    market_truth_complete: bool
    abnormal_score: float
    abnormal_score_passed: bool
    direction_state: str | None
    directional_mfe_bps: float


class OnlineSurvivalCalibrator:
    """Beta posterior containing only already-resolved level interactions."""

    def __init__(
        self,
        *,
        alpha: float,
        beta: float,
        minimum_resolved: int,
        age_buckets: tuple[int, ...],
        touch_buckets: tuple[int, ...],
    ) -> None:
        if alpha <= 0 or beta <= 0 or minimum_resolved <= 0:
            raise ValueError("invalid survival calibration contract")
        self.alpha = alpha
        self.beta = beta
        self.minimum_resolved = minimum_resolved
        self.age_buckets = age_buckets
        self.touch_buckets = touch_buckets
        self._cells: dict[tuple[str, str, str, str], list[int]] = defaultdict(lambda: [0, 0])
        self._side: dict[str, list[int]] = defaultdict(lambda: [0, 0])

    def observe(
        self,
        *,
        side: str,
        age_bars: int,
        touches: int,
        volatility_bucket: str,
        survived: bool,
    ) -> None:
        key = self._key(side, age_bars, touches, volatility_bucket)
        self._cells[key][0] += int(survived)
        self._cells[key][1] += 1
        self._side[side][0] += int(survived)
        self._side[side][1] += 1

    def estimate(
        self,
        *,
        side: str,
        age_bars: int,
        touches: int,
        volatility_bucket: str,
    ) -> SurvivalEstimate:
        key = self._key(side, age_bars, touches, volatility_bucket)
        successes, total = self._cells.get(key, [0, 0])
        source = "exact_cell"
        if total < self.minimum_resolved:
            successes, total = self._side.get(side, [0, 0])
            source = "side_fallback"
        probability = (successes + self.alpha) / (total + self.alpha + self.beta)
        return SurvivalEstimate(probability, total, successes, source)

    def _key(
        self, side: str, age_bars: int, touches: int, volatility_bucket: str
    ) -> tuple[str, str, str, str]:
        return (
            side,
            _bucket(age_bars, self.age_buckets),
            _bucket(touches, self.touch_buckets),
            volatility_bucket,
        )


class LiquidityLifecycle:
    """Streaming, completed-candle-only liquidity pool lifecycle."""

    def __init__(self, symbol: str, config: Mapping[str, Any]) -> None:
        location = config["location"]
        survival = config["survival"]
        self.symbol = symbol.upper()
        self.merge_tolerance_bps = float(location["merge_tolerance_bps"])
        self.maximum_event_distance_bps = float(location["maximum_event_distance_bps"])
        self.invalidation_close_bps = float(location["invalidation_close_bps"])
        self.interaction_cooldown_bars = int(location["interaction_cooldown_bars"])
        self.stop_buffer_bps = float(config["economics"]["stop_buffer_bps"])
        self.pools: list[TrackedPool] = []
        self.calibrator = OnlineSurvivalCalibrator(
            alpha=float(survival["beta_prior_alpha"]),
            beta=float(survival["beta_prior_beta"]),
            minimum_resolved=int(survival["minimum_resolved_interactions"]),
            age_buckets=tuple(int(value) for value in survival["age_buckets_bars"]),
            touch_buckets=tuple(int(value) for value in survival["touch_buckets"]),
        )

    def add_level(self, level: ConfirmedLevel) -> None:
        candidates = [
            pool
            for pool in self.pools
            if pool.active
            and pool.side == level.side
            and _distance_bps(pool.price, level.price) <= self.merge_tolerance_bps
        ]
        if candidates:
            pool = min(candidates, key=lambda row: _distance_bps(row.price, level.price))
            member_count = len(pool.members)
            pool.price = (pool.price * member_count + level.price) / (member_count + 1)
            pool.low = min(pool.low, level.price)
            pool.high = max(pool.high, level.price)
            pool.members.add(level.level_id)
            pool.scales.add(level.scale)
            pool.confirmed_at = min(pool.confirmed_at, level.confirmed_at)
            pool.expires_at = max(pool.expires_at, level.expires_at)
            return
        self.pools.append(
            TrackedPool(
                pool_id=level.level_id,
                symbol=level.symbol,
                side=level.side,
                price=level.price,
                low=level.price,
                high=level.price,
                confirmed_at=level.confirmed_at,
                expires_at=level.expires_at,
                members={level.level_id},
                scales={level.scale},
            )
        )

    def on_closed_15m(self, candle: Candle, *, bar_index: int, volatility_bucket: str) -> None:
        for pool in self.pools:
            if not pool.active:
                continue
            if candle.ts > pool.expires_at:
                pool.active = False
                continue
            # A pivot confirmed by this same close was not actionable during
            # the candle, so its first interaction can only be a later bar.
            if pool.confirmed_at >= candle.ts:
                continue
            intersects = candle.high >= pool.low and candle.low <= pool.high
            if not intersects:
                continue
            if (
                pool.last_interaction_index is not None
                and bar_index - pool.last_interaction_index < self.interaction_cooldown_bars
            ):
                continue
            age = max(0, int((candle.ts - pool.confirmed_at).total_seconds() // 900))
            boundary = (
                pool.high * (1.0 + self.invalidation_close_bps / 10_000.0)
                if pool.side == "high"
                else pool.low * (1.0 - self.invalidation_close_bps / 10_000.0)
            )
            survived = candle.close <= boundary if pool.side == "high" else candle.close >= boundary
            self.calibrator.observe(
                side=pool.side,
                age_bars=age,
                touches=pool.touches,
                volatility_bucket=volatility_bucket,
                survived=survived,
            )
            pool.touches += 1
            pool.last_interaction_index = bar_index
            if not survived:
                pool.active = False

    def snapshot(
        self,
        episode: QualityEpisodeRecord,
        *,
        volatility_bucket: str,
        last_closed_ts: datetime | None,
    ) -> LocationSnapshot:
        side = "high" if episode.reversal_direction < 0 else "low"
        location_fresh = bool(
            last_closed_ts is not None
            and last_closed_ts <= episode.decision_ts
            and episode.decision_ts - last_closed_ts <= timedelta(minutes=15)
        )
        candidates = []
        for pool in self.pools:
            if not pool.active or pool.side != side or episode.decision_ts > pool.expires_at:
                continue
            penetrated = (
                episode.anchor_price >= pool.low
                if side == "high"
                else episode.anchor_price <= pool.high
            )
            if (
                penetrated
                and _distance_bps(pool.price, episode.anchor_price)
                <= self.maximum_event_distance_bps
            ):
                candidates.append(pool)
        matched = (
            max(
                candidates,
                key=lambda row: (
                    len(row.members),
                    len(row.scales),
                    -_distance_bps(row.price, episode.anchor_price),
                ),
            )
            if candidates
            else None
        )
        if matched is None:
            return _empty_location(episode, last_closed_ts, location_fresh)
        age = max(0, int((episode.decision_ts - matched.confirmed_at).total_seconds() // 900))
        estimate = self.calibrator.estimate(
            side=matched.side,
            age_bars=age,
            touches=matched.touches,
            volatility_bucket=volatility_bucket,
        )
        direction = _direction(episode)
        targets = [
            pool
            for pool in self.pools
            if pool.active
            and pool.pool_id != matched.pool_id
            and episode.decision_ts <= pool.expires_at
            and (
                (direction > 0 and pool.price > episode.anchor_price)
                or (direction < 0 and pool.price < episode.anchor_price)
            )
        ]
        target = (
            min(targets, key=lambda row: abs(row.price - episode.anchor_price)) if targets else None
        )
        target_room = abs(target.price / episode.anchor_price - 1.0) * 10_000.0 if target else None
        invalidation = (
            _distance_bps(
                episode.anchor_price,
                matched.high if side == "high" else matched.low,
            )
            + self.stop_buffer_bps
        )
        return LocationSnapshot(
            episode_key=episode.key,
            symbol=episode.symbol,
            available_at=episode.decision_ts,
            anchor_price=episode.anchor_price,
            reversal_direction=episode.reversal_direction,
            matched_pool_id=matched.pool_id,
            pool_side=matched.side,
            pool_price=matched.price,
            merged_members=len(matched.members),
            scales=tuple(sorted(matched.scales)),
            age_bars_15m=age,
            touches=matched.touches,
            survival_probability=estimate.probability,
            survival_resolved_interactions=estimate.resolved_interactions,
            survival_source=estimate.source,
            target_pool_id=target.pool_id if target else None,
            target_price=target.price if target else None,
            target_room_bps=target_room,
            invalidation_bps=invalidation,
            location_fresh=location_fresh,
        )


def load_config(path: Path | str = DEFAULT_CONFIG) -> dict[str, Any]:
    payload = yaml.safe_load(Path(path).read_text())
    if payload.get("contract_id") != STUDY_ID:
        raise ValueError("unexpected liquidity survival contract")
    if payload.get("research_only") is not True:
        raise ValueError("liquidity survival study must be research-only")
    policy = payload.get("policy") or {}
    if policy.get("exits_before_control_pass") != "forbidden":
        raise ValueError("exit research must be locked behind controls")
    if policy.get("sealed_tail_auto_open") is not False:
        raise ValueError("sealed tail must never auto-open")
    if policy.get("amf_v3_evidence_mixing") != "forbidden":
        raise ValueError("AMF evidence must remain separate")
    offsets = tuple(int(value) for value in payload["controls"]["prior_offsets_ms"])
    if not offsets or any(value >= 0 for value in offsets):
        raise ValueError("all matched-control offsets must be strictly earlier")
    return payload


def load_quality_episodes(path: Path | str) -> tuple[dict[str, Any], list[QualityEpisodeRecord]]:
    payload = json.loads(Path(path).read_text())
    if payload.get("schema_version") != "vnedge.event_episode_quality.v1":
        raise ValueError("unsupported Event Episode Quality artifact")
    if payload.get("can_trade") is not False or payload.get("can_promote") is not False:
        raise ValueError("event-quality source must remain research locked")
    records: list[QualityEpisodeRecord] = []
    for row in payload.get("episodes") or []:
        price, direction = _anchor_fields(row)
        records.append(
            QualityEpisodeRecord(
                key=str(row["episode_key"]),
                symbol=str(row["symbol"]).upper(),
                decision_ts=_parse_ts(row["decision_ts"]),
                quality_available_ts=_parse_ts(row["quality_available_ts"]),
                anchor_price=price,
                reversal_direction=direction,
                market_truth_complete=row.get("market_truth_complete") is True,
                abnormal_score=float(row.get("abnormal_score") or 0.0),
                abnormal_score_passed=row.get("abnormal_score_passed") is True,
                direction_state=(
                    str(row["directional_confirmation"])
                    if row.get("directional_confirmation")
                    else None
                ),
                directional_mfe_bps=float(row.get("directional_mfe_bps") or 0.0),
            )
        )
    return payload, sorted(records, key=lambda row: (row.decision_ts, row.key))


def confirmed_levels(
    candles: list[Candle], *, symbol: str, spec: ScaleSpec
) -> dict[datetime, list[ConfirmedLevel]]:
    result: dict[datetime, list[ConfirmedLevel]] = defaultdict(list)
    seconds = 900 if spec.timeframe == "15m" else 3_600
    for index in range(spec.pivot_left + spec.pivot_right, len(candles)):
        pivot_index = index - spec.pivot_right
        start = pivot_index - spec.pivot_left
        window = candles[start : index + 1]
        pivot = candles[pivot_index]
        for side, price, values in (
            ("high", pivot.high, [row.high for row in window]),
            ("low", pivot.low, [row.low for row in window]),
        ):
            extreme = max(values) if side == "high" else min(values)
            if price != extreme or sum(value == price for value in values) != 1:
                continue
            confirmed_at = candles[index].ts
            result[confirmed_at].append(
                ConfirmedLevel(
                    level_id=(
                        f"{symbol}:{spec.name}:{side}:"
                        f"{int(pivot.ts.timestamp())}:{int(confirmed_at.timestamp())}"
                    ),
                    symbol=symbol,
                    side=side,
                    price=price,
                    scale=spec.name,
                    confirmed_at=confirmed_at,
                    expires_at=confirmed_at + timedelta(seconds=seconds * spec.maximum_age_bars),
                )
            )
    return result


def build_location_snapshots(
    one_minute: list[Candle],
    episodes: list[QualityEpisodeRecord],
    *,
    symbol: str,
    config: Mapping[str, Any],
) -> dict[str, LocationSnapshot]:
    if not episodes:
        return {}
    higher = _aggregate(one_minute, ("15m", "1h"))
    fifteen = higher["15m"]
    schedules: dict[datetime, list[ConfirmedLevel]] = defaultdict(list)
    for raw in config["location"]["scales"]:
        spec = ScaleSpec(
            name=str(raw["name"]),
            timeframe=str(raw["timeframe"]),
            pivot_left=int(raw["pivot_left"]),
            pivot_right=int(raw["pivot_right"]),
            maximum_age_bars=int(raw["maximum_age_bars"]),
        )
        for ts, rows in confirmed_levels(higher[spec.timeframe], symbol=symbol, spec=spec).items():
            schedules[ts].extend(rows)
    lifecycle = LiquidityLifecycle(symbol, config)
    bars = _with_volatility_buckets(fifteen)
    ordered = sorted(episodes, key=lambda row: row.decision_ts)
    snapshots: dict[str, LocationSnapshot] = {}
    bar_index = 0
    last_closed: datetime | None = None
    latest_volatility = "unknown"
    for episode in ordered:
        while bar_index < len(bars) and bars[bar_index][0].ts <= episode.decision_ts:
            candle, bucket = bars[bar_index]
            if last_closed is not None and candle.ts - last_closed != timedelta(minutes=15):
                for pool in lifecycle.pools:
                    pool.active = False
            for level in schedules.get(candle.ts, ()):
                lifecycle.add_level(level)
            lifecycle.on_closed_15m(candle, bar_index=bar_index, volatility_bucket=bucket)
            last_closed = candle.ts
            latest_volatility = bucket
            bar_index += 1
        snapshots[episode.key] = lifecycle.snapshot(
            episode,
            volatility_bucket=latest_volatility,
            last_closed_ts=last_closed,
        )
    return snapshots


def qualify_episode(
    episode: QualityEpisodeRecord,
    location: LocationSnapshot,
    *,
    config: Mapping[str, Any],
    cost_bps: float,
) -> dict[str, Any]:
    reasons: list[str] = []
    quality = config["quality"]
    survival = config["survival"]
    economics = config["economics"]
    if quality["require_market_truth_complete"] and not episode.market_truth_complete:
        reasons.append("MARKET_TRUTH_INCOMPLETE")
    if not episode.abnormal_score_passed or episode.abnormal_score < float(
        quality["minimum_abnormal_score"]
    ):
        reasons.append("ABNORMAL_EVENT_QUALITY_FAILED")
    if location.matched_pool_id is None:
        reasons.append("NO_CAUSAL_LIQUIDITY_LOCATION")
    if not location.location_fresh:
        reasons.append("LIQUIDITY_CONTEXT_STALE_OR_GAPPED")
    if location.merged_members < int(config["location"]["minimum_merged_members"]):
        reasons.append("NOT_MULTI_LEVEL")
    if location.survival_resolved_interactions < int(survival["minimum_resolved_interactions"]):
        reasons.append("SURVIVAL_HISTORY_INSUFFICIENT")
    if quality["require_directional_confirmation"] and episode.direction_state not in set(
        quality["allowed_direction_states"]
    ):
        reasons.append("DIRECTION_NOT_CONFIRMED")
    probability = (
        location.survival_probability
        if episode.direction_state == "reversal_reclaim"
        else 1.0 - location.survival_probability
    )
    if probability < float(survival["minimum_direction_probability"]):
        reasons.append("DIRECTION_PROBABILITY_TOO_LOW")
    target_room = location.target_room_bps
    minimum_room = float(economics["minimum_target_cost_multiple"]) * cost_bps
    if target_room is None:
        reasons.append("OPPOSING_TARGET_UNAVAILABLE")
    elif target_room < minimum_room:
        reasons.append("TARGET_ROOM_BELOW_COST_MULTIPLE")
    risk = location.invalidation_bps
    prior_expected_net = (
        probability * target_room - (1.0 - probability) * risk - cost_bps
        if target_room is not None and risk is not None
        else None
    )
    if prior_expected_net is None or prior_expected_net <= float(
        economics["minimum_prior_expected_net_bps"]
    ):
        reasons.append("PRIOR_STRUCTURAL_EV_NOT_POSITIVE")
    selected = not reasons
    return {
        "episode_key": episode.key,
        "symbol": episode.symbol,
        "decision_ts": episode.decision_ts.isoformat(),
        "quality_available_ts": episode.quality_available_ts.isoformat(),
        "anchor_price": episode.anchor_price,
        "anchor_reversal_direction": episode.reversal_direction,
        "direction_state": episode.direction_state,
        "trade_direction": _direction(episode),
        "abnormal_score": episode.abnormal_score,
        "location": asdict(location),
        "direction_probability": probability,
        "minimum_target_room_bps": minimum_room,
        "prior_expected_net_bps": prior_expected_net,
        "prior_expected_net_is_calibrated_realized_edge": False,
        "selected": selected,
        "rejection_reasons": reasons,
        "realized_directional_mfe_bps": episode.directional_mfe_bps,
        "realized_mfe_after_cost_bps": episode.directional_mfe_bps - cost_bps,
    }


def prior_matched_controls(
    selected: list[dict[str, Any]],
    all_episodes: list[QualityEpisodeRecord],
    tapes: Mapping[str, PriceTape],
    *,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    control = config["controls"]
    event_times: dict[str, tuple[int, ...]] = defaultdict(tuple)
    grouped: dict[str, list[int]] = defaultdict(list)
    for episode in all_episodes:
        grouped[episode.symbol].append(_us(episode.quality_available_ts))
    event_times = {symbol: tuple(sorted(values)) for symbol, values in grouped.items()}
    pairs: list[dict[str, Any]] = []
    used: set[tuple[str, int]] = set()
    unavailable = 0
    by_episode = {episode.key: episode for episode in all_episodes}
    for row in selected:
        episode = by_episode[str(row["episode_key"])]
        tape = tapes.get(episode.symbol)
        event_ts = _us(episode.quality_available_ts)
        event_move = (
            _best_movement(
                tape,
                event_ts,
                delay_ms=int(control["entry_delay_ms"]),
                horizon_ms=int(control["horizon_ms"]),
            )
            if tape
            else None
        )
        event_vol = (
            _pre_volatility(tape, event_ts, int(control["pre_event_volatility_ms"]))
            if tape
            else None
        )
        if tape is None or event_move is None or event_vol is None:
            unavailable += 1
            continue
        matched: tuple[int, float, float] | None = None
        for offset in control["prior_offsets_ms"]:
            candidate = event_ts + int(offset) * 1_000
            outcome_known_at = (
                candidate + (int(control["entry_delay_ms"]) + int(control["horizon_ms"])) * 1_000
            )
            if outcome_known_at > event_ts or (episode.symbol, candidate) in used:
                continue
            if _session(candidate) != _session(event_ts):
                continue
            if any(
                abs(candidate - other) <= int(control["event_exclusion_ms"]) * 1_000
                for other in event_times[episode.symbol]
            ):
                continue
            candidate_vol = _pre_volatility(
                tape, candidate, int(control["pre_event_volatility_ms"])
            )
            candidate_move = _best_movement(
                tape,
                candidate,
                delay_ms=int(control["entry_delay_ms"]),
                horizon_ms=int(control["horizon_ms"]),
            )
            if candidate_vol is None or candidate_move is None:
                continue
            low, high = sorted((event_vol, candidate_vol))
            if low == 0 or high / low > float(control["maximum_volatility_ratio"]):
                continue
            matched = candidate, candidate_move, candidate_vol
            break
        if matched is None:
            unavailable += 1
            continue
        candidate, candidate_move, candidate_vol = matched
        used.add((episode.symbol, candidate))
        pairs.append(
            {
                "episode_key": episode.key,
                "symbol": episode.symbol,
                "session": _session(event_ts),
                "event_ts_us": event_ts,
                "control_ts_us": candidate,
                "control_outcome_known_at_us": candidate
                + (int(control["entry_delay_ms"]) + int(control["horizon_ms"])) * 1_000,
                "event_pre_volatility_bps": event_vol,
                "control_pre_volatility_bps": candidate_vol,
                "event_best_mfe_bps": event_move,
                "control_best_mfe_bps": candidate_move,
                "uplift_bps": event_move - candidate_move,
            }
        )
    count = len(pairs)
    uplift = sum(float(row["uplift_bps"]) for row in pairs) / count if count else 0.0
    win_rate = sum(float(row["uplift_bps"]) > 0 for row in pairs) / count if count else 0.0
    checks = {
        "minimum_pairs": count >= int(control["minimum_pairs"]),
        "positive_average_uplift": uplift > float(control["minimum_average_uplift_bps"]),
        "outperformance_rate": win_rate > float(control["minimum_outperformance_rate"]),
        "all_controls_precede_and_resolve": all(
            int(row["control_outcome_known_at_us"]) <= int(row["event_ts_us"]) for row in pairs
        ),
    }
    return {
        "method": "prior_symbol_session_prevol_direction_neutral_mfe",
        "selected_episodes": len(selected),
        "matched_pairs": count,
        "unavailable_pairs": unavailable,
        "average_uplift_bps": uplift,
        "event_outperformance_rate": win_rate,
        "checks": checks,
        "passed": all(checks.values()),
        "pairs": pairs,
    }


async def run_study(
    config_path: Path | str = DEFAULT_CONFIG,
    *,
    output_path: Path | str = DEFAULT_OUTPUT,
    refresh_candles: bool = False,
    code_version: str = "unknown",
) -> dict[str, Any]:
    config = load_config(config_path)
    source = config["source"]
    quality_payload, all_episodes = load_quality_episodes(source["event_quality_artifact"])
    symbols = {str(value).upper() for value in source["symbols"]}
    all_episodes = [row for row in all_episodes if row.symbol in symbols]
    quality_candidates = [
        row
        for row in all_episodes
        if row.market_truth_complete
        and row.abnormal_score_passed
        and row.abnormal_score >= float(config["quality"]["minimum_abnormal_score"])
    ]
    if not all_episodes:
        raise ValueError("Event Episode Quality artifact has no configured symbols")
    first = min(row.decision_ts for row in all_episodes)
    last = max(row.decision_ts for row in all_episodes)
    candle_start = first - timedelta(days=int(source["candle_history_days"]))
    candle_end = last + timedelta(minutes=1)
    snapshots: dict[str, LocationSnapshot] = {}
    candle_source: dict[str, Any] = {}
    for symbol in sorted(symbols):
        symbol_episodes = [row for row in quality_candidates if row.symbol == symbol]
        candles = await _load_candles(
            symbol,
            candle_start,
            candle_end,
            cache_dir=Path(source["candle_cache"]),
            refresh=refresh_candles,
        )
        candles = [row for row in candles if row.ts <= candle_end]
        snapshots.update(
            build_location_snapshots(candles, symbol_episodes, symbol=symbol, config=config)
        )
        candle_source[symbol] = {
            "one_minute_bars": len(candles),
            "missing_minutes": _missing_minutes(candles),
            "first_close": candles[0].ts.isoformat() if candles else None,
            "last_close": candles[-1].ts.isoformat() if candles else None,
        }
    cost = route_cost_contract(str(config["economics"]["cost_contract_id"]))
    rows = [
        qualify_episode(
            episode,
            snapshots.get(episode.key, _empty_location(episode, None, False)),
            config=config,
            cost_bps=float(cost.round_trip_cost_bps),
        )
        for episode in quality_candidates
    ]
    selected = [row for row in rows if row["selected"]]
    observations = [
        EventObservation(
            key=row.key,
            symbol=row.symbol,
            decision_ts_us=_us(row.decision_ts),
            reversal_direction=row.reversal_direction,
            strength=row.abnormal_score,
            volume_percentile=0.0,
            stacked=False,
        )
        for row in all_episodes
    ]
    tapes = dict(
        _load_tapes(
            Path(source["event_root"]),
            observations,
            int(config["controls"]["horizon_ms"]),
            int(config["controls"]["entry_delay_ms"]),
            code_version,
        )
    )
    controls = prior_matched_controls(selected, all_episodes, tapes, config=config)
    controls_passed = controls["passed"] is True
    fee_wall = (
        sum(
            float(row["realized_directional_mfe_bps"]) >= float(cost.round_trip_cost_bps)
            for row in selected
        )
        if controls_passed
        else 0
    )
    rejection_counts = Counter(reason for row in rows for reason in row["rejection_reasons"])
    funnel = [
        _stage("raw_events", "Raw events", len(all_episodes), len(all_episodes), "OBSERVED"),
        _stage(
            "market_truth_abnormal",
            "Market truth + abnormal",
            len(quality_candidates),
            len(all_episodes),
            "PASSED" if quality_candidates else "BLOCKED",
        ),
        _stage(
            "multi_level_location",
            "Multi-level causal location",
            sum(
                row["location"]["matched_pool_id"] is not None
                and row["location"]["merged_members"]
                >= int(config["location"]["minimum_merged_members"])
                for row in rows
            ),
            len(quality_candidates),
            "RESEARCH",
        ),
        _stage(
            "survival_and_direction",
            "Survival + reclaim/failure",
            sum(row["selected"] for row in rows),
            len(quality_candidates),
            "RESEARCH",
        ),
        _stage(
            "matched_control_uplift",
            "Earlier matched controls",
            controls["matched_pairs"],
            controls["selected_episodes"],
            "PASSED" if controls_passed else "BLOCKED",
        ),
        _stage(
            "cleared_fee_wall",
            "Cleared fee wall",
            fee_wall,
            len(selected),
            "RESEARCH" if controls_passed else "LOCKED_BY_CONTROL",
        ),
        _stage(
            "simulated_outcome",
            "Simulated outcome",
            0,
            fee_wall,
            "LOCKED_PENDING_PREREGISTERED_EXIT",
        ),
    ]
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "study_id": STUDY_ID,
        "generated_at": datetime.now(UTC).isoformat(),
        "code_version": code_version,
        "contract": config,
        "source": {
            "event_quality_artifact": str(source["event_quality_artifact"]),
            "event_quality_hash": quality_payload.get("deterministic_result_hash"),
            "event_quality_verdict": (quality_payload.get("diagnosis") or {}).get("verdict"),
            "candle_window": {
                "start": candle_start.isoformat(),
                "end": candle_end.isoformat(),
            },
            "candles": candle_source,
            "tape_trades": {symbol: len(tape.prices) for symbol, tape in sorted(tapes.items())},
        },
        "funnel": funnel,
        "rejection_diagnostics": dict(sorted(rejection_counts.items())),
        "matched_controls": controls,
        "qualified_episodes": rows,
        "selection": {
            "quality_candidates": len(quality_candidates),
            "selected_episodes": len(selected),
            "control_gate_passed": controls_passed,
            "exit_testing_authorized": controls_passed,
            "registry_enrollment_authorized": False,
        },
        "exit_research": {
            "status": "ELIGIBLE_NOT_RUN" if controls_passed else "LOCKED_BY_MATCHED_CONTROLS",
            "run": False,
        },
        "sealed_holdout": {"status": "SEALED", "opened": False, "scored": False},
        "amf_v3": {
            "evidence_consumed": False,
            "observations_added": 0,
            "separate_hypothesis": True,
        },
        "verdict": (
            "CONTROL_GATE_PASSED_EXIT_CONTRACT_REQUIRED"
            if controls_passed
            else "NO_QUALIFIED_CONTROL_UPLIFT_EXIT_RESEARCH_LOCKED"
        ),
        "research_only": True,
        "paper_authorized": False,
        "can_trade": False,
        "can_promote": False,
        "order_route": "absent",
    }
    report["deterministic_result_hash"] = _result_hash(report)
    _atomic_json(Path(output_path), report)
    return report


def _aggregate(candles: list[Candle], timeframes: tuple[str, ...]) -> dict[str, list[Candle]]:
    aggregator = ClosedCandleAggregator(timeframes)
    result = {timeframe: [] for timeframe in timeframes}
    previous: datetime | None = None
    for candle in sorted(candles, key=lambda row: row.ts):
        if previous is not None and candle.ts - previous != timedelta(minutes=1):
            aggregator = ClosedCandleAggregator(timeframes)
        previous = candle.ts
        for emitted in aggregator.on_one_minute("REPLAY", candle):
            result[emitted.tf].append(emitted)
    return result


def _with_volatility_buckets(candles: list[Candle]) -> list[tuple[Candle, str]]:
    if not candles:
        return []
    frame = pd.DataFrame(
        {
            "high": [row.high for row in candles],
            "low": [row.low for row in candles],
            "close": [row.close for row in candles],
        }
    )
    previous = frame["close"].shift(1)
    true_range = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - previous).abs(),
            (frame["low"] - previous).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = true_range.rolling(14, min_periods=14).mean()
    baseline = atr.shift(1).rolling(96, min_periods=48).median()
    ratio = atr / baseline
    buckets = [
        "unknown"
        if not math.isfinite(value)
        else "low"
        if value < 0.8
        else "high"
        if value > 1.2
        else "medium"
        for value in ratio.to_numpy(float)
    ]
    return list(zip(candles, buckets))


def _empty_location(
    episode: QualityEpisodeRecord,
    last_closed_ts: datetime | None,
    fresh: bool,
) -> LocationSnapshot:
    del last_closed_ts
    return LocationSnapshot(
        episode_key=episode.key,
        symbol=episode.symbol,
        available_at=episode.decision_ts,
        anchor_price=episode.anchor_price,
        reversal_direction=episode.reversal_direction,
        matched_pool_id=None,
        pool_side=None,
        pool_price=None,
        merged_members=0,
        scales=(),
        age_bars_15m=0,
        touches=0,
        survival_probability=0.5,
        survival_resolved_interactions=0,
        survival_source="unavailable",
        target_pool_id=None,
        target_price=None,
        target_room_bps=None,
        invalidation_bps=None,
        location_fresh=fresh,
    )


def _direction(episode: QualityEpisodeRecord) -> int:
    if episode.direction_state == "reversal_reclaim":
        return episode.reversal_direction
    if episode.direction_state == "continuation_failure":
        return -episode.reversal_direction
    return 0


def _anchor_fields(row: Mapping[str, Any]) -> tuple[float, int]:
    if row.get("anchor_price") is not None and row.get("anchor_reversal_direction") is not None:
        price = float(row["anchor_price"])
        direction = int(row["anchor_reversal_direction"])
    else:
        # Backward-compatible migration for the already-published v1 artifact.
        # Newly generated evidence always emits explicit fields.
        parts = str(row.get("episode_key") or "").rsplit(":", 2)
        if len(parts) != 3:
            raise ValueError("quality episode lacks explicit anchor fields")
        direction = int(parts[1])
        price = float(parts[2])
    if price <= 0 or direction not in {-1, 1}:
        raise ValueError("invalid quality episode anchor")
    return price, direction


def _best_movement(
    tape: PriceTape | None, ts_us: int, *, delay_ms: int, horizon_ms: int
) -> float | None:
    if tape is None:
        return None
    start = tape.first_at_or_after(ts_us + delay_ms * 1_000)
    if start is None:
        return None
    end = tape.first_at_or_after(tape.timestamps_us[start] + horizon_ms * 1_000)
    if end is None:
        return None
    entry = tape.prices[start]
    long_mfe, _, _ = tape.directional_path_stats(start, end + 1, direction=1, entry_price=entry)
    short_mfe, _, _ = tape.directional_path_stats(start, end + 1, direction=-1, entry_price=entry)
    return max(long_mfe, short_mfe)


def _pre_volatility(tape: PriceTape | None, ts_us: int, window_ms: int) -> float | None:
    if tape is None:
        return None
    start = tape.first_at_or_after(ts_us - window_ms * 1_000)
    end = tape.first_at_or_after(ts_us)
    if start is None or end is None or end <= start:
        return None
    prices = tape.prices[start : end + 1]
    return (max(prices) / min(prices) - 1.0) * 10_000.0


def _session(ts_us: int) -> str:
    hour = datetime.fromtimestamp(ts_us / 1_000_000, tz=UTC).hour
    if hour < 8:
        return "asia"
    if hour < 13:
        return "london"
    if hour < 21:
        return "new_york"
    return "late_utc"


def _bucket(value: int, boundaries: tuple[int, ...]) -> str:
    for boundary in boundaries:
        if value <= boundary:
            return f"le_{boundary}"
    return f"gt_{boundaries[-1]}"


def _distance_bps(first: float, second: float) -> float:
    return abs(first / second - 1.0) * 10_000.0


def _parse_ts(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _us(value: datetime) -> int:
    return int(value.timestamp() * 1_000_000)


def _missing_minutes(candles: list[Candle]) -> int:
    return sum(
        max(0, int((current.ts - previous.ts).total_seconds() // 60) - 1)
        for previous, current in pairwise(candles)
    )


def _stage(stage_id: str, label: str, value: int, target: int, state: str) -> dict[str, Any]:
    return {"id": stage_id, "label": label, "value": value, "target": target, "state": state}


def _result_hash(payload: Mapping[str, Any]) -> str:
    stable = {
        key: payload[key]
        for key in (
            "study_id",
            "code_version",
            "contract",
            "source",
            "funnel",
            "rejection_diagnostics",
            "matched_controls",
            "qualified_episodes",
            "selection",
            "exit_research",
            "sealed_holdout",
            "amf_v3",
            "verdict",
        )
    }
    encoded = json.dumps(stable, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, default=str, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--refresh-candles", action="store_true")
    parser.add_argument("--code-version", default="local-research")
    args = parser.parse_args(argv)
    report = asyncio.run(
        run_study(
            args.config,
            output_path=args.output,
            refresh_candles=args.refresh_candles,
            code_version=args.code_version,
        )
    )
    print(
        json.dumps(
            {
                "study_id": report["study_id"],
                "funnel": report["funnel"],
                "matched_controls": report["matched_controls"],
                "verdict": report["verdict"],
                "can_trade": False,
                "can_promote": False,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
