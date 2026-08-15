"""Explainable, causal indicator-family scoring for Delta research candidates.

The score is observational metadata.  It does not emit candidates, accept an
order, or change any promotion state.  A family score becomes a gate only
after a separate chronological validation explicitly promotes a future policy
version.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from math import isfinite
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from vnedge.scalping.delta_engine.types import Side, SignalCandidate

if TYPE_CHECKING:
    from vnedge.scalping.delta_engine.event_trigger import EventMarketSnapshot


def _utc(ts: datetime) -> datetime:
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


class IndicatorFamily(str, Enum):
    STRUCTURE = "structure"
    MOMENTUM = "momentum"
    VOLATILITY = "volatility"
    PARTICIPATION = "participation"
    ORDER_FLOW = "order_flow"
    LIQUIDITY = "liquidity"
    ECONOMICS = "economics"
    DATA_QUALITY = "data_quality"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FamilyPolicy(_StrictModel):
    family: IndicatorFamily
    weight: float = Field(gt=0)
    required: bool = False
    minimum_evidence: int = Field(default=1, ge=1)
    minimum_score: float = Field(default=0.0, ge=0, le=100)


class IndicatorScoringConfig(_StrictModel):
    schema_version: Literal["vnedge.indicator_scoring.v1"] = "vnedge.indicator_scoring.v1"
    policy_version: str = "indicator_family_v1.0.0"
    minimum_composite_score: float = Field(default=72.0, ge=0, le=100)
    minimum_total_coverage: float = Field(default=0.55, gt=0, le=1)
    minimum_total_confidence: float = Field(default=0.55, ge=0, le=1)
    book_imbalance_full_scale: float = Field(default=0.75, gt=0, le=1)
    flow_imbalance_full_scale: float = Field(default=0.85, gt=0, le=1)
    vwap_full_scale_bps: float = Field(default=35.0, gt=0)
    maximum_spread_bps: float = Field(default=8.0, gt=0)
    target_depth_usd: float = Field(default=250_000.0, gt=0)
    maximum_book_age_ms: float = Field(default=1_000.0, gt=0)
    maximum_feed_delay_ms: float = Field(default=750.0, gt=0)
    excellent_target_cost_multiple: float = Field(default=5.0, gt=1)
    excellent_reward_risk: float = Field(default=3.0, gt=1)
    families: tuple[FamilyPolicy, ...]
    research_only: Literal[True] = True
    can_trade: Literal[False] = False
    can_promote: Literal[False] = False

    @model_validator(mode="after")
    def validate_policy(self) -> IndicatorScoringConfig:
        names = [row.family for row in self.families]
        if len(names) != len(set(names)):
            raise ValueError("indicator family policies must be unique")
        required = {row.family for row in self.families if row.required}
        must_require = {IndicatorFamily.ECONOMICS, IndicatorFamily.DATA_QUALITY}
        if not must_require.issubset(required):
            raise ValueError("economics and data_quality must be required families")
        return self


def default_indicator_scoring_config() -> IndicatorScoringConfig:
    return IndicatorScoringConfig(
        families=(
            FamilyPolicy(
                family=IndicatorFamily.STRUCTURE,
                weight=20,
                required=True,
                minimum_score=50,
            ),
            FamilyPolicy(family=IndicatorFamily.MOMENTUM, weight=12),
            FamilyPolicy(family=IndicatorFamily.VOLATILITY, weight=8),
            FamilyPolicy(family=IndicatorFamily.PARTICIPATION, weight=8),
            FamilyPolicy(family=IndicatorFamily.ORDER_FLOW, weight=18),
            FamilyPolicy(family=IndicatorFamily.LIQUIDITY, weight=10),
            FamilyPolicy(
                family=IndicatorFamily.ECONOMICS,
                weight=18,
                required=True,
                minimum_evidence=3,
                minimum_score=65,
            ),
            FamilyPolicy(
                family=IndicatorFamily.DATA_QUALITY,
                weight=6,
                required=True,
                minimum_evidence=2,
                minimum_score=85,
            ),
        )
    )


def load_indicator_scoring_config(
    path: Path | str = "configs/research/indicator_family_scoring_v1.yaml",
) -> IndicatorScoringConfig:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("indicator scoring config must be a mapping")
    return IndicatorScoringConfig.model_validate(payload)


@dataclass(frozen=True)
class IndicatorEvidence:
    indicator_id: str
    family: IndicatorFamily
    score: float
    confidence: float
    available_at: datetime
    reason: str
    raw_value: float | int | bool | str | None = None
    weight: float = 1.0
    hard_block: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "available_at", _utc(self.available_at))
        if not self.indicator_id or not self.reason:
            raise ValueError("indicator evidence needs an id and explanation")
        if not isfinite(self.score) or not 0 <= self.score <= 100:
            raise ValueError("indicator score must be finite and in [0, 100]")
        if not isfinite(self.confidence) or not 0 <= self.confidence <= 1:
            raise ValueError("indicator confidence must be finite and in [0, 1]")
        if not isfinite(self.weight) or self.weight <= 0:
            raise ValueError("indicator weight must be finite and positive")

    def to_dict(self) -> dict[str, object]:
        return {
            "indicator_id": self.indicator_id,
            "family": self.family.value,
            "score": round(self.score, 4),
            "confidence": round(self.confidence, 4),
            "available_at": self.available_at.isoformat(),
            "reason": self.reason,
            "raw_value": self.raw_value,
            "weight": self.weight,
            "hard_block": self.hard_block,
        }


@dataclass(frozen=True)
class IndicatorFamilyScore:
    family: IndicatorFamily
    score: float
    confidence: float
    evidence: tuple[IndicatorEvidence, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "family": self.family.value,
            "score": round(self.score, 4),
            "confidence": round(self.confidence, 4),
            "evidence_count": len(self.evidence),
            "evidence": [row.to_dict() for row in self.evidence],
        }


@dataclass(frozen=True)
class IndicatorScoreResult:
    policy_version: str
    symbol: str
    side: Side
    decision_ts: datetime
    composite_score: float
    coverage: float
    confidence: float
    quality_band: str
    research_qualified: bool
    blockers: tuple[str, ...]
    families: tuple[IndicatorFamilyScore, ...]
    research_only: bool = True
    can_trade: bool = False
    can_promote: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "vnedge.indicator_score_result.v1",
            "policy_version": self.policy_version,
            "symbol": self.symbol,
            "side": self.side.value,
            "decision_ts": self.decision_ts.isoformat(),
            "composite_score": round(self.composite_score, 4),
            "coverage": round(self.coverage, 4),
            "confidence": round(self.confidence, 4),
            "quality_band": self.quality_band,
            "research_qualified": self.research_qualified,
            "blockers": list(self.blockers),
            "families": [row.to_dict() for row in self.families],
            "research_only": True,
            "can_trade": False,
            "can_promote": False,
            "used_for_signal": False,
            "used_for_execution": False,
        }


@dataclass(frozen=True)
class CandidateEconomics:
    expected_move_bps: float
    modeled_cost_bps: float
    expected_net_bps: float
    reward_risk: float
    expectancy_calibrated: bool = True

    @classmethod
    def from_candidate(cls, candidate: SignalCandidate) -> CandidateEconomics:
        risk_bps = abs(candidate.entry_price / candidate.stop_loss - 1.0) * 10_000.0
        reward_risk = candidate.expected_move_bps / risk_bps if risk_bps > 0 else 0.0
        return cls(
            expected_move_bps=candidate.expected_move_bps,
            modeled_cost_bps=candidate.modeled_cost_bps,
            expected_net_bps=candidate.fee_adjusted_expectancy_bps,
            reward_risk=reward_risk,
            expectancy_calibrated=not bool(
                candidate.metadata.get("uncalibrated_probability_prior", False)
            ),
        )

    def __post_init__(self) -> None:
        values = (
            self.expected_move_bps,
            self.modeled_cost_bps,
            self.expected_net_bps,
            self.reward_risk,
        )
        if any(not isfinite(value) for value in values):
            raise ValueError("candidate economics must be finite")
        if self.expected_move_bps < 0 or self.modeled_cost_bps <= 0 or self.reward_risk < 0:
            raise ValueError("move/risk must be non-negative and modeled cost positive")


class IndicatorFamilyScorer:
    """Aggregate already-normalized evidence without creating a trade signal."""

    def __init__(self, config: IndicatorScoringConfig | None = None) -> None:
        self.config = config or default_indicator_scoring_config()

    def score(
        self,
        *,
        symbol: str,
        side: Side,
        decision_ts: datetime,
        evidence: Sequence[IndicatorEvidence],
    ) -> IndicatorScoreResult:
        decision_ts = _utc(decision_ts)
        rows = tuple(evidence)
        if any(row.available_at > decision_ts for row in rows):
            raise ValueError("indicator evidence contains future information")
        duplicate_ids = [row.indicator_id for row in rows]
        if len(duplicate_ids) != len(set(duplicate_ids)):
            raise ValueError("indicator evidence ids must be unique")

        blockers = [f"hard_block:{row.indicator_id}" for row in rows if row.hard_block]
        family_scores: list[IndicatorFamilyScore] = []
        policy_by_family = {policy.family: policy for policy in self.config.families}
        for family, policy in policy_by_family.items():
            family_rows = tuple(row for row in rows if row.family is family)
            if len(family_rows) < policy.minimum_evidence:
                if policy.required:
                    blockers.append(f"missing_required_family:{family.value}")
                continue
            effective = [row.weight * row.confidence for row in family_rows]
            denominator = sum(effective)
            if denominator <= 0:
                if policy.required:
                    blockers.append(f"zero_confidence_family:{family.value}")
                continue
            family_score = sum(
                row.score * weight for row, weight in zip(family_rows, effective, strict=True)
            ) / denominator
            base_weight = sum(row.weight for row in family_rows)
            family_confidence = sum(
                row.confidence * row.weight for row in family_rows
            ) / base_weight
            result = IndicatorFamilyScore(
                family=family,
                score=family_score,
                confidence=family_confidence,
                evidence=family_rows,
            )
            family_scores.append(result)
            if policy.required and family_score < policy.minimum_score:
                blockers.append(
                    f"family_below_floor:{family.value}:{family_score:.2f}<{policy.minimum_score:.2f}"
                )

        total_policy_weight = sum(policy.weight for policy in self.config.families)
        present_weight = sum(policy_by_family[row.family].weight for row in family_scores)
        coverage = present_weight / total_policy_weight
        if coverage < self.config.minimum_total_coverage:
            blockers.append(
                f"coverage_below_floor:{coverage:.3f}<{self.config.minimum_total_coverage:.3f}"
            )
        if present_weight:
            composite = sum(
                row.score * policy_by_family[row.family].weight for row in family_scores
            ) / present_weight
            confidence = sum(
                row.confidence * policy_by_family[row.family].weight for row in family_scores
            ) / present_weight
        else:
            composite = 0.0
            confidence = 0.0
        if confidence < self.config.minimum_total_confidence:
            blockers.append(
                f"confidence_below_floor:{confidence:.3f}<{self.config.minimum_total_confidence:.3f}"
            )
        non_score_blocked = bool(blockers)
        if composite < self.config.minimum_composite_score:
            blockers.append(
                f"composite_below_floor:{composite:.2f}<"
                f"{self.config.minimum_composite_score:.2f}"
            )
        qualified = not blockers
        if non_score_blocked:
            quality_band = "blocked"
        elif composite >= 85:
            quality_band = "exceptional"
        elif composite >= self.config.minimum_composite_score:
            quality_band = "strong"
        elif composite >= 60:
            quality_band = "watch"
        else:
            quality_band = "weak"
        return IndicatorScoreResult(
            policy_version=self.config.policy_version,
            symbol=symbol.upper(),
            side=side,
            decision_ts=decision_ts,
            composite_score=composite,
            coverage=coverage,
            confidence=confidence,
            quality_band=quality_band,
            research_qualified=qualified,
            blockers=tuple(blockers),
            families=tuple(family_scores),
        )

    def score_event_candidate(
        self,
        context: EventMarketSnapshot,
        candidate: SignalCandidate,
    ) -> IndicatorScoreResult:
        if context.symbol != candidate.symbol or context.decision_ts != candidate.decision_ts:
            raise ValueError("event context and candidate identity do not match")
        evidence = event_indicator_evidence(
            context,
            candidate.side,
            CandidateEconomics.from_candidate(candidate),
            self.config,
        )
        return self.score(
            symbol=candidate.symbol,
            side=candidate.side,
            decision_ts=candidate.decision_ts,
            evidence=evidence,
        )


def _directional_score(raw_value: float, full_scale: float, side: Side) -> float:
    direction = 1.0 if side is Side.LONG else -1.0
    return _clamp(50.0 + 50.0 * direction * raw_value / full_scale)


def _alignment_score(direction: int, side: Side) -> float:
    if direction == 0:
        return 50.0
    wanted = 1 if side is Side.LONG else -1
    return 100.0 if direction == wanted else 0.0


def _quality_below(value: float, maximum: float) -> float:
    return _clamp(100.0 * (1.0 - value / maximum))


def _economics_evidence(
    economics: CandidateEconomics,
    available_at: datetime,
    config: IndicatorScoringConfig,
) -> tuple[IndicatorEvidence, ...]:
    cost_multiple = economics.expected_move_bps / economics.modeled_cost_bps
    multiple_score = _clamp(
        100.0
        * (cost_multiple - 1.0)
        / (config.excellent_target_cost_multiple - 1.0)
    )
    reward_risk_score = _clamp(
        100.0
        * (economics.reward_risk - 1.0)
        / (config.excellent_reward_risk - 1.0)
    )
    net_score = _clamp(
        50.0 + 50.0 * economics.expected_net_bps / economics.modeled_cost_bps
    )
    return (
        IndicatorEvidence(
            "target_cost_multiple",
            IndicatorFamily.ECONOMICS,
            multiple_score,
            1.0,
            available_at,
            "structural target relative to fully modeled round-trip cost",
            round(cost_multiple, 6),
            weight=1.5,
        ),
        IndicatorEvidence(
            "expected_net_bps",
            IndicatorFamily.ECONOMICS,
            net_score,
            1.0,
            available_at,
            (
                "calibrated candidate expectancy after modeled costs"
                if economics.expectancy_calibrated
                else "uncalibrated probability prior cannot establish economic expectancy"
            ),
            economics.expected_net_bps,
            weight=1.25,
            hard_block=not economics.expectancy_calibrated,
        ),
        IndicatorEvidence(
            "reward_risk",
            IndicatorFamily.ECONOMICS,
            reward_risk_score,
            1.0,
            available_at,
            "target distance divided by stop distance",
            economics.reward_risk,
        ),
    )


def event_indicator_evidence(
    context: EventMarketSnapshot,
    side: Side,
    economics: CandidateEconomics,
    config: IndicatorScoringConfig | None = None,
) -> tuple[IndicatorEvidence, ...]:
    """Translate one immutable event snapshot into transparent score inputs."""

    config = config or default_indicator_scoring_config()
    at = context.available_at
    htf_available = bool(context.features.get("htf_available", False))
    evidence: list[IndicatorEvidence] = [
        IndicatorEvidence(
            "htf_bias_alignment",
            IndicatorFamily.STRUCTURE,
            _alignment_score(context.htf_bias, side),
            (0.85 if context.htf_bias else 0.45) if htf_available else 0.0,
            at,
            "higher-timeframe directional bias alignment",
            context.htf_bias,
            weight=1.5,
        ),
        IndicatorEvidence(
            "vwap_directional_location",
            IndicatorFamily.STRUCTURE,
            _directional_score(context.vwap_distance_bps, config.vwap_full_scale_bps, side),
            0.65 if htf_available else 0.0,
            at,
            "price location versus causal higher-timeframe VWAP",
            context.vwap_distance_bps,
        ),
        IndicatorEvidence(
            "event_confirmation_alignment",
            IndicatorFamily.MOMENTUM,
            _alignment_score(context.confirmed_direction, side),
            min(1.0, context.confirmation_samples / 6.0),
            at,
            "sustained event direction relative to candidate side",
            context.confirmed_direction,
        ),
        IndicatorEvidence(
            "book_imbalance",
            IndicatorFamily.ORDER_FLOW,
            _directional_score(context.book_imbalance, config.book_imbalance_full_scale, side),
            0.9 if context.book_healthy else 0.0,
            at,
            "top-level displayed depth imbalance",
            context.book_imbalance,
        ),
        IndicatorEvidence(
            "trade_flow_imbalance",
            IndicatorFamily.ORDER_FLOW,
            _directional_score(context.flow_imbalance, config.flow_imbalance_full_scale, side),
            min(1.0, context.confirmation_samples / 6.0),
            at,
            "aggressive buy/sell notional imbalance in the rolling event window",
            context.flow_imbalance,
            weight=1.25,
        ),
        IndicatorEvidence(
            "spread_quality",
            IndicatorFamily.LIQUIDITY,
            _quality_below(context.spread_bps, config.maximum_spread_bps),
            1.0,
            at,
            "narrower displayed spread receives a higher execution-quality score",
            context.spread_bps,
            hard_block=context.spread_bps > config.maximum_spread_bps,
        ),
        IndicatorEvidence(
            "top_depth_quality",
            IndicatorFamily.LIQUIDITY,
            _clamp(100.0 * float(context.features.get("depth_usd", 0.0)) / config.target_depth_usd),
            0.8,
            at,
            "displayed top-level notional relative to the configured robust-depth target",
            float(context.features.get("depth_usd", 0.0)),
        ),
    ]
    trend_coverage = float(context.features.get("trend_coverage_seconds", 0.0))
    if trend_coverage >= 300.0:
        trend_5m_bps = float(context.features.get("trend_5m_bps", 0.0))
        evidence.append(
            IndicatorEvidence(
                "event_trend_5m_alignment",
                IndicatorFamily.MOMENTUM,
                _directional_score(
                    trend_5m_bps,
                    config.vwap_full_scale_bps,
                    side,
                ),
                min(1.0, trend_coverage / 300.0),
                at,
                "causal five-minute trade-price trend relative to candidate side",
                trend_5m_bps,
                weight=1.25,
            )
        )
    if context.absorption is not None:
        evidence.append(
            IndicatorEvidence(
                "absorption_direction",
                IndicatorFamily.ORDER_FLOW,
                _alignment_score(context.absorption.reversal_direction, side),
                context.absorption.strength,
                at,
                "verified absorption reversal direction and detector strength",
                context.absorption.reversal_direction,
                weight=1.25,
            )
        )
    book_age = float(context.features.get("book_age_ms", float("inf")))
    feed_delay_ms = (
        max(0.0, (context.source_received_at - context.source_exchange_ts).total_seconds() * 1_000)
        if context.source_exchange_ts is not None
        else None
    )
    evidence.extend(
        (
            IndicatorEvidence(
                "book_integrity",
                IndicatorFamily.DATA_QUALITY,
                100.0 if context.book_healthy else 0.0,
                1.0,
                at,
                "sequence/checksum-valid, uncrossed order book",
                context.book_healthy,
                weight=1.5,
                hard_block=not context.book_healthy,
            ),
            IndicatorEvidence(
                "book_freshness",
                IndicatorFamily.DATA_QUALITY,
                _quality_below(book_age, config.maximum_book_age_ms),
                1.0,
                at,
                "local age of the most recent valid order-book update",
                book_age,
                hard_block=book_age > config.maximum_book_age_ms,
            ),
        )
    )
    if feed_delay_ms is not None:
        evidence.append(
            IndicatorEvidence(
                "feed_delay",
                IndicatorFamily.DATA_QUALITY,
                _quality_below(feed_delay_ms, config.maximum_feed_delay_ms),
                0.9,
                at,
                "exchange-event to local-receive delay",
                feed_delay_ms,
                hard_block=feed_delay_ms > config.maximum_feed_delay_ms,
            )
        )
    evidence.extend(_economics_evidence(economics, context.decision_ts, config))
    return tuple(evidence)


def candle_indicator_evidence(
    row: Mapping[str, object],
    *,
    side: Side,
    available_at: datetime,
    economics: CandidateEconomics,
    closed_candle: bool,
    config: IndicatorScoringConfig | None = None,
) -> tuple[IndicatorEvidence, ...]:
    """Adapter for the existing causal quant-pack columns.

    Missing optional families stay missing and lower coverage; they are never
    silently filled with favourable defaults.
    """

    config = config or default_indicator_scoring_config()
    at = _utc(available_at)
    long_side = side is Side.LONG

    def flag(long_key: str, short_key: str) -> bool:
        return bool(row.get(long_key if long_side else short_key, False))

    bias = flag("bias_long", "bias_short")
    structure_event = any(
        (
            flag("bos_up", "bos_down"),
            flag("choch_up", "choch_down"),
            flag("sweep_low", "sweep_high"),
            flag("bullish_fvg_retest", "bearish_fvg_retest"),
        )
    )
    displacement = flag("displacement_up", "displacement_down")
    side_score = float(row.get("long_score" if long_side else "short_score", 0.0))
    other_score = float(row.get("short_score" if long_side else "long_score", 0.0))
    score_delta = side_score - other_score
    evidence: list[IndicatorEvidence] = [
        IndicatorEvidence(
            "candle_bias_alignment",
            IndicatorFamily.STRUCTURE,
            100.0 if bias else 25.0,
            0.8,
            at,
            "EMA/efficiency higher-timeframe bias from closed candles",
            bias,
            weight=1.5,
        ),
        IndicatorEvidence(
            "mechanical_structure_event",
            IndicatorFamily.STRUCTURE,
            100.0 if structure_event else 35.0,
            0.75,
            at,
            "mechanical BOS/CHOCH/sweep/FVG evidence on the decision candle",
            structure_event,
        ),
        IndicatorEvidence(
            "directional_score_delta",
            IndicatorFamily.MOMENTUM,
            _clamp(50.0 + 10.0 * score_delta),
            0.75,
            at,
            "candidate-side quant score minus opposing-side score",
            score_delta,
        ),
        IndicatorEvidence(
            "displacement",
            IndicatorFamily.MOMENTUM,
            100.0 if displacement else 35.0,
            0.7,
            at,
            "ATR-normalized directional candle displacement",
            displacement,
        ),
    ]
    atr_pct = row.get("atr_pct")
    if atr_pct is not None and isfinite(float(atr_pct)):
        percentile = float(atr_pct)
        distance = 0.0 if 0.15 <= percentile <= 0.85 else min(
            abs(percentile - 0.15), abs(percentile - 0.85)
        )
        evidence.append(
            IndicatorEvidence(
                "atr_percentile_quality",
                IndicatorFamily.VOLATILITY,
                _clamp(100.0 - 200.0 * distance),
                0.8,
                at,
                "causal ATR percentile penalizes dead and parabolic conditions",
                percentile,
            )
        )
    volume_z = row.get("volume_z")
    if volume_z is not None and isfinite(float(volume_z)):
        evidence.append(
            IndicatorEvidence(
                "volume_participation",
                IndicatorFamily.PARTICIPATION,
                _clamp(50.0 + 25.0 * float(volume_z)),
                0.8,
                at,
                "decision-bar volume z-score versus its causal rolling history",
                float(volume_z),
            )
        )
    evidence.append(
        IndicatorEvidence(
            "closed_candle_integrity",
            IndicatorFamily.DATA_QUALITY,
            100.0 if closed_candle else 0.0,
            1.0,
            at,
            "decision inputs must come from an immutable completed candle",
            closed_candle,
            hard_block=not closed_candle,
        )
    )
    evidence.append(
        IndicatorEvidence(
            "finite_ohlcv",
            IndicatorFamily.DATA_QUALITY,
            100.0,
            1.0,
            at,
            "caller supplied a validated finite OHLCV row",
            True,
        )
    )
    evidence.extend(_economics_evidence(economics, at, config))
    return tuple(evidence)


def historical_trade_indicator_evidence(
    trade: Mapping[str, object],
    *,
    source_data_quality_pass: bool,
    config: IndicatorScoringConfig | None = None,
) -> tuple[IndicatorEvidence, ...]:
    """Reconstruct only decision-time evidence preserved in old backtests.

    The archived Delta outcome rows predate indicator-family scoring and do
    not contain event-level L2/CVD fields. Those families intentionally remain
    absent instead of being inferred from future price paths.
    """

    config = config or default_indicator_scoring_config()
    side = Side(str(trade["side"]).lower())
    available_at = _utc(datetime.fromisoformat(str(trade["decision_ts"])))
    wanted_direction = "up" if side is Side.LONG else "down"
    trend_direction = str(trade.get("trend_direction_at_entry") or "flat")
    trend_regime = str(trade.get("trend_regime_at_entry") or "unknown")
    direction_score = 100.0 if trend_direction == wanted_direction else (
        50.0 if trend_direction == "flat" else 0.0
    )
    strength_scores = {
        "strong_trend": 100.0,
        "weak_trend": 70.0,
        "range": 40.0,
        "unknown": 25.0,
    }
    probability = float(trade.get("scalper_probability") or 0.0)
    confidence = float(trade.get("confidence") or 0.0)
    evidence: list[IndicatorEvidence] = [
        IndicatorEvidence(
            "historical_trend_direction",
            IndicatorFamily.STRUCTURE,
            direction_score,
            0.8,
            available_at,
            "preserved causal trend direction relative to the candidate side",
            trend_direction,
            weight=1.5,
        ),
        IndicatorEvidence(
            "historical_trend_strength",
            IndicatorFamily.STRUCTURE,
            strength_scores.get(trend_regime, 25.0),
            0.7,
            available_at,
            "preserved rule-based trend-strength regime at entry",
            trend_regime,
        ),
        IndicatorEvidence(
            "primary_probability",
            IndicatorFamily.MOMENTUM,
            _clamp(probability * 100.0),
            0.65,
            available_at,
            "uncalibrated primary scanner probability preserved at decision time",
            probability,
        ),
        IndicatorEvidence(
            "primary_confidence",
            IndicatorFamily.MOMENTUM,
            _clamp(confidence * 100.0),
            0.65,
            available_at,
            "primary scanner confidence preserved at decision time",
            confidence,
        ),
    ]
    for indicator_id, field_name in (
        ("atr_percentile", "atr_percentile_at_entry"),
        ("bb_width_percentile", "bb_width_percentile_at_entry"),
    ):
        raw = trade.get(field_name)
        if raw is None or not isfinite(float(raw)):
            continue
        percentile = float(raw)
        distance = 0.0 if 0.15 <= percentile <= 0.85 else min(
            abs(percentile - 0.15), abs(percentile - 0.85)
        )
        evidence.append(
            IndicatorEvidence(
                indicator_id,
                IndicatorFamily.VOLATILITY,
                _clamp(100.0 - 200.0 * distance),
                0.8,
                available_at,
                "preserved causal percentile penalizes dead and parabolic conditions",
                percentile,
            )
        )
    evidence.extend(
        (
            IndicatorEvidence(
                "historical_closed_candle",
                IndicatorFamily.DATA_QUALITY,
                100.0,
                1.0,
                available_at,
                "archived engine contract used completed candles and next-bar entry",
                True,
            ),
            IndicatorEvidence(
                "historical_source_quality",
                IndicatorFamily.DATA_QUALITY,
                100.0 if source_data_quality_pass else 0.0,
                1.0,
                available_at,
                "source market data quality verdict from the archived backtest",
                source_data_quality_pass,
                hard_block=not source_data_quality_pass,
            ),
        )
    )
    planned_stop = float(trade.get("planned_stop_bps") or 0.0)
    expected_move = float(trade.get("expected_move_bps") or 0.0)
    economics = CandidateEconomics(
        expected_move_bps=expected_move,
        modeled_cost_bps=float(trade.get("modeled_cost_bps") or trade.get("cost_bps") or 0.0),
        expected_net_bps=float(trade.get("expected_net_bps") or 0.0),
        reward_risk=expected_move / planned_stop if planned_stop > 0 else 0.0,
    )
    evidence.extend(_economics_evidence(economics, available_at, config))
    return tuple(evidence)
