"""Multi-exchange lanes entry point.

    DASHBOARD_TOKEN=... python -m vnedge.runtime.multi_lane_shadow

Runs fully isolated lanes on Binance, Bybit, and Delta. Binance/Bybit run the
governed funding-MR paper/shadow lanes; Delta runs a candle-only trend shadow
lane plus a funding-MR shadow lane that accumulates funding live off its native
websocket (no REST funding history on Delta) and warms up until the percentile
window fills. All lanes use live public market data, $500 imaginary base, and
NO live orders.

  - PAPER  — simulated fills on live data; produces the live-$ venue
             comparison and keeps the human-approved funding_mr_btc_v1_20260703
             trial (Binance) + funding_mr_bybit_20260704 (Bybit) progressing
             toward their pre-registered verdicts, reusing their account files.
  - SHADOW — gateway-evaluated intents journaled per venue, never a fill;
             a pure signal/decision record independent of position state.

Neither mode ever submits to a real exchange. Modes/venues/symbols are env
driven (MULTI_LANE_MODES, MULTI_LANE_EXCHANGES, MULTI_LANE_SYMBOLS).

Set MULTI_LANE_PAPER_OBSERVE_ALL=1 (default OFF) to mirror shadow-only lanes
into isolated PAPER observation ledgers (`<lane_id>_paper_observation`). That is
still simulated execution on live data only — never a real order, never a
strategy promotion, and it never relaxes the live-order gates. Lanes that
already have a human-approved PAPER trial (the governed funding-MR trials) are
left untouched, not duplicated.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import signal
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from vnedge.runtime.multi_lane import (
    LaneSpec,
    MultiLaneProvider,
    MultiLaneShadowRunner,
)
from vnedge.runtime.runner_config import RunnerMode
from vnedge.research.strategy_evidence_registry import (
    DEFAULT_REGISTRY,
    strategy_authority_blockers,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Frozen funding-MR params — the human-approved manifest config (0.85 / 1.5).
FUNDING_MR_PARAMS = {
    "extreme_pct": 0.85,
    "z_entry": 1.5,
    "funding_pct_window": 240,
    "z_window": 48,
    "stop_atr_mult": 1.5,
}
TREND_PARAMS: dict = {}
SATS_5M_PARAMS: dict = {
    "min_tqi": 0.58,
    "min_quality_strength": 0.08,
    "min_momentum_persistence": 0.55,
    "min_bbp_atr": 0.10,
    "min_bbp_slope": -0.05,
    "stealth_trail_atr_mult": 2.5,
    "stealth_trail_reclaim_atr": 0.25,
    "min_volume_z": -0.75,
    "stop_atr_mult": 0.95,
    "take_profit_r": 3.0,
}
STEALTH_TRAIL_BBP_PARAMS: dict = {
    "min_expected_net_edge_bps": 25.0,
    "min_bbp_z": 0.20,
    "min_volume_z": 0.40,
    "min_body_atr": 0.45,
    "min_body_percentile": 0.60,
}
FVG_LIQUIDITY_BREAKOUT_PARAMS: dict = {
    "min_expected_net_edge_bps": 25.0,
    "min_room_to_liquidity_bps": 35.0,
    "min_volume_z": 0.35,
    "min_body_atr": 0.55,
    "min_body_percentile": 0.60,
}
CRYPTO_TREND_ATR_MARGIN_PARAMS: dict = {
    "fast_ema": 30,
    "slow_ema": 60,
    "atr_window": 60,
    "atr_margin_mult": 0.30,
    "stop_atr_mult": 1.60,
    "min_stop_bps": 15.0,
    # Entry-only in shadow: the research replay exited on neutral/reversal plus
    # max-hold. Paper promotion needs that exit parity before this lane owns PnL.
    "take_profit_r": None,
}
QUANTIFIED_FEE_WALL_SNIPER_PARAMS: dict = {
    "min_expected_net_edge_bps": 25.0,
    "min_room_to_liquidity_bps": 35.0,
    "min_quality_score": 0.58,
}
CONTEXT_SCALPER_V2_ALGO_PARAMS: dict = {
    "engine": "algo_ml",
    "min_expected_net_edge_bps": 25.0,
    "min_fill_probability": 0.50,
    "maker_fill_probability": 0.60,
    "taker_extra_buffer_bps": 5.0,
    "algo_use_ml_filter": True,
    "algo_ml_gate": 20.0,
}
CONTEXT_SCALPER_V2_STEALTH_PARAMS: dict = {
    "engine": "stealth",
    "min_expected_net_edge_bps": 25.0,
    "min_fill_probability": 0.50,
    "maker_fill_probability": 0.60,
    "taker_extra_buffer_bps": 5.0,
}
FEE_WALL_PAPER_PROBE_STRATEGIES = {
    "context_scalper_v2",
    "fvg_liquidity_breakout_v1",
    "luxara_live_plan_qtm_v1",
    "luxy_ut_bot_forecast_v1",
    "quantified_fee_wall_sniper_v1",
    "stealth_trail_bbp_v1",
}
FEE_WALL_PAPER_PROBE_VERDICTS = {"MAKER_EDGE", "MIXED_ROUTE_EDGE", "TAKER_EDGE"}
FEE_WALL_PAPER_PROBE_ACTION = "PRE_REGISTER_UNTOUCHED_JUDGMENT_WINDOW"

DEFAULT_PRIMARY_LANE_ID = "funding_mr_btc_v1_20260703"
DEFAULT_BYBIT_BTC_LANE_ID = "funding_mr_bybit_20260704"
DEFAULT_EXCHANGES = "binanceusdm,bybit,delta_india"
DELTA_EXCHANGE = "delta_india"

# The human-approved PAPER trials, keyed by (exchange, symbol). Their PAPER
# lane reuses these exact ids so it continues the governed trial (account
# files + pre-registered verdict). Shadow/other lanes get derived ids.
GOVERNED_PAPER_LANE_IDS = {
    ("binanceusdm", "BTC/USDT:USDT"): DEFAULT_PRIMARY_LANE_ID,
    ("bybit", "BTC/USDT:USDT"): DEFAULT_BYBIT_BTC_LANE_ID,
}

_MODES: dict[str, RunnerMode] = {
    "paper": RunnerMode.PAPER,
    "shadow": RunnerMode.SHADOW,
}

MANIFEST_RELOAD_EXIT_CODE = 75
DEFAULT_PAPER_GOVERNOR_PATH = "research/live_research/paper_lane_governor_latest.json"

# Exploratory candidate lanes surfaced by the edge-research agent — run in
# SHADOW only (gateway-evaluated intents journaled, NEVER a fill) so their live
# behaviour can be watched without committing capital. A rolling PASS on
# already-seen data is a candidate, NOT a promotion: each still needs a
# human-approved, pre-registered judgment on UNTOUCHED data before it could
# ever reach a paper lane. Toggle with MULTI_LANE_CANDIDATES=0.
#
# trend_continuation_v1 on XRP was the standout of the 2026-07-04 sweep:
# OOS +$92.20 (bybit) / +$80.25 (binance), 53-57 trades, but gate-REJECTED —
# so it is explicitly not promotable, only observable. Default trend config.
CANDIDATE_SHADOW_LANES = [
    LaneSpec(
        lane_id="trend_continuation_xrp_bybit_shadow",
        exchange="bybit", symbol="XRP/USDT:USDT",
        strategy_id="trend_continuation_v1",
        strategy_params={},          # default trend_continuation_v1 config
        mode=RunnerMode.SHADOW,
    ),
]


def crypto_trend_doge_shadow_lanes(
    environ: Mapping[str, str] = os.environ,
) -> list[LaneSpec]:
    """Source-backed DOGE 1h trend/ATR-margin lane from the Pine top-100 batch.

    The corresponding research proxy was the only accessible top-100
    TradingView source experiment that survived chronological OOS sanity on the
    2026-08-01 local replay: DOGEUSDT 1h, +66.55bps/trade full sample, OOS
    +125.96bps/trade with PF 2.23 over 33 trades. Still SHADOW only: this is
    live firing evidence, not paper/live promotion.
    """
    if not _truthy(environ, "MULTI_LANE_CRYPTO_TREND_DOGE", "1"):
        return []
    if "binanceusdm" not in _csv_env("MULTI_LANE_EXCHANGES", DEFAULT_EXCHANGES, environ):
        return []
    lanes = [
        LaneSpec(
            lane_id="crypto_trend_doge_binanceusdm_shadow",
            exchange="binanceusdm",
            symbol="DOGE/USDT:USDT",
            timeframe="1h",
            strategy_id="crypto_trend_atr_margin_v1",
            strategy_params=CRYPTO_TREND_ATR_MARGIN_PARAMS,
            mode=RunnerMode.SHADOW,
            # Same trail as the paper twin so shadow predicts paper/live, not the
            # legacy fixed-stop exit (now that ShadowOutcomeTracker runs the trail).
            trail_atr_mult=3.0,
        )
    ]
    # PAPER trial — human-approved 2026-08-03 after a pre-registered untouched
    # PASS (prereg_20260803: DOGE 1h 2024-01-01→2025-07-03, +$191.5 OOS, standard
    # gates). Runs the EXACT judged exit: active-exit + ATR trail 3× (trail_atr_mult
    # on the RunnerConfig). Simulated fills only — no live orders. Toggle off with
    # MULTI_LANE_CRYPTO_TREND_DOGE_PAPER=0.
    if _truthy(environ, "MULTI_LANE_CRYPTO_TREND_DOGE_PAPER", "1"):
        lanes.append(
            LaneSpec(
                lane_id="crypto_trend_doge_binanceusdm_paper",
                exchange="binanceusdm",
                symbol="DOGE/USDT:USDT",
                timeframe="1h",
                strategy_id="crypto_trend_atr_margin_v1",
                strategy_params=CRYPTO_TREND_ATR_MARGIN_PARAMS,
                mode=RunnerMode.PAPER,
                trail_atr_mult=3.0,
            )
        )
    return lanes


def _truthy(environ: Mapping[str, str], name: str, default: str) -> bool:
    return environ.get(name, default).lower() in ("1", "true", "yes", "on")


def manifest_shadow_lanes(environ: Mapping[str, str] = os.environ) -> list[LaneSpec]:
    """Shadow lanes auto-generated from research winners (shadow_lanes.json).

    ON by default (MULTI_LANE_MANIFEST_ENABLED=0 opts out): only lanes with
    human-locked params reach the manifest; all are shadow-only, can_trade=false
    research candidates. Paper/live promotion still needs explicit human
    approval and untouched-data judgment.
    """
    if not _truthy(environ, "MULTI_LANE_MANIFEST_ENABLED", "1"):
        return []
    from vnedge.research.shadow_manifest import load_shadow_manifest
    out_dir = Path(environ.get("MULTI_LANE_RESEARCH_DIR", "research/live_research"))
    specs: list[LaneSpec] = []
    for lane in load_shadow_manifest(out_dir).get("lanes", []):
        try:
            specs.append(LaneSpec(
                lane_id=lane["lane_id"], exchange=lane["exchange"],
                symbol=lane["symbol"], timeframe=lane.get("timeframe", "1h"),
                strategy_id=lane["strategy_id"],
                strategy_params=lane.get("strategy_params") or {},
                mode=RunnerMode.SHADOW,
            ))
        except (KeyError, TypeError):
            continue
    return specs


def _lane_identity(spec: LaneSpec) -> tuple[str, str, str, str, RunnerMode]:
    """Semantic lane key; protects against duplicate ids for the same lane."""
    return (
        spec.exchange,
        spec.symbol,
        spec.timeframe,
        spec.strategy_id,
        spec.mode,
    )


def dedupe_lane_specs(specs: list[LaneSpec]) -> list[LaneSpec]:
    """Preserve first occurrence, dropping duplicate ids or semantic twins."""
    out: list[LaneSpec] = []
    seen_ids: set[str] = set()
    seen_identities: set[tuple[str, str, str, str, RunnerMode]] = set()
    for spec in specs:
        identity = _lane_identity(spec)
        if spec.lane_id in seen_ids or identity in seen_identities:
            logger.info(
                "skipping duplicate lane %s (%s %s %s %s)",
                spec.lane_id, spec.exchange, spec.symbol,
                spec.strategy_id, spec.mode.value,
            )
            continue
        out.append(spec)
        seen_ids.add(spec.lane_id)
        seen_identities.add(identity)
    return out


def candidate_shadow_lanes(environ: Mapping[str, str] = os.environ) -> list[LaneSpec]:
    if not _truthy(environ, "MULTI_LANE_CANDIDATES", "1"):
        return []
    return dedupe_lane_specs(list(CANDIDATE_SHADOW_LANES) + manifest_shadow_lanes(environ))


def _strategy_params_key(params: dict | None) -> str:
    return json.dumps(params or {}, sort_keys=True, separators=(",", ":"), default=str)


def _paper_identity(spec: LaneSpec) -> tuple[str, str, str, str, str]:
    """Identity of a lane's simulated-paper twin — mode-agnostic, params-aware.

    Distinct from ``_lane_identity`` (which keys on mode to keep paper/shadow of
    the same lane apart): this collapses mode so a shadow lane can be matched
    against an EXISTING paper lane of the same strategy/venue/symbol/params — the
    signal we use to leave the governed paper trials untouched.
    """
    return (
        spec.exchange,
        spec.symbol,
        spec.timeframe,
        spec.strategy_id,
        _strategy_params_key(spec.strategy_params),
    )


def _paper_observation_lane_id(spec: LaneSpec) -> str:
    if spec.lane_id.endswith("_shadow"):
        return f"{spec.lane_id.removesuffix('_shadow')}_paper_observation"
    return f"{spec.lane_id}_paper_observation"


def paper_observation_lanes(
    specs: list[LaneSpec],
    environ: Mapping[str, str] = os.environ,
) -> list[LaneSpec]:
    """Mirror shadow-only lanes into isolated PAPER observation ledgers.

    OFF by default (MULTI_LANE_PAPER_OBSERVE_ALL=1 opts in). Deliberately
    separate from governed paper-trial promotion:

    - If an equivalent PAPER lane already exists (the human-approved BTC/Bybit
      funding-MR trials), it is NOT duplicated — the governed trial stays
      canonical on its own account files.
    - Each mirror gets a `<lane_id>_paper_observation` id, so its account and
      journal files can never collide with the trial or shadow ledgers.
    - PAPER remains simulated fills routed through the same gateway pipeline;
      no live order or credential path is added.

    Delta India is included by default here (it is still a simulated exchange in
    PAPER mode); operators can drop it with MULTI_LANE_DELTA_PAPER_OBSERVE=0.
    """
    if not _truthy(environ, "MULTI_LANE_PAPER_OBSERVE_ALL", "0"):
        return []

    allow_delta = _truthy(environ, "MULTI_LANE_DELTA_PAPER_OBSERVE", "1")
    existing_ids = {spec.lane_id for spec in specs}
    paper_identities = {
        _paper_identity(spec) for spec in specs if spec.mode is RunnerMode.PAPER
    }
    mirrors: list[LaneSpec] = []
    for spec in specs:
        if spec.mode is not RunnerMode.SHADOW:
            continue
        if spec.lane_id.startswith("velocity_"):
            # velocity lanes are ML-training only and must NEVER be mirrored to a
            # paper ledger — they are not, and must not look like, paper
            # candidates (see velocity_delta_lanes).
            continue
        if spec.exchange == DELTA_EXCHANGE and not allow_delta:
            continue
        identity = _paper_identity(spec)
        if identity in paper_identities:
            # an equivalent human-approved PAPER trial already runs this lane
            continue
        lane_id = _paper_observation_lane_id(spec)
        if lane_id in existing_ids:
            continue
        mirrors.append(replace(
            spec,
            lane_id=lane_id,
            mode=RunnerMode.PAPER,
            is_primary=False,
        ))
        existing_ids.add(lane_id)
        paper_identities.add(identity)
    return mirrors


def delta_funding_mr_lanes(environ: Mapping[str, str] = os.environ) -> list[LaneSpec]:
    """Delta India funding-MR — SHADOW only, accumulating funding live.

    Delta exposes no REST funding history, so this lane builds the percentile
    window purely from live funding observations (persisted across restarts via
    the funding accumulator) and warms up — emitting NO signal — until the
    window fills. Same frozen human-approved params as the governed lanes; no
    fills, $500 imaginary base. Toggle with MULTI_LANE_DELTA_FUNDING_MR=0.
    """
    if not _truthy(environ, "MULTI_LANE_DELTA_FUNDING_MR", "1"):
        return []
    if DELTA_EXCHANGE not in _csv_env(
        "MULTI_LANE_EXCHANGES", DEFAULT_EXCHANGES, environ
    ):
        return []
    symbol = "BTC/USD:USD"
    return [
        LaneSpec(
            lane_id=f"funding_mr_{DELTA_EXCHANGE}_{_slug_symbol(symbol)}_shadow",
            exchange=DELTA_EXCHANGE,
            symbol=symbol,
            timeframe=environ.get("MULTI_LANE_TIMEFRAME", "1h"),
            strategy_id="funding_mean_reversion_v1",
            strategy_params=FUNDING_MR_PARAMS,
            mode=RunnerMode.SHADOW,
        )
    ]


def sats_5m_delta_lanes(environ: Mapping[str, str] = os.environ) -> list[LaneSpec]:
    """Curated 5m Delta India SATS-style scalper observation lanes.

    These are SHADOW-only by default: the point is to collect live signal and
    virtual-outcome evidence for the 5m quality-trend setup operators compare
    against chart indicators. Existing PAPER observation mirroring can create
    isolated simulated ledgers, but these lanes are not promoted paper trials.
    """
    if not _truthy(environ, "MULTI_LANE_SATS_5M_DELTA", "1"):
        return []
    if DELTA_EXCHANGE not in _csv_env("MULTI_LANE_EXCHANGES", DEFAULT_EXCHANGES, environ):
        return []
    raw_symbols = _csv_env(
        "MULTI_LANE_SATS_5M_SYMBOLS",
        "ETH/USDT:USDT,BTC/USDT:USDT,SOL/USDT:USDT,XRP/USDT:USDT",
        environ,
    )
    symbols = [_delta_india_symbol(symbol) for symbol in raw_symbols]
    return [
        LaneSpec(
            lane_id=f"sats_5m_scalper_{DELTA_EXCHANGE}_{_slug_symbol(symbol)}_shadow",
            exchange=DELTA_EXCHANGE,
            symbol=symbol,
            timeframe="5m",
            strategy_id="sats_5m_scalper_v1",
            strategy_params=SATS_5M_PARAMS,
            mode=RunnerMode.SHADOW,
        )
        for symbol in symbols
    ]


def stealth_trail_bbp_delta_lanes(
    environ: Mapping[str, str] = os.environ,
) -> list[LaneSpec]:
    """Curated 5m Delta India human-fingerprint scanner observation lanes.

    These run the stricter BBP + stealth-trail + 15m/1h confirmation scanner in
    SHADOW only. The strategy reason string carries the taker fallback verdict,
    but no paper/live promotion is implied here.
    """
    if not _truthy(environ, "MULTI_LANE_STEALTH_TRAIL_BBP_DELTA", "1"):
        return []
    if DELTA_EXCHANGE not in _csv_env("MULTI_LANE_EXCHANGES", DEFAULT_EXCHANGES, environ):
        return []
    raw_symbols = _csv_env(
        "MULTI_LANE_STEALTH_TRAIL_BBP_SYMBOLS",
        "ETH/USDT:USDT,BTC/USDT:USDT,SOL/USDT:USDT,XRP/USDT:USDT",
        environ,
    )
    symbols = [_delta_india_symbol(symbol) for symbol in raw_symbols]
    return [
        LaneSpec(
            lane_id=f"stealth_trail_bbp_{DELTA_EXCHANGE}_{_slug_symbol(symbol)}_shadow",
            exchange=DELTA_EXCHANGE,
            symbol=symbol,
            timeframe="5m",
            strategy_id="stealth_trail_bbp_v1",
            strategy_params=STEALTH_TRAIL_BBP_PARAMS,
            mode=RunnerMode.SHADOW,
        )
        for symbol in symbols
    ]


def fvg_liquidity_breakout_delta_lanes(
    environ: Mapping[str, str] = os.environ,
) -> list[LaneSpec]:
    """Curated 5m Delta India FVG/liquidity breakout observation lanes.

    These are the main-line FVG lanes: 5m trigger, causal 15m confirmation, 1h
    bias, room-to-liquidity, and a strict 25 bps expected-net edge floor. They
    run SHADOW by default and can be mirrored into isolated PAPER observation
    ledgers with MULTI_LANE_PAPER_OBSERVE_ALL=1, but they are not promoted
    governed paper trials until untouched-window evidence clears.
    """
    if not _truthy(environ, "MULTI_LANE_FVG_LIQUIDITY_DELTA", "1"):
        return []
    if DELTA_EXCHANGE not in _csv_env("MULTI_LANE_EXCHANGES", DEFAULT_EXCHANGES, environ):
        return []
    raw_symbols = _csv_env(
        "MULTI_LANE_FVG_LIQUIDITY_SYMBOLS",
        "ETH/USDT:USDT,BTC/USDT:USDT,SOL/USDT:USDT,XRP/USDT:USDT",
        environ,
    )
    symbols = [_delta_india_symbol(symbol) for symbol in raw_symbols]
    return [
        LaneSpec(
            lane_id=(
                f"fvg_liquidity_breakout_{DELTA_EXCHANGE}_"
                f"{_slug_symbol(symbol)}_shadow"
            ),
            exchange=DELTA_EXCHANGE,
            symbol=symbol,
            timeframe="5m",
            strategy_id="fvg_liquidity_breakout_v1",
            strategy_params=FVG_LIQUIDITY_BREAKOUT_PARAMS,
            mode=RunnerMode.SHADOW,
        )
        for symbol in symbols
    ]


def context_scalper_v2_delta_lanes(
    environ: Mapping[str, str] = os.environ,
) -> list[LaneSpec]:
    """Curated 5m Delta India context-scalper v2 observation lanes.

    ETH uses the VNEDGE Algo ML Pro engine because the latest fee-wall scan
    found the strongest ETH watchlist rows there. XRP uses the Stealth/BBP
    engine because that family produced the strongest XRP watchlist rows.
    Both remain SHADOW-only unless the explicit paper observation mirror is
    enabled; no promotion or live permission is implied.
    """
    if not _truthy(environ, "MULTI_LANE_CONTEXT_SCALPER_V2_DELTA", "1"):
        return []
    if DELTA_EXCHANGE not in _csv_env("MULTI_LANE_EXCHANGES", DEFAULT_EXCHANGES, environ):
        return []
    raw_symbols = _csv_env(
        "MULTI_LANE_CONTEXT_SCALPER_V2_SYMBOLS",
        "ETH/USDT:USDT,XRP/USDT:USDT",
        environ,
    )
    symbols = [_delta_india_symbol(symbol) for symbol in raw_symbols]
    specs: list[LaneSpec] = []
    for symbol in symbols:
        base = symbol.split("/", maxsplit=1)[0].upper()
        params = (
            CONTEXT_SCALPER_V2_STEALTH_PARAMS
            if base == "XRP"
            else CONTEXT_SCALPER_V2_ALGO_PARAMS
        )
        specs.append(
            LaneSpec(
                lane_id=(
                    f"context_scalper_v2_{DELTA_EXCHANGE}_"
                    f"{_slug_symbol(symbol)}_shadow"
                ),
                exchange=DELTA_EXCHANGE,
                symbol=symbol,
                timeframe="5m",
                strategy_id="context_scalper_v2",
                strategy_params=params,
                mode=RunnerMode.SHADOW,
            )
        )
    return specs


def luxara_live_plan_qtm_delta_lanes(
    environ: Mapping[str, str] = os.environ,
) -> list[LaneSpec]:
    """Curated 5m Delta India Luxara live-plan QTM observation lanes.

    The Pine-derived ATR live-plan scanner (trailing-stop-first, BE-after-TP1)
    in SHADOW only. Runs on the loosened selective gates (30 bps expected net
    edge, 50 bps room-to-liquidity) so it accumulates real firing evidence
    without being silent; promotion still requires untouched-window judgment.
    """
    if not _truthy(environ, "MULTI_LANE_LUXARA_LIVE_PLAN_DELTA", "1"):
        return []
    if DELTA_EXCHANGE not in _csv_env("MULTI_LANE_EXCHANGES", DEFAULT_EXCHANGES, environ):
        return []
    raw_symbols = _csv_env(
        "MULTI_LANE_LUXARA_LIVE_PLAN_SYMBOLS",
        "ETH/USDT:USDT,BTC/USDT:USDT,SOL/USDT:USDT,XRP/USDT:USDT",
        environ,
    )
    symbols = [_delta_india_symbol(symbol) for symbol in raw_symbols]
    return [
        LaneSpec(
            lane_id=(
                f"luxara_live_plan_qtm_{DELTA_EXCHANGE}_"
                f"{_slug_symbol(symbol)}_shadow"
            ),
            exchange=DELTA_EXCHANGE,
            symbol=symbol,
            timeframe="5m",
            strategy_id="luxara_live_plan_qtm_v1",
            strategy_params={},
            mode=RunnerMode.SHADOW,
        )
        for symbol in symbols
    ]


def luxara_break_bounce_v27_delta_lanes(
    environ: Mapping[str, str] = os.environ,
) -> list[LaneSpec]:
    """Curated 5m Delta India Luxara Break & Bounce v27 observation lanes.

    The Pine-derived box-breakout/bounce scanner in SHADOW only, on the loosened
    selective gates (1.5x volume, grade >= 3, 8 bps breakout). The expected-net
    edge floor is aligned to the other Delta scalper lanes (25 bps) so it fires
    on the free-exit BTC/ETH scalper venue; promotion still requires untouched-
    window judgment.
    """
    if not _truthy(environ, "MULTI_LANE_LUXARA_BREAK_BOUNCE_DELTA", "1"):
        return []
    if DELTA_EXCHANGE not in _csv_env("MULTI_LANE_EXCHANGES", DEFAULT_EXCHANGES, environ):
        return []
    raw_symbols = _csv_env(
        "MULTI_LANE_LUXARA_BREAK_BOUNCE_SYMBOLS",
        "ETH/USDT:USDT,BTC/USDT:USDT,SOL/USDT:USDT,XRP/USDT:USDT",
        environ,
    )
    symbols = [_delta_india_symbol(symbol) for symbol in raw_symbols]
    return [
        LaneSpec(
            lane_id=(
                f"luxara_break_bounce_v27_{DELTA_EXCHANGE}_"
                f"{_slug_symbol(symbol)}_shadow"
            ),
            exchange=DELTA_EXCHANGE,
            symbol=symbol,
            timeframe="5m",
            strategy_id="luxara_break_bounce_v27_v1",
            strategy_params={"min_expected_net_edge_bps": 25.0},
            mode=RunnerMode.SHADOW,
        )
        for symbol in symbols
    ]


def fee_wall_paper_probe_lanes(
    environ: Mapping[str, str] = os.environ,
) -> list[LaneSpec]:
    """Promote strict fee-wall candidates into isolated live-data PAPER probes.

    This is deliberately weaker than a governed paper trial and stronger than a
    chart screenshot:

    - enabled only by ``MULTI_LANE_FEE_WALL_PAPER_PROBES=1``;
    - reads the latest research artifact and accepts only strict fee-wall rows;
    - writes separate ``*_paper_probe`` ledgers, never the governed trial ids;
    - keeps real orders impossible because the multi-lane runner mounts only the
      simulated PaperBroker.

    The artifact can still say ``can_promote=false`` because these probes are
    live-market sample expansion, not a live-capital promotion.
    """
    if not _truthy(environ, "MULTI_LANE_FEE_WALL_PAPER_PROBES", "0"):
        return []
    manifest_path = Path(
        environ.get(
            "MULTI_LANE_FEE_WALL_PAPER_PROBES_PATH",
            "research/live_research/fee_wall_paper_probes.json",
        )
    )
    source_is_manifest = manifest_path.exists()
    path = manifest_path if source_is_manifest else Path(
        environ.get(
            "MULTI_LANE_FEE_WALL_FORENSICS_PATH",
            "research/live_research/fee_wall_forensics_latest.json",
        )
    )
    try:
        payload = json.loads(path.read_text())
    except FileNotFoundError:
        logger.warning("fee-wall paper probes requested but %s is missing", path)
        return []
    except json.JSONDecodeError as exc:
        logger.warning("fee-wall paper probes requested but %s is invalid: %s", path, exc)
        return []

    if not source_is_manifest and _artifact_is_stale(payload, environ):
        logger.warning(
            "fee-wall paper probes skipped: %s generated_at=%r is stale",
            path, payload.get("generated_at"),
        )
        return []

    min_routed = int(environ.get("MULTI_LANE_FEE_WALL_PROBE_MIN_ROUTED", "10"))
    min_avg_net_bps = float(
        environ.get("MULTI_LANE_FEE_WALL_PROBE_MIN_AVG_NET_BPS", "8.0")
    )
    min_profit_factor = float(
        environ.get("MULTI_LANE_FEE_WALL_PROBE_MIN_PROFIT_FACTOR", "1.15")
    )

    specs: list[LaneSpec] = []
    for candidate in _paper_probe_candidates(payload):
        if not _candidate_ok_for_paper_probe(
            candidate,
            min_routed=min_routed,
            min_avg_net_bps=min_avg_net_bps,
            min_profit_factor=min_profit_factor,
        ):
            continue
        try:
            exchange = str(candidate["exchange"])
            symbol = str(candidate["symbol"])
            timeframe = str(candidate["timeframe"])
            strategy_id = str(candidate["strategy"])
        except KeyError:
            continue
        if exchange == DELTA_EXCHANGE:
            symbol = _delta_india_symbol(symbol)
        specs.append(
            LaneSpec(
                lane_id=_fee_wall_probe_lane_id(
                    exchange, symbol, timeframe, strategy_id
                ),
                exchange=exchange,
                symbol=symbol,
                timeframe=timeframe,
                strategy_id=strategy_id,
                strategy_params=_default_params_for_strategy(strategy_id),
                mode=RunnerMode.PAPER,
                is_primary=False,
            )
        )
    return dedupe_lane_specs(specs)


def _paper_probe_candidates(payload: Mapping[str, object]) -> list[Mapping[str, object]]:
    raw = payload.get("paper_probes")
    if raw is None:
        raw = payload.get("strict_fee_wall_candidates")
    if not isinstance(raw, list):
        return []
    return [row for row in raw if isinstance(row, Mapping)]


def _artifact_is_stale(
    payload: Mapping[str, object], environ: Mapping[str, str]
) -> bool:
    raw_hours = environ.get("MULTI_LANE_FEE_WALL_PROBE_MAX_AGE_HOURS", "72")
    try:
        max_age_hours = float(raw_hours)
    except ValueError:
        max_age_hours = 72.0
    if max_age_hours <= 0:
        return False
    generated_at = payload.get("generated_at")
    if not isinstance(generated_at, str) or not generated_at:
        return True
    try:
        ts = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return (datetime.now(UTC) - ts.astimezone(UTC)).total_seconds() > max_age_hours * 3600


def _candidate_ok_for_paper_probe(
    candidate: Mapping[str, object],
    *,
    min_routed: int,
    min_avg_net_bps: float,
    min_profit_factor: float,
) -> bool:
    strategy_id = str(candidate.get("strategy", ""))
    verdict = str(candidate.get("verdict", ""))
    action = str(candidate.get("recommended_action", ""))
    try:
        routed = int(candidate.get("routed") or candidate.get("opportunities") or 0)
        avg_net = float(candidate.get("avg_selected_net_bps"))
        profit_factor = float(candidate.get("profit_factor"))
    except (TypeError, ValueError):
        return False
    return (
        strategy_id in FEE_WALL_PAPER_PROBE_STRATEGIES
        and verdict in FEE_WALL_PAPER_PROBE_VERDICTS
        and action == FEE_WALL_PAPER_PROBE_ACTION
        and routed >= min_routed
        and avg_net >= min_avg_net_bps
        and profit_factor >= min_profit_factor
    )


def _fee_wall_probe_lane_id(
    exchange: str, symbol: str, timeframe: str, strategy_id: str
) -> str:
    strategy_slug = strategy_id.removesuffix("_v1")
    return (
        f"fee_wall_{strategy_slug}_{exchange}_{_slug_symbol(symbol)}_"
        f"{timeframe}_paper_probe"
    )


def _csv_env(name: str, default: str, environ: Mapping[str, str]) -> list[str]:
    raw = environ.get(name, default)
    return [part.strip() for part in raw.split(",") if part.strip()]


def _governor_paper_roster_filter(
    specs: list[LaneSpec],
    environ: Mapping[str, str] = os.environ,
) -> list[LaneSpec]:
    """Optionally keep PAPER mode aligned to the governor's bounded roster."""

    if not _truthy(environ, "MULTI_LANE_GOVERNOR_PAPER_ROSTER_ONLY", "0"):
        return specs
    path = Path(environ.get("MULTI_LANE_PAPER_GOVERNOR_PATH", DEFAULT_PAPER_GOVERNOR_PATH))
    try:
        payload = json.loads(path.read_text())
    except FileNotFoundError:
        logger.warning(
            "paper governor roster filter enabled but %s is missing; keeping paper specs",
            path,
        )
        return specs
    except json.JSONDecodeError as exc:
        logger.warning(
            "paper governor roster filter enabled but %s is invalid: %s; keeping paper specs",
            path,
            exc,
        )
        return specs
    proposed = payload.get("proposed_roster")
    if not isinstance(proposed, Mapping):
        logger.warning(
            "paper governor roster filter enabled but %s has no proposed_roster; "
            "keeping paper specs",
            path,
        )
        return specs
    proposed_paper = proposed.get("paper_lanes")
    allowed = _governor_paper_allowed_keys(proposed_paper)
    if not allowed:
        logger.warning(
            "paper governor roster filter enabled but %s proposes no paper_lanes; "
            "keeping paper specs",
            path,
        )
        return specs

    filtered: list[LaneSpec] = []
    removed = 0
    for spec in specs:
        if spec.mode is not RunnerMode.PAPER or _paper_spec_allowed(spec, allowed):
            filtered.append(spec)
        else:
            removed += 1
    filtered_paper = [spec for spec in filtered if spec.mode is RunnerMode.PAPER]
    existing_keys = {
        spec.lane_id for spec in filtered_paper
    } | {
        _paper_signature(spec.strategy_id, spec.exchange, spec.symbol, spec.timeframe)
        for spec in filtered_paper
    }
    added = 0
    for spec in _governor_paper_specs(proposed_paper, environ):
        keys = {
            spec.lane_id,
            _paper_signature(spec.strategy_id, spec.exchange, spec.symbol, spec.timeframe),
        }
        if existing_keys & keys:
            continue
        filtered.append(spec)
        existing_keys.update(keys)
        added += 1
    if removed:
        logger.info(
            "paper governor roster filter removed %d non-roster PAPER lane(s)",
            removed,
        )
    if added:
        logger.info(
            "paper governor roster filter added %d governor PAPER lane(s)",
            added,
        )
    return _ensure_primary(filtered)


