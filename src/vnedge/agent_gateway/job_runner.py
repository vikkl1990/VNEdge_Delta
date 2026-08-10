"""Research-only worker for Agent Gateway jobs.

The gateway records agent requests; this module is the safe executor that
turns those pending requests into backtest evidence. It has no exchange keys,
no order adapter, no promotion path, and every terminal payload is stamped
``can_trade=false`` / ``can_promote=false``.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from inspect import Parameter, signature
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from vnedge.agent_gateway.jobs import (
    BLOCKED_STATUS,
    DONE_STATUS,
    FAILED_STATUS,
    claim_job,
    pending_jobs,
    update_job,
)
from vnedge.backtest.backtester import BacktestConfig, Trade, run_backtest
from vnedge.backtest.fee_model import FeeModel
from vnedge.backtest.metrics import BacktestMetrics, compute_metrics
from vnedge.backtest.slippage_model import SlippageModel
from vnedge.data.parquet_store import ParquetStore
from vnedge.research.ai_candidate_research import (
    AI_STRATEGY_DIR,
    run_ai_candidate_research,
)
from vnedge.research.candidate_replay_executor import (
    EXECUTOR_ID as CANDIDATE_REPLAY_EXECUTOR_ID,
    DEFAULT_EVENT_LEADLAG,
    DEFAULT_ORDERFLOW,
    CandidateReplayConfig,
    run_candidate_replay,
)
from vnedge.strategy.ai_sandbox import AI_STRATEGY_ID_PREFIX
from vnedge.strategy.strategy_registry import get_strategy_class

logger = logging.getLogger(__name__)

RUNNER_VERSION = "agent_job_runner_v1"
CONFIG_PARAMETER_KEYS = frozenset({"max_holding_bars"})
CANDIDATE_REPLAY_ALIASES = frozenset(
    {CANDIDATE_REPLAY_EXECUTOR_ID, "candidate_replay", "candidate_replay_executor"}
)


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class JobOutcome:
    status: str
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    blocked_reason: str | None = None


JobExecutor = Callable[[dict[str, Any]], JobOutcome]


def execute_job(
    job: dict[str, Any],
    *,
    data_root: Path | str = "data",
    artifact_dir: Path | str | None = "research/live_research/agent_jobs",
) -> JobOutcome:
    if job.get("kind") != "backtest_request":
        return JobOutcome(
            status=BLOCKED_STATUS,
            blocked_reason=f"unsupported job kind: {job.get('kind')}",
            result=_base_result(job),
        )
    request = job.get("request")
    if not isinstance(request, dict):
        return JobOutcome(
            status=BLOCKED_STATUS,
            blocked_reason="missing request payload",
            result=_base_result(job),
        )
    guard = _guard_request(request)
    if guard is not None:
        return JobOutcome(status=BLOCKED_STATUS, blocked_reason=guard, result=_base_result(job))

    strategy_id = str(request["strategy_id"])
    if _is_candidate_replay_request(request):
        return _execute_candidate_replay_job(job, data_root=data_root)
    if strategy_id.startswith(AI_STRATEGY_ID_PREFIX):
        return _execute_ai_candidate_job(job, data_root=data_root, artifact_dir=artifact_dir)
    return _execute_registered_strategy_job(job, data_root=data_root)


def run_pending_jobs(
    *,
    jobs_dir: Path | str,
    data_root: Path | str = "data",
    artifact_dir: Path | str | None = "research/live_research/agent_jobs",
    max_jobs: int = 1,
    executor: JobExecutor | None = None,
) -> list[dict[str, Any]]:
    jobs_path = Path(jobs_dir)
    completed: list[dict[str, Any]] = []
    for pending in pending_jobs(jobs_path, limit=max_jobs):
        running = claim_job(jobs_path, str(pending["job_id"]))
        if running is None:
            continue
        try:
            outcome = (
                executor(running)
                if executor is not None
                else execute_job(running, data_root=data_root, artifact_dir=artifact_dir)
            )
        except Exception as exc:  # noqa: BLE001 - one bad job must not stop the worker
            logger.exception("agent job %s failed unexpectedly", running["job_id"])
            outcome = JobOutcome(
                status=FAILED_STATUS,
                error=str(exc),
                result=_base_result(running),
            )

        result = _research_only_payload(outcome.result)
        terminal_doc = {
            **running,
            "status": outcome.status,
            "updated_at": datetime.now(UTC).isoformat(),
            "result": result,
        }
        if outcome.error is not None:
            terminal_doc["error"] = outcome.error
        if outcome.blocked_reason is not None:
            terminal_doc["blocked_reason"] = outcome.blocked_reason
        if artifact_dir is not None:
            result["artifact_path"] = str(
                _write_artifact(Path(artifact_dir), str(running["job_id"]), terminal_doc)
            )

        updated = update_job(
            jobs_path,
            str(running["job_id"]),
            status=outcome.status,
            result=result,
            error=outcome.error,
            blocked_reason=outcome.blocked_reason,
        )
        if updated is not None:
            completed.append(updated)
    return completed


def _execute_registered_strategy_job(
    job: dict[str, Any],
    *,
    data_root: Path | str,
) -> JobOutcome:
    request = job["request"]
    strategy_id = str(request["strategy_id"])
    try:
        strategy_cls = get_strategy_class(strategy_id)
    except KeyError as exc:
        return JobOutcome(
            status=BLOCKED_STATUS,
            blocked_reason=str(exc),
            result=_base_result(job),
        )

    try:
        candles, funding, source = _load_market_window(
            data_root=Path(data_root),
            exchange=str(request["exchange"]),
            symbol=str(request["symbol"]),
            timeframe=str(request["timeframe"]),
            start=request.get("start"),
            end=request.get("end"),
        )
    except (FileNotFoundError, KeyError, ValueError) as exc:
        return JobOutcome(
            status=BLOCKED_STATUS,
            blocked_reason=f"market data unavailable: {exc}",
            result=_base_result(job),
        )

    params, ignored = _strategy_parameters(strategy_cls, request.get("parameters", {}))
    try:
        strategy = _build_strategy(strategy_cls, funding=funding, params=params)
    except (TypeError, ValueError) as exc:
        return JobOutcome(
            status=BLOCKED_STATUS,
            blocked_reason=f"strategy construction refused: {exc}",
            result={
                **_base_result(job),
                "accepted_parameters": params,
                "ignored_parameters": ignored,
            },
        )

    try:
        result = run_backtest(
            candles,
            funding,
            strategy,
            _backtest_config(request),
            symbol=str(request["symbol"]),
            timeframe=str(request["timeframe"]),
        )
        metrics = compute_metrics(result)
    except Exception as exc:  # noqa: BLE001 - persisted as job failure evidence
        return JobOutcome(
            status=FAILED_STATUS,
            error=str(exc),
            result={
                **_base_result(job),
                "accepted_parameters": params,
                "ignored_parameters": ignored,
                "bars": int(len(candles)),
                "data_source": source,
            },
        )

    payload = {
        **_base_result(job),
        "execution": "registered_strategy_backtest",
        "data_source": source,
        "bars": int(len(candles)),
        "window": _window_payload(candles),
        "accepted_parameters": params,
        "ignored_parameters": ignored,
        "metrics": _metrics_payload(metrics),
        "sample_trades": [_trade_payload(t) for t in result.trades[-20:]],
        "promotion_verdict": "NOT_EVALUATED_AGENT_JOB",
        "promotion_note": (
            "Agent jobs produce exploratory research evidence only. Promotion "
            "still requires the normal untouched judgment and human approval."
        ),
    }
    return JobOutcome(status=DONE_STATUS, result=payload)


def _execute_ai_candidate_job(
    job: dict[str, Any],
    *,
    data_root: Path | str,
    artifact_dir: Path | str | None,
) -> JobOutcome:
    request = job["request"]
    try:
        candles, funding, source = _load_market_window(
            data_root=Path(data_root),
            exchange=str(request["exchange"]),
            symbol=str(request["symbol"]),
            timeframe=str(request["timeframe"]),
            start=request.get("start"),
            end=request.get("end"),
        )
    except (FileNotFoundError, KeyError, ValueError) as exc:
        return JobOutcome(
            status=BLOCKED_STATUS,
            blocked_reason=f"market data unavailable: {exc}",
            result=_base_result(job),
        )

    try:
        train_bars = _bounded_int(
            (request.get("parameters") or {}).get("train_bars"),
            default=1440,
            lower=100,
            upper=20_000,
        )
        test_bars = _bounded_int(
            (request.get("parameters") or {}).get("test_bars"),
            default=720,
            lower=50,
            upper=10_000,
        )
        payload = run_ai_candidate_research(
            candles,
            funding,
            strategy_dir=AI_STRATEGY_DIR,
            exchange=str(request["exchange"]),
            symbol=str(request["symbol"]),
            timeframe=str(request["timeframe"]),
            dataset_source=source,
            train_bars=train_bars,
            test_bars=test_bars,
        )
    except Exception as exc:  # noqa: BLE001 - AI research failure is evidence
        return JobOutcome(status=FAILED_STATUS, error=str(exc), result=_base_result(job))

    strategy_id = str(request["strategy_id"])
    matches = [row for row in payload.get("candidates", []) if row.get("strategy_id") == strategy_id]
    result = {
        **_base_result(job),
        "execution": "ai_candidate_research",
        "ai_payload": payload,
        "matched_candidate": matches[0] if matches else None,
        "promotion_verdict": "NOT_EVALUATED_AGENT_JOB",
        "promotion_note": (
            "AI candidates remain sandboxed research artifacts. A CANDIDATE row "
            "is not a paper/shadow/live promotion."
        ),
    }
    if artifact_dir is not None:
        result["ai_candidates_artifact_dir"] = str(artifact_dir)
    if not matches:
        return JobOutcome(
            status=BLOCKED_STATUS,
            blocked_reason=f"AI strategy {strategy_id!r} was not loaded from {AI_STRATEGY_DIR}",
            result=result,
        )
    return JobOutcome(status=DONE_STATUS, result=result)


def _execute_candidate_replay_job(
    job: dict[str, Any],
    *,
    data_root: Path | str,
) -> JobOutcome:
    request = job["request"]
    params = request.get("parameters") if isinstance(request.get("parameters"), dict) else {}
    config = CandidateReplayConfig(
        max_event_leadlag_specs=_bounded_int(
            params.get("max_event_leadlag_specs") or params.get("max_event_leadlag"),
            default=3,
            lower=0,
            upper=50,
        ),
        max_orderflow_specs=_bounded_int(
            params.get("max_orderflow_specs") or params.get("max_orderflow"),
            default=20,
            lower=0,
            upper=250,
        ),
        min_replay_fills=_bounded_int(
            params.get("min_replay_fills"),
            default=5,
            lower=1,
            upper=10_000,
        ),
        queue_aware=bool(params.get("queue_aware", False)),
        notional_usd=_bounded_float(
            params.get("notional_usd"),
            default=100.0,
            lower=1.0,
            upper=100_000.0,
        ),
    )
    try:
        payload = run_candidate_replay(
            data_root,
            event_leadlag_path=DEFAULT_EVENT_LEADLAG,
            orderflow_path=DEFAULT_ORDERFLOW,
            config=config,
        )
    except Exception as exc:  # noqa: BLE001 - replay failure is persisted evidence
        return JobOutcome(
            status=FAILED_STATUS,
            error=str(exc),
            result={
                **_base_result(job),
                "execution": "candidate_replay",
                "config": config.to_dict(),
            },
        )

    result = {
        **_base_result(job),
        "execution": "candidate_replay",
        "config": config.to_dict(),
        "summary": payload.get("summary", {}),
        "top_rows": list(payload.get("rows", []))[:20],
        "candidate_replay_payload": payload,
        "promotion_verdict": "NOT_EVALUATED_AGENT_JOB",
        "promotion_note": (
            "Candidate replay rows are proof tasks only. REPLAY_CANDIDATE "
            "still requires untouched judgment, paper/shadow trial, and "
            "explicit human approval before promotion."
        ),
    }
    return JobOutcome(status=DONE_STATUS, result=result)


def _guard_request(request: dict[str, Any]) -> str | None:
    required = ("strategy_id", "exchange", "symbol", "timeframe")
    missing = [key for key in required if not request.get(key)]
    if missing:
        return f"missing required request fields: {missing}"
    if request.get("live_orders_enabled"):
        return "agent job requested live_orders_enabled=true"
    if request.get("strict_mode") is not True:
        return "agent job must keep strict_mode=true"
    return None


def _is_candidate_replay_request(request: dict[str, Any]) -> bool:
    params = request.get("parameters") if isinstance(request.get("parameters"), dict) else {}
    adapter = str(params.get("adapter") or params.get("job_adapter") or "")
    return str(request.get("strategy_id")) in CANDIDATE_REPLAY_ALIASES or adapter in {
        "candidate_replay",
        CANDIDATE_REPLAY_EXECUTOR_ID,
    }


def _load_market_window(
    *,
    data_root: Path,
    exchange: str,
    symbol: str,
    timeframe: str,
    start: Any,
    end: Any,
) -> tuple[pd.DataFrame, pd.DataFrame | None, str]:
    store = ParquetStore(data_root)
    candles = _normalize_frame(store.read_candles(exchange, symbol, timeframe), "candles")
    try:
        funding = _normalize_frame(store.read_funding(exchange, symbol), "funding")
    except FileNotFoundError:
        funding = None

    start_ts = _parse_ts(start)
    end_ts = _parse_ts(end)
    if start_ts is not None:
        candles = candles[candles["timestamp"] >= start_ts]
        if funding is not None:
            funding = funding[funding["timestamp"] >= start_ts]
    if end_ts is not None:
        candles = candles[candles["timestamp"] <= end_ts]
        if funding is not None:
            funding = funding[funding["timestamp"] <= end_ts]
    candles = candles.reset_index(drop=True)
    funding = None if funding is None else funding.reset_index(drop=True)
    if candles.empty:
        raise ValueError("window has zero candle bars")
    return candles, funding, str(store.candles_path(exchange, symbol, timeframe))


def _normalize_frame(frame: pd.DataFrame, label: str) -> pd.DataFrame:
    if frame.empty:
        raise ValueError(f"{label} frame is empty")
    df = frame.copy()
    if "timestamp" not in df:
        raise KeyError(f"{label} frame has no timestamp column")
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.drop_duplicates(subset="timestamp", keep="last")
    return df.sort_values("timestamp").reset_index(drop=True)


def _parse_ts(value: Any) -> pd.Timestamp | None:
    if value in (None, ""):
        return None
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def _strategy_parameters(strategy_cls: type, raw: Any) -> tuple[dict[str, Any], list[str]]:
    if not isinstance(raw, dict):
        return {}, []
    sig = signature(strategy_cls.__init__)
    allowed = {
        name
        for name, param in sig.parameters.items()
        if name not in {"self", "funding"}
        and param.kind in {Parameter.POSITIONAL_OR_KEYWORD, Parameter.KEYWORD_ONLY}
    }
    params = {key: value for key, value in raw.items() if key in allowed}
    ignored = sorted(
        key for key in raw if key not in allowed and key not in CONFIG_PARAMETER_KEYS
    )
    return params, ignored


def _build_strategy(strategy_cls: type, *, funding: pd.DataFrame | None, params: dict[str, Any]):
    sig = signature(strategy_cls.__init__)
    if "funding" in sig.parameters:
        return strategy_cls(funding=funding, **params)
    return strategy_cls(**params)


def _backtest_config(request: dict[str, Any]) -> BacktestConfig:
    params = request.get("parameters") if isinstance(request.get("parameters"), dict) else {}
    fee = (
        FeeModel(maker_bps=float(request["commission_bps"]), taker_bps=float(request["commission_bps"]))
        if request.get("commission_bps") is not None
        else FeeModel()
    )
    slippage = (
        SlippageModel(bps=float(request["slippage_bps"]))
        if request.get("slippage_bps") is not None
        else SlippageModel()
    )
    return BacktestConfig(
        initial_equity_usd=float(request.get("initial_capital_usd", 500.0)),
        max_holding_bars=_bounded_int(params.get("max_holding_bars"), default=48, lower=1, upper=10_000),
        fees=fee,
        slippage=slippage,
    )


def _bounded_int(value: Any, *, default: int, lower: int, upper: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(lower, min(parsed, upper))


def _bounded_float(value: Any, *, default: float, lower: float, upper: float) -> float:
    if value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed):
        return default
    return max(lower, min(parsed, upper))


def _base_result(job: dict[str, Any]) -> dict[str, Any]:
    request = job.get("request") if isinstance(job.get("request"), dict) else {}
    return _research_only_payload(
        {
            "runner": RUNNER_VERSION,
            "job_id": job.get("job_id"),
            "kind": job.get("kind"),
            "strategy_id": request.get("strategy_id"),
            "exchange": request.get("exchange"),
            "symbol": request.get("symbol"),
            "timeframe": request.get("timeframe"),
            "created_by": job.get("created_by"),
            "generated_at": datetime.now(UTC).isoformat(),
        }
    )


def _research_only_payload(payload: dict[str, Any]) -> dict[str, Any]:
    clean = _jsonable(dict(payload))
    clean["can_trade"] = False
    clean["can_promote"] = False
    clean["live_orders_enabled"] = False
    return clean


def _metrics_payload(metrics: BacktestMetrics) -> dict[str, Any]:
    return _jsonable(metrics.to_dict())


def _trade_payload(trade: Trade) -> dict[str, Any]:
    return _jsonable(
        {
            "side": trade.side,
            "quantity": trade.quantity,
            "entry_ts": trade.entry_ts.isoformat(),
            "entry_price": trade.entry_price,
            "exit_ts": trade.exit_ts.isoformat(),
            "exit_price": trade.exit_price,
            "exit_reason": trade.exit_reason,
            "net_pnl_usd": trade.net_pnl_usd,
            "fees_usd": trade.fees_usd,
            "funding_usd": trade.funding_usd,
            "entry_reason": trade.entry_reason,
        }
    )


def _window_payload(candles: pd.DataFrame) -> dict[str, Any]:
    return {
        "start": candles["timestamp"].iloc[0].isoformat(),
        "end": candles["timestamp"].iloc[-1].isoformat(),
    }


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _write_artifact(artifact_dir: Path, job_id: str, payload: dict[str, Any]) -> Path:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    path = artifact_dir / f"{job_id}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(_jsonable(payload), indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    return path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Agent Gateway research-only jobs")
    parser.add_argument(
        "--jobs-dir",
        default=os.environ.get("AGENT_GATEWAY_JOBS_DIR", "logs/agent_gateway/jobs"),
    )
    parser.add_argument("--data-root", default=os.environ.get("AGENT_JOB_RUNNER_DATA_ROOT", "data"))
    parser.add_argument(
        "--artifact-dir",
        default=os.environ.get("AGENT_JOB_RUNNER_ARTIFACT_DIR", "research/live_research/agent_jobs"),
    )
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=float(os.environ.get("AGENT_JOB_RUNNER_INTERVAL_SECONDS", "60")),
    )
    parser.add_argument(
        "--max-per-cycle",
        type=int,
        default=int(os.environ.get("AGENT_JOB_RUNNER_MAX_PER_CYCLE", "1")),
    )
    parser.add_argument(
        "--seed-defaults",
        action="store_true",
        default=_truthy(os.environ.get("AGENT_JOB_RUNNER_SEED_DEFAULTS")),
        help="Idempotently seed the default Quant OS research jobs before polling",
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args(argv)
    if args.seed_defaults:
        from vnedge.agent_gateway.seed_jobs import seed_default_jobs

        seeded = seed_default_jobs(Path(args.jobs_dir))
        if args.json and (args.once or seeded["created_count"]):
            print(json.dumps({"seeded": seeded}, indent=2, sort_keys=True))
        if seeded["created_count"]:
            logger.info("seeded %d Quant OS research job(s)", seeded["created_count"])
    while True:
        completed = run_pending_jobs(
            jobs_dir=args.jobs_dir,
            data_root=args.data_root,
            artifact_dir=args.artifact_dir,
            max_jobs=max(1, args.max_per_cycle),
        )
        if args.json and completed:
            print(json.dumps(completed, indent=2))
        if completed:
            logger.info("agent job runner completed %d job(s)", len(completed))
        if args.once:
            return 0
        time.sleep(max(1.0, args.interval_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
