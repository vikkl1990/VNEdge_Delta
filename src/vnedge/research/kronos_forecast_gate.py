"""Kronos-style forecast gate for VNEDGE research lanes.

Kronos is useful to VNEDGE as a path forecaster, not as an order policy.
This module consumes already-generated forecast paths (from Kronos or any
future foundation model) and turns them into a conservative, fee-aware gate:

- which side has more forecasted room,
- whether the expected move clears maker/taker costs plus a safety buffer,
- whether forecast samples agree enough to be useful,
- whether adverse path risk is too large.

Research-only. It never imports Kronos/Torch, never downloads model weights,
never writes manifests, and never authorizes orders.
"""

from __future__ import annotations

import argparse
import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

import pandas as pd

ForecastSide = Literal["long", "short"]
ForecastRoute = Literal["maker_taker", "taker_taker"]
ForecastVerdict = Literal[
    "FORECAST_GATE_PASS",
    "FORECAST_TOO_SMALL_AFTER_COSTS",
    "FORECAST_CONFIDENCE_TOO_LOW",
    "FORECAST_REWARD_RISK_TOO_LOW",
    "FORECAST_ADVERSE_PATH_TOO_LARGE",
    "FORECAST_INPUT_INVALID",
]

PRICE_COLUMNS = ("open", "high", "low", "close")


