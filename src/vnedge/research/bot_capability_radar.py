"""Competitive crypto-bot capability radar.

The awesome-crypto-trading-bots list is useful as a taxonomy, not as proof
that any bot or strategy is profitable. This module captures that taxonomy as a
research-only VNEDGE artifact so operator debates turn into ranked build gaps
instead of repeated link-reading.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping


BOT_CAPABILITY_RADAR_ID = "crypto_bot_competitive_radar_v1"
DEFAULT_LATEST = Path("research/live_research/bot_capability_radar_latest.json")
DEFAULT_FEED = Path("research/live_research/bot_capability_radar_feed.jsonl")
AWESOME_BOTS_SOURCE = "https://github.com/botcrypto-io/awesome-crypto-trading-bots"


@dataclass(frozen=True)
class PeerProject:
    name: str
    url: str
    archetype: str
    useful_pattern: str
    source_use: str = "architecture_pattern_only"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CapabilityDefinition:
    capability_id: str
    title: str
    archetype: str
    inspired_by: tuple[str, ...]
    peer_pattern: str
    vnedge_evidence: tuple[str, ...]
    status: str
    scalper_relevance: int
    build_priority: int
    next_build: str
    risk_note: str
    should_feed_signal_funnel: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CapabilityAssessment:
    capability_id: str
    title: str
    archetype: str
    status: str
    priority_score: float
    scalper_relevance: int
    build_priority: int
    next_build: str
    risk_note: str
    peer_pattern: str
    inspired_by: tuple[str, ...]
    vnedge_evidence: tuple[str, ...]
    should_feed_signal_funnel: bool
    can_trade: bool = False
    can_promote: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def peer_catalog() -> tuple[PeerProject, ...]:
    """Curated architecture patterns from the awesome-bots taxonomy."""
    return (
        PeerProject(
            "freqtrade",
            "https://github.com/freqtrade/freqtrade",
            "research_execution_framework",
            "backtesting, plotting, money management, ML-assisted optimization",
        ),
        PeerProject(
            "jesse",
            "https://github.com/jesse-ai/jesse",
            "research_framework",
            "strategy research ergonomics and reproducible historical simulations",
        ),
        PeerProject(
            "hummingbot",
            "https://github.com/hummingbot/hummingbot",
            "market_making",
            "connector-heavy automated strategies across centralized and decentralized venues",
        ),
        PeerProject(
            "k",
            "https://github.com/ctubio/Krypto-trading-bot",
            "low_latency_market_making",
            "fast quote placement/cancellation and market-making UI",
        ),
        PeerProject(
            "kelp",
            "https://github.com/stellar/kelp",
            "market_making",
            "explicit market-making bot architecture with exchange connectors",
        ),
        PeerProject(
            "the0",
            "https://github.com/alexanderwanyoike/the0",
            "isolated_strategy_runtime",
            "multi-language strategy containers with isolated deployment",
        ),
        PeerProject(
            "superalgos",
            "https://github.com/Superalgos/Superalgos",
            "visual_research_workspace",
            "integrated charting, data mining, backtesting, paper trading, and deployments",
        ),
        PeerProject(
            "octobot",
            "https://github.com/Drakkar-Software/OctoBot",
            "modular_bot_platform",
            "modular trading tools, backtesting, and user interface",
        ),
        PeerProject(
            "opentrader",
            "https://github.com/bludnic/opentrader",
            "retail_bot_workspace",
            "multi-bot UI with grid/DCA, paper trading, backtesting, and CCXT exchange reach",
        ),
        PeerProject(
            "ccxt",
            "https://github.com/ccxt/ccxt",
            "exchange_adapter_library",
            "broad unified exchange connectivity",
        ),
        PeerProject(
            "tradingview_lightweight_charts",
            "https://github.com/tradingview/lightweight-charts",
            "charting",
            "small, fast financial charting primitives for production-grade trader UI",
        ),
    )


def capability_catalog() -> tuple[CapabilityDefinition, ...]:
    """VNEDGE-relevant capabilities derived from peer-bot archetypes."""
    return (
        CapabilityDefinition(
            capability_id="walk_forward_research_engine",
            title="Walk-forward research and backtesting",
            archetype="research_execution_framework",
            inspired_by=("freqtrade", "jesse", "backtrader"),
            peer_pattern="repeatable historical simulation before execution",
            vnedge_evidence=(
                "fee-aware backtester",
                "promotion gates",
                "daily scalper pack",
                "alpha distillation pack",
            ),
            status="covered",
            scalper_relevance=8,
            build_priority=3,
            next_build="keep improving data quality and causality diagnostics",
            risk_note="covered does not imply profitable edge",
        ),
        CapabilityDefinition(
            capability_id="maker_quote_lifecycle_engine",
            title="Maker quote lifecycle engine",
            archetype="market_making",
            inspired_by=("hummingbot", "k", "kelp"),
            peer_pattern=(
                "dedicated quote placement, cancel/replace, inventory skew, "
                "and fill analytics"
            ),
            vnedge_evidence=(
                "maker-first replay assumptions",
                "pre-trade gateway",
                "order manager idempotency",
            ),
            status="partial",
            scalper_relevance=10,
            build_priority=10,
            next_build=(
                "build replay-first maker quote lifecycle: quote intent, cancel/replace, "
                "queue/adverse-selection telemetry, and inventory skew simulation"
            ),
            risk_note=(
                "without this, scalping keeps paying taker-like costs or "
                "optimistic fill assumptions"
            ),
            should_feed_signal_funnel=True,
        ),
        CapabilityDefinition(
            capability_id="strategy_sandbox_isolation",
            title="AI strategy sandbox isolation",
            archetype="isolated_strategy_runtime",
            inspired_by=("the0", "superalgos"),
            peer_pattern="strategies run in isolated runtimes with explicit contracts",
            vnedge_evidence=("roadmap item exists", "strategy contract not yet enforced"),
            status="gap",
            scalper_relevance=8,
            build_priority=9,
            next_build=(
                "create AST-validated AI strategy sandbox with allowed imports, data-only "
                "inputs, artifact output, and no core-source mutation"
            ),
            risk_note=(
                "agent-generated strategies must not bypass governance or "
                "overfit seen windows"
            ),
            should_feed_signal_funnel=False,
        ),
        CapabilityDefinition(
            capability_id="terminal_grade_operator_ui",
            title="Terminal-grade operator UI",
            archetype="visual_research_workspace",
            inspired_by=("superalgos", "octobot", "opentrader", "tradingview_lightweight_charts"),
            peer_pattern=(
                "dense market state, charting, bot status, logs, and research "
                "proof in one workspace"
            ),
            vnedge_evidence=(
                "cockpit UI exists",
                "alpha council panel exists",
                "operator still flags UI as immature",
            ),
            status="partial",
            scalper_relevance=7,
            build_priority=8,
            next_build=(
                "replace card-heavy cockpit surfaces with terminal-grade tape, ladder, "
                "latency, replay, and rejection drill-down panels"
            ),
            risk_note="UI polish must stay read-only and never add control routes",
        ),
        CapabilityDefinition(
            capability_id="multi_exchange_adapter_depth",
            title="Multi-exchange adapter depth",
            archetype="exchange_adapter_library",
            inspired_by=("ccxt", "hummingbot", "opentrader"),
            peer_pattern="broad exchange reach with venue-specific capability checks",
            vnedge_evidence=(
                "research lanes for Binance, Bybit, Delta India",
                "tick recorders for three venues",
                "live execution still intentionally gated",
            ),
            status="partial",
            scalper_relevance=8,
            build_priority=7,
            next_build=(
                "complete venue capability matrix: fees, min notional, precision, "
                "post-only support, reduce-only, funding, order types, and latency"
            ),
            risk_note=(
                "same signal can be tradable on one venue and dead on another "
                "due to fees/fills"
            ),
        ),
        CapabilityDefinition(
            capability_id="market_data_redundancy",
            title="Market data redundancy and vendor fallback",
            archetype="market_data_library",
            inspired_by=("ccxt", "ccxws", "shrimpy", "coinapi"),
            peer_pattern="redundant market-data paths and exchange-normalized streams",
            vnedge_evidence=("native recorders", "CCXT ingestion", "data quality gate"),
            status="partial",
            scalper_relevance=9,
            build_priority=7,
            next_build=(
                "add data-source health quorum and cross-source drift checks before judging "
                "microstructure candidates"
            ),
            risk_note="bad ticks create fake scalper edge faster than candle systems notice",
        ),
        CapabilityDefinition(
            capability_id="shadow_paper_governance",
            title="Shadow/paper governance ladder",
            archetype="governed_execution",
            inspired_by=("freqtrade", "superalgos", "opentrader"),
            peer_pattern="paper/backtest controls before real capital",
            vnedge_evidence=(
                "mode ladder",
                "paper/shadow runner",
                "three live gates",
                "risk gateway",
            ),
            status="covered",
            scalper_relevance=9,
            build_priority=4,
            next_build="keep trial manifests immutable and add more per-lane paper telemetry",
            risk_note="governance protects capital but does not create alpha",
        ),
        CapabilityDefinition(
            capability_id="portfolio_bot_modes",
            title="Grid/DCA/portfolio bot modes",
            archetype="retail_bot_workspace",
            inspired_by=("opentrader", "octobot"),
            peer_pattern="retail multi-bot workspace with grid and DCA strategies",
            vnedge_evidence=(
                "portfolio backtester exists",
                "VNEDGE focus remains scalping and F&O risk",
            ),
            status="watchlist",
            scalper_relevance=3,
            build_priority=2,
            next_build="do not prioritize until scalper and governed swing lanes have proof",
            risk_note="grid/DCA can hide martingale exposure and is not a free edge",
        ),
    )


def run_bot_capability_radar(
    *,
    status_overrides: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a source-aware capability radar for VNEDGE."""
    status_overrides = status_overrides or {}
    assessments = tuple(
        _assess(definition, status_overrides.get(definition.capability_id))
        for definition in capability_catalog()
    )
    ranked = sorted(assessments, key=_assessment_sort_key)
    source_counts: dict[str, int] = {}
    for peer in peer_catalog():
        source_counts[peer.archetype] = source_counts.get(peer.archetype, 0) + 1
    return {
        "generated_at": (now or datetime.now(UTC)).isoformat(),
        "radar_id": BOT_CAPABILITY_RADAR_ID,
        "source": {
            "url": AWESOME_BOTS_SOURCE,
            "use": "architecture taxonomy only",
            "not_profit_evidence": True,
            "warning": "peer listings are not tested by VNEDGE and are not trading proof",
        },
        "policy": {
            "research_only": True,
            "can_trade": False,
            "can_promote": False,
            "live_orders_enabled": False,
            "copying_policy": "extract architecture patterns only; do not copy proprietary logic",
        },
        "summary": _summary(ranked),
        "peer_catalog": [peer.to_dict() for peer in peer_catalog()],
        "source_archetypes": source_counts,
        "capabilities": [row.to_dict() for row in ranked],
        "top_builds": [
            row.to_dict()
            for row in ranked
            if row.status in {"gap", "partial"} and row.priority_score >= 70
        ][:5],
        "can_trade": False,
        "can_promote": False,
    }


