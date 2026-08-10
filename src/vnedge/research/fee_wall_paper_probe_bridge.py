"""Publish durable paper-probe manifests from fee-wall evidence.

``fee_wall_forensics_latest.json`` is a research report.  ``multi_lane_shadow``
needs a stable ``fee_wall_paper_probes.json`` manifest before it can launch
isolated simulated-paper probe lanes.  This bridge is the deliberately narrow
adapter between those two worlds:

* accepts only strict fee-wall candidates with enough routed examples;
* keeps live trading impossible (paper probes only);
* writes an atomic manifest plus compact feed row;
* leaves the existing multi-lane risk/order gates unchanged.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
import json
from pathlib import Path
from tempfile import NamedTemporaryFile
import time
from typing import Mapping


FEE_WALL_PAPER_PROBE_BRIDGE_ID = "fee_wall_paper_probe_bridge_v1"
DEFAULT_INPUT = Path("research/live_research/fee_wall_forensics_latest.json")
DEFAULT_OUT = Path("research/live_research/fee_wall_paper_probes.json")
DEFAULT_FEED = Path("research/live_research/fee_wall_paper_probes_feed.jsonl")
DEFAULT_ALLOWED_STRATEGIES = (
    "context_scalper_v2",
    "fvg_liquidity_breakout_v1",
    "luxara_live_plan_qtm_v1",
    "luxy_ut_bot_forecast_v1",
    "quantified_fee_wall_sniper_v1",
    "stealth_trail_bbp_v1",
)
DEFAULT_ALLOWED_VERDICTS = ("MAKER_EDGE", "MIXED_ROUTE_EDGE", "TAKER_EDGE")
REQUIRED_ACTION = "PRE_REGISTER_UNTOUCHED_JUDGMENT_WINDOW"


@dataclass(frozen=True)
class ProbeBridgeConfig:
    min_routed: int = 10
    min_avg_net_bps: float = 8.0
    min_profit_factor: float = 1.15
    max_probes: int = 12
    allowed_strategies: tuple[str, ...] = field(default_factory=lambda: DEFAULT_ALLOWED_STRATEGIES)
    allowed_verdicts: tuple[str, ...] = field(default_factory=lambda: DEFAULT_ALLOWED_VERDICTS)

    def __post_init__(self) -> None:
        if self.min_routed < 1:
            raise ValueError("min_routed must be >= 1")
        if self.min_avg_net_bps <= 0:
            raise ValueError("min_avg_net_bps must be positive")
        if self.min_profit_factor < 1.0:
            raise ValueError("min_profit_factor must be >= 1")
        if self.max_probes < 1:
            raise ValueError("max_probes must be >= 1")


def build_fee_wall_paper_probe_manifest(
    fee_wall_payload: Mapping[str, object],
    *,
    config: ProbeBridgeConfig = ProbeBridgeConfig(),
    now: datetime | None = None,
) -> dict:
    generated = now or datetime.now(UTC)
    raw = fee_wall_payload.get("strict_fee_wall_candidates")
    candidates = [row for row in raw if isinstance(row, Mapping)] if isinstance(raw, list) else []
    accepted: list[dict] = []
    rejected: list[dict] = []
    for candidate in candidates:
        normalized = _normalize_candidate(candidate)
        reasons = _rejection_reasons(normalized, config)
        if reasons:
            rejected.append({**normalized, "rejected_reasons": reasons})
            continue
        accepted.append(normalized)
    accepted.sort(key=_rank_key, reverse=True)
    selected = accepted[: config.max_probes]
    return {
        "manifest_id": FEE_WALL_PAPER_PROBE_BRIDGE_ID,
        "generated_at": generated.isoformat(),
        "source_generated_at": fee_wall_payload.get("generated_at"),
        "source_truth_layer": fee_wall_payload.get("truth_layer"),
        "approval": "auto-paper-probe-from-strict-fee-wall-evidence",
        "approved_by": "fee_wall_paper_probe_bridge",
        "policy": {
            "paper_only": True,
            "simulated_fills_only": True,
            "can_trade_live": False,
            "can_promote": False,
            "requires_untouched_judgment_for_promotion": True,
            "live_governance_unchanged": True,
        },
        "config": asdict(config),
        "summary": {
            "source_candidates": len(candidates),
            "eligible_candidates": len(accepted),
            "published_probes": len(selected),
            "rejected_candidates": len(rejected),
            "top_strategy": selected[0]["strategy"] if selected else None,
            "top_avg_net_bps": selected[0]["avg_selected_net_bps"] if selected else None,
        },
        "paper_probes": [_probe_row(row, rank=i + 1) for i, row in enumerate(selected)],
        "rejected": rejected[:25],
        "can_trade": False,
        "can_promote": False,
    }


def publish_fee_wall_paper_probe_manifest(
    payload: dict,
    *,
    out: Path | str = DEFAULT_OUT,
    feed: Path | str | None = DEFAULT_FEED,
) -> Path:
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    with NamedTemporaryFile(
        "w",
        dir=out_path.parent,
        prefix=out_path.name,
        suffix=".tmp",
        delete=False,
        encoding="utf-8",
    ) as tmp:
        tmp.write(encoded)
        tmp_path = Path(tmp.name)
    tmp_path.chmod(0o644)
    tmp_path.replace(out_path)
    out_path.chmod(0o644)
    if feed is not None:
        feed_path = Path(feed)
        feed_path.parent.mkdir(parents=True, exist_ok=True)
        with feed_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_feed_record(payload), sort_keys=True) + "\n")
        feed_path.chmod(0o644)
    return out_path


def _normalize_candidate(candidate: Mapping[str, object]) -> dict:
    return {
        "exchange": str(candidate.get("exchange") or ""),
        "symbol": str(candidate.get("symbol") or ""),
        "timeframe": str(candidate.get("timeframe") or ""),
        "strategy": str(candidate.get("strategy") or candidate.get("strategy_id") or ""),
        "verdict": str(candidate.get("verdict") or ""),
        "recommended_action": str(candidate.get("recommended_action") or ""),
        "routed": _int_or_zero(candidate.get("routed") or candidate.get("opportunities")),
        "avg_selected_net_bps": _float_or_none(candidate.get("avg_selected_net_bps")),
        "profit_factor": _float_or_none(candidate.get("profit_factor")),
        "fee_wall_break_rate_pct": _float_or_none(candidate.get("fee_wall_break_rate_pct")),
        "paper_margin_usd": _float_or_none(candidate.get("paper_margin_usd")),
        "paper_leverage": _float_or_none(candidate.get("paper_leverage")),
        "raw": dict(candidate),
    }


def _rejection_reasons(candidate: Mapping[str, object], config: ProbeBridgeConfig) -> list[str]:
    reasons: list[str] = []
    if not candidate["exchange"] or not candidate["symbol"] or not candidate["timeframe"]:
        reasons.append("missing_lane_identity")
    if candidate["strategy"] not in config.allowed_strategies:
        reasons.append("strategy_not_probe_allowed")
    if candidate["verdict"] not in config.allowed_verdicts:
        reasons.append("verdict_not_probe_allowed")
    if candidate["recommended_action"] != REQUIRED_ACTION:
        reasons.append("not_pre_registered_judgment_candidate")
    if int(candidate["routed"]) < config.min_routed:
        reasons.append("under_min_routed")
    avg = candidate["avg_selected_net_bps"]
    if avg is None or float(avg) < config.min_avg_net_bps:
        reasons.append("avg_net_bps_below_probe_floor")
    pf = candidate["profit_factor"]
    if pf is None or float(pf) < config.min_profit_factor:
        reasons.append("profit_factor_below_probe_floor")
    return reasons


def _probe_row(candidate: Mapping[str, object], *, rank: int) -> dict:
    exchange = str(candidate["exchange"])
    symbol = str(candidate["symbol"])
    timeframe = str(candidate["timeframe"])
    strategy = str(candidate["strategy"])
    return {
        "rank": rank,
        "probe_id": _probe_id(exchange, symbol, timeframe, strategy),
        "exchange": exchange,
        "symbol": symbol,
        "timeframe": timeframe,
        "strategy": strategy,
        "strategy_id": strategy,
        "verdict": candidate["verdict"],
        "recommended_action": REQUIRED_ACTION,
        "routed": int(candidate["routed"]),
        "avg_selected_net_bps": candidate["avg_selected_net_bps"],
        "profit_factor": candidate["profit_factor"],
        "fee_wall_break_rate_pct": candidate["fee_wall_break_rate_pct"],
        "paper_margin_usd": candidate["paper_margin_usd"],
        "paper_leverage": candidate["paper_leverage"],
        "mode": "paper_probe",
        "paper_only": True,
        "can_trade_live": False,
        "can_promote": False,
    }


def _rank_key(candidate: Mapping[str, object]) -> tuple[float, float, int, float]:
    avg = _float_or_none(candidate.get("avg_selected_net_bps")) or -1e9
    pf = _float_or_none(candidate.get("profit_factor")) or -1e9
    routed = int(candidate.get("routed") or 0)
    break_rate = _float_or_none(candidate.get("fee_wall_break_rate_pct")) or 0.0
    return (avg, pf, routed, break_rate)


def _probe_id(exchange: str, symbol: str, timeframe: str, strategy: str) -> str:
    return (
        f"{strategy}__{exchange}__{symbol}__{timeframe}"
        .lower()
        .replace("/", "_")
        .replace(":", "_")
        .replace("-", "_")
    )


def _feed_record(payload: Mapping[str, object]) -> dict:
    return {
        "manifest_id": payload.get("manifest_id"),
        "generated_at": payload.get("generated_at"),
        "published_probes": (payload.get("summary") or {}).get("published_probes")
        if isinstance(payload.get("summary"), Mapping)
        else None,
        "top_strategy": (payload.get("summary") or {}).get("top_strategy")
        if isinstance(payload.get("summary"), Mapping)
        else None,
        "top_avg_net_bps": (payload.get("summary") or {}).get("top_avg_net_bps")
        if isinstance(payload.get("summary"), Mapping)
        else None,
        "can_trade": False,
        "can_promote": False,
    }


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {"strict_fee_wall_candidates": []}
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload if isinstance(payload, dict) else {"strict_fee_wall_candidates": []}


def _float_or_none(value: object) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None


def _int_or_zero(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="publish fee-wall paper probe manifest")
    parser.add_argument("--input", default=str(DEFAULT_INPUT))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--feed", default=str(DEFAULT_FEED))
    parser.add_argument("--min-routed", type=int, default=10)
    parser.add_argument("--min-avg-net-bps", type=float, default=8.0)
    parser.add_argument("--min-profit-factor", type=float, default=1.15)
    parser.add_argument("--max-probes", type=int, default=12)
    parser.add_argument("--allowed-strategies", default=",".join(DEFAULT_ALLOWED_STRATEGIES))
    parser.add_argument("--allowed-verdicts", default=",".join(DEFAULT_ALLOWED_VERDICTS))
    parser.add_argument("--interval-seconds", type=int, default=0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    config = ProbeBridgeConfig(
        min_routed=args.min_routed,
        min_avg_net_bps=args.min_avg_net_bps,
        min_profit_factor=args.min_profit_factor,
        max_probes=args.max_probes,
        allowed_strategies=_split_csv(args.allowed_strategies),
        allowed_verdicts=_split_csv(args.allowed_verdicts),
    )
    while True:
        manifest = build_fee_wall_paper_probe_manifest(
            _read_json(Path(args.input)),
            config=config,
        )
        path = publish_fee_wall_paper_probe_manifest(
            manifest,
            out=args.out,
            feed=None if args.feed == "" else args.feed,
        )
        if args.json:
            print(json.dumps(manifest, indent=2, sort_keys=True), flush=True)
        else:
            summary = manifest["summary"]
            print(
                f"fee-wall paper probe bridge wrote {path} "
                f"({summary['published_probes']} probes)",
                flush=True,
            )
        if args.interval_seconds <= 0:
            break
        time.sleep(max(1, args.interval_seconds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
