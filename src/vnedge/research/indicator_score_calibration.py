"""Chronological outcome calibration for indicator-family research scores."""

from __future__ import annotations

import argparse
import json
from bisect import bisect_right
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from math import isfinite
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import numpy as np

from vnedge.scalping.delta_engine.indicator_scoring import (
    IndicatorFamilyScorer,
    IndicatorScoringConfig,
    default_indicator_scoring_config,
    historical_trade_indicator_evidence,
    load_indicator_scoring_config,
)
from vnedge.scalping.delta_engine.types import Side


@dataclass(frozen=True)
class CalibrationConfig:
    historical_tail_fraction: float = 0.20
    score_buckets: int = 10
    minimum_top_band_trades: int = 100
    minimum_monotonic_step_fraction: float = 0.70
    minimum_top_band_profit_factor: float = 1.20
    minimum_top_band_average_net_bps: float = 3.0
    minimum_positive_markets: int = 2

    def __post_init__(self) -> None:
        if not 0 < self.historical_tail_fraction < 0.5:
            raise ValueError("historical_tail_fraction must be in (0, 0.5)")
        if self.score_buckets < 2:
            raise ValueError("score_buckets must be >= 2")


def _profit_factor(values: list[float]) -> float | None:
    gains = sum(value for value in values if value > 0)
    losses = abs(sum(value for value in values if value < 0))
    if losses == 0:
        return None if gains == 0 else float("inf")
    return gains / losses


def _metrics(rows: list[dict[str, Any]]) -> dict[str, object]:
    net = [float(row["net_bps"]) for row in rows]
    mfe = [float(row["mfe_bps"]) for row in rows if row.get("mfe_bps") is not None]
    mae = [float(row["mae_bps"]) for row in rows if row.get("mae_bps") is not None]
    return {
        "trades": len(rows),
        "average_score": sum(float(row["score"]) for row in rows) / len(rows) if rows else 0.0,
        "average_net_bps": sum(net) / len(net) if net else 0.0,
        "total_net_bps": sum(net),
        "profit_factor": _profit_factor(net),
        "win_rate": sum(value > 0 for value in net) / len(net) if net else 0.0,
        "false_signal_rate": sum(value <= 0 for value in net) / len(net) if net else 0.0,
        "average_mfe_bps": sum(mfe) / len(mfe) if mfe else None,
        "average_mae_bps": sum(mae) / len(mae) if mae else None,
    }


def _market_metrics(rows: list[dict[str, Any]]) -> dict[str, dict[str, object]]:
    symbols = sorted({str(row["symbol"]) for row in rows})
    return {symbol: _metrics([row for row in rows if row["symbol"] == symbol]) for symbol in symbols}


def _quantile_boundaries(values: list[float], buckets: int) -> list[float]:
    if not values:
        raise ValueError("cannot build score buckets without selection scores")
    return [float(np.quantile(values, index / buckets)) for index in range(1, buckets)]


def _step_fraction(deciles: list[dict[str, object]], window: str) -> float:
    averages = [
        float(row[window]["average_net_bps"])
        for row in deciles
        if isinstance(row.get(window), dict) and int(row[window]["trades"]) > 0
    ]
    if len(averages) < 2:
        return 0.0
    return sum(right >= left for left, right in pairwise(averages)) / (
        len(averages) - 1
    )


def _score_trade(
    trade: dict[str, object],
    *,
    source_data_quality_pass: bool,
    scorer: IndicatorFamilyScorer,
) -> dict[str, Any]:
    side = Side(str(trade["side"]).lower())
    decision_ts = datetime.fromisoformat(str(trade["decision_ts"]))
    result = scorer.score(
        symbol=str(trade["symbol"]),
        side=side,
        decision_ts=decision_ts,
        evidence=historical_trade_indicator_evidence(
            trade,
            source_data_quality_pass=source_data_quality_pass,
            config=scorer.config,
        ),
    )
    return {
        "decision_ts": decision_ts,
        "symbol": str(trade["symbol"]),
        "scanner_id": str(trade["scanner_id"]),
        "side": side.value,
        "score": result.composite_score,
        "coverage": result.coverage,
        "score_confidence": result.confidence,
        "quality_band": result.quality_band,
        "research_qualified": result.research_qualified,
        "blockers": result.blockers,
        "family_scores": {row.family.value: row.score for row in result.families},
        "net_bps": float(trade["net_bps"]),
        "mfe_bps": trade.get("mfe_bps"),
        "mae_bps": trade.get("mae_bps"),
        "exit_reason": trade.get("exit_reason"),
    }


