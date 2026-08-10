"""Frozen scalper parameter registry.

This is the single map of the scalper research surface: horizons/TFs,
family-specific thresholds, exchange cost assumptions, route gates, and exit
policies. It is intentionally declarative. Research modules may consume it;
execution may not mutate it at runtime.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal


ExecutionTimeframeLabel = Literal[
    "event",
    "250ms",
    "500ms",
    "1s",
    "3s",
    "5s",
    "15s",
    "30s",
    "60s",
    "1m_research_proxy",
]

ContextTimeframeLabel = Literal["4h", "1h", "15m", "1m"]
TimeframeLabel = ExecutionTimeframeLabel | ContextTimeframeLabel
FamilyResearchStatus = Literal["active_research", "deprioritized", "tombstoned"]

DEFAULT_ACTIVE_SCALPER_FAMILY_ID = "forced_flow_continuation"
REFERENCE_REPLAY_FAMILY_ID = "book_imbalance_continuation"


@dataclass(frozen=True)
class ExchangeFeeProfile:
    exchange: str
    maker_bps: float
    taker_bps: float
    slippage_bps: float
    safety_buffer_bps: float
    min_notional_usd: float = 100.0
    notes: str = ""

    def __post_init__(self) -> None:
        for name, value in (
            ("maker_bps", self.maker_bps),
            ("taker_bps", self.taker_bps),
            ("slippage_bps", self.slippage_bps),
            ("safety_buffer_bps", self.safety_buffer_bps),
            ("min_notional_usd", self.min_notional_usd),
        ):
            if value < 0:
                raise ValueError(f"{name} cannot be negative")

    @property
    def maker_first_cost_bps(self) -> float:
        return self.maker_bps + self.taker_bps + self.slippage_bps + self.safety_buffer_bps

    @property
    def taker_round_trip_cost_bps(self) -> float:
        return 2 * self.taker_bps + self.slippage_bps + self.safety_buffer_bps

    def to_dict(self) -> dict:
        d = asdict(self)
        d["maker_first_cost_bps"] = self.maker_first_cost_bps
        d["taker_round_trip_cost_bps"] = self.taker_round_trip_cost_bps
        return d


@dataclass(frozen=True)
class RouteGate:
    maker_min_profit_factor: float = 1.15
    taker_min_profit_factor: float = 1.80
    min_avg_net_bps: float = 0.5
    min_fill_rate_pct: float = 5.0
    max_avg_adverse_bps: float = 8.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ExitPolicy:
    policy_id: str
    mode: Literal["static", "adverse_cut", "adaptive_trail"]
    ttl_ms: int
    stop_bps: float
    target_bps: float
    max_hold_ms: int
    adverse_cut_bps: float = 0.0
    trail_after_bps: float = 0.0
    trail_distance_bps: float = 0.0
    live_wired: bool = False

    def __post_init__(self) -> None:
        if self.ttl_ms <= 0 or self.max_hold_ms <= 0:
            raise ValueError("ttl_ms and max_hold_ms must be positive")
        for name, value in (
            ("stop_bps", self.stop_bps),
            ("target_bps", self.target_bps),
            ("adverse_cut_bps", self.adverse_cut_bps),
            ("trail_after_bps", self.trail_after_bps),
            ("trail_distance_bps", self.trail_distance_bps),
        ):
            if value < 0:
                raise ValueError(f"{name} cannot be negative")
        if self.mode == "adverse_cut" and self.adverse_cut_bps <= 0:
            raise ValueError("adverse_cut policy requires adverse_cut_bps")
        if self.mode == "adaptive_trail" and (
            self.trail_after_bps <= 0 or self.trail_distance_bps <= 0
        ):
            raise ValueError("adaptive_trail policy requires trail thresholds")

    @property
    def intelligence_score(self) -> int:
        score = 20  # reduce-only exit path / capital protection foundation
        score += 15 if self.stop_bps > 0 else 0
        score += 10 if self.target_bps > 0 else 0
        score += 10 if self.ttl_ms > 0 and self.max_hold_ms > 0 else 0
        score += 15 if self.adverse_cut_bps > 0 else 0
        score += 20 if self.trail_after_bps > 0 and self.trail_distance_bps > 0 else 0
        score += 10 if self.live_wired else 0
        return min(score, 100)

    @property
    def intelligence_label(self) -> str:
        if self.intelligence_score >= 80:
            return "SMART_REPLAY_READY"
        if self.intelligence_score >= 60:
            return "DEVELOPING"
        return "BASIC_STATIC"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["intelligence_score"] = self.intelligence_score
        d["intelligence_label"] = self.intelligence_label
        return d


@dataclass(frozen=True)
class ScalperFamilyParameters:
    family_id: str
    description: str
    horizons_ms: tuple[int, ...]
    timeframes: tuple[ExecutionTimeframeLabel, ...]
    exit_policy_id: str
    route_gate: RouteGate = RouteGate()
    imbalance_grid: tuple[float, ...] = (0.10, 0.20, 0.35, 0.50, 0.65)
    spread_grid_bps: tuple[float, ...] = (1.5, 3.0, 6.0)
    sample_every_ms: int = 500
    min_samples: int = 30
    max_spread_bps: float = 3.0
    min_trade_count: int = 8
    min_abs_imbalance: float = 0.35
    flow_agreement: float = 0.66
    min_pressure_notional_usd: float = 75_000.0
    microprice_dislocation_bps: float = 0.20
    liquidity_vacuum_depth_usd: float = 250_000.0
    min_realized_vol_bps: float = 0.08
    status: FamilyResearchStatus = "active_research"
    evidence: str = ""

    def __post_init__(self) -> None:
        if not self.horizons_ms:
            raise ValueError("family must define horizons_ms")
        if any(h <= 0 for h in self.horizons_ms):
            raise ValueError("horizons_ms must be positive")
        if not self.timeframes:
            raise ValueError("family must define timeframes")
        if self.sample_every_ms <= 0 or self.min_samples <= 0:
            raise ValueError("sample_every_ms and min_samples must be positive")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["route_gate"] = self.route_gate.to_dict()
        return d


@dataclass(frozen=True)
class ScalperParameterRegistry:
    version: str
    execution_timeframes: tuple[ExecutionTimeframeLabel, ...]
    context_timeframes: tuple[ContextTimeframeLabel, ...]
    exchange_fees: dict[str, ExchangeFeeProfile]
    exit_policies: dict[str, ExitPolicy]
    families: dict[str, ScalperFamilyParameters]

    @property
    def timeframes(self) -> tuple[ExecutionTimeframeLabel, ...]:
        """Backward-compatible alias for the execution/replay timeframes."""
        return self.execution_timeframes

    def fee_profile(self, exchange: str) -> ExchangeFeeProfile:
        return self.exchange_fees.get(exchange, self.exchange_fees["binanceusdm"])

    def family(self, family_id: str) -> ScalperFamilyParameters:
        try:
            return self.families[family_id]
        except KeyError as exc:
            raise KeyError(f"unknown scalper family: {family_id}") from exc

    def exit_policy(self, policy_id: str) -> ExitPolicy:
        try:
            return self.exit_policies[policy_id]
        except KeyError as exc:
            raise KeyError(f"unknown exit policy: {policy_id}") from exc

    def family_exit_policy(self, family_id: str) -> ExitPolicy:
        return self.exit_policy(self.family(family_id).exit_policy_id)

    def active_research_families(self) -> tuple[ScalperFamilyParameters, ...]:
        return tuple(
            family for family in self.families.values()
            if family.status == "active_research"
        )

    def tombstoned_families(self) -> tuple[ScalperFamilyParameters, ...]:
        return tuple(
            family for family in self.families.values()
            if family.status == "tombstoned"
        )

    def replay_sweep_kwargs(
        self,
        exchange: str = "binanceusdm",
        family_id: str = DEFAULT_ACTIVE_SCALPER_FAMILY_ID,
    ) -> dict:
        fee = self.fee_profile(exchange)
        family = self.family(family_id)
        exit_policy = self.exit_policy(family.exit_policy_id)
        return {
            "family_id": family.family_id,
            "exit_policy_id": exit_policy.policy_id,
            "min_imbalances": family.imbalance_grid,
            "max_spread_bps": family.spread_grid_bps,
            "ttl_ms": exit_policy.ttl_ms,
            "stop_bps": exit_policy.stop_bps,
            "target_bps": exit_policy.target_bps,
            "maker_bps": fee.maker_bps,
            "taker_bps": fee.taker_bps,
            "slippage_bps": fee.slippage_bps,
        }

    def scanner_gate_kwargs(
        self, family_id: str = DEFAULT_ACTIVE_SCALPER_FAMILY_ID
    ) -> dict:
        gate = self.family(family_id).route_gate
        return {
            "min_fill_rate_pct": gate.min_fill_rate_pct,
            "maker_min_profit_factor": gate.maker_min_profit_factor,
            "taker_min_profit_factor": gate.taker_min_profit_factor,
            "min_avg_net_bps": gate.min_avg_net_bps,
            "max_avg_adverse_bps": gate.max_avg_adverse_bps,
        }

    def alpha_factory_kwargs(self, exchange: str = "binanceusdm") -> dict:
        fee = self.fee_profile(exchange)
        families = tuple(
            self.family(f)
            for f in (
                "forced_flow_continuation",
                "absorption_reversal",
                "microprice_dislocation",
                "liquidity_vacuum_continuation",
                "volatility_impulse",
            )
        )
        horizons = tuple(sorted({h for f in families for h in f.horizons_ms}))
        return {
            "horizons_ms": horizons,
            "context_timeframes": self.context_timeframes,
            "sample_every_ms": min(f.sample_every_ms for f in families),
            "min_samples": min(f.min_samples for f in families),
            "max_spread_bps": max(f.max_spread_bps for f in families),
            "min_trade_count": min(f.min_trade_count for f in families),
            "min_abs_imbalance": min(f.min_abs_imbalance for f in families),
            "flow_agreement": max(f.flow_agreement for f in families),
            "min_pressure_notional_usd": min(f.min_pressure_notional_usd for f in families),
            "microprice_dislocation_bps": min(
                f.microprice_dislocation_bps for f in families
            ),
            "liquidity_vacuum_depth_usd": max(
                f.liquidity_vacuum_depth_usd for f in families
            ),
            "min_realized_vol_bps": min(f.min_realized_vol_bps for f in families),
            "maker_bps": fee.maker_bps,
            "taker_bps": fee.taker_bps,
            "slippage_bps": fee.slippage_bps,
            "safety_buffer_bps": fee.safety_buffer_bps,
        }

    def exit_intelligence_summary(self) -> dict:
        best = max(self.exit_policies.values(), key=lambda p: p.intelligence_score)
        live = [p for p in self.exit_policies.values() if p.live_wired]
        return {
            "best_policy": best.to_dict(),
            "live_wired_policy_ids": [p.policy_id for p in live],
            "current_live_label": (
                "BASIC_STATIC" if not live else max(live, key=lambda p: p.intelligence_score).intelligence_label
            ),
            "assessment": (
                "Replay can evaluate adaptive exits; live scalper exits remain "
                "reduce-only/static until the hot loop wires the selected policy."
            ),
        }

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "timeframes": list(self.execution_timeframes),
            "execution_timeframes": list(self.execution_timeframes),
            "context_timeframes": list(self.context_timeframes),
            "timeframe_layers": {
                "context": list(self.context_timeframes),
                "execution": [
                    tf for tf in self.execution_timeframes
                    if tf != "1m_research_proxy"
                ],
                "research_proxy": ["1m_research_proxy"],
            },
            "exchange_fees": {k: v.to_dict() for k, v in self.exchange_fees.items()},
            "exit_policies": {k: v.to_dict() for k, v in self.exit_policies.items()},
            "families": {k: v.to_dict() for k, v in self.families.items()},
            "family_lifecycle": {
                "active_research": [
                    family.family_id for family in self.active_research_families()
                ],
                "tombstoned": [
                    {
                        "family_id": family.family_id,
                        "evidence": family.evidence,
                    }
                    for family in self.tombstoned_families()
                ],
            },
            "exit_intelligence": self.exit_intelligence_summary(),
            "can_trade": False,
            "can_promote": False,
        }


def _registry() -> ScalperParameterRegistry:
    context_timeframes: tuple[ContextTimeframeLabel, ...] = (
        "4h",
        "1h",
        "15m",
        "1m",
    )
    execution_timeframes: tuple[ExecutionTimeframeLabel, ...] = (
        "event",
        "250ms",
        "500ms",
        "1s",
        "3s",
        "5s",
        "15s",
        "30s",
        "60s",
        "1m_research_proxy",
    )
    route = RouteGate()
    exits = {
        "static_fast": ExitPolicy(
            "static_fast", "static", ttl_ms=3_000, stop_bps=6.0,
            target_bps=8.0, max_hold_ms=15_000, live_wired=True,
        ),
        "adverse_cut": ExitPolicy(
            "adverse_cut", "adverse_cut", ttl_ms=2_000, stop_bps=6.0,
            target_bps=8.0, max_hold_ms=10_000, adverse_cut_bps=3.0,
        ),
        "adaptive_trail": ExitPolicy(
            "adaptive_trail", "adaptive_trail", ttl_ms=2_000, stop_bps=7.0,
            target_bps=12.0, max_hold_ms=20_000, adverse_cut_bps=4.0,
            trail_after_bps=6.0, trail_distance_bps=3.0,
        ),
    }
    return ScalperParameterRegistry(
        version="scalper_params_v1_20260705",
        execution_timeframes=execution_timeframes,
        context_timeframes=context_timeframes,
        exchange_fees={
            "binanceusdm": ExchangeFeeProfile(
                "binanceusdm", maker_bps=2.0, taker_bps=5.0,
                slippage_bps=1.0, safety_buffer_bps=1.0,
                notes="default non-VIP futures assumption",
            ),
            "bybit": ExchangeFeeProfile(
                "bybit", maker_bps=2.0, taker_bps=5.5,
                slippage_bps=1.0, safety_buffer_bps=1.0,
                notes="default derivatives assumption",
            ),
            "delta_india": ExchangeFeeProfile(
                "delta_india", maker_bps=2.0, taker_bps=5.0,
                slippage_bps=1.5, safety_buffer_bps=1.0,
                notes="India live candidate; verify fee tier before live",
            ),
        },
        exit_policies=exits,
        families={
            "book_imbalance_continuation": ScalperFamilyParameters(
                family_id="book_imbalance_continuation",
                description="reference top-of-book imbalance replay lane",
                horizons_ms=(1_000, 3_000, 5_000),
                timeframes=("event", "1s", "3s", "5s", "1m_research_proxy"),
                exit_policy_id="static_fast",
                route_gate=route,
                status="tombstoned",
                evidence=(
                    "2026-07-05 L2 replay: all 120 configs across 8 recorded "
                    "lanes were negative after maker/taker/slippage costs; "
                    "continuous top-of-book imbalance is a replay reference, "
                    "not an active alpha premise."
                ),
            ),
            "forced_flow_continuation": ScalperFamilyParameters(
                family_id="forced_flow_continuation",
                description="aggressive taker flow aligned with book pressure",
                horizons_ms=(250, 500, 1_000, 3_000, 5_000, 15_000),
                timeframes=("event", "250ms", "500ms", "1s", "3s", "5s", "15s"),
                exit_policy_id="adverse_cut",
                route_gate=route,
            ),
            "absorption_reversal": ScalperFamilyParameters(
                family_id="absorption_reversal",
                description="resting liquidity absorbs opposite aggressive flow",
                horizons_ms=(500, 1_000, 3_000, 5_000, 15_000, 30_000),
                timeframes=("event", "500ms", "1s", "3s", "5s", "15s", "30s"),
                exit_policy_id="adverse_cut",
                route_gate=route,
            ),
            "microprice_dislocation": ScalperFamilyParameters(
                family_id="microprice_dislocation",
                description="microprice displaced from mid with supportive book",
                horizons_ms=(250, 500, 1_000, 3_000, 5_000),
                timeframes=("event", "250ms", "500ms", "1s", "3s", "5s"),
                exit_policy_id="static_fast",
                route_gate=route,
            ),
            "liquidity_vacuum_continuation": ScalperFamilyParameters(
                family_id="liquidity_vacuum_continuation",
                description="thin touch plus one-sided flow/volatility impulse",
                horizons_ms=(250, 500, 1_000, 3_000, 5_000, 15_000),
                timeframes=("event", "250ms", "500ms", "1s", "3s", "5s", "15s"),
                exit_policy_id="adaptive_trail",
                route_gate=route,
                liquidity_vacuum_depth_usd=250_000.0,
            ),
            "volatility_impulse": ScalperFamilyParameters(
                family_id="volatility_impulse",
                description="volatility expansion with one-sided taker flow",
                horizons_ms=(500, 1_000, 3_000, 5_000, 15_000, 30_000, 60_000),
                timeframes=("event", "500ms", "1s", "3s", "5s", "15s", "30s", "60s"),
                exit_policy_id="adaptive_trail",
                route_gate=route,
            ),
        },
    )


DEFAULT_SCALPER_PARAMETER_REGISTRY = _registry()
