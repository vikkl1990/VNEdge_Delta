"""Scalper fee sweep (formerly the "scalper gauntlet") — the 1-minute fee-wall test.

    python scripts/scalper_fee_sweep.py --days 45

Standalone research CLI, not an importable library module — it moved here
from src/vnedge/research/scalper_gauntlet.py (nothing imports it). Requires
the vnedge package on the path (pip install -e . or PYTHONPATH=src).

Downloads 1m candles, runs the candle-approximation scalper through
walk-forward at a SWEEP of fee levels, and reports out-of-sample net at
each. The point is not a verdict — it is to locate the breakeven fee and
compare it to what retail actually pays (taker ~5bps, maker ~2bps). If
breakeven sits below the maker rate, the fee wall is fatal; if it sits
above, there is a (narrow) window worth the tick-recorder path.

Measurement config: leverage/exposure caps are RELAXED here so scalp trades
actually execute and the fee variable is isolated. This is NOT a live or
paper config — the real 5x cap would additionally throttle a tight-stop
scalper. That caveat is printed with the results.
"""

from __future__ import annotations

import argparse
import asyncio
import time

from vnedge.backtest.backtester import BacktestConfig
from vnedge.backtest.fee_model import FeeModel
from vnedge.backtest.slippage_model import SlippageModel
from vnedge.backtest.walk_forward import PromotionGates, evaluate_promotion, param_grid, walk_forward
from vnedge.config.risk_config import RiskConfig
from vnedge.data.ccxt_client import CcxtPublicClient
from vnedge.data.data_quality_gate import validate_candles
from vnedge.data.schemas import normalize_candles
from vnedge.risk.position_sizer import SymbolLimits
from vnedge.strategy.scalper_1m import Scalper1m

# Relaxed measurement config — research only, never live.
MEASURE_RISK = RiskConfig(
    risk_per_trade_pct=0.5,
    max_leverage_per_position=25,
    acknowledge_high_leverage=True,
    max_exposure_per_symbol_usd=100_000.0,
    max_total_exposure_usd=100_000.0,
    max_effective_account_leverage=10.0,  # config ceiling
    min_liquidation_buffer_pct=1.0,
)
MEASURE_LIMITS = SymbolLimits(
    min_qty=0.00001, qty_step=0.00001, min_notional_usd=1.0,
    maintenance_margin_rate=0.004,
)


async def fetch_1m(symbol: str, days: int):
    until = int(time.time() * 1000)
    since = until - days * 86_400_000
    async with CcxtPublicClient("binanceusdm") as client:
        raw = await client.fetch_candles(symbol, "1m", since, until)
    df = normalize_candles(raw)
    report = validate_candles(df, "1m", allow_gaps=True, dataset=f"scalp/{symbol}")
    return df, report


def run_fee_level(candles, taker_bps, *, train_bars, test_bars):
    config = BacktestConfig(
        initial_equity_usd=500.0, max_holding_bars=15, risk=MEASURE_RISK,
        limits=MEASURE_LIMITS,
        fees=FeeModel(maker_bps=max(0.0, taker_bps - 3.0), taker_bps=taker_bps),
        slippage=SlippageModel(bps=1.0),
    )
    result = walk_forward(
        candles, None, lambda **p: Scalper1m(**p),
        param_grid(flow_threshold=[0.4, 0.6], take_profit_r=[1.0, 1.5]),
        config, train_bars=train_bars, test_bars=test_bars,
        symbol="BTC/USDT:USDT", timeframe="1m",
    )
    trades = sum(w.test_metrics.num_trades for w in result.windows)
    fees = sum(w.test_metrics.total_fees_usd for w in result.windows)
    decision = evaluate_promotion(result, PromotionGates())
    return {
        "taker_bps": taker_bps,
        "windows": len(result.windows),
        "oos_trades": trades,
        "oos_net_usd": round(result.oos_net_profit_usd, 2),
        "oos_fees_usd": round(fees, 2),
        "verdict": "PASS" if decision.passed else "REJECT",
    }


def main(argv=None) -> int:
    import logging
    logging.basicConfig(level=logging.WARNING)
    p = argparse.ArgumentParser(description="1m scalper fee-wall gauntlet")
    p.add_argument("--symbol", default="BTC/USDT:USDT")
    p.add_argument("--days", type=int, default=45)
    p.add_argument("--fees", default="1,2,3,5", help="taker bps sweep")
    args = p.parse_args(argv)

    candles, report = asyncio.run(fetch_1m(args.symbol, args.days))
    print(f"data: {len(candles)} 1m candles ({args.days}d {args.symbol}); "
          f"gate {'PASS' if report.passed else 'REJECT'} ({report.gap_count} gaps)")
    train_bars, test_bars = 20 * 1440, 10 * 1440
    if train_bars + test_bars > len(candles):
        train_bars, test_bars = int(len(candles) * 0.6), int(len(candles) * 0.3)

    print(f"\nscalper fee-wall sweep (train {train_bars}b / test {test_bars}b, "
          f"walk-forward OOS):")
    print(f"{'taker bps':>9} {'net $':>10} {'fees $':>10} {'trades':>7} {'windows':>7}  verdict")
    prev = None
    breakeven = None
    for bps in [float(x) for x in args.fees.split(",")]:
        r = run_fee_level(candles, bps, train_bars=train_bars, test_bars=test_bars)
        print(f"{r['taker_bps']:>9.1f} {r['oos_net_usd']:>10.2f} {r['oos_fees_usd']:>10.2f} "
              f"{r['oos_trades']:>7} {r['windows']:>7}  {r['verdict']}")
        if prev and prev["oos_net_usd"] <= 0 < r["oos_net_usd"]:
            breakeven = (prev["taker_bps"], r["taker_bps"])
        if prev and prev["oos_net_usd"] > 0 >= r["oos_net_usd"]:
            breakeven = (r["taker_bps"], prev["taker_bps"])
        prev = r

    print("\nreference: retail taker ≈ 5bps, maker ≈ 2bps.")
    if breakeven:
        print(f"breakeven fee is between {breakeven[0]} and {breakeven[1]} bps.")
    print("CAVEAT: measurement config relaxes the 5x leverage cap so trades "
          "execute; live sizing would throttle a tight-stop scalper further. "
          "candle flow is a FAVORABLE proxy for real order flow.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
