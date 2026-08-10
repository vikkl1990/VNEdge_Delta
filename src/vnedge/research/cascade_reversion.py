"""Liquidation-cascade reversion research family — research only.

    python -m vnedge.research.cascade_reversion --exchanges binanceusdm,bybit

HYPOTHESIS: a one-sided liquidation cascade is FORCED, non-informational
flow. Once the cascade exhausts (no meaningful forced prints for a quiet
window), the pressure that moved price is gone and price mean-reverts toward
the pre-cascade reference (5-minute pre-cascade trade VWAP) at minute scale.

Structural inputs (both already recorded by zero-risk collectors):
- liquidation stream: ticks/exchange=<ex>/symbol=<sym>/stream=liquidations
  (rows ts_ms/price/amount/side/notional_usd; side is the FORCED ORDER side —
  "sell" means a long was liquidated; see vnedge.exchange.liquidation_recorder)
- trade tape: stream=trades, live recorder and/or the binanceusdm_hist
  aggTrades backfill archive.

Causality is structural: the burst threshold at any event uses ONLY rolling
sums recorded for strictly earlier events (trailing 24h percentile), the
detector consumes the merged liquidation+trade tape in time order, and every
entry/exit fills on a trade print at-or-after the decision.

TWO cost models are reported side by side for every event:
- taker_taker: taker fee + slippage on BOTH legs (no fill assumptions)
- maker_first: maker entry at the entry print's price with NO slippage +
  taker exit. This ASSUMES the passive order filled at that price — flagged
  ``assumed_maker_fill=True`` — and requires L2 queue replay before any
  maker-first number can support candidate status.

Research-only, same hard guards as every discovery layer: can_trade=false,
can_promote=false; a CANDIDATE verdict is a hypothesis for pre-registered
untouched judgment (burn registry) and human approval, never a signal.
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

from vnedge.scalping.parameter_registry import DEFAULT_SCALPER_PARAMETER_REGISTRY
from vnedge.scalping.replay_backtester import _load_stream_frame

logger = logging.getLogger(__name__)

CASCADE_REVERSION_ID = "cascade_reversion_v1"
CASCADE_REVERSION_LATEST = "cascade_reversion.json"
FAMILY = "liquidation_cascade_reversion"

DEFAULT_EXCHANGES = ("binanceusdm", "bybit")
DEFAULT_SYMBOLS = (
    "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT",
    "BNB/USDT:USDT", "XRP/USDT:USDT", "DOGE/USDT:USDT",
)
# Exchanges whose liquidation stream can be evaluated against another
# exchange's trade tape when the venue's own tape is absent for a day.
TRADE_TAPE_FALLBACKS: dict[str, tuple[str, ...]] = {
    "binanceusdm": ("binanceusdm_hist",),
}

_FLIP_ENTRY = {"sell": "buy", "buy": "sell"}  # trade AGAINST the cascade


def cascade_reversion_policy() -> dict:
    return {
        "status": "research_only",
        "can_trade": False,
        "can_promote": False,
        "requires_untouched_judgment": True,
        "requires_human_approval": True,
        "replay_id": CASCADE_REVERSION_ID,
        "family": FAMILY,
        "data_source": "liquidation stream + trade tape (recorded, public)",
        "principle": (
            "forced liquidation flow is non-informational; after exhaustion "
            "price mean-reverts toward the pre-cascade VWAP. maker_first "
            "numbers ASSUME passive fills and are not evidence until L2 "
            "queue replay confirms them"
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
class CascadeParams:
    """Every knob of the detector/evaluator, env-tunable via from_env()."""

    burst_window_ms: int = 60_000          # rolling liquidation-notional window
    trailing_window_ms: int = 86_400_000   # 24h percentile lookback
    threshold_pct: float = 0.95            # percentile of trailing rolling sums
    min_history_events: int = 50           # warmup before detection may fire
    min_burst_notional_usd: float = 1_000.0  # absolute floor under the percentile
    one_sided_min: float = 0.80            # dominant side share of burst notional
    exhaustion_peak_frac: float = 0.25     # liq > frac*peak keeps the cascade alive
    exhaustion_quiet_ms: int = 20_000      # quiet time that confirms exhaustion
    pre_vwap_window_ms: int = 300_000      # pre-cascade reference VWAP window
    stop_buffer_frac: float = 0.10         # stop beyond extreme by frac of extreme->VWAP
    timeout_ms: int = 900_000              # 15 min hard exit
    min_events_for_candidate: int = 20

    def __post_init__(self) -> None:
        if self.burst_window_ms <= 0 or self.trailing_window_ms <= self.burst_window_ms:
            raise ValueError("trailing window must exceed a positive burst window")
        if not 0.0 < self.threshold_pct < 1.0:
            raise ValueError("threshold_pct must be in (0, 1)")
        if not 0.5 <= self.one_sided_min <= 1.0:
            raise ValueError("one_sided_min must be in [0.5, 1.0]")
        if not 0.0 < self.exhaustion_peak_frac < 1.0:
            raise ValueError("exhaustion_peak_frac must be in (0, 1)")
        if (self.exhaustion_quiet_ms <= 0 or self.pre_vwap_window_ms <= 0
                or self.timeout_ms <= 0):
            raise ValueError("quiet, pre-VWAP, and timeout windows must be positive")
        if self.stop_buffer_frac < 0:
            raise ValueError("stop_buffer_frac cannot be negative")
        if self.min_history_events < 2 or self.min_events_for_candidate < 1:
            raise ValueError("history/candidate minimums must be sane")

    @classmethod
    def from_env(cls) -> "CascadeParams":
        d = cls()
        return cls(
            burst_window_ms=_env_int("CASCADE_BURST_WINDOW_MS", d.burst_window_ms),
            trailing_window_ms=_env_int("CASCADE_TRAILING_WINDOW_MS", d.trailing_window_ms),
            threshold_pct=_env_float("CASCADE_THRESHOLD_PCT", d.threshold_pct),
            min_history_events=_env_int("CASCADE_MIN_HISTORY_EVENTS", d.min_history_events),
            min_burst_notional_usd=_env_float(
                "CASCADE_MIN_BURST_NOTIONAL_USD", d.min_burst_notional_usd),
            one_sided_min=_env_float("CASCADE_ONE_SIDED_MIN", d.one_sided_min),
            exhaustion_peak_frac=_env_float(
                "CASCADE_EXHAUSTION_PEAK_FRAC", d.exhaustion_peak_frac),
            exhaustion_quiet_ms=_env_int(
                "CASCADE_EXHAUSTION_QUIET_MS", d.exhaustion_quiet_ms),
            pre_vwap_window_ms=_env_int("CASCADE_PRE_VWAP_WINDOW_MS", d.pre_vwap_window_ms),
            stop_buffer_frac=_env_float("CASCADE_STOP_BUFFER_FRAC", d.stop_buffer_frac),
            timeout_ms=_env_int("CASCADE_TIMEOUT_MS", d.timeout_ms),
            min_events_for_candidate=_env_int(
                "CASCADE_MIN_EVENTS_FOR_CANDIDATE", d.min_events_for_candidate),
        )


# --- Inputs ------------------------------------------------------------------------

@dataclass(frozen=True)
class LiquidationEvent:
    ts_ms: int
    price: float
    amount: float
    side: str            # FORCED ORDER side: "sell" = long liquidated; "" = unknown
    notional_usd: float


@dataclass(frozen=True)
class TradePrint:
    ts_ms: int
    price: float
    amount: float


# --- Causal cascade detection ------------------------------------------------------

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
class CascadeStart:
    side: str            # dominant forced-order side of the burst
    start_ms: int        # ts of the oldest event in the firing burst window
    detected_ms: int
    burst_notional_usd: float
    one_sided_frac: float
    threshold_usd: float
    peak_notional_usd: float   # largest single liquidation in the firing window
    extreme_price: float       # most adverse liq price in the firing window


class CascadeDetector:
    """Causal burst detector over the liquidation stream.

    For each liquidation event, the threshold (percentile of the trailing 24h
    rolling burst-window sums) is computed BEFORE the event is folded into any
    structure — a burst can never raise the bar it is judged against, and no
    future event ever contributes. The detector fires when the rolling sum
    clears max(percentile threshold, absolute floor) AND the dominant side
    holds >= one_sided_min of the window's notional. Warmup: at least
    min_history_events past rolling sums must exist before any fire.
    """

    def __init__(self, params: CascadeParams) -> None:
        self.params = params
        self._window: deque[LiquidationEvent] = deque()
        self._window_notional = 0.0
        self._history: deque[tuple[int, float]] = deque()  # (ts_ms, rolling_sum)

    def _threshold(self, now_ms: int) -> float | None:
        """Percentile of strictly-past rolling sums, or None while warming up."""
        cutoff = now_ms - self.params.trailing_window_ms
        while self._history and self._history[0][0] < cutoff:
            self._history.popleft()
        if len(self._history) < self.params.min_history_events:
            return None
        return _quantile([v for _ts, v in self._history], self.params.threshold_pct)

    def on_liquidation(self, ev: LiquidationEvent) -> CascadeStart | None:
        threshold = self._threshold(ev.ts_ms)   # strictly past — before folding ev in
        cutoff = ev.ts_ms - self.params.burst_window_ms
        while self._window and self._window[0].ts_ms <= cutoff:
            self._window_notional -= self._window.popleft().notional_usd
        self._window.append(ev)
        self._window_notional += ev.notional_usd
        rolling = self._window_notional

        fired: CascadeStart | None = None
        if threshold is not None and rolling >= max(
                threshold, self.params.min_burst_notional_usd):
            by_side = {"buy": 0.0, "sell": 0.0}
            total = 0.0
            for w in self._window:
                total += w.notional_usd
                if w.side in by_side:
                    by_side[w.side] += w.notional_usd
            dominant = max(by_side, key=lambda s: by_side[s])
            frac = by_side[dominant] / total if total > 0 else 0.0
            if frac >= self.params.one_sided_min:
                prices = [w.price for w in self._window]
                fired = CascadeStart(
                    side=dominant,
                    start_ms=self._window[0].ts_ms,
                    detected_ms=ev.ts_ms,
                    burst_notional_usd=rolling,
                    one_sided_frac=frac,
                    threshold_usd=threshold,
                    peak_notional_usd=max(w.notional_usd for w in self._window),
                    extreme_price=min(prices) if dominant == "sell" else max(prices),
                )
        # the event's rolling sum joins the history only AFTER the decision
        self._history.append((ev.ts_ms, rolling))
        return fired


# --- Cost models -------------------------------------------------------------------

@dataclass(frozen=True)
class CascadeCostModel:
    """Entry/exit fees + slippage for one execution assumption."""

    label: str
    entry_fee_bps: float
    exit_fee_bps: float
    entry_slippage_bps: float
    exit_slippage_bps: float
    assumed_maker_fill: bool = False

    @property
    def round_trip_cost_bps(self) -> float:
        return (self.entry_fee_bps + self.exit_fee_bps
                + self.entry_slippage_bps + self.exit_slippage_bps)

    def net_bps(self, direction: str, raw_entry: float, raw_exit: float) -> float:
        """Slippage is ALWAYS adverse on the leg it applies to; fees on both."""
        if direction not in ("buy", "sell"):
            raise ValueError(f"invalid direction: {direction}")
        e_slip = self.entry_slippage_bps / 10_000.0
        x_slip = self.exit_slippage_bps / 10_000.0
        if direction == "buy":
            entry = raw_entry * (1 + e_slip)   # pay up to buy
            exit_ = raw_exit * (1 - x_slip)    # hit down to sell
            gross = (exit_ - entry) / entry * 10_000.0
        else:
            entry = raw_entry * (1 - e_slip)   # hit down to sell
            exit_ = raw_exit * (1 + x_slip)    # pay up to buy back
            gross = (entry - exit_) / entry * 10_000.0
        return gross - self.entry_fee_bps - self.exit_fee_bps

    def to_dict(self) -> dict:
        d = asdict(self)
        d["round_trip_cost_bps"] = self.round_trip_cost_bps
        if self.assumed_maker_fill:
            d["caveat"] = ("ASSUMED_MAKER_FILL — passive entry fill at the print "
                           "price is an assumption; requires L2 queue replay "
                           "before candidate status")
        return d


def cost_models_for(exchange: str) -> dict[str, CascadeCostModel]:
    """Both cost models from the registry fee profile for the venue."""
    fee = DEFAULT_SCALPER_PARAMETER_REGISTRY.fee_profile(exchange.removesuffix("_hist"))
    return {
        "taker_taker": CascadeCostModel(
            label="taker_taker",
            entry_fee_bps=fee.taker_bps, exit_fee_bps=fee.taker_bps,
            entry_slippage_bps=fee.slippage_bps, exit_slippage_bps=fee.slippage_bps,
        ),
        "maker_first": CascadeCostModel(
            label="maker_first",
            entry_fee_bps=fee.maker_bps, exit_fee_bps=fee.taker_bps,
            entry_slippage_bps=0.0, exit_slippage_bps=fee.slippage_bps,
            assumed_maker_fill=True,
        ),
    }


# --- Reversion evaluation ----------------------------------------------------------

@dataclass(frozen=True)
class CascadeEventRow:
    exchange: str
    symbol: str
    day: str
    cascade_side: str          # forced-order side of the burst
    direction: str             # entry side (against the cascade)
    cascade_start_ms: int
    detected_ms: int
    entry_ts_ms: int
    exit_ts_ms: int
    burst_notional_usd: float
    peak_liq_notional_usd: float
    one_sided_frac: float
    pre_vwap: float            # target (pre-cascade reference)
    extreme_price: float
    stop_price: float
    entry_price_raw: float
    exit_price_raw: float
    exit_reason: str           # "target" | "stop" | "timeout" | "end"
    taker_net_bps: float
    maker_first_net_bps: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class _ActiveCascade:
    start: CascadeStart
    peak_notional: float
    last_significant_ms: int
    extreme_price: float
    pre_vwap: float | None
    evaluable: bool = True   # False: tracked through exhaustion, never entered


@dataclass
class _OpenEvaluation:
    cascade: _ActiveCascade
    direction: str
    entry_ts_ms: int
    entry_price: float
    stop_price: float
    target_price: float


@dataclass
class CascadeDayResult:
    rows: list[CascadeEventRow] = field(default_factory=list)
    cascades_detected: int = 0
    entries: int = 0
    skipped_no_pre_vwap: int = 0
    skipped_already_reverted: int = 0
    skipped_entry_beyond_stop: int = 0
    overlapping_cascades: int = 0
    unresolved_at_end: int = 0

    def counters(self) -> dict:
        return {
            "cascades_detected": self.cascades_detected,
            "entries": self.entries,
            "skipped_no_pre_vwap": self.skipped_no_pre_vwap,
            "skipped_already_reverted": self.skipped_already_reverted,
            "skipped_entry_beyond_stop": self.skipped_entry_beyond_stop,
            "overlapping_cascades": self.overlapping_cascades,
            "unresolved_at_end": self.unresolved_at_end,
        }


class CascadeReversionReplayer:
    """Deterministic single-day replay of the cascade-reversion protocol.

    Consumes the merged liquidation+trade tape in time order (liquidations
    before trades at the same millisecond, so the quiet timer is conservative).
    Protocol per detected cascade:

    - exhaustion: the cascade stays alive while any liquidation prints
      > exhaustion_peak_frac * running peak; the FIRST TRADE at least
      exhaustion_quiet_ms after the last such print is the entry print.
      A big liquidation during the quiet window resumes the cascade — you
      never enter into a resuming cascade.
    - entry: that first post-exhaustion trade, AGAINST the cascade
      ("sell" cascade = longs forced out = price pushed down = we buy).
    - target: the 5-minute pre-cascade trade VWAP (window ends at the
      cascade's start, so cascade prints never contaminate the reference).
    - stop: the cascade extreme extended beyond by stop_buffer_frac of the
      extreme-to-VWAP distance. Stop wins stop-vs-target ties.
    - timeout: hard exit at the first print past timeout_ms.

    One evaluation at a time; cascades firing while one is open are counted
    as overlapping and skipped, never queued.
    """

    def __init__(self, params: CascadeParams, models: dict[str, CascadeCostModel]) -> None:
        self.params = params
        self.models = models

    def run(self, liquidations: list[LiquidationEvent], trades: list[TradePrint],
            *, exchange: str, symbol: str, day: str) -> CascadeDayResult:
        params = self.params
        result = CascadeDayResult()
        detector = CascadeDetector(params)
        vwap_buf: deque[TradePrint] = deque()
        active: _ActiveCascade | None = None
        open_eval: _OpenEvaluation | None = None
        last_trade: TradePrint | None = None

        merged: list[tuple[int, int, object]] = [
            (ev.ts_ms, 0, ev) for ev in liquidations
        ] + [(tr.ts_ms, 1, tr) for tr in trades]
        merged.sort(key=lambda e: (e[0], e[1]))

        for ts_ms, kind, obj in merged:
            if kind == 0:
                ev = obj
                assert isinstance(ev, LiquidationEvent)
                fired = detector.on_liquidation(ev)
                if active is not None:
                    # same cascade continuing — repeated fires are absorbed
                    self._feed_active(active, ev)
                    continue
                if fired is not None:
                    # a non-evaluable cascade is STILL tracked through
                    # exhaustion, so counters count cascades, not prints
                    evaluable = True
                    pre_vwap: float | None = None
                    if open_eval is not None:
                        result.overlapping_cascades += 1
                        evaluable = False
                    else:
                        result.cascades_detected += 1
                        pre_vwap = self._pre_vwap(vwap_buf, fired.start_ms)
                        if pre_vwap is None:
                            result.skipped_no_pre_vwap += 1
                            evaluable = False
                    active = _ActiveCascade(
                        start=fired,
                        peak_notional=fired.peak_notional_usd,
                        last_significant_ms=ev.ts_ms,
                        extreme_price=fired.extreme_price,
                        pre_vwap=pre_vwap,
                        evaluable=evaluable,
                    )
                continue

            trade = obj
            assert isinstance(trade, TradePrint)
            last_trade = trade
            entered_this_print = False
            if active is not None:
                quiet = ts_ms - active.last_significant_ms
                if quiet >= params.exhaustion_quiet_ms:
                    if active.evaluable and open_eval is None:
                        open_eval = self._enter(active, trade, result)
                        entered_this_print = open_eval is not None
                    active = None
                else:
                    if active.start.side == "sell":
                        active.extreme_price = min(active.extreme_price, trade.price)
                    else:
                        active.extreme_price = max(active.extreme_price, trade.price)
            if open_eval is not None and not entered_this_print:
                reason = self._exit_reason(open_eval, trade)
                if reason is not None:
                    self._close(open_eval, trade, reason,
                                exchange=exchange, symbol=symbol, day=day,
                                result=result)
                    open_eval = None
            self._push_vwap(vwap_buf, trade)

        if active is not None:
            result.unresolved_at_end += 1
        if open_eval is not None and last_trade is not None:
            self._close(open_eval, last_trade, "end",
                        exchange=exchange, symbol=symbol, day=day, result=result)
        return result

    # -- cascade lifecycle helpers --

    def _feed_active(self, active: _ActiveCascade, ev: LiquidationEvent) -> None:
        active.peak_notional = max(active.peak_notional, ev.notional_usd)
        if ev.notional_usd > self.params.exhaustion_peak_frac * active.peak_notional:
            active.last_significant_ms = ev.ts_ms
        if active.start.side == "sell":
            active.extreme_price = min(active.extreme_price, ev.price)
        else:
            active.extreme_price = max(active.extreme_price, ev.price)

    def _push_vwap(self, buf: deque[TradePrint], trade: TradePrint) -> None:
        # retain pre-VWAP window + burst window: detection happens at most one
        # burst window after the cascade start, and the reference window ends
        # at that start.
        keep_ms = self.params.pre_vwap_window_ms + self.params.burst_window_ms
        buf.append(trade)
        cutoff = trade.ts_ms - keep_ms
        while buf and buf[0].ts_ms < cutoff:
            buf.popleft()

    def _pre_vwap(self, buf: deque[TradePrint], start_ms: int) -> float | None:
        lo = start_ms - self.params.pre_vwap_window_ms
        notional = 0.0
        qty = 0.0
        for tr in buf:
            if lo <= tr.ts_ms < start_ms:
                notional += tr.price * tr.amount
                qty += tr.amount
        return notional / qty if qty > 0 else None

    def _enter(self, active: _ActiveCascade, trade: TradePrint,
               result: CascadeDayResult) -> _OpenEvaluation | None:
        direction = _FLIP_ENTRY[active.start.side]
        target = active.pre_vwap
        assert target is not None    # only evaluable cascades reach entry
        extreme = active.extreme_price
        buffer_ = self.params.stop_buffer_frac * abs(extreme - target)
        if direction == "buy":
            stop = extreme - buffer_
            if trade.price >= target:
                result.skipped_already_reverted += 1
                return None
            if trade.price <= stop:
                result.skipped_entry_beyond_stop += 1
                return None
        else:
            stop = extreme + buffer_
            if trade.price <= target:
                result.skipped_already_reverted += 1
                return None
            if trade.price >= stop:
                result.skipped_entry_beyond_stop += 1
                return None
        result.entries += 1
        return _OpenEvaluation(
            cascade=active,
            direction=direction,
            entry_ts_ms=trade.ts_ms,
            entry_price=trade.price,
            stop_price=stop,
            target_price=target,
        )

    def _exit_reason(self, pos: _OpenEvaluation, trade: TradePrint) -> str | None:
        # stop checked FIRST — stop wins stop-vs-target ties, repo-wide rule
        if pos.direction == "buy":
            if trade.price <= pos.stop_price:
                return "stop"
            if trade.price >= pos.target_price:
                return "target"
        else:
            if trade.price >= pos.stop_price:
                return "stop"
            if trade.price <= pos.target_price:
                return "target"
        if trade.ts_ms - pos.entry_ts_ms >= self.params.timeout_ms:
            return "timeout"
        return None

    def _close(self, pos: _OpenEvaluation, trade: TradePrint, reason: str, *,
               exchange: str, symbol: str, day: str, result: CascadeDayResult) -> None:
        active = pos.cascade
        result.rows.append(CascadeEventRow(
            exchange=exchange,
            symbol=symbol,
            day=day,
            cascade_side=active.start.side,
            direction=pos.direction,
            cascade_start_ms=active.start.start_ms,
            detected_ms=active.start.detected_ms,
            entry_ts_ms=pos.entry_ts_ms,
            exit_ts_ms=trade.ts_ms,
            burst_notional_usd=active.start.burst_notional_usd,
            peak_liq_notional_usd=active.peak_notional,
            one_sided_frac=active.start.one_sided_frac,
            pre_vwap=active.pre_vwap,
            extreme_price=active.extreme_price,
            stop_price=pos.stop_price,
            entry_price_raw=pos.entry_price,
            exit_price_raw=trade.price,
            exit_reason=reason,
            taker_net_bps=self.models["taker_taker"].net_bps(
                pos.direction, pos.entry_price, trade.price),
            maker_first_net_bps=self.models["maker_first"].net_bps(
                pos.direction, pos.entry_price, trade.price),
        ))


# --- Data loading ------------------------------------------------------------------

def _symbol_root(data_root: Path | str, exchange: str, symbol: str) -> Path:
    safe = symbol.split(":")[0].replace("/", "")
    return Path(data_root) / "ticks" / f"exchange={exchange}" / f"symbol={safe}"


def discover_liquidation_days(data_root: Path | str, exchange: str,
                              symbol: str) -> tuple[str, ...]:
    stream_root = _symbol_root(data_root, exchange, symbol) / "stream=liquidations"
    if not stream_root.exists():
        return ()
    days = {p.stem for p in stream_root.glob("*.parquet")}
    days.update(p.name for p in stream_root.iterdir() if p.is_dir())
    return tuple(sorted(d for d in days if len(d) == 8 and d.isdigit()))


def load_liquidation_events(data_root: Path | str, exchange: str, symbol: str,
                            day: str) -> list[LiquidationEvent]:
    frame = _load_stream_frame(
        _symbol_root(data_root, exchange, symbol) / "stream=liquidations", day)
    if frame is None or frame.empty:
        return []
    events: list[LiquidationEvent] = []
    for r in frame.itertuples():
        try:
            ev = LiquidationEvent(
                ts_ms=int(r.ts_ms), price=float(r.price), amount=float(r.amount),
                side=str(r.side), notional_usd=float(r.notional_usd),
            )
        except (TypeError, ValueError, AttributeError):
            continue
        # recorded lakes contain occasional zero-price/zero-size rows; a 0.0
        # print would poison the extreme/stop, so they are dropped like the
        # TradeTick validation drops them on the scalping path
        if ev.price <= 0 or ev.notional_usd <= 0:
            continue
        events.append(ev)
    events.sort(key=lambda e: e.ts_ms)
    return events


def load_trade_prints(data_root: Path | str, exchange: str, symbol: str,
                      day: str) -> tuple[list[TradePrint], str | None]:
    """Trade tape for the day: the venue's own tape first, then any registered
    fallback archive (binanceusdm -> binanceusdm_hist). Returns (prints,
    source_exchange); ([], None) when no tape exists for the day."""
    for source in (exchange, *TRADE_TAPE_FALLBACKS.get(exchange, ())):
        frame = _load_stream_frame(
            _symbol_root(data_root, source, symbol) / "stream=trades", day)
        if frame is None or frame.empty:
            continue
        prints: list[TradePrint] = []
        for r in frame.itertuples():
            try:
                tr = TradePrint(
                    ts_ms=int(r.ts_ms), price=float(r.price), amount=float(r.amount))
            except (TypeError, ValueError, AttributeError):
                continue
            # zero-price prints exist in the recorded tape; against a 0.0
            # print any short "hits target" for ~10000 bps — drop them, same
            # rule as TradeTick's validation on the scalping path
            if tr.price <= 0 or tr.amount <= 0:
                continue
            prints.append(tr)
        if prints:
            prints.sort(key=lambda t: t.ts_ms)
            return prints, source
    return [], None


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


def cascade_verdict(events: int, taker_net_usd: float, maker_net_usd: float,
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

def run_cascade_reversion(
    data_root: Path | str,
    *,
    exchanges: tuple[str, ...] = DEFAULT_EXCHANGES,
    symbols: tuple[str, ...] = DEFAULT_SYMBOLS,
    days: tuple[str, ...] = (),
    params: CascadeParams | None = None,
    notional_usd: float = 100.0,
    max_rows_per_target: int = 200,
) -> dict:
    """Scan every exchange x symbol with recorded liquidation days; absent
    streams/days degrade to counted skips, never crashes."""
    params = params or CascadeParams()
    targets: list[dict] = []
    for exchange in exchanges:
        models = cost_models_for(exchange)
        replayer = CascadeReversionReplayer(params, models)
        for symbol in symbols:
            available = discover_liquidation_days(data_root, exchange, symbol)
            target_days = tuple(d for d in days if d in available) if days else available
            rows: list[CascadeEventRow] = []
            counters: dict[str, int] = {}
            days_scanned: list[str] = []
            days_missing_trades: list[str] = []
            for day in target_days:
                liqs = load_liquidation_events(data_root, exchange, symbol, day)
                if not liqs:
                    continue
                trades, source = load_trade_prints(data_root, exchange, symbol, day)
                if not trades:
                    days_missing_trades.append(day)
                    continue
                day_result = replayer.run(liqs, trades, exchange=exchange,
                                          symbol=symbol, day=day)
                rows.extend(day_result.rows)
                for key, val in day_result.counters().items():
                    counters[key] = counters.get(key, 0) + val
                days_scanned.append(day)
                logger.info(
                    "cascade scan %s %s %s: %d liqs, %d cascades, %d entries "
                    "(trades from %s)",
                    exchange, symbol, day, len(liqs),
                    day_result.cascades_detected, day_result.entries, source,
                )
            taker = _model_aggregate([r.taker_net_bps for r in rows], notional_usd)
            maker = _model_aggregate([r.maker_first_net_bps for r in rows], notional_usd)
            verdict = cascade_verdict(len(rows), taker["net_usd"], maker["net_usd"],
                                      params.min_events_for_candidate)
            targets.append({
                "exchange": exchange,
                "symbol": symbol,
                "days_with_liquidations": list(available),
                "days_scanned": days_scanned,
                "days_missing_trades": days_missing_trades,
                "counters": counters,
                "events": len(rows),
                "rows": [r.to_dict() for r in rows[-max_rows_per_target:]],
                "aggregates": {"taker_taker": taker, "maker_first": maker},
                "verdict": verdict,
                "can_trade": False,
                "can_promote": False,
            })
    targets.sort(key=lambda t: (_VERDICT_RANK.get(t["verdict"], 9),
                                -(t["aggregates"]["taker_taker"]["net_usd"]),
                                t["exchange"], t["symbol"]))
    verdict_counts: dict[str, int] = {}
    for t in targets:
        verdict_counts[t["verdict"]] = verdict_counts.get(t["verdict"], 0) + 1
    ref_models = cost_models_for(exchanges[0] if exchanges else "binanceusdm")
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "policy": cascade_reversion_policy(),
        "params": asdict(params),
        "cost_models": {k: m.to_dict() for k, m in ref_models.items()},
        "notional_usd": notional_usd,
        "exchanges": list(exchanges),
        "symbols": list(symbols),
        "targets": targets,
        "summary": {
            "targets": len(targets),
            "events": sum(t["events"] for t in targets),
            "verdict_counts": verdict_counts,
            "can_trade": False,
            "can_promote": False,
        },
        "can_trade": False,
        "can_promote": False,
    }


def write_cascade_reversion_payload(payload: dict, out_dir: Path | str) -> Path:
    """Atomic publish for the continuous_research folding hook (same
    tmp+replace discipline as event_taker_replay.json)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / CASCADE_REVERSION_LATEST
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(path)
    return path


# --- CLI ---------------------------------------------------------------------------

def render_report(payload: dict) -> str:
    lines = [
        "cascade reversion replay (liquidation stream + trade tape)",
        "policy=research_only can_trade=false can_promote=false",
        "maker_first numbers ASSUME passive fills (L2 queue replay required)",
        "",
        "verdict             events  taker$  maker$  exchange     symbol"
        "          days(scanned/missing_trades)",
    ]
    for t in payload["targets"]:
        agg = t["aggregates"]
        lines.append(
            f"{t['verdict']:<19} {t['events']:>6} "
            f"{agg['taker_taker']['net_usd']:>7.2f} "
            f"{agg['maker_first']['net_usd']:>7.2f} "
            f"{t['exchange']:<12} {t['symbol']:<15} "
            f"{len(t['days_scanned'])}/{len(t['days_missing_trades'])}"
        )
    if not payload["targets"]:
        lines.append("no targets — no liquidation stream recorded yet "
                     "(run vnedge.exchange.liquidation_recorder)")
    return "\n".join(lines)


def _split_csv(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(
        description="liquidation-cascade reversion replay (research only)")
    p.add_argument("--data-root", default="data")
    p.add_argument("--exchanges", default=",".join(DEFAULT_EXCHANGES))
    p.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    p.add_argument("--days", help="comma-separated UTC days YYYYMMDD; "
                                  "default: every recorded liquidation day")
    p.add_argument("--out", default="research/live_research",
                   help="directory for cascade_reversion.json")
    p.add_argument("--no-publish", action="store_true",
                   help="print only; do not write the folding-hook file")
    p.add_argument("--json", action="store_true")
    p.add_argument("--interval-seconds", type=float, default=0.0,
                   help="rescan cadence; <= 0 runs once and exits")
    args = p.parse_args(argv)

    params = CascadeParams.from_env()
    while True:
        payload = run_cascade_reversion(
            args.data_root,
            exchanges=_split_csv(args.exchanges) or DEFAULT_EXCHANGES,
            symbols=_split_csv(args.symbols) or DEFAULT_SYMBOLS,
            days=_split_csv(args.days),
            params=params,
        )
        if not args.no_publish:
            path = write_cascade_reversion_payload(payload, args.out)
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