def publish_bot_capability_radar(
    payload: Mapping[str, Any],
    out: Path,
    feed: Path | None = None,
) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    tmp.replace(out)
    if feed is not None:
        feed.parent.mkdir(parents=True, exist_ok=True)
        with open(feed, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, default=str) + "\n")


def _assess(
    definition: CapabilityDefinition,
    status_override: str | None,
) -> CapabilityAssessment:
    status = status_override or definition.status
    priority = _priority_score(
        status=status,
        scalper_relevance=definition.scalper_relevance,
        build_priority=definition.build_priority,
        signal_funnel=definition.should_feed_signal_funnel,
    )
    return CapabilityAssessment(
        capability_id=definition.capability_id,
        title=definition.title,
        archetype=definition.archetype,
        status=status,
        priority_score=priority,
        scalper_relevance=definition.scalper_relevance,
        build_priority=definition.build_priority,
        next_build=definition.next_build,
        risk_note=definition.risk_note,
        peer_pattern=definition.peer_pattern,
        inspired_by=definition.inspired_by,
        vnedge_evidence=definition.vnedge_evidence,
        should_feed_signal_funnel=definition.should_feed_signal_funnel,
    )


def _priority_score(
    *,
    status: str,
    scalper_relevance: int,
    build_priority: int,
    signal_funnel: bool,
) -> float:
    status_boost = {
        "gap": 36.0,
        "partial": 24.0,
        "watchlist": 6.0,
        "covered": -12.0,
    }.get(status, 0.0)
    score = (
        status_boost
        + scalper_relevance * 4.0
        + build_priority * 3.0
        + (8.0 if signal_funnel else 0.0)
    )
    return round(max(0.0, min(score, 100.0)), 2)


