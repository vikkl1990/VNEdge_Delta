"""External TradingView-style signal intake.

This module adapts manual-indicator/webhook alerts into VNEDGE's internal
``SignalCandidate`` contract. It is intentionally upstream of sizing, the
pre-trade gateway, journaling, and order submission. A parsed external alert is
not trusted as edge and never becomes an exchange order from here.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Mapping

from vnedge.execution.signal_arbiter import ExecutionRoute, SignalCandidate
from vnedge.strategy.base_strategy import SignalIntent


ExternalSide = Literal["long", "short"]


@dataclass(frozen=True)
class ExternalSignalPolicy:
    source_id: str = "tradingview"
    min_stage: int = 3
    min_score: float = 60.0
    min_confluence: int = 3
    min_reward_r: float = 1.0
    max_entry_slippage_bps: float = 50.0
    expected_cost_bps: float = 9.0
    source_verified: bool = False
    verified_expected_edge_bps: float | None = None
    verified_profit_factor: float | None = None
    verified_route: ExecutionRoute = "UNKNOWN"
    tp_splits: tuple[float, float, float, float] = (0.60, 0.20, 0.10, 0.10)

    def __post_init__(self) -> None:
        if self.min_stage < 1:
            raise ValueError("min_stage must be positive")
        if self.min_score < 0 or self.min_confluence < 0:
            raise ValueError("score/confluence floors cannot be negative")
        if self.min_reward_r <= 0:
            raise ValueError("min_reward_r must be positive")
        if self.max_entry_slippage_bps < 0 or self.expected_cost_bps < 0:
            raise ValueError("slippage/cost floors cannot be negative")
        if len(self.tp_splits) != 4:
            raise ValueError("tp_splits must contain TP1/TP2/TP3/runner weights")
        if any(v < 0 for v in self.tp_splits) or sum(self.tp_splits) > 1.000001:
            raise ValueError("tp_splits must be non-negative and sum to <= 1")

    def to_dict(self) -> dict:
        return asdict(self) | {
            "can_trade": False,
            "can_promote": False,
        }


@dataclass(frozen=True)
class ExternalSignalPlan:
    event: str
    ticker: str
    symbol: str
    timeframe: str
    side: ExternalSide
    stage: int
    score: float
    confluence: int
    entry: float
    stop: float
    tp1: float | None = None
    tp2: float | None = None
    tp3: float | None = None
    raw: dict = field(default_factory=dict)

    @property
    def final_target(self) -> float | None:
        return self.tp3 or self.tp2 or self.tp1

    @property
    def reward_r(self) -> float | None:
        target = self.final_target
        if target is None:
            return None
        risk = abs(self.entry - self.stop)
        if risk <= 0:
            return None
        return abs(target - self.entry) / risk

    def to_signal_intent(self, *, source_id: str) -> SignalIntent:
        reason = (
            f"external_signal {source_id} {self.side} "
            f"stage={self.stage} score={self.score:.1f} "
            f"confluence={self.confluence} tf={self.timeframe}"
        )
        return SignalIntent(
            self.side,
            stop_price=self.stop,
            take_profit_price=self.final_target,
            reason=reason,
        )

    def exit_plan(self, splits: tuple[float, float, float, float]) -> dict:
        return {
            "mode": "multi_tp_metadata_only",
            "live_wired": False,
            "tp1": {"price": self.tp1, "fraction": splits[0]},
            "tp2": {"price": self.tp2, "fraction": splits[1]},
            "tp3": {"price": self.tp3, "fraction": splits[2]},
            "runner": {
                "fraction": splits[3],
                "after": "tp1",
                "stop": "breakeven_then_trail",
            },
        }

    def to_dict(self) -> dict:
        return asdict(self) | {
            "reward_r": self.reward_r,
            "final_target": self.final_target,
            "can_trade": False,
            "can_promote": False,
        }


@dataclass(frozen=True)
class ExternalSignalIntakeDecision:
    accepted: bool
    policy: ExternalSignalPolicy
    failed_checks: tuple[str, ...] = ()
    plan: ExternalSignalPlan | None = None
    candidate: SignalCandidate | None = None

    @property
    def explanation(self) -> str:
        if self.accepted:
            return "ACCEPTED_FOR_ARBITRATION"
        return "REJECTED: " + "; ".join(self.failed_checks)

    def to_dict(self) -> dict:
        return {
            "accepted": self.accepted,
            "explanation": self.explanation,
            "failed_checks": list(self.failed_checks),
            "policy": self.policy.to_dict(),
            "plan": None if self.plan is None else self.plan.to_dict(),
            "candidate": None if self.candidate is None else {
                "source_id": self.candidate.source_id,
                "strategy_id": self.candidate.strategy_id,
                "symbol": self.candidate.symbol,
                "side": self.candidate.signal.side,
                "route": self.candidate.route,
                "expected_edge_bps": self.candidate.expected_edge_bps,
                "expected_cost_bps": self.candidate.expected_cost_bps,
                "profit_factor": self.candidate.profit_factor,
                "net_edge_bps": self.candidate.net_edge_bps,
                "metadata": self.candidate.metadata,
            },
            "can_trade": False,
            "can_promote": False,
        }


def external_signal_policy() -> dict:
    return {
        "status": "research_or_shadow_intake_only",
        "can_trade": False,
        "can_promote": False,
        "strategy_id": "external_tradingview_signal_v1",
        "principle": (
            "external indicator alerts are parsed as candidates only; they "
            "must pass VNEDGE arbitration, sizing, risk gateway, journal, and "
            "mode gates before any order path"
        ),
        "required_fields": [
            "event",
            "ticker",
            "tf",
            "direction",
            "stage",
            "score",
            "confluence",
            "entry",
            "sl",
            "tp1/tp2/tp3",
        ],
    }


def ingest_external_signal(
    payload: str | bytes | Mapping[str, Any],
    *,
    policy: ExternalSignalPolicy = ExternalSignalPolicy(),
    current_price: float | None = None,
) -> ExternalSignalIntakeDecision:
    """Parse, validate, and convert a webhook alert to a SignalCandidate.

    The default policy deliberately marks the source unverified, which means
    the candidate is emitted with ``route=BLOCKED`` and cannot be selected by
    ``SignalArbiter``. Promotion requires source-level VNEDGE evidence.
    """
    raw = _payload_dict(payload)
    plan = _plan_from_payload(raw)
    failed = _validation_failures(plan, policy, current_price)
    if failed:
        return ExternalSignalIntakeDecision(
            accepted=False,
            policy=policy,
            failed_checks=tuple(failed),
            plan=plan,
        )
    return ExternalSignalIntakeDecision(
        accepted=True,
        policy=policy,
        plan=plan,
        candidate=_candidate_from_plan(plan, policy),
    )


def _candidate_from_plan(
    plan: ExternalSignalPlan,
    policy: ExternalSignalPolicy,
) -> SignalCandidate:
    route = policy.verified_route if policy.source_verified else "BLOCKED"
    expected_edge = (
        policy.verified_expected_edge_bps
        if policy.source_verified and policy.verified_expected_edge_bps is not None
        else 0.0
    )
    profit_factor = (
        policy.verified_profit_factor if policy.source_verified else None
    )
    return SignalCandidate(
        source_id=f"{policy.source_id}:{plan.ticker}:{plan.timeframe}:{plan.event}",
        strategy_id="external_tradingview_signal_v1",
        symbol=plan.symbol,
        signal=plan.to_signal_intent(source_id=policy.source_id),
        expected_edge_bps=expected_edge,
        expected_cost_bps=policy.expected_cost_bps,
        profit_factor=profit_factor,
        confidence=max(0.0, min(plan.score / 100.0, 1.0)),
        route=route,
        metadata={
            "external_signal": plan.to_dict(),
            "exit_plan": plan.exit_plan(policy.tp_splits),
            "source_verified": policy.source_verified,
            "note": (
                "blocked until this source clears VNEDGE replay/paper evidence"
                if not policy.source_verified
                else "source-level evidence supplied; still requires arbiter and gateway"
            ),
        },
    )


def _payload_dict(payload: str | bytes | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(payload, Mapping):
        return dict(payload)
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON payload: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("external signal payload must be a JSON object")
    return parsed


def _plan_from_payload(raw: Mapping[str, Any]) -> ExternalSignalPlan:
    direction = str(raw.get("direction", raw.get("side", ""))).strip().lower()
    side: ExternalSide
    if direction in {"long", "buy"}:
        side = "long"
    elif direction in {"short", "sell"}:
        side = "short"
    else:
        side = "long"  # invalid direction is reported by validation

    ticker = str(raw.get("ticker", raw.get("symbol", ""))).strip()
    symbol = _normalize_symbol(str(raw.get("vnedge_symbol", "")) or ticker)
    return ExternalSignalPlan(
        event=str(raw.get("event", "")).strip(),
        ticker=ticker,
        symbol=symbol,
        timeframe=str(raw.get("tf", raw.get("timeframe", ""))).strip(),
        side=side,
        stage=_int(raw.get("stage")),
        score=_float(raw.get("score")),
        confluence=_confluence(raw.get("confluence")),
        entry=_float(raw.get("entry")),
        stop=_float(raw.get("sl", raw.get("stop", raw.get("stop_loss")))),
        tp1=_optional_float(raw.get("tp1")),
        tp2=_optional_float(raw.get("tp2")),
        tp3=_optional_float(raw.get("tp3")),
        raw=dict(raw),
    )


def _validation_failures(
    plan: ExternalSignalPlan,
    policy: ExternalSignalPolicy,
    current_price: float | None,
) -> list[str]:
    failed: list[str] = []
    if plan.event != "trade_opened":
        failed.append(f"event_not_trade_opened:{plan.event or 'missing'}")
    if not plan.ticker:
        failed.append("ticker_missing")
    if not plan.timeframe:
        failed.append("timeframe_missing")
    raw_direction = str(plan.raw.get("direction", plan.raw.get("side", "")))
    if raw_direction.strip().lower() not in {
        "long", "buy", "short", "sell",
    }:
        failed.append("direction_invalid")
    if plan.stage < policy.min_stage:
        failed.append(f"stage_below_floor:{plan.stage}< {policy.min_stage}")
    if plan.score < policy.min_score:
        failed.append(f"score_below_floor:{plan.score:.1f}< {policy.min_score:.1f}")
    if plan.confluence < policy.min_confluence:
        failed.append(
            f"confluence_below_floor:{plan.confluence}< {policy.min_confluence}"
        )
    prices = (plan.entry, plan.stop, *(v for v in (plan.tp1, plan.tp2, plan.tp3) if v))
    if any(v <= 0 for v in prices):
        failed.append("price_invalid")
    target = plan.final_target
    if target is None:
        failed.append("take_profit_missing")
    elif plan.side == "long" and not (plan.stop < plan.entry < target):
        failed.append("long_price_geometry_invalid")
    elif plan.side == "short" and not (target < plan.entry < plan.stop):
        failed.append("short_price_geometry_invalid")
    if plan.reward_r is None or plan.reward_r < policy.min_reward_r:
        failed.append(f"reward_r_below_floor:{plan.reward_r}")
    if current_price is not None and plan.entry > 0:
        slip = abs(current_price - plan.entry) / plan.entry * 10_000.0
        if slip > policy.max_entry_slippage_bps:
            failed.append(
                f"entry_slippage_too_high:{slip:.2f}bps>"
                f"{policy.max_entry_slippage_bps:.2f}bps"
            )
    return failed


def _normalize_symbol(ticker: str) -> str:
    ticker = ticker.strip()
    if "/" in ticker and ":" in ticker:
        return ticker
    compact = ticker.replace("-", "").replace("_", "").upper()
    for quote in ("USDT", "USDC", "USD"):
        if compact.endswith(quote) and len(compact) > len(quote):
            base = compact[: -len(quote)]
            return f"{base}/{quote}:{quote}"
    return ticker


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _optional_float(value: Any) -> float | None:
    out = _float(value)
    return out if out > 0 else None


def _int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _confluence(value: Any) -> int:
    if isinstance(value, str) and "/" in value:
        value = value.split("/", 1)[0]
    return _int(value)
