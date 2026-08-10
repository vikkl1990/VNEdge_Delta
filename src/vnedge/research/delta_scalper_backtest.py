"""Download and causally backtest the Delta scalper engine.

Example:
    python -m vnedge.research.delta_scalper_backtest \
      --start 2025-01-01 --symbols BTCUSD,ETHUSD --scalper-opted-in

The command is research-only.  It writes complete trade rows plus daily,
weekly, monthly, quarterly, market, and leverage summaries.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile

import numpy as np
import pandas as pd

from vnedge.data.delta_native_history import fetch_delta_candle_history
from vnedge.scalping.delta_engine.backtester import CausalScalperBacktester
from vnedge.scalping.delta_engine.candle_store import MultiTimeframeCandleStore
from vnedge.scalping.delta_engine.config import load_delta_scalper_config
from vnedge.scalping.delta_engine.factory import build_delta_scalper_assembly
from vnedge.scalping.delta_engine.types import Candle
from vnedge.scalping.delta_engine.validation import (
    fee_sensitivity,
    robust_validation_report,
    untouched_window_summary,
)


def _parse_date(value: str, *, end: bool = False) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    parsed = parsed.astimezone(UTC)
    if end and len(value) == 10:
        parsed += timedelta(days=1)
    return parsed


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


async def _load_candles(
    symbol: str,
    start: datetime,
    end: datetime,
    *,
    cache_dir: Path,
    refresh: bool,
) -> list[Candle]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    frames: list[pd.DataFrame] = []
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + timedelta(days=30), end)
        cache = cache_dir / (
            f"{symbol}_1m_{cursor:%Y%m%dT%H%M}_{chunk_end:%Y%m%dT%H%M}.parquet"
        )
        if cache.exists() and not refresh:
            frame = pd.read_parquet(cache)
        else:
            frame = await fetch_delta_candle_history(
                symbol,
                resolution="1m",
                start_s=int(cursor.timestamp()),
                end_s=int(chunk_end.timestamp()),
            )
            frame.to_parquet(cache, index=False)
        frames.append(frame)
        print(
            f"{symbol}: loaded {cursor:%Y-%m-%d} to {chunk_end:%Y-%m-%d} "
            f"({len(frame):,} closed 1m bars)",
            flush=True,
        )
        cursor = chunk_end
    frame = (
        pd.concat(frames, ignore_index=True)
        .drop_duplicates("timestamp", keep="last")
        .sort_values("timestamp")
        if frames
        else pd.DataFrame()
    )
    close_delta = timedelta(minutes=1)
    return [
        Candle(
            ts=row.timestamp.to_pydatetime() + close_delta,
            open=float(row.open),
            high=float(row.high),
            low=float(row.low),
            close=float(row.close),
            volume=float(row.volume),
            tf="1m",
        )
        for row in frame.itertuples(index=False)
    ]


def _period_key(ts: datetime, period: str) -> str:
    stamp = pd.Timestamp(ts)
    if period == "day":
        return stamp.strftime("%Y-%m-%d")
    if period == "week":
        iso = stamp.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    if period == "month":
        return stamp.strftime("%Y-%m")
    if period == "quarter":
        return f"{stamp.year}-Q{stamp.quarter}"
    raise ValueError(f"unknown period: {period}")


def _group_trades(
    trades: list[dict], period: str, *, start: datetime, end: datetime
) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for trade in trades:
        grouped[_period_key(datetime.fromisoformat(trade["exit_ts"]), period)].append(trade)
    expected_keys = {
        _period_key(stamp.to_pydatetime(), period)
        for stamp in pd.date_range(start=start.date(), end=(end - timedelta(seconds=1)).date())
    }
    rows = []
    for key in sorted(expected_keys | set(grouped)):
        members = grouped.get(key, [])
        wins = [row["net_bps"] for row in members if row["net_bps"] > 0]
        losses = [-row["net_bps"] for row in members if row["net_bps"] < 0]
        rows.append(
            {
                "period": key,
                "trades": len(members),
                "net_bps": sum(row["net_bps"] for row in members),
                "average_net_bps": (
                    sum(row["net_bps"] for row in members) / len(members) if members else 0.0
                ),
                "win_rate": len(wins) / len(members) if members else 0.0,
                "false_signal_rate": len(losses) / len(members) if members else 0.0,
                "profit_factor": sum(wins) / sum(losses) if losses else None,
                "markets": sorted({row["symbol"] for row in members}),
            }
        )
    return rows


def _rolling_expectancy(trades: list[dict], window: int = 30) -> list[dict]:
    return [
        {
            "exit_ts": trades[index]["exit_ts"],
            "window": window,
            "trades_in_window": min(window, index + 1),
            "average_net_bps": sum(
                row["net_bps"] for row in trades[max(0, index - window + 1) : index + 1]
            )
            / min(window, index + 1),
        }
        for index in range(len(trades))
    ]


def _leverage_scenarios(trades: list[dict], margin_usd: float) -> list[dict]:
    scenarios = []
    cumulative_net_bps = sum(row["net_bps"] for row in trades)
    for leverage in (1, 5, 10, 25, 50):
        notional = margin_usd * leverage
        equity = margin_usd
        peak = margin_usd
        max_drawdown = 0.0
        for trade in trades:
            equity += notional * trade["net_bps"] / 10_000.0
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, peak - equity)
        scenarios.append(
            {
                "leverage": leverage,
                "starting_margin_usd": margin_usd,
                "constant_notional_usd": notional,
                "net_pnl_usd": notional * cumulative_net_bps / 10_000.0,
                "ending_equity_usd": equity,
                "max_drawdown_usd": max_drawdown,
                "below_zero": equity <= 0,
                "liquidation_modeled": False,
            }
        )
    return scenarios


async def run(args: argparse.Namespace) -> dict:
    config = load_delta_scalper_config(args.config)
    start = _parse_date(args.start)
    end = _parse_date(args.end, end=True) if args.end else datetime.now(UTC)
    if start >= end:
        raise ValueError("start must be before end")
    symbols = (
        tuple(value.strip().upper() for value in args.symbols.split(",") if value.strip())
        if args.symbols
        else config.engine.symbols
    )
    if not symbols:
        raise ValueError("at least one symbol is required")
    slippage_bps = (
        args.slippage_bps
        if args.slippage_bps is not None
        else config.fee_model.default_slippage_bps_per_leg
    )
    market_reports: dict[str, dict] = {}
    all_trades: list[dict] = []
    for symbol in symbols:
        candles = await _load_candles(
            symbol,
            start,
            end,
            cache_dir=Path(args.cache_dir),
            refresh=args.refresh,
        )
        store = MultiTimeframeCandleStore(
            max_bars_per_timeframe=config.features.max_bars_per_timeframe
        )
        assembly = build_delta_scalper_assembly(
            store,
            config,
            deto_enabled=args.deto,
            scalper_opted_in=args.scalper_opted_in,
            slippage_bps=slippage_bps,
        )
        report = CausalScalperBacktester(
            assembly.generator, assembly.fee_model, store
        ).run(symbol, candles)
        market_reports[symbol] = report.to_dict()
        all_trades.extend(trade.to_dict() for trade in report.trades)
    all_trades.sort(key=lambda row: row["exit_ts"])
    positive_markets = sum(
        report["summary"]["net_bps"] > 0 for report in market_reports.values()
    )
    data_quality_pass = all(
        report["summary"]["data_quality_pass"] for report in market_reports.values()
    )
    gross_wins = sum(max(0.0, row["net_bps"]) for row in all_trades)
    gross_losses = sum(max(0.0, -row["net_bps"]) for row in all_trades)
    combined_pf = gross_wins / gross_losses if gross_losses else None
    same_bar_ambiguity_rate = (
        sum(row["same_bar_ambiguous"] for row in all_trades) / len(all_trades)
        if all_trades
        else 0.0
    )
    daily = _group_trades(all_trades, "day", start=start, end=end)
    weekly = _group_trades(all_trades, "week", start=start, end=end)
    monthly = _group_trades(all_trades, "month", start=start, end=end)
    quarterly = _group_trades(all_trades, "quarter", start=start, end=end)
    max_market_trade_share = (
        max(
            sum(row["symbol"] == symbol for row in all_trades) / len(all_trades)
            for symbol in symbols
        )
        if all_trades
        else 0.0
    )
    positive_months = [row for row in monthly if row["net_bps"] > 0]
    total_positive_month_bps = sum(row["net_bps"] for row in positive_months)
    max_positive_month_share = (
        max(row["net_bps"] for row in positive_months) / total_positive_month_bps
        if total_positive_month_bps > 0
        else 0.0
    )
    concentration_limit = config.promotion.maximum_single_market_share
    concentration_pass = (
        max_market_trade_share <= concentration_limit
        and max_positive_month_share <= concentration_limit
    )
    multiple_months_pass = (
        len(positive_months) >= 2 if config.promotion.require_multiple_months else True
    )
    performance_matrix = np.asarray(
        [float(row["net_bps"]) / 10_000.0 for row in all_trades], dtype=float
    ).reshape(-1, 1)
    robust = robust_validation_report(
        performance_matrix,
        selected_config=0,
        label_horizon=28,
    )
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "engine": "delta_scalper_engine_v1",
        "config": str(args.config),
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "cost_assumptions": {
            "maker_bps_including_gst": assembly.fee_model.maker_bps,
            "taker_bps_including_gst": assembly.fee_model.taker_bps,
            "slippage_bps_per_leg": assembly.fee_model.default_slippage_bps_per_leg,
            "deto_enabled": assembly.fee_model.deto_enabled,
            "scalper_opted_in": assembly.fee_model.scalper_opted_in,
        },
        "summary": {
            "markets": len(market_reports),
            "positive_markets": positive_markets,
            "trades": len(all_trades),
            "net_bps": sum(row["net_bps"] for row in all_trades),
            "profit_factor": combined_pf,
            "false_signal_rate": (
                sum(row["net_bps"] <= 0 for row in all_trades) / len(all_trades)
                if all_trades
                else 0.0
            ),
            "max_market_trade_share": max_market_trade_share,
            "positive_months": len(positive_months),
            "max_positive_month_share": max_positive_month_share,
            "data_quality_pass": data_quality_pass,
            "same_bar_ambiguity_rate": same_bar_ambiguity_rate,
        },
        "markets": market_reports,
        "daily": daily,
        "weekly": weekly,
        "monthly": monthly,
        "quarterly": quarterly,
        "rolling_expectancy_30_trades": _rolling_expectancy(all_trades),
        "fee_sensitivity": fee_sensitivity(
            all_trades, slippage_bps_per_leg=slippage_bps
        ),
        "untouched_window": untouched_window_summary(all_trades),
        "robust_validation": robust.to_dict(),
        "leverage_scenarios": _leverage_scenarios(all_trades, args.margin_usd),
        "promotion": {
            "minimum_positive_markets": config.promotion.minimum_positive_markets,
            "positive_markets_pass": (
                positive_markets >= config.promotion.minimum_positive_markets
            ),
            "minimum_profit_factor": config.promotion.minimum_profit_factor_after_costs,
            "profit_factor_pass": (
                combined_pf is not None
                and combined_pf > config.promotion.minimum_profit_factor_after_costs
            ),
            "maximum_concentration": concentration_limit,
            "market_and_month_concentration_pass": concentration_pass,
            "multiple_positive_months_pass": multiple_months_pass,
            "data_quality_pass": data_quality_pass,
            "intrabar_ambiguity_observed": same_bar_ambiguity_rate > 0,
            "intrabar_tick_replay_required": same_bar_ambiguity_rate > 0,
            "overall_pass": (
                positive_markets >= config.promotion.minimum_positive_markets
                and combined_pf is not None
                and combined_pf > config.promotion.minimum_profit_factor_after_costs
                and concentration_pass
                and multiple_months_pass
                and data_quality_pass
            ),
            "next_mode_if_passed": "paper",
            "current_mode": "research",
        },
        "policy": {
            "research_only": True,
            "closed_candles_only": True,
            "next_bar_entries": True,
            "l2_used_in_backtest": False,
            "l2_role_live": "confirmation_only",
            "liquidation_modeled_in_leverage_scenarios": False,
            "can_trade": False,
            "can_promote": False,
        },
        "can_trade": False,
        "can_promote": False,
    }
    _atomic_json(Path(args.output), payload)
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/delta_scalper.yaml")
    parser.add_argument("--symbols")
    parser.add_argument("--start", default="2025-01-01")
    parser.add_argument("--end")
    parser.add_argument("--cache-dir", default="data/delta_scalper_cache")
    parser.add_argument(
        "--output", default="research/live_research/delta_scalper_backtest_latest.json"
    )
    parser.add_argument("--margin-usd", type=float, default=100.0)
    parser.add_argument("--slippage-bps", type=float)
    parser.add_argument("--deto", action="store_true")
    parser.add_argument("--scalper-opted-in", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    return parser


def main() -> None:
    payload = asyncio.run(run(_parser().parse_args()))
    print(json.dumps(payload["summary"], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
