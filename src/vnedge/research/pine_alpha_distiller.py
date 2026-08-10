"""Source-backed Pine alpha distiller.

The Pine Research Lab answers "is source available?"  This module answers the
next question: "what VNEDGE-owned research task should source-backed scripts
be distilled into?"

It is intentionally research-only.  It reads local Pine source artifacts only
to derive hashes, primitive families, and risk flags.  It never emits Pine code
or grants trading permission.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import hashlib
import json
import math
from pathlib import Path
import re
from statistics import median
from tempfile import NamedTemporaryFile
import time
from typing import Iterable

from vnedge.research.pine_script_research import (
    DEFAULT_PINE_KB_PATH,
    DEFAULT_PINE_SOURCE_DIR,
    discover_pine_source_files,
    load_pine_research_payload,
)


PINE_ALPHA_DISTILLER_ID = "pine_alpha_distiller_v1"
DEFAULT_OUT = Path("research/live_research/pine_alpha_distiller_latest.json")
DEFAULT_FEED = Path("research/live_research/pine_alpha_distiller_feed.jsonl")
SOURCE_BACKED_PORTABLE = {"PORTABLE", "PORTABLE_WITH_CHANGES"}
REPLAY_TIMEFRAMES = ("5m", "15m", "1h", "4h")
REPLAY_VENUES = ("binanceusdm", "bybit", "delta_india")
PROMOTION_GATES = {
    "expected_net_edge_bps": "> 25",
    "profit_factor": "> 1.5",
    "minimum_historical_trades": ">= 20",
    "taker_route": "only if expected move pays fees, slippage, and safety buffer",
    "judgment": "pre-registered untouched window before paper or shadow promotion",
}


@dataclass(frozen=True)
class SourceArtifact:
    sha256: str
    lines: int
    path_name: str
    text: str


@dataclass(frozen=True)
class ScriptIntention:
    intent_family: str
    trading_thesis: str
    context: str
    setup: str
    trigger: str
    execution_bias: str
    exit_plan: str
    bot_use: str
    backtest_recipe: str
    portable_atoms: tuple[str, ...]
    non_portable_atoms: tuple[str, ...]
    uplift_questions: tuple[str, ...]


@dataclass(frozen=True)
class ScriptDistillation:
    script_id: str
    title: str
    url: str
    kind: str
    source_sha256: str
    source_lines: int
    source_path_name: str
    crypto_portability: str
    crypto_fit_score: int
    priority_score: int
    mechanism: str
    intention: ScriptIntention
    primitives: tuple[str, ...]
    risks: tuple[str, ...]
    action: str
    recommended_port: str
    distillation_score: int
    source_policy: str = "hash_only_no_pine_code_emitted"
    can_trade: bool = False
    can_promote: bool = False


@dataclass(frozen=True)
class PrimitiveFamily:
    primitive: str
    script_count: int
    recommended_port: str
    required_data: tuple[str, ...]
    gate_before_shadow: tuple[str, ...]
    top_scripts: tuple[dict, ...]


@dataclass(frozen=True)
class PortTask:
    task_id: str
    recommended_port: str
    source_count: int
    source_script_ids: tuple[str, ...]
    primitive_stack: tuple[str, ...]
    median_fit_score: float
    distillation_score: int
    risks: tuple[str, ...]
    required_data: tuple[str, ...]
    first_replay: str
    gate_before_shadow: tuple[str, ...]
    can_trade: bool = False
    can_promote: bool = False


@dataclass(frozen=True)
class IntentionCluster:
    intent_family: str
    source_count: int
    port_candidates: int
    quarantine_count: int
    recommended_port: str
    median_fit_score: float
    playbook: str
    bot_value: str
    first_backtest: str
    top_scripts: tuple[dict, ...]
    blocker_summary: tuple[str, ...]
    can_trade: bool = False
    can_promote: bool = False


PRIMITIVE_PATTERNS: dict[str, tuple[str, ...]] = {
    "liquidity_zone": (
        r"\bfvg\b",
        r"fair\s+value\s+gap",
        r"order\s+block",
        r"supply",
        r"demand",
        r"liquidity",
        r"imbalance",
        r"premium",
        r"discount",
        r"golden\s+zone",
        r"fib(?:onacci)?",
        r"support",
        r"resistance",
        r"\bs/r\b",
        r"\bsr\b",
    ),
    "sweep_reclaim": (
        r"sweep",
        r"liquidity\s+grab",
        r"stop\s+hunt",
        r"wick",
        r"reclaim",
        r"\bchoch\b",
        r"\bmss\b",
        r"market\s+structure\s+shift",
    ),
    "range_breakout": (
        r"breakout",
        r"break\s+out",
        r"range",
        r"\bbox\b",
        r"consolidation",
        r"compression",
        r"squeeze",
        r"opening\s+range",
        r"session",
    ),
    "trend_trail": (
        r"supertrend",
        r"\btrail",
        r"chandelier",
        r"atr\s+stop",
        r"\but\b",
        r"qtm",
        r"kalman",
        r"parabolic",
        r"\bema\b",
        r"trend",
    ),
    "momentum_confirm": (
        r"\brsi\b",
        r"\bmacd\b",
        r"stoch",
        r"\bbbp\b",
        r"bull\s+bear",
        r"momentum",
        r"\badx\b",
        r"\ber\b",
        r"\broc\b",
        r"divergence",
        r"oscillator",
    ),
    "volume_participation": (
        r"volume",
        r"\bvwap\b",
        r"\bcvd\b",
        r"cumulative\s+volume\s+delta",
        r"footprint",
        r"absorption",
        r"\bmfi\b",
        r"\bobv\b",
        r"profile",
        r"delta",
    ),
    "risk_plan": (
        r"\btp\b",
        r"\bsl\b",
        r"stop\s+loss",
        r"take\s+profit",
        r"target",
        r"\brr\b",
        r"risk\s+reward",
        r"breakeven",
        r"partial",
        r"trailing\s+stop",
        r"strategy\.exit",
        r"stop\s*=",
        r"limit\s*=",
    ),
    "mtf_bias": (
        r"request\.security",
        r"\bmtf\b",
        r"multi\s+tf",
        r"multi[-\s]?timeframe",
        r"higher\s+time",
        r"\b1h\b",
        r"\b4h\b",
    ),
}

RISK_PATTERNS: dict[str, tuple[str, ...]] = {
    "lookahead_on": (r"lookahead\s*=\s*barmerge\.lookahead_on", r"lookahead_on"),
    "last_bar_display_state": (r"barstate\.islast", r"barstate\.islastconfirmedhistory"),
    "visual_overlay_state": (r"label\.new", r"plotshape", r"table\.new", r"box\.new", r"line\.new"),
    "fixed_session_review": (r"input\.session", r"\bsession\.", r"\btime\("),
    "forecast_not_signal": (r"forecast", r"prediction", r"future", r"projection"),
}


def run_pine_alpha_distiller(
    *,
    kb_path: Path | str | None = DEFAULT_PINE_KB_PATH,
    source_dir: Path | str | None = DEFAULT_PINE_SOURCE_DIR,
    source_files: Iterable[Path | str] = (),
    max_scripts: int | None = None,
    include_repaint: bool = True,
    now: datetime | None = None,
) -> dict:
    """Build a research-only primitive/task report from source-backed Pine."""

    generated = now or datetime.now(UTC)
    kb = load_pine_research_payload(Path(kb_path) if kb_path is not None else None)
    source_index = build_source_index(source_dir=source_dir, source_files=source_files)
    records = _source_backed_records(kb.get("records", ()), include_repaint=include_repaint)
    if max_scripts is not None:
        records = records[:max_scripts]
    distillations = [
        _distill_script(row, source_index)
        for row in records
        if _source_for_row(row, source_index) is not None
    ]
    primitive_families = _primitive_families(distillations)
    intention_clusters = _intention_clusters(distillations)
    port_tasks = _port_tasks(distillations)
    return {
        "distiller_id": PINE_ALPHA_DISTILLER_ID,
        "generated_at": generated.isoformat(),
        "source": str(kb_path or "default_seed"),
        "source_scope": {
            "source_backed_only": True,
            "source_files_indexed": len(source_index),
            "kb_records": int(kb.get("summary", {}).get("total") or len(kb.get("records", ()))),
            "include_repaint": include_repaint,
            "no_pine_code_in_output": True,
        },
        "summary": _summary(distillations, primitive_families, intention_clusters, port_tasks),
        "policy": _policy(),
        "promotion_gates": PROMOTION_GATES,
        "intention_clusters": [asdict(row) for row in intention_clusters],
        "primitive_families": [asdict(row) for row in primitive_families],
        "port_tasks": [asdict(row) for row in port_tasks],
        "script_distillations": [asdict(row) for row in distillations],
        "operator_answer": _operator_answer(distillations, port_tasks),
        "can_trade": False,
        "can_promote": False,
    }


def publish_pine_alpha_distiller(
    payload: dict,
    *,
    out: Path | str = DEFAULT_OUT,
    feed: Path | str = DEFAULT_FEED,
) -> Path:
    out_path = Path(out)
    feed_path = Path(feed)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    _assert_no_pine_source_leak(encoded)
    with NamedTemporaryFile(
        "w",
        dir=out_path.parent,
        prefix=out_path.name,
        suffix=".tmp",
        delete=False,
        encoding="utf-8",
    ) as tmp:
        tmp.write(encoded)
        tmp_path = Path(tmp.name)
    tmp_path.chmod(0o644)
    tmp_path.replace(out_path)
    out_path.chmod(0o644)
    feed_path.parent.mkdir(parents=True, exist_ok=True)
    with feed_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
    feed_path.chmod(0o644)
    return out_path


def build_source_index(
    *,
    source_dir: Path | str | None = DEFAULT_PINE_SOURCE_DIR,
    source_files: Iterable[Path | str] = (),
) -> dict[str, SourceArtifact]:
    out: dict[str, SourceArtifact] = {}
    for path in discover_pine_source_files(source_dir, source_files):
        text = path.read_text(encoding="utf-8", errors="replace")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        out.setdefault(
            digest,
            SourceArtifact(
                sha256=digest,
                lines=len(text.splitlines()),
                path_name=path.name,
                text=text,
            ),
        )
    return out


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="distill source-backed Pine corpus")
    parser.add_argument("--kb", default=str(DEFAULT_PINE_KB_PATH))
    parser.add_argument("--source-dir", default=str(DEFAULT_PINE_SOURCE_DIR))
    parser.add_argument("--source-file", action="append", default=[])
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--feed", default=str(DEFAULT_FEED))
    parser.add_argument("--max-scripts", type=int)
    parser.add_argument(
        "--include-repaint",
        action="store_true",
        help="deprecated; repaint-risk rows are included in quarantine by default",
    )
    parser.add_argument(
        "--portable-only",
        action="store_true",
        help="exclude repaint-risk rows and emit only portable port candidates",
    )
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=0,
        help="repeat forever at this cadence; 0 means one-shot publish",
    )
    parser.add_argument("--no-write", action="store_true", help="print JSON only")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    while True:
        payload = run_pine_alpha_distiller(
            kb_path=args.kb,
            source_dir=args.source_dir,
            source_files=args.source_file,
            max_scripts=args.max_scripts,
            include_repaint=args.include_repaint or not args.portable_only,
        )
        if args.no_write:
            encoded = json.dumps(payload, indent=2, sort_keys=True)
            _assert_no_pine_source_leak(encoded)
            print(encoded)
        else:
            path = publish_pine_alpha_distiller(payload, out=args.out, feed=args.feed)
            summary = payload.get("summary", {})
            print(
                "pine alpha distiller published: "
                f"path={path} "
                f"reviewed={summary.get('source_backed_reviewed', 0)} "
                f"clusters={summary.get('intention_clusters', 0)} "
                f"top_intention={summary.get('top_intention_family', '')} "
                f"top_task={summary.get('top_task', '')}",
                flush=True,
            )
        if args.interval_seconds <= 0:
            return 0
        time.sleep(max(30, args.interval_seconds))


def _source_backed_records(records: Iterable[dict], *, include_repaint: bool) -> list[dict]:
    out = [
        dict(row)
        for row in records
        if row.get("source_available")
        and (
            include_repaint
            or str(row.get("crypto_portability") or "") in SOURCE_BACKED_PORTABLE
        )
    ]
    return sorted(
        out,
        key=lambda row: (
            -int(row.get("priority_score") or 0),
            str(row.get("script_id") or ""),
        ),
    )


def _distill_script(row: dict, source_index: dict[str, SourceArtifact]) -> ScriptDistillation:
    artifact = _source_for_row(row, source_index)
    if artifact is None:
        raise ValueError(f"source artifact missing for {row.get('script_id')}")
    text = _analysis_text(row, artifact.text)
    primitives = _detect_primitives(text)
    risks = _detect_risks(text, row)
    recommended_port = _recommended_port(row, primitives, risks)
    action = _action_for(row, recommended_port, risks)
    intention = _intention_for(
        row=row,
        text=text,
        primitives=primitives,
        risks=risks,
        recommended_port=recommended_port,
        action=action,
    )
    return ScriptDistillation(
        script_id=str(row.get("script_id") or ""),
        title=str(row.get("title") or ""),
        url=str(row.get("url") or ""),
        kind=str(row.get("kind") or "unknown"),
        source_sha256=artifact.sha256,
        source_lines=artifact.lines,
        source_path_name=artifact.path_name,
        crypto_portability=str(row.get("crypto_portability") or ""),
        crypto_fit_score=_bounded_int(row.get("crypto_fit_score"), 0, 100),
        priority_score=_bounded_int(row.get("priority_score"), 0, 100),
        mechanism=str(row.get("mechanism") or "general"),
        intention=intention,
        primitives=primitives,
        risks=risks,
        action=action,
        recommended_port=recommended_port,
        distillation_score=_distillation_score(row, primitives, risks, recommended_port),
    )


def _intention_for(
    *,
    row: dict,
    text: str,
    primitives: tuple[str, ...],
    risks: tuple[str, ...],
    recommended_port: str,
    action: str,
) -> ScriptIntention:
    primitive_set = set(primitives)
    risk_set = set(risks)
    family = _intent_family(primitive_set, recommended_port)
    thesis = _trading_thesis(family, primitive_set)
    context = _context_layer(primitive_set, text)
    setup = _setup_layer(family, primitive_set)
    trigger = _trigger_layer(family, primitive_set, text)
    execution_bias = _execution_layer(recommended_port, primitive_set, text)
    exit_plan = _exit_layer(primitive_set, text)
    bot_use = _bot_use(action, recommended_port, family)
    backtest_recipe = _backtest_recipe(recommended_port, family)
    portable_atoms = _portable_atoms(primitive_set, text)
    non_portable_atoms = _non_portable_atoms(risk_set)
    uplift_questions = _uplift_questions(row, family, primitive_set, risk_set)
    return ScriptIntention(
        intent_family=family,
        trading_thesis=thesis,
        context=context,
        setup=setup,
        trigger=trigger,
        execution_bias=execution_bias,
        exit_plan=exit_plan,
        bot_use=bot_use,
        backtest_recipe=backtest_recipe,
        portable_atoms=portable_atoms,
        non_portable_atoms=non_portable_atoms,
        uplift_questions=uplift_questions,
    )


def _intent_family(primitive_set: set[str], recommended_port: str) -> str:
    if recommended_port == "causality_quarantine_v1":
        return "causality_quarantine"
    if recommended_port == "source_feature_library_review_v1":
        return "feature_library"
    if recommended_port == "orderflow_proxy_v1":
        return "orderflow_absorption"
    if recommended_port == "trail_exit_lab_v1":
        return "adaptive_trail_exit"
    if recommended_port == "range_expansion_breakout_v1":
        return "range_expansion_breakout"
    if recommended_port == "fvg_liquidity_breakout_v1":
        if "sweep_reclaim" in primitive_set:
            return "liquidity_sweep_reclaim"
        return "liquidity_zone_breakout"
    if recommended_port == "trend_momentum_context_v1":
        return "trend_momentum_filter"
    if "momentum_confirm" in primitive_set:
        return "momentum_feature_bank"
    return "general_feature_bank"


def _trading_thesis(family: str, primitive_set: set[str]) -> str:
    mapping = {
        "liquidity_sweep_reclaim": (
            "Trade failed liquidity grabs: price sweeps a visible pool, reclaims "
            "structure, then displaces toward the next external-liquidity target."
        ),
        "liquidity_zone_breakout": (
            "Trade from pre-marked imbalance, support/resistance, Fibonacci, or "
            "order-block zones only when price proves acceptance away from the zone."
        ),
        "range_expansion_breakout": (
            "Wait for a compressed range or session box, require participation, "
            "then trade the confirmed expansion rather than every touch."
        ),
        "adaptive_trail_exit": (
            "Use adaptive ATR/supertrend/chandelier-style trails to hold winners, "
            "cut failed momentum quickly, and standardize TP/BE handling."
        ),
        "orderflow_absorption": (
            "Use public-trade pressure, CVD, footprint, and absorption proxies as "
            "a participation filter, not as a naked 1m entry signal."
        ),
        "trend_momentum_filter": (
            "Use trend, momentum, BBP/RSI/MACD/ADX, and volatility alignment as "
            "permission for an existing trigger, not as the trigger by itself."
        ),
        "momentum_feature_bank": (
            "Distill oscillator and momentum-state information into edge-model "
            "features that score whether a trigger has enough follow-through."
        ),
        "feature_library": (
            "Extract reusable causal helper atoms, then attach them to a VNEDGE "
            "scanner; the source is not a standalone trading system."
        ),
        "causality_quarantine": (
            "The visible idea may be useful, but the source has repaint or "
            "unconfirmed-HTF risk that must be rewritten before evidence counts."
        ),
    }
    if family in mapping:
        return mapping[family]
    if "risk_plan" in primitive_set:
        return "Extract the risk-plan logic first, then test it as an exit overlay."
    return "Extract causal features and let the edge model decide whether they add OOS lift."


def _context_layer(primitive_set: set[str], text: str) -> str:
    if "mtf_bias" in primitive_set:
        return "Use closed 1h/4h context only; never read an unfinished higher-timeframe bar."
    if "trend_trail" in primitive_set:
        return "Classify trend and volatility state before allowing lower-timeframe entries."
    if _contains_any(text, ("session", "opening range", "killzone", "new york", "london")):
        return "Normalize session ideas to 24x7 crypto windows and test time-of-day edge separately."
    return "Build context from closed candles, venue liquidity, spread, and fee regime."


def _setup_layer(family: str, primitive_set: set[str]) -> str:
    if family == "liquidity_sweep_reclaim":
        return "Prior swing, equal high/low, FVG/order-block, or range boundary is swept and reclaimed."
    if family == "range_expansion_breakout":
        return "A bounded range forms with falling volatility, then price approaches a clean boundary."
    if family == "adaptive_trail_exit":
        return "An existing entry has enough unrealized room for a dynamic trail and partial-exit ladder."
    if family == "orderflow_absorption":
        return "Trade pressure diverges from price at a visible level or during a displacement candle."
    if "liquidity_zone" in primitive_set:
        return "Pre-compute zones, invalidation buffer, and room-to-liquidity before any trigger."
    if "momentum_confirm" in primitive_set:
        return "Momentum state must agree with trend and volatility instead of firing alone."
    return "Convert the script into causal feature rows before standalone signal testing."


def _trigger_layer(family: str, primitive_set: set[str], text: str) -> str:
    trigger_bits: list[str] = []
    if "sweep_reclaim" in primitive_set:
        trigger_bits.append("closed-bar reclaim after sweep")
    if "range_breakout" in primitive_set:
        trigger_bits.append("close-confirmed break or retest")
    if "volume_participation" in primitive_set:
        trigger_bits.append("volume/delta impulse above train-only threshold")
    if "momentum_confirm" in primitive_set:
        trigger_bits.append("momentum slope or BBP/RSI/ADX agreement")
    if _contains_any(text, ("displacement", "body", "impulse")):
        trigger_bits.append("displacement candle with body/ATR expansion")
    if not trigger_bits:
        trigger_bits.append("edge-router score must exceed fee-aware threshold")
    return "; ".join(trigger_bits)


def _execution_layer(recommended_port: str, primitive_set: set[str], text: str) -> str:
    if recommended_port == "orderflow_proxy_v1":
        return "Maker-first only unless predicted move clears taker fee, slippage, and safety buffer."
    if recommended_port in {"fvg_liquidity_breakout_v1", "range_expansion_breakout_v1"}:
        return "Prefer maker/retest fills; allow taker only on high-displacement continuation with edge > fees."
    if "risk_plan" in primitive_set or _contains_any(text, ("tp", "sl", "target", "breakeven")):
        return "Route entry separately from exit logic; exits remain reduce-only and stop-first in replay."
    return "Use as context/feature input until a cost-aware entry route is proven."


def _exit_layer(primitive_set: set[str], text: str) -> str:
    if "risk_plan" in primitive_set and "trend_trail" in primitive_set:
        return "Structural stop, TP1 partial, move to breakeven after TP1, then adaptive trail."
    if "risk_plan" in primitive_set:
        return "Structural stop plus TP1/TP2/TP3 ladder; stop-first replay decides ties."
    if "trend_trail" in primitive_set:
        return "Use adaptive trail as an overlay on existing entries before standalone testing."
    if _contains_any(text, ("atr", "stop", "target")):
        return "Convert ATR stop/target concepts into VNEDGE risk-plan parameters."
    return "Attach VNEDGE default stop/target model before replay; no source exit is trusted blindly."


def _bot_use(action: str, recommended_port: str, family: str) -> str:
    if action == "CAUSALITY_QUARANTINE":
        return "Quarantine: rewrite or discard repaint/display logic before it can enter the feature bank."
    if action == "FEATURE_BANK_ONLY":
        return "Feature-bank only: useful as a component, not as a standalone scanner lane."
    if recommended_port == "trail_exit_lab_v1":
        return "Exit overlay candidate for current shadow/paper lanes before new entries are tested."
    if recommended_port in {"fvg_liquidity_breakout_v1", "range_expansion_breakout_v1"}:
        return "Standalone scanner candidate after causal port and all-venue multi-timeframe replay."
    if recommended_port == "orderflow_proxy_v1":
        return "Execution/context filter for real-time scanners; needs tick/L2 coverage before entries."
    if "feature" in family:
        return "Edge-model feature candidate; prove OOS lift against raw scanner baseline."
    return "Research-only candidate until VNEDGE-owned Python port and untouched judgment pass."


def _backtest_recipe(recommended_port: str, family: str) -> str:
    if recommended_port == "fvg_liquidity_breakout_v1":
        return "1h bias, 15m zones, 5m trigger, all venues, ETH/SOL/XRP first, min 20 trades and >25 bps net."
    if recommended_port == "range_expansion_breakout_v1":
        return "15m range construction, 5m close/retest trigger, maker-first replay, then 1h/4h sensitivity."
    if recommended_port == "trail_exit_lab_v1":
        return "Replay as exit overlay on existing entries; require higher PF or lower drawdown OOS."
    if recommended_port == "orderflow_proxy_v1":
        return "Use recorded trades/L2; compare orderflow-filtered entries with unfiltered scanner baseline."
    if recommended_port == "trend_momentum_context_v1":
        return "Inject as permission/context feature and measure OOS lift, not raw signal frequency."
    if family == "causality_quarantine":
        return "No backtest until repaint/HTF and display-state dependencies are removed."
    return "Chronological train/OOS split, train-only thresholds, then untouched-window judgment."


def _portable_atoms(primitive_set: set[str], text: str) -> tuple[str, ...]:
    atoms: list[str] = []
    if "mtf_bias" in primitive_set:
        atoms.append("closed HTF bias")
    if "liquidity_zone" in primitive_set:
        atoms.append("zone boundaries and invalidation buffer")
    if "sweep_reclaim" in primitive_set:
        atoms.append("sweep and reclaim event")
    if "range_breakout" in primitive_set:
        atoms.append("range box and close-confirmed break")
    if "trend_trail" in primitive_set:
        atoms.append("ATR/supertrend-style trail")
    if "momentum_confirm" in primitive_set:
        atoms.append("momentum slope, RSI/MACD/BBP/ADX state")
    if "volume_participation" in primitive_set:
        atoms.append("volume, VWAP, CVD, or delta participation")
    if "risk_plan" in primitive_set:
        atoms.append("structural SL, TP ladder, breakeven rule")
    if _contains_any(text, ("kalman", "regime", "efficiency")):
        atoms.append("regime/efficiency state")
    return tuple(dict.fromkeys(atoms)) or ("feature matrix candidate",)


def _non_portable_atoms(risk_set: set[str]) -> tuple[str, ...]:
    mapping = {
        "lookahead_on": "lookahead-enabled higher-timeframe values",
        "mtf_repaint_review_required": "higher-timeframe repaint review required",
        "request_security_requires_closed_bar_rewrite": "unconfirmed higher-timeframe reads",
        "last_bar_display_logic": "last-bar display logic that is not historical signal state",
        "last_bar_display_state": "last-bar-only display state",
        "visual_overlay_state": "labels/tables/boxes that are visual, not execution state",
        "visual_label_not_execution_strategy": "visual labels without a deterministic execution contract",
        "forecast_not_signal": "forecast/projection language without an executable trigger",
        "no_machine_alert_contract": "no machine-readable alert or order payload",
        "no_machine_alert_payload": "alert exists but no order-ready payload contract",
        "strategy_state_requires_event_rewrite": "broker/state assumptions that need event replay",
        "library_helper_not_standalone": "library helper code without standalone signal contract",
        "fixed_session_review": "market-session assumptions that must be crypto-normalized",
    }
    atoms = [label for risk, label in mapping.items() if risk in risk_set]
    return tuple(atoms) or ("none detected beyond normal causal-port review",)


def _uplift_questions(
    row: dict,
    family: str,
    primitive_set: set[str],
    risk_set: set[str],
) -> tuple[str, ...]:
    questions: list[str] = []
    if "lookahead_on" in risk_set or "request_security_requires_closed_bar_rewrite" in risk_set:
        questions.append("Can the same signal survive with closed HTF bars only?")
    if "visual_overlay_state" in risk_set:
        questions.append("Which drawn object becomes the deterministic entry/exit event?")
    if "volume_participation" in primitive_set:
        questions.append("Does participation improve net bps after venue-specific fees and slippage?")
    if family in {"liquidity_sweep_reclaim", "range_expansion_breakout"}:
        questions.append("Does maker-first/retest execution preserve edge better than taker breakout entry?")
    if "momentum_confirm" in primitive_set:
        questions.append("Does momentum work as a soft edge-router feature instead of a hard gate?")
    if int(row.get("crypto_fit_score") or 0) >= 70:
        questions.append("Can it pass a train-only threshold calibration across all three venues?")
    return tuple(dict.fromkeys(questions)) or (
        "Does this source add OOS lift over the current raw scanner baseline?",
    )


def _source_for_row(row: dict, source_index: dict[str, SourceArtifact]) -> SourceArtifact | None:
    digest = str(row.get("source_sha256") or "").strip()
    if not digest:
        extraction = row.get("source_extraction")
        if isinstance(extraction, dict):
            digest = str(extraction.get("source_sha256") or "").strip()
    return source_index.get(digest) if digest else None


def _analysis_text(row: dict, source: str) -> str:
    # Source-backed intent should come from the script artifact itself.  Generated
    # KB tags/notes are useful for UI filtering, but including them here makes
    # primitive detection echo our own prior labels instead of reading the Pine.
    metadata = " ".join(
        [
            str(row.get("title") or ""),
            str(row.get("script_id") or ""),
            str(row.get("mechanism") or ""),
        ]
    )
    return f"{metadata}\n{source}".lower()


def _detect_primitives(text: str) -> tuple[str, ...]:
    found = [
        primitive
        for primitive, patterns in PRIMITIVE_PATTERNS.items()
        if _matches_any(text, patterns)
    ]
    if not found:
        found.append("feature_bank")
    return tuple(found)


def _detect_risks(text: str, row: dict) -> tuple[str, ...]:
    risks = {str(item) for item in row.get("risks") or () if str(item).strip()}
    for risk, patterns in RISK_PATTERNS.items():
        if _matches_any(text, patterns):
            risks.add(risk)
    if "request.security" in text and "barstate.isconfirmed" not in text:
        risks.add("request_security_requires_closed_bar_rewrite")
    if "strategy(" not in text and "alertcondition(" not in text:
        risks.add("no_machine_alert_contract")
    if "library(" in text or str(row.get("kind") or "") == "library":
        risks.add("library_helper_not_standalone")
    if "strategy.position_size" in text or "pyramiding" in text:
        risks.add("strategy_state_requires_event_rewrite")
    return tuple(sorted(risks))


def _recommended_port(row: dict, primitives: tuple[str, ...], risks: tuple[str, ...]) -> str:
    primitive_set = set(primitives)
    risk_set = set(risks)
    row_text = " ".join(
        [
            str(row.get("title") or ""),
            str(row.get("script_id") or ""),
            str(row.get("mechanism") or ""),
            " ".join(str(item) for item in row.get("features") or ()),
            " ".join(str(item) for item in row.get("tags") or ()),
        ]
    ).lower()
    if (
        str(row.get("crypto_portability") or "") == "BLOCKED_REPAINT_RISK"
        or "lookahead_on" in risk_set
        or "request_security_requires_closed_bar_rewrite" in risk_set
    ):
        return "causality_quarantine_v1"
    if "library_helper_not_standalone" in risk_set:
        return "source_feature_library_review_v1"
    if "volume_participation" in primitive_set and (
        str(row.get("mechanism") or "") == "orderflow"
        or any(token in row_text for token in ("cvd", "footprint", "absorption", "volume delta"))
    ):
        return "orderflow_proxy_v1"
    if "liquidity_zone" in primitive_set and _has_any_primitive(
        primitive_set,
        ("sweep_reclaim", "range_breakout"),
    ):
        return "fvg_liquidity_breakout_v1"
    if "range_breakout" in primitive_set and "volume_participation" in primitive_set:
        return "range_expansion_breakout_v1"
    if "trend_trail" in primitive_set and "risk_plan" in primitive_set:
        return "trail_exit_lab_v1"
    if "momentum_confirm" in primitive_set and "trend_trail" in primitive_set:
        return "trend_momentum_context_v1"
    return "edge_model_feature_bank_v1"


def _action_for(row: dict, recommended_port: str, risks: tuple[str, ...]) -> str:
    risk_set = set(risks)
    verdict = str(row.get("crypto_portability") or "")
    if recommended_port == "causality_quarantine_v1":
        return "CAUSALITY_QUARANTINE"
    if "library_helper_not_standalone" in risk_set or verdict == "RESEARCH_ONLY":
        return "FEATURE_BANK_ONLY"
    if verdict in SOURCE_BACKED_PORTABLE:
        return "PORT_CANDIDATE"
    return "MANUAL_REVIEW"


def _distillation_score(
    row: dict,
    primitives: tuple[str, ...],
    risks: tuple[str, ...],
    recommended_port: str,
) -> int:
    score = int(round(
        _bounded_int(row.get("crypto_fit_score"), 0, 100) * 0.62
        + _bounded_int(row.get("priority_score"), 0, 100) * 0.18
    ))
    score += min(16, len(primitives) * 2)
    score += {
        "orderflow_proxy_v1": 10,
        "fvg_liquidity_breakout_v1": 9,
        "range_expansion_breakout_v1": 8,
        "trail_exit_lab_v1": 7,
        "trend_momentum_context_v1": 5,
        "edge_model_feature_bank_v1": 2,
        "source_feature_library_review_v1": -12,
        "causality_quarantine_v1": -40,
    }.get(recommended_port, 0)
    risk_set = set(risks)
    if "no_machine_alert_contract" in risk_set:
        score -= 6
    if "visual_overlay_state" in risk_set:
        score -= 8
    if "forecast_not_signal" in risk_set:
        score -= 8
    if "strategy_state_requires_event_rewrite" in risk_set:
        score -= 6
    return max(0, min(100, score))


def _primitive_families(distillations: Iterable[ScriptDistillation]) -> list[PrimitiveFamily]:
    by_primitive: dict[str, list[ScriptDistillation]] = defaultdict(list)
    for row in distillations:
        if row.action != "PORT_CANDIDATE":
            continue
        for primitive in row.primitives:
            by_primitive[primitive].append(row)
    families = []
    for primitive, rows in by_primitive.items():
        sorted_rows = sorted(rows, key=lambda row: (-row.distillation_score, row.script_id))
        port = _dominant_port(sorted_rows)
        families.append(
            PrimitiveFamily(
                primitive=primitive,
                script_count=len(sorted_rows),
                recommended_port=port,
                required_data=_required_data(port),
                gate_before_shadow=_gate_before_shadow(port),
                top_scripts=tuple(_top_script(row) for row in sorted_rows[:8]),
            )
        )
    return sorted(families, key=lambda row: (-row.script_count, row.primitive))


def _intention_clusters(distillations: Iterable[ScriptDistillation]) -> list[IntentionCluster]:
    by_family: dict[str, list[ScriptDistillation]] = defaultdict(list)
    for row in distillations:
        by_family[row.intention.intent_family].append(row)
    clusters: list[IntentionCluster] = []
    for family, rows in by_family.items():
        sorted_rows = sorted(rows, key=lambda row: (-row.distillation_score, row.script_id))
        port_counts = Counter(row.recommended_port for row in sorted_rows)
        risk_counts = Counter(risk for row in sorted_rows for risk in row.risks)
        candidates = [row for row in sorted_rows if row.action == "PORT_CANDIDATE"]
        fit_scores = [row.crypto_fit_score for row in sorted_rows]
        recommended_port = port_counts.most_common(1)[0][0]
        clusters.append(
            IntentionCluster(
                intent_family=family,
                source_count=len(sorted_rows),
                port_candidates=len(candidates),
                quarantine_count=sum(
                    1 for row in sorted_rows if row.action == "CAUSALITY_QUARANTINE"
                ),
                recommended_port=recommended_port,
                median_fit_score=round(float(median(fit_scores)), 2) if fit_scores else 0.0,
                playbook=_cluster_playbook(family),
                bot_value=_cluster_bot_value(family, recommended_port),
                first_backtest=_backtest_recipe(recommended_port, family),
                top_scripts=tuple(_top_script(row) for row in sorted_rows[:8]),
                blocker_summary=tuple(
                    f"{risk}: {count}" for risk, count in risk_counts.most_common(5)
                ),
            )
        )
    return sorted(
        clusters,
        key=lambda row: (-row.port_candidates, -row.source_count, row.intent_family),
    )


def _port_tasks(distillations: Iterable[ScriptDistillation]) -> list[PortTask]:
    by_port: dict[str, list[ScriptDistillation]] = defaultdict(list)
    for row in distillations:
        if row.action != "PORT_CANDIDATE":
            continue
        by_port[row.recommended_port].append(row)
    tasks = []
    for port, rows in by_port.items():
        sorted_rows = sorted(rows, key=lambda row: (-row.distillation_score, row.script_id))
        primitive_counts = Counter(
            primitive
            for row in sorted_rows
            for primitive in row.primitives
            if primitive != "feature_bank"
        )
        risk_counts = Counter(risk for row in sorted_rows for risk in row.risks)
        fit_scores = [row.crypto_fit_score for row in sorted_rows]
        score = _task_score(sorted_rows)
        tasks.append(
            PortTask(
                task_id=f"{PINE_ALPHA_DISTILLER_ID}|{port}",
                recommended_port=port,
                source_count=len(sorted_rows),
                source_script_ids=tuple(row.script_id for row in sorted_rows[:12]),
                primitive_stack=tuple(
                    primitive for primitive, _ in primitive_counts.most_common(6)
                ),
                median_fit_score=round(float(median(fit_scores)), 2) if fit_scores else 0.0,
                distillation_score=score,
                risks=tuple(risk for risk, _ in risk_counts.most_common(6)),
                required_data=_required_data(port),
                first_replay=_first_replay(port),
                gate_before_shadow=_gate_before_shadow(port),
            )
        )
    return sorted(tasks, key=lambda row: (-row.distillation_score, -row.source_count, row.recommended_port))


def _summary(
    distillations: list[ScriptDistillation],
    primitive_families: list[PrimitiveFamily],
    intention_clusters: list[IntentionCluster],
    port_tasks: list[PortTask],
) -> dict:
    actions = Counter(row.action for row in distillations)
    primitives = Counter(primitive for row in distillations for primitive in row.primitives)
    ports = Counter(row.recommended_port for row in distillations)
    port_ready = actions.get("PORT_CANDIDATE", 0)
    return {
        "source_backed_reviewed": len(distillations),
        "port_candidates": port_ready,
        "causality_quarantine": actions.get("CAUSALITY_QUARANTINE", 0),
        "feature_bank_only": actions.get("FEATURE_BANK_ONLY", 0),
        "manual_review": actions.get("MANUAL_REVIEW", 0),
        "intention_clusters": len(intention_clusters),
        "primitive_families": len(primitive_families),
        "port_tasks": len(port_tasks),
        "top_intention_family": intention_clusters[0].intent_family if intention_clusters else "",
        "top_intention_source_count": intention_clusters[0].source_count if intention_clusters else 0,
        "primitive_counts": dict(primitives.most_common()),
        "task_counts": dict(ports.most_common()),
        "top_task": port_tasks[0].recommended_port if port_tasks else "",
        "top_task_source_count": port_tasks[0].source_count if port_tasks else 0,
        "queued_backtest_cells": port_ready * len(REPLAY_TIMEFRAMES) * len(REPLAY_VENUES),
        "can_trade": False,
        "can_promote": False,
    }


def _policy() -> dict:
    return {
        "research_only": True,
        "can_trade": False,
        "can_promote": False,
        "source_rule": "source-backed rows only; output keeps hashes and metadata, never Pine code",
        "copying_policy": "distill primitives, then implement VNEDGE-owned causal Python",
        "requires_causal_port": True,
        "requires_backtest": True,
        "requires_untouched_judgment": True,
        "requires_human_approval": True,
    }


def _operator_answer(distillations: list[ScriptDistillation], port_tasks: list[PortTask]) -> str:
    if not distillations:
        return (
            "No source-backed portable Pine rows were found. Retry open-source extraction "
            "or supply Pine exports before VNEDGE can distill anything honestly."
        )
    if not port_tasks:
        return (
            "The source-backed rows are mostly quarantine/feature-bank material. Do not "
            "promote; first remove repaint/display-state risks."
        )
    top = port_tasks[0]
    return (
        f"Highest-ranked source-backed build is {top.recommended_port} from "
        f"{top.source_count} scripts. Next step is a VNEDGE-owned causal port, "
        "then replay on 5m/15m/1h/4h across Binance, Bybit, and Delta India."
    )


def _dominant_port(rows: list[ScriptDistillation]) -> str:
    if not rows:
        return "edge_model_feature_bank_v1"
    return Counter(row.recommended_port for row in rows).most_common(1)[0][0]


def _top_script(row: ScriptDistillation) -> dict:
    return {
        "script_id": row.script_id,
        "title": row.title,
        "url": row.url,
        "source_sha256": row.source_sha256[:12],
        "fit": row.crypto_fit_score,
        "distillation_score": row.distillation_score,
    }


def _task_score(rows: list[ScriptDistillation]) -> int:
    if not rows:
        return 0
    base = (sum(row.distillation_score for row in rows[:20]) / min(len(rows), 20)) * 0.82
    count_bonus = min(18, int(math.log2(len(rows) + 1) * 4))
    diversity_bonus = min(10, len({primitive for row in rows for primitive in row.primitives}))
    return max(0, min(100, int(round(base + count_bonus + diversity_bonus))))


def _required_data(port: str) -> tuple[str, ...]:
    common = ("fee_model", "slippage_model", "closed_candles")
    mapping = {
        "fvg_liquidity_breakout_v1": (
            "1h_bias_candles",
            "15m_zone_builder",
            "5m_displacement_trigger",
            "volume_zscore",
            "room_to_liquidity",
        ),
        "range_expansion_breakout_v1": (
            "15m_range_box",
            "5m_break_retest",
            "volume_impulse",
            "atr_percentile",
            "session_24x7_crypto_guard",
        ),
        "trail_exit_lab_v1": (
            "entry_journal",
            "5m_to_15m_ohlcv",
            "atr_trail",
            "tp1_be_state",
            "stop_first_backtest",
        ),
        "orderflow_proxy_v1": (
            "public_trades",
            "cvd_delta_bars",
            "l2_snapshot_coverage",
            "conservative_fill_model",
            "spread_depth_filter",
        ),
        "trend_momentum_context_v1": (
            "1h_trend_bias",
            "15m_momentum_state",
            "5m_trigger",
            "adx_er_alignment",
            "volume_confirmation",
        ),
        "edge_model_feature_bank_v1": (
            "scanner_opportunity_rows",
            "chronological_train_oos_split",
            "train_only_calibration",
            "model_drift_telemetry",
        ),
    }
    return tuple((*mapping.get(port, ("feature_matrix", "causality_audit")), *common))


def _first_replay(port: str) -> str:
    mapping = {
        "fvg_liquidity_breakout_v1": "ETH/SOL/XRP 5m trigger with 15m zones and 1h bias; require expected net >25 bps.",
        "range_expansion_breakout_v1": "All liquid pairs, 15m range setup and 5m close-confirmed break/retest.",
        "trail_exit_lab_v1": "Apply as an exit overlay to existing shadow/paper entries before standalone entry tests.",
        "orderflow_proxy_v1": "Use recorded trades/L2 only; replay with maker-first and taker-fallback fee wall.",
        "trend_momentum_context_v1": "Use as context permission for active scanner lanes before standalone entries.",
        "edge_model_feature_bank_v1": "Train edge_router features; compare OOS selected subset against raw scanner baseline.",
    }
    return mapping.get(port, "Run causal replay before any promotion.")


def _gate_before_shadow(port: str) -> tuple[str, ...]:
    gates = [
        "source hash recorded and no Pine code copied into runtime",
        "causality audit passes: no lookahead, no unconfirmed HTF bars",
        "multi-timeframe replay covers 5m, 15m, 1h, and 4h where applicable",
        "expected net edge >25 bps after fees, slippage, and safety buffer",
        "PF >1.5 and at least 20 historical trades",
        "untouched-window judgment passes before paper or shadow promotion",
    ]
    if port == "trail_exit_lab_v1":
        gates.append("improves existing entry exits OOS without increasing drawdown")
    if port == "orderflow_proxy_v1":
        gates.append("public-trade and L2 coverage pass recorder freshness checks")
    return tuple(gates)


def _cluster_playbook(family: str) -> str:
    mapping = {
        "liquidity_sweep_reclaim": (
            "Context: 1h bias and liquidity map. Setup: sweep/equal high-low/FVG. "
            "Trigger: 5m reclaim or displacement. Plan: structural stop, room-to-liquidity, TP ladder."
        ),
        "liquidity_zone_breakout": (
            "Context: zone quality and distance to next liquidity. Setup: FVG/order-block/SR zone. "
            "Trigger: acceptance or rejection away from the zone with volume confirmation."
        ),
        "range_expansion_breakout": (
            "Context: compressed 15m range. Setup: clean range high/low and volatility floor. "
            "Trigger: close-confirmed break or retest; reject wick-only fakeouts."
        ),
        "adaptive_trail_exit": (
            "Attach adaptive trail, TP1, BE, and structural stop rules to entries that already fire; "
            "measure PF/DD uplift before standalone entry testing."
        ),
        "orderflow_absorption": (
            "Convert public trade pressure, CVD, footprint, and absorption ideas into execution filters "
            "for already-qualified setups; never use raw 1m orderflow alone."
        ),
        "trend_momentum_filter": (
            "Use BBP/RSI/MACD/ADX/ER alignment as a soft permission score for candidate triggers, "
            "then test whether it increases OOS net bps."
        ),
        "momentum_feature_bank": (
            "Store oscillator/momentum states as edge-router features; train thresholds on the past "
            "and judge only on unseen windows."
        ),
        "feature_library": (
            "Review as reusable atoms only. No trading route exists until a standalone VNEDGE scanner "
            "contract consumes the helper."
        ),
        "causality_quarantine": (
            "Rewrite unconfirmed HTF, visual-only, or lookahead logic into closed-bar causal atoms; "
            "do not count any backtest before that rewrite."
        ),
    }
    return mapping.get(
        family,
        "Distill causal features, add them to the edge-model feature bank, and measure OOS lift.",
    )


def _cluster_bot_value(family: str, recommended_port: str) -> str:
    if family in {"liquidity_sweep_reclaim", "liquidity_zone_breakout"}:
        return "Best candidate for real scanner uplift because it defines context, setup, trigger, and risk."
    if family == "range_expansion_breakout":
        return "Can raise signal frequency without dropping governance if retest/maker execution is enforced."
    if family == "adaptive_trail_exit":
        return "Most likely to improve negative trades by smarter exits before new entries are promoted."
    if family == "orderflow_absorption":
        return "Useful for taker/maker routing and no-trade decisions when L2/trade coverage is healthy."
    if family in {"trend_momentum_filter", "momentum_feature_bank"}:
        return "Useful as model features and lane filters; weak as standalone scalper entries after costs."
    if recommended_port == "causality_quarantine_v1":
        return "Potential idea value is locked until repaint and display-state dependencies are removed."
    return "Research-only until a causal Python port proves positive OOS edge."


def _matches_any(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle.lower() in text for needle in needles)


def _has_any_primitive(primitives: set[str], needles: tuple[str, ...]) -> bool:
    return any(needle in primitives for needle in needles)


def _bounded_int(value: object, floor: int, ceiling: int) -> int:
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return floor
    if not math.isfinite(parsed):
        return floor
    return max(floor, min(ceiling, parsed))


def _assert_no_pine_source_leak(encoded: str) -> None:
    forbidden = ("//@version", "indicator(", "strategy(", "library(", "plotshape", "label.new")
    if any(token in encoded for token in forbidden):
        raise ValueError("distiller output contains Pine source-like text")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
