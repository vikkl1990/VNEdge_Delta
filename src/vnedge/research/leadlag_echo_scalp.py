"""Cross-venue lead-lag echo scalp — tick/L2 research only.

    python -m vnedge.research.leadlag_echo_scalp \
        --pairs BTC --data-root data

THESIS (architect blueprint, Phase 2): the deep books (Binance/Bybit) move
FIRST; Delta India ECHOES with a lag. When a leader venue impulses, a
MAKER-FIRST resting order on Delta's *follower* book can capture the echo —
a trade where our ~100ms latency is irrelevant because the information edge
is measured in seconds, on the venue (Delta) where WE are the sophisticated
player. This is the ONLY scalp path with a chance against the fee wall
(continuous tick book-imbalance is tombstoned): maker fees ~halve the round
trip AND the edge is DIRECTIONAL (the echo), not spread-capture.

This is DISTINCT from ``event_leadlag_alpha`` (candle-level, cheap first
pass). This module is the tick/L2, queue-aware, maker-fill-proven version.

Two structural inputs, aligned by recorder wall-clock ``ts_ms``:
- LEADER = ``binanceusdm`` trade tape (``stream=trades``; the
  ``binanceusdm_hist`` aggTrades archive is a per-day fallback).
- FOLLOWER = ``delta_india`` L2 book (``stream=book``, the ladder the Delta
  native recorder banks since ~2026-07-08) plus its trade tape
  (``stream=trades``) for queue-aware fills.

HONEST CAVEAT, stated up front: cross-venue timestamp alignment carries
~recorder-jitter uncertainty (two independent WS clients, two clocks). Every
lag number here is a RESEARCH ESTIMATE, not an execution guarantee. And the
maker-first fills are QUEUE-AWARE against RECORDED L2 depth — flagged
``ASSUMED_QUEUE_FILL``; they require live L2 validation before any maker
number can support candidate status.

Research-only, same hard guards as every discovery layer: can_trade=false,
can_promote=false; a CANDIDATE verdict is a hypothesis for pre-registered
untouched judgment and human approval, never a signal.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from vnedge.scalping.depth import OrderBookL2, load_l2_books
from vnedge.scalping.parameter_registry import DEFAULT_SCALPER_PARAMETER_REGISTRY
from vnedge.scalping.replay_backtester import _load_stream_frame

logger = logging.getLogger(__name__)

LEADLAG_ECHO_SCALP_ID = "leadlag_echo_scalp_v1"
LEADLAG_ECHO_SCALP_LATEST = "leadlag_echo_scalp.json"
FAMILY = "cross_venue_leadlag_echo_scalp"

# Trade-tape fallbacks (venue own tape first, then archive) — mirrors the
# cascade module so a day with only the backfill archive still resolves.
TRADE_TAPE_FALLBACKS: dict[str, tuple[str, ...]] = {
    "binanceusdm": ("binanceusdm_hist",),
}


@dataclass(frozen=True)
class LeadLagPair:
    """A leader/follower venue+symbol mapping. The follower is where we would
    rest the maker order (Delta); the leader is where the impulse originates."""

    base: str
    leader_exchange: str
    leader_symbol: str
    follower_exchange: str
    follower_symbol: str

    @property
    def label(self) -> str:
        return (f"{self.base}:{self.leader_exchange}/{self.leader_symbol}"
                f"->{self.follower_exchange}/{self.follower_symbol}")


# BTC/ETH: Binance USDT-margined perp leads; Delta India USD contract follows.
DEFAULT_PAIRS: tuple[LeadLagPair, ...] = (
    LeadLagPair("BTC", "binanceusdm", "BTC/USDT:USDT", "delta_india", "BTC/USD:USD"),
    LeadLagPair("ETH", "binanceusdm", "ETH/USDT:USDT", "delta_india", "ETH/USD:USD"),
)


def leadlag_echo_scalp_policy() -> dict:
    return {
        "status": "research_only",
        "can_trade": False,
        "can_promote": False,
        "requires_untouched_judgment": True,
        "requires_human_approval": True,
        "replay_id": LEADLAG_ECHO_SCALP_ID,
        "family": FAMILY,
        "data_source": (
            "leader trade tape (binanceusdm) + follower L2 book & trade tape "
            "(delta_india), recorded, public"
        ),
        "principle": (
            "deep books lead; Delta echoes with a lag. A maker-first resting "
            "order on the follower captures the DIRECTIONAL echo. maker_first "
            "fills are queue-aware against RECORDED L2 (ASSUMED_QUEUE_FILL) and "
            "are not evidence until live L2 validation confirms them; "
            "cross-venue timestamp alignment is a research estimate, not an "
            "execution guarantee"
        ),
    }


# --- Parameters --------------------------------------------------------------------

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class EchoScalpParams:
    """Every knob of the impulse detector, lag estimator and echo replay,
    env-tunable via from_env()."""

    # -- leader impulse detection (causal, trailing window) --
    impulse_window_ms: int = 2_000          # trailing window for the leader move
    impulse_threshold_bps: float = 8.0      # signed move to call an impulse
    impulse_cooldown_ms: int = 3_000        # suppress re-fires after an impulse
    # -- cross-venue lag estimation --
    response_threshold_bps: float = 3.0     # follower move that counts as a response
    max_lag_ms: int = 5_000                 # search horizon for the echo response
    # -- echo scalp execution (on the follower book) --
    maker_ttl_ms: int = 2_000               # resting maker order lifetime
    target_bps: float = 6.0                 # echo continuation target
    stop_bps: float = 6.0                   # adverse stop
    hold_ms: int = 10_000                   # hard timeout after fill
    notional_usd: float = 100.0             # clip size walked through the book
    min_events_for_candidate: int = 20

    def __post_init__(self) -> None:
        if self.impulse_window_ms <= 0 or self.impulse_cooldown_ms < 0:
            raise ValueError("impulse window must be positive, cooldown non-negative")
        if self.impulse_threshold_bps <= 0 or self.response_threshold_bps <= 0:
            raise ValueError("impulse/response thresholds must be positive")
        if self.max_lag_ms <= 0 or self.maker_ttl_ms <= 0 or self.hold_ms <= 0:
            raise ValueError("lag/ttl/hold windows must be positive")
        if self.target_bps <= 0 or self.stop_bps <= 0:
            raise ValueError("target_bps and stop_bps must be positive")
        if self.notional_usd <= 0:
            raise ValueError("notional_usd must be positive")
        if self.min_events_for_candidate < 1:
            raise ValueError("min_events_for_candidate must be >= 1")

    @classmethod
    def from_env(cls) -> "EchoScalpParams":
        d = cls()
        return cls(
            impulse_window_ms=_env_int("ECHO_IMPULSE_WINDOW_MS", d.impulse_window_ms),
            impulse_threshold_bps=_env_float(
                "ECHO_IMPULSE_THRESHOLD_BPS", d.impulse_threshold_bps),
            impulse_cooldown_ms=_env_int(
                "ECHO_IMPULSE_COOLDOWN_MS", d.impulse_cooldown_ms),
            response_threshold_bps=_env_float(
                "ECHO_RESPONSE_THRESHOLD_BPS", d.response_threshold_bps),
            max_lag_ms=_env_int("ECHO_MAX_LAG_MS", d.max_lag_ms),
            maker_ttl_ms=_env_int("ECHO_MAKER_TTL_MS", d.maker_ttl_ms),
            target_bps=_env_float("ECHO_TARGET_BPS", d.target_bps),
            stop_bps=_env_float("ECHO_STOP_BPS", d.stop_bps),
            hold_ms=_env_int("ECHO_HOLD_MS", d.hold_ms),
            notional_usd=_env_float("ECHO_NOTIONAL_USD", d.notional_usd),
            min_events_for_candidate=_env_int(
                "ECHO_MIN_EVENTS_FOR_CANDIDATE", d.min_events_for_candidate),
        )


# --- Inputs ------------------------------------------------------------------------

@dataclass(frozen=True)
class LeaderTrade:
    ts_ms: int
    price: float
    amount: float


@dataclass(frozen=True)
class FollowerTrade:
    ts_ms: int
    price: float
    amount: float
    taker_side: str      # "buy" | "sell"; anything else = unattributable


# --- Causal leader-impulse detection -----------------------------------------------

@dataclass(frozen=True)
class Impulse:
    ts_ms: int
    direction: str       # "buy" (up) | "sell" (down) — the echo we anticipate
    move_bps: float      # signed move over the trailing window
    ref_price: float     # leader price at the window start (past)
    leader_price: float  # leader price at the impulse


class LeaderImpulseDetector:
    """Causal impulse detector over the leader trade tape.

    At each leader trade the signed move is measured against the OLDEST price
    still inside the trailing ``impulse_window_ms`` window — strictly past data.
    The current price is appended only AFTER the decision, so a trade never
    serves as its own reference and no future print ever leaks in. A cooldown
    (from the last fire, also strictly past) stops one sustained move from
    firing on every tick. Decisions are therefore prefix-stable: truncating the
    tape after any point cannot change an earlier decision.
    """

    def __init__(self, params: EchoScalpParams) -> None:
        self.params = params
        self._window: deque[tuple[int, float]] = deque()  # (ts_ms, price)
        self._last_impulse_ms: int | None = None

    def on_trade(self, tr: LeaderTrade) -> Impulse | None:
        cutoff = tr.ts_ms - self.params.impulse_window_ms
        while self._window and self._window[0][0] < cutoff:
            self._window.popleft()
        fired: Impulse | None = None
        if self._window:
            ref_ts, ref_price = self._window[0]
            move_bps = (tr.price - ref_price) / ref_price * 10_000.0
            if abs(move_bps) >= self.params.impulse_threshold_bps:
                cooled = (self._last_impulse_ms is None
                          or tr.ts_ms - self._last_impulse_ms
                          >= self.params.impulse_cooldown_ms)
                if cooled:
                    fired = Impulse(
                        ts_ms=tr.ts_ms,
                        direction="buy" if move_bps > 0 else "sell",
                        move_bps=move_bps,
                        ref_price=ref_price,
                        leader_price=tr.price,
                    )
                    self._last_impulse_ms = tr.ts_ms
        # the current print joins the window only AFTER the decision
        self._window.append((tr.ts_ms, tr.price))
        return fired


# --- Cross-venue lag estimation ----------------------------------------------------

def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    vals = sorted(values)
    pos = (len(vals) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(vals) - 1)
    frac = pos - lo
    return vals[lo] * (1 - frac) + vals[hi] * frac


@dataclass(frozen=True)
class LeadLagStats:
    """Distribution of the leader-impulse -> follower-response lag (ms)."""

    impulses: int
    responded: int
    response_rate_pct: float
    median_lag_ms: float | None
    p25_lag_ms: float | None
    p75_lag_ms: float | None
    mean_lag_ms: float | None
    caveat: str = (
        "cross-venue ts alignment ~recorder-jitter; research estimate only"
    )

    def to_dict(self) -> dict:
        return asdict(self)


def estimate_leadlag(
    impulses: list[Impulse],
    follower_books: list[tuple[int, OrderBookL2]],
    params: EchoScalpParams,
) -> LeadLagStats:
    """For each (causally-detected) leader impulse, measure the lag until the
    follower mid first moves ``response_threshold_bps`` in the impulse
    direction, searching only FORWARD and only within ``max_lag_ms``. The
    follower reference is the last book at or before the impulse (past). No
    future data decides whether an impulse exists — that was already fixed
    causally by the detector; this only measures the forward response."""
    lags: list[float] = []
    responded = 0
    if not follower_books:
        return LeadLagStats(len(impulses), 0, 0.0, None, None, None, None)
    ts_list = [ts for ts, _ in follower_books]
    for imp in impulses:
        base_idx = _last_le(ts_list, imp.ts_ms)
        if base_idx is None:
            continue  # no follower book recorded yet at the impulse
        base_mid = follower_books[base_idx][1].mid_price
        deadline = imp.ts_ms + params.max_lag_ms
        for j in range(base_idx + 1, len(follower_books)):
            ts_j, book_j = follower_books[j]
            if ts_j > deadline:
                break
            if ts_j <= imp.ts_ms:
                continue
            move = (book_j.mid_price - base_mid) / base_mid * 10_000.0
            signed = move if imp.direction == "buy" else -move
            if signed >= params.response_threshold_bps:
                lags.append(float(ts_j - imp.ts_ms))
                responded += 1
                break
    n = len(impulses)
    return LeadLagStats(
        impulses=n,
        responded=responded,
        response_rate_pct=(responded / n * 100.0) if n else 0.0,
        median_lag_ms=_quantile(lags, 0.5),
        p25_lag_ms=_quantile(lags, 0.25),
        p75_lag_ms=_quantile(lags, 0.75),
        mean_lag_ms=(sum(lags) / len(lags)) if lags else None,
    )


def _last_le(sorted_ts: list[int], value: int) -> int | None:
    """Index of the last element <= value in a sorted list, or None."""
    lo, hi = 0, len(sorted_ts)
    while lo < hi:
        mid = (lo + hi) // 2
        if sorted_ts[mid] <= value:
            lo = mid + 1
        else:
            hi = mid
    return lo - 1 if lo > 0 else None


# --- Cost models -------------------------------------------------------------------

@dataclass(frozen=True)
class EchoCostModel:
    """Fees for one execution assumption. Slippage is NOT modelled here: the
    replay walks the recorded book (``FillWalk``) for taker legs, so the walked
    VWAP already carries liquidity-aware slippage; the maker entry rests at the
    touch (its whole point) and pays no slippage. This object applies fees to
    the price path the replay produced."""

    label: str
    entry_fee_bps: float
    exit_fee_bps: float
    assumed_queue_fill: bool = False

    @property
    def round_trip_fee_bps(self) -> float:
        return self.entry_fee_bps + self.exit_fee_bps

    def net_bps(self, direction: str, entry_price: float, exit_price: float) -> float:
        if direction not in ("buy", "sell"):
            raise ValueError(f"invalid direction: {direction}")
        if direction == "buy":
            gross = (exit_price - entry_price) / entry_price * 10_000.0
        else:
            gross = (entry_price - exit_price) / entry_price * 10_000.0
        return gross - self.entry_fee_bps - self.exit_fee_bps

    def to_dict(self) -> dict:
        d = asdict(self)
        d["round_trip_fee_bps"] = self.round_trip_fee_bps
        if self.assumed_queue_fill:
            d["caveat"] = ("ASSUMED_QUEUE_FILL — the maker entry is a queue-aware "
                           "fill against RECORDED L2 depth; requires live L2 "
                           "validation before candidate status")
        return d


def cost_models_for(exchange: str) -> dict[str, EchoCostModel]:
    """Both cost models from the follower venue's registry fee profile."""
    fee = DEFAULT_SCALPER_PARAMETER_REGISTRY.fee_profile(exchange.removesuffix("_hist"))
    return {
        "taker_taker": EchoCostModel(
            label="taker_taker",
            entry_fee_bps=fee.taker_bps, exit_fee_bps=fee.taker_bps,
        ),
        "maker_first": EchoCostModel(
            label="maker_first",
            entry_fee_bps=fee.maker_bps, exit_fee_bps=fee.taker_bps,
            assumed_queue_fill=True,
        ),
    }


