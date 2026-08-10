"""Batch Delta contract-risk matrix for the VNEDGE Algo ML Pro scanner.

The one-off VM proof for the TradingView-style scanner uncovered two important
facts: exact Pine lifecycle can look positive before costs, and Delta contract
sizing changes USD impact without changing bps edge. This CLI makes that proof
repeatable across symbols, timeframes, and capture modes.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import UTC, datetime
import json
from pathlib import Path
from tempfile import NamedTemporaryFile
import time
from typing import Iterable

import pandas as pd

from vnedge.data.parquet_store import ParquetStore
from vnedge.exchange.delta_contracts import DeltaContractSpec, fetch_india_contract_spec
from vnedge.research.vnedge_algo_ml_pro_pine_replay import (
    CaptureMode,
    PineReplayConfig,
    run_vnedge_algo_ml_pro_pine_replay,
)


DEFAULT_DELTA_SYMBOLS = ("BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD", "BNBUSD", "DOGEUSD")
DEFAULT_TIMEFRAMES = ("1m", "5m", "15m", "1h", "4h")
DEFAULT_CAPTURE_MODES: tuple[CaptureMode, ...] = ("pine_tp3", "smart_ladder")
DEFAULT_OUT = Path("research/live_research/vnedge_algo_ml_pro_contract_matrix_latest.json")


def run_contract_risk_matrix(
    *,
    data_root: Path | str = "data",
    exchange: str = "delta_india",
    symbols: Iterable[str] = DEFAULT_DELTA_SYMBOLS,
    timeframes: Iterable[str] = DEFAULT_TIMEFRAMES,
    capture_modes: Iterable[CaptureMode] = DEFAULT_CAPTURE_MODES,
    lookback_days: int = 30,
    sizing_mode: str = "delta_contract_risk",
    delta_live_product_spec: bool = False,
    delta_contract_specs: dict[str, DeltaContractSpec] | None = None,
    paper_margin_usd: float = 100.0,
    paper_leverage: float = 25.0,
    account_equity_usd: float = 500.0,
    risk_per_trade_pct: float = 1.0,
    acknowledge_high_leverage: bool = False,
    fee_cost_bps: float | None = 12.5,
    include_payloads: bool = False,
    now: datetime | None = None,
) -> dict:
    store = ParquetStore(data_root)
    symbols_tuple = tuple(symbols)
    timeframes_tuple = tuple(timeframes)
    modes_tuple = tuple(capture_modes)
    specs = dict(delta_contract_specs or {})
    rows: list[dict] = []
    payloads: list[dict] = []
    errors: list[dict] = []

    for symbol in symbols_tuple:
        spec = specs.get(symbol)
        if (
            sizing_mode == "delta_contract_risk"
            and spec is None
            and (delta_live_product_spec or exchange == "delta_india")
        ):
            spec = fetch_india_contract_spec(symbol)
            specs[symbol] = spec
        for timeframe in timeframes_tuple:
            try:
                candles = _window(
                    store.read_candles(exchange, symbol, timeframe),
                    lookback_days,
                )
            except Exception as exc:  # pragma: no cover - defensive CLI surface
                errors.append(_error(symbol, timeframe, "read_candles", exc))
                continue
            for capture_mode in modes_tuple:
                try:
                    config = PineReplayConfig(
                        paper_margin_usd=paper_margin_usd,
                        paper_leverage=paper_leverage,
                        account_equity_usd=account_equity_usd,
                        risk_per_trade_pct=risk_per_trade_pct,
                        acknowledge_high_leverage=acknowledge_high_leverage,
                        sizing_mode=sizing_mode,  # type: ignore[arg-type]
                        delta_contract_spec=spec,
                        fee_cost_bps=fee_cost_bps,
                        capture_mode=capture_mode,
                        lookback_days=lookback_days,
                    )
                    payload = run_vnedge_algo_ml_pro_pine_replay(
                        candles,
                        exchange=exchange,
                        symbol=symbol,
                        timeframe=timeframe,
                        config=config,
                    )
                    rows.append(compact_replay_payload(payload))
                    if include_payloads:
                        payloads.append(payload)
                except Exception as exc:  # pragma: no cover - defensive CLI surface
                    errors.append(_error(symbol, timeframe, capture_mode, exc))
    generated = now or datetime.now(UTC)
    return {
        "generated_at": generated.isoformat(),
        "truth_layer": "vnedge_algo_ml_pro_contract_matrix_v1",
        "scope": {
            "exchange": exchange,
            "symbols": list(symbols_tuple),
            "timeframes": list(timeframes_tuple),
            "capture_modes": list(modes_tuple),
            "lookback_days": lookback_days,
            "sizing_mode": sizing_mode,
            "paper_margin_usd": paper_margin_usd,
            "paper_leverage": paper_leverage,
            "account_equity_usd": account_equity_usd,
            "risk_per_trade_pct": risk_per_trade_pct,
            "acknowledge_high_leverage": acknowledge_high_leverage,
            "fee_cost_bps": fee_cost_bps,
            "delta_live_product_spec": delta_live_product_spec,
        },
        "summary": _summary(rows, errors),
        "delta_contract_specs": {key: asdict(value) for key, value in specs.items()},
        "rows": rows,
        "payloads": payloads,
        "errors": errors,
        "policy": {
            "research_only": True,
            "can_trade": False,
            "can_promote": False,
            "operator_note": (
                "This is a repeatable backtest matrix for the supplied scanner. "
                "Rows still need uplift analysis and untouched-window proof."
            ),
        },
        "can_trade": False,
        "can_promote": False,
    }


def compact_replay_payload(payload: dict) -> dict:
    summary = dict(payload.get("summary") or {})
    sizing = dict(summary.get("position_sizing") or {})
    return {
        "exchange": payload.get("exchange"),
        "symbol": payload.get("symbol"),
        "timeframe": payload.get("timeframe"),
        "strategy_id": payload.get("strategy_id"),
        "mode": payload.get("capture_mode"),
        "bars": payload.get("bars"),
        "closed": summary.get("closed_trades"),
        "win_rate_pct": summary.get("win_rate_pct"),
        "pf_r": summary.get("profit_factor_r"),
        "visual_avg_bps": summary.get("visual_avg_bps"),
        "fee_avg_bps": summary.get("fee_aware_avg_bps"),
        "visual_usd": summary.get("visual_paper_usd"),
        "fee_usd": summary.get("fee_aware_paper_usd"),
        "passed": dict(summary.get("promotion_gate") or {}).get("passed", False),
        "exits": summary.get("exit_reason_counts") or {},
        "actual_notional_avg": sizing.get("actual_notional_usd_avg"),
        "margin_avg": sizing.get("margin_usd_avg"),
        "contracts_avg": sizing.get("contracts_avg"),
        "sizing_mode": payload.get("sizing_mode"),
        "paper_margin_usd": payload.get("paper_margin_usd"),
        "paper_leverage": payload.get("paper_leverage"),
    }


def publish_contract_risk_matrix(payload: dict, *, out: Path | str = DEFAULT_OUT) -> Path:
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
    tmp_path.chmod(0o644)
    tmp_path.replace(out_path)
    out_path.chmod(0o644)
    return out_path


def _summary(rows: list[dict], errors: list[dict]) -> dict:
    completed = [row for row in rows if int(row.get("closed") or 0) > 0]
    positive = [
        row for row in completed
        if _float(row.get("fee_avg_bps")) is not None and (_float(row.get("fee_avg_bps")) or 0.0) > 0.0
    ]
    passed = [row for row in completed if bool(row.get("passed"))]
    best = max(
        completed,
        key=lambda row: _float(row.get("fee_avg_bps")) if _float(row.get("fee_avg_bps")) is not None else -1_000_000.0,
        default=None,
    )
    return {
        "rows": len(rows),
        "completed": len(completed),
        "positive_after_cost": len(positive),
        "passed": len(passed),
        "errors": len(errors),
        "best_symbol": best.get("symbol") if best is not None else None,
        "best_timeframe": best.get("timeframe") if best is not None else None,
        "best_mode": best.get("mode") if best is not None else None,
        "best_fee_avg_bps": best.get("fee_avg_bps") if best is not None else None,
        "best_profit_factor": best.get("pf_r") if best is not None else None,
    }


def _window(df: pd.DataFrame, lookback_days: int) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True)
    cutoff = out["timestamp"].max() - pd.Timedelta(days=lookback_days)
    return out[out["timestamp"] >= cutoff].reset_index(drop=True)


def _error(symbol: str, timeframe: str, phase: str, exc: Exception) -> dict:
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "phase": str(phase),
        "error": str(exc),
    }


def _float(value: object) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if pd.notna(out) else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="VNEDGE Algo ML Pro contract-risk matrix")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--exchange", default="delta_india")
    parser.add_argument("--symbols", nargs="+", default=list(DEFAULT_DELTA_SYMBOLS))
    parser.add_argument("--timeframes", nargs="+", default=list(DEFAULT_TIMEFRAMES))
    parser.add_argument(
        "--capture-modes",
        nargs="+",
        choices=list(DEFAULT_CAPTURE_MODES),
        default=list(DEFAULT_CAPTURE_MODES),
    )
    parser.add_argument("--lookback-days", type=int, default=30)
    parser.add_argument(
        "--sizing-mode",
        choices=("fixed_notional", "delta_contract_risk"),
        default="delta_contract_risk",
    )
    parser.add_argument("--delta-live-product-spec", action="store_true")
    parser.add_argument("--paper-margin-usd", type=float, default=100.0)
    parser.add_argument("--paper-leverage", type=float, default=25.0)
    parser.add_argument("--account-equity-usd", type=float, default=500.0)
    parser.add_argument("--risk-per-trade-pct", type=float, default=1.0)
    parser.add_argument("--acknowledge-high-leverage", action="store_true")
    parser.add_argument("--fee-cost-bps", type=float, default=12.5)
    parser.add_argument("--include-payloads", action="store_true")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=0,
        help="repeat forever at this cadence; 0 runs once",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    while True:
        payload = run_contract_risk_matrix(
            data_root=args.data_root,
            exchange=args.exchange,
            symbols=args.symbols,
            timeframes=args.timeframes,
            capture_modes=args.capture_modes,
            lookback_days=args.lookback_days,
            sizing_mode=args.sizing_mode,
            delta_live_product_spec=args.delta_live_product_spec,
            paper_margin_usd=args.paper_margin_usd,
            paper_leverage=args.paper_leverage,
            account_equity_usd=args.account_equity_usd,
            risk_per_trade_pct=args.risk_per_trade_pct,
            acknowledge_high_leverage=args.acknowledge_high_leverage,
            fee_cost_bps=args.fee_cost_bps,
            include_payloads=args.include_payloads,
        )
        path = publish_contract_risk_matrix(payload, out=args.out)
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True), flush=True)
        else:
            print(f"VNEDGE Algo ML Pro contract matrix wrote {path}", flush=True)
            print(json.dumps(payload["summary"], indent=2, sort_keys=True), flush=True)
        if args.interval_seconds <= 0:
            break
        time.sleep(max(1, args.interval_seconds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
