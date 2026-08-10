"""Pluggable, candle-triggered Delta scalper scanners."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import fmean, pstdev

from vnedge.scalping.delta_engine.fee_model import DeltaFeeModel
from vnedge.scalping.delta_engine.predictor import MovePredictor
from vnedge.scalping.delta_engine.regime import _adx, _ema, _true_ranges
from vnedge.scalping.delta_engine.types import (
    Candle,
    MarketContext,
    Regime,
    Side,
    SignalCandidate,
)


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


def _latest_volume_z(rows: tuple[Candle, ...], window: int = 20) -> float:
    latest = rows[-1]
    history = [float(row.volume) for row in rows[-(window + 1) : -1]]
    if len(history) < window:
        return 0.0
    deviation = pstdev(history)
    return (float(latest.volume) - fmean(history)) / deviation if deviation else 0.0


def _body_ratio(row: Candle) -> float:
    span = max(float(row.high) - float(row.low), float(row.close) * 1e-9)
    return abs(float(row.close) - float(row.open)) / span


@dataclass(frozen=True)
class HierarchicalPullbackConfig:
    four_hour_ema_fast: int = 20
    four_hour_ema_slow: int = 50
    min_four_hour_adx: float = 22.0
    one_hour_ema: int = 20
    one_hour_atr_window: int = 14
    pullback_tolerance_atr: float = 0.35
    one_hour_structure_lookback: int = 12
    setup_expiry_minutes: int = 65
    confirmation_expiry_minutes: int = 10
    min_five_minute_body_ratio: float = 0.55
    min_five_minute_volume_z: float = 0.50
    min_one_minute_body_ratio: float = 0.50
    min_one_minute_volume_z: float = 0.75
    stop_atr_fraction: float = 0.80
    min_stop_bps: float = 8.0
    max_stop_bps: float = 35.0
    reward_risk: float = 2.50
    minimum_target_cost_multiple: float = 3.50
    research_probability_prior: float = 0.75
    research_confidence_prior: float = 0.65
    expected_hold_seconds: int = 15 * 60
    time_stop_seconds: int = 28 * 60
    cooldown_minutes: int = 240
    prefer_maker: bool = False


class HierarchicalPullbackScanner(Scanner):
    """Candle-native 4h/1h/5m/1m continuation hypothesis.

    L2 remains attached as observational metadata and cannot change whether a
    candidate exists, its side, or any price/expectancy field.
    """

    scanner_id = "delta_htf_pullback_continuation_v1"

    def __init__(
        self,
        fee_model: DeltaFeeModel,
        config: HierarchicalPullbackConfig | None = None,
    ) -> None:
        self.fee_model = fee_model
        self.config = config or HierarchicalPullbackConfig()
        self._last_fired_setup: dict[str, str] = {}
        self._last_fire_ts: dict[str, datetime] = {}

    def required_features(self) -> tuple[str, ...]:
        return (
            "4h_ema_20_50_bias",
            "4h_adx_14",
            "1h_pullback_to_ema_20",
            "5m_directional_body",
            "5m_volume_z",
            "1m_previous_bar_break",
            "1m_volume_z",
        )

    def _bias(self, ctx: MarketContext) -> tuple[Side | None, dict[str, float]]:
        rows = ctx.candles.get("4h", ())
        required = max(self.config.four_hour_ema_slow + 1, 16)
        if len(rows) < required:
            return None, {"history_bars": float(len(rows))}
        closes = [row.close for row in rows]
        fast = _ema(closes, self.config.four_hour_ema_fast)
        slow = _ema(closes, self.config.four_hour_ema_slow)
        adx = _adx(rows, 14)
        metrics = {"ema_fast": fast, "ema_slow": slow, "adx_14": adx}
        if adx < self.config.min_four_hour_adx:
            return None, metrics
        if fast > slow and rows[-1].close > fast:
            return Side.LONG, metrics
        if fast < slow and rows[-1].close < fast:
            return Side.SHORT, metrics
        return None, metrics

    def _one_hour_setup(
        self,
        ctx: MarketContext,
        side: Side,
    ) -> tuple[str, dict[str, float]] | None:
        rows = ctx.candles.get("1h", ())
        required = max(
            self.config.one_hour_ema + 1,
            self.config.one_hour_atr_window + 1,
            self.config.one_hour_structure_lookback + 1,
        )
        if len(rows) < required:
            return None
        latest = rows[-1]
        age = ctx.ts - latest.ts
        if age < timedelta(0) or age > timedelta(minutes=self.config.setup_expiry_minutes):
            return None
        ema = _ema([row.close for row in rows], self.config.one_hour_ema)
        ranges = _true_ranges(rows)
        atr = fmean(ranges[-self.config.one_hour_atr_window :])
        tolerance = atr * self.config.pullback_tolerance_atr
        structure = rows[-(self.config.one_hour_structure_lookback + 1) : -1]
        if side is Side.LONG:
            valid = (
                latest.low <= ema + tolerance
                and latest.close >= ema
                and latest.close > latest.open
                and latest.low > min(row.low for row in structure)
            )
        else:
            valid = (
                latest.high >= ema - tolerance
                and latest.close <= ema
                and latest.close < latest.open
                and latest.high < max(row.high for row in structure)
            )
        if not valid:
            return None
        setup_id = f"{ctx.symbol}:{side.value}:1h_pullback_to_ema:{latest.ts.isoformat()}"
        return setup_id, {
            "ema_20": ema,
            "atr": atr,
            "distance_close_to_ema_bps": (latest.close / ema - 1) * 10_000,
            "setup_age_minutes": age.total_seconds() / 60.0,
        }

    def _five_minute_confirmation(
        self,
        ctx: MarketContext,
        side: Side,
    ) -> dict[str, float] | None:
        rows = ctx.candles.get("5m", ())
        if len(rows) < 21:
            return None
        latest = rows[-1]
        age = ctx.ts - latest.ts
        if age < timedelta(0) or age > timedelta(minutes=self.config.confirmation_expiry_minutes):
            return None
        direction_ok = (
            latest.close > latest.open and latest.close > rows[-2].close
            if side is Side.LONG
            else latest.close < latest.open and latest.close < rows[-2].close
        )
        volume_z = _latest_volume_z(rows)
        body = _body_ratio(latest)
        if (
            not direction_ok
            or body < self.config.min_five_minute_body_ratio
            or volume_z < self.config.min_five_minute_volume_z
        ):
            return None
        return {
            "body_ratio": body,
            "volume_z": volume_z,
            "age_minutes": age.total_seconds() / 60.0,
        }

    def _one_minute_trigger(
        self,
        ctx: MarketContext,
        side: Side,
    ) -> dict[str, float] | None:
        rows = ctx.candles.get("1m", ())
        if len(rows) < 21 or rows[-1].ts != ctx.ts:
            return None
        latest, previous = rows[-1], rows[-2]
        direction_ok = (
            latest.close > latest.open and latest.close > previous.high
            if side is Side.LONG
            else latest.close < latest.open and latest.close < previous.low
        )
        volume_z = _latest_volume_z(rows)
        body = _body_ratio(latest)
        if (
            not direction_ok
            or body < self.config.min_one_minute_body_ratio
            or volume_z < self.config.min_one_minute_volume_z
        ):
            return None
        return {
            "body_ratio": body,
            "volume_z": volume_z,
            "break_level": previous.high if side is Side.LONG else previous.low,
        }

    def evaluate(self, ctx: MarketContext) -> SignalCandidate | None:
        side, bias_metrics = self._bias(ctx)
        if side is None:
            return None
        setup = self._one_hour_setup(ctx, side)
        if setup is None:
            return None
        setup_id, setup_metrics = setup
        if self._last_fired_setup.get(ctx.symbol) == setup_id:
            return None
        last_fire = self._last_fire_ts.get(ctx.symbol)
        if last_fire is not None and ctx.ts - last_fire < timedelta(
            minutes=self.config.cooldown_minutes
        ):
            return None
        confirmation = self._five_minute_confirmation(ctx, side)
        if confirmation is None:
            return None
        trigger = self._one_minute_trigger(ctx, side)
        if trigger is None:
            return None

        five_minute_rows = ctx.candles["5m"]
        five_minute_atr = fmean(_true_ranges(five_minute_rows)[-14:])
        entry = ctx.candles["1m"][-1].close
        stop_bps = min(
            self.config.max_stop_bps,
            max(
                self.config.min_stop_bps,
                five_minute_atr / entry * 10_000 * self.config.stop_atr_fraction,
            ),
        )
        costs = self.fee_model.breakdown(
            ctx.symbol,
            entry_is_maker=self.config.prefer_maker,
            hold_seconds=self.config.expected_hold_seconds,
        )
        target_bps = max(
            stop_bps * self.config.reward_risk,
            costs.total_bps * self.config.minimum_target_cost_multiple,
        )
        probability = self.config.research_probability_prior
        confidence = self.config.research_confidence_prior
        raw_expectancy = probability * target_bps - (1 - probability) * stop_bps
        l2_agrees = (side is Side.LONG and ctx.l2.imbalance > 0) or (
            side is Side.SHORT and ctx.l2.imbalance < 0
        )
        candidate = SignalCandidate(
            scanner_id=self.scanner_id,
            symbol=ctx.symbol,
            side=side,
            decision_ts=ctx.ts,
            entry_price=entry,
            stop_loss=_price_at_bps(entry, side, -stop_bps),
            take_profits=(_price_at_bps(entry, side, target_bps),),
            time_stop_seconds=self.config.time_stop_seconds,
            expected_hold_seconds=self.config.expected_hold_seconds,
            expected_move_bps=target_bps,
            raw_expectancy_bps=raw_expectancy,
            modeled_cost_bps=costs.total_bps,
            fee_adjusted_expectancy_bps=raw_expectancy - costs.total_bps,
            scalper_probability=probability,
            confidence=confidence,
            entry_is_maker=self.config.prefer_maker,
            metadata={
                "signal_type": "htf_pullback_continuation",
                "htf_bias": 1 if side is Side.LONG else -1,
                "setup": "1h_pullback_to_ema_20",
                "confirmation": "5m_directional_close_and_volume",
                "entry": "1m_previous_bar_break_and_volume",
                "setup_id": setup_id,
                "hierarchy": {
                    "4h_bias": bias_metrics,
                    "1h_setup": setup_metrics,
                    "5m_confirmation": confirmation,
                    "1m_trigger": trigger,
                },
                "calibration": {
                    "status": "uncalibrated_structural_prior",
                    "probability": probability,
                    "confidence": confidence,
                    "promotion_eligible": False,
                },
                "regime": ctx.regime.value,
                "regime_profile": ctx.regime_profile.to_dict(),
                "l2_confirmation": {
                    "status": ctx.l2.status,
                    "agrees": l2_agrees,
                    "imbalance": ctx.l2.imbalance,
                    "cvd": ctx.l2.cvd,
                    "context_only": True,
                    "used_for_signal": False,
                    "used_for_execution": False,
                },
                "fee_breakdown": costs.to_dict(),
            },
        )
        self._last_fired_setup[ctx.symbol] = setup_id
        self._last_fire_ts[ctx.symbol] = ctx.ts
        return candidate


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
        raw_expectancy = (
            estimate.probability * estimate.expected_move_bps
            - (1 - estimate.probability) * stop_bps
        )
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
        raw_expectancy = (
            estimate.probability * estimate.expected_move_bps
            - (1 - estimate.probability) * stop_bps
        )
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