def _assessment_sort_key(row: CapabilityAssessment) -> tuple[float, str]:
    return (-row.priority_score, row.capability_id)


def _summary(rows: Iterable[CapabilityAssessment]) -> dict[str, Any]:
    rows = tuple(rows)
    by_status: dict[str, int] = {}
    by_archetype: dict[str, int] = {}
    for row in rows:
        by_status[row.status] = by_status.get(row.status, 0) + 1
        by_archetype[row.archetype] = by_archetype.get(row.archetype, 0) + 1
    return {
        "capabilities": len(rows),
        "by_status": by_status,
        "by_archetype": by_archetype,
        "top_gap": rows[0].capability_id if rows else None,
        "signal_funnel_relevant": sum(1 for row in rows if row.should_feed_signal_funnel),
        "can_trade": False,
        "can_promote": False,
    }


def _parse_status_overrides(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    out: dict[str, str] = {}
    for item in raw.split(","):
        if not item.strip():
            continue
        key, sep, value = item.partition("=")
        if not sep:
            raise ValueError(f"invalid status override: {item!r}; expected id=status")
        out[key.strip()] = value.strip()
    return out


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="build VNEDGE competitive bot radar")
    parser.add_argument("--out", default=str(DEFAULT_LATEST))
    parser.add_argument("--feed", default=str(DEFAULT_FEED))
    parser.add_argument("--status-overrides", default="")
    parser.add_argument("--interval-seconds", type=int, default=0)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    overrides = _parse_status_overrides(args.status_overrides)
    out = Path(args.out)
    feed = Path(args.feed) if args.feed else None
    while True:
        payload = run_bot_capability_radar(status_overrides=overrides)
        publish_bot_capability_radar(payload, out, feed)
        if args.json:
            print(json.dumps(payload, indent=2, default=str))
        if args.interval_seconds <= 0:
            return 0
        time.sleep(max(args.interval_seconds, 1))


if __name__ == "__main__":
    raise SystemExit(main())
