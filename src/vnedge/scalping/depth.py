"""L2 order-book depth primitive and depth-aware features.

Phase 2B consumes the L2 ladder the recorder banks (see
docs/SCALPER_REPLAY_CONTRACT.md §2.2). ``OrderBookL2`` is an immutable depth
snapshot that derives the things top-of-book cannot see:

- multi-level book imbalance (not just level 0),
- resting liquidity within N bps of mid,
- the realised price / slippage of *walking the book* to fill a clip (VWAP),
  i.e. liquidity-aware slippage instead of a flat ``slippage_bps`` guess.

These feed the fee-wall metric and per-symbol liquidity ranking, and the
queue-aware maker-fill model, in later 2B steps. This module reads depth; it
does not place orders.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from vnedge.scalping.microstructure import TopOfBook, _as_aware_utc

# (price, qty) pairs, best level first: bids descending in price, asks ascending
Level = tuple[float, float]


@dataclass(frozen=True)
class FillWalk:
    """Result of walking the book to fill a target notional."""
    avg_price: float          # VWAP of the consumed levels
    slippage_bps: float       # signed vs mid: >0 = paid worse than mid
    filled_notional: float    # how much of the target actually cleared
    fully_filled: bool        # False if the visible book was exhausted first


@dataclass(frozen=True)
class OrderBookL2:
    symbol: str
    bids: tuple[Level, ...]   # best-first (descending price)
    asks: tuple[Level, ...]   # best-first (ascending price)
    event_time: datetime

    def __post_init__(self) -> None:
        if not self.bids or not self.asks:
            raise ValueError(f"empty book for {self.symbol}")
        if self.bids[0][0] <= 0 or self.asks[0][0] <= 0:
            raise ValueError("book prices must be positive")
        if self.bids[0][0] >= self.asks[0][0]:
            raise ValueError(
                f"crossed/locked book for {self.symbol}: "
                f"{self.bids[0][0]} >= {self.asks[0][0]}")
        # levels must be monotonic away from the touch and sized non-negative
        for ladder, descending in ((self.bids, True), (self.asks, False)):
            prev = None
            for px, qty in ladder:
                if qty < 0:
                    raise ValueError("book sizes cannot be negative")
                if prev is not None and ((descending and px >= prev)
                                         or (not descending and px <= prev)):
                    raise ValueError(f"unordered ladder for {self.symbol}")
                prev = px
        object.__setattr__(self, "event_time", _as_aware_utc(self.event_time))

    # --- basics ---------------------------------------------------------------
    @property
    def best_bid(self) -> float:
        return self.bids[0][0]

    @property
    def best_ask(self) -> float:
        return self.asks[0][0]

    @property
    def mid_price(self) -> float:
        return (self.best_bid + self.best_ask) / 2.0

    @property
    def spread_bps(self) -> float:
        return (self.best_ask - self.best_bid) / self.mid_price * 10_000.0

    def top_of_book(self) -> TopOfBook:
        """The level-0 L1 view, for the existing top-of-book replay engine."""
        return TopOfBook(
            symbol=self.symbol,
            bid=self.best_bid, bid_size=self.bids[0][1],
            ask=self.best_ask, ask_size=self.asks[0][1],
            event_time=self.event_time,
        )

    # --- depth-aware features -------------------------------------------------
    def depth_imbalance(self, levels: int | None = None) -> float:
        """Cumulative (bid - ask) / (bid + ask) size over the first `levels`
        (all levels if None). +1 = all bid, -1 = all ask, 0 = balanced."""
        n_b = len(self.bids) if levels is None else min(levels, len(self.bids))
        n_a = len(self.asks) if levels is None else min(levels, len(self.asks))
        bid = sum(q for _, q in self.bids[:n_b])
        ask = sum(q for _, q in self.asks[:n_a])
        total = bid + ask
        return 0.0 if total <= 0 else (bid - ask) / total

    def liquidity_usd_within_bps(self, bps: float) -> float:
        """Resting notional (both sides) within `bps` of mid."""
        mid = self.mid_price
        lo = mid * (1 - bps / 10_000.0)
        hi = mid * (1 + bps / 10_000.0)
        bid_usd = sum(px * q for px, q in self.bids if px >= lo)
        ask_usd = sum(px * q for px, q in self.asks if px <= hi)
        return bid_usd + ask_usd

    def fill_walk(self, notional_usd: float, side: str) -> FillWalk:
        """Walk the book to fill `notional_usd`. side="buy" lifts asks,
        "sell" hits bids. Returns realised VWAP, signed slippage vs mid, the
        notional actually cleared, and whether the visible book sufficed."""
        if notional_usd <= 0:
            raise ValueError("notional_usd must be positive")
        if side not in ("buy", "sell"):
            raise ValueError(f"invalid side: {side}")
        ladder = self.asks if side == "buy" else self.bids
        mid = self.mid_price
        remaining = notional_usd
        cost = 0.0        # sum(px * base)
        base = 0.0        # sum(base)
        for px, qty in ladder:
            level_usd = px * qty
            take = min(remaining, level_usd)
            if take <= 0:
                continue
            base += take / px
            cost += take
            remaining -= take
            if remaining <= 1e-9:
                break
        if base <= 0:
            return FillWalk(mid, math.inf, 0.0, False)
        avg = cost / base
        slip = ((avg - mid) if side == "buy" else (mid - avg)) / mid * 10_000.0
        return FillWalk(avg, slip, cost, remaining <= 1e-9)

    # --- construction from a recorded L2 row ----------------------------------
    @classmethod
    def from_row(cls, row, symbol: str, levels: int = 10) -> "OrderBookL2":
        """Build from a recorded book row (bid_px_i/bid_qty_i/... columns).
        NaN-price levels (padded empties) are dropped. `row` is any object with
        attribute access (e.g. an itertuples row) or a mapping."""
        def get(name):
            try:
                return getattr(row, name)      # itertuples namedtuple row
            except AttributeError:
                return row[name]               # mapping / dict row

        def ladder(prefix):
            out = []
            for i in range(levels):
                px = float(get(f"{prefix}_px_{i}"))
                if math.isnan(px):
                    break
                out.append((px, float(get(f"{prefix}_qty_{i}"))))
            return tuple(out)

        return cls(symbol=symbol, bids=ladder("bid"), asks=ladder("ask"),
                   event_time=datetime.fromtimestamp(int(get("ts_ms")) / 1000, tz=UTC))


def load_l2_books(
    data_root: Path | str, exchange: str, symbol: str, day: str, levels: int = 10
) -> list[tuple[int, OrderBookL2]]:
    """Load recorded L2 book snapshots for a day as time-ordered
    (ts_ms, OrderBookL2). Rows without an L2 ladder (legacy L1-only data) are
    skipped, so a mixed day yields only its true depth snapshots."""
    from vnedge.scalping.replay_backtester import _load_stream_frame

    root = Path(data_root) / "ticks" / f"exchange={exchange}"
    safe = symbol.split(":")[0].replace("/", "")
    df = _load_stream_frame(root / f"symbol={safe}" / "stream=book", day)
    if df is None or "bid_px_0" not in df.columns:
        return []
    out: list[tuple[int, OrderBookL2]] = []
    for r in df.itertuples():
        try:
            out.append((int(r.ts_ms), OrderBookL2.from_row(r, symbol, levels)))
        except ValueError:
            continue  # crossed / empty (L1-padded) snapshot
    out.sort(key=lambda e: e[0])
    return out
