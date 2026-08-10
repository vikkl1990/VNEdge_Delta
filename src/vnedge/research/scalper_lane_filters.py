"""Freqtrade-style chainable filters for scalper lane admission.

The filters answer a narrower question than replay profitability: is this
exchange/symbol lane healthy enough to spend scout/mining/replay budget on?
They are research-only and never grant trade permission.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Protocol

from vnedge.scalping.microstructure import TopOfBook, TradeTick


FILTER_ID = "scalper_lane_filters_v1"


@dataclass(frozen=True)
class LaneFilterConfig:
    enabled_filters: tuple[str, ...] = (
        "recorder_coverage",
        "volume",
        "spread",
        "depth",
        "precision",
        "volatility",
        "replay_state",
        "shadow_performance",
    )
    min_recorder_span_seconds: float = 1.0
    min_book_events: int = 20
    min_trade_events: int = 20
    min_trade_count: int = 20
    min_trade_notional_usd: float = 1_000.0
    max_spread_p95_bps: float = 3.0
    min_top_depth_usd_p50: float = 500.0
    min_top_depth_usd_p10: float = 100.0
    max_price_step_bps: float = 5.0
    require_precision_observed: bool = False
    min_realized_volatility_bps: float = 1.0
    max_realized_volatility_bps: float = 1_000.0
    require_replay_state: bool = False
    blocked_replay_states: tuple[str, ...] = (
        "REJECTED_COST_WALL",
        "REJECTED_LIQUIDITY",
        "REJECTED_NO_QUOTES",
        "REJECTED_NO_FILLS",
        "BELOW_BREAKEVEN",
    )
    require_shadow_performance: bool = False
    min_shadow_trades: int = 5
    min_shadow_profit_factor: float = 1.05
    min_shadow_net_usd: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LaneFilterEvidence:
    exchange: str
    symbol: str
    day: str
    timeframe: str = "tick"
    missing_stream: bool = False
    span_seconds: float = 0.0
    book_events: int = 0
    trade_events: int = 0
    trade_count: int = 0
    trade_notional_usd: float = 0.0
    spread_bps_p50: float | None = None
    spread_bps_p95: float | None = None
    top_depth_usd_p50: float | None = None
    top_depth_usd_p10: float | None = None
    price_step_bps_p50: float | None = None
    realized_volatility_bps: float | None = None
    replay_state: str | None = None
    replay_route: str | None = None
    shadow_virtual_trades: int | None = None
    shadow_profit_factor: float | None = None
    shadow_net_usd: float | None = None

    @classmethod
    def from_events(
        cls,
        events: Iterable[tuple[int, str, object]],
        *,
        exchange: str,
        symbol: str,
        day: str,
        timeframe: str = "tick",
        stats: dict[str, Any] | None = None,
        replay_state: str | None = None,
        replay_route: str | None = None,
        shadow_perf: dict[str, Any] | None = None,
    ) -> "LaneFilterEvidence":
        stats = stats or {}
        events = tuple(events)
        books = [obj for _, kind, obj in events if kind == "book" and isinstance(obj, TopOfBook)]
        trades = [obj for _, kind, obj in events if kind == "trade" and isinstance(obj, TradeTick)]
        timestamps = [ts for ts, _, _ in events]
        mids = [book.mid_price for book in books if _finite(book.mid_price)]
        prices = [*mids, *(trade.price for trade in trades if _finite(trade.price))]
        spreads = [book.spread_bps for book in books if _finite(book.spread_bps)]
        depths = [book.top_depth_usd for book in books if _finite(book.top_depth_usd)]
        trade_notional = sum(trade.price * trade.quantity for trade in trades)
        span = _num(stats.get("span_seconds"))
        if span <= 0 and len(timestamps) >= 2:
            span = (max(timestamps) - min(timestamps)) / 1000.0

        return cls(
            exchange=exchange,
            symbol=symbol,
            day=day,
            timeframe=timeframe,
            missing_stream=bool(stats.get("missing_stream", False)),
            span_seconds=span,
            book_events=int(_num(stats.get("book_rows") or stats.get("book_events")) or len(books)),
            trade_events=int(
                _num(stats.get("trade_rows") or stats.get("trade_events")) or len(trades)
            ),
            trade_count=len(trades),
            trade_notional_usd=round(trade_notional, 6),
            spread_bps_p50=_first_num_or_none(
                stats.get("spread_bps_p50"), _quantile(spreads, 0.50)
            ),
            spread_bps_p95=_first_num_or_none(
                stats.get("spread_bps_p95"), _quantile(spreads, 0.95)
            ),
            top_depth_usd_p50=_quantile(depths, 0.50),
            top_depth_usd_p10=_quantile(depths, 0.10),
            price_step_bps_p50=_price_step_bps(prices),
            realized_volatility_bps=_realized_range_bps(mids),
            replay_state=replay_state,
            replay_route=replay_route,
            shadow_virtual_trades=_int_or_none((shadow_perf or {}).get("virtual_trades")),
            shadow_profit_factor=_num_or_none((shadow_perf or {}).get("profit_factor")),
            shadow_net_usd=_num_or_none((shadow_perf or {}).get("net_usd")),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LaneFilterCheck:
    name: str
    passed: bool
    severity: str
    reason: str
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LaneFilterDecision:
    filter_id: str
    exchange: str
    symbol: str
    day: str
    state: str
    passed: bool
    primary_blocker: str | None
    score: float
    checks: tuple[LaneFilterCheck, ...]
    can_trade: bool = False
    can_promote: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["checks"] = [check.to_dict() for check in self.checks]
        return payload


class LaneFilter(Protocol):
    name: str

    def evaluate(
        self,
        evidence: LaneFilterEvidence,
        config: LaneFilterConfig,
    ) -> LaneFilterCheck:
        ...


class RecorderCoverageFilter:
    name = "recorder_coverage"

    def evaluate(self, evidence: LaneFilterEvidence, config: LaneFilterConfig) -> LaneFilterCheck:
        ok = (
            not evidence.missing_stream
            and evidence.span_seconds >= config.min_recorder_span_seconds
            and evidence.book_events >= config.min_book_events
            and evidence.trade_events >= config.min_trade_events
        )
        return LaneFilterCheck(
            self.name,
            ok,
            "pass" if ok else "block",
            (
                "recorder coverage ok"
                if ok else "missing/short tick stream or insufficient book/trade events"
            ),
            {
                "span_seconds": evidence.span_seconds,
                "min_span_seconds": config.min_recorder_span_seconds,
                "book_events": evidence.book_events,
                "min_book_events": config.min_book_events,
                "trade_events": evidence.trade_events,
                "min_trade_events": config.min_trade_events,
                "missing_stream": evidence.missing_stream,
            },
        )


class VolumeFilter:
    name = "volume"

    def evaluate(self, evidence: LaneFilterEvidence, config: LaneFilterConfig) -> LaneFilterCheck:
        ok = (
            evidence.trade_count >= config.min_trade_count
            and evidence.trade_notional_usd >= config.min_trade_notional_usd
        )
        return LaneFilterCheck(
            self.name,
            ok,
            "pass" if ok else "block",
            "trade activity ok" if ok else "insufficient public trade count/notional",
            {
                "trade_count": evidence.trade_count,
                "min_trade_count": config.min_trade_count,
                "trade_notional_usd": evidence.trade_notional_usd,
                "min_trade_notional_usd": config.min_trade_notional_usd,
            },
        )


class SpreadFilter:
    name = "spread"

    def evaluate(self, evidence: LaneFilterEvidence, config: LaneFilterConfig) -> LaneFilterCheck:
        spread = evidence.spread_bps_p95
        ok = spread is not None and spread <= config.max_spread_p95_bps
        return LaneFilterCheck(
            self.name,
            ok,
            "pass" if ok else "block",
            "spread is inside scalper budget" if ok else "spread p95 exceeds scalper budget",
            {"spread_bps_p95": spread, "max_spread_p95_bps": config.max_spread_p95_bps},
        )


class DepthFilter:
    name = "depth"

    def evaluate(self, evidence: LaneFilterEvidence, config: LaneFilterConfig) -> LaneFilterCheck:
        p50 = evidence.top_depth_usd_p50
        p10 = evidence.top_depth_usd_p10
        ok = (
            p50 is not None
            and p10 is not None
            and p50 >= config.min_top_depth_usd_p50
            and p10 >= config.min_top_depth_usd_p10
        )
        return LaneFilterCheck(
            self.name,
            ok,
            "pass" if ok else "block",
            "visible top-book depth ok" if ok else "visible top-book depth too thin",
            {
                "top_depth_usd_p50": p50,
                "min_top_depth_usd_p50": config.min_top_depth_usd_p50,
                "top_depth_usd_p10": p10,
                "min_top_depth_usd_p10": config.min_top_depth_usd_p10,
            },
        )


class PrecisionFilter:
    name = "precision"

    def evaluate(self, evidence: LaneFilterEvidence, config: LaneFilterConfig) -> LaneFilterCheck:
        step = evidence.price_step_bps_p50
        if step is None:
            ok = not config.require_precision_observed
            return LaneFilterCheck(
                self.name,
                ok,
                "warn" if ok else "block",
                (
                    "price precision not observed; allowed for discovery"
                    if ok else "price precision not observed"
                ),
                {
                    "price_step_bps_p50": None,
                    "max_price_step_bps": config.max_price_step_bps,
                },
            )
        ok = step <= config.max_price_step_bps
        return LaneFilterCheck(
            self.name,
            ok,
            "pass" if ok else "block",
            "observed price step is fine enough" if ok else "observed price step is too coarse",
            {"price_step_bps_p50": step, "max_price_step_bps": config.max_price_step_bps},
        )


class VolatilityFilter:
    name = "volatility"

    def evaluate(self, evidence: LaneFilterEvidence, config: LaneFilterConfig) -> LaneFilterCheck:
        vol = evidence.realized_volatility_bps
        ok = (
            vol is not None
            and config.min_realized_volatility_bps <= vol <= config.max_realized_volatility_bps
        )
        return LaneFilterCheck(
            self.name,
            ok,
            "pass" if ok else "block",
            (
                "recent realized range is tradable"
                if ok else "recent realized range is too quiet or disorderly"
            ),
            {
                "realized_volatility_bps": vol,
                "min_realized_volatility_bps": config.min_realized_volatility_bps,
                "max_realized_volatility_bps": config.max_realized_volatility_bps,
            },
        )


class ReplayStateFilter:
    name = "replay_state"

    def evaluate(self, evidence: LaneFilterEvidence, config: LaneFilterConfig) -> LaneFilterCheck:
        state = evidence.replay_state
        if not state:
            ok = not config.require_replay_state
            return LaneFilterCheck(
                self.name,
                ok,
                "warn" if ok else "block",
                "no replay state yet; allowed for discovery" if ok else "replay state required",
                {"replay_state": state, "replay_route": evidence.replay_route},
            )
        ok = state not in set(config.blocked_replay_states)
        return LaneFilterCheck(
            self.name,
            ok,
            "pass" if ok else "block",
            "replay state does not block lane" if ok else f"blocked replay state: {state}",
            {"replay_state": state, "replay_route": evidence.replay_route},
        )


class ShadowPerformanceFilter:
    name = "shadow_performance"

    def evaluate(self, evidence: LaneFilterEvidence, config: LaneFilterConfig) -> LaneFilterCheck:
        trades = evidence.shadow_virtual_trades
        pf = evidence.shadow_profit_factor
        net = evidence.shadow_net_usd
        if trades is None or trades < config.min_shadow_trades:
            ok = not config.require_shadow_performance
            return LaneFilterCheck(
                self.name,
                ok,
                "warn" if ok else "block",
                (
                    "shadow sample missing/too small; allowed for discovery"
                    if ok else "shadow performance sample required"
                ),
                {
                    "shadow_virtual_trades": trades,
                    "min_shadow_trades": config.min_shadow_trades,
                    "shadow_profit_factor": pf,
                    "shadow_net_usd": net,
                },
            )
        ok = (
            net is not None
            and net >= config.min_shadow_net_usd
            and (pf is None or pf >= config.min_shadow_profit_factor)
        )
        return LaneFilterCheck(
            self.name,
            ok,
            "pass" if ok else "block",
            "shadow outcomes do not block lane" if ok else "shadow outcomes are negative",
            {
                "shadow_virtual_trades": trades,
                "min_shadow_trades": config.min_shadow_trades,
                "shadow_profit_factor": pf,
                "min_shadow_profit_factor": config.min_shadow_profit_factor,
                "shadow_net_usd": net,
                "min_shadow_net_usd": config.min_shadow_net_usd,
            },
        )


DEFAULT_LANE_FILTER_CHAIN: tuple[LaneFilter, ...] = (
    RecorderCoverageFilter(),
    VolumeFilter(),
    SpreadFilter(),
    DepthFilter(),
    PrecisionFilter(),
    VolatilityFilter(),
    ReplayStateFilter(),
    ShadowPerformanceFilter(),
)


def evaluate_lane_filters(
    evidence: LaneFilterEvidence,
    config: LaneFilterConfig = LaneFilterConfig(),
    chain: tuple[LaneFilter, ...] = DEFAULT_LANE_FILTER_CHAIN,
) -> LaneFilterDecision:
    enabled = set(config.enabled_filters)
    checks = tuple(
        lane_filter.evaluate(evidence, config)
        for lane_filter in chain
        if lane_filter.name in enabled
    )
    failed = tuple(check for check in checks if not check.passed)
    warnings = tuple(check for check in checks if check.severity == "warn")
    passed_count = sum(1 for check in checks if check.passed)
    score = round(passed_count / len(checks) * 100.0, 1) if checks else 100.0
    if failed:
        state = "FILTER_BLOCK"
        primary = failed[0].name
    elif warnings:
        state = "FILTER_WARN"
        primary = None
    else:
        state = "FILTER_PASS"
        primary = None
    return LaneFilterDecision(
        filter_id=FILTER_ID,
        exchange=evidence.exchange,
        symbol=evidence.symbol,
        day=evidence.day,
        state=state,
        passed=not failed,
        primary_blocker=primary,
        score=score,
        checks=checks,
    )


def summarize_filter_decisions(decisions: Iterable[LaneFilterDecision]) -> dict[str, Any]:
    decisions = tuple(decisions)
    states: dict[str, int] = {}
    blockers: dict[str, int] = {}
    for decision in decisions:
        states[decision.state] = states.get(decision.state, 0) + 1
        if decision.primary_blocker:
            blockers[decision.primary_blocker] = blockers.get(decision.primary_blocker, 0) + 1
    return {
        "filter_id": FILTER_ID,
        "lanes": len(decisions),
        "passed": sum(1 for decision in decisions if decision.passed),
        "blocked": sum(1 for decision in decisions if not decision.passed),
        "states": states,
        "primary_blockers": blockers,
        "can_trade": False,
        "can_promote": False,
    }


def lane_filter_policy(config: LaneFilterConfig = LaneFilterConfig()) -> dict[str, Any]:
    return {
        "filter_id": FILTER_ID,
        "status": "research_only",
        "can_trade": False,
        "can_promote": False,
        "principle": (
            "lane filters admit or reject research/mining budget only; a pass "
            "is not a signal and cannot bypass replay, shadow, paper, or risk"
        ),
        "config": config.to_dict(),
    }


def _quantile(values: list[float], q: float) -> float | None:
    clean = sorted(v for v in values if _finite(v))
    if not clean:
        return None
    if len(clean) == 1:
        return clean[0]
    pos = (len(clean) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return clean[lo]
    return clean[lo] + (clean[hi] - clean[lo]) * (pos - lo)


def _price_step_bps(prices: list[float]) -> float | None:
    clean = sorted({round(v, 12) for v in prices if _finite(v) and v > 0})
    if len(clean) < 2:
        return None
    diffs = [b - a for a, b in zip(clean, clean[1:]) if b > a]
    step = _quantile(diffs, 0.50)
    mid = _quantile(clean, 0.50)
    if step is None or mid is None or mid <= 0:
        return None
    return step / mid * 10_000.0


def _realized_range_bps(mids: list[float]) -> float | None:
    clean = [v for v in mids if _finite(v) and v > 0]
    if len(clean) < 2:
        return None
    lo = min(clean)
    hi = max(clean)
    mid = _quantile(clean, 0.50)
    if mid is None or mid <= 0:
        return None
    return (hi - lo) / mid * 10_000.0


def _first_num_or_none(*values: Any) -> float | None:
    for value in values:
        out = _num_or_none(value)
        if out is not None:
            return out
    return None


def _num(value: Any) -> float:
    out = _num_or_none(value)
    return out if out is not None else 0.0


def _num_or_none(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if _finite(out) else None


def _int_or_none(value: Any) -> int | None:
    out = _num_or_none(value)
    return int(out) if out is not None else None


def _finite(value: float) -> bool:
    return math.isfinite(value)
