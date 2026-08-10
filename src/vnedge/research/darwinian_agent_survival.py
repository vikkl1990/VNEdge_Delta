"""Atlas-inspired Darwinian agent survival layer.

Atlas-GIC's useful idea for VNEDGE is not "let agents trade"; it is
"measure which agents/scanner families are helping, make those voices louder,
and mute the ones that keep losing."  This module turns the existing
read-only evidence streams into a bounded influence scorecard:

* paper lane survival
* paper lane governor
* realtime scanner telemetry
* Alpha Arena Lite research scorecards

The artifact is explicitly read-only.  It cannot start lanes, stop lanes,
promote candidates, or submit orders.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
import math
from pathlib import Path
from typing import Any


DEFAULT_RESEARCH_DIR = Path("research/live_research")
DEFAULT_SURVIVAL = DEFAULT_RESEARCH_DIR / "lane_survival_latest.json"
DEFAULT_GOVERNOR = DEFAULT_RESEARCH_DIR / "paper_lane_governor_latest.json"
DEFAULT_SCANNER = DEFAULT_RESEARCH_DIR / "realtime_scanner_latest.json"
DEFAULT_ALPHA_ARENA = DEFAULT_RESEARCH_DIR / "alpha_arena_lite_latest.json"
DEFAULT_OUT = DEFAULT_RESEARCH_DIR / "darwinian_agent_survival_latest.json"
DEFAULT_FEED = DEFAULT_RESEARCH_DIR / "darwinian_agent_survival_feed.jsonl"

REPORT_ID = "darwinian_agent_survival_v1"

STATE_LEADING = "LEADING_AGENT"
STATE_SURVIVING = "SURVIVING_AGENT"
STATE_OBSERVE = "OBSERVE_MORE"
STATE_MUTED = "MUTED_AGENT"
STATE_EXTINCTION = "EXTINCTION_REVIEW"
STATE_NO_EVIDENCE = "NO_EVIDENCE"

COHORT_EVENT = "event_funding"
COHORT_SCALPER = "scalper_5m"
COHORT_INTRADAY = "intraday_15m"
COHORT_SWING_1H = "swing_1h"
COHORT_SWING_4H = "swing_4h"
COHORT_RESEARCH = "research_only"


@dataclass(frozen=True)
class DarwinianSurvivalConfig:
    min_weight: float = 0.30
    max_weight: float = 2.50
    top_quartile_multiplier: float = 1.05
    bottom_quartile_multiplier: float = 0.95
    min_closed_trades: int = 20
    min_profit_factor: float = 1.50
    min_avg_net_bps: float = 25.0
    max_agents: int = 80
    janus_min_weight: float = 0.05
    janus_max_weight: float = 0.55

    def __post_init__(self) -> None:
        if self.min_weight <= 0:
            raise ValueError("min_weight must be positive")
        if self.max_weight < self.min_weight:
            raise ValueError("max_weight must be >= min_weight")
        if not 0 < self.bottom_quartile_multiplier <= 1:
            raise ValueError("bottom_quartile_multiplier must be in (0, 1]")
        if self.top_quartile_multiplier < 1:
            raise ValueError("top_quartile_multiplier must be >= 1")
        if self.min_closed_trades < 1:
            raise ValueError("min_closed_trades must be positive")
        if self.min_profit_factor < 1:
            raise ValueError("min_profit_factor must be >= 1")
        if self.min_avg_net_bps <= 0:
            raise ValueError("min_avg_net_bps must be positive")
        if self.max_agents < 1:
            raise ValueError("max_agents must be positive")
        if self.janus_min_weight <= 0 or self.janus_max_weight <= 0:
            raise ValueError("janus weights must be positive")
        if self.janus_max_weight < self.janus_min_weight:
            raise ValueError("janus_max_weight must be >= janus_min_weight")


def build_darwinian_agent_survival(
    *,
    survival: Mapping[str, Any] | None = None,
    governor: Mapping[str, Any] | None = None,
    scanner: Mapping[str, Any] | None = None,
    alpha_arena: Mapping[str, Any] | None = None,
    previous: Mapping[str, Any] | None = None,
    survival_path: Path | str = DEFAULT_SURVIVAL,
    governor_path: Path | str = DEFAULT_GOVERNOR,
    scanner_path: Path | str = DEFAULT_SCANNER,
    alpha_arena_path: Path | str = DEFAULT_ALPHA_ARENA,
    previous_path: Path | str | None = DEFAULT_OUT,
    config: DarwinianSurvivalConfig = DarwinianSurvivalConfig(),
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the read-only Darwinian agent/cohort report."""

    generated = now or datetime.now(UTC)
    survival_path = Path(survival_path)
    governor_path = Path(governor_path)
    scanner_path = Path(scanner_path)
    alpha_arena_path = Path(alpha_arena_path)
    previous_path = Path(previous_path) if previous_path is not None else None

    survival_payload = _payload_or_file(survival, survival_path)
    governor_payload = _payload_or_file(governor, governor_path)
    scanner_payload = _payload_or_file(scanner, scanner_path)
    alpha_payload = _payload_or_file(alpha_arena, alpha_arena_path)
    previous_payload = _payload_or_file(previous, previous_path) if previous_path else (
        dict(previous) if isinstance(previous, Mapping) else {}
    )

    agents = _collect_agent_evidence(
        survival_payload=survival_payload,
        governor_payload=governor_payload,
        scanner_payload=scanner_payload,
        alpha_payload=alpha_payload,
        config=config,
    )
    previous_weights = _previous_weights(previous_payload)
    rows = _agent_rows(agents, previous_weights=previous_weights, config=config)
    rows.sort(key=_agent_sort_key)
    rows = rows[: max(1, int(config.max_agents))]
    cohorts = _cohort_rows(rows, config=config)
    summary = _summary(rows, cohorts)

    return {
        "generated_at": generated.isoformat(),
        "report_id": REPORT_ID,
        "mode": "atlas_inspired_read_only_agent_survival",
        "source": "chrisworsey55/atlas-gic adaptation: Darwinian weights + JANUS cohort weighting",
        "source_reports": {
            "survival": survival_payload.get("report_id"),
            "governor": governor_payload.get("report_id"),
            "scanner": scanner_payload.get("scanner_id") or scanner_payload.get("report_id"),
            "alpha_arena": alpha_payload.get("report_id"),
        },
        "source_generated_at": {
            "survival": survival_payload.get("generated_at"),
            "governor": governor_payload.get("generated_at"),
            "scanner": scanner_payload.get("generated_at"),
            "alpha_arena": alpha_payload.get("generated_at"),
        },
        "inputs": {
            "survival_path": str(survival_path),
            "governor_path": str(governor_path),
            "scanner_path": str(scanner_path),
            "alpha_arena_path": str(alpha_arena_path),
            "previous_path": str(previous_path) if previous_path else None,
        },
        "config": asdict(config),
        "summary": summary,
        "cohorts": cohorts,
        "agents": rows,
        "operator_answer": _operator_answer(summary, rows, cohorts),
        "policy": {
            "read_only": True,
            "can_trade": False,
            "can_promote": False,
            "weights_are_advisory": True,
            "no_prompt_or_strategy_auto_mutation": True,
            "promotion_still_requires_governor_and_untouched_judgment": True,
        },
        "can_trade": False,
        "can_promote": False,
    }


