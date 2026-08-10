"""Multi-exchange shadow lanes.

Runs the same (or different) strategy as parallel, fully isolated shadow
lanes across venues — e.g. funding-MR on Binance/Bybit and a candle-only
Delta lane — to answer: does the strategy behave better on one venue under
live markets?

Each lane is a complete, independent LivePaperSession:
- its own live feed (real venue websockets), simulated exchange, gateway,
  order manager, journal, account store, equity history, and $ shadow base.
- NO live orders, NO cross-venue routing. Pure per-venue shadow.

The dashboard sees the PRIMARY (governed) lane as the flat top-level
snapshot (backward-compatible), plus a `lanes` array for side-by-side
comparison. One venue's feed stalling never blocks another lane.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from ccxt.base.errors import NetworkError, NotSupported

from vnedge.config.risk_config import ABSOLUTE_MAX_LEVERAGE, RiskConfig
from vnedge.data.ccxt_client import CcxtPublicClient
from vnedge.data.schemas import normalize_candles, normalize_funding
from vnedge.exchange.feed_registry import SharedFeedView, acquire_market_feed
from vnedge.exchange.live_feed import LiveMarketFeed, RestPollingMarketFeed
from vnedge.exchange.venue_specs import venue_fill_model, venue_symbol_limits
from vnedge.execution.fill_ledger import FillLedger
from vnedge.execution.journal import DecisionJournal
from vnedge.execution.order_manager import OrderManager
from vnedge.execution.signal_arbiter import ArbiterConfig, SignalArbiter
from vnedge.paper.account_store import PaperAccountStore
from vnedge.runtime.funnel_store import LaneFunnelStore
from vnedge.paper.paper_broker import PaperBroker
from vnedge.paper.simulated_exchange import SimulatedExchange
from vnedge.risk.kill_switch import KillSwitch
from vnedge.risk.risk_manager import PreTradeRiskGateway
from vnedge.runtime.live_paper import LivePaperSession
from vnedge.runtime.paper_trial import LiveFundingMR
from vnedge.strategy.alpha_stack import AlphaStackConfluence
from vnedge.strategy.alpha_distillation_pack import AlphaDistillationPack
from vnedge.strategy.base_strategy import BaseStrategy
from vnedge.strategy.composite import CompositeSignalStrategy
from vnedge.strategy.context_scalper_v2 import ContextScalperV2
from vnedge.strategy.crypto_trend_atr_margin import CryptoTrendAtrMargin
from vnedge.strategy.fvg_liquidity_breakout import FvgLiquidityBreakoutScanner
from vnedge.strategy.funding_squeeze_continuation import FundingSqueezeContinuation
from vnedge.strategy.luxara_break_bounce_v27 import LuxaraBreakBounceV27Scanner
from vnedge.strategy.luxara_live_plan_qtm import LuxaraLivePlanQTMScanner
from vnedge.strategy.luxy_ut_bot_forecast import LuxyUTBotForecastScanner
from vnedge.strategy.panic_reversal import PanicReversal
from vnedge.strategy.quant_signal_pack import QuantSignalPack
from vnedge.strategy.quantified_fee_wall_sniper import QuantifiedFeeWallSniper
from vnedge.strategy.sats_5m_scalper import Sats5mScalper
from vnedge.strategy.scalper_1m import Scalper1m
from vnedge.strategy.smc_playbook_scalper import SMCPlaybookScalper
from vnedge.strategy.stealth_trail_bbp import StealthTrailBBPScanner
from vnedge.strategy.trend_continuation import TrendContinuation
from vnedge.strategy.trend_retest import TrendRetest
from vnedge.strategy.vnedge_algo_ml_pro import VNEDGEAlgoMLProScanner
from vnedge.strategy.vol_expansion_breakout import VolatilityExpansionBreakout
from vnedge.runtime.runner_config import RunnerConfig, RunnerMode
from vnedge.runtime.daily_factory import DailySignalFactoryConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LaneSpec:
    lane_id: str          # unique, e.g. "binance_funding_mr"
    exchange: str         # ccxt id, e.g. "binanceusdm" | "bybit"
    symbol: str
    timeframe: str = "1h"
    starting_equity: float = 500.0
    strategy_params: dict | None = None
    is_primary: bool = False   # the governed lane shown as the flat snapshot
    daily_loss_usd: float = 10.0
    mode: RunnerMode = RunnerMode.SHADOW
    strategy_id: str = "funding_mean_reversion_v1"
    #: Dynamic ATR-chandelier trail on the active-exit remainder (0.0 = off, the
    #: legacy arm-and-lock). Set per-lane when a strategy was JUDGED with a trail,
    #: so the running lane uses the exit its promotion evidence was measured on.
    trail_atr_mult: float = 0.0


@dataclass(frozen=True)
class _LaneRuntime:
    spec: LaneSpec
    session: LivePaperSession
    feed: LiveMarketFeed | RestPollingMarketFeed | SharedFeedView


# --- snapshot fan-in --------------------------------------------------------------

class _LaneSink:
    """A provider-shaped object one lane publishes to; tags + forwards."""

    def __init__(self, parent: "MultiLaneProvider", lane_id: str, exchange: str) -> None:
        self._parent, self._lane_id, self._exchange = parent, lane_id, exchange

    def publish(self, snapshot: dict) -> None:
        self._parent._publish(self._lane_id, self._exchange, snapshot)


class MultiLaneProvider:
    """Holds each lane's latest snapshot. latest() returns the primary lane
    (flat, backward-compatible) with a `lanes` array appended for comparison."""

    # Lane-health audit cadence: recompute at most every 5 minutes — the
    # audit reads file tails, so it must stay off the per-snapshot hot path.
    LANE_HEALTH_INTERVAL_SECONDS = 300.0

    def __init__(
        self,
        primary_lane_id: str,
        *,
        lane_specs: list[LaneSpec] | None = None,
        journal_dir: Path | None = None,
        runtime_control: dict | None = None,
    ) -> None:
        self.primary = primary_lane_id
        self._lanes: dict[str, dict] = {}
        self._order: list[str] = []
        # optional lane-health audit (desired-vs-active); off unless both
        # the desired spec list and the journal dir are provided
        self._health_specs = list(lane_specs) if lane_specs is not None else None
        self._health_journal_dir = Path(journal_dir) if journal_dir is not None else None
        self._health_cache: dict | None = None
        self._health_at = 0.0
        self._runtime_control = dict(runtime_control or {})

    def _lane_health(self) -> dict | None:
        """Cached desired-vs-active audit for the snapshot (never raises).

        The auditor cross-checks the configured spec list against the lane
        journal/equity files on disk; any exception is swallowed (keeping the
        previous result) — lane health must never take down the snapshot."""
        if self._health_specs is None or self._health_journal_dir is None:
            return None
        now = time.monotonic()
        if self._health_cache is not None and (
            now - self._health_at < self.LANE_HEALTH_INTERVAL_SECONDS
        ):
            return self._health_cache
        self._health_at = now  # even on failure: don't re-fail on every snapshot
        try:
            from vnedge.runtime.lane_health import audit_lanes

            report = audit_lanes(
                self._health_journal_dir, desired=self._health_specs
            )
            self._health_cache = report.to_snapshot()
        except Exception as exc:  # noqa: BLE001 — observability must not crash lanes
            logger.warning("lane-health audit failed: %s", exc)
        return self._health_cache

    def sink(self, lane_id: str, exchange: str) -> _LaneSink:
        return _LaneSink(self, lane_id, exchange)

    def _publish(self, lane_id: str, exchange: str, snapshot: dict) -> None:
        snap = dict(snapshot)
        snap["lane_id"] = lane_id
        snap["lane_exchange"] = exchange
        if lane_id not in self._lanes:
            self._order.append(lane_id)
        self._lanes[lane_id] = snap

    def publish_warming(self, lane_id: str, exchange: str, symbol: str) -> None:
        """(C) A placeholder published for every lane BEFORE it builds, so the
        dashboard shows the whole fleet immediately (each lane 'warming up')
        instead of a blank board until all builds finish. Overwritten by the
        lane's real snapshot as soon as it starts."""
        self._publish(lane_id, exchange, {
            "mode": "warming up",
            "symbol": symbol,
            "equity": 0.0,
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "fills": 0,
            "fees_usd": 0.0,
            "risk_status": "warming",
            "feed_health": {"candles": "warming", "last_update_ms": 0.0},
            "positions": [],
            "open_orders": [],
            "session": {},
        })

    def publish_error(
        self, lane_id: str, exchange: str, symbol: str, error: str
    ) -> None:
        self._publish(lane_id, exchange, {
            "mode": "shadow (live data)",
            "symbol": symbol,
            "equity": 0.0,
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "fills": 0,
            "fees_usd": 0.0,
            "risk_status": "lane_error",
            "feed_health": {"candles": "error", "last_update_ms": 0.0},
            "positions": [],
            "open_orders": [],
            "recent_alerts": [{
                "severity": "critical",
                "message": error,
                "rule_id": "lane_error",
            }],
            "session": {"lane_error": error},
        })

    def latest(self) -> dict | None:
        if not self._lanes:
            return None
        primary = self._lanes.get(self.primary) or self._lanes[self._order[0]]
        out = dict(primary)
        out["lanes"] = [
            {
                "lane_id": self._lanes[lid]["lane_id"],
                "exchange": self._lanes[lid]["lane_exchange"],
                "symbol": self._lanes[lid].get("symbol", ""),
                # per-lane mode + strategy so the dashboard lane matrix can
                # label paper vs shadow and which strategy each lane runs
                "mode": self._lanes[lid].get("mode", ""),
                "strategy_id": self._lanes[lid].get("strategy_id", ""),
                # per-lane last price + funding so the dashboard watchlist can
                # show real per-symbol quotes (one live price per lane symbol)
                "price": self._lanes[lid].get("price"),
                "funding_rate": self._lanes[lid].get("funding_rate"),
                "equity": self._lanes[lid].get("equity", 0.0),
                "realized_pnl": self._lanes[lid].get("realized_pnl", 0.0),
                "unrealized_pnl": self._lanes[lid].get("unrealized_pnl", 0.0),
                "fills": self._lanes[lid].get("fills", 0),
                "fees_usd": self._lanes[lid].get("fees_usd", 0.0),
                "risk_status": self._lanes[lid].get("risk_status", "?"),
                "feed": self._lanes[lid].get("feed_health", {}).get("candles", "?"),
                # transport + staleness so the dashboard connections panel can
                # show per-venue pipe health without new endpoints
                "feed_mode": self._lanes[lid].get("feed_health", {}).get("exchange", ""),
                "staleness_ms": self._lanes[lid].get("feed_health", {}).get("last_update_ms"),
                "positions": len(self._lanes[lid].get("positions", [])),
                # signal funnel: evaluated -> fired -> approved/submitted -> filled
                "funnel": {
                    "bars": self._lanes[lid].get("session", {}).get("bars_processed", 0),
                    "evals": self._lanes[lid].get("session", {}).get("evals", 0),
                    "live_evals": self._lanes[lid].get("session", {}).get("live_evals", 0),
                    "backfill_evals": self._lanes[lid].get("session", {}).get("backfill_evals", 0),
                    "signals": self._lanes[lid].get("session", {}).get("signals", 0),
                    "live_signals": self._lanes[lid].get("session", {}).get("live_signals", 0),
                    "backfill_signals": self._lanes[lid].get("session", {}).get("backfill_signals", 0),
                    "shadow_approved": self._lanes[lid].get("session", {}).get("shadow_approved", 0),
                    "shadow_rejected": self._lanes[lid].get("session", {}).get("shadow_rejected", 0),
                    "risk_rejects": self._lanes[lid].get("session", {}).get("risk_rejects", 0),
                    "sizing_skips": self._lanes[lid].get("session", {}).get("sizing_skips", 0),
                    "submitted": self._lanes[lid].get("session", {}).get("orders_submitted", 0),
                    "fills": self._lanes[lid].get("fills", 0),
                },
                # virtual performance of a shadow lane's approved intents
                # (resolved with backtester semantics; observability only)
                "shadow_perf": self._lanes[lid].get("session", {}).get("shadow_perf"),
                # latest strategy evaluation (features + thresholds) so the
                # lane matrix can explain WHY a lane is waiting/near trigger
                "last_eval": self._lanes[lid].get("session", {}).get("last_eval"),
                # cumulative funnel survives restarts (LaneFunnelStore); these
                # two let the dashboard show "last fired 2d ago · 4h bars"
                "last_fired_ts": self._lanes[lid].get("session", {}).get("last_fired_ts"),
                "timeframe": self._lanes[lid].get("session", {}).get("timeframe"),
                "why_no_trade": self._lanes[lid].get("session", {}).get("why_no_trade"),
                "daily_factory": self._lanes[lid].get("session", {}).get("daily_factory"),
                "trade_log": (self._lanes[lid].get("session", {}).get("trade_log") or [])[-10:],
                "trade_compatibility": _lane_trade_compatibility(self._lanes[lid]),
            }
            for lid in self._order if lid in self._lanes
        ]
        out["fleet"] = _fleet_aggregate(out["lanes"])
        health = self._lane_health()
        if health is not None:
            out["lane_health"] = health
        if self._runtime_control:
            out["runtime_control"] = dict(self._runtime_control)
        # A single inf/nan anywhere (e.g. a degenerate quote's spread_bps) makes
        # the whole snapshot fail JSON serialization — Starlette's JSONResponse
        # and websocket.send_json both use allow_nan=False — which 500s /state
        # and drops /ws, freezing the dashboard. Scrub non-finite floats to null.
        return _json_safe(out)


