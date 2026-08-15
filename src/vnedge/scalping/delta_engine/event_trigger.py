"""Research-only event-driven trigger layer for Delta public market data.

The layer is intentionally unable to submit orders.  It converts verified
public events into immutable point-in-time snapshots, evaluates event-native
scanner plugins, applies the exact same economic gates as the closed-candle
engine, and journals the decision.  The existing candle path remains a
parallel, independent A/B baseline.

Raw public events are not interchangeable with ``MarketContext``: the latter
contains proven-closed candles.  Event scanners therefore implement a small
separate protocol while sharing ``SignalCandidate`` and the gate policy.
"""

from __future__ import annotations

import heapq
import json
from collections import Counter, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from time import perf_counter_ns
from types import MappingProxyType
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vnedge.exchange.delta_event_recorder import classify_corrected_feed_delay
from vnedge.exchange.delta_public_schema import (
    parse_funding_rate_decimal,
    parse_public_trade,
    parse_ticker_open_interest,
)
from vnedge.execution.journal import DecisionJournal
from vnedge.scalping.delta_engine.absorption import (
    AbsorptionDetector,
    AbsorptionDetectorConfig,
    AbsorptionObservation,
)
from vnedge.scalping.delta_engine.absorption_research import AbsorptionResearchTracker
from vnedge.scalping.delta_engine.fee_model import DeltaFeeModel
from vnedge.scalping.delta_engine.indicator_scoring import IndicatorFamilyScorer
from vnedge.scalping.delta_engine.signal_generator import (
    PipelineStage,
    SignalGateConfig,
    candidate_gate_failures,
)
from vnedge.scalping.delta_engine.types import Side, SignalCandidate

BookSideName = Literal["bid", "ask"]
AggressorSide = Literal["buy", "sell"]


def _utc(ts: datetime) -> datetime:
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)


