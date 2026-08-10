"""External repo review synthesis for the operator cockpit.

This module is intentionally static and research-only. It converts public repo
reviews into VNEDGE-owned build tracks without importing their runtime code,
execution assumptions, or live-trading authority.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class SourceReview:
    repo: str
    url: str
    signal: str
    vnedge_takeaway: str
    adopt: tuple[str, ...]
    reject: tuple[str, ...]


@dataclass(frozen=True)
class BuildTrack:
    track_id: str
    priority: int
    label: str
    sources: tuple[str, ...]
    operator_value: str
    next_pr: str
    acceptance: tuple[str, ...]
    safety_boundary: str


SOURCES: tuple[SourceReview, ...] = (
    SourceReview(
        repo="Hitheshkaranth/OpenTerminalUI",
        url="https://github.com/Hitheshkaranth/OpenTerminalUI",
        signal="self-hosted terminal-style financial workspace",
        vnedge_takeaway="VNEDGE needs a trader-grade shell around real bot state, not more loose dashboard cards.",
        adopt=(
            "persistent workspace rail",
            "GO command routing",
            "market tape and provenance strip",
            "dense terminal layout",
        ),
        reject=("copied frontend source", "order controls in the browser"),
    ),
    SourceReview(
        repo="OpenBB-finance/OpenBB",
        url="https://github.com/OpenBB-finance/OpenBB",
        signal="open data platform for analysts, quants, and AI agents",
        vnedge_takeaway="Treat market, macro, funding, volatility, and account data as a provider fabric with lineage.",
        adopt=(
            "provider abstraction for research data",
            "data-source lineage in artifacts",
            "AI/research dashboard endpoints",
        ),
        reject=("unvetted external feeds on the live order path",),
    ),
    SourceReview(
        repo="Fincept-Corporation/FinceptTerminal",
        url="https://github.com/Fincept-Corporation/FinceptTerminal",
        signal="professional financial terminal UX and analytics surface",
        vnedge_takeaway="Research and execution truth should live in one workstation-style experience.",
        adopt=(
            "terminal-style navigation",
            "multi-pane research workspace",
            "market analytics hierarchy",
        ),
        reject=("generic equity-terminal scope creep",),
    ),
    SourceReview(
        repo="microsoft/qlib",
        url="https://github.com/microsoft/qlib",
        signal="AI-oriented quant research platform from ideas to production",
        vnedge_takeaway="VNEDGE's alpha factory needs experiment lineage, model freshness, and repeatable research workflows.",
        adopt=(
            "experiment registry",
            "model lifecycle telemetry",
            "feature pipeline governance",
            "workflow-oriented research loops",
        ),
        reject=("promotion by model score alone", "non-causal feature generation"),
    ),
    SourceReview(
        repo="coding-kitties/investing-algorithm-framework",
        url="https://github.com/coding-kitties/investing-algorithm-framework",
        signal="research workflow and backtest evidence organization",
        vnedge_takeaway="Rank research lanes before spending compute, then store every result in a searchable evidence layer.",
        adopt=(
            "factor ranking before heavy sweeps",
            "searchable backtest evidence",
            "parameter bundle archival",
        ),
        reject=("generic live deployment wrappers",),
    ),
    SourceReview(
        repo="evan-kolberg/prediction-market-backtesting",
        url="https://github.com/evan-kolberg/prediction-market-backtesting",
        signal="event-style backtesting and settlement discipline",
        vnedge_takeaway="Some scanner claims should be settled like prediction markets: define the event, close time, outcome, and payoff before replay.",
        adopt=(
            "pre-declared event settlement",
            "outcome ledger",
            "probability calibration checks",
        ),
        reject=("binary-event scoring as a substitute for exchange fills",),
    ),
    SourceReview(
        repo="AI4Finance-Foundation/FinRobot",
        url="https://github.com/AI4Finance-Foundation/FinRobot",
        signal="financial LLM agent platform",
        vnedge_takeaway="Agents should debate, critique, summarize, and request proofs, while execution stays sealed.",
        adopt=(
            "role-specialized financial agents",
            "agent memory over repeated hypotheses",
            "research artifact explanations",
        ),
        reject=("LLM-issued orders", "LLM-only edge approval"),
    ),
    SourceReview(
        repo="chrisworsey55/atlas-gic",
        url="https://github.com/chrisworsey55/atlas-gic",
        signal="self-improving agent trading loop with scored outcomes",
        vnedge_takeaway="Use measured paper/replay outcomes to keep, decay, or retire research agents and hypotheses.",
        adopt=(
            "keep-or-retire loop",
            "agent scorecards tied to real outcomes",
            "regime-specific agent memories",
        ),
        reject=("prompt mutation against burned data", "automatic live escalation"),
    ),
    SourceReview(
        repo="The-Swarm-Corporation/AutoHedge",
        url="https://github.com/The-Swarm-Corporation/AutoHedge",
        signal="swarm-style autonomous hedge fund concept",
        vnedge_takeaway="Separate specialist analysis agents from a hard risk officer and execution gateway.",
        adopt=(
            "specialist agent roles",
            "adversarial risk review",
            "portfolio hedge suggestions as research tasks",
        ),
        reject=("minimal-human-intervention live trading",),
    ),
    SourceReview(
        repo="OpenHands/OpenHands",
        url="https://github.com/OpenHands/OpenHands",
        signal="durable software-agent control plane",
        vnedge_takeaway="Quant OS tasks need event streams, artifacts, retries, and human-readable progress.",
        adopt=(
            "task/event/artifact ledger",
            "operator-visible agent progress",
            "bounded worker loops",
        ),
        reject=("agent access to exchange secrets",),
    ),
    SourceReview(
        repo="cobusgreyling/loop-engineering",
        url="https://github.com/cobusgreyling/loop-engineering",
        signal="practical AI loop design patterns",
        vnedge_takeaway="Research agents need scheduled loops with memory, verifier checks, and human gates.",
        adopt=(
            "loop readiness checklist",
            "maker/verifier split",
            "externalized state memory",
        ),
        reject=("unbounded autonomous loops",),
    ),
    SourceReview(
        repo="JerBouma/FinanceToolkit",
        url="https://github.com/JerBouma/FinanceToolkit",
        signal="transparent financial metrics toolkit",
        vnedge_takeaway="Every performance number should expose its formula and source, especially PF, drawdown, fees, and expectancy.",
        adopt=(
            "transparent metric definitions",
            "metric lineage",
            "portfolio/account diagnostics",
        ),
        reject=("equity-fundamental metrics as crypto scalp alpha",),
    ),
    SourceReview(
        repo="ghostfolio/ghostfolio",
        url="https://github.com/ghostfolio/ghostfolio",
        signal="open-source wealth and portfolio management dashboard",
        vnedge_takeaway="VNEDGE needs a capital/account control center, not only scanner panels.",
        adopt=(
            "account net-worth timeline",
            "allocation/exposure views",
            "fees/funding/cash ledger",
            "operator-friendly account history",
        ),
        reject=("wealth dashboard actions that bypass the order manager",),
    ),
)


BUILD_TRACKS: tuple[BuildTrack, ...] = (
    BuildTrack(
        track_id="terminal_operator_shell_v1",
        priority=100,
        label="Production Operator Terminal",
        sources=(
            "Hitheshkaranth/OpenTerminalUI",
            "Fincept-Corporation/FinceptTerminal",
            "OpenBB-finance/OpenBB",
        ),
        operator_value="Make the cockpit feel like a trading workstation while preserving real-state-only panels.",
        next_pr="codex/openterminal-production-ui",
        acceptance=(
            "fixed rail and GO navigation visible on the cockpit",
            "market tape sourced from snapshot lanes",
            "gateway, live-lock, and build provenance always visible",
        ),
        safety_boundary="Read-only UI; no browser execution controls.",
    ),
    BuildTrack(
        track_id="agentic_research_os_v2",
        priority=96,
        label="Agentic Research OS",
        sources=(
            "OpenHands/OpenHands",
            "AI4Finance-Foundation/FinRobot",
            "chrisworsey55/atlas-gic",
            "The-Swarm-Corporation/AutoHedge",
            "cobusgreyling/loop-engineering",
        ),
        operator_value="Turn agents into durable research workers with scorecards, artifacts, memory, and human gates.",
        next_pr="codex/agentic-research-os-v2",
        acceptance=(
            "agent task, event, artifact, and verifier status visible in dashboard",
            "hypotheses decay or retire based on measured paper/replay outcomes",
            "human gate required before untouched judgment or paper promotion",
        ),
        safety_boundary="Agents can request proofs and publish artifacts only; they cannot trade, promote, or mutate live config.",
    ),
    BuildTrack(
        track_id="experiment_lineage_factory_v1",
        priority=92,
        label="Experiment Lineage Factory",
        sources=(
            "microsoft/qlib",
            "coding-kitties/investing-algorithm-framework",
            "evan-kolberg/prediction-market-backtesting",
        ),
        operator_value="Make every scanner/backtest result queryable by source, feature set, window, cost model, and verdict.",
        next_pr="codex/experiment-lineage-factory",
        acceptance=(
            "single evidence table across Pine, Quantified, scanner, replay, paper, and prediction-style settlement rows",
            "point-in-time source hash and untouched/burned-window state attached",
            "negative results searchable as reusable training data",
        ),
        safety_boundary="Lineage improves research recall; it has no trade authority and is not a promotion gate override.",
    ),
    BuildTrack(
        track_id="account_control_center_v1",
        priority=88,
        label="Capital And Account Control Center",
        sources=(
            "ghostfolio/ghostfolio",
            "JerBouma/FinanceToolkit",
            "OpenBB-finance/OpenBB",
        ),
        operator_value="Show account truth: exposure, leverage, realized PnL, fees, funding, cash ledger, and drawdown by profile.",
        next_pr="codex/account-control-center",
        acceptance=(
            "paper/live profile inputs show margin, leverage, risk, and lot rounding",
            "closed-trade journal reconciles entry, exit, fees, funding, slippage, and expected-vs-realized bps",
            "allocation/exposure and capital-at-risk views exist per venue and symbol",
        ),
        safety_boundary="Account panels are read-only unless routed through existing risk/profile config reviews.",
    ),
    BuildTrack(
        track_id="provider_fabric_v1",
        priority=82,
        label="Research Data Provider Fabric",
        sources=(
            "OpenBB-finance/OpenBB",
            "Fincept-Corporation/FinceptTerminal",
            "JerBouma/FinanceToolkit",
        ),
        operator_value="Bring external context into research without contaminating live execution.",
        next_pr="codex/research-provider-fabric",
        acceptance=(
            "provider registry records source, latency, freshness, license, and live-path eligibility",
            "macro/calendar/funding/volatility context can enrich research artifacts",
            "live execution continues to depend only on approved exchange data feeds",
        ),
        safety_boundary="Provider data can label or rank research; it has no trade authority and cannot directly trigger live orders.",
    ),
)


def build_external_repo_synthesis() -> dict:
    """Return the static research-only external repo synthesis payload."""

    source_names = {source.repo for source in SOURCES}
    track_sources = {repo for track in BUILD_TRACKS for repo in track.sources}
    missing = sorted(track_sources - source_names)
    if missing:
        raise RuntimeError(f"unknown synthesis source references: {missing}")

    return {
        "synthesis_id": "external_repo_synthesis_20260731",
        "generated_at": datetime.now(UTC).isoformat(),
        "research_only": True,
        "can_trade": False,
        "can_promote": False,
        "source_count": len(SOURCES),
        "track_count": len(BUILD_TRACKS),
        "sources": [asdict(source) for source in SOURCES],
        "build_tracks": [asdict(track) for track in BUILD_TRACKS],
        "operator_answer": (
            "External repos are being used as architecture and UI references, "
            "not as copied strategy code or live-trading authority."
        ),
        "non_negotiables": (
            "no copied protected strategy source",
            "no agent direct trading",
            "no promotion without untouched-window proof",
            "no UI write controls around the risk gateway",
            "negative evidence remains training and rejection data",
        ),
    }


if __name__ == "__main__":  # pragma: no cover - convenience CLI
    import json

    print(json.dumps(build_external_repo_synthesis(), indent=2, sort_keys=True))
