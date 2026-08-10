"""Lane-health auditor — cross-checks four sources of truth that can drift.

The multi-lane runner has a DESIRED spec list (env grid + candidate lanes +
manifest lanes), while the filesystem holds what is ACTUALLY happening: one
``<lane_id>.journal.jsonl`` / ``<lane_id>.equity.jsonl`` pair per running
lane. Config changes, manifest churn, or a wedged feed can silently split
those apart. This module reconciles them:

1. DESIRED  — ``multi_lane_shadow.desired_lane_specs`` (same composition the
   runner launches: env grid + candidate/manifest shadow lanes + Delta MR).
2. ACTIVE   — journal/equity files present in the journal dir.
3. FRESHNESS — age of the newest journal/equity record vs the lane timeframe.
4. SIGNAL COUNTS — ``lane_eval`` records: a lane that journals *something*
   but has not evaluated a bar in 24h is broken, not quiet.

Per-lane verdicts:

- OK      — journaling, fresh, evaluating.
- STALE   — newest record older than 3x the lane timeframe (feed/loop dead).
- SILENT  — records are flowing but no ``lane_eval`` in 24h (strategy loop
  broken while the plumbing looks alive).
- MISSING — desired but no journal file at all (never started / renamed).
- ORPHAN  — journal file with no desired spec (left behind by a config
  change; consuming attention/disk while representing nothing).
- SHADOW_PROBATION — desired shadow lane is fresh/evaluating, but its
  resolved virtual outcomes are net-negative; it is not paper/live compatible.

The auditor is read-only and side-effect free. Exit-code contract for cron:
``python -m vnedge.runtime.lane_health`` returns 1 if any lane is MISSING or
STALE, 0 otherwise. That is the process-health contract. Production readiness
is stricter: every row must be OK/trade-compatible, so probation/silent/orphan
lanes can never look trade-ready on the dashboard.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from vnedge.data.schemas import TIMEFRAME_MS

if TYPE_CHECKING:
    from vnedge.runtime.multi_lane import LaneSpec

# Verdicts, in severity order (worst first) for reporting.
VERDICT_MISSING = "MISSING"
VERDICT_STALE = "STALE"
VERDICT_SILENT = "SILENT"
VERDICT_PROBATION = "SHADOW_PROBATION"
VERDICT_ORPHAN = "ORPHAN"
VERDICT_OK = "OK"

# A lane is "evaluating" if a lane_eval record landed within 2 bars.
EVAL_TIMEFRAME_MULTIPLE = 2.0
# A lane is STALE if its newest journal/equity record is older than 3 bars.
STALE_TIMEFRAME_MULTIPLE = 3.0
# A lane that has been active for 24h+ without a single lane_eval is SILENT.
SILENT_EVAL_SECONDS = 24 * 3600.0

_JOURNAL_SUFFIX = ".journal.jsonl"
_EQUITY_SUFFIX = ".equity.jsonl"
# How much file tail to scan for the newest record / newest lane_eval.
# lane_eval is written every evaluated bar, so it dominates recent history;
# 256 KiB of tail is thousands of records.
_TAIL_BYTES = 256 * 1024
_DEFAULT_TIMEFRAME_SECONDS = 3600.0


@dataclass(frozen=True)
class LaneHealthRow:
    lane_id: str
    verdict: str
    exchange: str = ""
    symbol: str = ""
    timeframe: str = ""
    mode: str = ""
    exists_active: bool = False
    last_record_age_seconds: float | None = None
    last_eval_age_seconds: float | None = None
    evaluating: bool = False
    stale: bool = False
    detail: str = ""
    shadow_virtual_trades: int = 0
    shadow_net_usd: float = 0.0
    shadow_wins: int = 0
    shadow_losses: int = 0
    trade_compatible: bool = True

    def to_dict(self) -> dict:
        return {
            "lane_id": self.lane_id,
            "verdict": self.verdict,
            "exchange": self.exchange,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "mode": self.mode,
            "exists_active": self.exists_active,
            "last_record_age_seconds": _round(self.last_record_age_seconds),
            "last_eval_age_seconds": _round(self.last_eval_age_seconds),
            "evaluating": self.evaluating,
            "stale": self.stale,
            "detail": self.detail,
            "shadow_virtual_trades": self.shadow_virtual_trades,
            "shadow_net_usd": round(self.shadow_net_usd, 4),
            "shadow_wins": self.shadow_wins,
            "shadow_losses": self.shadow_losses,
            "trade_compatible": self.trade_compatible,
        }


@dataclass(frozen=True)
class LaneHealthReport:
    generated_at: str
    journal_dir: str
    rows: tuple[LaneHealthRow, ...]
    totals: dict[str, int] = field(default_factory=dict)

    @property
    def healthy(self) -> bool:
        """Cron/monitoring contract: MISSING or STALE means unhealthy."""
        return not any(
            row.verdict in (VERDICT_MISSING, VERDICT_STALE) for row in self.rows
        )

    @property
    def production_healthy(self) -> bool:
        """Trading-readiness contract: no non-OK or non-compatible lanes."""
        return bool(self.rows) and all(
            row.verdict == VERDICT_OK and row.trade_compatible for row in self.rows
        )

    def summary(self) -> str:
        """Dashboard badge text: '18/18 OK' or '2 STALE, 1 MISSING'."""
        if any(
            self.totals.get(_total_key(verdict), 0)
            for verdict in (
                VERDICT_MISSING,
                VERDICT_STALE,
                VERDICT_PROBATION,
                VERDICT_SILENT,
                VERDICT_ORPHAN,
            )
        ):
            return ", ".join(
                f"{self.totals.get(_total_key(verdict), 0)} {verdict}"
                for verdict in (
                    VERDICT_MISSING,
                    VERDICT_STALE,
                    VERDICT_PROBATION,
                    VERDICT_SILENT,
                    VERDICT_ORPHAN,
                )
                if self.totals.get(_total_key(verdict), 0)
            )
        return f"{self.totals.get('ok', 0)}/{self.totals.get('desired', 0)} OK"

    def production_summary(self) -> str:
        """Stricter operator badge text for paper/live promotion readiness."""
        if not self.production_healthy:
            return self.summary()
        compatible = self.totals.get("trade_compatible", self.totals.get("ok", 0))
        desired = self.totals.get("desired", compatible)
        return f"{compatible}/{desired} trade-compatible"

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "journal_dir": self.journal_dir,
            "healthy": self.healthy,
            "process_healthy": self.healthy,
            "production_healthy": self.production_healthy,
            "summary": self.summary(),
            "production_summary": self.production_summary(),
            "totals": dict(self.totals),
            "rows": [row.to_dict() for row in self.rows],
        }

    def to_snapshot(self) -> dict:
        """Lightweight form for the published dashboard snapshot."""
        return {
            "generated_at": self.generated_at,
            "healthy": self.healthy,
            "process_healthy": self.healthy,
            "production_healthy": self.production_healthy,
            "summary": self.summary(),
            "production_summary": self.production_summary(),
            "totals": dict(self.totals),
            "problems": [
                {
                    "lane_id": row.lane_id,
                    "verdict": row.verdict,
                    "age_seconds": _round(row.last_record_age_seconds),
                    "detail": row.detail,
                    "trade_compatible": row.trade_compatible,
                }
                for row in self.rows
                if row.verdict != VERDICT_OK
            ],
        }


# --- record scanning ----------------------------------------------------------------


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


def _total_key(verdict: str) -> str:
    return "probation" if verdict == VERDICT_PROBATION else verdict.lower()


def _timeframe_seconds(timeframe: str) -> float:
    ms = TIMEFRAME_MS.get(timeframe)
    if ms is None:
        return _DEFAULT_TIMEFRAME_SECONDS
    return ms / 1000.0


def _parse_ts(value: object) -> float | None:
    """ISO timestamp (journal/equity 'ts' field) -> epoch seconds, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _tail_lines(path: Path, max_bytes: int = _TAIL_BYTES) -> list[str]:
    """Last complete lines of a JSONL file without reading the whole file."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
                handle.readline()  # drop the (probably partial) first line
            data = handle.read()
    except OSError:
        return []
    return [line for line in data.decode("utf-8", errors="replace").splitlines() if line.strip()]


def _first_record_ts(path: Path) -> float | None:
    """Timestamp of the first record — when this lane started journaling."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    return _parse_ts(json.loads(line).get("ts"))
                except (ValueError, AttributeError):
                    return None
    except OSError:
        return None
    return None