# --- lane construction ------------------------------------------------------------

def _lane_trade_compatibility(snapshot: dict) -> dict:
    """Operator-facing state: whether this lane is clean enough to discuss promotion.

    This does not grant permissions and never bypasses the gateway. It only
    prevents exploratory/negative shadow evidence from looking like a healthy
    paper/live candidate in the cockpit.
    """
    mode = str(snapshot.get("mode", "")).lower()
    risk_status = str(snapshot.get("risk_status", ""))
    feed = snapshot.get("feed_health", {})
    session = snapshot.get("session", {})
    shadow_perf = session.get("shadow_perf") or {}
    is_shadow = "shadow" in mode
    virtual_trades = int(shadow_perf.get("virtual_trades") or 0)
    virtual_net = float(shadow_perf.get("net_usd") or 0.0)
    if risk_status != "ok":
        state, reason = "BLOCKED", f"risk status {risk_status}"
    elif feed.get("candles") != "ok":
        state, reason = "BLOCKED", f"feed {feed.get('candles', '?')}"
    elif is_shadow and virtual_trades > 0 and virtual_net < 0:
        state, reason = "SHADOW_PROBATION", f"virtual net {virtual_net:+.2f} USD"
    elif is_shadow:
        state, reason = "SHADOW_ONLY", "observability only; no fills/live orders"
    else:
        state, reason = "PAPER_COMPATIBLE", "simulated fills through live-data path"
    return {
        "state": state,
        "reason": reason,
        "real_orders_allowed": False,
        "gateway_required": True,
        "journal_required": True,
    }

