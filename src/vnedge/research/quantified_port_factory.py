"""Research task factory for the QuantifiedStrategies title inventory.

The 95-title lab is only an intake surface. This module turns that intake into
durable Quant OS research tasks: one task per VNEDGE-owned port family, with
clear replay scope, feature work, and proof gates. It is still research-only;
no task can trade or promote a lane.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from vnedge.agent_gateway.task_registry import (
    QuantOSAgentGateway,
    env_quant_os_agent_gateway_dir,
)
from vnedge.research.quantified_strategy_lab import (
    QUANTIFIED_STRATEGY_LAB_ID,
    load_quantified_strategy_lab_payload,
)


QUANTIFIED_PORT_FACTORY_ID = "quantified_port_factory_v1"
DEFAULT_LAB = Path("research/live_research/quantified_strategy_lab_latest.json")
DEFAULT_OUT = Path("research/live_research/quantified_port_factory_latest.json")
DEFAULT_FEED = Path("research/live_research/quantified_port_factory_feed.jsonl")

CHUNK_ORDER: dict[str, int] = {"A": 10, "B": 20, "C": 30, "D": 40, "Q": 90}
SYNCABLE_CHUNKS = frozenset({"A", "B", "C", "D"})


@dataclass(frozen=True)
class QuantifiedPortFactoryConfig:
    chunks: tuple[str, ...] = ("A", "B", "C", "D")
    sync_gateway: bool = True
    max_title_examples: int = 6
    min_net_bps: float = 25.0
    min_profit_factor: float = 1.50
    min_trades: int = 20

    def __post_init__(self) -> None:
        bad = [chunk for chunk in self.chunks if chunk not in CHUNK_ORDER]
        if bad:
            raise ValueError(f"unknown chunks: {bad}")
        if self.max_title_examples < 1:
            raise ValueError("max_title_examples must be positive")
        if self.min_net_bps <= 0:
            raise ValueError("min_net_bps must be positive")
        if self.min_profit_factor < 1.0:
            raise ValueError("min_profit_factor must be >= 1")
        if self.min_trades < 1:
            raise ValueError("min_trades must be positive")


def build_quantified_port_factory_payload(
    *,
    lab_payload: dict[str, Any] | None = None,
    config: QuantifiedPortFactoryConfig = QuantifiedPortFactoryConfig(),
    gateway_dir: Path | str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    generated = now or datetime.now(UTC)
    lab = lab_payload or load_quantified_strategy_lab_payload(DEFAULT_LAB)
    reviews = _reviews(lab)
    chunks = _chunk_map(lab)
    blueprints = _blueprints(
        reviews=reviews,
        chunks=chunks,
        config=config,
    )
    gateway_summary = (
        _sync_gateway(blueprints, gateway_dir=gateway_dir)
        if config.sync_gateway
        else _gateway_disabled(gateway_dir)
    )
    payload = {
        "factory_id": QUANTIFIED_PORT_FACTORY_ID,
        "generated_at": generated.isoformat(),
        "source_lab": {
            "lab_id": lab.get("lab_id") or QUANTIFIED_STRATEGY_LAB_ID,
            "strategy_count": (lab.get("summary") or {}).get("total_strategies"),
            "source_policy": (lab.get("source") or {}).get("source_policy"),
        },
        "config": asdict(config),
        "summary": _summary(blueprints, gateway_summary),
        "blueprints": blueprints,
        "gateway": gateway_summary,
        "operator_answer": _operator_answer(blueprints),
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
    }
    return payload


def publish_quantified_port_factory(
    payload: dict[str, Any],
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
    tmp_path.replace(out_path)
    if feed is not None:
        feed_path = Path(feed)
        feed_path.parent.mkdir(parents=True, exist_ok=True)
        with feed_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_feed_record(payload), sort_keys=True) + "\n")
    return out_path


def load_quantified_port_factory_payload(path: Path | None = None) -> dict:
    if path is not None and path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {}
        if isinstance(payload, dict) and payload.get("factory_id") == QUANTIFIED_PORT_FACTORY_ID:
            return payload
    return build_quantified_port_factory_payload(
        config=QuantifiedPortFactoryConfig(sync_gateway=False)
    )


def _reviews(lab: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    rows = lab.get("strategy_reviews") or []
    return tuple(row for row in rows if isinstance(row, dict))


def _chunk_map(lab: dict[str, Any]) -> dict[int, str]:
    mapping: dict[int, str] = {}
    for chunk in lab.get("fast_track") or []:
        if not isinstance(chunk, dict):
            continue
        name = str(chunk.get("chunk") or "")
        if name not in CHUNK_ORDER:
            continue
        for n in chunk.get("strategy_numbers") or []:
            try:
                mapping[int(n)] = name
            except (TypeError, ValueError):
                continue
    return mapping


def _blueprints(
    *,
    reviews: tuple[dict[str, Any], ...],
    chunks: dict[int, str],
    config: QuantifiedPortFactoryConfig,
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in reviews:
        number = _int(row.get("strategy_number"))
        chunk = chunks.get(number, "Q")
        port = str(row.get("recommended_port") or "unknown")
        groups[(chunk, port)].append(row)

    selected_chunks = set(config.chunks)
    blueprints = [
        _blueprint(chunk=chunk, port=port, rows=tuple(rows), config=config)
        for (chunk, port), rows in groups.items()
        if chunk in selected_chunks and port != "wait_for_release_or_public_rules"
    ]
    return sorted(
        blueprints,
        key=lambda row: (
            int(row["run_order"]),
            -int(row["source_count"]),
            str(row["recommended_port"]),
        ),
    )


def _blueprint(
    *,
    chunk: str,
    port: str,
    rows: tuple[dict[str, Any], ...],
    config: QuantifiedPortFactoryConfig,
) -> dict[str, Any]:
    ordered = sorted(
        rows,
        key=lambda row: (-_int(row.get("crypto_fit_score")), _int(row.get("strategy_number"))),
    )
    strategy_numbers = tuple(_int(row.get("strategy_number")) for row in ordered)
    title_examples = tuple(str(row.get("title") or "") for row in ordered[:config.max_title_examples])
    priority = _priority(chunk, ordered)
    blueprint = {
        "blueprint_id": _blueprint_id(chunk, port),
        "chunk": chunk,
        "chunk_name": _chunk_name(chunk),
        "run_order": CHUNK_ORDER[chunk],
        "priority": priority,
        "recommended_port": port,
        "source_count": len(rows),
        "strategy_numbers": list(strategy_numbers),
        "title_examples": list(title_examples),
        "source_policy": "title_only_research_no_paid_or_proprietary_rules",
        "status": _blueprint_status(chunk),
        "task_kind": f"quantified_port_factory.{port}",
        "objective": _objective(port, title_examples),
        "causal_port_contract": {
            "must_write_vnedge_owned_rules": True,
            "may_use_titles_as_inspiration_only": True,
            "copy_paid_or_proprietary_rules": False,
            "must_be_causal": True,
            "must_pass_bias_audit": True,
        },
        "feature_work": _feature_work(port),
        "first_replay": _first_replay(port),
        "replay_matrix": _replay_matrix(port, chunk),
        "execution_replay": {
            "maker_first": True,
            "taker_fallback": "allowed_only_when_expected_net_covers_fees_slippage_and_buffer",
            "compare_exits": [
                "classic_tp3_hold",
                "tp1_partial_be_then_trail",
                "time_stop_after_edge_decay",
            ],
            "fee_wall_required": True,
        },
        "promotion_gate": {
            "min_net_bps": config.min_net_bps,
            "min_profit_factor": config.min_profit_factor,
            "min_trades": config.min_trades,
            "untouched_window_required": True,
        },
        "next_action": _next_action(chunk, port),
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
    }
    blueprint["blueprint_hash"] = _stable_hash(blueprint)
    return blueprint


def _priority(chunk: str, rows: tuple[dict[str, Any], ...]) -> int:
    best_fit = max((_int(row.get("crypto_fit_score")) for row in rows), default=0)
    source_boost = min(15, len(rows))
    return max(1, min(100, 110 - CHUNK_ORDER[chunk] + source_boost + best_fit // 20))


def _blueprint_id(chunk: str, port: str) -> str:
    return f"quantified|{chunk}|{port}"


def _chunk_name(chunk: str) -> str:
    return {
        "A": "crypto native + breakout/pullback fast track",
        "B": "indicator atoms as edge-model features",
        "C": "crypto session/calendar miner",
        "D": "cross-pair relative strength rotation",
        "Q": "quarantine / asset-specific review",
    }[chunk]


def _blueprint_status(chunk: str) -> str:
    if chunk in SYNCABLE_CHUNKS:
        return "READY_FOR_AGENT_CAUSAL_PORT"
    return "QUARANTINE_REVIEW_ONLY"


def _objective(port: str, examples: tuple[str, ...]) -> str:
    example = examples[0] if examples else port
    return f"Build VNEDGE-owned {port} causal port from title family: {example}"


def _feature_work(port: str) -> tuple[str, ...]:
    mapping: dict[str, tuple[str, ...]] = {
        "bitcoin_crypto_strategy_pack_v1": (
            "BTC/ETH/SOL/XRP 5m/15m/1h regime alignment",
            "breakout, pullback, session, and funding primitives",
            "active TP1/BE/trailing exit comparison",
        ),
        "range_volatility_breakout_reversion_v1": (
            "ATR compression and range-width percentile",
            "close-confirmed break plus retest/rejection variant",
            "volume impulse and room-to-liquidity filters",
        ),
        "pullback_reversion_pack_v1": (
            "IBS/Williams/RSI pullback percentile",
            "HTF trend permission and BBP/volume confirmation",
            "structural stop with TP ladder and time stop",
        ),
        "indicator_pack_mtf_v1": (
            "RSI, stochastic, MACD histogram, Bollinger, MFI, ADX, DMI atoms",
            "no standalone hard-gate promotion",
            "feed atoms into edge_model_v1 and compare OOS uplift",
        ),
        "crypto_session_calendar_miner_v1": (
            "UTC day boundary, Asia/London/NY windows, weekend liquidity",
            "funding timestamp and month-turn feature grid",
            "session-specific maker/taker outcome attribution",
        ),
        "crypto_relative_strength_rotation_v1": (
            "cross-pair 1h/4h momentum and volatility ranks",
            "liquidity/spread/depth eligibility",
            "15m trigger only after rank persistence",
        ),
        "trend_momentum_pack_v1": (
            "EMA200/golden-cross/momentum/ADX context",
            "5m/15m trigger under 1h/4h trend permission",
            "edge decay and trail-stop capture test",
        ),
        "price_action_structure_pack_v1": (
            "candlestick, Heikin, swing break, sweep, CHoCH/FVG proxies",
            "zone rejection with displacement confirmation",
            "structural stop and room-to-next-liquidity check",
        ),
        "short_tail_risk_pack_v1": (
            "short-only panic/tail-risk filters",
            "volatility expansion plus liquidity vacuum confirmation",
            "hard max loss, time stop, and no averaging down",
        ),
        "ensemble_blend_lab_v1": (
            "blend trend, pullback, session, and volatility ports",
            "model-router selection instead of majority voting",
            "OOS lift against each raw component",
        ),
        "swing_template_crypto_rebuild_v1": (
            "generic swing entry/exit template",
            "HTF bias, liquidity room, volatility regime",
            "start at 15m/1h before any 5m fee-wall attempt",
        ),
    }
    return mapping.get(port, ("write causal feature contract", "run replay", "audit failures"))


def _first_replay(port: str) -> str:
    mapping = {
        "bitcoin_crypto_strategy_pack_v1": "BTC/USDT and ETH/USDT on 15m/1h first, then 5m.",
        "range_volatility_breakout_reversion_v1": "BTC/ETH/SOL/XRP 5m/15m compression-breakout replay.",
        "pullback_reversion_pack_v1": "BTC/ETH/SOL 5m/15m pullback replay with HTF permission.",
        "indicator_pack_mtf_v1": "feature-bank OOS uplift against raw scanner baseline.",
        "crypto_session_calendar_miner_v1": "BTC/ETH/SOL 15m/1h by UTC/session/funding windows.",
        "crypto_relative_strength_rotation_v1": "liquid perp universe 1h/4h rank persistence then 15m trigger.",
    }
    return mapping.get(port, "15m/1h before lower-timeframe fee-wall tests.")


def _replay_matrix(port: str, chunk: str) -> dict[str, Any]:
    if chunk == "Q":
        return {"status": "blocked_quarantine", "timeframes": [], "venues": [], "pairs": []}
    if port == "crypto_session_calendar_miner_v1":
        return {
            "timeframes": ["15m", "1h", "4h"],
            "venues": ["binanceusdm", "bybit", "delta_india"],
            "pairs": ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT"],
        }
    if port == "crypto_relative_strength_rotation_v1":
        return {
            "timeframes": ["15m", "1h", "4h"],
            "venues": ["binanceusdm", "bybit"],
            "pairs": ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "DOGE/USDT", "BNB/USDT"],
        }
    return {
        "timeframes": ["5m", "15m", "1h", "4h"],
        "venues": ["binanceusdm", "bybit", "delta_india"],
        "pairs": ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT"],
    }


def _next_action(chunk: str, port: str) -> str:
    if chunk == "A":
        return "BUILD_CAUSAL_PORT_AND_RUN_FIRST_REPLAY"
    if chunk == "B":
        return "ADD_FEATURE_ATOMS_TO_EDGE_MODEL_AND_COMPARE_OOS_LIFT"
    if chunk == "C":
        return "MINE_CRYPTO_SESSION_WINDOWS_AND_REPLAY"
    if chunk == "D":
        return "BUILD_RELATIVE_STRENGTH_RANKER_AND_REPLAY"
    return f"KEEP_{port.upper()}_IN_QUARANTINE_UNTIL_CRYPTO_THESIS_EXISTS"


def _sync_gateway(
    blueprints: list[dict[str, Any]],
    *,
    gateway_dir: Path | str | None,
) -> dict[str, Any]:
    gateway = QuantOSAgentGateway(
        Path(gateway_dir) if gateway_dir is not None else env_quant_os_agent_gateway_dir()
    )
    snapshot = gateway.snapshot(limit=100_000)
    tasks_by_blueprint = _tasks_by_blueprint(snapshot)
    artifact_keys = _artifact_keys(snapshot)
    created = 0
    reused = 0
    artifacts_registered = 0
    artifacts_skipped = 0
    task_refs: list[dict[str, str]] = []
    for blueprint in blueprints:
        if blueprint["chunk"] not in SYNCABLE_CHUNKS:
            continue
        blueprint_id = str(blueprint["blueprint_id"])
        task = tasks_by_blueprint.get(blueprint_id)
        if task is None:
            task = gateway.create_task(
                kind=str(blueprint["task_kind"]),
                objective=str(blueprint["objective"]),
                requested_by=QUANTIFIED_PORT_FACTORY_ID,
                priority=int(blueprint["priority"]),
                target={
                    "recommended_port": blueprint["recommended_port"],
                    "chunk": blueprint["chunk"],
                    "strategy_numbers": blueprint["strategy_numbers"],
                },
                payload={
                    "quantified_port_factory": {
                        "blueprint_id": blueprint_id,
                        "blueprint_hash": blueprint["blueprint_hash"],
                        "research_only": True,
                    }
                },
            )
            created += 1
        else:
            reused += 1
        task_id = str(task["task_id"])
        blueprint["task_id"] = task_id
        artifact_key = str(blueprint["blueprint_hash"])
        if (task_id, artifact_key) in artifact_keys:
            artifacts_skipped += 1
        else:
            gateway.register_content_artifact(
                task_id,
                artifact_type="quantified_port_blueprint",
                summary=f"{blueprint['chunk']} {blueprint['recommended_port']}",
                content=blueprint,
                metadata={
                    "artifact_key": artifact_key,
                    "blueprint_id": blueprint_id,
                    "recommended_port": blueprint["recommended_port"],
                    "research_only": True,
                },
            )
            artifacts_registered += 1
        task_refs.append({"blueprint_id": blueprint_id, "task_id": task_id})
    gateway.write_snapshot()
    return {
        "root": str(gateway.root),
        "tasks_created": created,
        "tasks_reused": reused,
        "artifacts_registered": artifacts_registered,
        "artifacts_skipped": artifacts_skipped,
        "task_refs": task_refs,
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
    }


def _tasks_by_blueprint(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    tasks: dict[str, dict[str, Any]] = {}
    for task in snapshot.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
        factory = payload.get("quantified_port_factory") if isinstance(payload.get("quantified_port_factory"), dict) else {}
        blueprint_id = str(factory.get("blueprint_id") or "")
        if blueprint_id:
            tasks[blueprint_id] = task
    return tasks


def _artifact_keys(snapshot: dict[str, Any]) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    artifacts = snapshot.get("artifacts") if isinstance(snapshot.get("artifacts"), dict) else {}
    for artifact in artifacts.get("recent") or []:
        if not isinstance(artifact, dict):
            continue
        metadata = artifact.get("metadata") if isinstance(artifact.get("metadata"), dict) else {}
        artifact_key = str(metadata.get("artifact_key") or "")
        task_id = str(artifact.get("task_id") or "")
        if artifact_key and task_id:
            keys.add((task_id, artifact_key))
    return keys


def _gateway_disabled(gateway_dir: Path | str | None) -> dict[str, Any]:
    return {
        "root": str(gateway_dir or env_quant_os_agent_gateway_dir()),
        "tasks_created": 0,
        "tasks_reused": 0,
        "artifacts_registered": 0,
        "artifacts_skipped": 0,
        "task_refs": [],
        "disabled": True,
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
    }


def _summary(blueprints: list[dict[str, Any]], gateway: dict[str, Any]) -> dict[str, Any]:
    chunks = Counter(str(row.get("chunk") or "") for row in blueprints)
    ports = Counter(str(row.get("recommended_port") or "") for row in blueprints)
    strategy_coverage = len(
        {
            int(n)
            for row in blueprints
            for n in row.get("strategy_numbers", [])
            if isinstance(n, int)
        }
    )
    return {
        "blueprints": len(blueprints),
        "strategy_coverage": strategy_coverage,
        "chunks": dict(sorted(chunks.items())),
        "ports": dict(ports.most_common()),
        "syncable_blueprints": sum(1 for row in blueprints if row.get("chunk") in SYNCABLE_CHUNKS),
        "gateway_tasks_created": gateway.get("tasks_created", 0),
        "gateway_tasks_reused": gateway.get("tasks_reused", 0),
        "gateway_artifacts_registered": gateway.get("artifacts_registered", 0),
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
    }


def _operator_answer(blueprints: list[dict[str, Any]]) -> str:
    if not blueprints:
        return "No Quantified strategy port blueprints are queued."
    first = blueprints[0]
    return (
        "Next proof step is "
        f"{first['recommended_port']} from Chunk {first['chunk']}: build a VNEDGE-owned "
        "causal port, replay across the declared matrix, and reject it unless it clears "
        "25 bps net, PF 1.5, 20 trades, and untouched-window judgment."
    )


def _feed_record(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "ts": payload.get("generated_at"),
        "factory_id": payload.get("factory_id"),
        "summary": payload.get("summary"),
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
    }


def _stable_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish Quantified strategy port tasks")
    parser.add_argument("--lab", type=Path, default=DEFAULT_LAB)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--feed", type=Path, default=DEFAULT_FEED)
    parser.add_argument("--chunks", default="A,B,C,D")
    parser.add_argument("--gateway-dir", type=Path, default=None)
    parser.add_argument("--no-gateway-sync", action="store_true")
    parser.add_argument("--no-feed", action="store_true")
    args = parser.parse_args(argv)
    chunks = tuple(chunk.strip().upper() for chunk in args.chunks.split(",") if chunk.strip())
    config = QuantifiedPortFactoryConfig(
        chunks=chunks,
        sync_gateway=not args.no_gateway_sync,
    )
    payload = build_quantified_port_factory_payload(
        lab_payload=load_quantified_strategy_lab_payload(args.lab),
        config=config,
        gateway_dir=args.gateway_dir,
    )
    publish_quantified_port_factory(
        payload,
        out=args.out,
        feed=None if args.no_feed else args.feed,
    )
    summary = payload["summary"]
    print(
        "quantified port factory "
        f"{summary['blueprints']} blueprints / "
        f"{summary['gateway_tasks_created']} tasks created / "
        f"{summary['gateway_tasks_reused']} reused"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