def _scan_tail(path: Path) -> tuple[float | None, float | None]:
    """(newest record ts, newest lane_eval ts) from the file tail."""
    last_ts: float | None = None
    last_eval_ts: float | None = None
    for line in reversed(_tail_lines(path)):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        ts = _parse_ts(record.get("ts"))
        if ts is None:
            continue
        if last_ts is None:
            last_ts = ts
        if record.get("kind") == "lane_eval":
            last_eval_ts = ts
            break  # newest of each found; nothing older can be newer
    return last_ts, last_eval_ts


def _scan_shadow_outcomes(path: Path) -> tuple[int, float, int, int]:
    """Full-file shadow outcome summary: (trades, net, wins, losses).

    Lane-health runs on a slow cadence, so scanning the small per-lane journals
    is acceptable. This makes probation persistent even when the losing outcome
    has scrolled out of the normal freshness tail.
    """
    trades = wins = losses = 0
    net = 0.0
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict) or record.get("kind") != "shadow_outcome":
                    continue
                payload = record.get("payload") or {}
                try:
                    value = float(payload.get("virtual_net_usd") or 0.0)
                except (TypeError, ValueError):
                    value = 0.0
                trades += 1
                net += value
                if value > 0:
                    wins += 1
                else:
                    losses += 1
    except OSError:
        return 0, 0.0, 0, 0
    return trades, net, wins, losses


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _newest(*values: float | None) -> float | None:
    present = [v for v in values if v is not None]
    return max(present) if present else None


