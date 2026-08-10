"""Promotion red-team — the prosecutor that argues AGAINST every candidate.

VNEDGE's whole culture is anti-overfit: IS/OOS collapse gates, the burn
registry, "one run; verdict stands." The gates say whether a candidate *passed*.
This module says why you still might be *fooling yourself* — it builds the bear
case against a candidate that already passed, so a human promoting it has to
answer the prosecution first.

Deliberately NOT an LLM. VNEDGE is single-process by design and no model touches
the stack; every charge here is a NUMBER computed from the candidate's own
walk-forward metrics ("numbers code-calculated, LLMs only narrate"). If an LLM
is ever added it may narrate these findings — it may never invent them.

It is advisory and powerless: it can only argue against. ``can_promote`` and
``can_trade`` are always False; nothing here promotes, trades, or overrides a
gate. It consumes the candidates the experiment index already surfaces and
returns a structured brief for the human-gated promotion step.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from vnedge.research.experiment_index import (
    DEFAULT_BURN_REGISTRY_PATH,
    DEFAULT_FEED,
    DEFAULT_PAPER_TRIALS_DIR,
    KIND_WALK_FORWARD,
    build_experiment_index,
)

RED_TEAM_ID = "promotion_red_team_v1"
DEFAULT_OUT = Path("research/live_research/promotion_red_team_latest.json")

# Severities, worst first.
CRITICAL = "critical"
WARN = "warn"
INFO = "info"
_SEVERITY_RANK = {CRITICAL: 0, WARN: 1, INFO: 2}

# Comfort thresholds — INTENTIONALLY stricter than the promotion GATES. The
# gates decide pass/fail; the red-team argues a *passed* candidate is still thin.
# A charge firing does not mean "rejected"; it means "answer this before you
# promote." Kept as named constants, never magic numbers in the logic.
_THIN_NET_PER_TRADE_USD = 1.0      # < $1 net per OOS trade → edge is a rounding error
_FEE_DRAG_FLIP_MULT = 1.0          # fees >= net → a small fee rise flips it negative
_FEE_DRAG_WARN_MULT = 0.5          # fees >= half of net → fee-sensitive
_SPARSE_OOS_TRADES = 30            # >= gate min (10) but too few to trust the tails
_THIN_PROFIT_FACTOR = 1.3         # passed the 1.1 gate but barely
_THIN_PAYOFF_RATIO = 1.5           # low payoff → win-rate-dependent, fragile
_FRAGILE_WINDOWS_PCT = 60.0        # < 60% of windows profitable → luck-sensitive


@dataclass(frozen=True)
class Charge:
    """One quantified argument against promoting the candidate."""

    name: str
    severity: str
    claim: str
    evidence: dict[str, Any]
    what_would_answer_it: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RedTeamBrief:
    strategy_id: str
    symbol: str
    exchange: str
    input_verdict: str
    recommendation: str
    charges: list[Charge] = field(default_factory=list)
    can_promote: bool = False
    can_trade: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["charges"] = [c.to_dict() for c in self.charges]
        d["critical_count"] = sum(1 for c in self.charges if c.severity == CRITICAL)
        d["warn_count"] = sum(1 for c in self.charges if c.severity == WARN)
        return d


def _f(metrics: dict, key: str) -> float | None:
    v = metrics.get(key)
    return float(v) if isinstance(v, (int, float)) else None


# Each prosecutor takes the metrics dict and returns a Charge or None. Every
# claim cites the number it is built from — no vague "might be overfit".
def _charge_thin_edge(m: dict) -> Charge | None:
    net, trades = _f(m, "oos_net_usd"), _f(m, "oos_trades")
    if net is None or not trades:
        return None
    per = net / trades
    if per >= _THIN_NET_PER_TRADE_USD:
        return None
    return Charge(
        "thin_edge", CRITICAL if per <= 0 else WARN,
        f"net edge is ${per:.2f}/trade over {int(trades)} OOS trades — near the noise floor",
        {"oos_net_usd": net, "oos_trades": trades, "net_per_trade_usd": round(per, 4)},
        "a larger untouched sample where per-trade edge holds, or a wider stop/target that lifts it clear of costs",
    )


def _charge_fee_drag(m: dict) -> Charge | None:
    net, fees = _f(m, "oos_net_usd"), _f(m, "total_fees_usd")
    if net is None or fees is None or net <= 0:
        return None
    mult = fees / net
    if mult < _FEE_DRAG_WARN_MULT:
        return None
    sev = CRITICAL if mult >= _FEE_DRAG_FLIP_MULT else WARN
    return Charge(
        "fee_drag", sev,
        f"fees are {mult:.2f}x the net profit — a small fee/slippage rise flips this negative",
        {"total_fees_usd": fees, "oos_net_usd": net, "fee_to_net_ratio": round(mult, 3)},
        "confirmation on a lower-fee venue, or a maker-route fill model, keeping net positive",
    )


def _charge_single_symbol(m: dict, symbol: str) -> Charge | None:
    # A single walk-forward record is one symbol by construction. Always noted:
    # one symbol cannot distinguish edge from a symbol-specific fluke.
    return Charge(
        "single_symbol", INFO,
        f"evidence is a single symbol ({symbol or 'unknown'}) — no cross-symbol corroboration",
        {"symbol": symbol},
        "an independent PASS on a second symbol, or a portfolio-level result",
    )


def _charge_sparse_sample(m: dict) -> Charge | None:
    trades = _f(m, "oos_trades")
    if trades is None or trades >= _SPARSE_OOS_TRADES:
        return None
    return Charge(
        "sparse_sample", WARN,
        f"only {int(trades)} OOS trades — enough to pass the gate, too few to trust the tails",
        {"oos_trades": trades, "comfort_threshold": _SPARSE_OOS_TRADES},
        "more untouched data until the OOS sample clears a tail-robust count",
    )


def _charge_barely_passed_pf(m: dict) -> Charge | None:
    pf = _f(m, "profit_factor")
    if pf is None or pf >= _THIN_PROFIT_FACTOR:
        return None
    return Charge(
        "barely_passed_profit_factor", WARN,
        f"profit factor {pf:.2f} clears the gate but sits close to break-even",
        {"profit_factor": pf, "comfort_threshold": _THIN_PROFIT_FACTOR},
        "a profit factor that holds well above 1.0 on fresh data",
    )


def _charge_thin_payoff(m: dict) -> Charge | None:
    payoff = _f(m, "payoff_ratio")
    if payoff is None or payoff <= 0 or payoff >= _THIN_PAYOFF_RATIO:
        return None
    return Charge(
        "thin_payoff", WARN,
        f"payoff ratio {payoff:.2f} — small winners vs losers make this win-rate-dependent",
        {"payoff_ratio": payoff, "comfort_threshold": _THIN_PAYOFF_RATIO},
        "evidence the win rate is stable, or a target that widens the payoff",
    )


def _charge_window_fragility(m: dict) -> Charge | None:
    pct = _f(m, "profitable_windows_pct")
    windows, traded = _f(m, "windows"), _f(m, "traded_windows")
    zero_trade = windows is not None and traded is not None and traded < windows
    if (pct is None or pct >= _FRAGILE_WINDOWS_PCT) and not zero_trade:
        return None
    ev: dict[str, Any] = {"profitable_windows_pct": pct}
    if windows is not None:
        ev["windows"] = windows
        ev["traded_windows"] = traded
    bits = []
    if pct is not None and pct < _FRAGILE_WINDOWS_PCT:
        bits.append(f"only {pct:.0f}% of windows profitable")
    if zero_trade:
        bits.append(f"{int(windows - traded)} window(s) took no trade")
    return Charge(
        "window_fragility", WARN,
        "; ".join(bits) + " — result leans on a few windows",
        ev,
        "profitability spread across more windows, or a longer test span",
    )


_PROSECUTORS: tuple[Callable[[dict], Charge | None], ...] = (
    _charge_thin_edge,
    _charge_fee_drag,
    _charge_sparse_sample,
    _charge_barely_passed_pf,
    _charge_thin_payoff,
    _charge_window_fragility,
)


def prosecute(
    metrics: dict[str, Any],
    *,
    strategy_id: str = "",
    symbol: str = "",
    exchange: str = "",
    input_verdict: str = "",
) -> RedTeamBrief:
    """Build the bear case against one candidate from its walk-forward metrics."""
    charges = [c for c in (p(metrics) for p in _PROSECUTORS) if c is not None]
    single = _charge_single_symbol(metrics, symbol)
    if single is not None:
        charges.append(single)
    charges.sort(key=lambda c: _SEVERITY_RANK.get(c.severity, 9))

    criticals = sum(1 for c in charges if c.severity == CRITICAL)
    warns = sum(1 for c in charges if c.severity == WARN)
    # The judge. It never promotes — it only escalates how loudly to object.
    # A single CRITICAL charge means the edge flips negative under a mild
    # assumption (fees exceed net, or negative per-trade edge) — reason enough to
    # block until answered. Two-plus soft warnings warrant answers before a human
    # commits. Otherwise it is defensible, but promotion stays human-gated.
    if criticals >= 1:
        rec = "DO_NOT_PROMOTE_YET"
    elif warns >= 2:
        rec = "NEEDS_ANSWERS"
    else:
        rec = "DEFENSIBLE_BUT_HUMAN_GATED"
    return RedTeamBrief(
        strategy_id=strategy_id,
        symbol=symbol,
        exchange=exchange,
        input_verdict=input_verdict,
        recommendation=rec,
        charges=charges,
    )


def red_team_candidates(
    *,
    feed_path: Path | str = DEFAULT_FEED,
    burn_registry_path: Path | str = DEFAULT_BURN_REGISTRY_PATH,
    paper_trials_dir: Path | str = DEFAULT_PAPER_TRIALS_DIR,
) -> dict[str, Any]:
    """Prosecute every PASSED walk-forward candidate in the experiment index.

    These are exactly the records a human might reach for to promote — so they
    are exactly the ones that deserve a bear case first.
    """
    index = build_experiment_index(
        feed_path=feed_path,
        burn_registry_path=burn_registry_path,
        paper_trials_dir=paper_trials_dir,
    )
    briefs: list[dict[str, Any]] = []
    for rec in index["records"]:
        if rec.get("run_kind") != KIND_WALK_FORWARD or rec.get("verdict") != "PASS":
            continue
        brief = prosecute(
            rec.get("metrics", {}),
            strategy_id=rec.get("strategy_id", ""),
            symbol=rec.get("symbol", ""),
            exchange=rec.get("exchange", ""),
            input_verdict=rec.get("verdict", ""),
        )
        briefs.append(brief.to_dict())

    by_rec: dict[str, int] = {}
    for b in briefs:
        by_rec[b["recommendation"]] = by_rec.get(b["recommendation"], 0) + 1
    return {
        "red_team_id": RED_TEAM_ID,
        "summary": {
            "candidates_prosecuted": len(briefs),
            "by_recommendation": by_rec,
            "do_not_promote_yet": by_rec.get("DO_NOT_PROMOTE_YET", 0),
        },
        "briefs": briefs,
        "policy": {
            "source": "PASSED walk-forward candidates from the experiment index",
            "role": "argues AGAINST candidates only; every charge is code-calculated",
            "can_trade": False,
            "can_promote": False,
        },
    }


def _atomic_write_json(path: Path, payload: dict) -> None:
    from tempfile import NamedTemporaryFile

    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w", dir=path.parent, prefix=path.name, suffix=".tmp", delete=False, encoding="utf-8"
    ) as tmp:
        json.dump(payload, tmp, indent=2, sort_keys=True, default=str)
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)
    path.chmod(0o644)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Argue the bear case against every passed promotion candidate (read-only)."
    )
    parser.add_argument("--feed", default=str(DEFAULT_FEED))
    parser.add_argument("--burn-registry", default=str(DEFAULT_BURN_REGISTRY_PATH))
    parser.add_argument("--paper-trials", default=str(DEFAULT_PAPER_TRIALS_DIR))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--print", action="store_true")
    args = parser.parse_args(argv)

    payload = red_team_candidates(
        feed_path=args.feed,
        burn_registry_path=args.burn_registry,
        paper_trials_dir=args.paper_trials,
    )
    _atomic_write_json(Path(args.out), payload)
    if args.print:
        print(json.dumps(payload["summary"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