def _governor_paper_allowed_keys(rows: object) -> set[str]:
    if not isinstance(rows, list):
        return set()
    allowed: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        lane_id = str(row.get("lane_id") or row.get("trial_id") or "").strip()
        if lane_id:
            allowed.add(lane_id)
        signature = _paper_signature(
            str(row.get("strategy_id") or row.get("family") or ""),
            str(row.get("exchange") or row.get("venue") or ""),
            str(row.get("symbol") or ""),
            str(row.get("timeframe") or ""),
        )
        if signature.strip("|"):
            allowed.add(signature)
    return allowed


def _governor_paper_specs(
    rows: object,
    environ: Mapping[str, str],
) -> list[LaneSpec]:
    if not isinstance(rows, list):
        return []
    starting_equity = float(environ.get("MULTI_LANE_STARTING_EQUITY", "500"))
    daily_loss_usd = float(environ.get("MULTI_LANE_DAILY_LOSS_USD", "10"))
    out: list[LaneSpec] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        strategy_id = str(row.get("strategy_id") or row.get("family") or "").strip()
        exchange = str(row.get("exchange") or row.get("venue") or "").strip()
        symbol = str(row.get("symbol") or "").strip()
        timeframe = str(row.get("timeframe") or environ.get("MULTI_LANE_TIMEFRAME", "1h"))
        if not strategy_id or not exchange or not symbol:
            continue
        if exchange == DELTA_EXCHANGE:
            symbol = _delta_india_symbol(symbol)
        lane_id = str(row.get("lane_id") or row.get("trial_id") or "").strip()
        if not lane_id:
            lane_id = _lane_id(exchange, symbol, RunnerMode.PAPER, strategy_id)
        params = (
            dict(row.get("strategy_params"))
            if isinstance(row.get("strategy_params"), Mapping)
            else _default_params_for_strategy(strategy_id)
        )
        out.append(
            LaneSpec(
                lane_id=lane_id,
                exchange=exchange,
                symbol=symbol,
                timeframe=timeframe,
                starting_equity=starting_equity,
                daily_loss_usd=daily_loss_usd,
                is_primary=False,
                strategy_id=strategy_id,
                strategy_params=params,
                mode=RunnerMode.PAPER,
            )
        )
    return out