class EventTriggerConfig(BaseModel):
    """Frozen runtime policy for the event research path."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    min_eval_interval_ms: int = Field(default=50, ge=10, le=5_000)
    candidate_cooldown_ms: int = Field(default=30_000, ge=0, le=86_400_000)
    confirmation_ms: int = Field(default=400, ge=100, le=5_000)
    max_book_age_ms: int = Field(default=1_000, ge=50, le=30_000)
    max_trade_book_join_ms: int = Field(default=1_000, ge=10, le=10_000)
    max_reference_age_ms: int = Field(default=10_000, ge=500, le=300_000)
    max_funding_age_ms: int = Field(default=32_400_000, ge=60_000, le=86_400_000)
    max_open_interest_age_ms: int = Field(default=10_000, ge=500, le=300_000)
    max_htf_age_ms: int = Field(default=120_000, ge=10_000, le=600_000)
    book_truth_history: int = Field(default=512, ge=16, le=10_000)
    trade_window_ms: int = Field(default=5_000, ge=250, le=60_000)
    trend_window_ms: int = Field(default=900_000, ge=300_000, le=3_600_000)
    trend_sample_interval_ms: int = Field(default=1_000, ge=100, le=60_000)
    trend_max_gap_ms: int = Field(default=30_000, ge=1_000, le=300_000)
    top_levels: int = Field(default=5, ge=1, le=25)
    book_truth_levels: int = Field(default=25, ge=5, le=100)
    imbalance_threshold: float = Field(default=0.40, gt=0, le=1)
    flow_threshold: float = Field(default=0.60, gt=0, le=1)
    minimum_confirmation_samples: int = Field(default=3, ge=2, le=100)
    telemetry_window: int = Field(default=10_000, ge=100, le=100_000)
    require_htf_context: bool = False
    require_reference_prices: bool = False
    require_trade_book_join: bool = False
    one_active_observation_per_symbol: bool = False
    observation_lock_ms: int = Field(default=900_000, ge=1_000, le=3_600_000)
    enabled_symbols: tuple[str, ...] = ("BTCUSD", "ETHUSD")
    absorption: AbsorptionDetectorConfig = AbsorptionDetectorConfig()
    research_only: bool = True
    can_trade: bool = False
    can_promote: bool = False

    @model_validator(mode="after")
    def fail_closed(self) -> EventTriggerConfig:
        if not self.research_only or self.can_trade or self.can_promote:
            raise ValueError("event trigger must remain research-only")
        symbols = tuple(symbol.upper() for symbol in self.enabled_symbols)
        if not symbols or len(set(symbols)) != len(symbols):
            raise ValueError("enabled_symbols must be non-empty and unique")
        object.__setattr__(self, "enabled_symbols", symbols)
        return self


@dataclass(frozen=True)
class BookLevel:
    price: float
    size: float

    def __post_init__(self) -> None:
        if self.price <= 0 or self.size < 0:
            raise ValueError("book price must be positive and size non-negative")


@dataclass(frozen=True)
class L2Event:
    symbol: str
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    sequence: int
    is_snapshot: bool
    exchange_ts: datetime | None
    received_at: datetime
    received_monotonic_ns: int
    checksum_healthy: bool = True
    decision_eligible: bool = True
    source_delay_classification: str = "UNKNOWN"

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", self.symbol.upper())
        object.__setattr__(self, "bids", tuple(self.bids))
        object.__setattr__(self, "asks", tuple(self.asks))
        object.__setattr__(self, "received_at", _utc(self.received_at))
        if self.exchange_ts is not None:
            object.__setattr__(self, "exchange_ts", _utc(self.exchange_ts))
        if self.sequence < 0 or self.received_monotonic_ns < 0:
            raise ValueError("event sequence and monotonic timestamp cannot be negative")


@dataclass(frozen=True)
class TradeEvent:
    symbol: str
    price: float
    size: float
    side: AggressorSide
    exchange_ts: datetime | None
    received_at: datetime
    received_monotonic_ns: int
    publish_ts: datetime | None = None
    decision_eligible: bool = True
    source_delay_classification: str = "UNKNOWN"

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", self.symbol.upper())
        object.__setattr__(self, "received_at", _utc(self.received_at))
        if self.exchange_ts is not None:
            object.__setattr__(self, "exchange_ts", _utc(self.exchange_ts))
        if self.publish_ts is not None:
            object.__setattr__(self, "publish_ts", _utc(self.publish_ts))
        if self.price <= 0 or self.size <= 0:
            raise ValueError("trade price and size must be positive")
        if self.received_monotonic_ns < 0:
            raise ValueError("monotonic timestamp cannot be negative")


@dataclass(frozen=True)
class LiquidationEvent:
    symbol: str
    price: float
    size: float
    side: AggressorSide
    exchange_ts: datetime | None
    received_at: datetime
    received_monotonic_ns: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", self.symbol.upper())
        object.__setattr__(self, "received_at", _utc(self.received_at))
        if self.exchange_ts is not None:
            object.__setattr__(self, "exchange_ts", _utc(self.exchange_ts))
        if self.price <= 0 or self.size <= 0:
            raise ValueError("liquidation price and size must be positive")
        if self.received_monotonic_ns < 0:
            raise ValueError("monotonic timestamp cannot be negative")


@dataclass(frozen=True)
class FundingOpenInterestEvent:
    symbol: str
    funding_rate: float | None
    open_interest: float | None
    exchange_ts: datetime | None
    received_at: datetime
    received_monotonic_ns: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", self.symbol.upper())
        object.__setattr__(self, "received_at", _utc(self.received_at))
        if self.exchange_ts is not None:
            object.__setattr__(self, "exchange_ts", _utc(self.exchange_ts))
        if self.open_interest is not None and self.open_interest < 0:
            raise ValueError("open interest cannot be negative")
        if self.received_monotonic_ns < 0:
            raise ValueError("monotonic timestamp cannot be negative")


@dataclass(frozen=True)
class ReferencePriceEvent:
    symbol: str
    kind: Literal["mark", "spot"]
    price: float
    exchange_ts: datetime | None
    received_at: datetime
    received_monotonic_ns: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", self.symbol.upper())
        object.__setattr__(self, "received_at", _utc(self.received_at))
        if self.exchange_ts is not None:
            object.__setattr__(self, "exchange_ts", _utc(self.exchange_ts))
        if self.price <= 0:
            raise ValueError("reference price must be positive")
        if self.received_monotonic_ns < 0:
            raise ValueError("monotonic timestamp cannot be negative")


@dataclass(frozen=True)
class HigherTimeframeContext:
    symbol: str
    available_at: datetime
    bias: int = 0
    regime: str = "unknown"
    vwap_distance_bps: float = 0.0
    cusum_state: str = "unavailable"

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", self.symbol.upper())
        object.__setattr__(self, "available_at", _utc(self.available_at))
        if self.bias not in {-1, 0, 1}:
            raise ValueError("higher-timeframe bias must be -1, 0, or 1")


class _BookSide:
    """Dictionary + lazy heap: O(log n) delta update and bounded top-k read."""

    def __init__(self, *, bid: bool) -> None:
        self.bid = bid
        self.levels: dict[float, float] = {}
        self.heap: list[float] = []

    def clear(self) -> None:
        self.levels.clear()
        self.heap.clear()

    def update(self, level: BookLevel) -> None:
        if level.size == 0:
            self.levels.pop(level.price, None)
        else:
            self.levels[level.price] = level.size
            heapq.heappush(self.heap, -level.price if self.bid else level.price)
        if len(self.heap) > 4 * len(self.levels) + 100:
            self.heap = [(-price if self.bid else price) for price in self.levels]
            heapq.heapify(self.heap)

    def size_at(self, price: float) -> float:
        return self.levels.get(price, 0.0)

    def top(self, count: int) -> tuple[BookLevel, ...]:
        found: list[BookLevel] = []
        saved: list[float] = []
        while self.heap and len(found) < count:
            raw = heapq.heappop(self.heap)
            price = -raw if self.bid else raw
            size = self.levels.get(price)
            if size is None:
                continue
            found.append(BookLevel(price, size))
            saved.append(raw)
        for raw in saved:
            heapq.heappush(self.heap, raw)
        return tuple(found)


@dataclass(frozen=True)
class EventMarketSnapshot:
    symbol: str
    decision_ts: datetime
    available_at: datetime
    best_bid: float
    best_ask: float
    mid: float
    spread_bps: float
    book_imbalance: float
    flow_imbalance: float
    cvd_usd: float
    aggressive_buy_usd: float
    aggressive_sell_usd: float
    absorption_score: float
    absorption: AbsorptionObservation | None
    bid_wall_distance_bps: float | None
    ask_wall_distance_bps: float | None
    funding_rate: float | None
    open_interest: float | None
    open_interest_delta: float | None
    liquidation_distance_bps: float | None
    liquidation_side: str | None
    htf_bias: int
    regime: str
    vwap_distance_bps: float
    cusum_state: str
    confirmed_direction: int
    confirmation_source: str
    confirmation_age_ms: float
    confirmation_samples: int
    book_sequence: int | None
    book_healthy: bool
    event_kind: str
    source_exchange_ts: datetime | None
    source_received_at: datetime
    mark_price: float | None = None
    spot_price: float | None = None
    basis_bps: float | None = None
    market_truth_ready: bool = False
    market_truth_blockers: tuple[str, ...] = ()
    market_truth_required_blockers: tuple[str, ...] = ()
    features: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "decision_ts", _utc(self.decision_ts))
        object.__setattr__(self, "available_at", _utc(self.available_at))
        object.__setattr__(self, "source_received_at", _utc(self.source_received_at))
        object.__setattr__(self, "features", MappingProxyType(dict(self.features)))
        object.__setattr__(self, "market_truth_blockers", tuple(self.market_truth_blockers))
        object.__setattr__(
            self,
            "market_truth_required_blockers",
            tuple(self.market_truth_required_blockers),
        )
        if self.source_exchange_ts is not None:
            object.__setattr__(self, "source_exchange_ts", _utc(self.source_exchange_ts))
        if self.available_at > self.decision_ts:
            raise ValueError("snapshot contains information unavailable at decision time")
        if self.best_bid <= 0 or self.best_ask <= self.best_bid:
            raise ValueError("snapshot requires a valid uncrossed book")
        if self.confirmed_direction not in {-1, 0, 1}:
            raise ValueError("confirmed direction must be -1, 0, or 1")

    def to_dict(self) -> dict[str, object]:
        return {
            **self.__dict__,
            "decision_ts": self.decision_ts.isoformat(),
            "available_at": self.available_at.isoformat(),
            "source_exchange_ts": (
                self.source_exchange_ts.isoformat() if self.source_exchange_ts else None
            ),
            "source_received_at": self.source_received_at.isoformat(),
            "absorption": self.absorption.to_dict() if self.absorption else None,
            "features": dict(self.features),
            "market_truth_blockers": list(self.market_truth_blockers),
            "market_truth_required_blockers": list(self.market_truth_required_blockers),
        }


class EventScanner(Protocol):
    scanner_id: str

    def evaluate(self, context: EventMarketSnapshot) -> SignalCandidate | None: ...


@dataclass(frozen=True)
class SustainedFlowImbalanceScanner:
    """First event-native hypothesis; uncalibrated and rejected by default.

    The scanner records sustained book/flow agreement for forward research.
    Its 0.50 probability prior intentionally fails the production-grade 0.70
    gate until chronological event replay supplies real calibration evidence.
    """

    fee_model: DeltaFeeModel
    scanner_id: str = "event_sustained_flow_imbalance_v1"
    stop_bps: float = 12.0
    target_bps: float = 30.0
    time_stop_seconds: int = 30
    probability_prior: float = 0.50
    entry_is_maker: bool = False

    def __post_init__(self) -> None:
        if self.stop_bps <= 0 or self.target_bps <= 0 or self.time_stop_seconds <= 0:
            raise ValueError("event scanner exit geometry must be positive")
        if not 0 <= self.probability_prior <= 1:
            raise ValueError("probability prior must be in [0, 1]")

    def evaluate(self, context: EventMarketSnapshot) -> SignalCandidate | None:
        if (
            not context.book_healthy
            or context.confirmed_direction == 0
            or context.confirmation_source != "flow_imbalance"
        ):
            return None
        side = Side.LONG if context.confirmed_direction > 0 else Side.SHORT
        entry = context.mid
        stop_factor = self.stop_bps / 10_000.0
        target_factor = self.target_bps / 10_000.0
        stop = entry * (1 - stop_factor if side is Side.LONG else 1 + stop_factor)
        target = entry * (1 + target_factor if side is Side.LONG else 1 - target_factor)
        costs = self.fee_model.breakdown(
            context.symbol,
            entry_is_maker=self.entry_is_maker,
            exit_is_maker=False,
            hold_seconds=self.time_stop_seconds,
        )
        strength = min(1.0, (abs(context.book_imbalance) + abs(context.flow_imbalance)) / 2)
        raw_expectancy = (
            self.probability_prior * self.target_bps
            - (1.0 - self.probability_prior) * self.stop_bps
        )
        return SignalCandidate(
            scanner_id=self.scanner_id,
            symbol=context.symbol,
            side=side,
            decision_ts=context.decision_ts,
            entry_price=entry,
            stop_loss=stop,
            take_profits=(target,),
            time_stop_seconds=self.time_stop_seconds,
            expected_hold_seconds=self.time_stop_seconds,
            expected_move_bps=self.target_bps,
            raw_expectancy_bps=raw_expectancy,
            modeled_cost_bps=costs.total_bps,
            fee_adjusted_expectancy_bps=raw_expectancy - costs.total_bps,
            scalper_probability=self.probability_prior,
            confidence=strength,
            entry_is_maker=self.entry_is_maker,
            metadata={
                "event_context_schema": "vnedge.delta_event_context.v1",
                "confirmation_ms": context.confirmation_age_ms,
                "confirmation_samples": context.confirmation_samples,
                "book_imbalance": context.book_imbalance,
                "flow_imbalance": context.flow_imbalance,
                "uncalibrated_probability_prior": True,
                "research_only": True,
                "can_trade": False,
                "can_promote": False,
            },
        )


@dataclass(frozen=True)
class AbsorptionReversalScanner:
    """Research hypothesis: fade verified displayed absorption/exhaustion."""

    fee_model: DeltaFeeModel
    scanner_id: str = "event_absorption_reversal_v1"
    stop_bps: float = 10.0
    target_bps: float = 25.0
    time_stop_seconds: int = 45
    probability_prior: float = 0.50
    entry_is_maker: bool = False

    def __post_init__(self) -> None:
        if self.stop_bps <= 0 or self.target_bps <= 0 or self.time_stop_seconds <= 0:
            raise ValueError("absorption scanner exit geometry must be positive")
        if not 0 <= self.probability_prior <= 1:
            raise ValueError("probability prior must be in [0, 1]")

    def evaluate(self, context: EventMarketSnapshot) -> SignalCandidate | None:
        observation = context.absorption
        if (
            not context.book_healthy
            or context.confirmation_source != "absorption"
            or observation is None
            or observation.reversal_direction != context.confirmed_direction
        ):
            return None
        side = Side.LONG if observation.reversal_direction > 0 else Side.SHORT
        entry = context.mid
        stop_factor = self.stop_bps / 10_000.0
        target_factor = self.target_bps / 10_000.0
        stop = entry * (1 - stop_factor if side is Side.LONG else 1 + stop_factor)
        target = entry * (1 + target_factor if side is Side.LONG else 1 - target_factor)
        costs = self.fee_model.breakdown(
            context.symbol,
            entry_is_maker=self.entry_is_maker,
            exit_is_maker=False,
            hold_seconds=self.time_stop_seconds,
        )
        raw_expectancy = (
            self.probability_prior * self.target_bps
            - (1.0 - self.probability_prior) * self.stop_bps
        )
        return SignalCandidate(
            scanner_id=self.scanner_id,
            symbol=context.symbol,
            side=side,
            decision_ts=context.decision_ts,
            entry_price=entry,
            stop_loss=stop,
            take_profits=(target,),
            time_stop_seconds=self.time_stop_seconds,
            expected_hold_seconds=self.time_stop_seconds,
            expected_move_bps=self.target_bps,
            raw_expectancy_bps=raw_expectancy,
            modeled_cost_bps=costs.total_bps,
            fee_adjusted_expectancy_bps=raw_expectancy - costs.total_bps,
            scalper_probability=self.probability_prior,
            confidence=observation.strength,
            entry_is_maker=self.entry_is_maker,
            metadata={
                "event_context_schema": "vnedge.delta_event_context.v1",
                "absorption": observation.to_dict(),
                "uncalibrated_probability_prior": True,
                "research_only": True,
                "can_trade": False,
                "can_promote": False,
            },
        )


@dataclass(frozen=True)
class EventTriggerDecision:
    symbol: str
    decision_ts: datetime
    trigger_kind: str
    selected: SignalCandidate | None
    evaluated: tuple[SignalCandidate, ...]
    rejection_reasons: tuple[str, ...]
    context: EventMarketSnapshot | None
    pipeline_trace: tuple[PipelineStage, ...]
    total_duration_us: int
    feed_delay_us: int | None
    journal_write_success: bool | None = None
    duplicate: bool = False
    research_only: bool = True
    can_trade: bool = False
    can_promote: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "decision_ts": self.decision_ts.isoformat(),
            "trigger_kind": self.trigger_kind,
            "selected": self.selected.to_dict() if self.selected else None,
            "evaluated": [candidate.to_dict() for candidate in self.evaluated],
            "rejection_reasons": list(self.rejection_reasons),
            "context": self.context.to_dict() if self.context else None,
            "pipeline_trace": [stage.to_dict() for stage in self.pipeline_trace],
            "total_duration_us": self.total_duration_us,
            "feed_delay_us": self.feed_delay_us,
            "journal_write_success": self.journal_write_success,
            "duplicate": self.duplicate,
            "research_only": True,
            "can_trade": False,
            "can_promote": False,
            "order_route": "absent",
        }


@dataclass(frozen=True)
class _BookTruthSnapshot:
    """Causally received book state used to align a published trade."""

    exchange_ts: datetime
    received_at: datetime
    sequence: int
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]


@dataclass
class _SymbolState:
    bids: _BookSide = field(default_factory=lambda: _BookSide(bid=True))
    asks: _BookSide = field(default_factory=lambda: _BookSide(bid=False))
    trades: deque[tuple[int, float, float]] = field(default_factory=deque)
    trend_prices: deque[tuple[datetime, float]] = field(default_factory=deque)
    buy_usd: float = 0.0
    sell_usd: float = 0.0
    confirmation_direction: int = 0
    confirmation_started_ns: int | None = None
    confirmation_samples: int = 0
    last_book_sequence: int | None = None
    book_healthy: bool = False
    last_book_monotonic_ns: int | None = None
    book_truth: deque[_BookTruthSnapshot] = field(default_factory=deque)
    last_trade_book_join_ok: bool = False
    last_trade_book_join_lag_ms: float | None = None
    last_trade_book_join_reason: str = "no_trade_join_yet"
    last_eval_monotonic_ns: int | None = None
    last_mid: float | None = None
    last_trade_price: float | None = None
    last_trend_sample_at: datetime | None = None
    last_received_at: datetime | None = None
    last_source_exchange_ts: datetime | None = None
    last_source_decision_eligible: bool = True
    last_source_delay_classification: str = "UNKNOWN"
    flow_anchor_mid: float | None = None
    funding_rate: float | None = None
    open_interest: float | None = None
    open_interest_delta: float | None = None
    funding_observed_at: datetime | None = None
    funding_monotonic_ns: int | None = None
    open_interest_observed_at: datetime | None = None
    open_interest_monotonic_ns: int | None = None
    mark_price: float | None = None
    mark_observed_at: datetime | None = None
    mark_monotonic_ns: int | None = None
    spot_price: float | None = None
    spot_observed_at: datetime | None = None
    spot_monotonic_ns: int | None = None
    liquidation_price: float | None = None
    liquidation_side: str | None = None
    htf: HigherTimeframeContext | None = None
    absorption_detector: AbsorptionDetector | None = None
    last_absorption_evaluated_ns: int | None = None
    observation_active_until_ns: int | None = None


class _LatencyWindow:
    def __init__(self, size: int) -> None:
        self.values: deque[int] = deque(maxlen=size)

    def add(self, value: int | None) -> None:
        if value is not None:
            self.values.append(value)

    def summary(self) -> dict[str, int | None]:
        if not self.values:
            return {"count": 0, "p50_us": None, "p95_us": None, "p99_us": None}
        rows = sorted(self.values)

        def percentile(p: float) -> int:
            return rows[min(len(rows) - 1, int((len(rows) - 1) * p))]

        return {
            "count": len(rows),
            "p50_us": percentile(0.50),
            "p95_us": percentile(0.95),
            "p99_us": percentile(0.99),
        }


class EventDrivenTriggerLayer:
    """Incremental event state + research decision path; never routes orders."""

    def __init__(
        self,
        scanners: tuple[EventScanner, ...],
        *,
        config: EventTriggerConfig | None = None,
        gates: SignalGateConfig | None = None,
        journal: DecisionJournal | None = None,
        absorption_research: AbsorptionResearchTracker | None = None,
        indicator_scorer: IndicatorFamilyScorer | None = None,
    ) -> None:
        self.config = config or EventTriggerConfig()
        self.gates = gates or SignalGateConfig(allowed_symbols=self.config.enabled_symbols)
        self.scanners = scanners
        self.journal = journal
        self.absorption_research = absorption_research
        self.indicator_scorer = indicator_scorer or IndicatorFamilyScorer()
        self._states: dict[str, _SymbolState] = {}
        self._seen: set[str] = set()
        self._last_selected_ns: dict[tuple[str, str, str], int] = {}
        self._counts: dict[str, int] = {
            "events": 0,
            "evaluations": 0,
            "raw_candidates": 0,
            "selected": 0,
            "rate_limited": 0,
            "cooldown_blocked": 0,
            "not_confirmed": 0,
            "invalid_book": 0,
            "trade_book_joined": 0,
            "trade_book_join_rejected": 0,
            "market_truth_blocked": 0,
            "observation_blocked": 0,
            "absorption_observations": 0,
            "counterfactual_observations": 0,
            "counterfactual_outcomes": 0,
            "context_errors": 0,
            "scanner_errors": 0,
            "indicator_scoring_errors": 0,
            "scanner_no_signal": 0,
            "gate_rejected": 0,
            "stale_source_events": 0,
        }
        self._rejection_reasons: Counter[str] = Counter()
        self._feed_latency = _LatencyWindow(self.config.telemetry_window)
        self._decision_latency = _LatencyWindow(self.config.telemetry_window)

    def _state(self, symbol: str) -> _SymbolState:
        native = symbol.upper()
        if native not in self.config.enabled_symbols:
            raise ValueError(f"event symbol is not enabled: {native}")
        state = self._states.get(native)
        if state is None:
            state = _SymbolState()
            instrument = self.config.absorption.instrument(native)
            if self.config.absorption.enabled and instrument is not None:
                state.absorption_detector = AbsorptionDetector(
                    instrument,
                    self.config.absorption,
                )
            self._states[native] = state
        return state

    def update_higher_timeframe_context(self, context: HigherTimeframeContext) -> None:
        self._state(context.symbol).htf = context

    def on_l2(self, event: L2Event) -> EventTriggerDecision | None:
        started = perf_counter_ns()
        state = self._state(event.symbol)
        state.last_received_at = event.received_at
        state.last_source_exchange_ts = event.exchange_ts
        state.last_source_decision_eligible = event.decision_eligible
        state.last_source_delay_classification = event.source_delay_classification
        self._counts["events"] += 1
        valid_sequence = event.is_snapshot or (
            state.last_book_sequence is not None and event.sequence == state.last_book_sequence + 1
        )
        if not event.checksum_healthy or not valid_sequence:
            state.bids.clear()
            state.asks.clear()
            state.book_healthy = False
            state.last_book_sequence = None
            state.confirmation_direction = 0
            state.confirmation_started_ns = None
            state.confirmation_samples = 0
            if state.absorption_detector is not None:
                state.absorption_detector.reset()
            self._counts["invalid_book"] += 1
            return None
        if event.is_snapshot:
            state.bids.clear()
            state.asks.clear()
            if state.absorption_detector is not None:
                state.absorption_detector.reset()
        changes: list[tuple[BookSideName, float, float, float]] = []
        for level in event.bids:
            previous = state.bids.size_at(level.price)
            state.bids.update(level)
            changes.append(("bid", level.price, previous, level.size))
        for level in event.asks:
            previous = state.asks.size_at(level.price)
            state.asks.update(level)
            changes.append(("ask", level.price, previous, level.size))
        state.last_book_sequence = event.sequence
        state.book_healthy = True
        state.last_book_monotonic_ns = event.received_monotonic_ns
        if event.exchange_ts is not None:
            try:
                truth_bids = state.bids.top(self.config.book_truth_levels)
                truth_asks = state.asks.top(self.config.book_truth_levels)
                if truth_bids and truth_asks and truth_bids[0].price < truth_asks[0].price:
                    state.book_truth.append(
                        _BookTruthSnapshot(
                            exchange_ts=event.exchange_ts,
                            received_at=event.received_at,
                            sequence=event.sequence,
                            bids=truth_bids,
                            asks=truth_asks,
                        )
                    )
                    while len(state.book_truth) > self.config.book_truth_history:
                        state.book_truth.popleft()
            except ValueError:
                pass
        if not event.decision_eligible:
            # Preserve sequence-correct book state, but never turn stale source
            # data into a present-time decision or sustained confirmation.
            state.confirmation_direction = 0
            state.confirmation_started_ns = None
            state.confirmation_samples = 0
            if state.absorption_detector is not None:
                state.absorption_detector.reset()
            self._counts["stale_source_events"] += 1
            self._rejection_reasons[
                f"source_latency:{event.source_delay_classification.lower()}"
            ] += 1
            return None
        detector = state.absorption_detector
        if detector is not None:
            try:
                _, _, mid, _, _ = self._book_features(state)
            except ValueError:
                mid = 0.0
            if mid > 0:
                for side, price, previous, current in changes:
                    observation = detector.on_book_delta(
                        side=side,
                        price=price,
                        previous_size=previous,
                        current_size=current,
                        mid=mid,
                        now_ns=event.received_monotonic_ns,
                    )
                    if observation is not None:
                        self._counts["absorption_observations"] += 1
        self._record_confirmation(state, event.received_monotonic_ns)
        return self._maybe_evaluate(
            event.symbol,
            "l2",
            event.exchange_ts,
            event.received_at,
            event.received_monotonic_ns,
            started,
        )

    def on_trade(self, event: TradeEvent) -> EventTriggerDecision | None:
        started = perf_counter_ns()
        state = self._state(event.symbol)
        state.last_received_at = event.received_at
        state.last_source_exchange_ts = event.exchange_ts
        state.last_source_decision_eligible = event.decision_eligible
        state.last_source_delay_classification = event.source_delay_classification
        self._counts["events"] += 1
        if not event.decision_eligible:
            # The observer may retain the verified tape for later replay, but
            # stale/backlogged trades cannot alter the live rolling flow state.
            state.confirmation_direction = 0
            state.confirmation_started_ns = None
            state.confirmation_samples = 0
            self._counts["stale_source_events"] += 1
            self._rejection_reasons[
                f"source_latency:{event.source_delay_classification.lower()}"
            ] += 1
            return None
        state.last_trade_price = event.price
        self._record_trend_price(state, event.received_at, event.price)
        if self.absorption_research is not None:
            outcomes = self.absorption_research.on_trade(
                event.symbol,
                price=event.price,
                received_at=event.received_at,
                monotonic_ns=event.received_monotonic_ns,
            )
            self._counts["counterfactual_outcomes"] += len(outcomes)
        instrument = self.config.absorption.instrument(event.symbol)
        contract_value = instrument.contract_value if instrument is not None else 1.0
        notional = event.price * event.size * contract_value
        signed = notional if event.side == "buy" else -notional
        state.trades.append((event.received_monotonic_ns, signed, event.price))
        if signed > 0:
            state.buy_usd += signed
        else:
            state.sell_usd += -signed
        self._prune_trades(state, event.received_monotonic_ns)
        detector = state.absorption_detector
        matched = False
        resting_size = 0.0
        matched_mid = 0.0
        if state.book_healthy:
            matched, resting_size, matched_mid = self._trade_book_truth(state, event)
            self._counts["trade_book_joined" if matched else "trade_book_join_rejected"] += 1
        if detector is not None and state.book_healthy and matched:
            observation = detector.on_trade(
                price=event.price,
                size=event.size,
                notional_usd=notional,
                side=event.side,
                current_resting_size=resting_size,
                mid=matched_mid,
                now_ns=event.received_monotonic_ns,
            )
            if observation is not None:
                self._counts["absorption_observations"] += 1
        self._record_confirmation(state, event.received_monotonic_ns)
        return self._maybe_evaluate(
            event.symbol,
            "trade",
            event.exchange_ts,
            event.received_at,
            event.received_monotonic_ns,
            started,
        )

    def on_liquidation(self, event: LiquidationEvent) -> EventTriggerDecision | None:
        started = perf_counter_ns()
        state = self._state(event.symbol)
        state.last_received_at = event.received_at
        state.last_source_exchange_ts = event.exchange_ts
        self._counts["events"] += 1
        state.liquidation_price = event.price
        state.liquidation_side = event.side
        if state.absorption_detector is not None:
            state.absorption_detector.on_liquidation(
                price=event.price,
                size=event.size,
                liquidated_side="long" if event.side == "sell" else "short",
                now_ns=event.received_monotonic_ns,
            )
        self._record_confirmation(state, event.received_monotonic_ns)
        return self._maybe_evaluate(
            event.symbol,
            "liquidation",
            event.exchange_ts,
            event.received_at,
            event.received_monotonic_ns,
            started,
        )

    def on_funding_or_oi(self, event: FundingOpenInterestEvent) -> EventTriggerDecision | None:
        started = perf_counter_ns()
        state = self._state(event.symbol)
        state.last_received_at = event.received_at
        state.last_source_exchange_ts = event.exchange_ts
        self._counts["events"] += 1
        if event.funding_rate is not None:
            state.funding_rate = event.funding_rate
            state.funding_observed_at = event.received_at
            state.funding_monotonic_ns = event.received_monotonic_ns
        if event.open_interest is not None:
            previous = state.open_interest
            state.open_interest = event.open_interest
            state.open_interest_observed_at = event.received_at
            state.open_interest_monotonic_ns = event.received_monotonic_ns
            state.open_interest_delta = (
                event.open_interest - previous if previous is not None else None
            )
        self._record_confirmation(state, event.received_monotonic_ns)
        return self._maybe_evaluate(
            event.symbol,
            "funding_oi",
            event.exchange_ts,
            event.received_at,
            event.received_monotonic_ns,
            started,
        )

    def on_reference_price(self, event: ReferencePriceEvent) -> None:
        """Update mark/spot truth without turning a reference tick into a trigger."""

        state = self._state(event.symbol)
        state.last_received_at = event.received_at
        state.last_source_exchange_ts = event.exchange_ts
        self._counts["events"] += 1
        if event.kind == "mark":
            state.mark_price = event.price
            state.mark_observed_at = event.received_at
            state.mark_monotonic_ns = event.received_monotonic_ns
        else:
            state.spot_price = event.price
            state.spot_observed_at = event.received_at
            state.spot_monotonic_ns = event.received_monotonic_ns

    def _trade_book_truth(
        self,
        state: _SymbolState,
        event: TradeEvent,
    ) -> tuple[bool, float, float]:
        """Join a trade to the latest already-received book published before it."""

        published = event.publish_ts
        if published is None:
            state.last_trade_book_join_ok = False
            state.last_trade_book_join_lag_ms = None
            state.last_trade_book_join_reason = "trade_publish_timestamp_missing"
            return False, 0.0, 0.0
        matched = next(
            (
                row
                for row in reversed(state.book_truth)
                if row.exchange_ts <= published and row.received_at <= event.received_at
            ),
            None,
        )
        if matched is None:
            state.last_trade_book_join_ok = False
            state.last_trade_book_join_lag_ms = None
            state.last_trade_book_join_reason = "causal_book_snapshot_missing"
            return False, 0.0, 0.0
        lag_ms = (published - matched.exchange_ts).total_seconds() * 1_000.0
        state.last_trade_book_join_lag_ms = lag_ms
        if lag_ms < 0 or lag_ms > self.config.max_trade_book_join_ms:
            state.last_trade_book_join_ok = False
            state.last_trade_book_join_reason = "trade_book_join_stale"
            return False, 0.0, 0.0
        side = matched.asks if event.side == "buy" else matched.bids
        size = next((level.size for level in side if level.price == event.price), 0.0)
        if size <= 0:
            state.last_trade_book_join_ok = False
            state.last_trade_book_join_reason = "trade_price_absent_from_resting_book"
            return False, 0.0, 0.0
        mid = (matched.bids[0].price + matched.asks[0].price) / 2.0
        state.last_trade_book_join_ok = True
        state.last_trade_book_join_reason = "joined"
        return True, size, mid

    def _prune_trades(self, state: _SymbolState, now_ns: int) -> None:
        cutoff = now_ns - self.config.trade_window_ms * 1_000_000
        while state.trades and state.trades[0][0] < cutoff:
            _, signed, _ = state.trades.popleft()
            if signed > 0:
                state.buy_usd -= signed
            else:
                state.sell_usd -= -signed

    def _record_trend_price(
        self,
        state: _SymbolState,
        received_at: datetime,
        price: float,
    ) -> None:
        previous = state.last_trend_sample_at
        if previous is not None:
            gap_ms = (received_at - previous).total_seconds() * 1_000.0
            if gap_ms < 0 or gap_ms > self.config.trend_max_gap_ms:
                state.trend_prices.clear()
        if (
            previous is None
            or (received_at - previous).total_seconds() * 1_000.0
            >= self.config.trend_sample_interval_ms
        ):
            state.trend_prices.append((received_at, price))
            state.last_trend_sample_at = received_at
        cutoff = received_at.timestamp() - self.config.trend_window_ms / 1_000.0
        while state.trend_prices and state.trend_prices[0][0].timestamp() < cutoff:
            state.trend_prices.popleft()

    @staticmethod
    def _event_trend_features(state: _SymbolState, now: datetime) -> dict[str, float]:
        rows = tuple(state.trend_prices)
        if not rows:
            return {
                "last_trade_price": state.last_trade_price or 0.0,
                "trend_coverage_seconds": 0.0,
                "trend_1m_bps": 0.0,
                "trend_5m_bps": 0.0,
                "trend_15m_bps": 0.0,
            }
        latest = state.last_trade_price or rows[-1][1]
        coverage = max(0.0, (now - rows[0][0]).total_seconds())

        def trailing_return(seconds: int) -> float:
            if coverage < seconds:
                return 0.0
            cutoff = now.timestamp() - seconds
            reference = rows[0][1]
            for observed_at, price in rows:
                if observed_at.timestamp() >= cutoff:
                    reference = price
                    break
            return (latest / reference - 1.0) * 10_000.0

        return {
            "last_trade_price": latest,
            "trend_coverage_seconds": coverage,
            "trend_1m_bps": trailing_return(60),
            "trend_5m_bps": trailing_return(300),
            "trend_15m_bps": trailing_return(900),
        }

    def _book_features(
        self, state: _SymbolState
    ) -> tuple[tuple[BookLevel, ...], tuple[BookLevel, ...], float, float, float]:
        bids = state.bids.top(self.config.top_levels)
        asks = state.asks.top(self.config.top_levels)
        if not bids or not asks or bids[0].price >= asks[0].price:
            raise ValueError("event book is empty or crossed")
        weighted_bid = sum(level.size / (index + 1) for index, level in enumerate(bids))
        weighted_ask = sum(level.size / (index + 1) for index, level in enumerate(asks))
        total = weighted_bid + weighted_ask
        imbalance = (weighted_bid - weighted_ask) / total if total else 0.0
        mid = (bids[0].price + asks[0].price) / 2.0
        spread = (asks[0].price - bids[0].price) / mid * 10_000.0
        return bids, asks, mid, spread, imbalance

    def _direction(self, state: _SymbolState) -> int:
        if not state.book_healthy:
            return 0
        try:
            _, _, _, _, imbalance = self._book_features(state)
        except ValueError:
            return 0
        total = state.buy_usd + state.sell_usd
        flow = (state.buy_usd - state.sell_usd) / total if total else 0.0
        if imbalance >= self.config.imbalance_threshold and flow >= self.config.flow_threshold:
            return 1
        if imbalance <= -self.config.imbalance_threshold and flow <= -self.config.flow_threshold:
            return -1
        return 0

    def _record_confirmation(self, state: _SymbolState, now_ns: int) -> None:
        self._prune_trades(state, now_ns)
        direction = self._direction(state)
        if direction == 0:
            state.confirmation_direction = 0
            state.confirmation_started_ns = None
            state.confirmation_samples = 0
        elif direction != state.confirmation_direction:
            state.confirmation_direction = direction
            state.confirmation_started_ns = now_ns
            state.confirmation_samples = 1
        else:
            state.confirmation_samples += 1

    def _sustained_direction(self, state: _SymbolState, now_ns: int) -> tuple[int, int, float]:
        started = state.confirmation_started_ns
        samples = state.confirmation_samples
        if state.confirmation_direction == 0 or started is None:
            return 0, samples, 0.0
        age_ms = (now_ns - started) / 1_000_000.0
        if samples < self.config.minimum_confirmation_samples:
            return 0, samples, age_ms
        if age_ms < self.config.confirmation_ms:
            return 0, samples, age_ms
        return state.confirmation_direction, samples, age_ms

    def _freeze(
        self,
        symbol: str,
        state: _SymbolState,
        *,
        event_kind: str,
        exchange_ts: datetime | None,
        received_at: datetime,
        now_ns: int,
        direction: int,
        samples: int,
        confirmation_age_ms: float,
        confirmation_source: str,
    ) -> EventMarketSnapshot:
        bids, asks, mid, spread, imbalance = self._book_features(state)
        total_flow = state.buy_usd + state.sell_usd
        flow = (state.buy_usd - state.sell_usd) / total_flow if total_flow else 0.0
        instrument = self.config.absorption.instrument(symbol)
        contract_value = instrument.contract_value if instrument is not None else 1.0
        depth = sum(level.price * level.size * contract_value for level in (*bids, *asks))
        absorption = (
            state.absorption_detector.latest(now_ns)
            if state.absorption_detector is not None
            else None
        )
        bid_wall = max(bids, key=lambda level: level.size)
        ask_wall = max(asks, key=lambda level: level.size)
        liq_distance = (
            abs(state.liquidation_price / mid - 1) * 10_000.0
            if state.liquidation_price is not None
            else None
        )
        htf = state.htf
        blockers = self._market_truth_blockers(state, now_ns)
        required_blockers = self._required_market_truth_blockers(blockers)
        basis_bps = (
            (state.mark_price / state.spot_price - 1.0) * 10_000.0
            if state.mark_price is not None and state.spot_price is not None
            else None
        )
        open_interest_notional = (
            state.open_interest * contract_value * state.mark_price
            if state.open_interest is not None and state.mark_price is not None
            else None
        )
        available_at = max(received_at, htf.available_at if htf else received_at)
        if available_at > received_at:
            raise ValueError("higher-timeframe context is not yet available")
        state.last_mid = mid
        return EventMarketSnapshot(
            symbol=symbol.upper(),
            decision_ts=received_at,
            available_at=available_at,
            best_bid=bids[0].price,
            best_ask=asks[0].price,
            mid=mid,
            spread_bps=spread,
            book_imbalance=imbalance,
            flow_imbalance=flow,
            cvd_usd=state.buy_usd - state.sell_usd,
            aggressive_buy_usd=state.buy_usd,
            aggressive_sell_usd=state.sell_usd,
            absorption_score=absorption.strength if absorption else 0.0,
            absorption=absorption,
            bid_wall_distance_bps=(mid - bid_wall.price) / mid * 10_000.0,
            ask_wall_distance_bps=(ask_wall.price - mid) / mid * 10_000.0,
            funding_rate=state.funding_rate,
            open_interest=state.open_interest,
            open_interest_delta=state.open_interest_delta,
            liquidation_distance_bps=liq_distance,
            liquidation_side=state.liquidation_side,
            htf_bias=htf.bias if htf else 0,
            regime=htf.regime if htf else "unknown",
            vwap_distance_bps=htf.vwap_distance_bps if htf else 0.0,
            cusum_state=htf.cusum_state if htf else "unavailable",
            confirmed_direction=direction,
            confirmation_source=confirmation_source,
            confirmation_age_ms=confirmation_age_ms,
            confirmation_samples=samples,
            book_sequence=state.last_book_sequence,
            book_healthy=state.book_healthy,
            event_kind=event_kind,
            source_exchange_ts=exchange_ts,
            source_received_at=received_at,
            mark_price=state.mark_price,
            spot_price=state.spot_price,
            basis_bps=basis_bps,
            market_truth_ready=not required_blockers,
            market_truth_blockers=blockers,
            market_truth_required_blockers=required_blockers,
            features={
                "depth_usd": depth,
                "contract_value": contract_value,
                "htf_available": htf is not None,
                "basis_bps": basis_bps,
                "funding_available": state.funding_rate is not None,
                "open_interest_available": state.open_interest is not None,
                "open_interest_notional_usd": open_interest_notional,
                "trade_book_join_ok": state.last_trade_book_join_ok,
                "trade_book_join_lag_ms": state.last_trade_book_join_lag_ms,
                "trade_book_join_reason": state.last_trade_book_join_reason,
                "trade_window_ms": float(self.config.trade_window_ms),
                "book_age_ms": (
                    (now_ns - state.last_book_monotonic_ns) / 1_000_000.0
                    if state.last_book_monotonic_ns is not None
                    else float("inf")
                ),
                **self._event_trend_features(state, received_at),
            },
        )

    def _market_truth_blockers(
        self,
        state: _SymbolState,
        now_ns: int,
    ) -> tuple[str, ...]:
        blockers: list[str] = []
        if not state.book_healthy or state.last_book_monotonic_ns is None:
            blockers.append("book_unhealthy")
        elif now_ns - state.last_book_monotonic_ns > self.config.max_book_age_ms * 1_000_000:
            blockers.append("book_stale")
        if not state.last_trade_book_join_ok:
            blockers.append(state.last_trade_book_join_reason)
        if state.htf is None:
            blockers.append("htf_context_missing")
        elif (
            state.last_received_at is not None
            and (state.last_received_at - state.htf.available_at).total_seconds() * 1_000.0
            > self.config.max_htf_age_ms
        ):
            blockers.append("htf_context_stale")
        if state.mark_price is None:
            blockers.append("mark_price_missing")
        elif now_ns > 0 and (
            state.mark_monotonic_ns is None
            or now_ns - state.mark_monotonic_ns > self.config.max_reference_age_ms * 1_000_000
        ):
            blockers.append("mark_price_stale")
        if state.spot_price is None:
            blockers.append("spot_price_missing")
        elif now_ns > 0 and (
            state.spot_monotonic_ns is None
            or now_ns - state.spot_monotonic_ns > self.config.max_reference_age_ms * 1_000_000
        ):
            blockers.append("spot_price_stale")
        if state.funding_rate is None:
            blockers.append("funding_missing")
        elif now_ns > 0 and (
            state.funding_monotonic_ns is None
            or now_ns - state.funding_monotonic_ns > self.config.max_funding_age_ms * 1_000_000
        ):
            blockers.append("funding_stale")
        if state.open_interest is None:
            blockers.append("open_interest_missing")
        elif now_ns > 0 and (
            state.open_interest_monotonic_ns is None
            or now_ns - state.open_interest_monotonic_ns
            > self.config.max_open_interest_age_ms * 1_000_000
        ):
            blockers.append("open_interest_stale")
        return tuple(dict.fromkeys(blockers))

    def _required_market_truth_blockers(
        self,
        truth_blockers: tuple[str, ...],
    ) -> tuple[str, ...]:
        required: set[str] = set()
        if self.config.require_htf_context:
            required.update(
                reason for reason in truth_blockers if reason.startswith("htf_context_")
            )
        if self.config.require_reference_prices:
            required.update(
                reason
                for reason in truth_blockers
                if reason.startswith(("mark_price_", "spot_price_"))
            )
        if self.config.require_trade_book_join:
            required.update(
                reason
                for reason in truth_blockers
                if reason.startswith("trade_") or reason == "causal_book_snapshot_missing"
            )
        return tuple(reason for reason in truth_blockers if reason in required)

    def _maybe_evaluate(
        self,
        symbol: str,
        trigger_kind: str,
        exchange_ts: datetime | None,
        received_at: datetime,
        now_ns: int,
        event_started_ns: int,
    ) -> EventTriggerDecision | None:
        state = self._state(symbol)
        if state.last_eval_monotonic_ns is not None and (
            now_ns - state.last_eval_monotonic_ns < self.config.min_eval_interval_ms * 1_000_000
        ):
            self._counts["rate_limited"] += 1
            return None
        if state.last_book_monotonic_ns is None or (
            now_ns - state.last_book_monotonic_ns > self.config.max_book_age_ms * 1_000_000
        ):
            self._counts["invalid_book"] += 1
            return None
        absorption = (
            state.absorption_detector.latest(now_ns)
            if state.absorption_detector is not None
            else None
        )
        use_absorption = (
            absorption is not None
            and absorption.detected_monotonic_ns != state.last_absorption_evaluated_ns
        )
        if use_absorption:
            direction = absorption.reversal_direction
            samples = max(2, absorption.refresh_count + 1)
            confirmation_age_ms = absorption.duration_ms
            confirmation_source = "absorption"
            state.last_absorption_evaluated_ns = absorption.detected_monotonic_ns
            if self.absorption_research is not None:
                registered = self.absorption_research.register(
                    absorption,
                    decision_ts=received_at,
                )
                self._counts["counterfactual_observations"] += int(registered)
        else:
            direction, samples, confirmation_age_ms = self._sustained_direction(state, now_ns)
            confirmation_source = "flow_imbalance"
        observes_unconfirmed = any(
            bool(getattr(scanner, "observes_unconfirmed_events", False))
            for scanner in self.scanners
        )
        if direction == 0 and not observes_unconfirmed:
            self._counts["not_confirmed"] += 1
            return None
        truth_blockers = self._market_truth_blockers(state, now_ns)
        active_truth_blockers = self._required_market_truth_blockers(truth_blockers)
        if active_truth_blockers:
            self._counts["market_truth_blocked"] += 1
            self._rejection_reasons.update(
                f"market_truth:{reason}" for reason in active_truth_blockers
            )
            return None
        state.last_eval_monotonic_ns = now_ns
        trace: list[PipelineStage] = []
        freeze_started = perf_counter_ns()
        try:
            context = self._freeze(
                symbol,
                state,
                event_kind=trigger_kind,
                exchange_ts=exchange_ts,
                received_at=received_at,
                now_ns=now_ns,
                direction=direction,
                samples=samples,
                confirmation_age_ms=confirmation_age_ms,
                confirmation_source=confirmation_source,
            )
        except ValueError as exc:
            self._counts["context_errors"] += 1
            self._rejection_reasons[f"context_error:{type(exc).__name__}"] += 1
            trace.append(
                PipelineStage("event_context", "error", self._elapsed(freeze_started), str(exc))
            )
            return self._journal(
                EventTriggerDecision(
                    symbol.upper(),
                    received_at,
                    trigger_kind,
                    None,
                    (),
                    (f"context_error:{type(exc).__name__}",),
                    None,
                    tuple(trace),
                    self._elapsed(event_started_ns),
                    self._feed_delay_us(exchange_ts, received_at),
                )
            )
        trace.append(PipelineStage("event_context", "complete", self._elapsed(freeze_started)))
        candidates: list[SignalCandidate] = []
        reasons: list[str] = []
        for scanner in self.scanners:
            scanner_started = perf_counter_ns()
            try:
                candidate = scanner.evaluate(context)
            except Exception as exc:  # noqa: BLE001 - isolated plugin boundary
                reason = f"{scanner.scanner_id}:scanner_error:{type(exc).__name__}"
                reasons.append(reason)
                self._counts["scanner_errors"] += 1
                self._rejection_reasons[reason] += 1
                trace.append(
                    PipelineStage(
                        f"scanner:{scanner.scanner_id}",
                        "error",
                        self._elapsed(scanner_started),
                        f"{type(exc).__name__}: {str(exc)[:180]}",
                    )
                )
                continue
            if candidate is not None:
                try:
                    score = self.indicator_scorer.score_event_candidate(context, candidate)
                except Exception as exc:  # noqa: BLE001 - advisory boundary
                    self._counts["indicator_scoring_errors"] += 1
                    candidate = replace(
                        candidate,
                        metadata={
                            **dict(candidate.metadata),
                            "indicator_family_score_error": type(exc).__name__,
                        },
                    )
                else:
                    candidate = replace(
                        candidate,
                        metadata={
                            **dict(candidate.metadata),
                            "indicator_family_score": score.to_dict(),
                        },
                    )
                candidates.append(candidate)
            else:
                self._counts["scanner_no_signal"] += 1
            trace.append(
                PipelineStage(
                    f"scanner:{scanner.scanner_id}",
                    "candidate" if candidate else "no_signal",
                    self._elapsed(scanner_started),
                )
            )
        gate_started = perf_counter_ns()
        accepted: list[SignalCandidate] = []
        for candidate in candidates:
            failed = candidate_gate_failures(candidate, self.gates)
            if failed:
                detailed = [f"{candidate.scanner_id}:{reason}" for reason in failed]
                reasons.extend(detailed)
                self._counts["gate_rejected"] += 1
                self._rejection_reasons.update(detailed)
            else:
                accepted.append(candidate)
        trace.append(
            PipelineStage(
                "shared_candidate_gates",
                "complete",
                self._elapsed(gate_started),
                f"{len(accepted)}/{len(candidates)} accepted",
            )
        )
        selected = max(accepted, key=lambda row: row.rank_score) if accepted else None
        duplicate = False
        selected_cooldown_key: tuple[str, str, str] | None = None
        if selected is not None:
            duplicate = selected.dedup_key in self._seen
            if duplicate:
                reason = f"{selected.scanner_id}:duplicate_decision"
                reasons.append(reason)
                self._rejection_reasons[reason] += 1
                selected = None
            else:
                cooldown_key = (selected.scanner_id, selected.symbol, selected.side.value)
                previous_ns = self._last_selected_ns.get(cooldown_key)
                if previous_ns is not None and (
                    now_ns - previous_ns < self.config.candidate_cooldown_ms * 1_000_000
                ):
                    reason = f"{selected.scanner_id}:candidate_cooldown"
                    reasons.append(reason)
                    self._rejection_reasons[reason] += 1
                    self._counts["cooldown_blocked"] += 1
                    selected = None
                else:
                    selected_cooldown_key = cooldown_key
        # A rejected raw candidate is evidence, not an active observation.
        # Locking before shared economic gates caused permanently unqualified
        # setups to suppress later independent candidates.  Ownership of the
        # one-active policy starts only after a candidate has passed every
        # shared gate and survived deduplication/cooldown.
        if selected is not None and self.config.one_active_observation_per_symbol:
            active_until = state.observation_active_until_ns
            if active_until is not None and now_ns < active_until:
                reasons.append("one_active_observation_per_symbol")
                self._rejection_reasons["one_active_observation_per_symbol"] += 1
                self._counts["observation_blocked"] += 1
                selected = None
            else:
                state.observation_active_until_ns = (
                    now_ns + self.config.observation_lock_ms * 1_000_000
                )
        if selected is not None:
            self._seen.add(selected.dedup_key)
            if selected_cooldown_key is not None:
                self._last_selected_ns[selected_cooldown_key] = now_ns
        self._counts["evaluations"] += 1
        self._counts["raw_candidates"] += len(candidates)
        decision = EventTriggerDecision(
            symbol=symbol.upper(),
            decision_ts=received_at,
            trigger_kind=trigger_kind,
            selected=selected,
            evaluated=tuple(candidates),
            rejection_reasons=tuple(reasons),
            context=context,
            pipeline_trace=tuple(trace),
            total_duration_us=self._elapsed(event_started_ns),
            feed_delay_us=self._feed_delay_us(exchange_ts, received_at),
            duplicate=duplicate,
        )
        return self._journal(decision)

    def _journal(self, decision: EventTriggerDecision) -> EventTriggerDecision:
        written: bool | None = None
        if self.journal is not None:
            written = self.journal.append("delta_event_research_decision", decision.to_dict())
            decision = replace(decision, journal_write_success=written)
            if not written and decision.selected is not None:
                decision = replace(
                    decision,
                    selected=None,
                    rejection_reasons=(*decision.rejection_reasons, "journal_unavailable"),
                )
        self._counts["selected"] += int(decision.selected is not None)
        self._feed_latency.add(decision.feed_delay_us)
        self._decision_latency.add(decision.total_duration_us)
        return decision

    @staticmethod
    def _elapsed(started_ns: int) -> int:
        return max(0, (perf_counter_ns() - started_ns) // 1_000)

    @staticmethod
    def _feed_delay_us(exchange_ts: datetime | None, received_at: datetime) -> int | None:
        if exchange_ts is None:
            return None
        return int((_utc(received_at) - _utc(exchange_ts)).total_seconds() * 1_000_000)

    def telemetry(self, *, now_ns: int | None = None) -> dict[str, object]:
        """Read-only bounded telemetry for dashboard/report adapters."""

        return {
            "schema_version": "vnedge.delta_event_trigger_telemetry.v1",
            "counts": dict(self._counts),
            "feed_delay": self._feed_latency.summary(),
            "receive_to_decision": self._decision_latency.summary(),
            "rejection_reasons": dict(self._rejection_reasons.most_common()),
            "funnel": {
                "events": self._counts["events"],
                "trade_book_joined": self._counts["trade_book_joined"],
                "trade_book_join_rejected": self._counts["trade_book_join_rejected"],
                "market_truth_blocked": self._counts["market_truth_blocked"],
                "observation_blocked": self._counts["observation_blocked"],
                "absorption_observations": self._counts["absorption_observations"],
                "counterfactual_observations": self._counts["counterfactual_observations"],
                "evaluations": self._counts["evaluations"],
                "raw_candidates": self._counts["raw_candidates"],
                "gate_rejected": self._counts["gate_rejected"],
                "selected": self._counts["selected"],
                "counterfactual_outcomes": self._counts["counterfactual_outcomes"],
            },
            "market_states": self.market_states(now_ns=now_ns),
            "scanner_telemetry": {
                scanner.scanner_id: scanner.telemetry()
                for scanner in self.scanners
                if callable(getattr(scanner, "telemetry", None))
            },
            "counterfactual_absorption": (
                self.absorption_research.telemetry()
                if self.absorption_research is not None
                else {
                    "status": "not_configured",
                    "research_only": True,
                    "can_trade": False,
                }
            ),
            "config": self.config.model_dump(mode="json"),
            "research_only": True,
            "can_trade": False,
            "can_promote": False,
            "order_route": "absent",
        }

    def market_states(self, *, now_ns: int | None = None) -> dict[str, object]:
        """Publish actual event-time state without inventing unavailable HTF data."""

        rows: dict[str, object] = {}
        for symbol in self.config.enabled_symbols:
            state = self._states.get(symbol)
            if state is None:
                rows[symbol] = {"status": "unavailable", "book_healthy": False}
                continue
            if now_ns is None or state.last_book_monotonic_ns is None:
                book_age_ms = None
            else:
                book_age_ms = max(
                    0.0,
                    (now_ns - state.last_book_monotonic_ns) / 1_000_000.0,
                )
            try:
                bids, asks, mid, spread, imbalance = self._book_features(state)
            except ValueError:
                bids, asks, mid, spread, imbalance = (), (), None, None, None
            total_flow = state.buy_usd + state.sell_usd
            flow = (state.buy_usd - state.sell_usd) / total_flow if total_flow else 0.0
            htf = state.htf
            truth_blockers = self._market_truth_blockers(state, now_ns or 0)
            required_truth_blockers = self._required_market_truth_blockers(truth_blockers)
            basis_bps = (
                (state.mark_price / state.spot_price - 1.0) * 10_000.0
                if state.mark_price is not None and state.spot_price is not None
                else None
            )
            instrument = self.config.absorption.instrument(symbol)
            contract_value = instrument.contract_value if instrument is not None else 1.0
            oi_notional = (
                state.open_interest * contract_value * state.mark_price
                if state.open_interest is not None and state.mark_price is not None
                else None
            )
            rows[symbol] = {
                "status": "healthy" if state.book_healthy and mid is not None else "unavailable",
                "available_at": (
                    state.last_received_at.isoformat() if state.last_received_at else None
                ),
                "source_exchange_ts": (
                    state.last_source_exchange_ts.isoformat()
                    if state.last_source_exchange_ts
                    else None
                ),
                "source_latency": {
                    "decision_eligible": state.last_source_decision_eligible,
                    "classification": state.last_source_delay_classification,
                    "ordering_clock": "local_receive_availability",
                },
                "price": state.last_trade_price or mid,
                "mid": mid,
                "best_bid": bids[0].price if bids else None,
                "best_ask": asks[0].price if asks else None,
                "spread_bps": spread,
                "book_imbalance": imbalance,
                "flow_imbalance": flow,
                "book_age_ms": book_age_ms,
                "book_sequence": state.last_book_sequence,
                "book_healthy": state.book_healthy,
                "active_confirmation": state.confirmation_direction,
                "confirmation_samples": state.confirmation_samples,
                "market_truth": {
                    "ready": not required_truth_blockers,
                    "complete": not truth_blockers,
                    "blockers": list(truth_blockers),
                    "required_blockers": list(required_truth_blockers),
                    "trade_book_join_ok": state.last_trade_book_join_ok,
                    "trade_book_join_lag_ms": state.last_trade_book_join_lag_ms,
                    "trade_book_join_reason": state.last_trade_book_join_reason,
                    "mark_price": state.mark_price,
                    "spot_price": state.spot_price,
                    "basis_bps": basis_bps,
                    "funding_rate": state.funding_rate,
                    "open_interest": state.open_interest,
                    "open_interest_delta": state.open_interest_delta,
                    "open_interest_notional_usd": oi_notional,
                },
                "htf": {
                    "available": htf is not None,
                    "available_at": htf.available_at.isoformat() if htf else None,
                    "bias": htf.bias if htf else None,
                    "regime": htf.regime if htf else "unavailable",
                    "vwap_distance_bps": htf.vwap_distance_bps if htf else None,
                    "cusum_state": htf.cusum_state if htf else "unavailable",
                },
                "research_only": True,
                "can_trade": False,
            }
        return rows

    def absorption_dashboard(self, symbol: str, *, now_ns: int) -> dict[str, object]:
        """Read-only footprint/timeline payload; no controls or order hooks."""

        state = self._state(symbol)
        detector = state.absorption_detector
        if detector is None:
            return {
                "schema_version": "vnedge.absorption_dashboard.v1",
                "symbol": symbol.upper(),
                "status": "not_configured",
                "research_only": True,
                "can_trade": False,
            }
        bids = tuple((level.price, level.size) for level in state.bids.top(20))
        asks = tuple((level.price, level.size) for level in state.asks.top(20))
        return {
            "symbol": symbol.upper(),
            "status": "active" if state.book_healthy else "book_unhealthy",
            **detector.dashboard_snapshot(bids=bids, asks=asks, now_ns=now_ns),
        }


class DeltaVerifiedEventBridge:
    """Translate verified recorder envelopes into the typed event layer.

    ``integrity_verified`` is deliberately mandatory.  An archived raw book
    message must first pass the recorder's sequence/checksum verification (or
    full-shard verification during replay); unverified tape cannot become a
    scanner input.
    """

    def __init__(
        self,
        trigger: EventDrivenTriggerLayer,
        *,
        trade_observer: Callable[[TradeEvent], None] | None = None,
    ) -> None:
        self.trigger = trigger
        self.trade_observer = trade_observer

    def consume(
        self,
        envelope: Mapping[str, object],
        *,
        integrity_verified: bool,
    ) -> EventTriggerDecision | None:
        if not integrity_verified:
            raise ValueError("refusing unverified Delta event envelope")
        if envelope.get("record_kind") != "exchange":
            return None
        raw_text = envelope.get("raw_text")
        if not isinstance(raw_text, str):
            raise TypeError("verified envelope is missing raw_text")
        try:
            message = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise ValueError("verified envelope contains invalid JSON") from exc
        if not isinstance(message, dict):
            raise TypeError("verified envelope JSON must be an object")
        channel = str(envelope.get("channel") or message.get("type") or "")
        symbol_value = envelope.get("symbol") or message.get("sy") or message.get("symbol")
        if symbol_value is None:
            return None
        symbol = str(symbol_value).split(":", 1)[-1].upper()
        received_ns = self._required_int(envelope, "local_recv_ns")
        monotonic_ns = self._required_int(envelope, "local_monotonic_ns")
        received_at = datetime.fromtimestamp(received_ns / 1_000_000_000, tz=UTC)
        exchange_raw = envelope.get("exchange_timestamp_us")
        exchange_ts = (
            datetime.fromtimestamp(int(exchange_raw) / 1_000_000, tz=UTC)
            if exchange_raw is not None
            else None
        )
        corrected_delay = envelope.get("corrected_feed_delay_us")
        delay_classification = str(
            envelope.get("feed_delay_classification")
            or (
                classify_corrected_feed_delay(int(corrected_delay))
                if isinstance(corrected_delay, int)
                else "UNKNOWN"
            )
        ).upper()
        explicit_eligibility = envelope.get("decision_eligible")
        decision_eligible = (
            bool(explicit_eligibility)
            if isinstance(explicit_eligibility, bool)
            else delay_classification == "ON_TIME"
            if isinstance(corrected_delay, int)
            else True  # compatibility with recordings made before latency telemetry
        )
        if channel == "ob_updates":
            return self.trigger.on_l2(
                L2Event(
                    symbol=symbol,
                    bids=self._levels(message.get("b")),
                    asks=self._levels(message.get("a")),
                    sequence=int(message["seq"]),
                    is_snapshot=message.get("action") == "snapshot",
                    exchange_ts=exchange_ts,
                    received_at=received_at,
                    received_monotonic_ns=monotonic_ns,
                    checksum_healthy=True,
                    decision_eligible=decision_eligible,
                    source_delay_classification=delay_classification,
                )
            )
        if channel == "trades":
            trade = parse_public_trade(message)
            trade_exchange_ts = (
                datetime.fromtimestamp(trade.trade_timestamp_us / 1_000_000, tz=UTC)
                if trade.trade_timestamp_us is not None
                else exchange_ts
            )
            event = TradeEvent(
                symbol=symbol,
                price=trade.price,
                size=trade.size,
                side=trade.aggressor_side,
                exchange_ts=trade_exchange_ts,
                received_at=received_at,
                received_monotonic_ns=monotonic_ns,
                publish_ts=(
                    datetime.fromtimestamp(
                        int(envelope["publish_timestamp_us"]) / 1_000_000,
                        tz=UTC,
                    )
                    if envelope.get("publish_timestamp_us") is not None
                    else exchange_ts
                ),
                decision_eligible=decision_eligible,
                source_delay_classification=delay_classification,
            )
            if self.trade_observer is not None:
                self.trade_observer(event)
            return self.trigger.on_trade(event)
        if channel in {"mark_price", "spot_price"}:
            price = self._optional_number(message, "p", "price", "sp")
            if price is not None:
                self.trigger.on_reference_price(
                    ReferencePriceEvent(
                        symbol=symbol,
                        kind="mark" if channel == "mark_price" else "spot",
                        price=price,
                        exchange_ts=exchange_ts,
                        received_at=received_at,
                        received_monotonic_ns=monotonic_ns,
                    )
                )
            return None
        if channel == "funding_rate":
            return self.trigger.on_funding_or_oi(
                FundingOpenInterestEvent(
                    symbol=symbol,
                    funding_rate=parse_funding_rate_decimal(message),
                    open_interest=None,
                    exchange_ts=exchange_ts,
                    received_at=received_at,
                    received_monotonic_ns=monotonic_ns,
                )
            )
        if channel == "ticker":
            # Delta ticker carries both the mark (d[].m) and spot index (sp).
            spot = self._optional_number(message, "sp")
            rows = message.get("d")
            ticker_row = (
                rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}
            )
            mark = self._optional_number(ticker_row, "m")
            for kind, price in (("mark", mark), ("spot", spot)):
                if price is not None:
                    self.trigger.on_reference_price(
                        ReferencePriceEvent(
                            symbol=symbol,
                            kind=kind,
                            price=price,
                            exchange_ts=exchange_ts,
                            received_at=received_at,
                            received_monotonic_ns=monotonic_ns,
                        )
                    )
            open_interest = parse_ticker_open_interest(message, symbol)
            if open_interest is None:
                return None
            return self.trigger.on_funding_or_oi(
                FundingOpenInterestEvent(
                    symbol=symbol,
                    funding_rate=None,
                    open_interest=open_interest,
                    exchange_ts=exchange_ts,
                    received_at=received_at,
                    received_monotonic_ns=monotonic_ns,
                )
            )
        return None

    @staticmethod
    def _optional_number(message: Mapping[str, object], *keys: str) -> float | None:
        for key in keys:
            value = message.get(key)
            try:
                number = float(value) if value is not None else None
            except (TypeError, ValueError):
                continue
            if number is not None and number > 0:
                return number
        return None

    @staticmethod
    def _required_int(envelope: Mapping[str, object], key: str) -> int:
        try:
            value = int(envelope[key])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"verified envelope is missing valid {key}") from exc
        if value < 0:
            raise ValueError(f"verified envelope {key} cannot be negative")
        return value

    @staticmethod
    def _levels(value: object) -> tuple[BookLevel, ...]:
        if value is None:
            return ()
        if not isinstance(value, list):
            raise TypeError("Delta book levels must be a list")
        levels: list[BookLevel] = []
        for row in value:
            if not isinstance(row, list | tuple) or len(row) < 2:
                raise ValueError("Delta book level must contain price and size")
            levels.append(BookLevel(float(row[0]), float(row[1])))
        return tuple(levels)