# --- audit ---------------------------------------------------------------------------


def _audit_one(
    journal_path: Path,
    equity_path: Path,
    timeframe: str,
    now: float,
) -> tuple[bool, float | None, float | None, bool, bool, bool]:
    """Shared file-level probe: (exists, record_age, eval_age, evaluating,
    stale, silent) for one lane's journal/equity pair."""
    exists = journal_path.exists()
    tf_seconds = _timeframe_seconds(timeframe)

    journal_last_ts, last_eval_ts = (
        _scan_tail(journal_path) if exists else (None, None)
    )
    equity_last_ts = (
        _scan_tail(equity_path)[0] if equity_path.exists() else None
    )
    last_record_ts = _newest(journal_last_ts, equity_last_ts)
    if last_record_ts is None and exists:
        # journal probed open at startup but nothing written yet — fall back
        # to file mtime so an empty, abandoned journal still ages into STALE.
        last_record_ts = _newest(_mtime(journal_path), _mtime(equity_path))

    record_age = None if last_record_ts is None else max(0.0, now - last_record_ts)
    eval_age = None if last_eval_ts is None else max(0.0, now - last_eval_ts)

    evaluating = eval_age is not None and eval_age <= EVAL_TIMEFRAME_MULTIPLE * tf_seconds
    stale = record_age is not None and record_age > STALE_TIMEFRAME_MULTIPLE * tf_seconds

    # SILENT needs proof the lane has been active long enough that "no
    # lane_eval yet" cannot just be a fresh start.
    active_since_ts = _first_record_ts(journal_path) if exists else None
    if active_since_ts is None and exists:
        active_since_ts = _mtime(journal_path)
    active_seconds = 0.0 if active_since_ts is None else max(0.0, now - active_since_ts)
    no_recent_eval = eval_age is None or eval_age > SILENT_EVAL_SECONDS
    silent = exists and no_recent_eval and active_seconds > SILENT_EVAL_SECONDS

    return exists, record_age, eval_age, evaluating, stale, silent


