"""Forward evidence and untouched-market validation for the MTF/AMF scanner.

This module is intentionally research-only.  It journals each new closed-candle
alert once, resolves fixed-horizon outcomes from subsequently completed candles,
and publishes read-only evidence for the dashboard.  L2 features are attached as
context only: they never create, filter, size, route, or promote a signal.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import numpy as np
import pandas as pd

from vnedge.execution.journal import DecisionJournal
from vnedge.research.mtf_amf_rejection_scanner import (
    DEFAULT_CONFIG,
    SCANNER_ID,
    MtfAmfScannerConfig,
    fetch_delta_public_candles,
    scan_mtf_amf_rejections,
)

HORIZON_HOURS: tuple[int, ...] = (1, 4, 12, 24)
ROUND_TRIP_COST_BPS = 12.0
FRESH_ALERT_MINUTES = 75
ROLLING_SAMPLE_SIZE = 30
DEFAULT_LIVE_JOURNAL = Path("research/live_research/mtf_amf_alerts.jsonl")
DEFAULT_EVIDENCE_OUT = Path(
    "research/live_research/mtf_amf_forward_evidence_latest.json"
)
DEFAULT_BACKTEST_OUT = Path(
    "research/live_research/mtf_amf_expanded_backtest_latest.json"
)
DEFAULT_MARKETS: tuple[str, ...] = (
    "BTCUSD",
    "ETHUSD",
    "SOLUSD",
    "XRPUSD",
    "BNBUSD",
    "DOGEUSD",
    "LINKUSD",
    "AAVEUSD",
)
DEFAULT_ANALYSIS_START = datetime(2025, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class L2Confirmation:
    status: str
    observed_at: str | None
    age_seconds: float | None
    weighted_obi: float | None
    trade_flow_imbalance_5s: float | None
    microprice_deviation_bps: float | None
    spread_bps: float | None
    feed_age_ms: float | None
    book_valid: bool | None
    context_only: bool = True
    used_for_signal: bool = False
    used_for_execution: bool = False
    used_for_promotion: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def scanner_alert_id(alert: Mapping[str, Any]) -> str:
    """Return a stable identity independent of process time and restarts."""

    identity = "|".join(
        (
            str(alert.get("scanner_id") or SCANNER_ID),
            str(alert.get("symbol") or "").upper(),
            str(alert.get("bar_start") or ""),
            str(alert.get("side") or "").lower(),
        )
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _aware_utc(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(stamp):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize(UTC)
    else:
        stamp = stamp.tz_convert(UTC)
    return stamp.to_pydatetime()


def _canonical_candles(frame: pd.DataFrame) -> pd.DataFrame:
    required = {"timestamp", "open", "high", "low", "close"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"candles missing columns: {sorted(missing)}")
    out = frame.copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True).astype(
        "datetime64[ns, UTC]"
    )
    for column in ("open", "high", "low", "close"):
        out[column] = pd.to_numeric(out[column], errors="coerce")
    if out[["timestamp", "open", "high", "low", "close"]].isna().any().any():
        raise ValueError("candles contain invalid values")
    return out.drop_duplicates("timestamp", keep="last").sort_values("timestamp").reset_index(
        drop=True
    )


def completed_candles(
    frame: pd.DataFrame, *, now: datetime, timeframe: timedelta = timedelta(hours=1)
) -> pd.DataFrame:
    """Return only candles whose entire interval is known at ``now``."""

    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    candles = _canonical_candles(frame)
    cutoff = pd.Timestamp(now.astimezone(UTC))
    return candles.loc[candles["timestamp"] + timeframe <= cutoff].reset_index(drop=True)


def read_l2_confirmation(
    l2_root: Path | str | None,
    *,
    symbol: str,
    at: datetime,
    side: str,
    lookback_seconds: int = 120,
    max_files: int = 12,
) -> L2Confirmation:
    """Read the latest causal L2 feature row at or before an alert timestamp.

    Only a small tail of recorder segments is inspected because this function
    supports fresh live alerts, not historical L2 backfilling. Missing or stale
    tape produces ``unavailable`` and never changes the candle signal.
    """

    unavailable = L2Confirmation(
        status="unavailable",
        observed_at=None,
        age_seconds=None,
        weighted_obi=None,
        trade_flow_imbalance_5s=None,
        microprice_deviation_bps=None,
        spread_bps=None,
        feed_age_ms=None,
        book_valid=None,
    )
    if l2_root is None:
        return unavailable
    root = Path(l2_root)
    files = sorted(root.glob("**/events_*.parquet"))[-max_files:]
    if not files:
        return unavailable
    columns = [
        "recv_ts_us",
        "symbol",
        "weighted_obi",
        "tfi_5s",
        "microprice_dev_bps",
        "spread_bps",
        "feed_age_ms",
        "book_valid",
    ]
    frames: list[pd.DataFrame] = []
    at_us = int(at.astimezone(UTC).timestamp() * 1_000_000)
    start_us = at_us - lookback_seconds * 1_000_000
    for path in files:
        try:
            frame = pd.read_parquet(path, columns=columns)
        except (OSError, ValueError):
            continue
        mask = (
            frame["symbol"].astype(str).str.upper().eq(symbol.upper())
            & frame["recv_ts_us"].between(start_us, at_us)
            & frame["weighted_obi"].notna()
        )
        if mask.any():
            frames.append(frame.loc[mask])
    if not frames:
        return unavailable
    row = pd.concat(frames, ignore_index=True).sort_values("recv_ts_us").iloc[-1]
    row_time = datetime.fromtimestamp(float(row["recv_ts_us"]) / 1_000_000, tz=UTC)
    obi = _finite_or_none(row.get("weighted_obi"))
    flow = _finite_or_none(row.get("tfi_5s"))
    direction = 1.0 if str(side).lower() == "long" else -1.0
    signed = [direction * value for value in (obi, flow) if value is not None]
    if len(signed) < 2:
        status = "partial"
    elif all(value > 0 for value in signed):
        status = "aligned"
    elif all(value < 0 for value in signed):
        status = "opposed"
    else:
        status = "mixed"
    return L2Confirmation(
        status=status,
        observed_at=row_time.isoformat(),
        age_seconds=max(0.0, (at.astimezone(UTC) - row_time).total_seconds()),
        weighted_obi=obi,
        trade_flow_imbalance_5s=flow,
        microprice_deviation_bps=_finite_or_none(row.get("microprice_dev_bps")),
        spread_bps=_finite_or_none(row.get("spread_bps")),
        feed_age_ms=_finite_or_none(row.get("feed_age_ms")),
        book_valid=bool(row.get("book_valid")) if pd.notna(row.get("book_valid")) else None,
    )


def _finite_or_none(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def journal_fresh_alerts(
    payload: dict[str, Any],
    candles_by_symbol: Mapping[str, pd.DataFrame],
    *,
    journal: DecisionJournal,
    now: datetime,
    l2_root: Path | str | None = None,
) -> int:
    """Append each fresh alert once and attach its L2 context to the snapshot."""

    existing = {
        str(record.get("payload", {}).get("alert_id"))
        for record in journal.read_all()
        if record.get("kind") == "scanner_alert"
    }
    appended = 0
    symbols = payload.get("symbols") if isinstance(payload.get("symbols"), dict) else {}
    for symbol, report in symbols.items():
        if not isinstance(report, dict):
            continue
        summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
        alert = summary.get("latest_alert") if isinstance(summary.get("latest_alert"), dict) else None
        if alert is None:
            continue
        observed = _aware_utc(alert.get("observed_at"))
        if observed is None:
            continue
        age = (now.astimezone(UTC) - observed).total_seconds()
        if age < 0 or age > FRESH_ALERT_MINUTES * 60:
            continue
        alert_id = scanner_alert_id(alert)
        if alert_id in existing:
            prior = _journal_alert(journal.read_all(), alert_id)
            if prior is not None:
                alert["l2_confirmation"] = prior.get("l2_confirmation")
            continue
        raw = candles_by_symbol.get(str(symbol).upper())
        if raw is None:
            continue
        candles = _canonical_candles(raw)
        entry_row = candles.loc[candles["timestamp"] == pd.Timestamp(observed)]
        if entry_row.empty:
            continue
        entry_price = float(entry_row.iloc[0]["open"])
        confirmation = read_l2_confirmation(
            l2_root,
            symbol=str(symbol),
            at=observed,
            side=str(alert.get("side") or ""),
        )
        record = {
            "alert_id": alert_id,
            "scanner_id": str(alert.get("scanner_id") or SCANNER_ID),
            "market": str(symbol).upper(),
            "symbol": str(symbol).upper(),
            "bar_start": str(alert.get("bar_start")),
            "observed_at": observed.isoformat(),
            "entry_time": observed.isoformat(),
            "entry_price": entry_price,
            "entry_basis": "next_1h_candle_open",
            "side": str(alert.get("side") or "").lower(),
            "setup": str(alert.get("setup") or ""),
            "l2_confirmation": confirmation.to_dict(),
            "research_only": True,
            "can_trade": False,
            "can_promote": False,
        }
        if journal.append("scanner_alert", record):
            existing.add(alert_id)
            appended += 1
            alert["l2_confirmation"] = confirmation.to_dict()
    return appended


def _journal_alert(records: Iterable[dict[str, Any]], alert_id: str) -> dict[str, Any] | None:
    for record in records:
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        if record.get("kind") == "scanner_alert" and payload.get("alert_id") == alert_id:
            return payload
    return None


def _outcome_for_horizon(
    candles: pd.DataFrame,
    alert: Mapping[str, Any],
    *,
    horizon_hours: int,
    round_trip_cost_bps: float,
) -> dict[str, Any] | None:
    entry_time = pd.Timestamp(alert["entry_time"])
    if entry_time.tzinfo is None:
        entry_time = entry_time.tz_localize(UTC)
    else:
        entry_time = entry_time.tz_convert(UTC)
    matches = candles.index[candles["timestamp"] == entry_time]
    if len(matches) != 1:
        return None
    start = int(matches[0])
    end = start + horizon_hours - 1
    if end >= len(candles):
        return None
    path = candles.iloc[start : end + 1]
    entry = float(alert["entry_price"])
    exit_price = float(path.iloc[-1]["close"])
    direction = 1.0 if str(alert.get("side")) == "long" else -1.0
    gross = direction * (exit_price / entry - 1.0) * 10_000.0
    if direction > 0:
        mfe = (float(path["high"].max()) / entry - 1.0) * 10_000.0
        mae = (float(path["low"].min()) / entry - 1.0) * 10_000.0
    else:
        mfe = (1.0 - float(path["low"].min()) / entry) * 10_000.0
        mae = (1.0 - float(path["high"].max()) / entry) * 10_000.0
    net = gross - round_trip_cost_bps
    return {
        "outcome_id": f"{alert['alert_id']}:{horizon_hours}h",
        "alert_id": alert["alert_id"],
        "scanner_id": alert.get("scanner_id", SCANNER_ID),
        "market": alert["market"],
        "symbol": alert["symbol"],
        "side": alert["side"],
        "entry_time": alert["entry_time"],
        "entry_price": entry,
        "exit_time": pd.Timestamp(path.iloc[-1]["timestamp"] + pd.Timedelta(hours=1)).isoformat(),
        "exit_price": exit_price,
        "horizon_hours": horizon_hours,
        "gross_return_bps": gross,
        "round_trip_cost_bps": round_trip_cost_bps,
        "net_return_bps": net,
        "mfe_bps": mfe,
        "mae_bps": mae,
        "false_signal": net <= 0.0,
        "research_only": True,
        "can_trade": False,
        "can_promote": False,
    }


def resolve_forward_outcomes(
    candles_by_symbol: Mapping[str, pd.DataFrame],
    *,
    journal: DecisionJournal,
    now: datetime,
    horizons: Iterable[int] = HORIZON_HOURS,
    round_trip_cost_bps: float = ROUND_TRIP_COST_BPS,
) -> int:
    """Resolve all due alert/horizon pairs using completed candles only."""

    records = journal.read_all()
    alerts = [
        record["payload"]
        for record in records
        if record.get("kind") == "scanner_alert" and isinstance(record.get("payload"), dict)
    ]
    resolved_ids = {
        str(record.get("payload", {}).get("outcome_id"))
        for record in records
        if record.get("kind") == "scanner_forward_outcome"
    }
    appended = 0
    for alert in alerts:
        raw = candles_by_symbol.get(str(alert.get("symbol") or "").upper())
        if raw is None:
            continue
        candles = completed_candles(raw, now=now)
        for horizon in horizons:
            outcome_id = f"{alert.get('alert_id')}:{int(horizon)}h"
            if outcome_id in resolved_ids:
                continue
            outcome = _outcome_for_horizon(
                candles,
                alert,
                horizon_hours=int(horizon),
                round_trip_cost_bps=round_trip_cost_bps,
            )
            if outcome is not None and journal.append("scanner_forward_outcome", outcome):
                resolved_ids.add(outcome_id)
                appended += 1
    return appended


def _profit_factor(values: Iterable[float]) -> float | None:
    vals = tuple(float(value) for value in values)
    if not vals:
        return None
    gain = sum(value for value in vals if value > 0)
    loss = -sum(value for value in vals if value < 0)
    if loss == 0:
        # Keep JSON standards-compliant and promotion conservative: an
        # all-winning tiny sample has no finite PF yet, so it cannot clear the
        # PF gate until a denominator is actually observed.
        return None
    return gain / loss


def _metric_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    values = [float(row["net_return_bps"]) for row in rows]
    return {
        "samples": len(rows),
        "expectancy_bps": float(np.mean(values)) if values else None,
        "median_net_bps": float(np.median(values)) if values else None,
        "profit_factor": _profit_factor(values),
        "win_rate_pct": 100.0 * sum(value > 0 for value in values) / len(values)
        if values
        else None,
        "false_signal_rate_pct": 100.0 * sum(value <= 0 for value in values) / len(values)
        if values
        else None,
        "avg_mfe_bps": float(np.mean([float(row["mfe_bps"]) for row in rows]))
        if rows
        else None,
        "avg_mae_bps": float(np.mean([float(row["mae_bps"]) for row in rows]))
        if rows
        else None,
        "net_bps_sum": float(sum(values)),
    }


def build_forward_evidence_payload(
    journal: DecisionJournal,
    *,
    now: datetime | None = None,
    backtest_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(UTC)
    records = journal.read_all()
    alerts = [
        record["payload"]
        for record in records
        if record.get("kind") == "scanner_alert" and isinstance(record.get("payload"), dict)
    ]
    outcomes = [
        record["payload"]
        for record in records
        if record.get("kind") == "scanner_forward_outcome"
        and isinstance(record.get("payload"), dict)
    ]
    by_horizon: dict[str, dict[str, Any]] = {}
    for horizon in HORIZON_HOURS:
        rows = sorted(
            (row for row in outcomes if int(row.get("horizon_hours") or 0) == horizon),
            key=lambda row: str(row.get("entry_time") or ""),
        )
        by_horizon[f"{horizon}h"] = {
            **_metric_summary(rows),
            "rolling_last_30": _metric_summary(rows[-ROLLING_SAMPLE_SIZE:]),
        }
    markets: dict[str, Any] = {}
    for market in sorted({str(row.get("market")) for row in outcomes}):
        markets[market] = {
            f"{horizon}h": _metric_summary(
                [
                    row
                    for row in outcomes
                    if row.get("market") == market
                    and int(row.get("horizon_hours") or 0) == horizon
                ]
            )
            for horizon in HORIZON_HOURS
        }
    confirmations = defaultdict(int)
    for alert in alerts:
        confirmation = alert.get("l2_confirmation")
        if isinstance(confirmation, dict):
            confirmations[str(confirmation.get("status") or "unavailable")] += 1
    backtest = backtest_payload or {}
    promotion = backtest.get("promotion") if isinstance(backtest.get("promotion"), dict) else {}
    return {
        "generated_at": current.astimezone(UTC).isoformat(),
        "report_id": "mtf_amf_forward_evidence_v1",
        "scanner_id": SCANNER_ID,
        "mode": "research_observation_only",
        "cost_model": {"round_trip_cost_bps": ROUND_TRIP_COST_BPS},
        "summary": {
            "journaled_alerts": len(alerts),
            "resolved_outcomes": len(outcomes),
            "pending_24h": max(
                0,
                len(alerts)
                - sum(int(row.get("horizon_hours") or 0) == 24 for row in outcomes),
            ),
            "l2_confirmation_counts": dict(sorted(confirmations.items())),
        },
        "horizons": by_horizon,
        "market_breakdown": markets,
        "recent_alerts": alerts[-20:][::-1],
        "promotion": promotion
        or {
            "verdict": "INSUFFICIENT_UNTOUCHED_BACKTEST",
            "eligible_for_paper_review": False,
            "paper_trading_enabled": False,
            "gates": [],
        },
        "policy": {
            "l2_is_confirmation_only": True,
            "l2_can_trigger_execution": False,
            "no_repainting": True,
            "completed_candles_only": True,
            "can_trade": False,
            "can_promote": False,
            "paper_requires_all_gates_and_human_approval": True,
            "order_route_present": False,
        },
        "can_trade": False,
        "can_promote": False,
    }


def publish_json(payload: dict[str, Any], path: Path | str) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w", dir=target.parent, prefix=target.name, suffix=".tmp", delete=False, encoding="utf-8"
    ) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
        tmp = Path(handle.name)
    tmp.replace(target)
    return target


def _backtest_rows(
    one_hour: pd.DataFrame,
    four_hour: pd.DataFrame,
    *,
    symbol: str,
    config: MtfAmfScannerConfig,
    cost_bps: float,
) -> list[dict[str, Any]]:
    candles = _canonical_candles(one_hour)
    alerts = scan_mtf_amf_rejections(candles, four_hour, symbol=symbol, config=config)
    rows: list[dict[str, Any]] = []
    for alert in alerts:
        bar_positions = candles.index[candles["timestamp"] == pd.Timestamp(alert.bar_start)]
        if len(bar_positions) != 1 or int(bar_positions[0]) + 1 >= len(candles):
            continue
        entry_pos = int(bar_positions[0]) + 1
        entry_time = pd.Timestamp(candles.iloc[entry_pos]["timestamp"])
        base = {
            "alert_id": scanner_alert_id(alert.to_dict()),
            "scanner_id": SCANNER_ID,
            "market": symbol,
            "symbol": symbol,
            "side": alert.side,
            "entry_time": entry_time.isoformat(),
            "entry_price": float(candles.iloc[entry_pos]["open"]),
        }
        for horizon in HORIZON_HOURS:
            outcome = _outcome_for_horizon(
                candles,
                base,
                horizon_hours=horizon,
                round_trip_cost_bps=cost_bps,
            )
            if outcome is not None:
                rows.append(outcome)
    return rows


def _positive_concentration(values: Mapping[str, float]) -> float | None:
    positives = [max(0.0, float(value)) for value in values.values()]
    total = sum(positives)
    return 100.0 * max(positives) / total if total > 0 else None


def _period_breakdown(rows: list[dict[str, Any]], period: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        stamp = _aware_utc(row.get("entry_time"))
        if stamp is None:
            continue
        if period == "daily":
            key = stamp.strftime("%Y-%m-%d")
        elif period == "weekly":
            iso = stamp.isocalendar()
            key = f"{iso.year}-W{iso.week:02d}"
        elif period == "monthly":
            key = stamp.strftime("%Y-%m")
        elif period == "quarterly":
            key = f"{stamp.year}-Q{((stamp.month - 1) // 3) + 1}"
        else:
            raise ValueError(f"unsupported period: {period}")
        grouped[key].append(row)
    return [
        {"period": key, **_metric_summary(group)} for key, group in sorted(grouped.items())
    ]


def _promotion_assessment(
    rows: list[dict[str, Any]],
    *,
    development_cutoff: datetime,
) -> dict[str, Any]:
    development = [
        row for row in rows if (_aware_utc(row.get("entry_time")) or development_cutoff) < development_cutoff
    ]
    def development_expectancy(horizon: int) -> float:
        value = _metric_summary(
            [row for row in development if int(row["horizon_hours"]) == horizon]
        )["expectancy_bps"]
        return float(value) if value is not None else -math.inf

    selected = max(
        HORIZON_HOURS,
        key=lambda horizon: (
            development_expectancy(horizon),
            -horizon,
        ),
    )
    untouched = [
        row
        for row in rows
        if int(row["horizon_hours"]) == selected
        and (_aware_utc(row.get("entry_time")) or datetime.min.replace(tzinfo=UTC))
        >= development_cutoff
    ]
    overall = _metric_summary(untouched)
    market_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    month_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in untouched:
        market_rows[str(row["market"])].append(row)
        stamp = _aware_utc(row.get("entry_time"))
        month_rows[stamp.strftime("%Y-%m") if stamp else "unknown"].append(row)
    market_net = {key: _metric_summary(group)["net_bps_sum"] for key, group in market_rows.items()}
    month_net = {key: _metric_summary(group)["net_bps_sum"] for key, group in month_rows.items()}
    positive_markets = sum(_metric_summary(group)["expectancy_bps"] > 0 for group in market_rows.values())
    market_concentration = _positive_concentration(market_net)
    month_concentration = _positive_concentration(month_net)

    gate_values = (
        (
            "positive_untouched_markets",
            positive_markets >= 2,
            positive_markets,
            ">= 2",
        ),
        (
            "profit_factor_after_12bps",
            overall["profit_factor"] is not None and overall["profit_factor"] > 1.2,
            overall["profit_factor"],
            "> 1.2",
        ),
        (
            "single_market_positive_contribution",
            market_concentration is not None and market_concentration <= 60.0,
            market_concentration,
            "<= 60%",
        ),
        (
            "single_month_positive_contribution",
            month_concentration is not None and month_concentration <= 40.0,
            month_concentration,
            "<= 40%",
        ),
        (
            "minimum_untouched_samples",
            len(untouched) >= 30,
            len(untouched),
            ">= 30",
        ),
        ("no_repainting", True, True, "required"),
        ("completed_candles_only", True, True, "required"),
    )
    gates = [
        {"name": name, "passed": passed, "value": value, "requirement": requirement}
        for name, passed, value, requirement in gate_values
    ]
    passed = all(gate[1] for gate in gate_values)
    return {
        "verdict": "ELIGIBLE_FOR_PAPER_REVIEW" if passed else "REJECTED_BY_LOCKED_GATES",
        "eligible_for_paper_review": passed,
        "paper_trading_enabled": False,
        "human_approval_required": True,
        "selected_horizon_hours": selected,
        "horizon_selection": "highest development expectancy; untouched window never selects",
        "development_cutoff": development_cutoff.isoformat(),
        "untouched_summary": overall,
        "untouched_positive_markets": positive_markets,
        "market_positive_concentration_pct": market_concentration,
        "month_positive_concentration_pct": month_concentration,
        "market_breakdown": {
            key: _metric_summary(group) for key, group in sorted(market_rows.items())
        },
        "month_breakdown": {
            key: _metric_summary(group) for key, group in sorted(month_rows.items())
        },
        "gates": gates,
        "l2_used_in_assessment": False,
    }


def build_expanded_backtest_payload(
    candles_by_symbol: Mapping[str, tuple[pd.DataFrame, pd.DataFrame]],
    *,
    now: datetime | None = None,
    config: MtfAmfScannerConfig = DEFAULT_CONFIG,
    cost_bps: float = ROUND_TRIP_COST_BPS,
    untouched_fraction: float = 0.25,
    analysis_start: datetime = DEFAULT_ANALYSIS_START,
) -> dict[str, Any]:
    """Backtest unchanged scanner thresholds over a broader Delta universe."""

    if not 0.1 <= untouched_fraction <= 0.5:
        raise ValueError("untouched_fraction must be in [0.1, 0.5]")
    if analysis_start.tzinfo is None:
        raise ValueError("analysis_start must be timezone-aware")
    current = now or datetime.now(UTC)
    rows: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    starts: list[datetime] = []
    ends: list[datetime] = []
    for symbol, (one, four) in candles_by_symbol.items():
        try:
            canonical = _canonical_candles(one)
            starts.append(pd.Timestamp(canonical.iloc[0]["timestamp"]).to_pydatetime())
            ends.append(pd.Timestamp(canonical.iloc[-1]["timestamp"]).to_pydatetime())
            market_rows = _backtest_rows(
                canonical,
                four,
                symbol=str(symbol).upper(),
                config=config,
                cost_bps=cost_bps,
            )
            rows.extend(
                row
                for row in market_rows
                if (_aware_utc(row.get("entry_time")) or analysis_start) >= analysis_start
            )
        except (IndexError, TypeError, ValueError) as exc:
            errors[str(symbol).upper()] = str(exc)
    if not starts or not ends:
        raise ValueError("no valid candle markets supplied")
    common_start = max(max(starts), analysis_start.astimezone(UTC))
    common_end = min(ends)
    cutoff = common_start + (common_end - common_start) * (1.0 - untouched_fraction)
    by_horizon = {
        f"{horizon}h": _metric_summary(
            [row for row in rows if int(row["horizon_hours"]) == horizon]
        )
        for horizon in HORIZON_HOURS
    }
    promotion = _promotion_assessment(rows, development_cutoff=cutoff)
    selected_rows = [
        row
        for row in rows
        if int(row["horizon_hours"]) == int(promotion["selected_horizon_hours"])
    ]
    return {
        "generated_at": current.astimezone(UTC).isoformat(),
        "report_id": "mtf_amf_expanded_delta_backtest_v1",
        "scanner_id": SCANNER_ID,
        "mode": "causal_expanded_market_backtest",
        "data_window": {
            "common_start": common_start.isoformat(),
            "common_end": common_end.isoformat(),
            "development_cutoff": cutoff.isoformat(),
            "untouched_fraction": untouched_fraction,
        },
        "markets": sorted(candles_by_symbol),
        "errors": errors,
        "config": asdict(config),
        "cost_model": {"round_trip_cost_bps": cost_bps},
        "summary": {
            "outcome_rows": len(rows),
            "alerts": len({row["alert_id"] for row in rows}),
            "healthy_markets": len(candles_by_symbol) - len(errors),
            "error_markets": len(errors),
            "horizons": by_horizon,
        },
        "promotion": promotion,
        "period_breakdown": {
            period: _period_breakdown(selected_rows, period)
            for period in ("daily", "weekly", "monthly", "quarterly")
        },
        "policy": {
            "thresholds_unchanged_across_markets": True,
            "next_open_entries": True,
            "completed_candles_only": True,
            "no_repainting": True,
            "l2_used_in_backtest": False,
            "can_trade": False,
            "can_promote": False,
            "paper_requires_all_gates_and_human_approval": True,
        },
        "rows": rows,
        "can_trade": False,
        "can_promote": False,
    }


def fetch_and_build_expanded_backtest(
    symbols: Iterable[str] = DEFAULT_MARKETS,
    *,
    days: int = 590,
    now: datetime | None = None,
    analysis_start: datetime = DEFAULT_ANALYSIS_START,
) -> dict[str, Any]:
    current = now or datetime.now(UTC)
    frames: dict[str, tuple[pd.DataFrame, pd.DataFrame]] = {}
    errors: dict[str, str] = {}
    requested = tuple(dict.fromkeys(str(symbol).strip().upper() for symbol in symbols))
    for symbol in requested:
        try:
            frames[symbol] = (
                fetch_delta_public_candles(symbol, "1h", days=days, now=current),
                fetch_delta_public_candles(symbol, "4h", days=days, now=current),
            )
        except (OSError, TimeoutError, TypeError, ValueError) as exc:
            errors[symbol] = str(exc)
    payload = build_expanded_backtest_payload(
        frames,
        now=current,
        analysis_start=analysis_start,
    )
    payload["errors"].update(errors)
    payload["summary"]["requested_markets"] = len(requested)
    payload["summary"]["error_markets"] = len(payload["errors"])
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default=",".join(DEFAULT_MARKETS))
    parser.add_argument("--days", type=int, default=590)
    parser.add_argument("--start", default="2025-01-01T00:00:00+00:00")
    parser.add_argument("--out", type=Path, default=DEFAULT_BACKTEST_OUT)
    args = parser.parse_args()
    start = _aware_utc(args.start)
    if start is None:
        parser.error("--start must be an ISO-8601 timestamp")
    payload = fetch_and_build_expanded_backtest(
        args.symbols.split(","),
        days=args.days,
        analysis_start=start,
    )
    publish_json(payload, args.out)
    for period, rows in payload["period_breakdown"].items():
        pd.DataFrame(rows).to_csv(
            args.out.with_name(f"{args.out.stem}_{period}.csv"),
            index=False,
        )
    promotion = payload["promotion"]
    print(
        f"{payload['report_id']}: {payload['summary']['alerts']} alerts; "
        f"{promotion['verdict']}; can_trade=false paper_trading_enabled=false"
    )


if __name__ == "__main__":
    main()