def _json_safe(obj):
    """Recursively replace non-finite floats (inf/-inf/nan) with None.

    Starlette serializes JSON with allow_nan=False, so one inf/nan anywhere in
    the published snapshot raises ValueError and 500s /state (or drops /ws) —
    which silently freezes the dashboard on stale data. Null is always safe.
    """
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {key: _json_safe(value) for key, value in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(value) for value in obj]
    return obj


def _fleet_aggregate(lanes: list[dict]) -> dict:
    """Portfolio-wide truth across the ACTIVELY-RUNNING lanes.

    The flat snapshot's ``equity`` is the primary lane only — a single lane's
    NAV must never masquerade as the whole bot (a green primary lane hiding a
    red fleet is exactly the honesty bug this fixes). This sums every running
    lane so the headline is the real book, and splits paper (deploys paper
    capital) from shadow (virtual/observation-only) so neither dilutes the
    other. Error lanes are excluded — a crashed lane is not a $0 account.
    """
    eq = start = realized = unrealized = fees = 0.0
    # paper_* = only the lanes that actually deploy capital. Shadow lanes hold a
    # static nominal account that never moves, so folding them into the headline
    # return dilutes it to ~0 and hides how the traded book is really doing.
    paper_eq = paper_start = 0.0
    paper_n = shadow_n = profitable = losing = 0
    shadow_net = 0.0
    shadow_trades = 0
    counted = 0
    for lane in lanes:
        if str(lane.get("risk_status") or "") == "lane_error":
            continue
        counted += 1
        e = float(lane.get("equity") or 0.0)
        r = float(lane.get("realized_pnl") or 0.0)
        u = float(lane.get("unrealized_pnl") or 0.0)
        f = float(lane.get("fees_usd") or 0.0)
        eq += e
        realized += r
        unrealized += u
        fees += f
        lane_start = e - r - u  # starting = equity - realized - unrealized
        start += lane_start
        if "shadow" in str(lane.get("mode") or "").lower():
            shadow_n += 1
            sp = lane.get("shadow_perf") or {}
            shadow_net += float(sp.get("net_usd") or 0.0)
            shadow_trades += int(sp.get("virtual_trades") or 0)
        else:
            paper_n += 1
            paper_eq += e
            paper_start += lane_start
        if r > 1e-9:
            profitable += 1
        elif r < -1e-9:
            losing += 1
    ret_pct = ((eq - start) / start * 100.0) if start > 1e-9 else 0.0
    paper_ret_pct = ((paper_eq - paper_start) / paper_start * 100.0) if paper_start > 1e-9 else 0.0
    return {
        "lanes": counted,
        "equity": round(eq, 2),
        "starting_equity": round(start, 2),
        "realized_pnl": round(realized, 2),
        "unrealized_pnl": round(unrealized, 2),
        "fees_usd": round(fees, 2),
        "return_pct": round(ret_pct, 3),
        "paper_lanes": paper_n,
        "shadow_lanes": shadow_n,
        # the traded book, undiluted by static shadow accounts
        "paper_equity": round(paper_eq, 2),
        "paper_starting_equity": round(paper_start, 2),
        "paper_return_pct": round(paper_ret_pct, 3),
        "shadow_virtual_net_usd": round(shadow_net, 2),
        "shadow_virtual_trades": shadow_trades,
        "profitable_lanes": profitable,
        "losing_lanes": losing,
    }


