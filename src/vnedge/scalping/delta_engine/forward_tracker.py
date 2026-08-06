"""Orderless forward outcomes for live Delta scalper research alerts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from vnedge.scalping.delta_engine.fee_model import DeltaFeeModel
from vnedge.scalping.delta_engine.types import Candle, Side, SignalCandidate


@dataclass
class _OpenObservation:
    candidate: SignalCandidate
    entry_ts: datetime
    entry_price: float
    stop_price: float
    target_price: float
    mfe_bps: float = 0.0
    mae_bps: float = 0.0


@dataclass(frozen=True)
class ForwardOutcome:
    key: str
    scanner_id: str
    symbol: str
    side: str
    regime: str
    trend_regime: str
    trend_direction: str
    volatility_regime: str
    session_regime: str
    regime_shift: bool
    change_point_window: str
    change_point_bars_since_shift: int | None
    change_point_return_score: float
    change_point_volatility_score: float
    decision_ts: str
    entry_ts: str
    exit_ts: str
    entry_price: float
    exit_price: float
    exit_reason: str
    hold_seconds: int
    expected_move_bps: float
    expected_net_bps: float
    gross_bps: float
    cost_bps: float
    net_bps: float
    mfe_bps: float
    mae_bps: float
    l2_quality: str
    l2_status: str
    l2_sequence_healthy: bool | None
    l2_imbalance_z: float | None
    scalper_compliant: bool
    same_bar_ambiguous: bool

    def to_dict(self) -> dict[str, object]:
        return self.__dict__.copy()


class ForwardOutcomeTracker:
    """Labels every accepted alert independently; it never creates positions."""

    def __init__(self, fee_model: DeltaFeeModel) -> None:
        self.fee_model = fee_model
        self._seen: set[str] = set()
        self._pending: dict[str, list[SignalCandidate]] = {}
        self._open: dict[str, list[_OpenObservation]] = {}

    def register(self, candidate: SignalCandidate) -> bool:
        if candidate.dedup_key in self._seen:
            return False
        self._seen.add(candidate.dedup_key)
        self._pending.setdefault(candidate.symbol, []).append(candidate)
        return True

    def on_closed_bar(self, symbol: str, bar: Candle) -> tuple[ForwardOutcome, ...]:
        if bar.tf != "1m":
            return ()
        native = symbol.upper()
        pending = self._pending.get(native, [])
        still_pending: list[SignalCandidate] = []
        for candidate in pending:
            if candidate.decision_ts >= bar.ts:
                still_pending.append(candidate)
                continue
            self._open.setdefault(native, []).append(self._open_at_next_bar(candidate, bar))
        self._pending[native] = still_pending
        resolved: list[ForwardOutcome] = []
        active: list[_OpenObservation] = []
        for observation in self._open.get(native, []):
            outcome = self._evaluate(observation, bar)
            if outcome is None:
                active.append(observation)
            else:
                resolved.append(outcome)
        self._open[native] = active
        return tuple(resolved)

    @staticmethod
    def _open_at_next_bar(candidate: SignalCandidate, bar: Candle) -> _OpenObservation:
        entry = bar.open
        if candidate.side is Side.LONG:
            stop_bps = (1 - candidate.stop_loss / candidate.entry_price) * 10_000
            target_bps = (candidate.take_profits[0] / candidate.entry_price - 1) * 10_000
            stop = entry * (1 - stop_bps / 10_000)
            target = entry * (1 + target_bps / 10_000)
        else:
            stop_bps = (candidate.stop_loss / candidate.entry_price - 1) * 10_000
            target_bps = (1 - candidate.take_profits[0] / candidate.entry_price) * 10_000
            stop = entry * (1 + stop_bps / 10_000)
            target = entry * (1 - target_bps / 10_000)
        return _OpenObservation(
            candidate=candidate,
            entry_ts=bar.ts - timedelta(minutes=1),
            entry_price=entry,
            stop_price=stop,
            target_price=target,
        )

    def _evaluate(self, active: _OpenObservation, bar: Candle) -> ForwardOutcome | None:
        candidate = active.candidate
        entry = active.entry_price
        hold = int((bar.ts - active.entry_ts).total_seconds())
        if candidate.side is Side.LONG:
            favorable = (bar.high / entry - 1) * 10_000
            adverse = max(0.0, (1 - bar.low / entry) * 10_000)
            stop_hit = bar.low <= active.stop_price
            target_hit = bar.high >= active.target_price
        else:
            favorable = (entry / bar.low - 1) * 10_000
            adverse = max(0.0, (bar.high / entry - 1) * 10_000)
            stop_hit = bar.high >= active.stop_price
            target_hit = bar.low <= active.target_price
        active.mfe_bps = max(active.mfe_bps, favorable)
        active.mae_bps = max(active.mae_bps, adverse)
        if stop_hit:
            exit_price, reason = active.stop_price, "stop"
        elif target_hit:
            exit_price, reason = active.target_price, "target_1"
        elif hold >= candidate.time_stop_seconds:
            exit_price, reason = bar.close, "time_stop"
        else:
            return None
        costs = self.fee_model.breakdown(
            candidate.symbol,
            entry_is_maker=candidate.entry_is_maker,
            hold_seconds=hold,
        )
        if candidate.side is Side.LONG:
            gross_bps = (exit_price / entry - 1) * 10_000
        else:
            gross_bps = (entry / exit_price - 1) * 10_000
        l2 = candidate.metadata.get("l2_confirmation")
        l2_context = dict(l2) if isinstance(l2, dict) else {}
        l2_status = str(l2_context.get("status") or "unavailable")
        l2_sequence_healthy = l2_context.get("sequence_healthy")
        if l2_sequence_healthy is False:
            l2_quality = "unhealthy_sequence"
        elif l2_status == "stale":
            l2_quality = "stale"
        elif l2_status in {"fresh", "aligned"}:
            l2_quality = "healthy_fresh"
        else:
            l2_quality = "unavailable"
        raw_imbalance_z = l2_context.get("imbalance_z")
        try:
            l2_imbalance_z = (
                float(raw_imbalance_z) if raw_imbalance_z is not None else None
            )
        except (TypeError, ValueError):
            l2_imbalance_z = None
        raw_profile = candidate.metadata.get("regime_profile")
        profile = raw_profile if isinstance(raw_profile, dict) else {}
        raw_change_point = profile.get("change_point")
        change_point = raw_change_point if isinstance(raw_change_point, dict) else {}
        raw_bars_since_shift = change_point.get("bars_since_shift")
        return ForwardOutcome(
            key=candidate.dedup_key,
            scanner_id=candidate.scanner_id,
            symbol=candidate.symbol,
            side=candidate.side.value,
            regime=str(candidate.metadata.get("regime") or "unknown"),
            trend_regime=str(profile.get("trend") or "unknown"),
            trend_direction=str(profile.get("trend_direction") or "unknown"),
            volatility_regime=str(profile.get("volatility") or "unknown"),
            session_regime=str(profile.get("session") or "unknown"),
            regime_shift=bool(change_point.get("regime_shift")),
            change_point_window=str(
                change_point.get("shift_window") or "unavailable"
            ),
            change_point_bars_since_shift=(
                int(raw_bars_since_shift)
                if isinstance(raw_bars_since_shift, (int, float))
                else None
            ),
            change_point_return_score=float(
                change_point.get("return_score") or 0.0
            ),
            change_point_volatility_score=float(
                change_point.get("volatility_score") or 0.0
            ),
            decision_ts=candidate.decision_ts.isoformat(),
            entry_ts=active.entry_ts.isoformat(),
            exit_ts=bar.ts.isoformat(),
            entry_price=entry,
            exit_price=exit_price,
            exit_reason=reason,
            hold_seconds=hold,
            expected_move_bps=candidate.expected_move_bps,
            expected_net_bps=candidate.fee_adjusted_expectancy_bps,
            gross_bps=gross_bps,
            cost_bps=costs.total_bps,
            net_bps=gross_bps - costs.total_bps,
            mfe_bps=active.mfe_bps,
            mae_bps=active.mae_bps,
            l2_quality=l2_quality,
            l2_status=l2_status,
            l2_sequence_healthy=(
                l2_sequence_healthy if isinstance(l2_sequence_healthy, bool) else None
            ),
            l2_imbalance_z=l2_imbalance_z,
            scalper_compliant=costs.scalper_eligible,
            same_bar_ambiguous=stop_hit and target_hit,
        )