def publish_darwinian_agent_survival(
    payload: Mapping[str, Any], out: Path | str, feed: Path | str | None = None
) -> None:
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    tmp.replace(out_path)
    out_path.chmod(0o644)
    if feed is not None:
        feed_path = Path(feed)
        feed_path.parent.mkdir(parents=True, exist_ok=True)
        with feed_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_feed_record(payload), default=str, sort_keys=True) + "\n")
        feed_path.chmod(0o644)


def render_report(payload: Mapping[str, Any], *, limit: int = 30) -> str:
    summary = payload.get("summary") if isinstance(payload.get("summary"), Mapping) else {}
    lines = [
        "=== Darwinian agent survival ===",
        f"generated: {payload.get('generated_at')}",
        str(payload.get("operator_answer") or ""),
        (
            "summary: "
            f"{summary.get('agent_count', 0)} agents, "
            f"{summary.get('upweighted_agents', 0)} upweighted, "
            f"{summary.get('downweighted_agents', 0)} downweighted, "
            f"{summary.get('extinction_review', 0)} extinction-review"
        ),
    ]
    for row in list(payload.get("agents") or [])[:limit]:
        lines.append(
            f"  {row.get('survival_state', ''):<18} "
            f"{row.get('agent_id', ''):<34} "
            f"w {row.get('darwinian_weight', 0):>4.2f} "
            f"score {row.get('agent_score', 0):>6.1f} "
            f"{row.get('cohort', ''):<14} "
            f"{row.get('next_action', '')}"
        )
    lines.append("read-only: can_trade=false can_promote=false")
    return "\n".join(lines)