def build_indicator_score_calibration(
    backtest: dict[str, object],
    *,
    scoring_config: IndicatorScoringConfig | None = None,
    calibration_config: CalibrationConfig | None = None,
) -> dict[str, object]:
    calibration_config = calibration_config or CalibrationConfig()
    markets = backtest.get("markets")
    if not isinstance(markets, dict) or not markets:
        raise ValueError("backtest artifact has no market trade records")
    scorer = IndicatorFamilyScorer(scoring_config or default_indicator_scoring_config())
    scored: list[dict[str, Any]] = []
    source_quality: dict[str, bool] = {}
    for symbol, market in markets.items():
        if not isinstance(market, dict):
            continue
        summary = market.get("summary") if isinstance(market.get("summary"), dict) else {}
        quality_pass = bool(summary.get("data_quality_pass"))
        source_quality[str(symbol)] = quality_pass
        trades = market.get("trades") if isinstance(market.get("trades"), list) else []
        for trade in trades:
            if not isinstance(trade, dict):
                continue
            scored.append(
                _score_trade(
                    trade,
                    source_data_quality_pass=quality_pass,
                    scorer=scorer,
                )
            )
    scored.sort(key=lambda row: row["decision_ts"])
    if not scored:
        raise ValueError("backtest artifact contains no scoreable trades")
    split = int(len(scored) * (1.0 - calibration_config.historical_tail_fraction))
    if split <= 0 or split >= len(scored):
        raise ValueError("chronological split produced an empty window")
    selection = scored[:split]
    historical_tail = scored[split:]
    boundaries = _quantile_boundaries(
        [float(row["score"]) for row in selection],
        calibration_config.score_buckets,
    )
    for row in scored:
        row["score_decile"] = min(
            calibration_config.score_buckets,
            bisect_right(boundaries, float(row["score"])) + 1,
        )
    deciles: list[dict[str, object]] = []
    for bucket in range(1, calibration_config.score_buckets + 1):
        selection_rows = [row for row in selection if row["score_decile"] == bucket]
        tail_rows = [row for row in historical_tail if row["score_decile"] == bucket]
        deciles.append(
            {
                "decile": bucket,
                "selection": _metrics(selection_rows),
                "historical_tail": _metrics(tail_rows),
                "historical_tail_markets": _market_metrics(tail_rows),
            }
        )
    top_tail = [
        row for row in historical_tail if row["score_decile"] == calibration_config.score_buckets
    ]
    top_metrics = _metrics(top_tail)
    top_markets = _market_metrics(top_tail)
    positive_markets = sum(float(metrics["average_net_bps"]) > 0 for metrics in top_markets.values())
    tail_step_fraction = _step_fraction(deciles, "historical_tail")
    pf = top_metrics["profit_factor"]
    gates = {
        "source_data_quality": {
            "passed": all(source_quality.values()),
            "actual": source_quality,
        },
        "top_band_samples": {
            "passed": int(top_metrics["trades"]) >= calibration_config.minimum_top_band_trades,
            "actual": top_metrics["trades"],
            "required": calibration_config.minimum_top_band_trades,
        },
        "monotonic_tail_separation": {
            "passed": tail_step_fraction >= calibration_config.minimum_monotonic_step_fraction,
            "actual": tail_step_fraction,
            "required": calibration_config.minimum_monotonic_step_fraction,
        },
        "top_band_profit_factor": {
            "passed": pf is not None and isfinite(float(pf)) and float(pf) >= calibration_config.minimum_top_band_profit_factor,
            "actual": pf,
            "required": calibration_config.minimum_top_band_profit_factor,
        },
        "top_band_average_net": {
            "passed": float(top_metrics["average_net_bps"]) >= calibration_config.minimum_top_band_average_net_bps,
            "actual": top_metrics["average_net_bps"],
            "required": calibration_config.minimum_top_band_average_net_bps,
        },
        "positive_markets": {
            "passed": positive_markets >= calibration_config.minimum_positive_markets,
            "actual": positive_markets,
            "required": calibration_config.minimum_positive_markets,
        },
    }
    all_gates = all(bool(row["passed"]) for row in gates.values())
    family_attribution: list[dict[str, object]] = []
    family_names = sorted({name for row in scored for name in row["family_scores"]})
    for family in family_names:
        pairs = [
            (float(row["family_scores"][family]), float(row["net_bps"]))
            for row in historical_tail
            if family in row["family_scores"]
        ]
        pair_array = np.array(pairs)
        correlation = (
            float(np.corrcoef(pair_array.T)[0, 1])
            if len(pairs) >= 2
            and float(np.std(pair_array[:, 0])) > 0
            and float(np.std(pair_array[:, 1])) > 0
            else None
        )
        if correlation is not None and not isfinite(correlation):
            correlation = None
        family_attribution.append(
            {
                "family": family,
                "trades": len(pairs),
                "score_net_correlation": correlation,
            }
        )
    qualified_count = sum(bool(row["research_qualified"]) for row in scored)
    source_window = backtest.get("window") if isinstance(backtest.get("window"), dict) else {}
    return {
        "schema_version": "vnedge.indicator_score_calibration.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "policy_version": scorer.config.policy_version,
        "source": {
            "window": source_window,
            "trades": len(scored),
            "markets": sorted(source_quality),
            "market_data_quality": source_quality,
            "event_families_available": False,
            "missing_families": ["participation", "order_flow", "liquidity"],
        },
        "chronological_split": {
            "selection_trades": len(selection),
            "historical_tail_trades": len(historical_tail),
            "historical_tail_fraction": calibration_config.historical_tail_fraction,
            "tail_is_newly_sealed": False,
            "note": "The archived tail was previously inspected; this is diagnostic evidence only.",
        },
        "score_boundaries_from_selection": boundaries,
        "selection": _metrics(selection),
        "historical_tail": _metrics(historical_tail),
        "deciles": deciles,
        "top_score_band": {
            **top_metrics,
            "markets": top_markets,
            "positive_markets": positive_markets,
        },
        "monotonicity": {
            "selection_step_fraction": _step_fraction(deciles, "selection"),
            "historical_tail_step_fraction": tail_step_fraction,
        },
        "family_attribution": family_attribution,
        "research_qualified_trades": qualified_count,
        "gates": gates,
        "verdict": "DIAGNOSTIC_PASS_REQUIRES_NEW_SEALED_DATA" if all_gates else "NO_CALIBRATED_EDGE",
        "policy": {
            "advisory_only": True,
            "used_for_signal": False,
            "used_for_execution": False,
            "historical_tail_is_not_promotion_evidence": True,
            "can_trade": False,
            "can_promote": False,
        },
        "can_trade": False,
        "can_promote": False,
    }


def write_calibration_report(payload: dict[str, object], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", dir=path.parent, encoding="utf-8", delete=False) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backtest",
        type=Path,
        default=Path("research/live_research/delta_scalper_backtest_latest.json"),
    )
    parser.add_argument(
        "--scoring-config",
        type=Path,
        default=Path("configs/research/indicator_family_scoring_v1.yaml"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("research/live_research/indicator_score_calibration_latest.json"),
    )
    args = parser.parse_args()
    backtest = json.loads(args.backtest.read_text(encoding="utf-8"))
    report = build_indicator_score_calibration(
        backtest,
        scoring_config=load_indicator_scoring_config(args.scoring_config),
    )
    write_calibration_report(report, args.output)
    print(json.dumps({
        "output": str(args.output),
        "verdict": report["verdict"],
        "trades": report["source"]["trades"],
        "top_score_band": report["top_score_band"],
        "can_trade": False,
        "can_promote": False,
    }, indent=2))


if __name__ == "__main__":
    main()
