"""Data-burn registry — the mechanical record of which data windows are seen.

Pre-registration used to be convention (CLAUDE.md notes) only: the research
loop runs ~100k+ walk-forward trials/day on rolling windows, and nothing
*mechanical* stopped a failed variant from being retried as windows rolled,
or a judgment from re-running on already-seen data. This module closes that
gap with an append-only, hash-chained JSONL registry of burned windows.

The registry lives at ``research/judgments/burn_registry.jsonl`` — a
REPO-COMMITTED path. Commit every change to it: the git history of this file
IS the evidence trail that judgments only ever ran on untouched data. (This
is deliberately unlike research/live_research/*, which is gitignored runtime
state.)

Record kinds:

- ``judgment`` — a pre-registered verdict-producing run. Once recorded, the
  window is burned for that strategy/symbol/exchange forever.
- ``exploratory_burn`` — data seen by exploration (auto_explore variants,
  baselines). Burns the window just as hard: exploration contaminates data
  for judgment purposes, it just doesn't produce a verdict that stands.

Hash chain mirrors :mod:`vnedge.execution.fill_ledger`: each record embeds
the previous record's hash, so any edit, deletion, or reorder breaks the
chain and is detectable by ``verify``.

CLI (for ad-hoc/one-shot judgment scripts, which must check before running
and register after)::

    python -m vnedge.research.data_burn check \
        --strategy funding_mean_reversion_v1 --symbol "BTC/USDT:USDT" \
        --exchange binanceusdm --start 2024-07-03 --end 2025-07-03
    python -m vnedge.research.data_burn register --kind judgment \
        --strategy ... --symbol ... --exchange ... --start ... --end ... \
        --verdict PASS --note "round N"
    python -m vnedge.research.data_burn verify

``check`` exits non-zero when the window is burned (with the overlapping
records printed as evidence); ``register --kind judgment`` refuses burned
windows unless ``--allow-burned`` is given (backfill of historical facts).
Programmatic judgments should use :func:`judge_untouched`, which refuses to
run on burned data and records the burn when it runs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from vnedge.execution.fill_ledger import _GENESIS, _record_hash, verify_chain

#: Repo-committed evidence file. Changes to it should be committed to git.
DEFAULT_REGISTRY_PATH = Path("research/judgments/burn_registry.jsonl")

KIND_JUDGMENT = "judgment"
KIND_EXPLORATORY = "exploratory_burn"
KINDS = (KIND_JUDGMENT, KIND_EXPLORATORY)

#: Keys that carry the chain, not the record content.
_CHAIN_FIELDS = ("seq", "prev_hash", "hash")


class BurnedDataError(RuntimeError):
    """Raised when a window that must be untouched overlaps burned records."""

    def __init__(self, message: str, records: list[dict]):
        super().__init__(message)
        self.records = records


def _iso(value) -> str:
    """Normalize datetimes/pd.Timestamps/ISO strings to an ISO-8601 string."""
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "isoformat"):  # pd.Timestamp, date
        return value.isoformat()
    return str(value)


def _parse_ts(value) -> datetime:
    """Parse an ISO date or datetime; naive values are taken as UTC."""
    raw = _iso(value)
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


class BurnRegistry:
    """Append-only, fsync'd, hash-chained registry of burned data windows."""

    def __init__(self, path: str | Path = DEFAULT_REGISTRY_PATH) -> None:
        self.path = Path(path)
        self.records = 0
        self._prev_hash = _GENESIS
        self._resume()

    def _resume(self) -> None:
        """Continue an existing chain; refuse silently corrupt tails."""
        if not self.path.exists():
            return
        report = verify_chain(self.path)
        if not report.ok:
            raise ValueError(
                f"burn registry {self.path} fails chain verification at line "
                f"{report.first_bad_line} — refusing to append to a broken chain"
            )
        self.records = report.records
        if report.records:
            with self.path.open() as fh:
                last = None
                for line in fh:
                    if line.strip():
                        last = line
            self._prev_hash = json.loads(last)["hash"]

    def append(self, record: dict) -> str:
        """Append one burn record; returns its chain hash."""
        payload = dict(record)
        payload["seq"] = self.records
        h = _record_hash(payload, self._prev_hash)
        chained = {**payload, "prev_hash": self._prev_hash, "hash": h}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as fh:
            fh.write(json.dumps(chained, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        self._prev_hash = h
        self.records += 1
        return h


def read_records(path: str | Path = DEFAULT_REGISTRY_PATH) -> list[dict]:
    """All burn records, oldest first (chain fields stripped)."""
    path = Path(path)
    if not path.exists():
        return []
    out: list[dict] = []
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            for field in _CHAIN_FIELDS:
                record.pop(field, None)
            out.append(record)
    return out


def _record(
    kind: str,
    strategy_id: str,
    symbol: str,
    exchange: str,
    window_start,
    window_end,
    verdict: str,
    note: str = "",
    *,
    path: str | Path = DEFAULT_REGISTRY_PATH,
) -> dict:
    if kind not in KINDS:
        raise ValueError(f"unknown burn kind {kind!r} — expected one of {KINDS}")
    start, end = _parse_ts(window_start), _parse_ts(window_end)
    if end < start:
        raise ValueError(f"window_end {end} precedes window_start {start}")
    record = {
        "kind": kind,
        "strategy_id": strategy_id,
        "symbol": symbol,
        "exchange": exchange,
        "window_start": _iso(window_start),
        "window_end": _iso(window_end),
        "verdict": verdict,
        "registered_at": datetime.now(UTC).isoformat(),
        "note": note,
    }
    BurnRegistry(path).append(record)
    return record


def record_judgment(
    strategy_id: str,
    symbol: str,
    exchange: str,
    window_start,
    window_end,
    verdict: str,
    note: str = "",
    *,
    path: str | Path = DEFAULT_REGISTRY_PATH,
) -> dict:
    """Record a judgment run — burns the window for this lane permanently."""
    return _record(
        KIND_JUDGMENT, strategy_id, symbol, exchange,
        window_start, window_end, verdict, note, path=path,
    )


def record_burn(
    strategy_id: str,
    symbol: str,
    exchange: str,
    window_start,
    window_end,
    verdict: str = "EXPLORATORY",
    note: str = "",
    *,
    path: str | Path = DEFAULT_REGISTRY_PATH,
) -> dict:
    """Record an exploratory data burn (data seen, no standing verdict)."""
    return _record(
        KIND_EXPLORATORY, strategy_id, symbol, exchange,
        window_start, window_end, verdict, note, path=path,
    )


def overlaps(
    strategy_id: str,
    symbol: str,
    exchange: str,
    start,
    end,
    *,
    path: str | Path = DEFAULT_REGISTRY_PATH,
) -> list[dict]:
    """Burned records whose window intersects [start, end] for this lane.

    Inclusive intersection: sharing a single boundary timestamp counts —
    partially-seen data is seen data.
    """
    q_start, q_end = _parse_ts(start), _parse_ts(end)
    hits: list[dict] = []
    for record in read_records(path):
        if (
            record["strategy_id"] != strategy_id
            or record["symbol"] != symbol
            or record["exchange"] != exchange
        ):
            continue
        r_start = _parse_ts(record["window_start"])
        r_end = _parse_ts(record["window_end"])
        if q_start <= r_end and r_start <= q_end:
            hits.append(record)
    return hits


def assert_untouched(
    strategy_id: str,
    symbol: str,
    exchange: str,
    start,
    end,
    *,
    path: str | Path = DEFAULT_REGISTRY_PATH,
) -> None:
    """Raise :class:`BurnedDataError` (with the evidence) if the window is burned."""
    hits = overlaps(strategy_id, symbol, exchange, start, end, path=path)
    if hits:
        detail = "; ".join(
            f"[{h['kind']}] {h['window_start']}..{h['window_end']} "
            f"verdict={h['verdict']} registered={h['registered_at']}"
            + (f" ({h['note']})" if h.get("note") else "")
            for h in hits
        )
        raise BurnedDataError(
            f"{strategy_id} {symbol} {exchange} {_iso(start)}..{_iso(end)} "
            f"overlaps {len(hits)} burned window(s): {detail}",
            hits,
        )


def judge_untouched(
    strategy_id: str,
    symbol: str,
    exchange: str,
    window_start,
    window_end,
    run: Callable[[], object],
    *,
    note: str = "",
    path: str | Path = DEFAULT_REGISTRY_PATH,
) -> str:
    """Run a one-shot judgment ONLY if its window is untouched, and burn it.

    Refuses (raises :class:`BurnedDataError`) when the window overlaps any
    prior judgment or exploratory burn. When it does run, the window is
    recorded as a judgment even if ``run`` raises — attempting a judgment
    looks at the data, and looked-at data is burned. Returns the verdict
    (``str(run())``).
    """
    assert_untouched(strategy_id, symbol, exchange, window_start, window_end, path=path)
    try:
        verdict = str(run())
    except BaseException as exc:
        record_judgment(
            strategy_id, symbol, exchange, window_start, window_end,
            verdict="ERROR",
            note=(f"{note} | " if note else "") + f"judgment raised: {exc!r}",
            path=path,
        )
        raise
    record_judgment(
        strategy_id, symbol, exchange, window_start, window_end,
        verdict=verdict, note=note, path=path,
    )
    return verdict


def window_fingerprint(window_end, params: dict | None = None) -> str:
    """Fingerprint of the material data window an exploratory run saw.

    Day-rounded window end + a short hash of the run parameters. Keying
    auto-explore attempts by this means the same variant is NOT re-run on a
    materially-same window (same end day, same params), while a later window
    (data rolled forward) is a genuinely new attempt — recorded as a new burn.
    """
    day = _parse_ts(window_end).strftime("%Y%m%d")
    canonical = json.dumps(
        params or {}, sort_keys=True, separators=(",", ":"), default=str
    )
    return f"{day}|{hashlib.sha256(canonical.encode()).hexdigest()[:8]}"


# --------------------------------------------------------------------------
# CLI — for ad-hoc judgment scripts and operators.
# --------------------------------------------------------------------------


def _add_lane_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--strategy", required=True, help="strategy_id (or variant_id)")
    parser.add_argument("--symbol", required=True, help='e.g. "BTC/USDT:USDT"')
    parser.add_argument("--exchange", required=True, help="e.g. binanceusdm")
    parser.add_argument("--start", required=True, help="window start (ISO date/datetime)")
    parser.add_argument("--end", required=True, help="window end (ISO date/datetime)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m vnedge.research.data_burn",
        description=__doc__.splitlines()[0],
    )
    parser.add_argument(
        "--registry", default=str(DEFAULT_REGISTRY_PATH),
        help=f"registry path (default: {DEFAULT_REGISTRY_PATH})",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="exit 1 if the window is burned")
    _add_lane_args(check)

    register = sub.add_parser("register", help="append a burn record")
    _add_lane_args(register)
    register.add_argument("--kind", choices=KINDS, default=KIND_JUDGMENT)
    register.add_argument("--verdict", required=True, help="e.g. PASS / REJECT")
    register.add_argument("--note", default="", help="cite the round / context")
    register.add_argument(
        "--allow-burned", action="store_true",
        help="register a judgment even though the window overlaps burned data "
             "(backfilling historical facts only — never for new judgments)",
    )

    sub.add_parser("verify", help="verify the hash chain")

    args = parser.parse_args(argv)
    path = Path(args.registry)

    if args.command == "check":
        hits = overlaps(args.strategy, args.symbol, args.exchange,
                        args.start, args.end, path=path)
        if hits:
            print(f"BURNED: {len(hits)} overlapping record(s):")
            for h in hits:
                print("  " + json.dumps(h, sort_keys=True))
            return 1
        print("UNTOUCHED: no overlapping burn records")
        return 0

    if args.command == "register":
        if args.kind == KIND_JUDGMENT and not args.allow_burned:
            try:
                assert_untouched(args.strategy, args.symbol, args.exchange,
                                 args.start, args.end, path=path)
            except BurnedDataError as exc:
                print(f"REFUSED: {exc}")
                print("(use --allow-burned only to backfill historical facts)")
                return 1
        fn = record_judgment if args.kind == KIND_JUDGMENT else record_burn
        record = fn(args.strategy, args.symbol, args.exchange, args.start,
                    args.end, verdict=args.verdict, note=args.note, path=path)
        print("RECORDED: " + json.dumps(record, sort_keys=True))
        print(f"Remember to commit {path} — it is the evidence trail.")
        return 0

    if args.command == "verify":
        report = verify_chain(path)
        if report.ok:
            print(f"OK: {report.records} record(s), chain intact")
            return 0
        print(f"BROKEN: chain fails at line {report.first_bad_line}")
        return 1

    return 2  # pragma: no cover — argparse enforces the command set


if __name__ == "__main__":
    sys.exit(main())