def _collect_agent_evidence(
    *,
    survival_payload: Mapping[str, Any],
    governor_payload: Mapping[str, Any],
    scanner_payload: Mapping[str, Any],
    alpha_payload: Mapping[str, Any],
    config: DarwinianSurvivalConfig,
) -> dict[str, dict[str, Any]]:
    agents: dict[str, dict[str, Any]] = {}
    for row in _rows(survival_payload):
        agent = _agent_for(row)
        bucket = _agent_bucket(agents, agent)
        bucket["source_counts"]["survival"] += 1
        bucket["lanes"].add(str(row.get("lane_id") or ""))
        _merge_identity(bucket, row)
        _add_metrics(bucket, row)
        _count(bucket, "survival_states", row.get("survival_state"))
        _count(bucket, "decisions", row.get("decision"))
        _set_cohort(bucket, row)

    for row in _rows(governor_payload):
        agent = _agent_for(row)
        bucket = _agent_bucket(agents, agent)
        bucket["source_counts"]["governor"] += 1
        bucket["lanes"].add(str(row.get("lane_id") or ""))
        _merge_identity(bucket, row)
        _add_metrics(bucket, row)
        _count(bucket, "governor_buckets", row.get("governor_bucket"))
        _count(bucket, "actions", row.get("action"))
        _set_cohort(bucket, row)

    for row in _rows(scanner_payload):
        agent = _agent_for(row)
        bucket = _agent_bucket(agents, agent)
        bucket["source_counts"]["scanner"] += 1
        bucket["lanes"].add(str(row.get("lane_id") or ""))
        _merge_identity(bucket, row)
        _count(bucket, "scanner_states", row.get("state"))
        _count(bucket, "uplift_actions", _nested(row, "uplift", "action"))
        funnel = row.get("funnel") if isinstance(row.get("funnel"), Mapping) else {}
        bucket["live_signals"] += _int(funnel.get("live_signals"))
        bucket["paper_order_intents"] += _int(funnel.get("paper_order_intents"))
        _set_cohort(bucket, row)

    for card in list(alpha_payload.get("scorecards") or []):
        if not isinstance(card, Mapping):
            continue
        agent = _agent_for(card)
        bucket = _agent_bucket(agents, agent)
        bucket["source_counts"]["alpha_arena"] += 1
        _merge_identity(bucket, card)
        _count(bucket, "arena_verdicts", card.get("arena_verdict"))
        metrics = card.get("metrics") if isinstance(card.get("metrics"), Mapping) else {}
        bps = _maybe_float(metrics.get("top_avg_net_bps"))
        if bps is not None:
            samples = max(1, _int(metrics.get("max_samples")))
            bucket["bps_weighted_sum"] += bps * samples
            bucket["bps_weighted_n"] += samples
        pf = _maybe_float(metrics.get("best_profit_factor"))
        if pf is not None and pf < 999:
            bucket["pf_values"].append(pf)
        samples = _int(metrics.get("max_samples"))
        bucket["research_samples"] += samples
        _set_cohort(bucket, card)

    return {
        agent: bucket
        for agent, bucket in agents.items()
        if agent and agent != "unknown" and _has_evidence(bucket, config=config)
    }


def _agent_bucket(agents: dict[str, dict[str, Any]], agent_id: str) -> dict[str, Any]:
    if agent_id not in agents:
        agents[agent_id] = {
            "agent_id": agent_id,
            "source_counts": Counter(),
            "lanes": set(),
            "cohort_votes": Counter(),
            "exchanges": set(),
            "symbols": set(),
            "timeframes": set(),
            "survival_states": Counter(),
            "governor_buckets": Counter(),
            "scanner_states": Counter(),
            "arena_verdicts": Counter(),
            "decisions": Counter(),
            "actions": Counter(),
            "uplift_actions": Counter(),
            "closed_trades": 0,
            "closed_net_pnl_usd": 0.0,
            "fees_usd": 0.0,
            "bps_weighted_sum": 0.0,
            "bps_weighted_n": 0,
            "pf_values": [],
            "live_signals": 0,
            "paper_order_intents": 0,
            "research_samples": 0,
        }
    return agents[agent_id]


