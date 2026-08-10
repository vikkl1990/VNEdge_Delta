"""Research-winner -> shadow-lane manifest (the research/shadow bridge).

The slow research loop finds profitable candidate pairs; this turns the ones
whose strategy has HUMAN-LOCKED runtime params into shadow-only lane specs the
multi-lane runner can watch live. The lock is the safety line: a profitable
pair whose strategy has no locked params is BLOCKED, not run — we never
auto-spawn a live-data lane on un-vetted parameters.

Shadow-only and non-promoting: every output carries can_trade=false /
can_promote=false. A shadow lane is for observation; promotion to paper/live
still requires a human-approved, pre-registered judgment on untouched data.
"""

from __future__ import annotations

import json
import hashlib
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path

# Strategy runtime params a human has locked as safe to run as a SHADOW lane.
# A research winner whose strategy is absent here is surfaced but NOT run —
# that is the line between assistant and overfitter.
RUNTIME_LOCKED_PARAMS: dict[str, dict] = {
    "funding_mean_reversion_v1": {
        "extreme_pct": 0.85, "z_entry": 1.5,
        "funding_pct_window": 240, "z_window": 48, "stop_atr_mult": 1.5,
    },
    "trend_continuation_v1": {},              # default config (judged on DOGE/XRP)
    "volatility_expansion_breakout_v1": {},   # default config (DOGE-judged)
    "alpha_stack_confluence_v1": {},          # research-only confluence default
    "quant_signal_pack_v1": {},               # research-only signal-pack default
    "sats_5m_scalper_v1": {},                 # research-only 5m TQI/BBP lane
    "trend_retest_v1": {},                    # research-only retest-entry default
}

SHADOW_MANIFEST = "shadow_lanes.json"
MANIFEST_VERSION = 2

_REPLAY_CANDIDATE_VERDICTS = {"REPLAY_CANDIDATE"}


def _lane_id(exchange: str, symbol: str, strategy: str) -> str:
    s = symbol.split(":")[0].replace("/", "").lower()
    return f"{strategy}_{exchange}_{s}_shadow"


def generate_shadow_manifest(
    profitable_pairs,
    *,
    max_lanes: int = 12,
    max_shadow_trials: int = 20,
    judgment_records: Iterable[dict] | None = None,
    filtered_replay_payload: Mapping | None = None,
) -> dict:
    """Turn research `profitable_pairs` (dicts) into a shadow-lane manifest.
    Runnable lanes require locked params; the rest are surfaced as blocked."""
    lanes: list[dict] = []
    blocked: list[dict] = []
    seen: set[str] = set()
    judgments = _latest_judgments(judgment_records or ())
    for p in profitable_pairs:
        strat = p.get("best_strategy") or p.get("strategy")
        exchange, symbol = p.get("exchange"), p.get("symbol")
        if not (strat and exchange and symbol):
            continue
        latest = judgments.get((exchange, symbol, strat))
        if latest and latest.get("verdict") == "REJECT":
            blocked.append({
                "exchange": exchange, "symbol": symbol, "strategy_id": strat,
                "reason": "latest untouched judgment rejected — requires a fresh "
                          "approved judgment before shadow expansion",
                "latest_judgment": latest,
            })
            continue
        if strat not in RUNTIME_LOCKED_PARAMS:
            blocked.append({
                "exchange": exchange, "symbol": symbol, "strategy_id": strat,
                "reason": "no human-locked runtime params — cannot auto-run",
            })
            continue
        lane_id = _lane_id(exchange, symbol, strat)
        if lane_id in seen:
            continue
        seen.add(lane_id)
        lanes.append({
            "lane_id": lane_id,
            "exchange": exchange, "symbol": symbol,
            "timeframe": p.get("timeframe", "1h"),
            "strategy_id": strat,
            "strategy_params": dict(RUNTIME_LOCKED_PARAMS[strat]),
            "mode": "shadow",
            "source_verdict": p.get("verdict"),
            "oos_net_usd": p.get("oos_net_usd"),
            "latest_judgment": latest,
        })
    return {
        "manifest_version": MANIFEST_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "policy": {
            "can_trade": False, "can_promote": False,
            "requires_untouched_judgment": True, "shadow_only": True,
        },
        "lanes": lanes[:max_lanes],
        "shadow_trials": _shadow_trials_from_filtered_replay(
            filtered_replay_payload, max_shadow_trials=max_shadow_trials
        ),
        "blocked": blocked,
    }


