"""Pluggable, candle-triggered Delta scalper scanners."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from vnedge.scalping.delta_engine.fee_model import DeltaFeeModel
from vnedge.scalping.delta_engine.predictor import MovePredictor
from vnedge.scalping.delta_engine.types import MarketContext, Regime, Side, SignalCandidate


def _price_at_bps(price: float, side: Side, bps: float) -> float:
    direction = 1.0 if side is Side.LONG else -1.0
    return price * (1 + direction * bps / 10_000.0)


class Scanner(ABC):
    scanner_id: str

    @abstractmethod
    def evaluate(self, ctx: MarketContext) -> SignalCandidate | None:
        """Return a candidate from closed candles only, or ``None``."""

    @abstractmethod
    def required_features(self) -> tuple[str, ...]:
        """Feature names consumed by this scanner."""

    def regime_enabled(self, ctx: MarketContext) -> bool:
        """Whether this scanner is permitted in the already-closed context."""
        return True


@dataclass(frozen=True)
class MomentumBurstConfig:
    min_volume_z: float = 0.75
    min_body_ratio: float = 0.55
    min_breakout_bps: float = 0.4
    stop_atr_fraction: float = 0.55
    max_stop_bps: float = 14.0
    min_stop_bps: float = 6.0
    min_history: int = 31
    time_stop_seconds: int = 28 * 60
    prefer_maker: bool = True
    enabled_regimes: tuple[Regime, ...] = (
        Regime.QUIET,
        Regime.TRENDING_UP,
        Regime.TRENDING_DOWN,
        Regime.EXPANDING,
        Regime.FUNDING_EXTREME,
    )


class MomentumBurstScanner(Scanner):
    scanner_id = "delta_momentum_burst_v1"

    def __init__(
        self,
        fee_model: DeltaFeeModel,
        predictor: MovePredictor | None = None,
        config: MomentumBurstConfig | None = None,
    ) -> None:
        self.fee_model = fee_model
        self.predictor = predictor or MovePredictor()
        self.config = config or MomentumBurstConfig()

    def required_features(self) -> tuple[str, ...]:
        return ("volume_z", "body_ratio", "breakout_up_bps", "breakout_down_bps", "atr_bps")

    def regime_enabled(self, ctx: MarketContext) -> bool:
        return ctx.regime in self.config.enabled_regimes

    def evaluate(self, ctx: MarketContext) -> SignalCandidate | None:
        if len(ctx.candles.get("1m", ())) < self.config.min_history:
            return None
        if not self.regime_enabled(ctx):
            return None
        f = ctx.features
        if f.get("volume_z", 0.0) < self.config.min_volume_z:
            return None
        if f.get("body_ratio", 0.0) < self.config.min_body_ratio:
            return None
        up = f.get("breakout_up_bps", 0.0)
        down = f.get("breakout_down_bps", 0.0)
        direction = f.get("body_direction", 0.0)
        if up >= self.config.min_breakout_bps and direction > 0:
            side = Side.LONG
            if ctx.regime is Regime.TRENDING_DOWN:
                return None
            breakout = up
        elif down >= self.config.min_breakout_bps and direction < 0:
            side = Side.SHORT
            if ctx.regime is Regime.TRENDING_UP:
                return None
            breakout = down
        else:
            return None
        strength = min(1.5, 0.45 + breakout / 8.0 + max(0.0, f["volume_z"]) / 8.0)
        estimate = self.predictor.estimate(ctx, side, setup_strength=strength)
        costs = self.fee_model.breakdown(
            ctx.symbol,
            entry_is_maker=self.config.prefer_maker,
            hold_seconds=estimate.expected_hold_seconds,
        )
        stop_bps = min(
            self.config.max_stop_bps,
            max(self.config.min_stop_bps, f["atr_bps"] * self.config.stop_atr_fraction),
        )
        raw_expectancy = estimate.probability * estimate.expected_move_bps - (
            1 - estimate.probability
        ) * stop_bps
        entry = ctx.candles["1m"][-1].close
        l2_agrees = (side is Side.LONG and ctx.l2.imbalance > 0) or (
            side is Side.SHORT and ctx.l2.imbalance < 0
        )
        return SignalCandidate(
            scanner_id=self.scanner_id,
            symbol=ctx.symbol,
            side=side,
            decision_ts=ctx.ts,
            entry_price=entry,
            stop_loss=_price_at_bps(entry, side, -stop_bps),
            take_profits=(
                _price_at_bps(entry, side, estimate.expected_move_bps * 0.7),
                _price_at_bps(entry, side, estimate.expected_move_bps),
            ),
            time_stop_seconds=self.config.time_stop_seconds,
            expected_hold_seconds=estimate.expected_hold_seconds,
            expected_move_bps=estimate.expected_move_bps,
            raw_expectancy_bps=raw_expectancy,
            modeled_cost_bps=costs.total_bps,
            fee_adjusted_expectancy_bps=raw_expectancy - costs.total_bps,
            scalper_probability=estimate.probability,
            confidence=estimate.confidence,
            entry_is_maker=self.config.prefer_maker,
            metadata={
                "regime": ctx.regime.value,
                "regime_profile": ctx.regime_profile.to_dict(),
                "regime_filter": {
                    "allowed": True,
                    "enabled_regimes": [regime.value for regime in self.config.enabled_regimes],
                },
                "breakout_bps": breakout,
                "volume_z": f["volume_z"],
                "l2_confirmation": {
                    "status": ctx.l2.status,
                    "agrees": l2_agrees,
                    "imbalance": ctx.l2.imbalance,
                    "imbalance_z": ctx.l2.imbalance_z,
                    "cvd": ctx.l2.cvd,
                    "buy_aggression_ratio": ctx.l2.buy_aggression_ratio,
                    "absorption_score": ctx.l2.absorption_score,
                    "depth_usd": ctx.l2.depth_usd,
                    "sequence_healthy": ctx.l2.sequence_healthy,
                    "context_only": True,
                    "used_for_signal": False,
                    "used_for_execution": False,
                },
                "fee_breakdown": costs.to_dict(),
            },
        )


@dataclass(frozen=True)
class ImbalanceFadeConfig:
    min_wick_ratio: float = 0.48
    min_stretch_bps: float = 7.0
    rsi_high: float = 68.0
    rsi_low: float = 32.0
    min_history: int = 31
    time_stop_seconds: int = 28 * 60
    prefer_maker: bool = True
    enabled_regimes: tuple[Regime, ...] = (Regime.QUIET, Regime.EXPANDING)


class OrderFlowImbalanceFadeScanner(Scanner):
    """Candle rejection fade with L2 imbalance attached as confirmation only."""

    scanner_id = "delta_imbalance_fade_v1"

    def __init__(
        self,
        fee_model: DeltaFeeModel,
        predictor: MovePredictor | None = None,
        config: ImbalanceFadeConfig | None = None,
    ) -> None:
        self.fee_model = fee_model
        self.predictor = predictor or MovePredictor()
        self.config = config or ImbalanceFadeConfig()

    def required_features(self) -> tuple[str, ...]:
        return ("upper_wick_ratio", "lower_wick_ratio", "return_5_bps", "rsi_14", "atr_bps")

    def regime_enabled(self, ctx: MarketContext) -> bool:
        return ctx.regime in self.config.enabled_regimes

    def evaluate(self, ctx: MarketContext) -> SignalCandidate | None:
        if len(ctx.candles.get("1m", ())) < self.config.min_history:
            return None
        if not self.regime_enabled(ctx):
            return None
        f = ctx.features
        stretch = f.get("return_5_bps", 0.0)
        if (
            stretch >= self.config.min_stretch_bps
            and f.get("upper_wick_ratio", 0.0) >= self.config.min_wick_ratio
            and f.get("rsi_14", 50.0) >= self.config.rsi_high
        ):
            side = Side.SHORT
            wick = f["upper_wick_ratio"]
        elif (
            stretch <= -self.config.min_stretch_bps
            and f.get("lower_wick_ratio", 0.0) >= self.config.min_wick_ratio
            and f.get("rsi_14", 50.0) <= self.config.rsi_low
        ):
            side = Side.LONG
            wick = f["lower_wick_ratio"]
        else:
            return None
        strength = min(1.5, 0.45 + abs(stretch) / 25.0 + wick / 3.0)
        estimate = self.predictor.estimate(ctx, side, setup_strength=strength)
        costs = self.fee_model.breakdown(
            ctx.symbol,
            entry_is_maker=self.config.prefer_maker,
            hold_seconds=estimate.expected_hold_seconds,
        )
        stop_bps = min(16.0, max(7.0, f["atr_bps"] * 0.65))
        raw_expectancy = estimate.probability * estimate.expected_move_bps - (
            1 - estimate.probability
        ) * stop_bps
        entry = ctx.candles["1m"][-1].close
        l2_agrees = (side is Side.LONG and ctx.l2.imbalance > 0) or (
            side is Side.SHORT and ctx.l2.imbalance < 0
        )
        return SignalCandidate(
            scanner_id=self.scanner_id,
            symbol=ctx.symbol,
            side=side,
            decision_ts=ctx.ts,
            entry_price=entry,
            stop_loss=_price_at_bps(entry, side, -stop_bps),
            take_profits=(
                _price_at_bps(entry, side, estimate.expected_move_bps * 0.65),
                _price_at_bps(entry, side, estimate.expected_move_bps),
            ),
            time_stop_seconds=self.config.time_stop_seconds,
            expected_hold_seconds=estimate.expected_hold_seconds,
            expected_move_bps=estimate.expected_move_bps,
            raw_expectancy_bps=raw_expectancy,
            modeled_cost_bps=costs.total_bps,
            fee_adjusted_expectancy_bps=raw_expectancy - costs.total_bps,
            scalper_probability=estimate.probability,
            confidence=estimate.confidence,
            entry_is_maker=self.config.prefer_maker,
            metadata={
                "regime": ctx.regime.value,
                "regime_profile": ctx.regime_profile.to_dict(),
                "regime_filter": {
                    "allowed": True,
                    "enabled_regimes": [regime.value for regime in self.config.enabled_regimes],
                },
                "stretch_bps": stretch,
                "wick_ratio": wick,
                "l2_confirmation": {
                    "status": ctx.l2.status,
                    "agrees": l2_agrees,
                    "imbalance": ctx.l2.imbalance,
                    "imbalance_z": ctx.l2.imbalance_z,
                    "cvd": ctx.l2.cvd,
                    "buy_aggression_ratio": ctx.l2.buy_aggression_ratio,
                    "absorption_score": ctx.l2.absorption_score,
                    "depth_usd": ctx.l2.depth_usd,
                    "sequence_healthy": ctx.l2.sequence_healthy,
                    "context_only": True,
                    "used_for_signal": False,
                    "used_for_execution": False,
                },
                "fee_breakdown": costs.to_dict(),
            },
        )