def audit_lanes(
    journal_dir: Path | str,
    environ: Mapping[str, str] = os.environ,
    *,
    desired: list[LaneSpec] | None = None,
    now: float | None = None,
) -> LaneHealthReport:
    """Cross-check desired lane specs against the journal directory.

    ``desired`` overrides the env-derived spec list (tests / callers that
    already hold the runner's spec list); otherwise it is rebuilt from
    ``environ`` exactly the way the runner builds it.
    """
    journal_dir = Path(journal_dir)
    now = time.time() if now is None else now
    if desired is None:
        from vnedge.runtime.multi_lane_shadow import desired_lane_specs

        desired = desired_lane_specs(environ)
    try:
        shadow_probation_min_trades = int(
            environ.get("LANE_HEALTH_SHADOW_PROBATION_MIN_TRADES", "1")
        )
    except ValueError:
        shadow_probation_min_trades = 1

    rows: list[LaneHealthRow] = []
    desired_ids: set[str] = set()
    for spec in desired:
        desired_ids.add(spec.lane_id)
        journal_path = journal_dir / f"{spec.lane_id}{_JOURNAL_SUFFIX}"
        equity_path = journal_dir / f"{spec.lane_id}{_EQUITY_SUFFIX}"
        exists, record_age, eval_age, evaluating, stale, silent = _audit_one(
            journal_path, equity_path, spec.timeframe, now
        )
        mode = getattr(spec.mode, "value", str(spec.mode))
        shadow_trades = shadow_wins = shadow_losses = 0
        shadow_net = 0.0
        if mode == "shadow" and exists:
            shadow_trades, shadow_net, shadow_wins, shadow_losses = _scan_shadow_outcomes(
                journal_path
            )
        if not exists:
            verdict, detail = VERDICT_MISSING, "desired lane has no journal file"
        elif stale:
            verdict = VERDICT_STALE
            detail = (
                f"newest record {_fmt_age(record_age)} old "
                f"(> {STALE_TIMEFRAME_MULTIPLE:g}x {spec.timeframe})"
            )
        elif silent:
            verdict = VERDICT_SILENT
            detail = (
                "journaling but no lane_eval in "
                f"{_fmt_age(SILENT_EVAL_SECONDS)} — strategy loop not evaluating"
            )
        elif (
            mode == "shadow"
            and shadow_trades >= shadow_probation_min_trades
            and shadow_net < 0
        ):
            verdict = VERDICT_PROBATION
            detail = (
                f"shadow outcomes net ${shadow_net:+.2f} across "
                f"{shadow_trades} virtual trade(s); not paper/live compatible"
            )
        else:
            verdict, detail = VERDICT_OK, ""
        rows.append(LaneHealthRow(
            lane_id=spec.lane_id,
            verdict=verdict,
            exchange=spec.exchange,
            symbol=spec.symbol,
            timeframe=spec.timeframe,
            mode=mode,
            exists_active=exists,
            last_record_age_seconds=record_age,
            last_eval_age_seconds=eval_age,
            evaluating=evaluating,
            stale=stale,
            detail=detail,
            shadow_virtual_trades=shadow_trades,
            shadow_net_usd=shadow_net,
            shadow_wins=shadow_wins,
            shadow_losses=shadow_losses,
            trade_compatible=verdict == VERDICT_OK,
        ))

    # Orphans: journal files nothing in the desired list accounts for.
    if journal_dir.is_dir():
        for path in sorted(journal_dir.glob(f"*{_JOURNAL_SUFFIX}")):
            lane_id = path.name[: -len(_JOURNAL_SUFFIX)]
            if lane_id in desired_ids:
                continue
            _, record_age, eval_age, _, _, _ = _audit_one(
                path, journal_dir / f"{lane_id}{_EQUITY_SUFFIX}", "", now
            )
            rows.append(LaneHealthRow(
                lane_id=lane_id,
                verdict=VERDICT_ORPHAN,
                exists_active=True,
                last_record_age_seconds=record_age,
                last_eval_age_seconds=eval_age,
                detail="journal file has no desired lane spec (config change leftover?)",
                trade_compatible=False,
            ))

    totals = {
        "desired": len(desired),
        "active": sum(1 for r in rows if r.exists_active and r.verdict != VERDICT_ORPHAN),
        "ok": sum(1 for r in rows if r.verdict == VERDICT_OK),
        "stale": sum(1 for r in rows if r.verdict == VERDICT_STALE),
        "probation": sum(1 for r in rows if r.verdict == VERDICT_PROBATION),
        "silent": sum(1 for r in rows if r.verdict == VERDICT_SILENT),
        "missing": sum(1 for r in rows if r.verdict == VERDICT_MISSING),
        "orphan": sum(1 for r in rows if r.verdict == VERDICT_ORPHAN),
        "trade_compatible": sum(
            1
            for r in rows
            if r.trade_compatible and r.verdict != VERDICT_ORPHAN
        ),
        "production_blockers": sum(1 for r in rows if r.verdict != VERDICT_OK),
    }
    return LaneHealthReport(
        generated_at=datetime.fromtimestamp(now, tz=UTC).isoformat(),
        journal_dir=str(journal_dir),
        rows=tuple(rows),
        totals=totals,
    )


# --- CLI -----------------------------------------------------------------------------


def _fmt_age(seconds: float | None) -> str:
    if seconds is None:
        return "--"
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 90 * 60:
        return f"{seconds / 60:.1f}m"
    if seconds < 36 * 3600:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def _print_table(report: LaneHealthReport) -> None:
    headers = ("LANE", "VENUE", "SYMBOL", "TF", "MODE", "VERDICT", "LAST REC", "LAST EVAL")
    table = [headers] + [
        (
            row.lane_id,
            row.exchange or "--",
            row.symbol or "--",
            row.timeframe or "--",
            row.mode or "--",
            row.verdict,
            _fmt_age(row.last_record_age_seconds),
            _fmt_age(row.last_eval_age_seconds),
        )
        for row in report.rows
    ]
    widths = [max(len(line[col]) for line in table) for col in range(len(headers))]
    for line in table:
        print("  ".join(cell.ljust(width) for cell, width in zip(line, widths, strict=True)))
    print()
    totals = report.totals
    print(
        f"desired={totals['desired']} active={totals['active']} ok={totals['ok']} "
        f"stale={totals['stale']} probation={totals.get('probation', 0)} "
        f"silent={totals['silent']} "
        f"missing={totals['missing']} orphan={totals['orphan']}"
    )
    print(f"lane health: {report.summary()}")
    print(f"production readiness: {report.production_summary()}")


def main(
    argv: list[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m vnedge.runtime.lane_health",
        description="Audit desired vs active multi-lane journals "
                    "(exit 1 if any lane is MISSING or STALE).",
    )
    parser.add_argument(
        "--journal-dir",
        default=os.environ.get("MULTI_LANE_JOURNAL_DIR", "logs/paper_trials"),
        help="lane journal directory (default: %(default)s)",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit the full report as JSON"
    )
    args = parser.parse_args(argv)
    report = audit_lanes(args.journal_dir, environ if environ is not None else os.environ)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        _print_table(report)
    return 0 if report.healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