@dataclass(frozen=True)
class KronosForecastGateConfig:
    """Conservative defaults for crypto scalper/swing forecast gating."""

    min_expected_net_bps: float = 25.0
    min_confidence: float = 0.55
    min_reward_risk: float = 1.20
    max_adverse_bps: float = 180.0
    maker_taker_cost_bps: float = 8.0
    taker_taker_cost_bps: float = 12.0
    safety_buffer_bps: float = 5.0
    terminal_weight: float = 0.70
    favorable_weight: float = 0.30
    max_horizon_bars: int = 64

    def __post_init__(self) -> None:
        if self.min_expected_net_bps < 0 or self.safety_buffer_bps < 0:
            raise ValueError("edge thresholds cannot be negative")
        if not 0.0 <= self.min_confidence <= 1.0:
            raise ValueError("min_confidence must be in [0, 1]")
        if self.min_reward_risk < 0:
            raise ValueError("min_reward_risk cannot be negative")
        if self.max_adverse_bps <= 0:
            raise ValueError("max_adverse_bps must be positive")
        if self.maker_taker_cost_bps < 0 or self.taker_taker_cost_bps < 0:
            raise ValueError("route costs cannot be negative")
        if self.max_horizon_bars < 1:
            raise ValueError("max_horizon_bars must be positive")
        if not math.isclose(self.terminal_weight + self.favorable_weight, 1.0):
            raise ValueError("terminal_weight + favorable_weight must equal 1")

    def route_cost_bps(self, route: ForecastRoute) -> float:
        if route == "maker_taker":
            return self.maker_taker_cost_bps
        if route == "taker_taker":
            return self.taker_taker_cost_bps
        raise ValueError(f"unsupported route: {route}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ForecastSideScore:
    side: ForecastSide
    samples: int
    terminal_move_bps: float
    favorable_move_bps: float
    adverse_move_bps: float
    gross_edge_bps: float
    expected_net_bps: float
    confidence: float
    reward_risk: float
    route_cost_bps: float
    required_net_bps: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class KronosForecastGateDecision:
    verdict: ForecastVerdict
    route: ForecastRoute
    selected_side: ForecastSide | None
    recommended_action: str
    primary_blocker: str
    scores: dict[str, dict[str, Any]]
    can_trade: bool = False
    can_promote: bool = False
    research_only: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def score_kronos_forecast_gate(
    context: pd.DataFrame,
    forecast: pd.DataFrame,
    *,
    config: KronosForecastGateConfig = KronosForecastGateConfig(),
    route: ForecastRoute = "maker_taker",
    side: ForecastSide | None = None,
) -> KronosForecastGateDecision:
    """Score a forecast path against the VNEDGE fee wall.

    ``context`` is the closed historical candle window used by the forecaster.
    ``forecast`` is one or more future OHLC paths. Multiple paths can be passed
    by adding a ``sample_id`` column; otherwise the whole frame is one path.
    """

    try:
        entry = _last_close(context)
        paths = _forecast_paths(forecast, config=config)
    except ValueError as exc:
        return KronosForecastGateDecision(
            verdict="FORECAST_INPUT_INVALID",
            route=route,
            selected_side=None,
            recommended_action="SKIP",
            primary_blocker=str(exc),
            scores={},
        )

    sides: tuple[ForecastSide, ...] = (side,) if side else ("long", "short")
    score_rows = {
        candidate: _score_side(entry, paths, candidate, config=config, route=route)
        for candidate in sides
    }
    best = max(score_rows.values(), key=lambda item: item.expected_net_bps)
    verdict, blocker = _verdict(best, config)
    return KronosForecastGateDecision(
        verdict=verdict,
        route=route,
        selected_side=best.side,
        recommended_action=_action(verdict, route),
        primary_blocker=blocker,
        scores={key: value.to_dict() for key, value in score_rows.items()},
    )


def build_kronos_forecast_gate_report(
    rows: Iterable[Mapping[str, Any]],
    *,
    config: KronosForecastGateConfig = KronosForecastGateConfig(),
) -> dict[str, Any]:
    """Summarize many forecast gate decisions for an external runner."""

    decisions: list[dict[str, Any]] = []
    for row in rows:
        context = pd.DataFrame(row.get("context") or [])
        forecast = pd.DataFrame(row.get("forecast") or [])
        route = str(row.get("route") or "maker_taker")
        preferred_side = row.get("side")
        if route not in {"maker_taker", "taker_taker"}:
            route = "maker_taker"
        if preferred_side not in {"long", "short", None}:
            preferred_side = None
        decision = score_kronos_forecast_gate(
            context,
            forecast,
            config=config,
            route=route,  # type: ignore[arg-type]
            side=preferred_side,  # type: ignore[arg-type]
        ).to_dict()
        decision.update({
            "lane_id": row.get("lane_id"),
            "exchange": row.get("exchange"),
            "symbol": row.get("symbol"),
            "timeframe": row.get("timeframe"),
            "source": row.get("source") or "external_forecast",
        })
        decisions.append(decision)
    passes = sum(1 for row in decisions if row["verdict"] == "FORECAST_GATE_PASS")
    return {
        "report_id": "kronos_forecast_gate_v1",
        "mode": "read_only_forecast_gate",
        "summary": {
            "rows": len(decisions),
            "passes": passes,
            "blocked": len(decisions) - passes,
            "can_trade": False,
            "can_promote": False,
        },
        "config": config.to_dict(),
        "rows": decisions,
        "operator_answer": (
            f"{passes}/{len(decisions)} forecast row(s) clear costs and path risk; "
            "all remain research-only until OOS replay proves value."
        ),
        "can_trade": False,
        "can_promote": False,
    }


def _last_close(context: pd.DataFrame) -> float:
    if context.empty:
        raise ValueError("context is empty")
    if "close" not in context.columns:
        raise ValueError("context requires close column")
    value = _finite_float(context["close"].iloc[-1])
    if value <= 0:
        raise ValueError("last context close must be positive")
    return value


def _forecast_paths(
    forecast: pd.DataFrame,
    *,
    config: KronosForecastGateConfig,
) -> list[pd.DataFrame]:
    if forecast.empty:
        raise ValueError("forecast is empty")
    missing = [col for col in PRICE_COLUMNS if col not in forecast.columns]
    if missing:
        raise ValueError(f"forecast missing columns: {', '.join(missing)}")
    frame = forecast.copy()
    for col in PRICE_COLUMNS:
        frame[col] = frame[col].map(_finite_float)
    if (frame[list(PRICE_COLUMNS)] <= 0).any().any():
        raise ValueError("forecast prices must be positive")
    if "sample_id" not in frame.columns:
        frame["sample_id"] = "sample_0"
    paths: list[pd.DataFrame] = []
    for _, path in frame.groupby("sample_id", sort=False):
        if path.empty:
            continue
        paths.append(path.head(config.max_horizon_bars))
    if not paths:
        raise ValueError("forecast has no usable sample paths")
    return paths


def _score_side(
    entry: float,
    paths: list[pd.DataFrame],
    side: ForecastSide,
    *,
    config: KronosForecastGateConfig,
    route: ForecastRoute,
) -> ForecastSideScore:
    terminal: list[float] = []
    favorable: list[float] = []
    adverse: list[float] = []
    route_cost = config.route_cost_bps(route)
    required_net = config.min_expected_net_bps
    gross_threshold = required_net + route_cost + config.safety_buffer_bps
    for path in paths:
        last_close = float(path["close"].iloc[-1])
        high = float(path["high"].max())
        low = float(path["low"].min())
        if side == "long":
            term = _bps(last_close, entry)
            fav = max(0.0, _bps(high, entry))
            adv = max(0.0, _bps(entry, low))
        else:
            term = _bps(entry, last_close)
            fav = max(0.0, _bps(entry, low))
            adv = max(0.0, _bps(high, entry))
        terminal.append(term)
        favorable.append(fav)
        adverse.append(adv)

    terminal_mean = _mean(terminal)
    favorable_mean = _mean(favorable)
    adverse_mean = _mean(adverse)
    gross_edge = (
        config.terminal_weight * terminal_mean
        + config.favorable_weight * favorable_mean
    )
    expected_net = gross_edge - route_cost - config.safety_buffer_bps
    confidence = sum(
        1
        for term, fav in zip(terminal, favorable)
        if term > 0 and fav >= gross_threshold
    ) / len(paths)
    reward_risk = favorable_mean / max(adverse_mean, 1e-9)
    return ForecastSideScore(
        side=side,
        samples=len(paths),
        terminal_move_bps=round(terminal_mean, 6),
        favorable_move_bps=round(favorable_mean, 6),
        adverse_move_bps=round(adverse_mean, 6),
        gross_edge_bps=round(gross_edge, 6),
        expected_net_bps=round(expected_net, 6),
        confidence=round(confidence, 6),
        reward_risk=round(reward_risk, 6),
        route_cost_bps=route_cost,
        required_net_bps=required_net,
    )


def _verdict(
    score: ForecastSideScore,
    config: KronosForecastGateConfig,
) -> tuple[ForecastVerdict, str]:
    if score.expected_net_bps < config.min_expected_net_bps:
        return "FORECAST_TOO_SMALL_AFTER_COSTS", (
            f"expected net {score.expected_net_bps:.1f}bps < "
            f"{config.min_expected_net_bps:.1f}bps"
        )
    if score.confidence < config.min_confidence:
        return "FORECAST_CONFIDENCE_TOO_LOW", (
            f"confidence {score.confidence:.2f} < {config.min_confidence:.2f}"
        )
    if score.reward_risk < config.min_reward_risk:
        return "FORECAST_REWARD_RISK_TOO_LOW", (
            f"reward/risk {score.reward_risk:.2f} < {config.min_reward_risk:.2f}"
        )
    if score.adverse_move_bps > config.max_adverse_bps:
        return "FORECAST_ADVERSE_PATH_TOO_LARGE", (
            f"adverse path {score.adverse_move_bps:.1f}bps > "
            f"{config.max_adverse_bps:.1f}bps"
        )
    return "FORECAST_GATE_PASS", "forecast clears cost, confidence, and path-risk gates"


def _action(verdict: ForecastVerdict, route: ForecastRoute) -> str:
    if verdict != "FORECAST_GATE_PASS":
        return "SKIP"
    if route == "taker_taker":
        return "ALLOW_TAKER_RESEARCH_ONLY"
    return "ALLOW_MAKER_RESEARCH_ONLY"


def _bps(a: float, b: float) -> float:
    return ((a - b) / b) * 10_000.0


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _finite_float(value: Any) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("non-finite price value")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context-csv", type=Path, required=True)
    parser.add_argument("--forecast-csv", type=Path, required=True)
    parser.add_argument("--route", choices=["maker_taker", "taker_taker"], default="maker_taker")
    parser.add_argument("--side", choices=["long", "short"], default=None)
    parser.add_argument("--out", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    context = pd.read_csv(args.context_csv)
    forecast = pd.read_csv(args.forecast_csv)
    decision = score_kronos_forecast_gate(
        context,
        forecast,
        route=args.route,
        side=args.side,
    ).to_dict()
    text = json.dumps(decision, indent=2, sort_keys=True)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