def _paper_spec_allowed(spec: LaneSpec, allowed: set[str]) -> bool:
    return spec.lane_id in allowed or _paper_signature(
        spec.strategy_id,
        spec.exchange,
        spec.symbol,
        spec.timeframe,
    ) in allowed


def _paper_signature(
    strategy_id: str,
    exchange: str,
    symbol: str,
    timeframe: str,
) -> str:
    return "|".join(
        [
            str(strategy_id or "").strip().lower(),
            str(exchange or "").strip().lower(),
            str(symbol or "").split(":", 1)[0].strip().lower(),
            str(timeframe or "").strip().lower(),
        ]
    )


def _ensure_primary(specs: list[LaneSpec]) -> list[LaneSpec]:
    if not specs:
        return specs
    if any(spec.is_primary for spec in specs):
        return specs
    primary_idx = next(
        (idx for idx, spec in enumerate(specs) if spec.mode is RunnerMode.PAPER),
        0,
    )
    return [
        replace(spec, is_primary=True) if idx == primary_idx else spec
        for idx, spec in enumerate(specs)
    ]


def _slug_symbol(symbol: str) -> str:
    return (
        symbol.lower()
        .replace("/", "_")
        .replace(":", "_")
        .replace("-", "_")
    )


def _env_suffix(exchange: str) -> str:
    return exchange.upper().replace("-", "_").replace("/", "_")


