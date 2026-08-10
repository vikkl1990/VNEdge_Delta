"""Research-only cross-sectional factor ranking.

Inspired by IAF's pipeline-before-strategy workflow, this module ranks the
current research universe before expensive scanners run. It is deliberately a
slow-loop artifact: no order intent, promotion, or live decision can be derived
from this payload.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

import pandas as pd

from vnedge.data.parquet_store import ParquetStore
from vnedge.research.universe import ResearchTarget, load_research_targets


@dataclass(frozen=True)
class FactorRankerConfig:
    """Config for candle-based lane triage.

    The defaults are intentionally conservative: a lane needs enough fresh
    candles, visible range over the fee wall, and nonzero liquidity before it
    earns a READY state.
    """

    lookback_bars: int = 240
    momentum_bars: int = 48
    efficiency_bars: int = 48
    atr_bars: int = 48
    volume_bars: int = 48
    min_rows: int = 80
    max_rows: int = 50
    max_data_age_minutes: int = 180
    min_atr_bps: float = 18.0
    fee_wall_bps: float = 8.0
    liquidity_weight: float = 30.0
    trend_weight: float = 25.0
    range_weight: float = 20.0
    freshness_weight: float = 15.0
    coverage_weight: float = 10.0


@dataclass
class RawFactorRow:
    exchange: str
    symbol: str
    timeframe: str
    rows: int
    last_ts: str | None
    close: float | None
    avg_quote_volume: float
    momentum_pct: float
    volatility_bps: float
    efficiency_ratio: float
    atr_bps: float
    recency_minutes: float | None
    state: str
    reasons: list[str] = field(default_factory=list)

    @property
    def lane_key(self) -> str:
        return f"{self.exchange}|{self.symbol}|{self.timeframe}"

    @property
    def trend_quality(self) -> float:
        if not math.isfinite(self.momentum_pct) or not math.isfinite(self.efficiency_ratio):
            return 0.0
        return abs(self.momentum_pct) * max(self.efficiency_ratio, 0.0)


def _finite(value: object, default: float = 0.0) -> float:
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _timestamp_iso(value: object) -> str | None:
    if value is None or pd.isna(value):
        return None
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize(UTC)
    else:
        ts = ts.tz_convert(UTC)
    return ts.isoformat()


def _recency_minutes(value: object, now: datetime) -> float | None:
    if value is None or pd.isna(value):
        return None
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize(UTC)
    else:
        ts = ts.tz_convert(UTC)
    return max((pd.Timestamp(now) - ts).total_seconds() / 60.0, 0.0)


def _true_range(candles: pd.DataFrame) -> pd.Series:
    high = candles["high"].astype("float64")
    low = candles["low"].astype("float64")
    prev_close = candles["close"].astype("float64").shift(1)
    ranges = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    )
    return ranges.max(axis=1)


def _efficiency_ratio(close: pd.Series, bars: int) -> float:
    window = close.dropna().tail(max(bars + 1, 2))
    if len(window) < 2:
        return 0.0
    direction = abs(float(window.iloc[-1] - window.iloc[0]))
    path = float(window.diff().abs().sum())
    if path <= 0.0:
        return 0.0
    return max(min(direction / path, 1.0), 0.0)


def _momentum_pct(close: pd.Series, bars: int) -> float:
    window = close.dropna()
    if len(window) <= bars:
        return 0.0
    start = float(window.iloc[-bars - 1])
    end = float(window.iloc[-1])
    if start <= 0.0:
        return 0.0
    return (end / start) - 1.0


def _volatility_bps(close: pd.Series, bars: int) -> float:
    window = close.astype("float64").dropna().tail(max(bars + 1, 2))
    if len(window) < 3:
        return 0.0
    returns = (window / window.shift(1)).apply(lambda x: math.log(x) if x > 0 else math.nan)
    return _finite(returns.std(ddof=0) * 10_000.0)


def _atr_bps(candles: pd.DataFrame, bars: int) -> float:
    window = candles.dropna(subset=["high", "low", "close"]).tail(max(bars + 1, 2))
    if len(window) < 2:
        return 0.0
    atr = _finite(_true_range(window).tail(bars).mean())
    close = _finite(window["close"].iloc[-1])
    if close <= 0.0:
        return 0.0
    return atr / close * 10_000.0


def _raw_row(
    store: ParquetStore,
    target: ResearchTarget,
    config: FactorRankerConfig,
    now: datetime,
) -> RawFactorRow:
    try:
        candles = store.read_candles(target.exchange, target.symbol, target.timeframe)
    except FileNotFoundError:
        return RawFactorRow(
            exchange=target.exchange,
            symbol=target.symbol,
            timeframe=target.timeframe,
            rows=0,
            last_ts=None,
            close=None,
            avg_quote_volume=0.0,
            momentum_pct=0.0,
            volatility_bps=0.0,
            efficiency_ratio=0.0,
            atr_bps=0.0,
            recency_minutes=None,
            state="MISSING",
            reasons=["no candle dataset"],
        )

    required = {"timestamp", "open", "high", "low", "close", "volume"}
    if not required.issubset(candles.columns):
        missing = sorted(required - set(candles.columns))
        return RawFactorRow(
            exchange=target.exchange,
            symbol=target.symbol,
            timeframe=target.timeframe,
            rows=len(candles),
            last_ts=None,
            close=None,
            avg_quote_volume=0.0,
            momentum_pct=0.0,
            volatility_bps=0.0,
            efficiency_ratio=0.0,
            atr_bps=0.0,
            recency_minutes=None,
            state="MISSING",
            reasons=[f"missing columns: {','.join(missing)}"],
        )

    candles = candles.sort_values("timestamp").tail(config.lookback_bars).reset_index(drop=True)
    rows = len(candles)
    if rows == 0:
        return RawFactorRow(
            exchange=target.exchange,
            symbol=target.symbol,
            timeframe=target.timeframe,
            rows=0,
            last_ts=None,
            close=None,
            avg_quote_volume=0.0,
            momentum_pct=0.0,
            volatility_bps=0.0,
            efficiency_ratio=0.0,
            atr_bps=0.0,
            recency_minutes=None,
            state="MISSING",
            reasons=["empty candle dataset"],
        )

    close = candles["close"].astype("float64")
    last_close = _finite(close.iloc[-1], default=math.nan)
    last_ts = candles["timestamp"].iloc[-1]
    recency = _recency_minutes(last_ts, now)
    quote_volume = candles["close"].astype("float64") * candles["volume"].astype("float64")
    avg_quote_volume = _finite(quote_volume.tail(config.volume_bars).mean())
    momentum = _momentum_pct(close, config.momentum_bars)
    volatility = _volatility_bps(close, config.momentum_bars)
    efficiency = _efficiency_ratio(close, config.efficiency_bars)
    atr = _atr_bps(candles, config.atr_bars)

    reasons: list[str] = []
    state = "READY"
    if rows < config.min_rows:
        state = "UNDER_SAMPLED"
        reasons.append(f"rows {rows} < {config.min_rows}")
    elif recency is None or recency > config.max_data_age_minutes:
        state = "STALE"
        reason = "missing latest timestamp"
        if recency is not None:
            reason = f"age {recency:.1f}m > {config.max_data_age_minutes}m"
        reasons.append(reason)
    elif atr < config.min_atr_bps:
        state = "LOW_RANGE"
        reasons.append(f"ATR {atr:.1f}bps < {config.min_atr_bps:.1f}bps")
    elif avg_quote_volume <= 0.0:
        state = "ILLQUID"
        reasons.append("avg quote volume <= 0")

    return RawFactorRow(
        exchange=target.exchange,
        symbol=target.symbol,
        timeframe=target.timeframe,
        rows=rows,
        last_ts=_timestamp_iso(last_ts),
        close=last_close if math.isfinite(last_close) else None,
        avg_quote_volume=avg_quote_volume,
        momentum_pct=momentum,
        volatility_bps=volatility,
        efficiency_ratio=efficiency,
        atr_bps=atr,
        recency_minutes=recency,
        state=state,
        reasons=reasons,
    )


def _normalize(values: Iterable[float]) -> list[float]:
    vals = [_finite(v, default=0.0) for v in values]
    finite = [v for v in vals if math.isfinite(v)]
    if not finite:
        return [0.0 for _ in vals]
    low = min(finite)
    high = max(finite)
    if math.isclose(high, low):
        return [0.5 if math.isfinite(v) else 0.0 for v in vals]
    return [max(min((v - low) / (high - low), 1.0), 0.0) for v in vals]


def _recommended_action(row: RawFactorRow, score: float) -> str:
    if row.state == "READY" and score >= 65.0:
        return "scan_now"
    if row.state == "READY":
        return "keep_warm"
    if row.state in {"MISSING", "UNDER_SAMPLED", "STALE"}:
        return "backfill_or_record"
    if row.state in {"LOW_RANGE", "ILLQUID"}:
        return "watch_only"
    return "review"


def build_factor_ranker_payload(
    store: ParquetStore,
    targets: Iterable[ResearchTarget],
    *,
    config: FactorRankerConfig | None = None,
    now: datetime | None = None,
) -> dict:
    """Rank lanes by candle-derived scanner suitability.

    This is a triage surface, not a model score: it helps decide where scanner
    work should happen first and explains why lanes are quiet.
    """

    cfg = config or FactorRankerConfig()
    as_of = now or datetime.now(UTC)
    raw_rows = [_raw_row(store, target, cfg, as_of) for target in targets]

    liquidity_norm = _normalize(math.log10(max(r.avg_quote_volume, 1.0)) for r in raw_rows)
    trend_norm = _normalize(r.trend_quality for r in raw_rows)
    range_norm = _normalize(max(r.atr_bps - cfg.fee_wall_bps, 0.0) for r in raw_rows)
    freshness_norm = [
        0.0 if r.recency_minutes is None else 1.0 - min(r.recency_minutes / cfg.max_data_age_minutes, 1.0)
        for r in raw_rows
    ]
    coverage_norm = [min(r.rows / cfg.lookback_bars, 1.0) for r in raw_rows]

    scored: list[dict] = []
    for idx, raw in enumerate(raw_rows):
        score = (
            cfg.liquidity_weight * liquidity_norm[idx]
            + cfg.trend_weight * trend_norm[idx]
            + cfg.range_weight * range_norm[idx]
            + cfg.freshness_weight * freshness_norm[idx]
            + cfg.coverage_weight * coverage_norm[idx]
        )
        if raw.state != "READY":
            score *= 0.35
        direction = "flat"
        if raw.momentum_pct > 0.001:
            direction = "up"
        elif raw.momentum_pct < -0.001:
            direction = "down"
        scored.append(
            {
                **asdict(raw),
                "lane_key": raw.lane_key,
                "score": round(score, 2),
                "direction": direction,
                "expected_move_room_bps": round(max(raw.atr_bps - cfg.fee_wall_bps, 0.0), 2),
                "recommended_action": _recommended_action(raw, score),
            }
        )

    scored.sort(key=lambda r: (-_finite(r["score"]), str(r["lane_key"])))
    for rank, row in enumerate(scored, start=1):
        row["rank"] = rank

    counts: dict[str, int] = {}
    for row in scored:
        counts[row["state"]] = counts.get(row["state"], 0) + 1

    top = scored[: max(cfg.max_rows, 0)]
    return {
        "generated_at": as_of.isoformat(),
        "policy": {
            "source": "IAF-style cross-sectional factor ranker adapted for VNEDGE",
            "research_only": True,
            "can_trade": False,
            "can_promote": False,
            "lookahead_safe": True,
            "uses_only_closed_candles": True,
        },
        "config": asdict(cfg),
        "summary": {
            "targets": len(scored),
            "rows": len(top),
            "ready": counts.get("READY", 0),
            "missing": counts.get("MISSING", 0),
            "stale": counts.get("STALE", 0),
            "under_sampled": counts.get("UNDER_SAMPLED", 0),
            "low_range": counts.get("LOW_RANGE", 0),
            "illiquid": counts.get("ILLQUID", 0),
        },
        "rows": top,
        "top_scan_now": [r for r in top if r["recommended_action"] == "scan_now"][:10],
        "blockers_by_state": {
            state: [r["lane_key"] for r in scored if r["state"] == state]
            for state in sorted(counts)
            if state != "READY"
        },
    }


def write_factor_ranker_payload(payload: dict, out_dir: Path | str) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "factor_ranker.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rank VNEDGE research lanes by candle factors")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--out", default="research/live_research")
    parser.add_argument("--timeframe", default=None)
    parser.add_argument("--max-rows", type=int, default=50)
    parser.add_argument("--json", action="store_true", help="print payload JSON to stdout")
    args = parser.parse_args(argv)

    targets = load_research_targets(timeframe=args.timeframe)
    config = FactorRankerConfig(max_rows=args.max_rows)
    payload = build_factor_ranker_payload(ParquetStore(args.data_root), targets, config=config)
    write_factor_ranker_payload(payload, args.out)
    if args.json:
        print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
