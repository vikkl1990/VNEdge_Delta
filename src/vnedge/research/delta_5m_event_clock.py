"""Delta India 5m UP/DOWN event-style clock.

This module does not create a binary-options venue. It gives VNEDGE a
Polymarket-like decision surface over normal Delta India perpetuals: every
5-minute UTC window, closed-candle-only, fee-wall-aware, and read-only.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
import json
import math
from pathlib import Path
import time
from typing import Any

import pandas as pd

from vnedge.data.parquet_store import ParquetStore
from vnedge.data.schemas import TIMEFRAME_MS
from vnedge.scalping.parameter_registry import (
    DEFAULT_SCALPER_PARAMETER_REGISTRY,
    ExchangeFeeProfile,
)
from vnedge.strategy.indicators import atr, efficiency_ratio, ema


DELTA_5M_EVENT_CLOCK_ID = "delta_5m_event_clock_v1"
DEFAULT_EXCHANGE = "delta_india"
DEFAULT_TIMEFRAME = "5m"
DEFAULT_SYMBOLS = ("BTC/USD:USD", "ETH/USD:USD", "SOL/USD:USD", "XRP/USD:USD")
DEFAULT_OUT = Path("research/live_research/delta_5m_event_clock_latest.json")
DEFAULT_FEED = Path("research/live_research/delta_5m_event_clock_feed.jsonl")


@dataclass(frozen=True)
class Delta5mEventClockConfig:
    timeframe: str = DEFAULT_TIMEFRAME
    decision_window_seconds: int = 45
    stale_after_seconds: int = 660
    lookback_bars: int = 180
    warmup_bars: int = 60
    min_probability: float = 0.62
    taker_min_probability: float = 0.68
    profit_buffer_bps: float = 5.0
    min_expected_move_bps: float = 15.0
    paper_margin_usd: float = 100.0
    paper_leverage: float = 25.0

    def __post_init__(self) -> None:
        if self.timeframe not in TIMEFRAME_MS:
            raise ValueError(f"unsupported timeframe {self.timeframe!r}")
        if self.decision_window_seconds <= 0:
            raise ValueError("decision_window_seconds must be positive")
        if self.stale_after_seconds <= self.decision_window_seconds:
            raise ValueError("stale_after_seconds must exceed decision window")
        if self.lookback_bars < self.warmup_bars:
            raise ValueError("lookback_bars must be >= warmup_bars")
        if not 0.50 < self.min_probability < 1.0:
            raise ValueError("min_probability must be in (0.50, 1.0)")
        if self.taker_min_probability < self.min_probability:
            raise ValueError("taker_min_probability must be >= min_probability")
        if self.profit_buffer_bps < 0 or self.min_expected_move_bps < 0:
            raise ValueError("edge floors cannot be negative")
        if self.paper_margin_usd <= 0 or self.paper_leverage <= 0:
            raise ValueError("paper sizing values must be positive")

    @property
    def timeframe_seconds(self) -> int:
        return TIMEFRAME_MS[self.timeframe] // 1000

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["timeframe_seconds"] = self.timeframe_seconds
        return data


def build_delta_5m_event_clock(
    *,
    data_root: Path | str = Path("data"),
    exchange: str = DEFAULT_EXCHANGE,
    symbols: tuple[str, ...] = DEFAULT_SYMBOLS,
    config: Delta5mEventClockConfig = Delta5mEventClockConfig(),
    now: datetime | None = None,
) -> dict[str, Any]:
    store = ParquetStore(data_root)
    frames: dict[str, pd.DataFrame] = {}
    errors: dict[str, str] = {}
    for symbol in symbols:
        try:
            frames[symbol] = store.read_candles(exchange, symbol, config.timeframe)
        except FileNotFoundError as exc:
            errors[symbol] = str(exc)
    return build_delta_5m_event_clock_from_frames(
        frames,
        missing_symbols=errors,
        exchange=exchange,
        config=config,
        now=now,
    )


def build_delta_5m_event_clock_from_frames(
    frames: Mapping[str, pd.DataFrame],
    *,
    missing_symbols: Mapping[str, str] | None = None,
    exchange: str = DEFAULT_EXCHANGE,
    config: Delta5mEventClockConfig = Delta5mEventClockConfig(),
    now: datetime | None = None,
) -> dict[str, Any]:
    generated = _ensure_utc(now or datetime.now(UTC))
    fee = DEFAULT_SCALPER_PARAMETER_REGISTRY.fee_profile(exchange)
    window = _window_state(generated, config)
    rows = [
        _row_from_candles(
            symbol=symbol,
            candles=frame,
            exchange=exchange,
            config=config,
            fee=fee,
            now=generated,
            window=window,
        )
        for symbol, frame in frames.items()
    ]
    for symbol, reason in (missing_symbols or {}).items():
        rows.append(_missing_row(symbol, reason, exchange, config, fee, generated, window))
    rows.sort(key=_row_sort_key)
    summary = _summary(rows, window, generated)
    return {
        "report_id": DELTA_5M_EVENT_CLOCK_ID,
        "generated_at": generated.isoformat(),
        "mode": "read_only_delta_5m_up_down_perp_proxy",
        "exchange": exchange,
        "timeframe": config.timeframe,
        "config": config.to_dict(),
        "fee_profile": fee.to_dict(),
        "event_window": window,
        "summary": summary,
        "rows": rows,
        "operator_answer": _operator_answer(summary),
        "policy": {
            "read_only": True,
            "venue": "Delta India perpetuals, not a binary event market",
            "closed_bar_only": True,
            "paper_route_only": True,
            "live_requires_normal_pre_live_gates": True,
            "can_trade": False,
            "can_promote": False,
        },
        "can_trade": False,
        "can_promote": False,
    }


def publish_delta_5m_event_clock(
    payload: Mapping[str, Any],
    out: Path | str = DEFAULT_OUT,
    feed: Path | str | None = DEFAULT_FEED,
) -> None:
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    tmp.replace(out_path)
    if feed is not None:
        feed_path = Path(feed)
        feed_path.parent.mkdir(parents=True, exist_ok=True)
        with feed_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_feed_record(payload), sort_keys=True, default=str) + "\n")


def render_report(payload: Mapping[str, Any], *, limit: int = 8) -> str:
    summary = payload.get("summary", {})
    lines = [
        "=== Delta 5m event clock ===",
        f"generated: {payload.get('generated_at')}",
        str(payload.get("operator_answer") or ""),
        (
            "summary: "
            f"{summary.get('ready_now', 0)} ready, "
            f"{summary.get('waiting', 0)} waiting, "
            f"{summary.get('stale_or_missing', 0)} stale/missing"
        ),
    ]
    for row in list(payload.get("rows", []))[:limit]:
        lines.append(
            f"  {row.get('execution_window_state', ''):<18} "
            f"{row.get('symbol', ''):<12} {row.get('direction', ''):<4} "
            f"p={100 * float(row.get('selected_probability') or 0):5.1f}% "
            f"exp={float(row.get('expected_move_bps') or 0):6.2f}bps "
            f"route={row.get('route', ''):<16} {row.get('why', '')}"
        )
    lines.append("read-only: can_trade=false can_promote=false")
    return "\n".join(lines)


def _row_from_candles(
    *,
    symbol: str,
    candles: pd.DataFrame,
    exchange: str,
    config: Delta5mEventClockConfig,
    fee: ExchangeFeeProfile,
    now: datetime,
    window: Mapping[str, Any],
) -> dict[str, Any]:
    frame = _normalize_candles(candles, config)
    if len(frame) < config.warmup_bars:
        return _safe_row(
            symbol,
            exchange,
            config,
            fee,
            now,
            window,
            state="DATA_MISSING",
            why=f"need {config.warmup_bars} closed bars; have {len(frame)}",
            latest_closed_bar_ts=_latest_ts(frame),
        )

    latest_open = _ensure_utc(frame.iloc[-1]["timestamp"].to_pydatetime())
    latest_end = latest_open + timedelta(seconds=config.timeframe_seconds)
    data_age_seconds = max(0.0, (now - latest_end).total_seconds())
    stale = data_age_seconds > config.stale_after_seconds
    has_expected_close = latest_end >= _parse_iso(window["current_window_start"])
    if stale or not has_expected_close:
        state = "DATA_STALE"
        why = (
            f"latest closed bar ended {int(data_age_seconds)}s ago"
            if stale
            else "latest closed bar does not cover the prior 5m window"
        )
        return _safe_row(
            symbol,
            exchange,
            config,
            fee,
            now,
            window,
            state=state,
            why=why,
            latest_closed_bar_ts=latest_open.isoformat(),
            data_age_seconds=data_age_seconds,
        )

    enriched = _enrich(frame)
    last = enriched.iloc[-1]
    features = _features(last)
    probability_up, raw_score = _probability_up(features)
    probability_down = 1.0 - probability_up
    direction = "UP" if probability_up >= probability_down else "DOWN"
    selected_probability = probability_up if direction == "UP" else probability_down
    confidence = abs(selected_probability - 0.5) * 2.0
    expected_move_bps = _expected_move_bps(features, confidence)
    maker_required = fee.maker_first_cost_bps + config.profit_buffer_bps
    taker_required = fee.taker_round_trip_cost_bps + config.profit_buffer_bps
    route, route_state, why = _route_decision(
        selected_probability=selected_probability,
        expected_move_bps=expected_move_bps,
        maker_required_bps=maker_required,
        taker_required_bps=taker_required,
        config=config,
        window=window,
    )
    notional = config.paper_margin_usd * config.paper_leverage
    state = route_state
    return {
        "row_id": _row_id(exchange, symbol, config.timeframe),
        "exchange": exchange,
        "symbol": symbol,
        "timeframe": config.timeframe,
        "event_contract": "internal_delta_5m_up_down_perp_proxy",
        "execution_window_state": state,
        "route": route,
        "direction": direction,
        "probability_up": round(probability_up, 4),
        "probability_down": round(probability_down, 4),
        "selected_probability": round(selected_probability, 4),
        "confidence": round(confidence, 4),
        "raw_score": round(raw_score, 4),
        "expected_move_bps": round(expected_move_bps, 4),
        "expected_net_maker_bps": round(expected_move_bps - fee.maker_first_cost_bps, 4),
        "expected_net_taker_bps": round(expected_move_bps - fee.taker_round_trip_cost_bps, 4),
        "maker_first_cost_bps": round(fee.maker_first_cost_bps, 4),
        "taker_round_trip_cost_bps": round(fee.taker_round_trip_cost_bps, 4),
        "required_maker_move_bps": round(maker_required, 4),
        "required_taker_move_bps": round(taker_required, 4),
        "paper_margin_usd": config.paper_margin_usd,
        "paper_leverage": config.paper_leverage,
        "paper_notional_usd": round(notional, 2),
        "paper_execution_ready": route in {"MAKER_ONLY", "MAKER_THEN_TAKER", "TAKER_NOW"},
        "live_execution_ready": False,
        "latest_closed_bar_ts": latest_open.isoformat(),
        "latest_closed_bar_end_ts": latest_end.isoformat(),
        "data_age_seconds": round(data_age_seconds, 3),
        "window": dict(window),
        "features": features,
        "why": why,
        "next_action": _next_action(route, state, window),
        "can_trade": False,
        "can_promote": False,
    }


def _normalize_candles(candles: pd.DataFrame, config: Delta5mEventClockConfig) -> pd.DataFrame:
    required = {"timestamp", "open", "high", "low", "close", "volume"}
    if candles.empty or not required <= set(candles.columns):
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    frame = candles[["timestamp", "open", "high", "low", "close", "volume"]].copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    for col in ("open", "high", "low", "close", "volume"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce").astype("float64")
    frame = (
        frame.dropna(subset=["timestamp", "open", "high", "low", "close"])
        .drop_duplicates(subset="timestamp", keep="last")
        .sort_values("timestamp")
        .tail(config.lookback_bars)
        .reset_index(drop=True)
    )
    return frame


def _enrich(frame: pd.DataFrame) -> pd.DataFrame:
    enriched = frame.copy()
    close = enriched["close"].replace(0.0, pd.NA).astype("float64")
    ema13 = ema(close, 13)
    ema34 = ema(close, 34)
    enriched["ema13"] = ema13
    enriched["ema34"] = ema34
    enriched["atr14"] = atr(enriched, 14)
    enriched["er24"] = efficiency_ratio(close, 24)
    enriched["ret_1_bps"] = close.pct_change(1) * 10_000.0
    enriched["ret_3_bps"] = close.pct_change(3) * 10_000.0
    enriched["ret_6_bps"] = close.pct_change(6) * 10_000.0
    enriched["ema_gap_bps"] = (ema13 - ema34) / close * 10_000.0
    enriched["bull_power_bps"] = (enriched["high"] - ema13) / close * 10_000.0
    enriched["bear_power_bps"] = (enriched["low"] - ema13) / close * 10_000.0
    enriched["bbp_bps"] = enriched["bull_power_bps"] + enriched["bear_power_bps"]
    enriched["bbp_slope_bps"] = enriched["bbp_bps"] - enriched["bbp_bps"].shift(3)
    vol_mean = enriched["volume"].rolling(48).mean()
    vol_std = enriched["volume"].rolling(48).std().replace(0.0, pd.NA)
    enriched["volume_z"] = (enriched["volume"] - vol_mean) / vol_std
    enriched["atr_bps"] = enriched["atr14"] / close * 10_000.0
    return enriched


def _features(last: pd.Series) -> dict[str, float]:
    fields = (
        "close",
        "ret_1_bps",
        "ret_3_bps",
        "ret_6_bps",
        "ema_gap_bps",
        "bull_power_bps",
        "bear_power_bps",
        "bbp_bps",
        "bbp_slope_bps",
        "volume_z",
        "atr_bps",
        "er24",
    )
    result = {name: _finite_float(last.get(name), 0.0) for name in fields}
    result["er24"] = _clamp(result["er24"], 0.0, 1.0)
    result["volume_z"] = _clamp(result["volume_z"], -3.0, 5.0)
    return {key: round(value, 6) for key, value in result.items()}


def _probability_up(features: Mapping[str, float]) -> tuple[float, float]:
    raw = (
        0.34 * math.tanh(features["ret_3_bps"] / 32.0)
        + 0.22 * math.tanh(features["ema_gap_bps"] / 24.0)
        + 0.18 * math.tanh(features["bbp_slope_bps"] / 18.0)
        + 0.14 * math.tanh(features["ret_1_bps"] / 18.0)
        + 0.12 * math.tanh(features["ret_6_bps"] / 55.0) * max(0.25, features["er24"])
    )
    participation = _clamp(1.0 + 0.045 * features["volume_z"], 0.88, 1.18)
    raw *= participation
    probability = 1.0 / (1.0 + math.exp(-2.7 * raw))
    return _clamp(probability, 0.05, 0.95), raw


def _expected_move_bps(features: Mapping[str, float], confidence: float) -> float:
    atr_bps = max(0.0, features["atr_bps"])
    directional_impulse = 0.28 * abs(features["ret_3_bps"]) + 0.16 * abs(features["ret_1_bps"])
    participation = max(0.0, features["volume_z"]) * 1.4
    structural = atr_bps * (0.38 + 0.58 * confidence)
    return max(0.0, structural + directional_impulse + participation)


def _route_decision(
    *,
    selected_probability: float,
    expected_move_bps: float,
    maker_required_bps: float,
    taker_required_bps: float,
    config: Delta5mEventClockConfig,
    window: Mapping[str, Any],
) -> tuple[str, str, str]:
    if window["decision_window_state"] != "OPEN":
        return (
            "WAIT",
            "WAIT_NEXT_WINDOW",
            f"entry window closed; next decision at {window['next_decision_at']}",
        )
    if expected_move_bps < config.min_expected_move_bps:
        return (
            "WAIT",
            "EDGE_TOO_SMALL",
            f"expected move {expected_move_bps:.2f}bps below floor {config.min_expected_move_bps:.2f}bps",
        )
    maker_clear = (
        selected_probability >= config.min_probability
        and expected_move_bps >= maker_required_bps
    )
    taker_clear = (
        selected_probability >= config.taker_min_probability
        and expected_move_bps >= taker_required_bps
    )
    if taker_clear:
        return (
            "TAKER_NOW",
            "READY_TAKER",
            "taker fallback allowed: probability and expected move clear Delta fees plus buffer",
        )
    if maker_clear and selected_probability >= (config.taker_min_probability - 0.03):
        return (
            "MAKER_THEN_TAKER",
            "READY_MAKER_WITH_FALLBACK_WATCH",
            "maker first; taker only if the move expands before the window ages out",
        )
    if maker_clear:
        return (
            "MAKER_ONLY",
            "READY_MAKER_ONLY",
            "maker first only; taker does not clear the stricter fee wall",
        )
    return (
        "WAIT",
        "WAITING_FEE_WALL",
        (
            f"p={selected_probability:.2f}, expected={expected_move_bps:.2f}bps; "
            f"need maker {maker_required_bps:.2f}bps or taker {taker_required_bps:.2f}bps"
        ),
    )


def _window_state(now: datetime, config: Delta5mEventClockConfig) -> dict[str, Any]:
    tf = config.timeframe_seconds
    epoch = int(now.timestamp())
    start_epoch = (epoch // tf) * tf
    start = datetime.fromtimestamp(start_epoch, UTC)
    end = start + timedelta(seconds=tf)
    seconds_since_open = max(0, int((now - start).total_seconds()))
    seconds_to_close = max(0, int((end - now).total_seconds()))
    open_now = seconds_since_open <= config.decision_window_seconds
    next_decision = start if open_now else end
    return {
        "current_window_start": start.isoformat(),
        "current_window_end": end.isoformat(),
        "seconds_since_open": seconds_since_open,
        "seconds_to_close": seconds_to_close,
        "decision_window_seconds": config.decision_window_seconds,
        "decision_window_state": "OPEN" if open_now else "CLOSED",
        "next_decision_at": next_decision.isoformat(),
        "seconds_to_next_decision": max(0, int((next_decision - now).total_seconds())),
    }


def _safe_row(
    symbol: str,
    exchange: str,
    config: Delta5mEventClockConfig,
    fee: ExchangeFeeProfile,
    now: datetime,
    window: Mapping[str, Any],
    *,
    state: str,
    why: str,
    latest_closed_bar_ts: str | None = None,
    data_age_seconds: float | None = None,
) -> dict[str, Any]:
    return {
        "row_id": _row_id(exchange, symbol, config.timeframe),
        "exchange": exchange,
        "symbol": symbol,
        "timeframe": config.timeframe,
        "event_contract": "internal_delta_5m_up_down_perp_proxy",
        "execution_window_state": state,
        "route": "WAIT",
        "direction": "NONE",
        "probability_up": None,
        "probability_down": None,
        "selected_probability": None,
        "confidence": 0.0,
        "expected_move_bps": None,
        "expected_net_maker_bps": None,
        "expected_net_taker_bps": None,
        "maker_first_cost_bps": round(fee.maker_first_cost_bps, 4),
        "taker_round_trip_cost_bps": round(fee.taker_round_trip_cost_bps, 4),
        "required_maker_move_bps": round(fee.maker_first_cost_bps + config.profit_buffer_bps, 4),
        "required_taker_move_bps": round(
            fee.taker_round_trip_cost_bps + config.profit_buffer_bps, 4
        ),
        "paper_margin_usd": config.paper_margin_usd,
        "paper_leverage": config.paper_leverage,
        "paper_notional_usd": round(config.paper_margin_usd * config.paper_leverage, 2),
        "paper_execution_ready": False,
        "live_execution_ready": False,
        "latest_closed_bar_ts": latest_closed_bar_ts,
        "latest_closed_bar_end_ts": None,
        "data_age_seconds": round(data_age_seconds, 3) if data_age_seconds is not None else None,
        "window": dict(window),
        "features": {},
        "why": why,
        "next_action": "wait for fresh closed Delta 5m candles",
        "can_trade": False,
        "can_promote": False,
    }


def _missing_row(
    symbol: str,
    reason: str,
    exchange: str,
    config: Delta5mEventClockConfig,
    fee: ExchangeFeeProfile,
    now: datetime,
    window: Mapping[str, Any],
) -> dict[str, Any]:
    return _safe_row(
        symbol,
        exchange,
        config,
        fee,
        now,
        window,
        state="DATA_MISSING",
        why=reason,
    )


def _summary(rows: list[dict[str, Any]], window: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    ready = [row for row in rows if row.get("paper_execution_ready")]
    waiting = [row for row in rows if row.get("route") == "WAIT"]
    stale_or_missing = [
        row
        for row in rows
        if row.get("execution_window_state") in {"DATA_STALE", "DATA_MISSING"}
    ]
    best = ready[0] if ready else next((row for row in rows if row.get("direction") != "NONE"), None)
    return {
        "generated_at": now.isoformat(),
        "total_rows": len(rows),
        "ready_now": len(ready),
        "waiting": len(waiting),
        "stale_or_missing": len(stale_or_missing),
        "decision_window_state": window["decision_window_state"],
        "seconds_to_close": window["seconds_to_close"],
        "seconds_to_next_decision": window["seconds_to_next_decision"],
        "next_decision_at": window["next_decision_at"],
        "best_symbol": best.get("symbol") if best else None,
        "best_direction": best.get("direction") if best else None,
        "best_route": best.get("route") if best else None,
        "best_probability": best.get("selected_probability") if best else None,
        "best_expected_move_bps": best.get("expected_move_bps") if best else None,
    }


def _operator_answer(summary: Mapping[str, Any]) -> str:
    if int(summary.get("ready_now") or 0) > 0:
        return (
            f"Delta 5m timer: {summary['ready_now']} lane(s) clear the paper route now; "
            f"best is {summary.get('best_symbol')} {summary.get('best_direction')} via "
            f"{summary.get('best_route')}."
        )
    if str(summary.get("decision_window_state")) != "OPEN":
        return (
            "Delta 5m timer: waiting for the next UTC 5-minute boundary; "
            f"next decision in {summary.get('seconds_to_next_decision')}s."
        )
    return "Delta 5m timer: window is open, but no symbol clears probability plus fee wall."


def _next_action(route: str, state: str, window: Mapping[str, Any]) -> str:
    if route == "TAKER_NOW":
        return "paper-route taker is allowed by model edge; live remains gated"
    if route == "MAKER_THEN_TAKER":
        return "paper-route maker first; watch for taker fallback before window close"
    if route == "MAKER_ONLY":
        return "paper-route maker only; do not cross spread"
    if state == "WAIT_NEXT_WINDOW":
        return f"wait until {window['next_decision_at']}"
    return "wait; fee wall or probability is not clear"


def _feed_record(payload: Mapping[str, Any]) -> dict[str, Any]:
    summary = payload.get("summary", {})
    return {
        "report_id": payload.get("report_id"),
        "generated_at": payload.get("generated_at"),
        "ready_now": summary.get("ready_now"),
        "best_symbol": summary.get("best_symbol"),
        "best_direction": summary.get("best_direction"),
        "best_route": summary.get("best_route"),
        "can_trade": False,
        "can_promote": False,
    }


def _row_sort_key(row: Mapping[str, Any]) -> tuple[int, float, float, str]:
    route_rank = {
        "TAKER_NOW": 0,
        "MAKER_THEN_TAKER": 1,
        "MAKER_ONLY": 2,
        "WAIT": 3,
    }.get(str(row.get("route")), 9)
    probability = float(row.get("selected_probability") or 0.0)
    expected = float(row.get("expected_move_bps") or -1.0)
    return (route_rank, -probability, -expected, str(row.get("symbol") or ""))


def _row_id(exchange: str, symbol: str, timeframe: str) -> str:
    clean = symbol.lower().replace("/", "_").replace(":", "_").replace("-", "_")
    return f"{DELTA_5M_EVENT_CLOCK_ID}:{exchange}:{clean}:{timeframe}"


def _latest_ts(frame: pd.DataFrame) -> str | None:
    if frame.empty or "timestamp" not in frame.columns:
        return None
    ts = pd.to_datetime(frame.iloc[-1]["timestamp"], utc=True)
    return ts.isoformat()


def _ensure_utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _finite_float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _parse_symbols(raw: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    parser.add_argument("--exchange", default=DEFAULT_EXCHANGE)
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    parser.add_argument("--interval-seconds", type=int, default=15)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--feed", type=Path, default=DEFAULT_FEED)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--print", action="store_true")
    args = parser.parse_args()

    config = Delta5mEventClockConfig()
    while True:
        payload = build_delta_5m_event_clock(
            data_root=args.data_root,
            exchange=args.exchange,
            symbols=_parse_symbols(args.symbols),
            config=config,
        )
        publish_delta_5m_event_clock(payload, out=args.out, feed=args.feed)
        if args.print:
            print(render_report(payload))
        if args.once:
            break
        time.sleep(max(1, int(args.interval_seconds)))


if __name__ == "__main__":
    main()