def _agent_rows(
    agents: Mapping[str, dict[str, Any]],
    *,
    previous_weights: Mapping[str, float],
    config: DarwinianSurvivalConfig,
) -> list[dict[str, Any]]:
    raw: list[dict[str, Any]] = []
    for agent, bucket in agents.items():
        row = _score_agent(agent, bucket, config=config)
        row["previous_weight"] = round(
            _bounded(previous_weights.get(agent, 1.0), config.min_weight, config.max_weight),
            6,
        )
        raw.append(row)

    ranked = sorted(raw, key=lambda row: float(row["agent_score"]), reverse=True)
    evidence_rows = [row for row in ranked if row["survival_state"] != STATE_NO_EVIDENCE]
    quartile = max(1, math.ceil(len(evidence_rows) / 4)) if evidence_rows else 0
    top_pool = [
        row
        for row in ranked
        if row["survival_state"] in {STATE_LEADING, STATE_SURVIVING}
        and float(row["agent_score"]) > 0
    ]
    bottom_pool = sorted(
        [
            row
            for row in ranked
            if row["survival_state"] in {STATE_MUTED, STATE_EXTINCTION}
            or float(row["agent_score"]) < 0
        ],
        key=lambda row: float(row["agent_score"]),
    )
    top_ids = {str(row["agent_id"]) for row in top_pool[:quartile]}
    bottom_ids = {str(row["agent_id"]) for row in bottom_pool[:quartile]} - top_ids

    for row in raw:
        prev = float(row["previous_weight"])
        agent = str(row["agent_id"])
        if agent in top_ids:
            new = prev * config.top_quartile_multiplier
            influence = "UPWEIGHT"
        elif agent in bottom_ids:
            new = prev * config.bottom_quartile_multiplier
            influence = "DOWNWEIGHT"
        else:
            new = prev
            influence = "NEUTRAL"
        weight = _bounded(new, config.min_weight, config.max_weight)
        row["darwinian_weight"] = round(weight, 6)
        row["weight_delta"] = round(weight - prev, 6)
        row["influence_state"] = influence
    return raw


def _score_agent(
    agent_id: str, bucket: Mapping[str, Any], *, config: DarwinianSurvivalConfig
) -> dict[str, Any]:
    closed = _int(bucket.get("closed_trades"))
    avg_bps = _weighted_avg(bucket.get("bps_weighted_sum"), bucket.get("bps_weighted_n"))
    pf = _avg(bucket.get("pf_values") or [])
    survival_states = Counter(bucket.get("survival_states") or {})
    governor_buckets = Counter(bucket.get("governor_buckets") or {})
    scanner_states = Counter(bucket.get("scanner_states") or {})
    arena_verdicts = Counter(bucket.get("arena_verdicts") or {})
    sources = Counter(bucket.get("source_counts") or {})

    components = {
        "sample": min(18.0, closed / config.min_closed_trades * 18.0),
        "net_edge": _clamp(((avg_bps or 0.0) / config.min_avg_net_bps) * 28.0, -28.0, 28.0),
        "profit_factor": _clamp(((pf or 1.0) - 1.0) * 22.0, -22.0, 22.0),
        "paper_state": _paper_state_score(survival_states, governor_buckets),
        "research_state": _research_state_score(arena_verdicts),
        "live_state": _live_state_score(scanner_states),
    }
    score = round(sum(components.values()), 4)
    state = _survival_state(
        score=score,
        closed=closed,
        avg_bps=avg_bps,
        pf=pf,
        survival_states=survival_states,
        governor_buckets=governor_buckets,
        sources=sources,
        config=config,
    )
    cohort = _dominant(Counter(bucket.get("cohort_votes") or {})) or COHORT_RESEARCH
    next_action = _next_action(
        state=state,
        avg_bps=avg_bps,
        pf=pf,
        closed=closed,
        survival_states=survival_states,
        governor_buckets=governor_buckets,
        arena_verdicts=arena_verdicts,
    )
    return {
        "agent_id": agent_id,
        "cohort": cohort,
        "survival_state": state,
        "agent_score": score,
        "score_components": {k: round(v, 4) for k, v in components.items()},
        "closed_trades": closed,
        "closed_net_pnl_usd": round(_float(bucket.get("closed_net_pnl_usd")), 6),
        "fees_usd": round(_float(bucket.get("fees_usd")), 6),
        "avg_net_bps": round(avg_bps, 4) if avg_bps is not None else None,
        "profit_factor": round(pf, 4) if pf is not None else None,
        "live_signals": _int(bucket.get("live_signals")),
        "paper_order_intents": _int(bucket.get("paper_order_intents")),
        "research_samples": _int(bucket.get("research_samples")),
        "lane_count": len([x for x in bucket.get("lanes", set()) if x]),
        "exchanges": sorted(x for x in bucket.get("exchanges", set()) if x),
        "symbols": sorted(x for x in bucket.get("symbols", set()) if x),
        "timeframes": sorted(x for x in bucket.get("timeframes", set()) if x),
        "source_counts": dict(sources),
        "survival_states": dict(survival_states),
        "governor_buckets": dict(governor_buckets),
        "scanner_states": dict(scanner_states),
        "arena_verdicts": dict(arena_verdicts),
        "top_action": _dominant(Counter(bucket.get("actions") or {})),
        "top_uplift": _dominant(Counter(bucket.get("uplift_actions") or {})),
        "next_action": next_action,
        "can_trade": False,
        "can_promote": False,
    }