# --- Echo scalp replay -------------------------------------------------------------

@dataclass(frozen=True)
class EchoEventRow:
    base: str
    leader_exchange: str
    follower_exchange: str
    follower_symbol: str
    day: str
    impulse_ts_ms: int
    direction: str
    impulse_move_bps: float
    follower_mid_at_impulse: float
    # taker leg (immediate cross — the strict floor, always fills)
    taker_entry_ts_ms: int
    taker_entry_price: float
    taker_exit_ts_ms: int
    taker_exit_price: float
    taker_exit_reason: str          # "target" | "stop" | "timeout" | "end"
    taker_net_bps: float
    taker_entry_walk_slippage_bps: float
    taker_fully_filled: bool
    # maker leg (queue-aware resting fill — ASSUMED_QUEUE_FILL)
    maker_filled: bool
    maker_fill_lag_ms: int | None    # queue-clear lag from the impulse
    maker_entry_ts_ms: int | None
    maker_entry_price: float | None
    maker_exit_ts_ms: int | None
    maker_exit_price: float | None
    maker_exit_reason: str | None
    maker_net_bps: float | None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class _Leg:
    direction: str
    entry_ts_ms: int
    entry_price: float
    stop_price: float
    target_price: float
    state: str                       # "open" | "closed"
    exit_ts_ms: int = 0
    exit_price: float = 0.0
    exit_reason: str = ""


