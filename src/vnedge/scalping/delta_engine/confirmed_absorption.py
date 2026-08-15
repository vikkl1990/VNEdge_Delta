"""Confirmed event-time absorption reversal research scanner.

Absorption is a setup, never an entry.  A candidate is created only after
the public tape and displayed book sustain the proposed reversal direction
and price reclaims the absorbed level.  The scanner is stateful by design,
but its state depends exclusively on immutable event snapshots and their
recorded availability timestamps, so live and replay paths remain identical.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vnedge.scalping.delta_engine.absorption import AbsorptionObservation
from vnedge.scalping.delta_engine.event_trigger import EventMarketSnapshot
from vnedge.scalping.delta_engine.fee_model import DeltaFeeModel
from vnedge.scalping.delta_engine.types import Side, SignalCandidate


class ConfirmedAbsorptionConfig(BaseModel):
    """Frozen V3 research contract; parameters are not runtime-tunable."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tick_sizes: Mapping[str, float]
    pending_expiry_ms: int = Field(default=5_000, ge=500, le=30_000)
    minimum_reclaim_ticks: float = Field(default=2.0, gt=0, le=20)
    maximum_adverse_ticks: float = Field(default=2.0, gt=0, le=20)
    minimum_confirmation_ms: int = Field(default=400, ge=100, le=5_000)
    minimum_confirmation_samples: int = Field(default=3, ge=2, le=100)
    minimum_flow_imbalance: float = Field(default=0.60, gt=0, le=1)
    minimum_book_imbalance: float = Field(default=0.40, gt=0, le=1)
    required_trend_coverage_seconds: int = Field(default=300, ge=0, le=900)
    trend_veto_bps: float = Field(default=5.0, ge=0, le=100)
    stop_bps: float = Field(default=20.0, gt=0)
    target_bps: float = Field(default=45.0, gt=0)
    time_stop_seconds: int = Field(default=900, gt=0, le=1_800)
    minimum_target_cost_multiple: float = Field(default=2.5, ge=1.0, le=10.0)
    probability_prior: float = Field(default=0.50, ge=0, le=1)
    allowed_sides: tuple[str, ...] = ("long", "short")
    entry_is_maker: bool = False

    @model_validator(mode="after")
    def validate_contract(self) -> ConfirmedAbsorptionConfig:
        ticks = {str(symbol).upper(): float(value) for symbol, value in self.tick_sizes.items()}
        if not ticks or any(value <= 0 for value in ticks.values()):
            raise ValueError("confirmed absorption requires positive symbol tick sizes")
        sides = tuple(str(side).lower() for side in self.allowed_sides)
        if not sides or len(set(sides)) != len(sides) or any(
            side not in {"long", "short"} for side in sides
        ):
            raise ValueError("allowed_sides must contain unique long/short values")
        object.__setattr__(self, "tick_sizes", ticks)
        object.__setattr__(self, "allowed_sides", sides)
        return self


@dataclass
class _PendingSetup:
    observation: AbsorptionObservation
    created_at: datetime
    setup_mid: float
    lowest_mid: float
    highest_mid: float
    confirmation_started_at: datetime | None = None
    confirmation_last_ts: datetime | None = None
    confirmation_samples: int = 0