def _cohort_rows(
    rows: list[dict[str, Any]], *, config: DarwinianSurvivalConfig
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("cohort") or COHORT_RESEARCH)].append(row)
    if not grouped:
        return []
    scores = {
        cohort: _avg(float(row.get("agent_score") or 0.0) for row in members) or 0.0
        for cohort, members in grouped.items()
    }
    weights = _bounded_softmax(
        scores,
        min_weight=config.janus_min_weight,
        max_weight=config.janus_max_weight,
    )
    out = []
    for cohort, members in grouped.items():
        closed = sum(_int(row.get("closed_trades")) for row in members)
        net = sum(_float(row.get("closed_net_pnl_usd")) for row in members)
        weighted_agent_weight = sum(float(row.get("darwinian_weight") or 0.0) for row in members)
        avg_bps = _avg(
            float(row["avg_net_bps"])
            for row in members
            if row.get("avg_net_bps") is not None
        )
        leading = sum(1 for row in members if row.get("survival_state") == STATE_LEADING)
        muted = sum(
            1 for row in members
            if row.get("survival_state") in {STATE_MUTED, STATE_EXTINCTION}
        )
        out.append(
            {
                "cohort": cohort,
                "janus_weight": round(weights.get(cohort, 0.0), 6),
                "avg_agent_score": round(scores.get(cohort, 0.0), 4),
                "agents": len(members),
                "leading_agents": leading,
                "muted_agents": muted,
                "closed_trades": closed,
                "closed_net_pnl_usd": round(net, 6),
                "avg_net_bps": round(avg_bps, 4) if avg_bps is not None else None,
                "agent_weight_sum": round(weighted_agent_weight, 6),
                "signal": _cohort_signal(cohort, scores.get(cohort, 0.0), members),
                "can_trade": False,
                "can_promote": False,
            }
        )
    return sorted(out, key=lambda row: float(row["janus_weight"]), reverse=True)


def _summary(rows: list[dict[str, Any]], cohorts: list[dict[str, Any]]) -> dict[str, Any]:
    states = Counter(str(row.get("survival_state") or "") for row in rows)
    influence = Counter(str(row.get("influence_state") or "") for row in rows)
    top_agent = rows[0] if rows else None
    top_cohort = cohorts[0] if cohorts else None
    regime = _janus_regime(cohorts)
    return {
        "agent_count": len(rows),
        "cohort_count": len(cohorts),
        "leading_agents": states.get(STATE_LEADING, 0),
        "surviving_agents": states.get(STATE_SURVIVING, 0),
        "observe_more": states.get(STATE_OBSERVE, 0),
        "muted_agents": states.get(STATE_MUTED, 0),
        "extinction_review": states.get(STATE_EXTINCTION, 0),
        "no_evidence": states.get(STATE_NO_EVIDENCE, 0),
        "upweighted_agents": influence.get("UPWEIGHT", 0),
        "downweighted_agents": influence.get("DOWNWEIGHT", 0),
        "neutral_agents": influence.get("NEUTRAL", 0),
        "state_counts": dict(states),
        "influence_counts": dict(influence),
        "top_agent": None if top_agent is None else {
            "agent_id": top_agent.get("agent_id"),
            "weight": top_agent.get("darwinian_weight"),
            "score": top_agent.get("agent_score"),
            "state": top_agent.get("survival_state"),
        },
        "top_cohort": None if top_cohort is None else {
            "cohort": top_cohort.get("cohort"),
            "janus_weight": top_cohort.get("janus_weight"),
            "avg_agent_score": top_cohort.get("avg_agent_score"),
        },
        "janus_regime": regime,
        "research_only": True,
        "can_trade": False,
        "can_promote": False,
    }


def _operator_answer(
    summary: Mapping[str, Any],
    rows: list[dict[str, Any]],
    cohorts: list[dict[str, Any]],
) -> str:
    if not rows:
        return (
            "No agent evidence is available yet. Publish lane survival, governor, "
            "scanner, or Alpha Arena artifacts before assigning influence."
        )
    top = rows[0]
    cohort = cohorts[0] if cohorts else {}
    extinct = int(summary.get("extinction_review") or 0)
    muted = int(summary.get("muted_agents") or 0)
    return (
        f"Top agent is {top['agent_id']} at weight {top['darwinian_weight']:.2f}; "
        f"JANUS favors {cohort.get('cohort', 'unknown')} at "
        f"{float(cohort.get('janus_weight') or 0.0):.2f}. "
        f"{muted + extinct} weak agent(s) should stay muted/research-only until "
        "a replay or paper survival improvement proves otherwise."
    )


