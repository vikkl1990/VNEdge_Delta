"""Causal, level-aware absorption detection for verified Delta event tape.

The detector never infers queue identity or hidden orders.  It measures the
observable proxy we can defend from public L2 + aggressor trades: meaningful
aggressive volume repeatedly trades at a level, displayed capacity holds or
replenishes, the mid-price remains inside a tick tolerance, and dominant flow
then pauses or reverses.  Results are research observations, not orders.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
from math import log1p
from statistics import fmean
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vnedge.exchange.delta_contracts import DeltaContractSpec

AggressorSide = Literal["buy", "sell"]
BookSide = Literal["bid", "ask"]
AbsorbedSide = Literal["sell_limits_absorbing_buys", "buy_limits_absorbing_sells"]


class AbsorptionInstrumentConfig(BaseModel):
    """Venue metadata + an explicit economic volume floor for one market."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    tick_size: float = Field(gt=0)
    contract_value: float = Field(default=1.0, gt=0)
    minimum_aggressive_notional_usd: float = Field(gt=0)

    @model_validator(mode="after")
    def normalize_symbol(self) -> AbsorptionInstrumentConfig:
        native = self.symbol.upper().strip()
        if not native:
            raise ValueError("absorption instrument symbol is required")
        object.__setattr__(self, "symbol", native)
        return self

    @classmethod
    def from_delta_contract(
        cls,
        spec: DeltaContractSpec,
        *,
        minimum_aggressive_notional_usd: float,
    ) -> AbsorptionInstrumentConfig:
        """Use exchange product metadata; never guess an instrument tick."""

        if spec.tick_size is None or spec.tick_size <= 0:
            raise ValueError("Delta contract spec has no valid tick_size")
        return cls(
            symbol=spec.symbol,
            tick_size=spec.tick_size,
            contract_value=spec.contract_value,
            minimum_aggressive_notional_usd=minimum_aggressive_notional_usd,
        )


