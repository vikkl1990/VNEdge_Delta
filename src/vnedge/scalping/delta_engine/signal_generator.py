"""Candidate gating, ranking, journaling, and risk-gateway adaptation."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from time import perf_counter_ns

from vnedge.execution.journal import DecisionJournal
from vnedge.risk.risk_manager import AccountState, OrderIntent
from vnedge.scalping.delta_engine.context import MarketContextBuilder
from vnedge.scalping.delta_engine.scanners import Scanner
from vnedge.scalping.delta_engine.types import SignalCandidate
from vnedge.scalping.microstructure import MarketMicroState
from vnedge.scalping.risk import ScalperRiskDecision, ScalperRiskGateway


@dataclass(frozen=True)
class SignalGateConfig:
    min_expectancy_bps: float = 8.0
    min_probability: float = 0.70
    min_confidence: float = 0.60
    allowed_symbols: tuple[str, ...] = ("BTCUSD", "ETHUSD")
    primary_timeframes: tuple[str, ...] = ("1m", "5m")


@dataclass(frozen=True)
class PipelineStage:
    name: str
    status: str
    duration_us: int
    detail: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": self.status,
            "duration_us": self.duration_us,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class EngineDecision:
    symbol: str
    decision_ts: datetime
    selected: SignalCandidate | None
    evaluated: tuple[SignalCandidate, ...]
    rejection_reasons: tuple[str, ...]
    pipeline_trace: tuple[PipelineStage, ...] = ()
    total_duration_us: int = 0
    journal_write_success: bool | None = None
    duplicate: bool = False
    research_only: bool = True
    can_trade: bool = False
    can_promote: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "decision_ts": self.decision_ts.isoformat(),
            "selected": self.selected.to_dict() if self.selected else None,
            "evaluated": [row.to_dict() for row in self.evaluated],
            "rejection_reasons": list(self.rejection_reasons),
            "pipeline_trace": [stage.to_dict() for stage in self.pipeline_trace],
            "total_duration_us": self.total_duration_us,
            "journal_write_success": self.journal_write_success,
            "duplicate": self.duplicate,
            "research_only": True,
            "can_trade": False,
            "can_promote": False,
        }


class DeltaScalperSignalGenerator:
    """Builds and journals decisions but owns no order-submission capability."""

    def __init__(
        self,
        context_builder: MarketContextBuilder,
        scanners: tuple[Scanner, ...],
        *,
        journal: DecisionJournal | None = None,
        gates: SignalGateConfig | None = None,
    ) -> None:
        self.context_builder = context_builder
        self.scanners = scanners
        self.journal = journal
        self.gates = gates or SignalGateConfig()
        self._seen: set[str] = set()

    def on_candle_closed(
        self,
        symbol: str,
        timeframe: str,
        *,
        now: datetime | None = None,
    ) -> EngineDecision:
        pipeline_started = perf_counter_ns()
        if timeframe not in self.gates.primary_timeframes:
            current = now or datetime.now(UTC)
            return EngineDecision(
                symbol.upper(),
                current,
                None,
                (),
                ("non_primary_timeframe",),
                pipeline_trace=(
                    PipelineStage("timeframe_gate", "rejected", 0, timeframe),
                ),
                total_duration_us=self._elapsed_us(pipeline_started),
            )
        trace: list[PipelineStage] = []
        context_started = perf_counter_ns()
        try:
            ctx = self.context_builder.build(symbol, now=now)
        # Deliberate outer boundary: malformed context must become a rejection.
        except Exception as exc:  # noqa: BLE001
            trace.append(
                PipelineStage(
                    "context_builder",
                    "error",
                    self._elapsed_us(context_started),
                    type(exc).__name__,
                )
            )
            decision = EngineDecision(
                symbol.upper(),
                now or datetime.now(UTC),
                None,
                (),
                (f"context_error:{type(exc).__name__}",),
                pipeline_trace=tuple(trace),
                total_duration_us=self._elapsed_us(pipeline_started),
            )
            return self._journal(decision)
        trace.append(
            PipelineStage(
                "context_builder",
                "complete",
                self._elapsed_us(context_started),
                ctx.regime.value,
            )
        )
        candidates_list: list[SignalCandidate] = []
        reasons: list[str] = []
        for scanner in self.scanners:
            scanner_started = perf_counter_ns()
            if not scanner.regime_enabled(ctx):
                reasons.append(
                    f"{scanner.scanner_id}:regime_not_enabled:{ctx.regime.value}"
                )
                trace.append(
                    PipelineStage(
                        f"scanner:{scanner.scanner_id}",
                        "regime_filtered",
                        self._elapsed_us(scanner_started),
                        ctx.regime.value,
                    )
                )
                continue
            try:
                candidate = scanner.evaluate(ctx)
            # Deliberate plugin boundary: one scanner cannot stop its peers.
            except Exception as exc:  # noqa: BLE001
                reasons.append(f"{scanner.scanner_id}:scanner_error:{type(exc).__name__}")
                trace.append(
                    PipelineStage(
                        f"scanner:{scanner.scanner_id}",
                        "error",
                        self._elapsed_us(scanner_started),
                        type(exc).__name__,
                    )
                )
                continue
            if candidate is not None:
                candidates_list.append(candidate)
            trace.append(
                PipelineStage(
                    f"scanner:{scanner.scanner_id}",
                    "candidate" if candidate is not None else "no_signal",
                    self._elapsed_us(scanner_started),
                )
            )
        candidates = tuple(candidates_list)
        accepted: list[SignalCandidate] = []
        gates_started = perf_counter_ns()
        for candidate in candidates:
            failed: list[str] = []
            if candidate.symbol not in self.gates.allowed_symbols:
                failed.append("symbol_not_enabled")
            if candidate.fee_adjusted_expectancy_bps < self.gates.min_expectancy_bps:
                failed.append("fee_adjusted_expectancy_below_gate")
            if candidate.scalper_probability < self.gates.min_probability:
                failed.append("probability_below_gate")
            if candidate.confidence < self.gates.min_confidence:
                failed.append("confidence_below_gate")
            if candidate.expected_hold_seconds > candidate.time_stop_seconds:
                failed.append("scalper_window_noncompliant")
            if failed:
                reasons.extend(f"{candidate.scanner_id}:{reason}" for reason in failed)
            else:
                accepted.append(candidate)
        trace.append(
            PipelineStage(
                "fee_probability_confidence_gates",
                "complete",
                self._elapsed_us(gates_started),
                f"{len(accepted)}/{len(candidates)} accepted",
            )
        )
        selection_started = perf_counter_ns()
        selected = max(accepted, key=lambda row: row.rank_score) if accepted else None
        duplicate = False
        if selected is not None:
            duplicate = selected.dedup_key in self._seen
            if duplicate:
                reasons.append(f"{selected.scanner_id}:duplicate_decision")
                selected = None
            else:
                self._seen.add(selected.dedup_key)
        trace.append(
            PipelineStage(
                "ranking_and_dedup",
                "selected" if selected is not None else "no_selection",
                self._elapsed_us(selection_started),
                selected.scanner_id if selected is not None else None,
            )
        )
        decision = EngineDecision(
            symbol=ctx.symbol,
            decision_ts=ctx.ts,
            selected=selected,
            evaluated=candidates,
            rejection_reasons=tuple(reasons),
            pipeline_trace=tuple(trace),
            total_duration_us=self._elapsed_us(pipeline_started),
            duplicate=duplicate,
        )
        return self._journal(decision)

    @staticmethod
    def _elapsed_us(started_ns: int) -> int:
        return max(0, (perf_counter_ns() - started_ns) // 1_000)

    def _journal(self, decision: EngineDecision) -> EngineDecision:
        if self.journal is not None:
            written = self.journal.append(
                "delta_scalper_research_decision", decision.to_dict()
            )
            decision = replace(decision, journal_write_success=written)
            if not written and decision.selected is not None:
                decision = replace(
                    decision,
                    selected=None,
                    rejection_reasons=(*decision.rejection_reasons, "journal_unavailable"),
                )
        return decision


@dataclass(frozen=True)
class GatewayEvaluation:
    intent: OrderIntent
    risk: ScalperRiskDecision
    candidate: SignalCandidate
    submitted: bool = False


class ScalperRiskAdapter:
    """Converts a candidate into the existing gateway contract; never submits."""

    def __init__(self, gateway: ScalperRiskGateway, *, leverage: float = 1.0) -> None:
        self.gateway = gateway
        self.leverage = leverage

    def evaluate(
        self,
        candidate: SignalCandidate,
        *,
        notional_usd: float,
        account: AccountState,
        market: MarketMicroState,
        now: datetime | None = None,
    ) -> GatewayEvaluation:
        if notional_usd <= 0:
            raise ValueError("notional_usd must be positive")
        intent = OrderIntent(
            symbol=candidate.symbol,
            side=candidate.side.value,
            quantity=notional_usd / candidate.entry_price,
            notional_usd=notional_usd,
            leverage=self.leverage,
            reduce_only=False,
            strategy_id=candidate.scanner_id,
            order_type="limit" if candidate.entry_is_maker else "market",
            limit_price=candidate.entry_price if candidate.entry_is_maker else None,
            time_in_force="PO" if candidate.entry_is_maker else None,
        )
        risk = self.gateway.evaluate(
            intent,
            account,
            market,
            expected_edge_bps=candidate.expected_move_bps,
            now=now,
        )
        return GatewayEvaluation(intent=intent, risk=risk, candidate=candidate)
