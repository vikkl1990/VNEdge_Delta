"""Immutable domain contracts for the Delta scalper engine."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from types import MappingProxyType


class Side(str, Enum):
    LONG = "long"
    SHORT = "short"


class Regime(str, Enum):
    QUIET = "quiet"
    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    EXPANDING = "expanding"
    FUNDING_EXTREME = "funding_extreme"
    UNKNOWN = "unknown"


class TrendStrength(str, Enum):
    STRONG_TREND = "strong_trend"
    WEAK_TREND = "weak_trend"
    RANGE = "range"
    UNKNOWN = "unknown"


class VolatilityRegime(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"


class SessionRegime(str, Enum):
    ASIA = "asia"
    EUROPE = "europe"
    OVERLAP = "overlap"
    US = "us"


@dataclass(frozen=True)
class ChangePointProfile:
    """Causal sequential shift state; observational until separately validated."""

    source_timeframe: str = "unavailable"
    detector_ready: bool = False
    regime_shift: bool = False
    return_shift: bool = False
    volatility_shift: bool = False
    return_score: float = 0.0
    volatility_score: float = 0.0
    bars_since_shift: int | None = None
    minutes_since_shift: int | None = None

    @property
    def shift_window(self) -> str:
        if self.minutes_since_shift is None:
            return "no_prior_shift"
        if self.minutes_since_shift <= 30:
            return "00-30m"
        if self.minutes_since_shift <= 60:
            return "30-60m"
        if self.minutes_since_shift <= 240:
            return "01-04h"
        return "04h+"

    def to_dict(self) -> dict[str, object]:
        return {
            **self.__dict__,
            "shift_window": self.shift_window,
            "research_only": True,
            "used_for_signal": False,
            "used_for_execution": False,
        }


def _utc(ts: datetime) -> datetime:
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)


@dataclass(frozen=True)
class Candle:
    """One proven-closed candle; ``ts`` is its close timestamp."""

    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    tf: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts", _utc(self.ts))
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("candle prices must be positive")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("invalid candle OHLC ordering")
        if self.volume < 0:
            raise ValueError("candle volume cannot be negative")
        if not self.tf:
            raise ValueError("candle timeframe is required")

    @property
    def range(self) -> float:
        return self.high - self.low


@dataclass(frozen=True)
class L2Confirmation:
    """Optional context. It is explicitly forbidden from becoming a trigger."""

    imbalance: float = 0.0
    cvd: float = 0.0
    imbalance_z: float = 0.0
    buy_aggression_ratio: float = 0.5
    absorption_score: float = 0.0
    depth_usd: float = 0.0
    sequence_healthy: bool | None = None
    status: str = "unavailable"
    observed_at: datetime | None = None
    context_only: bool = True
    used_for_signal: bool = False
    used_for_execution: bool = False

    def __post_init__(self) -> None:
        if not -1.0 <= self.imbalance <= 1.0:
            raise ValueError("L2 imbalance must be between -1 and 1")
        if not 0.0 <= self.buy_aggression_ratio <= 1.0:
            raise ValueError("buy aggression ratio must be in [0, 1]")
        if not 0.0 <= self.absorption_score <= 1.0:
            raise ValueError("absorption score must be in [0, 1]")
        if self.depth_usd < 0:
            raise ValueError("depth_usd cannot be negative")
        if self.observed_at is not None:
            object.__setattr__(self, "observed_at", _utc(self.observed_at))
        if not self.context_only or self.used_for_signal or self.used_for_execution:
            raise ValueError("L2 may only be attached as non-triggering confirmation")


@dataclass(frozen=True)
class RegimeProfile:
    """Orthogonal causal labels attached for research attribution."""

    trend: TrendStrength = TrendStrength.UNKNOWN
    trend_direction: str = "flat"
    volatility: VolatilityRegime = VolatilityRegime.UNKNOWN
    session: SessionRegime = SessionRegime.ASIA
    source_timeframe: str = "unavailable"
    flags: Mapping[str, bool] = field(default_factory=dict)
    metrics: Mapping[str, float] = field(default_factory=dict)
    change_point: ChangePointProfile = ChangePointProfile()

    def __post_init__(self) -> None:
        if self.trend_direction not in {"up", "down", "flat"}:
            raise ValueError("trend_direction must be up, down, or flat")
        object.__setattr__(self, "flags", MappingProxyType(dict(self.flags)))
        object.__setattr__(self, "metrics", MappingProxyType(dict(self.metrics)))

    def to_dict(self) -> dict[str, object]:
        return {
            "trend": self.trend.value,
            "trend_direction": self.trend_direction,
            "volatility": self.volatility.value,
            "session": self.session.value,
            "source_timeframe": self.source_timeframe,
            "flags": dict(self.flags),
            "metrics": dict(self.metrics),
            "change_point": self.change_point.to_dict(),
        }


@dataclass(frozen=True)
class MarketContext:
    symbol: str
    ts: datetime
    candles: Mapping[str, tuple[Candle, ...]]
    regime: Regime
    funding_rate: float
    funding_velocity: float
    l2: L2Confirmation = L2Confirmation()
    regime_profile: RegimeProfile = RegimeProfile()
    features: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts", _utc(self.ts))
        object.__setattr__(
            self,
            "candles",
            MappingProxyType({key: tuple(value) for key, value in self.candles.items()}),
        )
        object.__setattr__(self, "features", MappingProxyType(dict(self.features)))
        for tf, rows in self.candles.items():
            if any(c.tf != tf for c in rows):
                raise ValueError(f"candle timeframe mismatch in {tf}")
            if any(c.ts > self.ts for c in rows):
                raise ValueError("market context contains a future candle")

    @property
    def l2_imbalance(self) -> float:
        return self.l2.imbalance

    @property
    def cvd(self) -> float:
        return self.l2.cvd


@dataclass(frozen=True)
class ExitPath:
    stop_loss: float
    take_profits: tuple[float, ...]
    time_stop_seconds: int
    trailing_activate_bps: float | None = None
    trailing_distance_bps: float | None = None

    @property
    def trailing_enabled(self) -> bool:
        return self.trailing_activate_bps is not None and self.trailing_distance_bps is not None


@dataclass(frozen=True)
class SignalCandidate:
    scanner_id: str
    symbol: str
    side: Side
    decision_ts: datetime
    entry_price: float
    stop_loss: float
    take_profits: tuple[float, ...]
    time_stop_seconds: int
    expected_hold_seconds: int
    expected_move_bps: float
    raw_expectancy_bps: float
    modeled_cost_bps: float
    fee_adjusted_expectancy_bps: float
    scalper_probability: float
    confidence: float
    entry_is_maker: bool = True
    trailing_activate_bps: float | None = None
    trailing_distance_bps: float | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "decision_ts", _utc(self.decision_ts))
        object.__setattr__(self, "take_profits", tuple(self.take_profits))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
        if self.entry_price <= 0 or self.stop_loss <= 0:
            raise ValueError("entry and stop must be positive")
        if not self.take_profits or any(price <= 0 for price in self.take_profits):
            raise ValueError("at least one positive take-profit is required")
        if self.time_stop_seconds <= 0 or self.expected_hold_seconds <= 0:
            raise ValueError("hold times must be positive")
        if self.expected_hold_seconds > self.time_stop_seconds:
            raise ValueError("expected hold cannot exceed the time stop")
        if not 0 <= self.scalper_probability <= 1 or not 0 <= self.confidence <= 1:
            raise ValueError("probability and confidence must be in [0, 1]")
        if (self.trailing_activate_bps is None) != (self.trailing_distance_bps is None):
            raise ValueError("trailing activation and distance must be configured together")
        if self.trailing_activate_bps is not None and (
            self.trailing_activate_bps <= 0 or self.trailing_distance_bps <= 0
        ):
            raise ValueError("trailing thresholds must be positive")
        if self.side is Side.LONG:
            if self.stop_loss >= self.entry_price or min(self.take_profits) <= self.entry_price:
                raise ValueError("invalid long exit geometry")
        elif self.stop_loss <= self.entry_price or max(self.take_profits) >= self.entry_price:
            raise ValueError("invalid short exit geometry")

    @property
    def rank_score(self) -> float:
        return self.fee_adjusted_expectancy_bps * self.confidence

    @property
    def dedup_key(self) -> str:
        return f"{self.scanner_id}:{self.symbol}:{self.side.value}:{self.decision_ts.isoformat()}"

    @property
    def exit_path(self) -> ExitPath:
        return ExitPath(
            stop_loss=self.stop_loss,
            take_profits=self.take_profits,
            time_stop_seconds=self.time_stop_seconds,
            trailing_activate_bps=self.trailing_activate_bps,
            trailing_distance_bps=self.trailing_distance_bps,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "scanner_id": self.scanner_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "decision_ts": self.decision_ts.isoformat(),
            "entry_price": self.entry_price,
            "stop_loss": self.stop_loss,
            "take_profits": list(self.take_profits),
            "time_stop_seconds": self.time_stop_seconds,
            "expected_hold_seconds": self.expected_hold_seconds,
            "expected_move_bps": self.expected_move_bps,
            "raw_expectancy_bps": self.raw_expectancy_bps,
            "modeled_cost_bps": self.modeled_cost_bps,
            "fee_adjusted_expectancy_bps": self.fee_adjusted_expectancy_bps,
            "scalper_probability": self.scalper_probability,
            "confidence": self.confidence,
            "entry_is_maker": self.entry_is_maker,
            "exit_path": {
                "stop_loss": self.stop_loss,
                "take_profits": list(self.take_profits),
                "time_stop_seconds": self.time_stop_seconds,
                "trailing_activate_bps": self.trailing_activate_bps,
                "trailing_distance_bps": self.trailing_distance_bps,
                "trailing_enabled": self.exit_path.trailing_enabled,
            },
            "metadata": dict(self.metadata),
        }