class ConfirmedAbsorptionReversalScanner:
    """V3: wait for causal reversal proof after displayed absorption."""

    scanner_id = "event_absorption_confirmed_reversal_v3_shadow"
    observes_unconfirmed_events = True

    def __init__(
        self,
        fee_model: DeltaFeeModel,
        config: ConfirmedAbsorptionConfig,
    ) -> None:
        self.fee_model = fee_model
        self.config = config
        for symbol in self.config.tick_sizes:
            costs = self.fee_model.breakdown(
                symbol,
                entry_is_maker=self.config.entry_is_maker,
                exit_is_maker=False,
                hold_seconds=self.config.time_stop_seconds,
            )
            required_target = costs.total_bps * self.config.minimum_target_cost_multiple
            if self.config.target_bps < required_target:
                raise ValueError(
                    "confirmed absorption target does not clear the frozen cost multiple: "
                    f"{symbol} target={self.config.target_bps:.3f}bps "
                    f"required={required_target:.3f}bps"
                )
        self._pending: dict[str, _PendingSetup] = {}
        self._counts: dict[str, int] = {
            "setups": 0,
            "confirmed": 0,
            "expired": 0,
            "adverse_invalidations": 0,
            "trend_vetoes": 0,
            "flow_rejections": 0,
            "confirmation_waits": 0,
            "confirmation_resets": 0,
            "price_rejections": 0,
        }

    def evaluate(self, context: EventMarketSnapshot) -> SignalCandidate | None:
        symbol = context.symbol.upper()
        if context.confirmation_source == "absorption" and context.absorption is not None:
            self._register_setup(context)
            return None

        pending = self._pending.get(symbol)
        if pending is None:
            return None
        pending.lowest_mid = min(pending.lowest_mid, context.mid)
        pending.highest_mid = max(pending.highest_mid, context.mid)

        age_ms = (context.decision_ts - pending.created_at).total_seconds() * 1_000.0
        if age_ms < 0 or age_ms > self.config.pending_expiry_ms:
            self._pending.pop(symbol, None)
            self._counts["expired"] += 1
            return None
        direction = pending.observation.reversal_direction
        tick_size = self.config.tick_sizes[symbol]
        if self._breached_extreme(pending, direction, tick_size):
            self._pending.pop(symbol, None)
            self._counts["adverse_invalidations"] += 1
            return None
        if not self._post_setup_flow_confirmed(pending, context, direction):
            return None
        if not self._price_confirmed(context, pending, direction, tick_size):
            self._counts["price_rejections"] += 1
            return None
        if self._trend_vetoed(context, direction):
            self._pending.pop(symbol, None)
            self._counts["trend_vetoes"] += 1
            return None

        candidate = self._candidate(context, pending)
        self._pending.pop(symbol, None)
        self._counts["confirmed"] += 1
        return candidate

    def telemetry(self) -> dict[str, object]:
        return {
            "scanner_id": self.scanner_id,
            "counts": dict(self._counts),
            "pending_symbols": sorted(self._pending),
            "contract": self.config.model_dump(mode="json"),
            "observation_lock_owner": "event_trigger_layer_after_shared_gates",
            "research_only": True,
            "can_trade": False,
            "can_promote": False,
        }

    def _register_setup(self, context: EventMarketSnapshot) -> None:
        observation = context.absorption
        assert observation is not None
        symbol = context.symbol.upper()
        side = "long" if observation.reversal_direction > 0 else "short"
        if symbol not in self.config.tick_sizes or side not in self.config.allowed_sides:
            return
        existing = self._pending.get(symbol)
        if (
            existing is not None
            and existing.observation.detected_monotonic_ns
            == observation.detected_monotonic_ns
        ):
            return
        self._pending[symbol] = _PendingSetup(
            observation=observation,
            created_at=context.decision_ts,
            setup_mid=context.mid,
            lowest_mid=context.mid,
            highest_mid=context.mid,
        )
        self._counts["setups"] += 1

    def _breached_extreme(
        self,
        pending: _PendingSetup,
        direction: int,
        tick_size: float,
    ) -> bool:
        tolerance = self.config.maximum_adverse_ticks * tick_size
        if direction > 0:
            return pending.lowest_mid < pending.observation.price - tolerance
        return pending.highest_mid > pending.observation.price + tolerance

    def _post_setup_flow_confirmed(
        self,
        pending: _PendingSetup,
        context: EventMarketSnapshot,
        direction: int,
    ) -> bool:
        """Require a new sustained flow sequence that begins after absorption.

        The event engine's confirmation clock may have started before the
        absorption setup.  Reusing that clock would turn pre-setup flow into
        look-alike confirmation.  This scanner therefore owns a separate,
        strictly post-setup streak and resets it on any disagreement.
        """

        valid = (
            context.confirmation_source == "flow_imbalance"
            and context.confirmed_direction == direction
            and direction * context.flow_imbalance >= self.config.minimum_flow_imbalance
            and direction * context.book_imbalance >= self.config.minimum_book_imbalance
        )
        if not valid or context.decision_ts <= pending.created_at:
            self._counts["flow_rejections"] += 1
            if pending.confirmation_started_at is not None:
                self._counts["confirmation_resets"] += 1
            pending.confirmation_started_at = None
            pending.confirmation_last_ts = None
            pending.confirmation_samples = 0
            return False
        if (
            pending.confirmation_last_ts is not None
            and context.decision_ts <= pending.confirmation_last_ts
        ):
            self._counts["flow_rejections"] += 1
            return False
        if pending.confirmation_started_at is None:
            pending.confirmation_started_at = context.decision_ts
            pending.confirmation_samples = 1
        else:
            pending.confirmation_samples += 1
        pending.confirmation_last_ts = context.decision_ts
        elapsed_ms = (
            context.decision_ts - pending.confirmation_started_at
        ).total_seconds() * 1_000.0
        confirmed = (
            pending.confirmation_samples >= self.config.minimum_confirmation_samples
            and elapsed_ms >= self.config.minimum_confirmation_ms
        )
        if not confirmed:
            self._counts["confirmation_waits"] += 1
        return confirmed

    def _price_confirmed(
        self,
        context: EventMarketSnapshot,
        pending: _PendingSetup,
        direction: int,
        tick_size: float,
    ) -> bool:
        reclaim = self.config.minimum_reclaim_ticks * tick_size
        last_trade = float(context.features.get("last_trade_price") or context.mid)
        threshold = pending.observation.price + direction * reclaim
        if direction > 0:
            return context.mid >= threshold and last_trade >= threshold
        return context.mid <= threshold and last_trade <= threshold

    def _trend_vetoed(self, context: EventMarketSnapshot, direction: int) -> bool:
        coverage = float(context.features.get("trend_coverage_seconds") or 0.0)
        if coverage < self.config.required_trend_coverage_seconds:
            return True
        trend_5m = float(context.features.get("trend_5m_bps") or 0.0)
        trend_15m = float(context.features.get("trend_15m_bps") or 0.0)
        veto = self.config.trend_veto_bps
        if direction > 0:
            return trend_5m < -veto or trend_15m < -(2 * veto)
        return trend_5m > veto or trend_15m > 2 * veto

    def _candidate(
        self,
        context: EventMarketSnapshot,
        pending: _PendingSetup,
    ) -> SignalCandidate:
        direction = pending.observation.reversal_direction
        side = Side.LONG if direction > 0 else Side.SHORT
        entry = context.mid
        stop_factor = self.config.stop_bps / 10_000.0
        target_factor = self.config.target_bps / 10_000.0
        stop = entry * (1 - stop_factor if side is Side.LONG else 1 + stop_factor)
        target = entry * (1 + target_factor if side is Side.LONG else 1 - target_factor)
        costs = self.fee_model.breakdown(
            context.symbol,
            entry_is_maker=self.config.entry_is_maker,
            exit_is_maker=False,
            hold_seconds=self.config.time_stop_seconds,
        )
        raw_expectancy = (
            self.config.probability_prior * self.config.target_bps
            - (1.0 - self.config.probability_prior) * self.config.stop_bps
        )
        return SignalCandidate(
            scanner_id=self.scanner_id,
            symbol=context.symbol,
            side=side,
            decision_ts=context.decision_ts,
            entry_price=entry,
            stop_loss=stop,
            take_profits=(target,),
            time_stop_seconds=self.config.time_stop_seconds,
            expected_hold_seconds=self.config.time_stop_seconds,
            expected_move_bps=self.config.target_bps,
            raw_expectancy_bps=raw_expectancy,
            modeled_cost_bps=costs.total_bps,
            fee_adjusted_expectancy_bps=raw_expectancy - costs.total_bps,
            scalper_probability=self.config.probability_prior,
            confidence=pending.observation.strength,
            entry_is_maker=self.config.entry_is_maker,
            metadata={
                "hypothesis": "confirmed_absorption_reversal",
                "setup_detected_at": pending.created_at.isoformat(),
                "setup_price": pending.observation.price,
                "confirmation_delay_ms": (
                    context.decision_ts - pending.created_at
                ).total_seconds()
                * 1_000.0,
                "post_setup_confirmation_ms": (
                    context.decision_ts
                    - (pending.confirmation_started_at or context.decision_ts)
                ).total_seconds()
                * 1_000.0,
                "post_setup_confirmation_samples": pending.confirmation_samples,
                "confirmation_flow_imbalance": context.flow_imbalance,
                "confirmation_book_imbalance": context.book_imbalance,
                "trend_5m_bps": context.features.get("trend_5m_bps", 0.0),
                "trend_15m_bps": context.features.get("trend_15m_bps", 0.0),
                "one_position_lock_seconds": self.config.time_stop_seconds,
                "uncalibrated_probability_prior": True,
                "research_only": True,
                "can_trade": False,
                "can_promote": False,
            },
        )
