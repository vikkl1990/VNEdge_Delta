"""Read-only aggregation of shadow-lane virtual outcomes for research views.

Shadow lanes journal every approved entry intent (``shadow_intent``) and its
forward-resolved virtual result (``shadow_outcome``) to
``logs/paper_trials/<lane_id>.journal.jsonl`` (see
``vnedge.runtime.shadow_outcomes``). This module READS those journals — never
writes, never mutates — and aggregates the outcomes per
``(strategy_id, exchange, symbol)`` so the edge leaderboard can rank
walk-forward candidates against their LIVE shadow track record.

Identity resolution is honest, not inventive:

- strategy + symbol come from the journaled intent payload itself
  (``payload.intent.strategy_id`` / ``payload.intent.symbol``);
- exchange is not journaled per record, so it is inferred from the lane's
  journal filename (lane ids embed the venue, longest-match against the known
  exchange ids); a file that matches nothing is reported as ``"unknown"``;
- a journal whose intents carry no identity is skipped — unattributable
  outcomes are dropped, not guessed.

Observability only: nothing here trades, promotes, or gates. Missing
directories, missing files, and malformed lines all degrade to "no data".
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_JOURNAL_DIR = Path("logs/paper_trials")
SHADOW_JOURNAL_GLOB = "*shadow*.journal.jsonl"

# The real-time shadow scalp runner (vnedge.runtime.realtime_shadow_scalp)
# writes its per-family journals here, with scalp_shadow_intent /
# scalp_shadow_outcome records. They carry the same intent identity + virtual
# net the aggregator needs, so folding them in is a small path addition, not a
# new reader. File names embed the venue (cascade_binanceusdm_BTCUSDT...),
# so the glob is broad and identity/exchange resolve exactly as before.
DEFAULT_SCALP_JOURNAL_DIR = Path("logs/scalp_shadow")
SCALP_JOURNAL_GLOB = "*.journal.jsonl"

# Both record spellings feed one aggregation: the candle shadow lanes emit
# shadow_intent/shadow_outcome; the real-time scalp lanes emit
# scalp_shadow_intent/scalp_shadow_outcome.
_INTENT_KINDS = ("shadow_intent", "scalp_shadow_intent")
_OUTCOME_KINDS = ("shadow_outcome", "scalp_shadow_outcome")

# Longest-first so "binanceusdm" wins over "binance" and "delta_india" over
# "delta" when both substrings appear in a lane id.
KNOWN_EXCHANGES: tuple[str, ...] = (
    "binanceusdm",
    "delta_india",
    "binance",
    "bybit",
    "delta",
)

_RESOLUTIONS = ("stop", "target", "timeout")


def shadow_perf_key(strategy: str, exchange: str, symbol: str) -> str:
    """Join key shared with the edge leaderboard: strategy|exchange|SYMBOL."""
    return f"{strategy}|{exchange}|{_normalize_symbol(symbol)}"


def _normalize_symbol(symbol: str) -> str:
    return symbol.split(":")[0].replace("/", "").replace("-", "").upper()


def read_shadow_perf(
    journal_dir: Path | str = DEFAULT_JOURNAL_DIR,
    *,
    known_exchanges: tuple[str, ...] = KNOWN_EXCHANGES,
    scalp_journal_dir: Path | str | None = None,
) -> dict:
    """Aggregate shadow_outcome records per (strategy, exchange, symbol).

    Returns a JSON-serializable payload; ``available=False`` with empty lanes
    when the directory or journals are absent (graceful degradation — a
    research container without the logs mount must not fail its cycle).

    ``scalp_journal_dir`` (opt-in) additionally folds the real-time shadow
    scalp lanes' journals into the same aggregation. It stays off by default so
    existing callers/tests are unchanged; ``continuous_research`` passes it.
    """
    journal_dir = Path(journal_dir)
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "journal_dir": str(journal_dir),
        "available": False,
        "journals_read": 0,
        "lanes": [],
        "policy": {
            "observability_only": True,
            "can_trade": False,
            "can_promote": False,
        },
    }

    sources: list[tuple[Path, str]] = []
    if journal_dir.is_dir():
        sources.append((journal_dir, SHADOW_JOURNAL_GLOB))
    if scalp_journal_dir is not None:
        scalp_dir = Path(scalp_journal_dir)
        if scalp_dir.is_dir() and scalp_dir != journal_dir:
            sources.append((scalp_dir, SCALP_JOURNAL_GLOB))
    if not sources:
        return payload

    aggregates: dict[str, _LaneAggregate] = {}
    journals_read = 0
    for source_dir, glob in sources:
        for path in sorted(source_dir.glob(glob)):
            lane = _read_journal(path, known_exchanges=known_exchanges)
            if lane is None:
                continue
            journals_read += 1
            identity, outcomes = lane
            key = shadow_perf_key(*identity)
            agg = aggregates.setdefault(key, _LaneAggregate(*identity))
            agg.absorb(path.name, outcomes)

    lanes = [agg.to_dict() for agg in aggregates.values() if agg.trades > 0]
    lanes.sort(key=lambda lane: (-lane["net_usd"], lane["strategy"], lane["symbol"]))
    payload["journals_read"] = journals_read
    payload["lanes"] = lanes
    payload["available"] = journals_read > 0
    return payload


def index_shadow_perf(payload: dict | None) -> dict[str, dict]:
    """Reader payload -> {shadow_perf_key: lane dict} for leaderboard joins."""
    if not payload:
        return {}
    out: dict[str, dict] = {}
    for lane in payload.get("lanes", []):
        try:
            key = shadow_perf_key(
                str(lane["strategy"]), str(lane["exchange"]), str(lane["symbol"])
            )
        except (KeyError, TypeError):
            continue
        out[key] = lane
    return out


# --- per-journal parsing ------------------------------------------------------------

def _read_journal(
    path: Path, *, known_exchanges: tuple[str, ...]
) -> tuple[tuple[str, str, str], dict[str, dict]] | None:
    """One journal -> ((strategy, exchange, symbol), {intent_key: outcome})."""
    strategy = symbol = None
    outcomes: dict[str, dict] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue  # torn/corrupt line — skip, never fail the read
                if not isinstance(record, dict):
                    continue
                kind = record.get("kind")
                rec_payload = record.get("payload")
                if not isinstance(rec_payload, dict):
                    continue
                if kind in _INTENT_KINDS and strategy is None:
                    intent = rec_payload.get("intent") or {}
                    if intent.get("strategy_id") and intent.get("symbol"):
                        strategy = str(intent["strategy_id"])
                        symbol = str(intent["symbol"])
                elif kind in _OUTCOME_KINDS:
                    key = rec_payload.get("intent_key")
                    if key is None or key in outcomes:
                        continue  # first record wins, same as the tracker
                    outcomes[str(key)] = {
                        "net": _as_float(rec_payload.get("virtual_net_usd")),
                        "resolution": str(rec_payload.get("resolution", "")),
                        "ts": _parse_ts(rec_payload.get("bar_ts") or record.get("ts")),
                    }
    except OSError as exc:
        logger.warning("shadow perf reader: cannot read %s: %s", path, exc)
        return None
    if strategy is None or symbol is None:
        if outcomes:
            logger.warning(
                "shadow perf reader: %s has %d outcomes but no attributable "
                "intent — skipped (never guess identity)", path.name, len(outcomes),
            )
        return None
    exchange = _exchange_from_name(path.name, known_exchanges)
    return (strategy, exchange, symbol), outcomes


def _exchange_from_name(name: str, known_exchanges: tuple[str, ...]) -> str:
    lowered = name.lower()
    for exchange in sorted(known_exchanges, key=len, reverse=True):
        if exchange in lowered:
            return exchange
    return "unknown"


def _as_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts


# --- aggregation --------------------------------------------------------------------

class _LaneAggregate:
    """Running per-(strategy, exchange, symbol) stats across shadow journals."""

    def __init__(self, strategy: str, exchange: str, symbol: str) -> None:
        self.strategy, self.exchange, self.symbol = strategy, exchange, symbol
        self.trades = 0
        self.wins = 0
        self.net_usd = 0.0
        self.gross_win = 0.0
        self.gross_loss = 0.0
        self.first_ts: datetime | None = None
        self.last_ts: datetime | None = None
        self.resolutions: dict[str, int] = dict.fromkeys(_RESOLUTIONS, 0)
        self.source_journals: list[str] = []
        self._seen_keys: set[str] = set()

    def absorb(self, journal_name: str, outcomes: dict[str, dict]) -> None:
        if journal_name not in self.source_journals:
            self.source_journals.append(journal_name)
        for intent_key, outcome in outcomes.items():
            if intent_key in self._seen_keys:
                continue  # duplicated journal copy — never double-count
            self._seen_keys.add(intent_key)
            net = outcome["net"]
            self.trades += 1
            self.net_usd += net
            if net > 0:
                self.wins += 1
                self.gross_win += net
            else:
                self.gross_loss += -net
            if outcome["resolution"] in self.resolutions:
                self.resolutions[outcome["resolution"]] += 1
            ts = outcome["ts"]
            if ts is not None:
                if self.first_ts is None or ts < self.first_ts:
                    self.first_ts = ts
                if self.last_ts is None or ts > self.last_ts:
                    self.last_ts = ts

    def to_dict(self) -> dict:
        if self.gross_loss > 0:
            pf = round(self.gross_win / self.gross_loss, 3)
        else:
            pf = None  # no losing virtual trades yet — PF undefined, not infinite
        span_days = 0.0
        if self.first_ts is not None and self.last_ts is not None:
            span_days = round((self.last_ts - self.first_ts).total_seconds() / 86400.0, 2)
        return {
            "strategy": self.strategy,
            "exchange": self.exchange,
            "symbol": self.symbol,
            "virtual_trades": self.trades,
            "wins": self.wins,
            "win_rate_pct": round(self.wins / self.trades * 100.0, 1) if self.trades else 0.0,
            "net_usd": round(self.net_usd, 4),
            "profit_factor": pf,
            "span_days": span_days,
            "last_resolution_ts": (
                self.last_ts.isoformat() if self.last_ts is not None else None
            ),
            "resolutions": dict(self.resolutions),
            "source_journals": list(self.source_journals),
        }