def _survival_state(
    *,
    score: float,
    closed: int,
    avg_bps: float | None,
    pf: float | None,
    survival_states: Counter[str],
    governor_buckets: Counter[str],
    sources: Counter[str],
    config: DarwinianSurvivalConfig,
) -> str:
    if not sources:
        return STATE_NO_EVIDENCE
    if survival_states.get("LEDGER_REPAIR_REQUIRED") or governor_buckets.get("REPAIR_QUEUE"):
        return STATE_OBSERVE
    if survival_states.get("DEMOTE_TO_SHADOW") or governor_buckets.get("DEMOTION_QUEUE"):
        return STATE_EXTINCTION
    if closed >= config.min_closed_trades:
        if (
            avg_bps is not None
            and avg_bps >= config.min_avg_net_bps
            and (pf or 0.0) >= config.min_profit_factor
        ):
            return STATE_LEADING
        if avg_bps is not None and avg_bps < 0:
            return STATE_MUTED
    if score >= 35:
        return STATE_SURVIVING
    if score < -10:
        return STATE_MUTED
    return STATE_OBSERVE


def _next_action(
    *,
    state: str,
    avg_bps: float | None,
    pf: float | None,
    closed: int,
    survival_states: Counter[str],
    governor_buckets: Counter[str],
    arena_verdicts: Counter[str],
) -> str:
    if state == STATE_LEADING:
        return "KEEP_IN_SURVIVOR_TOURNAMENT_AND_QUEUE_HUMAN_REVIEW"
    if state == STATE_EXTINCTION:
        return "QUARANTINE_AGENT_AND_REQUIRE_NEW_REPLAY_PROOF"
    if state == STATE_MUTED:
        return "MUTE_IN_CIO_BLEND_UNTIL_NET_EDGE_REPAIRED"
    if survival_states.get("LEDGER_REPAIR_REQUIRED"):
        return "REPAIR_LEDGER_BEFORE_AGENT_SCORING"
    if governor_buckets.get("REPAIR_QUEUE"):
        return "REPAIR_ROUTE_OR_CADENCE_BEFORE_AGENT_SCORING"
    if arena_verdicts.get("PRE_REGISTER_UNTOUCHED_JUDGMENT"):
        return "PRE_REGISTER_UNTOUCHED_JUDGMENT"
    if arena_verdicts.get("EXPAND_UNTOUCHED_SAMPLE"):
        return "EXPAND_SAMPLE_ON_UNTOUCHED_WINDOW"
    if closed < 20:
        return "COLLECT_MORE_PAPER_OUTCOMES"
    if avg_bps is not None and avg_bps < 25:
        return "MINE_EXIT_CAPTURE_AND_FEE_ROUTE_FILTERS"
    if pf is not None and pf < 1.5:
        return "REFACTOR_ENTRY_QUALITY_OR_EXIT_LOSS_CUT"
    return "OBSERVE_WITH_CURRENT_WEIGHT"


def _paper_state_score(
    survival_states: Counter[str], governor_buckets: Counter[str]
) -> float:
    score = 0.0
    score += 24.0 * survival_states.get("PAPER_SURVIVOR_CANDIDATE", 0)
    score += 16.0 * governor_buckets.get("SURVIVOR_TOURNAMENT", 0)
    score += 8.0 * governor_buckets.get("PAPER_ROSTER", 0)
    score += 4.0 * survival_states.get("PAPER_OBSERVE_MORE", 0)
    score -= 12.0 * governor_buckets.get("PROBATION_QUEUE", 0)
    score -= 20.0 * survival_states.get("DEMOTE_TO_SHADOW", 0)
    score -= 18.0 * governor_buckets.get("DEMOTION_QUEUE", 0)
    score -= 10.0 * survival_states.get("LEDGER_REPAIR_REQUIRED", 0)
    return _clamp(score, -30.0, 30.0)


def _research_state_score(arena_verdicts: Counter[str]) -> float:
    score = 0.0
    score += 12.0 * arena_verdicts.get("PRE_REGISTER_UNTOUCHED_JUDGMENT", 0)
    score += 6.0 * arena_verdicts.get("EXPAND_UNTOUCHED_SAMPLE", 0)
    score += 4.0 * arena_verdicts.get("REPLAY_WITH_EXECUTION_REPAIR", 0)
    score -= 5.0 * arena_verdicts.get("REJECT_RESEARCH_ONLY", 0)
    return _clamp(score, -12.0, 18.0)


def _live_state_score(scanner_states: Counter[str]) -> float:
    score = 0.0
    score += 6.0 * scanner_states.get("FIRING", 0)
    score += 3.0 * scanner_states.get("NEAR_TRIGGER", 0)
    score -= 2.0 * scanner_states.get("STALE", 0)
    return _clamp(score, -8.0, 10.0)