_FUNDING_HISTORY_REQUIRED = {"funding_mean_reversion_v1"}

# How far back the native Delta FUNDING: candle backfill reaches. 30 days of
# hourly prints covers the 240-bar percentile window (~10d) with room to
# spare, so a freshly built lane is warm on every bar of the candle warmup.
_DELTA_FUNDING_BACKFILL_DAYS = 30


def _requires_funding_history(strategy_id: str) -> bool:
    return strategy_id in _FUNDING_HISTORY_REQUIRED


async def _delta_funding_seed(spec: LaneSpec, fallback: pd.DataFrame) -> pd.DataFrame:
    """Backfill a funding seed from Delta's native ``FUNDING:`` candle API.

    Delta has no CCXT funding history, but its candle endpoint serves settled
    funding prints under the ``FUNDING:`` symbol prefix — seeding the
    percentile window at build time instead of a ~10-day live warmup.

    Failure posture: on ANY error (or an empty response) return ``fallback``
    — today's behaviour: empty seed, live accumulation behind the warmup
    mask. The backfill must never crash lane build.
    """
    from vnedge.data.delta_native_history import (
        DELTA_NATIVE_EXCHANGE_IDS,
        fetch_delta_funding_history,
    )

    if spec.exchange not in DELTA_NATIVE_EXCHANGE_IDS:
        return fallback
    try:
        seed = await fetch_delta_funding_history(
            spec.symbol, days=_DELTA_FUNDING_BACKFILL_DAYS
        )
    except Exception as exc:  # noqa: BLE001 — backfill is strictly best-effort
        logger.warning(
            "%s %s: native funding backfill failed (%s); lane keeps the "
            "live-accumulation warmup path",
            spec.exchange, spec.symbol, exc,
        )
        return fallback
    if seed.empty:
        logger.warning(
            "%s %s: native funding backfill returned no data; lane keeps the "
            "live-accumulation warmup path",
            spec.exchange, spec.symbol,
        )
        return fallback
    logger.info(
        "%s %s: seeded %d settled funding prints (%s -> %s) from the native "
        "FUNDING: candle API — percentile window warm from the start",
        spec.exchange, spec.symbol, len(seed),
        seed["timestamp"].iloc[0], seed["timestamp"].iloc[-1],
    )
    return seed


def _build_strategy(
    spec: LaneSpec, seed_funding, feed, *, funding_store_path=None
) -> BaseStrategy:
    """Construct the live strategy a lane runs, keyed by strategy_id."""
    params = spec.strategy_params or {}
    if spec.strategy_id == "signal_arbiter_v1":
        return _build_signal_arbiter_strategy(
            spec, seed_funding, feed, funding_store_path=funding_store_path
        )
    return _build_single_strategy(
        spec.strategy_id, params, seed_funding, feed,
        funding_store_path=funding_store_path,
    )