def _delta_india_symbol(symbol: str) -> str:
    if "/USDT" not in symbol:
        return symbol
    base = symbol.split("/", maxsplit=1)[0]
    return f"{base}/USD:USD"


def _symbols_for_exchange(
    exchange: str,
    symbols: list[str],
    environ: Mapping[str, str],
) -> list[str]:
    override = _csv_env(f"MULTI_LANE_SYMBOLS_{_env_suffix(exchange)}", "", environ)
    if override:
        return override
    if exchange == DELTA_EXCHANGE:
        return [_delta_india_symbol(symbol) for symbol in symbols]
    return symbols


def _lane_prefix(strategy_id: str) -> str:
    if strategy_id == "funding_mean_reversion_v1":
        return "funding_mr"
    if strategy_id == "trend_continuation_v1":
        return "trend_continuation"
    return strategy_id


def _lane_id(exchange: str, symbol: str, mode: RunnerMode, strategy_id: str) -> str:
    if strategy_id == "funding_mean_reversion_v1" and mode is RunnerMode.PAPER:
        governed = GOVERNED_PAPER_LANE_IDS.get((exchange, symbol))
        if governed is not None:
            return governed
    return f"{_lane_prefix(strategy_id)}_{exchange}_{_slug_symbol(symbol)}_{mode.value}"