def _cohort_signal(cohort: str, score: float, members: list[dict[str, Any]]) -> str:
    if any(row.get("survival_state") == STATE_LEADING for row in members):
        return "EVIDENCE_LEADER"
    if score >= 25:
        return "REGIME_SUPPORTIVE"
    if any(row.get("survival_state") == STATE_EXTINCTION for row in members):
        return "REGIME_HOSTILE_OR_AGENT_BROKEN"
    if cohort == COHORT_RESEARCH:
        return "RESEARCH_ONLY"
    return "MIXED_OR_UNPROVEN"


def _janus_regime(cohorts: list[dict[str, Any]]) -> str:
    if not cohorts:
        return "NO_COHORT_EVIDENCE"
    if len(cohorts) == 1:
        return f"{cohorts[0]['cohort'].upper()}_ONLY"
    top = cohorts[0]
    second = cohorts[1]
    top_w = float(top.get("janus_weight") or 0.0)
    second_w = float(second.get("janus_weight") or 0.0)
    short_w = sum(
        float(row.get("janus_weight") or 0.0)
        for row in cohorts
        if row.get("cohort") in {COHORT_SCALPER, COHORT_INTRADAY}
    )
    swing_w = sum(
        float(row.get("janus_weight") or 0.0)
        for row in cohorts
        if row.get("cohort") in {COHORT_SWING_1H, COHORT_SWING_4H}
    )
    if top_w - second_w >= 0.12:
        return f"{str(top.get('cohort')).upper()}_DOMINANT"
    if short_w - swing_w >= 0.12:
        return "SHORT_WINDOW_COHORTS_WORKING"
    if swing_w - short_w >= 0.12:
        return "SWING_COHORTS_WORKING"
    return "MIXED_COHORT_REGIME"


def _bounded_softmax(
    scores: Mapping[str, float], *, min_weight: float, max_weight: float
) -> dict[str, float]:
    if not scores:
        return {}
    if len(scores) == 1:
        only = next(iter(scores))
        return {only: 1.0}
    max_score = max(scores.values())
    exp_scores = {key: math.exp((value - max_score) / 25.0) for key, value in scores.items()}
    total = sum(exp_scores.values()) or 1.0
    weights = {key: value / total for key, value in exp_scores.items()}
    return _normalize_bounded(weights, min_weight=min_weight, max_weight=max_weight)


def _normalize_bounded(
    weights: Mapping[str, float], *, min_weight: float, max_weight: float
) -> dict[str, float]:
    out = {key: _bounded(value, min_weight, max_weight) for key, value in weights.items()}
    for _ in range(4):
        total = sum(out.values()) or 1.0
        out = {key: _bounded(value / total, min_weight, max_weight) for key, value in out.items()}
    total = sum(out.values()) or 1.0
    return {key: value / total for key, value in out.items()}


def _previous_weights(payload: Mapping[str, Any]) -> dict[str, float]:
    rows = payload.get("agents") if isinstance(payload.get("agents"), list) else []
    out: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        agent = str(row.get("agent_id") or "")
        if not agent:
            continue
        weight = _maybe_float(row.get("darwinian_weight"))
        if weight is not None:
            out[agent] = weight
    return out


def _set_cohort(bucket: dict[str, Any], row: Mapping[str, Any]) -> None:
    bucket["cohort_votes"][_cohort_for(row)] += 1


def _cohort_for(row: Mapping[str, Any]) -> str:
    strategy = str(row.get("strategy_id") or row.get("agent_id") or "").lower()
    lane = str(row.get("lane_id") or row.get("candidate_id") or "").lower()
    timeframes = row.get("timeframes")
    if isinstance(timeframes, list) and timeframes:
        timeframe = str(timeframes[0]).lower()
    else:
        timeframe = str(row.get("timeframe") or "").lower()
    marker = f"{strategy} {lane}"
    if any(key in marker for key in ("funding", "event", "leadlag", "liquidation")):
        return COHORT_EVENT
    if timeframe in {"1m", "3m", "5m"} or "scalp" in marker or "velocity" in marker:
        return COHORT_SCALPER
    if timeframe in {"15m", "30m"}:
        return COHORT_INTRADAY
    if timeframe == "1h":
        return COHORT_SWING_1H
    if timeframe in {"4h", "1d"}:
        return COHORT_SWING_4H
    return COHORT_RESEARCH