def _build_single_strategy(
    strategy_id: str,
    params: dict,
    seed_funding,
    feed,
    *,
    funding_store_path=None,
) -> BaseStrategy:
    """Construct one strategy implementation for a live-data lane."""
    if strategy_id == "funding_mean_reversion_v1":
        # No CCXT funding history (Delta) => the persistent accumulator owns
        # the series: seeded from the native FUNDING: candle backfill when
        # available (warm immediately), building live behind the warmup mask
        # otherwise; either way new prints keep appending to the fsync'd
        # store. Venues WITH history keep the proven REST-seeded path
        # unchanged (build_lane passes funding_store_path=None there).
        if funding_store_path is not None:
            from vnedge.runtime.funding_accumulator import LivePersistentFundingMR

            return LivePersistentFundingMR(
                seed_funding, feed, store_path=funding_store_path, **params
            )
        # needs the funding stream augmented live off the feed
        return LiveFundingMR(seed_funding, feed, **params)
    if strategy_id == "trend_continuation_v1":
        # candle-only; funding is a mild static filter (fine for a shadow lane)
        return TrendContinuation(seed_funding, **params)
    if strategy_id == "crypto_trend_atr_margin_v1":
        return CryptoTrendAtrMargin(seed_funding, **params)
    if strategy_id == "volatility_expansion_breakout_v1":
        return VolatilityExpansionBreakout(seed_funding, **params)
    if strategy_id == "panic_reversal_v1":
        return PanicReversal(seed_funding, **params)
    if strategy_id == "funding_squeeze_continuation_v1":
        return FundingSqueezeContinuation(seed_funding, **params)
    if strategy_id == "scalper_1m_v1":
        return Scalper1m(seed_funding, **params)
    if strategy_id == "alpha_stack_confluence_v1":
        return AlphaStackConfluence(seed_funding, **params)
    if strategy_id == "quant_signal_pack_v1":
        return QuantSignalPack(seed_funding, **params)
    if strategy_id == "sats_5m_scalper_v1":
        return Sats5mScalper(seed_funding, **params)
    if strategy_id == "stealth_trail_bbp_v1":
        return StealthTrailBBPScanner(seed_funding, **params)
    if strategy_id == "vnedge_algo_ml_pro_v1":
        return VNEDGEAlgoMLProScanner(seed_funding, **params)
    if strategy_id == "context_scalper_v2":
        return ContextScalperV2(seed_funding, **params)
    if strategy_id == "quantified_fee_wall_sniper_v1":
        return QuantifiedFeeWallSniper(seed_funding, **params)
    if strategy_id == "fvg_liquidity_breakout_v1":
        return FvgLiquidityBreakoutScanner(seed_funding, **params)
    if strategy_id == "luxy_ut_bot_forecast_v1":
        return LuxyUTBotForecastScanner(seed_funding, **params)
    if strategy_id == "luxara_live_plan_qtm_v1":
        return LuxaraLivePlanQTMScanner(seed_funding, **params)
    if strategy_id == "luxara_break_bounce_v27_v1":
        return LuxaraBreakBounceV27Scanner(seed_funding, **params)
    if strategy_id == "smc_playbook_scalper_v1":
        return SMCPlaybookScalper(seed_funding, **params)
    if strategy_id == "trend_retest_v1":
        return TrendRetest(seed_funding, **params)
    if strategy_id == "alpha_distillation_pack_v1":
        return AlphaDistillationPack(seed_funding, **params)
    if strategy_id == "vnedge_algo_ml_pro_v1":
        return VNEDGEAlgoMLProScanner(seed_funding, **params)
    raise ValueError(f"unsupported lane strategy_id: {strategy_id!r}")


def _build_signal_arbiter_strategy(
    spec: LaneSpec, seed_funding, feed, *, funding_store_path=None
) -> BaseStrategy:
    params = spec.strategy_params or {}
    children = params.get("strategies") or params.get("children")
    if not isinstance(children, list) or not children:
        raise ValueError("signal_arbiter_v1 requires a non-empty strategies list")

    strategies: list[BaseStrategy] = []
    candidate_defaults: dict[str, dict] = {}
    edge_keys = {
        "expected_edge_bps",
        "expected_cost_bps",
        "profit_factor",
        "confidence",
        "route",
        "planned_notional_usd",
        "metadata",
    }

    for index, child in enumerate(children):
        if not isinstance(child, dict):
            raise ValueError("signal_arbiter_v1 child entries must be objects")
        child_strategy_id = str(child.get("strategy_id", ""))
        if not child_strategy_id:
            raise ValueError("signal_arbiter_v1 child missing strategy_id")
        child_params = child.get("params", {})
        if not isinstance(child_params, dict):
            raise ValueError("signal_arbiter_v1 child params must be an object")

        strategies.append(
            _build_single_strategy(
                child_strategy_id, child_params, seed_funding, feed,
                funding_store_path=funding_store_path,
            )
        )
        default_source_id = f"{child_strategy_id}#{index + 1}"
        source_id = str(child.get("source_id", default_source_id))
        candidate_defaults[default_source_id] = {"source_id": source_id}
        candidate_defaults[source_id] = {
            key: child[key]
            for key in edge_keys
            if key in child
        }

    arbiter_params = params.get("arbiter", {})
    if not isinstance(arbiter_params, dict):
        raise ValueError("signal_arbiter_v1 arbiter config must be an object")
    return CompositeSignalStrategy(
        strategies,
        SignalArbiter(ArbiterConfig(**arbiter_params)),
        symbol=spec.symbol,
        candidate_defaults=candidate_defaults,
        strategy_id=spec.strategy_id,
    )


def _lane_risk_config(spec: LaneSpec, environ: Mapping[str, str] = os.environ) -> RiskConfig:
    """Per-lane risk config.

    Default is the LOCKED risk-based model (size from risk, halt on) — unchanged
    for every lane and, structurally, for the live path (multi-lane is
    paper/shadow only; the live trader builds its own config elsewhere).

    Opt-in PAPER-ONLY aggressive profile (env ``MULTI_LANE_FIXED_MARGIN`` > 0):
    fixed isolated-margin sizing (``$margin`` per trade, up to
    ``MULTI_LANE_FIXED_MARGIN_LEVERAGE`` — hard-capped at 30x, with leverage
    auto-reduced per-trade so the stop always fires before liquidation, so max
    loss per trade <= the margin) and the daily-loss halt off by default (the
    operator asked to remove it while paper). Reversible: unset the env var to
    return to risk-based sizing with the halt on.
    """
    margin_raw = environ.get("MULTI_LANE_FIXED_MARGIN", "").strip()
    margin_usd = 0.0
    if margin_raw:
        try:
            margin_usd = float(margin_raw)
        except ValueError:
            margin_usd = 0.0
    if margin_usd > 0:
        try:
            target_lev = int(float(environ.get("MULTI_LANE_FIXED_MARGIN_LEVERAGE", "30")))
        except ValueError:
            target_lev = 30
        target_lev = max(1, min(target_lev, ABSOLUTE_MAX_LEVERAGE))  # never above the hard cap
        halt_on = environ.get("MULTI_LANE_DAILY_LOSS_HALT", "0").lower() in ("1", "true", "yes", "on")
        cap = max(500.0, margin_usd * target_lev * 1.1)  # headroom for one full position
        return RiskConfig(
            max_daily_loss_usd=spec.daily_loss_usd,
            max_daily_loss_pct=2.0,
            daily_loss_halt_enabled=halt_on,
            fixed_margin_usd=margin_usd,
            max_leverage_per_position=target_lev,
            acknowledge_high_leverage=True,
            max_effective_account_leverage=10.0,
            max_exposure_per_symbol_usd=cap,
            max_total_exposure_usd=cap,
            max_open_positions=1,
        )
    return RiskConfig(max_daily_loss_usd=spec.daily_loss_usd, max_daily_loss_pct=2.0)


