"""Public indicator uplift audit for Willy/Lux-style concept stacks.

This module is deliberately research-only.  It maps public indicator names and
descriptions into VNEDGE-owned causal atoms, then identifies what is already
covered, what can be mined next, and what should stay out of the trading path.
It never copies Pine/TradingView logic and never grants trading permission.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Iterable

from vnedge.strategy.alpha_distillation_pack import FEATURE_ATOMS, concept_inventory


PUBLIC_INDICATOR_UPLIFT_ID = "public_indicator_uplift_v1"
DEFAULT_OUT = Path("research/live_research/public_indicator_uplift_latest.json")
DEFAULT_FEED = Path("research/live_research/public_indicator_uplift_feed.jsonl")

WILLY_PUBLIC_SOURCES = (
    "https://www.tradingview.com/u/WillyAlgoTrader/#published-scripts",
    "https://willyalgotrader.pro/bot/#setup",
    "https://www.tradingview.com/script/IZj18oYZ-Precision-Sniper-WillyAlgoTrader/",
    "https://www.tradingview.com/script/javzHA1B-Smart-Breakout-Targets-WillyAlgoTrader/",
    "https://www.tradingview.com/script/yq0wMzfY-Meridian-Flow-SMC-with-R-R-Levels-WillyAlgoTrader/",
    "https://www.tradingview.com/script/CRGVe4mg-Volume-Weighted-S-R-Zones-WillyAlgoTrader/",
    "https://www.tradingview.com/script/p7KgQ2NJ-Smart-Technical-Analysis-System-SATS-WillyAlgoTrader/",
    "https://www.tradingview.com/script/YhEU2620-Premium-Fibonacci-Levels-WillyAlgoTrader/",
    "https://www.tradingview.com/script/i6YsV99R-Adaptive-Volume-Trend-AVT-WillyAlgoTrader/",
    "https://www.tradingview.com/script/s1iJ9s7h-Adaptive-Trend-Flow-WillyAlgoTrader/",
    "https://www.tradingview.com/script/eqXNjWLn-Liquidity-Pools-and-Sweep-Detector-WillyAlgoTrader/",
    "https://www.tradingview.com/script/JaNZMn0x-Smart-Fair-Value-Gap-Detector-WillyAlgoTrader/",
    "https://www.tradingview.com/script/i9U1sX7R-Adaptive-Squeeze-Momentum-Detector-WillyAlgoTrader/",
)

ATOM_MODULES = {
    "liquidity_sweep": (
        "vnedge.strategy.quant_signal_pack",
        "vnedge.strategy.alpha_stack",
        "vnedge.strategy.liquidity_pools",
    ),
    "fvg_retest": (
        "vnedge.strategy.quant_signal_pack",
        "vnedge.strategy.alpha_stack",
        "vnedge.strategy.alpha_distillation_pack",
    ),
    "order_block": ("vnedge.strategy.quant_signal_pack", "vnedge.strategy.alpha_stack"),
    "squeeze_release": (
        "vnedge.strategy.quant_signal_pack",
        "vnedge.strategy.vol_expansion_breakout",
        "vnedge.strategy.funding_squeeze_continuation",
    ),
    "vwap_reclaim": ("vnedge.strategy.quant_signal_pack",),
    "structure_break": (
        "vnedge.strategy.quant_signal_pack",
        "vnedge.strategy.alpha_stack",
        "vnedge.strategy.trend_continuation",
    ),
    "trend_trail": (
        "vnedge.strategy.alpha_distillation_pack",
        "vnedge.strategy.trend_retest",
    ),
    "profile_reclaim": (
        "vnedge.strategy.alpha_distillation_pack",
        "vnedge.strategy.volume_profile",
    ),
    "momentum_impulse": (
        "vnedge.strategy.alpha_distillation_pack",
        "vnedge.strategy.scalper_1m",
    ),
    "oscillator_divergence": ("vnedge.strategy.alpha_distillation_pack",),
    "net_volume_flow": (
        "vnedge.strategy.alpha_distillation_pack",
        "vnedge.research.orderflow_footprint",
    ),
    "activity_zone_reclaim": (
        "vnedge.strategy.alpha_distillation_pack",
        "vnedge.strategy.volume_profile",
    ),
}


@dataclass(frozen=True)
class UpliftAssessment:
    concept: str
    vendor_family: str
    atom: str
    role: str
    current_coverage: str
    uplift_route: str
    missing_piece: str
    proposed_build: str
    data_required: tuple[str, ...]
    gate_before_shadow: tuple[str, ...]
    priority_score: int
    can_trade: bool = False
    can_promote: bool = False


def run_public_indicator_uplift(
    *,
    vendor_family: str = "WillyAlgo",
    now: datetime | None = None,
) -> dict:
    """Return the public-indicator uplift report.

    ``vendor_family`` defaults to WillyAlgo because the operator asked for that
    pass explicitly. Passing ``all`` keeps the Lux-style concepts from the same
    inventory in the report.
    """
    generated = now or datetime.now(UTC)
    concepts = _concepts_for_vendor(vendor_family)
    assessments = [_assess_concept(row) for row in concepts]
    routes = _route_counts(assessments)
    top = sorted(assessments, key=lambda row: row.priority_score, reverse=True)[:8]
    return {
        "audit_id": PUBLIC_INDICATOR_UPLIFT_ID,
        "generated_at": generated.isoformat(),
        "source_scope": {
            "mode": "public_descriptions_only",
            "vendor_family": vendor_family,
            "willy_public_sources": list(WILLY_PUBLIC_SOURCES),
            "not_profit_evidence": True,
            "no_pine_or_proprietary_logic_copied": True,
        },
        "policy": {
            "research_only": True,
            "can_trade": False,
            "can_promote": False,
            "requires_backtest": True,
            "requires_untouched_judgment": True,
            "requires_human_approval": True,
        },
        "summary": {
            "concepts_reviewed": len(assessments),
            "feature_atoms": len(FEATURE_ATOMS),
            "routes": routes,
            "high_priority_uplifts": sum(1 for row in assessments if row.priority_score >= 80),
            "highest_value_missing_pieces": _highest_value_missing_pieces(),
        },
        "coverage_matrix": _coverage_matrix(assessments),
        "top_uplifts": [asdict(row) for row in top],
        "assessments": [asdict(row) for row in assessments],
        "can_trade": False,
        "can_promote": False,
    }


def publish_public_indicator_uplift(
    payload: dict,
    out: Path | str = DEFAULT_OUT,
    feed: Path | str = DEFAULT_FEED,
) -> Path:
    out_path = Path(out)
    feed_path = Path(feed)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    with NamedTemporaryFile("w", dir=out_path.parent, prefix=out_path.name, suffix=".tmp",
                            delete=False) as tmp:
        tmp.write(encoded)
        tmp_path = Path(tmp.name)
    tmp_path.chmod(0o644)
    tmp_path.replace(out_path)
    out_path.chmod(0o644)
    feed_path.parent.mkdir(parents=True, exist_ok=True)
    with feed_path.open("a") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
    feed_path.chmod(0o644)
    return out_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="public indicator uplift audit")
    parser.add_argument("--vendor", default="WillyAlgo",
                        help="vendor_family to include; use 'all' for full inventory")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--feed", default=str(DEFAULT_FEED))
    parser.add_argument("--no-write", action="store_true", help="print JSON only")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    payload = run_public_indicator_uplift(vendor_family=args.vendor)
    if args.no_write:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    path = publish_public_indicator_uplift(payload, args.out, args.feed)
    print(path)
    return 0


def _concepts_for_vendor(vendor_family: str) -> list[dict]:
    inventory = concept_inventory()
    if vendor_family.lower() == "all":
        return inventory
    return [row for row in inventory if row["vendor_family"] == vendor_family]


def _assess_concept(row: dict) -> UpliftAssessment:
    concept = str(row["name"])
    atom = str(row["atom"])
    role = str(row["role"])
    priority = int(row["priority"])
    route, missing_piece, proposed_build, data_required, gates, base_score = _route_for(concept, atom)
    coverage = (
        "covered_by_causal_atoms"
        if atom in ATOM_MODULES
        else "not_covered_by_current_atoms"
    )
    return UpliftAssessment(
        concept=concept,
        vendor_family=str(row["vendor_family"]),
        atom=atom,
        role=role,
        current_coverage=coverage,
        uplift_route=route,
        missing_piece=missing_piece,
        proposed_build=proposed_build,
        data_required=data_required,
        gate_before_shadow=gates,
        priority_score=max(0, base_score - (priority - 1) * 6),
    )


def _route_for(
    concept: str,
    atom: str,
) -> tuple[str, str, str, tuple[str, ...], tuple[str, ...], int]:
    name = concept.lower()
    if "fvg" in name or atom == "fvg_retest":
        return (
            "ADD_STATEFUL_IMBALANCE_LIFECYCLE",
            "FVG is detected, but age, mitigation %, inverse/double-FVG state, "
            "and distance-to-zone are not first-class mining dimensions.",
            "imbalance_lifecycle_miner_v1",
            ("15m/1m candles", "L2 replay for fill quality"),
            ("walk_forward_after_fees", "untouched_judgment", "shadow_fill_parity"),
            96,
        )
    if "volume-weighted" in name or "volume profile" in name or atom == "profile_reclaim":
        return (
            "ADD_VOLUME_ZONE_REACTION_QUALITY",
            "VWAP/profile reclaim exists, but the bot does not yet score reaction "
            "strength, retest count, or room-to-next-volume-zone.",
            "volume_zone_reaction_miner_v1",
            ("15m/1m candles", "public trades for CVD confirmation"),
            ("walk_forward_after_fees", "negative-control zones", "shadow_fill_parity"),
            93,
        )
    if "fib" in name or "fibonacci" in name or "harmonic" in name or "abcd" in name:
        return (
            "ADD_FIB_CONTEXT_TAG_ONLY",
            "Fib/harmonic levels are useful location context, but too easy to "
            "overfit as entry triggers.",
            "fib_context_confluence_v1",
            ("4h/1h/15m candles",),
            ("feature_ablation", "walk_forward_after_fees", "untouched_judgment"),
            82,
        )
    if "breakout" in name or "calculator" in name or atom == "structure_break":
        return (
            "ADD_TARGET_ROOM_AND_BREAKOUT_QUALITY",
            "Structure breaks exist, but target ladder, false-break risk, and "
            "room-to-next-liquidity are not yet explicit scanner gates.",
            "breakout_room_quality_miner_v1",
            ("4h/1h/15m/1m candles", "L2 spread/liquidity snapshot"),
            ("walk_forward_after_fees", "same-symbol negative controls", "shadow_drawdown_gate"),
            88,
        )
    if "liquidity" in name or atom == "liquidity_sweep":
        return (
            "EXTEND_LIQUIDITY_POOL_LIFECYCLE",
            "Sweeps and pool strength exist, but prior day/week/month levels and "
            "multi-touch decay are still incomplete.",
            "liquidity_pool_lifecycle_v2",
            ("4h/1h/15m/1m candles", "public trades for sweep participation"),
            ("walk_forward_after_fees", "untouched_judgment", "replay_fill_check"),
            91,
        )
    if "squeeze" in name or "volatility" in name or atom == "squeeze_release":
        return (
            "ADD_ADAPTIVE_COMPRESSION_RELEASE_QUALITY",
            "Squeeze release exists, but compression duration, expansion delta, "
            "and volume-flow quality should be mined as separate features.",
            "adaptive_squeeze_quality_v1",
            ("15m/1m candles", "public trades for participation"),
            ("walk_forward_after_fees", "regime_split", "untouched_judgment"),
            86,
        )
    if "trend" in name or "trail" in name or "cloud" in name or atom == "trend_trail":
        return (
            "EXIT_AND_PERMISSION_UPLIFT",
            "Trend trail is covered as context, but live partial exits, breakeven "
            "promotion, and runner trailing are not fully wired.",
            "live_exit_ladder_v1",
            ("shadow fills", "paper fills", "15m/1m candles"),
            ("paper_only_first", "reduce_only_exit_safety", "no_entry_gate_bypass"),
            79,
        )
    if "momentum" in name or "flow" in name or "sniper" in name or atom == "momentum_impulse":
        return (
            "ADD_ADAPTIVE_FLOW_QUALITY",
            "Momentum impulse exists, but should be separated into fresh-flow "
            "continuation versus late-flow exhaustion.",
            "adaptive_flow_quality_v1",
            ("public trades", "15m/1m candles", "orderflow footprint"),
            ("feature_ablation", "walk_forward_after_fees", "untouched_judgment"),
            85,
        )
    return (
        "ALREADY_COVERED_RESEARCH_ONLY",
        "The concept is represented by existing causal atoms; no direct uplift "
        "until ablation proves incremental edge.",
        "no_build_until_ablation",
        ("existing research reports",),
        ("feature_ablation",),
        55,
    )


def _coverage_matrix(assessments: Iterable[UpliftAssessment]) -> dict:
    matrix: dict[str, dict] = {}
    for atom in FEATURE_ATOMS:
        rows = [row for row in assessments if row.atom == atom]
        matrix[atom] = {
            "concepts": [row.concept for row in rows],
            "concept_count": len(rows),
            "existing_modules": list(ATOM_MODULES.get(atom, ())),
            "top_route": rows[0].uplift_route if rows else "NO_VENDOR_CONCEPT",
        }
    return matrix


def _route_counts(assessments: Iterable[UpliftAssessment]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in assessments:
        counts[row.uplift_route] = counts.get(row.uplift_route, 0) + 1
    return dict(sorted(counts.items()))


def _highest_value_missing_pieces() -> tuple[str, ...]:
    return (
        "stateful FVG/order-block/liquidity-zone lifecycle with age and mitigation",
        "volume-weighted support/resistance reaction quality and room-to-next-zone",
        "fib/harmonic levels as context tags, not standalone entries",
        "adaptive fresh-flow versus late-flow exhaustion split",
        "breakout target-room and false-break quality gates",
        "live exit ladder: partial TP, breakeven, trailing runner",
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
