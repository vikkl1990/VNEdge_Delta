"""Virtual outcome resolution for shadow lanes — per-lane edge visibility.

Shadow lanes journal approved entry intents (``shadow_intent``) but never
fill, so their realized PnL is structurally zero and the operator cannot
see WHICH lane has edge. This module resolves each approved shadow intent
forward on subsequent closed bars into a VIRTUAL trade with the same
conservative semantics the backtester enforces:

- entry at the recorded reference price (the intent's notional/quantity —
  the adverse book side captured at intent time, so the spread cost is
  already embedded in the entry);
- the first bar AFTER the decision bar is the virtual fill bar (decisions
  at close, fills at next open — same as research), and a stop can be hit
  in the fill bar itself;
- intrabar exits resolve stop-first on ties: if both stop and target lie
  inside one bar's range, the STOP fills (``backtester._check_intrabar_exit``
  convention);
- max-holding timeout closes at bar close after ``max_holding_bars`` bars,
  mirroring ``run_backtest``'s ``j - entry_bar >= max_holding_bars``;
- taker fees on BOTH virtual fills via the paper ``FillModel`` — if virtual
  results look better than paper fills would have been, the model is wrong.

MAKER ROUTE (opt-in, per lane): strategies whose edge is defined *after maker
fees* (the fee-wall "MAKER_EDGE" set) are structurally mispriced by the all-taker
default — they never get to show their edge. For those lanes the tracker models
the resting-limit route honestly, not optimistically: the entry fills ONLY if a
subsequent bar's range touches the resting limit within its TTL (so the immediate
runners a maker order would MISS are missed here too — the adverse selection is
real), fills at the same reference price (conservative — no imaginary price
improvement), and pays the maker fee on the entry leg while the exit stays taker
(stops are market orders). Every maker outcome also carries the all-taker number
(``virtual_net_taker_usd``) so the conservative figure is never hidden.

Each resolution is journaled as a ``shadow_outcome`` record. The journal
itself is the durable store: on restart the tracker reloads unresolved
intents and skips every intent_key that already has an outcome record, so
nothing is ever resolved twice.

Virtual outcomes are OBSERVABILITY ONLY: they gate nothing, promote
nothing, and trade nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

import pandas as pd

from vnedge.execution.journal import DecisionJournal
from vnedge.paper.fill_model import FillModel
from vnedge.runtime.active_exit import _better_stop
from vnedge.strategy.base_strategy import StrategyExitIntent

logger = logging.getLogger(__name__)

#: Strategies whose scorecard edge is a MAKER edge (fee-wall "MAKER_EDGE" /
#: maker-routed set). Only these lanes get the maker-route shadow model; every
#: other lane stays all-taker. Matched by strategy-id prefix (version suffixes).
MAKER_ROUTE_STRATEGIES: tuple[str, ...] = (
    "stealth_trail_bbp",
    "luxy_ut_bot_forecast",
    "luxara_live_plan_qtm",
    "luxara_break_bounce",
    "fvg_liquidity_breakout",
    "context_scalper_v2",
    "vnedge_algo_ml_pro",
    "momentum_cascade_lyro",
)


def is_maker_route_strategy(strategy_id: str) -> bool:
    """True when the strategy's edge is defined after maker fees (opt-in route)."""
    sid = (strategy_id or "").lower()
    return any(sid.startswith(name) for name in MAKER_ROUTE_STRATEGIES)


@dataclass
class _PendingIntent:
    intent_key: str
    side: str  # "long" | "short"
    quantity: float
    notional_usd: float
    entry_price: float  # recorded ref quote = notional / quantity
    stop_price: float
    take_profit_price: float | None
    decision_bar_ts: pd.Timestamp  # bar whose close produced the intent
    signal_reason: str = ""
    bars_held: int = -1  # -1 = virtual fill not reached yet; fill bar = 0
    take_profit_levels: tuple[float, ...] = ()  # TP ladder (tp1, tp2, …) for the journal
    mfe_price: float = 0.0  # max-favourable price since entry — observation only
    filled: bool = True  # taker fills immediately; maker waits for a touch
    bars_waiting: int = 0  # maker-route only: bars the resting limit has waited


