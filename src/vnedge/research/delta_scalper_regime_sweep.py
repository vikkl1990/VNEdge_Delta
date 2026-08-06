"""Preregistered hard-regime experiment for the Delta scalper.

This experiment changes regime allow-lists only. It reuses the shared causal
candidate ledger, next-open fills, realistic costs, and conservative exit path.
The frozen tail remains unopened unless a selection-only variant first clears
all preregistered gates.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

from vnedge.research.delta_scalper_threshold_sweep import run_experiment
from vnedge.scalping.delta_engine.config import DeltaScalperConfig
from vnedge.scalping.delta_engine.types import Regime, SignalCandidate

DEFAULT_CONFIG = Path("configs/delta_scalper.yaml")
DEFAULT_BACKTEST = Path("research/live_research/delta_scalper_backtest_latest.json")
DEFAULT_CACHE = Path("data/delta_scalper_cache")
DEFAULT_OUTPUT = Path("research/live_research/delta_scalper_regime_sweep_latest.json")

MOMENTUM = "delta_momentum_burst_v1"
FADE = "delta_imbalance_fade_v1"


@dataclass(frozen=True)
class RegimeVariant:
    config_id: str
    hypothesis: str
    min_probability: float
    min_confidence: float
    min_expectancy_bps: float
    momentum_regimes: tuple[Regime, ...]
    fade_regimes: tuple[Regime, ...]

    def accepts(self, candidate: SignalCandidate) -> bool:
        if (
            candidate.scalper_probability < self.min_probability
            or candidate.confidence < self.min_confidence
            or candidate.fee_adjusted_expectancy_bps < self.min_expectancy_bps
        ):
            return False
        raw_regime = candidate.metadata.get("regime")
        try:
            regime = Regime(str(raw_regime))
        except ValueError:
            return False
        if candidate.scanner_id == MOMENTUM:
            return regime in self.momentum_regimes
        if candidate.scanner_id == FADE:
            return regime in self.fade_regimes
        return False

    def to_dict(self) -> dict[str, object]:
        return {
            "config_id": self.config_id,
            "family": "hard_regime_allowlist",
            "hypothesis": self.hypothesis,
            "unchanged_gates": {
                "min_probability": self.min_probability,
                "min_confidence": self.min_confidence,
                "min_expectancy_bps": self.min_expectancy_bps,
            },
            "momentum_regimes": [regime.value for regime in self.momentum_regimes],
            "fade_regimes": [regime.value for regime in self.fade_regimes],
        }


def preregistered_regime_variants(
    config: DeltaScalperConfig,
) -> tuple[RegimeVariant, ...]:
    engine = config.engine
    momentum = config.scanners.momentum_burst.enabled_regimes
    fade = config.scanners.imbalance_fade.enabled_regimes

    def variant(
        config_id: str,
        hypothesis: str,
        *,
        momentum_regimes: tuple[Regime, ...] = momentum,
        fade_regimes: tuple[Regime, ...] = fade,
    ) -> RegimeVariant:
        return RegimeVariant(
            config_id,
            hypothesis,
            engine.min_probability,
            engine.min_confidence,
            engine.min_expectancy_bps,
            momentum_regimes,
            fade_regimes,
        )

    quiet_expanding = (Regime.QUIET, Regime.EXPANDING)
    directional_trends = (Regime.TRENDING_UP, Regime.TRENDING_DOWN)
    return (
        variant("baseline", "checked-in allow-lists with no experimental filter"),
        variant(
            "momentum_no_directional_trends",
            "remove historically weak directional-trend momentum cells",
            momentum_regimes=(Regime.QUIET, Regime.EXPANDING, Regime.FUNDING_EXTREME),
        ),
        variant(
            "momentum_quiet_expanding",
            "momentum only in non-directional candle regimes",
            momentum_regimes=quiet_expanding,
        ),
        variant(
            "momentum_expanding_only",
            "momentum only during volatility expansion",
            momentum_regimes=(Regime.EXPANDING,),
        ),
        variant(
            "momentum_quiet_only",
            "momentum only during quiet conditions",
            momentum_regimes=(Regime.QUIET,),
        ),
        variant(
            "fade_expanding_only",
            "fade only during volatility expansion",
            fade_regimes=(Regime.EXPANDING,),
        ),
        variant(
            "fade_quiet_only",
            "fade only during quiet conditions",
            fade_regimes=(Regime.QUIET,),
        ),
        variant(
            "fade_only",
            "disable momentum and retain the baseline fade regimes",
            momentum_regimes=(),
        ),
        variant(
            "momentum_only",
            "disable fade and retain the baseline momentum regimes",
            fade_regimes=(),
        ),
        variant(
            "style_aligned",
            "momentum in directional or expanding regimes and fade in quiet",
            momentum_regimes=(*directional_trends, Regime.EXPANDING),
            fade_regimes=(Regime.QUIET,),
        ),
        variant(
            "nontrend_momentum_expanding_fade",
            "remove directional trends and require expansion for fades",
            momentum_regimes=quiet_expanding,
            fade_regimes=(Regime.EXPANDING,),
        ),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--backtest", type=Path, default=DEFAULT_BACKTEST)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--symbols")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--scalper-opted-in", action="store_true")
    parser.add_argument("--deto", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    return parser


async def run(args: argparse.Namespace) -> dict:
    return await run_experiment(
        args,
        variant_factory=preregistered_regime_variants,
        report_id="delta_scalper_regime_sweep_v1",
        experiment_constraints={
            "regime_logic_only": True,
            "existing_causal_regime_labels_only": True,
            "regime_definitions_changed": False,
            "score_thresholds_changed": False,
            "yaml_live_replay_parity": True,
        },
    )


def main() -> None:
    payload = asyncio.run(run(_parser().parse_args()))
    print(
        json.dumps(
            {
                "best_diagnostic_config": payload["best_diagnostic_config"],
                "selected_config": payload["selected_config"],
                "frozen_window": payload["frozen_window"],
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