def _env_bool(environ: Mapping[str, str], key: str, default: bool = False) -> bool:
    raw = environ.get(key)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _lane_daily_factory_config(
    spec: LaneSpec, environ: Mapping[str, str] = os.environ
) -> DailySignalFactoryConfig:
    """Daily signal-factory policy for multi-lane paper/shadow.

    Global env keys apply to every lane; strategy-specific keys use the
    normalized strategy id, e.g.
    ``MULTI_LANE_DAILY_FACTORY_DAILY_SCALPER_PACK_V1_ENABLED=1``.
    """
    suffix = "".join(ch if ch.isalnum() else "_" for ch in spec.strategy_id.upper())

    def pick(name: str, default: str | None = None) -> str | None:
        specific = environ.get(f"MULTI_LANE_DAILY_FACTORY_{suffix}_{name}")
        if specific is not None:
            return specific
        return environ.get(f"MULTI_LANE_DAILY_FACTORY_{name}", default)

    def pick_bool(name: str, default: bool) -> bool:
        raw = pick(name)
        if raw is None or str(raw).strip() == "":
            return default
        return str(raw).strip().lower() in ("1", "true", "yes", "on")

    def pick_int(name: str, default: int) -> int:
        raw = pick(name)
        if raw is None or str(raw).strip() == "":
            return default
        try:
            return int(float(str(raw).strip()))
        except ValueError:
            return default

    def pick_float(name: str, default: float) -> float:
        raw = pick(name)
        if raw is None or str(raw).strip() == "":
            return default
        try:
            return float(str(raw).strip())
        except ValueError:
            return default

    enabled_default = _env_bool(environ, "MULTI_LANE_DAILY_FACTORY_ENABLED", False)
    timezone = str(pick("TIMEZONE", "UTC") or "UTC")
    return DailySignalFactoryConfig(
        enabled=pick_bool("ENABLED", enabled_default),
        session_timezone=timezone,
        entry_cutoff_minute=pick_int("ENTRY_CUTOFF_MINUTE", 22 * 60 + 30),
        force_flatten_minute=pick_int("FORCE_FLATTEN_MINUTE", 23 * 60 + 55),
        max_entries_per_day=pick_int("MAX_ENTRIES_PER_DAY", 3),
        daily_profit_target_usd=pick_float("DAILY_PROFIT_TARGET_USD", 0.0),
        cancel_resting_entries_at_cutoff=pick_bool("CANCEL_RESTING_ENTRIES", True),
        flatten_open_positions=pick_bool("FLATTEN_OPEN_POSITIONS", True),
    )


_TF_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "6h": 21_600_000, "1d": 86_400_000,
}
_CANDLE_COLUMNS = ("timestamp", "open", "high", "low", "close", "volume")


def _timeframe_ms(timeframe: str) -> int:
    return _TF_MS.get(str(timeframe).strip(), 3_600_000)  # default 1h


def _warmup_bars(environ: Mapping[str, str] = os.environ) -> int:
    """Warmup lookback in BARS (covers the longest indicator window with room);
    right-sizes the fetch per timeframe instead of a fixed 450h."""
    try:
        return max(50, int(environ.get("MULTI_LANE_WARMUP_BARS", "500")))
    except ValueError:
        return 500


#: Retry policy for a lane's warmup build — transient venue network/rate-limit
#: errors are common when the whole fleet warms up at once; a couple of backed-off
#: retries recover them instead of dropping the lane for the whole session.
_LANE_BUILD_RETRIES = 3
_LANE_BUILD_BACKOFF_S = 1.5


def _lane_build_concurrency(environ: Mapping[str, str] = os.environ) -> int:
    """How many lanes may warm up (hit the venue REST APIs) at once.

    Building all ~50 lanes simultaneously bursts each venue's instrument/candle
    endpoints and rate-limits a chunk of them into build failures. A small cap
    spreads the load so the fleet reliably comes up whole; env-tunable."""
    try:
        return max(1, int(environ.get("MULTI_LANE_BUILD_CONCURRENCY", "6")))
    except ValueError:
        return 6


async def _retry_transient(factory, *, retries: int, backoff_s: float, label: str):
    """Await ``factory()`` with bounded retries on transient venue errors.

    ccxt ``NetworkError`` (timeouts, rate limits, exchange-not-available) during
    warmup is transient — back off and retry rather than losing the lane for the
    session. Non-network errors (bad symbol, etc.) raise immediately."""
    last: Exception | None = None
    for attempt in range(retries):
        try:
            return await factory()
        except NetworkError as exc:
            last = exc
            if attempt + 1 < retries:
                logger.warning(
                    "transient venue error building %s (attempt %d/%d): %s; retrying",
                    label, attempt + 1, retries, exc,
                )
                await asyncio.sleep(backoff_s * (attempt + 1))
    assert last is not None
    raise last


def _load_candle_cache(cache_path: Path):
    """A lane's persisted candle window, or None if missing/unreadable/malformed.
    A moved or corrupt cache is never trusted — warmup refetches instead."""
    if not cache_path.exists():
        return None
    try:
        frame = pd.read_parquet(cache_path)
    except Exception as exc:  # noqa: BLE001 — a bad cache must never crash a lane
        logger.warning("candle cache unreadable at %s: %s — refetching", cache_path, exc)
        return None
    if frame.empty or not set(_CANDLE_COLUMNS).issubset(frame.columns):
        return None
    return frame


