"""Event-loop candle backtester for USDT-margined perpetuals.

Realism rules (docs/DESIGN.md, backtesting rules):

- Decisions at bar close, fills at next bar open. Lookahead is structurally
  impossible: the engine, not the strategy, controls what data exists at
  decision time.
- Taker fees and adverse slippage on every fill.
- Funding cashflows applied at the actual timestamps from the ingested
  funding series (longs pay positive rates, shorts receive).
- Intrabar exits are conservative: if both stop and take-profit lie inside
  one bar's range, the STOP is assumed to fill. Stops fill with slippage.
- Position size comes from the SAME ``size_position`` function live trading
  uses — backtest and live can never disagree on sizing math. If sizing
  rejects (exchange minimums, exposure caps), the trade is skipped and
  counted, never force-fitted.
- One position at a time, one symbol per run (v1).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pandas as pd
from pydantic import BaseModel, Field

from vnedge.backtest.fee_model import FeeModel
from vnedge.backtest.slippage_model import SlippageModel
from vnedge.runtime.active_exit import ActiveExitState
from vnedge.config.risk_config import RiskConfig
from vnedge.risk.position_sizer import SymbolLimits, size_position
from vnedge.risk.protections import ProtectionConfig, ProtectionState
from vnedge.runtime.daily_factory import (
    DailySignalFactoryConfig,
    entry_block_reason,
    session_day,
    should_force_flatten,
)
from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent
from vnedge.strategy.indicators import atr as _atr_indicator

logger = logging.getLogger(__name__)


class BacktestConfig(BaseModel):
    model_config = {"frozen": True, "arbitrary_types_allowed": True}

    initial_equity_usd: float = Field(default=500.0, gt=0)
    max_holding_bars: int = Field(default=48, ge=1)
    fees: FeeModel = Field(default_factory=FeeModel)
    slippage: SlippageModel = Field(default_factory=SlippageModel)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    limits: SymbolLimits = Field(
        default=SymbolLimits(
            min_qty=0.0001, qty_step=0.0001, min_notional_usd=5.0,
            maintenance_margin_rate=0.005,
        )
    )
    # Entry protections (risk/protections.py) — the SAME state machine the
    # live paper/shadow runner consults, so research and operations block the
    # same entry decisions. ALL DEFAULT OFF (zero behavior change).
    protections: ProtectionConfig = Field(default_factory=ProtectionConfig)
    # Same-day signal-factory discipline. Default OFF to preserve historical
    # judgments; enable when researching lanes intended to open and close
    # within the session.
    daily_factory: DailySignalFactoryConfig = Field(
        default_factory=DailySignalFactoryConfig
    )

    # Breakeven / profit-lock stop management (DEFAULT OFF). The 2026-08-02
    # ledger study found 63% of losers ran into profit then gave it back. When
    # `breakeven_arm_bps` is set, the stop moves to `profit_lock_bps` above entry
    # (0 = breakeven) once favorable excursion reaches the arm threshold. Armed at
    # bar close on that bar's favorable extreme and applied to LATER bars, so
    # there is no intrabar lookahead (conservative vs a tick-level breakeven).
    breakeven_arm_bps: float | None = Field(default=None)
    profit_lock_bps: float = Field(default=0.0, ge=0.0)

    # RESEARCH↔RUNTIME EXIT PARITY (2026-08-02). When True the backtester drives
    # the EXACT `ActiveExitState` machine that paper/live use — TP-ladder partials,
    # fee-aware breakeven, and (with `trail_atr_mult`) a real per-bar ATR-chandelier
    # trail — instead of the single stop/TP check. This is what lets a scanner be
    # JUDGED on the same exit it will RUN. Default off = legacy behavior unchanged;
    # the simple breakeven_arm_bps above is ignored when this is on.
    use_active_exit: bool = Field(default=False)
    trail_atr_mult: float = Field(default=0.0, ge=0.0)
    # Canonical ATR window for the trail — computed by the backtester itself, NOT
    # read from a strategy column (those are named inconsistently: `atr`,
    # `atr_margin`, …). Keeps trailing identical across every scanner.
    trail_atr_window: int = Field(default=14, ge=1)


@dataclass(frozen=True)
class Trade:
    side: str
    quantity: float
    entry_ts: pd.Timestamp
    entry_price: float
    exit_ts: pd.Timestamp
    exit_price: float
    exit_reason: str  # "stop" | "take_profit" | "max_holding" | "end_of_data"
    gross_pnl_usd: float
    fees_usd: float
    funding_usd: float  # negative = paid
    entry_reason: str
    mae_usd: float = 0.0  # max adverse excursion while open (<= 0)
    mfe_usd: float = 0.0  # max favorable excursion while open (>= 0)
    symbol: str = ""  # set by the portfolio engine; single-symbol runs leave it empty

    @property
    def net_pnl_usd(self) -> float:
        return self.gross_pnl_usd - self.fees_usd + self.funding_usd


@dataclass
class _OpenPosition:
    intent: SignalIntent
    quantity: float
    entry_price: float
    entry_ts: pd.Timestamp
    entry_bar: int
    entry_fee_usd: float
    funding_usd: float = 0.0
    best_price: float = 0.0
    worst_price: float = 0.0
    managed_stop_price: float = 0.0  # effective stop; == intent.stop until armed
    stop_armed: bool = False         # True once breakeven/profit-lock moved it
    remaining_quantity: float = 0.0  # shrinks as TP-ladder partials close
    original_quantity: float = 0.0
    exit_state: object | None = None  # ActiveExitState when use_active_exit

    def __post_init__(self) -> None:
        self.best_price = self.worst_price = self.entry_price
        self.managed_stop_price = self.intent.stop_price
        self.remaining_quantity = self.original_quantity = self.quantity

    def track_excursion(self, high: float, low: float) -> None:
        if self.intent.side == "long":
            self.best_price = max(self.best_price, high)
            self.worst_price = min(self.worst_price, low)
        else:
            self.best_price = min(self.best_price, low)
            self.worst_price = max(self.worst_price, high)

    def arm_breakeven(self, arm_bps: float, lock_bps: float) -> None:
        """Once favorable excursion reaches ``arm_bps``, ratchet the stop to
        ``lock_bps`` beyond entry (0 = breakeven). Tighten-only — never loosens."""
        e = self.entry_price
        if self.intent.side == "long":
            if (self.best_price - e) / e * 1e4 >= arm_bps:
                lock = e * (1.0 + lock_bps / 1e4)
                if lock > self.managed_stop_price:
                    self.managed_stop_price = lock
                    self.stop_armed = True
        else:
            if (e - self.best_price) / e * 1e4 >= arm_bps:
                lock = e * (1.0 - lock_bps / 1e4)
                if lock < self.managed_stop_price:
                    self.managed_stop_price = lock
                    self.stop_armed = True


@dataclass(frozen=True)
class BacktestResult:
    symbol: str
    timeframe: str
    trades: tuple[Trade, ...]
    equity_curve: pd.Series  # timestamp-indexed, marked at bar close
    skipped_by_sizing: int
    final_equity_usd: float
    config: BacktestConfig = field(repr=False, default_factory=BacktestConfig)
    # (timestamp, reason) for every entry decision the protections blocked —
    # empty unless config.protections enables something. Mirrors the live
    # runner's lane_eval skip_reason records for engine-parity checks.
    protection_blocked: tuple[tuple[pd.Timestamp, str], ...] = ()
    # Entry decisions blocked by daily-factory rules (cutoff, max entries,
    # daily target already hit). Kept separate from protection blocks so
    # research can tell "bad setup" from "factory closed for the day".
    factory_blocked: tuple[tuple[pd.Timestamp, str], ...] = ()


def _unrealized(pos: _OpenPosition, price: float) -> float:
    direction = 1.0 if pos.intent.side == "long" else -1.0
    return direction * pos.remaining_quantity * (price - pos.entry_price)


def _check_intrabar_exit(
    pos: _OpenPosition, high: float, low: float
) -> tuple[str, float] | None:
    """Conservative exit resolution inside one bar. Stop always wins ties.
    Uses the MANAGED stop (== the original stop until breakeven/profit-lock
    arms it), so a rescued give-back exits at breakeven, labelled 'breakeven'."""
    intent = pos.intent
    stop = pos.managed_stop_price
    stop_reason = "breakeven" if pos.stop_armed else "stop"
    if intent.side == "long":
        if low <= stop:
            return stop_reason, stop
        if intent.take_profit_price is not None and high >= intent.take_profit_price:
            return "take_profit", intent.take_profit_price
    else:
        if high >= stop:
            return stop_reason, stop
        if intent.take_profit_price is not None and low <= intent.take_profit_price:
            return "take_profit", intent.take_profit_price
    return None


def run_backtest(
    candles: pd.DataFrame,
    funding: pd.DataFrame | None,
    strategy: BaseStrategy,
    config: BacktestConfig,
    *,
    symbol: str = "BTC/USDT:USDT",
    timeframe: str = "1h",
) -> BacktestResult:
    if candles.empty:
        raise ValueError("empty candle frame")
    if not candles["timestamp"].is_monotonic_increasing or candles["timestamp"].duplicated().any():
        raise ValueError("candles must be gate-validated: sorted, unique timestamps")

    df = strategy.prepare(candles).reset_index(drop=True)
    if len(df) != len(candles):
        raise ValueError("strategy.prepare() must not add or drop rows")

    # Canonical trail ATR (strategy-agnostic) — only when the active-exit trail is on.
    trail_atr = (
        _atr_indicator(df, config.trail_atr_window).to_numpy()
        if config.use_active_exit and config.trail_atr_mult > 0.0
        else None
    )

    # Funding events as parallel arrays with a moving cursor.
    if funding is not None and not funding.empty:
        f_ts = funding["timestamp"].to_numpy()
        f_rate = funding["funding_rate"].to_numpy()
    else:
        f_ts, f_rate = [], []
    f_idx = 0

    equity = config.initial_equity_usd
    position: _OpenPosition | None = None
    pending: SignalIntent | None = None
    trades: list[Trade] = []
    skipped = 0
    curve: list[float] = []
    # Entry protections: identical state machine + bar-index semantics as the
    # live runner (exit recorded at its bar, entries consulted at decision
    # time). Exits below NEVER consult it — stops/TPs always close.
    protections = ProtectionState(config.protections)
    protection_blocked: list[tuple[pd.Timestamp, str]] = []
    factory_blocked: list[tuple[pd.Timestamp, str]] = []
    n = len(df)
    start = max(strategy.warmup_bars, 1)
    timestamps = df["timestamp"]
    factory_seed_ts = timestamps.iloc[start] if start < len(timestamps) else timestamps.iloc[-1]
    factory_day = session_day(factory_seed_ts.to_pydatetime(), config.daily_factory)
    factory_entries_today = 0
    factory_day_open_equity = equity

    def roll_factory_day(ts: pd.Timestamp) -> None:
        nonlocal factory_day, factory_entries_today, factory_day_open_equity
        day = session_day(ts.to_pydatetime(), config.daily_factory)
        if day == factory_day:
            return
        factory_day = day
        factory_entries_today = 0
        factory_day_open_equity = equity

    def factory_daily_pnl(mark_price: float) -> float:
        mark = equity + (_unrealized(position, mark_price) if position else 0.0)
        return mark - factory_day_open_equity

    def close_position(
        pos: _OpenPosition, ts, raw_price: float, reason: str, bar_index: int,
        *, close_qty: float | None = None,
    ) -> None:
        """Close all of, or (for a TP-ladder partial) part of, a position. Entry
        fee is allocated by original-quantity fraction; funding by the fraction of
        the still-open size — so the sum across partials matches a single close."""
        nonlocal equity, position
        q = pos.remaining_quantity if close_qty is None else min(abs(close_qty), pos.remaining_quantity)
        if q <= 0:
            return
        direction = 1.0 if pos.intent.side == "long" else -1.0
        exit_side = "sell" if pos.intent.side == "long" else "buy"
        fill = config.slippage.fill_price(raw_price, exit_side)
        exit_fee = config.fees.taker_fee_usd(q * fill)
        gross = direction * q * (fill - pos.entry_price)
        entry_fee_alloc = pos.entry_fee_usd * (q / pos.original_quantity if pos.original_quantity else 1.0)
        funding_alloc = pos.funding_usd * (q / pos.remaining_quantity) if pos.remaining_quantity else 0.0
        equity += gross - exit_fee + funding_alloc
        pos.funding_usd -= funding_alloc
        trades.append(
            Trade(
                side=pos.intent.side, quantity=q,
                entry_ts=pos.entry_ts, entry_price=pos.entry_price,
                exit_ts=ts, exit_price=fill, exit_reason=reason,
                gross_pnl_usd=gross, fees_usd=entry_fee_alloc + exit_fee, funding_usd=funding_alloc,
                entry_reason=pos.intent.reason,
                mae_usd=direction * q * (pos.worst_price - pos.entry_price),
                mfe_usd=direction * q * (pos.best_price - pos.entry_price),
            )
        )
        pos.remaining_quantity -= q
        if pos.remaining_quantity <= 1e-12:
            position = None
            protections.on_exit(reason, bar_index)

    for j in range(start, n):
        bar = df.iloc[j]
        ts = timestamps.iloc[j]
        roll_factory_day(ts)

        # 1) Funding on positions held into this bar (events in (prev_ts, ts]).
        if position is not None:
            while f_idx < len(f_ts) and f_ts[f_idx] <= ts:
                if f_ts[f_idx] > timestamps.iloc[j - 1]:
                    direction = -1.0 if position.intent.side == "long" else 1.0
                    notional = position.remaining_quantity * float(bar["close"])
                    position.funding_usd += direction * float(f_rate[f_idx]) * notional
                f_idx += 1
        else:
            while f_idx < len(f_ts) and f_ts[f_idx] <= ts:
                f_idx += 1

        # 2) Fill last bar's intent at this bar's open.
        if position is None and pending is not None:
            entry_side = "buy" if pending.side == "long" else "sell"
            fill = config.slippage.fill_price(float(bar["open"]), entry_side)
            sizing = size_position(
                equity_usd=equity, entry_price=fill, stop_price=pending.stop_price,
                side=pending.side, config=config.risk, limits=config.limits,
            )
            if sizing.approved:
                fee = config.fees.taker_fee_usd(sizing.notional_usd)
                equity -= fee
                position = _OpenPosition(
                    intent=pending, quantity=sizing.quantity, entry_price=fill,
                    entry_ts=ts, entry_bar=j, entry_fee_usd=fee,
                )
                if config.use_active_exit:
                    position.exit_state = ActiveExitState.from_signal(
                        pending, entry_price=fill, quantity=sizing.quantity,
                        trail_atr_mult=config.trail_atr_mult,
                    )
                factory_entries_today += 1
            else:
                skipped += 1
                logger.debug("sizing rejected at %s: %s", ts, sizing.reasons)
        pending = None

        # 3) Exit checks (applies to just-entered positions too — a stop can
        #    be hit in the entry bar).
        if position is not None:
            hi, lo, cl = float(bar["high"]), float(bar["low"]), float(bar["close"])
            position.track_excursion(hi, lo)
            if config.use_active_exit and position.exit_state is not None:
                # RUNTIME-PARITY exit: drive the SAME ActiveExitState paper/live use —
                # TP-ladder partials, fee-aware breakeven, per-bar ATR trail.
                st: ActiveExitState = position.exit_state
                max_hold = j - position.entry_bar >= config.max_holding_bars
                decision = st.resolve_bar(
                    high=hi, low=lo, close=cl,
                    position_quantity=position.remaining_quantity,
                    min_qty=config.limits.min_qty, qty_step=config.limits.qty_step,
                    max_holding_hit=max_hold,
                )
                if decision is not None:
                    if decision.final:
                        close_position(position, ts, decision.exit_price, decision.reason, j)
                    else:
                        close_position(position, ts, decision.exit_price, decision.reason,
                                       j, close_qty=decision.quantity)
                        if position is not None:  # partial — position stays open
                            st.mark_accepted(decision)
                elif (exit_sig := strategy.exit_signal(
                        df, j, position.intent.side, position.entry_price)) is not None:
                    close_position(position, ts,
                                   float(exit_sig.exit_price) if exit_sig.exit_price is not None else cl,
                                   exit_sig.reason, j)
                elif should_force_flatten(ts.to_pydatetime(), config.daily_factory):
                    close_position(position, ts, cl, "daily_factory_close", j)
                elif trail_atr is not None:
                    # trail the stop for LATER bars off the canonical ATR (no lookahead)
                    a = trail_atr[j]
                    st.trail_stop(float(a) if a == a else 0.0)  # a==a drops NaN warmup
            else:
                # Legacy exit: single stop / take-profit (+ optional simple breakeven).
                hit = _check_intrabar_exit(position, hi, lo)
                if hit is not None:
                    close_position(position, ts, hit[1], hit[0], j)
                elif (exit_sig := strategy.exit_signal(
                        df, j, position.intent.side, position.entry_price)) is not None:
                    close_position(position, ts,
                                   float(exit_sig.exit_price) if exit_sig.exit_price is not None else cl,
                                   exit_sig.reason, j)
                elif should_force_flatten(ts.to_pydatetime(), config.daily_factory):
                    close_position(position, ts, cl, "daily_factory_close", j)
                elif j - position.entry_bar >= config.max_holding_bars:
                    close_position(position, ts, cl, "max_holding", j)
                # arm the simple breakeven for LATER bars (no intrabar lookahead)
                if position is not None and config.breakeven_arm_bps is not None:
                    position.arm_breakeven(config.breakeven_arm_bps, config.profit_lock_bps)

        # 4) New entry decision at this bar's close (only when flat).
        if position is None and j < n - 1 and equity > 0:
            factory_block = entry_block_reason(
                now=ts.to_pydatetime(),
                config=config.daily_factory,
                entries_today=factory_entries_today,
                daily_pnl_usd=factory_daily_pnl(float(bar["close"])),
            )
            allowed, block_reason = protections.entries_allowed(j)
            if factory_block is not None:
                factory_blocked.append((ts, factory_block))
            elif allowed:
                pending = strategy.signal(df, j)
            else:
                protection_blocked.append((ts, block_reason))

        # 5) Mark equity at bar close.
        mark = equity + (_unrealized(position, float(bar["close"])) if position else 0.0)
        curve.append(mark)

        if equity <= 0:
            logger.warning("equity depleted at %s — halting backtest", ts)
            break

    # Force-close anything still open at the last processed bar.
    if position is not None:
        last_j = start + len(curve) - 1
        last = df.iloc[last_j]
        close_position(
            position, last["timestamp"], float(last["close"]), "end_of_data", last_j
        )
        curve[-1] = equity

    equity_curve = pd.Series(
        curve, index=timestamps.iloc[start : start + len(curve)], name="equity"
    )
    return BacktestResult(
        symbol=symbol, timeframe=timeframe, trades=tuple(trades),
        equity_curve=equity_curve, skipped_by_sizing=skipped,
        final_equity_usd=equity, config=config,
        protection_blocked=tuple(protection_blocked),
        factory_blocked=tuple(factory_blocked),
    )
