"""Taker-only event-family replay over backfilled aggTrades history.

    python -m vnedge.research.event_taker_replay --days 20260701,20260702

Runs the taker-side event hypotheses (families aligned with the alpha
factory: forced_flow_continuation, volatility_impulse) over the historical
trade tape produced by vnedge.data.aggtrades_backfill. History has NO
order-book tape, so this replay makes ZERO maker assumptions:

- entry is a TAKER order at the NEXT trade print AFTER the signal + slippage
  (a same-print fill would assume zero reaction latency; we don't do that)
- exit is a TAKER order at the triggering trade print + slippage
- taker fee on BOTH legs; round-trip cost = 2 * (taker_bps + slippage_bps)
- a pending entry whose next print arrives too late is dropped as MISSED

Signals come from trade-derived features only (signed-flow z-score burst,
price velocity, volatility impulse) via TradeFlowEngine — the book-driven
ImbalanceScalper cannot run on this tape and is deliberately not used.

Research-only, same hard guards as every discovery layer: can_trade=false,
can_promote=false; a CANDIDATE row is a hypothesis for pre-registered
untouched judgment and human approval, never a signal.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from math import log, sqrt
from pathlib import Path
from statistics import mean, pstdev
from typing import Protocol

from vnedge.scalping.microstructure import TradeTick
from vnedge.scalping.replay_backtester import load_tick_events

logger = logging.getLogger(__name__)

EVENT_TAKER_REPLAY_ID = "event_taker_replay_v1"
EVENT_TAKER_LATEST = "event_taker_replay.json"
DEFAULT_HIST_EXCHANGE = "binanceusdm_hist"
# families must stay aligned with the alpha-factory / parameter-registry naming
TAKER_EVENT_FAMILIES = ("forced_flow_continuation", "volatility_impulse")


def event_taker_policy() -> dict:
    return {
        "status": "research_only",
        "can_trade": False,
        "can_promote": False,
        "requires_untouched_judgment": True,
        "requires_human_approval": True,
        "replay_id": EVENT_TAKER_REPLAY_ID,
        "execution_model": "taker_only",
        "data_source": "binance_vision_aggtrades_backfill",
        "families": list(TAKER_EVENT_FAMILIES),
        "principle": (
            "historical aggTrades tape has no order book; entries and exits "
            "are both taker + slippage — no maker fills are ever assumed"
        ),
    }


# --- Costs -------------------------------------------------------------------------

@dataclass(frozen=True)
class TakerFees:
    """Taker-only round trip: taker fee + slippage on BOTH legs."""

    taker_bps: float = 5.0
    slippage_bps: float = 1.0

    @property
    def round_trip_cost_bps(self) -> float:
        return 2.0 * (self.taker_bps + self.slippage_bps)


# --- Trade-derived features --------------------------------------------------------

@dataclass(frozen=True)
class TradeFlowFeatures:
    ts_ms: int
    last_price: float
    signed_flow_z: float          # burst signed notional vs baseline buckets
    burst_notional_usd: float
    price_velocity_bps: float     # move over the burst window
    short_vol_bps: float
    baseline_vol_bps: float
    vol_impulse_ratio: float      # short vol / baseline vol


@dataclass
class _Bucket:
    start_ms: int
    signed_notional: float = 0.0
    first_price: float = 0.0
    last_price: float = 0.0


class TradeFlowEngine:
    """Deterministic rolling trade-flow features on fixed time buckets.

    Trades are aggregated into `bucket_ms` buckets. The burst window is the
    newest `burst_buckets` buckets (including the in-progress one); the
    baseline is the trailing `baseline_buckets` CLOSED buckets. Gap buckets
    with no prints are filled with zero flow at the last price so quiet tape
    genuinely lowers the baseline instead of being skipped.
    """

    def __init__(self, *, bucket_ms: int = 1_000, burst_buckets: int = 3,
                 baseline_buckets: int = 60, min_baseline_buckets: int = 20) -> None:
        if bucket_ms <= 0 or burst_buckets < 1:
            raise ValueError("bucket_ms and burst_buckets must be positive")
        if baseline_buckets < min_baseline_buckets or min_baseline_buckets < 2:
            raise ValueError("baseline window must hold at least min_baseline_buckets >= 2")
        self.bucket_ms = bucket_ms
        self.burst_buckets = burst_buckets
        self.min_baseline_buckets = min_baseline_buckets
        self._closed: deque[_Bucket] = deque(maxlen=baseline_buckets)
        self._current: _Bucket | None = None

    def _close_through(self, bucket_start: int) -> None:
        cur = self._current
        if cur is None:
            return
        window = self._closed.maxlen or 0
        gap_buckets = (bucket_start - cur.start_ms) // self.bucket_ms
        if gap_buckets > window + 1:
            # fast-forward across a dead gap: only the trailing baseline
            # window can matter, so close the stale bucket and restart the
            # zero-flow fill just before the window (bounded, deterministic)
            self._closed.append(cur)
            cur = _Bucket(bucket_start - self.bucket_ms * (window + 1),
                          first_price=cur.last_price, last_price=cur.last_price)
        while cur.start_ms < bucket_start:
            self._closed.append(cur)
            cur = _Bucket(cur.start_ms + self.bucket_ms,
                          first_price=cur.last_price, last_price=cur.last_price)
        self._current = cur

    def on_trade(self, tick: TradeTick) -> TradeFlowFeatures | None:
        ts_ms = int(round(tick.event_time.timestamp() * 1000))
        bucket_start = ts_ms - ts_ms % self.bucket_ms
        if self._current is None:
            self._current = _Bucket(bucket_start, first_price=tick.price,
                                    last_price=tick.price)
        elif bucket_start > self._current.start_ms:
            self._close_through(bucket_start)
        self._current.signed_notional += tick.signed_notional_usd
        self._current.last_price = tick.price
        if self._current.first_price <= 0:
            self._current.first_price = tick.price
        return self._features(ts_ms, tick.price)

    def _features(self, ts_ms: int, price: float) -> TradeFlowFeatures | None:
        if len(self._closed) < self.min_baseline_buckets:
            return None
        closed = list(self._closed)
        burst_closed = closed[-(self.burst_buckets - 1):] if self.burst_buckets > 1 else []
        assert self._current is not None
        burst = self._current.signed_notional + sum(b.signed_notional for b in burst_closed)
        flows = [b.signed_notional for b in closed]
        flow_mu = mean(flows)
        flow_sd = pstdev(flows)
        n = self.burst_buckets
        z = 0.0
        if flow_sd > 0:
            z = (burst - n * flow_mu) / (flow_sd * sqrt(n))
        start_price = (burst_closed[0].first_price if burst_closed
                       else self._current.first_price)
        velocity = 0.0
        if start_price > 0:
            velocity = (price - start_price) / start_price * 10_000.0
        returns = [
            log(closed[i].last_price / closed[i - 1].last_price)
            for i in range(1, len(closed))
            if closed[i - 1].last_price > 0 and closed[i].last_price > 0
        ]
        short_n = min(10, len(returns))
        short_vol = pstdev(returns[-short_n:]) * 10_000.0 if short_n >= 2 else 0.0
        base_vol = pstdev(returns) * 10_000.0 if len(returns) >= 2 else 0.0
        impulse = short_vol / base_vol if base_vol > 0 else 0.0
        return TradeFlowFeatures(
            ts_ms=ts_ms,
            last_price=price,
            signed_flow_z=z,
            burst_notional_usd=burst,
            price_velocity_bps=velocity,
            short_vol_bps=short_vol,
            baseline_vol_bps=base_vol,
            vol_impulse_ratio=impulse,
        )


# --- Scalpers ----------------------------------------------------------------------

@dataclass(frozen=True)
class TakerSignal:
    side: str          # "buy" | "sell"
    stop_bps: float
    target_bps: float
    max_hold_ms: int

    def __post_init__(self) -> None:
        if self.side not in ("buy", "sell"):
            raise ValueError(f"invalid signal side: {self.side}")
        if self.stop_bps <= 0 or self.target_bps <= 0 or self.max_hold_ms <= 0:
            raise ValueError("stop_bps, target_bps, max_hold_ms must be positive")


class TradeEventScalper(Protocol):
    family: str

    def signal(self, features: TradeFlowFeatures) -> TakerSignal | None: ...


class ForcedFlowBurstScalper:
    """forced_flow_continuation on trade tape: a signed-volume z burst with
    agreeing price velocity is expected to continue over seconds."""

    family = "forced_flow_continuation"

    def __init__(self, *, z_entry: float = 3.0, min_velocity_bps: float = 1.0,
                 stop_bps: float = 8.0, target_bps: float = 12.0,
                 max_hold_ms: int = 30_000) -> None:
        self.z_entry = z_entry
        self.min_velocity_bps = min_velocity_bps
        self.stop_bps = stop_bps
        self.target_bps = target_bps
        self.max_hold_ms = max_hold_ms

    def signal(self, features: TradeFlowFeatures) -> TakerSignal | None:
        if (features.signed_flow_z >= self.z_entry
                and features.price_velocity_bps >= self.min_velocity_bps):
            return TakerSignal("buy", self.stop_bps, self.target_bps, self.max_hold_ms)
        if (features.signed_flow_z <= -self.z_entry
                and features.price_velocity_bps <= -self.min_velocity_bps):
            return TakerSignal("sell", self.stop_bps, self.target_bps, self.max_hold_ms)
        return None


class VolatilityImpulseScalper:
    """volatility_impulse on trade tape: volatility expansion with one-sided
    aggressive flow; join the flow side."""

    family = "volatility_impulse"

    def __init__(self, *, min_impulse_ratio: float = 2.0, min_flow_z: float = 1.5,
                 stop_bps: float = 10.0, target_bps: float = 15.0,
                 max_hold_ms: int = 20_000) -> None:
        self.min_impulse_ratio = min_impulse_ratio
        self.min_flow_z = min_flow_z
        self.stop_bps = stop_bps
        self.target_bps = target_bps
        self.max_hold_ms = max_hold_ms

    def signal(self, features: TradeFlowFeatures) -> TakerSignal | None:
        if features.vol_impulse_ratio < self.min_impulse_ratio:
            return None
        if features.signed_flow_z >= self.min_flow_z:
            return TakerSignal("buy", self.stop_bps, self.target_bps, self.max_hold_ms)
        if features.signed_flow_z <= -self.min_flow_z:
            return TakerSignal("sell", self.stop_bps, self.target_bps, self.max_hold_ms)
        return None


def default_taker_scalpers() -> tuple[TradeEventScalper, ...]:
    return (ForcedFlowBurstScalper(), VolatilityImpulseScalper())


# --- Replay engine -----------------------------------------------------------------

@dataclass(frozen=True)
class TakerTrade:
    side: str
    entry_ts: datetime
    entry_price: float            # slippage-adjusted taker entry
    exit_ts: datetime
    exit_price: float             # slippage-adjusted taker exit
    exit_reason: str              # "target" | "stop" | "timeout" | "end"
    gross_bps: float
    fees_bps: float               # 2 * taker_bps (slippage already in prices)

    @property
    def net_bps(self) -> float:
        return self.gross_bps - self.fees_bps


@dataclass
class TakerReplayResult:
    trades: list[TakerTrade] = field(default_factory=list)
    signals_seen: int = 0         # signal fired on a print (incl. while in position)
    entries: int = 0
    missed_entries: int = 0       # pending entries whose next print came too late
    notional_usd: float = 100.0

    @property
    def net_usd(self) -> float:
        return sum(t.net_bps / 10_000.0 * self.notional_usd for t in self.trades)


@dataclass
class _PendingEntry:
    signal: TakerSignal
    signal_ms: int


@dataclass
class _OpenTaker:
    side: str
    entry_price: float
    entry_ms: int
    stop_price: float
    target_price: float
    max_hold_ms: int = 0


class TakerReplayBacktester:
    """Trades-only replay with a taker-only cost model.

    Consumes the (ts_ms, kind, obj) event list from load_tick_events and uses
    ONLY the trade events (backfilled history has no book stream). Entries fill
    on the print AFTER the signal (no zero-latency same-print fills); entries
    and exits both pay taker fee + slippage. Stop wins stop-vs-target ties,
    same tie-break as everywhere else in this repo. Single position,
    deterministic.
    """

    def __init__(self, fees: TakerFees = TakerFees(), notional_usd: float = 100.0,
                 *, cooldown_ms: int = 1_000, entry_timeout_ms: int = 5_000,
                 engine_factory=TradeFlowEngine) -> None:
        self.fees = fees
        self.notional_usd = notional_usd
        self.cooldown_ms = cooldown_ms
        self.entry_timeout_ms = entry_timeout_ms
        self._engine_factory = engine_factory

    def _entry_price(self, side: str, price: float) -> float:
        slip = self.fees.slippage_bps / 10_000.0
        return price * (1 + slip) if side == "buy" else price * (1 - slip)

    def _exit_price(self, side: str, price: float) -> float:
        slip = self.fees.slippage_bps / 10_000.0
        # closing a long sells (worse = lower); closing a short buys (worse = higher)
        return price * (1 - slip) if side == "buy" else price * (1 + slip)

    def run(self, events: list[tuple[int, str, object]],
            scalper: TradeEventScalper) -> TakerReplayResult:
        engine = self._engine_factory()
        result = TakerReplayResult(notional_usd=self.notional_usd)
        position: _OpenTaker | None = None
        pending: _PendingEntry | None = None
        last_exit_ms: int | None = None
        last_ts = 0
        last_price: float | None = None

        for ts_ms, kind, obj in events:
            if kind != "trade" or not isinstance(obj, TradeTick):
                continue
            last_ts, last_price = ts_ms, obj.price
            features = engine.on_trade(obj)

            if position is not None:
                closed = self._check_exit(position, obj.price, ts_ms, result)
                if closed:
                    last_exit_ms = ts_ms
                    position = None
                    continue  # never re-enter on the exit print
            elif pending is not None:
                # taker entry on the FIRST print after the signal — unless the
                # tape went quiet for too long, in which case the setup is stale
                if ts_ms - pending.signal_ms > self.entry_timeout_ms:
                    result.missed_entries += 1
                else:
                    position = self._open(pending.signal, obj.price, ts_ms)
                    result.entries += 1
                pending = None
                continue  # the entry print never also evaluates a new signal

            if features is None:
                continue
            sig = scalper.signal(features)
            if sig is None:
                continue
            result.signals_seen += 1
            if position is not None:
                continue  # single position; concurrent signals are suppressed
            if last_exit_ms is not None and ts_ms - last_exit_ms < self.cooldown_ms:
                continue
            pending = _PendingEntry(sig, ts_ms)

        if pending is not None:
            result.missed_entries += 1  # no print ever arrived to fill it
        if position is not None and last_price is not None:
            self._close(position, last_price, last_ts, "end", result)
        return result

    def _open(self, sig: TakerSignal, raw_price: float, ts_ms: int) -> _OpenTaker:
        entry = self._entry_price(sig.side, raw_price)
        if sig.side == "buy":
            stop = entry * (1 - sig.stop_bps / 10_000.0)
            target = entry * (1 + sig.target_bps / 10_000.0)
        else:
            stop = entry * (1 + sig.stop_bps / 10_000.0)
            target = entry * (1 - sig.target_bps / 10_000.0)
        return _OpenTaker(sig.side, entry, ts_ms, stop, target,
                          max_hold_ms=sig.max_hold_ms)

    def _check_exit(self, pos: _OpenTaker, price: float, ts_ms: int,
                    result: TakerReplayResult) -> bool:
        if pos.side == "buy":
            if price <= pos.stop_price:            # stop wins ties
                self._close(pos, price, ts_ms, "stop", result)
                return True
            if price >= pos.target_price:
                self._close(pos, price, ts_ms, "target", result)
                return True
        else:
            if price >= pos.stop_price:
                self._close(pos, price, ts_ms, "stop", result)
                return True
            if price <= pos.target_price:
                self._close(pos, price, ts_ms, "target", result)
                return True
        if pos.max_hold_ms > 0 and ts_ms - pos.entry_ms >= pos.max_hold_ms:
            self._close(pos, price, ts_ms, "timeout", result)
            return True
        return False

    def _close(self, pos: _OpenTaker, raw_price: float, ts_ms: int, reason: str,
               result: TakerReplayResult) -> None:
        exit_px = self._exit_price(pos.side, raw_price)
        if pos.side == "buy":
            gross_bps = (exit_px - pos.entry_price) / pos.entry_price * 10_000.0
        else:
            gross_bps = (pos.entry_price - exit_px) / pos.entry_price * 10_000.0
        result.trades.append(TakerTrade(
            side=pos.side,
            entry_ts=datetime.fromtimestamp(pos.entry_ms / 1000, tz=UTC),
            entry_price=pos.entry_price,
            exit_ts=datetime.fromtimestamp(ts_ms / 1000, tz=UTC),
            exit_price=exit_px,
            exit_reason=reason,
            gross_bps=gross_bps,
            fees_bps=2.0 * self.fees.taker_bps,
        ))


# --- Rows / payload ----------------------------------------------------------------

@dataclass(frozen=True)
class EventTakerReplayRow:
    """Per symbol/day/family verdict row — shaped after ScalperReplayRow where
    the taker-only model makes the field meaningful."""

    exchange: str
    symbol: str
    day: str
    family: str
    signals: int
    entries: int
    missed_entries: int
    net_usd: float
    avg_net_bps: float | None
    win_rate_pct: float
    profit_factor: float | None
    round_trip_cost_bps: float
    exit_reason_counts: dict[str, int]
    verdict: str
    execution: str = "taker_only"
    can_trade: bool = False
    can_promote: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _verdict(result: TakerReplayResult, min_entries: int) -> str:
    if result.entries == 0:
        return "NO_SIGNALS"
    if result.net_usd <= 0:
        return "NEGATIVE_EDGE"
    if result.entries < min_entries:
        return "UNDER_SAMPLED_POSITIVE"
    return "CANDIDATE"


def build_row(result: TakerReplayResult, *, exchange: str, symbol: str, day: str,
              family: str, fees: TakerFees, min_entries: int = 20) -> EventTakerReplayRow:
    nets = [t.net_bps for t in result.trades]
    wins = [v for v in nets if v > 0]
    losses = [-v for v in nets if v < 0]
    pf = (sum(wins) / sum(losses)) if wins and losses else (999.0 if wins else None)
    exit_reasons: dict[str, int] = {}
    for t in result.trades:
        exit_reasons[t.exit_reason] = exit_reasons.get(t.exit_reason, 0) + 1
    return EventTakerReplayRow(
        exchange=exchange,
        symbol=symbol,
        day=day,
        family=family,
        signals=result.signals_seen,
        entries=result.entries,
        missed_entries=result.missed_entries,
        net_usd=result.net_usd,
        avg_net_bps=mean(nets) if nets else None,
        win_rate_pct=len(wins) / len(nets) * 100.0 if nets else 0.0,
        profit_factor=pf,
        round_trip_cost_bps=fees.round_trip_cost_bps,
        exit_reason_counts=exit_reasons,
        verdict=_verdict(result, min_entries),
    )


def discover_days(data_root: Path | str, exchange: str, symbol: str) -> tuple[str, ...]:
    """UTC days with backfilled/recorded trade shards for one symbol."""
    safe = symbol.split(":")[0].replace("/", "")
    stream_root = (Path(data_root) / "ticks" / f"exchange={exchange}"
                   / f"symbol={safe}" / "stream=trades")
    if not stream_root.exists():
        return ()
    days = {p.stem for p in stream_root.glob("*.parquet")}
    days.update(p.name for p in stream_root.iterdir() if p.is_dir())
    return tuple(sorted(d for d in days if len(d) == 8 and d.isdigit()))


def run_event_taker_replay(
    data_root: Path | str,
    symbols: list[str],
    *,
    days: tuple[str, ...] = (),
    exchange: str = DEFAULT_HIST_EXCHANGE,
    fees: TakerFees = TakerFees(),
    notional_usd: float = 100.0,
    scalpers: tuple[TradeEventScalper, ...] | None = None,
    min_entries: int = 20,
) -> dict:
    """Replay every family over every symbol/day of the backfilled tape."""
    scalpers = scalpers or default_taker_scalpers()
    runner = TakerReplayBacktester(fees, notional_usd=notional_usd)
    rows: list[EventTakerReplayRow] = []
    days_used: set[str] = set()
    for symbol in symbols:
        symbol_days = days or discover_days(data_root, exchange, symbol)
        for day in symbol_days:
            events = load_tick_events(data_root, exchange, symbol, day)
            if not events:
                continue
            days_used.add(day)
            for scalper in scalpers:
                result = runner.run(events, scalper)
                rows.append(build_row(
                    result, exchange=exchange, symbol=symbol, day=day,
                    family=scalper.family, fees=fees, min_entries=min_entries,
                ))
    rows.sort(key=_row_sort_key)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "policy": event_taker_policy(),
        "cost_model": {
            "execution": "taker_only",
            "taker_bps": fees.taker_bps,
            "slippage_bps": fees.slippage_bps,
            "round_trip_cost_bps": fees.round_trip_cost_bps,
            "maker_assumptions": "none — no book tape exists for history",
        },
        "exchange": exchange,
        "symbols": list(symbols),
        "days": sorted(days_used),
        "rows": [r.to_dict() for r in rows],
        "summary": _summary(rows),
        "can_trade": False,
        "can_promote": False,
    }


def _row_sort_key(r: EventTakerReplayRow) -> tuple:
    verdict_rank = {
        "CANDIDATE": 0,
        "UNDER_SAMPLED_POSITIVE": 1,
        "NEGATIVE_EDGE": 2,
        "NO_SIGNALS": 3,
    }.get(r.verdict, 9)
    return (verdict_rank, -(r.avg_net_bps or -999.0), -r.entries,
            r.family, r.symbol, r.day)


def _summary(rows: list[EventTakerReplayRow]) -> dict:
    verdicts: dict[str, int] = {}
    for r in rows:
        verdicts[r.verdict] = verdicts.get(r.verdict, 0) + 1
    return {
        "rows": len(rows),
        "families": len({r.family for r in rows}),
        "verdict_counts": verdicts,
        "can_trade": False,
        "can_promote": False,
    }


def write_event_taker_payload(payload: dict, out_dir: Path | str) -> Path:
    """Atomic publish for the folding hook (same tmp+replace discipline as
    l2_latest.json)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / EVENT_TAKER_LATEST
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(path)
    return path


