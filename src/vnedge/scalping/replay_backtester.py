"""Tick/L2 replay backtester — the real scalper-proof engine.

Replays recorded trades + top-of-book through the ACTUAL scalper
microstructure components (TopOfBook, TradeTick, IncrementalFeatureEngine),
so it proves the genuine signal path, not a reimplementation.

Fill model (conservative maker-in / taker-out):
- Entry is a POST-ONLY limit that joins the favored touch. It fills only when
  a taker trade prints strictly THROUGH it (a seller trades below your bid /
  a buyer trades above your ask). This is pessimistic on fill rate and,
  crucially, captures ADVERSE SELECTION: your passive order fills exactly when
  the market is pushing against you. Optimistic touch-fills are the #1 way
  scalper backtests lie; we don't do them.
- Exit is a TAKER market order at the opposite touch + slippage when the
  target or stop is reached. Maker fee on entry, taker fee on exit.
- Unfilled entries expire after ttl_ms and are counted as MISSED.

Fill models (choose via `queue_aware`):
- default (trade-through): fills when a taker prints strictly THROUGH the
  resting price — pessimistic, no queue assumption.
- queue_aware=True (FIFO): the resting size at the touch when we joined is our
  queue; same-side taker volume at-or-through our price clears that queue
  first, and we fill only once it is exhausted. Uses the touch depth the L2
  recorder banks; can be harsher (a queue that never clears never fills).

v1 scope (documented, not hidden): single position, no cancel/replace, and the
queue model uses top-of-book depth only (no walking deeper L2 for partials).
Deliberately harsh; a strategy that survives has a real chance live, one that
dies here is dead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import pandas as pd

from vnedge.scalping.features import IncrementalFeatureEngine, ScalperFeatures
from vnedge.scalping.microstructure import TopOfBook, TradeTick
from vnedge.scalping.parameter_registry import ExitPolicy


@dataclass(frozen=True)
class ReplayQuote:
    side: str          # "buy" | "sell" — the direction we want to end up long/short
    ttl_ms: int
    stop_bps: float    # stop distance from entry, basis points
    target_bps: float  # take-profit distance, basis points

    def __post_init__(self) -> None:
        if self.ttl_ms <= 0 or self.stop_bps <= 0 or self.target_bps <= 0:
            raise ValueError("ttl_ms, stop_bps, target_bps must be positive")


class QuotingScalper(Protocol):
    def quote(self, features: ScalperFeatures, top: TopOfBook) -> ReplayQuote | None: ...


@dataclass(frozen=True)
class ReplayFees:
    maker_bps: float = 2.0   # entry (limit). Set negative for a rebate.
    taker_bps: float = 5.0   # exit (market)
    slippage_bps: float = 1.0


@dataclass(frozen=True)
class ReplayTrade:
    side: str
    entry_ts: datetime
    entry_price: float
    exit_ts: datetime
    exit_price: float
    exit_reason: str          # "target" | "stop" | "end"
    gross_bps: float
    fees_bps: float
    adverse_bps: float        # worst adverse mid excursion while open (MAE), <= 0

    @property
    def net_bps(self) -> float:
        return self.gross_bps - self.fees_bps


@dataclass
class ReplayResult:
    trades: list[ReplayTrade] = field(default_factory=list)
    quotes_placed: int = 0
    missed_fills: int = 0
    open_quotes_at_end: int = 0
    notional_usd: float = 100.0

    @property
    def filled(self) -> int:
        return len(self.trades)

    @property
    def fill_rate(self) -> float:
        return self.filled / self.quotes_placed if self.quotes_placed else 0.0

    @property
    def net_usd(self) -> float:
        return sum(t.net_bps / 10_000.0 * self.notional_usd for t in self.trades)

    @property
    def summary(self) -> str:
        if not self.trades:
            return (f"0 fills / {self.quotes_placed} quotes "
                    f"({self.missed_fills} missed, "
                    f"{self.open_quotes_at_end} open at end) — no completed trades")
        wins = sum(1 for t in self.trades if t.net_bps > 0)
        avg_adverse = sum(t.adverse_bps for t in self.trades) / len(self.trades)
        return (
            f"{self.filled} fills / {self.quotes_placed} quotes "
            f"(fill rate {self.fill_rate:.0%}, {self.missed_fills} missed, "
            f"{self.open_quotes_at_end} open at end) | "
            f"net ${self.net_usd:+.2f} on ${self.notional_usd:.0f} notional | "
            f"win {wins / len(self.trades):.0%} | "
            f"avg adverse selection {avg_adverse:+.2f}bps"
        )


# --- Event loading ----------------------------------------------------------------

def _load_stream_frame(stream_base: Path, day: str) -> pd.DataFrame | None:
    """Read one stream for a day from EITHER layout, concatenated in shard
    order: new atomic shards (stream=<s>/<day>/*.parquet) and/or the legacy
    single file (stream=<s>/<day>.parquet). Returns None if neither exists."""
    frames: list[pd.DataFrame] = []
    shard_dir = stream_base / day
    if shard_dir.is_dir():
        for shard in sorted(shard_dir.glob("*.parquet")):
            frames.append(pd.read_parquet(shard))
    single = stream_base / f"{day}.parquet"
    if single.exists():
        frames.append(pd.read_parquet(single))
    if not frames:
        return None
    return pd.concat(frames, ignore_index=True)


def load_tick_events(
    data_root: Path | str, exchange: str, symbol: str, day: str
) -> list[tuple[int, str, object]]:
    """Merge recorded trades + book into one time-ordered event list.
    Returns (ts_ms, kind, obj) tuples; kind in {"book","trade"}. Reads both the
    L1 legacy schema and the L2 schema (the level-0 columns are identical)."""
    root = Path(data_root) / "ticks" / f"exchange={exchange}"
    safe = symbol.split(":")[0].replace("/", "")
    book_df = _load_stream_frame(root / f"symbol={safe}" / "stream=book", day)
    trade_df = _load_stream_frame(root / f"symbol={safe}" / "stream=trades", day)
    events: list[tuple[int, str, object]] = []
    if book_df is not None:
        for r in book_df.itertuples():
            try:
                top = TopOfBook(
                    symbol=symbol, bid=r.bid, bid_size=r.bid_qty,
                    ask=r.ask, ask_size=r.ask_qty,
                    event_time=datetime.fromtimestamp(r.ts_ms / 1000, tz=UTC),
                )
            except ValueError:
                continue  # crossed/invalid book snapshot — skip
            events.append((int(r.ts_ms), "book", top))
    if trade_df is not None:
        for r in trade_df.itertuples():
            try:
                tick = TradeTick(
                    symbol=symbol,
                    price=r.price,
                    quantity=r.amount,
                    taker_side=r.side,
                    event_time=datetime.fromtimestamp(r.ts_ms / 1000, tz=UTC),
                )
            except ValueError:
                continue
            events.append((int(r.ts_ms), "trade", tick))
    events.sort(key=lambda e: (e[0], 0 if e[1] == "book" else 1))
    return events


# --- Engine -----------------------------------------------------------------------

@dataclass
class _Resting:
    side: str
    limit_price: float
    placed_ms: int
    ttl_ms: int
    stop_bps: float
    target_bps: float
    queue_ahead: float = 0.0      # size resting at our price when we joined (queue model)
    queue_consumed: float = 0.0   # same-side taker volume that has cleared ahead of us


@dataclass
class _Open:
    side: str
    entry_price: float
    entry_ms: int
    entry_mid: float
    stop_price: float
    target_price: float
    worst_adverse_bps: float = 0.0
    best_favorable_bps: float = 0.0


class TickReplayBacktester:
    def __init__(self, fees: ReplayFees = ReplayFees(), notional_usd: float = 100.0,
                 *, queue_aware: bool = False,
                 exit_policy: ExitPolicy | None = None) -> None:
        self.fees = fees
        self.notional_usd = notional_usd
        # queue_aware=True uses the FIFO queue model (needs the touch depth the
        # L2 recorder banks); default False keeps the strict trade-through model.
        self.queue_aware = queue_aware
        self.exit_policy = exit_policy

    def run(self, events, scalper: QuotingScalper) -> ReplayResult:
        engine = IncrementalFeatureEngine()
        result = ReplayResult(notional_usd=self.notional_usd)
        top: TopOfBook | None = None
        resting: _Resting | None = None
        position: _Open | None = None

        for ts_ms, kind, obj in events:
            if self._expired(resting, ts_ms):
                result.missed_fills += 1
                resting = None
            if kind == "book":
                top = obj
                feats = engine.on_book(top)
                # adverse-selection tracking on the live position
                if position is not None:
                    drift = (top.mid_price - position.entry_mid) / position.entry_mid * 10_000.0
                    signed = drift if position.side == "buy" else -drift
                    position.worst_adverse_bps = min(position.worst_adverse_bps, signed)
                    position.best_favorable_bps = max(
                        position.best_favorable_bps,
                        self._tradable_bps(position, top),
                    )
                    # exit checks on book move (taker out)
                    if self._check_exit(position, top, ts_ms, result):
                        position = None
                # place a new quote only when flat and nothing resting
                if position is None and resting is None:
                    q = scalper.quote(feats, top)
                    if q is not None:
                        limit = top.bid if q.side == "buy" else top.ask
                        queue_ahead = 0.0
                        if self.queue_aware:
                            queue_ahead = top.bid_size if q.side == "buy" else top.ask_size
                        resting = _Resting(q.side, limit, ts_ms, q.ttl_ms,
                                           q.stop_bps, q.target_bps, queue_ahead=queue_ahead)
                        result.quotes_placed += 1
            else:  # trade
                engine.on_trade(obj)
                # Conservative maker fill: the taker must trade STRICTLY
                # THROUGH our resting price (price < our bid / > our ask), so
                # the whole level cleared and we definitely filled — touch
                # (<=) would assume front-of-queue. And the fill trade must
                # post-date our quote (ts_ms > placed_ms): a same-instant
                # trade was already in flight before our order joined the
                # queue. Both guards keep the engine honest about fills.
                if (resting is not None and position is None
                        and ts_ms > resting.placed_ms):
                    if self.queue_aware:
                        # FIFO queue model: same-side taker volume at OR through
                        # our price first clears the size that was resting ahead
                        # of us at placement; we fill only once that queue is
                        # exhausted. More realistic than trade-through (and can
                        # be harsher: if the queue never clears before the price
                        # leaves or the TTL, we simply never fill).
                        qualifies = (
                            resting.side == "buy" and obj.taker_side == "sell"
                            and obj.price <= resting.limit_price
                        ) or (
                            resting.side == "sell" and obj.taker_side == "buy"
                            and obj.price >= resting.limit_price
                        )
                        if qualifies:
                            resting.queue_consumed += obj.quantity
                            if (resting.queue_consumed >= resting.queue_ahead
                                    and top is not None):
                                position = self._open(resting, top, ts_ms)
                                resting = None
                    else:
                        filled = (
                            resting.side == "buy" and obj.taker_side == "sell"
                            and obj.price < resting.limit_price
                        ) or (
                            resting.side == "sell" and obj.taker_side == "buy"
                            and obj.price > resting.limit_price
                        )
                        if filled and top is not None:
                            position = self._open(resting, top, ts_ms)
                            resting = None

        if resting is not None:
            if self._expired(resting, ts_ms):
                result.missed_fills += 1
            else:
                result.open_quotes_at_end += 1

        # force-close any open position at the tradable touch, never mid
        if position is not None and top is not None:
            exit_price = top.bid if position.side == "buy" else top.ask
            self._close(position, exit_price, ts_ms, "end", result)
        return result

    @staticmethod
    def _expired(resting: _Resting | None, ts_ms: int) -> bool:
        return resting is not None and ts_ms - resting.placed_ms >= resting.ttl_ms

    def _open(self, resting: _Resting, top: TopOfBook, ts_ms: int) -> _Open:
        entry = resting.limit_price
        if resting.side == "buy":
            stop = entry * (1 - resting.stop_bps / 10_000.0)
            target = entry * (1 + resting.target_bps / 10_000.0)
        else:
            stop = entry * (1 + resting.stop_bps / 10_000.0)
            target = entry * (1 - resting.target_bps / 10_000.0)
        return _Open(resting.side, entry, ts_ms, top.mid_price, stop, target)

    def _check_exit(self, pos: _Open, top: TopOfBook, ts_ms: int,
                    result: ReplayResult) -> bool:
        """Returns True if the position was closed this book update."""
        current_bps = self._tradable_bps(pos, top)
        policy = self.exit_policy
        if pos.side == "buy":
            if top.bid <= pos.stop_price:
                self._close(pos, top.bid, ts_ms, "stop", result)
                return True
            if policy is not None and policy.adverse_cut_bps > 0 \
                    and current_bps <= -policy.adverse_cut_bps:
                self._close(pos, top.bid, ts_ms, "adverse_cut", result)
                return True
            if top.bid >= pos.target_price:
                self._close(pos, top.bid, ts_ms, "target", result)
                return True
            if self._trail_hit(pos, current_bps):
                self._close(pos, top.bid, ts_ms, "trail", result)
                return True
        else:
            if top.ask >= pos.stop_price:
                self._close(pos, top.ask, ts_ms, "stop", result)
                return True
            if policy is not None and policy.adverse_cut_bps > 0 \
                    and current_bps <= -policy.adverse_cut_bps:
                self._close(pos, top.ask, ts_ms, "adverse_cut", result)
                return True
            if top.ask <= pos.target_price:
                self._close(pos, top.ask, ts_ms, "target", result)
                return True
            if self._trail_hit(pos, current_bps):
                self._close(pos, top.ask, ts_ms, "trail", result)
                return True
        return False

    @staticmethod
    def _tradable_bps(pos: _Open, top: TopOfBook) -> float:
        if pos.side == "buy":
            return (top.bid - pos.entry_price) / pos.entry_price * 10_000.0
        return (pos.entry_price - top.ask) / pos.entry_price * 10_000.0

    def _trail_hit(self, pos: _Open, current_bps: float) -> bool:
        policy = self.exit_policy
        if policy is None or policy.trail_after_bps <= 0 or policy.trail_distance_bps <= 0:
            return False
        if pos.best_favorable_bps < policy.trail_after_bps:
            return False
        return current_bps <= pos.best_favorable_bps - policy.trail_distance_bps

    def _close(self, pos: _Open, raw_exit: float, ts_ms: int, reason: str,
               result: ReplayResult) -> None:
        slip = self.fees.slippage_bps / 10_000.0
        exit_px = raw_exit * (1 - slip) if pos.side == "buy" else raw_exit * (1 + slip)
        if pos.side == "buy":
            gross_bps = (exit_px - pos.entry_price) / pos.entry_price * 10_000.0
        else:
            gross_bps = (pos.entry_price - exit_px) / pos.entry_price * 10_000.0
        fees_bps = self.fees.maker_bps + self.fees.taker_bps
        result.trades.append(ReplayTrade(
            side=pos.side,
            entry_ts=datetime.fromtimestamp(pos.entry_ms / 1000, tz=UTC),
            entry_price=pos.entry_price,
            exit_ts=datetime.fromtimestamp(ts_ms / 1000, tz=UTC),
            exit_price=exit_px, exit_reason=reason,
            gross_bps=gross_bps, fees_bps=fees_bps,
            adverse_bps=pos.worst_adverse_bps,
        ))


class ImbalanceScalper:
    """Reference quoting strategy: when the top-of-book is imbalanced enough
    and the spread is thin, join the heavy side expecting continuation. Exists
    to exercise the engine — assume negative until the replay says otherwise."""

    def __init__(self, *, min_imbalance: float = 0.35, max_spread_bps: float = 3.0,
                 ttl_ms: int = 3000, stop_bps: float = 6.0, target_bps: float = 8.0) -> None:
        self.min_imbalance = min_imbalance
        self.max_spread_bps = max_spread_bps
        self.ttl_ms = ttl_ms
        self.stop_bps = stop_bps
        self.target_bps = target_bps

    def quote(self, features: ScalperFeatures, top: TopOfBook) -> ReplayQuote | None:
        if top.spread_bps > self.max_spread_bps:
            return None
        imb = features.book_imbalance
        if imb >= self.min_imbalance:
            return ReplayQuote("buy", self.ttl_ms, self.stop_bps, self.target_bps)
        if imb <= -self.min_imbalance:
            return ReplayQuote("sell", self.ttl_ms, self.stop_bps, self.target_bps)
        return None