def _agent_for(row: Mapping[str, Any]) -> str:
    for key in ("strategy_id", "scanner_id", "agent_id", "strategy"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    lane = str(row.get("lane_id") or row.get("candidate_id") or "").strip()
    return _strategy_from_lane(lane) if lane else "unknown"


def _strategy_from_lane(lane: str) -> str:
    lowered = lane.lower()
    for marker in ("_delta_india_", "_binanceusdm_", "_binance_", "_bybit_"):
        if marker in lowered:
            return lowered.split(marker, 1)[0]
    return lowered.split("|", 1)[0] or "unknown"


def _merge_identity(bucket: dict[str, Any], row: Mapping[str, Any]) -> None:
    for key, target in (
        ("exchange", "exchanges"),
        ("symbol", "symbols"),
        ("timeframe", "timeframes"),
    ):
        value = row.get(key)
        if isinstance(value, list):
            for item in value:
                if str(item).strip():
                    bucket[target].add(str(item))
        elif str(value or "").strip():
            bucket[target].add(str(value))


def _add_metrics(bucket: dict[str, Any], row: Mapping[str, Any]) -> None:
    closed = _int(row.get("closed_trades"))
    bucket["closed_trades"] += closed
    bucket["closed_net_pnl_usd"] += _float(row.get("closed_net_pnl_usd"))
    bucket["fees_usd"] += _float(row.get("fees_usd"))
    bps = _maybe_float(row.get("avg_closed_trade_net_bps"))
    if bps is not None and closed > 0:
        bucket["bps_weighted_sum"] += bps * closed
        bucket["bps_weighted_n"] += closed
    pf = _maybe_float(row.get("profit_factor"))
    if pf is not None and pf < 999:
        bucket["pf_values"].append(pf)
    bucket["live_signals"] += _int(row.get("live_signals"))
    bucket["paper_order_intents"] += _int(row.get("paper_order_intents"))


def _has_evidence(bucket: Mapping[str, Any], *, config: DarwinianSurvivalConfig) -> bool:
    return (
        sum(Counter(bucket.get("source_counts") or {}).values()) > 0
        or _int(bucket.get("closed_trades")) >= config.min_closed_trades
    )


def _feed_record(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "ts": payload.get("generated_at"),
        "report_id": REPORT_ID,
        "summary": payload.get("summary"),
        "can_trade": False,
        "can_promote": False,
    }


def _rows(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows = payload.get("rows")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, Mapping)]


def _payload_or_file(payload: Mapping[str, Any] | None, path: Path | str | None) -> dict[str, Any]:
    if isinstance(payload, Mapping):
        return dict(payload)
    if path is None:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _count(bucket: dict[str, Any], counter_key: str, value: Any) -> None:
    text = str(value or "").strip()
    if text:
        bucket[counter_key][text] += 1


def _nested(row: Mapping[str, Any], parent: str, child: str) -> Any:
    value = row.get(parent)
    if isinstance(value, Mapping):
        return value.get(child)
    return None


def _dominant(counter: Counter[str]) -> str | None:
    if not counter:
        return None
    return counter.most_common(1)[0][0]


def _agent_sort_key(row: Mapping[str, Any]) -> tuple[float, float, str]:
    return (
        -float(row.get("darwinian_weight") or 0.0),
        -float(row.get("agent_score") or 0.0),
        str(row.get("agent_id") or ""),
    )


def _weighted_avg(total: Any, count: Any) -> float | None:
    n = _int(count)
    if n <= 0:
        return None
    return _float(total) / n


def _avg(values: Any) -> float | None:
    xs = [float(v) for v in values if _maybe_float(v) is not None]
    if not xs:
        return None
    return sum(xs) / len(xs)


def _clamp(value: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, float(value)))


def _bounded(value: Any, lo: float, hi: float) -> float:
    return _clamp(_float(value), lo, hi)


def _int(value: Any) -> int:
    try:
        if value is None:
            return 0
        return int(value)
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return parsed if math.isfinite(parsed) else 0.0


def _maybe_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish Darwinian agent survival report")
    parser.add_argument("--survival", type=Path, default=DEFAULT_SURVIVAL)
    parser.add_argument("--governor", type=Path, default=DEFAULT_GOVERNOR)
    parser.add_argument("--scanner", type=Path, default=DEFAULT_SCANNER)
    parser.add_argument("--alpha-arena", type=Path, default=DEFAULT_ALPHA_ARENA)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--feed", type=Path, default=DEFAULT_FEED)
    parser.add_argument("--interval-seconds", type=float, default=0.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--print", action="store_true")
    args = parser.parse_args(argv)

    import time

    while True:
        payload = build_darwinian_agent_survival(
            survival_path=args.survival,
            governor_path=args.governor,
            scanner_path=args.scanner,
            alpha_arena_path=args.alpha_arena,
            previous_path=args.out,
        )
        publish_darwinian_agent_survival(payload, args.out, args.feed)
        if args.print:
            print(render_report(payload), flush=True)
        if args.once or args.interval_seconds <= 0:
            return 0
        time.sleep(max(1.0, float(args.interval_seconds)))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
