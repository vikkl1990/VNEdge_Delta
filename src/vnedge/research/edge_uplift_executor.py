"""Research-only executor queue for Pine edge-uplift experiments.

The Pine edge-uplift agent decides what failed evidence is worth salvaging.
This module turns those agent suggestions into concrete VNEDGE-owned research
tasks: causal port work, execution-filtered replay, feature-bank extraction,
or untouched-window judgment requests.

It does not run orders, does not promote lanes, and does not relax live
governance. The output is an operator/task board for the Pine Research Lab.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
from tempfile import NamedTemporaryFile
import time
from typing import Iterable, Literal


EDGE_UPLIFT_EXECUTOR_ID = "edge_uplift_executor_v1"
DEFAULT_UPLIFT = Path("research/live_research/pine_edge_uplift_agent_latest.json")
DEFAULT_SCANNER = Path("research/live_research/scanner_tournament_latest.json")
DEFAULT_FEE_WALL = Path("research/live_research/fee_wall_forensics_latest.json")
DEFAULT_OUT = Path("research/live_research/edge_uplift_experiments_latest.json")
DEFAULT_FEED = Path("research/live_research/edge_uplift_experiments_feed.jsonl")

TaskStatus = Literal[
    "READY_FOR_REPLAY",
    "READY_FOR_UNTOUCHED_JUDGMENT",
    "NEEDS_CAUSAL_PORT",
    "FEATURE_BANK_ONLY",
    "BLOCKED_NO_PORT",
]

REPLAY_EXPERIMENT_TYPES = {
    "execution_filtered_replay",
    "confluence_stack_replay",
}
FEATURE_BANK_EXPERIMENT_TYPES = {
    "edge_model_feature_bank",
    "context_feature_bank",
}


@dataclass(frozen=True)
class PortDefinition:
    port_id: str
    family: str
    mechanism: str
    primitives: tuple[str, ...]
    strategy_aliases: tuple[str, ...]
    trigger_timeframes: tuple[str, ...]
    confirmation_timeframes: tuple[str, ...]
    bias_timeframes: tuple[str, ...]
    venues: tuple[str, ...]
    pairs: tuple[str, ...]
    required_data: tuple[str, ...]
    pass_gates: dict[str, float | int | str]
    execution_rules: tuple[str, ...]
    blocked_actions: tuple[str, ...]
    operator_note: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class EdgeUpliftTask:
    task_id: str
    experiment_id: str
    experiment_type: str
    recommended_port: str
    status: TaskStatus
    executor_action: str
    priority: int
    salvage_score: int
    source_script_ids: tuple[str, ...]
    source_titles: tuple[str, ...]
    primitive_stack: tuple[str, ...]
    failed_cells: int
    positive_cells: int
    best_avg_net_bps: float | None
    best_profit_factor: float | None
    port_implementation: dict
    scanner_support: dict
    candidate_matches: tuple[dict, ...]
    required_data: tuple[str, ...]
    replay_plan: dict
    guardrails: tuple[str, ...]
    can_trade: bool = False
    can_promote: bool = False
    requires_untouched_judgment: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


def run_edge_uplift_executor(
    *,
    uplift_path: Path | str = DEFAULT_UPLIFT,
    scanner_path: Path | str = DEFAULT_SCANNER,
    fee_wall_path: Path | str | None = DEFAULT_FEE_WALL,
    max_experiments: int = 12,
    now: datetime | None = None,
) -> dict:
    """Build the next research task queue from agent and scanner artifacts."""

    generated = now or datetime.now(UTC)
    uplift = _read_json(Path(uplift_path))
    scanner = _read_json(Path(scanner_path))
    fee_wall = _read_json(Path(fee_wall_path)) if fee_wall_path is not None else {}
    experiments = [
        dict(row)
        for row in uplift.get("experiments", [])
        if isinstance(row, dict)
    ][:max(0, max_experiments)]
    candidates = tuple(
        dict(row)
        for row in scanner.get("candidates", [])
        if isinstance(row, dict)
    ) + _fee_wall_support_candidates(fee_wall)
    ports = causal_port_pack_v1()
    tasks = tuple(
        _task_for_experiment(
            row,
            ports=ports,
            scanner_candidates=candidates,
            index=index,
        )
        for index, row in enumerate(experiments, start=1)
    )
    summary = _summary(tasks, ports, uplift, scanner)
    return {
        "executor_id": EDGE_UPLIFT_EXECUTOR_ID,
        "generated_at": generated.isoformat(),
        "source": {
            "uplift": str(uplift_path),
            "scanner": str(scanner_path),
            "fee_wall": str(fee_wall_path) if fee_wall_path is not None else None,
        },
        "summary": summary,
        "port_pack": [port.to_dict() for port in ports.values()],
        "tasks": [task.to_dict() for task in tasks],
        "operator_answer": _operator_answer(summary, tasks),
        "policy": _policy(),
        "can_trade": False,
        "can_promote": False,
    }


def publish_edge_uplift_executor(
    payload: dict,
    *,
    out: Path | str = DEFAULT_OUT,
    feed: Path | str | None = DEFAULT_FEED,
) -> Path:
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
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
    _chmod_readable(tmp_path)
    tmp_path.replace(out_path)
    _chmod_readable(out_path)
    if feed is not None:
        feed_path = Path(feed)
        feed_path.parent.mkdir(parents=True, exist_ok=True)
        with feed_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_feed_record(payload), sort_keys=True) + "\n")
        _chmod_readable(feed_path)
    return out_path


def causal_port_pack_v1() -> dict[str, PortDefinition]:
    gates = {
        "expected_net_edge_bps_gt": 25.0,
        "profit_factor_gt": 1.5,
        "min_historical_trades": 20,
        "fee_model": "maker-first; taker only when predicted move pays fees, slippage, and buffer",
    }
    execution_rules = (
        "maker-first entry unless expected net edge clears taker fee wall plus buffer",
        "taker allowed only with model route score and room-to-liquidity confirmation",
        "structural stop is mandatory before sizing",
        "TP1/TP2/TP3 and breakeven are evaluated as exits, not entry permission",
    )
    blocked = (
        "auto_trade",
        "auto_promote",
        "copy_protected_pine_source",
        "relax_live_risk_gates",
        "judge_on_seen_data",
    )
    venues = ("binanceusdm", "bybit", "delta_india")
    pairs = (
        "BTC/USDT:USDT",
        "ETH/USDT:USDT",
        "SOL/USDT:USDT",
        "BNB/USDT:USDT",
        "XRP/USDT:USDT",
        "DOGE/USDT:USDT",
        "BTC/USD:USD",
        "ETH/USD:USD",
        "SOL/USD:USD",
        "XRP/USD:USD",
    )
    return {
        "fvg_liquidity_breakout_v1": PortDefinition(
            port_id="fvg_liquidity_breakout_v1",
            family="liquidity_breakout",
            mechanism="HTF bias plus premium/discount zone, sweep/reclaim, FVG displacement, and room-to-liquidity exits.",
            primitives=(
                "mtf_bias",
                "premium_discount",
                "liquidity_zone",
                "sweep_reclaim",
                "choch",
                "fvg_displacement",
                "risk_plan",
            ),
            strategy_aliases=(
                "smc_playbook_scalper_v1",
                "luxara_break_bounce_v27_v1",
                "human_trade_fingerprint_v1",
            ),
            trigger_timeframes=("5m", "15m"),
            confirmation_timeframes=("15m", "1h"),
            bias_timeframes=("1h", "4h"),
            venues=venues,
            pairs=pairs,
            required_data=(
                "closed OHLCV candles",
                "swing high/low state",
                "FVG or displacement gap proxy",
                "volume z-score",
                "fee/slippage model",
            ),
            pass_gates=gates,
            execution_rules=execution_rules,
            blocked_actions=blocked,
            operator_note="Highest priority for Pine SMC/Luxara style scripts because it models setup, trigger, plan, and exits separately.",
        ),
        "range_expansion_breakout_v1": PortDefinition(
            port_id="range_expansion_breakout_v1",
            family="range_expansion",
            mechanism="Compression box, wick probe, close confirmation, ATR/body expansion, and staged trade plan.",
            primitives=(
                "range_box",
                "range_breakout",
                "body_impulse",
                "volume_participation",
                "momentum_confirm",
                "risk_plan",
            ),
            strategy_aliases=(
                "luxara_live_plan_qtm_v1",
                "sats_5m_scalper_v1",
                "alpha_stack_confluence_v1",
            ),
            trigger_timeframes=("5m", "15m"),
            confirmation_timeframes=("15m",),
            bias_timeframes=("1h", "4h"),
            venues=venues,
            pairs=pairs,
            required_data=(
                "closed OHLCV candles",
                "rolling range high/low",
                "ATR percentile",
                "volume z-score",
                "fee/slippage model",
            ),
            pass_gates=gates,
            execution_rules=execution_rules,
            blocked_actions=blocked,
            operator_note="Use for Luxara Live Plan and box-break scripts; close-confirmed breaks are favored over wick-only alerts.",
        ),
        "orderflow_proxy_v1": PortDefinition(
            port_id="orderflow_proxy_v1",
            family="orderflow_proxy",
            mechanism="Public-trade footprint, CVD/delta slope, stacked imbalance proxies, and maker/taker route truth.",
            primitives=(
                "public_trade_delta",
                "cvd_slope",
                "stacked_imbalance",
                "volume_participation",
                "maker_taker_route",
            ),
            strategy_aliases=(
                "stealth_trail_bbp_v1",
                "quant_signal_pack_v1",
                "alpha_stack_confluence_v1",
            ),
            trigger_timeframes=("1m", "5m"),
            confirmation_timeframes=("5m", "15m"),
            bias_timeframes=("15m", "1h"),
            venues=venues,
            pairs=pairs,
            required_data=(
                "recorded public trades",
                "L2/top-book snapshots where available",
                "closed OHLCV candles",
                "fee/slippage model",
            ),
            pass_gates=gates,
            execution_rules=execution_rules,
            blocked_actions=blocked,
            operator_note="Do not use as a standalone book-imbalance replay; require participation and route evidence.",
        ),
        "trail_exit_lab_v1": PortDefinition(
            port_id="trail_exit_lab_v1",
            family="exit_overlay",
            mechanism="ATR/Supertrend/Chandelier style trailing stop, TP scaling, and breakeven after TP1.",
            primitives=(
                "trend_trail",
                "atr_trail",
                "tp_ladder",
                "breakeven_after_tp1",
                "time_stop",
            ),
            strategy_aliases=(
                "stealth_trail_bbp_v1",
                "luxy_ut_bot_forecast_v1",
                "momentum_cascade_lyro_v1",
            ),
            trigger_timeframes=("5m", "15m"),
            confirmation_timeframes=("15m",),
            bias_timeframes=("1h",),
            venues=venues,
            pairs=pairs,
            required_data=(
                "closed OHLCV candles",
                "ATR state",
                "trade outcome labels",
                "fee/slippage model",
            ),
            pass_gates=gates,
            execution_rules=execution_rules,
            blocked_actions=blocked,
            operator_note="Apply as an exit overlay to already-active entries before testing standalone entry signals.",
        ),
        "trend_momentum_context_v1": PortDefinition(
            port_id="trend_momentum_context_v1",
            family="context_permission",
            mechanism="1h/4h trend regime, BBP/momentum persistence, ADX/ER quality, and scanner permission matrix.",
            primitives=(
                "mtf_bias",
                "bbp_histogram",
                "momentum_confirm",
                "adx_er_quality",
                "regime_permission",
            ),
            strategy_aliases=(
                "human_trade_fingerprint_v1",
                "stealth_trail_bbp_v1",
                "luxy_ut_bot_forecast_v1",
                "momentum_cascade_lyro_v1",
                "quant_signal_pack_v1",
                "alpha_stack_confluence_v1",
            ),
            trigger_timeframes=("5m", "15m"),
            confirmation_timeframes=("15m", "1h"),
            bias_timeframes=("1h", "4h"),
            venues=venues,
            pairs=pairs,
            required_data=(
                "closed OHLCV candles",
                "BBP high/low minus EMA state",
                "ADX/ER trend quality",
                "fee/slippage model",
            ),
            pass_gates=gates,
            execution_rules=execution_rules,
            blocked_actions=blocked,
            operator_note="Use as a permission/context layer when standalone entries fail after fees.",
        ),
        "edge_model_feature_bank_v1": PortDefinition(
            port_id="edge_model_feature_bank_v1",
            family="feature_bank",
            mechanism="Negative and near-miss Pine primitive rows become labels/features for the edge router.",
            primitives=(
                "failed_cell_label",
                "near_miss_label",
                "primitive_stack_embedding",
                "route_truth_feature",
            ),
            strategy_aliases=(),
            trigger_timeframes=("1m", "5m", "15m", "1h", "4h"),
            confirmation_timeframes=("5m", "15m", "1h"),
            bias_timeframes=("1h", "4h"),
            venues=venues,
            pairs=pairs,
            required_data=(
                "scanner opportunity rows",
                "edge labeler route truth",
                "chronological train/OOS split",
            ),
            pass_gates={
                "oos_improvement_bps_gt": 1.0,
                "min_model_selected_trades": 20,
                "beats_raw_scanner_baseline": "required",
            },
            execution_rules=(
                "feature bank never opens trades",
                "model-routed output must prove OOS lift against raw scanner baseline",
            ),
            blocked_actions=blocked,
            operator_note="Use failed indicators as supervised learning signal rather than loosening entries.",
        ),
    }


def _task_for_experiment(
    experiment: dict,
    *,
    ports: dict[str, PortDefinition],
    scanner_candidates: tuple[dict, ...],
    index: int,
) -> EdgeUpliftTask:
    raw_port = str(experiment.get("recommended_port") or "").strip()
    experiment_type = str(experiment.get("experiment_type") or "unknown")
    port = ports.get(raw_port) or _fallback_feature_bank_port(ports, experiment_type)
    port_id = port.port_id if port is not None else raw_port
    matches = _candidate_matches(port, scanner_candidates)
    support = _scanner_support(matches)
    implementation = _port_implementation(port)
    status = _status(experiment_type, raw_port, port, support, implementation)
    priority = _priority(experiment, support, status)
    task_id = _task_id(index, port_id, experiment_type)
    primitives = tuple(
        str(item)
        for item in experiment.get("primitive_stack", ())
        if str(item)
    ) or (port.primitives if port is not None else ())
    return EdgeUpliftTask(
        task_id=task_id,
        experiment_id=str(experiment.get("experiment_id") or task_id),
        experiment_type=experiment_type,
        recommended_port=port_id,
        status=status,
        executor_action=_executor_action(status),
        priority=priority,
        salvage_score=_int(experiment.get("salvage_score")),
        source_script_ids=tuple(
            str(item)
            for item in experiment.get("source_script_ids", ())
            if str(item)
        ),
        source_titles=tuple(
            str(item)
            for item in experiment.get("source_titles", ())
            if str(item)
        ),
        primitive_stack=primitives,
        failed_cells=_int(experiment.get("failed_cells")),
        positive_cells=_int(experiment.get("positive_cells")),
        best_avg_net_bps=_float(experiment.get("best_avg_net_bps")),
        best_profit_factor=_float(experiment.get("best_profit_factor")),
        port_implementation=implementation,
        scanner_support=support,
        candidate_matches=matches,
        required_data=tuple(experiment.get("required_data") or ()) or (
            port.required_data if port is not None else ()
        ),
        replay_plan=_replay_plan(port, status),
        guardrails=tuple(experiment.get("guardrails") or ()) or _guardrails(),
    )


def _fallback_feature_bank_port(
    ports: dict[str, PortDefinition],
    experiment_type: str,
) -> PortDefinition | None:
    if experiment_type == "edge_model_feature_bank":
        return ports["edge_model_feature_bank_v1"]
    return None


def _candidate_matches(
    port: PortDefinition | None,
    scanner_candidates: tuple[dict, ...],
) -> tuple[dict, ...]:
    if port is None:
        return ()
    aliases = set(port.strategy_aliases)
    matches: list[dict] = []
    for row in scanner_candidates:
        if str(row.get("strategy_id") or "") not in aliases:
            continue
        matches.append({
            "rank": row.get("rank"),
            "candidate_id": row.get("candidate_id"),
            "verdict": row.get("verdict"),
            "recommended_action": row.get("recommended_action"),
            "score": row.get("score"),
            "exchange": row.get("exchange"),
            "symbol": row.get("symbol"),
            "timeframe": row.get("timeframe"),
            "strategy_id": row.get("strategy_id"),
            "routed": row.get("routed"),
            "avg_selected_net_bps": row.get("avg_selected_net_bps"),
            "profit_factor": row.get("profit_factor"),
            "dominant_route": row.get("dominant_route"),
            "strict_watchlist": row.get("strict_watchlist"),
            "evidence_source": row.get("evidence_source"),
        })
    return tuple(
        sorted(
            matches,
            key=lambda row: (
                _float(row.get("score")) or -1_000_000.0,
                _int(row.get("routed")),
            ),
            reverse=True,
        )[:5]
    )


def _scanner_support(matches: tuple[dict, ...]) -> dict:
    if not matches:
        return {
            "state": "NO_CURRENT_SCANNER_MATCH",
            "best_candidate_id": None,
            "best_verdict": None,
            "best_avg_net_bps": None,
            "best_profit_factor": None,
            "best_routed": 0,
        }
    best = matches[0]
    verdicts = {str(row.get("verdict") or "") for row in matches}
    if "STRICT_PROOF_WATCHLIST" in verdicts:
        state = "STRICT_WATCHLIST"
    elif "DISCOVERY_WATCHLIST" in verdicts:
        state = "DISCOVERY_WATCHLIST"
    elif "NEEDS_MORE_SAMPLES" in verdicts:
        state = "NEEDS_MORE_SAMPLES"
    else:
        state = "NEGATIVE_OR_WEAK_EVIDENCE"
    return {
        "state": state,
        "best_candidate_id": best.get("candidate_id"),
        "best_verdict": best.get("verdict"),
        "best_avg_net_bps": best.get("avg_selected_net_bps"),
        "best_profit_factor": best.get("profit_factor"),
        "best_routed": best.get("routed") or 0,
    }


def _fee_wall_support_candidates(report: dict) -> tuple[dict, ...]:
    rows: list[dict] = []
    for key, default_verdict in (
        ("strict_fee_wall_candidates", "DISCOVERY_WATCHLIST"),
        ("sample_expansion_candidates", "NEEDS_MORE_SAMPLES"),
        ("exit_salvage_candidates", "REJECT_NEGATIVE_AFTER_COST"),
    ):
        raw_rows = report.get(key, [])
        iterable = raw_rows if isinstance(raw_rows, list) else []
        for index, row in enumerate(iterable, start=1):
            if not isinstance(row, dict):
                continue
            strategy = str(row.get("strategy") or "")
            if not strategy:
                continue
            avg = _float(row.get("avg_selected_net_bps"))
            pf = _float(row.get("profit_factor"))
            routed = _int(row.get("routed"))
            verdict = default_verdict
            if (
                key == "strict_fee_wall_candidates"
                and routed >= 20
                and avg is not None
                and avg >= 25.0
                and pf is not None
                and pf >= 1.5
            ):
                verdict = "STRICT_PROOF_WATCHLIST"
            rows.append({
                "rank": index,
                "candidate_id": _fee_wall_candidate_id(row, key),
                "verdict": verdict,
                "recommended_action": row.get("recommended_action"),
                "score": _fee_wall_score(row, verdict),
                "exchange": row.get("exchange"),
                "symbol": row.get("symbol"),
                "timeframe": row.get("timeframe"),
                "strategy_id": strategy,
                "routed": routed,
                "avg_selected_net_bps": avg,
                "profit_factor": pf,
                "dominant_route": _dominant_fee_wall_route(row),
                "strict_watchlist": verdict == "STRICT_PROOF_WATCHLIST",
                "evidence_source": "fee_wall_forensics_latest.json",
            })
    return tuple(rows)


def _fee_wall_candidate_id(row: dict, bucket: str) -> str:
    strategy = _safe_id(str(row.get("strategy") or "strategy"))
    exchange = _safe_id(str(row.get("exchange") or "exchange"))
    symbol = _safe_id(str(row.get("symbol") or "symbol"))
    timeframe = _safe_id(str(row.get("timeframe") or "tf"))
    return f"fee_wall_{bucket}|{strategy}|{exchange}|{symbol}|{timeframe}"


def _fee_wall_score(row: dict, verdict: str) -> float:
    avg = _float(row.get("avg_selected_net_bps")) or -50.0
    pf = min(_float(row.get("profit_factor")) or 0.0, 5.0)
    routed = min(_int(row.get("routed")), 100)
    if verdict == "STRICT_PROOF_WATCHLIST":
        bonus = 25.0
    elif verdict == "DISCOVERY_WATCHLIST":
        bonus = 8.0
    else:
        bonus = 0.0
    return round(avg + pf * 4.0 + routed ** 0.5 + bonus, 4)


def _dominant_fee_wall_route(row: dict) -> str | None:
    verdict = str(row.get("verdict") or "")
    if verdict == "MAKER_EDGE":
        return "MAKER"
    if verdict == "TAKER_EDGE":
        return "TAKER"
    if verdict == "MIXED_ROUTE_EDGE":
        return "MAKER_THEN_TAKER"
    return None


def _status(
    experiment_type: str,
    raw_port: str,
    port: PortDefinition | None,
    support: dict,
    implementation: dict,
) -> TaskStatus:
    if port is None or not raw_port and experiment_type != "edge_model_feature_bank":
        return "BLOCKED_NO_PORT"
    if experiment_type in FEATURE_BANK_EXPERIMENT_TYPES or port.family == "feature_bank":
        return "FEATURE_BANK_ONLY"
    if experiment_type == "untouched_judgment_candidate":
        return "READY_FOR_UNTOUCHED_JUDGMENT"
    if experiment_type in REPLAY_EXPERIMENT_TYPES:
        if implementation.get("state") == "VNEDGE_PORT_REGISTERED":
            return "READY_FOR_REPLAY"
        return "NEEDS_CAUSAL_PORT"
    if support.get("state") in {"STRICT_WATCHLIST", "DISCOVERY_WATCHLIST"}:
        return "READY_FOR_REPLAY"
    return "NEEDS_CAUSAL_PORT"


def _port_implementation(port: PortDefinition | None) -> dict:
    if port is None:
        return {
            "state": "NO_PORT_DEFINITION",
            "registered_strategy_aliases": [],
            "missing_strategy_aliases": [],
        }
    if port.family == "feature_bank":
        return {
            "state": "FEATURE_BANK_PORT",
            "registered_strategy_aliases": [],
            "missing_strategy_aliases": [],
        }
    registry = _strategy_registry_ids()
    registered = sorted(alias for alias in port.strategy_aliases if alias in registry)
    missing = sorted(alias for alias in port.strategy_aliases if alias not in registry)
    if registered:
        state = "VNEDGE_PORT_REGISTERED"
    elif port.strategy_aliases:
        state = "PORT_ALIASES_UNREGISTERED"
    else:
        state = "NO_RUNTIME_ALIAS"
    return {
        "state": state,
        "registered_strategy_aliases": registered,
        "missing_strategy_aliases": missing,
    }


def _strategy_registry_ids() -> set[str]:
    from vnedge.strategy.strategy_registry import STRATEGIES

    return set(STRATEGIES)


def _executor_action(status: TaskStatus) -> str:
    return {
        "READY_FOR_REPLAY": "RUN_EXECUTION_FILTERED_REPLAY_ON_ALL_VENUES_AND_TFS",
        "READY_FOR_UNTOUCHED_JUDGMENT": "FREEZE_CONFIG_AND_REQUEST_UNTOUCHED_WINDOW_APPROVAL",
        "NEEDS_CAUSAL_PORT": "PORT_CAUSAL_PRIMITIVES_THEN_REPLAY",
        "FEATURE_BANK_ONLY": "ADD_TO_EDGE_MODEL_FEATURE_BANK_AND_COMPARE_OOS_LIFT",
        "BLOCKED_NO_PORT": "QUARANTINE_UNTIL_SOURCE_OR_PORT_MAPPING_EXISTS",
    }[status]


def _priority(experiment: dict, support: dict, status: TaskStatus) -> int:
    score = _int(experiment.get("salvage_score"))
    if status == "READY_FOR_UNTOUCHED_JUDGMENT":
        score += 25
    elif status == "READY_FOR_REPLAY":
        score += 15
    elif status == "FEATURE_BANK_ONLY":
        score += 5
    elif status == "BLOCKED_NO_PORT":
        score -= 30
    state = support.get("state")
    if state == "STRICT_WATCHLIST":
        score += 20
    elif state == "DISCOVERY_WATCHLIST":
        score += 10
    return max(0, min(100, score))


def _replay_plan(port: PortDefinition | None, status: TaskStatus) -> dict:
    if port is None:
        return {
            "scope": "blocked",
            "reason": "no causal port definition",
            "can_trade": False,
            "can_promote": False,
        }
    return {
        "scope": "research_only",
        "status": status,
        "venues": list(port.venues),
        "pairs": list(port.pairs),
        "trigger_timeframes": list(port.trigger_timeframes),
        "confirmation_timeframes": list(port.confirmation_timeframes),
        "bias_timeframes": list(port.bias_timeframes),
        "pass_gates": port.pass_gates,
        "execution_rules": list(port.execution_rules),
        "can_trade": False,
        "can_promote": False,
    }


def _summary(
    tasks: tuple[EdgeUpliftTask, ...],
    ports: dict[str, PortDefinition],
    uplift: dict,
    scanner: dict,
) -> dict:
    status_counts = Counter(task.status for task in tasks)
    top = max(tasks, key=lambda row: row.priority, default=None)
    scanner_summary = scanner.get("summary") if isinstance(scanner.get("summary"), dict) else {}
    uplift_summary = uplift.get("summary") if isinstance(uplift.get("summary"), dict) else {}
    return {
        "tasks_total": len(tasks),
        "ready_for_replay": status_counts["READY_FOR_REPLAY"],
        "ready_for_untouched_judgment": status_counts["READY_FOR_UNTOUCHED_JUDGMENT"],
        "needs_causal_port": status_counts["NEEDS_CAUSAL_PORT"],
        "feature_bank_only": status_counts["FEATURE_BANK_ONLY"],
        "blocked": status_counts["BLOCKED_NO_PORT"],
        "port_definitions": len(ports),
        "top_task_id": top.task_id if top is not None else None,
        "top_recommended_port": top.recommended_port if top is not None else None,
        "top_priority": top.priority if top is not None else None,
        "uplift_experiments_seen": uplift_summary.get("experiments", 0),
        "scanner_positive_watchlists": scanner_summary.get("positive_watchlists", 0),
        "scanner_strict_watchlists": scanner_summary.get("strict_watchlists", 0),
        "can_trade": False,
        "can_promote": False,
    }


def _operator_answer(summary: dict, tasks: tuple[EdgeUpliftTask, ...]) -> str:
    if not tasks:
        return (
            "No executable edge-uplift tasks are published yet. Keep source extraction, "
            "causal ports, and scanner tournament running."
        )
    if summary["ready_for_untouched_judgment"]:
        return (
            f"{summary['ready_for_untouched_judgment']} task(s) are judgment-ready. "
            "Freeze those configs and ask for a fresh untouched-window approval; do not tune them."
        )
    if summary["ready_for_replay"]:
        return (
            f"{summary['ready_for_replay']} task(s) are replay-ready. Run execution-filtered "
            "replay across Binance, Bybit, and Delta India before any paper discussion."
        )
    if summary["feature_bank_only"]:
        return (
            "Most signal is currently useful as feature-bank data, not standalone entries. "
            "Train the edge router and demand OOS lift against the raw scanner baseline."
        )
    return "Causal port work is the blocker: convert primitives first, then replay."


def _policy() -> dict:
    return {
        "research_only": True,
        "can_trade": False,
        "can_promote": False,
        "executor_role": "agent_output_to_replay_task_queue",
        "allowed_actions": [
            "causal_port_task",
            "execution_filtered_replay_task",
            "edge_model_feature_bank_task",
            "untouched_judgment_request",
        ],
        "blocked_actions": [
            "auto_trade",
            "auto_promote",
            "copy_protected_pine_source",
            "relax_live_risk_gates",
            "use_seen_data_for_judgment",
        ],
        "live_governance_unchanged": True,
        "risk_gateway_required_for_any_future_order": True,
    }


def _guardrails() -> tuple[str, ...]:
    return (
        "research-only; can_trade=false and can_promote=false",
        "closed-bar causality audit before replay",
        "fee-aware replay with maker/taker cost wall",
        "pre-registered untouched judgment before paper or shadow",
    )


def _feed_record(payload: dict) -> dict:
    summary = payload.get("summary", {})
    return {
        "generated_at": payload.get("generated_at"),
        "executor_id": payload.get("executor_id"),
        "tasks_total": summary.get("tasks_total"),
        "ready_for_replay": summary.get("ready_for_replay"),
        "ready_for_untouched_judgment": summary.get("ready_for_untouched_judgment"),
        "feature_bank_only": summary.get("feature_bank_only"),
        "top_task_id": summary.get("top_task_id"),
        "top_recommended_port": summary.get("top_recommended_port"),
        "can_trade": False,
        "can_promote": False,
    }


def _read_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _chmod_readable(path: Path) -> None:
    try:
        path.chmod(0o644)
    except OSError:
        pass


def _task_id(index: int, port_id: str, experiment_type: str) -> str:
    safe_port = _safe_id(port_id or "unknown_port")
    safe_type = _safe_id(experiment_type or "unknown")
    return f"edge_uplift_{index:03d}_{safe_port}_{safe_type}"


def _safe_id(raw: str) -> str:
    out = "".join(ch.lower() if ch.isalnum() else "_" for ch in raw)
    while "__" in out:
        out = out.replace("__", "_")
    return out.strip("_") or "unknown"


def _float(value: object) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and out not in {float("inf"), float("-inf")} else None


def _int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _render(payload: dict) -> str:
    summary = payload["summary"]
    lines = [
        "edge uplift executor v1",
        "policy=research_only can_trade=false can_promote=false",
        "",
        (
            f"tasks={summary['tasks_total']} replay={summary['ready_for_replay']} "
            f"judgment={summary['ready_for_untouched_judgment']} "
            f"feature_bank={summary['feature_bank_only']} blocked={summary['blocked']}"
        ),
        "",
        "priority status                         port                             action",
    ]
    for task in payload["tasks"][:20]:
        lines.append(
            f"{task['priority']:>8} {task['status']:<30} "
            f"{task['recommended_port']:<32} {task['executor_action']}"
        )
    if not payload["tasks"]:
        lines.append("no tasks published")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="build edge-uplift executor queue")
    parser.add_argument("--uplift", default=str(DEFAULT_UPLIFT))
    parser.add_argument("--scanner", default=str(DEFAULT_SCANNER))
    parser.add_argument("--fee-wall", default=str(DEFAULT_FEE_WALL))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--feed", default=str(DEFAULT_FEED))
    parser.add_argument("--max-experiments", type=int, default=12)
    parser.add_argument("--interval-seconds", type=int, default=0)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    while True:
        payload = run_edge_uplift_executor(
            uplift_path=args.uplift,
            scanner_path=args.scanner,
            fee_wall_path=args.fee_wall,
            max_experiments=args.max_experiments,
        )
        publish_edge_uplift_executor(payload, out=args.out, feed=args.feed)
        print(json.dumps(payload, indent=2, sort_keys=True) if args.json else _render(payload))
        if args.interval_seconds <= 0:
            return 0
        time.sleep(args.interval_seconds)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