def _default_strategy_for_exchange(exchange: str) -> str:
    if exchange == DELTA_EXCHANGE:
        return "trend_continuation_v1"
    return "funding_mean_reversion_v1"


def _default_params_for_strategy(strategy_id: str) -> dict:
    if strategy_id == "funding_mean_reversion_v1":
        return FUNDING_MR_PARAMS
    if strategy_id == "trend_continuation_v1":
        return TREND_PARAMS
    if strategy_id == "quantified_fee_wall_sniper_v1":
        return QUANTIFIED_FEE_WALL_SNIPER_PARAMS
    return {}


def _modes_for_exchange(
    exchange: str, modes: list[RunnerMode], environ: Mapping[str, str]
) -> list[RunnerMode]:
    if exchange != DELTA_EXCHANGE:
        return modes
    if _truthy(environ, "MULTI_LANE_DELTA_PAPER", "0"):
        return modes
    # Delta India has no funding history and no CCXT Pro websocket in the current
    # adapter set, so default it to observe-only shadow. Operators can opt in
    # to simulated paper explicitly after choosing a Delta-ready strategy.
    return [mode for mode in modes if mode is RunnerMode.SHADOW]


def _parse_modes(environ: Mapping[str, str]) -> list[RunnerMode]:
    modes: list[RunnerMode] = []
    for name in _csv_env("MULTI_LANE_MODES", "paper,shadow", environ):
        try:
            mode = _MODES[name.lower()]
        except KeyError:
            raise ValueError(
                f"unknown multi-lane mode {name!r} (expected one of {sorted(_MODES)})"
            ) from None
        if mode not in modes:
            modes.append(mode)
    if not modes:
        raise ValueError("at least one multi-lane mode is required")
    return modes


