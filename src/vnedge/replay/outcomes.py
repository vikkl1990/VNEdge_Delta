"""Causal next-trade outcomes for every accepted replay candidate."""

from __future__ import annotations

from dataclasses import dataclass

from vnedge.exchange.delta_public_schema import parse_public_trade
from vnedge.execution.journal import DecisionJournal
from vnedge.replay.models import RecordedEvent
from vnedge.scalping.delta_engine.types import Side, SignalCandidate


@dataclass(frozen=True)
class ReplayForwardOutcome:
    candidate_key: str
    scanner_id: str
    symbol: str
    side: str
    decision_ts_us: int
    entry_ts_us: int | None
    exit_ts_us: int | None
    entry_price: float | None
    exit_price: float | None
    exit_reason: str
    mfe_bps: float
    mae_bps: float
    gross_bps: float
    modeled_cost_bps: float
    net_bps: float
    research_only: bool = True
    can_trade: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            **self.__dict__,
            "research_only": True,
            "can_trade": False,
            "order_route": "absent",
        }


@dataclass
class _Pending:
    candidate: SignalCandidate
    decision_ts_us: int


@dataclass
class _Open:
    candidate: SignalCandidate
    decision_ts_us: int
    entry_ts_us: int
    entry_price: float
    stop_price: float
    target_price: float
    mfe_bps: float = 0.0
    mae_bps: float = 0.0