def _save_candle_cache(cache_path: Path, history) -> None:
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(".parquet.tmp")
        history.to_parquet(tmp, index=False)
        tmp.replace(cache_path)
    except Exception as exc:  # noqa: BLE001 — caching is best-effort, never fatal
        logger.warning("candle cache save failed for %s: %s", cache_path, exc)


def _closed_warmup_candles(frame, *, timeframe: str, until_ms: int):
    """Return only candles that were fully closed at ``until_ms``.

    CCXT OHLCV timestamps identify the *opening* instant of a candle and many
    venues include the currently-forming row in REST responses.  Seeding that
    row makes the first websocket/REST close look like a duplicate, delaying
    live evaluation by a whole timeframe.  Keep the causal contract explicit:
    an opening timestamp is warmup-eligible only after ``open + timeframe``.
    """
    if frame.empty:
        return frame
    closes_at = frame["timestamp"] + pd.to_timedelta(
        _timeframe_ms(timeframe), unit="ms"
    )
    cutoff = pd.to_datetime(until_ms, unit="ms", utc=True)
    return frame.loc[closes_at <= cutoff].reset_index(drop=True)


async def _warmup_candles(rest, spec: LaneSpec, cache_path: Path, since: int, until: int):
    """Normalized warmup candles for [since, until], reusing the persisted window
    and fetching only the gap since the last run. Any cache miss/shortfall/error
    falls back to a full REST fetch (so this is strictly a speedup, never a
    correctness change)."""
    cached = _load_candle_cache(cache_path)
    if cached is not None:
        last_ms = int(pd.Timestamp(cached["timestamp"].iloc[-1]).timestamp() * 1000)
        if last_ms >= since:  # the cache reaches back far enough to be useful
            frame = cached
            if last_ms + 1 < until:
                gap = normalize_candles(
                    await rest.fetch_candles(spec.symbol, spec.timeframe, last_ms + 1, until)
                )
                if not gap.empty:
                    frame = (
                        pd.concat([cached, gap], ignore_index=True)
                        .drop_duplicates(subset="timestamp", keep="last")
                        .sort_values("timestamp")
                        .reset_index(drop=True)
                    )
            cutoff = pd.to_datetime(since, unit="ms", utc=True)
            frame = frame[frame["timestamp"] >= cutoff].reset_index(drop=True)
            frame = _closed_warmup_candles(
                frame, timeframe=spec.timeframe, until_ms=until
            )
            _save_candle_cache(cache_path, frame)
            logger.info("lane %s warmup: cache + gap-fill (%d bars)", spec.lane_id, len(frame))
            return frame
    history = normalize_candles(
        await rest.fetch_candles(spec.symbol, spec.timeframe, since, until)
    )
    history = _closed_warmup_candles(
        history, timeframe=spec.timeframe, until_ms=until
    )
    _save_candle_cache(cache_path, history)
    return history


async def build_lane(
    spec: LaneSpec, provider: MultiLaneProvider, journal_dir: Path
) -> _LaneRuntime:
    """Seed warmup history + build an isolated LivePaperSession for one venue."""
    # (A) Bars-based warmup: fetch ~N bars for THIS timeframe, not a fixed 450h.
    # 5m lanes were pulling ~5,400 candles (many REST pages); ~500 bars is one
    # page and still covers the longest indicator window. Right-sizes per TF.
    warmup_bars = _warmup_bars()
    tf_ms = _timeframe_ms(spec.timeframe)
    until = int(time.time() * 1000)
    since = until - warmup_bars * tf_ms
    cache_path = journal_dir / f"{spec.lane_id}.candles.parquet"
    funding_history_unsupported = False
    async with CcxtPublicClient(spec.exchange) as rest:
        # (B) Cache + gap-fill: reuse the persisted candle window and fetch only
        # the gap since the last run; degrades to a full fetch on any cache miss.
        history = await _warmup_candles(rest, spec, cache_path, since, until)
        try:
            raw_f = await rest.fetch_funding_history(spec.symbol, since, until)
        except NotSupported:
            funding_history_unsupported = True
            if _requires_funding_history(spec.strategy_id):
                # No CCXT funding history (Delta). Don't fail the lane — try
                # the native FUNDING: candle backfill below; failing that,
                # build the window live from the funding stream and persist it.
                logger.info(
                    "%s %s: no CCXT funding history; %s will seed from the "
                    "native backfill or accumulate funding live",
                    spec.exchange, spec.symbol, spec.strategy_id,
                )
            else:
                logger.info(
                    "%s %s: funding history unavailable; running %s with zero funding",
                    spec.exchange, spec.symbol, spec.strategy_id,
                )
            raw_f = []
    seed_funding = normalize_funding(raw_f)
    if funding_history_unsupported and _requires_funding_history(spec.strategy_id):
        seed_funding = await _delta_funding_seed(spec, seed_funding)
    # The persistent accumulator (live samples + fsync'd store + warmup mask)
    # stays in charge whenever the venue can't REST-seed funding via CCXT —
    # the native backfill only SEEDS it. Venues with CCXT funding history
    # keep the proven REST-seeded LiveFundingMR path (store path None).
    funding_store_path = (
        journal_dir / f"{spec.lane_id}.funding.jsonl"
        if funding_history_unsupported or seed_funding.empty
        else None
    )

    # Shared-feed registry: lanes on the same (exchange, symbol, timeframe) —
    # e.g. the governed paper lane and its shadow twin — share ONE real feed;
    # each lane gets a view with its own closed-candle queue (fan-out, not
    # competition), and the last lane to stop tears the real feed down.
    feed = acquire_market_feed(spec.exchange, symbol=spec.symbol, timeframe=spec.timeframe)
    risk = _lane_risk_config(spec)
    daily_factory = _lane_daily_factory_config(spec)
    config = RunnerConfig(mode=spec.mode, symbol=spec.symbol,
                          timeframe=spec.timeframe,
                          starting_equity_usd=spec.starting_equity, risk=risk,
                          limits=venue_symbol_limits(spec.exchange, spec.symbol),
                          daily_factory=daily_factory,
                          trail_atr_mult=spec.trail_atr_mult)
    strategy = _build_strategy(
        spec, seed_funding, feed, funding_store_path=funding_store_path
    )
    exchange = SimulatedExchange(
        venue_fill_model(spec.exchange), config.starting_equity_usd)
    journal = DecisionJournal(journal_dir / f"{spec.lane_id}.journal.jsonl")
    kill = KillSwitch(kill_file=journal_dir / f"{spec.lane_id}.KILL")
    gateway = PreTradeRiskGateway(config.risk, kill)
    om = OrderManager(gateway, journal, PaperBroker(exchange))
    session = LivePaperSession(
        strategy, feed, history, config,
        gateway=gateway, order_manager=om, exchange=exchange, journal=journal,
        snapshot_provider=provider.sink(spec.lane_id, spec.exchange),
        account_store=PaperAccountStore(
            journal_dir / f"{spec.lane_id}.account.json", spec.lane_id),
        equity_history_path=journal_dir / f"{spec.lane_id}.equity.jsonl",
        fill_ledger=FillLedger(journal_dir / f"{spec.lane_id}.fills.jsonl"),
        funnel_store=LaneFunnelStore(
            journal_dir / f"{spec.lane_id}.funnel.json", spec.lane_id),
        trial_meta={"trial_id": spec.lane_id, "started": "2026-07-04",
                    "min_days": 14, "preferred_days": 30, "min_trades": 10,
                    "max_dd_pct": 6.0, "daily_stop_usd": spec.daily_loss_usd,
                    "promotion_source": spec.exchange,
                    "daily_factory": daily_factory.model_dump()},
    )
    # Expectations make a moved/edited store fail closed instead of injecting
    # a wrong-symbol position or absurd balance into the lane.
    resumed = session.account_store.restore_into(
        exchange, session.tracker,
        expected_symbol=spec.symbol,
        expected_starting_equity=spec.starting_equity,
    )
    if resumed:
        state = session.account_store.load() or {}
        session.restore_plan(state.get("plan"))
    # Resume the funnel counters so the live activity view doesn't reset to 0
    # on every deploy (display-only; never gates a trade).
    session.funnel_store.restore_into(session)
    logger.info("lane %s (%s %s %s %s) built; resumed=%s",
                spec.lane_id, spec.exchange, spec.symbol, spec.strategy_id,
                spec.mode.value, resumed)
    return _LaneRuntime(spec=spec, session=session, feed=feed)


