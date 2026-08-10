"""Per-symbol liquidity & fee-wall ranking over recorded L2 data.

    python -m vnedge.research.liquidity_ranking --data-root data --day 20260705

Answers Phase 2's core question — *which pairs can actually clear fees?* For
each recorded symbol it profiles the spread distribution, near-touch liquidity,
trade rate, and the **fee wall** (round-trip maker + taker + slippage cost, in
bps), then ranks by how close the spread comes to clearing that wall.

Honest by construction: for liquid perps the resting spread is a tiny fraction
of the fee wall, i.e. spread capture alone cannot pay for a round trip — only a
genuine directional microstructure edge (which the replay engine tests) could.
This tool quantifies exactly how far each pair is from viable; it does not
route orders and it is not a promotion.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from vnedge.scalping.depth import load_l2_books
from vnedge.scalping.replay_backtester import ReplayFees, _load_stream_frame

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LiquidityProfile:
    exchange: str
    symbol: str
    book_snapshots: int
    trades: int
    span_minutes: float
    spread_bps_p50: float
    spread_bps_p90: float
    near_touch_liq_usd_p50: float   # resting notional within `near_bps` of mid
    trades_per_min: float
    fee_wall_bps: float             # round-trip cost a scalp must clear
    spread_to_fee_wall: float       # spread_p50 / fee_wall; >= 1 == spread alone clears
    spread_clears_fee_wall_pct: float  # % of snapshots with spread >= fee wall

    @property
    def verdict(self) -> str:
        if self.book_snapshots < 100:
            return "UNDER_SAMPLED"
        if self.spread_clears_fee_wall_pct <= 0.0:
            return "SPREAD_BELOW_FEE_WALL"   # spread capture hopeless at this fee tier
        if self.spread_to_fee_wall < 1.0:
            return "MARGINAL"
        return "SPREAD_CLEARS"


def compute_profile(
    exchange: str, symbol: str, books, trade_df,
    fees: ReplayFees = ReplayFees(), near_bps: float = 5.0,
) -> LiquidityProfile:
    spreads = np.array([b.spread_bps for _, b in books], dtype=float)
    liqs = np.array([b.liquidity_usd_within_bps(near_bps) for _, b in books], dtype=float)
    fee_wall = fees.maker_bps + fees.taker_bps + fees.slippage_bps

    ts = [t for t, _ in books]
    span_min = (max(ts) - min(ts)) / 60_000.0 if len(ts) > 1 else 0.0
    n_trades = 0 if trade_df is None else len(trade_df)

    p50 = float(np.median(spreads)) if spreads.size else 0.0
    p90 = float(np.percentile(spreads, 90)) if spreads.size else 0.0
    clears_pct = float((spreads >= fee_wall).mean() * 100.0) if spreads.size else 0.0

    return LiquidityProfile(
        exchange=exchange, symbol=symbol,
        book_snapshots=len(books), trades=n_trades, span_minutes=span_min,
        spread_bps_p50=p50, spread_bps_p90=p90,
        near_touch_liq_usd_p50=float(np.median(liqs)) if liqs.size else 0.0,
        trades_per_min=(n_trades / span_min) if span_min > 0 else 0.0,
        fee_wall_bps=fee_wall,
        spread_to_fee_wall=(p50 / fee_wall) if fee_wall > 0 else 0.0,
        spread_clears_fee_wall_pct=clears_pct,
    )


def list_recorded_symbols(data_root: Path | str, exchange: str) -> list[str]:
    """Recorded symbols for an exchange, as ccxt symbols (best-effort un-slug)."""
    root = Path(data_root) / "ticks" / f"exchange={exchange}"
    out = []
    for d in sorted(root.glob("symbol=*")):
        safe = d.name.split("=", 1)[1]
        # BTCUSDT -> BTC/USDT:USDT (USDT-margined perp convention)
        base = safe[:-4] if safe.endswith("USDT") else safe
        out.append(f"{base}/USDT:USDT" if safe.endswith("USDT") else safe)
    return out


def rank_symbols(profiles: list[LiquidityProfile]) -> list[LiquidityProfile]:
    """Most-tradable first: the binding constraint is spread vs the fee wall,
    then near-touch liquidity as the tie-break."""
    return sorted(
        profiles,
        key=lambda p: (p.spread_to_fee_wall, p.near_touch_liq_usd_p50),
        reverse=True,
    )


def profile_day(
    data_root: Path | str, exchange: str, day: str,
    symbols: list[str] | None = None, fees: ReplayFees = ReplayFees(),
    near_bps: float = 5.0,
) -> list[LiquidityProfile]:
    symbols = symbols or list_recorded_symbols(data_root, exchange)
    root = Path(data_root) / "ticks" / f"exchange={exchange}"
    profiles = []
    for symbol in symbols:
        books = load_l2_books(data_root, exchange, symbol, day)
        if not books:
            logger.info("%s %s: no L2 books for %s — skipped", exchange, symbol, day)
            continue
        safe = symbol.split(":")[0].replace("/", "")
        trade_df = _load_stream_frame(root / f"symbol={safe}" / "stream=trades", day)
        profiles.append(compute_profile(exchange, symbol, books, trade_df, fees, near_bps))
    return rank_symbols(profiles)


def _format_table(profiles: list[LiquidityProfile]) -> str:
    lines = [
        f"fee wall = {profiles[0].fee_wall_bps:.1f}bps round trip "
        f"(maker+taker+slippage)" if profiles else "no data",
        f"{'symbol':16s} {'snaps':>7s} {'spread_p50':>11s} {'sprd/wall':>9s} "
        f"{'clears%':>8s} {'liq@5bps':>12s} {'tr/min':>7s}  verdict",
    ]
    for p in profiles:
        lines.append(
            f"{p.symbol:16s} {p.book_snapshots:>7d} {p.spread_bps_p50:>10.3f}b "
            f"{p.spread_to_fee_wall:>9.4f} {p.spread_clears_fee_wall_pct:>7.1f}% "
            f"${p.near_touch_liq_usd_p50:>10,.0f} {p.trades_per_min:>7.1f}  {p.verdict}"
        )
    return "\n".join(lines)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="per-symbol liquidity & fee-wall ranking")
    ap.add_argument("--data-root", default="data")
    ap.add_argument("--exchange", default="binanceusdm")
    ap.add_argument("--day", required=True, help="UTC day, YYYYMMDD")
    ap.add_argument("--near-bps", type=float, default=5.0)
    args = ap.parse_args(argv)
    profiles = profile_day(args.data_root, args.exchange, args.day, near_bps=args.near_bps)
    if not profiles:
        print(f"no recorded L2 data for {args.exchange} on {args.day}")
        return 0
    print(_format_table(profiles))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
