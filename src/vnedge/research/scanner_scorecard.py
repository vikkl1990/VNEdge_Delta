"""Scanner scorecard — the closed-loop selection brain.

The architectural correction (2026-07-18): the fleet ran OPEN-LOOP — scanners
fired into journals and nothing measured whether the signals paid, so the only
available lever was "add another scanner" (44 modules, 0 verdicts). This module
closes the loop: it consumes per-scanner realised outcomes and decides, per
scanner, ACTIVE / REDUCED / SUPPRESSED / PROBATION.

It is DENY-BY-DEFAULT: a scanner is not tradeable until it *proves* a
significant, fee-aware, net-positive expectancy. This inverts "everything runs
until manually killed" into "nothing is active until earned".

Hardened beyond the first bot's ``scanner_weights.py`` with this session's
lessons: expectancy alone is not enough (a +0.2 bps coin-flip is noise), so a
scanner must ALSO clear a sign-permutation significance test to go ACTIVE.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

# Lifecycle statuses. can_trade is TRUE only for ACTIVE.
STATUS_ACTIVE = "active"          # proven significant net-positive → eligible
STATUS_REDUCED = "reduced"        # positive but weak or not significant → shadow, watch
STATUS_SUPPRESSED = "suppressed"  # proven non-positive → shadow-only, do not trade
STATUS_PROBATION = "probation"    # under-sampled → collecting, deny by default


@dataclass(frozen=True)
class ScorecardConfig:
    """Thresholds are net-of-fees, in basis points per signal."""
    min_samples: int = 20             # below this, PROBATION (deny by default)
    active_expectancy_bps: float = 2.0  # >= this AND significant → ACTIVE
    suppress_expectancy_bps: float = 0.0  # <= this → SUPPRESSED
    significance_p: float = 0.10      # sign-permutation p must be below this for ACTIVE
    permutations: int = 5000
    seed: int = 20260718


@dataclass(frozen=True)
class ScannerVerdict:
    scanner_id: str
    status: str
    samples: int
    expectancy_bps: float
    win_rate: float
    perm_p: float
    reason: str

    @property
    def can_trade(self) -> bool:
        return self.status == STATUS_ACTIVE

    def to_dict(self) -> dict:
        return {
            "scanner_id": self.scanner_id, "status": self.status,
            "samples": self.samples, "expectancy_bps": round(self.expectancy_bps, 4),
            "win_rate": round(self.win_rate, 4), "perm_p": round(self.perm_p, 4),
            "reason": self.reason, "can_trade": self.can_trade,
        }


def sign_permutation_pvalue(
    net_bps: Sequence[float], permutations: int, seed: int
) -> float:
    """P(random-direction total >= observed) under a sign-flip null. Low p =>
    the directional edge is unlikely to be a coin flip. Deterministic (seeded)."""
    xs = [float(x) for x in net_bps]
    if len(xs) < 8:
        return float("nan")
    observed = sum(xs)
    rng = random.Random(seed)
    ge = 0
    for _ in range(permutations):
        if sum(x if rng.random() < 0.5 else -x for x in xs) >= observed:
            ge += 1
    return (ge + 1) / (permutations + 1)


def _verdict_for(scanner_id: str, net_bps: Sequence[float], cfg: ScorecardConfig) -> ScannerVerdict:
    xs = [float(x) for x in net_bps]
    n = len(xs)
    if n == 0:
        return ScannerVerdict(scanner_id, STATUS_PROBATION, 0, 0.0, 0.0, float("nan"),
                              "no outcomes recorded")
    exp = sum(xs) / n
    win_rate = sum(1 for x in xs if x > 0) / n
    if n < cfg.min_samples:
        return ScannerVerdict(scanner_id, STATUS_PROBATION, n, exp, win_rate, float("nan"),
                              f"only {n}/{cfg.min_samples} samples — collecting (deny by default)")
    p = sign_permutation_pvalue(xs, cfg.permutations, cfg.seed)
    if exp <= cfg.suppress_expectancy_bps:
        return ScannerVerdict(scanner_id, STATUS_SUPPRESSED, n, exp, win_rate, p,
                              f"net expectancy {exp:+.2f} bps <= {cfg.suppress_expectancy_bps} — suppressed")
    if exp >= cfg.active_expectancy_bps and p < cfg.significance_p:
        return ScannerVerdict(scanner_id, STATUS_ACTIVE, n, exp, win_rate, p,
                              f"net {exp:+.2f} bps, perm_p {p:.3f} < {cfg.significance_p} — proven")
    if p >= cfg.significance_p:
        return ScannerVerdict(scanner_id, STATUS_REDUCED, n, exp, win_rate, p,
                              f"net {exp:+.2f} bps but perm_p {p:.3f} — not significant, reduced")
    return ScannerVerdict(scanner_id, STATUS_REDUCED, n, exp, win_rate, p,
                          f"net {exp:+.2f} bps below active floor {cfg.active_expectancy_bps} — reduced")


class ScannerScorecard:
    """Evaluate scanners from realised per-signal net_bps outcomes."""

    def __init__(self, config: ScorecardConfig | None = None) -> None:
        self._cfg = config or ScorecardConfig()
        self._verdicts: dict[str, ScannerVerdict] = {}

    def evaluate(self, outcomes_by_scanner: Mapping[str, Sequence[float]]) -> dict[str, ScannerVerdict]:
        self._verdicts = {
            sid: _verdict_for(sid, nets, self._cfg)
            for sid, nets in outcomes_by_scanner.items()
        }
        return dict(self._verdicts)

    def status(self, scanner_id: str) -> str:
        """Deny-by-default: an unknown scanner is PROBATION, never tradeable."""
        v = self._verdicts.get(scanner_id)
        return v.status if v else STATUS_PROBATION

    def can_trade(self, scanner_id: str) -> bool:
        v = self._verdicts.get(scanner_id)
        return bool(v and v.can_trade)

    def active_scanners(self) -> list[str]:
        return sorted(sid for sid, v in self._verdicts.items() if v.can_trade)

    def to_json(self, as_of: str) -> str:
        return json.dumps({
            "as_of": as_of,
            "config": {
                "min_samples": self._cfg.min_samples,
                "active_expectancy_bps": self._cfg.active_expectancy_bps,
                "suppress_expectancy_bps": self._cfg.suppress_expectancy_bps,
                "significance_p": self._cfg.significance_p,
            },
            "verdicts": [v.to_dict() for v in
                         sorted(self._verdicts.values(), key=lambda v: -v.expectancy_bps)],
        }, indent=2)

    def write(self, path: Path, as_of: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json(as_of))
