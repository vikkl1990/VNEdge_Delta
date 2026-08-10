"""Decoupled tick/L2 research loop — restart-safe, per-symbol checkpoints.

Runs the L2-mining discovery passes — scalper discovery and the structural
alpha factory — on their own slower cadence (default 6h) so they never bloat
the hourly candle research loop.

Restart safety: a pass is expensive (mines the recorded tape per symbol), so it
is checkpointed to l2_progress.json AFTER EACH SYMBOL. On restart the loop
resumes from that file, skipping symbols already done for the same days — no
mining work is lost. Consumers read l2_latest.json, which only ever holds the
last COMPLETE pass (promoted from the progress file), so they never see a
partial regression. The candle loop folds l2_latest.json into its dashboard
latest.json.

Research-only, same hard guards as the inline passes: every output carries
can_trade=false / can_promote=false; raw hypotheses are not signals;
conservative replay + untouched-data judgment + human approval remain mandatory.
This loop discovers; it never trades or promotes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path

from vnedge.research.alpha_factory import build_alpha_tournament, run_alpha_factory
from vnedge.research.continuous_research import (
    OUT_DIR,
    _env_int,
    _scalper_research_days,
    run_scalper_research,
)
from vnedge.research.scalper_focus import build_scalper_focus
from vnedge.research.universe import load_research_targets
from vnedge.scalping.parameter_registry import DEFAULT_SCALPER_PARAMETER_REGISTRY

logger = logging.getLogger(__name__)

L2_LATEST = "l2_latest.json"
L2_PROGRESS = "l2_progress.json"

# keys constant across symbols (kept from the first) vs list results
# (accumulated across symbols) when merging per-symbol payloads.
_CONST_KEYS = frozenset({
    "policy", "flow", "flow_guards", "days", "targets",
    "generated_at", "note", "error", "source",
})


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _write_json(payload: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(path)   # atomic — readers only ever see a whole file


def load_l2_latest(out_dir: Path | None = None) -> dict:
    return _read_json((out_dir or OUT_DIR) / L2_LATEST)


def publish_l2(payload: dict, out_dir: Path | None = None) -> None:
    _write_json(payload, (out_dir or OUT_DIR) / L2_LATEST)


def _blank_pass(days: tuple[str, ...], total: int) -> dict:
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "days": list(days),
        "complete": False,
        "progress": {"completed_targets": [], "total": total},
        "scalper_parameter_registry": DEFAULT_SCALPER_PARAMETER_REGISTRY.to_dict(),
        "scalper_research": {},
        "alpha_factory": {},
    }


def _merge_stage(acc: dict, part: dict) -> None:
    """Accumulate a per-symbol stage payload: extend list results, keep the
    constant/scalar keys from the first symbol."""
    for k, v in part.items():
        if k in {"focus", "tournament"}:
            continue
        if k in _CONST_KEYS:
            acc.setdefault(k, v)
        elif isinstance(v, list):
            acc.setdefault(k, []).extend(v)
        else:
            acc.setdefault(k, v)


def _refresh_scalper_focus(payload: dict) -> None:
    sr = payload.get("scalper_research") or {}
    payload["scalper_research"] = sr
    sr["focus"] = build_scalper_focus(
        sr.get("scanner_results", []),
        sr.get("edge_hypotheses", []),
        recorder_targets=sr.get("recorder_targets", []),
        days=sr.get("days") or payload.get("days") or [],
        max_lanes=_env_int("SCALPER_FOCUS_MAX_LANES", 12),
        max_hypotheses=_env_int("SCALPER_FOCUS_MAX_HYPOTHESES", 12),
    )


def _refresh_alpha_tournament(payload: dict) -> None:
    af = payload.get("alpha_factory") or {}
    payload["alpha_factory"] = af
    af["tournament"] = build_alpha_tournament(
        af.get("hypotheses", []),
        max_rows=_env_int("ALPHA_TOURNAMENT_MAX_ROWS", 50),
    )


def run_incremental(data_root: str | Path = "data", out_dir: Path | None = None) -> dict:
    """One L2-mining pass, checkpointed per symbol and resumable across restarts."""
    out_dir = out_dir or OUT_DIR
    targets = load_research_targets()
    root = Path(data_root)
    days = _scalper_research_days(root, targets)
    max_rows = _env_int("ALPHA_FACTORY_MAX_ROWS", 50)

    prev = _read_json(out_dir / L2_PROGRESS)
    if prev.get("days") == list(days) and not prev.get("complete", True):
        payload = prev                     # resume the interrupted pass
        payload.setdefault("progress", {}).setdefault("completed_targets", [])
        payload["progress"]["total"] = len(targets)
        payload.setdefault(
            "scalper_parameter_registry",
            DEFAULT_SCALPER_PARAMETER_REGISTRY.to_dict(),
        )
        payload.setdefault("scalper_research", {})
        payload.setdefault("alpha_factory", {})
        payload["generated_at"] = datetime.now(UTC).isoformat()
        _refresh_scalper_focus(payload)
        _refresh_alpha_tournament(payload)
        # A resumed checkpoint may come from an older build. Refresh derived
        # telemetry before any expensive target finishes so the dashboard does
        # not keep showing a stale focus/tournament view for another cycle.
        _write_json(payload, out_dir / L2_PROGRESS)
    else:
        payload = _blank_pass(days, len(targets))
    done = set(payload["progress"]["completed_targets"])

    for target in targets:
        if target.label in done:
            continue                       # already mined this symbol this pass
        one = (target,)
        _merge_stage(payload["scalper_research"],
                     run_scalper_research(data_root, one, days=days))
        _merge_stage(payload["alpha_factory"],
                     run_alpha_factory(data_root, one, days=days, max_rows=max_rows))
        payload["progress"]["completed_targets"].append(target.label)
        payload["generated_at"] = datetime.now(UTC).isoformat()
        _refresh_scalper_focus(payload)
        _refresh_alpha_tournament(payload)
        _write_json(payload, out_dir / L2_PROGRESS)   # CHECKPOINT after each symbol

    payload["complete"] = True
    _refresh_scalper_focus(payload)
    _refresh_alpha_tournament(payload)
    _write_json(payload, out_dir / L2_PROGRESS)
    publish_l2(payload, out_dir)           # promote the complete pass for consumers
    return payload


async def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    interval = _env_int("L2_RESEARCH_INTERVAL_SECONDS", 21600)  # 6h
    logger.info("l2 research loop: every %ds -> %s (resumable via %s)",
                interval, OUT_DIR / L2_LATEST, L2_PROGRESS)
    while True:
        started = time.time()
        try:
            payload = run_incremental("data")
            sr, af = payload["scalper_research"], payload["alpha_factory"]
            logger.info(
                "l2 research: %d/%d symbols | %d edge / %d scanned / %d replay-cand "
                "| %d alpha hyps | %.0fs",
                len(payload["progress"]["completed_targets"]),
                payload["progress"]["total"],
                len(sr.get("edge_hypotheses", [])),
                len(sr.get("scanner_results", [])),
                len(sr.get("replay_candidates", [])),
                len(af.get("hypotheses", [])),
                time.time() - started,
            )
        except Exception as exc:  # noqa: BLE001 — one cycle must not kill the loop
            logger.exception("l2 research cycle failed: %s", exc)
        await asyncio.sleep(interval)


if __name__ == "__main__":
    asyncio.run(main())
