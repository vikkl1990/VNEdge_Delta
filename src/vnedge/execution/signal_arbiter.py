"""Rank and collapse multiple strategy signals into one execution candidate.

The arbiter is deliberately upstream of sizing and the pre-trade gateway:
it decides which signal is worth asking risk about. It never approves
capital, never submits orders, and never bypasses the gateway.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal

from vnedge.strategy.base_strategy import SignalIntent

ExecutionRoute = Literal["MAKER_ONLY", "TAKER_ALLOWED", "BLOCKED", "UNKNOWN"]


@dataclass(frozen=True)
class SignalCandidate:
    """One strategy's entry proposal, with after-cost edge metadata."""

    source_id: str
    strategy_id: str
    symbol: str
    signal: SignalIntent
    expected_edge_bps: float = 0.0
    expected_cost_bps: float = 0.0
    profit_factor: float | None = None
    confidence: float = 1.0
    route: ExecutionRoute = "UNKNOWN"
    planned_notional_usd: float | None = None
    metadata: dict = field(default_factory=dict)

    @property
    def net_edge_bps(self) -> float:
        return self.expected_edge_bps - self.expected_cost_bps


@dataclass(frozen=True)
class ArbiterConfig:
    """Conservative signal-selection policy.

    ``min_net_edge_bps`` is the breakeven floor. Set it above zero when a
    safety margin is required. ``profit_factor`` is optional because some
    signals arrive with expectancy but no robust PF yet; when present, it is
    enforced.
    """

    max_selected: int = 1
    max_per_symbol: int = 1
    min_net_edge_bps: float = 0.0
    min_profit_factor: float = 1.0
    allow_opposite_sides_same_symbol: bool = False
    max_total_planned_notional_usd: float | None = None
    taker_min_profit_factor: float = 1.30
    taker_min_net_edge_bps: float = 2.0
    maker_route_bonus_bps: float = 1.0
    taker_route_penalty_bps: float = 1.0
    confidence_weight_bps: float = 0.0
    profit_factor_weight_bps: float = 0.0

    def __post_init__(self) -> None:
        if self.max_selected < 1:
            raise ValueError("max_selected must be >= 1")
        if self.max_per_symbol < 1:
            raise ValueError("max_per_symbol must be >= 1")
        if self.min_profit_factor < 0:
            raise ValueError("min_profit_factor must be non-negative")
        if self.taker_min_profit_factor < self.min_profit_factor:
            raise ValueError("taker_min_profit_factor must be >= min_profit_factor")
        if self.max_total_planned_notional_usd is not None and (
            self.max_total_planned_notional_usd <= 0
        ):
            raise ValueError("max_total_planned_notional_usd must be positive")


@dataclass(frozen=True)
class SignalRejection:
    candidate: SignalCandidate
    reason: str


@dataclass(frozen=True)
class ArbitrationDecision:
    selected: tuple[SignalCandidate, ...]
    rejected: tuple[SignalRejection, ...]

    @property
    def approved(self) -> bool:
        return bool(self.selected)

    @property
    def best(self) -> SignalCandidate | None:
        return self.selected[0] if self.selected else None

    def to_signal(self) -> SignalIntent | None:
        """Return the winning intent, annotated for the decision journal."""
        best = self.best
        if best is None:
            return None
        suffix = (
            f"arbiter_selected source={best.source_id} "
            f"net_edge_bps={best.net_edge_bps:.2f} route={best.route}"
        )
        reason = f"{best.signal.reason}; {suffix}" if best.signal.reason else suffix
        return replace(best.signal, reason=reason)


class SignalArbiter:
    """Apply breakeven, PF, route, and conflict policy to raw candidates."""

    def __init__(self, config: ArbiterConfig = ArbiterConfig()) -> None:
        self.config = config

    def arbitrate(
        self,
        candidates: tuple[SignalCandidate, ...] | list[SignalCandidate],
    ) -> ArbitrationDecision:
        rejected: list[SignalRejection] = []
        eligible: list[SignalCandidate] = []

        for raw_candidate in candidates:
            candidate = self._with_effective_route(raw_candidate)
            reason = self._eligibility_rejection(candidate)
            if reason is None:
                eligible.append(candidate)
            else:
                rejected.append(SignalRejection(candidate, reason))

        selected: list[SignalCandidate] = []
        per_symbol: dict[str, int] = {}
        cumulative_notional = 0.0

        for candidate in sorted(eligible, key=self._score, reverse=True):
            if len(selected) >= self.config.max_selected:
                rejected.append(SignalRejection(candidate, "max_selected_reached"))
                continue

            count = per_symbol.get(candidate.symbol, 0)
            if count >= self.config.max_per_symbol:
                rejected.append(SignalRejection(candidate, "max_per_symbol_reached"))
                continue

            if not self.config.allow_opposite_sides_same_symbol:
                conflict = next(
                    (
                        winner
                        for winner in selected
                        if winner.symbol == candidate.symbol
                        and winner.signal.side != candidate.signal.side
                    ),
                    None,
                )
                if conflict is not None:
                    rejected.append(
                        SignalRejection(
                            candidate,
                            f"opposite_side_conflict_with={conflict.source_id}",
                        )
                    )
                    continue

            planned = candidate.planned_notional_usd
            cap = self.config.max_total_planned_notional_usd
            if planned is not None and cap is not None:
                if cumulative_notional + planned > cap:
                    rejected.append(SignalRejection(candidate, "notional_budget_exceeded"))
                    continue
                cumulative_notional += planned

            selected.append(candidate)
            per_symbol[candidate.symbol] = count + 1

        return ArbitrationDecision(tuple(selected), tuple(rejected))

    def _eligibility_rejection(self, candidate: SignalCandidate) -> str | None:
        if candidate.route not in {"MAKER_ONLY", "TAKER_ALLOWED", "BLOCKED", "UNKNOWN"}:
            return "route_invalid"
        if candidate.route == "BLOCKED":
            return "route_blocked"
        if candidate.net_edge_bps < self.config.min_net_edge_bps:
            return "below_breakeven_net_edge"
        if (
            candidate.profit_factor is not None
            and candidate.profit_factor < self.config.min_profit_factor
        ):
            return "below_min_profit_factor"
        if not 0.0 <= candidate.confidence <= 1.0:
            return "confidence_out_of_range"
        if candidate.planned_notional_usd is not None and candidate.planned_notional_usd <= 0:
            return "planned_notional_invalid"
        return None

    def _with_effective_route(self, candidate: SignalCandidate) -> SignalCandidate:
        if candidate.route != "UNKNOWN":
            return candidate
        if (
            candidate.profit_factor is not None
            and candidate.profit_factor >= self.config.taker_min_profit_factor
            and candidate.net_edge_bps >= self.config.taker_min_net_edge_bps
        ):
            return replace(candidate, route="TAKER_ALLOWED")
        return replace(candidate, route="MAKER_ONLY")

    def _score(self, candidate: SignalCandidate) -> float:
        route_adjustment = 0.0
        if candidate.route == "MAKER_ONLY":
            route_adjustment += self.config.maker_route_bonus_bps
        elif candidate.route == "TAKER_ALLOWED":
            route_adjustment -= self.config.taker_route_penalty_bps

        pf_score = 0.0
        if candidate.profit_factor is not None:
            pf_score = min(candidate.profit_factor, 5.0) * self.config.profit_factor_weight_bps

        return (
            candidate.net_edge_bps
            + route_adjustment
            + candidate.confidence * self.config.confidence_weight_bps
            + pf_score
        )
