"""Experiment index — one queryable view over VNEDGE's scattered run records.

Every research run already lands *somewhere*, but each pipeline writes its own
store and nothing joins them:

- rolling walk-forward verdicts  -> ``research/live_research/feed.jsonl``
- pre-registered untouched judgments (+ their provenance) ->
  ``research/judgments/burn_registry.jsonl`` (owned by ``data_burn``)
- paper-forward trial outcomes -> ``research/paper_trials/<id>.reports.jsonl``

So "show me every run, its verdict, its metrics, and whether the data was
untouched" has no single answer. This module is that answer: an MLflow-style
read-only join over the existing artifacts, exposing ``query`` / ``best`` /
``distinct`` / ``summary`` over one ``RunRecord`` schema.

It is a READER, not a new write path — it duplicates none of the stores it
reads, writes no run, grants no permission, and cannot promote a strategy. The
burn registry remains the provenance authority: a run is ``promotable`` here
**only** when it is a pre-registered untouched judgment that PASSed — exactly the
rule ``data_burn.judge_untouched`` enforces on the write side. Rolling research
and paper-forward runs are surfaced but never marked promotable by this index;
paper promotion has its own human-gated path.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Callable

from vnedge.research.data_burn import (
    DEFAULT_REGISTRY_PATH as DEFAULT_BURN_REGISTRY_PATH,
    KIND_JUDGMENT,
    read_records as read_burn_records,
)

EXPERIMENT_INDEX_ID = "research_experiment_index_v1"

DEFAULT_RESEARCH_DIR = Path("research/live_research")
DEFAULT_FEED = DEFAULT_RESEARCH_DIR / "feed.jsonl"
DEFAULT_PAPER_TRIALS_DIR = Path("research/paper_trials")
DEFAULT_OUT = DEFAULT_RESEARCH_DIR / "experiment_index_latest.json"

# run_kind values — the source pipeline a record came from.
KIND_WALK_FORWARD = "walk_forward"
KIND_UNTOUCHED_JUDGMENT = "untouched_judgment"
KIND_PAPER_TRIAL = "paper_trial"

# data_provenance values — how "burned" the underlying data is. Only the first
# is promotion-eligible; that is the whole point of the discipline.
PROV_UNTOUCHED = "untouched_judgment"
PROV_ROLLING = "rolling_research"
PROV_PAPER_FORWARD = "paper_forward"


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    run_kind: str
    strategy_id: str
    symbol: str
    exchange: str
    timeframe: str
    verdict: str
    data_provenance: str
    promotable: bool
    recorded_at: str
    metrics: dict[str, Any] = field(default_factory=dict)
    gates_failed: list[str] = field(default_factory=list)
    data_window: dict[str, Any] = field(default_factory=dict)
    commit: str = ""
    source_artifact: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _lane(strategy_id: str, exchange: str, symbol: str, timeframe: str) -> str:
    return f"{strategy_id}|{exchange}|{symbol}|{timeframe}"


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a torn tail line is not a reason to lose the whole index
    return out


# --------------------------------------------------------------------- loaders
def _records_from_feed(feed_path: Path) -> list[RunRecord]:
    """Rolling walk-forward verdicts (continuous_research ``wf_record`` shape)."""
    out: list[RunRecord] = []
    for i, rec in enumerate(_read_jsonl(feed_path)):
        strategy = rec.get("strategy", "")
        symbol = rec.get("symbol", "")
        exchange = rec.get("exchange", "")
        timeframe = rec.get("timeframe", "")
        verdict = rec.get("verdict", "")
        updated = rec.get("updated", "")
        # Rolling research runs are NEVER promotable via this index — they run on
        # seen/rolling data by construction; promotion needs an untouched judgment.
        out.append(
            RunRecord(
                run_id=f"wf-{i}-{_lane(strategy, exchange, symbol, timeframe)}",
                run_kind=KIND_WALK_FORWARD,
                strategy_id=strategy,
                symbol=symbol,
                exchange=exchange,
                timeframe=timeframe,
                verdict=verdict,
                data_provenance=PROV_ROLLING,
                promotable=False,
                recorded_at=updated,
                metrics={
                    "oos_net_usd": rec.get("oos_net_usd"),
                    "oos_trades": rec.get("oos_trades"),
                    "profit_factor": rec.get("profit_factor"),
                    "payoff_ratio": rec.get("payoff_ratio"),
                    "profitable_windows_pct": rec.get("profitable_windows_pct"),
                    "windows": rec.get("windows"),
                    "traded_windows": rec.get("traded_windows"),
                    "total_fees_usd": rec.get("total_fees_usd"),
                    "max_consecutive_stops": rec.get("max_consecutive_stops"),
                },
                gates_failed=list(rec.get("reasons", []) or []),
                commit=str(rec.get("attribution", "") or ""),
                source_artifact=feed_path.name,
                note="auto_explore" if rec.get("auto") else "",
            )
        )
    return out


def _records_from_burn_registry(registry_path: Path) -> list[RunRecord]:
    """Pre-registered judgments — the burn registry is the provenance authority."""
    out: list[RunRecord] = []
    try:
        records = read_burn_records(registry_path)
    except (FileNotFoundError, ValueError):
        return out
    for i, rec in enumerate(records):
        if rec.get("kind") != KIND_JUDGMENT:
            continue  # exploratory burns are not judgments; skip
        strategy = rec.get("strategy_id", "")
        symbol = rec.get("symbol", "")
        exchange = rec.get("exchange", "")
        verdict = rec.get("verdict", "")
        out.append(
            RunRecord(
                run_id=f"judg-{i}-{strategy}|{symbol}",
                run_kind=KIND_UNTOUCHED_JUDGMENT,
                strategy_id=strategy,
                symbol=symbol,
                exchange=exchange,
                timeframe="",
                verdict=verdict,
                data_provenance=PROV_UNTOUCHED,
                # The one promotion-eligible shape: an untouched judgment that PASSed.
                promotable=(verdict == "PASS"),
                recorded_at=rec.get("registered_at", ""),
                metrics={},
                gates_failed=[],
                data_window={
                    "start": rec.get("window_start", ""),
                    "end": rec.get("window_end", ""),
                },
                source_artifact=registry_path.name,
                note=rec.get("note", "") or "",
            )
        )
    return out


def _records_from_paper_trials(trials_dir: Path) -> list[RunRecord]:
    """Paper-forward trial outcomes — one record per appended report row."""
    out: list[RunRecord] = []
    if not trials_dir.exists():
        return out
    for reports_path in sorted(trials_dir.glob("*.reports.jsonl")):
        for i, row in enumerate(_read_jsonl(reports_path)):
            report = row.get("report", {}) or {}
            strategy = report.get("strategy_id", row.get("manifest_strategy", ""))
            symbol = report.get("symbol", "")
            pnl = report.get("realized_pnl_usd")
            # Paper trials do not carry a PASS/REJECT here — the verdict is the
            # trial's own pass/fail machinery. Surface an honest neutral verdict.
            out.append(
                RunRecord(
                    run_id=f"paper-{reports_path.stem}-{i}",
                    run_kind=KIND_PAPER_TRIAL,
                    strategy_id=strategy,
                    symbol=symbol,
                    exchange="",
                    timeframe="",
                    verdict="PAPER_FORWARD",
                    data_provenance=PROV_PAPER_FORWARD,
                    promotable=False,
                    recorded_at=row.get("ts", ""),
                    metrics={
                        "realized_pnl_usd": pnl,
                        "unrealized_pnl_usd": report.get("unrealized_pnl_usd"),
                        "max_drawdown_pct": report.get("max_drawdown_pct"),
                        "fills": report.get("fills"),
                        "orders_submitted": report.get("orders_submitted"),
                        "fees_usd": report.get("fees_usd"),
                        "final_equity_usd": report.get("final_equity_usd"),
                        "risk_rejects": report.get("risk_rejects"),
                        "reconciliation_mismatches": report.get("reconciliation_mismatches"),
                    },
                    commit=str(row.get("run_commit", "") or ""),
                    source_artifact=reports_path.name,
                    note=f"trial={row.get('trial_id', '')}",
                )
            )
    return out


# --------------------------------------------------------------------- queries
def query(
    records: list[RunRecord],
    *,
    run_kind: str | None = None,
    strategy_id: str | None = None,
    symbol: str | None = None,
    exchange: str | None = None,
    verdict: str | None = None,
    provenance: str | None = None,
    promotable: bool | None = None,
) -> list[RunRecord]:
    """Filter the index. Every criterion is an AND; ``None`` means "don't care"."""
    rows = records
    preds: list[Callable[[RunRecord], bool]] = []
    if run_kind is not None:
        preds.append(lambda r: r.run_kind == run_kind)
    if strategy_id is not None:
        preds.append(lambda r: r.strategy_id == strategy_id)
    if symbol is not None:
        preds.append(lambda r: r.symbol == symbol)
    if exchange is not None:
        preds.append(lambda r: r.exchange == exchange)
    if verdict is not None:
        preds.append(lambda r: r.verdict == verdict)
    if provenance is not None:
        preds.append(lambda r: r.data_provenance == provenance)
    if promotable is not None:
        preds.append(lambda r: r.promotable == promotable)
    return [r for r in rows if all(p(r) for p in preds)]


def best(
    records: list[RunRecord],
    *,
    metric: str,
    limit: int = 10,
    ascending: bool = False,
) -> list[RunRecord]:
    """Top runs by a numeric metric key; records missing the metric are dropped."""
    scored = [
        (r, r.metrics.get(metric))
        for r in records
        if isinstance(r.metrics.get(metric), (int, float))
    ]
    scored.sort(key=lambda kv: kv[1], reverse=not ascending)
    return [r for r, _ in scored[:limit]]


def distinct(records: list[RunRecord], field_name: str) -> list[Any]:
    """Sorted unique values of a top-level RunRecord field (e.g. ``strategy_id``)."""
    seen: list[Any] = []
    for r in records:
        v = getattr(r, field_name, None)
        if v not in seen:
            seen.append(v)
    return sorted(seen, key=lambda x: (x is None, str(x)))


def _summary(records: list[RunRecord]) -> dict[str, Any]:
    by_kind: dict[str, int] = {}
    for r in records:
        by_kind[r.run_kind] = by_kind.get(r.run_kind, 0) + 1
    promotable = [r for r in records if r.promotable]
    return {
        "total": len(records),
        "by_kind": by_kind,
        "strategies": len(distinct(records, "strategy_id")),
        "symbols": len(distinct(records, "symbol")),
        "promotable": len(promotable),
        "untouched_judgments": len(query(records, run_kind=KIND_UNTOUCHED_JUDGMENT)),
        "passes_on_untouched": len(
            query(records, provenance=PROV_UNTOUCHED, verdict="PASS")
        ),
    }


def build_experiment_index(
    *,
    feed_path: Path | str = DEFAULT_FEED,
    burn_registry_path: Path | str = DEFAULT_BURN_REGISTRY_PATH,
    paper_trials_dir: Path | str = DEFAULT_PAPER_TRIALS_DIR,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Join every run store into one read-only, queryable snapshot payload."""
    records: list[RunRecord] = []
    records.extend(_records_from_feed(Path(feed_path)))
    records.extend(_records_from_burn_registry(Path(burn_registry_path)))
    records.extend(_records_from_paper_trials(Path(paper_trials_dir)))
    records.sort(key=lambda r: (r.recorded_at, r.run_id))

    promotable = query(records, promotable=True)
    return {
        "experiment_index_id": EXPERIMENT_INDEX_ID,
        "generated_at": (now or datetime.now(UTC)).isoformat(),
        "summary": _summary(records),
        "records": [r.to_dict() for r in records],
        "promotable": [r.to_dict() for r in promotable],
        "policy": {
            "source": "read-only join of feed.jsonl + burn_registry.jsonl + paper_trials",
            "promotable_requires_untouched_judgment_pass": True,
            "provenance_authority": "research/judgments/burn_registry.jsonl (data_burn)",
            "can_trade": False,
            "can_promote": False,
        },
    }


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w", dir=path.parent, prefix=path.name, suffix=".tmp", delete=False, encoding="utf-8"
    ) as tmp:
        json.dump(payload, tmp, indent=2, sort_keys=True, default=str)
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)
    path.chmod(0o644)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the unified experiment index (read-only).")
    parser.add_argument("--feed", default=str(DEFAULT_FEED))
    parser.add_argument("--burn-registry", default=str(DEFAULT_BURN_REGISTRY_PATH))
    parser.add_argument("--paper-trials", default=str(DEFAULT_PAPER_TRIALS_DIR))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--print", action="store_true", help="print the summary to stdout")
    args = parser.parse_args(argv)

    payload = build_experiment_index(
        feed_path=args.feed,
        burn_registry_path=args.burn_registry,
        paper_trials_dir=args.paper_trials,
    )
    _atomic_write_json(Path(args.out), payload)
    if args.print:
        print(json.dumps(payload["summary"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
