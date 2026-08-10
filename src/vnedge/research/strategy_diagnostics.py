"""Automated failure diagnosis + bounded uplift-variant proposals.

When a research lane comes back REJECT, this module answers two questions the
operator would otherwise answer by hand:

  1. WHY did it fail? (categorise the gate reasons + side attribution)
  2. What bounded, WHITELISTED variant might do better, and why?

Hard boundary — this is a research assistant, not an auto-tuner:

  - Proposals are drawn from a fixed per-strategy CATALOG. The engine cannot
    invent arbitrary parameter searches, so it cannot torture the data until
    something passes by chance.
  - A proposed variant is a *candidate hypothesis*. Even if the auto-explorer
    later runs it and it passes the rolling lab, promotion still requires a
    human-approved, pre-registered judgment on UNTOUCHED data.
  - Some failures (win-concentration, IS/OOS collapse) are diagnosed as
    "needs more data / do NOT tune" — proposing a parameter change there is
    the overfitting trap, so the catalog deliberately offers none.
  - Nothing here touches the running paper trial.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Suggestion:
    variant_id: str          # unique, e.g. "funding_mean_reversion_v1__short_only"
    strategy_id: str         # base strategy to instantiate
    fixed_params: dict       # applied to every grid combo
    grid_axes: dict          # param_grid axes to sweep
    gates_label: str         # "sparse" | "standard" | "offensive"
    test_bars: int
    goal: str                # side_restrict | increase_quality | reduce_risk | ...
    rationale: str
    # False = research PROPOSAL only: the auto-explorer must not run it
    # (e.g. engine-level protection variants — their axes are not strategy
    # constructor params). A human decides whether/how to test it.
    auto_runnable: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Diagnosis:
    strategy: str
    symbol: str
    healthy: bool
    failure_tags: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    suggestions: tuple[Suggestion, ...] = ()

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy, "symbol": self.symbol,
            "healthy": self.healthy, "failure_tags": list(self.failure_tags),
            "notes": list(self.notes),
            "suggestions": [s.to_dict() for s in self.suggestions],
        }


# --- Failure categorisation -------------------------------------------------------

def _tags_from_reasons(reasons: list[str]) -> set[str]:
    tags: set[str] = set()
    for r in reasons:
        low = r.lower()
        if "not positive" in low or "no oos gross wins" in low:
            tags.add("negative_net")
        if "profit factor" in low:
            tags.add("low_pf")
        if "payoff ratio" in low:
            tags.add("low_payoff")
        if "total oos trades" in low or "valid oos splits" in low:
            tags.add("too_few_trades")
        if "windows traded" in low:
            tags.add("low_coverage")
        if "drawdown" in low:
            tags.add("high_drawdown")
        if "lucky trade" in low or "single trade contributes" in low:
            tags.add("win_concentration")
        if "collapse" in low or "retention" in low:
            tags.add("is_oos_collapse")
        if "zero oos trades" in low:
            tags.add("zero_trade_windows")
        if "consecutive stop" in low:
            tags.add("consecutive_stops")
    return tags


# Stops clustering in runs of this length (or longer) tag the record as a
# consecutive-stops failure — a candidate for the ENGINE-LEVEL post-stop
# cooldown protection (risk/protections.py), not for strategy-param tuning.
CONSECUTIVE_STOPS_MIN = 3

# Whitelisted protection grid — the only cooldown values a diagnosis may
# propose. Engine-config axes are namespaced "protections.<field>" so they
# can never be mistaken for strategy constructor params.
PROTECTION_COOLDOWN_GRID: tuple[int, ...] = (3, 6)


def _protection_suggestion(strategy_id: str, gates_label: str) -> Suggestion:
    return Suggestion(
        variant_id=f"{strategy_id}__stop_cooldown",
        strategy_id=strategy_id,
        fixed_params={},
        grid_axes={
            "protections.cooldown_bars_after_stop": list(PROTECTION_COOLDOWN_GRID)
        },
        gates_label=gates_label,
        test_bars=720,
        goal="protect_after_stop",
        rationale=(
            "losses cluster in consecutive-stop runs; test an engine-level "
            "post-stop entry cooldown (ProtectionConfig, default OFF). "
            "Research proposal ONLY — not auto-runnable; enabling any "
            "protection on a trial requires pre-registration "
            "(docs/PROTECTIONS.md). Protections are hypotheses to test, "
            "not obvious improvements."
        ),
        auto_runnable=False,
    )


# --- Per-strategy variant catalog (the whitelist) ---------------------------------
# Each goal maps to a template built into a concrete Suggestion by _mk().

def _mk(strategy_id, suffix, fixed, axes, gates, test_bars, goal, why) -> Suggestion:
    return Suggestion(
        variant_id=f"{strategy_id}__{suffix}", strategy_id=strategy_id,
        fixed_params=fixed, grid_axes=axes, gates_label=gates,
        test_bars=test_bars, goal=goal, rationale=why,
    )


def _funding_mr_catalog(winning_side: str | None) -> dict[str, Suggestion]:
    sid, axes, gates, tb = "funding_mean_reversion_v1", \
        {"extreme_pct": [0.85, 0.95], "z_entry": [1.5, 2.5]}, "sparse", 720
    cat: dict[str, Suggestion] = {
        "increase_quality": _mk(
            sid, "stricter_extreme", {}, {"extreme_pct": [0.92, 0.96],
            "z_entry": [2.0, 2.5]}, gates, tb, "increase_quality",
            "raise the funding/extension bar so only the richest crowding "
            "triggers — trades fewer, cleaner setups"),
    }
    if winning_side:
        cat["side_restrict"] = _mk(
            sid, f"{winning_side}_only", {"allowed_sides": [winning_side]},
            axes, gates, tb, "side_restrict",
            f"attribution shows the {winning_side} side carries the edge; "
            f"drop the drag side")
    return cat


def _best_positive_family(record: dict) -> str | None:
    families = record.get("family_attribution", {})
    candidates = []
    for family, stats in families.items():
        trades = int(stats.get("trades", 0))
        net = float(stats.get("net_usd", 0.0))
        pf = float(stats.get("profit_factor", 0.0))
        payoff = float(stats.get("payoff_ratio", 0.0))
        if trades >= 5 and net > 0:
            candidates.append((net, pf, payoff, trades, family))
    if not candidates:
        return None
    return max(candidates)[-1]


def _quant_signal_pack_catalog(
    winning_side: str | None,
    winning_family: str | None,
) -> dict[str, Suggestion]:
    sid, gates, tb = "quant_signal_pack_v1", "offensive", 720
    cat: dict[str, Suggestion] = {
        "increase_quality": _mk(
            sid, "higher_confluence",
            {"min_score": 6.0, "min_volume_z": 0.6},
            {"structure_window": [24, 48], "take_profit_r": [2.0, 2.5]},
            gates, tb, "increase_quality",
            "raise the confluence and volume bar so visual-style marks do "
            "not become low-quality fee churn"),
        "reduce_risk": _mk(
            sid, "tighter_stop",
            {"stop_atr_mult": 1.1, "take_profit_r": 2.5},
            {"structure_window": [24, 48], "min_score": [5.0, 6.0]},
            gates, tb, "reduce_risk",
            "tighten the structure stop and stretch reward; useful when "
            "payoff ratio or drawdown is the failing gate"),
        "macro_family_focus": _mk(
            sid, "sweep_fvg_squeeze_only",
            {"allowed_families": ["liquidity_sweep", "fvg_retest", "squeeze_release"]},
            {"structure_window": [24, 48], "take_profit_r": [2.0, 2.5]},
            gates, tb, "family_restrict",
            "focus on the three families most likely to encode actual "
            "forced-flow or displacement instead of passive trend marks"),
    }
    if winning_side:
        cat["side_restrict"] = _mk(
            sid, f"{winning_side}_only",
            {"allowed_sides": [winning_side]},
            {"structure_window": [24, 48], "take_profit_r": [2.0, 2.5]},
            gates, tb, "side_restrict",
            f"attribution shows the {winning_side} side carries the edge; "
            "drop the drag side")
    if winning_family:
        cat["family_restrict"] = _mk(
            sid, f"{winning_family}_only",
            {"allowed_families": [winning_family]},
            {"structure_window": [24, 48], "min_score": [5.0, 6.0],
             "take_profit_r": [2.0, 2.5]},
            gates, tb, "family_restrict",
            f"family attribution shows {winning_family} is the carrying "
            "sub-pattern; isolate it before wider mining")
    return cat


CATALOG: dict[str, dict[str, Suggestion]] = {
    "volatility_expansion_breakout_v1": {
        "increase_quality": _mk(
            "volatility_expansion_breakout_v1", "vol_confirmed",
            {"min_volume_z": 1.0}, {"breakout_bars": [48, 96]}, "offensive", 720,
            "increase_quality",
            "require stronger volume confirmation — most breakouts fail, so "
            "demand more evidence before paying the entry"),
        "reduce_risk": _mk(
            "volatility_expansion_breakout_v1", "tighter_stop",
            {"stop_atr_mult": 1.5, "take_profit_r": 3.0},
            {"breakout_bars": [48, 96]}, "offensive", 720, "reduce_risk",
            "tighten the stop and stretch the target — improves payoff ratio "
            "if the winners run"),
    },
    "panic_reversal_v1": {
        "increase_frequency": _mk(
            "panic_reversal_v1", "looser_panic", {"drop_z_entry": -2.0},
            {"drop_z_entry": [-2.0, -2.3]}, "offensive", 720, "increase_frequency",
            "loosen the panic threshold — 365d produced too few qualifying "
            "events; WARNING: trades quality for quantity, watch payoff"),
    },
    "funding_squeeze_continuation_v1": {
        "increase_quality": _mk(
            "funding_squeeze_continuation_v1", "vol_confirmed",
            {"min_volume_z": 0.75}, {"extreme_pct": [0.88, 0.94]}, "offensive", 720,
            "increase_quality",
            "require volume expansion to confirm the squeeze is live, not a "
            "stale funding print"),
    },
    "trend_continuation_v1": {
        "reduce_risk": _mk(
            "trend_continuation_v1", "higher_target",
            {"take_profit_r": 3.0}, {"breakout_bars": [48, 96]}, "standard", 360,
            "reduce_risk",
            "stretch the target for a better payoff ratio on the winners"),
    },
    "alpha_stack_confluence_v1": {
        "increase_quality": _mk(
            "alpha_stack_confluence_v1", "higher_confluence",
            {"min_score": 6.0}, {"structure_window": [24, 48], "take_profit_r": [2.0]},
            "offensive", 720, "increase_quality",
            "raise the confluence bar so SMC-style marks do not become noisy "
            "indicator spam"),
        "reduce_risk": _mk(
            "alpha_stack_confluence_v1", "tighter_structure_risk",
            {"stop_atr_mult": 1.2, "take_profit_r": 2.5},
            {"structure_window": [24, 48]}, "offensive", 720, "reduce_risk",
            "tighten ATR risk while keeping the structure stop; useful when "
            "payoff ratio is the limiting gate"),
    },
    "quant_signal_pack_v1": _quant_signal_pack_catalog(None, None),
}


def diagnose(record: dict) -> Diagnosis:
    """Analyse one wf_record; propose bounded uplift variants (never mutate)."""
    strategy, symbol = record["strategy"], record["symbol"]
    if record["verdict"] == "PASS":
        return Diagnosis(strategy, symbol, healthy=True)

    tags = _tags_from_reasons(record.get("reasons", []))
    if int(record.get("max_consecutive_stops", 0)) >= CONSECUTIVE_STOPS_MIN:
        tags.add("consecutive_stops")
    notes: list[str] = []
    goals: list[str] = []

    if "consecutive_stops" in tags:
        notes.append(
            "stops cluster in consecutive runs — candidate for the "
            "engine-level post-stop cooldown (default OFF, pre-registration "
            "required; a hypothesis to test, not an obvious improvement)"
        )

    # Some failures must NOT be "fixed" by tuning — say so and offer nothing.
    if "win_concentration" in tags:
        notes.append("profit concentrated in one trade — likely luck, not "
                     "edge; needs MORE DATA, not parameter changes")
    if "is_oos_collapse" in tags:
        notes.append("in-sample >> out-of-sample: overfit signature; do NOT "
                     "tune to the test window")

    # Side attribution: is one side carrying the edge?
    att = record.get("attribution", {})
    long_net = att.get("long", {}).get("net_usd", 0.0)
    short_net = att.get("short", {}).get("net_usd", 0.0)
    winning_side = None
    if long_net > 0 >= short_net and abs(long_net) > abs(short_net):
        winning_side = "long"
        notes.append("long side carries; short drags")
    elif short_net > 0 >= long_net and abs(short_net) > abs(long_net):
        winning_side = "short"
        notes.append("short side carries; long drags")
    if winning_side:
        goals.append("side_restrict")  # drop the drag side (pre-registered variant)

    winning_family = None
    if strategy == "quant_signal_pack_v1":
        winning_family = _best_positive_family(record)
        if winning_family:
            notes.append(f"{winning_family} family carries; mixed families drag")
            goals.append("family_restrict")

    # Map failure tags -> catalog goals (ordered by leverage).
    if {"low_pf", "low_payoff"} & tags:
        goals += ["increase_quality", "reduce_risk"]
    if {"negative_net"} & tags and "side_restrict" not in goals:
        goals += ["increase_quality", "reduce_risk"]
    if {"too_few_trades", "zero_trade_windows", "low_coverage"} & tags:
        goals.append("increase_frequency")
    if "high_drawdown" in tags:
        goals.append("reduce_risk")

    catalog = CATALOG.get(strategy, {})
    if strategy == "funding_mean_reversion_v1":
        catalog = _funding_mr_catalog(winning_side)
    elif strategy == "quant_signal_pack_v1":
        catalog = _quant_signal_pack_catalog(winning_side, winning_family)
        if "family_restrict" not in goals:
            goals.append("macro_family_focus")

    seen, suggestions = set(), []
    for goal in goals:
        s = catalog.get(goal)
        if s and s.variant_id not in seen:
            seen.add(s.variant_id)
            suggestions.append(s)
    suggestions = suggestions[:3]  # top 3, bounded

    # Consecutive-stop clustering: reserve a slot for the (non-auto-runnable)
    # protection proposal — strategy-agnostic, so it is not in the CATALOG.
    if "consecutive_stops" in tags:
        suggestions = suggestions[:2] + [
            _protection_suggestion(strategy, record.get("gates", "standard"))
        ]

    return Diagnosis(
        strategy, symbol, healthy=False,
        failure_tags=tuple(sorted(tags)), notes=tuple(notes),
        suggestions=tuple(suggestions),
    )
