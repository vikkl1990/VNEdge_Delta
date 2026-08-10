"""Purged walk-forward for ML models.

Per rolling window: build features and labels on the TRAIN slice only, purge
the label horizon (embargo — the last `horizon_bars` of train rows have
labels that peek into the test window), fit a fresh model, then judge the
frozen model with the SAME cost-aware backtester and the SAME promotion
gates every rule-based strategy faces. Reuses WindowResult/WalkForwardResult
so evaluate_promotion applies verbatim.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

from vnedge.backtest.backtester import BacktestConfig, run_backtest
from vnedge.backtest.metrics import compute_metrics
from vnedge.backtest.walk_forward import WalkForwardResult, WindowResult
from vnedge.ml.feature_matrix import FEATURE_COLUMNS, FeatureParams, build_feature_matrix
from vnedge.ml.labels import triple_barrier_labels
from vnedge.ml.ml_strategy import MLStrategy
from vnedge.ml.trainer import train_classifier

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MLRoundConfig:
    """The pre-registerable experiment definition."""

    stop_atr_mult: float = 2.0
    target_r: float = 2.0
    horizon_bars: int = 24
    threshold: float = 0.60
    train_bars: int = 2160  # 90d on 1h
    test_bars: int = 720    # 30d
    model_params: dict | None = None
    features: FeatureParams = FeatureParams()


def _train_window_model(train: pd.DataFrame, funding, cfg: MLRoundConfig):
    df = build_feature_matrix(train, funding, cfg.features)
    labels = triple_barrier_labels(
        train, stop_atr_mult=cfg.stop_atr_mult, target_r=cfg.target_r,
        horizon_bars=cfg.horizon_bars,
    )
    # Embargo: the last horizon_bars rows have labels that look past the
    # train boundary into the test window. They must not be trained on.
    usable = df.index < (len(df) - cfg.horizon_bars)
    valid = usable & df[FEATURE_COLUMNS].notna().all(axis=1) & labels.notna()
    X, y = df.loc[valid, FEATURE_COLUMNS], labels[valid]
    return train_classifier(X, y, cfg.model_params, compute_importances=False)


def walk_forward_ml(
    candles: pd.DataFrame,
    funding: pd.DataFrame | None,
    cfg: MLRoundConfig,
    bt_config: BacktestConfig,
    *,
    symbol: str = "BTC/USDT:USDT",
    timeframe: str = "1h",
) -> WalkForwardResult:
    n = len(candles)
    if cfg.train_bars + cfg.test_bars > n:
        raise ValueError(f"not enough data: need {cfg.train_bars + cfg.test_bars}, have {n}")

    windows: list[WindowResult] = []
    start = 0
    while start + cfg.train_bars + cfg.test_bars <= n:
        train = candles.iloc[start : start + cfg.train_bars].reset_index(drop=True)
        try:
            trained = _train_window_model(train, funding, cfg)
        except ValueError as exc:
            logger.warning("window %d: training refused (%s) — skipping", len(windows), exc)
            start += cfg.test_bars
            continue

        def make_strategy() -> MLStrategy:
            return MLStrategy(
                trained, funding,
                model_version=f"wf_w{len(windows)}",
                threshold=cfg.threshold, stop_atr_mult=cfg.stop_atr_mult,
                take_profit_r=cfg.target_r, features=cfg.features,
            )

        is_metrics = compute_metrics(
            run_backtest(train, funding, make_strategy(), bt_config,
                         symbol=symbol, timeframe=timeframe)
        )
        prefix = make_strategy().warmup_bars
        test_slice = candles.iloc[
            start + cfg.train_bars - prefix : start + cfg.train_bars + cfg.test_bars
        ].reset_index(drop=True)
        oos_metrics = compute_metrics(
            run_backtest(test_slice, funding, make_strategy(), bt_config,
                         symbol=symbol, timeframe=timeframe)
        )
        windows.append(
            WindowResult(
                window_index=len(windows),
                train_start=train["timestamp"].iloc[0],
                test_start=candles["timestamp"].iloc[start + cfg.train_bars],
                test_end=candles["timestamp"].iloc[start + cfg.train_bars + cfg.test_bars - 1],
                chosen_params={
                    "threshold": cfg.threshold, "horizon": cfg.horizon_bars,
                    "positive_rate": round(trained.positive_rate, 3),
                },
                train_metrics=is_metrics,
                test_metrics=oos_metrics,
            )
        )
        start += cfg.test_bars
    return WalkForwardResult(windows=tuple(windows))
