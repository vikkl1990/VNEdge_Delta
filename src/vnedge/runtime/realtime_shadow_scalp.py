"""Real-time shadow scalp runner — live-tick firing of the scalp detectors.

    python -m vnedge.runtime.realtime_shadow_scalp

The ``cascade_reversion`` and ``leadlag_echo_scalp`` research families detect
100-350x more events than the sparse 1h lanes ever fire, but they only run as
periodic BATCH replay over recorded tapes. This runner rebuilds the missing
piece: it drives the SAME detectors against the LIVE tick stream, firing a
shadow intent the instant a setup forms and resolving it into virtual PnL as
subsequent live ticks arrive. The point is evidence VELOCITY — a fast live
track record that converts an ``UNDER_SAMPLED`` lane into a verdict in days,
with the maker-vs-taker cost split surfaced live.

STRICTLY SHADOW. There is no order path, no gateway call, no credentials, no
private streams. ``can_trade`` / ``can_promote`` are False on every payload.
Real-time firing changes how fast evidence accumulates; it does NOT change the
gates. Promotion still requires the replay families' pre-registered judgment on
untouched data plus human approval (docs/REALTIME_SHADOW_SCALP.md).

Detector reuse (the whole point of "one implementation"): the cascade burst
detector (``CascadeDetector``), the leader-impulse detector
(``LeaderImpulseDetector``), the dual cost models, and — crucially — the batch
replayers' resolution helpers (``CascadeReversionReplayer`` /
``EchoScalpReplayer`` private methods) are imported and driven tick-by-tick.
Live firing and batch replay therefore share ONE implementation; a regression
test asserts the live lane and the batch replayer emit identical rows on the
same tape.

Two families, each keyed to the streams the recorders already carry:

- ``cascade``: Binance USDM liquidation stream (forced-order side) + the venue
  trade tape -> ``CascadeShadowLane``. A one-sided liquidation cascade that
  exhausts fires a mean-reversion entry against the cascade.
- ``leadlag_echo``: Binance USDM trade tape (leader) + Delta India native L2
  book & trades (follower) -> ``EchoShadowLane``. A leader impulse anticipates
  a directional echo on the follower; the maker leg rests queue-aware.

Journals live under ``logs/scalp_shadow/<family>_<venue>_<symbol>.journal.jsonl``
(``scalp_shadow_intent`` / ``scalp_shadow_outcome`` records) and survive
restarts: already-resolved intents are skipped and open scalps are rebuilt and
resolved forward against subsequent ticks, never double-counted. The aggregate
is published to ``research/live_research/realtime_shadow_scalp.json`` and folded
into the continuous-research document the dashboard reads.

MAKER-FILL CAVEAT: the maker legs ASSUME a passive fill (cascade: a resting
entry at the reversion print; echo: a queue-aware fill at the follower touch).
Those numbers are hypotheses flagged ``assumed_maker_fill`` /
``assumed_queue_fill`` — they are not evidence until L2 queue replay confirms
them. The taker numbers are the honest, always-fills floor.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

from vnedge.execution.journal import DecisionJournal
from vnedge.research.cascade_reversion import (
    CascadeDayResult,
    CascadeDetector,
    CascadeParams,
    CascadeReversionReplayer,
    CascadeStart,
    LiquidationEvent,
    TradePrint,
    _ActiveCascade,
    _model_aggregate,
    _OpenEvaluation,
    cascade_verdict,
)
from vnedge.research.cascade_reversion import (
    cost_models_for as cascade_cost_models,
)
from vnedge.research.leadlag_echo_scalp import (
    DEFAULT_PAIRS,
    EchoDayResult,
    EchoScalpParams,
    EchoScalpReplayer,
    FollowerTrade,
    Impulse,
    LeaderImpulseDetector,
    LeaderTrade,
    LeadLagPair,
    _Leg,
    _MakerLeg,
    _OpenScalp,
)
from vnedge.research.leadlag_echo_scalp import (
    cost_models_for as echo_cost_models,
)
from vnedge.scalping.depth import OrderBookL2

logger = logging.getLogger(__name__)

RUNNER_ID = "realtime_shadow_scalp_v1"
REALTIME_SHADOW_SCALP_LATEST = "realtime_shadow_scalp.json"
DEFAULT_JOURNAL_DIR = Path("logs/scalp_shadow")
DEFAULT_OUT_DIR = Path("research/live_research")

FAMILY_CASCADE = "cascade"
FAMILY_LEADLAG = "leadlag_echo"
CASCADE_STRATEGY_ID = "cascade_reversion_v1"
LEADLAG_STRATEGY_ID = "leadlag_echo_scalp_v1"

CASCADE_MAKER_CAVEAT = (
    "ASSUMED_MAKER_FILL — the maker_first edge assumes a passive entry fill at "
    "the reversion print; it is a hypothesis until L2 queue replay confirms it"
)
ECHO_MAKER_CAVEAT = (
    "ASSUMED_QUEUE_FILL — the maker leg is a queue-aware fill against the live "
    "follower touch; it is a hypothesis until live L2 validation confirms it"
)

DEFAULT_NOTIONAL_USD = 100.0


def realtime_shadow_scalp_policy() -> dict:
    """Hard-wired governance posture — identical intent to the batch families."""
    return {
        "status": "realtime_shadow_only",
        "can_trade": False,
        "can_promote": False,
        "requires_untouched_judgment": True,
        "requires_human_approval": True,
        "requires_replay_before_promotion": True,
        "runner_id": RUNNER_ID,
        "families": [FAMILY_CASCADE, FAMILY_LEADLAG],
        "principle": (
            "real-time firing accelerates EVIDENCE VELOCITY, not the gates. A "
            "live UNDER_SAMPLED -> CANDIDATE transition is still only a "
            "hypothesis; promotion requires the replay family's pre-registered "
            "judgment on untouched data plus human approval. maker legs assume "
            "passive fills and are not evidence until L2 queue replay confirms "
            "them; taker numbers are the always-fills floor"
        ),
    }


# --- helpers -----------------------------------------------------------------------

def _symbol_safe(symbol: str) -> str:
    return symbol.split(":")[0].replace("/", "").replace("-", "")


def _day_from_ms(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).strftime("%Y%m%d")


def _iso_from_ms(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).isoformat()


def _signed_gross_bps(direction: str, entry: float, target: float) -> float:
    """Signed gross move from entry to target in bps (before costs)."""
    if entry <= 0:
        return 0.0
    if direction == "buy":
        return (target - entry) / entry * 10_000.0
    return (entry - target) / entry * 10_000.0


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _split_csv(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


# --- shared lane accounting --------------------------------------------------------

class _ScalpLaneBase:
    """Journaling, restart resume bookkeeping and aggregation shared by both
    families. Subclasses own the tick routing and the open-scalp rebuild."""

    family: str = ""
    strategy_id: str = ""
    maker_caveat: str = ""

    def __init__(self, *, venue: str, symbol: str, journal: DecisionJournal,
                 min_events: int, notional_usd: float) -> None:
        self.venue = venue
        self.symbol = symbol
        self.journal = journal
        self.min_events = min_events
        self.notional_usd = notional_usd
        self._resolved: set[str] = set()
        self._intents = 0
        self._taker_bps: list[float] = []
        self._maker_bps: list[float] = []
        self._first_intent_ms: int | None = None
        self._last_intent_ms: int | None = None
        self._last_event_ms: int | None = None

    # -- accounting --
    def _touch(self, ts_ms: int) -> None:
        if self._last_event_ms is None or ts_ms > self._last_event_ms:
            self._last_event_ms = ts_ms

    def _note_intent(self, event_ms: int) -> None:
        self._intents += 1
        if self._first_intent_ms is None or event_ms < self._first_intent_ms:
            self._first_intent_ms = event_ms
        if self._last_intent_ms is None or event_ms > self._last_intent_ms:
            self._last_intent_ms = event_ms

    def _record_outcome(self, taker_bps: float, maker_bps: float | None) -> None:
        self._taker_bps.append(taker_bps)
        if maker_bps is not None:
            self._maker_bps.append(maker_bps)

    def append_intent(self, key: str, event_ms: int, payload: dict) -> None:
        self._note_intent(event_ms)
        self.journal.append("scalp_shadow_intent", payload)

    def append_outcome(self, key: str, payload: dict, *, taker_bps: float,
                       maker_bps: float | None) -> None:
        self._resolved.add(key)
        self._record_outcome(taker_bps, maker_bps)
        self.journal.append("scalp_shadow_outcome", payload)

    # -- restart replay (subclass rebuilds its own open scalp) --
    def _load(self) -> None:
        intents: dict[str, dict] = {}
        for record in self.journal.read_all():
            kind = record.get("kind")
            payload = record.get("payload") or {}
            if kind == "scalp_shadow_intent":
                key = payload.get("intent_key")
                if key and key not in intents:
                    intents[key] = payload
                    event_ms = _resume_event_ms(payload)
                    if event_ms is not None:
                        self._note_intent(event_ms)
            elif kind == "scalp_shadow_outcome":
                key = payload.get("intent_key")
                if key is None or key in self._resolved:
                    continue
                self._resolved.add(key)
                taker = _as_float(payload.get("taker_net_bps"))
                maker = payload.get("maker_net_bps")
                self._record_outcome(
                    taker, _as_float(maker) if maker is not None else None
                )
        for key, payload in intents.items():
            if key in self._resolved:
                continue
            self._rebuild_open(key, payload)

    def _rebuild_open(self, key: str, payload: dict) -> None:  # pragma: no cover
        raise NotImplementedError

    # -- reporting --
    def summary(self) -> dict:
        taker = _model_aggregate(self._taker_bps, self.notional_usd)
        maker = _model_aggregate(self._maker_bps, self.notional_usd)
        span_ms = 0
        if self._first_intent_ms is not None and self._last_intent_ms is not None:
            span_ms = max(self._last_intent_ms - self._first_intent_ms, 0)
        span_hours = span_ms / 3_600_000.0
        events_per_hour = (
            round(self._intents / span_hours, 3) if span_hours > 0 else None
        )
        verdict = cascade_verdict(
            len(self._taker_bps), taker["net_usd"], maker["net_usd"],
            self.min_events,
        )
        maker_beats_taker = maker["net_usd"] > taker["net_usd"]
        return {
            "family": self.family,
            "strategy_id": self.strategy_id,
            "exchange": self.venue,
            "symbol": self.symbol,
            "intents": self._intents,
            "virtual_trades": len(self._taker_bps),
            "maker_fills": len(self._maker_bps),
            "open_scalps": self.open_count(),
            "events_per_hour": events_per_hour,
            # last-fire timestamps for the dashboard "Real-Time Shadow Scalp"
            # panel (epoch ms; None until the lane fires). Observability only.
            "last_intent_ms": self._last_intent_ms,
            "last_event_ms": self._last_event_ms,
            "aggregates": {"taker_taker": taker, "maker_first": maker},
            "maker_beats_taker": maker_beats_taker,
            "assumed_maker_fill_caveat": self.maker_caveat,
            "verdict": verdict,
            "can_trade": False,
            "can_promote": False,
        }

    def open_count(self) -> int:  # pragma: no cover - overridden
        return 0


# --- cascade lane ------------------------------------------------------------------

class CascadeShadowLane(_ScalpLaneBase):
    """Live cascade-reversion firing. Drives ``CascadeDetector`` on the
    liquidation stream and reuses ``CascadeReversionReplayer``'s resolution
    helpers on the trade tape — one open evaluation at a time, exactly as the
    batch replayer, so results are identical on an identical tape."""

    family = FAMILY_CASCADE
    strategy_id = CASCADE_STRATEGY_ID
    maker_caveat = CASCADE_MAKER_CAVEAT

    def __init__(self, *, venue: str, symbol: str, journal: DecisionJournal,
                 params: CascadeParams | None = None,
                 notional_usd: float = DEFAULT_NOTIONAL_USD) -> None:
        self.params = params or CascadeParams()
        self.models = cascade_cost_models(venue)
        super().__init__(venue=venue, symbol=symbol, journal=journal,
                         min_events=self.params.min_events_for_candidate,
                         notional_usd=notional_usd)
        self._detector = CascadeDetector(self.params)
        self._replayer = CascadeReversionReplayer(self.params, self.models)
        self._result = CascadeDayResult()  # scratch accumulator for reused helpers
        from collections import deque

        self._vwap: deque[TradePrint] = deque()
        self._active: _ActiveCascade | None = None
        self._open: _OpenEvaluation | None = None
        self._open_key: str | None = None
        self._load()

    def open_count(self) -> int:
        return 1 if self._open is not None else 0

    def _cascade_key(self, active: _ActiveCascade) -> str:
        return (f"{self.strategy_id}|{self.venue}|{_symbol_safe(self.symbol)}"
                f"|{active.start.start_ms}")

    def on_liquidation(self, ev: LiquidationEvent) -> None:
        """One liquidation print — mirrors the batch merged-loop liq branch."""
        self._touch(ev.ts_ms)
        fired = self._detector.on_liquidation(ev)
        if self._active is not None:
            self._replayer._feed_active(self._active, ev)
            return
        if fired is None:
            return
        evaluable = True
        pre_vwap: float | None = None
        if self._open is not None:
            evaluable = False  # overlapping cascade — suppressed, never queued
        else:
            pre_vwap = self._replayer._pre_vwap(self._vwap, fired.start_ms)
            if pre_vwap is None:
                evaluable = False
        self._active = _ActiveCascade(
            start=fired,
            peak_notional=fired.peak_notional_usd,
            last_significant_ms=ev.ts_ms,
            extreme_price=fired.extreme_price,
            pre_vwap=pre_vwap,
            evaluable=evaluable,
        )

    def on_trade(self, tr: TradePrint) -> None:
        """One trade print — mirrors the batch merged-loop trade branch."""
        self._touch(tr.ts_ms)
        entered = False
        if self._active is not None:
            quiet = tr.ts_ms - self._active.last_significant_ms
            if quiet >= self.params.exhaustion_quiet_ms:
                if self._active.evaluable and self._open is None:
                    entered = self._try_enter(self._active, tr)
                self._active = None
            elif self._active.start.side == "sell":
                self._active.extreme_price = min(self._active.extreme_price, tr.price)
            else:
                self._active.extreme_price = max(self._active.extreme_price, tr.price)
        if self._open is not None and not entered:
            reason = self._replayer._exit_reason(self._open, tr)
            if reason is not None:
                self._close(tr, reason)
        self._replayer._push_vwap(self._vwap, tr)

    def _try_enter(self, active: _ActiveCascade, tr: TradePrint) -> bool:
        key = self._cascade_key(active)
        if key in self._resolved or key == self._open_key:
            return False
        open_eval = self._replayer._enter(active, tr, self._result)
        if open_eval is None:
            return False
        self._open = open_eval
        self._open_key = key
        self._journal_intent(key, open_eval, active)
        return True

    def _journal_intent(self, key: str, pos: _OpenEvaluation,
                        active: _ActiveCascade) -> None:
        direction = pos.direction
        gross = _signed_gross_bps(direction, pos.entry_price, pos.target_price)
        taker = self.models["taker_taker"]
        maker = self.models["maker_first"]
        side = "long" if direction == "buy" else "short"
        payload = {
            "intent_key": key,
            "family": self.family,
            "approved": False,
            "approval_state": "REALTIME_SHADOW_ONLY",
            "requires_replay_before_promotion": True,
            "assumed_maker_fill": True,
            "maker_fill_caveat": self.maker_caveat,
            "ref_price": pos.entry_price,
            "expected_edge_bps": {
                "taker_taker": round(gross - taker.round_trip_cost_bps, 4),
                "maker_first": round(gross - maker.round_trip_cost_bps, 4),
            },
            "intent": {
                "strategy_id": self.strategy_id,
                "symbol": self.symbol,
                "exchange": self.venue,
                "side": side,
                "direction": direction,
                "entry_price": pos.entry_price,
                "stop_price": pos.stop_price,
                "target_price": pos.target_price,
                "notional_usd": self.notional_usd,
            },
            "signal_reason": (
                f"cascade {active.start.side} exhaustion reversion; enter "
                f"{direction} @ {pos.entry_price:.6g} target {pos.target_price:.6g}"
            ),
            "resume": _cascade_resume(pos, active),
        }
        self.append_intent(key, pos.entry_ts_ms, payload)

    def _close(self, tr: TradePrint, reason: str) -> None:
        key = self._open_key
        assert key is not None
        self._replayer._close(
            self._open, tr, reason, exchange=self.venue, symbol=self.symbol,
            day=_day_from_ms(tr.ts_ms), result=self._result,
        )
        row = self._result.rows[-1]
        taker_bps = row.taker_net_bps
        maker_bps = row.maker_first_net_bps
        payload = {
            "intent_key": key,
            "family": self.family,
            "resolution": row.exit_reason,
            "side": "long" if row.direction == "buy" else "short",
            "virtual_net_usd": round(taker_bps / 10_000.0 * self.notional_usd, 6),
            "taker_net_usd": round(taker_bps / 10_000.0 * self.notional_usd, 6),
            "maker_net_usd": round(maker_bps / 10_000.0 * self.notional_usd, 6),
            "taker_net_bps": round(taker_bps, 4),
            "maker_net_bps": round(maker_bps, 4),
            "entry_price": row.entry_price_raw,
            "exit_price": row.exit_price_raw,
            "entry_ts_ms": row.entry_ts_ms,
            "exit_ts_ms": row.exit_ts_ms,
            "bar_ts": _iso_from_ms(row.exit_ts_ms),
        }
        self.append_outcome(key, payload, taker_bps=taker_bps, maker_bps=maker_bps)
        self._open = None
        self._open_key = None

    def _rebuild_open(self, key: str, payload: dict) -> None:
        pos = _rebuild_cascade_open(payload.get("resume") or {})
        if pos is None:
            return
        self._open = pos
        self._open_key = key


def _cascade_resume(pos: _OpenEvaluation, active: _ActiveCascade) -> dict:
    start = active.start
    return {
        "direction": pos.direction,
        "entry_ts_ms": pos.entry_ts_ms,
        "entry_price": pos.entry_price,
        "stop_price": pos.stop_price,
        "target_price": pos.target_price,
        "cascade": {
            "side": start.side,
            "start_ms": start.start_ms,
            "detected_ms": start.detected_ms,
            "burst_notional_usd": start.burst_notional_usd,
            "one_sided_frac": start.one_sided_frac,
            "threshold_usd": start.threshold_usd,
            "peak_notional_usd": start.peak_notional_usd,
            "extreme_price": active.extreme_price,
            "peak_notional": active.peak_notional,
            "pre_vwap": active.pre_vwap,
            "last_significant_ms": active.last_significant_ms,
        },
    }


def _rebuild_cascade_open(resume: dict) -> _OpenEvaluation | None:
    casc = resume.get("cascade") or {}
    try:
        start = CascadeStart(
            side=str(casc["side"]),
            start_ms=int(casc["start_ms"]),
            detected_ms=int(casc["detected_ms"]),
            burst_notional_usd=float(casc["burst_notional_usd"]),
            one_sided_frac=float(casc["one_sided_frac"]),
            threshold_usd=float(casc["threshold_usd"]),
            peak_notional_usd=float(casc["peak_notional_usd"]),
            extreme_price=float(casc["extreme_price"]),
        )
        active = _ActiveCascade(
            start=start,
            peak_notional=float(casc["peak_notional"]),
            last_significant_ms=int(casc.get("last_significant_ms", start.detected_ms)),
            extreme_price=float(casc["extreme_price"]),
            pre_vwap=float(casc["pre_vwap"]),
        )
        return _OpenEvaluation(
            cascade=active,
            direction=str(resume["direction"]),
            entry_ts_ms=int(resume["entry_ts_ms"]),
            entry_price=float(resume["entry_price"]),
            stop_price=float(resume["stop_price"]),
            target_price=float(resume["target_price"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


# --- lead-lag echo lane ------------------------------------------------------------

class EchoShadowLane(_ScalpLaneBase):
    """Live cross-venue lead-lag echo firing. Drives ``LeaderImpulseDetector``
    on the leader tape and reuses ``EchoScalpReplayer``'s two-leg resolution
    (taker cross + queue-aware maker) on the follower book & trades."""

    family = FAMILY_LEADLAG
    strategy_id = LEADLAG_STRATEGY_ID
    maker_caveat = ECHO_MAKER_CAVEAT

    def __init__(self, *, pair: LeadLagPair, journal: DecisionJournal,
                 params: EchoScalpParams | None = None) -> None:
        self.params = params or EchoScalpParams()
        self.pair = pair
        self.models = echo_cost_models(pair.follower_exchange)
        super().__init__(venue=pair.follower_exchange,
                         symbol=pair.follower_symbol, journal=journal,
                         min_events=self.params.min_events_for_candidate,
                         notional_usd=self.params.notional_usd)
        self._detector = LeaderImpulseDetector(self.params)
        self._replayer = EchoScalpReplayer(self.params, self.models)
        self._result = EchoDayResult()
        self._book: OrderBookL2 | None = None
        self._scalp: _OpenScalp | None = None
        self._scalp_key: str | None = None
        self._load()

    def open_count(self) -> int:
        return 1 if self._scalp is not None else 0

    def _echo_key(self, impulse_ts_ms: int) -> str:
        return (f"{self.strategy_id}|{self.venue}|{_symbol_safe(self.symbol)}"
                f"|{impulse_ts_ms}")

    def on_leader_trade(self, lt: LeaderTrade) -> None:
        self._touch(lt.ts_ms)
        impulse = self._detector.on_trade(lt)
        if impulse is None:
            return
        if self._scalp is not None:
            return  # overlapping impulse — skipped, never queued
        if self._book is None:
            return  # no follower book yet — nothing to rest against
        key = self._echo_key(impulse.ts_ms)
        if key in self._resolved or key == self._scalp_key:
            return
        self._scalp = self._replayer._open(impulse, self._book)
        self._scalp_key = key
        self._journal_intent(key, impulse, self._scalp)

    def on_follower_book(self, book: OrderBookL2) -> None:
        self._book = book
        ts_ms = int(book.event_time.timestamp() * 1000)
        self._touch(ts_ms)
        if self._scalp is not None:
            self._replayer._on_book(self._scalp, book, ts_ms)
            if self._scalp.done():
                self._emit()

    def on_follower_trade(self, ft: FollowerTrade) -> None:
        self._touch(ft.ts_ms)
        if self._scalp is not None and self._book is not None:
            self._replayer._on_follower_trade(self._scalp, ft, self._book)
            if self._scalp.done():
                self._emit()

    def _journal_intent(self, key: str, impulse: Impulse,
                        scalp: _OpenScalp) -> None:
        direction = impulse.direction
        taker = scalp.taker
        maker = scalp.maker
        taker_gross = _signed_gross_bps(direction, taker.entry_price,
                                        taker.target_price)
        maker_gross = _signed_gross_bps(direction, maker.limit_price,
                                        self._replayer._target(direction,
                                                               maker.limit_price))
        taker_model = self.models["taker_taker"]
        maker_model = self.models["maker_first"]
        side = "long" if direction == "buy" else "short"
        payload = {
            "intent_key": key,
            "family": self.family,
            "approved": False,
            "approval_state": "REALTIME_SHADOW_ONLY",
            "requires_replay_before_promotion": True,
            "assumed_queue_fill": True,
            "maker_fill_caveat": self.maker_caveat,
            "ref_price": scalp.follower_mid_at_impulse,
            "expected_edge_bps": {
                "taker_taker": round(taker_gross - taker_model.round_trip_fee_bps, 4),
                "maker_first": round(maker_gross - maker_model.round_trip_fee_bps, 4),
            },
            "intent": {
                "strategy_id": self.strategy_id,
                "symbol": self.symbol,
                "exchange": self.venue,
                "side": side,
                "direction": direction,
                "taker_entry_price": taker.entry_price,
                "maker_limit_price": maker.limit_price,
                "stop_price": taker.stop_price,
                "target_price": taker.target_price,
                "notional_usd": self.notional_usd,
            },
            "signal_reason": (
                f"{self.pair.leader_exchange} {self.pair.leader_symbol} impulse "
                f"{impulse.move_bps:+.2f}bps leads {self.venue} {self.symbol}; "
                f"{side} echo, maker rests at touch"
            ),
            "resume": _echo_resume(scalp),
        }
        self.append_intent(key, impulse.ts_ms, payload)

    def _emit(self) -> None:
        key = self._scalp_key
        assert key is not None
        self._replayer._emit(
            self._scalp, self._result, base=self.pair.base,
            leader_exchange=self.pair.leader_exchange,
            follower_exchange=self.pair.follower_exchange,
            follower_symbol=self.pair.follower_symbol,
            day=_day_from_ms(self._scalp.impulse.ts_ms),
        )
        row = self._result.rows[-1]
        taker_bps = row.taker_net_bps
        maker_bps = row.maker_net_bps  # None when the maker leg missed
        payload = {
            "intent_key": key,
            "family": self.family,
            "resolution": row.taker_exit_reason,
            "side": "long" if row.direction == "buy" else "short",
            "virtual_net_usd": round(taker_bps / 10_000.0 * self.notional_usd, 6),
            "taker_net_usd": round(taker_bps / 10_000.0 * self.notional_usd, 6),
            "maker_net_usd": (
                round(maker_bps / 10_000.0 * self.notional_usd, 6)
                if maker_bps is not None else None
            ),
            "taker_net_bps": round(taker_bps, 4),
            "maker_net_bps": round(maker_bps, 4) if maker_bps is not None else None,
            "maker_filled": bool(row.maker_filled),
            "maker_fill_lag_ms": row.maker_fill_lag_ms,
            "taker_entry_price": row.taker_entry_price,
            "taker_exit_price": row.taker_exit_price,
            "impulse_ts_ms": row.impulse_ts_ms,
            "bar_ts": _iso_from_ms(row.taker_exit_ts_ms or row.impulse_ts_ms),
        }
        self.append_outcome(key, payload, taker_bps=taker_bps, maker_bps=maker_bps)
        self._scalp = None
        self._scalp_key = None

    def _rebuild_open(self, key: str, payload: dict) -> None:
        scalp = _rebuild_echo_open(payload.get("resume") or {})
        if scalp is None:
            return
        self._scalp = scalp
        self._scalp_key = key


def _echo_resume(scalp: _OpenScalp) -> dict:
    imp = scalp.impulse
    t = scalp.taker
    m = scalp.maker
    return {
        "impulse": {
            "ts_ms": imp.ts_ms,
            "direction": imp.direction,
            "move_bps": imp.move_bps,
            "ref_price": imp.ref_price,
            "leader_price": imp.leader_price,
        },
        "follower_mid_at_impulse": scalp.follower_mid_at_impulse,
        "taker_entry_walk_slippage_bps": scalp.taker_entry_walk_slippage_bps,
        "taker_fully_filled": scalp.taker_fully_filled,
        "taker": {
            "direction": t.direction,
            "entry_ts_ms": t.entry_ts_ms,
            "entry_price": t.entry_price,
            "stop_price": t.stop_price,
            "target_price": t.target_price,
            "state": t.state,
            "exit_ts_ms": t.exit_ts_ms,
            "exit_price": t.exit_price,
            "exit_reason": t.exit_reason,
        },
        "maker": {
            "direction": m.direction,
            "limit_price": m.limit_price,
            "placed_ts_ms": m.placed_ts_ms,
            "queue_ahead": m.queue_ahead,
            "queue_consumed": m.queue_consumed,
            "state": m.state,
            "fill_ts_ms": m.fill_ts_ms,
            "stop_price": m.stop_price,
            "target_price": m.target_price,
            "exit_ts_ms": m.exit_ts_ms,
            "exit_price": m.exit_price,
            "exit_reason": m.exit_reason,
        },
    }


def _rebuild_echo_open(resume: dict) -> _OpenScalp | None:
    try:
        imp = resume["impulse"]
        t = resume["taker"]
        m = resume["maker"]
        impulse = Impulse(
            ts_ms=int(imp["ts_ms"]),
            direction=str(imp["direction"]),
            move_bps=float(imp["move_bps"]),
            ref_price=float(imp["ref_price"]),
            leader_price=float(imp["leader_price"]),
        )
        taker = _Leg(
            direction=str(t["direction"]),
            entry_ts_ms=int(t["entry_ts_ms"]),
            entry_price=float(t["entry_price"]),
            stop_price=float(t["stop_price"]),
            target_price=float(t["target_price"]),
            state=str(t["state"]),
            exit_ts_ms=int(t.get("exit_ts_ms") or 0),
            exit_price=float(t.get("exit_price") or 0.0),
            exit_reason=str(t.get("exit_reason") or ""),
        )
        fill_ts = m.get("fill_ts_ms")
        maker = _MakerLeg(
            direction=str(m["direction"]),
            limit_price=float(m["limit_price"]),
            placed_ts_ms=int(m["placed_ts_ms"]),
            queue_ahead=float(m["queue_ahead"]),
            queue_consumed=float(m.get("queue_consumed") or 0.0),
            state=str(m["state"]),
            fill_ts_ms=int(fill_ts) if fill_ts is not None else None,
            stop_price=float(m.get("stop_price") or 0.0),
            target_price=float(m.get("target_price") or 0.0),
            exit_ts_ms=int(m.get("exit_ts_ms") or 0),
            exit_price=float(m.get("exit_price") or 0.0),
            exit_reason=str(m.get("exit_reason") or ""),
        )
        return _OpenScalp(
            impulse=impulse,
            follower_mid_at_impulse=float(resume["follower_mid_at_impulse"]),
            taker=taker,
            taker_entry_walk_slippage_bps=float(resume["taker_entry_walk_slippage_bps"]),
            taker_fully_filled=bool(resume["taker_fully_filled"]),
            maker=maker,
        )
    except (KeyError, TypeError, ValueError):
        return None


def _resume_event_ms(payload: dict) -> int | None:
    resume = payload.get("resume") or {}
    if "entry_ts_ms" in resume:
        try:
            return int(resume["entry_ts_ms"])
        except (TypeError, ValueError):
            return None
    imp = resume.get("impulse") or {}
    if "ts_ms" in imp:
        try:
            return int(imp["ts_ms"])
        except (TypeError, ValueError):
            return None
    return None


def _as_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


# --- follower book conversion ------------------------------------------------------

def delta_book_from_arrays(symbol: str, buy: list, sell: list,
                           ts_ms: int, *, levels: int = 10) -> OrderBookL2 | None:
    """Build an ``OrderBookL2`` from Delta native ``l2_orderbook`` arrays
    (``{"limit_price","size",...}``). Crossed/empty snapshots return None so a
    bad book never poisons a scalp — the last good book stays in effect."""
    try:
        bids = tuple(
            (float(e["limit_price"]), float(e["size"])) for e in buy[:levels]
        )
        asks = tuple(
            (float(e["limit_price"]), float(e["size"])) for e in sell[:levels]
        )
    except (KeyError, TypeError, ValueError):
        return None
    if not bids or not asks:
        return None
    try:
        return OrderBookL2(
            symbol=symbol, bids=bids, asks=asks,
            event_time=datetime.fromtimestamp(ts_ms / 1000, tz=UTC),
        )
    except ValueError:
        return None


# --- runner ------------------------------------------------------------------------

class RealtimeShadowScalpRunner:
    """Owns the family lanes, routes live ticks, and publishes the aggregate.

    Construction is pure (no network): the lanes are testable with a scripted
    synthetic stream. ``run_forever`` wires the live public streams. Nothing
    here submits an order, evaluates a gateway, or touches credentials."""

    def __init__(
        self,
        *,
        cascade_targets: tuple[tuple[str, str], ...] = (),
        echo_pairs: tuple[LeadLagPair, ...] = (),
        journal_dir: Path | str = DEFAULT_JOURNAL_DIR,
        out_dir: Path | str = DEFAULT_OUT_DIR,
        cascade_params: CascadeParams | None = None,
        echo_params: EchoScalpParams | None = None,
        notional_usd: float = DEFAULT_NOTIONAL_USD,
    ) -> None:
        self.journal_dir = Path(journal_dir)
        self.out_dir = Path(out_dir)
        self.journal_dir.mkdir(parents=True, exist_ok=True)
        self.cascade_lanes: dict[tuple[str, str], CascadeShadowLane] = {}
        self.echo_lanes: dict[str, EchoShadowLane] = {}
        for venue, symbol in cascade_targets:
            journal = self._journal(FAMILY_CASCADE, venue, symbol)
            self.cascade_lanes[(venue, symbol)] = CascadeShadowLane(
                venue=venue, symbol=symbol, journal=journal,
                params=cascade_params, notional_usd=notional_usd,
            )
        for pair in echo_pairs:
            journal = self._journal(FAMILY_LEADLAG, pair.follower_exchange,
                                    pair.follower_symbol)
            self.echo_lanes[pair.label] = EchoShadowLane(
                pair=pair, journal=journal, params=echo_params,
            )

    def _journal(self, family: str, venue: str, symbol: str) -> DecisionJournal:
        name = f"{family}_{venue}_{_symbol_safe(symbol)}.journal.jsonl"
        return DecisionJournal(self.journal_dir / name)

    @property
    def lanes(self) -> list[_ScalpLaneBase]:
        return [*self.cascade_lanes.values(), *self.echo_lanes.values()]

    # -- payload / publish --
    def build_payload(self) -> dict:
        lane_rows = [lane.summary() for lane in self.lanes]
        verdict_counts: dict[str, int] = {}
        for row in lane_rows:
            verdict_counts[row["verdict"]] = verdict_counts.get(row["verdict"], 0) + 1
        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "runner_id": RUNNER_ID,
            "mode": "realtime_shadow_only",
            "policy": realtime_shadow_scalp_policy(),
            "notional_usd": self.lanes[0].notional_usd if self.lanes else DEFAULT_NOTIONAL_USD,
            "lanes": lane_rows,
            "summary": {
                "lanes": len(lane_rows),
                "intents": sum(r["intents"] for r in lane_rows),
                "virtual_trades": sum(r["virtual_trades"] for r in lane_rows),
                "maker_beats_taker_lanes": sum(
                    1 for r in lane_rows if r["maker_beats_taker"]
                ),
                "verdict_counts": verdict_counts,
                "can_trade": False,
                "can_promote": False,
            },
            "can_trade": False,
            "can_promote": False,
        }

    def publish(self) -> Path:
        return write_realtime_shadow_scalp_payload(self.build_payload(), self.out_dir)

    # -- live wiring (network; not exercised by unit tests) --
    async def run_forever(self, *, publish_interval_seconds: float = 30.0) -> None:
        """Subscribe to the public live streams and publish periodically.

        Every stream loop reconnects with bounded backoff on any error and never
        crashes the runner — a feed drop degrades to a reconnect, matching the
        recorders' failure posture."""
        tasks: list[asyncio.Task] = []
        cascade_venues = {venue for venue, _ in self.cascade_lanes}
        for venue in cascade_venues:
            symbols = [s for (v, s) in self.cascade_lanes if v == venue]
            tasks.append(asyncio.create_task(
                self._cascade_feed(venue, symbols), name=f"cascade-{venue}"))
        if self.echo_lanes:
            tasks.append(asyncio.create_task(
                self._echo_feeds(), name="echo-feeds"))
        tasks.append(asyncio.create_task(
            self._publish_loop(publish_interval_seconds), name="publish"))
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _publish_loop(self, interval: float) -> None:
        while True:
            try:
                path = self.publish()
                logger.info("published %s", path)
            except Exception as exc:  # noqa: BLE001 — publish must not kill feeds
                logger.warning("realtime shadow scalp publish failed: %s", exc)
            await asyncio.sleep(max(interval, 1.0))

    async def _cascade_feed(self, venue: str, symbols: list[str]) -> None:
        import ccxt.pro as ccxtpro

        from vnedge.exchange.liquidation_recorder import _liq_row

        ex = getattr(ccxtpro, venue)({"enableRateLimit": True})
        try:
            loops = []
            for symbol in symbols:
                lane = self.cascade_lanes[(venue, symbol)]
                loops.append(self._watch_trades(ex, symbol, lane))
                loops.append(self._watch_liquidations(ex, symbol, lane, _liq_row, venue))
            await asyncio.gather(*loops)
        finally:
            await ex.close()

    async def _watch_trades(self, ex, symbol: str, lane: CascadeShadowLane) -> None:
        while True:
            try:
                trades = await ex.watch_trades(symbol)
                for t in trades:
                    if t.get("price") and t.get("amount"):
                        lane.on_trade(TradePrint(
                            ts_ms=int(t["timestamp"]),
                            price=float(t["price"]),
                            amount=float(t["amount"]),
                        ))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — reconnect with backoff
                logger.warning("%s trades error: %s", symbol, exc)
                await asyncio.sleep(2.0)

    async def _watch_liquidations(self, ex, symbol: str, lane: CascadeShadowLane,
                                  liq_row, venue: str) -> None:
        while True:
            try:
                liqs = await ex.watch_liquidations(symbol)
                for liq in liqs:
                    row = liq_row(liq, venue)
                    if row is None or row["price"] <= 0 or row["notional_usd"] <= 0:
                        continue
                    lane.on_liquidation(LiquidationEvent(
                        ts_ms=int(row["ts_ms"]), price=float(row["price"]),
                        amount=float(row["amount"]), side=str(row["side"]),
                        notional_usd=float(row["notional_usd"]),
                    ))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — reconnect with backoff
                logger.warning("%s liquidations error: %s", symbol, exc)
                await asyncio.sleep(2.0)

    async def _echo_feeds(self) -> None:
        import ccxt.pro as ccxtpro

        from vnedge.exchange.delta_ws import DeltaPublicWsClient, delta_native_symbol

        # Follower (Delta native) book + trades — one WS client for all followers.
        follower_map: dict[str, EchoShadowLane] = {}
        follower_symbols: list[str] = []
        leader_targets: dict[str, list[tuple[str, EchoShadowLane]]] = {}
        for lane in self.echo_lanes.values():
            native = delta_native_symbol(lane.pair.follower_symbol)
            follower_map[native] = lane
            follower_symbols.append(lane.pair.follower_symbol)
            leader_targets.setdefault(lane.pair.leader_exchange, []).append(
                (lane.pair.leader_symbol, lane))

        def on_book(native: str, buy: list, sell: list, msg: dict) -> None:
            lane = follower_map.get(native)
            if lane is None:
                return
            ts_raw = msg.get("timestamp")
            ts_ms = int(ts_raw) // 1000 if ts_raw is not None else _now_ms()
            book = delta_book_from_arrays(lane.pair.follower_symbol, buy, sell, ts_ms)
            if book is not None:
                lane.on_follower_book(book)

        def on_trade(native: str, trade: dict) -> None:
            lane = follower_map.get(native)
            if lane is None:
                return
            lane.on_follower_trade(FollowerTrade(
                ts_ms=int(trade["ts_ms"]), price=float(trade["price"]),
                amount=float(trade["size"]), taker_side=str(trade.get("side", "")),
            ))

        client = DeltaPublicWsClient(
            follower_symbols, channels=("l2_orderbook", "all_trades"),
            on_book=on_book, on_trade=on_trade,
        )
        await client.start()
        try:
            loops = []
            for leader_exchange, targets in leader_targets.items():
                ex = getattr(ccxtpro, leader_exchange)({"enableRateLimit": True})
                loops.append(self._watch_leader(ex, targets))
            await asyncio.gather(*loops)
        finally:
            await client.stop()

    async def _watch_leader(self, ex,
                            targets: list[tuple[str, EchoShadowLane]]) -> None:
        try:
            loops = [self._watch_leader_symbol(ex, sym, lane) for sym, lane in targets]
            await asyncio.gather(*loops)
        finally:
            await ex.close()

    async def _watch_leader_symbol(self, ex, symbol: str,
                                   lane: EchoShadowLane) -> None:
        while True:
            try:
                trades = await ex.watch_trades(symbol)
                for t in trades:
                    if t.get("price") and t.get("amount"):
                        lane.on_leader_trade(LeaderTrade(
                            ts_ms=int(t["timestamp"]),
                            price=float(t["price"]),
                            amount=float(t["amount"]),
                        ))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — reconnect with backoff
                logger.warning("leader %s trades error: %s", symbol, exc)
                await asyncio.sleep(2.0)


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def write_realtime_shadow_scalp_payload(payload: dict, out_dir: Path | str) -> Path:
    """Atomic publish for the continuous_research folding hook (tmp+replace)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / REALTIME_SHADOW_SCALP_LATEST
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    tmp.replace(path)
    return path


# --- CLI ---------------------------------------------------------------------------

DEFAULT_CASCADE_SYMBOLS = ("BTC/USDT:USDT", "ETH/USDT:USDT")
DEFAULT_CASCADE_VENUE = "binanceusdm"


def render_report(payload: dict) -> str:
    lines = [
        "real-time shadow scalp (live-tick firing of the scalp detectors)",
        "policy=realtime_shadow_only can_trade=false can_promote=false",
        "maker legs ASSUME passive fills (L2 queue replay required); promotion "
        "still needs replay judgment + human approval",
        "",
        "verdict             family        intents  vtrades  taker$  maker$  ev/h"
        "   exchange     symbol",
    ]
    for row in payload["lanes"]:
        agg = row["aggregates"]
        eph = row["events_per_hour"]
        lines.append(
            f"{row['verdict']:<19} {row['family']:<13} {row['intents']:>7} "
            f"{row['virtual_trades']:>7} "
            f"{agg['taker_taker']['net_usd']:>7.2f} "
            f"{agg['maker_first']['net_usd']:>7.2f} "
            f"{(f'{eph:.1f}' if eph is not None else '-'):>6} "
            f"{row['exchange']:<12} {row['symbol']}"
        )
    if not payload["lanes"]:
        lines.append("no lanes configured")
    return "\n".join(lines)


def build_runner_from_env() -> RealtimeShadowScalpRunner:
    cascade_venue = os.environ.get("SCALP_CASCADE_VENUE", DEFAULT_CASCADE_VENUE)
    cascade_symbols = _split_csv(os.environ.get("SCALP_CASCADE_SYMBOLS")) or \
        DEFAULT_CASCADE_SYMBOLS
    cascade_targets = tuple((cascade_venue, s) for s in cascade_symbols)
    echo_bases = {b.upper() for b in _split_csv(os.environ.get("SCALP_ECHO_PAIRS"))}
    echo_pairs = tuple(
        p for p in DEFAULT_PAIRS if not echo_bases or p.base in echo_bases
    )
    return RealtimeShadowScalpRunner(
        cascade_targets=cascade_targets,
        echo_pairs=echo_pairs,
        cascade_params=CascadeParams.from_env(),
        echo_params=EchoScalpParams.from_env(),
        notional_usd=_env_float("SCALP_NOTIONAL_USD", DEFAULT_NOTIONAL_USD),
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(
        description="real-time shadow scalp runner (research only, never trades)")
    p.add_argument("--publish-interval-seconds", type=float, default=30.0)
    p.add_argument("--once", action="store_true",
                   help="build and publish one payload, then exit (no live feeds)")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    runner = build_runner_from_env()
    if args.once:
        payload = runner.build_payload()
        runner.publish()
        print(json.dumps(payload, indent=2, default=str) if args.json
              else render_report(payload))
        return 0
    logger.info("real-time shadow scalp: %d cascade lane(s), %d echo lane(s)",
                len(runner.cascade_lanes), len(runner.echo_lanes))
    asyncio.run(runner.run_forever(
        publish_interval_seconds=args.publish_interval_seconds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