@dataclass
class _MakerLeg:
    direction: str
    limit_price: float
    placed_ts_ms: int
    queue_ahead: float
    queue_consumed: float = 0.0
    state: str = "resting"           # "resting" | "open" | "closed" | "missed"
    fill_ts_ms: int | None = None
    stop_price: float = 0.0
    target_price: float = 0.0
    exit_ts_ms: int = 0
    exit_price: float = 0.0
    exit_reason: str = ""


@dataclass
class _OpenScalp:
    impulse: Impulse
    follower_mid_at_impulse: float
    taker: _Leg
    taker_entry_walk_slippage_bps: float
    taker_fully_filled: bool
    maker: _MakerLeg

    def done(self) -> bool:
        return (self.taker.state == "closed"
                and self.maker.state in ("closed", "missed"))


@dataclass
class EchoDayResult:
    rows: list[EchoEventRow] = field(default_factory=list)
    impulses_detected: int = 0
    scalps_opened: int = 0
    overlapping_impulses: int = 0
    skipped_no_follower_book: int = 0
    maker_missed: int = 0
    unresolved_at_end: int = 0

    def counters(self) -> dict:
        return {
            "impulses_detected": self.impulses_detected,
            "scalps_opened": self.scalps_opened,
            "overlapping_impulses": self.overlapping_impulses,
            "skipped_no_follower_book": self.skipped_no_follower_book,
            "maker_missed": self.maker_missed,
            "unresolved_at_end": self.unresolved_at_end,
        }