class AbsorptionDetectorConfig(BaseModel):
    """Frozen v1 detector policy; empty instruments means fail-closed/off."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    window_ms: int = Field(default=1_200, ge=200, le=10_000)
    minimum_duration_ms: int = Field(default=100, ge=0, le=5_000)
    exhaustion_ms: int = Field(default=200, ge=50, le=5_000)
    tolerance_ticks: float = Field(default=2.0, gt=0, le=20)
    minimum_absorption_ratio: float = Field(default=0.70, ge=0, le=1)
    minimum_resting_hold_ratio: float = Field(default=0.40, ge=0, le=1)
    dominance_ratio: float = Field(default=1.50, gt=1)
    opposing_flow_ratio: float = Field(default=0.15, ge=0, le=1)
    relative_trade_multiple: float = Field(default=3.0, ge=0)
    baseline_window_ms: int = Field(default=60_000, ge=1_000, le=600_000)
    freshness_ms: int = Field(default=3_000, ge=100, le=30_000)
    price_level_cooldown_ms: int = Field(default=20_000, ge=0, le=600_000)
    maximum_active_levels: int = Field(default=20, ge=1, le=200)
    stacked_enabled: bool = True
    stacked_band_ticks: int = Field(default=4, ge=1, le=20)
    stacked_minimum_levels: int = Field(default=2, ge=2, le=10)
    stacked_notional_multiplier: float = Field(default=1.5, ge=1, le=10)
    liquidation_max_distance_ticks: float = Field(default=40.0, gt=0, le=1_000)
    liquidation_cluster_ttl_ms: int = Field(default=60_000, ge=1_000, le=3_600_000)
    maximum_liquidation_clusters: int = Field(default=50, ge=1, le=1_000)
    timeline_size: int = Field(default=50, ge=1, le=500)
    instruments: tuple[AbsorptionInstrumentConfig, ...] = ()

    @model_validator(mode="after")
    def validate_windows_and_instruments(self) -> AbsorptionDetectorConfig:
        if self.minimum_duration_ms >= self.window_ms:
            raise ValueError("absorption minimum duration must be below its window")
        symbols = [row.symbol for row in self.instruments]
        if len(symbols) != len(set(symbols)):
            raise ValueError("absorption instruments must be unique")
        return self

    def instrument(self, symbol: str) -> AbsorptionInstrumentConfig | None:
        native = symbol.upper()
        return next((row for row in self.instruments if row.symbol == native), None)


@dataclass(frozen=True)
class AbsorptionObservation:
    symbol: str
    price: float
    absorbed_side: AbsorbedSide
    reversal_direction: int
    aggressive_buy_volume: float
    aggressive_sell_volume: float
    aggressive_notional_usd: float
    dynamic_minimum_notional_usd: float
    duration_ms: float
    price_range_ticks: float
    absorption_ratio: float
    resting_hold_ratio: float
    replenishment_ratio: float
    refresh_count: int
    strength: float
    detected_monotonic_ns: int
    age_ms: float = 0.0
    is_stacked: bool = False
    stacked_levels: tuple[float, ...] = ()
    liquidation_distance_ticks: float | None = None
    liquidation_cluster_side: str | None = None
    liquidation_cluster_size: float | None = None
    liquidation_relation: str = "unavailable"
    research_only: bool = True
    used_for_execution: bool = False

    def __post_init__(self) -> None:
        if self.reversal_direction not in {-1, 1}:
            raise ValueError("absorption reversal direction must be -1 or 1")
        for value in (
            self.absorption_ratio,
            self.resting_hold_ratio,
            self.replenishment_ratio,
            self.strength,
        ):
            if not 0 <= value <= 1:
                raise ValueError("absorption ratios and strength must be in [0, 1]")
        if not self.research_only or self.used_for_execution:
            raise ValueError("absorption observation must remain research-only")
        object.__setattr__(self, "stacked_levels", tuple(self.stacked_levels))
        if self.liquidation_relation not in {
            "unavailable",
            "nearby_long_liquidations",
            "nearby_short_liquidations",
        }:
            raise ValueError("invalid liquidation relation")

    def with_age(self, now_ns: int) -> AbsorptionObservation:
        age = max(0.0, (now_ns - self.detected_monotonic_ns) / 1_000_000.0)
        return AbsorptionObservation(**{**self.__dict__, "age_ms": age})

    def to_dict(self) -> dict[str, object]:
        return {**self.__dict__, "stacked_levels": list(self.stacked_levels)}


@dataclass(frozen=True)
class LiquidationCluster:
    price: float
    size: float
    liquidated_side: Literal["long", "short"]
    observed_monotonic_ns: int

    def __post_init__(self) -> None:
        if self.price <= 0 or self.size <= 0 or self.observed_monotonic_ns < 0:
            raise ValueError("invalid liquidation cluster")


@dataclass(frozen=True)
class FootprintLevel:
    price: float
    bid_size: float
    ask_size: float
    aggressive_buy_volume: float
    aggressive_sell_volume: float
    delta: float
    absorption_marker: str | None

    def to_dict(self) -> dict[str, object]:
        return self.__dict__.copy()


@dataclass
class _LevelStats:
    price: float
    resting_side: BookSide
    first_hit_ns: int
    last_hit_ns: int
    last_buy_hit_ns: int | None
    last_sell_hit_ns: int | None
    aggressive_buy_volume: float
    aggressive_sell_volume: float
    aggressive_notional_usd: float
    initial_resting_size: float
    max_resting_size: float
    current_resting_size: float
    replenished_size: float
    refresh_count: int
    price_high: float
    price_low: float


class AbsorptionDetector:
    """Bounded per-symbol state machine for classic displayed absorption."""

    def __init__(
        self,
        instrument: AbsorptionInstrumentConfig,
        config: AbsorptionDetectorConfig,
    ) -> None:
        self.instrument = instrument
        self.config = config
        self._levels: dict[tuple[float, BookSide], _LevelStats] = {}
        self._recent_trades: deque[tuple[int, float, AggressorSide, float]] = deque()
        self._cooldowns: dict[tuple[float, AbsorbedSide], int] = {}
        self._liquidations: deque[LiquidationCluster] = deque(
            maxlen=config.maximum_liquidation_clusters
        )
        self._timeline: deque[AbsorptionObservation] = deque(maxlen=config.timeline_size)
        self._latest: AbsorptionObservation | None = None

    def reset(self) -> None:
        self._levels.clear()
        self._recent_trades.clear()
        self._latest = None

    def reset_session_map(self) -> None:
        """Start a new dashboard session without disturbing hot detector state."""

        self._timeline.clear()

    def on_liquidation(
        self,
        *,
        price: float,
        size: float,
        liquidated_side: Literal["long", "short"],
        now_ns: int,
    ) -> None:
        self._prune(now_ns)
        self._liquidations.append(
            LiquidationCluster(
                price=price,
                size=size,
                liquidated_side=liquidated_side,
                observed_monotonic_ns=now_ns,
            )
        )

    def on_trade(
        self,
        *,
        price: float,
        size: float,
        notional_usd: float,
        side: AggressorSide,
        current_resting_size: float,
        mid: float,
        now_ns: int,
    ) -> AbsorptionObservation | None:
        self._prune(now_ns)
        self._recent_trades.append((now_ns, notional_usd, side, size))
        resting_side: BookSide = "ask" if side == "buy" else "bid"
        key = (price, resting_side)
        stats = self._levels.get(key)
        if stats is None:
            stats = _LevelStats(
                price=price,
                resting_side=resting_side,
                first_hit_ns=now_ns,
                last_hit_ns=now_ns,
                last_buy_hit_ns=None,
                last_sell_hit_ns=None,
                aggressive_buy_volume=0.0,
                aggressive_sell_volume=0.0,
                aggressive_notional_usd=0.0,
                initial_resting_size=current_resting_size,
                max_resting_size=current_resting_size,
                current_resting_size=current_resting_size,
                replenished_size=0.0,
                refresh_count=0,
                price_high=mid,
                price_low=mid,
            )
            self._levels[key] = stats
            self._bound_levels()
        stats.last_hit_ns = now_ns
        stats.price_high = max(stats.price_high, mid)
        stats.price_low = min(stats.price_low, mid)
        stats.current_resting_size = current_resting_size
        stats.max_resting_size = max(stats.max_resting_size, current_resting_size)
        stats.aggressive_notional_usd += notional_usd
        if side == "buy":
            stats.aggressive_buy_volume += size
            stats.last_buy_hit_ns = now_ns
        else:
            stats.aggressive_sell_volume += size
            stats.last_sell_hit_ns = now_ns
        return self._evaluate_all(mid=mid, now_ns=now_ns)

    def on_book_delta(
        self,
        *,
        side: BookSide,
        price: float,
        previous_size: float,
        current_size: float,
        mid: float,
        now_ns: int,
    ) -> AbsorptionObservation | None:
        self._prune(now_ns)
        stats = self._levels.get((price, side))
        if stats is not None:
            if current_size > previous_size:
                stats.replenished_size += current_size - previous_size
                stats.refresh_count += 1
            stats.current_resting_size = current_size
            stats.max_resting_size = max(stats.max_resting_size, current_size)
            stats.price_high = max(stats.price_high, mid)
            stats.price_low = min(stats.price_low, mid)
        return self._evaluate_all(mid=mid, now_ns=now_ns)

    def on_time(self, *, mid: float, now_ns: int) -> AbsorptionObservation | None:
        self._prune(now_ns)
        return self._evaluate_all(mid=mid, now_ns=now_ns)

    def latest(self, now_ns: int) -> AbsorptionObservation | None:
        latest = self._latest
        if latest is None:
            return None
        aged = latest.with_age(now_ns)
        return aged if aged.age_ms <= self.config.freshness_ms else None

    def timeline(self, now_ns: int, *, limit: int = 12) -> tuple[AbsorptionObservation, ...]:
        if limit < 1:
            raise ValueError("timeline limit must be positive")
        rows = [row.with_age(now_ns) for row in self._timeline]
        return tuple(rows[-limit:])

    def footprint(
        self,
        *,
        bids: tuple[tuple[float, float], ...],
        asks: tuple[tuple[float, float], ...],
        limit: int = 40,
    ) -> tuple[FootprintLevel, ...]:
        if limit < 1:
            raise ValueError("footprint limit must be positive")
        bid_map = dict(bids)
        ask_map = dict(asks)
        prices = sorted(set(bid_map) | set(ask_map), reverse=True)[:limit]
        latest = self._latest
        stacked = set(latest.stacked_levels) if latest and latest.is_stacked else set()
        rows: list[FootprintLevel] = []
        for price in prices:
            bid_stats = self._levels.get((price, "bid"))
            ask_stats = self._levels.get((price, "ask"))
            buy = ask_stats.aggressive_buy_volume if ask_stats else 0.0
            sell = bid_stats.aggressive_sell_volume if bid_stats else 0.0
            marker: str | None = None
            if price in stacked:
                marker = "stacked"
            elif latest is not None and price == latest.price:
                marker = "support" if latest.reversal_direction > 0 else "resistance"
            rows.append(
                FootprintLevel(
                    price=price,
                    bid_size=bid_map.get(price, 0.0),
                    ask_size=ask_map.get(price, 0.0),
                    aggressive_buy_volume=buy,
                    aggressive_sell_volume=sell,
                    delta=buy - sell,
                    absorption_marker=marker,
                )
            )
        return tuple(rows)

    def dashboard_snapshot(
        self,
        *,
        bids: tuple[tuple[float, float], ...],
        asks: tuple[tuple[float, float], ...],
        now_ns: int,
    ) -> dict[str, object]:
        latest = self.latest(now_ns)
        timeline = self.timeline(now_ns)
        session_levels: dict[float, dict[str, float | int]] = {}
        for row in self._timeline:
            bucket = session_levels.setdefault(
                row.price,
                {"support": 0, "resistance": 0, "max_strength": 0.0},
            )
            side = "support" if row.reversal_direction > 0 else "resistance"
            bucket[side] = int(bucket[side]) + 1
            bucket["max_strength"] = max(float(bucket["max_strength"]), row.strength)
        return {
            "schema_version": "vnedge.absorption_dashboard.v1",
            "latest": latest.to_dict() if latest else None,
            "strength_gauge": round((latest.strength if latest else 0.0) * 100, 2),
            "timeline": [row.to_dict() for row in timeline],
            "footprint": [
                row.to_dict() for row in self.footprint(bids=bids, asks=asks)
            ],
            "session_map": [
                {"price": price, **metrics}
                for price, metrics in sorted(session_levels.items(), reverse=True)
            ],
            "session_map_scope": f"bounded_last_{self.config.timeline_size}_events",
            "liquidation_strength_applied_to_signal": False,
            "research_only": True,
            "can_trade": False,
        }

    def _evaluate_all(self, *, mid: float, now_ns: int) -> AbsorptionObservation | None:
        newest: AbsorptionObservation | None = None
        for stats in tuple(self._levels.values()):
            stats.price_high = max(stats.price_high, mid)
            stats.price_low = min(stats.price_low, mid)
            observation = self._evaluate(stats, now_ns=now_ns)
            if observation is not None:
                newest = observation
        if newest is None and self.config.stacked_enabled:
            newest = self._evaluate_stacked(now_ns=now_ns)
        if newest is not None:
            self._latest = newest
            self._timeline.append(newest)
        return newest

    def _evaluate(self, stats: _LevelStats, *, now_ns: int) -> AbsorptionObservation | None:
        duration_ms = (stats.last_hit_ns - stats.first_hit_ns) / 1_000_000.0
        if duration_ms < self.config.minimum_duration_ms or duration_ms > self.config.window_ms:
            return None
        dynamic_minimum = self._dynamic_minimum_notional()
        if stats.aggressive_notional_usd < dynamic_minimum:
            return None
        buy = stats.aggressive_buy_volume
        sell = stats.aggressive_sell_volume
        if buy > sell * self.config.dominance_ratio:
            absorbed_side: AbsorbedSide = "sell_limits_absorbing_buys"
            reversal_direction = -1
            dominant = buy
            last_dominant_ns = stats.last_buy_hit_ns
            opposing_side: AggressorSide = "sell"
        elif sell > buy * self.config.dominance_ratio:
            absorbed_side = "buy_limits_absorbing_sells"
            reversal_direction = 1
            dominant = sell
            last_dominant_ns = stats.last_sell_hit_ns
            opposing_side = "buy"
        else:
            return None
        if dominant <= 0 or last_dominant_ns is None:
            return None
        flow_dried = now_ns - last_dominant_ns >= self.config.exhaustion_ms * 1_000_000
        opposing_after = sum(
            size
            for ts_ns, _, side, size in self._recent_trades
            if ts_ns > last_dominant_ns and side == opposing_side
        )
        flow_reversed = opposing_after / dominant >= self.config.opposing_flow_ratio
        if not flow_dried and not flow_reversed:
            return None
        range_ticks = (stats.price_high - stats.price_low) / self.instrument.tick_size
        if range_ticks > self.config.tolerance_ticks:
            return None
        # Initial displayed size plus positive replenishment is the total
        # observable passive capacity. max_size + replenishment double-counts
        # simple book growth and artificially saturates this ratio.
        displayed_capacity = stats.initial_resting_size + stats.replenished_size
        absorption_ratio = min(1.0, displayed_capacity / dominant) if dominant else 0.0
        if absorption_ratio < self.config.minimum_absorption_ratio:
            return None
        hold_ratio = (
            min(1.0, stats.current_resting_size / stats.max_resting_size)
            if stats.max_resting_size > 0
            else 0.0
        )
        if hold_ratio < self.config.minimum_resting_hold_ratio:
            return None
        replenishment_ratio = min(1.0, stats.replenished_size / dominant) if dominant else 0.0
        cooldown_key = (stats.price, absorbed_side)
        last_emit = self._cooldowns.get(cooldown_key)
        if last_emit is not None and (
            now_ns - last_emit < self.config.price_level_cooldown_ms * 1_000_000
        ):
            return None
        volume_score = min(1.0, log1p(stats.aggressive_notional_usd / dynamic_minimum) / log1p(3))
        stability_score = max(0.0, 1.0 - range_ticks / self.config.tolerance_ticks)
        strength = min(
            1.0,
            volume_score * 0.40
            + stability_score * 0.30
            + max(hold_ratio, replenishment_ratio) * 0.20
            + 0.10,
        )
        observation = AbsorptionObservation(
            symbol=self.instrument.symbol,
            price=stats.price,
            absorbed_side=absorbed_side,
            reversal_direction=reversal_direction,
            aggressive_buy_volume=buy,
            aggressive_sell_volume=sell,
            aggressive_notional_usd=stats.aggressive_notional_usd,
            dynamic_minimum_notional_usd=dynamic_minimum,
            duration_ms=duration_ms,
            price_range_ticks=range_ticks,
            absorption_ratio=absorption_ratio,
            resting_hold_ratio=hold_ratio,
            replenishment_ratio=replenishment_ratio,
            refresh_count=stats.refresh_count,
            strength=strength,
            detected_monotonic_ns=now_ns,
        )
        self._cooldowns[cooldown_key] = now_ns
        return self._enrich_liquidation(observation, now_ns=now_ns)

    def _evaluate_stacked(self, *, now_ns: int) -> AbsorptionObservation | None:
        candidates: list[AbsorptionObservation] = []
        for resting_side in ("bid", "ask"):
            rows = sorted(
                (
                    stats
                    for stats in self._levels.values()
                    if stats.resting_side == resting_side
                ),
                key=lambda stats: stats.price,
            )
            bands: list[list[_LevelStats]] = []
            current: list[_LevelStats] = []
            for stats in rows:
                if not current:
                    current = [stats]
                elif (
                    stats.price - current[0].price
                ) / self.instrument.tick_size <= self.config.stacked_band_ticks:
                    current.append(stats)
                else:
                    if len(current) >= self.config.stacked_minimum_levels:
                        bands.append(current)
                    current = [stats]
            if len(current) >= self.config.stacked_minimum_levels:
                bands.append(current)
            for band in bands:
                observation = self._evaluate_band(band, now_ns=now_ns)
                if observation is not None:
                    candidates.append(observation)
        if not candidates:
            return None
        return max(candidates, key=lambda row: row.strength)

    def _evaluate_band(
        self,
        band: list[_LevelStats],
        *,
        now_ns: int,
    ) -> AbsorptionObservation | None:
        first_hit_ns = min(stats.first_hit_ns for stats in band)
        last_hit_ns = max(stats.last_hit_ns for stats in band)
        duration_ms = (last_hit_ns - first_hit_ns) / 1_000_000.0
        if duration_ms < self.config.minimum_duration_ms or duration_ms > self.config.window_ms:
            return None
        dynamic_minimum = self._dynamic_minimum_notional()
        total_notional = sum(stats.aggressive_notional_usd for stats in band)
        if total_notional < dynamic_minimum * self.config.stacked_notional_multiplier:
            return None
        buy = sum(stats.aggressive_buy_volume for stats in band)
        sell = sum(stats.aggressive_sell_volume for stats in band)
        if buy > sell * self.config.dominance_ratio:
            absorbed_side: AbsorbedSide = "sell_limits_absorbing_buys"
            reversal_direction = -1
            dominant = buy
            last_dominant_ns = max(
                stats.last_buy_hit_ns or 0 for stats in band
            )
            opposing_side: AggressorSide = "sell"
        elif sell > buy * self.config.dominance_ratio:
            absorbed_side = "buy_limits_absorbing_sells"
            reversal_direction = 1
            dominant = sell
            last_dominant_ns = max(
                stats.last_sell_hit_ns or 0 for stats in band
            )
            opposing_side = "buy"
        else:
            return None
        if dominant <= 0 or last_dominant_ns <= 0:
            return None
        flow_dried = now_ns - last_dominant_ns >= self.config.exhaustion_ms * 1_000_000
        opposing_after = sum(
            size
            for ts_ns, _, side, size in self._recent_trades
            if ts_ns > last_dominant_ns and side == opposing_side
        )
        if not flow_dried and opposing_after / dominant < self.config.opposing_flow_ratio:
            return None
        price_high = max(stats.price_high for stats in band)
        price_low = min(stats.price_low for stats in band)
        range_ticks = (price_high - price_low) / self.instrument.tick_size
        if range_ticks > self.config.stacked_band_ticks + 1:
            return None
        displayed_capacity = sum(
            stats.initial_resting_size + stats.replenished_size for stats in band
        )
        absorption_ratio = min(1.0, displayed_capacity / dominant) if dominant else 0.0
        if absorption_ratio < self.config.minimum_absorption_ratio:
            return None
        total_max_resting = sum(stats.max_resting_size for stats in band)
        total_current_resting = sum(stats.current_resting_size for stats in band)
        hold_ratio = (
            min(1.0, total_current_resting / total_max_resting)
            if total_max_resting > 0
            else 0.0
        )
        if hold_ratio < self.config.minimum_resting_hold_ratio:
            return None
        replenished = sum(stats.replenished_size for stats in band)
        replenishment_ratio = min(1.0, replenished / dominant) if dominant else 0.0
        levels = tuple(stats.price for stats in band)
        center = sum(levels) / len(levels)
        cooldown_key = (center, absorbed_side)
        last_emit = self._cooldowns.get(cooldown_key)
        if last_emit is not None and (
            now_ns - last_emit < self.config.price_level_cooldown_ms * 1_000_000
        ):
            return None
        volume_score = min(
            1.0,
            log1p(total_notional / (dynamic_minimum * self.config.stacked_notional_multiplier))
            / log1p(3),
        )
        stability_score = max(
            0.0,
            1.0 - range_ticks / (self.config.stacked_band_ticks + 1),
        )
        level_score = min(1.0, len(levels) / self.config.stacked_band_ticks)
        strength = min(
            1.0,
            volume_score * 0.35
            + stability_score * 0.25
            + max(hold_ratio, replenishment_ratio) * 0.20
            + level_score * 0.20,
        )
        observation = AbsorptionObservation(
            symbol=self.instrument.symbol,
            price=center,
            absorbed_side=absorbed_side,
            reversal_direction=reversal_direction,
            aggressive_buy_volume=buy,
            aggressive_sell_volume=sell,
            aggressive_notional_usd=total_notional,
            dynamic_minimum_notional_usd=(
                dynamic_minimum * self.config.stacked_notional_multiplier
            ),
            duration_ms=duration_ms,
            price_range_ticks=range_ticks,
            absorption_ratio=absorption_ratio,
            resting_hold_ratio=hold_ratio,
            replenishment_ratio=replenishment_ratio,
            refresh_count=sum(stats.refresh_count for stats in band),
            strength=strength,
            detected_monotonic_ns=now_ns,
            is_stacked=True,
            stacked_levels=levels,
        )
        self._cooldowns[cooldown_key] = now_ns
        return self._enrich_liquidation(observation, now_ns=now_ns)

    def _enrich_liquidation(
        self,
        observation: AbsorptionObservation,
        *,
        now_ns: int,
    ) -> AbsorptionObservation:
        cutoff = now_ns - self.config.liquidation_cluster_ttl_ms * 1_000_000
        nearby = [
            cluster
            for cluster in self._liquidations
            if cluster.observed_monotonic_ns >= cutoff
            and abs(cluster.price - observation.price) / self.instrument.tick_size
            <= self.config.liquidation_max_distance_ticks
        ]
        if not nearby:
            return observation
        cluster = min(
            nearby,
            key=lambda row: abs(row.price - observation.price),
        )
        distance = abs(cluster.price - observation.price) / self.instrument.tick_size
        relation = (
            "nearby_long_liquidations"
            if cluster.liquidated_side == "long"
            else "nearby_short_liquidations"
        )
        # This is metadata only.  No strength/probability multiplier is
        # permitted until untouched event outcomes prove incremental value.
        return replace(
            observation,
            liquidation_distance_ticks=distance,
            liquidation_cluster_side=cluster.liquidated_side,
            liquidation_cluster_size=cluster.size,
            liquidation_relation=relation,
        )

    def _dynamic_minimum_notional(self) -> float:
        recent_average = fmean(value for _, value, _, _ in self._recent_trades)
        relative = recent_average * self.config.relative_trade_multiple
        return max(self.instrument.minimum_aggressive_notional_usd, relative)

    def _prune(self, now_ns: int) -> None:
        level_cutoff = now_ns - self.config.window_ms * 1_000_000
        for key, stats in tuple(self._levels.items()):
            if stats.first_hit_ns < level_cutoff:
                self._levels.pop(key, None)
        baseline_cutoff = now_ns - self.config.baseline_window_ms * 1_000_000
        while self._recent_trades and self._recent_trades[0][0] < baseline_cutoff:
            self._recent_trades.popleft()
        liquidation_cutoff = now_ns - self.config.liquidation_cluster_ttl_ms * 1_000_000
        while (
            self._liquidations
            and self._liquidations[0].observed_monotonic_ns < liquidation_cutoff
        ):
            self._liquidations.popleft()
        cooldown_cutoff = now_ns - self.config.price_level_cooldown_ms * 1_000_000
        for key, emitted_ns in tuple(self._cooldowns.items()):
            if emitted_ns < cooldown_cutoff:
                self._cooldowns.pop(key, None)

    def _bound_levels(self) -> None:
        excess = len(self._levels) - self.config.maximum_active_levels
        if excess <= 0:
            return
        oldest = sorted(self._levels, key=lambda key: self._levels[key].last_hit_ns)[:excess]
        for key in oldest:
            self._levels.pop(key, None)
