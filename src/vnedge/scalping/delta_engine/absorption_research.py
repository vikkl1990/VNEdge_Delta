"""Orderless event-time outcomes for absorption research observations."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from statistics import fmean, median

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vnedge.execution.journal import DecisionJournal
from vnedge.scalping.delta_engine.absorption import (
    AbsorptionInstrumentConfig,
    AbsorptionObservation,
)
from vnedge.scalping.delta_engine.fee_model import DeltaFeeModel


def _utc(ts: datetime) -> datetime:
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)


class AbsorptionResearchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target_1_ticks: float = Field(default=8.0, gt=0)
    target_2_ticks: float = Field(default=12.0, gt=0)
    stop_ticks: float = Field(default=6.0, gt=0)
    horizon_ms: int = Field(default=90_000, ge=1_000, le=3_600_000)
    entry_timeout_ms: int = Field(default=5_000, ge=100, le=60_000)

    @model_validator(mode="after")
    def validate_targets(self) -> AbsorptionResearchConfig:
        if self.target_2_ticks < self.target_1_ticks:
            raise ValueError("target_2_ticks must be >= target_1_ticks")
        return self


@dataclass(frozen=True)
class AbsorptionResearchOutcome:
    key: str
    symbol: str
    event: AbsorptionObservation
    decision_ts: str
    entry_ts: str | None
    realized_exit_ts: str | None
    resolved_ts: str
    entry_price: float | None
    realized_exit_price: float | None
    realized_exit_reason: str
    mfe_ticks: float
    mae_ticks: float
    realized_gross_ticks: float
    cost_ticks: float
    realized_net_ticks: float
    time_to_mfe_ms: float
    hit_target_1: bool
    hit_target_2: bool
    stopped_out: bool
    was_stacked: bool
    had_liquidation_confluence: bool
    volume_percentile: float
    research_only: bool = True
    can_trade: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            **self.__dict__,
            "event": self.event.to_dict(),
            "research_only": True,
            "can_trade": False,
            "order_route": "absent",
        }


@dataclass
class _Pending:
    observation: AbsorptionObservation
    decision_ts: datetime
    volume_percentile: float


@dataclass
class _Open:
    observation: AbsorptionObservation
    decision_ts: datetime
    entry_ts: datetime
    entry_ns: int
    entry_price: float
    volume_percentile: float
    mfe_ticks: float = 0.0
    mae_ticks: float = 0.0
    time_to_mfe_ms: float = 0.0
    hit_target_1: bool = False
    hit_target_2: bool = False
    realized_exit_price: float | None = None
    realized_exit_ns: int | None = None
    realized_exit_reason: str | None = None


@dataclass(frozen=True)
class AbsorptionResearchSummary:
    observations: int
    completed: int
    missed_entries: int
    target_1_win_rate: float
    average_mfe_mae_ratio: float
    expectancy_net_ticks: float
    profit_factor: float
    stacked_win_rate: float | None
    single_win_rate: float | None
    liquidation_win_rate: float | None
    no_liquidation_win_rate: float | None
    median_time_to_mfe_ms: float
    false_absorption_rate: float

    def to_dict(self) -> dict[str, object]:
        return self.__dict__.copy()


def summarize_absorption_outcomes(
    outcomes: tuple[AbsorptionResearchOutcome, ...] | list[AbsorptionResearchOutcome],
) -> AbsorptionResearchSummary:
    """Aggregate any pre-filtered period (for example one UTC week)."""

    rows = list(outcomes)
    completed = [row for row in rows if row.realized_exit_reason != "missed_entry"]
    wins = [row.realized_exit_reason == "target_1" for row in completed]

    def grouped_rate(values: list[AbsorptionResearchOutcome]) -> float | None:
        if not values:
            return None
        return fmean(row.realized_exit_reason == "target_1" for row in values)

    net = [row.realized_net_ticks for row in completed]
    gains = sum(value for value in net if value > 0)
    losses = abs(sum(value for value in net if value < 0))
    profit_factor = gains / losses if losses > 0 else (float("inf") if gains > 0 else 0.0)
    average_mfe = fmean(row.mfe_ticks for row in completed) if completed else 0.0
    average_mae = fmean(row.mae_ticks for row in completed) if completed else 0.0
    return AbsorptionResearchSummary(
        observations=len(rows),
        completed=len(completed),
        missed_entries=len(rows) - len(completed),
        target_1_win_rate=fmean(wins) if wins else 0.0,
        average_mfe_mae_ratio=(
            average_mfe / average_mae
            if average_mae > 0
            else (float("inf") if average_mfe > 0 else 0.0)
        ),
        expectancy_net_ticks=fmean(net) if net else 0.0,
        profit_factor=profit_factor,
        stacked_win_rate=grouped_rate([row for row in completed if row.was_stacked]),
        single_win_rate=grouped_rate([row for row in completed if not row.was_stacked]),
        liquidation_win_rate=grouped_rate(
            [row for row in completed if row.had_liquidation_confluence]
        ),
        no_liquidation_win_rate=grouped_rate(
            [row for row in completed if not row.had_liquidation_confluence]
        ),
        median_time_to_mfe_ms=(
            median(row.time_to_mfe_ms for row in completed) if completed else 0.0
        ),
        false_absorption_rate=(
            fmean(row.stopped_out for row in completed) if completed else 0.0
        ),
    )


class AbsorptionResearchTracker:
    """Next-trade entry and event-time MFE/MAE; never creates a position."""

    def __init__(
        self,
        fee_model: DeltaFeeModel,
        instruments: tuple[AbsorptionInstrumentConfig, ...],
        *,
        config: AbsorptionResearchConfig | None = None,
        journal: DecisionJournal | None = None,
    ) -> None:
        self.fee_model = fee_model
        self.instruments = {row.symbol: row for row in instruments}
        self.config = config or AbsorptionResearchConfig()
        self.journal = journal
        self._seen: set[str] = set()
        self._pending: dict[str, list[_Pending]] = {}
        self._open: dict[str, list[_Open]] = {}
        self._event_volumes: dict[str, list[float]] = {}
        self._counts: Counter[str] = Counter()
        self._net_ticks = 0.0
        self._gross_ticks = 0.0
        self._cost_ticks = 0.0
        self._gains = 0.0
        self._losses = 0.0

    @staticmethod
    def key(observation: AbsorptionObservation) -> str:
        return (
            f"absorption:{observation.symbol}:{observation.detected_monotonic_ns}:"
            f"{observation.reversal_direction}:{observation.price}"
        )

    def register(self, observation: AbsorptionObservation, *, decision_ts: datetime) -> bool:
        key = self.key(observation)
        if key in self._seen:
            self._counts["duplicate_observations"] += 1
            return False
        instrument = self.instruments.get(observation.symbol)
        if instrument is None:
            raise ValueError(f"no absorption research instrument for {observation.symbol}")
        self._seen.add(key)
        volumes = self._event_volumes.setdefault(observation.symbol, [])
        volumes.append(observation.aggressive_notional_usd)
        rank = sum(value <= observation.aggressive_notional_usd for value in volumes)
        percentile = rank / len(volumes)
        self._pending.setdefault(observation.symbol, []).append(
            _Pending(observation, _utc(decision_ts), percentile)
        )
        self._counts["observations_registered"] += 1
        if self.journal is not None:
            self.journal.append(
                "delta_absorption_research_observation",
                {
                    "key": key,
                    "decision_ts": _utc(decision_ts).isoformat(),
                    "symbol": observation.symbol,
                    "volume_percentile": percentile,
                    "event": observation.to_dict(),
                    "research_only": True,
                    "can_trade": False,
                    "order_route": "absent",
                },
            )
        return True

    def on_trade(
        self,
        symbol: str,
        *,
        price: float,
        received_at: datetime,
        monotonic_ns: int,
    ) -> tuple[AbsorptionResearchOutcome, ...]:
        native = symbol.upper()
        instrument = self.instruments.get(native)
        if instrument is None:
            return ()
        if price <= 0 or monotonic_ns < 0:
            raise ValueError("research trade price/timestamp is invalid")
        now = _utc(received_at)
        outcomes: list[AbsorptionResearchOutcome] = []
        still_pending: list[_Pending] = []
        for pending in self._pending.get(native, []):
            age_ns = monotonic_ns - pending.observation.detected_monotonic_ns
            if age_ns <= 0:
                still_pending.append(pending)
            elif age_ns > self.config.entry_timeout_ms * 1_000_000:
                outcomes.append(self._missed(pending, resolved_ts=now))
            else:
                self._open.setdefault(native, []).append(
                    _Open(
                        observation=pending.observation,
                        decision_ts=pending.decision_ts,
                        entry_ts=now,
                        entry_ns=monotonic_ns,
                        entry_price=price,
                        volume_percentile=pending.volume_percentile,
                    )
                )
        self._pending[native] = still_pending
        active: list[_Open] = []
        for row in self._open.get(native, []):
            outcome = self._update_open(
                row,
                price=price,
                now=now,
                now_ns=monotonic_ns,
                tick_size=instrument.tick_size,
            )
            if outcome is None:
                active.append(row)
            else:
                outcomes.append(outcome)
        self._open[native] = active
        for outcome in outcomes:
            self._counts["outcomes"] += 1
            self._counts[f"exit:{outcome.realized_exit_reason}"] += 1
            if outcome.realized_exit_reason != "missed_entry":
                self._counts["completed"] += 1
                self._net_ticks += outcome.realized_net_ticks
                self._gross_ticks += outcome.realized_gross_ticks
                self._cost_ticks += outcome.cost_ticks
                if outcome.realized_net_ticks > 0:
                    self._gains += outcome.realized_net_ticks
                elif outcome.realized_net_ticks < 0:
                    self._losses += abs(outcome.realized_net_ticks)
            if self.journal is not None:
                self.journal.append("delta_absorption_research_outcome", outcome.to_dict())
        return tuple(outcomes)

    def telemetry(self) -> dict[str, object]:
        """Bounded economic truth for the counterfactual research path."""

        completed = self._counts["completed"]
        return {
            "counts": dict(self._counts),
            "pending": sum(len(rows) for rows in self._pending.values()),
            "open": sum(len(rows) for rows in self._open.values()),
            "average_gross_ticks": self._gross_ticks / completed if completed else 0.0,
            "average_cost_ticks": self._cost_ticks / completed if completed else 0.0,
            "average_net_ticks": self._net_ticks / completed if completed else 0.0,
            "profit_factor": (
                self._gains / self._losses
                if self._losses
                else None
                if self._gains
                else 0.0
            ),
            "research_only": True,
            "can_trade": False,
            "can_promote": False,
            "order_route": "absent",
        }

    def _update_open(
        self,
        row: _Open,
        *,
        price: float,
        now: datetime,
        now_ns: int,
        tick_size: float,
    ) -> AbsorptionResearchOutcome | None:
        direction = row.observation.reversal_direction
        favorable = direction * (price - row.entry_price) / tick_size
        adverse = max(0.0, -favorable)
        favorable = max(0.0, favorable)
        if favorable > row.mfe_ticks:
            row.mfe_ticks = favorable
            row.time_to_mfe_ms = (now_ns - row.entry_ns) / 1_000_000.0
        row.mae_ticks = max(row.mae_ticks, adverse)
        row.hit_target_1 = row.hit_target_1 or favorable >= self.config.target_1_ticks
        row.hit_target_2 = row.hit_target_2 or favorable >= self.config.target_2_ticks
        if row.realized_exit_reason is None:
            if adverse >= self.config.stop_ticks:
                row.realized_exit_reason = "stop"
                row.realized_exit_price = row.entry_price - direction * (
                    self.config.stop_ticks * tick_size
                )
                row.realized_exit_ns = now_ns
            elif favorable >= self.config.target_1_ticks:
                row.realized_exit_reason = "target_1"
                row.realized_exit_price = row.entry_price + direction * (
                    self.config.target_1_ticks * tick_size
                )
                row.realized_exit_ns = now_ns
        if now_ns - row.entry_ns < self.config.horizon_ms * 1_000_000:
            return None
        if row.realized_exit_reason is None:
            row.realized_exit_reason = "horizon"
            row.realized_exit_price = price
            row.realized_exit_ns = now_ns
        return self._resolve(row, resolved_ts=now, tick_size=tick_size)

    def _resolve(
        self,
        row: _Open,
        *,
        resolved_ts: datetime,
        tick_size: float,
    ) -> AbsorptionResearchOutcome:
        assert row.realized_exit_price is not None
        assert row.realized_exit_ns is not None
        assert row.realized_exit_reason is not None
        direction = row.observation.reversal_direction
        gross_ticks = direction * (row.realized_exit_price - row.entry_price) / tick_size
        hold_seconds = max(0.0, (row.realized_exit_ns - row.entry_ns) / 1_000_000_000.0)
        costs = self.fee_model.breakdown(
            row.observation.symbol,
            entry_is_maker=False,
            exit_is_maker=False,
            hold_seconds=hold_seconds,
        )
        cost_ticks = costs.total_bps / 10_000.0 * row.entry_price / tick_size
        return AbsorptionResearchOutcome(
            key=self.key(row.observation),
            symbol=row.observation.symbol,
            event=row.observation,
            decision_ts=row.decision_ts.isoformat(),
            entry_ts=row.entry_ts.isoformat(),
            realized_exit_ts=(
                row.entry_ts
                + timedelta(
                    seconds=(row.realized_exit_ns - row.entry_ns) / 1_000_000_000.0
                )
            ).isoformat(),
            resolved_ts=resolved_ts.isoformat(),
            entry_price=row.entry_price,
            realized_exit_price=row.realized_exit_price,
            realized_exit_reason=row.realized_exit_reason,
            mfe_ticks=row.mfe_ticks,
            mae_ticks=row.mae_ticks,
            realized_gross_ticks=gross_ticks,
            cost_ticks=cost_ticks,
            realized_net_ticks=gross_ticks - cost_ticks,
            time_to_mfe_ms=row.time_to_mfe_ms,
            hit_target_1=row.hit_target_1,
            hit_target_2=row.hit_target_2,
            stopped_out=row.realized_exit_reason == "stop",
            was_stacked=row.observation.is_stacked,
            had_liquidation_confluence=(
                row.observation.liquidation_cluster_side is not None
            ),
            volume_percentile=row.volume_percentile,
        )

    def _missed(
        self,
        row: _Pending,
        *,
        resolved_ts: datetime,
    ) -> AbsorptionResearchOutcome:
        return AbsorptionResearchOutcome(
            key=self.key(row.observation),
            symbol=row.observation.symbol,
            event=row.observation,
            decision_ts=row.decision_ts.isoformat(),
            entry_ts=None,
            realized_exit_ts=None,
            resolved_ts=resolved_ts.isoformat(),
            entry_price=None,
            realized_exit_price=None,
            realized_exit_reason="missed_entry",
            mfe_ticks=0.0,
            mae_ticks=0.0,
            realized_gross_ticks=0.0,
            cost_ticks=0.0,
            realized_net_ticks=0.0,
            time_to_mfe_ms=0.0,
            hit_target_1=False,
            hit_target_2=False,
            stopped_out=False,
            was_stacked=row.observation.is_stacked,
            had_liquidation_confluence=(
                row.observation.liquidation_cluster_side is not None
            ),
            volume_percentile=row.volume_percentile,
        )
