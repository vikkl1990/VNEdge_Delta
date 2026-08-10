"""Bounded research agents for edge discovery.

These are intentionally local, deterministic agents. They behave like a
research assistant: inspect rolling walk-forward records, explain where edge
seems to exist, propose whitelisted follow-up lanes, and mark everything as
exploratory. They do not call execution code, do not mutate live trials, and
do not promote strategies.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable

from vnedge.research.strategy_diagnostics import Suggestion, diagnose
from vnedge.research.universe import ResearchTarget, profitable_pairs


@dataclass(frozen=True)
class AgentProposal:
    proposal_id: str
    agent: str
    proposal_type: str
    exchange: str
    symbol: str
    timeframe: str
    parent_strategy: str
    rationale: str
    status: str = "exploratory"
    can_trade: bool = False
    can_promote: bool = False
    requires_untouched_judgment: bool = True
    auto_runnable: bool = False
    variant_id: str | None = None
    strategy_id: str | None = None
    fixed_params: dict | None = None
    grid_axes: dict | None = None
    gates_label: str | None = None
    test_bars: int | None = None
    goal: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_suggestion(cls, record: dict, suggestion: Suggestion) -> "AgentProposal":
        exchange = record.get("exchange", "binanceusdm")
        symbol = record["symbol"]
        timeframe = record.get("timeframe", "1h")
        return cls(
            proposal_id=(
                f"variant|{exchange}|{symbol}|{timeframe}|"
                f"{record['strategy']}|{suggestion.variant_id}"
            ),
            agent="bounded_edge_research_agent",
            proposal_type="variant_backtest",
            exchange=exchange,
            symbol=symbol,
            timeframe=timeframe,
            parent_strategy=record["strategy"],
            variant_id=suggestion.variant_id,
            strategy_id=suggestion.strategy_id,
            fixed_params=dict(suggestion.fixed_params),
            grid_axes=dict(suggestion.grid_axes),
            gates_label=suggestion.gates_label,
            test_bars=suggestion.test_bars,
            goal=suggestion.goal,
            rationale=suggestion.rationale,
            # engine-level proposals (e.g. protections) are research-only:
            # the auto-explorer must never run them
            auto_runnable=suggestion.auto_runnable,
        )


@dataclass(frozen=True)
class AgentPlan:
    profitable_pairs: tuple[dict, ...]
    proposals: tuple[dict, ...]
    policy: dict


class EdgeResearchAgent:
    """Plans bounded follow-up work from rolling research records."""

    def __init__(self, max_variant_proposals: int = 12) -> None:
        self.max_variant_proposals = max_variant_proposals

    def plan(
        self,
        records: Iterable[dict],
        *,
        targets: Iterable[ResearchTarget] = (),
    ) -> AgentPlan:
        records = tuple(records)
        targets = tuple(targets)
        proposals: list[AgentProposal] = []

        for pair in profitable_pairs(records):
            proposals.append(
                AgentProposal(
                    proposal_id=(
                        f"judgment|{pair.exchange}|{pair.symbol}|{pair.timeframe}|"
                        f"{pair.best_strategy}"
                    ),
                    agent="bounded_edge_research_agent",
                    proposal_type="pre_registered_judgment",
                    exchange=pair.exchange,
                    symbol=pair.symbol,
                    timeframe=pair.timeframe,
                    parent_strategy=pair.best_strategy,
                    rationale=(
                        "profitable rolling lane found; candidate only. "
                        "Pre-register untouched-data judgment before paper promotion."
                    ),
                )
            )
            for target in targets:
                if target.symbol != pair.symbol or target.exchange == pair.exchange:
                    continue
                proposals.append(
                    AgentProposal(
                        proposal_id=(
                            f"cross_exchange|{target.exchange}|{target.symbol}|"
                            f"{target.timeframe}|{pair.best_strategy}"
                        ),
                        agent="bounded_edge_research_agent",
                        proposal_type="cross_exchange_validation",
                        exchange=target.exchange,
                        symbol=target.symbol,
                        timeframe=target.timeframe,
                        parent_strategy=pair.best_strategy,
                        rationale=(
                            "same symbol is profitable on another venue; validate "
                            "whether the edge survives venue microstructure, fees, "
                            "and funding differences."
                        ),
                    )
                )

        variant_count = 0
        rejected = [r for r in records if r.get("verdict") == "REJECT" and not r.get("auto")]
        rejected.sort(key=_proposal_priority)
        for record in rejected:
            if variant_count >= self.max_variant_proposals:
                break
            diagnosis = diagnose(record)
            for suggestion in diagnosis.suggestions:
                proposals.append(AgentProposal.from_suggestion(record, suggestion))
                variant_count += 1
                if variant_count >= self.max_variant_proposals:
                    break

        return AgentPlan(
            profitable_pairs=tuple(p.to_dict() for p in profitable_pairs(records)),
            proposals=tuple(p.to_dict() for p in proposals),
            policy={
                "status": "exploratory_only",
                "can_trade": False,
                "can_promote": False,
                "requires_human_approval": True,
                "requires_untouched_judgment": True,
                "max_variant_proposals": self.max_variant_proposals,
            },
        )


def runnable_variant_proposals(plan: AgentPlan) -> tuple[dict, ...]:
    return tuple(p for p in plan.proposals if p.get("auto_runnable")
                 and p.get("proposal_type") == "variant_backtest")


def _proposal_priority(record: dict) -> tuple[float, int, float]:
    net = float(record.get("oos_net_usd", 0.0))
    net_penalty = 0.0 if net > 0 else 1000.0 - net
    return (net_penalty, len(record.get("reasons", [])), -int(record.get("oos_trades", 0)))
