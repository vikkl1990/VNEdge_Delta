"""ML strategy wrapper — how a model is ALLOWED to trade.

The model enters the system as a BaseStrategy, which means every existing
safety layer applies to it unchanged: cost-aware backtesting, walk-forward
promotion gates, the pre-trade risk gateway, journaling, kill switch. The
model emits probabilities; this wrapper decides whether a probability is a
tradable intent (threshold), and attaches the mandatory stop and target that
the label was trained against. The model cannot place trades, cannot skip
the gateway, and cannot exist outside the registry in live use.

Inference is batched in prepare() — the frozen model scores every bar's
(causal) feature row at once, so signal() is a lookup. Long-only in v1: the
label answers a long question; a short model is a separate, pre-registered
experiment.
"""

from __future__ import annotations

import math

import pandas as pd

from vnedge.ml.feature_matrix import FEATURE_COLUMNS, FeatureParams, build_feature_matrix
from vnedge.ml.trainer import TrainedModel
from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent


class MLStrategy(BaseStrategy):
    def __init__(
        self,
        trained: TrainedModel,
        funding: pd.DataFrame | None,
        *,
        model_version: str = "unregistered",
        threshold: float = 0.60,
        stop_atr_mult: float = 2.0,
        take_profit_r: float = 2.0,
        features: FeatureParams = FeatureParams(),
    ) -> None:
        if not 0.5 <= threshold < 1.0:
            raise ValueError("threshold must be in [0.5, 1.0) — below coin-flip is not a signal")
        self.trained = trained
        self.funding = funding
        self.model_version = model_version
        self.threshold = threshold
        self.stop_atr_mult = stop_atr_mult
        self.take_profit_r = take_profit_r
        self.features = features
        self.strategy_id = f"ml_{model_version}"
        self.warmup_bars = features.warmup_bars

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        df = build_feature_matrix(candles, self.funding, self.features)
        valid = df[FEATURE_COLUMNS].notna().all(axis=1)
        df["p_up"] = float("nan")
        if valid.any():
            df.loc[valid, "p_up"] = self.trained.predict_proba_up(
                df.loc[valid, FEATURE_COLUMNS]
            )
        return df

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        row = df.iloc[index]
        p_up = float(row["p_up"])
        atr = float(row["atr"])
        if math.isnan(p_up) or math.isnan(atr) or atr <= 0:
            return None
        if p_up < self.threshold:
            return None
        close = float(row["close"])
        stop_dist = self.stop_atr_mult * atr
        return SignalIntent(
            side="long",
            stop_price=close - stop_dist,
            take_profit_price=close + self.take_profit_r * stop_dist,
            reason=(
                f"model {self.model_version}: p_up={p_up:.3f} >= {self.threshold} "
                f"(top features: "
                f"{', '.join(n for n, _ in self.trained.importances[:3]) or 'n/a'})"
            ),
        )