class EchoScalpReplayer:
    """Deterministic single-day, single-position replay of the echo scalp.

    One merged, time-ordered pass over three streams (follower book, follower
    trades, leader trades — that tie-break order at equal ts keeps the current
    follower book fresh before a fill decision). Per leader impulse, when flat,
    it opens ONE scalp with two legs resolved side by side on the follower:

    - taker leg: crosses the follower spread immediately (``FillWalk`` on the
      book) — the strict, always-fills floor.
    - maker leg: rests at the follower touch; fills QUEUE-AWARE only once
      follower trades clear the size displayed ahead of it at that level (reuse
      of the ``TickReplayBacktester`` FIFO model). Unfilled by ``maker_ttl_ms``
      => missed.

    Both legs exit taker (walk the book) at target / stop / timeout, stop wins
    stop-vs-target ties (repo-wide rule). New impulses while a scalp is open are
    counted as overlapping and skipped, never queued.
    """

    def __init__(self, params: EchoScalpParams,
                 models: dict[str, EchoCostModel]) -> None:
        self.params = params
        self.models = models

    def run(self, leader_trades: list[LeaderTrade],
            follower_books: list[tuple[int, OrderBookL2]],
            follower_trades: list[FollowerTrade],
            *, base: str, leader_exchange: str, follower_exchange: str,
            follower_symbol: str, day: str) -> EchoDayResult:
        result = EchoDayResult()
        detector = LeaderImpulseDetector(self.params)
        book: OrderBookL2 | None = None
        scalp: _OpenScalp | None = None

        # 0 = follower book, 1 = follower trade, 2 = leader trade
        merged: list[tuple[int, int, object]] = []
        for ts, bk in follower_books:
            merged.append((int(ts), 0, bk))
        for ft in follower_trades:
            merged.append((ft.ts_ms, 1, ft))
        for lt in leader_trades:
            merged.append((lt.ts_ms, 2, lt))
        merged.sort(key=lambda e: (e[0], e[1]))

        for ts_ms, kind, obj in merged:
            if kind == 0:
                book = obj
                if scalp is not None:
                    self._on_book(scalp, book, ts_ms)
                    if scalp.done():
                        self._emit(scalp, result, base=base,
                                   leader_exchange=leader_exchange,
                                   follower_exchange=follower_exchange,
                                   follower_symbol=follower_symbol, day=day)
                        scalp = None
            elif kind == 1:
                ft = obj
                assert isinstance(ft, FollowerTrade)
                if scalp is not None and book is not None:
                    self._on_follower_trade(scalp, ft, book)
                    if scalp.done():
                        self._emit(scalp, result, base=base,
                                   leader_exchange=leader_exchange,
                                   follower_exchange=follower_exchange,
                                   follower_symbol=follower_symbol, day=day)
                        scalp = None
            else:
                lt = obj
                assert isinstance(lt, LeaderTrade)
                impulse = detector.on_trade(lt)
                if impulse is None:
                    continue
                result.impulses_detected += 1
                if scalp is not None:
                    result.overlapping_impulses += 1
                    continue
                if book is None:
                    result.skipped_no_follower_book += 1
                    continue
                scalp = self._open(impulse, book)
                result.scalps_opened += 1

        if scalp is not None:
            result.unresolved_at_end += 1
            if book is not None:
                self._force_close(scalp, book)
            # _emit is the single place maker_missed is counted (see below)
            self._emit(scalp, result, base=base, leader_exchange=leader_exchange,
                       follower_exchange=follower_exchange,
                       follower_symbol=follower_symbol, day=day)
        return result

    # -- open / resolve --

    def _open(self, impulse: Impulse, book: OrderBookL2) -> _OpenScalp:
        direction = impulse.direction  # "buy" echo (up) / "sell" echo (down)
        # taker leg: cross the spread now, walked through the recorded book
        walk = book.fill_walk(self.params.notional_usd, direction)
        taker_entry = walk.avg_price
        taker = _Leg(
            direction=direction,
            entry_ts_ms=impulse.ts_ms,
            entry_price=taker_entry,
            stop_price=self._stop(direction, taker_entry),
            target_price=self._target(direction, taker_entry),
            state="open",
        )
        # maker leg: rest at the follower touch; queue = displayed touch size
        if direction == "buy":
            limit = book.best_bid
            queue_ahead = book.bids[0][1]
        else:
            limit = book.best_ask
            queue_ahead = book.asks[0][1]
        maker = _MakerLeg(
            direction=direction,
            limit_price=limit,
            placed_ts_ms=impulse.ts_ms,
            queue_ahead=queue_ahead,
        )
        return _OpenScalp(
            impulse=impulse,
            follower_mid_at_impulse=book.mid_price,
            taker=taker,
            taker_entry_walk_slippage_bps=walk.slippage_bps,
            taker_fully_filled=walk.fully_filled,
            maker=maker,
        )

    def _on_book(self, scalp: _OpenScalp, book: OrderBookL2, ts_ms: int) -> None:
        # maker TTL expiry (only while still resting)
        m = scalp.maker
        if m.state == "resting" and ts_ms - m.placed_ts_ms >= self.params.maker_ttl_ms:
            m.state = "missed"
        # exits for any open leg (taker out on the follower book)
        if scalp.taker.state == "open":
            self._resolve_leg_exit(scalp.taker, book, ts_ms)
        if m.state == "open":
            self._resolve_maker_exit(m, book, ts_ms)

    def _on_follower_trade(self, scalp: _OpenScalp, ft: FollowerTrade,
                           book: OrderBookL2) -> None:
        m = scalp.maker
        if m.state != "resting":
            return
        if ft.ts_ms - m.placed_ts_ms >= self.params.maker_ttl_ms:
            m.state = "missed"
            return
        # FIFO queue: same-side taker volume at OR through our limit clears the
        # size resting ahead of us; we fill only once that queue is exhausted.
        if m.direction == "buy":
            qualifies = ft.taker_side == "sell" and ft.price <= m.limit_price
        else:
            qualifies = ft.taker_side == "buy" and ft.price >= m.limit_price
        if not qualifies:
            return
        m.queue_consumed += ft.amount
        if m.queue_consumed >= m.queue_ahead:
            m.state = "open"
            m.fill_ts_ms = ft.ts_ms
            m.stop_price = self._stop(m.direction, m.limit_price)
            m.target_price = self._target(m.direction, m.limit_price)

    def _resolve_leg_exit(self, leg: _Leg, book: OrderBookL2, ts_ms: int) -> None:
        reason = self._exit_reason(leg.direction, leg.entry_ts_ms,
                                   leg.stop_price, leg.target_price, book, ts_ms)
        if reason is not None:
            leg.state = "closed"
            leg.exit_ts_ms = ts_ms
            leg.exit_price = self._exit_price(leg.direction, book)
            leg.exit_reason = reason

    def _resolve_maker_exit(self, m: _MakerLeg, book: OrderBookL2, ts_ms: int) -> None:
        assert m.fill_ts_ms is not None
        reason = self._exit_reason(m.direction, m.fill_ts_ms,
                                   m.stop_price, m.target_price, book, ts_ms)
        if reason is not None:
            m.state = "closed"
            m.exit_ts_ms = ts_ms
            m.exit_price = self._exit_price(m.direction, book)
            m.exit_reason = reason

    def _exit_reason(self, direction: str, entry_ts_ms: int, stop: float,
                     target: float, book: OrderBookL2, ts_ms: int) -> str | None:
        # exit is a taker out at the tradable touch; stop checked FIRST so it
        # wins stop-vs-target ties (repo-wide rule).
        exitable = book.best_bid if direction == "buy" else book.best_ask
        if direction == "buy":
            if exitable <= stop:
                return "stop"
            if exitable >= target:
                return "target"
        else:
            if exitable >= stop:
                return "stop"
            if exitable <= target:
                return "target"
        if ts_ms - entry_ts_ms >= self.params.hold_ms:
            return "timeout"
        return None

    def _exit_price(self, direction: str, book: OrderBookL2) -> float:
        # walk the book to get out (taker): a long sells into bids, a short
        # buys from asks.
        exit_side = "sell" if direction == "buy" else "buy"
        return book.fill_walk(self.params.notional_usd, exit_side).avg_price

    def _force_close(self, scalp: _OpenScalp, book: OrderBookL2) -> None:
        ts_ms = int(book.event_time.timestamp() * 1000)
        if scalp.taker.state == "open":
            scalp.taker.state = "closed"
            scalp.taker.exit_ts_ms = ts_ms
            scalp.taker.exit_price = self._exit_price(scalp.taker.direction, book)
            scalp.taker.exit_reason = "end"
        m = scalp.maker
        if m.state == "open":
            m.state = "closed"
            m.exit_ts_ms = ts_ms
            m.exit_price = self._exit_price(m.direction, book)
            m.exit_reason = "end"
        elif m.state == "resting":
            m.state = "missed"

    def _stop(self, direction: str, entry: float) -> float:
        if direction == "buy":
            return entry * (1 - self.params.stop_bps / 10_000.0)
        return entry * (1 + self.params.stop_bps / 10_000.0)

    def _target(self, direction: str, entry: float) -> float:
        if direction == "buy":
            return entry * (1 + self.params.target_bps / 10_000.0)
        return entry * (1 - self.params.target_bps / 10_000.0)

    def _emit(self, scalp: _OpenScalp, result: EchoDayResult, *, base: str,
              leader_exchange: str, follower_exchange: str,
              follower_symbol: str, day: str) -> None:
        t = scalp.taker
        m = scalp.maker
        if m.state == "missed":
            result.maker_missed += 1
        maker_filled = m.state == "closed"
        maker_net = (
            self.models["maker_first"].net_bps(m.direction, m.limit_price, m.exit_price)
            if maker_filled else None
        )
        result.rows.append(EchoEventRow(
            base=base,
            leader_exchange=leader_exchange,
            follower_exchange=follower_exchange,
            follower_symbol=follower_symbol,
            day=day,
            impulse_ts_ms=scalp.impulse.ts_ms,
            direction=scalp.impulse.direction,
            impulse_move_bps=scalp.impulse.move_bps,
            follower_mid_at_impulse=scalp.follower_mid_at_impulse,
            taker_entry_ts_ms=t.entry_ts_ms,
            taker_entry_price=t.entry_price,
            taker_exit_ts_ms=t.exit_ts_ms,
            taker_exit_price=t.exit_price,
            taker_exit_reason=t.exit_reason,
            taker_net_bps=self.models["taker_taker"].net_bps(
                t.direction, t.entry_price, t.exit_price),
            taker_entry_walk_slippage_bps=scalp.taker_entry_walk_slippage_bps,
            taker_fully_filled=scalp.taker_fully_filled,
            maker_filled=maker_filled,
            maker_fill_lag_ms=(m.fill_ts_ms - m.placed_ts_ms
                               if m.fill_ts_ms is not None else None),
            maker_entry_ts_ms=m.fill_ts_ms,
            maker_entry_price=m.limit_price if maker_filled else None,
            maker_exit_ts_ms=m.exit_ts_ms if maker_filled else None,
            maker_exit_price=m.exit_price if maker_filled else None,
            maker_exit_reason=m.exit_reason if maker_filled else None,
            maker_net_bps=maker_net,
        ))


