"""Preregistered causal replay for btc_eth_lead_lag_v1."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from collections import Counter, deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import fmean
from tempfile import NamedTemporaryFile

from vnedge.research.delta_scalper_backtest import _load_candles
from vnedge.scalping.delta_engine.backtester import (
    BacktestTrade,
    CausalScalperBacktester,
    OpenBacktestTrade,
)
from vnedge.scalping.delta_engine.candle_store import MultiTimeframeCandleStore
from vnedge.scalping.delta_engine.lead_lag import (
    BtcEthLeadLagConfig,
    BtcEthLeadLagScanner,
    LeadLagContext,
    lead_lag_fee_model,
    load_lead_lag_config,
)
from vnedge.scalping.delta_engine.types import Candle, SignalCandidate

DEFAULT_CONFIG = Path("configs/research/btc_eth_lead_lag_v1.yaml")
DEFAULT_CACHE = Path("data/delta_scalper_cache")
DEFAULT_OUTPUT = Path("research/live_research/btc_eth_lead_lag_v1_latest.json")


@dataclass(frozen=True)
class PairReplayResult:
    trades: tuple[BacktestTrade, ...]
    missing_minutes: int
    unresolved_dropped: int
    aligned_bars: int
    score_started_at: datetime | None
    score_ended_at: datetime | None


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def _parse_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def align_closed_pairs(
    btc: list[Candle],
    eth: list[Candle],
) -> list[tuple[Candle, Candle]]:
    btc_by_ts = {row.ts: row for row in btc}
    eth_by_ts = {row.ts: row for row in eth}
    return [(btc_by_ts[ts], eth_by_ts[ts]) for ts in sorted(btc_by_ts.keys() & eth_by_ts.keys())]


def _missing_between(previous: datetime, current: datetime) -> int:
    if current <= previous:
        raise ValueError("aligned candles must have unique ascending timestamps")
    return max(0, int((current - previous).total_seconds() // 60) - 1)


def simulate_pair_window(
    aligned: list[tuple[Candle, Candle]],
    config: BtcEthLeadLagConfig,
    *,
    score_start: datetime | None = None,
    score_end: datetime | None = None,
    process_end: datetime | None = None,
) -> PairReplayResult:
    fee_model = lead_lag_fee_model(config)
    scanner = BtcEthLeadLagScanner(config, fee_model)
    resolver = CausalScalperBacktester(
        None,  # type: ignore[arg-type] -- only the canonical exit resolver is used
        fee_model,
        MultiTimeframeCandleStore(),
    )
    history_size = max(scanner.minimum_history, 64)
    btc_history: deque[Candle] = deque(maxlen=history_size)
    eth_history: deque[Candle] = deque(maxlen=history_size)
    pending: SignalCandidate | None = None
    active: OpenBacktestTrade | None = None
    trades: list[BacktestTrade] = []
    previous_ts: datetime | None = None
    missing = 0
    dropped = 0
    processed = 0
    scored_timestamps: list[datetime] = []
    for btc_bar, eth_bar in aligned:
        if btc_bar.ts != eth_bar.ts:
            raise ValueError("pair timestamps must match")
        if process_end is not None and btc_bar.ts > process_end:
            break
        if previous_ts is not None:
            gap = _missing_between(previous_ts, btc_bar.ts)
            if gap:
                if score_start is None or btc_bar.ts >= score_start:
                    missing += gap
                dropped += int(pending is not None) + int(active is not None)
                pending = None
                active = None
                btc_history.clear()
                eth_history.clear()
                scanner.reset()
        previous_ts = btc_bar.ts
        processed += 1
        if pending is not None and active is None:
            active = OpenBacktestTrade(pending, eth_bar)
            pending = None
        if active is not None:
            resolved = resolver.resolve_on_bar(active, eth_bar)
            if resolved is not None:
                trades.append(resolved)
                active = None
        btc_history.append(btc_bar)
        eth_history.append(eth_bar)
        scoring = (score_start is None or btc_bar.ts >= score_start) and (
            score_end is None or btc_bar.ts <= score_end
        )
        if scoring:
            scored_timestamps.append(btc_bar.ts)
        if scoring and pending is None and active is None:
            candidate = scanner.evaluate(
                LeadLagContext(btc_bar.ts, tuple(btc_history), tuple(eth_history))
            )
            if candidate is not None:
                pending = candidate
    dropped += int(pending is not None) + int(active is not None)
    return PairReplayResult(
        trades=tuple(trades),
        missing_minutes=missing,
        unresolved_dropped=dropped,
        aligned_bars=processed,
        score_started_at=scored_timestamps[0] if scored_timestamps else None,
        score_ended_at=scored_timestamps[-1] if scored_timestamps else None,
    )


def _profit_factor(trades: tuple[BacktestTrade, ...]) -> float:
    gains = sum(row.net_bps for row in trades if row.net_bps > 0)
    losses = -sum(row.net_bps for row in trades if row.net_bps < 0)
    return gains / losses if losses else (float("inf") if gains else 0.0)


def replay_metrics(result: PairReplayResult) -> dict[str, object]:
    trades = result.trades
    days = (
        max(
            1.0,
            (result.score_ended_at - result.score_started_at).total_seconds() / 86_400,
        )
        if result.score_started_at and result.score_ended_at
        else 1.0
    )
    net = [row.net_bps for row in trades]
    gross = [row.gross_bps for row in trades]
    costs = [row.cost_bps for row in trades]
    return {
        "trades": len(trades),
        "trades_per_day": len(trades) / days,
        "average_gross_bps": fmean(gross) if gross else 0.0,
        "average_cost_bps": fmean(costs) if costs else 0.0,
        "average_net_bps": fmean(net) if net else 0.0,
        "net_bps": sum(net),
        "profit_factor": _profit_factor(trades),
        "win_rate": sum(value > 0 for value in net) / len(net) if net else 0.0,
        "false_signal_rate": sum(value <= 0 for value in net) / len(net) if net else 0.0,
        "exit_reasons": dict(Counter(row.exit_reason for row in trades)),
        "sides": dict(Counter(row.side.value for row in trades)),
        "missing_minutes": result.missing_minutes,
        "unresolved_dropped": result.unresolved_dropped,
        "data_quality_pass": result.missing_minutes == 0 and result.unresolved_dropped == 0,
        "score_started_at": (
            result.score_started_at.isoformat() if result.score_started_at else None
        ),
        "score_ended_at": result.score_ended_at.isoformat() if result.score_ended_at else None,
    }


def selection_gate(
    result: PairReplayResult,
    config: BtcEthLeadLagConfig,
) -> dict[str, object]:
    metrics = replay_metrics(result)
    trades = result.trades
    time_midpoint = (
        result.score_started_at + (result.score_ended_at - result.score_started_at) / 2
        if result.score_started_at and result.score_ended_at
        else None
    )
    first = tuple(
        row for row in trades if time_midpoint is not None and row.decision_ts <= time_midpoint
    )
    second = tuple(
        row for row in trades if time_midpoint is not None and row.decision_ts > time_midpoint
    )
    first_net = sum(row.net_bps for row in first)
    second_net = sum(row.net_bps for row in second)
    validation = config.validation
    checks = {
        "data_quality": bool(metrics["data_quality_pass"]),
        "minimum_trades": len(trades) >= validation.minimum_selection_trades,
        "minimum_trades_per_half": (
            len(first) >= validation.minimum_selection_trades_per_half
            and len(second) >= validation.minimum_selection_trades_per_half
        ),
        "average_net": (
            float(metrics["average_net_bps"]) >= validation.minimum_selection_average_net_bps
        ),
        "profit_factor": (
            float(metrics["profit_factor"]) >= validation.minimum_selection_profit_factor
        ),
        "gross_clears_cost": (
            float(metrics["average_gross_bps"]) > float(metrics["average_cost_bps"])
        ),
        "frequency": (
            validation.minimum_trades_per_day
            <= float(metrics["trades_per_day"])
            <= validation.maximum_trades_per_day
        ),
        "positive_halves": (
            (first_net > 0 and second_net > 0)
            if validation.require_positive_selection_halves
            else True
        ),
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "first_half": {"trades": len(first), "net_bps": first_net},
        "second_half": {"trades": len(second), "net_bps": second_net},
        "thresholds": validation.model_dump(),
    }


async def run(args: argparse.Namespace) -> dict[str, object]:
    config_path = Path(args.config)
    config = load_lead_lag_config(config_path)
    start = _parse_ts(config.data.start)
    end = _parse_ts(config.data.end)
    btc, eth = await asyncio.gather(
        _load_candles("BTCUSD", start, end, cache_dir=Path(args.cache_dir), refresh=False),
        _load_candles("ETHUSD", start, end, cache_dir=Path(args.cache_dir), refresh=False),
    )
    aligned = align_closed_pairs(btc, eth)
    if not aligned:
        raise RuntimeError("no synchronized BTC/ETH candles")
    split_index = int(len(aligned) * config.validation.selection_fraction)
    split_ts = aligned[split_index][0].ts
    selection_decision_end = split_ts - timedelta(minutes=config.validation.split_embargo_minutes)
    selection = simulate_pair_window(
        aligned,
        config,
        score_end=selection_decision_end,
        process_end=split_ts,
    )
    selection_metrics = replay_metrics(selection)
    gate = selection_gate(selection, config)
    untouched: dict[str, object] = {
        "status": "sealed",
        "predictions_computed": False,
        "trades_computed": False,
        "opened_once": False,
    }
    if gate["passed"]:
        tail = simulate_pair_window(aligned, config, score_start=split_ts)
        untouched = {
            "status": "opened_after_selection_pass",
            "predictions_computed": True,
            "trades_computed": True,
            "opened_once": True,
            "metrics": replay_metrics(tail),
            "trades": [row.to_dict() for row in tail.trades],
        }
    payload: dict[str, object] = {
        "report_id": "btc_eth_lead_lag_v1_first_replay",
        "generated_at": datetime.now(UTC).isoformat(),
        "contract": str(config_path),
        "contract_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "split": {
            "selection_fraction": config.validation.selection_fraction,
            "untouched_fraction": config.validation.untouched_fraction,
            "boundary": split_ts.isoformat(),
            "selection_decision_end": selection_decision_end.isoformat(),
            "embargo_minutes": config.validation.split_embargo_minutes,
        },
        "aligned_bars": len(aligned),
        "selection": {
            "metrics": selection_metrics,
            "gate": gate,
            "trades": [row.to_dict() for row in selection.trades],
        },
        "untouched": untouched,
        "policy": {
            "research_only": True,
            "can_trade": False,
            "can_promote": False,
            "l2_used": False,
            "next_bar_entry": True,
            "stop_first": True,
            "parameters_tunable_on_this_window": False,
        },
        "can_trade": False,
        "can_promote": False,
    }
    _atomic_json(Path(args.output), payload)
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    return parser


def main() -> None:
    payload = asyncio.run(run(_parser().parse_args()))
    selection = payload["selection"]
    untouched = payload["untouched"]
    print(
        json.dumps(
            {
                "selection": {
                    "metrics": selection["metrics"],
                    "gate": selection["gate"],
                },
                "untouched": {key: value for key, value in untouched.items() if key != "trades"},
            },
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