@dataclass(frozen=True)
class VirtualOutcome:
    intent_key: str
    resolution: str  # "stop" | "target" | "timeout" | strategy-managed reason
    bars_held: int
    virtual_net_usd: float
    side: str
    entry_price: float
    exit_price: float
    fees_usd: float
    resolved_bar_ts: str
    take_profit_levels: tuple[float, ...] = ()
    tp_reached: int = 0  # how many ladder levels the trade's excursion crossed
    route: str = "taker"  # "maker" if the entry filled as a resting limit
    fees_taker_usd: float = 0.0  # the all-taker equivalent fee (transparency)
    virtual_net_taker_usd: float = 0.0  # net under all-taker fees (transparency)


def _tp_reached(side: str, mfe_price: float, levels: tuple[float, ...]) -> int:
    """How many TP-ladder levels the max-favourable excursion crossed."""
    if not levels:
        return 0
    if side == "long":
        return sum(1 for level in levels if mfe_price >= level)
    return sum(1 for level in levels if mfe_price <= level)


class ShadowOutcomeTracker:
    """Resolves a shadow lane's journaled intents into virtual outcomes.

    Constructed from the lane's own decision journal: approved
    ``shadow_intent`` records without a matching ``shadow_outcome`` record
    become the pending set; already-resolved outcomes seed the cumulative
    stats so restarts keep the lane's virtual track record intact.
    """

    def __init__(
        self,
        journal: DecisionJournal,
        *,
        fill_model: FillModel | None = None,
        max_holding_bars: int = 48,
        maker_route: bool = False,
        maker_fill_ttl_bars: int = 1,
        trail_atr_mult: float = 0.0,
        strategy_exit: Callable[[str, float, pd.Series], StrategyExitIntent | None] | None = None,
    ) -> None:
        self.journal = journal
        self.fill_model = fill_model or FillModel()
        self.max_holding_bars = max_holding_bars
        # ATR-chandelier trail on the virtual stop — mirrors the paper/live
        # ActiveExitState exactly (same _better_stop ratchet, fed the same
        # canonical ATR), so a shadow lane predicts its paper/live twin instead
        # of running the legacy fixed-stop exit. 0.0 = off (unchanged behaviour).
        self.trail_atr_mult = float(trail_atr_mult)
        # Maker route: entries fill only on a touch within maker_fill_ttl_bars,
        # and pay the maker fee. Off => the all-taker default, byte-for-byte.
        self.maker_route = maker_route
        self.maker_fill_ttl_bars = max(1, int(maker_fill_ttl_bars))
        # Optional strategy-managed close hook. Stop/target are still checked
        # first; this only decides bar-close exits for open virtual positions.
        self.strategy_exit = strategy_exit
        self._pending: dict[str, _PendingIntent] = {}
        self._resolved_keys: set[str] = set()
        self._trades = 0
        self._wins = 0
        self._net_usd = 0.0
        self._net_taker_usd = 0.0  # same trades, priced all-taker (transparency)
        self._gross_win_usd = 0.0
        self._gross_loss_usd = 0.0
        self._unfilled = 0  # maker limits that never got a touch (missed trades)
        self._resolutions: dict[str, int] = {"stop": 0, "target": 0, "timeout": 0}
        self._load()

    # --- journal replay ----------------------------------------------------------
    def _load(self) -> None:
        intents: dict[str, dict] = {}
        for record in self.journal.read_all():
            kind, payload = record.get("kind"), record.get("payload", {})
            if kind == "shadow_intent":
                key = payload.get("intent_key")
                if key and payload.get("approved") and key not in intents:
                    intents[key] = payload
            elif kind == "shadow_outcome":
                key = payload.get("intent_key")
                if key is None or key in self._resolved_keys:
                    continue
                self._resolved_keys.add(key)
                self._accumulate(
                    str(payload.get("resolution", "")),
                    float(payload.get("virtual_net_usd", 0.0)),
                    float(payload.get("virtual_net_taker_usd",
                                      payload.get("virtual_net_usd", 0.0))),
                )
            elif kind == "shadow_maker_unfilled":
                key = payload.get("intent_key")
                if key is not None and key not in self._resolved_keys:
                    self._resolved_keys.add(key)
                    self._unfilled += 1
        for key, payload in intents.items():
            if key in self._resolved_keys:
                continue
            pending = self._parse_intent(key, payload)
            if pending is not None:
                self._pending[key] = pending

    def _parse_intent(self, key: str, payload: dict) -> _PendingIntent | None:
        """Build a pending virtual position from a journaled shadow_intent.

        Records predating outcome tracking lack stop/target/bar_ts and are
        skipped — they cannot be resolved honestly."""
        intent = payload.get("intent") or {}
        stop = payload.get("stop_price")
        bar_ts = payload.get("bar_ts")
        quantity = float(intent.get("quantity") or 0.0)
        notional = float(intent.get("notional_usd") or 0.0)
        if stop is None or bar_ts is None or quantity <= 0 or notional <= 0:
            return None
        tp = payload.get("take_profit_price")
        levels = payload.get("take_profit_levels") or ()
        entry_price = notional / quantity
        return _PendingIntent(
            intent_key=key,
            side=str(intent.get("side", "long")),
            quantity=quantity,
            notional_usd=notional,
            entry_price=entry_price,
            stop_price=float(stop),
            take_profit_price=float(tp) if tp is not None else None,
            decision_bar_ts=pd.Timestamp(bar_ts),
            signal_reason=str(payload.get("signal_reason") or ""),
            take_profit_levels=tuple(float(x) for x in levels),
            mfe_price=entry_price,
            filled=not self.maker_route,  # maker entries wait for a touch
        )

    # --- live registration -------------------------------------------------------
    def track(
        self,
        *,
        intent_key: str,
        side: str,
        quantity: float,
        notional_usd: float,
        stop_price: float,
        take_profit_price: float | None,
        decision_bar_ts: pd.Timestamp,
        signal_reason: str = "",
        take_profit_levels: tuple[float, ...] = (),
    ) -> None:
        """Register a just-approved shadow intent for forward resolution."""
        if intent_key in self._pending or intent_key in self._resolved_keys:
            return  # restart re-prime can re-journal the same decision
        if quantity <= 0 or notional_usd <= 0:
            return
        entry_price = notional_usd / quantity
        self._pending[intent_key] = _PendingIntent(
            intent_key=intent_key,
            side=side,
            quantity=quantity,
            notional_usd=notional_usd,
            entry_price=entry_price,
            stop_price=stop_price,
            take_profit_price=take_profit_price,
            decision_bar_ts=pd.Timestamp(decision_bar_ts),
            signal_reason=signal_reason,
            take_profit_levels=tuple(take_profit_levels),
            mfe_price=entry_price,  # starts at entry; updated each active bar
            filled=not self.maker_route,  # maker entries wait for a touch
        )

    @property
    def has_pending(self) -> bool:
        return bool(self._pending)

    # --- resolution --------------------------------------------------------------
    def resolve_bar(
        self, bar: pd.Series, atr: float | None = None
    ) -> list[VirtualOutcome]:
        """Advance every pending intent through one closed bar.

        Only bars strictly AFTER an intent's decision bar count (the first
        such bar is the virtual fill bar). Stop wins ties, target second,
        timeout at bar close once ``max_holding_bars`` is reached.

        ``atr`` is the canonical trail ATR for THIS bar (the same value the
        paper/live ActiveExitState uses). When ``trail_atr_mult > 0`` and a
        positive ATR is supplied, the virtual stop ratchets tighten-only off the
        running MFE, byte-identically to ActiveExitState.trail_stop."""
        bar_ts = pd.Timestamp(bar["timestamp"])
        high, low, close = float(bar["high"]), float(bar["low"]), float(bar["close"])
        outcomes: list[VirtualOutcome] = []
        for pending in list(self._pending.values()):
            if bar_ts <= pending.decision_bar_ts:
                continue
            # Maker route: the resting limit only fills if this bar's range
            # touches it. If the market runs away without a touch, the order is
            # cancelled after its TTL — the immediate runners a maker miss are
            # missed here too (honest adverse selection, not a free fill).
            if self.maker_route and not pending.filled:
                touched = (
                    low <= pending.entry_price if pending.side == "long"
                    else high >= pending.entry_price
                )
                if not touched:
                    pending.bars_waiting += 1
                    if pending.bars_waiting >= self.maker_fill_ttl_bars:
                        self._cancel_unfilled(pending, bar_ts)
                    continue
                pending.filled = True  # touched -> fills this bar as the fill bar
            pending.bars_held += 1  # fill bar counts as 0, like run_backtest
            # Track max-favourable excursion for the TP-ladder journal. This is
            # pure observation — it never feeds _check_exit, so the exit and P&L
            # are byte-identical to before.
            if pending.side == "long":
                pending.mfe_price = max(pending.mfe_price, high)
            else:
                pending.mfe_price = min(pending.mfe_price, low)
            # Check the exit FIRST, against the stop set by the PREVIOUS bar's
            # trail — exactly like ActiveExitState (resolve_bar then trail_stop).
            resolution, exit_price = self._check_exit(pending, high, low, close, bar)
            if resolution is None:
                # No exit this bar: ratchet the virtual stop off MFE, tighten-only,
                # using the SAME _better_stop + canonical ATR as paper/live. The
                # tightened stop applies to LATER bars (this bar's extreme is
                # already in mfe_price), so there is no intrabar lookahead —
                # byte-identical to ActiveExitState.trail_stop.
                if self.trail_atr_mult > 0.0 and atr is not None and atr > 0.0:
                    dist = self.trail_atr_mult * float(atr)
                    candidate = (
                        pending.mfe_price - dist if pending.side == "long"
                        else pending.mfe_price + dist
                    )
                    pending.stop_price = _better_stop(
                        pending.side, pending.stop_price, candidate
                    )
                continue
            outcomes.append(self._close(pending, resolution, exit_price, bar_ts))
        return outcomes

    def replay(self, candles: pd.DataFrame) -> list[VirtualOutcome]:
        """Resolve restored pending intents against already-seen candles —
        the seeded warmup history on restart. Live bars then continue
        naturally via resolve_bar."""
        if not self._pending or candles.empty:
            return []
        outcomes: list[VirtualOutcome] = []
        for i in range(len(candles)):
            outcomes.extend(self.resolve_bar(candles.iloc[i]))
            if not self._pending:
                break
        return outcomes

    def _check_exit(
        self,
        pending: _PendingIntent,
        high: float,
        low: float,
        close: float,
        bar: pd.Series,
    ) -> tuple[str | None, float]:
        tp = pending.take_profit_price
        if pending.side == "long":
            if low <= pending.stop_price:
                return "stop", pending.stop_price
            if tp is not None and high >= tp:
                return "target", tp
        else:
            if high >= pending.stop_price:
                return "stop", pending.stop_price
            if tp is not None and low <= tp:
                return "target", tp
        if self.strategy_exit is not None:
            exit_sig = self.strategy_exit(pending.side, pending.entry_price, bar)
            if exit_sig is not None:
                return (
                    exit_sig.reason,
                    (
                        float(exit_sig.exit_price)
                        if exit_sig.exit_price is not None
                        else close
                    ),
                )
        if pending.bars_held >= self.max_holding_bars:
            return "timeout", close
        return None, 0.0

    def _close(
        self,
        pending: _PendingIntent,
        resolution: str,
        exit_price: float,
        bar_ts: pd.Timestamp,
    ) -> VirtualOutcome:
        direction = 1.0 if pending.side == "long" else -1.0
        gross = direction * pending.quantity * (exit_price - pending.entry_price)
        exit_notional = pending.quantity * exit_price
        # Maker route pays the maker fee on the entry leg (the resting limit);
        # the exit stays taker (stops/targets exit at market) — conservative.
        entry_fee = self.fill_model.fee_usd(pending.notional_usd, maker=self.maker_route)
        exit_fee = self.fill_model.fee_usd(exit_notional)
        fees = entry_fee + exit_fee
        net = gross - fees
        # The all-taker equivalent on the SAME trade — the conservative figure
        # stays visible next to the maker number, never replaced by it.
        fees_taker = self.fill_model.fee_usd(pending.notional_usd) + exit_fee
        net_taker = gross - fees_taker
        route = "maker" if self.maker_route else "taker"
        tp_reached = _tp_reached(pending.side, pending.mfe_price, pending.take_profit_levels)
        outcome = VirtualOutcome(
            intent_key=pending.intent_key,
            resolution=resolution,
            bars_held=max(pending.bars_held, 0),
            virtual_net_usd=round(net, 6),
            side=pending.side,
            entry_price=round(pending.entry_price, 8),
            exit_price=round(exit_price, 8),
            fees_usd=round(fees, 6),
            resolved_bar_ts=bar_ts.isoformat(),
            take_profit_levels=pending.take_profit_levels,
            tp_reached=tp_reached,
            route=route,
            fees_taker_usd=round(fees_taker, 6),
            virtual_net_taker_usd=round(net_taker, 6),
        )
        del self._pending[pending.intent_key]
        self._resolved_keys.add(pending.intent_key)
        self._accumulate(resolution, net, net_taker)
        self.journal.append("shadow_outcome", {
            "intent_key": outcome.intent_key,
            "resolution": outcome.resolution,
            "bars_held": outcome.bars_held,
            "virtual_net_usd": outcome.virtual_net_usd,
            "side": outcome.side,
            "entry_price": outcome.entry_price,
            "exit_price": outcome.exit_price,
            "fees_usd": outcome.fees_usd,
            "route": outcome.route,
            "fees_taker_usd": outcome.fees_taker_usd,
            "virtual_net_taker_usd": outcome.virtual_net_taker_usd,
            "bar_ts": outcome.resolved_bar_ts,
            "signal_reason": pending.signal_reason,
            "take_profit_levels": list(pending.take_profit_levels),
            "tp_reached": tp_reached,
            "mfe_price": round(pending.mfe_price, 8),
        })
        logger.info(
            "shadow outcome: %s %s -> %s after %d bars, %s virtual %+0.2f USD",
            pending.side, pending.intent_key, resolution,
            outcome.bars_held, route, net,
        )
        return outcome

    def _cancel_unfilled(self, pending: _PendingIntent, bar_ts: pd.Timestamp) -> None:
        """A maker resting limit that never got a touch within its TTL — the
        trade simply didn't happen. Journaled as its own kind so it never enters
        the outcome/label stream (a missed fill is not a losing trade)."""
        del self._pending[pending.intent_key]
        self._resolved_keys.add(pending.intent_key)
        self._unfilled += 1
        self.journal.append("shadow_maker_unfilled", {
            "intent_key": pending.intent_key,
            "side": pending.side,
            "entry_price": round(pending.entry_price, 8),
            "bars_waited": pending.bars_waiting,
            "bar_ts": bar_ts.isoformat(),
            "signal_reason": pending.signal_reason,
        })
        logger.info(
            "shadow maker unfilled: %s %s no touch in %d bars — trade skipped",
            pending.side, pending.intent_key, pending.bars_waiting,
        )

    def _accumulate(self, resolution: str, net: float, net_taker: float | None = None) -> None:
        self._trades += 1
        self._net_usd += net
        self._net_taker_usd += net if net_taker is None else net_taker
        if net > 0:
            self._wins += 1
            self._gross_win_usd += net
        else:
            self._gross_loss_usd += -net
        self._resolutions[resolution] = self._resolutions.get(resolution, 0) + 1

    # --- reporting ----------------------------------------------------------------
    def stats(self) -> dict:
        """Per-lane virtual performance for session_stats / the dashboard."""
        if self._gross_loss_usd > 0:
            pf = round(self._gross_win_usd / self._gross_loss_usd, 3)
        else:
            pf = None  # no losing virtual trades yet — PF undefined, not infinite
        status = "SHADOW_PROBATION" if self._trades > 0 and self._net_usd < 0 else "OBSERVE"
        return {
            "virtual_trades": self._trades,
            "wins": self._wins,
            "losses": self._trades - self._wins,
            "net_usd": round(self._net_usd, 4),
            "profit_factor": pf,
            "open_intents": len(self._pending),
            "resolutions": dict(self._resolutions),
            "status": status,
            "trade_compatible": status != "SHADOW_PROBATION",
            # Maker-route transparency: the routing, the same trades priced
            # all-taker, and the maker limits that never filled (missed trades).
            "route": "maker" if self.maker_route else "taker",
            "net_taker_usd": round(self._net_taker_usd, 4),
            "maker_unfilled": self._unfilled,
        }