# --- Data loading ------------------------------------------------------------------

def _symbol_root(data_root: Path | str, exchange: str, symbol: str) -> Path:
    safe = symbol.split(":")[0].replace("/", "")
    return Path(data_root) / "ticks" / f"exchange={exchange}" / f"symbol={safe}"


def _stream_days(stream_root: Path) -> set[str]:
    if not stream_root.exists():
        return set()
    days = {p.stem for p in stream_root.glob("*.parquet")}
    days.update(p.name for p in stream_root.iterdir() if p.is_dir())
    return {d for d in days if len(d) == 8 and d.isdigit()}


def discover_overlap_days(data_root: Path | str, pair: LeadLagPair) -> tuple[str, ...]:
    """Days that have BOTH a leader trade tape (own venue or a registered
    fallback) AND a follower book stream — the only days the scalp can run."""
    leader_days: set[str] = set()
    for source in (pair.leader_exchange,
                   *TRADE_TAPE_FALLBACKS.get(pair.leader_exchange, ())):
        leader_days |= _stream_days(
            _symbol_root(data_root, source, pair.leader_symbol) / "stream=trades")
    follower_days = _stream_days(
        _symbol_root(data_root, pair.follower_exchange, pair.follower_symbol)
        / "stream=book")
    return tuple(sorted(leader_days & follower_days))


