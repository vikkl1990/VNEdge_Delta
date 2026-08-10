"""Open-source trading bot inspiration coverage matrix.

The linked repos are useful as design-pattern references, not as strategy code
to copy.  This report maps their durable ideas to VNEDGE-owned research lanes,
marks coverage, and proposes the next safe build where there is a gap.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile


PUBLIC_BOT_INSPIRATION_ID = "public_bot_inspiration_v1"
DEFAULT_OUT = Path("research/live_research/public_bot_inspiration_latest.json")
DEFAULT_FEED = Path("research/live_research/public_bot_inspiration_feed.jsonl")


@dataclass(frozen=True)
class BotSource:
    name: str
    url: str
    category: str
    useful_pattern: str
    caution: str


@dataclass(frozen=True)
class PatternCoverage:
    pattern_id: str
    pattern: str
    source_names: tuple[str, ...]
    current_status: str
    vnedge_surfaces: tuple[str, ...]
    missing_piece: str
    proposed_build: str
    priority_score: int
    safety_gate: tuple[str, ...]
    can_trade: bool = False
    can_promote: bool = False


SOURCES: tuple[BotSource, ...] = (
    BotSource(
        "Zenbot",
        "https://github.com/DeviaVir/zenbot",
        "bot_framework",
        "strategy plugin contract, paper/live separation, genetic backtester, "
        "configurable stops",
        "archived; many TA strategies are old and not evidence of edge",
    ),
    BotSource(
        "Magic8Bot",
        "https://github.com/magic8bot/magic8bot",
        "bot_framework",
        "period-update versus period-close event separation and stateless "
        "microservice decomposition",
        "microservices are too heavy for VNEDGE v1 execution",
    ),
    BotSource(
        "Gekko",
        "https://github.com/askmike/gekko",
        "bot_framework",
        "warmup discipline, advice events, paper trader, performance analyzer",
        "unmaintained; spot-era assumptions do not map to perps directly",
    ),
    BotSource(
        "Freqtrade",
        "https://github.com/freqtrade/freqtrade",
        "bot_framework",
        "pairlist protections, hyperopt, recursive/lookahead diagnostics, "
        "FreqAI lifecycle",
        "broad framework; VNEDGE keeps its own execution/risk path",
    ),
    BotSource(
        "WolfBot",
        "https://github.com/Ekliptor/WolfBot",
        "bot_framework",
        "strategy events chained across candle sizes, realtime trade updates, "
        "maker-fee order strategy, partial take-profit",
        "GPL/commercial mixed surface; use concepts only",
    ),
    BotSource(
        "BitProphet",
        "https://github.com/andresilvasantos/bitprophet",
        "bot_framework",
        "Telegram/Discord operational commands and per-strategy paper toggles",
        "Binance-only, old Node stack",
    ),
    BotSource(
        "PHPTradingBot",
        "https://github.com/kavehs87/PHPTradingBot",
        "bot_framework",
        "modular Laravel UI/database orientation",
        "old PHP stack; no execution advantage for VNEDGE",
    ),
    BotSource(
        "Superalgos",
        "https://github.com/Superalgos/Superalgos",
        "bot_framework",
        "visual data-mining and strategy design workspace",
        "large platform; do not import architecture wholesale",
    ),
    BotSource(
        "Freqtrade Strategies",
        "https://github.com/freqtrade/freqtrade-strategies",
        "strategy_collection",
        "benchmarkable public strategy zoo with ROI/stop metadata",
        "educational backtests only; no direct copy",
    ),
    BotSource(
        "Berlinguyinca Freqtrade Strategies",
        "https://github.com/freqtrade/freqtrade-strategies/tree/master/"
        "user_data/strategies/berlinguyinca",
        "strategy_collection",
        "1m/5m BB/RSI/ADX scalp templates and BinHV/Cluc-style bounce motifs",
        "old spot long-only examples; must be rebuilt causally",
    ),
    BotSource(
        "Gekko Strategies",
        "https://github.com/xFFFFF/Gekko-Strategies",
        "strategy_collection",
        "strategy benchmark database and many indicator combinations",
        "historical results are stale and mostly negative",
    ),
    BotSource(
        "Mynt Strategies",
        "https://github.com/sthewissen/Mynt/tree/master/src/Mynt.Core/Strategies",
        "strategy_collection",
        "compact C# strategy zoo: BB/RSI, ADX/momentum, scalpers, bear-bull",
        "educational, 1h-oriented, not crypto-perp execution aware",
    ),
    BotSource(
        "WolfBot Strategies",
        "https://github.com/Ekliptor/WolfBot/tree/master/src/Strategies",
        "strategy_collection",
        "volume/price spike detectors, RSI scalper, maker-fee order, partial TP, OI monitor",
        "concepts useful, code should not be copied",
    ),
    BotSource(
        "Superalgos BTC WeakHandsBuster",
        "https://github.com/Superalgos/Strategy-BTC-WeakHandsBuster",
        "strategy_collection",
        "weak-hands capitulation / panic-reversal concept",
        "repo unavailable in clone; treat name as concept only",
    ),
    BotSource(
        "Superalgos BTC BB Top Bounce",
        "https://github.com/Superalgos/Strategy-BTC-BB-Top-Bounce",
        "strategy_collection",
        "Bollinger top/bounce mean-reversion concept",
        "repo unavailable in clone; treat name as concept only",
    ),
)


PATTERNS: tuple[PatternCoverage, ...] = (
    PatternCoverage(
        "event_separated_signal_contract",
        "separate in-candle feature updates from closed-candle signal decisions",
        ("Magic8Bot", "Gekko", "Zenbot"),
        "partial",
        ("vnedge.runtime.live_paper", "vnedge.strategy.*"),
        "scanner outputs do not yet expose a uniform calculate/on_close lifecycle audit",
        "strategy_lifecycle_contract_audit_v1",
        78,
        ("no live path changes", "causality test per strategy", "dashboard lifecycle badge"),
    ),
    PatternCoverage(
        "warmup_and_preroll_discipline",
        "explicit strategy warmup/preroll before signals are eligible",
        ("Gekko", "Magic8Bot", "Zenbot"),
        "partial",
        ("vnedge.backtest.walk_forward", "vnedge.strategy.indicators"),
        "warmup is implemented by indicators/windows, but not surfaced as a lane reason",
        "warmup_reason_codes_v1",
        82,
        ("no signals during warmup", "why-no-trade reason every lane"),
    ),
    PatternCoverage(
        "mtf_zoom_in_event_chain",
        "chain higher-timeframe context into lower-timeframe trigger lanes",
        ("WolfBot", "Freqtrade Strategies", "Gekko Strategies"),
        "partial",
        ("vnedge.strategy.quant_signal_pack", "vnedge.strategy.alpha_stack"),
        "some strategies use context, but the lane funnel does not show HTF->trigger chain health",
        "mtf_chain_health_report_v1",
        90,
        ("4h/1h/15m/5m/1m causality", "same-candle reentry guard", "paper/shadow only"),
    ),
    PatternCoverage(
        "scalp_bounce_template_zoo",
        "BB/RSI/ADX/EMA scalp-bounce families rebuilt as owned causal atoms",
        ("Berlinguyinca Freqtrade Strategies", "Mynt Strategies", "Gekko Strategies"),
        "partial",
        ("vnedge.strategy.quant_signal_pack", "vnedge.research.public_indicator_uplift"),
        "public scalp templates are inventoried but not scored as a family coverage matrix",
        "public_strategy_family_miner_v1",
        86,
        ("walk-forward after fees", "no copied code", "untouched judgment"),
    ),
    PatternCoverage(
        "volume_price_spike_events",
        "detect rare price/volume shock events with history comparison and direction filter",
        ("WolfBot Strategies", "Gekko Strategies"),
        "covered",
        (
            "vnedge.research.event_taker_replay",
            "vnedge.research.cascade_reversion",
            "vnedge.research.alpha_factory",
        ),
        "coverage exists; next value is better UI attribution of why spikes are blocked",
        "event_spike_blocker_breakdown_v1",
        72,
        ("replay first", "fee wall proof", "shadow-only before paper"),
    ),
    PatternCoverage(
        "maker_fee_order_and_taker_fallback",
        "maker-first execution with taker fallback only when edge clears fees",
        ("WolfBot Strategies", "Zenbot", "Freqtrade"),
        "covered",
        ("vnedge.execution.maker_taker_executor", "vnedge.scalping.parameter_registry"),
        "core exists; monitor needs route-level realized fill quality",
        "maker_taker_route_quality_panel_v1",
        74,
        ("risk gateway mandatory", "taker buffer after fees", "journal every fallback"),
    ),
    PatternCoverage(
        "partial_take_profit_trailing",
        "partial TP, BE after TP1, trailing stop as primary exit",
        ("WolfBot Strategies", "Freqtrade Strategies", "Mynt Strategies"),
        "partial",
        ("vnedge.strategy.sats_5m_scalper", "vnedge.runtime.live_paper"),
        "exit intelligence is present in strategies but not audited as a cross-lane outcome",
        "exit_quality_scorecard_v1",
        88,
        ("reduce-only exits never blocked", "shadow outcome labels", "paper review after evidence"),
    ),
    PatternCoverage(
        "open_interest_funding_monitor",
        "perp-specific OI/funding changes as context or event triggers",
        ("WolfBot Strategies", "Freqtrade", "Zenbot"),
        "partial",
        ("vnedge.data.funding_ingestor", "vnedge.strategy.funding_mean_reversion"),
        "funding exists; OI coverage is exchange-limited and not a visible lane blocker",
        "oi_funding_context_lane_v1",
        84,
        ("OI data-quality gate", "funding fee accounting", "no OI-only live entries"),
    ),
    PatternCoverage(
        "genetic_and_hyperopt_tournaments",
        "bounded parameter tournaments that persist all tested configs and results",
        ("Zenbot", "Freqtrade", "Gekko Strategies"),
        "partial",
        ("vnedge.research.auto_explore", "vnedge.research.factor_ranker"),
        "auto-explore is bounded, but there is no durable public-style benchmark table",
        "research_result_index_v1",
        80,
        ("burn registry", "multiple-comparison cap", "no seen-window promotion"),
    ),
    PatternCoverage(
        "strategy_benchmark_database",
        "public-style benchmark table across strategy/pair/timeframe datasets",
        ("Gekko Strategies", "Freqtrade Strategies", "Zenbot"),
        "gap",
        ("research/live_research/feed.jsonl",),
        "feed is append-only but not indexed for fast strategy-family comparisons",
        "strategy_benchmark_index_v1",
        92,
        ("store every failed run", "rank by untouched/paper evidence first", "UI table"),
    ),
    PatternCoverage(
        "operator_chat_control_surface",
        "Telegram/Discord command surface for strategy status and paper toggles",
        ("BitProphet", "Freqtrade", "Zenbot"),
        "partial",
        ("vnedge.monitoring.notifiers",),
        "alerts exist, but command/control remains intentionally absent",
        "read_only_operator_commands_v1",
        62,
        ("read-only first", "no start/stop/live commands", "token/audit log"),
    ),
    PatternCoverage(
        "visual_strategy_workspace",
        "visual strategy/data-mining workspace showing data lineage and state",
        ("Superalgos", "Gekko", "Freqtrade"),
        "partial",
        ("vnedge.dashboard", "vnedge.research.alpha_workbench"),
        "cockpit still needs lineage-first strategy graph instead of card-only summaries",
        "strategy_lineage_workspace_v1",
        76,
        ("read-only dashboard", "artifact provenance", "no browser control routes"),
    ),
    PatternCoverage(
        "capitulation_reversal_and_bb_top_bounce",
        "weak-hands panic reversal and Bollinger top/bounce concepts",
        ("Superalgos BTC WeakHandsBuster", "Superalgos BTC BB Top Bounce", "Freqtrade Strategies"),
        "covered",
        ("vnedge.strategy.panic_reversal", "vnedge.strategy.quant_signal_pack"),
        "concept exists; recent data may show sparse events, so tune only via "
        "pre-registered windows",
        "sparse_reversal_judgment_queue_v1",
        70,
        ("sparse strategy gates", "untouched judgment", "shadow only after approval"),
    ),
)


def run_public_bot_inspiration(*, now: datetime | None = None) -> dict:
    generated = now or datetime.now(UTC)
    rows = [_with_runtime_status(row) for row in PATTERNS]
    rows.sort(key=lambda row: (-row["priority_score"], row["pattern_id"]))
    status_counts: dict[str, int] = {}
    for row in rows:
        status_counts[row["current_status"]] = status_counts.get(row["current_status"], 0) + 1
    return {
        "audit_id": PUBLIC_BOT_INSPIRATION_ID,
        "generated_at": generated.isoformat(),
        "source_scope": {
            "mode": "public_repos_and_readmes_only",
            "links_reviewed": [asdict(source) for source in SOURCES],
            "no_strategy_code_copied": True,
            "not_profit_evidence": True,
        },
        "policy": {
            "research_only": True,
            "can_trade": False,
            "can_promote": False,
            "requires_owned_reimplementation": True,
            "requires_backtest_after_fees": True,
            "requires_untouched_judgment": True,
            "requires_human_approval": True,
        },
        "summary": {
            "sources_reviewed": len(SOURCES),
            "patterns_reviewed": len(rows),
            "status_counts": status_counts,
            "highest_priority": rows[0]["pattern_id"] if rows else None,
            "top_gaps": [
                row["pattern_id"]
                for row in rows
                if row["runtime_status"] in {"gap", "partial"}
            ][:5],
        },
        "top_adaptations": rows[:8],
        "coverage_matrix": rows,
        "can_trade": False,
        "can_promote": False,
    }


def publish_public_bot_inspiration(
    payload: dict,
    out: Path | str = DEFAULT_OUT,
    feed: Path | str = DEFAULT_FEED,
) -> Path:
    out_path = Path(out)
    feed_path = Path(feed)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    with NamedTemporaryFile(
        "w",
        dir=out_path.parent,
        prefix=out_path.name,
        suffix=".tmp",
        delete=False,
    ) as tmp:
        tmp.write(encoded)
        tmp_path = Path(tmp.name)
    tmp_path.replace(out_path)
    feed_path.parent.mkdir(parents=True, exist_ok=True)
    with feed_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
    return out_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="public bot inspiration coverage matrix")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--feed", default=str(DEFAULT_FEED))
    parser.add_argument("--no-write", action="store_true", help="print JSON only")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    payload = run_public_bot_inspiration()
    if args.no_write:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    path = publish_public_bot_inspiration(payload, args.out, args.feed)
    print(path)
    return 0


def _with_runtime_status(row: PatternCoverage) -> dict:
    data = asdict(row)
    resolved = [_module_exists(surface) for surface in row.vnedge_surfaces]
    if any(resolved):
        runtime_status = row.current_status
    else:
        runtime_status = "gap"
    data["runtime_status"] = runtime_status
    data["resolved_surfaces"] = [
        surface
        for surface, exists in zip(row.vnedge_surfaces, resolved, strict=True)
        if exists
    ]
    return data


def _module_exists(surface: str) -> bool:
    if surface.endswith(".*"):
        prefix = surface.removeprefix("vnedge.").removesuffix(".*").replace(".", "/")
        return (Path("src/vnedge") / prefix.split("/", maxsplit=1)[-1]).exists()
    if surface.startswith("research/"):
        return Path(surface).exists()
    if not surface.startswith("vnedge."):
        return False
    return importlib.util.find_spec(surface) is not None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