class MultiLaneShadowRunner:
    def __init__(self, specs: list[LaneSpec], journal_dir: Path,
                 provider: MultiLaneProvider) -> None:
        self.specs = specs
        self.journal_dir = journal_dir
        self.provider = provider

    async def run(self, *, deadline_seconds: float | None = None) -> None:
        # (C) Show the whole fleet immediately as "warming up" while it builds,
        # so the dashboard is never a blank board mid-startup.
        for spec in self.specs:
            self.provider.publish_warming(spec.lane_id, spec.exchange, spec.symbol)
        # Bounded concurrency + transient-retry so the whole fleet comes up: a
        # naked gather over every lane bursts the venue APIs and rate-limits a
        # chunk of them into permanent build failures for the session.
        build_sem = asyncio.Semaphore(_lane_build_concurrency())

        async def _build(spec: LaneSpec) -> _LaneRuntime:
            async with build_sem:
                return await _retry_transient(
                    lambda: build_lane(spec, self.provider, self.journal_dir),
                    retries=_LANE_BUILD_RETRIES,
                    backoff_s=_LANE_BUILD_BACKOFF_S,
                    label=spec.lane_id,
                )

        results = await asyncio.gather(
            *[_build(s) for s in self.specs],
            return_exceptions=True,
        )
        runtimes: list[_LaneRuntime] = []
        for spec, result in zip(self.specs, results, strict=True):
            if isinstance(result, Exception):
                logger.error(
                    "lane %s (%s %s) failed to build: %s",
                    spec.lane_id, spec.exchange, spec.symbol, result,
                    exc_info=(type(result), result, result.__traceback__),
                )
                self.provider.publish_error(
                    spec.lane_id, spec.exchange, spec.symbol,
                    f"build failed: {result}",
                )
                continue
            runtimes.append(result)

        started: list[_LaneRuntime] = []
        for runtime in runtimes:
            try:
                await runtime.feed.start()
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "lane %s (%s %s) feed failed to start: %s",
                    runtime.spec.lane_id, runtime.spec.exchange,
                    runtime.spec.symbol, exc,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
                self.provider.publish_error(
                    runtime.spec.lane_id, runtime.spec.exchange,
                    runtime.spec.symbol, f"feed start failed: {exc}",
                )
                continue
            started.append(runtime)

        if not started:
            raise RuntimeError("no multi-lane shadow lanes started")

        logger.info("multi-lane shadow: %d/%d lanes running (%s)",
                    len(started), len(self.specs),
                    ", ".join(r.spec.lane_id for r in started))
        await asyncio.gather(*[
            self._run_lane(runtime, deadline_seconds=deadline_seconds)
            for runtime in started
        ])

    async def _run_lane(
        self, runtime: _LaneRuntime, *, deadline_seconds: float | None
    ) -> None:
        try:
            await runtime.session.run(deadline_seconds=deadline_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "lane %s (%s %s) stopped with error: %s",
                runtime.spec.lane_id, runtime.spec.exchange,
                runtime.spec.symbol, exc,
            )
            self.provider.publish_error(
                runtime.spec.lane_id, runtime.spec.exchange,
                runtime.spec.symbol, f"session failed: {exc}",
            )
        finally:
            try:
                await runtime.feed.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "lane %s feed stop failed: %s", runtime.spec.lane_id, exc
                )