def build_lane_specs_from_env(
    environ: Mapping[str, str] = os.environ,
) -> list[LaneSpec]:
    exchanges = _csv_env("MULTI_LANE_EXCHANGES", DEFAULT_EXCHANGES, environ)
    symbols = _csv_env("MULTI_LANE_SYMBOLS", "BTC/USDT:USDT", environ)
    if not exchanges or not symbols:
        raise ValueError("at least one multi-lane exchange and symbol is required")
    modes = _parse_modes(environ)
    timeframe = environ.get("MULTI_LANE_TIMEFRAME", "1h")
    primary_exchange = environ.get("MULTI_LANE_PRIMARY_EXCHANGE", exchanges[0])
    primary_symbol = environ.get("MULTI_LANE_PRIMARY_SYMBOL", symbols[0])
    # The flat top-level dashboard snapshot is the governed PAPER lane by
    # default (live-$ comparison), falling back to the first configured mode.
    default_primary_mode = "paper" if RunnerMode.PAPER in modes else modes[0].value
    primary_mode = _MODES[environ.get("MULTI_LANE_PRIMARY_MODE", default_primary_mode).lower()]
    starting_equity = float(environ.get("MULTI_LANE_STARTING_EQUITY", "500"))
    daily_loss_usd = float(environ.get("MULTI_LANE_DAILY_LOSS_USD", "10"))

    specs: list[LaneSpec] = []
    for exchange in exchanges:
        strategy_id = _default_strategy_for_exchange(exchange)
        strategy_params = _default_params_for_strategy(strategy_id)
        exchange_modes = _modes_for_exchange(exchange, modes, environ)
        if not exchange_modes:
            logger.warning(
                "%s produced no lanes for modes=%s; use shadow or set "
                "MULTI_LANE_DELTA_PAPER=1 deliberately",
                exchange,
                ",".join(mode.value for mode in modes),
            )
            continue
        for symbol in _symbols_for_exchange(exchange, symbols, environ):
            for mode in exchange_modes:
                specs.append(LaneSpec(
                    lane_id=_lane_id(exchange, symbol, mode, strategy_id),
                    exchange=exchange,
                    symbol=symbol,
                    timeframe=timeframe,
                    starting_equity=starting_equity,
                    daily_loss_usd=daily_loss_usd,
                    is_primary=(
                        exchange == primary_exchange
                        and symbol == primary_symbol
                        and mode is primary_mode
                    ),
                    strategy_id=strategy_id,
                    strategy_params=strategy_params,
                    mode=mode,
                ))
    if not specs:
        raise ValueError("multi-lane configuration produced no runnable lanes")
    if not any(spec.is_primary for spec in specs):
        specs[0] = replace(specs[0], is_primary=True)
    return specs


#: Grid-proven Delta-India survivors from the independent second-eye backtest
#: (research/second_eye_grid.py). Each (strategy, symbol, timeframe) cell
#: cleared >=20 trades AND taker profit_factor >= 1.5 in the position-aware
#: backtester on Delta-India data -- unlike the 5m fee-wall scanner lanes,
#: which were zero-trade or net-negative there.
EVIDENCE_ALIGNED_DELTA_LANES: tuple[tuple[str, str, str], ...] = (
    ("vnedge_algo_ml_pro_v1", "ETH/USDT:USDT", "4h"),   # grid PF 3.64, n=22
    ("vnedge_algo_ml_pro_v1", "DOGE/USDT:USDT", "1h"),  # grid PF 1.54, n=128
    ("stealth_trail_bbp_v1", "ETH/USDT:USDT", "4h"),    # grid PF 1.79, n=49
    ("luxy_ut_bot_forecast_v1", "XRP/USDT:USDT", "1h"), # grid PF 1.66, n=44
    ("quant_signal_pack_v1", "BNB/USDT:USDT", "4h"),    # grid PF 1.65, n=39
)


def evidence_aligned_shadow_lanes(
    environ: Mapping[str, str] = os.environ,
) -> list[LaneSpec]:
    """Evidence-aligned Delta-India shadow lanes (the 4h/1h survivors).

    Replaces the 5m fee-wall scanner lanes with the timeframe/pair cells that
    actually survived the independent second-eye grid backtest at honest taker
    fees and >=20 trades. SHADOW only; default strategy params so each lane
    matches exactly what the backtest measured. Nothing here implies promotion:
    a positive shadow result still requires the usual pre-registered
    untouched-window judgment before any paper or live permission.
    """
    if not _truthy(environ, "MULTI_LANE_EVIDENCE_ALIGNED", "1"):
        return []
    if DELTA_EXCHANGE not in _csv_env("MULTI_LANE_EXCHANGES", DEFAULT_EXCHANGES, environ):
        return []
    specs: list[LaneSpec] = []
    for strategy_id, raw_symbol, timeframe in EVIDENCE_ALIGNED_DELTA_LANES:
        symbol = _delta_india_symbol(raw_symbol)
        specs.append(
            LaneSpec(
                lane_id=(
                    f"evidence_{strategy_id}_{DELTA_EXCHANGE}_"
                    f"{_slug_symbol(symbol)}_{timeframe}_shadow"
                ),
                exchange=DELTA_EXCHANGE,
                symbol=symbol,
                timeframe=timeframe,
                strategy_id=strategy_id,
                strategy_params={},
                mode=RunnerMode.SHADOW,
            )
        )
    return specs


#: Crown-jewel candidate(s) promoted from the shadow watchlist to a live-forward
#: PAPER trial. Simulated fills on live Delta data through the full risk gateway
#: (real fees/funding/slippage, paper capital) -- the "real-money-equivalent but
#: paper money" forward test that stands in for a historical walk-forward.
#: Forward data is genuinely unseen, so it cannot be overfit. Pre-registered
#: pass/fail criteria: research/paper_trials/vnedge_algo_ml_pro_eth_4h_20260726.yaml
EVIDENCE_PAPER_TRIAL_LANES: tuple[tuple[str, str, str], ...] = (
    ("vnedge_algo_ml_pro_v1", "ETH/USDT:USDT", "4h"),   # crown jewel, grid PF 3.64
    ("vnedge_algo_ml_pro_v1", "DOGE/USDT:USDT", "1h"),  # faster-sampling companion, PF 1.54 n=128
    # Diversification round: the other cross-venue survivors, run in parallel so
    # several candidates mature at once (portfolio read, not a one-shot). Locked
    # criteria: research/paper_trials/evidence_survivors_20260726.yaml.
    ("stealth_trail_bbp_v1", "ETH/USDT:USDT", "4h"),    # grid PF 1.79, n=49 (watch DD)
    ("luxy_ut_bot_forecast_v1", "XRP/USDT:USDT", "1h"), # grid PF 1.66, n=44
    ("quant_signal_pack_v1", "BNB/USDT:USDT", "4h"),    # grid PF 1.65, n=39
)


def evidence_paper_trial_lanes(
    environ: Mapping[str, str] = os.environ,
) -> list[LaneSpec]:
    """Live-forward PAPER trial of the top evidence-aligned candidate(s).

    PAPER mode: simulated fills through the full gateway on live Delta data
    (real fees/funding/slippage, paper capital) -- a forward test on genuinely
    unseen data, a stronger out-of-sample check than a historical walk-forward
    (the grid data was already seen). NOT a promotion: criteria are locked in
    research/paper_trials/vnedge_algo_ml_pro_eth_4h_20260726.yaml and a PASS
    still needs explicit human sign-off before any live permission. Requires
    ``paper`` in MULTI_LANE_MODES (the default). Toggle with
    MULTI_LANE_EVIDENCE_PAPER_TRIAL.
    """
    if not _truthy(environ, "MULTI_LANE_EVIDENCE_PAPER_TRIAL", "1"):
        return []
    if DELTA_EXCHANGE not in _csv_env("MULTI_LANE_EXCHANGES", DEFAULT_EXCHANGES, environ):
        return []
    specs: list[LaneSpec] = []
    for strategy_id, raw_symbol, timeframe in EVIDENCE_PAPER_TRIAL_LANES:
        symbol = _delta_india_symbol(raw_symbol)
        specs.append(
            LaneSpec(
                lane_id=(
                    f"papertrial_{strategy_id}_{DELTA_EXCHANGE}_"
                    f"{_slug_symbol(symbol)}_{timeframe}"
                ),
                exchange=DELTA_EXCHANGE,
                symbol=symbol,
                timeframe=timeframe,
                strategy_id=strategy_id,
                strategy_params={},
                mode=RunnerMode.PAPER,
            )
        )
    return specs