def _latest_judgments(records: Iterable[dict]) -> dict[tuple[str, str, str], dict]:
    latest: dict[tuple[str, str, str], dict] = {}
    for record in records:
        if record.get("kind") != "judgment":
            continue
        exchange = record.get("exchange")
        symbol = record.get("symbol")
        strategy = record.get("strategy_id")
        if not (exchange and symbol and strategy):
            continue
        latest[(str(exchange), str(symbol), str(strategy))] = {
            "verdict": record.get("verdict"),
            "window_start": record.get("window_start"),
            "window_end": record.get("window_end"),
            "note": record.get("note", ""),
        }
    return latest


def _shadow_trials_from_filtered_replay(
    payload: Mapping | None,
    *,
    max_shadow_trials: int,
) -> list[dict]:
    """Filtered replay winners become governed trial candidates, not lanes.

    Conservative replay rows are event hypotheses, not BaseStrategy runtime
    implementations. They are therefore kept out of ``lanes`` until a proper
    shadow adapter exists that can emit real-time intents/outcomes through the
    same journal/gateway observability path as candle strategies.
    """
    if not payload:
        return []
    rows: list[dict] = []
    seen: set[str] = set()
    for row in payload.get("rows", []) or []:
        if not isinstance(row, Mapping):
            continue
        verdict = str(row.get("verdict") or "")
        if verdict not in _REPLAY_CANDIDATE_VERDICTS:
            continue
        candidate_id = str(row.get("candidate_id") or "")
        exchange = str(row.get("exchange") or "")
        symbol = str(row.get("symbol") or "")
        if not (candidate_id and exchange and symbol):
            continue
        dedupe_key = "|".join(
            [
                candidate_id,
                str(row.get("filter_name") or ""),
                str(row.get("day") or ""),
            ]
        )
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        rows.append(_shadow_trial_row(row))
    rows.sort(
        key=lambda item: (
            -float(item["replay"].get("avg_net_bps") or -999.0),
            -float(item["replay"].get("net_usd") or 0.0),
            item["trial_id"],
        )
    )
    return rows[:max_shadow_trials]


def _shadow_trial_row(row: Mapping) -> dict:
    candidate_id = str(row.get("candidate_id") or "")
    evidence = row.get("evidence")
    evidence = evidence if isinstance(evidence, Mapping) else {}
    return {
        "trial_id": _shadow_trial_id(candidate_id, str(row.get("filter_name") or "")),
        "candidate_id": candidate_id,
        "source": str(row.get("source") or ""),
        "family": str(row.get("family") or ""),
        "exchange": str(row.get("exchange") or ""),
        "symbol": str(row.get("symbol") or ""),
        "day": str(row.get("day") or ""),
        "side": str(row.get("side") or ""),
        "trigger_ts": row.get("trigger_ts"),
        "mode": "shadow_trial",
        "status": "REPLAY_POSITIVE_NEEDS_SHADOW_ADAPTER",
        "runtime_strategy_id": None,
        "runtime_adapter": None,
        "timeframe": row.get("timeframe") or evidence.get("timeframe"),
        "filter": {
            "name": str(row.get("filter_name") or ""),
            "condition_bucket": str(row.get("condition_bucket") or ""),
        },
        "replay": {
            "verdict": str(row.get("verdict") or ""),
            "quotes": int(row.get("quotes") or 0),
            "fills": int(row.get("fills") or 0),
            "net_usd": _float_or_none(row.get("net_usd")),
            "avg_net_bps": _float_or_none(row.get("avg_net_bps")),
            "profit_factor": _float_or_none(row.get("profit_factor")),
            "fill_rate_pct": _float_or_none(row.get("fill_rate_pct")),
        },
        "policy": {
            "can_trade": False,
            "can_promote": False,
            "paper_ready": False,
            "live_ready": False,
            "requires_runtime_shadow_adapter": True,
            "requires_live_shadow_outcomes": True,
            "requires_human_approval": True,
            "requires_untouched_judgment": True,
        },
        "next_action": (
            "build runtime shadow adapter for this event family, then collect "
            "live shadow intents/outcomes before paper review"
        ),
    }


def _shadow_trial_id(candidate_id: str, filter_name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", candidate_id).strip("_").lower()
    slug = slug[:64] or "candidate"
    digest = hashlib.sha256(f"{candidate_id}|{filter_name}".encode()).hexdigest()[:10]
    return f"shadow_trial_{slug}_{digest}"


def _float_or_none(value) -> float | None:
    try:
        return round(float(value), 6)
    except (TypeError, ValueError):
        return None


def write_shadow_manifest(manifest: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / (SHADOW_MANIFEST + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=2))
    tmp.replace(out_dir / SHADOW_MANIFEST)   # atomic


def load_shadow_manifest(out_dir: Path) -> dict:
    path = out_dir / SHADOW_MANIFEST
    if not path.exists():
        return {"lanes": [], "shadow_trials": [], "blocked": []}
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {"lanes": [], "shadow_trials": [], "blocked": []}