def load_leader_trades(data_root: Path | str, pair: LeadLagPair,
                       day: str) -> tuple[list[LeaderTrade], str | None]:
    """Leader trade tape for the day: own venue first, then any registered
    fallback archive. Returns (prints, source_exchange)."""
    for source in (pair.leader_exchange,
                   *TRADE_TAPE_FALLBACKS.get(pair.leader_exchange, ())):
        frame = _load_stream_frame(
            _symbol_root(data_root, source, pair.leader_symbol) / "stream=trades", day)
        if frame is None or frame.empty:
            continue
        out: list[LeaderTrade] = []
        for r in frame.itertuples():
            try:
                tr = LeaderTrade(ts_ms=int(r.ts_ms), price=float(r.price),
                                 amount=float(r.amount))
            except (TypeError, ValueError, AttributeError):
                continue
            if tr.price <= 0 or tr.amount <= 0:
                continue
            out.append(tr)
        if out:
            out.sort(key=lambda t: t.ts_ms)
            return out, source
    return [], None


def load_follower_books(data_root: Path | str, pair: LeadLagPair,
                        day: str) -> list[tuple[int, OrderBookL2]]:
    """Follower L2 book snapshots for the day (reuses depth.load_l2_books; L1-only
    legacy rows without an L2 ladder are skipped)."""
    return load_l2_books(data_root, pair.follower_exchange, pair.follower_symbol, day)