def velocity_delta_lanes(environ: Mapping[str, str] = os.environ) -> list[LaneSpec]:
    """Small, clearly-labelled VELOCITY cohort — fast 5m lanes whose ONLY job is
    to accelerate ML meta-label collection and keep the live tape visibly active.

    These are NOT edge candidates. The 5m Delta scanner setups are fee-wall
    losers — that is exactly why the evidence-aligned roster runs 4h/1h instead
    — so the value here is the opposite: a steady stream of win/loss examples
    (mostly losses) the meta-labeler needs to learn to REJECT bad setups. They
    are SHADOW-only, never mirrored to paper, and structurally unpromotable (no
    lane ever promotes without pre-registered untouched-data judgment, which a
    velocity lane never receives). The `velocity_` lane-id prefix makes that
    unmistakable in the cockpit and the journals. Gated off with
    MULTI_LANE_VELOCITY=0; kept deliberately small.
    """
    if not _truthy(environ, "MULTI_LANE_VELOCITY", "1"):
        return []
    if DELTA_EXCHANGE not in _csv_env("MULTI_LANE_EXCHANGES", DEFAULT_EXCHANGES, environ):
        return []
    symbols = [
        _delta_india_symbol(symbol)
        for symbol in _csv_env(
            "MULTI_LANE_VELOCITY_SYMBOLS",
            "ETH/USDT:USDT,BTC/USDT:USDT,SOL/USDT:USDT",
            environ,
        )
    ]
    return [
        LaneSpec(
            lane_id=f"velocity_sats_5m_scalper_{DELTA_EXCHANGE}_{_slug_symbol(symbol)}_shadow",
            exchange=DELTA_EXCHANGE,
            symbol=symbol,
            timeframe="5m",
            strategy_id="sats_5m_scalper_v1",
            strategy_params=SATS_5M_PARAMS,
            mode=RunnerMode.SHADOW,
        )
        for symbol in symbols
    ]


# --------------------------------------------------------------------------- #
# Evidence-based lane prune (2026-08-01). The live survival board (112 tracked
# lanes: 0 survivor-candidates, 7 profitable / 31 losing, ~55 that never fire)
# plus per-lane paper PnL showed these families have no edge and no path to one.
# Running them only pins the VM and buries the few real signals in noise. Pruned
# here (before paper-observation mirroring, so mirrors go too). REVERSIBLE:
# remove a rule to bring a family back, or set MULTI_LANE_PRUNE_DEAD=0 to disable
# the whole filter without a code change.
#
# KEEP (the only survivors): funding-MR BTC (proven 3× OOS+, the one paper
# earner) and crypto-trend-DOGE (OOS PASS, human-approved to paper). Everything
# else is cut.
#
# HUMAN-DIRECTED HARD CUT (2026-08-03): the operator chose an aggressive
# lean-fleet cut over the default promotion discipline. NOTE FOR THE RECORD:
# some strategies below (volatility_expansion_breakout_v1 OOS-passed on
# DOGE/BTC; quant_signal_pack ETH/SOL documented paper-positive; stealth_trail
# the claimed maker edge; vnedge_algo_ml_pro the ML evidence lane) were cut by
# explicit operator direction on a recent live-shadow loss, NOT by the
# pre-registered promotion machinery. This is deliberately recorded as an
# override so it is never mistaken for the discipline rejecting them — restore
# any of them (or set MULTI_LANE_PRUNE_DEAD=0) to resume observation.
_PRUNED_STRATEGIES = frozenset(
    {
        "alpha_stack_confluence_v1",  # NO_TRADE_EVIDENCE every symbol; 0 edge
        "luxy_ut_bot_forecast_v1",    # worst fee-wall losers (BTC 15m -$30/-$29)
        "trend_continuation_v1",      # documented REJECT; XRP lane -$22, never fires
        # Cut 2026-08-02 after a full-ledger pattern study (286 shadow trades):
        "sats_5m_scalper_v1",         # -$681 / 250 trades / 29% win — the worst lane;
                                      # 5m scalping is noise, 63% of losers gave back a
                                      # >15bps profit (no breakeven/trailing stop)
        "context_scalper_v2",         # -$27 / 14% win — no edge
        # Human-directed aggressive cut 2026-08-03 (override, see note above):
        "quant_signal_pack_v1",         # -$164 live shadow across symbols
        "volatility_expansion_breakout_v1",  # -$48 live (OOS-passed — override)
        "vnedge_algo_ml_pro_v1",        # ML evidence lane — paused per operator
        "stealth_trail_bbp_v1",         # maker fee-wall probe — claimed edge, thin
        "luxara_live_plan_qtm_v1",      # fee-wall probe, no OOS pedigree
        "fvg_liquidity_breakout_v1",    # experimental scanner, no earner status
        "luxara_break_bounce_v27_v1",   # experimental scanner, no earner status
    }
)


def _pruned_lane(spec: LaneSpec) -> bool:
    """True for a lane the evidence says never helps — excluded from the roster."""
    strat = str(spec.strategy_id or "")
    base_symbol = str(spec.symbol or "").split("/")[0].upper()
    frozen_paper_trials = {
        (strategy_id, _delta_india_symbol(symbol), timeframe)
        for strategy_id, symbol, timeframe in EVIDENCE_PAPER_TRIAL_LANES
    }
    # The 2026-08-03 family-wide hard cut remains in force for general shadow
    # and ad-hoc paper lanes. Only these exact, human-preregistered Delta paper
    # cells are restored for live-forward evidence collection. This does not
    # grant promotion or live-order authority.
    if (
        spec.mode is RunnerMode.PAPER
        and spec.exchange == DELTA_EXCHANGE
        and (strat, str(spec.symbol), str(spec.timeframe)) in frozen_paper_trials
        and dict(spec.strategy_params or {}) == {}
    ):
        return False
    if strat in _PRUNED_STRATEGIES:
        return True
    # quant_signal_pack: paper-positive on ETH/SOL, worst single loser on DOGE (-$44).
    if strat == "quant_signal_pack_v1" and base_symbol == "DOGE":
        return True
    # funding mean-reversion: only BTC has a proven edge; ETH is DEAD, others lose.
    if strat == "funding_mean_reversion_v1" and base_symbol != "BTC":
        return True
    return False


def desired_lane_specs(environ: Mapping[str, str] = os.environ) -> list[LaneSpec]:
    """The full deduped spec list the runner is SUPPOSED to run.

    Single source of truth shared by main() and the lane-health auditor
    (vnedge.runtime.lane_health), so "desired" can never silently diverge
    from what the runner actually launches.

    When MULTI_LANE_PAPER_OBSERVE_ALL is on, shadow-only lanes are additionally
    mirrored into isolated PAPER observation ledgers. It is layered on the fully
    deduped base set so the mirror sees which paper trials already exist and
    never duplicates a governed one; with the flag off it is a strict no-op.
    """
    base = dedupe_lane_specs(
        build_lane_specs_from_env(environ)
        + candidate_shadow_lanes(environ)
        + crypto_trend_doge_shadow_lanes(environ)
        + delta_funding_mr_lanes(environ)
        + evidence_aligned_shadow_lanes(environ)
        + evidence_paper_trial_lanes(environ)
        + sats_5m_delta_lanes(environ)
        + stealth_trail_bbp_delta_lanes(environ)
        + fvg_liquidity_breakout_delta_lanes(environ)
        + context_scalper_v2_delta_lanes(environ)
        + luxara_live_plan_qtm_delta_lanes(environ)
        + luxara_break_bounce_v27_delta_lanes(environ)
        + fee_wall_paper_probe_lanes(environ)
        + velocity_delta_lanes(environ)
    )
    # Prune proven-dead lane families (before mirroring, so their paper-observation
    # mirrors are dropped too). Toggle off with MULTI_LANE_PRUNE_DEAD=0.
    prune = _truthy(environ, "MULTI_LANE_PRUNE_DEAD", "1")
    if prune:
        base = [spec for spec in base if not _pruned_lane(spec)]
    roster = _governor_paper_roster_filter(
        dedupe_lane_specs(base + paper_observation_lanes(base, environ)),
        environ,
    )
    # Re-apply the prune to the FINAL roster: the governor can inject paper lanes
    # from its (possibly stale) JSON, which bypasses the family prune above. This
    # guarantees a pruned/dead strategy can NEVER be resurrected by a governor
    # proposal (a stale governor file resurrected a cut luxara probe on 2026-08-03).
    if prune:
        roster = [spec for spec in roster if not _pruned_lane(spec)]
    registry_path = Path(
        environ.get("VNEDGE_STRATEGY_REGISTRY", str(DEFAULT_REGISTRY))
    )
    canonical_roster: list[LaneSpec] = []
    for spec in roster:
        if spec.mode is not RunnerMode.PAPER:
            canonical_roster.append(spec)
            continue
        blockers = strategy_authority_blockers(
            spec.strategy_id,
            purpose="paper",
            registry_path=registry_path,
        )
        if blockers:
            logger.warning(
                "paper lane %s withheld by canonical strategy registry: %s",
                spec.lane_id,
                "; ".join(blockers),
            )
            continue
        canonical_roster.append(spec)
    roster = canonical_roster
    if not roster:
        raise ValueError("multi-lane policy produced no runnable lanes")
    if not any(spec.is_primary for spec in roster):
        # Pruning/governance may remove the primary chosen by the initial env
        # expansion. Reassign deterministically so the read-only dashboard and
        # paper runtime cannot crash with a hidden StopIteration.
        roster[0] = replace(roster[0], is_primary=True)
    return roster


