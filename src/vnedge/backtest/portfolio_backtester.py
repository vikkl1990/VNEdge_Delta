"""Multi-symbol portfolio backtester over one SHARED equity account.

Extends the single-symbol event loop (backtester.py) to several symbols
trading against one equity balance. Every realism rule carries over
unchanged: decisions at bar close, fills at next bar open, stop wins
intrabar ties, taker fees + adverse slippage, funding at actual event
timestamps, and sizing through the SAME ``size_position`` live trading uses
— sized from CURRENT shared equity, so losses anywhere shrink risk
everywhere.

Portfolio rules:

- Bars from all symbols are interleaved strictly by timestamp (same
  timeframe assumed). A symbol missing a bar at some timestamp simply does
  not act there; its pending intent fills at that symbol's NEXT bar open.
- Per timestamp: funding accrual, pending fills at the open, exits, then
  entry signals at the close — exits always free slots before new signals
  compete for them.
- One position per symbol. At most ``max_concurrent_positions`` open or
  pending portfolio-wide, and summed entry notional is capped at
  ``max_total_exposure_pct`` of current equity.
- When more symbols signal at one timestamp than slots remain, priority is
  ALPHABETICAL symbol order. This is the documented v1 tie-break:
  BaseStrategy intents carry no comparable score, so any ranking would be
  invented — alphabetical is arbitrary but deterministic and honest.
- v1 simplification: ``config.limits`` (one SymbolLimits) applies to every
  symbol; introduce per-symbol limits when a real candidate needs them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from pydantic import Field

from vnedge.backtest.backtester import (
    BacktestConfig,
    Trade,
    _check_intrabar_exit,
    _OpenPosition,
    _unrealized,
)
from vnedge.risk.position_sizer import size_position
from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent

logger = logging.getLogger(__name__)


class PortfolioBacktestConfig(BacktestConfig):
    max_concurrent_positions: int = Field(default=3, ge=1)
    # Cap on summed entry notional of open positions, as % of CURRENT equity.
    max_total_exposure_pct: float = Field(default=100.0, gt=0)


@dataclass(frozen=True)
class SymbolSummary:
    symbol: str
    num_trades: int
    net_pnl_usd: float
    fees_usd: float
    funding_usd: float
    skipped_by_sizing: int


@dataclass(frozen=True)
class PortfolioBacktestResult:
    symbols: tuple[str, ...]
    timeframe: str
    trades: tuple[Trade, ...]
    equity_curve: list[tuple[pd.Timestamp, float]]
    per_symbol: dict[str, SymbolSummary]
    max_drawdown_pct: float
    total_fees_usd: float
    total_funding_usd: float
    skipped_by_sizing: int
    skipped_by_slots: int
    skipped_by_exposure: int
    final_equity_usd: float
    config: PortfolioBacktestConfig = field(
        repr=False, default_factory=PortfolioBacktestConfig
    )


@dataclass
class _Lane:
    """Per-symbol state: prepared frame, cursors, open position, pending intent."""

    symbol: str
    strategy: BaseStrategy
    df: pd.DataFrame
    timestamps: pd.Series
    ts_arr: np.ndarray
    f_ts: np.ndarray
    f_rate: np.ndarray
    start: int  # first decision bar: max(warmup, 1)
    f_idx: int = 0
    cursor: int = 0  # next bar index to consume from ts_arr
    position: _OpenPosition | None = None
    pending: SignalIntent | None = None
    last_ts: pd.Timestamp | None = None  # last PROCESSED bar (for marking/force-close)
    last_close: float = 0.0
    skipped_by_sizing: int = 0

    @property
    def n(self) -> int:
        return len(self.df)


def _build_lane(
    symbol: str,
    candles: pd.DataFrame,
    funding: pd.DataFrame | None,
    strategy: BaseStrategy,
) -> _Lane:
    if candles.empty:
        raise ValueError(f"{symbol}: empty candle frame")
    ts = candles["timestamp"]
    if not ts.is_monotonic_increasing or ts.duplicated().any():
        raise ValueError(f"{symbol}: candles must be gate-validated: sorted, unique timestamps")
    df = strategy.prepare(candles).reset_index(drop=True)
    if len(df) != len(candles):
        raise ValueError(f"{symbol}: strategy.prepare() must not add or drop rows")
    if funding is not None and not funding.empty:
        f_ts, f_rate = funding["timestamp"].to_numpy(), funding["funding_rate"].to_numpy()
    else:
        f_ts, f_rate = np.array([], dtype="datetime64[ns]"), np.array([])
    return _Lane(
        symbol=symbol, strategy=strategy, df=df, timestamps=df["timestamp"],
        ts_arr=df["timestamp"].to_numpy(), f_ts=f_ts, f_rate=f_rate,
        start=max(strategy.warmup_bars, 1),
    )


def run_portfolio_backtest(
    datasets: dict[str, tuple[pd.DataFrame, pd.DataFrame | None]],
    strategies: dict[str, BaseStrategy],
    config: PortfolioBacktestConfig,
    *,
    timeframe: str = "1h",
) -> PortfolioBacktestResult:
    if not datasets:
        raise ValueError("no datasets")
    if set(datasets) != set(strategies):
        raise ValueError("datasets and strategies must cover the same symbols")

    # Alphabetical everywhere — the documented v1 priority order.
    order = sorted(datasets)
    lanes = {s: _build_lane(s, datasets[s][0], datasets[s][1], strategies[s]) for s in order}
    timeline = np.array(sorted({t for lane in lanes.values() for t in lane.ts_arr}))

    equity = config.initial_equity_usd
    trades: list[Trade] = []
    curve: list[tuple[pd.Timestamp, float]] = []
    skipped_slots = skipped_exposure = 0

    def slots_used() -> int:
        return sum(1 for la in lanes.values() if la.position is not None or la.pending is not None)

    def committed_notional() -> float:
        return sum(
            la.position.quantity * la.position.entry_price
            for la in lanes.values() if la.position is not None
        )

    def close_position(lane: _Lane, ts: pd.Timestamp, raw_price: float, reason: str) -> None:
        nonlocal equity
        pos = lane.position
        exit_side = "sell" if pos.intent.side == "long" else "buy"
        fill = config.slippage.fill_price(raw_price, exit_side)
        exit_fee = config.fees.taker_fee_usd(pos.quantity * fill)
        gross = _unrealized(pos, fill)
        equity += gross - exit_fee + pos.funding_usd
        direction = 1.0 if pos.intent.side == "long" else -1.0
        trades.append(
            Trade(
                side=pos.intent.side, quantity=pos.quantity,
                entry_ts=pos.entry_ts, entry_price=pos.entry_price,
                exit_ts=ts, exit_price=fill, exit_reason=reason,
                gross_pnl_usd=gross, fees_usd=pos.entry_fee_usd + exit_fee,
                funding_usd=pos.funding_usd, entry_reason=pos.intent.reason,
                mae_usd=direction * pos.quantity * (pos.worst_price - pos.entry_price),
                mfe_usd=direction * pos.quantity * (pos.best_price - pos.entry_price),
                symbol=lane.symbol,
            )
        )
        lane.position = None

    for raw_ts in timeline:
        # Lanes with a bar at this timestamp, past warmup, in priority order.
        active: list[tuple[_Lane, int]] = []
        for s in order:
            lane = lanes[s]
            if lane.cursor < lane.n and lane.ts_arr[lane.cursor] == raw_ts:
                j = lane.cursor
                lane.cursor += 1
                if j >= lane.start:
                    active.append((lane, j))
        if not active:
            continue

        # 1) Funding on positions held into this bar (events in (prev_ts, ts]);
        #    events while flat are consumed without effect.
        for lane, j in active:
            bar = lane.df.iloc[j]
            while lane.f_idx < len(lane.f_ts) and lane.f_ts[lane.f_idx] <= raw_ts:
                if lane.position is not None and lane.f_ts[lane.f_idx] > lane.ts_arr[j - 1]:
                    direction = -1.0 if lane.position.intent.side == "long" else 1.0
                    notional = lane.position.quantity * float(bar["close"])
                    lane.position.funding_usd += direction * float(lane.f_rate[lane.f_idx]) * notional
                lane.f_idx += 1

        # 2) Fill last bar's intents at this bar's open, sized from CURRENT
        #    shared equity, capped by total portfolio exposure.
        for lane, j in active:
            if lane.position is None and lane.pending is not None:
                intent = lane.pending
                entry_side = "buy" if intent.side == "long" else "sell"
                fill = config.slippage.fill_price(float(lane.df.iloc[j]["open"]), entry_side)
                sizing = size_position(
                    equity_usd=equity, entry_price=fill, stop_price=intent.stop_price,
                    side=intent.side, config=config.risk, limits=config.limits,
                )
                exposure_cap = equity * config.max_total_exposure_pct / 100.0
                if not sizing.approved:
                    lane.skipped_by_sizing += 1
                    logger.debug("%s sizing rejected at %s: %s", lane.symbol, raw_ts, sizing.reasons)
                elif committed_notional() + sizing.notional_usd > exposure_cap:
                    skipped_exposure += 1
                else:
                    fee = config.fees.taker_fee_usd(sizing.notional_usd)
                    equity -= fee
                    lane.position = _OpenPosition(
                        intent=intent, quantity=sizing.quantity, entry_price=fill,
                        entry_ts=lane.timestamps.iloc[j], entry_bar=j, entry_fee_usd=fee,
                    )
            lane.pending = None

        # 3) Exits — before entry signals, so a closing position frees its
        #    slot for same-timestamp signals. Just-entered positions are
        #    checked too: a stop can be hit in the entry bar.
        for lane, j in active:
            if lane.position is None:
                continue
            bar = lane.df.iloc[j]
            lane.position.track_excursion(float(bar["high"]), float(bar["low"]))
            hit = _check_intrabar_exit(lane.position, float(bar["high"]), float(bar["low"]))
            if hit is not None:
                close_position(lane, lane.timestamps.iloc[j], hit[1], hit[0])
            elif j - lane.position.entry_bar >= config.max_holding_bars:
                close_position(lane, lane.timestamps.iloc[j], float(bar["close"]), "max_holding")

        # 4) Entry signals at bar close. All strategies are consulted, then
        #    slots are granted in alphabetical order (v1 tie-break).
        if equity > 0:
            free = config.max_concurrent_positions - slots_used()
            for lane, j in active:
                if lane.position is not None or lane.pending is not None or j >= lane.n - 1:
                    continue
                intent = lane.strategy.signal(lane.df, j)
                if intent is None:
                    continue
                if free > 0:
                    lane.pending = intent
                    free -= 1
                else:
                    skipped_slots += 1

        # 5) Mark portfolio equity at this timestamp's close. Positions on
        #    lanes without a bar here are marked at their last seen close.
        for lane, j in active:
            lane.last_ts = lane.timestamps.iloc[j]
            lane.last_close = float(lane.df.iloc[j]["close"])
        mark = equity + sum(
            _unrealized(la.position, la.last_close)
            for la in lanes.values() if la.position is not None
        )
        curve.append((pd.Timestamp(raw_ts), mark))

        if equity <= 0:
            logger.warning("portfolio equity depleted at %s — halting backtest", raw_ts)
            break

    # Force-close anything still open at each symbol's last processed bar.
    for s in order:
        lane = lanes[s]
        if lane.position is not None:
            close_position(lane, lane.last_ts, lane.last_close, "end_of_data")
    if curve:
        curve[-1] = (curve[-1][0], equity)

    peak, max_dd = float("-inf"), 0.0
    for _, value in curve:
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, (peak - value) / peak * 100.0)

    per_symbol = {}
    for s in order:
        sym_trades = [t for t in trades if t.symbol == s]
        per_symbol[s] = SymbolSummary(
            symbol=s, num_trades=len(sym_trades),
            net_pnl_usd=sum(t.net_pnl_usd for t in sym_trades),
            fees_usd=sum(t.fees_usd for t in sym_trades),
            funding_usd=sum(t.funding_usd for t in sym_trades),
            skipped_by_sizing=lanes[s].skipped_by_sizing,
        )

    return PortfolioBacktestResult(
        symbols=tuple(order), timeframe=timeframe, trades=tuple(trades),
        equity_curve=curve, per_symbol=per_symbol, max_drawdown_pct=max_dd,
        total_fees_usd=sum(t.fees_usd for t in trades),
        total_funding_usd=sum(t.funding_usd for t in trades),
        skipped_by_sizing=sum(la.skipped_by_sizing for la in lanes.values()),
        skipped_by_slots=skipped_slots, skipped_by_exposure=skipped_exposure,
        final_equity_usd=equity, config=config,
    )
