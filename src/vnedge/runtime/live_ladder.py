"""Code-enforced promotion ladder for live trading.

This module does not place orders and does not enable live trading. It turns
the operating-mode ladder into an explainable evidence check so an operator
cannot treat "lower rungs validated" as a vague checkbox.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from vnedge.config.settings import Settings
from vnedge.governance.promotion_policy import DEFAULT_PROMOTION_POLICY
from vnedge.governance.proofs import PaperEligibilityProof


class LiveLadderStage(str, Enum):
    BACKTEST = "backtest"
    PAPER = "paper"
    SHADOW = "shadow"
    LIVE_SMALL = "live_small"
    LIVE_FULL = "live_full"


_NEXT_STAGE = {
    LiveLadderStage.BACKTEST: LiveLadderStage.PAPER,
    LiveLadderStage.PAPER: LiveLadderStage.SHADOW,
    LiveLadderStage.SHADOW: LiveLadderStage.LIVE_SMALL,
    LiveLadderStage.LIVE_SMALL: LiveLadderStage.LIVE_FULL,
}


_LADDER_POLICY = DEFAULT_PROMOTION_POLICY.ladder


@dataclass(frozen=True)
class LiveLadderConfig:
    min_paper_days: float = _LADDER_POLICY.min_paper_days
    min_paper_trades: int = _LADDER_POLICY.min_paper_trades
    max_paper_drawdown_pct: float = _LADDER_POLICY.max_paper_drawdown_pct
    min_shadow_days: float = _LADDER_POLICY.min_shadow_days
    min_shadow_trades: int = _LADDER_POLICY.min_shadow_trades
    min_shadow_profit_factor: float = _LADDER_POLICY.min_shadow_profit_factor
    max_shadow_drawdown_pct: float = _LADDER_POLICY.max_shadow_drawdown_pct
    min_live_small_days: float = _LADDER_POLICY.min_live_small_days
    min_live_small_trades: int = _LADDER_POLICY.min_live_small_trades
    max_live_small_drawdown_pct: float = _LADDER_POLICY.max_live_small_drawdown_pct
    policy_version: str = DEFAULT_PROMOTION_POLICY.policy_version


@dataclass(frozen=True)
class LiveLadderEvidence:
    current_stage: LiveLadderStage
    target_stage: LiveLadderStage
    strategy_id: str = ""
    symbol: str = ""

    human_approved: bool = False
    paper_eligibility_proof: PaperEligibilityProof | None = None

    paper_days: float = 0.0
    paper_trades: int = 0
    paper_net_usd: float = 0.0
    paper_max_drawdown_pct: float = 0.0

    shadow_days: float = 0.0
    shadow_trades: int = 0
    shadow_net_usd: float = 0.0
    shadow_profit_factor: float | None = None
    shadow_max_drawdown_pct: float = 0.0

    live_small_days: float = 0.0
    live_small_trades: int = 0
    live_small_net_usd: float = 0.0
    live_small_max_drawdown_pct: float = 0.0

    pre_live_checklist_cleared: bool = False
    three_live_gates_ready: bool = False
    reconciliation_clean: bool = True
    kill_switch_clear: bool = True
    journal_writable: bool = True


@dataclass(frozen=True)
class LiveLadderDecision:
    current_stage: LiveLadderStage
    target_stage: LiveLadderStage
    allowed: bool
    blockers: tuple[str, ...]
    policy_version: str

    @property
    def summary(self) -> str:
        if self.allowed:
            return (
                f"promotion allowed: {self.current_stage.value} -> "
                f"{self.target_stage.value}"
            )
        return (
            f"promotion blocked: {self.current_stage.value} -> "
            f"{self.target_stage.value} ({len(self.blockers)} blocker(s))"
        )


def settings_live_gates_ready(settings: Settings) -> bool:
    """Return true only when the Settings three-gate live contract is open."""
    return settings.is_live


def evaluate_live_ladder(
    evidence: LiveLadderEvidence,
    config: LiveLadderConfig | None = None,
) -> LiveLadderDecision:
    """Evaluate a single promotion attempt and list every missing condition."""
    config = config or LiveLadderConfig()
    blockers: list[str] = []

    expected = _NEXT_STAGE.get(evidence.current_stage)
    if expected is None:
        blockers.append("live_full is terminal; no higher promotion rung exists")
    elif evidence.target_stage is not expected:
        blockers.append(
            "must advance exactly one rung at a time: "
            f"{evidence.current_stage.value} -> {expected.value}"
        )

    if evidence.target_stage is LiveLadderStage.PAPER:
        blockers.extend(_paper_blockers(evidence))
    elif evidence.target_stage is LiveLadderStage.SHADOW:
        blockers.extend(_shadow_blockers(evidence, config))
    elif evidence.target_stage is LiveLadderStage.LIVE_SMALL:
        blockers.extend(_live_small_blockers(evidence, config))
    elif evidence.target_stage is LiveLadderStage.LIVE_FULL:
        blockers.extend(_live_full_blockers(evidence, config))
    else:
        blockers.append(f"unsupported promotion target: {evidence.target_stage.value}")

    return LiveLadderDecision(
        current_stage=evidence.current_stage,
        target_stage=evidence.target_stage,
        allowed=not blockers,
        blockers=tuple(blockers),
        policy_version=config.policy_version,
    )


def _paper_blockers(evidence: LiveLadderEvidence) -> list[str]:
    proof = evidence.paper_eligibility_proof
    if proof is None:
        return ["paper requires a valid PaperEligibilityProof"]
    blockers: list[str] = []
    if not evidence.strategy_id:
        blockers.append("paper promotion requires an explicit strategy identity")
    if not evidence.symbol:
        blockers.append("paper promotion requires an explicit market identity")
    blockers.extend(
        proof.verify(
            policy=DEFAULT_PROMOTION_POLICY,
            strategy_id=evidence.strategy_id or None,
            symbol=evidence.symbol or None,
        )
    )
    return blockers


def _shadow_blockers(
    evidence: LiveLadderEvidence,
    config: LiveLadderConfig,
) -> list[str]:
    blockers: list[str] = []
    if not evidence.human_approved:
        blockers.append("shadow requires explicit human approval after paper")
    if evidence.paper_days < config.min_paper_days:
        blockers.append(
            f"paper trial too short: {evidence.paper_days:g}d "
            f"< {config.min_paper_days:g}d"
        )
    if evidence.paper_trades < config.min_paper_trades:
        blockers.append(
            f"paper trial has too few trades: {evidence.paper_trades} "
            f"< {config.min_paper_trades}"
        )
    if evidence.paper_net_usd <= 0:
        blockers.append(
            f"paper trial is not net-positive: ${evidence.paper_net_usd:.2f}"
        )
    if evidence.paper_max_drawdown_pct > config.max_paper_drawdown_pct:
        blockers.append(
            "paper drawdown too high: "
            f"{evidence.paper_max_drawdown_pct:.2f}% "
            f"> {config.max_paper_drawdown_pct:.2f}%"
        )
    return blockers


def _live_small_blockers(
    evidence: LiveLadderEvidence,
    config: LiveLadderConfig,
) -> list[str]:
    blockers = _live_safety_blockers(evidence)
    if evidence.shadow_days < config.min_shadow_days:
        blockers.append(
            f"shadow trial too short: {evidence.shadow_days:g}d "
            f"< {config.min_shadow_days:g}d"
        )
    if evidence.shadow_trades < config.min_shadow_trades:
        blockers.append(
            f"shadow trial has too few trades: {evidence.shadow_trades} "
            f"< {config.min_shadow_trades}"
        )
    if evidence.shadow_net_usd <= 0:
        blockers.append(
            f"shadow trial is not net-positive: ${evidence.shadow_net_usd:.2f}"
        )
    if evidence.shadow_profit_factor is None:
        blockers.append("shadow trial profit factor is missing")
    elif evidence.shadow_profit_factor < config.min_shadow_profit_factor:
        blockers.append(
            f"shadow profit factor too low: {evidence.shadow_profit_factor:.2f} "
            f"< {config.min_shadow_profit_factor:.2f}"
        )
    if evidence.shadow_max_drawdown_pct > config.max_shadow_drawdown_pct:
        blockers.append(
            "shadow drawdown too high: "
            f"{evidence.shadow_max_drawdown_pct:.2f}% "
            f"> {config.max_shadow_drawdown_pct:.2f}%"
        )
    return blockers


def _live_full_blockers(
    evidence: LiveLadderEvidence,
    config: LiveLadderConfig,
) -> list[str]:
    blockers = _live_safety_blockers(evidence)
    if evidence.live_small_days < config.min_live_small_days:
        blockers.append(
            f"live_small observation too short: {evidence.live_small_days:g}d "
            f"< {config.min_live_small_days:g}d"
        )
    if evidence.live_small_trades < config.min_live_small_trades:
        blockers.append(
            f"live_small has too few trades: {evidence.live_small_trades} "
            f"< {config.min_live_small_trades}"
        )
    if evidence.live_small_net_usd <= 0:
        blockers.append(
            f"live_small is not net-positive: ${evidence.live_small_net_usd:.2f}"
        )
    if evidence.live_small_max_drawdown_pct > config.max_live_small_drawdown_pct:
        blockers.append(
            "live_small drawdown too high: "
            f"{evidence.live_small_max_drawdown_pct:.2f}% "
            f"> {config.max_live_small_drawdown_pct:.2f}%"
        )
    return blockers


def _live_safety_blockers(evidence: LiveLadderEvidence) -> list[str]:
    blockers: list[str] = []
    if not evidence.human_approved:
        blockers.append("live promotion requires explicit human approval")
    if not evidence.pre_live_checklist_cleared:
        blockers.append("pre-live checklist is not cleared")
    if not evidence.three_live_gates_ready:
        blockers.append("three live gates are not open")
    if not evidence.reconciliation_clean:
        blockers.append("reconciliation is not clean")
    if not evidence.kill_switch_clear:
        blockers.append("kill switch is active")
    if not evidence.journal_writable:
        blockers.append("decision journal is not writable")
    return blockers