def load_follower_trades(data_root: Path | str, pair: LeadLagPair,
                         day: str) -> list[FollowerTrade]:
    """Follower trade tape for the day (needed only for queue-aware maker
    fills). Rows without a usable taker side are dropped for queue purposes."""
    frame = _load_stream_frame(
        _symbol_root(data_root, pair.follower_exchange, pair.follower_symbol)
        / "stream=trades", day)
    if frame is None or frame.empty:
        return []
    out: list[FollowerTrade] = []
    for r in frame.itertuples():
        try:
            ft = FollowerTrade(ts_ms=int(r.ts_ms), price=float(r.price),
                               amount=float(r.amount), taker_side=str(r.side))
        except (TypeError, ValueError, AttributeError):
            continue
        if ft.price <= 0 or ft.amount <= 0:
            continue
        out.append(ft)
    out.sort(key=lambda t: t.ts_ms)
    return out


# --- Aggregation / verdicts --------------------------------------------------------

def _model_aggregate(nets_bps: list[float], notional_usd: float) -> dict:
    wins = [v for v in nets_bps if v > 0]
    losses = [-v for v in nets_bps if v < 0]
    pf = (sum(wins) / sum(losses)) if wins and losses else (999.0 if wins else None)
    return {
        "events": len(nets_bps),
        "net_usd": sum(v / 10_000.0 * notional_usd for v in nets_bps),
        "avg_net_bps": sum(nets_bps) / len(nets_bps) if nets_bps else None,
        "win_rate_pct": len(wins) / len(nets_bps) * 100.0 if nets_bps else 0.0,
        "profit_factor": pf,
    }


def echo_verdict(events: int, taker_net_usd: float, maker_net_usd: float,
                 min_events: int) -> str:
    if events < min_events:
        return "UNDER_SAMPLED"
    if taker_net_usd > 0:
        return "CANDIDATE"
    if maker_net_usd > 0:
        return "MAKER_ONLY_POSITIVE"
    return "NEGATIVE_EDGE"


_VERDICT_RANK = {
    "CANDIDATE": 0,
    "MAKER_ONLY_POSITIVE": 1,
    "UNDER_SAMPLED": 2,
    "NEGATIVE_EDGE": 3,
}


# --- Scanner -----------------------------------------------------------------------

def run_leadlag_echo_scalp(
    data_root: Path | str,
    *,
    pairs: tuple[LeadLagPair, ...] = DEFAULT_PAIRS,
    days: tuple[str, ...] = (),
    params: EchoScalpParams | None = None,
    max_rows_per_target: int = 200,
) -> dict:
    """Scan every configured pair over its recorded leader/follower overlap
    days; absent streams/days degrade to counted skips, never crashes."""
    params = params or EchoScalpParams()
    targets: list[dict] = []
    for pair in pairs:
        models = cost_models_for(pair.follower_exchange)
        replayer = EchoScalpReplayer(params, models)
        available = discover_overlap_days(data_root, pair)
        target_days = tuple(d for d in days if d in available) if days else available
        rows: list[EchoEventRow] = []
        counters: dict[str, int] = {}
        days_scanned: list[str] = []
        days_missing_follower_trades: list[str] = []
        lag_impulses = 0
        lag_responded = 0
        for day in target_days:
            leader_trades, source = load_leader_trades(data_root, pair, day)
            follower_books = load_follower_books(data_root, pair, day)
            if not leader_trades or not follower_books:
                continue
            follower_trades = load_follower_trades(data_root, pair, day)
            if not follower_trades:
                days_missing_follower_trades.append(day)
            # lag estimate (causal impulses vs follower book response)
            detector = LeaderImpulseDetector(params)
            impulses = [imp for tr in leader_trades
                        if (imp := detector.on_trade(tr)) is not None]
            stats = estimate_leadlag(impulses, follower_books, params)
            lag_impulses += stats.impulses
            lag_responded += stats.responded
            day_result = replayer.run(
                leader_trades, follower_books, follower_trades,
                base=pair.base, leader_exchange=pair.leader_exchange,
                follower_exchange=pair.follower_exchange,
                follower_symbol=pair.follower_symbol, day=day)
            rows.extend(day_result.rows)
            for key, val in day_result.counters().items():
                counters[key] = counters.get(key, 0) + val
            days_scanned.append(day)
            logger.info(
                "echo scalp %s %s: %d leader trades, %d impulses, %d scalps, "
                "%d maker fills (leader from %s, %d follower books, %d follower "
                "trades)",
                pair.label, day, len(leader_trades), day_result.impulses_detected,
                day_result.scalps_opened,
                sum(1 for r in day_result.rows if r.maker_filled), source,
                len(follower_books), len(follower_trades),
            )
        taker = _model_aggregate([r.taker_net_bps for r in rows], params.notional_usd)
        maker = _model_aggregate(
            [r.maker_net_bps for r in rows if r.maker_net_bps is not None],
            params.notional_usd)
        verdict = echo_verdict(len(rows), taker["net_usd"], maker["net_usd"],
                               params.min_events_for_candidate)
        targets.append({
            "base": pair.base,
            "leader_exchange": pair.leader_exchange,
            "leader_symbol": pair.leader_symbol,
            "follower_exchange": pair.follower_exchange,
            "follower_symbol": pair.follower_symbol,
            "overlap_days": list(available),
            "days_scanned": days_scanned,
            "days_missing_follower_trades": days_missing_follower_trades,
            "counters": counters,
            "events": len(rows),
            "maker_fills": sum(1 for r in rows if r.maker_filled),
            "lag_estimate": {
                "impulses": lag_impulses,
                "responded": lag_responded,
                "response_rate_pct": (lag_responded / lag_impulses * 100.0)
                if lag_impulses else 0.0,
                "caveat": ("cross-venue ts alignment ~recorder-jitter; "
                           "research estimate only"),
            },
            "rows": [r.to_dict() for r in rows[-max_rows_per_target:]],
            "aggregates": {"taker_taker": taker, "maker_first": maker},
            "verdict": verdict,
            "can_trade": False,
            "can_promote": False,
        })
    targets.sort(key=lambda t: (_VERDICT_RANK.get(t["verdict"], 9),
                                -(t["aggregates"]["taker_taker"]["net_usd"]),
                                t["base"]))
    verdict_counts: dict[str, int] = {}
    for t in targets:
        verdict_counts[t["verdict"]] = verdict_counts.get(t["verdict"], 0) + 1
    ref_exchange = pairs[0].follower_exchange if pairs else "delta_india"
    ref_models = cost_models_for(ref_exchange)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "policy": leadlag_echo_scalp_policy(),
        "params": asdict(params),
        "cost_models": {k: m.to_dict() for k, m in ref_models.items()},
        "notional_usd": params.notional_usd,
        "pairs": [p.label for p in pairs],
        "targets": targets,
        "summary": {
            "targets": len(targets),
            "events": sum(t["events"] for t in targets),
            "maker_fills": sum(t["maker_fills"] for t in targets),
            "verdict_counts": verdict_counts,
            "can_trade": False,
            "can_promote": False,
        },
        "can_trade": False,
        "can_promote": False,
    }