def _split_csv(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def render_report(payload: dict, *, limit: int = 40) -> str:
    lines = [
        "event taker replay (backfilled aggTrades tape)",
        "policy=research_only can_trade=false can_promote=false execution=taker_only",
        f"round_trip_cost_bps={payload['cost_model']['round_trip_cost_bps']}",
        "",
        "verdict                 net$    avg_bps  pf    win% entries signals "
        "symbol          day      family",
    ]
    for r in payload["rows"][:limit]:
        pf = r["profit_factor"]
        avg = r["avg_net_bps"]
        lines.append(
            f"{r['verdict']:<22} {r['net_usd']:>6.2f} "
            f"{'--' if avg is None else f'{avg:8.2f}':>8} "
            f"{'--' if pf is None else f'{pf:.2f}':>5} "
            f"{r['win_rate_pct']:>4.0f} {r['entries']:>7} {r['signals']:>7} "
            f"{r['symbol']:<15} {r['day']} {r['family']}"
        )
    if not payload["rows"]:
        lines.append("no rows — backfill aggTrades first "
                     "(python -m vnedge.data.aggtrades_backfill)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(
        description="taker-only event-family replay over backfilled aggTrades"
    )
    p.add_argument("--data-root", default="data")
    p.add_argument("--exchange", default=DEFAULT_HIST_EXCHANGE)
    p.add_argument("--symbols", default="BTC/USDT:USDT")
    p.add_argument("--days", help="comma-separated UTC days YYYYMMDD; "
                                  "default: every backfilled day")
    p.add_argument("--taker-bps", type=float, default=TakerFees().taker_bps)
    p.add_argument("--slippage-bps", type=float, default=TakerFees().slippage_bps)
    p.add_argument("--out", default="research/live_research",
                   help="directory for event_taker_replay.json")
    p.add_argument("--no-publish", action="store_true",
                   help="print only; do not write the folding-hook file")
    p.add_argument("--json", action="store_true")
    p.add_argument("--limit", type=int, default=40)
    args = p.parse_args(argv)

    payload = run_event_taker_replay(
        args.data_root,
        list(_split_csv(args.symbols)),
        days=_split_csv(args.days),
        exchange=args.exchange,
        fees=TakerFees(taker_bps=args.taker_bps, slippage_bps=args.slippage_bps),
    )
    if not args.no_publish:
        path = write_event_taker_payload(payload, args.out)
        logger.info("published %s", path)
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(render_report(payload, limit=args.limit))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