class ReplayForwardTracker:
    """Tick-path research tracker. It cannot create positions or orders."""

    def __init__(
        self,
        journal: DecisionJournal | None = None,
        *,
        signal_to_fill_latency_ms: int = 100,
    ) -> None:
        if signal_to_fill_latency_ms < 0:
            raise ValueError("signal-to-fill latency cannot be negative")
        self.journal = journal
        self.signal_to_fill_latency_us = signal_to_fill_latency_ms * 1_000
        self._seen: set[str] = set()
        self._pending: dict[str, list[_Pending]] = {}
        self._open: dict[str, list[_Open]] = {}
        self._last_trade: dict[str, tuple[int, float]] = {}
        self.outcomes: list[ReplayForwardOutcome] = []

    def register(self, candidate: SignalCandidate, *, decision_ts_us: int) -> bool:
        if candidate.dedup_key in self._seen:
            return False
        self._seen.add(candidate.dedup_key)
        self._pending.setdefault(candidate.symbol, []).append(
            _Pending(candidate, decision_ts_us)
        )
        return True

    def on_event(self, event: RecordedEvent) -> tuple[ReplayForwardOutcome, ...]:
        if event.event_type != "trade":
            return ()
        try:
            price = parse_public_trade(event.raw_message).price
        except (TypeError, ValueError) as exc:
            raise ValueError(f"trade event has invalid price: {event.event_id}") from exc
        if price <= 0:
            raise ValueError(f"trade event has non-positive price: {event.event_id}")
        symbol = event.symbol
        # Availability time, not exchange event time, defines when a simulated
        # strategy could have observed and acted on this trade.
        now_us = event.local_recv_ns // 1_000
        self._last_trade[symbol] = (now_us, price)
        resolved: list[ReplayForwardOutcome] = []
        active: list[_Open] = []
        for row in self._open.get(symbol, []):
            outcome = self._update(row, now_us=now_us, price=price)
            if outcome is None:
                active.append(row)
            else:
                resolved.append(outcome)
        self._open[symbol] = active

        still_pending: list[_Pending] = []
        for row in self._pending.get(symbol, []):
            if now_us < row.decision_ts_us + self.signal_to_fill_latency_us:
                still_pending.append(row)
                continue
            if row.candidate.entry_is_maker:
                resolved.append(self._unresolved(row, "unsupported_maker_queue_model"))
                continue
            direction = 1 if row.candidate.side is Side.LONG else -1
            stop_distance = abs(row.candidate.stop_loss / row.candidate.entry_price - 1)
            target_distance = abs(row.candidate.take_profits[0] / row.candidate.entry_price - 1)
            self._open.setdefault(symbol, []).append(
                _Open(
                    candidate=row.candidate,
                    decision_ts_us=row.decision_ts_us,
                    entry_ts_us=now_us,
                    entry_price=price,
                    stop_price=price * (1 - direction * stop_distance),
                    target_price=price * (1 + direction * target_distance),
                )
            )
        self._pending[symbol] = still_pending
        self._record(resolved)
        return tuple(resolved)

    def finalize(self) -> tuple[ReplayForwardOutcome, ...]:
        rows: list[ReplayForwardOutcome] = []
        for pending in self._pending.values():
            rows.extend(self._unresolved(row, "missed_entry") for row in pending)
        for symbol, opened in self._open.items():
            last = self._last_trade.get(symbol)
            for row in opened:
                if last is None:
                    rows.append(self._unresolved(_Pending(row.candidate, row.decision_ts_us), "open"))
                    continue
                now_us, price = last
                rows.append(self._incomplete(row, now_us=now_us, price=price))
        self._pending.clear()
        self._open.clear()
        self._record(rows)
        return tuple(rows)

    def _update(
        self,
        row: _Open,
        *,
        now_us: int,
        price: float,
    ) -> ReplayForwardOutcome | None:
        direction = 1 if row.candidate.side is Side.LONG else -1
        gross_bps = direction * (price / row.entry_price - 1) * 10_000.0
        row.mfe_bps = max(row.mfe_bps, gross_bps)
        row.mae_bps = max(row.mae_bps, -gross_bps)
        stopped = price <= row.stop_price if direction > 0 else price >= row.stop_price
        targeted = price >= row.target_price if direction > 0 else price <= row.target_price
        if stopped:
            # A stop-market order cannot assume a fill back at the barrier after
            # the observed tape has already traded through it.
            return self._resolve(row, now_us, price, "stop")
        if targeted:
            return self._resolve(row, now_us, row.target_price, "target_1")
        if now_us - row.entry_ts_us >= row.candidate.time_stop_seconds * 1_000_000:
            return self._resolve(row, now_us, price, "time_stop")
        return None

    def _resolve(
        self,
        row: _Open,
        exit_ts_us: int,
        exit_price: float,
        reason: str,
    ) -> ReplayForwardOutcome:
        direction = 1 if row.candidate.side is Side.LONG else -1
        gross = direction * (exit_price / row.entry_price - 1) * 10_000.0
        return ReplayForwardOutcome(
            candidate_key=row.candidate.dedup_key,
            scanner_id=row.candidate.scanner_id,
            symbol=row.candidate.symbol,
            side=row.candidate.side.value,
            decision_ts_us=row.decision_ts_us,
            entry_ts_us=row.entry_ts_us,
            exit_ts_us=exit_ts_us,
            entry_price=row.entry_price,
            exit_price=exit_price,
            exit_reason=reason,
            mfe_bps=max(0.0, row.mfe_bps),
            mae_bps=max(0.0, row.mae_bps),
            gross_bps=gross,
            modeled_cost_bps=row.candidate.modeled_cost_bps,
            net_bps=gross - row.candidate.modeled_cost_bps,
        )

    def _incomplete(self, row: _Open, *, now_us: int, price: float) -> ReplayForwardOutcome:
        outcome = self._resolve(row, now_us, price, "incomplete_replay_tail")
        return ReplayForwardOutcome(
            **{
                **outcome.__dict__,
                "gross_bps": 0.0,
                "net_bps": 0.0,
            }
        )

    @staticmethod
    def _unresolved(row: _Pending, reason: str) -> ReplayForwardOutcome:
        candidate = row.candidate
        return ReplayForwardOutcome(
            candidate_key=candidate.dedup_key,
            scanner_id=candidate.scanner_id,
            symbol=candidate.symbol,
            side=candidate.side.value,
            decision_ts_us=row.decision_ts_us,
            entry_ts_us=None,
            exit_ts_us=None,
            entry_price=None,
            exit_price=None,
            exit_reason=reason,
            mfe_bps=0.0,
            mae_bps=0.0,
            gross_bps=0.0,
            modeled_cost_bps=candidate.modeled_cost_bps,
            net_bps=0.0,
        )

    def _record(self, outcomes: list[ReplayForwardOutcome]) -> None:
        for outcome in outcomes:
            self.outcomes.append(outcome)
            if self.journal is not None:
                self.journal.append("delta_event_replay_forward_outcome", outcome.to_dict())