def write_leadlag_echo_scalp_payload(payload: dict, out_dir: Path | str) -> Path:
    """Atomic publish for the continuous_research folding hook (tmp+replace)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / LEADLAG_ECHO_SCALP_LATEST
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(path)
    return path


# --- CLI ---------------------------------------------------------------------------

def render_report(payload: dict) -> str:
    lines = [
        "cross-venue lead-lag echo scalp (leader trades -> follower L2 book)",
        "policy=research_only can_trade=false can_promote=false",
        "maker_first fills are queue-aware vs RECORDED L2 (ASSUMED_QUEUE_FILL);",
        "cross-venue ts alignment is a research estimate, not an execution guarantee",
        "",
        "verdict             events  mkfill  taker$  maker$  lag(resp%)  pair",
    ]
    for t in payload["targets"]:
        agg = t["aggregates"]
        lag = t["lag_estimate"]
        lines.append(
            f"{t['verdict']:<19} {t['events']:>6} {t['maker_fills']:>6} "
            f"{agg['taker_taker']['net_usd']:>7.2f} "
            f"{agg['maker_first']['net_usd']:>7.2f} "
            f"{lag['response_rate_pct']:>9.1f}%  "
            f"{t['base']}:{t['leader_exchange']}->{t['follower_exchange']}"
        )
    if not payload["targets"]:
        lines.append("no targets — no leader/follower overlap days recorded yet "
                     "(Delta L2 book recording started ~2026-07-08)")
    return "\n".join(lines)


def _split_csv(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _select_pairs(bases: tuple[str, ...]) -> tuple[LeadLagPair, ...]:
    if not bases:
        return DEFAULT_PAIRS
    wanted = {b.upper() for b in bases}
    return tuple(p for p in DEFAULT_PAIRS if p.base in wanted)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(
        description="cross-venue lead-lag echo scalp replay (research only)")
    p.add_argument("--data-root", default="data")
    p.add_argument("--pairs", default="",
                   help="comma-separated bases (BTC,ETH); default: all")
    p.add_argument("--days", help="comma-separated UTC days YYYYMMDD; "
                                  "default: every recorded overlap day")
    p.add_argument("--out", default="research/live_research",
                   help="directory for leadlag_echo_scalp.json")
    p.add_argument("--no-publish", action="store_true",
                   help="print only; do not write the folding-hook file")
    p.add_argument("--json", action="store_true")
    p.add_argument("--interval-seconds", type=float, default=0.0,
                   help="rescan cadence; <= 0 runs once and exits")
    args = p.parse_args(argv)

    params = EchoScalpParams.from_env()
    pairs = _select_pairs(_split_csv(args.pairs))
    while True:
        payload = run_leadlag_echo_scalp(
            args.data_root, pairs=pairs, days=_split_csv(args.days), params=params)
        if not args.no_publish:
            path = write_leadlag_echo_scalp_payload(payload, args.out)
            logger.info("published %s", path)
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(render_report(payload))
        if args.interval_seconds <= 0:
            return 0
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
