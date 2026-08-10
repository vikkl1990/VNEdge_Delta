"""Fee-wall / microstructure / session feature additions to the ML matrix."""

import numpy as np
import pandas as pd

from vnedge.ml.feature_matrix import FEATURE_COLUMNS, FeatureParams, build_feature_matrix

NEW_FEATURES = [
    "atr_bps", "range_bps",
    "body_atr", "upper_wick_atr", "lower_wick_atr",
    "ret_accel", "vol_ratio", "funding_z",
    "hour_sin", "hour_cos",
]
# funding_z is NaN when no funding series is supplied (constant funding), by the
# same "not computable" convention as the existing funding_pct feature.
MICRO_FEATURES = [f for f in NEW_FEATURES if f != "funding_z"]


def _candles(n=500, seed=0):
    rng = np.random.default_rng(seed)
    ts = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    open_ = close + rng.normal(0, 0.3, n)
    high = np.maximum(open_, close) + np.abs(rng.normal(0, 0.5, n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0, 0.5, n))
    vol = rng.uniform(100, 1000, n)
    return pd.DataFrame(
        {"timestamp": ts, "open": open_, "high": high, "low": low, "close": close, "volume": vol}
    )


def test_new_features_are_in_the_contract_and_output():
    df = build_feature_matrix(_candles(), None, FeatureParams())
    for f in NEW_FEATURES:
        assert f in FEATURE_COLUMNS, f
        assert f in df.columns, f


def test_microstructure_features_finite_post_warmup():
    params = FeatureParams()
    df = build_feature_matrix(_candles(600), None, params)
    tail = df.iloc[params.warmup_bars + 5 :]
    for f in MICRO_FEATURES:
        assert np.isfinite(tail[f].to_numpy(dtype=float)).all(), f


def test_atr_bps_positive_and_hour_on_unit_circle():
    df = build_feature_matrix(_candles(), None, FeatureParams())
    row = df.iloc[300]
    assert row["atr_bps"] > 0.0
    # cyclical hour encoding must lie on the unit circle
    assert abs(row["hour_sin"] ** 2 + row["hour_cos"] ** 2 - 1.0) < 1e-9


def test_new_features_are_causal():
    """Mutating FUTURE bars must not change the new feature values at bar 300."""
    candles = _candles(400)
    before = build_feature_matrix(candles, None, FeatureParams())
    tampered = candles.copy()
    tampered.loc[tampered.index > 300, ["open", "high", "low", "close", "volume"]] *= 7.0
    after = build_feature_matrix(tampered, None, FeatureParams())
    for f in NEW_FEATURES:
        b, a = before.loc[300, f], after.loc[300, f]
        assert (pd.isna(b) and pd.isna(a)) or abs(float(b) - float(a)) < 1e-9, f
