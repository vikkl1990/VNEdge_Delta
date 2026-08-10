"""MarketContext assembly from closed candles plus optional context feeds."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import replace
from datetime import UTC, datetime

from vnedge.scalping.delta_engine.candle_store import MultiTimeframeCandleStore
from vnedge.scalping.delta_engine.change_point import (
    CausalCusumConfig,
    CausalCusumDetector,
)
from vnedge.scalping.delta_engine.regime import (
    RegimeEngine,
    RegimeProfileConfig,
    build_features,
    build_regime_profile,
    regime_profile_flags,
    session_regime,
)
from vnedge.scalping.delta_engine.types import (
    L2Confirmation,
    MarketContext,
    RegimeProfile,
)


class MarketContextBuilder:
    def __init__(
        self,
        candle_store: MultiTimeframeCandleStore,
        regime_engine: RegimeEngine | None = None,
        regime_profile_config: RegimeProfileConfig | None = None,
        change_point_config: CausalCusumConfig | None = None,
        *,
        max_l2_age_seconds: float = 2.0,
    ) -> None:
        if max_l2_age_seconds <= 0:
            raise ValueError("max_l2_age_seconds must be positive")
        self.candle_store = candle_store
        self.regime_engine = regime_engine or RegimeEngine()
        self.regime_profile_config = regime_profile_config or RegimeProfileConfig()
        self.change_point_config = change_point_config or CausalCusumConfig()
        self.max_l2_age_seconds = max_l2_age_seconds
        self._funding: dict[str, deque[tuple[datetime, float]]] = defaultdict(
            lambda: deque(maxlen=24)
        )
        self._l2: dict[str, L2Confirmation] = {}
        self._regime_profile_cache: dict[str, tuple[datetime | None, RegimeProfile]] = {}
        self._change_point_detectors: dict[str, CausalCusumDetector] = {}

    def update_funding(self, symbol: str, rate: float, observed_at: datetime) -> None:
        ts = observed_at.replace(tzinfo=UTC) if observed_at.tzinfo is None else observed_at
        rows = self._funding[symbol.upper()]
        value = float(rate)
        if not rows or rows[-1][1] != value:
            rows.append((ts.astimezone(UTC), value))

    def update_l2_confirmation(
        self,
        symbol: str,
        *,
        imbalance: float,
        cvd: float,
        observed_at: datetime,
        status: str = "fresh",
        imbalance_z: float = 0.0,
        buy_aggression_ratio: float = 0.5,
        absorption_score: float = 0.0,
        depth_usd: float = 0.0,
        sequence_healthy: bool | None = None,
    ) -> None:
        self._l2[symbol.upper()] = L2Confirmation(
            imbalance=imbalance,
            cvd=cvd,
            imbalance_z=imbalance_z,
            buy_aggression_ratio=buy_aggression_ratio,
            absorption_score=absorption_score,
            depth_usd=depth_usd,
            sequence_healthy=sequence_healthy,
            status=status,
            observed_at=observed_at,
        )

    def build(self, symbol: str, *, now: datetime | None = None) -> MarketContext:
        native = symbol.upper()
        current = now or datetime.now(UTC)
        candles = self.candle_store.snapshot(native)
        latest = max((rows[-1].ts for rows in candles.values() if rows), default=None)
        if latest is None:
            raise RuntimeError(f"no closed candles for {native}")
        if latest > current:
            raise ValueError("latest closed candle is in the future")
        funding_rows = self._funding[native]
        funding_rate = funding_rows[-1][1] if funding_rows else 0.0
        funding_velocity = (
            funding_rows[-1][1] - funding_rows[-2][1] if len(funding_rows) >= 2 else 0.0
        )
        funding_values = [value for _, value in funding_rows]
        funding_percentile = (
            (
                sum(value < funding_rate for value in funding_values)
                + 0.5 * sum(value == funding_rate for value in funding_values)
            )
            / len(funding_values)
            if funding_values
            else 0.5
        )
        l2 = self._l2.get(native, L2Confirmation())
        if l2.observed_at is not None and (
            current.astimezone(UTC) - l2.observed_at
        ).total_seconds() > self.max_l2_age_seconds:
            l2 = L2Confirmation(
                imbalance=l2.imbalance,
                cvd=l2.cvd,
                imbalance_z=l2.imbalance_z,
                buy_aggression_ratio=l2.buy_aggression_ratio,
                absorption_score=l2.absorption_score,
                depth_usd=l2.depth_usd,
                sequence_healthy=l2.sequence_healthy,
                status="stale",
                observed_at=l2.observed_at,
            )
        features = build_features(candles)
        features.update(
            {
                "funding_percentile": funding_percentile,
                "funding_velocity": funding_velocity,
                "l2_imbalance": l2.imbalance,
                "l2_imbalance_z": l2.imbalance_z,
                "cvd_usd": l2.cvd,
                "buy_aggression_ratio": l2.buy_aggression_ratio,
                "absorption_score": l2.absorption_score,
                "l2_depth_usd": l2.depth_usd,
            }
        )
        source_rows = candles.get(self.regime_profile_config.source_timeframe, ())
        source_ts = source_rows[-1].ts if source_rows else None
        cached = self._regime_profile_cache.get(native)
        if cached is None or cached[0] != source_ts:
            base_profile = build_regime_profile(
                candles,
                funding_rate=funding_rate,
                l2=l2,
                config=self.regime_profile_config,
            )
            self._regime_profile_cache[native] = (source_ts, base_profile)
        else:
            base_profile = cached[1]
        detector = self._change_point_detectors.setdefault(
            native,
            CausalCusumDetector(self.change_point_config),
        )
        change_point_rows = candles.get(self.change_point_config.source_timeframe, ())
        change_point = detector.update(change_point_rows)
        profile = replace(
            base_profile,
            session=session_regime(latest.hour),
            flags=regime_profile_flags(
                funding_rate,
                l2,
                self.regime_profile_config,
            ),
            change_point=change_point,
        )
        return MarketContext(
            symbol=native,
            ts=latest,
            candles=candles,
            regime=self.regime_engine.classify(
                candles,
                funding_rate=funding_rate,
                funding_percentile=funding_percentile,
            ),
            funding_rate=funding_rate,
            funding_velocity=funding_velocity,
            l2=l2,
            regime_profile=profile,
            features=features,
        )
