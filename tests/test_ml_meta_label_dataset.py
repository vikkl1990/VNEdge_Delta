"""Meta-label dataset builder: journal parsing + leakage-free feature join."""

import json

import numpy as np
import pandas as pd

from vnedge.ml.feature_matrix import FEATURE_COLUMNS, FeatureParams, build_feature_matrix
from vnedge.ml.meta_label_dataset import (
    TradeOutcome,
    build_meta_label_dataset,
    load_lane_journal_trades,
    parse_journal_trades,
)

SYMBOL = "ETH/USD:USD"


def _candles(n=500, seed=0, freq="1h"):
    rng = np.random.default_rng(seed)
    ts = pd.date_range("2024-01-01", periods=n, freq=freq, tz="UTC")
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    open_ = close + rng.normal(0, 0.3, n)
    high = np.maximum(open_, close) + np.abs(rng.normal(0, 0.5, n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0, 0.5, n))
    vol = rng.uniform(100, 1000, n)
    return pd.DataFrame(
        {"timestamp": ts, "open": open_, "high": high, "low": low, "close": close, "volume": vol}
    )


def _ms(ts) -> int:
    return int(pd.Timestamp(ts).value // 1_000_000)


def _outcome_record(ts, net, side="long", symbol=SYMBOL, strategy="stealth_trail_bbp_v1"):
    return {
        "kind": "shadow_outcome",
        "intent_key": f"{strategy}|{symbol}|{side}|{_ms(ts)}",
        "virtual_net_usd": net,
        "symbol": symbol,
        "side": side,
        "lane": "papertrial_x",
    }


def test_parse_journal_trades_reads_intent_key_entry_time():
    candles = _candles()
    ts = candles["timestamp"].iloc[300]
    records = [
        {"kind": "lane_eval", "detail": "waiting"},          # ignored
        _outcome_record(ts, 4.2, side="long"),
        {"kind": "shadow_outcome", "intent_key": "bad"},     # malformed -> skipped
    ]
    trades = parse_journal_trades(records)
    assert len(trades) == 1
    t = trades[0]
    assert isinstance(t, TradeOutcome)
    assert t.symbol == SYMBOL and t.side == "long" and t.net_usd == 4.2
    assert t.entry_ts == pd.Timestamp(ts).tz_convert("UTC")


def test_parse_reads_nested_payload_records():
    # Real decision-journal rows nest their fields under "payload".
    ts = pd.Timestamp("2024-06-01T04:00:00Z")
    records = [{
        "ts": ts.isoformat(), "kind": "shadow_outcome", "lane": "papertrial_x",
        "payload": {
            "intent_key": f"stealth_trail_bbp_v1|ETH/USD:USD|long|{_ms(ts)}",
            "virtual_net_usd": 2.5, "symbol": "ETH/USD:USD", "side": "long",
        },
    }]
    trades = parse_journal_trades(records)
    assert len(trades) == 1
    assert trades[0].symbol == "ETH/USD:USD"
    assert trades[0].side == "long" and trades[0].net_usd == 2.5
    assert trades[0].entry_ts == ts


def test_build_dataset_labels_and_features_align():
    candles = _candles()
    entries = candles["timestamp"].iloc[[300, 350, 400]]
    trades = [
        TradeOutcome("stealth_trail_bbp_v1", SYMBOL, "long", pd.Timestamp(entries.iloc[0]), +5.0),
        TradeOutcome("stealth_trail_bbp_v1", SYMBOL, "short", pd.Timestamp(entries.iloc[1]), -3.0),
        TradeOutcome("luxy_ut_bot_forecast_v1", SYMBOL, "long", pd.Timestamp(entries.iloc[2]), +1.0),
    ]
    df, summary = build_meta_label_dataset(trades, {SYMBOL: candles})

    assert summary["samples"] == 3
    assert set(FEATURE_COLUMNS).issubset(df.columns)
    # labels follow net sign
    assert df.sort_values("entry_ts")["meta_label"].tolist() == [1.0, 0.0, 1.0]
    assert abs(summary["win_rate"] - 2 / 3) < 1e-9
    # features are finite and match the causal matrix at the entry bar (no leak)
    fm = build_feature_matrix(candles, None, FeatureParams()).set_index("timestamp")
    for _, r in df.iterrows():
        ref = float(fm.loc[r["entry_ts"], "atr_bps"])
        assert np.isfinite(r["atr_bps"]) and abs(float(r["atr_bps"]) - ref) < 1e-9
    assert summary["by_strategy"]["stealth_trail_bbp_v1"] == 2


def test_build_dataset_drops_warmup_unknown_and_missing():
    candles = _candles()
    warmup_ts = candles["timestamp"].iloc[5]      # warmup -> NaN features
    good_ts = candles["timestamp"].iloc[400]
    trades = [
        TradeOutcome("s", SYMBOL, "long", pd.Timestamp(warmup_ts), 1.0),
        TradeOutcome("s", "DOGE/USD:USD", "long", pd.Timestamp(good_ts), 1.0),  # no candles
        TradeOutcome("s", SYMBOL, "long", pd.Timestamp("2099-01-01", tz="UTC"), 1.0),  # no bar
        TradeOutcome("s", SYMBOL, "long", pd.Timestamp(good_ts), 2.0),  # keeper
    ]
    df, summary = build_meta_label_dataset(trades, {SYMBOL: candles})
    assert summary["samples"] == 1
    assert summary["dropped_no_symbol"] == 1
    assert summary["dropped_no_bar"] == 1
    assert summary["dropped_nan_feature"] == 1


def test_empty_input_is_honest_not_an_error():
    df, summary = build_meta_label_dataset([], {SYMBOL: _candles()})
    assert summary["samples"] == 0 and summary["win_rate"] == 0.0
    assert list(df.columns)[: len(FEATURE_COLUMNS)] == FEATURE_COLUMNS


def test_load_lane_journal_trades_stamps_lane_from_filename(tmp_path):
    # Records carry no "lane" field; the filename stem is the lane id.
    candles = _candles()
    ts = candles["timestamp"].iloc[300]
    rec = {
        "kind": "shadow_outcome",
        "intent_key": f"stealth_trail_bbp_v1|{SYMBOL}|long|{_ms(ts)}",
        "virtual_net_usd": 3.0, "symbol": SYMBOL, "side": "long",
    }
    lane_id = "stealth_trail_bbp_v1_bybit_ethusdt_shadow"
    (tmp_path / f"{lane_id}.journal.jsonl").write_text(json.dumps(rec) + "\n")
    trades = load_lane_journal_trades(tmp_path)
    assert len(trades) == 1 and trades[0].lane == lane_id


def test_lane_candles_align_a_trade_the_symbol_lake_cannot():
    # Same symbol, two timeframes: the lake has 1h bars, the lane trades 5m.
    # A 5m entry off the hour has no 1h bar -> the lane cache must rescue it.
    lake_1h = _candles(freq="1h")
    lane_5m = _candles(n=800, seed=1, freq="5min")
    off_hour = lane_5m["timestamp"].iloc[601]   # 601*5min -> :05, never a 1h bar
    assert off_hour not in set(lake_1h["timestamp"])  # genuinely absent from the lake
    trade = TradeOutcome("sats_5m_scalper_v1", SYMBOL, "long",
                         pd.Timestamp(off_hour), +2.0, lane="fastlane")

    # symbol lake alone drops it (no 1h bar at that minute)
    _, base = build_meta_label_dataset([trade], {SYMBOL: lake_1h})
    assert base["samples"] == 0 and base["dropped_no_bar"] == 1

    # the lane's own 5m cache aligns it -> labeled
    _, fixed = build_meta_label_dataset(
        [trade], {SYMBOL: lake_1h}, candles_by_lane={"fastlane": lane_5m}
    )
    assert fixed["samples"] == 1 and fixed["dropped_no_bar"] == 0


def test_symbol_lake_is_the_fallback_when_lane_has_no_cache():
    # A trade whose lane has no cache still resolves via the symbol lake.
    candles = _candles()
    ts = candles["timestamp"].iloc[400]
    trade = TradeOutcome("s", SYMBOL, "long", pd.Timestamp(ts), +1.0, lane="retired_lane")
    _, summary = build_meta_label_dataset(
        [trade], {SYMBOL: candles}, candles_by_lane={"other_lane": _candles(seed=9)}
    )
    assert summary["samples"] == 1
