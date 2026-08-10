"""Alpha Council: deterministic agent debate over research candidates.

Inspired by agent-native trading platforms, but intentionally scoped to VNEDGE's
promotion ladder. The council never trades and never promotes. It reads existing
research artifacts, lets specialized deterministic agents argue for/against each
candidate, and publishes a ranked next-action queue.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

COUNCIL_ID = "alpha_agent_council_v1"
DEFAULT_RESEARCH_DIR = Path("research/live_research")
DEFAULT_LATEST = DEFAULT_RESEARCH_DIR / "alpha_council_latest.json"
DEFAULT_FEED = DEFAULT_RESEARCH_DIR / "alpha_council_feed.jsonl"

SOURCE_QUOTAS = {
    "rolling_walk_forward": 10,
    "event_leadlag_alpha": 10,
    "daily_scalper_pack": 6,
    "alpha_distillation": 6,
    "artifact_health": 6,
    "bitcoin_regime": 4,
    "fast_l2_scout": 4,
    "orderflow_footprint": 4,
    "l2_research_loop": 4,
    "alpha_factory": 4,
}

ARTIFACT_MAX_AGE_SECONDS = {
    "latest.json": 2 * 3600,
    "event_leadlag_latest.json": 2 * 3600,
    "l2_scout_latest.json": 45 * 60,
    "orderflow_footprint_latest.json": 2 * 3600,
    "l2_latest.json": 7 * 3600,
    "daily_scalper_latest.json": 7 * 3600,
    "alpha_distillation_latest.json": 7 * 3600,
    "bitcoin_regime_latest.json": 2 * 3600,
    "candidate_replay_latest.json": 2 * 3600,
    "execution_condition_latest.json": 2 * 3600,
    "filtered_replay_latest.json": 2 * 3600,
}

ARTIFACT_PRODUCERS = {
    "latest.json": "research-loop",
    "event_leadlag_latest.json": "event-leadlag-miner",
    "l2_scout_latest.json": "l2-fast-scout",
    "orderflow_footprint_latest.json": "orderflow-footprint-miner",
    "l2_latest.json": "l2-research-loop",
    "daily_scalper_latest.json": "daily-scalper-pack",
    "alpha_distillation_latest.json": "alpha-distillation",
    "bitcoin_regime_latest.json": "bitcoin-regime-sensor",
    "candidate_replay_latest.json": "candidate-replay-executor",
    "execution_condition_latest.json": "execution-condition-miner",
    "filtered_replay_latest.json": "filtered-replay-executor",
}


@dataclass(frozen=True)
class AlphaCandidate:
    candidate_id: str
    source: str
    family: str
    exchange: str
    symbol: str
    timeframe: str
    state: str
    route_decision: str
    metrics: dict[str, Any]
    evidence: dict[str, Any]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class AgentOpinion:
    agent_id: str
    stance: str
    score_delta: float
    argument: str
    vetoes: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return asdict(self)


def run_alpha_council(
    research_dir: Path | str = DEFAULT_RESEARCH_DIR,
    *,
    max_candidates: int = 30,
) -> dict:
    research = Path(research_dir)
    candidates = collect_candidates(research, max_candidates=max_candidates)
    debates = [debate_candidate(candidate) for candidate in candidates]
    debates.sort(key=lambda row: (-row["priority_score"], row["candidate"]["candidate_id"]))

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "council_id": COUNCIL_ID,
        "mode": "research_only_agent_debate",
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
        "guardrails": {
            "orders_allowed": False,
            "auto_promotion_allowed": False,
            "requires_gateway_for_any_future_order": True,
            "requires_untouched_judgment": True,
            "requires_replay_for_microstructure": True,
            "principle": (
                "agents may rank hypotheses, but only governed replay, untouched "
                "judgment, shadow, and paper trials can move a lane toward trading"
            ),
        },
        "summary": _summary(debates, candidates),
        "agent_roster": [
            {
                "agent_id": "edge_advocate",
                "role": "argues for statistically useful edge evidence",
            },
            {
                "agent_id": "skeptic",
                "role": "attacks sample size, overfit, instability, and weak gates",
            },
            {
                "agent_id": "execution_specialist",
                "role": "checks fee wall, maker/taker route, and fill assumptions",
            },
            {
                "agent_id": "risk_governor",
                "role": "applies promotion-ladder vetoes and capital-safety invariants",
            },
            {
                "agent_id": "research_director",
                "role": "turns the debate into the next falsifiable experiment",
            },
        ],
        "debates": debates,
    }


def collect_candidates(
    research_dir: Path | str,
    *,
    max_candidates: int = 30,
) -> list[AlphaCandidate]:
    research = Path(research_dir)
    candidates: list[AlphaCandidate] = []
    candidates.extend(_candle_candidates(_read_json(research / "latest.json")))
    candidates.extend(_event_leadlag_candidates(_read_json(research / "event_leadlag_latest.json")))
    candidates.extend(_fast_l2_candidates(_read_json(research / "l2_scout_latest.json")))
    candidates.extend(_orderflow_footprint_candidates(
        _read_json(research / "orderflow_footprint_latest.json")
    ))
    candidates.extend(_l2_loop_candidates(_read_json(research / "l2_latest.json")))
    candidates.extend(_daily_scalper_candidates(_read_json(research / "daily_scalper_latest.json")))
    candidates.extend(_alpha_distillation_candidates(
        _read_json(research / "alpha_distillation_latest.json")
    ))
    candidates.extend(_bitcoin_regime_candidates(
        _read_json(research / "bitcoin_regime_latest.json")
    ))
    candidates.extend(_artifact_health_candidates(research))
    candidates = _dedupe_candidates(candidates)
    candidates = _apply_replay_labels(
        candidates,
        _read_json(research / "candidate_replay_latest.json"),
    )
    candidates = _apply_execution_condition_labels(
        candidates,
        _read_json(research / "execution_condition_latest.json"),
    )
    candidates = _apply_filtered_replay_labels(
        candidates,
        _read_json(research / "filtered_replay_latest.json"),
    )
    return _apply_source_quotas(
        sorted(candidates, key=_candidate_sort_key),
        max_candidates=max_candidates,
    )


def debate_candidate(candidate: AlphaCandidate) -> dict:
    opinions = (
        _edge_advocate(candidate),
        _skeptic(candidate),
        _execution_specialist(candidate),
        _risk_governor(candidate),
        _research_director(candidate),
    )
    vetoes = tuple(sorted({veto for opinion in opinions for veto in opinion.vetoes}))
    priority = _clamp(50.0 + sum(opinion.score_delta for opinion in opinions), 0.0, 100.0)
    next_action = _next_action(candidate, vetoes, priority)
    return {
        "candidate": candidate.to_dict(),
        "priority_score": round(priority, 2),
        "council_verdict": _verdict(priority, vetoes),
        "next_action": next_action,
        "can_trade": False,
        "can_promote": False,
        "vetoes": list(vetoes),
        "debate": [opinion.to_dict() for opinion in opinions],
    }


def _candle_candidates(payload: dict | None) -> list[AlphaCandidate]:
    out: list[AlphaCandidate] = []
    for row in (payload or {}).get("results", []):
        verdict = str(row.get("verdict", "UNKNOWN"))
        net = _num(row.get("oos_net_usd"))
        pf = _num(row.get("profit_factor"))
        trades = _num(row.get("oos_trades"))
        has_positive_family = any(
            _num(f.get("net_usd")) > 0 and _num(f.get("profit_factor")) >= 1.1
            for f in (row.get("family_attribution") or {}).values()
            if isinstance(f, dict)
        )
        if verdict != "PASS" and net <= 0 and pf < 1.15 and not has_positive_family:
            continue
        exchange = str(row.get("exchange", "unknown"))
        symbol = str(row.get("symbol", "unknown"))
        strategy = str(row.get("strategy", "unknown_strategy"))
        timeframe = str(row.get("timeframe", "unknown"))
        out.append(AlphaCandidate(
            candidate_id=f"candle|{exchange}|{symbol}|{timeframe}|{strategy}",
            source="rolling_walk_forward",
            family=strategy,
            exchange=exchange,
            symbol=symbol,
            timeframe=timeframe,
            state=verdict,
            route_decision="CANDLE_RESEARCH",
            metrics={
                "oos_net_usd": net,
                "oos_trades": trades,
                "profit_factor": pf,
                "payoff_ratio": _num(row.get("payoff_ratio")),
                "profitable_windows_pct": _num(row.get("profitable_windows_pct")),
                "total_fees_usd": _num(row.get("total_fees_usd")),
                "repair_action": _candle_repair_action(row),
            },
            evidence=row,
        ))
    return out


def _event_leadlag_candidates(payload: dict | None) -> list[AlphaCandidate]:
    out: list[AlphaCandidate] = []
    for row in (payload or {}).get("hypotheses", []):
        state = str(row.get("state", "UNKNOWN"))
        route = _route_label(row.get("route_decision"))
        maker_net = _num(row.get("maker_avg_net_bps"))
        maker_pf = _num(row.get("maker_profit_factor"))
        if not state.startswith("EDGE_CANDIDATE") and maker_net <= 0 and maker_pf < 1.15:
            continue
        exchange = str(row.get("follower_exchange", "unknown"))
        symbol = str(row.get("follower_symbol") or row.get("base_asset", "unknown"))
        horizon = row.get("horizon_min", "?")
        source_id = str(row.get("hypothesis_id") or f"{exchange}|{symbol}|{horizon}")
        out.append(AlphaCandidate(
            candidate_id=f"event_leadlag|{source_id}",
            source="event_leadlag_alpha",
            family="cross_venue_event_leadlag_v1",
            exchange=exchange,
            symbol=symbol,
            timeframe=f"{horizon}m",
            state=state,
            route_decision=route,
            metrics={
                "samples": _num(row.get("samples")),
                "maker_avg_net_bps": maker_net,
                "taker_avg_net_bps": _num(row.get("taker_avg_net_bps")),
                "maker_profit_factor": maker_pf,
                "taker_profit_factor": _num(row.get("taker_profit_factor")),
                "win_rate_pct": _num(row.get("win_rate_pct")),
            },
            evidence=row,
        ))
    return out


def _fast_l2_candidates(payload: dict | None) -> list[AlphaCandidate]:
    out: list[AlphaCandidate] = []
    for row in (payload or {}).get("top_results", []):
        state = str(row.get("state", "UNKNOWN"))
        route = _route_label(row.get("route_decision"))
        avg_net = _num(row.get("avg_net_bps") or row.get("maker_avg_net_bps"))
        pf = _num(row.get("profit_factor") or row.get("maker_profit_factor"))
        if "EDGE_CANDIDATE" not in state and avg_net <= 0 and pf < 1.15:
            continue
        exchange = str(row.get("exchange", "unknown"))
        symbol = str(row.get("symbol", "unknown"))
        family = str(row.get("family") or row.get("family_id") or row.get("strategy", "l2_scout"))
        out.append(AlphaCandidate(
            candidate_id=f"l2_scout|{exchange}|{symbol}|{family}|{state}",
            source="fast_l2_scout",
            family=family,
            exchange=exchange,
            symbol=symbol,
            timeframe=str(row.get("horizon_ms", "?")) + "ms",
            state=state,
            route_decision=route,
            metrics={
                "samples": _num(row.get("samples")),
                "avg_forward_bps": _num(row.get("avg_forward_bps")),
                "avg_net_bps": avg_net,
                "profit_factor": pf,
            },
            evidence=row,
        ))
    return out


def _orderflow_footprint_candidates(payload: dict | None) -> list[AlphaCandidate]:
    out: list[AlphaCandidate] = []
    rows = (
        (payload or {}).get("candidates")
        or (payload or {}).get("top_candidates")
        or []
    )
    for row in rows:
        if not isinstance(row, dict):
            continue
        state = str(row.get("state", "UNKNOWN"))
        if "ORDERFLOW" not in state and _num(row.get("score")) <= 0:
            continue
        exchange = str(row.get("exchange", "unknown"))
        symbol = str(row.get("symbol", "unknown"))
        day = str(row.get("day", "unknown_day"))
        side = str(row.get("side", "unknown"))
        start = str(row.get("start_ts_ms", "unknown_start"))
        candidate_id = str(
            row.get("candidate_id")
            or f"orderflow_footprint|{exchange}|{symbol}|{day}|{start}|{side}"
        )
        out.append(AlphaCandidate(
            candidate_id=candidate_id,
            source="orderflow_footprint",
            family=str(row.get("family") or "orderflow_footprint_v1"),
            exchange=exchange,
            symbol=symbol,
            timeframe=str(row.get("timeframe", "60s")),
            state=state,
            route_decision=_route_label(row.get("route_decision") or "REPLAY_REQUIRED"),
            metrics={
                "samples": _num(row.get("samples") or row.get("trade_count")),
                "score": _num(row.get("score")),
                "stacked_run_length": _num(row.get("stacked_run_length")),
                "delta_ratio": _num(row.get("delta_ratio")),
                "price_change_bps": _num(row.get("price_change_bps")),
                "cvd_notional_usd": _num(row.get("cvd_notional_usd")),
                "total_notional_usd": _num(row.get("total_notional_usd")),
                "avg_spread_bps": _num(row.get("avg_spread_bps")),
            },
            evidence=row,
        ))
    return out


def _l2_loop_candidates(payload: dict | None) -> list[AlphaCandidate]:
    out: list[AlphaCandidate] = []
    scalper = (payload or {}).get("scalper_research") or {}
    for row in scalper.get("edge_hypotheses", []):
        if not isinstance(row, dict):
            continue
        state = str(row.get("state", "UNKNOWN"))
        route = _route_label(row.get("route_decision"))
        avg_net = _num(row.get("avg_net_bps") or row.get("maker_avg_net_bps"))
        if "EDGE_CANDIDATE" not in state and avg_net <= 0:
            continue
        exchange = str(row.get("exchange", "unknown"))
        symbol = str(row.get("symbol", "unknown"))
        family = str(row.get("family") or row.get("family_id") or "scalper_edge")
        out.append(AlphaCandidate(
            candidate_id=f"l2_loop|{exchange}|{symbol}|{family}|{state}",
            source="l2_research_loop",
            family=family,
            exchange=exchange,
            symbol=symbol,
            timeframe=str(row.get("horizon_ms", "?")) + "ms",
            state=state,
            route_decision=route,
            metrics={
                "samples": _num(row.get("samples")),
                "avg_net_bps": avg_net,
                "profit_factor": _num(row.get("profit_factor") or row.get("maker_profit_factor")),
            },
            evidence=row,
        ))
    alpha = (payload or {}).get("alpha_factory") or {}
    for row in alpha.get("hypotheses", []):
        if not isinstance(row, dict):
            continue
        route = _route_label(row.get("route_decision"))
        state = str(row.get("state", "UNKNOWN"))
        avg_net = _num(row.get("avg_net_bps") or row.get("maker_avg_net_bps"))
        if "EDGE_CANDIDATE" not in state and avg_net <= 0:
            continue
        exchange = str(row.get("exchange", "unknown"))
        symbol = str(row.get("symbol", "unknown"))
        family = str(row.get("family") or row.get("family_id") or "alpha_factory")
        out.append(AlphaCandidate(
            candidate_id=f"alpha_factory|{exchange}|{symbol}|{family}|{state}",
            source="alpha_factory",
            family=family,
            exchange=exchange,
            symbol=symbol,
            timeframe=str(row.get("horizon_ms", "?")) + "ms",
            state=state,
            route_decision=route,
            metrics={
                "samples": _num(row.get("samples")),
                "avg_net_bps": avg_net,
                "profit_factor": _num(row.get("profit_factor") or row.get("maker_profit_factor")),
            },
            evidence=row,
        ))
    return out


def _daily_scalper_candidates(payload: dict | None) -> list[AlphaCandidate]:
    out: list[AlphaCandidate] = []
    for row in (payload or {}).get("candidates", []):
        if not isinstance(row, dict):
            continue
        score = _num(row.get("score"))
        if score <= 0:
            continue
        exchange = str(row.get("exchange", "unknown"))
        symbol = str(row.get("symbol", "unknown"))
        family = str(row.get("family") or row.get("strategy", "daily_scalper_pack"))
        out.append(AlphaCandidate(
            candidate_id=f"daily_scalper|{exchange}|{symbol}|{family}",
            source="daily_scalper_pack",
            family=family,
            exchange=exchange,
            symbol=symbol,
            timeframe=str(row.get("timeframe", "mixed")),
            state=str(row.get("state", "CANDIDATE")),
            route_decision=_route_label(row.get("route_decision")),
            metrics={"score": score},
            evidence=row,
        ))
    return out


def _alpha_distillation_candidates(payload: dict | None) -> list[AlphaCandidate]:
    out: list[AlphaCandidate] = []
    rows = (payload or {}).get("distilled_candidates") or (payload or {}).get("results", [])
    for row in rows:
        if not isinstance(row, dict):
            continue
        score = max(
            _num(row.get("score")),
            _num(row.get("oos_net_usd")) / 10.0,
            _num(row.get("profit_factor")),
        )
        if score <= 0 and str(row.get("verdict", "")) != "PASS":
            continue
        exchange = str(row.get("exchange", "unknown"))
        symbol = str(row.get("symbol", "unknown"))
        family = str(row.get("family") or row.get("strategy", "alpha_distillation"))
        out.append(AlphaCandidate(
            candidate_id=f"alpha_distillation|{exchange}|{symbol}|{family}",
            source="alpha_distillation",
            family=family,
            exchange=exchange,
            symbol=symbol,
            timeframe=str(row.get("timeframe", "mixed")),
            state=str(row.get("state") or row.get("verdict") or "CANDIDATE"),
            route_decision=_route_label(row.get("route_decision")),
            metrics={
                "score": score,
                "oos_net_usd": _num(row.get("oos_net_usd")),
                "oos_trades": _num(row.get("oos_trades")),
                "profit_factor": _num(row.get("profit_factor")),
                "payoff_ratio": _num(row.get("payoff_ratio")),
                "repair_action": _candle_repair_action(row),
            },
            evidence=row,
        ))
    return out


def _bitcoin_regime_candidates(payload: dict | None) -> list[AlphaCandidate]:
    if not payload:
        return []
    summary = payload.get("summary") or {}
    source = payload.get("source") or {}
    mempool = payload.get("mempool") or {}
    features = payload.get("features") or {}
    stress_state = str(
        summary.get("stress_state") or mempool.get("stress_state") or "missing"
    )
    source_status = str(summary.get("source_status") or source.get("status") or "unknown")
    if stress_state == "calm" and source_status == "ok":
        return []
    state = (
        f"BTC_REGIME_{stress_state.upper()}"
        if source_status == "ok" else f"BTC_SOURCE_{source_status.upper()}"
    )
    metrics = {
        "stress_score": _num(summary.get("stress_score")),
        "fee_pressure_score": _num(features.get("fee_pressure_score")),
        "mempool_pressure_score": _num(features.get("mempool_pressure_score")),
        "fastest_fee_sat_vb": _num(features.get("fastest_fee_sat_vb")),
        "mempool_vsize_vb": _num(features.get("mempool_vsize_vb")),
        "mempool_tx_count": _num(features.get("mempool_tx_count")),
        "fee_spike_z": _num(features.get("fee_spike_z")),
        "mempool_pressure_z": _num(features.get("mempool_pressure_z")),
    }
    return [
        AlphaCandidate(
            candidate_id=f"bitcoin_regime|BTC|{stress_state}|{source_status}",
            source="bitcoin_regime",
            family="bitcoin_network_regime_v1",
            exchange="bitcoin_network",
            symbol="BTC",
            timeframe="network",
            state=state,
            route_decision="CONTEXT_ONLY",
            metrics=metrics,
            evidence=payload,
        )
    ]


def _artifact_health_candidates(research: Path) -> list[AlphaCandidate]:
    if not research.exists():
        return []
    now = time.time()
    out: list[AlphaCandidate] = []
    for filename, max_age in ARTIFACT_MAX_AGE_SECONDS.items():
        path = research / filename
        producer = ARTIFACT_PRODUCERS.get(filename, "unknown")
        if not path.exists():
            state = "MISSING_ARTIFACT"
            age = 0.0
            reason = f"{filename} is missing; expected producer={producer}"
        else:
            age = max(0.0, now - path.stat().st_mtime)
            if age <= max_age:
                continue
            state = "STALE_ARTIFACT"
            reason = (
                f"{filename} age {age / 60:.1f}m exceeds "
                f"{max_age / 60:.1f}m freshness budget; expected producer={producer}"
            )
        out.append(AlphaCandidate(
            candidate_id=f"artifact_health|{filename}",
            source="artifact_health",
            family="research_artifact_refresh",
            exchange="all",
            symbol=filename,
            timeframe="scheduled",
            state=state,
            route_decision="RESEARCH_REFRESH",
            metrics={
                "age_seconds": age,
                "max_age_seconds": float(max_age),
                "freshness_ratio": (age / max_age) if max_age else 0.0,
            },
            evidence={
                "artifact": filename,
                "expected_producer": producer,
                "reason": reason,
                "can_trade": False,
                "can_promote": False,
            },
        ))
    return out


def _edge_advocate(candidate: AlphaCandidate) -> AgentOpinion:
    m = candidate.metrics
    delta = 0.0
    notes: list[str] = []
    samples = _sample_count(candidate)
    replay_verdict = str(m.get("replay_verdict") or "")
    if replay_verdict == "REPLAY_CANDIDATE":
        delta += 36
        notes.append("candidate survived conservative execution replay")
    elif replay_verdict == "UNDER_SAMPLED_POSITIVE_REPLAY":
        delta += 10
        notes.append("positive replay exists but needs more fills before trust")
    elif _is_bad_replay_verdict(replay_verdict):
        delta -= 28
        notes.append(f"conservative replay rejected execution edge: {replay_verdict}")
    if candidate.state == "PASS":
        delta += 28
        notes.append("rolling OOS gate currently marks this lane PASS")
    if candidate.state == "REJECT" and _num(m.get("oos_net_usd")) > 0:
        delta += 14
        notes.append("positive-after-fees lane is blocked by repairable gates")
    if "EDGE_CANDIDATE" in candidate.state:
        delta += 30
        notes.append("research miner labels this as an edge candidate")
    if "ORDERFLOW_CANDIDATE" in candidate.state:
        delta += 18
        notes.append("public-flow footprint shows a stacked orderflow anomaly")
    if candidate.source == "artifact_health":
        delta += 18
        notes.append("stale or missing research evidence is blocking the signal funnel")
    if candidate.source == "bitcoin_regime":
        source_status = str(candidate.evidence.get("summary", {}).get("source_status") or "")
        if source_status != "ok":
            delta += 12
            notes.append("Bitcoin telemetry health issue blocks regime-context confidence")
        else:
            delta += min(14.0, _num(m.get("stress_score")) * 2.0)
            notes.append("Bitcoin fee-market context is abnormal enough to split research")
    if samples >= 20 and _best_profit_factor(m) >= 1.5:
        delta += 12
        notes.append(f"profit factor is strong at {_best_profit_factor(m):.2f}")
    elif 0 < samples < 20 and _best_profit_factor(m) >= 1.5:
        delta += 2
        notes.append(
            f"PF={_best_profit_factor(m):.2f} is only a scout pulse with "
            f"{samples:.0f} sample(s)"
        )
    if samples >= 20 and _best_net_bps(m) > 5:
        delta += 10
        notes.append(f"net edge clears a useful bps buffer: {_best_net_bps(m):.2f}")
    elif 0 < samples < 20 and _best_net_bps(m) > 5:
        delta += 2
        notes.append(
            f"net bps pulse is positive but under-sampled: {_best_net_bps(m):.2f}"
        )
    if _num(m.get("oos_net_usd")) > 0:
        delta += 8
        notes.append(f"OOS net is positive: ${_num(m.get('oos_net_usd')):.2f}")
    if not notes:
        delta -= 8
        notes.append("no strong positive edge evidence survived extraction")
    return AgentOpinion("edge_advocate", "support", delta, "; ".join(notes))


def _skeptic(candidate: AlphaCandidate) -> AgentOpinion:
    m = candidate.metrics
    delta = 0.0
    notes: list[str] = []
    vetoes: list[str] = []
    samples = max(_num(m.get("samples")), _num(m.get("oos_trades")))
    replay_verdict = str(m.get("replay_verdict") or "")
    if replay_verdict in {"NO_QUOTE", "NO_FILLS"}:
        delta -= 22
        vetoes.append("no_executable_replay_sample")
        notes.append(f"replay produced no executable sample: {replay_verdict}")
    elif replay_verdict == "NEGATIVE_EDGE_AFTER_REPLAY":
        delta -= 30
        vetoes.append("replay_negative_edge")
        notes.append("replay fill was negative after fees/slippage")
    if candidate.state == "REJECT":
        delta -= 15 if _num(m.get("oos_net_usd")) > 0 else 25
        notes.append("rolling research verdict is REJECT")
    if candidate.source == "artifact_health":
        delta -= 4
        notes.append("artifact health issue requires refresh before debate quality improves")
    if candidate.source == "bitcoin_regime":
        delta -= 4
        notes.append("network stress is context only and does not prove directional edge")
        return AgentOpinion("skeptic", "challenge", delta, "; ".join(notes), tuple(vetoes))
    if "UNDER_SAMPLED" in candidate.state or 0 < samples < 20:
        delta -= 24
        vetoes.append("needs_more_samples")
        notes.append(f"sample count is not robust yet: {samples:.0f}")
    if samples == 0:
        delta -= 18
        vetoes.append("no_trade_sample")
        notes.append("no filled/replayed sample is available")
    if _best_profit_factor(m) and _best_profit_factor(m) < 1.25:
        delta -= 14
        notes.append(f"profit factor is below offensive threshold: {_best_profit_factor(m):.2f}")
    reasons = candidate.evidence.get("reasons") or candidate.evidence.get("why_no_trade") or []
    if reasons:
        delta -= min(12, 2 * len(reasons))
        notes.append("existing gate reasons remain unresolved")
    if not notes:
        delta += 4
        notes.append("no obvious overfit/sample-size objection in current artifact")
    return AgentOpinion("skeptic", "challenge", delta, "; ".join(notes), tuple(vetoes))


def _execution_specialist(candidate: AlphaCandidate) -> AgentOpinion:
    route = candidate.route_decision
    m = candidate.metrics
    delta = 0.0
    notes: list[str] = []
    vetoes: list[str] = []
    replay_verdict = str(m.get("replay_verdict") or "")
    filtered_verdict = str(m.get("filtered_replay_verdict") or "")
    condition_bucket = str(m.get("execution_condition_bucket") or "")
    if filtered_verdict:
        fills = _num(m.get("filtered_replay_fills"))
        quotes = _num(m.get("filtered_replay_quotes"))
        net = _num(m.get("filtered_replay_net_usd"))
        avg = _num(m.get("filtered_replay_avg_net_bps"))
        if filtered_verdict == "REPLAY_CANDIDATE":
            delta += 42
            notes.append(
                f"filtered fresh replay passed: fills={fills:.0f}/{quotes:.0f}, "
                f"net=${net:+.4f}, avg={avg:+.2f}bps"
            )
        elif filtered_verdict == "UNDER_SAMPLED_POSITIVE_REPLAY":
            delta += 10
            vetoes.append("needs_more_replay_samples")
            notes.append(
                f"filtered replay positive but under-sampled: fills={fills:.0f}/{quotes:.0f}"
            )
        elif _is_bad_replay_verdict(filtered_verdict):
            delta -= 36
            vetoes.append("filtered_replay_failed")
            notes.append(f"filtered replay still failed: {filtered_verdict}")
    if replay_verdict and filtered_verdict != "REPLAY_CANDIDATE":
        fills = _num(m.get("replay_fills"))
        quotes = _num(m.get("replay_quotes"))
        net = _num(m.get("replay_net_usd"))
        avg = _num(m.get("replay_avg_net_bps"))
        if replay_verdict == "REPLAY_CANDIDATE":
            delta += 36
            notes.append(
                f"execution replay passed: fills={fills:.0f}/{quotes:.0f}, "
                f"net=${net:+.4f}, avg={avg:+.2f}bps"
            )
        elif replay_verdict == "UNDER_SAMPLED_POSITIVE_REPLAY":
            delta += 8
            vetoes.append("needs_more_replay_samples")
            notes.append(
                f"positive replay is under-sampled: fills={fills:.0f}/{quotes:.0f}, "
                f"net=${net:+.4f}"
            )
        elif replay_verdict == "NO_QUOTE":
            delta -= 42
            vetoes.append("no_quote_after_event")
            notes.append("candidate event could not place a timely passive quote")
        elif replay_verdict == "NO_FILLS":
            delta -= 38
            vetoes.append("maker_fill_failed")
            notes.append("passive quote did not fill under conservative replay")
        elif replay_verdict == "NEGATIVE_EDGE_AFTER_REPLAY":
            delta -= 48
            vetoes.append("replay_negative_edge")
            notes.append(
                f"replay filled but lost after costs: net=${net:+.4f}, "
                f"avg={avg:+.2f}bps"
            )
        elif replay_verdict.startswith("REJECT_"):
            delta -= 34
            vetoes.append("execution_replay_rejected")
            notes.append(f"execution replay rejected candidate: {replay_verdict}")
    if condition_bucket:
        notes.append(
            "execution-condition miner bucket="
            f"{condition_bucket}, action={m.get('execution_condition_action')}"
        )
    if route == "CONTEXT_ONLY":
        delta -= 2
        vetoes.append("context_only_no_execution")
        notes.append("network-regime context is not an executable route")
    elif route == "BLOCKED":
        delta -= 28
        vetoes.append("execution_route_blocked")
        notes.append("route decision is BLOCKED")
    elif route == "TAKER_ALLOWED":
        delta += 16
        notes.append("taker route is allowed by current evidence")
    elif route == "MAKER_ONLY":
        delta += 8
        if replay_verdict == "REPLAY_CANDIDATE":
            notes.append("maker-only route has conservative replay fill proof")
        elif replay_verdict == "UNDER_SAMPLED_POSITIVE_REPLAY":
            vetoes.append("needs_more_replay_samples")
            notes.append("maker-only route has positive but under-sampled fill proof")
        elif not replay_verdict:
            vetoes.append("maker_fill_unproven")
            notes.append("maker-only route needs queue/fill proof")
        else:
            notes.append("maker-only route failed conservative fill proof")
    elif route == "CANDLE_RESEARCH":
        delta -= 4
        vetoes.append("execution_not_modeled_at_tick_level")
        notes.append("candle research does not prove intrabar execution")
    elif route == "RESEARCH_REFRESH":
        notes.append("research refresh task has no execution route")
    net_bps = _best_net_bps(m)
    if net_bps > 8:
        delta += 10
        notes.append(f"net bps appears above scalper fee wall: {net_bps:.2f}")
    elif candidate.source in {
        "fast_l2_scout",
        "orderflow_footprint",
        "l2_research_loop",
        "alpha_factory",
    }:
        delta -= 10
        notes.append(f"net bps buffer is weak for scalping: {net_bps:.2f}")
    return AgentOpinion(
        "execution_specialist",
        "route_check",
        delta,
        "; ".join(notes) or "no route-specific evidence",
        tuple(vetoes),
    )


def _risk_governor(candidate: AlphaCandidate) -> AgentOpinion:
    vetoes = ["research_only_no_auto_trade", "requires_human_promotion"]
    notes = ["council output is advisory only"]
    delta = -10.0
    replay_verdict = str(candidate.metrics.get("replay_verdict") or "")
    filtered_verdict = str(candidate.metrics.get("filtered_replay_verdict") or "")
    if candidate.source in {
        "event_leadlag_alpha",
        "fast_l2_scout",
        "orderflow_footprint",
        "l2_research_loop",
        "alpha_factory",
    }:
        if filtered_verdict == "REPLAY_CANDIDATE":
            vetoes.append("requires_shadow_trial_after_replay")
            notes.append("filtered replay passed; governed shadow/paper trial still required")
        elif filtered_verdict == "UNDER_SAMPLED_POSITIVE_REPLAY":
            vetoes.append("requires_more_replay_evidence")
            notes.append("filtered replay is positive but under-sampled")
        elif _is_bad_replay_verdict(filtered_verdict):
            vetoes.append("execution_replay_failed")
            notes.append("filtered replay failed conservative execution proof")
        elif not replay_verdict:
            vetoes.append("requires_conservative_l2_replay")
            notes.append("microstructure candidate requires conservative replay")
        elif replay_verdict == "REPLAY_CANDIDATE":
            vetoes.append("requires_shadow_trial_after_replay")
            notes.append("positive replay still needs governed shadow/paper trial")
        elif replay_verdict == "UNDER_SAMPLED_POSITIVE_REPLAY":
            vetoes.append("requires_more_replay_evidence")
            notes.append("positive replay is under-sampled")
        else:
            vetoes.append("execution_replay_failed")
            notes.append("candidate failed conservative execution replay")
    if candidate.source in {"rolling_walk_forward", "daily_scalper_pack", "alpha_distillation"}:
        vetoes.append("requires_untouched_judgment")
        notes.append("rolling PASS still needs pre-registered untouched judgment")
    if candidate.source == "artifact_health":
        vetoes.append("requires_fresh_research_artifact")
        notes.append("fresh artifact must be published before this lane can be judged")
    if candidate.source == "bitcoin_regime":
        vetoes.extend(("context_only_no_trade", "requires_replay_context_split"))
        notes.append("Bitcoin network regime may tag replay windows but cannot trade/promote")
    return AgentOpinion("risk_governor", "veto", delta, "; ".join(notes), tuple(vetoes))


def _research_director(candidate: AlphaCandidate) -> AgentOpinion:
    action = _next_action(candidate, (), 50.0)
    delta = 6.0 if action not in {"IGNORE", "RESEARCH_MORE"} else -2.0
    return AgentOpinion(
        "research_director",
        "next_experiment",
        delta,
        f"recommended next proof step: {action}",
    )


def _next_action(candidate: AlphaCandidate, vetoes: Iterable[str], priority: float) -> str:
    veto_set = set(vetoes)
    if candidate.source == "artifact_health":
        return "REFRESH_STALE_ARTIFACT"
    if candidate.source == "bitcoin_regime":
        source_status = str(
            candidate.evidence.get("summary", {}).get("source_status")
            or candidate.evidence.get("source", {}).get("status")
            or ""
        )
        if source_status != "ok":
            return "REFRESH_BITCOIN_NODE_HEALTH"
        return "SPLIT_REPLAY_BY_BTC_REGIME"
    if candidate.source in {
        "event_leadlag_alpha",
        "fast_l2_scout",
        "orderflow_footprint",
        "l2_research_loop",
        "alpha_factory",
    }:
        filtered_verdict = str(candidate.metrics.get("filtered_replay_verdict") or "")
        if filtered_verdict == "REPLAY_CANDIDATE":
            return "QUEUE_SHADOW_TRIAL_AFTER_REPLAY"
        if filtered_verdict == "UNDER_SAMPLED_POSITIVE_REPLAY":
            return "RECORD_MORE_TICKS"
        if _is_bad_replay_verdict(filtered_verdict):
            return "MINE_PRE_EVENT_EXECUTION_CONDITIONS"
        condition_action = str(candidate.metrics.get("execution_condition_action") or "")
        if veto_set & {
            "no_quote_after_event",
            "maker_fill_failed",
            "replay_negative_edge",
            "execution_replay_rejected",
            "execution_replay_failed",
            "no_executable_replay_sample",
        }:
            if condition_action == "RUN_FILTERED_REPLAY_FROM_EXECUTION_CONDITIONS":
                return "RUN_FILTERED_REPLAY_FROM_EXECUTION_CONDITIONS"
            if condition_action == "RECORD_MORE_TICKS":
                return "RECORD_MORE_TICKS"
            return "MINE_PRE_EVENT_EXECUTION_CONDITIONS"
        if "needs_more_replay_samples" in veto_set or "requires_more_replay_evidence" in veto_set:
            return "RECORD_MORE_TICKS"
        if candidate.metrics.get("replay_verdict") == "REPLAY_CANDIDATE":
            return "QUEUE_SHADOW_TRIAL_AFTER_REPLAY"
        if "needs_more_samples" in veto_set:
            return "RECORD_MORE_TICKS"
        if "execution_route_blocked" in veto_set and priority < 55:
            return "RESEARCH_MORE"
        if "maker_fill_unproven" in veto_set or "requires_conservative_l2_replay" in veto_set:
            return "RUN_CONSERVATIVE_L2_REPLAY"
        return "QUEUE_SHADOW_TRIAL_AFTER_REPLAY"
    if "execution_route_blocked" in veto_set and priority < 55:
        return "RESEARCH_MORE"
    if candidate.state == "PASS":
        return "PRE_REGISTER_UNTOUCHED_JUDGMENT"
    if _num(candidate.metrics.get("oos_net_usd")) > 0:
        return str(candidate.metrics.get("repair_action") or "DIAGNOSE_CLOSE_REJECT")
    return "RESEARCH_MORE"


def _verdict(priority: float, vetoes: Iterable[str]) -> str:
    veto_set = set(vetoes)
    if "execution_replay_failed" in veto_set or "replay_negative_edge" in veto_set:
        return "EXECUTION_REPLAY_FAILED"
    if "execution_route_blocked" in veto_set and priority < 55:
        return "BLOCKED"
    if priority >= 75:
        return "HIGH_PRIORITY_RESEARCH"
    if priority >= 55:
        return "WATCHLIST"
    return "LOW_PRIORITY"


def publish_alpha_council(payload: dict, out: Path, feed: Path | None = None) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    tmp.replace(out)
    if feed is not None:
        feed.parent.mkdir(parents=True, exist_ok=True)
        with open(feed, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, default=str) + "\n")


def _summary(debates: list[dict], candidates: list[AlphaCandidate]) -> dict:
    actions: dict[str, int] = {}
    verdicts: dict[str, int] = {}
    sources: dict[str, int] = {}
    for row in debates:
        actions[row["next_action"]] = actions.get(row["next_action"], 0) + 1
        verdicts[row["council_verdict"]] = verdicts.get(row["council_verdict"], 0) + 1
        source = row["candidate"]["source"]
        sources[source] = sources.get(source, 0) + 1
    return {
        "candidates": len(candidates),
        "debated": len(debates),
        "high_priority": verdicts.get("HIGH_PRIORITY_RESEARCH", 0),
        "watchlist": verdicts.get("WATCHLIST", 0),
        "blocked": verdicts.get("BLOCKED", 0),
        "actions": actions,
        "sources": sources,
        "top_candidate": debates[0]["candidate"]["candidate_id"] if debates else None,
    }


def _dedupe_candidates(candidates: list[AlphaCandidate]) -> list[AlphaCandidate]:
    seen: set[str] = set()
    unique: list[AlphaCandidate] = []
    for candidate in sorted(candidates, key=_candidate_sort_key):
        if candidate.candidate_id in seen:
            continue
        seen.add(candidate.candidate_id)
        unique.append(candidate)
    return unique


def _apply_replay_labels(
    candidates: list[AlphaCandidate],
    payload: dict | None,
) -> list[AlphaCandidate]:
    replay = _candidate_replay_index(payload)
    if not replay:
        return candidates
    out: list[AlphaCandidate] = []
    for candidate in candidates:
        row = replay.get(candidate.candidate_id)
        if row is None and candidate.source == "event_leadlag_alpha":
            raw_id = str(candidate.evidence.get("hypothesis_id") or "")
            row = replay.get(raw_id)
        if row is None:
            out.append(candidate)
            continue
        metrics = dict(candidate.metrics)
        metrics.update({
            "replay_verdict": str(row.get("verdict") or "UNKNOWN"),
            "replay_quotes": _num(row.get("quotes")),
            "replay_fills": _num(row.get("fills")),
            "replay_fill_rate_pct": _num(row.get("fill_rate_pct")),
            "replay_net_usd": _num(row.get("net_usd")),
            "replay_avg_net_bps": _num(row.get("avg_net_bps")),
            "replay_profit_factor": _num(row.get("profit_factor")),
            "replay_avg_adverse_bps": _num(row.get("avg_adverse_bps")),
        })
        evidence = dict(candidate.evidence)
        evidence["execution_replay"] = row
        out.append(replace(candidate, metrics=metrics, evidence=evidence))
    return out


def _candidate_replay_index(payload: dict | None) -> dict[str, dict[str, Any]]:
    rows = (payload or {}).get("rows") or []
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        candidate_id = str(row.get("candidate_id") or "")
        if not candidate_id:
            continue
        out[candidate_id] = row
        source = str(row.get("source") or "")
        if source == "event_leadlag_alpha":
            out[f"event_leadlag|{candidate_id}"] = row
    return out


def _apply_execution_condition_labels(
    candidates: list[AlphaCandidate],
    payload: dict | None,
) -> list[AlphaCandidate]:
    conditions = _execution_condition_index(payload)
    if not conditions:
        return candidates
    out: list[AlphaCandidate] = []
    for candidate in candidates:
        row = conditions.get(candidate.candidate_id)
        if row is None and candidate.source == "event_leadlag_alpha":
            raw_id = str(candidate.evidence.get("hypothesis_id") or "")
            row = conditions.get(raw_id)
        if row is None:
            out.append(candidate)
            continue
        proposal = row.get("filter_proposal") if isinstance(row.get("filter_proposal"), dict) else {}
        metrics = dict(candidate.metrics)
        metrics.update({
            "execution_condition_bucket": str(row.get("primary_bucket") or "UNKNOWN"),
            "execution_condition_action": str(row.get("recommended_action") or "UNKNOWN"),
            "execution_condition_confidence": _num(row.get("confidence")),
            "execution_condition_rows": _num(row.get("rows")),
            "execution_condition_filter": str(proposal.get("filter") or ""),
        })
        evidence = dict(candidate.evidence)
        evidence["execution_condition"] = row
        out.append(replace(candidate, metrics=metrics, evidence=evidence))
    return out


def _execution_condition_index(payload: dict | None) -> dict[str, dict[str, Any]]:
    rows = (payload or {}).get("candidate_conditions") or []
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        candidate_id = str(row.get("candidate_id") or "")
        if not candidate_id:
            continue
        out[candidate_id] = row
        source = str(row.get("source") or "")
        if source == "event_leadlag_alpha":
            out[f"event_leadlag|{candidate_id}"] = row
    return out


def _apply_filtered_replay_labels(
    candidates: list[AlphaCandidate],
    payload: dict | None,
) -> list[AlphaCandidate]:
    filtered = _filtered_replay_index(payload)
    if not filtered:
        return candidates
    out: list[AlphaCandidate] = []
    for candidate in candidates:
        row = filtered.get(candidate.candidate_id)
        if row is None and candidate.source == "event_leadlag_alpha":
            raw_id = str(candidate.evidence.get("hypothesis_id") or "")
            row = filtered.get(raw_id)
        if row is None:
            out.append(candidate)
            continue
        metrics = dict(candidate.metrics)
        metrics.update({
            "filtered_replay_verdict": str(row.get("verdict") or "UNKNOWN"),
            "filtered_replay_quotes": _num(row.get("quotes")),
            "filtered_replay_fills": _num(row.get("fills")),
            "filtered_replay_fill_rate_pct": _num(row.get("fill_rate_pct")),
            "filtered_replay_net_usd": _num(row.get("net_usd")),
            "filtered_replay_avg_net_bps": _num(row.get("avg_net_bps")),
            "filtered_replay_profit_factor": _num(row.get("profit_factor")),
            "filtered_replay_filter": str(row.get("filter_name") or ""),
            "filtered_replay_condition_bucket": str(row.get("condition_bucket") or ""),
        })
        evidence = dict(candidate.evidence)
        evidence["filtered_replay"] = row
        out.append(replace(candidate, metrics=metrics, evidence=evidence))
    return out


def _filtered_replay_index(payload: dict | None) -> dict[str, dict[str, Any]]:
    rows = (payload or {}).get("rows") or []
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        candidate_id = str(row.get("candidate_id") or "")
        if not candidate_id:
            continue
        out[candidate_id] = row
        source = str(row.get("source") or "")
        if source == "event_leadlag_alpha":
            out[f"event_leadlag|{candidate_id}"] = row
    return out


def _candidate_sort_key(candidate: AlphaCandidate) -> tuple[float, str]:
    m = candidate.metrics
    samples = _sample_count(candidate)
    replay_score = 0.0
    filtered_verdict = str(m.get("filtered_replay_verdict") or "")
    replay_verdict = str(m.get("replay_verdict") or "")
    if filtered_verdict == "REPLAY_CANDIDATE":
        replay_score = 190.0
    elif filtered_verdict == "UNDER_SAMPLED_POSITIVE_REPLAY":
        replay_score = 55.0
    elif _is_bad_replay_verdict(filtered_verdict):
        replay_score = -200.0
    elif replay_verdict == "REPLAY_CANDIDATE":
        replay_score = 160.0
    elif replay_verdict == "UNDER_SAMPLED_POSITIVE_REPLAY":
        replay_score = 45.0
    elif _is_bad_replay_verdict(replay_verdict):
        replay_score = -180.0
    state_score = 0.0
    if "EDGE_CANDIDATE_TAKER" in candidate.state:
        state_score = 120.0
    elif "EDGE_CANDIDATE_MAKER" in candidate.state:
        state_score = 110.0
    elif "ORDERFLOW_CANDIDATE" in candidate.state:
        state_score = 92.0
    elif candidate.state == "PASS":
        state_score = 100.0
    elif candidate.source == "artifact_health":
        state_score = 85.0 if candidate.state == "MISSING_ARTIFACT" else 70.0
    elif candidate.source == "bitcoin_regime":
        state_score = 78.0 + min(_num(m.get("stress_score")), 20.0)
    elif "UNDER_SAMPLED" in candidate.state:
        state_score = 20.0
    elif candidate.state == "REJECT" and _num(m.get("oos_net_usd")) > 0:
        state_score = 80.0
    elif candidate.state == "REJECT":
        state_score = -20.0
    score = (
        replay_score
        + state_score
        + min(_best_profit_factor(m), 5.0)
        + _best_net_bps(m) / 10
        + _num(m.get("oos_net_usd")) / 100
        + min(samples, 100.0) / 20
    )
    return (-score, candidate.candidate_id)


def _apply_source_quotas(
    candidates: list[AlphaCandidate],
    *,
    max_candidates: int,
) -> list[AlphaCandidate]:
    counts: dict[str, int] = {}
    selected: list[AlphaCandidate] = []
    for candidate in candidates:
        quota = SOURCE_QUOTAS.get(candidate.source, max_candidates)
        if counts.get(candidate.source, 0) >= quota:
            continue
        selected.append(candidate)
        counts[candidate.source] = counts.get(candidate.source, 0) + 1
        if len(selected) >= max_candidates:
            break
    return selected


def _candle_repair_action(row: dict[str, Any]) -> str:
    if _num(row.get("oos_net_usd")) <= 0 and str(row.get("verdict")) != "PASS":
        return "RESEARCH_MORE"
    reasons = " | ".join(str(reason).lower() for reason in row.get("reasons", []))
    if any(term in reasons for term in ("payoff", "profit factor", "pf below", "avg win")):
        return "REPAIR_EXIT_PAYOFF"
    if any(
        term in reasons
        for term in ("zero-trade", "zero trade", "windows with", "traded windows")
    ):
        return "CHECK_ZERO_WINDOW_STABILITY"
    if any(term in reasons for term in ("is/oos", "collapse", "concentration")):
        return "PRE_REGISTER_NEAR_PASS_JUDGMENT"
    return "DIAGNOSE_CLOSE_REJECT"


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def _route_label(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(
            value.get("route")
            or value.get("decision")
            or value.get("route_decision")
            or "UNKNOWN"
        )
    if value is None:
        return "UNKNOWN"
    return str(value)


def _num(value: Any) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    return out if math.isfinite(out) else 0.0


def _best_profit_factor(metrics: dict[str, Any]) -> float:
    return max(
        _num(metrics.get("filtered_replay_profit_factor")),
        _num(metrics.get("replay_profit_factor")),
        _num(metrics.get("profit_factor")),
        _num(metrics.get("maker_profit_factor")),
        _num(metrics.get("taker_profit_factor")),
    )


def _best_net_bps(metrics: dict[str, Any]) -> float:
    return max(
        _num(metrics.get("filtered_replay_avg_net_bps")),
        _num(metrics.get("replay_avg_net_bps")),
        _num(metrics.get("avg_net_bps")),
        _num(metrics.get("maker_avg_net_bps")),
        _num(metrics.get("taker_avg_net_bps")),
    )


def _sample_count(candidate: AlphaCandidate) -> float:
    return max(
        _num(candidate.metrics.get("filtered_replay_fills")),
        _num(candidate.metrics.get("replay_fills")),
        _num(candidate.metrics.get("samples")),
        _num(candidate.metrics.get("oos_trades")),
        _num(candidate.metrics.get("trades")),
    )


def _is_bad_replay_verdict(verdict: str) -> bool:
    return verdict in {
        "NO_QUOTE",
        "NO_FILLS",
        "NEGATIVE_EDGE_AFTER_REPLAY",
        "REJECT_LOW_FILL_RATE",
        "REJECT_BELOW_NET_BPS",
        "REJECT_BELOW_PF",
        "REJECT_ADVERSE_SELECTION",
    }


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the VNEDGE Alpha Council")
    parser.add_argument("--research-dir", default=str(DEFAULT_RESEARCH_DIR))
    parser.add_argument("--out", default=str(DEFAULT_LATEST))
    parser.add_argument("--feed", default=str(DEFAULT_FEED))
    parser.add_argument("--max-candidates", type=int, default=30)
    parser.add_argument("--interval-seconds", type=int, default=900)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    while True:
        payload = run_alpha_council(
            args.research_dir,
            max_candidates=args.max_candidates,
        )
        publish_alpha_council(payload, Path(args.out), Path(args.feed) if args.feed else None)
        if args.json:
            print(json.dumps(payload, indent=2, default=str))
        else:
            summary = payload["summary"]
            print(
                f"{payload['generated_at']} {COUNCIL_ID}: "
                f"{summary['debated']} debated, top={summary['top_candidate']}"
            )
        if args.once:
            return 0
        time.sleep(max(args.interval_seconds, 1))


if __name__ == "__main__":
    raise SystemExit(main())