def lane_specs_fingerprint(specs: list[LaneSpec]) -> str:
    """Stable digest of the loaded runtime lane set.

    The research loop rewrites ``shadow_lanes.json`` atomically. A running
    multi-lane process cannot hot-add/remove per-lane feeds safely without
    duplicating a second execution path, so it watches this digest and exits
    deliberately when the desired set changes. Docker then restarts it into
    the fresh lane set.
    """
    payload = [
        {
            "lane_id": spec.lane_id,
            "exchange": spec.exchange,
            "symbol": spec.symbol,
            "timeframe": spec.timeframe,
            "mode": spec.mode.value,
            "strategy_id": spec.strategy_id,
            "strategy_params": spec.strategy_params or {},
            "starting_equity": spec.starting_equity,
            "daily_loss_usd": spec.daily_loss_usd,
            "is_primary": spec.is_primary,
        }
        for spec in specs
    ]
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def _manifest_path(environ: Mapping[str, str]) -> Path:
    out_dir = Path(environ.get("MULTI_LANE_RESEARCH_DIR", "research/live_research"))
    return out_dir / "shadow_lanes.json"


class LaneSetChanged(RuntimeError):
    """Raised by the control-plane watcher to trigger a managed restart."""


async def _watch_lane_set(
    initial_fingerprint: str,
    environ: Mapping[str, str],
    *,
    interval_seconds: float,
) -> None:
    """Exit-on-drift watcher for the dynamic research -> shadow manifest bridge."""
    while True:
        await asyncio.sleep(interval_seconds)
        current = lane_specs_fingerprint(desired_lane_specs(environ))
        if current != initial_fingerprint:
            raise LaneSetChanged(
                "desired lane set changed "
                f"({initial_fingerprint} -> {current}); restarting to reload lanes"
            )


async def _publish_paper_activation_forever(
    lanes: list[LaneSpec],
    journal_dir: Path,
    *,
    interval_seconds: float = 30.0,
) -> None:
    """Publish the paper-route truth board beside the running simulator.

    This observer cannot start a lane or send an exchange order. It makes the
    already-constructed paper broker route visible to the dashboard and keeps
    live-capital permissions explicitly false.
    """

    from vnedge.research.paper_lane_activation import (
        DEFAULT_FEED,
        DEFAULT_OUT,
        PaperLaneActivationConfig,
        build_paper_lane_activation,
        publish_paper_lane_activation,
    )

    config = PaperLaneActivationConfig(
        requested_margin_usd=float(os.environ.get("PAPER_PLAN_MARGIN_USD", "100")),
        requested_leverage=float(os.environ.get("PAPER_PLAN_LEVERAGE", "5")),
        live_margin_usd=float(os.environ.get("LIVE_PLAN_MARGIN_USD", "100")),
        live_leverage=float(os.environ.get("LIVE_PLAN_LEVERAGE", "5")),
        high_leverage_ack=False,
    )
    while True:
        try:
            payload = await asyncio.to_thread(
                build_paper_lane_activation,
                desired_specs=lanes,
                journal_dir=journal_dir,
                config=config,
            )
            await asyncio.to_thread(
                publish_paper_lane_activation,
                payload,
                DEFAULT_OUT,
                DEFAULT_FEED,
            )
        except Exception:  # noqa: BLE001 - telemetry failure must not kill paper risk loop
            logger.exception("paper activation truth publisher failed")
        await asyncio.sleep(max(5.0, interval_seconds))


async def main() -> int:
    shutdown_requested = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed_signals: list[signal.Signals] = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown_requested.set)
            installed_signals.append(sig)
        except NotImplementedError:  # pragma: no cover - non-POSIX fallback
            pass
    journal_dir = Path(os.environ.get("MULTI_LANE_JOURNAL_DIR", "logs/paper_trials"))
    lanes = desired_lane_specs()
    primary = next(spec.lane_id for spec in lanes if spec.is_primary)
    lane_set_hash = lane_specs_fingerprint(lanes)
    reload_enabled = _truthy(os.environ, "MULTI_LANE_MANIFEST_RELOAD", "1")
    reload_interval = float(os.environ.get("MULTI_LANE_MANIFEST_RELOAD_SECONDS", "60"))
    provider = MultiLaneProvider(
        primary_lane_id=primary, lane_specs=lanes, journal_dir=journal_dir,
        runtime_control={
            "lane_set_hash": lane_set_hash,
            "configured_lanes": len(lanes),
            "manifest_reload_enabled": reload_enabled,
            "manifest_reload_seconds": reload_interval if reload_enabled else None,
            "manifest_path": str(_manifest_path(os.environ)),
            "mode_ladder": "paper/shadow only; live orders remain gated elsewhere",
            "real_time_trade_compatible": True,
            "orders_allowed": False,
            "safety": "same strategy -> gateway -> journal -> order-manager path; no live adapter mounted",
        },
    )

    server_task = None
    server = None
    from vnedge.dashboard.auth import TokenStore

    token_store = TokenStore.from_env()  # DASHBOARD_USERS + legacy DASHBOARD_TOKEN
    if len(token_store):
        import uvicorn

        from vnedge.dashboard.app import create_app

        app = create_app(
            provider, token_store=token_store,
            history_path=journal_dir / f"{primary}.equity.jsonl",
            research_path=Path("research/live_research/latest.json"),
            alpha_council_path=Path("research/live_research/alpha_council_latest.json"),
            alpha_workbench_path=Path("research/live_research/alpha_workbench_latest.json"),
            vibe_intelligence_path=Path("research/live_research/vibe_intelligence_latest.json"),
            alerts_path=Path("logs/alerts.jsonl"),
            journal_dir=journal_dir,
            lane_readiness_path=Path(
                "research/live_research/lane_promotion_readiness_latest.json"
            ),
            realtime_scanner_path=Path(
                "research/live_research/realtime_scanner_latest.json"
            ),
            delta_scalper_path=Path(
                "research/live_research/delta_scalper_engine_latest.json"
            ),
            pine_research_path=Path("research/pine_scripts/pine_research_kb.json"),
        )
        server = uvicorn.Server(uvicorn.Config(
            app, host=os.environ.get("DASHBOARD_HOST", "127.0.0.1"),
            port=int(os.environ.get("DASHBOARD_PORT", "8080")), log_level="warning"))
        server_task = asyncio.create_task(server.serve())

    by_mode: dict[str, int] = {}
    for spec in lanes:
        by_mode[spec.mode.value] = by_mode.get(spec.mode.value, 0) + 1
    logger.info("configured %d lanes (%s); primary=%s", len(lanes),
                ", ".join(f"{n} {m}" for m, n in sorted(by_mode.items())), primary)
    runner = MultiLaneShadowRunner(lanes, journal_dir, provider)
    tasks = [
        asyncio.create_task(runner.run(), name="multi-lane-runner"),
        asyncio.create_task(
            _publish_paper_activation_forever(
                lanes,
                journal_dir,
                interval_seconds=float(
                    os.environ.get("PAPER_ACTIVATION_REFRESH_SECONDS", "30")
                ),
            ),
            name="paper-activation-publisher",
        ),
    ]
    if reload_enabled:
        tasks.append(asyncio.create_task(
            _watch_lane_set(
                lane_set_hash, os.environ, interval_seconds=reload_interval
            ),
            name="lane-set-watch",
        ))
    shutdown_task = asyncio.create_task(
        shutdown_requested.wait(), name="runtime-shutdown-request"
    )
    try:
        done, pending = await asyncio.wait(
            [*tasks, shutdown_task], return_when=asyncio.FIRST_COMPLETED
        )
        if shutdown_task in done:
            logger.info("managed shutdown requested")
            return 0
        for task in done:
            exc = task.exception()
            if isinstance(exc, LaneSetChanged):
                logger.warning("%s", exc)
                return MANIFEST_RELOAD_EXIT_CODE
            if exc is not None:
                raise exc
        return 0
    finally:
        shutdown_task.cancel()
        await asyncio.gather(shutdown_task, return_exceptions=True)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if server_task is not None:
            if server is not None:
                server.should_exit = True
            try:
                await asyncio.wait_for(server_task, timeout=5.0)
            except TimeoutError:
                server_task.cancel()
                await asyncio.gather(server_task, return_exceptions=True)
        for sig in installed_signals:
            loop.remove_signal_handler(sig)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
